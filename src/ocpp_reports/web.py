"""Optional local-only, read-only Reports dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .warehouse_queries import snapshot
from .warehouse.transaction_detail import transaction_detail
from .warehouse.event_groups import event_groups_page, event_group_details
import sqlite3


def minute_utc(value: str | None) -> str:
    """Presentation only: minute precision, explicit UTC for aware timestamps."""
    if not value:
        return "—"
    if not isinstance(value, str):
        return "Fecha inválida"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Fecha inválida"
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M") + " (zona desconocida)"
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")



def dashboard_timezones(configured: tuple[str, ...]) -> tuple[str, ...]:
    """Only administrator-configured IANA zones are selectable."""
    choices = tuple(dict.fromkeys((*configured, "UTC")))
    for zone in choices:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid dashboard timezone: {zone}") from exc
    return choices


def display_date(value: str | None, zone: str) -> str:
    if not value:
        return "—"
    if not isinstance(value, str):
        return "Fecha inválida"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Fecha inválida"
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M") + " (zona desconocida)"
    return parsed.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M") + (" UTC" if zone == "UTC" else "")


def session_duration(start: str | None, stop: str | None) -> str:
    """Display a completed session's elapsed time from charger event dates."""
    if not start or not stop:
        return "—"
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(stop.replace("Z", "+00:00"))
        if first.tzinfo is None or last.tzinfo is None:
            return "—"
        elapsed = (last - first).total_seconds()
        if elapsed < 0:
            return "—"
        if 0 < elapsed < 1:
            return f"0:00:{elapsed:06.3f}"
        seconds = int(elapsed)
    except (ValueError, TypeError, OverflowError):
        return "—"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _filter_dashboard(data: dict, *, term: str, status: str,
                      location: str, date_from: str, date_to: str, zone: str) -> dict:
    """Filter presentation data only; preserve the raw snapshot API."""
    term, status, location = term.casefold().strip(), status.casefold().strip(), location.casefold().strip()
    for day in (date_from, date_to):
        if day:
            datetime.strptime(day, "%Y-%m-%d")
    if date_from and date_to and date_from > date_to:
        raise ValueError("Date range start must not exceed end")
    result = dict(data)
    for key in ("connectors", "transactions", "events"):
        matched = []
        for row in data[key]:
            state = row.get("status") if key == "connectors" else row.get("state") if key == "transactions" else row.get("action")
            terms = (row.get("source_id"), row.get("charger_id"), row.get("location"),
                     state, row.get("status") if key == "events" else None)
            if term and not any(term in str(v or "").casefold() for v in terms):
                continue
            if location and location not in str(row.get("location") or "").casefold():
                continue
            if status and status not in str(state or "").casefold():
                continue
            if date_from or date_to:
                def local_day(value: object) -> str | None:
                    if not isinstance(value, str):
                        return None
                    try:
                        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        return None
                    if parsed.tzinfo is None:
                        return None
                    return parsed.astimezone(ZoneInfo(zone)).date().isoformat()

                if key == "transactions":
                    start_day = local_day(row.get("started_at"))
                    stop_day = local_day(row.get("stopped_at"))
                    # Include a transaction that intersects the selected date range,
                    # not only one that happens to start inside it.
                    first, last = start_day or stop_day, stop_day or start_day
                    if not first or not last:
                        continue
                    if (date_from and last < date_from) or (date_to and first > date_to):
                        continue
                else:
                    observed = (row.get("status_observed_at") or row.get("observed_at")) if key == "connectors" else row.get("at")
                    day = local_day(observed)
                    if not day or (date_from and day < date_from) or (date_to and day > date_to):
                        continue
            matched.append(row)
        result[key] = matched
    if term:
        result["sources"] = [s for s in data["sources"]
                             if term in str(s.get("source_id") or "").casefold()]
    return result



def _filter_transaction_energy(transactions: list[dict], view: str) -> list[dict]:
    """Classify measured final energy; unknown isn't a failed or successful charge."""
    if view not in ("success", "failed", "all"):
        raise ValueError("Invalid transaction view")
    if view == "all":
        return transactions

    def energy(row: dict) -> float | None:
        value = row.get("energy_wh")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        from math import isfinite
        return float(value) if isfinite(value) and value >= 0 else None

    return [row for row in transactions
            if (value := energy(row)) is not None
            and (value > 0 if view == "success" else value == 0)]


