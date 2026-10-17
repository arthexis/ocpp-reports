"""Operator CLI contract: explicit ingestion and safe dispatch."""
import json
from pathlib import Path

from typer.testing import CliRunner
from ocpp_reports.cli import app

FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"
runner = CliRunner()


def test_help_exposes_single_cli_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("status", "ingest", "generate", "run"):
        assert command in result.output
    assert runner.invoke(app, []).exit_code != 0


def test_ingest_then_generate_from_reports_database(tmp_path):
    database = tmp_path / "reports.sqlite3"
    for name, expected in (("active.json", 1), ("late_stop.json", 2)):
        result = runner.invoke(app, ["ingest", "--source", str(FIXTURES / name),
                                      "--database", str(database), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["cursor"] == expected
    output = tmp_path / "summary.json"
    result = runner.invoke(app, ["generate", "--source", str(database),
        "--source-id", "fixture-source-a",
        "--since", "2026-01-01T00:00:00Z", "--until", "2026-01-02T00:00:00Z",
        "--output", str(output)])
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["summary"]["transactions"] == 1
    assert report["summary"]["known_energy_wh"] == 500


def test_ingest_replay_and_reject_same_source_destination(tmp_path):
    database = tmp_path / "reports.sqlite3"
    args = ["ingest", "--source", str(FIXTURES / "active.json"),
            "--database", str(database), "--json"]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.output)["cursor"] == json.loads(second.output)["cursor"] == 1
    rejected = runner.invoke(app, ["ingest", "--source", str(FIXTURES / "active.json"),
        "--database", str(FIXTURES / "active.json")])
    assert rejected.exit_code == 2
