"""SQLite-backed immutable email outbox with crash-safe claims."""
from __future__ import annotations
import hashlib
import os
import tempfile
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
CREATE TABLE IF NOT EXISTS report_periods (
    period_key TEXT PRIMARY KEY,
    delivery_key TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

@dataclass(frozen=True)
class Delivery:
    key: str
    recipient: str
    subject: str
    body: str
    attempts: int
    attachment_name: str | None = None
    attachment_path: Path | None = None
    attachment_sha256: str | None = None

class Outbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            for name in ("attachment_name", "attachment_sha256"):
                if name not in columns:
                    db.execute(f"ALTER TABLE deliveries ADD COLUMN {name} TEXT")
        if self.path.parent == Path("/var/lib/ocpp-reports"):
            self.path.chmod(0o660)

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

    def attachment_path(self, digest: str) -> Path:
        return self.path.parent / "attachments" / digest

    def enqueue_attachment(self, *, recipient: str, subject: str, body: str,
                           filename: str, content: bytes) -> str:
        """Store immutable content before atomically registering the delivery."""
        if not recipient or "@" not in recipient or any(ch in recipient for ch in (chr(10), chr(13))):
            raise ValueError("Invalid recipient")
        if not filename or Path(filename).name != filename or not content:
            raise ValueError("Invalid attachment")
        digest = hashlib.sha256(content).hexdigest()
        key = hashlib.sha256(repr((recipient, subject, filename, digest)).encode()).hexdigest()
        folder = self.path.parent / "attachments"
        folder.mkdir(parents=True, exist_ok=True, mode=0o2770)
        destination = self.attachment_path(digest)
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(dir=folder)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o660)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError("Stored attachment checksum mismatch")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deliveries "
                       "(key,recipient,subject,body,state,created_at,attachment_name,attachment_sha256) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (key, recipient, subject, body, "pending", int(time.time()), filename, digest))
            row = db.execute("SELECT recipient,subject,body,attachment_name,attachment_sha256 "
                             "FROM deliveries WHERE key=?", (key,)).fetchone()
            if tuple(row) != (recipient, subject, body, filename, digest):
                raise ValueError("Idempotency collision")
        return key

    def enqueue_period(self, *, period_key: str, recipient: str, subject: str,
                       body: str, revision: str) -> tuple[str, bool]:
        """Atomically reserve a reporting period and its immutable delivery.

        Returns (delivery key, newly reserved). Existing periods are never
        resent or rewritten automatically, even after crashes.
        """
        if not period_key:
            raise ValueError("period key required")
        key = hashlib.sha256(("\\0".join((recipient, subject, revision))).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT delivery_key FROM report_periods WHERE period_key=?",
                                  (period_key,)).fetchone()
            if existing:
                return existing["delivery_key"], False
            db.execute("INSERT OR IGNORE INTO deliveries (key,recipient,subject,body,state,created_at) VALUES (?,?,?,?,?,?)",
                       (key, recipient, subject, body, "pending", int(time.time())))
            row = db.execute("SELECT recipient,subject,body FROM deliveries WHERE key=?", (key,)).fetchone()
            if tuple(row) != (recipient, subject, body):
                raise ValueError("Idempotency collision: report revision or content changed")
            db.execute("INSERT INTO report_periods (period_key,delivery_key,created_at) VALUES (?,?,?)",
                       (period_key, key, int(time.time())))
            return key, True

    def period_status(self, period_key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT p.period_key,p.delivery_key,d.state,d.attempts,d.last_error
                FROM report_periods p JOIN deliveries d ON d.key=p.delivery_key
                WHERE p.period_key=?""", (period_key,)).fetchone()
            return dict(row) if row else None

    def claim(self, key: str | None = None) -> Delivery | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key is None:
                row = db.execute("SELECT * FROM deliveries WHERE state='pending' ORDER BY created_at,key LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT * FROM deliveries WHERE state='pending' AND key=?", (key,)).fetchone()
            if row is None:
                return None
            updated = db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,claimed_at=? WHERE key=? AND state='pending'", (int(time.time()), row["key"]))
            if updated.rowcount != 1:
                return None
            return Delivery(row["key"], row["recipient"], row["subject"], row["body"], row["attempts"] + 1, row["attachment_name"], self.attachment_path(row["attachment_sha256"]) if row["attachment_sha256"] else None, row["attachment_sha256"])

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
