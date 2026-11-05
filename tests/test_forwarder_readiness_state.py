"""Read-only readiness exemption must never accept a different identity."""
import sqlite3

from ocpp_reports.forwarder_readiness_state import established_source


def test_established_identity_requires_matching_ack_and_binding(tmp_path):
    state = tmp_path / "forwarder.sqlite"
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL)")
        db.execute("CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY, evidence_source TEXT NOT NULL)")
        db.execute("INSERT INTO forward_cursors VALUES ('site-a', 42)")
        db.execute("INSERT INTO source_bindings VALUES ('site-a', 'producer-uuid')")
    before = state.read_bytes()
    assert established_source(state, "site-a")
    assert not established_source(state, "site-b")
    assert state.read_bytes() == before


def test_missing_or_partial_state_fails_closed(tmp_path):
    state = tmp_path / "forwarder.sqlite"
    assert not established_source(state, "site-a")
    assert not state.exists()
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL)")
        db.execute("INSERT INTO forward_cursors VALUES ('site-a', 5)")
    assert not established_source(state, "site-a")
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY, evidence_source TEXT NOT NULL)")
        db.execute("INSERT INTO source_bindings VALUES ('site-b', 'producer-uuid')")
    assert not established_source(state, "site-a")


def test_corrupt_state_fails_closed(tmp_path):
    state = tmp_path / "forwarder.sqlite"
    state.write_text("not a sqlite database")
    assert not established_source(state, "site-a")