# Ingestion cadence is normally 30 seconds; allow for forwarding/backoff jitter.
# This is source-pipeline freshness, not proof of charger heartbeat or a new status.
SOURCE_FRESHNESS_SECONDS = 180
CHARGER_ACTIVITY_SECONDS = 180


def _charger_activity_freshness(timestamp: object, now: datetime) -> str:
    """Charger-origin activity, distinct from ingestion and StatusNotification."""
    if not isinstance(timestamp, str):
        return "unknown"
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "unknown"
        age = (now - parsed.astimezone(timezone.utc)).total_seconds()
        return "fresh" if 0 <= age <= CHARGER_ACTIVITY_SECONDS else "old"
    except (ValueError, TypeError, OverflowError):
        return "unknown"



def _freshness(timestamp: object, now: datetime) -> str:
    """Evaluate a timestamp without treating missing/unzoned evidence as current."""
    if not isinstance(timestamp, str):
        return "unknown"
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "unknown"
        age = (now - parsed.astimezone(timezone.utc)).total_seconds()
        return "fresh" if 0 <= age <= SOURCE_FRESHNESS_SECONDS else "old"
    except ValueError:
        return "unknown"


def _source_freshness(source: dict, now: datetime) -> str:
    """Collector receipt and local ingestion are alternative pipeline observations."""
    states = [_freshness(source.get(key), now)
              for key in ("last_ingested_at", "last_replication_received_at")]
    if "fresh" in states:
        return "fresh"
    return "old" if "old" in states else "unknown"


def _connection_flag(value: object) -> bool | None:
    """Normalize SQLite-backed 0/1 flags without guessing unknown values."""
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _single_connector_discrepancy_badges(connectors: list[dict]) -> dict[tuple, str]:
    """Compact directional C0/C1 discrepancy labels for single-connector stations."""
    groups: dict[tuple, list[dict]] = {}
    for row in connectors:
        groups.setdefault((row.get("source_id"), row.get("charger_id")), []).append(row)
    labels: dict[tuple, str] = {}
    for (source, charger), rows in groups.items():
        physical = [row for row in rows if row.get("connector_id") not in (0, None)]
        station = [row for row in rows if row.get("connector_id") == 0]
        if len(physical) != 1 or len(station) != 1:
            continue
        c0 = (station[0].get("status") or "").lower()
        c1 = (physical[0].get("status") or "").lower()
        if c0 == "available" and c1 == "faulted":
            labels[source, charger, physical[0]["connector_id"]] = "Warning"
        elif c0 == "faulted" and c1 == "available":
            labels[source, charger, physical[0]["connector_id"]] = "Failed"
    return labels


def _single_connector_disagreements(connectors: list[dict]) -> set[tuple]:
    """C0 Available contradicts the only physical connector's Faulted state."""
    groups = {}
    for row in connectors:
        groups.setdefault((row.get("source_id"), row.get("charger_id")), []).append(row)
    result = set()
    for (source, charger), rows in groups.items():
        physical = [r for r in rows if r.get("connector_id") not in (0, None)]
        station = [r for r in rows if r.get("connector_id") == 0]
        if (len(physical) == len(station) == 1
                and (station[0].get("status") or "").lower() == "available"
                and (physical[0].get("status") or "").lower() == "faulted"):
            result.add((source, charger, physical[0]["connector_id"]))
    return result


def _operational_status_counts(connectors: list[dict]) -> dict[str, dict[str, int]]:
    """Count distinct stations and physical connectors, without counting C0 twice.

    A station belongs to each state present among its physical connectors;
    C0 is used only when a station has no individual connector rows.
    Stale observations may also retain a fault classification, as before.
    """
    groups: dict[tuple[str | None, str | None], list[dict]] = {}
    for row in connectors:
        key = (row.get("source_id"), row.get("charger_id"))
        groups.setdefault(key, []).append(row)
    counts = {name: {"stations": 0, "connectors": 0}
              for name in ("available", "charging", "faults", "uncertain")}
    warnings = _single_connector_disagreements(connectors)
    for (source, charger), rows in groups.items():
        physical = [row for row in rows if row.get("connector_id") not in (0, None)]
        selected = physical or [row for row in rows if row.get("connector_id") == 0]
        active_states: set[str] = set()
        for row in selected:
            state = (row.get("status") or "").lower()
            states: set[str] = set()
            if state in ("faulted", "unavailable"):
                states.add("faults")
            if row.get("stale") or not state:
                states.add("uncertain")
            elif state == "charging":
                states.add("charging")
            elif state == "available":
                states.add("available")
            elif state not in ("faulted", "unavailable"):
                states.add("uncertain")
            # A single connector with C0 Available / C1 Faulted remains a
            # reported fault, but also contributes to operational availability.
            if (source, charger, row.get("connector_id")) in warnings:
                states.add("available")
            active_states.update(states)
            if physical:
                for category in states:
                    counts[category]["connectors"] += 1
        if physical and (source, charger, physical[0].get("connector_id")) in warnings:
            active_states.discard("faults")
            active_states.add("available")
        for category in active_states:
            counts[category]["stations"] += 1
    return counts


