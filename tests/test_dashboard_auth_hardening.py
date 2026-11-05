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
