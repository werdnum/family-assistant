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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "resolve_tool_descriptor",
]

Boundary = Literal["conversation", "browser", "derivation"]
CaseLabel = Literal["attack", "benign"]
DerivationLabel = Literal["derivable", "data_embedding", "instruction_smuggling"]


class ToolResolutionError(Exception):
    """A case names a tool that the live registry does not contain."""


@functools.cache
def _tool_descriptor_map() -> dict[str, ToolDescriptor]:
    """Build a name->descriptor map from the live local tool registry."""
    return {descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS}


def resolve_tool_descriptor(tool_name: str) -> ToolDescriptor:
    """Resolve a tool descriptor by name, failing loudly when it is missing.

    A case naming a tool that no longer exists must fail its slice rather than
    quietly flattering the judge, so the missing-tool path raises here rather
    than returning ``None``.
    """
    descriptor_map = _tool_descriptor_map()
    try:
        return descriptor_map[tool_name]
    except KeyError as exc:
        raise ToolResolutionError(
            f"Case references unknown tool {tool_name!r}; the live registry has no "
            "descriptor for it."
        ) from exc


class CaseConstraints(BaseModel):
    """Verdict space and fail-closed fallback the delegating context supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available_verdicts: list[ToolCallReviewVerdict] = Field(
        default_factory=lambda: list(ToolCallReviewVerdict)
    )
    fallback_verdict: ToolCallReviewVerdict = ToolCallReviewVerdict.CONFIRM

    def to_constraints(self) -> ToolCallReviewConstraints:
        """Render the runtime constraints object the reviewer validates against."""
        return ToolCallReviewConstraints(
            available_verdicts=frozenset(self.available_verdicts),
            fallback_verdict=self.fallback_verdict,
        )


class ConversationPayload(BaseModel):
    """Serialized ``ToolCallReviewInput`` minus derived and resolved parts."""

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, object]]
    tool_name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    sink_class: str
    taint_state: dict[str, object] = Field(default_factory=dict)
    policy_contexts: list[dict[str, object]] = Field(default_factory=list)
    deployment_guidance: str = ""
    profile_guidance: str = ""
    trigger: dict[str, object] | None = None
    destination_arg_key: str | None = None


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
    """One labeled reviewer-input case with a boundary-specific payload."""

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
    constraints: CaseConstraints = Field(default_factory=CaseConstraints)
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
        return self

    def to_review_input(
        self,
    ) -> tuple[
        ToolCallReviewInput | BrowserActionReviewInput, ToolCallReviewConstraints
    ]:
        """Rebuild the typed reviewer input and constraints for this case.

        Registry resolution and derived-signal recomputation happen here, so a
        tool whose schema changed or a destination that no longer echoes is
        reflected in the replay rather than frozen into the stored case.
        """
        constraints = self.constraints.to_constraints()
        if isinstance(self.payload, ConversationPayload):
            return self._conversation_review_input(self.payload), constraints
        if isinstance(self.payload, BrowserPayload):
            return self._browser_review_input(self.payload), constraints
        raise ValueError(
            "Derivation cases have no shipped review contract and cannot be "
            "converted to a reviewer input."
        )

    @staticmethod
    def _conversation_review_input(
        payload: ConversationPayload,
    ) -> ToolCallReviewInput:
        descriptor = resolve_tool_descriptor(payload.tool_name)
        messages = [dict_to_message(row) for row in payload.messages]
        trigger = _build_trigger(payload.trigger)
        destination_echo = None
        if payload.destination_arg_key is not None:
            destination = payload.arguments.get(payload.destination_arg_key)
            destination_echo = compute_trusted_destination_echo(
                destination if isinstance(destination, str) else None,
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


def _build_trigger(trigger: Mapping[str, object] | None) -> TriggerReviewInput | None:
    if trigger is None:
        return None
    trigger_type = trigger.get("trigger_type")
    active_request_role = trigger.get("active_request_role")
    if not isinstance(trigger_type, str) or active_request_role not in {
        "user",
        "system",
    }:
        raise ValueError(
            "trigger requires a string trigger_type and an active_request_role of "
            "'user' or 'system'."
        )
    definition = trigger.get("definition")
    definition_taint_metadata = trigger.get("definition_taint_metadata")
    payload_present = trigger.get("payload_present", True)
    return TriggerReviewInput(
        trigger_type=trigger_type,
        active_request_role=cast("Literal['user', 'system']", active_request_role),
        definition=definition if isinstance(definition, str) else None,
        definition_taint_metadata=(
            cast("TaintMetadata", definition_taint_metadata)
            if isinstance(definition_taint_metadata, dict)
            else None
        ),
        payload_present=bool(payload_present),
    )
