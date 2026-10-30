"""Source-scoped readiness failures must be machine-readable and safe."""
import json
import ssl

import httpx
import pytest
from typer.testing import CliRunner

from ocpp_reports import forwarder_config as readiness


def _config():
    return {"source_id": "node-a", "endpoint": "https://collector.example.test/api/replication",
            "token": "do-not-print-this"}


def _client(monkeypatch, response=None, exc=None):
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["timeout"] == 10

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, endpoint, json, headers):
            assert endpoint.endswith("/api/replication/ready")
            assert json == {"source_id": "node-a"}
            assert headers["Authorization"] == "Bearer do-not-print-this"
            if exc:
                raise exc
            return response

    monkeypatch.setattr(httpx, "Client", FakeClient)


@pytest.mark.parametrize(("status", "reason"), [
    (401, "collector_unauthorized"), (403, "collector_unauthorized"),
    (404, "collector_unsupported"), (405, "collector_unsupported"),
    (503, "collector_not_ready"), (502, "collector_http_error"),
])
def test_http_failures_are_redacted_and_classified(monkeypatch, status, reason):
    _client(monkeypatch, response=httpx.Response(status))
    result = readiness.check_collector(_config())
    assert result == {"ready": False, "status": "blocked",
                      "reason": reason, "source_id": "node-a"}
    assert "do-not-print-this" not in str(result)


def test_tls_and_connection_refused_are_distinct(monkeypatch):
    error = httpx.ConnectError("sensitive endpoint", request=httpx.Request("POST", "https://collector.example.test"))
    error.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    _client(monkeypatch, exc=error)
    assert readiness.check_collector(_config())["reason"] == "collector_tls_failure"
    _client(monkeypatch, exc=httpx.ConnectError("connection refused"))
    assert readiness.check_collector(_config())["reason"] == "collector_unreachable"


def test_ready_requires_matching_source_and_schema(monkeypatch):
    good = {"schema": "ocpp-reports/readiness/v1", "source_id": "node-a",
            "ready": True, "held": False, "cursor": 0}
    _client(monkeypatch, response=httpx.Response(200, json=good))
    assert readiness.check_collector(_config())["ready"] is True
    for altered, reason in [
        (dict(good, source_id="node-b"), "collector_invalid_response"),
        (dict(good, schema="legacy"), "collector_unsupported"),
        (dict(good, cursor=True), "collector_invalid_response"),
        (dict(good, ready=False), "collector_invalid_response"),
    ]:
        _client(monkeypatch, response=httpx.Response(200, json=altered))
        assert readiness.check_collector(_config())["reason"] == reason


def test_json_cli_emits_one_object_on_failure(monkeypatch):
    monkeypatch.setattr(readiness, "load_forwarder_config", lambda path: _config())
    monkeypatch.setattr(readiness, "check_collector", lambda cfg: {
        "ready": False, "status": "blocked", "reason": "collector_unreachable",
        "source_id": cfg["source_id"],
    })
    result = CliRunner().invoke(readiness.app, ["check", "--remote", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "collector_unreachable"
    assert "do-not-print-this" not in result.output


def test_json_cli_invalid_config_is_structured(monkeypatch):
    monkeypatch.setattr(readiness, "load_forwarder_config",
                        lambda path: (_ for _ in ()).throw(ValueError("contains token secret")))
    result = CliRunner().invoke(readiness.app, ["check", "--remote", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "ready": False, "status": "blocked", "reason": "invalid_configuration",
    }
    assert "secret" not in result.output
