"""Diagnostic archives are immutable and source-scoped."""
from io import BytesIO

from fastapi.testclient import TestClient

from ocpp_reports.collector import create_collector
from ocpp_reports.diagnostic_archive import inventory, store


def test_store_replay_and_source_isolation(tmp_path):
    first = store(tmp_path, "sat-001", "CP001", "charger.zip", BytesIO(b"PK\\x03\\x04data"))
    second = store(tmp_path, "sat-001", "CP001", "charger.zip", BytesIO(b"PK\\x03\\x04data"))
    assert first == second
    assert len(inventory(tmp_path, "sat-001", "CP001")) == 1
    assert inventory(tmp_path, "sat-002", "CP001") == []
    other = store(tmp_path, "sat-002", "CP001", "charger.zip", BytesIO(b"PK\\x03\\x04data"))
    assert other["sha256"] == first["sha256"]
    assert len(inventory(tmp_path)) == 2


def test_reject_invalid_identity_and_size(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        store(tmp_path, "../bad", "CP001", "diag", BytesIO(b"hello"))
    with pytest.raises(ValueError):
        store(tmp_path, "sat-001", "CP001", "diag", BytesIO(b"hello"), maximum=2)
    assert not list(tmp_path.glob(".incoming-*"))


def test_collector_requires_source_token_and_acknowledges_checksum(tmp_path, monkeypatch):
    from ocpp_reports import diagnostic_archive
    monkeypatch.setattr(diagnostic_archive, "DEFAULT_ARCHIVE", tmp_path / "archives")
    client = TestClient(create_collector(tmp_path / "warehouse.sqlite", {"sat-001": "secret"}))
    url = "/api/diagnostics?source_id=sat-001&cp=CP001&filename=diag.zip"
    assert client.post(url, content=b"abc").status_code == 401
    assert client.post(url, content=b"abc",
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.post(url, content=b"abc",
                           headers={"Authorization": "Bearer secret"})
    import hashlib
    assert response.status_code == 200
    assert response.json()["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert client.post(url, content=b"abc",
                       headers={"Authorization": "Bearer secret"}).status_code == 200
    assert len(inventory(tmp_path / "archives")) == 1


def test_forwarder_discovers_retained_uploads_and_deduplicates(tmp_path, monkeypatch):
    import json
    from ocpp_reports import diagnostic_archive as archive
    data_dir = tmp_path / "csms"
    records = data_dir / "diagnostics-requests"
    records.mkdir(parents=True)
    uploads = tmp_path / "uploads"
    token = "a" * 40
    folder = uploads / "retained" / token
    folder.mkdir(parents=True)
    (folder / "upload.bin").write_bytes(b"diagnostic evidence")
    (records / f"{token}.json").write_text(json.dumps({"charger": "CP001"}))
    seen = []

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"sha256": __import__("hashlib").sha256(b"diagnostic evidence").hexdigest()}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, endpoint, *, params, content, headers):
            assert endpoint == "https://collector.example/api/diagnostics"
            assert headers["Authorization"] == "Bearer secret"
            assert params["source_id"] == "sat-001"
            assert params["cp"] == "CP001"
            seen.append(content.read())
            return Reply()

    monkeypatch.setattr(archive.httpx, "Client", Client, raising=False) if hasattr(archive, "httpx") else None
    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    state = tmp_path / "forwarder"
    args = (data_dir, uploads, state, "https://collector.example/api/diagnostics", "sat-001", "secret")
    assert archive.forward_archives(*args) == 1
    assert archive.forward_archives(*args) == 0
    assert seen == [b"diagnostic evidence"]


def test_diagnose_cp_defaults_to_single_indexed_charger(tmp_path):
    from ocpp_reports.cli import _resolve_diagnostic_cp
    store(tmp_path, "site-a", "CP001", "first.zip", BytesIO(b"one"))
    assert _resolve_diagnostic_cp(None, tmp_path) == "CP001"
    assert _resolve_diagnostic_cp("OTHER", tmp_path) == "OTHER"
    store(tmp_path, "site-a", "CP002", "other.zip", BytesIO(b"two"))
    import pytest
    with pytest.raises(ValueError, match="Specify --cp"):
        _resolve_diagnostic_cp(None, tmp_path)


def test_diagnose_cp_defaults_from_local_request_records(tmp_path, monkeypatch):
    import json
    from ocpp_reports import active_csms
    from ocpp_reports.cli import _resolve_diagnostic_cp
    data = tmp_path / "8"
    records = data / "diagnostics-requests"
    records.mkdir(parents=True)
    (records / ("a" * 40 + ".json")).write_text(json.dumps({"charger": "CP001"}))
    monkeypatch.setattr(active_csms, "discover_active_csms", lambda: (data, "/usr/bin/python3"))
    assert _resolve_diagnostic_cp(None, tmp_path / "empty") == "CP001"


def test_diagnose_serve_credentials_have_real_newlines(monkeypatch):
    from typer.testing import CliRunner
    from ocpp_reports.cli import app
    from ocpp_reports import diagnostic_archive
    monkeypatch.setattr(diagnostic_archive, "publish", lambda *args: {
        "username": "CP001", "password": "secret", "files": 4,
    })
    result = CliRunner().invoke(app, ["diagnose", "serve", "--cp", "CP001"])
    assert result.exit_code == 0, result.output
    assert "Username: CP001\nPassword: secret\nFiles: 4" in result.output
    assert r"CP001\nPassword" not in result.output


def test_preview_escapes_untrusted_log_html(tmp_path):
    import zipfile
    from ocpp_reports.diagnostic_archive import _archive_preview, _viewer_page
    archive = tmp_path / "diag.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("service.log", "<script>alert(1)</script>")
    preview = _archive_preview(archive)
    html = _viewer_page("diagnostic", preview, "download.zip")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "download.zip" in html


def test_local_import_preserves_and_repairs_original_receipt_time(tmp_path):
    import json
    import os
    from datetime import datetime, timezone
    from ocpp_reports.diagnostic_archive import import_local
    data = tmp_path / "csms"
    records = data / "diagnostics-requests"
    records.mkdir(parents=True)
    token = "a" * 40
    (records / (token + ".json")).write_text(json.dumps({"charger": "CP001"}))
    upload = tmp_path / "uploads" / token
    upload.mkdir(parents=True)
    archive = upload / "upload.bin"
    archive.write_bytes(b"PK\\x03\\x04content")
    moment = datetime(2026, 10, 22, 22, 55, tzinfo=timezone.utc).timestamp()
    os.utime(archive, (moment, moment))
    destination = tmp_path / "reports"
    assert import_local(destination, data, tmp_path / "uploads", "host") == 1
    rows = inventory(destination, "host", "CP001")
    assert rows[0]["received_at"].startswith("2026-10-22T22:55:")
    assert import_local(destination, data, tmp_path / "uploads", "host") == 0
    assert inventory(destination, "host", "CP001")[0]["received_at"] == rows[0]["received_at"]