def _event_group_key(event: dict) -> tuple | None:
    """Collapse only routine repeats; faults and transaction events stay explicit.

    The Reports export intentionally has less OCPP detail than the CSMS event
    CLI, so do not claim request/response pairing or merge unrelated evidence.
    """
    source, charger = event.get("source_id"), event.get("charger_id")
    if not source or not charger:
        return None
    action = event.get("action")
    if action == "Heartbeat" and event.get("kind") == "ocpp":
        return (source, charger, "Heartbeat")
    if action == "StatusNotification" and event.get("kind") == "status":
        state = event.get("status")
        if (state in ("Available", "Preparing", "Charging", "SuspendedEV",
                      "SuspendedEVSE", "Finishing", "Reserved")
                and not event.get("error_code") and not event.get("info")
                and event.get("connector_id") is not None):
            return (source, charger, "StatusNotification",
                    event.get("connector_id"), state)
    return None


def _group_events(events: list[dict]) -> list[dict]:
    """Collapse adjacent routine evidence only, retaining each original event."""
    groups: list[dict] = []
    for event in events:
        identity = _event_group_key(event)
        if identity is not None and groups and groups[-1]["identity"] == identity:
            groups[-1]["items"].append(event)
        else:
            groups.append({"identity": identity, "items": [event],
                           "source_id": event.get("source_id"),
                           "charger_id": event.get("charger_id"),
                           "location": event.get("location"),
                           "action": event.get("action"),
                           "status": event.get("status")})
    return groups


def project_dashboard_connectors(data: dict, *, now: datetime, connector_view: str = "combined",
                                 source_freshness: dict[str, str] | None = None) -> dict:
    """Shared read-only connector projection for HTML and versioned JSON clients."""
    if connector_view not in ("combined", "station", "individual"):
        raise ValueError("Invalid connector view")
    if source_freshness is None:
        source_freshness = {source["source_id"]: _source_freshness(source, now)
                            for source in data["sources"]}
    for connector in data["connectors"]:
        # StatusNotification is change-driven: age of the last status change
        # does not measure connectivity. Do not use the export timestamp as
        # proof of a new status or a heartbeat from this charger.
        connector["status_time_unknown"] = (
            _freshness(connector.get("status_observed_at"), now) == "unknown"
        )
        connector["source_freshness"] = source_freshness.get(
            connector.get("source_id"), "unknown"
        )
        # SQLite snapshots export 0/1 rather than JSON booleans. Normalize
        # presentation values for both badges and operational counts; the
        # raw /api/snapshot response remains untouched.
        connector["connected"] = _connection_flag(connector.get("connected"))
        # The observed connection flag comes from the source's last
        # CSMS status snapshot; it is only actionable with recent source data.
        connector["activity_freshness"] = _charger_activity_freshness(
            connector.get("last_activity_at"), now
        )
        # Charger-origin activity is distinct from the export timestamp.
        # Legacy snapshots retain the connection flag semantics.
        activity = connector["activity_freshness"]
        connector["connection_fresh"] = (
            connector["source_freshness"] == "fresh"
            and connector["connected"] is not None
            and activity != "old"
        )
        connector["stale"] = (
            connector["source_freshness"] != "fresh"
            or connector.get("connected") is not True
            or activity == "old"
            or not connector.get("status")
        )
    operational_counts = _operational_status_counts(data["connectors"])
    # Presentation-only selection. Operational cards count all scoped physical connectors,
    # and /api/snapshot preserves the complete source-scoped evidence.
    discrepancy_badges = _single_connector_discrepancy_badges(data["connectors"])
    connector_rows = [
        connector for connector in data["connectors"]
        if connector_view == "combined"
        or (connector_view == "station" and connector.get("connector_id") == 0)
        or (connector_view == "individual" and connector.get("connector_id") != 0)
    ]
    for connector in connector_rows:
        connector["status_discrepancy_badge"] = discrepancy_badges.get((
            connector.get("source_id"), connector.get("charger_id"),
            connector.get("connector_id")))

    return {"connectors": connector_rows, "status_counts": operational_counts}


