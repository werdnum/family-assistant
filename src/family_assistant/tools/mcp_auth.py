"""Configuration shared by the MCP runtime and application config."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MCPUserCredentialConfig(BaseModel):
    """An environment-backed credential for one canonical application user."""

    model_config = ConfigDict(extra="forbid")

    token_env: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    expected_user_id: str = Field(min_length=1)


class MCPIdentityCheckConfig(BaseModel):
    """Verify the effective principal using a same-origin identity endpoint."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="/api/me", pattern=r"^/[^/\\].*$")
    expected_agent: str = Field(min_length=1)
    require_write: bool = True


class MCPUserAuthConfig(BaseModel):
    """Select a bearer credential using the trusted execution-context user."""

    model_config = ConfigDict(extra="forbid")

    call_timeout_seconds: float = Field(default=60, gt=0)
    users: dict[str, MCPUserCredentialConfig] = Field(min_length=1)
    identity_check: MCPIdentityCheckConfig | None = None
