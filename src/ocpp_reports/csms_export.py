"""B2A: strict envelope validation for the CSMS cursor export, without ingestion."""
from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any

SCHEMA = "ocpp-csms/export/v1"


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an offset-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an offset-aware timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} must be an offset-aware timestamp")


def validate_export(page: Any) -> dict[str, Any]:
    """Validate producer-defined fields; tolerate unknown additive fields.

    Does not imply that a completed cursor represents a complete report period.
    """
    if not isinstance(page, dict) or page.get("schema") != SCHEMA:
        raise ValueError("Unsupported CSMS export schema")
    data = page.get("data")
    if not isinstance(data, dict):
        raise ValueError("Export data must be an object")
    if not isinstance(data.get("source_id"), str) or not data["source_id"].strip():
        raise ValueError("Export source_id is required")
    cursor = data.get("cursor")
    if not isinstance(cursor, dict):
        raise ValueError("Export cursor must be an object")
    after = _integer(cursor.get("after"), "cursor.after")
    next_cursor = _integer(cursor.get("next"), "cursor.next")
    if next_cursor < after or type(cursor.get("more")) is not bool:
        raise ValueError("Invalid cursor ordering or more flag")
    for name in ("events", "energy", "transactions"):
        if not isinstance(data.get(name), list):
            raise ValueError(f"{name} must be a list")
    if not isinstance(data.get("status"), dict):
        raise ValueError("status must be an object")
    previous = after
    for event in data["events"]:
        if not isinstance(event, dict):
            raise ValueError("Event must be an object")
        identity = _integer(event.get("id"), "event.id", minimum=1)
        if identity <= previous or identity > next_cursor:
            raise ValueError("Event IDs must be increasing within cursor")
        previous = identity
        _timestamp(event.get("at"), "event.at")
        if not isinstance(event.get("charger_id"), str) or not event["charger_id"]:
            raise ValueError("Event charger_id is required")
        if not isinstance(event.get("kind"), str) or not isinstance(event.get("action"), str):
            raise ValueError("Event kind and action are required")
    if data["events"] and previous != next_cursor:
        raise ValueError("cursor.next must match last event")
    if not data["events"] and next_cursor != after:
        raise ValueError("Empty page must not advance cursor")
    if not data["events"] and cursor["more"]:
        raise ValueError("Empty page cannot claim more events")
    for sample in data["energy"]:
        if not isinstance(sample, dict):
            raise ValueError("Energy sample must be an object")
        source_event = _integer(sample.get("source_event_id"), "energy.source_event_id", minimum=1)
        if not (after < source_event <= next_cursor):
            raise ValueError("Energy sample references an event outside this page")
        _integer(sample.get("sample_index"), "energy.sample_index")
        _timestamp(sample.get("at"), "energy.at")
        for name in ("power_w", "energy_wh"):
            value = sample.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)):
                raise ValueError(f"energy.{name} must be numeric or null")
    for transaction in data["transactions"]:
        if not isinstance(transaction, dict):
            raise ValueError("Transaction must be an object")
        _integer(transaction.get("id"), "transaction.id")
        if not isinstance(transaction.get("charger_id"), str) or not transaction["charger_id"]:
            raise ValueError("Transaction charger_id is required")
        for name in ("started_at", "stopped_at", "last_activity_at"):
            if transaction.get(name) is not None:
                _timestamp(transaction[name], f"transaction.{name}")
        for name in ("energy_wh", "meter_start_wh", "meter_stop_wh"):
            value = transaction.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"transaction.{name} must be numeric or null")
    return page
