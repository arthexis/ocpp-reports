"""Read-only local dashboard benchmark CLI and result validation."""
import json
import sqlite3

from typer.testing import CliRunner

from ocpp_reports.cli import app
from ocpp_reports.dashboard_benchmark import _percentile, benchmark
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def _warehouse(path):
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)


def test_percentile_nearest_rank():
    values = [4, 1, 3, 2]
    assert _percentile(values, 50) == 2
    assert _percentile(values, 95) == 4
    assert _percentile(values, 99) == 4


def test_benchmark_runs_without_modifying_warehouse(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    _warehouse(database)
    before = database.read_bytes()
    result = benchmark(database, iterations=2, warmup=0)
    assert result["schema"] == "ocpp-reports/benchmark/v1"
    assert set(result["scenarios"]) == {
        "dashboard", "transactions_page_2", "charger_filter",
        "api_snapshot", "snapshot_query",
    }
    for item in result["scenarios"].values():
        assert item["iterations"] == 2
        assert 0 <= item["p50_ms"] <= item["p95_ms"] <= item["p99_ms"] <= item["max_ms"]
    assert database.read_bytes() == before


def test_benchmark_cli_json_and_no_overwrite(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    _warehouse(database)
    output = tmp_path / "bench.json"
    runner = CliRunner()
    arguments = ["benchmark", "--database", str(database), "--iterations", "1",
                 "--warmup", "0", "--output", str(output), "--json"]
    first = runner.invoke(app, arguments)
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["schema"] == "ocpp-reports/benchmark/v1"
    assert json.loads(output.read_text())["iterations"] == 1
    second = runner.invoke(app, arguments)
    assert second.exit_code == 1
    assert "Benchmark failed" in second.output


def test_benchmark_missing_database_and_invalid_options(tmp_path):
    runner = CliRunner()
    missing = tmp_path / "no.sqlite"
    result = runner.invoke(app, ["benchmark", "--database", str(missing),
                                 "--iterations", "1", "--warmup", "0"])
    assert result.exit_code == 1
    assert not missing.exists()
    result = runner.invoke(app, ["benchmark", "--iterations", "0"])
    assert result.exit_code != 0
