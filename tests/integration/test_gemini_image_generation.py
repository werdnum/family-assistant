"""Integration tests for GeminiImageBackend using SDK record/replay.

Exercises the configurable Gemini image model end-to-end against the real
Gemini API, using the Google GenAI SDK's built-in DebugConfig for deterministic
record/replay (same pattern as tests/integration/test_google_embedding.py).

The default recording targets the newly added Nano Banana Lite model
(``gemini-3.1-flash-lite-image``), which is the model this PR makes selectable.

## Running

```bash
# Replay existing recordings (default, safe for CI)
LLM_RECORD_MODE=replay pytest tests/integration/test_gemini_image_generation.py -xq -m llm_integration

# Record new interactions (requires GEMINI_API_KEY)
LLM_RECORD_MODE=record GEMINI_API_KEY=xxx pytest tests/integration/test_gemini_image_generation.py -xq -m llm_integration
```

Note: after re-recording, the committed cassette has its (large,
non-deterministic, test-irrelevant) ``thoughtSignature`` fields stripped to keep
the fixture small — the recording is ~3 MB with them and ~1.3 MB without. The
test only decodes the returned image, so removing them does not affect replay.
"""

import io
import os
from pathlib import Path

import pytest
from google.genai.client import DebugConfig
from PIL import Image

from family_assistant.tools.image_backends import GeminiImageBackend

# Nano Banana Lite — the fast/cheap image model added by this PR.
IMAGE_MODEL = "gemini-3.1-flash-lite-image"
CASSETTE_DIR = "tests/cassettes/gemini"
_REPLAY_ROOT = "integration.test_gemini_image_generation"


def _replay_file_exists(test_name: str) -> bool:
    replay_path = Path(CASSETTE_DIR) / _REPLAY_ROOT / test_name / "mldev.json"
    return replay_path.exists()


def _make_backend(llm_record_mode: str, test_name: str) -> GeminiImageBackend:
    replay_exists = _replay_file_exists(test_name)
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if llm_record_mode == "replay" and not replay_exists:
        pytest.fail(
            f"Replay file missing for {test_name}. Record with LLM_RECORD_MODE=record."
        )
    if llm_record_mode == "record" and not api_key:
        pytest.skip("Recording requires GEMINI_API_KEY or GOOGLE_API_KEY.")
    if llm_record_mode == "auto" and not replay_exists and not api_key:
        pytest.skip(
            "Auto-recording missing replays requires GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    debug_config = DebugConfig(
        client_mode=llm_record_mode,
        replay_id=f"{_REPLAY_ROOT}/{test_name}/mldev",
        replays_directory=CASSETTE_DIR,
    )
    return GeminiImageBackend(
        api_key=api_key or "test-key",
        model=IMAGE_MODEL,
        debug_config=debug_config,
    )


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_generate_image_nano_banana_lite(llm_record_mode: str) -> None:
    """Nano Banana Lite returns decodable image bytes from a text prompt."""
    backend = _make_backend(llm_record_mode, "test_generate_image_nano_banana_lite")

    image_bytes = await backend.generate_image(
        "A single ripe banana on a plain white background", style="photorealistic"
    )

    assert image_bytes, "backend returned no image bytes"
    # The bytes must be a real, decodable raster image.
    with Image.open(io.BytesIO(image_bytes)) as img:
        assert img.width > 0
        assert img.height > 0
