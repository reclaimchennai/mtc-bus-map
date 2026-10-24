#!/usr/bin/env python3
"""Live MTC buses from the official MTC app's backend (Chalo/Zophop), politely.

    chalo_collector.py --stops route_stops.json --timings timings.json --stop-dist stop_dist.json \
        --routes chalo_routes.json --archive DIR --out vehicles.json [--rate 1.5]

Stands in for live_collector.py while the Chennai One account is blocked. One
process on our server is the only thing that talks to Chalo; the map and the
dashboard read our copy, so visitors never call Chalo themselves.

    GET https://production.zophop.com/vasudha/route/chennai/<routeId>
      -> every live bus on that route variant: position, bearing, registration
         ("E" = electric, then "LF" = low floor), previous / next stop, fix time

Politeness: one variant at a time at --rate requests a second (default 1.5);
routes a rider has open on the map (--watch) are polled every 30 s, at most 16
variants at once;
variants empty three polls running are looked at every fourth cycle; nothing
between 00:30 and 04:30 IST; any 429 / 5xx pauses the whole collector for 10
minutes. The User-Agent says who we are.

Chalo's route ids are its own, so each bus is matched to one of our workings
(same route number) by where it is along that working's stops and which way it
is heading. Chalo gives no stop-by-stop predictions, so ETAs are our own: with
--model (travel_model.py) each stop-to-stop segment runs at its learned time
per metre for the hour (standing included), or at what buses of the fleet were
just seen doing on it (live probes, nowcast.py); without it, at city bus speed
plus a fixed dwell. A bus is seen only every ~13 min (route-by-route polling),
so probes come from fix pairs up to PROBE_GAP apart, and the per-bus pace used
for Delhi is left out: backtests (backtest.py, 23-24 Oct 2026) found it no help
at this fix rate. AC and Express come
from Chalo's route features, Premium from its P-prefixed route names (P570S); electric from the flags we archived per
registration from Chennai One. Output and archive are the same as
live_collector.py's, so the map, Traffic layer and dashboard carry on.
"""
import argparse
import datetime as dt
import glob
import gzip
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_collector import Collector, IST, log  # noqa: E402
import nowcast  # noqa: E402

