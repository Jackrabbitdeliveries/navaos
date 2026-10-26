"""
Live camera feeds (2026-10-26) - one registry so more cameras, in other
cities, are just more entries. Each feed carries its own credit, which is
shown wherever its images are used (Kevin's agreement with the Bridge of
Lions cam owners: "credit See St. Augustine where possible").

Built-in defaults live here; overrides and additions go in
~/navaos-data/feeds.json ({feed_id: {...fields...}}), which stays on the Pi
because it may later hold private camera addresses (e.g. a direct RTSP or
snapshot URL the owner shares) that must not reach the public repo.

Fields: name, city, youtube_id, credit, credit_url, permission (text),
and - once known - lat, lon, bearing_deg (view direction, true), fov_deg,
used by the fixed-camera AIS matching.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

FEEDS_FILE = Path.home() / "navaos-data" / "feeds.json"
LEGACY_BRIDGE_FILE = Path.home() / "navaos-data" / "bridge_cam.json"   # pre-registry video-id setting
FEED_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

BUILTIN = {
    "bridge-of-lions": {
        "name": "Bridge of Lions live cam",
        "city": "St. Augustine, FL",
        "youtube_id": "uLJBad4vSno",
        "credit": "See St. Augustine",
        "credit_url": "https://www.youtube.com/@seestaug",
        "permission": "Stills or clips for Boat ID, credit the channel - granted by the camera and channel owners, 2026-10-26",
        # The camera pans/zooms; boats in its bridge shots are near this point.
        "watch_point": {"lat": 29.8925, "lon": -81.3086, "radius_m": 300, "label": "the Bridge of Lions"},
        # Channel description: zooms on the drawbridge every half hour for 10 min, 08:00-19:45.
        "zoom_schedule": {"start": "08:00", "end": "19:45", "minutes": [0, 30], "duration_min": 10},
    },
}
DEFAULT_FEED = "bridge-of-lions"


def _overrides() -> dict:
    try:
        d = json.loads(FEEDS_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def all_feeds() -> dict[str, dict]:
    feeds = {k: dict(v) for k, v in BUILTIN.items()}
    # carry over the video id saved by the Bridge page before the registry existed
    try:
        vid = json.loads(LEGACY_BRIDGE_FILE.read_text()).get("video_id")
        if vid and YT_ID_RE.match(vid):
            feeds[DEFAULT_FEED]["youtube_id"] = vid
    except (OSError, ValueError):
        pass
    for fid, v in _overrides().items():
        if FEED_ID_RE.match(fid) and isinstance(v, dict):
            feeds.setdefault(fid, {}).update(v)
    return feeds


def get(feed_id: str) -> Optional[dict]:
    f = all_feeds().get(feed_id)
    return {"id": feed_id, **f} if f else None


def in_zoom_window(feed: dict, t: float) -> bool:
    """Is the camera on its zoomed bridge shot at epoch time t (Pi local time)?"""
    z = feed.get("zoom_schedule")
    if not z:
        return False
    import datetime as dt
    d = dt.datetime.fromtimestamp(t)
    hm = d.strftime("%H:%M")
    if not (z["start"] <= hm <= z["end"]):
        return False
    return any(0 <= (d.minute - m) % 60 < z["duration_min"] for m in z["minutes"])


def public(feed_id: str) -> Optional[dict]:
    """What pages may see: never private connection details."""
    f = get(feed_id)
    if not f:
        return None
    return {k: f.get(k) for k in ("id", "name", "city", "youtube_id", "credit", "credit_url")}


def set_field(feed_id: str, **fields) -> dict:
    d = _overrides()
    d.setdefault(feed_id, {}).update(fields)
    FEEDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FEEDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(FEEDS_FILE)
    return public(feed_id)
