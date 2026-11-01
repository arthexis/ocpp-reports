"""Human-readable release inventory remains equivalent to --json."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_prune.py"
spec = importlib.util.spec_from_file_location("release_prune_output", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PruneOutputTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "schema": "ocpp/owner-prune-preview/v2",
            "owner": "csms", "root": "/tmp/install", "status": "blocked",
            "dry_run": True, "deletion_enabled": False,
            "protected": [
                {"name": "a" * 12, "reasons": ["current", "service"], "deletable": False}
            ],
            "unverified": [
                {"name": "b" * 12, "reasons": [], "deletable": False}
            ],
            "legacy": [],
            "errors": ["process inventory incomplete"],
        }

    def run_cli(self, *options):
        output = io.StringIO()
        with patch.object(module, "preview", return_value=self.report):
            with contextlib.redirect_stdout(output):
                code = module.main(["--owner", "csms", "--root", "/tmp/install", *options])
        return code, output.getvalue()

    def test_default_human_output_mentions_blockers_and_protected_releases(self):
        code, output = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Protected releases", output)
        self.assertIn("Unverified releases", output)
        self.assertIn("a" * 12, output)
        self.assertIn("current, service", output)
        self.assertIn("process inventory incomplete", output)
        self.assertIn("No releases deleted", output)
        self.assertNotIn('"protected":', output)

    def test_json_output_retains_unmodified_structured_contract(self):
        code, output = self.run_cli("--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), self.report)

    def test_dry_run_and_json_can_be_combined(self):
        code, output = self.run_cli("--dry-run", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), self.report)


if __name__ == "__main__":
    unittest.main()
