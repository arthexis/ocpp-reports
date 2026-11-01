"""Regression: unrelated root-owned procfs entries must not block owner prune."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_prune.py"
spec = importlib.util.spec_from_file_location("release_prune_scoped", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class ScopedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        self.root = self.tmp / "install"
        (self.root / "releases").mkdir(parents=True)
        self.owner = "csms" if "csms" in str(SCRIPT) else "reports"
        self.name = ("a" * 12 if self.owner == "csms" else "a" * 40 + "-pkg")

    def process(self, pid, uid, cgroup, *, referenced=False):
        p = self.proc / str(pid)
        p.mkdir()
        (p / "status").write_text("Name:\ttest\nUid:\t%d\t%d\t%d\t%d\n" % ((uid,) * 4))
        (p / "cgroup").write_text("0::" + cgroup + "\n")
        if referenced:
            (p / "cmdline").write_bytes((str(self.root / "releases" / self.name) + "/venv/bin/python").encode())
            (p / "maps").write_bytes(b"")
            (p / "cwd").symlink_to(self.root)
            (p / "exe").symlink_to(self.root)
        # Deliberately omit cmdline/maps/cwd/exe for irrelevant foreign PIDs.
        return p

    def systemctl(self, argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0,
            "LoadState=loaded\nMainPID=300\nControlGroup=/system.slice/ocpp-csms.service\n"
            "ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 ; }\n"
            if self.owner == "csms" else
            "LoadState=loaded\nMainPID=300\nControlGroup=/system.slice/ocpp-reports-ingest.service\n"
            "ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 ; }\n",
            "")

    def test_ignores_unrelated_foreign_pid_and_protects_owner(self):
        self.process(101, os.geteuid() + 1, "/system.slice/unrelated.service")
        self.process(102, os.geteuid(), "/user.slice/user-1000.slice", referenced=True)
        with patch.object(prune.subprocess, "run", side_effect=self.systemctl):
            refs, errors = prune._runtime_references(self.root, self.owner, proc=self.proc)
        self.assertEqual(errors, [])
        self.assertIn("process", refs[self.name])

    def test_inaccessible_managed_service_process_fails_closed(self):
        self.process(101, os.geteuid() + 1, "/system.slice/unrelated.service")
        # PID matches the monitored service's MainPID; missing maps is an
        # incomplete observation, not a reason to silently allow deletion.
        self.process(300, os.geteuid() + 1, "/system.slice/ocpp-csms.service"
                     if self.owner == "csms" else "/system.slice/ocpp-reports-ingest.service")
        with patch.object(prune.subprocess, "run", side_effect=self.systemctl):
            _, errors = prune._runtime_references(self.root, self.owner, proc=self.proc)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
