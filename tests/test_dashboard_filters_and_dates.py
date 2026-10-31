"""Dashboard renders minute-precision UTC timestamps without changing API evidence."""
from fastapi.testclient import TestClient

from ocpp_reports import web


def test_minute_utc_handles_offsets_and_missing_provenance():
    assert web.minute_utc("2026-10-30T22:18:16.279339+00:00") == "2026-10-30 22:18 UTC"
    assert web.minute_utc("2026-10-30T16:18:59-06:00") == "2026-10-30 22:18 UTC"
    assert web.minute_utc("2026-10-30T22:18:05Z") == "2026-10-30 22:18 UTC"
    assert web.minute_utc(None) == "—"
    assert web.minute_utc("not-an-instant") == "Fecha inválida"
    assert web.minute_utc("2026-10-30T22:18:05") == "2026-10-30 22:18 (zona desconocida)"


def test_all_dashboard_tables_shorten_dates_but_api_preserves_original(monkeypatch, tmp_path):
    at = "2026-10-30T22:18:16.279339+00:00"
    def data(*args, **kwargs):
        return {
            "sources": [{"source_id": "node-a", "cursor": 3,
                         "last_ingested_at": at, "last_replication_received_at": at}],
            "connectors": [{"source_id": "node-a", "charger_id": "charger-1",
                            "location": "Station A", "connector_id": 1,
                            "status": "Available", "connected": True,
                            "power_w": None, "energy_wh": None,
                            "status_observed_at": at, "observed_at": at}],
            "transactions": [{"source_id": "node-a", "charger_id": "charger-1",
                              "location": "Station A", "id": 1,
                              "state": "closed", "started_at": at, "stopped_at": at,
                              "energy_wh": 500}],
            "events": [{"source_id": "node-a", "charger_id": "charger-1",
                        "location": "Station A", "id": 3, "action": "Heartbeat",
                        "at": at}],
        }
    monkeypatch.setattr(web, "snapshot", data)
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("node-a",)))
    page = client.get("/")
    assert page.status_code == 200
    assert page.text.count("2026-10-30 22:18 UTC") >= 6
    assert at not in page.text
    assert client.get("/api/snapshot").json()["connectors"][0]["observed_at"] == at
    assert client.get("/?source_id=other").status_code == 403
    assert 'id="dashboard-filters"' in page.text
    assert 'name="filter"' in page.text and 'name="status"' in page.text and 'name="location"' in page.text
    assert 'type="submit"' not in page.text


def test_dashboard_configured_timezones_and_persistent_selector(monkeypatch, tmp_path):
    at = "2026-10-30T22:18:16Z"
    def data(*args, **kwargs):
        return {
            "sources": [{"source_id": "site-a", "cursor": 3}],
            "connectors": [{"source_id": "site-a", "charger_id": "cp-a", "location": "Audi Calzada",
                            "connector_id": 1, "status": "Available", "connected": True,
                            "observed_at": at, "power_w": None, "energy_wh": None}],
            "transactions": [{"source_id": "site-a", "charger_id": "cp-a", "location": "Audi Calzada",
                              "id": 4, "state": "stopped", "started_at": at,
                              "stopped_at": at, "soc_start": 20, "soc_end": 82,
                              "energy_wh": 15000}],
            "events": [{"source_id": "site-a", "charger_id": "cp-a", "location": "Audi Calzada",
                        "id": 9, "action": "Heartbeat", "at": at}],
        }
    monkeypatch.setattr(web, "snapshot", data)
    app = web.create_app(tmp_path / "unused", allowed_source_ids=("site-a",),
                         timezones=("America/Monterrey", "UTC"))
    client = TestClient(app)
    default = client.get("/")
    assert default.status_code == 200
    assert "2026-10-30 16:18" in default.text
    assert "America/Monterrey" in default.text
    assert "20%" in default.text and "82%" in default.text
    assert "localStorage.setItem" in default.text
    assert 'id="timezone-choice"' in default.text
    utc = client.get("/?tz=UTC")
    assert "2026-10-30 22:18 UTC" in utc.text
    assert client.get("/?tz=Europe/Paris").status_code == 422
    assert client.get("/?source_id=site-b").status_code == 403
    assert client.get("/api/snapshot").json()["transactions"][0]["started_at"] == at


def test_dashboard_text_status_location_and_date_filters(monkeypatch, tmp_path):
    at = "2026-10-30T23:30:00Z"
    data = {
        "sources": [{"source_id": "node-a", "cursor": 1},
                    {"source_id": "node-b", "cursor": 2}],
        "connectors": [{"source_id": "node-a", "charger_id": "cp-a",
                        "location": "Audi Calzada", "connector_id": 1,
                        "status": "Available", "connected": True, "observed_at": at,
                        "power_w": None, "energy_wh": None},
                       {"source_id": "node-b", "charger_id": "cp-b",
                        "location": "Porsche", "connector_id": 1,
                        "status": "Faulted", "connected": False, "observed_at": at,
                        "power_w": None, "energy_wh": None}],
        "transactions": [],
        "events": [],
    }
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: data)
    client = TestClient(web.create_app(tmp_path / "unused", timezones=("America/Monterrey",)))
    filtered = client.get("/?filter=audi&location=calzada&status=available"
                          "&date_from=2026-10-30&date_to=2026-10-30&tz=America/Monterrey")
    assert filtered.status_code == 200
    assert "cp-a" in filtered.text and "cp-b" not in filtered.text
    not_matching = client.get("/?filter=audi&status=faulted")
    assert "Sin conectores registrados" in not_matching.text
    assert client.get("/?date_from=2026-10-31&date_to=2026-10-30").status_code == 422
    assert client.get("/?date_from=oops").status_code == 422
    assert "cp-b" in client.get("/api/snapshot").json()["connectors"][1]["charger_id"]


