# Private capture configuration migration

This is **not** an Ansible deployment task. The capture helper reads an
operator-owned file at `~/.config/ocpp-dashboard/integrated-accounts.json`;
Reports' Ansible `ansible/tasks/dashboard-instances.yml` manages the distinct
server-side `/etc/ocpp-reports/dashboards.json`. Do not copy login secrets into
that inventory, Git, Ansible logs, or a command line.

Before deploying the new capture helper, the operator must create a separate
owner-only (`0600`) JSON metadata file whose `sites`-equivalent top-level map
has **exactly the same keys** as the private account file. Each site needs:
`origin` (HTTPS URL), `title` (expected visible heading), and `source_ids`
(nonempty exact source allowlist). Derive these from the authoritative installed
dashboard inventory and validate against the authenticated endpoint; do not
infer source IDs from customer names. The metadata input is a direct mapping:

```json
{"tenant-a":{"origin":"https://tenant-a.example.test","title":"Tenant A","source_ids":["source-a"]}}
```

Run `python3 scripts/migrate_accounts.py --accounts PATH --metadata PATH` for
a read-only preview. It prints only `change_required`, `already_current`, or
`blocked:<error-class>`. The tool rejects missing tenants, conflicting scopes,
invalid metadata, non-owner files and permissions other than `0600`.

**Only after separate deployment authorization**, add `--apply` to atomically
replace the account file while preserving credentials and mode `0600`. Repeat
the command to verify `already_current`; then run capture's local account/scope
checks before any publication. No secret values, origins or site IDs are logged.
Do not deploy the new capture helper before this migration. The older installed
launcher still has a fixed site list and must be retired only after acceptance.
