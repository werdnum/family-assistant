"""The media handoff the OpenAI adapter suggests must actually exist.

When a provider cannot read an attachment, the adapter tells the model to
delegate it to a named profile. That instruction is a string in provider code
pointing at a profile in `defaults.yaml`, with nothing connecting the two, so a
rename or a provider change on the profile turns the instruction into advice the
model cannot follow. These tests are that connection.
"""

import inspect
from typing import cast

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the only helper that applies global_tools_policy injection, which is the whole point of the assertion
)
from family_assistant.config_loader import load_config
from family_assistant.config_models import (
    CONTEXT_PROVIDER_NAMES,
    AppConfig,
    ServiceProfile,
)
from family_assistant.context_providers import (
    CalendarContextProvider,
    ContextProvider,
    HomeAssistantContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
    WeatherContextProvider,
)
from family_assistant.llm.messages import ImageUrlContentPart, UserMessage
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.tools import (
    LOCAL_TOOL_DESCRIPTORS,
    PolicyEngine,
    ToolPolicyDecision,
)

_HANDOFF_PROFILE_ID = "media_analyst"
# The profile users send attachments to from Telegram and web chat.
_MEDIA_RECEIVING_PROFILE_ID = "default_assistant"


def _load_defaults() -> AppConfig:
    return load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )


def _shipped_profile(profile_id: str) -> ServiceProfile:
    config = _load_defaults()
    matching = [p for p in config.service_profiles if p.id == profile_id]
    assert matching, f"no shipped profile with id {profile_id!r}"
    return matching[0]


def _unreadable_media_note() -> str:
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-terra")
    items = client._messages_to_responses_input([
        UserMessage(
            content=[
                ImageUrlContentPart(
                    type="image_url",
                    image_url={"url": "data:audio/ogg;base64,aGVsbG8="},
                    attachment_id="att-1",
                )
            ]
        )
    ])
    content = items[0]["content"]
    assert isinstance(content, list)
    text = content[0]["text"]
    assert isinstance(text, str)
    return text


def test_the_profile_the_adapter_names_is_shipped() -> None:
    """The handoff target has to be a real profile to be delegated to."""
    assert _HANDOFF_PROFILE_ID in _unreadable_media_note()
    assert _shipped_profile(_HANDOFF_PROFILE_ID).id == _HANDOFF_PROFILE_ID


def test_the_handoff_profile_uses_a_provider_that_reads_audio_and_video() -> None:
    """Pointing the handoff at an OpenAI model would recreate the gap it exists for.

    Google is the only configured provider whose adapter represents audio and
    video at all.
    """
    processing_config = _shipped_profile(_HANDOFF_PROFILE_ID).processing_config
    assert processing_config is not None

    assert processing_config.provider == "google"
    # A retry_config would let it fall back to a provider that cannot read the
    # very media it was delegated, silently returning a description of nothing.
    assert processing_config.retry_config is None


def test_the_media_receiving_profile_may_delegate_to_the_handoff_profile() -> None:
    """The note is only an affordance if the profile reading it can make the call.

    `default_assistant` runs an OpenAI primary, so it is the profile that will
    actually see the note, and its policy denies `delegate_to_service` for
    several specific targets. A deny landing on this one would leave the model
    told to do something its own policy forbids.
    """
    profile = _shipped_profile(_MEDIA_RECEIVING_PROFILE_ID)
    assert profile.tools_policy is not None
    engine = PolicyEngine.from_policy_config(profile.tools_policy)
    descriptor = next(
        d for d in LOCAL_TOOL_DESCRIPTORS if d.name == "delegate_to_service"
    )

    evaluation = engine.evaluate(
        descriptor, arguments={"target_service_id": _HANDOFF_PROFILE_ID}
    )

    assert evaluation.decision == ToolPolicyDecision.ALLOW


def test_the_handoff_profile_can_reach_no_tool_that_acts() -> None:
    """It reads untrusted media, so it must have no way to act.

    Rule of Two: with [A] untrustworthy input, it must hold neither [B] nor [C].
    Asserted against the *effective* policy rather than the profile's own,
    because `global_tools_policy` rules are injected at the `profile` layer,
    which outranks the `defaults` layer the profile's own policy lands in — so
    an empty profile policy is not the same thing as no tools, and the profile
    cannot deny these at any priority.

    `excluded_global_tools` is what makes the deny effective, by denying in that
    same layer at a higher priority. All three globally granted tools are
    withheld: `read_text_attachment` and `jq_query` resolve any attachment id the
    acting user owns rather than only this turn's artifacts, so injected media
    naming an id could read private content [B]; `report_technical_problem`
    persists model-supplied text [C].

    Asserting the empty set rather than a list of tolerated names is the point —
    a tool added to the global policy has to fail this test rather than quietly
    reach a profile built to hold no privileges.
    """
    config = _load_defaults()
    profile = next(p for p in config.service_profiles if p.id == _HANDOFF_PROFILE_ID)
    engine = _build_profile_policy_engine(
        profile_id=profile.id,
        profile_tools_policy=profile.tools_policy,
        operator_tools_policy=None,
        global_tools_policy=config.global_tools_policy,
        excluded_global_tools=profile.excluded_global_tools,
    )

    reachable = {
        descriptor.name
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if engine.evaluate(descriptor).decision != ToolPolicyDecision.DENY
    }

    assert reachable == set()


def test_the_handoff_profile_gets_none_of_the_user_s_context() -> None:
    """Context providers inject private data into the system prompt by default.

    An empty tool policy does not deny [B] on its own: notes, calendar and
    known-users context is attached to every profile unless excluded. Pairing
    the user's notes with attacker-controlled audio in one prompt is the
    pairing the Rule of Two exists to prevent.
    """
    processing_config = _shipped_profile(_HANDOFF_PROFILE_ID).processing_config
    assert processing_config is not None

    assert set(processing_config.excluded_context_providers) == CONTEXT_PROVIDER_NAMES


def test_context_provider_names_match_config() -> None:
    """The validated name set has to match the providers that actually exist.

    A provider renamed on one side only would turn an exclusion into a silent
    no-op, which is the failure mode the exclusion exists to prevent.
    """
    provider_classes: tuple[type[ContextProvider], ...] = (
        NotesContextProvider,
        CalendarContextProvider,
        KnownUsersContextProvider,
        WeatherContextProvider,
        HomeAssistantContextProvider,
    )
    # `name` is a read-only property returning a literal on every provider, so
    # the value is available without building one -- these have constructors
    # wanting database handles, timezones and HTTP clients.
    live_names = {
        cast("str", inspect.getattr_static(provider_cls, "name").fget(provider_cls))
        for provider_cls in provider_classes
    }

    assert live_names == CONTEXT_PROVIDER_NAMES
