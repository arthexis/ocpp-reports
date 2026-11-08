"""Offline tests for capture account metadata migration."""
import json
from pathlib import Path
import tempfile
import unittest

from migrate_accounts import migrate, prepare


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.accounts = root / 'accounts.json'
        self.metadata = root / 'metadata.json'
        self.credentials = {'sites': {'tenant-a': {'username': 'user', 'password': 'private'}}}
        self.scope = {'tenant-a': {'origin': 'https://tenant-a.example.test',
                                   'title': 'Tenant A', 'source_ids': ['source-a']}}
        for path, content in ((self.accounts, self.credentials), (self.metadata, self.scope)):
            path.write_text(json.dumps(content))
            path.chmod(0o600)

    def test_preview_is_read_only_and_redacted(self):
        before = self.accounts.read_bytes()
        self.assertEqual(migrate(self.accounts, self.metadata), 'change_required')
        self.assertEqual(self.accounts.read_bytes(), before)

    def test_apply_is_idempotent_and_preserves_secrets_and_mode(self):
        self.assertEqual(migrate(self.accounts, self.metadata, True), 'updated')
        data = json.loads(self.accounts.read_text())
        self.assertEqual(data['sites']['tenant-a']['password'], 'private')
        self.assertEqual(data['sites']['tenant-a']['source_ids'], ['source-a'])
        self.assertEqual(self.accounts.stat().st_mode & 0o777, 0o600)
        before = self.accounts.read_bytes()
        self.assertEqual(migrate(self.accounts, self.metadata, True), 'already_current')
        self.assertEqual(self.accounts.read_bytes(), before)

    def test_missing_tenant_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'tenant_mapping_mismatch'):
            prepare(self.credentials, {})

    def test_scope_conflict_fails_closed(self):
        original = {'sites': {'tenant-a': {**self.credentials['sites']['tenant-a'], 'source_ids': ['other']}}}
        with self.assertRaisesRegex(ValueError, 'existing_scope_conflict'):
            prepare(original, self.scope)

    def test_public_permissions_refused(self):
        self.metadata.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private_file_requirements_not_met'):
            migrate(self.accounts, self.metadata)


if __name__ == '__main__':
    unittest.main()
