#!/usr/bin/env python3
"""Share links and social cards for the MTC map, like metro.reclaimchennai.city's.

    share_api.py --data DIR --live DIR --db DIR --fonts DIR --cache DIR [--port 5064] [--city chennai|delhi]

One service per city (CITIES below: address, names, map extents, wording); the
Delhi one sits behind maps.reclaimchennai.city/delhi, Caddy stripping /delhi.

Short links (what the app's Share button hands out):
    /r/<route>[?w=<working>]   a route        -> /?route=<route>&w=<working>
    /s/<lat>,<lon>             a bus stop     -> /?stop=<lat>,<lon>
    /b/<registration>          a live bus     -> /?bus=<registration>
Each answers with a tiny page carrying its own Open Graph / Twitter tags (the
title, a description and a 1200x630 card) and forwards a person straight into
the map; WhatsApp, X and Facebook read the tags without running JavaScript.

Cards (PNG, drawn here with Pillow at 2x and downsampled, cached on disk):
    /og/r/<route>.png?w=  /og/s/<lat>,<lon>.png  /og/b/<reg>.png  /og/site.png  /og/db.png
Left: the title, pills and one headline number; right: the route or stop on a
real-geography map with the sea and the lakes — the metro cards' layout.
"""
import argparse
import colorsys
import datetime as dt
import hashlib
import html
import io
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from PIL import Image, ImageDraw, ImageFont

CITIES = {
    'chennai': dict(origin='https://mtc.reclaimchennai.city', host='mtc.reclaimchennai.city', prefix='',
                    name='Chennai MTC Buses', script='tamil', sub_route='மாநகரப் பேருந்து', sub_stop='பேருந்து நிறுத்தம்',
                    sub_site='சென்னை மாநகரப் பேருந்து', on_map='the Chennai MTC map', bus_box=[80.1, 12.85, 80.32, 13.2],
                    site_box=[80.02, 12.78, 80.32, 13.25], db_title='MTC, measured', db_sub='பேருந்துகள், அளவிடப்பட்டவை',
                    db_desc="Chennai's buses from their own GPS: on the road, speed, bunching, crowding",
                    route_more='Live buses, stops, timings and fares',
                    kinds=(('Electric', 'e'), ('AC', 'a'), ('wheelchair accessible', 'l')), tags={}),
    'delhi': dict(origin='https://maps.reclaimchennai.city/delhi', host='maps.reclaimchennai.city/delhi', prefix='/delhi',
                  name='Delhi Buses', script='deva', sub_route='दिल्ली की बसें', sub_stop='बस स्टॉप',
                  sub_site='दिल्ली की बसें', on_map='the Delhi bus map', bus_box=[76.95, 28.45, 77.40, 28.85],
                  site_box=[76.88, 28.40, 77.42, 28.88], db_title='Delhi buses, measured', db_sub='दिल्ली की बसें, मापी गईं',
                  db_desc="Delhi's buses from their own GPS: on the road, speed, bunching",
                  route_more='Live buses, stops and timings',
                  kinds=(('DEVI electric feeder', 'd'), ('Electric', 'e'), ('cluster bus', 'k'), ('AC', 'a'), ('wheelchair accessible', 'l')),
                  tags={'DTC': (47, 154, 62), 'Cluster': (236, 122, 28), 'DEVI': (226, 86, 27)}),
}
CITY = CITIES['chennai']
ORIGIN = CITY['origin']
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
W, H, S = 1200, 630, 2                        # card size, supersampling
PAPER, INK, MUTED, RED = (249, 245, 237), (28, 28, 30), (110, 108, 104), (240, 27, 72)
WATER, TAG = (191, 227, 242), (47, 98, 232)
MAPX = 560                                   # the map fills x >= MAPX

A = {}                                       # loaded data


def nice(s):
    """CUMTA's names are mostly in capitals: title-case them, keeping short
    abbreviations (MGR, B.T, KCBT) as they are."""
    if not s or s != s.upper():
        return s
    out = []
    for w_ in s.split():
        core = w_.strip('().,-')
        out.append(w_ if ('.' in w_ or (len(core) <= 4 and core.isalpha() and not any(v in core for v in 'AEIOU'))) else w_.capitalize())
    return ' '.join(out)


