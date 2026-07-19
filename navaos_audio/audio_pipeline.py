"""
Orchestrates: RTLSDRReceiver -> AdaptiveSquelch -> FFmpegEncoder -> broadcast.

One AudioPipeline instance is created per channel and shared across every
listener currently tuned to that channel. RTL-SDR hardware only supports a
single reader at a time, so this is a hard requirement, not just an
optimization: a single upstream pipeline per channel, fanned out to N
browser tabs via per-listener queues, rather than spawning a new
rtl_fm/ffmpeg process pair per HTTP request.

The pipeline starts on first subscriber and stops when the last one leaves,
so an unwatched channel doesn't tie up the SDR or spend CPU on DSP no one
is listening to.
"""
from __future__ import annotations

import queue
import threading

import numpy as np

from .config import ConfigStore
from .ffmpeg_encoder import FFmpegEncoder
from .sdr_receiver import RTLSDRReceiver
from .squelch import AdaptiveSquelch

_VAD_FRAME_MS = 20  # webrtcvad supports 10/20/30ms frames only


class AudioPipeline:
    def __init__(self, config_store: ConfigStore, device_index: int = 0):
        self._config_store = config_store
        self._device_index = device_index
        self._subscribers: "list[queue.Queue[bytes]]" = []
        self._subscribers_lock = threading.Lock()
        self._thread: "threading.Thread | None" = None
        self._stop_event = threading.Event()

    def subscribe(self) -> "queue.Queue[bytes]":
        q: "queue.Queue[bytes]" = queue.Queue(maxsize=64)
        with self._subscribers_lock:
            self._subscribers.append(q)
        self._ensure_running()
        return q

    def unsubscribe(self, q: "queue.Queue[bytes]") -> None:
        stop_needed = False
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
            stop_needed = not self._subscribers
        if stop_needed:
            self._stop_event.set()

    def _ensure_running(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def subscriber_count(self) -> int:
        with self._subscribers_lock:
            return len(self._subscribers)

    def force_stop(self) -> None:
        """Immediately tear this pipeline down regardless of subscriber count.

        Used when a different session (another channel, or a scan) needs
        exclusive access to the single RTL-SDR dongle.
        """
        self._stop_event.set()
        with self._subscribers_lock:
            self._subscribers.clear()

    def _broadcast(self, chunk: bytes) -> None:
        with self._subscribers_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    # Drop audio for one slow listener rather than blocking
                    # (and stalling) every other listener on this channel.
                    try:
                        q.get_nowait()
                        q.put_nowait(chunk)
                    except queue.Empty:
                        pass

    def _run(self) -> None:
        cfg = self._config_store.get()
        receiver = RTLSDRReceiver(cfg, self._device_index)
        squelch = AdaptiveSquelch(cfg)
        encoder = FFmpegEncoder(cfg)

        frame_samples = int(cfg.sample_rate * _VAD_FRAME_MS / 1000)
        frame_bytes = frame_samples * 2  # int16 mono

        receiver.start()
        encoder.start()

        def pump_encoder_output() -> None:
            for mp3_chunk in encoder.read_mp3_chunks():
                if self._stop_event.is_set():
                    break
                self._broadcast(mp3_chunk)

        output_thread = threading.Thread(target=pump_encoder_output, daemon=True)
        output_thread.start()

        try:
            for pcm_chunk in receiver.read_frames(frame_bytes):
                if self._stop_event.is_set():
                    break

                # Pick up any live config changes (gain/threshold/etc from
                # a future UI slider) without restarting the pipeline.
                latest_cfg = self._config_store.get()
                if latest_cfg != squelch.config:
                    squelch.update_config(latest_cfg)

                frame = np.frombuffer(pcm_chunk, dtype=np.int16)
                gated = squelch.process(frame, cfg.sample_rate)
                encoder.write(gated.tobytes())
        finally:
            receiver.stop()
            encoder.stop()
            output_thread.join(timeout=2)
