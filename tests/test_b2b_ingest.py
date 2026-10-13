"""B2B1/B2B2: deterministic, offline ingestion and persistence."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from ocpp_reports.ingest import ingest_page, load_page

FIXTURE = Path(__file__).parent / "fixtures" / "csms_export_v1_page.json"


@pytest.fixture
def page():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _count(db, table):
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_load_does_not_write_source(tmp_path):
    original = FIXTURE.read_bytes()
    assert load_page(FIXTURE)["schema"] == "ocpp-csms/export/v1"
    assert FIXTURE.read_bytes() == original


def test_ingest_and_restart_replay(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    assert ingest_page(db, page, imported_at="2026-10-13T00:00:00Z") == 2
    assert ingest_page(db, page, imported_at="2026-10-14T00:00:00Z") == 2
    assert _count(db, "events") == 2
    assert _count(db, "energy_samples") == 1
    assert _count(db, "transaction_observations") == 1


def test_same_cursor_changed_transaction_observation_is_additive(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, page)
    updated = copy.deepcopy(page)
    updated["data"]["transactions"][0].update(state="ended", active=False,
        stopped_at="2026-10-07T12:05:00Z", meter_stop_wh=2000, energy_wh=1000)
    assert ingest_page(db, updated) == 2
    assert _count(db, "transaction_observations") == 2


def test_event_conflict_rolls_back_all_changes(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, page)
    bad = copy.deepcopy(page)
    bad["data"]["events"][1]["kind"] = "different"
    bad["data"]["transactions"][0]["state"] = "ended"
    with pytest.raises(ValueError, match="Conflicting immutable event"):
        ingest_page(db, bad)
    assert _count(db, "transaction_observations") == 1


def test_gap_rejected_without_cursor_advance(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, page)
    later = copy.deepcopy(page)
    later["data"].update(cursor={"after": 3, "next": 3, "more": False},
                         events=[], energy=[], transactions=[])
    with pytest.raises(ValueError, match="gap"):
        ingest_page(db, later)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT cursor FROM sources").fetchone()[0] == 2


def test_two_sources_isolated(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, page)
    other = copy.deepcopy(page)
    other["data"]["source_id"] = "sanitized-source-b"
    ingest_page(db, other)
    assert _count(db, "sources") == 2
    assert _count(db, "events") == 4


def test_bad_json_rejected(tmp_path):
    source = tmp_path / "bad.json"
    source.write_text("{invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_page(source)


def test_new_page_failure_does_not_advance_cursor_or_store_partial_evidence(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, page)
    later = copy.deepcopy(page)
    later["data"]["cursor"] = {"after": 2, "next": 3, "more": False}
    later["data"]["events"] = [
        {"id": 3, "at": "2026-10-07T12:02:00Z", "charger_id": "cp-a",
         "kind": "meter_values", "action": "MeterValues"}
    ]
    later["data"]["energy"] = [
        {"source_event_id": 3, "sample_index": 0,
         "at": "2026-10-07T12:02:00Z", "power_w": 3000, "energy_wh": 2000},
        {"source_event_id": 3, "sample_index": 0,
         "at": "2026-10-07T12:02:00Z", "power_w": 4000, "energy_wh": 2000},
    ]
    with pytest.raises(ValueError, match="Conflicting energy sample"):
        ingest_page(db, later)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT cursor FROM sources").fetchone()[0] == 2
    assert _count(db, "events") == 2
    assert _count(db, "energy_samples") == 1


def test_rejects_cursor_boundary_mismatch(tmp_path, page):
    broken = copy.deepcopy(page)
    broken["data"]["cursor"]["next"] = 10
    with pytest.raises(ValueError, match="cursor.next must match last event"):
        ingest_page(tmp_path / "reports.sqlite3", broken)


def test_rejects_energy_from_other_page(tmp_path, page):
    broken = copy.deepcopy(page)
    broken["data"]["energy"][0]["source_event_id"] = 100
    with pytest.raises(ValueError, match="outside (this )?page"):
        ingest_page(tmp_path / "reports.sqlite3", broken)


def test_replay_old_page_after_later_page_does_not_rewind(tmp_path, page):
    db = tmp_path / "reports.sqlite3"
    ingest_page(db, page)
    later = copy.deepcopy(page)
    later["data"]["cursor"] = {"after": 2, "next": 3, "more": False}
    later["data"]["events"] = [
        {"id": 3, "at": "2026-10-07T12:02:00Z", "charger_id": "cp-a",
         "kind": "meter_values", "action": "MeterValues"}
    ]
    later["data"]["energy"] = []
    later["data"]["transactions"] = []
    assert ingest_page(db, later) == 3
    assert ingest_page(db, page) == 3
    assert _count(db, "events") == 3


def test_empty_page_cannot_advance_cursor(tmp_path, page):
    broken = copy.deepcopy(page)
    broken["data"]["events"] = []
    broken["data"]["energy"] = []
    broken["data"]["transactions"] = []
    with pytest.raises(ValueError, match="Empty page must not advance cursor"):
        ingest_page(tmp_path / "reports.sqlite3", broken)
