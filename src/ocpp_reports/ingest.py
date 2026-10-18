"""Offline, read-only CSMS export ingestion into Reports-owned SQLite."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .csms_export import validate_export


def load_page(path: str | Path) -> dict[str, Any]:
    """Read a local producer page without modifying its source."""
    with Path(path).open("r", encoding="utf-8") as stream:
        return validate_export(json.load(stream))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,\n        last_ingested_at TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
        source_id TEXT NOT NULL, event_id INTEGER NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(source_id, event_id)
    );
    CREATE TABLE IF NOT EXISTS energy_samples (
        source_id TEXT NOT NULL, event_id INTEGER NOT NULL,
        sample_index INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(source_id, event_id, sample_index)
    );
    CREATE TABLE IF NOT EXISTS current_transactions (
        source_id TEXT NOT NULL, charger_id TEXT NOT NULL,
        transaction_id INTEGER NOT NULL, page_after INTEGER NOT NULL,
        page_next INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL, imported_at TEXT NOT NULL,
        PRIMARY KEY(source_id, charger_id, transaction_id)
    );
    CREATE TABLE IF NOT EXISTS transaction_observations (
        source_id TEXT NOT NULL, charger_id TEXT NOT NULL,
        transaction_id INTEGER NOT NULL, page_after INTEGER NOT NULL,
        page_next INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL, imported_at TEXT NOT NULL,
        PRIMARY KEY(source_id, charger_id, transaction_id, page_after, page_next, fingerprint)
    );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
    if "last_ingested_at" not in columns:
        db.execute("ALTER TABLE sources ADD COLUMN last_ingested_at TEXT")


def ingest_page(database: str | Path, page: dict[str, Any], *, imported_at: str | None = None) -> int:
    """Persist one validated sequential page atomically; return current cursor.

    Same-cursor replays may carry newer transaction observations. Immutable
    event/sample conflicts fail, but transaction observations remain additive.
    """
    validate_export(page)
    data = page["data"]
    source = data["source_id"]
    cursor = data["cursor"]
    timestamp = imported_at or datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO sources(source_id) VALUES (?)", (source,))
        current = db.execute("SELECT cursor FROM sources WHERE source_id=?", (source,)).fetchone()[0]
        if cursor["after"] > current:
            raise ValueError(f"Export cursor gap: expected {current}, got {cursor['after']}")
        if cursor["after"] < current and cursor["next"] > current:
            raise ValueError("Overlapping export cursor page")
        # A replay of an older page may only verify existing immutable evidence.
        # A new page must begin at the committed cursor; no silent overlaps.
        if cursor["next"] < cursor["after"]:
            raise ValueError("Export cursor regressed")
        if data["events"]:
            ids = [event["id"] for event in data["events"]]
            if ids != sorted(set(ids)) or ids[0] <= cursor["after"] or ids[-1] != cursor["next"]:
                raise ValueError("Event IDs do not match export cursor boundaries")
        elif cursor["next"] != cursor["after"]:
            raise ValueError("Empty event page advanced the cursor")
        for event in data["events"]:
            identity = event["id"]
            encoded = _canonical(event)
            prior = db.execute("SELECT payload FROM events WHERE source_id=? AND event_id=?",
                               (source, identity)).fetchone()
            if prior is not None and prior[0] != encoded:
                raise ValueError(f"Conflicting immutable event {identity}")
            if cursor["after"] < current and prior is None:
                raise ValueError(f"Previously imported event {identity} is missing")
            db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (source, identity, encoded))
        page_event_ids = {event["id"] for event in data["events"]}
        for sample in data["energy"]:
            if sample["source_event_id"] not in page_event_ids:
                raise ValueError("Energy sample references event outside page")
            event_id, index = sample["source_event_id"], sample["sample_index"]
            encoded = _canonical(sample)
            prior = db.execute("SELECT payload FROM energy_samples WHERE source_id=? AND event_id=? AND sample_index=?",
                               (source, event_id, index)).fetchone()
            if prior is not None and prior[0] != encoded:
                raise ValueError(f"Conflicting energy sample {event_id}/{index}")
            if cursor["after"] < current and prior is None:
                raise ValueError(f"Previously imported sample {event_id}/{index} is missing")
            db.execute("INSERT OR IGNORE INTO energy_samples VALUES (?,?,?,?)",
                       (source, event_id, index, encoded))
        for tx in data["transactions"]:
            encoded = _canonical(tx)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            values = (source, tx["charger_id"], tx["id"], cursor["after"], cursor["next"],
                      digest, encoded, timestamp)
            db.execute("""INSERT OR IGNORE INTO transaction_observations
                VALUES (?,?,?,?,?,?,?,?)""", values)
            # Reconcile the materialized projection in the same transaction as
            # evidence and cursor. Old-page replays cannot regress its state.
            current_tx = db.execute("""SELECT page_next, page_after, fingerprint
                FROM current_transactions
                WHERE source_id=? AND charger_id=? AND transaction_id=?""",
                (source, tx["charger_id"], tx["id"])).fetchone()
            position = (cursor["next"], cursor["after"])
            if current_tx is None or position > (current_tx[0], current_tx[1]) or (
                position == (current_tx[0], current_tx[1]) and digest != current_tx[2]
            ):
                db.execute("""INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id,charger_id,transaction_id) DO UPDATE SET
                    page_after=excluded.page_after, page_next=excluded.page_next,
                    fingerprint=excluded.fingerprint, payload=excluded.payload,
                    imported_at=excluded.imported_at""", values)
        if cursor["after"] == current:
            db.execute("UPDATE sources SET cursor=? WHERE source_id=?", (cursor["next"], source))
        db.execute("UPDATE sources SET last_ingested_at=? WHERE source_id=?", (timestamp, source))
        return db.execute("SELECT cursor FROM sources WHERE source_id=?", (source,)).fetchone()[0]
