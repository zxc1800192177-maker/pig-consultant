// PWA 離線快取。只快取靜態外殼(HTML/CSS/JS/圖示),絕不碰 /api/*。
//
// /api/consult 是 SSE 串流、/api/grade 等是 POST,一旦被這裡攔截並
// 誤用 cache-first 回應,串流會整個斷掉或回傳過期資料。所以 fetch
// 事件只在「同源 GET 且在下面清單內」才接手,其餘一律不呼叫
// respondWith,交還瀏覽器原生處理。
//
// CACHE_VERSION 需要在每次靜態檔案改版時手動遞增,否則使用者的瀏覽器
// 會一直吃到舊快取,看不到新版畫面。
const CACHE_VERSION = "v2";
const CACHE_NAME = `pig-consultant-${CACHE_VERSION}`;

const PRECACHE_URLS = [
  "/",
  "/index.html",
  "/style.css",
  "/app.js",
  "/lib/format.js",
  "/lib/markdown.js",
  "/lib/sse.js",
  "/lib/speech.js",
  "/pig-sleeping.png",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-192.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  const isPrecached =
    req.method === "GET" &&
    url.origin === self.location.origin &&
    PRECACHE_URLS.includes(url.pathname);

  if (!isPrecached) return; // 交還瀏覽器原生處理,含所有 /api/*

  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone()));
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
