# Data Visualization Guide

This document provides guidance for creating data visualizations using the `create_vega_chart` tool.

## Overview

The `create_vega_chart` tool allows you to create professional data visualizations from Vega or
Vega-Lite specifications. The tool generates high-quality PNG images that are automatically
displayed to the user.

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

- Vega-Lite documentation: https://vega.github.io/vega-lite/
- Vega documentation: https://vega.github.io/vega/
- Example gallery: https://vega.github.io/vega-lite/examples/
