#!/usr/bin/env python3
"""How well each method predicts where a bus will be, on a day it did not learn from.

    backtest.py --stops route_stops.json --stop-dist stop_dist.json --archive DIR
                --model model.json --day YYYY-MM-DD [--speeds speeds.json] [--sample 0.2]

For every fix of the test day, each method predicts the bus's position along its
route H seconds later (H = 30, 60, 120, 300); the truth is the bus's next fix
nearest that time. Error = |predicted - true| metres along the route. Methods:

  hold        the bus stays at its last fix (what showing raw GPS does)
  constant    --const m/s and s at every stop (the collector before learning;
              Delhi 4.2 m/s + 15 s, Chennai's Chalo collector 4.4 m/s + 12 s)
  hourly      the live-speeds file's segment speeds incl. stops (the collector now)
  nowcast     travel model: pooled time per metre (standing included), live probes
              from the whole fleet, per-bus pace (--no-pace / --no-probes / --no-live
              to take them out; --max-gap for sparse feeds such as Chalo's)

The whole fleet is replayed in time order, so live probes reach other buses when
they happened (the Delhi copy fed them bus by bus).
"""
import argparse
import bisect
import collections
import json
import random
import statistics
import time

import nowcast
import travel_model as tm

H = (30, 60, 120, 300)
CONST = (4.2, 15)    # the collector before learning: m/s and s per stop


def main():
    global H
    ap = argparse.ArgumentParser()
    ap.add_argument('--stops', required=True)
    ap.add_argument('--stop-dist', required=True)
    ap.add_argument('--archive', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--day', required=True)
    ap.add_argument('--speeds', default='')
    ap.add_argument('--sample', type=float, default=0.2)
    ap.add_argument('--max-gap', type=int, default=180, help='longest fix gap that feeds probes and pace (s)')
    ap.add_argument('--horizons', default=','.join(map(str, H)), help='seconds ahead, comma-separated')
    ap.add_argument('--const', default=','.join(map(str, CONST)), help='constant method: m/s,dwell s')
    ap.add_argument('--no-live', action='store_true', help='nowcast without live probes or pace (model alone)')
    ap.add_argument('--no-probes', action='store_true', help='nowcast without live probes (pace only)')
    ap.add_argument('--no-pace', action='store_true', help='nowcast without per-bus pace (probes only)')
    ap.add_argument('--pace-gap', type=int, default=0, help='longest fix gap that updates pace (s; default --max-gap)')
    a = ap.parse_args()
    H = tuple(int(x) for x in a.horizons.split(','))
    cv, cd = (float(x) for x in a.const.split(','))
    random.seed(7)
    probe = (lambda *x: None) if a.no_probes else None
    routes = tm.Routes(a.stops, a.stop_dist)
    M = nowcast.Model(a.model)
    probe = probe or M.add_probe
    pace = nowcast.Pace()
    hourly = json.load(open(a.speeds)) if a.speeds else {}

    def hourly_v(key, h):
        row = (hourly.get('s') or {}).get(key)
        v = row[2 + h] if row and row[2 + h] > 0 else ((hourly.get('city') or {}).get('h') or [None] * 24)[h]
        return (v / 3.6) if v and v > 2 else 4.2

    # pass 1: each bus's day as a list of (t, code, s, i), on its route
    tracks = collections.defaultdict(list)
    last = {}
    path = next(p for d, p in tm.day_files(a.archive, 'obs', 1, a.day))
    for r in tm.lines(path):
        prev = last.get(r['reg'])
        near = prev[3] if prev and prev[1] == r['r'] else None
        pr = routes.project(r['r'], r['la'], r['lo'], near)
        if not pr or pr[2] > 150:
            continue
        rec = (r['t'], r['r'], pr[0], pr[1])
        tracks[r['reg']].append(rec)
        last[r['reg']] = rec
    print(f'{len(tracks)} buses on {a.day}', flush=True)

    err = {m: {h: [] for h in H} for m in ('hold', 'constant', 'hourly', 'nowcast')}
    t0 = time.time()
    # replay the whole fleet in time order, so every bus's probes reach the others when
    # they happened; predictions are scored only for the sampled buses
    picked = {reg for reg in tracks if random.random() <= a.sample}
    times_of = {reg: [x[0] for x in tr] for reg, tr in tracks.items()}
    order = sorted((x[0], reg, n) for reg, tr in tracks.items() for n, x in enumerate(tr))
    for _t, reg, n in order:
        tr, times = tracks[reg], times_of[reg]
        t, code, s, i = tr[n]
        ids, ds = routes.ids[code], routes.ds[code]
        hour = time.gmtime(t + 19800).tm_hour
        # live probes and pace learn from the past only (this fix and before)
        if n and not a.no_live:
            pt, pc, ps, pi = tr[n - 1]
            if pc == code and 0 < t - pt <= a.max_gap and s - ps >= 12:
                v, span = (s - ps) / (t - pt), s - ps
                for j in range(pi, i + 1):
                    if j + 1 < len(ids):
                        share = max(0.0, min(s, ds[j + 1]) - max(ps, ds[j])) / span
                        if share > 0:
                            probe(f'{ids[j]}|{ids[j + 1]}', t, v, (t - pt) * share)
                if not a.no_pace and t - pt <= (a.pace_gap or a.max_gap):
                    pace.update(reg, M.expected_time(ids, ds, ps, s, hour, t), t - pt)
            elif pc == code and 0 < t - pt <= a.max_gap and abs(s - ps) < 12 and i + 1 < len(ids):
                probe(f'{ids[i]}|{ids[i + 1]}', t, 0.0, t - pt)       # standing counts too
        for h in (H if reg in picked else ()):
            j = bisect.bisect_left(times, t + h)
            if j >= len(tr) or abs(tr[j][0] - (t + h)) > max(15, h // 10) or tr[j][1] != code:
                continue
            truth = tr[j][2]
            if truth < s - 20:                        # trip ended / turned: skip
                continue
            tt = tr[j][0]
            err['hold'][h].append(abs(truth - s))
            # constant speed + a fixed dwell a stop
            here, t_ = s, t
            k = next((q for q in range(1, len(ds)) if ds[q] > s), len(ds) - 1)
            while k < len(ds):
                need = (ds[k] - here) / cv
                if t_ + need >= tt:
                    here += cv * (tt - t_); break
                t_ += need + cd; here = ds[k]; k += 1
                if t_ >= tt:
                    break
            err['constant'][h].append(abs(truth - here))
            # hourly incl. stops, no dwell
            here, t_ = s, t
            k = next((q for q in range(1, len(ds)) if ds[q] > s), len(ds) - 1)
            while k < len(ds):
                v = hourly_v(f'{ids[k - 1]}|{ids[k]}', hour)
                need = (ds[k] - here) / v
                if t_ + need >= tt:
                    here += v * (tt - t_); break
                t_ += need + 15; here = ds[k]; k += 1
            err['hourly'][h].append(abs(truth - here))
            err['nowcast'][h].append(abs(truth - M.advance(ids, ds, s, t, tt, pace.get(reg))))
    print(f'evaluated in {time.time() - t0:.0f}s')
    print('median / p80 error in metres along the route, predicting ahead by:')
    print('method      ' + ''.join(f'{h:>16}s' for h in H))
    for m in err:
        row = ''
        for h in H:
            L = sorted(err[m][h])
            row += f'{(str(round(L[len(L) // 2])) + " / " + str(round(L[int(len(L) * .8)]))) if L else "-":>17}'
        print(f'{m:<12}{row}')
    print('samples per horizon:', {h: len(err["nowcast"][h]) for h in H})


if __name__ == '__main__':
    main()
