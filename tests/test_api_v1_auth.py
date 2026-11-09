"""Authenticated API integration checks using the real dashboard session store."""
import hashlib
import sqlite3
import time

from fastapi.testclient import TestClient
from ocpp_reports import web
from ocpp_reports.dashboard_login import initialize, provision


def test_native_rotation_and_role_tenant_scope(monkeypatch, tmp_path):
    auth = tmp_path / "auth.sqlite"
    initialize(auth)
    provision(auth, "admin-a", "strong-password-a", "admin", "tenant-a")
    provision(auth, "operator-a", "strong-password-b", "operator", "tenant-a")
    provision(auth, "user-a", "strong-password-c", "user", "tenant-a")
    provision(auth, "admin-b", "strong-password-d", "admin", "tenant-b")
    monkeypatch.setenv("OCPP_REPORTS_AUTH_DATABASE", str(auth))
    monkeypatch.setenv("OCPP_REPORTS_AUTH_TENANT", "tenant-a")
    monkeypatch.setattr(web, "snapshot", lambda *a, **kw: {
        "connectors": [], "transactions": [], "events": [], "sources": [],
        "transaction_pagination": None,
    })
    client = TestClient(web.create_app(tmp_path / "warehouse", allowed_source_ids=("source-a",)),
                        base_url="https://reports.example")
    assert client.get("/api/v1/connectors").status_code == 403
    assert client.post("/api/v1/session/rotate").status_code == 401
    initial = client.post("/login", data={"username": "operator-a",
                    "password": "strong-password-b"},
                    headers={"Origin": "https://reports.example"}, follow_redirects=False)
    assert initial.status_code == 303
    issued = client.post("/api/v1/session/rotate",
                         headers={"Origin": "https://reports.example"})
    assert issued.status_code == 200
    assert issued.json()["token_type"] == "bearer"
    client.cookies.clear()
    assert client.get("/api/v1/connectors",
        headers={"Authorization": "Bearer " + issued.json()["access_token"]}).status_code == 200
    for name, role, expected in (
        ("admin-a", "admin", 200), ("operator-a", "operator", 200),
        ("user-a", "user", 403), ("admin-b", "admin", 403),
    ):
        token = "test-" + name
        with sqlite3.connect(auth) as db:
            db.execute("INSERT INTO dashboard_sessions VALUES (?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), name, int(time.time()) + 3600))
        response = client.get("/api/v1/connectors",
                              headers={"Authorization": "Bearer " + token})
        assert response.status_code == expected
        if expected == 200:
            rotated = client.post("/api/v1/session/rotate",
                                  headers={"Authorization": "Bearer " + token})
            assert rotated.status_code == 200
            replacement = rotated.json()["access_token"]
            assert client.get("/api/v1/connectors",
                              headers={"Authorization": "Bearer " + token}).status_code == 403
            assert client.get("/api/v1/connectors",
                              headers={"Authorization": "Bearer " + replacement}).status_code == 200
            assert client.get("/api/v1/connectors", params={"source_id": "source-b"},
                              headers={"Authorization": "Bearer " + replacement}).status_code == 403
        else:
            assert client.post("/api/v1/session/rotate",
                               headers={"Authorization": "Bearer " + token}).status_code == (401 if name == "admin-b" else 403)
