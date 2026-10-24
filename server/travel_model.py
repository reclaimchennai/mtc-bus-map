#!/usr/bin/env python3
"""How long buses take, learned from our own GPS archive: running speed between
stops and dwell at stops, by hour of day.

    travel_model.py --stops route_stops.json --stop-dist stop_dist.json --archive DIR
                    --out model.json [--days 7] [--until YYYY-MM-DD]

Every archived fix is placed on its route (projection onto the stop-to-stop
chords, searched near where the bus last was, so a loop or a parallel return
leg cannot pull it backwards). Then, bus by bus, consecutive fixes are read as:

  standing  moved < STILL_M: the bus is stopped. A run of standing fixes near a
            stop (within NEAR_M, not a terminus) is one dwell. Its length is
            what was seen standing plus a quarter of each unobserved gap either
            side (it arrived and left at some unknown moment within them). Fixes
            are ~20 s apart, so dwells shorter than that are mostly unseen: they
            show up as a lower chance of stopping, not as short dwells.
  running   moved >= STILL_M: distance along the route / time is a running
            speed, credited to every stop-to-stop segment the pair spans.

model.json:
  {"t", "days", "interval_s",
   "seg":   {"stopA|stopB": [n, v all-day m/s, [v by hour or -1]]}   effective speed, stops included
                                                                    (metres / seconds, pooled: time per metre)
   "dwell": {"stop": [passes, p_stop, dwell s when stopping all-day, [dwell by hour or -1]]},
   "city":  {"v": [running m/s by hour], "dwell": s, "p_stop": p}}
Medians throughout, from at most RESERVOIR samples per key and hour.
"""
import argparse
import collections
import datetime as dt
import glob
import gzip
import json
import math
import os
import random
import statistics
import sys
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STILL_M, NEAR_M, MAX_DWELL, RESERVOIR, MIN_N, STAND_MAX = 12, 40, 600, 160, 5, 240
# Longest gap between two fixes of one bus still read as one movement. Delhi (OTD,
# a fix every ~20 s) uses 180 s; Chalo is polled route by route, so a bus is seen
# every ~13 min (p10 2 min, p90 30 min) unless a rider has its route open (30 s).
MAX_GAP = 180
MAX_SPAN = 6         # most stop-to-stop segments one pair of fixes may be spread over
K = 111320.0


class Routes:
    """Stop chords and along-route distances per route code, with a projection
    that prefers the neighbourhood of a bus's previous position."""
    def __init__(self, stops_path, dist_path):
        rs = json.load(open(stops_path))
        sd = json.load(open(dist_path))
        self.ll, self.ds, self.ids = {}, {}, {}
        for c, r in rs.items():
            s = r['d'][0]['s']
            if c in sd and len(sd[c]) == len(s) and len(s) >= 2:
                self.ll[c] = [(p[2], p[3]) for p in s]
                self.ds[c] = sd[c]
                self.ids[c] = [p[0] for p in s]

    def project(self, code, lat, lon, near=None):
        """(metres along, segment index i (stop i -> i+1), offset m) or None."""
        ll, ds = self.ll.get(code), self.ds.get(code)
        if not ll:
            return None
        cx = math.cos(math.radians(lat)) * K
        def scan(lo, hi):
            best = None
            for i in range(max(0, lo), min(len(ll) - 1, hi)):
                (ay, ax), (by, bx) = ll[i], ll[i + 1]
                vx, vy = (bx - ax) * cx, (by - ay) * K
                px, py = (lon - ax) * cx, (lat - ay) * K
                L2 = vx * vx + vy * vy
                t = max(0.0, min(1.0, (px * vx + py * vy) / L2)) if L2 else 0.0
                off = math.hypot(px - t * vx, py - t * vy)
                if best is None or off < best[2]:
                    best = (ds[i] + t * (ds[i + 1] - ds[i]), i, off)
            return best
        if near is not None:
            b = scan(near - 2, near + 7)
            if b and b[2] <= 150:
                return b
        return scan(0, len(ll) - 1)


def day_files(archive, kind, days, until=None):
    end = dt.date.fromisoformat(until) if until else dt.datetime.now(IST).date()
    for n in range(days - 1, -1, -1):
        d = (end - dt.timedelta(days=n)).isoformat()
        for p in (f'{archive}/{kind}-{d}.jsonl', f'{archive}/{kind}-{d}.jsonl.gz'):
            if os.path.exists(p):
                yield d, p


