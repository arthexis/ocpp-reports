"""Read-only globally chronological SQLite event grouping and pagination.

Grouping and page boundaries are selected in SQL; event payloads are retrieved
only when a group is expanded. Missing/unparseable timestamps sort last.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from .event_information import event_information


def _day(value: object, zone: ZoneInfo) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(zone).date().isoformat() if timestamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _query(db: sqlite3.Connection, *, source_id: str | None, charger_id: str | None,
           allowed_source_ids: tuple[str, ...] | None, term: str, status: str,
           location: str, date_from: str, date_to: str, zone: str) -> tuple[str, list]:
    if allowed_source_ids is not None and not allowed_source_ids:
        raise ValueError("Source allowlist must not be empty")
    timezone = ZoneInfo(zone)
    for value in (date_from, date_to):
        if value:
            datetime.strptime(value, "%Y-%m-%d")
    if date_from and date_to and date_from > date_to:
        raise ValueError("Date range start must not exceed end")
    db.create_function("ev_casefold", 1, lambda value: str(value or "").casefold())
    db.create_function("ev_local_day", 1, lambda value: _day(value, timezone))

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conditions = []
    args = []
    if allowed_source_ids is not None:
        conditions.append("source_id IN (" + ",".join("?" for _ in allowed_source_ids) + ")")
        args.extend(allowed_source_ids)
    if source_id:
        conditions.append("source_id=?")
        args.append(source_id)
    scope = (" AND ".join(conditions)) if conditions else "1=1"
    pieces = []
    parameters = []
    if "events" in tables:
        pieces.append("SELECT source_id,event_id,payload FROM events WHERE " + scope)
        parameters.extend(args)
    if "collector_latest" in tables:
        remote = (
            "SELECT r.source_id, CAST(json_extract(r.payload,'$.id') AS INTEGER) AS event_id, r.payload "
            "FROM collector_latest r WHERE r.kind='event' AND " + scope
        )
        if "events" in tables:
            remote += (" AND NOT EXISTS (SELECT 1 FROM events l WHERE l.source_id=r.source_id"
                       " AND l.event_id=CAST(json_extract(r.payload,'$.id') AS INTEGER))")
        pieces.append(remote)
        parameters.extend(args)
    if not pieces:
        return "", []
    joined = " UNION ALL ".join(pieces)
    loc_join = (
        " LEFT JOIN collector_charger_locations l ON l.source_id=e.source_id "
        "AND l.charger_id=json_extract(e.payload,'$.charger_id')"
        if "collector_charger_locations" in tables else ""
    )
    loc = "COALESCE(l.location,e.source_id)" if loc_join else "e.source_id"
    filters, filter_args = [], []
    term, status, location = term.casefold().strip(), status.casefold().strip(), location.casefold().strip()
    if charger_id:
        filters.append("json_extract(e.payload,'$.charger_id')=?")
        filter_args.append(charger_id)
    if term:
        filters.append(
            "(instr(ev_casefold(e.source_id),?)>0 OR"
            " instr(ev_casefold(json_extract(e.payload,'$.charger_id')),?)>0 OR"
            " instr(ev_casefold(" + loc + "),?)>0 OR"
            " instr(ev_casefold(json_extract(e.payload,'$.action')),?)>0 OR"
            " instr(ev_casefold(json_extract(e.payload,'$.status')),?)>0)"
        )
        filter_args.extend([term] * 5)
    if status:
        filters.append("instr(ev_casefold(json_extract(e.payload,'$.action')),?)>0")
        filter_args.append(status)
    if location:
        filters.append("instr(ev_casefold(" + loc + "),?)>0")
        filter_args.append(location)
    if date_from or date_to:
        day = "ev_local_day(json_extract(e.payload,'$.at'))"
        filters.append(day + " IS NOT NULL")
        if date_from:
            filters.append(day + " >= ?")
            filter_args.append(date_from)
        if date_to:
            filters.append(day + " <= ?")
            filter_args.append(date_to)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    # A status-less projection may follow an actual StatusNotification by one
    # event ID within milliseconds. Pair ONLY that direct echo with its actual
    # status event, preserving both IDs and each distinct/fault observation.
    routine = "'Available','Preparing','Charging','SuspendedEV','SuspendedEVSE','Finishing','Reserved'"
    status_key = """
      CASE WHEN e.source_id IS NOT NULL AND COALESCE(e.charger_id,'')!=''
                AND e.action='StatusNotification' AND e.kind='status'
                AND e.status IS NOT NULL AND e.connector_id IS NOT NULL
           THEN json_array(e.source_id,e.charger_id,'StatusNotification',
                           e.connector_id,e.status,
                           CASE WHEN e.status IN (""" + routine + """)
                                AND COALESCE(e.error_code,'')=''
                                AND COALESCE(e.info,'')=''
                                THEN 'routine' ELSE e.event_id END)
           ELSE NULL END
    """
    ordering = "(event_time IS NULL),event_time DESC,source_id DESC,event_id DESC"
    next_fields = ("source_id", "charger_id", "action", "kind", "event_id",
                   "event_time", "status_key")
    next_sql = ",".join(
        f"LEAD({field}) OVER (ORDER BY {ordering}) AS next_{field}"
        for field in next_fields
    )
    cte = (
        "WITH raw AS (" + joined + "),"
        " eligible_base AS (SELECT e.source_id,e.event_id,e.payload," + loc + " AS location,"
        " json_extract(e.payload,'$.charger_id') AS charger_id,"
        " json_extract(e.payload,'$.action') AS action,"
        " json_extract(e.payload,'$.kind') AS kind,"
        " json_extract(e.payload,'$.status') AS status,"
        " json_extract(e.payload,'$.connector_id') AS connector_id,"
        " json_extract(e.payload,'$.error_code') AS error_code,"
        " json_extract(e.payload,'$.info') AS info,"
        " json_extract(e.payload,'$.transaction_id') AS transaction_id,"
        " json_extract(e.payload,'$.at') AS at,"
        " julianday(json_extract(e.payload,'$.at')) AS event_time"
        " FROM raw e" + loc_join + where + "),"
        " keyed AS (SELECT e.*," + status_key + " AS status_key FROM eligible_base e),"
        " neighbors AS (SELECT *, " + next_sql + " FROM keyed),"
        " eligible AS (SELECT *, CASE"
        " WHEN action='Heartbeat' AND kind='ocpp'"
        "  AND source_id IS NOT NULL AND COALESCE(charger_id,'')!=''"
        "  THEN json_array(source_id,charger_id,'Heartbeat')"
        " WHEN status_key IS NOT NULL THEN status_key"
        " WHEN action='StatusNotification' AND kind='status'"
        "  AND status IS NULL AND connector_id IS NULL"
        "  AND COALESCE(error_code,'')='' AND COALESCE(info,'')=''"
        "  AND next_source_id=source_id AND next_charger_id=charger_id"
        "  AND next_action=action AND next_kind=kind"
        "  AND next_event_id=event_id-1 AND next_status_key IS NOT NULL"
        "  AND (event_time-next_event_time)*86400 BETWEEN 0 AND 2"
        "  THEN next_status_key"
        " ELSE NULL END AS grouping_key FROM neighbors),"
        " compared AS (SELECT *,LAG(grouping_key) OVER"
        " (ORDER BY " + ordering + ") AS previous_key FROM eligible),"
        " numbered AS (SELECT *,SUM(CASE WHEN grouping_key IS NOT NULL"
        " AND grouping_key=previous_key THEN 0 ELSE 1 END)"
        " OVER (ORDER BY " + ordering + " ROWS UNBOUNDED PRECEDING)"
        " AS group_number FROM compared) "
    )
    return cte, [*parameters, *filter_args]



def _scoped_event_stream(db: sqlite3.Connection, source_id: str, charger_id: str):
    """Stream one charger's events in exact global time order, with one-row lookahead.

    Only the precise source/charger dashboard path uses this bounded-memory
    alternative to multiple whole-history SQLite window passes. Its ordering,
    local precedence and grouping keys match _query(). Raw payloads remain
    private; callers build allowlisted presentation fields only.
    """
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    pieces, args = [], []
    if "events" in tables:
        pieces.append("SELECT source_id,event_id,payload FROM events "
                      "WHERE source_id=? AND json_extract(payload,'$.charger_id')=?")
        args.extend((source_id, charger_id))
    if "collector_latest" in tables:
        remote = (
            "SELECT r.source_id,CAST(json_extract(r.payload,'$.id') AS INTEGER) AS event_id,r.payload "
            "FROM collector_latest r WHERE r.source_id=? AND r.kind='event' "
            "AND json_extract(r.payload,'$.charger_id')=?"
        )
        if "events" in tables:
            remote += (
                " AND NOT EXISTS (SELECT 1 FROM events l WHERE l.source_id=r.source_id "
                "AND l.event_id=CAST(json_extract(r.payload,'$.id') AS INTEGER))"
            )
        pieces.append(remote)
        args.extend((source_id, charger_id))
    if not pieces:
        return
    sql = (
        "SELECT source_id,event_id,payload,"
        "julianday(json_extract(payload,'$.at')) AS event_time FROM ("
        + " UNION ALL ".join(pieces)
        + ") ORDER BY (event_time IS NULL),event_time DESC,source_id DESC,event_id DESC"
    )
    rows = iter(db.execute(sql, args))

    def prepared(record):
        if record is None:
            return None
        source, event_id, raw, event_time = record
        payload = json.loads(raw)
        status_key = None
        if (source is not None and payload.get("charger_id") not in (None, "")
                and payload.get("action") == "StatusNotification"
                and payload.get("kind") == "status"
                and payload.get("status") is not None
                and payload.get("connector_id") is not None):
            state = payload["status"]
            routine = state in (
                "Available", "Preparing", "Charging", "SuspendedEV",
                "SuspendedEVSE", "Finishing", "Reserved"
            ) and payload.get("error_code") in (None, "") and payload.get("info") in (None, "")
            status_key = (source, payload["charger_id"], "StatusNotification",
                          payload["connector_id"], state, "routine" if routine else event_id)
        return {"source_id": source, "event_id": event_id, "payload": payload,
                "event_time": event_time, "status_key": status_key}

    current = prepared(next(rows, None))
    following = prepared(next(rows, None))
    while current is not None:
        e = current["payload"]
        key = current["status_key"]
        if (key is None and current["source_id"] is not None
                and e.get("charger_id") not in (None, "")
                and e.get("action") == "Heartbeat" and e.get("kind") == "ocpp"):
            key = (current["source_id"], e["charger_id"], "Heartbeat")
        if (key is None and e.get("action") == "StatusNotification"
                and e.get("kind") == "status" and e.get("status") is None
                and e.get("connector_id") is None
                and e.get("error_code") in (None, "")
                and e.get("info") in (None, "") and following is not None):
            next_e = following["payload"]
            at, next_at = current["event_time"], following["event_time"]
            if (following["source_id"] == current["source_id"]
                    and next_e.get("charger_id") == e.get("charger_id")
                    and next_e.get("action") == e.get("action")
                    and next_e.get("kind") == e.get("kind")
                    and following["event_id"] == current["event_id"] - 1
                    and following["status_key"] is not None
                    and at is not None and next_at is not None
                    and 0 <= (at - next_at) * 86400 <= 2):
                key = following["status_key"]
        yield current, key
        current = following
        following = prepared(next(rows, None))



def _scoped_visible_events(db: sqlite3.Connection, source_id: str, charger_id: str):
    """Yield OCPP initiators with their adjacent reply ID; retain unpaired evidence.

    Direction is authoritative only when exported explicitly. No attempt is
    made to suppress legacy records without direction. The association also
    requires exact source, charger, action, consecutive IDs and <=2s; event
    IDs are storage IDs, not a wire-level correlation identifier.
    """
    stream = iter(_scoped_event_stream(db, source_id, charger_id))
    newer = next(stream, None)
    while newer is not None:
        older = next(stream, None)
        paired = False
        inferred = False
        if older is not None:
            recent, previous = newer[0], older[0]
            first, second = recent["payload"], previous["payload"]
            time_delta = None
            if recent["event_time"] is not None and previous["event_time"] is not None:
                time_delta = (recent["event_time"] - previous["event_time"]) * 86400
            same_message = (
                first.get("action") == second.get("action")
                and first.get("kind") == second.get("kind")
                and recent["source_id"] == previous["source_id"]
                and first.get("charger_id") == second.get("charger_id")
                and recent["event_id"] == previous["event_id"] + 1
                and time_delta is not None
            )
            paired = (
                same_message and 0 <= time_delta <= 2
                and first.get("direction") in ("in", "out")
                and second.get("direction") in ("in", "out")
                and first["direction"] != second["direction"]
            )
            if (not paired and same_message and 0 < time_delta <= .1
                    and "direction" not in first and "direction" not in second):
                # Legacy CSMS export strips the persisted in/out direction.
                # Status responses have no state or connector; paired
                # Heartbeats are distinguishable only by short elapsed time.
                # Mark these historical associations as inferred in the UI.
                inferred = (
                    (first.get("action") == "StatusNotification"
                     and first.get("kind") == "status"
                     and "derived_status" in first and "derived_status" in second
                     and first.get("status") is None
                     and first.get("connector_id") is None
                     and second.get("status") is not None
                     and type(second.get("connector_id")) is int)
                    or (first.get("action") == "Heartbeat"
                        and first.get("kind") == "ocpp"
                        and set(first) == set(second)
                        and set(first).issubset({"id", "at", "charger_id", "action", "kind"}))
                )
                paired = inferred
        if paired:
            yield older[0], older[1], newer[0]["event_id"], inferred
            newer = next(stream, None)
        else:
            yield newer[0], newer[1], None, False
            newer = older


def _scoped_group_records(db: sqlite3.Connection, source_id: str,
                          charger_id: str, zone: str):
    """Group directed routine statuses by state episode and local day.

    Heartbeats are summarized per local day. An intervening heartbeat or an
    observation from another connector does not fragment the same status
    episode. A changed status, a different day or fault starts another group.
    Unattributed legacy records retain their original adjacent-only rules.
    """
    timezone = ZoneInfo(zone)
    legacy_run, previous_legacy_key = 0, None
    status_episodes = {}
    heartbeat_days = {}
    for row, legacy_key, response_id, inferred in _scoped_visible_events(db, source_id, charger_id):
        payload = row["payload"]
        direction = payload.get("direction")
        if direction not in ("in", "out") and not inferred:
            if legacy_key is None or legacy_key != previous_legacy_key:
                legacy_run += 1
            key = ("legacy", legacy_run)
            previous_legacy_key = legacy_key
        else:
            previous_legacy_key = None
            if inferred:
                direction = "in"
            day = _day(payload.get("at"), timezone)
            action = payload.get("action")
            if direction == "in" and action == "Heartbeat" and day:
                key = heartbeat_days.setdefault(day, ("heartbeat", row["event_id"]))
            elif (direction == "in" and action == "StatusNotification"
                  and type(payload.get("connector_id")) is int
                  and payload.get("status") is not None):
                connector = payload["connector_id"]
                status = payload["status"]
                routine = status in (
                    "Available", "Preparing", "Charging", "SuspendedEV",
                    "SuspendedEVSE", "Finishing", "Reserved"
                ) and payload.get("error_code") in (None, "") and payload.get("info") in (None, "")
                previous = status_episodes.get(connector)
                if routine and day is not None and previous and previous[:2] == (status, day):
                    key = previous[2]
                else:
                    key = ("status", row["event_id"])
                status_episodes[connector] = (status, day, key)
            else:
                key = ("single", row["event_id"])
        yield row, key, response_id, inferred


def _sql_value(value):
    """Normalize JSON scalars to sqlite json_extract representations."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def _sql_max(previous, value):
    value = _sql_value(value)
    if value is None:
        return previous
    if previous is None:
        return value
    # SQLite sorts text above numbers for MAX, then compares within a type.
    if isinstance(value, str):
        return value if not isinstance(previous, str) or value > previous else previous
    if isinstance(previous, str):
        return previous
    return max(previous, value)


