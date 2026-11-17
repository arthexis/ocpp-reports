"""Hermetic append-only local/forwarded meter history and explicit enrichment."""
import copy
import json
import sqlite3

import pytest

from ocpp_reports.changefeed import changes_page
from ocpp_reports.replication.collector import Batch, accept_batch
from ocpp_reports.replication.history_queue import queue_history, future_replication
from ocpp_reports.warehouse.history_enrichment import enrich_historical_page
from ocpp_reports.warehouse.ingestion import ingest_page
from ocpp_reports.warehouse.transaction_timeline import transaction_timeline


STAMP = "2026-11-16T20:10:00Z"


def _sample():
    return {"source_event_id": 1, "sample_index": 0, "at": STAMP,
            "charger_id": "cp", "connector_id": 1, "transaction_id": 7,
            "power_w": None, "energy_wh": 2000.0}


def _page(sample):
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "local-uuid", "cursor": {"after": 0, "next": 1, "more": False},
        "events": [{"id": 1, "at": STAMP, "charger_id": "cp",
                    "kind": "meter_values", "action": "MeterValues"}],
        "energy": [sample],
        "transactions": [{"charger_id": "cp", "id": 7, "connector_id": 1,
                          "started_at": "2026-11-16T20:00:00Z",
                          "stopped_at": "2026-11-16T20:20:00Z",
                          "meter_start_wh": 1000, "energy_wh": 1000}],
        "status": {"chargers": []},
    }}


def _tx(source_id):
    return {"source_id": source_id, "charger_id": "cp", "connector_id": 1,
            "id": 7, "started_at": "2026-11-16T20:00:00Z",
            "stopped_at": "2026-11-16T20:20:00Z", "meter_start_wh": 1000}


def _forward(source, collector, after=0, source_id="node-002"):
    batch = changes_page(source, "local-uuid", after=after, limit=500)
    batch["source_id"] = source_id
    return batch, accept_batch(collector, Batch.model_validate(batch))


def _opt_in(database):
    from ocpp_reports.warehouse.schema import _schema
    with sqlite3.connect(database) as db:
        _schema(db)
        db.execute("INSERT INTO meter_replication_sources VALUES ('local-uuid',1)")


def test_replication_sample_is_immutable_scoped_and_outside_latest(tmp_path):
    source, collector = tmp_path / "source.db", tmp_path / "collector.db"
    _opt_in(source)
    ingest_page(source, _page(_sample()))
    batch, cursor = _forward(source, collector)
    assert any(row["kind"] == "meter_sample" for row in batch["changes"])
    assert cursor == batch["next"]
    assert accept_batch(collector, Batch.model_validate(batch)) == cursor
    with sqlite3.connect(collector) as db:
        assert db.execute("SELECT count(*) FROM collector_meter_samples").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM collector_latest WHERE kind='meter_sample'").fetchone()[0] == 0
        assert db.execute("SELECT sequence FROM collector_cursors WHERE source_id='node-002'").fetchone()[0] == cursor
    result = transaction_timeline(collector, _tx("node-002"))
    assert result["source"] == "replicated-meter-samples"
    assert len(result["points"]) == 1
    assert result["points"][0]["energy_kwh"] == 1.0
    assert transaction_timeline(collector, _tx("other-tenant"))["points"] == []
    assert transaction_timeline(collector, {**_tx("node-002"), "connector_id": 2})["points"] == []
    tampered = copy.deepcopy(batch)
    tampered["changes"][1]["payload"]["energy_wh"] = 999
    with pytest.raises(ValueError, match="Conflicting replay"):
        accept_batch(collector, Batch.model_validate(tampered))


def test_explicit_history_queue_is_readonly_bounded_and_restart_safe(tmp_path):
    source = tmp_path / "source.db"
    ingest_page(source, _page(_sample()))
    with sqlite3.connect(source) as db:
        db.execute("DELETE FROM replication_changes WHERE kind='meter_sample'")
        # Simulate immutable legacy samples predating the trigger.
        db.execute("DROP TRIGGER replicate_energy_sample_insert")
        legacy = {**_sample(), "sample_index": 1}
        db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                   ("local-uuid", 1, 1, json.dumps(legacy)))
    original_cursor = sqlite3.connect(source).execute(
        "SELECT cursor FROM sources WHERE source_id='local-uuid'").fetchone()[0]
    preview = queue_history(source, "local-uuid", limit=1)
    assert preview["dry_run"] is True and preview["pending"] == 1
    assert preview["queued"] == 0 and preview["more"] is True
    apply = queue_history(source, "local-uuid", limit=1, apply=True)
    assert apply["queued"] == 1
    second = queue_history(source, "local-uuid",
                           after_event=apply["next"]["event_id"],
                           after_index=apply["next"]["sample_index"],
                           apply=True)
    assert second["queued"] == 1
    assert queue_history(source, "local-uuid", limit=500, apply=True)["queued"] == 0
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT cursor FROM sources WHERE source_id='local-uuid'").fetchone()[0] == original_cursor
        assert db.execute("SELECT count(*) FROM replication_changes WHERE kind='meter_sample'").fetchone()[0] == 2


