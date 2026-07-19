# NavaOS VHF Receiver — Handoff Brief for Claude Code

## Project

NavaOS is a Raspberry Pi based monitoring system for a sailboat (`nava-pi`,
reachable remotely via a Cloudflare Tunnel at `ssh.nnwx.com`). The VHF radio
subsystem is the first module: a user anywhere in the world opens
`https://vhf.nnwx.com`, picks a marine channel, and hears live audio from
an RTL-SDR dongle attached to the Pi.

Repo: `github.com/jackrabbitdeliveries/navaos` (branch `master`).
Real backend lives on the Pi at `/home/kevin/nava-os/backend`, running as
systemd service `navaos.service` (`uvicorn main:app`, port 8000, behind the
Cloudflare Tunnel — no port forwarding).

## What's wrong with the current implementation

The existing backend has **two independent, competing systems** that both
try to control the same physical RTL-SDR dongle:

1. **`api/stream.py`** — builds an `rtl_fm | ffmpeg` shell pipeline
   per-request (`shell=True`, an f-string command), streams MP3 to the
   browser via `StreamingResponse`. Uses a single global `active_proc`, so
   only one direct-tune listener can be active system-wide (switching
   channels kills the previous stream — that behavior is fine and worth
   keeping).
2. **`api/radio.py` + `services/radio_service.py` + `services/sdr_service.py`**
   — a completely separate scanning system that cycles channels every
   0.8s and plays audio through **the Pi's local speaker** (`sox ... -d`),
   not to any browser. Both routers are mounted in `main.py`
   (`app.include_router(radio_router)` and `app.include_router(stream_router)`),
   so **if a scan is running and someone starts a direct stream (or vice
   versa), two `rtl_fm` processes fight over one USB dongle.** This is a
   live bug, not hypothetical — confirmed by reading `main.py`.

Additionally, `rtl_fm`'s own `-l` squelch (whatever threshold is chosen)
is a blunt RSSI cutoff with no hysteresis, no hang timer, and no concept
of "is this actually a voice" — it can't distinguish continuous broadband
hiss from speech, which is the actual complaint driving this whole
project (marine channels sound noisy; the goal is voice-only, hiss-free,
low-latency audio).

## What's been designed and built: `navaos_audio` package

A new, tested-against-fakes (no real RTL-SDR hardware was available in the
environment that built this) package that replaces all of the above. Files:

| File | Purpose |
|---|---|
| `config.py` | `ChannelConfig` (frozen dataclass) + thread-safe `ConfigStore` — the backend for future UI sliders (Gain, Noise Reduction, Squelch, Hang Time, Volume). Live-updatable without restarting the service. |
| `squelch.py` | `AdaptiveSquelch` — the core DSP. Combines a rolling low-percentile `NoiseFloorEstimator` with WebRTC VAD (`webrtcvad` package) for real speech detection, hysteresis (different open/close thresholds), a hang timer, and a click-free attack/release gain envelope. Operates on raw PCM *before* the existing FFmpeg filter chain. |
| `sdr_receiver.py` | `RTLSDRReceiver` — wraps `rtl_fm` with an explicit arg list (no shell string). Always passes `-l 0`; squelch is 100% handled in software now. |
| `ffmpeg_encoder.py` | `FFmpegEncoder` — keeps the existing, already-tuned filter chain (highpass/lowpass/afftdn/compressor) + MP3 encode, fed via stdin PCM instead of a shell pipe. |
| `audio_pipeline.py` | `AudioPipeline` — one per direct-tuned channel. Wires receiver → squelch → encoder → broadcasts MP3 chunks to N subscriber queues (so multiple browser tabs on the *same* channel share one `rtl_fm` process). |
| `scan_controller.py` | `ScanController` — cycles a configurable list of channels, retuning the single receiver between dwell periods. Locks onto a channel the instant `AdaptiveSquelch` detects traffic and **stays locked until the user explicitly calls `resume()`** — it does not auto-resume when the channel goes quiet. Confirmed with the boat owner. |
| `channel_manager.py` | **The single hardware arbiter.** Owns one `ConfigStore` per channel. Enforces: at most one session (a direct tune OR a scan) may hold the RTL-SDR dongle at a time. Starting a scan while directly tuned → rejected (`SessionConflictError`); starting a direct tune while scanning → rejected. Switching the direct-tune channel while already direct-tuned works like the current app (old session torn down, new one starts). |
| `api_integration_example.py` | Example FastAPI router showing the intended replacement for `api/stream.py` and the old radio trio. Routes: `GET /radio/stream/{channel}.mp3`, `POST /radio/scan/start`, `GET /radio/scan/stream.mp3`, `POST /radio/scan/resume`, `POST /radio/scan/stop`, `GET /radio/status`, `PATCH /radio/{channel}/params`. Session conflicts return HTTP 409, not a silently-killed stream. |