def _scoped_location(db: sqlite3.Connection, source_id: str, charger_id: str) -> str:
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "collector_charger_locations" in tables:
        row = db.execute(
            "SELECT location FROM collector_charger_locations "
            "WHERE source_id=? AND charger_id=? LIMIT 1",
            (source_id, charger_id)).fetchone()
        if row and row[0] is not None:
            return row[0]
    return source_id


def _scoped_groups_page(database, *, source_id: str, charger_id: str,
                        allowed_source_ids, zone: str, page: int, page_size: int):
    if allowed_source_ids is not None and not allowed_source_ids:
        raise ValueError("Source allowlist must not be empty")
    ZoneInfo(zone)
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        return {"groups": [], "page": page, "has_next": False}
    groups, by_key = [], {}
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        location = _scoped_location(db, source_id, charger_id)
        for row, key, _, _ in _scoped_group_records(db, source_id, charger_id, zone):
            payload = row["payload"]
            group = by_key.get(key)
            if group is None:
                group = {
                    "source_id": row["source_id"], "charger_id": payload.get("charger_id"),
                    "location": location, "action": payload.get("action"),
                    "at": payload.get("at"), "first_event_id": row["event_id"],
                    "event_count": 0, "kind": payload.get("kind"),
                    "status_for_info": None, "connector_for_info": None,
                    "error_for_info": None, "tx_for_info": None,
                }
                by_key[key] = group
                groups.append(group)
            group["event_count"] += 1
            for field, field_name in (
                ("status", "status_for_info"),
                ("connector_id", "connector_for_info"),
                ("error_code", "error_for_info"),
                ("transaction_id", "tx_for_info"),
            ):
                group[field_name] = _sql_max(group[field_name], payload.get(field))
    selected = groups[(page - 1) * page_size:page * page_size]
    for row in selected:
        row["information"] = event_information({
            "action": row["action"], "kind": row.pop("kind"),
            "status": row.pop("status_for_info"),
            "connector_id": row.pop("connector_for_info"),
            "error_code": row.pop("error_for_info"),
            "transaction_id": row.pop("tx_for_info"),
        })
    return {"groups": selected, "page": page, "has_next": len(groups) > page * page_size}


