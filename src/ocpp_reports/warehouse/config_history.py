"""Immutable sanitized GetConfiguration observations in Reports-owned SQLite.

This module does not contact the CSMS or chargers.  Every write is driven by
stored evidence; source/event identity survives replay and historical backfill.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..contracts.csms_export import _timestamp

SCHEMA = "ocpp-csms/config-snapshots/v1"
REDACTED = "[REDACTED]"
_SENSITIVE = (
    "password", "passwd", "pwd", "passphrase", "secret", "token", "psk",
    "authorizationkey", "authkey", "privatekey", "apikey", "accesskey",
    "clientsecret", "credential", "privatecredential", "securitykey", "sessionkey",
    "encryptkey", "softkey", "ssid",
)


def _secret_key(key: str) -> bool:
    name = "".join(char for char in key.lower() if char.isalnum())
    return (name in {"rfidtagfreecharging", "tykey",
                     "lpin", "simpin", "simcardpin", "pincode",
                     "luser", "vpnuser", "simurfid", "key", "ssid", "apssid"}
            or (name.startswith("key") and name[3:].isdigit())
            or any(fragment in name for fragment in _SENSITIVE))


def validate_snapshot(item: Any) -> dict[str, Any]:
    """Fail closed before accepting or replicating stored configuration values."""
    if not isinstance(item, dict) or set(item) != {"source_event_id", "charger_id", "at", "coverage", "configuration", "unknown"}:
        raise ValueError("Invalid configuration snapshot")
    event_id = item.get("source_event_id")
    charger = item.get("charger_id")
    if type(event_id) is not int or event_id <= 0 or not isinstance(charger, str) or not charger:
        raise ValueError("Invalid configuration snapshot identity")
    _timestamp(item.get("at"), "configuration.at")
    if item.get("coverage") not in ("full", "partial", "unknown"):
        raise ValueError("Invalid configuration coverage")
    values, unknown = item.get("configuration"), item.get("unknown")
    if not isinstance(values, list) or len(values) > 10000:
        raise ValueError("Invalid configuration entries")
    if not isinstance(unknown, list) or len(unknown) > 10000 or any(
        not isinstance(key, str) or not key or len(key) > 2048 for key in unknown
    ):
        raise ValueError("Invalid unknown configuration keys")
    seen: set[str] = set()
    for row in values:
        if not isinstance(row, dict) or set(row) != {"key", "readonly", "value"}:
            raise ValueError("Invalid configuration entry")
        key, value, readonly = row.get("key"), row.get("value"), row.get("readonly")
        if (not isinstance(key, str) or not key or len(key) > 2048
                or key in seen or type(readonly) is not bool
                or value is not None and (not isinstance(value, str) or len(value) > 100000)):
            raise ValueError("Invalid configuration entry fields")
        seen.add(key)
        if _secret_key(key) and value not in (REDACTED, None):
            raise ValueError("Unredacted sensitive configuration is forbidden")
    # Discard unrecognized fields instead of persisting arbitrary producer data.
    return {
        "source_event_id": event_id, "charger_id": charger, "at": item["at"],
        "coverage": item["coverage"],
        "configuration": [{"key": row["key"], "readonly": row["readonly"],
                           "value": row.get("value")} for row in values],
        "unknown": list(unknown),
    }


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def insert_snapshot(db: sqlite3.Connection, source: str, snapshot: dict[str, Any]) -> bool:
    item = validate_snapshot(snapshot)
    encoded = _canonical(item)
    key = (source, item["source_event_id"])
    old = db.execute("SELECT payload FROM configuration_snapshots WHERE source_id=? "
                     "AND source_event_id=?", key).fetchone()
    if old:
        if old[0] != encoded:
            raise ValueError("Conflicting immutable configuration snapshot")
        return False
    db.execute(
        "INSERT INTO configuration_snapshots "
        "(source_id,charger_id,source_event_id,at,payload) VALUES (?,?,?,?,?)",
        (source, item["charger_id"], item["source_event_id"], item["at"], encoded),
    )
    return True


def import_history_page(database: str | Path, page: dict[str, Any], *, apply: bool = False) -> int:
    """Validate a bounded backfill page; never change the ordinary source cursor."""
    if not isinstance(page, dict) or page.get("schema") != SCHEMA:
        raise ValueError("Invalid historical configuration export schema")
    data = page.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("source_id"), str) or not data["source_id"]:
        raise ValueError("Invalid historical source")
    cursor = data.get("cursor")
    items = data.get("configuration_snapshots")
    if not isinstance(cursor, dict) or not isinstance(items, list) or len(items) > 500:
        raise ValueError("Invalid historical page")
    after, end = cursor.get("after"), cursor.get("next")
    if (type(after) is not int or type(end) is not int or after < 0
            or end < after or type(cursor.get("more")) is not bool):
        raise ValueError("Invalid historical cursor")
    normalized = [validate_snapshot(s) for s in items]
    event_ids = [s["source_event_id"] for s in normalized]
    if event_ids != sorted(set(event_ids)) or any(not after < key <= end for key in event_ids):
        raise ValueError("Invalid historical event order")
    if not apply:
        return len(normalized)
    if not Path(database).is_file():
        raise ValueError("Backfill requires an existing Reports warehouse")
    from .schema import _schema
    with sqlite3.connect(database) as db:
        _schema(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM sources WHERE source_id=?", (data["source_id"],)
        ).fetchone():
            raise ValueError("Backfill source is not registered in this local warehouse")
        for item in normalized:
            insert_snapshot(db, data["source_id"], item)
    return len(normalized)


def list_snapshots(database: str | Path, source: str, charger: str, *, limit: int = 25,
                   offset: int = 0) -> tuple[list[dict[str, Any]], bool]:
    """Read local + collector history with local precedence, bounded pagination."""
    if not source or not charger or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError("Invalid configuration query")
    path = Path(database)
    if not path.exists():
        return [], False
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        streams = []
        bound = offset + limit + 1
        if "configuration_snapshots" in tables:
            streams.append(db.execute(
                "SELECT source_event_id,payload FROM configuration_snapshots "
                "WHERE source_id=? AND charger_id=? ORDER BY source_event_id DESC LIMIT ?",
                (source, charger, bound),
            ).fetchall())
        if "collector_configuration_snapshots" in tables:
            streams.append(db.execute(
                "SELECT source_event_id,payload FROM collector_configuration_snapshots "
                "WHERE source_id=? AND charger_id=? ORDER BY source_event_id DESC LIMIT ?",
                (source, charger, bound),
            ).fetchall())
    distinct: dict[int, dict] = {}
    for rows in streams:
        for event_id, payload in rows:
            distinct.setdefault(event_id, json.loads(payload))
    ordered = [distinct[key] for key in sorted(distinct, reverse=True)]
    selected = ordered[offset:offset + limit + 1]
    return selected[:limit], len(selected) > limit


def one_snapshot(database: str | Path, source: str, charger: str, event_id: int) -> dict | None:
    if type(event_id) is not int or event_id < 1:
        raise ValueError("Invalid configuration event")
    path = Path(database)
    if not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("configuration_snapshots", "collector_configuration_snapshots"):
            if table not in tables:
                continue
            row = db.execute(
                f"SELECT payload FROM {table} WHERE source_id=? AND charger_id=? AND source_event_id=?",
                (source, charger, event_id),
            ).fetchone()
            if row:
                return json.loads(row[0])
    return None


def render_snapshot_csv(snapshot: dict) -> str:
    """Export the selected stored configuration table, with safe spreadsheet cells."""
    import csv
    import io

    from ..csv_rendering import _safe

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Clave", "Valor", "Solo lectura"))
    for row in snapshot["configuration"]:
        writer.writerow((_safe(row["key"]), _safe(row["value"]),
                         "Sí" if row["readonly"] else "No"))
    return output.getvalue()
