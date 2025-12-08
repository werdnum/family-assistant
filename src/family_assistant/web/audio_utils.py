"""Audio processing utilities for web endpoints."""

import logging
from typing import Any

import numpy as np
import soxr

logger = logging.getLogger(__name__)


class StatefulResampler:
    """
    Stateful audio resampler that maintains continuity across chunks.
    Uses libsoxr for high-quality, low-latency resampling suitable for real-time audio.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate

        # Create a resampler instance that maintains state
        # quality='VHQ' provides Very High Quality suitable for telephony
        # For even lower latency, could use 'HQ' (High Quality)
        self.resampler: Any = soxr.ResampleStream(
            src_rate, dst_rate, num_channels=1, dtype="int16", quality="VHQ"
        )
        logger.debug(f"Initialized soxr resampler: {src_rate}Hz -> {dst_rate}Hz (VHQ)")

    def resample(self, audio_data: bytes) -> bytes:
        """Resample audio data maintaining filter state across calls."""
        if self.src_rate == self.dst_rate or not audio_data:
            return audio_data

        # Convert bytes to numpy array
        audio_np = np.frombuffer(audio_data, dtype=np.int16)
        if len(audio_np) == 0:
            return b""

        # Resample using stateful resampler
        # The resampler maintains internal state for continuity
        resampled = self.resampler.resample_chunk(audio_np)

        return resampled.astype(np.int16).tobytes()