def _scoped_group_details(database, *, source_id: str, charger_id: str,
                          allowed_source_ids, zone: str, first_event_id: int,
                          offset: int, limit: int):
    ZoneInfo(zone)
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        raise PermissionError("Source is not available in this dashboard")
    first_by_group, selected_key, events, seen = {}, None, [], 0
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        location = _scoped_location(db, source_id, charger_id)
        for row, key, response_id, inferred in _scoped_group_records(db, source_id, charger_id, zone):
            if key not in first_by_group:
                first_by_group[key] = row["event_id"]
                if row["event_id"] == first_event_id:
                    selected_key = key
            if selected_key is None or key != selected_key:
                continue
            if seen >= offset and len(events) <= limit:
                payload = row["payload"]
                event = {
                    "source_id": row["source_id"], "location": location,
                    "id": payload.get("id"), "charger_id": payload.get("charger_id"),
                    "action": payload.get("action"), "at": payload.get("at"),
                    "information": event_information(payload),
                }
                if payload.get("direction") in ("in", "out"):
                    event["direction"] = payload["direction"]
                    event["response_id"] = response_id
                elif inferred:
                    event["response_id"] = response_id
                    event["response_inferred"] = True
                events.append(event)
            seen += 1
            if len(events) > limit:
                break
    if selected_key is None and offset == 0:
        raise LookupError("Event group not found")
    return {"events": events[:limit], "has_next": len(events) > limit}



