# Family Assistant Tools

This package holds the local (in-process) tools the LLM can call: notes, tasks, documents, calendar,
communication, browser and computer use, scripting, media generation, Home Assistant, and more.

## Layout

Tool implementations are grouped one module per theme — `notes.py`, `tasks.py`, `documents.py`,
`calendar.py`, `communication.py`, `automations.py`, `home_assistant.py`, `engineering.py`, and so
on. Each module exports a `*_TOOLS_DEFINITION` list of JSON schemas alongside the async functions
that implement them.

Supporting modules:

- `__init__.py` — the registry. `_LOCAL_TOOL_DEFINITIONS`, `_LOCAL_TOOL_IMPLEMENTATIONS`, and
  `LOCAL_TOOL_METADATA_BY_NAME` are the three inputs; `LOCAL_TOOL_REGISTRATIONS`,
  `LOCAL_TOOL_DESCRIPTORS`, `AVAILABLE_FUNCTIONS`, and `TOOLS_DEFINITION` are derived from them.
- `types.py` — `ToolDefinition`, `ToolResult`, and the `ToolExecutionContext` passed to every tool.
- `metadata.py` — the `ToolTag` enum and descriptor models used for policy matching and taint
  tracking.
- `policy.py` — the `tools_policy` configuration and evaluation engine that decides whether a call
  is allowed, confirmed, or denied.
- `on_demand.py` — the view that hides on-demand tools behind the `activate_tools` meta-tool.
- `infrastructure.py`, `schema.py`, `attachment_utils.py`, `taint_helpers.py` — shared base classes
  and helpers.

## Development

See [CLAUDE.md](CLAUDE.md) in this directory for the development guide: adding and registering a new
tool, policy rules, `ToolResult` patterns, async/blocking rules, testing, and custom tool UIs.
