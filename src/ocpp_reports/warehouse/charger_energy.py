"""30-day charger energy from completed physical-connector sessions.

Local transaction projections take precedence over identical remote records.
An absent completed session is not proof of zero physical energy usage.
"""
from __future__ import annotations
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def charger_energy_30_days(database: str | Path, *, source_id: str,
                           charger_id: str, allowed_source_ids=None,
                           zone: str = "UTC", now: datetime | None = None) -> dict:
    if not source_id or not charger_id:
        raise ValueError("Exact source and charger required")
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        raise ValueError("Source not authorized")
    tz = ZoneInfo(zone)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Naive current timestamp")
    instant = instant.astimezone(timezone.utc)
    today = instant.astimezone(tz).date()
    first = today - timedelta(days=29)
    daily = {first + timedelta(days=i): [0.0, 0] for i in range(30)}
    seen: set[int] = set()

    def include(tx_id, value):
        if type(tx_id) is not int or tx_id in seen:
            return
        seen.add(tx_id)  # local identity wins even if corrupt
        if not isinstance(value, dict) or value.get("id") != tx_id:
            return
        connector = value.get("connector_id")
        energy = value.get("energy_wh")
        stopped = _when(value.get("stopped_at"))
        if (type(connector) is not int or connector <= 0
                or type(energy) not in (int, float)
                or not math.isfinite(energy) or energy < 0
                or value.get("active") is True
                or stopped is None or stopped > instant):
            return
        day = stopped.astimezone(tz).date()
        if day in daily:
            daily[day][0] += energy
            daily[day][1] += 1

    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "current_transactions" in tables:
            for tx_id, payload in db.execute(
                    "SELECT transaction_id,payload FROM current_transactions "
                    "WHERE source_id=? AND charger_id=?", (source_id, charger_id)):
                try:
                    include(tx_id, json.loads(payload))
                except (TypeError, ValueError):
                    seen.add(tx_id)
        if "collector_latest" in tables:
            for (payload,) in db.execute(
                    "SELECT payload FROM collector_latest WHERE source_id=? "
                    "AND kind='transaction' "
                    "AND json_extract(payload,'$.charger_id')=?",
                    (source_id, charger_id)):
                try:
                    value = json.loads(payload)
                    if isinstance(value, dict):
                        include(value.get("id"), value)
                except (TypeError, ValueError):
                    continue
    largest = max(item[0] for item in daily.values())
    bars = []
    for i, (day, (wh, sessions)) in enumerate(daily.items()):
        height = 168 * wh / largest if largest else 0
        bars.append({
            "day": day.isoformat(), "kwh": round(wh / 1000, 3),
            "sessions": sessions, "x": 50 + i * 28,
            "y": round(194 - height, 2), "height": round(height, 2),
            "label": day.strftime("%d/%m") if i % 5 == 0 or i == 29 else None,
        })
    return {"bars": bars,
            "total_kwh": round(sum(x[0] for x in daily.values()) / 1000, 3),
            "sessions_count": sum(x[1] for x in daily.values()),
            "date_from": first.isoformat(), "date_to": today.isoformat()}
