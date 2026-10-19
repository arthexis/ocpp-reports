"""Collector contract tests: authentication, replay, isolation and atomicity."""
import sqlite3

from fastapi.testclient import TestClient

from ocpp_reports.collector import create_collector


def batch(source="sat-a", after=0, sequence=1, status="Available"):
    return {"schema": "ocpp-reports/replication/v1", "source_id": source,
            "after": after, "next": sequence, "more": False,
            "changes": [{"sequence": sequence, "kind": "connector",
                         "record_key": "cp-1:1",
                         "payload": {"charger_id": "cp-1", "status": status}}]}


def test_collector_requires_source_credentials_and_isolates_sources(tmp_path):
    db = tmp_path / "collector.sqlite"
    client = TestClient(create_collector(db, {"sat-a": "token-a", "sat-b": "token-b"}))
    data = batch()
    assert client.post("/api/replication", json=data).status_code == 401
    assert client.post("/api/replication", json=data,
                       headers={"Authorization": "Bearer token-b"}).status_code == 401
    headers = {"Authorization": "Bearer token-a"}
    response = client.post("/api/replication", json=data, headers=headers)
    assert response.status_code == 200
    assert response.json()["ack"] == 1
    assert client.post("/api/replication", json=data, headers=headers).status_code == 200
    assert client.post("/api/replication", json=batch(status="Faulted"),
                       headers=headers).status_code == 409
    assert client.post("/api/replication", json=batch(after=2, sequence=3),
                       headers=headers).status_code == 409
    assert client.post("/api/replication", json=batch(source="sat-b"),
                       headers={"Authorization": "Bearer token-b"}).status_code == 200
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collector_changes").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM collector_latest").fetchone()[0] == 2


def test_collector_rejects_invalid_order_without_partial_commit(tmp_path):
    db = tmp_path / "collector.sqlite"
    client = TestClient(create_collector(db, {"sat-a": "token-a"}))
    data = batch()
    data["changes"].append({"sequence": 1, "kind": "event",
                            "record_key": "1", "payload": {"id": 1}})
    assert client.post("/api/replication", json=data,
                       headers={"Authorization": "Bearer token-a"}).status_code == 409
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collector_changes").fetchone()[0] == 0
