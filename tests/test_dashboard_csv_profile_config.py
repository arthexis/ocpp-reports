"""Per-instance CSV profile propagation uses existing inventory and auth scope."""
import shlex
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "ansible" / "templates"


def _render(item):
    environment = Environment(loader=FileSystemLoader(TEMPLATES), trim_blocks=True)
    environment.filters["quote"] = shlex.quote
    return environment.get_template("ocpp-reports-dashboard-scope.conf.j2").render(
        item=item, reports_cli_link="/usr/local/bin/ocpp-reports",
    )


def test_per_instance_csv_profile_is_independent_of_tenant_scope():
    base = dict(name="dashboard-porsche", port=8080, title="Porsche",
                source_ids=["gway-005", "porsche-abb-1"])
    porsche = _render({**base, "csv_profile": "vendor"})
    audi = _render({**base, "name": "dashboard-audi", "port": 8082,
                    "title": "Audi", "source_ids": ["gway-002"],
                    "csv_profile": "vendor-csv"})
    assert "--csv-profile vendor " in porsche
    assert "--csv-profile vendor-csv " in audi
    assert "--source-id gway-005 --source-id porsche-abb-1" in porsche
    assert "--source-id gway-002" in audi
    assert "gway-005" not in audi
    assert "--csv-profile vendor " in _render(base)  # safe default
