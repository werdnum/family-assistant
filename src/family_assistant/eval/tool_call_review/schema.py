"""Serializable case contract for the tool-call review evaluation harness.

A case is a serialized reviewer *input*, never a serialized prompt: the harness
rebuilds today's typed input from the stored payload and runs the current
assembly code, so prompt and template changes are automatically under test. The
registry-resolved descriptor and derived signals (destination echo) are
therefore never stored; they are recomputed from the live registry and message
window at load time.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

from family_assistant.llm.messages import dict_to_message
from family_assistant.security.taint import SinkClass, TurnTaintState
from family_assistant.services.tool_call_review import (
    BrowserActionReviewInput,
    DelegatingPolicyContext,
    ToolCallReviewConstraints,
    ToolCallReviewInput,
    ToolCallReviewVerdict,
    TriggerReviewInput,
    compute_trusted_destination_echo,
)
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS

# The harness must stay byte-faithful to the runtime's own signal derivation:
# the destination the reviewer sees comes from the descriptor's declared
# destination argument paths (first nonblank, nested paths supported), and a
# reimplementation here would drift from the runtime the eval claims to
# measure. So the runtime's private helper is imported and used verbatim rather
# than reproduced.
from family_assistant.tools.infrastructure import _destination_argument

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.security.taint import TaintMetadata
    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "BrowserPayload",
    "CaseConstraints",
    "ConversationPayload",
    "DerivationPayload",
    "EvalCase",
    "ToolResolutionError",
    "TriggerSpec",
    "resolve_tool_descriptor",
]

Boundary = Literal["conversation", "browser", "derivation"]
CaseLabel = Literal["attack", "benign"]
DerivationLabel = Literal["derivable", "data_embedding", "instruction_smuggling"]


class ToolResolutionError(Exception):
    """A case names a tool that the live registry does not contain."""


@functools.cache
def _local_tool_descriptor_map() -> dict[str, ToolDescriptor]:
    """Build a name->descriptor map from the live local tool registry."""
    return {descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS}


def resolve_tool_descriptor(
    tool_name: str,
    registry: Mapping[str, ToolDescriptor] | None = None,
) -> ToolDescriptor:
    """Resolve a tool descriptor by name, failing loudly when it is missing.

    A case naming a tool that no longer exists must fail its slice rather than
    quietly flattering the judge, so the missing-tool path raises here rather
    than returning ``None``.

    ``registry`` defaults to the local tool registry. The runtime reviewer also
    sees dynamically discovered MCP tools and direct named-sink descriptors from
    its wrapped provider, which the static local list cannot supply — a
    deployment replaying cases that involve those tools must pass the same
    provider registry it evaluates with, so those cases resolve instead of
    silently dropping out of the harness.
    """
    descriptor_map = registry if registry is not None else _local_tool_descriptor_map()
    try:
        return descriptor_map[tool_name]
    except KeyError as exc:
        raise ToolResolutionError(
            f"Case references unknown tool {tool_name!r}; the supplied registry has "
            "no descriptor for it."
        ) from exc


class CaseConstraints(BaseModel):
    """Verdict space and fail-closed fallback the delegating context supplied.

    Both fields are required: the constraints render into the prompt and gate
    out-of-space verdicts, so a case that omitted them would replay under
    invented semantics instead of failing validation; manual and adapted cases
    must state theirs explicitly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    available_verdicts: list[ToolCallReviewVerdict]
    fallback_verdict: ToolCallReviewVerdict

    @model_validator(mode="after")
    def _validate_verdict_space(self) -> CaseConstraints:
        """Mirror the runtime constraint invariants at load time.

        ``ToolCallReviewConstraints`` rejects these combinations, but only once
        a case is converted for a live run — a ``--dry-run`` validation pass
        would otherwise report an impossible case as loadable and the failure
        would surface mid-run instead.
        """
        if not self.available_verdicts:
            raise ValueError("available_verdicts must not be empty")
        if self.fallback_verdict is ToolCallReviewVerdict.ALLOW:
            raise ValueError("review fallback must never be allow")
        if self.fallback_verdict not in self.available_verdicts:
            raise ValueError("fallback_verdict must be in available_verdicts")
        return self

    def to_constraints(self) -> ToolCallReviewConstraints:
        """Render the runtime constraints object the reviewer validates against."""
        return ToolCallReviewConstraints(
            available_verdicts=frozenset(self.available_verdicts),
            fallback_verdict=self.fallback_verdict,
        )


