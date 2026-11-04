"""Status changes, connection observations, and collector freshness are separate."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ocpp_reports import web


def _data(*, status_age_hours=51, source_age_seconds=20,
          connected=True, status="Available", status_timestamp=True):
    now = datetime.now(timezone.utc)
    data = {
        "sources": [{"source_id": "site-a", "cursor": 100,
                     "last_ingested_at": (now - timedelta(seconds=source_age_seconds)).isoformat()
                     if source_age_seconds is not None else None}],
        "connectors": [{
            "source_id": "site-a", "charger_id": "charger-a",
            "connector_id": 1, "location": "Garage", "status": status,
            "connected": connected,
            "status_observed_at": (now - timedelta(hours=status_age_hours)).isoformat()
            if status_timestamp else None,
            "observed_at": now.isoformat(),
            "power_w": None, "energy_wh": None,
        }],
        "transactions": [], "events": [],
    }
    return data


def _html(monkeypatch, tmp_path, data):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    response = TestClient(web.create_app(tmp_path / "warehouse")).get("/")
    assert response.status_code == 200
    return response.text


def test_unchanged_51_hour_status_with_recent_source_and_connected_stays_available(monkeypatch, tmp_path):
    data = _data()
    html = _html(monkeypatch, tmp_path, data)
    assert 'status status--ok">Available</span>' in html
    assert 'status status--ok" title=' in html
    assert ">Origen atrasado</span>" not in html
    assert ">Estado sin fecha</span>" not in html
    assert '1 (1)</strong><span>Disponibles' in html
    assert web.display_date(data["connectors"][0]["status_observed_at"], "UTC") in html


def test_recent_source_with_observed_disconnection_is_not_available(monkeypatch, tmp_path):
    html = _html(monkeypatch, tmp_path, _data(connected=False))
    assert 'status status--historic">Available</span>' in html
    assert 'status status--off" title=' in html
    assert '0 (0)</strong><span>Disponibles' in html
    assert '1 (1)</strong><span>Datos antiguos' in html


def test_old_source_stays_neutral_despite_last_connected_true(monkeypatch, tmp_path):
    html = _html(monkeypatch, tmp_path, _data(source_age_seconds=3600))
    assert 'status status--historic">Available</span>' in html
    assert 'status status--historic" title=' in html
    assert ">Origen atrasado</span>" in html
    assert '0 (0)</strong><span>Disponibles' in html


def test_missing_status_timestamp_does_not_invent_new_status(monkeypatch, tmp_path):
    html = _html(monkeypatch, tmp_path, _data(status_timestamp=False))
    assert ">Estado sin fecha</span>" in html
    assert ">Origen atrasado</span>" not in html
    assert 'status status--ok">Available</span>' in html


def test_absent_source_evidence_does_not_claim_live_connectivity(monkeypatch, tmp_path):
    html = _html(monkeypatch, tmp_path, _data(source_age_seconds=None))
    assert ">Origen sin fecha</span>" in html
    assert 'status status--historic" title=' in html


def test_timestamp_validation_and_pipeline_freshness_are_distinct():
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=20)).isoformat()
    old = (now - timedelta(hours=51)).isoformat()
    assert web._source_freshness({"last_ingested_at": fresh}, now) == "fresh"
    assert web._source_freshness({"last_ingested_at": old}, now) == "old"
    assert web._source_freshness({"last_ingested_at": None}, now) == "unknown"
    assert web._source_freshness({
        "last_ingested_at": None,
        "last_replication_received_at": fresh,
    }, now) == "fresh"
    assert web._freshness("2026-10-29T15:35:00", now) == "unknown"


@pytest.mark.parametrize(
    ("raw_flag", "badge", "status_class", "available_count", "uncertain_count"),
    [
        (1, "Sí", "status--ok", "1 (1)", "0 (0)"),
        (0, "No", "status--historic", "0 (0)", "1 (1)"),
        (None, "Sin datos", "status--historic", "0 (0)", "1 (1)"),
        ("1", "Sin datos", "status--historic", "0 (0)", "1 (1)"),
        (2, "Sin datos", "status--historic", "0 (0)", "1 (1)"),
    ],
)
def test_sqlite_connection_flags_drive_badges_and_counts(
        monkeypatch, tmp_path, raw_flag, badge, status_class,
        available_count, uncertain_count):
    # Model the SQLite snapshot's integer column rather than a boolean fixture.
    data = _data(connected=raw_flag)
    html = _html(monkeypatch, tmp_path, data)
    assert f'status {status_class}">Available</span>' in html
    assert f'>{badge}</span>' in html
    assert f'{available_count}</strong><span>Disponibles' in html
    assert f'{uncertain_count}</strong><span>Datos antiguos' in html
    assert web.display_date(data["connectors"][0]["status_observed_at"], "UTC") in html


@pytest.mark.parametrize(
    ("raw_flag", "expected"),
    [(True, True), (False, False), (1, True), (0, False),
     (None, None), ("1", None), (2, None)],
)
def test_connection_flag_normalization_is_strict(raw_flag, expected):
    assert web._connection_flag(raw_flag) is expected


def test_sqlite_snapshot_api_retains_raw_connection_flag(monkeypatch, tmp_path):
    data = _data(connected=1)
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    client = TestClient(web.create_app(tmp_path / "warehouse"))
    assert client.get("/").status_code == 200
    assert client.get("/api/snapshot").json()["connectors"][0]["connected"] == 1
