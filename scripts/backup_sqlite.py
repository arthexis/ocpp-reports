"""Operator-only, bounded SQLite snapshot; no live repair, hold, or restore."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time


def snapshot(source: Path, destination: Path, *, seconds: float = 90) -> dict:
    if not 0 < seconds <= 600:
        raise ValueError("timeout must be within 0–600 seconds")
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if not stat.S_ISREG(source.lstat().st_mode):
        raise ValueError("source must be a regular SQLite file")
    parent = destination.parent
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("destination parent must be an owned private directory")
    if destination.exists() or destination.is_symlink() or source == destination:
        raise FileExistsError("destination must be new")
    deadline = time.monotonic() + seconds

    def remaining():
        if time.monotonic() >= deadline:
            raise TimeoutError("snapshot deadline reached")

    def progress(status, remaining_pages, total_pages):
        remaining()

    fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=parent)
    os.close(fd)
    temp = Path(name)
    published = False
    try:
        # Online backup API reads a consistent committed snapshot. Never use
        # immutable=1 on the source, BEGIN IMMEDIATE, or a live integrity scan.
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as src:
            src.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(temp, timeout=0.2)) as dst:
                src.backup(dst, pages=64, progress=progress, sleep=0.05)
        remaining()
        # Validate only the closed, detached backup; no production reader lock.
        with closing(sqlite3.connect(temp.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as copy:
            copy.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("snapshot integrity check failed")
        digest = hashlib.sha256()
        with temp.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                remaining()
                digest.update(chunk)
            os.fsync(stream.fileno())
        size = temp.stat().st_size
        remaining()
        # Atomic no-overwrite publication; a conflicting path stays untouched.
        os.link(temp, destination)
        published = True
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return {"bytes": size, "sha256": digest.hexdigest(), "integrity": "ok"}
    except BaseException:
        if published:
            destination.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(temp) + suffix).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.destination, seconds=args.timeout), sort_keys=True))


if __name__ == "__main__":
    main()
