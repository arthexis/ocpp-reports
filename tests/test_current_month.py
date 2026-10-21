"""Current-month reporting boundaries and CLI behavior."""
from datetime import datetime, timezone
from pathlib import Path
import json

from typer.testing import CliRunner
from ocpp_reports.schedule import current_month
from ocpp_reports.cli import app


def test_current_month_starts_at_local_midnight_and_ends_now():
    now = datetime(2026, 10, 21, 18, 15, tzinfo=timezone.utc)
    start, end = current_month(now, zone="America/Monterrey")
    assert start.isoformat() == "2026-10-01T06:00:00+00:00"
    assert end == now


def test_current_month_rollover():
    now = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    start, end = current_month(now, zone="America/Monterrey")
    assert start.isoformat() == "2026-10-01T06:00:00+00:00"
    assert end == now


def test_run_current_month_with_explicit_source(tmp_path):
    source = Path(__file__).parent / "fixtures/csms_export_v1/late_stop.json"
    result = CliRunner().invoke(app, [
        "run", "--source", str(source), "--period", "current-month",
        "--timezone", "America/Monterrey", "--format", "json",
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["period"]["timezone"] == "America/Monterrey"
