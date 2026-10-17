"""Read-only local operational diagnostics; never create or migrate storage."""
from __future__ import annotations

import os
import sqlite3
import tomllib
from pathlib import Path


def inspect_status(*, config: Path | None = None, database: Path | None = None) -> dict:
    from .config import DEFAULT_CONFIG
    config = config or DEFAULT_CONFIG
    database = database or Path(os.environ.get("OCPP_REPORTS_DATABASE", "/var/lib/ocpp-reports/reports.sqlite3"))
    result = {
        "name": "ocpp-reports",
        "network_required": False,
        "config": {"path": str(config), "state": "missing"},
        "warehouse": {"path": str(database), "state": "missing", "sources": []},
    }
    try:
        with config.open("rb") as stream:
            document = tomllib.load(stream)
        if not isinstance(document, dict):
            raise ValueError("Configuration must be a TOML table")
        result["config"]["state"] = "valid"
    except FileNotFoundError:
        pass
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        result["config"]["state"] = "invalid"
        result["config"]["error"] = type(exc).__name__

    if database.exists():
        try:
            # URI mode=ro guarantees that status cannot initialize or migrate SQLite.
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute("SELECT source_id, cursor FROM sources ORDER BY source_id").fetchall()
            result["warehouse"]["state"] = "ready"
            result["warehouse"]["sources"] = [
                {"source_id": source, "cursor": cursor} for source, cursor in rows
            ]
        except (OSError, sqlite3.Error, ValueError) as exc:
            result["warehouse"]["state"] = "invalid"
            result["warehouse"]["error"] = type(exc).__name__
    result["state"] = "ready" if (
        result["config"]["state"] == "valid" and result["warehouse"]["state"] == "ready"
    ) else "degraded" if (
        "invalid" in (result["config"]["state"], result["warehouse"]["state"])
    ) else "unconfigured"
    return result
