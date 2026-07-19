"""
Thin wrapper around rtl_fm that yields raw PCM audio.

Deliberately does *not* build a giant shell string -- the command is
assembled from an explicit argument list. rtl_fm's own squelch (-l) is
always left at 0 here: squelch is handled entirely in software
(see squelch.py). This intentionally replaces the previous approach where a
squelch value was computed but the command still hardcoded -l 0 -- rather
than fix that value, we're removing rtl_fm from the squelch decision
entirely.
"""
from __future__ import annotations

import subprocess
from typing import Iterator, Optional

from .config import ChannelConfig


class RTLSDRReceiver:
    def __init__(self, config: ChannelConfig, device_index: int = 0):
        self._cfg = config
        self._device_index = device_index
        self._proc: Optional[subprocess.Popen] = None

    def _build_args(self) -> list[str]:
        cfg = self._cfg
        return [
            "rtl_fm",
            "-d", str(self._device_index),
            "-f", str(int(cfg.frequency_hz)),
            "-M", "fm",
            "-s", str(cfg.sample_rate),
            "-g", str(cfg.rf_gain),
            "-E", "deemp",
            "-l", "0",  # software squelch (AdaptiveSquelch) handles gating
            "-",
        ]

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self._build_args(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    def read_frames(self, frame_bytes: int) -> Iterator[bytes]:
        """Yield raw PCM chunks of exactly `frame_bytes` bytes each."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("Receiver not started - call start() first")
        stdout = self._proc.stdout
        while True:
            chunk = stdout.read(frame_bytes)
            if not chunk:
                break
            if len(chunk) < frame_bytes:
                chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
            yield chunk

    def __enter__(self) -> "RTLSDRReceiver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
