# Source identity activation — capability and maintenance guide (#105)

This is an explicit operator maintenance action, not part of normal deploy or restart.

| Role | Local CSMS | Forwarder | Identity plan | Activation |
|---|---|---|---|---|
| satellite | Required | Optional (fresh or preserved) | Yes | Supervised, guarded |
| control | Required | No | Yes | Explicit stopped-writer activation; no forwarder state created |
| watchtower | Optional local CSMS | No | Yes when local producer and warehouse paths are supplied | Supervised, guarded |
| terminal | No local producer | No | Reject | Reject |

Run the read-only inventory first, supplying actual DB paths:

```sh
python -m ocpp_reports.source_activation_plan \
  --role satellite --csms CSMS_DB --warehouse REPORTS_DB \
  --forwarder FORWARDER_DB --old ORIGINAL_ID --new CANONICAL_NODE_ID \
  --node CANONICAL_NODE_ID
```

Output deliberately excludes payloads, tokens and customer endpoints. The reported maintenance and collector gates are **not authorizations**. A new/absent forwarder state is not proof that an external collector is empty: independently inspect the collector before first forwarding.

For established forwarding, acquire **coordinated drained offline SQLite snapshots** of local CSMS, Reports warehouse, forwarder state and collector, and prove identical retained histories using `source_preflight`. The activation helper takes `--collector-snapshot` in this mode, preserves the node-keyed ACK and converted binding, and refuses missing or mismatched collector evidence. The collector snapshot must be acquired by its own local agent; satellites must not receive Watchtower administration access. This is narrower than asynchronous live receipts, which cannot prove the exact rollback boundary.

Before apply: obtain explicit operator maintenance approval, verify no active charging, stop the relevant CSMS/Reports writers, take WAL-consistent backups and ensure staged paths are clear. On a Watchtower with local ingestion, stop its collector and dashboard too while replacing the shared warehouse; remote forwarders will retry. The activation tool never starts services or releases forwarding. Keep its backup directory private and preserve the original files. Inspect `/etc/ocpp-csms/node.toml` and installed producer identity independently: this tool does not edit that config. Verify schema versions, row counts, configuration and producer export after supervised restart. The forwarding service must remain held until collector readiness and a coordinator release.

**Recovery:** If activation fails synchronously, the command attempts restoring backed-up files and leaves services stopped. A durable `recovery.json` records the installation phase, target paths, and previously verified backups before file replacements. If a process kill or machine crash occurs mid-activation, keep services stopped and use `python -m ocpp_reports.source_activation --recover` with the original mandatory path/identity arguments and `--backup-dir` to explicitly restore local backups. A completed conversion refuses implicit recovery: rollback after forwarding resumes is a separate coordinated collector operation. Inspect source identity and `node.toml` before restarting services. Never restart forwarding automatically.
