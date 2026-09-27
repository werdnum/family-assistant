"""Validate user-scoped MCP configuration without exposing credentials."""

import pytest
from pydantic import ValidationError

from family_assistant.config_models import MCPServerConfig
from family_assistant.tools.mcp_auth import MCPIdentityCheckConfig


@pytest.mark.parametrize("transport,token", [("stdio", None), ("http", "shared-token")])
def test_user_auth_rejects_ambiguous_configuration(
    transport: str, token: str | None
) -> None:
    with pytest.raises(ValidationError, match="user_auth requires"):
        MCPServerConfig.model_validate({
            "transport": transport,
            "token": token,
            "user_auth": {
                "users": {
                    "alex": {"token_env": "TUIT_ALEX", "expected_user_id": "alex"}
                }
            },
        })


@pytest.mark.parametrize(
    "path", ["https://other.test/me", "//other.test/me", "/\\other.test/me"]
)
def test_identity_check_cannot_send_token_to_another_origin(path: str) -> None:
    with pytest.raises(ValidationError):
        MCPIdentityCheckConfig(path=path, expected_agent="family-assistant")


def test_user_auth_contains_only_secret_references() -> None:
    config = MCPServerConfig.model_validate({
        "transport": "http",
        "user_auth": {
            "users": {"alex": {"token_env": "TUIT_ALEX", "expected_user_id": "alex"}}
        },
    })
    assert config.model_dump()["user_auth"]["users"]["alex"]["token_env"] == "TUIT_ALEX"