def test_group_repeated_events_by_source_charger_action():
    events = [
        {"source_id": "a", "charger_id": "cp", "kind": "ocpp", "action": "Heartbeat", "id": 8},
        {"source_id": "a", "charger_id": "cp", "kind": "ocpp", "action": "Heartbeat", "id": 7},
        {"source_id": "a", "charger_id": "cp", "kind": "status", "action": "StatusNotification",
         "status": "Available", "error_code": None, "connector_id": 1, "id": 6},
        {"source_id": "a", "charger_id": "cp", "kind": "status", "action": "StatusNotification",
         "status": "Available", "error_code": None, "connector_id": 1, "id": 5},
        {"source_id": "a", "charger_id": "cp", "kind": "status", "action": "StatusNotification",
         "status": "Faulted", "error_code": "InternalError", "connector_id": 1, "id": 4},
        {"source_id": "a", "charger_id": "cp", "kind": "status", "action": "StatusNotification",
         "status": "Faulted", "error_code": "InternalError", "connector_id": 1, "id": 3},
        {"source_id": "a", "charger_id": "cp", "kind": "transaction_started",
         "action": "StartTransaction", "id": 2},
        {"source_id": "b", "charger_id": "cp", "kind": "ocpp", "action": "Heartbeat", "id": 1},
    ]
    grouped = web._group_events(events)
    assert [len(g["items"]) for g in grouped] == [2, 2, 1, 1, 1, 1]
    assert grouped[0]["items"][0]["id"] == 8
    assert sum(len(g["items"]) for g in grouped) == len(events)


def test_invalid_instance_timezone_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="Invalid dashboard timezone"):
        web.create_app(tmp_path / "unused", timezones=("Not/A_Timezone",))


def test_transaction_date_filter_includes_session_overlapping_local_day():
    source = {"sources": [], "connectors": [], "events": [],
              "transactions": [{
                  "source_id": "node-a", "charger_id": "cp-1", "location": "Site A",
                  "id": 1, "state": "stopped",
                  "started_at": "2026-10-29T22:00:00-06:00",
                  "stopped_at": "2026-10-30T02:00:00-06:00",
              }]}
    result = web._filter_dashboard(source, term="", status="", location="",
                                   date_from="2026-10-30", date_to="2026-10-30",
                                   zone="America/Monterrey")
    assert len(result["transactions"]) == 1
    outside = web._filter_dashboard(source, term="", status="", location="",
                                    date_from="2026-10-31", date_to="2026-10-31",
                                    zone="America/Monterrey")
    assert outside["transactions"] == []


def test_transaction_energy_filter_defaults_to_positive_final_energy(monkeypatch, tmp_path):
    from copy import deepcopy

    rows = [
        {"source_id": "node-a", "charger_id": "cp", "location": "Site",
         "id": 21, "state": "stopped", "energy_wh": 10500},
        {"source_id": "node-a", "charger_id": "cp", "location": "Site",
         "id": 22, "state": "stopped", "energy_wh": 0},
        {"source_id": "node-a", "charger_id": "cp", "location": "Site",
         "id": 23, "state": "open", "active": True,
         "energy_wh": None, "live_energy_wh": 2500},
        {"source_id": "node-a", "charger_id": "cp", "location": "Site",
         "id": 24, "state": "stopped", "energy_wh": None},
    ]
    data = {"sources": [], "connectors": [], "events": [], "transactions": rows}
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(data))
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("node-a",)))
    success = client.get("/")
    assert success.status_code == 200
    assert ">21</td>" in success.text
    assert ">22</td>" not in success.text
    assert ">23</td>" not in success.text
    assert ">24</td>" not in success.text
    assert 'data-tx-view="success" aria-pressed="true"' in success.text
    assert 'data-tx-view="failed" aria-pressed="false"' in success.text
    assert 'name="tx_view" value="success"' in success.text
    for label in ("Exitosas (1)", "Fallidas (1)", "Todas (4)"):
        assert label in success.text
    failed = client.get("/?tx_view=failed")
    assert failed.status_code == 200
    assert ">22</td>" in failed.text
    assert ">21</td>" not in failed.text
    assert ">23</td>" not in failed.text
    assert 'data-tx-view="failed" aria-pressed="true"' in failed.text
    assert "Exitosas (1)" in failed.text and "Fallidas (1)" in failed.text
    assert "Todas (4)" in failed.text
    all_rows = client.get("/?tx_view=all")
    assert all_rows.status_code == 200
    assert all(f">{id}</td>" in all_rows.text for id in (21, 22, 23, 24))
    assert "Exitosas (1)" in all_rows.text and "Fallidas (1)" in all_rows.text
    assert "Todas (4)" in all_rows.text
    assert client.get("/?tx_view=other").status_code == 422
    api = client.get("/api/snapshot")
    assert api.status_code == 200
    assert [row["id"] for row in api.json()["transactions"]] == [21, 22, 23, 24]
    assert "next.searchParams.set(\"tx_view\", button.dataset.txView)" in success.text


def test_transaction_energy_filter_does_not_confuse_zero_with_missing():
    data = [
        {"energy_wh": 0}, {"energy_wh": 0.0}, {"energy_wh": 1},
        {"energy_wh": None}, {}, {"energy_wh": False},
        {"energy_wh": -1}, {"energy_wh": float("nan")},
    ]
    assert web._filter_transaction_energy(data, "failed") == data[:2]
    assert web._filter_transaction_energy(data, "success") == [data[2]]
    assert web._filter_transaction_energy(data, "all") is data
