// Minimal service worker — installable PWA shell only, no offline caching.
// Caching is intentionally skipped so users always get the latest deployed
// version instead of a stale cached page (see windows-traps.md verification lessons).

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
