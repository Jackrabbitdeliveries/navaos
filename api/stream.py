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


# ---- player page ------------------------------------------------------

@router.get("/player")
def player():
    buttons = "".join(
        f'<div class="channel-row">'
        f'<input type="checkbox" class="scan-checkbox" data-channel="{key}" '
        f'onchange="toggleScanChannel(\'{key}\', this.checked)" title="Include in scan">'
        f'<button id="btn-{key}" class="channel-button" onclick="playRadio(\'{key}\')">{val["name"]}</button>'
        f'<button type="button" class="settings-toggle" onclick="toggleSettings(\'{key}\')" '
        f'title="Squelch sensitivity settings">⚙</button>'
        f'</div>'
        f'<div class="settings-panel" id="settings-{key}">'
        f'<div class="slider-labels"><span>Sensitive</span><span>Strict</span></div>'
        f'<input type="range" min="0" max="100" value="50" class="sensitivity-slider" '
        f'data-channel="{key}" oninput="onSensitivityInput(\'{key}\', this.value)">'
        f'<div class="sensitivity-readout">Sensitivity: <span id="sens-label-{key}">—</span></div>'
        f'</div>'
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

    .channel-row {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .scan-checkbox {{
      width: 20px;
      height: 20px;
      cursor: pointer;
      flex-shrink: 0;
    }}

    .channel-button {{
      font-size: 22px;
      margin: 6px 6px 6px 0;
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

    .settings-toggle {{
      font-size: 18px;
      padding: 6px 10px;
      border: 1px solid #999;
      border-radius: 6px;
      background: #f0f0f0;
      cursor: pointer;
      flex-shrink: 0;
    }}

    .settings-toggle:hover {{
      background: #e0e0e0;
    }}

    .settings-panel {{
      display: none;
      margin: 0 0 10px 34px;
      padding: 10px 14px;
      border: 1px solid #ccc;
      border-radius: 6px;
      background: white;
      max-width: 340px;
    }}

    .settings-panel.open {{
      display: block;
    }}

    .slider-labels {{
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      color: #666;
      margin-bottom: 2px;
    }}

    .sensitivity-slider {{
      width: 100%;
    }}

    .sensitivity-readout {{
      font-size: 13px;
      color: #444;
      margin-top: 4px;
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

    async function toggleScanChannel(channel, included) {{
      const endpoint = included ? "/radio/scan/channels/add" : "/radio/scan/channels/remove";
      try {{
        await fetch(endpoint, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ channel: channel }}),
        }});
      }} catch (err) {{
        // leave the checkbox as the user set it; next scan start will just
        // reflect whatever the server actually has
      }}
    }}

    async function loadScanChannels() {{
      try {{
        const res = await fetch("/radio/scan/channels?x=" + Date.now());
        const data = await res.json();
        const included = new Set(data.channels || []);
        document.querySelectorAll(".scan-checkbox").forEach(chk => {{
          chk.checked = included.has(chk.dataset.channel);
        }});
      }} catch (err) {{
        // leave checkboxes unchecked if this fails; reload the page to retry
      }}
    }}

    // ---- per-channel squelch sensitivity -----------------------------------
    // Single slider (0=most sensitive, 100=least sensitive) mapped onto
    // vad_aggressiveness (discrete 0-3) and open_threshold_db (continuous
    // 3-15dB); close_threshold_db is derived as open_threshold_db - 3dB to
    // preserve a fixed hysteresis gap rather than exposing it separately.
    const SENSITIVITY_MIN_DB = 3.0;
    const SENSITIVITY_MAX_DB = 15.0;
    const CLOSE_THRESHOLD_OFFSET_DB = 3.0;
    const sensitivityDebounceTimers = {{}};

    function toggleSettings(channel) {{
      const panel = document.getElementById("settings-" + channel);
      if (panel) {{
        panel.classList.toggle("open");
      }}
    }}

    function sliderToParams(value) {{
      const v = Number(value);
      const vad_aggressiveness = Math.min(3, Math.floor(v / 25));
      const open_threshold_db = SENSITIVITY_MIN_DB + (v / 100) * (SENSITIVITY_MAX_DB - SENSITIVITY_MIN_DB);
      const close_threshold_db = open_threshold_db - CLOSE_THRESHOLD_OFFSET_DB;
      return {{ vad_aggressiveness, open_threshold_db, close_threshold_db }};
    }}

    function openThresholdToSlider(open_threshold_db) {{
      const clamped = Math.max(SENSITIVITY_MIN_DB, Math.min(SENSITIVITY_MAX_DB, open_threshold_db));
      return Math.round((clamped - SENSITIVITY_MIN_DB) / (SENSITIVITY_MAX_DB - SENSITIVITY_MIN_DB) * 100);
    }}

    function updateSensitivityLabel(channel, value) {{
      const label = document.getElementById("sens-label-" + channel);
      if (label) {{
        label.innerText = value + "%";
      }}
    }}

    function onSensitivityInput(channel, value) {{
      updateSensitivityLabel(channel, value);
      clearTimeout(sensitivityDebounceTimers[channel]);
      sensitivityDebounceTimers[channel] = setTimeout(() => {{
        sendSensitivityUpdate(channel, value);
      }}, 300);
    }}

    async function sendSensitivityUpdate(channel, value) {{
      const params = sliderToParams(value);
      try {{
        await fetch("/radio/" + channel + "/params", {{
          method: "PATCH",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(params),
        }});
      }} catch (err) {{
        // leave the slider as the user set it; will resync on next page load
      }}
    }}

    async function loadChannelSettings() {{
      for (const key of Object.keys(channels)) {{
        try {{
          const res = await fetch("/radio/" + key + "/params?x=" + Date.now());
          const data = await res.json();
          const slider = document.querySelector(`.sensitivity-slider[data-channel="${{key}}"]`);
          if (slider && typeof data.open_threshold_db === "number") {{
            const pos = openThresholdToSlider(data.open_threshold_db);
            slider.value = pos;
            updateSensitivityLabel(key, pos);
          }}
        }} catch (err) {{
          // leave the default slider position (50%) if this fails
        }}
      }}
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

    loadScanChannels();
    loadChannelSettings();
  </script>
</body>
</html>
""")
