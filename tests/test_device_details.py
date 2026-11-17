"""Scoped charger/connector views and SQL pagination, no external services."""
import json
import sqlite3

from fastapi.testclient import TestClient

from ocpp_reports.web import create_app
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def seed(path):
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)
        for source in ("a", "b"):
            db.execute("INSERT INTO sources(source_id,cursor) VALUES (?,?)", (source, 14))
            for connector in (1, 2):
                for tx_id in (connector * 10 + 1, connector * 10 + 2):
                    payload = {"id": tx_id, "charger_id": "same", "connector_id": connector,
                               "started_at": "2026-11-10T01:00:00Z",
                               "stopped_at": "2026-11-10T01:30:00Z", "active": False,
                               "state": "closed", "energy_wh": 1500}
                    db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                               (source, "same", tx_id, 0, 0, "digest",
                                json.dumps(payload), "2026-11-10T02:00:00Z"))
            for event_id, cp in ((1, "same"), (2, "other")):
                db.execute("INSERT INTO events VALUES (?,?,?)",
                           (source, event_id, json.dumps({
                               "id": event_id, "charger_id": cp, "kind": "ocpp",
                               "action": "BootNotification", "at": "2026-11-10T02:00:00Z",
                               "private_token": "never-expose",
                           })))


def test_charger_and_connector_pages_are_distinct_and_tenant_safe(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    seed(path)
    client = TestClient(create_app(path, title="Reports", allowed_source_ids=("a",)))
    home = client.get("/")
    assert home.status_code == 200
    assert 'id="events"' not in home.text
    assert "Eventos recientes" not in home.text
    assert "/chargers/a/same" in home.text or "Sin conectores registrados" in home.text

    charger = client.get("/chargers/a/same?tz=UTC")
    assert charger.status_code == 200
    assert 'id="events"' in charger.text
    assert "Eventos recientes de same" in charger.text
    assert "Conectores del cargador" in charger.text
    assert "Sesiones de carga" in charger.text
    assert "Origen a" in charger.text
    assert "Sincronización" not in charger.text
    assert "4</strong>" in charger.text  # source a, two connectors x two sessions
    assert "never-expose" not in charger.text
    assert "data-first-id=" in charger.text

    connector = client.get("/connectors/a/same/1?tz=UTC")
    assert connector.status_code == 200
    assert 'id="events"' not in connector.text
    assert "Conector 1" in connector.text
    assert "Conector 2" not in connector.text
    assert ">11</a>" in connector.text and ">12</a>" in connector.text
    assert ">21</a>" not in connector.text and ">22</a>" not in connector.text
    assert "/chargers/a/same" in connector.text

    for url in ("/chargers/b/same", "/connectors/b/same/1",
                "/chargers/a/other-does-not-exist", "/connectors/a/same/9"):
        assert client.get(url).status_code == 404
    assert client.get("/connectors/a/same/-1").status_code == 422
    assert client.get("/connectors/a/same/1?tx_page=0").status_code == 422
    assert client.get("/chargers/a/same?event_page=0").status_code == 422


def test_connector_sql_counts_and_pagination_precede_limit(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        for n in range(101, 141):
            payload = {"id": n, "charger_id": "same", "connector_id": 1,
                       "state": "closed", "active": False,
                       "energy_wh": 1400, "started_at": "2026-11-11T01:00:00Z",
                       "stopped_at": "2026-11-11T01:30:00Z"}
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ("a", "same", n, 0, 0, "digest", json.dumps(payload),
                        "2026-11-11T02:00:00Z"))
        # Same charger in another source and different connector in source a.
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("b", "transaction", "same:999", 999,
                    json.dumps({"id": 999, "charger_id": "same", "connector_id": 1})))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    first = client.get("/connectors/a/same/1")
    second = client.get("/connectors/a/same/1?tx_page=2")
    third = client.get("/connectors/a/same/1?tx_page=3")
    assert all(page.status_code == 200 for page in (first, second, third))
    assert "42</strong>" in first.text
    assert "1–20 de 42" in first.text
    assert "21–40 de 42" in second.text
    assert "41–42 de 42" in third.text
    assert ">999</a>" not in first.text
    assert ">21</a>" not in third.text
    page = snapshot(
        path, source_id="a", charger_id="same", allowed_source_ids=("a",),
        transaction_pagination={"term": "", "status": "", "location": "",
                                "date_from": "", "date_to": "", "zone": "UTC",
                                "view": "all", "page": 1, "connector_id": 2})
    assert page["transaction_pagination"]["total"] == 2
    assert {r["connector_id"] for r in page["transactions"]} == {2}


