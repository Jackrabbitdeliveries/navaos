"""Match vessel names spoken in transcripts against the AIS vessel database.

Usage: NAVAOS_DEFAULT_SCAN=0 NAVAOS_AIS=0 python3 tools/stt_names.py transcript.txt [...]

Uses the same matcher as the live transcriber (navaos_audio.transcriber).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from navaos_audio.transcriber import match_vessels  # noqa: E402

DB = Path(os.environ.get("NAVAOS_AIS_DIR", Path.home() / "navaos-data" / "ais")) / "vessels.json"

if __name__ == "__main__":
    try:
        names = {v["name"]: v["mmsi"] for v in json.loads(DB.read_text()).values() if v.get("name")}
    except (OSError, ValueError):
        names = {}
    print(f"{len(names)} AIS names known")
    for p in sys.argv[1:]:
        hits = match_vessels(Path(p).read_text(), names)
        if hits:
            print(Path(p).name, "->", ", ".join(h["name"] for h in hits))
