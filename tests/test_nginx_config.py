"""Static security contract for the Nginx reverse proxy template."""
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "ansible/templates/ocpp-reports-nginx.conf.j2"
)


def test_dashboard_requires_basic_auth_and_replication_uses_bearer():
    conf = TEMPLATE.read_text(encoding="utf-8")
    replication = conf.split("location = /api/replication {", 1)[1].split("\n    }", 1)[0]
    dashboard = conf.split("location / {", 1)[1].split("\n    }", 1)[0]
    assert "limit_except POST { deny all; }" in replication
    assert "proxy_pass http://127.0.0.1:8081;" in replication
    assert "proxy_set_header Authorization $http_authorization;" in replication
    assert "auth_basic" not in replication
    assert 'auth_basic "OCPP Reports";' in dashboard
    assert "auth_basic_user_file" in dashboard
    assert "proxy_pass http://127.0.0.1:8080;" in dashboard


def test_tls_is_required_for_exposed_routes():
    conf = TEMPLATE.read_text(encoding="utf-8")
    assert "listen 443 ssl;" in conf
    assert "ssl_certificate " in conf
    assert "ssl_certificate_key " in conf
    assert "return 301 https://$host$request_uri;" in conf
