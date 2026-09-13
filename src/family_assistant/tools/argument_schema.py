"""Check a tool's arguments against its declared parameter schema.

Tool definitions carry their argument schema in OpenAI function-calling
format, which is JSON Schema with one project-specific extension: a
``type: attachment`` parameter (see ``tools/attachment_utils.py``) that is an
attachment id on the wire. Plain jsonschema rejects that while checking the
*schema*, so the validator here teaches it that an attachment is a string
rather than skipping every tool that takes one.
"""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator, SchemaError, validators
from jsonschema.exceptions import UnknownType

TOOL_ARGUMENT_VALIDATOR = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "attachment", lambda _checker, instance: isinstance(instance, str)
    ),
)


def argument_schema_errors(
    parameters: Mapping[str, object], arguments: Mapping[str, object]
) -> list[str]:
    """Return every way ``arguments`` fails ``parameters``, empty if none.

    A top-level argument the schema does not declare is an error even though
    JSON Schema permits undeclared properties by default: a tool's arguments
    are bound to a function signature, so an undeclared name can never be
    used and is almost always an invented one. A schema that opts in with
    ``additionalProperties`` or ``patternProperties`` keeps its extra keys.

    Raises ``jsonschema.SchemaError`` when ``parameters`` names a type the
    validator does not know, which is the tool's defect rather than the
    arguments'. The meta-schema is deliberately not checked first: it would
    reject the ``attachment`` type this validator exists to accept.
    """
    try:
        errors = sorted(
            TOOL_ARGUMENT_VALIDATOR(parameters).iter_errors(arguments),
            key=lambda error: list(error.path),
        )
    except UnknownType as exc:
        raise SchemaError(str(exc)) from exc
    messages = [_describe(error) for error in errors]
    declared = parameters.get("properties")
    permits_extra = parameters.get("additionalProperties") or parameters.get(
        "patternProperties"
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
