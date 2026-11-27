"""Configuration history ingestion and collector replication use immutable event IDs."""
import sqlite3
import csv
import io

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


def test_admin_and_operator_can_view_configuration_but_user_cannot():
    from ocpp_reports.dashboard_auth import _ROLE_PERMISSIONS
    assert Permission.CONFIGURATION_VIEW in _ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.CONFIGURATION_VIEW in _ROLE_PERMISSIONS[Role.OPERATOR]
    assert Permission.CONFIGURATION_VIEW not in _ROLE_PERMISSIONS[Role.USER]


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


def test_configuration_page_selects_historical_versions_and_is_source_scoped(tmp_path, monkeypatch):
    from ocpp_reports.web import create_app
    from ocpp_reports.dashboard_login import provision
    from test_device_details import seed

    database = tmp_path / "warehouse.db"
    seed(database)  # source a / charger same, with a real device detail page
    page = history(
        {**config(2), "charger_id": "same"},
        {**config(5, "400"), "charger_id": "same",
         "configuration": [{"key": "HeartbeatInterval", "value": "400", "readonly": False},
                           {"key": "Descripción", "value": "Estación México — 中文", "readonly": True}]},
    )
    page["data"]["source_id"] = "a"
    import_history_page(database, page, apply=True)

    configuration_url = "/chargers/a/same/configuration"
    device_url = "/chargers/a/same"
    # Without integrated login there is no application role, even if a
    # reverse proxy uses Basic Auth. Configuration must fail closed.
    legacy = TestClient(create_app(database, allowed_source_ids=("a",)))
    assert legacy.get(configuration_url).status_code == 403
    assert legacy.get(configuration_url + "?event_id=2&download=1").status_code == 403
    legacy_device = legacy.get(device_url)
    assert legacy_device.status_code == 200
    assert "/configuration?" not in legacy_device.text

    auth = tmp_path / "auth.sqlite"
    for name, role in (("administrator", "admin"),
                       ("operator", "operator"), ("basic", "user")):
        provision(auth, name, "test-password-long-enough", role, "tenant-a")
    monkeypatch.setenv("OCPP_REPORTS_AUTH_DATABASE", str(auth))
    monkeypatch.setenv("OCPP_REPORTS_AUTH_TENANT", "tenant-a")
    app = create_app(database, allowed_source_ids=("a",), timezones=("UTC",))

    def login(browser, username):
        response = browser.post(
            "/login", data={"username": username, "password": "test-password-long-enough"},
            headers={"origin": "https://reports.example.test"}, follow_redirects=False,
        )
        assert response.status_code == 303

    with TestClient(app, base_url="https://reports.example.test") as browser:
        assert browser.get(configuration_url).status_code == 403
        login(browser, "basic")
        assert browser.get(configuration_url).status_code == 403
        assert browser.get(configuration_url + "?event_id=2").status_code == 403
        assert browser.get(configuration_url + "?event_id=2&download=1").status_code == 403
        assert browser.get(device_url).status_code == 403
        browser.post("/logout", follow_redirects=False)

        for username in ("operator", "administrator"):
            login(browser, username)
            device = browser.get(device_url)
            assert device.status_code == 200
            assert "/chargers/a/same/configuration?" in device.text
            newest = browser.get(configuration_url)
            assert newest.status_code == 200
            assert "400" in newest.text and "GetConfiguration" in newest.text
            assert newest.headers["Cache-Control"] == "no-store"
            older = browser.get(configuration_url + "?event_id=2")
            assert older.status_code == 200 and "300" in older.text
            assert 'event_id=2&amp;download=1"><svg aria-hidden="true"' in older.text
            assert 'event_id=5&amp;download=1"><svg aria-hidden="true"' in newest.text
            for event_id, expected in ((2, "300"), (5, "400")):
                download = browser.get(configuration_url, params={"event_id": event_id, "download": 1})
                assert download.status_code == 200
                assert download.headers["content-type"].startswith("text/csv")
                assert download.headers["content-disposition"] == (
                    f'attachment; filename="configuration-a-same-{event_id}.csv"')
                assert download.headers["cache-control"] == "no-store"
                assert download.content.startswith(b"\xef\xbb\xbf")
                assert list(csv.reader(io.StringIO(download.content.decode("utf-8-sig")))) == [
                    ["Clave", "Valor", "Solo lectura"], ["HeartbeatInterval", expected, "No"]] + (
                    [["Descripción", "Estación México — 中文", "Sí"]] if event_id == 5 else [])
            for url in ("/chargers/b/same/configuration", "/chargers/a/other/configuration"):
                assert browser.get(url + "?event_id=2&download=1").status_code == 404
            assert browser.get(configuration_url + "?event_id=999&download=1").status_code == 404

            assert browser.get("/chargers/b/same/configuration").status_code == 404
            assert browser.get("/chargers/a/other/configuration").status_code == 404
            assert browser.get(configuration_url + "?event_id=999").status_code == 404
            assert "/configuration?" not in browser.get("/connectors/a/same/1").text
            if username == "operator":
                assert browser.get("/accounts").status_code == 403
                assert browser.get("/chargers/a/same/site").status_code == 403
            browser.post("/logout", follow_redirects=False)


