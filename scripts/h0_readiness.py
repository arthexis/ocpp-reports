#!/usr/bin/env python3
"""H0 offline acceptance: verify manifests against a declared, reviewable policy.

No legacy application imports; no database writes. Unknown semantics block readiness.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CSMS_MINIMUM = {"events"}
ARTHEXIS_MINIMUM = {"ocpp_transaction"}


def evaluate(inventory: dict, fixtures: list[dict], policy: dict) -> dict:
    findings = []
    artifacts = inventory.get("artifacts", [])
    by_hash = {a["sha256"]: a for a in artifacts}
    if not artifacts:
        findings.append("No inventory artifacts")
    for a in artifacts:
        tables = a.get("tables", {})
        if a.get("integrity") != "ok":
            findings.append(f"{a.get('artifact', '?')}: integrity failed")
        if not (CSMS_MINIMUM <= tables.keys() or ARTHEXIS_MINIMUM <= tables.keys()):
            findings.append(f"{a.get('artifact', '?')}: unrecognized source signature")
    for f in fixtures:
        digest = f.get("artifact_sha256")
        if digest not in by_hash:
            findings.append("Fixture has no matching inventory SHA-256")
            continue
        a = by_hash[digest]
        if f.get("user_version") != a.get("user_version"):
            findings.append(f"{a['artifact']}: schema version differs")
        for table, rows in f.get("rows", {}).items():
            if table not in a.get("tables", {}):
                findings.append(f"{a['artifact']}: fixture table not inventoried: {table}")
            elif len(rows) > a["tables"][table]["row_count"]:
                findings.append(f"{a['artifact']}: sample larger than source count: {table}")
    # Explicit assertions are required: never derive units or timezone from a
    # numeric value or a timestamp string.
    for a in artifacts:
        digest = a["sha256"]
        assertion = policy.get("sources", {}).get(digest)
        if not assertion:
            findings.append(f"{a['artifact']}: no reviewed source policy")
            continue
        if assertion.get("lineage_id") in (None, ""):
            findings.append(f"{a['artifact']}: missing lineage_id")
        if assertion.get("timezone") in (None, "", "unknown"):
            findings.append(f"{a['artifact']}: unresolved timezone")
        if assertion.get("energy_unit") not in ("Wh", "kWh", "not_applicable"):
            findings.append(f"{a['artifact']}: unresolved energy unit")
        if not assertion.get("evidence"):
            findings.append(f"{a['artifact']}: missing evidence citation")
    # Nested snapshots may share a lineage; count records once per unique
    # event identity at adapter stage, never sum the inventory counts.
    lineages = {}
    for a in artifacts:
        p = policy.get("sources", {}).get(a["sha256"], {})
        lineage = p.get("lineage_id")
        if lineage:
            lineages.setdefault(lineage, []).append(a["sha256"])
    return {
        "format": "h0-readiness/v1",
        "ready": not findings and bool(fixtures),
        "findings": findings + ([] if fixtures else ["No sanitized fixtures"]),
        "lineages": {k: {"snapshot_count": len(v), "requires_record_level_dedup": len(v) > 1}
                     for k, v in sorted(lineages.items())},
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", required=True, type=Path)
    p.add_argument("--policy", required=True, type=Path)
    p.add_argument("--fixtures", nargs="+", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    def load(path):
        return json.loads(path.read_text(encoding="utf-8"))
    result = evaluate(load(args.inventory), [load(f) for f in args.fixtures], load(args.policy))
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(result, out, indent=2, sort_keys=True)
        out.write("\n")
    if not result["ready"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
