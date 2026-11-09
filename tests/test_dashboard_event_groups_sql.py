"""Window-selected event groups are exact, read-only and source-isolated."""
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from ocpp_reports.warehouse.event_groups import event_groups_page, event_group_details
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema
from ocpp_reports.web import create_app


def _event(n, action="Heartbeat", *, cp="cp-1", source="a", state=None, fault=False):
    data = {"id": n, "kind": "ocpp" if action == "Heartbeat" else "status",
            "charger_id": cp, "action": action,
            "at": "2026-10-31T12:00:00Z"}
    if state is not None:
        data.update(status=state, connector_id=1, error_code="InternalError" if fault else None)
    return data


def _seed(path):
    with sqlite3.connect(path) as db:
        _schema(db)
        ensure_collector_schema(db)
        db.execute("INSERT INTO sources(source_id,cursor) VALUES ('a',0)")
        db.execute("INSERT INTO collector_cursors(source_id,sequence) VALUES ('b',0)")
        # Newest 130 Heartbeats, one fault, 60 matching status records,
        # then a StartTransaction and older heartbeat group.
        for n in range(1, 195):
            if n == 3:
                payload = _event(n, "StartTransaction")
            elif n == 64:
                payload = _event(n, "StatusNotification", state="Faulted", fault=True)
            elif 65 <= n <= 125:
                payload = _event(n, "StatusNotification", state="Available")
            else:
                payload = _event(n)
            payload["rfid"] = "PRIVATE-SECRET-DO-NOT-RENDER"
            db.execute("INSERT INTO events(source_id,event_id,payload) VALUES (?,?,?)",
                       ("a", n, json.dumps(payload)))
        # Independent remote source has its own event IDs, including collisions.
        for n in (1, 2, 3):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       ("b", "event", str(n), n,
                        json.dumps(_event(n, source="b", cp="cp-b"))))
        # Same source/event imported both locally and into collector must not duplicate.
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "event", "194", 194, json.dumps(_event(194))))


