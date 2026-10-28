"""Warehouse-local schema ownership; non-destructive idempotent preparation."""
from __future__ import annotations
import sqlite3

def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
        last_ingested_at TEXT
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
    db.executescript("""
    CREATE TABLE IF NOT EXISTS replication_changes (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        record_key TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_replication_source_sequence
        ON replication_changes(source_id, sequence);
    CREATE TRIGGER IF NOT EXISTS replicate_event_insert
    AFTER INSERT ON events BEGIN
        INSERT INTO replication_changes(source_id,kind,record_key,payload)
        VALUES (NEW.source_id,'event',CAST(NEW.event_id AS TEXT),NEW.payload);
    END;
    CREATE TRIGGER IF NOT EXISTS replicate_transaction_insert
    AFTER INSERT ON current_transactions BEGIN
        INSERT INTO replication_changes(source_id,kind,record_key,payload)
        VALUES (NEW.source_id,'transaction',
                NEW.charger_id || ':' || NEW.transaction_id,NEW.payload);
    END;
    CREATE TRIGGER IF NOT EXISTS replicate_transaction_update
    AFTER UPDATE OF payload ON current_transactions
    WHEN OLD.payload != NEW.payload BEGIN
        INSERT INTO replication_changes(source_id,kind,record_key,payload)
        VALUES (NEW.source_id,'transaction',
                NEW.charger_id || ':' || NEW.transaction_id,NEW.payload);
    END;
    """)
    # Backfill projections from existing observation history when upgrading
    # a Reports warehouse. Never discard its original evidence.
    db.execute("""
        INSERT OR IGNORE INTO current_transactions
        SELECT source_id, charger_id, transaction_id, page_after, page_next,
               fingerprint, payload, imported_at
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY source_id, charger_id, transaction_id
                ORDER BY page_next DESC, page_after DESC, rowid DESC
            ) AS rank FROM transaction_observations
        ) WHERE rank = 1
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
    if "last_ingested_at" not in columns:
        db.execute("ALTER TABLE sources ADD COLUMN last_ingested_at TEXT")

