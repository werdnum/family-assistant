"""HTTP-route tests for owner-scoped attachment enforcement.

The attachment registry records an optional ``owner_user_id`` on personal-data
tool output. The attachment routes must refuse an owned attachment to any user
other than its owner (404, indistinguishable from missing), require auth on the
metadata route, and serve owned attachments with ``Cache-Control: private,
no-store`` so a shared cache can't leak one user's file to another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.storage.context import get_db_context
from family_assistant.web.dependencies import get_current_user

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.attachment_registry import AttachmentRegistry

OWNER = "owner_user"


async def _register(
    registry: AttachmentRegistry,
    db_engine: AsyncEngine,
    *,
    owner_user_id: str | None,
) -> str:
    async with get_db_context(engine=db_engine) as db_context:
        metadata = await registry.store_and_register_tool_attachment(
            file_content=b"personal payload",
            filename="personal.txt",
            content_type="text/plain",
            tool_name="gmail_get_attachment",
            owner_user_id=owner_user_id,
            db_context=db_context,
        )
    return metadata.attachment_id


def _as_user(app_fixture: FastAPI, user_identifier: str) -> None:
    async def _override() -> dict[str, object]:
        return {"user_identifier": user_identifier}

    app_fixture.dependency_overrides[get_current_user] = _override


@pytest.mark.asyncio
async def test_serve_owned_attachment_404_for_non_owner(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
) -> None:
    attachment_id = await _register(
        attachment_registry_fixture, db_engine, owner_user_id=OWNER
    )

    _as_user(app_fixture, "intruder")
    try:
        response = await api_test_client.get(f"/api/attachments/{attachment_id}")
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_serve_owned_attachment_ok_for_owner_private_cache(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
) -> None:
    attachment_id = await _register(
        attachment_registry_fixture, db_engine, owner_user_id=OWNER
    )

    _as_user(app_fixture, OWNER)
    try:
        response = await api_test_client.get(f"/api/attachments/{attachment_id}")
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_serve_ownerless_attachment_keeps_public_immutable_cache(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
) -> None:
    attachment_id = await _register(
        attachment_registry_fixture, db_engine, owner_user_id=None
    )

    # Default session user (test_user) — ownerless is visible to everyone.
    response = await api_test_client.get(f"/api/attachments/{attachment_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_metadata_owned_attachment_404_for_non_owner(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
) -> None:
    attachment_id = await _register(
        attachment_registry_fixture, db_engine, owner_user_id=OWNER
    )

    _as_user(app_fixture, "intruder")
    try:
        response = await api_test_client.get(
            f"/api/attachments/{attachment_id}/metadata"
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_metadata_owned_attachment_ok_for_owner(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
) -> None:
    attachment_id = await _register(
        attachment_registry_fixture, db_engine, owner_user_id=OWNER
    )

    _as_user(app_fixture, OWNER)
    try:
        response = await api_test_client.get(
            f"/api/attachments/{attachment_id}/metadata"
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["id"] == attachment_id


@pytest.mark.asyncio
async def test_delete_owned_attachment_404_for_non_owner(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
) -> None:
    attachment_id = await _register(
        attachment_registry_fixture, db_engine, owner_user_id=OWNER
    )

    _as_user(app_fixture, "intruder")
    try:
        response = await api_test_client.delete(f"/api/attachments/{attachment_id}")
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404

    # The owner can still read it: the failed delete left the row intact.
    async with get_db_context(engine=db_engine) as db_context:
        assert (
            await attachment_registry_fixture.get_attachment(
                db_context, attachment_id, acting_user_id=OWNER
            )
            is not None
        )
