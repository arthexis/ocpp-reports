"""Focused read-only prune contract; no service access or deletion."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_prune.py"
spec = importlib.util.spec_from_file_location("release_prune_tested", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class ReleasePrunePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "install"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        if "reports" in str(SCRIPT):
            (self.root / "role").write_text("terminal")
        self.owner = "csms" if "csms" in str(SCRIPT) else "reports"
        self.names = (("a" * 12, "b" * 12, "c" * 12) if self.owner == "csms"
                      else ("a" * 40 + "-pkg", "b" * 40 + "-pkg", "c" * 40 + "-pkg"))
        for name in self.names:
            (self.releases / name).mkdir()
        (self.root / "current").symlink_to(self.releases / self.names[0])
        (self.root / "previous").symlink_to(self.releases / self.names[1])

    def test_protect_current_previous_and_only_inventory_old(self):
        before = sorted(str(p) for p in self.base.rglob("*"))
        report = prune.preview(self.root, self.owner)
        self.assertEqual(report["status"], "candidates_ready")
        self.assertEqual([row["name"] for row in report["candidates"]], [self.names[2]])
        self.assertEqual({row["name"] for row in report["protected"]}, set(self.names[:2]))
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["deletion_enabled"])
        self.assertTrue(all(row["deletable"] for row in report["candidates"]))
        self.assertTrue(all(not row["deletable"] for row in report["protected"]))
        self.assertEqual(sorted(str(p) for p in self.base.rglob("*")), before)
        self.assertEqual(report, prune.preview(self.root, self.owner))

    def test_symlink_escape_and_malformed_release_fail_closed(self):
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(self.base)
        report = prune.preview(self.root, self.owner)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["candidates"], [])
        self.assertTrue(report["errors"])

    def test_broken_previous_blocks_candidates(self):
        (self.root / "previous").unlink()
        report = prune.preview(self.root, self.owner)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["candidates"])

    def test_apply_refused_with_no_mutations(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--owner", self.owner,
                                 "--root", str(self.root), "--apply"],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Reports releases are root-owned", result.stderr)
        self.assertTrue(all((self.releases / name).is_dir() for name in self.names))


if __name__ == "__main__":
    unittest.main()
