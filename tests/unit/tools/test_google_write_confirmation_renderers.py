"""Unit tests for scoped Google-write confirmation prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.tools.confirmation import (
    CONFIRMATION_VALUE_MAX_CHARS,
    TOOL_CONFIRMATION_RENDERERS,
    confirmation_payload_block_reason,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolArgumentsView, ToolExecutionContext


def _no_context() -> ToolExecutionContext:
    return cast("ToolExecutionContext", None)


@pytest.mark.asyncio
async def test_gmail_draft_confirmation_shows_recipients_content_and_attachments() -> (
    None
):
    prompt = await TOOL_CONFIRMATION_RENDERERS["gmail_create_draft"](
        {
            "to": ["to@example.com"],
            "cc": ["cc@example.com"],
            "bcc": ["bcc@example.com"],
            "subject": "Trip details",
            "body": "Here is the itinerary.",
            "attachment_ids": ["attachment-1"],
        },
        _no_context(),
    )

    assert "unsent Gmail draft" in prompt
    for expected in (
        "to@example.com",
        "cc@example.com",
        "bcc@example.com",
        "Trip details",
        "Here is the itinerary.",
        "attachment-1",
    ):
        assert expected in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {
                "name": "Plan",
                "content": "Family plan",
                "file_type": "google_doc",
                "overwrite": True,
            },
            ("Plan", "Family plan", "google_doc", "True"),
        ),
        (
            {"attachment_id": "attachment-2"},
            ("Use the attachment filename", "attachment-2", "False"),
        ),
    ],
)
async def test_drive_write_confirmation_shows_destination_and_source(
    arguments: ToolArgumentsView,
    expected: tuple[str, ...],
) -> None:
    prompt = await TOOL_CONFIRMATION_RENDERERS["drive_write_file"](
        arguments, _no_context()
    )

    assert "dedicated Google Drive folder" in prompt
    for value in expected:
        assert value in prompt
    if arguments.get("attachment_id"):
        assert "File type" not in prompt
        assert "google_doc" not in prompt


@pytest.mark.parametrize(
    ("tool_name", "arguments", "field_name"),
    [
        (
            "gmail_create_draft",
            {"body": "x" * (CONFIRMATION_VALUE_MAX_CHARS + 1)},
            "body",
        ),
        (
            "gmail_create_draft",
            {"subject": "x" * (CONFIRMATION_VALUE_MAX_CHARS + 1)},
            "subject",
        ),
        (
            "drive_write_file",
            {"content": "x" * (CONFIRMATION_VALUE_MAX_CHARS + 1)},
            "content",
        ),
    ],
)
def test_google_write_confirmation_blocks_hidden_payload_tails(
    tool_name: str,
    arguments: dict[str, object],
    field_name: str,
) -> None:
    reason = confirmation_payload_block_reason(tool_name, arguments)

    assert reason is not None
    assert field_name in reason
    assert str(CONFIRMATION_VALUE_MAX_CHARS) in reason


def test_drive_attachment_confirmation_does_not_apply_authored_content_limit() -> None:
    assert (
        confirmation_payload_block_reason(
            "drive_write_file",
            {
                "attachment_id": "attachment-2",
                "content": "x" * (CONFIRMATION_VALUE_MAX_CHARS + 1),
            },
        )
        is None
    )
