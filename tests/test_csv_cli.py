"""R1: CSV manual output must not change the text-only SMTP contract."""
import csv
import io
import json
from datetime import datetime, timezone, timedelta

from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.outbox import Outbox


def source_file(tmp_path):
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({
        "schema": "ocpp-reports/summary/v1",
        "source_id": "csms-a",
        "period": {"since": f"{yesterday}T00:00:00+00:00",
                   "until": f"{today}T00:00:00+00:00", "timezone": "UTC"},
        "summary": {"transactions": 1, "known_energy_wh": 0,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [{"id": 2, "charger_id": "CP1", "connector_id": 1,
                          "state": "completed", "energy_wh": 0}],
        "revision": "r1",
    }), encoding="utf-8")
    return path


def test_csv_to_stdout_and_file_without_queue(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    runner = CliRunner()
    args = ["run", "--source", str(source), "--format", "csv"]
    stdout = runner.invoke(app, args)
    assert stdout.exit_code == 0, stdout.output
    assert list(csv.DictReader(io.StringIO(stdout.output)))[0]["energy_wh"] == "0"
    target = tmp_path / "report.csv"
    saved = runner.invoke(app, args + ["--output", str(target)])
    assert saved.exit_code == 0, saved.output
    assert list(csv.DictReader(io.StringIO(target.read_text())))[0]["energy_wh"] == "0"
    assert Outbox(tmp_path / "outbox.sqlite3").list() == []


def test_csv_send_rejected_without_output_or_queue(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    target = tmp_path / "report.csv"
    result = CliRunner().invoke(app, ["run", "--source", str(source), "--format", "csv",
                                     "--output", str(target), "--send"])
    assert result.exit_code == 2
    assert "manual output only" in result.output
    assert not target.exists()
    assert not (tmp_path / "outbox.sqlite3").exists()


def test_invalid_format_rejected(tmp_path):
    result = CliRunner().invoke(app, ["run", "--source", str(source_file(tmp_path)),
                                     "--format", "html"])
    assert result.exit_code == 2
    assert "Format must be auto, text or csv" in result.output


def test_infers_csv_from_extension_and_explicit_override(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    runner = CliRunner()
    csv_path = tmp_path / "inferred.CSV"
    result = runner.invoke(app, ["run", "--source", str(source), "--output", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert list(csv.DictReader(io.StringIO(csv_path.read_text())))[0]["energy_wh"] == "0"
    text_path = tmp_path / "explicit.csv"
    result = runner.invoke(app, ["run", "--source", str(source), "--format", "text",
                                 "--output", str(text_path)])
    assert result.exit_code == 0, result.output
    assert "OCPP charging report" in text_path.read_text()
    assert Outbox(tmp_path / "outbox.sqlite3").list() == []


def test_inferred_csv_cannot_send(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    path = tmp_path / "inferred.csv"
    result = CliRunner().invoke(app, ["run", "--source", str(source),
                                     "--output", str(path), "--send"])
    assert result.exit_code == 2
    assert "manual output only" in result.output
    assert not path.exists()
    assert not (tmp_path / "outbox.sqlite3").exists()
