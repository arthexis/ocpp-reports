"""Versioned read-only JSON views over the existing warehouse snapshot.

No independent database, ingestion path, or business-rule implementation.
"""
from __future__ import annotations

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.security import HTTPBearer


class ItemsResponse(BaseModel):
    items: list[dict] = Field(default_factory=list)


class TransactionsResponse(ItemsResponse):
    total: int
    page: int
    pages: int
    counts: dict[str, int]

from fastapi.responses import JSONResponse
from .web import _operational_status_counts, _filter_transaction_energy


def register_api_v1(app, read):
    # Documentation only: authorization is enforced centrally by dashboard_login.
    bearer = HTTPBearer(auto_error=False)
    from fastapi import Depends
    def documented_auth(_credentials=Depends(bearer)):
        return None
    def visible(request: Request, source_id, charger_id, limit, *, pagination=None):
        data = read(source_id, charger_id, limit, transaction_pagination=pagination, skip_events=True)
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.role.value != "admin":
            data = {**data, "events": [], "sources": []}
        return data

    @app.get("/api/v1/summary", tags=["Dashboard API v1"], dependencies=[Depends(documented_auth)])
    def summary(request: Request, source_id: str | None = None,
                charger_id: str | None = None):
        data = visible(request, source_id, charger_id, 500)
        return {"status_counts": _operational_status_counts(data["connectors"])}

    @app.get("/api/v1/connectors", tags=["Dashboard API v1"], response_model=ItemsResponse)
    def connectors(request: Request, source_id: str | None = None,
                   charger_id: str | None = None):
        return {"items": visible(request, source_id, charger_id, 500)["connectors"]}

    @app.get("/api/v1/transactions", tags=["Dashboard API v1"], response_model=TransactionsResponse)
    def transactions(request: Request, source_id: str | None = None,
                     charger_id: str | None = None,
                     view: str = "all", page: int = Query(1, ge=1)):
        if view not in ("all", "success", "failed"):
            raise HTTPException(422, "Invalid transaction view")
        data = visible(request, source_id, charger_id, 100, pagination={
            "term": "", "status": "", "location": "", "date_from": "",
            "date_to": "", "zone": "UTC", "view": view, "page": page})
        info = data["transaction_pagination"]
        return {"items": data["transactions"], "total": info["total"],
                "page": info["page"], "pages": info["pages"], "counts": info["counts"]}

    @app.get("/api/v1/stations", tags=["Dashboard API v1"], response_model=ItemsResponse)
    def stations(request: Request, source_id: str | None = None):
        rows = visible(request, source_id, None, 500)["connectors"]
        groups = {}
        for row in rows:
            key = (row.get("source_id"), row.get("charger_id"))
            groups.setdefault(key, []).append(row)
        return {"items": [{"source_id": source, "charger_id": charger,
                           "connectors": items} for (source, charger), items in groups.items()]}

    @app.get("/api/v1/filters", tags=["Dashboard API v1"])
    def filters(request: Request, source_id: str | None = None):
        rows = visible(request, source_id, None, 500)["connectors"]
        return {"source_ids": sorted({r["source_id"] for r in rows if r.get("source_id")}),
                "charger_ids": sorted({r["charger_id"] for r in rows if r.get("charger_id")}),
                "locations": sorted({r["location"] for r in rows if r.get("location")})}

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    def openapi_v1():
        return JSONResponse(app.openapi(), headers={"Cache-Control": "no-store"})
