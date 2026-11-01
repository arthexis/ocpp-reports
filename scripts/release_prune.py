"""Owner-local immutable release retention. Never touch data or shared dependency roots."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

PATTERNS = {
    "csms": re.compile(r"[0-9a-f]{12}(?:-dev)?"),
    "reports": re.compile(r"[0-9a-f]{40}-pkg"),
}
SERVICES = {
    "csms": ("ocpp-csms.service",),
    "reports": ("ocpp-reports-ingest.service", "ocpp-reports-forwarder.service",
                "ocpp-reports-dashboard.service", "ocpp-reports-collector.service"),
}


def _directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
        raise ValueError(f"unsafe directory: {path}")
    return info


def _pointer(root: Path, name: str) -> Path | None:
    link = root / name
    if not link.is_symlink():
        if link.exists():
            raise ValueError(f"non-symlink {name}: {link}")
        return None
    target = link.resolve(strict=True)
    if target.parent != root / "releases":
        raise ValueError(f"{name} escapes releases directory")
    _directory(target)
    return target


def _runtime_references(root: Path, owner: str) -> dict[str, set[str]]:
    """Conservatively account for process references and systemd service definitions.

    An inaccessible/truncated runtime observation is a hard prune blocker.
    """
    found: dict[str, set[str]] = {}
    releases = root / "releases"
    prefix = re.escape(str(releases)) + r"/([^/\s\x00;}\]]+)"
    def consume(raw: str, reason: str) -> None:
        for name in re.findall(prefix, raw):
            found.setdefault(name, set()).add(reason)
    proc = Path("/proc")
    if not proc.is_dir():
        raise ValueError("process inventory unavailable")
    processes = [p for p in proc.iterdir() if p.name.isdigit()]
    if len(processes) > 32768:
        raise ValueError("process inventory limit exceeded")
    for process in processes:
        try:
            for file in ("cmdline", "maps"):
                with (process / file).open("rb") as stream:
                    data = stream.read(1048577)
                if len(data) > 1048576:
                    raise ValueError("process inventory truncated")
                consume(data.decode("utf-8", errors="replace"), "process")
            for file in ("cwd", "exe"):
                try:
                    consume(os.readlink(process / file), "process")
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass  # process exited during scan
        except PermissionError as exc:
            raise ValueError("process inventory inaccessible") from exc
    for service in SERVICES[owner]:
        result = subprocess.run(
            ["systemctl", "show", service, "--property=ExecStart,WorkingDirectory,FragmentPath",
             "--no-pager"], capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            raise ValueError("systemd inventory unavailable")
        if len(result.stdout) > 65536:
            raise ValueError("systemd inventory truncated")
        consume(result.stdout, "service")
    return found


def preview(root: Path, owner: str, cli: Path | None = None,
            *, inspect_runtime: bool = False) -> dict:
    root = Path(root).absolute()
    result = {
        "schema": "ocpp/owner-prune-preview/v2",
        "owner": owner, "root": str(root), "dry_run": True,
        "deletion_enabled": False, "status": "blocked",
        "protected": [], "unverified": [], "legacy": [], "errors": [],
    }
    try:
        if owner not in PATTERNS:
            raise ValueError("unregistered owner")
        _directory(root)
        releases = root / "releases"
        _directory(releases)
        active = _pointer(root, "current")
        previous = _pointer(root, "previous")
        protected: dict[Path, set[str]] = {}
        for path, reason in ((active, "current"), (previous, "previous")):
            if path is not None:
                protected.setdefault(path, set()).add(reason)
        if cli and (cli.is_symlink() or cli.exists()):
            if not cli.is_symlink():
                raise ValueError("CLI is not a symlink")
            executable = cli.resolve(strict=True)
            if releases not in executable.parents:
                raise ValueError("CLI target outside managed releases")
            ref = executable.relative_to(releases).parts[0]
            protected.setdefault(releases / ref, set()).add("cli")
        elif inspect_runtime:
            raise ValueError("managed CLI reference unavailable")
        if inspect_runtime:
            for name, reasons in _runtime_references(root, owner).items():
                if not PATTERNS[owner].fullmatch(name):
                    raise ValueError("runtime references unknown release")
                protected.setdefault(releases / name, set()).update(reasons)
        names = sorted(releases.iterdir())
        if len(names) > 4096:
            raise ValueError("release inventory exceeds limit")
        for path in names:
            info = _directory(path)
            if not PATTERNS[owner].fullmatch(path.name):
                result["legacy"].append({"name": path.name, "path": str(path),
                                         "reasons": ["legacy/unrecognized layout"],
                                         "deletable": False})
                continue
            if path in protected:
                result["protected"].append({"name": path.name, "path": str(path),
                                            "identity": [info.st_dev, info.st_ino],
                                            "reasons": sorted(protected[path]),
                                            "deletable": False})
            else:
                result["unverified"].append({"name": path.name, "path": str(path),
                                              "identity": [info.st_dev, info.st_ino],
                                              "reasons": ["runtime validation required"] if not inspect_runtime else [],
                                              "deletable": False})
        if any(p not in names for p in protected):
            raise ValueError("reference to missing release")
        if active is None:
            raise ValueError("missing current link")
        if previous is None and len(names) > 1:
            raise ValueError("missing previous link with multiple releases")
        if owner == "reports":
            role = root / "role"
            if not role.is_file() or role.is_symlink():
                raise ValueError("missing/unsafe Reports role")
            if role.read_text().strip() not in ("terminal", "satellite", "watchtower", "control"):
                raise ValueError("invalid Reports role")
        result["status"] = "runtime_checked" if inspect_runtime else "preview_only"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["protected"] = []
        result["unverified"] = []
        result["legacy"] = []
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Owner immutable release retention")
    parser.add_argument("--owner", choices=tuple(PATTERNS), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    if args.apply or args.confirm:
        parser.error("deletion remains disabled in increment 2 pending verified owner runtime and rollback acceptance")
    report = preview(args.root, args.owner, args.cli, inspect_runtime=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
