"""Explicit all-source dashboard inventory and tenant isolation regression tests."""
import shlex
from pathlib import Path

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.follow import sync_page
from test_web import empty_page


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "ansible" / "templates"


def render_scope(source_ids):
    environment = Environment(loader=FileSystemLoader(TEMPLATES), trim_blocks=True)
    environment.filters["quote"] = shlex.quote
    rendered = environment.get_template("ocpp-reports-dashboard-scope.conf.j2").render(
        reports_cli_link="/usr/local/bin/ocpp-reports",
        item={"name": "dashboard-admin", "port": 8083,
              "title": "Operator Administration", "source_ids": source_ids},
    )
    return shlex.split(rendered.split("ExecStart=", 2)[2].splitlines()[0])


def test_wildcard_admin_omits_source_allowlist():
    argv = render_scope("*")
    assert "--source-id" not in argv
    assert argv[argv.index("--port") + 1] == "8083"
    assert argv[argv.index("--title") + 1] == "Operator Administration"


def test_customer_array_remains_enforced_by_template():
    argv = render_scope(["source-b", "source-c"])
    assert [argv[i + 1] for i, x in enumerate(argv[:-1]) if x == "--source-id"] == [
        "source-b", "source-c"
    ]


def test_inventory_requires_explicit_wildcard_or_nonempty_allowlist():
    tasks = (ROOT / "ansible/tasks/dashboard-instances.yml").read_text()
    assert "item.source_ids == '*'" in tasks
    assert "item.source_ids is sequence and item.source_ids is not string" in tasks
    assert "item.source_ids | length > 0" in tasks


def test_admin_includes_later_sources_but_customer_isolation_remains(tmp_path, monkeypatch):
    database = tmp_path / "warehouse.sqlite"
    for source in ("source-a", "source-b"):
        page = empty_page()
        page["data"]["source_id"] = source
        page["data"]["status"]["chargers"][0]["id"] = "cp-" + source
        sync_page(database, page)

    served = []
    monkeypatch.setattr("uvicorn.run", lambda application, **kwargs: served.append(application))
    runner = CliRunner()
    base_args = ["serve", "--database", str(database)]

    admin_result = runner.invoke(app, base_args + ["--port", "8083", "--title", "Admin"])
    assert admin_result.exit_code == 0, admin_result.output
    admin = TestClient(served[-1])

    customer_result = runner.invoke(app, base_args + ["--source-id", "source-a"])
    assert customer_result.exit_code == 0, customer_result.output
    customer = TestClient(served[-1])

    def sources(client):
        response = client.get("/api/snapshot")
        assert response.status_code == 200
        return {row["source_id"] for row in response.json()["sources"]}

    assert sources(admin) == {"source-a", "source-b"}
    assert sources(customer) == {"source-a"}

    new_page = empty_page()
    new_page["data"]["source_id"] = "new-customer"
    new_page["data"]["status"]["chargers"][0]["id"] = "cp-new-customer"
    sync_page(database, new_page)

    assert sources(admin) == {"source-a", "source-b", "new-customer"}
    assert sources(customer) == {"source-a"}
    assert customer.get("/api/snapshot", params={"source_id": "new-customer"}).status_code == 403
    assert admin.get("/api/snapshot", params={"source_id": "new-customer"}).status_code == 200
