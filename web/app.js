/* Chennai MTC Bus Explorer — MapLibre GL (vector), busrouter.sg-style UI + chalo features.
   Multi-coloured route lines, route-number labels along the lines on zoom, busrouter route/stop cards. */
'use strict';

const DATA = 'data';
const DATA_V = '202611162108';   // Chennai One snapshot the data/ files were built from; bumps every cache   // self-contained: the enriched Chennai One layers, not /mtc/data
const dataJoin = name => fetch(`data/${name}?v=${DATA_V}`).then(r => r.ok ? r.json() : {}).catch(() => ({}));
const $ = id => document.getElementById(id);
const parseRoutes = v => (Array.isArray(v) ? v
  : (() => { try { const j = JSON.parse(v); if (Array.isArray(j)) return j; } catch (e) {} return String(v || '').replace(/[[\]"']/g, '').split(',').map(s => s.trim()).filter(Boolean); })()
  ).map(String).map(s => s.trim()).filter(Boolean);

/* route colours, the map pin and the padding that keeps a traced route clear of
   the cards are shared by every bus map: ../assets/busmap/busmap.js */
const { fitPad, hashColor, pinEl } = BUSMAP;
const catClass = c => c === 'MTC Small Bus' ? 'small' : c === 'CMRL Feeder' ? 'feeder' : '';


/* ── map ── */
const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/positron',
  center: [80.24, 13.06], zoom: 10.6, maxZoom: 18, attributionControl: false,
  // labels appear at once rather than fading in: a fade keeps the map repainting every frame for 300 ms after each
  // label placement, and the live buses ask for a frame more often than that, so the whole map was redrawn at the
  // screen's full rate even when nothing moved (106 frames a second on a phone at street zoom; 15 without it)
  fadeDuration: 0,
  pixelRatio: BUSMAP.pixelRatio(),   // by what the device can take (busmap.js TIER)
});
window.map = map;
map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: 'Routes/stops: MTC · CUMTA · Schedules: Chennai Open GTFS · live: MTC' }), 'bottom-right');
BUSMAP.quietAttribution(map);
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

let ROUTE_IDX = {}, STOPS = null, terminiMarkers = [], selMarkers = [], selStopMarker = null;
let TERMINI_FC = null;
const TERMINI_VISIBLE = new Set();
function renderTermini() {
  if (!TERMINI_FC) return;
  if (map.getZoom() < 11) { hideTermini(); return; }
  const b = map.getBounds(), keep = new Set();
  let made = 0;
  for (const f of TERMINI_FC.features) {
    const c = f.geometry.coordinates;
    if (c[0] < b.getWest() || c[0] > b.getEast() || c[1] < b.getSouth() || c[1] > b.getNorth()) continue;
    const key = c.join(',');
    keep.add(key);
    if (TERMINI_VISIBLE.has(key)) continue;
    if (made >= 120) continue;               // cap DOM pins per view
    made++;
    TERMINI_VISIBLE.add(key);
    const m = new maplibregl.Marker({ element: pinEl('#00454d', 0.85), anchor: 'bottom' }).setLngLat(c).addTo(map);
    m.getElement().addEventListener('click', ev => { ev.stopPropagation(); openTerminus(f); });
    terminiMarkers.push({ key, m });
  }
  terminiMarkers = terminiMarkers.filter(o => {
    if (keep.has(o.key)) return true;
    o.m.remove(); return false;
  });
}
function hideTermini() { terminiMarkers.forEach(o => o.m.remove()); terminiMarkers = []; TERMINI_VISIBLE.clear(); }
let selectedRoute = null, dataReady = false;
let CABLES = null, ROUTES_FC = null, cablesAsked = false;
/* The cables need every working's stop list and the road shape of every stop-to-stop
   segment; both are fetched the first time the map reaches street zoom. */
function buildCables() {
  if (cablesAsked || !ROUTES_FC || map.getZoom() < 12.3) return;
  cablesAsked = true;
  const rs = ROUTE_STOPS !== undefined ? Promise.resolve(ROUTE_STOPS) : dataJoin('route_stops.json').then(j => (ROUTE_STOPS = j));
  Promise.all([rs, fetch(`${DATA}/seg_geo.json?v=${DATA_V}`).then(r => r.ok ? r.json() : null).catch(() => null)]).then(([RS, G]) => {
    if (!RS || !G) return;
    const SEG = new Map(G.features.map(f => [f.properties.k, f.geometry.coordinates]));
    const routes = Object.values(ROUTE_IDX).map(e => ({ id: e.sn, num: e.sn, color: e.color, trips: e.trips,
      works: e.works.map(w => ({ id: w.properties.code, stops: ((RS[w.properties.code] || {}).d || [{ s: [] }])[0].s.map(x => x[0]) })) }));
    CABLES.build(routes, (a, b) => SEG.get(a + '|' + b) || null);
    CABLES.mark(ROUTES_FC);
    map.getSource('routes').setData(ROUTES_FC);
    map.setPaintProperty('routes', 'line-opacity', map.getPaintProperty('routes', 'line-opacity'));
  });
}
/* Every place that dims or restores the route lines (picking a route, a stop, a bus in
   live.js) sets 'routes' line-opacity; the cables follow it, and a route's plain line
   gives way to its cable at street zoom. */
{
  const spp = map.setPaintProperty.bind(map);
  map.setPaintProperty = (id, prop, v, o) => {
    if (CABLES && id === 'routes' && prop === 'line-opacity') {
      const op = typeof v === 'number' ? v : Array.isArray(v) && typeof v[2] === 'number' ? v[2] : 0.7;
      CABLES.dim(op >= 0.7 ? 0.9 : op + 0.02);
      v = CABLES.opacity(op);
    }
    return spp(id, prop, v, o);
  };
}
const enabled = { routes: true, labels: true, stops: true, metro: true, termini: true };