def test_configuration_route_is_read_only_by_explicit_auth_policy():
    from ocpp_reports.dashboard_login import route_permission
    assert route_permission("/chargers/source-a/cp-1/configuration", "GET") == Permission.CONFIGURATION_VIEW
    assert route_permission("/chargers/source-a/cp-1/configuration", "POST") is None
    assert route_permission("/chargers/source-a/cp-1/configuration/extra", "GET") is None


@pytest.mark.parametrize("vendor_key", [
    "KEY1", "KEY2", "KEY3", "KEY24", "lPin", "enSoftKey",
    "EncryptKey", "lUser", "vpnUser", "SSIDPassword", "apSSIDToken", "simuRFIDPassword", "wPwd",
    "vpnPsk", "apPwd", "rfidtagfreecharging",
])
def test_unredacted_vendor_credential_is_rejected_before_persistence(vendor_key):
    with pytest.raises(ValueError, match="Unredacted sensitive"):
        validate_snapshot(config(key=vendor_key, value="device-credential"))
    accepted = validate_snapshot(config(key=vendor_key, value="[REDACTED]"))
    assert accepted["configuration"][0]["value"] == "[REDACTED]"


@pytest.mark.parametrize("ordinary_key", [
    "HeartbeatInterval", "GetConfigurationMaxKeys",
    "WebSocketPingInterval", "CertificateStoreMaxLength",
])
def test_operational_key_is_permitted(ordinary_key):
    assert validate_snapshot(config(key=ordinary_key, value="42"))["configuration"][0]["value"] == "42"


def test_configuration_csv_preserves_values_and_protects_spreadsheets():
    from ocpp_reports.warehouse.config_history import render_snapshot_csv
    snapshot = config()
    snapshot["configuration"] = [
        {"key": "Quoted", "value": 'á,"b"\r\nc', "readonly": True},
        {"key": "Missing", "value": None, "readonly": False},
        {"key": "Formula", "value": "=1+1", "readonly": False},
        {"key": "@Key", "value": "[REDACTED]", "readonly": True},
    ]
    assert list(csv.reader(io.StringIO(render_snapshot_csv(snapshot)))) == [
        ["Clave", "Valor", "Solo lectura"],
        ["Quoted", 'á,"b"\r\nc', "Sí"],
        ["Missing", "", "No"],
        ["Formula", "'=1+1", "No"],
        ["'@Key", "[REDACTED]", "Sí"],
    ]


@pytest.mark.parametrize("key", ["SSID", "apSSID", "simuRFID", "sSiD"])
def test_explicit_configuration_visibility_exceptions(key):
    item = validate_snapshot(config(key=key, value="synthetic-operational-value"))
    assert item["configuration"][0]["value"] == "synthetic-operational-value"
    assert validate_snapshot(config(key=key, value="[REDACTED]"))["configuration"][0]["value"] == "[REDACTED]"
