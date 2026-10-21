"""The shipped `coder` profile, run against the real Antigravity agent.

What these pin is what the backend reports it ran, not what our code meant to
send: a delegated run at a tier must be *reasoned with* that tier's model, and
the agent must receive the profile's own prompt rather than the routing blurb
written for whoever picks a profile. Both are read back from the finished
interaction, which echoes the agent configuration and system instruction it
actually used.

The profile comes from the shipped configuration through the public startup
path, so a regression anywhere between `defaults.yaml` and the request --
tier resolution, the per-tier client, the frozen selection reaching submit,
prompt assembly -- shows up here.
"""

import hashlib
import json
import os
import random
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from google.genai.interactions import AntigravityAgentConfig, Interaction
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.llm.content_parts import ContentPartDict
from family_assistant.llm.model_selection import ModelSelectionRequest
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_interaction_terminal_error_status,
)
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
)
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database
from tests.helpers import wait_for_condition

from .vcr_helpers import sanitize_response

_CURRENT_TIME = re.compile(r"Current time: [^\n\"]*")
_UPLOAD_PLACEHOLDER = re.compile(rb"<\d+ bytes, sha256 [0-9a-f]{64}>")


def _pin_prompt_clock(request: Any) -> Any:  # noqa: ANN401 # VCR request objects are dynamically typed
    """Make the clock the service folds into the prompt match across runs.

    Applied by VCR to a request both before recording it and before matching
    it, so the recorded and the replayed prompt agree on the time. A file
    upload's bytes are reduced to their length and digest the same way, which
    keeps a megabyte out of the cassette while replay still checks exactly
    those bytes were sent.
    """
    if not request.body:
        return request
    if request.headers.get("X-Goog-Upload-Command") == "upload, finalize":
        content = (
            request.body if isinstance(request.body, bytes) else request.body.encode()
        )
        # VCR applies this to the same request more than once while matching.
        if not _UPLOAD_PLACEHOLDER.fullmatch(content):
            digest = hashlib.sha256(content).hexdigest()
            request.body = f"<{len(content)} bytes, sha256 {digest}>".encode()
        return request
    body = json.loads(request.body)
    instruction = body.get("system_instruction")
    if isinstance(instruction, str):
        body["system_instruction"] = _CURRENT_TIME.sub(
            "Current time: <pinned>", instruction
        )
        request.body = json.dumps(body).encode()
    return request


def _sanitize_keeping_upload_sessions(
    # ast-grep-ignore: no-dict-any - VCR response matches external HTTP response format
    response: dict[str, Any],
    # ast-grep-ignore: no-dict-any - VCR response matches external HTTP response format
) -> dict[str, Any] | None:
    """``sanitize_response``, except an upload handshake is kept.

    The handshake that opens a file upload answers with an empty body -- the
    session URL is in a header -- and ``sanitize_response`` drops empty bodies
    as duplicates, which would leave replay unable to start the upload.
    """
    headers = {name.lower() for name in response.get("headers", {})}
    if "x-goog-upload-url" in headers:
        return response
    return sanitize_response(response)


@pytest.fixture(name="vcr_config")
def vcr_config_fixture(vcr_config: dict[str, object]) -> dict[str, object]:
    return {**vcr_config, "before_record_request": _pin_prompt_clock}


@pytest.fixture(name="coder")
async def coder_fixture(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[InteractionsAgentProcessingService]:
    """The `coder` service exactly as the shipped configuration builds it."""
    # Replay needs no real key, and the cassette holds none; startup builds a
    # client for every provider the shipped tiers name, so each needs a value.
    monkeypatch.setenv("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", "test-gemini-key"))
    for env_var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(env_var, os.getenv(env_var, f"test-{env_var.lower()}"))
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml", load_dotenv_file=False
    )
    config.service_profiles = [p for p in config.service_profiles if p.id == "coder"]
    config.default_service_profile_id = "coder"
    config.telegram_enabled = False
    config.mcp_config.mcpServers = {}
    config.embedding_model = "mock-deterministic-embedder"
    config.embedding_dimensions = 10

    assistant = Assistant(config=config, database_engine=db_engine)
    try:
        await assistant.setup_dependencies()
        service = assistant.processing_services_registry["coder"]
        assert isinstance(service, InteractionsAgentProcessingService)
        yield service
    finally:
        await assistant.stop_services()


