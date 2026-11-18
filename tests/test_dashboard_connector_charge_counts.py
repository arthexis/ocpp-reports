"""Historical connector charge counts are source-scoped and desktop-only."""
from copy import deepcopy
import json
import re
import sqlite3

from fastapi.testclient import TestClient

from ocpp_reports import web
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.transaction_pagination import connector_charge_counts


def _tx(tx_id, charger="shared", connector=1, *, active=False):
    return {
        "id": tx_id,
        "charger_id": charger,
        "connector_id": connector,
        "started_at": "2026-11-01T10:00:00Z",
        "stopped_at": None if active else "2026-11-01T11:00:00Z",
        "state": "open" if active else "closed",
        "active": active,
        "energy_wh": None if active else 0,
    }


def _seed(path):
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)

        def local(source, tx_id, *, charger="shared", connector=1):
            record = _tx(tx_id, charger, connector)
            db.execute(
                "INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                (source, charger, tx_id, 0, 1, "hash", json.dumps(record),
                 "2026-11-01T11:00:00Z"),
            )

        def remote(source, key, tx_id, *, charger="shared", connector=1, active=False):
            record = _tx(tx_id, charger, connector, active=active)
            db.execute(
                "INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                (source, "transaction", key, tx_id, json.dumps(record)),
            )

        local("site-a", 1)
        local("site-a", 2)
        # The replicated copy must never override local identity/connector.
        remote("site-a", "shared:1", 1, connector=2)
        remote("site-a", "shared:3", 3)
        remote("site-a", "shared:4", 4, connector=2)
        remote("site-a", "shared:4-duplicate", 4, connector=2)
        remote("site-a", "shared:5", 5, connector=2, active=True)
        remote("site-a", "shared:8", 8, connector=0)
        remote("site-a", "shared:9", 9, connector="unknown")
        # Reused numeric IDs and charger IDs are independent by source.
        local("site-b", 1, connector=2)
        local("site-a", 1, charger="other", connector=1)


def _visible():
    return [
        {"source_id": "site-a", "charger_id": "shared", "connector_id": number,
         "location": "Garage A", "status": "Available", "connected": True,
         "observed_at": "2026-11-01T11:00:00Z", "energy_wh": None,
         "power_w": None, "rated_power_w": None}
        for number in (0, 1, 2, 3)
    ]


def test_distinct_history_local_wins_and_station_is_rollup(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _seed(path)
    visible = _visible()
    visible += [
        {"source_id": "site-b", "charger_id": "shared", "connector_id": number}
        for number in (0, 1, 2)
    ]
    visible.append({"source_id": "site-a", "charger_id": "other", "connector_id": 1})
    counts = connector_charge_counts(path, visible)
    assert counts[("site-a", "shared", 1)] == 3
    assert counts[("site-a", "shared", 2)] == 2
    assert counts[("site-a", "shared", 3)] == 0
    assert counts[("site-a", "shared", 0)] == 5
    assert counts[("site-b", "shared", 1)] == 0
    assert counts[("site-b", "shared", 2)] == 1
    assert counts[("site-b", "shared", 0)] == 1
    assert counts[("site-a", "other", 1)] == 1
    restricted = connector_charge_counts(
        path, visible, allowed_source_ids=("site-a",))
    assert all(source == "site-a" for source, _, _ in restricted)
    assert restricted[("site-a", "shared", 0)] == 5
    # Even the station-only view retains the historical physical count.
    assert connector_charge_counts(path, [visible[0]]) == {
        ("site-a", "shared", 0): 5
    }
    assert connector_charge_counts(path, []) == {}


def test_html_desktop_column_and_isolated_count_not_paged(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.sqlite"
    _seed(path)

    def data(*args, **kwargs):
        return {"sources": [], "connectors": deepcopy(_visible()),
                "transactions": [], "events": []}

    monkeypatch.setattr(web, "snapshot", data)
    client = TestClient(web.create_app(
        path, allowed_source_ids=("site-a",), timezones=("UTC",)))
    html = client.get("/?connector_view=individual").text
    assert re.search(
        r'<th class="numeric">Energía</th>'
        r'<th class="numeric connector-charge-count">Cargas</th>'
        r'<th class="connector-last-charge">Última carga</th>', html)
    assert 'class="numeric connector-charge-count" data-label="Cargas">3</td>' in html
    assert 'class="numeric connector-charge-count" data-label="Cargas">2</td>' in html
    assert 'class="numeric connector-charge-count" data-label="Cargas">0</td>' in html
    assert ".connectors-panel .connector-last-charge,.connectors-panel .connector-charge-count{display:none}" in html
    assert "@media(min-width:1101px)" in html
    assert "th.connector-charge-count,.connectors-panel td.connector-charge-count{display:table-cell}" in html
    assert ".connectors-panel th:nth-child(10),.connectors-panel td:nth-child(10){display:none}" in html
    combined = client.get("/?connector_view=combined").text
    assert 'class="numeric connector-charge-count" data-label="Cargas">5</td>' in combined
    station = client.get("/?connector_view=station").text
    assert 'class="numeric connector-charge-count" data-label="Cargas">5</td>' in station
    assert "colspan=\"10\"" in client.get("/?filter=none").text
    # This view-only enrichment does not alter the existing /api/snapshot contract.
    api = client.get("/api/snapshot").json()
    assert all("charge_count" not in row for row in api["connectors"])
    assert client.get("/?source_id=site-b").status_code == 403
