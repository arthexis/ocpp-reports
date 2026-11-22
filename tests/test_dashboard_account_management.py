"""Web account management: tenant isolation, role gates, CSRF and revocation."""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from ocpp_reports.dashboard_login import provision
from ocpp_reports.web import create_app
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema

ORIGIN = "https://reports.example.test"
ADMIN_PASS = "Test-admin-password-2026"
OP_PASS = "Test-operator-password-2026"
NEW_PASS = "Updated-operator-pass-2026"


def setup(tmp_path, monkeypatch):
    auth = tmp_path / "accounts.sqlite"
    provision(auth, "admin-main", ADMIN_PASS, "admin", "administration")
    provision(auth, "operator-a", OP_PASS, "operator", "administration")
    provision(auth, "hidden-other", "Other-tenant-password-2026", "admin", "porsche")
    warehouse = tmp_path / "warehouse.sqlite"
    with sqlite3.connect(warehouse) as db:
        _schema(db)
        ensure_collector_schema(db)
    monkeypatch.setenv("OCPP_REPORTS_AUTH_DATABASE", str(auth))
    monkeypatch.setenv("OCPP_REPORTS_AUTH_TENANT", "administration")
    app = create_app(warehouse)
    return auth, app


def login(client, username, password):
    result = client.post("/login", data={"username": username, "password": password},
                         headers={"origin": ORIGIN}, follow_redirects=False)
    assert result.status_code == 303, result.text


def token(client):
    page = client.get("/accounts")
    assert page.status_code == 200, page.text
    m = re.search(r'name="csrf_token" value="([a-f0-9]{64})"', page.text)
    assert m is not None
    return m.group(1)


def post(client, path, csrf, **fields):
    return client.post(path, data={"csrf_token": csrf, **fields},
                       headers={"origin": ORIGIN}, follow_redirects=False)


def test_only_admin_sees_management_and_only_own_tenant(tmp_path, monkeypatch):
    auth, app = setup(tmp_path, monkeypatch)
    with TestClient(app, base_url=ORIGIN) as anonymous:
        assert anonymous.get("/accounts").status_code == 403
        assert anonymous.post("/accounts/create").status_code == 403
    with TestClient(app, base_url=ORIGIN) as op:
        login(op, "operator-a", OP_PASS)
        assert op.get("/accounts").status_code == 403
        assert op.post("/accounts/delete", data={}, headers={"origin": ORIGIN}).status_code == 403
        assert "Administrar cuentas" not in op.get("/").text
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin, "admin-main", ADMIN_PASS)
        assert 'id="dashboard-users"' in admin.get("/").text
        assert 'href="/accounts"' in admin.get("/").text
        page = admin.get("/accounts")
        assert "admin-main" in page.text and "operator-a" in page.text
        assert "hidden-other" not in page.text
        assert "Other-tenant-password" not in page.text
        assert "password_hash" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "grid-template-columns:repeat(2,minmax(0,1fr))" in page.text
        assert "@media(max-width:760px)" in page.text
        csrf = token(admin)
        for action in ("update", "delete"):
            data = {"username": "hidden-other", "confirm": "ELIMINAR",
                    "role": "operator", "active": "0"}
            result = post(admin, "/accounts/" + action, csrf, **data)
            assert result.status_code == 404
        with sqlite3.connect(auth) as db:
            assert db.execute(
                "SELECT role,active FROM dashboard_users WHERE user_id='hidden-other'"
            ).fetchone() == ("admin", 1)
    # Unconfigured legacy auth does not grant account management from Basic Auth.
    monkeypatch.delenv("OCPP_REPORTS_AUTH_DATABASE", raising=False)
    monkeypatch.delenv("OCPP_REPORTS_AUTH_TENANT", raising=False)
    assert TestClient(create_app(tmp_path / "warehouse.sqlite")).get("/accounts").status_code == 403


