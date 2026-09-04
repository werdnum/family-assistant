"""A profile's calendar has to reach its tools, not only its prompt.

Calendar tools read the calendar from the `LocalToolsProvider` they were built
with, and the root provider is built from the application-wide config. Without a
per-profile provider, a profile naming its own calendar would be shown events
from that calendar in prompt context while `calendar_search`, `calendar_add` and
`calendar_modify` read and wrote the application-wide one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import SecretStr

from family_assistant.assistant import (
    _root_provider_for_profile,  # noqa: PLC2701 - the selection under test; there is no public seam for it
)
from family_assistant.config_models import CalDAVConfig, CalendarConfig
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    CompositeToolsProvider,
    LocalToolsProvider,
)

if TYPE_CHECKING:
    from family_assistant.tools import ToolsProvider
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


def _calendar_dict(url: str) -> CalendarConfigDict:
    return cast(
        "CalendarConfigDict", _calendar_config(url).model_dump(exclude_none=True)
    )


def _shared_root() -> ToolsProvider:
    providers: list[ToolsProvider] = [
        LocalToolsProvider(
            registrations=LOCAL_TOOL_REGISTRATIONS,
            embedding_generator=None,
            calendar_config=_calendar_dict(_APP_CALENDAR),
        )
    ]
    return CompositeToolsProvider(providers=providers)


def _calendar_urls(provider: ToolsProvider) -> list[str]:
    assert isinstance(provider, CompositeToolsProvider)
    local = next(
        p for p in provider.get_providers() if isinstance(p, LocalToolsProvider)
    )
    config = local.get_calendar_config()
    assert config is not None
    caldav = config.get("caldav")
    assert caldav is not None
    urls = caldav.get("calendar_urls")
    assert urls is not None
    return list(urls)


def test_a_profile_with_its_own_calendar_gets_its_own_local_provider() -> None:
    mcp_provider = CompositeToolsProvider(providers=[])
    shared_root = _shared_root()

    provider = _root_provider_for_profile(
        shared_root=shared_root,
        profile_calendar_config=_calendar_config(_PROFILE_CALENDAR),
        local_registrations=LOCAL_TOOL_REGISTRATIONS,
        mcp_provider=mcp_provider,
        embedding_generator=None,
    )

    assert provider is not shared_root
    assert _calendar_urls(provider) == [_PROFILE_CALENDAR]
    # The MCP provider is shared: nothing in it is calendar-scoped, and
    # reconnecting a second copy of every configured server would be wasteful.
    assert isinstance(provider, CompositeToolsProvider)
    assert mcp_provider in provider.get_providers()


def test_a_profile_without_its_own_calendar_shares_the_root_provider() -> None:
    shared_root = _shared_root()

    provider = _root_provider_for_profile(
        shared_root=shared_root,
        profile_calendar_config=None,
        local_registrations=LOCAL_TOOL_REGISTRATIONS,
        mcp_provider=CompositeToolsProvider(providers=[]),
        embedding_generator=None,
    )

    assert provider is shared_root
    assert _calendar_urls(provider) == [_APP_CALENDAR]
