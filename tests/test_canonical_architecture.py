"""Enforce the canonical internal boundaries without removing public compatibility paths."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src" / "ocpp_reports"

@pytest.mark.parametrize("relative", [
    "warehouse/ingestion.py",
    "reports/builder.py",
    "reports/rendering.py",
    "reports/csv_profiles.py",
])
def test_implementation_imports_do_not_use_legacy_facades(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    forbidden = {
        "csms_export", "export", "transaction_projection", "warehouse_queries",
        "ingest", "rendering", "csv_profiles",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 2:
            continue
        assert node.module not in forbidden, (relative, node.module)


def test_collector_schema_creation_is_idempotent(tmp_path):
    import sqlite3
    from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
    with sqlite3.connect(tmp_path / "warehouse.sqlite3") as db:
        ensure_collector_schema(db)
        ensure_collector_schema(db)
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"collector_changes", "collector_latest", "collector_cursors"} <= tables

def test_historical_file_compatibility_does_not_weaken_ingestion(tmp_path):
    import json
    import pytest
    from ocpp_reports.contracts.export import read_page
    from ocpp_reports.contracts.csms_export import validate_export
    page = {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "legacy", "cursor": {"after": 0, "next": 2, "more": False},
        "events": [], "energy": [], "transactions": [], "status": {},
    }}
    path = tmp_path / "export.json"
    path.write_text(json.dumps(page), encoding="utf-8")
    assert read_page(path).data.source_id == "legacy"
    with pytest.raises(ValueError, match="Empty page"):
        validate_export(page)
