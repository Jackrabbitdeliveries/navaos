"""Usage: rtl_power -f 156.3M:156.9M:6250 -g 49.6 -i 1 -e 12m marine.csv  (stop the scan first)
       python3 tools/rf_baseline.py marine.csv

Per-channel RF power baseline from rtl_power CSV output.

For each second: channel power = linear sum of bins within +/-HALF_BW of the
channel center, expressed in dB relative to that second's band-median bin
(scaled to the same number of bins), i.e. "dB above noise floor".
"""
import sys
import numpy as np

HALF_BW = 6000  # Hz; marine FM voice occupies ~+/-5 kHz
ACTIVE_DB = 10.0  # dB above floor counted as "carrier present"

CHANNELS = {
    "68": 156.425e6, "09": 156.450e6, "71": 156.575e6, "72": 156.625e6,
    "13": 156.650e6, "16": 156.800e6, "wx": 162.425e6,
}


def load(path):
    rows = {}
    for line in open(path):
        p = [x.strip() for x in line.split(",")]
        ts = p[0] + " " + p[1]
        lo, hi, step = float(p[2]), float(p[3]), float(p[4])
        vals = np.array([float(v) for v in p[6:]])
        freqs = lo + step * (np.arange(len(vals)) + 0.5)
        rows.setdefault(ts, ([], []))
        rows[ts][0].append(freqs)
        rows[ts][1].append(vals)
    out = []
    for ts, (fs, vs) in rows.items():
        out.append((ts, np.concatenate(fs), np.concatenate(vs)))
    return out


def analyze(path):
    data = load(path)
    freqs0 = data[0][1]
    chans = {k: f for k, f in CHANNELS.items() if freqs0.min() < f < freqs0.max()}
    res = {k: [] for k in chans}
    floors = []
    for ts, freqs, vals in data:
        floor = np.median(vals)
        floors.append(floor)
        for k, f in chans.items():
            m = np.abs(freqs - f) <= HALF_BW
            n = m.sum()
            ch_db = 10 * np.log10(np.sum(10 ** (vals[m] / 10)))
            floor_db = floor + 10 * np.log10(n)
            res[k].append((ts, ch_db - floor_db))
    print(f"{path}: {len(data)} sweeps, {data[0][0]} -> {data[-1][0]}")
    print(f"band median floor: {np.median(floors):.1f} dB (rtl_power relative units)")
    print(f"{'ch':>3} {'p10':>6} {'p50':>6} {'p90':>6} {'max':>6} {'%>+10dB':>8}  events(>=2s above +{ACTIVE_DB:.0f}dB)")
    for k, series in res.items():
        x = np.array([v for _, v in series])
        above = x >= ACTIVE_DB
        events, i = [], 0
        while i < len(x):
            if above[i]:
                j = i
                while j < len(x) and above[j]:
                    j += 1
                if j - i >= 2:
                    events.append(f"{series[i][0][-8:]} {j-i}s peak+{x[i:j].max():.0f}")
                i = j
            else:
                i += 1
        print(f"{k:>3} {np.percentile(x,10):6.1f} {np.percentile(x,50):6.1f} "
              f"{np.percentile(x,90):6.1f} {x.max():6.1f} {100*above.mean():7.1f}%  "
              f"{len(events)}: " + "; ".join(events[:8]) + (" ..." if len(events) > 8 else ""))


for p in sys.argv[1:]:
    analyze(p)
    print()