def event_groups_page(
    database: str | Path, *, source_id: str | None = None, charger_id: str | None = None,
    allowed_source_ids: tuple[str, ...] | None = None, term: str = "", status: str = "",
    location: str = "", date_from: str = "", date_to: str = "", zone: str = "UTC",
    page: int = 1, page_size: int = 10,
) -> dict:
    if page < 1 or page_size != 10:
        raise ValueError("Invalid event group page")
    if source_id and charger_id and not any((term, status, location, date_from, date_to)):
        return _scoped_groups_page(
            database, source_id=source_id, charger_id=charger_id,
            allowed_source_ids=allowed_source_ids, zone=zone,
            page=page, page_size=page_size)
    path = Path(database).resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        cte, args = _query(db, source_id=source_id, charger_id=charger_id,
                           allowed_source_ids=allowed_source_ids, term=term, status=status,
                           location=location, date_from=date_from, date_to=date_to, zone=zone)
        if not cte:
            return {"groups": [], "page": 1, "has_next": False}
        # Rank group numbers instead of materializing the event JSON for each
        # group; the newest row supplies the display information.
        sql = cte + (
            ", ranked AS (SELECT *,COUNT(*) OVER (PARTITION BY group_number) AS event_count,"
            " ROW_NUMBER() OVER (PARTITION BY group_number"
            " ORDER BY (event_time IS NULL),event_time DESC,source_id DESC,event_id DESC)"
            " AS group_rank,"
            " MAX(status) OVER (PARTITION BY group_number) AS status_for_info,"
            " MAX(connector_id) OVER (PARTITION BY group_number) AS connector_for_info,"
            " MAX(error_code) OVER (PARTITION BY group_number) AS error_for_info,"
            " MAX(transaction_id) OVER (PARTITION BY group_number) AS tx_for_info"
            " FROM numbered)"
            " SELECT source_id,charger_id,location,action,at,event_count,"
            " event_id AS first_event_id,status_for_info,connector_for_info,"
            " error_for_info,tx_for_info,kind FROM ranked WHERE group_rank=1"
            " ORDER BY group_number ASC LIMIT ? OFFSET ?"
        )
        rows = [dict(r) for r in db.execute(sql, [*args, page_size + 1, (page - 1) * page_size])]
    for row in rows:
        row["information"] = event_information({
            "action": row["action"], "kind": row.pop("kind"),
            "status": row.pop("status_for_info"),
            "connector_id": row.pop("connector_for_info"),
            "error_code": row.pop("error_for_info"),
            "transaction_id": row.pop("tx_for_info"),
        })
    return {"groups": rows[:page_size], "page": page, "has_next": len(rows) > page_size}


