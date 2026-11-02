#!/usr/bin/env python3
"""The rail lines as the map draws them: one feature per distinct track shape.

    build_metro_map.py metro_lines.geojson metro_map.geojson

metro_lines.geojson carries one shape per rail trip (842 of them: 800 suburban,
42 metro), because build_gtfs_clean.py reads it back as each trip's GTFS shape.
The map only draws the lines, in one style, so 708 of those are the same track
drawn again: 6.3 MB the browser downloads, parses and tiles for nothing. This
keeps each distinct shape once, with only the `line` property, and leaves
metro_lines.geojson untouched for the pipeline. snap_rail.py runs it after
writing metro_lines.geojson.
"""
import json
import sys


def slim(src, out):
    seen, feats = set(), []
    for f in json.load(open(src))['features']:
        line = f['properties'].get('line')
        k = (line, json.dumps(f['geometry']['coordinates']))
        if k in seen:
            continue
        seen.add(k)
        feats.append({'type': 'Feature', 'geometry': f['geometry'], 'properties': {'line': line}})
    with open(out, 'w') as fh:
        json.dump({'type': 'FeatureCollection', 'features': feats}, fh, separators=(',', ':'))
    return len(feats)


if __name__ == '__main__':
    src, out = sys.argv[1:3]
    print(f'{out}: {slim(src, out)} shapes')
