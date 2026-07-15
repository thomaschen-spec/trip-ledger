// 旅行手帳 service worker — 離線記帳支援。
// 策略：
//   - 只處理 GET、同源請求；POST 與任何 /api/ 一律不碰、不快取（離線記帳靠 IndexedDB 佇列，不靠 SW 快取請求）。
//   - HTML 導覽（頁面）：network-first，成功就更新 runtime cache；失敗回 cache，再沒有就回離線提示頁。
//   - /static/*（CSS 由內嵌 style 提供、主要是 icon/manifest 等靜態資源）：cache-first，背景更新。
// bump 這個字串＝強制所有裝置換新版快取。
const SW_VERSION = "trip-ledger-v1";
const RUNTIME_CACHE = `${SW_VERSION}-runtime`;
const PRECACHE = `${SW_VERSION}-precache`;

const PRECACHE_URLS = [
  "/offline",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(PRECACHE).then((cache) => cache.addAll(PRECACHE_URLS).catch(() => {}))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((name) => name.startsWith("trip-ledger-") && !name.startsWith(SW_VERSION))
          .map((name) => caches.delete(name))
      );
      await self.clients.claim();
    })()
  );
});

function isApiOrWrite(request, url) {
  if (request.method !== "GET") return true;
  if (url.pathname.includes("/api/")) return true;
  if (/\/receipt\/\d+\/image$/.test(url.pathname)) return true;
  return false;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // 只處理同源 GET，其餘一律放行、不碰快取（POST/API/收據圖片都在這裡被排除）。
  if (url.origin !== self.location.origin || isApiOrWrite(request, url)) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(request);
          const cache = await caches.open(RUNTIME_CACHE);
          cache.put(request, fresh.clone());
          return fresh;
        } catch (err) {
          const cache = await caches.open(RUNTIME_CACHE);
          const cached = await cache.match(request);
          if (cached) return cached;
          const offline = await caches.match("/offline");
          if (offline) return offline;
          return Response.error();
        }
      })()
    );
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(RUNTIME_CACHE);
        const cached = await cache.match(request);
        const networkFetch = fetch(request)
          .then((resp) => {
            cache.put(request, resp.clone());
            return resp;
          })
          .catch(() => null);
        return cached || (await networkFetch) || Response.error();
      })()
    );
  }
});
