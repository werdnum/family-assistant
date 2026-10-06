"""Classifier API for the scripting engine, backed by TypeSafe's Jev.

``classify()`` picks among options with a probability for each, and
``classify_yes_no()`` returns the probability that a statement holds. Both
are fast, deterministic and cheap next to ``llm()``, and their answers can
only be the options the script offered. They need the deployment's TypeSafe
integration (``TYPESAFE_API_KEY``); without it they raise.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from family_assistant.llm.typesafe import ChoiceQuestion, NoulQuestion

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm.typesafe import JevClient

__all__ = ["ClassifierUnavailableError", "classify_async", "classify_yes_no_async"]


class ClassifierUnavailableError(RuntimeError):
    """The deployment has no TypeSafe integration configured."""

    def __init__(self) -> None:
        super().__init__(
            "classify() needs the TypeSafe integration, which this deployment "
            "has not configured; use llm_json() instead."
        )


def _state(state: str | Mapping[str, object]) -> str | dict[str, object]:
    if isinstance(state, str):
        return state
    return dict(state)


async def classify_async(
    client: JevClient | None,
    state: str | Mapping[str, object],
    question: str,
    options: Sequence[str] | Mapping[str, str | None],
) -> dict[str, object]:
    """Choose among *options* for *state*.

    *options* is a list of labels, or a mapping of label to a description of
    when it applies. Returns ``{"label": best, "probabilities": {label: p}}``.
    """
    if client is None:
        raise ClassifierUnavailableError
    criteria: dict[str, str | None] = (
        dict.fromkeys(options)
        if not isinstance(options, Mapping)
        else {str(label): rubric for label, rubric in options.items()}
    )
    if len(criteria) < 2:
        raise ValueError("classify() needs at least two options")
    answers = await client.ask(
        _state(state),
        {"answer": ChoiceQuestion(instructions=question, options=criteria)},
    )
    probabilities = answers.choices["answer"]
    return {
        "label": max(probabilities, key=lambda label: probabilities[label]),
        "probabilities": probabilities,
    }


async def classify_yes_no_async(
    client: JevClient | None,
    state: str | Mapping[str, object],
    question: str,
    yes: str | None = None,
    no: str | None = None,
) -> float:
    """The probability that *question* holds for *state*, from 0 to 1.

    *yes* and *no* optionally describe what each answer means.
    """
    if client is None:
        raise ClassifierUnavailableError
    answers = await client.ask(
        _state(state),
        {
            "answer": NoulQuestion(
                instructions=question, true_criteria=yes, false_criteria=no
            )
        },
    )
    return answers.nouls["answer"]
