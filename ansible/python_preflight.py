#!/usr/bin/env python3
"""Select a supported Reports Python before any deployment state is changed.

This helper must run on the host's Ansible Python (possibly 3.10), not on the
yet-to-be-selected Reports runtime. Only the selected runtime requires >=3.11.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

MINIMUM = (3, 11)
CANDIDATES = ("python3", "python3.13", "python3.12", "python3.11")


def inspect(executable):
    """Return absolute executable and minor version, or a useful error."""
    candidate = shutil.which(executable)
    if not candidate:
        raise ValueError(f"{executable!r} is not installed or executable")
    resolved = str(Path(candidate).resolve())
    check = subprocess.run(
        [resolved, "-c",
         "import json,sys,venv,ensurepip; print(json.dumps(list(sys.version_info[:3])))"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if check.returncode:
        raise ValueError(f"{resolved}: cannot load venv/ensurepip ({check.stderr.strip()})")
    try:
        version = tuple(json.loads(check.stdout.strip()))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{resolved}: invalid interpreter version output") from exc
    if version < MINIMUM:
        raise ValueError(f"{resolved}: Python {version[0]}.{version[1]} is unsupported (>=3.11 required)")
    return {"executable": resolved, "minor": f"{version[0]}.{version[1]}"}


def select(override=""):
    if override:
        return inspect(override)
    problems = []
    for candidate in CANDIDATES:
        try:
            return inspect(candidate)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{candidate}: {exc}")
    raise ValueError(
        "OCPP Reports requires Python >=3.11 with venv/ensurepip. "
        "Install a supported Python and its venv package or set "
        "OCPP_REPORTS_PYTHON=/absolute/path/to/python3.11. "
        + "; ".join(problems)
    )


def check_existing(path, expected_minor=None):
    """Reject invalid reusable environments; never repair or delete one."""
    executable = Path(path) / "bin" / "python"
    if not executable.is_file():
        raise ValueError(f"Incomplete venv {path}: missing bin/python; investigate before retrying")
    info = inspect(str(executable))
    if expected_minor and info["minor"] != expected_minor:
        raise ValueError(
            f"Existing venv {path} uses Python {info['minor']}, expected "
            f"{expected_minor}; do not reuse environments across minor versions"
        )
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-venv", metavar="DIR")
    parser.add_argument("--minor", help="Required Python minor for an existing venv")
    args = parser.parse_args(argv)
    try:
        if args.check_venv:
            info = check_existing(args.check_venv, args.minor)
        elif args.minor:
            parser.error("--minor requires --check-venv")
        else:
            info = select(os.getenv("OCPP_REPORTS_PYTHON", "").strip())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
