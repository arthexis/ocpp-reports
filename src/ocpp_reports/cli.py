"""One-shot command interface; never starts a background scheduler."""
from datetime import datetime
from pathlib import Path
import json
import typer
from . import __version__
from .models import ReportRequest
from .mail_cli import app as mail_app
from .config import DEFAULT_CONFIG, load_config

app = typer.Typer(help="OCPP Reports operator CLI: inspect, ingest, run and deliver reports.", no_args_is_help=True)
app.add_typer(mail_app, name="mail")

# Commands are explicit one-shot operations; invoking the root never ingests or sends.

@app.command("config")
def config_command(
    show: bool = typer.Option(False, "--show", help="Show non-secret configuration"),
    check: bool = typer.Option(False, "--check", help="Validate configuration without editing"),
) -> None:
    """Edit the system-wide TOML configuration with nano."""
    import os
    import subprocess
    from .config import mail_settings
    if show and check:
        raise typer.BadParameter("Choose --show or --check")
    if not show and not check:
        if not __import__("sys").stdin.isatty():
            typer.echo("Configuration editing requires an interactive terminal", err=True)
            raise typer.Exit(2)
        command = ["nano", str(DEFAULT_CONFIG)] if os.access(DEFAULT_CONFIG, os.W_OK) else ["sudo", "-e", str(DEFAULT_CONFIG)]
        environment = dict(os.environ)
        if command[0] == "sudo":
            environment["SUDO_EDITOR"] = "nano"
        try:
            completed = subprocess.run(command, env=environment, check=False)
        except OSError as exc:
            typer.echo(f"Cannot launch editor: {exc}", err=True)
            raise typer.Exit(2)
        if completed.returncode:
            raise typer.Exit(completed.returncode)
    try:
        document = load_config()
        mail_settings()
    except (OSError, ValueError) as exc:
        typer.echo(f"Configuration invalid: {exc}", err=True)
        raise typer.Exit(2)
    if show:
        safe = {key: value for key, value in document.items() if key != "mail"}
        safe["mail"] = {key: value for key, value in document.get("mail", {}).items() if key not in {"password", "secret", "token"}}
        typer.echo(json.dumps(safe, indent=2, sort_keys=True))
    else:
        typer.echo(f"Configuration valid: {DEFAULT_CONFIG}")


@app.command()
def status(json_output: bool = typer.Option(False, "-j", "--json", help="Machine-readable output")) -> None:
    from .diagnostics import inspect_status
    result = inspect_status()
    result["version"] = __version__
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        typer.echo(f"ocpp-reports {__version__}: {result['state']} (offline)")
        typer.echo(f"Config: {result['config']['state']}")
        typer.echo(f"Warehouse: {result['warehouse']['state']}")
        for source in result['warehouse']['sources']:
            typer.echo(f"  {source['source_id']}: cursor={source['cursor']}, last_ingested_at={source['last_ingested_at'] or 'never'}")

