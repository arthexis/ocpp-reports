"""Configuration history ingestion and collector replication use immutable event IDs."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ocpp_reports.warehouse.ingestion import ingest_page
from ocpp_reports.warehouse.config_history import (
    import_history_page, list_snapshots, one_snapshot, validate_snapshot,
)
from ocpp_reports.collector import create_collector
from ocpp_reports.dashboard_auth import Permission, Principal, Role, authorized


def config(event=2, value="300", key="HeartbeatInterval"):
    return {"source_event_id": event, "charger_id": "cp-1",
            "at": "2026-11-20T18:42:00+00:00", "coverage": "full",
            "configuration": [{"key": key, "readonly": False, "value": value}],
            "unknown": []}


def history(*snapshots, after=0, end=10):
    return {"schema": "ocpp-csms/config-snapshots/v1",
            "data": {"source_id": "source-a",
                     "cursor": {"after": after, "next": end, "more": False},
                     "configuration_snapshots": list(snapshots)}}


def test_backfill_preview_and_apply_never_modify_source_cursor(tmp_path):
    database = tmp_path / "warehouse.db"
    page = history(config(2), config(5, "400"))
    assert import_history_page(database, page) == 2
    assert not database.exists()
    assert import_history_page(database, page, apply=True) == 2
    assert import_history_page(database, page, apply=True) == 2
    snapshots, more = list_snapshots(database, "source-a", "cp-1")
    assert [s["source_event_id"] for s in snapshots] == [5, 2]
    assert more is False
    assert one_snapshot(database, "source-a", "cp-1", 2)["configuration"][0]["value"] == "300"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM configuration_snapshots").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM replication_changes").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_conflict_and_unredacted_secrets_are_rejected(tmp_path):
    db = tmp_path / "warehouse.db"
    assert import_history_page(db, history(config()), apply=True) == 1
    with pytest.raises(ValueError, match="Conflicting immutable"):
        import_history_page(db, history(config(value="other")), apply=True)
    with pytest.raises(ValueError, match="Unredacted sensitive"):
        validate_snapshot(config(key="AuthorizationKey", value="super-secret"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM configuration_snapshots").fetchone()[0] == 1


def test_collector_history_is_not_latest_projection_and_replays_idempotently(tmp_path):
    db = tmp_path / "collector.db"
    app = TestClient(create_collector(db, {"source-a": "token"}))
    change = {"schema": "ocpp-reports/replication/v1", "source_id": "source-a",
              "after": 0, "next": 1, "more": False,
              "changes": [{"sequence": 1, "kind": "config_snapshot", "record_key": "2",
                           "payload": config()}]}
    auth = {"Authorization": "Bearer token"}
    readiness = app.post("/api/replication/ready",
                         json={"source_id": "source-a", "capabilities": ["config_snapshot"]},
                         headers=auth)
    assert readiness.status_code == 200
    assert "config_snapshot" in readiness.json()["supported_kinds"]
    assert app.post("/api/replication", json=change, headers=auth).status_code == 200
    assert app.post("/api/replication", json=change, headers=auth).status_code == 200
    assert one_snapshot(db, "source-a", "cp-1", 2) == config()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collector_configuration_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM collector_latest").fetchone()[0] == 0


def test_admin_role_can_view_configuration_but_operator_cannot():
    from ocpp_reports.dashboard_auth import _ROLE_PERMISSIONS
    assert Permission.CONFIGURATION_VIEW in _ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.CONFIGURATION_VIEW not in _ROLE_PERMISSIONS[Role.OPERATOR]
