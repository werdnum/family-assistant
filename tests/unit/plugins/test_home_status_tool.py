"""The get_home_status tool: the operator's Home Assistant overview on demand."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant_api.errors import HomeassistantAPIError
from pydantic import SecretStr

from family_assistant.plugins.config import PluginsConfig
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.plugin import HOME_ASSISTANT_PLUGIN
from family_assistant.plugins.home_assistant.tools import get_home_status_tool
from family_assistant.plugins.runtime import ProfilePlugins, withheld_profile_tools
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from family_assistant.plugins.home_assistant.client import (
        HomeAssistantClientWrapper,
    )

pytestmark = pytest.mark.no_db

TEMPLATE = "{{ states('person.alice') }} / {{ states('sensor.price') }}"


class FakeTemplateRenderer:
    """Renders templates from a fixed table, as Home Assistant would."""

    def __init__(
        self,
        rendered: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._rendered = rendered or {}
        self._error = error
        self.templates: list[str] = []

    async def async_get_rendered_template(self, template: str) -> str:
        self.templates.append(template)
        if self._error is not None:
            raise self._error
        return self._rendered[template]


def _exec_context(plugins: ProfilePlugins | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="home-status-test",
        user_name="Tester",
        turn_id=None,
        db_context=MagicMock(spec=Database),
        processing_service=None,
        clock=None,
        plugins=plugins,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )


def _with_instance(
    renderer: FakeTemplateRenderer, context_template: str | None = TEMPLATE
) -> ToolExecutionContext:
    instance = HomeAssistantInstance(
        cast("HomeAssistantClientWrapper", renderer),
        context_template=context_template,
    )
    return _exec_context(ProfilePlugins((instance,)))


def _ha(context_template: str | None = None) -> HomeAssistantConfig:
    return HomeAssistantConfig(
        api_url="http://ha.local:8123",
        token=SecretStr("token"),
        context_template=context_template,
    )


def _served_names(configs: dict[str, HomeAssistantConfig]) -> set[str]:
    return {r.name for r in HOME_ASSISTANT_PLUGIN.served_tools(configs)}


@pytest.mark.asyncio
async def test_renders_the_configured_template() -> None:
    renderer = FakeTemplateRenderer({TEMPLATE: "\n  Alice: home / Price: 21c\n\n"})

    result = await get_home_status_tool(_with_instance(renderer))

    assert result.get_text() == "Alice: home / Price: 21c"
    assert renderer.templates == [TEMPLATE]


@pytest.mark.asyncio
async def test_api_error_is_reported() -> None:
    renderer = FakeTemplateRenderer(error=HomeassistantAPIError("boom"))

    result = await get_home_status_tool(_with_instance(renderer))

    assert result.get_text() == "Error: Home Assistant API error - boom"


@pytest.mark.asyncio
async def test_empty_render_says_so() -> None:
    renderer = FakeTemplateRenderer({TEMPLATE: "  \n"})

    result = await get_home_status_tool(_with_instance(renderer))

    assert result.get_text() == "The home status overview rendered empty."


@pytest.mark.asyncio
async def test_instance_without_a_template_is_an_error() -> None:
    renderer = FakeTemplateRenderer()

    result = await get_home_status_tool(_with_instance(renderer, context_template=None))

    assert result.get_text().startswith("Error: No home status overview")
    assert renderer.templates == []


@pytest.mark.asyncio
async def test_profile_without_home_assistant_is_an_error() -> None:
    result = await get_home_status_tool(_exec_context(ProfilePlugins(())))

    assert result.get_text() == (
        "Error: Home Assistant integration is not configured or available."
    )


def test_served_only_when_an_instance_has_a_template() -> None:
    assert "get_home_status" in _served_names({
        "default": _ha(),
        "upstairs": _ha(context_template=TEMPLATE),
    })


def test_withheld_when_no_instance_has_a_template() -> None:
    served = _served_names({"default": _ha()})

    assert "get_home_status" not in served
    assert "render_home_assistant_template" in served


def test_withheld_without_home_assistant() -> None:
    assert "get_home_status" not in _served_names({})


def test_withheld_from_a_profile_whose_instance_has_no_template() -> None:
    config = PluginsConfig(
        home_assistant={
            "default": _ha(),
            "upstairs": _ha(context_template=TEMPLATE),
        }
    )

    assert withheld_profile_tools(config, {}) == {"get_home_status"}
    assert withheld_profile_tools(config, {"home_assistant": None}) == {
        "get_home_status"
    }
    assert withheld_profile_tools(config, {"home_assistant": "upstairs"}) == set()
