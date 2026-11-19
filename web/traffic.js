/* Traffic — how fast MTC buses are actually moving, road by road.

   Not a timetable estimate: live/speeds.json is built on the server from the
   GPS fixes of the live fleet (chennai_one/v36/live_speeds.py). Two fixes of
   one bus on its route give the distance it covered along the road and the
   time it took, stops and signals included — the speed a rider gets. Every
   stop-to-stop segment pools the buses of all routes that use it.

   "Last hour" shows segments a bus crossed in the past 60 minutes; "Typical"
   shows the median for an hour of the day over the last week. Segment shapes
   (data/seg_geo.json) are cut from the road-snapped route lines.

   Shares app.js's script scope (map, $, DATA_V). */
'use strict';
(() => {
  const RAMP = [[8, '#c8102e'], [12, '#ec6a2b'], [16, '#f5b21b'], [22, '#9bcf4f'], [Infinity, '#2fa35a']];
  let on = (() => { try { return localStorage.getItem('mtcTraffic') === '1'; } catch (e) { return false; } })();
  let GEO = null, SPD = null, mode = 'now', hour = new Date().getHours(), refreshT = null, popup = null;

  const css = document.createElement('style');
  css.textContent = `
    #traffic-card { position: fixed; z-index: 589; left: calc(12px + env(safe-area-inset-left)); bottom: calc(60px + env(safe-area-inset-bottom));
      width: 248px; display: none; padding: 11px 12px 10px; border-radius: 14px; background: rgba(255,255,255,.95);
      box-shadow: 0 6px 22px rgba(0,0,0,.16); font-size: 12px; color: var(--ink); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); }
    #traffic-card.show { display: block; }
    #traffic-card h3 { margin: 0 0 2px; font-size: 13px; display: flex; align-items: center; gap: 6px; }
    #traffic-card h3 svg { width: 15px; height: 15px; color: var(--red); }
    #traffic-card .sub { color: var(--muted); font-size: 11.5px; line-height: 1.4; }
    #traffic-card .seg { display: grid; grid-template-columns: 1fr 1fr; background: #f1ece2; border-radius: 9px; padding: 2px; margin: 9px 0 8px; }
    #traffic-card .seg button { border: 0; background: none; border-radius: 7px; padding: 5px 0; font-size: 12px; font-weight: 600; color: var(--muted); cursor: pointer; }
    #traffic-card .seg button.on { background: #fff; color: var(--ink); box-shadow: 0 1px 3px rgba(0,0,0,.12); }
    #traffic-card .hr { display: none; align-items: center; gap: 8px; margin-bottom: 8px; font-variant-numeric: tabular-nums; }
    #traffic-card .hr.show { display: flex; }
    #traffic-card .hr input { flex: 1; accent-color: var(--red); }
    #traffic-card .ramp { display: flex; height: 7px; border-radius: 4px; overflow: hidden; }
    #traffic-card .ramp i { flex: 1; }
    #traffic-card .rl { display: flex; justify-content: space-between; color: var(--muted); font-size: 10.5px; margin-top: 3px; font-variant-numeric: tabular-nums; }
    #traffic-card .city { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line); display: flex; justify-content: space-between; }
    #traffic-card .city b { font-variant-numeric: tabular-nums; }
    @media (max-width: 640px) {
      #traffic-card { width: auto; right: calc(76px + env(safe-area-inset-right)); padding: 8px 10px 7px; font-size: 11.5px; }
      #traffic-card .sub, #traffic-card h3 { display: none; }
      #traffic-card .seg { margin: 0 0 6px; }
      #traffic-card .city { margin-top: 5px; padding-top: 5px; }
      body.card-open #traffic-card { display: none; } }
    body.bm-dark #traffic-card { background: rgba(30,32,38,.94); box-shadow: 0 6px 22px rgba(0,0,0,.5); }
    body.bm-dark #traffic-card .seg { background: #2a2d34; }
    body.bm-dark #traffic-card .seg button.on { background: #3a3e47; box-shadow: 0 1px 3px rgba(0,0,0,.4); }
    body.bm-dark .tr-pop .maplibregl-popup-content { background: #1d2025; color: var(--ink); }
    body.bm-dark .tr-pop .maplibregl-popup-tip { border-top-color: #1d2025; border-bottom-color: #1d2025; }
    .tr-pop .maplibregl-popup-content { border-radius: 12px; padding: 9px 12px; font-size: 12.5px; box-shadow: 0 6px 20px rgba(0,0,0,.18); }
    .tr-pop b { font-size: 15px; font-variant-numeric: tabular-nums; }
  `;
  document.head.appendChild(css);

  const GAUGE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>';
  const card = document.createElement('div');
  card.id = 'traffic-card';
  document.body.appendChild(card);
  const hh = h => `${String(h).padStart(2, '0')}:00`;

  function paintCard() {
    if (!on) { card.classList.remove('show'); return; }
    card.classList.add('show');
    const segs = SPD ? Object.values(SPD.s) : [];
    const nNow = segs.filter(r => r[0] >= 0).length, nH = segs.filter(r => r[2 + hour] >= 0).length;
    const age = SPD ? Math.round((Date.now() / 1000 - SPD.t) / 60) : null;
    const cityNow = SPD && SPD.city.now, cityH = SPD && SPD.city.h[hour];
    card.innerHTML = `<h3>${GAUGE}Bus speeds, observed</h3>`
      + `<div class="sub">From the GPS of MTC's live fleet${SPD ? ` · updated ${age <= 1 ? 'just now' : age + ' min ago'}` : ''}</div>`
      + `<div class="seg"><button data-m="now" class="${mode === 'now' ? 'on' : ''}">Last hour</button><button data-m="typ" class="${mode === 'typ' ? 'on' : ''}">Typical</button></div>`
      + `<div class="hr ${mode === 'typ' ? 'show' : ''}"><input type="range" min="0" max="23" value="${hour}" aria-label="Hour of day"><b>${hh(hour)}</b></div>`
      + `<div class="ramp">${RAMP.map(([, c]) => `<i style="background:${c}"></i>`).join('')}</div>`
      + `<div class="rl"><span>&lt;8</span><span>12</span><span>16</span><span>22</span><span>km/h</span></div>`
      + `<div class="city"><span>${mode === 'now' ? 'City median, last hour' : `City median at ${hh(hour)}`}</span>`
      + `<b>${(mode === 'now' ? cityNow : cityH) != null ? (mode === 'now' ? cityNow : cityH) + ' km/h' : '—'}</b></div>`
      + `<div class="sub" style="margin-top:5px">${mode === 'now' ? nNow : nH} road segments with buses${SPD ? '' : ' · loading…'}</div>`;
    card.querySelectorAll('[data-m]').forEach(b => b.addEventListener('click', () => { mode = b.dataset.m; draw(); }));
    const r = card.querySelector('input');
    r.addEventListener('input', () => { hour = +r.value; draw(); });
  }

  function colourExpr() {
    const e = ['step', ['get', 'v']];
    e.push(RAMP[0][1]);
    for (let i = 1; i < RAMP.length; i++) e.push(RAMP[i - 1][0], RAMP[i][1]);
    return e;
  }
  function addLayers() {
    if (map.getSource('traffic')) return;
    map.addSource('traffic', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    const before = map.getLayer('live-halo') ? 'live-halo' : (map.getLayer('stops') ? 'stops' : undefined);
    map.addLayer({ id: 'traffic-case', type: 'line', source: 'traffic', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#fff', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 3, 14, 7, 17, 12], 'line-opacity': .9 } }, before);
    map.addLayer({ id: 'traffic', type: 'line', source: 'traffic', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': colourExpr(), 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1.8, 14, 4.5, 17, 8] } }, before);
    map.on('click', 'traffic', e => {
      if (window.liveBusAt && window.liveBusAt(e.point)) return;
      const p = e.features[0].properties, row = SPD && SPD.s[p.k];
      if (!row) return;
      const typ = row[2 + new Date().getHours()];
      const html = mode === 'now'
        ? `<b>${row[0]} km/h</b> · last hour, ${row[1]} bus${row[1] === 1 ? '' : 'es'}`
          + (typ >= 0 ? `<br><span style="color:var(--muted)">typically ${typ} km/h at ${hh(new Date().getHours())}</span>` : '')
        : `<b>${row[2 + hour]} km/h</b> · typical at ${hh(hour)}<br><span style="color:var(--muted)">median over the last ${SPD.days} days</span>`;
      if (popup) popup.remove();
      popup = new maplibregl.Popup({ className: 'tr-pop', closeButton: false, maxWidth: '240px' }).setLngLat(e.lngLat).setHTML(html).addTo(map);
    });
    map.on('mouseenter', 'traffic', () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', 'traffic', () => map.getCanvas().style.cursor = '');
  }
  window.trafficAt = p => on && map.getLayer('traffic') && map.queryRenderedFeatures([[p.x - 4, p.y - 4], [p.x + 4, p.y + 4]], { layers: ['traffic'] }).length > 0;

  function draw() {
    paintCard();
    if (!on || !GEO || !SPD || !map.getSource('traffic')) return;
    const feats = [];
    for (const f of GEO.features) {
      const r = SPD.s[f.properties.k]; if (!r) continue;
      const v = mode === 'now' ? r[0] : r[2 + hour];
      if (v < 0) continue;
      feats.push({ type: 'Feature', geometry: f.geometry, properties: { k: f.properties.k, v } });
    }
    map.getSource('traffic').setData({ type: 'FeatureCollection', features: feats });
  }
  function loadSpeeds() {
    return fetch(`live/speeds.json?t=${Math.floor(Date.now() / 60000)}`).then(r => r.ok ? r.json() : null)
      .then(j => { if (j && j.s) SPD = j; draw(); }).catch(() => draw());
  }
  function start() {
    addLayers();
    ['traffic', 'traffic-case'].forEach(id => map.setLayoutProperty(id, 'visibility', 'visible'));
    map.setPaintProperty('routes', 'line-opacity', 0.22);
    paintCard();
    const g = GEO ? Promise.resolve() : fetch(`data/seg_geo.json?v=${DATA_V}`).then(r => r.json()).then(j => { GEO = j; });
    Promise.all([g, loadSpeeds()]).then(draw);
    clearInterval(refreshT); refreshT = setInterval(loadSpeeds, 5 * 60000);
  }
  function stop() {
    clearInterval(refreshT);
    ['traffic', 'traffic-case'].forEach(id => map.getLayer(id) && map.setLayoutProperty(id, 'visibility', 'none'));
    if (map.getLayer('routes')) map.setPaintProperty('routes', 'line-opacity', 0.7);
    if (popup) { popup.remove(); popup = null; }
    paintCard();
  }

  const btn = document.createElement('button');
  btn.className = 'lyr' + (on ? ' on' : '');
  btn.innerHTML = `${GAUGE}<span>Traffic (observed)</span>`;
  const lm = $('layer-menu');
  lm.insertBefore(btn, lm.children[1] || null);
  btn.addEventListener('click', e => {
    e.stopPropagation();
    on = !on; btn.classList.toggle('on', on);
    try { localStorage.setItem('mtcTraffic', on ? '1' : '0'); } catch (err) {}
    on ? start() : stop();
  });
  const boot = () => { if (on) start(); };
  if (map.loaded() && map.getSource('routes')) boot(); else map.once('idle', boot);
})();
