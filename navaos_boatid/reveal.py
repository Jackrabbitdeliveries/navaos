"""
AIS reveal (Boat ID piece 3): which AIS-broadcasting boat was in the photo?

Inputs per sighting: the phone's GPS position + accuracy, the back camera's
magnetic heading, where the user tapped the boat in the frame, the photo
time, and the AI's reading of any name on the hull. Positions come from the
AIS VesselDB (navaos_audio.ais), which only has fixes from timeshared
windows - so each vessel's position at the photo time is *estimated*, with
an uncertainty that grows with the age of the fix (docs/boat-id-plan.md 3.4).

Status: unavailable (no GPS/heading) | match (one clear boat) |
        ambiguous (a few fit - user picks) | none (no AIS boat in view).
A background thread re-runs the match until an AIS fix *after* the photo
exists (or ~13 min pass), then marks it final.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Optional

from . import store

KIT_FILE = Path.home() / "navaos-data" / "kit.json"
DEFAULT_DECLINATION = -7.6     # St. Augustine, degrees (west negative): true = magnetic + declination
DEFAULT_COMPASS_ERR = 20.0     # degrees, 1-sigma-ish, when the phone gives no accuracy
MAX_RANGE_M = 4000             # an identifiable photo is rarely farther than this
NAME_RANGE_M = 8000
NEARBY_M = 1500                # "pick from nearby boats" list, any direction            # a matching hull name counts within this distance
FINAL_WAIT_S = 13 * 60
M_PER_NM = 1852.0
KN_TO_MPS = 0.514444


def _kit() -> dict:
    try:
        return json.loads(KIT_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def _dist_bearing(lat1, lon1, lat2, lon2) -> tuple[float, float]:
    """Metres and true bearing (deg) from point 1 to point 2."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * 6371000 * math.asin(math.sqrt(a))
    b = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return d, (b + 360) % 360


def _ang_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _moving(v: dict) -> bool:
    sog = v.get("sog")
    return sog is not None and 0.5 <= sog < 102.3 and v.get("status") not in (1, 5)


def estimate_position(v: dict, t: float) -> Optional[dict]:
    """Where vessel v probably was at time t: {lat, lon, err_m, method, age_s}."""
    pts = sorted(v.get("track") or [])
    if v.get("lat") is not None and v.get("pos_ts"):
        pts.append([v["pos_ts"], v["lat"], v["lon"]])
        pts.sort()
    if not pts:
        return None
    before = [p for p in pts if p[0] <= t]
    after = [p for p in pts if p[0] > t]
    b = before[-1] if before else None
    a = after[0] if after else None
    moving = _moving(v)

    if not moving:
        # Moored / anchored / drifting slowly: last known spot, plus swing room.
        p = b or a
        age = abs(t - p[0])
        if age > 24 * 3600:
            return None
        return {"lat": p[1], "lon": p[2], "err_m": 30.0, "method": "moored", "age_s": round(age)}   # swing room on a mooring

    if b and a and a[0] - b[0] <= 20 * 60:
        f = (t - b[0]) / max(1.0, a[0] - b[0])
        lat, lon = b[1] + f * (a[1] - b[1]), b[2] + f * (a[2] - b[2])
        seg, _ = _dist_bearing(b[1], b[2], a[1], a[2])
        return {"lat": lat, "lon": lon, "err_m": 40.0 + 0.25 * seg, "method": "between fixes",
                "age_s": round(min(t - b[0], a[0] - t))}
    p = b or a
    age = t - p[0]                         # positive: fix is older than the photo
    if abs(age) > 10 * 60:
        return None                        # a mover with no recent fix: position unknown
    sog, cog = v.get("sog") or 0, v.get("cog")
    lat, lon = p[1], p[2]
    if cog is not None and cog < 360:
        d = sog * KN_TO_MPS * age          # project forward (or back) along course
        lat += d * math.cos(math.radians(cog)) / 111320
        lon += d * math.sin(math.radians(cog)) / (111320 * math.cos(math.radians(lat)))
    return {"lat": lat, "lon": lon, "err_m": 40.0 + 0.35 * sog * KN_TO_MPS * abs(age),
            "method": "projected" if age >= 0 else "from later fix", "age_s": round(abs(age))}


def size_class(length_m: float) -> str:
    ft = length_m * 3.281
    return "under_30" if ft < 30 else "30_39" if ft < 40 else "40_49" if ft < 50 else "50_plus"