async def _run_to_completion(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    *,
    model_tier: str | None,
    llm_record_mode: str,
    content_parts: list[ContentPartDict] | None = None,
    acting_user_id: str | None = None,
    expected_output: str = "39287",
) -> Interaction:
    selection = (
        coder.resolve_model_selection(
            ModelSelectionRequest(tier=model_tier, source="model")
        )
        if model_tier is not None
        else None
    )
    submission = await coder.submit_async(
        content_parts
        or [{"type": "text", "text": "Run Python to print 17 * 2311, then stop."}],
        conversation_id="coder-integration",
        subconversation_id=None,
        user_name="Tester",
        db_context=Database(db_engine),
        acting_user_id=acting_user_id,
        model_selection=selection,
    )
    # Reading a run back goes by its id alone, which any of the profile's
    # clients serves.
    client = coder.llm_client
    assert isinstance(client, GoogleGenAIClient)
    interaction = await client.get_agent_interaction(submission.remote_task_id)

    async def finished() -> bool:
        nonlocal interaction
        interaction = await client.get_agent_interaction(submission.remote_task_id)
        return (
            interaction.status == "completed"
            or is_interaction_terminal_error_status(interaction.status)
        )

    try:
        await wait_for_condition(
            finished,
            timeout=600.0,
            interval=0.01 if llm_record_mode == "replay" else 15.0,
        )
    finally:
        if interaction.status == "in_progress":
            await client.cancel_agent_interaction(submission.remote_task_id)
    assert interaction.status == "completed"
    assert expected_output in (interaction.output_text or "")
    return interaction


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    ("model_tier", "reasoning_model"),
    [(None, "gemini-3.8-flash"), ("gemini_4_argon", "gemini-4-argon")],
    ids=["default", "argon"],
)
async def test_a_delegated_run_is_reasoned_with_its_tiers_model(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    llm_record_mode: str,
    model_tier: str | None,
    reasoning_model: str,
) -> None:
    """The default is Flash; a delegation that asks for Argon is run on Argon."""
    interaction = await _run_to_completion(
        coder, db_engine, model_tier=model_tier, llm_record_mode=llm_record_mode
    )

    assert isinstance(interaction.agent_config, AntigravityAgentConfig)
    assert interaction.agent_config.model == reasoning_model


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_the_agent_receives_the_profiles_prompt_not_its_routing_blurb(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    llm_record_mode: str,
) -> None:
    """The description tells a *caller* when to pick coder; the agent never sees it.

    It says "choose it over spawn_worker" in the second person, which the agent
    would read as addressed to itself, offering alternatives it cannot reach.
    The prompt also arrives as a first-class system instruction, so it carries
    no `System:` marker of the kind the inline paths need.
    """
    interaction = await _run_to_completion(
        coder, db_engine, model_tier=None, llm_record_mode=llm_record_mode
    )

    instruction = interaction.system_instruction or ""
    assert "working for Tester" in instruction
    assert "spawn_worker" not in instruction
    assert "complex_tasks" not in instruction
    assert not instruction.startswith("System:")
    # A delegated run collects what the agent leaves for the user, so it is
    # told where to leave it.
    assert "/workspace/outputs" in instruction


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=_sanitize_keeping_upload_sessions)
async def test_a_delegated_attachment_reaches_the_sandbox_intact(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    llm_record_mode: str,
) -> None:
    """A screenshot-sized file arrives in the sandbox byte for byte.

    Sized past the 1 MiB of base64 the API accepts for an inline source, which
    is where every ordinary photo was refused with a bare 400 (#1457). The
    agent reporting the file's digest is evidence that it got the whole file.
    """
    content = random.Random(1457).randbytes(1_100_000)
    registry = coder.attachment_registry
    assert registry is not None
    attachment = await registry.register_user_attachment(
        db_context=Database(db_engine),
        content=content,
        filename="screenshot.png",
        mime_type="image/png",
        user_id="tester",
    )

    await _run_to_completion(
        coder,
        db_engine,
        model_tier=None,
        llm_record_mode=llm_record_mode,
        content_parts=[
            {
                "type": "text",
                "text": "Print the SHA-256 of /workspace/screenshot.png with "
                "sha256sum and report the hex digest, then stop.",
            },
            {"type": "attachment", "attachment_id": attachment.attachment_id},
        ],
        acting_user_id="tester",
        expected_output=hashlib.sha256(content).hexdigest(),
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=_sanitize_keeping_upload_sessions)
async def test_files_the_agent_leaves_in_outputs_come_back_as_attachments(
    coder: InteractionsAgentProcessingService,
    db_engine: AsyncEngine,
    llm_record_mode: str,
) -> None:
    """What the agent writes under /workspace/outputs is returned byte for byte.

    Read back from the run's environment after it finishes and registered as
    the run's user's attachments, nested paths included. A file outside the
    outputs directory stays behind.
    """
    interaction = await _run_to_completion(
        coder,
        db_engine,
        model_tier=None,
        llm_record_mode=llm_record_mode,
        content_parts=[
            {
                "type": "text",
                "text": "Run exactly this, then reply with just the word DONE: "
                "mkdir -p /workspace/outputs/data && "
                "printf 'tps-1457' > /workspace/outputs/report.txt && "
                "python3 -c \"open('/workspace/outputs/data/table.bin','wb')"
                '.write(bytes(range(256))*4)" && '
                "printf 'scratch' > /workspace/scratch.txt",
            }
        ],
        expected_output="DONE",
    )
    assert interaction.id

    db_context = Database(db_engine)
    collected = await coder.collect_outputs(
        await coder.observe_async(interaction.id),
        owner_user_id="tester",
        conversation_id="coder-integration",
        taint_state=TurnTaintState.empty(),
        db_context=db_context,
    )

    assert collected.omitted == []
    registry = coder.attachment_registry
    assert registry is not None
    returned: dict[str, tuple[bytes, str]] = {}
    for attachment_id in collected.attachment_ids:
        metadata = await registry.get_attachment(
            db_context, attachment_id, acting_user_id="tester"
        )
        content = await registry.get_attachment_content(
            db_context, attachment_id, acting_user_id="tester"
        )
        assert metadata is not None
        assert content is not None
        assert metadata.owner_user_id == "tester"
        returned[str(metadata.metadata["original_filename"])] = (
            content,
            metadata.mime_type,
        )
    assert returned == {
        "report.txt": (b"tps-1457", "text/plain"),
        "data_table.bin": (bytes(range(256)) * 4, "application/octet-stream"),
    }
