"""Audi CSV compatibility profile tests."""
import csv
import io
from ocpp_reports.csv_profiles import HEADERS
from ocpp_reports.reporting import render


def test_audi_headers_and_nullable_fields():
    report = {
        "schema": "ocpp-reports/summary/v1",
        "source_id": "site", "revision": "test",
        "period": {"since": "2026-01-01T00:00:00+00:00",
                   "until": "2026-01-02T00:00:00+00:00",
                   "timezone": "UTC", "semantics": "[since,until)"},
        "summary": {"transactions": 1, "known_energy_wh": 0,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [{"id": 7, "charger_id": "cp1", "connector_id": 1,
                          "started_at": "2026-01-01T01:00:00Z",
                          "stopped_at": "2026-01-01T01:01:00Z",
                          "meter_start_wh": 100, "meter_stop_wh": 100,
                          "energy_wh": 0, "active": False, "state": "completed"}],
        "evidence": [],
    }
    rows = list(csv.reader(io.StringIO(render(report, "csv", profile="audi",
                                           locations={"cp1": "Calzada"}))))
    assert rows[0] == list(HEADERS)
    assert len(rows[0]) == 10
    assert rows[1] == ["Calzada", "1", "2026-01-01 01:00:00",
                       "2026-01-01 01:01:00", "100", "100", "0", "", "", ""]
