"""Publication must fail closed when Android or any capture is unavailable."""
import asyncio
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import capture
import shots


class TargetTests(unittest.TestCase):
    def test_numbers_urls_and_pull_requests(self):
        self.assertEqual(shots.target('108', 'arthexis/ocpp-reports'), ('arthexis/ocpp-reports', 108))
        self.assertEqual(shots.target('other/repo#7', 'unused/repo'), ('other/repo', 7))
        self.assertEqual(shots.target('https://github.com/other/repo/pull/9#discussion', 'unused/repo'), ('other/repo', 9))
        for invalid in ['0', '-1', 'https://example.com/other/repo/issues/9', 'https://github.com/other/repo/actions/9']:
            with self.subTest(target=invalid), self.assertRaises(shots.Blocked):
                shots.target(invalid, 'arthexis/ocpp-reports')


class PhoneTests(unittest.TestCase):
    def test_missing_unauthorized_and_multiple_devices_are_rejected(self):
        for listing in ['List of devices attached\n\n',
                        'List of devices attached\na unauthorized\n',
                        'List of devices attached\na device\nb device\n']:
            with self.subTest(listing=listing), patch.object(capture, 'adb', return_value=listing) as adb:
                with self.assertRaisesRegex(RuntimeError, 'android_not_connected_and_authorized'):
                    capture.device(None, wake=True)
                self.assertEqual(adb.call_count, 1)

    def test_locked_phone_is_never_woken_or_unlocked(self):
        for state, trust in [('showing=true', 'deviceLocked=0'),
                             ('isStatusBarKeyguard=true', 'deviceLocked=0'),
                             ('showing=false', 'deviceLocked=1')]:
            with self.subTest(state=state), patch.object(capture, 'adb', side_effect=[
                    'List of devices attached\na device\n', state, trust]) as adb:
                with self.assertRaisesRegex(RuntimeError, 'android_unlock_required'):
                    capture.device(None, wake=True)
                self.assertEqual(adb.call_count, 3)

    def test_wrong_foreground_display_is_rejected(self):
        with patch.object(capture, 'device'), patch.object(capture, 'adb', return_value='mCurrentFocus=StatusBar'):
            with self.assertRaisesRegex(RuntimeError, 'android_browser_not_foreground'):
                capture.verify_phone_display('a', 'https://tenant-a.example.test', 'Tenant A Dashboard')

    def test_wrong_display_url_stops_and_removes_temporary_ui_file(self):
        xml = ('<hierarchy><node resource-id="com.android.chrome:id/url_bar" '
               'text="wrong.example"/><node text="Tenant A Dashboard"/></hierarchy>')
        with patch.object(capture, 'device'), patch.object(capture, 'adb', side_effect=[
            'mCurrentFocus=Window com.android.chrome/Main', '', xml, ''
        ]) as adb:
            with self.assertRaisesRegex(RuntimeError, 'android_display_unverified'):
                capture.verify_phone_display('a', 'https://tenant-a.example.test', 'Tenant A Dashboard')
        self.assertEqual(adb.call_args.args[:3], ('shell', 'rm', '-f'))

    def test_successful_display_check_requires_url_and_title(self):
        xml = ('<hierarchy><node resource-id="com.android.chrome:id/url_bar" '
               'text="tenant-a.example.test"/><node text="Tenant A Dashboard"/></hierarchy>')
        with patch.object(capture, 'device'), patch.object(capture, 'adb', side_effect=[
            'mCurrentFocus=Window com.android.chrome/Main', '', xml, ''
        ]):
            capture.verify_phone_display('a', 'https://tenant-a.example.test', 'Tenant A Dashboard')


