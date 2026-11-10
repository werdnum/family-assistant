# Vega-Lite Quick Reference

Condensed reference for Vega-Lite v5
(`"$schema": "https://vega.github.io/schema/vega-lite/v5.json"`). For how `create_vega_chart` binds
data and attachments, see [data_visualization.md](data_visualization.md).

## Data Binding in `create_vega_chart`

- **Inline values:** `"data": {"values": [{"x": 1, "y": 2}]}`
- **Direct `data=<list>` argument:** `"data": {"name": "data"}` (must be `"data"`)
- **Named `data={"my_set": [...]}` or `data_attachments`:** `"data": {"name": "my_set"}` (or
  attachment filename)

## Marks and Encoding Channels

- **Marks:** `"bar"`, `"line"`, `"point"`, `"area"`, `"circle"`, `"square"`, `"tick"`, `"rect"`
  (heatmaps), `"arc"` (pie/donut: `{"type": "arc", "innerRadius": 0}` with `theta` + `color`),
  `"rule"`, `"text"`, `"boxplot"`. Mark object properties include `type`, `color`, `opacity`,
  `point`, `interpolate`, `tooltip`.
- **Position:** `x`, `y`, `x2`, `y2`, `theta`, `radius`
- **Mark properties:** `color`, `opacity`, `size`, `shape`, `strokeWidth`, `strokeDash`
- **Labels & facets:** `text`, `tooltip`, `row`, `column`, `facet`, `detail`
- **Field types:** `"quantitative"` (continuous numbers), `"temporal"` (dates/timestamps),
  `"ordinal"` (ordered categories), `"nominal"` (unordered categories)

## Transforms and Time Units

```json
"transform": [
  {"filter": "datum.value != null && datum.value > 10"},
  {"calculate": "datum.price * 1.1", "as": "price_with_tax"},
  {
    "aggregate": [{"op": "mean", "field": "value", "as": "avg_value"}],
    "groupby": ["category"]
  }
]
```

- **Aggregation ops:** `"count"`, `"sum"`, `"mean"`, `"median"`, `"min"`, `"max"`, `"stdev"`,
  `"q1"`, `"q3"`
- **`timeUnit` (in encoding or transform):** `"year"`, `"yearmonth"`, `"yearmonthdate"`, `"month"`,
  `"monthdate"`, `"date"`, `"day"`, `"hours"`, `"hoursminutes"`
- **Multi-view composition:** `"layer": [...]` (overlay marks sharing axes), `"hconcat": [...]`,
  `"vconcat": [...]`, `"repeat": [...]`

## Fetching Full Vega-Lite Docs

For advanced scales, window transforms, projections, or layering, fetch the official docs with
`scrape_url`:

- `https://vega.github.io/vega-lite/docs/encoding.html`
- `https://vega.github.io/vega-lite/docs/mark.html`
- `https://vega.github.io/vega-lite/docs/transform.html`
- `https://vega.github.io/vega-lite/docs/scale.html`
- `https://vega.github.io/vega-lite/docs/timeunit.html`
- `https://vega.github.io/vega/docs/specification/` (low-level Vega)
