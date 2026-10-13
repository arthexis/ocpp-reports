"""R3C1 offline SMTP failure, ambiguity and operator-controlled recovery."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import pytest
from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.outbox import Outbox

START = "2026-10-12T00:00:00+00:00"
END = "2026-10-13T00:00:00+00:00"
OPERATOR = "operator@example.org"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """All network activity is replaced by a controllable in-memory SMTP fake."""
    db = tmp_path / "outbox.sqlite3"
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(db))
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", OPERATOR)
    monkeypatch.setenv("OCPP_REPORTS_SMTP_HOST", "offline.invalid")
    monkeypatch.setenv("OCPP_REPORTS_SMTP_FROM", "reports@example.org")
    monkeypatch.setenv("OCPP_REPORTS_SMTP_TLS", "starttls")
    state = {"mode": "ok", "accepted": [], "attempts": 0}

    class SMTP:
        def __init__(self, host, port, timeout=None, **kwargs):
            assert (host, port) == ("offline.invalid", 587)
            state["attempts"] += 1
            if state["mode"] == "connect":
                raise ConnectionError("offline pre-acceptance connection failure")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self, context=None):
            pass
        def login(self, username, password):
            raise AssertionError("Unexpected SMTP authentication")
        def send_message(self, message):
            assert isinstance(message, EmailMessage)
            if state["mode"] == "reject":
                raise RuntimeError("SMTP rejected message")
            state["accepted"].append(message)
            if state["mode"] == "disconnect":
                raise ConnectionError("connection lost after acceptance")

    monkeypatch.setattr("ocpp_reports.mailer.smtplib.SMTP", SMTP)
    source = tmp_path / "summary.json"
    source.write_text(json.dumps({
        "schema": "ocpp-reports/summary/v1", "source_id": "fixture-csms",
        "period": {"since": START, "until": END, "timezone": "UTC"},
        "summary": {"transactions": 1, "known_energy_wh": 1200,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [{"id": 1, "charger_id": "CP-A", "connector_id": 1,
                          "state": "completed", "energy_wh": 1200}],
        "revision": "r3c1-rev-1",
    }), encoding="utf-8")
    return {"db": db, "state": state, "source": source, "runner": CliRunner()}


def invoke(scenario, *args):
    return scenario["runner"].invoke(app, list(args))


def run(scenario):
    return invoke(scenario, "run", "--source", str(scenario["source"]),
                  "--since", START, "--until", END, "--send")


@pytest.mark.parametrize("mode,accepted", [
    ("connect", 0),
    ("reject", 0),
    ("disconnect", 1),
])
def test_failure_is_persistent_and_never_automatically_resent(scenario, mode, accepted):
    state = scenario["state"]
    state["mode"] = mode
    first = run(scenario)
    assert first.exit_code == 1, first.output
    records = Outbox(scenario["db"]).list()
    assert len(records) == 1
    record = records[0]
    assert record["state"] == "failed"
    assert record["attempts"] == 1
    assert record["last_error"]
    assert len(state["accepted"]) == accepted
    assert state["attempts"] == 1

    state["mode"] = "ok"
    again = run(scenario)
    assert again.exit_code == 0, again.output
    assert "no automatic retry" in again.output
    assert state["attempts"] == 1
    assert len(state["accepted"]) == accepted
    assert Outbox(scenario["db"]).list()[0]["state"] == "failed"


@pytest.mark.parametrize("mode,prior_acceptance", [
    ("reject", 0),
    ("disconnect", 1),
])
def test_explicit_retry_requires_operator_and_sends_once(scenario, mode, prior_acceptance):
    state = scenario["state"]
    state["mode"] = mode
    assert run(scenario).exit_code == 1
    record = Outbox(scenario["db"]).list()[0]
    key = record["key"]
    state["mode"] = "ok"
    assert invoke(scenario, "mail", "send", "--key", key).exit_code == 0
    # A failed delivery cannot be sent until the operator explicitly requeues it.
    assert state["attempts"] == 1
    retry = invoke(scenario, "mail", "retry", key)
    assert retry.exit_code == 0, retry.output
    assert Outbox(scenario["db"]).list()[0]["state"] == "pending"
    sent = invoke(scenario, "mail", "send", "--key", key)
    assert sent.exit_code == 0, sent.output
    assert state["attempts"] == 2
    assert len(state["accepted"]) == prior_acceptance + 1
    record = Outbox(scenario["db"]).list()[0]
    assert record["state"] == "sent" and record["attempts"] == 2
    assert invoke(scenario, "mail", "retry", key).exit_code == 2
    assert run(scenario).exit_code == 0
    assert state["attempts"] == 2


def test_manual_retry_after_pre_acceptance_failure(scenario):
    state = scenario["state"]
    state["mode"] = "connect"
    assert run(scenario).exit_code == 1
    key = Outbox(scenario["db"]).list()[0]["key"]
    state["mode"] = "ok"
    retry = invoke(scenario, "mail", "retry", key)
    assert retry.exit_code == 0, retry.output
    assert invoke(scenario, "mail", "send", "--key", key).exit_code == 0
    assert state["attempts"] == 2
    assert len(state["accepted"]) == 1


def test_restart_after_queue_and_after_success(scenario):
    """Pending and sent state survive opening the database in a new process."""
    outbox = Outbox(scenario["db"])
    key = outbox.enqueue(recipient=OPERATOR, subject="report",
                         body="immutable body", revision="revision-1")
    assert Outbox(scenario["db"]).list()[0]["state"] == "pending"
    scenario["state"]["mode"] = "ok"
    result = invoke(scenario, "mail", "send", "--key", key)
    assert result.exit_code == 0, result.output
    restarted = Outbox(scenario["db"])
    assert restarted.list()[0]["state"] == "sent"
    assert restarted.list()[0]["attempts"] == 1
    assert invoke(scenario, "mail", "send", "--key", key).exit_code == 0
    assert scenario["state"]["attempts"] == 1


def test_interrupted_claim_survives_restart_and_requires_manual_retry(scenario):
    outbox = Outbox(scenario["db"])
    key = outbox.enqueue(recipient=OPERATOR, subject="report",
                         body="immutable body", revision="revision-1")
    claimed = outbox.claim(key)
    assert claimed is not None and claimed.key == key
    restarted = Outbox(scenario["db"])
    assert restarted.list()[0]["state"] == "sending"
    assert restarted.claim(key) is None
    assert invoke(scenario, "mail", "send", "--key", key).exit_code == 0
    assert scenario["state"]["attempts"] == 0
    retry = invoke(scenario, "mail", "retry", key)
    assert retry.exit_code == 0, retry.output
    sent = invoke(scenario, "mail", "send", "--key", key)
    assert sent.exit_code == 0, sent.output
    assert Outbox(scenario["db"]).list()[0]["state"] == "sent"
    assert Outbox(scenario["db"]).list()[0]["attempts"] == 2
    assert scenario["state"]["attempts"] == 1


def test_concurrent_claims_only_one_worker_gets_delivery(scenario):
    outbox = Outbox(scenario["db"])
    key = outbox.enqueue(recipient=OPERATOR, subject="report",
                         body="immutable body", revision="revision-1")
    def claim(_):
        delivery = Outbox(scenario["db"]).claim(key)
        return delivery.key if delivery else None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))
    assert results.count(key) == 1
    assert results.count(None) == 15
    record = Outbox(scenario["db"]).list()[0]
    assert record["state"] == "sending" and record["attempts"] == 1


def test_concurrent_period_reservations_remain_immutable(scenario):
    period = "operator|fixture-csms|2026-10-12|2026-10-13"
    def reserve(_):
        return Outbox(scenario["db"]).enqueue_period(
            period_key=period, recipient=OPERATOR, subject="daily report",
            body="original body", revision="revision-1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(16)))
    key = results[0][0]
    assert len({item[0] for item in results}) == 1
    assert sum(created for _, created in results) == 1
    assert Outbox(scenario["db"]).period_status(period)["delivery_key"] == key
    assert len(Outbox(scenario["db"]).list()) == 1
    revised_key, created = Outbox(scenario["db"]).enqueue_period(
        period_key=period, recipient=OPERATOR, subject="revised report",
        body="revised body", revision="revision-2")
    assert (revised_key, created) == (key, False)
    assert Outbox(scenario["db"]).list()[0]["body"] == "original body"


def test_revision_change_after_interrupted_scheduled_delivery(scenario):
    first = run(scenario)
    assert first.exit_code == 0, first.output
    outbox = Outbox(scenario["db"])
    original = outbox.list()[0]
    document = json.loads(scenario["source"].read_text(encoding="utf-8"))
    document["revision"] = "r3c2-revision-2"
    document["transactions"][0]["energy_wh"] = 1300
    document["summary"]["known_energy_wh"] = 1300
    scenario["source"].write_text(json.dumps(document), encoding="utf-8")
    again = run(scenario)
    assert again.exit_code == 0, again.output
    assert "no automatic retry" in again.output
    records = Outbox(scenario["db"]).list()
    assert len(records) == 1
    assert records[0]["key"] == original["key"]
    assert records[0]["body"] == original["body"]
    assert scenario["state"]["attempts"] == 1
