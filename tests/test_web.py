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
