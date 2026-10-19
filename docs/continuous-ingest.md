# M1 continuous local warehouse

`ocpp-reports ingest --source page.json --database reports.sqlite` remains a finite, offline import.

On a host with the **ocpp-csms Python package installed in the same Python environment**, run:

```sh
ocpp-reports ingest --follow --data-dir /path/to/csms/schemas/8 --database /var/lib/ocpp-reports/reports.sqlite --interval 5
```

The data directory is the CSMS evidence directory expected by `ocpp_csms.export_contract.export_contract`. The consumer calls the producer's public export contract, never queries or modifies the CSMS database itself. It catches up by source cursor before polling, and persists a Reports-owned `connector_state` projection for warehouse-only readers. `observed_at` is the producer snapshot timestamp; `synced_at` is local receipt time. Power and energy fields are **latest known samples**, not live guaranteed measurements or session totals.

This command runs in the foreground; supervise it with systemd (`Restart=always`) if desired. Ansible integration and optional dashboard are later increments. The process exits visibly on malformed export, cursor mismatch or unavailable producer; restart resumes from the committed cursor. For the initial M1, the CSMS Python package must be importable from the Reports environment. Packaging and cross-venv deployment compatibility require validation on the Porsche ABB host before enabling this in production.

`ocpp_reports.follow.current_connectors(database)` exposes a read-only warehouse query. Consumers must interpret stale `observed_at` separately from last-known connector status. Connector status updates without event cursor progress are supported via producer snapshots. Remote forwarding/collector are out of scope.
