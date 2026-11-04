from ocpp_reports.web import _operational_status_counts, _single_connector_disagreements

def row(connector, status, charger="one"):
    return {"source_id": "source", "charger_id": charger,
            "connector_id": connector, "status": status}

def test_single_connector_disagreement_counts_station_available_but_keeps_fault():
    rows = [row(0, "Available"), row(1, "Faulted")]
    assert _single_connector_disagreements(rows) == {("source", "one", 1)}
    counts = _operational_status_counts(rows)
    assert counts["available"]["stations"] == 1
    assert counts["faults"]["stations"] == 0
    assert counts["faults"]["connectors"] == 1

def test_multi_connector_fault_is_not_reclassified():
    rows = [row(0, "Available"), row(1, "Faulted"), row(2, "Available")]
    assert not _single_connector_disagreements(rows)
    assert _operational_status_counts(rows)["faults"]["stations"] == 1

def test_no_station_evidence_does_not_override_fault():
    rows = [row(1, "Faulted")]
    assert not _single_connector_disagreements(rows)
    assert _operational_status_counts(rows)["faults"]["stations"] == 1

def test_single_connector_without_fault_does_not_warn():
    rows = [row(0, "Available"), row(1, "Available")]
    assert not _single_connector_disagreements(rows)
