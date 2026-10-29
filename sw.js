// X 情报站 service worker
// HTML: network-first (newest brief when online), fallback to cache offline.
// Icons / manifest: cache-first.
const VERSION = '90109fb442';
const CACHE = 'xbrief-' + VERSION;
const PRECACHE = ['./', './index.html', './archive.html', './favorites.html', './manifest.webmanifest',
  './icons/icon-180.png', './icons/icon-192.png', './icons/icon-512.png',
  './icons/maskable-192.png', './icons/maskable-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => Promise.all(PRECACHE.map(u => c.add(new Request(u, {cache: 'reload'})).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k.startsWith('xbrief-') && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function isHTML(req) {
  return req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html') || /\.html?$/.test(new URL(req.url).pathname);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (isHTML(req)) {
    e.respondWith((async () => {
      try {
        const res = await fetch(req, {cache: 'no-store'});
        if (res && res.ok) { const c = await caches.open(CACHE); c.put(req, res.clone()); }
        return res;
      } catch (err) {
        const hit = await caches.match(req, {ignoreSearch: true});
        if (hit) return hit;
        return (await caches.match('./index.html')) || (await caches.match('./')) ||
          new Response('<meta charset=utf-8><p style="font:16px sans-serif;padding:2em">离线中，且该页面尚未缓存。</p>',
            {headers: {'Content-Type': 'text/html; charset=utf-8'}});
      }
    })());
    return;
  }

  if (/\/icons\/|\.(png|jpe?g|webp|svg|ico|webmanifest)$/.test(url.pathname)) {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return res;
    })));
    return;
  }
  // everything else: network, fallback cache
  e.respondWith(fetch(req).catch(() => caches.match(req)));
});
