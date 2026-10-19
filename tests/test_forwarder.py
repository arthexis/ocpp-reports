"""Forwarder retry and durable cursor contract tests."""
import httpx
import pytest

from ocpp_reports.collector import create_collector
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


def test_forwarder_acknowledges_and_resumes(tmp_path):
    warehouse = tmp_path / "warehouse.sqlite"
    collector = tmp_path / "collector.sqlite"
    state = tmp_path / "cursor.sqlite"
    sync_page(warehouse, page())
    app = create_collector(collector, {"sat-a": "secret"})
    with httpx.Client(transport=httpx.ASGITransport(app=app)) as client:
        pass


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
