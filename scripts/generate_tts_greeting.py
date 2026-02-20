#!/usr/bin/env python3
"""Generate a greeting WAV file using Gemini TTS.

Uses the Gemini API to synthesize speech from text and saves as a 16kHz mono
16-bit PCM WAV file suitable for the Asterisk Live greeting system.

Requires:
    - GEMINI_API_KEY environment variable
    - google-genai package (already a project dependency)

Usage:
    python scripts/generate_tts_greeting.py \\
        --text "Hello! How can I help you?" \\
        --output src/family_assistant/web/resources/greeting.wav

    python scripts/generate_tts_greeting.py \\
        --text "Hello, you've reached the family residence. Please note that this call may be recorded. How can I help you today?" \\
        --output src/family_assistant/web/resources/greeting_external.wav \\
        --voice Autonoe
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import wave
from pathlib import Path

from google import genai
from google.genai import types

GEMINI_OUTPUT_SAMPLE_RATE = 24000
TARGET_SAMPLE_RATE = 16000


def _resample_linear(pcm_data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM via linear interpolation (fallback)."""
    samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data)
    ratio = dst_rate / src_rate
    new_length = int(len(samples) * ratio)
    resampled = []
    for i in range(new_length):
        src_pos = i / ratio
        idx = int(src_pos)
        frac = src_pos - idx
        if idx + 1 < len(samples):
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[min(idx, len(samples) - 1)]
        resampled.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(resampled)}h", *resampled)


def resample_pcm16(pcm_data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM data using libsoxr if available."""
    if src_rate == dst_rate:
        return pcm_data
    try:
        from family_assistant.web.audio_utils import (  # noqa: PLC0415 - inside try/except for ImportError fallback
            StatefulResampler,
        )

        resampler = StatefulResampler(src_rate, dst_rate)
        return resampler.resample(pcm_data)
    except ImportError:
        return _resample_linear(pcm_data, src_rate, dst_rate)


def generate_greeting(
    text: str,
    output_path: Path,
    voice: str = "Autonoe",
    model: str = "gemini-3-flash-preview-tts",
) -> None:
    """Generate a greeting WAV file using Gemini TTS."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print(f"Generating TTS with voice={voice}, model={model}...")
    print(f"Text: {text!r}")

    response = client.models.generate_content(
        model=model,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice,
                    )
                )
            ),
        ),
    )

    candidates = response.candidates
    if not candidates:
        print("Error: No candidates in response", file=sys.stderr)
        sys.exit(1)

    content = candidates[0].content
    if not content or not content.parts:
        print("Error: No content parts in response", file=sys.stderr)
        sys.exit(1)

    inline_data = content.parts[0].inline_data
    if not inline_data or not inline_data.data:
        print("Error: No audio data in response", file=sys.stderr)
        sys.exit(1)

    pcm_data = bytes(inline_data.data)
    duration_24k = len(pcm_data) / 2 / GEMINI_OUTPUT_SAMPLE_RATE
    print(
        f"Received {len(pcm_data)} bytes ({duration_24k:.2f}s at {GEMINI_OUTPUT_SAMPLE_RATE}Hz)"
    )

    pcm_16k = resample_pcm16(pcm_data, GEMINI_OUTPUT_SAMPLE_RATE, TARGET_SAMPLE_RATE)
    duration_16k = len(pcm_16k) / 2 / TARGET_SAMPLE_RATE
    print(
        f"Resampled to {len(pcm_16k)} bytes ({duration_16k:.2f}s at {TARGET_SAMPLE_RATE}Hz)"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_SAMPLE_RATE)
        wf.writeframes(pcm_16k)

    print(f"Saved to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a greeting WAV file using Gemini TTS"
    )
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument(
        "--output", required=True, type=Path, help="Output WAV file path"
    )
    parser.add_argument(
        "--voice", default="Autonoe", help="Voice name (default: Autonoe)"
    )
    parser.add_argument(
        "--model",
        default="gemini-3-flash-preview-tts",
        help="Gemini TTS model (default: gemini-3-flash-preview-tts)",
    )

    args = parser.parse_args()
    generate_greeting(args.text, args.output, voice=args.voice, model=args.model)


if __name__ == "__main__":
    main()
