import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ocpp_reports.dashboard_login import (
    attach_login, manage_user, provision, prune_expired_sessions, route_permission,
)
from ocpp_reports.dashboard_auth import Permission


def test_route_permissions_fail_closed():
    assert route_permission("/api/snapshot", "GET") is Permission.DASHBOARD_VIEW
    assert route_permission("/api/snapshot", "POST") is None
    assert route_permission("/api/unknown", "GET") is None


def test_login_lockout_and_revocation(tmp_path):
    path = str(tmp_path / "auth.sqlite")
    provision(path, "tester", "a-secure-test-password", "operator", "tenant-a")
    app = FastAPI()

    @app.get("/")
    def index():
        return {"ok": True}

    attach_login(app, path=path, tenant="tenant-a")
    with TestClient(app, base_url="https://reports.example.test") as client:
        headers = {"origin": "https://reports.example.test"}
        for _ in range(5):
            assert client.post("/login", data={"username": "tester", "password": "wrong"}, headers=headers).status_code == 401
        assert client.post("/login", data={"username": "tester", "password": "a-secure-test-password"}, headers=headers).status_code == 429
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM dashboard_login_attempts")
        assert client.post("/login", data={"username": "tester", "password": "a-secure-test-password"}, headers=headers, follow_redirects=False).status_code == 303
        assert client.get("/").status_code == 200
        manage_user(path, "tester", active=False)
        assert client.get("/").status_code == 403
        manage_user(path, "tester", active=True, role="user")
        assert client.post("/login", data={"username": "tester", "password": "a-secure-test-password"}, headers=headers, follow_redirects=False).status_code == 303
        assert client.get("/").status_code == 403


def test_prune_expired(tmp_path):
    path = str(tmp_path / "auth.sqlite")
    provision(path, "tester", "a-secure-test-password", "admin", "tenant-a")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dashboard_sessions VALUES(?,?,?)", ("expired", "tester", 1))
    assert prune_expired_sessions(path, now=2) == 1

def test_same_url_enforces_roles_and_tenant(tmp_path):
    path = str(tmp_path / "auth.sqlite")
    provision(path, "operator-a", "a-secure-test-password", "operator", "tenant-a")
    provision(path, "admin-b", "a-secure-test-password", "admin", "tenant-b")
    app = FastAPI()

    @app.get("/")
    def dashboard():
        return {"ok": True}

    @app.get("/api/event-group")
    def events():
        return {"ok": True}

    attach_login(app, path=path, tenant="tenant-a")
    headers = {"origin": "https://reports.example.test"}
    with TestClient(app, base_url="https://reports.example.test") as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.post("/login", data={"username": "admin-b", "password": "a-secure-test-password"}, headers=headers).status_code == 401
        assert client.post("/login", data={"username": "operator-a", "password": "a-secure-test-password"}, headers=headers, follow_redirects=False).status_code == 303
        assert client.get("/").status_code == 200
        assert client.get("/api/event-group").status_code == 403
        assert client.get("/api/unknown").status_code == 403
        assert client.post("/api/snapshot").status_code == 403


def test_login_route_methods_and_transaction_shape(tmp_path):
    path = str(tmp_path / "auth.sqlite")
    provision(path, "operator", "a-secure-test-password", "operator", "tenant-a")
    app = FastAPI()

    @app.get("/transactions/{source}/{charger}/{transaction}")
    def transaction(source: str, charger: str, transaction: str):
        return {"ok": True}

    attach_login(app, path=path, tenant="tenant-a")
    with TestClient(app, base_url="https://reports.example.test") as client:
        headers = {"origin": "https://reports.example.test"}
        assert client.get("/login").status_code == 200
        assert client.get("/logout").status_code == 403
        assert client.put("/login").status_code == 403
        assert client.post("/login", data={"username": "operator", "password": "a-secure-test-password"}, headers=headers, follow_redirects=False).status_code == 303
        assert client.get("/transactions/source/charger/123").status_code == 200
        assert client.get("/transactions/source/charger/123/extra").status_code == 403
        assert client.get("/transactions/").status_code == 403
        assert client.post("/logout", headers=headers, follow_redirects=False).status_code == 303
        assert client.get("/transactions/source/charger/123").status_code == 403


def test_invalid_database_role_fails_closed(tmp_path):
    path = str(tmp_path / "auth.sqlite")
    provision(path, "operator", "a-secure-test-password", "operator", "tenant-a")
    app = FastAPI()

    @app.get("/")
    def dashboard():
        return {"ok": True}

    attach_login(app, path=path, tenant="tenant-a")
    headers = {"origin": "https://reports.example.test"}
    with TestClient(app, base_url="https://reports.example.test") as client:
        assert client.post("/login", data={"username": "operator", "password": "a-secure-test-password"}, headers=headers, follow_redirects=False).status_code == 303
        with sqlite3.connect(path) as db:
            db.execute("UPDATE dashboard_users SET role='unexpected' WHERE user_id='operator'")
        assert client.get("/").status_code == 403
