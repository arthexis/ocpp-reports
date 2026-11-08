"""Capture scoped customer dashboards without exposing authentication secrets."""
import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import uuid
from datetime import datetime, timezone

ACCOUNTS = Path.home() / '.config/ocpp-dashboard/integrated-accounts.json'


def site_config(path=ACCOUNTS):
    import stat
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
            raise RuntimeError('integrated_accounts_permissions_invalid')
        sites = json.loads(path.read_text())['sites']
        if not isinstance(sites, dict) or not sites:
            raise ValueError()
        for item in sites.values():
            if not isinstance(item.get('origin'), str) or not item['origin'].startswith('https://') or not item['origin'].rstrip('/').count('.'):
                raise ValueError()
            if not isinstance(item.get('title'), str) or not item['title'] or not isinstance(item.get('source_ids'), list) or not item['source_ids'] or any(not isinstance(x, str) or not x for x in item['source_ids']):
                raise ValueError()
        return sites
    except (OSError, KeyError, ValueError, TypeError):
        raise RuntimeError('integrated_accounts_unavailable') from None


def integrated_account(site, path=ACCOUNTS):
    # Local owner-only file, never included in captures or published manifests.
    import stat
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077 or path.stat().st_uid != os.getuid():
            raise RuntimeError('integrated_accounts_permissions_invalid')
        account = json.loads(path.read_text())['sites'][site]
        username, password = account['username'], account['password']
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise ValueError()
        return username, password
    except (OSError, KeyError, ValueError, TypeError):
        raise RuntimeError('integrated_accounts_unavailable') from None


async def integrated_login(page, origin, username, password):
    # Never expose credentials in URL, CDP logs, exception text or screenshots.
    response = await page.goto(origin + '/login', wait_until='domcontentloaded', timeout=45000)
    if response is None or response.status != 200:
        raise RuntimeError('dashboard_login_unavailable')
    await page.locator('input[name="username"]').fill(username)
    await page.locator('input[name="password"]').fill(password)
    await page.locator('form button').click()
    await page.wait_for_load_state('networkidle')
    if page.url.rstrip('/') != origin or await page.locator('input[name="password"]').count():
        raise RuntimeError('dashboard_authentication_failed')


def adb(*args, serial=None, binary=False):
    command = ['adb'] + (['-s', serial] if serial else []) + list(args)
    return subprocess.run(command, check=True, capture_output=True,
                          text=not binary, timeout=25).stdout


def locked(window, trust):
    return bool(re.search(r'(?:deviceLocked|isDeviceLocked)=(?:1|true)\b', trust)
                or re.search(r'(?:mDreamingLockscreen|isStatusBarKeyguard|mShowingLockscreen)\s*=\s*true\b', window)
                or re.search(r'^\s*showing=true\s*$', window, re.M))


def device(serial, wake=False):
    entries = [line.split() for line in adb('devices').splitlines()[1:] if line.strip()]
    entries = [item for item in entries if not serial or item[0] == serial]
    if len(entries) != 1 or entries[0][1] != 'device':
        raise RuntimeError('android_not_connected_and_authorized')
    selected = entries[0][0]
    state = adb('shell', 'dumpsys', 'window', 'policy', serial=selected)
    trust = adb('shell', 'dumpsys', 'trust', serial=selected)
    if locked(state, trust):
        raise RuntimeError('android_unlock_required')
    if wake:
        # Wake an already unlocked device; never dismiss or bypass keyguard.
        adb('shell', 'input', 'keyevent', 'KEYCODE_WAKEUP', serial=selected)
        state = adb('shell', 'dumpsys', 'window', 'policy', serial=selected)
        if locked(state, adb('shell', 'dumpsys', 'trust', serial=selected)):
            raise RuntimeError('android_unlock_required')
    return selected


def verify_phone_display(serial, origin, title):
    device(serial)
    focus = adb('shell', 'dumpsys', 'window', 'windows', serial=serial)
    focused = next((line for line in focus.splitlines() if 'mCurrentFocus=' in line), '')
    if 'com.android.chrome/' not in focused:
        raise RuntimeError('android_browser_not_foreground')
    # Samsung's shell reports success for /dev/tty without returning the XML.
    # Use a unique shell-private temporary file and remove it on every path.
    temporary = '/data/local/tmp/dashboard-ui-' + uuid.uuid4().hex + '.xml'
    try:
        adb('shell', 'uiautomator', 'dump', '--compressed', temporary, serial=serial)
        xml = adb('exec-out', 'cat', temporary, serial=serial)
    finally:
        adb('shell', 'rm', '-f', temporary, serial=serial)
    start, end = xml.find('<hierarchy'), xml.rfind('</hierarchy>')
    if start < 0 or end < 0:
        raise RuntimeError('android_display_unverified')
    root = ET.fromstring(xml[start:end + len('</hierarchy>')])
    nodes = list(root.iter('node'))
    host = origin.removeprefix('https://')
    urls = [n.get('text', '') for n in nodes if n.get('resource-id', '').endswith(':id/url_bar')]
    if not any(value.rstrip('/') in {host, origin} for value in urls):
        raise RuntimeError('android_display_unverified')
    if not any(n.get('text', '').strip() == title for n in nodes):
        raise RuntimeError('android_display_unverified')


