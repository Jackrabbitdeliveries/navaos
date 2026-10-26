"""
Background lookup (2026-10-26): what's publicly known about a named boat.

On demand only (a button on the result screen - it costs money): Claude Opus 5
with the web search server tool searches for the boat's name plus what was
seen (hull type, size, place) and reports builder / model / year / length,
public links (the boat's own YouTube channel, blog, listings) and how well
that matches the photo - many boats share a name. It reports on the *boat*
and its public channels, never personal details about people.

Result stored with the sighting (column `lookup`), so reopening is free.
Budget: counted by budget.py (tokens + $0.01 per web search) within the
daily cap.
"""
from __future__ import annotations

import json
import re
import threading
import time

import anthropic

from . import identify, store

SEARCH_USD = 0.01           # web search: $10 per 1,000 searches
MAX_SEARCHES = 4          # 5 searches cost ~32 cents on the first test (2026-10-26)
MAX_CONTINUATIONS = 3       # pause_turn resumes

SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "True if public information about THIS boat was found."},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"],
                       "description": "How sure it is the same boat as in the photo (name + type + size + area agree?)"},
        "summary": {"type": "string", "description": "Two or three plain sentences about the boat."},
        "builder": {"type": "string"}, "model": {"type": "string"},
        "year": {"type": "integer", "description": "Build year, 0 if unknown."},
        "length_ft": {"type": "integer", "description": "0 if unknown."},
        "hull_type": {"type": "string", "description": "monohull / catamaran / trimaran / power / unknown"},
        "match_notes": {"type": "string", "description": "What agrees or disagrees with the photo and sighting."},
        "links": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "url": {"type": "string"},
                           "kind": {"type": "string", "enum": ["youtube", "blog", "social", "listing", "registry", "article", "other"]}},
            "required": ["title", "url", "kind"], "additionalProperties": False}},
    },
    "required": ["found", "confidence", "summary", "builder", "model", "year", "length_ft", "hull_type", "match_notes", "links"],
    "additionalProperties": False,
}

SYSTEM = """You research a named boat seen around St. Augustine, Florida, for a boat-spotting app.

Search efficiently - you have at most 4 searches. Good queries: the exact name in quotes with "sailboat" or "catamaran" (e.g. "ego domi" catamaran), then "sailing <name>" to find the boat's own channel or blog. Owners usually state the boat's builder, model and year in a YouTube channel's About text, a blog's "the boat" page, or past sale listings - look for those details there. Report what is publicly known about the VESSEL: builder, model, year, length, hull type, and public links such as the boat's own YouTube channel, blog or sailing-log site, social pages, sale listings or registry entries.

Many boats share a name, so judge whether what you found is plausibly the same boat as observed and say so in match_notes and confidence. If nothing convincing turns up, set found to false rather than guessing.

Privacy: report on the boat and its public channels only. Do not include home addresses, phone numbers, email addresses, workplaces, or other personal details about owners or crew, even if you come across them. Use only URLs that appeared in your search results."""


def _observed(row: dict, name: str) -> str:
    ai = row.get("ai") or {}
    conf = row.get("confirm") or {}
    ais_choice = (row.get("ais") or {}).get("user_choice") or {}
    bits = [f'Boat name: "{name}".']
    if ai.get("hull") and ai["hull"] != "unknown":
        bits.append(f"Hull: {ai['hull']}.")
    if ai.get("rig") and ai["rig"] != "unknown":
        bits.append(f"Rig: {ai['rig']}.")
    if ai.get("length_ft_estimate"):
        bits.append(f"Estimated length from the photo: about {ai['length_ft_estimate']} ft.")
    if ais_choice.get("length_m"):
        bits.append(f"AIS reports length {round(ais_choice['length_m'] * 3.281)} ft, MMSI {ais_choice.get('mmsi')}.")
    if conf.get("make"):
        bits.append(f"The user identified it as {conf['make']} {conf.get('model', '')}.")
    if ai.get("description"):
        bits.append(f"Seen: {ai['description']}")
    bits.append("Seen in St. Augustine, Florida.")
    return " ".join(bits)


def _cost(usage) -> float:
    from . import budget
    pin, pout = budget.PRICES.get(identify.MODEL, budget.FALLBACK_PRICE)
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
    return (usage.input_tokens * pin + usage.output_tokens * pout) / 1e6 + searches * SEARCH_USD, searches


def run(sid: str, name: str) -> None:
    row = store.get(sid)
    if row is None:
        return
    t0 = time.time()
    messages = [{"role": "user", "content": _observed(row, name) + " What is publicly known about this boat?"}]
    cost, searches, req_ids = 0.0, 0, []
    try:
        client = identify._client()
        for _ in range(MAX_CONTINUATIONS + 1):
            resp = client.beta.messages.create(
                model=identify.MODEL, max_tokens=16000, system=SYSTEM,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=messages,
            )
            c, n = _cost(resp.usage)
            cost, searches = cost + c, searches + n
            req_ids.append(resp._request_id)
            if resp.stop_reason == "pause_turn":          # server-side search loop paused: resume as-is
                messages = messages + [{"role": "assistant", "content": resp.content}]
                continue
            break
        meta = {"model": resp.model, "cost_usd": round(cost, 4), "searches": searches,
                "seconds": round(time.time() - t0, 1), "request_ids": req_ids, "name": name}
        if resp.stop_reason == "refusal":
            store.update(sid, lookup={"status": "error", "error": "The model declined this lookup.", "meta": meta})
            return
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
        data["links"] = [l for l in data.get("links", []) if str(l.get("url", "")).startswith("https://")][:8]
        store.update(sid, lookup={"status": "done", "result": data, "meta": meta, "at": time.time()})
        print(f"BOATID-LOOKUP {sid} {name!r} found={data.get('found')} conf={data.get('confidence')} "
              f"searches={searches} cost=${cost:.3f} {meta['seconds']}s", flush=True)
    except anthropic.APIStatusError as e:
        store.update(sid, lookup={"status": "error", "error": f"API error {e.status_code}: {e.message}"[:300],
                                  "meta": {"cost_usd": round(cost, 4), "searches": searches, "name": name}})
    except anthropic.APIConnectionError:
        store.update(sid, lookup={"status": "error", "error": "Couldn't reach the API.", "meta": {"cost_usd": round(cost, 4), "name": name}})
    except (ValueError, AttributeError) as e:
        store.update(sid, lookup={"status": "error", "error": f"Unexpected answer format: {e}"[:300],
                                  "meta": {"cost_usd": round(cost, 4), "searches": searches, "name": name}})


def start(sid: str, name: str) -> None:
    store.update(sid, lookup={"status": "pending", "name": name, "at": time.time()})
    threading.Thread(target=run, args=(sid, name), daemon=True, name=f"boatid-lookup-{sid}").start()
