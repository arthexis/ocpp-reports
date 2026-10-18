"""H0b fixture allowlist, deterministic pseudonyms and safety."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "h0_fixtures.py"
spec = importlib.util.spec_from_file_location("h0_fixtures", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_allowlist_redacts_sensitive_columns_and_identifiers(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events (id INTEGER, received_at TEXT, action TEXT, "
                   "rfid TEXT, payload TEXT)")
        db.execute("INSERT INTO events VALUES (123, '2026-10-04T12:00:00Z', "
                   "'Heartbeat', 'RFID_SECRET', 'PAYLOAD_SECRET')")
        db.execute("CREATE TABLE ocpp_transaction "
                   "(id INTEGER, start_time TEXT, energy REAL, customer_name TEXT)")
        db.execute("INSERT INTO ocpp_transaction VALUES "
                   "(9, '2025-09-26 10:00:00', 4.5, 'PRIVATE_PERSON')")
    before = path.read_bytes()
    result = module.extract(path, salt="test-private-salt", limit=2)
    assert path.read_bytes() == before
    assert result["rows"]["events"][0]["action"] == "Heartbeat"
    assert result["rows"]["events"][0]["id"] != 123
    assert result["rows"]["ocpp_transaction"][0]["energy"] == 4.5
    rendered = str(result)
    for forbidden in ("RFID_SECRET", "PAYLOAD_SECRET", "PRIVATE_PERSON", "customer_name"):
        assert forbidden not in rendered
    assert module.extract(path, salt="test-private-salt") == result
    assert module.extract(path, salt="different-private-salt")["rows"]["events"][0]["id"] != result["rows"]["events"][0]["id"]


def test_bounds_and_missing_input(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        module.extract(path, salt="private-salt")
    with pytest.raises(ValueError):
        module.extract(path, salt="", limit=5)
    with pytest.raises(ValueError):
        module.extract(path, salt="private-salt", limit=101)
    assert not path.exists()


def test_unknown_enum_not_exported(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events (id INTEGER, action TEXT)")
        db.execute("INSERT INTO events VALUES (1, 'RFID_SECRET')")
    result = module.extract(path, salt="private-salt")
    assert result["rows"]["events"][0]["action"] == "other"
    assert "RFID_SECRET" not in str(result)
