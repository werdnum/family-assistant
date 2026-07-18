"""Unit tests for the Google integration startup-enablement evaluator.

Covers the ordered enablement conditions and their reasons, the read-only scope
allowlist rejection, and the taint-floor semantics — including the design's
motivating case where a full ``matrix:`` replacement silently drops a floor sink
below confirm, and the explicit waiver path. All taint config is built through
real :class:`TaintPolicyConfig` parsing so the floor exercises the actual
runtime evaluator, not a re-derived approximation.
"""

from __future__ import annotations

from family_assistant.config_models import (
    AppConfig,
    GoogleIntegrationConfig,
    ServiceProfile,
    TaintPolicyConfig,
)
from family_assistant.services.credential_encryption import CredentialEncryption
from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope
from family_assistant.services.oauth_integration_state import (
    evaluate_oauth_integration_state,
)
from family_assistant.tools import ToolPolicyConfig
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES

GMAIL_SCOPE = GoogleScope.GMAIL_READONLY.value
DRIVE_SCOPE = GoogleScope.DRIVE_READONLY.value
DRIVE_METADATA_SCOPE = GoogleScope.DRIVE_METADATA_READONLY.value


def _valid_key() -> str:
    return CredentialEncryption.generate_key()


def _google_config(**overrides: object) -> GoogleIntegrationConfig:
    base: dict[str, object] = {
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
        "credential_encryption_key": _valid_key(),
        "scopes": [GMAIL_SCOPE, DRIVE_SCOPE],
        "require_taint_enforcement": True,
    }
    base.update(overrides)
    return GoogleIntegrationConfig.model_validate(base)


def _allow_google_policy() -> ToolPolicyConfig:
    """Profile policy that allows the Google tools by name."""
    return ToolPolicyConfig.model_validate({
        "default_decision": "deny",
        "rules": [
            {
                "match": {
                    "names": [
                        "gmail_search",
                        "gmail_get_message",
                        "gmail_get_attachment",
                        "drive_search",
                        "drive_get_file",
                    ]
                },
                "decision": "allow",
                "priority": 10,
            }
        ],
    })


def _deny_all_policy() -> ToolPolicyConfig:
    return ToolPolicyConfig.model_validate({"default_decision": "deny", "rules": []})


def _profile(
    profile_id: str,
    *,
    tools_policy: ToolPolicyConfig | None,
    taint_policy: TaintPolicyConfig | None = None,
) -> ServiceProfile:
    return ServiceProfile(
        id=profile_id,
        tools_policy=tools_policy,
        taint_policy=taint_policy,
    )


def _users() -> list[dict[str, object]]:
    """A minimal populated users block so OIDC identities resolve canonically."""
    return [{"id": "alice", "oidc": {"emails": ["alice@example.com"]}}]


def _app_config(
    google: GoogleIntegrationConfig,
    *,
    taint_policy: TaintPolicyConfig | None = None,
    profiles: list[ServiceProfile] | None = None,
    users: list[dict[str, object]] | None = None,
) -> AppConfig:
    kwargs: dict[str, object] = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "google_integration": google,
        "users": _users() if users is None else users,
    }
    if taint_policy is not None:
        kwargs["taint_policy"] = taint_policy
    if profiles is not None:
        kwargs["service_profiles"] = profiles
    return AppConfig.model_validate(kwargs)


def _enforce_taint() -> TaintPolicyConfig:
    return TaintPolicyConfig.model_validate({"mode": "enforce"})


# --- Ordered enablement conditions -----------------------------------------


