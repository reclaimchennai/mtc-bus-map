/* Route lines as cables: at street zoom, every route on a road gets its own lane,
   side by side like a bundle of network cables, instead of a dozen lines drawn on
   top of each other.

   The network is cut into stop-to-stop road segments. Every segment knows which
   routes run along it (either way, so the up and down working share a lane);
   each route keeps one place in a single network-wide order, so on two
   neighbouring segments with the same routes the lanes line up exactly, and where
   a route joins or leaves the others just close up by one lane. Segments are
   oriented along the network (each one continues from the one before it), so a
   lane stays on the same side of the road through a turn. Each lane carries its
   route number, written on its own lane.

   A segment whose shape is only a straight line between two stops (the road
   router found no road) is not drawn as a cable at all — it would cut across
   the blocks — but as one faint dashed line.

     const C = BUSCABLES.create(map, { minzoom: 13, before: 'sel-glow' });
     C.build(routes, segCoords)   routes: [{ id, num, color, works: [{ id, stops }] }];
                                  segCoords(a, b) -> [[lon, lat], ...], or { c: coords, routed: 0|1 }, or null
                                  routed = 0 (no road found) is drawn dashed; without the flag, a 2-point
                                  segment over 150 m counts as unrouted
     C.dim(opacity)               while a route is picked
     C.mark(fc), C.opacity(op)    hide a route's plain line at street zoom once it has a cable
     C.layers                     ids, for clicks
   Built lazily for what is in view, so the vertex count stays small on phones. */
