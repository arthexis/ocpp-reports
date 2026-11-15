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
from types import SimpleNamespace
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
                                              has_touch=mobile, locale="es-MX", color_scheme="light")
                page = context.new_page()
                counts = {"GET": 0}
                page.route("**/*", _offline_route(client, counts))
                # The explicit clock makes the 15s refresh deterministic and fast.
                page.clock.install()
                page.goto("http://reports.test/?tx_view=active", wait_until="load")
                assert page.get_by_role("button", name="En curso (1)").count() == 1
                assert page.locator("a.current-session-link").count() == 0
                assert page.locator("a.charger-session-link").count() == 1
                assert page.locator("a.status-session-link").count() == 1
                assert "Ver carga actual" not in page.locator(".connectors-panel").inner_text()
                theme = page.evaluate("""() => {
                  const s = getComputedStyle(document.documentElement);
                  return Object.fromEntries(['--bg','--surface','--ink','--muted',
                    '--line','--nav','--accent','--warning'].map(key =>
                      [key, s.getPropertyValue(key).trim()]));
                }""")
                assert theme["--bg"] == "#f5f7fa" and theme["--nav"] == "#203247"
                if mobile:
                    badge = page.locator('.connectors-panel td[data-label="Estado"] .status').first
                    assert badge.evaluate("(node) => getComputedStyle(node).whiteSpace") == "nowrap"
                    assert badge.evaluate("(node) => node.scrollWidth <= node.clientWidth + 2")
                    assert badge.bounding_box()["width"] >= 65, "Charging badge too narrow"
                    for text in ("En curso (1)", "Exitosas (0)", "Fallidas (0)", "Todas (1)"):
                        button = page.get_by_role("button", name=text)
                        assert button.is_visible() and button.bounding_box()["width"] >= 55
                target = page.locator("a.charger-session-link" if mobile else "a.status-session-link").first
                bounds = target.bounding_box()
                assert bounds is not None and bounds["height"] >= 44, bounds
                if mobile:
                    assert bounds["width"] >= 44, "Charger name tap target is too narrow"
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
                page.wait_for_url("**/transactions/a/cp/7?tz=UTC*")
                assert page.locator("#transaction-energy").inner_text().find("34.75 kWh") >= 0
                assert page.locator(".site-header").is_visible()
                assert page.locator(".detail-heading").is_visible()
                assert page.locator(".back-link").bounding_box()["height"] >= 44
                detail_theme = page.evaluate("""() => {
                  const s = getComputedStyle(document.documentElement);
                  return Object.fromEntries(['--bg','--surface','--ink','--muted',
                    '--line','--nav','--accent','--warning'].map(key =>
                      [key, s.getPropertyValue(key).trim()]));
                }""")
                assert detail_theme == theme
                assert page.locator("header .brand h1").inner_text() == "OCPP Reports"
                # Both requested entry points must lead to the same scoped detail.
                page.go_back(wait_until="load")
                alternate = page.locator("a.status-session-link" if mobile else "a.charger-session-link").first
                assert alternate.is_visible()
                alternate.click()
                page.wait_for_url("**/transactions/a/cp/7?tz=UTC*")
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

            # Dark theme also shares dashboard tokens and status contrast.
            dark = browser.new_context(viewport={"width": 390, "height": 844},
                                       is_mobile=True, has_touch=True, color_scheme="dark")
            dark_page = dark.new_page()
            dark_page.route("**/*", _offline_route(client, {"GET": 0}))
            dark_page.goto("http://reports.test/?tx_view=active", wait_until="load")
            dark_bg = dark_page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
            assert dark_bg == "#111923"
            dark_page.locator("a.status-session-link").click()
            dark_page.wait_for_url("**/transactions/a/cp/7?tz=UTC*")
            assert dark_page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()") == dark_bg
            assert dark_page.locator(".site-header").is_visible()
            _shot(dark_page, "mobile-dark-detail.png")
            dark.close()

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



