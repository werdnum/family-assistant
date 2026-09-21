"""Tool policy configuration and evaluation engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from family_assistant.tools.metadata import (
    ToolTag,  # noqa: TC001 - Pydantic resolves this enum at runtime
)

if TYPE_CHECKING:
    from family_assistant.tools.metadata import ToolDescriptor

# Highest priority a rule may declare. Rules compete on priority within a policy
# layer, so this is the value to use when a rule must beat every other rule in
# its layer -- e.g. withholding a globally granted tool from one profile.
MAX_POLICY_RULE_PRIORITY = 99

DEFAULT_POLICY_PRIORITY_OFFSET = 0
PROFILE_POLICY_PRIORITY_OFFSET = 100
OPERATOR_POLICY_PRIORITY_OFFSET = 1000

type PolicyLayer = Literal["defaults", "profile", "operator"]

_LAYER_PRIORITY_OFFSETS: dict[PolicyLayer, int] = {
    "defaults": DEFAULT_POLICY_PRIORITY_OFFSET,
    "profile": PROFILE_POLICY_PRIORITY_OFFSET,
    "operator": OPERATOR_POLICY_PRIORITY_OFFSET,
}

_LAYER_TIEBREAK_ORDER: dict[PolicyLayer, int] = {
    "operator": 0,
    "profile": 1,
    "defaults": 2,
}


class ToolPolicyDecision(StrEnum):
    """Access decisions produced by the policy engine."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"
    REVIEW = "review"


class ToolMatcher(BaseModel):
    """Matches tools by name patterns, tags, and MCP origin."""

    model_config = ConfigDict(extra="forbid")

    names: list[str] | None = None
    tags_all: list[ToolTag] | None = None
    tags_any: list[ToolTag] | None = None
    mcp_server_ids: list[str] | None = None
    argument_equals: dict[str, str | bool | int | float | None] | None = None

    def matches(
        self,
        descriptor: ToolDescriptor,
        arguments: dict[str, object] | None = None,
    ) -> bool:
        """Return whether this matcher applies to the given descriptor."""
        if not any((
            self.names,
            self.tags_all,
            self.tags_any,
            self.mcp_server_ids,
            self.argument_equals,
        )):
            return False

        if self.names and not any(
            fnmatchcase(descriptor.name, pattern) for pattern in self.names
        ):
            return False

        if self.tags_all and not descriptor.tags.issuperset(self.tags_all):
            return False

        if self.tags_any and descriptor.tags.isdisjoint(self.tags_any):
            return False

        if self.mcp_server_ids:
            if descriptor.origin != "mcp" or descriptor.mcp_server_id is None:
                return False
            if "*" not in self.mcp_server_ids and (
                descriptor.mcp_server_id not in self.mcp_server_ids
            ):
                return False

        if self.argument_equals:
            if arguments is None:
                return False
            for key, expected_value in self.argument_equals.items():
                if key not in arguments:
                    return False
                actual = arguments[key]
                if type(actual) is not type(expected_value) or actual != expected_value:
                    return False

        return True


class PolicyRule(BaseModel):
    """A single policy rule mapping tool matches to access decisions."""

    model_config = ConfigDict(extra="forbid")

    match: ToolMatcher
    decision: ToolPolicyDecision
    priority: int = Field(default=0, ge=0, le=MAX_POLICY_RULE_PRIORITY)
    description: str = ""


class ToolPolicyConfig(BaseModel):
    """Policy configuration for a single layer."""

    model_config = ConfigDict(extra="forbid")

    rules: list[PolicyRule] = Field(default_factory=list)
    default_decision: ToolPolicyDecision | None = None


@dataclass(frozen=True, slots=True)
class ResolvedPolicyRule:
    """A policy rule after layer offsets and tie-break metadata are applied."""

    rule: PolicyRule
    layer: PolicyLayer
    declaration_order: int
    effective_priority: int

    @property
    def decision(self) -> ToolPolicyDecision:
        """Return the rule decision."""
        return self.rule.decision

    @property
    def description(self) -> str:
        """Return the rule description."""
        return self.rule.description

    @property
    def match(self) -> ToolMatcher:
        """Return the rule matcher."""
        return self.rule.match


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """Result of evaluating a descriptor against policy."""

    decision: ToolPolicyDecision
    reason: str
    matched_rule: ResolvedPolicyRule | None = None


@dataclass(frozen=True, slots=True)
class ResolvedToolPolicy:
    """Merged policy state used by the evaluation engine."""

    rules: tuple[ResolvedPolicyRule, ...]
    default_decision: ToolPolicyDecision


class PolicyEngine:
    """Evaluates tool descriptors against merged policy layers."""

    def __init__(self, policy: ResolvedToolPolicy) -> None:
        self._policy = policy

    @classmethod
    def from_policy_config(
        cls,
        policy: ToolPolicyConfig | None,
        *,
        fallback_default_decision: ToolPolicyDecision = ToolPolicyDecision.DENY,
    ) -> PolicyEngine:
        """Build an engine from a single-layer policy config."""
        return cls.from_layers(
            defaults=policy,
            fallback_default_decision=fallback_default_decision,
        )

    @classmethod
    def from_layers(
        cls,
        *,
        defaults: ToolPolicyConfig | None = None,
        profile: ToolPolicyConfig | None = None,
        operator: ToolPolicyConfig | None = None,
        fallback_default_decision: ToolPolicyDecision = ToolPolicyDecision.DENY,
    ) -> PolicyEngine:
        """Build an engine from default, profile, and operator policy layers."""
        resolved_policy = ResolvedToolPolicy(
            rules=tuple(
                _resolve_rules(
                    defaults=defaults,
                    profile=profile,
                    operator=operator,
                )
            ),
            default_decision=_resolve_default_decision(
                defaults=defaults,
                profile=profile,
                operator=operator,
                fallback=fallback_default_decision,
            ),
        )
        return cls(policy=resolved_policy)

    @property
    def rules(self) -> tuple[ResolvedPolicyRule, ...]:
        """Return resolved rules in evaluation order."""
        return self._policy.rules

    @property
    def default_decision(self) -> ToolPolicyDecision:
        """Return the resolved default decision."""
        return self._policy.default_decision

    def evaluate(
        self,
        descriptor: ToolDescriptor,
        *,
        arguments: dict[str, object] | None = None,
    ) -> PolicyEvaluation:
        """Return the raw policy result for a descriptor."""
        for resolved_rule in self._policy.rules:
            if resolved_rule.match.matches(descriptor, arguments):
                reason = resolved_rule.description or (
                    f"matched {resolved_rule.layer} rule"
                )
                return PolicyEvaluation(
                    decision=resolved_rule.decision,
                    reason=reason,
                    matched_rule=resolved_rule,
                )

        return PolicyEvaluation(
            decision=self._policy.default_decision,
            reason="no matching rule (default)",
        )

    def evaluate_for_advertisement(
        self,
        descriptor: ToolDescriptor,
        *,
        can_confirm: bool,
    ) -> PolicyEvaluation:
        """Return the policy result for tool advertisement."""
        return self._apply_confirmation_capability(
            self.evaluate(descriptor, arguments=None),
            can_confirm=can_confirm,
            context="advertisement",
        )

    def evaluate_for_execution(
        self,
        descriptor: ToolDescriptor,
        *,
        arguments: dict[str, object] | None = None,
        can_confirm: bool = True,
    ) -> PolicyEvaluation:
        """Return the policy result for tool execution."""
        return self._apply_confirmation_capability(
            self.evaluate(descriptor, arguments=arguments),
            can_confirm=can_confirm,
            context="execution",
        )

    def explain(
        self,
        descriptor: ToolDescriptor,
        *,
        arguments: dict[str, object] | None = None,
    ) -> PolicyEvaluation:
        """Return the raw evaluation result for diagnostics and tests."""
        return self.evaluate(descriptor, arguments=arguments)

    @staticmethod
    def _apply_confirmation_capability(
        evaluation: PolicyEvaluation,
        *,
        can_confirm: bool,
        context: str,
    ) -> PolicyEvaluation:
        if evaluation.decision != ToolPolicyDecision.CONFIRM or can_confirm:
            return evaluation

        return PolicyEvaluation(
            decision=ToolPolicyDecision.DENY,
            reason=(
                f"{evaluation.reason}; confirmation required but unavailable for "
                f"{context}"
            ),
            matched_rule=evaluation.matched_rule,
        )


def _resolve_rules(
    *,
    defaults: ToolPolicyConfig | None,
    profile: ToolPolicyConfig | None,
    operator: ToolPolicyConfig | None,
) -> list[ResolvedPolicyRule]:
    layered_rules: list[ResolvedPolicyRule] = []
    layered_configs: tuple[tuple[PolicyLayer, ToolPolicyConfig | None], ...] = (
        ("defaults", defaults),
        ("profile", profile),
        ("operator", operator),
    )
    for layer, policy in layered_configs:
        if policy is None:
            continue
        offset = _LAYER_PRIORITY_OFFSETS[layer]
        for declaration_order, rule in enumerate(policy.rules):
            layered_rules.append(
                ResolvedPolicyRule(
                    rule=rule,
                    layer=layer,
                    declaration_order=declaration_order,
                    effective_priority=rule.priority + offset,
                )
            )

    layered_rules.sort(
        key=lambda resolved_rule: (
            -resolved_rule.effective_priority,
            _LAYER_TIEBREAK_ORDER[resolved_rule.layer],
            resolved_rule.declaration_order,
        )
    )
    return layered_rules


def _resolve_default_decision(
    *,
    defaults: ToolPolicyConfig | None,
    profile: ToolPolicyConfig | None,
    operator: ToolPolicyConfig | None,
    fallback: ToolPolicyDecision,
) -> ToolPolicyDecision:
    for candidate in (
        profile,
        operator,
        defaults,
    ):
        if candidate is not None and candidate.default_decision is not None:
            return candidate.default_decision
    return fallback