class WorkflowTests(unittest.TestCase):
    def args(self, root):
        return SimpleNamespace(target='108', repo='arthexis/ocpp-reports', serial=None,
                               output=Path(root), check=False)

    def test_unavailable_phone_stops_before_github_or_captures(self):
        with tempfile.TemporaryDirectory() as root, patch.object(shots.shutil, 'which', return_value='/bin/tool'), \
             patch.object(shots, 'phone', side_effect=shots.Blocked('unplugged')), \
             patch.object(shots, 'api') as api, patch.object(shots, 'collect') as collect, \
             patch.object(shots, 'publish') as publish, redirect_stdout(io.StringIO()):
            with self.assertRaises(shots.Blocked):
                shots.run(self.args(root))
            api.assert_not_called()
            collect.assert_not_called()
            publish.assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_failure_after_partial_capture_never_publishes(self):
        with tempfile.TemporaryDirectory() as root, patch.object(shots.shutil, 'which', return_value='/bin/tool'), \
             patch.object(shots, 'phone', return_value='a'), patch.object(shots.capture, 'site_config', return_value={'tenant-a': {}}), patch.object(shots.capture, 'integrated_account', return_value=('u', 'dummy')), \
             patch.object(shots, 'api', return_value={'html_url': 'https://github.com/arthexis/ocpp-reports/issues/108'}), \
             patch.object(shots, 'collect', new=AsyncMock(side_effect=shots.Blocked('phone disconnected'))), \
             patch.object(shots, 'publish') as publish, redirect_stdout(io.StringIO()):
            with self.assertRaises(shots.Blocked):
                shots.run(self.args(root))
            publish.assert_not_called()

    def test_incomplete_image_set_is_not_accepted(self):
        with patch.object(capture, 'site_config', return_value={'tenant-a': {}}), patch.object(capture, 'capture', new=AsyncMock(return_value=[])), \
             patch.object(shots, 'phone') as phone, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(shots.Blocked, 'configured image set'):
                asyncio.run(shots.collect(Path('/unused'), 'a'))
            phone.assert_not_called()

    def test_corrupt_uploaded_blob_never_creates_branch_or_comment(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'capture.png').write_bytes(b'expected')
            answers = [
                {'default_branch': 'main'}, {'object': {'sha': 'parent'}}, {'tree': {'sha': 'base'}},
                {'sha': 'blob'}, {'encoding': 'base64', 'content': 'Y29ycnVwdA=='},
            ]
            with patch.object(shots, 'api', side_effect=answers) as api:
                with self.assertRaisesRegex(shots.Blocked, 'digest mismatch'):
                    shots.publish('owner/repo', 1, 'issue', directory,
                                  [{'file': 'capture.png'}], directory/'manifest.json', {})
            self.assertFalse(any('/comments' in call.args[0] or call.args[0].endswith('/git/refs')
                                 for call in api.call_args_list))


if __name__ == '__main__':
    unittest.main()

class IntegratedAccountTests(unittest.TestCase):
    def test_scoped_accounts_and_permissions(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'accounts.json'
            p.write_text(json.dumps({'sites': {'tenant-a': {'username': 'a', 'password': 'dummy-a'}, 'tenant-b': {'username': 'p', 'password': 'dummy-p'}}}))
            p.chmod(0o600)
            self.assertEqual(capture.integrated_account('tenant-a', p)[0], 'a')
            self.assertEqual(capture.integrated_account('tenant-b', p)[0], 'p')
            p.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'permissions_invalid'):
                capture.integrated_account('tenant-a', p)

    def test_missing_tenant_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'accounts.json'
            p.write_text('{"sites":{}}')
            p.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'accounts_unavailable'):
                capture.integrated_account('tenant-b', p)

class ConfiguredSitesTests(unittest.TestCase):
    def test_private_config_requires_scoped_metadata(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'accounts.json'
            p.write_text(json.dumps({'sites': {'tenant-a': {
                'origin': 'https://tenant-a.example.test',
                'title': 'Tenant A Dashboard', 'source_ids': ['source-a'],
                'username': 'user', 'password': 'secret'}}}))
            p.chmod(0o600)
            self.assertEqual(capture.site_config(p)['tenant-a']['source_ids'], ['source-a'])
            p.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'permissions_invalid'):
                capture.site_config(p)

    def test_missing_scope_fails_closed(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'accounts.json'
            p.write_text(json.dumps({'sites': {'tenant-a': {
                'origin': 'https://tenant-a.example.test',
                'title': 'Tenant A Dashboard', 'username': 'user', 'password': 'secret'}}}))
            p.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'accounts_unavailable'):
                capture.site_config(p)


class IntegratedLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_form_login(self):
        page = Mock()
        page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        page.locator.return_value.fill = AsyncMock()
        page.locator.return_value.click = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=0)
        page.wait_for_load_state = AsyncMock()
        page.url = 'https://tenant-a.example.test/'
        await capture.integrated_login(page, 'https://tenant-a.example.test', 'user', 'dummy')
        page.goto.assert_awaited_once_with('https://tenant-a.example.test/login', wait_until='domcontentloaded', timeout=45000)
        page.locator.assert_any_call('input[name="username"]')
        page.locator.assert_any_call('input[name="password"]')

    async def test_invalid_login_fails_closed(self):
        page = Mock()
        page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        page.locator.return_value.fill = AsyncMock()
        page.locator.return_value.click = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.wait_for_load_state = AsyncMock()
        page.url = 'https://tenant-a.example.test/login'
        with self.assertRaisesRegex(RuntimeError, 'authentication_failed'):
            await capture.integrated_login(page, 'https://tenant-a.example.test', 'user', 'dummy')
