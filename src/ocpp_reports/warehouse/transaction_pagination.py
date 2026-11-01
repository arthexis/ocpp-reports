"""Read-only SQL pagination of local and replicated transaction projections.

The source allowlist is enforced in SQLite before filters, counts or selection.
Only selected transaction payloads are JSON-decoded by Python. No schema
change, cursor advance, or write operation is needed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo


def _local_day(value: object, zone: ZoneInfo) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(zone).date().isoformat()
    except (ValueError, OverflowError):
        return None


def transaction_page(
    db: sqlite3.Connection, tables: set[str], *,
    source_id: str | None, charger_id: str | None,
    allowed_source_ids: tuple[str, ...] | None,
    term: str, status: str, location: str,
    date_from: str, date_to: str, zone: str,
    view: str, page: int, page_size: int = 20,
) -> dict:
    if view not in ("success", "failed", "all"):
        raise ValueError("Invalid transaction view")
    if page < 1 or page_size != 20:
        raise ValueError("Invalid transaction page")
    if allowed_source_ids is not None and not allowed_source_ids:
        raise ValueError("Source allowlist must not be empty")
    for day in (date_from, date_to):
        if day:
            datetime.strptime(day, "%Y-%m-%d")
    if date_from and date_to and date_from > date_to:
        raise ValueError("Date range start must not exceed end")
    timezone = ZoneInfo(zone)
    db.create_function("dashboard_casefold", 1, lambda s: str(s or "").casefold())
    db.create_function("dashboard_local_day", 1, lambda s: _local_day(s, timezone))

    source_predicates, source_args = [], []
    if allowed_source_ids is not None:
        source_predicates.append("t.source_id IN (" + ",".join("?" * len(allowed_source_ids)) + ")")
        source_args.extend(allowed_source_ids)
    if source_id:
        source_predicates.append("t.source_id=?")
        source_args.append(source_id)
    if charger_id:
        source_predicates.append("t.charger_id=?")
        source_args.append(charger_id)
    source_clause = " AND ".join(source_predicates) if source_predicates else "1=1"

    # CTE covers only current projection rows, not the large event/change log.
    # The local projection wins for an identical (source, charger, transaction)
    # when a warehouse serves both its local source and the central collector.
    branches = []
    args = []
    if "current_transactions" in tables:
        branches.append(
            "SELECT t.source_id,t.charger_id,t.transaction_id AS tx_id,t.payload,"
            " t.imported_at AS fallback_time FROM current_transactions t WHERE " + source_clause
        )
        args.extend(source_args)
    if "collector_latest" in tables:
        remote_clause = " AND ".join([
            "t.kind='transaction'",
            source_clause,
        ])
        if "current_transactions" in tables:
            remote_clause += (
                " AND NOT EXISTS (SELECT 1 FROM current_transactions l"
                " WHERE l.source_id=t.source_id AND l.charger_id=json_extract(t.payload,'$.charger_id')"
                " AND l.transaction_id=json_extract(t.payload,'$.id'))"
            )
        # 'charger_id' is stored in the remote JSON payload, not a column.
        remote_source_clause = source_clause.replace("t.charger_id=?", "json_extract(t.payload,'$.charger_id')=?")
        remote_clause = remote_clause.replace(source_clause, remote_source_clause, 1)
        branches.append(
            "SELECT t.source_id,json_extract(t.payload,'$.charger_id') AS charger_id,"
            " json_extract(t.payload,'$.id') AS tx_id,t.payload,"
            " '' AS fallback_time FROM collector_latest t WHERE " + remote_clause
        )
        args.extend(source_args)
    if not branches:
        return {"transactions": [], "counts": {"all": 0, "success": 0, "failed": 0},
                "page": 1, "pages": 1, "total": 0}

    location_join = (
        "LEFT JOIN collector_charger_locations loc ON loc.source_id=x.source_id"
        " AND loc.charger_id=x.charger_id"
        if "collector_charger_locations" in tables else ""
    )
    location_expression = "COALESCE(loc.location,x.source_id)" if location_join else "x.source_id"
    # Keep the established casefold substring and date-overlap semantics.
    filters, filter_args = [], []
    term = term.casefold().strip()
    status = status.casefold().strip()
    location = location.casefold().strip()
    if term:
        filters.append(
            "(instr(dashboard_casefold(x.source_id),?)>0 OR"
            " instr(dashboard_casefold(x.charger_id),?)>0 OR"
            " instr(dashboard_casefold(" + location_expression + "),?)>0 OR"
            " instr(dashboard_casefold(json_extract(x.payload,'$.state')),?)>0)"
        )
        filter_args.extend([term] * 4)
    if location:
        filters.append("instr(dashboard_casefold(" + location_expression + "),?)>0")
        filter_args.append(location)
    if status:
        filters.append("instr(dashboard_casefold(json_extract(x.payload,'$.state')),?)>0")
        filter_args.append(status)
    if date_from or date_to:
        start = "dashboard_local_day(json_extract(x.payload,'$.started_at'))"
        stop = "dashboard_local_day(json_extract(x.payload,'$.stopped_at'))"
        filters.append("COALESCE(" + start + "," + stop + ") IS NOT NULL")
        if date_from:
            filters.append("COALESCE(" + stop + "," + start + ") >= ?")
            filter_args.append(date_from)
        if date_to:
            filters.append("COALESCE(" + start + "," + stop + ") <= ?")
            filter_args.append(date_to)
    filter_clause = " WHERE " + " AND ".join(filters) if filters else ""

    sql = (
        "WITH projected AS (" + " UNION ALL ".join(branches) + "),"
        " classified AS (SELECT x.source_id,x.charger_id,x.tx_id,x.payload,"
        " COALESCE(json_extract(x.payload,'$.stopped_at'),"
        " json_extract(x.payload,'$.started_at'),x.fallback_time) AS sort_at,"
        " CASE WHEN json_type(x.payload,'$.energy_wh') IN ('integer','real')"
        " AND json_extract(x.payload,'$.energy_wh') > 0 THEN 1 ELSE 0 END AS success,"
        " CASE WHEN json_type(x.payload,'$.energy_wh') IN ('integer','real')"
        " AND json_extract(x.payload,'$.energy_wh') = 0 THEN 1 ELSE 0 END AS failed"
        " FROM projected x " + location_join + filter_clause + ") "
    )
    parameters = [*args, *filter_args]
    count = db.execute(
        sql + "SELECT COUNT(*),COALESCE(SUM(success),0),COALESCE(SUM(failed),0) FROM classified",
        parameters,
    ).fetchone()
    counts = {"all": count[0], "success": count[1], "failed": count[2]}
    total = counts[view]
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    view_clause = {"all": "", "success": " WHERE success=1", "failed": " WHERE failed=1"}[view]
    rows = db.execute(
        sql + "SELECT source_id,charger_id,tx_id,payload FROM classified" + view_clause
        + " ORDER BY sort_at DESC,source_id,charger_id,tx_id DESC LIMIT ? OFFSET ?",
        [*parameters, page_size, (page - 1) * page_size],
    ).fetchall()
    return {"transactions": [{**json.loads(r["payload"]), "source_id": r["source_id"]}
                              for r in rows], "counts": counts, "page": page,
            "pages": pages, "total": total}
