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
    assert html.index('id="dashboard-filters"') < html.index('id="content"')
    assert html.index('id="operations-heading"') < html.index('id="connectors"')
    assert 'type="submit"' not in html
    assert 'setTimeout(apply, 650)' in html
    assert 'name="filter"' in html and 'name="location"' not in html
    assert 'name="status"' not in html and 'name="date_from"' not in html
    assert 'name="date_to"' not in html and 'id="reset-filters"' not in html
    assert html.index('id="connectors"') < html.index('id="synchronization"')
    card_classes = ["operational-card--available", "operational-card--charging",
                    "operational-card--fault", "operational-card--uncertain"]
    card_positions = [html.index('class="operational-card ' + name + '"') for name in card_classes]
    assert card_positions == sorted(card_positions)
    for name, border in zip(card_classes, ("#19975b", "#dba520", "#b94d4d", "#8994a3")):
        assert "." + name + "{border-left:4px solid " + border + "}" in html
    assert 'href="#connectors"' in html
    assert 'data-label="Snapshot recibido"' in html
    assert 'Estado reportado' not in html
    assert html.index('class="transactions-panel"') < html.index('id="synchronization"')
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


def test_status_counts_do_not_double_count_station_zero():
    rows = [
        {"source_id": "a", "charger_id": "same", "connector_id": 0, "status": "Available"},
        {"source_id": "a", "charger_id": "same", "connector_id": 1, "status": "Available"},
        {"source_id": "a", "charger_id": "same", "connector_id": 2, "status": "Available"},
        {"source_id": "b", "charger_id": "same", "connector_id": 0, "status": "Available"},
        {"source_id": "b", "charger_id": "same", "connector_id": 1, "status": "Charging"},
        {"source_id": "b", "charger_id": "same", "connector_id": 2, "status": "Faulted", "stale": True},
        {"source_id": "c", "charger_id": "station-only", "connector_id": 0, "status": "Unavailable"},
    ]
    got = web._operational_status_counts(rows)
    assert got["available"] == {"stations": 1, "connectors": 2}
    assert got["charging"] == {"stations": 1, "connectors": 1}
    assert got["faults"] == {"stations": 2, "connectors": 1}
    assert got["uncertain"] == {"stations": 1, "connectors": 1}


def test_status_cards_render_station_and_connector_totals(monkeypatch, tmp_path):
    def data(*args, **kwargs):
        rows = _snapshot()
        rows["connectors"] = [
            {"source_id": "site-a", "charger_id": "cp", "connector_id": cid,
             "status": "Available", "connected": True,
             "observed_at": "2099-01-01T00:00:00Z", "location": "Site A",
             "power_w": None, "energy_wh": None}
            for cid in (0, 1, 2)
        ]
        return rows

    monkeypatch.setattr(web, "snapshot", data)
    client = TestClient(web.create_app(tmp_path / "unused"))
    for view in ("combined", "station", "individual"):
        html = client.get("/?connector_view=" + view).text
        assert 'class="operational-card operational-card--available" href="#connectors"><strong>1 (2)</strong>' in html
        assert 'class="operational-card operational-card--charging" href="#connectors"><strong>0 (0)</strong>' in html
        assert "Estaciones (conectores individuales)" in html
