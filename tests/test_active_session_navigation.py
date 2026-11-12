"""Offline acceptance for issue #209: live navigation, scope and progress."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ocpp_reports.web import create_app
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def _warehouse(path):
    with sqlite3.connect(path) as db:
        ensure_collector_schema(db)
    return path


def _add(db, source, kind, key, data):
    db.execute(
        "INSERT INTO collector_latest(source_id,kind,record_key,sequence,payload) VALUES (?,?,?,?,?)",
        (source, kind, key, 1, json.dumps(data)),
    )


def _active(tx_id, started, *, connector_id=1):
    return {
        "id": tx_id, "charger_id": "cp", "connector_id": connector_id,
        "active": True, "state": "open", "started_at": started,
        "stopped_at": None, "energy_wh": None, "live_energy_wh": 34750,
        "live_energy_observed_at": datetime.now(timezone.utc).isoformat(),
        "soc_start": 25, "soc_end": 71,
    }


def _connector(tx_id, observed, *, status="Charging", connector_id=1):
    return {
        "charger_id": "cp", "connector_id": connector_id,
        "status": status, "transaction_id": tx_id, "connected": True,
        "power_w": 7200, "power_observed_at": observed,
    }


def _seed(path, *, tx_id=7, source="a"):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as db:
        _add(db, source, "transaction", f"cp:{tx_id}", _active(tx_id, (now - timedelta(minutes=12)).isoformat()))
        _add(db, source, "connector", "cp:1", _connector(tx_id, now.isoformat()))
        db.execute("INSERT INTO collector_receipts VALUES (?,?,?)", (source, now.isoformat(), 1))
    return now


def test_current_link_and_progress_with_confirmed_scope(tmp_path):
    path = _warehouse(tmp_path / "warehouse.sqlite")
    _seed(path)
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    page = client.get("/?tx_view=active")
    assert page.status_code == 200
    assert 'class="charger-session-link"' in page.text
    assert 'class="status-session-link"' in page.text
    assert "current-session-link" not in page.text
    assert 'aria-label="Ver carga actual del cargador cp, conector 1"' in page.text
    assert 'aria-label="Ver carga actual desde el estado del cargador cp, conector 1"' in page.text
    assert '/transactions/a/cp/7?tz=UTC' in page.text
    assert "En curso (1)" in page.text
    assert "Mostrando 1–1 de 1" in page.text
    assert "tx_view=active" in page.text and "#transactions" in page.text
    detail = client.get("/transactions/a/cp/7")
    assert detail.status_code == 200
    assert "34.75 kWh" in detail.text and "Estimada" in detail.text
    assert "7.20 kW" in detail.text and "Medición reciente" in detail.text
    assert "Duración transcurrida (aprox.)" in detail.text
    assert "window.setInterval(refreshTransaction, 15000)" in detail.text
    assert "new DOMParser()" in detail.text
    assert 'http-equiv="refresh"' not in detail.text
    assert "0:12:" in detail.text
    assert "71%" in detail.text
    assert "sin hora de medición verificada" in detail.text
    assert '@media(max-width:540px)' in detail.text
    assert detail.headers["cache-control"] == "no-store"
    api = client.get("/api/v1/transactions?view=active").json()
    assert api["counts"]["active"] == 1 and api["total"] == 1
    assert api["items"][0]["id"] == 7
    assert client.get("/api/v1/transactions?view=bad").status_code == 422
    assert client.get("/api/v1/transactions?source_id=b&view=active").status_code == 403


def test_active_pagination_not_hidden_behind_completed_history(tmp_path):
    path = _warehouse(tmp_path / "warehouse.sqlite")
    now = _seed(path)
    with sqlite3.connect(path) as db:
        for index in range(1, 48):
            tx = {**_active(index + 100, now.isoformat()),
                  "active": False, "state": "closed",
                  "stopped_at": now.isoformat(), "energy_wh": 1000}
            _add(db, "a", "transaction", f"closed:{index}", tx)
        _add(db, "b", "transaction", "cp:7", _active(7, now.isoformat()))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    result = client.get("/api/v1/transactions?view=active").json()
    assert result["counts"] == {"all": 48, "success": 47, "failed": 0, "active": 1}
    assert result["items"][0]["id"] == 7
    assert client.get("/?tx_view=active&tx_page=2").status_code == 200
    assert "En curso (1)" in client.get("/?tx_view=active").text
    assert client.get("/api/v1/transactions?view=success").json()["total"] == 47


def test_reuse_connector_mismatch_and_stale_power_are_explicit(tmp_path):
    path = _warehouse(tmp_path / "warehouse.sqlite")
    now = _seed(path)
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    with sqlite3.connect(path) as db:
        closed = {**_active(7, now.isoformat()), "active": False, "state": "closed",
                  "stopped_at": now.isoformat(), "energy_wh": 800}
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='transaction'",
                   (json.dumps(closed),))
    assert "Ver carga actual" not in client.get("/?tx_view=active").text
    assert "Transacción aún no disponible" in client.get("/?tx_view=active").text
    with sqlite3.connect(path) as db:
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='transaction'",
                   (json.dumps(_active(7, now.isoformat(), connector_id=2)),))
    assert "Ver carga actual" not in client.get("/?tx_view=active").text
    assert "7.20 kW" not in client.get("/transactions/a/cp/7").text
    with sqlite3.connect(path) as db:
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='transaction'",
                   (json.dumps(_active(7, (now - timedelta(minutes=10)).isoformat())),))
        # Old but genuinely post-Start power is displayed with stale labeling.
        old = _connector(7, (now - timedelta(minutes=7)).isoformat())
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='connector'",
                   (json.dumps(old),))
    assert "Ver carga actual" in client.get("/?tx_view=active").text
    detail = client.get("/transactions/a/cp/7")
    assert "7.20 kW" in detail.text and "Medición antigua / no verificada" in detail.text
    with sqlite3.connect(path) as db:
        db.execute("UPDATE collector_latest SET payload=? WHERE source_id='a' AND kind='connector'",
                   (json.dumps(_connector(99, now.isoformat())),))
    assert "7.20 kW" not in client.get("/transactions/a/cp/7").text
    assert "Transacción aún no disponible" in client.get("/?tx_view=active").text


def test_mobile_navigation_labels_and_source_isolation(tmp_path):
    path = _warehouse(tmp_path / "warehouse.sqlite")
    now = _seed(path)
    with sqlite3.connect(path) as db:
        _add(db, "b", "connector", "cp:1", _connector(7, now.isoformat()))
        _add(db, "b", "transaction", "cp:7", _active(7, now.isoformat()))
    client = TestClient(create_app(path, allowed_source_ids=("a",)))
    page = client.get("/?connector_view=individual").text
    assert page.count('class="charger-session-link"') == 1
    assert page.count('class="status-session-link"') == 1
    assert "current-session-link" not in page
    assert "/transactions/b/cp/7" not in page
    assert "charger-session-link" in page and "status-session-link" in page
    assert '@media(max-width:520px)' in page
    assert "min-height:44px" in page
    assert "--nav:#203247" in client.get("/transactions/a/cp/7").text
    assert client.get("/transactions/b/cp/7").status_code == 404
