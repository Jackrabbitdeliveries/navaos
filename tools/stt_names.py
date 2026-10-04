"""Match vessel names spoken in transcripts against the AIS vessel database.

Usage: python3 tools/stt_names.py transcript.txt [...]

Fuzzy: a vessel name (1-3 words) matches if a window of the same number of
transcript words is within MIN_RATIO similarity (difflib), so "sweet
emotion" ~ SWEET EMOCEAN. Very short names (<4 letters) must match exactly.
"""
import difflib
import json
import os
import re
import sys
from pathlib import Path

DB = Path(os.environ.get("NAVAOS_AIS_DIR", Path.home() / "navaos-data" / "ais")) / "vessels.json"
MIN_RATIO = 0.82
# Words that are also common radio talk - a vessel with one of these as its
# whole name would match far too often.
TOO_COMMON = {"OFF AIR", "SOMEDAY", "MON CHERI"}


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def load_names() -> dict[str, int]:
    try:
        vs = json.loads(DB.read_text())
    except (OSError, ValueError):
        return {}
    return {v["name"].strip(): v["mmsi"] for v in vs.values() if v.get("name", "").strip()}


def match(text: str, names: dict[str, int]) -> list[tuple[str, int, float, str]]:
    words = norm(text).split()
    hits = []
    for name, mmsi in names.items():
        if name.upper() in TOO_COMMON:
            continue
        target = norm(name)
        n = len(target.split())
        best, best_phrase = 0.0, ""
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n])
            if len(target.replace(" ", "")) < 4:
                r = 1.0 if phrase == target else 0.0
            else:
                r = difflib.SequenceMatcher(None, phrase, target).ratio()
            if r > best:
                best, best_phrase = r, phrase
        if best >= MIN_RATIO:
            hits.append((name, mmsi, round(best, 2), best_phrase))
    return sorted(hits, key=lambda h: -h[2])


if __name__ == "__main__":
    names = load_names()
    print(f"{len(names)} AIS names known")
    for p in sys.argv[1:]:
        hits = match(Path(p).read_text(), names)
        if hits:
            print(Path(p).name, "->", "; ".join(f"{n} (heard '{ph}', {r})" for n, _, r, ph in hits))
