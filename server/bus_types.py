"""MTC's service types, and buses with no trip, for the /db dashboard (called by db_build.py).

Service type: MTC's own name for each bus on its tracking (servicetype: Ordinary, LF Bus (D),
Deluxe, Express, Small Bus, Ladies Only, Deluxe (EV), AC (EV), Premium), which the collector
folds into flags (mtcits_collector.flags_of). Every AC bus MTC runs is electric. The order and
names are the map's (mapsv2/live.js ST).

No trip ("ghost"): MTC's tracking has the bus's position but no trip for it (flag n; its Track a
Bus shows "N/A" to "N/A"). Per hour, a bus counts as on a trip if any of its fixes in the hour
has one, and as a ghost only if none does. Where ghosts stand: at a terminus (first or last stop
of any route, within TERM_M) or out on the road.

homes(): each bus's base, the terminus it most often starts its day from (bus_build.py's
"home"), placed at that stop: db/homes.json, which the map uses to put a ghost that has gone
quiet back at its base.
"""
import collections
import glob
import json
import math
import os
import statistics

TYPES = [('ordinary', 'Ordinary'), ('lf', 'Low floor'), ('deluxe', 'Deluxe'), ('express', 'Express'),
         ('small', 'Small bus'), ('ladies', 'Ladies only'), ('devl', 'Electric'), ('acev', 'Electric AC'),
         ('premium', 'Premium')]
IDX = {k: i for i, (k, _) in enumerate(TYPES)}
TERM_M = 300


def stype(fl):
    fl = fl or ''
    if 'p' in fl:
        return 'premium'
    if 'e' in fl:
        return 'acev' if 'a' in fl else 'devl'
    for c, k in (('s', 'small'), ('w', 'ladies'), ('l', 'lf'), ('d', 'deluxe'), ('x', 'express')):
        if c in fl:
            return k
    return 'ordinary'


class Termini:
    """first and last stops of every route, on a ~300 m grid"""

    def __init__(self, route_stops):
        self.cell = collections.defaultdict(list)
        for r in route_stops.values():
            for d in r.get('d') or []:
                s = d.get('s') or []
                for p in (s[:1] + s[-1:]):
                    self.cell[(round(p[2] / 0.003), round(p[3] / 0.003))].append((p[2], p[3], p[1]))

    def near(self, la, lo):
        k = (round(la / 0.003), round(lo / 0.003))
        best, bd = None, TERM_M
        cx = math.cos(math.radians(la)) * 111320
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for pla, plo, name in self.cell.get((k[0] + a, k[1] + b), ()):
                    d = math.hypot((plo - lo) * cx, (pla - la) * 111320)
                    if d < bd:
                        best, bd = name, d
        return best


class Tally:
    def __init__(self, route_stops):
        self.types = {}                                              # reg -> type (latest flags)
        self.hour = collections.defaultdict(lambda: [set(), set()])  # (day, h) -> [buses on a trip, buses seen]
        self.ghost_fix = collections.defaultdict(lambda: [0, 0])     # reg -> [ghost fixes, all fixes]
        self.ghost_last = {}                                         # reg -> (t, la, lo) of its latest ghost fix
        self.last_t = 0
        self.term = Termini(route_stops)

    def add(self, day, h, r):
        fl, reg = r.get('f') or '', r['reg']
        self.types[reg] = stype(fl)
        cell = self.hour[(day, h)]
        cell[1].add(reg)
        g = self.ghost_fix[reg]
        g[1] += 1
        if 'n' in fl:
            g[0] += 1
            self.ghost_last[reg] = (r['t'], r['la'], r['lo'])
        else:
            cell[0].add(reg)
        self.last_t = max(self.last_t, r['t'])

    def result(self):
        modes = {'reporting': [], 'on_trip': [], 'no_trip': []}
        per = collections.defaultdict(lambda: {m: [] for m in modes})   # h -> mode -> [counts per type, per day]
        ghost_hours = collections.Counter()
        trip_hours = collections.Counter()
        for (day, h), (trip, seen) in self.hour.items():
            sets = {'reporting': seen, 'on_trip': trip, 'no_trip': seen - trip}
            for m, s in sets.items():
                row = [0] * len(TYPES)
                for reg in s:
                    row[IDX[self.types.get(reg, 'ordinary')]] += 1
                per[h][m].append(row)
            for reg in seen - trip:
                ghost_hours[reg] += 1
            for reg in trip:
                trip_hours[reg] += 1
        for m in modes:
            for h in range(24):
                rows = per[h][m] if h in per else []
                modes[m].append([round(statistics.mean(c)) for c in zip(*rows)] if rows else None)
        # right now: buses heard in the last 15 min of the archive
        recent = {reg for reg, (t, _, _) in self.ghost_last.items() if self.last_t - t <= 900}
        places = collections.Counter()
        for reg in recent:
            _, la, lo = self.ghost_last[reg]
            places[self.term.near(la, lo) or '']+= 1
        at_term = sum(n for k, n in places.items() if k)
        worst = sorted(ghost_hours.items(), key=lambda x: -x[1])[:25]
        return {
            'types': [list(t) for t in TYPES],
            'fleet_by_type': modes,
            'ghost': {
                'rule': {'terminus_m': TERM_M, 'now_min': 15},
                'now': len(recent), 'now_at_terminus': at_term,
                'now_places': [[k, n] for k, n in places.most_common(20) if k],
                'buses_ever': sum(1 for n in ghost_hours.values() if n),
                'share_of_fixes': round(sum(g[0] for g in self.ghost_fix.values()) / max(1, sum(g[1] for g in self.ghost_fix.values())), 4),
                'most_hours': [[reg, self.types.get(reg, 'ordinary'), n, trip_hours.get(reg, 0)] for reg, n in worst],
            },
        }


def homes(site, route_stops):
    """db/homes.json: {reg: [lon, lat, base name]} from db/bus/<reg>.json's home terminus"""
    where = collections.defaultdict(list)
    for r in route_stops.values():
        for d in r.get('d') or []:
            for p in d.get('s') or []:
                where[(p[1] or '').strip().upper()].append((p[3], p[2]))
    out, missing = {}, 0
    for f in glob.glob(os.path.join(site, 'db', 'bus', '*.json')):
        if f.endswith('index.json'):
            continue
        try:
            b = json.load(open(f))
        except (OSError, ValueError):
            continue
        hm = b.get('home') or []
        pts = where.get((hm[0] if hm else '').strip().upper())
        if not pts:
            missing += 1
            continue
        lo, la = statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts)
        out[b['reg']] = [round(lo, 5), round(la, 5), hm[0]]
    tmp = os.path.join(site, 'db', 'homes.json.tmp')
    json.dump(out, open(tmp, 'w'), separators=(',', ':'))
    os.replace(tmp, os.path.join(site, 'db', 'homes.json'))
    print(f'homes.json: {len(out)} buses placed at their base, {missing} without one')
