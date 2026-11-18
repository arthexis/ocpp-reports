"""Instance-scoped optional header branding."""
import pytest
from ocpp_reports.web import create_app


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
    create_app(tmp_path / 'nonexistent.sqlite', brand_image_url='/assets/brand.png')
    create_app(tmp_path / 'nonexistent.sqlite', brand_image_url='https://example.com/logo.png')
    for url in ('javascript:alert(1)', '//evil.example/logo.png', 'http://example.com/logo.png',
                '/assets/logo.png?wrong=1', 'https://evil.example@wrong.example/logo.png'):
        with pytest.raises(ValueError):
            create_app(tmp_path / 'nonexistent.sqlite', brand_image_url=url)



def test_branding_requires_explicit_instance_configuration():
    from pathlib import Path
    scope = (Path(__file__).resolve().parents[1] / 'ansible/templates/ocpp-reports-dashboard-scope.conf.j2').read_text()
    assert "item.brand_image_url | default('')" in scope

def test_exact_png_is_packaged_and_route_authorized():
    from pathlib import Path
    import hashlib
    from ocpp_reports.dashboard_login import route_permission
    asset = Path(__file__).resolve().parents[1] / 'src/ocpp_reports/assets/brand.png'
    raw = asset.read_bytes()
    assert len(raw) == 20546
    assert hashlib.sha256(raw).hexdigest() == '0d98df804e5f18ee50b6f4d45b7456dad1aac949d528191c47cf774cbc6973a8'
    assert raw[:8] == bytes.fromhex('89504e470d0a1a0a')
    assert route_permission('/assets/brand.png', 'GET') is not None

def test_white_wordmark_variant_is_exact_and_preserves_original():
    from pathlib import Path
    import hashlib
    from ocpp_reports.dashboard_login import route_permission
    assets = Path(__file__).resolve().parents[1] / "src/ocpp_reports/assets"
    original = (assets / "brand.png").read_bytes()
    variant = (assets / "brand-light.png").read_bytes()
    assert hashlib.sha256(original).hexdigest() == "0d98df804e5f18ee50b6f4d45b7456dad1aac949d528191c47cf774cbc6973a8"
    assert hashlib.sha256(variant).hexdigest() == "7fe9ce7218a5b3e3fb35d20715c14b47100ca5801932abed29ba31a169e6f28a"
    assert route_permission("/assets/brand-light.png", "GET") is not None
    # Byte-exact asset fingerprints also detect unintended recoloring of the icon or II.
    assert len(original) == 20546
    assert len(variant) == 12757
