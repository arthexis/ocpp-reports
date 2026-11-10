"""Durable at-least-once forwarding of Reports changefeed batches."""
from __future__ import annotations

import json
import sqlite3
import time
import logging
from pathlib import Path
from typing import Callable

import httpx

from ..changefeed import changes_page


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



def collector_status(state: Path, source_id: str, endpoint: str, token: str,
                     client: httpx.Client) -> dict:
    """Probe even while idle; bind Collector generation durably, fail closed on replacement."""
    if not endpoint.endswith("/api/replication"):
        raise ValueError("Invalid collector replication endpoint")
    response = client.post(
        endpoint + "/status", json={"source_id": source_id,
                                    "forward_ack": confirmed_cursor(state, source_id)},
        headers={"Authorization": "Bearer " + token},
    )
    response.raise_for_status()
    status = response.json()
    if (status.get("schema") != "ocpp-reports/status/v1"
            or status.get("source_id") != source_id
            or not isinstance(status.get("generation"), str)
            or not status["generation"]
            or type(status.get("cursor")) is not int
            or status["cursor"] < 0
            or type(status.get("held")) is not bool
            or type(status.get("has_evidence")) is not bool):
        raise ValueError("Invalid authenticated Collector status")
    if status["held"]:
        raise ValueError("Collector source held")
    if type(status.get("reset_required", False)) is not bool:
        raise ValueError("Invalid RESET_REQUIRED signal")
    if status.get("reset_required"):
        return status
    state.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS collector_bindings (
            source_id TEXT PRIMARY KEY, generation TEXT NOT NULL
        )""")
        prior = db.execute(
            "SELECT generation FROM collector_bindings WHERE source_id=?",
            (source_id,),
        ).fetchone()
        active_recovery = (db.execute("SELECT 1 FROM sqlite_master WHERE name='forward_recovery'").fetchone()
                           and db.execute("SELECT 1 FROM forward_recovery WHERE source_id=? AND generation=?",
                                          (source_id, status["generation"])).fetchone())
        if prior and prior[0] != status["generation"] and not (status.get("reset_required") or active_recovery):
            raise ValueError("Collector generation changed; recovery handshake required")
        if not prior and confirmed_cursor(state, source_id) > status["cursor"] and not status.get("reset_required"):
            raise ValueError("Cannot bind existing Forwarder ACK to an older Collector")
        db.execute("INSERT OR IGNORE INTO collector_bindings VALUES (?,?)",
                   (source_id, status["generation"]))
    return status



def recovery_once(database: Path, state: Path, source_id: str, evidence_source: str,
                  endpoint: str, token: str, generation: str, client: httpx.Client,
                  *, limit: int) -> int:
    """Durable recovery cursor independent of monotonic normal ACK."""
    headers = {"Authorization": "Bearer " + token}
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS forward_recovery (
            source_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
            session_id TEXT, cursor INTEGER NOT NULL DEFAULT 0
        )""")
        old = db.execute("SELECT generation,session_id,cursor FROM forward_recovery WHERE source_id=?",
                         (source_id,)).fetchone()
        if old and old[0] != generation:
            raise ValueError("Recovery generation changed")
        if not old:
            db.execute("INSERT INTO forward_recovery VALUES (?,?,NULL,0)",
                       (source_id, generation))
    with sqlite3.connect(state) as db:
        row = db.execute("SELECT session_id,cursor FROM forward_recovery WHERE source_id=?",
                         (source_id,)).fetchone()
    session, cursor = row
    start = client.post(endpoint + "/recovery/start",
                        json={"source_id": source_id, "generation": generation,
                              "session_id": session},
                        headers=headers)
    start.raise_for_status()
    receipt = start.json()
    if (receipt.get("schema") != "ocpp-reports/recovery/v1"
            or receipt.get("source_id") != source_id
            or receipt.get("generation") != generation
            or not isinstance(receipt.get("session_id"), str)
            or not receipt["session_id"]
            or type(receipt.get("cursor")) is not int
            or receipt["cursor"] < cursor
            or (session is not None and receipt["session_id"] != session)):
        raise ValueError("Invalid recovery session receipt")
    session = receipt["session_id"]
    cursor = receipt["cursor"]
    with sqlite3.connect(state) as db:
        db.execute("UPDATE forward_recovery SET session_id=?,cursor=? WHERE source_id=?",
                   (session, cursor, source_id))
    batch = changes_page(database, evidence_source, after=cursor, limit=limit)
    if not batch["changes"]:
        if cursor == 0:
            raise ValueError("Cannot recover without retained journal evidence")
        finish = client.post(endpoint + "/recovery/finish",
                             json={"source_id": source_id, "generation": generation,
                                   "session_id": session, "cursor": cursor}, headers=headers)
        finish.raise_for_status()
        done = finish.json()
        if (done.get("schema") != "ocpp-reports/recovery/v1"
                or done.get("source_id") != source_id
                or done.get("generation") != generation
                or done.get("session_id") != session
                or done.get("cursor") != cursor
                or done.get("complete") is not True):
            raise ValueError("Invalid recovery completion receipt")
        with sqlite3.connect(state) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE collector_bindings SET generation=? WHERE source_id=?",
                       (generation, source_id))
            db.execute("DELETE FROM forward_recovery WHERE source_id=?", (source_id,))
        return confirmed_cursor(state, source_id)
    batch["source_id"] = source_id
    batch["recovery_generation"] = generation
    batch["recovery_session"] = session
    response = client.post(endpoint, json=batch, headers=headers)
    response.raise_for_status()
    ack = response.json()
    if ack.get("source_id") != source_id or ack.get("ack") != batch["next"]:
        raise ValueError("Recovery acknowledgement mismatch")
    with sqlite3.connect(state) as db:
        db.execute("UPDATE forward_recovery SET cursor=? WHERE source_id=? AND cursor<=?",
                   (batch["next"], source_id, batch["next"]))
    return confirmed_cursor(state, source_id)


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
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=10)
    try:
        status = collector_status(state, source_id, endpoint, token, client)
        with sqlite3.connect(state) as db:
            recovery_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='forward_recovery'").fetchone()
            pending = db.execute("SELECT 1 FROM forward_recovery WHERE source_id=?",
                                 (source_id,)).fetchone() if recovery_table else None
        if status.get("reset_required") or pending:
            return recovery_once(database, state, source_id, evidence_source,
                                 endpoint, token, status["generation"], client, limit=limit)
        if not batch["changes"]:
            return after
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

