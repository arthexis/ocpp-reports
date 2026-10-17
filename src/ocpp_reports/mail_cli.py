"""Operator-only mail queue CLI, separate from report generation."""
from __future__ import annotations
import json
import os
from pathlib import Path
import typer
from .outbox import Outbox
from .mailer import deliver_once

app = typer.Typer(help="Inspect and manually operate the local email outbox.", no_args_is_help=True)


def db_path() -> Path:
    return Path(os.environ.get("OCPP_REPORTS_OUTBOX", "~/.local/state/ocpp-reports/outbox.sqlite3")).expanduser()


def operator_address() -> str:
    address = os.environ.get("OCPP_REPORTS_OPERATOR_EMAIL", "").strip()
    if not address or "\n" in address or "\r" in address or "@" not in address:
        raise typer.BadParameter("Set a valid OCPP_REPORTS_OPERATOR_EMAIL")
    return address


@app.command("queue")
def queue(
    report: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="Existing rendered text report"),
    revision: str = typer.Option(..., help="Stable report revision/idempotency key"),
    subject: str = typer.Option("OCPP charging report", help="Email subject"),
) -> None:
    """Queue an immutable report for the operator, without sending it."""
    if not revision.strip():
        raise typer.BadParameter("Revision must not be empty")
    content = report.read_text(encoding="utf-8")
    if not content.strip():
        raise typer.BadParameter("Report must not be empty")
    key = Outbox(db_path()).enqueue(recipient=operator_address(), subject=subject, body=content, revision=revision)
    typer.echo(key)


@app.command("list")
def list_mail(json_output: bool = typer.Option(False, "-j", "--json")) -> None:
    """Show queued and previously attempted messages."""
    records = Outbox(db_path()).list()
    if json_output:
        typer.echo(json.dumps([{k: v for k, v in record.items() if k != "body"} for record in records], sort_keys=True))
    else:
        for record in records:
            typer.echo(f"{record['key']}  {record['state']}  attempts={record['attempts']}  {record['recipient']}")


@app.command("retry")
def retry(key: str = typer.Argument(..., help="Delivery key from mail list")) -> None:
    """Manually requeue a failed/interrupted send; may duplicate accepted mail."""
    outbox = Outbox(db_path())
    record = next((item for item in outbox.list() if item["key"] == key), None)
    if record is None or record["state"] not in {"failed", "sending"}:
        typer.echo("Delivery not found or not retryable", err=True)
        raise typer.Exit(2)
    outbox.retry(key)
    typer.echo(f"Requeued {key}; check inbox first to avoid duplicates.")


@app.command("send")
def send(key: str | None = typer.Option(None, "--key", help="Send only this queued delivery")) -> None:
    """Send one pending message to the configured operator via TLS SMTP."""
    recipient = operator_address()
    host = os.environ.get("OCPP_REPORTS_SMTP_HOST", "")
    sender = os.environ.get("OCPP_REPORTS_SMTP_FROM", "")
    username = os.environ.get("OCPP_REPORTS_SMTP_USER")
    password = os.environ.get("OCPP_REPORTS_SMTP_PASSWORD")
    tls = os.environ.get("OCPP_REPORTS_SMTP_TLS", "starttls")
    if tls not in {"ssl", "starttls"} or not host or not sender:
        raise typer.BadParameter("Set SMTP_HOST, SMTP_FROM and SMTP_TLS=starttls|ssl")
    try:
        port = int(os.environ.get("OCPP_REPORTS_SMTP_PORT", "465" if tls == "ssl" else "587"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise typer.BadParameter("SMTP port must be between 1 and 65535")
    outbox = Outbox(db_path())
    # Enforce operator-only delivery even if the local SQLite DB was modified.
    pending = [item for item in outbox.list() if item["state"] == "pending"]
    target = next((item for item in pending if item["key"] == key), None) if key else (pending[0] if pending else None)
    if target and target["recipient"] != recipient:
        typer.echo("Pending recipient differs from configured operator; refusing to send", err=True)
        raise typer.Exit(2)
    try:
        key = deliver_once(outbox, host=host, port=port, sender=sender,
                           username=username, password=password, tls=tls, key=key)
    except Exception as exc:
        typer.echo(f"SMTP delivery failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"SMTP accepted {key}" if key else "No pending messages")

@app.command("send-file")
def send_file(
    report: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    to: str | None = typer.Option(None, "--to"),
    subject: str = typer.Option("OCPP charging report", "--subject"),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """Send an existing CSV/JSON report as an immutable MIME attachment."""
    from .config import mail_settings
    try:
        if report.suffix.lower() not in {".csv", ".json"}:
            raise ValueError("Only CSV and JSON report attachments are supported")
        settings = mail_settings(config)
        recipient = to or settings["recipient"]
        if not recipient or "@" not in recipient or any(c in recipient for c in (chr(10), chr(13), " ")):
            raise ValueError("Invalid recipient")
        if not settings["host"] or not settings["sender"]:
            raise ValueError("SMTP host and sender are required")
        outbox = Outbox(db_path())
        key = outbox.enqueue_attachment(
            recipient=recipient, subject=subject,
            body="Attached is the requested OCPP charging report.",
            filename=report.name, content=report.read_bytes())
        record = next(item for item in outbox.list() if item["key"] == key)
        if record["state"] != "pending":
            typer.echo(f"Existing delivery {key}: {record['state']}; no automatic resend")
            return
        delivered = deliver_once(outbox, host=settings["host"], port=settings["port"],
                                 sender=settings["sender"], username=settings["username"],
                                 password=settings["password"], tls=settings["tls"], key=key)
        typer.echo(f"SMTP accepted {delivered}" if delivered else f"No pending delivery for {key}")
    except (OSError, ValueError) as exc:
        typer.echo(f"Cannot send report: {exc}", err=True)
        raise typer.Exit(2)
    except Exception as exc:
        typer.echo(f"SMTP delivery failed: {type(exc).__name__}", err=True)
        raise typer.Exit(1)


