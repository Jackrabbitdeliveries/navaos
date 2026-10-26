"""
Ask Claude what sailboat is in a photo (Boat ID piece 1).

One Messages API call per sighting, run on a background thread so the web
request returns at once and the page polls for the result. The answer is
constrained to a JSON schema (structured outputs), so the page can compare
it field by field with the user's guess.

Model: claude-opus-5 (Kevin's choice, D3/D4 in docs/boat-id-plan.md), with
server-side fallback ("default") so a safety decline is retried on another
model instead of failing. Key: ~/navaos-data/anthropic_api_key - never
logged or committed.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

import anthropic

from . import store

MODEL = "claude-opus-5"
# Tests and spare dev instances MUST set NAVAOS_BOATID_AI=0: the key file is
# global, so otherwise every test sighting is a real, billed API call.
AI_ENABLED = os.environ.get("NAVAOS_BOATID_AI", "1") != "0"
KEY_FILE = Path.home() / "navaos-data" / "anthropic_api_key"

HULLS = ["monohull", "catamaran", "trimaran", "unknown"]
RIGS = ["sloop", "cutter", "ketch_or_yawl", "schooner", "cat_rig", "unknown"]
SIZES = ["under_30", "30_39", "40_49", "50_plus", "unknown"]

SCHEMA = {
    "type": "object",
    "properties": {
        "is_sailboat": {"type": "boolean"},
        "hull": {"type": "string", "enum": HULLS},
        "rig": {"type": "string", "enum": RIGS},
        "size_class": {"type": "string", "enum": SIZES},
        "length_ft_estimate": {"type": "integer",
                               "description": "Best estimate of overall length in feet; 0 if it can't be judged."},
        "hull_color": {"type": "string"},
        "visible_name": {"type": "string",
                         "description": "Boat name painted on the hull if clearly legible, else empty string. Never guess."},
        "features": {"type": "array", "items": {"type": "string"},
                     "description": "Short visible features, e.g. 'center cockpit', 'bowsprit', 'davits', 'deck saloon'."},
        "description": {"type": "string", "description": "One or two plain sentences describing the boat."},
        "candidates": {
            "type": "array",
            "description": "Up to 5 make/model guesses, most likely first. Empty if no reasonable guess.",
            "items": {
                "type": "object",
                "properties": {
                    "make": {"type": "string"},
                    "model": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "why": {"type": "string"},
                },
                "required": ["make", "model", "confidence", "why"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string",
                  "description": "Anything the user should know, e.g. several boats in view, too far away, partly hidden."},
    },
    "required": ["is_sailboat", "hull", "rig", "size_class", "length_ft_estimate", "hull_color",
                 "visible_name", "features", "description", "candidates", "notes"],
    "additionalProperties": False,
}

SYSTEM = """You identify sailboats in photos taken from the shore or the water around St. Augustine, Florida, for a "guess, then reveal" game.

Be honest about uncertainty: exact make and model from one photo is often impossible, because boats in a model line look alike and are often modified. Give your best ranked candidates with the visible evidence for each, and mark confidence low unless the evidence is distinctive. Never invent a boat name: report visible_name only if it is clearly legible in the photo.

Judge rig from the masts and headsails you can actually see: sloop (one mast, one headsail), cutter (one mast, two headsails), ketch or yawl (two masts, the aft one shorter), schooner (two or more masts, the aft one as tall or taller), cat rig (one mast well forward, no headsail). Use "unknown" when it can't be judged, e.g. sails furled and masts out of frame.

If the photo shows no sailboat, set is_sailboat to false and describe what you see."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=KEY_FILE.read_text().strip(), timeout=120.0, max_retries=2)


def _prompt(row: dict) -> str:
    parts = []
    if row.get("tap_x") is not None and row.get("tap_y") is not None:
        parts.append(f"The user tapped the boat they mean at {row['tap_x'] * 100:.0f}% from the left and "
                     f"{row['tap_y'] * 100:.0f}% from the top of the image. Identify that boat; if several boats "
                     "are near that point, say so in notes.")
    else:
        parts.append("Identify the main sailboat in the photo; if there are several, say so in notes.")
    parts.append("Do not use the user's own guess - it is hidden from you so the two can be compared.")
    return " ".join(parts)


def run(sid: str) -> None:
    """Identify one sighting and store the result (blocking; call via start())."""
    row = store.get(sid)
    if row is None:
        return
    path = store.photo_path(row)
    if path is None:
        store.update(sid, ai_status="error", ai_error="photo missing")
        return
    t0 = time.time()
    try:
        img = base64.standard_b64encode(path.read_bytes()).decode()
        resp = _client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                {"type": "text", "text": _prompt(row)},
            ]}],
        )
        meta = {"model": resp.model, "request_id": resp._request_id, "seconds": round(time.time() - t0, 1),
                "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        if resp.stop_reason == "refusal":
            store.update(sid, ai_status="error", ai_meta=meta,
                         ai_error="The model declined to answer this photo.")
            return
        text = next((b.text for b in resp.content if b.type == "text"), "")
        store.update(sid, ai_status="done", ai=json.loads(text), ai_meta=meta, ai_error=None)
        print(f"BOATID-AI {sid} {meta['model']} {meta['seconds']}s in={meta['input_tokens']} out={meta['output_tokens']}",
              flush=True)
    except FileNotFoundError:
        store.update(sid, ai_status="error", ai_error="No Anthropic API key on the Pi.")
    except anthropic.AuthenticationError:
        store.update(sid, ai_status="error", ai_error="The Anthropic API key was rejected.")
    except anthropic.RateLimitError:
        store.update(sid, ai_status="error", ai_error="Rate limited by the API - try again in a minute.")
    except anthropic.APIStatusError as e:
        store.update(sid, ai_status="error", ai_error=f"API error {e.status_code}: {e.message}"[:300])
    except anthropic.APIConnectionError:
        store.update(sid, ai_status="error", ai_error="Couldn't reach the API (internet down?).")
    except (ValueError, StopIteration) as e:
        store.update(sid, ai_status="error", ai_error=f"Unexpected answer format: {e}"[:300])


def start(sid: str) -> None:
    if not AI_ENABLED:
        store.update(sid, ai_status="error", ai_error="AI disabled (NAVAOS_BOATID_AI=0).")
        return
    threading.Thread(target=run, args=(sid,), daemon=True, name=f"boatid-{sid}").start()
