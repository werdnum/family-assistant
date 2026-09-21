"""Startup validation for the MCP adapter (docs/design/mcp-adapter.md)."""

import pytest

from family_assistant.config_models import AppConfig, MCPAdapterConfig
from family_assistant.web.mcp_adapter.config import require_authentication_for_adapter


def _config(enabled: bool) -> AppConfig:
    return AppConfig(mcp_adapter=MCPAdapterConfig(enabled=enabled))


def test_enabled_adapter_without_authentication_is_a_startup_error() -> None:
    with pytest.raises(ValueError, match="mcp_adapter.enabled requires authentication"):
        require_authentication_for_adapter(
            _config(enabled=True), oidc_enabled=False, jwt_enabled=False
        )


@pytest.mark.parametrize(
    ("oidc_enabled", "jwt_enabled"), [(True, False), (False, True), (True, True)]
)
def test_enabled_adapter_with_an_authentication_mode_starts(
    oidc_enabled: bool, jwt_enabled: bool
) -> None:
    require_authentication_for_adapter(
        _config(enabled=True), oidc_enabled=oidc_enabled, jwt_enabled=jwt_enabled
    )


def test_disabled_adapter_needs_no_authentication() -> None:
    require_authentication_for_adapter(
        _config(enabled=False), oidc_enabled=False, jwt_enabled=False
    )
