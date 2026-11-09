"""Real SQLite paging and source isolation for the public v1 API."""
import json
import sqlite3

from fastapi.testclient import TestClient
from ocpp_reports.web import create_app


def test_sqlite_pagination_and_source_scope(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE current_transactions (
            source_id TEXT, charger_id TEXT, transaction_id TEXT,
            payload TEXT, imported_at TEXT)""")
        for source in ("allowed", "hidden"):
            for index in range(25):
                payload = {"id": index, "charger_id": "CP1", "energy_wh": 0 if index == 0 else 100,
                           "started_at": f"2026-01-01T00:{index:02d}:00Z"}
                db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?)",
                           (source, "CP1", str(index), json.dumps(payload),
                            payload["started_at"]))
    client = TestClient(create_app(database, allowed_source_ids=("allowed",)))
    first = client.get("/api/v1/transactions").json()
    second = client.get("/api/v1/transactions", params={"page": 2}).json()
    assert first["total"] == 25
    assert first["pages"] == 2
    assert len(first["items"]) == 20
    assert len(second["items"]) == 5
    assert all(row["source_id"] == "allowed" for row in first["items"] + second["items"])
    assert client.get("/api/v1/transactions", params={"view": "failed"}).json()["total"] == 1
    assert client.get("/api/v1/transactions", params={"source_id": "hidden"}).status_code == 403
