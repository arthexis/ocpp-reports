"""Local-only, repeatable, source-scoped collector maintenance.

Hold acquisition shares the batch writer lock: on return, earlier batches have
committed and later batches cannot write. All collector workers must run the
hold-aware release before using this tool. Coordination receipts and release
authorization remain operator gates; this module never starts a forwarder.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .warehouse.collector_schema import ensure_collector_schema

DATA_TABLES = ("collector_changes", "collector_latest", "collector_cursors")
HOLD_TABLE = "collector_source_holds"


def _connect(database: Path, *, readonly: bool = False) -> sqlite3.Connection:
    # mode=rw refuses accidental creation or a mistyped database path.
    mode = "ro" if readonly else "rw"
    db = sqlite3.connect(database.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=30)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def _schema(db: sqlite3.Connection) -> tuple:
    return (db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                       "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall())


def _check_schema(db: sqlite3.Connection) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if not r[0].startswith("sqlite_")}
    if tables != {*DATA_TABLES, HOLD_TABLE}:
        raise ValueError("Unrecognized collector tables; reset requires schema review")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
        raise ValueError("Collector triggers require reset review")
    expected = {
        "collector_changes": ["source_id", "sequence", "kind", "record_key", "payload"],
        "collector_latest": ["source_id", "kind", "record_key", "sequence", "payload"],
        "collector_cursors": ["source_id", "sequence"],
        HOLD_TABLE: ["source_id", "held_at"],
    }
    for table, columns in expected.items():
        if [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')] != columns:
            raise ValueError("Unrecognized collector columns; reset requires schema review")


def _digest(db: sqlite3.Connection, source: str | None = None) -> str:
    """Stable logical digest, independent of SQLite file layout and row order."""
    result = hashlib.sha256()
    for table in (*DATA_TABLES, HOLD_TABLE):
        result.update(table.encode())
        count = len(db.execute(f'PRAGMA table_info("{table}")').fetchall())
        order = ",".join(str(i) for i in range(1, count + 1))
        query = f'SELECT * FROM "{table}"'
        params = ()
        if source is not None:
            query += " WHERE source_id<>?"
            params = (source,)
        for row in db.execute(query + " ORDER BY " + order, params):
            result.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            result.update(b"\n")
    return result.hexdigest()


def _status(db: sqlite3.Connection, source: str) -> dict:
    return {"source_id": source,
            "held": bool(db.execute(f"SELECT 1 FROM {HOLD_TABLE} WHERE source_id=?",
                                    (source,)).fetchone()),
            "rows": {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE source_id=?",
                                       (source,)).fetchone()[0] for table in DATA_TABLES},
            "cursor": next((r[0] for r in db.execute(
                "SELECT sequence FROM collector_cursors WHERE source_id=?", (source,))), None)}


def maintain(database: Path, source: str, action: str, backup: Path | None = None) -> dict:
    if not source.strip() or action not in {"status", "hold", "release", "reset"}:
        raise ValueError("Explicit nonempty source and valid action required")
    if action == "status":
        with closing(_connect(database, readonly=True)) as db:
            db.execute("BEGIN")
            _check_schema(db)
            return _status(db, source)
    with closing(_connect(database)) as db, db:
        if action == "hold":
            ensure_collector_schema(db)
            db.commit()
        db.execute("BEGIN IMMEDIATE")
        _check_schema(db)
        if action == "hold":
            db.execute(f"INSERT OR IGNORE INTO {HOLD_TABLE} VALUES (?,?)",
                       (source, datetime.now(timezone.utc).isoformat()))
        elif action == "release":
            db.execute(f"DELETE FROM {HOLD_TABLE} WHERE source_id=?", (source,))
        else:
            if not _status(db, source)["held"]:
                raise ValueError("Source must be held before reset")
            if backup is None:
                raise ValueError("A new private backup path is required")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Collector integrity check failed")
            before = _status(db, source)
            signature, other_sources, all_rows = _schema(db), _digest(db, source), _digest(db)
            # The reserved writer lock prevents ALL collector writes during the
            # backup, verification and deletion. A separate reader can back up
            # the last committed state without backing up a write transaction.
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(_connect(database, readonly=True)) as original:
                with closing(sqlite3.connect(backup)) as snapshot:
                    original.backup(snapshot)
                    if snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise ValueError("Backup integrity check failed")
                    if _schema(snapshot) != signature or _digest(snapshot) != all_rows:
                        raise ValueError("Backup does not match locked collector state")
            with backup.open("rb") as stream:
                os.fsync(stream.fileno())
            parent_fd = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            for table in DATA_TABLES:
                db.execute(f"DELETE FROM {table} WHERE source_id=?", (source,))
            after = _status(db, source)
            if any(after["rows"].values()) or not after["held"]:
                raise ValueError("Source reset verification failed")
            if _schema(db) != signature or _digest(db, source) != other_sources:
                raise ValueError("Reset changed schema or another source")
            return {"status": "source_reset_held", "before": before, "after": after,
                    "backup": str(backup), "backup_verified": True,
                    "other_sources_unchanged": True}
        return _status(db, source)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "hold", "reset", "release"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    try:
        result = maintain(args.database, args.source, args.action, args.backup)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Collector maintenance failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