def hash_color(name):
    """The map's own route colour (app.js hashColor), as RGB."""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    hue, sat, lig = (h * 47) % 360, 62 + (h % 18), 42 + (h % 12)
    r, g, b = colorsys.hls_to_rgb(hue / 360, lig / 100, sat / 100)
    return (round(r * 255), round(g * 255), round(b * 255))


def font(bold, size, tamil=False):
    script = CITY['script'] if tamil else ''                  # tamil=True: the city's own script
    key = (bold, size, script)
    if key not in A['fonts']:
        name = {'tamil': 'NotoSansTamil-', 'deva': 'NotoSansDevanagari-'}.get(script, 'NotoSans-') + ('Bold' if bold else 'Regular') + '.ttf'
        A['fonts'][key] = ImageFont.truetype(os.path.join(A['fontdir'], name), size * S)
    return A['fonts'][key]


def load(a):
    A.update(dict(data=a.data, live=a.live, db=a.db, fontdir=a.fonts, cache=a.cache, fonts={}))
    A['idx'] = json.load(open(os.path.join(a.data, 'share_index.json')))
    A['mtime'] = os.path.getmtime(os.path.join(a.data, 'share_index.json'))
    A['cat'] = {r[0]: r[8] for r in A['idx']['routes'].values() if len(r) > 8}     # route number -> category (tag colour)
    os.makedirs(a.cache, exist_ok=True)


# ── drawing helpers ──
def merc(lon, lat):
    return lon, math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


class Map:
    """Fits a lon/lat box into the card's right side, keeping the aspect."""
    def __init__(self, bbox, pad=0.12):
        x0, y0 = merc(bbox[0], bbox[1]); x1, y1 = merc(bbox[2], bbox[3])
        dx, dy = max(x1 - x0, 0.004), max(y1 - y0, 0.004)
        x0, x1, y0, y1 = x0 - dx * pad, x1 + dx * pad, y0 - dy * pad, y1 + dy * pad
        bw, bh = (W - MAPX) * S, H * S
        k = min(bw / (x1 - x0), bh / (y1 - y0))
        self.k, self.cx, self.cy = k, (x0 + x1) / 2, (y0 + y1) / 2
        self.ox, self.oy = MAPX * S + bw / 2, bh / 2

    def __call__(self, lon, lat):
        x, y = merc(lon, lat)
        return (self.ox + (x - self.cx) * self.k, self.oy - (y - self.cy) * self.k)


def water(d, m):
    d.rectangle([MAPX * S, 0, W * S, H * S], fill=(244, 240, 231))
    for ring in A['idx']['sea'] + A['idx']['lakes']:
        pts = [m(x, y) for x, y in ring]
        if any(MAPX * S - 200 < px < W * S + 200 and -200 < py < H * S + 200 for px, py in pts):
            d.polygon(pts, fill=WATER)


def line(d, m, coords, color, width, casing=True):
    pts = [m(x, y) for x, y in coords]
    if casing:
        d.line(pts, fill=(255, 255, 255), width=int((width + 5) * S), joint='curve')
    d.line(pts, fill=color, width=int(width * S), joint='curve')


def dot(d, xy, r, fill, ring=None, rw=2):
    x, y = xy
    if ring:
        d.ellipse([x - (r + rw) * S, y - (r + rw) * S, x + (r + rw) * S, y + (r + rw) * S], fill=ring)
    d.ellipse([x - r * S, y - r * S, x + r * S, y + r * S], fill=fill)


def logo(d, x, y):
    s = 64 * S
    d.rounded_rectangle([x * S, y * S, x * S + s, y * S + s], radius=16 * S, fill=RED)
    bx, by = x * S + 13 * S, y * S + 16 * S
    d.rounded_rectangle([bx, by, bx + 38 * S, by + 28 * S], radius=6 * S, fill=(255, 255, 255))
    d.rounded_rectangle([bx + 4 * S, by + 5 * S, bx + 34 * S, by + 14 * S], radius=2 * S, fill=RED)
    for wx in (bx + 8 * S, bx + 30 * S):
        d.ellipse([wx - 4 * S, by + 24 * S, wx + 4 * S, by + 32 * S], fill=(255, 255, 255), outline=RED, width=2 * S)


