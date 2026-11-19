#!/usr/bin/env python3
"""Chalo's live MTC bus positions, polled in a process of their own, for mtcits_collector.py to merge.

    chalo_poller.py --routes chalo_routes.json --out live/chalo_raw.json [--rate 6] [--workers 6]
                    [--watch reports/watch.json] [--feed live/vehicles.json]

Chalo (the Chennai One app's backend, production.zophop.com, no account) answers one route variant a call
with every bus on it: position, bearing, halted flag and GPS time. Its fixes are ~11 s old at the median
(17 Nov 2026) against minutes for MTC's per-direction answers, so it is the cheap way to keep positions
fresh. It used to run as threads inside mtcits_collector.py, where every network step queued for the GIL
behind the MTC threads: 1-1.8 s a request instead of 0.08 s, so a 4-6 req/s budget ran at 1.4-2.4.

Which variant next: buses on it x seconds since asked, a bus the map has no fresh MTC fix for (--feed:
absent, older than STALE_S, or itself from Chalo) counting three; a variant a rider has open (--watch)
every HOT_S; empty variants every 10-40 min; never one twice within MIN_S. Nothing 00:30-04:30 IST. A 429
or 5xx pauses everything for 10 min. Output (atomic, every WRITE_S): {"t", "b": [[vid, reg, lat, lon,
bearing, halted, fix], ...]}, buses with a fix in the last KEEP_S; regs as Chalo sends them (suffixes kept).
"""
import argparse
import datetime as dt
import http.client
import json
import os
import ssl
import threading
import time

HOST, PATH = 'production.zophop.com', '/vasudha/route/chennai/'
UA = 'ReclaimChennai-bus-map/1.0 (+https://maps.reclaimchennai.city)'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MIN_S, HOT_S, STALE_S, KEEP_S, WRITE_S = 45, 20, 300, 1200, 5
TLS = ssl.create_default_context()


def log(msg):
    print(dt.datetime.now(IST).strftime('%H:%M:%S'), msg, flush=True)


_tl = threading.local()


def get(vid):
    """One variant, over this thread's kept-alive connection: a new TCP connection and TLS handshake for every
    request (urlopen) was 46% of the poller's time and held it at 2.2 req/s (17 Nov)."""
    for attempt in (0, 1):
        c = getattr(_tl, 'conn', None)
        if c is None:
            c = _tl.conn = http.client.HTTPSConnection(HOST, timeout=20, context=TLS)
        try:
            c.request('GET', PATH + vid, headers={'Accept': 'application/json', 'User-Agent': UA})
            r = c.getresponse()
            body = r.read()
            if r.status != 200:
                return r.status, None
            return 200, json.loads(body or b'null')
        except (http.client.HTTPException, OSError, ValueError):
            c.close()
            _tl.conn = None                       # the server closed it: open a new one, once
    return 0, None


def norm(s):
    return ''.join(ch for ch in (s or '').upper() if ch.isalnum())


