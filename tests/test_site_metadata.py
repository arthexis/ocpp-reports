"""Customer/site metadata: shared fields, optional display and fail-closed writes."""
import hashlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from ocpp_reports.site_metadata import (
    DAYS, DEFAULT_HOURS, assign_site, clean, editable_sites, hours,
    hours_display, phone, save_site, site_for_charger,
)
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.web import create_app
from ocpp_reports.warehouse.queries import snapshot


def warehouse(tmp_path):
    path = tmp_path / "collector.sqlite"
    with sqlite3.connect(path) as db:
        ensure_collector_schema(db)
        for source, charger in (("a", "same"), ("a", "second"),
                                ("b", "same"), ("b", "second")):
            db.execute("""INSERT INTO collector_latest
                (source_id,kind,record_key,sequence,payload) VALUES (?,?,?,?,?)""",
                (source, "connector", f"{charger}:1", 1,
                 json.dumps({"charger_id": charger, "connector_id": 1,
                             "status": "Available"})))
    return path


def meta(name="", address="", branch="", contact="", contact_phone="", **extra):
    return {"customer_name": name, "location_name": extra.get("location_name", ""),
            "address": address, "branch_phone": branch,
            "contact_name": contact, "contact_phone": contact_phone,
            "timezone": "America/Monterrey"}


def test_shared_site_updates_all_chargers_and_not_other_sources(tmp_path):
    db = warehouse(tmp_path)
    fields = meta("Porsche", "Av. Constitución 101", "+52 81 1111 2222",
                  "Guardia", "+52 81 3333 4444", location_name="Porsche Centro")
    site = save_site(db, "a", "same", fields, allowed_source_ids=None)
    assign_site(db, "a", "second", site, allowed_source_ids=None)
    assert site_for_charger(db, "a", "same")["site_id"] == site
    assert site_for_charger(db, "a", "second")["site_id"] == site
    assert site_for_charger(db, "b", "same") is None
    fields["contact_phone"] = "8112345678"
    save_site(db, "a", "second", fields, allowed_source_ids=None)
    for charger in ("same", "second"):
        result = site_for_charger(db, "a", charger)
        assert result["contact_tel"] == "tel:8112345678"
        assert result["branch_tel"] == "tel:+528111112222"
        assert "query=Av.%20Constituci%C3%B3n%20101" in result["map_url"]


def test_identical_labels_are_not_auto_merged(tmp_path):
    db = warehouse(tmp_path)
    a = save_site(db, "a", "same", meta("Client 1", location_name="Centro"),
                  allowed_source_ids=None)
    b = save_site(db, "b", "same", meta("Client 2", location_name="Centro"),
                  allowed_source_ids=None)
    assert a != b
    assert site_for_charger(db, "a", "same")["customer_name"] == "Client 1"
    assert site_for_charger(db, "b", "same")["customer_name"] == "Client 2"


def test_optional_fields_and_default_hours(tmp_path):
    db = warehouse(tmp_path)
    save_site(db, "a", "same", {}, allowed_source_ids=("a",))
    record = site_for_charger(db, "a", "same")
    assert record["address"] is None and record["contact_name"] is None
    assert record["branch_tel"] is None and record["contact_tel"] is None
    assert record["map_url"] is None
    assert "Lunes: 09:00–18:00" in record["hours_display"]
    assert "Domingo: 09:00–14:00" in record["hours_display"]
    assert hours({"sun": None, "mon": ["10:00", "16:00"]})
    assert "Domingo: Cerrado" in hours_display(hours({"sun": None}))
    assert all(day in DEFAULT_HOURS for day in DAYS)
    for invalid in ("javascript:alert(1)", "123", "+52 (555) x100", "000\n1234567"):
        with pytest.raises(ValueError):
            phone(invalid)
    assert clean("  ") is None


