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
    assert result["warehouse"]["sources"] == [{"source_id": "source-a", "cursor": 12, "last_ingested_at": None}]
    assert result["warehouse"]["last_ingested_at"] is None


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


def test_successful_ingest_updates_timestamp_even_on_replay(tmp_path):
    from ocpp_reports.ingest import ingest_page, load_page
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "csms_export_v1" / "active.json"
    page = load_page(fixture)
    database = tmp_path / "reports.sqlite3"
    config = tmp_path / "config.toml"
    config.write_text("[mail]\\n", encoding="utf-8")
    first = "2026-10-17T10:00:00+00:00"
    second = "2026-10-17T11:00:00+00:00"
    assert ingest_page(database, page, imported_at=first) == 1
    assert ingest_page(database, page, imported_at=second) == 1
    result = inspect_status(config=config, database=database)
    assert result["warehouse"]["sources"] == [
        {"source_id": "fixture-source-a", "cursor": 1, "last_ingested_at": second}
    ]
    assert result["warehouse"]["last_ingested_at"] == second


def test_legacy_sources_schema_is_upgraded_on_ingest_only(tmp_path):
    from ocpp_reports.ingest import ingest_page, load_page
    from pathlib import Path

    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE sources(source_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0)")
    config = tmp_path / "config.toml"
    config.write_text("[mail]\\n", encoding="utf-8")
    before = inspect_status(config=config, database=database)
    assert before["warehouse"]["sources"] == []
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
    assert "last_ingested_at" not in columns
    fixture = Path(__file__).parent / "fixtures" / "csms_export_v1" / "active.json"
    ingest_page(database, load_page(fixture), imported_at="2026-10-17T12:00:00+00:00")
    after = inspect_status(config=config, database=database)
    assert after["warehouse"]["sources"][0]["last_ingested_at"] == "2026-10-17T12:00:00+00:00"
