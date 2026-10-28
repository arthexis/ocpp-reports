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

## Additional canonical ownership (PR #90 follow-up)

- `contracts.csms_export.validate_export` validates all producer pages, including those subsequently converted to Pydantic `ExportPage` models. Both file and warehouse ingestion use this entry point.
- `warehouse.schema` creates the local evidence schema and `warehouse.collector_schema` creates remote collector tables. No collector DDL is embedded in the request handler; existing on-disk structures remain unchanged.
- Internal implementations use canonical imports for export contracts, warehouse transaction queries and report rendering; legacy flat entry points remain for external callers.
- `reports.builder.build_report_from_database` validates via `validation.validate_summary` rather than invoking the CSV renderer as a side effect.
- `test_canonical_architecture.py` checks package boundaries and schema idempotency alongside prior legacy import-identity regression tests.

The refactor does not yet remove the distinct legacy Audi CSV layouts, rename public commands or apply a production source-ID migration.
