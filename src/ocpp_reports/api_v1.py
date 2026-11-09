"""Versioned read-only JSON views over the existing warehouse snapshot.

No independent database, ingestion path, or business-rule implementation.
"""
from __future__ import annotations

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime, timezone
from fastapi.security import HTTPBearer


class ItemsResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class ConnectorItem(BaseModel):
    """Stable connector identity; preserve vendor-specific projected fields."""
    model_config = {"extra": "allow"}

    source_id: str
    charger_id: str
    connector_id: int


class ConnectorsResponse(BaseModel):
    items: list[ConnectorItem] = Field(default_factory=list)


class TransactionItem(BaseModel):
    """Stable transaction identity with backward-compatible optional details."""
    model_config = {"extra": "allow"}

    source_id: str
    charger_id: str


class SourcesResponse(ItemsResponse):
    pass


class OperationalCount(BaseModel):
    stations: int
    connectors: int


class SummaryResponse(BaseModel):
    status_counts: dict[str, OperationalCount]


class FiltersResponse(BaseModel):
    source_ids: list[str]
    charger_ids: list[str]
    locations: list[str]


class StationItem(BaseModel):
    source_id: str
    charger_id: str
    connectors: list[dict[str, Any]] = Field(default_factory=list)


class StationsResponse(BaseModel):
    items: list[StationItem] = Field(default_factory=list)


class TransactionCounts(BaseModel):
    all: int
    success: int
    failed: int


class TransactionsResponse(BaseModel):
    items: list[TransactionItem] = Field(default_factory=list)
    total: int
    page: int
    pages: int
    counts: TransactionCounts

from fastapi.responses import JSONResponse
from .web import project_dashboard_connectors, _operational_status_counts, _source_freshness, _filter_dashboard, validate_dashboard_window


def register_api_v1(app, read, *, timezones: tuple[str, ...] = ("UTC",)):
    # Documentation only: authorization is enforced centrally by dashboard_login.
    bearer = HTTPBearer(auto_error=False)
    from fastapi import Depends
    def documented_auth(_credentials=Depends(bearer)):
        return None
    def visible(request: Request, source_id, charger_id, limit, *, pagination=None, source_evidence=False):
        data = read(source_id, charger_id, limit, transaction_pagination=pagination, skip_events=True)
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value != "admin":
            data = {**data, "events": [], "sources": data["sources"] if source_evidence else []}
        return data

    @app.get("/api/v1/summary", tags=["Dashboard API v1"], response_model=SummaryResponse, dependencies=[Depends(documented_auth)])
    def summary(request: Request, source_id: str | None = None,
                charger_id: str | None = None):
        data = visible(request, source_id, charger_id, 500, source_evidence=True)
        return {"status_counts": _operational_status_counts(data["connectors"])}

    @app.get("/api/v1/connectors", tags=["Dashboard API v1"], response_model=ConnectorsResponse, dependencies=[Depends(documented_auth)])
    def connectors(request: Request, source_id: str | None = None,
                   charger_id: str | None = None, connector_view: str = "combined",
                   filter: str = "", status: str = "", location: str = "",
                   date_from: str = "", date_to: str = "", tz: str | None = None):
        if connector_view not in ("combined", "station", "individual"):
            raise HTTPException(422, "Invalid connector view")
        zone = tz or timezones[0]
        validate_dashboard_window(zone, timezones, date_from, date_to)
        data = visible(request, source_id, charger_id, 500, source_evidence=True)
        try:
            source_freshness = {source["source_id"]: _source_freshness(source, datetime.now(timezone.utc))
                                for source in data["sources"]}
            data = _filter_dashboard(data, term=filter, status=status, location=location,
                                     date_from=date_from, date_to=date_to, zone=zone)
            projected = project_dashboard_connectors(data, now=datetime.now(timezone.utc),
                                                     connector_view=connector_view, source_freshness=source_freshness)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"items": projected["connectors"]}

    @app.get("/api/v1/sources", tags=["Dashboard API v1"], response_model=SourcesResponse,
             dependencies=[Depends(documented_auth)])
    def sources(request: Request, source_id: str | None = None):
        # Match the existing HTML visibility policy: source sync diagnostics are admin-only.
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value != "admin":
            raise HTTPException(403, "Source diagnostics require administrator access")
        data = visible(request, source_id, None, 500, source_evidence=True)
        now = datetime.now(timezone.utc)
        return {"items": [{**source, "freshness": _source_freshness(source, now)}
                          for source in data["sources"]]}

    @app.get("/api/v1/transactions", tags=["Dashboard API v1"], response_model=TransactionsResponse, dependencies=[Depends(documented_auth)])
    def transactions(request: Request, source_id: str | None = None,
                     charger_id: str | None = None,
                     view: str = "all", page: int = Query(1, ge=1),
                     filter: str = "", status: str = "", location: str = "",
                     date_from: str = "", date_to: str = "", tz: str | None = None):
        if view not in ("all", "success", "failed"):
            raise HTTPException(422, "Invalid transaction view")
        zone = tz or timezones[0]
        validate_dashboard_window(zone, timezones, date_from, date_to)
        # Use the exact SQL pagination/filter contract consumed by the HTML dashboard.
        data = visible(request, source_id, charger_id, 100, pagination={
            "term": filter, "status": status, "location": location,
            "date_from": date_from, "date_to": date_to,
            "zone": zone, "view": view, "page": page})
        info = data["transaction_pagination"]
        return {"items": data["transactions"], "total": info["total"],
                "page": info["page"], "pages": info["pages"], "counts": info["counts"]}

    @app.get("/api/v1/stations", tags=["Dashboard API v1"], response_model=StationsResponse, dependencies=[Depends(documented_auth)])
    def stations(request: Request, source_id: str | None = None):
        data = visible(request, source_id, None, 500, source_evidence=True)
        rows = project_dashboard_connectors(data, now=datetime.now(timezone.utc))["connectors"]
        groups = {}
        for row in rows:
            key = (row.get("source_id"), row.get("charger_id"))
            groups.setdefault(key, []).append(row)
        return {"items": [{"source_id": source, "charger_id": charger,
                           "connectors": items} for (source, charger), items in groups.items()]}

    @app.get("/api/v1/filters", tags=["Dashboard API v1"], response_model=FiltersResponse, dependencies=[Depends(documented_auth)])
    def filters(request: Request, source_id: str | None = None):
        rows = visible(request, source_id, None, 500)["connectors"]
        return {"source_ids": sorted({r["source_id"] for r in rows if r.get("source_id")}),
                "charger_ids": sorted({r["charger_id"] for r in rows if r.get("charger_id")}),
                "locations": sorted({r["location"] for r in rows if r.get("location")})}

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    def openapi_v1():
        return JSONResponse(app.openapi(), headers={"Cache-Control": "no-store"})
