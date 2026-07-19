from fastapi import APIRouter
from services.radio_service import radio
router = APIRouter(prefix="/radio", tags=["radio"])

CHANNELS = [
    {"channel": "09", "freq": 156.450, "name": "Bridge / Hailing"},
    {"channel": "13", "freq": 156.650, "name": "Bridge-to-Bridge"},
    {"channel": "16", "freq": 156.800, "name": "Distress / Calling"},
    {"channel": "68", "freq": 156.425, "name": "Recreational"},
    {"channel": "69", "freq": 156.475, "name": "Recreational"},
    {"channel": "71", "freq": 156.575, "name": "Recreational"},
    {"channel": "72", "freq": 156.625, "name": "Ship-to-Ship"},
    {"channel": "WX4", "freq": 162.425, "name": "NOAA Weather"},
]

radio_state = {
    "mode": "idle",
    "active_channel": None,
    "scanning": False,
}

@router.get("/channels")
def get_channels():
    return CHANNELS

@router.get("/status")
def get_radio_status():
    return radio.status()

@router.post("/tune/{channel}")
def tune_channel(channel: str):
    match = next(
        (c for c in CHANNELS if c["channel"].lower() == channel.lower()),
        None
    )

    if not match:
        return {"ok": False, "error": "Unknown channel"}
    return radio.tune(match)

@router.post("/scan/start")
def start_scan():
    radio.start_scan()
    return radio.status()

@router.post("/scan/stop")
def stop_scan():
    radio.stop_scan()
    return radio.status()