def test_create_login_update_disable_delete_and_audit(tmp_path, monkeypatch):
    auth, app = setup(tmp_path, monkeypatch)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin, "admin-main", ADMIN_PASS)
        csrf = token(admin)
        creation = post(admin, "/accounts/create", csrf, username="tecnologia",
                        role="operator", password="Technology-Password-2026",
                        confirm_password="Technology-Password-2026")
        assert creation.status_code == 303
        assert creation.headers["location"] == "/accounts?notice=created"
        page = admin.get("/accounts?notice=created").text
        assert "tecnologia" in page and "Cuenta creada" in page
        assert "Technology-Password-2026" not in page
        with TestClient(app, base_url=ORIGIN) as tech:
            login(tech, "tecnologia", "Technology-Password-2026")
            assert tech.get("/").status_code == 200
            assert tech.get("/accounts").status_code == 403
            update = post(admin, "/accounts/update", csrf, username="tecnologia",
                          role="admin", active="1", password=NEW_PASS,
                          confirm_password=NEW_PASS)
            assert update.status_code == 303
            # Any role/password/account change revokes active sessions immediately.
            assert tech.get("/accounts").status_code == 403
            assert tech.get("/", follow_redirects=False).status_code == 303
            login(tech, "tecnologia", NEW_PASS)
            assert tech.get("/accounts").status_code == 200
            disable = post(admin, "/accounts/update", csrf, username="tecnologia",
                           role="operator", active="0", password="", confirm_password="")
            assert disable.status_code == 303
            assert tech.get("/accounts").status_code == 403
            assert tech.get("/", follow_redirects=False).status_code == 303
        deletion = post(admin, "/accounts/delete", csrf, username="tecnologia",
                        confirm="ELIMINAR")
        assert deletion.status_code == 303
        assert "tecnologia" not in admin.get("/accounts").text
        with sqlite3.connect(auth) as db:
            assert db.execute("SELECT 1 FROM dashboard_users WHERE user_id='tecnologia'").fetchone() is None
            assert db.execute("SELECT COUNT(*) FROM dashboard_sessions WHERE user_id='tecnologia'").fetchone()[0] == 0
            audit = db.execute("SELECT actor_id,tenant_id,action,target_id FROM dashboard_account_audit ORDER BY audit_id").fetchall()
            assert [a[2] for a in audit] == ["create", "update", "update", "delete"]
            assert all(a[0] == "admin-main" and a[1] == "administration" and a[3] == "tecnologia" for a in audit)


def test_reject_csrf_origin_malformed_duplicates_passwords_and_unsafe_roles(tmp_path, monkeypatch):
    auth, app = setup(tmp_path, monkeypatch)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin, "admin-main", ADMIN_PASS)
        csrf = token(admin)
        fields = {"username": "new-user", "role": "admin",
                  "password": "New-user-password-2026", "confirm_password": "New-user-password-2026"}
        assert admin.post("/accounts/create", data=fields, headers={"origin": ORIGIN}).status_code == 403
        assert post(admin, "/accounts/create", "wrong", **fields).status_code == 403
        assert admin.post("/accounts/create", data={"csrf_token": csrf, **fields},
                          headers={"origin": "https://evil.example.test"}).status_code == 403
        dupes = [("csrf_token", csrf), ("username", "x"), ("username", "y"),
                 ("role", "admin"), ("password", "New-user-password-2026"),
                 ("confirm_password", "New-user-password-2026")]
        assert admin.post("/accounts/create", content=urlencode(dupes),
                          headers={"origin": ORIGIN,
                                   "content-type": "application/x-www-form-urlencoded"}).status_code == 422
        assert post(admin, "/accounts/create", csrf, **{**fields, "role": "user"}).status_code == 422
        assert post(admin, "/accounts/create", csrf, **{**fields, "username": "../attack"}).status_code == 422
        assert post(admin, "/accounts/create", csrf, **{**fields, "password": "short", "confirm_password": "short"}).status_code == 422
        assert post(admin, "/accounts/create", csrf, **{**fields, "confirm_password": "another"}).status_code == 422
        assert post(admin, "/accounts/delete", csrf, username="operator-a").status_code == 422
        assert post(admin, "/accounts/update", csrf, username="operator-a", role="admin", active="2").status_code == 422
        assert post(admin, "/accounts/create", csrf, **fields).status_code == 303
        assert post(admin, "/accounts/create", csrf, **fields).status_code == 409
        with sqlite3.connect(auth) as db:
            assert db.execute("SELECT COUNT(*) FROM dashboard_users WHERE user_id='new-user'").fetchone()[0] == 1


def test_self_lockout_protected_and_last_admin_retained(tmp_path, monkeypatch):
    auth, app = setup(tmp_path, monkeypatch)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin, "admin-main", ADMIN_PASS)
        csrf = token(admin)
        assert post(admin, "/accounts/delete", csrf, username="admin-main", confirm="ELIMINAR").status_code == 403
        assert post(admin, "/accounts/update", csrf, username="admin-main",
                    role="operator", active="0", password="", confirm_password="").status_code == 403
        assert "No puedes" in admin.get("/accounts").text or "admin-main" in admin.get("/accounts").text
        with sqlite3.connect(auth) as db:
            assert db.execute(
                "SELECT role,active FROM dashboard_users WHERE user_id='admin-main'"
            ).fetchone() == ("admin", 1)
        assert admin.get("/accounts").status_code == 200
