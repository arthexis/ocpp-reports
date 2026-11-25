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
                assert page.locator("a.charger-session-link").count() == 2  # charger and connector detail links
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
                target = page.locator("a.status-session-link").first
                bounds = target.bounding_box()
                assert bounds is not None and bounds["height"] >= 44, bounds
                if mobile:
                    assert bounds["width"] >= 44, "Active-session status tap target is too narrow"
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
                page.wait_for_url(lambda url: urlsplit(url).path == "/transactions/a/cp/7" and urlsplit(url).query.startswith("tz=UTC"))
                assert page.locator("#transaction-energy").inner_text().find("34.75 kWh") >= 0
                assert page.locator(".site-header").is_visible()
                assert page.locator(".detail-heading").is_visible()
                cards = page.locator(".overview > section, .overview > .detail-heading")
                assert cards.count() == 3
                boxes = [cards.nth(i).bounding_box() for i in range(3)]
                assert all(box is not None for box in boxes)
                if mobile:
                    assert boxes[0]["y"] < boxes[1]["y"]
                    assert abs(boxes[1]["y"] - boxes[2]["y"]) < 3
                    assert boxes[1]["x"] < boxes[2]["x"]
                else:
                    assert max(box["y"] for box in boxes) - min(box["y"] for box in boxes) < 3
                    assert boxes[0]["x"] < boxes[1]["x"] < boxes[2]["x"]
                # Session metadata uses two columns on phones/tablets, three on wide screens.
                fields = page.locator("#transaction-session-data dl > div")
                assert fields.count() == 6
                field_boxes = [fields.nth(i).bounding_box() for i in range(4)]
                assert all(field_boxes)
                assert abs(field_boxes[0]["y"] - field_boxes[1]["y"]) < 3
                if mobile:
                    assert field_boxes[2]["y"] > field_boxes[1]["y"]
                else:
                    assert abs(field_boxes[1]["y"] - field_boxes[2]["y"]) < 3
                    assert field_boxes[3]["y"] > field_boxes[2]["y"]
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
                assert page.locator("#transaction-power").count() == 0
                assert page.locator("#transaction-timeline h2").count() == 0
                assert page.locator(".back-link").bounding_box()["height"] >= 44
                detail_theme = page.evaluate("""() => {
                  const s = getComputedStyle(document.documentElement);
                  return Object.fromEntries(['--bg','--surface','--ink','--muted',
                    '--line','--nav','--accent','--warning'].map(key =>
                      [key, s.getPropertyValue(key).trim()]));
                }""")
                assert detail_theme == theme
                assert page.locator("header .brand h1").inner_text() == "OCPP Reports"
                # Charger/connector navigation now opens scoped device pages,
                # while the status badge remains a direct session link.
                page.go_back(wait_until="load")
                charger_link = page.locator("a.charger-session-link").first
                assert charger_link.get_attribute("href").startswith("/chargers/a/cp")
                charger_link.click()
                page.wait_for_url(lambda url: urlsplit(url).path == "/chargers/a/cp")
                assert page.locator("#device-title").inner_text().startswith("Cargador cp")
                page.go_back(wait_until="load")
                connector_link = page.locator('.connectors-panel td[data-label="Conector"] a').first
                assert connector_link.get_attribute("href").startswith("/connectors/a/cp/1")
                connector_link.click()
                page.wait_for_url(lambda url: urlsplit(url).path == "/connectors/a/cp/1")
                assert "Conector 1" in page.locator("#device-title").inner_text()
                page.go_back(wait_until="load")
                page.locator("a.status-session-link").first.click()
                page.wait_for_url(lambda url: urlsplit(url).path == "/transactions/a/cp/7" and urlsplit(url).query.startswith("tz=UTC"))
                assert page.locator("#transaction-energy").inner_text().find("34.75 kWh") >= 0
                assert "Estimada" in page.locator("#transaction-energy").inner_text()
                assert page.locator("#transaction-power").count() == 0
                assert page.locator(".overview > section, .overview > .detail-heading").count() == 3
                assert "71%" in page.locator("#transaction-soc").inner_text()
                assert "Final / última lectura" in page.locator("#transaction-soc").inner_text()
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
                page.wait_for_timeout(500)
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
                assert page.locator("#transaction-power").count() == 0
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
            dark_page.wait_for_url(lambda url: urlsplit(url).path == "/transactions/a/cp/7" and urlsplit(url).query.startswith("tz=UTC"))
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
    _, nominal_connector = _data()
    nominal_connector.update(power_w=None, power_observed_at=None)
    _replace(database, connector=nominal_connector)
    # Collector stores nominal ratings independently from connector status.
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "rating", "cp:1", 3, json.dumps({
                       "charger_id": "cp", "connector_id": 1,
                       "rated_power_w": 60000, "rating_scope": "connector"})))
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
                short = card.locator('td[data-label="Conector"]')
                wide = card.locator('td[data-label="Ubicación"]')
                first, second = short.bounding_box(), wide.bounding_box()
                assert first and second
                assert abs(first["y"] - second["y"]) <= 2
                assert first["x"] > second["x"], (width, first, second)
                assert second["width"] >= first["width"] * 1.8, (width, first, second)
                # Left now holds the former right-hand fields; each group's row order is unchanged.
                left = ("Ubicación", "Cargador", "Estado", "Medidor")
                right = ("Conector", "Potencia", "Conectado", "Recibido")
                for row, (left_name, right_name) in enumerate(zip(left, right)):
                    left_box = card.locator(f'td[data-label="{left_name}"]').bounding_box()
                    right_box = card.locator(f'td[data-label="{right_name}"]').bounding_box()
                    assert left_box and right_box, (width, left_name, right_name)
                    assert abs(left_box["y"] - right_box["y"]) < 3, (width, left_name, right_name)
                    assert left_box["x"] < right_box["x"], (width, left_name, right_name)
                    if row:
                        prev = card.locator(f'td[data-label="{left[row-1]}"]').bounding_box()
                        assert prev and left_box["y"] > prev["y"], (width, left_name)
                assert card.evaluate("(el) => parseFloat(getComputedStyle(el).columnGap)") <= 5
                label_size = short.evaluate(
                    "(el) => parseFloat(getComputedStyle(el, '::before').fontSize)")
                value_size = short.evaluate(
                    "(el) => parseFloat(getComputedStyle(el).fontSize)")
                assert value_size >= label_size + 3.5, (width, label_size, value_size)
                assert short.evaluate(
                    "(el) => parseFloat(getComputedStyle(el).gap)") <= 3
                assert cells.nth(7).get_attribute("data-label") == "Cargas"
                assert cells.nth(7).is_hidden()
                assert cells.nth(8).get_attribute("data-label") == "Última carga"
                assert cells.nth(8).is_hidden()
                assert cells.nth(9).get_attribute("data-label") == "Recibido"
                power = card.locator('td[data-label="Potencia"] .connector-power')
                assert power.count() == 1
                assert " ".join(power.inner_text().split()) == "60.00 kW nominal"
                assert power.evaluate("(el) => getComputedStyle(el).whiteSpace") == "normal"
                assert power.evaluate(
                    "(el) => el.getBoundingClientRect().right "
                    "<= el.parentElement.getBoundingClientRect().right + 2"
                )
                charger_link = page.locator(".connectors-panel a.charger-session-link").first
                assert charger_link.bounding_box()["height"] < 44  # no extra blank space
                for identity in ("Cargador", "Conector"):
                    link = card.locator(f'td[data-label="{identity}"] a.charger-session-link')
                    assert link.count() == 1
                    alignment = link.evaluate("""(el) => {
                      const bounds = el.getBoundingClientRect();
                      const text = document.createRange();
                      text.selectNodeContents(el);
                      const touch = getComputedStyle(el, "::after");
                      return {topGap: text.getBoundingClientRect().top - bounds.top,
                              textHeight: bounds.height, touchHeight: parseFloat(touch.height),
                              touchBottom: touch.bottom,
                              minHeight: getComputedStyle(el).minHeight};
                    }""")
                    assert alignment["topGap"] <= 4, (width, identity, alignment)
                    assert alignment["minHeight"] == "0px", (width, identity, alignment)
                    assert alignment["touchHeight"] >= 44, (width, identity, alignment)
                    assert alignment["textHeight"] <= 43, (width, identity, alignment)
                    assert alignment["touchBottom"] == "0px"
                assert card.locator('td[data-label="Conector"]').evaluate(
                    "(el) => parseFloat(getComputedStyle(el).fontSize)") >= 17
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
                    assert page.locator(".timeline-plot svg").count() == 1
                    assert page.locator(".timeline-evidence tbody tr").count() == 2
                    assert page.locator(".timeline-plot svg").first.is_visible()
                    assert page.locator(".timeline-plot circle title").all_text_contents() == [
                        "0.200 kWh · 31.0%",
                        "1.300 kWh · 45.0%",
                    ]
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    assert "tx_view=all" in page.locator(".back-link").get_attribute("href")
                    page.locator(".back-link").click()
                    assert page.url.endswith("tz=UTC#transactions")
                    page.get_by_role("link", name="Ver detalles de transacción 7").click()
                    assert page.locator("main").get_attribute("data-active") == "true"
                    assert page.locator(".timeline-evidence tbody tr").count() in (2, 3)
                    assert page.locator(".timeline-plot svg").count() == 1
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    # Required visual empty-state and single-point contracts.
                    page.goto("http://reports.test/transactions/a/cp/8?tz=UTC")
                    assert page.locator(".timeline-empty").is_visible()
                    assert page.locator(".timeline-plot svg").count() == 0
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                    page.goto("http://reports.test/transactions/a/cp/9?tz=UTC")
                    assert page.locator(".timeline-plot svg").count() == 1
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
                        assert any("1.500 kWh · 67.0%" in title
                                   for title in page.locator(".timeline-plot circle title").all_text_contents())
                        assert page.evaluate("document.activeElement.classList.contains('back-link')")
                        assert abs(page.evaluate("window.scrollY") - before) <= 3
                        _shot(page, "215-timeline-refresh-390.png")
                    context.close()
        finally:
            browser.close()


