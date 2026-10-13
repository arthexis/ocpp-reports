"""Shared summary/v1 contract applies equally to all output formats."""
from copy import deepcopy

import pytest

from ocpp_reports.rendering import render_report

FORMATS = ("html", "text", "csv")


def valid():
    return {
        "schema": "ocpp-reports/summary/v1", "source_id": "cp-source",
        "period": {"since": "2026-10-01T00:00:00+00:00",
                   "until": "2026-10-02T00:00:00+00:00", "timezone": "UTC"},
        "summary": {"transactions": 2, "known_energy_wh": 0,
                    "missing_energy_transactions": 1, "completeness": "partial"},
        "transactions": [
            {"id": 1, "charger_id": "CP", "connector_id": 1, "state": "completed", "energy_wh": 0},
            {"id": 2, "charger_id": "CP", "connector_id": None, "state": "active", "energy_wh": None},
        ],
        "revision": "r1",
    }


@pytest.mark.parametrize("format", FORMATS)
def test_missing_and_zero_preserved(format):
    report = valid()
    assert render_report(report, format) == render_report(deepcopy(report), format)
    if format == "csv":
        import csv
        import io
        rows = list(csv.DictReader(io.StringIO(render_report(report, format))))
        assert [r["energy_wh"] for r in rows] == ["0", ""]


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("mutation,match", [
    (lambda r: r["transactions"].append(deepcopy(r["transactions"][0])), "Duplicate"),
    (lambda r: r["transactions"][0].update(energy_wh=-1), "energy_wh"),
    (lambda r: r["transactions"][0].update(energy_wh="0"), "energy_wh"),
    (lambda r: r["transactions"][0].update(started_at="2026-10-01"), "started_at"),
    (lambda r: r["period"].update(until="2026-09-30T00:00:00Z"), "period"),
    (lambda r: r["summary"].update(known_energy_wh=None), "known_energy_wh"),
    (lambda r: r["transactions"][0].update(rfid={"bad": "value"}), "rfid"),
])
def test_invalid_data_rejected_consistently(format, mutation, match):
    report = valid()
    mutation(report)
    if match == "Duplicate":
        report["summary"]["transactions"] = len(report["transactions"])
    with pytest.raises(ValueError, match=match):
        render_report(report, format)


@pytest.mark.parametrize("format", FORMATS)
def test_transaction_count_mismatch_rejected(format):
    report = valid()
    report["summary"]["transactions"] = 99
    with pytest.raises(ValueError, match="count mismatch"):
        render_report(report, format)