All files compile cleanly (`python3 -m py_compile`). Two isolated functional
tests were run against fakes (no real hardware):
1. `AdaptiveSquelch` — confirmed the gate opens on a synthetic tone burst
   and closes after the hang timer with a smooth envelope (not a hard cut).
2. `ScanController` — confirmed it cycles through channels with no traffic,
   locks onto a channel where traffic was injected, stays locked, and
   resumes cycling only after `resume()` is called.

**Neither has been tested against real RTL-SDR hardware or real RF noise.**
That's the most important next step (see below).

## Integration steps

1. Copy the `navaos_audio/` package into `/home/kevin/nava-os/backend/`.
2. Install the new dependency: `pip install webrtcvad` (inside the
   project's `.venv`).
3. Delete the old, conflicting scanning system:
   - `api/radio.py`
   - `services/radio_service.py`
   - `services/sdr_service.py`
   - `services/sdr_service.py.save` (stray editor backup, already flagged
     for removal from git)
4. Replace `api/stream.py`'s contents with a router built on
   `channel_manager` (see `api_integration_example.py` — note it uses
   `prefix="/radio"`, matching the existing URL scheme the frontend
   already expects, so no frontend changes should be needed for the
   direct-tune path).
5. Update `main.py` to include only the new single router (remove the
   `radio_router` import/include entirely).
6. Restart `navaos.service` and verify with `systemctl status navaos.service`
   and `journalctl -u navaos.service -f`.

## Known caveats / open items for real-hardware testing

- **VAD tuning against real RF noise is unverified.** In a synthetic test,
  WebRTC VAD occasionally false-triggered on pure Gaussian noise once the
  noise floor had settled. Real VHF hiss has different spectral
  characteristics than synthetic noise — `vad_aggressiveness` (0–3) and
  `open_threshold_db`/`close_threshold_db` in `ChannelConfig` will very
  likely need real-world tuning. Suggest logging `squelch.is_open` and
  `squelch.noise_floor_dbfs` for a day of real channel 16 traffic before
  finalizing defaults.
- **AGC/leveler is not implemented.** `ChannelConfig` has the fields
  (`agc_enabled`, `agc_target_rms_dbfs`, `agc_max_gain_db`) reserved but
  `AudioPipeline`/`ScanController` don't use them yet. Natural next
  increment once squelch is validated on real hardware.
- **UI sliders don't exist yet.** `PATCH /radio/{channel}/params` is the
  backend endpoint for them; no frontend work has been done.
- **The Pi currently has broken outbound IPv4** (confirmed: IPv6 works
  fine to arbitrary hosts, IPv4 times out even to `1.1.1.1`, likely a
  Verizon LTE carrier-NAT/IPv6-preferred issue). This blocks the Pi from
  reaching `github.com` directly (GitHub has no IPv6 addresses). Current
  workaround: push/pull via a laptop acting as a relay over the existing
  SSH tunnel (`git clone`/`pull`/`push` with
  `core.sshCommand="ssh -o ProxyCommand='cloudflared access ssh --hostname ssh.nnwx.com'"`).
  This needs a real fix (router reboot, or contacting Verizon about IPv4
  provisioning) — the local router admin UI is reachable on the LAN
  (`192.168.0.1`, self-signed cert) once port-forwarded through the same
  SSH tunnel, but hasn't been usable via browser yet due to what looked
  like a stale/dropped tunnel session rather than the router itself.
- **SSH access to the Pi is currently password-based with no additional
  hardening applied yet.** Worth doing before/alongside this work:
  confirm `PasswordAuthentication no` / `PermitRootLogin no` in
  `sshd_config`, confirm whether `ssh.nnwx.com` sits behind a Cloudflare
  Access policy (browser-based login) or is a bare tunnel, and consider
  `fail2ban` as a backstop.
- **A previous git mistake was caught and remediated this session**: an
  early `git init` was run in the home directory and briefly committed
  `.bash_history`, `.Xauthority`, and cache files (including one GitHub
  personal access token that had been typed into a command). That commit
  was never pushed; the token was revoked; the repo was rebuilt correctly
  scoped to just the backend source. Worth a reminder to always `git init`
  inside the project directory, never `$HOME`.

## Suggested order of work for Claude Code

1. Do the file integration steps above (copy package, delete old files,
   rewrite `stream.py`/`main.py`).
2. Get it running on real hardware; watch `journalctl -u navaos.service -f`
   for errors on first boot with the new code (most likely failure point:
   `webrtcvad` frame-size assumptions — it requires exactly 10/20/30ms
   frames at 8/16/32/48kHz, which `scan_controller.py`/`audio_pipeline.py`
   already compute correctly, but worth double-checking against the real
   `rtl_fm` output rate).
3. Tune squelch thresholds against real channel 16 traffic.
4. Only after squelch is validated: build the AGC/leveler increment.
5. Only after that: expose the UI sliders on the frontend.