def test_215_decimation_never_draws_false_bridge_chromium(tmp_path):
    """Offline SVG: 135 observed samples must not become a 119-stroke curve."""
    playwright = pytest.importorskip("playwright.sync_api")
    from ocpp_reports.warehouse.schema import _schema
    db_path = tmp_path / "decimation-browser.sqlite"
    _seed(db_path)
    transaction = {"id": 50, "charger_id": "cp", "connector_id": 1,
                   "state": "stopped", "active": False,
                   "started_at": _now(-900), "stopped_at": _now(-200),
                   "meter_start_wh": 1000, "energy_wh": 3700}
    with sqlite3.connect(db_path) as db:
        _schema(db)
        db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                   ("a", "cp", 50, 0, 140, "digest",
                    json.dumps(transaction), _now()))
        db.execute("INSERT INTO transaction_observations VALUES (?,?,?,?,?,?,?,?)",
                   ("a", "cp", 50, 0, 140, "digest",
                    json.dumps(transaction), _now()))
        for event in range(1, 136):
            sample = {"source_event_id": event, "sample_index": 0,
                      "charger_id": "cp", "connector_id": 1,
                      "transaction_id": 50, "at": _now(-800 + event * 2),
                      "energy_wh": 1000 + event * 20, "power_w": 6000}
            db.execute("INSERT INTO energy_samples VALUES (?,?,?,?)",
                       ("a", event, 0, json.dumps(sample)))
    client = TestClient(create_app(db_path, allowed_source_ids=("a",)))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844},
                                    is_mobile=True, has_touch=True)
            page.route("**/*", _offline_route(client, {"GET": 0}))
            page.goto("http://reports.test/transactions/a/cp/50?tz=UTC",
                      wait_until="load")
            assert page.locator(".timeline-evidence tbody tr").count() == 120
            assert page.locator(".timeline-plot svg").count() == 1
            # The sole energy plot omits artificial long jumps from decimation.
            for plot in page.locator(".timeline-plot").all():
                dots = plot.locator("circle").count()
                strokes = plot.locator(".timeline-segment").count()
                assert dots == 120
                assert 0 < strokes < dots - 1
            assert page.get_by_text("Vista parcial", exact=False).is_visible()
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 2")
            _shot(page, "215-decimation-no-false-bridges-390.png")
        finally:
            browser.close()


