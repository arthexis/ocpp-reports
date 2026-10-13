import sqlite3
import pytest
from ocpp_reports.outbox import Outbox
from ocpp_reports.mailer import deliver_once


def test_enqueue_is_idempotent(tmp_path):
    outbox = Outbox(tmp_path / "mail.sqlite3")
    key = outbox.enqueue(recipient="a@example.org", subject="Report", body="hello", revision="r1")
    assert outbox.enqueue(recipient="a@example.org", subject="Report", body="hello", revision="r1") == key
    assert len(outbox.list()) == 1
    with pytest.raises(ValueError, match="collision"):
        outbox.enqueue(recipient="a@example.org", subject="Report", body="changed", revision="r1")


def test_claim_and_success(tmp_path):
    outbox = Outbox(tmp_path / "mail.sqlite3")
    outbox.enqueue(recipient="a@example.org", subject="Report", body="hello", revision="r1")
    seen = []
    assert deliver_once(outbox, send=lambda item: seen.append(item.key)) == seen[0]
    assert deliver_once(outbox, send=lambda item: None) is None
    assert outbox.list()[0]["state"] == "sent"


def test_ambiguous_send_requires_explicit_retry(tmp_path):
    path = tmp_path / "mail.sqlite3"
    outbox = Outbox(path)
    key = outbox.enqueue(recipient="a@example.org", subject="Report", body="hello", revision="r1")
    def broken(item):
        raise ConnectionError("connection dropped")
    with pytest.raises(ConnectionError):
        deliver_once(outbox, send=broken)
    assert outbox.list()[0]["state"] == "failed"
    assert deliver_once(outbox, send=lambda item: None) is None
    Outbox(path).retry(key)
    assert deliver_once(outbox, send=lambda item: None) == key


def test_crashed_sender_remains_sending(tmp_path):
    path = tmp_path / "mail.sqlite3"
    outbox = Outbox(path)
    outbox.enqueue(recipient="a@example.org", subject="Report", body="hello", revision="r1")
    assert outbox.claim()
    assert Outbox(path).claim() is None
    assert Outbox(path).list()[0]["state"] == "sending"
