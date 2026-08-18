"""The media handoff the OpenAI adapter suggests must actually exist.

When a provider cannot read an attachment, the adapter tells the model to
delegate it to a named profile. That instruction is a string in provider code
pointing at a profile in `defaults.yaml`, with nothing connecting the two, so a
rename or a provider change on the profile turns the instruction into advice the
model cannot follow. These tests are that connection.
"""

import inspect
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

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
from family_assistant.llm.messages import (
    ImageUrlContentPart,
    TextContentPart,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.tools import (
    LOCAL_TOOL_DESCRIPTORS,
    PolicyEngine,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ToolAttachment

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

    `default_assistant` runs a Google primary that reads audio and video itself,
    but falls back to OpenAI, so it is the profile that will see the note on a
    fallback turn. Its policy denies `delegate_to_service` for several specific
    targets, and a deny landing on this one would leave the model told to do
    something its own policy forbids.
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


def test_shipped_config_admits_the_media_the_handoff_transcribes() -> None:
    """A type absent from the allowlist is rejected before any model sees it.

    `AttachmentRegistry` refuses an upload whose MIME type is not in
    `attachment_config.allowed_mime_types`, so the handoff can only ever run for
    types listed there. This was shipping without any `audio/*`: the code default
    in `attachment_registry.py` includes them, but `defaults.yaml` supplies the
    key and therefore wins, which made audio work under test and fail in
    production — the profile and its note were reachable only in theory.

    Telegram sends audio as `audio/mpeg` and voice notes as `audio/ogg`.
    """
    allowed = set(_load_defaults().attachment_config.allowed_mime_types)

    assert {"audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"} <= allowed
    assert {"video/mp4", "video/webm", "video/ogg"} <= allowed
    # The two the Responses API reads directly, for contrast: if these ever left
    # the list the OpenAI path would break rather than degrade to the handoff.
    assert {"image/png", "application/pdf"} <= allowed


# A test config replaces `global_tools_policy` wholesale, which strands the
# exclusions of every shipped profile that withholds a grant -- validation
# rejects an exclusion naming a tool nothing grants. These tests are about how
# one profile's exclusion is matched against the grants, not about narrowing the
# grant set, so they keep shipping the three global grants alongside whatever
# rule they are actually exercising.
_SHIPPED_GLOBAL_GRANTS = (
    "    - match:\n"
    "        names:\n"
    '          - "read_text_attachment"\n'
    '          - "jq_query"\n'
    '          - "report_technical_problem"\n'
    '      decision: "allow"\n'
    "      priority: 50\n"
)


def test_an_exclusion_that_withholds_nothing_is_rejected(tmp_path: Path) -> None:
    """A misspelled exclusion must not validate as a working security control.

    The generated matcher silently matches nothing, so the global grant the
    operator meant to remove stays active on a profile built to hold no
    privileges. Startup rejects it rather than accepting the insecure config.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "service_profiles:\n"
        '  - id: "media_analyst"\n'
        "    excluded_global_tools:\n"
        '      - "report_technical_problm"\n'
    )

    with pytest.raises(ValidationError, match="withholds nothing"):
        load_config(config_file_path=str(config_file), load_dotenv_file=False)


def test_two_overlapping_globs_are_not_rejected(tmp_path: Path) -> None:
    """A grant pattern and an exclusion pattern may overlap without either
    matching the other's literal text.

    `read_*` and `*_attachment` both match `read_text_attachment`, so the
    exclusion is effective, but neither `fnmatchcase` direction says so.
    Deciding whether two globs intersect is not worth doing to reach a stricter
    answer than "cannot tell", and refusing to start on a working config is the
    worse failure, so the pair is accepted.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "global_tools_policy:\n"
        "  rules:\n"
        "    - match:\n"
        "        names:\n"
        '          - "read_*"\n'
        '      decision: "allow"\n'
        "      priority: 50\n" + _SHIPPED_GLOBAL_GRANTS + "service_profiles:\n"
        '  - id: "media_analyst"\n'
        "    excluded_global_tools:\n"
        '      - "*_attachment"\n'
    )

    config = load_config(config_file_path=str(config_file), load_dotenv_file=False)

    profile = next(p for p in config.service_profiles if p.id == _HANDOFF_PROFILE_ID)
    assert profile.excluded_global_tools == ["*_attachment"]


def test_a_glob_exclusion_matching_no_grant_is_still_rejected(tmp_path: Path) -> None:
    """Leniency is limited to glob-vs-glob pairs.

    A pattern exclusion against concrete grants can still be shown to withhold
    nothing, so a typo in the pattern fails at startup as before.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "service_profiles:\n"
        '  - id: "media_analyst"\n'
        "    excluded_global_tools:\n"
        '      - "write_*"\n'
    )

    with pytest.raises(ValidationError, match="withholds nothing"):
        load_config(config_file_path=str(config_file), load_dotenv_file=False)


def test_an_exclusion_matching_only_a_global_deny_is_rejected(tmp_path: Path) -> None:
    """A denied name is not a granted one.

    Counting the names a `deny` rule matches as granted would let an exclusion
    that withholds nothing validate — the profile reads as having given up a tool
    the global policy never handed it, while whatever the policy does grant stays
    active. That is exactly the no-op this validator exists to reject.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "global_tools_policy:\n"
        "  rules:\n"
        "    - match:\n"
        "        names:\n"
        '          - "report_technical_problem"\n'
        '      decision: "allow"\n'
        "      priority: 50\n"
        "    - match:\n"
        "        names:\n"
        '          - "spawn_worker"\n'
        '      decision: "deny"\n'
        "      priority: 50\n"
        "service_profiles:\n"
        '  - id: "media_analyst"\n'
        "    excluded_global_tools:\n"
        '      - "spawn_worker"\n'
    )

    with pytest.raises(ValidationError, match="withholds nothing"):
        load_config(config_file_path=str(config_file), load_dotenv_file=False)


