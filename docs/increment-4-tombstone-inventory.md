# Increment 4 — tombstone inventory and compatibility decisions

This increment cleans duplicated implementation while retaining supported external interfaces. Open issue reconciliation occurs **after merge**, not within this PR.

| Candidate | Decision | Evidence / reason |
| --- | --- | --- |
| Audi CSV `profiles.render_audi_csv` vs `reports.csv_profiles.render_audi` | Consolidate formatting primitives, retain both interfaces | Both are reachable from `reports.builder.render`; ISO vs local-space timestamps and different SOC keys represent distinct supported output contracts. Golden tests enforce exact output. |
| Flat module wrappers from Increment 3 | Retain | Existing public imports and script/tests reference them. Do not remove until cross-repository caller audit. |
| Historical `generate` vs `run` CLI | Retain | Distinct historical-file and warehouse workflows. Deleting one changes documented usage. |
| Historical SQLite schemas and source conversion tool | Retain | Required for audit/rollback and pending GWay-005 field migration; no deletion or automatic cutover. |
| Warehouse/collector replication schema and triggers | Retain | Durable event, transaction and cursor contracts; not tombstones. |
| Old Ansible/systemd configuration | Retain until independently proven unused | Four deployment roles remain supported and should not be changed during code cleanup. |
| Duplicated Audi CSV header, writer setup, timezone conversion | Consolidated | Moved into `reports.audi_common`; no public output or CLI change. |

## Compatibility gate

- The test suite includes exact CRLF-separated Audi CSV golden output for *both* layouts, preserving distinct SOC fields, location mapping, and timestamp formatting.
- Maintain all four Ansible installation roles, Python 3.11 and 3.13, import aliases and the CSMS export contract.
- No SQLite rewrite, evidence deletion, forwarder cursor reset, schema upgrade, or production migration is authorized by this PR.
- Any further tombstone removal requires proven absence of external callers, not just internal search absence.

## Next action after merge

Review open issues #1, #18, #36, #58, #79, #85 and #88 individually against actual implementation and field evidence. Close/update them in a separate step after the Increment 4 PR is merged, as requested.
