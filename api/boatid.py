"""
Boat ID routes (/boats/...) - see docs/boat-id-plan.md. Piece 1: snap a
photo, record the phone's sensors, make a quick guess, get Claude's guess.

Photos arrive as a JPEG data URL produced by the page's own canvas (already
resized to <=1568 px and free of EXIF), so no image or multipart libraries
are needed. Sightings are not locked down (Kevin's decision D6) but every id
is random and unguessable and nothing is listed publicly except via the log.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from api.nav import with_nav
from navaos_boatid import capture, feeds, identify, library, lookup, reveal, store

router = APIRouter(prefix="/boats", tags=["boat-id"])

_TEMPLATES = Path(__file__).parent / "templates"
MAX_PHOTO_BYTES = 3 * 1024 * 1024
GUESS_FIELDS = {"hull": identify.HULLS, "rig": identify.RIGS, "size": identify.SIZES}


def _page(name: str) -> HTMLResponse:
    html = (_TEMPLATES / name).read_text()
    html = html.replace("__HULLS__", json.dumps(identify.HULLS))
    html = html.replace("__RIGS__", json.dumps(identify.RIGS))
    return HTMLResponse(with_nav(html.replace("__SIZES__", json.dumps(identify.SIZES))))


@router.get("/snap")
def snap_page():
    return _page("boats_snap.html")


@router.get("/log")
def log_page():
    return _page("boats_log.html")


class NewSighting(BaseModel):
    image: str                      # data:image/jpeg;base64,...
    source: str = "phone"           # phone | upload | feed
    feed_id: str | None = None      # when source = feed
    client: str | None = None
    taken_at: float | None = None   # epoch seconds from the phone
    img_w: int | None = None
    img_h: int | None = None
    tap_x: float | None = None      # 0..1
    tap_y: float | None = None
    sensors: dict | None = None     # lat/lon/accuracy/heading/raw orientation - stored as given
    guess: dict | None = None       # hull/rig/size/model


@router.post("/sightings")
def create_sighting(req: NewSighting):
    prefix = "data:image/jpeg;base64,"
    if not req.image.startswith(prefix):
        raise HTTPException(400, "Photo must be a JPEG.")
    try:
        jpeg = base64.b64decode(req.image[len(prefix):], validate=True)
    except ValueError:
        raise HTTPException(400, "Photo data is corrupt.")
    if len(jpeg) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo is too large.")
    if not jpeg.startswith(b"\xff\xd8"):
        raise HTTPException(400, "Photo must be a JPEG.")
    if req.source not in ("phone", "upload", "feed"):
        raise HTTPException(400, "Unknown source.")
    if req.source == "feed" and (not req.feed_id or feeds.get(req.feed_id) is None):
        raise HTTPException(400, "Unknown feed.")

    guess = {}
    for k, allowed in GUESS_FIELDS.items():
        v = (req.guess or {}).get(k)
        guess[k] = v if v in allowed else "unknown"
    guess["model"] = str((req.guess or {}).get("model") or "").strip()[:80]

    def clamp01(v):
        return None if v is None else min(1.0, max(0.0, float(v)))

    sid = store.new_id()
    store.insert({
        "id": sid, "created_at": time.time(), "kit_id": store.kit_id(),
        "client": (req.client or "")[:64] or None, "source": req.source,
        "taken_at": req.taken_at, "photo": store.save_photo(sid, jpeg),
        "img_w": req.img_w, "img_h": req.img_h,
        "tap_x": clamp01(req.tap_x), "tap_y": clamp01(req.tap_y),
        "sensors": req.sensors, "guess": guess, "ai_status": "pending",
        "feed_id": req.feed_id if req.source == "feed" else None,
    })
    identify.start(sid)
    reveal.start(sid)
    return {"id": sid}


def _public(row: dict) -> dict:
    """What the page needs - no client id, no file path."""
    d = {k: row.get(k) for k in ("id", "created_at", "source", "taken_at", "img_w", "img_h", "tap_x", "tap_y",
                                 "sensors", "guess", "ai_status", "ai", "ai_meta", "ai_error", "confirm", "ais", "lookup")}
    d["feed"] = feeds.public(row["feed_id"]) if row.get("feed_id") else None   # incl. the credit to show
    return d


@router.get("/sightings/{sid}")
def get_sighting(sid: str):
    row = store.get(sid)
    if row is None:
        raise HTTPException(404, "No such sighting")
    return _public(row)


@router.get("/feeds")
def list_feeds():
    """Live camera feeds (public fields only), each with its credit."""
    return [feeds.public(fid) for fid in feeds.all_feeds()]


_last_grab = {"t": 0.0}


@router.post("/feeds/{feed_id}/frame")
def grab_frame(feed_id: str):
    """Grab the current still from a live camera feed (with the owners'
    permission) so the user can frame a boat in it. Nothing is stored until
    the user submits it as a sighting."""
    if feeds.get(feed_id) is None:
        raise HTTPException(404, "Unknown feed")
    if time.time() - _last_grab["t"] < 3:
        raise HTTPException(429, "One moment - grabbing frames too fast.")
    _last_grab["t"] = time.time()
    try:
        jpeg, t = capture.grab(feed_id)
    except capture.CaptureError as e:
        raise HTTPException(502, str(e))
    w, h = capture.image_size(jpeg) or (None, None)
    return {"image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(), "taken_at": t,
            "w": w, "h": h, "feed": feeds.public(feed_id),
            "zoomed_on_bridge": feeds.in_zoom_window(feeds.get(feed_id), t)}


@router.get("/watch")
def watch_status():
    """Bridge watch: is it in a bridge window, when's the next, what it caught today."""
    from navaos_boatid.watch import bridge_watch
    return bridge_watch.status()


