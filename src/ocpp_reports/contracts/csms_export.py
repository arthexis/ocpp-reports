"""B2A: strict envelope validation for the CSMS cursor export, without ingestion."""
from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any

SCHEMA = "ocpp-csms/export/v1"

# Allowlisted producer channel contract. Other additive export fields remain
# tolerated, but no unverified/ambiguous measurand becomes a display scalar.
METER_CHANNELS = {
    "SoC": ("soc", "Percent", 0.0, 100.0),
    "Voltage": ("voltage_v", "V", 0.0, 1_000_000.0),
    "Current.Import": ("current_import_a", "A", 0.0, 1_000_000.0),
    "Current.Offered": ("current_offered_a", "A", 0.0, 1_000_000.0),
    "Temperature": ("temperature_c", "Celsius", -273.15, 1_000.0),
}


def _measured(value: Any, name: str, low: float, high: float) -> None:
    if (type(value) not in (int, float) or not isfinite(value)
            or not low <= value <= high):
        raise ValueError(f"{name} must be a finite measured quantity")


def validate_meter_fields(sample: dict[str, Any]) -> None:
    """Validate optional measured electrical channels and their provenance."""
    channels = sample.get("meter_channels", [])
    if not isinstance(channels, list) or len(channels) > 64:
        raise ValueError("energy.meter_channels must contain <=64 readings")
    witnessed: dict[str, list[float]] = {}
    for channel in channels:
        if not isinstance(channel, dict):
            raise ValueError("Meter channel must be an object")
        name = channel.get("measurand")
        if not isinstance(name, str) or name not in METER_CHANNELS:
            raise ValueError("Unsupported measured channel")
        field, unit, low, high = METER_CHANNELS[name]
        if channel.get("unit") != unit:
            raise ValueError("Mismatched OCPP meter unit")
        _measured(channel.get("value"), f"meter_channels.{name}", low, high)
        for optional, size in (("phase", 32), ("context", 64), ("location", 64)):
            value = channel.get(optional)
            if value is not None and (not isinstance(value, str)
                                      or not 1 <= len(value) <= size):
                raise ValueError(f"Malformed meter channel {optional}")
        if channel.get("phase") is None:
            witnessed.setdefault(field, []).append(channel["value"])
    for name, (field, _, low, high) in METER_CHANNELS.items():
        if field not in sample:
            continue
        value = sample[field]
        _measured(value, f"energy.{field}", low, high)
        # A scalar requires one unambiguous unphased OCPP observation.
        matches = witnessed.get(field, [])
        if not matches or len(set(matches)) != 1 or value != matches[0]:
            raise ValueError(f"Missing or conflicting meter evidence for {field}")
    if "soc" in sample:
        if sample.get("soc_provenance") != "ocpp-meter-values":
            raise ValueError("Unverified OCPP SoC provenance")
    elif sample.get("soc_provenance") is not None:
        raise ValueError("SoC provenance without measured SoC")


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


def validate_export(page: Any, *, require_page_consistency: bool = True) -> dict[str, Any]:
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
    if "exported_at" in data:
        _timestamp(data["exported_at"], "exported_at")
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
    # Optional additive CSMS operator metadata: capacity is never measured
    # power, and connector zero is a charger-wide aggregate.
    for charger in data["status"].get("chargers", []):
        if not isinstance(charger, dict):
            raise ValueError("status.chargers items must be objects")
        value = charger.get("rated_power_w")
        if value is not None and (type(value) is not int or not 0 < value <= 10_000_000):
            raise ValueError("Invalid charger nominal power")
        for connector in charger.get("connectors", []):
            if not isinstance(connector, dict):
                raise ValueError("status.connector items must be objects")
            value = connector.get("rated_power_w")
            scope = connector.get("rating_scope")
            if value is not None and (type(value) is not int or not 0 < value <= 10_000_000):
                raise ValueError("Invalid connector nominal power")
            if scope not in (None, "charger", "connector"):
                raise ValueError("Invalid connector rating scope")
            if (value is None) != (scope is None):
                raise ValueError("Connector rating and scope must be supplied together")
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
    if require_page_consistency and not data["events"] and next_cursor != after:
        raise ValueError("Empty page must not advance cursor")
    if require_page_consistency and not data["events"] and cursor["more"]:
        raise ValueError("Empty page cannot claim more events")
    snapshots = data.get("configuration_snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("Invalid configuration snapshots")
    from ..warehouse.config_history import validate_snapshot
    events_by_id = {event["id"]: event for event in data["events"]}
    for raw_snapshot in snapshots:
        item = validate_snapshot(raw_snapshot)
        event = events_by_id.get(item["source_event_id"])
        if (event is None or event["action"] != "GetConfiguration"
                or event.get("direction") != "in"
                or event["charger_id"] != item["charger_id"]):
            raise ValueError("Configuration snapshot lacks source event evidence")
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
        validate_meter_fields(sample)
    for transaction in data["transactions"]:
        if not isinstance(transaction, dict):
            raise ValueError("Transaction must be an object")
        _integer(transaction.get("id"), "transaction.id")
        if not isinstance(transaction.get("charger_id"), str) or not transaction["charger_id"]:
            raise ValueError("Transaction charger_id is required")
        for name in ("started_at", "stopped_at", "last_activity_at",
                     "start_event_at", "start_received_at", "stop_event_at", "stop_received_at"):
            if transaction.get(name) is not None:
                _timestamp(transaction[name], f"transaction.{name}")
        for name in ("energy_wh", "meter_start_wh", "meter_stop_wh"):
            value = transaction.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"transaction.{name} must be numeric or null")
        if "meter_sample_times" in transaction:
            samples = transaction["meter_sample_times"]
            if not isinstance(samples, list):
                raise ValueError("transaction.meter_sample_times must be a list")
            for sample in samples:
                if not isinstance(sample, dict):
                    raise ValueError("transaction meter sample must be an object")
                _integer(sample.get("payload_index"), "meter_sample_times.payload_index")
                _integer(sample.get("sample_index"), "meter_sample_times.sample_index")
                for field in ("event_at", "received_at"):
                    if sample.get(field) is not None:
                        _timestamp(sample[field], f"meter_sample_times.{field}")
        if "quality" in transaction:
            quality = transaction["quality"]
            if not isinstance(quality, dict):
                raise ValueError("transaction.quality must be an object")
            for field in ("active", "incomplete", "missing_meter", "recovered", "inferred", "corrected"):
                if field in quality and type(quality[field]) is not bool:
                    raise ValueError(f"transaction.quality.{field} must be boolean")
    return page
