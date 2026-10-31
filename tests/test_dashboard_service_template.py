"""Protect systemd unit rendering from Jinja whitespace-trimming regressions."""
from pathlib import Path
import shlex

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "ansible" / "templates"


@pytest.mark.parametrize("zones", [[], ["America/Monterrey"], ["America/Monterrey", "UTC"]])
def test_dashboard_service_execstart_stays_separate_from_restart(zones):
    # Ansible enables trim_blocks: an endfor at end-of-line otherwise consumes
    # the newline, appending Restart=on-failure to the --port argument.
    environment = Environment(loader=FileSystemLoader(TEMPLATES), trim_blocks=True)
    environment.filters["quote"] = shlex.quote
    unit = environment.get_template("ocpp-reports-dashboard.service.j2").render(
        reports_cli_link="/usr/local/bin/ocpp-reports-ci",
        reports_dashboard_timezones=zones,
    )
    lines = unit.splitlines()
    exec_lines = [line for line in lines if line.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert "Restart=on-failure" in lines
    assert "RestartSec=5" in lines
    assert "Restart=" not in exec_lines[0]
    argv = shlex.split(exec_lines[0].removeprefix("ExecStart="))
    assert argv[:9] == [
        "/usr/local/bin/ocpp-reports-ci", "serve", "--database",
        "/var/lib/ocpp-reports/warehouse.sqlite", "--host", "127.0.0.1",
        "--port", "8080",
    ][:9]
    assert argv[8:] == [item for zone in zones for item in ("--timezone", zone)]
