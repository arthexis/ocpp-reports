# Increment 3 package boundaries

Internal implementation packages are now `contracts`, `warehouse`, `replication`, and `reports`. Previous flat import paths are compatibility modules and must remain until a separate public-interface deprecation audit.

| Boundary | Canonical modules | Legacy aliases |
| --- | --- | --- |
| Producer contract | `contracts.csms_export`, `contracts.export` | `csms_export`, `export` |
| Warehouse | `warehouse.ingestion`, `warehouse.transactions`, `warehouse.queries` | `ingest`, `transaction_projection`, `warehouse_queries` |
| Replication | `replication.collector`, `replication.forwarder` | `collector`, `forwarder` |
| Reporting | `reports.builder`, `reports.rendering`, `reports.csv_profiles` | `reporting`, `rendering`, `csv_profiles` |

The CLI, systemd entrypoints, CSMS read-only export contract, SQLite warehouse and replication schemas, and four immutable installation roles are unchanged.

This PR **does not** remove legacy interfaces or merge the two distinct Audi CSV formatting behaviors. Those remain intentional compatibility surfaces until Increment 4 audits their callers. It also does not change the migration behavior introduced in Increment 2.

CI acceptance requires Python 3.11/3.13, role matrix (terminal/satellite/watchtower/control) and existing fixture-based report equivalence tests. New package-identity checks ensure old imports delegate to canonical implementations.
