"""Offline-only source-ID conversion on copies; never modifies installed databases.

Coordinate CSMS node.toml, credentials and collector cursors in a maintenance window.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


WAREHOUSE_TABLES = (
    "sources", "events", "energy_samples", "current_transactions",
    "transaction_observations", "connector_state", "replication_changes",
)


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def migrate_copies(csms: Path, warehouse: Path, forwarder: Path, output: Path,
                   old: str, new: str, node: str) -> dict:
    """Create checked candidate DBs; operator controls cutover/rollback separately."""
    if not old or not new or old == new or not node:
        raise ValueError("Distinct nonempty old/new source IDs and node ID required")
    sources = {"csms": csms, "warehouse": warehouse, "forwarder": forwarder}
    if any(not p.is_file() for p in sources.values()):
        raise ValueError("All source databases must exist")
    if output.exists():
        raise ValueError("Output directory must not exist (no overwrite)")
    output.mkdir(parents=True)
    candidates = {}
    try:
        for name, source in sources.items():
            dest = output / (name + ".sqlite3")
            with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as original:
                with sqlite3.connect(dest) as target:
                    original.backup(target)
            candidates[name] = dest
        with sqlite3.connect(candidates["csms"]) as db:
            if "appliance_metadata" not in _tables(db):
                raise ValueError("Unsupported CSMS schema")
            value = db.execute(
                "SELECT value FROM appliance_metadata WHERE key='source_id'"
            ).fetchone()
            if value != (old,):
                raise ValueError("CSMS identity does not match expected old ID")
            db.execute(
                "UPDATE appliance_metadata SET value=? WHERE key='source_id' AND value=?",
                (new, old),
            )
        with sqlite3.connect(candidates["warehouse"]) as db:
            present = _tables(db)
            if "sources" not in present:
                raise ValueError("Warehouse lacks sources table")
            if db.execute("SELECT 1 FROM sources WHERE source_id=?", (new,)).fetchone():
                raise ValueError("Destination identity already exists in warehouse")
            if not db.execute("SELECT 1 FROM sources WHERE source_id=?", (old,)).fetchone():
                raise ValueError("Old source identity absent from warehouse")
            # Remove only replication triggers on the *copy* while updating identity.
            # Restored definitions in sqlite_master are re-created after conversion.
            triggers = [(r[0], r[1]) for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
            )]
            for name, _sql in triggers:
                db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            for table in WAREHOUSE_TABLES:
                if table in present:
                    db.execute(f"UPDATE {table} SET source_id=? WHERE source_id=?", (new, old))
            for _name, sql in triggers:
                db.execute(sql)
        with sqlite3.connect(candidates["forwarder"]) as db:
            tables = _tables(db)
            if "forward_cursors" not in tables:
                raise ValueError("Forwarder cursor table missing")
            if "source_bindings" in tables:
                binding = db.execute(
                    "SELECT evidence_source FROM source_bindings WHERE node_id=?", (node,)
                ).fetchone()
                if binding is not None and binding != (old,):
                    raise ValueError("Forwarder binding conflicts with expected source")
                db.execute("UPDATE source_bindings SET evidence_source=? WHERE node_id=?",
                           (new, node))
            # forward_cursors must remain keyed by node, not CSMS evidence UUID.
            if db.execute("SELECT 1 FROM forward_cursors WHERE source_id=?", (old,)).fetchone():
                raise ValueError("Legacy source-keyed forwarding cursors require separate review")
        for candidate in candidates.values():
            with sqlite3.connect(candidate) as db:
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError(f"SQLite integrity check failed: {candidate}")
        return {key: str(value) for key, value in candidates.items()}
    except Exception:
        # Never expose a partially converted directory as a successful candidate.
        for candidate in candidates.values():
            candidate.unlink(missing_ok=True)
        output.rmdir()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("csms", "warehouse", "forwarder", "output", "old", "new", "node"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args()
    print(migrate_copies(*(Path(getattr(args, k)) for k in
                           ("csms", "warehouse", "forwarder", "output")),
                         args.old, args.new, args.node))


if __name__ == "__main__":
    main()
