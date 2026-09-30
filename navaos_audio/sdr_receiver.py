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
import threading
from typing import Iterator, Optional

from .config import ChannelConfig

_STDERR_TAIL_LINES = 50


class RTLSDRReceiver:
    def __init__(self, config: ChannelConfig, device_index: int = 0):
        self._cfg = config
        self._device_index = device_index
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: Optional[threading.Thread] = None

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
        """rtl_fm's most recent stderr lines - useful for diagnosing a
        device claim failure or other startup error, which otherwise looks
        identical to a healthy receiver producing no traffic."""
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