def match_feed(row: dict, vessels: list[dict]) -> dict:
    """Camera-feed stills: no phone position or compass. The feed's watch
    point (e.g. the Bridge of Lions) stands in for "where the boat is":
    AIS boats within its radius at the frame time are the candidates."""
    from . import feeds
    feed = feeds.get(row.get("feed_id") or "") or {}
    wp = feed.get("watch_point")
    if not wp:
        return {"status": "unavailable", "reason": "This camera has no known viewpoint for AIS matching yet."}
    t = row.get("taken_at") or row.get("created_at")
    hull_name = _norm((row.get("ai") or {}).get("visible_name", ""))
    own = _kit().get("own_mmsi")
    cands, nearby = [], []
    for v in vessels:
        if own and v.get("mmsi") == own:
            continue
        pos = estimate_position(v, t)
        if pos is None:
            continue
        dist, brg = _dist_bearing(wp["lat"], wp["lon"], pos["lat"], pos["lon"])
        name = v.get("name") or ""
        length = (v.get("to_bow") or 0) + (v.get("to_stern") or 0)
        name_hit = bool(hull_name) and len(hull_name) >= 3 and (
            _norm(name) == hull_name or difflib.SequenceMatcher(None, _norm(name), hull_name).ratio() >= 0.85)
        entry = {"mmsi": v["mmsi"], "name": name, "distance_m": round(dist), "bearing": round(brg),
                 "length_m": length if length > 1 else None, "sailing": v.get("shiptype") == 36}
        if dist <= NEARBY_M:
            nearby.append(entry)
        if name_hit or dist <= wp.get("radius_m", 300) + pos["err_m"]:
            cands.append({**entry, "shiptype": v.get("shiptype"), "kind": v.get("kind"),
                          "size_class": size_class(length) if length > 1 else None, "off_deg": None,
                          "pos_method": pos["method"], "pos_age_s": pos["age_s"],
                          "z": 0.0 if name_hit else round(dist / max(1, wp.get("radius_m", 300)), 2),
                          "name_match": name_hit, "moving": _moving(v)})
    # moving boats first (they're the ones going through the bridge), then nearest
    cands.sort(key=lambda c: (not c["name_match"], not c["moving"], c["distance_m"]))
    nearby.sort(key=lambda n: n["distance_m"])
    base = {"viewpoint": wp.get("label"), "candidates": cands[:6], "nearby": nearby[:15],
            "computed_at": time.time(), "aim_true_deg": None}
    if not cands:
        return {**base, "status": "none"}
    if cands[0]["name_match"] or len(cands) == 1:
        return {**base, "status": "match"}
    return {**base, "status": "ambiguous"}


