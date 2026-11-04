"""Smoke checks runnable even without optional FastAPI dependencies."""
import ast
from pathlib import Path


def test_login_module_syntax():
    root = Path(__file__).resolve().parents[1]
    for name in ("dashboard_login.py", "web.py"):
        ast.parse((root / "src" / "ocpp_reports" / name).read_text())


def test_nginx_basic_auth_retained_until_migration():
    root = Path(__file__).resolve().parents[1]
    template = (root / "ansible" / "templates" / "ocpp-reports-dashboard-instance.nginx.j2").read_text()
    assert 'auth_basic_user_file' in template