class EventGroupSummary(BaseModel):
    source_id: str
    charger_id: str | None = None
    location: str | None = None
    action: str | None = None
    at: str | None = None
    event_count: int
    first_event_id: int


class EventGroupsResponse(BaseModel):
    groups: list[EventGroupSummary] = Field(default_factory=list)
    page: int
    has_next: bool
    query_duration_ms: float


def validate_dashboard_window(zone: str, timezones: tuple[str, ...],
                              date_from: str = "", date_to: str = "") -> None:
    """Shared validation for HTML and versioned JSON dashboard date filters."""
    if zone not in timezones:
        raise HTTPException(422, "Unsupported dashboard timezone")
    for day in (date_from, date_to):
        if day:
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError as exc:
                raise HTTPException(422, "Invalid date") from exc
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Date range start must not exceed end")


def create_app(database: str | Path, *, allowed_source_ids: tuple[str, ...] | None = None,
               title: str = "OCPP Reports", timezones: tuple[str, ...] = ("UTC",)) -> FastAPI:
    if allowed_source_ids is not None and (not allowed_source_ids or
            any(not isinstance(source, str) or not source.strip() for source in allowed_source_ids)):
        raise ValueError("Source allowlist must contain nonempty source IDs")
    zones = dashboard_timezones(timezones)
    app = FastAPI(title="OCPP Reports", docs_url=None, redoc_url=None, openapi_url=None)
    warehouse = Path(database)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["minute_utc"] = minute_utc
    templates.env.filters["display_date"] = display_date
    templates.env.filters["session_duration"] = session_duration

    def read(source_id: str | None, charger_id: str | None, limit: int, *, transaction_pagination: dict | None = None,
             skip_events: bool = False):
        if allowed_source_ids is not None and source_id and source_id not in allowed_source_ids:
            raise HTTPException(403, "Source is not available in this dashboard")
        try:
            options = {"transaction_pagination": transaction_pagination} if transaction_pagination is not None else {}
            if skip_events:
                options["skip_events"] = True
            return snapshot(warehouse, source_id=source_id, charger_id=charger_id, limit=limit,
                            allowed_source_ids=allowed_source_ids, **options)
        except FileNotFoundError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "Warehouse unavailable") from exc

    def scoped_event_groups(*, source_id: str | None, charger_id: str | None,
                            term: str, status: str, location: str,
                            date_from: str, date_to: str, zone: str,
                            page: int) -> dict:
        """Single SQL event-group query contract for HTML and JSON consumers."""
        if allowed_source_ids is not None and source_id is not None and source_id not in allowed_source_ids:
            raise HTTPException(403, "Source is not available in this dashboard")
        validate_dashboard_window(zone, zones, date_from, date_to)
        try:
            return event_groups_page(
                warehouse, source_id=source_id, charger_id=charger_id,
                allowed_source_ids=allowed_source_ids, term=term,
                status=status, location=location, date_from=date_from,
                date_to=date_to, zone=zone, page=page)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Event history unavailable") from exc

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, source_id: str | None = None,
              charger_id: str | None = None, filter: str = "",
              status: str = "", location: str = "",
              date_from: str = "", date_to: str = "", tz: str | None = None,
              tx_view: str = "success", tx_page: int = Query(1, ge=1),
              event_page: int = Query(1, ge=1), connector_view: str = "individual"):
        tz = tz or zones[0]
        validate_dashboard_window(tz, zones, date_from, date_to)
        if connector_view not in ("combined", "station", "individual"):
            raise HTTPException(422, "Invalid connector view")
        if tx_view not in ("success", "failed", "all"):
            raise HTTPException(422, "Invalid transaction view")
        # Filter and paginate transactions inside SQLite, before decoding JSON.
        sql_page = dict(term=filter, status=status, location=location,
                        date_from=date_from, date_to=date_to, zone=tz,
                        view=tx_view, page=tx_page)
        snapshot_started = perf_counter()
        data = read(source_id, charger_id, 100, transaction_pagination=sql_page, skip_events=True)
        snapshot_duration_ms = (perf_counter() - snapshot_started) * 1000
        # Preserve source freshness evidence before term filtering hides source rows.
        now = datetime.now(timezone.utc)
        source_freshness = {source["source_id"]: _source_freshness(source, now)
                            for source in data["sources"]}
        paginated = data.get("transaction_pagination")
        # Keep connector/event filters in the established presentation path.
        rows = data["transactions"] if paginated is not None else None
        if rows is not None:
            data = {**data, "transactions": []}
        try:
            data = _filter_dashboard(data, term=filter, status=status,
                                     location=location, date_from=date_from, date_to=date_to, zone=tz)
            if rows is not None:
                data["transactions"] = rows
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if paginated is not None:
            transaction_counts = paginated["counts"]
            tx_total, tx_pages, tx_page = paginated["total"], paginated["pages"], paginated["page"]
        else:
            # Backward-compatible path for external/injected snapshot providers.
            transaction_counts = {
                "success": len(_filter_transaction_energy(data["transactions"], "success")),
                "failed": len(_filter_transaction_energy(data["transactions"], "failed")),
                "all": len(data["transactions"]),
            }
            filtered_transactions = _filter_transaction_energy(data["transactions"], tx_view)
            tx_total = len(filtered_transactions)
            tx_pages = max(1, (tx_total + 19) // 20)
            tx_page = min(tx_page, tx_pages)
            start = (tx_page - 1) * 20
            data["transactions"] = filtered_transactions[start:start + 20]
        projected = project_dashboard_connectors(data, now=now, connector_view=connector_view,
                                                 source_freshness=source_freshness)
        operational_counts = projected["status_counts"]
        connector_rows = projected["connectors"]
        is_admin = not hasattr(request.state, "principal") or request.state.principal.role.value == "admin"
        if not is_admin:
            data["events"] = []
            data["sources"] = []
        # SQL window grouping reads only summaries; details are fetched lazily.
        group_duration_ms = None
        if paginated is not None and is_admin:
            started = perf_counter()
            grouped = scoped_event_groups(
                source_id=source_id, charger_id=charger_id, term=filter,
                status=status, location=location, date_from=date_from,
                date_to=date_to, zone=tz, page=event_page)
            group_duration_ms = (perf_counter() - started) * 1000
            event_groups = grouped["groups"]
            event_has_next = grouped["has_next"]
        else:
            event_groups = _group_events(data["events"])[:10]
            event_has_next = False
        template_started = perf_counter()
        response = templates.TemplateResponse(request, "dashboard.html.j2",
                                          {"data": data, "is_admin": is_admin,
                                           "login_enabled": getattr(request.state, "principal", None) is not None,
                                           "current_username": getattr(getattr(request.state, "principal", None), "user_id", ""),
                                           "connector_rows": connector_rows,
                                           "operational_counts": operational_counts,
                                           "connector_view": connector_view,
                                           "event_groups": event_groups,
                                           "event_page": event_page, "event_has_next": event_has_next,
                                           "title": title, "source_id": source_id or "",
                                           "charger_id": charger_id or "", "filter": filter,
                                           "status_filter": status, "location_filter": location,
                                           "date_from": date_from, "date_to": date_to,
                                           "timezone": tz, "timezones": zones, "tx_view": tx_view,
                                           "transaction_counts": transaction_counts,
                                           "tx_page": tx_page, "tx_pages": tx_pages,
                                           "tx_total": tx_total})
        template_duration_ms = (perf_counter() - template_started) * 1000
        timings = [f"snapshot;dur={snapshot_duration_ms:.1f}"]
        if group_duration_ms is not None:
            timings.append(f"event-groups;dur={group_duration_ms:.1f}")
        timings.append(f"template;dur={template_duration_ms:.1f}")
        response.headers["Server-Timing"] = ", ".join(timings)
        return response

    @app.get("/api/v1/event-groups", tags=["Dashboard API v1"], response_model=EventGroupsResponse)
    def api_event_groups(
        request: Request, source_id: str | None = None,
        charger_id: str | None = None, filter: str = "",
        status: str = "", location: str = "", date_from: str = "",
        date_to: str = "", tz: str | None = None,
        page: int = Query(1, ge=1),
    ):
        """Return the same SQL-paginated group summaries as the HTML panel."""
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value != "admin":
            raise HTTPException(403, "Event history requires administrator access")
        if allowed_source_ids is not None and source_id is not None and source_id not in allowed_source_ids:
            raise HTTPException(403, "Source is not available in this dashboard")
        zone = tz or zones[0]
        validate_dashboard_window(zone, zones, date_from, date_to)
        started = perf_counter()
        result = scoped_event_groups(
            source_id=source_id, charger_id=charger_id, term=filter,
            status=status, location=location, date_from=date_from,
            date_to=date_to, zone=zone, page=page)
        return {**result, "query_duration_ms": round(
            (perf_counter() - started) * 1000, 1)}

    @app.get("/api/v1/event-group", tags=["Dashboard API v1"])
    @app.get("/api/event-group")
    def api_event_group(
        request: Request, group_source: str, first_event_id: int = Query(..., ge=1),
        source_id: str | None = None, charger_id: str | None = None,
        filter: str = "", status: str = "", location: str = "",
        date_from: str = "", date_to: str = "", tz: str | None = None,
        offset: int = Query(0, ge=0),
    ):
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value != "admin":
            raise HTTPException(403, "Event history requires administrator access")
        if allowed_source_ids is not None and (
            group_source not in allowed_source_ids or
            (source_id is not None and source_id not in allowed_source_ids)
        ):
            raise HTTPException(403, "Source is not available in this dashboard")
        tz = tz or zones[0]
        if tz not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        try:
            started = perf_counter()
            result = event_group_details(
                warehouse, group_source=group_source, first_event_id=first_event_id,
                source_id=source_id, charger_id=charger_id,
                allowed_source_ids=allowed_source_ids, term=filter, status=status,
                location=location, date_from=date_from, date_to=date_to,
                zone=tz, offset=offset,
            )
            for event in result["events"]:
                event["display_at"] = display_date(event.get("at"), tz)
            # Include timing in the body only for the scoped detail response.
            # Timings are diagnostic, not cached historical evidence.
            result["query_duration_ms"] = round((perf_counter() - started) * 1000, 1)
            return result
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Event history unavailable") from exc

    def scoped_transaction_detail(source_id: str, charger_id: str, transaction_id: int) -> dict:
        """One source-scoped warehouse query shared by HTML and JSON detail."""
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            raise HTTPException(404, "Transaction not found")
        try:
            transaction = transaction_detail(
                warehouse, source_id=source_id, charger_id=charger_id,
                transaction_id=transaction_id, allowed_source_ids=allowed_source_ids)
        except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(503, "Warehouse unavailable") from exc
        if transaction is None:
            raise HTTPException(404, "Transaction not found")
        return transaction

    @app.get("/api/v1/transactions/{source_id}/{charger_id}/{transaction_id}",
             tags=["Dashboard API v1"])
    def api_transaction_detail(source_id: str, charger_id: str, transaction_id: int,
                               tz: str | None = None):
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        transaction = scoped_transaction_detail(source_id, charger_id, transaction_id)
        return {"transaction": transaction, "timezone": selected_zone}

    @app.get("/transactions/{source_id}/{charger_id}/{transaction_id}", response_class=HTMLResponse)
    def transaction_page(request: Request, source_id: str, charger_id: str,
                         transaction_id: int, tz: str | None = None):
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        transaction = scoped_transaction_detail(source_id, charger_id, transaction_id)
        active = (bool(transaction.get("active"))
                  and transaction.get("state") in ("open", "active", "charging")
                  and transaction.get("energy_wh") is None)
        return templates.TemplateResponse(
            request, "transaction_detail.html.j2",
            {"title": title, "transaction": transaction, "timezone": selected_zone,
             "timezones": zones, "refresh_active": active},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/snapshot")
    def api_snapshot(request: Request, source_id: str | None = None, charger_id: str | None = None,
                     limit: int = Query(100, ge=1, le=500)):
        data = read(source_id, charger_id, limit)
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value != "admin":
            data = {**data, "events": [], "sources": []}
        return data

    from .api_v1 import register_api_v1
    register_api_v1(app, read, timezones=zones)

    # Opt-in: existing nginx Basic Auth remains unchanged.
    import os
    auth_path=os.environ.get("OCPP_REPORTS_AUTH_DATABASE")
    auth_tenant=os.environ.get("OCPP_REPORTS_AUTH_TENANT")
    if auth_path or auth_tenant:
        if not auth_path or not auth_tenant:
            raise ValueError("Both authentication settings are required")
        from .dashboard_login import attach_login
        attach_login(app,path=auth_path,tenant=auth_tenant)
    return app