async def validate(page, expected_title, expected_sources):
    # Navigation already waited for network idle. Native DOM reads also work
    # on Android Chrome versions lacking Playwright's checkVisibility API.
    if await page.evaluate("document.querySelector('h1')?.innerText") != expected_title:
        raise RuntimeError('unexpected_dashboard_title')
    scope = await page.evaluate('''async () => {
        const r = await fetch('/api/snapshot');
        if (!r.ok) return null;
        const s = await r.json();
        return {sources: s.sources.map(x => x.source_id),
            all: [...new Set(Object.values(s).flatMap(rows =>
                Array.isArray(rows) ? rows.map(x => x.source_id).filter(Boolean) : []))]};
    }''')
    if not scope or set(scope['sources']) != expected_sources or set(scope['all']) - expected_sources:
        raise RuntimeError('unexpected_dashboard_source_scope')
    await page.evaluate('document.fonts.ready')


async def capture(args):
    from playwright.async_api import async_playwright
    config = site_config()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.output.stat().st_mode & 0o077:
        raise RuntimeError('output_directory_must_be_private')
    serial = device(args.serial, wake=True) if args.mode == 'android' else None
    sites = list(config) if args.site == 'all' else [args.site]
    if not sites or any(site not in config for site in sites):
        raise RuntimeError('dashboard_site_unconfigured')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    records = []
    async with async_playwright() as pw:
        for site in sites:
            origin, expected_sources, title = (config[site]['origin'], set(config[site]['source_ids']), config[site]['title'])
            port = None
            browser = None
            username, secret = integrated_account(site)
            try:
                if args.mode == 'android':
                    # Install scoped auth handling before the first HTTPS navigation.
                    adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
                        '-d', 'about:blank', '-p', 'com.android.chrome', serial=serial)
                    port = adb('forward', 'tcp:0', 'localabstract:chrome_devtools_remote', serial=serial).strip()
                    browser = await pw.chromium.connect_over_cdp('http://127.0.0.1:' + port, timeout=15000)
                    pages = browser.contexts[0].pages
                    page = next((p for p in reversed(pages) if p.url == 'about:blank'), None)
                    if page is None:
                        page = await browser.contexts[0].new_page()
                else:
                    browser = await pw.chromium.launch(executable_path='/usr/bin/chromium',
                                                      headless=True, chromium_sandbox=True)
                    mobile = args.mode == 'mobile-emulated'
                    context = await browser.new_context(
                        viewport={'width': 412 if mobile else 1440, 'height': 915 if mobile else 1000},
                        is_mobile=mobile, has_touch=mobile, device_scale_factor=1)
                    page = await context.new_page()
                await integrated_login(page, origin, username, secret)
                await validate(page, title, expected_sources)
                dimensions = await page.evaluate('({width: innerWidth, height: innerHeight, dpr: devicePixelRatio})')
                target = args.output / f'{site}-{args.mode}-{stamp}.png'
                if args.mode == 'android':
                    # A successful CDP navigation can target a background tab.
                    # Make it the displayed page before capturing phone pixels.
                    await page.bring_to_front()
                    await page.wait_for_function("document.visibilityState === 'visible'", timeout=10000)
                    await page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    verify_phone_display(serial, origin, title)
                    target.write_bytes(adb('exec-out', 'screencap', '-p', serial=serial, binary=True))
                else:
                    # Capture the summary/status area, ending before customer record tables.
                    cutoff = await page.get_by_role('heading', name='Transacciones recientes', exact=True).evaluate(
                        'el => el.closest("section").getBoundingClientRect().top')
                    bounds = {'x': 0, 'y': 0, 'width': dimensions['width'],
                              'height': min(dimensions['height'], max(1, int(cutoff) - 12))}
                    await page.screenshot(path=str(target), clip=bounds)
                os.chmod(target, 0o600)
                if target.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
                    raise RuntimeError('invalid_png_capture')
                record = {'site': site, 'mode': args.mode, 'origin': origin,
                          'captured_at': datetime.now(timezone.utc).isoformat(),
                          'viewport': dimensions, 'source_scope_verified': True,
                          'tls_verification': True, 'file': target.name,
                          'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
                records.append(record)
                if not getattr(args, 'quiet', False):
                    print(json.dumps(record), flush=True)
            finally:
                try:
                    if browser is not None and args.mode != 'android':
                        await browser.close()
                finally:
                    if port:
                        adb('forward', '--remove', 'tcp:' + port, serial=serial)
    manifest = args.output / f'manifest-{args.mode}-{stamp}.json'
    manifest.write_text(json.dumps({'captures': records}, indent=2) + '\n')
    os.chmod(manifest, 0o600)
    return records


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['desktop', 'mobile-emulated', 'android'], required=True)
    parser.add_argument('--site', default='all', help='Configured site key or all')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--serial')
    args = parser.parse_args()
    try:
        asyncio.run(capture(args))
    except Exception as exc:
        # Third-party exceptions may contain authenticated protocol details.
        result = {'status': 'blocked', 'mode': args.mode, 'error_class': type(exc).__name__}
        if type(exc) is RuntimeError and str(exc) in {
            'android_not_connected_and_authorized', 'android_unlock_required',
            'output_directory_must_be_private', 'unexpected_dashboard_title',
            'unexpected_dashboard_source_scope', 'dashboard_authentication_failed',
            'integrated_accounts_unavailable', 'integrated_accounts_permissions_invalid',
            'dashboard_login_unavailable', 'dashboard_site_unconfigured',
            'invalid_png_capture',
            'android_browser_not_foreground', 'android_display_unverified',
        }:
            result['reason'] = str(exc)
        print(json.dumps(result), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
