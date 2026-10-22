"""Durable at-least-once forwarding of Reports changefeed batches."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

import httpx

from .changefeed import changes_page


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


def forward_once(database: Path, state: Path, source_id: str, endpoint: str,
                 token: str, *, limit: int = 500,
                 client: httpx.Client | None = None) -> int:
    """Return acknowledged sequence. Never advance after an unsuccessful response."""
    if not source_id or not token or not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1:"):
        raise ValueError("Forwarding requires source identity, token and HTTPS endpoint (or loopback HTTP)")
    if database.resolve() == state.resolve():
        raise ValueError("Forwarder state must be separate from the warehouse")
    after = confirmed_cursor(state, source_id)
    batch = changes_page(database, source_id, after=after, limit=limit)
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
    last_diagnostics = 0.0
    while True:
        previous = confirmed_cursor(state, source_id)
        current = forward_once(database, state, source_id, endpoint, token, limit=limit)
        if time.monotonic() - last_diagnostics >= 60 or once:
            last_diagnostics = time.monotonic()
            try:
                from .active_csms import discover_active_csms
                from .diagnostic_archive import forward_archives
                data_dir, _ = discover_active_csms()
                diagnostic_url = endpoint.removesuffix("/api/replication") + "/api/diagnostics"
                forward_archives(Path(data_dir), Path("/var/lib/ocpp-csms/diagnostics"),
                                 state.parent / "diagnostic-forwarding", diagnostic_url, source_id, token)
            except (ValueError, OSError, ImportError, RuntimeError, httpx.HTTPError) as exc:
                logging.getLogger(__name__).warning("Diagnostic forwarding deferred: %s", type(exc).__name__)
        if current > previous:
            continue
        if once:
            return
        sleep(interval)
