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

    try:
        database_present = database.exists()
    except OSError as exc:
        database_present = False
        result["warehouse"]["state"] = "inaccessible"
        result["warehouse"]["error"] = type(exc).__name__

    if database_present:
        try:
            # URI mode=ro guarantees that status cannot initialize or migrate SQLite.
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
                has_ingested_at = "last_ingested_at" in columns
                selection = "source_id, cursor, last_ingested_at" if has_ingested_at else "source_id, cursor, NULL"
                rows = db.execute(f"SELECT {selection} FROM sources ORDER BY source_id").fetchall()
            result["warehouse"]["state"] = "ready"
            result["warehouse"]["last_ingested_at"] = max((row[2] for row in rows if row[2]), default=None)
            result["warehouse"]["sources"] = [
                {"source_id": source, "cursor": cursor, "last_ingested_at": ingested_at}
                for source, cursor, ingested_at in rows
            ]
        except (OSError, sqlite3.Error, ValueError) as exc:
            result["warehouse"]["state"] = "invalid"
            result["warehouse"]["error"] = type(exc).__name__
    result["state"] = "ready" if (
        result["config"]["state"] == "valid" and result["warehouse"]["state"] == "ready"
    ) else "degraded" if (
        any(state in ("invalid", "inaccessible") for state in (result["config"]["state"], result["warehouse"]["state"]))
    ) else "unconfigured"
    return result
