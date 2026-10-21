"""ExportPage retains the CSMS wire contract without a Pydantic warning."""
import json
import warnings
from pathlib import Path

from ocpp_reports.export import ExportPage, read_page


def test_export_schema_alias_roundtrip():
    fixture = Path(__file__).parent / "fixtures/csms_export_v1/active.json"
    payload = json.loads(fixture.read_text())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        page = ExportPage.model_validate(payload)
    assert not any("shadows an attribute" in str(w.message) for w in caught)
    assert page.schema_id == "ocpp-csms/export/v1"
    assert page.model_dump(by_alias=True)["schema"] == payload["schema"]
    assert read_page(fixture).schema_id == page.schema_id
