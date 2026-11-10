# Data Visualization Guide

This guide covers creating charts with `create_vega_chart` and `execute_script`. For Vega-Lite
encoding and transform syntax, see [vega_lite_reference.md](vega_lite_reference.md).

## Overview

`create_vega_chart` renders a Vega-Lite (`https://vega.github.io/schema/vega-lite/v5.json`,
recommended) or Vega (`https://vega.github.io/schema/vega/v5.json`) JSON specification into a PNG
attachment that is automatically displayed to the user. When asked for a visualization, always
produce a chart rather than only describing the numbers.

**Tool parameters:**

- `spec` (required): JSON string of the Vega-Lite or Vega specification
- `data` (optional): Computed data (a list of records, or a `dict` mapping dataset names to data)
- `data_attachments` (optional): List of CSV or JSON attachment IDs (or attachment dicts in scripts)
- `title` (optional): Chart title (default: `"Data Visualization"`)
- `scale` (optional): PNG scale factor (default: `2`)

## Passing Data: `data` vs `data_attachments`

1. **`data_attachments=[attachment_id, ...]`** — Use for uploaded CSV/JSON files and large tool
   results already stored as attachments. The tool loads and injects the attachment content
   automatically; reference it in the spec via `"data": {"name": "<filename_or_dataset_name>"}` (or
   omit `data.name` when passing a single attachment). Never inline large attachment contents into
   `spec`.
2. **`data=...`** — Use for computed or filtered results, such as the output of `jq_query()` in a
   script, or small inline datasets.
   - **Critical dataset-naming rule:** When `data` is a list (not a dict of named datasets),
     `create_vega_chart` binds it to the dataset name **`"data"`**. Your spec **must** reference
     `"data": {"name": "data"}` — using any other name renders a blank chart.
   - To pass multiple named computed datasets, pass a dictionary (`data={"summary": rows}`) and
     reference `"data": {"name": "summary"}` in the spec.
3. **Combining both** — You can pass both `data={"summary": computed}` and
   `data_attachments=[raw_file]` for multi-layer charts.

## Large Attachments and `jq_query`

Large JSON tool results (such as `download_state_history`) arrive in context as an attachment ID
with an inferred JSON schema rather than raw content:

1. Inspect the schema, and if needed probe with `jq_query`:
   - Record count: `jq_query(attachment_id, "length")`
   - First item: `jq_query(attachment_id, ".[0]")`
   - Date range: `jq_query(attachment_id, "[.[0].last_changed, .[-1].last_changed]")`
2. Either pass `data_attachments=[attachment_id]` directly to `create_vega_chart`, or transform and
   filter the data with `jq_query` inside `execute_script` and pass the result via `data=...`.

## Cleaning Home Assistant Sensor History

Home Assistant sensors frequently record non-numeric states such as `"unavailable"`, `"unknown"`,
`"None"`, `""`, or `null`, which break quantitative axes. Always convert with `tonumber?`, drop
nulls, and sort by timestamp:

```python
cleaned_data = jq_query(
    history_attachment_id,
    """
    .entities[0].states
    | map({time: .last_changed, value: (.state | tonumber?)})
    | map(select(.value != null))
    | sort_by(.time)
    """,
)

spec = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "data": {"name": "data"},  # Required when passing a list via `data=`
    "mark": {"type": "line", "point": True, "tooltip": True},
    "encoding": {
        "x": {"field": "time", "type": "temporal", "title": "Time"},
        "y": {"field": "value", "type": "quantitative", "title": "Temperature (°C)"},
    },
}

create_vega_chart(
    spec=json_encode(spec),
    data=cleaned_data,
    title="Pool Temperature",
)
```

## Multi-Chart Scripts

For multi-step pipelines or dashboards with several charts, run a single script with
`execute_script` and return a list of charts as the final expression — all returned chart
attachments are automatically shown to the user:

```python
temp_data = download_state_history(
    entity_ids=["sensor.pool_temp"],
    start_time=time_add_duration(time_now(), -7, "days"),
)
ph_data = download_state_history(
    entity_ids=["sensor.pool_ph"],
    start_time=time_add_duration(time_now(), -7, "days"),
)

temp_chart = create_vega_chart(spec=json_encode(temp_spec), data_attachments=[temp_data])
ph_chart = create_vega_chart(spec=json_encode(ph_spec), data_attachments=[ph_data])
[temp_chart, ph_chart]
```

## Troubleshooting and Further Reference

- **Blank chart:** Verify `"data": {"name": "data"}` when using `data=<list>`, check that encoded
  field names match the dataset keys, and confirm `jq_query` did not return an empty list.
- **Rendering errors:** Strip non-numeric sensor states (`"unavailable"`, `"unknown"`) with
  `tonumber?` before plotting quantitative fields.
- **Spec reference:** See [vega_lite_reference.md](vega_lite_reference.md), or fetch specific pages
  from `https://vega.github.io/vega-lite/docs/` (such as `encoding.html`, `mark.html`,
  `transform.html`, `scale.html`, `timeunit.html`) using `scrape_url`.
