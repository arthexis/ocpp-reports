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
from ..warehouse.collector_schema import ensure_collector_schema, collector_generation


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
    forward_ack: int | None = Field(default=None, ge=0)


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class SourceHeldError(ValueError):
    """The operator has drained and held this source for maintenance."""


def accept_batch(database: Path, batch: Batch) -> int:
    if batch.schema_ != "ocpp-reports/replication/v1":
        raise ValueError("Unsupported replication schema")
    if any(change.kind not in {"event", "transaction", "connector", "rating"} for change in batch.changes):
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
            db.execute("INSERT INTO collector_changes VALUES (?,?,?,?,?)",
                       (batch.source_id, change.sequence, change.kind, change.record_key, encoded))
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
    if not credentials or any(not key or not value for key, value in credentials.items()):
        raise ValueError("Collector requires nonempty source credentials")
    app = FastAPI(title="OCPP Reports Collector", docs_url=None, redoc_url=None,
                  openapi_url=None)
    warehouse = Path(database)


    @app.post("/api/replication/status")
    def replication_status(query: ReadyRequest, authorization: str | None = Header(default=None)):
        """Authenticated idle probe; never interpret cursor zero as proof of reset."""
        expected = credentials.get(query.source_id)
        if not expected or not authorization or not hmac.compare_digest(
            authorization, "Bearer " + expected
        ):
            raise HTTPException(401, "Invalid source credentials",
                                headers={"WWW-Authenticate": "Bearer"})
        warehouse.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(warehouse) as db:
            ensure_collector_schema(db)
            generation = collector_generation(db)
            cursor_row = db.execute(
                "SELECT sequence FROM collector_cursors WHERE source_id=?",
                (query.source_id,),
            ).fetchone()
            held = db.execute(
                "SELECT 1 FROM collector_source_holds WHERE source_id=?",
                (query.source_id,),
            ).fetchone() is not None
            existing = any(db.execute(
                "SELECT 1 FROM " + table + " WHERE source_id=? LIMIT 1",
                (query.source_id,),
            ).fetchone() for table in ("collector_changes", "collector_latest"))
        cursor = int(cursor_row[0]) if cursor_row else 0
        reset_required = (query.forward_ack is not None and query.forward_ack > cursor
                          and not existing and not held)
        return {"schema": "ocpp-reports/status/v1", "source_id": query.source_id,
                "generation": generation, "cursor": cursor,
                "held": held, "has_evidence": existing,
                "reset_required": reset_required}

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
                # Cursor zero alone is insufficient: refuse a reused or inconsistent target.
                if "collector_changes" in tables and db.execute(
                    "SELECT 1 FROM collector_changes WHERE source_id=? LIMIT 1",
                    (query.source_id,),
                ).fetchone():
                    raise HTTPException(409, "Replay target contains existing changes")
                if "collector_latest" in tables and db.execute(
                    "SELECT 1 FROM collector_latest WHERE source_id=? LIMIT 1",
                    (query.source_id,),
                ).fetchone():
                    raise HTTPException(409, "Replay target contains existing projections")
                if "collector_cursors" in tables:
                    row = db.execute(
                        "SELECT sequence FROM collector_cursors WHERE source_id=?",
                        (query.source_id,),
                    ).fetchone()
                    cursor = int(row[0]) if row else 0
        if held:
            raise HTTPException(503, "Source forwarding held",
                                headers={"Retry-After": "60"})
        return {"schema": "ocpp-reports/readiness/v1", "source_id": query.source_id,
                "ready": True, "held": False, "cursor": cursor}

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
