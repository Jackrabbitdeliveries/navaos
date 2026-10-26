"""
Reference photos for the comparison screen (Boat ID piece 2).

Two legitimate sources only (no scraping - docs/boat-id-plan.md section 5):
  1. "own"     - the user's own confirmed sightings of that make/model
  2. "commons" - freely licensed photos found through the official Wikimedia
                 Commons API, shown hot-linked with photographer + license
                 credit; never downloaded into the library.

Model names come from the AI or are typed by people, so matching is fuzzy:
same make (first word) and a model string that's close or shares the model
number. Commons results are cached per query for 30 days.
"""
from __future__ import annotations

import difflib
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

from . import store

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "NavaOS-BoatID/0.1 (personal hobby project; https://github.com/Jackrabbitdeliveries/navaos)"
CACHE_FILE = store.DATA_DIR / "commons_cache.json"
CACHE_TTL_S = 30 * 86400
OK_LICENSES = ("cc0", "public domain", "pd", "cc by")   # "cc by" also covers CC BY-SA
# A reference photo must be of a boat: its title or a Commons category has to say
# so ("Leopard 43" otherwise finds Leopard tanks, "Bristol 42" street addresses).
BOAT_WORDS = ("sailboat", "sailing", "yacht", "catamaran", "trimaran", "sloop", "ketch", "yawl", "schooner",
              "cutter (boat)", "boat", "sail ", "marina", "regatta", "monohull")
CACHE_VERSION = "v2|"   # bump when the stored fields change

_cache_lock = threading.Lock()


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def make_word(make: str) -> str:
    w = norm(make).split()
    return w[0] if w else ""


def model_number(model: str) -> str:
    """The first token with a digit ('30' from '30 / 320'), else the first word."""
    toks = norm(model).split()
    for t in toks:
        if any(c.isdigit() for c in t):
            return t
    return toks[0] if toks else ""


def same_model(make_a: str, model_a: str, make_b: str, model_b: str) -> bool:
    if not make_word(make_a) or make_word(make_a) != make_word(make_b):
        return False
    na, nb = norm(model_a), norm(model_b)
    if not na or not nb:
        return False
    if na == nb or model_number(model_a) == model_number(model_b):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


# ---- 1. own confirmed photos -----------------------------------------------------

def own_references(make: str, model: str, exclude: Optional[str] = None, limit: int = 6) -> list[dict]:
    out = []
    for r in store.recent(1000):
        c = r.get("confirm") or {}
        if r["id"] == exclude or not c.get("make") or c.get("model_unknown"):
            continue
        if same_model(make, model, c["make"], c.get("model", "")):
            out.append({"source": "own", "url": f"/boats/photo/{r['id']}.jpg", "sighting": r["id"],
                        "label": "Your photo, " + time.strftime("%b %-d", time.localtime(r["created_at"]))})
            if len(out) >= limit:
                break
    return out


def known_models() -> list[str]:
    """Make + model strings already confirmed, for autocomplete."""
    seen = {}
    for r in store.recent(1000):
        c = r.get("confirm") or {}
        if c.get("make") and not c.get("model_unknown"):
            name = f"{c['make']} {c.get('model', '')}".strip()
            seen.setdefault(norm(name), name)
    return sorted(seen.values())


