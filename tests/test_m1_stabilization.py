"""M1: transactional ingestion and projection regression coverage."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from ocpp_reports.ingest import ingest_page
from ocpp_reports.transaction_projection import current_transactions

FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def snapshot(database):
    with sqlite3.connect(database) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "sources", "events", "energy_samples",
                "transaction_observations", "current_transactions",
            )
        }


def test_conflicting_second_event_rolls_back_first_insert_and_cursor(tmp_path):
    database = tmp_path / "reports.sqlite"
    original = fixture("active.json")
    ingest_page(database, original)
    before = snapshot(database)
    bad = copy.deepcopy(original)
    bad["data"]["events"][0]["action"] = "ChangedAction"
    with pytest.raises(ValueError, match="Conflicting immutable event"):
        ingest_page(database, bad)
    assert snapshot(database) == before


def test_gap_does_not_advance_cursor_or_create_evidence(tmp_path):
    database = tmp_path / "reports.sqlite"
    first = fixture("active.json")
    ingest_page(database, first)
    before = snapshot(database)
    gap = fixture("late_stop.json")
    gap["data"]["cursor"]["after"] = 99
    gap["data"]["cursor"]["next"] = 100
    gap["data"]["events"][0]["id"] = 100
    with pytest.raises(ValueError, match="Export cursor gap"):
        ingest_page(database, gap)
    assert snapshot(database) == before


def test_late_stop_replay_preserves_projection_and_observation_count(tmp_path):
    database = tmp_path / "reports.sqlite"
    active = fixture("active.json")
    late = fixture("late_stop.json")
    ingest_page(database, active)
    ingest_page(database, late)
    expected = current_transactions(database)
    observations = snapshot(database)["transaction_observations"]
    ingest_page(database, active)
    ingest_page(database, late)
    assert current_transactions(database)[0]["transaction"] == expected[0]["transaction"]
    assert snapshot(database)["transaction_observations"] == observations
    assert current_transactions(database)[0]["transaction"]["energy_wh"] == 500


def test_source_isolation_for_same_transaction_identity(tmp_path):
    database = tmp_path / "reports.sqlite"
    first = fixture("active.json")
    other = copy.deepcopy(first)
    other["data"]["source_id"] = "another-source"
    ingest_page(database, first)
    ingest_page(database, other)
    assert len(current_transactions(database)) == 2
    assert len(current_transactions(database, source_id="another-source")) == 1