@router.get("/budget")
def ai_budget():
    from navaos_boatid import budget
    ok, spent, cap = budget.allowed()
    return {"spent_today_usd": round(spent, 2), "cap_usd": cap, "ok": ok}


@router.get("/sightings")
def list_sightings(limit: int = 50):
    return [_public(r) for r in store.recent(min(max(limit, 1), 500))]


@router.get("/references")
def references(make: str, model: str = "", exclude: str | None = None):
    """Reference photos for one make/model: your own confirmed photos first,
    then freely licensed Wikimedia Commons photos (with credit)."""
    return library.references(make[:80], model[:80], exclude if exclude and store.valid_id(exclude) else None)


@router.get("/models")
def models():
    """Make/model names already confirmed - for autocomplete."""
    return library.known_models()


class Confirm(BaseModel):
    choice: str                     # candidate | own | typed | model_unknown | unsure ("can't tell")
    candidate_index: int | None = None
    make: str = ""
    model: str = ""
    hull: str = "unknown"
    rig: str = "unknown"
    size: str = "unknown"


def _model_points(row: dict, conf: dict) -> dict:
    """Did you / the AI get the confirmed make+model?"""
    if conf.get("model_unknown"):
        return {"you": None, "ai_rank": None}
    g = (row.get("guess") or {}).get("model", "")
    you = None
    if g:
        parts = g.split(None, 1)
        you = library.same_model(parts[0], parts[1] if len(parts) > 1 else "", conf["make"], conf["model"]) \
            or library.norm(g) == library.norm(f"{conf['make']} {conf['model']}")
    rank = None
    for i, c in enumerate((row.get("ai") or {}).get("candidates") or []):
        if library.same_model(c.get("make", ""), c.get("model", ""), conf["make"], conf["model"]):
            rank = i + 1
            break
    return {"you": you, "ai_rank": rank}


@router.post("/sightings/{sid}/confirm")
def confirm(sid: str, req: Confirm):
    row = store.get(sid)
    if row is None:
        raise HTTPException(404, "No such sighting")
    if req.choice not in ("candidate", "own", "typed", "model_unknown", "unsure"):
        raise HTTPException(400, "Unknown choice.")
    make, model = req.make.strip()[:60], req.model.strip()[:80]
    unknown = req.choice in ("model_unknown", "unsure")
    if not unknown and not make:
        raise HTTPException(400, "Please give at least the make (e.g. Catalina).")
    conf = {
        "choice": req.choice, "candidate_index": req.candidate_index,
        "make": "" if unknown else make, "model": "" if unknown else model,
        "model_unknown": unknown,
        "hull": req.hull if req.hull in identify.HULLS else "unknown",
        "rig": req.rig if req.rig in identify.RIGS else "unknown",
        "size": req.size if req.size in identify.SIZES else "unknown",
        "at": time.time(),
    }
    conf["points"] = _model_points(row, conf)
    store.update(sid, confirm=conf)
    return _public(store.get(sid))


