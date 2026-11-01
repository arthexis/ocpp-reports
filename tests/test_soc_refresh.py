"""One-transaction SoC refresh: immutable evidence, source isolation and forwarding."""
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocpp_reports.changefeed import changes_page
from ocpp_reports.cli import app
from ocpp_reports.follow import sync_page
from ocpp_reports.replication.collector import Batch, accept_batch
from ocpp_reports.soc_refresh import refresh
from ocpp_reports.warehouse.queries import snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "csms_export_v1" / "active.json"


def seed(tmp_path, source="fixture-source-a"):
    data = json.loads(FIXTURE.read_text())
    data["data"]["source_id"] = source
    # Historical completed projection with pre-feature missing SoC fields.
    data["data"]["transactions"][0]["state"] = "closed"
    data["data"]["transactions"][0]["active"] = False
    data["data"]["transactions"][0]["quality"]["active"] = False
    database = tmp_path / "local.sqlite"
    sync_page(database, data)
    return database


def evidence(*, cp="cp-a", transaction=42, source="fixture-source-a", start=28, end=100):
    return {"schema": "ocpp-csms/transaction-soc/v1",
            "source_id": source, "charger_id": cp, "transaction_id": transaction,
            "soc_start": start, "soc_end": end, "provenance": "ocpp-meter-values"}


def state(db):
    with sqlite3.connect(db) as conn:
        return {
            "cursor": conn.execute("SELECT cursor FROM sources").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "observations": conn.execute("SELECT COUNT(*) FROM transaction_observations").fetchone()[0],
            "changes": conn.execute("SELECT COUNT(*) FROM replication_changes").fetchone()[0],
            "tx": json.loads(conn.execute("SELECT payload FROM current_transactions").fetchone()[0]),
            "imported_at": conn.execute("SELECT imported_at FROM current_transactions").fetchone()[0],
        }


def test_dry_run_apply_idempotence_and_forwarding(tmp_path):
    db = seed(tmp_path)
    original = state(db)
    provider = lambda **_: evidence()
    preview = refresh(db, cp="cp-a", transaction=42, producer=provider)
    assert preview["status"] == "would_update"
    assert preview["fields_changed"] == ["soc_end", "soc_start"]
    assert len(preview["preview_token"]) == 64
    assert preview["imported_at"] == original["imported_at"]
    assert preview["replication_sequence_before"] == preview["replication_sequence_after"]
    assert state(db) == original
    apply_receipt = refresh(db, cp="cp-a", transaction=42, dry_run=False,
                            expect_preview=preview["preview_token"], producer=provider)
    assert apply_receipt["status"] == "updated"
    assert apply_receipt["replication_sequence_after"] > apply_receipt["replication_sequence_before"]
    assert apply_receipt["imported_at"] == original["imported_at"]
    assert apply_receipt["soc_metadata_refreshed_at"]
    updated = state(db)
    assert updated["imported_at"] == original["imported_at"]
    assert updated["tx"]["soc_metadata_refreshed_at"]
    assert updated["tx"]["soc_metadata_provenance"] == "ocpp-meter-values"
    assert updated["cursor"] == original["cursor"]
    assert updated["events"] == original["events"]
    assert updated["observations"] == original["observations"]
    assert updated["changes"] == original["changes"] + 1
    assert updated["tx"]["soc_start"] == 28 and updated["tx"]["soc_end"] == 100
    for key in ("energy_wh", "state", "active", "started_at", "meter_start_wh", "quality"):
        assert updated["tx"][key] == original["tx"][key]
    assert refresh(db, cp="cp-a", transaction=42, dry_run=False, producer=provider)["status"] == "unchanged"
    assert state(db) == updated
    batch = changes_page(db, "fixture-source-a")
    last = batch["changes"][-1]
    assert last["kind"] == "transaction"
    assert last["payload"]["soc_start"] == 28
    remote = tmp_path / "collector.sqlite"
    assert accept_batch(remote, Batch.model_validate(batch)) == batch["next"]
    assert snapshot(remote, allowed_source_ids=("fixture-source-a",))["transactions"][0]["soc_end"] == 100
    assert snapshot(remote, allowed_source_ids=("another-source",))["transactions"] == []


def test_refuses_incorrect_identity_conflicting_or_missing_transaction(tmp_path):
    db = seed(tmp_path)
    before = state(db)
    for bad in (
        evidence(source="different"),
        evidence(cp="different"),
        evidence(transaction=43),
        evidence(end=110),
        {**evidence(), "schema": "invalid"},
        {**evidence(), "provenance": "synthetic"},
    ):
        with pytest.raises(ValueError):
            refresh(db, cp="cp-a", transaction=42, dry_run=False, producer=lambda **_: bad)
        assert state(db) == before
    with pytest.raises(ValueError, match="does not match"):
        refresh(db, cp="cp-a", transaction=42, expected_source="wrong",
                producer=lambda **_: evidence())
    with pytest.raises(ValueError, match="not projected"):
        refresh(db, cp="cp-a", transaction=43, producer=lambda **_: evidence(transaction=43))
    refresh(db, cp="cp-a", transaction=42, dry_run=False, producer=lambda **_: evidence())
    with pytest.raises(ValueError, match="Conflicting soc_end"):
        refresh(db, cp="cp-a", transaction=42, dry_run=False,
                producer=lambda **_: evidence(end=99))
    assert state(db)["tx"]["soc_end"] == 100


