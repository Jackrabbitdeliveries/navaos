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

from navaos_boatid import identify, store

router = APIRouter(prefix="/boats", tags=["boat-id"])

_TEMPLATES = Path(__file__).parent / "templates"
MAX_PHOTO_BYTES = 3 * 1024 * 1024
GUESS_FIELDS = {"hull": identify.HULLS, "rig": identify.RIGS, "size": identify.SIZES}


def _page(name: str) -> HTMLResponse:
    html = (_TEMPLATES / name).read_text()
    html = html.replace("__HULLS__", json.dumps(identify.HULLS))
    html = html.replace("__RIGS__", json.dumps(identify.RIGS))
    return HTMLResponse(html.replace("__SIZES__", json.dumps(identify.SIZES)))


@router.get("/snap")
def snap_page():
    return _page("boats_snap.html")


class NewSighting(BaseModel):
    image: str                      # data:image/jpeg;base64,...
    source: str = "phone"           # phone | upload
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
    if req.source not in ("phone", "upload"):
        raise HTTPException(400, "Unknown source.")

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
    })
    identify.start(sid)
    return {"id": sid}


def _public(row: dict) -> dict:
    """What the page needs - no client id, no file path."""
    return {k: row.get(k) for k in ("id", "created_at", "source", "taken_at", "img_w", "img_h", "tap_x", "tap_y",
                                    "sensors", "guess", "ai_status", "ai", "ai_meta", "ai_error", "confirm", "ais")}


@router.get("/sightings/{sid}")
def get_sighting(sid: str):
    row = store.get(sid)
    if row is None:
        raise HTTPException(404, "No such sighting")
    return _public(row)


@router.get("/sightings")
def list_sightings(limit: int = 50):
    return [_public(r) for r in store.recent(min(max(limit, 1), 500))]


@router.get("/photo/{sid}.jpg")
def photo(sid: str):
    row = store.get(sid)
    path = store.photo_path(row) if row else None
    if path is None:
        raise HTTPException(404, "No such photo")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
