"""Automatic local CSMS synchronization contract for run."""
import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.ingest import ingest_page

FIXTURE = Path(__file__).parent / "fixtures/csms_export_v1/late_stop.json"
BOUNDS = ["--since", "2026-01-01T00:00:00Z", "--until", "2026-01-02T00:00:00Z"]


def test_run_auto_sync_and_reuse(tmp_path, monkeypatch):
    import ocpp_reports.active_csms as active
    import ocpp_reports.follow as follower
    db = tmp_path / "warehouse.sqlite3"
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(db))
    monkeypatch.setattr(active, "discover_active_csms", lambda: (tmp_path, "python"))
    page = json.loads(FIXTURE.read_text())
    calls = []
    def producer(data_dir, *, after, limit):
        calls.append(after)
        return page
    monkeypatch.setattr(active, "active_csms_export", producer)
    for i in range(2):
        output = tmp_path / f"report-{i}.csv"
        result = CliRunner().invoke(app, ["run", *BOUNDS, "--format", "csv", "--output", str(output)])
        assert result.exit_code == 0, result.output
        assert output.is_file()
    assert len(calls) >= 4
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_explicit_source_skips_discovery(tmp_path, monkeypatch):
    import ocpp_reports.active_csms as active
    monkeypatch.setattr(active, "discover_active_csms", lambda: (_ for _ in ()).throw(AssertionError("discovery called")))
    result = CliRunner().invoke(app, ["run", "--source", str(FIXTURE), *BOUNDS, "--format", "json"])
    assert result.exit_code == 0, result.output


def test_auto_discovery_failure_does_not_create_output(tmp_path, monkeypatch):
    import ocpp_reports.active_csms as active
    monkeypatch.setattr(active, "discover_active_csms", lambda: (_ for _ in ()).throw(RuntimeError("CSMS not running")))
    output = tmp_path / "missing.csv"
    result = CliRunner().invoke(app, ["run", *BOUNDS, "--format", "csv", "--output", str(output)])
    assert result.exit_code == 2
    assert "CSMS not running" in result.output
    assert not output.exists()
