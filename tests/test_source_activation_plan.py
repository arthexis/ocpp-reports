"""Role-based source activation inventory and history preservation gates."""
import sqlite3

import pytest

from ocpp_reports.source_activation_plan import inventory
from test_source_activation import _inputs


def test_fresh_satellite_plan_is_read_only(tmp_path):
    csms, warehouse, state = _inputs(tmp_path)
    report = inventory("satellite", csms, warehouse, state, "uuid-old", "node-a", "node-a")
    assert report["mode"] == "fresh"
    assert report["feed_tip"] == 1
    assert report["forwarder_ack"] is None
    assert report["apply_allowed"] is True
    assert report["requires_maintenance_authorization"] is True
    assert not state.exists()


@pytest.mark.parametrize("role", ["terminal"])
def test_non_producer_roles_refused(tmp_path, role):
    csms, warehouse, state = _inputs(tmp_path)
    with pytest.raises(ValueError, match="no local CSMS producer"):
        inventory(role, csms, warehouse, state, "uuid-old", "node-a", "node-a")


def test_watchtower_local_producer_plan_and_missing_producer_refusal(tmp_path):
    csms, warehouse, state = _inputs(tmp_path)
    report = inventory("watchtower", csms, warehouse, state, "uuid-old", "node-a", "node-a")
    assert report["apply_allowed"] is True
    assert report["role"] == "watchtower"
    with pytest.raises(ValueError, match="requires CSMS and warehouse"):
        inventory("watchtower", None, warehouse, state, "uuid-old", "node-a", "node-a")


def test_existing_forwarding_requires_collector_preflight(tmp_path):
    csms, warehouse, state = _inputs(tmp_path)
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY,evidence_source TEXT)")
        db.execute("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY,sequence INTEGER)")
        db.execute("INSERT INTO source_bindings VALUES('node-a','uuid-old')")
        db.execute("INSERT INTO forward_cursors VALUES('node-a',1)")
    report = inventory("satellite", csms, warehouse, state, "uuid-old", "node-a", "node-a")
    assert report["mode"] == "existing-history"
    assert report["forwarder_ack"] == 1
    assert report["apply_allowed"] is False
    assert report["requires_collector_preflight"] is True


def test_rejects_mismatched_binding_and_cursor(tmp_path):
    csms, warehouse, state = _inputs(tmp_path)
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY,evidence_source TEXT)")
        db.execute("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY,sequence INTEGER)")
        db.execute("INSERT INTO source_bindings VALUES('node-a','wrong')")
    with pytest.raises(ValueError, match="binding"):
        inventory("satellite", csms, warehouse, state, "uuid-old", "node-a", "node-a")
    with sqlite3.connect(state) as db:
        db.execute("UPDATE source_bindings SET evidence_source='uuid-old'")
        db.execute("INSERT INTO forward_cursors VALUES('node-a',999)")
    with pytest.raises(ValueError, match="cursor"):
        inventory("satellite", csms, warehouse, state, "uuid-old", "node-a", "node-a")


def test_active_transaction_denies_apply(tmp_path):
    csms, warehouse, state = _inputs(tmp_path)
    with sqlite3.connect(csms) as db:
        db.execute("INSERT INTO transactions VALUES(1,NULL)")
    report = inventory("satellite", csms, warehouse, state, "uuid-old", "node-a", "node-a")
    assert report["active_transactions"] == 1
    assert report["apply_allowed"] is False


def test_existing_binding_without_ack_still_requires_collector_evidence(tmp_path):
    csms, warehouse, state = _inputs(tmp_path)
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY,evidence_source TEXT)")
        db.execute("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY,sequence INTEGER)")
        db.execute("INSERT INTO source_bindings VALUES('node-a','uuid-old')")
    report = inventory("satellite", csms, warehouse, state, "uuid-old", "node-a", "node-a")
    assert report["mode"] == "existing-history"
    assert report["requires_collector_preflight"]
    assert report["apply_allowed"] is False
