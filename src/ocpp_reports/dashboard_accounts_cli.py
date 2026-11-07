"""Local dashboard account administration; no network API or password arguments."""
from __future__ import annotations

import getpass
import os
from pathlib import Path

import typer

app = typer.Typer(help="Manage dashboard login accounts locally.", no_args_is_help=True)


def _database(path: Path | None) -> str:
    value = str(path) if path is not None else os.environ.get("OCPP_REPORTS_AUTH_DATABASE", "")
    if not value:
        raise ValueError("Specify --database or OCPP_REPORTS_AUTH_DATABASE")
    db = Path(value)
    if not db.is_file():
        raise ValueError("Authentication database does not exist; initialize it explicitly")
    return str(db)


def _password() -> str:
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    return password


@app.command("init")
def init(database: Path = typer.Option(..., "--database")) -> None:
    """Initialize a new private authentication database."""
    from .dashboard_login import initialize
    try:
        if database.exists():
            raise ValueError("Database already exists")
        if not database.parent.is_dir():
            raise ValueError("Parent directory must exist")
        initialize(str(database))
        database.chmod(0o600)
    except (ValueError, OSError) as exc:
        typer.echo(f"Database initialization refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo("Authentication database initialized")


@app.command("create")
def create(
    username: str = typer.Option(..., "--username"),
    tenant: str = typer.Option(..., "--tenant"),
    role: str = typer.Option(..., "--role", help="admin or operator"),
    database: Path | None = typer.Option(None, "--database"),
) -> None:
    """Provision a new account; password is entered without CLI arguments."""
    from .dashboard_login import provision
    try:
        if role not in ("admin", "operator"):
            raise ValueError("Only admin and operator accounts can use this dashboard")
        provision(_database(database), username, _password(), role, tenant)
    except (ValueError, OSError) as exc:
        typer.echo(f"Account creation refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo("Account created")


@app.command("list")
def list_accounts(
    database: Path | None = typer.Option(None, "--database"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """List account identities and roles; never print password hashes."""
    import sqlite3
    try:
        with sqlite3.connect(_database(database)) as db:
            if tenant:
                rows = db.execute("SELECT user_id,tenant_id,role,active FROM dashboard_users WHERE tenant_id=? ORDER BY user_id", (tenant,)).fetchall()
            else:
                rows = db.execute("SELECT user_id,tenant_id,role,active FROM dashboard_users ORDER BY tenant_id,user_id").fetchall()
        for username, scope, role, active in rows:
            typer.echo(f"{username}\t{scope}\t{role}\t{'active' if active else 'disabled'}")
    except (ValueError, OSError, sqlite3.Error) as exc:
        typer.echo(f"Account listing refused: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("update")
def update(
    username: str = typer.Option(..., "--username"),
    role: str | None = typer.Option(None, "--role"),
    enable: bool = typer.Option(False, "--enable"),
    disable: bool = typer.Option(False, "--disable"),
    reset_password: bool = typer.Option(False, "--reset-password"),
    database: Path | None = typer.Option(None, "--database"),
) -> None:
    """Change role, account status or password; revoke all existing sessions."""
    from .dashboard_login import manage_user
    try:
        if enable and disable:
            raise ValueError("--enable and --disable are mutually exclusive")
        if role is not None and role not in ("admin", "operator"):
            raise ValueError("Only admin and operator roles are supported")
        manage_user(
            _database(database), username, role=role,
            active=True if enable else False if disable else None,
            password=_password() if reset_password else None,
        )
    except (ValueError, OSError) as exc:
        typer.echo(f"Account update refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo("Account updated; sessions revoked")


@app.command("inventory-htpasswd")
def inventory_htpasswd(
    htpasswd: Path = typer.Option(..., "--htpasswd", help="Existing local Nginx password file"),
    tenant: str = typer.Option(..., "--tenant", help="Explicit dashboard tenant; never inferred"),
) -> None:
    """Preview usernames and hash families only; no password hashes or writes."""
    import json
    from .dashboard_htpasswd import inventory
    try:
        if not tenant.strip():
            raise ValueError("Tenant is required")
        accounts = inventory(htpasswd)
    except (ValueError, OSError, UnicodeError) as exc:
        typer.echo(f"Inventory refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps({"tenant": tenant, "accounts": accounts}, ensure_ascii=False))
