"""Media input against the models this repo actually ships.

The provider matrix in `test_providers.py` runs `gpt-4.1-nano`,
`gemini-2.5-flash-lite` and `claude-haiku-4-5` -- none of which any profile
uses. It proves the adapters work; it does not prove they work for the models
production calls. These tests cover the shipped tier on the paths this repo
changed most recently: the per-MIME Responses conversion, and the Gemini
capability the `media_analyst` handoff depends on.

Every test here gates on the record mode rather than on `CI` plus a key. The
committed cassette replays without a credential, so gating on the key would
mean these assertions never actually run in CI -- which is where they matter,
since a local run is not what merges.
"""

import base64
import os
import pathlib
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import ImageUrlContentPart, TextContentPart
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolAttachment
from tests.factories.messages import create_user_message

from .vcr_helpers import sanitize_response

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import ContentPart, LLMMessage
    from family_assistant.tools.types import ToolDefinition

# The default assistant's fallback. Reads images and PDFs; cannot take audio or
# video in any form, which is what the handoff note exists for.
_SHIPPED_OPENAI_MODEL = "gpt-5.6-terra"
# The default assistant's primary and the media_analyst provider, and the only
# configured one whose adapter represents audio at all.
_SHIPPED_GOOGLE_MODEL = "gemini-3.7-flash"

_CALCULATE_TOOL: "ToolDefinition" = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
}

_CASSETTE_DIR = pathlib.Path("tests/cassettes/llm")

_PDF = pathlib.Path(__file__).parent.parent.parent / "data" / "test_doc.pdf"
# A short real recording rather than synthesised silence: an empty track proves
# nothing about whether the provider accepted audio it could work with.
_AUDIO = (
    pathlib.Path(__file__).parents[3]
    / "src"
    / "family_assistant"
    / "web"
    / "resources"
    / "greeting.wav"
)


def _client(provider: str, model: str, env_var: str) -> "LLMInterface":
    return LLMClientFactory.create_client({
        "provider": provider,
        "model": model,
        "api_key": os.getenv(env_var, f"test-{provider}-key"),
    })


def _skip_unless_replayable(llm_record_mode: str, env_var: str) -> None:
    if llm_record_mode != "replay" and not os.getenv(env_var):
        pytest.skip(f"Recording this test requires {env_var}")


@pytest.fixture
def _require_cassette(request: pytest.FixtureRequest, llm_record_mode: str) -> None:
    """Fail with the missing cassette's path rather than a connection error.

    In replay mode CI has no network, so a VCR test with no recording fails with
    `ProviderConnectionError: Connection error` -- or worse, the client swallows
    it and a later assertion fails for an unrelated-looking reason. Neither names
    the file to record. Gemini's SDK replay reports the missing id itself, so this
    only guards the VCR-backed tests, which request it explicitly.
    """
    if llm_record_mode != "replay":
        return
    cassette = _CASSETTE_DIR / f"{request.node.name}.yaml"
    if not cassette.exists():
        pytest.fail(
            f"Cassette missing at {cassette}. Record with LLM_RECORD_MODE=record."
        )


def _audio_data_uri() -> str:
    return "data:audio/wav;base64," + base64.b64encode(_AUDIO.read_bytes()).decode()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("_require_cassette")
