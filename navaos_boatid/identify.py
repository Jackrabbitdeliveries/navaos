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
EFFORT = "high"          # was "medium" until 2026-11-08
PROMPT_VERSION = 4       # 2 = rig check, design details, fleet memory; 3 = one real make/model per candidate;
                         # 4 = hull ports vs cabin ports as separate details (2026-11-08)
# Tests and spare dev instances MUST set NAVAOS_BOATID_AI=0: the key file is
# global, so otherwise every test sighting is a real, billed API call.
AI_ENABLED = os.environ.get("NAVAOS_BOATID_AI", "1") != "0"
KEY_FILE = Path.home() / "navaos-data" / "anthropic_api_key"

HULLS = ["monohull", "catamaran", "trimaran", "unknown"]
RIGS = ["sloop", "cutter", "ketch_or_yawl", "schooner", "cat_rig", "unknown"]
SIZES = ["under_30", "30_39", "40_49", "50_plus", "unknown"]

DETAIL_FIELDS = {
    "hull_ports": "Ports/windows in the HULL itself (the topsides below the deck edge): none, or how many per "
                  "side, shape (round, oval, rectangular, long strip), placement (forward, aft, in a band). "
                  "Many models are told apart by this alone - say 'none' only if that part of the hull is "
                  "clearly visible.",
    "cabin_ports": "Ports/windows in the cabin trunk or deckhouse ABOVE the deck: count per side, shape (round, "
                   "oval, rectangular, slanted, wraparound), frames (bronze, black, stainless).",
    "cabin_trunk": "Coachroof/deckhouse profile: low, stepped, raised saloon, pilothouse; front windshield shape.",
    "sheer_and_freeboard": "Sheer line (straight, springy, reverse), freeboard, any hull stripe or cove line.",
    "bow": "Bow shape and gear: plumb, raked, clipper, spoon; bowsprit, anchor platform, pulpit.",
    "stern": "Stern/transom: reverse, vertical, raked, canoe/double-ender, sugar-scoop steps, swim platform, davits.",
    "cockpit": "Cockpit position (aft, center), dodger/hardtop shape (shape only - its fabric color is not evidence).",
    "rig_details": "Spreaders (number, swept or straight), headstay position, furlers, boom height, mizzen if any.",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "is_sailboat": {"type": "boolean"},
        "hull": {"type": "string", "enum": HULLS},
        "masts_on_this_hull": {"type": "integer",
                               "description": "Masts you can see standing on THIS boat's own deck; 0 if none can "
                                              "be seen. Masts of other boats behind or in front do not count."},
        "rig_evidence": {"type": "string",
                         "description": "One sentence: what you see that decides the rig, incl. how you know each "
                                        "mast stands on this hull."},
        "rig": {"type": "string", "enum": RIGS},
        "size_class": {"type": "string", "enum": SIZES},
        "length_ft_estimate": {"type": "integer",
                               "description": "Best estimate of overall length in feet; 0 if it can't be judged."},
        "hull_color": {"type": "string"},
        "visible_name": {"type": "string",
                         "description": "Boat name painted on the hull if clearly legible, else empty string. Never guess."},
        "id_details": {
            "type": "object",
            "description": "The design details that tell one model from another. 'not visible' when you can't see it.",
            "properties": {k: {"type": "string", "description": v} for k, v in DETAIL_FIELDS.items()},
            "required": list(DETAIL_FIELDS),
            "additionalProperties": False,
        },
        "features": {"type": "array", "items": {"type": "string"},
                     "description": "Up to 6 short, distinguishing design features (e.g. 'three oval ports per side', "
                                    "'reverse transom with steps', 'raised saloon'). Not canvas colors or moorings."},
        "description": {"type": "string", "description": "One or two plain sentences describing the boat."},
        "known_boat": {
            "type": "object",
            "description": "Whether this is one of the KNOWN BOATS the user has already identified.",
            "properties": {
                "ref": {"type": "string", "description": "The known boat's label (e.g. 'K3'), or empty string if none."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "why": {"type": "string", "description": "Which specific details agree or disagree."},
            },
            "required": ["ref", "confidence", "why"],
            "additionalProperties": False,
        },
        "candidates": {
            "type": "array",
            "description": "Up to 5 make/model guesses, most likely first. Empty if no reasonable guess.",
            "items": {
                "type": "object",
                "properties": {
                    "make": {"type": "string",
                             "description": "ONE real builder name as owners write it, e.g. 'Catalina', 'Ta Chiao', "
                                            "'Fountaine Pajot'. No lists, 'type', 'style' or '(various)'."},
                    "model": {"type": "string",
                              "description": "ONE real model name/number, e.g. '34', 'CT 41', 'Belize 43'. Put "
                                             "close alternatives in 'why', or make them separate candidates."},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "why": {"type": "string", "description": "The id_details that fit this model (and any that don't)."},
                },
                "required": ["make", "model", "confidence", "why"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string",
                  "description": "Anything the user should know, e.g. several boats in view, too far away, partly hidden."},
    },
    "required": ["is_sailboat", "hull", "masts_on_this_hull", "rig_evidence", "rig", "size_class",
                 "length_ft_estimate", "hull_color", "visible_name", "id_details", "features", "description",
                 "known_boat", "candidates", "notes"],
    "additionalProperties": False,
}

SYSTEM = """You identify sailboats in photos taken from the shore, the water or two live harbour cameras around St. Augustine, Florida, for a "guess, then reveal" game. Many photos show a crowded mooring field: boats sit close together and overlap.

Work in this order.

1. Find the target boat (the one the user framed or tapped) and keep to it. Trace its hull outline first.

2. Rig. Count only masts that stand on the target's own deck. In a mooring field the mast of a boat behind or in front very often lines up with the target and looks like a second mast - so do cranes, flagpoles, light poles and antennas. A second mast counts only if you can see it rise from this hull's deck (inside its outline, between its bow and stern) and it has its own rigging or boom/sail cover; if you can't see that, it's one mast. Then: sloop (one mast, one headstay), cutter (one mast, two headstays), ketch or yawl (two masts, the aft one shorter), schooner (two or more masts, the forward one shorter or equal), cat rig (one mast well forward, no headsail). A ketch is far rarer here than a sloop - say ketch only when both masts are clearly on this hull; if unsure, prefer "unknown" over a guess.

3. Design details. Before naming any model, look closely at what actually differs between models and fill in id_details: ports and windows in the hull itself versus in the cabin above deck (a strong tell - keep the two apart), their shape and count, cabin trunk and deckhouse profile, sheer, bow, stern/transom, cockpit position, spreaders and rig details. Canvas colors, sail covers, fenders, dinghies and moorings change from owner to owner and say nothing about the model - never use them as evidence for a make/model (they can help recognise a KNOWN BOAT, below).

4. Known boats. If the message lists KNOWN BOATS the user identified before (mostly boats that live on moorings in view of the cameras), check whether the target is one of them: same hull type and rig, same design details, same hull color, plus matching personal touches (canvas, sail-cover color, dinghy, solar arch). If it is, set known_boat and make that boat's make/model your first candidate, with confidence from how sure the match is. Don't force a match: a different boat of a popular model is common.

5. Candidates: models whose design details match what you saw, each "why" naming the details that fit. Each candidate is one real builder and one real model (the app looks up photos of it by that name) - not a family like "Taiwanese-built (Formosa/Ta Chiao type)"; give the likeliest specific model and mention look-alikes in "why" Don't just list the most common boats of that type and size - if the details don't narrow it down, give fewer candidates with low confidence.

Be honest about uncertainty: exact make and model from one photo is often impossible. Never invent a boat name: report visible_name only if it is clearly legible.

If the photo shows no sailboat, set is_sailboat to false and describe what you see."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=KEY_FILE.read_text().strip(), timeout=120.0, max_retries=2)


def _known_line(k: dict) -> str:
    bits = [f"{k['ref']}: {k['make']} {k['model']}".strip()]
    if k["name"]:
        bits.append(f'named "{k["name"]}"')
    bits.append(", ".join(x for x in (k["hull"], k["rig"].replace("_", " "), k["hull_color"]) if x and x != "unknown"))
    bits.append(f"seen {k['seen']} on {k['where']}")
    line = " - ".join(b for b in bits if b)
    if k["description"]:
        line += f". Earlier description: {k['description']}"
    return line


def _same_boat_type(c: dict, hit: dict) -> bool:
    """Is candidate c just the known boat's model again? Lenient on purpose: the model
    writes 'Royal Cape 45 (Majestic 530 family)' when the confirm said make 'Royal Cape 45'."""
    from . import library
    a = library.norm(f"{c.get('make', '')} {c.get('model', '')}").split()
    b = library.norm(f"{hit['make']} {hit['model']}").split()
    if not a or not b or a[0] != b[0]:
        return False
    nums = [t for t in b if any(ch.isdigit() for ch in t)]
    return all(n in a for n in nums) if nums else " ".join(b) in " ".join(a)


def resolve_known(ai: dict, known: list[dict]) -> dict:
    """Turn the model's known_boat label into the sighting it means, and make sure
    a medium/high match is the first candidate (the page's first slide)."""
    kb = ai.get("known_boat") or {}
    hit = next((k for k in known if kb.get("ref") and k["ref"] == kb["ref"].strip().upper()), None)
    if hit is None:
        ai["known_boat"] = None
        return ai
    ai["known_boat"] = {"ref": hit["ref"], "sighting": hit["sighting"], "make": hit["make"], "model": hit["model"],
                        "name": hit["name"], "seen": hit["seen"], "confidence": kb.get("confidence", "low"),
                        "why": kb.get("why", "")}
    if kb.get("confidence") in ("medium", "high"):
        from . import library
        cands = [c for c in ai.get("candidates") or [] if not _same_boat_type(c, hit)]
        first = {"make": hit["make"], "model": hit["model"], "confidence": kb["confidence"],
                 "why": "Seen before" + (f" as {hit['name']}" if hit["name"] else "") + f" ({hit['seen']}): " + kb.get("why", "")}
        ai["candidates"] = [first] + cands[:4]
    return ai


def _prompt(row: dict, framed: bool = False, known: bool = False) -> str:
    parts = []
    if framed:
        c = row["crop"]
        parts.append(f"The user zoomed in and framed the boat they mean: the first image is that close-up, cut from "
                     f"the second (whole) image at {c['x'] * 100:.0f}-{(c['x'] + c['w']) * 100:.0f}% from the left and "
                     f"{c['y'] * 100:.0f}-{(c['y'] + c['h']) * 100:.0f}% from the top. Identify the boat in the middle of "
                     "the close-up; use the whole image only for context (scale, other boats). If the close-up holds "
                     "several boats, say so in notes.")
    elif row.get("tap_x") is not None and row.get("tap_y") is not None:
        parts.append(f"The user tapped the boat they mean at {row['tap_x'] * 100:.0f}% from the left and "
                     f"{row['tap_y'] * 100:.0f}% from the top of the image. Identify that boat; if several boats "
                     "are near that point, say so in notes.")
    else:
        parts.append("Identify the main sailboat in the photo; if there are several, say so in notes.")
    if known:
        parts.append("Check the target against the KNOWN BOATS above before giving candidates.")
    parts.append("Do not use the user's own guess - it is hidden from you so the two can be compared.")
    return " ".join(parts)


def build_content(row: dict, path: Path) -> tuple[list, list]:
    """The user message for one sighting: [close-up], whole photo, [known boats], instructions."""
    from . import fleet
    img = base64.standard_b64encode(path.read_bytes()).decode()
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}]
    crop = store.crop_path(row)
    if crop is not None:   # close-up first, then the whole picture for context
        cimg = base64.standard_b64encode(crop.read_bytes()).decode()
        content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": cimg}})
    known = fleet.known_boats(exclude=row["id"], feed_id=row.get("feed_id"))
    if known:
        content.append({"type": "text", "text": "KNOWN BOATS the user identified before (one close-up each):"})
        for k in known:
            content.append({"type": "text", "text": _known_line(k)})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(k["path"].read_bytes()).decode()}})
    content.append({"type": "text", "text": _prompt(row, framed=crop is not None, known=bool(known))})
    return content, known


def ask(content: list):
    return _client().beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )


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
        content, known = build_content(row, path)
        resp = ask(content)
        meta = {"model": resp.model, "request_id": resp._request_id, "seconds": round(time.time() - t0, 1),
                "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
                "effort": EFFORT, "prompt": PROMPT_VERSION, "known_boats": len(known)}
        if resp.stop_reason == "refusal":
            store.update(sid, ai_status="error", ai_meta=meta,
                         ai_error="The model declined to answer this photo.")
            return
        text = next((b.text for b in resp.content if b.type == "text"), "")
        store.update(sid, ai_status="done", ai=resolve_known(json.loads(text), known), ai_meta=meta, ai_error=None)
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
    from . import budget
    ok, spent, cap = budget.allowed()
    if not ok:
        store.update(sid, ai_status="error",
                     ai_error=f"Daily AI budget reached (${spent:.2f} of ${cap:.2f}) - resets at midnight.")
        print(f"BOATID-BUDGET {sid} refused spent={spent:.2f} cap={cap:.2f}", flush=True)
        return
    threading.Thread(target=run, args=(sid,), daemon=True, name=f"boatid-{sid}").start()
