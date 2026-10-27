"""Read current transactions from local ingestion and replicated collectors."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def current_transactions(database: str | Path, *, source_id: str | None = None) -> list[dict[str, Any]]:
    """One current snapshot per source/CP/transaction; never sum revisions.

    Local ingest maintains its projection atomically; collectors store latest replicas.
    This is an observation projection, not a guarantee of physical finality.
    """
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        local_table = ("current_transactions" if "current_transactions" in tables
                       else "transaction_observations" if "transaction_observations" in tables
                       else None)
        rows = []
        if local_table is not None:
            query = (
                "SELECT source_id, charger_id, transaction_id, page_after, page_next, "
                "fingerprint, payload, imported_at FROM " + local_table
            )
            parameters: tuple[str, ...] = ()
            if source_id is not None:
                query += " WHERE source_id = ?"
                parameters = (source_id,)
            query += (
                " ORDER BY source_id, charger_id, transaction_id, "
                "page_next DESC, page_after DESC, rowid DESC"
            )
            rows = db.execute(query, parameters).fetchall()
        remote_rows = []
        if "collector_latest" in tables:
            remote_query = (
                "SELECT source_id, sequence, payload FROM collector_latest "
                "WHERE kind='transaction'"
            )
            remote_args: tuple[str, ...] = ()
            if source_id is not None:
                remote_query += " AND source_id=?"
                remote_args = (source_id,)
            remote_rows = db.execute(remote_query, remote_args).fetchall()
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
    # Collector records retain their own replication sequence and are read-only.
    # Prefer the local projection when the same identity is present in both paths.
    for row in remote_rows:
        transaction = json.loads(row["payload"])
        identity = (row["source_id"], transaction["charger_id"], transaction["id"])
        if identity in selected:
            continue
        encoded = json.dumps(transaction, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        selected[identity] = {
            "source_id": row["source_id"],
            "transaction": transaction,
            "evidence": {
                "page_after": None,
                "page_next": row["sequence"],
                "fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                "imported_at": None,
                "replication_sequence": row["sequence"],
            },
        }
    return list(selected.values())
