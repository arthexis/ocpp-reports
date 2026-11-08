"""Collector-only source inventory must remain tenant-scoped."""
import sqlite3

from ocpp_reports.warehouse.queries import snapshot


def test_collector_only_sources_respect_allowlist(tmp_path):
    database = tmp_path / "collector.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE collector_latest (source_id TEXT, kind TEXT, "
            "record_key TEXT, payload TEXT)"
        )
        db.executemany(
            "INSERT INTO collector_latest VALUES (?, 'other', 'key', '{}')",
            [("tenant-a",), ("tenant-b",)]
        )
    result = snapshot(database, allowed_source_ids=("tenant-a",))
    assert [source["source_id"] for source in result["sources"]] == ["tenant-a"]
    assert result["sources"][0]["cursor"] is None
    assert result["sources"][0]["last_ingested_at"] is None
    assert result["connectors"] == []
    assert result["transactions"] == []
    assert result["events"] == []


def test_collector_only_sources_do_not_cross_tenants(tmp_path):
    database = tmp_path / "collector.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE collector_latest (source_id TEXT, kind TEXT, "
            "record_key TEXT, payload TEXT)"
        )
        db.executemany(
            "INSERT INTO collector_latest VALUES (?, 'other', 'key', '{}')",
            [("tenant-a",), ("tenant-b",)]
        )
    assert [s["source_id"] for s in snapshot(
        database, allowed_source_ids=("tenant-b",)
    )["sources"]] == ["tenant-b"]
    assert snapshot(database, allowed_source_ids=("absent",))["sources"] == []
