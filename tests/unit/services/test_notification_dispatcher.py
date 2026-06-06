"""Unit tests for NotificationDispatcher fan-out behavior."""

from typing import Any

import pytest

from family_assistant.services.notification_dispatcher import NotificationDispatcher


class _FakeChannel:
    """Minimal stand-in matching the notification service interface."""

    def __init__(self, *, enabled: bool, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Any,  # noqa: ANN401
    ) -> None:
        self.calls.append((user_identifier, title, body))
        if self.fail:
            raise RuntimeError("channel down")


@pytest.mark.asyncio
async def test_enabled_reflects_any_channel() -> None:
    """The dispatcher is enabled when any underlying channel is enabled."""
    assert NotificationDispatcher().enabled is False
    assert (
        NotificationDispatcher(web_push=_FakeChannel(enabled=False)).enabled is False  # type: ignore[arg-type]
    )
    assert (
        NotificationDispatcher(apns=_FakeChannel(enabled=True)).enabled is True  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_fans_out_to_all_enabled_channels() -> None:
    """A notification reaches every enabled channel."""
    web = _FakeChannel(enabled=True)
    apns = _FakeChannel(enabled=True)
    dispatcher = NotificationDispatcher(web_push=web, apns=apns)  # type: ignore[arg-type]

    await dispatcher.send_notification("user-1", "Title", "Body", db_context=None)  # type: ignore[arg-type]

    assert web.calls == [("user-1", "Title", "Body")]
    assert apns.calls == [("user-1", "Title", "Body")]


@pytest.mark.asyncio
async def test_skips_disabled_channels() -> None:
    """Disabled channels are not invoked."""
    web = _FakeChannel(enabled=False)
    apns = _FakeChannel(enabled=True)
    dispatcher = NotificationDispatcher(web_push=web, apns=apns)  # type: ignore[arg-type]

    await dispatcher.send_notification("user-1", "Title", "Body", db_context=None)  # type: ignore[arg-type]

    assert web.calls == []
    assert apns.calls == [("user-1", "Title", "Body")]


@pytest.mark.asyncio
async def test_one_channel_failure_does_not_block_others() -> None:
    """A failing channel is isolated and does not prevent other deliveries."""
    web = _FakeChannel(enabled=True, fail=True)
    apns = _FakeChannel(enabled=True)
    dispatcher = NotificationDispatcher(web_push=web, apns=apns)  # type: ignore[arg-type]

    # Should not raise despite the web channel failing.
    await dispatcher.send_notification("user-1", "Title", "Body", db_context=None)  # type: ignore[arg-type]

    assert apns.calls == [("user-1", "Title", "Body")]
