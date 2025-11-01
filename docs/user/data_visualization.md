# Data Visualization Guide

This document provides guidance for creating data visualizations using the `create_vega_chart` tool.

## Overview

The `create_vega_chart` tool allows you to create professional data visualizations from Vega or
Vega-Lite specifications. The tool generates high-quality PNG images that are automatically
displayed to the user.

## Your Role: Fetch Data, Then Visualize

**IMPORTANT**: Your goal is to create data visualizations. You MUST create a visualization -
returning without a chart is not acceptable.

Data can come from multiple sources:

1. **Delegated attachments**: When the default assistant delegates to you, large datasets may appear
   as JSON schema with an attachment ID rather than full content (for files >10KB)
2. **URLs**: Fetch data yourself if given a URL using available tools
3. **Home Assistant**: Use `download_state_history` or other Home Assistant tools to fetch data
4. **User-provided**: Small inline data from the user's message

### Workflow for Large Data Attachments

When you receive a large JSON dataset (>10KB), it will appear as:

- A JSON schema showing the data structure
- File size and attachment ID
- A note to use the `jq_query` tool for exploration

**Example workflow**:

1. **Understand the data structure** from the provided schema
2. **Explore the data** using `jq_query` tool:
   - Count records: `jq_query(attachment_id, 'length')`
   - View first item: `jq_query(attachment_id, '.[0]')`
   - Get date range: `jq_query(attachment_id, '[.[0].last_changed, .[-1].last_changed]')`
   - Extract specific fields: `jq_query(attachment_id, 'map(.state)')`
3. **Create Vega-Lite spec** based on the data structure
4. **Call `create_vega_chart`** with `data_attachments=[attachment_id]`
   - The tool will automatically fetch the full content and inject it into your Vega spec
   - You don't need to inline the data - just reference it by attachment ID

### Example: Visualizing Home Assistant State History

```python
# User asks: "Chart pool temperature over last 5 days"
# Default assistant calls download_state_history, gets 199KB JSON, delegates to you

# You receive:
# [System: Large data attachment (199KB)]
# [Attachment ID: 636058f3-...]
# Schema: { type: "array", items: { properties: { entity_id, state, last_changed, ... }}}

# Step 1: Explore the data
jq_query("636058f3-...", "length")  # Returns: 367
jq_query("636058f3-...", ".[0]")    # See first record structure
jq_query("636058f3-...", "[.[0].last_changed, .[-1].last_changed]")  # Date range

# Step 2: Create Vega-Lite spec
spec = {
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"name": "pool_data"},  # Reference by name
  "mark": "line",
  "encoding": {
    "x": {"field": "last_changed", "type": "temporal"},
    "y": {"field": "state", "type": "quantitative"}
  }
}

# Step 3: Create chart with attachment
create_vega_chart(
  spec=json.dumps(spec),
  data_attachments=["636058f3-..."],  # Tool fetches full content
  title="Pool Temperature Over 5 Days"
)
```

## When to Use This Tool

Use `create_vega_chart` when users request:

- Charts or graphs from data (bar charts, line graphs, scatter plots, etc.)
- Visual representation of datasets
- Data analysis visualizations
- Comparisons or trends shown graphically

## Vega vs Vega-Lite

**Vega-Lite** (Recommended for most cases):

- High-level grammar for simple, common charts
- More concise specifications
- Ideal for: bar charts, line graphs, scatter plots, area charts, pie charts
- Schema: `https://vega.github.io/schema/vega-lite/v5.json`

**Vega** (For complex visualizations):

- Low-level specification with fine-grained control
- More verbose but highly customizable
- Use for: custom interactions, complex layouts, advanced transformations
- Schema: `https://vega.github.io/schema/vega/v5.json`

## Data Sources

The tool supports three ways to provide data:

1. **Inline in spec**: Embed data directly in the `values` field
2. **CSV attachments**: Reference by filename in the spec's `data.name` field
3. **JSON attachments**: Reference by filename in the spec's `data.name` field

### Example with Inline Data

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {
    "values": [
      {"category": "A", "value": 28},
      {"category": "B", "value": 55},
      {"category": "C", "value": 43}
    ]
  },
  "mark": "bar",
  "encoding": {
    "x": {"field": "category", "type": "nominal"},
    "y": {"field": "value", "type": "quantitative"}
  }
}
```

### Example with CSV Attachment

When the user provides a CSV file:

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"name": "data.csv"},
  "mark": "line",
  "encoding": {
    "x": {"field": "month", "type": "temporal"},
    "y": {"field": "sales", "type": "quantitative"}
  }
}
```