def event_group_details(
    database: str | Path, *, group_source: str, first_event_id: int,
    source_id: str | None = None, charger_id: str | None = None,
    allowed_source_ids: tuple[str, ...] | None = None, term: str = "", status: str = "",
    location: str = "", date_from: str = "", date_to: str = "", zone: str = "UTC",
    offset: int = 0, limit: int = 50,
) -> dict:
    if not group_source or first_event_id < 1 or offset < 0 or not 1 <= limit <= 50:
        raise ValueError("Invalid event group detail selector")
    if allowed_source_ids is not None and group_source not in allowed_source_ids:
        raise PermissionError("Source is not available in this dashboard")
    if (source_id == group_source and charger_id
            and not any((term, status, location, date_from, date_to))):
        return _scoped_group_details(
            database, source_id=source_id, charger_id=charger_id,
            allowed_source_ids=allowed_source_ids, zone=zone,
            first_event_id=first_event_id, offset=offset, limit=limit)
    path = Path(database).resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        cte, args = _query(db, source_id=source_id, charger_id=charger_id,
                           allowed_source_ids=allowed_source_ids, term=term, status=status,
                           location=location, date_from=date_from, date_to=date_to, zone=zone)
        if not cte:
            raise LookupError("Event group not found")
        # The submitted newest ID must actually be the first (newest) member
        # of its group; old/stale or guessed boundaries cannot widen access.
        sql = cte + (
            ", ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY group_number"
            " ORDER BY (event_time IS NULL),event_time DESC,source_id DESC,event_id DESC)"
            " AS group_rank FROM numbered),"
            " selected AS (SELECT group_number FROM ranked"
            " WHERE source_id=? AND event_id=? AND group_rank=1)"
            " SELECT n.source_id,n.payload,n.location FROM ranked n"
            " JOIN selected s USING(group_number)"
            " ORDER BY (n.event_time IS NULL),n.event_time DESC,"
            " n.source_id DESC,n.event_id DESC LIMIT ? OFFSET ?"
        )
        rows = db.execute(sql, [*args, group_source, first_event_id,
                                limit + 1, offset]).fetchall()
    if not rows and offset == 0:
        raise LookupError("Event group not found")
    # Return only columns displayed in the dashboard. Never send raw OCPP
    # evidence, RFID values, vendor payloads or private event metadata.
    events = []
    for row in rows[:limit]:
        payload = json.loads(row["payload"])
        events.append({
            "source_id": row["source_id"], "location": row["location"],
            "id": payload.get("id"), "charger_id": payload.get("charger_id"),
            "action": payload.get("action"), "at": payload.get("at"),
            "information": event_information(payload),
        })
    return {"events": events, "has_next": len(rows) > limit}



