"""Legacy Reports SHA-only releases are disposable owner release directories."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_prune.py"
spec = importlib.util.spec_from_file_location("reports_legacy_retention", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class LegacyReleasePruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "reports"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        (self.root / "role").write_text("terminal")
        self.current = "a" * 40 + "-pkg"
        self.previous = "b" * 40 + "-pkg"
        self.pkg_old = "c" * 40 + "-pkg"
        self.legacy_old = "d" * 40
        for name in (self.current, self.previous, self.pkg_old, self.legacy_old):
            release = self.releases / name
            release.mkdir()
            (release / "marker").write_text(name)
        (self.root / "current").symlink_to(self.releases / self.current)
        (self.root / "previous").symlink_to(self.releases / self.previous)
        self.cli = Path(self.temp.name) / "ocpp-reports"
        exe = self.releases / self.current / "venv" / "bin" / "ocpp-reports"
        exe.parent.mkdir(parents=True)
        exe.write_text("cli")
        self.cli.symlink_to(exe)

    def test_preview_lists_both_historical_formats(self):
        report = prune.preview(self.root, "reports", self.cli)
        self.assertEqual(report["status"], "candidates_ready")
        self.assertEqual({e["name"] for e in report["candidates"]},
                         {self.pkg_old, self.legacy_old})
        self.assertEqual(report["legacy"], [])
        self.assertEqual({e["name"] for e in report["protected"]},
                         {self.current, self.previous})
        self.assertFalse(report["deletion_enabled"])

    def test_apply_removes_both_formats_preserving_pointers_and_data(self):
        data = self.root / "reports.sqlite3"
        data.write_text("persistent")
        receipt = prune.apply_prune(self.root, "reports", self.cli)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(set(receipt["deleted"]), {self.pkg_old, self.legacy_old})
        self.assertTrue((self.releases / self.current).exists())
        self.assertTrue((self.releases / self.previous).exists())
        self.assertEqual(data.read_text(), "persistent")

    def test_protected_legacy_pointer_is_never_deleted(self):
        (self.root / "previous").unlink()
        (self.root / "previous").symlink_to(self.releases / self.legacy_old)
        receipt = prune.apply_prune(self.root, "reports", self.cli)
        self.assertNotIn(self.legacy_old, receipt["deleted"])
        self.assertTrue((self.releases / self.legacy_old).exists())

    def test_unsafe_or_unknown_names_are_not_deleted(self):
        foreign = self.releases / "unmanaged"
        foreign.mkdir()
        outside = Path(self.temp.name) / "external"
        outside.mkdir()
        (self.releases / ("e" * 40)).symlink_to(outside, target_is_directory=True)
        receipt = prune.apply_prune(self.root, "reports", self.cli)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["deleted"], [])
        self.assertTrue(outside.exists())
        self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
