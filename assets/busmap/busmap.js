/* Shared parts of every bus map on maps.reclaim.city.

   The Delhi bus map is the reference design; what is here was lifted out of
   delhi/app.js and delhi/live.js so that APSRTC, TGSRTC and the rest draw the
   same thing the same way:

     BUSMAP.busScale(z)       how big a 3D bus is at a zoom (one curve, every network)
     BUSMAP.hashColor(name)   a route's colour from its number
     BUSMAP.pinEl / fitPad    the teardrop map pin; padding that keeps a traced route clear of the cards
     BUSMAP.warm(map)         positron warmed toward the busrouter cream
     BUSMAP.dark(map)         the dark map: menu item, basemap recolour, persisted per browser
     BUSMAP.chrome(map, o)    search roll-out, card fold, layers menu, 3D tilt, locate, keyboard lift
     BUSMAP.railHTML(...)     the stop diagram: two rails joined by U-turns, one straight line, or a loop
     BUSMAP.hoursClock(h, o)  departures on a 24-hour day/night dial; dayShape(h) names its shape

   Only the bus models differ by network (delhi/iso.js, <rtc>/coaches3d.js). */
'use strict';
const BUSMAP = (() => {
  const $ = id => document.getElementById(id);

  /* ── 3D bus size ──
     Sprite scale against zoom, applied to a sprite drawn at 9 px per metre
     (delhi/iso.js and every coaches3d.js use that), so a 12 m bus is 108 * s
     screen px wide on every network. Linear between the stops. */
  const BUS_SIZE = [[9, 0.16], [12, 0.22], [14, 0.38], [16, 0.62], [18, 1.0]];
  const lerp = (z, pts) => {
    if (z <= pts[0][0]) return pts[0][1];
    for (let i = 1; i < pts.length; i++) if (z <= pts[i][0]) { const [z0, a] = pts[i - 1], [z1, b] = pts[i]; return a + (b - a) * (z - z0) / (z1 - z0); }
    return pts[pts.length - 1][1];
  };
  const busScale = z => lerp(z, BUS_SIZE);

  /* per-route colour (chalo-style multi-colour) — stable hash → golden-angle hue */
  function hashColor(name) {
    name = String(name || '');
    let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    const hue = (h * 47) % 360, sat = 62 + (h % 18), lig = 42 + (h % 12);
    return `hsl(${hue} ${sat}% ${lig}%)`;
  }

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

  /* Padding that keeps the traced route in the clear strip between the left panel
     and the right-docked card. Clamped: on a narrow desktop window the two
     paddings can exceed the viewport, and MapLibre answers that by zooming all
     the way out, so never claim more than 35% of the width per side. */
  function fitPad() {
    if (window.innerWidth <= 700) return { top: 60, bottom: 330, left: 20, right: 20 };
    const cap = window.innerWidth * 0.35;
    const searching = document.body.classList.contains('search-open');
    return { top: 70, bottom: 60, left: Math.min(searching ? 380 : 90, cap), right: Math.min(410, cap) };
  }

  /* warm positron toward busrouter cream (best-effort) */
  function warm(map) {
    try {
      map.getStyle().layers.forEach(l => {
        if (l.type === 'background') map.setPaintProperty(l.id, 'background-color', '#f9f5ed');
        else if (l.type === 'fill' && /water|ocean|sea/i.test(l.id)) map.setPaintProperty(l.id, 'fill-color', '#bfe3f2');
        else if (l.type === 'fill' && /(wood|forest|park|grass|landcover|landuse_|cemetery|pitch)/i.test(l.id)) { try { map.setPaintProperty(l.id, 'fill-color', '#e6ebda'); } catch (e) {} }
      });
    } catch (e) {}
  }

  /* ── the dark map ──
     Not a second style (swapping styles drops every source and layer the page
     added): the basemap's own layers are recoloured in place, and remembered so
     the light map comes back exactly. The page's route and stop layers keep their
     colours; only their white halos turn dark. */
  const DARK = { bg: '#15171b', water: '#0f2532', green: '#18231c', fill: '#1d2025', road: '#33363e', major: '#43464f',
    rail: '#3b3e46', bound: '#4b4e57', text: '#b9bcc4', halo: '#121418', building: '#23262c' };
  const HALO_IDS = /^(route-labels|cable-labels|stop-labels|net-num|bus-labels|hlstop-labels)$/;
  let darkOn = (() => { try { return localStorage.getItem('busmapDark') === '1'; } catch (e) { return false; } })();
  const saved = new Map();
  function paint(map, id, prop, v) {
    try {
      const k = id + '|' + prop;
      if (!saved.has(k)) saved.set(k, map.getPaintProperty(id, prop));
      map.setPaintProperty(id, prop, v);
    } catch (e) {}
  }
  function applyDark(map, onIt) {
    document.body.classList.toggle('bm-dark', onIt);
    const layers = (map.getStyle() && map.getStyle().layers) || [];
    if (!onIt) {
      for (const [k, v] of saved) { const [id, prop] = k.split('|'); try { if (map.getLayer(id)) map.setPaintProperty(id, prop, v); } catch (e) {} }
      saved.clear(); return;
    }
    for (const l of layers) {
      const id = l.id, src = l.source;
      const own = !src || !(map.getSource(src) && map.getSource(src).type === 'vector');
      if (HALO_IDS.test(id)) { paint(map, id, 'text-halo-color', DARK.halo); if (/stop-labels|bus-labels|hlstop/.test(id)) paint(map, id, 'text-color', DARK.text); continue; }
      if (id === 'stops') { paint(map, id, 'circle-stroke-color', DARK.halo); continue; }
      if (id === 'cables-case') { paint(map, id, 'line-color', '#0e1013'); continue; }
      if (own && l.type !== 'background') continue;               // the page's own overlays keep their colours
      if (l.type === 'background') paint(map, id, 'background-color', DARK.bg);
      else if (l.type === 'fill') paint(map, id, 'fill-color', /water|ocean|sea/i.test(id) ? DARK.water
        : /(wood|forest|park|grass|landcover|cemetery|pitch)/i.test(id) ? DARK.green : /building/i.test(id) ? DARK.building : DARK.fill);
      else if (l.type === 'fill-extrusion') paint(map, id, 'fill-extrusion-color', DARK.building);
      else if (l.type === 'line') paint(map, id, 'line-color', /water|river|stream|canal/i.test(id) ? DARK.water
        : /rail|transit/i.test(id) ? DARK.rail : /boundary|admin/i.test(id) ? DARK.bound
        : /(motorway|trunk|primary|major)/i.test(id) ? DARK.major : DARK.road);
      else if (l.type === 'symbol') { paint(map, id, 'text-color', DARK.text); paint(map, id, 'text-halo-color', DARK.halo); }
    }
  }
  const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
  /* Adds "Dark map" to the layers menu and applies the saved choice on every style load.
     onChange(on) lets a page repaint anything of its own (canvas sprites, pins). */
  function dark(map, onChange) {
    const lm = $('layer-menu');
    let btn = null;
    if (lm && !lm.querySelector('[data-dark]')) {
      btn = document.createElement('button');
      btn.className = 'lyr dark' + (darkOn ? ' on' : ''); btn.dataset.dark = '1';
      btn.innerHTML = MOON + '<span>Dark map</span>';
      const dash = lm.querySelector('a');
      dash ? lm.insertBefore(btn, dash) : lm.appendChild(btn);
      btn.addEventListener('click', () => {
        darkOn = !darkOn; btn.classList.toggle('on', darkOn);
        try { localStorage.setItem('busmapDark', darkOn ? '1' : '0'); } catch (e) {}
        applyDark(map, darkOn); onChange && onChange(darkOn);
      });
    }
    const go = () => { if (darkOn) { saved.clear(); applyDark(map, true); } };
    // the page adds its own layers in style.load; recolour after them
    map.on('style.load', () => setTimeout(go, 0));
    if (map.isStyleLoaded && map.isStyleLoaded()) setTimeout(go, 0);
    document.body.classList.toggle('bm-dark', darkOn);
    return { isDark: () => darkOn, reapply: go };
  }

  /* ── page chrome ──
     Search rolls out from the button and rolls away again: on a click elsewhere,
     on Escape, and whenever a card opens. Tap a card's header to fold it away and
     see the whole map. The layers menu, the 3D tilt and "my location" buttons.
     The search card is lifted above the on-screen keyboard (visualViewport). */
  /* The map credits stay folded into the (i) button: a tap opens them. MapLibre's compact attribution opens
     itself on load and whenever the style changes; we fold it back unless the rider opened it. */
  function quietAttribution(map) {
    let user = false;
    const shut = () => { const a = map.getContainer().querySelector('.maplibregl-ctrl-attrib'); if (a && !user) a.classList.remove('maplibregl-compact-show'); };
    map.getContainer().addEventListener('click', e => { if (e.target.closest('.maplibregl-ctrl-attrib-button')) user = true; }, true);
    map.on('load', shut); map.on('styledata', shut); map.on('resize', shut); setTimeout(shut, 0); setTimeout(shut, 1500);
  }

  function chrome(map, opts = {}) {
    const settle = () => setTimeout(() => map.resize(), 360);
    const inert = t => t.closest('button, a, input, select, textarea, .service-tag');
    function setSearch(open) {
      document.body.classList.toggle('search-open', open);
      $('search-fab').setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) setTimeout(() => { try { $('q').focus({ preventScroll: true }); } catch (e) {} }, 140);
      settle();
    }
    $('search-fab').addEventListener('click', e => { e.stopPropagation(); setSearch(!document.body.classList.contains('search-open')); });
    $('panel').addEventListener('click', e => { if (e.target.closest('.ptop') && !inert(e.target)) setSearch(false); });
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
    (() => {
      const vv = window.visualViewport; if (!vv) return;
      const fit = () => {
        const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        document.documentElement.style.setProperty('--kb', (kb > 60 ? kb : 0) + 'px');
        document.documentElement.style.setProperty('--vvh', vv.height + 'px');
      };
      vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
    })();
    const lm = $('layer-menu');
    if (lm && $('layers-btn')) {
      $('layers-btn').addEventListener('click', e => { e.stopPropagation(); lm.classList.toggle('open'); });
      document.addEventListener('click', e => { if (!lm.contains(e.target) && e.target.closest('#layers-btn') == null) lm.classList.remove('open'); });
    }
    const tb = $('tilt-btn');
    if (tb) {
      const sync = () => tb.classList.toggle('on', map.getPitch() > 5);
      tb.addEventListener('click', () => {
        const up = map.getPitch() > 5;
        map.easeTo(up ? { pitch: 0, bearing: 0, duration: 700 }
                      : { pitch: 58, bearing: -18, zoom: Math.max(map.getZoom(), opts.tiltZoom || 15.6), duration: 900 });
      });
      map.on('pitchend', sync); sync();
    }
    let locMarker = null;
    if ($('loc-btn')) $('loc-btn').addEventListener('click', () => {
      if (!navigator.geolocation) { alert('Geolocation not supported.'); return; }
      navigator.geolocation.getCurrentPosition(p => {
        const ll = [p.coords.longitude, p.coords.latitude];
        if (locMarker) locMarker.remove();
        const el = document.createElement('div'); el.style.cssText = 'width:18px;height:18px;border-radius:50%;background:#007aff;border:3px solid #fff;box-shadow:0 0 0 2px rgba(0,122,255,.4)';
        locMarker = new maplibregl.Marker({ element: el }).setLngLat(ll).addTo(map);
        map.flyTo({ center: ll, zoom: 15, duration: 800 });
      }, err => alert(err.message), { enableHighAccuracy: true, timeout: 10000 });
    });
    return { setSearch, settle };
  }

  /* ── the stop diagram ──
     busrouter.sg style. Stops are [code, name, lat, lon].
       back given  -> two rails, outbound down the left and the return leg back up
                      the right, joined by U-turns at both termini, with dotted
                      links between paired stops and arrows for travel direction;
       loop        -> one rail whose last stop curls back into the first;
       otherwise   -> one straight line (a one-way working, or an intercity route
                      drawn once for both directions).
     o.extra(st, i) adds html under a stop's name (times, ETAs); o.note is shown
     under a single rail; o.head is a line above it; o.arrows = false drops the
     direction arrows (a line run both ways). Rows carry data-i (index in
     `s`), data-y/data-x (lat/lon) so a page can wire clicks and live progress. */
  const LOOP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
  function railHTML(s, back, o = {}) {
    const extra = o.extra || (() => '');
    const cell = (st, i) => st ? `<span class="nm">${st[1]}</span>${i != null ? extra(st, i) : ''}`
                               : '<span class="none">—</span>';
    if (!back) {
      const loop = !!o.loop;
      const rows = s.map((st, i) => {
        const last = i === s.length - 1;
        const arw = (i % 3 === 1 && !last && o.arrows !== false) ? '<span class="arw dn"></span>' : '';
        const cls = (i === 0 ? ' stop-start' : '') + (last && !loop ? ' stop-end' : '');
        return `<li class="grid ${i === 0 || last ? 'term' : ''}" data-i="${i}" data-y="${st[2]}" data-x="${st[3]}">`
          + `<div class="rl out${cls}"><span class="dot"></span>${arw}</div>`
          + `<div class="lbl out">${cell(st, i)}</div></li>`;
      }).join('');
      const tail = loop && s.length ? `<div class="loopback" data-i="0" data-y="${s[0][2]}" data-x="${s[0][3]}">${LOOP_ICON}<span>Loops back to <b>${s[0][1]}</b></span></div>` : '';
      return `<div class="rail single${loop ? ' loop' : ''}">`
        + (o.head ? `<div class="bothways">${o.head}</div>` : '')
        + `<ul>${rows}</ul>${tail}</div>`
        + (o.note ? `<div class="tmsg" style="padding:10px 2px">${o.note}</div>` : '');
    }
    const key = st => (st[1] || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    const pool = new Map();
    back.forEach(st => { if (!pool.has(key(st))) pool.set(key(st), st); });
    /* The termini live in the U-turn caps: a dot at the top of each loop and the
       name once beside it. */
    const t0 = s[0], t1 = s[s.length - 1];
    pool.delete(key(t0)); pool.delete(key(t1));
    const rows = s.map((st, i) => {
      if (i === 0 || i === s.length - 1) return '';
      const m = pool.get(key(st));
      if (m) pool.delete(key(st));
      const dn = (i % 3 === 1) ? '<span class="arw dn"></span>' : '';
      const up = (i % 3 === 1) ? '<span class="arw up"></span>' : '';
      return `<li class="grid" data-i="${i}" data-y="${st[2]}" data-x="${st[3]}">`
        + `<div class="lbl l out">${cell(st, i)}</div>`
        + `<div class="rl out"><span class="dot"></span>${dn}</div>`
        + `<div class="link${m ? '' : ' none'}"></div>`
        + `<div class="rl"><span class="dot${m ? '' : ' blank'}"></span>${up}</div>`
        + `<div class="lbl r">${cell(m)}</div></li>`;
    }).join('');
    const left = pool.size;
    return `<div class="rail">`
      + `<div class="grid"><div class="capname" data-i="0" data-y="${t0[2]}" data-x="${t0[3]}">${t0[1]}${extra(t0, 0)}</div></div>`
      + `<div class="grid"><div></div><div class="cap" data-i="0"><span class="capdot"></span></div><div></div></div>`
      + `<div class="grid hdr"><div class="l">Going</div><div></div><div></div><div></div><div class="r">Coming back</div></div>`
      + `<ul>${rows}</ul>`
      + `<div class="grid"><div></div><div class="cap btm" data-i="${s.length - 1}"><span class="capdot"></span></div><div></div></div>`
      + `<div class="grid"><div class="capname" data-i="${s.length - 1}" data-y="${t1[2]}" data-x="${t1[3]}">${t1[1]}${extra(t1, s.length - 1)}</div></div></div>`
      + (left ? `<div class="tmsg" style="padding:10px 2px">${left} stop${left === 1 ? '' : 's'} on the return leg have no counterpart here — the two directions do not use identical stops.</div>` : '');
  }

  /* When a working runs tells you what it is for: a depot run, a peak-only service. */
  function dayShape(h) {
    const total = h.reduce((a, b) => a + b, 0);
    if (!total) return '';
    const share = (a, b) => h.slice(a, b).reduce((x, y) => x + y, 0) / total;
    const early = share(0, 8), late = share(19, 24), mid = share(10, 16);
    if (early >= 0.9) return 'Early morning only';
    if (late >= 0.9) return 'Late evening only';
    if (early + late >= 0.9) return 'Start and end of the day only — the shape of a depot run';
    if (mid === 0 && early + late >= 0.6) return 'Peak hours only';
    return '';
  }

  /* Sunrise and sunset from NOAA's solar equations (zenith 90.833°), in IST. */
  function sunHours(lat, lon, tz = 5.5) {
    const d = new Date(Date.now() + tz * 3600e3), y = d.getUTCFullYear();
    const doy = Math.round((Date.UTC(y, d.getUTCMonth(), d.getUTCDate()) - Date.UTC(y, 0, 0)) / 864e5);
    const g = 2 * Math.PI / 365 * (doy - 1), rd = Math.PI / 180;
    const eq = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g) - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
    const dec = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g) + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    const ha = Math.acos(Math.cos(90.833 * rd) / (Math.cos(lat * rd) * Math.cos(dec)) - Math.tan(lat * rd) * Math.tan(dec)) / rd;
    const t = m => ((m + tz * 60) % 1440 + 1440) % 1440 / 60;
    return [t(720 - 4 * (lon + ha) - eq), t(720 - 4 * (lon - ha) - eq)];
  }
  const hhmm = x => `${String(Math.floor(x)).padStart(2, '0')}:${String(Math.round(x % 1 * 60) % 60).padStart(2, '0')}`;
  const SUN_ICON = '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/><circle cx="12" cy="12" r="4"/>';
  const MOON_ICON = '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>';

  /* Departures on one 24-hour dial, split into day and night. 06:00 sits at the
     top and the day runs clockwise down the right half to 18:00 at the bottom; the
     night fills the left half back up to 06:00. One wedge an hour, area-true, gold
     for the part of the hour the sun is up at o.lat/o.lon today and blue for the rest. */
  let dnSeq = 0;
  function hoursClock(h, o = {}) {
    const lat = o.lat != null ? o.lat : 28.6139, lon = o.lon != null ? o.lon : 77.2090, place = o.place || 'Delhi';
    const tot = h.reduce((a, b) => a + b, 0), max = Math.max(...h, 1);
    const id = 'dn' + (++dnSeq);
    const cx = 100, cy = 100, R = 74, r0 = 24;
    const pol = (pos, r) => { const a = pos / 24 * 2 * Math.PI - Math.PI / 2; return [(cx + r * Math.cos(a)).toFixed(2), (cy + r * Math.sin(a)).toFixed(2)]; };
    const posOf = hr => (hr - 6 + 24) % 24;
    const rad = n => r0 + 3 + (R - r0 - 3) * Math.sqrt(n / max);
    const wedge = (p0, p1, r1) => {
      const [a, b] = pol(p0, r1), [c, d] = pol(p1, r1), [e, f] = pol(p1, r0), [g, k] = pol(p0, r0);
      return `M${a} ${b}A${r1} ${r1} 0 0 1 ${c} ${d}L${e} ${f}A${r0} ${r0} 0 0 0 ${g} ${k}Z`;
    };
    const [rise, set] = sunHours(lat, lon);
    const wedges = Array.from({ length: 24 }, (_, hr) => {
      const n = h[hr] || 0, pos = posOf(hr), r1 = n ? rad(n) : r0 + 2;
      const hh = String(hr).padStart(2, '0');
      const tip = `<title>${hh}:00–${String((hr + 1) % 24).padStart(2, '0')}:00 — ${n} departure${n === 1 ? '' : 's'}</title>`;
      const a = Math.max(hr, rise), b = Math.min(hr + 1, set), at = x => pos + .06 + .88 * (x - hr);
      const parts = b <= a ? [[hr, hr + 1, 'nt']]
        : [[hr, a, 'nt'], [a, b, 'dy'], [b, hr + 1, 'nt']].filter(([x, y]) => y - x > 1e-3);
      return parts.map(([x, y, c]) => `<path class="dn-w ${c}${n ? '' : ' z'}" d="${wedge(at(x), at(y), r1)}">${tip}</path>`).join('');
    }).join('');
    const nums = [[6, '06'], [12, '12'], [18, '18'], [0, '00']].map(([hr, l]) => {
      const [x, y] = pol(posOf(hr), R + 12); return `<text class="dn-num" x="${x}" y="${(+y + 3).toFixed(2)}">${l}</text>`;
    }).join('');
    const ticks = Array.from({ length: 24 }, (_, q) => { const [x1, y1] = pol(q, R + (q % 3 ? 2.5 : 5)), [x2, y2] = pol(q, R + 1); return `<line class="dn-tick" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`; }).join('');
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
      + `<div class="dn-peak dn-sun">Wedges gold while the sun is up in ${place} today, ${hhmm(rise)}–${hhmm(set)}</div>`
      + (tot ? `<div class="dn-peak">Busiest hour <b>${String(peak).padStart(2, '0')}:00–${String((peak + 1) % 24).padStart(2, '0')}:00</b> · ${max} departure${max === 1 ? '' : 's'}</div>` : '');
  }

  /* One download per data file, whoever asks for it: the network loader and the traffic layer both want
     segs.json (APSRTC 7.5 MB, Odisha 5.3 MB unpacked), and each used to fetch its own copy. */
  const JSONS = new Map();
  // keyed by path: a cache-buster (?t=) on one caller's URL is still the same file
  const json = u => { const k = String(u).split('?')[0]; if (!JSONS.has(k)) JSONS.set(k, fetch(u).then(r => r.ok ? r.json() : null).catch(() => null)); return JSONS.get(k); };

  /* How much this device can take, so a basic phone gets a light map and a good one the full one:
     'low'  a basic phone: 2 GB or less, data saver, or a 2G connection
     'high' a desktop, an iPhone/iPad, or an Android with 6 GB+ and 8 cores
     'mid'  the rest. ?tier=low|mid|high forces one (for testing). */
  const TIER = (() => {
    try { const f = new URLSearchParams(location.search).get('tier'); if (/^(low|mid|high)$/.test(f)) return f; } catch (e) {}
    const n = navigator, c = n.connection || {}, mem = n.deviceMemory || 4, cores = n.hardwareConcurrency || 4;
    const touch = matchMedia('(pointer: coarse)').matches, apple = /iPhone|iPad|Macintosh/.test(n.userAgent);
    if (c.saveData || /(^|-)2g$/.test(c.effectiveType || '') || mem <= 2) return 'low';
    if (!touch || apple || (mem >= 6 && cores >= 8)) return 'high';
    return 'mid';
  })();
  // device pixels drawn per screen point: every one is GPU work. 1.25 on a basic phone, 1.75 on the rest, up to 2.5 where it can
  const pixelRatio = () => Math.min(window.devicePixelRatio || 1, TIER === 'low' ? 1.25 : TIER === 'mid' ? 1.75 : 2.5);

  return { BUS_SIZE, quietAttribution, busScale, lerp, hashColor, pinEl, fitPad, warm, dark, chrome, railHTML, dayShape, sunHours, hoursClock, json, TIER, pixelRatio };
})();
