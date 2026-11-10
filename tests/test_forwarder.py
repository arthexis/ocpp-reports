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
