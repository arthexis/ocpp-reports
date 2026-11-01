"""Read-only warehouse queries shared by the dashboard and future consumers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path


def snapshot(database: Path, *, source_id: str | None = None, charger_id: str | None = None,
             limit: int = 100, allowed_source_ids: tuple[str, ...] | None = None,
             transaction_limit: int | None = None) -> dict:
    if transaction_limit is None:
        transaction_limit = limit
    if not 1 <= transaction_limit <= 500:
        raise ValueError("transaction_limit must be between 1 and 500")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if allowed_source_ids is not None and not allowed_source_ids:
        raise ValueError("Source allowlist must not be empty")

    def source_filter():
        where, args = [], []
        if allowed_source_ids is not None:
            where.append("source_id IN (" + ",".join("?" for _ in allowed_source_ids) + ")")
            args.extend(allowed_source_ids)
        if source_id:
            where.append("source_id=?")
            args.append(source_id)
        return where, args

    def source_clause():
        where, args = source_filter()
        return (" WHERE " + " AND ".join(where) if where else ""), args

    scope_clause, scope_args = source_clause()
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Warehouse not found: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = [dict(r) for r in db.execute(
            "SELECT source_id,cursor,last_ingested_at FROM sources" + scope_clause + " ORDER BY source_id",
            scope_args
        )] if "sources" in tables else []
        # Nominal power is configured CSMS metadata, never a meter sample.
        ratings: dict[tuple[str, str, int], tuple[int | None, str | None]] = {}
        if "connector_ratings" in tables:
            clause, args = source_clause()
            for row in db.execute(
                "SELECT source_id,charger_id,connector_id,rated_power_w,rating_scope "
                "FROM connector_ratings" + clause, args
            ):
                ratings[(row["source_id"], row["charger_id"], int(row["connector_id"]))] = (
                    row["rated_power_w"], row["rating_scope"]
                )
        connectors = []
        if "connector_state" in tables:
            where, args = source_filter()
            if charger_id:
                where.append("charger_id=?")
                args.append(charger_id)
            clause = " WHERE " + " AND ".join(where) if where else ""
            connectors = [{**{k: v for k, v in dict(r).items() if k != "payload"},
                           "power_observed_at": json.loads(r["payload"]).get("power_observed_at"),
                           "energy_observed_at": json.loads(r["payload"]).get("energy_observed_at"),
                           "status_observed_at": json.loads(r["payload"]).get("status_observed_at")}
                          for r in db.execute(
                "SELECT source_id,charger_id,connector_id,status,connected,transaction_id,"
                "power_w,energy_wh,observed_at,synced_at,payload FROM connector_state" + clause +
                " ORDER BY source_id,charger_id,connector_id", args)]
        def recent(table, ordering, column, row_limit):
            if table not in tables:
                return []
            where, args = source_filter()
            if charger_id:
                where.append("charger_id=?" if table == "current_transactions" else "json_extract(payload, '$.charger_id')=?")
                args.append(charger_id)
            clause = " WHERE " + " AND ".join(where) if where else ""
            rows = db.execute(f"SELECT source_id,{column},payload FROM {table}" +
                              clause + f" ORDER BY {ordering} DESC LIMIT ?", (*args, row_limit))
            return [{**json.loads(r["payload"]), "source_id": r["source_id"]} for r in rows]
        transactions = recent("current_transactions", "COALESCE(json_extract(payload, '$.stopped_at'), json_extract(payload, '$.started_at'), imported_at)", "transaction_id", transaction_limit)
        events = recent("events", "event_id", "event_id", limit)
        # Replicated evidence is stored separately from local ingestion. Expose both
        # through the same read-only dashboard without rewriting source evidence.
        if "collector_latest" in tables:
            remote = db.execute(
                "SELECT source_id,kind,record_key,payload FROM collector_latest"
                + scope_clause
                + " ORDER BY source_id,kind,record_key",
                scope_args,
            ).fetchall()
            receipt_times = {}
            if "collector_receipts" in tables:
                receipt_times = {r["source_id"]: r["received_at"] for r in db.execute(
                    "SELECT source_id,received_at FROM collector_receipts" + scope_clause, scope_args
                )}
            if "collector_cursors" in tables:
                cursor_rows = db.execute(
                    "SELECT source_id,sequence FROM collector_cursors"
                    + scope_clause,
                    scope_args,
                )
                for r in cursor_rows:
                    if r["source_id"] not in {s["source_id"] for s in sources}:
                        sources.append({"source_id": r["source_id"], "cursor": r["sequence"],
                                        "last_ingested_at": None,
                                        "last_replication_received_at": receipt_times.get(r["source_id"])})
            for s in sources:
                s.setdefault("last_replication_received_at", receipt_times.get(s["source_id"]))
            existing_connectors = {(r["source_id"], r["charger_id"], r["connector_id"]) for r in connectors}
            existing_transactions = {(r["source_id"], r.get("charger_id"), r.get("id")) for r in transactions}
            existing_events = {(r["source_id"], r.get("id")) for r in events}
            for item in remote:
                source = item["source_id"]
                payload = json.loads(item["payload"])
                if item["kind"] == "rating":
                    charger = payload.get("charger_id")
                    connector = payload.get("connector_id")
                    if type(connector) is int and connector >= 0 and isinstance(charger, str):
                        ratings[(source, charger, connector)] = (
                            payload.get("rated_power_w"), payload.get("rating_scope")
                        )
                elif item["kind"] == "connector":
                    charger = payload.get("charger_id")
                    connector = payload.get("connector_id")
                    if charger_id and charger != charger_id:
                        continue
                    identity = (source, charger, connector)
                    if identity not in existing_connectors:
                        connectors.append({"source_id": source, **{key: payload.get(key) for key in ("charger_id", "connector_id", "status", "connected", "transaction_id", "power_w", "energy_wh", "observed_at", "synced_at", "power_observed_at", "energy_observed_at", "status_observed_at")}})
                        existing_connectors.add(identity)
                elif item["kind"] == "transaction":
                    charger = payload.get("charger_id")
                    if charger_id and charger != charger_id:
                        continue
                    identity = (source, charger, payload.get("id"))
                    if identity not in existing_transactions:
                        transactions.append({**payload, "source_id": source})
                        existing_transactions.add(identity)
                elif item["kind"] == "event":
                    charger = payload.get("charger_id")
                    if charger_id and charger != charger_id:
                        continue
                    identity = (source, payload.get("id"))
                    if identity not in existing_events:
                        events.append({**payload, "source_id": source})
                        existing_events.add(identity)
            sources.sort(key=lambda r: r["source_id"])
            connectors.sort(key=lambda r: (r["source_id"], str(r.get("charger_id")), str(r.get("connector_id"))))
            transactions.sort(key=lambda r: str(r.get("stopped_at") or r.get("started_at") or ""), reverse=True)
            events.sort(key=lambda r: (r["source_id"], r.get("id") or 0), reverse=True)
            transactions = transactions[:transaction_limit]
            events = events[:limit]
        # Collector metadata enrichment after source-scoped queries. Never use
        # location to authorize sources or to alter replicated evidence.
        locations = {}
        if "collector_charger_locations" in tables:
            scope, arguments = source_clause()
            for row in db.execute(
                "SELECT source_id,charger_id,location FROM collector_charger_locations"
                + scope, arguments
            ):
                locations[(row["source_id"], row["charger_id"])] = row["location"]
        # Overlay ratings by source+charger+connector, preserving independent
        # customer scopes and never substituting nominal values for power_w.
        for connector in connectors:
            key = (connector["source_id"], connector["charger_id"], connector["connector_id"])
            rated, scope = ratings.get(key, (None, None))
            connector["rated_power_w"] = rated
            connector["rating_scope"] = scope
        for rows in (connectors, transactions, events):
            for row in rows:
                source = row.get("source_id")
                charger = row.get("charger_id")
                row["location"] = locations.get((source, charger), source)
    # Only a recent, timestamped observation can be shown as live measured
    # power. Old meter readings are retained in raw evidence, not current UI.
    reference = datetime.now(timezone.utc)
    for connector in connectors:
        at = connector.get("power_observed_at")
        watts = connector.get("power_w")
        connector["power_fresh"] = False
        if isinstance(at, str) and isinstance(watts, (int, float)) and not isinstance(watts, bool):
            try:
                moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
                if moment.tzinfo is not None:
                    age = reference - moment.astimezone(timezone.utc)
                    connector["power_fresh"] = timedelta(0) <= age <= timedelta(seconds=120)
            except ValueError:
                pass
    # Estimates are never finalized consumption and must carry actual meter age.
    now = datetime.now(timezone.utc)
    for transaction in transactions:
        transaction["live_energy_stale"] = True
        value = transaction.get("live_energy_wh")
        at = transaction.get("live_energy_observed_at")
        if transaction.get("energy_wh") is not None or value is None or not isinstance(at, str):
            continue
        if not transaction.get("active") or transaction.get("state") not in ("open", "active", "charging"):
            continue
        try:
            measured = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if measured.tzinfo is None:
                continue
            age = now - measured.astimezone(timezone.utc)
            transaction["live_energy_stale"] = not (timedelta(0) <= age <= timedelta(minutes=5))
        except ValueError:
            pass
    return {"sources": sources, "connectors": connectors,
            "transactions": transactions, "events": events}
