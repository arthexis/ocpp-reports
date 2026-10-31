## Charger power: measured versus nominal

The **Potencia** cell is a connector snapshot, not transaction energy.
When OCPP `MeterValues` contains `Power.Active.Import`, Reports uses
the measured `power_w` only if its timestamp is offset-aware and no more
than 120 seconds old. An older measurement is not presented as live.

An operator may instead configure `ocpp-csms rating set --power-kw N`
for the one known CSMS charge point (or specify `--cp ID`). Omitted
`--connector` sets the charger-wide default; `--connector all` writes
explicit ratings for observed physical outlets, and `--connector N` sets
a specific outlet. The CSMS adds `rated_power_w` and `rating_scope`
to its export/v1 status snapshot, including empty-cursor refreshes.

The local Reports warehouse projects these into additive
`connector_ratings` metadata and emits `rating` changefeed updates
to the existing collector; the collector stores the latest values per
source/charger/connector. No transaction replay or manual Reports
reset is required. Dashboard displays **medida** for fresh metered power,
otherwise **nominal** for the configured rating, otherwise an em dash.
Raw metered `power_w` is never overwritten by rating metadata.

Connector 0 describes the shared station capability, not an
independent charging connector: inherited ratings must not be summed
to estimate station power. Existing source allowlists still apply.

# Replication freshness and connector status provenance (#111)

There are separate clocks. `sources.last_ingested_at` is **local ingest time**. `collector_receipts.received_at` is when a **new replication sequence** was accepted by the collector. A replay or no-op must not advance this value. `connector_state.observed_at` is the producer snapshot export time and can advance even if the charger remains faulted. `status_observed_at` is the original CSMS StatusNotification receipt, never inferred from exported_at, a Heartbeat, or a collector acknowledgement.

## Compatibility and installation

The new collector receipt table is additive and created by the existing collector schema initializer at first accepted batch. Historical receipts are **unknown** until a new sequence arrives; do not backfill from cursor positions or current wall clock. Existing connector rows lacking provenance remain unknown, and new exports may populate the timestamp from CSMS persisted original events. The schema and source IDs of CSMS are untouched. No database reset, source rekey, ACK reset, or manual historical rewrite is required.

Deploy the additive CSMS export producer change before expecting a reliable status age on newly ingested connector snapshots. Deploy Reports to the satellite (ingestion), then to Watchtower (collector and dashboard), preserving normal role assignments and existing customer source allowlists. During production validation, compare the same source/connector at two naturally separated export intervals: last status receipt remains fixed while the snapshot time advances. A genuine StatusNotification must update the original receipt time. Check Porsche shows remote receipt freshness for GWay-005 and ABB local ingestion freshness; Audi must remain scoped to GWay-002.

For rollback, restore the previous application release. Retain the additive `collector_receipts` table and payload metadata: older versions ignore them. Do not reset replication data, customer credentials, source mappings, or charger state. A fault, including EmergencyStop, is still the last known observation until superseded by actual newer evidence. Physical emergency-stop status requires an independent on-site check.


## Collector-owned charger locations (optional)

Location labels live in the **existing Watchtower collector warehouse** and are
not part of CSMS exports, source credentials, replication batches or immutable
evidence. Each assignment is keyed by `(source_id, charger_id)`; multiple
chargers may share a label and the same charger ID in another source is distinct.

Run these commands on the collector host with local SQLite permissions.
By default the CLI infers the deployed collector warehouse
`/var/lib/ocpp-reports/warehouse.sqlite` (the same path used by the managed
collector and dashboard systemd units). `OCPP_REPORTS_DATABASE` overrides this
default if set, and `--database PATH` takes precedence. The CLI refuses a
missing or unrelated inferred database rather than creating or guessing another
warehouse:

```sh
ocpp-reports locations list
ocpp-reports locations set --source gway-002 --charger CHARGER_ID \
  --name 'Audi San Pedro'
ocpp-reports locations remove --source gway-002 --charger CHARGER_ID
```


You may omit **either** `--source` or `--charger` for `locations set` and
`locations remove` when exactly one matching charger identity exists in the
collector's local/replicated charger inventory (or its persisted location
assignments). For example:

```sh
ocpp-reports locations set --charger CHARGER_ID --name 'Audi San Pedro'
ocpp-reports locations remove --source gway-002
```

