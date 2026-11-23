"""Live ``browser_profile`` trial runner with real Playwright DOM tools and spend caps.

Each trial runs one candidate LLM in an isolated headless stealth Chromium
session using the shipped ``browser_profile`` system prompt and semantic DOM
tools (``browser_open``, ``browser_snapshot``, ``browser_click``, ``browser_fill``,
``browser_select``, ``browser_extract``, ``browser_exec``, ``browser_wait``,
``browser_screenshot``, ``browser_request_handoff``, ``browser_claim_handback``).
Outbound side-effect / cross-profile boundaries (``delegate_to_service``,
``attach_to_response``, ``report_technical_problem``) are stubbed and recorded
so a single-model trial stays strictly attributable to the candidate under test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from family_assistant.eval.browser.pricing import (
    TokenTotals,
    accumulate_usage,
    estimate_call_cost_usd,
)
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageReasoningInfo,
    SystemMessage,
    UserMessage,
    tool_result_to_llm_message,
)
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database
from family_assistant.tools.browser_dom import (
    BROWSER_DOM_TOOLS_DEFINITION,
    browser_claim_handback_tool,
    browser_click_tool,
    browser_exec_tool,
    browser_extract_tool,
    browser_fill_tool,
    browser_open_tool,
    browser_request_handoff_tool,
    browser_screenshot_tool,
    browser_select_tool,
    browser_snapshot_tool,
    browser_wait_tool,
)
from family_assistant.tools.browser_session import close_browser_session
from family_assistant.tools.computer_use import (
    COMPUTER_USE_TOOLS_DEFINITION,
    computer_use_click,
    computer_use_double_click,
    computer_use_drag_and_drop,
    computer_use_go_back,
    computer_use_go_forward,
    computer_use_hotkey,
    computer_use_key_down,
    computer_use_key_up,
    computer_use_middle_click,
    computer_use_mouse_down,
    computer_use_mouse_up,
    computer_use_move,
    computer_use_navigate,
    computer_use_press_key,
    computer_use_right_click,
    computer_use_scroll,
    computer_use_take_screenshot,
    computer_use_triple_click,
    computer_use_type,
    computer_use_wait,
)
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import (
    ToolCallBatch,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from family_assistant.config_models import AppConfig
    from family_assistant.eval.browser.cases import BrowserEvalCase
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.llm.tool_call import ToolCallItem

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserEvalTrial",
    "BudgetExceededError",
    "SpendBudget",
    "ToolTraceEntry",
    "TrialStatus",
    "append_trial_jsonl",
    "build_browser_eval_tools_provider",
    "build_visual_eval_tools_provider",
    "get_browser_profile_system_prompt",
    "get_profile_system_prompt",
    "load_trials_jsonl",
    "run_browser_trial",
]

TrialStatus = Literal[
    "completed", "iteration_limit", "timeout", "error", "budget_exceeded"
]

DEFAULT_MAX_TOOL_RESULT_CHARS = 32_000
_TIME_MARKER = "\n\nCurrent local date and time: "

_STUB_TOOL_DEFINITIONS: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_to_service",
            "description": (
                "Delegate a sub-task to another service profile such as "
                "'browser_visual_profile' or 'credential_browser_profile'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_service_id": {"type": "string"},
                    "user_request": {"type": "string"},
                    "confirm_delegation": {"type": "boolean"},
                },
                "required": ["target_service_id", "user_request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_to_response",
            "description": "Attach previously generated attachment IDs to the final reply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["attachment_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_technical_problem",
            "description": "Record a technical issue encountered while running tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "details": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
    },
]


class BudgetExceededError(RuntimeError):
    """Raised when cumulative eval spend reaches or exceeds the configured cap."""


@dataclass
class SpendBudget:
    """Async-safe cumulative spend tracker enforcing a hard USD ceiling."""

    max_spend_usd: float
    spent_usd: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def check_available(self) -> bool:
        async with self._lock:
            return self.spent_usd < self.max_spend_usd

    async def record(self, amount_usd: float) -> float:
        async with self._lock:
            self.spent_usd += max(amount_usd, 0.0)
            return self.spent_usd


class ToolTraceEntry(BaseModel):
    """Compact summary of one tool call inside a browser eval trial."""

    name: str
    arguments_preview: str
    ok: bool
    duration_s: float
    output_chars: int
    has_image: bool = False
    error_preview: str | None = None


class BrowserEvalTrial(BaseModel):
    """Persisted record of one candidate running one browser eval case."""

    case_id: str
    category: str
    flags: list[str] = Field(default_factory=list)
    candidate_name: str
    mode: Literal["dom", "visual"] = "dom"
    provider: str
    model: str
    resolved_model: str | None = None
    # ast-grep-ignore: no-dict-any - effective provider parameters logged verbatim
    llm_parameters: dict[str, Any] = Field(default_factory=dict)
    status: TrialStatus = "completed"
    error_message: str | None = None
    started_at: str
    duration_s: float = 0.0
    requests: int = 0
    tool_calls: int = 0
    tool_error_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    tool_trace: list[ToolTraceEntry] = Field(default_factory=list)
    requested_handoff: bool = False
    # ast-grep-ignore: no-dict-any - recorded delegation payloads
    attempted_delegations: list[dict[str, Any]] = Field(default_factory=list)
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    billed_output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    final_reply: str | None = None


@dataclass
class _BoundaryRecorder:
    """Captures stubbed delegation/problem-report calls during a single trial."""

    # ast-grep-ignore: no-dict-any - recorded delegation payloads
    delegations: list[dict[str, Any]] = field(default_factory=list)
    technical_problems: list[str] = field(default_factory=list)


def get_profile_system_prompt(
    config: AppConfig,
    profile_id: str,
    *,
    user_name: str = "User",
    timezone_name: str = "Australia/Sydney",
    now: datetime | None = None,
) -> str:
    """Return a shipped profile's system prompt with current date/time."""
    profile = next(
        (p for p in config.service_profiles if p.id == profile_id),
        None,
    )
    if profile is None:
        raise KeyError(f"service_profiles does not define '{profile_id}'")
    raw_prompt = profile.processing_config.prompts.get("system_prompt")
    if not raw_prompt:
        raise KeyError(f"{profile_id} has no system_prompt configured")
    formatted = raw_prompt.format(user_name=user_name)
    tz = ZoneInfo(timezone_name)
    current_time = (now or datetime.now(UTC)).astimezone(tz)
    time_line = (
        f"{_TIME_MARKER}"
        f"{current_time.strftime('%A, %d %B %Y %H:%M %Z')} ({timezone_name})."
    )
    return formatted + time_line


