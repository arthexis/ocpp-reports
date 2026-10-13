"""End-to-end outbox selection and service configuration regression tests."""
from pathlib import Path
from ocpp_reports.mailer import deliver_once
from ocpp_reports.outbox import Outbox


def test_targeted_delivery_never_sends_older_pending(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    first = outbox.enqueue(recipient="operator@example.org", subject="older", body="old", revision="1")
    second = outbox.enqueue(recipient="operator@example.org", subject="current", body="new", revision="2")
    sent = []
    assert deliver_once(outbox, key=second, send=lambda delivery, **kwargs: sent.append(delivery.key)) == second
    assert sent == [second]
    records = {record["key"]: record for record in outbox.list()}
    assert records[first]["state"] == "pending"
    assert records[second]["state"] == "sent"
    assert deliver_once(outbox, key=second, send=lambda delivery, **kwargs: sent.append(delivery.key)) is None


def test_targeted_failed_delivery_stays_failed(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    key = outbox.enqueue(recipient="operator@example.org", subject="report", body="body", revision="r")
    def fail(delivery, **kwargs):
        raise ConnectionError("ambiguous SMTP disconnect")
    try:
        deliver_once(outbox, key=key, send=fail)
    except ConnectionError:
        pass
    else:
        raise AssertionError("SMTP error must propagate")
    assert outbox.list()[0]["state"] == "failed"
    assert deliver_once(outbox, key=key, send=lambda delivery, **kwargs: None) is None


def test_systemd_service_uses_unified_run_and_writable_state():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/ocpp-reports.service").read_text()
    assert "ocpp-reports run " in service
    assert "scheduled-run" not in service
    assert "OCPP_REPORTS_OUTBOX=/var/lib/ocpp-reports/outbox.sqlite3" in service
    assert "StateDirectory=ocpp-reports" in service
    assert "--send" in service
