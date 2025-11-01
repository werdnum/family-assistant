# JSON Attachment Handling in LLM Context

## Problem

Large JSON files (e.g., 199KB of Home Assistant state history) bloat LLM context unnecessarily when
passed as tool result attachments. This wastes tokens and can overwhelm the model with data it
doesn't need to see in full.

Example from logs: A 199KB JSON file with 367 temperature readings was being injected with a
placeholder message "Binary content not accessible to model" - resulting in the model seeing neither
the data structure nor the actual data, and consequently doing nothing.

## Solution

Implement intelligent attachment injection based on file size and type, allowing symbolic
manipulation of large datasets via the `jq` tool.

### Size Thresholds

- **≤10KiB**: Inject full JSON content inline in LLM context

  - Rationale: Small files (like a typical API response ~4KB) are manageable in context
  - Model can inspect and work with the data directly

- **>10KiB**: Inject schema + metadata only, enable symbolic querying

  - Generate JSON schema using `genson` library
  - Include file size and attachment ID
  - Model uses `jq` tool to query specific fields/records as needed

### Architecture

#### Framework-Level Injection

Attachment handling occurs in `BaseLLMClient._create_attachment_injection()`:

- Checks MIME type (`application/json`, `text/csv`, `text/*`)
- Determines inline vs symbolic strategy based on size
- Generates injection message for LLM

#### jq Tool for Symbolic Queries

When data is too large for inline injection, the model can:

- Query record counts: `jq(attachment_id, 'length')`
- Inspect structure: `jq(attachment_id, '.[0]')`
- Extract date ranges: `jq(attachment_id, 'map(.last_changed) | [min, max]')`
- Filter/transform: `jq(attachment_id, 'map(select(.state > 20))')`

This allows the model to "explore" large datasets without loading them entirely into context.

### Workflow Example: Data Visualization

1. **User requests**: "Make me a chart of pool temperature over the last 5 days"

2. **Default assistant**:

   - Calls `download_state_history` tool
   - Tool returns 199KB JSON as attachment (367 records)
   - Delegates to `data_visualization` service with attachment

3. **Delegation processing**:

   - Attachment injection detects 199KB > 10KiB
   - Generates schema showing: array of `{entity_id, state, last_changed, attributes}` objects
   - Injects: "Large data attachment (199KB) - use jq tool to query symbolically"

4. **data_visualization service**:

   - Sees schema, understands data structure
   - Optionally queries: `jq(id, '.[0]')` to see example record
   - Optionally queries: `jq(id, '[.[0].last_changed, .[-1].last_changed]')` for date range
   - Creates Vega-Lite spec referencing data by structure
   - Calls `create_vega_chart(spec, data_attachments=[attachment_id])`
   - Tool internally fetches full content and injects into Vega spec

5. **Result**: Chart generated without ever loading 199KB into LLM context

## Implementation Details

### MIME Type Detection

Handled types:

- `application/json` - JSON data
- `text/csv` - CSV data
- `text/*` - Other text formats

### Schema Generation

Using `genson` library:

```python
from genson import SchemaBuilder
builder = SchemaBuilder()
builder.add_object(json_data)
schema = builder.to_json()
```

Advantages:

- Automatic type inference
- Handles nested structures
- Compact representation

### jq Integration

Using `jq.py` library:

```python
import jq
result = jq.compile(jq_program).input(json_data).text()
```

Tool provides safe interface:

- Attachment ID validation
- JSON parsing
- jq error handling
- Formatted output

## Future Considerations

### Mandatory Tools

Processing profiles may eventually support "mandatory tools" that the model **must** use before
completing. For data_visualization, this could enforce:

- Must call `create_vega_chart` before completing
- Must handle all delegated attachments

This would prevent the empty-response issue seen in the original bug.

**Status**: Not implemented yet - requires changes to tool execution framework.

### Other Data Formats

Similar approaches could apply to:

- **Large CSV files**: Provide schema + sample rows, allow SQL-style queries
- **XML/HTML**: Provide structure, allow XPath queries
- **Binary formats**: Extract metadata without full content

## Testing Strategy

- **Attachment injection**: Verify small files inline, large files get schema
- **jq tool integration**: Test end-to-end query execution with real attachments
- **Integration test**: Full delegation flow resulting in chart creation

## Related Documentation

- User guide: `docs/user/data_visualization.md` - Instructions for data_visualization service
- Tool implementation: `src/family_assistant/tools/data_manipulation.py` - jq tool
- Framework code: `src/family_assistant/llm/__init__.py` - Attachment injection
