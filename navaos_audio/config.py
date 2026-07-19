"""
Runtime-tunable configuration for the NavaOS VHF audio pipeline.

Values here are meant to eventually be driven by UI sliders (Gain, Noise
Reduction, Speech Enhancement, Squelch, Hang Time, Volume) via the API,
without restarting navaos.service. `ConfigStore` provides thread-safe
read/update access so a running pipeline thread can pick up new values
between frames.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock


@dataclass(frozen=True)
class ChannelConfig:
    channel: str
    frequency_hz: float
    sample_rate: int = 48_000
    rf_gain: float = 49.6

    # Squelch / speech-detection tuning
    squelch_enabled: bool = True
    vad_aggressiveness: int = 2          # webrtcvad 0-3 (0=permissive, 3=aggressive)
    noise_floor_percentile: float = 20.0  # percentile used to track quiet-period energy
    open_threshold_db: float = 6.0       # dB above noise floor required to OPEN squelch
    close_threshold_db: float = 3.0      # dB above noise floor required to STAY open (hysteresis)
    hang_time_s: float = 1.2             # how long to hold the gate open after speech stops
    attack_ms: float = 15.0              # envelope fade-in time when opening (avoids clicks)
    release_ms: float = 120.0            # envelope fade-out time when closing

    # Gain / leveling (reserved for the AGC follow-up increment)
    agc_enabled: bool = True
    agc_target_rms_dbfs: float = -18.0
    agc_max_gain_db: float = 24.0

    volume: float = 1.0


class ConfigStore:
    """Thread-safe holder for a ChannelConfig that supports live updates."""

    def __init__(self, config: ChannelConfig):
        self._lock = RLock()
        self._config = config

    def get(self) -> ChannelConfig:
        with self._lock:
            return self._config

    def update(self, **changes) -> ChannelConfig:
        """Apply partial changes and return the new config. Thread-safe."""
        with self._lock:
            self._config = replace(self._config, **changes)
            return self._config
