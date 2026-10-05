"""TypeSafe's HTTP contract and unusable responses."""

import json
import uuid

import httpx
import pytest
from prometheus_client import REGISTRY

from family_assistant.llm.call_context import (
    reset_processing_profile,
    set_processing_profile,
)
from family_assistant.llm.typesafe import (
    ChoiceQuestion,
    JevClient,
    NoulQuestion,
    TypeSafeError,
)


async def test_request_body_contains_model_state_and_typed_questions() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "followup": {"noul": 0.8},
                    "tier": {"probabilities": {"standard": 0.0, "deep": 1.0}},
                }
            },
        )

    client = JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(respond),
    )
    try:
        await client.ask(
            {"request": "Explain more"},
            {
                "followup": NoulQuestion(
                    "Continues?",
                    true_criteria="Same topic",
                    false_criteria="Other topic",
                ),
                "tier": ChoiceQuestion(
                    "Which tier?", options={"standard": "Everyday", "deep": "Complex"}
                ),
            },
        )
    finally:
        await client.close()

    assert captured[0].method == "POST"
    assert captured[0].url.path == "/v1/systemone"
    assert json.loads(captured[0].content) == {
        "model": "jev-test",
        "state": {"request": "Explain more"},
        "questions": {
            "followup": {
                "type": "noul",
                "instructions": "Continues?",
                "criteria": {"true": "Same topic", "false": "Other topic"},
            },
            "tier": {
                "type": "choice",
                "instructions": "Which tier?",
                "criteria": {"standard": "Everyday", "deep": "Complex"},
            },
        },
    }


async def test_request_authenticates_with_bearer_key() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"answers": {"q": {"noul": 0.5}}})

    client = JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(respond),
    )
    try:
        await client.ask("state", {"q": NoulQuestion("Relevant?")})
    finally:
        await client.close()

    assert captured[0].headers["Authorization"] == "Bearer test-key"


async def test_parses_answers_model_and_usage() -> None:
    client = JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": "jev-served",
                    "usage": {"input_tokens": 42},
                    "answers": {
                        "q": {"noul": 0.7},
                        "tier": {"probabilities": {"standard": 0.2, "deep": 0.8}},
                    },
                },
            )
        ),
    )
    try:
        answers = await client.ask(
            "state",
            {
                "q": NoulQuestion("Relevant?"),
                "tier": ChoiceQuestion(
                    "Tier?", options={"standard": None, "deep": None}
                ),
            },
        )
    finally:
        await client.close()

    assert answers.nouls == {"q": 0.7}
    assert answers.choices == {"tier": {"standard": 0.2, "deep": 0.8}}
    assert answers.model == "jev-served"
    assert answers.input_tokens == 42


@pytest.mark.parametrize("status", [429, 500])
async def test_http_failure_raises_typesafe_error(status: int) -> None:
    client = JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    try:
        with pytest.raises(TypeSafeError, match="Jev request failed"):
            await client.ask("state", {"q": NoulQuestion("Relevant?")})
    finally:
        await client.close()


async def test_non_json_response_raises_typesafe_error() -> None:
    client = JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not JSON")),
    )
    try:
        with pytest.raises(TypeSafeError, match="Jev request failed"):
            await client.ask("state", {"q": NoulQuestion("Relevant?")})
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("body", "message"), [({}, "no answers"), ({"answers": {}}, "no answer for 'q'")]
)
async def test_missing_answer_raises_typesafe_error(
    body: dict[str, object], message: str
) -> None:
    client = JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(TypeSafeError, match=message):
            await client.ask("state", {"q": NoulQuestion("Relevant?")})
    finally:
        await client.close()


def _client_answering(body: object, status: int = 200) -> JevClient:
    return JevClient(
        api_key="test-key",
        model="jev-test",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )


@pytest.mark.parametrize("value", [2, -0.1, True, "0.5"])
async def test_a_noul_answer_off_the_probability_scale_is_refused(
    value: object,
) -> None:
    client = _client_answering({"answers": {"q": {"noul": value}}})
    try:
        with pytest.raises(TypeSafeError):
            await client.ask("state", {"q": NoulQuestion("Is it?")})
    finally:
        await client.close()


async def test_a_choice_answer_off_the_probability_scale_is_refused() -> None:
    client = _client_answering({
        "answers": {"q": {"probabilities": {"a": 1.5, "b": -0.5}}}
    })
    try:
        with pytest.raises(TypeSafeError):
            await client.ask(
                "state", {"q": ChoiceQuestion("Which?", options={"a": None, "b": None})}
            )
    finally:
        await client.close()


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_a_call_is_counted_with_its_tokens_under_the_active_profile() -> None:
    profile = f"jev_profile_{uuid.uuid4().hex[:8]}"
    client = _client_answering({
        "model": "jev-1.13.0",
        "answers": {"q": {"noul": 0.4}},
        "usage": {"input_tokens": 120, "output_tokens": 3},
    })
    token = set_processing_profile(profile)
    try:
        await client.ask("state", {"q": NoulQuestion("Is it?")})
    finally:
        reset_processing_profile(token)
        await client.close()

    labels = {
        "profile": profile,
        "tier": "none",
        "provider": "typesafe",
        "model": "jev-test",
        "resolved_model": "jev-1.13.0",
        "operation": "classify",
    }
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "success", "error_type": ""},
        )
        == 1
    )
    assert (
        _sample(
            "family_assistant_llm_tokens_total", {**labels, "kind": "input_uncached"}
        )
        == 120
    )
    assert (
        _sample("family_assistant_llm_tokens_total", {**labels, "kind": "output"}) == 3
    )


async def test_a_failed_call_is_counted_as_an_error() -> None:
    profile = f"jev_profile_{uuid.uuid4().hex[:8]}"
    client = _client_answering({}, status=429)
    token = set_processing_profile(profile)
    try:
        with pytest.raises(TypeSafeError):
            await client.ask("state", {"q": NoulQuestion("Is it?")})
    finally:
        reset_processing_profile(token)
        await client.close()

    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {
                "profile": profile,
                "tier": "none",
                "provider": "typesafe",
                "model": "jev-test",
                "resolved_model": "jev-test",
                "operation": "classify",
                "outcome": "error",
                "error_type": "TypeSafeError",
            },
        )
        == 1
    )


@pytest.mark.parametrize(
    "probabilities",
    [{"a": 1.0}, {"a": 0.5, "b": 0.3, "c": 0.2}, {"a": 0.5, "z": 0.5}],
)
async def test_a_choice_answer_not_covering_exactly_the_offered_options_is_refused(
    probabilities: dict[str, float],
) -> None:
    client = _client_answering({"answers": {"q": {"probabilities": probabilities}}})
    try:
        with pytest.raises(TypeSafeError, match="offered options"):
            await client.ask(
                "state", {"q": ChoiceQuestion("Which?", options={"a": None, "b": None})}
            )
    finally:
        await client.close()
