from fastapi import FastAPI
from fastapi.testclient import TestClient
from ocpp_reports.dashboard_login import attach_login, provision

def test_login_http(tmp_path):
    path=str(tmp_path/"auth.sqlite")
    provision(path,"tester","long-password-for-testing","operator","tenant-a")
    app=FastAPI()
    @app.get("/")
    def index(): return {"ok":True}
    @app.get("/api/event-group")
    def events(): return {"ok":True}
    attach_login(app,path=path,tenant="tenant-a")
    with TestClient(app,base_url="https://reports.example.test") as client:
        assert client.get("/", follow_redirects=False).status_code==303
        assert client.get("/login").status_code==200
        response=client.post("/login",data={"username":"tester","password":"long-password-for-testing"},headers={"origin":"https://reports.example.test"},follow_redirects=False)
        assert response.status_code==303
        assert client.get("/").status_code==200
        assert client.get("/api/event-group").status_code==403


def test_account_directory_migration_and_authenticated_admin_only(tmp_path):
    """Legacy schema upgrades without losing hashes; only own tenant is listed."""
    import sqlite3
    from datetime import datetime
    from ocpp_reports.dashboard_login import initialize, dashboard_account_directory
    from ocpp_reports.web import create_app
    from ocpp_reports.warehouse.schema import _schema
    from ocpp_reports.warehouse.collector_schema import ensure_collector_schema

    auth = tmp_path / "legacy-auth.sqlite"
    with sqlite3.connect(auth) as connection:
        connection.execute("""CREATE TABLE dashboard_users(
            user_id TEXT PRIMARY KEY,password_hash TEXT NOT NULL,role TEXT NOT NULL,
            tenant_id TEXT NOT NULL,active INTEGER DEFAULT 1)""")
    initialize(auth)
    initialize(auth)
    with sqlite3.connect(auth) as connection:
        assert "last_login_at" in {
            col[1] for col in connection.execute("PRAGMA table_info(dashboard_users)")}
    provision(auth, "admin-a", "very-secure-admin-password", "admin", "tenant-a")
    provision(auth, "operator-a", "very-secure-user-password", "operator", "tenant-a")
    provision(auth, "private-b", "very-secure-other-password", "admin", "tenant-b")
    before = dashboard_account_directory(auth, tenant="tenant-a")
    assert [row["user_id"] for row in before] == ["admin-a", "operator-a"]
    assert all(row["last_login_at"] is None for row in before)

    warehouse = tmp_path / "warehouse.sqlite"
    with sqlite3.connect(warehouse) as connection:
        _schema(connection)
        ensure_collector_schema(connection)
    app = create_app(warehouse, allowed_source_ids=("a",))
    attach_login(app, path=auth, tenant="tenant-a")
    with TestClient(app, base_url="https://reports.example.test") as client:
        assert client.get("/").status_code == 200  # redirected to login
        assert "Usuarios con acceso" not in client.get("/login").text
        response = client.post(
            "/login", data={"username": "operator-a", "password": "very-secure-user-password"},
            headers={"origin": "https://reports.example.test"}, follow_redirects=False)
        assert response.status_code == 303
        assert 'id="dashboard-users"' not in client.get("/").text
        client.post("/logout", headers={"origin": "https://reports.example.test"})
        response = client.post(
            "/login", data={"username": "admin-a", "password": "very-secure-admin-password"},
            headers={"origin": "https://reports.example.test"}, follow_redirects=False)
        assert response.status_code == 303
        html = client.get("/").text
        assert 'id="dashboard-users"' in html
        assert "admin-a" in html and "operator-a" in html
        assert "private-b" not in html
        assert "very-secure-admin-password" not in html
        assert "last_login_at" not in html
        users = dashboard_account_directory(auth, tenant="tenant-a")
        assert datetime.fromisoformat(users[0]["last_login_at"]).tzinfo is not None
        assert users[1]["last_login_at"] is not None
        assert "Sin registro" not in html  # both tenant-a users signed in
    assert 'id="dashboard-users"' not in TestClient(
        create_app(warehouse, allowed_source_ids=("a",))).get("/").text

    provision(auth, "central-admin", "very-secure-central-password",
              "admin", "administration")
    central_app = create_app(warehouse)
    attach_login(central_app, path=auth, tenant="administration")
    with TestClient(central_app, base_url="https://reports.example.test") as client:
        response = client.post(
            "/login",
            data={"username": "central-admin", "password": "very-secure-central-password"},
            headers={"origin": "https://reports.example.test"},
            follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/").text
        assert 'id="dashboard-users"' in page
        assert "private-b" in page and "tenant-b" in page
        assert "operator-a" in page and "tenant-a" in page
        assert "central-admin" in page and "administration" in page
        assert "very-secure-central-password" not in page
