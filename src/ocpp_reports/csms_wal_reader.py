"""Keep SQLite WAL sidecars available to the unprivileged Reports reader.

The helper runs as the existing CSMS user, with the main database mounted
read-only. Its idle read-only connection keeps WAL/SHM files alive without an
open transaction, allowing writers to continue and checkpoints to complete.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import sqlite3
import subprocess
import threading

from .active_csms import discover_active_csms


def describe(data_dir: Path) -> dict:
    active_dir, _ = discover_active_csms()
    if data_dir.resolve() != active_dir.resolve():
        raise ValueError("Selected directory differs from active CSMS")
    pid = int(subprocess.check_output(
        ["systemctl", "show", "ocpp-csms.service", "--property=MainPID", "--value"],
        text=True).strip())
    uid = Path(f"/proc/{pid}").stat().st_uid
    if uid == 0:
        raise ValueError("Read-access helper requires an unprivileged CSMS owner")
    database = data_dir.resolve() / "ocpp-csms.sqlite3"
    if not database.is_file():
        raise ValueError("Active CSMS database is missing")
    return {"user": pwd.getpwuid(uid).pw_name, "uid": uid, "database": str(database)}


def open_reader(database: Path) -> sqlite3.Connection:
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                         isolation_level=None)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except Exception:
        db.close()
        raise
    return db


def grant_sidecar_reads(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        path = Path(str(database) + suffix)
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid():
                raise ValueError("Unexpected SQLite sidecar ownership or file type")
            subprocess.run(["setfacl", "-m", "u:ocpp-reports:r--", str(path)], check=True)


def notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect("\0" + address[1:] if address.startswith("@") else address)
            channel.sendall(message.encode())


def serve(database: Path, stop: threading.Event, interval: float = 30) -> None:
    with closing(open_reader(database)) as db:
        grant_sidecar_reads(database)
        notify("READY=1")
        while not stop.wait(interval):
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            if db.in_transaction:
                raise RuntimeError("WAL reader must not retain a read transaction")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()
    try:
        context = describe(args.data_dir)
        if args.describe:
            print(json.dumps(context, sort_keys=True))
            return
        if os.geteuid() != context["uid"]:
            raise ValueError("Read-access helper must run as the active CSMS owner")
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_args: stop.set())
        serve(Path(context["database"]), stop)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"CSMS read-access helper failed: {exc}\n")


if __name__ == "__main__":
    main()
