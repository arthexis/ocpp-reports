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



def _resolve_identity(db: sqlite3.Connection, source: str | None,
                      charger: str | None, tables: set[str]) -> tuple[str, str]:
    """Infer only from exact distinct (source, charger) identities in this warehouse.

    Include persisted assignments so operators can remove labels after a source
    reset; never infer from the source cursor alone (it identifies no charger).
    Called under the same write transaction as the resulting mutation.
    """
    if source is not None and charger is not None:
        return source, charger
    if source is None and charger is None:
        raise ValueError("Specify --source or --charger; neither may be inferred together")

    identities: set[tuple[str, str]] = set()
    if "connector_state" in tables:
        identities.update((row[0], row[1]) for row in db.execute(
            "SELECT DISTINCT source_id,charger_id FROM connector_state"
        ) if row[0] and row[1])
    if "collector_latest" in tables:
        identities.update((row[0], row[1]) for row in db.execute(
            """SELECT source_id,json_extract(payload, '$.charger_id')
               FROM collector_latest
               WHERE kind='connector' AND json_valid(payload)"""
        ) if row[0] and row[1])
    if TABLE in tables:
        identities.update((row[0], row[1]) for row in db.execute(
            f"SELECT source_id,charger_id FROM {TABLE}"
        ) if row[0] and row[1])

    candidates = sorted((s, c) for s, c in identities
                        if (source is None or s == source)
                        and (charger is None or c == charger))
    missing = "--source" if source is None else "--charger"
    if len(candidates) != 1:
        examples = ", ".join(f"{s}/{c}" for s, c in candidates[:8])
        if len(candidates) > 8:
            examples += ", ..."
        if not candidates:
            raise ValueError(f"Cannot infer {missing}: no matching charger identity; specify it explicitly")
        raise ValueError(f"Ambiguous {missing}: {len(candidates)} candidates ({examples}); specify it explicitly")
    return candidates[0]



def list_inventory(database: Path, *, source: str | None = None,
                   assigned: bool | None = None) -> list[dict]:
    """Read-only inventory with exact charger mappings and source-only entries.

    A source cursor is evidence of a source, not of a particular charger.
    Source-only entries therefore never fabricate charger IDs.
    """
    if source is not None:
        source = validate(source, "source")
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError("Existing collector warehouse required")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        sources: set[str] = set()
        for table in ("sources", "collector_cursors", "collector_receipts"):
            if table in tables:
                sources.update(row[0] for row in db.execute(
                    f"SELECT source_id FROM {table}"
                ) if row[0])
        identity_pairs: set[tuple[str, str]] = set()
        if "connector_state" in tables:
            identity_pairs.update((row[0], row[1]) for row in db.execute(
                "SELECT DISTINCT source_id, charger_id FROM connector_state"
            ) if row[0] and row[1])
        if "collector_latest" in tables:
            identity_pairs.update((row[0], row[1]) for row in db.execute(
                """SELECT source_id,json_extract(payload, '$.charger_id')
                   FROM collector_latest
                   WHERE kind='connector' AND json_valid(payload)"""
            ) if row[0] and row[1])
        labels: dict[tuple[str, str], str] = {}
        if TABLE in tables:
            labels = {(s, c): name for s, c, name in db.execute(
                f"SELECT source_id,charger_id,location FROM {TABLE}"
            )}
            identity_pairs.update(labels)
        sources.update(s for s, _ in identity_pairs)

        rows: list[dict] = []
        for s, c in sorted(identity_pairs):
            if source is not None and s != source:
                continue
            name = labels.get((s, c))
            if assigned is not None and (name is not None) != assigned:
                continue
            rows.append({"source_id": s, "charger_id": c, "location": name,
                         "assigned": name is not None})
        known = {s for s, _ in identity_pairs}
        if assigned is not True:
            for s in sorted(sources - known):
                if source is None or s == source:
                    rows.append({"source_id": s, "charger_id": None,
                                 "location": None, "assigned": False})
        return sorted(rows, key=lambda row: (
            row["source_id"], row["charger_id"] or ""
        ))


def manage(database: Path, action: str, *, source: str | None = None,
           charger: str | None = None, location: str | None = None) -> list[dict] | dict:
    """CRUD, with an explicit existing database and no mutation on list."""
    if action not in {"list", "set", "remove"}:
        raise ValueError("Unsupported location operation")
    if action != "list":
        source = validate(source, "source") if source is not None else None
        charger = validate(charger, "charger") if charger is not None else None
        if source is None and charger is None:
            raise ValueError("Specify at least one of --source or --charger")
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
        source, charger = _resolve_identity(db, source, charger, {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        })
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
