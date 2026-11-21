/* Keeps a copy of the pages staff opened, so the checkpoint still opens without signal.
 * Pages: try the network first, fall back to the saved copy. Style and scripts: saved copy first.
 */
var CACHE = 'wh-v1';
var ASSETS = ['/static/style.css', '/static/offline.js'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(ASSETS); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.indexOf('/static/') === 0 && url.pathname.indexOf('/static/leaflet/') !== 0) {
    e.respondWith(caches.match(req).then(function (hit) {
      var net = fetch(req).then(function (r) {
        if (r.ok) { var copy = r.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); }
        return r;
      });
      return hit || net;
    }));
    return;
  }
  // Only the checkpoint pages are kept, not the admin pages.
  if (req.mode === 'navigate' && /^\/checkpoint\/\d+(\/groep\/\d+)?$/.test(url.pathname)) {
    e.respondWith(fetch(req).then(function (r) {
      if (r.ok && !r.redirected) { var copy = r.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); }
      return r;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || new Response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">' +
          '<p style="font:16px system-ui;padding:1rem">Geen verbinding, en deze pagina is nog niet eerder geopend op dit toestel.</p>',
          {headers: {'Content-Type': 'text/html; charset=utf-8'}});
      });
    }));
  }
});
