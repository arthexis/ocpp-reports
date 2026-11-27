"""Immutable sanitized GetConfiguration observations in Reports-owned SQLite.

This module does not contact the CSMS or chargers.  Every write is driven by
stored evidence; source/event identity survives replay and historical backfill.
"""
from __future__ import annotations

import hashlib
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


VISIBLE_KEYS = frozenset({"ssid", "apssid", "simurfid"})


def _normalized(key: str) -> str:
    return "".join(char for char in key.lower() if char.isalnum())


def _secret_key(key: str) -> bool:
    name = _normalized(key)
    # Explicitly visible operational keys; retain the broad secret rules for all others.
    if name in VISIBLE_KEYS:
        return False
    return (name in {"rfidtagfreecharging", "tykey",
                     "lpin", "simpin", "simcardpin", "pincode",
                     "luser", "vpnuser", "key"}
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


def configuration_overlay(base_payload: str, record: dict) -> dict:
    """Verify a strictly additive disclosure against the exact immutable base."""
    base = validate_snapshot(json.loads(base_payload))
    if (not isinstance(record, dict)
            or set(record) != {"source_event_id", "base_sha256", "fields"}
            or type(record["source_event_id"]) is not int
            or record["source_event_id"] != base["source_event_id"]
            or record["base_sha256"] != hashlib.sha256(base_payload.encode()).hexdigest()
            or not isinstance(record["fields"], dict) or not record["fields"]):
        raise ValueError("Invalid configuration enrichment identity")
    fields = record["fields"]
    rows = {row["key"]: row for row in base["configuration"]}
    for key, value in fields.items():
        if (key not in rows or _normalized(key) not in VISIBLE_KEYS
                or rows[key]["value"] != REDACTED or value == REDACTED
                or value is not None and not isinstance(value, str)):
            raise ValueError("Invalid configuration enrichment field")
        rows[key]["value"] = value
    return validate_snapshot(base)


def _enrichment_record(base_payload: str, fresh: dict) -> dict | None:
    base = validate_snapshot(json.loads(base_payload))
    original = {row["key"]: row for row in base["configuration"]}
    fields = {row["key"]: row["value"] for row in fresh["configuration"]
              if row["key"] in original and _normalized(row["key"]) in VISIBLE_KEYS
              and original[row["key"]]["value"] == REDACTED
              and row["value"] != REDACTED}
    record = {"source_event_id": base["source_event_id"],
              "base_sha256": hashlib.sha256(base_payload.encode()).hexdigest(),
              "fields": fields}
    projected = configuration_overlay(base_payload, record) if fields else base
    if projected != fresh:
        raise ValueError("Historical configuration does not match immutable evidence")
    return record if fields else None


def _read_enriched(db, tables, table, source, event_id, payload):
    overlay_table = ("configuration_enrichments" if table == "configuration_snapshots"
                     else "collector_configuration_enrichments")
    if overlay_table in tables:
        row = db.execute(f"SELECT payload FROM {overlay_table} WHERE source_id=? AND source_event_id=?",
                         (source, event_id)).fetchone()
        if row:
            return configuration_overlay(payload, json.loads(row[0]))
    return json.loads(payload)


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


def import_history_page(database: str | Path, page: dict[str, Any], *, apply: bool = False,
                        reconcile: bool = False) -> int:
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
    if reconcile:
        return reconcile_history(database, data["source_id"], normalized, apply=apply)
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


def reconcile_history(database, source, items, *, apply=False):
    """Append verified disclosure overlays only, with a read-only preview."""
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError("Reconciliation requires an existing Reports warehouse")
    with sqlite3.connect(path.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True) as db:
        if not apply:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sources", "configuration_snapshots"} <= tables:
            raise ValueError("Local source-scoped warehouse required")
        if apply and "configuration_enrichments" not in tables:
            raise ValueError("Deploy configuration enrichment schema before --apply")
        if not db.execute("SELECT 1 FROM sources WHERE source_id=?", (source,)).fetchone():
            raise ValueError("Backfill source is not registered in this local warehouse")
        count = 0
        for item in items:
            event = item["source_event_id"]
            base = db.execute("SELECT payload FROM configuration_snapshots WHERE source_id=? AND source_event_id=?",
                              (source, event)).fetchone()
            if not base:
                continue  # Reconcile existing snapshots only; ordinary import is separate.
            record = _enrichment_record(base[0], item)
            prior = (db.execute("SELECT payload FROM configuration_enrichments WHERE source_id=? AND source_event_id=?",
                                (source, event)).fetchone() if "configuration_enrichments" in tables else None)
            if prior:
                if not record or prior[0] != _canonical(record):
                    raise ValueError("Conflicting immutable configuration enrichment")
                continue
            if record:
                count += 1
                if apply:
                    db.execute("INSERT INTO configuration_enrichments VALUES (?,?,?)",
                               (source, event, _canonical(record)))
        return count


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
        names = [table for table in ("configuration_snapshots", "collector_configuration_snapshots")
                 if table in tables]
        for table, rows in zip(names, streams):
            for event_id, payload in rows:
                if event_id not in distinct:
                    distinct[event_id] = _read_enriched(db, tables, table, source, event_id, payload)
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
                return _read_enriched(db, tables, table, source, event_id, row[0])
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
