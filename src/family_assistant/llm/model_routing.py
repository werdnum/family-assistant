"""The Auto classifier: choosing a model tier for one request.

Auto is a routing policy over tiers, not a tier. A separate cheap request runs
before the turn, so the weaker execution model is not left to judge its own
adequacy, and its answer is a tier id and nothing else. The decision is made
once per turn and frozen for the run: providers differ in reasoning metadata,
tool representation and attachment handling, and the attachment injection is
built by the primary adapter, so a mid-run change of model would change what
the model can even see.

**A routing failure is visible, never a silent decision.** Nothing here raises:
a lost turn is worse than a weaker one, so a timeout, an unusable answer or a
provider error all return a :class:`RoutingDecision` saying so, and the caller
runs on the profile's configured tier with the outcome recorded beside the
resolved tier. A classifier outage then reads as an outage rather than as a run
of confident ``Auto -> Standard`` decisions.

The classifier reads the trusted trigger and bounded history under the
profile, and it is given no tools. Its answer is constrained to the profile's
``auto_model_tiers`` by the response schema itself, which is what makes a
prompt nudge ("think hard about this") a legitimate steering channel rather
than an escalation path: it cannot select outside the range Auto was already
authorized to choose from.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError, create_model

from family_assistant.llm.base import StructuredOutputError
from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.model_selection import (
    ResolvedModelSelection,
    RoutingOutcome,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.llm.model_selection import ModelTierEligibility

logger = logging.getLogger(__name__)

__all__ = [
    "MODEL_ROUTING_PROMPT_KEY",
    "ROUTER_CALL_SELECTION",
    "ROUTER_CALL_TIER",
    "ModelRouter",
    "ModelRoutingChoice",
    "RoutingDecision",
    "RoutingOutcome",
    "validate_routing_prompt_renders",
]

MODEL_ROUTING_PROMPT_KEY = "model_routing_prompt"
"""Where the classifier's instructions live in ``prompts.yaml``.

A prompt, not code, because the thresholds it describes are the product
decision this whole mechanism turns on -- and because an operator retuning them
should not need a release.
"""

_HISTORY_CHARS_PER_MESSAGE = 600
"""How much of one historical message the classifier sees.

The decision is about the shape of the request, not its detail, so a long tool
result contributes its opening rather than its whole body -- which also keeps
the classifier's own cost bounded by the window size rather than by whatever
the conversation happened to contain.
"""

_REQUEST_CHARS = 4000

ROUTER_CALL_TIER = "router"
"""Tier label the classifier's own call is counted under.

Routing is spend, and it is spend on the profile whose turn is about to run,
but it is not spend *at* that profile's tier: bucketing it under `standard`
would inflate the cheap tier by one small call per turn and hide the cost of
routing itself. One extra label value rather than one per tier, because there
is only ever one classifier."""

ROUTER_CALL_SELECTION = ResolvedModelSelection(
    tier=ROUTER_CALL_TIER,
    requested=None,
    source="auto",
    frozen=True,
)
"""The attribution envelope a classification is made under.

