#!/usr/bin/env python3
"""Exercise a disposable CSMS/reader/ingest stop-start transaction under real systemd.

Runs on the isolated CI runner with root access. Never stops installed CSMS or
Reports services: all fixtures have unique ocpp-reports-ci-* unit names.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIRECTORY = Path("/run/systemd/system")


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *args], check=check, text=True, capture_output=True)


def await_state(unit: str, wanted: bool) -> None:
    for _ in range(40):
        active = systemctl("is-active", "--quiet", unit, check=False).returncode == 0
        if active == wanted:
            return
        time.sleep(0.25)
    details = systemctl("status", "--no-pager", unit, check=False).stdout
    raise AssertionError(f"{unit}: expected active={wanted}; status:\n{details}")


def unit_section(path: str, replacements: dict[str, str]) -> str:
    source = (ROOT / path).read_text()
    section = source.split("[Unit]", 1)[1].split("[Service]", 1)[0]
    for old, new in replacements.items():
        section = section.replace(old, new)
    if "{{" in section:
        raise AssertionError(f"Unresolved template variable in {path}")
    return "[Unit]" + section


def main() -> None:
    if os.geteuid() != 0 or not UNIT_DIRECTORY.is_dir():
        raise RuntimeError("Requires isolated Linux CI systemd runner and root access")

    suffix = uuid.uuid4().hex[:10]
    csms = f"ocpp-reports-ci-{suffix}-csms.service"
    reader = f"ocpp-reports-ci-{suffix}-reader.service"
    ingest = f"ocpp-reports-ci-{suffix}-ingest.service"
    replace = {
        "ocpp-reports-csms-reader.service": reader,
        "ocpp-reports-ingest.service": ingest,
        "ocpp-csms.service": csms,
        "{{ reports_local_csms_data_dir }}": "/run",
    }
    template = (ROOT / "ansible/templates/ocpp-reports-ingest.service.j2").read_text()
    assert "ocpp-csms.service" in template.split("[Install]", 1)[1].split("WantedBy=", 1)[1]
    names = (csms, reader, ingest)
    files = [UNIT_DIRECTORY / name for name in names]
    wants = UNIT_DIRECTORY / (csms + ".wants")
    link = wants / ingest

    def fixture(unit: str, content: str) -> None:
        (UNIT_DIRECTORY / unit).write_text(
            content + "\n[Service]\nType=simple\nExecStart=/usr/bin/sleep infinity\n"
        )

    try:
        fixture(csms, "[Unit]\nDescription=CI isolated fake CSMS\n")
        fixture(
            reader,
            unit_section("ansible/templates/ocpp-reports-csms-reader.service.j2", replace),
        )
        fixture(
            ingest,
            unit_section("ansible/templates/ocpp-reports-ingest.service.j2", replace),
        )
        wants.mkdir()
        link.symlink_to(UNIT_DIRECTORY / ingest)
        systemctl("daemon-reload")
        systemctl("start", csms)
        for name in names:
            await_state(name, True)
        assert systemctl("is-enabled", ingest).stdout.strip() in ("enabled", "enabled-runtime", "linked", "linked-runtime")

        # A clean dependency stop is not a crash. The wanted child must be
        # started again by the subsequent producer start, not Restart=on-failure.
        systemctl("stop", csms)
        for name in names:
            await_state(name, False)
        systemctl("start", csms)
        for name in names:
            await_state(name, True)

        # Simulate the explicit opt-out link removal and service disable.
        # Unrelated CSMS starts must no longer bring the Reports ingest back.
        systemctl("stop", ingest)
        link.unlink()
        systemctl("daemon-reload")
        systemctl("stop", csms)
        systemctl("start", csms)
        await_state(csms, True)
        await_state(ingest, False)
        print("PASS: clean stop/start recovers ingest; explicit link removal prevents reactivation")
    finally:
        for name in reversed(names):
            systemctl("stop", name, check=False)
        link.unlink(missing_ok=True)
        try:
            wants.rmdir()
        except OSError:
            pass
        for path in files:
            path.unlink(missing_ok=True)
        systemctl("daemon-reload", check=False)


if __name__ == "__main__":
    main()
