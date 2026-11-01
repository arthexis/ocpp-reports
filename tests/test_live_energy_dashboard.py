import json
import sqlite3
from datetime import datetime, timedelta, timezone

from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.web import minute_utc, display_date


def test_forwarded_live_energy_and_finalization(tmp_path):
    dbpath = tmp_path / "collector.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE collector_latest (source_id TEXT, kind TEXT, record_key TEXT, payload TEXT)")
        db.execute("CREATE TABLE collector_cursors (source_id TEXT, sequence INTEGER)")
        db.execute("INSERT INTO collector_cursors VALUES ('gway-005', 1)")
        item = {
            "id": 8, "charger_id": "test", "state": "open", "active": True,
            "energy_wh": None, "live_energy_wh": 34750,
            "live_energy_observed_at": datetime.now(timezone.utc).isoformat(),
        }
        db.execute("INSERT INTO collector_latest VALUES ('gway-005','transaction','8',?)", (json.dumps(item),))
    data = snapshot(dbpath)
    assert data["transactions"][0]["live_energy_stale"] is False
    environment = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "src/ocpp_reports/templates")))
    environment.filters["minute_utc"] = minute_utc
    environment.filters["display_date"] = display_date
    template = environment.get_template("dashboard.html.j2")
    html = template.render(data=data, source_id="", charger_id="", timezone="UTC", timezones=("UTC",), event_groups=[], transaction_counts={"success": 0, "failed": 0, "all": 1},
                           tx_page=1, tx_pages=1, tx_total=1, event_page=1, event_has_next=False)
    assert "34.75" in html and "en vivo" in html
    with sqlite3.connect(dbpath) as db:
        item["live_energy_observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        db.execute("UPDATE collector_latest SET payload=?", (json.dumps(item),))
    assert snapshot(dbpath)["transactions"][0]["live_energy_stale"] is True
    with sqlite3.connect(dbpath) as db:
        item.update(state="stopped", active=False, energy_wh=35000)
        db.execute("UPDATE collector_latest SET payload=?", (json.dumps(item),))
    finalized = snapshot(dbpath)
    html = template.render(data=finalized, source_id="", charger_id="", timezone="UTC", timezones=("UTC",), event_groups=[], transaction_counts={"success": 0, "failed": 0, "all": 1},
                           tx_page=1, tx_pages=1, tx_total=1)
    assert "35.00" in html and "(final)" in html
