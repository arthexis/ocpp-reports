"""Read-only replication batches for M3 forwarders.

The journal sequence is local to a warehouse. A source_id is never inferred
from the host or a client-supplied display name.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def changes_page(database: str | Path, source_id: str, *, after: int = 0,
                 limit: int = 500) -> dict:
    if not source_id or after < 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid replication page parameters")
    path = Path(database).resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT sequence,kind,record_key,payload FROM replication_changes "
            "WHERE source_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (source_id, after, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        batch = rows[:limit]
        return {
            "schema": "ocpp-reports/replication/v1",
            "source_id": source_id,
            "after": after,
            "next": batch[-1]["sequence"] if batch else after,
            "more": more,
            "changes": [
                {"sequence": row["sequence"], "kind": row["kind"],
                 "record_key": row["record_key"], "payload": json.loads(row["payload"])}
                for row in batch
            ],
        }
