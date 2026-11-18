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
    # Exact scoped streaming path must preserve SQL window semantics.
    for page_number in (1, 2, 3):
        options = dict(source_id="a", charger_id="cp-1",
                       allowed_source_ids=("a", "b"), page=page_number)
        fast = event_groups_page(db, **options)
        legacy = event_groups_page(db, term="cp-1", **options)
        assert fast == legacy
    precise = dict(group_source="a", first_event_id=194,
                   source_id="a", charger_id="cp-1",
                   allowed_source_ids=("a", "b"))
    for at in (0, 50, 80):
        assert event_group_details(db, offset=at, **precise) == event_group_details(
            db, offset=at, term="cp-1", **precise)
    # Non-leading identifiers must not open a different group.
    try:
        event_group_details(db, first_event_id=193, **{k: v for k, v in precise.items()
                                                         if k != "first_event_id"})
    except LookupError:
        pass
    else:
        raise AssertionError("Non-leading event ID unexpectedly accepted")
    assert event_groups_page(db, allowed_source_ids=("b",))["groups"][0]["event_count"] == 3


def test_source_security_and_filter_scope(tmp_path):
    db = tmp_path / "warehouse.sqlite"
    _seed(db)
    app = create_app(db, allowed_source_ids=("a",), timezones=("UTC",))
    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert "Eventos recientes" not in home.text
    assert "event-groups;dur=" not in home.headers["server-timing"]
    html = client.get("/chargers/a/cp-1")
    assert html.status_code == 200
    assert 'class="events-table"' in html.text
    assert '<td class="event-count">69</td>' in html.text
    assert 'data-first-id="194"' in html.text
    assert "Ver eventos de" in html.text
    assert "Ver eventos del grupo" not in html.text
    assert 'class="event-expand-row" hidden' in html.text
    assert 'class="event-action"><details class="event-group"' in html.text
    assert '<summary aria-label="Ver eventos de ' in html.text
    assert "Ver 50 más" in html.text
    assert client.get("/chargers/b/cp-b").status_code == 404
    assert client.get("/chargers/a/nonexistent").status_code == 404
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
    fast = event_groups_page(db, source_id="a", charger_id="cp-1",
                             allowed_source_ids=("a",))
    assert fast == event_groups_page(
        db, source_id="a", charger_id="cp-1",
        allowed_source_ids=("a",), term="cp-1")
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



def test_status_echo_pairs_preserve_exact_state_and_fault_boundaries(tmp_path):
    """Real-world status-less echo follows a full state in milliseconds."""
    db = tmp_path / "status-echo.sqlite"
    with sqlite3.connect(db) as connection:
        _schema(connection)
        ensure_collector_schema(connection)
        entries = [
            (90, "Available", 1, None, 0),
            (91, None, None, None, 20),
            (92, "Available", 1, None, 100),
            (93, None, None, None, 120),
            (94, "Available", 2, None, 1000),
            (95, None, None, None, 1020),
            (96, "Faulted", 2, "ReaderFailure", 2000),
            (97, None, None, None, 2020),
            (98, "Faulted", 2, "ReaderFailure", 2100),
            (99, None, None, None, 2120),
            (100, "Charging", 1, None, 3000),
            (101, None, None, None, 3020),
            (102, None, None, None, 10000),
        ]
        from datetime import datetime, timedelta, timezone
        for event_id, status, connector, error, milliseconds in entries:
            at = (datetime(2026, 11, 16, tzinfo=timezone.utc)
                  + timedelta(milliseconds=milliseconds)).isoformat()
            payload = {
                "id": event_id, "charger_id": "charger-1", "action": "StatusNotification",
                "kind": "status", "status": status, "connector_id": connector,
                "error_code": error, "info": None, "at": at,
                "rfid": "PRIVATE-MUST-NOT-RENDER",
            }
            connection.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                               ("node-a", "event", str(event_id), event_id, json.dumps(payload)))

    page = event_groups_page(
        db, source_id="node-a", charger_id="charger-1",
        allowed_source_ids=("node-a",))
    assert [(r["first_event_id"], r["event_count"], r["information"])
            for r in page["groups"]] == [
        (102, 1, "Sin estado en este registro"),
        (101, 2, "C1 · Charging"),
        (99, 2, "C2 · Faulted · ReaderFailure"),
        (97, 2, "C2 · Faulted · ReaderFailure"),
        (95, 2, "C2 · Available"),
        (93, 4, "C1 · Available"),
    ]
    fault = event_group_details(
        db, group_source="node-a", first_event_id=99,
        source_id="node-a", charger_id="charger-1", allowed_source_ids=("node-a",))
    assert [x["id"] for x in fault["events"]] == [99, 98]
    assert [x["information"] for x in fault["events"]] == [
        "Sin estado en este registro", "C2 · Faulted · ReaderFailure"
    ]
    assert "PRIVATE-MUST-NOT-RENDER" not in json.dumps(fault)
    assert page == event_groups_page(
        db, source_id="node-a", charger_id="charger-1",
        allowed_source_ids=("node-a",), term="charger-1")
    assert fault == event_group_details(
        db, group_source="node-a", first_event_id=99,
        source_id="node-a", charger_id="charger-1",
        allowed_source_ids=("node-a",), term="charger-1")
    assert event_groups_page(db, source_id="node-b", charger_id="charger-1",
                             allowed_source_ids=("node-b",))["groups"] == []
    assert event_groups_page(db, source_id="node-a", charger_id="other",
                             allowed_source_ids=("node-a",))["groups"] == []
    app = create_app(db, allowed_source_ids=("node-a",), timezones=("UTC",))
    client = TestClient(app)
    response = client.get("/api/v1/charger-event-group", params={
        "source_id": "node-a", "charger_id": "charger-1", "first_event_id": 99,
    })
    assert response.status_code == 200
    assert [v["information"] for v in response.json()["events"]] == [
        "Sin estado en este registro", "C2 · Faulted · ReaderFailure"
    ]
    assert "PRIVATE-MUST-NOT-RENDER" not in response.text