def text(d, xy, s, f, fill=INK):
    d.text((xy[0] * S, xy[1] * S), s, font=f, fill=fill)
    return d.textlength(s, font=f) / S


def wrap(d, s, f, width, lines):
    words, out, cur = s.split(), [], ''
    for w_ in words:
        t = (cur + ' ' + w_).strip()
        if d.textlength(t, font=f) / S <= width:
            cur = t
        else:
            out.append(cur); cur = w_
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        while d.textlength(out[-1] + '…', font=f) / S > width and ' ' in out[-1]:
            out[-1] = out[-1].rsplit(' ', 1)[0]
        out[-1] += '…'
    return [o for o in out if o]


def pill(d, x, y, label, size=30, bg=TAG, fg=(255, 255, 255)):
    f = font(True, size)
    tw = d.textlength(label, font=f) / S
    h, padx = size * 1.45, size * 0.48
    d.rounded_rectangle([x * S, y * S, (x + tw + 2 * padx) * S, (y + h) * S], radius=int(h * 0.3 * S), fill=bg)
    d.text(((x + padx) * S, (y + h * 0.5) * S), label, font=f, fill=fg, anchor='lm')
    return tw + 2 * padx


def runs(d, x, y, parts, size):
    """'**bold** regular' parts as [(text, bold)] on one line."""
    for s, b in parts:
        x += text(d, (x, y), s, font(b, size))


def footer(d, path):
    f1, f2 = font(False, 24), font(True, 24)
    w1 = text(d, (56, H - 76), CITY['host'], f2 if not path else f1, INK if not path else MUTED)
    if path:
        text(d, (56 + w1, H - 76), path, f2, INK)


def finish(img):
    out = img.resize((W, H), Image.LANCZOS)
    b = io.BytesIO(); out.save(b, 'PNG', optimize=True)
    return b.getvalue()


def base():
    img = Image.new('RGB', (W * S, H * S), PAPER)
    return img, ImageDraw.Draw(img)


# ── cards ──
def bbox_of(coords):
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def card_route(sn, code=None):
    idx = A['idx']
    codes = idx['services'].get(sn)
    if not codes:
        return None
    code = code if code in codes else codes[0]
    sn_, src, dst, nst, trips, first, last, coords = idx['routes'][code][:8]
    tag = CITY['tags'].get(idx['routes'][code][8] if len(idx['routes'][code]) > 8 else '', TAG)
    img, d = base()
    m = Map(bbox_of(coords))
    water(d, m)
    col = hash_color(sn)
    for other in codes[1:]:                                   # the service's other workings, faint
        line(d, m, idx['routes'][other][7], tuple(int(c * .35 + 255 * .65) for c in col), 4, casing=False)
    line(d, m, coords, col, 9)
    dot(d, m(*coords[0]), 9, (255, 255, 255), col, 4)
    dot(d, m(*coords[-1]), 9, col, (255, 255, 255), 4)
    d.rectangle([0, 0, MAPX * S, H * S], fill=PAPER)
    logo(d, 56, 50)
    text(d, (134, 62), CITY['name'], font(True, 26), INK)
    text(d, (134, 96), CITY['sub_route'], font(False, 21, True), MUTED)
    pill(d, 56, 150, sn, 44, tag)
    y = 236
    for ln in wrap(d, f'{nice(src)} to {nice(dst)}', font(True, 40), 470, 3):
        text(d, (56, y), ln, font(True, 40)); y += 52
    y += 14
    runs(d, 56, y, [(str(nst), True), (' stops · ', False), (str(trips), True), (' trips a day', False)], 28)
    if first and last:
        text(d, (56, y + 42), f'First bus {first} · last {last}', font(False, 24), MUTED)
    text(d, (56, H - 120), CITY['route_more'], font(False, 23), MUTED)
    footer(d, '/r/' + sn)
    return finish(img)


def nearest_stop(lat, lon):
    best, bd = None, 1e9
    for s in A['idx']['stops']:
        dd = (s[0] - lon) ** 2 + (s[1] - lat) ** 2
        if dd < bd:
            bd, best = dd, s
    return best if bd < 0.0000025 else None                  # within ~170 m


