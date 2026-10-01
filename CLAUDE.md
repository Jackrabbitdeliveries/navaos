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

NavaOS is a Raspberry Pi–based monitoring system for a sailboat (Nava). The first
module is a **marine VHF receiver**: an RTL-SDR dongle on the Pi, served over
the internet so the owner can listen to live channel audio (direct tune or
scan) from a phone/browser anywhere.

- Host: Raspberry Pi 5 (`nava-pi`), user `kevin`
- Remote access: Cloudflare Tunnel (`cloudflared` service) — `ssh.nnwx.com`
  for SSH, `vhf.nnwx.com` for the web app. No port forwarding.
- **Location (since 2026-09-30 evening): at Kevin's home, not on the boat.**
  Moved home because of recurring problems aboard. Wired Ethernet (`eth0`,
  DHCP 10.0.0.x) to an Xfinity router; IPv4 + IPv6 both work. Tailscale is
  also installed (`tailscale0`). Antenna/cable: **same whip + cable as on the boat**, indoors in front of a window.
- On the boat it was on Verizon LTE (IPv4 flaky in July) with a new outdoor
  VHF antenna ~6 ft above deck (late Sep 2026).
- RF numbers below were measured **on the boat**; they don't transfer to the
  home setup.

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
- Logs: `journalctl -u navaos.service -f` — watch for `SQUELCH-OPEN` /
  `SQUELCH-CLOSE` (one pair per transmission) and `*-FAILURE` lines.
- Restarting interrupts any listener/scan — check `journalctl` for recent
  `/radio/status` polling from a client before restarting, and tell Kevin.

Key installed deps (no requirements.txt yet): fastapi 0.139, uvicorn 0.49,
numpy 2.5, pydantic 2.13, webrtcvad 2.0.10, setuptools 80.x (webrtcvad needs
`pkg_resources`; setuptools ≥81 would break it). System: `rtl_fm`, `ffmpeg`.

## Architecture

```
rtl_sdr (IQ 240 kS/s, tuned +50 kHz) → IQReceiver (RF SNR + NBFM demod → PCM 48 kHz)
    → AdaptiveSquelch (RF mode) → FFmpegEncoder (filters + MP3) → browser
```
Legacy path (`ChannelConfig.receiver="rtl_fm"`): rtl_fm → AdaptiveSquelch
(audio/VAD mode) → … — kept as a fallback.

- `main.py` — FastAPI app; mounts the single router; shuts down
  `channel_manager` on lifespan exit.
- `api/stream.py` — all HTTP routes under `/radio`, plus the `/radio/player`
  HTML page (web UI is inline in this file). Channel table `CHANNELS` lives here.
- `navaos_audio/` package:
  - `config.py` — `ChannelConfig` (per-channel, frozen dataclass),
    `ScanSettings` (scan-wide), thread-safe `ConfigStore` for live updates.
  - `iq_receiver.py` — **default receiver.** `IQReceiver` runs `rtl_sdr`
    and, per 20 ms frame, measures `rf_snr_db` (channel ±6 kHz vs. band-median
    noise, ~0 dB when empty) and demodulates NBFM with rtl_fm-compatible
    scaling + 75 µs de-emphasis (pure numpy, ~5% of one core).
    `make_receiver()` picks IQ vs. rtl_fm from config.
  - `sdr_receiver.py` — legacy `RTLSDRReceiver` (rtl_fm); fallback only.
  - `squelch.py` — `AdaptiveSquelch`. **RF mode** (when the receiver supplies
    `rf_snr_db`): open ≥ `rf_open_threshold_db`, stay open ≥
    `rf_close_threshold_db`, closed for the first 3 frames after a tune.
    **Audio mode** (legacy rtl_fm): VAD + level above adaptive floor, closed
    for the first 10 frames. Both share the hang timer + click-free envelope.
  - `ffmpeg_encoder.py` — filter chain + MP3 encode via stdin:
    highpass 300 → lowpass 3k → afftdn → **+`makeup_gain_db`** → compressor
    (makeup ×`compressor_makeup`) → `volume` → limiter 0.9. NBFM audio leaves
    the demod ~−40 dBFS whatever the signal strength, so the gain is fixed,
    not AGC. `build_filter_chain(cfg)` is shared with the recorder.
  - `recorder.py` — `TransmissionRecorder`: each squelch opening → one MP3
    (same filter chain, own short-lived ffmpeg writing to disk). Files:
    `~/navaos-data/recordings/YYYY-MM-DD/HHMMSS_ch<ch>_<dur>s_<peak snr>dB.mp3`
    (metadata lives in the filename; no DB). Drops clips < 0.5 s, prunes day
    folders > 30 days, pauses under 1 GB free. Env: `NAVAOS_RECORDINGS_DIR`,
    `NAVAOS_RECORDING=0` disables. Fed by both pipelines after the squelch.
  - `audio_pipeline.py` — one per direct-tuned channel; fans MP3 out to N
    subscribers.
  - `scan_controller.py` — cycles channels, locks on traffic (open ≥
    `lock_sustain_s`), auto-resumes after `auto_unlock_quiet_s` of quiet or on
    `resume()`. Dwell is timed from the **first frame** (device open takes
    ~0.7 s) and never hops while the gate is open; a **one-channel scan list
    never hops** (use it for unattended watching of a single channel — scans
    keep running with no listener, direct tune stops when the last listener
    leaves). ~1.75 s per channel,
    ~9 s per 5-channel cycle; lock lands ~1.1 s after arriving on a busy channel.
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
`GET /status`, `GET|PATCH /{channel}/params`, `GET /player`,
`GET /recordings[?channel=&limit=]` (JSON), `GET /recordings/file/{day}/{name}`
(filename-validated), `GET /recordings/view` (page, linked from the player).

