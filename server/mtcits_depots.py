#!/usr/bin/env python3
"""Where MTC's buses park: depots found from the buses themselves, for the collector's in-depot rule.

    mtcits_depots.py ARCHIVE_DIR OUT.json [--nights 7] [--route-stops mapsv2/data/route_stops.json]

Every night most buses stand still at their depot. For each of the last NIGHTS nights, each bus that stood
within 100 m of one place for most of 00:00-04:30 IST gives one point; points within CLUSTER_M of each other
are one depot, kept when MIN_NIGHTS bus-nights put a bus there. OUT: [[lat, lon, bus_nights(, buses_by_day)], ...].
A big terminus where buses sleep (K.K. Nagar, Koyambedu) is found as well, and for this purpose it is one:
a bus standing there for minutes is not running a trip.
With --route-stops, yards used by day are added (charging, shift changes, spare buses: Kundrathur and Anna Nagar
East depots had buses standing 40-60 bus-hours over 14-16 Nov and few overnight): places where buses stood still
for DAY_MIN minutes or more, more than OFF_M from every stop of the route they were on, by at least MIN_BUSES
different buses. A long stand at a bus station on the route is a layover, not a yard, and is not counted.
Run weekly on raw (mtc-depots.timer); mtcits_collector.py reloads the file when it changes.
"""
import argparse
import collections
import datetime as dt
import glob
import gzip
import json
import math
import os
import statistics

CLUSTER_M, MIN_NIGHTS, STILL_M = 250, 5, 100
DAY_MIN, OFF_M, MIN_BUSES = 30, 300, 3
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def hav(a, b):
    p = math.pi / 180
    h = math.sin((b[0] - a[0]) * p / 2) ** 2 + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(min(1.0, h)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('archive'); ap.add_argument('out')
    ap.add_argument('--nights', type=int, default=7)
    ap.add_argument('--route-stops', default='')
    a = ap.parse_args()
    stops = {}
    if a.route_stops:
        for c, r in json.load(open(a.route_stops)).items():
            stops[c] = [(x[2], x[3]) for d in r.get('d') or [] for x in d.get('s') or []]
    files = sorted(glob.glob(os.path.join(a.archive, 'obs-*.jsonl*')))
    files = [f for f in files if f.endswith(('.jsonl', '.jsonl.gz'))][-(a.nights + 1):]
    pts, day_pts = [], []
    for fn in files:
        night = collections.defaultdict(list)
        allfx = collections.defaultdict(list)
        with (gzip.open(fn, 'rt') if fn.endswith('.gz') else open(fn)) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not r.get('la'):
                    continue
                t = dt.datetime.fromtimestamp(r['t'], IST)
                if stops:
                    allfx[r['reg']].append((r['t'], r['la'], r['lo'], r.get('r')))
                if t.hour < 4 or (t.hour == 4 and t.minute < 30):
                    night[(t.date(), r['reg'])].append((r['la'], r['lo']))
        for fx in night.values():
            if len(fx) < 3:
                continue
            m = (statistics.median(p[0] for p in fx), statistics.median(p[1] for p in fx))
            if sum(hav(m, p) < STILL_M for p in fx) >= 0.8 * len(fx):
                pts.append(m)
        for reg, fx in allfx.items():               # yards by day: long stands away from the route's stops
            fx.sort()
            i = 0
            while i < len(fx):
                j = i
                while j + 1 < len(fx) and hav(fx[i][1:3], fx[j + 1][1:3]) <= 60:
                    j += 1
                if fx[j][0] - fx[i][0] >= DAY_MIN * 60:
                    p, own = fx[i][1:3], stops.get(fx[i][3])
                    if own and min(hav(p, q) for q in own) > OFF_M:
                        day_pts.append((p, reg))
                i = j + 1
    left, depots = list(pts), []
    while left:
        best = max(left[:4000], key=lambda p: sum(hav(p, q) < CLUSTER_M for q in left))
        mem = [q for q in left if hav(best, q) < CLUSTER_M]
        left = [q for q in left if hav(best, q) >= CLUSTER_M]
        if len(mem) >= MIN_NIGHTS:
            depots.append([round(statistics.median(q[0] for q in mem), 5), round(statistics.median(q[1] for q in mem), 5), len(mem)])
        elif len(mem) < 2:
            break                                   # the rest are single buses parked anywhere
    left = list(day_pts)                            # yards by day, where no night depot already is
    while left:
        best = max(left[:4000], key=lambda p: sum(hav(p[0], q[0]) < CLUSTER_M for q in left))
        mem = [q for q in left if hav(best[0], q[0]) < CLUSTER_M]
        left = [q for q in left if hav(best[0], q[0]) >= CLUSTER_M]
        if len({q[1] for q in mem}) < MIN_BUSES:
            if len(mem) < 2:
                break
            continue
        c = (round(statistics.median(q[0][0] for q in mem), 5), round(statistics.median(q[0][1] for q in mem), 5))
        if all(hav(c, d[:2]) >= CLUSTER_M for d in depots):
            depots.append([c[0], c[1], 0, len({q[1] for q in mem})])     # [lat, lon, bus-nights 0, buses by day]
    tmp = a.out + '.tmp'
    json.dump(sorted(depots, key=lambda d: -d[2]), open(tmp, 'w'))
    os.replace(tmp, a.out)
    print(f'{len(files)} days, {len(pts)} bus-nights parked, {len(day_pts)} long stands off route; '
          f'{sum(1 for d in depots if d[2])} depots by night + {sum(1 for d in depots if not d[2])} yards by day -> {a.out}')


if __name__ == '__main__':
    main()