def card_stop(lat, lon):
    s = nearest_stop(lat, lon)
    if not s:
        return None
    lon, lat, name, rs = s
    img, d = base()
    span = 0.022
    m = Map([lon - span, lat - span * 0.62, lon + span, lat + span * 0.62], pad=0.0)
    water(d, m)
    idx = A['idx']
    for sn in rs[:40]:
        for code in idx['services'].get(sn, [])[:1]:
            line(d, m, idx['routes'][code][7], hash_color(sn), 6)
    dot(d, m(lon, lat), 13, RED, (255, 255, 255), 5)
    dot(d, m(lon, lat), 5, (255, 255, 255))
    d.rectangle([0, 0, MAPX * S, H * S], fill=PAPER)
    logo(d, 56, 50)
    text(d, (134, 62), CITY['name'], font(True, 26), INK)
    text(d, (134, 96), CITY['sub_stop'], font(False, 21, True), MUTED)
    y = 160
    for ln in wrap(d, nice(name), font(True, 48), 470, 2):
        text(d, (56, y), ln, font(True, 48)); y += 62
    y += 12
    runs(d, 56, y, [(str(len(rs)), True), (' route' + ('' if len(rs) == 1 else 's') + ' stop here', False)], 28)
    y += 56
    x = 56
    for sn in rs:
        f = font(True, 22)
        wdt = d.textlength(sn, font=f) / S + 22
        if x + wdt > 520:
            x, y = 56, y + 44
            if y > H - 190:
                pill(d, x, y, f'+{len(rs) - rs.index(sn)} more', 22, (228, 222, 210), INK)
                break
        x += pill(d, x, y, sn, 22, CITY['tags'].get(A['cat'].get(sn, ''), TAG)) + 8
    text(d, (56, H - 120), 'Live arrivals, the next bus and every route', font(False, 23), MUTED)
    footer(d, None)
    return finish(img)


def live_bus(reg):
    try:
        j = json.load(open(os.path.join(A['live'], 'vehicles.json')))
    except Exception:
        return None, None
    for v in j['v']:
        if v[0] == reg:
            return v, j
    return None, j


def card_bus(reg):
    v, j = live_bus(reg)
    idx = A['idx']
    img, d = base()
    if v:
        _, code, lat, lon, _b, fix, fl, eta = v[:8]
        r = idx['routes'].get(code)
        tag = CITY['tags'].get(r[8] if r and len(r) > 8 else '', TAG)
        sn, dst, coords = (r[0], r[2], r[7]) if r else (code, '', [[lon, lat]])
        bb = bbox_of(coords + [[lon, lat]])
        m = Map(bb)
        water(d, m)
        line(d, m, coords, hash_color(sn), 8)
        dot(d, m(lon, lat), 16, RED, (255, 255, 255), 5)
        dot(d, m(lon, lat), 6, (255, 255, 255))
    else:
        m = Map(CITY['bus_box']); water(d, m)
    d.rectangle([0, 0, MAPX * S, H * S], fill=PAPER)
    logo(d, 56, 50)
    text(d, (134, 62), CITY['name'], font(True, 26), INK)
    d.ellipse([134 * S, 104 * S, 146 * S, 116 * S], fill=RED)
    text(d, (154, 96), 'LIVE', font(True, 21), RED)
    if not v:
        text(d, (56, 170), f'Bus {reg}', font(True, 52))
        text(d, (56, 248), 'Not reporting its position right now.', font(False, 26), MUTED)
    else:
        pill(d, 56, 150, sn, 44, tag)
        y = 236
        for ln in wrap(d, f'to {nice(dst)}', font(True, 40), 470, 2):
            text(d, (56, y), ln, font(True, 40)); y += 52
        y += 10
        kinds = [k for k, c in CITY['kinds'] if c in fl and not (c == 'e' and 'd' in fl)]
        runs(d, 56, y, [(f'Bus {reg}', True), ((' · ' + ' · '.join(kinds)) if kinds else '', False)], 26)
        t = time.time()
        nxt = [(i, fix + dt_) for i, dt_ in zip(eta[0::2], eta[1::2]) if fix + dt_ > t] if j.get('fmt') == 2 else []
        sstops = None
        if nxt:
            text(d, (56, y + 44), f'Next stop in {max(1, round((nxt[0][1] - t) / 60))} min · as of '
                 f'{dt.datetime.now(IST).strftime("%H:%M")}', font(False, 24), MUTED)
    text(d, (56, H - 120), 'Follow it live on the map', font(False, 23), MUTED)
    footer(d, '/b/' + reg)
    return finish(img)


