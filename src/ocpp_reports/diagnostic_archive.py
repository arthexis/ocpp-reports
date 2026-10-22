"""Immutable diagnostic archive transport, indexing, and explicit publication."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
DEFAULT_ARCHIVE = Path("/var/lib/ocpp-reports/diagnostics")
PUBLIC = Path("/var/lib/ocpp-reports/diagnostics-public")
SNIPPETS = Path("/etc/nginx/ocpp-reports-diagnostics")
CREDENTIALS = Path("/etc/ocpp-reports/diagnostics-auth")


def validate(source: str, cp: str) -> None:
    if not SOURCE.fullmatch(source) or not CP.fullmatch(cp) or source in {".", ".."}:
        raise ValueError("Invalid source or charge point identity")


def connection(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "index.sqlite3")
    db.execute("""CREATE TABLE IF NOT EXISTS diagnostics (
        source TEXT NOT NULL, cp TEXT NOT NULL, digest TEXT NOT NULL,
        size INTEGER NOT NULL, original_name TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY(source, cp, digest))""")
    return db


def store(root: Path, source: str, cp: str, name: str, stream, *, maximum: int = 100 * 1024 * 1024) -> dict:
    validate(source, cp)
    if not isinstance(name, str) or not name or len(name) > 255:
        raise ValueError("Invalid diagnostic filename")
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".incoming-", dir=root)
    size, sha = 0, hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise ValueError("Diagnostic file exceeds maximum size")
                sha.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        digest = sha.hexdigest()
        dest = root / source / cp / (digest + ".bin")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            os.replace(tmp, dest)
        with connection(root) as db:
            db.execute("INSERT OR IGNORE INTO diagnostics VALUES (?,?,?,?,?,?)",
                       (source, cp, digest, size, name,
                        datetime.now(timezone.utc).isoformat()))
        return {"source_id": source, "cp": cp, "sha256": digest, "bytes": size}
    finally:
        Path(tmp).unlink(missing_ok=True)


def inventory(root: Path, source: str | None = None, cp: str | None = None) -> list[dict]:
    if not (root / "index.sqlite3").is_file():
        return []
    sql = "SELECT source, cp, digest, size, original_name, received_at FROM diagnostics WHERE 1=1"
    values = []
    if source:
        sql += " AND source=?"
        values.append(source)
    if cp:
        sql += " AND cp=?"
        values.append(cp)
    with sqlite3.connect(f"file:{(root / 'index.sqlite3').resolve()}?mode=ro", uri=True) as db:
        return [dict(zip(("source_id", "cp", "sha256", "bytes", "filename", "received_at"), row))
                for row in db.execute(sql + " ORDER BY received_at DESC", values)]


def local_uploads(data_dir: Path, uploads_root: Path):
    """Discover only attributed, regular CSMS uploads without accessing CSMS SQLite."""
    for record in (data_dir / "diagnostics-requests").glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{40}", record.stem) or record.is_symlink():
            continue
        try:
            entry = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cp = entry.get("charger")
        if not isinstance(cp, str) or not CP.fullmatch(cp):
            continue
        folder = uploads_root / record.stem
        if not folder.is_dir():
            folder = uploads_root / "retained" / record.stem
        if not folder.is_dir() or folder.is_symlink():
            continue
        for item in folder.iterdir():
            if item.is_file() and not item.is_symlink():
                yield cp, item


def forward_archives(data_dir: Path, uploads_root: Path, state: Path, endpoint: str,
                     source: str, token: str) -> int:
    """Best-effort independent file lane; never touches the replication cursor."""
    import httpx
    if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1:"):
        raise ValueError("Archive forwarder requires HTTPS")
    if not token:
        raise ValueError("Archive forwarder requires a bearer token")
    validate(source, "probe")
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "diagnostics.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS delivered (source TEXT, cp TEXT, sha TEXT, PRIMARY KEY(source, cp, sha))")
        sent = 0
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            for cp, path in local_uploads(data_dir, uploads_root):
                sha = hashlib.sha256()
                with path.open("rb") as file:
                    while chunk := file.read(1024 * 1024):
                        sha.update(chunk)
                digest = sha.hexdigest()
                if db.execute("SELECT 1 FROM delivered WHERE source=? AND cp=? AND sha=?", (source, cp, digest)).fetchone():
                    continue
                with path.open("rb") as file:
                    response = client.post(endpoint, params={"source_id": source, "cp": cp, "filename": path.name},
                                           content=file, headers={"Authorization": "Bearer " + token,
                                           "Content-Type": "application/octet-stream"})
                response.raise_for_status()
                if response.json().get("sha256") != digest:
                    raise ValueError("Collector diagnostic acknowledgement mismatch")
                db.execute("INSERT OR IGNORE INTO delivered VALUES (?,?,?)", (source, cp, digest))
                db.commit()
                sent += 1
        return sent


def publish(root: Path, cp: str, source: str | None = None) -> dict:
    """Explicit static snapshot. nginx site and TLS are managed by Reports deployment."""
    if not CP.fullmatch(cp):
        raise ValueError("Invalid charge point ID")
    items = inventory(root, source=source, cp=cp)
    sources = {item["source_id"] for item in items}
    if len(sources) > 1 and source is None:
        raise ValueError("Multiple sources use this CP; specify --source")
    if not items:
        raise ValueError("No collected diagnostics for this charger")
    if os.geteuid() != 0:
        raise PermissionError("Publishing requires root")
    import grp
    group = grp.getgrnam("www-data").gr_gid
    PUBLIC.mkdir(parents=True, exist_ok=True)
    SNIPPETS.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    hashed = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n",
                            text=True, capture_output=True, check=True).stdout.strip()
    staged = Path(tempfile.mkdtemp(prefix=".staged-", dir=PUBLIC))
    try:
        os.chmod(staged, 0o750)
        os.chown(staged, 0, group)
        for n, item in enumerate(items, 1):
            candidate = root / item["source_id"] / cp / (item["sha256"] + ".bin")
            dest = staged / f"diagnostic-{n:03d}.bin"
            shutil.copyfile(candidate, dest)
            os.chmod(dest, 0o640)
            os.chown(dest, 0, group)
        auth = CREDENTIALS / (cp + ".htpasswd")
        snippet = SNIPPETS / (cp + ".conf")
        auth.write_text(f"{cp}:{hashed}\n")
        os.chmod(auth, 0o640)
        os.chown(auth, 0, group)
        snippet.write_text(
            f"location ^~ /{cp}/ {{\n"
            f"    alias {PUBLIC}/{cp}/;\n"
            "    autoindex on;\n"
            '    auth_basic "Diagnostics";\n'
            f"    auth_basic_user_file {auth};\n"
            "    limit_except GET { deny all; }\n"
            '    add_header Cache-Control "private, no-store" always;\n'
            "}\n")
        previous = PUBLIC / cp
        if previous.exists():
            shutil.rmtree(previous)
        staged.rename(previous)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        return {"cp": cp, "source": next(iter(sources)), "username": cp, "password": password, "files": len(items)}
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def revoke(cp: str) -> None:
    if not CP.fullmatch(cp):
        raise ValueError("Invalid charge point ID")
    if os.geteuid() != 0:
        raise PermissionError("Revocation requires root")
    (CREDENTIALS / (cp + ".htpasswd")).unlink(missing_ok=True)
    (SNIPPETS / (cp + ".conf")).unlink(missing_ok=True)
    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True)
    shutil.rmtree(PUBLIC / cp, ignore_errors=True)
