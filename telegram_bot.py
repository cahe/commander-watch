"""Telegram alerts for single decks.

Someone who cares about one deck taps its "Get alerts" link on the page (t.me/<bot>?start=<deck key>),
which opens the bot in Telegram. Each check (check.py) then reads the bot's new messages, records who
watches which deck, and messages them when that deck's cheapest shop price drops or it can be ordered
again. Only the bot can message its users, so nobody can fake an alert.

There's no server: Telegram keeps new messages until the next check fetches them (so a "subscribed"
reply can take up to an hour). Subscribers' chat ids are personal data and the repo is public, so the
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

    @classmethod
    def from_env(cls, path: Path) -> Bot | None:
        token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        return cls(token, path, os.environ.get("PAGE_URL", "")) if token else None

    def save(self) -> None:
        self.path.write_bytes(self.fernet.encrypt(json.dumps(self.data, sort_keys=True).encode()) + b"\n")

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
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self.call("sendMessage", **params)
        except TelegramError as e:
            if e.code == 403:  # they blocked the bot or deleted the chat: stop messaging them
                self.unsubscribe(chat)
            else:
                print(f"Telegram: couldn't message a subscriber ({e})")

    def help_text(self) -> str:
        page = f"\n{self.page_url}" if self.page_url else ""
        return ("I send alerts for single Commander decks: when the cheapest shop price drops, "
                "or a sold-out deck can be ordered again.\n\n"
                f"To watch a deck, open Commander Watch and tap “Get alerts” next to it.{page}\n\n"
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
                                    "I'll message you when its cheapest shop price drops, or when it can be ordered "
                                    "again after selling out. /list shows what you watch.")
                else:
                    self.send(chat, "I don't know that deck any more (shops may have renamed it). "
                                    "Open Commander Watch and tap its “Get alerts” link again."
                                    + (f"\n{self.page_url}" if self.page_url else ""))
            elif command == "/list":
                text, buttons = self.list_message(chat, decks)
                self.send(chat, text, buttons)
            elif command == "/stop":
                self.unsubscribe(chat)
                self.send(chat, "Stopped all alerts. Tap “Get alerts” on the page to watch a deck again.")
            else:
                self.send(chat, self.help_text())

    # ------------------------------------------------------------------ alerts

    def send_alerts(self, alerts: dict[str, str]) -> int:
        sent = 0
        for chat, keys in list(self.data["chats"].items()):
            for key in keys:
                if key in alerts:
                    self.send(chat, alerts[key])
                    sent += 1
        return sent


def status_line(deck: dict) -> str:
    if deck.get("price") is None:
        return "not in stock anywhere"
    return f"{zl(deck['price'])} at {deck['shop']}"


def deck_alerts(before: dict[str, dict], after: dict[str, dict]) -> dict[str, str]:
    """Alert texts for decks whose cheapest shop price dropped, or that can be ordered again.

    before/after map deck key -> {"name", "price" (None if no shop can take an order), "shop", "url"}.
    """
    alerts = {}
    for key, now in after.items():
        was = before.get(key)
        if was is None or now["price"] is None:
            continue  # a deck seen for the first time, or not orderable now
        if was["price"] is None:
            alerts[key] = f"📦 {now['name']} can be ordered again: {zl(now['price'])} at {now['shop']}\n{now['url']}"
        elif now["price"] <= was["price"] - PRICE_STEP:
            alerts[key] = (f"📉 {now['name']}: {zl(now['price'])} at {now['shop']} (was {zl(was['price'])})\n"
                           f"{now['url']}")
    return alerts
