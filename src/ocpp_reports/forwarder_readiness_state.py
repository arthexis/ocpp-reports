"""Read-only identity evidence for safe Forwarder readiness exemptions."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .forwarder_config import load_forwarder_config


def established_source(state: Path, source_id: str) -> bool:
    """Only an acknowledged, durably bound source may bypass an online probe.

    A fresh or changed identity must authenticate with the live collector.
    Missing, malformed, or unreadable state fails closed without modifying it.
    """
    if not state.is_file() or state.is_symlink():
        return False
    try:
        with sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True) as db:
            cursor = db.execute(
                "SELECT sequence FROM forward_cursors WHERE source_id=?", (source_id,)
            ).fetchone()
            binding = db.execute(
                "SELECT evidence_source FROM source_bindings WHERE node_id=?", (source_id,)
            ).fetchone()
            return (
                cursor is not None and type(cursor[0]) is int and cursor[0] >= 0
                and binding is not None and isinstance(binding[0], str)
                and bool(binding[0])
            )
    except (sqlite3.Error, OSError, ValueError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/ocpp-reports/forwarder.sqlite"))
    parser.add_argument("--config", type=Path, default=Path("/etc/ocpp-reports/forwarder.toml"))
    args = parser.parse_args()
    try:
        source_id = load_forwarder_config(args.config)["source_id"]
    except (OSError, ValueError):
        raise SystemExit(1)
    raise SystemExit(0 if established_source(args.state, source_id) else 1)


if __name__ == "__main__":
    main()
