"""Status changes, connection observations, and collector freshness are separate."""
from datetime import datetime, timedelta, timezone

import pytest

from ocpp_reports import web


def _html(dashboard_client_factory, data):
    response = dashboard_client_factory(data).get("/")
    assert response.status_code == 200
    return response.text


def test_unchanged_51_hour_status_with_recent_source_and_connected_stays_available(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory()
    html = _html(dashboard_client_factory, data)
    assert 'status status--ok">Available</span>' in html
    assert 'status status--ok" title=' in html
    assert ">Origen atrasado</span>" not in html
    assert ">Estado sin fecha</span>" not in html
    assert '1 (1)</strong><span>Disponibles' in html
    assert web.display_date(data["connectors"][0]["status_observed_at"], "UTC") in html


def test_recent_source_with_observed_disconnection_is_not_available(dashboard_client_factory, dashboard_snapshot_factory):
    html = _html(dashboard_client_factory, dashboard_snapshot_factory(connected=False))
    assert 'status status--historic">Available</span>' in html
    assert 'status status--off" title=' in html
    assert '0 (0)</strong><span>Disponibles' in html
    assert '1 (1)</strong><span>Datos antiguos' in html


def test_old_source_stays_neutral_despite_last_connected_true(dashboard_client_factory, dashboard_snapshot_factory):
    html = _html(dashboard_client_factory, dashboard_snapshot_factory(source_age_seconds=3600))
    assert 'status status--historic">Available</span>' in html
    assert 'status status--historic" title=' in html
    assert ">Origen atrasado</span>" in html
    assert '0 (0)</strong><span>Disponibles' in html


def test_missing_status_timestamp_does_not_invent_new_status(dashboard_client_factory, dashboard_snapshot_factory):
    html = _html(dashboard_client_factory, dashboard_snapshot_factory(status_timestamp=False))
    assert ">Estado sin fecha</span>" in html
    assert ">Origen atrasado</span>" not in html
    assert 'status status--ok">Available</span>' in html


def test_absent_source_evidence_does_not_claim_live_connectivity(dashboard_client_factory, dashboard_snapshot_factory):
    html = _html(dashboard_client_factory, dashboard_snapshot_factory(source_age_seconds=None))
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
        dashboard_client_factory, dashboard_snapshot_factory, raw_flag, badge, status_class,
        available_count, uncertain_count):
    # Model the SQLite snapshot's integer column rather than a boolean fixture.
    data = dashboard_snapshot_factory(connected=raw_flag)
    html = _html(dashboard_client_factory, data)
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


def test_sqlite_snapshot_api_retains_raw_connection_flag(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory(connected=1)
    client = dashboard_client_factory(data)
    assert client.get("/").status_code == 200
    assert client.get("/api/snapshot").json()["connectors"][0]["connected"] == 1


@pytest.mark.parametrize("search", ["Available", "Garage", "charger-a"])
def test_connector_search_preserves_original_source_freshness(dashboard_client_factory, dashboard_snapshot_factory, search):
    data = dashboard_snapshot_factory(connected=1)
    client = dashboard_client_factory(data)
    page = client.get("/", params={"filter": search, "connector_view": "individual", "tz": "UTC"})
    assert page.status_code == 200
    assert 'status status--ok">Available</span>' in page.text
    assert 'status status--ok" title=' in page.text
    assert '1 (1)</strong><span>Disponibles' in page.text
    assert ">Origen sin fecha</span>" not in page.text
    assert ">Origen atrasado</span>" not in page.text
    assert web.display_date(data["connectors"][0]["status_observed_at"], "UTC") in page.text
    assert "Sin orígenes" in page.text
    assert client.get("/api/snapshot").json()["sources"][0]["source_id"] == "site-a"


def test_filtered_connector_still_warns_about_old_source(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory(connected=1, source_age_seconds=3600)
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    html = dashboard_client_factory(data).get(
        "/", params={"filter": "Available"}).text
    assert ">Origen atrasado</span>" in html
    assert 'status status--historic">Available</span>' in html


def test_filtered_connector_does_not_widen_source_authorization(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory(connected=1)
    client = dashboard_client_factory(data, allowed_source_ids=("site-a",))
    assert client.get("/", params={"filter": "Available"}).status_code == 200
    assert client.get("/", params={"source_id": "other", "filter": "Available"}).status_code == 403


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [(0, "fresh"), (59, "fresh"), (60, "fresh"),
     (120, "fresh"), (180, "fresh"), (181, "old")],
)
def test_charger_activity_allows_three_heartbeat_intervals(age_seconds, expected):
    now = datetime.now(timezone.utc)
    activity = (now - timedelta(seconds=age_seconds)).isoformat()
    assert web._charger_activity_freshness(activity, now) == expected


def test_charger_activity_does_not_use_status_notification_age(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory(status_age_hours=51)
    data["connectors"][0]["last_activity_at"] = datetime.now(timezone.utc).isoformat()
    html = _html(dashboard_client_factory, data)
    assert 'status status--ok">Available</span>' in html


def test_old_charger_activity_overrides_recent_pipeline(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory()
    data["connectors"][0]["last_activity_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=240)
    ).isoformat()
    html = _html(dashboard_client_factory, data)
    assert 'status status--historic">Available</span>' in html
    assert '0 (0)</strong><span>Disponibles' in html


def test_missing_charger_activity_preserves_legacy_behavior(dashboard_client_factory, dashboard_snapshot_factory):
    html = _html(dashboard_client_factory, dashboard_snapshot_factory())
    assert 'status status--ok">Available</span>' in html


@pytest.mark.parametrize("timestamp", [
    None, "", "not-a-date", "2026-11-05T03:00:00",
])
def test_charger_activity_without_valid_utc_evidence_is_unknown(timestamp):
    assert web._charger_activity_freshness(
        timestamp, datetime.now(timezone.utc)
    ) == "unknown"


def test_recent_charger_activity_cannot_hide_stale_replication(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory(source_age_seconds=3600)
    data["connectors"][0]["last_activity_at"] = datetime.now(timezone.utc).isoformat()
    html = _html(dashboard_client_factory, data)
    assert 'status status--historic">Available</span>' in html
    assert '0 (0)</strong><span>Disponibles' in html


def test_activity_metadata_does_not_change_raw_snapshot(dashboard_client_factory, dashboard_snapshot_factory):
    data = dashboard_snapshot_factory()
    data["connectors"][0]["last_activity_at"] = datetime.now(timezone.utc).isoformat()
    client = dashboard_client_factory(data)
    connector = client.get("/api/snapshot").json()["connectors"][0]
    assert connector["last_activity_at"] == data["connectors"][0]["last_activity_at"]
    assert "activity_freshness" not in connector
    assert "stale" not in connector
