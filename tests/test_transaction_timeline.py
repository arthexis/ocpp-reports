"""Hermetic adversarial timeline, provenance, cursor and browser-page checks."""
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
        db.execute("CREATE TABLE transaction_observations (source_id TEXT, charger_id TEXT,"
                   "transaction_id INTEGER, page_after INTEGER, page_next INTEGER,"
                   "fingerprint TEXT,payload TEXT, imported_at TEXT,"
                   "PRIMARY KEY(source_id,charger_id,transaction_id,page_after,page_next,fingerprint))")
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


def _bound(path, *, source="a", charger="cp", tx=7, first=0, last=40):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO transaction_observations VALUES (?,?,?,?,?,?,?,?)",
                   (source, charger, tx, first, last, "digest", "{}", "2026-01-01T12:00:00Z"))


def test_identity_window_and_real_meter_samples(tmp_path):
    path = _db(tmp_path)
    _bound(path)
    _sample(path, 5)
    _sample(path, 10, power_w=6000, soc=45,
            soc_provenance="ocpp-meter-values")
    _sample(path, 12, source="b")
    _sample(path, 15, charger_id="other")
    _sample(path, 16, connector_id=2)
    _sample(path, 17, transaction_id=8)
    _sample(path, 18, at="2025-12-31T12:18:00Z")
    _sample(path, 19, at="2026-01-01T12:50:00Z")
    _sample(path, 20, at="not-a-date")
    series = transaction_timeline(path, _tx())
    assert [p["energy_kwh"] for p in series["points"]] == [0.5, 1.0]
    assert series["points"][1]["power_kw"] == 6
    assert series["points"][1]["soc"] == 45
    assert [c["field"] for c in timeline_charts(series["points"])] == [
        "energy_kwh", "power_kw", "soc"]
    assert len(transaction_timeline(path, _tx(connector_id=2))["points"]) == 1
    assert transaction_timeline(path, _tx(connector_id=3))["points"] == []
    assert transaction_timeline(path, _tx(started_at="2026-01-01T12:30:00Z"))["points"] == []
    assert transaction_timeline(path, _tx(source_id="b"))["points"] == []


def test_fail_closed_on_counter_drop_and_undocumented_soc(tmp_path):
    path = _db(tmp_path)
    _bound(path)
    _sample(path, 1, energy_wh=1100, power_w=7000, soc=30)
    _sample(path, 2, energy_wh=1600, power_w=6500, soc=45,
            soc_provenance="ocpp-meter-values")
    _sample(path, 3, energy_wh=1200, power_w=5500)
    _sample(path, 4, energy_wh=2100, power_w=4500)
    rows = transaction_timeline(path, _tx())
    assert rows["discontinuous"] is True
    assert [p["energy_kwh"] for p in rows["points"]] == [0.1, 0.6, None, None]
    assert rows["points"][0]["soc"] is None
    assert rows["points"][1]["soc"] == 45
    assert rows["points"][-1]["power_kw"] == 4.5
    assert len([c for c in timeline_charts(rows["points"]) if c["field"] == "energy_kwh"][0]["segments"]) == 1
    assert transaction_timeline(path, _tx(started_at=None))["points"] == []


def test_scoped_index_range_not_latest_source_samples(tmp_path):
    path = _db(tmp_path)
    _bound(path, first=0, last=10)
    _sample(path, 5)
    _sample(path, 10)
    for n in range(200, 240):
        _sample(path, n, transaction_id=8, at="2026-01-01T12:20:00Z")
    rows = transaction_timeline(path, _tx(), scan_limit=3)
    assert [p["energy_kwh"] for p in rows["points"]] == [0.5, 1.0]
    assert rows["limited"] is False
    with sqlite3.connect(path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT payload FROM energy_samples "
                          "WHERE source_id=? AND event_id>? AND event_id<=? "
                          "ORDER BY event_id DESC,sample_index DESC LIMIT ?",
                          ("a", 0, 10, 3)).fetchall()
    assert any("INDEX" in row[3].upper() for row in plan)
    # A legacy local DB without observed cursor bounds must not scan a source.
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM transaction_observations")
    assert transaction_timeline(path, _tx())["points"] == []


def test_bounded_partial_does_not_claim_energy_continuity(tmp_path):
    path = _db(tmp_path)
    _bound(path)
    for n in range(1, 7):
        _sample(path, n)
    series = transaction_timeline(path, _tx(), scan_limit=3, point_limit=3)
    assert series["limited"] is True
    assert len(series["points"]) == 3
    assert all(p["energy_kwh"] is None for p in series["points"])
    assert series["points"][0]["power_kw"] == 7


def test_html_history_empty_and_tenant_rejection(tmp_path):
    path = _db(tmp_path)
    _bound(path)
    _sample(path, 5)
    record = {k: v for k, v in _tx().items() if k != "source_id"}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO current_transactions VALUES (?,?,?,?)",
                   ("a", "cp", 7, json.dumps(record)))
    client = TestClient(web.create_app(path, allowed_source_ids=("a",)))
    page = client.get("/transactions/a/cp/7?return_to=%2F%3Ftx_view%3Dall%26tx_page%3D4%26tz%3DUTC%23transactions")
    assert page.status_code == 200
    assert "Evolución temporal de la carga" in page.text
    assert "Tabla de mediciones verificadas" in page.text
    assert "tx_view=all" in page.text and "tx_page=4" in page.text
    assert 'data-active="false"' in page.text
    assert client.get("/transactions/b/cp/7").status_code == 404
    assert client.get("/transactions/a/another/7").status_code == 404
    bad = client.get("/transactions/a/cp/7?return_to=https%3A%2F%2Fevil.example%2F")
    assert "evil.example" not in bad.text


def test_remote_only_empty_state_and_active_refresh(tmp_path):
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
