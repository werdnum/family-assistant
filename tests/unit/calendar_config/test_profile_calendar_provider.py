"""A profile's calendar has to reach its tools, not only its prompt.

The same effective config feeds the profile's calendar context provider and the
tool execution contexts its processing service builds. If the two diverged, a
profile naming its own calendar would be shown events from that calendar in
prompt context while `calendar_search`, `calendar_add` and `calendar_modify`
read and wrote the application-wide one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr

from family_assistant.assistant import (
    _calendar_config_to_dict,  # noqa: PLC2701 - no public seam for the runtime dict
    _profile_calendar_config,  # noqa: PLC2701 - the selection under test; there is no public seam for it
)
from family_assistant.config_models import CalDAVConfig, CalendarConfig

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig as CalendarConfigDict

_APP_CALENDAR = "https://calendar.example/dav/household"
_PROFILE_CALENDAR = "https://calendar.example/dav/kids"


def _calendar_config(url: str) -> CalendarConfig:
    return CalendarConfig(
        caldav=CalDAVConfig(
            username="user",
            password=SecretStr("secret"),
            base_url="https://calendar.example/dav/",
            calendar_urls=[url],
        )
    )


def _calendar_urls(config: CalendarConfigDict) -> list[str]:
    caldav = config.get("caldav")
    assert caldav is not None
    urls = caldav.get("calendar_urls")
    assert urls is not None
    return [u if isinstance(u, str) else u.get("url", "") for u in urls]


def test_a_profile_with_its_own_calendar_uses_only_that_calendar() -> None:
    config = _profile_calendar_config(
        _calendar_config(_PROFILE_CALENDAR), _calendar_config(_APP_CALENDAR)
    )

    assert _calendar_urls(config) == [_PROFILE_CALENDAR]


def test_a_profile_without_its_own_calendar_uses_the_app_calendar() -> None:
    config = _profile_calendar_config(None, _calendar_config(_APP_CALENDAR))

    assert _calendar_urls(config) == [_APP_CALENDAR]


def test_calendar_runtime_dict_carries_the_real_caldav_password() -> None:
    """DAVClient is handed a plain dict, which loses the SecretStr declaration.

    A SecretStr surviving into it would be serialized as its mask, so every
    CalDAV read and write would fail authentication.
    """
    config = CalendarConfig(
        caldav=CalDAVConfig(
            username="user",
            password=SecretStr("caldav-secret"),
            base_url="https://calendar.example/dav/",
        )
    )

    caldav = _calendar_config_to_dict(config).get("caldav")
    assert caldav is not None
    assert caldav.get("password") == "caldav-secret"