def test_offline_chromium_mobile_profile_and_connector_grid(tmp_path):
    """Physical-like viewport checks with fixture-only HTML and no outside network."""
    playwright = pytest.importorskip("playwright.sync_api")
    database = tmp_path / "mobile-card-fixture.sqlite"
    _seed(database)
    app = create_app(database, allowed_source_ids=("a",))

    @app.middleware("http")
    async def synthetic_profile(request, call_next):
        # Synthetic test principal only; no real authentication or credentials.
        request.state.principal = SimpleNamespace(
            user_id="fixture-user", role=SimpleNamespace(value="admin"))
        return await call_next(request)

    with TestClient(app) as client, playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for width in (320, 360, 390, 520):
                context = browser.new_context(
                    viewport={"width": width, "height": 844},
                    is_mobile=True, has_touch=True, locale="es-MX",
                )
                page = context.new_page()
                page.route("**/*", _offline_route(client, {"GET": 0}))
                page.goto("http://reports.test/", wait_until="load")
                search = page.locator("#dashboard-search").bounding_box()
                profile = page.locator(".profile-menu summary").bounding_box()
                assert search and profile
                assert profile["width"] >= 44 and profile["height"] >= 44
                assert abs(search["y"] - profile["y"]) <= 12, (width, search, profile)
                assert search["x"] + search["width"] <= profile["x"] + 2
                assert profile["x"] + profile["width"] <= width + 2
                card = page.locator(".connectors-panel tbody tr").first
                assert card.evaluate("(el) => getComputedStyle(el).display") == "grid"
                assert card.evaluate(
                    "(el) => getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length"
                ) == 2
                cells = card.locator("td")
                first, second = cells.nth(0).bounding_box(), cells.nth(1).bounding_box()
                assert first and second
                assert abs(first["y"] - second["y"]) <= 2
                assert second["x"] > first["x"]
                assert cells.nth(7).get_attribute("data-label") == "Recibido"
                charger_link = page.locator(".connectors-panel a.charger-session-link").first
                assert charger_link.bounding_box()["height"] >= 44
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                page.locator(".profile-menu summary").click()
                assert page.locator(".profile-popover .profile-name").inner_text() == "fixture-user"
                _shot(page, f"mobile-grid-profile-{width}.png")
                context.close()
        finally:
            browser.close()


