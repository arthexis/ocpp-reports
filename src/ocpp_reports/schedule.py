"""Deterministic previous-period calculation for external schedulers."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def previous_period(now: datetime, *, cadence: str, zone: str) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if cadence not in {"daily", "weekly", "monthly"}:
        raise ValueError("cadence must be daily, weekly or monthly")
    local = now.astimezone(ZoneInfo(zone))
    today = local.date()
    if cadence == "daily":
        end_date = today
        start_date = end_date - timedelta(days=1)
    elif cadence == "weekly":
        end_date = today - timedelta(days=today.weekday())
        start_date = end_date - timedelta(days=7)
    else:
        end_date = today.replace(day=1)
        start_date = (end_date - timedelta(days=1)).replace(day=1)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=ZoneInfo(zone))
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=ZoneInfo(zone))
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
