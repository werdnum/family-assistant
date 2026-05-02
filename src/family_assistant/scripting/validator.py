"""
Script validation using Monty's static type checker.

Generates Python type stubs for all available external functions (tools, APIs)
and runs Monty's type_check() to catch errors before execution.
"""

import keyword
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pydantic_monty

from .config import ScriptConfig

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

    if isinstance(json_type, list):
        types = [_JSON_TYPE_MAP.get(t, "Any") for t in json_type if isinstance(t, str)]
        return " | ".join(types) if types else "Any"

    return _JSON_TYPE_MAP.get(json_type, "Any") if isinstance(json_type, str) else "Any"


logger = logging.getLogger(__name__)


def _is_valid_identifier(name: str) -> bool:
    """Check if a name is a valid Python identifier and not a keyword."""
    return name.isidentifier() and not keyword.iskeyword(name)


def _generate_tool_stub(name: str, parameters: Mapping[str, Any]) -> str:
    """Generate a Python function stub from a tool's JSON Schema parameters.

    Produces a stub like:
        def tool_name(req1: str, req2: int, *, opt1: str = ...) -> Any: ...

    Required params are positional in the order specified by the JSON Schema
    ``required`` list (matching runtime behavior where positional args are
    mapped to required params in order). Optional params are keyword-only
    (after ``*``) since runtime ignores extra positional args.

    Returns empty string if the name is not a valid Python identifier.
    Falls back to **kwargs if any parameter name is invalid.
    """
    if not _is_valid_identifier(name):
        return ""

    properties = parameters.get("properties", {})
    required_list: list[str] = parameters.get("required", [])
    required_set = set(required_list)

    # Validate all identifiers first
    for param_name in properties:
        if not _is_valid_identifier(param_name):
            return f"def {name}(**kwargs: Any) -> Any: ..."

    # Required params in the order specified by the "required" list
    required_params: list[str] = []
    for param_name in required_list:
        if param_name in properties:
            py_type = _json_schema_to_python_type(properties[param_name])
            required_params.append(f"{param_name}: {py_type}")

    # Optional params are keyword-only (runtime only maps positional args to required)
    optional_params: list[str] = []
    for param_name, param_schema in properties.items():
        if param_name not in required_set:
            py_type = _json_schema_to_python_type(param_schema)
            optional_params.append(f"{param_name}: {py_type} = ...")

    parts = required_params
    if optional_params:
        parts = required_params + ["*"] + optional_params
    param_str = ", ".join(parts)

    return f"def {name}({param_str}) -> Any: ..."


