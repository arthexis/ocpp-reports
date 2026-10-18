"""B2C acceptance: six producer-cursor and reporting edge cases."""
import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ocpp_reports.ingest import ingest_page
from ocpp_reports.reporting import build_report_from_database
from ocpp_reports.transaction_projection import current_transactions

FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"


def page(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def next_page(previous, ids, *, more=False):
    result = copy.deepcopy(previous)
    after = previous["data"]["cursor"]["next"]
    result["data"]["cursor"] = {"after": after, "next": ids[-1] if ids else after, "more": more}
    result["data"]["events"] = [
        {"id": event_id, "at": "2026-01-01T00:10:00Z",
         "charger_id": "cp-a", "kind": "Heartbeat", "action": "Heartbeat"}
        for event_id in ids
    ]
    result["data"]["energy"] = []
    result["data"]["transactions"] = []
    return result


def counts(db):
    with sqlite3.connect(db) as connection:
        return {
            table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("events", "energy_samples", "transaction_observations", "current_transactions")
        }, connection.execute("SELECT source_id, cursor FROM sources ORDER BY source_id").fetchall()


def test_paginated_cursor_with_sparse_ids_and_empty_final_page(tmp_path):
    db = tmp_path / "reports.db"
    first = page("active.json")
    first["data"]["cursor"]["more"] = True
    assert ingest_page(db, first) == 1
    second = next_page(first, [4, 9], more=True)
    assert ingest_page(db, second) == 9
    empty = next_page(second, [])
    assert ingest_page(db, empty) == 9
    assert ingest_page(db, second) == 9
    assert counts(db)[0]["events"] == 3
    assert counts(db)[1] == [("fixture-source-a", 9)]


def test_late_stop_event_time_older_than_newer_event_cursor(tmp_path):
    db = tmp_path / "reports.db"
    active = page("active.json")
    ingest_page(db, active)
    middle = next_page(active, [4])
    ingest_page(db, middle)
    late = page("late_stop.json")
    late["data"]["cursor"] = {"after": 4, "next": 7, "more": False}
    late["data"]["events"][0]["id"] = 7
    # Arrival order (event cursor) is authoritative, not the original stop time.
    late["data"]["events"][0]["at"] = "2026-01-01T00:00:00Z"
    ingest_page(db, late)
    assert current_transactions(db)[0]["transaction"]["energy_wh"] == 500
    ingest_page(db, active)
    assert current_transactions(db)[0]["evidence"]["page_next"] == 7
    assert counts(db)[1] == [("fixture-source-a", 7)]


def test_same_source_reused_event_id_with_different_payload_is_rejected(tmp_path):
    db = tmp_path / "reports.db"
    original = page("active.json")
    ingest_page(db, original)
    restored = copy.deepcopy(original)
    restored["data"]["events"][0]["action"] = "DifferentAction"
    before = counts(db)
    with pytest.raises(ValueError, match="Conflicting immutable event"):
        ingest_page(db, restored)
    assert counts(db) == before


def test_run_report_does_not_mutate_warehouse(tmp_path):
    db = tmp_path / "reports.db"
    ingest_page(db, page("active.json"))
    before = counts(db)
    bytes_before = db.read_bytes()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    report = build_report_from_database(db, start, end, "UTC")
    assert report["summary"]["missing_energy_transactions"] == 1
    assert counts(db) == before
    assert db.read_bytes() == bytes_before


def test_restart_between_pages_and_replay(tmp_path):
    db = tmp_path / "reports.db"
    first = page("active.json")
    assert ingest_page(db, first) == 1
    # Each call opens a fresh SQLite connection, as after a process restart.
    second = page("late_stop.json")
    assert ingest_page(db, second) == 2
    assert ingest_page(db, first) == 2
    assert ingest_page(db, second) == 2
    assert counts(db)[0]["events"] == 2
    assert counts(db)[0]["transaction_observations"] == 2
    assert current_transactions(db)[0]["transaction"]["energy_wh"] == 500


def test_same_source_two_charge_points_same_transaction_id(tmp_path):
    db = tmp_path / "reports.db"
    first = page("active.json")
    second = copy.deepcopy(first)
    second["data"]["events"][0]["charger_id"] = "cp-b"
    second["data"]["transactions"][0]["charger_id"] = "cp-b"
    second["data"]["cursor"] = {"after": 1, "next": 3, "more": False}
    second["data"]["events"][0]["id"] = 3
    ingest_page(db, first)
    ingest_page(db, second)
    projections = current_transactions(db)
    assert len(projections) == 2
    assert {row["transaction"]["charger_id"] for row in projections} == {"cp-a", "cp-b"}
    assert {row["transaction"]["id"] for row in projections} == {first["data"]["transactions"][0]["id"]}