def test_status_echo_never_crosses_time_source_action_or_event_identity(tmp_path):
    """A missing status without the exact next observation remains independent."""
    db = tmp_path / "unpaired-status.sqlite"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        entries = [
            ("a", 1, "2026-11-16T00:00:00Z", "Available", 1, "StatusNotification"),
            ("a", 2, "2026-11-16T00:00:04Z", None, None, "StatusNotification"),
            ("a", 3, "2026-11-16T00:00:05Z", None, None, "StatusNotification"),
            ("a", 4, "2026-11-16T00:00:06Z", "Available", 1, "Heartbeat"),
            ("b", 5, "2026-11-16T00:00:07Z", "Available", 1, "StatusNotification"),
            ("a", 6, "2026-11-16T00:00:08Z", None, None, "StatusNotification"),
        ]
        for source, event_id, at, status, connector, action in entries:
            payload = {"id": event_id, "charger_id": "charger-1", "action": action,
                       "kind": "status", "status": status, "connector_id": connector,
                       "at": at, "error_code": None, "info": None}
            conn.execute("INSERT INTO events VALUES (?,?,?)",
                         (source, event_id, json.dumps(payload)))
    groups = event_groups_page(db)
    assert len(groups["groups"]) == 6
    assert all(g["event_count"] == 1 for g in groups["groups"])


def test_scoped_event_pagination_exactly_eleven_groups(tmp_path):
    db = tmp_path / "exact-page.sqlite"
    with sqlite3.connect(db) as connection:
        _schema(connection)
        for event_id in range(1, 12):
            payload = _event(event_id, "StartTransaction")
            payload["at"] = f"2026-11-16T00:00:{event_id:02d}Z"
            connection.execute("INSERT INTO events VALUES (?,?,?)",
                               ("a", event_id, json.dumps(payload)))
    opts = dict(source_id="a", charger_id="cp-1", allowed_source_ids=("a",))
    first = event_groups_page(db, page=1, **opts)
    last = event_groups_page(db, page=2, **opts)
    assert len(first["groups"]) == 10 and first["has_next"]
    assert len(last["groups"]) == 1 and not last["has_next"]
    assert event_groups_page(db, page=3, **opts)["groups"] == []
    assert first == event_groups_page(db, page=1, term="cp-1", **opts)
    assert last == event_groups_page(db, page=2, term="cp-1", **opts)


def test_scoped_collector_only_database_keeps_event_id_and_html(tmp_path):
    """No local events table: the remote projection still aliases event_id."""
    db = tmp_path / "collector-only.sqlite"
    with sqlite3.connect(db) as connection:
        ensure_collector_schema(connection)
        connection.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                           ("a", "connector", "cp-1:1", 1,
                            json.dumps({"charger_id": "cp-1", "connector_id": 1,
                                        "status": "Available"})))
        connection.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                           ("a", "event", "1", 2,
                            json.dumps(_event(1, cp="cp-1"))))
    groups = event_groups_page(db, source_id="a", charger_id="cp-1",
                               allowed_source_ids=("a",))
    assert len(groups["groups"]) == 1
    assert groups["groups"][0]["first_event_id"] == 1
    response = TestClient(create_app(db, allowed_source_ids=("a",))).get(
        "/chargers/a/cp-1")
    assert response.status_code == 200 and 'id="energy-chart"' in response.text


