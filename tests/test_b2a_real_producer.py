"""Real producer-to-consumer compatibility check; runs with CSMS installed in CI."""
from __future__ import annotations

import pytest

from ocpp_reports.csms_export import validate_export


def test_schema8_export_roundtrip_and_late_stop(tmp_path):
    store_module = pytest.importorskip("ocpp_csms.evidence.store")
    exporter = pytest.importorskip("ocpp_csms.export_contract")
    store = store_module.EventStore(tmp_path)
    transaction_id = store.commit_start("fixture-cp", {
        "connector_id": 1, "id_tag": "SYNTHETIC", "meter_start": 100,
        "timestamp": "2026-01-01T00:00:00Z",
    })
    first = validate_export(exporter.export_contract(tmp_path, after=0, limit=50))
    assert first["schema"] == "ocpp-csms/export/v1"
    assert first["data"]["transactions"][0]["id"] == transaction_id
    assert first["data"]["transactions"][0]["energy_wh"] is None
    cursor = first["data"]["cursor"]["next"]
    store.commit_transaction_evidence("fixture-cp", "MeterValues", {
        "connector_id": 1, "transaction_id": transaction_id,
        "meter_value": [{"timestamp": "2026-01-01T00:05:00Z", "sampled_value": [
            {"measurand": "Energy.Active.Import.Register", "unit": "Wh", "value": "350"}
        ]}],
    })
    store.commit_transaction_evidence("fixture-cp", "StopTransaction", {
        "transaction_id": transaction_id, "meter_stop": 600,
        "timestamp": "2026-01-01T00:10:00Z",
    })
    second = validate_export(exporter.export_contract(tmp_path, after=cursor, limit=50))
    assert second["data"]["source_id"] == first["data"]["source_id"]
    assert second["data"]["cursor"]["next"] > cursor
    assert any(e["action"] == "StopTransaction" for e in second["data"]["events"])
    assert second["data"]["transactions"][0]["energy_wh"] == 500
    assert second["data"]["energy"][0]["energy_wh"] == 350
    replay = validate_export(exporter.export_contract(tmp_path, after=cursor, limit=50))
    assert replay["data"]["events"] == second["data"]["events"]
    empty = validate_export(exporter.export_contract(tmp_path, after=second["data"]["cursor"]["next"], limit=50))
    assert empty["data"]["cursor"]["next"] == empty["data"]["cursor"]["after"]
    assert empty["data"]["events"] == []
