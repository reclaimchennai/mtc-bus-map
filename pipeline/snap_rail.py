#!/usr/bin/env python3
"""Align suburban / MRTS / Metro routes to the real railway alignment.

metro_lines.geojson was straight station-to-station segments — median 19 points
for 19 stations, every one of its 844 features containing a >500 m gap (worst
29.8 km). Trains were drawn cutting across the city rather than following track.

OSRM cannot help here: its public demo only has a `driving` profile, and snapping
a train to the road network is wrong in a different way. Instead route over the
actual rail geometry: CUMTA's SuburbanandMRTSNetwork layer (OSM-derived, 570 ways,
784 km) plus the CMRL metro corridor layers, as a graph, and walk each route's
station sequence along it with Dijkstra.

    python3 snap_rail.py [--out FILE]
"""
from __future__ import annotations

import argparse
import glob
import heapq
import json
import math
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
HARVEST = os.path.join(HERE, 'harvest', 'routes')
CUMTA = '/Users/admin/Desktop/projects/cumta/repos/CMRL-Network-MTC-Routes-Feeders/layers'
DEFAULT_OUT = '/Users/admin/Desktop/projects/maps-site/mapsv2/data/metro_lines.geojson'
# Per-station-pair legs for the congestion dashboard. snap_routes.py writes the
# bus half of that to seg_geo.json; rail is kept in its own file so re-running
# either script cannot wipe the other's segments.
SEG_OUT = ('/Users/admin/Desktop/projects/maps-site-congestion/congestion/data/'
           'seg_geo_rail.json')

# Ways are split at junctions and their endpoints do not always coincide exactly,
# so vertices are welded onto a ~1 m grid and near-touching ends joined.
GRID = 5                # decimal places ≈ 1 m
WELD_M = 12             # join endpoints this close to each other
STATION_MAX_M = 1500    # a station further than this from any track is not on it
# Dijkstra will happily return a path that exists but is absurd: St Thomas Mount to
# Velachery came back as a 45 km loop round the whole network (8.96x the direct
# distance) because the short MRTS link between them is missing from the layer. A
# connected path is not automatically the real alignment — reject the ones that are
# far longer than the stations are apart and keep a straight line for that leg.
LEG_MAX_RATIO = 3.0
LEG_SLACK_M = 2000


def load_js(path):
    t = open(path, encoding='utf-8', errors='replace').read()
    return json.loads(t[t.find('=') + 1:].strip().rstrip(';').strip())


