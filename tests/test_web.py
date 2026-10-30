import json
import sqlite3

"""Standalone dashboard: warehouse-only, read-only, filtering and freshness."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from ocpp_reports.ingest import ingest_page
from ocpp_reports.follow import sync_page
from ocpp_reports.web import create_app
from ocpp_reports.warehouse_queries import snapshot


def empty_page():
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "satellite-a", "exported_at": "2026-01-01T00:00:00Z",
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "cp-1", "connected": True,
            "connectors": [{"id": 1, "status": "Available", "transaction": None}]}]}
    }}


def test_dashboard_reads_warehouse_without_csms(tmp_path):
    database = tmp_path / "reports.sqlite"
    sync_page(database, empty_page())
    client = TestClient(create_app(database))
    page = client.get("/")
    assert page.status_code == 200
    assert "cp-1" in page.text
    assert "Antiguo" in page.text
    data = client.get("/api/snapshot").json()
    assert data["connectors"][0]["status"] == "Available"
    assert client.get("/api/snapshot?charger_id=other").json()["connectors"] == []
    assert snapshot(database)["sources"][0]["source_id"] == "satellite-a"


def test_missing_warehouse_is_unavailable(tmp_path):
    client = TestClient(create_app(tmp_path / "missing.sqlite"))
    assert client.get("/").status_code == 503
    assert not (tmp_path / "missing.sqlite").exists()


def test_limit_validation(tmp_path):
    database = tmp_path / "reports.sqlite"
    sync_page(database, empty_page())
    client = TestClient(create_app(database))
    assert client.get("/api/snapshot?limit=0").status_code == 422
    assert client.get("/api/snapshot?limit=501").status_code == 422

def test_d3_filters_sources_and_orders_by_activity(tmp_path):
    database = tmp_path / "reports.sqlite"
    sync_page(database, empty_page())
    second = empty_page()
    second["data"]["source_id"] = "satellite-b"
    second["data"]["status"]["chargers"][0]["id"] = "cp-2"
    sync_page(database, second)
    with sqlite3.connect(database) as db:
        for source, charger, tx_id, started, imported in [
            ("satellite-a", "cp-1", 1, "2026-01-05T10:00:00Z", "2026-10-19T12:00:00Z"),
            ("satellite-a", "cp-1", 2, "2026-02-05T10:00:00Z", "2026-03-01T12:00:00Z"),
            ("satellite-b", "cp-2", 1, "2026-04-05T10:00:00Z", "2026-04-06T12:00:00Z"),
        ]:
            payload = json.dumps({"id": tx_id, "charger_id": charger, "started_at": started})
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       (source, charger, tx_id, 0, 0, str(tx_id), payload, imported))
    data = snapshot(database, source_id="satellite-a", charger_id="cp-1")
    assert [s["source_id"] for s in data["sources"]] == ["satellite-a"]
    assert [c["charger_id"] for c in data["connectors"]] == ["cp-1"]
    assert [t["id"] for t in data["transactions"]] == [2, 1]
    client = TestClient(create_app(database))
    page = client.get("/?source_id=satellite-a&charger_id=cp-1")
    assert page.status_code == 200
    assert 'value="satellite-a"' in page.text
    assert 'value="cp-1"' in page.text
    assert 'id="dashboard-filters"' in page.text
    assert "Limpiar" in page.text
    assert 'type="submit"' not in page.text
    assert "cp-2" not in page.text

def test_d4_accessibility_and_empty_state(tmp_path):
    database = tmp_path / "reports.sqlite"
    sync_page(database, empty_page())
    client = TestClient(create_app(database))
    page = client.get("/?charger_id=unknown")
    assert page.status_code == 200
    assert 'href="#content"' in page.text
    assert 'id="content"' in page.text
    assert 'role="search"' in page.text
    assert 'tabindex="0"' in page.text
    assert "Sin conectores registrados" in page.text
    assert "Sin transacciones" in page.text
    assert "Sin eventos" in page.text
    assert client.get("/api/snapshot?charger_id=unknown").json()["connectors"] == []
