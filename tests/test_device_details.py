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
    assert "Eventos registrados de same" in charger.text
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
    assert 'class="event-expand-row" hidden' in page.text
    assert page.text.count('class="event-summary-row"') == page.text.count('class="event-expand-row" hidden')
    assert '<td class="event-action"><details class="event-group"' in page.text
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



def test_last_charger_receipt_is_not_source_ingest_or_last_heartbeat(tmp_path):
    from ocpp_reports.warehouse.event_groups import (
        latest_charger_receipt_at, latest_heartbeat_at)
    path = tmp_path / "receipts.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sources SET last_ingested_at=? WHERE source_id='a'",
                   ("2026-11-17T18:00:00Z",))
        for source, event_id, charger, action, at in (
            ("a", 3, "same", "Heartbeat", "2026-11-15T10:00:00Z"),
            ("a", 4, "same", "StatusNotification", "2026-11-16T10:00:00Z"),
            ("a", 5, "other", "StatusNotification", "2026-11-17T15:00:00Z"),
            ("b", 3, "same", "Heartbeat", "2026-11-17T19:00:00Z"),
        ):
            db.execute("INSERT INTO events VALUES (?,?,?)", (
                source, event_id, json.dumps({"id": event_id, "charger_id": charger,
                    "kind": "ocpp", "action": action, "at": at})))
    assert latest_heartbeat_at(path, source_id="a", charger_id="same",
                               allowed_source_ids=("a",)) == "2026-11-15T10:00:00Z"
    assert latest_charger_receipt_at(path, source_id="a", charger_id="same",
                                     allowed_source_ids=("a",)) == "2026-11-16T10:00:00Z"
    assert latest_charger_receipt_at(path, source_id="b", charger_id="same",
                                     allowed_source_ids=("a",)) is None
    page = TestClient(create_app(path, allowed_source_ids=("a",))).get(
        "/chargers/a/same?tz=UTC")
    assert page.status_code == 200
    assert '<span class="note">Último Heartbeat</span><strong>2026-11-15 10:00 UTC</strong>' in page.text
    assert "Último recibo</span><strong>2026-11-16 10:00 UTC</strong>" in page.text
    assert "2026-11-17 18:00 UTC" not in page.text
    assert 'id="energy-30-days"' in page.text
    assert page.text.count('id="energy-chart"') == 1
    assert 'data-days="7" data-view="total" role="list"' in page.text
    assert 'data-energy-view="total" aria-pressed="true"' in page.text
    assert 'data-energy-view="connectors" aria-pressed="false"' in page.text
    assert page.text.count('class="energy-bar"') == 30
    assert 'data-energy-days="7" aria-label="7 días" aria-pressed="true"' in page.text
    assert 'data-energy-days="30" aria-label="30 días" aria-pressed="false"' in page.text
    connector = TestClient(create_app(path, allowed_source_ids=("a",))).get(
        "/connectors/a/same/1")
    assert connector.status_code == 200 and 'id="energy-30-days"' not in connector.text


def test_last_charger_receipt_remote_projection_and_empty(tmp_path):
    from ocpp_reports.warehouse.event_groups import latest_charger_receipt_at
    path = tmp_path / "remote.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        for src, record, event_id, charger, at in (
            ("a", "same:99", 99, "same", "2026-11-17T07:45:00Z"),
            ("a", "other:100", 100, "other", "2026-11-17T10:45:00Z"),
            ("b", "same:101", 101, "same", "2026-11-17T11:45:00Z"),
        ):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       (src, "event", record, event_id, json.dumps({
                           "id": event_id, "charger_id": charger, "kind": "ocpp",
                           "action": "StatusNotification", "at": at})))
    assert latest_charger_receipt_at(path, source_id="a", charger_id="same",
                                     allowed_source_ids=("a",)) == "2026-11-17T07:45:00Z"
    assert latest_charger_receipt_at(path, source_id="a", charger_id="missing",
                                     allowed_source_ids=("a",)) is None


