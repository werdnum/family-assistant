"""Mechanical effective-surface check for authenticated-site browser profiles.

An authenticated-site run gives a model the authority a saved login exposes on
the configured origins. What keeps that bounded is not a reviewed tool list but
a check that can be re-run mechanically at startup: the profile's *effective*
surface -- its own policy, the globally granted tools it must withhold, and the
ambient context providers it must exclude -- is compared against a set derived
from the tool table rather than written down by hand.

The check is deliberately syntactic and fails closed. A rule it cannot analyse
statically (a tag matcher, an MCP-server matcher, a glob) is a rejection, not a
pass, because whether such a rule grants something outside the admissible set
depends on a registry that changes under the configuration.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import ToolPolicyDecision

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.tools.metadata import LocalToolMetadata
    from family_assistant.tools.policy import ToolPolicyConfig

AMBIENT_CONTEXT_PROVIDERS: frozenset[str] = frozenset({
    "notes",
    "calendar",
    "known_users",
    "weather",
    "home_assistant",
})
"""Providers that inject the household's own data into a profile's prompt.

An authenticated browser run pairs page-controlled input with whatever is in
its prompt, so all of these are excluded. Keep in sync with the provider names
``ProcessingConfig.excluded_context_providers`` validates against.
"""

NON_AUTHENTICATED_BROWSER_TOOLS: frozenset[str] = frozenset({
    # Arbitrary page evaluation reads non-HttpOnly cookies and origin storage,
    # which collapses the opaque-session-state boundary.
    "browser_exec",
    # Raw DOM extraction bypasses the snapshot walker's read-back masking.
    "browser_extract",
    # A model-selected handback can replace this run's confined session.
    "browser_claim_handback",
})

NON_BROWSER_ADMISSIBLE_TOOLS: frozenset[str] = frozenset({
    # Not browser-server-mediated, but it only selects which of this run's own
    # screenshots reach the reply. It reaches no network and no household data.
    "attach_to_response",
})

PINNED_DELEGATION_TOOL = "delegate_to_service"
"""The one tool admitted by an argument-level pin rather than by name alone."""


def admissible_tools(metadata: Mapping[str, LocalToolMetadata]) -> frozenset[str]:
    """The browser-server-mediated tools an authenticated profile may hold.

    Derived from the tool table: everything tagged :attr:`ToolTag.BROWSER` is
    driven through browser-server's confined session, which is exactly the
    property that makes a tool admissible here. A tool that reaches the network
    on its own -- the shipped browser profile's UCP shopping tools act on a
    model-supplied business URL -- is not tagged BROWSER and so is rejected
    without anyone having to remember to list it.
    """
    browser_mediated = {
        name
        for name, descriptor in metadata.items()
        if ToolTag.BROWSER in descriptor.tags
    }
    return frozenset(
        (browser_mediated - NON_AUTHENTICATED_BROWSER_TOOLS)
        | NON_BROWSER_ADMISSIBLE_TOOLS
    )


def _is_glob(name: str) -> bool:
    return any(character in name for character in "*?[")


def _granting_rule_problems(
    policy: ToolPolicyConfig | None,
    *,
    allowed: frozenset[str],
    pinned_delegation_target: str | None,
    where: str,
) -> list[str]:
    """Reasons *policy*'s granting rules reach outside *allowed*.

    ``pinned_delegation_target`` is the visual profile the semantic profile may
    delegate to, or ``None`` for a profile that may delegate to nothing.
    """
    problems: list[str] = []
    if policy is None:
        return problems
    for index, rule in enumerate(policy.rules):
        if rule.decision is ToolPolicyDecision.DENY:
            continue
        match = rule.match
        label = f"{where} rule {index}"
        if match.tags_all or match.tags_any or match.mcp_server_ids:
            problems.append(
                f"{label} grants by tag or MCP server, which cannot be checked "
                "against the admissible tool set"
            )
            continue
        if not match.names:
            problems.append(f"{label} grants without naming any tool")
            continue
        for name in match.names:
            if _is_glob(name):
                problems.append(
                    f"{label} grants the pattern {name!r}; authenticated-site "
                    "profiles must name each tool literally"
                )
            elif name == PINNED_DELEGATION_TOOL:
                problems.extend(
                    _delegation_rule_problems(
                        rule_match_argument_equals=match.argument_equals,
                        pinned_delegation_target=pinned_delegation_target,
                        label=label,
                    )
                )
            elif name not in allowed:
                problems.append(
                    f"{label} grants {name!r}, which is not browser-server-mediated"
                )
    return problems


def _delegation_rule_problems(
    *,
    rule_match_argument_equals: Mapping[str, object] | None,
    pinned_delegation_target: str | None,
    label: str,
) -> list[str]:
    """Reasons a ``delegate_to_service`` grant is not pinned to one profile."""
    if pinned_delegation_target is None:
        return [
            f"{label} grants delegate_to_service, but this profile must not "
            "delegate to anything"
        ]
    pinned = (rule_match_argument_equals or {}).get("target_service_id")
    if pinned != pinned_delegation_target:
        return [
            f"{label} grants delegate_to_service without pinning "
            f"target_service_id to {pinned_delegation_target!r}; an open "
            "delegation grant reaches household-capable profiles indirectly"
        ]
    return []


def surface_violations(
    *,
    tools_policy: ToolPolicyConfig | None,
    operator_tools_policy: ToolPolicyConfig | None = None,
    excluded_global_tools: frozenset[str],
    excluded_context_providers: frozenset[str],
    include_aggregated_context: bool,
    globally_granted_tools: frozenset[str],
    admissible: frozenset[str],
    pinned_delegation_target: str | None,
) -> list[str]:
    """Every way a profile's effective surface breaks the authenticated rules.

    Returns an empty list when the profile is usable for authenticated runs.
    Each entry is a complete sentence naming what is wrong, so a startup failure
    tells the operator which line of their configuration to change.
    """
    problems: list[str] = []

    if tools_policy is None or tools_policy.default_decision is not (
        ToolPolicyDecision.DENY
    ):
        problems.append(
            "its tools_policy must set default_decision: deny, so a tool nobody "
            "considered is withheld rather than granted"
        )

    problems.extend(
        _granting_rule_problems(
            tools_policy,
            allowed=admissible,
            pinned_delegation_target=pinned_delegation_target,
            where="tools_policy",
        )
    )

    if operator_tools_policy is not None:
        if operator_tools_policy.default_decision is not ToolPolicyDecision.DENY:
            problems.append("operator_tools_policy must set default_decision: deny")
        problems.extend(
            _granting_rule_problems(
                operator_tools_policy,
                allowed=admissible,
                pinned_delegation_target=pinned_delegation_target,
                where="operator_tools_policy",
            )
        )

    missing_exclusions = sorted(
        name
        for name in globally_granted_tools
        if not any(fnmatchcase(name, pattern) for pattern in excluded_global_tools)
    )
    if missing_exclusions:
        problems.append(
            "it does not withhold the globally granted tool(s) "
            f"{', '.join(missing_exclusions)} via excluded_global_tools; a "
            "profile's own policy cannot refuse a global grant"
        )

    if include_aggregated_context:
        problems.append(
            "it sets include_aggregated_context: true, which injects the "
            "household's own data into a prompt built around page content"
        )

    missing_providers = sorted(AMBIENT_CONTEXT_PROVIDERS - excluded_context_providers)
    if missing_providers:
        problems.append(
            "it does not exclude the ambient context provider(s) "
            f"{', '.join(missing_providers)} via excluded_context_providers"
        )

    return problems
