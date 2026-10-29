#!/usr/bin/env python3
"""One bus over time, for the MTC dashboard's "Single bus" view, from the live archive.

    bus_build.py --site ~/projects/maps-site/mapsv2 --archive ~/projects/mtc-live/archive \
                 [--cache ~/projects/mtc-live/busdays]

Also run at the end of db_build.py. Reads every obs-YYYY-MM-DD.jsonl[.gz] the
collectors archived (Chennai One from 19 Oct 2026, the MTC app's feed, Chalo,
from 20 Oct) and writes, under SITE/db/bus/:

  index.json      every registration seen: [reg, days, km, fleet flags, last day]
                  plus fleet-wide daily counts
  <REG>.json      one bus: per day km, hours, fixes, routes run, where the day
                  started and ended; totals, routes and workings by runs

Everything is what we observed, not what MTC planned or ticketed:

km          distance ALONG the route between two consecutive fixes of the bus
            (stop_dist.json, the projection live_collector.Collector.along uses),
            not straight line. Counted when both fixes are on the same working,
            at most GAP_S apart, the bus advanced >= MIN_STEP_M (less is GPS
            jitter at a stand) at no more than MAX_KMH. When the working changes
            to another direction of the same route number (a turnaround), the
            rest of the old trip plus the start of the new one is counted if
            that is plausible in the time. Gaps longer than GAP_S, fixes on a
            route we could not place ("c:" codes) and buses the feed did not
            report add nothing, so this undercounts: a floor, not an odometer.
hours       time between consecutive fixes no more than GAP_S apart.
run         consecutive fixes on one working: one trip, or the part of it seen.
home        the bus terminus (bus_termini.geojson; ones within PLACE_M merged)
            nearest the day's first fix after 03:00 and its last fix, within
            NEAR_M, most often over all days — where the bus usually starts and
            ends its day. Depot locations are not published, and nothing is
            polled between 00:30 and 04:30, so this is a likely base, not a fact.

Closed days (gzipped archive files) are summarised once into --cache; only the
open day is re-read each hour.
"""
import argparse
import collections
import datetime as dt
import glob
import gzip
import json
import math
import os
import re
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GAP_S, MIN_STEP_M, MAX_KMH, TURN_KMH, NEAR_M, PLACE_M = 2400, 30, 70, 50, 1500, 600
FLAGS = 'aelxp'
VERSION = 2                      # bump to re-summarise cached days after a rule change


def lines(path):
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt') as fh:
        for ln in fh:
            try:
                yield json.loads(ln)
            except ValueError:
                pass


class Routes:
    def __init__(self, data):
        rs = json.load(open(data + 'route_stops.json'))
        self.sd = json.load(open(data + 'stop_dist.json'))
        self.sn = {c: r['sn'] for c, r in rs.items()}
        self.ll = {c: [(s[2], s[3]) for s in r['d'][0]['s']] for c, r in rs.items()}
        self.ends = {c: f"{r['d'][0]['s'][0][1]} → {r['d'][0]['s'][-1][1]}" for c, r in rs.items() if r['d'][0]['s']}
        # termini within PLACE_M of each other are one place ("TAMBARAM WEST" and its bus
        # stand), named after the member that serves the most routes
        g = json.load(open(data + 'bus_termini.geojson'))
        term = sorted(([(f['properties'].get('Stop Name') or f['properties'].get('Name of th') or '').strip(),
                        f['geometry']['coordinates'][1], f['geometry']['coordinates'][0],
                        f['properties'].get('nroutes') or 0] for f in g['features']), key=lambda t: -t[3])
        self.term = []
        for name, la, lo, _n in (t for t in term if t[0]):
            home = next((p for p in self.term if self.dist(p, la, lo) <= PLACE_M), None)
            self.term.append([name, la, lo, home[3] if home else name])

    @staticmethod
    def dist(p, lat, lon):
        return math.hypot((p[2] - lon) * math.cos(math.radians(lat)) * 111320, (p[1] - lat) * 111320)

    def along(self, code, lat, lon, k):
        """Metres along the working of a fix whose next stop is k (as Collector.along)."""
        ds, ll = self.sd.get(code), self.ll.get(code)
        if not ds or not ll or k is None or k >= len(ds) or k >= len(ll):
            return None
        if k == 0:
            return ds[0]
        (ay, ax), (by, bx) = ll[k - 1], ll[k]
        cx = math.cos(math.radians(lat)) * 111320
        vx, vy = (bx - ax) * cx, (by - ay) * 111320
        px, py = (lon - ax) * cx, (lat - ay) * 111320
        L2 = vx * vx + vy * vy
        t = max(0.0, min(1.0, (px * vx + py * vy) / L2)) if L2 else 0.0
        return ds[k - 1] + t * (ds[k] - ds[k - 1])

    def route_no(self, code):
        return code[2:].strip() if code.startswith('c:') else self.sn.get(code, code)

    def terminus(self, lat, lon):
        best = min(self.term, key=lambda t: self.dist(t, lat, lon), default=None)
        return best[3] if best and self.dist(best, lat, lon) <= NEAR_M else None


