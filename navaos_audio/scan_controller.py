"""
Cycles through a set of marine channels using a single RTL-SDR session,
locking onto whichever channel currently has voice traffic (as judged by
AdaptiveSquelch) and staying there until explicitly resumed by the user --
it does NOT automatically resume cycling when the traffic stops.

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
from typing import Optional

import numpy as np

from .config import ConfigStore
from .ffmpeg_encoder import FFmpegEncoder
from .sdr_receiver import RTLSDRReceiver
from .squelch import AdaptiveSquelch

_VAD_FRAME_MS = 20  # webrtcvad supports 10/20/30ms frames only


class ScanController:
    def __init__(
        self,
        config_stores: "dict[str, ConfigStore]",
        device_index: int = 0,
        dwell_seconds: float = 2.0,
    ):
        """config_stores: ordered mapping of channel key -> its ConfigStore.
        Scan order follows dict insertion order (Python dicts preserve it).
        """
        self._stores = config_stores
        self._order = list(config_stores.keys())
        self._device_index = device_index
        self._dwell_seconds = dwell_seconds

        self._subscribers: "list[queue.Queue[bytes]]" = []
        self._subscribers_lock = threading.Lock()

        self._thread: "threading.Thread | None" = None
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()

        self._state_lock = threading.Lock()
        self._current_channel: Optional[str] = None
        self._locked = False

    # ---- public control -----------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

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
        while not self._stop_event.is_set():
            channel = self._order[index]
            cfg = self._stores[channel].get()

            with self._state_lock:
                self._current_channel = channel
                self._locked = False
            self._resume_event.clear()

            receiver = RTLSDRReceiver(cfg, self._device_index)
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

            dwell_start = time.monotonic()
            advance = False

            try:
                for pcm_chunk in receiver.read_frames(frame_bytes):
                    if self._stop_event.is_set():
                        break

                    latest_cfg = self._stores[channel].get()
                    if latest_cfg != squelch.config:
                        squelch.update_config(latest_cfg)

                    frame = np.frombuffer(pcm_chunk, dtype=np.int16)
                    gated = squelch.process(frame, cfg.sample_rate)
                    encoder.write(gated.tobytes())

                    if squelch.is_open:
                        with self._state_lock:
                            self._locked = True

                    with self._state_lock:
                        locked = self._locked

                    if locked:
                        if self._resume_event.is_set():
                            advance = True
                            break
                        # Stay on this channel indefinitely while locked --
                        # do NOT auto-advance even after traffic stops.
                        continue

                    if (time.monotonic() - dwell_start) > self._dwell_seconds:
                        advance = True
                        break
            finally:
                receiver.stop()
                encoder.stop()
                output_thread.join(timeout=2)

            if advance:
                index = (index + 1) % len(self._order)
            # If we broke out without `advance` (stop_event fired mid-dwell
            # or mid-lock), the outer while condition will exit the loop.

        with self._state_lock:
            self._current_channel = None
            self._locked = False