## Common Chart Types

### Bar Chart

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"values": [...]},
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
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"values": [...]},
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
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"values": [...]},
  "mark": "point",
  "encoding": {
    "x": {"field": "x_value", "type": "quantitative"},
    "y": {"field": "y_value", "type": "quantitative"},
    "color": {"field": "category", "type": "nominal"}
  }
}
```

### Pie Chart

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"values": [...]},
  "mark": {"type": "arc", "innerRadius": 0},
  "encoding": {
    "theta": {"field": "value", "type": "quantitative"},
    "color": {"field": "category", "type": "nominal"}
  }
}
```

### Area Chart

```json
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "data": {"values": [...]},
  "mark": "area",
  "encoding": {
    "x": {"field": "time", "type": "temporal"},
    "y": {"field": "value", "type": "quantitative"}
  }
}
```

## Field Types

Vega-Lite requires you to specify the data type for each field:

- **`quantitative`**: Numeric values (e.g., sales amounts, temperatures)
- **`nominal`**: Categorical values without order (e.g., colors, names)
- **`ordinal`**: Categorical values with order (e.g., size: small, medium, large)
- **`temporal`**: Date/time values (e.g., timestamps, dates)

## Best Practices

1. **Choose appropriate chart types**: Match the visualization to the data and question
2. **Use meaningful titles**: Set a descriptive title parameter
3. **Label axes clearly**: Include field names that describe the data
4. **Consider scale**: Default scale is 2x for high-DPI displays
5. **Keep it simple**: Start with Vega-Lite unless you need advanced features
6. **Test incrementally**: If a complex spec fails, simplify and build up

## Tool Parameters

- **`spec`** (required): JSON string of Vega/Vega-Lite specification
- **`data_attachments`** (optional): List of CSV/JSON attachment IDs
- **`title`** (optional): Chart title for the user (default: "Data Visualization")
- **`scale`** (optional): PNG scale factor (default: 2 for high-DPI)

## Error Handling

If the tool returns an error:

- **"Invalid JSON in spec"**: Fix JSON syntax errors
- **"Error rendering chart"**: Invalid Vega spec - check field names, types, and structure
- Check that referenced data fields exist in your dataset
- Verify attachment IDs are correct if using external data

## Example Workflow

1. User provides data (inline, CSV, or JSON)
2. Analyze the data structure and user's visualization goal
3. Choose appropriate chart type
4. Generate Vega-Lite spec with correct field types
5. Call `create_vega_chart` with the spec
6. The PNG image is automatically displayed to the user

## Resources

### Quick Reference

A condensed Vega-Lite reference is automatically included in this profile's system prompt (see
`vega_lite_reference.md`). It covers common chart types, encoding channels, data types, and
essential transforms.

### Full Documentation

For detailed information beyond the quick reference, you can use the `scrape_url` tool to
dynamically fetch specific documentation pages:

**Key Vega-Lite Documentation Pages:**

- Main docs: https://vega.github.io/vega-lite/docs/
- Encoding reference: https://vega.github.io/vega-lite/docs/encoding.html
- Mark types: https://vega.github.io/vega-lite/docs/mark.html
- Data transforms: https://vega.github.io/vega-lite/docs/transform.html
- Scales: https://vega.github.io/vega-lite/docs/scale.html
- Time units: https://vega.github.io/vega-lite/docs/timeunit.html
- Example gallery: https://vega.github.io/vega-lite/examples/

**Vega (low-level) Documentation:**

- Main docs: https://vega.github.io/vega/docs/
- Specification: https://vega.github.io/vega/docs/specification/
- Transforms: https://vega.github.io/vega/docs/transforms/

**Using the scrape_url Tool:**

When you need detailed information about specific Vega/Vega-Lite features, use the `scrape_url` MCP
tool to fetch the documentation:

```
scrape_url("https://vega.github.io/vega-lite/docs/encoding.html")
```

This will return the documentation page as markdown that you can use to understand advanced
features, complex configurations, or troubleshoot issues.
