"""Non-destructive dashboard presentation contract for the next field increment."""
from pathlib import Path

from jinja2 import Environment

TEMPLATE = (Path(__file__).resolve().parents[1] /
            "src/ocpp_reports/templates/dashboard.html.j2")


def test_mobile_historical_labels_are_short_but_keep_freshness():
    source = TEMPLATE.read_text()
    Environment().parse(source)
    assert "Último: " not in source
    assert "último registro" not in source
    assert "Estado antiguo</span>" not in source
    assert "Origen atrasado</span>" in source
    assert "Estado sin fecha</span>" in source
    assert 'data-label="Conectado"' in source
    assert 'c.connected is sameas true' in source
    assert "c.stale and state in ['available', 'finishing', 'charging']" in source


def test_scroll_hint_appears_only_for_overflowing_desktop_table():
    source = TEMPLATE.read_text()
    assert 'id="connector-scroll-hint" hidden>→ Más columnas' in source
    assert 'aria-describedby="connector-scroll-hint"' in source
    assert "connectorRegion.scrollWidth > connectorRegion.clientWidth + 2" in source
    assert "window.innerWidth > 520 && overflows && !atEnd" in source
    assert 'connectorRegion.addEventListener("scroll", updateConnectorHint' in source
    assert 'window.addEventListener("resize", updateConnectorHint)' in source