def lines(p):
    op = gzip.open if p.endswith('.gz') else open
    with op(p, 'rt') as fh:
        for ln in fh:
            try:
                yield json.loads(ln)
            except ValueError:
                pass


class Res:
    """Per key-and-hour reservoir of samples (bounded memory)."""
    def __init__(self):
        self.d = collections.defaultdict(list)
        self.n = collections.Counter()

    def add(self, key, v):
        self.n[key] += 1
        L = self.d[key]
        if len(L) < RESERVOIR:
            L.append(v)
        else:
            j = random.randrange(self.n[key])
            if j < RESERVOIR:
                L[j] = v


def walk(routes, archive, days, until, on_pair, on_dwell, on_pass, on_stand=None):
    """Stream the archive bus by bus. Callbacks:
       on_pair(code, s0, s1, i0, i1, t0, t1)  a running pair
       on_stand(code, i, t0, t1, so_far)      a standing pair on segment i, standing so_far s
       on_dwell(code, stop_index, seconds, t) a dwell at a stop
       on_pass(code, stop_index, t)           a bus went by a stop (stopping or not)"""
    last, ep, intervals = {}, {}, []
    for day, p in day_files(archive, 'obs', days, until):
        for r in lines(p):
            reg, code, t = r['reg'], r['r'], r['t']
            prev = last.get(reg)
            near = prev[3] if prev and prev[1] == code else None
            pr = routes.project(code, r['la'], r['lo'], near)
            if not pr or pr[2] > 150:
                last.pop(reg, None); ep.pop(reg, None)
                continue
            s, i, _off = pr
            if prev and prev[1] == code and 0 < t - prev[0] <= MAX_GAP:
                dt_ = t - prev[0]
                intervals.append(dt_) if len(intervals) < 200000 else None
                d = s - prev[2]
                ds = routes.ds[code]
                if abs(d) < STILL_M:                                   # standing
                    e = ep.get(reg)
                    if e is None:
                        e = ep[reg] = {'ta': prev[0], 's': prev[2], 'tb': t, 'gin': prev[4]}
                    else:
                        e['tb'] = t
                    if on_stand:
                        on_stand(code, i, prev[0], t, t - e['ta'])
                elif d >= STILL_M and d <= 25 * dt_:                   # running
                    e = ep.pop(reg, None)
                    if e:
                        j = min(range(len(ds)), key=lambda q: abs(ds[q] - e['s']))
                        if abs(ds[j] - e['s']) <= NEAR_M and 0 < j < len(ds) - 1:
                            dur = (e['tb'] - e['ta']) + 0.25 * ((e['gin'] or 0) + (t - e['tb']))
                            if dur <= MAX_DWELL:
                                on_dwell(code, j, dur, e['ta'])
                    on_pair(code, prev[2], s, prev[3], i, prev[0], t)
                    for j in range(prev[3] + 1, i + 1):                # stops passed in between
                        if 0 < j < len(ds) - 1:
                            on_pass(code, j, t)
                else:                                                  # backwards or a jump: a new trip
                    ep.pop(reg, None)
                gap = dt_
            else:
                ep.pop(reg, None)
                gap = 0
            last[reg] = (t, code, s, i, gap if (prev and abs(s - prev[2]) >= STILL_M) else (prev[4] if prev else 0))
    return statistics.median(intervals) if intervals else 20


