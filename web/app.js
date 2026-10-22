/* Chennai MTC Bus Explorer — MapLibre GL (vector), busrouter.sg-style UI + chalo features.
   Multi-coloured route lines, route-number labels along the lines on zoom, busrouter route/stop cards. */
'use strict';

const DATA = 'data';
const DATA_V = '20261020b';   // Chennai One snapshot the data/ files were built from; bumps every cache   // self-contained: the enriched Chennai One layers, not /mtc/data
const $ = id => document.getElementById(id);
const parseRoutes = v => (Array.isArray(v) ? v
  : (() => { try { const j = JSON.parse(v); if (Array.isArray(j)) return j; } catch (e) {} return String(v || '').replace(/[[\]"']/g, '').split(',').map(s => s.trim()).filter(Boolean); })()
  ).map(String).map(s => s.trim()).filter(Boolean);

/* per-route colour (chalo-style multi-colour) — stable hash → golden-angle hue */
/* Padding that keeps the traced route in the clear strip between the left panel
   and the right-docked card. Clamped: on a narrow desktop window the two
   paddings can exceed the viewport, and MapLibre answers that by zooming all
   the way out (observed: z7.6 on an 851px window), so never claim more than
   35% of the width per side. */
function fitPad() {
  if (window.innerWidth <= 700) return { top: 60, bottom: 330, left: 20, right: 20 };
  const cap = window.innerWidth * 0.35;
  // only reserve the search card's width while it is actually rolled out
  const searching = document.body.classList.contains('search-open');
  return { top: 70, bottom: 60,
           left: Math.min(searching ? 380 : 90, cap), right: Math.min(410, cap) };
}

function hashColor(name) {
  let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const hue = (h * 47) % 360, sat = 62 + (h % 18), lig = 42 + (h % 12);
  return `hsl(${hue} ${sat}% ${lig}%)`;
}
const catClass = c => c === 'MTC Small Bus' ? 'small' : c === 'CMRL Feeder' ? 'feeder' : '';

/* busrouter teardrop bus-pin (HTML marker) */
const BUS_PATH = 'M17.5 8.52c-.15-.66-.6-.92-1.29-1.19-.66-.26-2.27-.57-3.46-.58-1.19 0-2.8.32-3.47.58-.68.26-1.13.53-1.27 1.19l-.51 3.67v5.05h.88v.79c0 .96 1.51.96 1.51 0v-.8h5.72v.8c0 .96 1.51.96 1.51 0v-.8H18v-5.05l-.51-3.66zm-6.91-.33h4.32c.43 0 .43.6 0 .6H10.6a.3.3 0 0 1 0-.59zm-1.46 7.47c-.4 0-.72-.3-.72-.69 0-.37.32-.67.71-.67.4 0 .73.3.73.67 0 .38-.33.68-.73.68h.01zm3.57-2.69H9.12c-.38 0-.46-.25-.42-.5l.4-2.66c.05-.33.17-.54.62-.54h6.04c.46 0 .57.21.63.54l.4 2.66c.04.25-.04.5-.41.5H12.7zm3.67 2.68c-.4 0-.73-.3-.73-.68 0-.37.33-.67.73-.67.4 0 .72.3.74.67 0 .38-.33.68-.73.68h-.01z';
function pinEl(color, scale) {
  const s = scale || 1; const el = document.createElement('div'); el.className = 'pin-marker';
  el.innerHTML = `<svg width="${26 * s}" height="${32 * s}" viewBox="0 0 26 32" xmlns="http://www.w3.org/2000/svg">
    <path fill="#fff" d="M2.85 20.79a12.76 12.76 0 1 1 19.8 0l-.03.03c-.24.29-.49.57-.75.84l-9.12 10.19-9.12-10.2c-.26-.26-.51-.54-.75-.83l-.03-.03z"/>
    <path fill="${color}" d="M5.39 19.14a9.75 9.75 0 1 1 14.72 0l-7.36 8.9-7.36-8.9z"/>
    <path fill="#fff" d="${BUS_PATH}"/></svg>`;
  return el;
}

/* ── map ── */
const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/positron',
  center: [80.24, 13.06], zoom: 10.6, maxZoom: 18, attributionControl: false,
});
window.map = map;
map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: 'Routes/stops: MTC · CUMTA · Schedules: Chennai Open GTFS' }), 'bottom-right');
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

