"""Hermetic adversarial contract of read-only transaction timelines (#215)."""
import json
import sqlite3

from fastapi.testclient import TestClient

from ocpp_reports import web
from ocpp_reports.warehouse.transaction_timeline import transaction_timeline, timeline_charts


def _db(tmp_path):
    path = tmp_path / "reports.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE energy_samples (source_id TEXT, event_id INTEGER, "
                   "sample_index INTEGER, payload TEXT, "
                   "PRIMARY KEY(source_id,event_id,sample_index))")
        db.execute("CREATE TABLE current_transactions (source_id TEXT,charger_id TEXT,"
                   "transaction_id INTEGER,payload TEXT)")
        db.execute("CREATE TABLE collector_latest (source_id TEXT,kind TEXT,"
                   "record_key TEXT,payload TEXT)")
    return path


def _tx(**changes):
    return {"source_id": "a", "charger_id": "cp", "connector_id": 1,
            "id": 7, "state": "stopped", "active": False,
            "started_at": "2026-01-01T12:00:00Z",
            "stopped_at": "2026-01-01T12:40:00Z",
            "meter_start_wh": 1000, "energy_wh": 2400, **changes}


def _sample(path, event, *, source="a", **changes):
    item = {"at": f"2026-01-01T12:{event:02}:00Z", "source_event_id": event,
            "sample_index": 0, "charger_id": "cp", "connector_id": 1,
            "transaction_id": 7, "energy_wh": 1000 + event * 100,
            "power_w": 7000, **changes}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                   (source, event, 0, json.dumps(item)))


def test_real_samples_only_with_source_charger_connector_transaction_and_time(tmp_path):
    path = _db(tmp_path)
    _sample(path, 5)
    _sample(path, 10, power_w=6000, soc=45)
    _sample(path, 12, source="b")
    _sample(path, 15, charger_id="other")
    _sample(path, 16, connector_id=2)
    _sample(path, 17, transaction_id=8)
    _sample(path, 18, at="2025-12-31T12:18:00Z")
    _sample(path, 19, at="2026-01-01T12:50:00Z")
    _sample(path, 20, at="not-a-date")
    series = transaction_timeline(path, _tx())
    assert [p["energy_kwh"] for p in series["points"]] == [0.5, 1.0]
    assert series["points"][1]["power_kw"] == 6.0
    assert series["points"][1]["soc"] == 45
    assert [c["field"] for c in timeline_charts(series["points"])] == [
        "energy_kwh", "power_kw", "soc"
    ]
    assert transaction_timeline(path, _tx(connector_id=2))["points"] == []
    assert transaction_timeline(path, _tx(started_at="2026-01-01T12:30:00Z"))["points"] == []


def test_missing_history_and_reset_never_invents_energy(tmp_path):
    path = _db(tmp_path)
    assert transaction_timeline(path, _tx())["points"] == []
    _sample(path, 1, energy_wh=10, power_w=None, soc=None)
    assert transaction_timeline(path, _tx())["points"] == []
    _sample(path, 2, energy_wh=10, power_w=5000, soc=150)
    row = transaction_timeline(path, _tx())["points"]
    assert len(row) == 1 and row[0]["energy_kwh"] is None
    assert row[0]["power_kw"] == 5 and row[0]["soc"] is None
    assert [c["field"] for c in timeline_charts(row)] == ["power_kw"]
    assert transaction_timeline(path, _tx(started_at=None))["points"] == []


def test_bounded_scan_and_historical_render_without_active_charge(tmp_path):
    path = _db(tmp_path)
    for n in range(1, 7):
        _sample(path, n)
    series = transaction_timeline(path, _tx(), scan_limit=3, point_limit=3)
    assert series["limited"] is True
    assert len(series["points"]) == 3
    record = {k: v for k, v in _tx().items() if k != "source_id"}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO current_transactions VALUES (?,?,?,?)",
                   ("a", "cp", 7, json.dumps(record)))
    client = TestClient(web.create_app(path, allowed_source_ids=("a",)))
    page = client.get("/transactions/a/cp/7")
    assert page.status_code == 200
    assert "Evolución temporal de la carga" in page.text
    assert "Tabla de mediciones verificadas" in page.text
    assert 'data-active="false"' in page.text
    assert client.get("/transactions/b/cp/7").status_code == 404
    assert client.get("/transactions/a/another/7").status_code == 404


def test_remote_only_is_empty_and_active_retains_refresh(tmp_path):
    path = _db(tmp_path)
    record = {k: v for k, v in _tx(state="open", active=True, stopped_at=None,
                                   energy_wh=None).items() if k != "source_id"}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?)",
                   ("a", "transaction", "cp:7", json.dumps(record)))
    client = TestClient(web.create_app(path, allowed_source_ids=("a",)))
    page = client.get("/transactions/a/cp/7")
    assert page.status_code == 200
    assert "Historial de mediciones no disponible" in page.text
    assert 'data-active="true"' in page.text
    assert '"transaction-timeline"' in page.text
    assert "window.setInterval(refreshTransaction, 15000)" in page.text