def get_browser_profile_system_prompt(
    config: AppConfig,
    *,
    user_name: str = "User",
    timezone_name: str = "Australia/Sydney",
    now: datetime | None = None,
) -> str:
    """Return ``browser_profile``'s shipped system prompt with current date/time."""
    return get_profile_system_prompt(
        config,
        "browser_profile",
        user_name=user_name,
        timezone_name=timezone_name,
        now=now,
    )


def _boundary_stub_implementations(
    recorder: _BoundaryRecorder,
) -> dict[str, Callable[..., Awaitable[ToolResult]]]:
    async def _stub_delegate(
        exec_context: ToolExecutionContext,
        target_service_id: str,
        user_request: str,
        confirm_delegation: bool = False,
    ) -> ToolResult:
        del exec_context, confirm_delegation
        recorder.delegations.append({
            "target_service_id": target_service_id,
            "user_request": user_request[:500],
        })
        return ToolResult(
            text=(
                f"Delegation to '{target_service_id}' is unavailable in this "
                "automated single-model browser evaluation. Complete what you can "
                "directly with your browser tools, or clearly report what blocked "
                "you (such as a login wall, visual-only control, or bot block)."
            ),
            data={
                "error": "delegation_unavailable_in_eval",
                "target_service_id": target_service_id,
            },
        )

    async def _stub_attach(
        exec_context: ToolExecutionContext,
        attachment_ids: list[str],
    ) -> ToolResult:
        del exec_context
        return ToolResult(
            text=f"Recorded {len(attachment_ids)} attachment(s) for response.",
            data={"attached": attachment_ids},
        )

    async def _stub_report_problem(
        exec_context: ToolExecutionContext,
        summary: str,
        details: str = "",
    ) -> ToolResult:
        del exec_context
        recorder.technical_problems.append(f"{summary}: {details}"[:300])
        return ToolResult(text=f"Technical problem recorded: {summary}")

    return {
        "delegate_to_service": _stub_delegate,
        "attach_to_response": _stub_attach,
        "report_technical_problem": _stub_report_problem,
    }


