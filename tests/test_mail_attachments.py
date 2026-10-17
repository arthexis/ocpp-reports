import hashlib
from email.message import EmailMessage

import pytest
from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.outbox import Outbox
from ocpp_reports.mailer import deliver_once

def test_attachment_snapshot_and_idempotency(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    key = box.enqueue_attachment(recipient="me@example.org", subject="Report",
                                 body="Attached", filename="report.csv", content=b"a,b\n1,2\n")
    assert box.enqueue_attachment(recipient="me@example.org", subject="Report",
                                  body="Attached", filename="report.csv", content=b"a,b\n1,2\n") == key
    delivery = box.claim(key)
    assert delivery.attachment_path.read_bytes() == b"a,b\n1,2\n"
    assert delivery.attachment_sha256 == hashlib.sha256(b"a,b\n1,2\n").hexdigest()

def test_smtp_mime_attachment(tmp_path, monkeypatch):
    from ocpp_reports import mailer
    box = Outbox(tmp_path / "outbox.sqlite3")
    key = box.enqueue_attachment(recipient="me@example.org", subject="Report",
                                 body="Attached", filename="report.csv", content=b"a,b\n1,2\n")
    accepted = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self, **kwargs): pass
        def send_message(self, message): accepted.append(message)
    monkeypatch.setattr(mailer.smtplib, "SMTP", SMTP)
    assert deliver_once(box, host="smtp.invalid", port=587, sender="sender@example.org", key=key) == key
    message = accepted[0]
    assert isinstance(message, EmailMessage)
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "report.csv"
    assert attachments[0].get_payload(decode=True) == b"a,b\n1,2\n"

def test_cli_send_file_does_not_resend(tmp_path, monkeypatch):
    from ocpp_reports import mailer
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("OCPP_REPORTS_SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("OCPP_REPORTS_SMTP_FROM", "sender@example.org")
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", "me@example.org")
    monkeypatch.setenv("OCPP_REPORTS_SMTP_TLS", "starttls")
    path = tmp_path / "config.toml"
    path.write_text("[mail]\n")
    report = tmp_path / "report.json"
    report.write_text('{"ok":true}')
    sent = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self, **kwargs): pass
        def send_message(self, message): sent.append(message)
    monkeypatch.setattr(mailer.smtplib, "SMTP", SMTP)
    runner = CliRunner()
    args = ["mail", "send-file", str(report), "--config", str(path)]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args).exit_code == 0
    assert len(sent) == 1
