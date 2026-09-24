"""Configuration of an external authorization server for the MCP adapter."""

import pytest
from pydantic import ValidationError

from family_assistant.config_models import MCPExternalAuthorizationServer


@pytest.mark.parametrize("field", ["issuer", "jwks_uri"])
def test_malformed_url_is_refused(field: str) -> None:
    values = {
        "issuer": "https://id.example.com/realms/household",
        "jwks_uri": "https://id.example.com/realms/household/certs",
        "audience": "family-assistant-mcp",
        field: "id.example.com/realms/household",
    }

    with pytest.raises(ValidationError, match=field):
        MCPExternalAuthorizationServer.model_validate(values)


def test_issuer_is_kept_exactly_as_written() -> None:
    server = MCPExternalAuthorizationServer(
        issuer="https://id.example.com",
        jwks_uri="http://keycloak.default.svc:8080/certs",
        audience="family-assistant-mcp",
    )

    assert server.issuer == "https://id.example.com"
