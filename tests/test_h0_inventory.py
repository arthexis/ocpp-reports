"""Safety and metadata tests for the offline H0 inventory utility."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "h0_inventory.py"
spec = importlib.util.spec_from_file_location("h0_inventory", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_metadata_only_and_no_source_changes(tmp_path):
    source = tmp_path / "history.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE events (id INTEGER, received_at TEXT, secret TEXT)")
        db.execute("INSERT INTO events VALUES (1, '2026-10-04T00:00:00Z', 'PRIVATE_RFID')")
        db.execute("PRAGMA user_version=2")
    before = source.read_bytes()
    result = module.inspect(source)
    assert source.read_bytes() == before
    assert result["user_version"] == 2
    assert result["tables"]["events"]["row_count"] == 1
    assert result["tables"]["events"]["time_bounds"]["received_at"]["min"] == "2026-10-04T00:00:00Z"
    assert "PRIVATE_RFID" not in str(result)
    assert result["sha256"]
    assert not (tmp_path / "history.sqlite3-journal").exists()


def test_missing_file_never_created(tmp_path):
    source = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        module.inspect(source)
    assert not source.exists()


def test_invalid_sqlite_rejected(tmp_path):
    source = tmp_path / "invalid.sqlite3"
    source.write_text("not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        module.inspect(source)
