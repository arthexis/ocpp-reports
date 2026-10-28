"""Client-specific CSV projections; absent evidence stays blank."""
from __future__ import annotations

from zoneinfo import ZoneInfo
from .reports.audi_common import HEADERS, writer_buffer, local_date, blank



def render_audi_csv(report: dict) -> str:
    output, writer = writer_buffer()
    zone = ZoneInfo(report["period"]["timezone"])

    for tx in report["transactions"]:
        writer.writerow([
            blank(tx.get("location")), blank(tx.get("connector_id")),
            local_date(tx.get("started_at"), zone, iso=True), local_date(tx.get("stopped_at"), zone, iso=True),
            blank(tx.get("meter_start_wh")), blank(tx.get("meter_stop_wh")),
            blank(tx.get("energy_wh")), blank(tx.get("soc_start")),
            blank(tx.get("soc_end")), blank(tx.get("stop_reason")),
        ])
    return output.getvalue()
