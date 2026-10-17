"""Read-only operational status contract."""
import sqlite3

from ocpp_reports.diagnostics import inspect_status


def test_missing_config_and_warehouse_are_not_created(tmp_path):
    config = tmp_path / "missing.toml"
    database = tmp_path / "missing.sqlite3"
    result = inspect_status(config=config, database=database)
    assert result["state"] == "unconfigured"
    assert result["config"]["state"] == "missing"
    assert result["warehouse"]["state"] == "missing"
    assert not config.exists() and not database.exists()


def test_valid_config_and_warehouse_sources(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[mail]\ntls = 'starttls'\n", encoding="utf-8")
    database = tmp_path / "reports.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE sources(source_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL)")
        db.execute("INSERT INTO sources VALUES ('source-a', 12)")
        db.execute("CREATE TABLE transaction_observations(imported_at TEXT)")
        db.execute("INSERT INTO transaction_observations VALUES ('2026-10-17T12:00:00+00:00')")
    result = inspect_status(config=config, database=database)
    assert result["state"] == "ready"
    assert result["warehouse"]["sources"] == [{"source_id": "source-a", "cursor": 12}]
    assert result["warehouse"]["last_transaction_import_at"] == "2026-10-17T12:00:00+00:00"


def test_corrupt_database_is_reported_without_modification(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[mail]\n", encoding="utf-8")
    database = tmp_path / "bad.sqlite3"
    database.write_bytes(b"not a sqlite database")
    before = database.read_bytes()
    result = inspect_status(config=config, database=database)
    assert result["state"] == "degraded"
    assert result["warehouse"]["state"] == "invalid"
    assert database.read_bytes() == before
