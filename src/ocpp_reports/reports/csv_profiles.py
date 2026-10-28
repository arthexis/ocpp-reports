"""CSV compatibility profiles for historical customer reports."""
from __future__ import annotations

from zoneinfo import ZoneInfo
from .vendor_common import HEADERS, writer_buffer, local_date



def render_vendor(report: dict, *, locations: dict | None = None) -> str:
    """Render the vendor ten-column layout; unknown measurements remain blank."""
    from ..validation import validate_summary
    validate_summary(report)
    locations = locations or {}
    zone = ZoneInfo(report["period"]["timezone"])
    output, writer = writer_buffer()
    for tx in report["transactions"]:
        writer.writerow([
            locations.get(tx["charger_id"], ""),
            tx.get("connector_id") if tx.get("connector_id") is not None else "",
            local_date(tx.get("started_at"), zone, iso=False), local_date(tx.get("stopped_at"), zone, iso=False),
            tx.get("meter_start_wh") if tx.get("meter_start_wh") is not None else "",
            tx.get("meter_stop_wh") if tx.get("meter_stop_wh") is not None else "",
            tx.get("energy_wh") if tx.get("energy_wh") is not None else "",
            tx.get("battery_start_pct") if tx.get("battery_start_pct") is not None else "",
            tx.get("battery_stop_pct") if tx.get("battery_stop_pct") is not None else "",
            tx.get("stop_reason") or "",
        ])
    return output.getvalue()