def test_directed_response_ids_are_compact_and_status_episodes_span_heartbeats(tmp_path):
    """Only the request is displayed; replies remain traceable by storage ID."""
    from datetime import datetime, timedelta, timezone
    db = tmp_path / "directed.sqlite"
    with sqlite3.connect(db) as connection:
        ensure_collector_schema(connection)
        events = [
            ("StatusNotification", "in", "Available", 1),
            ("StatusNotification", "out", None, None),
            ("Heartbeat", "in", None, None),
            ("Heartbeat", "out", None, None),
            ("StatusNotification", "in", "Available", 1),
            ("StatusNotification", "out", None, None),
            ("StatusNotification", "in", "Charging", 2),
            ("StatusNotification", "out", None, None),
            ("Heartbeat", "in", None, None),
            ("Heartbeat", "out", None, None),
            ("StatusNotification", "in", "Available", 1),
            ("StatusNotification", "out", None, None),
            ("StatusNotification", "in", "Charging", 1),
            ("StatusNotification", "out", None, None),
            ("StatusNotification", "in", "Available", 1),
            ("StatusNotification", "out", None, None),
            ("Reset", "out", None, None),  # server-initiated, no response yet
        ]
        start = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        for event_id, (action, direction, status, connector) in enumerate(events, 1):
            # Pair replies within one second; requests may be minutes apart.
            seconds = (event_id - 1) // 2 * 180 + (event_id - 1) % 2 * 0.01
            payload = dict(id=event_id, charger_id="charger-a",
                           action=action, kind="status" if action == "StatusNotification" else "ocpp",
                           direction=direction, status=status, connector_id=connector,
                           at=(start + timedelta(seconds=seconds)).isoformat())
            connection.execute(
                "INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                ("a", "event", str(event_id), event_id, json.dumps(payload)))
        # Foreign source reuses IDs and must not affect any group or response.
        connection.execute(
            "INSERT INTO collector_latest VALUES (?,?,?,?,?)",
            ("b", "event", "16", 16, json.dumps({
                "id": 16, "charger_id": "charger-a", "kind": "ocpp",
                "direction": "in", "action": "BootNotification",
                "at": start.isoformat()})))
    opts = dict(source_id="a", charger_id="charger-a", allowed_source_ids=("a",))
    result = event_groups_page(db, **opts)
    groups = result["groups"]
    assert [g["first_event_id"] for g in groups] == [17, 15, 13, 11, 9, 7, 5, 1]
    assert [g["event_count"] for g in groups] == [1, 1, 1, 1, 2, 1, 1, 1]
    assert all(g["source_id"] == "a" for g in groups)
    available = event_group_details(
        db, group_source="a", first_event_id=11, **opts)
    assert [e["id"] for e in available["events"]] == [11]
    assert [e["response_id"] for e in available["events"]] == [12]
    assert all(e["direction"] == "in" for e in available["events"])
    heartbeat = event_group_details(
        db, group_source="a", first_event_id=9, **opts)
    assert [e["id"] for e in heartbeat["events"]] == [9, 3]
    assert [e["response_id"] for e in heartbeat["events"]] == [10, 4]
    try:
        event_group_details(db, group_source="a", first_event_id=12, **opts)
    except LookupError:
        pass
    else:
        raise AssertionError("Response ID must not select a group")
    assert "private" not in json.dumps(available)
    assert event_groups_page(db, source_id="b", charger_id="charger-a",
                             allowed_source_ids=("a",))["groups"] == []


def test_legacy_events_with_no_direction_are_not_silently_hidden(tmp_path):
    db = tmp_path / "legacy-no-direction.sqlite"
    with sqlite3.connect(db) as connection:
        ensure_collector_schema(connection)
        for event_id, status in ((1, "Available"), (2, None)):
            payload = {"id": event_id, "charger_id": "cp",
                       "kind": "status", "action": "StatusNotification",
                       "at": "2026-11-16T12:00:00Z",
                       "status": status,
                       "connector_id": 1 if status else None}
            connection.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                               ("a", "event", str(event_id), event_id, json.dumps(payload)))
    group = event_groups_page(db, source_id="a", charger_id="cp",
                              allowed_source_ids=("a",))["groups"][0]
    assert group["event_count"] == 2
    details = event_group_details(db, group_source="a",
                                  first_event_id=2, source_id="a",
                                  charger_id="cp", allowed_source_ids=("a",))
    assert len(details["events"]) == 2
    assert all("response_id" not in e for e in details["events"])


