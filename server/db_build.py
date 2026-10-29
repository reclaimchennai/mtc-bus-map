#!/usr/bin/env python3
"""Data for the MTC dashboard (mtc.reclaimchennai.city/db), from the live archive.

    db_build.py --site ~/projects/maps-site/mapsv2 --archive ~/projects/mtc-live/archive \
                --roster ~/projects/mtc-live/roster [--cmrl ~/projects/cmrl/data/cmrl.db] [--days 7]

Writes SITE/db/data.json. Everything is measured from what the live collector
archived, except crowding, which is labelled as an estimate:

fleet       distinct buses reporting GPS per hour, split electric / AC / other
speeds      observed road speed (distance along the bus's route between two GPS
            fixes over the time between them): city median by hour, distribution,
            and the slowest stop-to-stop segments at the peaks
bunching    every route poll returns all of a working's buses at once. Ordered
            along the route, two of them less than BUNCH_M apart — on a working
            scheduled at least MIN_HEADWAY minutes apart, and away from either
            terminus — are bunched. By hour, by route, by place, plus recent cases.
crowding    when the city travels (CMRL ticket entries by hour) against buses on
            the road by hour (observed) — a relative index, not a passenger count
roster      the day's scheduled trips with the bus rostered to each

Then bus_build.py writes SITE/db/bus/ — every bus over time, for the page's
"Single bus" view, from the whole archive (closed days cached in --bus-cache).
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import gzip
import json
import math
import os
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bus_build  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BUNCH_M, NEAR_M, END_M, MIN_HEADWAY = 250, 500, 400, 6
DEM_WD = [0.01, 0, 0, 0, 0.27, 0.93, 2.41, 3.92, 7.53, 11.08, 7.71, 4.39, 4.16, 4.67, 5.17, 4.44, 4.86, 6.73, 10.12, 9.44, 6.16, 3.69, 1.83, 0.47]


def lines(path):
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt') as fh:
        for ln in fh:
            try:
                yield json.loads(ln)
            except ValueError:
                pass


def day_files(archive, kind, days):
    today = dt.datetime.now(IST).date()
    for n in range(days, -1, -1):
        d = (today - dt.timedelta(days=n)).isoformat()
        for p in (f'{archive}/{kind}-{d}.jsonl', f'{archive}/{kind}-{d}.jsonl.gz'):
            if os.path.exists(p):
                yield d, p


def cmrl_profile(db):
    """Weekday share of the day's metro entries by hour, from the CMRL archive."""
    try:
        c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=20)
        by = collections.defaultdict(dict)
        q = ("select service_date, hour, sum(v) from (select service_date, hour, series, max(value) v from hourly_obs "
             "where domain='passenger' group by service_date, hour, series) group by service_date, hour")
        for d, h, v in c.execute(q):
            by[d][h] = v
        acc = collections.defaultdict(list)
        for d, hs in by.items():
            if len(hs) < 15 or dt.date.fromisoformat(d).weekday() >= 5:
                continue
            tot = sum(hs.values())
            for h in range(24):
                acc[h].append(100 * hs.get(h, 0) / tot)
        prof = [round(sum(acc[h]) / len(acc[h]), 2) if acc[h] else 0 for h in range(24)]
        return prof, len(by)
    except Exception:
        return DEM_WD, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', required=True)
    ap.add_argument('--archive', required=True)
    ap.add_argument('--roster', default='')
    ap.add_argument('--cmrl', default='')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--bus-cache', default='', help='default: busdays/ next to the archive')
    a = ap.parse_args()
    t0 = time.time()
    D = a.site + '/data/'
    RS = json.load(open(D + 'route_stops.json'))
    SD = json.load(open(D + 'stop_dist.json'))
    TM = json.load(open(D + 'timings.json'))
    sn = {c: r['sn'] for c, r in RS.items()}
    st = {c: r['d'][0]['s'] for c, r in RS.items()}

    def along(code, lat, lon, k):
        ds, ss = SD.get(code), st.get(code)
        if not ds or not ss or k is None or k >= len(ds) or k >= len(ss):
            return None
        if k == 0:
            return ds[0]
        (_, _, ay, ax), (_, _, by_, bx) = ss[k - 1], ss[k]
        cx = math.cos(math.radians(lat)) * 111320
        vx, vy = (bx - ax) * cx, (by_ - ay) * 111320
        px, py = (lon - ax) * cx, (lat - ay) * 111320
        L2 = vx * vx + vy * vy
        t = max(0.0, min(1.0, (px * vx + py * vy) / L2)) if L2 else 0.0
        return ds[k - 1] + t * (ds[k] - ds[k - 1])

    def nearest_stop(code, d):
        ds = SD[code]
        i = min(range(len(ds)), key=lambda j: abs(ds[j] - d))
        s = st[code][i]
        return s[1], s[2], s[3]

    # ── one pass over the GPS archive ──
    fleet = collections.defaultdict(lambda: [set(), set(), set()])     # hour -> all, electric, ac
    days_seen, regs_all, routes_all, nfix = set(), set(), set(), 0
    batches = collections.defaultdict(dict)                           # (code, 2-min bucket) -> reg -> (d, lat, lon, t)
    last = {}
    for day, p in day_files(a.archive, 'obs', a.days):
        days_seen.add(day)
        for r in lines(p):
            nfix += 1
            h = dt.datetime.fromtimestamp(r['t'], IST).hour
            f = fleet[(day, h)]
            f[0].add(r['reg'])
            if 'e' in (r.get('f') or ''):
                f[1].add(r['reg'])
            if 'a' in (r.get('f') or ''):
                f[2].add(r['reg'])
            regs_all.add(r['reg']); routes_all.add(r['r'])
            d = along(r['r'], r['la'], r['lo'], r.get('k'))
            # moving = advanced >= 100 m ALONG its route since its previous fix, within
            # 10 min. Parked buses (depots, mid-route stands) and GPS jitter are not
            # bunching; a straight-line distance let jitter through.
            pv = last.get(r['reg'])
            moving = bool(pv and d is not None and pv[3] is not None and pv[1] == r['r']
                          and 0 < r['t'] - pv[0] <= 600 and d - pv[3] >= 100)
            last[r['reg']] = (r['t'], r['r'], None, d)
            if d is not None and moving:
                batches[(r['r'], r['t'] // 120)][r['reg']] = (d, r['la'], r['lo'], r['t'])

    # fleet by hour: mean over days of distinct buses
    fh = collections.defaultdict(lambda: [[], [], []])
    for (day, h), f in fleet.items():
        for i in range(3):
            fh[h][i].append(len(f[i]))
    fleet_by_hour = [[round(statistics.mean(fh[h][i])) if fh[h][i] else 0 for i in range(3)] for h in range(24)]

    # ── bunching ──
    pairs_h, bunch_h, near_h = [0] * 24, [0] * 24, [0] * 24
    by_route = collections.defaultdict(lambda: [0, 0])                # code -> [pairs, bunched]
    places = collections.defaultdict(lambda: [0, 0, 0, set()])        # stop name -> [count, lat, lon, routes]
    cases = []
    for (code, b), buses in batches.items():
        if len(buses) < 2 or code not in TM:
            continue
        total = SD[code][-1] if SD.get(code) else 0
        h = dt.datetime.fromtimestamp(b * 120, IST).hour
        per_h = (TM[code].get('hours') or [0] * 24)[h]
        if per_h and 60 / per_h < MIN_HEADWAY:
            continue                                                  # turn-up-and-go: closeness is normal
        pts = sorted((d, reg, la, lo, t) for reg, (d, la, lo, t) in buses.items() if END_M < d < total - END_M)
        for (d1, r1, la1, lo1, t1), (d2, r2, la2, lo2, t2) in zip(pts, pts[1:]):
            gap = d2 - d1
            pairs_h[h] += 1; by_route[code][0] += 1
            if gap < NEAR_M:
                near_h[h] += 1
            if gap < BUNCH_M:
                bunch_h[h] += 1; by_route[code][1] += 1
                name, slat, slon = nearest_stop(code, (d1 + d2) / 2)
                pl = places[name]
                pl[0] += 1; pl[1] = slat; pl[2] = slon; pl[3].add(sn.get(code, code))
                cases.append([max(t1, t2), sn.get(code, code), code, name, r1, r2, round(gap)])
    top_routes = sorted(((c, p, bb) for c, (p, bb) in by_route.items() if p >= 20), key=lambda x: -x[2] / x[1])[:25]
    top_routes = [[sn.get(c, c), c, f"{st[c][0][1]} → {st[c][-1][1]}", p, bb, round(100 * bb / p, 1)] for c, p, bb in top_routes]
    hot = sorted(places.items(), key=lambda kv: -kv[1][0])[:40]
    hot = [[n, v[0], round(v[1], 5), round(v[2], 5), sorted(v[3])[:6]] for n, v in hot]
    cases.sort(reverse=True)
    cases = [[dt.datetime.fromtimestamp(c[0], IST).strftime('%d %b %H:%M')] + c[1:] for c in cases[:60]]

    # ── speeds ──
    city = collections.defaultdict(list)
    seg = collections.defaultdict(lambda: [[], []])                    # key -> [all, peak]
    seg_routes = collections.defaultdict(set)
    nspeed = 0
    for day, p in day_files(a.archive, 'speed', a.days):
        for r in lines(p):
            ss = st.get(r['r'])
            if not ss or r['i1'] - r['i0'] > 20 or r['v'] < 2:
                continue                                              # parked / laid over, not traffic
            nspeed += 1
            h = dt.datetime.fromtimestamp(r['t'], IST).hour
            city[h].append(r['v'])
            peak = h in (8, 9, 10, 17, 18, 19)
            for i in range(max(1, r['i0']), min(r['i1'], len(ss) - 3) + 1):   # not the terminus segments
                k = (ss[i][0], ss[i + 1][0])
                seg[k][0].append(r['v'])
                if peak:
                    seg[k][1].append(r['v'])
                seg_routes[k].add(sn.get(r['r'], r['r']))
    med = lambda xs: round(statistics.median(xs), 1) if xs else None
    city_h = [med(city[h]) for h in range(24)]
    allv = [v for h in city for v in city[h]]
    hist = [0] * 16
    for v in allv:
        hist[min(15, int(v // 4))] += 1
    names = {}
    for c, ss in st.items():
        for s in ss:
            names[s[0]] = (s[1], s[2], s[3])
    slow = []
    for k, (va, vp) in seg.items():
        if len(vp) >= 15 and k[0] in names and k[1] in names:
            slow.append([med(vp), len(vp), med(va), names[k[0]][0], names[k[1]][0],
                         round(names[k[0]][1], 5), round(names[k[0]][2], 5), round(names[k[1]][1], 5), round(names[k[1]][2], 5),
                         sorted(seg_routes[k])[:8], f'{k[0]}|{k[1]}'])
    slow.sort(key=lambda x: x[0])

    # ── crowding: demand share / fleet share, by hour ──
    prof, cmrl_days = cmrl_profile(a.cmrl) if a.cmrl else (DEM_WD, 0)
    fl_tot = sum(f[0] for f in fleet_by_hour) or 1
    crowd = [round((prof[h] / 100) / max(fleet_by_hour[h][0] / fl_tot, 1e-6), 2) if fleet_by_hour[h][0] else None for h in range(24)]

    # ── roster ──
    roster = None
    rf = sorted(glob.glob(os.path.join(a.roster, 'roster-*.csv.gz'))) if a.roster else []
    if rf:
        rows = list(csv.DictReader(gzip.open(rf[-1], 'rt')))
        per_bus = collections.Counter(r['vehicle_number'] for r in rows if r['vehicle_number'])
        roster = {'date': os.path.basename(rf[-1])[7:17], 'trips': len(rows), 'buses': len(per_bus),
                  'electric_trips': sum(1 for r in rows if r['electric'] == '1'),
                  'low_floor_trips': sum(1 for r in rows if r['low_floor'] == '1'),
                  'by_tier': collections.Counter(r['tier'] for r in rows).most_common(),
                  'trips_per_bus': sorted(collections.Counter(min(n, 20) for n in per_bus.values()).items())}

    out = {'generated': dt.datetime.now(IST).isoformat(timespec='minutes'), 'days': sorted(days_seen),
           'coverage': {'fixes': nfix, 'buses': len(regs_all), 'routes': len(routes_all), 'speed_samples': nspeed},
           'fleet_by_hour': fleet_by_hour,
           'speeds': {'city_by_hour': city_h, 'hist_4kmh': hist, 'median': med(allv), 'slowest_peak': slow[:30]},
           'bunching': {'by_hour': [[pairs_h[h], bunch_h[h], near_h[h]] for h in range(24)],
                        'total_pairs': sum(pairs_h), 'total_bunched': sum(bunch_h),
                        'top_routes': top_routes, 'hotspots': hot, 'recent': cases,
                        'rule': {'bunch_m': BUNCH_M, 'near_m': NEAR_M, 'terminus_m': END_M, 'min_headway': MIN_HEADWAY}},
           'crowding': {'demand_profile': prof, 'cmrl_days': cmrl_days, 'index_by_hour': crowd},
           'roster': roster}
    os.makedirs(a.site + '/db', exist_ok=True)
    tmp = a.site + '/db/data.json.tmp'
    json.dump(out, open(tmp, 'w'), separators=(',', ':'))
    os.replace(tmp, a.site + '/db/data.json')
    print(f'{nfix} fixes, {nspeed} speed samples, {sum(bunch_h)}/{sum(pairs_h)} pairs bunched, '
          f'{len(days_seen)} days, {time.time() - t0:.0f}s')
    try:
        bus_build.build(a.site, a.archive, a.bus_cache or os.path.join(os.path.dirname(os.path.abspath(a.archive)), 'busdays'))
    except Exception as e:                                            # the dashboard above is already written
        print(f'bus_build failed: {e!r}')


if __name__ == '__main__':
    main()