Not a run envelope -- nothing executes at this "tier" -- but the label carrier
the metrics and span attributes below the processing layer read, so that a
classifier call lands under the profile it was made for instead of under
whatever turn happened to be on the stack."""


class ModelRoutingChoice(BaseModel):
    """The classifier's answer. One field, because it decides one thing.

    The per-request subclass narrows ``tier`` to a ``Literal`` of the profile's
    automatic tiers, so the constraint is carried by the schema handed to the
    provider rather than by a check afterwards.
    """

    tier: str


@dataclass(frozen=True)
class RoutingDecision:
    """What Auto chose, or why it did not choose.

    ``tier`` is ``None`` for every outcome but ``decided``: an outcome that
    failed has no tier, and returning the default here would make the failure
    indistinguishable from a decision to stay put.
    """

    tier: str | None
    outcome: RoutingOutcome
    classifier_model: str
    latency_ms: int


def validate_routing_prompt_renders(prompt_template: str) -> None:
    """Render the classifier's instructions once, raising if the template is bad.

    Called at startup, for the same reason ``validate_system_prompt_renders``
    is: an operator prompt with a stray ``{`` renders nothing, and discovering
    that per turn would show up as a run of routing errors that look like a
    provider problem. The placeholder values are stand-ins -- any value
    exercises the same formatting.
    """
    try:
        prompt_template.format(
            tiers="(startup validation)", guidance="(startup validation)"
        )
    except (KeyError, IndexError, ValueError) as error:
        msg = (
            f"'{MODEL_ROUTING_PROMPT_KEY}' does not render: {error}. It is "
            "formatted with {tiers} and {guidance}; any other brace has to be "
            "doubled."
        )
        raise ValueError(msg) from error


def _routing_choice_model(tier_ids: Sequence[str]) -> type[ModelRoutingChoice]:
    """A response schema admitting exactly *tier_ids*."""
    tier_type: object = Literal[tuple(tier_ids)]
    return create_model(
        "ProfileModelRoutingChoice",
        __base__=ModelRoutingChoice,
        tier=(tier_type, ...),
    )


def _message_text(message: LLMMessage) -> str:
    """One history message as plain text, or empty if it carries none."""
    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            return message.content
        return " ".join(
            part.text for part in message.content if isinstance(part, TextContentPart)
        )
    if isinstance(message, AssistantMessage):
        return message.content or ""
    if isinstance(message, (ToolMessage, SystemMessage)):
        return message.content
    return ""


def _render_history(history: Sequence[LLMMessage]) -> str:
    lines: list[str] = []
    for message in history:
        text = _message_text(message).strip()
        if not text:
            continue
        lines.append(f"{message.role}: {text[:_HISTORY_CHARS_PER_MESSAGE]}")
    return "\n".join(lines)


def _render_tiers(eligibility: ModelTierEligibility) -> str:
    return "\n".join(
        f"- {option.id}: {option.label}"
        + (f" -- {option.description}" if option.description else "")
        for option in eligibility.auto_options
    )


class ModelRouter:
    """Runs the Auto classifier for whichever profile asks.

    One router serves every profile: what varies per request is the tier list
    and the profile's guidance, both of which arrive as arguments. Its client
    is built once at startup from ``model_routing.classifier`` and is never a
    profile's own -- routing must not cost what the turn itself costs.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        *,
        prompt_template: str,
        classifier_model: str,
        timeout_seconds: float,
        history_messages: int,
    ) -> None:
        self._llm_client = llm_client
        self._prompt_template = prompt_template
        self._classifier_model = classifier_model
        self._timeout_seconds = timeout_seconds
        self.history_messages = history_messages
        """How many recent messages a caller should read for :meth:`route`."""

    async def route(
        self,
        *,
        eligibility: ModelTierEligibility,
        guidance: str | None,
        history: Sequence[LLMMessage],
        request_text: str,
        attachment_summary: Sequence[str],
    ) -> RoutingDecision:
        """Choose a tier from *eligibility*'s automatic list. Never raises."""
        started = time.monotonic()
        tier_ids = [option.id for option in eligibility.auto_options]
        if not tier_ids:
            # Startup validation refuses an auto profile with nothing to choose
            # from, so reaching here means a caller routed a profile it should
            # not have. Say so rather than calling a model to pick from ().
            logger.error(
                "Model routing asked for a decision with no eligible tiers; "
                "skipping the classifier call."
            )
            return RoutingDecision(
                tier=None,
                outcome="invalid",
                classifier_model=self._classifier_model,
                latency_ms=self._elapsed_ms(started),
            )

        try:
            # Building the prompt is inside the try with the call it precedes:
            # an operator template that fails to render, or a history message
            # that fails to, is a routing failure like any other and must not
            # take the turn down with it.
            messages = self._build_messages(
                eligibility=eligibility,
                guidance=guidance,
                history=history,
                request_text=request_text,
                attachment_summary=attachment_summary,
            )
            choice = await asyncio.wait_for(
                self._llm_client.generate_structured(
                    messages, _routing_choice_model(tier_ids)
                ),
                timeout=self._timeout_seconds,
            )
            chosen_tier = choice.tier
        except TimeoutError:
            logger.warning(
                "Model routing timed out after %.1fs; the run continues on the "
                "profile's configured tier.",
                self._timeout_seconds,
            )
            return self._failed("timeout", started)
        except (StructuredOutputError, ValidationError):
            # The classifier answered, but not with one of the offered ids in
            # the shape asked for. That is a different problem from the
            # provider being unavailable, and a deployment seeing a lot of it
            # should look at the classifier model rather than at its network.
            logger.warning(
                "Model routing returned no usable choice among %s.",
                ", ".join(tier_ids),
            )
            return self._failed("invalid", started)
        except Exception:
            # Deliberately broad, and deliberately not re-raised: whatever the
            # provider -- or the prompt this deployment is configured with --
            # did, the turn it was asked about still has to run.
            logger.exception(
                "Model routing failed; the run continues on the profile's "
                "configured tier."
            )
            return self._failed("error", started)

        if chosen_tier not in tier_ids:
            logger.warning(
                "Model routing returned tier %r, which is not one of %s.",
                chosen_tier,
                ", ".join(tier_ids),
            )
            return self._failed("invalid", started)

        latency_ms = self._elapsed_ms(started)
        logger.info(
            "Model routing chose tier '%s' in %dms.",
            chosen_tier,
            latency_ms,
        )
        return RoutingDecision(
            tier=chosen_tier,
            outcome="decided",
            classifier_model=self._classifier_model,
            latency_ms=latency_ms,
        )

    def _build_messages(
        self,
        *,
        eligibility: ModelTierEligibility,
        guidance: str | None,
        history: Sequence[LLMMessage],
        request_text: str,
        attachment_summary: Sequence[str],
    ) -> list[LLMMessage]:
        """The classifier's whole conversation: instructions, then the request.

        The request and history go in a user message rather than into the
        rendered prompt so that the instructions stay one stable, cacheable
        block and the material being judged is plainly separated from it.
        """
        instructions = self._prompt_template.format(
            tiers=_render_tiers(eligibility),
            guidance=(guidance or "").strip() or "(no profile-specific guidance)",
        )
        sections = [
            f"<recent_conversation>\n{_render_history(history)}\n</recent_conversation>"
        ]
        if attachment_summary:
            sections.append(
                "<attachments>\n" + "\n".join(attachment_summary) + "\n</attachments>"
            )
        sections.append(f"<request>\n{request_text[:_REQUEST_CHARS]}\n</request>")
        return [
            SystemMessage(content=instructions),
            UserMessage(content="\n\n".join(sections)),
        ]

    def _failed(self, outcome: RoutingOutcome, started: float) -> RoutingDecision:
        return RoutingDecision(
            tier=None,
            outcome=outcome,
            classifier_model=self._classifier_model,
            latency_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.monotonic() - started) * 1000)
