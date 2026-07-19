from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

from navaos_audio.channel_manager import channel_manager, SessionConflictError

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
                yield q.get()
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
        cfg = channel_manager.update_config(channel.lower(), **changes)
    except KeyError:
        raise HTTPException(404, f"Unknown channel: {channel}")
    return cfg.__dict__


# ---- player page ------------------------------------------------------

@router.get("/player")
def player():
    buttons = "".join(
        f'<button id="btn-{key}" class="channel-button" onclick="playRadio(\'{key}\')">{val["name"]}</button><br>'
        for key, val in CHANNELS.items()
    )

    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <style>
    body {{
      font-family: Arial;
      padding: 30px;
      background: #f7f7f7;
    }}

    h1 {{
      margin-bottom: 10px;
    }}

    #statusBox {{
      border: 1px solid #aaa;
      background: white;
      padding: 14px;
      margin-bottom: 18px;
      max-width: 520px;
      font-size: 18px;
    }}

    #status {{
      font-weight: bold;
      margin-bottom: 6px;
    }}

    #frequency {{
      color: #444;
    }}

    .channel-button {{
      font-size: 22px;
      margin: 6px;
      padding: 8px 14px;
      border: 2px solid #888;
      border-radius: 6px;
      background: #eee;
      cursor: pointer;
      min-width: 360px;
      text-align: left;
    }}

    .channel-button.active {{
      background: #d7ffd7;
      border-color: #138a13;
      font-weight: bold;
    }}

    .channel-button:hover {{
      background: #e0e0e0;
    }}

    .channel-button.active:hover {{
      background: #d7ffd7;
    }}

    #stopButton {{
      font-size: 22px;
      margin: 6px;
      padding: 8px 14px;
      border-radius: 6px;
      cursor: pointer;
    }}

    #scanControls {{
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px dashed #999;
    }}

    .scan-button {{
      font-size: 20px;
      margin: 6px;
      padding: 8px 14px;
      border: 2px solid #2a6fb0;
      border-radius: 6px;
      background: #eaf2fb;
      cursor: pointer;
    }}

    .scan-button:hover:not(:disabled) {{
      background: #d9e9fa;
    }}

    .scan-button:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
    }}

    audio {{
      margin-top: 14px;
      width: 380px;
    }}
  </style>
