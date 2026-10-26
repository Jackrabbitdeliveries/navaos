"""
Grab still frames from a live camera feed (2026-10-26).

Used with the camera owners' permission (see feeds.py / CLAUDE.md): the user
"framing" a boat from the Bridge page, and the automatic Bridge watch.

YouTube feeds: yt-dlp (own venv, ~/src/ytdlp-venv) resolves the live HLS
URL - cached, since resolving takes ~2 s and the URL stays valid for hours -
then ffmpeg grabs one frame, scaled to <=1568 px wide (Claude's recommended
size) and re-encoded, which also drops all metadata. One grab at a time,
at low CPU priority, so it can't crowd the radio.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import feeds

YTDLP = Path(os.environ.get("NAVAOS_YTDLP", Path.home() / "src" / "ytdlp-venv" / "bin" / "yt-dlp"))
FORMAT = "270/232/best"          # 1080p, else 720p, else whatever there is
URL_TTL_S = 30 * 60
MAX_W = 1568

_lock = threading.Lock()
_url_cache: dict[str, tuple[float, str]] = {}


class CaptureError(Exception):
    pass


def _resolve(feed: dict, fresh: bool = False) -> str:
    vid = feed.get("youtube_id")
    if not vid:
        raise CaptureError("This feed has no stream address.")
    hit = _url_cache.get(vid)
    if hit and not fresh and time.time() - hit[0] < URL_TTL_S:
        return hit[1]
    if not YTDLP.is_file():
        raise CaptureError("Frame grabber (yt-dlp) isn't installed on the Pi.")
    r = subprocess.run(["nice", "-n", "10", str(YTDLP), "-q", "--no-warnings", "-g", "-f", FORMAT,
                        f"https://www.youtube.com/watch?v={vid}"], capture_output=True, text=True, timeout=45)
    url = (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else ""
    if r.returncode != 0 or not url.startswith("https://"):
        raise CaptureError("Couldn't reach the live stream (" + ((r.stderr or "").strip().splitlines() or ["no detail"])[-1][:120] + ")")
    _url_cache[vid] = (time.time(), url)
    return url


def _ffmpeg_frame(url: str) -> bytes:
    r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-i", url, "-frames:v", "1", "-vf", f"scale='min({MAX_W},iw)':-2",
                        "-map_metadata", "-1", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                       capture_output=True, timeout=40)
    if r.returncode != 0 or not r.stdout.startswith(b"\xff\xd8"):
        raise CaptureError("ffmpeg couldn't read a frame")
    return r.stdout


def grab(feed_id: str) -> tuple[bytes, float]:
    """One JPEG frame from the feed, and the time it was grabbed (epoch s)."""
    feed = feeds.get(feed_id)
    if feed is None:
        raise CaptureError("Unknown feed.")
    with _lock:
        url = _resolve(feed)
        t = time.time()
        try:
            jpeg = _ffmpeg_frame(url)
        except (CaptureError, subprocess.TimeoutExpired):
            url = _resolve(feed, fresh=True)       # the cached stream URL may have expired
            t = time.time()
            try:
                jpeg = _ffmpeg_frame(url)
            except subprocess.TimeoutExpired:
                raise CaptureError("The live stream didn't respond in time.")
    return jpeg, t


def image_size(jpeg: bytes) -> Optional[tuple[int, int]]:
    """Width/height from a baseline/progressive JPEG header (no image library needed)."""
    i = 2
    while i + 9 < len(jpeg):
        if jpeg[i] != 0xFF:
            return None
        marker, seg = jpeg[i + 1], int.from_bytes(jpeg[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(jpeg[i + 7:i + 9], "big"), int.from_bytes(jpeg[i + 5:i + 7], "big")
        i += 2 + seg
    return None
