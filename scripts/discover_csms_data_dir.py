"""Read the active CSMS data directory without assuming its installation root."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def data_dir_from_argv(argv: list[str]) -> Path:
    matches = []
    for index, arg in enumerate(argv):
        if arg == "--data-dir":
            if index + 1 >= len(argv):
                raise ValueError("CSMS --data-dir has no value")
            matches.append(argv[index + 1])
        elif arg.startswith("--data-dir="):
            matches.append(arg.partition("=")[2])
    if len(matches) != 1 or not matches[0]:
        raise ValueError("Expected exactly one explicit CSMS --data-dir argument")
    path = Path(matches[0])
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("CSMS data directory must be absolute and normalized")
    return path


def active_data_dir() -> Path:
    pid_text = subprocess.check_output(
        ["systemctl", "show", "ocpp-csms.service", "--property=MainPID", "--value", "--no-pager"],
        text=True,
    ).strip()
    if not pid_text.isdecimal() or int(pid_text) <= 0:
        raise ValueError("No active CSMS MainPID")
    argv = [
        part.decode("utf-8")
        for part in Path(f"/proc/{int(pid_text)}/cmdline").read_bytes().split(b"\0")
        if part
    ]
    if not argv:
        raise ValueError("Active CSMS process has no command line")
    return data_dir_from_argv(argv)


def main() -> int:
    try:
        print(active_data_dir())
    except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"Unable to discover active CSMS --data-dir: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
