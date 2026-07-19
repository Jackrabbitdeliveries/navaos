import threading
from services.sdr_service import sdr


class RadioService:

    def __init__(self):
        self.channel = None
        self.scanning = False
        self.mode = "idle"
        self.scan_thread = None
        self.stop_event = threading.Event()
        self.dwell_seconds = 0.8
        self.squelch = 100

        self.channels = [
            {"channel": "09", "freq": 156.450, "name": "Bridge / Hailing"},
            {"channel": "13", "freq": 156.650, "name": "Bridge-to-Bridge"},
            {"channel": "16", "freq": 156.800, "name": "Distress / Calling"},
            {"channel": "68", "freq": 156.425, "name": "Recreational"},
            {"channel": "69", "freq": 156.475, "name": "Recreational"},
            {"channel": "71", "freq": 156.575, "name": "Recreational"},
            {"channel": "72", "freq": 156.625, "name": "Ship-to-Ship"},
        ]

    def scan_loop(self):
        while not self.stop_event.is_set():
            for channel in self.channels:
                if self.stop_event.is_set():
                    break

                self.channel = channel
                print(f"Scanning CH {channel['channel']} {channel['freq']}")
                sdr.play_channel(
                    channel["freq"],
                    seconds=self.dwell_seconds,
                    squelch=self.squelch,
                )

        print("Scanner stopped")

    def start_scan(self):
        if self.scanning:
            return self.status()

        self.stop_event.clear()
        self.scanning = True
        self.mode = "python_scanner"

        self.scan_thread = threading.Thread(target=self.scan_loop, daemon=True)
        self.scan_thread.start()

        return self.status()

    def stop_scan(self):
        self.scanning = False
        self.mode = "idle"
        self.stop_event.set()
        sdr.stop()
        return self.status()

    def tune(self, channel):
        self.stop_scan()

        if channel["channel"] == "WX4":
            self.mode = "weather"
            self.channel = channel
            sdr.play_weather()
            return self.status()

        return {"ok": False, "error": "Manual tuning comes next"}

    def status(self):
        return {
            "mode": self.mode,
            "channel": self.channel,
            "scanning": self.scanning,
            "dwell_seconds": self.dwell_seconds,
            "squelch": self.squelch,
            "channels": self.channels,
        }


radio = RadioService()
