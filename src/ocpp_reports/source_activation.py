"""Activate a source identity conversion while all local writers are stopped.

This is a local operation. It never starts services or contacts a collector.
The private backup directory is the rollback boundary until forwarding starts.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .source_migration import migrate_copies
from .source_activation_plan import inventory
from .source_preflight import check_snapshots


SERVICES = (
    "ocpp-csms.service", "ocpp-reports-ingest.service",
    "ocpp-reports-csms-reader.service", "ocpp-reports-forwarder.service",
    "ocpp-reports-collector.service", "ocpp-reports-dashboard.service",
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



def _journal(directory: Path, status: str, targets: dict[str, Path],
             *, touched: list[str] | None = None) -> None:
    """Durable recovery marker. Never store credentials or evidence payloads."""
    path = directory / "recovery.json"
    temporary = directory / "recovery.json.tmp"
    payload = {"status": status, "targets": {key: str(value) for key, value in targets.items()},
               "replaced": list(touched or [])}
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover(backup_dir: Path) -> dict[str, Any]:
    """Explicit crash recovery; refuse when services are running."""
    require_stopped()
    path = backup_dir / "recovery.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("No protected recovery journal")
    marker = json.loads(path.read_text(encoding="utf-8"))
    if marker["status"] == "completed":
        raise ValueError("Completed conversion requires operator-led rollback")
    if marker["status"] == "recovered":
        return {"status": "recovered", "backup_dir": str(backup_dir)}
    targets = {name: Path(p) for name, p in marker["targets"].items()}
    if set(targets) - {"csms", "warehouse", "forwarder"}:
        raise ValueError("Unexpected recovery target")
    for name in ("csms", "warehouse"):
        if name not in targets or not (backup_dir / f"{name}.sqlite3").is_file():
            raise ValueError("Missing recovery evidence")
    _journal(backup_dir, "recovering", targets, touched=marker.get("replaced", []))
    for name, target in targets.items():
        if target.is_symlink():
            raise ValueError("Recovery target must not be a symlink")
        original = backup_dir / ("forwarder-empty.sqlite3" if name == "forwarder"
                                 else f"{name}.sqlite3")
        if name == "forwarder" and not (backup_dir / "forwarder-existed").exists():
            target.unlink(missing_ok=True)
            continue
        if not original.is_file():
            raise ValueError("Recovery state missing: " + name)
        staged = target.with_name(target.name + ".source-activation-rollback")
        if staged.exists():
            raise ValueError("Unresolved recovery staging file")
        backup_sqlite(original, staged)
        info = target.stat() if target.exists() else original.stat()
        os.chown(staged, info.st_uid, info.st_gid)
        os.chmod(staged, info.st_mode & 0o777)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        os.replace(staged, target)
    _journal(backup_dir, "recovered", targets)
    return {"status": "recovered", "backup_dir": str(backup_dir)}


def activate(csms: Path, warehouse: Path, forwarder: Path, backup_dir: Path,
             old: str, new: str, node: str, *, role: str = "satellite",
             collector_snapshot: Path | None = None) -> dict:
    # Read-only capability check runs before any mutations or backup creation.
    plan = inventory(role, csms, warehouse, forwarder, old, new, node)
    if plan["active_transactions"]:
        raise ValueError("Active CSMS transactions block source activation")
    if plan["requires_collector_preflight"] and collector_snapshot is None:
        raise ValueError("Existing forwarding state requires coordinated collector snapshot")
    require_stopped()
    if not old or not new or old == new or new != node:
        raise ValueError("Distinct old/new identities and matching new node required")
    if backup_dir.exists():
        raise ValueError("Backup directory already exists")
    if not csms.is_file() or not warehouse.is_file():
        raise ValueError("Missing CSMS or warehouse")
    if role not in ("satellite", "control", "watchtower"):
        raise ValueError("This role has no locally activatable producer")
    if role in ("control", "watchtower") and forwarder.exists():
        raise ValueError("Non-forwarding producer activation cannot use a forwarder state")
    if any(path.is_symlink() for path in (csms, warehouse, forwarder)):
        raise ValueError("Database symlinks are not supported")
    with sqlite3.connect(csms.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM transactions WHERE stopped_at IS NULL").fetchone()[0]:
            raise ValueError("Active CSMS transactions block source activation")
    backup_dir.mkdir(parents=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    originals = {"csms": csms, "warehouse": warehouse}
    targets = {**originals, **({"forwarder": forwarder} if role == "satellite" else {})}
    stages: dict[str, Path] = {}
    touched: list[str] = []
    try:
        for kind, path in originals.items():
            backup_sqlite(path, backup_dir / f"{kind}.sqlite3")
        state = backup_dir / "forwarder-empty.sqlite3"
        if forwarder.exists():
            (backup_dir / "forwarder-existed").touch(mode=0o600)
        if role == "satellite" and forwarder.exists():
            backup_sqlite(forwarder, state)
            # Compare the actual durable acknowledgement against independently
            # captured collector history before allowing any state-preserving conversion.
            check_snapshots(backup_dir / "csms.sqlite3",
                            backup_dir / "warehouse.sqlite3", state,
                            collector_snapshot, old, new, node)
        else:
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
        for kind, target in targets.items():
            stages[kind] = _stage(candidates[kind], target, warehouse)
        # SQLite backup includes WAL content. Remove old sidecars only after all
        # backups and candidate checks pass, while services remain stopped.
        require_stopped()
        _journal(backup_dir, "installing", targets)
        for kind, target in targets.items():
            _journal(backup_dir, "installing", targets, touched=touched + [kind])
            touched.append(kind)
            for suffix in ("-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            os.replace(stages[kind], target)
        _journal(backup_dir, "completed", targets, touched=touched)
        return {"backup_dir": str(backup_dir), "source_id": new,
                "converted": tuple(touched)}
    except Exception:
        # Keep services stopped. Restore every replaced database from the
        # verified SQLite backups, including the first replacement on failure.
        for kind in reversed(touched):
            target = targets[kind]
            if kind == "forwarder":
                if plan["mode"] == "existing-history":
                    recovery = target.with_name(target.name + ".source-activation-rollback")
                    backup_sqlite(backup_dir / "forwarder-empty.sqlite3", recovery)
                    info = target.stat()
                    os.chown(recovery, info.st_uid, info.st_gid)
                    os.chmod(recovery, info.st_mode & 0o777)
                    os.replace(recovery, target)
                else:
                    target.unlink(missing_ok=True)
            else:
                recovery = target.with_name(target.name + ".source-activation-rollback")
                backup_sqlite(backup_dir / f"{kind}.sqlite3", recovery)
                info = target.stat()
                os.chown(recovery, info.st_uid, info.st_gid)
                os.chmod(recovery, info.st_mode & 0o777)
                os.replace(recovery, target)
        if (backup_dir / "recovery.json").exists():
            _journal(backup_dir, "recovered", targets)
        raise
    finally:
        for staged in stages.values():
            staged.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("csms", "warehouse", "forwarder", "backup-dir", "old", "new", "node"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--role", choices=("satellite", "control", "watchtower", "terminal"),
                        default="satellite")
    parser.add_argument("--collector-snapshot", type=Path)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    if args.recover:
        print(recover(Path(args.backup_dir)))
        return
    print(activate(Path(args.csms), Path(args.warehouse), Path(args.forwarder),
                   Path(args.backup_dir), args.old, args.new, args.node,
                   role=args.role, collector_snapshot=args.collector_snapshot))


if __name__ == "__main__":
    main()
