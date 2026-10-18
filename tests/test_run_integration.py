"""End-to-end operator contract for the unified run command."""
import json
from pathlib import Path

from typer.testing import CliRunner

from ocpp_reports.cli import app

FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"
BOUNDS = ["--since", "2026-01-01T00:00:00Z", "--until", "2026-01-02T00:00:00Z"]
runner = CliRunner()


def test_run_direct_export_is_read_only_and_does_not_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    source = FIXTURES / "late_stop.json"
    original = source.read_bytes()
    result = runner.invoke(app, ["run", "--source", str(source), *BOUNDS, "--format", "json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["schema"] == "ocpp-reports/summary/v1"
    assert report["source_id"] == "fixture-source-a"
    assert source.read_bytes() == original
    assert not (tmp_path / "outbox.sqlite3").exists()


def test_run_rejects_output_overwrite_and_source_directory_write(tmp_path):
    source = FIXTURES / "late_stop.json"
    existing = tmp_path / "report.json"
    existing.write_text("keep")
    args = ["run", "--source", str(source), *BOUNDS, "--format", "json"]
    result = runner.invoke(app, args + ["--output", str(existing)])
    assert result.exit_code == 2
    assert existing.read_text() == "keep"
    result = runner.invoke(app, args + ["--output", str(source)])
    assert result.exit_code == 2


def test_run_requires_explicit_send_for_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    source = FIXTURES / "late_stop.json"
    result = runner.invoke(app, ["run", "--source", str(source), *BOUNDS, "--format", "csv"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "outbox.sqlite3").exists()
    rejected = runner.invoke(app, ["run", "--source", str(source), *BOUNDS,
                                    "--format", "json", "--send"])
    assert rejected.exit_code == 2
    assert not (tmp_path / "outbox.sqlite3").exists()


def test_run_rejects_mismatched_verified_summary_period(tmp_path):
    source = tmp_path / "summary.json"
    source.write_text(json.dumps({
        "schema": "ocpp-reports/summary/v1",
        "source_id": "test",
        "period": {"since": "2026-01-03T00:00:00Z", "until": "2026-01-04T00:00:00Z"},
        "summary": {"transactions": 0, "known_energy_wh": 0,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [], "revision": "r1",
    }))
    result = runner.invoke(app, ["run", "--source", str(source), *BOUNDS, "--format", "json"])
    assert result.exit_code == 2
    assert "period" in result.output.lower()
