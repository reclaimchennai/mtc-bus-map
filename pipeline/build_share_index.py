#!/usr/bin/env python3
"""Compact data for the share-card service (v36/share_api.py).

    build_share_index.py MAPSV2_DATA COAST.geojson LAKES.geojson OUT.json [W,S,E,N]

COAST may be "-" for an inland city (Delhi: no sea); the box defaults to Chennai's.

Everything a card needs, small enough to hold in memory on the server:
  routes    {code: [sn, from, to, nstops, trips, first, last, [[lon, lat], ...], category]}  lines simplified to ~25 m
  services  {sn: [codes, longest working first]}
  stops     [[lon, lat, name, [sn, ...]], ...]
  sea       polygons east of the coastline, lakes  polygons — the card maps' water
"""
import json
import sys

from shapely.geometry import LineString, Polygon, box, mapping, shape
from shapely.ops import split, unary_union

data, coast_p, lakes_p, out_p = sys.argv[1:5]
BOX_ARG = [float(x) for x in sys.argv[5].split(',')] if len(sys.argv) > 5 else [79.85, 12.55, 80.55, 13.65]
F = json.load(open(f'{data}/mtc_routes.geojson'))['features']
TM = json.load(open(f'{data}/timings.json'))
STOPS = json.load(open(f'{data}/bus_stops.geojson'))['features']

routes, services = {}, {}
for f in F:
    p, g = f['properties'], f['geometry']
    if not g:
        continue
    cs = g['coordinates'] if g['type'] == 'LineString' else [c for part in g['coordinates'] for c in part]
    if len(cs) < 2:
        continue
    line = LineString(cs).simplify(0.00025)
    t = TM.get(str(p['code']), {})
    routes[str(p['code'])] = [p.get('sn') or p.get('route_name'), p.get('source') or '', p.get('destinatio') or '', p.get('nstops') or 0,
                              t.get('trips') or p.get('trips') or 0, t.get('first') or '', t.get('last') or '',
                              [[round(x, 4), round(y, 4)] for x, y in line.coords], p.get('category') or '']
for code, r in routes.items():
    services.setdefault(r[0], []).append(code)
for sn in services:
    services[sn].sort(key=lambda c: (-routes[c][3], -routes[c][4], c))

stops = []
for f in STOPS:
    lon, lat = f['geometry']['coordinates']
    rs = f['properties'].get('route name') or ''
    rs = [x.strip() for x in (json.loads(rs) if rs.startswith('[') else rs.split(',')) if x.strip()]
    stops.append([round(lon, 5), round(lat, 5), f['properties'].get('Stop Name') or '', sorted(set(rs))])

# water: lakes as they are (simplified); the sea = the part of the city box east of the coast
BOX = box(*BOX_ARG)
lakes = []
for f in json.load(open(lakes_p))['features']:
    g = shape(f['geometry'])
    if g.geom_type in ('Polygon', 'MultiPolygon') and g.area > 2e-7 and g.intersects(BOX):
        g = g.simplify(0.0003)
        for poly in (g.geoms if g.geom_type == 'MultiPolygon' else [g]):
            if poly.area > 2e-7:
                lakes.append([[round(x, 4), round(y, 4)] for x, y in poly.exterior.coords])
sea = []
coast = None if coast_p == '-' else unary_union([shape(f['geometry']) for f in json.load(open(coast_p))['features']
                                                 if f['geometry'] and f['geometry']['type'] in ('LineString', 'MultiLineString')])
pieces = split(BOX, coast).geoms if coast is not None else []
for poly in pieces:
    # sea pieces touch the box's east edge and lie mostly east of the coast
    if poly.intersects(LineString([(BOX_ARG[2], BOX_ARG[1]), (BOX_ARG[2], BOX_ARG[3])])) and poly.area > 1e-4:
        poly = poly.simplify(0.0003)
        sea.append([[round(x, 4), round(y, 4)] for x, y in poly.exterior.coords])

json.dump({'routes': routes, 'services': services, 'stops': stops, 'sea': sea, 'lakes': lakes},
          open(out_p, 'w'), separators=(',', ':'))
print(f'{len(routes)} workings, {len(services)} services, {len(stops)} stops, {len(sea)} sea, {len(lakes)} lakes')
