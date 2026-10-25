"""Actual CSMS export preflight has to inspect data, not just CLI --help."""
import importlib.util
from pathlib import Path
import sys

import pytest

script = Path(__file__).resolve().parents[1] / "scripts" / "preflight_csms_export.py"
spec = importlib.util.spec_from_file_location("preflight_csms_export", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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
