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
        assert body["source_id"] == "gway-005"
        assert body["changes"]
        return httpx.Response(200, json={"source_id": "gway-005", "ack": body["next"]})

    with httpx.Client(transport=httpx.MockTransport(received)) as client:
        assert forward_once(warehouse, state, "gway-005",
                            "https://collector.test/api/replication",
                            "secret", client=client) == 1
    assert confirmed_cursor(state, "gway-005") == 1
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
        forward_once(warehouse, state, "gway-005",
                     "https://collector.test/api/replication", "secret")
    assert not state.exists()