def latest_heartbeat_at(
    database: str | Path, *, source_id: str, charger_id: str,
    allowed_source_ids: tuple[str, ...] | None = None,
) -> str | None:
    """Return the last original OCPP Heartbeat timestamp for one charger.

    Event ID orders producer receipts within a source. Local and replicated
    event projections are read without changing them. Ingestion timestamps
    and source freshness are never substituted for the actual Heartbeat.
    """
    if not source_id or not charger_id:
        raise ValueError("A precise source and charger are required")
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        return None
    path = Path(database).resolve()
    candidates: list[tuple[int, str]] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" in tables:
            row = db.execute(
                "SELECT event_id, json_extract(payload,'$.at') FROM events "
                "WHERE source_id=? AND json_extract(payload,'$.charger_id')=? "
                "AND json_extract(payload,'$.action')='Heartbeat' "
                "ORDER BY event_id DESC LIMIT 1",
                (source_id, charger_id),
            ).fetchone()
            if row and isinstance(row[1], str):
                candidates.append((row[0], row[1]))
        if "collector_latest" in tables:
            row = db.execute(
                "SELECT CAST(json_extract(payload,'$.id') AS INTEGER), "
                "json_extract(payload,'$.at') FROM collector_latest "
                "WHERE source_id=? AND kind='event' "
                "AND json_type(payload,'$.id')='integer' "
                "AND json_extract(payload,'$.charger_id')=? "
                "AND json_extract(payload,'$.action')='Heartbeat' "
                "ORDER BY CAST(json_extract(payload,'$.id') AS INTEGER) DESC LIMIT 1",
                (source_id, charger_id),
            ).fetchone()
            if row and isinstance(row[1], str):
                candidates.append((row[0], row[1]))
    if not candidates:
        return None
    at = max(candidates, key=lambda item: item[0])[1]
    try:
        # Unparseable or timezone-free evidence cannot establish a date.
        if datetime.fromisoformat(at.replace("Z", "+00:00")).tzinfo is None:
            return None
    except (ValueError, OverflowError):
        return None
    return at



