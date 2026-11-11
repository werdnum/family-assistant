"""The turn replay drives a tool loop from recordings and grades the result."""

from family_assistant.config_models import AppConfig
from family_assistant.eval.turn_replay import (
    RecordedToolResult,
    ReplayCase,
    ReplayExpectation,
    build_tier_entry_client,
    run_trial,
)
from family_assistant.llm import LLMOutput
from family_assistant.llm.custom_instructions import CustomInstructionsClient
from family_assistant.llm.messages import ToolMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient


def _case() -> ReplayCase:
    return ReplayCase(
        id="earlier-train",
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Can I get an earlier train?"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "plan_trip",
                    "description": "Plan a public transport trip.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        recorded_tool_results=[
            RecordedToolResult(
                name="plan_trip",
                arguments_contain={"origin": "chatswood"},
                result="17:10 T1 from Chatswood",
            )
        ],
        expect=ReplayExpectation(
            required_tools_any=["plan_trip"],
            forbidden_reply_patterns=[r"haven'?t checked"],
        ),
    )


def _has_tool_result(args: MatcherArgs) -> bool:
    return any(isinstance(m, ToolMessage) for m in args["messages"])


async def test_a_turn_that_checks_before_answering_passes() -> None:
    llm = RuleBasedMockLLMClient(
        rules=[
            (_has_tool_result, LLMOutput(content="Take the 17:10 T1.")),
        ],
        default_response=LLMOutput(
            tool_calls=[
                ToolCallItem(
                    id="call-1",
                    type="function",
                    function=ToolCallFunction(
                        name="plan_trip", arguments='{"origin": "Chatswood"}'
                    ),
                )
            ]
        ),
    )

    trial = await run_trial(llm, _case(), variant="shipped", model="mock")

    assert (trial.passed, trial.tool_calls, trial.unrecorded_calls) == (
        True,
        [{"name": "plan_trip", "arguments": {"origin": "Chatswood"}, "recorded": True}],
        0,
    )


async def test_a_turn_that_names_the_check_without_running_it_fails() -> None:
    llm = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(
            content="An earlier T1 may be possible. I haven't checked for one."
        ),
    )

    trial = await run_trial(llm, _case(), variant="shipped", model="mock")

    assert trial.failures == [
        "none of ['plan_trip'] called before the reply",
        'reply matches "haven\'?t checked"',
    ]


def test_the_control_variant_drops_the_entry_custom_instructions(
    shipped_config: AppConfig,
    provider_api_keys: None,
) -> None:
    client = build_tier_entry_client(
        shipped_config, "claude_sonnet_5_5", 0, with_custom_instructions=False
    )

    assert isinstance(client, AnthropicClient)


def test_the_shipped_variant_keeps_them(
    shipped_config: AppConfig,
    provider_api_keys: None,
) -> None:
    client = build_tier_entry_client(
        shipped_config, "claude_sonnet_5_5", 0, with_custom_instructions=True
    )

    assert isinstance(client, CustomInstructionsClient)


async def test_an_activation_offers_the_deferred_tool_on_the_next_request() -> None:
    case = _case().model_copy(
        update={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "activate_tools",
                        "description": "Activate on-demand tools.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "deferred_tools": _case().tools,
        }
    )

    def _offered(args: MatcherArgs) -> list[str]:
        return [t["function"]["name"] for t in args.get("tools") or []]

    def _call(name: str, arguments: str) -> LLMOutput:
        return LLMOutput(
            tool_calls=[
                ToolCallItem(
                    id=f"call-{name}",
                    type="function",
                    function=ToolCallFunction(name=name, arguments=arguments),
                )
            ]
        )

    llm = RuleBasedMockLLMClient(
        rules=[
            (
                lambda args: any(
                    isinstance(m, ToolMessage) and m.name == "plan_trip"
                    for m in args["messages"]
                ),
                LLMOutput(content="Take the 17:10 T1."),
            ),
            (
                lambda args: "plan_trip" in _offered(args),
                _call("plan_trip", '{"origin": "Chatswood"}'),
            ),
        ],
        default_response=_call("activate_tools", '{"tool_names": ["plan_trip"]}'),
    )

    trial = await run_trial(llm, case, variant="shipped", model="mock")

    assert [c["name"] for c in trial.tool_calls] == ["activate_tools", "plan_trip"]
