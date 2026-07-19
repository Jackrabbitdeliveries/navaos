"""
EXAMPLE integration into your existing FastAPI app.

This replaces the direct-tune streaming behavior in api/stream.py and adds
the scan feature, both routed through the single ChannelManager so the
one-dongle constraint is enforced in one place rather than in each route.

Drop the navaos_audio package alongside your existing api/ directory and
wire this router into your app with app.include_router(router). This is
meant to fully replace both api/stream.py and the old api/radio.py +
services/radio_service.py + services/sdr_service.py trio, since those
older files could grab the RTL-SDR dongle concurrently with a live stream.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from navaos_audio.channel_manager import channel_manager, SessionConflictError

router = APIRouter(prefix="/radio", tags=["radio"])


# ---- direct tune --------------------------------------------------------

@router.get("/stream/{channel}.mp3")
def stream_channel(channel: str):
    try:
        channel_manager.get_config(channel)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")

    try:
        # Subscribe eagerly, outside the generator, so a session conflict
        # raises here and returns a clean 409 - rather than only surfacing
        # once StreamingResponse starts iterating (which FastAPI can't
        # turn into a normal HTTP error response at that point).
        q = channel_manager.subscribe_direct(channel)
    except SessionConflictError as e:
        raise HTTPException(409, str(e))

    def gen():
        try:
            while True:
                yield q.get()
        finally:
            channel_manager.unsubscribe_direct(channel, q)

    return StreamingResponse(gen(), media_type="audio/mpeg")


# ---- scanning -------------------------------------------------------------

class ScanStartRequest(BaseModel):
    channels: list[str] | None = None  # None = use default scan order


@router.post("/scan/start")
def start_scan(req: ScanStartRequest = ScanStartRequest()):
    try:
        channel_manager.start_scan(req.channels)
    except SessionConflictError as e:
        raise HTTPException(409, str(e))
    return channel_manager.status()


@router.get("/scan/stream.mp3")
def scan_stream():
    """Subscribe to the scanner's audio output. Call /radio/scan/start first."""
    if channel_manager.status().get("mode") != "scan":
        raise HTTPException(409, "Scan is not currently running. POST /radio/scan/start first.")

    # start_scan() is idempotent when a scan is already active: it just
    # returns a fresh subscriber queue onto the existing ScanController
    # rather than starting a second one.
    q = channel_manager.start_scan()

    def gen():
        try:
            while True:
                yield q.get()
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


@router.patch("/{channel}/params")
def update_params(channel: str, params: ChannelParams):
    changes = {k: v for k, v in params.model_dump().items() if v is not None}
    try:
        cfg = channel_manager.update_config(channel, **changes)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")
    return cfg.__dict__
