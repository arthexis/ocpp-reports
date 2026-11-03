"""Presentation-only regressions for the combined dashboard."""
from pathlib import Path

from jinja2 import Environment

TEMPLATE = (Path(__file__).resolve().parents[1] /
            "src/ocpp_reports/templates/dashboard.html.j2")


def test_stale_observations_never_look_live():
    source = TEMPLATE.read_text()
    assert "c.stale or c.status_time_unknown %}status--historic" in source
    assert "Último: " in source
    assert "último registro" in source
    assert ".status--historic" in source
    Environment().parse(source)


def test_desktop_table_scrolls_within_panel_and_mobile_is_compact():
    source = TEMPLATE.read_text()
    assert ".connectors-panel .scroll{width:100%;max-width:100%;min-width:0;overflow-x:auto" in source
    assert ".connectors-panel table{min-width:1140px}" in source
    assert ".connectors-panel .transactions-heading{gap:.2rem;margin-bottom:.35rem}" in source
    assert ".connectors-panel tbody tr{padding:.4rem;margin-bottom:.45rem}" in source


def test_summary_count_meaning_is_visible_without_help():
    source = TEMPLATE.read_text()
    assert 'class="counts-legend">Cifras: estaciones (conectores individuales)' in source
    assert "Solo lectura · Actualiza cada 15 s" in source


def test_admin_timezone_inventory_example_is_explicit():
    doc = (Path(__file__).resolve().parents[1] / "docs/dashboard.md").read_text()
    assert '"timezones": ["America/Monterrey", "UTC"]' in doc
