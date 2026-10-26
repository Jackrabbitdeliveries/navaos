"""
Bridge watch (2026-10-26): automatic Boat ID from the Bridge of Lions live cam.

During the camera's daytime bridge close-ups (feeds.in_zoom_window - 10 min
every half hour, which is when the bridge opens), it asks the radio for AIS
bursts and watches for AIS boats that are moving within WATCH_RADIUS_M of
the bridge. Each such boat (once per window, at most MAX_PER_WINDOW) gets a
frame grabbed from the stream and saved as a credited camera sighting
(source=feed); the usual AI identification (inside the daily budget) and
AIS reveal then run on it, so it shows up in My boat IDs.

Boats without AIS can't trigger it (v1). With one dongle, AIS bursts don't
run while someone is listening - then there may be nothing fresh to trigger on.

Env: NAVAOS_BRIDGE_WATCH=0 disables (tests and spare instances MUST set it).
"""
from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
from typing import Optional

from . import capture, feeds, identify, reveal, store

ENABLED = os.environ.get("NAVAOS_BRIDGE_WATCH", "1") != "0"
FEED_ID = feeds.DEFAULT_FEED
CLIENT = "bridge-watch"
TICK_S = 5
WATCH_RADIUS_M = 250
FRESH_FIX_S = 180
AIS_BURST_EVERY_S = 120
MAX_PER_WINDOW = 6


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return 111320 * math.hypot(lat2 - lat1, (lon2 - lon1) * k)


class BridgeWatch:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self.window_key: Optional[str] = None
        self.captured_this_window: set[int] = set()
        self.last_ais_request = 0.0
        self.last_capture: Optional[dict] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if not ENABLED or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="bridge-watch")
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as e:   # never let the watcher die
                self.last_error = repr(e)[:200]
                print(f"BRIDGEWATCH-ERROR {e!r}", flush=True)
            time.sleep(TICK_S)

    def _tick(self) -> None:
        feed = feeds.get(FEED_ID)
        now = time.time()
        if not feed or not feeds.in_zoom_window(feed, now):
            self.window_key = None
            return
        key = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:") + ("00" if dt.datetime.fromtimestamp(now).minute < 30 else "30")
        if key != self.window_key:
            self.window_key, self.captured_this_window = key, set()
            print(f"BRIDGEWATCH-WINDOW {key}", flush=True)
        if now - self.last_ais_request > AIS_BURST_EVERY_S:
            self.last_ais_request = now
            self._request_ais()
        if len(self.captured_this_window) >= MAX_PER_WINDOW:
            return
        wp = feed["watch_point"]
        from navaos_audio.ais import ais_service
        own = reveal._kit().get("own_mmsi")
        for v in ais_service.db.snapshot(FRESH_FIX_S):
            mmsi = v.get("mmsi")
            if mmsi in self.captured_this_window or mmsi == own or v.get("lat") is None:
                continue
            if now - (v.get("pos_ts") or 0) > FRESH_FIX_S or not reveal._moving(v):
                continue
            if _dist_m(wp["lat"], wp["lon"], v["lat"], v["lon"]) > WATCH_RADIUS_M:
                continue
            self.captured_this_window.add(mmsi)
            self._capture(v)
            break   # one grab per tick; the next boat waits ~5 s

    def _request_ais(self) -> None:
        try:
            from navaos_audio.control import control
            control.ais_now(CLIENT, None)
        except Exception as e:   # someone listening / radio busy - fine, try again later
            print(f"BRIDGEWATCH-AIS-SKIPPED {e}", flush=True)

    def _capture(self, v: dict) -> None:
        jpeg, t = capture.grab(FEED_ID)
        w, h = capture.image_size(jpeg) or (None, None)
        sid = store.new_id()
        store.insert({
            "id": sid, "created_at": time.time(), "kit_id": store.kit_id(), "client": CLIENT,
            "source": "feed", "feed_id": FEED_ID, "taken_at": t, "photo": store.save_photo(sid, jpeg),
            "img_w": w, "img_h": h, "tap_x": None, "tap_y": None,
            "sensors": {"auto": "bridge-watch", "trigger_mmsi": v.get("mmsi"), "trigger_name": v.get("name"),
                        "trigger_sog": v.get("sog")},
            "guess": {"hull": "unknown", "rig": "unknown", "size": "unknown", "model": ""},
            "ai_status": "pending",
        })
        identify.start(sid)
        reveal.start(sid)
        self.last_capture = {"id": sid, "at": t, "name": v.get("name"), "mmsi": v.get("mmsi")}
        print(f"BRIDGEWATCH-CAPTURE {sid} {v.get('name')} ({v.get('mmsi')}) sog={v.get('sog')}", flush=True)

    def status(self) -> dict:
        feed = feeds.get(FEED_ID) or {}
        now = time.time()
        nxt = None
        if feed.get("zoom_schedule"):
            t = now - now % 60
            for _ in range(24 * 60):                    # next minute that is inside a window
                t += 60
                if feeds.in_zoom_window(feed, t):
                    nxt = t
                    break
        start = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        today = [r for r in store.recent(500) if r["created_at"] >= start and r.get("client") == CLIENT]
        return {"enabled": ENABLED, "in_window": bool(feed) and feeds.in_zoom_window(feed, now),
                "next_window": None if (feed and feeds.in_zoom_window(feed, now)) else nxt,
                "captured_today": len(today), "last_capture": self.last_capture, "last_error": self.last_error}


bridge_watch = BridgeWatch()
