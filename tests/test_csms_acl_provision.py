"""Read-only ACL provisioning regression tests."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision_csms_read_access.py"
spec = importlib.util.spec_from_file_location("provision_csms_read_access", SCRIPT)
import sys
sys.path.insert(0, str(SCRIPT.parent))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_grants_traversal_then_read_only_tree(tmp_path):
    root = tmp_path / "csms"
    root.mkdir()
    folder = root / "schemas"
    folder.mkdir()
    file = folder / "evidence.sqlite3"
    file.write_bytes(b"test")
    exe = root / "ocpp-csms"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    actions = []

    def record(path, entry, *, default=False):
        actions.append((Path(path), entry, default))

    with patch.object(module, "acl", record):
        module.grant_tree(root, "ocpp-reports", future=True)
    assert (root.parent, "u:ocpp-reports:--x", False) in actions
    assert (folder, "u:ocpp-reports:r-x", True) in actions
    assert (file, "u:ocpp-reports:r--", False) in actions
    assert (exe, "u:ocpp-reports:r-x", False) in actions
    assert all("rw" not in entry for _, entry, _ in actions)


def test_release_tree_does_not_set_inherited_acl(tmp_path):
    root = tmp_path / "release"
    root.mkdir()
    actions = []
    with patch.object(module, "acl", lambda p, e, default=False: actions.append((p,e,default))):
        module.grant_tree(root, "ocpp-reports", future=False)
    assert actions
    assert not any(default for _, _, default in actions)


def test_launcher_detection_with_python_argv0(tmp_path):
    release = tmp_path / "releases" / "b04d6888dfb4"
    script = release / "venv" / "bin" / "ocpp-csms"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    assert module.active_launcher([
        str(release / "venv" / "bin" / "python"),
        str(script),
        "--data-dir",
        "/home/arthe/ocpp-csms-data/schemas/8",
        "serve",
    ]) == script


def test_launcher_detection_with_direct_argv0(tmp_path):
    script = tmp_path / "releases" / "b04d6888dfb4" / "venv" / "bin" / "ocpp-csms"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    assert module.active_launcher([str(script), "serve"]) == script
