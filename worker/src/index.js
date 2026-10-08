// Commander Watch's Telegram bot (@commanderwatchbot) as a Cloudflare Worker.
//
// Telegram calls POST /telegram the moment someone writes to the bot, so replies are instant. Who watches which
// deck is kept in Workers KV (private to the Cloudflare account, never in the public repo). Prices come from
// data/bot.json, which the hourly check (check.py) publishes to GitHub Pages.
//
// The hourly check works out the alerts itself and asks GET /subscribers who watches each deck; it reports
// people who blocked the bot to POST /unsubscribe. Both need the shared ALERTS_KEY. POST /setup (same key)
// points Telegram's webhook here and sets the bot's description and command menu.
//
// It also starts the hourly check: a Cron Trigger (wrangler.toml) runs check.yml through GitHub's
// workflow_dispatch API, which GitHub's own schedule does too unreliably.
//
// Device notifications (Web Push) work the same way without Telegram: the page's bell sends the browser's push
// subscription and its decks to POST /push (public), kept in KV as push:<id>; the hourly check posts each alert
// to POST /push/send (ALERTS_KEY), which encrypts and sends it to every device watching that deck (webpush.js).
//
// Secrets (wrangler secret put): TELEGRAM_TOKEN, WEBHOOK_SECRET (Telegram sends it back on every webhook call),
// ALERTS_KEY, GITHUB_TOKEN (fine-grained, this repo only, Actions: read and write), VAPID_PRIVATE_KEY (a JWK; set
// by the deploy workflow from the GitHub secret). Vars and the KV binding (SUBS) are in wrangler.toml.

import { b64u, sendPush } from "./webpush.js";

const DESCRIPTION = "Price alerts for MTG Commander decks in Polish shops and on CardTrader.\n\n" +
  "Tap the bell next to a deck on Commander Watch, then Start here. I'll message you when that deck " +
  "gets cheaper or can be ordered again.";
const SHORT_DESCRIPTION = "Commander deck price alerts from Commander Watch";
const COMMANDS = [
  { command: "list", description: "Decks you watch, with buttons to stop each" },
  { command: "stop", description: "Stop all alerts" },
];

export default {
  // Hourly: start the check workflow.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(startCheck(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/telegram") {
      if (!same(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.WEBHOOK_SECRET)) {
        return new Response("forbidden", { status: 403 });
      }
      try {
        await handleUpdate(await request.json(), env);
      } catch (e) {
        console.log("update failed:", e.stack || e);
      }
      return new Response("ok");  // always 200, so Telegram doesn't resend an update that breaks us
    }
    if (url.pathname === "/push" && (request.method === "POST" || request.method === "OPTIONS")) {
      const cors = corsHeaders(request, env);
      if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
      let result;
      try {
        result = await handlePushSubscription(await request.json(), env);
      } catch (e) {
        result = { status: 400, body: { error: String(e.message || e) } };
      }
      return Response.json(result.body, { status: result.status || 200, headers: cors });
    }
    if (!same(request.headers.get("Authorization"), `Bearer ${env.ALERTS_KEY}`)) {
      return new Response("not found", { status: 404 });
    }
    if (request.method === "GET" && url.pathname === "/subscribers") {
      return Response.json(byDeck(await loadChats(env)));
    }
    if (request.method === "POST" && url.pathname === "/unsubscribe") {
      const { chat } = await request.json();
      const chats = await loadChats(env);
      delete chats[String(chat)];
      await saveChats(env, chats);
      return Response.json({ ok: true });
    }
    if (request.method === "POST" && url.pathname === "/push/send") {
      return Response.json(await sendDeckAlert(await request.json(), env));
    }
    if (request.method === "POST" && url.pathname === "/run-check") {  // to test the trigger by hand
      return Response.json(await startCheck(env));
    }
    if (request.method === "POST" && url.pathname === "/setup") {
      const results = {
        webhook: await tg(env, "setWebhook", { url: `${url.origin}/telegram`, secret_token: env.WEBHOOK_SECRET,
                                               allowed_updates: ["message", "callback_query"] }),
        description: await tg(env, "setMyDescription", { description: DESCRIPTION }),
        shortDescription: await tg(env, "setMyShortDescription", { short_description: SHORT_DESCRIPTION }),
        commands: await tg(env, "setMyCommands", { commands: COMMANDS }),
      };
      return Response.json(results);
    }
    return new Response("not found", { status: 404 });
  },
};

