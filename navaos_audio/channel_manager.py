"""
Single point of hardware arbitration.

There is exactly one physical RTL-SDR dongle, so exactly one "session" --
either a direct tune to one channel, or a scan across multiple channels --
may be active at a time. This class owns that rule so the API layer never
has to reason about the hardware constraint itself.

Policy (confirmed with the boat owner):
  - Starting a scan while a direct tune is active is rejected; stop the
    direct listener first.
  - Starting a direct tune while a scan is active is rejected; stop the
    scan first. (Symmetric with the above -- one hardware session at a time.)
  - Switching the direct-tune channel while already directly tuned works
    like the original app: the old channel's session is torn down and the
    new one starts, same as before.
  - When the scanner locks onto a channel with traffic, it stays there
    until the user explicitly calls resume_scan() -- it does not
    auto-resume once the channel goes quiet again.
"""
from __future__ import annotations

import queue
import threading
from typing import Optional

from .audio_pipeline import AudioPipeline
from .config import ChannelConfig, ConfigStore
from .scan_controller import ScanController

CHANNEL_FREQUENCIES_HZ = {
    "09": 156.450e6,
    "13": 156.650e6,
    "16": 156.800e6,
    "68": 156.425e6,
    "71": 156.575e6,
    "wx": 162.425e6,
}

# WX4 (continuous weather broadcast) is excluded from the default scan --
# it would always look like "traffic" and the scanner would lock on it forever.
DEFAULT_SCAN_ORDER = ["09", "13", "16", "68", "71"]


class SessionConflictError(Exception):
    """Raised when a request can't proceed because a conflicting session
    (the other kind, or a different channel) currently holds the hardware."""


class ChannelManager:
    def __init__(self, device_index: int = 0):
        self._device_index = device_index
        self._lock = threading.Lock()

        self._stores: dict[str, ConfigStore] = {
            ch: ConfigStore(ChannelConfig(channel=ch, frequency_hz=freq))
            for ch, freq in CHANNEL_FREQUENCIES_HZ.items()
        }

        self._active_kind: Optional[str] = None       # None | "direct" | "scan"
        self._active_channel: Optional[str] = None    # set when kind == "direct"
        self._direct_pipeline: Optional[AudioPipeline] = None
        self._scan_controller: Optional[ScanController] = None

    # ---- direct tune ------------------------------------------------------

    def subscribe_direct(self, channel: str) -> "queue.Queue[bytes]":
        if channel not in self._stores:
            raise KeyError(f"Unknown channel: {channel}")

        with self._lock:
            if self._active_kind == "scan":
                raise SessionConflictError(
                    "A scan is currently active. Stop the scan before tuning directly."
                )
            if self._active_kind == "direct" and self._active_channel != channel:
                self._teardown_direct_locked()
            if self._direct_pipeline is None:
                self._direct_pipeline = AudioPipeline(self._stores[channel], self._device_index)
                self._active_kind = "direct"
                self._active_channel = channel
            return self._direct_pipeline.subscribe()

    def unsubscribe_direct(self, channel: str, q: "queue.Queue[bytes]") -> None:
        with self._lock:
            if self._direct_pipeline is None or self._active_channel != channel:
                return
            self._direct_pipeline.unsubscribe(q)
            if self._direct_pipeline.subscriber_count == 0:
                self._teardown_direct_locked()

    def stop_direct(self) -> None:
        """Force-tear-down the current direct-tune session, if any.

        Used by an explicit user-facing "stop" action, as opposed to the
        automatic teardown that happens when the last subscriber leaves.
        """
        with self._lock:
            if self._active_kind == "direct":
                self._teardown_direct_locked()

    def _teardown_direct_locked(self) -> None:
        if self._direct_pipeline is not None:
            self._direct_pipeline.force_stop()
        self._direct_pipeline = None
        if self._active_kind == "direct":
            self._active_kind = None
            self._active_channel = None

    # ---- scanning -----------------------------------------------------------

    def start_scan(self, channel_order: Optional[list] = None) -> "queue.Queue[bytes]":
        with self._lock:
            if self._active_kind == "direct":
                raise SessionConflictError(
                    "A direct tune is currently active. Stop it before starting a scan."
                )
            if self._scan_controller is None:
                order = channel_order or DEFAULT_SCAN_ORDER
                stores = {ch: self._stores[ch] for ch in order}
                self._scan_controller = ScanController(stores, self._device_index)
                self._scan_controller.start()
                self._active_kind = "scan"
            return self._scan_controller.subscribe()

    def stop_scan(self) -> None:
        with self._lock:
            if self._scan_controller is not None:
                self._scan_controller.stop()
                self._scan_controller = None
            if self._active_kind == "scan":
                self._active_kind = None

    def resume_scan(self) -> None:
        with self._lock:
            if self._scan_controller is not None:
                self._scan_controller.resume()

    def unsubscribe_scan(self, q: "queue.Queue[bytes]") -> None:
        with self._lock:
            if self._scan_controller is not None:
                self._scan_controller.unsubscribe(q)

    # ---- shared -------------------------------------------------------------

    def get_config(self, channel: str) -> ChannelConfig:
        if channel not in self._stores:
            raise KeyError(f"Unknown channel: {channel}")
        return self._stores[channel].get()

    def update_config(self, channel: str, **changes) -> ChannelConfig:
        if channel not in self._stores:
            raise KeyError(f"Unknown channel: {channel}")
        return self._stores[channel].update(**changes)

    def status(self) -> dict:
        with self._lock:
            if self._active_kind == "direct":
                return {"mode": "direct", "channel": self._active_channel}
            if self._active_kind == "scan" and self._scan_controller is not None:
                return self._scan_controller.status()
            return {"mode": "idle"}


# Singleton used by the FastAPI app.
channel_manager = ChannelManager()
