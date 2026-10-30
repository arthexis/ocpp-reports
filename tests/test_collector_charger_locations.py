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
    assert runner.invoke(app, ["locations", "list", "--json", *args]).exit_code == 2
    assert runner.invoke(app, ["locations", "set", "--json", *args, "--source", "node-a",
                               "--charger", "cp", "--name", "Site A"]).exit_code == 2
    assert not db.exists()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sentinel (id INTEGER)")
    assert json.loads(runner.invoke(app, ["locations", "list", "--json", *args]).output) == []
    set_result = runner.invoke(app, ["locations", "set", "--json", *args, "--source", "node-a",
                                    "--charger", "cp", "--name", "Site A"])
    assert set_result.exit_code == 0, set_result.output
    assert json.loads(runner.invoke(app, ["locations", "list", "--json", *args]).output)[0]["location"] == "Site A"
    assert runner.invoke(app, ["locations", "remove", "--json", *args,
                               "--source", "node-a", "--charger", "cp"]).exit_code == 0
    assert json.loads(runner.invoke(app, ["locations", "list", "--json", *args]).output) == []


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
    result = runner.invoke(app, ["locations", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"source_id": "node-a", "charger_id": "charger-1",
         "location": None, "assigned": False}
    ]
    result = runner.invoke(app, ["locations", "set", "--json", "--source", "node-a",
                                 "--charger", "charger-1", "--name", "Site A"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["locations", "list", "--json"])
    assert json.loads(result.output)[0]["location"] == "Site A"
    result = runner.invoke(app, ["locations", "remove", "--json", "--source", "node-a",
                                 "--charger", "charger-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(runner.invoke(app, ["locations", "list", "--json"]).output) == [
        {"source_id": "node-a", "charger_id": "charger-1",
         "location": None, "assigned": False}
    ]
    assert collector_database() == database.resolve()


def test_missing_or_unrelated_inferred_database_fails_closed(tmp_path, monkeypatch):
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(missing))
    result = CliRunner().invoke(app, ["locations", "list", "--json"])
    assert result.exit_code == 2
    assert not missing.exists()
    unrelated = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE other (id INTEGER)")
    monkeypatch.setenv("OCPP_REPORTS_DATABASE", str(unrelated))
    result = CliRunner().invoke(app, ["locations", "set", "--json", "--source", "node-a",
                                     "--charger", "cp", "--name", "Site A"])
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
    result = CliRunner().invoke(app, ["locations", "set", "--json", *args,
                                      "--source", "node-b", "--charger", "charger-2",
                                      "--name", "Second"])
    assert result.exit_code == 0, result.output
    assert manage(first, "list") == []
    assert manage(second, "list")[0]["location"] == "Second"


def test_infer_source_from_charger_and_charger_from_source(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    accept_batch(db, batch("node-a", "charger-a"))
    accept_batch(db, batch("node-b", "charger-b"))
    runner = CliRunner()
    base = ["--database", str(db)]
    inferred_source = runner.invoke(app, ["locations", "set", "--json", *base,
                                         "--charger", "charger-a", "--name", "A"])
    assert inferred_source.exit_code == 0, inferred_source.output
    assert json.loads(inferred_source.output)["source_id"] == "node-a"
    inferred_charger = runner.invoke(app, ["locations", "set", "--json", *base,
                                          "--source", "node-b", "--name", "B"])
    assert inferred_charger.exit_code == 0, inferred_charger.output
    assert json.loads(inferred_charger.output)["charger_id"] == "charger-b"
    assert [row["location"] for row in manage(db, "list")] == ["A", "B"]
    removed = runner.invoke(app, ["locations", "remove", "--json", *base, "--source", "node-b"])
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.output)["charger_id"] == "charger-b"
    removed = runner.invoke(app, ["locations", "remove", "--json", *base, "--charger", "charger-a"])
    assert removed.exit_code == 0, removed.output
    assert manage(db, "list") == []


def test_refuse_ambiguous_and_missing_inference_without_writes(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    accept_batch(db, batch("node-a", "same"))
    accept_batch(db, batch("node-b", "same"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO collector_latest(source_id,kind,record_key,sequence,payload)
               VALUES ('node-a','connector','other:1',2,?)""",
            (json.dumps({"charger_id": "other", "connector_id": 1}),),
        )
    runner = CliRunner()
    base = ["--database", str(db)]
    for args in (
        ["--charger", "same"],  # multiple source IDs
        ["--source", "node-a"],  # multiple charger IDs
        ["--charger", "missing"],  # no matches
        ["--source", "missing"],  # no matches
        [],  # never infer both
    ):
        result = runner.invoke(app, ["locations", "set", "--json", *base, *args, "--name", "Should not write"])
        assert result.exit_code == 2, (args, result.output)
        assert "Location assignment refused:" in result.output
        if args in (["--charger", "same"], ["--source", "node-a"]):
            assert "Ambiguous" in result.output and "specify it explicitly" in result.output
    assert manage(db, "list") == []


def test_inference_from_local_connector_and_persisted_label_after_reset(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    from ocpp_reports.follow import sync_page
    page = {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "local-1", "exported_at": "2026-10-30T00:00:00Z",
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "local-cp", "connected": True,
                                "connectors": [{"id": 1, "status": "Available",
                                                "transaction": None}]}]}}}
    sync_page(db, page)
    result = CliRunner().invoke(app, ["locations", "set", "--json", "--database", str(db),
                                      "--charger", "local-cp", "--name", "Local Garage"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source_id"] == "local-1"

    remote = tmp_path / "remote.sqlite"
    accept_batch(remote, batch("node-a", "remote-cp"))
    manage(remote, "set", source="node-a", charger="remote-cp", location="Remote Garage")
    maintain(remote, "node-a", "hold")
    maintain(remote, "node-a", "reset", tmp_path / "backup.sqlite")
    # No connector evidence remains, but the existing label is still removable.
    removed = CliRunner().invoke(app, ["locations", "remove", "--json", "--database", str(remote),
                                       "--charger", "remote-cp"])
    assert removed.exit_code == 0, removed.output
    assert manage(remote, "list") == []


def test_name_is_required_and_old_location_option_not_accepted(tmp_path):
    db = tmp_path / "collector.sqlite3"
    accept_batch(db, batch("node-a", "charger-1"))
    runner = CliRunner()
    base = ["locations", "set", "--json", "--database", str(db),
            "--source", "node-a", "--charger", "charger-1"]
    old = runner.invoke(app, [*base, "--location", "Legacy"])
    assert old.exit_code != 0
    assert manage(db, "list") == []
    new = runner.invoke(app, [*base, "--name", "New Label"])
    assert new.exit_code == 0, new.output
    assert manage(db, "list")[0]["location"] == "New Label"


def test_sqlite_write_failures_show_actionable_guidance(monkeypatch, tmp_path):
    from ocpp_reports import charger_locations
    db = tmp_path / "collector.sqlite3"
    accept_batch(db, batch("node-a", "charger-1"))
    actual_manage = charger_locations.manage

    def denied(database, action, **kwargs):
        if action in ("set", "remove"):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return actual_manage(database, action, **kwargs)

    monkeypatch.setattr(charger_locations, "manage", denied)
    runner = CliRunner()
    result = runner.invoke(app, ["locations", "set", "--json", "--database", str(db),
                                 "--source", "node-a", "--name", "Site"])
    assert result.exit_code == 2
    assert "not writable by this user" in result.output
    assert "ocpp-reports service account" in result.output
    assert actual_manage(db, "list") == []
    result = runner.invoke(app, ["locations", "remove", "--json", "--database", str(db),
                                 "--source", "node-a"])
    assert result.exit_code == 2
    assert "not writable by this user" in result.output


def test_locked_sqlite_has_distinct_message(monkeypatch, tmp_path):
    from ocpp_reports import charger_locations
    db = tmp_path / "collector.sqlite3"
    accept_batch(db, batch("node-a", "charger-1"))

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(charger_locations, "manage", busy)
    result = CliRunner().invoke(app, ["locations", "set", "--json",
                                      "--database", str(db), "--charger", "charger-1",
                                      "--name", "Site"])
    assert result.exit_code == 2
    assert "busy/locked" in result.output


def test_other_sqlite_error_reports_actual_cause(monkeypatch, tmp_path):
    from ocpp_reports import charger_locations
    db = tmp_path / "collector.sqlite3"
    accept_batch(db, batch("node-a", "charger-1"))

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such column: charger_id")

    monkeypatch.setattr(charger_locations, "manage", broken)
    result = CliRunner().invoke(app, ["locations", "set", "--json", "--database", str(db),
                                      "--charger", "charger-1", "--name", "Site"])
    assert result.exit_code == 2
    assert "no such column: charger_id" in result.output


def test_human_readable_inventory_and_set_unset_filters(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    accept_batch(db, batch("node-a", "charger-a"))
    accept_batch(db, batch("node-b", "charger-b"))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO collector_cursors (source_id,sequence) VALUES ('source-no-charger',0)")
    runner = CliRunner()
    base = ["locations", "list", "--database", str(db)]
    initial = runner.invoke(app, base)
    assert initial.exit_code == 0, initial.output
    assert "Source" in initial.output and "Charger" in initial.output and "Name" in initial.output
    assert "charger-a" in initial.output and "charger-b" in initial.output
    assert "source-no-charger" in initial.output and "(not observed)" in initial.output
    assert "(unassigned)" in initial.output and not initial.output.startswith("[")
    rows = json.loads(runner.invoke(app, [*base, "--json"]).output)
    assert len(rows) == 3
    assert all(r["assigned"] is False for r in rows)
    assert next(r for r in rows if r["source_id"] == "source-no-charger")["charger_id"] is None
    assert runner.invoke(app, [*base, "--set"]).output == "No matching sources or chargers.\\n"
    assignment = runner.invoke(app, ["locations", "set", "--database", str(db),
                                     "--source", "node-a", "--name", "Audi Calzada"])
    assert assignment.exit_code == 0, assignment.output
    assert "Named node-a / charger-a: Audi Calzada" in assignment.output
    assert not assignment.output.startswith("{")
    assert "charger-a" in runner.invoke(app, [*base, "--set"]).output
    assert "charger-b" not in runner.invoke(app, [*base, "--set"]).output
    unassigned = json.loads(runner.invoke(app, [*base, "--unset", "--json"]).output)
    assert {(r["source_id"], r["charger_id"]) for r in unassigned} == {
        ("node-b", "charger-b"), ("source-no-charger", None)}
    assert runner.invoke(app, [*base, "--set", "--unset"]).exit_code == 2
    filtered = json.loads(runner.invoke(app, [*base, "--source", "node-a", "--json"]).output)
    assert len(filtered) == 1 and filtered[0]["location"] == "Audi Calzada"
    removed = runner.invoke(app, ["locations", "remove", "--database", str(db),
                                  "--source", "node-a"])
    assert removed.exit_code == 0, removed.output
    assert "Cleared name for node-a / charger-a" in removed.output
    assert "charger-a" in runner.invoke(app, [*base, "--unset"]).output


def test_inventory_deduplicates_connectors_and_persists_saved_orphans(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    accept_batch(db, batch("node-a", "one"))
    from ocpp_reports.charger_locations import list_inventory
    with sqlite3.connect(db) as conn:
        conn.execute("""INSERT INTO collector_latest
            (source_id,kind,record_key,sequence,payload)
            VALUES ('node-a','connector','one:2',2,?)""",
            (json.dumps({"charger_id": "one", "connector_id": 2}),))
    rows = list_inventory(db)
    assert len(rows) == 1 and rows[0]["charger_id"] == "one"
    manage(db, "set", source="other", charger="orphan", location="Saved")
    rows = list_inventory(db)
    assert len(rows) == 2
    assert any(r["source_id"] == "other" and r["location"] == "Saved" for r in rows)
    assert list_inventory(db, assigned=True) == [
        {"source_id": "other", "charger_id": "orphan", "location": "Saved", "assigned": True}
    ]
