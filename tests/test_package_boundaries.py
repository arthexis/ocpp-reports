"""Legacy import paths continue to expose the canonical package implementations."""
import importlib

import pytest


@pytest.mark.parametrize("legacy,canonical,public", [
    ("csms_export", "contracts.csms_export", "validate_export"),
    ("export", "contracts.export", "read_page"),
    ("ingest", "warehouse.ingestion", "ingest_page"),
    ("transaction_projection", "warehouse.transactions", "current_transactions"),
    ("warehouse_queries", "warehouse.queries", "snapshot"),
    ("forwarder", "replication.forwarder", "forward_once"),
    ("collector", "replication.collector", "accept_batch"),
    ("reporting", "reports.builder", "build_report_from_database"),
    ("rendering", "reports.rendering", "render_report"),
    ("csv_profiles", "reports.csv_profiles", "render_vendor"),
])
def test_legacy_interfaces_preserve_function_identity(legacy, canonical, public):
    old = importlib.import_module("ocpp_reports." + legacy)
    new = importlib.import_module("ocpp_reports." + canonical)
    assert getattr(old, public) is getattr(new, public)


def test_producer_contract_validator_is_shared():
    from ocpp_reports.csms_export import validate_export as old
    from ocpp_reports.contracts.csms_export import validate_export as canonical
    assert old is canonical
