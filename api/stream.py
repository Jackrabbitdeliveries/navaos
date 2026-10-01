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
    names = {k: v["name"] for k, v in CHANNELS.items()}
    return HTMLResponse(_RECORDINGS_HTML.replace("__CHANNELS__", json.dumps(names)))


_RECORDINGS_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NavaOS Recordings</title>
  <style>
    body { font-family: Arial; padding: 16px; background: #f7f7f7; max-width: 720px; margin: 0 auto; }
    h1 { margin-bottom: 4px; }
    a { color: #0b5cad; }
    #filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
    #filters button { padding: 8px 12px; border: 1px solid #aaa; background: white; border-radius: 6px; font-size: 15px; }
    #filters button.active { background: #0b5cad; color: white; border-color: #0b5cad; }
    .day { margin-top: 18px; font-weight: bold; color: #444; border-bottom: 1px solid #ccc; padding-bottom: 4px; }
    .clip { background: white; border: 1px solid #ddd; border-radius: 6px; padding: 10px; margin-top: 8px; }
    .meta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 15px; margin-bottom: 6px; }
    .time { font-weight: bold; }
    .weak { color: #b26a00; } .ok { color: #2e7d32; }
    audio { width: 100%; }
    #summary { color: #555; font-size: 14px; }
  </style>
</head>
<body>
  <h1>Recordings</h1>
  <div><a href="/radio/player">&larr; Back to player</a></div>
  <div id="filters"></div>
  <div id="summary">Loading&hellip;</div>
  <div id="list"></div>
<script>
  const channels = __CHANNELS__;
  let filter = null;

  function snrLabel(db) {
    if (db === null) return "";
    const cls = db < 15 ? "weak" : "ok";
    const word = db < 15 ? "weak" : (db < 30 ? "good" : "strong");
    return `<span class="${cls}">signal +${db} dB (${word})</span>`;
  }

  function renderFilters() {
    const el = document.getElementById("filters");
    const keys = [null, ...Object.keys(channels)];
    el.innerHTML = keys.map(k =>
      `<button class="${k === filter ? "active" : ""}" data-k="${k ?? ""}">${k === null ? "All" : "Ch " + k}</button>`
    ).join("");
    el.querySelectorAll("button").forEach(b => b.onclick = () => {
      filter = b.dataset.k || null; renderFilters(); load();
    });
  }

  async function load() {
    const url = "/radio/recordings?limit=1000" + (filter ? "&channel=" + filter : "") + "&x=" + Date.now();
    const clips = await (await fetch(url)).json();
    const total = clips.reduce((a, c) => a + c.duration_s, 0);
    document.getElementById("summary").innerText =
      clips.length ? `${clips.length} transmissions, ${Math.round(total)} s total (kept 30 days)` : "No recordings yet.";
    let html = "", day = "";
    for (const c of clips) {
      const d = c.time.slice(0, 10);
      if (d !== day) { day = d; html += `<div class="day">${d}</div>`; }
      html += `<div class="clip"><div class="meta">
          <span class="time">${c.time.slice(11)}</span>
          <span>${channels[c.channel] || "Ch " + c.channel}</span>
          <span>${c.duration_s.toFixed(1)} s</span>
          ${snrLabel(c.peak_rf_snr_db)}
        </div>
        <audio controls preload="none" src="/radio/recordings/file/${c.path}"></audio></div>`;
    }
    document.getElementById("list").innerHTML = html;
  }

  function playing() {
    return [...document.querySelectorAll("audio")].some(a => !a.paused);
  }

  renderFilters();
  load();
  // Pick up new transmissions, but never yank the list out from under a clip that's playing.
  setInterval(() => { if (!playing()) load(); }, 30000);
</script>
</body>
</html>
"""


# ---- player page ------------------------------------------------------

@router.get("/player")
def player():
    html = (_TEMPLATES / "player.html").read_text()
    html = html.replace("__CHANNEL_ORDER__", json.dumps(list(CHANNELS)))
    return HTMLResponse(html.replace("__CHANNELS__", json.dumps(CHANNELS)))
