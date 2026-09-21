"""Integration tests for the call_home_assistant_action tool with real HA."""

from zoneinfo import ZoneInfo

import homeassistant_api
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
from family_assistant.storage.database import Database
from family_assistant.tools.home_assistant import (
    call_home_assistant_action_tool,
    list_home_assistant_actions_tool,
    render_home_assistant_template_tool,
)
from family_assistant.tools.types import ToolExecutionContext


def _make_exec_context(
    wrapper: HomeAssistantClientWrapper, db_context: Database
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=wrapper,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.integration
@pytest.mark.vcr
async def test_call_action_turns_on_input_boolean(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """Calling input_boolean.turn_on against a real HA flips the switch to 'on'."""
    base_url, token = home_assistant_service

    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    db_context = Database(engine=db_engine)
    exec_context = _make_exec_context(wrapper, db_context)

    try:
        # Ensure switch starts off (fixture default is "off")
        off_result = await call_home_assistant_action_tool(
            exec_context,
            domain="input_boolean",
            action="turn_off",
            service_data={"entity_id": "input_boolean.test_switch"},
        )
        assert off_result.text is not None
        assert "input_boolean.turn_off" in off_result.text

        # Flip it on via the tool
        on_result = await call_home_assistant_action_tool(
            exec_context,
            domain="input_boolean",
            action="turn_on",
            service_data={"entity_id": "input_boolean.test_switch"},
        )
        assert on_result.text is not None
        assert "input_boolean.turn_on" in on_result.text

        data = on_result.get_data()
        assert isinstance(data, dict)
        assert data["domain"] == "input_boolean"
        assert data["action"] == "turn_on"
        # HA returns the changed state for the switch
        states = {s["entity_id"]: s["state"] for s in data["changed_states"]}
        assert states.get("input_boolean.test_switch") == "on"

        # Confirm via a template render
        template_result = await render_home_assistant_template_tool(
            exec_context,
            template="{{ states('input_boolean.test_switch') }}",
        )
        assert template_result == "on"

    finally:
        # Best-effort: leave the entity in its original off state
        await call_home_assistant_action_tool(
            exec_context,
            domain="input_boolean",
            action="turn_off",
            service_data={"entity_id": "input_boolean.test_switch"},
        )
        await ha_lib_client.async_cache_session.close()


@pytest.mark.integration
@pytest.mark.vcr
async def test_list_actions_returns_live_catalog(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """The discovery tool returns the actions HA actually exposes.

    This proves the discovery path is wired to ``GET /api/services`` against a
    real HA, so the LLM can rely on it to find action names that match the
    user's installation rather than guessing from training data.
    """
    base_url, token = home_assistant_service

    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    db_context = Database(engine=db_engine)
    exec_context = _make_exec_context(wrapper, db_context)
    try:
        result = await list_home_assistant_actions_tool(exec_context)
        data = result.get_data()
        assert isinstance(data, dict)
        actions = data["actions"]

        # Catalog includes the input_boolean domain that the test fixture
        # configures, and entries carry the field schemas HA returns.
        keys = {(entry["domain"], entry["action"]) for entry in actions}
        assert ("input_boolean", "turn_on") in keys
        assert ("input_boolean", "turn_off") in keys

        input_boolean_entries = [
            entry for entry in actions if entry["domain"] == "input_boolean"
        ]
        assert input_boolean_entries
        for entry in input_boolean_entries:
            assert "fields" in entry
            assert "supports_response" in entry
    finally:
        await ha_lib_client.async_cache_session.close()


@pytest.mark.integration
@pytest.mark.vcr
async def test_call_action_unknown_action_returns_error(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """Calling a non-existent action returns a descriptive error."""
    base_url, token = home_assistant_service

    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    db_context = Database(engine=db_engine)
    exec_context = _make_exec_context(wrapper, db_context)

    try:
        result = await call_home_assistant_action_tool(
            exec_context,
            domain="input_boolean",
            action="bogus_action_does_not_exist",
            service_data={"entity_id": "input_boolean.test_switch"},
        )
        assert result.text is not None
        assert "Error" in result.text
        assert "input_boolean.bogus_action_does_not_exist" in result.text
    finally:
        await ha_lib_client.async_cache_session.close()