def test_offline_chromium_tablet_header_and_compact_tables(tmp_path):
    """The intermediate breakpoint hides cells only visually, without losing links."""
    playwright = pytest.importorskip("playwright.sync_api")
    database = tmp_path / "tablet-dashboard-fixture.sqlite"
    _seed(database)
    app = create_app(database, allowed_source_ids=("a",))

    @app.middleware("http")
    async def synthetic_profile(request, call_next):
        request.state.principal = SimpleNamespace(
            user_id="fixture-user", role=SimpleNamespace(value="admin"))
        return await call_next(request)

    with TestClient(app) as client, playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for width in (390, 521, 700, 759, 760, 900, 1100, 1200):
                mobile = width <= 520
                context = browser.new_context(
                    viewport={"width": width, "height": 844}, is_mobile=mobile,
                    has_touch=mobile, locale="es-MX", color_scheme="dark")
                page = context.new_page()
                page.route("**/*", _offline_route(client, {"GET": 0}))
                page.goto("http://reports.test/", wait_until="load")
                assert page.locator(".transactions-panel h2").inner_text() == "Sesiones de carga"
                assert page.locator("#transaction-history-help").count() == 0
                assert page.locator(".connectors-panel th").all_inner_texts() == [
                    "Ubicación", "Cargador", "Conector", "Estado",
                    "Conectado", "Potencia", "Energía", "Cargas", "Última carga", "Recibido"]
                if 521 <= width <= 1100:
                    search = page.locator("#dashboard-search").bounding_box()
                    profile = page.locator(".profile-menu summary").bounding_box()
                    assert search and profile
                    assert search["width"] >= width * .70, (width, search)
                    assert 0 <= profile["x"] - (search["x"] + search["width"]) <= 24
                    assert abs(search["y"] + search["height"]
                               - profile["y"] - profile["height"]) <= 12
                    assert profile["width"] >= 44 and profile["height"] >= 44
                    hidden_connectors = (2, 7, 8, 9, 10) if width >= 760 else (2, 6, 7, 8, 9, 10)
                    for index in hidden_connectors:
                        cell = page.locator(".connectors-panel thead th").nth(index - 1)
                        assert not cell.is_visible(), (width, index)
                    shown_connectors = (1, 3, 4, 5, 6) if width >= 760 else (1, 3, 4, 5)
                    for index in shown_connectors:
                        assert page.locator(".connectors-panel thead th").nth(index - 1).is_visible()
                    for index in (2, 6, 8, 9):
                        assert not page.locator(".transactions-panel thead th").nth(index - 1).is_visible()
                        assert not page.locator(".transactions-panel tbody td").nth(index - 1).is_visible()
                    for index in (1, 3, 4, 5, 7, 10):
                        assert page.locator(".transactions-panel thead th").nth(index - 1).is_visible()
                    assert page.locator(".connectors-panel a.status-session-link").count() == 1
                    assert page.locator(".connectors-panel .scroll").evaluate(
                        "(el) => el.scrollWidth <= el.clientWidth + 2"), width
                elif width == 1200:
                    assert page.locator(".connectors-panel thead th").nth(1).is_visible()
                    assert page.locator(".connectors-panel thead th").nth(5).is_visible()
                    assert page.locator(".connectors-panel thead th").nth(7).is_visible()
                    assert page.locator(".connectors-panel thead th").nth(8).is_visible()
                    for column in (6, 7):
                        assert page.locator(".connectors-panel thead th").nth(column - 1).evaluate(
                            "(cell) => getComputedStyle(cell).textAlign") == "right"
                    assert page.locator(".transactions-panel thead th").nth(4).is_visible()
                else:
                    assert page.locator(".connectors-panel td[data-label='Cargador']").first.is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), width
                if width == 900:
                    _shot(page, "tablet-compact-dashboard-900.png")
                    page.goto("http://reports.test/transactions/a/cp/7?tz=UTC", wait_until="load")
                    assert page.locator(".detail-disclaimer").count() == 0
                    assert "Cobertura: intervalo de eventos observado" not in page.locator(
                        "#transaction-timeline").inner_text()
                    _shot(page, "tablet-transaction-detail-900.png")
                context.close()
        finally:
            browser.close()