let ROUTE_IDX = {}, STOPS = null, terminiMarkers = [], selMarkers = [], selStopMarker = null, locMarker = null;
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
  // warm positron toward busrouter cream (best-effort)
  try {
    map.getStyle().layers.forEach(l => {
      if (l.type === 'background') map.setPaintProperty(l.id, 'background-color', '#f9f5ed');
      else if (l.type === 'fill' && /water|ocean|sea/i.test(l.id)) map.setPaintProperty(l.id, 'fill-color', '#bfe3f2');
      else if (l.type === 'fill' && /(wood|forest|park|grass|landcover|landuse_|cemetery|pitch)/i.test(l.id)) { try { map.setPaintProperty(l.id, 'fill-color', '#e6ebda'); } catch (e) {} }
    });
  } catch (e) {}

  // sources
  map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addSource('metro',  { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addSource('stops',  { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addSource('sel',    { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

  // metro under bus routes
  map.addLayer({ id: 'metro', type: 'line', source: 'metro',
    paint: { 'line-color': '#7c3aed', 'line-width': 2, 'line-opacity': 0.7, 'line-dasharray': [1, 2] } });
  // bus routes — multi-coloured per route
  map.addLayer({ id: 'routes', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1, 14, 2.6, 16, 3.4], 'line-opacity': 0.7 } });
  // selected route(s) highlight — a static glowing line (blurred halo under a crisp line; no animation → no freeze on big hubs)
  map.addLayer({ id: 'sel-glow', type: 'line', source: 'sel', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': 14, 'line-blur': 8, 'line-opacity': 0.55 } });
  map.addLayer({ id: 'sel-line', type: 'line', source: 'sel', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ['get', 'color'], 'line-width': 5, 'line-opacity': 0.98 } });
  map.addLayer({ id: 'route-labels', type: 'symbol', source: 'routes', minzoom: 13.2,
    /* Label with the real service number. `route_name` carries a generated
       "(2)"/"(3)" suffix to keep each working uniquely addressable, but painting
       that on the map invents bus numbers that do not exist — "S60 (2)" reads as
       a service, and MTC really does have a route called 18K(SEC), so brackets
       are not even a safe tell. All four S60 workings are S60. */
    layout: { 'symbol-placement': 'line', 'text-field': ['get', 'sn'], 'text-size': 11.5,
      'text-font': ['Noto Sans Bold'], 'symbol-spacing': 220, 'text-allow-overlap': false, 'text-padding': 4 },
    paint: { 'text-color': ['get', 'color'], 'text-halo-color': '#fff', 'text-halo-width': 1.8 } });
  // bus stops as points
  map.addLayer({ id: 'stops', type: 'circle', source: 'stops', minzoom: 12,
    paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 1.6, 14, 3, 16, 5],
      'circle-color': '#f01b48', 'circle-stroke-color': '#fff', 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 12, 0.4, 15, 1.2] } });
  // stop name labels (more info on zoom)
  map.addLayer({ id: 'stop-labels', type: 'symbol', source: 'stops', minzoom: 15.5,
    layout: { 'text-field': ['get', 'Stop Name'], 'text-size': 11, 'text-font': ['Noto Sans Regular'],
      'text-offset': [0, 1], 'text-anchor': 'top', 'text-optional': true },
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
  map.on('click', 'stops', e => { if (!underBus(e)) openStop(e.features[0]); }); ptr('stops');
  const pickFeature = e => {
    if (underBus(e)) return;
    const p = e.features[0].properties;
    selectRoute(p.sn || p.route_name, p.code);   // open the exact working clicked
  };
  map.on('click', 'routes', pickFeature); ptr('routes');
  map.on('click', 'route-labels', pickFeature); ptr('route-labels');

  loadData();
});

/* ── load + index data ── */
function loadData() {
  fetch(`${DATA}/mtc_routes.geojson?v=${DATA_V}`).then(r => r.json()).then(fc => {
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
    map.getSource('routes').setData(fc);
  });
  fetch(`${DATA}/metro_map.geojson`).then(r => r.json()).then(fc => map.getSource('metro').setData(fc));
  fetch(`${DATA}/bus_termini.geojson?v=${DATA_V}`).then(r => r.json()).then(fc => {
    TERMINI_FC = fc;                          // lazy viewport-based pin rendering
    map.on('moveend', renderTermini);
    renderTermini();
  });
  fetch(`${DATA}/bus_stops.geojson?v=${DATA_V}`).then(r => r.json()).then(fc => {
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
  useWorking(e, pick || e.works[0]);
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

/* Tap the header of either surface to fold it away and see the whole map, tap
   again to bring it back. Both were reported as covering the map — the sidebar
   on desktop and tablet, the card on phones. Buttons, links and inputs inside
   the header keep their own behaviour.

   The panel needs one handler on the whole element rather than on its header:
   once folded, only a 34px lip is on screen and the header itself has slid off
   it, so there would be nothing left to tap to bring it back. */
const settle = () => setTimeout(() => map.resize(), 360);
const inert = t => t.closest('button, a, input, select, textarea, .service-tag');

/* Search rolls out from the button and rolls away again: on a click elsewhere,
   on Escape, and whenever a card opens - the answer is on the map, and the
   search card has done its job by then. */
function setSearch(open) {
  document.body.classList.toggle('search-open', open);
  $('search-fab').setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) setTimeout(() => { try { $('q').focus({ preventScroll: true }); } catch (e) {} }, 140);
  settle();
}
$('search-fab').addEventListener('click', e => {
  e.stopPropagation();
  setSearch(!document.body.classList.contains('search-open'));
});
$('panel').addEventListener('click', e => {
  if (e.target.closest('.ptop') && !inert(e.target)) setSearch(false);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') setSearch(false); });
document.addEventListener('click', e => {
  if (!document.body.classList.contains('search-open')) return;
  if (e.target.closest('#panel') || e.target.closest('#fabs')) return;
  setSearch(false);
});

$('card').addEventListener('click', e => {
  const card = $('card');
  if (card.classList.contains('min')) { card.classList.remove('min'); settle(); return; }
  if (e.target.closest('.chead') && !inert(e.target)) { card.classList.add('min'); settle(); }
});

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
  fetch(`data/timings.json?v=${DATA_V}`).then(r => r.ok ? r.json() : {})
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
    const key = st => (st[1] || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    const pool = new Map();
    if (rev) rev.forEach(st => { if (!pool.has(key(st))) pool.set(key(st), st); });

    // the feed's stop "code" is an opaque 32-hex id (453f3f0cf8…), not a public
    // stop number — printing it just put a line of hashes beside every name
    const cell = st => st ? `<span class="nm">${st[1]}</span>`
                          : '<span class="none">—</span>';

    if (!rev) {
      // One-way service. A U-turn cap into a rail of blank dots read as a round
      // trip the timetable does not run, so draw a single straight line.
      const rows = dir.s.map((st, i) => {
        const last = i === dir.s.length - 1;
        const arw = (i % 3 === 1 && !last) ? '<span class="arw dn"></span>' : '';
        const cls = (i === 0 ? ' stop-start' : '') + (last ? ' stop-end' : '');
        return `<li class="grid ${i === 0 || last ? 'term' : ''}" data-i="${i}" data-y="${st[2]}" data-x="${st[3]}">`
          + `<div class="rl out${cls}"><span class="dot"></span>${arw}</div>`
          + `<div class="lbl out">${cell(st)}</div></li>`;
      }).join('');
      wrap.innerHTML =
        `<div class="rail single">`
        + `<ul>${rows}</ul></div>`
        + `<div class="tmsg" style="padding:10px 2px">One-way service — CUMTA publishes no return working for it.</div>`;
      wire();
      return;
    }

    /* The termini live in the U-turn caps: a dot at the top of each loop and the
       name once beside it. They used to appear three times each (column header,
       cap label and the first row of both rails). */
    const t0 = dir.s[0], t1 = dir.s[dir.s.length - 1];
    pool.delete(key(t0)); pool.delete(key(t1));
    const rows = dir.s.map((st, i) => {
      if (i === 0 || i === dir.s.length - 1) return '';
      const last = i === dir.s.length - 1;
      const m = pool.get(key(st));
      if (m) pool.delete(key(st));
      // outbound runs down the left rail, the return leg back up the right one
      const dn = (i % 3 === 1 && !last) ? '<span class="arw dn"></span>' : '';
      const up = (i % 3 === 1 && !last) ? '<span class="arw up"></span>' : '';
      // no stop-end: the rail now runs on into the U-turn at the far terminus,
      // the same way it runs out of the one at the near end
      const lcls = '';
      return `<li class="grid" data-i="${i}" data-y="${st[2]}" data-x="${st[3]}">`
        + `<div class="lbl l out">${cell(st)}</div>`
        + `<div class="rl out${lcls}"><span class="dot"></span>${dn}</div>`
        + `<div class="link${m ? '' : ' none'}"></div>`
        + `<div class="rl${lcls}"><span class="dot${m ? '' : ' blank'}"></span>${up}</div>`
        + `<div class="lbl r">${cell(m)}</div></li>`;
    }).join('');
    const left = pool.size;
    wrap.innerHTML =
      `<div class="rail">`
      + `<div class="grid"><div class="capname" data-i="0" data-y="${t0[2]}" data-x="${t0[3]}">${t0[1]}</div></div>`
      + `<div class="grid"><div></div><div class="cap" data-i="0"><span class="capdot"></span></div><div></div></div>`
      + `<div class="grid hdr"><div class="l">Going</div><div></div><div></div><div></div><div class="r">Coming back</div></div>`
      + `<ul>${rows}</ul>`
      + `<div class="grid"><div></div><div class="cap btm" data-i="${dir.s.length - 1}"><span class="capdot"></span></div><div></div></div>`
      + `<div class="grid"><div class="capname" data-i="${dir.s.length - 1}" data-y="${t1[2]}" data-x="${t1[3]}">${t1[1]}</div></div></div>`
      + (left ? `<div class="tmsg" style="padding:10px 2px">${left} stop${left === 1 ? '' : 's'} on the return leg have no counterpart here — the two directions do not use identical stops.</div>` : '');
    wire();
  };
  if (ROUTE_STOPS !== undefined) return draw();
  wrap.innerHTML = '<div class="tmsg" style="padding:10px 2px">Loading stops…</div>';
  fetch(`data/route_stops.json?v=${DATA_V}`).then(r => r.ok ? r.json() : {}).then(j => { ROUTE_STOPS = j; draw(); }).catch(() => { ROUTE_STOPS = {}; draw(); });
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

/* When a working runs tells you what it is for. A 6-stop working that only
   departs between 20:10 and 23:02, whose mirror image only departs between
   04:15 and 07:05, is the bus getting to and from its depot in revenue service
   rather than dead-running — riders can use it, but it is not an all-day
   service and the card should not imply that it is. */
function dayShape(h) {
  const total = h.reduce((a, b) => a + b, 0);
  if (!total) return '';
  const share = (a, b) => h.slice(a, b).reduce((x, y) => x + y, 0) / total;
  // 0–08 and 19–24. Narrower windows mislabelled real cases: a working running
  // 19:30–21:25 came out as "Peak hours only" and one running 04:15–07:05 the
  // same, because 07:05 and 19:30 fell outside 0–07 / 20–24.
  const early = share(0, 8), late = share(19, 24), mid = share(10, 16);
  if (early >= 0.9) return 'Early morning only';
  if (late >= 0.9) return 'Late evening only';
  if (early + late >= 0.9) return 'Start and end of the day only — the shape of a depot run';
  if (mid === 0 && early + late >= 0.6) return 'Peak hours only';
  return '';
}

/* Departures on one 24-hour dial, split into day and night. 06:00 sits at the
   top and the day runs clockwise down the right half to 18:00 at the bottom; the
   night fills the left half back up to 06:00 - the sun on the right of the hub,
   the moon on the left, each half washed in its own sky. One wedge an hour,
   area-true (radius by square root) on one scale, so a depot run still reads as
   a pair of wedges with nothing between them and a night service shows up on
   the left. */
let dnSeq = 0;
const SUN_ICON = '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/><circle cx="12" cy="12" r="4"/>';
const MOON_ICON = '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>';
function hoursClock(h) {
  const tot = h.reduce((a, b) => a + b, 0), max = Math.max(...h, 1);
  const id = 'dn' + (++dnSeq);
  const cx = 100, cy = 100, R = 74, r0 = 24;
  const pol = (pos, r) => { const a = pos / 24 * 2 * Math.PI - Math.PI / 2; return [(cx + r * Math.cos(a)).toFixed(2), (cy + r * Math.sin(a)).toFixed(2)]; };
  const posOf = hr => (hr - 6 + 24) % 24;                      // 06:00 at the top, clockwise
  const rad = n => r0 + 3 + (R - r0 - 3) * Math.sqrt(n / max);
  const wedge = (p0, p1, r1) => {
    const [a, b] = pol(p0, r1), [c, d] = pol(p1, r1), [e, f] = pol(p1, r0), [g, k] = pol(p0, r0);
    return `M${a} ${b}A${r1} ${r1} 0 0 1 ${c} ${d}L${e} ${f}A${r0} ${r0} 0 0 0 ${g} ${k}Z`;
  };
  const wedges = Array.from({ length: 24 }, (_, hr) => {
    const n = h[hr] || 0, pos = posOf(hr), night = hr < 6 || hr >= 18;
    const hh = String(hr).padStart(2, '0');
    return `<path class="dn-w ${night ? 'nt' : 'dy'}${n ? '' : ' z'}" d="${wedge(pos + .06, pos + .94, n ? rad(n) : r0 + 2)}">`
         + `<title>${hh}:00–${String((hr + 1) % 24).padStart(2, '0')}:00 — ${n} departure${n === 1 ? '' : 's'}</title></path>`;
  }).join('');
  const nums = [[6, '06'], [12, '12'], [18, '18'], [0, '00']].map(([hr, l]) => {
    const [x, y] = pol(posOf(hr), R + 12); return `<text class="dn-num" x="${x}" y="${(+y + 3).toFixed(2)}">${l}</text>`;
  }).join('');
  const ticks = Array.from({ length: 24 }, (_, q) => { const [x1, y1] = pol(q, R + (q % 3 ? 2.5 : 5)), [x2, y2] = pol(q, R + 1); return `<line class="dn-tick" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`; }).join('');
  // the two skies: a soft sun on the right half, moon and stars on the left
  const sun = Array.from({ length: 9 }, (_, k) => { const a = -Math.PI / 2 + (k + .5) / 9 * Math.PI, b = .1, P = (r, t) => `${(cx + r * Math.cos(t)).toFixed(1)},${(cy + r * Math.sin(t)).toFixed(1)}`;
      return `<path d="M${P(R * .6, a - b)}L${P(R * .97, a)}L${P(R * .6, a + b)}Z" fill="#f6ad2b" opacity=".7"/>`; }).join('')
    + `<circle cx="${cx + R * .3}" cy="${cy}" r="${R * .5}" fill="url(#${id}s)"/>`;
  const moon = `<circle cx="${cx - R * .3}" cy="${cy + 2}" r="${R * .46}" fill="url(#${id}m)" mask="url(#${id}k)"/>`
    + [[-.55, -.6, 2.4], [-.78, .1, 1.6], [-.4, .66, 1.9], [-.2, -.82, 1.3], [-.84, -.32, 1.2]].map(([x, y, r]) => `<circle cx="${cx + x * R}" cy="${cy + y * R}" r="${r}" fill="#c9ccff"/>`).join('');
  const defs = `<defs>`
    + `<filter id="${id}f" x="-30%" y="-30%" width="160%" height="160%"><feTurbulence type="fractalNoise" baseFrequency="0.05" numOctaves="3" seed="${dnSeq * 7}" result="n"/>`
    + `<feDisplacementMap in="SourceGraphic" in2="n" scale="6" xChannelSelector="R" yChannelSelector="G" result="d"/><feGaussianBlur in="d" stdDeviation=".7"/></filter>`
    + `<radialGradient id="${id}s" cx="45%" cy="42%" r="60%"><stop offset="0" stop-color="#ffe28a"/><stop offset=".55" stop-color="#f7b733"/><stop offset="1" stop-color="#f08c1a" stop-opacity=".2"/></radialGradient>`
    + `<radialGradient id="${id}m" cx="40%" cy="38%" r="65%"><stop offset="0" stop-color="#eef0ff"/><stop offset=".6" stop-color="#b9bdf2"/><stop offset="1" stop-color="#7f86d8" stop-opacity=".25"/></radialGradient>`
    + `<mask id="${id}k"><rect width="200" height="200" fill="#fff"/><circle cx="${cx - R * .3 + R * .2}" cy="${cy - R * .12}" r="${R * .38}" fill="#000"/></mask>`
    + `<clipPath id="${id}r"><rect x="${cx}" y="0" width="${cx}" height="200"/></clipPath><clipPath id="${id}l"><rect x="0" y="0" width="${cx}" height="200"/></clipPath></defs>`;
  const dayN = h.reduce((a, n, hr) => a + (hr >= 6 && hr < 18 ? n : 0), 0), nightN = tot - dayN;
  const pct = n => tot ? ` · ${Math.round(100 * n / tot)}%` : '';
  const svg = `<svg viewBox="0 0 200 200" role="img" aria-label="Departures by hour, day on the right and night on the left">${defs}`
    + `<g filter="url(#${id}f)" opacity=".45"><g clip-path="url(#${id}r)">${sun}</g><g clip-path="url(#${id}l)">${moon}</g></g>`
    + `<circle class="dn-face" cx="${cx}" cy="${cy}" r="${R}"/>` + ticks + wedges + nums
    + `<line class="dn-split" x1="${cx}" y1="${cy - R - 3}" x2="${cx}" y2="${cy + R + 3}"/>`
    + `<circle class="dn-hub" cx="${cx}" cy="${cy}" r="${r0 - 2}"/>`
    + `<line class="dn-split" x1="${cx}" y1="${cy - r0 + 4}" x2="${cx}" y2="${cy + r0 - 4}"/>`
    + `<svg x="${cx + 3}" y="${cy - 7}" width="14" height="14" viewBox="0 0 24 24" class="dn-ic day">${SUN_ICON}</svg>`
    + `<svg x="${cx - 17}" y="${cy - 7}" width="14" height="14" viewBox="0 0 24 24" class="dn-ic night">${MOON_ICON}</svg>`
    + `</svg>`;
  const peak = h.indexOf(max);
  return `<div class="dnclock"><div class="dn1">${svg}</div>`
    + `<div class="dn-caps"><div class="dn-cap night"><b>Night</b> 6 pm – 6 am<span>${nightN} trip${nightN === 1 ? '' : 's'}${pct(nightN)}</span></div>`
    + `<div class="dn-cap day"><b>Day</b> 6 am – 6 pm<span>${dayN} trip${dayN === 1 ? '' : 's'}${pct(dayN)}</span></div></div></div>`
    + (tot ? `<div class="dn-peak">Busiest hour <b>${String(peak).padStart(2, '0')}:00–${String((peak + 1) % 24).padStart(2, '0')}:00</b> · ${max} departure${max === 1 ? '' : 's'}</div>` : '');
}

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
    fetch(`data/timings.json?v=${DATA_V}`).then(r => r.ok ? r.json() : {})
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

/* ── on-screen keyboard: keep the search card above it ──
   On phones the card sits at the bottom of the screen, which is exactly where the
   keyboard opens, so typing went on out of sight. visualViewport says how much of
   the window the keyboard covers; the card is lifted by that much and capped to
   the height that is left. */
(() => {
  const vv = window.visualViewport; if (!vv) return;
  const fit = () => {
    const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    document.documentElement.style.setProperty('--kb', (kb > 60 ? kb : 0) + 'px');
    document.documentElement.style.setProperty('--vvh', vv.height + 'px');
  };
  vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
})();

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
$('layers-btn').addEventListener('click', e => { e.stopPropagation(); lm.classList.toggle('open'); });
document.addEventListener('click', e => { if (!lm.contains(e.target) && e.target.closest('#layers-btn') == null) lm.classList.remove('open'); });
const LAYER_IDS = { routes: ['routes'], labels: ['route-labels'], stops: ['stops', 'stop-labels'], metro: ['metro'], termini: [] };
lm.querySelectorAll('button[data-lyr]').forEach(b => b.addEventListener('click', () => {
  const key = b.dataset.lyr; const on = b.classList.toggle('on'); enabled[key] = on;
  if (key === 'termini') { terminiMarkers.forEach(o => o.m.getElement().style.display = on ? '' : 'none'); return; }
  LAYER_IDS[key].forEach(id => map.getLayer(id) && map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none'));
}));

/* ── locate ── */
$('loc-btn').addEventListener('click', () => {
  if (!navigator.geolocation) { alert('Geolocation not supported.'); return; }
  navigator.geolocation.getCurrentPosition(p => {
    const ll = [p.coords.longitude, p.coords.latitude];
    if (locMarker) locMarker.remove();
    const el = document.createElement('div'); el.style.cssText = 'width:18px;height:18px;border-radius:50%;background:#007aff;border:3px solid #fff;box-shadow:0 0 0 2px rgba(0,122,255,.4)';
    locMarker = new maplibregl.Marker({ element: el }).setLngLat(ll).addTo(map);
    map.flyTo({ center: ll, zoom: 15, duration: 800 });
  }, err => alert(err.message), { enableHighAccuracy: true, timeout: 10000 });
});