def build_browser_eval_tools_provider(
    recorder: _BoundaryRecorder,
) -> LocalToolsProvider:
    """Build a ``LocalToolsProvider`` with real DOM tools + recorded boundary stubs."""
    definitions: list[ToolDefinition] = [
        *BROWSER_DOM_TOOLS_DEFINITION,
        *_STUB_TOOL_DEFINITIONS,
    ]
    implementations: dict[str, Callable[..., Awaitable[ToolResult]]] = {
        "browser_open": browser_open_tool,
        "browser_snapshot": browser_snapshot_tool,
        "browser_click": browser_click_tool,
        "browser_fill": browser_fill_tool,
        "browser_select": browser_select_tool,
        "browser_extract": browser_extract_tool,
        "browser_exec": browser_exec_tool,
        "browser_wait": browser_wait_tool,
        "browser_screenshot": browser_screenshot_tool,
        "browser_request_handoff": browser_request_handoff_tool,
        "browser_claim_handback": browser_claim_handback_tool,
        **_boundary_stub_implementations(recorder),
    }
    return LocalToolsProvider(definitions=definitions, implementations=implementations)


def build_visual_eval_tools_provider(
    recorder: _BoundaryRecorder,
) -> LocalToolsProvider:
    """Build a ``LocalToolsProvider`` with real Computer Use visual tools + stubs."""
    definitions: list[ToolDefinition] = [
        *COMPUTER_USE_TOOLS_DEFINITION,
        *_STUB_TOOL_DEFINITIONS,
    ]
    implementations: dict[str, Callable[..., Awaitable[ToolResult]]] = {
        "click": computer_use_click,
        "double_click": computer_use_double_click,
        "triple_click": computer_use_triple_click,
        "middle_click": computer_use_middle_click,
        "right_click": computer_use_right_click,
        "mouse_down": computer_use_mouse_down,
        "mouse_up": computer_use_mouse_up,
        "move": computer_use_move,
        "type": computer_use_type,
        "press_key": computer_use_press_key,
        "key_down": computer_use_key_down,
        "key_up": computer_use_key_up,
        "hotkey": computer_use_hotkey,
        "scroll": computer_use_scroll,
        "drag_and_drop": computer_use_drag_and_drop,
        "navigate": computer_use_navigate,
        "go_back": computer_use_go_back,
        "go_forward": computer_use_go_forward,
        "take_screenshot": computer_use_take_screenshot,
        "wait": computer_use_wait,
        **_boundary_stub_implementations(recorder),
    }
    return LocalToolsProvider(definitions=definitions, implementations=implementations)


def _parse_call_arguments(call: ToolCallItem) -> dict[str, object]:
    raw = call.function.arguments
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.75)
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... truncated {omitted} chars ...]\n\n{text[-tail:]}"


