"""Read the transaction projection reconciled atomically by ingest."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def current_transactions(database: str | Path, *, source_id: str | None = None) -> list[dict[str, Any]]:
    """One current snapshot per source/CP/transaction; never sum revisions.

    Ingest maintains this table atomically with evidence and its cursor.
    This is an observation projection, not a guarantee of physical finality.
    """
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='current_transactions'"
        ).fetchone()
        query = """
            SELECT source_id, charger_id, transaction_id, page_after, page_next,
                   fingerprint, payload, imported_at
            FROM {table}
        """
        query = query.format(table="current_transactions" if table_exists else "transaction_observations")
        parameters: tuple[str, ...] = ()
        if source_id is not None:
            query += " WHERE source_id = ?"
            parameters = (source_id,)
        query += """
            ORDER BY source_id, charger_id, transaction_id,
                     page_next DESC, page_after DESC, rowid DESC
        """
        rows = db.execute(query, parameters).fetchall()
    selected: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        identity = (row["source_id"], row["charger_id"], row["transaction_id"])
        if identity in selected:
            continue
        selected[identity] = {
            "source_id": row["source_id"],
            "transaction": json.loads(row["payload"]),
            "evidence": {
                "page_after": row["page_after"],
                "page_next": row["page_next"],
                "fingerprint": row["fingerprint"],
                "imported_at": row["imported_at"],
            },
        }
    return list(selected.values())