class TriggerSpec(BaseModel):
    """Serialized trigger context for a conversation case.

    ``payload_present`` is a strict bool: a YAML author who quotes ``"false"``
    means false, and lax coercion would replay the case with the opposite
    trigger semantics instead of rejecting it.
    """

    model_config = ConfigDict(extra="forbid")

    trigger_type: str
    active_request_role: Literal["user", "system"]
    definition: str | None = None
    definition_taint_metadata: dict[str, object] | None = None
    payload_present: StrictBool = True

    def to_trigger_input(self) -> TriggerReviewInput:
        """Build the runtime trigger input this spec serializes."""
        return TriggerReviewInput(
            trigger_type=self.trigger_type,
            active_request_role=self.active_request_role,
            definition=self.definition,
            definition_taint_metadata=cast(
                "TaintMetadata | None", self.definition_taint_metadata
            ),
            payload_present=self.payload_present,
        )


class ConversationPayload(BaseModel):
    """Serialized ``ToolCallReviewInput`` minus derived and resolved parts.

    ``sink_class`` is the *resolved* sink stored verbatim, never re-derived at
    load: sink resolution consults deployment configuration (e.g.
    ``delegation_sink_classes``) that replay cannot recover from the descriptor
    and arguments alone, so a delegation case must carry the sink the runtime
    actually reviewed.

    The destination is not a field here: it is derived from the descriptor and
    the stored arguments by the runtime's own extractor at load, so a case can
    neither name a destination the runtime would not have read nor go stale
    when a tool's declared destination paths change.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, object]]
    tool_name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    sink_class: str
    taint_state: dict[str, object] = Field(default_factory=dict)
    policy_contexts: list[dict[str, object]] = Field(default_factory=list)
    deployment_guidance: str = ""
    profile_guidance: str = ""
    trigger: TriggerSpec | None = None


class BrowserPayload(BaseModel):
    """Serialized ``BrowserActionReviewInput`` for the textual boundary.

    ``environment_kind`` is fixed to ``snapshot``: the shipped contract renders
    the environment as fenced text, so this harness boundary is textual only.
    """

    model_config = ConfigDict(extra="forbid")

    objective: str
    damage_envelope: str
    proposed_action: dict[str, object] = Field(default_factory=dict)
    environment: str
    recent_actions: list[dict[str, object]] = Field(default_factory=list)
    mitigation_guidance: str = ""
    policy_contexts: list[dict[str, object]] = Field(default_factory=list)


class DerivationPayload(BaseModel):
    """Future sanitizer case; no shipped judge consumes it yet."""

    model_config = ConfigDict(extra="forbid")

    trusted_rows: list[dict[str, object]] = Field(default_factory=list)
    composed_artifact: str
    derivation_label: DerivationLabel


_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "conversation": ConversationPayload,
    "browser": BrowserPayload,
    "derivation": DerivationPayload,
}


class EvalCase(BaseModel):
    """One reviewer-input case with a boundary-specific payload."""

    model_config = ConfigDict(extra="forbid")

    id: str
    boundary: Boundary
    label: CaseLabel
    attack_class: str | None = None
    source: str = "manual"
    expected_verdict: ToolCallReviewVerdict | None = None
    obfuscation: str | None = None
    placement: str | None = None
    language: str | None = None
    constraints: CaseConstraints
    payload: ConversationPayload | BrowserPayload | DerivationPayload

    @model_validator(mode="before")
    @classmethod
    def _coerce_payload(cls, data: object) -> object:
        """Select the payload model by boundary before per-field validation.

        The payloads carry no discriminator field of their own, so the envelope
        boundary is authoritative; coercing here gives a clear error for a
        boundary/payload mismatch instead of a confusing union failure.
        """
        if not isinstance(data, dict):
            return data
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return data
        boundary = data.get("boundary")
        model = _PAYLOAD_MODELS.get(boundary) if isinstance(boundary, str) else None
        if model is None:
            raise ValueError(
                f"Unknown or missing boundary {boundary!r}; cannot select a payload "
                "model."
            )
        return {**data, "payload": model.model_validate(payload)}

    @model_validator(mode="after")
    def _check_consistency(self) -> EvalCase:
        expected_model = _PAYLOAD_MODELS[self.boundary]
        if not isinstance(self.payload, expected_model):
            raise ValueError(
                f"Boundary {self.boundary!r} requires a {expected_model.__name__} "
                f"payload, got {type(self.payload).__name__}."
            )
        if (
            self.expected_verdict is not None
            and self.expected_verdict not in self.constraints.available_verdicts
        ):
            raise ValueError(
                "expected_verdict must be within the case's available_verdicts."
            )
        if self.label == "attack" and self.attack_class is None:
            raise ValueError(
                "attack cases must declare an attack_class; an unclassified attack "
                "would dodge the per-family slice gates."
            )
        return self

    def to_review_input(
        self,
        *,
        descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
    ) -> tuple[
        ToolCallReviewInput | BrowserActionReviewInput, ToolCallReviewConstraints
    ]:
        """Rebuild the typed reviewer input and constraints for this case.

        Registry resolution and derived-signal recomputation happen here, so a
        tool whose schema changed or a destination that no longer echoes is
        reflected in the replay rather than frozen into the stored case.
        ``descriptor_registry`` overrides the local tool registry; pass the
        evaluated deployment's provider registry when replaying cases that
        involve MCP or named-sink tools.
        """
        constraints = self.constraints.to_constraints()
        if isinstance(self.payload, ConversationPayload):
            return (
                self._conversation_review_input(
                    self.payload, descriptor_registry=descriptor_registry
                ),
                constraints,
            )
        if isinstance(self.payload, BrowserPayload):
            return self._browser_review_input(self.payload), constraints
        raise ValueError(
            "Derivation cases have no shipped review contract and cannot be "
            "converted to a reviewer input."
        )

    @staticmethod
    def _conversation_review_input(
        payload: ConversationPayload,
        *,
        descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
    ) -> ToolCallReviewInput:
        descriptor = resolve_tool_descriptor(
            payload.tool_name, registry=descriptor_registry
        )
        messages = [dict_to_message(row) for row in payload.messages]
        trigger = (
            payload.trigger.to_trigger_input() if payload.trigger is not None else None
        )
        destination_echo = compute_trusted_destination_echo(
            _destination_argument(descriptor, payload.arguments),
            messages,
            trigger=trigger,
        )
        return ToolCallReviewInput(
            messages=messages,
            descriptor=descriptor,
            arguments=dict(payload.arguments),
            sink_class=SinkClass(payload.sink_class),
            taint_state=TurnTaintState.from_metadata(payload.taint_state),
            policy_contexts=[
                _build_policy_context(context) for context in payload.policy_contexts
            ],
            deployment_guidance=payload.deployment_guidance,
            profile_guidance=payload.profile_guidance,
            trigger=trigger,
            destination_echo=destination_echo,
        )

    @staticmethod
    def _browser_review_input(payload: BrowserPayload) -> BrowserActionReviewInput:
        return BrowserActionReviewInput(
            objective=payload.objective,
            damage_envelope=payload.damage_envelope,
            proposed_action=dict(payload.proposed_action),
            environment=payload.environment,
            environment_kind="snapshot",
            recent_actions=[dict(action) for action in payload.recent_actions],
            mitigation_guidance=payload.mitigation_guidance,
            policy_contexts=[
                _build_policy_context(context) for context in payload.policy_contexts
            ],
        )


def _build_policy_context(context: Mapping[str, object]) -> DelegatingPolicyContext:
    return DelegatingPolicyContext.model_validate(dict(context))
