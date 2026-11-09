"""Generic local-source binding and relay identity regression tests."""
import sqlite3

import httpx
import pytest

from ocpp_reports.relay import relay_once, relay_page
from ocpp_reports.replication.forwarder import confirmed_cursor


def test_local_changefeed_uses_explicit_source_binding(tmp_path):
    warehouse = tmp_path / "warehouse.sqlite"
    state = tmp_path / "relay.sqlite"
    with sqlite3.connect(warehouse) as db:
        db.execute("CREATE TABLE replication_changes (source_id TEXT, sequence INTEGER, kind TEXT, record_key TEXT, payload TEXT)")
        db.execute("INSERT INTO replication_changes VALUES (?,?,?,?,?)",
                   ("csms-internal-uuid", 1, "event", "key", '{"ok":true}'))
    page = relay_page(warehouse, "public-origin", local_evidence_source="csms-internal-uuid")
    assert page["source_id"] == "public-origin"
    assert page["changes"][0]["record_key"] == "key"
    received = []

    def handler(request):
        data = __import__("json").loads(request.content)
        received.append(data)
        return httpx.Response(200, json={"source_id": data["source_id"], "ack": data["next"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        args = (warehouse, state, "public-origin", "https://receiver.test/api/replication", "secret")
        kwargs = dict(destination_id="receiver", local_evidence_source="csms-internal-uuid", client=client)
        assert relay_once(*args, **kwargs) == 1
        assert relay_once(*args, **kwargs) == 1
        with pytest.raises(ValueError, match="identity changed"):
            relay_once(*args, **{**kwargs, "local_evidence_source": "different-uuid"})
    assert len(received) == 1
