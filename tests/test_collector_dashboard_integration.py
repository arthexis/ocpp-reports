"""Two-source collector integration: local vendor plus a forwarded satellite."""
from fastapi.testclient import TestClient

from ocpp_reports.collector import create_collector
from ocpp_reports.follow import sync_page
from ocpp_reports.web import create_app


def local_page():
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "customer-vendor", "exported_at": "2026-10-24T12:00:00Z",
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "vendor", "connected": True,
            "connectors": [{"id": 1, "status": "Available", "transaction": None}]}]}
    }}


def test_local_and_forwarded_chargers_share_dashboard_without_identity_collision(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    sync_page(database, local_page())
    collector = TestClient(create_collector(database, {"source-b": "token-005"}))
    changes = [
        {"sequence": 1, "kind": "connector", "record_key": "etron:1",
         "payload": {"charger_id": "etron", "connector_id": 1,
                     "status": "Charging", "connected": 1,
                     "observed_at": "2026-10-24T12:00:00Z"}},
        {"sequence": 2, "kind": "transaction", "record_key": "etron:1",
         "payload": {"id": 1, "charger_id": "etron", "connector_id": 1,
                     "started_at": "2026-10-24T11:30:00Z", "energy_wh": 1200}},
        {"sequence": 3, "kind": "event", "record_key": "1",
         "payload": {"id": 1, "charger_id": "etron", "action": "StartTransaction"}},
    ]
    batch = {"schema": "ocpp-reports/replication/v1", "source_id": "source-b",
             "after": 0, "next": 3, "more": False, "changes": changes}
    headers = {"Authorization": "Bearer token-005"}
    assert collector.post("/api/replication", json=batch, headers=headers).json()["ack"] == 3
    assert collector.post("/api/replication", json=batch, headers=headers).json()["ack"] == 3
    web = TestClient(create_app(database))
    response = web.get("/api/snapshot")
    assert response.status_code == 200
    data = response.json()
    assert {s["source_id"] for s in data["sources"]} == {"customer-vendor", "source-b"}
    assert {(c["source_id"], c["charger_id"]) for c in data["connectors"]} == {
        ("customer-vendor", "vendor"), ("source-b", "etron")}
    assert len([t for t in data["transactions"] if t["source_id"] == "source-b"]) == 1
    assert len([e for e in data["events"] if e["source_id"] == "source-b"]) == 1
    assert len(web.get("/api/snapshot?source_id=source-b").json()["connectors"]) == 1
    assert web.get("/api/snapshot?source_id=source-b&charger_id=vendor").json()["connectors"] == []
    remote_connector = next(c for c in data["connectors"] if c["source_id"] == "source-b")
    assert remote_connector["power_w"] is None
    assert remote_connector["energy_wh"] is None
    assert remote_connector["synced_at"] is None
    assert "etron" in web.get("/").text
    assert "—" in web.get("/").text
