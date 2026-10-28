import csv
import io
from ocpp_reports.profiles import HEADERS, render_vendor_csv


def test_vendor_profile_exact_columns_and_missing_evidence():
    report = {"period": {"timezone": "UTC"}, "transactions": [{
        "connector_id": 2, "started_at": "2026-01-01T01:00:00Z",
        "stopped_at": "2026-01-01T01:05:00Z",
        "meter_start_wh": 1200, "meter_stop_wh": 1200, "energy_wh": 0
    }]}
    rows = list(csv.reader(io.StringIO(render_vendor_csv(report))))
    assert tuple(rows[0]) == HEADERS
    assert len(rows[0]) == 10
    assert rows[1] == ["", "2", "2026-01-01T01:00:00+00:00",
                       "2026-01-01T01:05:00+00:00", "1200", "1200",
                       "0", "", "", ""]
