#!/usr/bin/env python3
"""H0 offline SQLite inventory. Metadata only; never emits row values or identifiers."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

TABLES = ("events", "transactions", "ocpp_transaction", "ocpp_metervalue", "django_migrations")
TIME_FIELDS = ("received_at", "timestamp", "started_at", "start_time", "stopped_at", "stop_time")


def inspect(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    # Operate on an existing, quiescent backup only. immutable=1 ignores WAL;
    # using it on an active database could silently omit committed data.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"SQLite quick_check failed: {check}")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        available = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        result = {
            "format": "h0-sqlite-metadata/v1",
            "artifact": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
            "user_version": version,
            "integrity": check,
            "tables": {},
            "limitations": [
                "Metadata only; no personal data or raw rows exported",
                "Timestamp timezone and energy units not inferred",
                "Snapshot must be quiescent and self-contained (no active WAL)",
            ],
        }
        for table in TABLES:
            if table not in available:
                continue
            # Only hardcoded identifiers may appear in SQL.
            cols = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            bounds = {}
            for field in TIME_FIELDS:
                if field in cols:
                    first, last = db.execute(
                        f'SELECT MIN("{field}"), MAX("{field}") FROM "{table}"'
                    ).fetchone()
                    bounds[field] = {"min": first, "max": last}
            result["tables"][table] = {
                "columns": cols,
                "row_count": count,
                "time_bounds": bounds,
            }
        return result
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backups", nargs="+", type=Path, help="Quiescent SQLite backup copies")
    parser.add_argument("--output", type=Path, help="New manifest file (never overwritten)")
    args = parser.parse_args()
    manifest = {"format": "h0-inventory/v1", "artifacts": [inspect(p) for p in args.backups]}
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