def test_charger_energy_30_days_local_remote_dedup_and_timezone(tmp_path):
    from datetime import datetime, timezone
    from ocpp_reports.warehouse.charger_energy import charger_energy_30_days
    path = tmp_path / "energy.sqlite"
    seed(path)
    now = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        # Same local transaction is also replicated: it must count only once.
        for source, kind, key, seq, tx in (
            ("a", "transaction", "same:11", 3,
             dict(id=11, charger_id="same", connector_id=1, active=False,
                  energy_wh=9999999, stopped_at="2026-11-17T14:00:00Z")),
            ("a", "transaction", "same:77", 4,
             dict(id=77, charger_id="same", connector_id=2, active=False,
                  energy_wh=3000, stopped_at="2026-11-17T05:30:00Z")),
            ("a", "transaction", "same:78", 5,
             dict(id=78, charger_id="same", connector_id=0, active=False,
                  energy_wh=70000, stopped_at="2026-11-17T16:00:00Z")),
            ("a", "transaction", "same:79", 6,
             dict(id=79, charger_id="same", connector_id=1, active=True,
                  energy_wh=9000, stopped_at="2026-11-17T16:00:00Z")),
            ("a", "transaction", "same:80", 7,
             dict(id=80, charger_id="same", connector_id=1, active=False,
                  energy_wh=-1000, stopped_at="2026-11-17T16:00:00Z")),
            ("a", "transaction", "other:81", 8,
             dict(id=81, charger_id="other", connector_id=1, active=False,
                  energy_wh=4000, stopped_at="2026-11-17T16:00:00Z")),
            ("b", "transaction", "same:82", 9,
             dict(id=82, charger_id="same", connector_id=1, active=False,
                  energy_wh=7000, stopped_at="2026-11-17T16:00:00Z")),
        ):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       (source, kind, key, seq, json.dumps(tx)))
    data = charger_energy_30_days(
        path, source_id="a", charger_id="same",
        allowed_source_ids=("a",), zone="America/Monterrey", now=now)
    assert len(data["bars"]) == 30
    assert data["date_from"] == "2026-10-19" and data["date_to"] == "2026-11-17"
    assert data["sessions_count"] == 5
    assert data["total_kwh"] == 9.0
    assert data["last_7"] == {"total_kwh": 3.0, "sessions_count": 1}
    assert data["connector_ids"] == [1, 2]
    assert sum(c["kwh"] for bar in data["bars"] for c in bar["connectors"]) == data["total_kwh"]
    assert data["bars"][-2]["connectors"][1]["kwh"] == 3.0
    assert sum(1 for bar in data["bars"] if bar["label_7"]) == 7
    assert data["bars"][-2]["height_pct_7"] == 100  # Nov 16: the only energy in last 7 days
    assert data["bars"][-1]["height_pct_7"] == 0
    assert 0 < data["bars"][-2]["height_pct_30"] < 100
    days = {item["day"]: item for item in data["bars"]}
    assert days["2026-11-16"]["kwh"] == 3.0  # UTC 05:30 = prior local day
    assert days["2026-11-09"]["kwh"] == 6.0  # 01:30 UTC is 19:30 on Nov 9 in Monterrey
    assert days["2026-11-17"]["kwh"] == 0
    assert days["2026-11-16"]["sessions"] == 1
    assert all(item["height"] >= 0 for item in data["bars"])
    import pytest
    with pytest.raises(ValueError, match="authorized"):
        charger_energy_30_days(path, source_id="b", charger_id="same",
                               allowed_source_ids=("a",), now=now)


def test_charger_energy_30_days_empty_and_unparseable(tmp_path):
    from datetime import datetime, timezone
    from ocpp_reports.warehouse.charger_energy import charger_energy_30_days
    path = tmp_path / "empty.sqlite"
    seed(path)
    today = datetime(2026, 12, 20, 12, tzinfo=timezone.utc)
    data = charger_energy_30_days(
        path, source_id="a", charger_id="same",
        allowed_source_ids=("a",), now=today)
    assert data["total_kwh"] == 0 and data["sessions_count"] == 0
    assert data["last_7"] == {"total_kwh": 0.0, "sessions_count": 0}
    assert data["connector_ids"] == []
    assert all(item["height"] == 0 for item in data["bars"])
    assert all(item["height_pct_7"] == 0 and item["height_pct_30"] == 0
               for item in data["bars"])
    # No false bars from a missing/broken final timestamp.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "transaction", "same:99", 99, json.dumps({
                       "id": 99, "charger_id": "same", "connector_id": 1,
                       "active": False, "energy_wh": 999,
                       "stopped_at": "not-a-date"})))
    assert charger_energy_30_days(
        path, source_id="a", charger_id="same",
        allowed_source_ids=("a",), now=today)["sessions_count"] == 0



