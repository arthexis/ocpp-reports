# M2 standalone dashboard

Run `ocpp-reports ingest --follow --data-dir /path/to/csms/schemas/8 --database /var/lib/ocpp-reports/reports.sqlite` separately, then:

```sh
ocpp-reports serve --database /var/lib/ocpp-reports/reports.sqlite --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080/ locally. The dashboard reads **only** Reports-owned SQLite, using a read-only connection. It does not import CSMS Python modules or contact chargers. It displays last-known connector state separately from source ingestion freshness, plus transactions and administrative source cursors. Recent events appear only on each charger's detail page. Filter by source or charger. A JSON read-only endpoint is available at `/api/snapshot`.

Security: `serve` rejects non-loopback binds. It has no user accounts or authentication; do not expose it publicly. M4 will add nginx HTTPS, authentication, and Ansible services. The main dashboard updates data in place every 5 seconds without reloading the page; no WebSockets. Charger and connector detail pages refresh state and session rows at the same interval while leaving expanded events and historical energy charts intact. Browser polling is distinct from the CSMS-to-warehouse ingestion cadence. M2 does not require forwarder or collector.

**Field validation:** install on customer vendor dedicated host; verify warehouse is populated, dashboard shows live/stale connector state, active and stopped transactions, and survives stopped ingest with visibly old timestamps. This PR does not claim that field validation has occurred.

## Customer source boundaries

Run one loopback process per customer against the same collector warehouse:

```sh
ocpp-reports serve --database /var/lib/ocpp-reports/warehouse.sqlite --port 8080 \
  --title 'Customer B OCPP Reports' --source-id source-b --source-id source-c
ocpp-reports serve --database /var/lib/ocpp-reports/warehouse.sqlite --port 8082 \
  --title 'Customer A OCPP Reports' --source-id source-a
```

Repeated `--source-id` arguments are an enforced allowlist for the HTML page,
JSON API, local records, replicated records and source cursors. URL filters can
only narrow that scope; another source returns 403. Without these arguments,
the backwards-compatible operator dashboard shows all sources. Bind only to
loopback and use independent HTTPS virtual hosts and password files.

Provision `/etc/ocpp-reports/dashboards.json` (root-owned, mode 0640) to make
these instances persistent across Watchtower/control deployment:

```json
[
  {"name": "dashboard", "title": "Customer B OCPP Reports",
   "hostname": "customer-b.example.com", "port": 8080,
   "source_ids": ["source-b", "source-c"],
   "htpasswd": "/etc/ocpp-reports/dashboard.htpasswd"},
  {"name": "dashboard-client-a", "title": "Customer A OCPP Reports",
   "hostname": "customer-a.example.com", "port": 8082,
   "source_ids": ["source-a"],
   "htpasswd": "/etc/ocpp-reports/dashboard-client-a.htpasswd"}
]
```

For an operator dashboard that includes **every current and future ingested source**,
add a third independent instance to the same inventory (retain the Customer A and
Customer B entries):

```json
{"name": "dashboard-admin", "title": "Operator Administration",
 "hostname": "customer.example.com", "port": 8083,
 "source_ids": "*",
 "timezones": ["America/Monterrey", "UTC"],
 "htpasswd": "/etc/ocpp-reports/dashboard-admin.htpasswd"}
