"""Unit tests for scoped Google-write confirmation prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.tools.confirmation import TOOL_CONFIRMATION_RENDERERS

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
