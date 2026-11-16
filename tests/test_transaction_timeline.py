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


def test_decimation_of_over_120_real_points_keeps_original_adjacency(tmp_path):
    """No fake long-range strokes between the 120 points retained from 135."""
    from datetime import datetime, timedelta, timezone

    path = _db(tmp_path)
    _bound(path, first=0, last=200)
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        for event in range(1, 136):
            item = {"source_event_id": event, "sample_index": 0,
                    "charger_id": "cp", "connector_id": 1, "transaction_id": 7,
                    "at": (start + timedelta(seconds=event)).isoformat(),
                    "energy_wh": 1000 + event * 20, "power_w": 6000}
            db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                       ("a", event, 0, json.dumps(item)))
    result = transaction_timeline(path, _tx())
    assert result["limited"] is True
    assert len(result["points"]) == 120
    orders = [p["_source_order"] for p in result["points"]]
    assert orders[0] == 0 and orders[-1] == 134
    assert any(b - a > 1 for a, b in zip(orders, orders[1:]))
    expected_adjacent_pairs = sum(b == a + 1 for a, b in zip(orders, orders[1:]))
    for chart in timeline_charts(result["points"]):
        assert len(chart["dots"]) == 120
        assert len(chart["segments"]) == expected_adjacent_pairs
        assert len(chart["segments"]) < len(chart["dots"]) - 1


def test_null_rows_and_out_of_order_times_break_chart_strokes(tmp_path):
    """Rejected readings and time-reordered events cannot be joined."""
    path = _db(tmp_path)
    _bound(path)
    _sample(path, 1, power_w=5500, energy_wh=1100)
    _sample(path, 2, power_w=None, energy_wh=None)
    _sample(path, 3, power_w=6500, energy_wh=1300)
    _sample(path, 4, at="2026-01-01T12:02:30Z",
            power_w=7000, energy_wh=1400)
    result = transaction_timeline(path, _tx())
    assert len(result["points"]) == 3
    assert [p["_source_order"] for p in result["points"]] == [0, 3, 2]
    charts = {c["field"]: c for c in timeline_charts(result["points"])}
    # Time disorder also exposes a real counter regression: energy is gated
    # after that regression, while all three independent power samples remain.
    assert len(charts["energy_kwh"]["dots"]) == 2
    assert len(charts["power_kw"]["dots"]) == 3
    assert charts["energy_kwh"]["segments"] == []
    assert charts["power_kw"]["segments"] == []


def test_counter_regression_never_reconnects_energy_after_downsampling(tmp_path):
    from datetime import datetime, timedelta, timezone

    path = _db(tmp_path)
    _bound(path, first=0, last=200)
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        for event in range(1, 136):
            # A real meter regression after 70 valid observed records.
            energy = 1000 + (event if event <= 70 else event - 60) * 20
            item = {"source_event_id": event, "sample_index": 0,
                    "charger_id": "cp", "connector_id": 1, "transaction_id": 7,
                    "at": (start + timedelta(seconds=event)).isoformat(),
                    "energy_wh": energy, "power_w": 7000}
            db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                       ("a", event, 0, json.dumps(item)))
    result = transaction_timeline(path, _tx())
    assert result["discontinuous"] is True
    assert result["limited"] is True
    # Selection preserves measured pre-reset energy, but never repairs a reset.
    assert any(p["energy_kwh"] is not None for p in result["points"])
    assert all(p["energy_kwh"] is None for p in result["points"]
               if p["_source_order"] >= 70)
    charts = timeline_charts(result["points"])
    assert [c["field"] for c in charts] == ["energy_kwh", "power_kw"]
    assert len(charts[1]["segments"]) < len(charts[1]["dots"]) - 1
    assert any(b["_source_order"] - a["_source_order"] > 1
               for a, b in zip(result["points"], result["points"][1:]))


def test_compact_detail_uses_verified_timeline_soc_only_as_fallback(tmp_path):
    path = _db(tmp_path)
    _bound(path)
    _sample(path, 5, soc=42, soc_provenance="ocpp-meter-values")
    _sample(path, 10, soc=68, soc_provenance="ocpp-meter-values")
    record = {k: v for k, v in _tx(soc_start=25, soc_end=None).items()
              if k != "source_id"}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO current_transactions VALUES (?,?,?,?)",
                   ("a", "cp", 7, json.dumps(record)))
    page = TestClient(web.create_app(path, allowed_source_ids=("a",))).get(
        "/transactions/a/cp/7")
    assert page.status_code == 200
    assert 'class="soc-start">25%' in page.text
    assert 'class="soc-end">68.0%' in page.text
    assert "Última muestra" in page.text
    assert 'id="transaction-power"' not in page.text
    assert 'class="overview"' in page.text
    assert '<h2 id="timeline-heading">' not in page.text

    # Projection SoC takes precedence over an independently timed sample.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE current_transactions SET payload=?",
                   (json.dumps({**record, "soc_end": 75}),))
    projected = TestClient(web.create_app(path)).get("/transactions/a/cp/7")
    assert 'class="soc-end">75%' in projected.text

    # Unattested SoC must never become a displayed endpoint.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE current_transactions SET payload=?",
                   (json.dumps(record),))
        db.execute("UPDATE energy_samples SET payload=json_remove(payload, '$.soc_provenance')")
    unverified = TestClient(web.create_app(path)).get("/transactions/a/cp/7")
    assert 'class="soc-end">—' in unverified.text
