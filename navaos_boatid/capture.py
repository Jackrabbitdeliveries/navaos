"""
Grab still frames from a live camera feed (2026-10-26).

Used with the camera owners' permission (see feeds.py / CLAUDE.md): the user
"framing" a boat from the Bridge page, and the automatic Bridge watch.

YouTube feeds: yt-dlp (own venv, ~/src/ytdlp-venv) resolves the live HLS
URL - cached, since resolving takes ~2 s and the URL stays valid for hours -
then ffmpeg grabs one full-size frame (1920x1080), re-encoded, which also
drops all metadata. The copy the page sees is scaled to <=1568 px wide
(Claude's recommended size); the full-size frame is kept in memory for
FULL_TTL_S under a random token so the user's zoom box can be cut from it
(about 20% more detail in the close-up the AI sees). One grab at a time,
at low CPU priority, so it can't crowd the radio.
"""
from __future__ import annotations

import os
import secrets
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
FULL_TTL_S = 30 * 60

_lock = threading.Lock()
_full: dict[str, tuple[float, bytes]] = {}     # token -> (grabbed at, full-size JPEG)
_full_lock = threading.Lock()
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
                        "-i", url, "-frames:v", "1",
                        "-map_metadata", "-1", "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
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
    return scale(jpeg, MAX_W), t


def grab_full(feed_id: str) -> tuple[bytes, float, str]:
    """Like grab(), plus a token for the full-size frame (see cut())."""
    feed = feeds.get(feed_id)
    if feed is None:
        raise CaptureError("Unknown feed.")
    with _lock:
        url = _resolve(feed)
        t = time.time()
        try:
            full = _ffmpeg_frame(url)
        except (CaptureError, subprocess.TimeoutExpired):
            url = _resolve(feed, fresh=True)
            t = time.time()
            try:
                full = _ffmpeg_frame(url)
            except subprocess.TimeoutExpired:
                raise CaptureError("The live stream didn't respond in time.")
    token = secrets.token_urlsafe(12)
    with _full_lock:
        now = time.time()
        for k in [k for k, (at, _) in _full.items() if now - at > FULL_TTL_S]:
            del _full[k]
        while len(_full) >= 20:                     # a few MB at most
            del _full[min(_full, key=lambda k: _full[k][0])]
        _full[token] = (now, full)
    return scale(full, MAX_W), t, token


def _ffmpeg(jpeg: bytes, vf: str, q: int = 3) -> bytes:
    r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "image2pipe", "-vcodec", "mjpeg", "-i", "-", "-vf", vf, "-frames:v", "1",
                        "-map_metadata", "-1", "-q:v", str(q), "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                       input=jpeg, capture_output=True, timeout=20)
    if r.returncode != 0 or not r.stdout.startswith(b"\xff\xd8"):
        raise CaptureError("ffmpeg couldn't process the image")
    return r.stdout


def scale(jpeg: bytes, max_w: int) -> bytes:
    """Shrink a JPEG to <= max_w px wide (unchanged if already small enough)."""
    size = image_size(jpeg)
    if size and size[0] <= max_w:
        return jpeg
    return _ffmpeg(jpeg, f"scale='min({max_w},iw)':-2")


def fit(jpeg: bytes, max_side: int) -> bytes:
    """Shrink a JPEG so neither side exceeds max_side (never enlarges)."""
    size = image_size(jpeg)
    if size and max(size) <= max_side:
        return jpeg
    return _ffmpeg(jpeg, f"scale=w={max_side}:h={max_side}:force_original_aspect_ratio=decrease")


def crop_box(jpeg: bytes, box: dict, max_w: int = 0) -> bytes:
    """Cut box {x,y,w,h} (0..1) out of a JPEG, optionally shrunk to <= max_w px wide."""
    size = image_size(jpeg)
    if not size:
        raise CaptureError("Unreadable image.")
    W, H = size
    x, y = int(box["x"] * W), int(box["y"] * H)
    w, h = max(2, min(W - x, round(box["w"] * W))), max(2, min(H - y, round(box["h"] * H)))
    vf = f"crop={w}:{h}:{x}:{y}" + (f",scale='min({max_w},iw)':-2" if max_w else "")
    return _ffmpeg(jpeg, vf, q=2)


def full_frame(token: str) -> Optional[bytes]:
    """The full-size frame grabbed under this token, if it's still kept."""
    with _full_lock:
        hit = _full.get(token or "")
    return hit[1] if hit else None


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
