---
name: Data Visualization
description: Build charts and graphs from data (sensor history, CSV or JSON files, query results) as Vega-Lite PNGs. Load before any request to chart, plot or graph data. Activates create_vega_chart.
activate_tools:
  - create_vega_chart
---

# Data Visualization

`create_vega_chart` renders a Vega or Vega-Lite spec to a PNG that is shown to the user. When asked
for a chart, produce one; a description of the data instead of a chart is not an answer.

For worked examples, read `data_visualization.md` with `get_user_documentation_content`; for spec
syntax (chart types, field types, transforms), read `vega_lite_reference.md`.

## Getting the Data

- Home Assistant history: `download_state_history`, which returns an attachment.
- Files the user sent: use their attachment ids.
- Large tool results (over about 10KB) arrive as an attachment id with a schema. Explore with
  `jq_query` before writing the spec: `length`, `.[0]`, the first and last timestamps.

## Passing Data to `create_vega_chart`

- `data_attachments=[id, ...]` for files and stored attachments. The tool loads the full content, so
  never inline it.
- `data=...` for computed results, such as the output of `jq_query`. Raw data passed this way is
  named **`"data"`**, and the spec must reference exactly `{"name": "data"}`; any other name renders
  a blank chart. Pass a dict to name several datasets.
- Both can be combined for layered charts from several sources.

## Clean Before Charting

Sensor states include `"unavailable"`, `"unknown"`, `"None"`, `""` and nulls, which break rendering.
Convert and filter, and sort time series:

```
.entities[0].states
| map({time: .last_changed, value: (.state | tonumber?)})
| map(select(.value != null))
| sort_by(.time)
```

Check for empty datasets and single points before building an elaborate spec.

## Multi-Step Work

For several transformations or several charts, write one script with `execute_script` that composes
`download_state_history`, `jq_query` and `create_vega_chart`: attachments a script's tools create
are returned automatically. A single chart from data you already have needs only a direct
`create_vega_chart` call.
