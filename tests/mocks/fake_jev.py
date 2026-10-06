"""Controllable TypeSafe client for classifier tests."""

import asyncio
from collections.abc import Callable, Mapping

import httpx

from family_assistant.llm.typesafe import (
    ChoiceQuestion,
    JevAnswers,
    JevClient,
    NoulQuestion,
    TypeSafeError,
)


class FakeJevClient(JevClient):
    """Returns an answer (fixed, or computed by ``respond``), fails, or waits until
    the caller cancels it."""

    def __init__(
        self,
        answers: JevAnswers | None = None,
        *,
        fail: bool = False,
        stall: bool = False,
        respond: Callable[
            [str | Mapping[str, object], Mapping[str, NoulQuestion | ChoiceQuestion]],
            JevAnswers,
        ]
        | None = None,
    ) -> None:
        super().__init__(
            api_key="test-key",
            model="jev-test",
            timeout_seconds=1,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        self.answers = answers
        self.fail = fail
        self.stall = stall
        self.respond = respond
        self.gate = asyncio.Event()
        self.calls: list[
            tuple[
                str | Mapping[str, object], Mapping[str, NoulQuestion | ChoiceQuestion]
            ]
        ] = []

    async def ask(
        self,
        state: str | Mapping[str, object],
        questions: Mapping[str, NoulQuestion | ChoiceQuestion],
    ) -> JevAnswers:
        self.calls.append((state, questions))
        if self.stall:
            await self.gate.wait()
        if self.fail:
            raise TypeSafeError("provider unavailable")
        if self.respond is not None:
            return self.respond(state, questions)
        assert self.answers is not None
        return self.answers
