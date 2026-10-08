// Commander Watch's service worker: shows the deck alerts the Worker pushes (worker/src/webpush.js), also while
// the page is closed. A tap opens the page at that deck; "Open at <shop>" opens the cheapest offer.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let m;
  try { m = e.data.json(); } catch { m = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(m.title || "Commander Watch", {
    body: m.body || "",
    icon: "icons/icon-192.png",
    badge: "icons/badge-96.png",  // Android's status bar
    tag: m.tag,                   // a newer alert for the same deck replaces the older one
    renotify: Boolean(m.tag),
    data: { url: m.url || self.registration.scope, shopUrl: m.shopUrl },
    actions: m.shopUrl ? [{ action: "shop", title: m.shopLabel || "Open shop" }] : [],
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const { url, shopUrl } = e.notification.data || {};
  const target = e.action === "shop" && shopUrl ? shopUrl : url;
  e.waitUntil((async () => {
    if (target.startsWith(self.registration.scope)) {
      // An open Commander Watch tab is reused: it's told which deck to show.
      const tabs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const tab = tabs.find(t => t.url.startsWith(self.registration.scope));
      if (tab) { tab.postMessage({ open: target }); return tab.focus(); }
    }
    return self.clients.openWindow(target);
  })());
});
