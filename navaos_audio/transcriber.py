"""
Background speech-to-text for recorded transmissions (2026-10-04).

Each saved clip is transcribed with whisper.cpp (base.en - tested 2026-10-04:
2.4x faster than real time on the Pi 5 and more reliable than small.en, see
CLAUDE.md) and the result is written beside the clip as
`<clip>.stt.json`: {"text", "vessels": [{"name", "mmsi"}], "model", "at"}.
The sidecar lives in the clip's day folder, so the recorder's 30-day
retention removes it with the clip.

One worker thread, niced, 2 threads for whisper, so the radio always has
CPU to spare. New clips (enqueue()) go first; a backfill of any clip without
a sidecar runs newest-first when the live queue is empty.

Vessel tags: spoken names fuzzy-matched against AIS names known at the time
of transcription (navaos_audio.ais VesselDB).

Env: NAVAOS_STT=0 disables (tests MUST set it), NAVAOS_WHISPER_BIN,
NAVAOS_WHISPER_MODEL.
"""
from __future__ import annotations

import collections
import difflib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .recorder import CLIP_RE, DAY_RE, RECORDINGS_DIR

WHISPER_BIN = Path(os.environ.get("NAVAOS_WHISPER_BIN", Path.home() / "src" / "whisper.cpp" / "build" / "bin" / "whisper-cli"))
WHISPER_MODEL = Path(os.environ.get("NAVAOS_WHISPER_MODEL", Path.home() / "src" / "whisper.cpp" / "models" / "ggml-base.en.bin"))
STT_ENABLED = os.environ.get("NAVAOS_STT", "1") != "0"
THREADS = 2
SUFFIX = ".stt.json"

# Non-speech tokens whisper emits for noise/silence.
_NOISE_RE = re.compile(r"\[[^\]]*\]|\((?:static|music|silence|noise|inaudible|beep|wind)[^)]*\)", re.I)
# Whisper's classic hallucinations on near-silent audio - drop if that's all there is.
_HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "okay", "thank you very much"}

MIN_RATIO = 0.82
TOO_COMMON = {"OFF AIR", "SOMEDAY", "MON CHERI"}


def sidecar(clip: Path) -> Path:
    return clip.with_name(clip.name + SUFFIX)


def read_sidecar(clip: Path) -> Optional[dict]:
    try:
        return json.loads(sidecar(clip).read_text())
    except (OSError, ValueError):
        return None


def clean(text: str) -> str:
    t = " ".join(_NOISE_RE.sub(" ", text).split())
    if t.lower().strip(" .!?,") in _HALLUCINATIONS:
        return ""
    return t


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def match_vessels(text: str, names: dict[str, int]) -> list[dict]:
    """Vessel names (from AIS) that appear in `text`, allowing close spellings."""
    words = _norm(text).split()
    hits = []
    for name, mmsi in names.items():
        if name.upper() in TOO_COMMON:
            continue
        target = _norm(name)
        n = len(target.split())
        if not n:
            continue
        best = 0.0
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n])
            if len(target.replace(" ", "")) < 4:
                r = 1.0 if phrase == target else 0.0
            else:
                r = difflib.SequenceMatcher(None, phrase, target).ratio()
            best = max(best, r)
        if best >= MIN_RATIO:
            hits.append((best, {"name": name, "mmsi": mmsi}))
    return [h for _, h in sorted(hits, key=lambda x: -x[0])]


def _ais_names() -> dict[str, int]:
    try:
        from .ais import ais_service
        return {v["name"]: v["mmsi"] for v in ais_service.db.snapshot(10 ** 9) if v.get("name")}
    except Exception:
        return {}


class Transcriber:
    def __init__(self, base_dir: Path = RECORDINGS_DIR):
        self._base = Path(base_dir)
        self._live: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self.done = 0
        self.failed = 0
        self.current: Optional[str] = None

    @property
    def available(self) -> bool:
        return WHISPER_BIN.is_file() and WHISPER_MODEL.is_file()

    def start(self) -> None:
        if not STT_ENABLED or self._thread is not None:
            return
        if not self.available:
            print(f"STT-DISABLED whisper not found ({WHISPER_BIN}, {WHISPER_MODEL})", flush=True)
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def enqueue(self, clip: Path) -> None:
        with self._cv:
            self._live.append(Path(clip))
            self._cv.notify()

    def _backlog(self) -> list[Path]:
        """Clips without a transcript, newest first."""
        out = []
        if not self._base.is_dir():
            return out
        for day in sorted((d for d in self._base.iterdir() if d.is_dir() and DAY_RE.match(d.name)), reverse=True):
            for f in sorted(day.iterdir(), reverse=True):
                if CLIP_RE.match(f.name) and not sidecar(f).exists():
                    out.append(f)
        return out

    def pending(self) -> int:
        return len(self._live) + len(self._backlog())

    def _next(self) -> Optional[Path]:
        with self._cv:
            if self._live:
                return self._live.popleft()
        back = self._backlog()
        if back:
            return back[0]
        with self._cv:
            self._cv.wait(timeout=60)
        return None

    def _run(self) -> None:
        try:
            os.nice(10)
        except OSError:
            pass
        while True:
            clip = self._next()
            if clip is None or not clip.exists() or sidecar(clip).exists():
                continue
            self.current = f"{clip.parent.name}/{clip.name}"
            try:
                self.transcribe(clip)
                self.done += 1
            except Exception as e:
                self.failed += 1
                print(f"STT-FAILURE {self.current}: {e!r}", flush=True)
                # write an empty sidecar so a clip that always fails doesn't block the backlog
                self._write(clip, {"text": "", "error": str(e)[:200]})
            finally:
                self.current = None

    def transcribe(self, clip: Path) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "a.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, timeout=60)
            r = subprocess.run([str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(wav), "-t", str(THREADS),
                                "-nt", "-np", "-l", "en"], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(f"whisper exit {r.returncode}: {r.stderr.strip()[-200:]}")
        text = clean(r.stdout)
        data = {"text": text, "vessels": match_vessels(text, _ais_names()) if text else [],
                "model": WHISPER_MODEL.name, "at": time.time()}
        self._write(clip, data)
        return data

    def _write(self, clip: Path, data: dict) -> None:
        f = sidecar(clip)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(f)


transcriber = Transcriber()
