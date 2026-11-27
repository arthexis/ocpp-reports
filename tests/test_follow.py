"""M1 continuous warehouse integration against export/v1 fixtures."""
import copy
import json
from pathlib import Path

import pytest

from ocpp_reports.follow import follow, current_connectors, source_cursor, sync_page


FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"


def page(name):
    return json.loads((FIXTURES / name).read_text())


def test_follow_catches_up_and_restarts_without_duplicates(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    active, late = page("active.json"), page("late_stop.json")
    source = active["data"]["source_id"]
    calls = []

    def producer(_directory, *, after, limit):
        calls.append(after)
        if after == 0:
            result = copy.deepcopy(active)
            result["data"]["cursor"]["more"] = True
            return result
        if after == active["data"]["cursor"]["next"]:
            return copy.deepcopy(late)
        empty = copy.deepcopy(late)
        empty["data"]["events"] = []
        empty["data"]["energy"] = []
        empty["data"]["transactions"] = []
        empty["data"]["cursor"] = {"after": after, "next": after, "more": False}
        return empty

    follow(database, tmp_path, once=True, producer=producer)
    cursor = source_cursor(database, source)
    assert cursor == late["data"]["cursor"]["next"]
    follow(database, tmp_path, once=True, producer=producer)
    assert source_cursor(database, source) == cursor
    assert calls.count(0) == 3  # first probe, first import, restart probe


def test_state_updates_even_without_cursor_progress(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    first = page("empty.json")
    first["data"]["cursor"] = {"after": 0, "next": 0, "more": False}
    first["data"]["status"] = {"chargers": [{
        "id": "cp", "connected": True,
        "connectors": [{"id": 1, "status": "Available", "transaction": None}]
    }]}
    first["data"]["exported_at"] = "2026-01-01T00:00:00Z"
    sync_page(database, first)
    second = copy.deepcopy(first)
    second["data"]["status"]["chargers"][0]["connectors"][0]["status"] = "Charging"
    second["data"]["exported_at"] = "2026-01-01T00:00:05Z"
    sync_page(database, second)
    assert current_connectors(database)[0]["status"] == "Charging"
    sync_page(database, first)
    assert current_connectors(database)[0]["status"] == "Charging"


def test_invalid_follow_configuration(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        follow(tmp_path / "db", tmp_path, interval=0, once=True, producer=lambda *_a, **_k: {})


def test_follow_recovers_transient_warehouse_lock_without_duplicate_evidence(tmp_path, monkeypatch):
    import importlib
    import sqlite3
    module = importlib.import_module("ocpp_reports.follow")
    database = tmp_path / "warehouse.sqlite"
    fixture = page("active.json")
    source = fixture["data"]["source_id"]
    # Generate an authentic SQLITE_BUSY using a separate file and connection.
    lockfile = tmp_path / "lock.sqlite"
    with sqlite3.connect(lockfile) as writer:
        writer.execute("CREATE TABLE fixture (id INTEGER)")
        writer.execute("BEGIN EXCLUSIVE")
        def busy_once(*args, **kwargs):
            with sqlite3.connect(lockfile, timeout=0) as contender:
                contender.execute("SELECT * FROM fixture").fetchall()
            return sync_page(*args, **kwargs)
        monkeypatch.setattr(module, "sync_page", busy_once)
        delays = []
        def release(delay):
            delays.append(delay)
            writer.rollback()
        follow(database, tmp_path, once=True, producer=lambda *_a, **_k: fixture, sleep=release)
    assert delays == [0.25]
    assert source_cursor(database, source) == fixture["data"]["cursor"]["next"]
    with sqlite3.connect(database) as db:
        first_count = db.execute("SELECT count(*) FROM events").fetchone()[0]
    follow(database, tmp_path, once=True, producer=lambda *_a, **_k: fixture, sleep=lambda _: None)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == first_count


def test_contention_retry_is_bounded_and_preserves_other_errors(tmp_path):
    import importlib
    import sqlite3
    module = importlib.import_module("ocpp_reports.follow")
    dbpath = tmp_path / "lock.sqlite"
    with sqlite3.connect(dbpath) as writer:
        writer.execute("CREATE TABLE fixture (id INTEGER)")
        writer.execute("BEGIN EXCLUSIVE")
        delays = []
        def blocked():
            with sqlite3.connect(dbpath, timeout=0) as reader:
                reader.execute("SELECT * FROM fixture").fetchall()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            module._retry_sqlite_contention(blocked, delays.append)
        assert delays == [0.25, 0.5, 1.0]
        writer.rollback()
    delays = []
    def invalid():
        with sqlite3.connect(dbpath) as db:
            db.execute("SELECT * FROM nonexistent_table")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        module._retry_sqlite_contention(invalid, delays.append)
    assert not delays
