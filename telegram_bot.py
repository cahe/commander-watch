"""Telegram alerts for single decks: the hourly check's side.

People subscribe through the bot (@commanderwatchbot), which runs as a Cloudflare Worker (worker/): it answers
them instantly and keeps who watches which deck. After each scan, check.py works out which decks got cheaper or
can be ordered again (deck_alerts), asks the Worker who watches them, and messages those people here. The Worker
reads deck prices from data/bot.json, which check.py writes on every run.

Needs TELEGRAM_TOKEN, BOT_API_URL (the Worker's address) and BOT_ALERTS_KEY (shared with the Worker).
"""

from __future__ import annotations

import os

import requests

PRICE_STEP = 1.0  # zł; and a drop must be at least 2%, so CardTrader's currency drift doesn't alert


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code


def zl(price: float) -> str:
    return f"{price:,.2f}".replace(",", " ").replace(".", ",") + " zł"


def offer_text(price: float, shop: str, approx: bool) -> str:
    return f"≈ {zl(price)} on CardTrader" if approx else f"{zl(price)} at {shop}"


def deck_alerts(before: dict[str, dict], after: dict[str, dict]) -> dict[str, dict]:
    """Alerts for decks that got cheaper, or that can be ordered again: deck key -> {"text", "url", "label"}.

    before/after map deck key -> check.deck_prices' entry: "price" is the cheapest way to order the deck
    (CardTrader included; None if there's none), with its "shop", "url" and "approx" (True for CardTrader).
    """
    alerts = {}
    for key, now in after.items():
        was = before.get(key)
        if was is None or now["price"] is None:
            continue  # a deck seen for the first time, or not orderable now
        approx = now.get("approx", False)
        offer = offer_text(now["price"], now["shop"], approx)
        link = {"url": now["url"], "label": "Open on CardTrader" if approx else f"Open at {now['shop']}"}
        if was["price"] is None:
            alerts[key] = {"text": f"📦 {now['name']} can be ordered again: {offer}", **link}
        elif now["price"] <= was["price"] - max(PRICE_STEP, was["price"] * 0.02):
            alerts[key] = {"text": f"📉 {now['name']}: {offer} (was {zl(was['price'])})", **link}
    return alerts


class Alerts:
    def __init__(self, token: str, api_url: str, key: str, page_url: str = ""):
        self.token, self.api_url, self.key, self.page_url = token, api_url.rstrip("/"), key, page_url

    @classmethod
    def from_env(cls) -> Alerts | None:
        env = {k: os.environ.get(k, "").strip() for k in ("TELEGRAM_TOKEN", "BOT_API_URL", "BOT_ALERTS_KEY", "PAGE_URL")}
        if not (env["TELEGRAM_TOKEN"] and env["BOT_API_URL"] and env["BOT_ALERTS_KEY"]):
            return None
        return cls(env["TELEGRAM_TOKEN"], env["BOT_API_URL"], env["BOT_ALERTS_KEY"], env["PAGE_URL"])

    def worker(self, method: str, path: str, **kwargs):
        r = requests.request(method, f"{self.api_url}{path}", headers={"Authorization": f"Bearer {self.key}"},
                             timeout=30, **kwargs)
        r.raise_for_status()
        return r.json()

    def call(self, method: str, **params):
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params, timeout=30)
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(r.status_code, r.text[:200])
        if not body.get("ok"):
            raise TelegramError(body.get("error_code"), body.get("description", ""))
        return body["result"]

    def send(self, alerts: dict[str, dict]) -> int:
        """Message each deck's watchers. Returns how many messages went out."""
        if not alerts:
            return 0
        watchers = self.worker("GET", "/subscribers")
        sent = 0
        for key, a in alerts.items():
            for chat in watchers.get(key, []):
                buttons = [[{"text": a["label"], "url": a["url"]}]]
                if self.page_url:
                    buttons.append([{"text": "Commander Watch", "url": self.page_url}])
                try:
                    self.call("sendMessage", chat_id=int(chat), text=a["text"], disable_web_page_preview=True,
                              reply_markup={"inline_keyboard": buttons})
                    sent += 1
                except TelegramError as e:
                    if e.code == 403:  # they blocked the bot or deleted the chat: stop messaging them
                        self.worker("POST", "/unsubscribe", json={"chat": chat})
                    else:
                        print(f"Telegram: couldn't message a subscriber ({e})")
        return sent
