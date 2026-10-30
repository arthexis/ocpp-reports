"""Collector-owned display locations are optional, read-only to dashboards."""
import json
import sqlite3

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ocpp_reports.charger_locations import collector_database, manage
from ocpp_reports.cli import app
from ocpp_reports.replication.collector import accept_batch, Batch
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.web import create_app
from ocpp_reports.collector_admin import maintain


def batch(source, charger, number=1):
    return Batch.model_validate({
        "schema": "ocpp-reports/replication/v1", "source_id": source,
        "after": 0, "next": number, "more": False,
        "changes": [
            {"sequence": number, "kind": "connector", "record_key": f"{charger}:1",
             "payload": {"charger_id": charger, "connector_id": 1,
                         "status": "Available", "observed_at": "2026-10-30T12:00:00Z"}},
        ],
    })


def test_fallback_and_source_scoped_duplicate_charger(tmp_path):
    db = tmp_path / "collector.sqlite3"
    accept_batch(db, batch("node-a", "charger-1"))
    accept_batch(db, batch("node-b", "charger-1"))
    initial = snapshot(db)
    assert {(c["source_id"], c["location"]) for c in initial["connectors"]} == {
        ("node-a", "node-a"), ("node-b", "node-b")}
    assert manage(db, "set", source="node-a", charger="charger-1",
                  location="Audi San Pedro")["location"] == "Audi San Pedro"
    scoped = snapshot(db, allowed_source_ids=("node-a",))
    assert len(scoped["connectors"]) == 1
    assert scoped["connectors"][0]["location"] == "Audi San Pedro"
    assert snapshot(db, allowed_source_ids=("node-b",))["connectors"][0]["location"] == "node-b"
    assert len(manage(db, "list")) == 1
    assert manage(db, "list", source="node-b") == []
    client = TestClient(create_app(db, allowed_source_ids=("node-a",)))
    assert "Audi San Pedro" in client.get("/").text
    assert "Ubicación" in client.get("/").text
    assert client.get("/api/snapshot").json()["connectors"][0]["location"] == "Audi San Pedro"
    assert client.get("/api/snapshot?source_id=node-b").status_code == 403
    manage(db, "remove", source="node-a", charger="charger-1")
    assert snapshot(db, allowed_source_ids=("node-a",))["connectors"][0]["location"] == "node-a"


def test_metadata_survives_replication_reset(tmp_path):
    db = tmp_path / "collector.sqlite3"
    accept_batch(db, batch("node-a", "charger-1"))
    manage(db, "set", source="node-a", charger="charger-1", location="Garage A")
    manage(db, "set", source="node-b", charger="charger-1", location="Garage B")
    maintain(db, "node-a", "hold")
    result = maintain(db, "node-a", "reset", tmp_path / "backup.sqlite3")
    assert result["backup_verified"] and result["other_sources_unchanged"]
    assert manage(db, "list") == [
        {"source_id": "node-a", "charger_id": "charger-1", "location": "Garage A"},
        {"source_id": "node-b", "charger_id": "charger-1", "location": "Garage B"},
    ]


def test_cli_and_missing_warehouse_no_create(tmp_path):
    db = tmp_path / "collector.sqlite3"
    runner = CliRunner()
    args = ["--database", str(db)]
    assert runner.invoke(app, ["locations", "list", *args]).exit_code == 2
    assert runner.invoke(app, ["locations", "set", *args, "--source", "node-a",
                               "--charger", "cp", "--location", "Site A"]).exit_code == 2
    assert not db.exists()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sentinel (id INTEGER)")
    assert json.loads(runner.invoke(app, ["locations", "list", *args]).output) == []
    set_result = runner.invoke(app, ["locations", "set", *args, "--source", "node-a",
                                    "--charger", "cp", "--location", "Site A"])
    assert set_result.exit_code == 0, set_result.output
    assert json.loads(runner.invoke(app, ["locations", "list", *args]).output)[0]["location"] == "Site A"
    assert runner.invoke(app, ["locations", "remove", *args,
                               "--source", "node-a", "--charger", "cp"]).exit_code == 0
    assert json.loads(runner.invoke(app, ["locations", "list", *args]).output) == []


