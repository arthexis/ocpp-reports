"""A read-only keeper must allow checkpoints and preserve CSMS ownership."""
from contextlib import closing
from pathlib import Path
import socket
import sqlite3

import pytest

from ocpp_reports import csms_wal_reader as reader


def test_readonly_keeper_preserves_sidecars_without_blocking_writers_or_checkpoint(tmp_path):
    path = tmp_path / "csms.sqlite"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY)")
        db.commit()
    assert not Path(str(path) + "-wal").exists()
    with closing(reader.open_reader(path)) as keeper:
        assert not keeper.in_transaction
        assert Path(str(path) + "-wal").is_file()
        assert Path(str(path) + "-shm").is_file()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            keeper.execute("INSERT INTO evidence VALUES (1)")
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("INSERT INTO evidence VALUES (2)")
            writer.commit()
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        assert Path(str(path) + "-wal").is_file()
        assert keeper.execute("SELECT id FROM evidence").fetchall() == [(2,)]
        assert not keeper.in_transaction


def test_acl_grants_are_readonly_and_only_for_sidecars(tmp_path, monkeypatch):
    path = tmp_path / "csms.sqlite"
    path.touch()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).touch()
    commands = []
    monkeypatch.setattr(reader.subprocess, "run", lambda argv, **kwargs: commands.append(argv))
    reader.grant_sidecar_reads(path)
    assert commands == [["setfacl", "-m", "u:ocpp-reports:r--", str(path) + suffix]
                        for suffix in ("-wal", "-shm")]


def test_notify_readiness_uses_systemd_unix_socket(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        server.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        reader.notify("READY=1")
        assert server.recv(100) == b"READY=1"


def test_units_keep_reports_unprivileged_and_source_database_readonly():
    templates = Path(__file__).resolve().parents[1] / "ansible/templates"
    helper = (templates / "ocpp-reports-csms-reader.service.j2").read_text()
    ingest = (templates / "ocpp-reports-ingest.service.j2").read_text()
    assert "Type=notify" in helper
    assert "User={{ reports_local_csms_reader.user }}" in helper
    assert "ReadOnlyPaths={{ reports_local_csms_reader.database }}" in helper
    assert "ProtectSystem=strict" in helper
    assert "PartOf=ocpp-reports-ingest.service" in helper
    assert "Requires=ocpp-reports-csms-reader.service" in ingest
    assert "User=ocpp-reports" in ingest
    assert "ReadOnlyPaths={{ reports_local_csms_data_dir }}" in ingest
