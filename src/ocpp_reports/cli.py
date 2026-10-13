"""One-shot command interface; never starts a background scheduler."""
from datetime import datetime
from pathlib import Path
import json
import typer
from . import __version__
from .models import ReportRequest
from .mail_cli import app as mail_app

app = typer.Typer(help="Offline OCPP report generation and operator mail delivery.", no_args_is_help=True)
app.add_typer(mail_app, name="mail")

@app.command()
def status(json_output: bool = typer.Option(False, "-j", "--json", help="Machine-readable output")) -> None:
    result = {"name": "ocpp-reports", "version": __version__, "state": "foundation", "network_required": False}
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        typer.echo(f"ocpp-reports {__version__}: foundation ready (offline)")

@app.command()
def check(
    since: str = typer.Option(..., help="Inclusive ISO timestamp with offset"),
    until: str = typer.Option(..., help="Exclusive ISO timestamp with offset"),
    timezone: str = typer.Option("UTC", help="IANA timezone name"),
    json_output: bool = typer.Option(False, "-j", "--json"),
) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone)
        request = ReportRequest(since=datetime.fromisoformat(since.replace("Z", "+00:00")), until=datetime.fromisoformat(until.replace("Z", "+00:00")), timezone=timezone)
        request.validate_period()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        typer.echo(f"Invalid reporting period: {exc}", err=True)
        raise typer.Exit(2)
    payload = {"valid": True, "since": request.since.isoformat(), "until": request.until.isoformat(), "timezone": timezone}
    typer.echo(json.dumps(payload, sort_keys=True) if json_output else "Configuration valid")

@app.command()
def generate(
    source: Path = typer.Option(..., exists=True, file_okay=True, dir_okay=True, readable=True, help="CSMS export location"),
    output: Path = typer.Option(..., help="Reports-owned destination"),
    since: str = typer.Option(...),
    until: str = typer.Option(...),
    timezone: str = typer.Option("UTC"),
    json_output: bool = typer.Option(False, "-j", "--json"),
) -> None:
    """Foundation only: do not generate reports until the real CSMS contract is frozen."""
    typer.echo("CSMS export adapter not yet implemented (B2); no files were written.", err=True)
    raise typer.Exit(3)

if __name__ == "__main__":
    app()
