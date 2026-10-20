"""Validate separate customer dashboard and collector proxy contracts."""
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "ansible/templates"


def test_dashboard_is_basic_auth_only():
    conf = (TEMPLATES / "ocpp-reports-dashboard.nginx.j2").read_text()
    assert "auth_basic_user_file" in conf
    assert "proxy_pass http://127.0.0.1:8080;" in conf
    assert "/api/replication" not in conf
    assert "listen 443 ssl;" in conf


def test_collector_only_exposes_replication():
    conf = (TEMPLATES / "ocpp-reports-collector.nginx.j2").read_text()
    assert "location = /api/replication" in conf
    assert "limit_except POST { deny all; }" in conf
    assert "proxy_pass http://127.0.0.1:8081;" in conf
    assert "proxy_set_header Authorization $http_authorization;" in conf
    assert "location / { return 404; }" in conf
    assert "auth_basic" not in conf
    assert "listen 443 ssl;" in conf


def test_both_hosts_redirect_http_to_https():
    for name in ("dashboard", "collector"):
        conf = (TEMPLATES / f"ocpp-reports-{name}.nginx.j2").read_text()
        assert "return 301 https://$host$request_uri;" in conf
        assert "ssl_certificate_key " in conf
