"""Capture configured dashboards on desktop and Android, then post verified evidence."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlparse
import uuid

import capture


class Blocked(RuntimeError):
    pass


def target(value, repository):
    if value.startswith('https://'):
        parsed = urlparse(value)
        match = re.fullmatch(r'/([^/]+/[^/]+)/(?:issues|pull)/(\d+)/?', parsed.path)
        if parsed.netloc != 'github.com' or not match:
            raise Blocked('Use a github.com issue or PR URL.')
        repository, number = match.groups()
    elif '#' in value:
        repository, number = value.rsplit('#', 1)
    else:
        number = value
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise Blocked('Repository must be OWNER/REPO.')
    if not number.isdigit() or int(number) < 1:
        raise Blocked('Target must be an issue/PR number, OWNER/REPO#NUMBER, or GitHub URL.')
    return repository, int(number)


def api(path, data=None):
    command = ['gh', 'api', '--hostname', 'github.com', path]
    if data is not None:
        command += ['--method', 'POST', '--input', '-']
    env = dict(os.environ, GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0')
    try:
        result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked('GitHub CLI unavailable or timed out. Check gh auth status.') from None
    if result.returncode:
        status = re.search(r'HTTP (\d+)', result.stderr)
        suffix = ' (HTTP ' + status[1] + ')' if status else ''
        raise Blocked('GitHub request failed' + suffix + '. Check gh auth status and repository access.')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise Blocked('GitHub returned an invalid API response.') from None


def phone(serial):
    try:
        return capture.device(serial, wake=True)
    except RuntimeError as exc:
        if str(exc) == 'android_not_connected_and_authorized':
            raise Blocked('Android unavailable: connect one phone, enable USB debugging, and accept authorization. Nothing uploaded.') from None
        if str(exc) == 'android_unlock_required':
            raise Blocked('Android is locked: unlock the screen, then rerun. Nothing uploaded.') from None
        raise Blocked('Android readiness failed. Check the phone and rerun; nothing uploaded.') from None
    except (OSError, subprocess.SubprocessError):
        raise Blocked('Android disconnected or ADB failed. Reconnect and authorize the phone; nothing uploaded.') from None


def progress(message):
    print(message, flush=True)


async def collect(directory, serial):
    records = []
    sites = list(capture.site_config())
    for mode in ['android', 'desktop']:
        progress('Capturing configured dashboards on ' + ('Android…' if mode == 'android' else 'desktop Chromium…'))
        try:
            records.extend(await capture.capture(SimpleNamespace(
                mode=mode, site='all', output=directory, serial=serial, quiet=True)))
        except Exception:
            hint = 'Reconnect/unlock the phone and close browser dialogs.' if mode == 'android' else 'Check Chromium, dashboard access and the protected local credential.'
            raise Blocked(f'{mode.title()} capture or verification failed. {hint} Nothing uploaded.') from None
    expected = {(site, mode) for site in sites for mode in ['android', 'desktop']}
    if {(r['site'], r['mode']) for r in records} != expected or len(records) != len(expected):
        raise Blocked('The complete configured image set is missing. Nothing uploaded.')
    for record in records:
        data = (directory / record['file']).read_bytes()
        if data[:8] != b'\x89PNG\r\n\x1a\n' or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise Blocked('An image failed PNG/digest verification. Nothing uploaded.')
        width, height = struct.unpack('>II', data[16:24])
        if min(width, height) < 300:
            raise Blocked('An image is unexpectedly small. Nothing uploaded.')
        record['image_pixels'] = {'width': width, 'height': height}
    phone(serial)  # Stop if Android became unavailable before publishing.
    return records


def publish(repository, number, kind, directory, records, manifest, receipt):
    prefix = 'repos/' + repository
    metadata = api(prefix)
    ref = api(prefix + '/git/ref/heads/' + metadata['default_branch'])
    parent = ref['object']['sha']
    tree = api(prefix + '/git/commits/' + parent)['tree']['sha']
    unique = directory.name
    folder = 'docs/evidence/dashboard-shots/' + unique
    entries = []
    for name in [r['file'] for r in records] + [manifest.name]:
        data = (directory / name).read_bytes()
        blob = api(prefix + '/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        # Independently verify server-stored bytes before linking the evidence.
        stored = api(prefix + '/git/blobs/' + blob['sha'])
        if stored.get('encoding') != 'base64' or hashlib.sha256(base64.b64decode(stored['content'])).digest() != hashlib.sha256(data).digest():
            raise Blocked('Uploaded artifact digest mismatch; no evidence comment was posted.')
        entries.append({'path': folder + '/' + name, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    new_tree = api(prefix + '/git/trees', {'base_tree': tree, 'tree': entries})
    commit = api(prefix + '/git/commits', {'message': f'Dashboard screenshots for #{number} ({unique})',
                                         'tree': new_tree['sha'], 'parents': [parent]})
    branch = f'evidence/dashboard-shots-{number}-{unique}'
    api(prefix + '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit['sha']})
    receipt.update({'branch': branch, 'commit': commit['sha'], 'status': 'evidence_uploaded'})
    save(directory / 'receipt.json', receipt)
    image_base = ('https://raw.githubusercontent.com/' + repository + '/' + commit['sha'] + '/' + folder + '/')
    if metadata.get('private'):
        image_base = 'https://github.com/' + repository + '/blob/' + commit['sha'] + '/' + folder + '/'
    def artifact(name):
        return image_base + name + ('?raw=true' if metadata.get('private') else '')
    lines = [f'Configured dashboard screenshots — desktop and physical Android.', '',
             f'Captured {receipt["created_at"]}. Dashboard titles and source scopes verified with HTTPS.', '']
    for site in sorted({r['site'] for r in records}):
        for mode, label in [('desktop', 'desktop Chromium'), ('android', 'actual Android browser')]:
            record = next(r for r in records if r['site'] == site and r['mode'] == mode)
            width = '900' if mode == 'desktop' else '360'
            lines += [f'### {site} — {label}', '',
                      f'<img src="{artifact(record["file"])}" alt="Dashboard {site} — {label}" width="{width}">', '']
    lines += [f'[Capture provenance and SHA-256 manifest]({artifact(manifest.name)}).', '',
              f'Evidence branch: `{branch}`; commit `{commit["sha"]}`. All uploaded artifact digests verified.', '',
              'Captured using `ocpp-dashboard-shots`. All configured captures passed before publication; temporary ADB forwards were removed. No production service/configuration changes were made.', '',
              'Dashboard rendering and source-scope evidence does not establish collector readiness or physical charger state.']
    comment = api(prefix + f'/issues/{number}/comments', {'body': '\n'.join(lines)})
    receipt.update({'status': 'complete', 'comment_url': comment['html_url']})
    save(directory / 'receipt.json', receipt)
    return comment['html_url']


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)


def run(args):
    repository, number = target(args.target, args.repo)
    for executable in ['adb', 'gh', 'chromium']:
        if not shutil.which(executable):
            raise Blocked(executable + ' is missing. Nothing uploaded.')
    progress('Checking Android and GitHub target…')
    serial = phone(args.serial)  # Must precede desktop work and all GitHub mutations.
    try:
        for site in capture.site_config():
            capture.integrated_account(site)
    except RuntimeError:
        raise Blocked("Integrated account file unavailable; nothing captured or uploaded.") from None
    issue = api(f'repos/{repository}/issues/{number}')
    kind = 'PR' if 'pull_request' in issue else 'issue'
    if args.check:
        progress(f'Ready: Android authorized and unlocked; {repository} {kind} #{number} accessible. Nothing captured or uploaded.')
        return
    unique = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    root = args.output or Path.home() / '.local/share/ocpp-dashboard-launcher/captures'
    directory = root / unique
    directory.mkdir(parents=True, mode=0o700)
    progress('Local evidence: ' + str(directory))
    records = asyncio.run(collect(directory, serial))
    manifest = directory / 'manifest.json'
    save(manifest, {'captures': records, 'physical_android': True,
                    'android_model': capture.adb('shell', 'getprop', 'ro.product.model', serial=serial).strip(),
                    'android_version': capture.adb('shell', 'getprop', 'ro.build.version.release', serial=serial).strip(),
                    'target': issue['html_url'], 'temporary_adb_forwards_cleaned': True})
    receipt = {'created_at': datetime.now(timezone.utc).isoformat(), 'target': issue['html_url'],
               'local_directory': str(directory), 'status': 'captures_verified'}
    save(directory / 'receipt.json', receipt)
    progress('All configured captures verified. Uploading evidence…')
    url = publish(repository, number, kind, directory, records, manifest, receipt)
    progress('Uploaded: ' + url)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog='ocpp-dashboard-shots', description=__doc__, epilog='Examples: ocpp-dashboard-shots 108 | ocpp-dashboard-shots https://github.com/OWNER/REPO/pull/123')
    parser.add_argument('target', help='Issue/PR number, OWNER/REPO#NUMBER, or GitHub issue/PR URL')
    parser.add_argument('--repo', default='arthexis/ocpp-reports', help='Repository for a numeric target (default: %(default)s)')
    parser.add_argument('--serial', help='ADB serial when more than one Android device is attached')
    parser.add_argument('--output', type=Path, help='Local evidence root; each run gets a private unique subdirectory')
    parser.add_argument('--check', action='store_true', help='Check phone, credentials and target without capturing/uploading')
    args = parser.parse_args()
    lock_path = Path.home() / '.cache/ocpp-dashboard-shots.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Stopped: another dashboard capture is running.', file=sys.stderr)
            return 2
        try:
            run(args)
        except Blocked as exc:
            print('Stopped: ' + str(exc), file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print('Stopped by operator. Check the local receipt before retrying a publication.', file=sys.stderr)
            return 130
        except Exception as exc:
            print(f'Stopped: {type(exc).__name__}. Check the local receipt; do not assume an upload completed.', file=sys.stderr)
            return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
