"""Replication change journal tests."""
from ocpp_reports.changefeed import changes_page
from ocpp_reports.follow import sync_page


def page(source, observed, status):
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": source, "exported_at": observed,
        "cursor": {"after": 0, "next": 0, "more": False},
        "events": [], "energy": [], "transactions": [],
        "status": {"chargers": [{"id": "cp-1", "connected": True,
            "connectors": [{"id": 1, "status": status, "transaction": None}]}]}
    }}


def test_connector_changes_without_new_events(tmp_path):
    db = tmp_path / "reports.sqlite"
    sync_page(db, page("sat-a", "2026-10-19T00:00:00Z", "Available"))
    first = changes_page(db, "sat-a")
    assert len(first["changes"]) == 1
    sync_page(db, page("sat-a", "2026-10-19T00:00:05Z", "Charging"))
    second = changes_page(db, "sat-a", after=first["next"])
    assert len(second["changes"]) == 1
    assert second["changes"][0]["payload"]["status"] == "Charging"
    sync_page(db, page("sat-b", "2026-10-19T00:00:00Z", "Available"))
    assert changes_page(db, "sat-a", after=second["next"])["changes"] == []
    assert len(changes_page(db, "sat-b")["changes"]) == 1
