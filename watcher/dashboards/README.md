# Kraken's Eye dashboard

Dashboards have no API, so the dashboard lives here as files.

- `krakens-eye.json` is a placeholder. The Insights dashboard file format is not documented, so this file only
  sketches the dashboard (name, variables, groups). Build the dashboard in the control panel from the sidecar
  below, export it from Settings, JSON tab, and replace this file with the export. Porthole serves the file as is
  (`/watcher/dashboards/krakens-eye.json`) and never parses it.
- `krakens-eye.queries.json` is the sidecar the Dashboards page reads: `variables` (name, type, where the values
  come from, default) and `charts`, each with `title`, `type`, `group`, `legend`, `promql` and optional
  `thresholds`. In `promql`, `$region` and `$tentacle` are the dashboard variables; Porthole substitutes the region
  shown and the tentacle URNs of that region when it runs a chart's query, and `$load_balancer_urn`, `$database_urn`
  and `$head_urn` with the URNs from the fleet description (paste them in when building the dashboard by hand).
  Charts select fleet members by `resource_urn` because fresh Droplets report no `resource_name` (BUGS.md B-023); the
  Function chart keeps `resource_name="kraken"` because the namespace has no URN in the fleet description. Charts
  without a query (the log table and the markdown note) are listed but not run.

The brief describes the sidecar as a list of charts; it is an object with `variables` and `charts` so the variables
and thresholds the page lists live in the same file.

Import steps, as the Dashboards page shows them: open Insights, Dashboards; Create dashboard and name it
Kraken's Eye; import the dashboard file; pick tor1 in the region list (syd1 for tentacle-3).