</head>
<body>
  <h1>NavaOS Radio</h1>

  <div id="statusBox">
    <div id="status">Status: Idle</div>
    <div id="frequency">Frequency: —</div>
  </div>

  {buttons}

  <br>
  <button id="stopButton" onclick="stopRadio()">Stop</button>

  <div id="scanControls">
    <button id="btn-start-scan" class="scan-button" onclick="startScan()">Start Scan</button>
    <button id="btn-resume-scan" class="scan-button" onclick="resumeScan()" disabled>Resume Scan</button>
    <button id="btn-stop-scan" class="scan-button" onclick="stopScan()">Stop Scan</button>
  </div>

  <hr>
  <audio id="radio" controls preload="none"></audio>

  <script>
    const audio = document.getElementById("radio");
    const status = document.getElementById("status");
    const frequency = document.getElementById("frequency");

    const scanStartBtn = document.getElementById("btn-start-scan");
    const scanResumeBtn = document.getElementById("btn-resume-scan");

    const channels = {CHANNELS};

    let scanPollTimer = null;

    function clearActiveButtons() {{
      document.querySelectorAll(".channel-button").forEach(btn => {{
        btn.classList.remove("active");
      }});
    }}

    function setActiveChannel(channel) {{
      clearActiveButtons();
      const btn = document.getElementById("btn-" + channel);
      if (btn) {{
        btn.classList.add("active");
      }}
    }}

    async function playRadio(channel) {{
      const info = channels[channel];

      try {{
        const check = await fetch("/radio/status?x=" + Date.now());
        const checkData = await check.json();
        if (checkData.mode === "scan") {{
          status.innerText = "Status: Error - A scan is currently active. Stop the scan before tuning directly.";
          return;
        }}
      }} catch (err) {{
        // if the status check itself fails, fall through and let the
        // normal flow (and the 409 from the server, if any) surface it
      }}

      status.innerText = "Status: Stopping old stream...";
      frequency.innerText = "Frequency: —";

      audio.pause();
      audio.removeAttribute("src");
      audio.load();

      await fetch("/radio/stop?x=" + Date.now());
      await new Promise(resolve => setTimeout(resolve, 900));

      status.innerText = "Status: Loading " + info.name;
      frequency.innerText = "Frequency: " + info.mhz;

      audio.src = "/radio/stream/" + channel + ".mp3?t=" + Date.now();
      audio.volume = 1.0;
      audio.load();

      try {{
        await audio.play();
        status.innerText = "Status: Listening on " + info.name;
        frequency.innerText = "Frequency: " + info.mhz;
        setActiveChannel(channel);
      }} catch (err) {{
        status.innerText = "Status: Error - " + err;
        frequency.innerText = "Frequency: " + info.mhz;
        clearActiveButtons();
      }}
    }}

    async function stopRadio() {{
      audio.pause();
      audio.removeAttribute("src");
      audio.load();

      await fetch("/radio/stop?x=" + Date.now());

      status.innerText = "Status: Stopped";
      frequency.innerText = "Frequency: —";
      clearActiveButtons();
    }}

    function startScanPolling() {{
      if (scanPollTimer) return;
      scanPollTimer = setInterval(pollScanStatus, 1500);
      pollScanStatus();
    }}

    function stopScanPolling() {{
      if (scanPollTimer) {{
        clearInterval(scanPollTimer);
        scanPollTimer = null;
      }}
      scanResumeBtn.disabled = true;
    }}

    async function pollScanStatus() {{
      try {{
        const res = await fetch("/radio/status?x=" + Date.now());
        const data = await res.json();

        if (data.mode !== "scan") {{
          stopScanPolling();
          return;
        }}

        const info = channels[data.current_channel];
        const label = info ? info.name : (data.current_channel || "—");

        clearActiveButtons();

        if (data.locked) {{
          status.innerText = "Status: Locked on " + label;
          scanResumeBtn.disabled = false;
        }} else {{
          status.innerText = "Status: Scanning... (" + label + ")";
          scanResumeBtn.disabled = true;
        }}
        frequency.innerText = info ? "Frequency: " + info.mhz : "Frequency: —";
      }} catch (err) {{
        // transient poll failure; leave the last-known status displayed
      }}
    }}

    async function startScan() {{
      clearActiveButtons();
      status.innerText = "Status: Starting scan...";
      frequency.innerText = "Frequency: —";

      let res;
      try {{
        res = await fetch("/radio/scan/start", {{ method: "POST" }});
      }} catch (err) {{
        status.innerText = "Status: Error - " + err;
        return;
      }}

      if (res.status === 409) {{
        const err = await res.json();
        status.innerText = "Status: Error - " + (err.detail || "A direct tune is currently active.");
        return;
      }}

      if (!res.ok) {{
        status.innerText = "Status: Error - could not start scan.";
        return;
      }}

      audio.src = "/radio/scan/stream.mp3?t=" + Date.now();
      audio.volume = 1.0;
      audio.load();

      try {{
        await audio.play();
      }} catch (err) {{
        status.innerText = "Status: Error - " + err;
        return;
      }}

      startScanPolling();
    }}

    async function resumeScan() {{
      try {{
        const res = await fetch("/radio/scan/resume", {{ method: "POST" }});
        if (!res.ok) {{
          status.innerText = "Status: Error - could not resume scan.";
        }}
      }} catch (err) {{
        status.innerText = "Status: Error - " + err;
      }}
    }}

    async function stopScan() {{
      stopScanPolling();

      audio.pause();
      audio.removeAttribute("src");
      audio.load();

      await fetch("/radio/scan/stop", {{ method: "POST" }});

      status.innerText = "Status: Stopped";
      frequency.innerText = "Frequency: —";
      clearActiveButtons();
    }}

    audio.onerror = function() {{
      status.innerText = "Status: Audio error. Press Stop, wait 2 seconds, then select a channel.";
      clearActiveButtons();
    }};

    // Sync UI with whatever session is already active (e.g. page reload
    // during a scan or a direct tune started from another tab).
    (async function initStatus() {{
      try {{
        const res = await fetch("/radio/status?x=" + Date.now());
        const data = await res.json();

        if (data.mode === "scan") {{
          startScanPolling();
        }} else if (data.mode === "direct") {{
          const info = channels[data.channel];
          if (info) {{
            status.innerText = "Status: Listening on " + info.name;
            frequency.innerText = "Frequency: " + info.mhz;
            setActiveChannel(data.channel);
          }}
        }}
      }} catch (err) {{
        // leave default "Idle" status if this check fails
      }}
    }})();
  </script>
</body>
</html>
""")
