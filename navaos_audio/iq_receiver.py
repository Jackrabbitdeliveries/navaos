"""
IQ-based receiver: rtl_sdr raw samples -> RF power measurement + NBFM demod.

Replaces rtl_fm when `ChannelConfig.receiver == "iq"`. rtl_fm only outputs
demodulated audio, so it can't tell the squelch whether a carrier is
actually present. Reading IQ ourselves lets every 20 ms frame carry an
RF signal-to-noise reading (`rf_snr_db`) alongside its PCM, and the squelch
gates on that instead of on audio loudness (which on FM is close to
backwards: no signal = loud hiss).

RF SNR method (same as the 2026-09-30 rtl_power baseline): power in the
channel (+/- chan_half_bw) vs. the median per-bin power across the rest of
the captured band, excluding the DC spike and the channel itself. Measured
against the band in the same frame, so it needs no history and can't be
fooled by a stale floor estimate after a retune.

The dongle is tuned `iq_offset_hz` away from the channel so its DC spike
doesn't sit on the signal. Output PCM is scaled like rtl_fm's (+/-pi rad ->
+/-16384) with the same 75 us de-emphasis, so the downstream FFmpeg filter
chain sees the same levels it was tuned for.

Pure numpy - no scipy.
"""
from __future__ import annotations

import subprocess
import threading
from typing import Iterator, Optional

import numpy as np

from .config import ChannelConfig

_STDERR_TAIL_LINES = 50
_DEEMPH_TAU_S = 75e-6
_DC_EXCLUDE_HZ = 20_000      # ignore bins this close to the tuned center
_EDGE_EXCLUDE_FRAC = 0.1     # ignore outer 10% of the band (anti-alias rolloff)


def _lowpass_taps(num_taps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    """Windowed-sinc low-pass FIR, unity DC gain."""
    n = np.arange(num_taps) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / fs * n) * np.hamming(num_taps)
    return (h / h.sum()).astype(np.float32)


class NBFMDemodulator:
    """Stateful, frame-at-a-time NBFM demodulator + RF SNR meter.

    Feed complex baseband blocks (centered on the tuned frequency, channel at
    `channel_offset_hz`) of `iq_rate / audio_rate * N` samples; get back N
    int16 audio samples and an RF SNR reading for the block.
    """

    def __init__(
        self,
        iq_rate: int,
        audio_rate: int,
        channel_offset_hz: float,
        chan_half_bw_hz: float = 6_000,
        audio_cutoff_hz: float = 8_000,
        num_taps: int = 101,
    ):
        if iq_rate % audio_rate:
            raise ValueError("iq_rate must be an integer multiple of audio_rate")
        self.iq_rate = iq_rate
        self.audio_rate = audio_rate
        self.decim = iq_rate // audio_rate
        self.channel_offset_hz = channel_offset_hz
        self.chan_half_bw_hz = chan_half_bw_hz

        self._taps = _lowpass_taps(num_taps, audio_cutoff_hz, iq_rate)
        self._fir_state = np.zeros(num_taps - 1, dtype=np.complex64)
        self._nco_phase = 0.0
        self._last_sample = np.complex64(1.0)

        # De-emphasis as a truncated-exponential FIR (equivalent to rtl_fm's
        # single-pole IIR to within 1e-3) so it vectorizes in numpy.
        alpha = 1.0 - np.exp(-1.0 / (audio_rate * _DEEMPH_TAU_S))
        k = np.arange(int(np.ceil(np.log(1e-3) / np.log(1 - alpha))) + 1)
        self._deemph = (alpha * (1 - alpha) ** k).astype(np.float32)
        self._deemph_state = np.zeros(len(self._deemph) - 1, dtype=np.float32)

        self._masks_for_len: Optional[int] = None
        self._chan_mask: Optional[np.ndarray] = None
        self._ref_mask: Optional[np.ndarray] = None
        self._window: Optional[np.ndarray] = None

    def _build_masks(self, n: int) -> None:
        f = np.fft.fftfreq(n, 1.0 / self.iq_rate)
        edge = self.iq_rate / 2 * (1 - _EDGE_EXCLUDE_FRAC)
        self._chan_mask = np.abs(f - self.channel_offset_hz) <= self.chan_half_bw_hz
        self._ref_mask = (
            (np.abs(f) > _DC_EXCLUDE_HZ)
            & (np.abs(f) < edge)
            & (np.abs(f - self.channel_offset_hz) > 2 * self.chan_half_bw_hz)
        )
        self._window = np.hanning(n).astype(np.float32)
        self._masks_for_len = n

    def rf_snr_db(self, iq: np.ndarray) -> float:
        """Channel power vs. band-median noise, in dB (~0 on an empty channel)."""
        if self._masks_for_len != len(iq):
            self._build_masks(len(iq))
        p = np.abs(np.fft.fft(iq * self._window)) ** 2
        # Median of exponentially-distributed noise bins is ln(2) x the mean.
        noise_per_bin = np.median(p[self._ref_mask]) / np.log(2)
        chan = p[self._chan_mask].sum()
        return float(10 * np.log10(max(chan, 1e-20) / (noise_per_bin * self._chan_mask.sum() + 1e-20)))

    def demodulate(self, iq: np.ndarray) -> np.ndarray:
        n = len(iq)
        # Mix the channel down to 0 Hz with a phase-continuous NCO.
        step = -2 * np.pi * self.channel_offset_hz / self.iq_rate
        phases = self._nco_phase + step * np.arange(n)
        self._nco_phase = float((self._nco_phase + step * n) % (2 * np.pi))
        x = iq * np.exp(1j * phases).astype(np.complex64)

        # Channel filter + decimate (keep FIR history across frames).
        buf = np.concatenate([self._fir_state, x])
        self._fir_state = buf[-(len(self._taps) - 1):]
        y = np.convolve(buf, self._taps, mode="valid")[:: self.decim]

        # Polar discriminator.
        prev = np.concatenate([[self._last_sample], y[:-1]])
        self._last_sample = y[-1]
        d = np.angle(y * np.conj(prev)).astype(np.float32)

        # De-emphasis, then rtl_fm-compatible scaling (+/-pi -> +/-16384).
        dbuf = np.concatenate([self._deemph_state, d])
        self._deemph_state = dbuf[-(len(self._deemph) - 1):]
        audio = np.convolve(dbuf, self._deemph, mode="valid")
        return np.clip(audio * (16384 / np.pi), -32768, 32767).astype(np.int16)

    def process(self, iq: np.ndarray) -> tuple[np.ndarray, float]:
        return self.demodulate(iq), self.rf_snr_db(iq)


