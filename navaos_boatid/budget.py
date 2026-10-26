"""
Daily spending cap for Boat ID's AI calls (2026-10-26: Kevin set $2/day for
testing). Covers every call - phone snaps, framed feed stills and the
automatic Bridge watch - so the total can't exceed the cap.

Spend is worked out from the token counts already stored with each
sighting (ai_meta), priced per model; today = the Pi's local calendar day.
Cap: "ai_daily_cap_usd" in ~/navaos-data/kit.json (default 2.0).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import store

KIT_FILE = Path.home() / "navaos-data" / "kit.json"
DEFAULT_CAP_USD = 2.0
# $ per million tokens (input, output) - Claude list prices, Oct 2026
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_PRICE = (5.0, 25.0)
EST_CALL_USD = 0.05   # headroom kept for the call about to be made


def cap_usd() -> float:
    try:
        return float(json.loads(KIT_FILE.read_text()).get("ai_daily_cap_usd", DEFAULT_CAP_USD))
    except (OSError, ValueError, TypeError):
        return DEFAULT_CAP_USD


def call_cost(meta: dict) -> float:
    pin, pout = PRICES.get(meta.get("model", ""), FALLBACK_PRICE)
    return (meta.get("input_tokens", 0) * pin + meta.get("output_tokens", 0) * pout) / 1e6


def spent_today() -> float:
    start = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
    total = 0.0
    for r in store.recent(2000):
        if r["created_at"] >= start and r.get("ai_meta"):
            total += call_cost(r["ai_meta"])
        lk = r.get("lookup") or {}
        if (lk.get("at") or 0) >= start:             # a lookup counts on the day it ran, whatever the photo's date
            total += (lk.get("meta") or {}).get("cost_usd", 0) or 0
    return total


def allowed() -> tuple[bool, float, float]:
    """(may we make another call, spent today, cap)."""
    spent, cap = spent_today(), cap_usd()
    return spent + EST_CALL_USD <= cap, spent, cap
