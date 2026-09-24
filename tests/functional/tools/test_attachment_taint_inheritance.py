"""Attachment-reading tools take the taint of the attachment they read."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TurnTaintState,
    unknown_external_taint_metadata,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.attachments import read_text_attachment_tool
from family_assistant.tools.data_manipulation import jq_query_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


async def _read_text(context: ToolExecutionContext, attachment_id: str) -> ToolResult:
    return await read_text_attachment_tool(context, attachment_id)


async def _jq(context: ToolExecutionContext, attachment_id: str) -> ToolResult:
    return await jq_query_tool(context, attachment_id, jq_program=".name")


@pytest.mark.parametrize("read", [_read_text, _jq], ids=["read_text", "jq_query"])
@pytest.mark.parametrize(
    ("provenance", "expected_tier"),
    [
        # An authenticated user's own upload is stamped with an empty state.
        (
            {"taint_metadata": TurnTaintState.empty().to_metadata()},
            SourceTrustTier.TRUSTED_USER,
        ),
        (
            {"taint_metadata": unknown_external_taint_metadata("emailed file")},
            SourceTrustTier.UNKNOWN_EXTERNAL,
        ),
        ({}, SourceTrustTier.UNKNOWN_EXTERNAL),
    ],
    ids=["household-upload", "external-attachment", "unstamped-attachment"],
)
@pytest.mark.asyncio
async def test_attachment_read_takes_the_attachments_taint(
    db_engine: AsyncEngine,
    tmp_path: Path,
    read: Callable[[ToolExecutionContext, str], Awaitable[ToolResult]],
    # ast-grep-ignore: no-dict-any - free-form attachment metadata, as stored
    provenance: dict[str, Any],
    expected_tier: SourceTrustTier,
) -> None:
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    db_context = Database(db_engine)
    attachment_id = str(uuid.uuid4())
    content = json.dumps({"name": "Alice"}).encode("utf-8")
    file_path = registry._get_file_path(attachment_id, "data.json")
    file_path.write_bytes(content)
    await registry.register_attachment(
        db_context=db_context,
        attachment_id=attachment_id,
        source_type="tool",
        source_id="test",
        mime_type="application/json",
        description="test data",
        size=len(content),
        storage_path=str(file_path),
        conversation_id="conversation",
        metadata=provenance,
    )
    tracker = InMemoryTurnTaintTracker()
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id="turn-1",
        db_context=db_context,
        attachment_registry=registry,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        taint_tracker=tracker,
        credential_resolvers=None,
        api_backend=None,
    )

    result = await read(context, attachment_id)

    assert "Alice" in (result.text or json.dumps(result.get_data()))
    assert tracker.snapshot().max_tier is expected_tier
