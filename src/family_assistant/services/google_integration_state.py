"""Single source of truth for whether the Google (Gmail/Drive) integration is on.

Startup evaluates one :class:`GoogleIntegrationState` from the deployment config
and stores it on the assistant and ``fastapi_app.state``. Everything else — the
tool-gating chokepoint at app wiring, the status endpoint, and the connect-flow
409s — reads that single object rather than re-deriving enablement, so the
enablement contract lives in exactly one place.

The evaluation walks the design's conditions IN ORDER (first failure wins,
``docs/design/user-scoped-google-data-access.md`` §"Configuration"):

1. OAuth client id/secret and the credential encryption key are all present.
2. The encryption key is a well-formed Fernet key.
3. Every configured scope is in the read-only allowlist the shipped tools can
   serve (an unlisted or write scope *disables* the integration — coherence
   validation, not a policy knob).
4. Real web authentication is enabled (the dev ``test_user`` mode, which serves
   one shared synthetic identity, must refuse so a Google account is never
   attached to that shared identity) AND the ``users`` block is populated so
   OIDC identities resolve to canonical user ids (with an empty ``users`` list,
   connections would be keyed by raw OIDC identifiers).
5. The **taint floor** — only when ``require_taint_enforcement`` is ``True``:
   ``taint_policy.mode`` is ``enforce`` AND, for every profile in which any
   Google tool is policy-allowed, the *fully merged effective* taint policy —
   queried through the same :class:`TaintPolicyEvaluator` the runtime uses —
   yields at least ``confirm`` at the ``unknown_external`` tier for each of the
   floor sink classes. Validating the real evaluator (not a re-derived
   approximation) is deliberate: a full ``matrix:`` replacement that the runtime
   honors must be caught here too. When the requirement is waived
   (``require_taint_enforcement: false``) the floor is skipped entirely and the
   waiver is surfaced as a visible, deliberate risk acceptance.

When enabled, :attr:`GoogleIntegrationState.enabled_tool_names` is the
scope-conditional subset of the five Google tools whose required-scope set
intersects the configured scopes, so the LLM is never advertised a tool its
credentials cannot serve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyEvaluator,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_policy_config,
)
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    CredentialEncryptionError,
)
from family_assistant.services.google_credentials import SUPPORTED_GOOGLE_SCOPES
from family_assistant.tools import (
    LOCAL_TOOL_DESCRIPTORS,
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_assistant.config_models import AppConfig, ServiceProfile
    from family_assistant.tools.metadata import ToolDescriptor, ToolRegistration


# The five Google tools whose registration this feature governs.
GOOGLE_TOOL_NAMES: frozenset[str] = frozenset(GOOGLE_TOOL_REQUIRED_SCOPES)

# Sink classes the taint floor requires to sit at >= confirm at unknown_external.
# The design deliberately omits home_local (allow) and artifact_write (audit):
# those are the shipped default matrix's softer entries, tuned by the operator's
# own matrix, not re-litigated through this side-door registration check.
_FLOOR_SINK_CLASSES: tuple[SinkClass, ...] = (
    SinkClass.ARBITRARY_EXTERNAL_MESSAGE,
    SinkClass.ATTACKER_ADDRESSABLE_EGRESS,
    SinkClass.SANDBOX_NETWORK,
    SinkClass.SENSITIVE_READ_BROADENING,
)

# Outcomes that satisfy the floor (>= confirm).
_FLOOR_SATISFYING_OUTCOMES: frozenset[TaintPolicyOutcome] = frozenset({
    TaintPolicyOutcome.CONFIRM,
    TaintPolicyOutcome.DENY,
})


@dataclass(frozen=True)
class GoogleIntegrationState:
    """Resolved enablement of the Google integration for this deployment."""

    enabled: bool
    reason: str | None
    taint_enforcement_waived: bool
    enabled_tool_names: frozenset[str]


def _any_config_field_set(config: AppConfig) -> bool:
    """Return True if the operator set any Google config field (tried to enable)."""
    integration = config.google_integration
    return bool(
        integration.oauth_client_id
        or integration.oauth_client_secret
        or integration.credential_encryption_key
    )


def _missing_credentials_reason(config: AppConfig) -> str | None:
    """Return a reason for missing OAuth credentials, or None when all present."""
    integration = config.google_integration
    has_any = _any_config_field_set(config)
    if not has_any:
        return "Google integration is not configured."
    if not integration.oauth_client_id:
        return "Google integration is disabled: GOOGLE_OAUTH_CLIENT_ID is not set."
    if not integration.oauth_client_secret:
        return "Google integration is disabled: GOOGLE_OAUTH_CLIENT_SECRET is not set."
    if not integration.credential_encryption_key:
        return "Google integration is disabled: CREDENTIAL_ENCRYPTION_KEY is not set."
    return None


def _google_descriptor_by_name() -> dict[str, ToolDescriptor]:
    """Map each Google tool name to its shipped descriptor (for policy queries)."""
    return {
        descriptor.name: descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if descriptor.name in GOOGLE_TOOL_NAMES
    }


def _profile_allows_any_google_tool(
    profile: ServiceProfile,
    global_tools_policy: ToolPolicyConfig | None,
    google_descriptors: Iterable[ToolDescriptor],
) -> bool:
    """Return True if the profile's policy allows/confirms any Google tool.

    Uses the same layered :class:`PolicyEngine` the runtime builds for the
    profile so this reflects real registration, not a tool-name heuristic. A
    profile with no ``tools_policy`` (remote A2A profiles) cannot register local
    tools, so it never contributes to the floor.
    """
    if profile.tools_policy is None:
        return False

    synthetic_policy = (
        ToolPolicyConfig(rules=list(global_tools_policy.rules))
        if global_tools_policy is not None
        else None
    )
    engine = PolicyEngine.from_layers(
        defaults=profile.tools_policy,
        profile=synthetic_policy,
        operator=profile.operator_tools_policy,
    )
    for descriptor in google_descriptors:
        decision = engine.evaluate(descriptor).decision
        if decision is not ToolPolicyDecision.DENY:
            return True
    return False


def _floor_state() -> TurnTaintState:
    """Build a turn state carrying a single unknown_external source."""
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.MANUAL,
            source_id=None,
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Google integration taint-floor startup validation.",
        )
    )


def _profile_floor_reason(
    profile_id: str,
    merged_policy_evaluator: TaintPolicyEvaluator,
    state: TurnTaintState,
) -> str | None:
    """Return a floor-violation reason for a profile, or None when it passes.

    Runs the REAL evaluator so a full ``matrix:`` replacement that silently drops
    a floor sink below confirm is caught.
    """
    for sink_class in _FLOOR_SINK_CLASSES:
        outcome = merged_policy_evaluator.evaluate(
            state=state, sink_class=sink_class
        ).requested_outcome
        if outcome not in _FLOOR_SATISFYING_OUTCOMES:
            return (
                "Google integration is disabled: taint floor not met for profile "
                f"'{profile_id}' — sink '{sink_class.value}' resolves to "
                f"'{outcome.value}' (below 'confirm') at the unknown_external tier. "
                "Raise it to confirm via taint_policy.matrix_overrides / "
                "operator_minimum, or set "
                "google_integration.require_taint_enforcement: false to accept the "
                "risk."
            )
    return None


def _taint_floor_reason(config: AppConfig) -> str | None:
    """Return a taint-floor violation reason, or None when the floor holds.

    Skips the check entirely when ``require_taint_enforcement`` is False.
    """
    if not config.google_integration.require_taint_enforcement:
        return None

    if config.taint_policy.mode is not TaintPolicyMode.ENFORCE:
        return (
            "Google integration is disabled: taint_policy.mode is "
            f"'{config.taint_policy.mode.value}', but 'enforce' is required. Set "
            "taint_policy.mode: enforce, or set "
            "google_integration.require_taint_enforcement: false to accept the risk."
        )

    google_descriptors = list(_google_descriptor_by_name().values())
    state = _floor_state()
    for profile in config.service_profiles:
        if not _profile_allows_any_google_tool(
            profile, config.global_tools_policy, google_descriptors
        ):
            continue
        try:
            merged = merge_taint_policy_config(
                base=config.taint_policy, profile=profile.taint_policy
            )
        except ValueError as exc:
            # The profile's taint_policy tries to relax the base policy (the merge
            # guard rejects it). A Google-enabled profile that weakens the floor is
            # disqualifying — surface it as a floor failure rather than crashing.
            return (
                "Google integration is disabled: profile "
                f"'{profile.id}' taint_policy relaxes the deployment policy "
                f"({exc}). Tighten it or set "
                "google_integration.require_taint_enforcement: false to accept the "
                "risk."
            )
        evaluator = TaintPolicyEvaluator(merged)
        reason = _profile_floor_reason(profile.id, evaluator, state)
        if reason is not None:
            return reason
    return None


def _enabled_tool_names(config: AppConfig) -> frozenset[str]:
    """Return the scope-conditional subset of Google tools that can be served."""
    configured = set(config.google_integration.scopes)
    return frozenset(
        name
        for name, required in GOOGLE_TOOL_REQUIRED_SCOPES.items()
        if required & configured
    )


def filter_google_tool_registrations(
    registrations: list[ToolRegistration],
    state: GoogleIntegrationState,
) -> list[ToolRegistration]:
    """Drop Google tool registrations the integration cannot serve.

    Removes any of the five Google tools not in ``state.enabled_tool_names`` (all
    of them when the integration is disabled). Filtering the shared root
    registrations is the single chokepoint: every profile's provider and the
    UI/API listing wrap the root provider, so a filtered-out tool is advertised
    nowhere. Filtering registrations (rather than the raw definitions) keeps the
    definition/implementation/metadata sets consistent, which
    ``build_local_tool_registrations`` requires.
    """
    allowed = state.enabled_tool_names
    return [
        registration
        for registration in registrations
        if registration.name not in GOOGLE_TOOL_NAMES or registration.name in allowed
    ]


def evaluate_google_integration_state(
    config: AppConfig,
    *,
    auth_enabled: bool,
) -> GoogleIntegrationState:
    """Evaluate whether the Google integration is enabled for this deployment.

    Args:
        config: The full application config (Google section + taint policy +
            profiles + global tool policy).
        auth_enabled: Whether real web authentication is active (from
            ``auth_service.auth_enabled``). The dev ``test_user`` mode is False.

    Returns:
        A :class:`GoogleIntegrationState` with the first unmet condition's reason
        when disabled, the waiver flag, and the scope-conditional tool subset.
    """
    waived = not config.google_integration.require_taint_enforcement

    def disabled(reason: str) -> GoogleIntegrationState:
        return GoogleIntegrationState(
            enabled=False,
            reason=reason,
            taint_enforcement_waived=waived,
            enabled_tool_names=frozenset(),
        )

    # 1. Credentials present.
    credentials_reason = _missing_credentials_reason(config)
    if credentials_reason is not None:
        return disabled(credentials_reason)

    # 2. Encryption key well-formed.
    try:
        CredentialEncryption(config.google_integration.credential_encryption_key)
    except CredentialEncryptionError as exc:
        return disabled(f"Google integration is disabled: {exc}")

    # 3. Configured scopes are all in the read-only allowlist.
    unsupported = [
        scope
        for scope in config.google_integration.scopes
        if scope not in SUPPORTED_GOOGLE_SCOPES
    ]
    if unsupported:
        return disabled(
            "Google integration is disabled: unsupported scope(s) "
            f"{sorted(unsupported)!r} configured. Only the read-only scopes "
            f"{sorted(SUPPORTED_GOOGLE_SCOPES)!r} are allowed; remove the extra "
            "scope(s) from google_integration.scopes."
        )

    # 4. Real web authentication enabled.
    if not auth_enabled:
        return disabled(
            "Google integration is disabled: real web authentication must be "
            "enabled so each user has a distinct identity (the dev test_user mode "
            "shares one identity and is refused)."
        )

    # 4b. Canonical identities resolve: with OIDC on but an empty ``users`` block,
    # connections would be keyed by raw OIDC identifiers instead of canonical user
    # ids, so require the users block to be populated (the same signal
    # ``UserIdentityResolver.users_configured`` derives from ``config.users``).
    if not config.users:
        return disabled(
            "Google integration is disabled: configure the users block so OIDC "
            "identities resolve to canonical user ids "
            "(google_integration keys connections by canonical user id)."
        )

    # 5. Taint floor (unless waived).
    floor_reason = _taint_floor_reason(config)
    if floor_reason is not None:
        return disabled(floor_reason)

    return GoogleIntegrationState(
        enabled=True,
        reason=None,
        taint_enforcement_waived=waived,
        enabled_tool_names=_enabled_tool_names(config),
    )
