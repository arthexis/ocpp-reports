"""M1 continuous warehouse integration against export/v1 fixtures."""
import copy
import json
from pathlib import Path

import pytest

from ocpp_reports.follow import follow, current_connectors, source_cursor, sync_page


FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"


def page(name):
    return json.loads((FIXTURES / name).read_text())


def test_follow_catches_up_and_restarts_without_duplicates(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    active, late = page("active.json"), page("late_stop.json")
    source = active["data"]["source_id"]
    calls = []

    def producer(_directory, *, after, limit):
        calls.append(after)
        if after == 0:
            return copy.deepcopy(active)
        if after == active["data"]["cursor"]["next"]:
            return copy.deepcopy(late)
        empty = copy.deepcopy(late)
        empty["data"]["events"] = []
        empty["data"]["energy"] = []
        empty["data"]["transactions"] = []
        empty["data"]["cursor"] = {"after": after, "next": after, "more": False}
        return empty

    follow(database, tmp_path, once=True, producer=producer)
    cursor = source_cursor(database, source)
    assert cursor == late["data"]["cursor"]["next"]
    follow(database, tmp_path, once=True, producer=producer)
    assert source_cursor(database, source) == cursor
    assert calls.count(0) == 2  # source identity probe


def test_state_updates_even_without_cursor_progress(tmp_path):
    database = tmp_path / "warehouse.sqlite"
    first = page("empty.json")
    first["data"]["status"] = {"chargers": [{
        "id": "cp", "connected": True,
        "connectors": [{"id": 1, "status": "Available", "transaction": None}]
    }]}
    first["data"]["exported_at"] = "2026-01-01T00:00:00Z"
    sync_page(database, first)
    second = copy.deepcopy(first)
    second["data"]["status"]["chargers"][0]["connectors"][0]["status"] = "Charging"
    second["data"]["exported_at"] = "2026-01-01T00:00:05Z"
    sync_page(database, second)
    assert current_connectors(database)[0]["status"] == "Charging"
    sync_page(database, first)
    assert current_connectors(database)[0]["status"] == "Charging"


def test_invalid_follow_configuration(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        follow(tmp_path / "db", tmp_path, interval=0, once=True, producer=lambda *_a, **_k: {})
