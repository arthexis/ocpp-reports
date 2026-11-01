"""Owner-local immutable release retention. Never touch data or shared dependency roots."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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


def _runtime_references(root: Path, owner: str, *, proc: Path = Path("/proc")) -> tuple[dict[str, set[str]], list[str]]:
    """Conservatively account for process references and systemd service definitions.

    An inaccessible/truncated runtime observation is a hard prune blocker.
    """
    found: dict[str, set[str]] = {}
    errors: list[str] = []
    releases = root / "releases"
    prefix = re.escape(str(releases)) + r"/([^/\s\x00;}\]]+)"
    def consume(raw: str, reason: str) -> None:
        for name in re.findall(prefix, raw):
            found.setdefault(name, set()).add(reason)
    # First determine *which* processes are relevant using public metadata.
    # Do not attempt to read private maps/cmdlines of unrelated accounts.
    units = []
    for service in SERVICES[owner]:
        result = subprocess.run(
            ["systemctl", "show", service,
             "--property=ExecStart,WorkingDirectory,FragmentPath,MainPID,ControlGroup,LoadState",
             "--no-pager"], capture_output=True, text=True, timeout=15, check=False)
        if result.returncode or len(result.stdout) > 65536:
            errors.append("systemd inventory unavailable")
            continue
        info = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if info.get("LoadState") == "not-found":
            continue  # Optional service unit, absent for this Reports role.
        consume(result.stdout, "service")
        try:
            main_pid = int(info.get("MainPID", "0"))
        except ValueError:
            errors.append("invalid systemd MainPID")
            continue
        group = info.get("ControlGroup", "")
        if group and (not group.startswith("/") or ".." in group.split("/")):
            errors.append("invalid systemd cgroup")
            continue
        units.append((main_pid, group))

    if not proc.is_dir():
        raise ValueError("process inventory unavailable")
    processes = [p for p in proc.iterdir() if p.name.isdigit()]
    if len(processes) > 32768:
        raise ValueError("process inventory limit exceeded")
    owner_uid = os.geteuid()
    for process in processes:
        try:
            status = (process / "status").read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            uid = int(uid_line.split()[1])
            cgroup = (process / "cgroup").read_text()
            pid = int(process.name)
            member = any(
                pid == main or (
                    bool(group) and any(
                        entry.partition("::")[2] == group or
                        entry.partition("::")[2].startswith(group.rstrip("/") + "/")
                        for entry in cgroup.splitlines()
                        if "::" in entry
                    )
                )
                for main, group in units
            )
            if uid != owner_uid and not member:
                continue
            # Once classified as relevant, unreadable evidence blocks pruning.
            for file in ("cmdline", "maps"):
                with (process / file).open("rb") as stream:
                    data = stream.read(1048577)
                if len(data) > 1048576:
                    raise ValueError("relevant process inventory truncated")
                consume(data.decode("utf-8", errors="replace"), "process")
            for file in ("cwd", "exe"):
                consume(os.readlink(process / file), "process")
        except FileNotFoundError:
            # A vanished PID is benign; missing metadata on a *live* PID is not.
            if process.exists():
                errors.append("process inventory incomplete: missing live PID evidence")
        except (ProcessLookupError, StopIteration):
            if process.exists():
                errors.append("process inventory incomplete: live PID changed")
        except (PermissionError, OSError, ValueError) as exc:
            errors.append("process inventory incomplete: " + type(exc).__name__)
    return found, sorted(set(errors))


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
            runtime_references, runtime_errors = _runtime_references(root, owner)
            result["errors"].extend(runtime_errors)
            for name, reasons in runtime_references.items():
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
        result["status"] = ("blocked" if result["errors"] else
                            ("runtime_checked" if inspect_runtime else "preview_only"))
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