def test_event_history_is_exact_charger_and_source_only(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    seed(path)
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    scoped = client.get("/api/event-group", params={
        "group_source": "a", "source_id": "a", "charger_id": "same",
        "first_event_id": 1})
    assert scoped.status_code == 200
    assert [e["charger_id"] for e in scoped.json()["events"]] == ["same"]
    assert "never-expose" not in scoped.text
    assert client.get("/api/event-group", params={
        "group_source": "b", "source_id": "b", "charger_id": "same",
        "first_event_id": 1}).status_code == 403
    assert client.get("/api/event-group", params={
        "group_source": "a", "source_id": "a", "charger_id": "same",
        "first_event_id": 2}).status_code == 404


def test_station_c0_is_excluded_and_heartbeat_uses_real_charger_events(tmp_path):
    from ocpp_reports.warehouse.event_groups import latest_heartbeat_at
    path = tmp_path / "station.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        for connector_id in (0, 1, 2):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
                "a", "connector", f"same:{connector_id}", 10 + connector_id,
                json.dumps({"charger_id": "same", "connector_id": connector_id,
                            "status": "Available", "connected": True,
                            "observed_at": "2026-11-17T10:00:00Z"})))
        for source, event_id, charger, action, at in (
            ("a", 3, "same", "Heartbeat", "2026-11-17T11:00:00Z"),
            ("a", 4, "other", "Heartbeat", "2026-11-17T13:00:00Z"),
            ("a", 5, "same", "StatusNotification", "2026-11-17T15:00:00Z"),
            ("b", 3, "same", "Heartbeat", "2026-11-17T17:00:00Z"),
        ):
            db.execute("INSERT INTO events VALUES (?,?,?)", (
                source, event_id, json.dumps({"id": event_id,
                    "charger_id": charger, "action": action, "kind": "ocpp", "at": at})))
    assert latest_heartbeat_at(path, source_id="a", charger_id="same",
                               allowed_source_ids=("a",)) == "2026-11-17T11:00:00Z"
    assert latest_heartbeat_at(path, source_id="a", charger_id="other",
                               allowed_source_ids=("a",)) == "2026-11-17T13:00:00Z"
    assert latest_heartbeat_at(path, source_id="b", charger_id="same",
                               allowed_source_ids=("a",)) is None
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    page = client.get("/chargers/a/same?tz=UTC")
    assert page.status_code == 200
    assert '<span class="note">Conectores visibles</span><strong>2</strong>' in page.text
    assert "Estación (C0)" in page.text
    assert "Último Heartbeat" in page.text and "2026-11-17 11:00 UTC" in page.text
    assert "Frescura del origen" not in page.text
    assert 'id="synchronization"' not in page.text
    assert "Las fechas observadas corresponden" not in page.text
    assert 'class="events-table"' in page.text and 'id="events"' in page.text
    assert 'class="event-summary-row"' in page.text
    assert "data-first-id=" in page.text
    assert page.text.index('id="transactions"') < page.text.index('id="events"')


def test_replica_heartbeat_is_scoped_and_never_uses_sync_timestamp(tmp_path):
    from ocpp_reports.warehouse.event_groups import latest_heartbeat_at
    path = tmp_path / "replica.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
            "a", "event", "same:7", 77,
            json.dumps({"id": 7, "charger_id": "same", "kind": "ocpp",
                        "action": "Heartbeat", "at": "2026-11-17T12:30:00Z"})))
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
            "b", "event", "same:8", 78,
            json.dumps({"id": 8, "charger_id": "same", "kind": "ocpp",
                        "action": "Heartbeat", "at": "2026-11-17T19:45:00Z"})))
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
            "a", "event", "other:9", 79,
            json.dumps({"id": 9, "charger_id": "other", "kind": "ocpp",
                        "action": "Heartbeat", "at": "2026-11-17T20:00:00Z"})))
    assert latest_heartbeat_at(path, source_id="a", charger_id="same",
                               allowed_source_ids=("a",)) == "2026-11-17T12:30:00Z"
    assert latest_heartbeat_at(path, source_id="a", charger_id="missing",
                               allowed_source_ids=("a",)) is None
    assert latest_heartbeat_at(path, source_id="b", charger_id="same",
                               allowed_source_ids=("a",)) is None
    page = TestClient(create_app(path, allowed_source_ids=("a",))).get(
        "/chargers/a/same?tz=UTC")
    assert page.status_code == 200
    assert "2026-11-17 12:30 UTC" in page.text
    assert "2026-11-17 19:45 UTC" not in page.text
    assert "2026-11-17 20:00 UTC" not in page.text
    blank = TestClient(create_app(path, allowed_source_ids=("a",))).get(
        "/chargers/a/missing?tz=UTC")
    assert blank.status_code == 404


def test_charger_without_heartbeat_shows_missing_date_and_no_sync_panel(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    seed(path)
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    page = client.get("/chargers/a/same")
    assert page.status_code == 200
    assert '<span class="note">Último Heartbeat</span><strong>Sin fecha</strong>' in page.text
    assert "Sincronización" not in page.text
    assert 'id="events"' in page.text
    assert 'class="events-table"' in page.text
    connector = client.get("/connectors/a/same/1")
    assert connector.status_code == 200
    assert 'id="synchronization"' not in connector.text
    assert 'id="events"' not in connector.text
