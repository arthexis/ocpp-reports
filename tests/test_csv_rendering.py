import csv
import io

import pytest
from ocpp_reports.rendering import render_report
from ocpp_reports.csv_rendering import COLUMNS


def sample(rows):
    return {"schema": "ocpp-reports/summary/v1", "source_id": "source",
            "period": {"since": "2026-10-01T00:00:00+00:00", "until": "2026-10-02T00:00:00+00:00", "timezone": "UTC"},
            "summary": {"transactions": len(rows), "known_energy_wh": 0, "missing_energy_transactions": 0, "completeness": "complete"},
            "transactions": rows, "revision": "r1"}


def parsed(rows):
    return list(csv.DictReader(io.StringIO(render_report(sample(rows), "csv"))))


def test_empty_report_has_stable_header():
    assert render_report(sample([]), "csv") == ",".join(COLUMNS) + "\n"


def test_missing_is_not_zero_and_order_is_stable():
    rows = [{"id": 2, "charger_id": "B", "state": "active", "energy_wh": None},
            {"id": 1, "charger_id": "A", "state": "completed", "energy_wh": 0}]
    result = parsed(rows)
    assert [r["id"] for r in result] == ["1", "2"]
    assert result[0]["energy_wh"] == "0"
    assert result[1]["energy_wh"] == ""
    assert result[1]["completeness"] == "provisional"
    assert render_report(sample(rows), "csv") == render_report(sample(list(reversed(rows))), "csv")


def test_unicode_quotes_newlines_and_formula_injection():
    row = {"id": 1, "charger_id": "CP, \"Árbol\"", "rfid": "=SUM(1,2)\nhello",
           "state": "completed", "energy_wh": 1000}
    result = parsed([row])[0]
    assert result["charger_id"] == 'CP, "Árbol"'
    assert result["rfid"].startswith("'=SUM")
    assert "\nhello" in result["rfid"]


def test_reject_duplicate_transaction_and_invalid_energy():
    row = {"id": 1, "charger_id": "CP", "energy_wh": 1}
    with pytest.raises(ValueError, match="Duplicate"):
        render_report(sample([row, row]), "csv")
    with pytest.raises(ValueError, match="energy_wh"):
        render_report(sample([{**row, "energy_wh": "unknown"}]), "csv")


def test_reject_wrong_schema_and_malformed_rows():
    with pytest.raises(ValueError):
        render_report(sample(["invalid"]), "csv")
    report = sample([])
    report["schema"] = "ocpp-csms/export/v1"
    with pytest.raises(ValueError):
        render_report(report, "csv")


def test_numeric_identity_sort_not_lexicographic():
    rows = [{"id": 10, "charger_id": "CP", "energy_wh": 1},
            {"id": 2, "charger_id": "CP", "energy_wh": 2}]
    assert [r["id"] for r in parsed(rows)] == ["2", "10"]


@pytest.mark.parametrize("value", ["=2+2", "  =2+2", "\\t+SUM(1)", "\\r@evil", "\\ufeff=1"])
def test_formula_prefixes_are_escaped(value):
    result = parsed([{"id": 1, "charger_id": "CP", "rfid": value}])[0]
    assert result["rfid"].startswith("'")


@pytest.mark.parametrize("energy", [-1, True, 1.5, "1000"])
def test_invalid_energy_is_rejected(energy):
    with pytest.raises(ValueError, match="energy_wh"):
        render_report(sample([{"id": 1, "charger_id": "CP", "energy_wh": energy}]), "csv")


@pytest.mark.parametrize("identity", [True, "2", 2.0])
def test_noninteger_transaction_identity_is_rejected(identity):
    with pytest.raises(ValueError, match="id"):
        render_report(sample([{"id": identity, "charger_id": "CP"}]), "csv")
