"""Regression tests for systemd CSMS process discovery."""
from pathlib import Path
from types import SimpleNamespace
import ocpp_reports.active_csms as active


def test_proc_cmdline_nul_delimited_explicit_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "schemas" / "8"
    data.mkdir(parents=True)
    launcher = tmp_path / "ocpp-csms"
    launcher.write_text("#!/usr/bin/python3\n")
    command = (str(launcher) + "\0--data-dir\0" + str(data) + "\0serve\0").encode()
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: command if str(self).endswith("/cmdline") else original(self))
    monkeypatch.setattr(active.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="123\n"))
    assert active.discover_active_csms() == (data, "/usr/bin/python3")


def test_default_data_dir_comes_from_active_csms(tmp_path, monkeypatch):
    data = tmp_path / "schemas" / "8"
    data.mkdir(parents=True)
    launcher = tmp_path / "ocpp-csms"
    launcher.write_text("#!/usr/bin/python3\n")
    command = (str(launcher) + "\0serve\0").encode()
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: command if str(self).endswith("/cmdline") else original(self))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="123\n" if argv[0] == "systemctl" else str(data) + "\n")
    monkeypatch.setattr(active.subprocess, "run", run)
    assert active.discover_active_csms() == (data, "/usr/bin/python3")
    assert any("cli_data_dir" in " ".join(argv) for argv in calls)