class AisChoice(BaseModel):
    mmsi: int | None = None         # None = "not on AIS / none of these"


@router.post("/sightings/{sid}/ais")
def ais_choice(sid: str, req: AisChoice):
    """The user's answer to the AIS reveal. Also records how far off the
    compass was (true bearing to the chosen boat minus the aim), which is the
    compass calibration data (experiment E2) collected through normal use."""
    row = store.get(sid)
    if row is None:
        raise HTTPException(404, "No such sighting")
    res = dict(row.get("ais") or {})
    choice = {"at": time.time(), "none": req.mmsi is None}
    if req.mmsi is not None:
        from navaos_audio.ais import ais_service
        v = ais_service.db.get(str(req.mmsi))
        if v is None:
            raise HTTPException(404, "Unknown vessel")
        known = {c["mmsi"]: c for c in (res.get("candidates") or []) + (res.get("nearby") or [])}
        c = known.get(req.mmsi, {})
        length = (v.get("to_bow") or 0) + (v.get("to_stern") or 0)
        choice.update(mmsi=req.mmsi, name=v.get("name") or "", shiptype=v.get("shiptype"),
                      length_m=length if length > 1 else None,
                      size_class=reveal.size_class(length) if length > 1 else None,
                      distance_m=c.get("distance_m"), bearing=c.get("bearing"))
        if res.get("aim_true_deg") is not None and c.get("bearing") is not None:
            choice["compass_error_deg"] = round((c["bearing"] - res["aim_true_deg"] + 180) % 360 - 180)
    res["user_choice"] = choice
    res["final"] = True
    store.update(sid, ais=res)
    if choice.get("compass_error_deg") is not None:
        print(f"BOATID-COMPASS {sid} error={choice['compass_error_deg']}deg dist={choice.get('distance_m')}m", flush=True)
    return _public(store.get(sid))


@router.post("/sightings/{sid}/ais/reset")
def ais_reset(sid: str):
    """'Change' on the AIS answer: forget the user's choice and re-run the match."""
    row = store.get(sid)
    if row is None:
        raise HTTPException(404, "No such sighting")
    res = dict(row.get("ais") or {})
    res.pop("user_choice", None)
    store.update(sid, ais=res)
    try:
        from navaos_audio.ais import ais_service
        res = reveal.match(store.get(sid), ais_service.db.snapshot(26 * 3600, with_track=True))
        res["final"] = True
        store.update(sid, ais=res)
    except Exception as e:
        print(f"BOATID-AIS-RESET-FAILED {sid}: {e!r}", flush=True)
    return _public(store.get(sid))


class LookupRequest(BaseModel):
    name: str


@router.post("/sightings/{sid}/lookup")
def start_lookup(sid: str, req: LookupRequest):
    """Search the web for what's publicly known about a named boat (on demand -
    it costs ~5-20 cents, inside the daily budget)."""
    row = store.get(sid)
    if row is None:
        raise HTTPException(404, "No such sighting")
    name = " ".join(req.name.split())[:60]
    if len(name) < 2:
        raise HTTPException(400, "Give the boat's name.")
    if (row.get("lookup") or {}).get("status") == "pending":
        return _public(row)
    if not identify.AI_ENABLED:
        raise HTTPException(503, "AI is disabled on this instance.")
    from navaos_boatid import budget
    ok, spent, cap = budget.allowed()
    if not ok:
        raise HTTPException(429, f"Daily AI budget reached (${spent:.2f} of ${cap:.2f}) - resets at midnight.")
    lookup.start(sid, name)
    return _public(store.get(sid))


@router.get("/photo/{sid}.jpg")
def photo(sid: str):
    row = store.get(sid)
    path = store.photo_path(row) if row else None
    if path is None:
        raise HTTPException(404, "No such photo")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