function stopsOnRoute(name) { return STOPS ? STOPS.features.filter(f => parseRoutes(f.properties['route name']).includes(name)) : []; }
function orderedStops(name) {
  const e = ROUTE_IDX[name]; if (!e) return [];
  const stops = stopsOnRoute(name), C = e.coords;
  stops.forEach(s => { const sc = s.geometry.coordinates; let bi = 0, bd = Infinity;
    for (let i = 0; i < C.length; i++) { const dx = C[i][0] - sc[0], dy = C[i][1] - sc[1], d = dx * dx + dy * dy; if (d < bd) { bd = d; bi = i; } }
    s.__ord = bi; });
  const sorted = stops.slice().sort((a, b) => a.__ord - b.__ord);
  // the stop data has many duplicate-named poles at one location → collapse to one per name, keep route order
  const seen = new Set(), out = [];
  for (const s of sorted) { const n = s.properties['Stop Name'] || 'stop'; if (seen.has(n)) continue; seen.add(n); out.push(s); }
  return out;
}

map.on('style.load', () => {   // 'load' waits on basemap tiles that never settle on this host; 'style.load' is reliable
  BUSMAP.warm(map);   // positron warmed toward the busrouter cream

  // sources
  map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addSource('metro',  { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addSource('stops',  { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  // street zoom: a pin per side of the road (bus_stop_sides.geojson, dataset/fix_stop_sides.py), not one per place
  map.addSource('stop-sides', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addSource('sel',    { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

  // metro under bus routes
  map.addLayer({ id: 'metro', type: 'line', source: 'metro',
    paint: { 'line-color': ['coalesce', ['get', 'color'], '#7c3aed'], 'line-width': 2.2, 'line-opacity': 0.75, 'line-dasharray': [1, 2] } });
  // bus routes — multi-coloured per route
  map.addLayer({ id: 'routes', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1, 14, 2.6, 16, 3.4], 'line-opacity': 0.7 } });
  // selected route(s) highlight — a static glowing line (blurred halo under a crisp line; no animation → no freeze on big hubs)
  map.addLayer({ id: 'sel-glow', type: 'line', source: 'sel', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': 14, 'line-blur': 8, 'line-opacity': 0.55 } });
  map.addLayer({ id: 'sel-line', type: 'line', source: 'sel', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': 5, 'line-opacity': 0.98 } });
  map.addLayer({ id: 'route-labels', type: 'symbol', source: 'routes', minzoom: 13.2, filter: ['!=', ['get', 'cab'], 1],
    /* Label with the real service number. `route_name` carries a generated
       "(2)"/"(3)" suffix to keep each working uniquely addressable, but painting
       that on the map invents bus numbers that do not exist — "S60 (2)" reads as
       a service, and MTC really does have a route called 18K(SEC), so brackets
       are not even a safe tell. All four S60 workings are S60. */
    // long names (intercity and special services) cut to 14 characters on the map; the card has them in full
    layout: { 'symbol-placement': 'line', 'text-field': ['case', ['>', ['length', ['get', 'sn']], 14], ['concat', ['slice', ['get', 'sn'], 0, 13], '…'], ['get', 'sn']], 'text-size': 11.5,
      'text-font': ['Noto Sans Bold'], 'symbol-spacing': 220, 'text-allow-overlap': false, 'text-padding': 4 },
    paint: { 'text-color': ['get', 'color'], 'text-halo-color': '#fff', 'text-halo-width': 1.8 } });
  // bus stops as points
  // street zoom: routes as cables, one lane each, side by side (../assets/busmap/cables.js)
  CABLES = BUSCABLES.create(map, { minzoom: 13, before: 'sel-glow', routeKey: 'sn' });
  map.addLayer({ id: 'stops', type: 'circle', source: 'stops', minzoom: 12, maxzoom: 15.5,
    paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 1.6, 14, 3, 16, 5],
      'circle-color': '#f01b48', 'circle-stroke-color': '#fff', 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 12, 0.4, 15, 1.2] } });
  // street zoom: a stop is a red square with a bus on it, big enough for a finger
  if (!map.hasImage('stop-sq')) {
    const S = 44, cv = document.createElement('canvas'); cv.width = cv.height = S;
    const g = cv.getContext('2d'), rr = (x, y, w, h, r) => { g.beginPath(); g.roundRect(x, y, w, h, r); };
    rr(2, 2, S - 4, S - 4, 9); g.fillStyle = '#f01b48'; g.fill(); g.lineWidth = 3; g.strokeStyle = '#fff'; g.stroke();
    // the bus, from the front: body, windscreen, lamps, wheels
    rr(12, 9, 20, 22, 4); g.fillStyle = '#fff'; g.fill();
    rr(14.5, 12, 15, 9, 1.5); g.fillStyle = '#f01b48'; g.fill();
    g.fillStyle = '#f01b48'; g.beginPath(); g.arc(16.5, 26, 1.6, 0, 7); g.arc(27.5, 26, 1.6, 0, 7); g.fill();
    g.fillStyle = '#fff'; rr(13.5, 31, 4, 4, 1); g.fill(); rr(26.5, 31, 4, 4, 1); g.fill();
    map.addImage('stop-sq', g.getImageData(0, 0, S, S), { pixelRatio: 2 });
  }
  map.addLayer({ id: 'stop-icons', type: 'symbol', source: 'stop-sides', minzoom: 15.5,
    layout: { 'icon-image': 'stop-sq', 'icon-size': ['interpolate', ['linear'], ['zoom'], 15.5, 0.8, 17, 1, 19, 1.25],
      'icon-allow-overlap': true, 'icon-ignore-placement': true } });
  // stop name labels (more info on zoom)
  map.addLayer({ id: 'stop-labels', type: 'symbol', source: 'stop-sides', minzoom: 15.5,
    layout: { 'text-field': ['get', 'Stop Name'], 'text-size': 11, 'text-font': ['Noto Sans Regular'],
      'text-offset': [0, 1.5], 'text-anchor': 'top', 'text-optional': true },
    paint: { 'text-color': '#444', 'text-halo-color': '#fff', 'text-halo-width': 1.3 } });

  /* Which way the bus runs, drawn on the map itself.

     A text glyph was the first attempt and it was too faint to read against a
     5px line — '\u203A' at 20px is a thin hairline. A sprite icon is crisp at
     any size, so the arrowhead is painted into a canvas and registered once.
     With `icon-rotation-alignment: map` the icon's +x axis follows the line
     direction, so a triangle drawn pointing right points the way the bus goes. */
  if (!map.hasImage('route-arrow')) {
    const S = 34, cv = document.createElement('canvas');
    cv.width = cv.height = S;
    const g = cv.getContext('2d');
    g.beginPath();
    g.moveTo(S * 0.78, S * 0.5);
    g.lineTo(S * 0.26, S * 0.20);
    g.lineTo(S * 0.26, S * 0.80);
    g.closePath();
    g.fillStyle = '#fff';
    g.strokeStyle = 'rgba(0,0,0,.35)';
    g.lineWidth = 2.5; g.lineJoin = 'round';
    g.stroke(); g.fill();
    map.addImage('route-arrow', g.getImageData(0, 0, S, S), { pixelRatio: 2 });
  }
  map.addLayer({ id: 'sel-arrows', type: 'symbol', source: 'sel', minzoom: 9,
    layout: { 'symbol-placement': 'line',
      'symbol-spacing': ['interpolate', ['linear'], ['zoom'], 9, 60, 15, 120],
      'icon-image': 'route-arrow',
      'icon-size': ['interpolate', ['linear'], ['zoom'], 9, 0.55, 15, 0.85],
      'icon-rotation-alignment': 'map', 'icon-pitch-alignment': 'map',
      'icon-keep-upright': false, 'icon-allow-overlap': true, 'icon-ignore-placement': true },
    paint: { 'icon-opacity': 0.98 } });
  // route-number labels along the lines (chalo feature) — GL collision handles density

  // interactions
  const ptr = id => { map.on('mouseenter', id, () => map.getCanvas().style.cursor = 'pointer'); map.on('mouseleave', id, () => map.getCanvas().style.cursor = ''); };
  // a live bus or an observed-speed segment drawn on top takes the click (live.js, traffic.js)
  const underBus = e => (window.liveBusAt && window.liveBusAt(e.point)) || (window.trafficAt && window.trafficAt(e.point));
  // a stop takes the click from the route lines under it, within a finger's reach (the lines are
  // wide and many; a click used to open the route even on the stop's dot)
  const REACH = matchMedia('(pointer: coarse)').matches ? 18 : 10;
  const stopNear = p => {
    const ids = ['stops', 'stop-icons'].filter(id => map.getLayer(id));
    const fs = map.queryRenderedFeatures([[p.x - REACH, p.y - REACH], [p.x + REACH, p.y + REACH]], { layers: ids });
    let best = null, bd = Infinity;
    for (const f of fs) {
      const q = map.project(f.geometry.coordinates), d = Math.hypot(q.x - p.x, q.y - p.y);
      if (d < bd) { bd = d; best = f; }
    }
    return best;
  };
  map.on('click', e => { if (underBus(e)) return; const f = stopNear(e.point); if (f) openStop(f); });
  ptr('stops'); ptr('stop-icons');
  const pickFeature = e => {
    if (underBus(e) || stopNear(e.point)) return;
    const p = e.features[0].properties;
    selectRoute(p.sn || p.route_name, p.code);   // open the exact working clicked
  };
  map.on('click', 'routes', pickFeature); ptr('routes');
  map.on('click', 'route-labels', pickFeature); ptr('route-labels');
  for (const id of CABLES.layers) { map.on('click', id, e => { if (!underBus(e) && !stopNear(e.point)) selectRoute(e.features[0].properties.route); }); ptr(id); }
  map.on('zoomend', buildCables);
  // the per-side pins (~0.8 MB) only once someone reaches street zoom
  let sidesAsked = false;
  const sides = () => { if (sidesAsked || map.getZoom() < 15) return; sidesAsked = true;
    fetch(`data/bus_stop_sides.geojson?v=${DATA_V}s1`).then(r => r.ok ? r.json() : null).then(fc => fc && map.getSource('stop-sides').setData(fc)).catch(() => { sidesAsked = false; }); };
  map.on('zoomend', sides); map.once('idle', sides);

  loadData();
});

/* Routes in two steps. At start, data/routes_lite.geojson: every working, but only each route number's main
   working with its road, simplified at 4 m (assets/busmap/server/routes_lite.py; a fifth to a half of the full
   file). Then data/mtc_routes.geojson, every working's road, patched in place: once the map has settled, or at
   street zoom, or when a picked route needs another working. A basic phone (2 GB or less, data saver) waits for
   one of the last two. Without a lite file, the full one loads at start as before. */
const LOWEND = (navigator.deviceMemory || 4) <= 2 || !!(navigator.connection || {}).saveData;
let ROUTES_SHOWN = null, ROUTES_FULL = null, routesFullDone = false;
function routesLite() {
  return dataJoin('routes_lite.geojson').then(fc => {
    if (!(fc && fc.features && fc.features.length)) { routesFullDone = true; return dataJoin('mtc_routes.geojson'); }
    map.once('idle', () => setTimeout(() => { if (!LOWEND) routesFull(); }, 1200));
    map.on('zoomend', () => { if (map.getZoom() >= 14) routesFull(); });
    return fc;
  });
}
function routesFull() {
  if (routesFullDone) return Promise.resolve();
  return ROUTES_FULL || (ROUTES_FULL = dataJoin('mtc_routes.geojson').then(full => {
    if (!full || !full.features || !ROUTES_SHOWN) return;
    const key = p => (p.route_name || '') + '|' + (p.code || '');
    const shown = new Map(ROUTES_SHOWN.features.map(f => [key(f.properties), f]));
    for (const f of full.features) {
      const s = shown.get(key(f.properties));
      if (s) s.geometry = f.geometry;
      else ROUTES_SHOWN.features.push(f);         // published after the lite file was made: drawn, if not yet searchable
    }
    routesFullDone = true;
    Object.values(ROUTE_IDX).forEach(e => e.feats && e.feats[0] && useWorking(e, e.feats[0]));
    if (map.getSource('routes')) map.getSource('routes').setData(ROUTES_SHOWN);
    if (selectedRoute && ROUTE_IDX[selectedRoute] && map.getSource('sel')) map.getSource('sel').setData({ type: 'FeatureCollection', features: ROUTE_IDX[selectedRoute].feats });
    document.dispatchEvent(new Event('routes-full'));
  }));
}

/* ── load + index data ── */
function loadData() {
  routesLite().then(fc => {
    ROUTES_SHOWN = fc;
    /* One entry per SERVICE NUMBER, not per working.
       CUMTA publishes scheduled workings, not routes: 4,283 workings under only
       1,099 service numbers. 18A alone has 16 — both directions of the full
       Royapuram–Kilambakkam run plus short-turns to Tambaram West, Keelkattalai,
       Saidapet, Cement Road and Vallalar Nagar, between 1 and 34 trips a day.
       Listing those as sixteen "routes" is wrong; Moovit shows one 18A. So group
       by `sn` and keep the workings inside. */
    fc.features.forEach(f => {
      const p = f.properties, sn = p.sn || p.route_name; if (!sn) return;
      p.color = hashColor(sn);
      const e = ROUTE_IDX[sn] || (ROUTE_IDX[sn] = { name: sn, sn, category: p.category, color: p.color, coords: [], feats: [], works: [] });
      e.works.push(f);
      const g = f.geometry; const cs = g.type === 'MultiLineString' ? g.coordinates.flat() : g.coordinates;
      e.coords.push(...cs);
    });
    /* Show exactly ONE working at a time.
       Drawing a working together with its return leg looks like the route loops:
       21G's two directions are 49.0 km and 43.9 km, so 5 km of them runs on
       different streets, and superimposed that reads as knots around the
       terminus rather than an out-and-back. The card lists every working and
       switches between them, which is also how busrouter.sg does it. */
    Object.values(ROUTE_IDX).forEach(e => {
      e.works.sort((a, b) => (b.properties.nstops || 0) - (a.properties.nstops || 0)
                          || (b.properties.trips || 0) - (a.properties.trips || 0));
      e.trips = e.works.reduce((n, w) => n + (w.properties.trips || 0), 0);
      useWorking(e, e.works[0]);
    });
    ROUTES_FC = fc;
    map.getSource('routes').setData(fc);
    buildCables();
  });
  fetch(`${DATA}/metro_lines.geojson`).then(r => r.json()).then(fc => map.getSource('metro').setData(fc));
  dataJoin('bus_termini.geojson').then(fc => {
    TERMINI_FC = fc;                          // lazy viewport-based pin rendering
    map.on('moveend', renderTermini);
    renderTermini();
  });
  dataJoin('bus_stops.geojson').then(fc => {
    STOPS = fc; map.getSource('stops').setData(fc); dataReady = true; $('loading').classList.add('done');
  });
}

/* Point a service at one of its workings: the drawn feature, the coordinates
   that the stop ordering / terminus pins / fitBounds all read, and the labels.
   Keeping these in lockstep matters — coords used to be the union of every
   working, so the pins and the stop list described a different line than the
   one on screen. */
function useWorking(e, w) {
  const p = w.properties, g = w.geometry;
  e.feats = [w];
  e.coords = g.type === 'MultiLineString' ? g.coordinates.flat() : g.coordinates;
  e.code = p.code; e.source = p.source; e.dest = p.destinatio; e.nstops = p.nstops;
  return e;
}

/* ── cards ── */
function showCard() { $('card').classList.add('open'); document.body.classList.add('card-open'); setSearch(false); }
function closeCard() { $('card').classList.remove('open'); document.body.classList.remove('card-open'); clearSelection(); if (selStopMarker) { selStopMarker.remove(); selStopMarker = null; } }
$('card-close').addEventListener('click', closeCard);
function tagHTML(name, cat) {
  // "21G (2)" -> base + a lighter suffix, so the variant reads as secondary
  const m = /^(.*?)\s*\((\d+)\)$/.exec(String(name || ''));
  const label = m ? `${m[1]}<span class="vsuffix">(${m[2]})</span>` : name;
  // P570S etc. are MTC Premium: gold, like the buses
  return `<span class="service-tag ${/^P\d/.test(String(name || '')) ? 'premium' : catClass(cat)}" title="${String(name || '').replace(/"/g, '&quot;')}">${label}</span>`;
}

function clearSelection() {
  selectedRoute = null;
  if (map.getSource('sel')) map.getSource('sel').setData({ type: 'FeatureCollection', features: [] });
  selMarkers.forEach(m => m.remove()); selMarkers = [];
  if (map.getLayer('routes')) map.setPaintProperty('routes', 'line-opacity', 0.7);
}

function selectRoute(name, workCode) {
  const e = ROUTE_IDX[name]; if (!e) return;
  selectedRoute = name;
  const pick = workCode && e.works.find(x => x.properties.code === workCode);
  // another working's road comes with the full file: the main working now, this one when it lands
  const bare = pick && !routesFullDone && !((pick.geometry || {}).coordinates || []).length;
  if (bare) routesFull().then(() => { if (selectedRoute === name) selectRoute(name, workCode); });
  useWorking(e, (!bare && pick) || e.works[0]);
  // highlight: dim others, draw this route bold via 'sel' source
  map.getSource('sel').setData({ type: 'FeatureCollection', features: e.feats });
  map.setPaintProperty('routes', 'line-opacity', 0.18);
  map.setPaintProperty('sel-line', 'line-width', 5);
  map.setPaintProperty('sel-glow', 'line-width', 15); map.setPaintProperty('sel-glow', 'line-blur', 9);
  map.setLayoutProperty('sel-arrows', 'visibility', 'visible');
  selMarkers.forEach(m => m.remove()); selMarkers = [];

  const stops = orderedStops(name);
  if (stops.length) {
    const a = stops[0].geometry.coordinates, b = stops[stops.length - 1].geometry.coordinates;
    selMarkers.push(new maplibregl.Marker({ element: pinEl('#16a34a', 0.95), anchor: 'bottom' }).setLngLat(a).addTo(map));
    selMarkers.push(new maplibregl.Marker({ element: pinEl('#f01b48', 0.95), anchor: 'bottom' }).setLngLat(b).addTo(map));
  }

  $('card-title').innerHTML = `${tagHTML(name, e.category)}<span class="ttl">`
    + `<span>${e.source || '?'}</span>`
    + `<span><i class="ar">\u2192</i>${e.dest || '?'}</span></span>`;
  const nw = (e.works || []).length;
  // count the working actually drawn, not the union of all of them — the union
  // said "110 stops" for a line showing 48, which is the sort of mismatch that
  // makes the whole card untrustworthy.
  const shown = e.nstops || stops.length;
  $('card-sub').innerHTML = `${e.category || 'MTC Route'} &middot; ${shown} stops`
    + (nw > 1 ? ` &middot; ${nw} workings` : '') + (e.trips ? ` &middot; ${e.trips} trips/day` : '');
  $('card-body').innerHTML =
    `<button class="timings-btn" id="timings-btn">Show route timings</button><div id="timings-box"></div>` +
    `<div id="stops-wrap"></div>`;
  // by WORKING code, not service number: every working of 21G used to show the
  // parent service's 02:00–23:15 / 88 trips, which is exactly the fact that
  // hides what the short workings are for
  $('timings-btn').addEventListener('click', () => toggleTimings(e.code));
  renderWorkings(name, e);
  renderStops(name, e);
  paintTimings(e.code);          // honour the remembered open/closed state
  showCard();
  try {
    const b = new maplibregl.LngLatBounds();
    e.coords.forEach(c => b.extend(c));
    map.fitBounds(b, { padding: fitPad(), maxZoom: 14.5, duration: 800 });
  } catch (err) {}
}
window.selectRoute = selectRoute;

/* The page chrome every bus map shares (../assets/busmap/busmap.js): search rolls
   out from its button and away again, a card's header folds it, the layers menu,
   the 3D tilt, "my location" and the lift above the on-screen keyboard. */
const { setSearch } = BUSMAP.chrome(map);
BUSMAP.dark(map);                 // "Dark map" in the layers menu

/* The workings that make up one service. A rider cares that 18A runs Royapuram
   to Kilambakkam; the 1-trip-a-day Cement Road short-turn still belongs to 18A,
   so list them rather than promoting each to its own route. */
function renderWorkings(name, e) {
  const works = e.works || [];
  if (works.length < 2) return;
  const cur = e.code;
  const rows = works.map(w => {
    const p = w.properties;
    return `<div class="it wk${p.code === cur ? ' on' : ''}" data-w="${p.code}">`
      + `<span><i class="wkac" data-for="${p.code}"></i>${p.source} <span style="color:var(--muted)">→</span> ${p.destinatio}</span>`
      + `<span class="wkmeta">${p.nstops} stops${p.trips ? ` · ${p.trips}/day` : ''}</span></div>`;
  }).join('');
  $('card-body').insertAdjacentHTML('afterbegin',
    `<div class="seclbl">${works.length} workings on this service</div><div class="wklist">${rows}</div>`);
  $('card-body').querySelectorAll('[data-w]').forEach(el =>
    el.addEventListener('click', () => selectRoute(name, el.dataset.w)));
  markAC();
}

/* A snowflake beside the workings that run AC trips, so the AC service is
   findable without opening each one's timings in turn — on 21G it is the two
   17-stop workings, not the 50-stop mainline ones. */
function markAC() {
  const paint = () => document.querySelectorAll('.wkac[data-for]').forEach(el => {
    const t = TIMINGS && TIMINGS[el.dataset.for];
    const n = t && t.tiers && t.tiers.AC;
    if (!n) return;
    el.innerHTML = tierIcon('AC');
    el.title = `${n} AC trip${n === 1 ? '' : 's'} a day`;
    el.classList.add('on');
  });
  if (TIMINGS !== undefined) return paint();
  // prefetch: the card is open, the user is one tap away from wanting this
  dataJoin('timings.json')
    .then(j => { TIMINGS = j; paint(); }).catch(() => { TIMINGS = {}; });
}

/* route stop diagram — GTFS ordered stops (both directions, with codes); falls back to geometry order */
let ROUTE_STOPS;
function renderStops(name, e) {
  const wrap = $('stops-wrap'); if (!wrap) return;
  const wire = () => wrap.querySelectorAll('.rail li, .stopline li, .rail .capname[data-x]').forEach(li => li.addEventListener('click', () => {
    if (li.dataset.x) map.flyTo({ center: [+li.dataset.x, +li.dataset.y], zoom: 16, duration: 600 });
  }));
  const draw = () => {
    const rs = ROUTE_STOPS && (ROUTE_STOPS[e.code] || ROUTE_STOPS[name]);
    if (!(rs && rs.d && rs.d.length)) {
      wrap.innerHTML = '<div class="tmsg" style="padding:12px 2px">A stop-by-stop list isn’t published for this route. The line is traced on the map — tap any stop on it to see its services.</div>';
      return;
    }
    const dir = rs.d[0];
    // the return working: its ends are this working's ends swapped. CUMTA writes
    // the same terminus in different cases ("TAMBARAM WEST BUS STAND" / "Tambaram
    // West Bus Stand"), so compare names loosely, then by place (within 300 m)
    const nk = x => (x || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    const near = (a, b) => a && b && Math.hypot((a[3] - b[3]) * 108000, (a[2] - b[2]) * 111000) < 300;
    const cands = (e.works || []).filter(w => w.properties.code !== e.code)
      .map(w => ROUTE_STOPS[w.properties.code]).filter(o => o && o.d && o.d[0] && o.d[0].s && o.d[0].s.length > 1);
    const back = cands.find(o => nk(o.d[0].e) === nk(dir.o) && nk(o.d[0].o) === nk(dir.e))
      || cands.find(o => near(o.d[0].s[o.d[0].s.length - 1], dir.s[0]) && near(o.d[0].s[0], dir.s[dir.s.length - 1]));
    // the return leg runs the other way; reversing it puts each stop beside the
    // outbound stop it actually pairs with
    const rev = back ? back.d[0].s.slice().reverse() : null;
    // the diagram itself is shared by every bus map (busmap.js): two rails joined by
    // U-turns at the termini, or one straight line for a one-way service
    wrap.innerHTML = BUSMAP.railHTML(dir.s, rev, rev ? {} : { note: 'One-way service — CUMTA publishes no return working for it.' });
    wire();
  };
  if (ROUTE_STOPS !== undefined) return draw();
  wrap.innerHTML = '<div class="tmsg" style="padding:10px 2px">Loading stops…</div>';
  dataJoin('route_stops.json').then(j => { ROUTE_STOPS = j; draw(); }).catch(() => { ROUTE_STOPS = {}; draw(); });
}

/* timings — loads from data/timings.json (route_name keyed) if present */
let TIMINGS = undefined;
/* Board type per working.

   `serviceTierType` is the only vehicle classification the feed carries, and it
   is stated on every trip, so a working that runs 10 AC trips and 2 EXECUTIVE
   ones says so exactly. It describes the BOARD — what MTC sells — not the
   vehicle's propulsion: nothing in the source distinguishes an electric bus
   from a diesel one.

   Icons are Lucide (ISC), inlined rather than loaded, and drawn in the same
   2px round-cap stroke as the map buttons. */
const TIER = {
  AC:        { label: 'AC', ac: true, cls: 'ac',
    d: ['m10 20-1.25-2.5L6 18','M10 4 8.75 6.5 6 6','m14 20 1.25-2.5L18 18','m14 4 1.25 2.5L18 6',
        'm17 21-3-6h-4','m17 3-3 6 1.5 3','M2 12h6.5L10 9','m20 10-1.5 2 1.5 2','M22 12h-6.5L14 15',
        'm4 10 1.5 2L4 14','m7 21 3-6-1.5-3','m7 3 3 6h4'] },
  NON_AC:    { label: 'Non-AC', cls: 'plain',
    d: ['M10.827 16.379a6.082 6.082 0 0 1-8.618-7.002l5.412 1.45a6.082 6.082 0 0 1 7.002-8.618l-1.45 5.412a6.082 6.082 0 0 1 8.618 7.002l-5.412-1.45a6.082 6.082 0 0 1-7.002 8.618l1.45-5.412Z',
        'M12 12v.01'] },
  EXECUTIVE: { label: 'Deluxe', cls: 'plain',
    d: ['M19 9V6a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v3',
        'M3 16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5a2 2 0 0 0-4 0v1.5a.5.5 0 0 1-.5.5h-9a.5.5 0 0 1-.5-.5V11a2 2 0 0 0-4 0z',
        'M5 18v2','M19 18v2'] },
  EXPRESS:   { label: 'Express', cls: 'plain', d: ['m6 17 5-5-5-5','m13 17 5-5-5-5'] },
  SPECIAL:   { label: 'Special', cls: 'plain',
    d: ['M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z'] },
  ORDINARY:  { label: 'Ordinary', cls: 'plain',
    d: ['M8 6v6','M15 6v6','M2 12h19.6',
        'M18 18h3s.5-1.7.8-2.8c.1-.4.2-.8.2-1.2 0-.4-.1-.8-.2-1.2l-1.4-5C20.1 6.8 19.1 6 18 6H4a2 2 0 0 0-2 2v10h3'] },
};

function tierIcon(key) {
  const t = TIER[key] || TIER.ORDINARY;
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">`
    + t.d.map(d => `<path d="${d}"/>`).join('') + '</svg>';
}

/* Chips, most-run first. The count is trips per day on this working, so a
   reader can tell "mostly AC" from "one AC trip". */
function tierChips(tiers) {
  const rows = Object.entries(tiers || {}).sort((a, b) => b[1] - a[1]);
  if (!rows.length) return '';
  return `<div class="tiers">` + rows.map(([k, n]) => {
    const t = TIER[k] || { label: k.toLowerCase(), cls: 'plain' };
    return `<span class="tier ${t.cls}" title="${n} trip${n === 1 ? '' : 's'} a day on the ${t.label} board">`
      + tierIcon(k) + `${t.label}<b>${n}</b></span>`;
  }).join('') + '</div>';
}

/* When a working runs tells you what it is for (a depot run, a peak-only service), and
   the 24-hour day/night dial of its departures, gold while the sun is up in Chennai:
   both shared by every bus map (../assets/busmap/busmap.js). */
const dayShape = BUSMAP.dayShape;
const SUN_AT = [13.0827, 80.2707];
const hoursClock = h => BUSMAP.hoursClock(h, { lat: SUN_AT[0], lon: SUN_AT[1], place: 'Chennai' });

/* Whether the timetable is open is the rider's choice, not the card's: it used
   to reset to closed every time another working was picked, so comparing two
   workings meant re-opening it each time. The preference is remembered across
   cards and across reloads, and only a tap on the button changes it. */
let TIMINGS_OPEN = (() => {
  try { return localStorage.getItem('mtcTimingsOpen') === '1'; } catch (e) { return false; }
})();

function timingsHTML(code) {
  const t = TIMINGS && TIMINGS[code];
  if (!t) {
    // distinguish "this one working has no timetable" from "the whole file is
    // the wrong shape", which is what a stale cached copy looks like
    const stale = TIMINGS && Object.keys(TIMINGS).length &&
                  !Object.values(TIMINGS)[0].hours;
    return stale
      ? '<div class="tmsg">Timetable data is out of date in this browser — reload the page to refresh it.</div>'
      : '<div class="tmsg">No timetable is published for this working.</div>';
  }
  const shape = t.hours ? dayShape(t.hours) : '';
  return `<div class="trow"><span>First bus</span><b>${t.first || '—'}</b></div>` +
    `<div class="trow"><span>Last bus</span><b>${t.last || '—'}</b></div>` +
    `<div class="trow"><span>Frequency</span><b>${t.freq || '—'}</b></div>` +
    `<div class="trow"><span>Trips / day</span><b>${t.trips != null ? t.trips : '—'}</b></div>` +
    tierChips(t.tiers) +
    (t.hours ? `<div class="hwrap"><div class="hlbl">Departures by hour${shape ? ` · <b>${shape}</b>` : ''}</div>${hoursClock(t.hours)}</div>` : '') +
    `<div class="tmsg">This working only \u00b7 GTFS timetable via Chennai One (CUMTA / MTC / CMRL / SR).</div>` +
    `<div class="tmsg" style="border-top:1px solid var(--line);margin-top:2px;padding-top:8px">Schedule: <a href="https://github.com/ungalsoththu/ChennaiGTFS" target="_blank" rel="noopener" style="color:var(--red);font-weight:600">Chennai Open GTFS</a></div>`;
}

/* Draw the box to match the remembered state. Called on every card build, so a
   card opened while the preference is "open" arrives with the timetable already
   showing rather than needing a second tap. */
function paintTimings(code) {
  const box = $('timings-box'), btn = $('timings-btn');
  if (!box || !btn) return;
  btn.textContent = TIMINGS_OPEN ? 'Hide route timings' : 'Show route timings';
  if (!TIMINGS_OPEN) { box.innerHTML = ''; return; }
  if (TIMINGS === undefined) {
    box.innerHTML = '<div class="tmsg">Loading…</div>';
    dataJoin('timings.json')
      .then(j => { TIMINGS = j; if (TIMINGS_OPEN) box.innerHTML = timingsHTML(code); markAC(); })
      .catch(() => { TIMINGS = {}; box.innerHTML = timingsHTML(code); });
    return;
  }
  box.innerHTML = timingsHTML(code);
}

function toggleTimings(code) {
  TIMINGS_OPEN = !TIMINGS_OPEN;
  try { localStorage.setItem('mtcTimingsOpen', TIMINGS_OPEN ? '1' : '0'); } catch (e) {}
  paintTimings(code);
}

function openStop(f) {
  if (selStopMarker) selStopMarker.remove();
  const c = f.geometry.coordinates;
  selStopMarker = new maplibregl.Marker({ element: pinEl('#f01b48', 1), anchor: 'bottom' }).setLngLat(c).addTo(map);
  const rs = [...new Set(parseRoutes(f.properties['route name']))];
  $('card-title').innerHTML = `<span class="ttl"><span>${f.properties['Stop Name'] || 'Stop'}</span></span>`;
  // several feed records for one physical stop are merged into this pin; show the
  // other names it goes by so a rider searching either spelling still recognises it
  const alt = f.properties.alt;
  $('card-sub').innerHTML = `Bus stop &middot; ${rs.length} route${rs.length === 1 ? '' : 's'}`
    + (alt ? `<div style="margin-top:3px">also known as <b>${alt}</b></div>` : '');
  $('card-body').innerHTML = `<div class="seclbl">Routes passing here</div>` +
    (rs.length ? `<div class="pillwrap">${rs.map(r => tagHTML(r, ROUTE_IDX[r] && ROUTE_IDX[r].category)).join('')}</div>` : '<span style="color:var(--muted)">No route info</span>');
  $('card-body').querySelectorAll('.service-tag').forEach((t, i) => t.addEventListener('click', () => selectRoute(rs[i])));
  showCard();
  // highlight every route that passes through this stop, then zoom out to reveal them (busrouter behaviour)
  selMarkers.forEach(m => m.remove()); selMarkers = [];
  selectedRoute = null;
  const feats = [], b = new maplibregl.LngLatBounds(); b.extend(c);
  rs.forEach(rn => { const e = ROUTE_IDX[rn]; if (e) { feats.push(...e.feats); e.coords.forEach(p => b.extend(p)); } });
  map.getSource('sel').setData({ type: 'FeatureCollection', features: feats });
  map.setPaintProperty('routes', 'line-opacity', feats.length ? 0.1 : 0.7);
  const many = rs.length > 4;
  map.setPaintProperty('sel-line', 'line-width', many ? 2.6 : 5);
  map.setPaintProperty('sel-glow', 'line-width', many ? 7 : 15); map.setPaintProperty('sel-glow', 'line-blur', many ? 4 : 9);
  // chevrons only make sense on a single traced line; on a fan of routes from
  // one terminus they overlap into noise
  map.setLayoutProperty('sel-arrows', 'visibility', many ? 'none' : 'visible');
  const pad = fitPad();
  if (feats.length) { try { map.fitBounds(b, { padding: pad, maxZoom: 13, duration: 850 }); } catch (e) { map.flyTo({ center: c, zoom: 12.5, duration: 700 }); } }
  else map.flyTo({ center: c, zoom: Math.max(map.getZoom(), 14), duration: 600 });
}

function openTerminus(f) {
  const c = f.geometry.coordinates, nm = f.properties['Name of th'] || 'Terminus';
  $('card-title').innerHTML = `<span class="ttl"><span>${nm}</span></span>`;
  $('card-sub').innerHTML = `Bus terminus &middot; ${f.properties.Ownership || ''}`;
  const key = nm.toLowerCase().split(' ')[0];
  const rel = Object.values(ROUTE_IDX).filter(e => (e.source || '').toLowerCase().includes(key) || (e.dest || '').toLowerCase().includes(key)).slice(0, 80);
  $('card-body').innerHTML = rel.length ? `<div class="seclbl">Routes from here</div><div class="pillwrap">${rel.map(e => tagHTML(e.name, e.category)).join('')}</div>` : '<div class="seclbl">Terminus</div>';
  $('card-body').querySelectorAll('.service-tag').forEach((t, i) => t.addEventListener('click', () => selectRoute(rel[i].name)));
  showCard();
  // glow-highlight every route that uses this terminus, then zoom out to reveal them
  if (selStopMarker) { selStopMarker.remove(); selStopMarker = null; }
  selMarkers.forEach(m => m.remove()); selMarkers = [];
  selectedRoute = null;
  const feats = [], b = new maplibregl.LngLatBounds(); b.extend(c);
  rel.forEach(e => { feats.push(...e.feats); e.coords.forEach(p => b.extend(p)); });
  map.getSource('sel').setData({ type: 'FeatureCollection', features: feats });
  map.setPaintProperty('routes', 'line-opacity', feats.length ? 0.1 : 0.7);
  const many = rel.length > 4;
  map.setPaintProperty('sel-line', 'line-width', many ? 2.6 : 5);
  map.setPaintProperty('sel-glow', 'line-width', many ? 7 : 15); map.setPaintProperty('sel-glow', 'line-blur', many ? 4 : 9);
  // chevrons only make sense on a single traced line; on a fan of routes from
  // one terminus they overlap into noise
  map.setLayoutProperty('sel-arrows', 'visibility', many ? 'none' : 'visible');
  const pad = fitPad();
  if (feats.length) { try { map.fitBounds(b, { padding: pad, maxZoom: 13, duration: 850 }); } catch (e) { map.flyTo({ center: c, zoom: 12.5, duration: 700 }); } }
  else map.flyTo({ center: c, zoom: Math.max(map.getZoom(), 14), duration: 650 });
}


/* ── search ── */
const rkey = s => String(s || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
function renderHint() { $('results').innerHTML = '<div class="hint">Type a route number (e.g. <b>5B</b>, <b>21G</b>, <b>M1</b>), a stop name (e.g. <b>Adyar</b>) or a bus number (e.g. <b>K5657</b>). Or explore the map — tap a stop for its buses, tap a line to trace a route.</div>'; }
renderHint();
$('q').addEventListener('input', () => {
  const v = $('q').value.trim().toLowerCase();
  if (!v) { renderHint(); return; }
  // route numbers however typed: "570s", "570 S", "p570s" all find P570S. Spaces, case and
  // punctuation are ignored, and a letter prefix (P remium, M ini, A, S …) may be left off.
  const q = rkey(v);
  const rank = e => { const k = rkey(e.name), bare = k.replace(/^[A-Z]+(?=\d)/, '');
    return !q ? -1 : k.startsWith(q) ? 0 : bare.startsWith(q) ? 1 : k.includes(q) ? 2 : -1; };
  const rts = Object.values(ROUTE_IDX).map(e => [rank(e), e]).filter(([r]) => r >= 0)
    .sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name, undefined, { numeric: true })).map(([, e]) => e).slice(0, 22);
  const sts = ((STOPS && STOPS.features) || []).filter(f =>
    ((f.properties['Stop Name'] || '') + ' ' + (f.properties.alt || '')).toLowerCase().includes(v)).slice(0, 20);
  let h = '';
  if (rts.length) h += '<div class="grp">Routes</div>' + rts.map(e => `<div class="it" data-r="${e.name}">${tagHTML(e.name, e.category)}<span>${e.source || ''} → ${e.dest || ''}</span></div>`).join('');
  if (sts.length) h += '<div class="grp">Stops</div>' + sts.map((f, i) => `<div class="it" data-s="${i}"><svg width="15" height="15" viewBox="0 0 24 24" style="color:var(--red);flex:0 0 auto" fill="currentColor"><path d="M12 2a8 8 0 0 0-8 8c0 5.4 8 12 8 12s8-6.6 8-12a8 8 0 0 0-8-8zm0 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/></svg><span>${f.properties['Stop Name']}<div class="sub">${parseRoutes(f.properties['route name']).join(', ')}</div></span></div>`).join('');
  $('results').innerHTML = h || '<div class="hint">No matches.</div>';
  $('results').querySelectorAll('[data-r]').forEach(el => el.addEventListener('click', () => selectRoute(el.dataset.r)));
  $('results').querySelectorAll('[data-s]').forEach(el => el.addEventListener('click', () => openStop(sts[+el.dataset.s])));
});

/* ── layer toggles ── */
const lm = $('layer-menu');
const LAYER_IDS = { routes: ['routes', 'cables-case', 'cables', 'cables-straight'], labels: ['route-labels', 'cable-labels'], stops: ['stops', 'stop-icons', 'stop-labels'], metro: ['metro'], termini: [] };
lm.querySelectorAll('button[data-lyr]').forEach(b => b.addEventListener('click', () => {
  const key = b.dataset.lyr; const on = b.classList.toggle('on'); enabled[key] = on;
  if (key === 'termini') { terminiMarkers.forEach(o => o.m.getElement().style.display = on ? '' : 'none'); return; }
  LAYER_IDS[key].forEach(id => map.getLayer(id) && map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none'));
}));


