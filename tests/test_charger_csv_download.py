"""End-to-end, source-isolated, unpaginated CSV download for charger detail."""
import csv
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ocpp_reports.web import create_app
from ocpp_reports.dashboard_login import route_permission
from ocpp_reports.dashboard_auth import Permission
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.reports.vendor_common import HEADERS


def rows(response):
    return list(csv.reader(io.StringIO(response.text)))


def seed(path):
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)
        for source in ("a", "b"):
            db.execute("INSERT INTO sources(source_id,cursor) VALUES (?,?)", (source, 1))
            for txid in range(1, 46):
                tx = {
                    "id": txid, "charger_id": "same", "connector_id": 1,
                    "started_at": "2026-11-01T10:00:00Z",
                    "stopped_at": "2026-11-01T11:00:00Z",
                    "energy_wh": 0 if txid == 1 else txid,
                    "meter_start_wh": 100, "meter_stop_wh": 100 + txid,
                    "state": "completed", "active": False,
                    "stop_reason": "=SUM(1,1)" if txid == 1 else "Local",
                }
                db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                           (source, "same", txid, 0, 1, "digest",
                            json.dumps(tx), "2026-11-01T12:00:00Z"))
            db.execute(
                "INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                (source, "transaction", "same:1", 1, json.dumps({
                    "id": 1, "charger_id": "same", "connector_id": 2,
                    "energy_wh": 99999,
                    "started_at": "2026-11-01T10:00:00Z"})))
            db.execute(
                "INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                (source, "transaction", "same:46", 2, json.dumps({
                    "id": 46, "charger_id": "same", "connector_id": 2,
                    "energy_wh": None, "state": "open", "active": True,
                    "started_at": "2026-11-01T12:00:00Z"})))
        db.execute("INSERT INTO collector_charger_locations VALUES (?,?,?)",
                   ("a", "same", "=BAD()"))
        db.execute("INSERT INTO collector_charger_locations VALUES (?,?,?)",
                   ("b", "same", "Otro cliente"))
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "connector", "empty:1", 3,
                    json.dumps({"charger_id": "empty", "connector_id": 1})))


def test_csv_download_all_rows_exact_scope_and_safe_cells(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    seed(db)
    client = TestClient(create_app(db, allowed_source_ids=("a",), timezones=("America/Monterrey",)))
    html = client.get("/chargers/a/same?tz=America/Monterrey")
    assert html.status_code == 200
    assert 'Descargar CSV' in html.text
    assert '/chargers/a/same/report.csv?tz=America/Monterrey' in html.text
    response = client.get("/chargers/a/same/report.csv?tz=America/Monterrey")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "attachment;" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("text/csv")
    data = rows(response)
    assert tuple(data[0]) == HEADERS
    assert len(data) == 47  # 46 sessions, including beyond the HTML's page 1
    assert data[1] == ["'=BAD()", "1", "2026-11-01 04:00:00", "2026-11-01 05:00:00",
                       "100", "101", "0", "", "", "'=SUM(1,1)"]
    assert data[-1][1] == "2" and data[-1][6] == ""  # provisional, never fabricated
    assert "99999" not in response.text
    assert "Otro cliente" not in response.text
    assert client.get("/chargers/b/same/report.csv").status_code == 404
    assert client.get("/chargers/a/unknown/report.csv").status_code == 404
    assert client.get("/chargers/a/same/report.csv?tz=Europe/Paris").status_code == 422


def test_empty_known_charger_header_only_and_legacy_layout(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    seed(db)
    app = create_app(db, allowed_source_ids=("a",), csv_profile="vendor-csv")
    client = TestClient(app)
    empty = client.get("/chargers/a/empty/report.csv")
    assert empty.status_code == 200 and rows(empty) == [list(HEADERS)]
    other = rows(client.get("/chargers/a/same/report.csv"))
    assert other[1][2] == "2026-11-01T10:00:00+00:00"
    assert len(other) == 47
    assert client.get("/chargers/a/empty").status_code == 200


def test_invalid_profile_and_authenticated_permission_policy(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    seed(db)
    with pytest.raises(ValueError, match="CSV profile"):
        create_app(db, csv_profile="unknown")
    assert (route_permission("/chargers/a/same/report.csv", "GET")
            == Permission.CONNECTORS_VIEW)
    assert route_permission("/chargers/a/same/report.csv", "POST") is None
    assert route_permission("/chargers/a/same/anything", "GET") is None


def test_source_and_charger_required_even_if_other_source_has_same_id(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    seed(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM current_transactions WHERE source_id='a'")
        conn.execute("DELETE FROM collector_latest WHERE source_id='a' AND kind='transaction'")
        conn.execute("DELETE FROM collector_latest WHERE source_id='a' AND kind='connector'")
    client = TestClient(create_app(db, allowed_source_ids=("a",)))
    assert client.get("/chargers/a/same/report.csv").status_code == 404
    assert client.get("/chargers/b/same/report.csv").status_code == 404