def summarise_day(path, R):
    """{reg: day record} for one archive file."""
    fx = collections.defaultdict(list)
    for r in lines(path):
        if r.get('reg') and r.get('r'):
            fx[r['reg']].append(r)
    out = {}
    for reg, rs in fx.items():
        rs.sort(key=lambda r: r['t'])
        km = sec = 0.0
        runs, routes, flags = collections.Counter(), collections.Counter(), collections.Counter()
        placed = 0
        prev = None                                                    # (t, code, d)
        for r in rs:
            code = r['r']
            d = R.along(code, r['la'], r['lo'], r.get('k'))
            placed += d is not None
            for f in r.get('f') or '':
                if f in FLAGS:
                    flags[f] += 1
            gap = r['t'] - prev[0] if prev else None
            if prev is None or prev[1] != code or gap > GAP_S:
                runs[code] += 1
                routes[R.route_no(code)] += 1
            if prev and 0 < gap <= GAP_S:
                sec += gap
                if d is not None and prev[2] is not None:
                    if prev[1] == code:
                        dd = d - prev[2]
                        if dd >= MIN_STEP_M and dd / gap * 3.6 <= MAX_KMH:
                            km += dd
                    elif R.route_no(prev[1]) == R.route_no(code):
                        dd = (R.sd[prev[1]][-1] - prev[2]) + (d - R.sd[code][0])
                        if dd >= MIN_STEP_M and dd / gap * 3.6 <= TURN_KMH:
                            km += dd
            prev = (r['t'], code, d)
        # the day starts at its first fix after 03:00: earlier ones are last night's tail
        day0 = next((r for r in rs if dt.datetime.fromtimestamp(r['t'], IST).hour >= 3), rs[0])
        out[reg] = {'n': len(rs), 'placed': placed, 't0': day0['t'], 't1': rs[-1]['t'],
                    'km': round(km / 1000, 1), 'h': round(sec / 3600, 2),
                    'runs': dict(runs), 'routes': dict(routes), 'flags': dict(flags),
                    's0': R.terminus(day0['la'], day0['lo']), 's1': R.terminus(rs[-1]['la'], rs[-1]['lo'])}
    return out


def day_summaries(archive, cache, R):
    days = {}
    for p in sorted(glob.glob(os.path.join(archive, 'obs-*.jsonl*'))):
        m = re.search(r'obs-(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$', p)
        if not m:
            continue
        day, closed = m.group(1), bool(m.group(2))
        if day in days and not closed:
            continue                                                   # both forms present: the .gz is the whole day
        cf = os.path.join(cache, f'{day}.json') if cache else None
        if closed and cf and os.path.exists(cf):
            j = json.load(open(cf))
            if j.get('v') == VERSION:
                days[day] = j['buses']
                continue
        days[day] = summarise_day(p, R)
        if closed and cf:
            json.dump({'v': VERSION, 'buses': days[day]}, open(cf + '.tmp', 'w'), separators=(',', ':'))
            os.replace(cf + '.tmp', cf)
    return days


def fleet_type(flags, n):
    """Flags carried on at least half the bus's fixes."""
    return ''.join(f for f in FLAGS if flags.get(f, 0) >= n / 2)