def test_charger_energy_buttons_are_single_chart_and_mobile_fields_removed(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
            "a", "connector", "same:1", 17, json.dumps({
                "charger_id": "same", "connector_id": 1,
                "status": "Available", "connected": True, "energy_wh": 25000,
                "observed_at": "2026-11-17T15:00:00Z"
            })))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    page = client.get("/chargers/a/same?tz=UTC")
    assert page.status_code == 200
    body = page.text
    assert body.count('id="energy-chart"') == 1
    assert body.count('class="energy-bar"') == 30
    assert 'data-energy-days="7" aria-label="7 días" aria-pressed="true"' in body
    assert 'data-energy-days="30" aria-label="30 días" aria-pressed="false"' in body
    assert 'data-energy-view="total" aria-pressed="true">Estación</button>' in body
    assert 'data-energy-view="connectors" aria-pressed="false"' in body
    assert 'data-days="7" data-view="total" role="list"' in body
    assert "<h2 id=\"energy-30-title\">Energía</h2>" in body
    assert 'id="energy-help" hidden' in body
    assert body.count('id="energy-legend"') == 1
    assert 'data-total-7=' in body and 'data-total-30=' in body
    assert 'overflow-x:auto' not in body.split(".energy-days{")[1].split(".energy-day{")[0]
    assert "min-width:850px" not in body
    assert 'class="sessions-table"' in body
    assert '<th class="session-start">Inicio</th>' in body
    assert '<td class="session-start">' in body
    assert ".sessions-table .session-start,.sessions-table .session-duration{display:none}" in body
    assert '<th class="session-duration">Duración</th>' in body
    for obsolete in ("<dt>Conexión observada</dt>", "<dt>Último estado</dt>",
                     "<dt>Sincronizado</dt>", "<dt>Medición de potencia</dt>"):
        assert obsolete not in body
    for kept in ("<dt>Estado informado</dt>", "<dt>Potencia</dt>",
                 "<dt>Medidor acumulado</dt>", "<dt>Medición de energía</dt>"):
        assert kept in body
    for caption in ("Último Heartbeat", "Último recibo", "Conectores visibles"):
        assert caption in body
    connector = client.get("/connectors/a/same/1?tz=UTC")
    assert connector.status_code == 200
    assert 'id="energy-chart"' not in connector.text
    assert '<th class="session-start">Inicio</th>' in connector.text
    assert "Medidor acumulado" in connector.text
    assert "Medición de potencia" not in connector.text


def test_charger_energy_recent_window_scales_independently(tmp_path):
    from datetime import datetime, timezone
    from ocpp_reports.warehouse.charger_energy import charger_energy_30_days
    path = tmp_path / "recent.sqlite"
    seed(path)
    now = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
    # Seed 10-day and 2-day completed sessions: one belongs only to 30 days.
    with sqlite3.connect(path) as db:
        for tid,day,kwh in ((201, 7, 3000), (202, 15, 2000)):
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ("a", "same", tid, 0, 0, "digest",
                        json.dumps({"id": tid, "charger_id": "same", "connector_id": 1,
                                    "active": False, "energy_wh": kwh,
                                    "stopped_at": f"2026-11-{day:02d}T14:00:00Z"}),
                        "2026-11-17T16:00:00Z"))
    data = charger_energy_30_days(path, source_id="a", charger_id="same",
                                   allowed_source_ids=("a",), now=now)
    assert data["total_kwh"] == 11.0  # Four existing x 1.5 + 3 + 2
    assert data["sessions_count"] == 6
    assert data["last_7"] == {"total_kwh": 2.0, "sessions_count": 1}
    last = data["bars"][-7:]
    assert len(last) == 7
    assert max(item["height_pct_7"] for item in last) == 100
    assert all(item["height_pct_7"] == 0 for item in data["bars"][:-7])
    assert max(item["height_pct_30"] for item in data["bars"]) == 100



