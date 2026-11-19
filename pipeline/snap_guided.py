#!/usr/bin/env python3
"""Re-snap bus routes, guided by the operator's own path.

    CHENNAI_ONE_HARVEST=harvest_20261017 CHENNAI_ONE_SNAPSHOT=pt_data_20261017.json \
    python3 snap_guided.py --from snap_cache_20261017 --to snap_cache_guided [--workers 3] [--codes a,b]

Routing stop to stop takes the shortest roads, and where CUMTA puts a stop a
little off the main road, OSRM dives into side streets to reach it and back
out — 121A and 170CET near the Madhavaram Milk Colony gate ran ~1 km of interior
streets the bus never uses (field report, 2026-10-20). The route detail's
`waypoints` are the operator's own path and stay on the main road, but they
are not reliable end to end (only 1,461 of 3,716 pass a whole-route check).

So the guidance is per leg: where the operator path passes within LEG_OFF_M of
both stops of a leg, in running order, via-points are taken along it every
VIA_EVERY_M and OSRM is asked to route through them. The result is still
snapped to the road network, but on the bus's road. Legs the operator path does
not cover are routed stop to stop as before. A guided route is kept only if
it is no longer than the plain one (+2 %) — guidance must remove detours, never
add them.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from shapely.geometry import LineString, Point
from shapely.ops import transform
import pyproj

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snap_routes as sr  # noqa: E402

TO_M = pyproj.Transformer.from_crs(4326, 32644, always_xy=True).transform
TO_LL = pyproj.Transformer.from_crs(32644, 4326, always_xy=True).transform
LEG_OFF_M, VIA_EVERY_M, VIA_END_M = 60, 350, 120
LADDER = [(100, 40), (500, 120), (2000, 400), (None, None)]   # (stop radius, via radius)


def heading(W, d):
    a, b = W.interpolate(max(0, d - 25)), W.interpolate(min(W.length, d + 25))
    return (math.degrees(math.atan2(b.x - a.x, b.y - a.y)) + 360) % 360


def plan(stops, wps):
    """Stops interleaved with via-points; returns (points, guided legs).

    The stops themselves are the real trouble: at a junction OSRM attaches a stop
    to whichever road is nearest — MMC Main Gate to Moolachatram Main Road, Arul
    Nagar to Milk Colony Road — and the line is dragged in and out of it. A stop
    the operator path passes within LEG_OFF_M is therefore routed from its
    projection onto that path, with the path's heading as an OSRM bearing, so it
    can only attach to the road the bus is driving along."""
    pts = [dict(s, _stop=True) for s in stops]
    if len(wps) < 3:
        return pts, 0
    W = transform(TO_M, LineString([(p['lon'], p['lat']) for p in wps]))
    proj = []
    for i, s in enumerate(stops):
        p = transform(TO_M, Point(s['lon'], s['lat']))
        d = W.project(p)
        off = W.interpolate(d).distance(p)
        proj.append((d, off))
        if off < LEG_OFF_M:
            x, y = W.interpolate(d).coords[0]
            lon, lat = TO_LL(x, y)
            pts[i].update(lon=lon, lat=lat, _bearing=heading(W, d))
    out, guided = [pts[0]], 0
    for i in range(len(stops) - 1):
        (d0, o0), (d1, o1) = proj[i], proj[i + 1]
        if o0 < LEG_OFF_M and o1 < LEG_OFF_M and d1 - d0 > 2 * VIA_END_M:
            k, any_ = d0 + VIA_EVERY_M, False
            while k < d1 - VIA_END_M:
                x, y = W.interpolate(k).coords[0]
                lon, lat = TO_LL(x, y)
                out.append({'lon': lon, 'lat': lat, '_stop': False, '_bearing': heading(W, k)})
                k += VIA_EVERY_M; any_ = True
            guided += any_
        out.append(pts[i + 1])
    return out, guided


def route(osrm, pts):
    """One OSRM pass over all points (chunked); stop-to-stop legs rebuilt from via legs."""
    for rs, rv in LADDER:
        full, legs_all, ok = [], [], True
        i = 0
        while i < len(pts) - 1:
            c = pts[i:i + sr.CHUNK]
            coords = ';'.join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in c)
            url = f"{osrm}/route/v1/driving/{urllib.parse.quote(coords, safe=',;')}?geometries=geojson&overview=full&steps=true"
            if rs is not None:
                url += '&radiuses=' + ';'.join(str(rs if p['_stop'] else rv) for p in c)
                # only attach a point to a road running the way the bus runs there
                url += '&bearings=' + ';'.join(f"{round(p['_bearing'])},50" if '_bearing' in p else '' for p in c)
            d = sr.fetch(url)
            if not d or d.get('code') != 'Ok' or not d.get('routes'):
                ok = False
                break
            r = d['routes'][0]
            g = r['geometry']['coordinates']
            full.extend(g[1:] if full and full[-1] == g[0] else g)
            for leg in r.get('legs', []):
                lp = []
                for step in leg.get('steps', []):
                    for q in (step.get('geometry') or {}).get('coordinates') or []:
                        if not lp or lp[-1] != q:
                            lp.append(q)
                legs_all.append(lp)
            if i + sr.CHUNK >= len(pts):
                break
            i += sr.CHUNK - 1
        if not ok:
            continue
        # merge via legs back into one leg per stop pair
        legs, cur = [], []
        for k, lp in enumerate(legs_all):
            cur.extend(lp[1:] if cur and lp and cur[-1] == lp[0] else lp)
            if pts[k + 1]['_stop']:
                legs.append(cur); cur = []
        return {'coords': full, 'legs': legs, 'radius': rs}
    return None


def length(coords):
    return transform(TO_M, LineString(coords)).length if len(coords) > 1 else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='src', required=True)
    ap.add_argument('--to', dest='dst', required=True)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--codes', default='')
    ap.add_argument('--osrm', default='https://router.project-osrm.org')
    ap.add_argument('--waypoints', help='JSON {code: [[lat, lon], ...]}: the operator path to use instead of the '
                    "harvest's (e.g. MTC's own RoutePoints from mtcits_routepaths.py); a result must then also stay "
                    'closer to that path than the plain route does')
    a = ap.parse_args()
    ext = json.load(open(a.waypoints)) if a.waypoints else None
    os.makedirs(a.dst, exist_ok=True)
    routes = sr.load_routes()
    if a.codes:
        want = set(a.codes.split(','))
        routes = [r for r in routes if r['code'] in want]
    stats = {'guided': 0, 'kept_plain': 0, 'no_path': 0, 'failed': 0, 'saved_m': 0.0}
    t0 = time.time()

    def work(r):
        code = r['code']
        out = os.path.join(a.dst, code + '.json')
        if os.path.exists(out):
            return
        plain = json.load(open(os.path.join(a.src, code + '.json')))
        if ext is not None:
            wps = [{'lat': y, 'lon': x} for y, x in ext.get(code) or []]
        else:
            try:
                wps = json.load(open(os.path.join(sr.HARVEST, f'BUS_{code}.json'))).get('waypoints') or []
            except Exception:
                wps = []
        pts, nguided = plan(r['stops'], wps)
        res = None
        if nguided or any('_bearing' in p for p in pts):
            res = route(a.osrm, pts)
        closer = True
        if res and ext is not None and len(wps) > 2:      # must also follow the operator's path more closely
            W = transform(TO_M, LineString([(p['lon'], p['lat']) for p in wps])).buffer(LEG_OFF_M)
            off = lambda cs: transform(TO_M, LineString(cs)).difference(W).length if len(cs) > 1 else 0
            closer = off(res['coords']) < off(plain['coords']) - 50
        if res and closer and len(res['legs']) == len(r['stops']) - 1 and length(res['coords']) <= length(plain['coords']) * 1.02:
            res.update(ok=True, seq=plain.get('seq'), guided_legs=nguided, **({'guide': 'mtc'} if ext is not None else {}))
            stats['guided'] += 1; stats['saved_m'] += length(plain['coords']) - length(res['coords'])
            json.dump(res, open(out, 'w'), separators=(',', ':'))
        else:
            stats['no_path' if not nguided else ('failed' if not res else 'kept_plain')] += 1
            json.dump(plain, open(out, 'w'), separators=(',', ':'))
        n = sum(v for k, v in stats.items() if k != 'saved_m')
        if n % 100 == 0:
            sr.log(f"{n}/{len(routes)} {stats['guided']} guided · {stats['saved_m'] / 1000:.0f} km of detours removed · {n / (time.time() - t0):.1f}/s")

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(work, routes))
    sr.log(f'done: {stats}')


if __name__ == '__main__':
    main()
