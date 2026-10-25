"""CSMS data discovery is tied to the active process, not a guessed directory."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "discover_csms_data_dir.py"
spec = importlib.util.spec_from_file_location("csms_data_discovery", SCRIPT)
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


@pytest.mark.parametrize(("argv", "expected"), [
    (["/opt/ocpp-csms", "--data-dir", "/home/arthe/ocpp-csms-data/schemas/8", "serve"],
     "/home/arthe/ocpp-csms-data/schemas/8"),
    (["/opt/ocpp-csms", "--data-dir=/srv/csms/schemas/9", "serve"],
     "/srv/csms/schemas/9"),
])
def test_reads_explicit_active_path(argv, expected):
    assert str(discovery.data_dir_from_argv(argv)) == expected


@pytest.mark.parametrize("argv", [
    ["/opt/ocpp-csms", "serve"],
    ["/opt/ocpp-csms", "--data-dir"],
    ["/opt/ocpp-csms", "--data-dir", "relative/path"],
    ["/opt/ocpp-csms", "--data-dir", "/tmp/../data"],
    ["/opt/ocpp-csms", "--data-dir", "/one", "--data-dir", "/two"],
])
def test_fails_closed_on_ambiguous_or_unsafe_paths(argv):
    with pytest.raises(ValueError):
        discovery.data_dir_from_argv(argv)


def test_uses_running_main_pid_and_nul_separated_argv():
    argv = b"/opt/ocpp-csms\0--data-dir\0/home/arthe/ocpp-csms-data/schemas/8\0serve\0"
    original = Path.read_bytes

    def read_bytes(path):
        if str(path) == "/proc/210364/cmdline":
            return argv
        return original(path)

    with patch.object(discovery.subprocess, "check_output", return_value="210364\n") as check:
        with patch.object(Path, "read_bytes", read_bytes):
            assert str(discovery.active_data_dir()) == "/home/arthe/ocpp-csms-data/schemas/8"
    assert check.call_args.args[0][:3] == ["systemctl", "show", "ocpp-csms.service"]


def test_refuses_inactive_service():
    with patch.object(discovery.subprocess, "check_output", return_value="0\n"):
        with pytest.raises(ValueError, match="MainPID"):
            discovery.active_data_dir()
