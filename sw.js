// Commander Watch's service worker: shows the deck alerts the Worker pushes (worker/src/webpush.js), also while
// the page is closed. A tap opens the page at that deck; "Open at <shop>" opens the cheapest offer, and
// "Stop alerts" turns that deck's notifications off for this device.
const PUSH_API = "https://commander-watch-bot.maciej-janowski.workers.dev/push";
const ICONS = { down: "icons/alert-down.png", stock: "icons/alert-stock.png" };  // price down, back in stock

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let m;
  try { m = e.data.json(); } catch { m = { body: e.data ? e.data.text() : "" }; }
  const actions = [];
  if (m.shopUrl) actions.push({ action: "shop", title: m.shopLabel || "Open shop" });
  if (m.kind && m.tag) actions.push({ action: "stop", title: "Stop alerts" });
  e.waitUntil(self.registration.showNotification(m.title || "Commander Watch", {
    body: m.body || "",
    icon: ICONS[m.kind],          // none for the confirmation, so its text gets the full width
    badge: "icons/badge-96.png",  // Android's status bar
    tag: m.tag,                   // a newer alert for the same deck replaces the older one
    renotify: Boolean(m.tag),
    timestamp: m.at ? Date.parse(m.at) : Date.now(),  // when the hourly check found it
    vibrate: [60, 40, 60],
    data: { url: m.url || self.registration.scope, shopUrl: m.shopUrl, deck: m.tag },
    actions,
  }));
});

async function stopAlerts(deck, name) {
  const sub = await self.registration.pushManager.getSubscription();
  if (!sub) return;
  const r = await fetch(PUSH_API, { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ subscription: sub.toJSON(), remove: deck }) });
  const { decks } = await r.json();
  if (!decks?.length) await sub.unsubscribe();
  await self.registration.showNotification(name, { body: "Alerts off for this deck. Tap its bell on Commander Watch to turn them on again.",
    badge: "icons/badge-96.png", tag: deck, silent: true, data: { url: self.registration.scope } });
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const { url, shopUrl, deck } = e.notification.data || {};
  if (e.action === "stop" && deck) return e.waitUntil(stopAlerts(deck, e.notification.title).catch(() => {}));
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
