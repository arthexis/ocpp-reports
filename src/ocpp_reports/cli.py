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

@app.command("run")
def run(
    source: Path = typer.Option(..., exists=True, dir_okay=False, readable=True, help="Verified internal summary JSON"),
    period: str = typer.Option("previous-day", help="previous-day, previous-week, previous-month"),
    since: str | None = typer.Option(None, help="Explicit inclusive ISO timestamp with offset"),
    until: str | None = typer.Option(None, help="Explicit exclusive ISO timestamp with offset"),
    timezone: str = typer.Option("UTC", help="IANA reporting timezone"),
    output: Path | None = typer.Option(None, help="Write rendered text to a new file"),
    send: bool = typer.Option(False, "--send", help="Queue and attempt SMTP delivery"),
    format: str = typer.Option("text", "--format", help="Manual output format: text or csv"),
) -> None:
    """Render one verified report; optionally save it or send it to the operator."""
    from .scheduled_run import prepare_scheduled_report
    from .mail_cli import db_path, operator_address, send as send_mail
    from .outbox import Outbox
    from zoneinfo import ZoneInfo
    try:
        if format not in ("text", "csv"):
            raise ValueError("Format must be text or csv")
        if format == "csv" and send:
            raise ValueError("CSV is manual output only; --send requires text")
        if (since is None) != (until is None):
            raise ValueError("Both --since and --until are required together")
        if since is not None and period != "previous-day":
            raise ValueError("Do not combine explicit bounds with --period")
        cadence = {"previous-day": "daily", "previous-week": "weekly", "previous-month": "monthly"}.get(period)
        if cadence is None:
            raise ValueError("Invalid --period")
        ZoneInfo(timezone)
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        start = parse(since) if since is not None else None
        end = parse(until) if until is not None else None
        if output is None and not send:
            rendered = prepare_scheduled_report(source=source, outbox=Outbox(db_path()),
                operator="operator@localhost", cadence=cadence, zone=timezone,
                since=start, until=end, queue=False, format=format)
            typer.echo(rendered)
            return
        result = prepare_scheduled_report(source=source, outbox=Outbox(db_path()),
            operator=operator_address() if send else "operator@localhost",
            cadence=cadence, zone=timezone, since=start, until=end,
            output=output, queue=send, format=format)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(f"Report run refused: {exc}", err=True)
        raise typer.Exit(2)
    if send:
        if result.startswith("EXISTS:"):
            typer.echo(f"Already recorded for this period: {result[7:]}; no automatic retry")
            return
        typer.echo(f"Queued {result}")
        send_mail(key=result)
    elif output is not None:
        typer.echo(f"Written {output}")

if __name__ == "__main__":
    app()
