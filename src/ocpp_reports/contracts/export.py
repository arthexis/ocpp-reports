"""Strict, independent consumer of the CSMS cursor export/v1 contract."""
from __future__ import annotations
import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from .csms_export import validate_export

class Cursor(BaseModel):
    model_config = ConfigDict(extra="allow")
    after: int = Field(ge=0)
    next: int = Field(ge=0)
    more: bool

class Transaction(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    charger_id: str
    connector_id: int | None = None
    rfid: str | None = None
    state: str
    active: bool
    started_at: str | None = None
    stopped_at: str | None = None
    meter_start_wh: int | None = None
    meter_stop_wh: int | None = None
    energy_wh: int | None = None
    last_activity_at: str | None = None

class ExportData(BaseModel):
    model_config = ConfigDict(extra="allow")
    source_id: str = Field(min_length=1)
    cursor: Cursor
    events: list[dict] = Field(default_factory=list)
    energy: list[dict] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)
    status: dict = Field(default_factory=dict)

class ExportPage(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_id: str = Field(alias="schema")
    data: ExportData

def read_page(path: Path) -> ExportPage:
    raw = json.loads(path.read_text(encoding="utf-8"))
    validate_export(raw)
    page = ExportPage.model_validate(raw)
    if page.schema_id != "ocpp-csms/export/v1":
        raise ValueError(f"Unsupported CSMS export schema: {page.schema_id}")
    if page.data.cursor.next < page.data.cursor.after:
        raise ValueError("Export cursor regressed")
    return page
