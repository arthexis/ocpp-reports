"""Keep local ingestion attached to subsequent CSMS starts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_csms_start_pulls_in_ingest_after_clean_dependency_stop():
    ingest = (ROOT / "ansible/templates/ocpp-reports-ingest.service.j2").read_text()
    reader = (ROOT / "ansible/templates/ocpp-reports-csms-reader.service.j2").read_text()
    tasks = (ROOT / "ansible/tasks/local-ingest.yml").read_text()

    assert "WantedBy=multi-user.target ocpp-csms.service" in ingest
    assert "Requires=ocpp-reports-csms-reader.service" in ingest
    assert "After=network.target ocpp-reports-csms-reader.service" in ingest
    assert "Requires=ocpp-csms.service" in reader
    assert "After=ocpp-csms.service" in reader
    assert "dest: /etc/systemd/system/ocpp-csms.service.wants/ocpp-reports-ingest.service" in tasks
    assert "src: /etc/systemd/system/ocpp-reports-ingest.service" in tasks
    assert "reports_local_csms_start_link.changed" in tasks


def test_explicit_opt_out_reconciles_ingest_without_affecting_omitted_setting():
    playbook = (ROOT / "ansible/playbook.yml").read_text()
    disabled = (ROOT / "ansible/tasks/local-ingest-disabled.yml").read_text()
    for role in ("watchtower", "control"):
        tasks = (ROOT / f"ansible/roles/{role}/tasks/main.yml").read_text()
        assert "tasks/local-ingest-disabled.yml" in tasks
        assert "when: reports_local_ingest_explicitly_disabled | bool" in tasks
        assert "when: reports_local_ingest_enabled | bool" in tasks
    assert "reports_local_ingest_explicitly_disabled" in playbook
    assert "['false', '0', 'no', 'off']" in playbook
    assert "state: stopped" in disabled
    assert "enabled: false" in disabled
    assert "state: absent" in disabled
    assert "daemon_reload: true" in disabled


def test_local_ingest_deployment_default_matches_fast_follow_and_allows_override():
    playbook = (ROOT / "ansible/playbook.yml").read_text()
    service = (ROOT / "ansible/templates/ocpp-reports-ingest.service.j2").read_text()
    assert "reports_local_ingest_interval: \"{{ lookup('env', 'OCPP_REPORTS_LOCAL_INGEST_INTERVAL') | default('5', true) }}\"" in playbook
    assert "--interval {{ reports_local_ingest_interval }}" in service


def test_real_systemd_lifecycle_runner_is_wired_to_pr_ci():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    runner = (ROOT / "scripts/test_local_ingest_systemd.py").read_text()
    assert "sudo python3 scripts/test_local_ingest_systemd.py" in workflow
    assert 'systemctl("stop", csms)' in runner
    assert 'systemctl("start", csms)' in runner
    assert 'await_state(ingest, False)' in runner



def test_deploy_resolves_role_defaults_and_explicit_local_ingest_override():
    """Exercise the actual Ansible Jinja expression for each supported input."""
    from jinja2.nativetypes import NativeEnvironment
    playbook = (ROOT / "ansible/playbook.yml").read_text()
    expression = playbook.split("    reports_local_ingest_enabled: >-\n", 1)[1].split(
        "    reports_local_ingest_explicitly_disabled:", 1
    )[0].strip()
    env = NativeEnvironment()
    cases = [
        ("control", "", "", True),
        ("control", "", "disabled", True),
        ("control", "false", "enabled", False),
        ("watchtower", "", "enabled", True),
        ("watchtower", "", "enabled-runtime", True),
        ("watchtower", "", "disabled", False),
        ("watchtower", "", "masked", False),
        ("watchtower", "", "", False),
        ("watchtower", "true", "disabled", True),
        ("watchtower", "yes", "", True),
        ("watchtower", "1", "", True),
        ("watchtower", "on", "", True),
        ("watchtower", "false", "enabled", False),
        ("watchtower", "no", "enabled", False),
        ("watchtower", "0", "enabled", False),
        ("watchtower", "off", "enabled", False),
    ]
    for role, override, prior, expected in cases:
        actual = env.from_string(expression).render(
            reports_local_ingest_setting=override,
            reports_role=role,
            reports_existing_ingest_enabled={"stdout": prior},
        )
        assert actual is expected, (role, override, prior, actual)


def test_deploy_checks_enablement_and_reconciles_optional_roles():
    play = (ROOT / "ansible/playbook.yml").read_text()
    for role in ("watchtower", "control"):
        tasks = (ROOT / f"ansible/roles/{role}/tasks/main.yml").read_text()
        assert 'when: reports_local_ingest_enabled | bool' in tasks
        assert 'tasks/local-ingest-disabled.yml' in tasks
    assert 'systemctl' in play and 'is-enabled' in play
    assert 'reports_existing_ingest_enabled' in play
    assert "reports_local_ingest_setting in ['', 'true', '1', 'yes', 'on', 'false', '0', 'no', 'off']" in play
    assert 'failed_when: false' in play
    assert 'changed_when: false' in play
