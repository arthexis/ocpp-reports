"""Collector-owned, optional names for physical charger locations.

These labels are presentation metadata, not OCPP evidence or source identity.
Administrative writes never create a missing warehouse.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

TABLE = "collector_charger_locations"
# Matches the installed collector and customer-dashboard systemd unit templates.
DEFAULT_COLLECTOR_DATABASE = Path("/var/lib/ocpp-reports/warehouse.sqlite")


def collector_database(database: Path | None = None) -> Path:
    """Resolve one installed collector warehouse; never search or create alternatives."""
    if database is not None:
        path = Path(database)
    else:
        configured = os.environ.get("OCPP_REPORTS_DATABASE")
        path = Path(configured) if configured else DEFAULT_COLLECTOR_DATABASE
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Collector warehouse not found: {path}; use --database to override")
    if database is None:
        # Default inference must never silently target an unrelated SQLite file.
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"collector_cursors", "collector_latest"}.issubset(tables):
                raise ValueError("Inferred database is not a collector warehouse; use --database")
    return path


def validate(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 160:
        raise ValueError(f"{field} must contain 1–160 characters")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def manage(database: Path, action: str, *, source: str | None = None,
           charger: str | None = None, location: str | None = None) -> list[dict] | dict:
    """CRUD, with an explicit existing database and no mutation on list."""
    if action not in {"list", "set", "remove"}:
        raise ValueError("Unsupported location operation")
    if action != "list":
        source = validate(source, "source")
        charger = validate(charger, "charger")
        if action == "set":
            location = validate(location, "location")
        elif location is not None:
            raise ValueError("remove does not accept location")
    else:
        if charger is not None or location is not None:
            raise ValueError("list accepts only optional source")
        if source is not None:
            source = validate(source, "source")
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError("Existing collector warehouse required")
    mode = "ro" if action == "list" else "rw"
    with sqlite3.connect(f"file:{path.as_posix()}?mode={mode}", uri=True) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone() is not None
        if action == "list":
            if not exists:
                return []
            db.execute("PRAGMA query_only=ON")
            statement = f"SELECT source_id,charger_id,location FROM {TABLE}"
            if source is not None:
                statement += " WHERE source_id=?"
            statement += " ORDER BY source_id,charger_id"
            rows = db.execute(statement, (source,) if source is not None else ())
            return [dict(zip(("source_id", "charger_id", "location"), row))
                    for row in rows]
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            source_id TEXT NOT NULL, charger_id TEXT NOT NULL, location TEXT NOT NULL,
            PRIMARY KEY(source_id,charger_id)
        )""")
        if action == "set":
            db.execute(f"""INSERT INTO {TABLE} (source_id,charger_id,location) VALUES (?,?,?)
                ON CONFLICT(source_id,charger_id) DO UPDATE SET location=excluded.location""",
                (source, charger, location))
        else:
            db.execute(f"DELETE FROM {TABLE} WHERE source_id=? AND charger_id=?",
                       (source, charger))
        db.commit()
    return {"source_id": source, "charger_id": charger,
            "location": location if action == "set" else source, "action": action}
