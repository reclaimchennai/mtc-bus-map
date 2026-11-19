#!/usr/bin/env python3
"""Live MTC fleet for the map: polls Chennai One route by route, writes one small JSON.

Chennai One answers live positions one route per call
(`POST /multimodal/routeServiceability`, see ENDPOINTS.md) — extra routes in the
body are ignored — so the fleet is assembled by cycling through the routes that
are running now, at a fixed polite rate. Each bus record carries the app's own
predicted arrival at every remaining stop, which is what lets the map move a bus
smoothly between polls and answer "when is the next bus at this stop".

    live_collector.py --stops route_stops.json --timings timings.json --out vehicles.json

Session: CHENNAI_ONE_TOKEN / CHENNAI_ONE_SID in the environment (systemd
EnvironmentFile, chmod 600). Never logged.

Output (rewritten atomically every --write seconds):
  {"t": now, "polled": n routes in the last cycle, "cycle": seconds per cycle,
   "v": [[reg, routeCode, lat, lon, bearing, fixEpoch, flags, [[stopIdx, etaEpoch], ...]], ...]}
  stopIdx indexes route_stops.json[routeCode].d[0].s; flags: a=AC e=electric l=low floor
  x=express p=premium u=waiting for its next trip (fmt 2: eta is [i, dt, ...], dt after the fix);
  an optional 9th field is the route number to show when it is not the working's own

With --archive DIR it also keeps, per IST day:
  obs-YYYY-MM-DD.jsonl    every new GPS fix: {t, reg, r, la, lo, k (next stop idx), f}
  speed-YYYY-MM-DD.jsonl  observed speed between two consecutive fixes of one bus on
                          one route: {t, r, i0, i1, v km/h, dt s} — distance is along
                          the route (stop_dist.json), so it is road speed including
                          dwell at stops, the speed a rider actually gets.
"""
import argparse
import datetime as dt
import gzip
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = 'https://api.moving.tech/pilot/app/v2'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ETA_KEEP = 25


def headers():
    return {'token': os.environ['CHENNAI_ONE_TOKEN'], 'session_id': os.environ['CHENNAI_ONE_SID'],
            'content-type': 'application/json', 'accept-encoding': 'gzip',
            'x-bundle-version': '0.1.7', 'x-client-version': '3.6.0', 'x-config-version': '0.0.1',
            'x-device': 'google/sdk_gphone64_arm64/Android v15/goldfish_arm64/Handset',
            'x-package': 'in.mobility.cumta', 'x-rn-version': '--', 'user-agent': 'okhttp/4.12.0'}


