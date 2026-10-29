"""Offline receipt verification and continued forwarding after conversion."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from ocpp_reports.changefeed import changes_page
from ocpp_reports.collector import create_collector
from ocpp_reports.replication.collector import Batch, accept_batch
from ocpp_reports.replication.forwarder import forward_once
from ocpp_reports.source_migration import migrate_copies
from ocpp_reports.source_preflight import check_snapshots
from test_source_migration import init


def snapshots(tmp_path):
    paths = init(tmp_path)
    collector = tmp_path / "collector.sqlite3"
    page = changes_page(paths[1], "uuid-old")
    page["source_id"] = "gway-005"
    accept_batch(collector, Batch.model_validate(page))
    return (*paths, collector)


def check(paths):
    return check_snapshots(*paths, "uuid-old", "gway-005", "gway-005")


def test_read_only_preflight_and_cli(tmp_path):
    paths = snapshots(tmp_path)
    before = [p.read_bytes() for p in paths]
    report = check(paths)
    assert report["source_cursor"] == 135
    assert report["replication_sequence"] == report["forwarder_ack"] == report["collector_cursor"] == 73
    assert report["retained_changes"] == 1
    assert report["cutover_authorized"] is False
    command = [sys.executable, "-m", "ocpp_reports.source_preflight"]
    for option, path in zip(("csms", "warehouse", "forwarder", "collector"), paths):
        command += ["--" + option, str(path)]
    command += ["--old", "uuid-old", "--new", "gway-005", "--node", "gway-005"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == report
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize("database,sql,error", [
    (2, "UPDATE forward_cursors SET sequence=72", "must agree"),
    (1, "INSERT INTO replication_changes VALUES (74,'uuid-old','event','24','{}')", "must agree"),
    (3, "UPDATE collector_cursors SET sequence=74", "must agree"),
    (3, "UPDATE collector_changes SET sequence=72", "history differs"),
    (3, "DELETE FROM collector_changes", "history differs"),
    (3, "UPDATE collector_changes SET payload='{\"different\":true}'", "history differs"),
    (3, "UPDATE collector_changes SET record_key='other'", "history differs"),
    (3, "INSERT INTO collector_cursors VALUES ('uuid-old',73)", "collision review"),
    (2, "DELETE FROM source_bindings", "durable source binding"),
    (2, "DELETE FROM forward_cursors", "receipt required"),
    (3, "DELETE FROM collector_cursors", "receipt required"),
    (1, "INSERT INTO sources VALUES ('gway-005',0)", "expected old source"),
    (0, "UPDATE appliance_metadata SET value='other'", "CSMS identity"),
])
def test_refuses_unsynchronized_or_unrelated_histories(tmp_path, database, sql, error):
    paths = snapshots(tmp_path)
    with sqlite3.connect(paths[database]) as db:
        db.execute(sql)
    before = [p.read_bytes() for p in paths]
    with pytest.raises(ValueError, match=error):
        check(paths)
    assert [p.read_bytes() for p in paths] == before


def test_missing_snapshot_is_not_created(tmp_path):
    paths = snapshots(tmp_path)
    paths[-1].unlink()
    with pytest.raises(ValueError, match="must exist"):
        check(paths)
    assert not paths[-1].exists()


def test_payload_canonicalization_and_other_collector_nodes(tmp_path):
    paths = snapshots(tmp_path)
    with sqlite3.connect(paths[1]) as db:
        db.execute("UPDATE replication_changes SET payload=?", ('{ "z": 1, "a": 2 }',))
    with sqlite3.connect(paths[3]) as db:
        db.execute("UPDATE collector_changes SET payload=?", ('{"a":2,"z":1}',))
        db.execute("INSERT INTO collector_cursors VALUES ('other-node',999)")
        db.execute("INSERT INTO collector_changes VALUES ('other-node',999,'event','1','{}')")
    assert check(paths)["retained_changes"] == 1


def test_cli_failure_is_nonzero_and_does_not_create_missing_inputs(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    command = [sys.executable, "-m", "ocpp_reports.source_preflight"]
    for option in ("csms", "warehouse", "forwarder", "collector"):
        command += ["--" + option, str(missing)]
    command += ["--old", "uuid-old", "--new", "gway-005", "--node", "gway-005"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "must exist" in result.stderr
    assert not result.stdout
    assert not missing.exists()


def test_conversion_then_forwarding_has_no_replay_or_gap(tmp_path):
    paths = snapshots(tmp_path)
    check(paths)
    files = migrate_copies(*paths[:3], tmp_path / "converted", "uuid-old", "gway-005", "gway-005")
    warehouse, state = map(Path, (files["warehouse"], files["forwarder"]))
    receiver = TestClient(create_collector(paths[3], {"gway-005": "existing-token"}))
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        response = receiver.post("/api/replication", json=sent[-1],
                                 headers={"Authorization": request.headers["Authorization"]})
        return httpx.Response(response.status_code, json=response.json())

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        def forward():
            return forward_once(warehouse, state, "gway-005", "https://collector/api/replication",
                                "existing-token", client=client)
        assert forward() == 73
        assert not sent
        with sqlite3.connect(warehouse) as db:
            db.execute("INSERT INTO replication_changes VALUES (74,'gway-005','event','24','{}')")
        assert forward() == 74
        assert forward() == 74
    assert len(sent) == 1
    assert sent[0]["after"] == 73
    assert [c["sequence"] for c in sent[0]["changes"]] == [74]
    with sqlite3.connect(paths[3]) as db:
        assert db.execute("SELECT sequence FROM collector_changes ORDER BY sequence").fetchall() == [(73,), (74,)]
