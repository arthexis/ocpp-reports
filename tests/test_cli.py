import json
from typer.testing import CliRunner
from ocpp_reports.cli import app
from ocpp_reports.models import ReportRequest
from datetime import datetime
import pytest

runner = CliRunner()

def test_status_json():
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["network_required"] is False

def test_check_period():
    result = runner.invoke(app, ["check", "--since", "2026-10-01T00:00:00+00:00", "--until", "2026-10-02T00:00:00+00:00", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["valid"] is True

def test_reject_naive_time():
    req = ReportRequest(since=datetime(2026, 10, 1), until=datetime(2026, 10, 2), timezone="UTC")
    with pytest.raises(ValueError, match="offsets"):
        req.validate_period()

def test_generate_is_not_false_success(tmp_path):
    source = tmp_path / "export.json"
    source.write_text("{}")
    output = tmp_path / "report.json"
    result = runner.invoke(app, ["generate", "--source", str(source), "--output", str(output), "--since", "2026-10-01T00:00:00+00:00", "--until", "2026-10-02T00:00:00+00:00"])
    assert result.exit_code != 0
    assert "Report generation failed" in result.output
    assert not output.exists()
