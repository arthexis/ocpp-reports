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


def _prepared_warehouse(database, cursor=27):
    from ocpp_reports.warehouse.schema import _schema
    with sqlite3.connect(database) as conn:
        _schema(conn)
        conn.execute("INSERT INTO sources(source_id,cursor) VALUES ('source-a',?)", (cursor,))


def test_backfill_preview_and_apply_never_modify_source_cursor(tmp_path):
    database = tmp_path / "warehouse.db"
    page = history(config(2), config(5, "400"))
    assert import_history_page(database, page) == 2
    assert not database.exists()
    with pytest.raises(ValueError, match="existing Reports warehouse"):
        import_history_page(database, page, apply=True)
    _prepared_warehouse(database)
    assert import_history_page(database, page, apply=True) == 2
    assert import_history_page(database, page, apply=True) == 2
    snapshots, more = list_snapshots(database, "source-a", "cp-1")
    assert [s["source_event_id"] for s in snapshots] == [5, 2]
    assert more is False
    assert one_snapshot(database, "source-a", "cp-1", 2)["configuration"][0]["value"] == "300"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM configuration_snapshots").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM replication_changes").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert db.execute("SELECT cursor FROM sources WHERE source_id='source-a'").fetchone()[0] == 27


def test_conflict_and_unredacted_secrets_are_rejected(tmp_path):
    db = tmp_path / "warehouse.db"
    _prepared_warehouse(db)
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


def test_normal_csms_ingest_carries_snapshot_with_event_identity(tmp_path):
    from ocpp_reports.warehouse.ingestion import ingest_page
    db = tmp_path / "warehouse.db"
    page = {"schema": "ocpp-csms/export/v1",
            "data": {"source_id": "source-a",
                     "cursor": {"after": 0, "next": 2, "more": False},
                     "events": [
                         {"id": 1, "at": "2026-11-20T18:40:00+00:00", "charger_id": "cp-1",
                          "action": "GetConfiguration", "kind": "ocpp", "direction": "out"},
                         {"id": 2, "at": "2026-11-20T18:42:00+00:00", "charger_id": "cp-1",
                          "action": "GetConfiguration", "kind": "ocpp", "direction": "in"}],
                     "energy": [], "transactions": [], "status": {"chargers": []},
                     "configuration_snapshots": [config()]}}
    assert ingest_page(db, page) == 2
    assert ingest_page(db, page) == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT cursor FROM sources WHERE source_id='source-a'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM configuration_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM replication_changes WHERE kind='config_snapshot'").fetchone()[0] == 1
    page["data"]["configuration_snapshots"][0]["source_event_id"] = 1
    with pytest.raises(ValueError, match="source event evidence"):
        ingest_page(db, page)


def test_configuration_page_selects_historical_versions_and_is_source_scoped(tmp_path):
    from ocpp_reports.web import create_app
    database = tmp_path / "warehouse.db"
    _prepared_warehouse(database)
    import_history_page(database, history(config(2), config(5, "400")), apply=True)
    browser = TestClient(create_app(database, allowed_source_ids=("source-a",), timezones=("UTC",)))
    newest = browser.get("/chargers/source-a/cp-1/configuration")
    assert newest.status_code == 200
    assert "400" in newest.text and "GetConfiguration" in newest.text
    assert newest.headers["Cache-Control"] == "no-store"
    older = browser.get("/chargers/source-a/cp-1/configuration?event_id=2")
    assert older.status_code == 200 and "300" in older.text
    assert browser.get("/chargers/source-b/cp-1/configuration").status_code == 404
    assert browser.get("/chargers/source-a/other/configuration").status_code == 404
    assert browser.get("/chargers/source-a/cp-1/configuration?event_id=999").status_code == 404


def test_configuration_route_is_admin_only_by_explicit_auth_policy():
    from ocpp_reports.dashboard_login import route_permission
    assert route_permission("/chargers/source-a/cp-1/configuration", "GET") == Permission.CONFIGURATION_VIEW
    assert route_permission("/chargers/source-a/cp-1/configuration", "POST") is None
    assert route_permission("/chargers/source-a/cp-1/configuration/extra", "GET") is None
