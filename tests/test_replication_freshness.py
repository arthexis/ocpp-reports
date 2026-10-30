"""Regression coverage for distinct collector receipt and charger status clocks."""
import json
import sqlite3

from ocpp_reports.replication.collector import Batch, accept_batch
from ocpp_reports.warehouse.queries import snapshot


def _batch(after, next_value):
    return Batch.model_validate({
        "schema": "ocpp-reports/replication/v1", "source_id": "node-a",
        "after": after, "next": next_value, "more": False,
        "changes": [{"sequence": next_value, "kind": "connector",
                     "record_key": "charger:1",
                     "payload": {"charger_id": "charger", "connector_id": 1,
                                 "status": "Faulted", "observed_at": "2026-10-30T06:00:00Z",
                                 "status_observed_at": "2026-10-29T21:35:00Z"}}],
    })


def test_collector_receipt_advances_only_new_evidence(tmp_path):
    path = tmp_path / "collector.sqlite3"
    assert accept_batch(path, _batch(0, 1)) == 1
    with sqlite3.connect(path) as db:
        receipt = db.execute("SELECT received_at,sequence FROM collector_receipts").fetchone()
        assert receipt[1] == 1
    assert accept_batch(path, _batch(0, 1)) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT received_at,sequence FROM collector_receipts").fetchone() == receipt
    view = snapshot(path)
    assert view["sources"][0]["last_replication_received_at"] == receipt[0]
    assert view["sources"][0]["last_ingested_at"] is None
    assert view["connectors"][0]["status_observed_at"] == "2026-10-29T21:35:00Z"
    assert view["connectors"][0]["observed_at"] == "2026-10-30T06:00:00Z"


def test_legacy_collector_has_unknown_receipt_and_status(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE collector_cursors(source_id TEXT PRIMARY KEY,sequence INTEGER)")
        db.execute("INSERT INTO collector_cursors VALUES('node-a',1)")
        db.execute("CREATE TABLE collector_latest(source_id TEXT,kind TEXT,record_key TEXT,sequence INTEGER,payload TEXT)")
        db.execute("INSERT INTO collector_latest VALUES(?,?,?,?,?)",
                   ("node-a", "connector", "charger:1", 1,
                    json.dumps({"charger_id": "charger", "connector_id": 1,
                                "status": "Faulted", "observed_at": "2026-10-30T06:00:00Z"})))
    view = snapshot(path)
    assert view["sources"][0]["last_replication_received_at"] is None
    assert view["connectors"][0]["status_observed_at"] is None
