"""Shared dashboard test fixtures."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from ocpp_reports import web

@pytest.fixture
def dashboard_source():
    return (Path(__file__).resolve().parents[1] / "src/ocpp_reports/templates/dashboard.html.j2").read_text()

@pytest.fixture
def empty_dashboard_html(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    response = TestClient(web.create_app(tmp_path / "unused")).get("/")
    assert response.status_code == 200
    return response.text

@pytest.fixture
def dashboard_snapshot_factory():
    """Independent, minimally complete dashboard snapshots for behavioral tests."""
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone

    def make(*, source_age_seconds=20, status_age_hours=51,
             connected=True, status="Available", status_timestamp=True,
             last_activity_age_seconds=None):
        now = datetime.now(timezone.utc)
        source = {
            "source_id": "site-a",
            "cursor": 100,
            "last_ingested_at": (
                now - timedelta(seconds=source_age_seconds)
            ).isoformat() if source_age_seconds is not None else None,
        }
        connector = {
            "source_id": "site-a",
            "charger_id": "charger-a",
            "connector_id": 1,
            "location": "Garage",
            "status": status,
            "connected": connected,
            "status_observed_at": (
                now - timedelta(hours=status_age_hours)
            ).isoformat() if status_timestamp else None,
            "observed_at": now.isoformat(),
            "power_w": None,
            "energy_wh": None,
        }
        if last_activity_age_seconds is not None:
            connector["last_activity_at"] = (
                now - timedelta(seconds=last_activity_age_seconds)
            ).isoformat()
        return deepcopy({
            "sources": [source], "connectors": [connector],
            "transactions": [], "events": [],
        })
    return make


@pytest.fixture
def dashboard_client_factory(monkeypatch, tmp_path):
    """Render snapshots through the real HTTP endpoints without mutating fixtures."""
    from copy import deepcopy

    def make(snapshot_data, **app_kwargs):
        monkeypatch.setattr(
            web, "snapshot", lambda *args, **kwargs: deepcopy(snapshot_data)
        )
        return TestClient(web.create_app(tmp_path / "warehouse", **app_kwargs))
    return make


@pytest.fixture
def integrated_dashboard_warehouse(tmp_path):
    """Real SQLite schema, two tenants and event history; no patched queries.

    Use this for HTTP integration tests instead of partial snapshot mocks.
    All data is synthetic, deterministic and isolated from production.
    """
    import json
    import sqlite3
    from ocpp_reports.warehouse.schema import _schema
    from ocpp_reports.warehouse.collector_schema import ensure_collector_schema

    path = tmp_path / "integrated-dashboard.sqlite"
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)
        for source in ("tenant-a", "tenant-b"):
            db.execute("INSERT INTO sources(source_id,cursor) VALUES (?,0)", (source,))
            for n, action in ((1, "BootNotification"), (2, "Heartbeat"),
                              (3, "StatusNotification")):
                payload = {
                    "id": n, "kind": "ocpp", "charger_id": "CP-" + source,
                    "action": action, "at": "2026-11-01T06:30:00Z",
                }
                if action == "StatusNotification":
                    payload.update(connector_id=1, status="Available")
                db.execute(
                    "INSERT INTO events(source_id,event_id,payload) VALUES (?,?,?)",
                    (source, n, json.dumps(payload)),
                )
            for n in range(1, 46):
                record = {
                    "id": n, "charger_id": "CP-" + source, "state": "closed",
                    "active": False, "started_at": "2026-11-01T05:30:00Z",
                    "stopped_at": "2026-11-01T06:30:00Z",
                    "energy_wh": 0 if n % 10 == 0 else 1000,
                }
                db.execute(
                    "INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                    (source, "CP-" + source, n, 0, 0, "fixture",
                     json.dumps(record), "2026-11-01T06:30:00Z"),
                )
    return path
