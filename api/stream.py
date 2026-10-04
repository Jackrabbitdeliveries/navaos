import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel

from navaos_audio.channel_manager import channel_manager, SessionConflictError
from navaos_audio.control import ControlError, control
from navaos_audio import noise_meter as nm
from navaos_audio.ais import ais_service
from navaos_audio.recorder import last_heard, list_recordings, mark_listened, resolve_recording

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).resolve().parent.parent / "static"
_STATIC_NAME_RE = re.compile(r"^[a-z0-9_-]+\.(jpg|jpeg|png|webp|svg)$")

router = APIRouter(prefix="/radio", tags=["radio"])

# Display metadata for the player page. Frequencies/keys themselves live in
# navaos_audio.channel_manager (the single source of truth for the hardware
# layer) - this is presentation only. "monitored_by" = shore stations that
# listen on the channel (shown as tags on the player cards).
CHANNELS = {
    "09": {"mhz": "156.450 MHz", "name": "Channel 09 - Bridge of Lions"},
    "13": {"mhz": "156.650 MHz", "name": "Channel 13 - Bridge-to-Bridge"},
    "16": {"mhz": "156.800 MHz", "name": "Channel 16 - Distress / Calling"},
    "68": {"mhz": "156.425 MHz", "name": "Channel 68",
           "monitored_by": ["Comachee Cove Yacht Harbor"]},
    "69": {"mhz": "156.475 MHz", "name": "Channel 69",
           "monitored_by": ["Conch House Marina Resort"]},
    "71": {"mhz": "156.575 MHz", "name": "Channel 71",
           "monitored_by": ["St. Augustine Municipal Marina"]},
    "72": {"mhz": "156.625 MHz", "name": "Channel 72 - St. Augustine Cruisers Net"},
    "wx": {"mhz": "162.425 MHz", "name": "NOAA Weather WX4"},
}


# ---- direct tune ------------------------------------------------------

def _control(client: str | None, action: str, channel: str | None = None, pin: str | None = None) -> dict:
    """Run a radio action through the shared-control rules (turns/queue/PIN)."""
    try:
        return control.request(client or "api", action, channel, pin)
    except ControlError as e:
        raise HTTPException(e.status, str(e))
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")
    except SessionConflictError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _check_settings(client: str | None, pin: str | None) -> None:
    try:
        control.check_settings(client, pin)
    except ControlError as e:
        raise HTTPException(e.status, str(e))


class ControlRequest(BaseModel):
    action: str                 # tune | scan | stop | resume | cancel
    channel: str | None = None
    client: str | None = None
    pin: str | None = None


@router.post("/control")
def control_radio(req: ControlRequest):
    """The player's single entry point for changing the radio. Result is
    "applied", "queued" (someone else's 10-min turn), "joined" (already
    selected) or "cancelled"; plus the same control status as /status."""
    channel = req.channel.lower() if req.channel else None
    return _control(req.client, req.action, channel, req.pin)


@router.get("/stream/{channel}.mp3")
def stream_channel(channel: str, client: str | None = None):
    channel_key = channel.lower()
    try:
        channel_manager.get_config(channel_key)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")

    # Listening never changes the radio: only the selected channel can be
    # streamed (change it via POST /radio/control).
    if control.selection() != {"mode": "direct", "channel": channel_key}:
        raise HTTPException(409, "That channel isn't what the radio is set to.")

    try:
        # Subscribe eagerly, outside the generator, so a session conflict
        # raises here and returns a clean 409 - rather than only surfacing
        # once StreamingResponse starts iterating.
        q = channel_manager.subscribe_direct(channel_key)
    except SessionConflictError as e:
        raise HTTPException(409, str(e))

    control.stream_opened(client)

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            control.stream_closed(client)
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
def stop_radio_stream(client: str | None = None):
    return _control(client, "stop")


# ---- scanning -----------------------------------------------------------

@router.post("/scan/start")
def start_scan(client: str | None = None):
    """Scans whatever channels are currently in the scan list - see
    GET/POST /radio/scan/channels to manage that list."""
    return _control(client, "scan")


@router.get("/scan/stream.mp3")
def scan_stream(client: str | None = None):
    """Subscribe to the scanner's audio output. Call /radio/scan/start first."""
    control.wake_for_listener()   # an AIS window pauses the scan; listening ends it
    if channel_manager.status().get("mode") != "scan":
        raise HTTPException(409, "Scan is not currently running. POST /radio/scan/start first.")

    try:
        q = channel_manager.start_scan()
    except ValueError as e:
        raise HTTPException(400, str(e))

    control.stream_opened(client)

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            control.stream_closed(client)
            channel_manager.unsubscribe_scan(q)

    return StreamingResponse(gen(), media_type="audio/mpeg")


@router.post("/scan/resume")
def resume_scan(client: str | None = None):
    """Unlock from whatever channel the scanner is currently parked on."""
    return _control(client, "resume")


@router.post("/scan/stop")
def stop_scan(client: str | None = None):
    return _control(client, "stop")


