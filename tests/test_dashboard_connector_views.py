"""Conectores view changes only the rendered panel, never source authorization or API."""
from copy import deepcopy

from fastapi.testclient import TestClient

from ocpp_reports import web
from dashboard_html import DashboardHTML


def _snapshot():
    connectors = [
        {"source_id": "site-a", "charger_id": "cp-a", "connector_id": number,
         "location": "Garage A", "status": "Available", "connected": True,
         "observed_at": "2026-10-31T18:00:00Z", "energy_wh": None,
         "power_w": None, "rated_power_w": None}
        for number in (0, 1, 2)
    ]
    connectors.extend(
        {"source_id": "site-b", "charger_id": "cp-b", "connector_id": number,
         "location": "Garage B", "status": "Faulted", "connected": False,
         "observed_at": "2026-10-31T18:00:00Z", "energy_wh": None,
         "power_w": None, "rated_power_w": None}
        for number in (0, 1)
    )
    return {"sources": [], "connectors": connectors, "transactions": [], "events": []}


def _ids(html):
    return DashboardHTML(html).connector_ids()


def test_default_individual_and_explicit_combined_station(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(_snapshot()))
    client = TestClient(web.create_app(tmp_path / "unused"))
    default = client.get("/")
    assert default.status_code == 200
    assert _ids(default.text) == [1, 2, 1]
    assert DashboardHTML(default.text).view_pressed("individual")
    assert not DashboardHTML(default.text).view_pressed("station")
    assert not DashboardHTML(default.text).view_pressed("combined")
    assert 'name="connector_view" value="individual"' in default.text
    combined = client.get("/?connector_view=combined")
    assert _ids(combined.text) == [0, 1, 2, 0, 1]
    assert DashboardHTML(combined.text).view_pressed("combined")
    station = client.get("/?connector_view=station")
    assert _ids(station.text) == [0, 0]
    assert DashboardHTML(station.text).view_pressed("station")
    individual = client.get("/?connector_view=individual")
    assert _ids(individual.text) == [1, 2, 1]
    assert DashboardHTML(individual.text).view_pressed("individual")
    assert client.get("/?connector_view=other").status_code == 422
    # The API and the operational cards remain unfiltered.
    assert [c["connector_id"] for c in client.get("/api/snapshot").json()["connectors"]] == [0, 1, 2, 0, 1]
    assert "Falla / no disponible" in station.text


def test_connector_mode_respects_other_filters_and_authorization(monkeypatch, tmp_path):
    def data(*args, **kwargs):
        allowed = kwargs.get("allowed_source_ids")
        rows = deepcopy(_snapshot())
        if allowed is not None:
            rows["connectors"] = [c for c in rows["connectors"] if c["source_id"] in allowed]
        return rows
    monkeypatch.setattr(web, "snapshot", data)
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("site-a",),
                                       timezones=("America/Monterrey", "UTC")))
    page = client.get("/?source_id=site-a&tz=UTC&connector_view=individual")
    assert page.status_code == 200
    assert _ids(page.text) == [1, 2]
    assert 'name="connector_view" value="individual"' in page.text
    assert 'next.searchParams.set("connector_view", button.dataset.connectorView)' in page.text
    assert client.get("/?source_id=site-b&connector_view=station").status_code == 403
    assert _ids(client.get("/?filter=garage%20a&connector_view=station").text) == [0]
    assert "Sin conectores registrados" in client.get("/?filter=missing&connector_view=station").text


def test_directional_discrepancy_badges_are_single_and_colored(monkeypatch, tmp_path):
    data = _snapshot()
    # Single-connector stations in both directions; no multi-connector inference.
    data["connectors"] = [
        {"source_id": "site-w", "charger_id": "cp-warning", "connector_id": 0,
         "status": "Available", "connected": True, "source_freshness": "fresh"},
        {"source_id": "site-w", "charger_id": "cp-warning", "connector_id": 1,
         "status": "Faulted", "connected": True, "source_freshness": "fresh"},
        {"source_id": "site-f", "charger_id": "cp-failed", "connector_id": 0,
         "status": "Faulted", "connected": True, "source_freshness": "fresh"},
        {"source_id": "site-f", "charger_id": "cp-failed", "connector_id": 1,
         "status": "Available", "connected": True, "source_freshness": "fresh"},
        {"source_id": "site-a", "charger_id": "cp-ok", "connector_id": 0,
         "status": "Available", "connected": True, "source_freshness": "fresh"},
        {"source_id": "site-a", "charger_id": "cp-ok", "connector_id": 1,
         "status": "Available", "connected": True, "source_freshness": "fresh"},
    ]
    # Freshness is derived from source ingestion receipts, not connector flags.
    from datetime import datetime, timezone
    ingested_at = datetime.now(timezone.utc).isoformat()
    data["sources"] = [
        {"source_id": source_id, "last_ingested_at": ingested_at}
        for source_id in ("site-w", "site-f", "site-a")
    ]
    # Dashboard template expects these standard connector metrics even when absent.
    for connector in data["connectors"]:
        connector.update(energy_wh=None, power_w=None, rated_power_w=None,
                         connection_fresh=True, status_time_unknown=False)
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    client = TestClient(web.create_app(tmp_path / "unused"))
    html = client.get("/?connector_view=individual").text

    badges = [node for node in DashboardHTML(html).status_badges()
              if node["text"].strip() in {"Warning", "Failed", "Available"}]
    assert len(badges) == 3
    warning, failed, available = badges
    assert warning["text"].strip() == "Warning" and "status--warn" in warning["attrs"].get("class", "")
    assert "C0: Available" in warning["attrs"].get("title", "") and "C1: Faulted" in warning["attrs"].get("title", "")
    assert "Warning" in warning["attrs"].get("aria-label", "")
    assert failed["text"].strip() == "Failed" and "status--off" in failed["attrs"].get("class", "")
    assert "C0: Faulted" in failed["attrs"].get("title", "") and "C1: Available" in failed["attrs"].get("title", "")
    assert available["text"].strip() == "Available" and "status--ok" in available["attrs"].get("class", "")
    assert "Discrepancia con estación" not in html
    # API remains evidence-only; the presentation badge is not persisted.
    assert all("status_discrepancy_badge" not in row
               for row in client.get("/api/snapshot").json()["connectors"])


def test_directional_badges_ignore_multiconnector_and_unknown_states():
    rows = [
        {"source_id": "s", "charger_id": "multi", "connector_id": 0, "status": "Faulted"},
        {"source_id": "s", "charger_id": "multi", "connector_id": 1, "status": "Available"},
        {"source_id": "s", "charger_id": "multi", "connector_id": 2, "status": "Faulted"},
        {"source_id": "s", "charger_id": "other", "connector_id": 0, "status": "Unavailable"},
        {"source_id": "s", "charger_id": "other", "connector_id": 1, "status": "Available"},
    ]
    assert web._single_connector_discrepancy_badges(rows) == {}