def match(row: dict, vessels: list[dict]) -> dict:
    if row.get("source") == "feed":
        return match_feed(row, vessels)
    s = row.get("sensors") or {}
    t = row.get("taken_at") or row.get("created_at")
    ai = row.get("ai") or {}
    hull_name = _norm(ai.get("visible_name", ""))
    if s.get("lat") is None:
        return {"status": "unavailable", "reason": "No GPS position with this photo, so there's no way to place it among AIS boats."}

    kit = _kit()
    decl = float(kit.get("declination_deg", DEFAULT_DECLINATION))
    heading = s.get("heading_deg")
    aim = None
    if heading is not None:
        aim = (heading + decl) % 360
        if row.get("tap_x") is not None:
            fov = 69.0 if (row.get("img_w") or 0) > (row.get("img_h") or 0) else 50.0   # rough phone main-camera FOV
            off = math.degrees(math.atan((row["tap_x"] - 0.5) * 2 * math.tan(math.radians(fov / 2))))
            aim = (aim + off) % 360
    acc = s.get("compass_accuracy_deg")
    cerr = float(acc) if isinstance(acc, (int, float)) and 0 < acc < 90 else DEFAULT_COMPASS_ERR
    gps_err = float(s.get("accuracy_m") or 30)

    own = kit.get("own_mmsi")
    cands, nearby = [], []
    for v in vessels:
        if own and v.get("mmsi") == own:
            continue                     # never offer the kit's own boat (Nava) as the answer
        pos = estimate_position(v, t)
        if pos is None:
            continue
        dist, brg = _dist_bearing(s["lat"], s["lon"], pos["lat"], pos["lon"])
        name = v.get("name") or ""
        length = (v.get("to_bow") or 0) + (v.get("to_stern") or 0)
        if dist <= NEARBY_M:
            nearby.append({"mmsi": v["mmsi"], "name": name, "distance_m": round(dist), "bearing": round(brg),
                           "length_m": length if length > 1 else None, "sailing": v.get("shiptype") == 36})
        name_hit = bool(hull_name) and len(hull_name) >= 3 and (
            _norm(name) == hull_name or difflib.SequenceMatcher(None, _norm(name), hull_name).ratio() >= 0.85)
        if name_hit and dist <= NAME_RANGE_M:
            z = 0.0
        else:
            if aim is None or dist > MAX_RANGE_M:
                continue
            # angular tolerance: compass error + the position uncertainty seen from here
            # (capped: a boat 20 m away with ±60 m uncertainty would otherwise "fit" any direction)
            tol = cerr + min(15.0, math.degrees(math.atan((pos["err_m"] + gps_err) / max(dist, 1.0))))
            z = _ang_diff(brg, aim) / tol
            if z > 1.5:
                continue
        cands.append({
            "mmsi": v["mmsi"], "name": name, "shiptype": v.get("shiptype"), "kind": v.get("kind"),
            "length_m": length if length > 1 else None,
            "size_class": size_class(length) if length > 1 else None,
            "distance_m": round(dist), "bearing": round(brg), "off_deg": None if aim is None else round(_ang_diff(brg, aim)),
            "pos_method": pos["method"], "pos_age_s": pos["age_s"], "z": round(z, 2), "name_match": name_hit,
            "sailing": v.get("shiptype") == 36,
        })
    # Rank: name matches first, then how centred in the aim, gently preferring sailboats and nearer boats.
    cands.sort(key=lambda c: (not c["name_match"], c["z"] - (0.15 if c["sailing"] else 0) + c["distance_m"] / 20000))
    cands = cands[:6]
    nearby.sort(key=lambda n: n["distance_m"])
    base = {"aim_true_deg": None if aim is None else round(aim), "compass_err_deg": cerr, "declination_deg": decl,
            "candidates": cands, "nearby": nearby[:15], "computed_at": time.time()}
    if aim is None and not cands:
        return {**base, "status": "unavailable", "reason": "No compass direction with this photo, so there's no way to tell which way you were pointing."}
    if not cands:
        return {**base, "status": "none"}
    best = cands[0]
    clear = best["z"] <= 1.0 and (len(cands) == 1 or cands[1]["z"] - best["z"] >= 0.6)
    if best["name_match"] or clear:
        return {**base, "status": "match"}
    return {**base, "status": "ambiguous"}


# ---- background: re-run until there's an AIS fix after the photo ---------------

def _vessels() -> list[dict]:
    from navaos_audio.ais import ais_service
    return ais_service.db.snapshot(26 * 3600, with_track=True)


def _last_window_end() -> float:
    try:
        from navaos_audio.ais import ais_service
        return (ais_service.last_window or {}).get("end") or 0
    except Exception:
        return 0


def run(sid: str) -> None:
    deadline = time.time() + FINAL_WAIT_S
    while True:
        row = store.get(sid)
        if row is None:
            return
        prev = row.get("ais") or {}
        if prev.get("user_choice") is not None:
            return                                   # the user already decided - don't overwrite
        t = row.get("taken_at") or row["created_at"]
        try:
            res = match(row, _vessels())
        except Exception as e:
            res = {"status": "error", "reason": f"AIS matching failed: {e!r}"[:200]}
        final = res["status"] in ("unavailable", "error") or _last_window_end() > t + 20 or time.time() > deadline
        res["final"] = final
        if not final:
            res["waiting_reason"] = "Waiting for the next AIS update after your photo"
        latest = store.get(sid) or {}
        if (latest.get("ais") or {}).get("user_choice") is not None:
            return                                   # user answered while we were computing
        store.update(sid, ais=res)
        if final:
            print(f"BOATID-AIS {sid} {res['status']} cands={len(res.get('candidates') or [])}", flush=True)
            return
        time.sleep(15)


def start(sid: str) -> None:
    threading.Thread(target=run, args=(sid,), daemon=True, name=f"boatid-ais-{sid}").start()
