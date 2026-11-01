"""Privilege boundary and compact Reports pruning display tests."""
import contextlib
import fcntl
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_prune.py"
spec = importlib.util.spec_from_file_location("release_prune_elevation", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class ReportsPruneElevationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.lock = self.home / ".local/state/ocpp-reports/deploy.lock"
        self.lock.parent.mkdir(parents=True)
        self.lock.touch()

    def test_elevated_apply_requires_locked_owner_deploy(self):
        with patch.object(prune.os, "geteuid", return_value=0), \
             patch.object(prune.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_dir=str(self.home))), \
             patch.dict(prune.os.environ, {"SUDO_USER": "arthe"}):
            with self.assertRaisesRegex(ValueError, "held deploy.sh owner lock"):
                prune._require_owner_lock("reports", elevated=True)
            with self.lock.open("rb") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                prune._require_owner_lock("reports", elevated=True)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            with self.assertRaisesRegex(ValueError, "held deploy.sh owner lock"):
                prune._require_owner_lock("reports", elevated=True)

    def test_elevated_apply_rejects_non_root_or_missing_sudo_user(self):
        with patch.object(prune.os, "geteuid", return_value=1000):
            with self.assertRaises(ValueError):
                prune._require_owner_lock("reports", elevated=True)
        with patch.object(prune.os, "geteuid", return_value=0), \
             patch.dict(prune.os.environ, {"SUDO_USER": ""}):
            with self.assertRaises(ValueError):
                prune._require_owner_lock("reports", elevated=True)

    def test_brief_legacy_marker_and_apply_heading(self):
        legacy = "a" * 40
        report = {
            "owner": "reports", "root": "/opt/ocpp-reports",
            "status": "candidates_ready", "protected": [],
            "candidates": [{"name": legacy, "reasons": ["legacy"]}],
            "legacy": [], "errors": [],
        }
        human = prune.format_report(report)
        self.assertIn(legacy + "  (legacy)", human)
        self.assertNotIn("SHA-only release; not current", human)
        receipt = {
            "owner": "reports", "root": "/opt/ocpp-reports",
            "status": "completed", "protected": [],
            "deleted": [legacy], "skipped": [], "errors": [],
        }
        applied = prune.format_report(receipt)
        self.assertIn("release retention — apply", applied)
        self.assertNotIn("read-only", applied)
        self.assertIn("Deleted 1 release(s)", applied)


if __name__ == "__main__":
    unittest.main()
