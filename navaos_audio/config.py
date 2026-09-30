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
from typing import Generic, TypeVar


@dataclass(frozen=True)
class ChannelConfig:
    channel: str
    frequency_hz: float
    sample_rate: int = 48_000
    rf_gain: float = 49.6

    # Receiver: "iq" (rtl_sdr + in-process demod, gives RF SNR per frame) or
    # "rtl_fm" (legacy, audio only - squelch falls back to audio mode).
    receiver: str = "iq"
    iq_sample_rate: int = 240_000        # must be a multiple of sample_rate
    iq_offset_hz: float = 50_000.0       # tune this far above the channel to dodge the DC spike
    rf_channel_half_bw_hz: float = 6_000.0

    # Squelch / speech-detection tuning
    squelch_enabled: bool = True
    # RF squelch (used whenever the receiver supplies rf_snr_db). dB above
    # the band noise floor; 2026-09-30 boat baseline: quiet ~0 dB, local
    # traffic +24..+33 dB, NOAA WX ~+10 dB.
    rf_open_threshold_db: float = 10.0
    rf_close_threshold_db: float = 6.0
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


@dataclass(frozen=True)
class ScanSettings:
    """Scan-wide tuning - NOT per-channel, unlike ChannelConfig. One instance
    governs an entire ScanController regardless of which channel it's
    currently visiting."""
    dwell_seconds: float = 1.0          # how long to sit on a quiet channel before advancing
    lock_sustain_s: float = 0.4         # how long is_open must hold continuously before locking
    auto_unlock_quiet_s: float = 60.0   # how long a lock must be continuously quiet before auto-resume


_T = TypeVar("_T")


class ConfigStore(Generic[_T]):
    """Thread-safe holder for a frozen dataclass config that supports live
    updates (ChannelConfig or ScanSettings)."""

    def __init__(self, config: _T):
        self._lock = RLock()
        self._config = config

    def get(self) -> _T:
        with self._lock:
            return self._config

    def update(self, **changes) -> _T:
        """Apply partial changes and return the new config. Thread-safe."""
        with self._lock:
            self._config = replace(self._config, **changes)
            return self._config