def test_bounded_historical_enrichment_keeps_raw_bytes_and_cursors(tmp_path):
    source, collector = tmp_path / "source.db", tmp_path / "collector.db"
    old = _sample()
    ingest_page(source, _page(old))
    new = {**old,
           "soc": 85.0, "soc_provenance": "ocpp-meter-values",
           "voltage_v": 220.0, "meter_channels": [
               {"measurand": "SoC", "unit": "Percent", "value": 85.0},
               {"measurand": "Voltage", "unit": "V", "value": 220.0},
           ]}
    upgraded = _page(new)
    with sqlite3.connect(source) as db:
        before = db.execute("SELECT payload FROM energy_samples").fetchone()[0]
        cursor = db.execute("SELECT cursor FROM sources").fetchone()[0]
    preview = enrich_historical_page(source, upgraded, source_id="local-uuid")
    assert preview["pending"] == 1 and preview["inserted"] == 0
    applied = enrich_historical_page(source, upgraded, source_id="local-uuid", apply=True)
    assert applied["inserted"] == 1
    assert enrich_historical_page(source, upgraded, source_id="local-uuid",
                                 apply=True)["inserted"] == 0
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT payload FROM energy_samples").fetchone()[0] == before
        assert db.execute("SELECT cursor FROM sources").fetchone()[0] == cursor
        assert db.execute("SELECT count(*) FROM replication_changes WHERE kind='meter_enrichment'").fetchone()[0] == 1
    points = transaction_timeline(source, _tx("local-uuid"))["points"]
    assert points[0]["soc"] == 85.0 and points[0]["voltage_v"] == 220.0
    assert queue_history(source, "local-uuid", apply=True)["queued"] == 1
    batch, ack = _forward(source, collector)
    remote = transaction_timeline(collector, _tx("node-002"))["points"]
    assert ack == batch["next"] and remote[0]["soc"] == 85.0
    assert remote[0]["voltage_v"] == 220.0
    bad = copy.deepcopy(upgraded)
    bad["data"]["energy"][0]["energy_wh"] = 3000.0
    with pytest.raises(ValueError, match="does not match original evidence"):
        enrich_historical_page(source, bad, source_id="local-uuid")
    with pytest.raises(ValueError, match="Source mismatch"):
        enrich_historical_page(source, upgraded, source_id="other")
    with pytest.raises(ValueError, match="unbounded"):
        enrich_historical_page(source, upgraded, source_id="local-uuid",
                               max_samples=0)


def test_forwarder_never_advances_without_collector_meter_capability(tmp_path):
    import httpx
    from ocpp_reports.replication.forwarder import forward_once, confirmed_cursor

    source, state = tmp_path / "local.db", tmp_path / "forward-state.db"
    _opt_in(source)
    ingest_page(source, _page(_sample()))
    observed = []

    def unsupported(request):
        observed.append(request.url.path)
        if request.url.path.endswith("/ready"):
            return httpx.Response(200, json={
                "schema": "ocpp-reports/readiness/v1", "source_id": "node-002",
                "ready": True, "supported_kinds": ["event", "transaction", "connector"],
            })
        raise AssertionError("Unsupported collector must not receive sample batch")

    with httpx.Client(transport=httpx.MockTransport(unsupported)) as client:
        with pytest.raises(ValueError, match="does not advertise"):
            forward_once(source, state, "node-002",
                         "https://collector.invalid/api/replication",
                         "synthetic", client=client)
    assert observed == ["/api/replication/ready"]
    assert confirmed_cursor(state, "node-002") == 0

    def supported(request):
        observed.append(request.url.path)
        if request.url.path.endswith("/ready"):
            return httpx.Response(200, json={
                "source_id": "node-002", "ready": True,
                "supported_kinds": ["meter_sample", "meter_enrichment"],
            })
        if request.url.path.endswith("/replication"):
            payload = json.loads(request.content)
            return httpx.Response(200, json={"source_id": "node-002",
                                             "ack": payload["next"]})
        raise AssertionError("Unexpected network path")

    with httpx.Client(transport=httpx.MockTransport(supported)) as client:
        ack = forward_once(source, state, "node-002",
                           "https://collector.invalid/api/replication",
                           "synthetic", client=client)
    assert ack > 0
    assert observed[-2:] == ["/api/replication/ready", "/api/replication"]
    assert confirmed_cursor(state, "node-002") == ack


def test_collector_maintenance_resets_only_held_historical_source(tmp_path):
    from pathlib import Path
    from ocpp_reports.collector_admin import maintain

    source, collector = tmp_path / "source.db", tmp_path / "collector.db"
    _opt_in(source)
    ingest_page(source, _page(_sample()))
    batch, _ = _forward(source, collector, source_id="node-002")
    other = {**batch, "source_id": "node-003"}
    accept_batch(collector, Batch.model_validate(other))
    assert maintain(collector, "node-002", "hold")["held"]
    result = maintain(collector, "node-002", "reset", tmp_path / "private-backup.db")
    assert result["backup_verified"] and result["other_sources_unchanged"]
    assert not any(result["after"]["rows"].values())
    with sqlite3.connect(collector) as db:
        assert db.execute("SELECT COUNT(*) FROM collector_meter_samples WHERE source_id='node-002'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM collector_meter_samples WHERE source_id='node-003'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM collector_changes WHERE source_id='node-003'").fetchone()[0] == len(batch["changes"])


def test_future_samples_do_not_replicate_until_explicit_opt_in(tmp_path):
    source = tmp_path / "source.db"
    ingest_page(source, _page(_sample()))
    with sqlite3.connect(source) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM replication_changes WHERE kind='meter_sample'"
        ).fetchone()[0] == 0
    preview = future_replication(source, "local-uuid")
    assert preview == {"source_id": "local-uuid", "dry_run": True,
                       "previously_enabled": False, "enabled": False,
                       "changed": False}
    assert future_replication(source, "local-uuid", apply=True)["changed"] is True
    assert future_replication(source, "local-uuid", apply=True)["changed"] is False
    second = {**_sample(), "source_event_id": 2, "sample_index": 1}
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                   ("local-uuid", 2, 1, json.dumps(second)))
        assert db.execute(
            "SELECT COUNT(*) FROM replication_changes WHERE kind='meter_sample'"
        ).fetchone()[0] == 1
    assert queue_history(source, "local-uuid", apply=False)["pending"] == 1
