"""Explicit, immutable-safe historical OCPP sample enrichment.

Re-export an old, bounded CSMS export/v1 event page with the newer producer.
The original warehouse energy_samples are never rewritten. Each new measured
field must be backed by a matching immutable base sample and source identity.
No source/forwarder/collector cursor is rewound. Dry run is the default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..contracts.csms_export import validate_export, validate_meter_fields

EXTRA = frozenset({
    "soc", "soc_provenance", "voltage_v", "current_import_a",
    "current_offered_a", "temperature_c", "meter_channels",
})
BASE = ("source_event_id", "sample_index", "at", "charger_id",
        "connector_id", "transaction_id", "power_w", "energy_wh")


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def verified_overlay(base: dict, old_payload: str, record: dict) -> dict | None:
    """Fail closed when a stored enrichment is not tied to these exact bytes."""
    if not isinstance(record, dict) or not isinstance(base, dict):
        return None
    if record.get("base_sha256") != hashlib.sha256(old_payload.encode()).hexdigest():
        return None
    if (record.get("source_event_id") != base.get("source_event_id")
            or record.get("sample_index") != base.get("sample_index")):
        return None
    fields = record.get("fields")
    if not isinstance(fields, dict) or not fields or not set(fields) <= EXTRA:
        return None
    if any(field in base and base[field] != value for field, value in fields.items()):
        return None
    merged = {**base, **fields}
    try:
        validate_meter_fields(merged)
    except ValueError:
        return None
    return merged


def enrich_historical_page(database: str | Path, page: dict[str, Any], *,
                           source_id: str, apply: bool = False,
                           max_samples: int = 500) -> dict:
    validate_export(page)
    data = page["data"]
    if (data["source_id"] != source_id or not source_id
            or type(apply) is not bool
            or type(max_samples) is not int or not 1 <= max_samples <= 500
            or len(data["energy"]) > max_samples):
        raise ValueError("Source mismatch or unbounded historical enrichment")
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Missing local warehouse: {path}")
    with sqlite3.connect(path.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True) as db:
        if not apply:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sources", "energy_samples"} <= tables:
            raise ValueError("Local source-scoped warehouse required")
        if apply and not {"sample_enrichments", "replication_changes"} <= tables:
            raise ValueError("Deploy enrichment schema before --apply")
        if not db.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
            raise ValueError("Source identity not found")
        pending = 0
        added = 0
        for sample in data["energy"]:
            event, index = sample["source_event_id"], sample["sample_index"]
            stored = db.execute(
                "SELECT payload FROM energy_samples WHERE source_id=?"
                " AND event_id=? AND sample_index=?",
                (source_id, event, index),
            ).fetchone()
            if stored is None:
                raise ValueError("Cannot enrich missing immutable sample")
            base = json.loads(stored[0])
            if not isinstance(base, dict) or any(base.get(k) != sample.get(k) for k in BASE):
                raise ValueError("Historical sample does not match original evidence")
            fields = {key: sample[key] for key in EXTRA
                      if key in sample and key not in base}
            if not fields:
                continue
            record = {"source_event_id": event, "sample_index": index,
                      "base_sha256": hashlib.sha256(stored[0].encode()).hexdigest(),
                      "fields": fields}
            if verified_overlay(base, stored[0], record) is None:
                raise ValueError("Unverified historical sample enrichment")
            encoded = _canonical(record)
            existing = db.execute(
                "SELECT payload FROM sample_enrichments WHERE source_id=?"
                " AND event_id=? AND sample_index=?",
                (source_id, event, index),
            ).fetchone() if "sample_enrichments" in tables else None
            if existing is not None:
                if existing[0] != encoded:
                    raise ValueError("Conflicting historical enrichment")
                continue
            pending += 1
            if apply:
                db.execute("INSERT INTO sample_enrichments VALUES (?,?,?,?)",
                           (source_id, event, index, encoded))
                added += 1
        return {"source_id": source_id, "dry_run": not apply,
                "scanned": len(data["energy"]), "pending": pending,
                "inserted": added, "cursor_unchanged": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify old CSMS MeterValues without rewriting evidence")
    parser.add_argument("--database", required=True)
    parser.add_argument("--page", required=True, help="Bounded JSON export/v1 page from active producer")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--max-samples", type=int, default=500)
    parser.add_argument("--apply", action="store_true", help="Explicitly append verified enrichment")
    args = parser.parse_args()
    with Path(args.page).open(encoding="utf-8") as stream:
        page = json.load(stream)
    result = enrich_historical_page(args.database, page, source_id=args.source_id,
                                    apply=args.apply, max_samples=args.max_samples)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
