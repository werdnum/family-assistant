"""Integration test for GeminiOmniVideoBackend using VCR record/replay.

Exercises Gemini Omni Flash (``gemini-omni-flash``) video generation
end-to-end against the real Interactions API. Unlike the Gemini chat/image/
embedding paths (which use the SDK's DebugConfig replay), the Interactions
client is a separate httpx transport, so this test records/replays at the HTTP
layer with VCR.py.

## Running

```bash
# Replay existing cassette (default, safe for CI)
LLM_RECORD_MODE=replay pytest tests/integration/test_gemini_omni_video_generation.py -xq -m llm_integration

# Record against the real API (requires GEMINI_API_KEY and google-genai>=2.0)
LLM_RECORD_MODE=record GEMINI_API_KEY=xxx pytest tests/integration/test_gemini_omni_video_generation.py -xq -m llm_integration
```
"""

import os
from collections.abc import Iterator

import pytest
import vcr
from vcr.record_mode import RecordMode

from family_assistant.tools.video_backends import (
    GeminiOmniVideoBackend,
    VideoGenerationRequest,
)

CASSETTE = "tests/cassettes/llm/test_omni_flash_video_generation.yaml"

_RECORD_MODE_MAP = {
    "replay": RecordMode.NONE,
    "auto": RecordMode.ONCE,
    "record": RecordMode.ALL,
}


@pytest.fixture
def omni_cassette(llm_record_mode: str) -> Iterator[None]:
    """Enforce record/replay preconditions and keep the cassette open for the test.

    The filesystem/API-key checks live here (synchronous fixture setup) rather
    than inside the async test, so no blocking I/O runs on the event loop.
    """
    if llm_record_mode == "replay" and not os.path.exists(CASSETTE):
        pytest.fail(
            f"Cassette missing at {CASSETTE}. Record with LLM_RECORD_MODE=record."
        )
    if llm_record_mode == "record" and not _api_key():
        pytest.skip("Recording requires GEMINI_API_KEY or GOOGLE_API_KEY.")

    my_vcr = vcr.VCR(
        record_mode=_RECORD_MODE_MAP[llm_record_mode],
        match_on=["method", "scheme", "host", "port", "path", "query"],
        filter_headers=["authorization", "x-goog-api-key", "x-api-key", "api-key"],
        filter_query_parameters=["key", "api_key"],
    )
    with my_vcr.use_cassette(CASSETTE):
        yield


def _api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.usefixtures("omni_cassette")
async def test_omni_flash_video_generation() -> None:
    """Omni Flash returns MP4 bytes for a text prompt via the Interactions API."""
    backend = GeminiOmniVideoBackend(api_key=_api_key() or "test-key")

    result = await backend.generate_video(
        VideoGenerationRequest(
            prompt="A short clip of a paper airplane gliding across a bright office",
            aspect_ratio="16:9",
            # 3s is Omni Flash's minimum — keeps the recorded cassette small.
            duration_seconds=3,
        )
    )

    assert result.model == "gemini-omni-flash"
    assert result.mime_type == "video/mp4"
    assert result.content, "backend returned no video bytes"
    # ISO base media (MP4) files carry an 'ftyp' box near the start.
    assert b"ftyp" in result.content[:32]