def build(site, archive, cache=''):
    t0 = time.time()
    R = Routes(os.path.join(site, 'data') + '/')
    if cache:
        os.makedirs(cache, exist_ok=True)
    days = day_summaries(archive, cache, R)
    out_dir = os.path.join(site, 'db', 'bus')
    os.makedirs(out_dir, exist_ok=True)
    per = collections.defaultdict(dict)                                # reg -> day -> rec
    fleet = []
    for day in sorted(days):
        b = days[day]
        kms = sorted(v['km'] for v in b.values())
        moving = [k for k in kms if k > 0]
        fleet.append({'d': day, 'buses': len(b), 'moving': len(moving), 'km': round(sum(kms)),
                      'km_med': moving[len(moving) // 2] if moving else 0,
                      'km_mean': round(sum(moving) / len(moving), 1) if moving else 0,
                      'over200': sum(1 for k in moving if k > 200),
                      'h_med': sorted(v['h'] for v in b.values())[len(b) // 2] if b else 0})
        for reg, rec in b.items():
            per[reg][day] = rec
    index = []
    for reg, dd in per.items():
        safe = re.sub(r'[^A-Z0-9]', '', reg.upper())
        if not safe:
            continue
        flags, n = collections.Counter(), 0
        runs, routes, route_days, starts, ends = (collections.Counter() for _ in range(5))
        log = []
        for day in sorted(dd):
            r = dd[day]
            flags.update(r['flags']); n += r['n']
            runs.update(r['runs']); routes.update(r['routes'])
            route_days.update(r['routes'].keys())
            if r['s0']:
                starts[r['s0']] += 1
            if r['s1']:
                ends[r['s1']] += 1
            top = sorted(r['routes'].items(), key=lambda kv: -kv[1])
            log.append([day, r['km'], r['h'], r['n'], [k for k, _ in top],
                        dt.datetime.fromtimestamp(r['t0'], IST).strftime('%H:%M'),
                        dt.datetime.fromtimestamp(r['t1'], IST).strftime('%H:%M'), r['s0'], r['s1'],
                        round(100 * r['placed'] / r['n']) if r['n'] else 0])
        ndays = len(dd)
        km = round(sum(r['km'] for r in dd.values()), 1)
        ft = fleet_type(flags, n)
        nights = starts + ends                                         # where it ends one day and starts the next
        home = nights.most_common(1)[0] if nights else None
        working = sorted(runs.items(), key=lambda kv: -kv[1])[:15]
        rec = {'reg': reg, 'type': ft, 'days': ndays, 'first': min(dd), 'last': max(dd),
               'km': km, 'h': round(sum(r['h'] for r in dd.values()), 1), 'fixes': n,
               'home': [home[0], home[1], sum(nights.values())] if home else None,
               'starts': starts.most_common(3), 'ends': ends.most_common(3),
               'routes': [[k, route_days[k], v] for k, v in sorted(routes.items(), key=lambda kv: (-route_days[kv[0]], -kv[1]))[:15]],
               'workings': [[c, R.route_no(c), R.ends.get(c, ''), v] for c, v in working],
               'log': log}
        json.dump(rec, open(os.path.join(out_dir, safe + '.json.tmp'), 'w'), separators=(',', ':'))
        os.replace(os.path.join(out_dir, safe + '.json.tmp'), os.path.join(out_dir, safe + '.json'))
        index.append([reg, ndays, round(km), ft, max(dd), home[0] if home else ''])
    index.sort()
    idx = {'generated': dt.datetime.now(IST).isoformat(timespec='minutes'), 'days': sorted(days), 'fleet': fleet,
           'buses': index, 'rule': {'gap_s': GAP_S, 'min_step_m': MIN_STEP_M, 'max_kmh': MAX_KMH, 'near_m': NEAR_M}}
    json.dump(idx, open(os.path.join(out_dir, 'index.json.tmp'), 'w'), separators=(',', ':'))
    os.replace(os.path.join(out_dir, 'index.json.tmp'), os.path.join(out_dir, 'index.json'))
    print(f'buses: {len(index)} over {len(days)} days, {time.time() - t0:.0f}s')
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', required=True)
    ap.add_argument('--archive', required=True)
    ap.add_argument('--cache', default='')
    a = ap.parse_args()
    build(a.site, a.archive, a.cache)


if __name__ == '__main__':
    main()
