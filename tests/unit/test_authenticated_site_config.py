"""Startup validation for `authenticated_sites`.

The effective-surface check is a security control that has to fail closed, so
every rejection path gets a test: a configuration that would silently widen an
authenticated run must stop the application from starting.
"""

# ast-grep-ignore-block: no-dict-any - config dicts are re-validated dynamically

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig
from family_assistant.tools import LOCAL_TOOL_METADATA_BY_NAME
from family_assistant.tools.authenticated_site_surface import admissible_tools

VALID_SITE: dict[str, Any] = {
    "display_name": "HelloFresh",
    "jar_id": "jar_0123456789abcdef",
    "start_url": "https://www.hellofresh.com.au/menus",
    "authenticated_origins": ["https://www.hellofresh.com.au"],
    "navigation_allowlist": [],
    "credential_alias": "hellofresh",
    "authorized_users": ["andrew"],
    "caller_profiles": ["default_assistant"],
    "damage_envelope": "Meal selections and reversible preferences only.",
}


@pytest.fixture(scope="module")
def shipped_config_data() -> dict[str, Any]:
    """The shipped defaults, as a plain dict to re-validate variants of."""
    return load_config("defaults.yaml").model_dump()


def _validate(config_data: dict[str, Any], sites: dict[str, Any]) -> AppConfig:
    return AppConfig.model_validate({**config_data, "authenticated_sites": sites})


def _profile(config_data: dict[str, Any], profile_id: str) -> dict[str, Any]:
    for profile in config_data["service_profiles"]:
        if profile["id"] == profile_id:
            return profile
    raise AssertionError(f"no profile {profile_id!r} in the shipped configuration")


def _with_profile_change(
    config_data: dict[str, Any], profile_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """A copy of the configuration with one profile's fields replaced."""
    profiles = [
        {**profile, **changes} if profile["id"] == profile_id else profile
        for profile in config_data["service_profiles"]
    ]
    return {**config_data, "service_profiles": profiles}


def test_shipped_defaults_configure_no_sites() -> None:
    """A deployment that configures nothing cannot run an authenticated task."""
    assert load_config("defaults.yaml").authenticated_sites == {}


def test_shipped_authenticated_profiles_satisfy_the_validator(
    shipped_config_data: dict[str, Any],
) -> None:
    config = _validate(shipped_config_data, {"hellofresh": VALID_SITE})
    site = config.authenticated_sites["hellofresh"]
    assert site.browser_profile == "authenticated_browser_profile"
    assert site.visual_profile == "authenticated_browser_visual_profile"
    assert site.effective_origins == {"https://www.hellofresh.com.au"}


def test_shipped_browser_profile_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    """The shipped semantic profile reaches past the authenticated boundary."""
    with pytest.raises(ValidationError) as failure:
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "browser_profile": "browser_profile"}},
        )
    message = str(failure.value)
    assert "browser_exec" in message
    assert "not browser-server-mediated" in message


def test_shipped_visual_profile_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    """Naming the shipped visual profile breaks the pin as well as the surface.

    The semantic profile's delegation is pinned to the *site's* configured
    visual profile, so redirecting a site at the shipped one is rejected before
    that profile's own drag_and_drop grant is even reached -- which is the point
    of pinning to the configured id rather than to a fixed name.
    """
    with pytest.raises(ValidationError, match="without pinning target_service_id"):
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "visual_profile": "browser_visual_profile"}},
        )


def test_admissible_set_is_derived_from_the_tool_table() -> None:
    """The admissible set is the browser-server-mediated tools, minus the leaks."""
    admissible = admissible_tools(LOCAL_TOOL_METADATA_BY_NAME)
    assert {"browser_snapshot", "browser_autofill", "attach_to_response"} <= admissible
    assert admissible.isdisjoint({"browser_exec", "browser_extract", "drag_and_drop"})
    # A tool that reaches the network on its own is not tagged BROWSER, so it
    # is excluded without anyone having to remember to list it.
    assert admissible.isdisjoint({"ucp_add_to_cart", "spawn_worker", "gmail_search"})


def test_unknown_profile_is_rejected(shipped_config_data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="not a configured service profile"):
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "browser_profile": "nope"}},
        )


def test_unknown_caller_profile_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="which do not exist"):
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "caller_profiles": ["ghost"]}},
        )


def test_site_with_no_acquisition_path_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="login-acquisition"):
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "jar_id": None, "credential_alias": None}},
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://www.hellofresh.com.au",
        "https://www.hellofresh.com.au/menus",
        "www.hellofresh.com.au",
    ],
)
def test_inexact_origins_are_rejected(
    shipped_config_data: dict[str, Any], origin: str
) -> None:
    with pytest.raises(ValidationError, match="exact https origin"):
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "authenticated_origins": [origin]}},
        )


def test_start_url_outside_the_origin_set_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="confined away from its own"):
        _validate(
            shipped_config_data,
            {"hellofresh": {**VALID_SITE, "start_url": "https://example.com/x"}},
        )


