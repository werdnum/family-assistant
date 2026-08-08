"""End-to-end tests for self-delegation policy behaviour.

Verifies that a profile can delegate to itself without requiring user
confirmation, that cross-profile delegation still respects its policy
rules, and that an operator-layer deny can override the self-delegation
allow.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import KnownUsersContextProvider
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.types import DelegationSecurityLevel
from family_assistant.storage.database import Database
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS as local_tool_registrations,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ConfirmationOutcome
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    last_real_message,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

PROFILE_ID = "self_delegate_profile"
OTHER_PROFILE_ID = "other_target_profile"
DELEGATED_TASK = "Process this request for me."
TEST_CHAT_ID = "999888"
TEST_INTERFACE_TYPE = "test_interface"
TEST_USER_NAME = "SelfDelegationTester"


def _make_llm_mock(
    target_service_id: str,
    task_description: str,
) -> RuleBasedMockLLMClient:
    """Build a mock LLM that issues a delegate_to_service tool call then relays the result."""

    def _tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last = messages[-1]
        return last.role == "tool" and bool(last.content)

    def _tool_result_response(kwargs: MatcherArgs) -> MockLLMOutput:
        content = kwargs.get("messages", [])[-1].content or ""
        return MockLLMOutput(content=content, tool_calls=None)

    def _user_query_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last = last_real_message(messages)
        return last is not None and last.role == "user"

    def _user_query_response(kwargs: MatcherArgs) -> MockLLMOutput:
        # ast-grep-ignore: no-dict-any - tool call arguments match external LLM API format
        call_args: dict[str, Any] = {
            "target_service_id": target_service_id,
            "user_request": task_description,
        }
        return MockLLMOutput(
            content=f"Delegating to {target_service_id}.",
            tool_calls=[
                ToolCallItem(
                    id=f"call_{uuid.uuid4()}",
                    type="function",
                    function=ToolCallFunction(
                        name="delegate_to_service",
                        arguments=json.dumps(call_args),
                    ),
                )
            ],
        )

    return RuleBasedMockLLMClient(
        rules=[
            (_tool_result_matcher, _tool_result_response),
            (_user_query_matcher, _user_query_response),
        ]
    )


def _make_target_llm_mock() -> RuleBasedMockLLMClient:
    """Build a mock LLM for the delegation target that echoes the request."""
    return RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(
            content=f"Handled by target: {DELEGATED_TASK}",
        ),
    )


def _build_tools_provider(
    *,
    profile_tools_policy: ToolPolicyConfig,
    self_delegation_profile_id: str,
    operator_tools_policy: ToolPolicyConfig | None = None,
) -> PolicyEnforcingToolsProvider:
    """Build a PolicyEnforcingToolsProvider the same way production does.

    Uses PolicyEngine.from_layers with a synthetic self-delegation allow
    rule at the profile layer, mirroring _build_profile_policy_engine.
    """
    registrations = [
        r for r in local_tool_registrations if r.name == "delegate_to_service"
    ]
    local_provider = LocalToolsProvider(registrations=registrations)
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite = CompositeToolsProvider(providers=[local_provider, mcp_provider])

    self_delegation_policy = ToolPolicyConfig(
        rules=[
            PolicyRule(
                match=ToolMatcher(
                    names=["delegate_to_service"],
                    argument_equals={"target_service_id": self_delegation_profile_id},
                ),
                decision=ToolPolicyDecision.ALLOW,
                priority=50,
                description=f"Allow self-delegation for profile '{self_delegation_profile_id}'",
            ),
        ],
    )

    engine = PolicyEngine.from_layers(
        defaults=profile_tools_policy,
        profile=self_delegation_policy,
        operator=operator_tools_policy,
    )
    return PolicyEnforcingToolsProvider(
        wrapped_provider=composite, policy_engine=engine
    )


def _make_service_config(profile_id: str) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={"system_prompt": "You are a test assistant."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id=profile_id,
    )


@pytest_asyncio.fixture
async def mock_confirmation_callback() -> AsyncMock:
    callback = AsyncMock(spec=Callable[..., Awaitable[ConfirmationOutcome]])
    callback.return_value = ConfirmationOutcome(kind="approved")
    return callback


@pytest.mark.asyncio
async def test_self_delegation_succeeds_without_confirmation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    mock_confirmation_callback: AsyncMock,
) -> None:
    """When a profile delegates to itself, no confirmation is required."""
    config = _make_service_config(PROFILE_ID)
    llm = _make_llm_mock(target_service_id=PROFILE_ID, task_description=DELEGATED_TASK)

    # Build provider with a base policy that would normally require confirm
    # for delegation to PROFILE_ID, but the synthetic self-delegation rule
    # at the profile layer overrides it.
    base_policy = ToolPolicyConfig(
        default_decision=ToolPolicyDecision.DENY,
        rules=[
            PolicyRule(
                match=ToolMatcher(names=["delegate_to_service"]),
                decision=ToolPolicyDecision.ALLOW,
                priority=10,
            ),
            PolicyRule(
                match=ToolMatcher(
                    names=["delegate_to_service"],
                    argument_equals={"target_service_id": PROFILE_ID},
                ),
                decision=ToolPolicyDecision.CONFIRM,
                priority=99,
                description="Confirm self-delegation (should be overridden by synthetic rule)",
            ),
        ],
    )
    tools_provider = _build_tools_provider(
        profile_tools_policy=base_policy,
        self_delegation_profile_id=PROFILE_ID,
    )
    await tools_provider.get_tool_definitions()

    # The target service uses the same profile (self-delegation), with its own
    # LLM mock that returns a response.
    target_llm = _make_target_llm_mock()
    target_config = _make_service_config(PROFILE_ID)
    target_tools_provider = _build_tools_provider(
        profile_tools_policy=base_policy,
        self_delegation_profile_id=PROFILE_ID,
    )
    await target_tools_provider.get_tool_definitions()
    target_service = ProcessingService(
        llm_client=target_llm,
        tools_provider=target_tools_provider,
        service_config=target_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )

    primary_service = ProcessingService(
        llm_client=llm,
        tools_provider=tools_provider,
        service_config=config,
        context_providers=[
            KnownUsersContextProvider(chat_id_to_name_map={}, prompts=config.prompts)
        ],
        server_url="http://test.server",
        app_config=AppConfig(),
    )

    registry = {PROFILE_ID: target_service}
    primary_service.processing_services_registry = registry
    target_service.processing_services_registry = registry
    task_worker_manager(
        primary_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )

    db_context = Database(engine=db_engine)
    result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CHAT_ID,
        trigger_content_parts=[{"type": "text", "text": "Please delegate this."}],
        trigger_interface_message_id="msg_self",
        user_name=TEST_USER_NAME,
        chat_interface=MagicMock(spec=ChatInterface),
        request_confirmation_callback=mock_confirmation_callback,
    )

    assert result.error_traceback is None, f"Error: {result.error_traceback}"
    assert result.text_reply is not None
    assert "Handled by target" in result.text_reply
    mock_confirmation_callback.assert_not_called()


@pytest.mark.asyncio
async def test_cross_profile_delegation_still_requires_confirmation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    mock_confirmation_callback: AsyncMock,
) -> None:
    """Delegating to a different profile with a confirm rule still triggers confirmation."""
    config = _make_service_config(PROFILE_ID)
    llm = _make_llm_mock(
        target_service_id=OTHER_PROFILE_ID, task_description=DELEGATED_TASK
    )

    base_policy = ToolPolicyConfig(
        default_decision=ToolPolicyDecision.DENY,
        rules=[
            PolicyRule(
                match=ToolMatcher(names=["delegate_to_service"]),
                decision=ToolPolicyDecision.ALLOW,
                priority=10,
            ),
            PolicyRule(
                match=ToolMatcher(
                    names=["delegate_to_service"],
                    argument_equals={"target_service_id": OTHER_PROFILE_ID},
                ),
                decision=ToolPolicyDecision.CONFIRM,
                priority=99,
                description="Confirm delegation to other profile",
            ),
        ],
    )
    tools_provider = _build_tools_provider(
        profile_tools_policy=base_policy,
        self_delegation_profile_id=PROFILE_ID,
    )
    await tools_provider.get_tool_definitions()

    target_llm = _make_target_llm_mock()
    target_config = _make_service_config(OTHER_PROFILE_ID)
    target_tools_provider = _build_tools_provider(
        profile_tools_policy=ToolPolicyConfig(default_decision=ToolPolicyDecision.DENY),
        self_delegation_profile_id=OTHER_PROFILE_ID,
    )
    await target_tools_provider.get_tool_definitions()
    target_service = ProcessingService(
        llm_client=target_llm,
        tools_provider=target_tools_provider,
        service_config=target_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )

    primary_service = ProcessingService(
        llm_client=llm,
        tools_provider=tools_provider,
        service_config=config,
        context_providers=[
            KnownUsersContextProvider(chat_id_to_name_map={}, prompts=config.prompts)
        ],
        server_url="http://test.server",
        app_config=AppConfig(),
    )

    registry = {PROFILE_ID: primary_service, OTHER_PROFILE_ID: target_service}
    primary_service.processing_services_registry = registry
    target_service.processing_services_registry = registry
    task_worker_manager(
        primary_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )

    db_context = Database(engine=db_engine)
    result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CHAT_ID,
        trigger_content_parts=[{"type": "text", "text": "Please delegate this."}],
        trigger_interface_message_id="msg_cross",
        user_name=TEST_USER_NAME,
        chat_interface=MagicMock(spec=ChatInterface),
        request_confirmation_callback=mock_confirmation_callback,
    )

    assert result.error_traceback is None, f"Error: {result.error_traceback}"
    assert result.text_reply is not None
    assert "Handled by target" in result.text_reply
    mock_confirmation_callback.assert_called_once()
    call_kwargs = mock_confirmation_callback.call_args.kwargs
    assert call_kwargs["tool_name"] == "delegate_to_service"
    confirmed_args = call_kwargs["tool_args"]
    assert confirmed_args["target_service_id"] == OTHER_PROFILE_ID


@pytest.mark.asyncio
async def test_operator_deny_overrides_self_delegation(
    db_engine: AsyncEngine,
    mock_confirmation_callback: AsyncMock,
) -> None:
    """An operator-layer deny rule blocks self-delegation even though
    the synthetic profile-layer rule allows it."""
    config = _make_service_config(PROFILE_ID)
    llm = _make_llm_mock(target_service_id=PROFILE_ID, task_description=DELEGATED_TASK)

    base_policy = ToolPolicyConfig(
        default_decision=ToolPolicyDecision.DENY,
        rules=[
            PolicyRule(
                match=ToolMatcher(names=["delegate_to_service"]),
                decision=ToolPolicyDecision.ALLOW,
                priority=10,
            ),
        ],
    )
    operator_policy = ToolPolicyConfig(
        rules=[
            PolicyRule(
                match=ToolMatcher(
                    names=["delegate_to_service"],
                    argument_equals={"target_service_id": PROFILE_ID},
                ),
                decision=ToolPolicyDecision.DENY,
                priority=0,
                description="Operator blocks self-delegation",
            ),
        ],
    )
    tools_provider = _build_tools_provider(
        profile_tools_policy=base_policy,
        self_delegation_profile_id=PROFILE_ID,
        operator_tools_policy=operator_policy,
    )
    await tools_provider.get_tool_definitions()

    primary_service = ProcessingService(
        llm_client=llm,
        tools_provider=tools_provider,
        service_config=config,
        context_providers=[
            KnownUsersContextProvider(chat_id_to_name_map={}, prompts=config.prompts)
        ],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    primary_service.processing_services_registry = {PROFILE_ID: primary_service}

    db_context = Database(engine=db_engine)
    result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CHAT_ID,
        trigger_content_parts=[{"type": "text", "text": "Please delegate this."}],
        trigger_interface_message_id="msg_blocked",
        user_name=TEST_USER_NAME,
        chat_interface=MagicMock(spec=ChatInterface),
        request_confirmation_callback=mock_confirmation_callback,
    )

    assert result.error_traceback is None, f"Error: {result.error_traceback}"
    assert result.text_reply is not None
    assert "not allowed" in result.text_reply.lower()
    mock_confirmation_callback.assert_not_called()
