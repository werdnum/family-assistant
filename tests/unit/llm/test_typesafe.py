"""TypeSafe's HTTP contract and unusable responses."""

import json

import httpx
import pytest

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
                    "tier": {"probabilities": {"deep": 1.0}},
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
