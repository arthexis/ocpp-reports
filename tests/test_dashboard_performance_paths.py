"""Bounded history loading and fast event-group selection preserve semantics."""
import json
import sqlite3

from fastapi.testclient import TestClient

from ocpp_reports.warehouse.event_groups import event_groups_page
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.web import create_app


def _build(path):
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)
        db.execute("INSERT INTO sources(source_id,cursor) VALUES ('local',0)")
        for n in range(1, 251):
            payload = {
                "id": n, "charger_id": "a",
                "kind": "ocpp", "action": "Heartbeat",
                "at": "2026-10-31T12:00:00Z",
            }
            db.execute("INSERT INTO events VALUES (?,?,?)",
                       ("local", n, json.dumps(payload)))
        for n in range(1, 401):
            payload = {
                "id": n, "charger_id": "b",
                "kind": "ocpp", "action": "Heartbeat" if n > 300 else "BootNotification",
                "at": "2026-10-31T12:00:00Z",
            }
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("remote", "event", str(n), n, json.dumps(payload)))
        for n in range(1, 301):
            payload = {
                "id": n, "charger_id": "b", "state": "stopped",
                "started_at": "2026-01-01T00:00:00Z" if n < 250 else "2026-11-01T00:00:00Z",
                "energy_wh": n,
            }
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("remote", "transaction", f"b:{n}", 1000+n, json.dumps(payload)))


def test_fast_event_groups_match_window_on_full_first_page(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _build(path)
    fast = event_groups_page(path)
    # A date condition uses the existing SQL window path; all fixture
    # events match this inclusive bound.
    reference = event_groups_page(path, date_from="2026-01-01")
    assert fast == reference
    assert fast["has_next"]


def test_snapshot_api_does_not_materialize_full_remote_history(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _build(path)
    data = snapshot(path, limit=10)
    assert len(data["transactions"]) == 10
    assert len(data["events"]) == 10
    assert all(item["source_id"] == "remote" for item in data["transactions"])
    assert max(item["id"] for item in data["transactions"]) == 300
    assert data["events"][0]["id"] == 400


def test_dashboard_stage_timings_and_source_scope(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _build(path)
    client = TestClient(create_app(path, allowed_source_ids=("local",)))
    response = client.get("/")
    assert response.status_code == 200
    timings = response.headers["server-timing"]
    assert "snapshot;dur=" in timings
    assert "event-groups;dur=" in timings
    assert "template;dur=" in timings
    assert "remote" not in client.get("/api/snapshot").text
    assert client.get("/?source_id=remote").status_code == 403