def _generate_builtin_api_stubs(
    include_tools_api: bool = True,
    include_attachment_api: bool = True,
    include_json_api: bool = True,
    include_time_api: bool = True,
    include_llm_api: bool = True,
) -> str:
    """Generate type stubs for all built-in APIs (time, JSON, LLM, etc.)."""
    lines: list[str] = []

    # JSON API
    if include_json_api:
        lines.append("def json_encode(obj: Any) -> str: ...")
        lines.append("def json_decode(value: Any) -> Any: ...")
        lines.append("")

    # Base64 API (always available)
    lines.append("def base64_encode(data: str | bytes) -> str: ...")
    lines.append("def base64_decode(data: str) -> str: ...")
    lines.append("def base64_decode_bytes(data: str) -> bytes: ...")
    lines.append("")

    # LLM API
    if include_llm_api:
        lines.append(
            "def llm(prompt: str, system: str | None = None, model: str | None = None) -> str: ..."
        )
        lines.append(
            "def llm_json(prompt: str, schema: dict[str, Any] | None = None, system: str | None = None, model: str | None = None) -> dict[str, Any]: ..."
        )
        lines.append("")

    # Attachment API (only when attachment registry is available at runtime)
    if include_attachment_api:
        lines.append(
            "def attachment_get(attachment_id: str) -> dict[str, Any] | None: ..."
        )
        lines.append("def attachment_read(attachment_id: str) -> str | None: ...")
        lines.append(
            "def attachment_read_bytes(attachment_id: str) -> bytes | None: ..."
        )
        lines.append(
            "def attachment_create(content: bytes | str, filename: str, description: str = ..., mime_type: str = ...) -> dict[str, Any]: ..."
        )
        lines.append("")

    # Tools meta-API (only when tools_provider is available at runtime)
    if include_tools_api:
        lines.append("def tools_list() -> list[dict[str, Any]]: ...")
        lines.append("def tools_get(tool_name: str) -> dict[str, Any] | None: ...")
        lines.append(
            "def tools_execute(tool_name: str, *args: Any, **kwargs: Any) -> Any: ..."
        )
        lines.append(
            "def tools_execute_json(tool_name: str, args_json: str) -> Any: ..."
        )
        lines.append("")

    # Time API - creation (aligned with scripting/apis/time.py)
    if include_time_api:
        lines.append("def time_now(tz: str = ...) -> dict[str, Any]: ...")
        lines.append("def time_now_utc() -> dict[str, Any]: ...")
        lines.append(
            "def time_create(year: int = ..., month: int = ..., day: int = ..., "
            "hour: int = ..., minute: int = ..., second: int = ..., "
            "nanosecond: int = ..., timezone_name: str = ...) -> dict[str, Any]: ..."
        )
        lines.append(
            "def time_from_timestamp(seconds: float, nanoseconds: int = ..., "
            "tz: str = ...) -> dict[str, Any]: ..."
        )
        lines.append(
            "def time_parse(time_string: str, format_string: str = ..., "
            "timezone_name: str = ...) -> dict[str, Any]: ..."
        )

        # Time API - manipulation
        lines.append(
            "def time_in_location(time_dict: dict[str, Any], timezone_name: str) -> dict[str, Any]: ..."
        )
        lines.append(
            "def time_format(time_dict: dict[str, Any], format_string: str) -> str: ..."
        )
        lines.append(
            "def time_add(time_dict: dict[str, Any], seconds: float) -> dict[str, Any]: ..."
        )
        lines.append(
            "def time_add_duration(time_dict: dict[str, Any], amount: float, unit: str) -> dict[str, Any]: ..."
        )

        # Time API - components
        for comp in ["year", "month", "day", "hour", "minute", "second"]:
            lines.append(f"def time_{comp}(time_dict: dict[str, Any]) -> int: ...")
        lines.append("def time_weekday(time_dict: dict[str, Any]) -> int: ...")

        # Time API - comparison
        lines.append(
            "def time_before(t1: dict[str, Any], t2: dict[str, Any]) -> bool: ..."
        )
        lines.append(
            "def time_after(t1: dict[str, Any], t2: dict[str, Any]) -> bool: ..."
        )
        lines.append(
            "def time_equal(t1: dict[str, Any], t2: dict[str, Any]) -> bool: ..."
        )
        lines.append(
            "def time_diff(t1: dict[str, Any], t2: dict[str, Any]) -> float: ..."
        )

        # Time API - duration
        lines.append("def duration_parse(duration_string: str) -> float: ...")
        lines.append("def duration_human(seconds: float) -> str: ...")

        # Time API - timezone
        lines.append("def timezone_is_valid(timezone_name: str) -> bool: ...")
        lines.append(
            "def timezone_offset(timezone_name: str, time_dict: dict[str, Any] | None = ...) -> int: ..."
        )

        # Time API - utility
        lines.append(
            "def is_between(start_hour: int, end_hour: int, time_dict: dict[str, Any] | None = ...) -> bool: ..."
        )
        lines.append(
            "def is_weekend(time_dict: dict[str, Any] | None = ...) -> bool: ..."
        )
        lines.append("")

        # Duration constants (seconds-based floats)
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
            lines.append(f"{name}: float")
        lines.append("")

    return "\n".join(lines)


