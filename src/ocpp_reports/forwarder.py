"""Durable at-least-once forwarding of Reports changefeed batches."""
from __future__ import annotations

import json
import sqlite3
import time
import logging
from pathlib import Path
from typing import Callable

import httpx

from .changefeed import changes_page


def local_evidence_source(database: Path) -> str:
    """Resolve the single underlying CSMS identity without forwarding its UUID."""
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise ValueError("Forwarder requires exactly one local CSMS source")
    return rows[0][0]


def confirmed_cursor(state: Path, source_id: str) -> int:
    if not state.exists():
        return 0
    with sqlite3.connect(state) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='forward_cursors'").fetchone():
            return 0
        row = db.execute("SELECT sequence FROM forward_cursors WHERE source_id=?",
                         (source_id,)).fetchone()
        return int(row[0]) if row else 0


def save_cursor(state: Path, source_id: str, cursor: int) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS forward_cursors (
            source_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL
        )""")
        db.execute("""INSERT INTO forward_cursors VALUES (?,?)
            ON CONFLICT(source_id) DO UPDATE SET sequence=excluded.sequence
            WHERE excluded.sequence >= forward_cursors.sequence""", (source_id, cursor))


def verify_source_binding(state: Path, node_id: str, evidence_source: str) -> None:
    """Persist mapping once, reject accidental identity switches without losing cursors."""
    state.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS source_bindings (
            node_id TEXT PRIMARY KEY, evidence_source TEXT NOT NULL
        )""")
        old = db.execute(
            "SELECT evidence_source FROM source_bindings WHERE node_id=?", (node_id,)
        ).fetchone()
        if old and old[0] != evidence_source:
            raise ValueError(
                f"CSMS evidence identity changed for {node_id}; explicit maintenance migration required"
            )
        db.execute(
            "INSERT OR IGNORE INTO source_bindings VALUES (?,?)",
            (node_id, evidence_source),
        )


def validate_endpoint(endpoint: str) -> None:
    if not (endpoint.startswith('https://') or endpoint.startswith('http://127.0.0.1:')):
        raise ValueError('Forwarder endpoint must use HTTPS (or loopback HTTP)')


def forward_once(database: Path, state: Path, source_id: str, endpoint: str,
                 token: str, *, limit: int = 500,
                 client: httpx.Client | None = None) -> int:
    """Return acknowledged sequence. Never advance after an unsuccessful response."""
    validate_endpoint(endpoint)
    if not source_id or not token:
        raise ValueError("Forwarding requires source identity, token and HTTPS endpoint (or loopback HTTP)")
    if database.resolve() == state.resolve():
        raise ValueError("Forwarder state must be separate from the warehouse")
    after = confirmed_cursor(state, source_id)
    evidence_source = local_evidence_source(database)
    # Bind a durable forwarding cursor to its original evidence identity.
    # A later CSMS identity change must not replay a new source under an old ACK.
    verify_source_binding(state, source_id, evidence_source)
    batch = changes_page(database, evidence_source, after=after, limit=limit)
    # Collector identity and cursor use the stable configured node name.
    # The local warehouse retains the CSMS UUID unchanged.
    batch["source_id"] = source_id
    if not batch["changes"]:
        return after
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=10)
    try:
        response = client.post(endpoint, json=batch,
                               headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        acknowledgement = response.json()
        if acknowledgement.get("source_id") != source_id or acknowledgement.get("ack") != batch["next"]:
            raise ValueError("Collector acknowledgement does not match the sent batch")
        save_cursor(state, source_id, batch["next"])
        return batch["next"]
    finally:
        if owns_client:
            client.close()


def forward(database: Path, state: Path, source_id: str, endpoint: str, token: str,
            *, interval: float = 2.0, limit: int = 500,
            once: bool = False, sleep: Callable[[float], None] = time.sleep) -> None:
    """Drain pending batches, then poll. Raise failures for systemd restart."""
    if interval <= 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid forwarding interval or batch limit")
    failures = 0
    while True:
        try:
            previous = confirmed_cursor(state, source_id)
            current = forward_once(database, state, source_id, endpoint, token, limit=limit)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            transient = isinstance(exc, httpx.TransportError) or (
                isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {408, 429, 500, 502, 503, 504})
            if once or not transient:
                raise
            failures += 1
            delay = min(300.0, max(interval, 5.0) * (2 ** min(failures - 1, 10)))
            logging.getLogger(__name__).warning("Collector unavailable (%s); retry in %.0fs", type(exc).__name__, delay)
            sleep(delay)
            continue
        failures = 0
        if current > previous:
            continue
        if once:
            return
        sleep(interval)