def build(a):
    routes = Routes(a.stops, a.stop_dist)
    seg, dwell, passes, city_v = Res(), Res(), collections.Counter(), Res()
    stops_n = collections.Counter()
    hour = lambda t: dt.datetime.fromtimestamp(t, IST).hour

    # Travel time adds up, so what is averaged is time per metre, not speed: per
    # segment and hour, all the seconds buses spent on it (standing included, so
    # dwell is in by construction) over all the metres they covered. A pair's time
    # is shared between the segments it spans in proportion to the distance in each.
    TD = collections.defaultdict(lambda: [0.0, 0.0, 0])      # (key, hour) -> [seconds, metres, pairs]

    def credit(code, s0, s1, i0, i1, t0, t1):
        ids, ds = routes.ids[code], routes.ds[code]
        h, span = hour(t1), max(s1 - s0, 1e-6)
        for i in range(i0, i1 + 1):
            if i + 1 >= len(ids) or i == 0 or i + 2 >= len(ids):      # no terminus segments: layovers
                continue
            a_, b_ = max(s0, ds[i]), min(s1, ds[i + 1])
            if b_ <= a_:
                continue
            k = f'{ids[i]}|{ids[i + 1]}'
            share = (b_ - a_) / span
            for key in ((k, h), (k, -1), ('*', h)):
                x = TD[key]; x[0] += (t1 - t0) * share; x[1] += b_ - a_; x[2] += 1

    def on_pair(code, s0, s1, i0, i1, t0, t1):
        if i1 - i0 > MAX_SPAN:
            return
        credit(code, s0, s1, i0, i1, t0, t1)
        city_v.add(hour(t1), (s1 - s0) / (t1 - t0))

    def on_stand(code, i, t0, t1, so_far):
        # standing time counts against the segment it stands on, up to STAND_MAX; longer
        # is a layover or a breakdown, not traffic
        ids = routes.ids[code]
        if so_far > STAND_MAX or i == 0 or i + 2 >= len(ids):
            return
        k = f'{ids[i]}|{ids[i + 1]}'
        for key in ((k, hour(t1)), (k, -1), ('*', hour(t1))):
            TD[key][0] += t1 - t0

    def on_dwell(code, j, dur, t):
        sid = routes.ids[code][j]
        dwell.add((sid, hour(t)), dur); dwell.add((sid, -1), dur)
        stops_n[sid] += 1

    def on_pass(code, j, t):
        passes[routes.ids[code][j]] += 1

    t0 = time.time()
    interval = walk(routes, a.archive, a.days, a.until, on_pair, on_dwell, on_pass, on_stand)
    med = lambda L: round(statistics.median(L), 2) if len(L) >= MIN_N else -1
    eff = lambda x: round(x[1] / x[0], 2) if x and x[2] >= MIN_N and x[0] > 0 and x[1] >= 100 else -1
    seg_out = {}
    for (k, h), x in list(TD.items()):
        if h != -1 or k == '*':
            continue
        seg_out[k] = [x[2], eff(x), [eff(TD.get((k, hh))) for hh in range(24)]]
    dw_out, all_d = {}, []
    for (sid, h), L in dwell.d.items():
        if h != -1:
            continue
        n_pass = max(passes[sid] + stops_n[sid], 1)
        dw_out[sid] = [n_pass, round(min(1.0, stops_n[sid] / n_pass), 3), med(L), [med(dwell.d.get((sid, hh), [])) for hh in range(24)]]
        all_d += L
    tot_pass = sum(passes.values()) + sum(stops_n.values())
    out = {'t': int(time.time()), 'days': a.days, 'until': a.until, 'interval_s': interval,
           'seg': seg_out, 'dwell': dw_out,
           'city': {'v': [eff(TD.get(('*', h))) for h in range(24)], 'v_running': [med(city_v.d.get(h, [])) for h in range(24)],
                    'dwell': med(all_d) if all_d else 20, 'p_stop': round(sum(stops_n.values()) / max(tot_pass, 1), 3)}}
    tmp = a.out + '.tmp'
    json.dump(out, open(tmp, 'w'), separators=(',', ':'))
    os.replace(tmp, a.out)
    if a.dwell_out:
        # for the map's stop cards: how long a bus typically stands at each stop when it
        # does stop (seconds), from stops seen at least 8 times
        small = {sid: round(v[2]) for sid, v in dw_out.items() if v[2] > 0 and v[0] >= 8}
        json.dump({'t': out['t'], 'city': out['city']['dwell'], 'stop': small}, open(a.dwell_out + '.tmp', 'w'), separators=(',', ':'))
        os.replace(a.dwell_out + '.tmp', a.dwell_out)
    print(f"{len(seg_out)} segments, {len(dw_out)} stops with dwells, fix interval {interval:.0f} s, "
          f"city dwell {out['city']['dwell']} s at p_stop {out['city']['p_stop']}, {time.time() - t0:.0f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stops', required=True)
    ap.add_argument('--stop-dist', required=True)
    ap.add_argument('--archive', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--until', default=None, help='last day to learn from (YYYY-MM-DD), for backtests')
    ap.add_argument('--dwell-out', default='', help='small per-stop dwell file for the map')
    ap.add_argument('--max-gap', type=int, default=MAX_GAP, help='longest fix gap read as one movement (s)')
    ap.add_argument('--max-span', type=int, default=MAX_SPAN, help='most segments one fix pair may span')
    a = ap.parse_args()
    configure(a.max_gap, a.max_span)
    build(a)


def configure(max_gap, max_span):
    global MAX_GAP, MAX_SPAN
    MAX_GAP, MAX_SPAN = max_gap, max_span


if __name__ == '__main__':
    sys.exit(main())
