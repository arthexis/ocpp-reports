"""Supplemental B2A tests for the revised single-cursor producer contract."""
import json
from pathlib import Path

from ocpp_reports.csms_export import validate_export

ROOT = Path(__file__).parent / "fixtures" / "csms_export_v1"


def load(name):
    return json.loads((ROOT / (name + ".json")).read_text(encoding="utf-8"))


def test_late_stop_is_visible_with_one_event_cursor():
    start, stop = load("active"), load("late_stop")
    assert start["data"]["cursor"]["next"] == stop["data"]["cursor"]["after"]
    assert start["data"]["source_id"] == stop["data"]["source_id"]
    before, after = start["data"]["transactions"][0], stop["data"]["transactions"][0]
    assert before["id"] == after["id"] == 42
    assert before["energy_wh"] is None
    assert after["energy_wh"] == 500
    assert before["quality"]["active"] and not after["quality"]["active"]
    assert stop["data"]["events"][0]["kind"] == "transaction_stopped"


def test_empty_page_does_not_advance_cursor():
    empty = load("empty")["data"]
    assert empty["cursor"] == {"after": 2, "next": 2, "more": False}
    assert empty["events"] == empty["transactions"] == []


def test_provenance_and_no_revision_feed():
    for name in ("active", "late_stop", "empty"):
        data = load(name)["data"]
        assert "transaction_changes" not in data
        assert "changes_after" not in data
        assert data["cursor"]["next"] >= data["cursor"]["after"]
    stop = load("late_stop")["data"]["transactions"][0]
    assert stop["start_event_at"] != stop["start_received_at"]
    assert stop["stop_event_at"] != stop["stop_received_at"]


def test_validator_accepts_sanitized_single_cursor_pages():
    for name in ("active", "late_stop", "empty"):
        page = load(name)
        assert validate_export(page) is page
