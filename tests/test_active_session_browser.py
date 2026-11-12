"""Real Chromium navigation against synthetic Reports TestClient responses only.

The browser network is fully intercepted. No live node, charger, DNS, or dashboard
can be contacted by this test. All screenshots contain synthetic fixture data.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest
from fastapi.testclient import TestClient

from ocpp_reports.web import create_app
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def _now(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


def _data():
    started = _now(-800)
    return (
        {"id": 7, "charger_id": "cp", "connector_id": 1, "state": "open",
         "active": True, "started_at": started, "stopped_at": None,
         "energy_wh": None, "live_energy_wh": 34750,
         "live_energy_observed_at": _now(-30),
         "soc_start": 25, "soc_end": 71},
        {"charger_id": "cp", "connector_id": 1, "status": "Charging",
         "transaction_id": 7, "connected": True,
         "power_w": 7200, "power_observed_at": _now(-30)},
    )


def _seed(db_path):
    tx, connector = _data()
    with sqlite3.connect(db_path) as db:
        ensure_collector_schema(db)
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "transaction", "cp:7", 1, json.dumps(tx)))
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "connector", "cp:1", 2, json.dumps(connector)))
        db.execute("INSERT INTO collector_receipts VALUES (?,?,?)",
                   ("a", _now(), 2))


def _replace(db_path, *, transaction=None, connector=None):
    with sqlite3.connect(db_path) as db:
        if transaction is not None:
            db.execute("UPDATE collector_latest SET payload=? "
                       "WHERE source_id='a' AND kind='transaction' AND record_key='cp:7'",
                       (json.dumps(transaction),))
        if connector is not None:
            db.execute("UPDATE collector_latest SET payload=? "
                       "WHERE source_id='a' AND kind='connector' AND record_key='cp:1'",
                       (json.dumps(connector),))


def _offline_route(client, counts):
    def handle(route):
        url = urlsplit(route.request.url)
        # Explicitly fail closed. No request may escape the synthetic origin.
        if (url.scheme != "http" or url.netloc != "reports.test"
                or route.request.method != "GET"):
            route.abort(error_code="blockedbyclient")
            raise AssertionError("External or non-read request: " + route.request.url)
        counts["GET"] += 1
        path = url.path + ("?" + url.query if url.query else "")
        result = client.get(path, follow_redirects=False)
        route.fulfill(status=result.status_code, body=result.content,
                      headers={"content-type": result.headers.get("content-type", "text/html; charset=utf-8"),
                               "cache-control": "no-store"})
    return handle


def _shot(page, name):
    folder = Path(os.environ.get("OCPP_BROWSER_ARTIFACT_DIR", "browser-evidence"))
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / name), full_page=True)


def test_offline_chromium_mobile_and_desktop(tmp_path):
    # This dedicated browser test runs from CI's isolated browser_offline job.
    # pytest's unit matrix does not install chromium and is unaffected.
    playwright = pytest.importorskip("playwright.sync_api")
    db_path = tmp_path / "synthetic-collector.sqlite"
    _seed(db_path)
    client = TestClient(create_app(db_path, allowed_source_ids=("a",)))
    assert client.get("/api/v1/transactions?source_id=b&view=active").status_code == 403
    assert client.get("/transactions/b/cp/7").status_code == 404

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for label, viewport, mobile in (
                ("mobile", {"width": 390, "height": 844}, True),
                ("desktop", {"width": 1920, "height": 1080}, False),
            ):
                context = browser.new_context(viewport=viewport, is_mobile=mobile,
                                              has_touch=mobile, locale="es-MX")
                page = context.new_page()
                counts = {"GET": 0}
                page.route("**/*", _offline_route(client, counts))
                # The explicit clock makes the 15s refresh deterministic and fast.
                page.clock.install()
                page.goto("http://reports.test/?tx_view=active", wait_until="load")
                assert page.get_by_role("button", name="En curso (1)").count() == 1
                assert page.get_by_role("link", name="Ver carga actual del conector 1").count() == 1
                target = page.locator("a.current-session-link").first
                bounds = target.bounding_box()
                assert bounds is not None and bounds["height"] >= 44, bounds
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                _shot(page, f"{label}-dashboard.png")
                if mobile:
                    # A real tap must work without zoom or hidden overlay.
                    target.scroll_into_view_if_needed()
                    bounds = target.bounding_box()
                    page.touchscreen.tap(bounds["x"] + bounds["width"] / 2,
                                         bounds["y"] + bounds["height"] / 2)
                else:
                    target.click()
                page.wait_for_url("**/transactions/a/cp/7?tz=UTC")
                assert page.locator("#transaction-energy").inner_text().find("34.75 kWh") >= 0
                assert "Estimada" in page.locator("#transaction-energy").inner_text()
                assert "7.20 kW" in page.locator("#transaction-power").inner_text()
                assert "Medición reciente" in page.locator("#transaction-power").inner_text()
                assert "71%" in page.locator("#transaction-soc").inner_text()
                assert "sin hora de medición verificada" in page.locator("#transaction-soc").inner_text()
                assert page.locator("main").get_attribute("data-active") == "true"
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                _shot(page, f"{label}-active.png")

                # Source is fresh, but the power sample becomes stale; refresh
                # must preserve the focused back link and scroll position.
                tx, stale_connector = _data()
                stale_connector["power_observed_at"] = _now(-500)
                _replace(db_path, connector=stale_connector)
                page.locator("header a").focus()
                page.evaluate("window.scrollTo(0, Math.min(150, document.body.scrollHeight))")
                before_scroll = page.evaluate("window.scrollY")
                before_requests = counts["GET"]
                page.clock.fast_forward(15_200)
                page.wait_for_function("document.querySelector('#transaction-power').textContent.includes('Medición antigua')")
                assert counts["GET"] > before_requests
                assert page.evaluate("document.activeElement.tagName") == "A"
                assert abs(page.evaluate("window.scrollY") - before_scroll) <= 3
                _shot(page, f"{label}-stale.png")

                # Natural Stop equivalent, but entirely synthetic: only the
                # temporary fixture is edited; no OCPP call is ever made.
                tx["state"] = "closed"
                tx["active"] = False
                tx["energy_wh"] = 35600
                tx["stopped_at"] = _now()
                _replace(db_path, transaction=tx)
                page.clock.fast_forward(15_200)
                page.wait_for_function("document.querySelector('main').dataset.active === 'false'")
                assert "35.60 kWh" in page.locator("#transaction-energy").inner_text()
                assert "Final" in page.locator("#transaction-energy").inner_text()
                assert "Sin potencia atribuible a esta sesión" in page.locator("#transaction-power").inner_text()
                assert page.locator("#transaction-status").inner_text().find("closed") >= 0
                _shot(page, f"{label}-closed.png")

                # Reset fixture before the second screen size.
                _replace(db_path, transaction=_data()[0], connector=_data()[1])
                context.close()

            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page = context.new_page()
            counts = {"GET": 0}
            page.route("**/*", _offline_route(client, counts))
            page.goto("http://reports.test/?source_id=a&status=Available&filter=closed&date_to=2000-01-01",
                      wait_until="load")
            page.locator(".operational-card--charging").click()
            page.wait_for_url("**/??*") if False else None
            url = urlsplit(page.url)
            query = parse_qs(url.query)
            assert query.get("tx_view") == ["active"], query
            assert query.get("source_id") == ["a"], query
            assert "filter" not in query and "status" not in query and "date_to" not in query
            assert page.get_by_role("button", name="En curso (1)").count() == 1
            _shot(page, "mobile-cleared-filters.png")
            context.close()
        finally:
            browser.close()