def test_null_soc_never_infers_start_or_advances_cursor(tmp_path):
    db = seed(tmp_path)
    before = state(db)
    no_sample = refresh(db, cp="cp-a", transaction=42, dry_run=False,
                        producer=lambda **_: evidence(start=None, end=None))
    assert no_sample["status"] == "unchanged"
    assert state(db) == before
    one = refresh(db, cp="cp-a", transaction=42, dry_run=False,
                  producer=lambda **_: evidence(start=None, end=47))
    assert one["fields_changed"] == ["soc_end"]
    assert state(db)["tx"].get("soc_start") is None


def test_cli_requires_explicit_apply_and_identifiers(monkeypatch, tmp_path):
    db = seed(tmp_path)
    monkeypatch.setattr("ocpp_reports.soc_refresh.active_csms_transaction_soc",
                        lambda **_: evidence())
    # Use explicit injected test producer through wrapper (avoid active service discovery).
    import ocpp_reports.soc_refresh as module
    original = module.refresh
    def injected(database, **kwargs):
        return original(database, **kwargs, producer=lambda **_: evidence())
    monkeypatch.setattr(module, "refresh", injected)
    cmd = ["metadata", "refresh", "--cp", "cp-a", "--transaction", "42",
           "--database", str(db)]
    result = CliRunner().invoke(app, cmd)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "would_update"
    assert state(db)["tx"].get("soc_end") is None
    preview_token = json.loads(result.stdout)["preview_token"]
    result = CliRunner().invoke(app, [*cmd, "--apply", "--expect-preview", preview_token])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "updated"


def test_refuses_refresh_of_live_transaction(tmp_path):
    data = json.loads(FIXTURE.read_text())
    db = tmp_path / "live.sqlite"
    sync_page(db, data)
    with pytest.raises(ValueError, match="completed transaction"):
        refresh(db, cp="cp-a", transaction=42, dry_run=False,
                producer=lambda **_: evidence())


def test_stale_preview_rejects_producer_evidence_change(tmp_path):
    db = seed(tmp_path)
    preview = refresh(db, cp="cp-a", transaction=42,
                      producer=lambda **_: evidence())
    before = state(db)
    with pytest.raises(ValueError, match="Stale SoC preview"):
        refresh(db, cp="cp-a", transaction=42, dry_run=False,
                expect_preview=preview["preview_token"],
                producer=lambda **_: evidence(start=29))
    assert state(db) == before


def test_stale_preview_rejects_local_projection_change(tmp_path):
    db = seed(tmp_path)
    provider = lambda **_: evidence()
    preview = refresh(db, cp="cp-a", transaction=42, producer=provider)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT payload FROM current_transactions").fetchone()
        payload = json.loads(row[0])
        payload["quality"]["source_refreshed"] = True
        conn.execute("UPDATE current_transactions SET payload=?",
                     (json.dumps(payload),))
    before = state(db)
    with pytest.raises(ValueError, match="Stale SoC preview"):
        refresh(db, cp="cp-a", transaction=42, dry_run=False,
                expect_preview=preview["preview_token"], producer=provider)
    assert state(db) == before


def test_preview_does_not_go_stale_from_unrelated_replication(tmp_path):
    db = seed(tmp_path)
    provider = lambda **_: evidence()
    preview = refresh(db, cp="cp-a", transaction=42, producer=provider)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO replication_changes(source_id,kind,record_key,payload)"
            " VALUES (?,?,?,?)",
            ("fixture-source-a", "event", "unrelated", "{}"),
        )
    receipt = refresh(db, cp="cp-a", transaction=42, dry_run=False,
                      expect_preview=preview["preview_token"], producer=provider)
    assert receipt["status"] == "updated"
    assert receipt["replication_sequence_after"] > receipt["replication_sequence_before"]


def test_replay_of_reviewed_preview_refused_after_apply(tmp_path):
    db = seed(tmp_path)
    provider = lambda **_: evidence()
    preview = refresh(db, cp="cp-a", transaction=42, producer=provider)
    refresh(db, cp="cp-a", transaction=42, dry_run=False,
            expect_preview=preview["preview_token"], producer=provider)
    before = state(db)
    with pytest.raises(ValueError, match="Stale SoC preview"):
        refresh(db, cp="cp-a", transaction=42, dry_run=False,
                expect_preview=preview["preview_token"], producer=provider)
    assert state(db) == before


def test_preview_guard_rejects_invalid_use_and_cli_stale_token(monkeypatch, tmp_path):
    db = seed(tmp_path)
    provider = lambda **_: evidence()
    with pytest.raises(ValueError, match="requires --apply"):
        refresh(db, cp="cp-a", transaction=42, expect_preview="0" * 64,
                producer=provider)
    with pytest.raises(ValueError, match="64-character"):
        refresh(db, cp="cp-a", transaction=42, dry_run=False,
                expect_preview="bad", producer=provider)
    import ocpp_reports.soc_refresh as module
    original = module.refresh

    def injected(database, **kwargs):
        return original(database, **kwargs, producer=provider)

    monkeypatch.setattr(module, "refresh", injected)
    arguments = ["metadata", "refresh", "--cp", "cp-a", "--transaction", "42",
                 "--database", str(db)]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    wrong = CliRunner().invoke(app, [*arguments, "--apply", "--expect-preview", "0" * 64])
    assert wrong.exit_code == 2
    assert "Stale SoC preview" in wrong.output
    assert state(db)["tx"].get("soc_end") is None
