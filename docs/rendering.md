# B3: offline presentation layer

`ocpp_reports.rendering.render_report(report, format="html" | "text")` accepts only the internal `ocpp-reports/summary/v1` document produced by the B2 aggregator, **not** the CSMS export schema. It has no CSMS imports, network access, scheduler or delivery side effects.

Templates are packaged as application resources and rendered with Jinja2 StrictUndefined. HTML escapes source-supplied values. Both formats show a prominent **PROVISIONAL** notice for any non-complete report or missing energy. Energy is converted from Wh to kWh for display, with Wh retained in the transaction table.

This is a presentation-only layer. Finality cannot be established by a template; B2 ingestion must determine completeness from the authoritative source contract (CSMS issue #224). Do not automatically email provisional reports as final.