def test_215_measured_timeline_browser_viewports_and_refresh(tmp_path):
    """Actual new SVG plots, temporal semantics and back URL; offline fixtures only."""
    playwright = pytest.importorskip("playwright.sync_api")
    from ocpp_reports.warehouse.schema import _schema
    db_path = tmp_path / "timeline-chromium.sqlite"
    _seed(db_path)
    active, _ = _data()
    active.update(meter_start_wh=1000)
    history = {"id": 6, "charger_id": "cp", "connector_id": 1,
               "state": "stopped", "active": False,
               "started_at": _now(-1800), "stopped_at": _now(-900),
               "meter_start_wh": 1000, "energy_wh": 3000}
    empty_history = {**history, "id": 8}
    one_point = {**history, "id": 9}
    with sqlite3.connect(db_path) as db:
        _schema(db)
        for tx, low, high in ((history, 0, 9), (active, 10, 20),
                              (one_point, 20, 30)):
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ("a", "cp", tx["id"], low, high, "digest",
                        json.dumps(tx), _now()))
            db.execute("INSERT INTO transaction_observations VALUES (?,?,?,?,?,?,?,?)",
                       ("a", "cp", tx["id"], low, high, "digest",
                        json.dumps(tx), _now()))
        db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                   ("a", "cp", 8, 0, 0, "digest",
                    json.dumps(empty_history), _now()))
        for event, tid, delta, energy, power, soc in (
            (2, 6, -1500, 1200, 3200, 31), (3, 6, -1200, 2300, 4100, 45),
            (11, 7, -500, 1400, 6400, 51), (12, 7, -400, 1900, 7000, 62),
            (25, 9, -1400, 1900, 3500, 41)
        ):
            sample = {"source_event_id": event, "sample_index": 0,
                      "charger_id": "cp", "connector_id": 1,
                      "transaction_id": tid, "at": _now(delta),
                      "energy_wh": energy, "power_w": power, "soc": soc,
                      "soc_provenance": "ocpp-meter-values"}
            db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                       ("a", event, 0, json.dumps(sample)))
    client = TestClient(create_app(db_path, allowed_source_ids=("a",)))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for width in (320, 360, 390, 520, 1920):
                for theme in ("light", "dark"):
                    context = browser.new_context(
                        viewport={"width": width, "height": 844},
                        is_mobile=width < 1000, has_touch=width < 1000,
                        locale="es-MX", color_scheme=theme)
                    page = context.new_page()
                    page.route("**/*", _offline_route(client, {"GET": 0}))
                    page.clock.install()
                    page.goto("http://reports.test/?tx_view=all&source_id=a&tx_page=1&tz=UTC",
                              wait_until="load")
                    assert page.get_by_role("button", name="Todas (4)").is_visible()
                    page.get_by_role("link", name="Ver detalles de transacción 6").click()
                    assert page.locator("main").get_attribute("data-active") == "false"
                    assert page.locator(".timeline-plot svg").count() == 3
                    assert page.locator(".timeline-evidence tbody tr").count() == 2
                    assert all(page.locator(".timeline-plot svg").nth(i).is_visible()
                               for i in range(3))
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    assert "tx_view=all" in page.locator(".back-link").get_attribute("href")
                    page.locator(".back-link").click()
                    assert page.url.endswith("tz=UTC#transactions")
                    page.get_by_role("link", name="Ver detalles de transacción 7").click()
                    assert page.locator("main").get_attribute("data-active") == "true"
                    assert page.locator(".timeline-evidence tbody tr").count() == 2
                    assert page.locator(".timeline-plot svg").count() == 3
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    # Required visual empty-state and single-point contracts.
                    page.goto("http://reports.test/transactions/a/cp/8?tz=UTC")
                    assert page.locator(".timeline-empty").is_visible()
                    assert page.locator(".timeline-plot svg").count() == 0
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    page.goto("http://reports.test/transactions/a/cp/9?tz=UTC")
                    assert page.locator(".timeline-plot svg").count() == 3
                    assert page.locator(".timeline-evidence tbody tr").count() == 1
                    assert page.locator(".timeline-segment").count() == 0
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    page.goto("http://reports.test/transactions/a/cp/7?tz=UTC")
                    if width == 390 and theme == "dark":
                        _shot(page, "215-timeline-dark-390.png")
                    if width == 1920 and theme == "light":
                        _shot(page, "215-timeline-desktop-1920.png")
                    if width == 390 and theme == "light":
                        # Fixture-only read: add one real indexed observation
                        # and prove the 15-second browser read updates the SVG.
                        sample = {"source_event_id": 13, "sample_index": 0,
                                  "charger_id": "cp", "connector_id": 1,
                                  "transaction_id": 7, "at": _now(-250),
                                  "energy_wh": 2500, "power_w": 6400,
                                  "soc": 67, "soc_provenance": "ocpp-meter-values"}
                        with sqlite3.connect(db_path) as db:
                            db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                                       ("a", 13, 0, json.dumps(sample)))
                        page.locator(".back-link").focus()
                        page.evaluate("window.scrollTo(0, 170)")
                        before = page.evaluate("window.scrollY")
                        page.clock.fast_forward(15_200)
                        page.wait_for_function(
                            "document.querySelectorAll('.timeline-evidence tbody tr').length === 3")
                        assert page.evaluate("document.activeElement.classList.contains('back-link')")
                        assert abs(page.evaluate("window.scrollY") - before) <= 3
                        _shot(page, "215-timeline-refresh-390.png")
                    context.close()
        finally:
            browser.close()
