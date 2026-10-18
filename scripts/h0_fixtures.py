#!/usr/bin/env python3
"""H0b: bounded, allowlisted offline SQLite fixtures. Never exports raw payloads."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

# No RFID, names, charger serials, payloads, tokens or raw identifier values.
FIELDS = {
    "events": ("id", "received_at", "timestamp", "action", "direction"),
    "transactions": ("id", "started_at", "stopped_at", "start_time", "stop_time",
                     "meter_start", "meter_stop", "energy_kwh", "energy_wh"),
    "ocpp_transaction": ("id", "start_time", "stop_time", "started_at",
                         "stopped_at", "meter_start", "meter_stop", "energy"),
    "ocpp_metervalue": ("id", "timestamp", "energy", "value", "unit"),
}
IDENTIFIERS = {"id"}
# Restrict string outputs to a known finite vocabulary; arbitrary free text
# (including event actions) is NOT emitted.
SAFE_ENUMS = {
    "action": {"Heartbeat", "BootNotification", "StartTransaction",
               "StopTransaction", "MeterValues", "StatusNotification",
               "Authorize", "DataTransfer"},
    "direction": {"in", "out", "inbound", "outbound"},
    "unit": {"Wh", "kWh", "W", "kW"},
}
NUMERIC = {"meter_start", "meter_stop", "energy", "energy_kwh", "energy_wh", "value"}
TIMES = {"received_at", "timestamp", "start_time", "stop_time", "started_at", "stopped_at"}


def pseudonym(value: object, salt: str) -> str:
    return hashlib.sha256((salt + ":" + str(value)).encode()).hexdigest()[:20]


def extract(path: Path, *, salt: str, limit: int = 5) -> dict:
    if not salt or limit < 1 or limit > 100:
        raise ValueError("A nonempty private salt and limit in 1..100 are required")
    if not path.is_file():
        raise FileNotFoundError(path)
    # Use a self-contained offline snapshot only; immutable ignores WAL.
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        result = {"format": "h0-sanitized-fixtures/v1",
                  "artifact_sha256": digest,
                  "user_version": db.execute("PRAGMA user_version").fetchone()[0],
                  "rows": {}, "warnings": [
                      "Energy units and naive timestamp timezone are unverified",
                      "Bounded illustrative samples are not a completeness check",
                      "Pseudonyms are scoped to the operator-provided private salt",
                  ]}
        for table, candidates in FIELDS.items():
            if table not in tables:
                continue
            present = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            columns = [c for c in candidates if c in present]
            if not columns:
                continue
            selection = ", ".join(f'"{c}"' for c in columns)
            # SQLite rowid order is deterministic for these observed historical
            # tables; avoid exporting any actual native PK.
            rows = db.execute(
                f'SELECT {selection} FROM "{table}" ORDER BY rowid LIMIT ?', (limit,)
            )
            sample = []
            for values in rows:
                item = {}
                for col, value in zip(columns, values):
                    if value is None:
                        item[col] = None
                    elif col in IDENTIFIERS:
                        item[col] = pseudonym(value, salt + ":" + table)
                    elif col in NUMERIC:
                        # Do not export unexpected text masquerading as a number.
                        item[col] = value if type(value) in (int, float) else None
                    elif col in TIMES:
                        # Only accept ISO-like date/time; reject arbitrary text.
                        s = str(value)
                        item[col] = s if len(s) <= 40 and len(s) >= 10 and s[:4].isdigit() and s[4] == "-" else None
                    elif col in SAFE_ENUMS:
                        item[col] = value if value in SAFE_ENUMS[col] else "other"
                sample.append(item)
            result["rows"][table] = sample
        return result
    finally:
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("backup", type=Path, help="Quiescent offline SQLite backup")
    p.add_argument("--output", required=True, type=Path, help="New output file, never overwritten")
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()
    salt = os.environ.get("H0_FIXTURE_SALT")
    if not salt or len(salt) < 16:
        p.error("H0_FIXTURE_SALT must be a private random string of at least 16 characters")
    result = extract(args.backup, salt=salt, limit=args.limit)
    # Mode x protects existing outputs. Do not include the secret salt.
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    main()
