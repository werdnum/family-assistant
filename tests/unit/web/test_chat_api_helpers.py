from __future__ import annotations

from datetime import UTC, datetime

from family_assistant.storage.types import MessageHistoryRow
from family_assistant.web.routers.chat_api import (
    _user_name_for_chat,  # noqa: PLC2701 - testing private helper directly
    serialize_conversation_messages,
)


def test_prefers_configured_user_label() -> None:
    current_user = {
        "user_label": "Andrew",
        "name": "Andrew Garrett",
        "user_identifier": "andrew@example.com",
    }

    assert _user_name_for_chat(current_user) == "Andrew"


def test_falls_back_to_oidc_name_when_label_missing() -> None:
    current_user = {
        "name": "Andrew Garrett",
        "user_identifier": "andrew@example.com",
    }

    assert _user_name_for_chat(current_user) == "Andrew Garrett"


def test_falls_back_to_user_identifier_when_name_missing() -> None:
    current_user = {"user_identifier": "andrew@example.com"}

    assert _user_name_for_chat(current_user) == "andrew@example.com"


def test_token_auth_skips_stale_name_claim() -> None:
    # For API tokens the "name" claim is a copy of the (possibly legacy) token
    # owner; resolution has rewritten user_identifier to the canonical id, so
    # the canonical id should win over the stale name.
    current_user = {
        "name": "keycloak-subject-123",
        "user_identifier": "andrew@example.com",
        "identity_source": "api_token",
    }

    assert _user_name_for_chat(current_user) == "andrew@example.com"


def test_token_auth_still_prefers_configured_label() -> None:
    current_user = {
        "user_label": "Andrew",
        "name": "keycloak-subject-123",
        "user_identifier": "andrew@example.com",
        "source": "app_token_session",
    }

    assert _user_name_for_chat(current_user) == "Andrew"


def test_blank_values_are_skipped() -> None:
    current_user = {
        "user_label": "   ",
        "name": "",
        "user_identifier": "andrew@example.com",
    }

    assert _user_name_for_chat(current_user) == "andrew@example.com"


def test_defaults_to_api_user_when_nothing_available() -> None:
    assert _user_name_for_chat({}) == "API User"


def _history_row(timestamp: datetime) -> MessageHistoryRow:
    return MessageHistoryRow(
        internal_id=1,
        interface_type="web",
        conversation_id="web_conv",
        interface_message_id=None,
        turn_id="turn-1",
        thread_root_id=None,
        timestamp=timestamp,
        role="assistant",
        content="Hello",
        tool_calls=None,
        reasoning_info=None,
        tool_call_id=None,
        error_traceback=None,
        processing_profile_id=None,
        subconversation_id=None,
        user_id=None,
        attachments=None,
        tool_name=None,
        provider_metadata=None,
        taint_metadata_json=None,
        taint_metadata_version=None,
        taint_metadata=None,
        is_internal=False,
        echoed_from_conversation_id=None,
        moved_to_conversation_id=None,
        started_thread_conversation_id=None,
    )


def test_serialized_message_timestamp_carries_utc_offset() -> None:
    # SQLite returns timezone columns naive; the browser would read a naive
    # ISO string in its own local zone.
    naive = datetime(2026, 11, 10, 5, 0, 0)

    messages = serialize_conversation_messages([_history_row(naive)])

    assert len(messages) == 1
    message = messages[0]

    assert message.timestamp == naive.replace(tzinfo=UTC)
    assert message.model_dump(mode="json")["timestamp"].endswith("Z")