def hav(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp, dl = p2 - p1, math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def key(c):
    return (round(c[0], GRID), round(c[1], GRID))


class RailGraph:
    def __init__(self):
        self.adj = defaultdict(dict)     # node -> {node: metres}
        self.cells = defaultdict(list)   # coarse grid -> nodes, for nearest lookup

    def add_line(self, coords):
        prev = None
        for c in coords:
            k = key(c)
            if prev is not None and prev != k:
                d = hav(prev, k)
                if d > 0:
                    # keep the shortest if a pair appears twice
                    if k not in self.adj[prev] or d < self.adj[prev][k]:
                        self.adj[prev][k] = d
                        self.adj[k][prev] = d
            prev = k

    def index(self):
        for n in self.adj:
            self.cells[(round(n[0], 2), round(n[1], 2))].append(n)

    def weld(self):
        """Join endpoints of different ways that nearly touch."""
        joined = 0
        nodes = list(self.adj)
        for n in nodes:
            cx, cy = round(n[0], 2), round(n[1], 2)
            for dx in (-0.01, 0, 0.01):
                for dy in (-0.01, 0, 0.01):
                    for m in self.cells.get((round(cx + dx, 2), round(cy + dy, 2)), ()):
                        if m == n or m in self.adj[n]:
                            continue
                        d = hav(n, m)
                        if d <= WELD_M:
                            self.adj[n][m] = d
                            self.adj[m][n] = d
                            joined += 1
        return joined // 2

    def nearest(self, lonlat, max_m=STATION_MAX_M):
        cx, cy = round(lonlat[0], 2), round(lonlat[1], 2)
        best, bd = None, None
        step = 0.01
        for ring in range(0, 6):                 # widen until something is found
            found = False
            for i in range(-ring, ring + 1):
                for j in range(-ring, ring + 1):
                    if ring and max(abs(i), abs(j)) != ring:
                        continue
                    for m in self.cells.get((round(cx + i * step, 2), round(cy + j * step, 2)), ()):
                        d = hav(lonlat, m)
                        found = True
                        if bd is None or d < bd:
                            best, bd = m, d
            if found and bd is not None and bd <= ring * 1100:
                break
        if bd is None or bd > max_m:
            return None, bd
        return best, bd

    def path(self, a, b):
        """Dijkstra. Returns list of nodes, or None if the two are not connected."""
        if a == b:
            return [a]
        dist = {a: 0.0}
        prev = {}
        pq = [(0.0, a)]
        seen = set()
        while pq:
            d, n = heapq.heappop(pq)
            if n in seen:
                continue
            seen.add(n)
            if n == b:
                out = [b]
                while out[-1] != a:
                    out.append(prev[out[-1]])
                return out[::-1]
            for m, w in self.adj[n].items():
                nd = d + w
                if nd < dist.get(m, float('inf')):
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(pq, (nd, m))
        return None


def graph_files(kind):
    """Metro and suburban are physically separate networks that happen to share
    interchange stations. Routing them on one graph lets a Metro train leak onto
    suburban track — it put the Green line at 2.5x its straight-line length — so
    each gets its own graph.

    OSM (fetched via Overpass, see osm_rail.geojson / osm_subway.geojson) is the
    primary source: 708 ways / 12,440 vertices against CUMTA's 570 / 6,595, which
    left 207 station-legs with no track within 1.5 km — Reddipalayam, Villiyambakkam
    and the rest of the outer lines simply were not in that layer. CUMTA's metro
    corridors are still added because several are built/under construction and
    mapped there in more detail than OSM."""
    if kind == 'METRO':
        f = [os.path.join(HERE, 'osm_subway.geojson')]
        f += [p for p in glob.glob(os.path.join(CUMTA, '*.js'))
              if any(t in os.path.basename(p) for t in ('BlueLine', 'GreenLine', 'Corridor'))
              and 'Stations' not in os.path.basename(p)]
        return f
    return [os.path.join(HERE, 'osm_rail.geojson'),
            os.path.join(CUMTA, 'SuburbanandMRTSNetwork_1.js')]


def build_graph(kind):
    g = RailGraph()
    files = graph_files(kind)
    used = 0
    for p in files:
        if not os.path.exists(p):
            continue
        try:
            fc = json.load(open(p)) if p.endswith('.geojson') else load_js(p)
        except Exception:
            continue
        for ft in fc.get('features', []):
            geom = ft.get('geometry') or {}
            t = geom.get('type')
            if t == 'LineString':
                g.add_line(geom['coordinates'])
            elif t == 'MultiLineString':
                for l in geom['coordinates']:
                    g.add_line(l)
            else:
                continue
            used += 1
    g.index()
    welded = g.weld()
    g.index()
    return g, used, welded


def components(g):
    seen, comps = set(), []
    for n in g.adj:
        if n in seen:
            continue
        stack, size = [n], 0
        seen.add(n)
        while stack:
            x = stack.pop()
            size += 1
            for m in g.adj[x]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        comps.append(size)
    return sorted(comps, reverse=True)


def routes_of(vt):
    pt = json.load(open(os.path.join(HERE, 'pt_data.json')))
    for r in pt['rs']:
        if r['vt'] != vt:
            continue
        p = os.path.join(HARVEST, f"{vt}_{r['cd']}.json")
        if not os.path.exists(p) or os.path.getsize(p) < 5:
            continue
        try:
            stops = json.load(open(p)).get('stops') or []
        except Exception:
            continue
        # CMRL's Phase-2 corridor is filed under placeholder station names
        # ([TEST]-0417 …). It is under construction, so drawing it beside the
        # operating lines says a train runs there when none does.
        import snap_routes as _sr
        stops = [s for s in stops
                 if s.get('lat') and s.get('lon') and not _sr.is_placeholder(s)]
        if len(stops) >= 2:
            yield r, stops


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT)
    a = ap.parse_args()

    feats = []
    segs = {}          # "aCode|bCode" -> track-aligned leg
    stats = defaultdict(int)
    far = []
    # A rail short name covers many workings (Metro 'Blue' has 44). The map draws
    # every feature matching the selected name, so disambiguate the same way the
    # bus routes are: base service keeps the name, the rest get (2), (3)…
    seen_name = defaultdict(int)
    for vt, label in (('SUBWAY', 'Suburban'), ('METRO', 'Metro')):
        g, ways, welded = build_graph(vt)
        comps = components(g)
        print(f'{label} graph: {len(g.adj)} nodes from {ways} ways · welded {welded} ends · '
              f'{len(comps)} components (largest {comps[:3]})')
        for r, stops in routes_of(vt):
            coords, ok_legs, legs = [], 0, len(stops) - 1
            for i in range(legs):
                A, B = stops[i], stops[i + 1]
                na, da = g.nearest([A['lon'], A['lat']])
                nb, db = g.nearest([B['lon'], B['lat']])
                seg = None
                if na and nb:
                    p = g.path(na, nb)
                    if p and len(p) > 1:
                        direct = hav([A['lon'], A['lat']], [B['lon'], B['lat']])
                        plen = sum(hav(p[k], p[k + 1]) for k in range(len(p) - 1))
                        if direct < 300 or plen <= max(direct * LEG_MAX_RATIO, direct + LEG_SLACK_M):
                            seg = [list(x) for x in p]
                        else:
                            stats['leg_rejected_detour'] += 1
                if seg is None:
                    seg = [[A['lon'], A['lat']], [B['lon'], B['lat']]]
                    if da is not None and da > STATION_MAX_M:
                        far.append((r.get('sN'), A.get('name'), round(da or 0)))
                else:
                    ok_legs += 1
                    # only routed legs are published: a straight-line fallback
                    # would tell the congestion map it has a real distance when
                    # it does not
                    k = f"{A.get('code')}|{B.get('code')}"
                    if k not in segs and A.get('code') and B.get('code'):
                        segs[k] = [[round(c[0], 5), round(c[1], 5)] for c in seg]
                if coords and coords[-1] == seg[0]:
                    coords.extend(seg[1:])
                else:
                    coords.extend(seg)
            if len(coords) < 2:
                continue
            base = (r.get('sN') or r['cd']).strip()
            seen_name[base] += 1
            disp = base if seen_name[base] == 1 else f'{base} ({seen_name[base]})'
            stats[f'{label}:full' if ok_legs == legs else
                  (f'{label}:partial' if ok_legs else f'{label}:none')] += 1
            feats.append({
                'type': 'Feature',
                'geometry': {'type': 'LineString',
                             'coordinates': [[round(c[0], 5), round(c[1], 5)] for c in coords]},
                'properties': {'line': label, 'route_name': disp,
                               'code': r['cd'], 'ln': r.get('lN') or '',
                               'nstops': len(stops), 'on_track': round(ok_legs / max(legs, 1), 2)},
            })

    tmp = a.out + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump({'type': 'FeatureCollection', 'features': feats}, fh, separators=(',', ':'))
    os.replace(tmp, a.out)
    # the map draws only the distinct track shapes; metro_lines stays per trip for GTFS
    from build_metro_map import slim
    slim_out = os.path.join(os.path.dirname(a.out), 'metro_map.geojson')
    print(f'{slim(a.out, slim_out)} distinct rail shapes -> {slim_out}')

    if segs:
        os.makedirs(os.path.dirname(SEG_OUT), exist_ok=True)
        tmp = SEG_OUT + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(segs, fh, separators=(',', ':'))
        os.replace(tmp, SEG_OUT)
        print(f'{len(segs)} rail station-pair legs -> {SEG_OUT} '
              f'({os.path.getsize(SEG_OUT)/1048576:.2f} MB)')
    print(f'{len(feats)} rail routes -> {a.out} ({os.path.getsize(a.out)/1048576:.2f} MB)')
    print('  ' + ' · '.join(f'{k} {v}' for k, v in sorted(stats.items())))
    if far:
        u = sorted({f for f in far})[:6]
        print(f'  stations >{STATION_MAX_M} m from any track: {len(far)} legs, e.g. {u}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
