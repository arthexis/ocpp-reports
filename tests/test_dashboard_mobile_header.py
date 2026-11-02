"""Regression checks for compact mobile navigation without losing search access."""
from fastapi.testclient import TestClient

from ocpp_reports import web


def test_mobile_header_preserves_search_and_timezone_controls(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    response = TestClient(web.create_app(tmp_path / "unused")).get("/")
    assert response.status_code == 200
    html = response.text
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    compact = css.split("/* Narrow phones:", 1)[1].split("\n}", 1)[0]
    assert "@media(max-width:520px)" in compact
    assert "header{padding:.5rem .9rem;gap:.3rem}" in compact
    assert ".header-filters label{gap:0;font-size:0;line-height:0}" in compact
    assert "min-height:40px" in compact
    assert 'id="dashboard-search"' in html
    assert 'name="filter"' in html
    assert 'placeholder="Origen, cargador, ubicación o estado"' in html
    assert '<label for="dashboard-search">Filtrar <input' in html
    assert 'id="timezone-choice"' in html
    assert 'id="dashboard-filters"' in html
