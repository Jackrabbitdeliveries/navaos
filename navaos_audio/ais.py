"""
AIS (ship positions) on the shared dongle, in short timeshared windows.

AISService runs AIS-catcher (built in ~/src/AIS-catcher/build, see CLAUDE.md)
for one window at a time and feeds every decoded message into VesselDB.
*When* a window runs is decided by navaos_audio.control (voice always wins);
this module only knows how to collect and remember.

VesselDB keeps one record per MMSI: static data (name, type, size...) is
kept for RETAIN_DAYS so short windows rarely need to re-learn names; the
latest position plus a thinned track of the last TRACK_HOURS. Saved to
~/navaos-data/ais/vessels.json (atomic replace) at the end of each window.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

AIS_BIN = Path(os.environ.get("NAVAOS_AIS_BIN", Path.home() / "src" / "AIS-catcher" / "build" / "AIS-catcher"))
DATA_DIR = Path(os.environ.get("NAVAOS_AIS_DIR", Path.home() / "navaos-data" / "ais"))
DB_FILE = DATA_DIR / "vessels.json"
RETAIN_DAYS = 30
TRACK_HOURS = 24
TRACK_MIN_M = 40          # keep a track point only after moving this far...
TRACK_MIN_S = 600         # ...or this long since the last kept point

STATIC_KEYS = ("shipname", "callsign", "imo", "shiptype", "to_bow", "to_stern", "to_port",
               "to_starboard", "draught", "destination")


def _metres(lat1, lon1, lat2, lon2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return 111_320 * math.hypot(lat2 - lat1, (lon2 - lon1) * k)


def _kind(msg_type: int, mmsi: int) -> str:
    s = str(mmsi)
    if s.startswith("970"):
        return "sart"
    if s.startswith("972") or s.startswith("974"):
        return "mob"
    if msg_type in (1, 2, 3, 5, 27):
        return "A"
    if msg_type in (18, 19, 24):
        return "B"
    if msg_type in (4, 11):
        return "base"
    if msg_type == 21:
        return "aton"
    return "other"


class VesselDB:
    def __init__(self, path: Path = DB_FILE):
        self._path = path
        self._lock = threading.Lock()
        self.vessels: dict[str, dict] = {}
        try:
            self.vessels = json.loads(path.read_text())
        except (OSError, ValueError):
            pass

    def update(self, m: dict, now: Optional[float] = None) -> None:
        mmsi = m.get("mmsi")
        if not mmsi:
            return
        now = now or time.time()
        with self._lock:
            v = self.vessels.setdefault(str(mmsi), {"mmsi": mmsi, "track": []})
            kind = _kind(m.get("type", 0), mmsi)
            if v.get("kind") in (None, "other") or kind in ("A", "B", "sart", "mob"):
                v["kind"] = kind
            v["last_seen"] = now
            v["msgs"] = v.get("msgs", 0) + 1
            name = (m.get("shipname") or m.get("name") or "").strip()
            if name:
                v["name"] = name
            for k in STATIC_KEYS[1:]:
                val = m.get(k)
                if val not in (None, "", 0):
                    v[k] = val.strip() if isinstance(val, str) else val
            if m.get("type") in (1, 2, 3) and m.get("status") is not None:
                v["status"] = m["status"]
            if m.get("signalpower") is not None:
                v["power"] = round(m["signalpower"], 1)
            lat, lon = m.get("lat"), m.get("lon")
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
                return
            v.update(lat=round(lat, 6), lon=round(lon, 6), pos_ts=now)
            for k, key in (("speed", "sog"), ("course", "cog"), ("heading", "heading")):
                val = m.get(k)
                if val is not None:
                    v[key] = val
            tr = v["track"]
            if not tr or now - tr[-1][0] >= TRACK_MIN_S or _metres(tr[-1][1], tr[-1][2], lat, lon) >= TRACK_MIN_M:
                tr.append([round(now), round(lat, 6), round(lon, 6)])

    def prune(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        with self._lock:
            for k in [k for k, v in self.vessels.items() if now - v.get("last_seen", 0) > RETAIN_DAYS * 86400]:
                del self.vessels[k]
            for v in self.vessels.values():
                v["track"] = [p for p in v.get("track", []) if now - p[0] <= TRACK_HOURS * 3600]

    def save(self) -> None:
        with self._lock:
            data = json.dumps(self.vessels)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(self._path)

    def snapshot(self, max_age_s: float, with_track: bool = False) -> list[dict]:
        cut = time.time() - max_age_s
        with self._lock:
            out = []
            for v in self.vessels.values():
                if v.get("last_seen", 0) < cut:
                    continue
                d = {k: val for k, val in v.items() if k != "track"}
                if with_track:
                    d["track"] = list(v.get("track", []))
                out.append(d)
            return out

    def get(self, mmsi: str) -> Optional[dict]:
        with self._lock:
            v = self.vessels.get(str(mmsi))
            return json.loads(json.dumps(v)) if v else None


class AISService:
    def __init__(self, db: Optional[VesselDB] = None):
        self.db = db or VesselDB()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self.window_started: Optional[float] = None
        self.last_window: Optional[dict] = None   # {start, end, msgs, vessels}
        self._msgs = 0
        self._mmsis: set = set()
        self.error: Optional[str] = None

    @property
    def available(self) -> bool:
        return AIS_BIN.is_file() and os.access(AIS_BIN, os.X_OK)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        if self.running:
            return True
        if not self.available:
            self.error = f"AIS-catcher not found at {AIS_BIN}"
            return False
        self.error = None
        self._msgs, self._mmsis = 0, set()
        self.window_started = time.time()
        self._proc = subprocess.Popen(
            [str(AIS_BIN), "-d:0", "-o", "5", "-M", "DT"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._thread.start()
        return True

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            if not line.startswith("{"):
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            self.db.update(m)
            self._msgs += 1
            if m.get("mmsi"):
                self._mmsis.add(m["mmsi"])
        rc = proc.poll()
        if rc not in (None, 0, -15):
            tail = (proc.stderr.read() or "").strip().splitlines()[-3:]
            self.error = " / ".join(tail) or f"AIS-catcher exited ({rc})"
            print(f"AIS-FAILURE {self.error}", flush=True)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self.last_window = {"start": self.window_started, "end": time.time(),
                            "msgs": self._msgs, "vessels": len(self._mmsis)}
        self.window_started = None
        print(f"AIS-WINDOW msgs={self._msgs} vessels={len(self._mmsis)}", flush=True)
        try:
            self.db.prune()
            self.db.save()
        except OSError as e:
            print(f"AIS-SAVE-FAILED {e}", flush=True)


ais_service = AISService()
