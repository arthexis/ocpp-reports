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
    assert 'data-label="Snapshot recibido"' not in html
    assert '<th>Snapshot recibido</th>' not in html
    assert 'Estado reportado' not in html
    assert html.index('class="transactions-panel"') < html.index('id="synchronization"')
    assert "cp-fault" in html and "Faulted" in html
    assert "Falla / no disponible (último estado)" in html
    assert "Una falla con origen atrasado también puede contarse como dato incierto" in html
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in html
    assert "connectors-panel td::before" in html



def test_empty_connector_table_spans_eight_columns(monkeypatch, tmp_path):
    def empty_connectors(*args, **kwargs):
        data = _snapshot()
        data["connectors"] = []
        return data

    monkeypatch.setattr(web, "snapshot", empty_connectors)
    client = TestClient(web.create_app(tmp_path / "unused"))
    html = client.get("/").text
    assert 'colspan="8">Sin conectores registrados' in html
    assert '<th>Snapshot recibido</th>' not in html



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


def test_warning_connector_counts_as_available_without_hiding_reported_fault():
    rows = [
        {"source_id": source, "charger_id": charger,
         "connector_id": cid, "status": status}
        for source, charger, cid, status in (
            ("a", "cp-a", 0, "Available"),
            ("a", "cp-a", 1, "Available"),
            ("a", "cp-a", 2, "Available"),
            ("b", "cp-b", 0, "Available"),
            ("b", "cp-b", 1, "Available"),
            ("b", "cp-b", 2, "Available"),
            ("c", "cp-c", 0, "Available"),
            ("c", "cp-c", 1, "Faulted"),
        )
    ]
    assert web._single_connector_discrepancy_badges(rows)[("c", "cp-c", 1)] == "Warning"
    counts = web._operational_status_counts(rows)
    assert counts["available"] == {"stations": 3, "connectors": 5}
    assert counts["faults"] == {"stations": 0, "connectors": 1}

    # The opposite disagreement must not be classified as available
    # at station level merely because its sole connector reports Available.
    reverse = [
        {"source_id": "d", "charger_id": "cp-d", "connector_id": 0, "status": "Faulted"},
        {"source_id": "d", "charger_id": "cp-d", "connector_id": 1, "status": "Available"},
    ]
    assert web._single_connector_discrepancy_badges(reverse)[("d", "cp-d", 1)] == "Failed"


def test_status_cards_render_station_and_connector_totals(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    def data(*args, **kwargs):
        rows = _snapshot()
        rows["sources"][0]["last_ingested_at"] = datetime.now(timezone.utc).isoformat()
        rows["connectors"] = [
            {"source_id": "site-a", "charger_id": "cp", "connector_id": cid,
             "status": "Available", "connected": True,
             "observed_at": "2099-01-01T00:00:00Z",
             "status_observed_at": datetime.now(timezone.utc).isoformat(),
             "location": "Site A",
             "power_w": None, "energy_wh": None}
            for cid in (0, 1, 2)
        ]
        return rows

    monkeypatch.setattr(web, "snapshot", data)
    client = TestClient(web.create_app(tmp_path / "unused"))
    for view in ("combined", "station", "individual"):
        html = client.get("/?connector_view=" + view).text
        assert 'class="operational-card operational-card--available" href="#connectors"><strong>1 (2)</strong>' in html
        assert 'class="operational-card operational-card--charging"' in html
        assert "tx_view=active" in html
        assert "<strong>0 (0)</strong><span>Cargando</span>" in html
        assert "Estaciones (conectores individuales)" in html


def test_status_provenance_does_not_freshen_with_snapshots(monkeypatch, tmp_path):
    """A new export cannot overwrite the age of an unchanged fault."""
    from datetime import datetime, timedelta, timezone
    from copy import deepcopy

    now = datetime.now(timezone.utc)
    original = (now - timedelta(hours=3)).isoformat()
    snapshot_time = now.isoformat()
    data = {
        "sources": [{"source_id": "site-a", "cursor": 3,
                     "last_ingested_at": None,
                     "last_replication_received_at": snapshot_time}],
        "connectors": [{
            "source_id": "site-a", "charger_id": "cp-a",
            "connector_id": 1, "location": "Garage",
            "status": "Faulted", "connected": True,
            "observed_at": snapshot_time,
            "status_observed_at": original,
            "power_w": None, "energy_wh": None,
        }],
        "transactions": [], "events": [],
    }
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("site-a",)))
    html = client.get("/").text
    assert ">Origen atrasado</span>" not in html
    assert ">Estado sin fecha</span>" not in html
    assert web.display_date(original, "UTC") in html
    assert 'data-label="Snapshot recibido"' not in html
    assert '<th>Snapshot recibido</th>' not in html
    assert "Recepción de réplica" in html
    assert '0 (0)</strong><span>Disponibles' in html
    assert '1 (1)</strong><span>Falla' in html
    assert '0 (0)</strong><span>Datos antiguos' in html
    # Presentation-only age classification does not alter authoritative API evidence.
    raw = client.get("/api/snapshot").json()["connectors"][0]
    assert raw["status_observed_at"] == original
    assert raw["observed_at"] == snapshot_time


def test_missing_or_invalid_original_status_time_is_unknown(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from copy import deepcopy

    current = datetime.now(timezone.utc).isoformat()
    source = _snapshot()
    source["connectors"] = [{
        "source_id": "site-a", "charger_id": "old-producer",
        "connector_id": 1, "location": "Garage",
        "status": "Available", "connected": True,
        "observed_at": current, "status_observed_at": None,
        "power_w": None, "energy_wh": None,
    }]
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(source))
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("site-a",)))
    html = client.get("/").text
    assert ">Estado sin fecha</span>" in html
    assert '0 (0)</strong><span>Disponibles' in html
    assert '1 (1)</strong><span>Datos antiguos' in html
    for bad in ("not-a-time", "2026-10-29T15:35:00"):
        source["connectors"][0]["status_observed_at"] = bad
        assert ">Estado sin fecha</span>" in client.get("/").text
