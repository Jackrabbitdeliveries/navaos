"""
Live RF noise meter for antenna placement (2026-10-01).

Runs `rtl_power` continuously over the marine band (156.3-156.9 MHz, 6.25 kHz
bins, 1 s integration, gain 49.6 - the exact settings of the 2026-09-30/10-01
baseline measurements, so readings compare directly with them) and keeps the
last HISTORY_S seconds of readings:

  floor_db  - band-median bin power (rtl_power relative units)
  channels  - per-channel power vs. that floor, dB (same maths as
              tools/rf_baseline.py), so traffic shows up too

The useful number is floor_db minus the *reference*: the dongle's own floor
with the coax disconnected (-30.4 dB measured 2026-10-01). Within 1-2 dB of
it = an RF-quiet antenna spot; the first roof-line spot read +6.8 dB.

It needs the dongle exclusively, so it only runs as a radio "mode" started
through navaos_audio.control (selection mode "noise").
"""
from __future__ import annotations

import collections
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

RANGE = "156.3M:156.9M:6250"
GAIN = "49.6"
HISTORY_S = 600
HALF_BW_HZ = 6000
CHANNELS_HZ = {"68": 156.425e6, "09": 156.450e6, "71": 156.575e6, "13": 156.650e6, "16": 156.800e6}
DEFAULT_REFERENCE_DB = -30.4
REFERENCE_FILE = Path(os.environ.get("NAVAOS_NOISE_REF_FILE", Path.home() / "navaos-data" / "noise_reference_db"))


def load_reference() -> float:
    try:
        return float(REFERENCE_FILE.read_text().strip())
    except (OSError, ValueError):
        return DEFAULT_REFERENCE_DB


def save_reference(db: float) -> None:
    REFERENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_FILE.write_text(f"{db:.1f}\n")


def parse_line(line: str) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """One rtl_power CSV row -> (bin centre freqs, dB values)."""
    p = [x.strip() for x in line.split(",")]
    if len(p) < 7:
        return None
    try:
        lo, step = float(p[2]), float(p[4])
        vals = np.array([float(v) for v in p[6:]])
    except ValueError:
        return None
    return lo + step * (np.arange(len(vals)) + 0.5), vals


def reading(freqs: np.ndarray, vals: np.ndarray) -> dict:
    floor = float(np.median(vals))
    chans = {}
    for k, f in CHANNELS_HZ.items():
        m = np.abs(freqs - f) <= HALF_BW_HZ
        n = int(m.sum())
        if n:
            ch_db = 10 * np.log10(np.sum(10 ** (vals[m] / 10)))
            chans[k] = round(float(ch_db - (floor + 10 * np.log10(n))), 1)
    return {"ts": time.time(), "floor_db": round(floor, 2), "channels": chans}


class NoiseMeter:
    def __init__(self, device_index: int = 0):
        self._device_index = device_index
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._history: collections.deque = collections.deque()
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.error = None
        with self._lock:
            self._history.clear()
        self._proc = subprocess.Popen(
            ["rtl_power", "-d", str(self._device_index), "-f", RANGE, "-g", GAIN, "-i", "1", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._thread.start()

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            parsed = parse_line(line)
            if parsed is None:
                continue
            r = reading(*parsed)
            with self._lock:
                self._history.append(r)
                while self._history and r["ts"] - self._history[0]["ts"] > HISTORY_S:
                    self._history.popleft()
        if proc.poll() not in (None, 0, -15):
            tail = (proc.stderr.read() or "").strip().splitlines()[-3:]
            self.error = " / ".join(tail) or f"rtl_power exited ({proc.returncode})"
            print(f"NOISEMETER-FAILURE {self.error}", flush=True)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def readings(self, since: float = 0.0) -> list[dict]:
        with self._lock:
            return [r for r in self._history if r["ts"] > since]


noise_meter = NoiseMeter()
