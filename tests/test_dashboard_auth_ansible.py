"""The proxy and service must switch authentication together per instance."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path(__file__).resolve().parents[1] / "ansible" / "templates"


def _render(name: str, instance: dict) -> str:
    environment = Environment(loader=FileSystemLoader(TEMPLATES))
    environment.filters["quote"] = str
    return environment.get_template(name).render(
        item=instance, reports_cli_link="/usr/local/bin/ocpp-reports",
    )


def test_dashboard_auth_staging_and_cutover():
    instance = {
        "name": "dashboard-tenant-a", "hostname": "tenant-a.example.test", "port": 8082,
        "title": "Tenant A", "htpasswd": "/etc/ocpp-reports/tenant-a.htpasswd",
        "source_ids": ["tenant-a"], "auth_tenant": "tenant-a",
    }
    service = _render("ocpp-reports-dashboard-scope.conf.j2", instance)
    assert 'OCPP_REPORTS_AUTH_TENANT=tenant-a' in service
    assert 'OCPP_REPORTS_AUTH_DATABASE=/var/lib/ocpp-reports/dashboard-auth.sqlite' in service
    assert "--source-id tenant-a" in service

    proxy = _render("ocpp-reports-dashboard-instance.nginx.j2", instance)
    assert "auth_basic_user_file /etc/ocpp-reports/tenant-a.htpasswd;" in proxy
    instance["auth_mode"] = "integrated"
    proxy = _render("ocpp-reports-dashboard-instance.nginx.j2", instance)
    assert "auth_basic" not in proxy
    assert "proxy_set_header X-Forwarded-Proto https;" in proxy


def test_legacy_instance_keeps_basic_auth_without_login_environment():
    instance = {
        "name": "dashboard", "hostname": "legacy.example.com", "port": 8080,
        "title": "Legacy", "htpasswd": "/etc/ocpp-reports/dashboard.htpasswd",
        "source_ids": "*",
    }
    assert "OCPP_REPORTS_AUTH_" not in _render("ocpp-reports-dashboard-scope.conf.j2", instance)
    assert "auth_basic_user_file" in _render("ocpp-reports-dashboard-instance.nginx.j2", instance)
