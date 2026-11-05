"""Shared dashboard test fixtures."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from ocpp_reports import web

@pytest.fixture
def dashboard_source():
    return (Path(__file__).resolve().parents[1] / "src/ocpp_reports/templates/dashboard.html.j2").read_text()

@pytest.fixture
def empty_dashboard_html(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    response = TestClient(web.create_app(tmp_path / "unused")).get("/")
    assert response.status_code == 200
    return response.text