def test_profile_granting_a_non_browser_tool_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    """A tool that reaches the network on its own is outside the admissible set."""
    policy = {
        "default_decision": "deny",
        "rules": [
            {
                "match": {"names": ["browser_snapshot", "ucp_add_to_cart"]},
                "decision": "allow",
                "priority": 10,
            }
        ],
    }
    data = _with_profile_change(
        shipped_config_data, "authenticated_browser_profile", {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="ucp_add_to_cart"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_granting_by_tag_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    """A tag matcher cannot be checked statically, so it fails closed."""
    policy = {
        "default_decision": "deny",
        "rules": [
            {
                "match": {"tags_any": ["browser"]},
                "decision": "allow",
                "priority": 10,
            }
        ],
    }
    data = _with_profile_change(
        shipped_config_data, "authenticated_browser_profile", {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="grants by tag or MCP server"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_granting_a_glob_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    policy = {
        "default_decision": "deny",
        "rules": [
            {"match": {"names": ["browser_*"]}, "decision": "allow", "priority": 10}
        ],
    }
    data = _with_profile_change(
        shipped_config_data, "authenticated_browser_profile", {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="must name each tool literally"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_allowing_open_delegation_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    """An unpinned delegation grant reaches household-capable profiles."""
    policy = {
        "default_decision": "deny",
        "rules": [
            {
                "match": {"names": ["delegate_to_service"]},
                "decision": "allow",
                "priority": 10,
            }
        ],
    }
    data = _with_profile_change(
        shipped_config_data, "authenticated_browser_profile", {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="without pinning target_service_id"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_pinned_to_the_wrong_visual_profile_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    policy = {
        "default_decision": "deny",
        "rules": [
            {
                "match": {
                    "names": ["delegate_to_service"],
                    "argument_equals": {"target_service_id": "complex_tasks"},
                },
                "decision": "allow",
                "priority": 99,
            }
        ],
    }
    data = _with_profile_change(
        shipped_config_data, "authenticated_browser_profile", {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="without pinning target_service_id"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_visual_profile_may_not_delegate_at_all(
    shipped_config_data: dict[str, Any],
) -> None:
    policy = {
        "default_decision": "deny",
        "rules": [
            {
                "match": {
                    "names": ["delegate_to_service"],
                    "argument_equals": {
                        "target_service_id": "authenticated_browser_profile"
                    },
                },
                "decision": "allow",
                "priority": 99,
            }
        ],
    }
    data = _with_profile_change(
        shipped_config_data,
        "authenticated_browser_visual_profile",
        {"tools_policy": policy},
    )
    with pytest.raises(ValidationError, match="must not delegate to anything"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_without_default_deny_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    profile = _profile(shipped_config_data, "authenticated_browser_profile")
    policy = {**profile["tools_policy"], "default_decision": "allow"}
    data = _with_profile_change(
        shipped_config_data, "authenticated_browser_profile", {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="default_decision: deny"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_retaining_a_global_grant_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    """A profile's own policy cannot refuse a global grant, so it must exclude it."""
    data = _with_profile_change(
        shipped_config_data,
        "authenticated_browser_profile",
        {"excluded_global_tools": ["jq_query", "report_technical_problem"]},
    )
    with pytest.raises(ValidationError, match="read_text_attachment"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_keeping_ambient_context_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    profile = _profile(shipped_config_data, "authenticated_browser_profile")
    processing = {
        **profile["processing_config"],
        "excluded_context_providers": ["notes", "calendar"],
    }
    data = _with_profile_change(
        shipped_config_data,
        "authenticated_browser_profile",
        {"processing_config": processing},
    )
    with pytest.raises(ValidationError, match="known_users"):
        _validate(data, {"hellofresh": VALID_SITE})


def test_profile_with_aggregated_context_is_rejected(
    shipped_config_data: dict[str, Any],
) -> None:
    profile = _profile(shipped_config_data, "authenticated_browser_profile")
    processing = {**profile["processing_config"], "include_aggregated_context": True}
    data = _with_profile_change(
        shipped_config_data,
        "authenticated_browser_profile",
        {"processing_config": processing},
    )
    with pytest.raises(ValidationError, match="include_aggregated_context"):
        _validate(data, {"hellofresh": VALID_SITE})


@pytest.mark.parametrize(
    "profile_id",
    ["authenticated_browser_profile", "authenticated_browser_visual_profile"],
)
def test_authenticated_profiles_cannot_grant_arbitrary_handback(
    shipped_config_data: dict[str, Any], profile_id: str
) -> None:
    policy = {
        "default_decision": "deny",
        "rules": [
            {"match": {"names": ["browser_claim_handback"]}, "decision": "allow"}
        ],
    }
    data = _with_profile_change(
        shipped_config_data, profile_id, {"tools_policy": policy}
    )
    with pytest.raises(ValidationError, match="browser_claim_handback"):
        _validate(data, {"hellofresh": VALID_SITE})
