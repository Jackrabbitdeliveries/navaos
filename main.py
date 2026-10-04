from contextlib import asynccontextmanager

from fastapi import FastAPI
from datetime import datetime
from api.stream import router as stream_router
from navaos_audio.channel_manager import channel_manager
from navaos_audio.transcriber import transcriber


@asynccontextmanager
async def lifespan(app: FastAPI):
    transcriber.start()   # background speech-to-text of recordings (NAVAOS_STT=0 disables)
    yield
    channel_manager.shutdown()


app = FastAPI(title="NavaOS API", version="0.1.0", lifespan=lifespan)

app.include_router(stream_router)

@app.get("/")
def root():
    return {
        "system": "NavaOS",
        "boat": "Nava",
        "status": "online",
        "time": datetime.now().isoformat(),
    }

@app.get("/status")
def status():
    return {
        "system": "NavaOS",
        "boat": "Nava",
        "modules": {
            "radio": "available",
            "radio_stream": "available",
            "bilge": "not_installed",
            "battery": "not_installed",
            "ais": "available",
            "transcription": "available",
            "camera": "not_installed",
        },
    }
