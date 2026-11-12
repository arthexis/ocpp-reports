"""Read-only issue #209 regression gates: freshness, chronology, parity, cost."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from time import perf_counter
from urllib.parse import parse_qs, urlparse
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.warehouse.active_sessions import is_active_session, _lookup_transaction
from ocpp_reports.web import create_app


def _stamp(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def _tx(n=7, *, active=True, started=None, charger="cp"):
    return dict(id=n, charger_id=charger, connector_id=1, active=active,
                state="open" if active is True else "closed",
                started_at=started or _stamp(-120), stopped_at=None if active is True else _stamp(-60),
                energy_wh=None if active is True else 1000, live_energy_wh=450,
                live_energy_observed_at=_stamp(-15), soc_start=30, soc_end=54)


def _connector(n=7, *, charger="cp", power_at=None):
    return dict(charger_id=charger, connector_id=1, status="Charging",
                transaction_id=n, connected=True, power_w=7400,
                power_observed_at=power_at or _stamp(-25))


def _seed(path, source="a", *, receipt=None, tx=None, connector=None):
    with sqlite3.connect(path) as db:
        ensure_collector_schema(db)
        tx = tx or _tx()
        conn = connector or _connector()
        db.execute("INSERT OR REPLACE INTO collector_latest VALUES (?,?,?,?,?)",
                   (source, "transaction", f"{tx['charger_id']}:{tx['id']}", 1, json.dumps(tx)))
        db.execute("INSERT OR REPLACE INTO collector_latest VALUES (?,?,?,?,?)",
                   (source, "connector", f"{conn['charger_id']}:{conn['connector_id']}", 2, json.dumps(conn)))
        if receipt is not None:
            db.execute("INSERT OR REPLACE INTO collector_receipts VALUES (?,?,?)",
                       (source, receipt, 2))


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def test_fresh_vs_stale_label_must_not_assert_live_without_recent_receipt(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _seed(path, receipt=_stamp(-900))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    old_page = client.get("/?tx_view=active").text
    assert "Ver sesión informada (sin confirmar)" in old_page
    assert "Ver carga actual" not in old_page
    assert "/transactions/a/cp/7?tz=UTC" in old_page
    assert client.get("/api/v1/transactions?view=active").json()["total"] == 1
    with sqlite3.connect(path) as db:
        db.execute("UPDATE collector_receipts SET received_at=? WHERE source_id='a'", (_stamp(),))
    fresh_page = client.get("/?tx_view=active").text
    assert "Ver carga actual" in fresh_page
    assert "Ver sesión informada (sin confirmar)" not in fresh_page
    # Missing source freshness and negative connection evidence are NOT live proof.
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM collector_receipts WHERE source_id='a'")
    assert "Ver sesión informada (sin confirmar)" in client.get("/").text


def test_recent_pre_start_and_future_meter_power_are_never_attributed(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    # The previous session's sample is 90s old, but the new session is only 60s old.
    _seed(path, receipt=_stamp(), tx=_tx(started=_stamp(-60)),
          connector=_connector(power_at=_stamp(-90)))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    page = client.get("/transactions/a/cp/7")
    assert "Sin potencia atribuible a esta sesión" in page.text
    assert "7.40 kW" not in page.text
    with sqlite3.connect(path) as db:
        record = _connector(power_at=_stamp(-20))
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='connector'",
                   (json.dumps(record),))
    assert "7.40 kW" in client.get("/transactions/a/cp/7").text
    with sqlite3.connect(path) as db:
        record["power_observed_at"] = _stamp(600)
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='connector'",
                   (json.dumps(record),))
    assert "7.40 kW" not in client.get("/transactions/a/cp/7").text


def test_boolean_only_active_is_identical_in_sql_and_python(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _seed(path, receipt=_stamp())
    with sqlite3.connect(path) as db:
        for n, flag in ((30, 1), (31, "true"), (32, False), (33, None)):
            tx = _tx(n)
            tx["active"] = flag
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("a", "transaction", f"cp:{n}", n, json.dumps(tx)))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    api = client.get("/api/v1/transactions?view=active").json()
    assert api["counts"]["active"] == 1
    assert [t["id"] for t in api["items"]] == [7]
    assert "En curso (1)" in client.get("/?tx_view=active").text
    assert is_active_session(_tx(active=True))
    assert not is_active_session(_tx(active=1))
    assert not is_active_session(_tx(active="true"))


def test_charging_card_resets_historical_filters_but_preserves_source_and_tz(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _seed(path, receipt=_stamp())
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    html = client.get("/", params={"source_id": "a", "charger_id": "cp",
                     "filter": "closed", "status": "Available",
                     "date_from": "2000-01-01", "date_to": "2001-01-01",
                     "tz": "UTC", "tx_view": "failed"}).text
    parser = _Links()
    parser.feed(html)
    charging = [a for a in parser.links if "operational-card--charging" in a.get("class", "")]
    assert len(charging) == 1
    href = charging[0]["href"]
    parsed = urlparse(href)
    args = parse_qs(parsed.query)
    assert args == {"tx_view": ["active"], "tz": ["UTC"], "source_id": ["a"],
                    "charger_id": ["cp"]}
    assert parsed.fragment == "transactions"
    assert 'button.dataset.txView === "active"' in html
    assert 'next.searchParams.delete(key)' in html
    assert client.get(href).status_code == 200
    assert "En curso (1)" in client.get(href).text


def test_soc_metadata_refresh_is_not_a_measured_soc_timestamp(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    tx = _tx()
    tx["soc_metadata_provenance"] = "ocpp-meter-values"
    tx["soc_metadata_refreshed_at"] = "2000-01-01T01:23:45Z"
    _seed(path, receipt=_stamp(), tx=tx)
    html = TestClient(create_app(path, allowed_source_ids=("a",))).get("/transactions/a/cp/7").text
    assert "OCPP MeterValues" in html
    assert "sin hora de medición verificada" in html
    assert "2000-01-01" not in html
    assert "54%" in html


def test_indexed_collector_keys_and_bounded_page_with_long_history(tmp_path):
    path = tmp_path / "warehouse.sqlite"
    _seed(path, receipt=_stamp())
    with sqlite3.connect(path) as db:
        for n in range(1000, 3000):
            tx = _tx(n, active=False)
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("a", "transaction", f"cp:{n}", n, json.dumps(tx)))
        # Canonical lookup must use composite PK instead of JSON scans.
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM collector_latest WHERE source_id=? "
            "AND kind='transaction' AND record_key=? LIMIT 1",
            ("a", "cp:7")).fetchall()
        assert any("USING INDEX" in str(row[3]).upper() for row in plan), plan
        seen = []
        db.set_trace_callback(seen.append)
        tables = {"collector_latest"}
        assert _lookup_transaction(db, tables, "a", "cp", 7)["id"] == 7
        assert _lookup_transaction(db, tables, "a", "cp", 99999) is None
        db.set_trace_callback(None)
        assert len(seen) == 3, seen  # 1 canonical + 2 indexed misses
        assert all("record_key=" in statement for statement in seen)
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    samples = []
    for _ in range(5):
        began = perf_counter()
        response = client.get("/api/v1/transactions?view=active")
        samples.append(perf_counter() - began)
        assert response.status_code == 200
        assert response.json()["total"] == 1
    assert sorted(samples)[4] < 1.0, samples
    assert client.get("/api/v1/transactions?view=all&page=2").json()["total"] == 2001
