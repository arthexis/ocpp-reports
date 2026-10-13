"""Report metadata; source transaction schema is deliberately not assumed."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime
    until: datetime
    timezone: str = Field(min_length=1)

    def validate_period(self) -> None:
        if self.since.tzinfo is None or self.until.tzinfo is None:
            raise ValueError("since and until must include UTC offsets")
        if self.since >= self.until:
            raise ValueError("since must precede until")

class ReportManifest(BaseModel):
    schema_version: Literal["ocpp-reports/manifest/v1"] = "ocpp-reports/manifest/v1"
    report_type: str
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    source_schema: str | None = None
