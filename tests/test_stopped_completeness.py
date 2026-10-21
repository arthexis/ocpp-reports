import csv
import io
from ocpp_reports.csv_rendering import render_csv
from ocpp_reports.reporting import build_report
from datetime import datetime, timezone
import json


def report(rows):
    return {"schema":"ocpp-reports/summary/v1","source_id":"source",
            "period":{"since":"2026-10-01T00:00:00Z","until":"2026-11-01T00:00:00Z","timezone":"UTC"},
            "summary":{"transactions":len(rows),"known_energy_wh":0,"missing_energy_transactions":0,"completeness":"complete"},
            "transactions":rows,"revision":"test"}


def test_stopped_with_meter_energy_is_complete():
    rows = [{"id":1,"charger_id":"CP","state":"stopped","active":False,"energy_wh":2500},
            {"id":2,"charger_id":"CP","state":"open","active":True,"energy_wh":100},
            {"id":3,"charger_id":"CP","state":"stopped","active":False,"energy_wh":None}]
    result = list(csv.DictReader(io.StringIO(render_csv(report(rows)))))
    assert [row["completeness"] for row in result] == ["complete","provisional","provisional"]


def test_csms_stopped_state_report_summary(tmp_path):
    payload={"schema":"ocpp-csms/export/v1","data":{"source_id":"source",
      "cursor":{"after":0,"next":1,"more":False},"events":[],"energy":[],
      "transactions":[{"id":1,"charger_id":"CP","state":"stopped","active":False,
      "started_at":"2026-10-04T00:00:00Z","stopped_at":"2026-10-04T01:00:00Z","energy_wh":2500}],
      "status":{}}}
    path=tmp_path/"export.json"
    path.write_text(json.dumps(payload))
    result=build_report(path,datetime(2026,10,1,tzinfo=timezone.utc),
                        datetime(2026,11,1,tzinfo=timezone.utc),"UTC")
    assert list(csv.DictReader(io.StringIO(render_csv(result))))[0]["completeness"] == "complete"
