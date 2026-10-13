"""SQLite-backed immutable email outbox with crash-safe claims."""
from __future__ import annotations
import hashlib
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    key TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    claimed_at INTEGER,
    sent_at INTEGER,
    last_error TEXT
);
"""

@dataclass(frozen=True)
class Delivery:
    key: str
    recipient: str
    subject: str
    body: str
    attempts: int

class Outbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def enqueue(self, *, recipient: str, subject: str, body: str, revision: str) -> str:
        if not recipient or not revision:
            raise ValueError("recipient and report revision are required")
        key = hashlib.sha256(("\\0".join((recipient, subject, revision))).encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deliveries (key,recipient,subject,body,state,created_at) VALUES (?,?,?,?,?,?)",
                       (key, recipient, subject, body, "pending", int(time.time())))
            row = db.execute("SELECT recipient,subject,body FROM deliveries WHERE key=?", (key,)).fetchone()
            if tuple(row) != (recipient, subject, body):
                raise ValueError("Idempotency collision: report revision or content changed")
        return key

    def claim(self) -> Delivery | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE state='pending' ORDER BY created_at,key LIMIT 1").fetchone()
            if row is None:
                return None
            updated = db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,claimed_at=? WHERE key=? AND state='pending'", (int(time.time()), row["key"]))
            if updated.rowcount != 1:
                return None
            return Delivery(row["key"], row["recipient"], row["subject"], row["body"], row["attempts"] + 1)

    def mark_sent(self, key: str):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='sent',sent_at=?,last_error=NULL WHERE key=? AND state='sending'", (int(time.time()), key))

    def mark_failed(self, key: str, error: str):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='failed',last_error=? WHERE key=? AND state='sending'", (error[:2000], key))

    def retry(self, key: str):
        """Manual retry after checking inbox; may duplicate a previously accepted email."""
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='pending',last_error=NULL WHERE key=? AND state IN ('failed','sending')", (key,))

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM deliveries ORDER BY created_at,key")]