def api(path, body=None, timeout=40):
    req = urllib.request.Request(BASE + path, method='POST' if body is not None else 'GET',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get('content-encoding') == 'gzip':
                raw = gzip.decompress(raw)
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def log(*a):
    print(dt.datetime.now(IST).strftime('%H:%M:%S'), *a, flush=True)


def hm(s):
    h, m = s.split(':')[:2]
    return int(h) * 60 + int(m)


class Collector:
    def __init__(self, a):
        self.a = a
        rs = json.load(open(a.stops))
        self.idx = {c: {s[0]: i for i, s in enumerate(r['d'][0]['s'])} for c, r in rs.items()}
        self.stopll = {c: [(s[2], s[3]) for s in r['d'][0]['s']] for c, r in rs.items()}
        self.sdist = json.load(open(a.stop_dist)) if a.stop_dist and os.path.exists(a.stop_dist) else {}
        self.last = {}           # reg -> (code, fix, along metres, seg idx)
        self.alock = threading.Lock()
        tm = json.load(open(a.timings)) if os.path.exists(a.timings) else {}
        # service window per working, minutes after midnight IST
        self.window = {c: (hm(t['first']), hm(t['last'])) for c, t in tm.items() if t.get('first')}
        self.state = {}          # code -> (polled_epoch, [vehicle rows])
        self.empty = {}          # code -> consecutive polls with no bus
        self.lock = threading.Lock()
        self.cycle_n, self.cycle_s = 0, 0.0
        self.auth_fail = 0

    def running_now(self):
        now = dt.datetime.now(IST)
        m = now.hour * 60 + now.minute
        out = []
        for c in self.idx:
            f, l = self.window.get(c, (0, 24 * 60))
            # a bus is on the road from its first departure until it finishes the last
            # trip; give it 2 h past the last departure (and wrap past midnight)
            l2 = l + 120
            if f - 10 <= m <= l2 or (l2 >= 1440 and m <= l2 - 1440):
                out.append(c)
        return out

    def poll(self, code):
        c, j = api('/multimodal/routeServiceability?', {'routeCodes': [{'legOrder': 0, 'routeCodes': [code]}]})
        if c in (401, 403):
            self.auth_fail += 1
            return
        if c != 200 or not j:
            return
        self.auth_fail = 0
        rows = []
        for rw in (j.get('legs') or [{}])[0].get('routeWithLiveVehicles') or []:
            rc = rw.get('routeCode') or code
            ix = self.idx.get(rc)
            if ix is None:
                continue
            for v in rw.get('liveVehicles') or []:
                p = v.get('position') or {}
                if not (p.get('lat') and p.get('lon') and v.get('number')):
                    continue
                try:
                    fix = int(dt.datetime.strptime(v['locationUTCTimestamp'], '%Y-%m-%dT%H:%M:%SZ')
                              .replace(tzinfo=dt.timezone.utc).timestamp())
                except Exception:
                    fix = int(time.time())
                sub = v.get('serviceSubTypes') or []
                tier = v.get('serviceTierType') or ''
                flags = ('a' if tier == 'AC' else '') + ('e' if 'EV' in sub else '') + \
                        ('l' if 'LF' in sub else '') + ('x' if tier == 'EXPRESS' else '') + \
                        ('p' if tier == 'PREMIUM' else '') + ('u' if v.get('isUpcomingTrip') else '')
                eta = [[ix[e['stop_id']], e['arrival_time_unix']] for e in v.get('eta') or []
                       if e.get('stop_id') in ix and e.get('arrival_time_unix')]
                rows.append([v['number'], rc, round(p['lat'], 5), round(p['lon'], 5),
                             v.get('bearing') or 0, fix, flags, eta])
        if self.a.archive:
            self.record(rows)
        with self.lock:
            self.state[code] = (time.time(), rows)
            self.empty[code] = 0 if rows else self.empty.get(code, 0) + 1

    def along(self, code, lat, lon, k):
        """Metres along the route of a fix whose next stop is k, and the index of
        the segment it is on: the fix is projected onto the chord from stop k-1
        to stop k and placed that far between their along-route distances."""
        ds, ll = self.sdist.get(code), self.stopll.get(code)
        if not ds or not ll or k >= len(ds):
            return None
        if k == 0:
            return ds[0], 0
        (ay, ax), (by, bx) = ll[k - 1], ll[k]
        cx = math.cos(math.radians(lat)) * 111320
        vx, vy = (bx - ax) * cx, (by - ay) * 111320
        px, py = (lon - ax) * cx, (lat - ay) * 111320
        L2 = vx * vx + vy * vy
        t = max(0.0, min(1.0, (px * vx + py * vy) / L2)) if L2 else 0.0
        return ds[k - 1] + t * (ds[k] - ds[k - 1]), k - 1

    def record(self, rows):
        day = dt.datetime.now(IST).strftime('%Y-%m-%d')
        obs, spd = [], []
        with self.alock:
            for reg, code, lat, lon, _b, fix, flags, eta, *_ in rows:
                prev = self.last.get(reg)
                if prev and prev[1] >= fix:
                    continue                                   # same fix seen via another route
                k = eta[0][0] if eta else None
                obs.append({'t': fix, 'reg': reg, 'r': code, 'la': lat, 'lo': lon, 'k': k, 'f': flags})
                pos = self.along(code, lat, lon, k) if k is not None else None
                if pos and prev and prev[0] == code and prev[2] is not None:
                    dtt, dd = fix - prev[1], pos[0] - prev[2]
                    v = dd / dtt * 3.6 if dtt else 0
                    # dd >= 30: a parked or laid-over bus is not traffic
                    if 60 <= dtt <= 2400 and dd >= 30 and v <= 70 and pos[1] >= prev[3]:
                        spd.append({'t': fix, 'r': code, 'i0': prev[3], 'i1': pos[1], 'v': round(v, 1), 'dt': dtt})
                self.last[reg] = (code, fix, pos[0] if pos else None, pos[1] if pos else None)
            for name, recs in (('obs', obs), ('speed', spd)):
                if recs:
                    with open(os.path.join(self.a.archive, f'{name}-{day}.jsonl'), 'a') as fh:
                        fh.write(''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in recs))

    def snapshot(self):
        now = time.time()
        best = {}
        with self.lock:
            items = list(self.state.values())
        for polled, rows in items:
            for r in rows:
                # a bus is gone once its last predicted stop is well past, or the
                # data is older than a cycle and a half
                if r[7] and r[7][-1][1] < now - 300:
                    continue
                if now - polled > max(1800, 1.5 * self.cycle_s):
                    continue
                prev = best.get(r[0])
                if prev is None or r[5] > prev[5]:
                    best[r[0]] = r
        # fmt 2: eta as a flat [i, dt, i, dt ...] with dt seconds after the fix, and
        # only the next ETA_KEEP stops — about half the bytes phones download every poll
        out = []
        for r in best.values():
            flat = []
            for i, t in [e for e in r[7] if e[1] >= now - 120][:ETA_KEEP]:
                flat += [i, int(t - r[5])]
            out.append(r[:7] + [flat] + r[8:9])           # [8]: route number shown, when not the working's own
        return {'t': int(now), 'fmt': 2, 'polled': self.cycle_n, 'cycle': round(self.cycle_s), 'v': out}

    def writer(self):
        while True:
            snap = self.snapshot()
            try:
                tmp = self.a.out + '.tmp'
                with open(tmp, 'w') as fh:
                    json.dump(snap, fh, separators=(',', ':'))
                os.replace(tmp, self.a.out)
            except OSError as e:                       # a full disk must not end this thread for good
                log(f'vehicles.json not written: {e}')
            time.sleep(self.a.write)

    def run(self):
        threading.Thread(target=self.writer, daemon=True).start()
        gap = 1.0 / self.a.rate
        while True:
            codes = self.running_now()
            # routes that came back empty three times running are probably not out
            # yet; look at them every third cycle instead of every cycle
            codes = [c for c in codes if self.empty.get(c, 0) < 3 or random.random() < 1 / 3]
            random.shuffle(codes)
            t0 = time.time()
            sem = threading.Semaphore(self.a.workers)

            def go(c):
                try:
                    self.poll(c)
                finally:
                    sem.release()
            for c in codes:
                sem.acquire()
                threading.Thread(target=go, args=(c,), daemon=True).start()
                time.sleep(gap)
                if self.auth_fail > 20:
                    log('session rejected — refresh CHENNAI_ONE_TOKEN/SID; pausing 10 min')
                    time.sleep(600)
                    self.auth_fail = 0
            for _ in range(self.a.workers):
                sem.acquire()
            for _ in range(self.a.workers):
                sem.release()
            self.cycle_n, self.cycle_s = len(codes), time.time() - t0
            snap = self.snapshot()
            log(f'cycle {len(codes)} routes {self.cycle_s:.0f}s, {len(snap["v"])} buses live')
            if not codes:
                time.sleep(60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stops', required=True)
    ap.add_argument('--timings', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--rate', type=float, default=4.0, help='requests per second')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--write', type=float, default=20)
    ap.add_argument('--stop-dist', default='', help='stop_dist.json for observed speeds')
    ap.add_argument('--archive', default='', help='directory for the daily obs/speed archive')
    a = ap.parse_args()
    if not os.environ.get('CHENNAI_ONE_TOKEN'):
        sys.exit('CHENNAI_ONE_TOKEN not set')
    if a.archive:
        os.makedirs(a.archive, exist_ok=True)
    Collector(a).run()


if __name__ == '__main__':
    main()
