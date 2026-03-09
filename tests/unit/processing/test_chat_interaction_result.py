from __future__ import annotations

from typing import Any

import pytest

from family_assistant.llm.messages import MessageReasoningInfo
from family_assistant.processing.types import (
    ChatInteractionResult,
    ChatInteractionStatus,
)


@pytest.mark.no_db
def test_status_must_be_enum_not_plain_string() -> None:
    invalid_status: Any = "success"
    with pytest.raises(ValueError, match="Invalid status"):
        ChatInteractionResult(
            status=invalid_status,
            text_reply="ok",
        )


@pytest.mark.no_db
def test_success_factory_sets_enum_status() -> None:
    result = ChatInteractionResult.success(text_reply="ok")
    assert result.status == ChatInteractionStatus.SUCCESS
    assert result.text_reply == "ok"
    assert result.has_error is False


@pytest.mark.no_db
def test_error_factory_sets_enum_status() -> None:
    result = ChatInteractionResult.error(
        text_reply="error",
        error_traceback="traceback",
    )
    assert result.status == ChatInteractionStatus.ERROR
    assert result.has_error is True


@pytest.mark.no_db
def test_success_factory_defaults_to_empty_text_reply() -> None:
    result = ChatInteractionResult.success()
    assert not result.text_reply


@pytest.mark.no_db
def test_post_init_enforces_success_invariants() -> None:
    with pytest.raises(ValueError, match="cannot include error_traceback"):
        ChatInteractionResult(
            status=ChatInteractionStatus.SUCCESS,
            text_reply="ok",
            error_traceback="traceback",
        )


@pytest.mark.no_db
def test_post_init_enforces_error_requires_error_traceback() -> None:
    with pytest.raises(ValueError, match="requires error_traceback"):
        ChatInteractionResult(
            status=ChatInteractionStatus.ERROR,
            text_reply="error",
        )


@pytest.mark.no_db
def test_post_init_enforces_error_requires_user_facing_text_reply() -> None:
    with pytest.raises(ValueError, match="requires non-empty user-facing text_reply"):
        ChatInteractionResult(
            status=ChatInteractionStatus.ERROR,
            text_reply="",
            error_traceback="traceback",
        )


@pytest.mark.no_db
def test_post_init_enforces_error_excludes_reasoning_info() -> None:
    with pytest.raises(ValueError, match="cannot include reasoning_info"):
        ChatInteractionResult(
            status=ChatInteractionStatus.ERROR,
            text_reply="error",
            error_traceback="traceback",
            reasoning_info=MessageReasoningInfo(total_tokens=1),
        )


@pytest.mark.no_db
def test_post_init_enforces_error_excludes_attachment_ids() -> None:
    with pytest.raises(ValueError, match="cannot include attachment_ids"):
        ChatInteractionResult(
            status=ChatInteractionStatus.ERROR,
            text_reply="error",
            error_traceback="traceback",
            attachment_ids=["att-1"],
        )
