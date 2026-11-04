"""The live dashboard must refresh data without document navigation or losing scroll."""
from fastapi.testclient import TestClient

from ocpp_reports import web


def test_dashboard_polls_data_in_place_without_meta_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    page = TestClient(web.create_app(tmp_path / "empty")).get("/")
    assert page.status_code == 200
    html = page.text
    assert 'http-equiv="refresh"' not in html
    assert 'id="live-event-groups"' in html
    assert 'window.setInterval(refreshDashboard, 15000)' in html
    assert 'cache: "no-store"' in html
    assert 'window.location.pathname + window.location.search' in html
    assert 'new DOMParser().parseFromString' in html
    # Data is swapped under the existing scroll regions, not their containers.
    for selector in (
        '".operations-cards"', '".connectors-panel tbody"',
        '".transactions-panel tbody"', '".transactions-panel .transaction-pagination"',
        '".sync-panel tbody"',
    ):
        assert selector in html
    assert 'eventRegion.querySelector("details[open]")' in html
    assert 'current.contains(document.activeElement)' in html
    assert 'setupEventGroups(eventRegion)' in html
    # Filter buttons retain their original listeners and selected state.
    assert 'button.textContent = next.textContent' in html
    assert '".transaction-filters",' not in html


def test_polling_uses_same_filtered_route_without_replacing_form(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    client = TestClient(web.create_app(tmp_path / "empty", timezones=("UTC",)))
    page = client.get("/?filter=example&tx_view=all&connector_view=station&tz=UTC")
    assert page.status_code == 200
    assert 'value="example"' in page.text
    assert 'data-connector-view="station" aria-pressed="true"' in page.text
    assert 'data-tx-view="all" aria-pressed="true"' in page.text
    assert 'id="dashboard-filters"' in page.text
    assert 'document.querySelector(".transaction-filters [data-tx-view]")' not in page.text
