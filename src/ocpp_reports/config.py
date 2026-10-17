"""System-wide configuration for the installed Reports CLI."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path("/etc/ocpp-reports/config.toml")


def load_config(path: Path | None = None) -> dict:
    path = path or DEFAULT_CONFIG
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a TOML table")
    return config


def mail_settings(path: Path | None = None) -> dict:
    config = load_config(path)
    mail = config.get("mail", {})
    if not isinstance(mail, dict):
        raise ValueError("[mail] must be a TOML table")
    env = os.environ
    result = {
        "host": env.get("OCPP_REPORTS_SMTP_HOST", mail.get("host", "")),
        "sender": env.get("OCPP_REPORTS_SMTP_FROM", mail.get("from", "")),
        "username": env.get("OCPP_REPORTS_SMTP_USER", mail.get("username")),
        "password": env.get("OCPP_REPORTS_SMTP_PASSWORD"),
        "tls": env.get("OCPP_REPORTS_SMTP_TLS", mail.get("tls", "starttls")),
        "recipient": env.get("OCPP_REPORTS_OPERATOR_EMAIL", mail.get("to", "")),
    }
    if result["tls"] not in ("starttls", "ssl"):
        raise ValueError("mail.tls must be starttls or ssl")
    port = env.get("OCPP_REPORTS_SMTP_PORT", mail.get("port", 465 if result["tls"] == "ssl" else 587))
    try:
        result["port"] = int(port)
    except (ValueError, TypeError) as exc:
        raise ValueError("mail.port must be an integer") from exc
    if not 1 <= result["port"] <= 65535:
        raise ValueError("mail.port must be between 1 and 65535")
    return result
