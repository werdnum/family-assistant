from __future__ import annotations

from family_assistant.web.routers.chat_api import (
    _user_name_for_chat,  # noqa: PLC2701 - testing private helper directly
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


def test_blank_values_are_skipped() -> None:
    current_user = {
        "user_label": "   ",
        "name": "",
        "user_identifier": "andrew@example.com",
    }

    assert _user_name_for_chat(current_user) == "andrew@example.com"


def test_defaults_to_api_user_when_nothing_available() -> None:
    assert _user_name_for_chat({}) == "API User"
