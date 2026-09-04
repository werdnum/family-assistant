"""Functional tests for judge-gated engineer side effects (M3).

Tests that:
1. Aligned `spawn_worker` from an engineer turn executes on an `ALLOW` verdict without prompting.
2. A `CONFIRM` reviewer verdict still prompts the user for confirmation.
3. Reviewer failure degrades static `review` on `spawn_worker` to confirmation.
4. `resolve_tool_policy` reports `review` for flipped rules (spawn_worker, delegate_to_service,
   and into-engineer delegations).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import BaseModel

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - smallest helper that applies global_tools_policy injection to a profile
)
from family_assistant.config_loader import (
    load_prompts_yaml,
    resolve_all_service_profiles,
)
from family_assistant.config_models import (
    AIWorkerConfig,
    DefaultProfileSettings,
    ServiceProfile,
    ToolCallReviewConfig,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    TurnTaintState,
)
from family_assistant.services.backends.mock import MockBackend
from family_assistant.services.tool_call_review import (
    ToolCallReviewer,
    ToolCallReviewResponse,
    ToolCallReviewVerdict,
)
from family_assistant.storage.database import Database
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.engineering import resolve_tool_policy
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import ToolPolicyDecision
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.processing import ProcessingService
    from family_assistant.tools.types import ToolArguments, ToolDefinition


DEFAULTS_PATH = Path("defaults.yaml")
PROMPTS_PATH = Path("prompts.yaml")


class _MockReviewLLM:
    def __init__(
        self,
        verdict: ToolCallReviewVerdict,
        *,
        raise_timeout: bool = False,
        reason: str | None = None,
    ) -> None:
        self.verdict = verdict
        self.raise_timeout = raise_timeout
        self.reason = reason
        self.calls = 0

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        del messages, max_retries
        assert response_model is ToolCallReviewResponse
        self.calls += 1
        if self.raise_timeout:
            raise TimeoutError("Reviewer timed out")
        return cast(
            "T",
            ToolCallReviewResponse(
                verdict=self.verdict,
                reason=self.reason or f"Reviewer chose {self.verdict.value}.",
            ),
        )


class _ConfirmationRecorder:
    def __init__(self, outcome: ConfirmationOutcome | None = None) -> None:
        self.calls = 0
        self.call_args: list[ToolArguments] = []
        self.outcome = outcome or ConfirmationOutcome(kind="approved")

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        del (
            interface_type,
            conversation_id,
            turn_id,
            tool_name,
            call_id,
            timeout_seconds,
            context,
        )
        self.calls += 1
        self.call_args.append(tool_args)
        return self.outcome


def _load_resolved_profiles() -> tuple[
    DefaultProfileSettings, dict[str, ServiceProfile]
]:
    config_data = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    assert isinstance(config_data, dict)
    config_data.setdefault("default_service_profile_id", "default_assistant")
    default_profile_settings = config_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    default_prompts, service_profile_prompts = load_prompts_yaml(str(PROMPTS_PATH))
    if default_prompts:
        default_profile_settings.setdefault("processing_config", {})["prompts"] = (
            default_prompts
        )
    config_data["service_profiles"] = resolve_all_service_profiles(
        config_data, service_profile_prompts
    )
    default_settings = DefaultProfileSettings.model_validate(default_profile_settings)
    profiles = {
        profile["id"]: ServiceProfile.model_validate(profile)
        for profile in config_data["service_profiles"]
    }
    return default_settings, profiles


def _build_engineer_provider(
    engineer_profile: ServiceProfile,
    reviewer_llm: _MockReviewLLM | None,
) -> TaintTrackingToolsProvider:
    policy_engine = _build_profile_policy_engine(
        engineer_profile.id,
        engineer_profile.tools_policy,
        None,
        None,
        engineer_profile.excluded_global_tools,
    )
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=local_provider,
        policy_engine=policy_engine,
    )
    review_config = ToolCallReviewConfig(timeout_seconds=5)
    reviewer = (
        ToolCallReviewer(cast("LLMInterface", reviewer_llm), review_config)
        if reviewer_llm is not None
        else None
    )
    return TaintTrackingToolsProvider(
        wrapped_provider=policy_provider,
        tool_call_reviewer=reviewer,
        review_config=review_config,
        profile_review_guidance=engineer_profile.processing_config.review_guidance,
        include_aggregated_context=False,
    )


def _make_exec_context(
    db_engine: AsyncEngine,
    confirmation_recorder: _ConfirmationRecorder,
    mock_backend: MockBackend,
    workspace_path: Path,
    profile_id: str = "engineer",
) -> ToolExecutionContext:
    mock_service = MagicMock()
    mock_service.app_config.ai_worker_config = AIWorkerConfig(
        enabled=True,
        backend_type="mock",
        workspace_mount_path=str(workspace_path),
    )
    mock_service.ai_worker_backend = mock_backend

    db = Database(engine=db_engine)
    state = TurnTaintState.empty()
    tracker = InMemoryTurnTaintTracker(state)

    return ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-engineer-test",
        user_name="Alice",
        turn_id="turn-1",
        db_context=db,
        processing_service=cast("ProcessingService", mock_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        processing_profile_id=profile_id,
        request_confirmation_callback=confirmation_recorder,
        taint_tracker=tracker,
        taint_policy_snapshot=state,
    )


def test_engineer_reaches_sandboxed_code_execution_but_not_in_app_execution() -> None:
    """Sandboxed runners are reviewable; in-app scripts and the workspace are not."""
    _, profiles = _load_resolved_profiles()
    engineer = profiles["engineer"]
    policy_engine = _build_profile_policy_engine(
        engineer.id,
        engineer.tools_policy,
        None,
        None,
        engineer.excluded_global_tools,
    )

    sandbox_runner = ToolDescriptor(
        name="execute_shell",
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": "execute_shell",
                    "description": "Run a shell command in a sandbox.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        tags=frozenset({
            ToolTag.CODE_EXECUTION,
            ToolTag.WORKER,
            ToolTag.OUTPUT_UNTRUSTED,
        }),
        origin="mcp",
        mcp_server_id="code-execution",
    )

    assert policy_engine.evaluate(sandbox_runner).decision is ToolPolicyDecision.REVIEW
    local_by_name = {
        registration.name: registration for registration in LOCAL_TOOL_REGISTRATIONS
    }
    for in_app_tool in ("execute_script", "workspace_write"):
        descriptor = ToolDescriptor(
            name=in_app_tool,
            definition=local_by_name[in_app_tool].definition,
            tags=local_by_name[in_app_tool].metadata.tags,
            origin="local",
        )
        assert policy_engine.evaluate(descriptor).decision is ToolPolicyDecision.DENY


@pytest.mark.asyncio
async def test_engineer_spawn_worker_executes_on_allow_verdict_without_prompt(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """An aligned spawn_worker call from the engineer executes on reviewer ALLOW without user confirmation."""
    _, profiles = _load_resolved_profiles()
    engineer = profiles["engineer"]

    review_llm = _MockReviewLLM(ToolCallReviewVerdict.ALLOW)
    provider = _build_engineer_provider(engineer, review_llm)
    confirmation = _ConfirmationRecorder()
    mock_backend = MockBackend()
    context = _make_exec_context(db_engine, confirmation, mock_backend, tmp_path)

    result = await provider.execute_tool(
        "spawn_worker",
        {
            "agent": "claude",
            "task_description": "Investigate bug in authentication handler and produce a fix.",
        },
        context,
    )

    assert isinstance(result, ToolResult)
    assert confirmation.calls == 0
    assert review_llm.calls == 1
    data = result.get_data()
    assert isinstance(data, dict)
    assert "task_id" in data
    assert data.get("status") == "submitted"


@pytest.mark.asyncio
async def test_engineer_spawn_worker_prompts_on_confirm_verdict(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A spawn_worker call where the reviewer returns CONFIRM prompts the user for confirmation."""
    _, profiles = _load_resolved_profiles()
    engineer = profiles["engineer"]

    review_llm = _MockReviewLLM(ToolCallReviewVerdict.CONFIRM)
    provider = _build_engineer_provider(engineer, review_llm)
    confirmation = _ConfirmationRecorder(outcome=ConfirmationOutcome(kind="approved"))
    mock_backend = MockBackend()
    context = _make_exec_context(db_engine, confirmation, mock_backend, tmp_path)

    result = await provider.execute_tool(
        "spawn_worker",
        {
            "agent": "claude",
            "task_description": "Suspicious task description.",
        },
        context,
    )

    assert isinstance(result, ToolResult)
    assert review_llm.calls == 1
    assert confirmation.calls == 1
    data = result.get_data()
    assert isinstance(data, dict)
    assert "task_id" in data
    assert data.get("status") == "submitted"