class IQReceiver:
    """Drop-in replacement for RTLSDRReceiver (same start/stop/read_frames/
    stderr_tail interface) that also exposes `rf_snr_db` for the most
    recently yielded frame."""

    def __init__(self, config: ChannelConfig, device_index: int = 0):
        self._cfg = config
        self._device_index = device_index
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: Optional[threading.Thread] = None
        self.rf_snr_db: Optional[float] = None
        self._demod = NBFMDemodulator(
            iq_rate=config.iq_sample_rate,
            audio_rate=config.sample_rate,
            channel_offset_hz=-config.iq_offset_hz,
            chan_half_bw_hz=config.rf_channel_half_bw_hz,
        )

    def _build_args(self) -> list[str]:
        cfg = self._cfg
        return [
            "rtl_sdr",
            "-d", str(self._device_index),
            "-f", str(int(cfg.frequency_hz + cfg.iq_offset_hz)),
            "-s", str(cfg.iq_sample_rate),
            "-g", str(cfg.rf_gain),
            # Small USB block (~34 ms at 240 kS/s) instead of the default
            # ~0.5 s, so a scan hop starts producing audio quickly.
            "-b", "16384",
            "-",
        ]

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self._build_args(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_lines = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw_line in proc.stderr:
            line = raw_line.decode(errors="replace").rstrip()
            if not line:
                continue
            self._stderr_lines.append(line)
            del self._stderr_lines[:-_STDERR_TAIL_LINES]

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_lines)

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None

    def read_frames(self, frame_bytes: int) -> Iterator[bytes]:
        """Yield int16 PCM chunks of exactly `frame_bytes` bytes, updating
        `rf_snr_db` for each one before it's yielded."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("Receiver not started - call start() first")
        stdout = self._proc.stdout
        iq_bytes = (frame_bytes // 2) * self._demod.decim * 2  # 2 bytes (I,Q) per sample
        while True:
            chunk = b""
            while len(chunk) < iq_bytes:
                part = stdout.read(iq_bytes - len(chunk))
                if not part:
                    return
                chunk += part
            raw = np.frombuffer(chunk, dtype=np.uint8).astype(np.float32)
            raw = (raw - 127.5) / 127.5
            iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
            pcm, self.rf_snr_db = self._demod.process(iq)
            yield pcm.tobytes()

    def __enter__(self) -> "IQReceiver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def make_receiver(config: ChannelConfig, device_index: int = 0):
    """Pick the receiver implementation from config ("iq" or "rtl_fm")."""
    if config.receiver == "iq":
        return IQReceiver(config, device_index)
    from .sdr_receiver import RTLSDRReceiver
    return RTLSDRReceiver(config, device_index)
