"""Copy-only coordinated source identity conversion tests."""
import sqlite3

import pytest

from ocpp_reports.source_migration import migrate_copies


def init(tmp_path):
    csms = tmp_path / "csms.sqlite3"
    warehouse = tmp_path / "warehouse.sqlite3"
    forwarder = tmp_path / "forwarder.sqlite3"
    with sqlite3.connect(csms) as db:
        db.execute("CREATE TABLE appliance_metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO appliance_metadata VALUES ('source_id','uuid-old')")
    with sqlite3.connect(warehouse) as db:
        db.execute("CREATE TABLE sources (source_id TEXT PRIMARY KEY, cursor INTEGER)")
        db.execute("CREATE TABLE replication_changes (sequence INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, record_key TEXT, payload TEXT)")
        db.execute("INSERT INTO sources VALUES ('uuid-old', 135)")
        db.execute("INSERT INTO replication_changes VALUES (73,'uuid-old','event','23','{}')")
    with sqlite3.connect(forwarder) as db:
        db.execute("CREATE TABLE forward_cursors (source_id TEXT PRIMARY KEY, sequence INTEGER)")
        db.execute("CREATE TABLE source_bindings (node_id TEXT PRIMARY KEY, evidence_source TEXT)")
        db.execute("INSERT INTO forward_cursors VALUES ('gway-005', 73)")
        db.execute("INSERT INTO source_bindings VALUES ('gway-005', 'uuid-old')")
    return csms, warehouse, forwarder


def test_migrate_only_copies_without_losing_cursors(tmp_path):
    csms, warehouse, forwarder = init(tmp_path)
    dest = tmp_path / "candidates"
    files = migrate_copies(csms, warehouse, forwarder, dest, "uuid-old", "gway-005", "gway-005")
    with sqlite3.connect(files["csms"]) as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("gway-005",)
    with sqlite3.connect(files["warehouse"]) as db:
        assert db.execute("SELECT cursor FROM sources WHERE source_id='gway-005'").fetchone() == (135,)
        assert db.execute("SELECT sequence FROM replication_changes WHERE source_id='gway-005'").fetchone() == (73,)
    with sqlite3.connect(files["forwarder"]) as db:
        assert db.execute("SELECT sequence FROM forward_cursors").fetchone() == (73,)
        assert db.execute("SELECT evidence_source FROM source_bindings").fetchone() == ("gway-005",)
    with sqlite3.connect(csms) as db:
        assert db.execute("SELECT value FROM appliance_metadata").fetchone() == ("uuid-old",)
    with sqlite3.connect(warehouse) as db:
        assert db.execute("SELECT source_id FROM sources").fetchone() == ("uuid-old",)


def test_collision_refuses_and_removes_candidates(tmp_path):
    csms, warehouse, forwarder = init(tmp_path)
    with sqlite3.connect(warehouse) as db:
        db.execute("INSERT INTO sources VALUES ('gway-005', 0)")
    output = tmp_path / "candidates"
    with pytest.raises(ValueError, match="already exists"):
        migrate_copies(csms, warehouse, forwarder, output, "uuid-old", "gway-005", "gway-005")
    assert not output.exists()


def test_schema_versions_and_ddl_are_unchanged(tmp_path):
    csms, warehouse, forwarder = init(tmp_path)
    sources = {"csms": csms, "warehouse": warehouse, "forwarder": forwarder}
    before = {}
    for name, path in sources.items():
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=8")
            before[name] = (db.execute("PRAGMA user_version").fetchone()[0],
                            db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall())
    files = migrate_copies(csms, warehouse, forwarder, tmp_path / "copies", "uuid-old", "gway-005", "gway-005")
    for name, path in files.items():
        with sqlite3.connect(path) as db:
            after = (db.execute("PRAGMA user_version").fetchone()[0],
                     db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall())
        assert after == before[name]


def test_unknown_source_references_fail_closed(tmp_path):
    csms, warehouse, forwarder = init(tmp_path)
    with sqlite3.connect(warehouse) as db:
        db.execute("CREATE TABLE new_projection (source_id TEXT, value INTEGER)")
        db.execute("INSERT INTO new_projection VALUES ('uuid-old', 1)")
    output = tmp_path / "copies"
    with pytest.raises(ValueError, match="Unrecognized source references"):
        migrate_copies(csms, warehouse, forwarder, output, "uuid-old", "gway-005", "gway-005")
    assert not output.exists()