def card_site(title=None, sub=None, stat=None, desc=None, path=None):
    title, sub = title or CITY['name'], sub or CITY['sub_site']
    idx = A['idx']
    img, d = base()
    m = Map(CITY['site_box'], pad=0.0)
    water(d, m)
    for code, r in idx['routes'].items():
        line(d, m, r[7], tuple(int(c * .7 + 255 * .3) for c in hash_color(r[0])), 2.2, casing=False)
    d.rectangle([0, 0, MAPX * S, H * S], fill=PAPER)
    logo(d, 56, 56)
    y = 150
    for ln in wrap(d, title, font(True, 72), 470, 2):
        text(d, (56, y), ln, font(True, 72)); y += 84
    text(d, (56, y + 6), sub, font(False, 30, True), MUTED)
    y += 74
    runs(d, 56, y, stat or [(f"{len(idx['services']):,}", True), (' routes, ', False), (f"{len(idx['stops']):,}", True), (' stops', False)], 30)
    for i, ln in enumerate(wrap(d, desc or 'Every route, every live bus, the next bus at every stop, real road speeds and fares',
                                font(False, 23), 460, 3)):
        text(d, (56, y + 56 + i * 32), ln, font(False, 23), MUTED)
    footer(d, path)
    return finish(img)


def card_db():
    try:
        j = json.load(open(os.path.join(A['db'], 'data.json')))
        b = j['bunching']
        stat = [(f"{j['coverage']['buses']:,}", True), (' buses tracked · ', False), (f"{100 * b['total_bunched'] / max(1, b['total_pairs']):.1f}%", True), (' bunched', False)]
    except Exception:
        stat = None
    return card_site(CITY['db_title'], CITY['db_sub'], stat, CITY['db_desc'], '/db')


# ── http ──
def cached(key, ttl, make):
    p = os.path.join(A['cache'], hashlib.sha1(key.encode()).hexdigest() + '.png')
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl and os.path.getmtime(p) > A['mtime']:
        return open(p, 'rb').read()
    png = make()
    if png:
        tmp = p + '.tmp'
        open(tmp, 'wb').write(png); os.replace(tmp, p)
    return png