def test_location_input_rejects_controls_and_unbounded_labels(tmp_path):
    db = tmp_path / "collector.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sentinel (id INTEGER)")
    for label in ("", " ", "bad\nlabel", "x" * 161):
        try:
            manage(db, "set", source="node-a", charger="charger-1", location=label)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid location accepted")
    assert manage(db, "list") == []


def test_location_applies_to_transactions_and_events(tmp_path):
    db = tmp_path / "collector.sqlite3"
    record = Batch.model_validate({
        "schema": "ocpp-reports/replication/v1", "source_id": "node-a",
        "after": 0, "next": 3, "more": False,
        "changes": [
            {"sequence": 1, "kind": "connector", "record_key": "charger-1:1",
             "payload": {"charger_id": "charger-1", "connector_id": 1,
                         "status": "Available", "observed_at": "2026-10-30T12:00:00Z"}},
            {"sequence": 2, "kind": "transaction", "record_key": "charger-1:2",
             "payload": {"charger_id": "charger-1", "id": 2,
                         "started_at": "2026-10-30T12:00:00Z"}},
            {"sequence": 3, "kind": "event", "record_key": "3",
             "payload": {"charger_id": "charger-1", "id": 3, "action": "Heartbeat",
                         "at": "2026-10-30T12:00:00Z"}},
        ],
    })
    accept_batch(db, record)
    manage(db, "set", source="node-a", charger="charger-1", location="Site West")
    view = snapshot(db)
    assert view["connectors"][0]["location"] == "Site West"
    assert view["transactions"][0]["location"] == "Site West"
    assert view["events"][0]["location"] == "Site West"
    html = TestClient(create_app(db)).get("/").text
    assert "<th>Ubicación</th>" in html
    assert html.count("Site West") >= 3


def test_cli_infers_collector_database_from_installed_setting(tmp_path, monkeypatch):
    database = tmp_path / "warehouse.sqlite"
    accept_batch(database, batch("node-a", "charger-1"))
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(database))
    runner = CliRunner()
    result = runner.invoke(app, ["locations", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
    result = runner.invoke(app, ["locations", "set", "--source", "node-a",
                                 "--charger", "charger-1", "--location", "Site A"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["locations", "list"])
    assert json.loads(result.output)[0]["location"] == "Site A"
    result = runner.invoke(app, ["locations", "remove", "--source", "node-a",
                                 "--charger", "charger-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(runner.invoke(app, ["locations", "list"]).output) == []
    assert collector_database() == database.resolve()


def test_missing_or_unrelated_inferred_database_fails_closed(tmp_path, monkeypatch):
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(missing))
    result = CliRunner().invoke(app, ["locations", "list"])
    assert result.exit_code == 2
    assert not missing.exists()
    unrelated = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE other (id INTEGER)")
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(unrelated))
    result = CliRunner().invoke(app, ["locations", "set", "--source", "node-a",
                                     "--charger", "cp", "--location", "Site A"])
    assert result.exit_code == 2
    with sqlite3.connect(unrelated) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='collector_charger_locations'"
        ).fetchone() is None


def test_explicit_override_wins_over_inferred_path(tmp_path, monkeypatch):
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    accept_batch(first, batch("node-a", "charger-1"))
    accept_batch(second, batch("node-b", "charger-2"))
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(first))
    args = ["--database", str(second)]
    result = CliRunner().invoke(app, ["locations", "set", *args,
                                      "--source", "node-b", "--charger", "charger-2",
                                      "--location", "Second"])
    assert result.exit_code == 0, result.output
    assert manage(first, "list") == []
    assert manage(second, "list")[0]["location"] == "Second"
