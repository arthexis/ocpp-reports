"""Golden compatibility contracts for both supported Audi CSV interfaces."""
import csv
import io

from ocpp_reports.profiles import render_audi_csv
from ocpp_reports.reports.csv_profiles import render_audi


def summary():
    return {
        "schema": "ocpp-reports/summary/v1",
        "source_id": "source",
        "period": {"since": "2026-10-01T00:00:00Z",
                   "until": "2026-11-01T00:00:00Z", "timezone": "UTC"},
        "summary": {"transactions": 1, "known_energy_wh": 250,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [{
            "id": 1, "charger_id": "cp", "connector_id": 1,
            "state": "stopped", "active": False,
            "started_at": "2026-10-04T12:00:00Z",
            "stopped_at": "2026-10-04T12:30:00Z",
            "meter_start_wh": 100, "meter_stop_wh": 350, "energy_wh": 250,
            "location": "Legacy Site", "soc_start": 25, "soc_end": 50,
            "battery_start_pct": 20, "battery_stop_pct": 60,
            "stop_reason": "Normal",
        }],
    }


def test_legacy_audi_csv_bytes():
    actual = render_audi_csv(summary())
    expected = (
        "LOCACION,CONECTOR,FECHA INICIO,FECHA FINAL,WH INICIO,WH FINAL,"
        "WH USADOS,% INICIAL,% FINAL,RAZON FINAL\r\n"
        "Legacy Site,1,2026-10-04T12:00:00+00:00,"
        "2026-10-04T12:30:00+00:00,100,350,250,25,50,Normal\r\n"
    )
    assert actual == expected


def test_configurable_audi_profile_bytes():
    actual = render_audi(summary(), locations={"cp": "Mapped Site"})
    expected = (
        "LOCACION,CONECTOR,FECHA INICIO,FECHA FINAL,WH INICIO,WH FINAL,"
        "WH USADOS,% INICIAL,% FINAL,RAZON FINAL\r\n"
        "Mapped Site,1,2026-10-04 12:00:00,"
        "2026-10-04 12:30:00,100,350,250,20,60,Normal\r\n"
    )
    assert actual == expected


def test_shared_header_and_missing_cell_contract():
    report = summary()
    report["transactions"][0]["soc_start"] = None
    rows = list(csv.reader(io.StringIO(render_audi_csv(report))))
    assert rows[1][7] == ""
