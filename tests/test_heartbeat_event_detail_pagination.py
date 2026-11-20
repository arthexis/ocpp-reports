"""Regression coverage for large charger Heartbeat event groups."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ocpp_reports.warehouse.event_groups import event_group_details, event_groups_page


def _fixture(tmp_path, *, count=1305):
    database = tmp_path / "reports.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE events (source_id TEXT NOT NULL, event_id INTEGER NOT NULL, payload TEXT NOT NULL)")
        start = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
        for index in range(count):
            at = start + timedelta(seconds=index)
            event_id = index * 2 + 1
            for number, direction, moment in (
                (event_id, "in", at),
                (event_id + 1, "out", at + timedelta(milliseconds=20)),
            ):
                db.execute(
                    "INSERT INTO events VALUES (?,?,?)",
                    ("source-a", number, json.dumps({
                        "id": number, "charger_id": "charger-1",
                        "at": moment.isoformat(), "action": "Heartbeat",
                        "kind": "ocpp", "direction": direction,
                        "secret": "must-never-be-returned",
                    })),
                )
        # Neither a different charger nor an identically named charger on
        # another source may enter the selected group.
        for source, charger in (("source-b", "charger-1"), ("source-a", "charger-2")):
            db.execute("INSERT INTO events VALUES (?,?,?)", (source, 900001, json.dumps({
                "id": 900001, "charger_id": charger, "at": (start + timedelta(hours=1)).isoformat(),
                "action": "Heartbeat", "kind": "ocpp", "direction": "in",
            })))
        db.execute("INSERT INTO events VALUES (?,?,?)", ("source-a", 900002, json.dumps({
            "id": 900002, "charger_id": "charger-1", "at": start.isoformat(),
            "action": "StatusNotification", "kind": "status", "status": "Available",
            "connector_id": 1,
        })))
    return database, count * 2 - 1


def test_large_heartbeat_group_offsets_and_scope(tmp_path):
    database, anchor = _fixture(tmp_path)
    values = []
    for offset in range(0, 1305, 50):
        result = event_group_details(
            database, group_source="source-a", first_event_id=anchor,
            source_id="source-a", charger_id="charger-1",
            allowed_source_ids=("source-a",), zone="UTC", offset=offset,
        )
        assert len(result["events"]) == min(50, 1305 - offset)
        assert result["has_next"] is (offset + 50 < 1305)
        for event in result["events"]:
            assert event["source_id"] == "source-a"
            assert event["charger_id"] == "charger-1"
            assert event["action"] == "Heartbeat"
            assert event["response_id"] == event["id"] + 1
            assert "secret" not in event
            assert "payload" not in event
            assert "location" in event
            values.append(event["id"])
    assert values == list(range(anchor, 0, -2))
    empty = event_group_details(
        database, group_source="source-a", first_event_id=anchor,
        source_id="source-a", charger_id="charger-1",
        allowed_source_ids=("source-a",), zone="UTC", offset=1305,
    )
    assert empty == {"events": [], "has_next": False}


def test_anchor_validation_and_source_allowlist(tmp_path):
    database, anchor = _fixture(tmp_path, count=60)
    kwargs = dict(group_source="source-a", source_id="source-a",
                  charger_id="charger-1", zone="UTC")
    with pytest.raises(LookupError):
        event_group_details(database, first_event_id=anchor-2, **kwargs)
    with pytest.raises(PermissionError):
        event_group_details(database, first_event_id=anchor,
                            allowed_source_ids=("source-b",), **kwargs)


def test_utc_midnight_response_association(tmp_path):
    database = tmp_path / "boundary.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE events (source_id TEXT, event_id INTEGER, payload TEXT)")
        for id_, direction, at in (
            (11, "in", "2026-11-19T23:59:59.990+00:00"),
            (12, "out", "2026-11-20T00:00:00.010+00:00"),
        ):
            db.execute("INSERT INTO events VALUES (?,?,?)", (
                "source-a", id_, json.dumps({
                    "id": id_, "charger_id": "charger-1",
                    "action": "Heartbeat", "kind": "ocpp",
                    "direction": direction, "at": at,
                }),
            ))
    result = event_group_details(
        database, group_source="source-a", first_event_id=11,
        source_id="source-a", charger_id="charger-1", zone="UTC",
    )
    assert len(result["events"]) == 1
    assert result["events"][0]["response_id"] == 12
