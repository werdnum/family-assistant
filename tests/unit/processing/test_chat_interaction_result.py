from __future__ import annotations

from typing import Any

import pytest

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
    assert result.has_error is False


@pytest.mark.no_db
def test_error_factory_sets_enum_status() -> None:
    result = ChatInteractionResult.error(
        text_reply="error",
        error_traceback="traceback",
    )
    assert result.status == ChatInteractionStatus.ERROR
    assert result.has_error is True
