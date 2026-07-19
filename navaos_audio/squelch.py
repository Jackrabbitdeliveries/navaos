"""
Adaptive digital squelch and speech-presence detection.

Replaces rtl_fm's -l squelch (unreliable, coarse, and was never actually
wired up correctly in the previous implementation) with a software squelch
that:

  1. Tracks the ambient noise floor adaptively (so it keeps working as RF
     conditions change with weather, tide, boat orientation, etc).
  2. Uses a real speech-detection algorithm (WebRTC's VAD) rather than raw
     energy, so continuous broadband hiss doesn't falsely open the gate.
  3. Applies hysteresis + a hang timer so the gate doesn't chatter on brief
     syllable gaps.
  4. Applies a short attack/release envelope so opening/closing the gate
     doesn't produce an audible click/thump.

This module operates on raw PCM (16-bit signed mono) *before* the existing
FFmpeg filter chain, so the already-tuned de-emphasis / band-pass / afftdn /
compressor chain is untouched and continues to run only on audio that has
already been judged worth transmitting.
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass

import numpy as np

try:
    import webrtcvad
except ImportError:  # pragma: no cover
    webrtcvad = None

from .config import ChannelConfig


def _rms_dbfs(samples: np.ndarray) -> float:
    """RMS level of int16 PCM samples, expressed in dBFS."""
    if samples.size == 0:
        return -120.0
    rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    if rms <= 0:
        return -120.0
    return 20.0 * np.log10(rms / 32768.0)


class NoiseFloorEstimator:
    """
    Tracks the ambient noise floor using a rolling low percentile of frame
    energy. A low percentile (rather than a mean) means a burst of speech
    doesn't drag the estimated floor upward -- only genuinely quiet frames
    pull it down, so the estimate reflects "the noise when nobody is
    talking" rather than "the average level."
    """

    def __init__(self, window_frames: int = 250, percentile: float = 20.0):
        self._history: "collections.deque[float]" = collections.deque(maxlen=window_frames)
        self.percentile = percentile
        self._floor_dbfs = -60.0  # reasonable starting guess before enough history exists

    def update(self, frame_dbfs: float) -> float:
        self._history.append(frame_dbfs)
        if len(self._history) >= 10:
            self._floor_dbfs = float(np.percentile(self._history, self.percentile))
        return self._floor_dbfs

    @property
    def floor_dbfs(self) -> float:
        return self._floor_dbfs


@dataclass
class _SquelchState:
    open: bool = False
    gain: float = 0.0          # current envelope gain, 0.0-1.0
    last_speech_time: float = 0.0


class AdaptiveSquelch:
    """
    Combines adaptive noise-floor tracking with WebRTC VAD to decide, frame
    by frame, whether incoming audio is worth passing through.

    Call `process(pcm_frame, sample_rate)` with 16-bit mono PCM in one of
    the VAD's supported frame durations (10/20/30 ms) at a supported rate
    (8000/16000/32000/48000 Hz). Returns a same-length frame with the
    envelope gain applied, so silence is faded smoothly rather than cut.
    """

    def __init__(self, config: ChannelConfig):
        if webrtcvad is None:
            raise RuntimeError(
                "webrtcvad is required for AdaptiveSquelch. Install with "
                "`pip install webrtcvad`."
            )
        self._cfg = config
        self._vad = webrtcvad.Vad(config.vad_aggressiveness)
        self._noise_floor = NoiseFloorEstimator(percentile=config.noise_floor_percentile)
        self._state = _SquelchState()

    def update_config(self, config: ChannelConfig) -> None:
        """Apply a new config to the running squelch (called between frames)."""
        if config.vad_aggressiveness != self._cfg.vad_aggressiveness:
            self._vad.set_mode(config.vad_aggressiveness)
        self._noise_floor.percentile = config.noise_floor_percentile
        self._cfg = config

    def process(self, frame: np.ndarray, sample_rate: int) -> np.ndarray:
        cfg = self._cfg
        now = time.monotonic()

        frame_dbfs = _rms_dbfs(frame)
        floor = self._noise_floor.update(frame_dbfs)

        if cfg.squelch_enabled:
            try:
                is_speech = self._vad.is_speech(frame.tobytes(), sample_rate)
            except Exception:
                # VAD raises on malformed frame sizes; fail safe to "no speech"
                # rather than let a decode hiccup jam the gate open.
                is_speech = False

            # Require the frame to also be meaningfully above the noise
            # floor, so a VAD false-positive on pure hiss doesn't open the
            # gate on its own. Uses a lower bar to *stay* open than to
            # *open* (hysteresis), so we don't chop the tail off words.
            threshold = cfg.close_threshold_db if self._state.open else cfg.open_threshold_db
            above_floor = (frame_dbfs - floor) >= threshold
            speech_confirmed = is_speech and above_floor
        else:
            speech_confirmed = True  # squelch disabled -> always pass audio

        if speech_confirmed:
            self._state.open = True
            self._state.last_speech_time = now
        elif self._state.open and (now - self._state.last_speech_time) > cfg.hang_time_s:
            self._state.open = False

        target_gain = 1.0 if self._state.open else 0.0
        frame_ms = 1000.0 * len(frame) / sample_rate
        ramp_ms = cfg.attack_ms if target_gain > self._state.gain else cfg.release_ms
        direction = 1.0 if target_gain > self._state.gain else -1.0
        step = frame_ms / max(ramp_ms, 1.0)
        self._state.gain = float(np.clip(self._state.gain + direction * step, 0.0, 1.0))

        if self._state.gain <= 0.0:
            return np.zeros_like(frame)
        if self._state.gain >= 1.0:
            return frame
        return (frame.astype(np.float32) * self._state.gain).astype(np.int16)

    @property
    def config(self) -> ChannelConfig:
        return self._cfg

    @property
    def is_open(self) -> bool:
        return self._state.open

    @property
    def noise_floor_dbfs(self) -> float:
        return self._noise_floor.floor_dbfs
