"""Bounded read-only lookup for one source-scoped transaction projection."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .active_sessions import live_power_for_transaction


def transaction_detail(database: str | Path, *, source_id: str, charger_id: str,
                       transaction_id: int,
                       allowed_source_ids: tuple[str, ...] | None = None) -> dict | None:
    """Return one local or forwarded transaction, never scanning event history.

    Local authoritative projection wins if both local and collector copies exist.
    Unknown and stale live measurements remain distinct from finalized energy.
    """
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        return None
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Warehouse not found: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        payload = None
        if "current_transactions" in tables:
            row = db.execute(
                "SELECT payload FROM current_transactions WHERE source_id=?"
                " AND charger_id=? AND transaction_id=? LIMIT 1",
                (source_id, charger_id, transaction_id),
            ).fetchone()
            if row:
                payload = row[0]
        if payload is None and "collector_latest" in tables:
            row = db.execute(
                "SELECT payload FROM collector_latest WHERE source_id=?"
                " AND kind='transaction'"
                " AND json_extract(payload,'$.charger_id')=?"
                " AND json_extract(payload,'$.id')=? LIMIT 1",
                (source_id, charger_id, transaction_id),
            ).fetchone()
            if row:
                payload = row[0]
        if payload is None:
            return None
        result = json.loads(payload)
        if not isinstance(result, dict):
            return None
        # Never trust a payload's embedded identity over its storage identity.
        if (result.get("charger_id") != charger_id
                or result.get("id") != transaction_id):
            return None
        result["source_id"] = source_id
        result["location"] = source_id
        if "collector_charger_locations" in tables:
            row = db.execute(
                "SELECT location FROM collector_charger_locations"
                " WHERE source_id=? AND charger_id=?",
                (source_id, charger_id),
            ).fetchone()
            if row:
                result["location"] = row[0]
        result.update(live_power_for_transaction(db, tables, result))

    result["live_energy_stale"] = True
    observed = result.get("live_energy_observed_at")
    if (result.get("energy_wh") is None
            and result.get("active")
            and result.get("state") in ("open", "active", "charging")
            and isinstance(observed, str)):
        try:
            measured = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if measured.tzinfo is not None:
                age = datetime.now(timezone.utc) - measured.astimezone(timezone.utc)
                result["live_energy_stale"] = not (
                    timedelta(0) <= age <= timedelta(minutes=5))
        except ValueError:
            pass
    return result