def test_an_exclusion_matching_a_confirm_rule_is_accepted(tmp_path: Path) -> None:
    """A tool reachable behind a confirmation is still reachable."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "global_tools_policy:\n"
        "  rules:\n"
        "    - match:\n"
        "        names:\n"
        '          - "spawn_worker"\n'
        '      decision: "confirm"\n'
        "      priority: 50\n" + _SHIPPED_GLOBAL_GRANTS + "service_profiles:\n"
        '  - id: "media_analyst"\n'
        "    excluded_global_tools:\n"
        '      - "spawn_worker"\n'
    )

    config = load_config(config_file_path=str(config_file), load_dotenv_file=False)

    profile = next(p for p in config.service_profiles if p.id == _HANDOFF_PROFILE_ID)
    assert profile.excluded_global_tools == ["spawn_worker"]


def _injected(mime_type: str, *, attachment_id: str | None = "att-7") -> UserMessage:
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-terra")
    attachment = ToolAttachment(
        mime_type=mime_type,
        content=b"\0" * 2048,
        description="From a tool",
        attachment_id=attachment_id,
    )
    return client.create_attachment_injection(attachment)


@pytest.mark.parametrize("mime_type", ["audio/ogg", "video/mp4", "application/pdf"])
def test_an_injected_media_attachment_is_described_in_text_too(mime_type: str) -> None:
    """The description has to survive a provider that drops the media part.

    `RetryingLLMClient.create_attachment_injection` delegates to the primary and
    hands the resulting message list to the fallback unchanged, so a
    cross-provider fallback renders a part built for the other provider. The text
    part is the only thing every adapter carries.
    """
    message = _injected(mime_type)

    text = next(
        part.text
        for part in message.content
        if isinstance(part, TextContentPart)  # pyright: ignore[reportUnnecessaryIsInstance]
    )
    assert mime_type in text
    assert "att-7" in text


def test_the_anthropic_fallback_still_learns_what_arrived() -> None:
    """End to end over the two adapters, since that is where this broke.

    Anthropic skips any data URI that is not an image, so before the text part
    carried the description the fallback saw only "a file arrived" -- less than
    the descriptive text this path produced before the branch changed it.
    """
    message = _injected("audio/ogg")

    blocks = AnthropicClient(
        api_key="test-key", model="claude-fable-5"
    )._convert_user_content(message)

    assert isinstance(blocks, list)
    rendered = " ".join(block["text"] for block in blocks if block["type"] == "text")
    assert "audio/ogg" in rendered
    assert "att-7" in rendered
    # The audio itself is gone -- that is the provider's limit, not a bug here.
    assert not any(block.get("type") == "image" for block in blocks)


def test_an_injected_image_keeps_the_plain_prelude() -> None:
    """Images are carried by every adapter, so they need no textual stand-in.

    Describing them anyway would change the request body for the one type that
    was never at risk, invalidating recordings for no gain.
    """
    message = _injected("image/png")

    text = next(
        part.text
        for part in message.content
        if isinstance(part, TextContentPart)  # pyright: ignore[reportUnnecessaryIsInstance]
    )
    assert text == "[System: File from previous tool response]"
