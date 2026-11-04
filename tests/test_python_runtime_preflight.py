"""Preflight must select compatible Python before touching a Reports installation."""
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "ansible/python_preflight.py"
PREFLIGHT = runpy.run_path(str(HELPER), run_name="python_preflight_test")


def test_current_interpreter_can_be_selected_explicitly(monkeypatch):
    monkeypatch.setenv("OCPP_REPORTS_PYTHON", sys.executable)
    info = PREFLIGHT["select"](sys.executable)
    assert tuple(map(int, info["minor"].split("."))) >= (3, 11)
    assert Path(info["executable"]).exists()


def test_rejects_missing_override_instead_of_silently_falling_back():
    with pytest.raises(ValueError, match="not installed"):
        PREFLIGHT["select"]("/not/a/real/reports/python")


def test_auto_selection_skips_unsupported_python(monkeypatch):
    attempts = []
    def fake_inspect(name):
        attempts.append(name)
        if name == "python3":
            raise ValueError("Python 3.10 is unsupported")
        return {"executable": "/usr/bin/python3.13", "minor": "3.13"}
    monkeypatch.setitem(PREFLIGHT["select"].__globals__, "inspect", fake_inspect)
    result = PREFLIGHT["select"]()
    assert attempts == ["python3", "python3.13"]
    assert result["minor"] == "3.13"


def test_existing_venv_requires_compatible_minor(tmp_path):
    env = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert PREFLIGHT["check_existing"](env, minor)["minor"] == minor
    with pytest.raises(ValueError, match="do not reuse"):
        PREFLIGHT["check_existing"](env, "3.99")
    with pytest.raises(ValueError, match="missing bin/python"):
        PREFLIGHT["check_existing"](tmp_path / "absent", minor)


def test_preflight_happens_before_host_mutations():
    playbook = (ROOT / "ansible/playbook.yml").read_text()
    assert playbook.index("Select supported Reports Python") < playbook.index(
        "Prepare shared Reports installation")
    assert playbook.index("Select supported Reports Python") < playbook.index(
        "Inspect existing local ingestion enablement")
    assert "OCPP_REPORTS_PYTHON" in HELPER.read_text()


def test_release_and_reusable_venvs_share_selected_interpreter():
    tasks = (ROOT / "ansible/tasks/release.yml").read_text()
    assert tasks.count("{{ reports_python }} -m venv") == 3
    assert tasks.count("--check-venv") == 3 + 1
    assert tasks.count("--minor") == 3
    assert "-py{{ reports_python_minor | replace('.', '') }}" in tasks
    assert "build-venv-py{{ reports_python_minor | replace('.', '') }}" in tasks
    assert 'cmd: "python3 -m venv' not in tasks
    assert "when: not reports_existing_release.stat.exists" in tasks
