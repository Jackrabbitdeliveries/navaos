"""
Transmission recorder: saves each squelch opening as its own MP3 clip.

Fed the same gated PCM the live stream gets, one frame at a time, together
with the squelch state. A clip starts on the frame the gate opens and ends
when it closes (after the hang time), so it holds exactly what a listener
would have heard. Each clip is encoded by its own short-lived ffmpeg
process writing straight to disk (no stdout to drain, so it can't stall the
pipeline thread), using the live stream's filter chain.

Layout:  <RECORDINGS_DIR>/YYYY-MM-DD/HHMMSS_ch<channel>_<dur>s_<snr>dB.mp3
         (local time of the opening; <snr> = peak RF SNR, "na" without one)

The filename carries all the metadata - listing is a directory walk, no
database. Clips shorter than MIN_CLIP_S are discarded as blips. Day folders
older than RETENTION_DAYS are pruned, and recording pauses while free disk
space is under MIN_FREE_BYTES.

Env overrides: NAVAOS_RECORDINGS_DIR, NAVAOS_RECORDING=0 to disable.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

from .config import ChannelConfig
from .ffmpeg_encoder import build_filter_chain

RECORDINGS_DIR = Path(os.environ.get("NAVAOS_RECORDINGS_DIR", Path.home() / "navaos-data" / "recordings"))
RECORDING_ENABLED = os.environ.get("NAVAOS_RECORDING", "1") != "0"
MIN_CLIP_S = 0.5
RETENTION_DAYS = 30
MIN_FREE_BYTES = 1 * 1024**3

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLIP_RE = re.compile(
    r"^(?P<hms>\d{6})_ch(?P<channel>[0-9a-z]+)_(?P<dur>\d+(?:\.\d+)?)s_(?P<snr>-?\d+|na)dB\.mp3$"
)


class TransmissionRecorder:
    def __init__(self, base_dir: Path = RECORDINGS_DIR, enabled: bool = RECORDING_ENABLED):
        self._base = Path(base_dir)
        self._enabled = enabled
        self._proc: Optional[subprocess.Popen] = None
        self._tmp_path: Optional[Path] = None
        self._started: Optional[_dt.datetime] = None
        self._channel = ""
        self._frames = 0
        self._frame_s = 0.0
        self._peak_snr: Optional[float] = None

    def feed(
        self,
        cfg: ChannelConfig,
        gated_frame: np.ndarray,
        is_open: bool,
        rf_snr_db: Optional[float],
    ) -> None:
        """Call once per frame, after squelch.process()."""
        if not self._enabled:
            return
        if is_open and self._proc is None:
            self._start(cfg)
        if self._proc is None:
            return
        if is_open:
            self._write(gated_frame, cfg.sample_rate)
            if rf_snr_db is not None:
                self._peak_snr = rf_snr_db if self._peak_snr is None else max(self._peak_snr, rf_snr_db)
        else:
            self._finish()

    def close(self) -> None:
        """Finish any clip in progress (pipeline stopping / channel hop)."""
        if self._proc is not None:
            self._finish()

    # ---- internals ---------------------------------------------------------

    def _start(self, cfg: ChannelConfig) -> None:
        probe = self._base
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if shutil.disk_usage(probe).free < MIN_FREE_BYTES:
            print("RECORDER-SKIP low disk space", flush=True)
            return
        self._started = _dt.datetime.now()
        day_dir = self._base / self._started.strftime("%Y-%m-%d")
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"RECORDER-FAILURE mkdir {day_dir}: {e}", flush=True)
            return
        self._channel = cfg.channel
        self._tmp_path = day_dir / f".{self._started.strftime('%H%M%S')}_ch{cfg.channel}.partial.mp3"
        self._frames = 0
        self._peak_snr = None
        try:
            self._proc = subprocess.Popen(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "s16le", "-ar", str(cfg.sample_rate), "-ac", "1", "-i", "pipe:0",
                    "-af", build_filter_chain(cfg),
                    "-f", "mp3", "-b:a", "64k",
                    str(self._tmp_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            print(f"RECORDER-FAILURE ffmpeg: {e}", flush=True)
            self._proc = None

    def _write(self, frame: np.ndarray, sample_rate: int) -> None:
        try:
            self._proc.stdin.write(frame.tobytes())
            self._frames += 1
            self._frame_s = len(frame) / sample_rate
        except (BrokenPipeError, OSError) as e:
            print(f"RECORDER-FAILURE write: {e}", flush=True)
            self._abort()

    def _finish(self) -> None:
        proc, tmp = self._proc, self._tmp_path
        self._proc = None
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        duration = self._frames * self._frame_s
        if duration < MIN_CLIP_S or proc.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            return
        snr = "na" if self._peak_snr is None else f"{round(self._peak_snr):d}"
        final = tmp.parent / f"{self._started.strftime('%H%M%S')}_ch{self._channel}_{duration:.1f}s_{snr}dB.mp3"
        tmp.rename(final)
        print(f"RECORDER-SAVED {final.parent.name}/{final.name}", flush=True)
        self._prune()

    def _abort(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc = None
        if self._tmp_path is not None:
            self._tmp_path.unlink(missing_ok=True)

    def _prune(self) -> None:
        cutoff = (_dt.date.today() - _dt.timedelta(days=RETENTION_DAYS)).isoformat()
        try:
            for d in self._base.iterdir():
                if d.is_dir() and DAY_RE.match(d.name) and d.name < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def list_recordings(base_dir: Path = RECORDINGS_DIR, channel: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Newest-first clip metadata, parsed from filenames."""
    out: list[dict] = []
    base = Path(base_dir)
    if not base.is_dir():
        return out
    for day in sorted((d for d in base.iterdir() if d.is_dir() and DAY_RE.match(d.name)), reverse=True):
        for f in sorted(day.iterdir(), reverse=True):
            m = CLIP_RE.match(f.name)
            if not m or (channel and m["channel"] != channel):
                continue
            hms = m["hms"]
            when = f"{day.name}T{hms[:2]}:{hms[2:4]}:{hms[4:]}"
            out.append({
                "path": f"{day.name}/{f.name}",
                "time": when,
                # Epoch seconds (Pi local time -> UTC) so browsers in other
                # timezones can still say "4 min ago" correctly.
                "ts": _dt.datetime.fromisoformat(when).timestamp(),
                "channel": m["channel"],
                "duration_s": float(m["dur"]),
                "peak_rf_snr_db": None if m["snr"] == "na" else int(m["snr"]),
                "bytes": f.stat().st_size,
            })
            if len(out) >= limit:
                return out
    return out


def last_heard(base_dir: Path = RECORDINGS_DIR) -> dict[str, dict]:
    """Most recent clip per channel: {channel: {time, ts, duration_s,
    peak_rf_snr_db}}. Walks newest day folders first, so it's cheap."""
    out: dict[str, dict] = {}
    for clip in list_recordings(base_dir=base_dir, limit=100_000):
        if clip["channel"] not in out:
            out[clip["channel"]] = {k: clip[k] for k in ("time", "ts", "duration_s", "peak_rf_snr_db")}
    return out


def resolve_recording(day: str, name: str, base_dir: Path = RECORDINGS_DIR) -> Optional[Path]:
    """Validated path to a clip, or None. Only names the recorder itself
    produces are accepted, so no path traversal is possible."""
    if not DAY_RE.match(day) or not CLIP_RE.match(name):
        return None
    p = Path(base_dir) / day / name
    return p if p.is_file() else None