BASE = 'https://production.zophop.com'
UA = 'MTCMap/1.0 (+https://mtc.reclaimchennai.city; civic transit map)'
SPEED = 4.4          # m/s (~16 km/h, the observed city median) for estimated ETAs
DWELL = 12           # s per stop
HOT_EVERY = 30       # s between polls of a route a rider has open on the map
PROBE_GAP = 1800     # s: longest gap between two fixes of a bus still read as one drive
HOT_MAX = 16         # route variants watched at once: at most HOT_MAX / HOT_EVERY extra requests a second


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path, headers={'Accept': 'application/json', 'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def norm(s):
    return ''.join(ch for ch in (s or '').upper() if ch.isalnum())


class ChaloCollector(Collector):
    def __init__(self, a):
        super().__init__(a)
        rs = json.load(open(a.stops))
        self.by_sn = {}
        for c, r in rs.items():
            self.by_sn.setdefault(norm(r['sn']), []).append(c)
        self.variants = {v['route_id']: v for v in json.load(open(a.routes))}
        self.fleet = self.load_fleet(a.archive)
        self.pause_until = 0
        self.stop_ids = {c: [s[0] for s in r['d'][0]['s']] for c, r in rs.items()}
        self.model = nowcast.Model(a.model) if a.model else None
        self.mlock = threading.Lock()   # poll() runs in several threads
        self.track = {}                 # reg -> (code, fix time, metres along, segment index)
        log(f'{len(self.variants)} Chalo variants, {len(self.fleet)} buses with known flags')

    @staticmethod
    def load_fleet(archive):
        """Registration -> Chennai One flags (e, l, a ...), from the archived fixes."""
        fleet = {}
        for p in sorted(glob.glob(os.path.join(archive or '', 'obs-*.jsonl*'))):
            op = gzip.open if p.endswith('.gz') else open
            try:
                with op(p, 'rt') as fh:
                    for ln in fh:
                        try:
                            r = json.loads(ln)
                            fleet[r['reg']] = r.get('f') or ''
                        except ValueError:
                            pass
            except OSError:
                pass
        return fleet

    def running_now(self):
        m = dt.datetime.now(IST)
        mins = m.hour * 60 + m.minute
        if 30 <= mins < 270:                       # 00:30-04:30: the fleet is asleep, so are we
            return []
        if time.time() < self.pause_until:
            return []
        # only route numbers with one of our workings in its service window now
        on = set(Collector.running_now(self))
        live = {sn for sn, codes in self.by_sn.items() if on.intersection(codes)}
        return [vid for vid, v in self.variants.items()
                if self.key(v['route_name']) in live or self.key(v['route_name']) is None]

    def key(self, name):
        """Our route number for a Chalo route name: as is, with the Premium "P" prefix
        (Chalo files P570S buses under 570S too), or its first word ("S100 CMRL Feeder
        Service"). None for services Chennai One does not carry (Chennai Ula, lifts)."""
        for k in (norm(name), 'P' + norm(name), norm((name or '').split(' ')[0])):
            if k in self.by_sn:
                return k
        return None

    def match(self, sn, lat, lon, bearing, halted):
        """Our working (code), next stop index k and metres along it, or None."""
        best = None
        cx = math.cos(math.radians(lat)) * 111320
        for code in self.by_sn.get(self.key(sn), []):
            ll, ds = self.stopll.get(code), self.sdist.get(code)
            if not ll or not ds or len(ll) < 2:
                continue
            for i in range(len(ll) - 1):
                (ay, ax), (by, bx) = ll[i], ll[i + 1]
                vx, vy = (bx - ax) * cx, (by - ay) * 111320
                px, py = (lon - ax) * cx, (lat - ay) * 111320
                L2 = vx * vx + vy * vy
                t = max(0.0, min(1.0, (px * vx + py * vy) / L2)) if L2 else 0.0
                off = math.hypot(px - t * vx, py - t * vy)
                if off > 250:
                    continue
                score = off
                if not halted and bearing is not None and L2:
                    h = (math.degrees(math.atan2(vx, vy)) + 360) % 360
                    if abs((h - bearing + 180) % 360 - 180) > 75:
                        score += 400                 # heading the wrong way for this working
                if best is None or score < best[0]:
                    best = (score, code, i + 1, ds[i] + t * (ds[i + 1] - ds[i]))
        return best[1:] if best and best[0] < 400 else None

    def learn(self, reg, code, k, d, fix):
        """Every segment a bus drove (or stood on) since its last fix is a live probe
        for the buses behind it."""
        tk = self.track.get(reg)
        ids, ds = self.stop_ids[code], self.sdist[code]
        if tk and tk[0] == code and 0 < fix - tk[1] <= PROBE_GAP:
            gap, moved = fix - tk[1], d - tk[2]
            if moved >= 12 and moved / gap <= 25:
                for j in range(tk[3], k):
                    if j + 1 < len(ids):
                        share = max(0.0, min(d, ds[j + 1]) - max(tk[2], ds[j])) / moved
                        if share > 0:
                            self.model.add_probe(f'{ids[j]}|{ids[j + 1]}', fix, moved / gap, gap * share)
            elif abs(moved) < 12 and k < len(ids):
                self.model.add_probe(f'{ids[k - 1]}|{ids[k]}', fix, 0.0, gap)       # standing counts too
        if not tk or fix > tk[1]:
            self.track[reg] = (code, fix, d, k - 1)

    def poll(self, vid):
        if self.model is not None:
            with self.mlock:
                self.model.reload()
        v = self.variants[vid]
        c, j = get(f'/vasudha/route/chennai/{vid}')
        if c == 429 or c >= 500:
            self.pause_until = time.time() + 600
            log(f'Chalo answered {c}: pausing 10 min')
            return
        if c != 200 or not isinstance(j, dict):
            return
        flags_route = ('a' if 'AC' in (v.get('special_features') or []) else '') + \
                      ('x' if any('xpress' in s for s in (v.get('spf') or [])) else '')
        rows = []
        for raw in j.values():
            try:
                b = json.loads(raw) if isinstance(raw, str) else raw
                lat, lon, vno = b['_latitude'], b['_longitude'], b['vNo']
            except (ValueError, KeyError, TypeError):
                continue
            # Chalo suffixes the fleet number: "E" electric, "LF" low floor, "AC"
            # (K5008ELF, K5421ELFAC = K5008, K5421)
            reg, suf = vno, set()
            for sx in ('AC', 'LF', 'E'):
                if reg.endswith(sx) and len(reg) > len(sx) + 3:
                    reg, suf = reg[:-len(sx)], suf | {sx}
            lf, ev, acb = 'LF' in suf, 'E' in suf, 'AC' in suf
            fix = int((b.get('tS') or time.time() * 1000) / 1000)
            m = self.match(v['route_name'], lat, lon, b.get('bearing'), b.get('_isHalted'))
            known = self.fleet.get(reg, '')
            if m and self.model is not None:
                code, k, d = m
                with self.mlock:
                    self.learn(reg, code, k, d, fix)
                    eta = [[i, round(t_)] for i, t_ in self.model.eta(self.stop_ids[code], self.sdist[code], d, k, fix)]
            elif m:
                code, k, d = m
                ds = self.sdist[code]
                eta = [[i, fix + (ds[i] - d) / SPEED + DWELL * (i - k)] for i in range(k, min(len(ds), k + 25))]
            else:
                # off our workings, or a service Chennai One does not carry: still a bus on the
                # road, shown under Chalo's route name ("c:" codes have no stops or ETAs)
                cands = self.by_sn.get(self.key(v['route_name'])) or ['c:' + v['route_name'].strip()]
                code, eta = cands[0], []
            flags = ''.join(sorted(set(flags_route + ('l' if lf or 'l' in known else '') +
                                       ('e' if ev or 'e' in known else '') +
                                       ('a' if acb or 'a' in known else '') +
                                       # MTC Premium is the OHM-run Switch EiV12 fleet: electric AC
                                       ('pe' if re.match(r'P\d', v['route_name']) else ''))))
            row = [reg, code, round(lat, 5), round(lon, 5), b.get('bearing') or 0, fix, flags, eta]
            if not code.startswith('c:') and self.key(v['route_name']) == 'P' + norm(v['route_name']):
                row.append(v['route_name'].strip())   # matched by alias (570S on P570S's stops): show its own number
            rows.append(row)
        if self.a.archive:
            self.record(rows)
        with self.lock:
            self.state[vid] = (time.time(), rows)
            self.empty[vid] = 0 if rows else self.empty.get(vid, 0) + 1


def hot_loop(c, path):
    """Routes riders have open on the map (watch.json, written by report_api.py)
    are polled every HOT_EVERY s instead of once a cycle."""
    last = {}
    while True:
        time.sleep(5)
        try:
            w = json.load(open(path))
        except (OSError, ValueError):
            continue
        now = time.time()
        sns = {norm(k) for k, t in (w.get('sn') or {}).items() if now - t < (w.get('ttl') or 150)}
        if not sns or not c.running_now():          # night, or paused after a 429
            continue
        vids = [vid for vid, v in c.variants.items()
                if (c.key(v['route_name']) or norm(v['route_name'])) in sns and c.empty.get(vid, 0) < 3][:HOT_MAX]
        for vid in vids:
            if now - last.get(vid, 0) >= HOT_EVERY:
                last[vid] = time.time()
                c.poll(vid)
                time.sleep(1.0 / c.a.rate)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stops', required=True)
    ap.add_argument('--timings', required=True)
    ap.add_argument('--stop-dist', required=True)
    ap.add_argument('--routes', required=True, help="Chalo's route list (autocomplete/route, str=)")
    ap.add_argument('--archive', default='')
    ap.add_argument('--out', required=True)
    ap.add_argument('--rate', type=float, default=1.5)
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--write', type=float, default=10)
    ap.add_argument('--model', default='', help='model.json from travel_model.py: learned segment times (nowcast ETAs)')
    ap.add_argument('--watch', default='', help='watch.json from report_api.py: routes riders have open')
    a = ap.parse_args()
    if a.archive:
        os.makedirs(a.archive, exist_ok=True)
    random.seed()
    c = ChaloCollector(a)
    if a.watch:
        threading.Thread(target=hot_loop, args=(c, a.watch), daemon=True).start()
    c.run()


if __name__ == '__main__':
    main()
