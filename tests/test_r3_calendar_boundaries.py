"""R3D1 fixed-clock calendar boundary and daylight-saving regression matrix."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ocpp_reports.schedule import previous_period


@pytest.mark.parametrize("now,cadence,zone,start,end", [
    ("2026-01-01T12:00:00+00:00", "daily", "UTC",
     "2025-12-31T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    ("2024-03-01T12:00:00+00:00", "daily", "UTC",
     "2024-02-29T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),
    ("2024-03-01T12:00:00+00:00", "monthly", "UTC",
     "2024-02-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),
    ("2025-03-01T12:00:00+00:00", "monthly", "UTC",
     "2025-02-01T00:00:00+00:00", "2025-03-01T00:00:00+00:00"),
    ("2026-01-01T12:00:00+00:00", "monthly", "UTC",
     "2025-12-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    ("2026-01-01T12:00:00+00:00", "weekly", "UTC",
     "2025-12-22T00:00:00+00:00", "2025-12-29T00:00:00+00:00"),
    ("2026-03-09T12:00:00+00:00", "daily", "America/New_York",
     "2026-03-08T05:00:00+00:00", "2026-03-09T04:00:00+00:00"),
    ("2026-11-02T12:00:00+00:00", "daily", "America/New_York",
     "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
    ("2026-03-09T12:00:00+00:00", "weekly", "America/New_York",
     "2026-03-02T05:00:00+00:00", "2026-03-09T04:00:00+00:00"),
    ("2026-11-02T12:00:00+00:00", "weekly", "America/New_York",
     "2026-10-26T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
    ("2026-04-15T12:00:00+00:00", "monthly", "America/New_York",
     "2026-03-01T05:00:00+00:00", "2026-04-01T04:00:00+00:00"),
    ("2026-11-15T12:00:00+00:00", "monthly", "America/New_York",
     "2026-10-01T04:00:00+00:00", "2026-11-01T04:00:00+00:00"),
])
def test_exact_completed_calendar_period(now, cadence, zone, start, end):
    actual = previous_period(datetime.fromisoformat(now), cadence=cadence, zone=zone)
    assert actual == (datetime.fromisoformat(start), datetime.fromisoformat(end))
    assert all(bound.tzinfo == timezone.utc for bound in actual)
    assert actual[0] < actual[1]


@pytest.mark.parametrize("cadence,anchor", [
    ("daily", datetime(2026, 3, 9, 12, tzinfo=timezone.utc)),
    ("weekly", datetime(2026, 3, 9, 12, tzinfo=timezone.utc)),
    ("monthly", datetime(2026, 4, 15, 12, tzinfo=timezone.utc)),
])
def test_adjacent_periods_touch_without_overlap_or_gap(cadence, anchor):
    zone = "America/New_York"
    later_start, later_end = previous_period(anchor, cadence=cadence, zone=zone)
    local_start = later_start.astimezone(ZoneInfo(zone))
    earlier_clock = local_start - timedelta(days=1)
    earlier_start, earlier_end = previous_period(earlier_clock, cadence=cadence, zone=zone)
    assert earlier_end == later_start
    assert earlier_start < earlier_end <= later_end


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Europe/Berlin"])
def test_boundary_is_selected_using_local_date_not_utc_date(zone):
    clock = datetime(2026, 10, 13, 1, tzinfo=timezone.utc)
    start, end = previous_period(clock, cadence="daily", zone=zone)
    local = clock.astimezone(ZoneInfo(zone))
    assert end.astimezone(ZoneInfo(zone)).date() == local.date()
    assert start.astimezone(ZoneInfo(zone)).date() == local.date() - timedelta(days=1)


@pytest.mark.parametrize("cadence", ["hourly", "", "yearly"])
def test_invalid_cadence_is_rejected(cadence):
    with pytest.raises(ValueError, match="cadence"):
        previous_period(datetime(2026, 10, 13, tzinfo=timezone.utc),
                        cadence=cadence, zone="UTC")
