"""Explicit, bounded transaction SoC metadata refresh.

Reads the active CSMS using its own interpreter, then updates ONLY an existing
Reports transaction projection. OCPP events, archived observations, CSMS data,
source IDs and all durable cursors are immutable here. The existing transaction
UPDATE trigger emits the normal Reports changefeed record for forwarding.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .active_csms import active_csms_transaction_soc

CSMS_SCHEMA = "ocpp-csms/transaction-soc/v1"
REPORT_SCHEMA = "ocpp-reports/soc-refresh/v1"


def _validate_evidence(evidence: dict, *, cp: str, transaction: int) -> tuple[str, dict]:
    if not isinstance(evidence, dict) or evidence.get("schema") != CSMS_SCHEMA:
        raise ValueError("Unsupported CSMS transaction SoC metadata contract")
    source = evidence.get("source_id")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("CSMS source identity is missing")
    if evidence.get("charger_id") != cp or evidence.get("transaction_id") != transaction:
        raise ValueError("CSMS transaction identity mismatch")
    if evidence.get("provenance") != "ocpp-meter-values":
        raise ValueError("Untrusted SoC provenance")
    result = {}
    for field in ("soc_start", "soc_end"):
        value = evidence.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= 100):
            raise ValueError("Invalid CSMS SoC percent")
        result[field] = value
    return source, result


def refresh(
    database: Path,
    *,
    cp: str,
    transaction: int,
    dry_run: bool = True,
    expected_source: str | None = None,
    producer: Callable[..., dict] = active_csms_transaction_soc,
) -> dict:
    """Preview or commit one existing transaction's derived SoC fields only."""
    if not cp or type(transaction) is not int or transaction <= 0:
        raise ValueError("Explicit --cp and positive --transaction are required")
    database = Path(database)
    if not database.is_file():
        raise ValueError("Existing Reports warehouse is required")
    evidence = producer(cp=cp, transaction=transaction)
    source, new = _validate_evidence(evidence, cp=cp, transaction=transaction)
    if expected_source is not None and expected_source != source:
        raise ValueError("Selected source does not match active CSMS source identity")
    uri = database.resolve().as_uri() + ("?mode=ro" if dry_run else "?mode=rw")
    with sqlite3.connect(uri, uri=True, timeout=10) as db:
        if dry_run:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
        else:
            db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='replicate_transaction_update'").fetchone() is None:
            raise ValueError("Reports transaction replication trigger missing")
        if db.execute("SELECT 1 FROM sources WHERE source_id=?", (source,)).fetchone() is None:
            raise ValueError("Active CSMS source is not enrolled in this Reports warehouse")
        row = db.execute(
            "SELECT payload FROM current_transactions WHERE source_id=? AND charger_id=? AND transaction_id=?",
            (source, cp, transaction),
        ).fetchone()
        if row is None:
            raise ValueError("Transaction not projected for the selected source and CP; refusing insertion")
        existing = json.loads(row[0])
        if (not isinstance(existing, dict) or existing.get("charger_id") != cp
                or existing.get("id") != transaction):
            raise ValueError("Stored transaction identity mismatch")
        # Historical-only: an active session can still receive new evidence.
        # Avoid racing the continuously ingested live transaction projection.
        if existing.get("active") is True or existing.get("state") in ("open", "active", "charging"):
            raise ValueError("SoC metadata refresh requires a completed transaction")
        changed = {}
        for field, value in new.items():
            prior = existing.get(field)
            if prior is not None and value is not None and prior != value:
                raise ValueError(f"Conflicting {field} evidence; refusing historical overwrite")
            if prior is None and value is not None:
                changed[field] = value
        result = {
            "schema": REPORT_SCHEMA,
            "status": "would_update" if dry_run and changed else "unchanged" if not changed else "updated",
            "source_id": source,
            "charger_id": cp,
            "transaction_id": transaction,
            "soc_start": existing.get("soc_start"),
            "soc_end": existing.get("soc_end"),
            "proposed_soc_start": new["soc_start"],
            "proposed_soc_end": new["soc_end"],
            "fields_changed": sorted(changed),
        }
        if changed and not dry_run:
            patched = dict(existing)
            patched.update(changed)
            patched["soc_metadata_provenance"] = "ocpp-meter-values"
            patched["soc_metadata_refreshed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            encoded = json.dumps(patched, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            db.execute(
                "UPDATE current_transactions SET payload=?, fingerprint=?, imported_at=? "
                "WHERE source_id=? AND charger_id=? AND transaction_id=?",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                 patched["soc_metadata_refreshed_at"], source, cp, transaction),
            )
        return result
