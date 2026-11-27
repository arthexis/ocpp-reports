"""Detached backup safety checks; never touch a deployed warehouse."""
from pathlib import Path
import importlib.util
import sqlite3

import pytest

SPEC = importlib.util.spec_from_file_location(
    "backup_sqlite", Path(__file__).resolve().parents[1] / "scripts/backup_sqlite.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_wal_snapshot_includes_committed_evidence_without_live_changes(tmp_path):
    source = tmp_path / "live.sqlite"
    output = tmp_path / "private"
    output.mkdir(mode=0o700)
    dest = output / "snapshot.sqlite"
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE evidence(id INTEGER)")
        writer.executemany("INSERT INTO evidence VALUES (?)", [(1,), (2,)])
        writer.commit()
        result = MODULE.snapshot(source, dest)
        assert result["integrity"] == "ok"
        assert len(result["sha256"]) == 64
        assert dest.stat().st_mode & 0o777 == 0o600
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert writer.execute("SELECT * FROM evidence").fetchall() == [(1,), (2,)]
        writer.execute("INSERT INTO evidence VALUES (3)")
        writer.commit()
        with sqlite3.connect(dest.as_uri() + "?mode=ro&immutable=1", uri=True) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored.execute("SELECT * FROM evidence").fetchall() == [(1,), (2,)]
    assert sorted(p.name for p in output.iterdir()) == ["snapshot.sqlite"]


def test_refuses_overwrite_and_nonprivate_parent(tmp_path):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence(id INTEGER)")
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        MODULE.snapshot(source, public / "out.sqlite")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    dest = private / "out.sqlite"
    dest.write_bytes(b"retain")
    with pytest.raises(FileExistsError):
        MODULE.snapshot(source, dest)
    assert dest.read_bytes() == b"retain"


def test_invalid_source_publishes_no_partial(tmp_path):
    source = tmp_path / "bad.sqlite"
    source.write_bytes(b"not SQLite")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(sqlite3.DatabaseError):
        MODULE.snapshot(source, private / "out.sqlite")
    assert not list(private.iterdir())
