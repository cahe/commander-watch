"""Telegram alerts for single decks.

Someone who cares about one deck taps its "Get alerts" link on the page (t.me/<bot>?start=<deck key>),
which opens the bot in Telegram. Each check (check.py) then reads the bot's new messages, records who
watches which deck, and messages them when that deck's cheapest shop price drops or it can be ordered
again. Only the bot can message its users, so nobody can fake an alert.

There's no server: Telegram keeps new messages until they're fetched, by the hourly check or by the
"Bot replies" workflow that cron-job.org starts every 2 minutes (`check.py --bot`). Subscribers' chat ids are personal data and the repo is public, so the
list is stored encrypted (data/subscribers.enc), with a key derived from the bot token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

PRICE_STEP = 1.0  # zł; smaller drops aren't worth a message

# What people see before pressing Start, and the command menu. Bump PROFILE_VERSION to send changes.
PROFILE_VERSION = 1
DESCRIPTION = ("Price alerts for MTG Commander decks in Polish shops and on CardTrader.\n\n"
               "Tap the bell next to a deck on Commander Watch, then Start here. I'll message you when that deck "
               "gets cheaper or can be ordered again. Replies take a minute or two.")
SHORT_DESCRIPTION = "Commander deck price alerts from Commander Watch"
COMMANDS = [{"command": "list", "description": "Decks you watch, with buttons to stop each"},
            {"command": "stop", "description": "Stop all alerts"}]


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code


def zl(price: float) -> str:
    return f"{price:,.2f}".replace(",", " ").replace(".", ",") + " zł"


class Bot:
    def __init__(self, token: str, path: Path, page_url: str = ""):
        self.token, self.path, self.page_url = token, path, page_url
        key = base64.urlsafe_b64encode(hashlib.sha256(b"commander-watch subscribers:" + token.encode()).digest())
        self.fernet = Fernet(key)
        self.data = {"offset": 0, "chats": {}}
        if path.exists():
            try:
                self.data = json.loads(self.fernet.decrypt(path.read_bytes()))
            except InvalidToken:
                # A new bot token can't read the old list; start over rather than fail every check.
                print("Telegram: subscribers file can't be decrypted with this token; starting a new list.")
        self.loaded = json.dumps(self.data, sort_keys=True) if path.exists() else None

    @classmethod
    def from_env(cls, path: Path) -> Bot | None:
        token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        return cls(token, path, os.environ.get("PAGE_URL", "")) if token else None

    def save(self) -> bool:
        """Write the list only if it changed: encryption gives new bytes every time, which would be a commit per run."""
        plain = json.dumps(self.data, sort_keys=True)
        if plain == self.loaded:
            return False
        self.path.write_bytes(self.fernet.encrypt(plain.encode()) + b"\n")
        self.loaded = plain
        return True

    def ensure_profile(self) -> None:
        """Set the bot's description and command menu once (and again when PROFILE_VERSION changes)."""
        if self.data.get("profile", 0) >= PROFILE_VERSION:
            return
        self.call("setMyDescription", description=DESCRIPTION)
        self.call("setMyShortDescription", short_description=SHORT_DESCRIPTION)
        self.call("setMyCommands", commands=COMMANDS)
        self.data["profile"] = PROFILE_VERSION

    def call(self, method: str, **params):
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params, timeout=30)
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(r.status_code, r.text[:200])
        if not body.get("ok"):
            raise TelegramError(body.get("error_code"), body.get("description", ""))
        return body["result"]

    # ------------------------------------------------------------------ subscriptions

    def watching(self, chat: str) -> list[str]:
        return self.data["chats"].get(chat, [])

    def subscribe(self, chat: str, key: str) -> None:
        keys = self.data["chats"].setdefault(chat, [])
        if key not in keys:
            keys.append(key)

    def unsubscribe(self, chat: str, key: str | None = None) -> None:
        keys = [k for k in self.watching(chat) if key is not None and k != key]
        if keys:
            self.data["chats"][chat] = keys
        else:
            self.data["chats"].pop(chat, None)

    # ------------------------------------------------------------------ messages

    def send(self, chat: str, text: str, buttons: list[list[dict]] | None = None) -> None:
        params = {"chat_id": int(chat), "text": text, "disable_web_page_preview": True}
        buttons = [row for row in buttons or [] if row]  # Telegram rejects empty rows
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self.call("sendMessage", **params)
        except TelegramError as e:
            if e.code == 403:  # they blocked the bot or deleted the chat: stop messaging them
                self.unsubscribe(chat)
            else:
                print(f"Telegram: couldn't message a subscriber ({e})")

    def page_button(self) -> list[dict]:
        return [{"text": "Commander Watch", "url": self.page_url}] if self.page_url else []

    def deck_buttons(self, deck: dict) -> list[list[dict]]:
        """Link buttons under a message: the cheapest shop, CardTrader, and the page."""
        row = []
        if deck.get("shopUrl"):
            row.append({"text": f"Open at {deck['shopName']}", "url": deck["shopUrl"]})
        if deck.get("ctUrl"):
            row.append({"text": "CardTrader", "url": deck["ctUrl"]})
        return [r for r in (row, self.page_button()) if r]

    def help_text(self) -> str:
        return ("I send alerts for single Commander decks: when one gets cheaper (in a shop or on CardTrader), "
                "or a sold-out deck can be ordered again.\n\n"
                "To watch a deck, open Commander Watch and tap the bell next to it, then Start.\n\n"
                "/list shows the decks you watch, with buttons to stop each one. /stop stops all alerts.")

    def list_message(self, chat: str, decks: dict[str, dict]) -> tuple[str, list[list[dict]]]:
        keys = [k for k in self.watching(chat) if k in decks]
        if not keys:
            return "You're not watching any decks. " + self.help_text().split("\n\n")[1], []
        lines = [f"• {decks[k]['name']}: {status_line(decks[k])}" for k in keys]
        buttons = [[{"text": f"Stop: {decks[k]['name'][:40]}", "callback_data": f"stop:{k}"}] for k in keys]
        return "You're watching:\n" + "\n".join(lines), buttons

    # ------------------------------------------------------------------ commands

    def handle_updates(self, decks: dict[str, dict]) -> None:
        """Read messages sent to the bot since the last check: /start <deck>, /list, /stop, and Stop buttons."""
        updates = self.call("getUpdates", offset=self.data["offset"], timeout=0,
                            allowed_updates=["message", "callback_query"])
        for u in updates:
            self.data["offset"] = u["update_id"] + 1
            if "callback_query" in u:
                q = u["callback_query"]
                chat = str(q["message"]["chat"]["id"])
                data = q.get("data") or ""
                if data.startswith("stop:"):
                    key = data[5:]
                    self.unsubscribe(chat, key)
                    name = decks.get(key, {}).get("name", "that deck")
                    try:
                        self.call("answerCallbackQuery", callback_query_id=q["id"], text=f"Stopped alerts for {name}")
                        text, buttons = self.list_message(chat, decks)
                        self.call("editMessageText", chat_id=int(chat), message_id=q["message"]["message_id"], text=text,
                                  reply_markup={"inline_keyboard": buttons}, disable_web_page_preview=True)
                    except TelegramError as e:
                        print(f"Telegram: couldn't update a list message ({e})")
                continue
            msg = u.get("message") or {}
            if (msg.get("chat") or {}).get("type") != "private" or not msg.get("text"):
                continue  # only one-to-one chats
            chat, words = str(msg["chat"]["id"]), msg["text"].split()
            command = words[0].split("@")[0].lower()
            if command == "/start" and len(words) > 1:
                key = words[1]
                if key in decks:
                    self.subscribe(chat, key)
                    self.send(chat, f"Watching {decks[key]['name']}.\nNow: {status_line(decks[key])}.\n\n"
                                    "I'll message you when it gets cheaper, or when it can be ordered again after "
                                    "selling out. /list shows what you watch.", self.deck_buttons(decks[key]))
                else:
                    self.send(chat, "I don't know that deck any more (shops may have renamed it). "
                                    "Open Commander Watch and tap its bell again.", [self.page_button()])
            elif command == "/list":
                text, buttons = self.list_message(chat, decks)
                self.send(chat, text, buttons)
            elif command == "/stop":
                self.unsubscribe(chat)
                self.send(chat, "Stopped all alerts. Tap a deck's bell on the page to watch it again.", [self.page_button()])
            else:
                self.send(chat, self.help_text(), [self.page_button()])

    # ------------------------------------------------------------------ alerts

    def send_alerts(self, alerts: dict[str, dict]) -> int:
        """alerts: deck key -> {"text", "url", "label"} from deck_alerts; the offer's link goes in a button."""
        sent = 0
        for chat, keys in list(self.data["chats"].items()):
            for key in keys:
                if key in alerts:
                    a = alerts[key]
                    buttons = [[{"text": a["label"], "url": a["url"]}]] + ([self.page_button()] if self.page_url else [])
                    self.send(chat, a["text"], buttons)
                    sent += 1
        return sent


def offer_text(price: float, shop: str, approx: bool) -> str:
    return f"≈ {zl(price)} on CardTrader" if approx else f"{zl(price)} at {shop}"


def status_line(deck: dict) -> str:
    """The cheapest shop and CardTrader side by side: "209,99 zł at Magic Cafe · CardTrader ≈ 190,68 zł"."""
    parts = []
    if deck.get("shopPrice") is not None:
        parts.append(f"{zl(deck['shopPrice'])} at {deck['shopName']}")
    if deck.get("ctPrice") is not None:
        parts.append(f"CardTrader ≈ {zl(deck['ctPrice'])}")
    return " · ".join(parts) or "not in stock anywhere"


def deck_alerts(before: dict[str, dict], after: dict[str, dict]) -> dict[str, str]:
    """Alert texts for decks that got cheaper, or that can be ordered again.

    before/after map deck key -> {"name", "price" (the cheapest way to order it, CardTrader included; None if
    there's none), "shop", "url", "approx" (True when that's CardTrader), plus "shopPrice"/"shopName"/"ctPrice"}.
    A drop has to be 2% (and 1 zł) or more: CardTrader's prices, converted from euros, drift a little every day.
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