The player's per-channel **sensitivity slider** sets `rf_open_threshold_db`
4 (Sensitive) … 16 (Strict) dB, close = open − 4 (min 3). Before 2026-09-30
it set the legacy audio thresholds and did nothing in RF mode. Param changes
are **in memory only — lost on service restart.**

### Channels
| Key | Freq | Notes |
|---|---|---|
| 09 | 156.450 MHz | Bridge of Lions |
| 13 | 156.650 MHz | Bridge-to-bridge |
| 16 | 156.800 MHz | Distress / calling |
| 68 | 156.425 MHz | |
| 71 | 156.575 MHz | |
| wx | 162.425 MHz | NOAA WX4 — continuous but weak here (~+10 dB); the only WX station received. Good weak-signal test |

### Key defaults (`config.py`)
Receiver: `receiver="iq"`, `iq_sample_rate=240000`, `iq_offset_hz=50000`,
`rf_channel_half_bw_hz=6000`, `rf_gain=49.6`.
RF squelch: `rf_open_threshold_db=10`, `rf_close_threshold_db=6`,
`hang_time_s=1.2`. Output: `makeup_gain_db=18`, `compressor_makeup=3`,
`volume=1.0`. Audio-mode (legacy) squelch: `open_threshold_db=6`,
`close_threshold_db=3`, `vad_aggressiveness=2`, `noise_floor_percentile=20`.
Scan: `dwell_seconds=1.0`, `lock_sustain_s=0.4`, `auto_unlock_quiet_s=60`.

## Current status (update this!)

_Last updated: 2026-09-30 (evening — moved home)_

- Integration from HANDOFF.md is complete and running on real hardware.
- Repo unified: Pi's Jul 19 work (previously uncommitted) committed and
  merged with GitHub; service repointed to run from this repo.
- Frame-0 scan false lock: **fixed** (warmup guard in `squelch.py`), verified
  from a live scan on 2026-09-30 — 0 opens during warmup across 331 dwells.
- **Step 3 deployed 2026-09-30 19:17.** First handheld test (19:21, ch71
  direct, whip outdoors, handheld nearby): both transmissions caught (6.1 s,
  5.6 s), handheld at **+43 to +54 dB**, noise between them max +1.3 dB, no
  false opens. **Audio was very quiet** (voice ~−39 dBFS, chain had no gain)
  → added fixed makeup gain + limiter (~+24 dB). Re-test 19:29: level good
  (speech ~−18 to −21 dBFS, peaks −1) but a 120 Hz hum → traced to the
  handheld's charger (see Open issues). **Scan test 19:46 passed:** hopped
  ~1.8 s/channel, opened on arrival at 68 (+59 dB), locked, held after
  unkey, auto-resumed after 60 s quiet.
- Step 3 verification before deploy:
  Verified: synthetic FM — SNR meter accurate to ±0.5 dB from 6–30 dB,
  demod tone level exactly matches rtl_fm scaling; real dongle at home —
  empty channels 0 ± 0.7 dB (max +1.3 in 450 frames); simulated scan —
  cycles, locks, auto-unlocks correctly.