@app.command()
def ingest(
    source: Path | None = typer.Option(None, exists=True, dir_okay=False, readable=True,
                                help="One local CSMS export/v1 JSON page"),
    database: Path = typer.Option(..., "--database", help="Reports-owned SQLite destination"),
    json_output: bool = typer.Option(False, "-j", "--json"),
    follow: bool = typer.Option(False, "--follow", help="Continuously sync local CSMS"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Local CSMS evidence directory"),
    interval: float = typer.Option(5.0, "--interval", help="Follow polling interval in seconds"),
    active_csms: bool = typer.Option(False, "--active-csms", help="Use the active CSMS service Python producer"),
) -> None:
    """Import one export page or continuously follow local CSMS evidence."""
    from .ingest import ingest_page, load_page
    try:
        if follow:
            if source is not None or data_dir is None:
                raise ValueError("--follow requires --data-dir and does not accept --source")
            from .follow import follow as run_follow
            if active_csms:
                from .active_csms import active_csms_export
                run_follow(database, data_dir, interval=interval, producer=active_csms_export)
            else:
                run_follow(database, data_dir, interval=interval)
            return
        if data_dir is not None or source is None:
            raise ValueError("One-shot ingest requires --source; --data-dir requires --follow")
        if source.resolve() == database.resolve():
            raise ValueError("Source and destination must differ")
        if database.exists() and database.is_dir():
            raise ValueError("Database destination must be a file")
        page = load_page(source)
        database.parent.mkdir(parents=True, exist_ok=True)
        cursor = ingest_page(database, page)
        result = {"source_id": page["data"]["source_id"], "cursor": cursor,
                  "database": str(database)}
        typer.echo(json.dumps(result, sort_keys=True) if json_output
                   else f"Ingested {result['source_id']} through event {cursor}")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        typer.echo(f"Ingest refused: {exc}", err=True)
        raise typer.Exit(2)


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
    format: str = typer.Option("json", "--format", help="json or csv"),
    source_id: str | None = typer.Option(None, "--source-id", help="CSMS source identity for Reports SQLite"),
    profile: str | None = typer.Option(None, "--profile", help="CSV layout profile: audi"),
    locations: Path | None = typer.Option(None, "--locations", help="JSON map of charger ID to location"),
) -> None:
    """Generate a report from CSMS export JSON or Reports-owned SQLite."""
    from .reporting import build_report, build_report_from_database, render
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone)
        if profile is not None and (profile != "audi" or format != "csv"):
            raise ValueError("--profile audi requires --format csv")
        if locations is not None and profile != "audi":
            raise ValueError("--locations requires --profile audi")
        if format not in {"json", "csv"}:
            raise ValueError("format must be json or csv")
        start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            report = build_report_from_database(source, start, end, timezone, source_id=source_id)
        else:
            if source_id is not None:
                raise ValueError("--source-id is only supported for Reports SQLite")
            report = build_report(source, start, end, timezone)
        if output.resolve() == source.resolve() or (source.is_dir() and source.resolve() in output.resolve().parents):
            raise ValueError("Output must not overwrite or reside inside the source")
        if output.exists():
            raise ValueError("Output already exists; use a new revision path")
        output.parent.mkdir(parents=True, exist_ok=True)
        location_map = json.loads(locations.read_text(encoding="utf-8")) if locations else None
        if location_map is not None and (not isinstance(location_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in location_map.items())):
            raise ValueError("--locations must be a JSON object mapping charger IDs to location names")
        output.write_text(render(report, format, profile=profile, locations=location_map), encoding="utf-8")
        typer.echo(json.dumps({"output": str(output), "revision": report["revision"]}) if json_output else f"Generated {output}")
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        typer.echo(f"Report generation failed: {exc}", err=True)
        raise typer.Exit(2)


