"""Authenticated, idempotent M3 collector.

Credentials are provisioned out-of-band; tokens are never stored in the warehouse.
"""
from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from ..warehouse.collector_schema import ensure_collector_schema
from ..warehouse.config_history import validate_snapshot, configuration_overlay
from ..contracts.csms_export import validate_meter_fields, _timestamp
from ..warehouse.history_enrichment import verified_overlay, EXTRA


class Change(BaseModel):
    sequence: int = Field(gt=0)
    kind: str
    record_key: str
    payload: dict


class Batch(BaseModel):
    schema_: str = Field(alias="schema")
    source_id: str = Field(min_length=1)
    after: int = Field(ge=0)
    next: int = Field(ge=0)
    more: bool
    changes: list[Change] = Field(max_length=500)


class ReadyRequest(BaseModel):
    source_id: str = Field(min_length=1)
    capabilities: list[str] | None = None


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _meter_identity(change: Change) -> tuple[int, int, str, int | None, int | None, str]:
    """Reject unscoped or fabricated replicated meter identities."""
    sample = change.payload
    event, index = sample.get("source_event_id"), sample.get("sample_index")
    charger = sample.get("charger_id")
    connector, transaction = sample.get("connector_id"), sample.get("transaction_id")
    stamp = sample.get("at")
    if (type(event) is not int or event <= 0
            or type(index) is not int or index < 0
            or change.record_key != f"{event}:{index}"
            or not isinstance(charger, str) or not charger
            or connector is not None and (type(connector) is not int or connector < 0)
            or transaction is not None and (type(transaction) is not int or transaction < 0)):
        raise ValueError("Invalid meter sample identity")
    _timestamp(stamp, "meter_sample.at")
    validate_meter_fields(sample)
    return event, index, charger, connector, transaction, stamp


def _enrichment_identity(change: Change) -> tuple[int, int]:
    evidence = change.payload
    event, index = evidence.get("source_event_id"), evidence.get("sample_index")
    digest, fields = evidence.get("base_sha256"), evidence.get("fields")
    if (type(event) is not int or event <= 0
            or type(index) is not int or index < 0
            or change.record_key != f"{event}:{index}"
            or not isinstance(digest, str) or len(digest) != 64
            or any(letter not in "0123456789abcdef" for letter in digest)
            or not isinstance(fields, dict) or not fields
            or not set(fields) <= EXTRA):
        raise ValueError("Invalid historical enrichment identity")
    validate_meter_fields(fields)
    return event, index


class SourceHeldError(ValueError):
    """The operator has drained and held this source for maintenance."""