- **Fixed pre-existing scan bug:** dwell was timed from device start, so each
  1 s hop only listened ~0.3 s (< the 0.4 s `lock_sustain_s`) — the scanner
  could almost never lock. Now timed from first frame.
- Per-frame `SQUELCH-DEBUG` print **removed**; replaced by one
  `SQUELCH-OPEN ch= rf_snr= audio_dbfs=` / `SQUELCH-CLOSE ch= open_s=
  peak_rf_snr=` line per transmission.

### Plan (agreed 2026-09-30, in order)
1. ~~Unify repo + deploy path~~ — done 2026-09-30.
2. ~~Baseline the new antenna~~ — done 2026-09-30 (results below).
3. **Move squelch to RF/carrier power** instead of audio loudness (see
   Diagnosis). `rtl_fm` outputs demodulated audio only — no power reading.
   **Decision (2026-09-30): read IQ** (`rtl_sdr`/pyrtlsdr), do FM demod +
   RF power measurement in Python. The FM-noise-squelch shortcut was tested
   and rejected (see "Step 3 prototype test"). Tune offset from the channel
   to avoid the DC/center hump. Keep VAD optional.
   — done & deployed 2026-09-30.
4. ~~Handheld test~~ — passed 2026-09-30 (direct + scan).
5. ~~Remove debug print~~ — done 2026-09-30.
6. **Next (changed 2026-09-30): stay at home for a while.** Kevin plans to
   mount the whip above the house roof and try to receive ch09 (bridge
   tender + boats) during bridge openings. **Transmission recorder built
   2026-09-30** for this (scan list = just 09, leave it running, review on
   /radio/recordings/view).
   Boat test (real traffic; confirm boat hum = battery charger with shore
   power on / charger off) comes later.

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

### RF baseline, new antenna (2026-09-30, 12:45–12:59)
`rtl_power`, gain 49.6, 1 s integration, ±6 kHz per channel; dB relative to the
band-median noise floor. Re-run with `tools/rf_baseline.py` (usage in file).
- **Quiet channels are dead flat:** 68, 09, 16 median ≈ 0 dB, p90 ≤ 0 dB.
- **Real traffic is +24 to +33 dB:** ch68 at 12:46 (3 bursts, 2–6 s, +30–33 dB);
  ch71 at 12:54–12:56 (6 bursts, 2–9 s, ~+24 dB).
- ⇒ **~25 dB margin.** An RF threshold of roughly +10 dB open / +6 dB close
  should gate cleanly; confirm with the handheld test.
- **NOAA wx is weak here: only ~+10 dB** (continuous). Don't use wx as a
  strong reference, and don't apply marine thresholds to it blindly.
- Artifacts, not signals: a ~100 kHz hump around the rtl_power tuning center
  (156.60 MHz, inflated 71/13 readings to +3–7 dB) and a narrow spur at
  156.752 MHz. When measuring power in code, tune offset from the channel so
  the dongle's DC/center hump doesn't land on it.
- Test transmissions from Nava's own radio: **1 W / low power only** — 25 W
  right next to the SDR antenna risks overloading/damaging the RTL-SDR.
- These numbers are for the **boat antenna/location only** — re-baseline
  wherever the receiver moves.

### Step 3 prototype test: FM noise squelch (2026-09-30, on the boat)
Alternative to IQ/RF power that keeps `rtl_fm`: FM receivers "quiet" when a
carrier is present, so energy **above the voice band** drops. 15 s captures
with the service's exact rtl_fm args, 20 ms frames, 100 ms smoothing,
metric = (8–16 kHz energy) − (0.3–3 kHz energy):
- Pure noise (ch16): **+2 to +10 dB** (p1 +1.7).
- Weak carrier + voice (WX4, ~+10 dB RF): **−6 to −2 dB** (p99 −2.3) — clean
  separation even though the weak signal barely reduced absolute HF noise.
- Strong local traffic (ch13, caught mid-capture): 8–16 kHz energy fell
  ~15 dB when the carrier keyed; ratio −8 to −10 dB.
- ⇒ Looked promising on the boat, **but failed the home re-test (same day,
  evening):** pure noise at home scored **−7 dB on every channel** — i.e.
  what counted as "carrier" on the boat. The absolute ratio depends on the
  location/antenna/noise environment, and weak signals (WX on the boat)
  barely quiet the HF noise at all. **Rejected as the primary squelch** —
  go with IQ/RF power (option b), which measured cleanly in both places.

