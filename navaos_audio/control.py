"""
Shared-radio control: who gets to change what the single dongle is doing.

Everyone who opens the player shares one radio. Rules (agreed 2026-09-30):

- Whoever changes the radio (tune a channel, start a scan, stop) becomes the
  *holder* and gets a LEASE_S (10 min) turn, timed from when they took it.
  Further changes by the holder don't extend it.
- A change requested by someone else during an active turn is queued (one
  pending request; the latest one wins) and applied when the turn ends - or
  immediately if the holder has left (no heartbeat and no open audio stream
  for ACTIVE_S). A queued request is dropped if its requester has left too.
- Once a turn has expired with nobody waiting, the radio is open: the next
  change applies at once and starts a new turn for that person.
- "Settings" actions (scan list, sensitivity, resume) can't sensibly be
  queued; they're allowed for the holder, when the radio is open, or with the
  PIN, and refused otherwise.
- The override PIN applies anything immediately. It is read from the
  NAVAOS_OVERRIDE_PIN env var or ~/navaos-data/override_pin - never from
  the repo (which is public). Wrong PINs are rate-limited.

Default mode: after IDLE_RETURN_S (20 min) with no activity - no control
or settings request and nobody listening - the radio goes back to scanning
DEFAULT_SCAN_ORDER (the five marine channels; WX is excluded because it
transmits continuously and would hold the scanner forever). A scan that's
already running is left alone, even with a custom list (e.g. a 09-only
bridge watch). The same default starts STARTUP_DEFAULT_S after the service
starts, so the radio is monitoring/recording after any reboot.

The radio's *selection* (idle / direct:<channel> / scan) is tracked here: a
direct-tune pipeline only runs while someone is subscribed, so "the radio is
set to 71" can be true with no pipeline running. Pages follow the selection
and (re)attach their audio to it.
"""
from __future__ import annotations

import collections
import hmac
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .channel_manager import DEFAULT_SCAN_ORDER, SessionConflictError, channel_manager
from .noise_meter import noise_meter as _noise_meter
from .ais import ais_service as _ais_service

LEASE_S = 600
ACTIVE_S = 15
PIN_FILE = Path(os.environ.get("NAVAOS_PIN_FILE", Path.home() / "navaos-data" / "override_pin"))
# AIS timeshare: a window of AIS_WINDOW_S every AIS_INTERVAL_S, only while the
# radio is just scanning (unlocked) or idle, nobody is listening and nothing is
# queued. Any control request or new listener ends a window at once.
AIS_INTERVAL_S = int(os.environ.get("NAVAOS_AIS_INTERVAL_S", "600"))
AIS_WINDOW_S = int(os.environ.get("NAVAOS_AIS_WINDOW_S", "90"))
AIS_ENABLED = os.environ.get("NAVAOS_AIS", "1") != "0"
IDLE_RETURN_S = 1200
STARTUP_DEFAULT_S = 30
# Tests and spare dev instances MUST set NAVAOS_DEFAULT_SCAN=0, or importing
# this module starts a real scan on the dongle STARTUP_DEFAULT_S later.
DEFAULT_SCAN_ENABLED = os.environ.get("NAVAOS_DEFAULT_SCAN", "1") != "0"
PIN_MAX_FAILURES = 5
PIN_FAILURE_WINDOW_S = 600

RADIO_ACTIONS = ("tune", "scan", "stop", "noise")   # queueable
SETTINGS_ACTIONS = ("resume",)                # immediate-or-refused


