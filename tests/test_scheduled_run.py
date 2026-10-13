import json
from datetime import datetime, timezone
import pytest
from typer.testing import CliRunner
from ocpp_reports.cli import app
from ocpp_reports.outbox import Outbox
from ocpp_reports.scheduled_run import prepare_scheduled_report

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def fixture(tmp_path, *, complete=True, start="2026-10-12T00:00:00+00:00"):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({
        "schema": "ocpp-reports/summary/v1",
        "source_id": "csms-a",
        "period": {"since": start, "until": "2026-10-13T00:00:00+00:00", "timezone": "UTC"},
        "summary": {"transactions": 1, "known_energy_wh": 1250, "missing_energy_transactions": 0,
                    "completeness": "complete" if complete else "partial"},
        "transactions": [{"id": 1, "charger_id": "CP1", "connector_id": 1, "state": "completed", "energy_wh": 1250}],
        "revision": "stable-r1",
    }), encoding="utf-8")
    return path


def test_queues_once_for_same_revision(tmp_path):
    source = fixture(tmp_path)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    args = dict(source=source, outbox=outbox, operator="operator@example.org", cadence="daily", zone="UTC", now=NOW)
    key = prepare_scheduled_report(**args)
    assert prepare_scheduled_report(**args) == key
    assert len(outbox.list()) == 1


def test_rejects_provisional_and_wrong_period(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    args = dict(outbox=outbox, operator="operator@example.org", cadence="daily", zone="UTC", now=NOW)
    with pytest.raises(ValueError, match="incomplete"):
        prepare_scheduled_report(source=fixture(tmp_path, complete=False), **args)
    with pytest.raises(ValueError, match="period"):
        prepare_scheduled_report(source=fixture(tmp_path, start="2026-10-11T00:00:00+00:00"), **args)
    assert outbox.list() == []


def test_cli_queues_without_sending(monkeypatch, tmp_path):
    # CLI uses current time, so use the real previous UTC date.
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    source = fixture(tmp_path, start=f"{yesterday.isoformat()}T00:00:00+00:00")
    document = json.loads(source.read_text())
    document["period"]["until"] = f"{today.isoformat()}T00:00:00+00:00"
    source.write_text(json.dumps(document))
    monkeypatch.setenv("OCPP_REPORTS_OPERATOR_EMAIL", "operator@example.org")
    monkeypatch.setenv("OCPP_REPORTS_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    result = CliRunner().invoke(app, ["run", "--source", str(source), "--output", str(tmp_path / "report.txt")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "report.txt").exists()\n    assert Outbox(tmp_path / "outbox.sqlite3").list() == []

def test_explicit_period_and_stdout(tmp_path):
    source = fixture(tmp_path)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    from datetime import datetime
    rendered = prepare_scheduled_report(source=source, outbox=outbox, operator="operator@localhost",
        since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"),
        until=datetime.fromisoformat("2026-10-13T00:00:00+00:00"), queue=False)
    assert "OCPP charging report" in rendered
    assert outbox.list() == []
