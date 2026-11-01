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
    assert 'data-first-id="194"' in html.text
    assert "Abre el grupo" in html.text
    assert "Ver 50 más" in html.text
    assert client.get("/?source_id=b").status_code == 403
    assert client.get("/api/event-group?group_source=b&first_event_id=3").status_code == 403
    details = client.get("/api/event-group?group_source=a&first_event_id=194")
    assert details.status_code == 200
    assert len(details.json()["events"]) == 50
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
