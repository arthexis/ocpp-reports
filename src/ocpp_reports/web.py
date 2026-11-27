"""Optional local-only, read-only Reports dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates

from .warehouse_queries import snapshot
from .warehouse.transaction_detail import transaction_detail
from .warehouse.transaction_timeline import transaction_timeline, timeline_charts
from .warehouse.active_sessions import is_active_session
from .warehouse.event_groups import (event_groups_page, event_group_details,
                                     latest_heartbeat_at, latest_charger_receipt_at)
from .warehouse.charger_energy import charger_energy_30_days
from .warehouse.config_history import list_snapshots, one_snapshot, render_snapshot_csv
from .warehouse.transaction_pagination import latest_completed_charges, connector_charge_counts, transaction_page as query_transaction_page
from .warehouse.charger_csv import CSV_PROFILES, charger_transactions, render_charger_csv
from .dashboard_login import dashboard_account_directory, COOKIE
from .dashboard_account_management import (AccountError, csrf_token, list_tenant_accounts,
    create_account, update_account, delete_account)
from .dashboard_auth import Permission, authorized
from .site_metadata import (site_for_charger, editable_sites, editable_customers, save_site,
                            assign_site, hours_fields, DAYS, DAY_NAMES, hours_display)
import sqlite3
import json
import re
import hmac


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


def display_date(value: str | None, zone: str, *, seconds: bool = False) -> str:
    date_format = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    if not value:
        return "—"
    if not isinstance(value, str):
        return "Fecha inválida"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Fecha inválida"
    if parsed.tzinfo is None:
        return parsed.strftime(date_format) + " (zona desconocida)"
    return parsed.astimezone(ZoneInfo(zone)).strftime(date_format) + (" UTC" if zone == "UTC" else "")


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
    if view not in ("success", "failed", "all", "active"):
        raise ValueError("Invalid transaction view")
    if view == "all":
        return transactions
    if view == "active":
        return [row for row in transactions if is_active_session(row)]

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
        connector.setdefault("active_transaction_id", None)
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


def create_app(database: str | Path, *, allowed_source_ids: tuple[str, ...] | None = None,
               title: str = "OCPP Reports", timezones: tuple[str, ...] = ("UTC",),
               brand_image_url: str | None = None, csv_profile: str = "vendor") -> FastAPI:
    if csv_profile not in CSV_PROFILES:
        raise ValueError("Unsupported charger CSV profile")
    if brand_image_url is not None:
        parsed = urlsplit(brand_image_url)
        if not (brand_image_url.startswith("/assets/") and not brand_image_url.startswith("//")
                and not parsed.query and not parsed.fragment and not parsed.netloc
                and parsed.path == brand_image_url) and not (
                parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username
                and not parsed.password):
            raise ValueError("Brand image must be a local /assets/ path or an HTTPS URL")
    if allowed_source_ids is not None and (not allowed_source_ids or
            any(not isinstance(source, str) or not source.strip() for source in allowed_source_ids)):
        raise ValueError("Source allowlist must contain nonempty source IDs")
    zones = dashboard_timezones(timezones)
    app = FastAPI(title="OCPP Reports", docs_url=None, redoc_url=None, openapi_url=None)
    warehouse = Path(database)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    def _can_view_configuration(request: Request) -> bool:
        """Require an authenticated administrator in this dashboard tenant.

        Legacy Basic Auth does not assign application roles, so it cannot
        authorize access to configuration history.
        """
        return authorized(
            getattr(request.state, "principal", None),
            Permission.CONFIGURATION_VIEW,
            tenant_id=getattr(request.app.state, "dashboard_login_tenant", ""),
        )

    def _can_view_site(request: Request) -> bool:
        """Integrated operators and admins may read scoped site contact data.

        Never grant access to legacy Basic Auth (principal=None), basic users,
        or an unscoped non-administration dashboard.
        """
        principal = getattr(request.state, "principal", None)
        tenant = getattr(request.app.state, "dashboard_login_tenant", "")
        return (authorized(principal, Permission.CONNECTORS_VIEW, tenant_id=tenant)
                and (allowed_source_ids is not None or tenant == "administration"))

    def _can_edit_site(request: Request) -> bool:
        """Only integrated administrators can modify site contact data."""
        principal = getattr(request.state, "principal", None)
        tenant = getattr(request.app.state, "dashboard_login_tenant", "")
        return (authorized(principal, Permission.USERS_MANAGE, tenant_id=tenant)
                and (allowed_source_ids is not None or tenant == "administration"))

    def _site_scope(request: Request, source_id: str):
        if not _can_edit_site(request):
            raise HTTPException(403, "Site administration requires an authenticated administrator")
        if not source_id.strip() or (allowed_source_ids is not None
                                      and source_id not in allowed_source_ids):
            raise HTTPException(404, "Charger not found")

    def _site_write_request(request: Request):
        if request.url.scheme != "https":
            raise HTTPException(403, "HTTPS required")
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Invalid origin")
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Unsupported content type")

    @app.get("/assets/brand.png", include_in_schema=False)
    def dashboard_brand_asset():
        return FileResponse(Path(__file__).parent / "assets" / "brand.png", media_type="image/png")

    @app.get("/assets/brand-light.png", include_in_schema=False)
    def dashboard_brand_light_asset():
        return FileResponse(Path(__file__).parent / "assets" / "brand-light.png", media_type="image/png")

    # Shared across dashboard instances; never sourced from tenant branding configuration.
    _favicon_assets = {
        "/favicon.ico": ("favicon.ico", "image/x-icon"),
        **{f"/assets/favicon-{size}.png": (f"favicon-{size}.png", "image/png")
           for size in (16, 32, 180, 192, 512)},
    }
    def _favicon_endpoint(name: str, mime: str):
        def favicon_asset():
            return FileResponse(Path(__file__).parent / "assets" / name,
                                media_type=mime)
        return favicon_asset

    for asset_path, (asset_name, media_type) in _favicon_assets.items():
        app.add_api_route(asset_path, _favicon_endpoint(asset_name, media_type),
                          methods=["GET"], include_in_schema=False)

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

    def _account_scope(request: Request) -> tuple[str, str, str]:
        """Only current integrated tenant admins may manage login accounts."""
        principal = getattr(request.state, "principal", None)
        tenant = getattr(request.app.state, "dashboard_login_tenant", "")
        database = getattr(request.app.state, "dashboard_login_db", None)
        if not database or not authorized(principal, Permission.USERS_MANAGE, tenant_id=tenant):
            raise HTTPException(403, "Account management requires an authenticated administrator")
        return str(database), tenant, principal.user_id

    def _accounts_page(request: Request, *, notice: str = "", error: str = "",
                       status_code: int = 200, selected_user: str = ""):
        database, tenant, actor = _account_scope(request)
        accounts = list_tenant_accounts(database, tenant)
        selected_account = next((account for account in accounts if account["user_id"] == selected_user), None)
        messages = {"created": "Cuenta creada.", "updated": "Cuenta actualizada; sus sesiones anteriores fueron revocadas.",
                    "deleted": "Cuenta eliminada; sus sesiones anteriores fueron revocadas."}
        response = templates.TemplateResponse(
            request, "dashboard_accounts.html.j2",
            {"title": title, "brand_image_url": brand_image_url, "tenant": tenant, "actor": actor,
             "accounts": accounts, "selected_account": selected_account,
             "csrf_token": csrf_token(request.cookies.get(COOKIE, ""), tenant),
             "notice": messages.get(notice, ""), "error": error},
            status_code=status_code,
            headers={"Cache-Control": "no-store",
                     "Referrer-Policy": "no-referrer",
                     "X-Content-Type-Options": "nosniff"},
        )
        return response

    async def _account_form(request: Request) -> tuple[dict[str, str], str, str, str]:
        database, tenant, actor = _account_scope(request)
        if request.url.scheme != "https" or request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Invalid origin or transport")
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Unsupported content type")
        body = await request.body()
        if len(body) > 8192:
            raise HTTPException(413, "Form too large")
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=12)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, "Invalid form") from exc
        if any(len(parts) != 1 for parts in values.values()):
            raise HTTPException(422, "Duplicate form field")
        fields = {key: parts[0] for key, parts in values.items()}
        expected = csrf_token(request.cookies.get(COOKIE, ""), tenant)
        submitted = fields.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, submitted):
            raise HTTPException(403, "Invalid CSRF token")
        return fields, database, tenant, actor

    def _password_fields(fields: dict[str, str], *, required: bool) -> str | None:
        password = fields.get("password", "")
        confirmation = fields.get("confirm_password", "")
        if not password and not confirmation and not required:
            return None
        if password != confirmation:
            raise AccountError(422, "Las contraseñas no coinciden.")
        if not password:
            raise AccountError(422, "Debes establecer una contraseña.")
        return password

    @app.get("/accounts", response_class=HTMLResponse, include_in_schema=False)
    def accounts_page(request: Request, notice: str = "", user: str = ""):
        return _accounts_page(request, notice=notice, selected_user=user)

    @app.post("/accounts/create", include_in_schema=False)
    async def accounts_create(request: Request):
        fields, database, tenant, actor = await _account_form(request)
        try:
            password = _password_fields(fields, required=True)
            create_account(database, actor=actor, tenant=tenant,
                           username=fields.get("username", ""), role=fields.get("role", ""),
                           password=password)
        except AccountError as exc:
            return _accounts_page(request, error=str(exc), status_code=exc.status_code)
        return RedirectResponse("/accounts?notice=created", status_code=303,
                                headers={"Cache-Control": "no-store"})

    @app.post("/accounts/update", include_in_schema=False)
    async def accounts_update(request: Request):
        fields, database, tenant, actor = await _account_form(request)
        try:
            password = _password_fields(fields, required=False)
            if fields.get("active") not in ("0", "1"):
                raise AccountError(422, "Selecciona el estado de la cuenta.")
            update_account(database, actor=actor, tenant=tenant,
                           username=fields.get("username", ""),
                           role=fields.get("role", ""), active=fields["active"] == "1",
                           password=password)
        except AccountError as exc:
            return _accounts_page(request, error=str(exc), status_code=exc.status_code,
                                  selected_user=fields.get("username", ""))
        return RedirectResponse("/accounts?" + urlencode({"notice": "updated", "user": fields["username"]}), status_code=303,
                                headers={"Cache-Control": "no-store"})

    @app.post("/accounts/delete", include_in_schema=False)
    async def accounts_delete(request: Request):
        fields, database, tenant, actor = await _account_form(request)
        try:
            if fields.get("confirm") != "ELIMINAR":
                raise AccountError(422, "Confirma explícitamente la eliminación.")
            delete_account(database, actor=actor, tenant=tenant,
                           username=fields.get("username", ""))
        except AccountError as exc:
            return _accounts_page(request, error=str(exc), status_code=exc.status_code,
                                  selected_user=fields.get("username", ""))
        return RedirectResponse("/accounts?notice=deleted", status_code=303,
                                headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, source_id: str | None = None,
              charger_id: str | None = None, filter: str = "",
              status: str = "", location: str = "",
              date_from: str = "", date_to: str = "", tz: str | None = None,
              tx_view: str = "success", tx_page: int = Query(1, ge=1),
              event_page: int = Query(1, ge=1), connector_view: str = "individual"):
        tz = tz or zones[0]
        if tz not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        if connector_view not in ("combined", "station", "individual"):
            raise HTTPException(422, "Invalid connector view")
        if tx_view not in ("success", "failed", "all", "active"):
            raise HTTPException(422, "Invalid transaction view")
        for day in (date_from, date_to):
            if day:
                try:
                    datetime.strptime(day, "%Y-%m-%d")
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "Date range start must not exceed end")
        # Filter and paginate transactions inside SQLite, before decoding JSON.
        sql_page = dict(term=filter, status=status, location=location,
                        date_from=date_from, date_to=date_to, zone=tz,
                        view=tx_view, page=tx_page, page_size=10, card_summary=True)
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
                "active": len(_filter_transaction_energy(data["transactions"], "active")),
            }
            filtered_transactions = _filter_transaction_energy(data["transactions"], tx_view)
            tx_total = len(filtered_transactions)
            tx_pages = max(1, (tx_total + 9) // 10)
            tx_page = min(tx_page, tx_pages)
            start = (tx_page - 1) * 10
            data["transactions"] = filtered_transactions[start:start + 10]
        projected = project_dashboard_connectors(data, now=now, connector_view=connector_view,
                                                 source_freshness=source_freshness)
        # Operational 'Cargando' is a source-scoped summary, independent of
        # presentation search/status/date filters. Clear those filters when
        # navigating to all open sessions while preserving site and timezone.
        active_query = [("tx_view", "active"), ("tz", tz)]
        if source_id:
            active_query.append(("source_id", source_id))
        if charger_id:
            active_query.append(("charger_id", charger_id))
        active_transactions_href = "/?" + urlencode(active_query) + "#transactions"
        operational_counts = projected["status_counts"]
        connector_rows = projected["connectors"]
        # The displayed page of sessions is not enough to establish the last
        # completed charge for each connector. Read only closed projections.
        latest_charge_at = (
            latest_completed_charges(warehouse, connector_rows, allowed_source_ids=allowed_source_ids)
            if warehouse.is_file() and connector_rows else {}
        )
        charge_counts = (
            connector_charge_counts(warehouse, connector_rows, allowed_source_ids=allowed_source_ids)
            if warehouse.is_file() and connector_rows else {}
        )
        for connector in connector_rows:
            key = (connector.get("source_id"), connector.get("charger_id"),
                   connector.get("connector_id"))
            connector["last_charge_at"] = latest_charge_at.get(key)
            connector["charge_count"] = charge_counts.get(key, 0)
        is_admin = not hasattr(request.state, "principal") or request.state.principal.role.value == "admin"
        if not is_admin:
            data["events"] = []
            data["sources"] = []
        principal = getattr(request.state, "principal", None)
        account_db = getattr(request.app.state, "dashboard_login_db", None)
        account_tenant = getattr(request.app.state, "dashboard_login_tenant", None)
        dashboard_accounts = (
            dashboard_account_directory(account_db, tenant=account_tenant)
            if principal is not None and is_admin and account_db and account_tenant
            else []
        )
        is_central_admin = bool(principal is not None and is_admin
                                and account_tenant == "administration")
        # Event history belongs to the individual charger page, not dashboard.
        event_groups, event_has_next = [], False
        group_duration_ms = None
        template_started = perf_counter()
        response = templates.TemplateResponse(request, "dashboard.html.j2",
                                          {"data": data, "is_admin": is_admin,
                                           "dashboard_accounts": dashboard_accounts,
                                           "is_central_admin": is_central_admin,
                                           "login_enabled": getattr(request.state, "principal", None) is not None,
                                           "current_username": getattr(getattr(request.state, "principal", None), "user_id", ""),
                                           "connector_rows": connector_rows,
                                           "operational_counts": operational_counts,
                                           "connector_view": connector_view,
                                           "event_groups": event_groups,
                                           "event_page": event_page, "event_has_next": event_has_next,
                                           "title": title, "brand_image_url": brand_image_url, "source_id": source_id or "",
                                           "charger_id": charger_id or "", "filter": filter,
                                           "status_filter": status, "location_filter": location,
                                           "date_from": date_from, "date_to": date_to,
                                           "timezone": tz, "timezones": zones, "tx_view": tx_view,
                                           "active_transactions_href": active_transactions_href,
                                           "transaction_counts": transaction_counts,
                                           "tx_page": tx_page, "tx_pages": tx_pages,
                                           "tx_total": tx_total, "rfid_cards": (paginated or {}).get("cards", [])})
        template_duration_ms = (perf_counter() - template_started) * 1000
        timings = [f"snapshot;dur={snapshot_duration_ms:.1f}"]
        if group_duration_ms is not None:
            timings.append(f"event-groups;dur={group_duration_ms:.1f}")
        timings.append(f"template;dur={template_duration_ms:.1f}")
        response.headers["Server-Timing"] = ", ".join(timings)
        return response

    def _device_page(request: Request, source_id: str, charger_id: str,
                     connector_id: int | None, tz: str | None, tx_page: int,
                     event_page: int):
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        if tx_page < 1 or event_page < 1 or (connector_id is not None and connector_id < 0):
            raise HTTPException(422, "Invalid page or connector")
        # Exact source+charger identity, not a display name or location. A
        # forbidden source and an unknown device share the same 404 response.
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            raise HTTPException(404, "Device not found")
        sql_page = dict(term="", status="", location="", date_from="",
                        date_to="", zone=selected_zone, view="all", page=tx_page,
                        connector_id=connector_id)
        data = read(source_id, charger_id, 100, transaction_pagination=sql_page,
                    skip_events=True)
        now = datetime.now(timezone.utc)
        source = next((s for s in data["sources"] if s["source_id"] == source_id), None)
        freshness = _source_freshness(source, now) if source else "unknown"
        projected = project_dashboard_connectors(
            data, now=now, connector_view="combined",
            source_freshness={source_id: freshness})
        connectors = [
            row for row in projected["connectors"]
            if row.get("source_id") == source_id and row.get("charger_id") == charger_id
            and (connector_id is None or row.get("connector_id") == connector_id)
        ]
        page = data.get("transaction_pagination")
        if page is None:
            # Preserve read-only compatibility for injected snapshot providers,
            # never leaking another source/charger or connector.
            matched = [t for t in data["transactions"]
                       if t.get("source_id") == source_id
                       and t.get("charger_id") == charger_id
                       and (connector_id is None or
                            type(t.get("connector_id")) is int and
                            t.get("connector_id") == connector_id)]
            total = len(matched)
            pages = max(1, (total + 19) // 20)
            actual_page = min(tx_page, pages)
            data["transactions"] = matched[(actual_page - 1) * 20:actual_page * 20]
        else:
            total, pages, actual_page = page["total"], page["pages"], page["page"]
        principal = getattr(request.state, "principal", None)
        is_admin = principal is None or principal.role.value == "admin"
        event_groups, event_has_next = [], False
        if connector_id is None:
            try:
                events = event_groups_page(
                    warehouse, source_id=source_id, charger_id=charger_id,
                    allowed_source_ids=allowed_source_ids, zone=selected_zone,
                    page=event_page)
            except (ValueError, OSError, sqlite3.Error) as exc:
                raise HTTPException(503, "Event history unavailable") from exc
            event_groups, event_has_next = events["groups"], events["has_next"]
        if not connectors and not total and not event_groups:
            raise HTTPException(404, "Device not found")
        heartbeat_at = latest_heartbeat_at(
            warehouse, source_id=source_id, charger_id=charger_id,
            allowed_source_ids=allowed_source_ids)
        receipt_at = latest_charger_receipt_at(
            warehouse, source_id=source_id, charger_id=charger_id,
            allowed_source_ids=allowed_source_ids)
        energy_30_days = (
            charger_energy_30_days(
                warehouse, source_id=source_id, charger_id=charger_id,
                allowed_source_ids=allowed_source_ids, zone=selected_zone, now=now,
                physical_connector_ids=[row.get("connector_id") for row in connectors])
            if connector_id is None else None
        )
        physical_connector_count = sum(
            type(row.get("connector_id")) is int and row["connector_id"] > 0
            for row in connectors)
        has_config = False
        if connector_id is None and _can_view_configuration(request):
            has_config = bool(list_snapshots(
                warehouse, source_id, charger_id, limit=1)[0])
        location = (next((row.get("location") for row in connectors if row.get("location")), None)
                    or next((row.get("location") for row in data["transactions"]
                             if row.get("location")), None) or source_id)
        site_visible = connector_id is None and _can_view_site(request)
        site_admin = site_visible and _can_edit_site(request)
        site_meta = (site_for_charger(warehouse, source_id, charger_id)
                     if site_visible else None)
        if site_visible and site_meta is None:
            # Unmigrated locations retain their display label and default contact hours.
            site_meta = {"location_name": location if location != source_id else None,
                         "hours_display": hours_display(None)}
        return templates.TemplateResponse(
            request, "device_detail.html.j2",
            {"title": title, "brand_image_url": brand_image_url, "source_id": source_id, "charger_id": charger_id,
             "selected_connector_id": connector_id, "connectors": connectors,
             "data": data, "location": location, "source": source,
             "source_freshness": freshness, "heartbeat_at": heartbeat_at,
             "receipt_at": receipt_at, "energy_30_days": energy_30_days,
              "physical_connector_count": physical_connector_count,
              "timezone": selected_zone,
             "timezones": zones, "tx_total": total, "tx_page": actual_page,
             "tx_pages": pages, "event_groups": event_groups,
             "event_page": event_page, "event_has_next": event_has_next,
             "is_admin": is_admin, "has_config": has_config,
             "site_visible": site_visible, "site_admin": site_admin, "site_meta": site_meta},
            headers={"Cache-Control": "no-store"})

    @app.get("/chargers/{source_id}/{charger_id}/report.csv")
    def charger_csv_download(source_id: str, charger_id: str, tz: str | None = None):
        # Route-derived, immutable dashboard scope; the caller cannot select
        # another CSV profile or bypass an instance's source allowlist.
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            raise HTTPException(404, "Device not found")
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        try:
            found = charger_transactions(warehouse, source_id=source_id,
                                         charger_id=charger_id)
            if found is None:
                raise HTTPException(404, "Device not found")
            transactions, location = found
            content = render_charger_csv(
                transactions, location=location, timezone=selected_zone,
                profile=csv_profile)
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise HTTPException(503, "Charger report unavailable") from exc
        safe_source = re.sub(r"[^A-Za-z0-9._-]", "_", source_id)[:64]
        safe_charger = re.sub(r"[^A-Za-z0-9._-]", "_", charger_id)[:80]
        filename = f"charger-{safe_source or 'source'}-{safe_charger or 'station'}.csv"
        return Response(
            content=content, media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            })

    @app.get("/chargers/{source_id}/{charger_id}/configuration", response_class=HTMLResponse)
    def charger_config_history(
        request: Request, source_id: str, charger_id: str,
        tz: str | None = None, event_id: int | None = Query(None, ge=1),
        page: int = Query(1, ge=1, le=1000), download: bool = False,
    ):
        """Administrator-only, evidence-backed GetConfiguration history."""
        if not _can_view_configuration(request):
            raise HTTPException(403, "Configuration history requires administrator access")
        if (not source_id.strip() or not charger_id.strip()
                or allowed_source_ids is not None and source_id not in allowed_source_ids):
            raise HTTPException(404, "Configuration history not found")
        zone = tz or zones[0]
        if zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        try:
            versions, has_next = list_snapshots(
                warehouse, source_id, charger_id, limit=25, offset=(page - 1) * 25)
            selected = (one_snapshot(warehouse, source_id, charger_id, event_id)
                        if event_id is not None else (versions[0] if versions else None))
        except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise HTTPException(503, "Configuration history unavailable") from exc
        if selected is None:
            raise HTTPException(404, "Configuration history not found")
        if download:
            safe_source = re.sub(r"[^A-Za-z0-9._-]", "_", source_id)[:64]
            safe_charger = re.sub(r"[^A-Za-z0-9._-]", "_", charger_id)[:80]
            filename = (f"configuration-{safe_source}-{safe_charger}-"
                        f"{selected['source_event_id']}.csv")
            return Response(
                content=render_snapshot_csv(selected), media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"',
                         "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        return templates.TemplateResponse(
            request, "charger_configuration.html.j2",
            {"title": title, "brand_image_url": brand_image_url, "source_id": source_id, "charger_id": charger_id,
             "selected": selected, "versions": versions, "has_next": has_next,
             "page": page, "timezone": zone},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/chargers/{source_id}/{charger_id}/site", response_class=HTMLResponse)
    def charger_site_editor(request: Request, source_id: str, charger_id: str):
        _site_scope(request, source_id)
        from .site_metadata import _connect, known_charger
        with _connect(warehouse) as db:
            if not known_charger(db, source_id, charger_id):
                raise HTTPException(404, "Charger not found")
        current = site_for_charger(warehouse, source_id, charger_id)
        if current is None:
            legacy = read(source_id, charger_id, 1, skip_events=True)
            legacy_name = next((r.get("location") for r in legacy["connectors"]
                                if r.get("location") and r["location"] != source_id), None)
            current = {"location_name": legacy_name}
        details = hours_fields(current.get("contact_hours") if current else None)
        return templates.TemplateResponse(
            request, "charger_site.html.j2",
            {"title": title, "brand_image_url": brand_image_url, "source_id": source_id, "charger_id": charger_id,
             "site": current or {}, "days": list(zip(DAYS, DAY_NAMES)),
             "hours": details, "other_sites": editable_sites(warehouse, allowed_source_ids),
             "customers": editable_customers(warehouse, allowed_source_ids)},
            headers={"Cache-Control": "no-store"})

    @app.post("/chargers/{source_id}/{charger_id}/site")
    async def charger_site_save(request: Request, source_id: str, charger_id: str):
        _site_scope(request, source_id)
        _site_write_request(request)
        body = await request.body()
        if len(body) > 10000:
            raise HTTPException(413, "Form too large")
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=36)
            if any(len(item) != 1 for item in parsed.values()):
                raise ValueError("Duplicate form fields")
            fields = {key: item[0] for key, item in parsed.items()}
            save_site(warehouse, source_id, charger_id, fields,
                      allowed_source_ids=allowed_source_ids)
        except PermissionError:
            raise HTTPException(403, "Site access denied")
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(
            f"/chargers/{urlencode({'s': source_id})[2:]}/{urlencode({'c': charger_id})[2:]}",
            status_code=303, headers={"Cache-Control": "no-store"})

    @app.post("/chargers/{source_id}/{charger_id}/site/assign")
    async def charger_site_assign(request: Request, source_id: str, charger_id: str):
        _site_scope(request, source_id)
        _site_write_request(request)
        body = await request.body()
        if len(body) > 2000:
            raise HTTPException(413, "Form too large")
        try:
            parsed = parse_qs(body.decode("utf-8"), max_num_fields=2)
            site_id = parsed.get("site_id", [""])[0]
            assign_site(warehouse, source_id, charger_id, site_id,
                        allowed_source_ids=allowed_source_ids)
        except PermissionError:
            raise HTTPException(403, "Site access denied")
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(
            f"/chargers/{urlencode({'s': source_id})[2:]}/{urlencode({'c': charger_id})[2:]}",
            status_code=303, headers={"Cache-Control": "no-store"})

    @app.get("/chargers/{source_id}/{charger_id}", response_class=HTMLResponse)
    def charger_page(request: Request, source_id: str, charger_id: str,
                     tz: str | None = None, tx_page: int = Query(1, ge=1),
                     event_page: int = Query(1, ge=1)):
        return _device_page(request, source_id, charger_id, None, tz, tx_page, event_page)

    @app.get("/connectors/{source_id}/{charger_id}/{connector_id}", response_class=HTMLResponse)
    def connector_page(request: Request, source_id: str, charger_id: str,
                       connector_id: int, tz: str | None = None,
                       tx_page: int = Query(1, ge=1)):
        return _device_page(request, source_id, charger_id, connector_id, tz, tx_page, 1)

    @app.get("/api/v1/charger-event-group", tags=["Dashboard API v1"])
    def api_charger_event_group(
        request: Request, source_id: str, charger_id: str,
        first_event_id: int = Query(..., ge=1), tz: str | None = None,
        offset: int = Query(0, ge=0),
    ):
        """Scoped, sanitized charger event details; global event API stays admin-only."""
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value not in ("admin", "operator"):
            raise HTTPException(403, "Charger event history unavailable")
        if not source_id.strip() or not charger_id.strip():
            raise HTTPException(422, "Source and charger are required")
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            raise HTTPException(404, "Charger not found")
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        try:
            # The exact source and charger are mandatory here. The grouping
            # function returns only previously approved display fields, never
            # raw OCPP payloads or identifiers such as RFID/vendor metadata.
            result = event_group_details(
                warehouse, group_source=source_id, first_event_id=first_event_id,
                source_id=source_id, charger_id=charger_id,
                allowed_source_ids=allowed_source_ids, zone=selected_zone,
                offset=offset,
            )
            for event in result["events"]:
                event["display_at"] = display_date(event.get("at"), selected_zone)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except LookupError as exc:
            raise HTTPException(404, "Event group not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, "Source is not available") from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid event selection") from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Event history unavailable") from exc

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

    @app.get("/api/v1/transactions/{source_id}/{charger_id}/{transaction_id}",
             tags=["Dashboard API v1"])
    def api_transaction_detail(source_id: str, charger_id: str, transaction_id: int,
                               tz: str | None = None):
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        # Identical scope and not-found semantics to the existing HTML detail.
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
        return {"transaction": transaction, "timezone": selected_zone}

    @app.get("/rfid", response_class=HTMLResponse)
    def rfid_page(request: Request, rfid: str | None = Query(None, min_length=1),
                  otros: bool = False, tz: str | None = None, page: int = Query(1, ge=1)):
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        if (rfid is None) == (not otros):
            raise HTTPException(422, "Select RFID or Otros")
        if rfid is not None and not rfid.strip():
            raise HTTPException(404, "Card not found")
        try:
            with sqlite3.connect(warehouse.resolve().as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                result = query_transaction_page(
                    db, tables, source_id=None, charger_id=None, allowed_source_ids=allowed_source_ids,
                    term="", status="", location="", date_from="", date_to="", zone=selected_zone,
                    view="all", page=page, rfid=rfid, card_history=True, others=otros,
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(503, "Warehouse unavailable") from exc
        if not result["total"]:
            raise HTTPException(404, "Card not found")
        points = result["card_points"]
        cumulative_kwh = 0.0
        for point in points:
            if point["kwh"] is not None:
                cumulative_kwh += point["kwh"]
                point["cumulative_kwh"] = cumulative_kwh
            else:
                point["cumulative_kwh"] = None
        peak = max(1, cumulative_kwh)
        for i, point in enumerate(points):
            point["x"] = 65 + 645 * i / max(1, len(points) - 1)
            point["y"] = (205 - 180 * point["cumulative_kwh"] / peak
                          if point["cumulative_kwh"] is not None else None)
        return templates.TemplateResponse(request, "rfid_detail.html.j2", {
            "title": title, "brand_image_url": brand_image_url, "rfid": rfid, "otros": otros,
            "timezone": selected_zone, "result": result, "points": points, "peak": peak,
            "energy_kwh": cumulative_kwh,
        }, headers={"Cache-Control": "no-store"})

    @app.get("/transactions/{source_id}/{charger_id}/{transaction_id}", response_class=HTMLResponse)
    def transaction_page(request: Request, source_id: str, charger_id: str,
                         transaction_id: int, tz: str | None = None,
                         return_to: str | None = None):
        selected_zone = tz or zones[0]
        if selected_zone not in zones:
            raise HTTPException(422, "Unsupported dashboard timezone")
        # Return the same 404 for missing and inaccessible transactions.
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            raise HTTPException(404, "Transaction not found")
        try:
            transaction = transaction_detail(
                warehouse, source_id=source_id, charger_id=charger_id,
                transaction_id=transaction_id, allowed_source_ids=allowed_source_ids,
            )
        except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(503, "Warehouse unavailable") from exc
        if transaction is None:
            raise HTTPException(404, "Transaction not found")
        active = is_active_session(transaction)
        # A return URL is accepted only for the dashboard root, never a
        # caller-supplied hostname or another internal endpoint.
        from urllib.parse import urlsplit, parse_qsl, urlencode
        back_href = "/?tz=" + urlencode({"tz": selected_zone})[3:] + "#transactions"
        if return_to and len(return_to) <= 2048:
            target = urlsplit(return_to)
            allowed_keys = {"source_id", "charger_id", "filter", "status",
                            "location", "date_from", "date_to", "tx_view",
                            "tx_page", "event_page", "tz"}
            if (not target.scheme and not target.netloc and target.path == "/"
                    and target.fragment == "transactions"):
                values = [(k, v) for k, v in parse_qsl(target.query, keep_blank_values=True)
                          if k in allowed_keys]
                if all(k != "tz" or v in zones for k, v in values):
                    back_href = "/?" + urlencode(values) + "#transactions"
        try:
            timeline = transaction_timeline(warehouse, transaction)
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Timeline warehouse unavailable") from exc
        # The latest independently verified OCPP SoC sample can populate the
        # HTML display when the transaction projection has no SoC endpoint.
        # Do not modify the transaction projection or claim a final-at-stop reading.
        timeline_soc_end = next(
            (p["soc"] for p in reversed(timeline["points"]) if p.get("soc") is not None),
            None,
        )
        # Show the power column only when at least one verified sample exists.
        timeline_has_power = any(p.get("power_kw") is not None for p in timeline["points"])
        timeline_available = {
            field: any(p.get(field) is not None for p in timeline["points"])
            for field in ("soc", "voltage_v", "current_import_a",
                          "current_offered_a", "temperature_c")
        }
        return templates.TemplateResponse(
            request, "transaction_detail.html.j2",
            {"title": title, "brand_image_url": brand_image_url, "transaction": transaction, "timezone": selected_zone,
             "timezones": zones, "refresh_active": active,
             "timeline": timeline, "timeline_charts": timeline_charts(timeline["points"]),
             "timeline_soc_end": timeline_soc_end, "timeline_has_power": timeline_has_power,
             "timeline_available": timeline_available,
             "back_href": back_href,
              "elapsed_duration": session_duration(transaction.get("started_at"), datetime.now(timezone.utc).isoformat()) if active else None},
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