def test_tenant_scope_rejects_shared_site_edits_and_assignments(tmp_path):
    db = warehouse(tmp_path)
    site = save_site(db, "a", "same", meta("Client 1"), allowed_source_ids=None)
    assign_site(db, "b", "same", site, allowed_source_ids=None)
    assert not editable_sites(db, ("a",))
    with pytest.raises(PermissionError):
        save_site(db, "a", "same", meta("Unauthorized"), allowed_source_ids=("a",))
    with pytest.raises(PermissionError):
        assign_site(db, "a", "second", site, allowed_source_ids=("a",))
    assert site_for_charger(db, "b", "same")["customer_name"] == "Client 1"


def _logged_client(db, tmp_path, monkeypatch, role="admin", sources=("a",)):
    auth = tmp_path / ("auth-" + role + ".sqlite")
    monkeypatch.setenv("OCPP_REPORTS_AUTH_DATABASE", str(auth))
    monkeypatch.setenv("OCPP_REPORTS_AUTH_TENANT", "administration")
    app = create_app(db, allowed_source_ids=sources)
    token = "test-token-" + role
    with sqlite3.connect(auth) as conn:
        conn.execute("""INSERT INTO dashboard_users
            (user_id,password_hash,role,tenant_id,active)
            VALUES (?,?,?,?,1)""", ("test", "not-used", role, "administration"))
        conn.execute("""INSERT INTO dashboard_sessions
            (token_hash,user_id,expires) VALUES (?,?,?)""",
            (hashlib.sha256(token.encode()).hexdigest(), "test", int(time.time()) + 3600))
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("reports_session", token)
    return client


def test_admin_only_site_view_and_two_independent_phone_actions(tmp_path, monkeypatch):
    db = warehouse(tmp_path)
    admin = _logged_client(db, tmp_path, monkeypatch)
    url = "/chargers/a/same"
    data = meta("Porsche", "Av. Reforma 10", "+52 81 1111 2222",
                "Responsable", "+52 81 3333 4444", location_name="Porsche Centro")
    response = admin.post(url + "/site", data=data, headers={"origin": "https://testserver"},
                          follow_redirects=False)
    assert response.status_code == 303, response.text
    body = admin.get(url).text
    assert 'id="site-details"' in body
    assert "Llamar a sucursal" in body and "Llamar al contacto" in body
    assert "tel:+528111112222" in body and "tel:+528133334444" in body
    assert 'class="site-grid"' in body
    assert "Porsche Centro" in body
    assert "Horario de atención" in body
    assert "Av. Reforma 10" in body
    assert admin.get("/chargers/b/same").status_code == 404
    operator = _logged_client(db, tmp_path, monkeypatch, role="operator")
    body = operator.get(url).text
    assert 'id="site-details"' not in body
    assert "Responsable" not in body
    assert "811112222" not in body
    assert operator.get(url + "/site").status_code == 403
    assert operator.post(url + "/site", data=data,
                         headers={"origin": "https://testserver"}).status_code == 403
    assert admin.post(url + "/site", data=data,
                      headers={"origin": "https://another.example"}).status_code == 403


def test_missing_fields_and_contact_only_phone_link(tmp_path, monkeypatch):
    db = warehouse(tmp_path)
    admin = _logged_client(db, tmp_path, monkeypatch)
    url = "/chargers/a/same"
    res = admin.post(url + "/site", data={"contact_phone": "8112345678"},
                     headers={"origin": "https://testserver"})
    assert res.status_code == 200
    body = res.text
    assert "Llamar al contacto" in body
    assert "Llamar a sucursal" not in body
    assert "Abrir mapa" not in body
    assert "Nombre del contacto</dt>" not in body
    assert "Teléfono de sucursal</dt>" not in body
    assert "Cliente</dt>" not in body
    assert "Horario de atención" in body
    assert "site-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in body
    assert "@media(max-width:700px){.site-grid{grid-template-columns:1fr" in body


