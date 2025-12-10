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
    Includes self-healing and fallback mechanisms for robustness.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.fallback_to_stateless = False
        self.resampler: Any = None

        self._init_resampler(quality="VHQ")

    def _init_resampler(self, quality: str = "VHQ") -> None:
        """Initialize or re-initialize the soxr ResampleStream."""
        try:
            self.resampler = soxr.ResampleStream(
                self.src_rate,
                self.dst_rate,
                num_channels=1,
                dtype="int16",
                quality=quality,
            )
            logger.info(
                f"Initialized soxr resampler: {self.src_rate}Hz -> {self.dst_rate}Hz ({quality})"
            )
        except Exception as e:
            logger.error(
                f"Failed to initialize ResampleStream ({quality}): {e}. Using stateless fallback."
            )
            self.fallback_to_stateless = True

    def resample(self, audio_data: bytes) -> bytes:
        """Resample audio data maintaining filter state across calls."""
        if self.src_rate == self.dst_rate or not audio_data:
            return audio_data

        # Convert bytes to numpy array
        audio_np = np.frombuffer(audio_data, dtype=np.int16)
        if len(audio_np) == 0:
            return b""

        if self.fallback_to_stateless:
            return self._resample_stateless(audio_np)

        # Resample using stateful resampler
        try:
            resampled = self.resampler.resample_chunk(audio_np)
        except Exception as e:
            logger.error(f"ResampleStream failed: {e}. Attempting recovery.")
            return self._recover_and_resample(audio_np)

        # Check for 1:1 resampling anomaly (when rates differ significantly)
        if self._is_anomaly(len(audio_np), len(resampled)):
            logger.warning(
                f"Resampler anomaly detected (1:1 output) for {self.src_rate}->{self.dst_rate}. "
                "Attempting re-initialization with HQ."
            )
            return self._recover_and_resample(audio_np, quality="HQ")

        return resampled.astype(np.int16).tobytes()

    def _is_anomaly(self, in_len: int, out_len: int) -> bool:
        """Check if input/output lengths indicate a resampling failure (1:1 pass-through)."""
        if in_len < 100:
            # Ignore small chunks where buffering might obscure the ratio
            return False

        if in_len != out_len:
            # If lengths differ, it's resampling (or buffering), likely fine
            return False

        # If lengths are identical, check if they SHOULD be identical
        expected_ratio = self.dst_rate / self.src_rate
        # Anomaly if we expect a ratio different from 1.0 (allow 10% margin)
        return abs(expected_ratio - 1.0) > 0.1

    def _recover_and_resample(self, audio_np: np.ndarray, quality: str = "HQ") -> bytes:
        """Attempt to recover stateful resampling by re-initializing, or fallback to stateless."""
        # Try re-initializing (resetting state)
        self._init_resampler(quality=quality)

        if self.fallback_to_stateless:
            # Init failed, go stateless
            return self._resample_stateless(audio_np)

        # Try resampling again with new instance
        try:
            resampled = self.resampler.resample_chunk(audio_np)

            # Verify if recovery worked
            if self._is_anomaly(len(audio_np), len(resampled)):
                logger.error(
                    "Recovery failed (still anomalous). Switching to stateless fallback."
                )
                self.fallback_to_stateless = True
                return self._resample_stateless(audio_np)

            return resampled.astype(np.int16).tobytes()

        except Exception as e:
            logger.error(
                f"Recovery resampling failed: {e}. Switching to stateless fallback."
            )
            self.fallback_to_stateless = True
            return self._resample_stateless(audio_np)

    def _resample_stateless(self, audio_np: np.ndarray) -> bytes:
        """Fallback stateless resampling."""
        try:
            resampled = soxr.resample(audio_np, self.src_rate, self.dst_rate)
            return resampled.astype(np.int16).tobytes()
        except Exception as e:
            logger.error(f"Stateless resampling failed: {e}")
            return audio_np.tobytes()  # Last resort: return original
