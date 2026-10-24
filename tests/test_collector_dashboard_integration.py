"""Two-source collector integration: local ABB plus a forwarded satellite."""
from fastapi.testclient import TestClient

from ocpp_reports.collector import create_collector
from ocpp_reports.follow import sync_page
from ocpp_reports.web import create_app


def local_page():
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "porsche-abb", "exported_at": "2026-10-24T12:00:00Z",
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "abb", "connected": True,
            "connectors": [{"id": 1, "status": "Available", "transaction": None}]}]}
    }}


def test_local_and_forwarded_chargers_share_dashboard_without_identity_collision(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    sync_page(database, local_page())
    collector = TestClient(create_collector(database, {"gway-005": "token-005"}))
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
    batch = {"schema": "ocpp-reports/replication/v1", "source_id": "gway-005",
             "after": 0, "next": 3, "more": False, "changes": changes}
    headers = {"Authorization": "Bearer token-005"}
    assert collector.post("/api/replication", json=batch, headers=headers).json()["ack"] == 3
    assert collector.post("/api/replication", json=batch, headers=headers).json()["ack"] == 3
    web = TestClient(create_app(database))
    response = web.get("/api/snapshot")
    assert response.status_code == 200
    data = response.json()
    assert {s["source_id"] for s in data["sources"]} == {"porsche-abb", "gway-005"}
    assert {(c["source_id"], c["charger_id"]) for c in data["connectors"]} == {
        ("porsche-abb", "abb"), ("gway-005", "etron")}
    assert len([t for t in data["transactions"] if t["source_id"] == "gway-005"]) == 1
    assert len([e for e in data["events"] if e["source_id"] == "gway-005"]) == 1
    assert len(web.get("/api/snapshot?source_id=gway-005").json()["connectors"]) == 1
    assert web.get("/api/snapshot?source_id=gway-005&charger_id=abb").json()["connectors"] == []
    assert "etron" in web.get("/").text
