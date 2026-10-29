"""Source maintenance must preserve other tenants and serialize with batches."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from ocpp_reports import collector_admin as admin
from ocpp_reports.replication import collector
from ocpp_reports.cli import app


def batch(source="gway-005", sequence=1, after=0, payload=None):
    return collector.Batch.model_validate({
        "schema": "ocpp-reports/replication/v1", "source_id": source,
        "after": after, "next": sequence, "more": False,
        "changes": [{"sequence": sequence, "kind": "event", "record_key": str(sequence),
                     "payload": payload or {"id": sequence}}],
    })


def seeded(tmp_path):
    path = tmp_path / "collector.sqlite"
    collector.accept_batch(path, batch(sequence=8))
    collector.accept_batch(path, batch("other-source", sequence=12))
    return path


def test_hold_persists_and_http_authentication_still_precedes_hold(tmp_path):
    path = seeded(tmp_path)
    assert admin.maintain(path, "gway-005", "hold")["held"]
    client = TestClient(collector.create_collector(path, {"gway-005": "secret", "other-source": "other"}))
    data = batch(sequence=9, after=8).model_dump(by_alias=True)
    assert client.post("/api/replication", json=data).status_code == 401
    response = client.post("/api/replication", json=data, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert admin.maintain(path, "gway-005", "status")["cursor"] == 8
    assert collector.accept_batch(path, batch("other-source", sequence=13, after=12)) == 13
    admin.maintain(path, "gway-005", "release")
    assert collector.accept_batch(path, batch(sequence=9, after=8)) == 9


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_reset_is_source_scoped_with_exact_verified_private_backup(tmp_path, journal_mode):
    path = seeded(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=" + journal_mode)
    admin.maintain(path, "gway-005", "hold")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=97")
        other_before = admin._digest(db, "gway-005")
        schema_before = admin._schema(db)
        all_before = admin._digest(db)
    backup = tmp_path / "backup.sqlite"
    result = admin.maintain(path, "gway-005", "reset", backup)
    assert result["backup_verified"] and result["other_sources_unchanged"]
    assert result["before"]["cursor"] == 8
    assert result["after"]["held"] and not any(result["after"]["rows"].values())
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as snapshot, sqlite3.connect(path) as db:
        assert admin._digest(snapshot) == all_before
        assert admin._schema(db) == schema_before
        assert admin._digest(db, "gway-005") == other_before
        assert snapshot.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with pytest.raises(collector.SourceHeldError):
        collector.accept_batch(path, batch(payload={"id": 1, "new": True}))
    admin.maintain(path, "gway-005", "release")
    fresh = batch(payload={"id": 1, "new": True})
    assert collector.accept_batch(path, fresh) == 1
    assert collector.accept_batch(path, fresh) == 1
    assert collector.accept_batch(path, batch(sequence=2, after=1)) == 2


def test_reset_requires_hold_new_backup_and_known_schema(tmp_path):
    path = seeded(tmp_path)
    backup = tmp_path / "backup.sqlite"
    with pytest.raises(ValueError, match="held"):
        admin.maintain(path, "gway-005", "reset", backup)
    admin.maintain(path, "gway-005", "hold")
    with pytest.raises(ValueError, match="backup"):
        admin.maintain(path, "gway-005", "reset")
    backup.write_text("preserve")
    with pytest.raises(FileExistsError):
        admin.maintain(path, "gway-005", "reset", backup)
    assert backup.read_text() == "preserve"
    assert admin.maintain(path, "gway-005", "status")["cursor"] == 8
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future_receipts(source_id TEXT, receipt TEXT)")
    with pytest.raises(ValueError, match="schema review"):
        admin.maintain(path, "gway-005", "reset", tmp_path / "new.sqlite")
    assert not (tmp_path / "new.sqlite").exists()


def test_post_delete_validation_failure_rolls_back_but_retains_backup(tmp_path, monkeypatch):
    path = seeded(tmp_path)
    admin.maintain(path, "gway-005", "hold")
    real_status = admin._status
    def fail_when_empty(db, source):
        result = real_status(db, source)
        if result["cursor"] is None:
            raise ValueError("injected validation failure")
        return result
    monkeypatch.setattr(admin, "_status", fail_when_empty)
    backup = tmp_path / "backup.sqlite"
    with pytest.raises(ValueError, match="injected"):
        admin.maintain(path, "gway-005", "reset", backup)
    assert backup.is_file()
    assert admin.maintain(path, "gway-005", "status")["cursor"] == 8


def test_hold_waits_for_inflight_transaction_then_blocks_subsequent_batch(tmp_path, monkeypatch):
    path = seeded(tmp_path)
    entered, finish, hold_started = threading.Event(), threading.Event(), threading.Event()
    real_canonical = collector._canonical
    def pause_in_transaction(value):
        entered.set()
        assert finish.wait(10)
        return real_canonical(value)
    monkeypatch.setattr(collector, "_canonical", pause_in_transaction)
    def hold():
        hold_started.set()
        return admin.maintain(path, "gway-005", "hold")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(collector.accept_batch, path, batch(sequence=9, after=8))
        assert entered.wait(10)
        held = pool.submit(hold)
        assert hold_started.wait(10)
        assert not held.done()
        finish.set()
        assert pending.result(timeout=10) == 9
        assert held.result(timeout=10)["cursor"] == 9
    with pytest.raises(collector.SourceHeldError):
        collector.accept_batch(path, batch(sequence=10, after=9))


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        admin.maintain(path, "gway-005", "hold")
    assert not path.exists()


def test_status_inspects_legacy_collector_without_schema_writes(tmp_path):
    path = seeded(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE collector_source_holds")
    before = path.read_bytes()
    state = admin.maintain(path, "gway-005", "status")
    assert state["cursor"] == 8 and not state["held"]
    assert path.read_bytes() == before


def test_public_cli_requires_explicit_source_and_supports_repeated_resets(tmp_path):
    path = seeded(tmp_path)
    runner = CliRunner()
    base = ["collector", "reset", "--database", str(path)]
    assert runner.invoke(app, base + ["--backup", str(tmp_path / "missing-source")]).exit_code == 2
    assert runner.invoke(app, base + ["--source", "gway-005"]).exit_code == 2
    for cycle in (1, 2):
        args = ["--database", str(path), "--source", "gway-005"]
        assert runner.invoke(app, ["collector", "hold", *args]).exit_code == 0
        backup = tmp_path / f"cycle-{cycle}.sqlite"
        result = runner.invoke(app, ["collector", "reset", *args, "--backup", str(backup)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["backup_verified"]
        status = runner.invoke(app, ["collector", "status", *args])
        assert status.exit_code == 0
        state = json.loads(status.output)
        assert state["held"] and not any(state["rows"].values())
        repeat = runner.invoke(app, ["collector", "reset", *args, "--backup", str(backup)])
        assert repeat.exit_code == 2  # Never overwrite an earlier rollback snapshot.
        assert runner.invoke(app, ["collector", "release", *args]).exit_code == 0
        assert collector.accept_batch(path, batch()) == 1
    assert admin.maintain(path, "other-source", "status")["cursor"] == 12
