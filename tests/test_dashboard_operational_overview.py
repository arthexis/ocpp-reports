"""Operational dashboard priority, source boundaries and responsive markup."""
from fastapi.testclient import TestClient

from ocpp_reports import web


def _snapshot(*args, **kwargs):
    return {
        "sources": [{"source_id": "site-a", "cursor": 4, "last_ingested_at": None}],
        "connectors": [
            {"source_id": "site-a", "charger_id": "cp-fault", "connector_id": 1,
             "status": "Faulted", "connected": False, "power_w": None, "energy_wh": None,
             "observed_at": "2020-01-01T00:00:00Z"},
            {"source_id": "site-a", "charger_id": "cp-charge", "connector_id": 1,
             "status": "Charging", "connected": True, "power_w": 8000, "energy_wh": 12000,
             "observed_at": "2099-01-01T00:00:00Z"},
            {"source_id": "site-a", "charger_id": "cp-unknown", "connector_id": 0,
             "status": None, "connected": False, "power_w": None, "energy_wh": None,
             "observed_at": None},
        ],
        "transactions": [], "events": [],
    }


def test_status_first_and_historic_fault_not_hidden(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", _snapshot)
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("site-a",)))
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert html.index("Estado de los cargadores") < html.index("Filtrar resultados")
    assert html.index('id="connectors"') < html.index('id="synchronization"')
    assert 'href="#connectors"' in html
    assert 'data-label="Observado"' in html
    assert "cp-fault" in html and "Faulted" in html
    assert "Falla / no disponible (último estado)" in html
    assert "Una falla antigua también puede contarse como dato antiguo" in html
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in html
    assert "connectors-panel td::before" in html


def test_existing_source_scope_still_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", _snapshot)
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("site-a",)))
    assert client.get("/?source_id=site-b").status_code == 403
    assert client.get("/api/snapshot?source_id=site-b").status_code == 403
