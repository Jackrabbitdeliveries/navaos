"""
Cycles through a set of marine channels using a single RTL-SDR session,
locking onto whichever channel currently has voice traffic (as judged by
AdaptiveSquelch) and staying there until either the user explicitly resumes,
or the channel has been continuously quiet for `auto_unlock_quiet_s` (a long
backstop timeout, not a quick auto-advance -- see resume()/auto-unlock notes
below). Timing (dwell_seconds/lock_sustain_s/auto_unlock_quiet_s) lives in a
ScanSettings ConfigStore, read live each frame - like per-channel squelch
tuning, it's adjustable without restarting the scan.

Because rtl_fm can't be retuned while running, moving to the next channel
means stopping and restarting the receiver subprocess at the new frequency.
That's unavoidable with this tool, but it's fast (well under the dwell
time) and only happens between dwell periods -- never while locked and
listening.

Only one ScanController (and no concurrent AudioPipeline) may run at a
time, since there is exactly one physical RTL-SDR dongle. ChannelManager
enforces that rule; this class assumes it already has exclusive access.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import numpy as np

from .config import ConfigStore, ScanSettings
from .ffmpeg_encoder import FFmpegEncoder
from .iq_receiver import make_receiver
from .squelch import AdaptiveSquelch

_VAD_FRAME_MS = 20  # webrtcvad supports 10/20/30ms frames only

# If a channel visit produces zero frames (rtl_fm failed to start/claim the
# device), back off before retrying instead of respawning it instantly in a
# tight loop - and give up entirely after enough consecutive failures, since
# that pattern means a device-level problem, not a bad channel.
_FAILURE_BACKOFF_BASE_S = 0.5
_FAILURE_BACKOFF_MAX_S = 10.0
_MAX_CONSECUTIVE_FAILURES = 5


class ScanController:
    def __init__(
        self,
        config_stores: "dict[str, ConfigStore]",
        settings_store: "ConfigStore[ScanSettings]",
        device_index: int = 0,
    ):
        """config_stores: ordered mapping of channel key -> its ConfigStore.
        Scan order follows dict insertion order (Python dicts preserve it).

        settings_store: holds the scan-wide ScanSettings (dwell_seconds,
        lock_sustain_s, auto_unlock_quiet_s) - shared across every channel
        this controller cycles through, and re-read live each frame so
        changes take effect on a running scan without restarting it.
        """
        self._stores = config_stores
        self._order = list(config_stores.keys())
        self._device_index = device_index
        self._settings_store = settings_store

        self._subscribers: "list[queue.Queue[bytes]]" = []
        self._subscribers_lock = threading.Lock()

        self._thread: "threading.Thread | None" = None
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()

        self._state_lock = threading.Lock()
        self._current_channel: Optional[str] = None
        self._locked = False
        self._last_error: Optional[str] = None

        # Set by the owner (ChannelManager) to learn when this controller
        # gives up on its own (repeated rtl_fm failures) rather than via an
        # explicit stop() - so "active" bookkeeping doesn't stay stale.
        self.on_unexpected_stop: Optional[Callable[[], None]] = None

    # ---- public control -----------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Blocks until the scan thread's `finally` block has actually
        stopped the rtl_fm/ffmpeg subprocesses, so the caller can safely
        start a new session against the same dongle the instant this
        returns."""
        self._stop_event.set()
        self._notify_subscribers_ended()
        if self._thread is not None:
            self._thread.join(timeout=8)

    def _notify_subscribers_ended(self) -> None:
        with self._subscribers_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(None)  # wake any blocked listener so its HTTP response can end
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(None)
                    except queue.Empty:
                        pass

    def resume(self) -> None:
        """Unlock from the current channel and continue cycling.

        No-op if the scanner isn't currently locked on a channel.
        """
        with self._state_lock:
            self._locked = False
        self._resume_event.set()

    def subscribe(self) -> "queue.Queue[bytes]":
        q: "queue.Queue[bytes]" = queue.Queue(maxsize=64)
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[bytes]") -> None:
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def status(self) -> dict:
        with self._state_lock:
            return {
                "mode": "scan",
                "current_channel": self._current_channel,
                "locked": self._locked,
                "error": self._last_error,
            }

    # ---- internals ------------------------------------------------------

    def _broadcast(self, chunk: bytes) -> None:
        with self._subscribers_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(chunk)
                    except queue.Empty:
                        pass

    def _run(self) -> None:
        index = 0
        consecutive_failures = 0
        gave_up = False
        while not self._stop_event.is_set():
            channel = self._order[index]
            cfg = self._stores[channel].get()

            with self._state_lock:
                self._current_channel = channel
                self._locked = False
            self._resume_event.clear()

            receiver = make_receiver(cfg, self._device_index)
            squelch = AdaptiveSquelch(cfg)
            encoder = FFmpegEncoder(cfg)

            frame_samples = int(cfg.sample_rate * _VAD_FRAME_MS / 1000)
            frame_bytes = frame_samples * 2  # int16 mono

            receiver.start()
            encoder.start()

            def pump_encoder_output(enc: FFmpegEncoder = encoder) -> None:
                for mp3_chunk in enc.read_mp3_chunks():
                    if self._stop_event.is_set():
                        break
                    self._broadcast(mp3_chunk)

            output_thread = threading.Thread(target=pump_encoder_output, daemon=True)
            output_thread.start()

            # Dwell is timed from the first frame, not from receiver.start():
            # device open takes ~0.7 s, which used to eat most of a 1 s dwell
            # and left less listening time than lock_sustain_s needs.
            dwell_start: Optional[float] = None
            advance = False
            open_since: Optional[float] = None
            quiet_since: Optional[float] = None

            got_any_frame = False
            unexpected_eof = False
            try:
                for pcm_chunk in receiver.read_frames(frame_bytes):
                    got_any_frame = True
                    if dwell_start is None:
                        dwell_start = time.monotonic()
                    if self._stop_event.is_set():
                        break

                    latest_cfg = self._stores[channel].get()
                    if latest_cfg != squelch.config:
                        squelch.update_config(latest_cfg)

                    settings = self._settings_store.get()

                    frame = np.frombuffer(pcm_chunk, dtype=np.int16)
                    gated = squelch.process(
                        frame, cfg.sample_rate, rf_snr_db=getattr(receiver, "rf_snr_db", None)
                    )
                    encoder.write(gated.tobytes())

                    now = time.monotonic()
                    with self._state_lock:
                        locked = self._locked

                    if squelch.is_open:
                        quiet_since = None
                        if not locked:
                            if open_since is None:
                                open_since = now
                            elif (now - open_since) >= settings.lock_sustain_s:
                                with self._state_lock:
                                    self._locked = True
                                locked = True
                    else:
                        open_since = None
                        if locked:
                            if quiet_since is None:
                                quiet_since = now
                            elif (now - quiet_since) >= settings.auto_unlock_quiet_s:
                                with self._state_lock:
                                    self._locked = False
                                locked = False
                                quiet_since = None

                    if locked:
                        if self._resume_event.is_set():
                            advance = True
                            break
                        continue

                    # Don't hop away while the gate is open and a lock is pending.
                    if open_since is None and (now - dwell_start) > settings.dwell_seconds:
                        advance = True
                        break
                else:
                    # Loop ran out on its own (rtl_fm exited/EOF) rather than
                    # via a `break` above - not a deliberate stop or advance.
                    unexpected_eof = not self._stop_event.is_set()
            finally:
                receiver.stop()
                encoder.stop()
                output_thread.join(timeout=2)

            if self._stop_event.is_set():
                break

            if unexpected_eof or not got_any_frame:
                consecutive_failures += 1
                print(
                    f"SCANCONTROLLER-FAILURE ch={channel} "
                    f"consecutive_failures={consecutive_failures} "
                    f"rtl_fm_stderr_tail={receiver.stderr_tail}",
                    flush=True,
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    with self._state_lock:
                        self._last_error = (
                            f"gave up after {consecutive_failures} consecutive "
                            f"channel visits produced no data (device issue?)"
                        )
                    self._notify_subscribers_ended()
                    gave_up = True
                    break
                backoff = min(
                    _FAILURE_BACKOFF_BASE_S * (2 ** (consecutive_failures - 1)),
                    _FAILURE_BACKOFF_MAX_S,
                )
                time.sleep(backoff)
                index = (index + 1) % len(self._order)
                continue

            consecutive_failures = 0
            if advance:
                index = (index + 1) % len(self._order)
            # If we broke out without `advance` (stop_event fired mid-dwell
            # or mid-lock), the outer while condition will exit the loop.

        with self._state_lock:
            self._current_channel = None
            self._locked = False
        if gave_up and self.on_unexpected_stop is not None:
            self.on_unexpected_stop()
