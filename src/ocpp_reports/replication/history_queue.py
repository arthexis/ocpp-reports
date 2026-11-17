"""Explicit bounded historical sample enqueue; never rewinds a replication cursor.

Read-only by default. Queueing immutable existing local samples is separate
from CSMS export and never rewrites the original evidence. Remote ACKs advance
only via the normal authenticated Forwarder/Collector handshake.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def queue_history(database: str | Path, source_id: str, *,
                  after_event: int = 0, after_index: int = -1,
                  limit: int = 500, apply: bool = False) -> dict:
    if (not isinstance(source_id, str) or not source_id
            or type(after_event) is not int or after_event < 0
            or type(after_index) is not int or after_index < -1
            or type(limit) is not int or not 1 <= limit <= 500
            or type(apply) is not bool):
        raise ValueError("Explicit source, bounded cursor, limit, and apply flag required")
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Missing local warehouse: {path}")
    url = path.as_uri() + ("?mode=rw" if apply else "?mode=ro")
    with sqlite3.connect(url, uri=True) as db:
        if not apply:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sources", "energy_samples", "replication_changes"} <= tables:
            raise ValueError("Warehouse missing required local replication tables")
        if not db.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
            raise ValueError("Source not found in local CSMS warehouse")
        rows = db.execute("""
            SELECT event_id,sample_index,payload FROM energy_samples
            WHERE source_id=? AND (event_id>? OR (event_id=? AND sample_index>?))
            ORDER BY event_id,sample_index LIMIT ?
        """, (source_id, after_event, after_event, after_index, limit + 1)).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        queued = 0
        pending = 0
        for event_id, index, payload in page:
            key = f"{event_id}:{index}"
            old = db.execute(
                "SELECT payload FROM replication_changes WHERE source_id=?"
                " AND kind='meter_sample' AND record_key=? ORDER BY sequence DESC LIMIT 1",
                (source_id, key),
            ).fetchone()
            if old is not None:
                if old[0] != payload:
                    raise ValueError("Conflicting journal payload for historical sample")
                continue
            pending += 1
            if apply:
                db.execute("""INSERT INTO replication_changes
                    (source_id,kind,record_key,payload) VALUES (?,'meter_sample',?,?)""",
                    (source_id, key, payload))
                queued += 1
        next_cursor = {"event_id": page[-1][0], "sample_index": page[-1][1]} if page else {
            "event_id": after_event, "sample_index": after_index
        }
        return {"source_id": source_id, "dry_run": not apply,
                "scanned": len(page), "pending": pending, "queued": queued,
                "next": next_cursor, "more": more}


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded historical meter sample enqueue")
    parser.add_argument("--database", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--after-event", type=int, default=0)
    parser.add_argument("--after-index", type=int, default=-1)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--apply", action="store_true", help="Explicitly enqueue; default is read-only")
    args = parser.parse_args()
    result = queue_history(
        args.database, args.source_id, after_event=args.after_event,
        after_index=args.after_index, limit=args.limit, apply=args.apply,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
