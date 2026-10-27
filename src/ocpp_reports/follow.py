"""Continuous, read-only CSMS export consumption into the Reports warehouse."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from .ingest import ingest_page


def source_cursor(database: Path, source_id: str) -> int:
    if not database.exists():
        return 0
    with sqlite3.connect(database) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sources'").fetchone():
            return 0
        row = db.execute("SELECT cursor FROM sources WHERE source_id=?", (source_id,)).fetchone()
        return int(row[0]) if row else 0


def sync_page(database: Path, page: dict[str, Any]) -> int:
    """Commit evidence, cursor and connector projections atomically.

    A status snapshot may change even when the event cursor has not advanced.
    Do not let an older producer observation overwrite a newer snapshot.
    """
    data = page["data"]
    observed = data.get("exported_at") or datetime.now(timezone.utc).isoformat()
    source = data["source_id"]
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS connector_state (
            source_id TEXT NOT NULL, charger_id TEXT NOT NULL,
            connector_id INTEGER NOT NULL, status TEXT, connected INTEGER,
            transaction_id INTEGER, power_w REAL, energy_wh REAL,
            observed_at TEXT NOT NULL, synced_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(source_id, charger_id, connector_id)
        )""")
        db.executescript("""
        CREATE TRIGGER IF NOT EXISTS replicate_connector_insert
        AFTER INSERT ON connector_state BEGIN
            INSERT INTO replication_changes(source_id,kind,record_key,payload)
            VALUES (NEW.source_id,'connector',
                    NEW.charger_id || ':' || NEW.connector_id,
                    json_object('charger_id',NEW.charger_id,'connector_id',NEW.connector_id,
                        'status',NEW.status,'connected',NEW.connected,
                        'transaction_id',NEW.transaction_id,'power_w',NEW.power_w,
                        'energy_wh',NEW.energy_wh,'observed_at',NEW.observed_at));
        END;
        CREATE TRIGGER IF NOT EXISTS replicate_connector_update
        AFTER UPDATE ON connector_state
        WHEN OLD.observed_at != NEW.observed_at OR OLD.status IS NOT NEW.status
             OR OLD.connected IS NOT NEW.connected OR OLD.power_w IS NOT NEW.power_w
             OR OLD.energy_wh IS NOT NEW.energy_wh
             OR OLD.transaction_id IS NOT NEW.transaction_id BEGIN
            INSERT INTO replication_changes(source_id,kind,record_key,payload)
            VALUES (NEW.source_id,'connector',
                    NEW.charger_id || ':' || NEW.connector_id,
                    json_object('charger_id',NEW.charger_id,'connector_id',NEW.connector_id,
                        'status',NEW.status,'connected',NEW.connected,
                        'transaction_id',NEW.transaction_id,'power_w',NEW.power_w,
                        'energy_wh',NEW.energy_wh,'observed_at',NEW.observed_at));
        END;
        """)
    def project(db: sqlite3.Connection) -> None:
        # The producer status is a read snapshot, not a stream of immutable events.
        # Preserve the most recent energy sample already seen when a page is empty.
        samples = {}
        for sample in data["energy"]:
            key = (sample.get("charger_id"), sample.get("connector_id"))
            samples[key] = sample
        for charger in data["status"].get("chargers", []):
            for connector in charger.get("connectors", []):
                cid = connector["id"]
                sample = samples.get((charger["id"], cid), {})
                previous = db.execute(
                    "SELECT power_w, energy_wh FROM connector_state WHERE source_id=? AND charger_id=? AND connector_id=?",
                    (source, charger["id"], cid),
                ).fetchone()
                power = sample.get("power_w") if sample.get("power_w") is not None else (previous[0] if previous else None)
                energy = sample.get("energy_wh") if sample.get("energy_wh") is not None else (previous[1] if previous else None)
                transaction = connector.get("transaction") or {}
                db.execute("""INSERT INTO connector_state VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id,charger_id,connector_id) DO UPDATE SET
                    status=excluded.status, connected=excluded.connected,
                    transaction_id=excluded.transaction_id, power_w=excluded.power_w,
                    energy_wh=excluded.energy_wh, observed_at=excluded.observed_at,
                    synced_at=excluded.synced_at, payload=excluded.payload
                    WHERE excluded.observed_at >= connector_state.observed_at""",
                    (source, charger["id"], cid, connector.get("derived_status") or connector.get("status"),
                     int(bool(charger.get("connected"))), transaction.get("id"),
                     power, energy, observed, datetime.now(timezone.utc).isoformat(),
                     json.dumps({"charger": charger, "connector": connector}, sort_keys=True)))
    return ingest_page(database, page, project=project)


def follow(database: Path, data_dir: Path, *, interval: float = 5.0,
           limit: int = 500, once: bool = False,
           producer: Callable[..., dict[str, Any]] | None = None,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """Catch up completely, then keep synchronizing. Fail visibly for systemd restart."""
    if interval <= 0 or limit < 1:
        raise ValueError("interval and limit must be positive")
    if producer is None:
        try:
            from ocpp_csms.export_contract import export_contract
        except ImportError as exc:
            raise RuntimeError("Local ocpp-csms Python package is required for --follow") from exc
        producer = export_contract
    database.parent.mkdir(parents=True, exist_ok=True)
    while True:
        # Source identity comes from producer; never infer it from hostname.
        identity = producer(data_dir, after=0, limit=1)["data"]["source_id"]
        while True:
            cursor = source_cursor(database, identity)
            page = producer(data_dir, after=cursor, limit=limit)
            if page["data"]["source_id"] != identity:
                raise ValueError("CSMS source identity changed during synchronization")
            new_cursor = sync_page(database, page)
            if new_cursor < cursor or (page["data"]["cursor"]["more"] and new_cursor == cursor):
                raise ValueError("CSMS cursor did not progress")
            if not page["data"]["cursor"]["more"]:
                break
        if once:
            return
        sleep(interval)


def current_connectors(database: Path) -> list[dict[str, Any]]:
    """Read current state only from Reports SQLite; clients calculate freshness."""
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT source_id,charger_id,connector_id,status,connected,transaction_id,"
            "power_w,energy_wh,observed_at,synced_at FROM connector_state "
            "ORDER BY source_id,charger_id,connector_id"
        )]
