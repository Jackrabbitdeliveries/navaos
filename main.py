from fastapi import FastAPI
from datetime import datetime
from api.radio import router as radio_router
from api.stream import router as stream_router

app = FastAPI(title="NavaOS API", version="0.1.0")

app.include_router(radio_router)
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
            "ais": "available_later",
            "camera": "not_installed",
        },
    }
