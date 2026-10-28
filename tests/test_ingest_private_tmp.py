"""Regression for SQLite temp writes in the sandboxed local ingestion unit."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ingest_unit_provides_private_tmp_without_relaxing_sandbox():
    unit = (ROOT / "ansible/templates/ocpp-reports-ingest.service.j2").read_text()
    directives = dict(line.split("=", 1) for line in unit.splitlines()
                      if "=" in line and not line.lstrip().startswith("#"))
    assert directives["PrivateTmp"].lower() == "true"
    assert directives["ProtectSystem"] == "strict"
    assert directives["ReadWritePaths"] == "/var/lib/ocpp-reports"
    assert "ReadOnlyPaths" in directives
    assert "ocpp-reports" in directives["User"]


def test_ingest_sqlite_backfill_can_use_temp_storage(tmp_path):
    import sqlite3
    from ocpp_reports.warehouse.schema import _schema
    warehouse = tmp_path / "warehouse.sqlite3"
    temporary = tmp_path / "private-tmp"
    temporary.mkdir()
    with sqlite3.connect(warehouse) as db:
        db.execute("PRAGMA temp_store=FILE")
        _schema(db)
        db.execute("CREATE TEMP TABLE temporary_probe(value INTEGER)")
        db.execute("INSERT INTO temporary_probe VALUES (42)")
        assert db.execute("SELECT value FROM temporary_probe").fetchone() == (42,)
    # This is a process-level SQLite smoke test; only the deployed systemd
    # sandbox can prove actual PrivateTmp mount behavior on a target host.
