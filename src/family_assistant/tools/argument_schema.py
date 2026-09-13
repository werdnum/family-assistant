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
import re
from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, validators
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

TOOL_ARGUMENT_VALIDATOR = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "attachment", lambda _checker, instance: isinstance(instance, str)
    ),
)

_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef"})
_EXTRA_KEY_KEYWORDS = frozenset({
    "additionalProperties",
    "patternProperties",
    "unevaluatedProperties",
})


def check_parameter_schema(parameters: Mapping[str, object]) -> None:
    """Raise ``jsonschema.SchemaError`` unless ``parameters`` is a usable schema.

    Called where tool definitions enter the process — a local provider's
    constructor, an MCP server's discovery — so a tool that cannot be
    validated against fails there, at startup, rather than when a model first
    calls it. That covers the meta-schema and every ``$ref`` or ``$dynamicRef`` the
    schema makes:
    the meta-schema only checks a reference's shape, and one pointing nowhere
    would otherwise surface as an exception on the first argument that
    reaches it. The ``attachment`` type is checked as a string, which is what
    it is on the wire.
    """
    schema = _with_attachments_as_strings(parameters)
    Draft202012Validator.check_schema(schema)
    resolver = (
        Registry()
        .with_resource(
            "", Resource.from_contents(schema, default_specification=DRAFT202012)
        )
        .resolver()
    )
    for ref in _references(schema):
        try:
            resolver.lookup(ref)
        except Unresolvable as exc:
            msg = f"$ref {ref!r} cannot be resolved"
            raise SchemaError(msg) from exc


def _references(node: object) -> Iterator[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in _REFERENCE_KEYWORDS and isinstance(value, str):
                yield value
            else:
                yield from _references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _references(item)


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
    used and is almost always an invented one. "Declared" is what the schema
    evaluates, so a property reached through ``$ref``, ``allOf`` or another
    applicator counts; the check is JSON Schema's own
    ``unevaluatedProperties``, applied only when the schema has a top-level
    ``properties`` map and says nothing itself about extra keys.

    ``parameters`` is expected to have passed ``check_parameter_schema``.
    """
    schema = dict(parameters)
    if isinstance(schema.get("properties"), Mapping) and not (
        _EXTRA_KEY_KEYWORDS & schema.keys()
    ):
        schema["unevaluatedProperties"] = False
    errors = sorted(
        TOOL_ARGUMENT_VALIDATOR(schema).iter_errors(arguments),
        key=lambda error: list(error.path),
    )
    return [_describe(error) for error in errors]


def _describe(error: object) -> str:
    path = ".".join(str(part) for part in getattr(error, "path", ()))
    message = getattr(error, "message", str(error))
    if not path and getattr(error, "validator", None) in {
        "unevaluatedProperties",
        "additionalProperties",
    }:
        names = re.findall(r"'([^']*)'", message)
        if names:
            listed = ", ".join(f"'{name}'" for name in names)
            verb = "is not an argument" if len(names) == 1 else "are not arguments"
            return f"{listed} {verb} of this tool"
    return f"'{path}': {message}" if path else message
