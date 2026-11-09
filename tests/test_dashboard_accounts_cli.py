"""Regression coverage for local account administration."""
from pathlib import Path
import sqlite3

from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.dashboard_login import initialize, provision

runner = CliRunner()


def test_account_cli_init_and_list(tmp_path):
    db = tmp_path / "auth.sqlite"
    result = runner.invoke(app, ["accounts", "init", "--database", str(db)])
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert runner.invoke(app, ["accounts", "init", "--database", str(db)]).exit_code == 2
    provision(str(db), "operator-a", "a-secure-test-password", "operator", "tenant-a")
    result = runner.invoke(app, ["accounts", "list", "--database", str(db)])
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
    result = runner.invoke(app, ["accounts", "update", "--username", "operator-a", "--disable", "--database", str(db)])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT active FROM dashboard_users WHERE user_id='operator-a'").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM dashboard_sessions WHERE user_id='operator-a'").fetchone()[0] == 0


def test_account_cli_refuses_unsupported_role(tmp_path):
    db = tmp_path / "auth.sqlite"
    initialize(str(db))
    provision(str(db), "operator-a", "a-secure-test-password", "operator", "tenant-a")
    result = runner.invoke(app, ["accounts", "update", "--username", "operator-a", "--role", "user", "--database", str(db)])
    assert result.exit_code == 2


def test_account_database_autodetection_and_tenants(tmp_path, monkeypatch):
    import json
    from ocpp_reports import dashboard_accounts_cli as accounts

    db = tmp_path / "auth.sqlite"
    inventory = tmp_path / "dashboards.json"
    initialize(str(db))
    provision(str(db), "a-admin", "secure-test-password-a", "admin", "tenant-a")
    inventory.write_text(json.dumps([
        {"hostname": "customer.example.com", "auth_mode": "integrated",
         "auth_tenant": "administration", "auth_database": str(db)},
        {"hostname": "other.example.com", "auth_mode": "integrated",
         "auth_tenant": "tenant-a", "auth_database": str(db)},
    ]))
    monkeypatch.setattr(accounts, "INVENTORY", inventory)
    monkeypatch.setattr(accounts, "DEFAULT_DATABASE", tmp_path / "missing.sqlite")
    monkeypatch.delenv("OCPP_REPORTS_AUTH_DATABASE", raising=False)
    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0, result.output
    assert "a-admin" in result.output
    result = runner.invoke(app, ["accounts", "tenants"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["administration", "tenant-a"]
    assert "secure-test-password-a" not in result.output

    other = tmp_path / "other.sqlite"
    initialize(str(other))
    inventory.write_text(json.dumps([
        {"auth_mode": "integrated", "auth_tenant": "tenant-a", "auth_database": str(db)},
        {"auth_mode": "integrated", "auth_tenant": "tenant-b", "auth_database": str(other)},
    ]))
    ambiguous = runner.invoke(app, ["accounts", "list"])
    assert ambiguous.exit_code == 2
    assert "Multiple authentication databases" in ambiguous.output
    explicit = runner.invoke(app, ["accounts", "tenants", "--database", str(db)])
    assert explicit.exit_code == 0, explicit.output
    assert explicit.output.splitlines() == ["tenant-a"]
    monkeypatch.setenv("OCPP_REPORTS_AUTH_DATABASE", str(other))
    assert runner.invoke(app, ["accounts", "list"]).exit_code == 0


def test_account_database_missing_inventory_requires_explicit_path(tmp_path, monkeypatch):
    from ocpp_reports import dashboard_accounts_cli as accounts

    monkeypatch.setattr(accounts, "INVENTORY", tmp_path / "absent.json")
    monkeypatch.setattr(accounts, "DEFAULT_DATABASE", tmp_path / "absent.sqlite")
    monkeypatch.delenv("OCPP_REPORTS_AUTH_DATABASE", raising=False)
    result = runner.invoke(app, ["accounts", "tenants"])
    assert result.exit_code == 2
    assert "specify --database" in result.output
