"""
Fleet memory (2026-11-08, Kevin's request): the boats the user has already
confirmed, shown to the AI with every new photo.

The mooring fields in front of the cameras hold the same boats for weeks, so
"is this one of the boats we already know?" is usually a better question than
"which of all the boats ever built is this?". Before this, the teal-covered
catamaran Kevin confirmed as a Royal Cape 45 ("Ego Domi") was snapped five
more times and the AI said "Lagoon 410" every time.

Each confirmed boat gets a small reference close-up, <id>_ref.jpg next to its
photo: the user's framed close-up if there is one, else a box around the tap,
else the whole photo - neither side over REF_W px to keep the extra image
tokens low (~150-500 per boat).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from . import capture, library, store

REF_W = 800
MAX_BOATS = 10
TAP_BOX = 1 / 3            # same size as the zoom view's default 3x box


def _tap_box(row: dict) -> Optional[dict]:
    if row.get("tap_x") is None or row.get("tap_y") is None:
        return None
    w = h = TAP_BOX
    x = min(max(row["tap_x"] - w / 2, 0.0), 1 - w)
    y = min(max(row["tap_y"] - h / 2, 0.0), 1 - h)
    return {"x": x, "y": y, "w": w, "h": h}


def ensure_ref(row: dict) -> Optional[Path]:
    """Make (once) and return the boat's reference close-up, or None."""
    out = store.ref_file(row)
    if out.is_file():
        return out
    try:
        crop = store.crop_path(row)
        if crop is not None:
            jpeg = crop.read_bytes()
        else:
            photo = store.photo_path(row)
            if photo is None:
                return None
            box = _tap_box(row)
            jpeg = capture.crop_box(photo.read_bytes(), box) if box else photo.read_bytes()
        jpeg = capture.fit(jpeg, REF_W)
    except Exception as e:   # ffmpeg missing/timeout, unreadable photo: just no reference
        print(f"BOATID-REF-FAILED {row.get('id')}: {e!r}", flush=True)
        return None
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(jpeg)
    tmp.replace(out)
    return out


def boat_name(row: dict) -> str:
    """The boat's own name if we know it: the AIS answer, the hull lettering, a lookup."""
    uc = (row.get("ais") or {}).get("user_choice") or {}
    if uc.get("name"):
        return uc["name"].strip().title()
    vn = ((row.get("ai") or {}).get("visible_name") or "").strip()
    if vn:
        return vn
    lk = row.get("lookup") or {}
    if lk.get("status") == "done" and (lk.get("result") or {}).get("found"):
        return ((lk.get("meta") or {}).get("name") or "").strip()
    return ""


def known_boats(exclude: Optional[str] = None, feed_id: Optional[str] = None, limit: int = MAX_BOATS) -> list[dict]:
    """Distinct confirmed boats, same camera first, then most recent."""
    seen, out = set(), []
    rows = [r for r in store.recent(2000) if r["id"] != exclude]
    rows.sort(key=lambda r: (r.get("feed_id") != feed_id if feed_id else False, -r["created_at"]))
    for r in rows:
        c = r.get("confirm") or {}
        if not c.get("make") or c.get("model_unknown"):
            continue
        name = boat_name(r)
        mmsi = ((r.get("ais") or {}).get("user_choice") or {}).get("mmsi")
        color = ((r.get("ai") or {}).get("hull_color") or "").lower()
        key = f"mmsi:{mmsi}" if mmsi else f"name:{library.norm(name)}" if name else \
            f"model:{library.norm(c['make'] + ' ' + c.get('model', ''))}|{library.norm(color)}"
        if key in seen:
            continue
        ref = ensure_ref(r)
        if ref is None:
            continue
        seen.add(key)
        ai = r.get("ai") or {}
        out.append({
            "ref": f"K{len(out) + 1}", "sighting": r["id"], "make": c["make"], "model": c.get("model", ""),
            "name": name, "hull": c.get("hull") or ai.get("hull") or "unknown",
            "rig": c.get("rig") or "unknown", "size": c.get("size") or "unknown",
            "hull_color": ai.get("hull_color", ""), "description": ai.get("description", ""),
            "seen": time.strftime("%b %-d", time.localtime(r["created_at"])),
            "where": "this camera" if feed_id and r.get("feed_id") == feed_id else
                     (f"camera {r['feed_id']}" if r.get("feed_id") else "a phone photo"),
            "path": ref,
        })
        if len(out) >= limit:
            break
    return out
