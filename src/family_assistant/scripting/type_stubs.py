"""Type stub generation for Monty script type checking.

Generates Python type stub strings for external functions so that
Monty's type_check(prefix_code=...) can validate scripts statically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition, ToolPropertySchema

# JSON Schema type → Python type annotation
_JSON_SCHEMA_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}

# Static stubs for built-in APIs that don't change at runtime.
# TimeDict is declared as a dict so the type checker can validate
# scripts that pass time dicts around without needing the actual TypedDict.
_TIME_DICT_TYPE = "dict[str, int | str]"

_STATIC_STUBS = f"""\
# JSON API
def json_encode(obj: object) -> str: ...
def json_decode(s: str | bytes | bytearray | object) -> object: ...

# Time API
def time_now() -> {_TIME_DICT_TYPE}: ...
def time_now_utc() -> {_TIME_DICT_TYPE}: ...
def time_create(year: int = 1970, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0, second: int = 0, nanosecond: int = 0, timezone_name: str = "UTC") -> {_TIME_DICT_TYPE}: ...
def time_from_timestamp(seconds: float, nanoseconds: int = 0) -> {_TIME_DICT_TYPE}: ...
def time_parse(time_string: str, format_string: str = "", timezone_name: str = "") -> {_TIME_DICT_TYPE}: ...
def time_in_location(time_dict: {_TIME_DICT_TYPE}, timezone_name: str) -> {_TIME_DICT_TYPE}: ...
def time_format(time_dict: {_TIME_DICT_TYPE}, format_string: str) -> str: ...
def time_add(time_dict: {_TIME_DICT_TYPE}, seconds: float) -> {_TIME_DICT_TYPE}: ...
def time_add_duration(time_dict: {_TIME_DICT_TYPE}, amount: float, unit: str) -> {_TIME_DICT_TYPE}: ...
def time_year(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_month(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_day(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_hour(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_minute(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_second(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_weekday(time_dict: {_TIME_DICT_TYPE}) -> int: ...
def time_before(t1: {_TIME_DICT_TYPE}, t2: {_TIME_DICT_TYPE}) -> bool: ...
def time_after(t1: {_TIME_DICT_TYPE}, t2: {_TIME_DICT_TYPE}) -> bool: ...
def time_equal(t1: {_TIME_DICT_TYPE}, t2: {_TIME_DICT_TYPE}) -> bool: ...
def time_diff(t1: {_TIME_DICT_TYPE}, t2: {_TIME_DICT_TYPE}) -> float: ...
def duration_parse(duration_string: str) -> float: ...
def duration_human(seconds: float) -> str: ...
def timezone_is_valid(timezone_name: str) -> bool: ...
def timezone_offset(timezone_name: str, time_dict: {_TIME_DICT_TYPE} | None = None) -> int: ...
def is_between(start_hour: int, end_hour: int, time_dict: {_TIME_DICT_TYPE} | None = None) -> bool: ...
def is_weekend(time_dict: {_TIME_DICT_TYPE} | None = None) -> bool: ...

# Time duration constants
NANOSECOND: float
MICROSECOND: float
MILLISECOND: float
SECOND: int
MINUTE: int
HOUR: int
DAY: int
WEEK: int

# Attachment API
def attachment_get(attachment_id: str) -> dict[str, object] | None: ...
def attachment_read(attachment_id: str) -> str | None: ...
def attachment_create(content: bytes | str, filename: str, description: str = "", mime_type: str = "application/octet-stream") -> dict[str, object]: ...

# wake_llm
def wake_llm(context: dict[str, object] | str, include_event: bool = True) -> None: ...

# Tools introspection API
def tools_list() -> list[dict[str, object]]: ...
def tools_get(tool_name: str) -> dict[str, object] | None: ...
def tools_execute(tool_name: str, *args: object, **kwargs: object) -> object: ...
def tools_execute_json(tool_name: str, args_json: str) -> object: ...
"""


def _json_schema_to_type(schema: ToolPropertySchema) -> str:
    """Convert a JSON Schema property to a Python type annotation."""
    schema_type = schema.get("type", "object")

    if isinstance(schema_type, list):
        types = [_JSON_SCHEMA_TYPE_MAP.get(t, "object") for t in schema_type]
        return " | ".join(types)

    return _JSON_SCHEMA_TYPE_MAP.get(schema_type, "object")


def generate_tool_stub(tool_def: ToolDefinition) -> str:
    """Generate a type stub for a single tool definition.

    Produces a function signature like:
        def search_notes(*, query: str) -> str: ...

    All tool parameters are keyword-only since that's how scripts call them.
    """
    function = tool_def.get("function", {})
    name = function.get("name", "unknown")
    params = function.get("parameters", {})
    properties = params.get("properties", {})
    required = set(params.get("required", []))

    parts = ["*"]
    for param_name, param_schema in properties.items():
        type_hint = _json_schema_to_type(param_schema)
        if param_name in required:
            parts.append(f"{param_name}: {type_hint}")
        else:
            parts.append(f"{param_name}: {type_hint} | None = None")

    params_str = ", ".join(parts) if len(parts) > 1 else ""
    return f"def {name}({params_str}) -> str: ..."


def generate_tool_stubs(tool_definitions: list[ToolDefinition]) -> str:
    """Generate type stubs for all tool definitions.

    Also generates tool_ prefixed variants for each tool.
    """
    lines: list[str] = []
    for tool_def in tool_definitions:
        stub = generate_tool_stub(tool_def)
        lines.append(stub)

        function = tool_def.get("function", {})
        name = function.get("name", "unknown")
        prefixed_stub = stub.replace(f"def {name}(", f"def tool_{name}(", 1)
        lines.append(prefixed_stub)

    return "\n".join(lines)


def generate_prefix_code(
    tool_definitions: list[ToolDefinition] | None = None,
    include_apis: bool = True,
    include_tools_api: bool = True,
) -> str:
    """Generate the full prefix code for type checking a Monty script.

    Args:
        tool_definitions: Tool definitions to generate stubs for.
        include_apis: Whether to include time/JSON/attachment API stubs.
        include_tools_api: Whether to include tools_list/tools_get/etc stubs.

    Returns:
        Python code string suitable for Monty.type_check(prefix_code=...).
    """
    parts: list[str] = []

    if include_apis:
        parts.append(_STATIC_STUBS)
    elif include_tools_api:
        # Extract just the tools API portion from static stubs
        tools_api_section = "\n".join(
            line for line in _STATIC_STUBS.split("\n") if line.startswith("def tools_")
        )
        if tools_api_section:
            parts.append(tools_api_section)

    if tool_definitions:
        parts.append(generate_tool_stubs(tool_definitions))

    return "\n".join(parts)
