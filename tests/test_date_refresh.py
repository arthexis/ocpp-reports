"""Historical burst repair preserves immutable evidence and source scoping."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ocpp_reports.changefeed import changes_page
from ocpp_reports.cli import app
from ocpp_reports.date_refresh import refresh
from ocpp_reports.follow import sync_page
from ocpp_reports.replication.collector import Batch, accept_batch
from ocpp_reports.warehouse.queries import snapshot
from ocpp_reports.web import create_app


FIXTURE = Path(__file__).parent / "fixtures" / "csms_export_v1" / "active.json"
START = "2026-10-08T18:59:18.928Z"
STOP = "2026-10-08T19:44:10.086Z"
RECEIVED_START = "2026-10-22T00:21:47.137Z"
RECEIVED_STOP = "2026-10-22T00:21:47.654311Z"


def _page(source="porsche-source"):
    page = json.loads(FIXTURE.read_text())
    data = page["data"]
    data["source_id"] = source
    data["cursor"] = {"after": 0, "next": 2, "more": False}
    start = data["events"][0]
    start.update({"transaction_id": 233, "at": RECEIVED_START})
    data["events"].append({**start, "id": 2, "action": "StopTransaction", "at": RECEIVED_STOP})
    tx = data["transactions"][0]
    tx.update({"id": 233, "state": "closed", "active": False,
               "started_at": RECEIVED_START, "stopped_at": RECEIVED_STOP,
               "start_event_at": START, "stop_event_at": STOP,
               "start_received_at": RECEIVED_START, "stop_received_at": RECEIVED_STOP,
               "energy_wh": 34062, "meter_stop_wh": 34162})
    tx["quality"] = {"active": False, "incomplete": False, "missing_meter": False}
    return page


def _tx(db, source):
    page = snapshot(db, allowed_source_ids=(source,), transaction_pagination={
        "term": "", "status": "", "location": "", "date_from": "", "date_to": "",
        "zone": "UTC", "view": "success", "page": 1})
    return page["transactions"][0]


def test_already_replicated_burst_dates_repair_with_new_sequence(tmp_path):
    local, remote = tmp_path / "local.sqlite", tmp_path / "collector.sqlite"
    page = _page()
    sync_page(local, page)
    other = _page("other-customer")
    sync_page(local, other)
    cursors = {}
    for source in ("porsche-source", "other-customer"):
        batch = changes_page(local, source)
        accept_batch(remote, Batch.model_validate(batch))
        cursors[source] = batch["next"]
    with sqlite3.connect(local) as db:
        original = db.execute("SELECT cursor FROM sources WHERE source_id='porsche-source'").fetchone()[0]
        observations = db.execute("SELECT payload FROM transaction_observations WHERE source_id='porsche-source'").fetchall()
        journal = db.execute("SELECT sequence,payload FROM replication_changes ORDER BY sequence").fetchall()
    assert _tx(remote, "porsche-source")["started_at"] == RECEIVED_START
    preview = refresh(local, source="porsche-source", cp="cp-a", transactions=[233])
    assert preview["status"] == "would_update"
    assert preview["transactions"][0]["proposed"] == {"started_at": START, "stopped_at": STOP}
    with pytest.raises(ValueError, match="backup"):
        refresh(local, source="porsche-source", cp="cp-a", transactions=[233], dry_run=False)
    backup = tmp_path / "before.sqlite"
    result = refresh(local, source="porsche-source", cp="cp-a", transactions=[233],
                     dry_run=False, backup=backup)
    assert result["status"] == "updated"
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(local) as db:
        assert db.execute("SELECT cursor FROM sources WHERE source_id='porsche-source'").fetchone()[0] == original
        assert db.execute("SELECT payload FROM transaction_observations WHERE source_id='porsche-source'").fetchall() == observations
        assert db.execute("SELECT sequence,payload FROM replication_changes ORDER BY sequence LIMIT ?", (len(journal),)).fetchall() == journal
    # A replayed old producer page cannot overwrite the operator repair.
    sync_page(local, page)
    repaired = _tx(local, "porsche-source")
    assert repaired["started_at"] == START and repaired["stopped_at"] == STOP
    assert repaired["energy_wh"] == 34062 and repaired["source_id"] == "porsche-source"
    assert (datetime.fromisoformat(STOP.replace("Z", "+00:00")) -
            datetime.fromisoformat(START.replace("Z", "+00:00"))).total_seconds() > 2600
    batch = changes_page(local, "porsche-source", after=cursors["porsche-source"])
    accept_batch(remote, Batch.model_validate(batch))
    remote_tx = _tx(remote, "porsche-source")
    assert (remote_tx["started_at"], remote_tx["stopped_at"], remote_tx["energy_wh"], remote_tx["source_id"]) == (
        START, STOP, 34062, "porsche-source")
    assert _tx(remote, "other-customer")["started_at"] == RECEIVED_START
    for database in (local, remote):
        client = TestClient(create_app(database, allowed_source_ids=("porsche-source",), timezones=("UTC",)))
        rendered = client.get("/?tx_view=success&date_from=2026-10-08&date_to=2026-10-08")
        assert rendered.status_code == 200
        assert "2026-10-08 18:59 UTC" in rendered.text
        assert "2026-10-08 19:44 UTC" in rendered.text
        assert "0:44:51" in rendered.text
        assert "34.06 kWh" in rendered.text
        assert client.get("/?source_id=other-customer").status_code == 403
    assert refresh(local, source="porsche-source", cp="cp-a", transactions=[233])["status"] == "unchanged"


def test_refuses_missing_event_time_or_other_source(tmp_path):
    local = tmp_path / "local.sqlite"
    sync_page(local, _page())
    with pytest.raises(ValueError, match="not enrolled"):
        refresh(local, source="wrong", cp="cp-a", transactions=[233])
    with sqlite3.connect(local) as db:
        tx = json.loads(db.execute("SELECT payload FROM current_transactions").fetchone()[0])
        tx["stop_event_at"] = None
        db.execute("UPDATE current_transactions SET payload=?", (json.dumps(tx),))
    with pytest.raises(ValueError, match="matching immutable observation"):
        refresh(local, source="porsche-source", cp="cp-a", transactions=[233])


def test_cli_defaults_to_preview_and_requires_backup_for_apply(tmp_path):
    local = tmp_path / "local.sqlite"
    sync_page(local, _page())
    args = ["metadata", "dates", "--database", str(local), "--source", "porsche-source",
            "--cp", "cp-a", "--transaction", "233"]
    preview = CliRunner().invoke(app, args)
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["status"] == "would_update"
    refused = CliRunner().invoke(app, [*args, "--apply"])
    assert refused.exit_code == 2
    assert "--backup" in refused.output
    applied = CliRunner().invoke(app, [*args, "--backup", str(tmp_path / "copy.sqlite"), "--apply"])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["status"] == "updated"
