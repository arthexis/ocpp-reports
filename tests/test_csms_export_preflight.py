"""Actual CSMS export preflight has to inspect data, not just CLI --help."""
import sys
from pathlib import Path

import pytest

from ocpp_reports import csms_export_preflight as module


def test_readonly_export_preflight_succeeds(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["preflight", str(tmp_path)])
    observed = {}
    def producer(path, *, after, limit):
        observed.update(path=path, after=after, limit=limit)
        return {"schema": "ocpp-csms/export/v1", "data": {}}
    monkeypatch.setattr(module, "active_csms_export", producer)
    module.main()
    assert observed == {"path": tmp_path, "after": 0, "limit": 1}
    assert "readable" in capsys.readouterr().out


def test_readonly_export_preflight_rejects_bad_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["preflight", str(tmp_path)])
    monkeypatch.setattr(module, "active_csms_export", lambda *_args, **_kwargs: {"schema": "other"})
    with pytest.raises(RuntimeError, match="unsupported export schema"):
        module.main()


def test_ansible_runs_packaged_preflight_without_checkout_access():
    task = (Path(__file__).resolve().parents[1] / "ansible/tasks/local-ingest.yml").read_text()
    assert "ocpp_reports.csms_export_preflight" in task
    assert "scripts/preflight_csms_export.py" not in task
