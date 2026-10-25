"""Unmarked release recovery must be explicit and fail closed."""
import os
from pathlib import Path
import subprocess


DEPLOY = Path(__file__).resolve().parents[1] / "deploy.sh"


def run(tmp_path, *, current_target="valid", role=True):
    root = tmp_path / "opt" / "ocpp-reports"
    release = root / "releases" / "abc123-pkg"
    exe = release / "venv" / "bin" / "ocpp-reports"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    current = root / "current"
    current.symlink_to(release if current_target == "valid" else tmp_path / "unmanaged")
    cli = tmp_path / "ocpp-reports"
    cli.symlink_to(current / "venv" / "bin" / "ocpp-reports")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ansible = bin_dir / "ansible-playbook"
    ansible.write_text("#!/bin/sh\nexit 0\n")
    ansible.chmod(0o755)
    env = dict(os.environ, OCPP_REPORTS_INSTALL_ROOT=str(root),
               OCPP_REPORTS_CLI_LINK=str(cli),
               OCPP_REPORTS_LOCAL_INGEST_ENABLED="true",
               PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    return subprocess.run(["sh", str(DEPLOY)] + (["--satellite"] if role else []),
                          env=env, text=True, capture_output=True)


def test_explicit_satellite_resume_accepts_installer_release(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_without_role_cannot_adopt_unmarked_active_release(tmp_path):
    result = run(tmp_path, role=False)
    assert result.returncode == 2
    assert "explicit role" in result.stderr


def test_refuses_unmanaged_current_pointer(tmp_path):
    result = run(tmp_path, current_target="unmanaged")
    assert result.returncode == 2
    assert "Unrecognized Reports current pointer" in result.stderr