@pytest.mark.asyncio
async def test_engineer_spawn_worker_falls_back_to_confirm_on_reviewer_error(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Reviewer timeout or failure degrades static review on spawn_worker to user confirmation."""
    _, profiles = _load_resolved_profiles()
    engineer = profiles["engineer"]

    review_llm = _MockReviewLLM(ToolCallReviewVerdict.ALLOW, raise_timeout=True)
    provider = _build_engineer_provider(engineer, review_llm)
    confirmation = _ConfirmationRecorder(outcome=ConfirmationOutcome(kind="approved"))
    mock_backend = MockBackend()
    context = _make_exec_context(db_engine, confirmation, mock_backend, tmp_path)

    result = await provider.execute_tool(
        "spawn_worker",
        {
            "agent": "claude",
            "task_description": "Fix bug in repo.",
        },
        context,
    )

    assert isinstance(result, ToolResult)
    assert review_llm.calls == 1
    assert confirmation.calls == 1
    data = result.get_data()
    assert isinstance(data, dict)
    assert "task_id" in data
    assert data.get("status") == "submitted"


@pytest.mark.asyncio
async def test_resolve_tool_policy_reports_review_for_flipped_rules(
    db_engine: AsyncEngine,
) -> None:
    """resolve_tool_policy reports 'review' for spawn_worker and delegate_to_service on engineer and into-engineer."""
    _, profiles = _load_resolved_profiles()

    services_registry: dict[str, MagicMock] = {}
    for pid, profile in profiles.items():
        engine = _build_profile_policy_engine(
            profile.id,
            profile.tools_policy,
            None,
            None,
            profile.excluded_global_tools,
        )
        policy_provider = PolicyEnforcingToolsProvider(
            wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
            policy_engine=engine,
        )
        mock_svc = MagicMock()
        mock_svc.tools_provider = policy_provider
        mock_svc.on_demand_view = None
        services_registry[pid] = mock_svc

    mock_service = MagicMock()
    mock_service.processing_services_registry = services_registry
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-diag",
        user_name="Alice",
        turn_id="turn-1",
        db_context=db,
        processing_service=cast("ProcessingService", mock_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        processing_profile_id="engineer",
    )

    # 1. spawn_worker on engineer -> review
    res_spawn = await resolve_tool_policy(
        context, tool_name="spawn_worker", profile_id="engineer"
    )
    data_spawn = res_spawn.get_data()
    assert isinstance(data_spawn, dict)
    entry = data_spawn["profiles"][0]
    assert entry["raw"]["decision"] == "review"

    # 2. delegate_to_service on engineer -> review
    res_del_eng = await resolve_tool_policy(
        context,
        tool_name="delegate_to_service",
        profile_id="engineer",
        arguments={"target_service_id": "default_assistant"},
    )
    data_del_eng = res_del_eng.get_data()
    assert isinstance(data_del_eng, dict)
    entry = data_del_eng["profiles"][0]
    assert entry["raw"]["decision"] == "review"

    # 3. reconnect_mcp_server and cancel_worker_task on engineer -> allow
    for allowed_tool in ("reconnect_mcp_server", "cancel_worker_task"):
        res_allowed = await resolve_tool_policy(
            context, tool_name=allowed_tool, profile_id="engineer"
        )
        data_allowed = res_allowed.get_data()
        assert isinstance(data_allowed, dict)
        entry = data_allowed["profiles"][0]
        assert entry["raw"]["decision"] == "allow", f"Failed for {allowed_tool}"

    # 4. create_github_issue on engineer -> confirm
    res_conf = await resolve_tool_policy(
        context, tool_name="create_github_issue", profile_id="engineer"
    )
    data_conf = res_conf.get_data()
    assert isinstance(data_conf, dict)
    entry = data_conf["profiles"][0]
    assert entry["raw"]["decision"] == "confirm"

    # 5. Into-engineer delegation on default_assistant, browser_profile, telephone, complex_tasks -> review
    for delegating_pid in (
        "default_assistant",
        "browser_profile",
        "telephone",
        "complex_tasks",
    ):
        res_into = await resolve_tool_policy(
            context,
            tool_name="delegate_to_service",
            profile_id=delegating_pid,
            arguments={"target_service_id": "engineer"},
        )
        data_into = res_into.get_data()
        assert isinstance(data_into, dict)
        entry = data_into["profiles"][0]
        assert entry["raw"]["decision"] == "review", f"Failed for {delegating_pid}"
