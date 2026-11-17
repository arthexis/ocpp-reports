"""Exercise merged device routes through real session/tenant middleware."""
from fastapi.testclient import TestClient

from ocpp_reports.dashboard_login import provision, route_permission
from ocpp_reports.dashboard_auth import Permission
from ocpp_reports.web import create_app
from test_device_details import seed


def _authenticated(app, username):
    client = TestClient(app, base_url="https://reports.example.test")
    response = client.post(
        "/login",
        data={"username": username, "password": "test-password-long-enough"},
        headers={"origin": "https://reports.example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_exact_device_routes_reuse_existing_connector_permission():
    for path in ("/chargers/a/same", "/connectors/a/same/1",
                 "/connectors/a/same/0"):
        assert route_permission(path, "GET") is Permission.CONNECTORS_VIEW
        assert route_permission(path, "POST") is None
        assert route_permission(path, "DELETE") is None
    for path in ("/chargers", "/chargers/a", "/chargers/a/same/extra",
                 "/connectors/a/same", "/connectors/a/same/1/extra",
                 "/connectors/a//1", "/chargers//same",
                 "/connectors/a/same/1/", "/chargers/a/same/"):
        assert route_permission(path, "GET") is None
    assert route_permission("/api/event-group", "GET") is Permission.EVENTS_VIEW
    assert route_permission("/api/v1/event-group", "GET") is Permission.EVENTS_VIEW
    assert route_permission("/api/v1/charger-event-group", "GET") is Permission.CONNECTORS_VIEW
    for method in ("POST", "PUT", "DELETE"):
        assert route_permission("/api/v1/charger-event-group", method) is None
    for route in ("/api/v1/charger-event-group/", "/api/v1/charger-event-group/extra"):
        assert route_permission(route, "GET") is None


def test_auth_sessions_allow_device_pages_without_widening_event_access(tmp_path, monkeypatch):
    warehouse = tmp_path / "warehouse.sqlite"
    seed(warehouse)
    database = str(tmp_path / "auth.sqlite")
    for name, role in (("administrator", "admin"), ("operator", "operator"),
                       ("customer", "user")):
        provision(database, name, "test-password-long-enough", role, "tenant-a")
    monkeypatch.setenv("OCPP_REPORTS_AUTH_DATABASE", database)
    monkeypatch.setenv("OCPP_REPORTS_AUTH_TENANT", "tenant-a")
    app = create_app(warehouse, allowed_source_ids=("a",))

    with TestClient(app, base_url="https://reports.example.test") as guest:
        assert guest.get("/chargers/a/same").status_code == 403
        assert guest.get("/connectors/a/same/1").status_code == 403
        assert guest.get("/api/v1/charger-event-group", params={
            "source_id": "a", "charger_id": "same", "first_event_id": 1,
        }).status_code == 403

    with _authenticated(app, "operator") as operator:
        charger = operator.get("/chargers/a/same")
        connector = operator.get("/connectors/a/same/1")
        assert charger.status_code == connector.status_code == 200
        assert "Cargador same" in charger.text
        assert "Conector 1" in connector.text
        assert "Eventos registrados de same" in charger.text
        assert 'id="events"' in charger.text
        assert charger.text.index('id="transactions"') < charger.text.index('id="events"')
        assert "never-expose" not in charger.text
        assert 'id="events"' not in connector.text
        assert "Cursor del origen" not in charger.text
        scoped = operator.get("/api/v1/charger-event-group", params={
            "source_id": "a", "charger_id": "same", "first_event_id": 1,
        })
        assert scoped.status_code == 200
        assert scoped.headers["cache-control"] == "no-store"
        assert [e["charger_id"] for e in scoped.json()["events"]] == ["same"]
        assert set(scoped.json()["events"][0]) == {
            "source_id", "location", "id", "charger_id",
            "action", "at", "display_at"
        }
        assert "never-expose" not in scoped.text
        for params, expected in (
            ({"source_id": "b", "charger_id": "same", "first_event_id": 1}, 404),
            ({"source_id": "a", "charger_id": "other", "first_event_id": 1}, 404),
            ({"source_id": "a", "charger_id": "same", "first_event_id": 2}, 404),
            ({"source_id": "a", "charger_id": "same", "first_event_id": 1, "offset": -1}, 422),
            ({"source_id": "a", "first_event_id": 1}, 422),
            ({"charger_id": "same", "first_event_id": 1}, 422),
        ):
            assert operator.get("/api/v1/charger-event-group", params=params).status_code == expected
        assert operator.post("/api/v1/charger-event-group").status_code == 403
        assert operator.get("/api/event-group", params={
            "group_source": "a", "source_id": "a",
            "charger_id": "same", "first_event_id": 1,
        }).status_code == 403
        assert operator.get("/api/v1/event-group", params={
            "group_source": "a", "source_id": "a",
            "charger_id": "same", "first_event_id": 1,
        }).status_code == 403
        assert operator.get("/chargers/b/same").status_code == 404
        assert operator.get("/connectors/b/same/1").status_code == 404
        assert operator.get("/chargers/a/same/extra").status_code == 403
        assert operator.post("/chargers/a/same").status_code == 403

    with _authenticated(app, "administrator") as admin:
        charger = admin.get("/chargers/a/same")
        assert charger.status_code == 200
        assert "Eventos registrados de same" in charger.text
        assert "Último Heartbeat" in charger.text
        assert "Sincronización" not in charger.text
        event = admin.get("/api/event-group", params={
            "group_source": "a", "source_id": "a",
            "charger_id": "same", "first_event_id": 1,
        })
        assert event.status_code == 200
        assert [r["charger_id"] for r in event.json()["events"]] == ["same"]
        assert "never-expose" not in event.text
        scoped = admin.get("/api/v1/charger-event-group", params={
            "source_id": "a", "charger_id": "same", "first_event_id": 1,
        })
        assert scoped.status_code == 200
        assert scoped.json()["events"] == event.json()["events"]
        assert admin.get("/connectors/a/same/1").status_code == 200
        assert admin.get("/chargers/b/same").status_code == 404

    with _authenticated(app, "customer") as customer:
        assert customer.get("/").status_code == 403
        assert customer.get("/chargers/a/same").status_code == 403
        assert customer.get("/connectors/a/same/1").status_code == 403
        assert customer.get("/api/v1/charger-event-group", params={
            "source_id": "a", "charger_id": "same", "first_event_id": 1,
        }).status_code == 403
