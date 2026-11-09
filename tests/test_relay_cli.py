"""Relay config safety and idempotent restart tests."""
import os

import httpx
import pytest
from typer.testing import CliRunner

from ocpp_reports.relay_cli import app, load_config
from ocpp_reports.relay import relay_once
from ocpp_reports.replication.collector import Batch, accept_batch


def test_protected_config_and_explicit_sources(tmp_path):
    path = tmp_path / "relay.toml"
    path.write_text('[relay]\ndestination_id="001"\nendpoint="https://collector.test/api/replication"\n'
                    '[relay.sources]\n"local"="token-local"\n"002"="token-002"\n"005"="token-005"\n')
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="protected"):
        load_config(path)
    os.chmod(path, 0o600)
    assert sorted(load_config(path)["sources"]) == ["002", "005", "local"]
    result = CliRunner().invoke(app, ["check", "--config", str(path)])
    assert result.exit_code == 0
    assert "token-" not in result.stdout


def test_restart_replay_and_downstream_auth_denial(tmp_path):
    warehouse, state = tmp_path / "warehouse.sqlite", tmp_path / "relay.sqlite"
    accept_batch(warehouse, Batch.model_validate({
        "schema": "ocpp-reports/replication/v1", "source_id": "002",
        "after": 0, "next": 1, "more": False,
        "changes": [{"sequence": 1, "kind": "event",
                     "record_key": "a", "payload": {"value": 1}}]
    }))
    attempts = []

    def remote(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(401)
        body = __import__("json").loads(request.content)
        return httpx.Response(200, json={"source_id": "002", "ack": body["next"]})

    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            relay_once(warehouse, state, "002", "https://collector.test/api/replication",
                       "bad", destination_id="001", client=client)
        assert relay_once(warehouse, state, "002", "https://collector.test/api/replication",
                          "good", destination_id="001", client=client) == 1
    # New process reads persisted state, sends nothing twice.
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: pytest.fail("unexpected replay"))) as client:
        assert relay_once(warehouse, state, "002", "https://collector.test/api/replication",
                          "good", destination_id="001", client=client) == 1


def test_relay_config_rejects_symlink(tmp_path):
    path = tmp_path / "relay.toml"
    path.write_text('[relay]\ndestination_id="sink"\n'
                    'endpoint="https://collector.test/api/replication"\n'
                    '[relay.sources]\n"origin"="token"\n')
    os.chmod(path, 0o600)
    assert load_config(path)["sources"] == {"origin": "token"}
    link = tmp_path / "alias.toml"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="protected regular file"):
        load_config(link)


def test_local_bindings_must_be_unique_and_authorized(tmp_path):
    path = tmp_path / "relay.toml"
    base = ('[relay]\ndestination_id="sink"\n'
            'endpoint="https://collector.test/api/replication"\n'
            '[relay.sources]\n"origin-a"="token-a"\n"origin-b"="token-b"\n')
    for mapping in (
        '[relay.local_sources]\n"unknown"="internal"\n',
        '[relay.local_sources]\n"origin-a"="internal"\n"origin-b"="internal"\n',
        '[relay.local_sources]\n"origin-a"="  "\n',
    ):
        path.write_text(base + mapping)
        os.chmod(path, 0o600)
        with pytest.raises(ValueError, match="Local evidence"):
            load_config(path)