@app.command("run")
def run(
    source: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=True, readable=True, help="Explicit offline source; omit to synchronize local CSMS"),
    period: str = typer.Option("previous-day", help="previous-day, previous-week, previous-month, current-month"),
    since: str | None = typer.Option(None, help="Explicit inclusive ISO timestamp with offset"),
    until: str | None = typer.Option(None, help="Explicit exclusive ISO timestamp with offset"),
    timezone: str | None = typer.Option(None, help="IANA reporting timezone; defaults by profile"),
    output: Path | None = typer.Option(None, help="Write rendered text to a new file"),
    send: bool = typer.Option(False, "--send", help="Queue and attempt SMTP delivery"),
    format: str = typer.Option("auto", "--format", help="Output format: auto, text, json or csv"),
    source_id: str | None = typer.Option(None, "--source-id"),
    profile: str | None = typer.Option(None, "--profile"),
    locations: Path | None = typer.Option(None, "--locations"),
    json_output: bool = typer.Option(False, "-j", "--json"),
) -> None:
    """Run one configured reporting period; optionally save or deliver the result."""
    from .reporting import build_report, build_report_from_database, render
    from .schedule import previous_period, current_month
    from datetime import timezone as utc_timezone
    from .scheduled_run import prepare_scheduled_report
    from .mail_cli import db_path, operator_address, send as send_mail
    from .outbox import Outbox
    from zoneinfo import ZoneInfo
    try:
        if format not in ("auto", "text", "json", "csv"):
            raise ValueError("Format must be auto, text, json or csv")
        if format == "auto":
            format = output.suffix.lower().lstrip(".") if output is not None and output.suffix.lower() in {".csv", ".json"} else "text"
        if format != "text" and send:
            raise ValueError("--send requires text")
        if (since is None) != (until is None):
            raise ValueError("Both --since and --until are required together")
        if since is not None and period != "previous-day":
            raise ValueError("Do not combine explicit bounds with --period")
        cadence = {"previous-day": "daily", "previous-week": "weekly", "previous-month": "monthly"}.get(period)
        if cadence is None and period != "current-month":
            raise ValueError("Invalid --period")
        timezone = timezone or ("America/Monterrey" if profile == "audi" else "UTC")
        ZoneInfo(timezone)
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        start = parse(since) if since is not None else None
        end = parse(until) if until is not None else None
        if start is None:
            now = datetime.now(utc_timezone.utc)
            start, end = (current_month(now, zone=timezone) if period == "current-month"
                          else previous_period(now, cadence=cadence, zone=timezone))
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Expected ordered timezone-aware reporting bounds")
        if profile is not None and (profile != "audi" or format != "csv"):
            raise ValueError("--profile audi requires --format csv")
        if locations is not None and profile != "audi":
            raise ValueError("--locations requires --profile audi")
        if source is None:
            from .active_csms import discover_active_csms, active_csms_export
            from .follow import follow
            import os
            data_dir, _ = discover_active_csms()
            source = Path(os.environ.get("OCPP_REPORTS_DATABASE", "/var/lib/ocpp-reports/reports.sqlite3"))
            follow(source, data_dir, once=True, producer=active_csms_export)
            import sqlite3
            with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as connection:
                identities = [row[0] for row in connection.execute("SELECT source_id FROM sources")]
            if len(identities) != 1:
                raise ValueError("Automatic reporting requires exactly one local CSMS source")
            if source_id is not None and source_id != identities[0]:
                raise ValueError("Requested source does not match local CSMS")
            source_id = identities[0]
        if output is not None and (output.resolve() == source.resolve() or
                (source.is_dir() and source.resolve() in output.resolve().parents)):
            raise ValueError("Output must not overwrite or reside inside the source")
        if output is not None and output.exists():
            raise ValueError("Output already exists; use a new revision path")
        document = None
        if source.is_file() and source.suffix.lower() == ".json":
            candidate = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("schema") == "ocpp-reports/summary/v1":
                document = candidate
        if document is None:
            if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                document = build_report_from_database(source, start, end, timezone, source_id=source_id)
            else:
                if source_id is not None:
                    raise ValueError("--source-id is only valid for Reports SQLite")
                document = build_report(source, start, end, timezone)
        elif source_id is not None:
            raise ValueError("--source-id is only valid for Reports SQLite")
        if format in {"json", "csv"}:
            period_info = document.get("period")
            if not isinstance(period_info, dict) or parse(period_info["since"]) != start or parse(period_info["until"]) != end:
                raise ValueError("Report period does not match requested interval")
            location_map = json.loads(locations.read_text(encoding="utf-8")) if locations else None
            if location_map is not None and (not isinstance(location_map, dict) or
                    not all(isinstance(k, str) and isinstance(v, str) for k, v in location_map.items())):
                raise ValueError("--locations must map charger IDs to location names")
            rendered = render(document, format, profile=profile, locations=location_map)
            if output is None:
                typer.echo(rendered, nl=False)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as stream:
                    stream.write(rendered)
                typer.echo(json.dumps({"output": str(output), "revision": document["revision"]}) if json_output else f"Written {output}")
            return
        if locations is not None:
            raise ValueError("--locations requires CSV format")
        if not send:
            from .rendering import render_report
            rendered = render_report(document, "text")
            if output is None:
                typer.echo(rendered, nl=False)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as stream:
                    stream.write(rendered)
                typer.echo(f"Written {output}")
            return
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
        if document.get("summary", {}).get("completeness") != "complete" or document.get("summary", {}).get("missing_energy_transactions") != 0:
            raise ValueError("Refusing to schedule incomplete or provisional report")
        result = prepare_scheduled_report(source=source, document=document, outbox=Outbox(db_path()),
            operator=operator_address() if send else "operator@localhost",
            cadence=cadence, zone=timezone, since=start, until=end,
            output=output, queue=send, format=format)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, json.JSONDecodeError) as exc:
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


