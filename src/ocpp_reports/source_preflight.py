"""Read-only synchronization check on coordinated offline migration snapshots.

Success verifies replication history only; it does not authorize a live cutover.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3


def check_snapshots(csms: Path, warehouse: Path, forwarder: Path, collector: Path,
                    old: str, new: str, node: str) -> dict:
    """Require fully drained, identical retained histories before a rehearsal.

    Inputs must be independently captured while producers and consumers are
    stopped. SQLite read transactions cannot establish a cross-host snapshot.
    Missing receipts or pruned history require separate operator review.
    """
    if not old or not new or old == new or not node or old == node:
        raise ValueError("Distinct nonempty evidence IDs and stable node ID required")
    paths = [csms, warehouse, forwarder, collector]
    if any(not path.is_file() for path in paths):
        raise ValueError("All four database snapshots must exist")
    with ExitStack() as stack:
        databases = []
        for path in paths:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            stack.callback(db.close)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Snapshot integrity check failed")
            databases.append(db)
        source, local, state, central = databases
        if source.execute("SELECT value FROM appliance_metadata WHERE key='source_id'").fetchall() != [(old,)]:
            raise ValueError("CSMS identity does not match expected old ID")
        sources = local.execute("SELECT source_id,cursor FROM sources").fetchall()
        if len(sources) != 1 or sources[0][0] != old:
            raise ValueError("Warehouse must contain exactly the expected old source")
        if state.execute("SELECT evidence_source FROM source_bindings WHERE node_id=?", (node,)).fetchall() != [(old,)]:
            raise ValueError("Missing or conflicting durable source binding")
        if state.execute("SELECT 1 FROM forward_cursors WHERE source_id=?", (old,)).fetchone():
            raise ValueError("Legacy source-keyed forwarding cursor requires review")
        ack = state.execute("SELECT sequence FROM forward_cursors WHERE source_id=?", (node,)).fetchone()
        receipt = central.execute("SELECT sequence FROM collector_cursors WHERE source_id=?", (node,)).fetchone()
        if ack is None or receipt is None:
            raise ValueError("Existing forwarder ACK and collector receipt required")
        for table in ("collector_cursors", "collector_changes", "collector_latest"):
            if central.execute(f"SELECT 1 FROM {table} WHERE source_id=? LIMIT 1", (old,)).fetchone():
                raise ValueError("Collector contains legacy evidence identity; collision review required")
        local_rows = local.execute(
            "SELECT sequence,kind,record_key,payload FROM replication_changes WHERE source_id=? ORDER BY sequence",
            (old,),
        ).fetchall()
        remote_rows = central.execute(
            "SELECT sequence,kind,record_key,payload FROM collector_changes WHERE source_id=? ORDER BY sequence",
            (node,),
        ).fetchall()
        tip = local_rows[-1][0] if local_rows else 0
        if not isinstance(ack[0], int) or ack[0] < 0 or ack != receipt or ack[0] != tip:
            raise ValueError("Warehouse tip, durable ACK and collector cursor must agree; drain pending changes")
        # Compare parsed payloads: collector canonicalizes JSON on receipt.
        def normalized(rows):
            return [(seq, kind, key, json.loads(payload)) for seq, kind, key, payload in rows]
        if normalized(local_rows) != normalized(remote_rows):
            raise ValueError("Collector history differs from warehouse; missing or conflicting receipts")
        return {"status": "snapshots_synchronized", "old_source_id": old,
                "new_source_id": new, "node_id": node, "source_cursor": sources[0][1],
                "replication_sequence": tip, "forwarder_ack": ack[0],
                "collector_cursor": receipt[0], "retained_changes": len(local_rows),
                "cutover_authorized": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("csms", "warehouse", "forwarder", "collector"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("old", "new", "node"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        report = check_snapshots(**vars(args))
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Snapshot preflight failed: {exc}\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
