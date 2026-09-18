"""Resolve script inputs and describe invocation-local execution authority."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, TypedDict

from family_assistant.scripting.apis.keychute import (
    get_keychute_config,
    keychute_external_function_names,
)
from family_assistant.scripting.validator import ScriptValidator
from family_assistant.security.definition_records import (
    DefinitionResolution,
    resolve_definition_record,
    script_definition_content,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class ScriptInvocationArguments(TypedDict, total=False):
    """Public script invocation inputs before source resolution."""

    script: str | None
    globals: dict[str, object] | None
    name: str | None
    parameters: dict[str, object] | None


class ScriptPreparationError(ValueError):
    """An invalid invocation, rendered identically by direct and gated callers."""

    def __init__(self, message: str, error_type: str = "validation_error") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class ScriptReviewContext:
    """Complete program evidence, never trusted reviewer instructions."""

    source: str
    inputs: dict[str, object]
    tools: tuple[ToolDefinition, ...]
    external_functions: tuple[str, ...]
    stored_name: str | None = None
    definition: DefinitionResolution | None = None
    policy: dict[str, object] = field(default_factory=dict)
    decision: str = "unreviewed"
    review_id: str | None = None
    approval_active: bool = False


def _copy_globals(values: dict[str, object]) -> dict[str, object]:
    """Pin data inputs while preserving Python callers' external function bindings."""
    return {
        key: value if callable(value) else copy.deepcopy(value)
        for key, value in values.items()
    }


@dataclass
class PreparedScriptInvocation:
    """Pinned execution payload and the review evidence that describes it."""

    review: ScriptReviewContext
    globals: dict[str, object]
    approved: bool = False

    def arguments(self) -> dict[str, object]:
        """Return the inline payload used for review, confirmation and execution."""
        return {"script": self.review.source, "globals": _copy_globals(self.globals)}


@dataclass
class ScriptExecutionScope:
    """Shared only by operations within one running program, not sibling calls."""

    invocation: PreparedScriptInvocation
    parent: ScriptExecutionScope | None = None
    active: bool = True
    revoked: bool = False

    @property
    def approved(self) -> bool:
        return self.active and not self.revoked and self.invocation.approved

    def revoke(self) -> None:
        """A new decision also invalidates every enclosing caller continuation."""
        self.revoked = True
        if self.parent is not None:
            self.parent.revoke()

    def review_contexts(self) -> tuple[ScriptReviewContext, ...]:
        """Keep enclosing source available even after its approval has ended."""
        parents = self.parent.review_contexts() if self.parent is not None else ()
        return (
            *parents,
            replace(self.invocation.review, approval_active=self.approved),
        )


async def prepare_script_invocation(
    context: ToolExecutionContext,
    script: str | None = None,
    globals: dict[str, object] | None = None,
    name: str | None = None,
    parameters: dict[str, object] | None = None,
    *,
    allow_external_script_apis: bool = True,
) -> PreparedScriptInvocation:
    """Resolve and validate without evaluating code or constructing external APIs."""
    if name and script:
        raise ScriptPreparationError(
            "Provide either 'script' (inline) or 'name' (stored), not both"
        )
    inputs = _copy_globals(globals or {})
    definition = None
    if name and not script:
        row = await context.db_context.scripts.get_by_name(name)
        if row is None:
            raise ScriptPreparationError(f"Script '{name}' not found", "not_found")
        script = row.script_code
        if row.parameters_schema:
            required = row.parameters_schema.get("required", [])
            if isinstance(required, list):
                for key in required:
                    if key not in (parameters or {}):
                        raise ScriptPreparationError(
                            f"Missing required parameter: {key}"
                        )
        inputs.update(_copy_globals(parameters or {}))
        definition = resolve_definition_record(
            row.definition_record,
            script_definition_content(
                name=row.name,
                description=row.description,
                script_code=row.script_code,
                parameters_schema=row.parameters_schema,
            ),
        )
    if not script:
        raise ScriptPreparationError(
            "Either 'script' (inline code) or 'name' (stored script) must be provided"
        )

    provider = context.tools_provider
    if provider is None and context.processing_service is not None:
        provider = context.processing_service.tools_provider
    definitions = await provider.get_tool_definitions() if provider is not None else []
    external = [key for key, value in inputs.items() if callable(value)]
    if allow_external_script_apis:
        external.extend(
            keychute_external_function_names(get_keychute_config(context)) or []
        )

    validation = ScriptValidator(tool_definitions=definitions).validate(
        script,
        input_names=[key for key, value in inputs.items() if not callable(value)],
        extra_external_functions=external,
        include_tools_api=provider is not None,
        include_attachment_api=bool(context.attachment_registry),
    )
    if not validation.is_valid:
        first = validation.errors[0] if validation.errors else None
        if first and first.message.startswith("Syntax error"):
            location = f" at line {first.line}" if first.line else ""
            raise ScriptPreparationError(
                f"Syntax error in script{location}: {first.message}", "syntax_error"
            )
        raise ScriptPreparationError(
            f"Script validation failed: {validation.error_message}"
        )
    return PreparedScriptInvocation(
        review=ScriptReviewContext(
            source=script,
            inputs=_copy_globals(inputs),
            tools=tuple(copy.deepcopy(definitions)),
            external_functions=tuple([
                *external,
                "llm",
                "llm_json",
                "wake_llm",
                "json_*",
                "time_*",
                "base64_*",
                *(["attachment_*"] if context.attachment_registry else []),
            ]),
            stored_name=name,
            definition=definition,
        ),
        globals=inputs,
    )
