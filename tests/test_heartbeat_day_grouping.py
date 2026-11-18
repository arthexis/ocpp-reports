"""Regression: Heartbeats share a charger/local-day group despite legacy reply gaps."""
import json
import sqlite3

from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.warehouse.event_groups import event_group_details, event_groups_page


def test_heartbeat_day_group_includes_unpaired_and_legacy_events(tmp_path):
    db_path = tmp_path / "heartbeat-gaps.sqlite"
    with sqlite3.connect(db_path) as db:
        ensure_collector_schema(db)
        entries = (
            # One legacy pair may be inferred, but this is not a grouping requirement.
            (1, "Heartbeat", "2026-11-17T11:00:00.000Z", None),
            (2, "Heartbeat", "2026-11-17T11:00:00.020Z", None),
            # Two unpaired legacy records: 500 ms exceeds the inference limit.
            (3, "Heartbeat", "2026-11-17T16:51:00.000Z", None),
            (4, "Heartbeat", "2026-11-17T16:51:00.500Z", None),
            (5, "StatusNotification", "2026-11-17T18:00:00.000Z", "in"),
            (6, "StatusNotification", "2026-11-17T18:00:00.010Z", "out"),
            (7, "Heartbeat", "2026-11-17T20:00:00.000Z", "in"),
            (8, "Heartbeat", "2026-11-17T20:00:00.010Z", "out"),
            # Direction is explicit, but the outgoing response is unpaired.
            (9, "Heartbeat", "2026-11-17T21:59:00Z", "out"),
            (10, "Heartbeat", "2026-11-18T05:59:00Z", None),
            (11, "Heartbeat", "2026-11-18T06:01:00Z", None),
        )
        for event_id, action, at, direction in entries:
            payload = dict(id=event_id, charger_id="charger-a",
                           action=action,
                           kind="ocpp" if action == "Heartbeat" else "status",
                           at=at)
            if direction is not None:
                payload["direction"] = direction
            if action == "StatusNotification":
                payload.update(status="Available" if direction == "in" else None,
                               connector_id=1 if direction == "in" else None)
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("source-a", "event", str(event_id), event_id, json.dumps(payload)))
        # Same day and event ID cannot cross source or charger scope.
        for source, charger in (("source-b", "charger-a"), ("source-a", "charger-b")):
            payload = dict(id=99, charger_id=charger, action="Heartbeat",
                           kind="ocpp", at="2026-11-18T05:00:00Z")
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       (source, "event", charger, 99, json.dumps(payload)))

    scope = dict(source_id="source-a", charger_id="charger-a",
                 allowed_source_ids=("source-a",), zone="America/Monterrey")
    groups = event_groups_page(db_path, **scope)["groups"]
    heartbeats = [g for g in groups if g["action"] == "Heartbeat"]
    assert [(g["first_event_id"], g["event_count"]) for g in heartbeats] == [
        (11, 1), (10, 6)
    ]
    assert any(g["action"] == "StatusNotification" for g in groups)

    events = event_group_details(db_path, group_source="source-a",
                                first_event_id=10, **scope)["events"]
    assert [e["id"] for e in events] == [10, 9, 7, 4, 3, 1]
    assert next(e for e in events if e["id"] == 7)["response_id"] == 8
    inferred = next(e for e in events if e["id"] == 1)
    assert inferred["response_id"] == 2 and inferred["response_inferred"]
    for event_id in (10, 4, 3):
        assert "response_id" not in next(e for e in events if e["id"] == event_id)
    # Explicit but unmatched direction retains a null response ID.
    unmatched = next(e for e in events if e["id"] == 9)
    assert unmatched["direction"] == "out" and unmatched["response_id"] is None
    assert all(e["source_id"] == "source-a" for e in events)
    # A filtered/legacy SQL query remains a separate global contract.
    assert event_groups_page(db_path, source_id="source-b", charger_id="charger-a",
                             allowed_source_ids=("source-b",))["groups"][0]["event_count"] == 1
