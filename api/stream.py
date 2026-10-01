import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel

from navaos_audio.channel_manager import channel_manager, SessionConflictError
from navaos_audio.recorder import last_heard, list_recordings, resolve_recording

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).resolve().parent.parent / "static"
_STATIC_NAME_RE = re.compile(r"^[a-z0-9_-]+\.(jpg|jpeg|png|webp|svg)$")

router = APIRouter(prefix="/radio", tags=["radio"])

# Display metadata for the player page. Frequencies/keys themselves live in
# navaos_audio.channel_manager (the single source of truth for the hardware
# layer) - this is presentation only.
CHANNELS = {
    "09": {"mhz": "156.450 MHz", "name": "Channel 09 - Bridge of Lions"},
    "13": {"mhz": "156.650 MHz", "name": "Channel 13 - Bridge-to-Bridge"},
    "16": {"mhz": "156.800 MHz", "name": "Channel 16 - Distress / Calling"},
    "68": {"mhz": "156.425 MHz", "name": "Channel 68"},
    "71": {"mhz": "156.575 MHz", "name": "Channel 71"},
    "wx": {"mhz": "162.425 MHz", "name": "NOAA Weather WX4"},
}


# ---- direct tune ------------------------------------------------------

@router.get("/stream/{channel}.mp3")
def stream_channel(channel: str):
    channel_key = channel.lower()
    try:
        channel_manager.get_config(channel_key)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")

    try:
        # Subscribe eagerly, outside the generator, so a session conflict
        # raises here and returns a clean 409 - rather than only surfacing
        # once StreamingResponse starts iterating.
        q = channel_manager.subscribe_direct(channel_key)
    except SessionConflictError as e:
        raise HTTPException(409, str(e))

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            channel_manager.unsubscribe_direct(channel_key, q)

    return StreamingResponse(
        gen(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/stop")
def stop_radio_stream():
    channel_manager.stop_direct()
    return {"ok": True, "status": "stopped"}


# ---- scanning -----------------------------------------------------------

@router.post("/scan/start")
def start_scan():
    """Scans whatever channels are currently in the scan list - see
    GET/POST /radio/scan/channels to manage that list. No per-request
    channel selection; the list itself is the persistent state."""
    try:
        channel_manager.start_scan()
    except SessionConflictError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return channel_manager.status()


@router.get("/scan/stream.mp3")
def scan_stream():
    """Subscribe to the scanner's audio output. Call /radio/scan/start first."""
    if channel_manager.status().get("mode") != "scan":
        raise HTTPException(409, "Scan is not currently running. POST /radio/scan/start first.")

    try:
        q = channel_manager.start_scan()
    except ValueError as e:
        raise HTTPException(400, str(e))

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            channel_manager.unsubscribe_scan(q)

    return StreamingResponse(gen(), media_type="audio/mpeg")


@router.post("/scan/resume")
def resume_scan():
    """Unlock from whatever channel the scanner is currently parked on."""
    channel_manager.resume_scan()
    return channel_manager.status()


@router.post("/scan/stop")
def stop_scan():
    channel_manager.stop_scan()
    return channel_manager.status()


# ---- scan channel list (scan memory) ---------------------------------------

class ScanChannelRequest(BaseModel):
    channel: str


@router.get("/scan/channels")
def get_scan_channels():
    return {"channels": channel_manager.get_scan_channels()}


@router.post("/scan/channels/add")
def add_scan_channel(req: ScanChannelRequest):
    try:
        channels = channel_manager.add_scan_channel(req.channel.lower())
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {req.channel}")
    return {"channels": channels}


@router.post("/scan/channels/remove")
def remove_scan_channel(req: ScanChannelRequest):
    try:
        channels = channel_manager.remove_scan_channel(req.channel.lower())
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {req.channel}")
    return {"channels": channels}


class ScanParams(BaseModel):
    """All fields optional - only supplied fields are updated (PATCH semantics).
    Scan-wide, unlike /{channel}/params - applies regardless of which channel
    the scan is currently visiting, and takes effect live on a running scan."""
    dwell_seconds: float | None = None
    lock_sustain_s: float | None = None
    auto_unlock_quiet_s: float | None = None


@router.patch("/scan/params")
def update_scan_params(params: ScanParams):
    changes = {k: v for k, v in params.model_dump().items() if v is not None}
    settings = channel_manager.update_scan_settings(**changes)
    return settings.__dict__


# ---- shared status / tuning -----------------------------------------------

@router.get("/status")
def status():
    return channel_manager.status()


class ChannelParams(BaseModel):
    """All fields optional - only supplied fields are updated (PATCH semantics)."""
    squelch_enabled: bool | None = None
    vad_aggressiveness: int | None = None
    open_threshold_db: float | None = None
    close_threshold_db: float | None = None
    hang_time_s: float | None = None
    agc_enabled: bool | None = None
    volume: float | None = None
    rf_gain: float | None = None
    rf_open_threshold_db: float | None = None
    rf_close_threshold_db: float | None = None


@router.get("/{channel}/params")
def get_params(channel: str):
    try:
        cfg = channel_manager.get_config(channel.lower())
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")
    return cfg.__dict__


@router.patch("/{channel}/params")
def update_params(channel: str, params: ChannelParams):
    changes = {k: v for k, v in params.model_dump().items() if v is not None}
    try:
        cfg = channel_manager.update_config(channel.lower(), **changes)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")
    return cfg.__dict__


# ---- recordings -------------------------------------------------------

@router.get("/recordings")
def recordings(channel: str | None = None, limit: int = 500):
    """Saved transmissions, newest first (see navaos_audio/recorder.py)."""
    return list_recordings(channel=channel.lower() if channel else None, limit=min(limit, 5000))


@router.get("/recordings/last-heard")
def recordings_last_heard():
    return last_heard()


@router.get("/static/{name}")
def static_file(name: str):
    """Images for the pages (e.g. the player's header photo) from ./static."""
    if not _STATIC_NAME_RE.match(name) or not (_STATIC / name).is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(_STATIC / name, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/recordings/file/{day}/{name}")
def recording_file(day: str, name: str):
    path = resolve_recording(day, name)
    if path is None:
        raise HTTPException(404, "No such recording")
    return FileResponse(path, media_type="audio/mpeg")


@router.get("/recordings/view")
def recordings_page():
    return HTMLResponse(_render("recordings.html"))


# ---- player page ------------------------------------------------------

def _render(template: str) -> str:
    """Pages are plain HTML in api/templates/, read per request (edits need no
    restart), with the channel table substituted in."""
    html = (_TEMPLATES / template).read_text()
    html = html.replace("__CHANNEL_ORDER__", json.dumps(list(CHANNELS)))
    return html.replace("__CHANNELS__", json.dumps(CHANNELS))


@router.get("/player")
def player():
    return HTMLResponse(_render("player.html"))
