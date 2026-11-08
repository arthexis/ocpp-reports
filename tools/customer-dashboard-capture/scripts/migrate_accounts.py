"""Preview or atomically migrate owner-only dashboard capture account metadata.

The metadata input maps existing site keys to origin, title and source_ids.
Never print secrets, site keys, origins, titles or source IDs.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import tempfile

FIELDS = ('origin', 'title', 'source_ids')


def private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('private_file_requirements_not_met')
    return json.loads(path.read_text())


def prepare(accounts, metadata):
    sites = accounts['sites']
    if not isinstance(sites, dict) or not sites or set(sites) != set(metadata):
        raise ValueError('tenant_mapping_mismatch')
    result = {'sites': {}}
    for key, original in sites.items():
        extra = metadata[key]
        if not isinstance(original, dict) or not isinstance(extra, dict):
            raise ValueError('invalid_site')
        if not all(isinstance(original.get(k), str) and original[k] for k in ('username', 'password')):
            raise ValueError('missing_credentials')
        if set(extra) != set(FIELDS):
            raise ValueError('invalid_metadata_keys')
        origin, title, sources = (extra[k] for k in FIELDS)
        if not isinstance(origin, str) or not origin.startswith('https://') or not isinstance(title, str) or not title:
            raise ValueError('invalid_origin_or_title')
        if not isinstance(sources, list) or not sources or any(not isinstance(x, str) or not x for x in sources) or len(set(sources)) != len(sources):
            raise ValueError('invalid_source_scope')
        for field in FIELDS:
            if field in original and original[field] != extra[field]:
                raise ValueError('existing_scope_conflict')
        result['sites'][key] = {**original, **extra}
    if set(accounts) != {'sites'}:
        raise ValueError('unexpected_account_fields')
    return result


def migrate(accounts_path, metadata_path, apply=False):
    accounts = private_json(accounts_path)
    metadata = private_json(metadata_path)
    updated = prepare(accounts, metadata)
    changed = updated != accounts
    if changed and apply:
        fd, temp = tempfile.mkstemp(prefix='.capture-accounts-', dir=accounts_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(updated, handle, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, accounts_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return 'change_required' if changed and not apply else ('updated' if changed else 'already_current')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accounts', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Requires separate operator authorization')
    args = parser.parse_args()
    try:
        print(migrate(args.accounts, args.metadata, args.apply))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print('blocked:' + type(exc).__name__)
        raise SystemExit(2) from None
