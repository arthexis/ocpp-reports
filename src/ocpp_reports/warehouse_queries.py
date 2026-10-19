"""Read-only warehouse queries shared by the dashboard and future consumers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def snapshot(database: Path, *, source_id: str | None = None, charger_id: str | None = None,
             limit: int = 100) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Warehouse not found: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = [dict(r) for r in db.execute(
            "SELECT source_id,cursor,last_ingested_at FROM sources ORDER BY source_id"
        )] if "sources" in tables else []
        connectors = []
        if "connector_state" in tables:
            where, args = [], []
            if source_id:
                where.append("source_id=?")
                args.append(source_id)
            if charger_id:
                where.append("charger_id=?")
                args.append(charger_id)
            clause = " WHERE " + " AND ".join(where) if where else ""
            connectors = [dict(r) for r in db.execute(
                "SELECT source_id,charger_id,connector_id,status,connected,transaction_id,"
                "power_w,energy_wh,observed_at,synced_at FROM connector_state" + clause +
                " ORDER BY source_id,charger_id,connector_id", args)]
        def recent(table, ordering, column):
            if table not in tables:
                return []
            where, args = [], []
            if source_id:
                where.append("source_id=?")
                args.append(source_id)
            if charger_id:
                where.append("charger_id=?")
                args.append(charger_id)
            clause = " WHERE " + " AND ".join(where) if where else ""
            rows = db.execute(f"SELECT source_id,{column},payload FROM {table}" +
                              clause + f" ORDER BY {ordering} DESC LIMIT ?", (*args, limit))
            return [{"source_id": r["source_id"], **json.loads(r["payload"])} for r in rows]
        transactions = recent("current_transactions", "imported_at", "transaction_id")
        events = recent("events", "event_id", "event_id")
    return {"sources": sources, "connectors": connectors,
            "transactions": transactions, "events": events}
