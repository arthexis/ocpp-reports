"""Collector relay contract tests: source isolation, replay, and failure safety."""
import httpx
import pytest

from ocpp_reports.relay import relay_once, relay_page
from ocpp_reports.replication.collector import Batch, Change, accept_batch
from ocpp_reports.replication.forwarder import confirmed_cursor
import hashlib
import json


def seed(path, source, sequence, key):
    batch = Batch.model_validate({
        "schema": "ocpp-reports/replication/v1",
        "source_id": source, "after": 0, "next": sequence, "more": False,
        "changes": [{"sequence": sequence, "kind": "event",
                     "record_key": key, "payload": {"id": key}}],
    })
    accept_batch(path, batch)


def test_multisource_relay_keeps_identity_and_independent_ack(tmp_path):
    warehouse, state = tmp_path / "warehouse.sqlite", tmp_path / "relay.sqlite"
    seed(warehouse, "local", 1, "one")
    seed(warehouse, "002", 1, "two")
    seed(warehouse, "005", 1, "five")
    received = []

    def downstream(request):
        assert request.headers["authorization"] == "Bearer secret"
        body = __import__("json").loads(request.content)
        received.append(body)
        return httpx.Response(200, json={"source_id": body["source_id"], "ack": body["next"]})

    with httpx.Client(transport=httpx.MockTransport(downstream)) as client:
        for source in ("local", "002", "005"):
            assert relay_once(warehouse, state, source,
                              "https://collector.test/api/replication",
                              "secret", destination_id="001", client=client) == 1
        assert relay_once(warehouse, state, "002",
                          "https://collector.test/api/replication",
                          "secret", destination_id="001", client=client) == 1
    assert [r["source_id"] for r in received] == ["local", "002", "005"]
    assert all(len(r["changes"]) == 1 for r in received)


def test_relay_retries_after_network_error_and_bad_ack(tmp_path):
    warehouse, state = tmp_path / "warehouse.sqlite", tmp_path / "relay.sqlite"
    seed(warehouse, "002", 1, "one")
    key = json.dumps(["001", hashlib.sha256(b"https://collector.test/api/replication").hexdigest(), "002"], separators=(",", ":"))

    def offline(request):
        raise httpx.ConnectError("offline")

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        with pytest.raises(httpx.ConnectError):
            relay_once(warehouse, state, "002",
                       "https://collector.test/api/replication", "secret",
                       destination_id="001", client=client)
    assert confirmed_cursor(state, key) == 0

    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"source_id": "wrong", "ack": 1}))) as client:
        with pytest.raises(ValueError, match="acknowledgement"):
            relay_once(warehouse, state, "002",
                       "https://collector.test/api/replication", "secret",
                       destination_id="001", client=client)
    assert confirmed_cursor(state, key) == 0


def test_relay_rejects_unsafe_endpoint_and_self_identity(tmp_path):
    warehouse = tmp_path / "warehouse.sqlite"
    state = tmp_path / "relay.sqlite"
    seed(warehouse, "002", 1, "one")
    with pytest.raises(ValueError, match="HTTPS"):
        relay_once(warehouse, state, "002",
                   "http://10.0.0.1:8081/api/replication", "secret",
                   destination_id="001")
    with pytest.raises(ValueError, match="self-replication"):
        relay_once(warehouse, state, "002",
                   "https://collector.test/api/replication", "secret",
                   destination_id="002")
    assert not state.exists()
