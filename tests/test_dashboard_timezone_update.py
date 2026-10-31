"""Installed-dashboard timezone updates never rewrite service scope or CSMS data."""
import json
import subprocess

import pytest
from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports import dashboard_timezones as updates

runner = CliRunner()


def completed(code=0, output=""):
    return subprocess.CompletedProcess([], code, stdout=output, stderr="")


def prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(updates.os, "geteuid", lambda: 0)
    path = updates.timezone_file("dashboard")
    service = "ocpp-reports-dashboard.service"
    effective = f"{{ path=/usr/local/bin/ocpp-reports ; argv[]=/usr/local/bin/ocpp-reports serve --source-id customer --timezone-file {path} ; }}"
    commands = []

    def run(args):
        commands.append(args)
        if args[:2] == ["systemctl", "show"]:
            return completed(output=effective)
        return completed()

    monkeypatch.setattr(updates, "_run", run)
    return path, commands, service


def test_update_cli_existing_service_without_new_bind(tmp_path, monkeypatch):
    path, commands, service = prepare(tmp_path, monkeypatch)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("Must not start second dashboard"))
    result = runner.invoke(app, [
        "serve", "--update", "--timezone", "America/Monterrey", "--timezone", "UTC"
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text()) == ["America/Monterrey", "UTC"]
    assert commands == [
        ["systemctl", "show", service, "-p", "ExecStart", "--value"],
        ["systemctl", "restart", service],
        ["systemctl", "is-active", "--quiet", service],
    ]
    commands.clear()
    result = runner.invoke(app, ["serve", "--update", "--timezone", "America/Monterrey"])
    assert result.exit_code == 0
    assert "already configured" in result.output
    assert len(commands) == 1  # idempotent; no unnecessary service restart


def test_update_rejects_non_root_or_missing_install(tmp_path, monkeypatch):
    path, commands, _ = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000)
    denied = runner.invoke(app, ["serve", "--update", "--timezone", "UTC"])
    assert denied.exit_code == 2
    assert not path.exists()
    monkeypatch.setattr(updates.os, "geteuid", lambda: 0)
    monkeypatch.setattr(updates, "_run", lambda args: completed(output="unrelated unit"))
    missing = runner.invoke(app, ["serve", "--update", "--timezone", "UTC"])
    assert missing.exit_code == 2
    assert not path.exists()
    assert not commands


def test_update_preserves_original_config_after_failed_restart(tmp_path, monkeypatch):
    path, commands, service = prepare(tmp_path, monkeypatch)
    old = b'["Europe/Paris", "UTC"]\n'
    path.write_bytes(old)
    def fail_first_restart(args):
        commands.append(args)
        if args[:2] == ["systemctl", "show"]:
            return completed(output=f"ExecStart=serve --timezone-file {path}")
        if args[:2] == ["systemctl", "restart"] and commands.count(args) == 1:
            return completed(code=1)
        return completed()
    monkeypatch.setattr(updates, "_run", fail_first_restart)
    result = runner.invoke(app, ["serve", "--update", "--timezone", "America/Monterrey"])
    assert result.exit_code == 2
    assert "previous timezone configuration restored" in result.output
    assert path.read_bytes() == old
    assert commands.count(["systemctl", "restart", service]) == 2


def test_scoped_dashboard_targeting_and_invalid_input(tmp_path, monkeypatch):
    path, commands, _ = prepare(tmp_path, monkeypatch)
    customer = updates.timezone_file("dashboard-audi")
    monkeypatch.setattr(updates, "_run", lambda args: completed(
        output=f"ExecStart=serve --source-id audi --timezone-file {customer}"
    ))
    result = runner.invoke(app, ["serve", "--update", "--instance", "dashboard-audi",
                                 "--timezone", "America/Monterrey"])
    assert result.exit_code == 0, result.output
    assert json.loads(customer.read_text()) == ["America/Monterrey", "UTC"]
    assert not path.exists()
    invalid = runner.invoke(app, ["serve", "--update", "--instance", "../dashboard",
                                   "--timezone", "UTC"])
    assert invalid.exit_code == 2
    invalid_tz = runner.invoke(app, ["serve", "--update", "--timezone", "Not/A_Zone"])
    assert invalid_tz.exit_code == 2
    no_zone = runner.invoke(app, ["serve", "--update"])
    assert no_zone.exit_code == 2
    assert not path.exists()


def test_timezone_override_file_preserves_configured_default(tmp_path):
    path = tmp_path / "dashboard.timezones.json"
    assert updates.configured_timezones(path, ("Europe/Paris",)) == ("Europe/Paris", "UTC")
    path.write_text('["America/Monterrey", "UTC"]')
    assert updates.configured_timezones(path, ("Europe/Paris",)) == ("America/Monterrey", "UTC")
    path.write_text('{"unexpected": true}')
    with pytest.raises(ValueError, match="nonempty JSON list"):
        updates.configured_timezones(path, ("UTC",))