// ------------------------------------------------------------------ hourly check

async function startCheck(env) {
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/check.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2026-03-10",
      "User-Agent": "commander-watch-bot",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  const result = { status: r.status, ok: r.ok };  // 204, or 200 with the new run's id (API 2026-03-10)
  if (!result.ok) {
    result.error = (await r.text()).slice(0, 300);
    console.log("starting the check failed:", result.status, result.error);
  }
  return result;
}

// ------------------------------------------------------------------ Telegram

async function tg(env, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params),
  });
  return r.json();
}

async function send(env, chat, text, buttons = []) {
  const rows = buttons.filter(row => row.length);
  const params = { chat_id: Number(chat), text, disable_web_page_preview: true };
  if (rows.length) params.reply_markup = { inline_keyboard: rows };
  return tg(env, "sendMessage", params);
}

// ------------------------------------------------------------------ subscriptions (one KV entry: chat id -> deck keys)

async function loadChats(env) {
  return (await env.SUBS.get("chats", "json")) || {};
}

async function saveChats(env, chats) {
  await env.SUBS.put("chats", JSON.stringify(chats));
}

function byDeck(chats) {
  const out = {};
  for (const [chat, keys] of Object.entries(chats)) for (const key of keys) (out[key] ||= []).push(chat);
  return out;
}

// ------------------------------------------------------------------ device notifications (Web Push)

