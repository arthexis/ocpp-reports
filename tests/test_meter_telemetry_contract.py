"""Hermetic contract and ingestion tests for measured OCPP telemetry."""
import copy
import json
import sqlite3

import pytest

from ocpp_reports.contracts.csms_export import validate_export, validate_meter_fields
from ocpp_reports.warehouse.ingestion import ingest_page


def _sample():
    return {
        "source_event_id": 1, "sample_index": 0,
        "at": "2026-11-16T20:00:00Z",
        "charger_id": "cp", "connector_id": 1, "transaction_id": 7,
        "energy_wh": 7500.0, "power_w": None,
        "soc": 65.0, "soc_provenance": "ocpp-meter-values",
        "voltage_v": 220.0, "current_import_a": 13.5,
        "current_offered_a": 32.0, "temperature_c": -5.0,
        "meter_channels": [
            {"measurand": "SoC", "unit": "Percent", "value": 65.0},
            {"measurand": "Voltage", "unit": "V", "value": 220.0},
            {"measurand": "Current.Import", "unit": "A", "value": 13.5},
            {"measurand": "Current.Offered", "unit": "A", "value": 32.0},
            {"measurand": "Temperature", "unit": "Celsius", "value": -5.0},
        ]
    }


def _page(sample):
    return {"schema": "ocpp-csms/export/v1", "data": {
        "source_id": "source-a", "cursor": {"after": 0, "next": 1, "more": False},
        "events": [{"id": 1, "at": sample["at"], "charger_id": "cp",
                    "kind": "meter_values", "action": "MeterValues"}],
        "energy": [sample], "transactions": [], "status": {"chargers": []},
    }}


def test_measured_channels_roundtrip_and_idempotent_immutable_storage(tmp_path):
    sample = _sample()
    page = _page(sample)
    validate_export(page)
    warehouse = tmp_path / "warehouse.sqlite"
    assert ingest_page(warehouse, page) == 1
    assert ingest_page(warehouse, page) == 1
    with sqlite3.connect(warehouse) as db:
        rows = db.execute("SELECT payload FROM energy_samples").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == sample
    changed = copy.deepcopy(page)
    changed["data"]["energy"][0]["voltage_v"] = 200
    changed["data"]["energy"][0]["meter_channels"][1]["value"] = 200
    validate_export(changed)
    with pytest.raises(ValueError, match="Conflicting energy sample"):
        ingest_page(warehouse, changed)


@pytest.mark.parametrize("bad", [
    {"soc": 101},
    {"soc": 65.0, "soc_provenance": "operator"},
    {"voltage_v": 210.0},
    {"current_import_a": float("nan")},
    {"current_offered_a": True},
    {"temperature_c": -274.0},
    {"meter_channels": [{"measurand": "Voltage", "unit": "kV", "value": 0.22}]},
    {"meter_channels": [{"measurand": "Unknown", "unit": "V", "value": 220}]},
    {"meter_channels": [{"measurand": "SoC", "unit": "Percent", "value": 42,
                          "phase": []}]},
])
def test_unverified_or_conflicting_telemetry_fails_closed(bad):
    sample = _sample()
    sample.update(bad)
    with pytest.raises(ValueError):
        validate_meter_fields(sample)


def test_old_samples_and_phase_specific_channels_remain_compatible():
    legacy = {"source_event_id": 1, "sample_index": 0, "at": "2026-11-16T20:00:00Z",
              "charger_id": "cp", "connector_id": 1, "transaction_id": 7,
              "power_w": None, "energy_wh": 1200.0}
    validate_export(_page(legacy))
    phased = {**legacy, "meter_channels": [
        {"measurand": "Voltage", "unit": "V", "value": 127.0, "phase": "L1-N"},
        {"measurand": "Voltage", "unit": "V", "value": 130.0, "phase": "L2-N"},
    ]}
    validate_meter_fields(phased)
    assert "voltage_v" not in phased
