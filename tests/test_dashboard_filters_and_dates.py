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
    assert page.text.count("2026-10-30 22:18 UTC") == 7
    assert at not in page.text
    assert client.get("/api/snapshot").json()["connectors"][0]["observed_at"] == at
    assert client.get("/?source_id=other").status_code == 403
    assert 'id="dashboard-filters"' in page.text
    assert 'name="source_id"' in page.text and 'name="charger_id"' in page.text
    assert 'type="submit"' not in page.text
