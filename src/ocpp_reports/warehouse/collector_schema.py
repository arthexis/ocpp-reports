"""Idempotent collector table initialization, separate from request processing."""
from __future__ import annotations
import sqlite3


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
        db.execute("""CREATE TABLE IF NOT EXISTS collector_source_holds (
            source_id TEXT PRIMARY KEY, held_at TEXT NOT NULL
        )""")