### Home reception (2026-09-30 evening, whip indoors at window)
- FM broadcast stations only +7 to +18 dB (normally +30 dB or more) ⇒ antenna
  heavily attenuated indoors (low-E window glass is a common cause).
- **No NOAA WX signal on any of the 7 WX frequencies**, no marine traffic.
  At home there is currently **no real signal to test squelch against** —
  need the whip outdoors, or another narrowband-FM source (e.g. a local 2 m
  amateur repeater, receive-only). Don't transmit on marine VHF from land
  (FCC: ship stations are for use on vessels).

## Open issues / backlog

(No GitHub issues exist yet — the deploy key can't use the issues API. Track
here until issues are set up.)

- **Monitor all channels at once:** 09/13/16/68/71 span only 156.425–156.800
  MHz, so one ~1 MS/s IQ capture holds all five — demod + record every
  channel simultaneously, no scanning gaps. Main cost: CPU (channelizer).
  Recorder already takes the channel per clip.
- Persist per-channel param changes (slider) across restarts.
- Recordings and player are reachable by anyone who can reach
  `vhf.nnwx.com` — check whether it's behind a Cloudflare Access policy.
- **Faster scanning:** keep the dongle open and retune instead of restarting
  rtl_sdr per hop (rtl_tcp or pyrtlsdr). RF squelch decides in ~60 ms, so a
  5-channel cycle could drop from ~9 s to ~1–2 s.
- **Boat electrical noise (2026-09-30) — likely charger, see below:** on shore power, a ch71 radio check
  from Nava's own VHF had a "wicked hum" on the transmitted audio; hum went
  away when shore power was killed (some static remained). Wi-Fi camera and
  Google Meet also kept dropping. Not the SDR (receive-only). Suspects:
  battery charger/inverter ripple on the 12 V bus, or a shore-power ground
  loop. Next test aboard: shore power ON with charger/inverter OFF — hum gone
  ⇒ charger/inverter; hum stays ⇒ shore-power grounding (galvanic isolator).
  The Pi was also on shore power, so re-check SDR noise floor once fixed.
- **120 Hz hum at home too (2026-09-30 19:29–19:35):** handheld test on ch71
  (+46 dB) had a "major hum". Recording: lines at **120 Hz harmonics**
  (360/480/720/1080/1200 Hz…) = rectified 60 Hz mains, +16 dB above the
  local spectrum on a silent carrier, +23 dB with voice; hum ≈ as loud as
  speech (~0.5 kHz FM deviation). Not software (frame artifacts would be
  50 Hz multiples). No 120 Hz pattern on FM broadcast 102.2/88.5 MHz
  received by the same SDR (but that test is ~20 dB less sensitive) ⇒
  points at the handheld or its surroundings rather than the Pi/SDR.
  **RESOLVED:** hum disappeared with the handheld off its charger (Kevin's
  ear test) — the handheld's charger put 120 Hz ripple on its TX audio.
  Receiver/SDR/software are fine. Likely the same mechanism as the boat hum
  (boat battery charger on shore power → ripple on Nava's radio); confirm
  aboard with shore power ON and charger OFF. Test transmitters must be on
  battery, never on a charger. WX4 not receivable at home even outdoors.
- Add `requirements.txt` (pin setuptools <81 for webrtcvad).
- AGC/leveler not implemented.
- More UI sliders (only RF sensitivity exists; gain/volume/hang time not).
- SSH hardening unverified: `PasswordAuthentication no`, `PermitRootLogin no`,
  Cloudflare Access policy on `ssh.nnwx.com`, fail2ban.
- `~/nava-os/backend` old code copy could be cleaned up (keep `.venv`, or move
  the venv into the repo dir and update the unit).

## Rules / gotchas

- Never `git init` or commit from `$HOME` — a July mistake briefly committed
  shell history and a token (never pushed; token revoked).
- Never commit secrets; `.env` is gitignored.
- Only one process may use the RTL-SDR dongle — always go through
  `channel_manager`, never spawn `rtl_fm`/`rtl_sdr` directly. For ad-hoc
  measurements (rtl_power etc.) first check `GET /radio/status` is `idle`
  and no `rtl_*` process is running — Kevin may be listening.
- webrtcvad needs exact 10/20/30 ms frames at 8/16/32/48 kHz.
- Kevin is the boat owner and sole user, usually connecting from a phone
  over the tunnel. Give step-by-step instructions for anything Kevin must run.