def test_not_configured_when_all_fields_empty() -> None:
    config = _app_config(
        _google_config(
            oauth_client_id="",
            oauth_client_secret="",
            credential_encryption_key="",
        )
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert state.reason == "Google integration is not configured."
    assert state.enabled_tool_names == frozenset()


def test_missing_client_id_names_the_field() -> None:
    config = _app_config(_google_config(oauth_client_id=""))
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "GOOGLE_OAUTH_CLIENT_ID" in (state.reason or "")


def test_missing_client_secret_names_the_field() -> None:
    config = _app_config(_google_config(oauth_client_secret=""))
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in (state.reason or "")


def test_missing_encryption_key_names_the_field() -> None:
    config = _app_config(_google_config(credential_encryption_key=""))
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "CREDENTIAL_ENCRYPTION_KEY" in (state.reason or "")


def test_malformed_encryption_key_disables() -> None:
    config = _app_config(_google_config(credential_encryption_key="not-a-fernet-key"))
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "Fernet" in (state.reason or "")


def test_unsupported_scope_disables_with_clear_error() -> None:
    config = _app_config(
        _google_config(
            scopes=[GMAIL_SCOPE, "https://www.googleapis.com/auth/gmail.send"]
        )
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "gmail.send" in (state.reason or "")


def test_auth_disabled_refuses_even_when_fully_configured() -> None:
    config = _app_config(
        _google_config(require_taint_enforcement=False),
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=False,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "web authentication" in (state.reason or "")


def test_empty_users_block_refuses_even_with_auth_enabled() -> None:
    # With OIDC on but no users block, connections would be keyed by raw OIDC
    # identifiers instead of canonical user ids, so enablement must refuse.
    config = _app_config(
        _google_config(require_taint_enforcement=False),
        users=[],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "users block" in (state.reason or "")


# --- Taint floor ------------------------------------------------------------


def test_floor_passes_with_enforce_and_default_matrix() -> None:
    config = _app_config(
        _google_config(),
        taint_policy=_enforce_taint(),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is True
    assert state.reason is None
    assert state.taint_enforcement_waived is False


def test_floor_fails_when_mode_is_observe() -> None:
    config = _app_config(
        _google_config(),
        taint_policy=TaintPolicyConfig.model_validate({"mode": "observe"}),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "taint_policy.mode" in (state.reason or "")
    assert "observe" in (state.reason or "")


def test_floor_fails_on_full_matrix_replacement_dropping_read_broadening() -> None:
    # The design's motivating case: a full matrix replacement in enforce mode that
    # silently softens sensitive_read_broadening to 'audit' at unknown_external.
    # Building through real TaintPolicyConfig parsing means the REAL evaluator
    # (not a re-derived approximation) must catch it.
    matrix = {
        "trusted_user": {
            "user_local": "allow",
            "home_local": "allow",
            "artifact_write": "allow",
            "sensitive_read_broadening": "allow",
        },
        "unknown_external": {
            "user_local": "allow",
            "home_local": "allow",
            "artifact_write": "audit",
            "arbitrary_external_message": "confirm",
            "attacker_addressable_egress": "confirm",
            "sandbox_network": "deny",
            # Silently softened below confirm — the floor must reject this.
            "sensitive_read_broadening": "audit",
        },
    }
    config = _app_config(
        _google_config(),
        taint_policy=TaintPolicyConfig.model_validate({
            "mode": "enforce",
            "matrix": matrix,
        }),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "sensitive_read_broadening" in (state.reason or "")
    assert "default_assistant" in (state.reason or "")


def test_floor_skipped_and_waived_when_requirement_false_even_in_observe() -> None:
    config = _app_config(
        _google_config(require_taint_enforcement=False),
        taint_policy=TaintPolicyConfig.model_validate({"mode": "observe"}),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is True
    assert state.taint_enforcement_waived is True
    assert state.reason is None


def test_floor_ignores_profiles_that_do_not_allow_google_tools() -> None:
    # A profile with a matrix that would fail the floor is irrelevant when it does
    # not allow any Google tool, so the integration still enables.
    weak_taint = TaintPolicyConfig.model_validate({
        "mode": "enforce",
        "matrix_overrides": {
            "unknown_external": {"sensitive_read_broadening": "audit"}
        },
    })
    config = _app_config(
        _google_config(),
        taint_policy=_enforce_taint(),
        profiles=[
            _profile("default_assistant", tools_policy=_allow_google_policy()),
            _profile(
                "email_intake",
                tools_policy=_deny_all_policy(),
                taint_policy=weak_taint,
            ),
        ],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is True


def test_floor_rejects_profile_taint_policy_that_relaxes_a_floor_sink() -> None:
    # A Google-enabled profile whose own taint_policy softens a floor sink is
    # rejected by the merge guard; the floor surfaces that as a disabling reason
    # rather than crashing startup.
    weak_profile_taint = TaintPolicyConfig.model_validate({
        "matrix_overrides": {
            "unknown_external": {"attacker_addressable_egress": "audit"}
        }
    })
    config = _app_config(
        _google_config(),
        taint_policy=_enforce_taint(),
        profiles=[
            _profile(
                "default_assistant",
                tools_policy=_allow_google_policy(),
                taint_policy=weak_profile_taint,
            )
        ],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is False
    assert "default_assistant" in (state.reason or "")
    assert "relaxes" in (state.reason or "")


# --- Scope-conditional tool names ------------------------------------------


def test_enabled_tool_names_gmail_only() -> None:
    config = _app_config(
        _google_config(scopes=[GMAIL_SCOPE]),
        taint_policy=_enforce_taint(),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is True
    assert state.enabled_tool_names == frozenset({
        "gmail_search",
        "gmail_get_message",
        "gmail_get_attachment",
    })


def test_enabled_tool_names_drive_metadata_only_has_search_not_get_file() -> None:
    config = _app_config(
        _google_config(scopes=[DRIVE_METADATA_SCOPE]),
        taint_policy=_enforce_taint(),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled is True
    assert "drive_search" in state.enabled_tool_names
    assert "drive_get_file" not in state.enabled_tool_names


def test_enabled_tool_names_full_scopes() -> None:
    config = _app_config(
        _google_config(scopes=[GMAIL_SCOPE, DRIVE_SCOPE]),
        taint_policy=_enforce_taint(),
        profiles=[_profile("default_assistant", tools_policy=_allow_google_policy())],
    )
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    assert state.enabled_tool_names == frozenset({
        "gmail_search",
        "gmail_get_message",
        "gmail_get_attachment",
        "drive_search",
        "drive_get_file",
    })