class Poller:
    def __init__(self, a):
        self.a = a
        self.var = {v['route_id']: {'sn': norm(v['route_name']), 'polled': 0.0, 'n': 1, 'empty': 0, 'w': 1.0}
                    for v in json.load(open(a.routes))}
        self.bus = {}                 # reg -> [vid, reg, lat, lon, bearing, halted, fix]
        self.lock = threading.Lock()
        self.pause = 0.0
        self.hot, self.fresh, self.side_t = set(), {}, 0.0
        self.stat = {'asked': 0, 'buses': 0, 'failed': 0}

    def side(self):
        """Every 15 s: routes riders have open, and which buses the map has a fresh MTC fix for."""
        now = time.time()
        if now - self.side_t < 15:
            return
        self.side_t = now
        try:
            w = json.load(open(self.a.watch)) if self.a.watch else {}
            self.hot = {norm(s) for s, t in (w.get('sn') or {}).items() if now - t < (w.get('ttl') or 150)}
        except (OSError, ValueError):
            self.hot = set()
        try:
            v = json.load(open(self.a.feed))['v'] if self.a.feed else []
            self.fresh = {r[0]: r[5] for r in v if 'h' not in r[6]}
        except (OSError, ValueError, KeyError):
            pass

    def pick(self, now):
        best = None
        with self.lock:
            for vid, c in self.var.items():
                since = now - c['polled']
                if since < MIN_S:
                    continue
                hot = c['sn'] in self.hot or 'P' + c['sn'] in self.hot
                if hot and since >= HOT_S and c['empty'] < 3:
                    sc = 1e9 + since
                elif c['n']:
                    sc = since * c['w']
                elif since >= 600 * (1 + min(c['empty'], 3)):
                    sc = since / 10
                else:
                    continue
                if best is None or sc > best[0]:
                    best = (sc, vid)
            if best:
                self.var[best[1]]['polled'] = now
        return best and best[1]

    def one(self, vid):
        c, j = get(vid)
        now = time.time()
        if c == 429 or c >= 500:
            self.pause = now + 600
            log(f'Chalo answered {c}: paused 10 min')
            return
        if c != 200 or not isinstance(j, dict):
            self.stat['failed'] += 1
            return
        rows, w = [], 0.0
        for raw in j.values():
            try:
                b = json.loads(raw) if isinstance(raw, str) else raw
                lat, lon, reg, fix = b['_latitude'], b['_longitude'], b['vNo'], int(b['tS'] / 1000)
            except (ValueError, KeyError, TypeError):
                continue
            if not (12.3 < lat < 13.7 and 79.5 < lon < 80.5) or fix > now + 120:
                continue
            rows.append([vid, reg, lat, lon, b.get('bearing'), bool(b.get('_isHalted')), fix])
            base = reg
            for sx in ('AC', 'LF', 'E'):
                if base.endswith(sx) and len(base) > len(sx) + 3:
                    base = base[:-len(sx)]
            w += 1.0 if now - self.fresh.get(base, 0) < STALE_S else 3.0
        with self.lock:
            v = self.var[vid]
            v['n'], v['w'], v['empty'] = len(rows), w, 0 if rows else v['empty'] + 1
            for r in rows:
                p = self.bus.get(r[1])
                if p is None or r[6] >= p[6]:
                    self.bus[r[1]] = r
        self.stat['asked'] += 1
        self.stat['buses'] += len(rows)

    def worker(self):
        gap = self.a.workers / self.a.rate
        while True:
            t0 = time.time()
            h = dt.datetime.now(IST)
            if t0 < self.pause or (0, 30) <= (h.hour, h.minute) < (4, 30):
                time.sleep(20)
                continue
            vid = self.pick(t0)
            if vid:
                try:
                    self.one(vid)
                except Exception as e:
                    log(f'{vid}: {type(e).__name__} {str(e)[:100]}')
            time.sleep(max(0.05, gap - (time.time() - t0)))

    def run(self):
        for _ in range(self.a.workers):
            threading.Thread(target=self.worker, daemon=True).start()
        last_log = time.time()
        while True:
            time.sleep(WRITE_S)
            self.side()
            now = time.time()
            with self.lock:
                for reg in [r for r, b in self.bus.items() if now - b[6] > KEEP_S]:
                    del self.bus[reg]
                out = list(self.bus.values())
            tmp = self.a.out + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'t': int(now), 'b': out}, fh, separators=(',', ':'))
            os.replace(tmp, self.a.out)
            if now - last_log >= 300:
                ages = sorted(now - b[6] for b in out)
                p50 = round(ages[len(ages) // 2]) if ages else None
                withb = sum(1 for v in self.var.values() if v['n'])
                log(f"{len(out)} buses, fix age p50 {p50} s, {self.stat['asked'] / (now - last_log):.2f} req/s, "
                    f"{self.stat['buses']} bus answers, {self.stat['failed']} failed, {withb} variants with buses")
                self.stat = {'asked': 0, 'buses': 0, 'failed': 0}
                last_log = now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--routes', required=True, help="Chalo's variant list (chalo_routes.json)")
    ap.add_argument('--out', required=True)
    ap.add_argument('--rate', type=float, default=6.0, help='requests per second to Chalo, all workers together')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--watch', default='', help='watch.json from report_api.py')
    ap.add_argument('--feed', default='', help="the map's vehicles.json (which buses MTC has fresh)")
    Poller(ap.parse_args()).run()


if __name__ == '__main__':
    main()
