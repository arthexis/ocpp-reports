"""Non-destructive dashboard presentation contract for the next field increment."""
from pathlib import Path

from jinja2 import Environment

TEMPLATE = (Path(__file__).resolve().parents[1] /
            "src/ocpp_reports/templates/dashboard.html.j2")


def test_mobile_historical_labels_are_short_but_keep_freshness():
    source = TEMPLATE.read_text()
    Environment().parse(source)
    assert ".connector-mobile-text{display:none}" in source
    assert ".connector-desktop-text{display:none}" in source
    assert 'class="connector-mobile-text">Antiguo</span>' in source
    assert 'class="connector-mobile-text">Sin fecha</span>' in source
    assert '"antiguo" if c.stale else ("sin fecha" if c.status_time_unknown else "observado")' in source
    assert "c.stale or c.status_time_unknown %}status--historic" in source


def test_scroll_hint_appears_only_for_overflowing_desktop_table():
    source = TEMPLATE.read_text()
    assert 'id="connector-scroll-hint" hidden' in source
    assert 'aria-describedby="connector-scroll-hint"' in source
    assert "connectorRegion.scrollWidth > connectorRegion.clientWidth + 2" in source
    assert "window.innerWidth > 520 && overflows && !atEnd" in source
    assert 'connectorRegion.addEventListener("scroll", updateConnectorHint' in source
    assert 'window.addEventListener("resize", updateConnectorHint)' in source
