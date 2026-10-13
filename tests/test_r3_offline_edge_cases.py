"""R3D2 scheduled-report edge cases and offline-only execution."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone

import pytest

from ocpp_reports.outbox import Outbox
from ocpp_reports.scheduled_run import prepare_scheduled_report

START = "2026-10-12T00:00:00+00:00"
END = "2026-10-13T00:00:00+00:00"


@pytest.fixture
def offline_report(tmp_path, monkeypatch):
    """One deterministic fixture with no external connections or shared database."""
    def block_network(*args, **kwargs):
        raise AssertionError("R3 integration tests must never open a network socket")
    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)
    source = tmp_path / "summary.json"
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    document = {
        "schema": "ocpp-reports/summary/v1",
        "source_id": "offline-fixture",
        "period": {"since": START, "until": END, "timezone": "UTC"},
        "summary": {"transactions": 0, "known_energy_wh": 0,
                    "missing_energy_transactions": 0, "completeness": "complete"},
        "transactions": [], "revision": "fixed-r1",
    }
    def write():
        source.write_text(json.dumps(document), encoding="utf-8")
    write()
    def run(**kwargs):
        return prepare_scheduled_report(
            source=source, outbox=outbox, operator="operator@example.org",
            cadence="daily", zone="UTC",
            now=datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
            **kwargs)
    return document, write, run, outbox


def test_empty_completed_report_can_be_queued_once(offline_report):
    _, _, run, outbox = offline_report
    key = run()
    assert run() == f"EXISTS:{key}"
    rows = outbox.list()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["attempts"] == 0
    assert "0" in rows[0]["body"]


@pytest.mark.parametrize("change", [
    lambda d: d["summary"].update(completeness="partial"),
    lambda d: d["summary"].update(missing_energy_transactions=1),
    lambda d: d["period"].update(since="2026-10-11T00:00:00+00:00"),
    lambda d: d["period"].update(until="2026-10-14T00:00:00+00:00"),
    lambda d: d["period"].update(since="2026-10-12T00:00:01+00:00"),
    lambda d: d["period"].update(until="2026-10-12T23:59:59+00:00"),
])
def test_incomplete_or_mismatched_period_never_queues(offline_report, change):
    document, write, run, outbox = offline_report
    change(document)
    write()
    with pytest.raises(ValueError):
        run()
    assert outbox.list() == []


def test_equivalent_offset_boundaries_are_accepted(offline_report):
    document, write, run, outbox = offline_report
    document["period"]["since"] = "2026-10-11T20:00:00-04:00"
    document["period"]["until"] = "2026-10-12T20:00:00-04:00"
    write()
    assert run()
    assert len(outbox.list()) == 1


def test_explicit_period_rejects_naive_and_reversed_bounds(offline_report):
    _, _, run, outbox = offline_report
    for since, until in [
        (datetime(2026, 10, 12), datetime(2026, 10, 13, tzinfo=timezone.utc)),
        (datetime(2026, 10, 13, tzinfo=timezone.utc),
         datetime(2026, 10, 12, tzinfo=timezone.utc)),
    ]:
        with pytest.raises(ValueError):
            run(since=since, until=until)
    assert outbox.list() == []
