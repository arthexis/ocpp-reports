"""SQLite report generation integration tests."""
import json
from pathlib import Path
from typer.testing import CliRunner
from ocpp_reports.cli import app
from ocpp_reports.ingest import ingest_page

FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"

def fixture(name):
    return json.loads((FIXTURES / name).read_text())

def test_sqlite_generate_late_stop_and_repeatable_revision(tmp_path):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, fixture("active.json"))
    ingest_page(db, fixture("late_stop.json"))
    args = ["generate", "--source", str(db), "--source-id", "fixture-source-a",
            "--since", "2026-01-01T00:00:00Z", "--until", "2026-01-02T00:00:00Z"]
    results = []
    for name in ("one.json", "two.json"):
        output = tmp_path / name
        result = CliRunner().invoke(app, args + ["--output", str(output)])
        assert result.exit_code == 0, result.output
        results.append(json.loads(output.read_text()))
    assert results[0] == results[1]
    assert results[0]["summary"]["transactions"] == 1
    assert results[0]["summary"]["known_energy_wh"] == 500
    assert results[0]["transactions"][0]["active"] is False

def test_sqlite_generate_missing_energy_is_partial(tmp_path):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, fixture("active.json"))
    output = tmp_path / "partial.json"
    result = CliRunner().invoke(app, ["generate", "--source", str(db),
        "--source-id", "fixture-source-a", "--since", "2026-01-01T00:00:00Z",
        "--until", "2026-01-02T00:00:00Z", "--output", str(output)])
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["summary"]["missing_energy_transactions"] == 1
    assert report["summary"]["completeness"] == "partial"
    assert report["transactions"][0]["energy_wh"] is None
