"""Forwarder retry and durable cursor contract tests."""
import httpx
import pytest

from ocpp_reports.follow import sync_page
from ocpp_reports.forwarder import confirmed_cursor, forward_once


def page():
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "sat-a", "exported_at": "2026-10-19T00:00:00Z",
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "cp-1", "connected": True,
            "connectors": [{"id": 1, "status": "Available", "transaction": None}]}]}
    }}


def test_forwarder_retries_without_advancing_cursor(tmp_path):
    warehouse = tmp_path / "warehouse.sqlite"
    state = tmp_path / "cursor.sqlite"
    sync_page(warehouse, page())

    def failed(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            forward_once(warehouse, state, "sat-a", "https://collector.test/api/replication",
                         "secret", client=client)
    assert confirmed_cursor(state, "sat-a") == 0

    def success(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"schema": "ocpp-reports/status/v1",
                "source_id": "sat-a", "generation": "collector-one",
                "cursor": 1, "held": False, "has_evidence": True})
        return httpx.Response(200, json={"source_id": "sat-a", "ack": 1})

    with httpx.Client(transport=httpx.MockTransport(success)) as client:
        assert forward_once(warehouse, state, "sat-a", "https://collector.test/api/replication",
                            "secret", client=client) == 1
        assert forward_once(warehouse, state, "sat-a", "https://collector.test/api/replication",
                            "secret", client=client) == 1
    assert confirmed_cursor(state, "sat-a") == 1


def test_forwarder_uses_node_name_with_distinct_local_evidence_uuid(tmp_path):
    from ocpp_reports.forwarder import local_evidence_source
    warehouse = tmp_path / "warehouse.sqlite"
    state = tmp_path / "forwarder.sqlite"
    source_page = page()
    source_page["data"]["source_id"] = "b0bcb9b6-6342-44f8-b36a-eabb675c961b"
    sync_page(warehouse, source_page)
    assert local_evidence_source(warehouse) == source_page["data"]["source_id"]

    def received(request):
        import json
        body = json.loads(request.content)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"schema": "ocpp-reports/status/v1",
                "source_id": "source-b", "generation": "collector-one",
                "cursor": 1, "held": False, "has_evidence": True})
        assert body["source_id"] == "source-b"
        assert body["changes"]
        return httpx.Response(200, json={"source_id": "source-b", "ack": body["next"]})

    with httpx.Client(transport=httpx.MockTransport(received)) as client:
        assert forward_once(warehouse, state, "source-b",
                            "https://collector.test/api/replication",
                            "secret", client=client) == 1
    assert confirmed_cursor(state, "source-b") == 1
    assert confirmed_cursor(state, source_page["data"]["source_id"]) == 0


def test_forwarder_refuses_ambiguous_local_sources(tmp_path):
    from ocpp_reports.forwarder import local_evidence_source
    warehouse = tmp_path / "warehouse.sqlite"
    state = tmp_path / "forwarder.sqlite"
    sync_page(warehouse, page())
    second = page()
    second["data"]["source_id"] = "second-source"
    sync_page(warehouse, second)
    with pytest.raises(ValueError, match="exactly one"):
        local_evidence_source(warehouse)
    with pytest.raises(ValueError, match="exactly one"):
        forward_once(warehouse, state, "source-b",
                     "https://collector.test/api/replication", "secret")
    assert not state.exists()



def test_idle_probe_rejects_wrong_source_and_generation_change(tmp_path):
    from ocpp_reports.replication.forwarder import collector_status, save_cursor
    state = tmp_path / "state.sqlite"
    def handler(request):
        return httpx.Response(200, json={
            "schema": "ocpp-reports/status/v1", "source_id": "node-a",
            "generation": "first", "cursor": 0, "held": False, "has_evidence": False,
        })
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert collector_status(state, "node-a", "https://example.test/api/replication",
                                "token", client)["generation"] == "first"
    def replaced(request):
        return httpx.Response(200, json={
            "schema": "ocpp-reports/status/v1", "source_id": "node-a",
            "generation": "second", "cursor": 0, "held": False, "has_evidence": False,
        })
    with httpx.Client(transport=httpx.MockTransport(replaced)) as client:
        with pytest.raises(ValueError, match="generation changed"):
            collector_status(state, "node-a", "https://example.test/api/replication",
                             "token", client)
    assert not (tmp_path / "missing.sqlite").exists()
    save_cursor(state, "node-a", 12)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert collector_status(state, "node-a", "https://example.test/api/replication",
                                "token", client)["generation"] == "first"



