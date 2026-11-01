"""SQL-backed dashboard pagination preserves source scope, filters and full history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from ocpp_reports.web import create_app
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def _record(n, *, cp="cp-local", energy=1000, state="closed", start="2026-10-30T12:00:00Z",
            stop="2026-10-30T13:00:00Z"):
    return {"id": n, "charger_id": cp, "state": state, "active": False,
            "started_at": start, "stopped_at": stop, "energy_wh": energy}


def _seed(dbpath, *, local=61, remote=42):
    with sqlite3.connect(dbpath) as db:
        _schema(db)
        ensure_collector_schema(db)
        db.execute("INSERT INTO sources(source_id,cursor) VALUES (?,?)", ("local", 0))
        db.execute("INSERT INTO collector_cursors(source_id,sequence) VALUES (?,?)", ("remote", 1))
        db.execute("INSERT INTO collector_charger_locations VALUES (?,?,?)",
                   ("remote", "cp-remote", "Audi Calzada"))
        db.execute("INSERT INTO collector_charger_locations VALUES (?,?,?)",
                   ("local", "cp-local", "Porsche"))
        for n in range(1, local + 1):
            payload = json.dumps(_record(n, energy=0 if n % 10 == 0 else 1000),
                                 sort_keys=True)
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ("local", "cp-local", n, 0, 0, "hash", payload, "2026-10-30T13:00:00Z"))
        for n in range(1, remote + 1):
            payload = json.dumps(_record(n, cp="cp-remote", energy=0 if n % 10 == 0 else 1000))
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("remote", "transaction", f"cp-remote:{n}", n, payload))


def _page(dbpath, **kw):
    return snapshot(dbpath, allowed_source_ids=("local", "remote"),
                    transaction_pagination={
                        "term": "", "status": "", "location": "", "date_from": "",
                        "date_to": "", "zone": "UTC", "view": "all", "page": 1,
                        **kw,
                    })


def test_all_history_is_counted_and_only_page_records_loaded(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=521, remote=42)
    first = _page(dbpath)
    assert first["transaction_pagination"]["counts"] == {
        "all": 563, "success": 509, "failed": 54
    }
    assert len(first["transactions"]) == 20
    assert first["transaction_pagination"]["pages"] == 29
    last = _page(dbpath, page=29)
    assert len(last["transactions"]) == 3
    assert last["transaction_pagination"]["total"] == 563
    assert _page(dbpath, page=50000)["transaction_pagination"]["page"] == 29
    # Existing public API still returns the regular bounded snapshot.
    assert len(snapshot(dbpath, limit=10)["transactions"]) == 10


def test_filter_and_count_before_offset_and_source_isolation(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=25, remote=42)
    remote = _page(dbpath, location="audi", view="failed")
    assert remote["transaction_pagination"]["counts"] == {
        "all": 42, "success": 38, "failed": 4
    }
    assert [r["id"] for r in remote["transactions"]] == [40, 30, 20, 10]
    matching = _page(dbpath, term="PORSCHE", view="success", page=2)
    assert matching["transaction_pagination"]["counts"]["all"] == 25
    assert matching["transaction_pagination"]["counts"]["success"] == 23
    assert len(matching["transactions"]) == 3
    porsche = snapshot(dbpath, allowed_source_ids=("local",),
                       transaction_pagination={
                           "term": "", "status": "", "location": "",
                           "date_from": "", "date_to": "", "zone": "UTC",
                           "view": "all", "page": 1,
                       })
    assert porsche["transaction_pagination"]["total"] == 25
    assert all(r["source_id"] == "local" for r in porsche["transactions"])
    client = TestClient(create_app(dbpath, allowed_source_ids=("remote",), timezones=("UTC",)))
    assert client.get("/?source_id=local").status_code == 403
    response = client.get("/?tx_view=all&tx_page=2")
    assert response.status_code == 200
    assert "Mostrando 21–40 de 42" in response.text
    assert "Porsche" not in response.text


def test_date_intersection_and_unknown_energy_semantics(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=2, remote=0)
    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE current_transactions SET payload=? WHERE transaction_id=1",
                   (json.dumps(_record(1, start="2026-10-29T23:00:00-06:00",
                                       stop="2026-10-30T03:00:00-06:00", energy=None)),))
        db.execute("UPDATE current_transactions SET payload=? WHERE transaction_id=2",
                   (json.dumps(_record(2, start="2026-10-31T12:00:00Z",
                                       stop="2026-10-31T13:00:00Z", energy=0)),))
    result = _page(dbpath, zone="America/Monterrey",
                   date_from="2026-10-30", date_to="2026-10-30")
    assert result["transaction_pagination"]["counts"] == {
        "all": 1, "success": 0, "failed": 0
    }
    assert result["transactions"][0]["id"] == 1
    failed = _page(dbpath, zone="America/Monterrey",
                   date_from="2026-10-31", date_to="2026-10-31", view="failed")
    assert failed["transaction_pagination"]["total"] == 1
    assert failed["transactions"][0]["id"] == 2


def test_local_projection_wins_against_duplicate_collector_latest(tmp_path):
    dbpath = tmp_path / "warehouse.sqlite"
    _seed(dbpath, local=1, remote=0)
    with sqlite3.connect(dbpath) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("local", "transaction", "cp-local:1", 2,
                    json.dumps(_record(1, energy=0))))
    result = _page(dbpath)
    assert result["transaction_pagination"]["total"] == 1
    assert result["transactions"][0]["energy_wh"] == 1000
