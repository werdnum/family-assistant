"""The media handoff the OpenAI adapter suggests must actually exist.

When a provider cannot read an attachment, the adapter tells the model to
delegate it to a named profile. That instruction is a string in provider code
pointing at a profile in `defaults.yaml`, with nothing connecting the two, so a
rename or a provider change on the profile turns the instruction into advice the
model cannot follow. These tests are that connection.
"""

from family_assistant.config_loader import load_config
from family_assistant.config_models import ServiceProfile
from family_assistant.llm.messages import ImageUrlContentPart, UserMessage
from family_assistant.llm.providers.openai_client import OpenAIClient

_HANDOFF_PROFILE_ID = "media_analyst"


def _shipped_profile(profile_id: str) -> ServiceProfile:
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
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


def test_the_handoff_profile_has_no_tools() -> None:
    """It reads untrusted media, so it gets no sensitive data and no way to act.

    Rule of Two: with [A] untrustworthy input, it must hold neither [B] nor [C].
    """
    tools_policy = _shipped_profile(_HANDOFF_PROFILE_ID).tools_policy
    assert tools_policy is not None

    assert tools_policy.default_decision == "deny"
    assert tools_policy.rules == []
