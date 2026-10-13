"""SMTP transport; credentials are never persisted in the outbox."""
from __future__ import annotations
import os
import smtplib
import ssl
from email.message import EmailMessage
from .outbox import Outbox, Delivery


def send_smtp(delivery: Delivery, *, host: str, port: int, sender: str,
              username: str | None = None, password: str | None = None,
              tls: str = "starttls", timeout: float = 20) -> None:
    if tls not in {"starttls", "ssl"}:
        raise ValueError("SMTP requires starttls or ssl")
    if not host or not sender:
        raise ValueError("SMTP host and sender are required")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = delivery.recipient
    message["Subject"] = delivery.subject
    message["Message-ID"] = f"<{delivery.key}@ocpp-reports.local>"
    message.set_content(delivery.body)
    context = ssl.create_default_context()
    client_type = smtplib.SMTP_SSL if tls == "ssl" else smtplib.SMTP
    with client_type(host, port, timeout=timeout, **({"context": context} if tls == "ssl" else {})) as client:
        if tls == "starttls":
            client.starttls(context=context)
        if username:
            client.login(username, password or "")
        client.send_message(message)


def deliver_once(outbox: Outbox, send=send_smtp, *, key: str | None = None, **settings) -> str | None:
    delivery = outbox.claim(key=key)
    if delivery is None:
        return None
    try:
        send(delivery, **settings)
    except Exception as exc:
        # SMTP acceptance can be ambiguous after a disconnect. Never silently retry.
        outbox.mark_failed(delivery.key, f"{type(exc).__name__}: {exc}")
        raise
    else:
        outbox.mark_sent(delivery.key)
    return delivery.key