def test_legacy_basic_without_principal_never_sees_contact(tmp_path, monkeypatch):
    db = warehouse(tmp_path)
    save_site(db, "a", "same", meta("Client", branch="8112345678"),
              allowed_source_ids=None)
    monkeypatch.delenv("OCPP_REPORTS_AUTH_DATABASE", raising=False)
    monkeypatch.delenv("OCPP_REPORTS_AUTH_TENANT", raising=False)
    client = TestClient(create_app(db, allowed_source_ids=("a",)))
    page = client.get("/chargers/a/same")
    assert 'id="site-details"' not in page.text
    assert "tel:8112345678" not in page.text
    assert client.get("/chargers/a/same/site").status_code == 403
    snap = client.get("/api/snapshot").json()
    assert "branch_phone" not in json.dumps(snap)
    assert "contact_name" not in json.dumps(snap)


def test_customer_identity_is_shared_across_distinct_branches(tmp_path):
    db = warehouse(tmp_path)
    first = save_site(db, "a", "same",
                      meta("Porsche", location_name="Porsche Norte"),
                      allowed_source_ids=("a",))
    customer_id = site_for_charger(db, "a", "same")["customer_id"]
    second_fields = meta("Porsche", location_name="Porsche Sur")
    second_fields["customer_id"] = customer_id
    second = save_site(db, "a", "second", second_fields,
                       allowed_source_ids=("a",))
    assert first != second
    assert site_for_charger(db, "a", "second")["customer_id"] == customer_id
    assert site_for_charger(db, "a", "same")["location_name"] == "Porsche Norte"
    second_fields["customer_name"] = "Porsche México"
    save_site(db, "a", "second", second_fields, allowed_source_ids=("a",))
    assert site_for_charger(db, "a", "same")["customer_name"] == "Porsche México"


def test_contact_name_without_phone_and_branch_only_phone(tmp_path, monkeypatch):
    db = warehouse(tmp_path)
    admin = _logged_client(db, tmp_path, monkeypatch)
    url = "/chargers/a/same"
    data = {"contact_name": "Supervisora", "branch_phone": "8112345678"}
    result = admin.post(url + "/site", data=data,
                        headers={"origin": "https://testserver"})
    assert result.status_code == 200
    body = result.text
    assert "Supervisora" in body
    assert "Llamar a sucursal" in body
    assert "Llamar al contacto" not in body
    assert "Abrir mapa" not in body
    assert "Teléfono del contacto</dt>" not in body


def test_sensitive_customer_name_rename_is_scoped_across_branches(tmp_path):
    db = warehouse(tmp_path)
    save_site(db, "a", "same", meta("Original"), allowed_source_ids=None)
    customer_id = site_for_charger(db, "a", "same")["customer_id"]
    second = meta("Original")
    second["customer_id"] = customer_id
    save_site(db, "b", "same", second, allowed_source_ids=None)
    with pytest.raises(PermissionError):
        save_site(db, "a", "same", meta("Escaped"), allowed_source_ids=("a",))
    assert site_for_charger(db, "b", "same")["customer_name"] == "Original"


def test_shared_site_label_overlays_only_exact_charger_and_multiline_address(tmp_path):
    db = warehouse(tmp_path)
    values = meta("Client", "Calle Norte 10\nMonterrey", location_name="Sucursal Norte")
    site = save_site(db, "a", "same", values, allowed_source_ids=("a",))
    scoped = snapshot(db, allowed_source_ids=("a",))
    assert next(row for row in scoped["connectors"] if row["charger_id"] == "same")["location"] == "Sucursal Norte"
    assert next(row for row in scoped["connectors"] if row["charger_id"] == "second")["location"] == "a"
    assert snapshot(db, allowed_source_ids=("b",))["connectors"][0]["location"] == "b"
    assert "%0A" in site_for_charger(db, "a", "same")["map_url"]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE report_sites SET contact_phone=? WHERE site_id=?",
                     ("javascript:alert(1)", site))
    assert site_for_charger(db, "a", "same")["contact_tel"] is None
