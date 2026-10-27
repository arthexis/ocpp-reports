"""Integrity regression tests: atomic rollback, identity binding and replay safety."""
import copy
import sqlite3

import pytest

from ocpp_reports.follow import sync_page, source_cursor, current_connectors
from ocpp_reports.forwarder import verify_source_binding
from ocpp_reports.collector import Batch, accept_batch


def page():
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "uuid-001", "exported_at": "2026-10-27T12:00:00Z",
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "cp", "connected": True, "connectors": [
            {"id": 1, "status": "Available", "transaction": None}
        ]}]},
    }}


def test_bad_connector_rolls_back_cursor_and_evidence(tmp_path):
    db = tmp_path / "warehouse.sqlite3"
    first = page()
    sync_page(db, first)
    bad = copy.deepcopy(first)
    bad["data"]["status"]["chargers"][0]["connectors"][0]["id"] = None
    bad["data"]["exported_at"] = "2026-10-27T12:00:05Z"
    with pytest.raises(sqlite3.IntegrityError):
        sync_page(db, bad)
    assert source_cursor(db, "uuid-001") == 0
    assert current_connectors(db)[0]["observed_at"] == "2026-10-27T12:00:00Z"


def test_forward_identity_binding_rejects_unplanned_migration(tmp_path):
    state = tmp_path / "forward.sqlite3"
    verify_source_binding(state, "gway-005", "uuid-old")
    verify_source_binding(state, "gway-005", "uuid-old")
    with pytest.raises(ValueError, match="explicit maintenance migration"):
        verify_source_binding(state, "gway-005", "gway-005")
    with sqlite3.connect(state) as db:
        assert db.execute(
            "SELECT evidence_source FROM source_bindings WHERE node_id='gway-005'"
        ).fetchone()[0] == "uuid-old"


def test_conflicting_collector_replay_preserves_cursor(tmp_path):
    db = tmp_path / "collector.sqlite3"
    def batch(value):
        return Batch.model_validate({
            "schema": "ocpp-reports/replication/v1", "source_id": "gway-005",
            "after": 0, "next": 1, "more": False,
            "changes": [{"sequence": 1, "kind": "event",
                         "record_key": "1", "payload": {"value": value}}],
        })
    assert accept_batch(db, batch("original")) == 1
    with pytest.raises(ValueError, match="Conflicting replay"):
        accept_batch(db, batch("changed"))
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT sequence FROM collector_cursors").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM collector_changes").fetchone()[0] == 1
