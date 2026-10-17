"""B2B3: current projection retains all imported observations."""
import copy
import json
import sqlite3
from pathlib import Path

from ocpp_reports.ingest import ingest_page
from ocpp_reports.transaction_projection import current_transactions

FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_late_stop_replaces_active_without_summing_energy(tmp_path):
    database = tmp_path / "reports.sqlite3"
    active, stopped = fixture("active.json"), fixture("late_stop.json")
    ingest_page(database, active, imported_at="2026-01-01T00:01:00Z")
    first = current_transactions(database)
    assert len(first) == 1
    assert first[0]["transaction"]["active"] is True
    assert first[0]["transaction"]["energy_wh"] is None

    ingest_page(database, stopped, imported_at="2026-01-01T00:12:00Z")
    result = current_transactions(database)
    assert len(result) == 1
    assert result[0]["transaction"]["active"] is False
    assert result[0]["transaction"]["energy_wh"] == 500
    assert result[0]["evidence"]["page_next"] == 2
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM transaction_observations").fetchone()[0] == 2

    ingest_page(database, active)
    assert current_transactions(database) == result


def test_same_cursor_revised_snapshot_and_idempotency(tmp_path):
    database = tmp_path / "reports.sqlite3"
    page = fixture("active.json")
    ingest_page(database, page, imported_at="2026-01-01T00:01:00Z")
    revised = copy.deepcopy(page)
    revised["data"]["transactions"][0]["energy_wh"] = 0
    ingest_page(database, revised, imported_at="2026-01-01T00:02:00Z")
    assert current_transactions(database)[0]["transaction"]["energy_wh"] == 0
    ingest_page(database, revised, imported_at="2026-01-01T00:03:00Z")
    assert len(current_transactions(database)) == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM transaction_observations").fetchone()[0] == 2


def test_sources_with_same_transaction_id_stay_separate(tmp_path):
    database = tmp_path / "reports.sqlite3"
    page = fixture("active.json")
    ingest_page(database, page)
    second = copy.deepcopy(page)
    second["data"]["source_id"] = "fixture-source-b"
    ingest_page(database, second)
    assert len(current_transactions(database)) == 2
    assert len(current_transactions(database, source_id="fixture-source-b")) == 1


def test_empty_source_projection(tmp_path):
    database = tmp_path / "reports.sqlite3"
    ingest_page(database, fixture("active.json"))
    assert current_transactions(database, source_id="unknown") == []