def test_charger_energy_split_by_physical_connector_exactly_matches_total(tmp_path):
    """C0 must never double-count C1 + C2; duplicates and bad sessions excluded."""
    from datetime import datetime, timezone
    from ocpp_reports.warehouse.charger_energy import charger_energy_30_days
    path = tmp_path / "split.sqlite"
    seed(path)
    now = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        for tid, connector, wh, stopped, active in (
            (701, 1, 2000, "2026-11-17T10:00:00Z", False),
            (702, 2, 4500, "2026-11-17T10:00:00Z", False),
            (703, 0, 6500, "2026-11-17T10:00:00Z", False),
            (704, 2, 2500, "2026-11-10T03:00:00Z", False),
            (705, 2, 9000, "2026-11-17T10:00:00Z", True),
            (706, 2, -999, "2026-11-17T10:00:00Z", False),
        ):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
                "a", "transaction", f"same:{tid}", tid,
                json.dumps({"id": tid, "charger_id": "same",
                            "connector_id": connector, "energy_wh": wh,
                            "stopped_at": stopped, "active": active})))
        # This remote projection duplicates a local transaction and must lose.
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)", (
            "a", "transaction", "same:11", 90,
            json.dumps({"id": 11, "charger_id": "same", "connector_id": 2,
                        "energy_wh": 900000, "active": False,
                        "stopped_at": "2026-11-17T10:00:00Z"})))
    data = charger_energy_30_days(
        path, source_id="a", charger_id="same", allowed_source_ids=("a",),
        zone="America/Monterrey", now=now, physical_connector_ids=(0, 1, 2))
    assert data["connector_ids"] == [1, 2]
    assert data["total_kwh"] == 15.0  # local 4x1.5 + 2 + 4.5 + 2.5
    assert data["last_7"] == {"total_kwh": 6.5, "sessions_count": 2}
    assert sum(c["kwh"] for day in data["bars"] for c in day["connectors"]) == 15.0
    day17 = next(item for item in data["bars"] if item["day"] == "2026-11-17")
    assert [c["kwh"] for c in day17["connectors"]] == [2.0, 4.5]
    assert day17["kwh"] == 6.5
    assert max(c["height_pct_7"] for day in data["bars"][-7:]
               for c in day["connectors"]) == 100
    assert "C1: 2.00 kWh" in day17["connector_description"]
    assert "C2: 4.50 kWh" in day17["connector_description"]
    assert all(c["id"] > 0 for bar in data["bars"] for c in bar["connectors"])
    other = charger_energy_30_days(
        path, source_id="a", charger_id="same", allowed_source_ids=("a",),
        now=now, physical_connector_ids=(0, 1, 2, 3))
    assert other["connector_ids"] == [1, 2, 3]
    assert all(c["kwh"] == 0 for bar in other["bars"]
               for c in bar["connectors"] if c["id"] == 3)



def test_device_event_pagination_is_charger_scoped_across_multiple_pages(tmp_path):
    path = tmp_path / "events.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        for event_id in range(11, 27):
            db.execute("INSERT INTO events VALUES (?,?,?)", (
                "a", event_id, json.dumps({
                    "id": event_id, "charger_id": "same",
                    "action": "Heartbeat" if event_id % 3 == 0 else "BootNotification",
                    "kind": "ocpp", "at": f"2026-11-17T10:{event_id:02d}:00Z",
                    "rfid": "should-never-appear",
                })))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    first = client.get("/chargers/a/same?event_page=1")
    second = client.get("/chargers/a/same?event_page=2")
    assert first.status_code == second.status_code == 200
    assert first.text.count('class="event-summary-row"') == 10
    assert second.text.count('class="event-summary-row"') >= 1
    assert "Siguiente" in first.text and "Anterior" in second.text
    assert "should-never-appear" not in first.text + second.text
    assert 'id="events"' in first.text
    assert client.get("/connectors/a/same/1").text.find('id="events"') == -1
    event = client.get("/api/v1/charger-event-group", params={
        "source_id": "a", "charger_id": "same", "first_event_id": 26
    })
    assert event.status_code == 200
    assert event.json()["events"][0]["id"] == 26
    assert "should-never-appear" not in event.text
    assert client.get("/api/v1/charger-event-group", params={
        "source_id": "b", "charger_id": "same", "first_event_id": 26
    }).status_code == 404


def test_desktop_charger_session_soc_is_recorded_not_inferred(tmp_path):
    path = tmp_path / "soc.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT payload FROM current_transactions "
            "WHERE source_id='a' AND charger_id='same' AND transaction_id=11").fetchone()
        payload = json.loads(row[0])
        payload.update(soc_start=18, soc_end=82)
        db.execute(
            "UPDATE current_transactions SET payload=? "
            "WHERE source_id='a' AND charger_id='same' AND transaction_id=11",
            (json.dumps(payload),))
    page = TestClient(create_app(path, allowed_source_ids=("a",))).get("/chargers/a/same")
    assert page.status_code == 200
    assert '<th class="numeric session-soc"' in page.text
    assert 'SoC inicial</th>' in page.text
    assert 'SoC último</th>' in page.text
    assert '18%</td>' in page.text and '82%</td>' in page.text
    assert '<td class="numeric session-soc">—</td>' in page.text
    assert "@media(max-width:900px){.sessions-table .session-soc{display:none}}" in page.text
    # The same session-scoped fields render without cross-tenant leakage.
    connector = TestClient(create_app(path, allowed_source_ids=("a",))).get(
        "/connectors/a/same/1")
    assert connector.status_code == 200 and "18%</td>" in connector.text



