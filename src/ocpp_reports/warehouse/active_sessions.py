"""Read-only, source-scoped identity checks for live charging sessions."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone


def is_active_session(row: dict) -> bool:
    """Treat only a non-final, explicitly active projection as in progress."""
    return (
        row.get("active") is True
        and isinstance(row.get("state"), str)
        and row["state"].casefold() in ("open", "active", "charging")
        and row.get("stopped_at") is None
        and row.get("energy_wh") is None
    )


def _lookup_transaction(db, tables: set[str], source: str, charger: str, tx_id: int):
    row = None
    if "current_transactions" in tables:
        row = db.execute(
            "SELECT payload FROM current_transactions WHERE source_id=? "
            "AND charger_id=? AND transaction_id=? LIMIT 1",
            (source, charger, tx_id),
        ).fetchone()
    if row is None and "collector_latest" in tables:
        row = db.execute(
            "SELECT payload FROM collector_latest WHERE source_id=? AND kind='transaction' "
            "AND json_extract(payload,'$.charger_id')=? "
            "AND json_extract(payload,'$.id')=? LIMIT 1",
            (source, charger, tx_id),
        ).fetchone()
    if row is None:
        return None
    try:
        tx = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if (not isinstance(tx, dict) or tx.get("charger_id") != charger
            or type(tx.get("id")) is not int or tx["id"] != tx_id):
        return None
    return tx


def attach_active_links(db, tables: set[str], connectors: list[dict]) -> None:
    """Point lookups for already source-scoped charging connectors only."""
    for connector in connectors:
        connector["active_transaction_id"] = None
        source, charger = connector.get("source_id"), connector.get("charger_id")
        connector_id, tx_id = connector.get("connector_id"), connector.get("transaction_id")
        if (not isinstance(source, str) or not isinstance(charger, str)
                or type(connector_id) is not int or connector_id <= 0
                or type(tx_id) is not int
                or str(connector.get("status") or "").casefold() != "charging"):
            continue
        tx = _lookup_transaction(db, tables, source, charger, tx_id)
        if (tx is not None and is_active_session(tx)
                and type(tx.get("connector_id")) is int
                and tx["connector_id"] == connector_id):
            connector["active_transaction_id"] = tx_id


def live_power_for_transaction(db, tables: set[str], tx: dict) -> dict:
    """Only measured connector power with matching transaction and meter timestamp."""
    if not is_active_session(tx):
        return {}
    source, charger = tx.get("source_id"), tx.get("charger_id")
    connector_id, tx_id = tx.get("connector_id"), tx.get("id")
    if (not isinstance(source, str) or not isinstance(charger, str)
            or type(connector_id) is not int or connector_id <= 0
            or type(tx_id) is not int):
        return {}
    connector = None
    if "connector_state" in tables:
        row = db.execute(
            "SELECT transaction_id,status,power_w,payload FROM connector_state "
            "WHERE source_id=? AND charger_id=? AND connector_id=? LIMIT 1",
            (source, charger, connector_id),
        ).fetchone()
        if row is not None:
            try:
                connector = json.loads(row[3])
            except (ValueError, TypeError):
                connector = {}
            if not isinstance(connector, dict):
                connector = {}
            connector.update(transaction_id=row[0], status=row[1], power_w=row[2])
    if connector is None and "collector_latest" in tables:
        row = db.execute(
            "SELECT payload FROM collector_latest WHERE source_id=? AND kind='connector' "
            "AND json_extract(payload,'$.charger_id')=? "
            "AND json_extract(payload,'$.connector_id')=? LIMIT 1",
            (source, charger, connector_id),
        ).fetchone()
        if row is not None:
            try:
                connector = json.loads(row[0])
            except (ValueError, TypeError):
                connector = None
    if not isinstance(connector, dict):
        return {}
    if (type(connector.get("transaction_id")) is not int
            or connector["transaction_id"] != tx_id
            or str(connector.get("status") or "").casefold() != "charging"
            or (connector.get("charger_id") is not None and connector["charger_id"] != charger)
            or (connector.get("connector_id") is not None and connector["connector_id"] != connector_id)):
        return {}
    value, observed = connector.get("power_w"), connector.get("power_observed_at")
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return {}
    if not isinstance(observed, str):
        return {}
    try:
        measured = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if measured.tzinfo is None:
            return {}
        age = datetime.now(timezone.utc) - measured.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return {}
    return {
        "live_power_w": value,
        "live_power_observed_at": observed,
        "live_power_stale": not (timedelta(0) <= age <= timedelta(seconds=120)),
    }
