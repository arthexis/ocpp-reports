"""Persistent, instance-scoped dashboard timezone overrides.

Keep the rendered systemd ExecStart (and customer --source-id isolation)
untouched. Operators update only a small configuration file; it is read when
the existing dashboard service restarts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .web import dashboard_timezones

CONFIG_DIR = Path("/etc/ocpp-reports")
INSTANCE_RE = re.compile(r"dashboard(?:-[a-z0-9]+)*\Z")


def timezone_file(instance: str) -> Path:
    if not INSTANCE_RE.fullmatch(instance):
        raise ValueError("Invalid dashboard instance; expected dashboard or dashboard-<name>")
    return CONFIG_DIR / f"{instance}.timezones.json"


def configured_timezones(path: Path, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Read an optional operator override; never create a file while serving."""
    if not path.exists():
        return dashboard_timezones(defaults)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Timezone configuration is not a regular file: {path}")
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid dashboard timezone configuration: {path}") from exc
    if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
        raise ValueError("Dashboard timezone configuration must be a nonempty JSON list")
    return dashboard_timezones(tuple(values))


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Unsafe timezone configuration path: {path}")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def update_timezones(instance: str, requested: tuple[str, ...]) -> str:
    """Persist timezones and restart only the selected existing dashboard.

    Explicit root access required. A restart failure restores prior config.
    Never edits systemd ExecStart, customer source restrictions or Ansible files.
    """
    if os.geteuid() != 0:
        raise PermissionError("Updating the installed dashboard requires sudo")
    path = timezone_file(instance)
    zones = dashboard_timezones(requested)
    if not requested:
        raise ValueError("Provide at least one --timezone with --update")
    if not CONFIG_DIR.is_dir() or CONFIG_DIR.is_symlink():
        raise ValueError(f"Missing or unsafe Reports configuration directory: {CONFIG_DIR}")
    service = f"ocpp-reports-{instance}.service"
    effective = _run(["systemctl", "show", service, "-p", "ExecStart", "--value"])
    if effective.returncode != 0 or f"--timezone-file {path}" not in effective.stdout:
        raise ValueError(f"{service} must be installed with --timezone-file {path}; deploy updated Reports first")
    previous = path.read_bytes() if path.exists() and not path.is_symlink() and path.is_file() else None
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Unsafe timezone configuration path: {path}")
    payload = (json.dumps(list(zones), ensure_ascii=True) + "\n").encode("utf-8")
    if previous == payload:
        return f"{service}: timezone choices already configured"
    _atomic_write(path, payload)
    restarted = _run(["systemctl", "restart", service])
    active = _run(["systemctl", "is-active", "--quiet", service]) if restarted.returncode == 0 else restarted
    if restarted.returncode != 0 or active.returncode != 0:
        # Restore previous settings when a restart fails, then attempt to
        # recover the original service without suppressing the failure.
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, previous)
        recovery = _run(["systemctl", "restart", service])
        raise RuntimeError(
            f"Failed to restart {service}; previous timezone configuration restored. "
            f"Service recovery {'succeeded' if recovery.returncode == 0 else 'failed'}"
        )
    return f"{service}: configured {', '.join(zones)}"