'use strict';
const BUSCABLES = (() => {
  const K = 111320;
  const len = c => { let s = 0; for (let i = 1; i < c.length; i++) s += Math.hypot((c[i][0] - c[i - 1][0]) * K * Math.cos(c[i][1] * Math.PI / 180), (c[i][1] - c[i - 1][1]) * K); return s; };
  const MAXLANES = 16;

  function create(map, opts = {}) {
    const MINZ = opts.minzoom || 13;
    const SEG = new Map();             // 'a|b' (a < b) -> { a, b, c: coords a->b, routes: Set, bbox, straight, flip }
    const ORDER = new Map();           // route id -> place in the network-wide lane order
    const R = new Map();               // route id -> route
    let built = false;

    map.addSource('cables', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    map.addLayer({ id: 'cables-straight', type: 'line', source: 'cables', minzoom: MINZ, filter: ['==', ['get', 'st'], 1],
      layout: { 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': 1.1, 'line-opacity': 0.4, 'line-dasharray': [2, 3] } }, opts.before);
    // the bundle's sleeve: one white casing under all its lanes, so a trunk road reads as one cable
    map.addLayer({ id: 'cables-case', type: 'line', source: 'cables', minzoom: MINZ, filter: ['==', ['get', 'st'], 2],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#fff', 'line-width': ['get', 'w'], 'line-opacity': 0.92 } }, opts.before);
    map.addLayer({ id: 'cables', type: 'line', source: 'cables', minzoom: MINZ, filter: ['==', ['get', 'st'], 0],
      layout: { 'line-cap': 'butt', 'line-join': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': ['get', 'w'], 'line-offset': ['get', 'off'], 'line-opacity': 0.9 } }, opts.before);
    map.addLayer({ id: 'cable-labels', type: 'symbol', source: 'cables', minzoom: MINZ + 1, filter: ['all', ['==', ['get', 'st'], 0], ['==', ['get', 'lbl'], 1]],
      layout: { 'symbol-placement': 'line', 'text-field': ['get', 'sn'], 'text-size': 10.5, 'text-font': ['Noto Sans Bold'],
        'text-offset': ['get', 'toff'], 'symbol-spacing': 260, 'text-padding': 2, 'text-allow-overlap': false, 'text-keep-upright': true,
        'text-max-angle': 30 },
      paint: { 'text-color': ['get', 'color'], 'text-halo-color': '#fff', 'text-halo-width': 1.6 } }, opts.before);

    function build(routes, segCoords) {
      routes.slice().sort((p, q) => String(p.num).localeCompare(String(q.num), undefined, { numeric: true }))
        .forEach((r, i) => { ORDER.set(r.id, i); R.set(r.id, r); });
      for (const r of routes) for (const w of r.works) for (let i = 0; i + 1 < w.stops.length; i++) {
        const x = w.stops[i], y = w.stops[i + 1]; if (x === y) continue;
        const a = x < y ? x : y, b = x < y ? y : x, k = a + '|' + b;
        let s = SEG.get(k);
        if (!s) {
          const get = (x, y) => { const v = segCoords(x, y); return !v ? null : Array.isArray(v) ? { c: v, routed: null } : v; };
          let g = get(a, b);
          if (!g || !g.c || g.c.length < 2) { const r2 = get(b, a); g = r2 && r2.c && r2.c.length > 1 ? { c: r2.c.slice().reverse(), routed: r2.routed } : null; }
          if (!g) continue;
          const c = g.c;
          let W = 180, E = -180, S_ = 90, N = -90;
          for (const p of c) { if (p[0] < W) W = p[0]; if (p[0] > E) E = p[0]; if (p[1] < S_) S_ = p[1]; if (p[1] > N) N = p[1]; }
          SEG.set(k, s = { a, b, c, routes: new Set(), bbox: [W, S_, E, N], straight: g.routed != null ? !g.routed : c.length <= 2 && len(c) > 150, flip: null });
        }
        s.routes.add(r.id);
        if (!s.straight) covered.add(String(r.id));
      }
      orient();
      built = true;
      refresh();
    }

    /* Point every segment along the network: walk outward from each, and give a
       neighbour the direction that continues this one (it starts where this one
       ends, or ends where this one starts). Then lane n is on the same side of the
       road on both, through bends and turns. */
    function orient() {
      const at = new Map();
      for (const [k, s] of SEG) for (const p of [s.a, s.b]) { let l = at.get(p); if (!l) at.set(p, l = []); l.push(k); }
      for (const [k0, s0] of SEG) {
        if (s0.flip !== null) continue;
        s0.flip = false;
        const q = [k0];
        while (q.length) {
          const s = SEG.get(q.pop());
          const from = s.flip ? s.b : s.a, to = s.flip ? s.a : s.b;
          for (const [p, wantStart] of [[to, true], [from, false]]) for (const kn of at.get(p) || []) {
            const n = SEG.get(kn); if (n.flip !== null) continue;
            n.flip = wantStart ? n.a !== p : n.b !== p;
            q.push(kn);
          }
        }
      }
    }

    let last = '';
    function refresh() {
      if (!built) return;
      const z = map.getZoom();
      if (z < MINZ - 0.3) { if (last !== 'off') { map.getSource('cables').setData({ type: 'FeatureCollection', features: [] }); last = 'off'; } return; }
      const b = map.getBounds(), px = (b.getEast() - b.getWest()) * 0.25, py = (b.getNorth() - b.getSouth()) * 0.25;
      const W = b.getWest() - px, E = b.getEast() + px, S_ = b.getSouth() - py, N = b.getNorth() + py;
      const key = [W, S_, E, N].map(v => v.toFixed(3)).join(',');
      if (key === last) return; last = key;
      const feats = [];
      for (const s of SEG.values()) {
        const bb = s.bbox; if (bb[2] < W || bb[0] > E || bb[3] < S_ || bb[1] > N) continue;
        let ids = [...s.routes];
        if (ids.length > MAXLANES) ids = ids.sort((p, q) => (R.get(q).trips || 0) - (R.get(p).trips || 0)).slice(0, MAXLANES);
        ids.sort((p, q) => ORDER.get(p) - ORDER.get(q));
        const n = ids.length;
        let c = s.flip ? s.c.slice().reverse() : s.c;
        if (s.straight) {
          for (const id of ids.slice(0, 1)) feats.push({ type: 'Feature', properties: { st: 1, route: id, work: '', sn: R.get(id).num, color: R.get(id).color }, geometry: { type: 'LineString', coordinates: c } });
          continue;
        }
        // lane spacing: generous for a few routes, tighter for a trunk road, never under 1.4 px
        const gap = n <= 8 ? 3.4 : Math.max(2.1, 27 / n), w = Math.max(1.3, gap - 0.9);
        if (n > 1) feats.push({ type: 'Feature', properties: { st: 2, w: n * gap + 2.5 }, geometry: { type: 'LineString', coordinates: c } });
        // write each lane west-to-east so its label is never flipped (and so lands on its own lane)
        const westward = c[c.length - 1][0] < c[0][0];
        if (westward) c = c.slice().reverse();
        const long = len(c) > 120;
        ids.forEach((id, i) => {
          let off = (i - (n - 1) / 2) * gap;
          if (westward) off = -off;
          const r = R.get(id);
          feats.push({ type: 'Feature', properties: { st: 0, route: id, work: '', sn: r.num, color: r.color, off, w, lbl: long ? 1 : 0, toff: [0, off / 10.5] },
            geometry: { type: 'LineString', coordinates: c } });
        });
      }
      map.getSource('cables').setData({ type: 'FeatureCollection', features: feats });
    }
    map.on('moveend', refresh);
    map.on('zoomend', refresh);
    /* The nearest road segment to a point, for keeping live buses on the road (rtc.js): a
       grid of ~550 m cells over every segment's points, built on first use. Returns the
       segment's line (prepared: coords + cumulative metres), the along-distance and how far off. */
    let GRID = null;
    const K_ = 111320, CELL = 0.002;                     // ~220 m cells
    function prepSeg(s) {
      if (!s.P) { const c = s.c, cum = [0];
        for (let i = 1; i < c.length; i++) cum.push(cum[i - 1] + Math.hypot((c[i][0] - c[i - 1][0]) * K_ * Math.cos(c[i][1] * Math.PI / 180), (c[i][1] - c[i - 1][1]) * K_));
        s.P = { c, cum, key: s.a + '|' + s.b }; }
      return s.P;
    }
    function buildGrid() {
      // each cell lists the road pieces (segment, piece index) passing through it, sampled every ~100 m
      GRID = new Map();
      for (const s of SEG.values()) {
        if (s.straight) continue;
        const c = s.c;
        for (let i = 1; i < c.length; i++) {
          const a = c[i - 1], b = c[i], n = Math.max(1, Math.ceil(Math.hypot((b[0] - a[0]) * K_, (b[1] - a[1]) * K_) / 100));
          let last = '';
          for (let j = 0; j <= n; j++) {
            const x = a[0] + (b[0] - a[0]) * j / n, y = a[1] + (b[1] - a[1]) * j / n, k = Math.floor(x / CELL) + ',' + Math.floor(y / CELL);
            if (k === last) continue; last = k;
            (GRID.get(k) || GRID.set(k, []).get(k)).push(s, i);
          }
        }
      }
    }
    function nearest(p, maxOff = 40) {
      if (!built) return null;
      if (!GRID) buildGrid();
      const cx = Math.floor(p[0] / CELL), cy = Math.floor(p[1] / CELL), kx = K_ * Math.cos(p[1] * Math.PI / 180);
      let best = null, bo = maxOff * maxOff;
      for (let a = -1; a <= 1; a++) for (let b = -1; b <= 1; b++) {
        const L = GRID.get((cx + a) + ',' + (cy + b)); if (!L) continue;
        for (let j = 0; j < L.length; j += 2) {
          const s = L[j], i = L[j + 1], c = s.c;
          const ax = c[i - 1][0], ay = c[i - 1][1], dx = (c[i][0] - ax) * kx, dy = (c[i][1] - ay) * K_;
          const L2 = dx * dx + dy * dy, px = (p[0] - ax) * kx, py = (p[1] - ay) * K_;
          const u = L2 ? Math.max(0, Math.min(1, (px * dx + py * dy) / L2)) : 0;
          const ex = px - u * dx, ey = py - u * dy, d2 = ex * ex + ey * ey;
          if (d2 < bo) { const P = prepSeg(s); bo = d2; best = { P, d: P.cum[i - 1] + u * Math.sqrt(L2), off: Math.sqrt(d2), pt: [ax + (c[i][0] - ax) * u, ay + (c[i][1] - ay) * u] }; }
        }
      }
      return best;
    }

    /* ── routing along the bus network, for the route editor (editor.js) ──
       The network's stop-to-stop road segments are a graph (stops are its nodes); a route between two
       points follows the roads buses already run on: from each point to the nearest segment, then the
       shortest path along segments (Dijkstra). leg(a, b) is one stop pair: the segment itself when the
       two stops are already joined, a routed path otherwise. Null when the network does not connect them. */
    let G = null;
    function graph() {
      if (G) return G;
      G = new Map();
      for (const s of SEG.values()) {
        if (s.straight) continue;
        const P = prepSeg(s), L = P.cum[P.cum.length - 1];
        (G.get(s.a) || G.set(s.a, []).get(s.a)).push([s.b, L, s, 1]);    // 1: along s.c
        (G.get(s.b) || G.set(s.b, []).get(s.b)).push([s.a, L, s, -1]);   // -1: against it
      }
      return G;
    }
    const part = (P, from, to) => {                    // the segment's line between two along-distances
      const c = P.c, cum = P.cum, out = [], lo = Math.min(from, to), hi = Math.max(from, to);
      const at = d => { let i = 1; while (i < cum.length - 1 && cum[i] < d) i++; const u = (d - cum[i - 1]) / ((cum[i] - cum[i - 1]) || 1);
        return [c[i - 1][0] + (c[i][0] - c[i - 1][0]) * u, c[i - 1][1] + (c[i][1] - c[i - 1][1]) * u]; };
      out.push(at(lo)); for (let i = 0; i < c.length; i++) if (cum[i] > lo && cum[i] < hi) out.push(c[i]); out.push(at(hi));
      return from <= to ? out : out.reverse();
    };
    // a small binary heap for Dijkstra
    function heap() { const a = []; return { push(x) { a.push(x); let i = a.length - 1; while (i) { const p = (i - 1) >> 1; if (a[p][0] <= a[i][0]) break; [a[p], a[i]] = [a[i], a[p]]; i = p; } },
      pop() { const t = a[0], l = a.pop(); if (a.length) { a[0] = l; let i = 0; for (;;) { const L = 2 * i + 1, R = L + 1; let m = i; if (L < a.length && a[L][0] < a[m][0]) m = L; if (R < a.length && a[R][0] < a[m][0]) m = R; if (m === i) break; [a[m], a[i]] = [a[i], a[m]]; i = m; } } return t; }, get size() { return a.length; } }; }
    function route(p, q) {
      if (!built) return null;
      const A = nearest(p, 150), B = nearest(q, 150);
      if (!A || !B) return null;
      const straight = Math.hypot((q[0] - p[0]) * K_ * Math.cos(p[1] * Math.PI / 180), (q[1] - p[1]) * K_);
      if (A.P === B.P) return [p, ...part(A.P, A.d, B.d), q];                      // the same road
      const Gr = graph(), [a1, a2] = A.P.key.split('|'), [b1, b2] = B.P.key.split('|');
      const LA = A.P.cum[A.P.cum.length - 1], LB = B.P.cum[B.P.cum.length - 1];
      const dist = new Map(), prev = new Map(), H = heap(), limit = straight * 4 + 3000;
      // start: from p's point on its segment to either end of it
      for (const [n, c] of [[a1, A.d], [a2, LA - A.d]]) { if (c < (dist.get(n) ?? Infinity)) { dist.set(n, c); prev.set(n, null); H.push([c, n]); } }
      const goal = new Map([[b1, B.d], [b2, LB - B.d]]);                         // from either end of q's segment to q
      let best = Infinity, bestN = null;
      while (H.size) {
        const [d, n] = H.pop();
        if (d > (dist.get(n) ?? Infinity) || d > limit || d >= best) continue;
        if (goal.has(n) && d + goal.get(n) < best) { best = d + goal.get(n); bestN = n; }
        for (const [m, L, s, dir] of Gr.get(n) || []) {
          const nd = d + L;
          if (nd < (dist.get(m) ?? Infinity)) { dist.set(m, nd); prev.set(m, [n, s, dir]); H.push([nd, m]); }
        }
      }
      if (!bestN) return null;
      // walk back: segments from the start node to bestN
      const legs = []; for (let n = bestN; prev.get(n); n = prev.get(n)[0]) legs.push(prev.get(n));
      legs.reverse();
      const startNode = legs.length ? legs[0][0] : bestN;
      const out = [p, ...part(A.P, A.d, startNode === a1 ? 0 : LA)];
      for (const [, sg, dir] of legs) { const c = dir > 0 ? sg.c : sg.c.slice().reverse(); out.push(...c.slice(1)); }
      out.push(...part(B.P, bestN === b1 ? 0 : LB, B.d).slice(1), q);
      return out;
    }
    function leg(a, b, pa, pb) {
      // two stops already joined by a segment, unmoved: that segment, oriented a -> b
      if (a && b) { const s = SEG.get(a < b ? a + '|' + b : b + '|' + a);
        if (s && !s.straight) return s.a === a ? s.c : s.c.slice().reverse(); }
      return route(pa, pb);
    }

    /* The plain per-route lines stay for routes that have no cable (no segment shapes):
       a route line's opacity at street zoom is 0 once its route is drawn as a cable.
       Route features carry `cab: 1` for that (mark()). */
    const covered = new Set();
    const opacity = op => ['step', ['zoom'], op, MINZ, ['case', ['==', ['get', 'cab'], 1], 0, op]];
    const mark = fc => { for (const f of fc.features) { const id = f.properties[opts.routeKey || 'route']; if (covered.has(String(id))) f.properties.cab = 1; } return fc; };
    const dim = o => {
      if (!map.getLayer('cables')) return;
      map.setPaintProperty('cables', 'line-opacity', o);
      map.setPaintProperty('cable-labels', 'text-opacity', Math.min(1, o + 0.1));
      map.setPaintProperty('cables-case', 'line-opacity', o < 0.5 ? 0.5 : 0.92);
    };
    return { build, refresh, dim, opacity, mark, covered, nearest, route, leg, layers: ['cables', 'cable-labels', 'cables-straight'] };   // (the sleeve is not clickable)
  }
  return { create };
})();
