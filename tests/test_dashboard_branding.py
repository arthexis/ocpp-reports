"""Instance-scoped optional header branding."""
from pathlib import Path
import pytest
from ocpp_reports.web import create_app
from ocpp_reports.dashboard_login import route_permission


def test_template_keeps_text_for_unbranded_dashboard(dashboard_source):
    header = dashboard_source.split('<header>', 1)[1].split('</header>', 1)[0]
    assert '{% if brand_image_url %}' in header
    assert '{% else %}<h1>{{ title }}</h1>{% endif %}' in header


def test_logo_falls_back_when_asset_fails(dashboard_source):
    assert 'onerror="this.hidden=true;this.nextElementSibling.hidden=false"' in dashboard_source
    assert '<h1 hidden>{{ title }}</h1>' in dashboard_source
    assert 'header [hidden]{display:none!important}' in dashboard_source
    assert 'max-height:56px' in dashboard_source


def test_brand_url_validation(tmp_path):
    create_app(tmp_path / 'nonexistent.sqlite', brand_image_url='/assets/gelectriic.svg')
    create_app(tmp_path / 'nonexistent.sqlite', brand_image_url='https://example.com/logo.png')
    for url in ('javascript:alert(1)', '//evil.example/logo.png', 'http://example.com/logo.png',
                '/assets/logo.png?wrong=1', 'https://evil.example@wrong.example/logo.png'):
        with pytest.raises(ValueError):
            create_app(tmp_path / 'nonexistent.sqlite', brand_image_url=url)


def test_embedded_gelectriic_asset_has_transparent_png():
    logo = Path(__file__).resolve().parents[1] / 'src/ocpp_reports/assets/gelectriic.svg'
    source = logo.read_text()
    assert 'data:image/png;base64,' in source
    assert 'viewBox="0 0 58 57"' in source
    assert route_permission('/assets/gelectriic.svg', 'GET') is not None


def test_default_branding_is_customer_hostname_only():
    scope = (Path(__file__).resolve().parents[1] / 'ansible/templates/ocpp-reports-dashboard-scope.conf.j2').read_text()
    assert "item.hostname == 'customer.gelectriic.com'" in scope
    assert 'item.brand_image_url | default(' in scope
