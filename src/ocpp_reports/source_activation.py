"""Activate a source identity conversion while all local writers are stopped.

This is a local operation. It never starts services or contacts a collector.
The private backup directory is the rollback boundary until forwarding starts.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from pathlib import Path

from .source_migration import migrate_copies


SERVICES = (
    "ocpp-csms.service", "ocpp-reports-ingest.service",
    "ocpp-reports-csms-reader.service", "ocpp-reports-forwarder.service",
)


def require_stopped() -> None:
    for service in SERVICES:
        result = subprocess.run(("systemctl", "is-active", "--quiet", service),
                                check=False)
        if result.returncode == 0:
            raise ValueError(f"{service} must be stopped before source activation")


def backup_sqlite(source: Path, target: Path) -> None:
    """Use SQLite backup to include committed WAL rows in a standalone file."""
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as backup:
            original.backup(backup)
    os.chmod(target, 0o600)
    with sqlite3.connect(target) as check:
        if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError(f"Backup integrity failed: {source}")


def signature(path: Path) -> tuple:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return (db.execute("PRAGMA user_version").fetchone()[0],
                tuple(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                 "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")))


def verify_candidate(original: Path, candidate: Path, old: str, new: str,
                     kind: str) -> None:
    if signature(original) != signature(candidate):
        raise ValueError(f"{kind} schema or user_version changed")
    with sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True) as left, \
            sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True) as right:
        if right.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError(f"{kind} candidate integrity failed")
        tables = [row[0] for row in left.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in left.execute(f"PRAGMA table_info({quoted})")]
            source_column = columns.index("source_id") if "source_id" in columns else None
            before = []
            for row in left.execute(f"SELECT * FROM {quoted}"):
                values = list(row)
                if kind == "csms" and table == "appliance_metadata" and values[0] == "source_id":
                    if values[1] != old:
                        raise ValueError("Unexpected CSMS source identity")
                    values[1] = new
                elif source_column is not None and values[source_column] == old:
                    values[source_column] = new
                elif kind == "forwarder" and table == "source_bindings" and values[1] == old:
                    values[1] = new
                before.append(tuple(values))
            after = list(right.execute(f"SELECT * FROM {quoted}"))
            if sorted(map(repr, before)) != sorted(map(repr, after)):
                raise ValueError(f"{kind}.{table} contains unexpected changes")


def _stage(candidate: Path, destination: Path, ownership_source: Path) -> Path:
    staged = destination.with_name(destination.name + ".source-activation-new")
    if staged.exists():
        raise ValueError(f"Staging path already exists: {staged}")
    backup_sqlite(candidate, staged)
    info = (destination if destination.exists() else ownership_source).stat()
    os.chown(staged, info.st_uid, info.st_gid)
    os.chmod(staged, (info.st_mode & 0o777) if destination.exists() else 0o660)
    return staged


def activate(csms: Path, warehouse: Path, forwarder: Path, backup_dir: Path,
             old: str, new: str, node: str) -> dict:
    require_stopped()
    if not old or not new or old == new or new != node:
        raise ValueError("Distinct old/new identities and matching new node required")
    if backup_dir.exists():
        raise ValueError("Backup directory already exists")
    if not csms.is_file() or not warehouse.is_file() or forwarder.exists():
        raise ValueError("Expected CSMS and warehouse files and no prior forwarder state")
    if any(path.is_symlink() for path in (csms, warehouse, forwarder)):
        raise ValueError("Database symlinks are not supported")
    with sqlite3.connect(csms.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM transactions WHERE stopped_at IS NULL").fetchone()[0]:
            raise ValueError("Active CSMS transactions block source activation")
    backup_dir.mkdir(parents=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    originals = {"csms": csms, "warehouse": warehouse}
    stages: dict[str, Path] = {}
    touched: list[str] = []
    try:
        for kind, path in originals.items():
            backup_sqlite(path, backup_dir / f"{kind}.sqlite3")
        state = backup_dir / "forwarder-empty.sqlite3"
        with sqlite3.connect(state) as db:
            db.executescript("CREATE TABLE forward_cursors(source_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL);"
                             "CREATE TABLE source_bindings(node_id TEXT PRIMARY KEY, evidence_source TEXT NOT NULL);")
            db.execute("INSERT INTO source_bindings VALUES (?,?)", (node, old))
        os.chmod(state, 0o600)
        converted = migrate_copies(backup_dir / "csms.sqlite3",
                                   backup_dir / "warehouse.sqlite3", state,
                                   backup_dir / "converted", old, new, node)
        candidates = {kind: Path(path) for kind, path in converted.items()}
        for kind, original in {**originals, "forwarder": state}.items():
            verify_candidate(original if kind == "forwarder" else backup_dir / f"{kind}.sqlite3",
                             candidates[kind], old, new, kind)
        targets = {**originals, "forwarder": forwarder}
        for kind, target in targets.items():
            stages[kind] = _stage(candidates[kind], target, warehouse)
        # SQLite backup includes WAL content. Remove old sidecars only after all
        # backups and candidate checks pass, while services remain stopped.
        require_stopped()
        for kind, target in targets.items():
            touched.append(kind)
            for suffix in ("-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            os.replace(stages[kind], target)
        return {"backup_dir": str(backup_dir), "source_id": new,
                "converted": tuple(touched)}
    except Exception:
        # Keep services stopped. Restore every replaced database from the
        # verified SQLite backups, including the first replacement on failure.
        for kind in reversed(touched):
            target = {**originals, "forwarder": forwarder}[kind]
            if kind == "forwarder":
                target.unlink(missing_ok=True)
            else:
                recovery = target.with_name(target.name + ".source-activation-rollback")
                backup_sqlite(backup_dir / f"{kind}.sqlite3", recovery)
                info = target.stat()
                os.chown(recovery, info.st_uid, info.st_gid)
                os.chmod(recovery, info.st_mode & 0o777)
                os.replace(recovery, target)
        raise
    finally:
        for staged in stages.values():
            staged.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("csms", "warehouse", "forwarder", "backup-dir", "old", "new", "node"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    print(activate(Path(args.csms), Path(args.warehouse), Path(args.forwarder),
                   Path(args.backup_dir), args.old, args.new, args.node))


if __name__ == "__main__":
    main()