// Push services the big browsers use; subscriptions pointing anywhere else are refused, so this can't be used
// to make the Worker post to arbitrary addresses.
const PUSH_HOSTS = /(^|\.)(fcm\.googleapis\.com|push\.services\.mozilla\.com|push\.apple\.com|notify\.windows\.com)$/;
const DECK_KEY = /^[a-z0-9-]{1,64}$/;
const MAX_DECKS = 50;

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin") || "";
  const allowed = [new URL(env.PAGE_URL).origin, "http://localhost:8765"];  // the page, and its local preview
  return {
    "Access-Control-Allow-Origin": allowed.includes(origin) ? origin : allowed[0],
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function checkSubscription(sub) {
  const endpoint = new URL(sub?.endpoint || "");
  if (endpoint.protocol !== "https:" || !PUSH_HOSTS.test(endpoint.hostname)) throw new Error("unsupported push service");
  const p256dh = b64u.decode(sub.keys?.p256dh || ""), auth = b64u.decode(sub.keys?.auth || "");
  if (p256dh.length !== 65 || p256dh[0] !== 4 || auth.length !== 16) throw new Error("bad subscription keys");
  return { endpoint: endpoint.href, keys: { p256dh: sub.keys.p256dh, auth: sub.keys.auth } };
}

async function pushId(endpoint) {
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint)));
  return "push:" + [...hash.slice(0, 16)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function pushOptions(env) {
  return { privateJwk: JSON.parse(env.VAPID_PRIVATE_KEY), publicKey: env.VAPID_PUBLIC_KEY, subject: env.PAGE_URL };
}

// POST /push {subscription, decks?, added?}: with decks, this device now watches exactly those (none: forget it);
// without, it just asks which decks it watches. "added" is the deck just turned on: it gets a confirmation push,
// which also proves the whole way to the device works.
async function handlePushSubscription(body, env) {
  const sub = checkSubscription(body.subscription);
  const id = await pushId(sub.endpoint);
  if (typeof body.remove === "string") {  // "Stop alerts" on a notification: just that deck
    const saved = await env.SUBS.get(id, "json");
    const keys = (saved?.decks || []).filter(k => k !== body.remove);
    if (keys.length) await env.SUBS.put(id, JSON.stringify({ ...saved, decks: keys }));
    else await env.SUBS.delete(id);
    return { body: { decks: keys } };
  }
  if (!Array.isArray(body.decks)) {
    const saved = await env.SUBS.get(id, "json");
    return { body: { decks: saved?.decks || [] } };
  }
  const decks = await loadDecks(env);
  const keys = [...new Set(body.decks.filter(k => typeof k === "string" && DECK_KEY.test(k) && decks[k]))].slice(0, MAX_DECKS);
  if (!keys.length) {
    await env.SUBS.delete(id);
    return { body: { decks: [] } };
  }
  await env.SUBS.put(id, JSON.stringify({ sub, decks: keys, at: new Date().toISOString() }));
  const added = keys.includes(body.added) ? decks[body.added] : null;
  if (added) {
    const r = await sendPush(sub, {
      title: shortName(added.name), body: `Alerts on · now ${cheapestOffer(added)}\n` +
        "You'll get a notification when it gets cheaper or can be ordered again.",
      url: deckPageUrl(env, body.added), tag: body.added, at: new Date().toISOString(),
    }, pushOptions(env));
    if (r.status === 404 || r.status === 410) {
      await env.SUBS.delete(id);
      return { status: 410, body: { error: "the browser's push subscription has expired", decks: [] } };
    }
    if (!r.ok) console.log("confirmation push failed:", r.status, (await r.text()).slice(0, 200));
  }
  return { body: { decks: keys } };
}

const deckPageUrl = (env, key) => `${env.PAGE_URL}#deck=${encodeURIComponent(key)}`;

// "Foundations Starter – Tramplesaurus Rex" → "Tramplesaurus Rex": notification titles are short on phones.
const shortName = (name) => name.split(" – ").pop();

function cheapestOffer(deck) {
  const ct = deck.ctPrice != null && (deck.shopPrice == null || deck.ctPrice < deck.shopPrice);
  return ct ? `≈ ${zl(deck.ctPrice)} on CardTrader` : deck.shopPrice != null ? `${zl(deck.shopPrice)} at ${deck.shopName}` : "not in stock anywhere";
}

// POST /push/send {deck, title, body, kind, at, shopUrl?, shopLabel?} from the hourly check: one alert to every
// device watching that deck. Subscriptions the push service no longer knows are dropped.
async function sendDeckAlert(alert, env) {
  const out = { sent: 0, removed: 0, failed: 0 };
  if (!DECK_KEY.test(alert.deck || "")) return { ...out, error: "bad deck" };
  const message = { title: alert.title, body: alert.body, kind: alert.kind, at: alert.at, url: deckPageUrl(env, alert.deck),
                    tag: alert.deck, shopUrl: alert.shopUrl, shopLabel: alert.shopLabel };
  let cursor;
  do {
    const page = await env.SUBS.list({ prefix: "push:", cursor });
    for (const { name } of page.keys) {
      const saved = await env.SUBS.get(name, "json");
      if (!saved?.decks?.includes(alert.deck)) continue;
      const r = await sendPush(saved.sub, message, pushOptions(env));
      if (r.status === 404 || r.status === 410) { await env.SUBS.delete(name); out.removed++; }
      else if (r.ok) out.sent++;
      else { out.failed++; console.log("push failed:", r.status, (await r.text()).slice(0, 200)); }
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return out;
}

// ------------------------------------------------------------------ deck data and texts

async function loadDecks(env) {
  // Cached for 5 minutes at Cloudflare's edge; the data only changes hourly.
  const r = await fetch(env.DATA_URL, { cf: { cacheTtl: 300, cacheEverything: true } });
  return r.ok ? (await r.json()).decks || {} : {};
}

// "1 249,99 zł", as the page and check.py write prices.
function zl(price) {
  const [whole, cents] = price.toFixed(2).split(".");
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, " ")},${cents} zł`;
}

function statusLine(deck) {
  const parts = [];
  if (deck.shopPrice != null) parts.push(`${zl(deck.shopPrice)} at ${deck.shopName}`);
  if (deck.ctPrice != null) parts.push(`CardTrader ≈ ${zl(deck.ctPrice)}`);
  return parts.join(" · ") || "not in stock anywhere";
}

function pageButton(env) {
  return [{ text: "Commander Watch", url: env.PAGE_URL }];
}

function deckButtons(env, deck) {
  const row = [];
  if (deck.shopUrl) row.push({ text: `Open at ${deck.shopName}`, url: deck.shopUrl });
  if (deck.ctUrl) row.push({ text: "CardTrader", url: deck.ctUrl });
  return [row, pageButton(env)];
}

const HELP = "I send alerts for single Commander decks: when one gets cheaper (in a shop or on CardTrader), " +
  "or a sold-out deck can be ordered again.\n\n" +
  "To watch a deck, open Commander Watch and tap the bell next to it, then Start.\n\n" +
  "/list shows the decks you watch, with buttons to stop each one. /stop stops all alerts.";

function listMessage(keys, decks) {
  const known = keys.filter(k => decks[k]);
  if (!known.length) return { text: "You're not watching any decks. Tap the bell next to a deck on Commander Watch to start.", buttons: [] };
  return {
    text: "You're watching:\n" + known.map(k => `• ${decks[k].name}: ${statusLine(decks[k])}`).join("\n"),
    buttons: known.map(k => [{ text: `Stop: ${decks[k].name.slice(0, 40)}`, callback_data: `stop:${k}` }]),
  };
}

// ------------------------------------------------------------------ commands

async function handleUpdate(update, env) {
  if (update.callback_query) {
    const q = update.callback_query, chat = String(q.message.chat.id), data = q.data || "";
    if (!data.startsWith("stop:")) return;
    const key = data.slice(5);
    const [chats, decks] = await Promise.all([loadChats(env), loadDecks(env)]);
    const keys = (chats[chat] || []).filter(k => k !== key);
    if (keys.length) chats[chat] = keys; else delete chats[chat];
    await saveChats(env, chats);
    await tg(env, "answerCallbackQuery", { callback_query_id: q.id, text: `Stopped alerts for ${decks[key]?.name || "that deck"}` });
    const { text, buttons } = listMessage(keys, decks);
    await tg(env, "editMessageText", { chat_id: Number(chat), message_id: q.message.message_id, text,
                                       reply_markup: { inline_keyboard: buttons }, disable_web_page_preview: true });
    return;
  }
  const msg = update.message;
  if (!msg || msg.chat?.type !== "private" || !msg.text) return;  // one-to-one chats only
  const chat = String(msg.chat.id), words = msg.text.trim().split(/\s+/);
  const command = words[0].split("@")[0].toLowerCase();

  if (command === "/start" && words[1]) {
    const key = words[1];
    const decks = await loadDecks(env);
    if (!decks[key]) {
      return send(env, chat, "I don't know that deck any more (shops may have renamed it). " +
                             "Open Commander Watch and tap its bell again.", [pageButton(env)]);
    }
    const chats = await loadChats(env);
    if (!(chats[chat] ||= []).includes(key)) chats[chat].push(key);
    await saveChats(env, chats);
    return send(env, chat, `Watching ${decks[key].name}.\nNow: ${statusLine(decks[key])}.\n\n` +
      "I'll message you when it gets cheaper, or when it can be ordered again after selling out. " +
      "/list shows what you watch.", deckButtons(env, decks[key]));
  }
  if (command === "/list") {
    const [chats, decks] = await Promise.all([loadChats(env), loadDecks(env)]);
    const { text, buttons } = listMessage(chats[chat] || [], decks);
    return send(env, chat, text, buttons);
  }
  if (command === "/stop") {
    const chats = await loadChats(env);
    delete chats[chat];
    await saveChats(env, chats);
    return send(env, chat, "Stopped all alerts. Tap a deck's bell on the page to watch it again.", [pageButton(env)]);
  }
  return send(env, chat, HELP, [pageButton(env)]);
}

// Constant-time comparison for the secrets.
function same(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || !b) return false;
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  return x.length === y.length && crypto.subtle.timingSafeEqual(x, y);
}
