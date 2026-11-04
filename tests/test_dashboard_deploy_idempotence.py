"""No-op Reports redeploy must preserve running service processes."""
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "ansible/tasks/dashboard-instances.yml"
SERVICE = ROOT / "ansible/tasks/service.yml"
ACTIVATION = ROOT / "ansible/tasks/activation.yml"


def dashboard_state(release_changed, scope_changes, base_changes, index):
    """Exercise the exact per-item Ansible Jinja decision, not a Python reimplementation."""
    import re

    source = DASHBOARD.read_text()
    expression = re.search(r"state: >-\n\s+({{ 'restarted'.*?}})", source, re.S)
    assert expression, "dashboard state decision missing"
    environment = Environment()
    environment.filters["bool"] = bool  # Ansible provides this filter; plain Jinja does not.
    template = environment.from_string(expression.group(1))
    return template.render(
        reports_release_changed=release_changed,
        reports_dashboard_scopes={"results": [{"changed": changed} for changed in scope_changes]},
        reports_dashboard_bases={"results": [{"changed": changed} for changed in base_changes]},
        dashboard_index=index,
    ).strip()


def test_noop_redeploy_does_not_restart_any_dashboard():
    for index in range(3):
        assert dashboard_state(False, [False] * 3, [False] * 3, index) == "started"


def test_one_dashboard_scope_change_restarts_only_that_instance():
    assert [dashboard_state(False, [False, True, False], [False] * 3, n)
            for n in range(3)] == ["started", "restarted", "started"]


def test_release_change_restarts_every_dashboard_once():
    assert [dashboard_state(True, [False] * 3, [False] * 3, n)
            for n in range(3)] == ["restarted"] * 3


def test_one_new_dashboard_base_restarts_only_its_instance():
    assert [dashboard_state(False, [False] * 3, [False, False, True], n)
            for n in range(3)] == ["started", "started", "restarted"]


def test_service_reload_and_release_activation_are_change_gated():
    service = SERVICE.read_text()
    dashboard = DASHBOARD.read_text()
    activation = ACTIVATION.read_text()
    assert 'daemon_reload: "{{ reports_unit.changed }}"' in service
    assert "'restarted' if (reports_unit.changed or reports_release_changed | bool)" in service
    assert 'daemon_reload: "{{ reports_dashboard_scopes.changed or reports_dashboard_bases.changed }}"' in dashboard
    assert "index_var: dashboard_index" in dashboard
    assert "reports_old_cli_target.stdout != reports_release ~ '/venv/bin/ocpp-reports'" in activation
