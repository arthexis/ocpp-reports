from datetime import datetime, timezone
import pytest
from ocpp_reports.schedule import previous_period


def test_previous_day_uses_local_midnight_and_dst():
    start, end = previous_period(datetime(2026, 3, 9, 14, tzinfo=timezone.utc), cadence="daily", zone="America/New_York")
    assert (end - start).total_seconds() == 23 * 3600


def test_weekly_previous_complete_monday_to_monday():
    start, end = previous_period(datetime(2026, 10, 14, tzinfo=timezone.utc), cadence="weekly", zone="UTC")
    assert start.isoformat() == "2026-10-05T00:00:00+00:00"
    assert end.isoformat() == "2026-10-12T00:00:00+00:00"


def test_monthly_handles_year_boundary():
    start, end = previous_period(datetime(2026, 1, 12, tzinfo=timezone.utc), cadence="monthly", zone="UTC")
    assert start.year == 2025 and start.month == 12
    assert end.year == 2026 and end.month == 1


def test_reject_naive_clock():
    with pytest.raises(ValueError):
        previous_period(datetime(2026, 10, 1), cadence="daily", zone="UTC")
