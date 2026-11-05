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


def test_default_individual_and_explicit_combined_station(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(_snapshot()))
    client = TestClient(web.create_app(tmp_path / "unused"))
    default = client.get("/")
    assert default.status_code == 200
    assert _ids(default.text) == [1, 2, 1]
    assert 'data-connector-view="individual" aria-pressed="true"' in default.text
    assert 'data-connector-view="station" aria-pressed="false"' in default.text
    assert 'data-connector-view="combined" aria-pressed="false"' in default.text
    assert 'name="connector_view" value="individual"' in default.text
    combined = client.get("/?connector_view=combined")
    assert _ids(combined.text) == [0, 1, 2, 0, 1]
    assert 'data-connector-view="combined" aria-pressed="true"' in combined.text
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


def test_directional_discrepancy_badges_are_single_and_colored(monkeypatch, tmp_path):
    from html.parser import HTMLParser

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
    # Dashboard template expects these standard connector metrics even when absent.
    for connector in data["connectors"]:
        connector.update(energy_wh=None, power_w=None, rated_power_w=None)
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    client = TestClient(web.create_app(tmp_path / "unused"))
    html = client.get("/?connector_view=individual").text

    class StatusCells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_status = False
            self.primary_seen = False
            self.in_primary_span = False
            self.badges = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "td" and attrs.get("data-label") == "Estado":
                self.in_status = True
                self.primary_seen = False
                self.in_primary_span = False
            if self.in_status and not self.primary_seen and tag == "span" and "status" in attrs.get("class", "").split():
                self.primary_seen = True
                self.in_primary_span = True
                self.badges.append({"class": attrs["class"], "title": attrs.get("title", ""),
                                    "aria": attrs.get("aria-label", ""), "text": ""})

        def handle_data(self, data):
            if self.in_primary_span:
                self.badges[-1]["text"] += data

        def handle_endtag(self, tag):
            if tag == "span" and self.in_primary_span:
                self.in_primary_span = False
            if tag == "td":
                self.in_status = False
                self.in_primary_span = False

    parser = StatusCells()
    parser.feed(html)
    assert len(parser.badges) == 3
    warning, failed, available = parser.badges
    assert warning["text"] == "Warning" and "status--warn" in warning["class"]
    assert "C0: Available" in warning["title"] and "C1: Faulted" in warning["title"]
    assert "Warning" in warning["aria"]
    assert failed["text"] == "Failed" and "status--off" in failed["class"]
    assert "C0: Faulted" in failed["title"] and "C1: Available" in failed["title"]
    assert available["text"] == "Available" and "status--ok" in available["class"]
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
