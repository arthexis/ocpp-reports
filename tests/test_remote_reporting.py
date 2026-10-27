"""End-to-end collector-to-report transaction coverage."""
from datetime import datetime, timezone
import sqlite3

import pytest

from ocpp_reports.collector import Batch, accept_batch
from ocpp_reports.reporting import build_report_from_database
from ocpp_reports.transaction_projection import current_transactions


def transaction(ident=7, energy=4200):
    return {
        "id": ident, "charger_id": "CP-1", "connector_id": 1,
        "rfid": "test", "state": "completed", "active": False,
        "started_at": "2026-10-01T10:00:00Z",
        "stopped_at": "2026-10-01T11:00:00Z", "energy_wh": energy,
        "meter_start_wh": 100, "meter_stop_wh": 100 + energy,
    }


def send(db, sequence, tx, after=0):
    batch = Batch.model_validate({
        "schema": "ocpp-reports/replication/v1", "source_id": "gway-005",
        "after": after, "next": sequence, "more": False,
        "changes": [{"sequence": sequence, "kind": "transaction",
                     "record_key": f"CP-1:{tx['id']}", "payload": tx}],
    })
    return accept_batch(db, batch)


def report(db, source="gway-005"):
    return build_report_from_database(
        db, datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 1, tzinfo=timezone.utc), "UTC", source_id=source,
    )


def test_collected_transaction_is_reportable_without_local_tables(tmp_path):
    db = tmp_path / "collector.sqlite3"
    assert send(db, 1, transaction()) == 1
    result = report(db)
    assert result["summary"]["transactions"] == 1
    assert result["summary"]["known_energy_wh"] == 4200
    assert result["transactions"][0]["id"] == 7
    assert current_transactions(db)[0]["evidence"]["replication_sequence"] == 1


def test_replication_revision_replaces_latest_in_reports(tmp_path):
    db = tmp_path / "collector.sqlite3"
    send(db, 1, transaction())
    send(db, 2, transaction(energy=4600), after=1)
    result = report(db)
    assert result["summary"]["transactions"] == 1
    assert result["summary"]["known_energy_wh"] == 4600
    assert len(current_transactions(db)) == 1


def test_replayed_batch_does_not_double_count(tmp_path):
    db = tmp_path / "collector.sqlite3"
    send(db, 1, transaction())
    assert send(db, 1, transaction()) == 1
    assert report(db)["summary"]["transactions"] == 1


def test_local_and_remote_sources_are_independent(tmp_path):
    db = tmp_path / "warehouse.sqlite3"
    send(db, 1, transaction())
    with sqlite3.connect(db) as connection:
        connection.execute("""CREATE TABLE current_transactions (
            source_id TEXT, charger_id TEXT, transaction_id INTEGER,
            page_after INTEGER, page_next INTEGER, fingerprint TEXT,
            payload TEXT, imported_at TEXT)""")
        import json
        connection.execute(
            "INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
            ("local", "CP-1", 7, 0, 1, "localhash",
             json.dumps(transaction(energy=1000)), "2026-10-01T11:00:00Z"),
        )
    assert report(db, "local")["summary"]["known_energy_wh"] == 1000
    assert report(db)["summary"]["known_energy_wh"] == 4200
    with pytest.raises(ValueError, match="Specify --source-id"):
        report(db, None)
