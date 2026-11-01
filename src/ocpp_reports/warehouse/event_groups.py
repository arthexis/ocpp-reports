"""Read-only SQLite window selection of consecutive dashboard event groups.

No materialized index or changes to authoritative evidence. Groups follow the
existing collapse predicate and order (source, descending event ID). The
window only selects group summaries; full payloads are retrieved on demand.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


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
    # A null grouping key makes every row a singleton. For routine status
    # events include connector and state, and never collapse fault evidence.
    grouping_key = """
    CASE WHEN e.source_id IS NOT NULL
           AND COALESCE(json_extract(e.payload,'$.charger_id'),'') != ''
           AND json_extract(e.payload,'$.action')='Heartbeat'
           AND json_extract(e.payload,'$.kind')='ocpp'
         THEN json_array(e.source_id,json_extract(e.payload,'$.charger_id'),'Heartbeat')
         WHEN e.source_id IS NOT NULL
           AND COALESCE(json_extract(e.payload,'$.charger_id'),'') != ''
           AND json_extract(e.payload,'$.action')='StatusNotification'
           AND json_extract(e.payload,'$.kind')='status'
           AND json_extract(e.payload,'$.status') IN
             ('Available','Preparing','Charging','SuspendedEV','SuspendedEVSE','Finishing','Reserved')
           AND COALESCE(json_extract(e.payload,'$.error_code'),'')=''
           AND COALESCE(json_extract(e.payload,'$.info'),'')=''
           AND json_extract(e.payload,'$.connector_id') IS NOT NULL
         THEN json_array(e.source_id,json_extract(e.payload,'$.charger_id'),'StatusNotification',
                         json_extract(e.payload,'$.connector_id'),json_extract(e.payload,'$.status'))
         ELSE NULL END
    """
    cte = (
        "WITH raw AS (" + joined + "),"
        " eligible AS (SELECT e.source_id,e.event_id,e.payload," + loc + " AS location,"
        " json_extract(e.payload,'$.charger_id') AS charger_id,"
        " json_extract(e.payload,'$.action') AS action,"
        " json_extract(e.payload,'$.at') AS at," + grouping_key + " AS grouping_key"
        " FROM raw e" + loc_join + where + "),"
        " compared AS (SELECT *,LAG(grouping_key) OVER "
        " (ORDER BY source_id DESC,event_id DESC) AS previous_key FROM eligible),"
        " numbered AS (SELECT *,SUM(CASE WHEN grouping_key IS NOT NULL "
        " AND grouping_key=previous_key THEN 0 ELSE 1 END)"
        " OVER (ORDER BY source_id DESC,event_id DESC ROWS UNBOUNDED PRECEDING)"
        " AS group_number FROM compared) "
    )
    return cte, [*parameters, *filter_args]



def _default_first_page(db: sqlite3.Connection, tables: set[str],
                        source_id: str | None,
                        allowed_source_ids: tuple[str, ...] | None,
                        page_size: int) -> dict:
    """Stream exactly enough ordered evidence to finish the first ten groups.

    Unlike the window query, this doesn't decode and rank all historic event
    payloads on a normal unfiltered first-page dashboard request.
    """
    conditions, args = [], []
    if allowed_source_ids is not None:
        conditions.append("source_id IN (" + ",".join("?" for _ in allowed_source_ids) + ")")
        args.extend(allowed_source_ids)
    if source_id:
        conditions.append("source_id=?")
        args.append(source_id)
    scope = " AND ".join(conditions) if conditions else "1=1"
    branches, parameters = [], []
    if "events" in tables:
        branches.append("SELECT source_id,event_id,payload FROM events WHERE " + scope)
        parameters.extend(args)
    if "collector_latest" in tables:
        branches.append(
            "SELECT r.source_id, CAST(json_extract(r.payload,'$.id') AS INTEGER) AS event_id,"
            " r.payload FROM collector_latest r WHERE r.kind='event' AND " + scope
            + (" AND NOT EXISTS (SELECT 1 FROM events l WHERE l.source_id=r.source_id"
               " AND l.event_id=CAST(json_extract(r.payload,'$.id') AS INTEGER))"
               if "events" in tables else "")
        )
        parameters.extend(args)
    if not branches:
        return {"groups": [], "page": 1, "has_next": False}
    locations = {}
    if "collector_charger_locations" in tables:
        locations = {(r[0], r[1]): r[2] for r in db.execute(
            "SELECT source_id,charger_id,location FROM collector_charger_locations")}
    sql = ("SELECT source_id,event_id,payload FROM (" + " UNION ALL ".join(branches)
           + ") ORDER BY source_id DESC,event_id DESC")
    groups: list[dict] = []
    previous_key = None
    for source, event_id, serialized in db.execute(sql, parameters):
        payload = json.loads(serialized)
        charger = payload.get("charger_id")
        action = payload.get("action")
        kind = payload.get("kind")
        status = payload.get("status")
        key = None
        if source is not None and charger:
            if action == "Heartbeat" and kind == "ocpp":
                key = (source, charger, "Heartbeat")
            elif (action == "StatusNotification" and kind == "status"
                  and status in ("Available", "Preparing", "Charging",
                                 "SuspendedEV", "SuspendedEVSE", "Finishing", "Reserved")
                  and not payload.get("error_code") and not payload.get("info")
                  and payload.get("connector_id") is not None):
                key = (source, charger, "StatusNotification",
                       payload["connector_id"], status)
        if not groups or key is None or key != previous_key:
            if len(groups) == page_size:
                return {"groups": groups, "page": 1, "has_next": True}
            groups.append({
                "source_id": source, "charger_id": charger,
                "location": locations.get((source, charger), source),
                "action": action, "at": payload.get("at"),
                "event_count": 0, "first_event_id": event_id,
            })
        groups[-1]["event_count"] += 1
        previous_key = key
    return {"groups": groups, "page": 1, "has_next": False}



def event_groups_page(
    database: str | Path, *, source_id: str | None = None, charger_id: str | None = None,
    allowed_source_ids: tuple[str, ...] | None = None, term: str = "", status: str = "",
    location: str = "", date_from: str = "", date_to: str = "", zone: str = "UTC",
    page: int = 1, page_size: int = 10,
) -> dict:
    if page < 1 or page_size != 10:
        raise ValueError("Invalid event group page")
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
        if (page == 1 and not any((charger_id, term, status, location,
                                   date_from, date_to))):
            return _default_first_page(db, {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }, source_id, allowed_source_ids, page_size)
        # Rank group numbers instead of materializing the event JSON for each
        # group; the newest row supplies the display information.
        sql = cte + (
            ", summaries AS (SELECT group_number,COUNT(*) AS event_count,"
            " MAX(event_id) AS newest_id FROM numbered GROUP BY group_number)"
            " SELECT n.source_id,n.charger_id,n.location,n.action,n.at,"
            " s.event_count,s.newest_id AS first_event_id"
            " FROM summaries s JOIN numbered n ON n.group_number=s.group_number"
            " AND n.event_id=s.newest_id"
            " ORDER BY s.group_number ASC LIMIT ? OFFSET ?"
        )
        rows = [dict(r) for r in db.execute(sql, [*args, page_size + 1, (page - 1) * page_size])]
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
            ", selected AS (SELECT group_number FROM numbered WHERE source_id=? AND event_id=?),"
            " first_member AS (SELECT n.group_number FROM numbered n"
            " JOIN selected s USING(group_number) GROUP BY n.group_number"
            " HAVING MAX(n.event_id)=?)"
            " SELECT n.source_id,n.payload,n.location FROM numbered n"
            " JOIN first_member f USING(group_number)"
            " ORDER BY n.event_id DESC LIMIT ? OFFSET ?"
        )
        rows = db.execute(sql, [*args, group_source, first_event_id, first_event_id,
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
        })
    return {"events": events, "has_next": len(rows) > limit}
