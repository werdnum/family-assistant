"""Unit tests for the browser task evaluation harness."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from family_assistant.config_loader import load_config
from family_assistant.eval.browser.candidates import (
    BUILTIN_CANDIDATES,
    BrowserEvalCandidate,
    build_candidate_client,
    resolve_candidates,
)
from family_assistant.eval.browser.cases import (
    BrowserEvalCase,
    filter_cases,
    load_browser_cases,
)
from family_assistant.eval.browser.judge import (
    BlindCandidateGrade,
    BlindCaseJudgement,
    CandidateGrade,
    CaseJudgement,
    assign_blind_labels,
    compute_trial_set_fingerprint,
    judge_case_trials,
)
from family_assistant.eval.browser.pricing import (
    TokenTotals,
    accumulate_usage,
    estimate_call_cost_usd,
)
from family_assistant.eval.browser.report import (
    compute_candidate_summaries,
    compute_pairwise_lift,
    render_markdown_report,
)
from family_assistant.eval.browser.runner import (
    BrowserEvalTrial,
    SpendBudget,
    ToolTraceEntry,
    append_trial_jsonl,
    completed_trial_keys,
    get_browser_profile_system_prompt,
    load_trials_jsonl,
    run_browser_trial,
)
from family_assistant.eval.private_paths import PrivateEvalPathError
from family_assistant.llm import (
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import MessageReasoningInfo, SystemMessage
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition


class _ScriptedBrowserLLM:
    """Minimal LLMInterface double that drives a scripted sequence of outputs."""

    def __init__(self, outputs: Sequence[LLMOutput]) -> None:
        self._outputs = list(outputs)
        self.calls: int = 0

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        del messages, tools, tool_choice
        self.calls += 1
        if not self._outputs:
            return LLMOutput(content="Done.")
        return self._outputs.pop(0)


class _FakeJudgeLLM:
    """Minimal structured-output LLM double for blind judge unit tests."""

    def __init__(self, response: BlindCaseJudgement) -> None:
        self._response = response
        self.last_prompt: str = ""

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        del max_retries
        assert response_model is BlindCaseJudgement
        self.last_prompt = str(messages[-1].content)
        return self._response  # type: ignore[return-value]


def test_load_raw_export_and_filter(tmp_path: Path) -> None:
    raw_export = {
        "rows": [
            {
                "eval_set": {
                    "description": "synthetic set",
                    "cases": [
                        {
                            "id": "c1",
                            "category": "single_page_extraction",
                            "flags": ["stable"],
                            "task": "Read page A",
                            "original_final_answer": "Alpha",
                        },
                        {
                            "id": "c2",
                            "category": "single_page_extraction",
                            "flags": ["private_event_page"],
                            "task": "Private event",
                            "original_final_answer": None,
                        },
                        {
                            "id": "c3",
                            "category": "constraint_product_research",
                            "flags": ["followup_correction_of_c1"],
                            "task": "Followup",
                        },
                        {
                            "id": "c4",
                            "category": "retail_search",
                            "flags": ["live_prices"],
                            "task": "Check price",
                        },
                    ],
                }
            }
        ]
    }
    cases_file = tmp_path / "export.json"
    cases_file.write_text(json.dumps(raw_export), encoding="utf-8")

    with pytest.raises(PrivateEvalPathError):
        load_browser_cases(cases_file, require_private_path=True)

    dataset = load_browser_cases(cases_file, require_private_path=False)
    assert len(dataset.cases) == 4
    assert dataset.cases[1].original_final_answer is None

    filtered = filter_cases(dataset.cases)
    assert [c.id for c in filtered] == ["c1", "c4"]

    cat_filtered = filter_cases(dataset.cases, include_categories=["retail_search"])
    assert [c.id for c in cat_filtered] == ["c4"]


def test_provider_aware_pricing_and_usage_accounting() -> None:
    # Google: cached is subset of prompt, reasoning is billed separately as output.
    google_usage = MessageReasoningInfo(
        prompt_tokens=1_000_000,
        cached_prompt_tokens=600_000,
        completion_tokens=100_000,
        reasoning_tokens=200_000,
    )
    google_totals = TokenTotals()
    accumulate_usage(google_totals, "google", google_usage)
    assert google_totals.uncached_input_tokens == 400_000
    assert google_totals.cached_input_tokens == 600_000
    assert google_totals.billed_output_tokens == 300_000
    # Hydrogen: $1.50 uncached in, $0.075 cached in, $7.50 out
    google_cost = estimate_call_cost_usd("google", "gemini-4-hydrogen", google_usage)
    expected_google = 0.4 * 1.50 + 0.6 * 0.075 + 0.3 * 7.50
    assert google_cost == pytest.approx(expected_google, rel=1e-6)

    # OpenAI: cached & cache_write are subsets of prompt; reasoning folded in completion.
    openai_usage = MessageReasoningInfo(
        prompt_tokens=1_000_000,
        cached_prompt_tokens=500_000,
        cache_write_tokens=100_000,
        completion_tokens=300_000,
        reasoning_tokens=200_000,
    )
    openai_totals = TokenTotals()
    accumulate_usage(openai_totals, "openai", openai_usage)
    assert openai_totals.uncached_input_tokens == 400_000
    assert openai_totals.billed_output_tokens == 300_000
    openai_cost = estimate_call_cost_usd("openai", "gpt-6.2-sol-2026-11", openai_usage)
    expected_openai = 0.4 * 2.00 + 0.5 * 0.10 + 0.1 * 2.50 + 0.3 * 10.00
    assert openai_cost == pytest.approx(expected_openai, rel=1e-6)

    # Anthropic: uncached, cached, and cache_write are disjoint buckets.
    anthropic_usage = MessageReasoningInfo(
        prompt_tokens=200_000,
        cached_prompt_tokens=700_000,
        cache_write_tokens=100_000,
        completion_tokens=100_000,
    )
    anthropic_totals = TokenTotals()
    accumulate_usage(anthropic_totals, "anthropic", anthropic_usage)
    assert anthropic_totals.uncached_input_tokens == 200_000
    assert anthropic_totals.total_input_tokens == 1_000_000
    anthropic_cost = estimate_call_cost_usd(
        "anthropic", "claude-sonnet-5-5", anthropic_usage
    )
    expected_anthropic = 0.2 * 2.00 + 0.7 * 0.10 + 0.1 * 2.50 + 0.1 * 10.00
    assert anthropic_cost == pytest.approx(expected_anthropic, rel=1e-6)


def test_shipped_candidates_and_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    config = load_config(load_dotenv_file=False)

    prompt = get_browser_profile_system_prompt(config, user_name="Tester")
    assert "browser_open" in prompt
    assert "Current local date and time:" in prompt

    resolved = resolve_candidates(["hydrogen", "neon-medium", "gpt-6.2-sol"])
    assert [c.name for c in resolved] == ["hydrogen", "neon-medium", "gpt-6.2-sol"]

    _, provider, model, params = build_candidate_client(
        config, BUILTIN_CANDIDATES["neon-medium"]
    )
    assert provider == "google"
    assert model == "gemini-4-neon"
    assert params.get("thinking_config", {}).get("thinking_level") == "medium"

    _, sol_provider, sol_model, sol_params = build_candidate_client(
        config, BUILTIN_CANDIDATES["gpt-6.2-sol"]
    )
    assert sol_provider == "openai"
    assert sol_model == "gpt-6.2-sol"
    assert sol_params.get("reasoning_effort") == "medium"

    _, sonnet_provider, sonnet_model, sonnet_params = build_candidate_client(
        config, BUILTIN_CANDIDATES["sonnet-5.5"]
    )
    assert sonnet_provider == "anthropic"
    assert sonnet_model == "claude-sonnet-5-5"
    assert sonnet_params.get("output_config", {}).get("effort") == "medium"
    assert "custom_instructions" not in sonnet_params

    with pytest.raises(ValueError, match="Unknown browser eval candidate"):
        resolve_candidates(["nonexistent-model"])

    with pytest.raises(KeyError):
        build_candidate_client(
            config, BrowserEvalCandidate(name="bad", tier_id="missing_tier")
        )


@pytest.mark.asyncio
async def test_run_browser_trial_against_local_html_and_budget(
    tmp_path: Path,
) -> None:
    async def _index(_request: web.Request) -> web.Response:
        return web.Response(
            text="<html><head><title>Widget Shop</title></head>"
            "<body><h1>Widget Price</h1><p>$42.50 in stock</p></body></html>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", _index)
    server = TestServer(app)
    await server.start_server()
    try:
        base_url = str(server.make_url("/"))
        llm = _ScriptedBrowserLLM([
            LLMOutput(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(
                            name="browser_open",
                            arguments=json.dumps({"url": base_url}),
                        ),
                    ),
                    ToolCallItem(
                        id="call_2",
                        type="function",
                        function=ToolCallFunction(
                            name="delegate_to_service",
                            arguments=json.dumps({
                                "target_service_id": "browser_visual_profile",
                                "user_request": "check image",
                            }),
                        ),
                    ),
                ],
                reasoning_info=MessageReasoningInfo(
                    prompt_tokens=1000,
                    cached_prompt_tokens=0,
                    completion_tokens=100,
                    reasoning_tokens=50,
                ),
            ),
            LLMOutput(
                content="The widget costs $42.50 and is in stock.",
                reasoning_info=MessageReasoningInfo(
                    prompt_tokens=2000,
                    cached_prompt_tokens=1000,
                    completion_tokens=50,
                    reasoning_tokens=20,
                ),
            ),
        ])
        case = BrowserEvalCase(
            id="synth-1",
            category="single_page_extraction",
            flags=["stable"],
            task=f"Open {base_url} and report the widget price.",
        )
        budget = SpendBudget(max_spend_usd=10.0)
        trial = await run_browser_trial(
            llm,  # type: ignore[arg-type]
            case,
            candidate_name="hydrogen",
            provider="google",
            model="gemini-4-hydrogen",
            system_prompt="You are a browser agent.",
            budget=budget,
        )
        assert trial.status == "completed"
        assert trial.requests == 2
        assert trial.tool_calls == 2
        assert trial.tool_names == ["browser_open", "delegate_to_service"]
        assert trial.attempted_delegations == [
            {
                "target_service_id": "browser_visual_profile",
                "user_request": "check image",
            }
        ]
        assert trial.final_reply == "The widget costs $42.50 and is in stock."
        assert trial.estimated_cost_usd > 0
        assert budget.spent_usd == pytest.approx(trial.estimated_cost_usd, rel=1e-4)

        # JSONL persistence and resume tracking
        jsonl_path = tmp_path / "trials.jsonl"
        append_trial_jsonl(jsonl_path, trial)
        loaded = load_trials_jsonl(jsonl_path)
        assert len(loaded) == 1
        assert completed_trial_keys(loaded) == {("synth-1", "hydrogen")}

        # Exhausted budget skips cleanly without calling the LLM
        exhausted_budget = SpendBudget(max_spend_usd=0.01, spent_usd=0.01)
        skipped_llm = _ScriptedBrowserLLM([])
        skipped = await run_browser_trial(
            skipped_llm,  # type: ignore[arg-type]
            case,
            candidate_name="neon",
            provider="google",
            model="gemini-4-neon",
            system_prompt="You are a browser agent.",
            budget=exhausted_budget,
        )
        assert skipped.status == "budget_exceeded"
        assert skipped_llm.calls == 0
        assert completed_trial_keys([skipped]) == set()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_blind_judge_and_sanitized_report() -> None:
    labels = assign_blind_labels("case-1", ["hydrogen", "neon"])
    assert set(labels.keys()) == {"A", "B"}
    assert set(labels.values()) == {"hydrogen", "neon"}
    assert assign_blind_labels("case-1", ["neon", "hydrogen"]) == labels

    case = BrowserEvalCase(
        id="case-1",
        category="single_page_extraction",
        flags=["stable"],
        task="SECRET HOUSEHOLD PROMPT THAT MUST NOT APPEAR IN SANITIZED REPORT",
        original_final_answer="Reference answer",
    )
    trials = [
        BrowserEvalTrial(
            case_id="case-1",
            category="single_page_extraction",
            candidate_name="hydrogen",
            provider="google",
            model="gemini-4-hydrogen",
            started_at="2026-11-23T00:00:00Z",
            duration_s=10.0,
            tool_calls=2,
            estimated_cost_usd=0.05,
            final_reply="Hydrogen answer",
        ),
        BrowserEvalTrial(
            case_id="case-1",
            category="single_page_extraction",
            candidate_name="neon",
            provider="google",
            model="gemini-4-neon",
            started_at="2026-11-23T00:00:00Z",
            duration_s=12.0,
            tool_calls=2,
            estimated_cost_usd=0.15,
            final_reply="Neon answer",
        ),
    ]
    hydro_label = next(k for k, v in labels.items() if v == "hydrogen")
    neon_label = next(k for k, v in labels.items() if v == "neon")
    judge_llm = _FakeJudgeLLM(
        BlindCaseJudgement(
            grades=[
                BlindCandidateGrade(
                    label=hydro_label,
                    outcome="complete",
                    score=4,
                    grounded=True,
                    followed_constraints=True,
                    rationale="Complete grounded extraction.",
                ),
                BlindCandidateGrade(
                    label=neon_label,
                    outcome="complete",
                    score=5,
                    grounded=True,
                    followed_constraints=True,
                    rationale="Complete grounded extraction with extra detail.",
                ),
            ],
            consensus_status="all_agree",
            consensus_labels=[hydro_label, neon_label],
            winner_labels=[neon_label],
            disagreement_summary="SENSITIVE CASE NOTE ONLY FOR LOCAL REPORT",
        )
    )
    judgement = await judge_case_trials(
        judge_llm,  # type: ignore[arg-type]
        judge_model="claude-sonnet-5-5",
        case=case,
        trials=trials,
    )
    # Blind prompt must not leak model names
    assert "gemini-4-hydrogen" not in judge_llm.last_prompt
    assert "gemini-4-neon" not in judge_llm.last_prompt
    assert isinstance(judgement, CaseJudgement)
    assert judgement.winner_candidates == ["neon"]
    assert judgement.consensus_candidates == ["hydrogen", "neon"]
    grade_by_name: dict[str, CandidateGrade] = {
        g.candidate_name: g for g in judgement.grades
    }
    assert grade_by_name["hydrogen"].score == 4
    assert grade_by_name["neon"].score == 5

    summaries = compute_candidate_summaries(trials, [judgement])
    assert [s.candidate_name for s in summaries] == ["hydrogen", "neon"]
    assert summaries[0].passes == 1
    assert summaries[1].passes == 1

    lift = compute_pairwise_lift(
        trials, [judgement], baseline="hydrogen", challenger="neon"
    )
    assert lift.shared_cases == 1
    assert lift.pass_lift == 0
    assert lift.challenger_wins == 1
    assert lift.mean_score_delta == pytest.approx(1.0)
    assert lift.cost_delta_usd == pytest.approx(0.10)

    local_md = render_markdown_report(
        trials, [judgement], baseline_candidate="hydrogen", sanitized=False
    )
    assert "SENSITIVE CASE NOTE ONLY FOR LOCAL REPORT" in local_md
    assert "case-1" in local_md

    sanitized_md = render_markdown_report(
        trials, [judgement], baseline_candidate="hydrogen", sanitized=True
    )
    assert "SENSITIVE CASE NOTE" not in sanitized_md
    assert "SECRET HOUSEHOLD PROMPT" not in sanitized_md
    assert "case-1" not in sanitized_md
    assert "all_agree" in sanitized_md
    assert "Pairwise Lift vs `hydrogen`" in sanitized_md


class _StreamingBrowserLLM:
    """Minimal streaming LLM double verifying stream consumption and stable_prefix_len."""

    def __init__(self) -> None:
        self.seen_system_message: SystemMessage | None = None

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        del tools, tool_choice
        assert isinstance(messages[0], SystemMessage)
        self.seen_system_message = messages[0]
        yield LLMStreamEvent(type="content", content="Streamed ")
        yield LLMStreamEvent(type="content", content="answer.")
        yield LLMStreamEvent(
            type="done",
            metadata={
                "reasoning_info": MessageReasoningInfo(
                    prompt_tokens=1000,
                    cached_prompt_tokens=800,
                    completion_tokens=20,
                    resolved_model="gpt-6.2-sol-2026-11",
                )
            },
        )


@pytest.mark.asyncio
async def test_run_browser_trial_uses_streaming_and_stable_prefix() -> None:
    llm = _StreamingBrowserLLM()
    case = BrowserEvalCase(
        id="stream-1",
        category="single_page_extraction",
        task="Summarize.",
    )
    prompt = "Base browser instructions.\n\nCurrent local date and time: Monday."
    trial = await run_browser_trial(
        llm,  # type: ignore[arg-type]
        case,
        candidate_name="gpt-6.2-sol",
        provider="openai",
        model="gpt-6.2-sol",
        system_prompt=prompt,
    )
    assert trial.status == "completed"
    assert trial.final_reply == "Streamed answer."
    assert trial.resolved_model == "gpt-6.2-sol-2026-11"
    assert trial.cached_input_tokens == 800
    assert trial.uncached_input_tokens == 200
    assert llm.seen_system_message is not None
    assert llm.seen_system_message.stable_prefix_len == len(
        "Base browser instructions."
    )


@pytest.mark.asyncio
async def test_visual_candidate_and_safety_decision_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    config = load_config(load_dotenv_file=False)
    client, provider, model, params = build_candidate_client(
        config, BUILTIN_CANDIDATES["visual-hydrogen-medium"]
    )
    assert provider == "google"
    assert model == "gemini-4-hydrogen"
    assert getattr(client, "enable_computer_use", False) is True
    assert params.get("thinking_config", {}).get("thinking_level") == "medium"

    llm = _ScriptedBrowserLLM([
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="cu_1",
                    type="function",
                    function=ToolCallFunction(
                        name="click",
                        arguments=json.dumps({
                            "x": 500,
                            "y": 500,
                            "intent": "Confirm payment",
                            "safety_decision": {
                                "decision": "require_confirmation",
                                "explanation": "Consequential action",
                            },
                        }),
                    ),
                )
            ],
        ),
        LLMOutput(content="Stopped because confirmation is required."),
    ])
    case = BrowserEvalCase(
        id="vis-1",
        category="interactive_quote_no_submit",
        task="Check quote without booking.",
    )
    trial = await run_browser_trial(
        llm,  # type: ignore[arg-type]
        case,
        candidate_name="visual-hydrogen-medium",
        mode="visual",
        provider="google",
        model="gemini-4-hydrogen",
        system_prompt="You are a visual browser agent.",
    )
    assert trial.status == "completed"
    assert trial.mode == "visual"
    assert trial.tool_calls == 1
    assert trial.tool_error_count == 1
    assert trial.tool_trace[0].error_preview == "safety_decision_refused"
    assert trial.final_reply == "Stopped because confirmation is required."


@pytest.mark.asyncio
async def test_visual_allows_production_safe_decisions_and_captures_output_preview() -> (
    None
):
    executed: list[str] = []

    async def _fake_click(
        exec_context: ToolExecutionContext,
        x: int,
        y: int,
        intent: str = "",
    ) -> ToolResult:
        del exec_context, x, y
        executed.append(intent)
        return ToolResult(text=f"Clicked successfully: {intent} (price $19.99)")

    provider = LocalToolsProvider(
        definitions=[
            {
                "type": "function",
                "function": {
                    "name": "click",
                    "description": "Click coordinates",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "intent": {"type": "string"},
                        },
                        "required": ["x", "y"],
                    },
                },
            }
        ],
        implementations={"click": _fake_click},
    )
    llm = _ScriptedBrowserLLM([
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="c1",
                    type="function",
                    function=ToolCallFunction(
                        name="click",
                        arguments=json.dumps({
                            "x": 100,
                            "y": 100,
                            "intent": "open menu",
                            "safety_decision": {"decision": "allowed"},
                        }),
                    ),
                ),
                ToolCallItem(
                    id="c2",
                    type="function",
                    function=ToolCallFunction(
                        name="click",
                        arguments=json.dumps({
                            "x": 200,
                            "y": 200,
                            "intent": "select tab",
                            "safety_decision": {"decision": "regular"},
                        }),
                    ),
                ),
            ],
        ),
        LLMOutput(content="Price is $19.99."),
    ])
    case = BrowserEvalCase(id="vis-2", category="retail_search", task="Check price.")
    trial = await run_browser_trial(
        llm,  # type: ignore[arg-type]
        case,
        candidate_name="visual-hydrogen-medium",
        mode="visual",
        provider="google",
        model="gemini-4-hydrogen",
        system_prompt="Visual agent.",
        tools_provider=provider,
    )
    assert executed == ["open menu", "select tab"]
    assert trial.tool_error_count == 0
    assert "$19.99" in trial.tool_trace[0].output_preview

    # Verify observed tool output reaches the blind judge prompt and fingerprint updates
    fp_one = compute_trial_set_fingerprint([trial])
    trial_v2 = trial.model_copy(update={"final_reply": "Updated price $20.00."})
    assert compute_trial_set_fingerprint([trial_v2]) != fp_one

    trial_with_preview = BrowserEvalTrial(
        case_id="vis-2",
        category="retail_search",
        candidate_name="hydrogen",
        provider="google",
        model="gemini-4-hydrogen",
        started_at="2026-11-23T00:00:00Z",
        tool_trace=[
            ToolTraceEntry(
                name="browser_open",
                arguments_preview='{"url": "https://example.test"}',
                ok=True,
                duration_s=0.5,
                output_chars=40,
                output_preview="Observed page snippet: $19.99 in stock",
            )
        ],
        final_reply="$19.99",
    )
    judge_llm = _FakeJudgeLLM(
        BlindCaseJudgement(
            grades=[
                BlindCandidateGrade(
                    label="A",
                    outcome="complete",
                    score=5,
                    grounded=True,
                    followed_constraints=True,
                    rationale="Matches observed snippet.",
                )
            ],
            consensus_status="all_agree",
            consensus_labels=["A"],
            winner_labels=["A"],
        )
    )
    judgement = await judge_case_trials(
        judge_llm,  # type: ignore[arg-type]
        judge_model="claude-opus-5-5",
        case=case,
        trials=[trial_with_preview],
    )
    assert "Observed page snippet: $19.99 in stock" in judge_llm.last_prompt
    assert judgement.trial_fingerprint == compute_trial_set_fingerprint([
        trial_with_preview
    ])


@pytest.mark.asyncio
async def test_spend_budget_atomic_reservations() -> None:
    budget = SpendBudget(max_spend_usd=0.08)
    r1 = await budget.try_reserve(0.05)
    r2 = await budget.try_reserve(0.05)
    r3 = await budget.try_reserve(0.05)
    assert r1 is not None
    assert r1 == pytest.approx(0.05)
    assert r2 == pytest.approx(0.03)
    assert r3 is None
    assert not await budget.check_available()

    await budget.settle(r1, 0.02)
    assert await budget.check_available()
    assert budget.spent_usd == pytest.approx(0.02)
    assert budget.reserved_usd == pytest.approx(0.03)
