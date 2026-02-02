from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request, status

from family_assistant.web.auth import AuthService


@pytest.fixture
def mock_oauth() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_service(mock_oauth: MagicMock) -> AuthService:
    service = AuthService()
    service.oauth = mock_oauth
    return service


@pytest.fixture
def mock_request() -> MagicMock:
    request = MagicMock(spec=Request)
    request.session = {}
    return request


@pytest.mark.asyncio
async def test_handle_auth_callback_no_allowlist(
    auth_service: AuthService, mock_request: MagicMock, mock_oauth: MagicMock
) -> None:
    """Test that any email is allowed when ALLOWED_OIDC_EMAILS is not set."""
    with patch("family_assistant.web.auth.ALLOWED_OIDC_EMAILS", None):
        user_info = {"email": "test@example.com", "sub": "123"}

        # authlib authorize_access_token is async
        # ast-grep-ignore: no-dict-any
        async def mock_authorize(*args: object, **kwargs: object) -> dict[str, Any]:  # noqa: ARG001
            return {"userinfo": user_info}

        mock_oauth.oidc_provider.authorize_access_token = mock_authorize

        mock_request.session = {"redirect_after_login": "/"}

        response = await auth_service.handle_auth_callback(mock_request)

        # Starlette's RedirectResponse defaults to 307
        assert response.status_code in {302, 307}
        assert mock_request.session["user"] == user_info


@pytest.mark.asyncio
async def test_handle_auth_callback_with_allowlist_success(
    auth_service: AuthService, mock_request: MagicMock, mock_oauth: MagicMock
) -> None:
    """Test that email in allowlist is allowed."""
    with patch(
        "family_assistant.web.auth.ALLOWED_OIDC_EMAILS",
        "allowed@example.com, second@test.com",
    ):
        user_info = {
            "email": "ALLOWED@example.com",
            "sub": "123",
        }  # Case insensitive check

        # ast-grep-ignore: no-dict-any
        async def mock_authorize(*args: object, **kwargs: object) -> dict[str, Any]:  # noqa: ARG001
            return {"userinfo": user_info}

        mock_oauth.oidc_provider.authorize_access_token = mock_authorize

        mock_request.session = {"redirect_after_login": "/"}

        response = await auth_service.handle_auth_callback(mock_request)

        assert response.status_code in {302, 307}
        assert mock_request.session["user"] == user_info


@pytest.mark.asyncio
async def test_handle_auth_callback_with_allowlist_denied(
    auth_service: AuthService, mock_request: MagicMock, mock_oauth: MagicMock
) -> None:
    """Test that email NOT in allowlist is denied."""
    with patch("family_assistant.web.auth.ALLOWED_OIDC_EMAILS", "allowed@example.com"):
        user_info = {"email": "hacker@example.com", "sub": "123"}

        # ast-grep-ignore: no-dict-any
        async def mock_authorize(*args: object, **kwargs: object) -> dict[str, Any]:  # noqa: ARG001
            return {"userinfo": user_info}

        mock_oauth.oidc_provider.authorize_access_token = mock_authorize

        with pytest.raises(HTTPException) as excinfo:
            await auth_service.handle_auth_callback(mock_request)

        assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
        assert "not in the allowlist" in excinfo.value.detail


@pytest.mark.asyncio
async def test_handle_auth_callback_with_allowlist_no_email(
    auth_service: AuthService, mock_request: MagicMock, mock_oauth: MagicMock
) -> None:
    """Test that missing email is denied when allowlist is active."""
    with patch("family_assistant.web.auth.ALLOWED_OIDC_EMAILS", "allowed@example.com"):
        user_info = {"sub": "123"}  # No email

        # ast-grep-ignore: no-dict-any
        async def mock_authorize(*args: object, **kwargs: object) -> dict[str, Any]:  # noqa: ARG001
            return {"userinfo": user_info}

        mock_oauth.oidc_provider.authorize_access_token = mock_authorize

        with pytest.raises(HTTPException) as excinfo:
            await auth_service.handle_auth_callback(mock_request)

        assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
        assert "No email provided" in excinfo.value.detail