def test_charger_energy_toggle_and_mobile_session_columns_offline(tmp_path):
    """Real Chromium: one 7/30 chart, instant selection and no side-scroll."""
    playwright = pytest.importorskip("playwright.sync_api")
    database = tmp_path / "charger-energy-mobile.sqlite"
    _seed(database)
    with sqlite3.connect(database) as db:
        for tid, age, wh, connector in (
                (800, 2, 2000, 1), (801, 10, 3000, 1),
                (802, 1, 1000, 2), (803, 1, 9000, 0),
            ):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("a", "transaction", f"cp:{tid}", tid,
                        json.dumps({"id": tid, "charger_id": "cp",
                                    "connector_id": connector, "active": False,
                                    "state": "closed", "energy_wh": wh,
                                    "started_at": _now(-age * 86400 - 3600),
                                    "stopped_at": _now(-age * 86400)})))
    client = TestClient(create_app(database, allowed_source_ids=("a",)))
    assert client.get("/chargers/b/cp").status_code == 404
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for width in (320, 360, 390, 520, 1280):
                mobile = width <= 520
                context = browser.new_context(
                    viewport={"width": width, "height": 844}, is_mobile=mobile,
                    has_touch=mobile, locale="es-MX")
                page = context.new_page()
                page.route("**/*", _offline_route(client, {"GET": 0}))
                page.goto("http://reports.test/chargers/a/cp?tz=UTC", wait_until="load")
                chart = page.locator("#energy-chart")
                assert chart.count() == 1 and chart.get_attribute("data-days") == "7"
                heading = page.locator(".energy-heading h2").bounding_box()
                first_filter = page.get_by_role("button", name="7 días").bounding_box()
                last_filter = page.get_by_role("button", name="Conectores", exact=True).bounding_box()
                assert heading and first_filter and last_filter
                assert abs(heading["y"] - first_filter["y"]) <= 14, (width, heading, first_filter)
                assert abs(heading["y"] - last_filter["y"]) <= 14, (width, heading, last_filter)
                assert last_filter["x"] + last_filter["width"] <= width + 1, width
                energy_buttons = [
                    page.get_by_role("button", name=name, exact=True)
                    for name in ("7 días", "30 días", "Estación", "Conectores")
                ]
                energy_button_boxes = [button.bounding_box() for button in energy_buttons]
                for button in energy_buttons:
                    spacing = button.evaluate("(el) => {const css=getComputedStyle(el);"
                                              "return {left:parseFloat(css.paddingLeft),right:parseFloat(css.paddingRight),"
                                              "top:parseFloat(css.paddingTop),bottom:parseFloat(css.paddingBottom)}}")
                    assert spacing["left"] == spacing["right"] == 0, (width, spacing)
                    assert spacing["top"] > 0 and spacing["bottom"] > 0, (width, spacing)
                assert all(energy_button_boxes), (width, energy_button_boxes)
                assert max(b["width"] for b in energy_button_boxes) - min(
                    b["width"] for b in energy_button_boxes) <= 1.5, (width, energy_button_boxes)
                if 390 <= width <= 520:
                    # The group should not stretch over all remaining row space:
                    # narrower buttons, not merely narrower gaps.
                    assert max(b["width"] for b in energy_button_boxes) <= 66, (
                        width, energy_button_boxes)
                if mobile:
                    compact_label = page.locator(".energy-label-compact")
                    full_label = page.locator(".energy-label-full")
                    if width <= 375:
                        assert compact_label.is_visible() and not full_label.is_visible()
                        assert compact_label.inner_text() == "Conect."
                    else:
                        assert full_label.is_visible() and not compact_label.is_visible()
                    for button in energy_buttons:
                        dims = button.evaluate(
                            "(el) => ({scroll: el.scrollWidth, client: el.clientWidth})")
                        assert dims["scroll"] <= dims["client"] + 1, (
                            width, button.inner_text(), dims)
                if mobile:
                    for left, right in zip(energy_button_boxes, energy_button_boxes[1:]):
                        gap = right["x"] - (left["x"] + left["width"])
                        assert 0 <= gap <= 1.2, (width, gap, energy_button_boxes)
                for name in ("7 días", "30 días"):
                    assert name in page.get_by_role("button", name=name).inner_text(), width
                if mobile:
                    value_size = page.locator(".connector dd").first.evaluate(
                        "(el) => parseFloat(getComputedStyle(el).fontSize)")
                    label_size = page.locator(".connector dt").first.evaluate(
                        "(el) => parseFloat(getComputedStyle(el).fontSize)")
                    assert value_size >= label_size + 2.5, (width, value_size, label_size)
                assert chart.locator(".energy-day:visible").count() == 7
                assert page.locator("#energy-total").inner_text() == "3.00 kWh"
                assert page.locator("#energy-sessions").inner_text().strip() == "· 2 sesiones cerradas"
                assert page.get_by_role("button", name="7 días").get_attribute("aria-pressed") == "true"
                assert page.get_by_role("button", name="Estación", exact=True).get_attribute("aria-pressed") == "true"
                assert page.get_by_role("button", name="Conectores", exact=True).click() is None
                assert chart.get_attribute("data-view") == "connectors"
                assert page.locator("#energy-legend").is_visible()
                assert "C1" in page.locator("#energy-legend").inner_text()
                assert "C2" in page.locator("#energy-legend").inner_text()
                assert "C0" not in page.locator("#energy-legend").inner_text()
                assert chart.locator(".energy-day:visible .energy-connector-bar").count() == 14
                assert chart.locator(".energy-connector-bar:visible").count() == 2
                assert page.locator("#energy-total").inner_text() == "3.00 kWh"
                assert page.locator(".energy-day:visible").first.get_attribute("aria-label").find("C1:") >= 0
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), width
                assert chart.evaluate("(el) => el.scrollWidth <= el.clientWidth + 2"), width
                start_cells = page.locator(".sessions-table .session-start")
                assert start_cells.count() >= 2
                assert start_cells.first.is_visible() != mobile
                assert page.locator(".sessions-table thead th").filter(has_text="Fin").is_visible()
                assert page.locator(".sessions-table .session-duration").first.is_visible() != mobile
                assert page.get_by_role("button", name="30 días").click() is None
                assert page.locator("#energy-chart").get_attribute("data-days") == "30"
                assert page.locator("#energy-chart .energy-day:visible").count() == 30
                assert chart.locator(".energy-day:visible .energy-connector-bar").count() == 60
                assert chart.locator(".energy-connector-bar:visible").count() == 3
                assert page.locator("#energy-total").inner_text() == "6.00 kWh"
                assert page.locator("#energy-sessions").inner_text().strip() == "· 3 sesiones cerradas"
                assert page.get_by_role("button", name="30 días").get_attribute("aria-pressed") == "true"
                assert page.get_by_role("button", name="7 días").get_attribute("aria-pressed") == "false"
                assert page.get_by_role("button", name="Estación", exact=True).click() is None
                assert chart.get_attribute("data-view") == "total"
                assert chart.locator(".energy-bar").count() == 30
                assert chart.locator(".energy-bar:visible").count() == 3
                assert chart.locator(".energy-connector-bar:visible").count() == 0
                assert page.locator("#energy-legend").is_hidden()
                assert page.locator("#energy-total").inner_text() == "6.00 kWh"
                assert page.get_by_role("button", name="Conectores", exact=True).click() is None
                assert chart.locator(".energy-day:visible .energy-connector-bar").count() == 60
                assert page.get_by_role("button", name="Información sobre energía").click() is None
                assert page.locator("#energy-help").is_visible()
                assert "C0" in page.locator("#energy-help").inner_text()
                assert page.get_by_role("button", name="Información sobre energía").get_attribute("aria-expanded") == "true"
                assert page.get_by_role("button", name="Información sobre energía").click() is None
                assert page.locator("#energy-help").is_hidden()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), width
                assert chart.evaluate("(el) => el.scrollWidth <= el.clientWidth + 2"), width
                if mobile:
                    assert chart.bounding_box()["width"] <= width - 10
                    _shot(page, f"charger-energy-30d-{width}.png")
                page.get_by_role("button", name="7 días").click()
                assert chart.locator(".energy-day:visible").count() == 7
                assert page.locator("#energy-total").inner_text() == "3.00 kWh"
                assert page.url.endswith("/chargers/a/cp?tz=UTC")
                page.goto("http://reports.test/?tx_view=all&source_id=a&tz=UTC", wait_until="load")
                assert page.locator(".transactions-panel tbody tr").count() > 0
                hidden = (1, 6, 8, 9)
                shown = (2, 3, 4, 5, 7, 10)
                for index in hidden:
                    assert page.locator(f".transactions-panel thead th:nth-child({index})").is_visible() != mobile
                for index in shown:
                    assert page.locator(f".transactions-panel thead th:nth-child({index})").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), width
                context.close()
        finally:
            browser.close()


