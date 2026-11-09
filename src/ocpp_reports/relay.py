"""Collector-to-collector replication of a single, explicitly selected source.

This reads collector evidence without rewriting the original source identity.
One durable acknowledgement cursor is maintained per destination/source pair.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

from .replication.forwarder import confirmed_cursor, save_cursor, validate_endpoint


def relay_page(database: Path, source_id: str, *, after: int = 0, limit: int = 500) -> dict:
    if not source_id or after < 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid relay source, cursor or limit")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT sequence,kind,record_key,payload FROM collector_changes "
            "WHERE source_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (source_id, after, limit + 1),
        ).fetchall()
        page = rows[:limit]
        return {
            "schema": "ocpp-reports/replication/v1",
            "source_id": source_id,
            "after": after,
            "next": page[-1]["sequence"] if page else after,
            "more": len(rows) > limit,
            "changes": [
                {"sequence": r["sequence"], "kind": r["kind"],
                 "record_key": r["record_key"], "payload": json.loads(r["payload"])}
                for r in page
            ],
        }


def relay_once(database: Path, state: Path, source_id: str, endpoint: str,
               token: str, *, destination_id: str, limit: int = 500,
               client: httpx.Client | None = None) -> int:
    """Send one page; persist ACK only after the downstream collector confirms it."""
    validate_endpoint(endpoint)
    if not source_id or not token or not destination_id:
        raise ValueError("Source, destination and token are required")
    if source_id == destination_id:
        raise ValueError("Refusing self-replication identity")
    if database.resolve() == state.resolve():
        raise ValueError("Relay state must be separate from warehouse")
    # A destination-qualified cursor prevents unrelated collectors sharing ACK state.
    cursor_key = json.dumps([destination_id, source_id], separators=(",", ":"))
    after = confirmed_cursor(state, cursor_key)
    batch = relay_page(database, source_id, after=after, limit=limit)
    if not batch["changes"]:
        return after
    owned = client is None
    if owned:
        client = httpx.Client(timeout=10, follow_redirects=False)
    try:
        response = client.post(endpoint, json=batch,
                               headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        receipt = response.json()
        if receipt.get("source_id") != source_id or receipt.get("ack") != batch["next"]:
            raise ValueError("Collector acknowledgement does not match batch")
        save_cursor(state, cursor_key, batch["next"])
        return batch["next"]
    finally:
        if owned:
            client.close()
