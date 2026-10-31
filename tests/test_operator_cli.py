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
    for command in ("status", "ingest", "run"):
        assert command in result.output
    assert runner.invoke(app, []).exit_code != 0


def test_ingest_then_run_from_reports_database(tmp_path):
    database = tmp_path / "reports.sqlite3"
    for name, expected in (("active.json", 1), ("late_stop.json", 2)):
        result = runner.invoke(app, ["ingest", "--source", str(FIXTURES / name),
                                      "--database", str(database), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["cursor"] == expected
    output = tmp_path / "summary.json"
    result = runner.invoke(app, ["run", "--source", str(database),
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


def test_root_help_has_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for arguments in ([], ["--help"]):
        result = runner.invoke(app, arguments)
        assert "status" in result.output
        assert "ingest" in result.output
        assert "run" in result.output
        assert "run" in result.output
    assert list(tmp_path.iterdir()) == []


def test_subcommand_help_and_json_flags():
    for command in ("status", "ingest", "run", "config", "check", "mail"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, (command, result.output)
    for command in ("status", "ingest", "run", "check"):
        result = runner.invoke(app, [command, "--help"])
        import re
        plain = re.sub("\\x1b\\[[0-9;]*m", "", result.output)
        assert "--json" in plain, (command, plain)


def test_ingest_validation_failure_is_nonzero_and_read_only(tmp_path):
    source = tmp_path / "invalid.json"
    source.write_text("{}", encoding="utf-8")
    destination = tmp_path / "warehouse.sqlite3"
    result = runner.invoke(app, ["ingest", "--source", str(source),
                                  "--database", str(destination), "--json"])
    assert result.exit_code == 2, result.output
    assert not destination.exists()


def test_serve_infers_collector_database_and_keeps_override(tmp_path, monkeypatch):
    import sqlite3
    import uvicorn

    first = tmp_path / "warehouse.sqlite"
    second = tmp_path / "other.sqlite"
    for path in (first, second):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE collector_cursors (source_id TEXT)")
            db.execute("CREATE TABLE collector_latest (source_id TEXT)")
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(first))
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: calls.append((application, kwargs)))
    result = runner.invoke(app, ["serve", "--timezone", "America/Monterrey"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0].title == "OCPP Reports"
    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][0].routes
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(tmp_path / "missing.sqlite"))
    override = runner.invoke(app, ["serve", "--database", str(second)])
    assert override.exit_code == 0, override.output
    assert len(calls) == 2


def test_serve_inference_fails_without_collector_and_does_not_create_db(tmp_path, monkeypatch):
    import sqlite3
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: calls.append(kwargs))
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(missing))
    failure = runner.invoke(app, ["serve"])
    assert failure.exit_code == 2
    assert "Collector warehouse not found" in failure.output
    assert not missing.exists()
    unrelated = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(unrelated))
    failure = runner.invoke(app, ["serve"])
    assert failure.exit_code == 2
    assert "not a collector warehouse" in failure.output
    assert not calls