def page(title, desc, image, canonical, target):
    e = html.escape
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="{e(canonical)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{e(CITY['name'])}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(canonical)}">
<meta property="og:image" content="{e(image)}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}"><meta name="twitter:description" content="{e(desc)}">
<meta name="twitter:image" content="{e(image)}">
<meta http-equiv="refresh" content="0; url={e(target)}">
<script>location.replace({json.dumps(target)})</script>
</head><body style="font-family:sans-serif;background:#f9f5ed;padding:24px"><a href="{e(target)}">{e(title)} — open the map</a></body></html>""".encode()


class Hdl(BaseHTTPRequestHandler):
    server_version = 'mtc-share'

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype, maxage=300):
        self.send_response(code)
        self.send_header('content-type', ctype)
        self.send_header('cache-control', f'public, max-age={maxage}')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [unquote(x) for x in u.path.strip('/').split('/')]
        try:
            return self.route(parts, q)
        except Exception as e:                                # never take the map down with a card
            return self.reply(500, f'error: {type(e).__name__}'.encode(), 'text/plain', 0)

    def route(self, parts, q):
        idx, ver = A['idx'], int(A['mtime'])
        if parts[0] == 'og' and len(parts) >= 2:
            kind = parts[1]
            if kind in ('site.png', 'db.png'):
                png = cached(kind, 3600 if kind == 'db.png' else 86400 * 7, card_db if kind == 'db.png' else card_site)
                return self.reply(200, png, 'image/png', 3600)
            ident = '/'.join(parts[2:])
            if ident.endswith('.png'):
                ident = ident[:-4]
            if kind == 'r':
                png = cached(f'r:{ident}:{q.get("w", "")}', 86400 * 7, lambda: card_route(ident, q.get('w')))
            elif kind == 's':
                lat, lon = (float(x) for x in ident.split(','))
                png = cached(f's:{lat:.5f},{lon:.5f}', 86400 * 7, lambda: card_stop(lat, lon))
            elif kind == 'b':
                png = cached(f'b:{ident}:{int(time.time() // 120)}', 120, lambda: card_bus(ident))
            else:
                png = None
            if not png:
                return self.reply(404, b'not found', 'text/plain', 60)
            return self.reply(200, png, 'image/png', 600 if kind == 'b' else 86400)

        if parts[0] == 'r' and len(parts) >= 2:
            sn = '/'.join(parts[1:])
            codes = idx['services'].get(sn)
            if not codes:
                return self.reply(404, page('Route not found', '', f'{ORIGIN}/og/site.png', ORIGIN, CITY['prefix'] + '/'), 'text/html; charset=utf-8', 60)
            code = q.get('w') if q.get('w') in codes else codes[0]
            r = idx['routes'][code]
            title = f'Route {sn} · {nice(r[1])} → {nice(r[2])}'
            desc = f'{r[3]} stops · {r[4]} trips a day' + (f' · first bus {r[5]}, last {r[6]}' if r[5] else '') + ' — live buses, stops and timings on ' + CITY['on_map'] + '.'
            tgt = f"{CITY['prefix']}/?route={quote(sn)}&w={code}"
            return self.reply(200, page(title, desc, f'{ORIGIN}/og/r/{quote(sn)}.png?w={code}&v={ver}', f'{ORIGIN}/r/{quote(sn)}?w={code}', tgt), 'text/html; charset=utf-8')
        if parts[0] == 's' and len(parts) >= 2:
            lat, lon = (float(x) for x in parts[1].split(','))
            s = nearest_stop(lat, lon)
            if not s:
                return self.reply(404, page('Stop not found', '', f'{ORIGIN}/og/site.png', ORIGIN, CITY['prefix'] + '/'), 'text/html; charset=utf-8', 60)
            key = f'{s[1]:.5f},{s[0]:.5f}'
            title = f'{nice(s[2])} · bus stop'
            desc = f'{len(s[3])} routes: ' + ', '.join(s[3][:12]) + ('…' if len(s[3]) > 12 else '') + ' — live arrivals on ' + CITY['on_map'] + '.'
            return self.reply(200, page(title, desc, f'{ORIGIN}/og/s/{key}.png?v={ver}', f'{ORIGIN}/s/{key}', f"{CITY['prefix']}/?stop={key}"), 'text/html; charset=utf-8')
        if parts[0] == 'b' and len(parts) >= 2:
            reg = parts[1][:12]
            v, _ = live_bus(reg)
            r = idx['routes'].get(v[1]) if v else None
            if v and len(v) > 8 and r:                  # alias-matched bus: its own route number
                r = [v[8]] + list(r[1:])
            title = f'Bus {reg} · {r[0]} → {nice(r[2])}' if r else f'Bus {reg}'
            desc = ('Follow it live on ' + CITY['on_map'] + '.' if v else 'Not reporting right now — see every live bus on ' + CITY['on_map'] + '.')
            return self.reply(200, page(title, desc, f'{ORIGIN}/og/b/{quote(reg)}.png?t={int(time.time() // 120)}', f'{ORIGIN}/b/{quote(reg)}', f"{CITY['prefix']}/?bus={quote(reg)}"), 'text/html; charset=utf-8', 60)
        return self.reply(404, b'not found', 'text/plain', 60)


def main():
    ap = argparse.ArgumentParser()
    for k in ('data', 'live', 'db', 'fonts', 'cache'):
        ap.add_argument('--' + k, required=True)
    ap.add_argument('--port', type=int, default=5064)
    ap.add_argument('--city', default='chennai', choices=sorted(CITIES))
    a = ap.parse_args()
    global CITY, ORIGIN
    CITY = CITIES[a.city]; ORIGIN = CITY['origin']
    load(a)

    def reload_loop():                                       # pick up a refreshed share_index.json
        while True:
            time.sleep(300)
            try:
                if os.path.getmtime(os.path.join(a.data, 'share_index.json')) > A['mtime']:
                    load(a)
            except Exception:
                pass
    threading.Thread(target=reload_loop, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', a.port), Hdl).serve_forever()


if __name__ == '__main__':
    main()
