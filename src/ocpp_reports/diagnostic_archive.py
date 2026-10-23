"""Immutable diagnostic archive transport, indexing, and explicit publication."""
from __future__ import annotations

import hashlib
import html
import zipfile
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


def store(root: Path, source: str, cp: str, name: str, stream, *, maximum: int = 100 * 1024 * 1024, received_at: str | None = None) -> dict:
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
                        received_at or datetime.now(timezone.utc).isoformat()))
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
            if item.name == "upload.bin" and item.is_file() and not item.is_symlink():
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


def _replace_text(path: Path, content: str, *, mode: int, group: int) -> None:
    """Install nginx data atomically, never expose a partially written credential."""
    fd, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(content)
        os.chmod(temporary, mode)
        os.chown(temporary, 0, group)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _reload_nginx() -> None:
    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True)


def _archive_preview(path: Path) -> str:
    """Bounded, escaped view of diagnostics; never execute archive content."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = [m for m in archive.infolist()
                         if not m.is_dir() and m.file_size <= 1024 * 1024
                         and m.filename.lower().endswith((".log", ".txt"))]
                if not names:
                    return "No small text logs available for preview. Use Download."
                return "\n\n".join(
                    f"--- {m.filename} ---\n" +
                    archive.read(m).decode("utf-8", errors="replace")
                    for m in names[:8]
                )[:1024 * 1024]
        if path.stat().st_size <= 1024 * 1024:
            return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        pass
    return "Preview unavailable. Use Download."


def _viewer_page(title: str, body: str, download: str) -> str:
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<meta name='referrer' content='no-referrer'><title>" + html.escape(title) +
            "</title><style>body{font:16px system-ui;margin:2rem;max-width:100%}"
            "pre{font:13px monospace;white-space:pre-wrap;overflow-wrap:anywhere;"
            "background:#111;color:#eee;padding:1rem;border-radius:6px}"
            "a{margin-right:1rem}</style><h1>" + html.escape(title) +
            "</h1><p><a href='./'>Back to files</a><a href='" +
            html.escape(download, quote=True) + "' download>Download original</a></p>"
            "<pre>" + html.escape(body) + "</pre></html>")


def publish(root: Path, cp: str, source: str | None = None) -> dict:
    """Rotate one CP's publication, or restore the previous state on failure."""
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
    if not SNIPPETS.is_dir() or not PUBLIC.is_dir() or not CREDENTIALS.is_dir():
        raise ValueError("Reports diagnostic nginx host is not deployed")
    import grp
    group = grp.getgrnam("www-data").gr_gid
    password = secrets.token_urlsafe(24)
    hashed = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"], input=password + "\n",
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    staged = Path(tempfile.mkdtemp(prefix=".staged-", dir=PUBLIC))
    destination = PUBLIC / cp
    backup = PUBLIC / ("." + cp + ".previous")
    auth = CREDENTIALS / (cp + ".htpasswd")
    snippet = SNIPPETS / (cp + ".conf")
    if backup.exists():
        shutil.rmtree(staged)
        raise ValueError("Unrecovered prior publication snapshot; inspect before retry")
    previous_auth = auth.read_text(encoding="utf-8") if auth.is_file() else None
    previous_snippet = snippet.read_text(encoding="utf-8") if snippet.is_file() else None
    swapped = False
    try:
        os.chmod(staged, 0o750)
        os.chown(staged, 0, group)
        rows = []
        for index, item in enumerate(items, 1):
            candidate = root / item["source_id"] / cp / (item["sha256"] + ".bin")
            with candidate.open("rb") as stream:
                extension = ".zip" if stream.read(4) == b"PK\\x03\\x04" else ".bin"
            target = staged / f"diagnostic-{index:03d}{extension}"
            shutil.copyfile(candidate, target)
            os.chmod(target, 0o640)
            os.chown(target, 0, group)
            label = f"diagnostic-{index:03d}"
            viewer = staged / f"{label}.html"
            viewer.write_text(_viewer_page(label, _archive_preview(candidate), target.name), encoding="utf-8")
            os.chmod(viewer, 0o640)
            os.chown(viewer, 0, group)
            rows.append((viewer.name, target.name, item["received_at"], item["bytes"]))
        listing = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                   "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                   "<meta name='referrer' content='no-referrer'><title>Diagnostics</title>"
                   "<style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse}"
                   "td,th{padding:.6rem;border-bottom:1px solid #aaa;text-align:left}</style>"
                   "<h1>Diagnostic archives</h1><table><tr><th>View</th><th>Received (UTC)</th>"
                   "<th>Bytes</th><th>Download</th></tr>" +
                   "".join("<tr><td><a href='" + html.escape(view, quote=True) + "'>" +
                           html.escape(view.removesuffix(".html")) + "</a></td><td>" +
                           html.escape(when) + "</td><td>" + str(size) +
                           "</td><td><a href='" + html.escape(download, quote=True) +
                           "' download>Download</a></td></tr>"
                           for view, download, when, size in rows) +
                   "</table></html>")
        page = staged / "index.html"
        page.write_text(listing, encoding="utf-8")
        os.chmod(page, 0o640)
        os.chown(page, 0, group)
        if destination.exists():
            destination.rename(backup)
        staged.rename(destination)
        swapped = True
        _replace_text(auth, f"{cp}:{hashed}\n", mode=0o640, group=group)
        _replace_text(snippet, (
            f"location ^~ /{cp}/ {{\n"
            f"    alias {PUBLIC}/{cp}/;\n"
            "    autoindex on;\n"
            '    auth_basic "Diagnostics";\n'
            f"    auth_basic_user_file {auth};\n"
            "    limit_except GET { deny all; }\n"
            '    add_header Cache-Control "private, no-store" always;\n'
            "}\n"
        ), mode=0o644, group=group)
        _reload_nginx()
    except Exception:
        if previous_auth is None:
            auth.unlink(missing_ok=True)
        else:
            _replace_text(auth, previous_auth, mode=0o640, group=group)
        if previous_snippet is None:
            snippet.unlink(missing_ok=True)
        else:
            _replace_text(snippet, previous_snippet, mode=0o644, group=group)
        if swapped and destination.is_dir():
            shutil.rmtree(destination)
        if backup.exists():
            backup.rename(destination)
        try:
            _reload_nginx()
        except Exception:
            pass
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    if backup.exists():
        shutil.rmtree(backup)
    return {
        "cp": cp, "source": next(iter(sources)), "username": cp,
        "password": password, "files": len(items),
    }


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


def import_local(root: Path, data_dir: Path, uploads_root: Path, source: str) -> int:
    """Import historical and new charger uploads without HTTP or CSMS mutations.

    SHA-256 indexed copies are immutable and repeated scans are idempotent.
    """
    validate(source, "probe")
    inserted = 0
    for cp, path in local_uploads(data_dir, uploads_root):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        # Preserve original CSMS receiver time, not the time of central import.
        original_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        if any(item["sha256"] == digest for item in inventory(root, source, cp)):
            # Repair historical imports that accidentally stamped import time.
            with connection(root) as db:
                db.execute("UPDATE diagnostics SET received_at=? WHERE source=? AND cp=? AND digest=?",
                           (original_time, source, cp, digest))
            continue
        with path.open("rb") as stream:
            store(root, source, cp, path.name, stream, received_at=original_time)
        inserted += 1
    return inserted