def test_charger_event_action_expands_in_same_table_row_offline(tmp_path):
    """Closed groups use one rendered row; action text opens full-width lazy details."""
    from test_device_details import seed
    playwright = pytest.importorskip("playwright.sync_api")
    path = tmp_path / "events-compact.sqlite"
    seed(path)
    client = TestClient(create_app(path, allowed_source_ids=("a",), timezones=("UTC",)))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for width in (320, 360, 390, 520, 1280):
                mobile = width <= 520
                context = browser.new_context(
                    viewport={"width": width, "height": 844}, is_mobile=mobile,
                    has_touch=mobile, locale="es-MX")
                page = context.new_page()
                requests = {"GET": 0}
                page.route("**/*", _offline_route(client, requests))
                page.goto("http://reports.test/chargers/a/same?tz=UTC", wait_until="load")
                assert page.locator(".events-table .event-summary-row").count() == 1
                assert page.locator(".events-table .event-expand-row:visible").count() == 0
                assert page.locator(".events-table .event-group summary").inner_text() == "BootNotification"
                summary = page.locator(".events-table .event-group summary").first
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), width
                if mobile:
                    assert page.locator(".events-table .event-id").first.is_hidden()
                    assert page.locator(".events-table .event-count").first.is_visible()
                    metrics = page.locator(".events-table").evaluate(
                        "(el) => ({scroll:el.scrollWidth,client:el.clientWidth,"
                        "columns:[...el.querySelectorAll('thead th')].map(c=>({name:c.textContent,w:c.getBoundingClientRect().width,scroll:c.scrollWidth}))})")
                    assert metrics["scroll"] <= metrics["client"] + 2, (width, metrics)
                else:
                    assert page.locator(".events-table .event-id").first.is_visible()
                before = requests["GET"]
                summary.click()
                page.locator(".events-table .event-expand-row").first.wait_for(state="visible")
                page.locator(".event-detail tbody tr").first.wait_for()
                assert page.locator(".event-detail tbody tr").count() == 1
                assert page.locator(".event-detail thead th").all_inner_texts() == [
                    "ID", "ID respuesta", "Fecha", "Dirección", "Información"
                ]
                assert page.locator(".event-detail tbody td").count() == 5
                assert "Inicio del cargador registrado" in page.locator(".event-detail").inner_text()
                assert "never-expose" not in page.locator(".event-detail").inner_text()
                assert requests["GET"] > before
                summary.click()
                page.locator(".events-table .event-expand-row").first.wait_for(state="hidden")
                assert page.locator(".events-table .event-expand-row:visible").count() == 0
                assert page.locator(".events-table .event-summary-row").count() == 1
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), width
                context.close()
        finally:
            browser.close()

