"""Conectores view changes only the rendered panel, never source authorization or API."""
from copy import deepcopy

from fastapi.testclient import TestClient

from ocpp_reports import web


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
    from html.parser import HTMLParser

    class ConnectorTable(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inside = False
            self.rows = []
            self.active = False
        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "section" and attributes.get("id") == "connectors":
                self.inside = True
            if tag == "section" and self.inside and attributes.get("id") != "connectors":
                self.inside = False
            if self.inside and tag == "td" and attributes.get("data-label") == "Conector":
                self.active = True
        def handle_endtag(self, tag):
            if tag == "td":
                self.active = False
            if tag == "section" and self.inside:
                self.inside = False
        def handle_data(self, data):
            if self.inside and self.active:
                self.rows.append(int(data.strip()))
    parser = ConnectorTable()
    parser.feed(html)
    return parser.rows


def test_default_combined_and_station_individual(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(_snapshot()))
    client = TestClient(web.create_app(tmp_path / "unused"))
    combined = client.get("/")
    assert combined.status_code == 200
    assert _ids(combined.text) == [0, 1, 2, 0, 1]
    assert 'data-connector-view="combined" aria-pressed="true"' in combined.text
    assert 'data-connector-view="station" aria-pressed="false"' in combined.text
    assert 'data-connector-view="individual" aria-pressed="false"' in combined.text
    assert 'name="connector_view" value="combined"' in combined.text
    station = client.get("/?connector_view=station")
    assert _ids(station.text) == [0, 0]
    assert 'data-connector-view="station" aria-pressed="true"' in station.text
    individual = client.get("/?connector_view=individual")
    assert _ids(individual.text) == [1, 2, 1]
    assert 'data-connector-view="individual" aria-pressed="true"' in individual.text
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
