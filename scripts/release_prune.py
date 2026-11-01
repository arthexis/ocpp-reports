"""Read-only inventory for owner-managed immutable OCPP releases.

Deliberately no deletion in this increment: the owner deploy/rollback lock is not
yet shared with Ansible, so listing candidates must not authorize pruning.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

PATTERNS = {
    "csms": re.compile(r"[0-9a-f]{12}(?:-dev)?"),
    "reports": re.compile(r"[0-9a-f]{40}-pkg"),
}


def _directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"not a real directory: {path}")
    if info.st_mode & 0o002:
        raise ValueError(f"world-writable install directory: {path}")


def _pointer(root: Path, name: str) -> Path | None:
    link = root / name
    if not link.is_symlink():
        if link.exists():
            raise ValueError(f"non-symlink {name}: {link}")
        return None
    target = link.resolve(strict=True)
    release_root = root / "releases"
    if target.parent != release_root:
        raise ValueError(f"{name} escapes releases directory")
    _directory(target)
    return target


def preview(root: Path, owner: str, cli: Path | None = None) -> dict:
    """Return a deterministic read-only inventory; ambiguous owners block all."""
    root = Path(root).absolute()
    result = {
        "schema": "ocpp/owner-prune-preview/v1",
        "owner": owner,
        "root": str(root),
        "dry_run": True,
        "deletion_enabled": False,
        "status": "blocked",
        "protected": [],
        "unverified": [],
        "legacy": [],
        "errors": [],
    }
    try:
        _directory(root)
        releases = root / "releases"
        _directory(releases)
        active = _pointer(root, "current")
        previous = _pointer(root, "previous")
        protected: dict[Path, set[str]] = {}
        for path, reason in ((active, "current"), (previous, "previous")):
            if path:
                protected.setdefault(path, set()).add(reason)
        if cli and (cli.is_symlink() or cli.exists()):
            if not cli.is_symlink():
                raise ValueError("CLI is not a symlink")
            executable = cli.resolve(strict=True)
            for release in releases.iterdir():
                if release == executable or release in executable.parents:
                    protected.setdefault(release, set()).add("cli")
                    break
            else:
                raise ValueError("CLI target outside managed releases")
        names = sorted(releases.iterdir())
        if len(names) > 4096:
            raise ValueError("release inventory exceeds limit")
        for path in names:
            if not PATTERNS[owner].fullmatch(path.name):
                # Historical installs can contain noncanonical release names.
                # Show but never propose them for removal without migration proof.
                if path.is_symlink() or not path.is_dir():
                    raise ValueError(f"unsafe legacy release entry: {path.name}")
                _directory(path)
                result["legacy"].append({"name": path.name, "path": str(path),
                                         "reasons": ["legacy/unrecognized layout"],
                                         "deletable": False})
                continue
            _directory(path)
            (result["protected"] if path in protected else result["unverified"]).append({
                "name": path.name,
                "path": str(path),
                "reasons": sorted(protected[path]) if path in protected else ["needs runtime/rollback/lock verification"],
                "deletable": False,
            })
        if active is None:
            raise ValueError("missing current link")
        if previous is None and len(names) > 1:
            raise ValueError("missing previous link with multiple releases")
        result["status"] = "preview_only"
    except (OSError, ValueError, KeyError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["protected"] = []
        result["unverified"] = []
        result["legacy"] = []
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only immutable release prune inventory")
    parser.add_argument("--owner", choices=tuple(PATTERNS), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        parser.error("deletion is not enabled; owner deploy/rollback locking must be integrated first")
    report = preview(args.root, args.owner, args.cli)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