@pytest.mark.parametrize("response", [
    {"schema": "ocpp-reports/status/v1", "source_id": "other",
     "generation": "first", "cursor": 0, "held": False, "has_evidence": False},
    {"schema": "ocpp-reports/status/v1", "source_id": "node-a",
     "generation": "first", "cursor": True, "held": False, "has_evidence": False},
    {"schema": "ocpp-reports/status/v1", "source_id": "node-a",
     "generation": "first", "cursor": 0, "held": "false", "has_evidence": False},
    {"schema": "ocpp-reports/status/v1", "source_id": "node-a",
     "generation": "", "cursor": 0, "held": False, "has_evidence": False},
])
def test_collector_status_rejects_invalid_or_cross_source_receipts(tmp_path, response):
    from ocpp_reports.replication.forwarder import collector_status
    state = tmp_path / "state.sqlite"
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)
    )) as client:
        with pytest.raises(ValueError, match="Invalid authenticated"):
            collector_status(state, "node-a", "https://example.test/api/replication",
                             "secret", client)
    assert not state.exists()


def test_collector_status_refuses_unbound_ack_regression(tmp_path):
    from ocpp_reports.replication.forwarder import collector_status, save_cursor
    state = tmp_path / "state.sqlite"
    save_cursor(state, "node-a", 77)
    receipt = {"schema": "ocpp-reports/status/v1", "source_id": "node-a",
               "generation": "new", "cursor": 0,
               "held": False, "has_evidence": False}
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=receipt)
    )) as client:
        with pytest.raises(ValueError, match="older Collector"):
            collector_status(state, "node-a", "https://example.test/api/replication",
                             "secret", client)
    assert confirmed_cursor(state, "node-a") == 77



def test_generic_recovery_rebuilds_and_keeps_normal_ack(tmp_path):
    import json
    import sqlite3
    from fastapi.testclient import TestClient
    from ocpp_reports.replication.collector import create_collector
    from ocpp_reports.replication.forwarder import save_cursor
    warehouse, state, collector = (tmp_path / x for x in
                                    ("local.sqlite", "state.sqlite", "collector.sqlite"))
    sync_page(warehouse, page())
    save_cursor(state, "sat-a", 1)
    receiver = TestClient(create_collector(collector, {"sat-a": "secret"}))
    def relay(request):
        reply = receiver.post(request.url.path, json=json.loads(request.content),
                              headers={"Authorization": request.headers["Authorization"]})
        return httpx.Response(reply.status_code, json=reply.json())
    with httpx.Client(transport=httpx.MockTransport(relay)) as client:
        for _ in range(4):
            assert forward_once(warehouse, state, "sat-a",
                                "https://collector.test/api/replication",
                                "secret", client=client) == 1
    with sqlite3.connect(collector) as db:
        assert db.execute("SELECT sequence FROM collector_cursors WHERE source_id='sat-a'").fetchone() == (1,)
        assert db.execute("SELECT state FROM collector_recovery WHERE source_id='sat-a'").fetchone() == ("complete",)
    assert confirmed_cursor(state, "sat-a") == 1


def test_recovery_fences_normal_writers_and_rejects_wrong_generation(tmp_path):
    from fastapi.testclient import TestClient
    from ocpp_reports.replication.collector import create_collector
    with TestClient(create_collector(tmp_path / "collector.sqlite", {"a": "secret"})) as client:
        auth = {"Authorization": "Bearer secret"}
        status = client.post("/api/replication/status",
                             json={"source_id": "a", "forward_ack": 12}, headers=auth).json()
        generation = status["generation"]
        assert client.post("/api/replication/recovery/start",
                           json={"source_id": "a", "generation": "wrong"}, headers=auth).status_code == 409
        start = client.post("/api/replication/recovery/start",
                            json={"source_id": "a", "generation": generation}, headers=auth)
        assert start.status_code == 200
        session = start.json()["session_id"]
        assert client.post("/api/replication/recovery/start",
                           json={"source_id": "a", "generation": generation,
                                 "session_id": session}, headers=auth).json()["session_id"] == session
        assert client.post("/api/replication", json={
            "schema": "ocpp-reports/replication/v1", "source_id": "a",
            "after": 0, "next": 1, "more": False,
            "changes": [{"sequence": 1, "kind": "event",
                         "record_key": "one", "payload": {}}]
        }, headers=auth).status_code == 503
        assert client.post("/api/replication/recovery/finish",
                           json={"source_id": "a", "generation": generation,
                                 "session_id": "wrong", "cursor": 0}, headers=auth).status_code == 409
