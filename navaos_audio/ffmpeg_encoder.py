"""
Wraps the existing FFmpeg filter chain (de-emphasis is now handled by
rtl_fm's -E deemp / squelch stage upstream, so this stage focuses on
spectral noise reduction, compression, volume, and MP3 encoding -- the
same processing you already tuned and confirmed sounds good on WX4).

Reads raw PCM from stdin so it can sit directly downstream of
AdaptiveSquelch instead of being fed by a giant shell pipe.
"""
from __future__ import annotations

import subprocess
from typing import Iterator, Optional

from .config import ChannelConfig


def build_filter_chain(cfg: ChannelConfig) -> str:
    """The FFmpeg -af chain shared by the live stream and the recorder."""
    return ",".join([
        "highpass=f=300",
        "lowpass=f=3000",
        "afftdn=nr=12:nf=-25",
        # NBFM audio comes out of the demod around -40 dBFS regardless of
        # signal strength (level is set by transmitter deviation), so it
        # needs fixed makeup gain - the compressor alone never engaged.
        # Gain goes after afftdn so its absolute noise floor is unchanged;
        # the limiter keeps hot transmitters from clipping.
        f"volume={cfg.makeup_gain_db}dB",
        f"acompressor=threshold=-20dB:ratio=3:attack=5:release=100:makeup={cfg.compressor_makeup}",
        f"volume={cfg.volume}",
        "alimiter=limit=0.9:level=disabled",
    ])


class FFmpegEncoder:
    def __init__(self, config: ChannelConfig):
        self._cfg = config
        self._proc: Optional[subprocess.Popen] = None

    def _build_args(self) -> list[str]:
        cfg = self._cfg
        filters = build_filter_chain(cfg)
        return [
            "ffmpeg",
            "-f", "s16le",
            "-ar", str(cfg.sample_rate),
            "-ac", "1",
            "-i", "pipe:0",
            "-af", filters,
            "-f", "mp3",
            "-b:a", "64k",
            "pipe:1",
        ]

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self._build_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    def write(self, pcm_bytes: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Encoder not started - call start() first")
        self._proc.stdin.write(pcm_bytes)

    def read_mp3_chunks(self, chunk_size: int = 4096) -> Iterator[bytes]:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("Encoder not started - call start() first")
        stdout = self._proc.stdout
        while True:
            chunk = stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
