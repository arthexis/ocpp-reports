"""Configured CSMS ratings propagate independently of measured OCPP power."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from ocpp_reports.changefeed import changes_page
from ocpp_reports.follow import sync_page
from ocpp_reports.replication.collector import Batch, accept_batch
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.web import create_app


def page(rating=22000, scope="connector", *, source="source-a"):
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": source, "exported_at": datetime.now(timezone.utc).isoformat(),
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{
            "id": "cp-1", "connected": True,
            "rated_power_w": 50000,
            "connectors": [
                {"id": 0, "status": "Available", "rated_power_w": 50000,
                 "rating_scope": "charger", "transaction": None},
                {"id": 1, "status": "Available", "rated_power_w": rating,
                 "rating_scope": scope, "transaction": None},
                {"id": 2, "status": "Available", "rated_power_w": 50000,
                 "rating_scope": "charger", "transaction": None},
            ],
        }]},
    }}


def test_rating_refresh_at_unchanged_csms_event_cursor_and_shared_c0(tmp_path):
    db = tmp_path / "local.sqlite"
    assert sync_page(db, page()) == 0
    rows = {c["connector_id"]: c for c in snapshot(db)["connectors"]}
    assert rows[0]["rated_power_w"] == 50000
    assert rows[1]["rated_power_w"] == 22000
    assert rows[1]["rating_scope"] == "connector"
    assert rows[2]["rated_power_w"] == 50000
    assert all(not c["power_fresh"] for c in rows.values())
    first = changes_page(db, "source-a")
    assert {c["kind"] for c in first["changes"]} >= {"rating", "connector"}
    assert sync_page(db, page(rating=11000)) == 0
    updated = changes_page(db, "source-a", after=first["next"])
    assert any(c["kind"] == "rating" and c["payload"]["rated_power_w"] == 11000
               for c in updated["changes"])
    assert sync_page(db, page(rating=11000)) == 0
    assert changes_page(db, "source-a", after=updated["next"])["changes"] == []


def test_rating_reaches_collector_and_respects_source_scope(tmp_path):
    db = tmp_path / "local.sqlite"
    collector = tmp_path / "remote.sqlite"
    sync_page(db, page())
    batch = changes_page(db, "source-a")
    assert accept_batch(collector, Batch.model_validate(batch)) == batch["next"]
    result = snapshot(collector, allowed_source_ids=("source-a",))
    by_id = {c["connector_id"]: c for c in result["connectors"]}
    assert by_id[1]["rated_power_w"] == 22000
    assert by_id[0]["rating_scope"] == "charger"
    assert snapshot(collector, allowed_source_ids=("another-source",))["connectors"] == []


def test_dashboard_distinguishes_nominal_and_fresh_measured_power(monkeypatch, tmp_path):
    from ocpp_reports import web
    at = datetime.now(timezone.utc).isoformat()
    rows = [
        dict(source_id="s", charger_id="cp", connector_id=0, location="S", status="Available",
             connected=True, power_w=None, power_fresh=False, rated_power_w=50000,
             rating_scope="charger", energy_wh=None, observed_at=at),
        dict(source_id="s", charger_id="cp", connector_id=1, location="S", status="Available",
             connected=True, power_w=7200, power_fresh=True, rated_power_w=22000,
             rating_scope="connector", energy_wh=None, observed_at=at),
        dict(source_id="s", charger_id="cp", connector_id=2, location="S", status="Available",
             connected=True, power_w=3400, power_fresh=False, rated_power_w=11000,
             rating_scope="connector", energy_wh=None, observed_at=at),
    ]
    monkeypatch.setattr(web, "snapshot", lambda *a, **k: {
        "sources": [], "connectors": rows, "events": [], "transactions": []})
    response = TestClient(create_app(tmp_path / "unused")).get("/")
    assert response.status_code == 200
    assert "50.00 kW" in response.text and "nominal" in response.text
    assert "7.20 kW" in response.text and "medida" in response.text
    assert "11.00 kW" in response.text
    assert "3.40 kW" not in response.text
