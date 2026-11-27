"""SQL-backed dashboard pagination preserves source scope, filters and full history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from ocpp_reports.web import create_app
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def _record(n, *, cp="cp-local", energy=1000, state="closed", start="2026-10-30T12:00:00Z",
            stop="2026-10-30T13:00:00Z"):
    return {"id": n, "charger_id": cp, "state": state, "active": False,
            "started_at": start, "stopped_at": stop, "energy_wh": energy}


def _seed(dbpath, *, local=61, remote=42):
    with sqlite3.connect(dbpath) as db:
        _schema(db)
        ensure_collector_schema(db)
        db.execute("INSERT INTO sources(source_id,cursor) VALUES (?,?)", ("local", 0))
        db.execute("INSERT INTO collector_cursors(source_id,sequence) VALUES (?,?)", ("remote", 1))
        db.execute("INSERT INTO collector_charger_locations VALUES (?,?,?)",
                   ("remote", "cp-remote", "West Calzada"))
        db.execute("INSERT INTO collector_charger_locations VALUES (?,?,?)",
                   ("local", "cp-local", "East"))
        for n in range(1, local + 1):
            payload = json.dumps(_record(n, energy=0 if n % 10 == 0 else 1000),
                                 sort_keys=True)
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ("local", "cp-local", n, 0, 0, "hash", payload, "2026-10-30T13:00:00Z"))
        for n in range(1, remote + 1):
            payload = json.dumps(_record(n, cp="cp-remote", energy=0 if n % 10 == 0 else 1000))
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("remote", "transaction", f"cp-remote:{n}", n, payload))


def _page(dbpath, **kw):
    return snapshot(dbpath, allowed_source_ids=("local", "remote"),
                    transaction_pagination={
                        "term": "", "status": "", "location": "", "date_from": "",
                        "date_to": "", "zone": "UTC", "view": "all", "page": 1,
                        **kw,
                    })


def test_all_history_is_counted_and_only_page_records_loaded(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=521, remote=42)
    first = _page(dbpath)
    assert first["transaction_pagination"]["counts"] == {
        "all": 563, "success": 507, "failed": 56, "active": 0
    }
    assert len(first["transactions"]) == 20
    assert first["transaction_pagination"]["pages"] == 29
    last = _page(dbpath, page=29)
    assert len(last["transactions"]) == 3
    assert last["transaction_pagination"]["total"] == 563
    assert _page(dbpath, page=50000)["transaction_pagination"]["page"] == 29
    # Existing public API still returns the regular bounded snapshot.
    assert len(snapshot(dbpath, limit=10)["transactions"]) == 10


def test_filter_and_count_before_offset_and_source_isolation(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=25, remote=42)
    remote = _page(dbpath, location="west", view="failed")
    assert remote["transaction_pagination"]["counts"] == {
        "all": 42, "success": 38, "failed": 4, "active": 0
    }
    assert [r["id"] for r in remote["transactions"]] == [40, 30, 20, 10]
    matching = _page(dbpath, term="EAST", view="success", page=2)
    assert matching["transaction_pagination"]["counts"]["all"] == 25
    assert matching["transaction_pagination"]["counts"]["success"] == 23
    assert len(matching["transactions"]) == 3
    east = snapshot(dbpath, allowed_source_ids=("local",),
                       transaction_pagination={
                           "term": "", "status": "", "location": "",
                           "date_from": "", "date_to": "", "zone": "UTC",
                           "view": "all", "page": 1,
                       })
    assert east["transaction_pagination"]["total"] == 25
    assert all(r["source_id"] == "local" for r in east["transactions"])
    client = TestClient(create_app(dbpath, allowed_source_ids=("remote",), timezones=("UTC",)))
    assert client.get("/?source_id=local").status_code == 403
    response = client.get("/?tx_view=all&tx_page=2")
    assert response.status_code == 200
    assert "Mostrando 11–20 de 42" in response.text
    assert response.text.count('aria-label="Ver detalles de transacción ') == 10
    assert "East" not in response.text
    third = client.get("/?tx_view=all&tx_page=3")
    assert "Mostrando 21–30 de 42" in third.text
    final = client.get("/?tx_view=all&tx_page=5")
    assert "Mostrando 41–42 de 42" in final.text
    assert final.text.count('aria-label="Ver detalles de transacción ') == 2
    assert 'aria-current="page">5</span>' in final.text


def test_date_intersection_and_unknown_energy_semantics(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=2, remote=0)
    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE current_transactions SET payload=? WHERE transaction_id=1",
                   (json.dumps(_record(1, start="2026-10-29T23:00:00-06:00",
                                       stop="2026-10-30T03:00:00-06:00", energy=None)),))
        db.execute("UPDATE current_transactions SET payload=? WHERE transaction_id=2",
                   (json.dumps(_record(2, start="2026-10-31T12:00:00Z",
                                       stop="2026-10-31T13:00:00Z", energy=0)),))
    result = _page(dbpath, zone="America/Monterrey",
                   date_from="2026-10-30", date_to="2026-10-30")
    assert result["transaction_pagination"]["counts"] == {
        "all": 1, "success": 0, "failed": 0, "active": 0
    }
    assert result["transactions"][0]["id"] == 1
    failed = _page(dbpath, zone="America/Monterrey",
                   date_from="2026-10-31", date_to="2026-10-31", view="failed")
    assert failed["transaction_pagination"]["total"] == 1
    assert failed["transactions"][0]["id"] == 2


def test_local_projection_wins_against_duplicate_collector_latest(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=1, remote=0)
    with sqlite3.connect(dbpath) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("local", "transaction", "cp-local:1", 2,
                    json.dumps(_record(1, energy=0))))
    result = _page(dbpath)
    assert result["transaction_pagination"]["total"] == 1
    assert result["transactions"][0]["energy_wh"] == 1000


def test_dashboard_10_rows_does_not_change_20_row_device_detail(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    _seed(db, local=27, remote=0)
    client = TestClient(create_app(db, allowed_source_ids=("local",), timezones=("UTC",)))
    home = client.get("/?tx_view=all")
    assert home.status_code == 200
    assert "Mostrando 1–10 de 27" in home.text
    assert home.text.count('aria-label="Ver detalles de transacción ') == 10
    device = client.get("/chargers/local/cp-local")
    assert device.status_code == 200
    assert "1–20 de 27" in device.text
    assert device.text.count('href="/transactions/local/cp-local/') == 20
    assert client.get("/chargers/local/cp-local?tx_page=2").status_code == 200


def test_rfid_summary_and_history_are_scoped_and_not_page_limited(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=25, remote=3)
    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE current_transactions SET payload=json_set(payload, '$.rfid', 'A/B+&')")
        db.execute("UPDATE collector_latest SET payload=json_set(payload, '$.rfid', 'A/B+&')")
    result = _page(dbpath, card_summary=True, page_size=10)
    assert result["transaction_pagination"]["cards"] == [
        {"rfid": "A/B+&", "charges": 26, "energy_kwh": 26.0,
         "last_access_at": "2026-10-30T12:00:00.000Z"}
    ]
    client = TestClient(create_app(dbpath, allowed_source_ids=("local",)))
    dashboard = client.get("/?tx_view=all")
    assert dashboard.status_code == 200
    assert "<th>ID</th><th>RFID</th>" in dashboard.text
    assert "Tarjetas RFID" in dashboard.text
    history = client.get("/rfid", params={"rfid": "A/B+&"})
    assert history.status_code == 200
    assert "25 sesiones" in history.text
    assert history.text.count("<circle ") == 25
    assert "<polyline " in history.text
    assert "23.00 kWh acumulados" in history.text
    assert "Último acceso" in dashboard.text
    assert "<th>Ubicación</th><th>Cargador</th>" in history.text
    assert "<th>Fin</th><th>Duración</th>" in history.text
    assert "<td>1:00:00</td>" in history.text
    assert "<td>East</td><td>cp-local</td>" in history.text
    assert "West Calzada" not in history.text
    assert "cp-remote" not in history.text
    assert client.get("/rfid", params={"rfid": "unknown"}).status_code == 404


def test_rfid_last_access_uses_session_start_and_cumulative_energy(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=4, remote=1)
    with sqlite3.connect(dbpath) as db:
        for n, start, stop, energy in [
            (1, "2026-10-30T10:00:00Z", "2026-10-30T11:00:00Z", 1500),
            (2, "2026-10-30T11:00:00Z", "2026-10-30T12:00:00Z", 2500),
            (3, "2026-10-30T13:00:00Z", "2026-10-30T14:00:00Z", 0),
            (4, "2026-10-30T09:30:00-06:00", None, None),
        ]:
            payload = _record(n, start=start, stop=stop, energy=energy)
            payload["rfid"] = "card"
            if n == 4:
                payload.update(state="open", active=True)
            db.execute("UPDATE current_transactions SET payload=? WHERE transaction_id=?",
                       (json.dumps(payload), n))
        db.execute("UPDATE collector_latest SET payload=json_set(payload, '$.rfid', 'card', '$.started_at', '2026-11-01T00:00:00Z')")
    result = snapshot(dbpath, allowed_source_ids=("local",), transaction_pagination={
        "term": "", "status": "", "location": "", "date_from": "", "date_to": "",
        "zone": "UTC", "view": "success", "page": 1, "card_summary": True,
    })
    assert result["transaction_pagination"]["cards"] == [
        {"rfid": "card", "charges": 2, "energy_kwh": 4.0,
         "last_access_at": "2026-10-30T15:30:00.000Z"}
    ]
    history = TestClient(create_app(dbpath, allowed_source_ids=("local",))).get("/rfid", params={"rfid": "card"})
    assert history.status_code == 200
    assert history.text.count("<circle ") == 3
    assert "1.50 kWh acumulados" in history.text
    assert "2.50 kWh de sesión · 4.00 kWh acumulados" in history.text
    assert "0.00 kWh de sesión · 4.00 kWh acumulados" in history.text
    assert "<polyline " in history.text


def test_rfid_history_missing_location_does_not_display_satellite(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=1, remote=0)
    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE current_transactions SET payload=json_set(payload, '$.rfid', 'card')")
        db.execute("DELETE FROM collector_charger_locations WHERE source_id='local'")
    history = TestClient(create_app(dbpath, allowed_source_ids=("local",))).get("/rfid", params={"rfid": "card"})
    assert history.status_code == 200
    assert "<td>—</td><td>cp-local</td>" in history.text


def test_rfid_pages_escape_authorization_tag(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=1, remote=0)
    tag = '<TEST&RFID>'
    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE current_transactions SET payload=json_set(payload, '$.rfid', ?)", (tag,))
    client = TestClient(create_app(dbpath, allowed_source_ids=("local",)))
    for response in (client.get("/?tx_view=all"), client.get("/rfid", params={"rfid": tag})):
        assert response.status_code == 200
        assert tag not in response.text
        assert "&lt;TEST&amp;RFID&gt;" in response.text