At least one identifier must be provided. If an identifier is missing or
ambiguous across sources or chargers, the command refuses the change and
reports matching candidates. Inference only uses this collector's exact charger
identities; a source cursor alone is insufficient to infer a charger. Explicit
`--source` and `--charger` are still supported for new registrations.

For nonstandard installations, add `--database /path/to/warehouse.sqlite`.
Write operations require collector warehouse permissions, which may mean
running as the configured `ocpp-reports` service account. The database must
already exist; the CLI never creates another warehouse.

The dashboard/API adds `location` to charger-specific connectors,
transactions and events and shows **Ubicación** in place of **Origen**.
Unassigned chargers display their original `source_id`. Synchronization
retains source identities. Customer source allowlists remain unchanged.
Deleting an assignment restores the source fallback; collector source reset
preserves assignments, while wiping/replacing the entire collector database
requires a separate metadata backup. Only the collector owner can edit labels,
and a viewer cannot edit them through the dashboard.


### Operator output and inventory filters

`ocpp-reports locations list` prints readable columns **Source**, **Charger**
and **Name**. It includes all known charger identities, whether or not a name
has been assigned, plus sources with replication/local-ingestion evidence but
no known charger. The latter display `(not observed)` for the charger and
cannot be used to infer a charger ID. A source with multiple chargers appears
on multiple rows.

```sh
ocpp-reports locations list
ocpp-reports locations list --set          # assigned names only
ocpp-reports locations list --unset        # missing names, including source-only
ocpp-reports locations list --source gway-002
ocpp-reports locations list --json         # array of {source_id, charger_id, location, assigned}
ocpp-reports locations set --source gway-002 --name "Audi Calzada"
ocpp-reports locations set --source gway-002 --name "Audi Calzada" --json
ocpp-reports locations remove --source gway-002 --json
```

`--set` and `--unset` are mutually exclusive. The machine-readable
`--json` switch applies to all three `locations` commands; plain
`set` and `remove` acknowledge the resolved source/charger and operation in
human-readable text. The SQLite column and dashboard/API field remain named
`location` for compatibility; only the operator CLI uses `--name`.


## Dashboard layout, filtering and instance timezones (#114)

The dashboard exposes a single **Filtrar** search field for source ID,
charger ID, collector-owned location label and connector status / transaction
state / event action. The previously visible **Estado / acción**,
**Ubicación**, **Desde**, **Hasta**, and **Limpiar** controls are hidden for
the December feature freeze. Existing explicit query parameters for those
filters remain supported for compatibility, but there are no visible controls
for them; period-filter UI work is deferred. Source allowlists remain
enforced by the warehouse query before these presentation-only filters.
The `/api/snapshot` endpoint keeps its unchanged raw UTC/source-scoped
evidence; existing `source_id` and `charger_id` links continue working.

The Transactions title includes **Exitosas | Fallidas | Todas**. The default
**Exitosas** view shows transactions with explicitly positive finalized
`energy_wh`. **Fallidas** shows exactly zero finalized Wh; each tab displays its count from the current source/site/date-filtered snapshot before the transaction-category filter; **Todas**
retains the full unfiltered transaction history, including ongoing sessions
and missing final meter evidence. Zero energy alone is only a display
classification, not proof of a charger failure. The selection is preserved
in the query string when using the other dashboard filters; none of these
views modify the raw snapshot API or the stored evidence.

Transaction history spans the full page and shows start and stop, measured
SoC start/end when available, and final or clearly identified live estimated
energy. SoC fields `soc_start` and `soc_end` require the additive CSMS
export change in ocpp-csms PR #310; older transaction snapshots correctly
display `—` until the producer sends measured values during normal
synchronization. Do **not** infer battery SoC or reset replication cursors to
backfill. Unanswered/abnormal event records are never grouped with unrelated
events: the compact **Eventos recientes** section only collapses repeated
routine evidence while keeping each event expandable. The removed empty
connector status timestamp column is still available in the raw API.

Instance operators can configure allowed IANA zones with repeated
`ocpp-reports serve --timezone` arguments; the **first** is the default,
and UTC remains available. For example:

```sh
ocpp-reports serve --timezone America/Monterrey --timezone UTC
```

### Update timezones on a running dashboard

`serve` normally starts a **new** HTTP server. On an existing installation,
use `--update` with administrator privileges instead; it saves the choices
and restarts only the selected dashboard systemd service:

```sh
sudo ocpp-reports serve --update --timezone America/Monterrey --timezone UTC
# If the customer dashboard's inventory name is dashboard-audi:
sudo ocpp-reports serve --update --instance dashboard-audi --timezone America/Monterrey --timezone UTC
```

The default instance is `dashboard` (`ocpp-reports-dashboard.service`).
Other instance names correspond to `dashboards.json` entries, e.g.
`dashboard-audi`, and **must be selected explicitly**; the command does not
modify every customer dashboard. Deploy the updated service templates once
before the first `--update`: the command refuses a service that does not
already reference its persistent timezone file. No collector/CSMS service,
warehouse, or charging session is modified. The update preserves the
source-scoped `ExecStart` options. It is idempotent; failed restarts restore
the previous config and attempt service recovery.

Timezone choices are stored in
`/etc/ocpp-reports/<instance>.timezones.json`, with the first zone as default
and UTC automatically offered. Operator overrides take precedence over
Ansible's configured defaults and persist across deployments; remove that
override file to return to Ansible defaults. A plain `serve --timezone`
continues to start a separate server and may encounter port 8080 already in
use.

The installed collector warehouse is inferred from `/var/lib/ocpp-reports/warehouse.sqlite` (or `OCPP_REPORTS_DATABASE` if set); pass `--database /path/to/warehouse.sqlite` only for a nonstandard existing warehouse. If the warehouse has not yet been created, the dashboard service starts without creating it and returns HTTP 503 until collector data exists. A present unrelated inferred SQLite file is refused; explicit `--database` continues to support nonstandard deployments.

For Ansible-managed dashboard instances use
`reports_dashboard_timezones: ['America/Monterrey', 'UTC']`; individual
customer dashboard entries may override this with `timezones`. Leaving the
list unset preserves the prior UTC default. Invalid IANA names fail before
the dashboard starts. The selected zone is shown after "Página actualizada";
the browser remembers it locally, and the selected zone is kept in the URL
through filter submissions. Timestamps keep minute precision, and the
original timestamps remain unchanged in SQLite and API output.

Deployment is dashboard-only and reversible through the previous immutable
release. Roll out CSMS SoC export independently and validate on a known
transaction with actual SoC samples; missing SoC is expected for chargers
which do not report it.


## Historical transaction SoC refresh (issue #124)

After upgrading CSMS, an old Reports transaction projection may still have
`soc_start` or `soc_end` null even when the historical MeterValues contain
genuine SoC measurements: event-cursor export pages include transaction
snapshots only for events within that page.

For an explicitly selected **completed** transaction, from the local CSMS /
Reports host with the active producer and existing local warehouse:

```sh
ocpp-reports metadata refresh --cp T175-IT1-0620-055 --transaction 247 \
  --database /var/lib/ocpp-reports/warehouse.sqlite --dry-run
# Inspect the preview and approve this exact transaction separately.
ocpp-reports metadata refresh --cp T175-IT1-0620-055 --transaction 247 \
  --database /var/lib/ocpp-reports/warehouse.sqlite --apply
```

Dry-run is the **default**. `--apply` updates only missing SoC fields in an
existing local `current_transactions` projection after verifying the
producer's persisted source identity, exact charge-point and transaction ID,
evidence contract and `ocpp-meter-values` provenance. Conflicting existing
SoC evidence is refused. No rows are created for unknown transactions.
A transaction with no genuine SoC samples remains unchanged.

The update runs in one Reports SQLite transaction and uses the existing
`replicate_transaction_update` trigger. The normal forwarder sends the
updated projection to the authorized Watchtower collector; a dashboard source
allowlist remains enforced. Retried identical refreshes do nothing.
`events`, `transaction_observations`, CSMS data and durable event/forwarder/
collector cursors are never reset, rewound or replayed. The normal forwarder
ACK *may* advance when it delivers the resulting new changefeed entry.

The command uses the **running CSMS service's installed Python interpreter**
through `active_csms` discovery, not a source path from the user. It must be
run with an operator identity that can read the active CSMS schema-8 database
and write the Reports warehouse (normally via an authorized Reports runtime
operator; do not loosen file permissions). It is not a Watchtower-side
reload command and does not restart or configure the charger.

Production validation is **not** part of deployment: run a preview, approve
one historical transaction, apply once, then verify its local projection and
source-scoped dashboard and confirm normal forwarder delivery without
resetting any cursor. See issue #124 for the bounded operational checklist.