def _is_error_result(result: ToolResult) -> tuple[bool, str | None]:
    data = result.get_data()
    if isinstance(data, dict) and data.get("error"):
        return True, str(data.get("error"))[:160]
    text = result.get_text()
    if text.startswith("Error:"):
        return True, text[:160]
    return False, None


def _check_safety_decision(
    tool_name: str, args: dict[str, object]
) -> ToolResult | None:
    """Pop Gemini computer-use ``safety_decision`` and fail closed if unpermitted."""
    safety_decision = args.pop("safety_decision", None)
    if safety_decision is None:
        return None
    decision = (
        safety_decision.get("decision") if isinstance(safety_decision, dict) else None
    )
    if decision == "allow":
        return None
    explanation = (
        str(safety_decision.get("explanation", ""))
        if isinstance(safety_decision, dict)
        else ""
    )
    return ToolResult(
        text=(
            f"Action '{tool_name}' was refused in automated read-only eval because "
            f"computer-use flagged safety_decision={decision!r}: {explanation}"
        ),
        data={"error": "safety_decision_refused", "decision": decision},
    )


async def _execute_one_tool_call(
    tools_provider: LocalToolsProvider,
    call: ToolCallItem,
    base_context: ToolExecutionContext,
    batch: ToolCallBatch,
    *,
    max_tool_result_chars: int,
) -> tuple[ToolResult, ToolTraceEntry]:
    args = _parse_call_arguments(call)
    args_preview = json.dumps(args, ensure_ascii=False)[:240]
    call_context = replace(
        base_context,
        tool_call_batch=batch,
        tool_call_id=call.id,
    )
    started = time.monotonic()
    refused = _check_safety_decision(call.function.name, args)
    if refused is not None:
        batch.mark_done(call.id)
        raw_result: str | ToolResult = refused
    else:
        try:
            raw_result = await tools_provider.execute_tool(
                call.function.name,
                args,
                call_context,
                call_id=call.id,
            )
        except Exception as exc:
            logger.warning("Eval tool %s failed: %s", call.function.name, exc)
            raw_result = ToolResult(
                text=f"Error executing {call.function.name}: {exc}",
                data={"error": str(exc)},
            )
        finally:
            batch.mark_done(call.id)

    elapsed = time.monotonic() - started
    result = (
        raw_result
        if isinstance(raw_result, ToolResult)
        else ToolResult(text=str(raw_result))
    )
    text = result.get_text()
    if len(text) > max_tool_result_chars:
        result = ToolResult(
            text=_truncate_text(text, max_tool_result_chars),
            attachments=result.attachments,
            data=result.data,
        )
    is_err, err_preview = _is_error_result(result)
    trace = ToolTraceEntry(
        name=call.function.name,
        arguments_preview=args_preview,
        ok=not is_err,
        duration_s=round(elapsed, 3),
        output_chars=len(result.get_text()),
        has_image=bool(result.attachments),
        error_preview=err_preview,
    )
    return result, trace


def _create_eval_database() -> Database:
    """Create a lazy in-memory SQLite ``Database`` (never queried by DOM tools)."""
    return Database(create_engine_with_sqlite_optimizations("sqlite+aiosqlite://"))


async def _invoke_llm(
    client: LLMInterface,
    messages: list[LLMMessage],
    tool_defs: list[ToolDefinition],
) -> LLMOutput:
    """Call ``generate_response_stream`` (matching production caching) when available."""
    stream_fn = getattr(client, "generate_response_stream", None)
    if stream_fn is None:
        return await client.generate_response(messages, tool_defs or None)

    content_chunks: list[str] = []
    tool_calls: list[ToolCallItem] = []
    reasoning_info: MessageReasoningInfo | None = None
    provider_metadata = None

    async for event in stream_fn(messages, tool_defs or None):
        if event.type == "content" and event.content:
            content_chunks.append(event.content)
        elif event.type == "tool_call" and event.tool_call is not None:
            tool_calls.append(event.tool_call)
        elif event.type == "done" and event.metadata:
            if "reasoning_info" in event.metadata:
                reasoning_info = event.metadata["reasoning_info"]
            if "provider_metadata" in event.metadata:
                provider_metadata = event.metadata["provider_metadata"]
        elif event.type == "error":
            raise RuntimeError(event.error or "LLM stream returned an error event")

    raw_resolved = reasoning_info.get("resolved_model") if reasoning_info else None
    resolved_model = str(raw_resolved) if raw_resolved else None
    return LLMOutput(
        content="".join(content_chunks) if content_chunks else None,
        tool_calls=tool_calls or None,
        reasoning_info=reasoning_info,
        provider_metadata=provider_metadata,
        resolved_model=resolved_model,
    )


