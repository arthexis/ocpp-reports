"""Read-only, source-scoped charger CSV projection from the Reports warehouse.

This projection includes all currently recorded sessions, not the HTML page size.
Local transaction snapshots take precedence over Collector replicas by identity.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from ..csv_rendering import _safe
from ..reports.vendor_common import writer_buffer, local_date

CSV_PROFILES = frozenset({"vendor", "vendor-csv"})


def charger_transactions(database: str | Path, *, source_id: str, charger_id: str
                         ) -> tuple[list[dict], str] | None:
    """Return (deduplicated sessions, location) or None for an unknown device.

    Authorization is enforced by the HTTP route before this function is called.
    Every SQL read is constrained to the exact source and charger; no paging or
    writes are performed. A malformed projection fails closed rather than
    emitting another charger's data.
    """
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        by_id: dict[int, dict] = {}

        local = ("current_transactions" if "current_transactions" in tables
                 else "transaction_observations" if "transaction_observations" in tables
                 else None)
        if local:
            rows = db.execute(
                "SELECT transaction_id,payload FROM " + local
                + " WHERE source_id=? AND charger_id=?"
                + " ORDER BY transaction_id,page_next DESC,page_after DESC,rowid DESC",
                (source_id, charger_id))
            for tx_id, payload in rows:
                transaction = json.loads(payload)
                _check_identity(transaction, charger_id, tx_id)
                by_id.setdefault(tx_id, transaction)

        if "collector_latest" in tables:
            rows = db.execute(
                "SELECT payload FROM collector_latest"
                " WHERE source_id=? AND kind='transaction'"
                " AND json_extract(payload,'$.charger_id')=?"
                " ORDER BY record_key", (source_id, charger_id))
            for (payload,) in rows:
                transaction = json.loads(payload)
                tx_id = _check_identity(transaction, charger_id)
                by_id.setdefault(tx_id, transaction)

        found = bool(by_id)
        if not found and "connector_state" in tables:
            found = db.execute(
                "SELECT 1 FROM connector_state WHERE source_id=? AND charger_id=? LIMIT 1",
                (source_id, charger_id)).fetchone() is not None
        if not found and "events" in tables:
            found = db.execute(
                "SELECT 1 FROM events WHERE source_id=?"
                " AND json_extract(payload,'$.charger_id')=? LIMIT 1",
                (source_id, charger_id)).fetchone() is not None
        if not found and "collector_latest" in tables:
            found = db.execute(
                "SELECT 1 FROM collector_latest WHERE source_id=?"
                " AND kind IN ('connector','event')"
                " AND json_extract(payload,'$.charger_id')=? LIMIT 1",
                (source_id, charger_id)).fetchone() is not None
        if not found:
            return None

        location = source_id
        if "collector_charger_locations" in tables:
            row = db.execute(
                "SELECT location FROM collector_charger_locations"
                " WHERE source_id=? AND charger_id=?",
                (source_id, charger_id)).fetchone()
            if row and row[0]:
                location = row[0]
    return [by_id[key] for key in sorted(by_id)], location


def _check_identity(transaction: dict, charger_id: str, expected_id: int | None = None) -> int:
    if not isinstance(transaction, dict) or transaction.get("charger_id") != charger_id:
        raise ValueError("Inconsistent charger transaction projection")
    tx_id = transaction.get("id")
    if type(tx_id) is not int or (expected_id is not None and tx_id != expected_id):
        raise ValueError("Inconsistent transaction identity")
    return tx_id


def render_charger_csv(transactions: list[dict], *, location: str, timezone: str,
                       profile: str) -> str:
    """The two established ten-column layouts: vendor (local), vendor-csv (ISO)."""
    if profile not in CSV_PROFILES:
        raise ValueError("Unsupported charger CSV profile")
    zone = ZoneInfo(timezone)
    iso = profile == "vendor-csv"
    output, writer = writer_buffer()
    for tx in transactions:
        values = [
            location,
            tx.get("connector_id"),
            local_date(tx.get("started_at"), zone, iso=iso),
            local_date(tx.get("stopped_at"), zone, iso=iso),
            tx.get("meter_start_wh"),
            tx.get("meter_stop_wh"),
            tx.get("energy_wh"),
            tx.get("soc_start") if iso else tx.get("battery_start_pct"),
            tx.get("soc_end") if iso else tx.get("battery_stop_pct"),
            tx.get("stop_reason"),
        ]
        # Unlike the offline report, location and reason can arrive from
        # tenant-owned metadata. Prevent spreadsheet formula injection.
        writer.writerow([_safe(value) for value in values])
    return output.getvalue()
