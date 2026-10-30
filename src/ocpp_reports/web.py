"""Optional local-only, read-only Reports dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .warehouse_queries import snapshot


def create_app(database: str | Path, *, allowed_source_ids: tuple[str, ...] | None = None,
               title: str = "OCPP Reports") -> FastAPI:
    if allowed_source_ids is not None and (not allowed_source_ids or
            any(not isinstance(source, str) or not source.strip() for source in allowed_source_ids)):
        raise ValueError("Source allowlist must contain nonempty source IDs")
    app = FastAPI(title="OCPP Reports", docs_url=None, redoc_url=None, openapi_url=None)
    warehouse = Path(database)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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
              charger_id: str | None = None):
        data = read(source_id, charger_id, 100)
        now = datetime.now(timezone.utc)
        for connector in data["connectors"]:
            try:
                observed = datetime.fromisoformat(connector["observed_at"].replace("Z", "+00:00"))
                connector["stale"] = (now - observed).total_seconds() > 30
            except (ValueError, AttributeError, TypeError):
                connector["stale"] = True
        return templates.TemplateResponse(request, "dashboard.html.j2",
                                          {"data": data, "title": title, "source_id": source_id or "",
                                           "charger_id": charger_id or ""})

    @app.get("/api/snapshot")
    def api_snapshot(source_id: str | None = None, charger_id: str | None = None,
                     limit: int = Query(100, ge=1, le=500)):
        return read(source_id, charger_id, limit)

    return app