def latest_charger_receipt_at(database: str | Path, *, source_id: str,
                              charger_id: str,
                              allowed_source_ids: tuple[str, ...] | None = None) -> str | None:
    """Last OCPP event recorded by the source CSMS for this exact charger.

    Do not substitute the source-level warehouse ingestion/replica receipt time:
    a source may contain multiple chargers.
    """
    if not source_id or not charger_id:
        raise ValueError("Exact source and charger required")
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        return None
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = []
        if "events" in tables:
            row = db.execute(
                "SELECT event_id,json_extract(payload,'$.at') FROM events "
                "WHERE source_id=? AND json_extract(payload,'$.charger_id')=? "
                "AND json_extract(payload,'$.kind')='ocpp' "
                "ORDER BY event_id DESC LIMIT 1", (source_id, charger_id)).fetchone()
            if row and type(row[0]) is int and isinstance(row[1], str):
                rows.append(row)
        if "collector_latest" in tables:
            row = db.execute(
                "SELECT CAST(json_extract(payload,'$.id') AS INTEGER), "
                "json_extract(payload,'$.at') FROM collector_latest "
                "WHERE source_id=? AND kind='event' AND json_type(payload,'$.id')='integer' "
                "AND json_extract(payload,'$.charger_id')=? "
                "AND json_extract(payload,'$.kind')='ocpp' "
                "ORDER BY CAST(json_extract(payload,'$.id') AS INTEGER) DESC LIMIT 1",
                (source_id, charger_id)).fetchone()
            if row and type(row[0]) is int and isinstance(row[1], str):
                rows.append(row)
    if not rows:
        return None
    at = max(rows, key=lambda row: row[0])[1]
    try:
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return at if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None
