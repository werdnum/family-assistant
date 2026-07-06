"""Tests for JSON-safe message serialization."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.llm.messages import (
    AssistantMessage,
    ToolMessage,
    message_to_json_dict,
)

if TYPE_CHECKING:
    from family_assistant.security.taint import TaintMetadata


@pytest.mark.no_db
def test_message_to_json_dict_omits_taint_metadata_by_default() -> None:
    taint_metadata = cast(
        "TaintMetadata",
        {
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "sources": [],
        },
    )

    assistant_payload = message_to_json_dict(
        AssistantMessage(content="hello", taint_metadata=taint_metadata)
    )
    tool_payload = message_to_json_dict(
        ToolMessage(
            tool_call_id="call-1",
            name="lookup",
            content="external",
            taint_metadata=taint_metadata,
        )
    )

    assert "taint_metadata" not in assistant_payload
    assert "taint_metadata" not in tool_payload
    assert (
        message_to_json_dict(
            AssistantMessage(content="hello", taint_metadata=taint_metadata),
            include_taint_metadata=True,
        )["taint_metadata"]
        == taint_metadata
    )
