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
