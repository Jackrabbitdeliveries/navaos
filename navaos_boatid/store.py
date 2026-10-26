"""
SQLite storage for Boat ID sightings, plus the photo files.

Layout (outside the repo - photos must never reach the public GitHub repo):
  ~/navaos-data/boatid/boatid.db
  ~/navaos-data/boatid/photos/YYYY-MM/<id>.jpg
  ~/navaos-data/kit.json            {"kit_id": ..., "name": ...}  (optional)

Rows are multi-kit ready (kit_id) and source-aware (phone / upload, later
fixed-camera). Structured parts (guess, sensors, AI result, confirmation,
AIS match) are JSON columns so the schema can grow without migrations.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("NAVAOS_BOATID_DIR", Path.home() / "navaos-data" / "boatid"))
DB_FILE = DATA_DIR / "boatid.db"
PHOTO_DIR = DATA_DIR / "photos"
KIT_FILE = Path.home() / "navaos-data" / "kit.json"
ID_ALPHABET_LEN = 12

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    kit_id      TEXT NOT NULL,
    client      TEXT,
    source      TEXT NOT NULL,          -- phone | upload | feed (a live camera, see feed_id)
    taken_at    REAL,                   -- epoch s, from the phone at the shutter
    photo       TEXT NOT NULL,          -- path relative to DATA_DIR
    img_w       INTEGER, img_h INTEGER,
    tap_x       REAL, tap_y REAL,       -- 0..1 of the image, where the user tapped the boat
    sensors     TEXT,                   -- JSON: lat, lon, accuracy, heading, raw orientation...
    guess       TEXT,                   -- JSON: hull, rig, size, model (the user's)
    ai_status   TEXT NOT NULL,          -- pending | done | error
    ai          TEXT,                   -- JSON: the model's structured answer
    ai_meta     TEXT,                   -- JSON: model, tokens, request id, seconds
    ai_error    TEXT,
    confirm     TEXT,                   -- JSON (piece 2)
    ais         TEXT,                   -- JSON (piece 3)
    feed_id     TEXT,                   -- navaos_boatid.feeds id when source = feed
    lookup      TEXT                    -- JSON: background web lookup (lookup.py)
);
CREATE INDEX IF NOT EXISTS sightings_created ON sightings(created_at);
"""

JSON_COLS = ("sensors", "guess", "ai", "ai_meta", "confirm", "ais", "lookup")


def kit_id() -> str:
    try:
        return json.loads(KIT_FILE.read_text()).get("kit_id") or "nava-pi"
    except (OSError, ValueError):
        return "nava-pi"


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_FILE, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(sightings)")}
    if "feed_id" not in cols:   # databases created before 2026-10-26
        c.execute("ALTER TABLE sightings ADD COLUMN feed_id TEXT")
    if "lookup" not in cols:
        c.execute("ALTER TABLE sightings ADD COLUMN lookup TEXT")
    return c


def new_id() -> str:
    return secrets.token_urlsafe(9)   # 12 url-safe chars, unguessable


def valid_id(sid: str) -> bool:
    return isinstance(sid, str) and len(sid) == ID_ALPHABET_LEN and all(ch.isalnum() or ch in "-_" for ch in sid)


def save_photo(sid: str, jpeg: bytes) -> str:
    rel = Path("photos") / time.strftime("%Y-%m") / f"{sid}.jpg"
    path = DATA_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(jpeg)
    tmp.replace(path)
    return str(rel)


def photo_path(row: dict) -> Optional[Path]:
    p = (DATA_DIR / row["photo"]).resolve()
    return p if p.is_file() and DATA_DIR.resolve() in p.parents else None


def insert(row: dict) -> None:
    row = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in row.items()}
    cols = ", ".join(row)
    with _lock, _conn() as c:
        c.execute(f"INSERT INTO sightings ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def update(sid: str, **fields) -> None:
    fields = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in fields.items()}
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE sightings SET {sets} WHERE id = ?", [*fields.values(), sid])


def _decode(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def get(sid: str) -> Optional[dict]:
    if not valid_id(sid):
        return None
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM sightings WHERE id = ?", (sid,)).fetchone()
    return _decode(r) if r else None


def recent(limit: int = 50) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM sightings ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_decode(r) for r in rows]
