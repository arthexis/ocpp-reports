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
