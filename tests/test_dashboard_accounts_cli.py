"""Regression coverage for local account administration."""
from pathlib import Path
import sqlite3

from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.dashboard_login import initialize, provision

runner = CliRunner()


def test_account_cli_init_and_list(tmp_path):
    db = tmp_path / "auth.sqlite"
    result = runner.invoke(app, ["dashboard-accounts", "init", "--database", str(db)])
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert runner.invoke(app, ["dashboard-accounts", "init", "--database", str(db)]).exit_code == 2
    provision(str(db), "operator-a", "a-secure-test-password", "operator", "tenant-a")
    result = runner.invoke(app, ["dashboard-accounts", "list", "--database", str(db)])
    assert result.exit_code == 0, result.output
    assert "operator-a" in result.output
    assert "tenant-a" in result.output
    assert "a-secure-test-password" not in result.output


def test_account_cli_disable_revokes_sessions(tmp_path):
    db = tmp_path / "auth.sqlite"
    initialize(str(db))
    provision(str(db), "operator-a", "a-secure-test-password", "operator", "tenant-a")
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO dashboard_sessions VALUES(?,?,?)", ("token-hash", "operator-a", 9999999999))
    result = runner.invoke(app, ["dashboard-accounts", "update", "--username", "operator-a", "--disable", "--database", str(db)])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT active FROM dashboard_users WHERE user_id='operator-a'").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM dashboard_sessions WHERE user_id='operator-a'").fetchone()[0] == 0


def test_account_cli_refuses_unsupported_role(tmp_path):
    db = tmp_path / "auth.sqlite"
    initialize(str(db))
    provision(str(db), "operator-a", "a-secure-test-password", "operator", "tenant-a")
    result = runner.invoke(app, ["dashboard-accounts", "update", "--username", "operator-a", "--role", "user", "--database", str(db)])
    assert result.exit_code == 2
