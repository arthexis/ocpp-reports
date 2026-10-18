"""B2A producer-shaped fixture and strict consumer contract regressions."""
import copy
import json
from pathlib import Path

import pytest

from ocpp_reports.csms_export import validate_export

FIXTURE = Path(__file__).parent / "fixtures" / "csms_export_v1_page.json"


@pytest.fixture
def page():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_sanitized_export_contract(page):
    assert validate_export(page) is page
    assert page["data"]["transactions"][0]["energy_wh"] is None
    assert page["data"]["energy"][0]["power_w"] == 7200.0


def test_unknown_additive_fields_are_compatible(page):
    page["new_top_level"] = {"future": True}
    page["data"]["new_source_field"] = "opaque"
    page["data"]["transactions"][0]["future_field"] = [1]
    assert validate_export(page) is page


def test_empty_page_and_nonzero_retry_cursor(page):
    page["data"].update(cursor={"after": 15, "next": 15, "more": False},
                        events=[], energy=[], transactions=[])
    assert validate_export(page) is page


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema="ocpp-csms/export/v2"),
    lambda p: p["data"].update(source_id=""),
    lambda p: p["data"]["cursor"].update(after=True),
    lambda p: p["data"]["cursor"].update(next=0),
    lambda p: p["data"]["cursor"].update(more="false"),
    lambda p: p["data"]["events"].reverse(),
    lambda p: p["data"]["events"][0].update(id=0),
    lambda p: p["data"]["events"][0].update(at="2026-10-07T12:00:00"),
    lambda p: p["data"]["events"][0].update(charger_id=None),
    lambda p: p["data"]["energy"][0].update(source_event_id=99),
    lambda p: p["data"]["energy"][0].update(at="2026-10-07T12:01:00"),
    lambda p: p["data"]["energy"][0].update(power_w="7200"),
    lambda p: p["data"]["energy"][0].update(power_w=float("nan")),
    lambda p: p["data"]["energy"][0].update(energy_wh=float("inf")),
    lambda p: p["data"]["transactions"][0].update(energy_wh="1500"),
    lambda p: p["data"]["transactions"][0].update(started_at="2026-10-07T12:00:00"),
    lambda p: p["data"].pop("status"),
    lambda p: p["data"].update(transactions=None),
])
def test_invalid_export_rejected(page, mutate):
    mutate(page)
    with pytest.raises(ValueError):
        validate_export(page)


def test_validation_does_not_mutate_export(page):
    before = copy.deepcopy(page)
    validate_export(page)
    assert page == before


def test_fixture_is_byte_stable(page):
    assert json.loads(json.dumps(page, sort_keys=True)) == page

PRODUCER_FIXTURES = Path(__file__).parent / "fixtures" / "csms_export_v1"


@pytest.mark.parametrize("name", ["active.json", "late_stop.json", "empty.json"])
def test_producer_shaped_pages(name):
    page = json.loads((PRODUCER_FIXTURES / name).read_text(encoding="utf-8"))
    assert validate_export(page) is page


@pytest.mark.parametrize("mutate", [
    lambda p: p["data"].update(exported_at="not-a-date"),
    lambda p: p["data"]["transactions"][0].update(start_received_at="2026-01-01T00:00:00"),
    lambda p: p["data"]["transactions"][0].update(meter_sample_times="invalid"),
    lambda p: p["data"]["transactions"][0].update(meter_sample_times=[{"payload_index": 0, "sample_index": 0, "event_at": "bad", "received_at": None}]),
    lambda p: p["data"]["transactions"][0].update(quality={"incomplete": "false"}),
])
def test_producer_additive_metadata_rejects_malformed_values(mutate):
    page = json.loads((PRODUCER_FIXTURES / "active.json").read_text(encoding="utf-8"))
    mutate(page)
    with pytest.raises(ValueError):
        validate_export(page)


def test_producer_additive_metadata_accepts_nulls_and_future_fields():
    page = json.loads((PRODUCER_FIXTURES / "active.json").read_text(encoding="utf-8"))
    tx = page["data"]["transactions"][0]
    tx["meter_sample_times"] = [{"payload_index": 0, "sample_index": 0,
                                 "event_at": None, "received_at": "2026-01-01T00:01:00Z"}]
    tx["quality"]["future_flag"] = {"value": "unknown"}
    tx["future_metadata"] = True
    assert validate_export(page) is page
