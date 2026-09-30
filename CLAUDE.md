# NavaOS — Project Guide for Claude Code

> **Required for every session:** read this file before doing any work on
> NavaOS. When you change anything it describes (architecture, deploy
> process, config defaults, hardware, open issues, current status), update
> this file **in the same commit** as the change. Keep "Current status" and
> "Open issues" accurate — they are how the next session knows where things
> stand.
>
> **All development happens on the Pi, in this repo.** Sessions on other
> machines (laptop sandboxes, etc.) are retired — do not make changes there.

## What this is

NavaOS is a Raspberry Pi–based monitoring system aboard a sailboat. The first
module is a **marine VHF receiver**: an RTL-SDR dongle on the Pi, served over
the internet so the owner can listen to live channel audio (direct tune or
scan) from a phone/browser anywhere.

- Host: Raspberry Pi 5 (`nava-pi`), user `kevin`
- Remote access: Cloudflare Tunnel (`cloudflared` service) — `ssh.nnwx.com`
  for SSH, `vhf.nnwx.com` for the web app. No port forwarding.
- Internet: Verizon LTE (IPv6 works; IPv4 was broken in July but works as of
  2026-09-30)
- Antenna: **new outdoor VHF antenna ~6 ft above deck, installed late
  Sep 2026.** All squelch thresholds tuned before this date were against the
  old antenna and should be re-baselined.

## Repo & deploy

- Repo: `~/navaos-project` → `git@github.com:Jackrabbitdeliveries/navaos.git`,
  branch `master`. Pi pushes via an SSH **deploy key** (`~/.ssh/id_ed25519`,
  write access).
- Service: `navaos.service` (systemd) runs
  `uvicorn main:app --host 0.0.0.0 --port 8000` with
  `WorkingDirectory=/home/kevin/navaos-project` — **it runs straight from this
  repo.** Backup of the pre-2026-09-30 unit: `/etc/systemd/system/navaos.service.bak`.
- Python venv: `~/nava-os/backend/.venv` (outside the repo; the service's
  `PATH`/`ExecStart` point at it). The code files in `~/nava-os/backend` are an
  old, **unused** copy — don't edit them.
