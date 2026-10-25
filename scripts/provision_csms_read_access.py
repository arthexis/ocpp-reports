"""Grant narrow read-only ACL access to the active CSMS producer and evidence.

Only called by the Reports Ansible installer, as root. Never changes ownership.
"""
import os
from pathlib import Path
import subprocess
import sys

from discover_csms_data_dir import active_data_dir


def acl(path: Path, entry: str, *, default: bool = False) -> None:
    command = ["setfacl", "-m", ("d:" if default else "") + entry, str(path)]
    subprocess.run(command, check=True)


def ancestors(path: Path):
    return list(reversed(path.parents))


def grant_tree(root: Path, user: str, *, future: bool) -> None:
    for parent in ancestors(root):
        acl(parent, f"u:{user}:--x")
    for current, dirs, files in os.walk(root, followlinks=False):
        directory = Path(current)
        acl(directory, f"u:{user}:r-x")
        if future:
            acl(directory, f"u:{user}:r-x", default=True)
        for name in files:
            path = directory / name
            if path.is_symlink():
                continue
            mode = "r-x" if os.access(path, os.X_OK) else "r--"
            acl(path, f"u:{user}:{mode}")


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("CSMS ACL provisioning requires root")
    user, data_dir = sys.argv[1], Path(sys.argv[2]).resolve(strict=True)
    active_dir = active_data_dir().resolve(strict=True)
    if data_dir != active_dir:
        raise RuntimeError("Selected ingestion directory differs from active CSMS")
    pid = int(subprocess.check_output(
        ["systemctl", "show", "ocpp-csms.service", "--property=MainPID", "--value", "--no-pager"],
        text=True).strip())
    if pid <= 0:
        raise RuntimeError("CSMS is not active")
    argv = [part.decode() for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
    executable = Path(argv[0]).resolve(strict=True)
    if executable.name != "ocpp-csms" or "/releases/" not in str(executable):
        raise RuntimeError("Unexpected CSMS executable path")
    # The venv's Python and packages live beneath this release.
    release = next((p for p in executable.parents if p.parent.name == "releases"), None)
    if release is None:
        raise RuntimeError("CSMS release directory not found")
    grant_tree(release, user, future=False)
    grant_tree(data_dir, user, future=True)
    # Verify the effective identity, not merely filesystem metadata.
    probe = subprocess.run(
        ["runuser", "-u", user, "--", "test", "-x", str(executable)],
        check=False)
    if probe.returncode:
        raise RuntimeError("Reports cannot read the CSMS executable")
    probe = subprocess.run(
        ["runuser", "-u", user, "--", "test", "-r", str(data_dir)],
        check=False)
    if probe.returncode:
        raise RuntimeError("Reports cannot read CSMS evidence")
    # An actual --help run catches inaccessible venv modules or shebang interpreter.
    subprocess.run(["runuser", "-u", user, "--", str(executable), "--help"],
                   check=True, stdout=subprocess.DEVNULL, timeout=20)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"CSMS access provisioning failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
