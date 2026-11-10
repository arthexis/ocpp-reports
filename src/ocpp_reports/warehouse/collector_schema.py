"""Idempotent collector table initialization, separate from request processing."""
from __future__ import annotations
import sqlite3
import uuid


def ensure_collector_schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS collector_cursors (
            source_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collector_receipts (\n            source_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, sequence INTEGER NOT NULL\n        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collector_changes (
            source_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            kind TEXT NOT NULL, record_key TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(source_id, sequence)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collector_latest (
            source_id TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL,
            sequence INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(source_id, kind, record_key)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collector_charger_locations (
            source_id TEXT NOT NULL, charger_id TEXT NOT NULL, location TEXT NOT NULL,
            PRIMARY KEY(source_id, charger_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collector_source_holds (
            source_id TEXT PRIMARY KEY, held_at TEXT NOT NULL
        )""")


def collector_generation(db: sqlite3.Connection) -> str:
    """Durable identity of a Collector database; unchanged across process restarts."""
    db.execute("""CREATE TABLE IF NOT EXISTS collector_identity (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        generation TEXT NOT NULL
    )""")
    db.execute("INSERT OR IGNORE INTO collector_identity VALUES (1, ?)", (str(uuid.uuid4()),))
    return db.execute("SELECT generation FROM collector_identity WHERE singleton=1").fetchone()[0]


def ensure_recovery_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS collector_recovery (
        source_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
        session_id TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL CHECK(state IN ('active','complete'))
    )""")
