from ocpp_reports.rendering import render_report
import pytest


def report(completeness="partial"):
    return {
        "schema": "ocpp-reports/summary/v1",
        "source_id": "<charger&1>",
        "period": {"since": "2026-10-01T00:00:00+00:00", "until": "2026-10-02T00:00:00+00:00", "timezone": "UTC"},
        "summary": {"transactions": 2, "known_energy_wh": 1500, "missing_energy_transactions": 1, "completeness": completeness},
        "transactions": [
            {"id": 1, "charger_id": "<script>alert(1)</script>", "connector_id": 1, "state": "completed", "energy_wh": 1500},
            {"id": 2, "charger_id": "CP2", "connector_id": None, "state": "active", "energy_wh": None},
        ],
        "revision": "abc123",
    }


def test_html_escapes_and_marks_provisional():
    html = render_report(report(), "html")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "PROVISIONAL" in html
    assert "Missing" in html
    assert "1.500 kWh" in html


def test_text_report():
    text = render_report(report(), "text")
    assert "PROVISIONAL" in text
    assert "Transaction 2" in text
    assert "abc123" in text


def test_complete_report_not_marked_provisional():
    data = report("complete")
    data["summary"]["missing_energy_transactions"] = 0
    assert "PROVISIONAL" not in render_report(data)


def test_reject_unsupported_schema_and_format():
    data = report()
    with pytest.raises(ValueError):
        render_report(data, "pdf")
    data["schema"] = "ocpp-csms/export/v1"
    with pytest.raises(ValueError):
        render_report(data)
