"""Summarise an AIS-catcher JSON capture (-o 5 -M D).

Usage: python3 tools/ais_report.py capture.jsonl [minutes]
  (minutes only needed if the capture lacks per-message times - run
   AIS-catcher with -M DT so each message carries rxuxtime)

Distances are from the Bridge of Lions (a fixed local reference point, so
the receiver's own location isn't needed).
"""
import collections
import json
import math
import sys

REF = ("Bridge of Lions", 29.8925, -81.3086)

SHIP_TYPES = {30: "Fishing", 31: "Towing", 32: "Towing (large)", 33: "Dredging", 35: "Military",
              36: "Sailing", 37: "Pleasure craft", 50: "Pilot", 51: "Search & rescue", 52: "Tug",
              53: "Port tender", 55: "Law enforcement", 58: "Medical"}
NAV_STATUS = {0: "under way (engine)", 1: "at anchor", 2: "not under command", 3: "restricted manoeuvrability",
              5: "moored", 7: "fishing", 8: "under way (sail)", 15: "undefined"}


def ship_type(t):
    if t is None:
        return "?"
    if t in SHIP_TYPES:
        return SHIP_TYPES[t]
    return {6: "Passenger", 7: "Cargo", 8: "Tanker", 4: "High-speed craft", 2: "Wing in ground"}.get(t // 10, f"type {t}")


def nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(d))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    b = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return "N NE E SE S SW W NW".split()[int(((b + 360) % 360 + 22.5) // 45) % 8]


def main(path, minutes=None):
    rows = [json.loads(l) for l in open(path) if l.startswith("{")]
    if not rows:
        print("no messages")
        return
    for i, r in enumerate(rows):
        r.setdefault("rxuxtime", i)   # no timestamps: message order still gives "latest"
    times = [r["rxuxtime"] for r in rows]
    mins = minutes or max(1e-9, (max(times) - min(times)) / 60)
    v = collections.defaultdict(lambda: {"n": 0, "pos": [], "types": collections.Counter(), "power": []})
    for r in rows:
        m = v[r["mmsi"]]
        m["n"] += 1
        m["types"][r["type"]] += 1
        if r.get("signalpower") is not None:
            m["power"].append(r["signalpower"])
        if r.get("lat") is not None and abs(r["lat"]) <= 90 and abs(r.get("lon", 999)) <= 180 and not (r["lat"] == 0 and r["lon"] == 0):
            m["pos"].append((r["rxuxtime"], r["lat"], r["lon"], r.get("speed"), r.get("course")))
        for k in ("shipname", "name", "callsign", "shiptype", "destination", "to_bow", "to_stern", "draught", "status"):
            if r.get(k) not in (None, "", 0) or (k == "status" and r.get(k) == 0):
                m[k] = r[k]
    print(f"{path}\n{len(rows)} messages over {mins:.1f} min ({len(rows)/mins:.0f}/min) from {len(v)} transmitters")
    print("message types:", dict(sorted(collections.Counter(r["type"] for r in rows).items())))
    ppm = [r["ppm"] for r in rows if r.get("ppm") is not None]
    if ppm:
        print(f"dongle frequency error ~{sorted(ppm)[len(ppm)//2]:+.1f} ppm (median)")

    def kind(mm, m):
        if 1 in m["types"] or 2 in m["types"] or 3 in m["types"] or 5 in m["types"]:
            return "A"
        if 18 in m["types"] or 19 in m["types"] or 24 in m["types"]:
            return "B"
        if 4 in m["types"]:
            return "base"
        if 21 in m["types"]:
            return "AtoN"
        if str(mm).startswith("97"):
            return "SART/MOB"
        return "other"

    groups = collections.defaultdict(list)
    for mm, m in v.items():
        groups[kind(mm, m)].append((mm, m))
    print("by kind:", {k: len(x) for k, x in groups.items()})

    dists = []
    print(f"\n{'kind':5} {'MMSI':>10}  {'name':22} {'type':16} {'nm':>5} {'dir':>3} {'kn':>5} {'msgs':>4} {'dBFS':>6}  status")
    for k in ("A", "B", "base", "AtoN", "SART/MOB", "other"):
        for mm, m in sorted(groups.get(k, []), key=lambda x: -x[1]["n"]):
            name = (m.get("shipname") or m.get("name") or "").strip()[:22]
            d = brg = spd = ""
            if m["pos"]:
                _, la, lo, s, _ = m["pos"][-1]
                dd = nm(REF[1], REF[2], la, lo)
                dists.append((dd, k, name or str(mm)))
                d, brg = f"{dd:.1f}", bearing(REF[1], REF[2], la, lo)
                spd = "" if s is None else f"{s:.1f}"
            pw = f"{sorted(m['power'])[len(m['power'])//2]:.0f}" if m["power"] else ""
            st = NAV_STATUS.get(m.get("status"), "") if k == "A" else ""
            print(f"{k:5} {mm:>10}  {name:22} {ship_type(m.get('shiptype'))[:16]:16} {d:>5} {brg:>3} {spd:>5} {m['n']:>4} {pw:>6}  {st}")
    if dists:
        dists.sort()
        print(f"\nnearest {dists[0][0]:.1f} nm ({dists[0][2]}), farthest {dists[-1][0]:.1f} nm ({dists[-1][2]}, class {dists[-1][1]}) from the {REF[0]}")
        named = sum(1 for _, m in v.items() if (m.get("shipname") or m.get("name")))
        print(f"names received for {named} of {len(v)} transmitters in this window")


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else None)
