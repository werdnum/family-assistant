"""Check a tool's arguments against its declared parameter schema.

Tool definitions carry their argument schema in OpenAI function-calling
format, which is JSON Schema with one project-specific extension: a
``type: attachment`` parameter (see ``tools/attachment_utils.py``) that is an
attachment id on the wire. Plain jsonschema rejects that while checking the
*schema*, so the validator here teaches it that an attachment is a string
rather than skipping every tool that takes one.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, validators

TOOL_ARGUMENT_VALIDATOR = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "attachment", lambda _checker, instance: isinstance(instance, str)
    ),
)


def check_parameter_schema(parameters: Mapping[str, object]) -> None:
    """Raise ``jsonschema.SchemaError`` unless ``parameters`` is a usable schema.

    Called where tool definitions enter the process — a local provider's
    constructor, an MCP server's discovery — so a tool that cannot be
    validated against fails there, at startup, rather than when a model first
    calls it. The ``attachment`` type is checked as a string, which is what it
    is on the wire.
    """
    Draft202012Validator.check_schema(_with_attachments_as_strings(parameters))


# ast-grep-ignore: no-dict-any - JSON Schema is arbitrary nested JSON
def _with_attachments_as_strings(schema: Mapping[str, object]) -> dict[str, Any]:
    return {
        key: ("string" if key == "type" and value == "attachment" else value)
        if key == "type"
        else _rewrite(value)
        for key, value in schema.items()
    }


def _rewrite(schema: object) -> object:
    if isinstance(schema, Mapping):
        return _with_attachments_as_strings(schema)
    if isinstance(schema, list):
        return [_rewrite(item) for item in schema]
    return copy.deepcopy(schema)


def argument_schema_errors(
    parameters: Mapping[str, object], arguments: Mapping[str, object]
) -> list[str]:
    """Return every way ``arguments`` fails ``parameters``, empty if none.

    A top-level argument the schema does not declare is an error even though
    JSON Schema permits undeclared properties by default: a tool's arguments
    are bound to a function signature, so an undeclared name can never be
    used and is almost always an invented one. A schema that opts in with
    ``additionalProperties`` (anything but ``false``, including the empty
    schema ``{}``) or ``patternProperties`` keeps its extra keys.

    ``parameters`` is expected to have passed ``check_parameter_schema``.
    """
    errors = sorted(
        TOOL_ARGUMENT_VALIDATOR(parameters).iter_errors(arguments),
        key=lambda error: list(error.path),
    )
    messages = [_describe(error) for error in errors]
    declared = parameters.get("properties")
    permits_extra = (
        parameters.get("additionalProperties", False) is not False
        or "patternProperties" in parameters
    )
    if isinstance(declared, Mapping) and not permits_extra:
        messages.extend(
            f"'{name}' is not an argument of this tool"
            for name in arguments
            if name not in declared
        )
    return messages


def _describe(error: object) -> str:
    path = ".".join(str(part) for part in getattr(error, "path", ()))
    message = getattr(error, "message", str(error))
    return f"'{path}': {message}" if path else message
