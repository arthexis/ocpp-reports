"""Operator-invoked repair of historical transaction display dates.

The original charger timestamps are already present in Reports' immutable
transaction observations. A new projection update emits a new replication
sequence; neither the producer cursor nor old changefeed entries are edited.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Charger event timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid charger event timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Charger event timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _backup(db: sqlite3.Connection, database: Path, destination: Path) -> None:
    if not destination.parent.is_dir() or destination.resolve() == database.resolve():
        raise ValueError("Backup must be a new file in an existing private directory")
    if destination.parent.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (mode 0700)")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Reports warehouse integrity check failed")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        # The caller's IMMEDIATE transaction prevents intervening writers.
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Reports backup integrity check failed")
                if copy.execute("SELECT COUNT(*) FROM replication_changes").fetchone() != db.execute(
                    "SELECT COUNT(*) FROM replication_changes").fetchone():
                    raise ValueError("Reports backup changed during creation")
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def refresh(database: Path, *, source: str, cp: str, transactions: list[int],
            dry_run: bool = True, backup: Path | None = None) -> dict:
    """Preview or repair selected completed projections from stored event times."""
    if not source.strip() or not cp.strip() or not transactions or any(
        type(n) is not int or n <= 0 for n in transactions
    ) or len(transactions) != len(set(transactions)) or len(transactions) > 500:
        raise ValueError("Explicit source, CP and distinct positive transaction IDs are required")
    database = Path(database)
    if not database.is_file():
        raise ValueError("Existing Reports warehouse is required")
    if not dry_run and backup is None:
        raise ValueError("--backup is required for --apply")
    uri = database.resolve().as_uri() + ("?mode=ro" if dry_run else "?mode=rw")
    with sqlite3.connect(uri, uri=True, timeout=30) as db:
        db.execute("PRAGMA query_only=ON" if dry_run else "PRAGMA foreign_keys=ON")
        db.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='replicate_transaction_update'").fetchone() is None:
            raise ValueError("Reports transaction replication trigger missing")
        if db.execute("SELECT 1 FROM sources WHERE source_id=?", (source,)).fetchone() is None:
            raise ValueError("Selected source is not enrolled")
        cursor = db.execute("SELECT cursor FROM sources WHERE source_id=?", (source,)).fetchone()[0]
        before_sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM replication_changes").fetchone()[0]
        updates = []
        rows = []
        for transaction in transactions:
            row = db.execute("SELECT payload FROM current_transactions WHERE source_id=? AND charger_id=? AND transaction_id=?",
                             (source, cp, transaction)).fetchone()
            if row is None:
                raise ValueError(f"Transaction {transaction} is not projected for selected source and CP")
            old = json.loads(row[0])
            if old.get("id") != transaction or old.get("charger_id") != cp:
                raise ValueError("Stored transaction identity mismatch")
            if old.get("active") is True or old.get("state") in ("open", "active", "charging"):
                raise ValueError("Date refresh requires completed transactions")
            observations = db.execute(
                "SELECT payload FROM transaction_observations WHERE source_id=? AND charger_id=? AND transaction_id=?",
                (source, cp, transaction)).fetchall()
            if not any(
                (observed := json.loads(item[0])).get("start_event_at") == old.get("start_event_at")
                and observed.get("stop_event_at") == old.get("stop_event_at")
                and observed.get("energy_wh") == old.get("energy_wh")
                for item in observations
            ):
                raise ValueError("Current transaction dates lack matching immutable observation evidence")
            start, stop = _timestamp(old.get("start_event_at")), _timestamp(old.get("stop_event_at"))
            if stop <= start:
                raise ValueError("Charger stop must follow start")
            proposed = {"started_at": _utc(start), "stopped_at": _utc(stop)}
            changed = [key for key, value in proposed.items() if old.get(key) != value]
            rows.append({"transaction_id": transaction, "current": {key: old.get(key) for key in proposed},
                         "proposed": proposed, "energy_wh": old.get("energy_wh"), "fields_changed": changed})
            if changed:
                patched = {**old, **proposed, "date_metadata_provenance": "stored-charger-event-times"}
                encoded = json.dumps(patched, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                updates.append((encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                                datetime.now(timezone.utc).isoformat(), source, cp, transaction))
        if not dry_run:
            _backup(db, database, Path(backup))
            for update in updates:
                db.execute("UPDATE current_transactions SET payload=?,fingerprint=?,imported_at=? "
                           "WHERE source_id=? AND charger_id=? AND transaction_id=?", update)
            db.commit()
        return {"schema": "ocpp-reports/date-refresh/v1", "status": "would_update" if dry_run and updates else
                "updated" if updates else "unchanged", "source_id": source, "charger_id": cp,
                "source_cursor": cursor, "change_sequence_before": before_sequence,
                "change_sequence_after": before_sequence + (len(updates) if not dry_run else 0),
                "transactions": rows, "backup": str(backup) if not dry_run else None}
