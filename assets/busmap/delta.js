/* The live feed, downloaded once and then only what changed (server: assets/busmap/server/live_delta.py).

     const F = BUSDELTA.feed('live/vehicles.json');
     F.get().then(j => ...)     j is the whole feed, the same shape as vehicles.json, kept up to date

   The first call fetches vehicles.json (~1 MB for 9,000 buses). After that each call asks for
   delta/<t>.json, t being the feed's own time as we hold it: 404 = nothing newer yet (we keep what we
   have); a delta = the buses that changed, applied, then the next one is asked for straight away (a tab
   that slept catches up in a few small requests). The whole file is fetched again only when the deltas
   do not reach: the server has none for us (restarted, or we were away longer than it keeps them), our
   copy is more than 90 s older than it should be, or every 30 min as a safety net. A server with no delta
   service at all is noticed after two misses and simply polled for the whole file, as before.
   Any error falls back to the whole file, so a page never ends up worse off than without deltas. */
'use strict';
const BUSDELTA = (() => {
  const getJSON = (u, ms = 20000) => {
    const ac = typeof AbortController !== 'undefined' ? new AbortController() : null, tm = ac && setTimeout(() => ac.abort(), ms);
    return fetch(u, { cache: 'no-store', ...(ac ? { signal: ac.signal } : {}) })
      .then(r => { if (!r.ok) throw Object.assign(new Error(r.status), { status: r.status }); return r.json(); })
      .finally(() => tm && clearTimeout(tm));
  };
  function feed(url, opt = {}) {
    const dir = url.replace(/[^/]*$/, '') + 'delta/';
    const FULL_EVERY = opt.fullEvery || 30 * 60e3, STALE = opt.stale || 90;
    let deltaOk = false;              // has this server ever given us a delta? (no: it runs no delta service; plain polling)
    let snap = null, list = null, idOf = null, rows = null, fullAt = 0, busy = null;
    const stats = { full: 0, deltas: 0, bytes: 0 };
    function adopt(j) {
      list = Array.isArray(j.buses) && j.cols ? 'buses' : 'v';
      const i = list === 'buses' ? j.cols.indexOf('id') : 0;
      idOf = r => String(r[i]);
      rows = new Map((j[list] || []).map(r => [idOf(r), r]));
      snap = j; fullAt = Date.now();
    }
    async function full() {
      const j = await getJSON(`${url}?t=${Date.now()}`);
      adopt(j); stats.full++;
      return snap;
    }
    function apply(d) {
      for (const k of d.rm || []) rows.delete(String(k));
      for (const r of d.up || []) rows.set(idOf(r), r);
      snap = { ...(d.meta || {}), t: d.t, [list]: [...rows.values()] };
      stats.deltas++;
    }
    async function step() {
      if (!snap || Date.now() - fullAt > FULL_EVERY || (!deltaOk && stats.full >= 2)) return full();
      try {
        for (let n = 0; n < 20; n++) {
          let d;
          try { d = await getJSON(`${dir}${snap.t}.json`); }
          catch (e) { if (e.status === 404) break; throw e; }
          if (!d || d.from !== snap.t) throw new Error('delta out of step');
          apply(d); deltaOk = true;
        }
      } catch (e) { return full(); }
      // nothing newer for us although the feed should have moved on: the deltas no longer reach us
      if (typeof snap.t === 'number' && Date.now() / 1000 - snap.t > STALE) return full();
      return snap;
    }
    // one request chain at a time: a second caller waits for the same answer
    const get = () => busy || (busy = step().finally(() => { busy = null; }));
    return { get, full, stats, get t() { return snap && snap.t; } };
  }
  return { feed };
})();
