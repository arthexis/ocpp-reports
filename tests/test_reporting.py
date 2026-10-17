import json
import csv
import io
from datetime import datetime, timezone
from pathlib import Path
import pytest
from ocpp_reports.export import read_page
from ocpp_reports.reporting import build_report, render
from ocpp_reports.rendering import render_report
from ocpp_reports.csv_rendering import COLUMNS

def sample(tmp_path):
    source = tmp_path / "export.json"
    source.write_text(json.dumps({"schema":"ocpp-csms/export/v1","data":{"source_id":"station-1","cursor":{"after":0,"next":2,"more":False},"events":[],"energy":[],"transactions":[{"id":10,"charger_id":"CP1","connector_id":1,"rfid":"A","state":"completed","active":False,"started_at":"2026-10-01T10:00:00+00:00","stopped_at":"2026-10-01T11:00:00+00:00","energy_wh":2300},{"id":11,"charger_id":"CP1","state":"active","active":True,"started_at":"2026-10-02T00:00:00+00:00","energy_wh":None}],"status":{}}}))
    return source

def test_contract(tmp_path):
    assert read_page(sample(tmp_path)).data.transactions[0].energy_wh == 2300

def test_half_open_energy_and_determinism(tmp_path):
    path = sample(tmp_path)
    report = build_report(path,datetime(2026,10,1,tzinfo=timezone.utc),datetime(2026,10,2,tzinfo=timezone.utc),"UTC")
    assert report["summary"]["transactions"] == 1
    assert report["summary"]["known_energy_wh"] == 2300
    assert report["revision"] == build_report(path,datetime(2026,10,1,tzinfo=timezone.utc),datetime(2026,10,2,tzinfo=timezone.utc),"UTC")["revision"]
    assert "energy_wh" in render(report,"csv")

def test_schema_rejection(tmp_path):
    path = sample(tmp_path)
    value = json.loads(path.read_text())
    value["schema"] = "unknown"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Unsupported"):
        read_page(path)

def test_no_source_write(tmp_path):
    path = sample(tmp_path)
    before = path.read_bytes()
    build_report(path,datetime(2026,10,1,tzinfo=timezone.utc),datetime(2026,10,2,tzinfo=timezone.utc),"UTC")
    assert path.read_bytes() == before


def test_generate_csv_uses_shared_schema_and_formula_escaping(tmp_path):
    path = sample(tmp_path)
    raw = json.loads(path.read_text())
    raw['data']['transactions'][0]['rfid'] = '\t=1+1'
    path.write_text(json.dumps(raw), encoding='utf-8')
    report = build_report(path, datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc), 'UTC')
    output = render(report, 'csv')
    assert output == render_report(report, 'csv')
    assert output == render(report, 'csv')
    rows = list(csv.DictReader(io.StringIO(output)))
    assert tuple(rows[0]) == COLUMNS
    assert rows[0]['rfid'] == "'\t=1+1"
    assert rows[0]['energy_wh'] == '2300'
