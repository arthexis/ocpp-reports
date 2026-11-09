"""Real Chromium smoke for dashboard polling, focus, scroll and lazy event history."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def main():
    with tempfile.TemporaryDirectory(prefix="ocpp-browser-ci-") as directory:
        path = Path(directory) / "warehouse.sqlite"
        with sqlite3.connect(path) as db:
            _schema(db)
            ensure_collector_schema(db)
            db.execute("INSERT INTO sources(source_id,cursor) VALUES ('tenant-a',0)")
            db.execute("INSERT INTO sources(source_id,cursor) VALUES ('tenant-b',0)")
            for source in ("tenant-a", "tenant-b"):
                for n in range(1, 66):
                    payload = {"id": n, "kind": "ocpp", "charger_id": "cp-1",
                               "action": "Heartbeat", "at": "2026-11-01T06:30:00Z"}
                    db.execute("INSERT INTO events VALUES (?,?,?)",
                               (source, n, json.dumps(payload)))
                for n in range(1, 46):
                    tx = {"id": n, "charger_id": "cp-1", "state": "closed",
                          "active": False, "started_at": "2026-11-01T05:30:00Z",
                          "stopped_at": "2026-11-01T06:30:00Z", "energy_wh": 1000}
                    db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                               (source, "cp-1", n, 0, 0, "ci", json.dumps(tx),
                                "2026-11-01T06:30:00Z"))
        app_module = Path(directory) / "browser_app.py"
        app_module.write_text(
            "from ocpp_reports.web import create_app\n"
            f"app = create_app({str(path)!r}, allowed_source_ids=('tenant-a',), timezones=('UTC',))\n"
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, PYTHONPATH=str(directory) + os.pathsep +
                   str(Path.cwd() / "src"))
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "browser_app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1100, "height": 700})
                page.on("console", lambda message: print("browser:", message.text)
                        if message.type == "error" else None)
                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded",
                          timeout=30000)
                assert page.locator(".event-group").count() >= 1
                assert page.request.get(
                    f"http://127.0.0.1:{port}/?source_id=tenant-b").status == 403
                assert page.request.get(
                    f"http://127.0.0.1:{port}/api/event-group?group_source=tenant-b&first_event_id=65").status == 403
                group = page.locator(".event-group").first
                group.locator("summary").click()
                page.wait_for_function(
                    "() => document.querySelector('.event-group .event-detail tbody tr') !== null")
                assert group.locator("tbody tr").count() == 50
                group.get_by_role("button", name="Ver 50 más").click()
                page.wait_for_function(
                    "() => document.querySelectorAll('.event-group tbody tr').length === 65")
                assert group.locator("tbody tr").count() == 65
                page.evaluate("window.scrollTo(0, 400)")
                before = page.evaluate("window.scrollY")
                page.clock.install()
                page.clock.fast_forward(15000)
                page.wait_for_timeout(500)
                assert group.get_attribute("open") is not None
                assert group.locator("tbody tr").count() == 65
                assert page.evaluate("window.scrollY") == before
                page.locator(".unified-filter").focus()
                page.clock.fast_forward(15000)
                page.wait_for_timeout(500)
                assert page.evaluate("document.activeElement.classList.contains('unified-filter')")
                assert page.evaluate("window.scrollY") == before
                page.screenshot(path=os.environ.get("BROWSER_SCREENSHOT", "dashboard-browser.png"),
                                full_page=True)
                browser.close()
        finally:
            server.terminate()
            try:
                server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()


if __name__ == "__main__":
    main()