def test_window_groups_and_lazy_details_preserve_boundaries(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    _seed(db)
    result = event_groups_page(db, allowed_source_ids=("a", "b"))
    groups = result["groups"]
    assert groups[0]["source_id"] == "b"
    assert groups[0]["event_count"] == 3
    assert groups[1]["source_id"] == "a"
    assert groups[1]["first_event_id"] == 194
    assert groups[1]["event_count"] == 69
    assert groups[2]["action"] == "StatusNotification"
    assert groups[2]["event_count"] == 61
    assert groups[3]["event_count"] == 1
    assert groups[3]["action"] == "StatusNotification"
    detail = event_group_details(db, group_source="a", first_event_id=194,
                                 allowed_source_ids=("a", "b"))
    assert len(detail["events"]) == 50 and detail["has_next"]
    second = event_group_details(db, group_source="a", first_event_id=194,
                                 allowed_source_ids=("a", "b"), offset=50)
    assert len(second["events"]) == 19 and not second["has_next"]
    assert len({r["id"] for r in detail["events"] + second["events"]}) == 69
    assert event_groups_page(db, allowed_source_ids=("b",))["groups"][0]["event_count"] == 3


def test_source_security_and_filter_scope(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    _seed(db)
    app = create_app(db, allowed_source_ids=("a",), timezones=("UTC",))
    client = TestClient(app)
    html = client.get("/")
    assert html.status_code == 200
    assert "× 69" in html.text
    assert "event-groups;dur=" in html.headers["server-timing"]
    assert 'data-first-id="194"' in html.text
    assert "Abre el grupo" in html.text
    assert "Ver 50 más" in html.text
    assert client.get("/?source_id=b").status_code == 403
    assert client.get("/api/event-group?group_source=b&first_event_id=3").status_code == 403
    details = client.get("/api/event-group?group_source=a&first_event_id=194")
    assert details.status_code == 200
    assert len(details.json()["events"]) == 50
    assert details.json()["query_duration_ms"] >= 0
    assert "PRIVATE-SECRET-DO-NOT-RENDER" not in details.text
    assert client.get("/api/event-group?group_source=a&first_event_id=193").status_code == 404
    assert client.get("/api/event-group?group_source=a&first_event_id=194&offset=-1").status_code == 422
    assert client.get("/?event_page=0").status_code == 422
    # Filter applied before grouping, and date matches configured time zone.
    filtered = event_groups_page(db, allowed_source_ids=("a",), status="Faulted")
    assert filtered["groups"] == []  # status filter targets action, as existing dashboard
    assert len(event_groups_page(db, allowed_source_ids=("a",), term="faulted")["groups"]) == 1
    assert event_groups_page(db, allowed_source_ids=("a",), date_from="2026-11-01")["groups"] == []


def test_large_heartbeat_run_is_one_group_without_large_payload_response(tmp_path):
    db = tmp_path / "many.sqlite"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        for n in range(1, 10001):
            conn.execute("INSERT INTO events VALUES (?,?,?)",
                         ("a", n, json.dumps(_event(n))))
    first = event_groups_page(db, allowed_source_ids=("a",))
    assert len(first["groups"]) == 1
    assert first["groups"][0]["event_count"] == 10000
    assert not first["has_next"]
    assert len(event_group_details(db, group_source="a", first_event_id=10000,
                                    allowed_source_ids=("a",))["events"]) == 50


def test_global_chronological_order_with_sql_pagination(tmp_path):
    db = tmp_path / "chronological.sqlite"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        ensure_collector_schema(conn)
        # Source Z has *older* data despite sorting after A alphabetically.
        for n in range(1, 8):
            payload = _event(n, "BootNotification", cp="z")
            payload["at"] = f"2026-11-08T00:{n:02}:00Z"
            conn.execute("INSERT INTO events VALUES (?,?,?)",
                         ("z", n, json.dumps(payload)))
        for n in range(1, 8):
            payload = _event(n, "BootNotification", cp="a")
            payload["at"] = f"2026-11-09T00:{n:02}:00Z"
            conn.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                         ("a", "event", str(n), n, json.dumps(payload)))
        missing = _event(8, "BootNotification", cp="a")
        missing.pop("at")
        conn.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                     ("a", "event", "8", 8, json.dumps(missing)))
    page1 = event_groups_page(db)
    page2 = event_groups_page(db, page=2)
    assert len(page1["groups"]) == 10
    assert page1["has_next"]
    assert len(page2["groups"]) == 5
    assert not page2["has_next"]
    ordered = page1["groups"] + page2["groups"]
    assert [g["source_id"] for g in ordered] == ["a"] * 7 + ["z"] * 7 + ["a"]
    assert [g["first_event_id"] for g in ordered[:7]] == list(range(7, 0, -1))
    assert ordered[-1]["at"] is None
    assert event_groups_page(db, allowed_source_ids=("z",))["groups"][0]["source_id"] == "z"
    assert event_groups_page(db, date_from="2026-11-09")["groups"][0]["source_id"] == "a"


def test_global_grouping_only_collapses_adjacent_events_and_lazy_details(tmp_path):
    db = tmp_path / "interleaved.sqlite"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        for source, n, timestamp in (
            ("z", 1, "2026-11-09T10:00:00Z"),
            ("z", 2, "2026-11-09T10:03:00Z"),
            ("a", 1, "2026-11-09T10:02:00Z"),
            ("z", 3, "2026-11-09T10:01:00Z"),
        ):
            event = _event(n)
            event["at"] = timestamp
            conn.execute("INSERT INTO events VALUES (?,?,?)",
                         (source, n, json.dumps(event)))
    groups = event_groups_page(db)["groups"]
    assert [(g["source_id"], g["event_count"]) for g in groups] == [
        ("z", 1), ("a", 1), ("z", 2)]
    assert [g["first_event_id"] for g in groups] == [2, 1, 3]
    details = event_group_details(db, group_source="z", first_event_id=3)
    assert [item["id"] for item in details["events"]] == [3, 1]
    assert event_groups_page(db, page=2)["groups"] == []
