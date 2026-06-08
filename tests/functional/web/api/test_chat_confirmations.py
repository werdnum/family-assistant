from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from family_assistant.config_models import AppConfig
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.context import get_db_context
from family_assistant.storage.tasks import tasks_table
from family_assistant.web.dependencies import get_current_user

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine


async def _create_confirmation(
    db_engine: AsyncEngine,
    *,
    request_user_id: str = "test_user",
    expires_at: datetime | None = None,
) -> str:
    service = ConfirmationService(
        db_context_factory=lambda: get_db_context(db_engine),
    )
    request = await service.create_request(
        target_user_id=request_user_id,
        tool_name="add_or_update_note",
        tool_args={"title": "Trip", "content": "Flight lands at 6pm"},
        tool_call_id="tool-call-123",
        source_message_internal_id=None,
        confirmation_prompt="Create a note for this itinerary?",
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=30),
    )
    return request["id"]


async def _task_exists(db_engine: AsyncEngine, task_id: str) -> bool:
    async with get_db_context(db_engine) as db:
        row = await db.fetch_one(
            select(tasks_table.c.id).where(tasks_table.c.original_task_id == task_id)
        )
    return row is not None


async def _resolved_interface(db_engine: AsyncEngine, request_id: str) -> str | None:
    async with get_db_context(db_engine) as db:
        row = await db.fetch_one(
            select(confirmation_requests_table.c.resolved_via_interface).where(
                confirmation_requests_table.c.id == request_id
            )
        )
    return row["resolved_via_interface"] if row else None


class _TokenAuthService:
    auth_enabled = True

    async def get_user_from_api_token(
        self,
        auth_header: str,
        request: Request,
    ) -> dict[str, object] | None:
        if auth_header == "Bearer test-token":
            return {
                "sub": "keycloak-subject",
                "email": "andrew@example.com",
                "source": "api_token",
                "token_id": 1,
            }
        return None