```

The first `timezones` entry sets the default for this dashboard. The
Watchtower's existing admin inventory must be updated separately after merging;
a code deployment alone does not modify its persistent `dashboards.json`.

The literal `"*"` is an explicit **all-sources** scope. The dashboard runs
without `--source-id` filters and therefore automatically includes newly
ingested sources. Customer dashboards must continue using nonempty source-ID
arrays; `[]`, omitted `source_ids`, and other wildcard strings are invalid.
The admin dashboard must have its **own** authentication file, independent of
the customer password files. All dashboards share the existing collector
warehouse, without separate ingestion or replication credentials.

Before deployment, point the DNS record at Watchtower, obtain the hostname
certificate, and provision the admin credentials interactively:

```sh
sudo certbot certonly --nginx -d customer.example.com
sudo install -o root -g www-data -m 0640 /dev/null /etc/ocpp-reports/dashboard-admin.htpasswd
sudo htpasswd -B /etc/ocpp-reports/dashboard-admin.htpasswd admin
sudo ansible-playbook -i localhost, ansible/dashboard-instances.yml
```

Validate admin access to all sources and both customer scopes (including the
snapshot API); after another source starts forwarding, it must appear only in
the admin dashboard unless explicitly added to a customer allowlist.
The `"*"` scope is privileged: do not assign it to a customer-facing instance.

Provision certificates at `/etc/letsencrypt/live/<hostname>/` and each password
file first. The dashboard playbook does not generate credentials or certificates.
Existing base-unit regeneration cannot remove the source restrictions because
these are persistent systemd overrides. Apply only dashboard services/proxies:

```sh
sudo ansible-playbook -i localhost, ansible/dashboard-instances.yml
```

The playbook is also included in Watchtower/control deployments when the inventory
exists. Adding a new instance does not change collector credentials or source
identities. Removing an inventory entry does not delete an existing instance;
explicitly disable its service and Nginx site when retiring it. Keep the persistent
ACME webroot `/var/www/ocpp-reports-dashboard-acme` available for renewal.

## Dashboard users

Nginx HTTP Basic authentication provides per-dashboard user/password access;
there are no application accounts, roles or self-service signup. Anyone in a
dashboard's password file can view all sources allowed by that dashboard.
A username can exist in both files with independent passwords. Collector bearer
credentials are separate and must never be used as dashboard passwords.

On the customer host, add a user or change their password interactively:

```sh
sudo htpasswd -B /etc/ocpp-reports/dashboard-client-a.htpasswd USERNAME
sudo htpasswd -B /etc/ocpp-reports/dashboard.htpasswd USERNAME
```

Do not use `-c` on existing files (it erases other users), or `-b` (it puts the
password in process arguments). For initial file creation use `sudo install -o
root -g www-data -m 0640 /dev/null PATH`, then `htpasswd -B PATH USERNAME`.
Remove access with `sudo htpasswd -D PATH USERNAME`; list only usernames with
`sudo cut -d: -f1 PATH`. Nginx reads the file on requests; no reload is needed.
Browsers cache Basic credentials, so test with a private window after changes.


## Event groups: read-only window implementation (PR #127)

The **Eventos recientes** panel on `/chargers/{source_id}/{charger_id}` selects the latest **10 groups per page**
with SQLite window functions over the same source-scoped local and replicated
events. Only adjacent routine Heartbeats and eligible benign status messages
collapse; faults and transaction lifecycle events remain individual groups.
Grouping follows the existing source/event-ID ordering rather than merging
unrelated events across sources. SQL selects summaries only, so opening a charger page does not download the groups' underlying event payloads. The main `/` dashboard does not query or render event groups.

Expanding a group fetches up to **50 display-only event entries** using
`GET /api/event-group?group_source=SOURCE&source_id=SOURCE&charger_id=CHARGER&first_event_id=ID&offset=0`.
The API revalidates the exact group boundary and source allowlist on each
request; application-role users other than administrators cannot fetch the event history. The browser can request the next 50 entries explicitly. The JSON
response intentionally contains only event ID, source, charger, location,
action, and timestamp fields, not raw OCPP payloads, RFID or customer secrets.
Event-group queries never write to the warehouse, CSMS or replication cursors.

**Field performance check:** measure the charger-specific event-group page and the scoped group-detail JSON
`query_duration_ms` on several normal refreshes and expanded large groups.
Compare warm/cold timings to current dashboard response latency and watch
CPU/SQLite load; treat repeated high latency as evidence for a future
materialized Reports-owned group projection. Avoid creating derived tables or
indexes in the authoritative CSMS database. A 10,000-event synthetic
heartbeat group is exercised in tests, but synthetic timing is not a
production performance guarantee.


## Read-only dashboard benchmark

Run on the watchtower using its installed warehouse (or explicitly select a copy):

```bash
ocpp-reports benchmark --iterations 100 --warmup 5 --output benchmark.json
ocpp-reports benchmark --database /path/to/warehouse.sqlite --iterations 30 --json
```

The command checks the installed collector database when `--database` is omitted.
It runs sequential in-process FastAPI requests without opening a listening port
or needing dashboard credentials. It includes dashboard default, second
transaction page, station connector view, snapshot API, and a direct
SQL-backed snapshot query. The "charger_filter" scenario refers to the
**station connector view**, not a source/charger search filter.

Results report p50, p95, p99, max and mean time per scenario (milliseconds),
with a separate Python peak-memory probe for HTTP routes; SQL query results
have no memory probe. The JSON result records database size and row counts.
Use distinct output filenames for successive baselines; `--output` refuses
to overwrite an existing file.

This is a low-concurrency baseline, **not** an external HTTP load test.
Run against a copy of the warehouse if even repeated read queries could
interfere with production operations. The database is opened read-only;
no schema changes or migrations are performed.


### Performance acceptance for the current watchtower

The measured production baseline is recorded in [issue #140](https://github.com/arthexis/ocpp-reports/issues/140):
dashboard p95 3084.509 ms, transaction page 2 p95 3098.317 ms,
station view p95 3267.191 ms, API snapshot p95 1271.926 ms.

The target for the **current charger count and current warehouse size**
is **p95 below 1000 ms for all HTML dashboard scenarios**. This
target must be confirmed by repeating the 100-iteration benchmark on
the same watchtower after merging and deploying the optimization.
Do not treat CI passing or a single fast request as proof.

The benchmark now records `stages` with percentile distributions from
the main dashboard's `Server-Timing` header: `snapshot` and `template`.
Event grouping is scoped to the charger detail view and does not run on `/`. The default first event page streams groups without the full
historic window calculation; filtered requests and subsequent pages
retain the exact window path. API snapshot history is bounded to the
requested limit; raw evidence and stored records are unchanged.

After deploying, run the benchmark with a new filename and compare
the HTML p95, API p95, API `peak_python_bytes`, and stage breakdown
against issue #140. Keep the benchmark read-only and sequential.


## Charger and connector detail navigation (#230)

The charger name in the main **Conectores** section opens
`/chargers/{source_id}/{charger_id}`. The connector number opens
`/connectors/{source_id}/{charger_id}/{connector_id}`; the status link for
an active charging session continues to open its transaction directly.
The charger view contains all recorded connectors and its own recent events;
the connector view contains only the selected connector and its SQL-paginated
sessions. Both use source+charger-qualified identities, not unscoped IDs or
location text. Unknown and forbidden detail identities return 404.

The connector card shows the original status/energy/power observation times and
the last synchronized timestamp when present. The separate admin-only origin
section shows its cursor, local ingestion and replica-receipt timestamps. These
**do not** prove that a particular charger has communicated recently.
No periodic background work, schema change or inferred SoC is introduced.
The energy chart tooltip contains only measured kWh and verified, same-point
SoC (or a missing-value marker when no such reading exists).


## Device UI revision (#233)

The device page shows the most recently **observed Heartbeat**, scoped to the
exact source and charger and read from original local/replicated events. An
absent or invalid timestamp displays “Sin fecha” rather than source ingestion
freshness. “Conectores visibles” counts physical connector IDs above zero;
station C0 remains visible but is excluded from the count. The source-wide
synchronization panel and explanatory paragraph are removed.

The device's recent grouped events are now displayed as a **table directly
after sessions**, with 10 groups per page and expandable, bounded event rows
(50 at a time) retrieved only after opening a group. Event access remains
administrator-only and source/charger-scoped; the main dashboard still has
no event table. Narrow mobile connector cards use unequal short/long columns,
larger values than labels and reduced gaps, with desktop/tablet layout unchanged.



## Charger last receipt / energy over 30 days / mobile links (#235)

“Último recibo” is the latest OCPP event timestamp for the **exact source
and charger** recorded by CSMS, regardless of action. It is not the Reports
source-level ingestion time (which could describe other chargers), and is
separate from “Último Heartbeat”. Unavailable observations show “Sin fecha”.

The charger chart sums **completed session** energy (final `energy_wh`)
for all physical connectors >0 for 30 calendar days including today in the
selected dashboard timezone, grouped by each session's completion date.
Local current transactions take precedence over remote projections; C0 and
open or invalid sessions are excluded. The total is not a continuous meter
time-series, and empty days do not certify zero consumption. Chart is
read-only accessible SVG with no third-party assets or changes to storage.

On the mobile connector cards, all values are slightly larger, and links
align their text to the top while keeping a 44px touch target.


## Detalle compacto y selector de energía 7/30 días (#237)

Una sola gráfica en la pantalla del cargador ofrece botones `7 días` y
`30 días` sin navegar ni hacer consultas adicionales. La agregación original
de treinta días, filtrada por origen y cargador, proporciona ambos totales.
El periodo de siete días comprende los siete días **calendario locales**
más recientes; ambos periodos incluyen hoy. Los máximos de altura se
recalculan para que la escala se aproveche en cada vista. Inicio: siete días.

Las barras usan CSS Grid adaptable a cualquier anchura de móvil: 7 barras
anchas o 30 compactas, sin ancho mínimo ni scroll horizontal. Cada barra
conserva descripción accesible con día, kWh y sesiones cerradas. Ausencia de
sesiones contabilizadas no equivale a consumo físico cero.

Las tarjetas de conector ya no muestran conexión observada, fecha de último
estado, fecha de sincronización ni momento de medición de potencia. La columna
`Inicio` de sesiones se oculta únicamente en pantalla <=520px; en escritorio
permanece en el HTML y en pantalla. Se conservan los cuatro KPIs de
cabecera, enlaces de sesiones activas y eventos según permisos.


## Energía: dos modos y enlaces móviles compactos (#239)

En las tarjetas de la pantalla principal, el enlace vuelve a ocupar solo
la altura del texto. La superficie `::after` de 44px se extiende hacia
la propia etiqueta (arriba), sin forzar `min-height` en el renglón,
manteniendo una zona amplia para pulsar en móvil.

En el detalle de cargador hay una sola gráfica con dos filtros ortogonales:
`7 días / 30 días` y `Total / Conectores`. **Total** agrega por día
los consumos finales de sesiones cerradas de los conectores físicos positivos;
**Conectores** muestra barras de distinto color por C1/C2/otros conectores
existentes. C0 no se suma ni representa como histórico diario: es una
proyección de medidor de estación, no mediciones de energía diaria por sesión.
Por ello los totales coinciden entre modos, sin doble contabilización.
La fuente exacta, el huso horario y la precedencia local frente a réplica se
conservan. Un solo recorrido de SQLite produce ambas vistas.

El título es solo «Energía» y la explicación se encuentra en el control de
información accesible. Ambos filtros se ajustan por línea en móvil; no hay
gráfica adicional ni scroll horizontal en 320/360/390/520 CSS px.

## Más legibilidad móvil y eventos registrados por cargador (#241)

- La tipografía de los valores de las tarjetas de conector en el dashboard
  sube de 1.10 a 1.18 rem y recupera 1 px de separación inferior; las
  etiquetas mantienen 0.77 rem. Se conserva el objetivo táctil ampliado
  sin forzar un renglón de 44 px.
- En detalle, los filtros se sitúan en la misma línea que «Energía»; en móvil
  los periodos se abrevian 7d/30d (nombres accesibles «7 días»/«30 días»).
  La agregación anteriormente «Total» se llama «Estación». En las tarjetas
  individuales se agrandan los valores, no las etiquetas.
- La sección «Eventos registrados» está justo después de Sesiones de carga.
  Ya existía SQL de agrupación/paginación/expansión en lectura; el bloqueo
  anterior era `is_admin`. Ahora operadores con permiso de ver conectores
  pueden consultar solo los eventos del **source_id+charger_id exactos** de
  la página, 10 grupos por página y hasta 50 entradas depuradas al expandir.
  El endpoint nuevo `/api/v1/charger-event-group` requiere ambos parámetros
  y solo devuelve columnas mostradas, no JSON bruto. `/api/event-group`
  y `/api/v1/event-group` siguen requiriendo permiso EVENTS_VIEW/admin.
  Los roles y la autoridad por tenant no cambian; usuarios sin CONNECTORS_VIEW
  nunca reciben los nuevos resultados.


## Tabla de eventos compacta y sesiones recientes (#243)

La vista individual del cargador conserva el historial paginado con una
**sola fila visible por grupo cerrado**: el nombre de la acción es el
`<summary>` desplegable, con un `aria-label` que identifica el grupo.
El renglón de detalle ocupa las cuatro columnas pero permanece `hidden`
hasta abrir el grupo. La carga diferida de hasta 50 registros y las
comprobaciones exactas de source+charger/roles son las mismas.

La gráfica «Energía» utiliza cuatro botones de igual anchura y los
periodos se escriben completos («7 días», «30 días») también en móvil.
En pantallas estrechas la tabla de eventos oculta ID, mientras mantiene
la acción expandible, fecha y cantidad de registros, sin forzar 480 px.

El resumen principal del dashboard pagina **10 sesiones**, sin perder
filtros ni totales. Su tabla móvil muestra cargador, ID/enlace, estado,
fecha de fin y energía; omite ubicación, inicio, duración y SoC.
El historial de cada cargador/conector mantiene **20 por página** y en
móvil omite inicio y duración. La rutina SQL común acepta únicamente 10
o 20; el valor predeterminado sigue siendo 20 para consultas antiguas.


## Información contextual y StatusNotification (#245)

La tabla expandida de eventos del cargador muestra **ID, Fecha, Información**.
La información deriva únicamente de atributos OCPP previstos y depurados:
estado y conector (y código estándar de error en fallas), ID numérico de sesión
en eventos que lo contienen, o un texto descriptivo sin prometer resultados que
el registro no trae. Nunca se exponen los JSON brutos, cadenas `info` de
fabricantes, RFID, idTag ni datos de clientes. La agrupación sigue usando las
fuentes de lectura originales y el aislamiento existente.

Un evento `StatusNotification` sin `status/connector_id` puede agruparse
con el reporte inmediatamente anterior en tiempo **solo** si fuente/cargador/
acción/kind coinciden, el ID es consecutivo y la diferencia temporal
verificable es de 0 a 2 segundos. Se conservan **ambos eventos** en detalle.
La clave de los estados ordinarios permite comprimir repeticiones contiguas
solo de idéntico estado/conector; los errores, eventos `Faulted` y reportes
con campos complementarios mantienen clave única por ID del reporte.

Los cuatro botones de energía siguen teniendo anchuras iguales. Su padding
lateral se establece explícitamente en cero; el padding vertical no cambia.

## Descargar CSV por cargador y elegir formato por dashboard

La vista de cargador (`/chargers/{source_id}/{charger_id}`) incluye **Descargar CSV** junto a **Dashboard**. El enlace descarga las **todas las sesiones registradas** para la identidad exacta `(source_id, charger_id)`, no solo las 20 sesiones de la página actual. La exportación es de solo lectura, deduplica por identidad con prioridad local frente a réplicas Collector, mantiene las sesiones abiertas y de energía cero y deja vacíos los datos que no se midieron. El nombre de archivo contiene origen y cargador. La zona horaria corresponde a la selección válida del dashboard.

Cada instancia en `/etc/ocpp-reports/dashboards.json` puede definir `"csv_profile": "vendor"` (por defecto para Porsche, Audi y cualquier instancia existente) o `"csv_profile": "vendor-csv"` (variante histórica ISO). Ambas usan los diez encabezados acordados: `LOCACION, CONECTOR, FECHA INICIO, FECHA FINAL, WH INICIO, WH FINAL, WH USADOS, % INICIAL, % FINAL, RAZON FINAL`. `vendor` usa fechas locales con espacio y campos `battery_start_pct/battery_stop_pct`; `vendor-csv` usa fechas ISO con offset y `soc_start/soc_end`. No se infieren porcentajes ausentes ni se reemplazan por cero.

Ejemplo: una instancia Porsche puede tener `"csv_profile": "vendor"` y otra Audi `"csv_profile": "vendor-csv"` sin afectar sus filtros `source_ids`, puertos, hostname, login o zona horaria. Si no se configura, ambas usan `vendor`. La plantilla Ansible convierte el campo a `serve --csv-profile`, verifica que sea un valor conocido y mantiene la selección del lado del servidor: los parámetros de la URL **no pueden cambiar el perfil**. El CSV conserva las mismas barreras de autenticación de la vista de conectores y no expone registros de otras fuentes.
