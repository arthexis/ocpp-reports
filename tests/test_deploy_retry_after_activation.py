"""Behavioral deployment retry contract: activation does not establish convergence."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy.sh"


def test_failed_post_activation_retries_ansible_even_if_status_exits_zero(tmp_path):
    root = tmp_path / "reports"
    release = root / "releases" / "deadbeef-pkg"
    executable = release / "venv/bin/ocpp-reports"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho '{\"state\":\"unconfigured\"}'\n")
    executable.chmod(0o755)
    (root / "role").write_text("control\n")
    (root / "current").symlink_to(release)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ansible = fake_bin / "ansible-playbook"
    marker = tmp_path / "attempts"
    ansible.write_text(
        "#!/bin/sh\n"
        f"printf 'attempt\\n' >> '{marker}'\n"
        f"if [ \"$(wc -l < '{marker}')\" -eq 1 ]; then exit 42; fi\n"
        "exit 0\n"
    )
    ansible.chmod(0o755)
    env = dict(os.environ, OCPP_REPORTS_INSTALL_ROOT=str(root),
               OCPP_REPORTS_CLI_LINK=str(tmp_path / "unused-cli"),
               PATH=f"{fake_bin}:{os.environ['PATH']}")
    first = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert first.returncode == 42
    assert (root / "current").resolve() == release
    second = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert second.returncode == 0
    assert marker.read_text().splitlines() == ["attempt", "attempt"]