@pytest.mark.asyncio
async def test_pending_confirmations_lists_only_current_user_unexpired_requests(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)
    await _create_confirmation(db_engine, request_user_id="other_user")
    await _create_confirmation(
        db_engine,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    response = await api_test_client.get("/api/v1/chat/confirmations/pending")

    assert response.status_code == 200
    body = response.json()
    assert [item["request_id"] for item in body["confirmations"]] == [request_id]
    confirmation = body["confirmations"][0]
    assert confirmation["tool_name"] == "add_or_update_note"
    assert confirmation["tool_call_id"] == "tool-call-123"
    assert confirmation["args"] == {
        "title": "Trip",
        "content": "Flight lands at 6pm",
    }


@pytest.mark.asyncio
async def test_get_confirmation_returns_detail_for_owner(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)

    response = await api_test_client.get(f"/api/v1/chat/confirmations/{request_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == request_id
    assert body["tool_name"] == "add_or_update_note"
    assert body["tool_call_id"] == "tool-call-123"
    assert body["confirmation_prompt"] == "Create a note for this itinerary?"
    assert body["args"] == {"title": "Trip", "content": "Flight lands at 6pm"}
    assert body["status"] == "pending"
    assert body["time_remaining_seconds"] > 0


@pytest.mark.asyncio
async def test_get_confirmation_reports_status_after_resolution(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)

    await api_test_client.post(
        "/api/v1/chat/confirm_tool",
        json={"request_id": request_id, "approved": False},
    )

    response = await api_test_client.get(f"/api/v1/chat/confirmations/{request_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_get_confirmation_unknown_request_returns_404(
    api_test_client: AsyncClient,
) -> None:
    response = await api_test_client.get("/api/v1/chat/confirmations/confirm_missing")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_confirmation_for_other_user_returns_404(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine, request_user_id="owner_user")

    async def other_user() -> dict[str, object]:
        return {"user_identifier": "other_user"}

    app_fixture.dependency_overrides[get_current_user] = other_user
    try:
        response = await api_test_client.get(f"/api/v1/chat/confirmations/{request_id}")
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_approving_pending_confirmation_via_web_enqueues_execution_task(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)

    response = await api_test_client.post(
        "/api/v1/chat/confirm_tool",
        json={"request_id": request_id, "approved": True},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )

    pending_response = await api_test_client.get("/api/v1/chat/confirmations/pending")
    assert pending_response.json()["confirmations"] == []


@pytest.mark.asyncio
async def test_rejecting_pending_confirmation_via_web_does_not_enqueue_task(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)

    response = await api_test_client.post(
        "/api/v1/chat/confirm_tool",
        json={"request_id": request_id, "approved": False},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert not await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )


@pytest.mark.asyncio
async def test_confirm_tool_accepts_optional_ios_approving_interface(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)

    response = await api_test_client.post(
        "/api/v1/chat/confirm_tool",
        json={
            "request_id": request_id,
            "approved": True,
            "conversation_id": "web_conv_ios",
            "approving_interface": "ios",
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert await _resolved_interface(db_engine, request_id) == "ios"
    assert await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )


@pytest.mark.asyncio
async def test_confirm_tool_rejects_unsupported_approving_interface(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)

    response = await api_test_client.post(
        "/api/v1/chat/confirm_tool",
        json={
            "request_id": request_id,
            "approved": True,
            "approving_interface": "x" * 51,
        },
    )

    assert response.status_code == 422
    assert await _resolved_interface(db_engine, request_id) is None
    assert not await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )


@pytest.mark.asyncio
async def test_other_web_user_cannot_list_or_resolve_confirmation(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine, request_user_id="owner_user")

    async def other_user() -> dict[str, object]:
        return {"user_identifier": "other_user"}

    app_fixture.dependency_overrides[get_current_user] = other_user
    try:
        list_response = await api_test_client.get("/api/v1/chat/confirmations/pending")
        confirm_response = await api_test_client.post(
            "/api/v1/chat/confirm_tool",
            json={"request_id": request_id, "approved": True},
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert list_response.status_code == 200
    assert list_response.json()["confirmations"] == []
    assert confirm_response.status_code == 200
    assert confirm_response.json()["success"] is False
    assert not await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )


@pytest.mark.asyncio
async def test_web_can_approve_confirmation_created_for_matching_telegram_user(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    canonical_user_id = "andrew@example.com"
    app_fixture.state.config = AppConfig.model_validate({
        "users": [
            {
                "id": canonical_user_id,
                "oidc": {
                    "emails": ["andrew@example.com"],
                    "subjects": ["keycloak-subject"],
                },
                "telegram": {"user_ids": [123456789]},
            }
        ]
    })
    app_fixture.state.user_identity_resolver = UserIdentityResolver(
        app_fixture.state.config
    )
    app_fixture.state.auth_service = _TokenAuthService()

    request_id = await _create_confirmation(
        db_engine,
        request_user_id=canonical_user_id,
    )

    headers = {"Authorization": "Bearer test-token"}
    pending_response = await api_test_client.get(
        "/api/v1/chat/confirmations/pending",
        headers=headers,
    )
    me_response = await api_test_client.get("/api/me", headers=headers)
    auth_me_response = await api_test_client.get("/api/auth/me", headers=headers)
    approve_response = await api_test_client.post(
        "/api/v1/chat/confirm_tool",
        json={"request_id": request_id, "approved": True},
        headers=headers,
    )

    assert pending_response.status_code == 200
    assert [
        item["request_id"] for item in pending_response.json()["confirmations"]
    ] == [request_id]
    assert me_response.status_code == 200
    assert me_response.json()["user_identifier"] == canonical_user_id
    assert auth_me_response.status_code == 200
    assert auth_me_response.json()["user_identifier"] == canonical_user_id
    assert approve_response.status_code == 200
    assert approve_response.json()["success"] is True
    assert await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )
