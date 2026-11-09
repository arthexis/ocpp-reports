"""Contract checks for versioned dashboard read endpoints."""
from fastapi.testclient import TestClient
from ocpp_reports import web


def test_v1_routes_and_source_allowlist(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, *, source_id=None, charger_id=None, limit=100,
                      allowed_source_ids=None, **kwargs):
        calls.append((source_id, allowed_source_ids))
        return {
            "connectors": [{"source_id": "tenant-a", "charger_id": "CP1",
                            "connector_id": 1, "status": "Available", "location": "North"}],
            "transactions": [{"source_id": "tenant-a", "charger_id": "CP1",
                              "energy_wh": 1200}],
            "sources": [], "events": [],
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    app = web.create_app(tmp_path / "unused.sqlite", allowed_source_ids=("tenant-a",))
    client = TestClient(app)
    assert client.get("/api/v1/connectors").json()["items"][0]["charger_id"] == "CP1"
    assert client.get("/api/v1/stations").json()["items"][0]["charger_id"] == "CP1"
    assert client.get("/api/v1/filters").json()["source_ids"] == ["tenant-a"]
    assert client.get("/api/v1/transactions", params={"view": "success"}).status_code == 200
    assert client.get("/api/v1/summary").json()["status_counts"]["available"]["stations"] == 1
    assert client.get("/api/v1/openapi.json").json()["paths"]["/api/v1/connectors"]
    assert client.get("/api/v1/connectors", params={"source_id": "tenant-b"}).status_code == 403
    assert all(allowlist == ("tenant-a",) for _, allowlist in calls)
    assert client.post("/api/v1/connectors").status_code == 405
