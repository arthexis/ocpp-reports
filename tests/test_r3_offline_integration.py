"""R3A/B offline fixtures and full CLI -> outbox -> SMTP integration tests."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.outbox import Outbox

START = "2026-10-12T00:00:00+00:00"
END = "2026-10-13T00:00:00+00:00"
OPERATOR = "operator@example.org"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Fixed summary/v1 data, isolated SQLite and in-memory SMTP transport."""
    db = tmp_path / "outbox.sqlite3"
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(db))
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", OPERATOR)
    monkeypatch.setenv("OCPP_REPORTS_SMTP_HOST", "offline.invalid")
    monkeypatch.setenv("OCPP_REPORTS_SMTP_FROM", "reports@example.org")
    monkeypatch.setenv("OCPP_REPORTS_SMTP_TLS", "starttls")
    messages = []

    class SMTP:
        def __init__(self, host, port, timeout=None, **kwargs):
            assert host == "offline.invalid" and port == 587
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self, context=None):
            pass
        def login(self, username, password):
            raise AssertionError("No SMTP authentication expected")
        def send_message(self, message):
            assert isinstance(message, EmailMessage)
            messages.append(message)

    monkeypatch.setattr("ocpp_reports.mailer.smtplib.SMTP", SMTP)
    document = {
        "schema": "ocpp-reports/summary/v1",
        "source_id": "fixture-csms",
        "period": {"since": START, "until": END, "timezone": "UTC"},
        "summary": {"transactions": 2, "known_energy_wh": 0,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [
            {"id": 2, "charger_id": "CP-B", "connector_id": 1, "state": "completed", "energy_wh": 0},
            {"id": 1, "charger_id": "CP-A", "connector_id": 1, "state": "completed", "energy_wh": 0},
        ],
        "revision": "fixture-r1",
    }
    source = tmp_path / "summary.json"
    def write(**changes):
        updated = json.loads(json.dumps(document))
        updated.update(changes)
        source.write_text(json.dumps(updated), encoding="utf-8")
        return source
    write()
    return {"source": source, "db": db, "messages": messages, "write": write,
            "document": document, "tmp": tmp_path, "runner": CliRunner()}


def run(harness, *args):
    return harness["runner"].invoke(app, ["run", "--source", str(harness["source"]),
                                         "--since", START, "--until", END, *args])


def test_csv_and_text_cli_output_are_deterministic_and_not_queued(harness):
    csv_path = harness["tmp"] / "report.CSV"
    result = run(harness, "--output", str(csv_path))
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    assert [r["charger_id"] for r in rows] == ["CP-A", "CP-B"]
    assert [r["energy_wh"] for r in rows] == ["0", "0"]
    stdout = run(harness, "--format", "csv")
    assert stdout.exit_code == 0 and stdout.output == csv_path.read_text()
    text = run(harness, "--format", "text")
    assert text.exit_code == 0 and "OCPP charging report" in text.output
    assert run(harness, "--output", str(csv_path)).exit_code == 2  # no overwrite
    assert Outbox(harness["db"]).list() == []
    assert harness["messages"] == []


def test_cli_send_outbox_smtp_and_repeat_is_idempotent(harness):
    first = run(harness, "--send")
    assert first.exit_code == 0, first.output
    records = Outbox(harness["db"]).list()
    assert len(records) == 1 and records[0]["state"] == "sent"
    assert records[0]["attempts"] == 1
    assert len(harness["messages"]) == 1
    message = harness["messages"][0]
    assert message["To"] == OPERATOR
    assert message["From"] == "reports@example.org"
    assert "OCPP charging report" in message.get_content()
    assert message["Message-ID"] == f"<{records[0]['key']}@ocpp-reports.local>"
    again = run(harness, "--send")
    assert again.exit_code == 0, again.output
    assert "Already recorded" in again.output
    assert len(harness["messages"]) == 1
    assert len(Outbox(harness["db"]).list()) == 1


def test_incomplete_report_never_reaches_outbox_or_smtp(harness):
    partial = json.loads(json.dumps(harness["document"]))
    partial["summary"]["completeness"] = "partial"
    partial["summary"]["missing_energy_transactions"] = 1
    partial["transactions"][0]["energy_wh"] = None
    harness["write"](**partial)
    result = run(harness, "--send")
    assert result.exit_code == 2
    assert "incomplete" in result.output
    assert Outbox(harness["db"]).list() == []
    assert harness["messages"] == []


def test_revised_summary_cannot_replace_sent_period(harness):
    first = run(harness, "--send")
    assert first.exit_code == 0, first.output
    updated = json.loads(json.dumps(harness["document"]))
    updated["revision"] = "fixture-r2"
    harness["write"](**updated)
    again = run(harness, "--send")
    assert again.exit_code == 0, again.output
    assert "Already recorded" in again.output
    assert len(harness["messages"]) == 1
    assert len(Outbox(harness["db"]).list()) == 1