def accept_batch(database: Path, batch: Batch) -> int:
    if batch.schema_ != "ocpp-reports/replication/v1":
        raise ValueError("Unsupported replication schema")
    if any(change.kind not in {"event", "transaction", "connector", "rating", "meter_sample", "meter_enrichment", "config_snapshot", "config_enrichment"} for change in batch.changes):
        raise ValueError("Unsupported change kind")
    positions = [change.sequence for change in batch.changes]
    if positions != sorted(set(positions)) or any(n <= batch.after for n in positions):
        raise ValueError("Invalid change sequence")
    if batch.next != (positions[-1] if positions else batch.after):
        raise ValueError("Invalid batch next cursor")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        ensure_collector_schema(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM collector_source_holds WHERE source_id=?",
                      (batch.source_id,)).fetchone():
            raise SourceHeldError("Source forwarding held for maintenance")
        row = db.execute("SELECT sequence FROM collector_cursors WHERE source_id=?",
                         (batch.source_id,)).fetchone()
        cursor = row[0] if row else 0
        if batch.after > cursor:
            raise ValueError("Replication cursor gap")
        for change in batch.changes:
            encoded = _canonical(change.payload)
            old = db.execute(
                "SELECT kind,record_key,payload FROM collector_changes WHERE source_id=? AND sequence=?",
                (batch.source_id, change.sequence)).fetchone()
            if old:
                if old != (change.kind, change.record_key, encoded):
                    raise ValueError("Conflicting replay")
                continue
            if change.sequence <= cursor:
                raise ValueError("Missing historical replication change")
            if change.kind == "config_snapshot":
                item = validate_snapshot(change.payload)
                if change.record_key != str(item["source_event_id"]):
                    raise ValueError("Conflicting configuration record key")
                original = db.execute(
                    "SELECT payload FROM collector_configuration_snapshots "
                    "WHERE source_id=? AND source_event_id=?",
                    (batch.source_id, item["source_event_id"]),
                ).fetchone()
                if original is not None and original[0] != _canonical(item):
                    raise ValueError("Conflicting immutable configuration history")
                db.execute(
                    "INSERT OR IGNORE INTO collector_configuration_snapshots "
                    "(source_id,charger_id,source_event_id,at,payload) VALUES (?,?,?,?,?)",
                    (batch.source_id, item["charger_id"], item["source_event_id"],
                     item["at"], _canonical(item)),
                )
            if change.kind == "config_enrichment":
                event = change.payload.get("source_event_id")
                if type(event) is not int or event <= 0 or change.record_key != str(event):
                    raise ValueError("Invalid configuration enrichment identity")
                base = db.execute("SELECT payload FROM collector_configuration_snapshots WHERE source_id=? AND source_event_id=?",
                                  (batch.source_id, event)).fetchone()
                if base is None:
                    raise ValueError("Missing immutable configuration base")
                configuration_overlay(base[0], change.payload)
                prior = db.execute("SELECT payload FROM collector_configuration_enrichments WHERE source_id=? AND source_event_id=?",
                                   (batch.source_id, event)).fetchone()
                if prior and prior[0] != encoded:
                    raise ValueError("Conflicting immutable configuration enrichment")
                db.execute("INSERT OR IGNORE INTO collector_configuration_enrichments VALUES (?,?,?)",
                           (batch.source_id, event, encoded))
            if change.kind == "meter_sample":
                event, index, charger, connector, transaction, stamp = _meter_identity(change)
                prior = db.execute(
                    "SELECT payload FROM collector_meter_samples"
                    " WHERE source_id=? AND event_id=? AND sample_index=?",
                    (batch.source_id, event, index),
                ).fetchone()
                if prior is not None and prior[0] != encoded:
                    raise ValueError("Conflicting immutable replicated meter sample")
                pending = db.execute(
                    "SELECT payload FROM collector_meter_enrichments WHERE source_id=?"
                    " AND event_id=? AND sample_index=?",
                    (batch.source_id, event, index),
                ).fetchone()
                if pending is not None and verified_overlay(
                    change.payload, encoded, json.loads(pending[0])
                ) is None:
                    raise ValueError("Pending historical enrichment does not match sample")
                db.execute("""INSERT OR IGNORE INTO collector_meter_samples
                    (source_id,event_id,sample_index,charger_id,connector_id,
                     transaction_id,observed_at,payload) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch.source_id, event, index, charger, connector,
                     transaction, stamp, encoded))
            if change.kind == "meter_enrichment":
                event, index = _enrichment_identity(change)
                prior = db.execute(
                    "SELECT payload FROM collector_meter_enrichments "
                    "WHERE source_id=? AND event_id=? AND sample_index=?",
                    (batch.source_id, event, index),
                ).fetchone()
                if prior is not None and prior[0] != encoded:
                    raise ValueError("Conflicting immutable historical enrichment")
                base = db.execute(
                    "SELECT payload FROM collector_meter_samples "
                    "WHERE source_id=? AND event_id=? AND sample_index=?",
                    (batch.source_id, event, index),
                ).fetchone()
                if base is not None and verified_overlay(
                    json.loads(base[0]), base[0], change.payload
                ) is None:
                    raise ValueError("Historical enrichment does not match base sample")
                db.execute("INSERT OR IGNORE INTO collector_meter_enrichments "
                           "VALUES (?,?,?,?)",
                           (batch.source_id, event, index, encoded))
            db.execute("INSERT INTO collector_changes VALUES (?,?,?,?,?)",
                       (batch.source_id, change.sequence, change.kind, change.record_key, encoded))
            if change.kind not in {"meter_sample", "meter_enrichment", "config_snapshot", "config_enrichment"}:
                db.execute("""INSERT INTO collector_latest VALUES (?,?,?,?,?)
                    ON CONFLICT(source_id,kind,record_key) DO UPDATE SET
                    sequence=excluded.sequence,payload=excluded.payload
                    WHERE excluded.sequence > collector_latest.sequence""",
                    (batch.source_id, change.kind, change.record_key, change.sequence, encoded))
        cursor = max(cursor, batch.next)
        # Receipt freshness advances only when new contiguous evidence is stored.
        if positions and any(position > (row[0] if row else 0) for position in positions):
            db.execute("""INSERT INTO collector_receipts VALUES (?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                    received_at=excluded.received_at, sequence=excluded.sequence""",
                (batch.source_id, datetime.now(timezone.utc).isoformat(), cursor))
        db.execute("""INSERT INTO collector_cursors VALUES (?,?)
            ON CONFLICT(source_id) DO UPDATE SET sequence=excluded.sequence""",
            (batch.source_id, cursor))
    return cursor


def create_collector(database: str | Path, credentials: dict[str, str]) -> FastAPI:
    """Map source IDs to independent bearer tokens. Bind to loopback behind TLS."""
    if any(not key or not value for key, value in credentials.items()):
        raise ValueError("Collector requires nonempty source credentials")
    app = FastAPI(title="OCPP Reports Collector", docs_url=None, redoc_url=None,
                  openapi_url=None)
    warehouse = Path(database)


    @app.post("/api/replication/ready")
    def readiness(query: ReadyRequest, authorization: str | None = Header(default=None)):
        expected = credentials.get(query.source_id)
        if not expected or not authorization or not hmac.compare_digest(
            authorization, "Bearer " + expected
        ):
            raise HTTPException(401, "Invalid source credentials",
                                headers={"WWW-Authenticate": "Bearer"})
        cursor = 0
        held = False
        # Never create the warehouse or run schema migration during readiness.
        if warehouse.is_file():
            with sqlite3.connect(warehouse.resolve().as_uri() + "?mode=ro", uri=True) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if "collector_source_holds" in tables:
                    held = db.execute(
                        "SELECT 1 FROM collector_source_holds WHERE source_id=?",
                        (query.source_id,),
                    ).fetchone() is not None
                if "collector_cursors" in tables:
                    row = db.execute(
                        "SELECT sequence FROM collector_cursors WHERE source_id=?",
                        (query.source_id,),
                    ).fetchone()
                    cursor = int(row[0]) if row else 0
        if held:
            raise HTTPException(503, "Source forwarding held",
                                headers={"Retry-After": "60"})
        result = {"schema": "ocpp-reports/readiness/v1", "source_id": query.source_id,
                  "ready": True, "held": False, "cursor": cursor}
        # Legacy callers preserve their exact v1 response. A new Forwarder
        # must explicitly request the optional negotiation extension.
        if query.capabilities is not None:
            result["supported_kinds"] = [
                "event", "transaction", "connector", "rating",
                "meter_sample", "meter_enrichment", "config_snapshot", "config_enrichment",
            ]
        return result

    @app.post("/api/replication")
    def receive(batch: Batch, authorization: str | None = Header(default=None)):
        expected = credentials.get(batch.source_id)
        if not expected or not authorization or not hmac.compare_digest(
            authorization, "Bearer " + expected
        ):
            raise HTTPException(401, "Invalid source credentials",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            cursor = accept_batch(warehouse, batch)
        except SourceHeldError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "60"}) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"source_id": batch.source_id, "ack": cursor}

    @app.post("/api/diagnostics")
    async def receive_diagnostic(request: Request, source_id: str, cp: str, filename: str,
                                 authorization: str | None = Header(default=None)):
        expected = credentials.get(source_id)
        if not expected or not authorization or not hmac.compare_digest(authorization, "Bearer " + expected):
            raise HTTPException(401, "Invalid source credentials")
        from ..diagnostic_archive import DEFAULT_ARCHIVE, store, validate
        from io import BytesIO
        try:
            validate(source_id, cp)
            limit = 100 * 1024 * 1024
            payload = bytearray()
            async for chunk in request.stream():
                payload.extend(chunk)
                if len(payload) > limit:
                    raise HTTPException(413, "Diagnostic archive too large")
            return store(DEFAULT_ARCHIVE, source_id, cp, filename, BytesIO(payload), maximum=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app
