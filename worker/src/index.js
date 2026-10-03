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
// Secrets (wrangler secret put): TELEGRAM_TOKEN, WEBHOOK_SECRET (Telegram sends it back on every webhook call),
// ALERTS_KEY, GITHUB_TOKEN (fine-grained, this repo only, Actions: read and write). Vars and the KV binding
// (SUBS) are in wrangler.toml.

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
