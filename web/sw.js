/* The MTC map as an installed app. Live data is never cached: live/, api/ and anything without a
   ?v= version go to the network first, the cache only when offline. Versioned files (?v=..., the
   scripts, sprite sheets and data/) never change under one URL, so they come from the cache once
   fetched. A new deploy changes the ?v= and so fetches afresh. */
const C = 'mtc-v1';
const SHELL = ['./', 'manifest.webmanifest', 'pwa/icon-192.png', 'pwa/icon.svg'];
self.addEventListener('install', e => { e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;            // tiles, CDNs: the browser's own cache
  if (/\/(live|api|rt)\//.test(u.pathname)) return;                          // live buses: always the network
  if (u.searchParams.has('v')) {                                             // versioned: cache first
    e.respondWith(caches.match(r).then(hit => hit || fetch(r).then(res => {
      if (res.ok) { const cp = res.clone(); caches.open(C).then(c => c.put(r, cp)); }
      return res;
    })));
    return;
  }
  e.respondWith(fetch(r).then(res => {                                       // the page and the rest: network first
    if (res.ok && (r.mode === 'navigate' || SHELL.some(s => u.pathname.endsWith(s.replace('./', ''))))) {
      const cp = res.clone(); caches.open(C).then(c => c.put(r.mode === 'navigate' ? './' : r, cp));
    }
    return res;
  }).catch(() => caches.match(r).then(hit => hit || (r.mode === 'navigate' ? caches.match('./') : undefined))));
});
