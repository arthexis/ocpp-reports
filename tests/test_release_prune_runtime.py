"""Focused runtime-reference protection in the owner prune inventory."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_prune.py"
spec = importlib.util.spec_from_file_location("release_prune_runtime_tested", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


class RuntimeReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "install"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        self.owner = "csms" if "csms" in str(SCRIPT) else "reports"
        name = "a" * (12 if self.owner == "csms" else 40)
        self.old = name.replace("a", "b") + ("-pkg" if self.owner == "reports" else "")
        self.current = name + ("-pkg" if self.owner == "reports" else "")
        for item in (self.current, self.old):
            (self.releases / item).mkdir()
        (self.root / "current").symlink_to(self.releases / self.current)
        (self.root / "previous").symlink_to(self.releases / self.old)
        if self.owner == "reports":
            (self.root / "role").write_text("terminal")
        self.cli = Path(self.temp.name) / "bin" / ("ocpp-csms" if self.owner == "csms" else "ocpp-reports")
        self.cli.parent.mkdir(parents=True)
        executable = self.releases / self.current / "venv" / "bin" / self.cli.name
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\\nexit 0\\n")
        self.cli.symlink_to(executable)

    def test_protects_runtime_consumer(self):
        with patch.object(prune, "_runtime_references",
                          return_value=({self.old: {"process"}}, [])):
            report = prune.preview(self.root, self.owner, self.cli, inspect_runtime=True)
        self.assertEqual(report["status"], "candidates_ready")
        self.assertEqual(report["candidates"], [])
        self.assertIn("process", next(x["reasons"] for x in report["protected"]
                                       if x["name"] == self.old))
        self.assertFalse(report["deletion_enabled"])

    def test_incomplete_process_inventory_blocks_cleanup(self):
        with patch.object(prune, "_runtime_references",
                          return_value=({}, ["process inventory inaccessible"])):
            report = prune.preview(self.root, self.owner, self.cli, inspect_runtime=True)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["errors"])
        self.assertTrue(all(not row["deletable"] for row in report["protected"]))
        self.assertTrue((self.releases / self.old).exists())

    def test_unknown_runtime_release_fails_closed(self):
        name = "f" * (12 if self.owner == "csms" else 40)
        name += "-pkg" if self.owner == "reports" else ""
        with patch.object(prune, "_runtime_references",
                          return_value=({name: {"service"}}, [])):
            report = prune.preview(self.root, self.owner, self.cli, inspect_runtime=True)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["candidates"], [])
        self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()
