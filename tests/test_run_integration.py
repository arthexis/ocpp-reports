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

def test_partial_summary_is_manual_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", "operator@example.org")
    source = tmp_path / "partial.json"
    source.write_text(json.dumps({
        "schema": "ocpp-reports/summary/v1", "source_id": "csms-a",
        "period": {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z", "timezone": "UTC"},
        "summary": {"transactions": 1, "known_energy_wh": 0,
                    "missing_energy_transactions": 1, "completeness": "partial"},
        "transactions": [{"id": 1, "charger_id": "CP1", "connector_id": 1,
                          "state": "active", "energy_wh": None}], "revision": "partial-r1",
    }))
    manual = runner.invoke(app, ["run", "--source", str(source), *BOUNDS, "--format", "text"])
    assert manual.exit_code == 0, manual.output
    assert "OCPP charging report" in manual.output
    rejected = runner.invoke(app, ["run", "--source", str(source), *BOUNDS, "--send"])
    assert rejected.exit_code == 2
    assert "incomplete" in rejected.output
    assert not (tmp_path / "outbox.sqlite3").exists()

def test_send_complete_report_once_with_stubbed_smtp(tmp_path, monkeypatch):
    from ocpp_reports.outbox import Outbox
    import ocpp_reports.mail_cli as mail_cli
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", "operator@example.org")
    source = tmp_path / "complete.json"
    source.write_text(json.dumps({
        "schema": "ocpp-reports/summary/v1", "source_id": "csms-a",
        "period": {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z", "timezone": "UTC"},
        "summary": {"transactions": 0, "known_energy_wh": 0,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [], "revision": "complete-r1",
    }))
    calls = []
    def fake_send(*, key=None):
        calls.append(key)
        Outbox(tmp_path / "outbox.sqlite3").claim(key)
        Outbox(tmp_path / "outbox.sqlite3").mark_sent(key)
    monkeypatch.setattr(mail_cli, "send", fake_send)
    args = ["run", "--source", str(source), *BOUNDS, "--send"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "Queued" in first.output
    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "Already recorded" in second.output
    assert len(calls) == 1
    records = Outbox(tmp_path / "outbox.sqlite3").list()
    assert len(records) == 1 and records[0]["state"] == "sent"

def test_profile_timezone_default_and_explicit_override(tmp_path):
    source = FIXTURES / "late_stop.json"
    base = ["run", "--source", str(source), *BOUNDS, "--format", "json"]
    default = runner.invoke(app, base)
    assert default.exit_code == 0, default.output
    assert json.loads(default.stdout)["period"]["timezone"] == "UTC"
    # vendor requires CSV; its default zone is visible in a generated JSON summary
    # only through an explicit override on the standard profile.
    overridden = runner.invoke(app, base + ["--timezone", "America/Monterrey"])
    assert overridden.exit_code == 0, overridden.output
    assert json.loads(overridden.stdout)["period"]["timezone"] == "America/Monterrey"
    vendor = runner.invoke(app, ["run", "--source", str(source), *BOUNDS,
                               "--format", "csv", "--profile", "vendor"])
    assert vendor.exit_code == 0, vendor.output
    explicit = runner.invoke(app, ["run", "--source", str(source), *BOUNDS,
                                   "--format", "csv", "--profile", "vendor",
                                   "--timezone", "UTC"])
    assert explicit.exit_code == 0, explicit.output