async def _run_loop(
    client: LLMInterface,
    tools_provider: LocalToolsProvider,
    messages: list[LLMMessage],
    tool_defs: list[ToolDefinition],
    base_context: ToolExecutionContext,
    trial: BrowserEvalTrial,
    totals: TokenTotals,
    *,
    max_iterations: int,
    max_tool_result_chars: int,
    budget: SpendBudget | None,
) -> None:
    for _ in range(max_iterations):
        if budget is not None and not await budget.check_available():
            trial.status = "budget_exceeded"
            trial.error_message = (
                f"Stopped before request: cumulative spend reached "
                f"${budget.spent_usd:.2f} / ${budget.max_spend_usd:.2f}"
            )
            return

        output = await _invoke_llm(client, messages, tool_defs)
        trial.requests += 1
        if output.resolved_model:
            trial.resolved_model = output.resolved_model

        cost_model = output.resolved_model or trial.model
        accumulate_usage(totals, trial.provider, output.reasoning_info)
        call_cost = estimate_call_cost_usd(
            trial.provider, cost_model, output.reasoning_info
        )
        trial.estimated_cost_usd += call_cost
        if budget is not None:
            await budget.record(call_cost)

        if not output.tool_calls:
            trial.final_reply = output.content
            trial.status = "completed"
            return

        messages.append(
            AssistantMessage(
                content=output.content,
                tool_calls=output.tool_calls,
                provider_metadata=output.provider_metadata,
                reasoning_info=output.reasoning_info,
            )
        )

        batch = ToolCallBatch([
            (call.id, call.function.name) for call in output.tool_calls
        ])
        tasks = [
            _execute_one_tool_call(
                tools_provider,
                call,
                base_context,
                batch,
                max_tool_result_chars=max_tool_result_chars,
            )
            for call in output.tool_calls
        ]
        results_and_traces = await asyncio.gather(*tasks)

        for call, (result, trace) in zip(
            output.tool_calls, results_and_traces, strict=True
        ):
            trial.tool_calls += 1
            trial.tool_names.append(call.function.name)
            trial.tool_trace.append(trace)
            if not trace.ok:
                trial.tool_error_count += 1
            if call.function.name == "browser_request_handoff":
                trial.requested_handoff = True
            messages.append(
                tool_result_to_llm_message(
                    result,
                    tool_call_id=call.id,
                    function_name=call.function.name,
                )
            )

    trial.status = "iteration_limit"


