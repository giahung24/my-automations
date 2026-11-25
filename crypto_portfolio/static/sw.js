/* Service worker: offline app shell only. Balances and settings (/api) always go to the
 * network - a cached net worth would be a lie, and settings must never be served stale.
 *
 * Shell files: network first (so a deploy shows up on the next open), cache as fallback.
 * Vendored libraries, fonts, icons: cache first (their content only changes with the path).
 * Bump VERSION to drop every old cache.
 */
const VERSION = 'portfolio-v2';
const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/manifest.json',
  '/static/vendor/vue.global.prod.js',
  '/static/vendor/lightweight-charts.standalone.production.js',
  '/static/vendor/tailwindcss-browser.js',
  '/static/fonts/manrope-latin-wght-normal.woff2',
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
];
const IMMUTABLE = ['/static/vendor/', '/static/fonts/', '/static/icons/', '/static/coins/'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  if (IMMUTABLE.some((prefix) => url.pathname.startsWith(prefix))) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      })),
    );
    return;
  }

  const key = request.mode === 'navigate' ? '/' : request;
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && (request.mode === 'navigate' || SHELL.includes(url.pathname))) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(key, copy));
        }
        return response;
      })
      .catch(() => caches.match(key).then((hit) => hit || Response.error())),
  );
});