# ---- scan channel list (scan memory) ---------------------------------------

class ScanChannelRequest(BaseModel):
    channel: str


@router.get("/scan/channels")
def get_scan_channels():
    return {"channels": channel_manager.get_scan_channels()}


@router.post("/scan/channels/add")
def add_scan_channel(req: ScanChannelRequest, client: str | None = None,
                     x_override_pin: str | None = Header(default=None)):
    _check_settings(client, x_override_pin)
    try:
        channels = channel_manager.add_scan_channel(req.channel.lower())
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {req.channel}")
    return {"channels": channels}


@router.post("/scan/channels/remove")
def remove_scan_channel(req: ScanChannelRequest, client: str | None = None,
                        x_override_pin: str | None = Header(default=None)):
    _check_settings(client, x_override_pin)
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
def update_scan_params(params: ScanParams, client: str | None = None,
                       x_override_pin: str | None = Header(default=None)):
    _check_settings(client, x_override_pin)
    changes = {k: v for k, v in params.model_dump().items() if v is not None}
    settings = channel_manager.update_scan_settings(**changes)
    return settings.__dict__


# ---- shared status / tuning -----------------------------------------------

@router.get("/status")
def status(client: str | None = None):
    """Hardware status plus shared-control state for `client`; polling it is
    also that client's heartbeat."""
    control.heartbeat(client)
    return {**channel_manager.status(), **control.status(client)}


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
def update_params(channel: str, params: ChannelParams, client: str | None = None,
                  x_override_pin: str | None = Header(default=None)):
    _check_settings(client, x_override_pin)
    changes = {k: v for k, v in params.model_dump().items() if v is not None}
    try:
        cfg = channel_manager.update_config(channel.lower(), **changes)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")
    return cfg.__dict__


# ---- noise meter ------------------------------------------------------
# Start/stop it with POST /radio/control {"action": "noise"} / {"action": "stop"}.

@router.get("/noise")
def noise_readings(since: float = 0.0, client: str | None = None):
    """Live noise-meter readings newer than `since` (epoch s). Polling this
    while the meter runs counts as activity (holds off the default scan)."""
    running = control.selection()["mode"] == "noise"
    if running:
        control.touch(client)
    return {
        "running": running,
        "reference_db": nm.load_reference(),
        "error": nm.noise_meter.error,
        "readings": nm.noise_meter.readings(since) if running else [],
    }


class NoiseReference(BaseModel):
    db: float | None = None     # omit = use the median of the last 10 s


@router.post("/noise/reference")
def set_noise_reference(req: NoiseReference, client: str | None = None,
                        x_override_pin: str | None = Header(default=None)):
    """Set the 'dongle's own floor' reference (measure with the coax off)."""
    _check_settings(client, x_override_pin)
    db = req.db
    if db is None:
        recent = nm.noise_meter.readings(time.time() - 10)
        if not recent:
            raise HTTPException(409, "No recent readings - start the noise meter first.")
        db = float(sorted(r["floor_db"] for r in recent)[len(recent) // 2])
    nm.save_reference(db)
    return {"reference_db": nm.load_reference()}


@router.get("/noise/view")
def noise_page():
    return HTMLResponse(_render("noise.html"))


# ---- AIS --------------------------------------------------------------

@router.get("/ais/vessels")
def ais_vessels(max_age_h: float = 24, client: str | None = None):
    """Vessels heard in the last `max_age_h` hours (latest state, no tracks),
    plus the timeshare status."""
    control.heartbeat(client)
    return {
        "now": time.time(),
        "ais": control.ais_status(),
        "vessels": ais_service.db.snapshot(max(0.1, min(max_age_h, 24 * 30)) * 3600),
    }


@router.get("/ais/vessel/{mmsi}")
def ais_vessel(mmsi: int):
    v = ais_service.db.get(str(mmsi))
    if v is None:
        raise HTTPException(404, "Unknown vessel")
    return v


@router.post("/ais/now")
def ais_now(client: str | None = None, x_override_pin: str | None = Header(default=None)):
    """Start an AIS window now (if nobody is listening and the radio is just
    scanning/idle)."""
    try:
        control.ais_now(client, x_override_pin)
    except ControlError as e:
        raise HTTPException(e.status, str(e))
    return control.ais_status()


@router.get("/ais/view")
def ais_page():
    return HTMLResponse(_render("ais.html"))


# ---- recordings -------------------------------------------------------

@router.get("/recordings")
def recordings(channel: str | None = None, limit: int = 500):
    """Saved transmissions, newest first (see navaos_audio/recorder.py)."""
    return list_recordings(channel=channel.lower() if channel else None, limit=min(limit, 5000))


class ListenedRequest(BaseModel):
    paths: list[str]
    listened: bool = True


@router.post("/recordings/listened")
def recordings_listened(req: ListenedRequest):
    """Mark clips heard (or unheard). Shared across everyone, not per device."""
    return {"updated": mark_listened(req.paths[:5000], req.listened)}


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
