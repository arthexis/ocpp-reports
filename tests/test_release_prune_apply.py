"""Focused bounded release deletion tests; only disposable temp directories."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_prune.py"
spec = importlib.util.spec_from_file_location("release_prune_apply", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class PruneApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "install"
        releases = self.root / "releases"
        releases.mkdir(parents=True)
        self.owner = "csms" if "csms" in str(SCRIPT) else "reports"
        names = ["a" * 12, "b" * 12, "c" * 12]
        if self.owner == "reports":
            names = [name * 3 + name[:4] + "-pkg" for name in names]
            (self.root / "role").write_text("terminal")
        self.current, self.previous, self.old = names
        for name in names:
            release = releases / name
            release.mkdir()
            (release / "marker").write_text(name)
        (self.root / "current").symlink_to(releases / self.current)
        (self.root / "previous").symlink_to(releases / self.previous)
        self.cli = Path(self.temp.name) / "bin" / "app"
        self.cli.parent.mkdir()
        active = releases / self.current / "venv" / "bin" / "app"
        active.parent.mkdir(parents=True)
        active.write_text("app")
        self.cli.symlink_to(active)

    def test_apply_deletes_only_old_release_and_is_idempotent(self):
        receipt = prune.apply_prune(self.root, self.owner, self.cli)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["deleted"], [self.old])
        self.assertFalse((self.root / "releases" / self.old).exists())
        self.assertTrue((self.root / "releases" / self.current).exists())
        self.assertTrue((self.root / "releases" / self.previous).exists())
        self.assertEqual(prune.apply_prune(self.root, self.owner, self.cli)["deleted"], [])

    def test_pointer_switch_before_remove_skips_candidate(self):
        original_preview = prune.preview
        calls = 0
        def changed(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / "previous").unlink()
                (self.root / "previous").symlink_to(self.root / "releases" / self.old)
            return original_preview(*args, **kwargs)
        with patch.object(prune, "preview", side_effect=changed):
            receipt = prune.apply_prune(self.root, self.owner, self.cli)
        self.assertEqual(receipt["deleted"], [])
        self.assertTrue((self.root / "releases" / self.old).exists())

    def test_path_escape_blocks_all_removal(self):
        (self.root / "previous").unlink()
        (self.root / "previous").symlink_to(Path(self.temp.name))
        receipt = prune.apply_prune(self.root, self.owner, self.cli)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["deleted"], [])
        self.assertTrue((self.root / "releases" / self.old).exists())

    def test_limit_blocks_bulk_deletion(self):
        receipt = prune.apply_prune(self.root, self.owner, self.cli, limit=0)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["deleted"], [])


if __name__ == "__main__":
    unittest.main()