def generate_prefix_code(
    tool_definitions: Sequence[Mapping[str, Any]] | None = None,
    input_names: list[str] | None = None,
    extra_external_functions: list[str] | None = None,
    include_tools_api: bool = True,
    include_attachment_api: bool = True,
    include_json_api: bool = True,
    include_time_api: bool = True,
    include_llm_api: bool = True,
) -> str:
    """Generate prefix_code for Monty type_check().

    Args:
        tool_definitions: Tool definitions to generate stubs for.
        input_names: Names of input variables (typed as Any).
        extra_external_functions: Additional callable names needing generic stubs.
        include_tools_api: Whether tools_* functions are available at runtime.
        include_attachment_api: Whether attachment_* functions are available at runtime.
        include_json_api: Whether json_encode/json_decode are available at runtime.
        include_time_api: Whether time_*/duration_* helpers are available at runtime.
        include_llm_api: Whether llm()/llm_json() are available at runtime.

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

    parts.append(
        _generate_builtin_api_stubs(
            include_tools_api=include_tools_api,
            include_attachment_api=include_attachment_api,
            include_json_api=include_json_api,
            include_time_api=include_time_api,
            include_llm_api=include_llm_api,
        )
    )

    # Input variables (globals injected at runtime)
    if input_names:
        parts.append("# Input variables")
        for name in input_names:
            if _is_valid_identifier(name):
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

    # Extra callable names (e.g. callable globals injected at runtime)
    if extra_external_functions:
        parts.append("# Extra callable globals")
        for name in extra_external_functions:
            if _is_valid_identifier(name):
                parts.append(f"def {name}(*args: Any, **kwargs: Any) -> Any: ...")
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
        extra_external_functions: list[str] | None = None,
        include_tools_api: bool = True,
        include_attachment_api: bool = True,
    ) -> ValidationResult:
        """Validate a script using static type checking.

        The set of built-in APIs (json, time, llm) made visible to the type
        checker is taken from ``self.config`` so that validation runs in the
        same environment as execution.

        Args:
            script: The script source code.
            input_names: Names of globals that will be injected at runtime.
            extra_external_functions: Additional callable names available at runtime
                (e.g. callable globals injected by the caller).
            include_tools_api: Whether tools_* functions are available at runtime.
            include_attachment_api: Whether attachment_* functions are available at runtime.

        Returns:
            ValidationResult with any diagnostics found.
        """
        diagnostics: list[ValidationDiagnostic] = []

        # Build the Monty instance (catches syntax errors)
        try:
            m = pydantic_monty.Monty(
                script,
                inputs=input_names or [],
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
            extra_external_functions=extra_external_functions,
            include_tools_api=include_tools_api,
            include_attachment_api=include_attachment_api,
            include_json_api=self.config.enable_json_api,
            include_time_api=self.config.enable_time_api,
            include_llm_api=self.config.enable_llm_api,
        )

        # Run type checking — let infrastructure errors (RuntimeError) propagate
        try:
            m.type_check(type_check_stubs=prefix_code)
        except pydantic_monty.MontyTypingError as e:
            diagnostics.extend(_parse_typing_error(e))
            return ValidationResult(is_valid=False, diagnostics=diagnostics)
        except pydantic_monty.MontySyntaxError as e:
            # Syntax error in prefix_code (our generated stubs), not the user's script.
            # Fail closed: a broken stub is a bug that should be surfaced.
            logger.error("Syntax error in generated prefix code: %s", e)
            return ValidationResult(
                is_valid=False,
                diagnostics=[
                    ValidationDiagnostic(
                        message=f"Internal error: invalid type definitions: {e}",
                        severity="error",
                    )
                ],
            )

        return ValidationResult(is_valid=True, diagnostics=diagnostics)


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


def validate_script(
    script: str,
    tool_definitions: Sequence[Mapping[str, Any]] | None = None,
    input_names: list[str] | None = None,
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
    )
