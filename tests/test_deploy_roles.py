"""Deployment role selection regression tests (no Ansible installation required)."""
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy.sh"


def deploy(tmp_path, role=None, *, installed=False, marker=None):
    root = tmp_path / "install"
    root.mkdir(exist_ok=True)
    if installed:
        (root / "releases").mkdir(exist_ok=True)
    if marker is not None:
        (root / "role").write_text(marker)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    executable = fake_bin / "ansible-playbook"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
    executable.chmod(0o755)
    env = dict(os.environ, OCPP_REPORTS_INSTALL_ROOT=str(root),
               OCPP_REPORTS_CLI_LINK=str(tmp_path / "cli"),
               PATH=f"{fake_bin}:{os.environ['PATH']}")
    args = ["sh", str(SCRIPT)] + ([role] if role else [])
    return subprocess.run(args, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("role", ["terminal", "satellite", "watchtower", "control"])
def test_initial_explicit_role(tmp_path, role):
    result = deploy(tmp_path, "--" + role)
    assert result.returncode == 0
    assert "reports_role=" + role in result.stdout


def test_initial_default_terminal(tmp_path):
    result = deploy(tmp_path)
    assert result.returncode == 0
    assert "reports_role=terminal" in result.stdout


def test_reinstall_reuses_persisted_role(tmp_path):
    result = deploy(tmp_path, installed=True, marker="satellite\n")
    assert result.returncode == 0
    assert "reports_role=satellite" in result.stdout


def test_repeating_role_is_idempotent(tmp_path):
    result = deploy(tmp_path, "--watchtower", installed=True, marker="watchtower\n")
    assert result.returncode == 0
    assert "reports_role=watchtower" in result.stdout


def test_role_change_rejected(tmp_path):
    result = deploy(tmp_path, "--control", installed=True, marker="terminal\n")
    assert result.returncode != 0
    assert "uninstall" in result.stderr


def test_existing_install_without_role_cannot_be_adopted(tmp_path):
    result = deploy(tmp_path, "--satellite", installed=True)
    assert result.returncode != 0
    assert "lacks a role marker" in result.stderr


def test_invalid_marker_rejected(tmp_path):
    result = deploy(tmp_path, marker="unknown")
    assert result.returncode != 0
    assert "Invalid existing" in result.stderr
