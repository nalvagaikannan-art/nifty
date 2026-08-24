// Service Worker — AI NIFTY Option Analyzer Pro
// Caches app shell for offline access; API calls always go to network.

// BUG FIX (2026-08-22): bumped v1 -> v2 so browsers that already installed
// the old (broken) service worker register this fixed one instead of
// continuing to run the cached old version indefinitely.
const CACHE_NAME = 'nifty-ai-v2';

// App shell: static assets that rarely change
const SHELL_URLS = [
  '/',
  '/dashboard',
  '/market',
  '/options',
  '/analysis',
  '/settings',
  '/static/css/style.css',
  '/static/js/app.js',
  '/static/js/charts.js',
  '/static/js/websocket.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Install: cache app shell
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      // Don't fail install if some assets are missing
      return Promise.allSettled(
        SHELL_URLS.map(url => cache.add(url).catch(() => null))
      );
    }).then(() => self.skipWaiting())
  );
});

// Activate: clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
//   /api/* → Network only (live data must be fresh)
//   Static assets → Cache first, fallback to network
//   HTML pages → Network first, fallback to cache
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Skip non-GET and cross-origin requests
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;

  // API calls — always network, never cache.
  // BUG FIX (2026-08-22): `event.respondWith(fetch(event.request))` with no
  // .catch() meant that any transient network hiccup (very common on a
  // mobile connection — this app is a PWA installed on phones) made the
  // fetch() promise passed to respondWith() reject. That produces exactly
  // "The FetchEvent for '<URL>' resulted in a network error response: the
  // promise was rejected" in the console, and the browser hands the page's
  // own fetch() call a hard network error instead of a real HTTP response —
  // even though the server may have answered fine a moment later. On the
  // dashboard that showed up as every card stuck on "LOADING..." forever,
  // since ltRefresh()'s .catch() only shows a warning banner and never
  // resets those labels.
  // Fix: don't intercept /api/* through the service worker at all — just
  // `return` and let the browser perform its own normal fetch (with its
  // own retry/redirect handling), which is also simpler and strictly safer
  // than re-wrapping the request through an extra fetch() layer.
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // HTML pages — network first, cache fallback.
  // BUG FIX (2026-08-22): if the network fetch failed AND there was no
  // cached copy either, `caches.match()` resolves to `undefined` —
  // respondWith(undefined) is itself a rejection (a Response is required),
  // producing the same "network error response" failure this whole file is
  // being fixed for. Fall back to a minimal offline page instead of
  // `undefined` so that never happens.
  if (event.request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          return response;
        })
        .catch(() =>
          caches.match(event.request).then(cached =>
            cached || new Response(
              '<h1>Offline</h1><p>No connection and no cached copy of this page yet.</p>',
              { status: 503, headers: { 'Content-Type': 'text/html' } }
            )
          )
        )
    );
    return;
  }

  // Static assets — cache first, network fallback.
  // BUG FIX (2026-08-22): the network fetch() here had no .catch() — a
  // failed fetch (offline, flaky connection) rejected the whole
  // respondWith() promise the same way the /api/ branch used to.
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => new Response('', { status: 503, statusText: 'Offline' }));
    })
  );
});
