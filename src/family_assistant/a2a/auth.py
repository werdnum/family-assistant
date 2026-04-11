"""Authentication configuration for A2A agent connections."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Generator


class A2AAuthConfig(BaseModel):
    """Authentication configuration for an A2A agent.

    Credentials are always read from environment variables, never stored directly.
    """

    type: Literal["bearer", "api_key", "none"] = "none"
    token_env: str | None = None
    header_name: str = "Authorization"

    def to_httpx_auth(self) -> httpx.Auth | None:
        """Build an httpx Auth object from this config, or None for no auth."""
        if self.type == "none":
            return None

        if self.token_env is None:
            raise ValueError(
                f"A2A auth type '{self.type}' requires token_env to be set"
            )

        token = os.environ.get(self.token_env)
        if not token:
            raise ValueError(
                f"Environment variable '{self.token_env}' is not set or empty"
            )

        if self.type == "bearer":
            return _BearerAuth(token)
        if self.type == "api_key":
            return _HeaderAuth(self.header_name, token)

        raise ValueError(f"Unsupported auth type: {self.type}")

    def validate_env_vars(self) -> list[str]:
        """Check that required env vars are present. Returns list of errors."""
        errors: list[str] = []
        if self.type == "none":
            return errors
        if self.token_env is None:
            errors.append(f"Auth type '{self.type}' requires token_env")
        elif not os.environ.get(self.token_env):
            errors.append(f"Environment variable '{self.token_env}' is not set")
        return errors


class _BearerAuth(httpx.Auth):
    """httpx auth that adds a Bearer token header."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class _HeaderAuth(httpx.Auth):
    """httpx auth that adds a custom header."""

    def __init__(self, header_name: str, value: str) -> None:
        self._header_name = header_name
        self._value = value

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        request.headers[self._header_name] = self._value
        yield request
