"""Enforce vendor-neutral source/API surfaces and avoid branded regressions."""
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ocpp_reports"


def test_no_branded_identifiers_in_shipped_python_modules():
    # Construct terms to keep the guard itself free of brand references.
    names = ("au" + "di", "por" + "sche", "a" + "bb")
    expression = re.compile(r"(?<![A-Za-z])(?:" + "|".join(names) + r")(?![A-Za-z])", re.I)
    violations = []
    for path in SOURCE.rglob("*.py"):
        if expression.search(path.name) or expression.search(path.read_text(encoding="utf-8")):
            violations.append(str(path.relative_to(SOURCE)))
    assert not violations, f"Vendor-specific source modules: {violations}"


def test_generic_vendor_layout_and_removed_brand_aliases():
    from ocpp_reports.reports.builder import render
    report = {"schema": "ocpp-reports/summary/v1", "source_id": "customer",
              "period": {"since": "2026-10-01T00:00:00Z",
                         "until": "2026-11-01T00:00:00Z", "timezone": "UTC"},
              "summary": {"transactions": 0, "known_energy_wh": 0,
                          "missing_energy_transactions": 0, "completeness": "complete"},
              "transactions": []}
    assert "LOCACION" in render(report, "csv", profile="vendor")
    assert "LOCACION" in render(report, "vendor-csv")
    with pytest.raises(ValueError, match="vendor"):
        render(report, "csv", profile="au" + "di")


def test_ingress_guard_requires_configured_csms_hostname():
    text = (ROOT / "ansible/tasks/nginx.yml").read_text(encoding="utf-8")
    assert "reports_csms_hostname | length > 0" in text
    for name in ("dashboard", "collector", "diagnostics"):
        assert f"reports_{name}_hostname != reports_csms_hostname" in text
