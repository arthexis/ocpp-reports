import json
from typer.testing import CliRunner
from ocpp_reports.cli import app

runner = CliRunner()


def test_mail_queue_list_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", "operator@example.org")
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    report = tmp_path / "report.txt"
    report.write_text("PROVISIONAL report", encoding="utf-8")
    queued = runner.invoke(app, ["mail", "queue", str(report), "--revision", "rev-1"])
    assert queued.exit_code == 0, queued.output
    key = queued.output.strip()
    listed = runner.invoke(app, ["mail", "list", "-j"])
    assert listed.exit_code == 0, listed.output
    records = json.loads(listed.output)
    assert len(records) == 1 and records[0]["state"] == "pending"
    assert "body" not in records[0]
    retry = runner.invoke(app, ["mail", "retry", key])
    assert retry.exit_code == 2


def test_queue_requires_operator_email(monkeypatch, tmp_path):
    monkeypatch.delenv("OCPP_REPORTS_OPERATOR_EMAIL", raising=False)
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    report = tmp_path / "report.txt"
    report.write_text("report", encoding="utf-8")
    result = runner.invoke(app, ["mail", "queue", str(report), "--revision", "rev-1"])
    assert result.exit_code != 0


def test_send_requires_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", "operator@example.org")
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    monkeypatch.delenv("OCPP_REPORTS_SMTP_HOST", raising=False)
    monkeypatch.delenv("OCPP_REPORTS_SMTP_FROM", raising=False)
    assert runner.invoke(app, ["mail", "send"]).exit_code != 0