def test_realistic_legacy_status_and_heartbeat_reply_pairs_are_marked_inferred(tmp_path):
    from datetime import datetime, timedelta, timezone
    db = tmp_path / "legacy-paired.sqlite"
    with sqlite3.connect(db) as connection:
        ensure_collector_schema(connection)
        base = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        events = [
            ("StatusNotification", "Available", 1, True),
            ("StatusNotification", None, None, True),
            ("Heartbeat", None, None, False),
            ("Heartbeat", None, None, False),
            ("StatusNotification", "Available", 1, True),
            ("StatusNotification", None, None, True),
            ("Heartbeat", None, None, False),
            ("Heartbeat", None, None, False),
            ("StatusNotification", "Charging", 1, True),
            ("StatusNotification", None, None, True),
            ("StatusNotification", "Available", 1, True),
            ("StatusNotification", None, None, True),
        ]
        for n, (action, status, connector, has_derived) in enumerate(events, 1):
            timestamp = base + timedelta(minutes=(n-1)//2, milliseconds=10 * ((n-1)%2))
            item = dict(id=n, action=action, charger_id="cp",
                        kind="status" if action == "StatusNotification" else "ocpp",
                        at=timestamp.isoformat())
            if has_derived:
                item.update(status=status, derived_status=status)
                if connector is not None:
                    item["connector_id"] = connector
            connection.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                               ("a", "event", str(n), n, json.dumps(item)))
    opts = dict(source_id="a", charger_id="cp", allowed_source_ids=("a",))
    groups = event_groups_page(db, **opts)["groups"]
    assert [g["first_event_id"] for g in groups] == [11, 9, 7, 5, 1]
    assert [g["event_count"] for g in groups] == [1, 1, 2, 1, 1]
    details = event_group_details(db, group_source="a", first_event_id=5, **opts)
    assert [e["id"] for e in details["events"]] == [5]
    assert [e["response_id"] for e in details["events"]] == [6]
    assert all(e["response_inferred"] is True for e in details["events"])
    assert all("direction" not in e for e in details["events"])



def test_minute_groups_combine_connectors_states_and_faults_without_losing_responses(tmp_path):
    """Four StatusNotifications at 15:47 become one expandable group."""
    from datetime import datetime, timedelta, timezone
    path = tmp_path / "minute-status.sqlite"
    with sqlite3.connect(path) as db:
        ensure_collector_schema(db)
        start = datetime(2026, 11, 16, 21, 47, tzinfo=timezone.utc)
        for i, (connector, state, error) in enumerate((
                (0, "Available", None), (2, "Available", None),
                (1, "Available", None), (1, "Faulted", "ReaderFailure"))):
            for response in (False, True):
                event_id = 95559 + 2 * i + int(response)
                at = start + timedelta(seconds=i * 7, milliseconds=8 if response else 0)
                payload = {"id": event_id, "charger_id": "real-cp",
                           "action": "StatusNotification", "kind": "status",
                           "direction": "out" if response else "in",
                           "at": at.isoformat(),
                           "status": None if response else state,
                           "connector_id": None if response else connector,
                           "error_code": None if response else error}
                db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                           ("a", "event", str(event_id), event_id, json.dumps(payload)))
        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                   ("a", "event", "95567", 95567, json.dumps({
                       "id": 95567, "charger_id": "real-cp",
                       "action": "Heartbeat", "kind": "ocpp", "direction": "in",
                       "at": "2026-11-16T21:48:00Z"})))
        # Another charger and another source share minute, never leak into group.
        for source, cp, event_id in (("a", "other-cp", 95568), ("b", "real-cp", 95569)):
            db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                       (source, "event", str(event_id), event_id, json.dumps({
                           "id": event_id, "charger_id": cp,
                           "action": "StatusNotification", "kind": "status",
                           "direction": "in", "status": "Charging",
                           "connector_id": 1, "at": "2026-11-16T21:47:30Z"})))
    kwargs = dict(source_id="a", charger_id="real-cp",
                  allowed_source_ids=("a",), zone="America/Monterrey")
    page = event_groups_page(path, **kwargs)
    assert [g["action"] for g in page["groups"]] == ["Heartbeat", "StatusNotification"]
    group = page["groups"][1]
    assert group["first_event_id"] == 95565
    assert group["event_count"] == 4
    assert "notificaciones" in group["information"]
    events = event_group_details(path, group_source="a",
                                 first_event_id=95565, **kwargs)["events"]
    assert [e["id"] for e in events] == [95565, 95563, 95561, 95559]
    assert [e["response_id"] for e in events] == [95566, 95564, 95562, 95560]
    assert [e["information"] for e in events] == [
        "C1 · Faulted · ReaderFailure", "C1 · Available",
        "C2 · Available", "C0 · Available"]
    assert all(e["direction"] == "in" for e in events)
