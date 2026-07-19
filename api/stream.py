from fastapi import APIRouter
from fastapi.responses import StreamingResponse, HTMLResponse
import subprocess
import os
import signal
import time
import threading

router = APIRouter(prefix="/radio", tags=["radio-stream"])

CHANNELS = {
    "09": {"freq": "156.450M", "mhz": "156.450 MHz", "name": "Channel 09 - Bridge of Lions"},
    "13": {"freq": "156.650M", "mhz": "156.650 MHz", "name": "Channel 13 - Bridge-to-Bridge"},
    "16": {"freq": "156.800M", "mhz": "156.800 MHz", "name": "Channel 16 - Distress / Calling"},
    "68": {"freq": "156.425M", "mhz": "156.425 MHz", "name": "Channel 68"},
    "71": {"freq": "156.575M", "mhz": "156.575 MHz", "name": "Channel 71"},
    "wx": {"freq": "162.425M", "mhz": "162.425 MHz", "name": "NOAA Weather WX4"},
}

active_proc = None
proc_lock = threading.Lock()


def stop_active_stream():
    global active_proc

    with proc_lock:
        proc = active_proc
        active_proc = None

    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass

    time.sleep(0.6)


@router.get("/stream/{channel}.mp3")
def stream_channel(channel: str):
    global active_proc

    channel_key = channel.lower()
    selected = CHANNELS.get(channel_key, CHANNELS["09"])
    freq = selected["freq"]
    # NOAA is continuous, so leave squelch open.
    # Marine channels use an empirical rtl_fm squelch threshold.
    squelch = 0 if channel_key == "wx" else 40

    stop_active_stream()

    cmd = (
      f"rtl_fm -f {freq} -M fm -s 48000 -g 49.6 -l {squelch} -E deemp "
       "| ffmpeg -hide_banner -loglevel error "
       "-f s16le -ar 48000 -ac 1 -i pipe:0 "
       '-af "highpass=f=250,lowpass=f=3200,afftdn=nr=10:nf=-45:tn=1,acompressor=threshold=-24dB:ratio=3:attack=20:release=250:makeup=4,volume=10dB" '
       "-acodec libmp3lame -b:a 64k -f mp3 pipe:1"  
    )

    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        preexec_fn=os.setsid,
    )
    with proc_lock:
        active_proc = proc

    def audio_generator():
        global active_proc
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            with proc_lock:
                if active_proc == proc:
                    active_proc = None

            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass

    return StreamingResponse(
        audio_generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/stop")
def stop_radio_stream():
    stop_active_stream()
    return {"ok": True, "status": "stopped"}


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

  <hr>
  <audio id="radio" controls preload="none"></audio>

  <script>
    const audio = document.getElementById("radio");
    const status = document.getElementById("status");
    const frequency = document.getElementById("frequency");

    const channels = {CHANNELS};

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

    audio.onerror = function() {{
      status.innerText = "Status: Audio error. Press Stop, wait 2 seconds, then select a channel.";
      clearActiveButtons();
    }};
  </script>
</body>
</html>
""")