async def run_browser_trial(
    client: LLMInterface,
    case: BrowserEvalCase,
    *,
    candidate_name: str,
    provider: str,
    model: str,
    system_prompt: str,
    mode: Literal["dom", "visual"] = "dom",
    # ast-grep-ignore: no-dict-any - provider parameters recorded verbatim
    llm_parameters: dict[str, Any] | None = None,
    timezone_name: str = "Australia/Sydney",
    max_iterations: int = 50,
    timeout_s: float = 600.0,
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
    budget: SpendBudget | None = None,
    tools_provider: LocalToolsProvider | None = None,
) -> BrowserEvalTrial:
    """Execute one live browser trial (DOM or visual) and guarantee cleanup."""
    started_iso = datetime.now(UTC).isoformat()
    started_mono = time.monotonic()
    conversation_id = f"browser-eval-{case.id}-{candidate_name}-{uuid.uuid4().hex[:8]}"

    trial = BrowserEvalTrial(
        case_id=case.id,
        category=case.category,
        flags=list(case.flags),
        candidate_name=candidate_name,
        mode=mode,
        provider=provider,
        model=model,
        llm_parameters=dict(llm_parameters or {}),
        started_at=started_iso,
    )
    if budget is not None and not await budget.check_available():
        trial.status = "budget_exceeded"
        trial.error_message = (
            f"Skipped before start: cumulative spend ${budget.spent_usd:.2f} "
            f">= cap ${budget.max_spend_usd:.2f}"
        )
        return trial

    recorder = _BoundaryRecorder()
    if tools_provider is not None:
        active_provider = tools_provider
    elif mode == "visual":
        active_provider = build_visual_eval_tools_provider(recorder)
    else:
        active_provider = build_browser_eval_tools_provider(recorder)
    tool_defs = await active_provider.get_tool_definitions()
    eval_db = _create_eval_database()
    base_context = ToolExecutionContext(
        interface_type="eval",
        conversation_id=conversation_id,
        user_name="User",
        turn_id=f"turn-{uuid.uuid4().hex[:8]}",
        db_context=eval_db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo(timezone_name),
    )
    stable_prefix_len = (
        system_prompt.index(_TIME_MARKER)
        if _TIME_MARKER in system_prompt
        else len(system_prompt)
    )
    messages: list[LLMMessage] = [
        SystemMessage(content=system_prompt, stable_prefix_len=stable_prefix_len),
        UserMessage(content=case.task),
    ]
    totals = TokenTotals()

    try:
        await asyncio.wait_for(
            _run_loop(
                client,
                active_provider,
                messages,
                tool_defs,
                base_context,
                trial,
                totals,
                max_iterations=max_iterations,
                max_tool_result_chars=max_tool_result_chars,
                budget=budget,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        trial.status = "timeout"
        trial.error_message = f"Trial exceeded {timeout_s:.0f}s timeout"
    except Exception as exc:
        logger.exception(
            "Browser eval trial failed: case=%s candidate=%s", case.id, candidate_name
        )
        trial.status = "error"
        trial.error_message = f"{type(exc).__name__}: {exc}"
    finally:
        await close_browser_session(base_context)
        await eval_db.engine.dispose()

    trial.duration_s = round(time.monotonic() - started_mono, 2)
    trial.attempted_delegations = recorder.delegations
    trial.uncached_input_tokens = totals.uncached_input_tokens
    trial.cached_input_tokens = totals.cached_input_tokens
    trial.cache_write_tokens = totals.cache_write_tokens
    trial.cache_write_1h_tokens = totals.cache_write_1h_tokens
    trial.output_tokens = totals.output_tokens
    trial.reasoning_tokens = totals.reasoning_tokens
    trial.billed_output_tokens = totals.billed_output_tokens
    trial.estimated_cost_usd = round(trial.estimated_cost_usd, 6)
    return trial


def append_trial_jsonl(path: Path, trial: BrowserEvalTrial) -> None:
    """Append one completed trial as a single JSON line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(trial.model_dump_json() + "\n")


def load_trials_jsonl(path: Path) -> list[BrowserEvalTrial]:
    """Load persisted trials from a JSONL file (returning empty list if absent)."""
    if not path.exists():
        return []
    trials: list[BrowserEvalTrial] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            trials.append(BrowserEvalTrial.model_validate_json(stripped))
    return trials


def completed_trial_keys(trials: Sequence[BrowserEvalTrial]) -> set[tuple[str, str]]:
    """Return ``(case_id, candidate_name)`` keys already run (excluding budget skips)."""
    return {
        (trial.case_id, trial.candidate_name)
        for trial in trials
        if trial.status != "budget_exceeded"
    }
