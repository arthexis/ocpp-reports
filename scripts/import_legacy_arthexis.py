#!/usr/bin/env python3
"""One-shot Arthexis/Django OCPP transaction backfill into Reports' local changefeed.

Historical inputs are read-only. Existing CSMS, forwarder and collector cursors
are never changed; the current Reports forwarder publishes the new projections.

Python standard library only. Dry run by default; --apply requires a new backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

# Distinct, stable namespaces within the existing node's source_id.  The archive
# tag is explicit; file paths and DB-local PKs are not globally unique.
LEGACY_BASES = {"primary": 1_000_000_000, "failover-2025": 2_000_000_000}
REQUIRED_REPORTS = {"sources", "current_transactions", "transaction_observations", "replication_changes"}
REQUIRED_OLD = {
    "ocpp_transaction": {"id", "charger_id", "connector_id",
                         "meter_start", "meter_stop", "start_time", "stop_time",
                         "is_deleted", "is_seed_data"},
    "ocpp_charger": {"id", "charger_id", "connector_id"},
}


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Source is not a regular SQLite database: {path}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(db, name):
    return {r[1] for r in db.execute(f'PRAGMA table_info("{name}")')}


def utc_date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Unexpected legacy timestamp type")
    # Original Arthexis Django settings: USE_TZ=True. SQLite stores UTC naive.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def meter(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Unexpected legacy meter type")
    if value < 0 or value > 1_000_000_000_000:
        raise ValueError("Out-of-range meter reading")
    return value


def record(row: sqlite3.Row, charger: str, source: str, archive_tag: str):
    if archive_tag not in LEGACY_BASES:
        raise ValueError("Unknown legacy archive tag")
    legacy_pk = row["id"]
    if type(legacy_pk) is not int or not 0 < legacy_pk < 1_147_483_647:
        raise ValueError("Legacy primary key outside reserved identity namespace")
    connector = row["connector_id"]
    if type(connector) is not int or not 0 < connector < 1000:
        raise ValueError("Unmapped physical connector")
    start = utc_date(row["start_time"])
    stop = utc_date(row["stop_time"])
    if not start:
        raise ValueError("Transaction has no start timestamp")
    if stop and stop < start:
        raise ValueError("Transaction stops before it starts")
    year = datetime.fromisoformat(start.replace("Z", "+00:00")).year
    if (archive_tag == "failover-2025" and year != 2025 or
            archive_tag == "primary" and year < 2026):
        raise ValueError("Legacy archive tag does not match observed transaction year")
    begin, end = meter(row["meter_start"]), meter(row["meter_stop"])
    if begin is not None and end is not None and end < begin:
        raise ValueError("Meter register regressed")
    # Do not classify an incomplete historical transaction as successful even
    # when a stop meter was stored without an actual StopTransaction timestamp.
    energy = end - begin if stop and begin is not None and end is not None else None
    if energy is not None and energy < 0:
        raise ValueError("Negative energy")
    legacy_ocpp_id = row["ocpp_transaction_id"]
    if legacy_ocpp_id is not None and not isinstance(legacy_ocpp_id, (int, str)):
        raise ValueError("Invalid legacy OCPP transaction identifier")
    payload = {
        "id": LEGACY_BASES[archive_tag] + legacy_pk,
        "charger_id": charger,
        "connector_id": connector,
        "started_at": start,
        "stopped_at": stop,
        "state": "completed" if stop else "incomplete",
        "active": False,
        "meter_start_wh": begin,
        "meter_stop_wh": end,
        "energy_wh": energy,
        "quality": {"active": False, "incomplete": stop is None,
                    "missing_meter": energy is None},
        "historical_provenance": {
            "producer": "arthexis-django-sqlite",
            "table": "ocpp_transaction",
            "row_id": legacy_pk,
            "archive_tag": archive_tag,
            "ocpp_transaction_id": str(legacy_ocpp_id or ""),
            "node_source_id": source,
            "timestamps": "django-utc-naive",
            "meters": "legacy-ocpp-wh",
        },
    }
    return payload


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def build_plan(old: sqlite3.Connection, target: sqlite3.Connection, source: str, archive_tag: str):
    if archive_tag not in LEGACY_BASES:
        raise ValueError("Unknown legacy archive tag")
    if not set(REQUIRED_OLD) <= tables(old) or any(
        not cols <= columns(old, name) for name, cols in REQUIRED_OLD.items()
    ):
        raise ValueError("Unsupported Arthexis OCPP schema")
    if not REQUIRED_REPORTS <= tables(target):
        raise ValueError("Missing Reports warehouse schema")
    trigger = target.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='replicate_transaction_insert'").fetchone()
    if not trigger:
        raise ValueError("Reports transaction changefeed trigger unavailable")
    ids = [r[0] for r in target.execute("SELECT source_id FROM sources ORDER BY source_id")]
    if ids != [source]:
        raise ValueError("Expected exactly one existing Reports source matching --source-id")
    live_chargers = {r[0] for r in target.execute(
        "SELECT DISTINCT charger_id FROM current_transactions WHERE source_id=?", (source,))}
    if len(live_chargers) != 1:
        raise ValueError("Expected exactly one current CSMS charger")
    active_charger = next(iter(live_chargers))
    charger_rows = {r[0]: (r[1], r[2]) for r in old.execute(
        "SELECT id,charger_id,connector_id FROM ocpp_charger")}
    # The 2025 Django schema has no ocpp_transaction_id/stop_reason.  Neither
    # field is necessary to prove the start/stop boundary or energy register.
    has_ocpp_id = "ocpp_transaction_id" in columns(old, "ocpp_transaction")
    ocpp_column = "ocpp_transaction_id" if has_ocpp_id else "NULL AS ocpp_transaction_id"
    old_rows = list(old.execute(
        "SELECT id,charger_id,connector_id," + ocpp_column + ",meter_start,meter_stop,"
        "start_time,stop_time,is_deleted,is_seed_data "
        "FROM ocpp_transaction WHERE is_deleted=0 AND is_seed_data=0 ORDER BY id"
    ))
    seen = set()
    projected = []
    duplicates = 0
    for row in old_rows:
        assignment = charger_rows.get(row["charger_id"])
        if assignment is None or assignment[0] != active_charger or assignment[1] != row["connector_id"]:
            raise ValueError("Historical charger/connector identity mismatch")
        payload = record(row, active_charger, source, archive_tag)
        signature = (payload["charger_id"], payload["connector_id"],
                     payload["started_at"], payload["stopped_at"],
                     payload["meter_start_wh"], payload["meter_stop_wh"])
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        projected.append(payload)
    # Compare all existing transactions, including other archive namespaces,
    # rather than relying on recycled OCPP transaction IDs.  Skip identical
    # evidence from another archive; refuse conflicting overlapping starts.
    current = target.execute("SELECT payload FROM current_transactions WHERE source_id=?",
                             (source,)).fetchall()
    previous_starts = {}
    for row in current:
        x = json.loads(row[0])
        key = (x.get("charger_id"), x.get("connector_id"), x.get("started_at"))
        if all(k is not None for k in key):
            previous_starts.setdefault(key, []).append(x)
    kept = []
    prior_archive_duplicates = 0
    for payload in projected:
        start_key = (payload["charger_id"], payload["connector_id"], payload["started_at"])
        overlaps = previous_starts.get(start_key, [])
        skip = False
        for old_payload in overlaps:
            if old_payload.get("id") == payload["id"]:
                # Below: idempotency check by immutable exact bytes.
                continue
            if old_payload.get("historical_provenance", {}).get("producer") != "arthexis-django-sqlite":
                raise ValueError("Legacy transaction overlaps current CSMS start identity")
            fields = ("charger_id", "connector_id", "started_at", "stopped_at",
                      "meter_start_wh", "meter_stop_wh")
            if all(old_payload.get(k) == payload.get(k) for k in fields):
                prior_archive_duplicates += 1
                skip = True
            else:
                raise ValueError("Conflicting overlapping legacy archive transaction")
        if not skip:
            kept.append(payload)
    projected = kept
    new = 0
    for payload in projected:
        key = (source, payload["charger_id"], payload["id"])
        previous = target.execute(
            "SELECT payload FROM current_transactions WHERE source_id=? AND charger_id=? AND transaction_id=?",key
        ).fetchone()
        if previous:
            if previous[0] != canonical(payload):
                raise ValueError(f"Historical identity conflict on row {payload['historical_provenance']['row_id']}")
        else:
            new += 1
    return projected, {"source_id": source, "charger_id": active_charger,
                       "archive_tag": archive_tag,
                       "previous_archive_duplicates": prior_archive_duplicates,
                       "old_rows": len(old_rows), "exact_duplicates_skipped": duplicates,
                       "projected": len(projected), "to_insert": new,
                       "already_present": len(projected)-new,
                       "completed": sum(p["stopped_at"] is not None for p in projected),
                       "incomplete": sum(p["stopped_at"] is None for p in projected),
                       "unknown_energy": sum(p["energy_wh"] is None for p in projected)}


def backup_database(database: Path, destination: Path):
    if destination.resolve() in (database.resolve(),):
        raise ValueError("Backup must not overwrite the warehouse")
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError("Backup path must be new, under an existing directory")
    if destination.parent.stat().st_mode & 0o002:
        raise ValueError("World-writable backup directory is not allowed")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(database.resolve().as_uri()+"?mode=ro",uri=True) as old:
            with sqlite3.connect(destination) as new:
                old.backup(new)
                if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup failed integrity_check")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def import_once(legacy: Path, warehouse: Path, source: str, *, archive_tag: str = "primary", apply: bool, backup: Path | None):
    if legacy.resolve() == warehouse.resolve():
        raise ValueError("Legacy and target databases must differ")
    if not warehouse.is_file() or warehouse.is_symlink():
        raise ValueError("Reports warehouse is missing or unsafe")
    with open_readonly(legacy) as old, open_readonly(warehouse) as current:
        rows, summary = build_plan(old, current, source, archive_tag)
    summary["mode"] = "apply" if apply else "dry-run"
    if not apply or summary["to_insert"] == 0:
        return summary
    if backup is None:
        raise ValueError("--backup is mandatory for --apply with new rows")
    backup_database(warehouse, backup)
    summary["backup"] = str(backup)
    # A single short SQLite write transaction creates the projection and the
    # existing changefeed trigger produces the next forwardable sequence.
    with sqlite3.connect(warehouse, timeout=30) as target:
        target.execute("PRAGMA busy_timeout=30000")
        target.execute("BEGIN IMMEDIATE")
        # Re-evaluate conflicts after acquiring the writer lock.
        with open_readonly(legacy) as old:
            refreshed, _ = build_plan(old, target, source, archive_tag)
        timestamp = datetime.now(timezone.utc).isoformat()
        added = 0
        for p in refreshed:
            encoded = canonical(p)
            key = (source,p["charger_id"],p["id"])
            prior = target.execute(
                "SELECT payload FROM current_transactions WHERE source_id=? AND charger_id=? AND transaction_id=?",key
            ).fetchone()
            if prior:
                if prior[0]!=encoded:raise ValueError("Concurrent historical identity conflict")
                continue
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            values=(source,p["charger_id"],p["id"],0,0,digest,encoded,timestamp)
            target.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",values)
            target.execute("INSERT OR IGNORE INTO transaction_observations VALUES (?,?,?,?,?,?,?,?)",values)
            added+=1
        summary["inserted"] = added
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--legacy-db",type=Path,required=True)
    p.add_argument("--warehouse",type=Path,required=True)
    p.add_argument("--source-id",required=True,help="Existing gway-005 or gway-002 source ID")
    p.add_argument("--archive-tag", choices=tuple(LEGACY_BASES), default="primary",
                   help="Stable original archive namespace; failover-2025 for 2025 gway-005 backup")
    p.add_argument("--apply",action="store_true",help="Write to Reports-owned warehouse")
    p.add_argument("--backup",type=Path,help="NEW, protected warehouse snapshot path (required for writes)")
    a=p.parse_args(argv)
    try:
        result=import_once(a.legacy_db,a.warehouse,a.source_id,archive_tag=a.archive_tag,apply=a.apply,backup=a.backup)
        print(json.dumps(result,sort_keys=True))
    except (OSError,ValueError,sqlite3.Error) as exc:
        print(json.dumps({"status":"failed","type":type(exc).__name__,"reason":str(exc)}),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
