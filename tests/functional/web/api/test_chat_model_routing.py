"""Auto routing through a whole turn: what it records, and what it changes.

Shadow mode is the shipped configuration, so most of this is about a decision
that changes nothing: the turn runs on the profile's configured tier and the
classifier's answer lands beside it in history, where the evaluation reads it.
The active-mode cases are the same turns with the flip applied, which is what
makes stage four a config change rather than another implementation.

The conditions for routing at all are the other half. An explicit selection
bypasses the classifier entirely, a profile that selects explicitly is never
routed, and a run replaying an envelope frozen at enqueue time is not routed
again -- each asserted by the classifier client having no calls, because "it
did not spend a model call" is the property, not "it happened to agree".
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, Protocol, cast

import pytest
from pydantic import BaseModel

from family_assistant.config_models import (
    AppConfig,
    ModelRoutingConfig,
    RetryModelConfig,
)
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.call_context import CallAttribution, attributed_call
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.model_routing import (
    ROUTER_CALL_TIER,
    ModelRouter,
    RoutingDecision,
)
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
    ResolvedModelSelection,
)
from family_assistant.observability.metrics import current_call_attribution
from family_assistant.processing import ProcessingService
from family_assistant.storage.database import Database
from family_assistant.utils.clock import SystemClock
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage, MessageReasoningInfo
    from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
        MatcherArgs,
        StructuredMatcherArgs,
    )

_ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(
        ModelTierOption(id="standard", label="Standard", description="Everyday work."),
        ModelTierOption(id="deep", label="Deep"),
        ModelTierOption(id="frontier", label="Max"),
    ),
    # `frontier` is a person's choice; Auto chooses between the other two.
    auto=frozenset({"standard", "deep"}),
)

_ROUTING_PROMPT = "Choose a tier from:\n{tiers}\nGuidance:\n{guidance}"


def _client_saying(reply: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=reply))


def _classifier_choosing(tier: str) -> RuleBasedMockLLMClient:
    def answer(args: StructuredMatcherArgs) -> BaseModel:
        return args["response_model"].model_validate({"tier": tier})

    return RuleBasedMockLLMClient(rules=[], structured_rules=[(lambda _: True, answer)])


def _capturing_classifier(
    captured: list[StructuredMatcherArgs],
) -> RuleBasedMockLLMClient:
    """A deciding classifier that also hands the test what it was asked."""

    def answer(args: StructuredMatcherArgs) -> BaseModel:
        captured.append(args)
        return args["response_model"].model_validate({"tier": "deep"})

    return RuleBasedMockLLMClient(rules=[], structured_rules=[(lambda _: True, answer)])


def _attribution_reading_classifier(
    seen: list[CallAttribution],
) -> RuleBasedMockLLMClient:
    """A classifier that records what its own call would be billed to.

    Read where a provider client reads it, so this is the attribution the
    metrics and the span below the processing layer would carry.
    """

    def answer(args: StructuredMatcherArgs) -> BaseModel:
        seen.append(current_call_attribution())
        return args["response_model"].model_validate({"tier": "deep"})

    return RuleBasedMockLLMClient(rules=[], structured_rules=[(lambda _: True, answer)])


def _prompt_text(args: StructuredMatcherArgs) -> str:
    return "\n".join(
        message.content
        for message in args["messages"]
        if isinstance(message.content, str)
    )


async def _add_history(
    db_engine: AsyncEngine,
    conversation_id: str,
    text: str,
    *,
    age: timedelta = timedelta(minutes=1),
    interface_type: str = "api",
) -> None:
    """Put one earlier user message in the conversation the classifier reads."""
    db = Database(engine=db_engine)
    await db.message_history.add_message(
        UserMessage(content=text),
        interface_type=interface_type,
        conversation_id=conversation_id,
        timestamp=SystemClock().now() - age,
        turn_id=str(uuid.uuid4()),
        processing_profile_id="chat_api_test_profile",
    )


type RoutingMode = Literal["off", "shadow", "active"]
type SelectionPolicy = Literal["explicit", "auto"]


def _app_config(mode: RoutingMode) -> AppConfig:
    return AppConfig(
        model_routing=ModelRoutingConfig(
            mode=mode,
            classifier=RetryModelConfig(provider="google", model="mock-classifier"),
        )
    )


@pytest.fixture(name="tier_clients")
def tier_clients_fixture() -> dict[str, RuleBasedMockLLMClient]:
    """One distinguishable client per tier, so the reply names what served it."""
    return {
        "standard": _client_saying("standard served this"),
        "deep": _client_saying("deep served this"),
        "frontier": _client_saying("frontier served this"),
    }


@pytest.fixture(name="classifier")
def classifier_fixture() -> RuleBasedMockLLMClient:
    """A classifier that always says `deep`, so a routed turn is unmistakable."""
    return _classifier_choosing("deep")


class RoutedServiceBuilder(Protocol):
    """Builds the shared profile as one Auto may route, in a given mode."""

    def __call__(
        self,
        mode: RoutingMode,
        *,
        model_selection: SelectionPolicy = "auto",
        classifier_client: LLMInterface | None = None,
        timeout_seconds: float = 5.0,
        history_messages: int = 6,
        router: ModelRouter | None = None,
    ) -> ProcessingService: ...


@pytest.fixture(name="build_routed_service")
def build_routed_service_fixture(
    api_test_processing_service: ProcessingService,
    tier_clients: dict[str, RuleBasedMockLLMClient],
    classifier: RuleBasedMockLLMClient,
) -> RoutedServiceBuilder:
    """Rebuilds the shared profile as one Auto may route.

    Rebuilt rather than mutated: the client a profile runs on is fixed when the
    service is built, which is what a resolved tier binds to.
    """

    def build(
        mode: RoutingMode,
        *,
        model_selection: SelectionPolicy = "auto",
        classifier_client: LLMInterface | None = None,
        timeout_seconds: float = 5.0,
        history_messages: int = 6,
        router: ModelRouter | None = None,
    ) -> ProcessingService:
        config = api_test_processing_service.service_config
        config.tier_eligibility = _ELIGIBILITY
        config.model_selection = model_selection
        config.auto_routing_guidance = "Reach for deep when evidence conflicts."
        router = router or ModelRouter(
            classifier_client if classifier_client is not None else classifier,
            prompt_template=_ROUTING_PROMPT,
            classifier_model="mock-classifier",
            timeout_seconds=timeout_seconds,
            history_messages=history_messages,
        )
        return ProcessingService(
            llm_client=tier_clients["standard"],
            tools_provider=api_test_processing_service.tools_provider,
            service_config=config,
            context_providers=api_test_processing_service.context_providers,
            server_url="http://testserver",
            app_config=_app_config(mode),
            attachment_registry=api_test_processing_service.attachment_registry,
            tier_llm_clients=tier_clients,
            model_router=router,
        )

    return build


@pytest.fixture(name="install_service")
def install_service_fixture(
    app_fixture: FastAPI,
    build_routed_service: RoutedServiceBuilder,
) -> RoutedServiceBuilder:
    """Put a freshly built routed service behind the chat API."""

    def install(
        mode: RoutingMode,
        *,
        model_selection: SelectionPolicy = "auto",
        classifier_client: LLMInterface | None = None,
        timeout_seconds: float = 5.0,
        history_messages: int = 6,
        router: ModelRouter | None = None,
    ) -> ProcessingService:
        service = build_routed_service(
            mode,
            model_selection=model_selection,
            classifier_client=classifier_client,
            timeout_seconds=timeout_seconds,
            history_messages=history_messages,
            router=router,
        )
        app_fixture.state.processing_service = service
        app_fixture.state.processing_services = {service.service_config.id: service}
        return service

    return install


async def _assistant_reasoning_info(
    db_engine: AsyncEngine, conversation_id: str, interface_type: str = "api"
) -> MessageReasoningInfo:
    db = Database(engine=db_engine)
    rows = await db.message_history.get_recent_with_metadata(
        interface_type=interface_type, conversation_id=conversation_id, limit=10
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert assistant_rows, "the turn persisted no assistant reply"
    reasoning = assistant_rows[-1]["reasoning_info"]
    assert reasoning is not None
    return cast("MessageReasoningInfo", reasoning)


async def _send(client: AsyncClient, conversation_id: str, **body: object) -> str:
    response = await client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "hello", "conversation_id": conversation_id, **body},
    )
    assert response.status_code == 200, response.text
    return cast("str", response.json()["reply"])


async def test_a_shadow_decision_is_recorded_while_the_turn_runs_on_the_default(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    install_service: RoutedServiceBuilder,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """The whole point of shadow mode: a decision nothing acted on."""
    install_service("shadow")
    conversation_id = f"routing-shadow-{uuid.uuid4()}"

    reply = await _send(api_test_client, conversation_id)

    assert reply == "standard served this"
    assert not tier_clients["deep"].get_calls()
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "standard"
    assert reasoning.get("model_tier_source") == "default"
    assert reasoning.get("model_tier_would_choose") == "deep"
    assert reasoning.get("model_tier_routing_outcome") == "decided"
    # The evaluation reads this column after the traces have expired, so the
    # decision has to say which model made it.
    assert reasoning.get("model_tier_classifier_model") == "mock-classifier"


async def test_the_same_turn_in_active_mode_runs_on_the_tier_auto_chose(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    install_service: RoutedServiceBuilder,
) -> None:
    """Stage four is this flip and nothing else."""
    install_service("active")
    conversation_id = f"routing-active-{uuid.uuid4()}"

    reply = await _send(api_test_client, conversation_id)

    assert reply == "deep served this"
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "deep"
    assert reasoning.get("model_tier_source") == "auto"
    assert reasoning.get("model_tier_routing_outcome") == "decided"
    # Nothing "would" have been chosen: it was chosen.
    assert "model_tier_would_choose" not in reasoning
    # And nobody requested it. The source says Auto; a turn detail rendering
    # "requested to resolved" must read `Auto -> Deep`, not `deep -> deep`.
    assert "model_tier_requested" not in reasoning


async def test_an_explicit_selection_never_reaches_the_classifier(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
    classifier: RuleBasedMockLLMClient,
) -> None:
    """A person has already answered the question the classifier exists for.

    Asserted on the classifier having made no call: routing that agreed with
    the user would be indistinguishable from routing that never ran.
    """
    install_service("active")

    reply = await _send(
        api_test_client, f"routing-explicit-{uuid.uuid4()}", model_tier="frontier"
    )

    assert reply == "frontier served this"
    assert not classifier.get_calls()


async def test_a_profile_that_selects_explicitly_is_never_routed(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
    classifier: RuleBasedMockLLMClient,
) -> None:
    """Auto is enabled per profile, so `explicit` has to actually mean it."""
    install_service("active", model_selection="explicit")

    reply = await _send(api_test_client, f"routing-off-profile-{uuid.uuid4()}")

    assert reply == "standard served this"
    assert not classifier.get_calls()


async def test_a_classifier_timeout_runs_the_turn_and_says_so(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    install_service: RoutedServiceBuilder,
) -> None:
    """A lost turn is worse than a weaker one -- but the outage stays visible.

    Without the recorded outcome, a classifier that is down reads as a run of
    confident `Auto -> Standard` decisions.
    """

    class StallingClassifier(RuleBasedMockLLMClient):
        async def generate_structured[T: BaseModel](
            self,
            messages: Sequence[LLMMessage],
            response_model: type[T],
            max_retries: int = 2,
        ) -> T:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    install_service(
        "active",
        classifier_client=StallingClassifier(rules=[]),
        timeout_seconds=0.05,
    )
    conversation_id = f"routing-timeout-{uuid.uuid4()}"

    reply = await _send(api_test_client, conversation_id)

    assert reply == "standard served this"
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "standard"
    assert reasoning.get("model_tier_source") == "default"
    assert reasoning.get("model_tier_routing_outcome") == "timeout"


async def test_routing_happens_once_per_turn_not_once_per_iteration(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
    classifier: RuleBasedMockLLMClient,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """The resolved tier is frozen for the run, so there is one decision to make.

    Re-deciding mid-loop would also change provider mid-turn, and the
    attachment injection and tool representation were built by the first one.
    """

    def before_tool_result(args: MatcherArgs) -> bool:
        return not any(message.role == "tool" for message in args.get("messages", []))

    tier_clients["deep"].rules.append((
        before_tool_result,
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="note_call_1",
                    type="function",
                    function=ToolCallFunction(
                        name="add_or_update_note",
                        arguments=json.dumps({"title": "T", "content": "C"}),
                    ),
                )
            ],
        ),
    ))
    install_service("active")

    reply = await _send(api_test_client, f"routing-loop-{uuid.uuid4()}")

    assert reply == "deep served this"
    # Two turns of the tool loop against the tier client, one classification.
    assert len(tier_clients["deep"].get_calls()) >= 2
    assert len(classifier.get_calls()) == 1


async def test_a_delegation_that_named_no_tier_is_routed(
    db_engine: AsyncEngine,
    build_routed_service: RoutedServiceBuilder,
    classifier: RuleBasedMockLLMClient,
) -> None:
    """Each agent boundary resolves its own tier; a parent's silence is not a choice.

    `delegate_to_service` resolves against the target's eligibility before the
    run exists, producing a `default`-sourced envelope. That is the absence of
    a selection, so the target routes it.
    """
    service = build_routed_service("active")
    db = Database(engine=db_engine)

    result = await service.handle_chat_interaction(
        db_context=db,
        interface_type="delegation",
        conversation_id=f"routing-delegated-{uuid.uuid4()}",
        trigger_content_parts=[{"type": "text", "text": "look into this"}],
        trigger_interface_message_id=None,
        user_name="tester",
        model_selection=ResolvedModelSelection.unselected("standard"),
    )

    assert result.text_reply == "deep served this"
    assert len(classifier.get_calls()) == 1


async def test_a_queued_run_replays_its_envelope_rather_than_routing_again(
    db_engine: AsyncEngine,
    build_routed_service: RoutedServiceBuilder,
    classifier: RuleBasedMockLLMClient,
) -> None:
    """The envelope was frozen when the run was created, and stays frozen.

    Routing at execution time would decide the models of an already-authorized
    run from whatever the deployment looks like whenever a worker got to it --
    exactly the drift persisting the envelope exists to prevent. Nothing here
    tells the service not to route: the envelope came out of storage, and that
    is what says so.
    """
    service = build_routed_service("active")
    db = Database(engine=db_engine)

    result = await service.handle_chat_interaction(
        db_context=db,
        interface_type="delegation",
        conversation_id=f"routing-queued-{uuid.uuid4()}",
        trigger_content_parts=[{"type": "text", "text": "look into this"}],
        trigger_interface_message_id=None,
        user_name="tester",
        model_selection=ResolvedModelSelection.from_json({
            "tier": "standard",
            "requested": None,
            "source": "default",
        }),
    )

    assert result.text_reply == "standard served this"
    assert not classifier.get_calls()


async def test_shadow_mode_leaves_the_tier_the_run_arrived_with(
    db_engine: AsyncEngine,
    build_routed_service: RoutedServiceBuilder,
) -> None:
    """Shadow mode records; it does not decide, and it does not undecide.

    Substituting a fresh default here would throw away the envelope the run
    arrived with -- which is the one thing shadow mode must never touch.
    """
    service = build_routed_service("shadow")
    db = Database(engine=db_engine)

    result = await service.handle_chat_interaction(
        db_context=db,
        interface_type="delegation",
        conversation_id=f"routing-shadow-envelope-{uuid.uuid4()}",
        trigger_content_parts=[{"type": "text", "text": "look into this"}],
        trigger_interface_message_id=None,
        user_name="tester",
        model_selection=ResolvedModelSelection(
            tier="frontier", requested=None, source="default"
        ),
    )

    assert result.text_reply == "frontier served this"
    reasoning = result.reasoning_info
    assert reasoning is not None
    assert reasoning.get("model_tier") == "frontier"
    assert reasoning.get("model_tier_would_choose") == "deep"


async def test_a_decision_the_admission_gate_refuses_is_not_acted_on(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    install_service: RoutedServiceBuilder,
) -> None:
    """The classifier is offered the same list the gate enforces -- until it is not.

    A profile reconfigured between the two is the case this covers, modelled by
    a router that answers off-list. A decision the gate refuses is a
    classification this deployment cannot use, not a licence to spend at a tier
    Auto may not reach.
    """

    class OffListRouter(ModelRouter):
        async def route(self, **kwargs: object) -> RoutingDecision:
            _ = kwargs
            return RoutingDecision(
                tier="frontier",
                outcome="decided",
                classifier_model="mock-classifier",
                latency_ms=1,
            )

    install_service(
        "active",
        router=OffListRouter(
            _classifier_choosing("deep"),
            prompt_template=_ROUTING_PROMPT,
            classifier_model="mock-classifier",
            timeout_seconds=5.0,
            history_messages=6,
        ),
    )
    conversation_id = f"routing-refused-{uuid.uuid4()}"

    reply = await _send(api_test_client, conversation_id)

    assert reply == "standard served this"
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "standard"
    assert reasoning.get("model_tier_routing_outcome") == "invalid"


async def test_a_history_window_of_zero_shows_the_classifier_no_history(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    install_service: RoutedServiceBuilder,
) -> None:
    """Zero has to skip the read, not be passed down.

    The repository treats a falsy limit as *no* limit, so a deployment asking
    for no history would otherwise hand the classifier the whole window.
    """
    conversation_id = f"routing-no-history-{uuid.uuid4()}"
    await _add_history(
        db_engine, conversation_id, "the settlement figure we discussed earlier"
    )
    captured: list[StructuredMatcherArgs] = []
    install_service(
        "shadow", classifier_client=_capturing_classifier(captured), history_messages=0
    )

    await _send(api_test_client, conversation_id)

    assert len(captured) == 1
    assert "the settlement figure we discussed earlier" not in _prompt_text(captured[0])


async def test_the_classifier_reads_history_no_older_than_the_interface_allows(
    db_engine: AsyncEngine,
    build_routed_service: RoutedServiceBuilder,
) -> None:
    """The classifier's own message count, but the interface's configured age.

    A message too old for the turn itself to see is not context for what is
    being asked now, and reading it on one surface but not another would route
    the same conversation differently depending on where it was typed.
    """
    captured: list[StructuredMatcherArgs] = []
    service = build_routed_service(
        "shadow", classifier_client=_capturing_classifier(captured)
    )
    service.service_config.history_max_age_hours = 24.0
    service.service_config.web_history_max_age_hours = 0.5
    conversation_id = f"routing-age-{uuid.uuid4()}"
    await _add_history(
        db_engine,
        conversation_id,
        "an hour-old remark",
        age=timedelta(hours=1),
        interface_type="telegram",
    )
    await _add_history(
        db_engine,
        conversation_id,
        "an hour-old remark",
        age=timedelta(hours=1),
        interface_type="web",
    )
    db = Database(engine=db_engine)

    for interface_type in ("telegram", "web"):
        await service.handle_chat_interaction(
            db_context=db,
            interface_type=interface_type,
            conversation_id=conversation_id,
            trigger_content_parts=[{"type": "text", "text": "and now?"}],
            trigger_interface_message_id=None,
            user_name="tester",
        )

    telegram_prompt, web_prompt = (_prompt_text(args) for args in captured)
    assert "an hour-old remark" in telegram_prompt
    assert "an hour-old remark" not in web_prompt


async def test_the_streaming_path_shows_the_classifier_the_request_once(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
) -> None:
    """`POST /chat/turns` commits the prompt before the producer runs.

    Without excluding the turn's own row the classifier reads the request twice
    -- once as history, once as the request it is judging -- and a repeated
    request is not the same evidence as a fresh one.
    """
    captured: list[StructuredMatcherArgs] = []
    install_service("shadow", classifier_client=_capturing_classifier(captured))
    conversation_id = f"routing-stream-{uuid.uuid4()}"
    prompt = "weigh these two quotes against each other"

    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "prompt": prompt,
        },
    )
    assert post.status_code == 200
    stream = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )
    assert stream.status_code == 200

    assert len(captured) == 1
    assert _prompt_text(captured[0]).count(prompt) == 1


async def test_the_classifier_call_is_billed_to_the_profile_it_routes_for(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
) -> None:
    """Routing is the profile's spend, but not spend at the tier it routes to.

    Attributed here rather than left to whatever turn is on the stack: the
    binding has not happened yet when the classifier runs, so an unattributed
    call is what the metrics would otherwise record.
    """
    seen: list[CallAttribution] = []
    install_service("shadow", classifier_client=_attribution_reading_classifier(seen))

    await _send(api_test_client, f"routing-attribution-{uuid.uuid4()}")

    assert len(seen) == 1
    assert seen[0].profile_id == "chat_api_test_profile"
    assert seen[0].model_selection is not None
    assert seen[0].model_selection.tier == ROUTER_CALL_TIER


async def test_a_child_profiles_classifier_is_not_billed_to_its_parent(
    db_engine: AsyncEngine,
    build_routed_service: RoutedServiceBuilder,
) -> None:
    """A delegation routes for the target, so the target pays for the routing.

    Under the parent's attribution -- which is what is in effect when a
    synchronous delegation runs -- the child's classifier would otherwise land
    in the parent's column.
    """
    seen: list[CallAttribution] = []
    service = build_routed_service(
        "shadow", classifier_client=_attribution_reading_classifier(seen)
    )
    db = Database(engine=db_engine)

    with attributed_call(CallAttribution(profile_id="delegating_parent")):
        await service.handle_chat_interaction(
            db_context=db,
            interface_type="delegation",
            conversation_id=f"routing-child-{uuid.uuid4()}",
            trigger_content_parts=[{"type": "text", "text": "look into this"}],
            trigger_interface_message_id=None,
            user_name="tester",
            model_selection=ResolvedModelSelection.unselected("standard"),
        )

    assert len(seen) == 1
    assert seen[0].profile_id == "chat_api_test_profile"


async def _listed_model_selection(client: AsyncClient) -> str:
    response = await client.get("/api/v1/profiles")
    assert response.status_code == 200
    profiles = {profile["id"]: profile for profile in response.json()["profiles"]}
    return cast("str", profiles["chat_api_test_profile"]["model_selection"])


async def test_the_profile_listing_offers_auto_once_auto_decides_something(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
) -> None:
    """The contract a composer's intelligence control reads to show `Auto`."""
    install_service("active")

    assert await _listed_model_selection(api_test_client) == "auto"


async def test_the_profile_listing_reports_shadow_mode_as_explicit(
    api_test_client: AsyncClient,
    install_service: RoutedServiceBuilder,
) -> None:
    """What the deployment does, not what the profile asked for.

    A profile configured `auto` under shadow mode still runs every request on
    its configured tier, so offering the user an Auto control there would offer
    one that changes nothing.
    """
    install_service("shadow")

    assert await _listed_model_selection(api_test_client) == "explicit"