def test_last_completed_charge_uses_all_scoped_transactions_not_visible_page(tmp_path):
    from ocpp_reports.warehouse.transaction_pagination import latest_completed_charges

    path = tmp_path / "last-charge.sqlite"
    seed(path)
    with sqlite3.connect(path) as db:
        # Show three actual connector rows; seed() only adds transaction history.
        for connector_id in (0, 1, 2):
            payload = {"charger_id": "same", "connector_id": connector_id,
                       "status": "Available", "connected": True,
                       "observed_at": "2026-11-16T06:00:00Z"}
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("a", "connector", f"same:{connector_id}", 50, json.dumps(payload)))
        def add(source, record_key, charge, connector, stopped_at):
            record = dict(id=int(record_key.split(":")[-1]),
                          charger_id=charge, connector_id=connector,
                          started_at="2026-11-14T00:00:00Z",
                          stopped_at=stopped_at,
                          active=stopped_at is None,
                          state="stopped" if stopped_at else "open",
                          energy_wh=1200 if stopped_at else None)
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       (source, "transaction", record_key, 200,
                        json.dumps(record)))
        add("a", "same:501", "same", 1, "2026-11-16T05:10:00Z")
        add("a", "same:502", "same", 1, None)  # Newer active session is not final.
        add("a", "different:503", "different", 1, "2099-11-16T05:10:00Z")
        add("b", "same:504", "same", 1, "2099-11-16T05:10:00Z")
    visible = [
        dict(source_id="a", charger_id="same", connector_id=i)
        for i in (0, 1, 2)
    ]
    seen = latest_completed_charges(path, visible, allowed_source_ids=("a",))
    assert seen[("a", "same", 1)] == "2026-11-16T05:10:00Z"
    assert seen[("a", "same", 2)] == "2026-11-10T01:30:00Z"
    assert ("a", "same", 0) not in seen
    assert all(key[0] == "a" for key in seen)
    page = TestClient(create_app(path, allowed_source_ids=("a",))).get(
        "/?connector_view=combined&tz=UTC")
    assert page.status_code == 200
    assert '<th class="connector-last-charge">Última carga</th>' in page.text
    assert 'data-label="Última carga">2026-11-16 05:10 UTC' in page.text
    connectors_html = page.text.split('id="connectors"', 1)[1].split("</section>", 1)[0]
    assert "2099-11-16" not in connectors_html
    assert 'class="connector-last-charge" data-label="Última carga">—' in connectors_html
    assert ".connectors-panel .connector-last-charge{display:none}" in page.text
    assert "@media(min-width:1101px)" in page.text


def test_charger_and_connector_detail_headers_inherit_instance_branding(tmp_path):
    path = tmp_path / "brand-detail.sqlite"
    seed(path)
    logo = "/assets/brand-light.png"
    client = TestClient(create_app(path, title="Customer Dashboard",
                                   allowed_source_ids=("a",), brand_image_url=logo))
    plain = TestClient(create_app(path, title="Other Dashboard", allowed_source_ids=("a",)))
    for route in ("/chargers/a/same", "/connectors/a/same/1"):
        branded = client.get(route)
        assert branded.status_code == 200
        assert '<img class="brand-logo" src="/assets/brand-light.png" alt="Customer Dashboard"' in branded.text
        assert '<h1 hidden>Customer Dashboard</h1>' in branded.text
        assert 'onerror="this.hidden=true;this.nextElementSibling.hidden=false"' in branded.text
        assert 'max-height:56px' in branded.text
        assert "Customer Dashboard</title>" not in branded.text  # contextual page title still present
        unbranded = plain.get(route)
        assert unbranded.status_code == 200
        assert '<h1>Other Dashboard</h1>' in unbranded.text
        assert 'class="brand-logo"' not in unbranded.text
