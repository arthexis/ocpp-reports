"""Focused canonical deploy/prune lock contention test."""
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OWNER = "ocpp-csms" if "csms" in str(ROOT) else "ocpp-reports"


class PruneLockTests(unittest.TestCase):
    def test_canonical_prune_refuses_held_owner_lock(self):
        with tempfile.TemporaryDirectory() as home:
            lock_dir = Path(home) / ".local/state" / OWNER
            lock_dir.mkdir(parents=True)
            lock_file = lock_dir / "deploy.lock"
            with lock_file.open("w+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                env = os.environ.copy()
                env["HOME"] = home
                result = subprocess.run(
                    ["sh", str(ROOT / "deploy.sh"), "--prune", "--dry-run"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("holds the owner lock", result.stderr)


if __name__ == "__main__":
    unittest.main()
