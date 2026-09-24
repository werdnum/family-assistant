"""Resolve script inputs and describe invocation-local execution authority."""

from __future__ import annotations

import ast
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
from family_assistant.security.script_closure import (
    ScriptBinding,
    ScriptClosureError,
    resolve_script_closure,
)

if TYPE_CHECKING:
    from family_assistant.security.script_closure import ScriptClosure
    from family_assistant.storage.repositories.scripts import ScriptRow
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class ScriptInvocationArguments(TypedDict, total=False):
    """Public script invocation inputs before source resolution."""

    script: str | None
    globals: dict[str, object] | None
    name: str | None
    parameters: dict[str, object] | None
    script_bindings: list[dict[str, object]] | None


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
    script_bindings: tuple[dict[str, object], ...] = ()
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
    bound_child: bool = False

    def arguments(self) -> dict[str, object]:
        """Return the inline payload used for review, confirmation and execution."""
        arguments: dict[str, object] = {
            "script": self.review.source,
            "globals": _copy_globals(self.globals),
        }
        if self.review.script_bindings:
            arguments["script_bindings"] = copy.deepcopy(
                list(self.review.script_bindings)
            )
        return arguments


@dataclass
class ScriptExecutionScope:
    """Shared only by operations within one running program, not sibling calls."""

    invocation: PreparedScriptInvocation
    parent: ScriptExecutionScope | None = None
    active: bool = True
    model_output_received: bool = False

    def __post_init__(self) -> None:
        # A child can be handed its caller's model output as a parameter.
        if self.parent is not None and self.parent.model_output_received:
            self.model_output_received = True

    @property
    def approved(self) -> bool:
        if not self.active:
            return False
        if self.invocation.approved:
            return True
        return self._bound_parent is not None and self._bound_parent.approved

    @property
    def _bound_parent(self) -> ScriptExecutionScope | None:
        """The program this one is statically bound into, and so part of."""
        return self.parent if self.invocation.bound_child else None

    def note_model_output(self) -> None:
        """Record that a model or delegated agent has handed this run a result.

        A model can turn instructions it read into a well-formed destination,
        which raw untrusted data rarely is, so a destination chosen after this
        point is reviewed rather than inherited. Enclosing programs receive the
        result too, through whatever the child returns.
        """
        self.model_output_received = True
        if self.parent is not None:
            self.parent.note_model_output()

    @property
    def awaiting_program_review(self) -> bool:
        """Whether no review has yet decided this running program.

        A program admitted without a review of its own -- a scheduled firing,
        or an ``execute_script`` call no gate asked about -- is decided by the
        first model review one of its operations needs, once.
        """
        if not self.active or self.approved:
            return False
        if self._bound_parent is not None:
            return self._bound_parent.awaiting_program_review
        return self.invocation.review.decision == "unreviewed"

    def program_to_decide(self) -> ScriptExecutionScope:
        """The outermost program this one is statically bound into.

        A bound child is part of the program that names it, so a review that
        decides the child decides that whole program.
        """
        scope = self
        while scope._bound_parent is not None:
            scope = scope._bound_parent
        return scope

    def approve_program(self, review_id: str | None) -> None:
        """Record a model allow of the complete program as its approval."""
        self.invocation.approved = True
        self.invocation.review = replace(
            self.invocation.review, decision="allow", review_id=review_id
        )

    def record_program_decision(self, decision: str, review_id: str | None) -> None:
        """Record a review that decided the program without approving it."""
        self.invocation.review = replace(
            self.invocation.review, decision=decision, review_id=review_id
        )

    def program_string_literals(self) -> frozenset[str]:
        """Complete string literals written in the reviewed program's source.

        Covers the program's own source and every hash-bound stored script in
        its closure. Parts of f-strings are not complete literals, and inputs
        are values rather than code, so neither counts.
        """
        review = self.invocation.review
        sources = (
            review.source,
            *(
                str(binding.get("script_code", ""))
                for binding in review.script_bindings
            ),
        )
        return frozenset(
            literal
            for source in sources
            for literal in _complete_string_literals(source)
        )

    def review_contexts(self) -> tuple[ScriptReviewContext, ...]:
        """Keep enclosing source available even after its approval has ended."""
        parents = self.parent.review_contexts() if self.parent is not None else ()
        return (
            *parents,
            replace(self.invocation.review, approval_active=self.approved),
        )


def _complete_string_literals(source: str) -> set[str]:
    """String constants that stand alone as an expression, never f-string parts."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    fragments = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for child in ast.walk(node)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in fragments
    }


async def _resolve_bound_closure(
    context: ToolExecutionContext,
    source: str,
    row: ScriptRow | None,
    supplied_bindings: list[dict[str, object]] | None,
    bound_child: bool,
) -> ScriptClosure:
    closure = await resolve_script_closure(context.db_context, source, loaded_root=row)
    if supplied_bindings is not None:
        closure.verify_bindings(supplied_bindings)
    if bound_child and context.script_execution is not None:
        inherited_bindings = {
            item["name"]: item
            for item in context.script_execution.invocation.review.script_bindings
        }
        for binding in closure.bindings:
            if inherited_bindings.get(binding.name) != binding.to_dict():
                raise ScriptClosureError(
                    f"Stored script '{binding.name}' changed since preparation; prepare and review again"
                )
    return closure


async def prepare_script_invocation(
    context: ToolExecutionContext,
    script: str | None = None,
    globals: dict[str, object] | None = None,
    name: str | None = None,
    parameters: dict[str, object] | None = None,
    *,
    script_bindings: list[dict[str, object]] | None = None,
    allow_external_script_apis: bool = True,
) -> PreparedScriptInvocation:
    """Resolve and validate without evaluating code or constructing external APIs."""
    if name and script:
        raise ScriptPreparationError(
            "Provide either 'script' (inline) or 'name' (stored), not both"
        )
    inputs = _copy_globals(globals or {})
    definition = None
    row = None
    bound_child = False
    if name and not script:
        row = await context.db_context.scripts.get_by_name(name)
        if row is None:
            raise ScriptPreparationError(f"Script '{name}' not found", "not_found")
        parent = context.script_execution
        if parent is not None:
            expected = next(
                (
                    binding
                    for binding in parent.invocation.review.script_bindings
                    if binding["name"] == name
                ),
                None,
            )
            if expected is not None:
                if ScriptBinding.from_row(row).to_dict() != expected:
                    raise ScriptPreparationError(
                        f"Stored script '{name}' changed since preparation; prepare and review again",
                        "stale_script_binding",
                    )
                bound_child = True
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
    try:
        closure = await _resolve_bound_closure(
            context, script, row, script_bindings, bound_child
        )
    except ScriptClosureError as exc:
        raise ScriptPreparationError(str(exc), "stale_script_binding") from exc
    if closure.resolution is not None:
        definition = (
            closure.resolution
            if definition is None
            else definition.combine(closure.resolution)
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
            script_bindings=tuple(binding.to_dict() for binding in closure.bindings),
        ),
        globals=inputs,
        bound_child=bound_child,
    )
