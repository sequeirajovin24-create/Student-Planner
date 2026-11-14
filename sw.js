/* StudentPlanner service worker: keeps the app files on the phone so the app opens
   instantly and works with no internet at all. Tasks live in the phone's own storage,
   never here. Paths are relative so the app works from any folder or web address. */
const CACHE = "studentplanner-shell-v2";
const SCOPE = self.registration.scope;
const SHELL = ["./", "index.html", "scene.js", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"]
  .map((p) => new URL(p, SCOPE).href);
const HOME = new URL("./", SCOPE).href;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;   // not ours
  const key = req.mode === "navigate" ? HOME : url.origin + url.pathname;
  // network first (so updates arrive when online), saved copy when offline
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok && SHELL.includes(key)) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); }
      return res;
    }).catch(() => caches.match(key).then((r) => r || Response.error()))
  );
});
