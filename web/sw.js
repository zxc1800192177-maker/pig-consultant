// PWA 離線快取。只快取靜態外殼(HTML/CSS/JS/圖示),絕不碰 /api/*。
//
// /api/consult 是 SSE 串流、/api/grade 等是 POST,一旦被這裡攔截並
// 誤用 cache-first 回應,串流會整個斷掉或回傳過期資料。所以 fetch
// 事件只在「同源 GET 且在下面清單內」才接手,其餘一律不呼叫
// respondWith,交還瀏覽器原生處理。
//
// CACHE_VERSION 需要在每次靜態檔案改版時手動遞增,否則使用者的瀏覽器
// 會一直吃到舊快取,看不到新版畫面。
const CACHE_VERSION = "v21";
const CACHE_NAME = `pig-consultant-${CACHE_VERSION}`;

// 程式碼:一律先問網路,拿不到才用快取(也就是只在離線時用)。
//
// 這裡曾經跟圖片一樣走「快取優先」,造成兩個真實發生過的問題:
//   1. 每次部署後,使用者第一次載入必定看到舊版,要再重新整理一次才更新。
//   2. 每個檔案各自更新,會出現「舊的 index.html 配新的 app.js」——
//      新 JS 找不到舊 HTML 裡還不存在的元素,整個檔案的事件接線中斷,
//      畫面上所有按鈕都沒反應。這個症狀真的被回報過,當時只加了防呆
//      (檢查元素存在才接線),沒有解決根因,所以又復發了一次。
//
// 代價是有網路時每次載入多一次網路往返。以這個站的體積(HTML+CSS+JS
// 不到 100KB)完全可以接受,遠比「使用者看到的是哪一版無法確定」好。
const CODE_URLS = [
  "/",
  "/index.html",
  "/style.css",
  "/v2.css",
  "/app.js",
  "/lib/format.js",
  "/lib/markdown.js",
  "/lib/sse.js",
  "/lib/factors.js",
  "/lib/v2.js",
  "/lib/record.js",
];

// 靜態素材:內容幾乎不變、檔案較大,而且不會造成版本錯配 ——
// 圖示配上新版 HTML 也不會壞掉。維持快取優先以求載入速度。
const ASSET_URLS = [
  "/pig-sleeping.png",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-192.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

const PRECACHE_URLS = [...CODE_URLS, ...ASSET_URLS];

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

function saveToCache(req, res) {
  // 只快取成功的回應。把 404 或 502 存起來的話,離線時會拿它當「內容」用。
  if (res && res.ok) {
    const copy = res.clone();
    caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
  }
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  const path = url.pathname;
  const isCode = CODE_URLS.includes(path);
  const isAsset = ASSET_URLS.includes(path);

  // 其餘一律不呼叫 respondWith,交還瀏覽器原生處理 —— 含所有 /api/*。
  // /api/consult 是 SSE 串流,被攔截就會整個斷掉。
  if (!isCode && !isAsset) return;

  if (isCode) {
    // 網路優先:線上永遠拿到最新版,而且 HTML 與 JS 必定同一版。
    // 離線時才退回快取,PWA 的離線能力不受影響。
    event.respondWith(
      fetch(req)
        .then((res) => saveToCache(req, res))
        .catch(() => caches.match(req))
    );
    return;
  }

  // 素材維持快取優先,背景更新供下次使用
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => saveToCache(req, res))
        .catch(() => cached);
      return cached || network;
    })
  );
});
