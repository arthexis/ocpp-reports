"""Read-only transaction detail: exact identity, customer scope, live/final semantics."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ocpp_reports import web
from ocpp_reports.warehouse.transaction_detail import transaction_detail


def _warehouse(tmp_path):
    path = tmp_path / "reports.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE current_transactions (source_id TEXT, charger_id TEXT,"
                   " transaction_id INTEGER, payload TEXT)")
        db.execute("CREATE TABLE collector_latest (source_id TEXT, kind TEXT,"
                   " record_key TEXT, payload TEXT)")
        db.execute("CREATE TABLE collector_charger_locations (source_id TEXT,"
                   " charger_id TEXT, location TEXT)")
        db.execute("INSERT INTO collector_charger_locations VALUES ('a','cp','Station A')")
    return path


def _store(path, kind, source, value):
    with sqlite3.connect(path) as db:
        if kind == "local":
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?)",
                       (source, value["charger_id"], value["id"], json.dumps(value)))
        else:
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?)",
                       (source, "transaction", str(value["id"]), json.dumps(value)))


def test_live_transaction_detail_uses_exact_identity_and_live_age(tmp_path):
    path = _warehouse(tmp_path)
    now = datetime.now(timezone.utc)
    current = {"id": 7, "charger_id": "cp", "connector_id": 1,
               "state": "open", "active": True, "started_at": now.isoformat(),
               "energy_wh": None, "live_energy_wh": 34750,
               "live_energy_observed_at": now.isoformat(),
               "soc_start": 23, "soc_end": 78}
    _store(path, "local", "a", current)
    client = TestClient(web.create_app(path, allowed_source_ids=("a",)))
    page = client.get("/transactions/a/cp/7")
    assert page.status_code == 200
    assert "34.75 kWh" in page.text
    assert "Estimada" in page.text and "medición reciente" in page.text
    assert "78%" in page.text and "23%" in page.text
    assert 'http-equiv="refresh" content="15"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert transaction_detail(path, source_id="a", charger_id="cp",
                              transaction_id=7)["location"] == "Station A"
    # Other customer source and other CP must not share transaction IDs.
    assert client.get("/transactions/b/cp/7").status_code == 404
    assert client.get("/transactions/a/wrong/7").status_code == 404
    assert client.get("/transactions/a/cp/8").status_code == 404
    assert client.get("/transactions/a/cp/7?tz=Europe/Paris").status_code == 422

    current["live_energy_observed_at"] = (now - timedelta(minutes=10)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE current_transactions SET payload=? WHERE source_id='a'",
                   (json.dumps(current),))
    stale = client.get("/transactions/a/cp/7")
    assert "medición antigua / no verificada" in stale.text
    assert "34.75 kWh" in stale.text


def test_forwarded_and_local_finalization_do_not_duplicate_or_guess_energy(tmp_path):
    path = _warehouse(tmp_path)
    record = {"id": 4, "charger_id": "cp", "connector_id": 2,
              "state": "stopped", "active": False,
              "started_at": "2026-10-30T12:00:00Z",
              "stopped_at": "2026-10-30T12:30:00Z",
              "energy_wh": 35200, "soc_start": 30, "soc_end": 80}
    _store(path, "remote", "a", record)
    _store(path, "remote", "b", {**record, "energy_wh": 9000})
    client = TestClient(web.create_app(path, allowed_source_ids=("a",)))
    page = client.get("/transactions/a/cp/4?tz=UTC")
    assert page.status_code == 200
    assert "35.20 kWh" in page.text
    assert "Final · medición al cierre" in page.text
    assert 'http-equiv="refresh"' not in page.text
    assert client.get("/transactions/b/cp/4").status_code == 404

    # Local authoritative snapshot wins when both sources exist for same identity.
    _store(path, "local", "a", {**record, "energy_wh": 36000})
    assert "36.00 kWh" in client.get("/transactions/a/cp/4").text
    # A completed session without final evidence cannot show the provisional value as final.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE current_transactions SET payload=? WHERE source_id='a'",
                   (json.dumps({**record, "energy_wh": None,
                                "live_energy_wh": 33000}),))
    missing = client.get("/transactions/a/cp/4").text
    assert "Sin energía final verificada" in missing
    assert "33.00 kWh" not in missing
    assert 'http-equiv="refresh"' not in missing


def test_dashboard_transaction_ids_are_detail_links(monkeypatch, tmp_path):
    data = {
        "sources": [], "connectors": [], "events": [],
        "transactions": [{"source_id": "a", "charger_id": "cp",
                          "id": 9, "location": "Station A",
                          "state": "open", "active": True,
                          "energy_wh": None, "started_at": "2026-10-30T12:00:00Z"}],
    }
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: data)
    page = TestClient(web.create_app(tmp_path / "unused")).get("/?tx_view=all")
    assert page.status_code == 200
    assert '/transactions/a/cp/9?tz=UTC' in page.text
    assert "Ver detalles de transacción 9" in page.text