async def test_shipped_openai_model_reads_an_injected_pdf(
    llm_record_mode: str,
) -> None:
    """A PDF sent down the injection path must reach the model as a file.

    This is the path web chat uses for every non-image upload: `chat_api.py`
    routes it through `attachment_content`, which lands in
    `create_attachment_injection` rather than the chat content-part path. It used
    to substitute `[PDF Document: ... cannot be displayed]` placeholder text on a
    model that reads PDFs natively, so the document was silently discarded.

    Asserting on the document's actual subject is what distinguishes a real read
    from a plausible guess off the filename.
    """
    _skip_unless_replayable(llm_record_mode, "OPENAI_API_KEY")
    client = _client("openai", _SHIPPED_OPENAI_MODEL, "OPENAI_API_KEY")

    injection = client.create_attachment_injection(
        ToolAttachment(
            mime_type="application/pdf",
            content=_PDF.read_bytes(),
            attachment_id="att-pdf-1",
            description="Document from search results",
        )
    )
    messages: list[LLMMessage] = [
        injection,
        create_user_message("What is this document about? Answer in one sentence."),
    ]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content
    assert any(
        keyword in response.content.lower()
        for keyword in ("update", "software", "security", "patch", "vulnerability")
    ), f"model did not read the PDF; it replied: {response.content!r}"


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("_require_cassette")
async def test_shipped_openai_model_accepts_the_unreadable_media_note(
    llm_record_mode: str,
) -> None:
    """An audio attachment must leave the turn sendable rather than malformed.

    The Responses API accepts no audio in any form. Before the per-MIME
    conversion every non-text part became an `input_image`, so a voice note was
    sent as a malformed image and the API rejected or misread it. The adapter now
    substitutes a text note naming the attachment.

    The assertion is deliberately about *acceptance*, not wording: that the
    request completes at all is the regression, and what the model then says
    about the note is not something a test should pin. The note's exact text and
    the delegation instruction it carries are covered by unit tests.
    """
    _skip_unless_replayable(llm_record_mode, "OPENAI_API_KEY")
    client = _client("openai", _SHIPPED_OPENAI_MODEL, "OPENAI_API_KEY")

    parts: list[ContentPart] = [
        TextContentPart(type="text", text="What does this recording say?"),
        ImageUrlContentPart(
            type="image_url",
            image_url={"url": _audio_data_uri()},
            attachment_id="att-audio-1",
        ),
    ]
    messages: list[LLMMessage] = [create_user_message(parts)]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_media_analyst_provider_can_actually_read_audio(
    request: pytest.FixtureRequest,
    llm_record_mode: str,
) -> None:
    """The handoff target has to be able to do the job it exists for.

    `test_shipped_media_handoff.py` proves `media_analyst` is configured, points
    at Google and holds no privileges. None of that establishes that the provider
    accepts audio -- and the whole handoff is pointless if it does not. Pinning
    the model here means a future retier of that profile has to keep a provider
    that can take audio, or fail.

    Uses the genai SDK's own replay rather than VCR: Gemini recordings are JSON
    under `tests/cassettes/gemini/`, keyed by module/test/mldev.
    """
    _skip_unless_replayable(llm_record_mode, "GEMINI_API_KEY")

    module_name = request.node.module.__name__.replace("tests.", "")
    client = GoogleGenAIClient(
        api_key=os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        model=_SHIPPED_GOOGLE_MODEL,
        debug_config={
            "client_mode": llm_record_mode,
            "replay_id": f"{module_name}/{request.node.name}/mldev",
            "replays_directory": "tests/cassettes/gemini",
        },
    )
    parts: list[ContentPart] = [
        TextContentPart(
            type="text",
            text="Transcribe this audio. If any of it is inaudible, say so.",
        ),
        ImageUrlContentPart(type="image_url", image_url={"url": _audio_data_uri()}),
    ]

    try:
        response = await client.generate_response([create_user_message(parts)])
    finally:
        await client.close()

    assert isinstance(response, LLMOutput)
    assert response.content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("_require_cassette")
async def test_shipped_openai_model_calls_a_tool(llm_record_mode: str) -> None:
    """Agentic tool calling on the default assistant's primary.

    Moving `default_assistant` off Gemini was motivated entirely by tool-calling
    strength, and nothing exercised that model. A basic single call is a shallow
    assertion, but it is the difference between the shipped primary being covered
    and being assumed.
    """
    _skip_unless_replayable(llm_record_mode, "OPENAI_API_KEY")
    client = _client("openai", _SHIPPED_OPENAI_MODEL, "OPENAI_API_KEY")

    response = await client.generate_response(
        [create_user_message("What is 42 times 17? Use the calculate tool.")],
        tools=[_CALCULATE_TOOL],
        tool_choice="auto",
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls, "expected the model to call the tool"
    assert response.tool_calls[0].function.name == "calculate"


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("_require_cassette")
async def test_shipped_default_retry_pair_completes_a_turn(
    llm_record_mode: str,
) -> None:
    """The pair `default_assistant` actually runs, in the order it runs them.

    `test_retry_fallback.py` covers nano/2.5-flash-lite/5.2 pairings that no
    profile uses. This is Terra primary with a Gemini fallback -- the shipped
    chain, and the one whose cross-provider hop is the known soft spot.
    """
    _skip_unless_replayable(llm_record_mode, "OPENAI_API_KEY")
    if llm_record_mode != "replay" and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Recording this test requires GEMINI_API_KEY")

    client = LLMClientFactory.create_client({
        "retry_config": {
            "primary": {
                "provider": "openai",
                "model": _SHIPPED_OPENAI_MODEL,
                "api_key": os.getenv("OPENAI_API_KEY", "test-openai-key"),
            },
            "fallback": {
                "provider": "google",
                "model": _SHIPPED_GOOGLE_MODEL,
                "api_key": os.getenv("GEMINI_API_KEY", "test-gemini-key"),
                "api_base": "https://generativelanguage.googleapis.com/v1beta",
            },
        }
    })

    response = await client.generate_response([
        create_user_message("Reply with exactly: 'Primary response received'")
    ])

    assert response.content