# ---- 2. Wikimedia Commons ----------------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(c: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    tmp.replace(CACHE_FILE)


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _search_commons(query: str, limit: int = 20) -> list[dict]:
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
              "gsrsearch": query, "gsrlimit": str(limit),
              "iiprop": "url|extmetadata|mime", "iiurlwidth": "800",
              "prop": "imageinfo|categories", "cllimit": "max", "clshow": "!hidden"}
    req = urllib.request.Request(COMMONS_API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    out = []
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = ii.get("extmetadata", {})
        lic = _strip_html(meta.get("LicenseShortName", {}).get("value", ""))
        if not lic.lower().startswith(OK_LICENSES):
            continue
        thumb = ii.get("thumburl") or ii.get("url")
        if not thumb or not thumb.startswith(("https://upload.wikimedia.org/", "https://thumb.wikimedia.org/")):
            continue
        cats = " ".join(c.get("title", "") for c in p.get("categories", [])).lower()
        title = p.get("title", "").lower()
        if not any(w in title or w in cats for w in BOAT_WORDS):
            continue                     # not a boat photo
        out.append({"source": "commons", "url": thumb, "page": ii.get("descriptionurl"),
                    "title": p.get("title", "").removeprefix("File:"),
                    "artist": _strip_html(meta.get("Artist", {}).get("value", ""))[:80] or "unknown",
                    "license": lic})
    return out


def _cached_search(query: str) -> Optional[list[dict]]:
    query_key = CACHE_VERSION + query
    with _cache_lock:
        hit = _load_cache().get(query_key)
    if hit and time.time() - hit["at"] < CACHE_TTL_S:
        return hit["results"]
    try:
        results = _search_commons(query)
    except Exception as e:   # network/API trouble: just no Commons photos this time
        print(f"BOATID-COMMONS-FAILED {query!r}: {e!r}", flush=True)
        return None
    with _cache_lock:
        cache = _load_cache()
        cache[query_key] = {"at": time.time(), "results": results}
        _save_cache(cache)
    return results


def model_alternatives(model: str) -> list[str]:
    """'Belize 43 / Helia 44' -> ['belize 43', 'helia 44']; '440 / 450' -> ['440', '450'];
    'Oceanis 350-390' -> ['oceanis 350', 'oceanis 390']; drops chatty extras ('e.g.', 'era sloop')."""
    out = []
    for part in re.split(r"[/,]| or ", model or ""):
        toks = norm(part).split()
        words = [t for t in toks if not any(c.isdigit() for c in t) and t not in ("e", "g", "eg", "era", "sloop", "similar")]
        nums = [n for t in toks if any(c.isdigit() for c in t) for n in t.split("-") if n]
        if nums:
            for n in nums:
                out.append(" ".join(words[:1] + [n]))
        elif words:
            out.append(" ".join(words[:2]))
    return list(dict.fromkeys(a for a in out if a))[:3]


def commons_references(make: str, model: str, limit: int = 6) -> list[dict]:
    mw = make_word(make)
    if not mw:
        return []
    keep: list[dict] = []

    def take(results, must: list[str]):
        for r in results or []:
            t = norm(r["title"]).split()
            if mw in t and all(m in t for m in must) and all(k["url"] != r["url"] for k in keep):
                keep.append(r)

    # One search per model alternative ("make + model"), falling back to the first
    # make word; a file must name the make and the model number (or model word).
    for alt in model_alternatives(model) or [model_number(model)]:
        key = [t for t in alt.split() if any(c.isdigit() for c in t)] or alt.split()[:1]
        for query in dict.fromkeys([f"{norm(make)} {alt}".strip(), f"{mw} {alt}".strip()]):
            take(_cached_search(query), key)
            if len(keep) >= 2:
                break
        if len(keep) >= limit:
            break
    return keep[:limit]


def make_references(make: str, limit: int = 4) -> list[dict]:
    """Fallback when a model has no photos: other boats by the same builder,
    labelled as such - a builder's boats share a family look."""
    mw = make_word(make)
    if not mw:
        return []
    out = []
    for query in dict.fromkeys([f"{norm(make)} sailboat", f"{norm(make)} catamaran", norm(make)]):
        for r in _cached_search(query) or []:
            if mw in norm(r["title"]).split() and all(o["url"] != r["url"] for o in out):
                out.append({**r, "source": "commons-make"})
        if len(out) >= limit:
            break
    return out[:limit]


def references(make: str, model: str, exclude: Optional[str] = None) -> list[dict]:
    exact = own_references(make, model, exclude) + commons_references(make, model)
    return exact or make_references(make)