def test_offline_chromium_search_profile_alignment_and_caption(tmp_path):
    """Actual browser geometry in authenticated, offline-only synthetic dashboard."""
    import hashlib
    import time
    from ocpp_reports.dashboard_login import attach_login, provision, COOKIE

    playwright = pytest.importorskip("playwright.sync_api")
    warehouse = tmp_path / "synthetic-header.sqlite"
    _seed(warehouse)
    db = tmp_path / "header-auth.sqlite"
    provision(db, "synthetic-operator", "offline-only-strong-password", "operator", "synthetic")
    app = create_app(warehouse, allowed_source_ids=("a",))
    attach_login(app, path=db, tenant="synthetic")
    token = "synthetic-browser-test-token"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO dashboard_sessions(token_hash,user_id,expires) VALUES (?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), "synthetic-operator", int(time.time())+300))
    client = TestClient(app)
    client.cookies.set(COOKIE, token)
    assert client.get("/").status_code == 200

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for width in (390, 620, 850, 1280):
                context = browser.new_context(viewport={"width":width,"height":850}, locale="es-MX")
                page = context.new_page()
                page.route("**/*", _offline_route(client, {"GET":0}))
                page.goto("http://reports.test/", wait_until="load")
                field = page.locator("#dashboard-search")
                profile = page.locator(".profile-menu > summary")
                caption = page.locator(".filter-caption")
                assert field.count() == 1 and profile.count() == 1
                bounds = field.bounding_box()
                other = profile.bounding_box()
                assert bounds is not None and other is not None
                assert abs(bounds["y"] - other["y"]) <= 1, (width,bounds,other)
                assert abs(bounds["height"] - 44) <= 1, (width,bounds)
                assert abs(other["height"] - 44) <= 1, (width,other)
                assert page.locator('label[for="dashboard-search"]').count() == 1
                if width <= 700:
                    assert not caption.is_visible(), width
                else:
                    assert caption.is_visible(), width
                    assert caption.bounding_box()["y"] >= bounds["y"]+bounds["height"], width
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
                context.close()
        finally:
            browser.close()


def test_accounts_shared_header_layout():
    """Render synthetic accounts page and verify the common header at both widths."""
    from importlib.resources import files
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    playwright = pytest.importorskip("playwright.sync_api")
    env = Environment(loader=FileSystemLoader(str(files("ocpp_reports").joinpath("templates"))),
                      autoescape=select_autoescape(default=True))
    html = env.get_template("dashboard_accounts.html.j2").render(
        title="Reports", tenant="administration", actor="admin-test", accounts=[],
        selected_account=None, csrf_token="synthetic", notice="", error="")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (1280, 390, 320):
            page = browser.new_page(viewport={"width": width, "height": 800})
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            back = page.locator(".site-header .back-link")
            heading = page.locator(".site-header h1")
            assert back.get_attribute("href") == "/"
            assert heading.inner_text() == "Administrar cuentas"
            b, h = back.bounding_box(), heading.bounding_box()
            assert h["x"] + h["width"] <= b["x"]
            assert b["y"] < h["y"] + h["height"] and h["y"] < b["y"] + b["height"]
            assert b["height"] >= 44
            assert page.locator(".site-header").evaluate("(e) => e.scrollWidth <= e.clientWidth")
            page.close()
        browser.close()
