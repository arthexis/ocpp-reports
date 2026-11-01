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


def test_real_systemd_lifecycle_runner_is_wired_to_pr_ci():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    runner = (ROOT / "scripts/test_local_ingest_systemd.py").read_text()
    assert "sudo python3 scripts/test_local_ingest_systemd.py" in workflow
    assert 'systemctl("stop", csms)' in runner
    assert 'systemctl("start", csms)' in runner
    assert 'await_state(ingest, False)' in runner



def test_deploy_infers_previous_local_ingest_without_explicit_override():
    """Exercise the actual Ansible Jinja expression for each supported input."""
    from jinja2.nativetypes import NativeEnvironment
    import yaml

    play = yaml.safe_load((ROOT / "ansible/playbook.yml").read_text())[0]
    expression = play["vars"]["reports_local_ingest_enabled"]
    env = NativeEnvironment()
    cases = [
        ("", "enabled", True),
        ("", "enabled-runtime", True),
        ("", "disabled", False),
        ("", "masked", False),
        ("", "", False),
        ("true", "disabled", True),
        ("yes", "", True),
        ("1", "", True),
        ("on", "", True),
        ("false", "enabled", False),
        ("no", "enabled", False),
        ("0", "enabled", False),
        ("off", "enabled", False),
    ]
    for override, prior, expected in cases:
        actual = env.from_string(expression).render(
            reports_local_ingest_setting=override,
            reports_existing_ingest_enabled={"stdout": prior},
        )
        assert actual is expected, (override, prior, actual)


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