@app.command("serve")
def serve(
    database: Path = typer.Option(..., "--database", help="Reports SQLite warehouse"),
    host: str = typer.Option("127.0.0.1", "--host", help="Local bind address"),
    port: int = typer.Option(8080, "--port", min=1, max=65535),
) -> None:
    """Serve the read-only dashboard; external exposure requires a secure proxy."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        typer.echo("Refusing non-loopback bind; use an authenticated HTTPS reverse proxy", err=True)
        raise typer.Exit(2)
    import uvicorn
    from .web import create_app
    uvicorn.run(create_app(database), host=host, port=port)


@app.command("forward")
def forward_command(
    database: Path = typer.Option(..., "--database", help="Local Reports warehouse"),
    state: Path = typer.Option(..., "--state", help="Forwarder cursor SQLite file"),
    source_id: str = typer.Option(..., "--source-id", help="Authorized satellite source"),
    endpoint: str = typer.Option(..., "--endpoint", help="Collector HTTPS replication endpoint"),
    token_file: Path = typer.Option(..., "--token-file", help="File containing the source bearer token"),
    interval: float = typer.Option(2.0, "--interval", help="Seconds between idle polls"),
    limit: int = typer.Option(500, "--limit", min=1, max=500),
    once: bool = typer.Option(False, "--once", help="Drain available changes and exit"),
) -> None:
    """Forward local changefeed with durable acknowledgements; no silent retries."""
    from .forwarder import forward
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        forward(database, state, source_id, endpoint, token,
                interval=interval, limit=limit, once=once)
    except (OSError, ValueError) as exc:
        typer.echo(f"Forward refused: {exc}", err=True)
        raise typer.Exit(2)


@app.command("collect")
def collect_command(
    database: Path = typer.Option(..., "--database"),
    credentials_file: Path = typer.Option(..., "--credentials-file"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8081, "--port", min=1, max=65535),
) -> None:
    """Run the authenticated collector on loopback behind HTTPS reverse proxy."""
    from .collector_server import serve_collector
    try:
        serve_collector(database, credentials_file, host, port)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"Collector refused: {exc}", err=True)
        raise typer.Exit(2)


if __name__ == "__main__":
    app()

# Explicit Reports-owned diagnostic publication. No CSMS download command remains.
diagnose_app = typer.Typer(help="Manage centralized diagnostic archives.", no_args_is_help=True)
app.add_typer(diagnose_app, name="diagnose")

@diagnose_app.command("serve")
def diagnose_serve(cp: str = typer.Option(..., "--cp"),
                   source: str | None = typer.Option(None, "--source"),
                   status: bool = typer.Option(False, "--status"),
                   revoke: bool = typer.Option(False, "--revoke")) -> None:
    from .diagnostic_archive import DEFAULT_ARCHIVE, inventory, publish, revoke as revoke_access
    if status and revoke:
        raise typer.BadParameter("--status and --revoke are mutually exclusive")
    if status:
        typer.echo(json.dumps({"cp": cp, "files": len(inventory(DEFAULT_ARCHIVE, source, cp))}))
        return
    if revoke:
        revoke_access(cp)
        typer.echo("Diagnostic access revoked")
        return
    result = publish(DEFAULT_ARCHIVE, cp, source)
    typer.echo(f"Username: {result['username']}\\nPassword: {result['password']}\\nFiles: {result['files']}")


@diagnose_app.command("forward")
def diagnose_forward(data_dir: Path = typer.Option(..., "--data-dir"),
                     uploads_root: Path = typer.Option(Path("/var/lib/ocpp-csms/diagnostics"), "--uploads-root"),
                     state: Path = typer.Option(Path("/var/lib/ocpp-reports/forwarder-state"), "--state"),
                     source: str = typer.Option(..., "--source"),
                     endpoint: str = typer.Option(..., "--endpoint"),
                     token: str = typer.Option(..., "--token", hide_input=True)) -> None:
    from .diagnostic_archive import forward_archives
    typer.echo(f"Forwarded {forward_archives(data_dir, uploads_root, state, endpoint, source, token)} diagnostic archives")
