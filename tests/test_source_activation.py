"""Stopped-service activation keeps a complete rollback boundary."""
import os
import sqlite3

import pytest

from ocpp_reports import source_activation as activation


def _inputs(tmp_path):
    csms = tmp_path / "csms.sqlite3"
    warehouse = tmp_path / "warehouse.sqlite3"
    state = tmp_path / "forwarder.sqlite3"
    with sqlite3.connect(csms) as db:
        db.execute("CREATE TABLE appliance_metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO appliance_metadata VALUES('source_id','uuid-old')")
        db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,payload TEXT)")
        db.execute("INSERT INTO events VALUES(1,'retained')")
        db.execute("CREATE TABLE transactions(id INTEGER PRIMARY KEY,stopped_at TEXT)")
        db.execute("PRAGMA user_version=8")
    with sqlite3.connect(warehouse) as db:
        db.execute("CREATE TABLE sources(source_id TEXT PRIMARY KEY,cursor INTEGER)")
        db.execute("CREATE TABLE replication_changes(sequence INTEGER PRIMARY KEY,source_id TEXT,kind TEXT,record_key TEXT,payload TEXT)")
        db.execute("INSERT INTO sources VALUES('uuid-old',1)")
        db.execute("INSERT INTO replication_changes VALUES(1,'uuid-old','event','1','{}')")
    return csms, warehouse, state


def test_activation_preserves_history_and_initializes_binding(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    receipt = activation.activate(csms, warehouse, state, tmp_path / "backup",
                                  "uuid-old", "gway-002", "gway-002")
    assert receipt["source_id"] == "gway-002"
    with sqlite3.connect(csms) as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("gway-002",)
        assert db.execute("SELECT payload FROM events").fetchone() == ("retained",)
        assert db.execute("PRAGMA user_version").fetchone() == (8,)
    with sqlite3.connect(warehouse) as db:
        assert db.execute("SELECT source_id,cursor FROM sources").fetchone() == ("gway-002", 1)
        assert db.execute("SELECT sequence,source_id FROM replication_changes").fetchone() == (1, "gway-002")
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT * FROM source_bindings").fetchone() == ("gway-002", "gway-002")
        assert db.execute("SELECT count(*) FROM forward_cursors").fetchone() == (0,)
    with sqlite3.connect(tmp_path / "backup" / "csms.sqlite3") as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("uuid-old",)


def test_activation_failure_restores_first_replacement(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    real_replace = os.replace
    def fail_warehouse(source, target):
        if str(target) == str(warehouse) and str(source).endswith("source-activation-new"):
            raise OSError("interrupted installation")
        return real_replace(source, target)
    monkeypatch.setattr(activation.os, "replace", fail_warehouse)
    with pytest.raises(OSError, match="interrupted installation"):
        activation.activate(csms, warehouse, state, tmp_path / "backup",
                            "uuid-old", "gway-002", "gway-002")
    with sqlite3.connect(csms) as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("uuid-old",)
        assert db.execute("SELECT payload FROM events").fetchone() == ("retained",)
    with sqlite3.connect(warehouse) as db:
        assert db.execute("SELECT source_id FROM sources").fetchone() == ("uuid-old",)
    assert not state.exists()


def test_refuses_running_service_before_any_backup(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: (_ for _ in ()).throw(ValueError("running")))
    with pytest.raises(ValueError, match="running"):
        activation.activate(csms, warehouse, state, tmp_path / "backup",
                            "uuid-old", "gway-002", "gway-002")
    assert not (tmp_path / "backup").exists()


def test_refuses_active_transaction_before_any_backup(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    with sqlite3.connect(csms) as db:
        db.execute("INSERT INTO transactions VALUES(1,NULL)")
    with pytest.raises(ValueError, match="Active CSMS transactions"):
        activation.activate(csms, warehouse, state, tmp_path / "backup",
                            "uuid-old", "gway-002", "gway-002")
    assert not (tmp_path / "backup").exists()


def test_control_activation_does_not_create_forwarder(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    result = activation.activate(csms, warehouse, state, tmp_path / "control-backup",
                                 "uuid-old", "node-a", "node-a", role="control")
    assert result["converted"] == ("csms", "warehouse")
    assert not state.exists()
    with sqlite3.connect(csms) as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("node-a",)
    assert (tmp_path / "control-backup" / "recovery.json").exists()


def test_crash_recovery_restores_stopped_snapshot(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    backup = tmp_path / "recovery-backup"
    activation.activate(csms, warehouse, state, backup, "uuid-old", "node-a", "node-a")
    # Simulate an interrupted activation using its verified pre-replacement files.
    activation._journal(backup, "installing", {"csms": csms, "warehouse": warehouse,
                                            "forwarder": state}, touched=["csms"])
    result = activation.recover(backup)
    assert result["status"] == "recovered"
    with sqlite3.connect(csms) as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("uuid-old",)
    with sqlite3.connect(warehouse) as db:
        assert db.execute("SELECT source_id FROM sources").fetchone() == ("uuid-old",)
    assert not state.exists()
    assert activation.recover(backup)["status"] == "recovered"


def test_recovery_refuses_completed_cutover(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    backup = tmp_path / "complete"
    activation.activate(csms, warehouse, state, backup, "uuid-old", "node-a", "node-a")
    with pytest.raises(ValueError, match="Completed conversion"):
        activation.recover(backup)


def test_existing_forwarder_refuses_without_collector_snapshot(tmp_path, monkeypatch):
    csms, warehouse, state = _inputs(tmp_path)
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY,evidence_source TEXT)")
        db.execute("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY,sequence INTEGER)")
        db.execute("INSERT INTO source_bindings VALUES('node-a','uuid-old')")
        db.execute("INSERT INTO forward_cursors VALUES('node-a',1)")
    monkeypatch.setattr(activation, "require_stopped", lambda: None)
    with pytest.raises(ValueError, match="collector snapshot"):
        activation.activate(csms, warehouse, state, tmp_path / "backup",
                            "uuid-old", "node-a", "node-a")
    assert not (tmp_path / "backup").exists()
