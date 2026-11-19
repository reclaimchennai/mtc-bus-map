#!/usr/bin/env python3
"""Fares for every class, from Chennai One's own fare search.

    fare_harvest.py PLAN.json OUTDIR [--rate 2] [--types METRO,SUBWAY,BUS]

    POST /frfs/search?vehicleType="BUS"&integratedBppConfigId=<ibc>&
         {"fromStationCode", "toStationCode", "quantity": 1, "routeCode"}

One search prices a stop pair in every class the route runs — for buses
Ordinary, Small Bus, Express, Small Bus Express, Deluxe and AC at once, for
suburban trains first and second class. A search only quotes; nothing is
booked. The plan (dataset/fare_plan_<date>.json) lists, per route:
  BUS     first stop -> every later stop (fares rise by stage from the origin,
          so this is the route's whole stage table)
  METRO / SUBWAY  every station pair the route serves, each pair once

Writes OUTDIR/fares-<VT>.jsonl, one line per pair:
  {"vt","route","from","to","q":[[tierType, tierName, price, {category: price}], ...]}
Resumable: pairs already in the file are skipped. Promotional offers (UPI
cashback etc.) are left out — they are payment-app promotions, not fares.
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_collector import api, log  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('plan')
    ap.add_argument('out')
    ap.add_argument('--rate', type=float, default=2)
    ap.add_argument('--types', default='METRO,SUBWAY,BUS')
    ap.add_argument('--workers', type=int, default=1)
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    os.makedirs(a.out, exist_ok=True)
    for vt in a.types.split(','):
        path = os.path.join(a.out, f'fares-{vt}.jsonl')
        done = set()
        if os.path.exists(path):
            for line in open(path):
                try:
                    r = json.loads(line)
                    done.add((r['route'], r['from'], r['to']))
                except ValueError:
                    pass
        todo = [(r['route'], r['ibc'], p[0], p[1]) for r in plan.get(vt, []) for p in r['pairs']
                if (r['route'], p[0], p[1]) not in done]
        log(f'{vt}: {len(done)} held, {len(todo)} to fetch')
        st = {'fails': 0, 'auth': 0, 'n': 0}
        lock, gap = threading.Lock(), 1 / a.rate
        nxt = [time.monotonic()]

        def one(item):
            route, ibc, s0, s1 = item
            if st['auth'] > 20:
                return
            with lock:                                  # shared rate limit across workers
                t = max(time.monotonic(), nxt[0]); nxt[0] = t + gap
            time.sleep(max(0, t - time.monotonic()))
            body = {'fromStationCode': s0, 'toStationCode': s1, 'quantity': 1, 'routeCode': route}
            c, j = api(f'/frfs/search?vehicleType="{vt}"&integratedBppConfigId={ibc}&', body)
            with lock:
                if c in (401, 403):
                    st['auth'] += 1
                    return
                if c != 200 or not j:
                    st['fails'] += 1
                    return
                st['auth'] = 0
                q = [[x.get('serviceTierType'), x.get('serviceTierName'), x.get('price'),
                      {cat['categoryName']: (cat.get('categoryPrice') or {}).get('amount') for cat in x.get('categories') or []}]
                     for x in j.get('quotes') or [] if (x.get('routeCode') in (None, route))]
                fh.write(json.dumps({'vt': vt, 'route': route, 'from': s0, 'to': s1, 'q': q}, separators=(',', ':')) + '\n')
                st['n'] += 1
                if st['n'] % 500 == 0:
                    fh.flush()
                    log(f"{vt}: {st['n']}/{len(todo)} ({st['fails']} failed)")

        with open(path, 'a') as fh, ThreadPoolExecutor(a.workers) as ex:
            list(ex.map(one, todo))
        if st['auth'] > 20:
            log('session rejected — stopping; rerun after refreshing session.env')
            return
        fails = st['fails']
        log(f'{vt}: done, {fails} failed')


if __name__ == '__main__':
    main()