class ControlError(Exception):
    """Refused request; `status` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 423):
        super().__init__(message)
        self.status = status


def _configured_pin() -> Optional[str]:
    pin = os.environ.get("NAVAOS_OVERRIDE_PIN")
    if pin:
        return pin.strip()
    try:
        return PIN_FILE.read_text().strip() or None
    except OSError:
        return None


class ControlManager:
    def __init__(self, cm=channel_manager, meter=_noise_meter, ais=_ais_service):
        self._cm = cm
        self._meter = meter
        self._ais = ais
        self._ais_active = False
        self._ais_until = 0.0
        self._ais_next = time.monotonic() + 120   # first window ~2 min after start
        self._lock = threading.RLock()
        self._selection: dict = {"mode": "idle", "channel": None}
        self._holder: Optional[str] = None
        self._lease_until = 0.0
        self._pending: Optional[dict] = None
        self._seen: dict[str, float] = {}
        self._streams: collections.Counter = collections.Counter()
        self._pin_failures: collections.deque = collections.deque()
        self._last_change: Optional[dict] = None
        # Pretend the last activity was long enough ago that the default scan
        # starts STARTUP_DEFAULT_S after boot.
        self._last_activity = time.monotonic() - IDLE_RETURN_S + STARTUP_DEFAULT_S
        threading.Thread(target=self._tick_loop, daemon=True).start()

    # ---- presence ---------------------------------------------------------

    def heartbeat(self, client: Optional[str]) -> None:
        if client:
            with self._lock:
                self._seen[client] = time.monotonic()

    def stream_opened(self, client: Optional[str]) -> None:
        if client:
            with self._lock:
                self._end_ais("listener")
                self._streams[client] += 1
                self._seen[client] = self._last_activity = time.monotonic()

    def stream_closed(self, client: Optional[str]) -> None:
        if client:
            with self._lock:
                self._streams[client] -= 1
                if self._streams[client] <= 0:
                    del self._streams[client]
                # The idle clock starts when the last listener stops.
                self._last_activity = time.monotonic()

    def _active(self, client: Optional[str]) -> bool:
        if not client:
            return False
        # An open audio stream counts: a phone with its screen locked stops
        # polling but is still listening.
        return self._streams.get(client, 0) > 0 or (time.monotonic() - self._seen.get(client, -1e9)) < ACTIVE_S

    def _turn_active(self) -> bool:
        return self._holder is not None and time.monotonic() < self._lease_until and self._active(self._holder)

    # ---- PIN ----------------------------------------------------------------

    def _pin_ok(self, pin: Optional[str]) -> bool:
        if not pin:
            return False
        now = time.monotonic()
        while self._pin_failures and now - self._pin_failures[0] > PIN_FAILURE_WINDOW_S:
            self._pin_failures.popleft()
        if len(self._pin_failures) >= PIN_MAX_FAILURES:
            raise ControlError("Too many wrong PIN attempts - try again in a few minutes.", 429)
        expected = _configured_pin()
        if expected and hmac.compare_digest(pin.strip().encode(), expected.encode()):
            return True
        self._pin_failures.append(now)
        raise ControlError("Wrong PIN.", 403)

    # ---- requests -------------------------------------------------------------

    def request(self, client: str, action: str, channel: Optional[str] = None, pin: Optional[str] = None) -> dict:
        """Apply, queue, or refuse a radio action. Returns {"result": "applied"
        | "queued" | "joined" | "cancelled", ...status}."""
        if action not in RADIO_ACTIONS + SETTINGS_ACTIONS + ("cancel",):
            raise ControlError(f"Unknown action {action!r}", 400)
        if action == "tune":
            if not channel:
                raise ControlError("tune needs a channel", 400)
            self._cm.get_config(channel)  # KeyError -> 404 upstream
        with self._lock:
            self.heartbeat(client)
            if action != "cancel":
                self._end_ais("request")
            self._reconcile()
            self._last_activity = time.monotonic()

            if action == "cancel":
                if self._pending and self._pending["by"] == client:
                    self._pending = None
                return {"result": "cancelled", **self.status(client)}

            # Joining what's already selected is never a change.
            if action == "tune" and self._selection == {"mode": "direct", "channel": channel}:
                return {"result": "joined", **self.status(client)}
            if action == "scan" and self._selection["mode"] == "scan":
                return {"result": "joined", **self.status(client)}
            if action == "stop" and self._selection["mode"] == "idle":
                return {"result": "joined", **self.status(client)}
            if action == "noise" and self._selection["mode"] == "noise":
                return {"result": "joined", **self.status(client)}

            mine = self._holder == client
            turn = self._turn_active()
            # The PIN only matters when it's actually needed, so a remembered
            # PIN sent with every request can't trip the wrong-PIN lockout
            # (or be locked out by someone else's guesses) on normal taps.
            override = bool(pin) and turn and not mine and self._pin_ok(pin)

            if action in SETTINGS_ACTIONS:
                if not (override or mine or not turn):
                    raise ControlError(self._locked_message())
                self._apply(action, channel)
                return {"result": "applied", **self.status(client)}

            if override or mine or not turn:
                if override or not turn:
                    # A new turn starts for whoever takes an open radio, or
                    # with the PIN (which also clears anyone waiting). The
                    # holder's changes during their turn keep its deadline.
                    self._holder = client
                    self._lease_until = time.monotonic() + LEASE_S
                if override:
                    self._pending = None
                self._apply(action, channel)
                self._last_change = {"by": client, "at": time.time()}
                return {"result": "applied", **self.status(client)}

            self._pending = {"by": client, "action": action, "channel": channel, "at": time.time()}
            return {"result": "queued", **self.status(client)}

    def check_settings(self, client: Optional[str], pin: Optional[str]) -> None:
        """Raise ControlError unless this client may change shared settings
        (scan list, per-channel squelch) right now."""
        with self._lock:
            self.heartbeat(client)
            self._last_activity = time.monotonic()
            if self._holder == client or not self._turn_active():
                return
            if pin and self._pin_ok(pin):
                return
            raise ControlError(self._locked_message())

    def _locked_message(self) -> str:
        left = max(0, int(self._lease_until - time.monotonic()))
        return (f"Another listener has the radio for {left // 60}:{left % 60:02d} more. "
                f"Settings unlock when their turn ends (or use the PIN).")

    # ---- applying -------------------------------------------------------------

    def _apply(self, action: str, channel: Optional[str]) -> None:
        cm = self._cm
        if action in RADIO_ACTIONS and action != "noise":
            self._meter.stop()
        if action == "tune":
            cm.stop_scan()
            if cm.status().get("mode") == "direct" and cm.status().get("channel") != channel:
                cm.stop_direct()
            self._selection = {"mode": "direct", "channel": channel}
        elif action == "scan":
            cm.stop_direct()
            q = cm.start_scan()        # may raise ValueError (empty list)
            cm.unsubscribe_scan(q)     # we only wanted it started
            self._selection = {"mode": "scan", "channel": None}
        elif action == "stop":
            cm.stop_direct()
            cm.stop_scan()
            self._selection = {"mode": "idle", "channel": None}
        elif action == "noise":
            cm.stop_direct()
            cm.stop_scan()
            self._meter.start()
            self._selection = {"mode": "noise", "channel": None}
        elif action == "resume":
            cm.resume_scan()

    # ---- AIS timeshare --------------------------------------------------------

    def _end_ais(self, why: str) -> None:
        """Stop an AIS window (if any) and put the scan back if the radio was
        scanning. Selection never changes for AIS - it's a pause."""
        if not self._ais_active:
            return
        self._ais.stop()
        self._ais_active = False
        self._ais_next = time.monotonic() + AIS_INTERVAL_S
        if self._selection["mode"] == "scan":
            try:
                self._cm.unsubscribe_scan(self._cm.start_scan())
            except (ValueError, SessionConflictError) as e:
                print(f"AIS-RESUME-SCAN-FAILED {e}", flush=True)
        print(f"AIS-WINDOW-END ({why})", flush=True)

    def _ais_eligible(self) -> bool:
        if self._selection["mode"] not in ("scan", "idle") or self._pending is not None:
            return False
        if any(n > 0 for n in self._streams.values()):
            return False
        return not self._cm.status().get("locked")   # a conversation is going on

    def _start_ais(self, now: float) -> bool:
        if self._selection["mode"] == "scan":
            self._cm.stop_scan()
        if not self._ais.start():
            print(f"AIS-START-FAILED {self._ais.error}", flush=True)
            self._ais_next = now + AIS_INTERVAL_S
            if self._selection["mode"] == "scan":
                self._cm.unsubscribe_scan(self._cm.start_scan())
            return False
        self._ais_active = True
        self._ais_until = now + AIS_WINDOW_S
        print(f"AIS-WINDOW-START {AIS_WINDOW_S}s", flush=True)
        return True

    def _maybe_ais(self, now: float) -> None:
        if self._ais_active:
            if now >= self._ais_until or not self._ais.running or not self._ais_eligible():
                self._end_ais("done" if now >= self._ais_until else "interrupted")
            return
        if AIS_ENABLED and now >= self._ais_next and self._ais_eligible():
            self._start_ais(now)

    def ais_now(self, client: Optional[str], pin: Optional[str]) -> None:
        """'Update now' from the map page: same permission as settings, and
        never cuts off someone who's listening."""
        self.check_settings(client, pin)
        with self._lock:
            if self._ais_active:
                return
            if any(n > 0 for n in self._streams.values()):
                raise ControlError("Someone is listening to the radio - AIS will update when it's free.", 409)
            if not self._ais_eligible():
                raise ControlError("The radio is busy (tuned, noise meter, or a conversation) - try again later.", 409)
            if not self._start_ais(time.monotonic()):
                raise ControlError(self._ais.error or "AIS couldn't start.", 500)

    def wake_for_listener(self) -> None:
        """Someone wants scan audio: end an AIS window so the scan is back."""
        with self._lock:
            self._end_ais("listener")

    def ais_status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                "enabled": AIS_ENABLED,
                "active": self._ais_active,
                "remaining_s": max(0, int(self._ais_until - now)) if self._ais_active else 0,
                "next_in_s": None if self._ais_active or not AIS_ENABLED else max(0, int(self._ais_next - now)),
                "interval_s": AIS_INTERVAL_S,
                "window_s": AIS_WINDOW_S,
                "last_window": self._ais.last_window,
                "error": self._ais.error,
            }

    def _reconcile(self) -> None:
        if self._ais_active:
            return   # the scan is deliberately paused for AIS
        # A scan that died on its own (device error) leaves the selection stale.
        if self._selection["mode"] == "scan" and self._cm.status().get("mode") != "scan":
            self._selection = {"mode": "idle", "channel": None}
        if self._selection["mode"] == "noise" and not self._meter.running:
            self._selection = {"mode": "idle", "channel": None}

    def _tick_loop(self) -> None:
        while True:
            time.sleep(1.0)
            try:
                self._tick()
            except Exception as e:  # never let the loop die
                print(f"CONTROL-TICK-ERROR {e!r}", flush=True)

    def _tick(self) -> None:
        with self._lock:
            self._reconcile()
            now = time.monotonic()
            for c, t in list(self._seen.items()):
                if now - t > 3600 and not self._streams.get(c):
                    del self._seen[c]
            if self._pending is not None and not self._turn_active():
                p, self._pending = self._pending, None
                if self._active(p["by"]):
                    self._holder = p["by"]
                    self._lease_until = now + LEASE_S
                    try:
                        self._apply(p["action"], p["channel"])
                        self._last_change = {"by": p["by"], "at": time.time()}
                        print(f"CONTROL-QUEUED-APPLIED {p['action']} {p['channel'] or ''}", flush=True)
                    except (ValueError, SessionConflictError) as e:
                        print(f"CONTROL-QUEUED-FAILED {p['action']}: {e}", flush=True)
            elif self._holder is not None and not self._active(self._holder):
                self._holder = None  # holder left: radio is open
            self._maybe_ais(now)
            if not self._ais_active:
                self._maybe_default_scan(now)

    def _maybe_default_scan(self, now: float) -> None:
        if not DEFAULT_SCAN_ENABLED:
            return
        if self._selection["mode"] == "scan" or self._pending is not None:
            return
        if any(n > 0 for n in self._streams.values()):
            return
        if now - self._last_activity < IDLE_RETURN_S:
            return
        cm = self._cm
        for ch in cm.get_scan_channels():
            if ch not in DEFAULT_SCAN_ORDER:
                cm.remove_scan_channel(ch)
        for ch in DEFAULT_SCAN_ORDER:
            cm.add_scan_channel(ch)
        self._holder = None
        self._last_activity = now  # don't retry every second if it fails
        try:
            self._apply("scan", None)
            self._last_change = {"by": None, "at": time.time()}
            print(f"CONTROL-DEFAULT-SCAN channels={cm.get_scan_channels()}", flush=True)
        except (ValueError, SessionConflictError) as e:
            print(f"CONTROL-DEFAULT-SCAN-FAILED {e}", flush=True)

    # ---- status -------------------------------------------------------------

    def status(self, client: Optional[str] = None) -> dict:
        with self._lock:
            self._reconcile()
            now = time.monotonic()
            turn = self._turn_active()
            pending = None
            if self._pending:
                applies_in = max(0, int(self._lease_until - now)) if turn else 0
                pending = {
                    "action": self._pending["action"],
                    "channel": self._pending["channel"],
                    "yours": self._pending["by"] == client,
                    "applies_in_s": applies_in,
                }
            return {
                "selection": dict(self._selection),
                "control": {
                    "yours": self._holder is not None and self._holder == client,
                    "turn_active": turn,
                    "turn_remaining_s": max(0, int(self._lease_until - now)) if turn else 0,
                    "pending": pending,
                    "last_change_by_you": bool(self._last_change and self._last_change["by"] == client),
                    "last_change_default": bool(self._last_change and self._last_change["by"] is None),
                    "listeners": sum(1 for c in self._streams if self._streams[c] > 0),
                    "ais_active": self._ais_active,
                    "ais_remaining_s": max(0, int(self._ais_until - now)) if self._ais_active else 0,
                    "default_scan_in_s": (
                        None if not DEFAULT_SCAN_ENABLED or self._selection["mode"] == "scan"
                        or any(n > 0 for n in self._streams.values())
                        else max(0, int(IDLE_RETURN_S - (now - self._last_activity)))
                    ),
                },
            }

    def touch(self, client: Optional[str]) -> None:
        """Count as user activity (e.g. someone watching the noise meter), so
        the 20-min default scan doesn't take the radio away mid-test."""
        with self._lock:
            self.heartbeat(client)
            self._last_activity = time.monotonic()

    def selection(self) -> dict:
        with self._lock:
            self._reconcile()
            return dict(self._selection)


control = ControlManager()
