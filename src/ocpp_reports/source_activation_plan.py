"""Read-only capability inventory for explicit source-identity maintenance."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


ROLE_CAPABILITIES = {
    "satellite": {"local_producer": True, "forwarder": True, "collector": False},
    "control": {"local_producer": True, "forwarder": False, "collector": True},
    "watchtower": {"local_producer": True, "forwarder": False, "collector": True},
    "terminal": {"local_producer": False, "forwarder": False, "collector": False},
}


def _database(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Missing or symlinked database artifact")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def inventory(role: str, csms: Path | None, warehouse: Path | None,
              forwarder: Path | None, old: str, new: str, node: str) -> dict:
    """No files or databases created; refuse unsupported or ambiguous states."""
    if role not in ROLE_CAPABILITIES:
        raise ValueError("Unknown Reports role")
    cap = ROLE_CAPABILITIES[role]
    if not cap["local_producer"]:
        raise ValueError(f"{role} has no local CSMS producer identity to migrate")
    if not old or not new or not node or old == new or new != node:
        raise ValueError("Require distinct original and canonical identities")
    if csms is None or warehouse is None:
        raise ValueError("Local producer activation requires CSMS and warehouse paths")
    with _database(csms) as source, _database(warehouse) as local:
        original = source.execute(
            "SELECT value FROM appliance_metadata WHERE key='source_id'"
        ).fetchone()
        if original != (old,):
            raise ValueError("Actual CSMS identity does not match requested source")
        sources = local.execute("SELECT source_id,cursor FROM sources").fetchall()
        if len(sources) != 1 or sources[0][0] != old:
            raise ValueError("Warehouse source identity ambiguous or mismatched")
        active = source.execute(
            "SELECT COUNT(*) FROM transactions WHERE stopped_at IS NULL"
        ).fetchone()[0]
        tip = local.execute("SELECT COALESCE(MAX(sequence),0) FROM replication_changes").fetchone()[0]
        versions = [db.execute("PRAGMA user_version").fetchone()[0] for db in (source, local)]
        counts = {
            "csms_events": source.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "warehouse_changes": local.execute("SELECT COUNT(*) FROM replication_changes").fetchone()[0],
        }
    mode = "fresh"
    binding = None
    ack = None
    if not cap["forwarder"] and forwarder is not None and forwarder.exists():
        raise ValueError("Unexpected forwarder state for this role")
    if forwarder is not None and forwarder.exists():
        if not cap["forwarder"]:
            raise ValueError("Unexpected forwarder state on non-forwarding role")
        with _database(forwarder) as state:
            tables = {row[0] for row in state.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "source_bindings" not in tables or "forward_cursors" not in tables:
                raise ValueError("Unrecognized forwarding state; inspection required")
            binding = state.execute(
                "SELECT evidence_source FROM source_bindings WHERE node_id=?", (node,)
            ).fetchone()
            ack_row = state.execute(
                "SELECT sequence FROM forward_cursors WHERE source_id=?", (node,)
            ).fetchone()
            if binding != (old,):
                raise ValueError("Existing binding does not match original source")
            ack = ack_row[0] if ack_row else None
            # Any persisted state warrants collector verification; a missing ACK
            # is not proof the central collector has never received batches.
            mode = "existing-history"
            if ack is not None:
                if not isinstance(ack, int) or not 0 <= ack <= tip:
                    raise ValueError("Invalid persisted forwarding cursor")
    elif cap["forwarder"] and forwarder is None:
        raise ValueError("Satellite requires explicit forwarder state path")
    return {
        "role": role, "old_source_id": old, "new_source_id": new,
        "node_id": node, "mode": mode, "csms_user_version": versions[0],
        "warehouse_user_version": versions[1], "source_cursor": sources[0][1],
        "feed_tip": tip, "forwarder_ack": ack, "forwarder_binding": binding[0] if binding else None,
        "active_transactions": active, "counts": counts,
        "apply_allowed": active == 0 and mode == "fresh" and role in ("satellite", "control", "watchtower"),
        "requires_collector_preflight": mode == "existing-history",
        "requires_maintenance_authorization": True,
        "forwarding_release_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=tuple(ROLE_CAPABILITIES))
    for name in ("csms", "warehouse", "forwarder"):
        parser.add_argument("--" + name, type=Path)
    for name in ("old", "new", "node"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        report = inventory(**vars(args))
    except (sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Source activation plan blocked: {exc}\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
