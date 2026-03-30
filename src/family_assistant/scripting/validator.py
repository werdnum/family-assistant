"""
Script validation using Monty's static type checker.

Generates Python type stubs for all available external functions (tools, APIs)
and runs Monty's type_check() to catch errors before execution.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pydantic_monty

from .config import ScriptConfig

logger = logging.getLogger(__name__)


# JSON Schema type -> Python type annotation mapping
_JSON_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


@dataclass
class ValidationDiagnostic:
    """A single validation diagnostic (error or warning)."""

    message: str
    line: int | None = None
    severity: str = "error"  # "error" or "warning"

    def __str__(self) -> str:
        loc = f" at line {self.line}" if self.line else ""
        return f"{self.severity}{loc}: {self.message}"


@dataclass
class ValidationResult:
    """Result of script validation."""

    is_valid: bool
    diagnostics: list[ValidationDiagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def error_message(self) -> str | None:
        """Single error message for backward compatibility."""
        if not self.errors:
            return None
        return "; ".join(str(e) for e in self.errors)


def _json_schema_to_python_type(schema: Mapping[str, Any]) -> str:
    """Convert a JSON Schema type to a Python type annotation string."""
    if not schema:
        return "Any"

    json_type = schema.get("type", "")

    if "enum" in schema:
        return "str" if all(isinstance(v, str) for v in schema["enum"]) else "Any"

    if json_type == "array":
        items = schema.get("items", {})
        item_type = _json_schema_to_python_type(items)
        return f"list[{item_type}]"

    if json_type == "object":
        return "dict[str, Any]"

    return _JSON_TYPE_MAP.get(json_type, "Any")


def _generate_tool_stub(name: str, parameters: Mapping[str, Any]) -> str:
    """Generate a Python function stub from a tool's JSON Schema parameters.

    Produces a stub like:
        def tool_name(*, param1: str, param2: int = ...) -> Any: ...
    """
    properties = parameters.get("properties", {})
    required = set(parameters.get("required", []))

    params: list[str] = []
    for param_name, param_schema in properties.items():
        py_type = _json_schema_to_python_type(param_schema)
        if param_name in required:
            params.append(f"{param_name}: {py_type}")
        else:
            params.append(f"{param_name}: {py_type} = ...")

    # Use keyword-only args since tools are called with kwargs
    param_str = ", ".join(params)
    if param_str:
        param_str = "*, " + param_str

    return f"def {name}({param_str}) -> Any: ..."


def _generate_builtin_api_stubs() -> str:
    """Generate type stubs for all built-in APIs (time, JSON, LLM, etc.)."""
    lines: list[str] = []

    # JSON API
    lines.append("def json_encode(obj: Any) -> str: ...")
    lines.append("def json_decode(value: Any) -> Any: ...")
    lines.append("")

    # LLM API
    lines.append(
        "def llm(prompt: str, system: str | None = None, model: str | None = None) -> str: ..."
    )
    lines.append(
        "def llm_json(prompt: str, schema: dict[str, Any] | None = None, system: str | None = None, model: str | None = None) -> dict[str, Any]: ..."
    )
    lines.append("")

    # Attachment API
    lines.append("def attachment_get(attachment_id: str) -> dict[str, Any] | None: ...")
    lines.append("def attachment_read(attachment_id: str) -> str | None: ...")
    lines.append(
        "def attachment_create(content: bytes | str, filename: str, description: str = ..., mime_type: str = ...) -> dict[str, Any]: ..."
    )
    lines.append("")

    # Tools meta-API
    lines.append("def tools_list() -> list[dict[str, Any]]: ...")
    lines.append("def tools_get(tool_name: str) -> dict[str, Any] | None: ...")
    lines.append(
        "def tools_execute(tool_name: str, *args: Any, **kwargs: Any) -> Any: ..."
    )
    lines.append("def tools_execute_json(tool_name: str, args_json: str) -> Any: ...")
    lines.append("")

    # Time API - creation
    lines.append("def time_now() -> dict[str, Any]: ...")
    lines.append("def time_now_utc() -> dict[str, Any]: ...")
    lines.append(
        "def time_create(year: int = ..., month: int = ..., day: int = ..., hour: int = ..., minute: int = ..., second: int = ..., nanosecond: int = ..., timezone: str = ...) -> dict[str, Any]: ..."
    )
    lines.append(
        "def time_from_timestamp(timestamp: int | float) -> dict[str, Any]: ..."
    )
    lines.append("def time_parse(value: str, format: str = ...) -> dict[str, Any]: ...")

    # Time API - manipulation
    lines.append(
        "def time_in_location(t: dict[str, Any], timezone: str) -> dict[str, Any]: ..."
    )
    lines.append("def time_format(t: dict[str, Any], format: str = ...) -> str: ...")
    lines.append(
        "def time_add(t: dict[str, Any], duration: int) -> dict[str, Any]: ..."
    )
    lines.append(
        "def time_add_duration(t: dict[str, Any], years: int = ..., months: int = ..., days: int = ..., hours: int = ..., minutes: int = ..., seconds: int = ...) -> dict[str, Any]: ..."
    )

    # Time API - components
    for comp in ["year", "month", "day", "hour", "minute", "second"]:
        lines.append(f"def time_{comp}(t: dict[str, Any]) -> int: ...")
    lines.append("def time_weekday(t: dict[str, Any]) -> int: ...")

    # Time API - comparison
    lines.append("def time_before(a: dict[str, Any], b: dict[str, Any]) -> bool: ...")
    lines.append("def time_after(a: dict[str, Any], b: dict[str, Any]) -> bool: ...")
    lines.append("def time_equal(a: dict[str, Any], b: dict[str, Any]) -> bool: ...")
    lines.append("def time_diff(a: dict[str, Any], b: dict[str, Any]) -> int: ...")

    # Time API - duration
    lines.append("def duration_parse(s: str) -> int: ...")
    lines.append("def duration_human(nanoseconds: int) -> str: ...")

    # Time API - timezone
    lines.append("def timezone_is_valid(timezone: str) -> bool: ...")
    lines.append("def timezone_offset(timezone: str) -> str: ...")

    # Time API - utility
    lines.append(
        "def is_between(t: dict[str, Any], start: dict[str, Any], end: dict[str, Any]) -> bool: ..."
    )
    lines.append("def is_weekend(t: dict[str, Any]) -> bool: ...")
    lines.append("")

    # Duration constants
    for name in [
        "NANOSECOND",
        "MICROSECOND",
        "MILLISECOND",
        "SECOND",
        "MINUTE",
        "HOUR",
        "DAY",
        "WEEK",
    ]:
        lines.append(f"{name}: int")
    lines.append("")

    return "\n".join(lines)


def generate_prefix_code(
    tool_definitions: Sequence[Mapping[str, Any]] | None = None,
    input_names: list[str] | None = None,
    include_apis: bool = True,
) -> str:
    """Generate prefix_code for Monty type_check().

    Args:
        tool_definitions: Tool definitions to generate stubs for.
        input_names: Names of input variables (typed as Any).
        include_apis: Whether to include built-in API stubs.

    Returns:
        Python stub code declaring all available names.
    """
    parts: list[str] = []

    # Always include core stubs (wake_llm and print are always available)
    parts.append("from typing import Any")
    parts.append(
        "def wake_llm(context: dict[str, Any] | str, include_event: bool = True) -> None: ..."
    )
    parts.append("def print(*args: Any) -> None: ...")
    parts.append("")

    if include_apis:
        parts.append(_generate_builtin_api_stubs())

    # Input variables (globals injected at runtime)
    if input_names:
        parts.append("# Input variables")
        for name in input_names:
            parts.append(f"{name}: Any")
        parts.append("")

    # Tool function stubs
    if tool_definitions:
        parts.append("# Tool functions")
        for tool_def in tool_definitions:
            function = tool_def.get("function", {})
            name = function.get("name", "")
            if not name:
                continue
            parameters = function.get("parameters", {})
            stub = _generate_tool_stub(name, parameters)
            parts.append(stub)
            # Also generate tool_<name> alias
            prefixed_stub = _generate_tool_stub(f"tool_{name}", parameters)
            parts.append(prefixed_stub)
        parts.append("")

    return "\n".join(parts)


class ScriptValidator:
    """Validates scripts using Monty's static type checker.

    Generates type stubs for all available external functions and runs
    Monty's type_check() to catch errors before execution.
    """

    def __init__(
        self,
        tool_definitions: Sequence[Mapping[str, Any]] | None = None,
        config: ScriptConfig | None = None,
    ) -> None:
        self.tool_definitions = tool_definitions
        self.config = config or ScriptConfig()

    def validate(
        self,
        script: str,
        input_names: list[str] | None = None,
        include_apis: bool = True,
    ) -> ValidationResult:
        """Validate a script using static type checking.

        Args:
            script: The script source code.
            input_names: Names of globals that will be injected at runtime.
            include_apis: Whether built-in APIs are available.

        Returns:
            ValidationResult with any diagnostics found.
        """
        diagnostics: list[ValidationDiagnostic] = []

        # Respect config.disable_apis
        effective_include_apis = include_apis and not self.config.disable_apis

        # Collect all external function names for Monty
        ext_fn_names = self._collect_external_function_names(
            input_names, effective_include_apis
        )

        # Build the Monty instance (catches syntax errors)
        try:
            m = pydantic_monty.Monty(
                script,
                inputs=input_names or [],
                external_functions=ext_fn_names,
            )
        except pydantic_monty.MontySyntaxError as e:
            line = None
            match = re.search(r"line (\d+)", str(e))
            if match:
                line = int(match.group(1))
            diagnostics.append(
                ValidationDiagnostic(
                    message=f"Syntax error: {e}",
                    line=line,
                    severity="error",
                )
            )
            return ValidationResult(is_valid=False, diagnostics=diagnostics)

        # Generate prefix_code with type stubs
        prefix_code = generate_prefix_code(
            tool_definitions=self.tool_definitions,
            input_names=input_names,
            include_apis=effective_include_apis,
        )

        # Run type checking
        try:
            m.type_check(prefix_code=prefix_code)
        except pydantic_monty.MontyTypingError as e:
            # Parse diagnostics from the error
            diagnostics.extend(_parse_typing_error(e))
            return ValidationResult(is_valid=False, diagnostics=diagnostics)
        except RuntimeError as e:
            # type_check infrastructure failure - log but don't block
            logger.warning("Type check infrastructure error: %s", e)
            return ValidationResult(
                is_valid=True,
                diagnostics=[
                    ValidationDiagnostic(
                        message=f"Type checking unavailable: {e}",
                        severity="warning",
                    )
                ],
            )

        return ValidationResult(is_valid=True, diagnostics=diagnostics)

    def _collect_external_function_names(
        self,
        input_names: list[str] | None,
        include_apis: bool,
    ) -> list[str]:
        """Collect all external function names that Monty should know about."""
        names: list[str] = []

        # wake_llm is always available
        names.append("wake_llm")

        if include_apis:
            names.extend([
                "json_encode",
                "json_decode",
                "llm",
                "llm_json",
                "time_now",
                "time_now_utc",
                "time_create",
                "time_from_timestamp",
                "time_parse",
                "time_in_location",
                "time_format",
                "time_add",
                "time_add_duration",
                "time_year",
                "time_month",
                "time_day",
                "time_hour",
                "time_minute",
                "time_second",
                "time_weekday",
                "time_before",
                "time_after",
                "time_equal",
                "time_diff",
                "duration_parse",
                "duration_human",
                "timezone_is_valid",
                "timezone_offset",
                "is_between",
                "is_weekend",
                "attachment_get",
                "attachment_read",
                "attachment_create",
                "tools_list",
                "tools_get",
                "tools_execute",
                "tools_execute_json",
            ])

        # Tool names (direct and tool_-prefixed)
        if self.tool_definitions:
            for tool_def in self.tool_definitions:
                name = tool_def.get("function", {}).get("name", "")
                if name:
                    names.append(name)
                    names.append(f"tool_{name}")

        # Filter out any input names (they're inputs, not functions)
        input_set = set(input_names or [])
        return [n for n in names if n not in input_set]


def _parse_typing_error(
    error: pydantic_monty.MontyTypingError,
) -> list[ValidationDiagnostic]:
    """Parse a MontyTypingError into structured diagnostics."""
    diagnostics: list[ValidationDiagnostic] = []
    error_text = error.display(format="full", color=False)

    # Parse individual error blocks (e.g. "error[rule-name]: message\n --> file:line:col")
    pattern = re.compile(
        r"(error|warning)\[([^\]]+)\]:\s*(.+?)(?=\n\s*-->|\Z)",
        re.DOTALL,
    )
    line_pattern = re.compile(r"-->\s*\S+:(\d+):\d+")

    # Split by "error[" or "warning[" boundaries
    blocks = re.split(r"(?=(?:error|warning)\[)", error_text)

    for raw_block in blocks:
        block = raw_block.strip()
        if not block:
            continue

        severity_match = pattern.match(block)
        if not severity_match:
            # Fallback: use whole block as one diagnostic
            if block:
                diagnostics.append(
                    ValidationDiagnostic(
                        message=block.strip(),
                        severity="error",
                    )
                )
            continue

        severity = severity_match.group(1)
        message = severity_match.group(3).strip()

        line = None
        line_match = line_pattern.search(block)
        if line_match:
            line = int(line_match.group(1))

        diagnostics.append(
            ValidationDiagnostic(
                message=message,
                line=line,
                severity=severity,
            )
        )

    if not diagnostics:
        # Fallback if parsing failed
        diagnostics.append(
            ValidationDiagnostic(
                message=str(error),
                severity="error",
            )
        )

    return diagnostics


async def validate_script(
    script: str,
    tool_definitions: Sequence[Mapping[str, Any]] | None = None,
    input_names: list[str] | None = None,
    include_apis: bool = True,
    config: ScriptConfig | None = None,
) -> ValidationResult:
    """Convenience function to validate a script.

    This is the primary entry point for script validation.
    """
    validator = ScriptValidator(
        tool_definitions=tool_definitions,
        config=config,
    )
    return validator.validate(
        script,
        input_names=input_names,
        include_apis=include_apis,
    )
