# Vega-Lite Quick Reference

This document provides a condensed reference for creating data visualizations with Vega-Lite. For
detailed documentation, use the `scrape_url` tool to fetch specific pages from
https://vega.github.io/vega-lite/docs/ as needed.

## Core Concepts

### Specification Structure

A Vega-Lite specification is a JSON object with these key components:

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "description": "Brief description of the visualization",
  "data": { /* data source */ },
  "mark": { /* visual mark type */ },
  "encoding": { /* visual encodings */ },
  "transform": [ /* optional data transformations */ ]
}
```

### Data Sources

**Inline data:**

```json
"data": {"values": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]}
```

**From URL/file:**

```json
"data": {"url": "data.csv"}
```

**Named data (for referencing attachments):**

```json
"data": {"name": "mydata"}
```

## Mark Types

Common mark types for basic visualizations:

- `"bar"` - Bar charts (vertical bars)
- `"line"` - Line charts
- `"point"` - Scatter plots
- `"area"` - Area charts (filled line charts)
- `"circle"` - Scatter plots with circles
- `"square"` - Scatter plots with squares
- `"tick"` - Tick marks for distributions
- `"rect"` - Rectangles (for heatmaps)
- `"arc"` - Arcs (for pie/donut charts)
- `"text"` - Text labels

Marks can be configured with properties:

```json
"mark": {
  "type": "bar",
  "color": "steelblue",
  "opacity": 0.8,
  "tooltip": true
}
```

## Encoding Channels

Encodings map data fields to visual properties:

### Position Channels

- `"x"` - Horizontal position
- `"y"` - Vertical position
- `"x2"`, `"y2"` - Secondary position (for ranges)

### Mark Property Channels

- `"color"` - Color of the mark
- `"opacity"` - Opacity (0-1)
- `"size"` - Size of point marks
- `"shape"` - Shape of point marks
- `"strokeWidth"` - Width of stroke/line

### Text Channels

- `"text"` - Text content for text marks
- `"tooltip"` - Tooltip content

### Faceting Channels

- `"row"` - Create rows of subplots
- `"column"` - Create columns of subplots
- `"facet"` - General faceting

## Data Types

Specify the type of each field in encodings:

- `"quantitative"` - Continuous numbers (counts, measurements)
- `"temporal"` - Dates and times
- `"ordinal"` - Ordered categories (small, medium, large)
- `"nominal"` - Unordered categories (names, labels)

Example encoding:

```json
"encoding": {
  "x": {"field": "date", "type": "temporal"},
  "y": {"field": "price", "type": "quantitative"},
  "color": {"field": "category", "type": "nominal"}
}
```

## Common Chart Patterns

### Bar Chart

```json
{
  "mark": "bar",
  "encoding": {
    "x": {"field": "category", "type": "nominal"},
    "y": {"field": "value", "type": "quantitative"}
  }
}
```

### Line Chart

```json
{
  "mark": "line",
  "encoding": {
    "x": {"field": "time", "type": "temporal"},
    "y": {"field": "value", "type": "quantitative"}
  }
}
```

### Scatter Plot

```json
{
  "mark": "point",
  "encoding": {
    "x": {"field": "xval", "type": "quantitative"},
    "y": {"field": "yval", "type": "quantitative"},
    "color": {"field": "category", "type": "nominal"}
  }
}
```

### Pie Chart

```json
{
  "mark": {"type": "arc", "innerRadius": 0},
  "encoding": {
    "theta": {"field": "value", "type": "quantitative"},
    "color": {"field": "category", "type": "nominal"}
  }
}
```

### Heatmap

```json
{
  "mark": "rect",
  "encoding": {
    "x": {"field": "xcat", "type": "ordinal"},
    "y": {"field": "ycat", "type": "ordinal"},
    "color": {"field": "value", "type": "quantitative"}
  }
}
```

## Essential Transforms

Transforms modify data before visualization:

### Filter

```json
"transform": [
  {"filter": "datum.value > 10"}
]
```

### Calculate (create new fields)

```json
"transform": [
  {"calculate": "datum.price * 1.1", "as": "price_with_tax"}
]
```

### Aggregate

```json
"transform": [
  {
    "aggregate": [{
      "op": "mean",
      "field": "value",
      "as": "avg_value"
    }],
    "groupby": ["category"]
  }
]
```

Common aggregation operations: `"count"`, `"sum"`, `"mean"`, `"median"`, `"min"`, `"max"`,
`"stdev"`.

### Time Unit

```json
"encoding": {
  "x": {
    "timeUnit": "yearmonth",
    "field": "date",
    "type": "temporal"
  }
}
```

Common time units: `"year"`, `"month"`, `"date"`, `"day"`, `"hours"`, `"yearmonth"`, `"monthdate"`.

## Additional Resources

For comprehensive documentation, examples, and advanced features:

- **Vega-Lite Documentation**: https://vega.github.io/vega-lite/docs/
- **Vega Documentation** (low-level): https://vega.github.io/vega/docs/
- **Example Gallery**: https://vega.github.io/vega-lite/examples/

Use the `scrape_url` MCP tool to fetch specific documentation pages when you need detailed
information about:

- Advanced mark properties and configurations
- Complex transformations (window, bin, density, etc.)
- Layer and concatenation for multi-view compositions
- Interaction and selection
- Scales, axes, and legends customization
- Projection and geographic visualizations

**Example usage:**

```
scrape_url("https://vega.github.io/vega-lite/docs/encoding.html")
```

This will fetch the full encoding documentation as markdown for detailed reference.
