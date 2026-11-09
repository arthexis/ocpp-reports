"""Contract checks for versioned dashboard read endpoints."""
from fastapi.testclient import TestClient
from ocpp_reports import web


def test_v1_routes_and_source_allowlist(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, *, source_id=None, charger_id=None, limit=100,
                      allowed_source_ids=None, **kwargs):
        calls.append((source_id, allowed_source_ids))
        return {
            "transaction_pagination": {"total": 1, "page": 1, "pages": 1,
                                       "counts": {"all": 1, "success": 1, "failed": 0}} if kwargs.get("transaction_pagination") else None,
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


def test_v1_connector_view_and_tenant_isolation(monkeypatch, tmp_path):
    def fake_snapshot(path, *, source_id=None, charger_id=None, limit=100,
                      allowed_source_ids=None, **kwargs):
        assert allowed_source_ids == ("tenant-a",)
        return {
            "connectors": [
                {"source_id": "tenant-a", "charger_id": "CP1",
                 "connector_id": 0, "status": "Available", "connected": 1},
                {"source_id": "tenant-a", "charger_id": "CP1",
                 "connector_id": 1, "status": "Charging", "connected": 1},
            ],
            "transactions": [], "sources": [], "events": [],
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite",
                                       allowed_source_ids=("tenant-a",)))
    combined = client.get("/api/v1/connectors").json()["items"]
    station = client.get("/api/v1/connectors",
                         params={"connector_view": "station"}).json()["items"]
    individual = client.get("/api/v1/connectors",
                            params={"connector_view": "individual"}).json()["items"]
    assert len(combined) == 2
    assert [row["connector_id"] for row in station] == [0]
    assert [row["connector_id"] for row in individual] == [1]
    assert all("stale" in row and "source_freshness" in row for row in combined)
    assert client.get("/api/v1/connectors",
                      params={"connector_view": "bad"}).status_code == 422
    assert client.get("/api/v1/connectors",
                      params={"source_id": "tenant-b"}).status_code == 403
