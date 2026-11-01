"""Optional local-only, read-only Reports dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .warehouse_queries import snapshot


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

    def read(source_id: str | None, charger_id: str | None, limit: int):
        if allowed_source_ids is not None and source_id and source_id not in allowed_source_ids:
            raise HTTPException(403, "Source is not available in this dashboard")
        try:
            return snapshot(warehouse, source_id=source_id, charger_id=charger_id, limit=limit,
                            allowed_source_ids=allowed_source_ids)
        except FileNotFoundError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "Warehouse unavailable") from exc

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, source_id: str | None = None,
              charger_id: str | None = None, filter: str = "",
              status: str = "", location: str = "",
              date_from: str = "", date_to: str = "", tz: str | None = None,
              tx_view: str = "success", tx_page: int = Query(1, ge=1)):
        tz = tz or zones[0]
        if tz not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        # Bound the dashboard to the latest 500 entries; render only one
        # 20-row page, retaining existing server-side presentation filters.
        data = read(source_id, charger_id, 500)
        try:
            data = _filter_dashboard(data, term=filter, status=status,
                                     location=location, date_from=date_from, date_to=date_to, zone=tz)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            transaction_counts = {
                "success": len(_filter_transaction_energy(data["transactions"], "success")),
                "failed": len(_filter_transaction_energy(data["transactions"], "failed")),
                "all": len(data["transactions"]),
            }
            filtered_transactions = _filter_transaction_energy(data["transactions"], tx_view)
            tx_page_size = 20
            tx_total = len(filtered_transactions)
            tx_pages = max(1, (tx_total + tx_page_size - 1) // tx_page_size)
            tx_page = min(tx_page, tx_pages)
            start = (tx_page - 1) * tx_page_size
            data["transactions"] = filtered_transactions[start:start + tx_page_size]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        now = datetime.now(timezone.utc)
        for connector in data["connectors"]:
            try:
                observed = datetime.fromisoformat(connector["observed_at"].replace("Z", "+00:00"))
                connector["stale"] = (now - observed).total_seconds() > 30
            except (ValueError, AttributeError, TypeError):
                connector["stale"] = True
        return templates.TemplateResponse(request, "dashboard.html.j2",
                                          {"data": data, "event_groups": _group_events(data["events"]),
                                           "title": title, "source_id": source_id or "",
                                           "charger_id": charger_id or "", "filter": filter,
                                           "status_filter": status, "location_filter": location,
                                           "date_from": date_from, "date_to": date_to,
                                           "timezone": tz, "timezones": zones, "tx_view": tx_view,
                                           "transaction_counts": transaction_counts,
                                           "tx_page": tx_page, "tx_pages": tx_pages,
                                           "tx_total": tx_total})

    @app.get("/api/snapshot")
    def api_snapshot(source_id: str | None = None, charger_id: str | None = None,
                     limit: int = Query(100, ge=1, le=500)):
        return read(source_id, charger_id, limit)

    return app