- Deploy a change: edit → commit → `git push` → `sudo systemctl restart navaos.service`
  (sudo needs Kevin's password; ask Kevin to run it in their own terminal).
- Logs: `journalctl -u navaos.service -f`
- Restarting interrupts any listener/scan — check `journalctl` for recent
  `/radio/status` polling from a client before restarting, and tell Kevin.

Key installed deps (no requirements.txt yet): fastapi 0.139, uvicorn 0.49,
numpy 2.5, pydantic 2.13, webrtcvad 2.0.10, setuptools 80.x (webrtcvad needs
`pkg_resources`; setuptools ≥81 would break it). System: `rtl_fm`, `ffmpeg`.

## Architecture

```
rtl_fm (-l 0, raw PCM 48 kHz) → AdaptiveSquelch → FFmpegEncoder (filters + MP3) → browser
```

- `main.py` — FastAPI app; mounts the single router; shuts down
  `channel_manager` on lifespan exit.
- `api/stream.py` — all HTTP routes under `/radio`, plus the `/radio/player`
  HTML page (web UI is inline in this file). Channel table `CHANNELS` lives here.
- `navaos_audio/` package:
  - `config.py` — `ChannelConfig` (per-channel, frozen dataclass),
    `ScanSettings` (scan-wide), thread-safe `ConfigStore` for live updates.
  - `sdr_receiver.py` — `RTLSDRReceiver`, wraps `rtl_fm` (explicit argv, no
    shell); keeps a tail of rtl_fm stderr for diagnostics.
  - `squelch.py` — `AdaptiveSquelch`: noise-floor estimator + WebRTC VAD +
    hysteresis + hang timer + click-free gain envelope. Forced closed for the
    first 10 frames after (re)tune.
  - `ffmpeg_encoder.py` — existing tuned filter chain + MP3 encode via stdin.
  - `audio_pipeline.py` — one per direct-tuned channel; fans MP3 out to N
    subscribers.
  - `scan_controller.py` — cycles channels, locks on traffic (open ≥
    `lock_sustain_s`), auto-resumes after `auto_unlock_quiet_s` of quiet or on
    `resume()`.
  - `channel_manager.py` — **the single hardware arbiter**: only one session
    (direct tune OR scan) may own the dongle; conflicts → HTTP 409.
    `DEFAULT_SCAN_ORDER = ["09", "13", "16", "68", "71"]`.
- `docs/HANDOFF.md` — original July design brief. Useful background, but
  **partly stale** (integration steps are done; IPv4 and scan-lock notes are
  outdated). This file supersedes it.

### Routes (prefix `/radio`)
`GET /stream/{channel}.mp3`, `GET /stop`, `POST /scan/start`,
`GET /scan/stream.mp3`, `POST /scan/resume`, `POST /scan/stop`,
`GET|POST /scan/channels[/add|/remove]`, `PATCH /scan/params`,
`GET /status`, `GET|PATCH /{channel}/params`, `GET /player`.

### Channels
| Key | Freq | Notes |
|---|---|---|
| 09 | 156.450 MHz | Bridge of Lions |
| 13 | 156.650 MHz | Bridge-to-bridge |
| 16 | 156.800 MHz | Distress / calling |
| 68 | 156.425 MHz | |
| 71 | 156.575 MHz | |
| wx | 162.425 MHz | NOAA WX4 — continuous, good known-good test signal |

### Key defaults (`config.py`)
Squelch: `open_threshold_db=6`, `close_threshold_db=3`, `hang_time_s=1.2`,
`vad_aggressiveness=2`, `noise_floor_percentile=20`, `rf_gain=49.6`.
Scan: `dwell_seconds=1.0`, `lock_sustain_s=0.4`, `auto_unlock_quiet_s=60`.

## Current status (update this!)

_Last updated: 2026-09-30_

- Integration from HANDOFF.md is complete and running on real hardware.
- Repo unified: Pi's Jul 19 work (previously uncommitted) committed and
  merged with GitHub; service repointed to run from this repo.
- Frame-0 scan false lock: **fixed** (warmup guard in `squelch.py`), verified
  from a live scan on 2026-09-30 — 0 opens during warmup across 331 dwells.
- **Temporary `SQUELCH-DEBUG` print is still in `AdaptiveSquelch.process()`**
  (~600 journal lines/min while scanning). Remove or convert to
  `logging.debug` once squelch work is done.

### Plan (agreed 2026-09-30, in order)
1. ~~Unify repo + deploy path~~ — done 2026-09-30.
2. **Baseline the new antenna** — log per-channel RF power, quiet vs. traffic,
   10–15 min; use `wx` as known-good signal.
3. **Move squelch to RF/carrier power** instead of audio loudness (see
   Diagnosis). Check whether `sdr_receiver.py`/`rtl_fm` can expose signal
   power first; keep VAD optional.
4. **Handheld test** on a working channel (68/71, not 16): lock fast, hold
   through transmission, release after hang time.
5. Remove debug print, commit.

Later: AGC/leveler (config fields reserved, not implemented) → UI sliders on
the player page (`PATCH /radio/{channel}/params` already exists).

### Diagnosis driving step 3
From the 2026-09-30 scan logs:
- WebRTC VAD reports `is_speech=True` on ~85% of pure-noise frames — it
  cannot distinguish FM hiss from voice, so it's effectively not gating.
- The gate therefore decides on audio level alone. On FM, **no signal = loud
  hiss; a carrier *quiets* the hiss**, so "audio N dB above floor" is close to
  backwards and produces random single-frame opens right after warmup.
- Marine radios use carrier (RF power) squelch; with the new antenna real
  traffic should stand well clear of the noise floor.

## Open issues / backlog

(No GitHub issues exist yet — the deploy key can't use the issues API. Track
here until issues are set up.)

- Squelch redesign around RF power (plan steps 2–4).
- Remove `SQUELCH-DEBUG` print.
- Add `requirements.txt` (pin setuptools <81 for webrtcvad).
- AGC/leveler not implemented.
- UI sliders not built.
- SSH hardening unverified: `PasswordAuthentication no`, `PermitRootLogin no`,
  Cloudflare Access policy on `ssh.nnwx.com`, fail2ban.
- `~/nava-os/backend` old code copy could be cleaned up (keep `.venv`, or move
  the venv into the repo dir and update the unit).

## Rules / gotchas

- Never `git init` or commit from `$HOME` — a July mistake briefly committed
  shell history and a token (never pushed; token revoked).
- Never commit secrets; `.env` is gitignored.
- Only one process may use the RTL-SDR dongle — always go through
  `channel_manager`, never spawn `rtl_fm` directly.
- webrtcvad needs exact 10/20/30 ms frames at 8/16/32/48 kHz.
- Kevin is the boat owner and sole user, usually connecting from a phone
  over the tunnel. Give step-by-step instructions for anything Kevin must run.
