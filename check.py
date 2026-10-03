"""Commander Watch: hourly check of MTG Commander precon listings in Polish shops.

Reads each shop's listing pages, compares them with data/decks.json and
sends a push notification (ntfy.sh) when a shop lists a deck for the first time.
Run by .github/workflows/check.yml; also runnable locally: `python check.py`.
"""

from __future__ import annotations

import difflib
import html as htmllib
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote

import requests
from bs4 import BeautifulSoup

import telegram_bot

ROOT = Path(__file__).parent
DECKS_FILE = ROOT / "data" / "decks.json"
STATE_FILE = ROOT / "data" / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}
MAX_PAGES = 12
FAIL_ALERT_AFTER = 6  # consecutive failed runs (~6 h at hourly checks) before a "shop is failing" alert

# Words that mark accessories rather than sealed decks, and non-English editions (Magic Cafe sells Russian ones).
NOT_A_DECK = re.compile(
    r"sleeve|koszulk|deck ?box|pudełk|playmat|mata|dragon shield|ultra pro|booster|binder|album|bundle|"
    r"rosyjsk|niemieck|francusk|hiszpańsk|włosk|japońsk|chińsk|koreańsk",
    re.I,
)
# Name filter for mixed preorder categories, where other games sell "Commander" products too.
COMMANDER_DECK = re.compile(r"commander deck", re.I)
# Other sealed MTG products. Dragoneye names some Commander decks without the word
# ("Secrets of Strixhaven - Lorehold Spirit"), so there anything that isn't one of these counts.
OTHER_MTG = re.compile(
    r"bundle|beginner box|starter kit|scene box|theme ?deck|display|jumpstart|prerelease|gift|draft night|secret lair|team-up",
    re.I)


# --------------------------------------------------------------------------- HTTP

def fetch(url: str, session: requests.Session) -> str:
    """GET with retries; raises on final failure."""
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                r.encoding = r.encoding or "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code not in (403, 429):  # 403: bot protection, often transient
                break
        except requests.RequestException as e:  # timeouts, resets
            last = type(e).__name__
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{last} for {url}")  # URL last: log viewers would link a trailing ":" too


# ------------------------------------------------------------------------ helpers

def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    t = text.replace("\xa0", " ").replace("zł", "").replace(" ", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def status_from(orderable: bool, *texts: str) -> str:
    """A preorder only counts while it can be ordered; Cardstore keeps "[PRZEDSPRZEDAŻ]" on sold-out listings."""
    if not orderable:
        return "out_of_stock"
    blob = " ".join(t or "" for t in texts).lower()
    if "przedsprzeda" in blob or "pre-order" in blob or "preorder" in blob:
        return "preorder"
    return "in_stock"


def norm_url(url: str) -> str:
    p = urlsplit(url)
    return unquote(p.path).rstrip("/").lower()


def page_numbers(soup: BeautifulSoup, pattern: re.Pattern) -> int:
    nums = [int(m.group(1)) for a in soup.find_all("a", href=True) if (m := pattern.search(a["href"]))]
    return max(nums, default=1)


# ------------------------------------------------------------------------ parsers

def parse_shoper(html: str, base: str, *, category: str | None = None, name_filter: bool = False):
    """Shoper stores (Time4Magic, Mr. Puggy): <product-tile> elements carry the data as attributes."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tile in soup.find_all("product-tile"):
        if not (tile.get("list") or "").startswith("list_context"):
            continue  # sidebars such as "products of the day"
        if category and tile.get("category") != category:
            continue
        name = " ".join((tile.get("name") or "").split())
        if name_filter and ("commander" not in name.lower() or NOT_A_DECK.search(name)):
            continue
        link = tile.find("a", href=True)
        if not link or not tile.get("product-id"):
            continue
        buyable = tile.find("buy-button", attrs={"is-buyable": "1"}) is not None
        pv = tile.select_one(".price__value")
        price = parse_price(tile.get("price")) or parse_price(pv.get_text() if pv else None)
        out.append({
            "pid": int(tile["product-id"]),
            "name": name,
            "price": price,
            "url": urljoin(base, link["href"]),
            "status": status_from(buyable, name),
        })
    return out, soup


def parse_shoper_classic(html: str, base: str):
    """Older Shoper template (Pan Mysza): no <product-tile>; a basket button means it can be bought."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for prod in soup.select("div.product[data-product-id]"):
        a = prod.select_one("a.prodname")
        if not a:
            continue
        name = " ".join(a.get_text().split())
        if "commander" not in name.lower() or NOT_A_DECK.search(name):
            continue
        price = prod.select_one(".price em")
        out.append({
            "pid": int(prod["data-product-id"]),
            "name": name,
            "price": parse_price(price.get_text() if price else None),
            "url": urljoin(base, a["href"]),
            "status": status_from(prod.select_one("button.addtobasket") is not None, name),
        })
    return out, soup


def parse_presta(html: str, base: str, *, match: re.Pattern | None = None):
    """PrestaShop 1.6 (Cardstore, XJoy). XJoy marks preorders only in the availability label."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("ul.product_list li.ajax_block_product"):
        a = li.select_one("a.product-name") or li.select_one("a.product_img_link")
        if not a:
            continue
        name = " ".join((a.get("title") or a.get_text()).split())
        if NOT_A_DECK.search(name) or (match and not match.search(name)):
            continue
        m = re.search(r"/(\d+)-", a["href"])
        desc = li.select_one(".product-desc")
        avail = li.select_one(".availability")
        schema = li.select_one("link[itemprop=availability]")
        in_stock = (li.select_one(".availability .available-now") is not None
                    or bool(schema and schema.get("href", "").endswith("/InStock")))
        out.append({
            "pid": int(m.group(1)) if m else None,
            "name": name,
            "price": parse_price(li.select_one(".price").get_text() if li.select_one(".price") else None),
            "url": urljoin(base, a["href"]),
            "status": status_from(in_stock, name, desc.get_text() if desc else "",
                                  avail.get_text() if avail else ""),
        })
    return out, soup


def parse_presta17(html: str, base: str, *, match: re.Pattern | None = None):
    """PrestaShop 1.7 (Wargamer): a disabled cart button means out of stock; flags carry "Przedsprzedaż"."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for art in soup.select("article.product-miniature"):
        a = art.select_one(".product-title a")
        if not a or not art.get("data-id-product"):
            continue
        name = " ".join(a.get_text().split())
        if NOT_A_DECK.search(name) or (match and not match.search(name)):
            continue
        button = art.select_one("button.add-to-cart")
        flags = " ".join(f.get_text() for f in art.select(".product-flag"))
        price = art.select_one(".price")
        out.append({
            "pid": int(art["data-id-product"]),
            "name": name,
            "price": parse_price(price.get_text() if price else None),
            "url": urljoin(base, a["href"]),
            "status": status_from(button is not None and not button.has_attr("disabled"), name, flags),
        })
    return out, soup


def parse_sstore(html: str, base: str, *, preorder_category: bool = False):
    """sStore (Dragoneye). Only a Magic filter here; the preorder category lists every game."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for box in soup.select("div.boxProdSmall"):
        a = box.select_one("p.nazwa a")
        m = a and re.search(r"-p-(\d+)\.html", a["href"])
        if not m:
            continue
        name = " ".join(a.get_text().split())
        if NOT_A_DECK.search(name) or ("commander" not in name.lower() and OTHER_MTG.search(name)):
            continue
        if preorder_category and not re.search(r"magic the gathering|\bmtg\b", name, re.I):
            continue
        price = box.select_one(".productSpecialPrice") or box.select_one(".cenaBrutto")
        in_stock = box.select_one("p.produktDostepny") is not None
        status = status_from(in_stock, name)
        if preorder_category and in_stock:
            status = "preorder"  # listed as "produkt na zamówienie", with no preorder badge
        out.append({
            "pid": int(m.group(1)),
            "name": name,
            "price": parse_price(price.get_text() if price else None),
            "url": urljoin(base, a["href"]),
            "status": status,
        })
    return out, soup


def parse_woo(text: str, base: str):
    """WooCommerce Store API (Magic Cafe): JSON product list. Preorders carry the "Przedsprzedaż" tag."""
    out = []
    for p in json.loads(text):
        name = " ".join(htmllib.unescape(p["name"]).split())
        if "commander" not in name.lower() or NOT_A_DECK.search(name):
            continue
        prices = p.get("prices") or {}
        price = int(prices["price"]) / 10 ** prices.get("currency_minor_unit", 2) if prices.get("price") else None
        tags = " ".join(t["name"] for t in p.get("tags", []))
        out.append({
            "pid": p["id"],
            "name": name,
            "price": price,
            "url": urljoin(base, p["permalink"]),
            "status": status_from(p["is_in_stock"] and p["is_purchasable"], name, tags),
        })
    return out


def parse_sote(html: str, base: str):
    """SOTE (Wilczek) search results. Each tile has an availability label ("Brak" = none,
    "Przedsprzedaż" = preorder); the page's schema.org data says InStock for all of them, so it's not used.
    The search finds decks named without "Commander" too ("Modern Horizons 3 - Creative Energy"),
    so any Magic product that isn't another kind of sealed product counts."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tile in soup.select("div.product.thumbnail"):
        a = tile.select_one("a.product_name")
        hint = tile.select_one("span.hint")
        basket = tile.select_one("a.basket_add_link")
        m = basket and re.search(r"/basket/add/(\d+)", basket.get("href", ""))
        if not a or not m:
            continue
        name = " ".join(((hint.get("title") if hint else None) or a.get_text()).split())
        if (not re.search(r"magic", name, re.I) or NOT_A_DECK.search(name) or WILCZEK_NOT_A_DECK.search(name)
                or ("commander" not in name.lower() and OTHER_MTG.search(name))):
            continue
        label = tile.select_one(".product-availability-label")
        label = label.get_text(strip=True) if label else ""
        price = tile.select_one(".price")
        out.append({
            "pid": int(m.group(1)),
            "name": name,
            "price": parse_price(price.get_text() if price else None),
            "url": urljoin(base, a["href"]),
            "status": status_from(label.lower() != "brak", name, label),
        })
    return out, soup


# Wilczek's "commander" search also finds prerelease packs and pin sets.
WILCZEK_NOT_A_DECK = re.compile(r"prerelease|\bpins?\b", re.I)


def parse_frostmagic(text: str, base: str, *, presale: bool = False):
    """Frost Magic's own JSON API. Its product list holds only what's in stock; presale campaigns hold preorders."""
    out = []
    for p in json.loads(text)["products"]:
        name = " ".join((p.get("name") or "").split())
        cats = {c["category"]["slug"] for c in p.get("categories", [])}
        if "commander-decks" not in cats or NOT_A_DECK.search(name):
            continue
        orderable = bool(p.get("canPurchase")) and (p.get("stock") or 0) > 0
        out.append({
            "pid": None,  # ids are UUIDs, so they can't tell new listings from missed ones
            "name": name,
            "price": p.get("effectivePrice", p.get("price")),
            "url": f"{base}/pl/products/{p['slug']}",
            "status": ("preorder" if presale else "in_stock") if orderable else "out_of_stock",
        })
    return out


# -------------------------------------------------------------------------- shops

def scan_time4magic(s):
    base = "https://time4magic.pl"
    url = lambda n: f"{base}/commander-decki-magic-the-gathering/{n}"
    return scan_paged(s, url, lambda h: parse_shoper(h, base, category="Commander Decki"),
                      re.compile(r"/commander-decki-magic-the-gathering/(\d+)$"))


def scan_mrpuggy(s):
    base = "https://mrpuggy.pl"
    url = lambda n: f"{base}/pl/c/Magic-The-Gathering/23/{n}"
    return scan_paged(s, url, lambda h: parse_shoper(h, base, name_filter=True),
                      re.compile(r"/pl/c/Magic-The-Gathering/23/(\d+)$"))


def scan_panmysza(s):
    base = "https://panmysza.pl"
    url = lambda n: f"{base}/pl/c/Magic-The-Gathering/53" + (f"/{n}" if n > 1 else "")
    return scan_paged(s, url, lambda h: parse_shoper_classic(h, base),
                      re.compile(r"/pl/c/Magic-The-Gathering/53/(\d+)$"))


def scan_cardstore(s):
    base = "https://cardstore.pl"
    url = lambda n: f"{base}/157-commander" + (f"?p={n}" if n > 1 else "")
    return scan_paged(s, url, lambda h: parse_presta(h, base), re.compile(r"157-commander\?(?:.*&)?p=(\d+)"))


def scan_wargamer(s):
    base = "https://sklep.wargamer.pl"
    mtg = scan_paged(s, lambda n: f"{base}/pl/43-magic-the-gathering" + (f"?page={n}" if n > 1 else ""),
                     lambda h: parse_presta17(h, base, match=re.compile("commander", re.I)),
                     re.compile(r"43-magic-the-gathering\?(?:.*&)?page=(\d+)"))
    # All games' preorders, newest first; a new deck would be near the top.
    pre = scan_paged(s, lambda n: f"{base}/pl/130-przedsprzedaz" + (f"?page={n}" if n > 1 else ""),
                     lambda h: parse_presta17(h, base, match=COMMANDER_DECK),
                     re.compile(r"130-przedsprzedaz\?(?:.*&)?page=(\d+)"), max_pages=3, may_be_empty=True)
    return merge_scans(mtg, pre)


def scan_xjoy(s):
    base = "https://www.xjoy.pl"
    decks = scan_paged(s, lambda n: f"{base}/371-mtg-commander" + (f"?p={n}" if n > 1 else ""),
                       lambda h: parse_presta(h, base), re.compile(r"371-mtg-commander\?(?:.*&)?p=(\d+)"))
    pre = scan_paged(s, lambda n: f"{base}/66-przedsprzedaz" + (f"?p={n}" if n > 1 else ""),
                     lambda h: parse_presta(h, base, match=COMMANDER_DECK),
                     re.compile(r"66-przedsprzedaz\?(?:.*&)?p=(\d+)"), may_be_empty=True)
    return merge_scans(decks, pre)


def scan_dragoneye(s):
    base = "https://dragoneye.pl"
    mtg = scan_paged(s, lambda n: f"{base}/magic-the-gathering-c-16_211.html" + (f"?page={n}" if n > 1 else ""),
                     lambda h: parse_sstore(h, base), re.compile(r"c-16_211\.html\?(?:.*&)?page=(\d+)"))
    # All games' preorders; sort=4d lists the newest additions first.
    pre = scan_paged(s, lambda n: f"{base}/przedsprzedaz-c-75.html?sort=4d&page={n}",
                     lambda h: parse_sstore(h, base, preorder_category=True),
                     re.compile(r"c-75\.html\?(?:.*&)?page=(\d+)"), max_pages=3, may_be_empty=True)
    return merge_scans(mtg, pre)


def scan_magiccafe(s):
    base = "https://magiccafe.eu"
    products, errors, n = {}, [], 1
    while n <= MAX_PAGES:  # category 245 is "Gotowe Talie" (ready-made decks), 100 products per page
        try:
            text = fetch(f"{base}/wp-json/wc/store/v1/products?category=245&per_page=100&page={n}", s)
            for it in parse_woo(text, base):
                products.setdefault(norm_url(it["url"]), it)
            if len(json.loads(text)) < 100:
                break
        except Exception as e:
            errors.append(str(e))
            break
        n += 1
    return list(products.values()), errors


def scan_wilczek(s):
    base = "https://wilczek.poznan.pl"
    # No Commander category; the site search finds the decks, newest first. Its Cloudflare blocks bursts
    # (a quick run of requests got HTTP 429 for half an hour), so pages are fetched a few seconds apart.
    limit = RateLimit(0.25)

    def url(n):
        limit.wait()
        return f"{base}/product/search/{n}/long/created_at/desc/0?query=commander"

    return scan_paged(s, url,
                      lambda h: parse_sote(h, base), re.compile(r"/product/search/(\d+)/long/"))


def scan_frostmagic(s):
    base = "https://frostmagic.pl"
    presales, regular, errors = [], [], []
    try:
        for c in json.loads(fetch(f"{base}/api/presales", s))["presales"]:
            if re.search(r"magic|mtg", c["name"], re.I):
                presales += parse_frostmagic(fetch(f"{base}/api/presales/{c['slug']}/products?pageSize=100", s),
                                             base, presale=True)
    except Exception as e:
        errors.append(str(e))
    try:
        n = 1
        while n <= MAX_PAGES:
            text = fetch(f"{base}/api/products?category=commander-decks&pageSize=100&page={n}", s)
            regular += parse_frostmagic(text, base)
            if not json.loads(text)["pagination"]["hasMore"]:
                break
            n += 1
    except Exception as e:
        errors.append(str(e))
    # A preorder is also in the regular list while it has stock; its presale entry says what it is.
    return merge_scans((presales, []), (regular, errors))


def merge_scans(*scans):
    """Combine (products, errors) from several categories; a deck in both appears once."""
    products, errors = {}, []
    for items, errs in scans:
        for it in items:
            products.setdefault(norm_url(it["url"]), it)
        errors += errs
    return list(products.values()), errors


def scan_paged(s, url_for, parse, page_re, max_pages=MAX_PAGES, may_be_empty=False):
    """Fetch page 1, read how many pages exist, fetch the rest. Returns (products, errors).

    A category that should always hold decks but yields none is reported as an error, with the
    page's title and size, so a block page (HTTP 200 with no products) counts as a failed check.
    Mixed preorder categories pass may_be_empty=True: no Commander deck there is normal.
    """
    products, errors = {}, []
    last_page, n, first = 1, 1, None
    while n <= min(last_page, max_pages):
        try:
            html = fetch(url_for(n), s)
            first = first or html
            items, soup = parse(html)
            last_page = max(last_page, page_numbers(soup, page_re))
            for it in items:
                products.setdefault(norm_url(it["url"]), it)
        except Exception as e:  # keep going; a failed page is not a removal
            errors.append(str(e))
            if n == 1:
                break
        n += 1
    if not products and not errors and not may_be_empty:
        title = re.search(r"<title[^>]*>([^<]*)", first or "", re.I)
        errors.append(f"no products found on {url_for(1)} "
                      f"(page title {' '.join(title.group(1).split())[:60] if title else None!r}, {len(first or '')} bytes)")
    return list(products.values()), errors


def scan_cardtrader(s):
    """CardTrader marketplace: cheapest CardTrader Zero offer per Commander precon, converted to PLN.

    Prices only: listings from QUIET_SHOPS never trigger pushes. Without CARDTRADER_TOKEN it's skipped.
    """
    if not os.environ.get("CARDTRADER_TOKEN", "").strip():
        return None  # skipped, not failed
    try:
        rates = pln_rates(s)
        blueprints = sold_by_shops(cardtrader_blueprints())
    except Exception as e:
        return [], [str(e)]
    out, errors = [], []
    limit = RateLimit(8)  # the marketplace endpoint allows 10 requests per second

    def one(bp):
        limit.wait()
        offers = cardtrader_get("/marketplace/products", blueprint_id=bp["id"]).get(str(bp["id"]), [])
        best = cheapest_zero_offer(offers, rates)
        return {"pid": bp["id"], "name": bp["name"], "price": best,
                "url": f"https://www.cardtrader.com/en/cards/{bp['id']}",
                "status": "in_stock" if best is not None else "out_of_stock"}

    with ThreadPoolExecutor(max_workers=6) as pool:
        for fut in [pool.submit(one, bp) for bp in blueprints]:
            try:
                out.append(fut.result())
            except Exception as e:
                errors.append(str(e))
    return out, errors


class RateLimit:
    """Spaces calls from several threads at least 1/per_second apart."""

    def __init__(self, per_second: float):
        self.gap, self.next, self.lock = 1 / per_second, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.gap
        time.sleep(max(0.0, at - now))


def cheapest_zero_offer(offers: list[dict], rates: dict[str, float]) -> float | None:
    """Lowest PLN price among sealed, English, single-copy offers shipped via CardTrader Zero."""
    best = None
    for o in offers:
        user = o.get("user") or {}
        lang = (o.get("properties_hash") or {}).get("mtg_language")
        price = o.get("price") or {}
        rate = rates.get(price.get("currency"))
        if (not user.get("can_sell_via_hub") or o.get("on_vacation") or o.get("graded")
                or (o.get("bundle_size") or 1) > 1 or not o.get("quantity")
                or (lang and lang != "en") or price.get("cents") is None or rate is None):
            continue
        pln = round(price["cents"] / 100 * rate, 2)
        best = pln if best is None else min(best, pln)
    return best


def pln_rates(s) -> dict[str, float]:
    """PLN per unit of each currency: National Bank of Poland mid rates (table A)."""
    r = s.get("https://api.nbp.pl/api/exchangerates/tables/A/?format=json", timeout=30)
    r.raise_for_status()
    return {"PLN": 1.0, **{x["code"]: x["mid"] for x in r.json()[0]["rates"]}}


CARDTRADER_CACHE = ROOT / "data" / "cardtrader.json"
# Commander precons sit in "Commander: …" expansions, but Universes Beyond decks live in the set
# itself ("Universes Beyond: Doctor Who"), so both kinds of expansion are read.
CARDTRADER_EXPANSION = re.compile(r"commander|^universes beyond", re.I)
CARDTRADER_PRECON_CATEGORY = 17  # "Magic Preconstructed Decks"


def cardtrader_blueprints() -> list[dict]:
    """Commander precons on CardTrader, cached in data/cardtrader.json and refreshed daily.

    If the refresh fails, the previous list is used rather than none.
    """
    cache = load(CARDTRADER_CACHE, {})
    today = date.today().isoformat()
    if cache.get("refreshed") == today and cache.get("blueprints"):
        return cache["blueprints"]
    try:
        exps = [e for e in cardtrader_get("/expansions")
                if e.get("game_id") == 1 and CARDTRADER_EXPANSION.search(str(e.get("name", "")))
                and "promo" not in str(e.get("name", "")).lower()]
        limit = RateLimit(10)  # well inside the 200 requests per 10 seconds global limit

        def precons(e):
            limit.wait()
            # The version tells otherwise identically named entries apart (e.g. a Collector's Edition).
            return [{"id": b["id"], "name": " ".join(f"{b['name']} {b.get('version') or ''}".split()),
                     "version": b.get("version"), "expansion": e.get("code")}
                    for b in cardtrader_get("/blueprints/export", expansion_id=e["id"])
                    if b.get("category_id") == CARDTRADER_PRECON_CATEGORY]

        with ThreadPoolExecutor(max_workers=6) as pool:
            blueprints = [b for found in pool.map(precons, exps) for b in found]
    except Exception:
        if cache.get("blueprints"):
            return cache["blueprints"]
        raise
    dupes = [n for n, c in Counter(b["name"] for b in blueprints).items() if c > 1]
    print(f"    CardTrader: {len(blueprints)} precons in {len(exps)} expansions"
          + (f"; same name twice: {dupes}" if dupes else ""))
    save(CARDTRADER_CACHE, {"refreshed": today, "blueprints": blueprints})
    return blueprints


def sold_by_shops(blueprints: list[dict]) -> list[dict]:
    """Only the CardTrader decks that match a shop listing: the page hides the rest anyway,
    so this keeps the hourly offer requests to the decks the shops sell."""
    shops = [{"id": d["id"], "shop": d["shop"], "name": d["name"]}
             for d in load(DECKS_FILE, []) if d["shop"] not in QUIET_SHOPS]
    if not shops:
        return blueprints
    probes = [{"id": f"ct-{b['id']}", "shop": "cardtrader", "name": b["name"]} for b in blueprints]
    assign_groups(shops + probes)
    shop_groups = {d["group"] for d in shops}
    keep = {p["id"] for p in probes if p["group"] in shop_groups}
    return [b for b in blueprints if f"ct-{b['id']}" in keep]


SHOPS = {
    "time4magic": ("Time4Magic", scan_time4magic),
    "cardstore": ("Cardstore", scan_cardstore),
    "mrpuggy": ("Mr. Puggy", scan_mrpuggy),
    "wargamer": ("Wargamer", scan_wargamer),
    "xjoy": ("XJoy", scan_xjoy),
    "dragoneye": ("Dragoneye", scan_dragoneye),
    "panmysza": ("Pan Mysza", scan_panmysza),
    "frostmagic": ("Frost Magic", scan_frostmagic),
    "magiccafe": ("Magic Cafe", scan_magiccafe),
    "wilczek": ("Wilczek", scan_wilczek),
    "cardtrader": ("CardTrader", scan_cardtrader),
}
QUIET_SHOPS = {"cardtrader"}  # prices only: never announce new decks or restocks
# Shops whose listings leave out sold-out decks instead of marking them: after a complete scan,
# a known deck that isn't listed any more has sold out.
VANISHING_SHOPS = {"frostmagic"}


# ---------------------------------------------------------------------- grouping
# Shops name the same deck differently ("Secrets of Strixhaven Lorehold Spirit Commander Deck" vs
# "Magic the Gathering: Secrets of Strixhaven - Lorehold Spirit"), so listings are matched on the
# words that are left once shop noise is stripped, weighted so rare words (the deck's own name)
# count far more than the set name every deck of that set shares.

NAME_NOISE = re.compile(
    r"magic\s*:?\s*the\s+gathering|universes beyond|\bmtg\b|commander('s)?|\bdecks?\b|"
    r"wersja angielska|edycja angielska|angielski|english|\beng?\b|\(.*?\)|\[.*?\]", re.I)
SET_LIKE = re.compile(r"display|\bset\b|zestaw|case|\(\s*[45]\s*\)|x\s*[45]\b|\b[45]\s*decks?\b", re.I)
NAME_STOP = {"the", "of", "a", "an", "and", "to", "z", "i", "w", "na", "do", "vs", "edition", "editions", "edycja", "full",
             "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv", "xvi"}
GROUP_THRESHOLD = 0.6


def deck_kind(name: str) -> str:
    """Regular decks, collector's editions and multi-deck sets/displays never share a group."""
    n = name.lower()
    return ("collector " if "collector" in n else "") + ("set" if SET_LIKE.search(n) else "deck")


def name_tokens(name: str) -> frozenset[str]:
    n = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    n = n.replace("&", " and ").replace("collector's", " ").replace("collector", " ")
    n = SET_LIKE.sub(" ", NAME_NOISE.sub(" ", n))
    return frozenset(t for t in re.findall(r"[a-z0-9]+", n) if len(t) > 1 and t not in NAME_STOP)


@lru_cache(maxsize=None)
def same_word(a: str, b: str) -> bool:
    # Tolerates shop typos ("Dace of the Elements") and plurals ("Turtles").
    return a == b or (len(a) > 3 and len(b) > 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)


def assign_groups(decks: list[dict]) -> None:
    """Give listings of the same deck the same "group" id and a shared "groupName"."""
    toks = [name_tokens(d["name"]) for d in decks]
    kinds = [deck_kind(d["name"]) for d in decks]
    df = Counter(t for ts in toks for t in ts)
    idf = {t: math.log(1 + len(decks) / c) for t, c in df.items()}

    def score(a: frozenset, b: frozenset) -> float:
        matched = 0.0
        for x in a:
            ws = [(idf[x] + idf[y]) / 2 for y in b if same_word(x, y)]
            if ws:
                matched += max(ws)
        total = sum(idf[t] for t in a) + sum(idf[t] for t in b) - matched
        return matched / total if total else 0.0

    def contained(a: frozenset, b: frozenset) -> bool:
        # Every word of the shorter name is in the longer one ("The Lord of the Rings | Food And
        # Fellowship" vs "... Tales of Middle-earth - Food and Fellowship"). Needs 3+ words, so a
        # vague "Reality Fracture - Commander Deck" doesn't join some deck of that set.
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        return len(small) >= 3 and all(any(same_word(x, y) for y in big) for x in small)

    pairs = []
    for i in range(len(decks)):
        for j in range(i + 1, len(decks)):
            if decks[i]["shop"] != decks[j]["shop"] and kinds[i] == kinds[j] and toks[i] and toks[j]:
                s = score(toks[i], toks[j])
                if s >= GROUP_THRESHOLD or contained(toks[i], toks[j]):
                    pairs.append((s, i, j))  # best matches are merged first
    parent = list(range(len(decks)))
    shops = [{d["shop"]} for d in decks]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, i, j in sorted(pairs, reverse=True):  # best matches first
        ri, rj = find(i), find(j)
        if ri != rj and not shops[ri] & shops[rj]:  # at most one listing per shop in a group
            parent[rj] = ri
            shops[ri] |= shops[rj]

    # Listings named only by their set ("Reality Fracture Commander Deck") join that set's deck,
    # but only when exactly one deck group of the same kind holds all their words; a vague
    # "Doctor Who Commander Deck" stays apart because Doctor Who has several decks.
    def words(root: int) -> frozenset:
        return frozenset().union(*(toks[i] for i in range(len(decks)) if find(i) == root))

    roots = {find(i) for i in range(len(decks))}
    rwords = {r: words(r) for r in roots}
    for r in sorted(roots, key=lambda r: len(rwords[r])):
        small = rwords[r]
        if find(r) != r or len(small) > 2 or not small:
            continue
        kind = kinds[r]
        hosts = [h for h in roots if h != r and find(h) == h and kinds[h] == kind and len(rwords[h]) > len(small)
                 and all(any(same_word(x, y) for y in rwords[h]) for x in small)]
        if len(hosts) == 1 and not shops[r] & shops[hosts[0]]:
            parent[r] = hosts[0]
            shops[hosts[0]] |= shops[r]

    members: dict[int, list[int]] = {}
    for i in range(len(decks)):
        members.setdefault(find(i), []).append(i)
    # Biggest groups first, so when a group splits the larger part keeps the deck key.
    taken: set[str] = set()
    for idx in sorted(members.values(), key=len, reverse=True):
        names = [clean_name(decks[i]["name"]) for i in idx]
        # The name whose words most shops agree on (so one shop's typo or missing deck name loses), then the shortest.
        agree = Counter(t for i in idx for t in toks[i])
        best = max(range(len(idx)), key=lambda k: (sum(agree[t] for t in toks[idx[k]]), -len(names[k])))
        gid = min(decks[i]["id"] for i in idx)
        key = deck_key(idx, decks, names[best], kinds[idx[0]], taken)
        for i in idx:
            decks[i]["group"] = gid
            decks[i]["groupName"] = names[best]
            decks[i]["deck"] = key


def deck_key(idx: list[int], decks: list[dict], name: str, kind: str, taken: set[str]) -> str:
    """A deck's permanent key, for Telegram subscriptions: group ids change as shops join a group, this doesn't.

    The group keeps the key most of its listings already carry; a new deck gets one made from its name
    and kind ("star-trek-we-are-the-borg", "...-collector", "...-set"), made unique. Telegram start links
    allow 64 characters of [A-Za-z0-9_-].
    """
    keys = Counter(decks[i]["deck"] for i in idx if decks[i].get("deck"))
    for key, _ in keys.most_common():
        if key not in taken:
            taken.add(key)
            return key
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()).strip("-")
    suffix = "" if kind == "deck" else "-" + kind.replace(" ", "-")
    base = (slug[:56 - len(suffix)].rstrip("-") or "deck") + suffix
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    taken.add(key)
    return key


def clean_name(name: str) -> str:
    """Shop name without brand, language tags and product-type words: "Marvel Super Heroes – Avengers Assemble"."""
    n = re.sub(r"\[.*?\]|\(.*?\)|\bwersja angielska\b|\bangielski\b|\bmagic\s*:?\s*the\s+gathering\b|\bmtg\b|"
               r"\buniverses beyond\b|\bcollector'?s?\b|\bedition\b|\bcommander('s)?\b|\bdecks?\b|"
               r"\bdisplay\b|\bfull set\b|\bset\b|\bzestaw\b|\bx\s*[45]\b|\ben\b|\beditions?\b", " ", name, flags=re.I)
    # One separator style; hyphens inside a word ("Middle-earth") stay.
    n = re.sub(r"(?:\s*:\s*|\s+[-–]+\s*|\s*[-–]+\s+)+", " – ", " ".join(n.split()))
    return n.strip(" –\"'")


# ---------------------------------------------------------------------- notifying

def notify(title: str, lines: list[str], tags: str = "black_joker"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set; skipping push.")
        return
    body = {"topic": topic, "title": title, "message": "\n".join(lines)[:3900], "tags": [tags]}
    if os.environ.get("PAGE_URL"):
        body["click"] = os.environ["PAGE_URL"]
    try:
        r = requests.post(os.environ.get("NTFY_SERVER", "https://ntfy.sh"), json=body, timeout=20)
        print("ntfy:", r.status_code)
    except requests.RequestException as e:
        print("ntfy failed:", e)


# --------------------------------------------------------------------------- main

def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- CardTrader

CARDTRADER_API = "https://api.cardtrader.com/api/v2"


def cardtrader_get(path: str, **params):
    token = os.environ.get("CARDTRADER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CARDTRADER_TOKEN is not set")
    r = requests.get(f"{CARDTRADER_API}{path}", params=params, timeout=60,
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {path}")  # never echo the request headers
    return r.json()


def cardtrader_discover() -> int:
    """One-off: show where Commander decks live in CardTrader's catalogue, to design the scanner."""
    try:
        games = cardtrader_get("/games")
        games = games.get("array", games) if isinstance(games, dict) else games
        magic = next(g for g in games if "magic" in str(g.get("name", "")).lower())
        print("Magic game:", {k: magic.get(k) for k in ("id", "name", "display_name")})
        cats = cardtrader_get("/categories", game_id=magic["id"])
        print("\nCategories:")
        for c in cats:
            print(f"  {c.get('id'):>5}  {c.get('name')}")
        exps = [e for e in cardtrader_get("/expansions") if e.get("game_id") == magic["id"]]
        cmd = [e for e in exps if re.search(r"commander|precon|deck", str(e.get("name")), re.I)]
        cmd.sort(key=lambda e: e.get("id", 0), reverse=True)
        print(f"\nMagic expansions: {len(exps)}; with commander/precon/deck in the name: {len(cmd)}. Newest 30:")
        for e in cmd[:30]:
            print(f"  {e.get('id'):>6}  {e.get('code')!s:8} {e.get('name')}")
        cat_name = {c.get("id"): c.get("name") for c in cats}
        sample = None
        for e in cmd[:4]:
            bps = cardtrader_get("/blueprints/export", expansion_id=e["id"])
            by_cat = Counter(cat_name.get(b.get("category_id"), b.get("category_id")) for b in bps)
            print(f"\nBlueprints in {e.get('name')} ({len(bps)}): {dict(by_cat)}")
            if bps:
                print("  fields:", sorted(bps[0].keys()))
            for b in bps:
                if not re.search(r"card|single|token", str(cat_name.get(b.get("category_id"))), re.I):
                    print(f"  {b.get('id'):>8}  [{cat_name.get(b.get('category_id'))}]  {b.get('name')}  {b.get('version') or ''}")
                    sample = sample or b
        if sample:
            prods = cardtrader_get("/marketplace/products", blueprint_id=sample["id"])
            offers = prods.get(str(sample["id"]), []) if isinstance(prods, dict) else prods
            print(f"\nOffers for {sample.get('name')}: {len(offers)}")
            if offers:
                print("  fields:", sorted(offers[0].keys()))
            for o in offers[:5]:
                user = o.get("user") or {}
                print("  ", {"price": o.get("price"), "qty": o.get("quantity"), "zero": user.get("can_sell_via_hub"),
                             "country": user.get("country_code"), "props": o.get("properties_hash")})
        return 0
    except Exception as e:
        print("CardTrader discovery failed:", e)
        return 1


ORDERABLE = {"in_stock", "preorder"}


def cardtrader_newly_cheapest(decks: list[dict], lost: list | None = None) -> list[tuple[dict, dict | None]]:
    """CardTrader listings that just became the cheapest way to order their deck.

    Only decks a shop also lists count. Each CardTrader listing keeps a "cheapest" flag, so a push
    goes out when it turns true, not every hour it stays true; the first time a listing is
    evaluated the flag is just recorded. Returns (CardTrader listing, cheapest shop offer or None).
    Listings that stopped being cheapest are appended to `lost`, for the changelog.
    """
    groups: dict[str, list[dict]] = {}
    for d in decks:
        groups.setdefault(d.get("group", d["id"]), []).append(d)
    news = []
    for members in groups.values():
        shops = [d for d in members if d["shop"] not in QUIET_SHOPS]
        for ct in (d for d in members if d["shop"] in QUIET_SHOPS):
            if not shops:
                ct.pop("cheapest", None)
                continue
            rivals = [d for d in shops if d["status"] in ORDERABLE and d.get("price") is not None]
            nxt = min(rivals, key=lambda d: d["price"], default=None)
            now_cheapest = (ct["status"] in ORDERABLE and ct.get("price") is not None
                            and (nxt is None or ct["price"] < nxt["price"]))
            if now_cheapest and ct.get("cheapest") is False:
                news.append((ct, nxt))
            elif not now_cheapest and ct.get("cheapest") is True and lost is not None:
                lost.append(ct)
            ct["cheapest"] = now_cheapest
    return news


# ---------------------------------------------------------------------- changelog
# data/changes.json is an append-only list of events for the page's "Changes" panel. Each event
# carries the deck, shop, prices and some context from that moment (where else it's in stock,
# whether it's now the cheapest). CardTrader only logs "cheapest" / "notcheapest": its raw prices
# move every hour and would drown the shops' changes.

CHANGES_DAYS = 30
PRICE_STEP = 1.0  # zł; smaller shop price moves aren't logged


def deck_prices(decks: list[dict]) -> dict[str, dict]:
    """Each deck's cheapest way to order it, by its permanent key, for Telegram: the cheapest shop and CardTrader
    separately (shopPrice/shopName/shopUrl, ctPrice/ctUrl), and the cheaper of the two (price/shop/url/approx)."""
    out: dict[str, dict] = {}
    for d in decks:
        if not d.get("deck"):
            continue
        cur = out.setdefault(d["deck"], {"name": d.get("groupName") or d["name"], "shopPrice": None, "shopName": None,
                                         "shopUrl": None, "ctPrice": None, "ctUrl": None})
        if d.get("status") not in ORDERABLE or d.get("price") is None:
            continue
        if d["shop"] in QUIET_SHOPS:
            if cur["ctPrice"] is None or d["price"] < cur["ctPrice"]:
                cur.update(ctPrice=d["price"], ctUrl=d["url"])
        elif cur["shopPrice"] is None or d["price"] < cur["shopPrice"]:
            cur.update(shopPrice=d["price"], shopName=SHOPS[d["shop"]][0] if d["shop"] in SHOPS else d["shop"], shopUrl=d["url"])
    for cur in out.values():
        ct_cheaper = cur["ctPrice"] is not None and (cur["shopPrice"] is None or cur["ctPrice"] < cur["shopPrice"])
        cur.update(price=cur["ctPrice"] if ct_cheaper else cur["shopPrice"],
                   shop="CardTrader" if ct_cheaper else cur["shopName"],
                   url=cur["ctUrl"] if ct_cheaper else cur["shopUrl"], approx=ct_cheaper)
    return out


def bot_file() -> Path:
    return DECKS_FILE.with_name("bot.json")


def run_telegram(before: dict[str, dict], after: dict[str, dict]) -> None:
    """Publish deck prices for the Telegram bot (the Cloudflare Worker reads data/bot.json), then message the
    watchers of decks that got cheaper or can be ordered again. Never fails the check."""
    save(bot_file(), {"decks": {k: {f: v[f] for f in ("name", "shopPrice", "shopName", "shopUrl", "ctPrice", "ctUrl")}
                                for k, v in after.items()}})
    alerts = telegram_bot.Alerts.from_env()
    if not alerts:
        return
    try:
        found = telegram_bot.deck_alerts(before, after)
        print(f"Telegram: {len(found)} decks to alert, {alerts.send(found)} messages sent")
    except Exception as e:  # the bot being down shouldn't stop the shop data being saved
        print(f"Telegram: {type(e).__name__}: {e}")


def history_file() -> Path:
    return DECKS_FILE.with_name("history.json")


def record_history(history: dict, decks: list[dict], day: str) -> None:
    """Price history per listing (listing ids are stable; group ids aren't), as the day's lowest orderable price.

    Each listing keeps [day, price] points only where its price changes by PRICE_STEP or more (CardTrader
    drifts by grosze daily with the exchange rate), with None while it can't be ordered; the page combines
    a deck's listings into its cheapest price over time.
    """
    def changed(a, b):
        return (a is None) != (b is None) or (a is not None and abs(a - b) >= PRICE_STEP)

    series = history.setdefault("listings", {})
    for d in decks:
        price = d.get("price") if d.get("status") in ORDERABLE else None
        price = round(price, 2) if price is not None else None
        points = series.setdefault(d["id"], [])
        if points and points[-1][0] == day:
            old = points[-1][1]
            points[-1][1] = price if old is None else old if price is None else min(old, price)
            # A same-day low that matches the day before is no change.
            if len(points) > 1 and not changed(points[-2][1], points[-1][1]):
                points.pop()
        elif not points or changed(points[-1][1], price):
            points.append([day, price])


def save_history(history: dict) -> None:
    """One listing per line: small, and each hourly commit's diff shows just the listings that changed."""
    rows = [f" {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, separators=(',', ':'))}"
            for k, v in sorted(history.get("listings", {}).items())]
    history_file().write_text('{"listings": {\n' + ",\n".join(rows) + "\n}}\n", encoding="utf-8")


def changes_file() -> Path:
    return DECKS_FILE.with_name("changes.json")


def status_event(old: str | None, new: str) -> str | None:
    # A preorder counts as in stock: shops label the same release differently, so a preorder that's
    # released (or one shop calling it in stock, another a preorder) isn't a change.
    if old == new or old is None or (old in ORDERABLE and new in ORDERABLE):
        return None
    if new == "in_stock":
        return "restock"
    if new == "preorder":
        return "preorder"
    if new == "out_of_stock" and old in ORDERABLE:
        return "soldout"
    return None


def finish_events(pending: list[tuple[str, dict, float | None]], decks: list[dict], stamp: str) -> list[dict]:
    """Turn (kind, listing, old price) into changelog events, once listings have their groups."""
    groups: dict[str, list[dict]] = {}
    for d in decks:
        groups.setdefault(d.get("group", d["id"]), []).append(d)
    events = []
    for kind, d, old in pending:
        members = groups.get(d.get("group", d["id"]), [d])
        others = [m for m in members if m is not d and m["shop"] not in QUIET_SHOPS
                  and m["status"] in ORDERABLE and m.get("price") is not None]
        best = min(others, key=lambda m: m["price"], default=None)
        orderable = d["status"] in ORDERABLE and d.get("price") is not None
        e = {"at": stamp, "kind": kind, "shop": d["shop"], "id": d["id"], "group": d.get("group", d["id"]),
             "deck": d.get("groupName") or d["name"], "url": d["url"], "price": d.get("price"),
             "status": d["status"], "inStock": len(others) + (orderable and d["shop"] not in QUIET_SHOPS)}
        if old is not None:
            e["old"] = old
        if best:
            e["best"] = {"shop": best["shop"], "price": best["price"]}
        if orderable:
            e["cheapest"] = best is None or d["price"] < best["price"]
        events.append(e)
    return events


def append_changes(events: list[dict], now: datetime) -> None:
    path = changes_file()
    log = load(path, [])
    cutoff = (now - timedelta(days=CHANGES_DAYS)).isoformat().replace("+00:00", "Z")
    kept = [e for e in log if e.get("at", "") >= cutoff]
    if events or len(kept) != len(log) or not path.exists():
        save(path, kept + events)


def describe_change(old_price: float | None, old_status: str | None, d: dict) -> str:
    """Short note for the page, e.g. "back in stock, price 239.00 → 219.00 zł"; "" if nothing changed."""
    parts = []
    if old_status != d["status"]:
        parts.append({"in_stock": "back in stock" if old_status == "out_of_stock" else "now in stock",
                      "preorder": "back in stock" if old_status == "out_of_stock" else "now in stock", "out_of_stock": "sold out"}.get(d["status"], d["status"]))
    if old_price is not None and d["price"] is not None and abs(old_price - d["price"]) >= 0.01:
        parts.append(f"price {old_price:.2f} → {d['price']:.2f} zł")
    return ", ".join(parts)


def deck_line(d: dict) -> str:
    shop = SHOPS[d["shop"]][0]
    if d["price"] is None:
        return f"{d['name']} — {shop}"
    status = "in stock" if d["status"] == "preorder" else d["status"].replace("_", " ")  # preorders count as in stock
    return f"{d['name']} — {shop} — {d['price']:.2f} zł ({status})"


def main() -> int:
    if "--test-push" in sys.argv:  # only send a test notification; no scan, nothing saved
        if not os.environ.get("NTFY_TOPIC", "").strip():
            print("NTFY_TOPIC is not set.")
            return 1
        notify("Commander Watch test", ["Test push. Notifications are working."], tags="white_check_mark")
        return 0
    if "--cardtrader-discover" in sys.argv:  # print CardTrader's catalogue structure; no scan, nothing saved
        return cardtrader_discover()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.isoformat().replace("+00:00", "Z")
    decks = load(DECKS_FILE, [])
    state = load(STATE_FILE, {"shops": {}})
    first_run = not decks
    by_id = {d["id"]: d for d in decks}
    by_url = {norm_url(d["url"]): d for d in decks}
    prices_before = deck_prices(decks)  # for Telegram alerts

    new_decks, restocked, alerts = [], [], []
    pending: list[tuple[str, dict, float | None]] = []  # changelog events, finished after grouping

    def run(item):
        _, (_, scan) = item
        started = time.monotonic()
        try:
            result = scan(requests.Session())
        except Exception as e:  # one broken scanner shouldn't stop the other shops
            result = [], [f"{type(e).__name__}: {e}"]
        return result, time.monotonic() - started

    # Shops are scanned in parallel; each still fetches its own pages one at a time.
    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
        results = dict(zip(SHOPS, pool.map(run, SHOPS.items())))

    for shop, (label, _) in SHOPS.items():
        result, took = results[shop]
        if result is None:  # e.g. CardTrader without a token
            print(f"{label}: skipped")
            continue
        found, errors = result
        if not found and not errors:
            errors = ["no products found"]
        shop_first = shop not in state["shops"]  # a newly added shop: its first scan is silent
        st = state["shops"].setdefault(shop, {"maxId": 0, "failStreak": 0})
        print(f"{label}: {len(found)} commander products, {len(errors)} page errors ({took:.0f} s)")
        for e in errors:
            print("   ", e)

        if errors and not found:
            st["failStreak"] = st.get("failStreak", 0) + 1
            if st["failStreak"] == FAIL_ALERT_AFTER:
                alerts.append(f"{label}: no data for {FAIL_ALERT_AFTER} checks in a row ({errors[0]})")
        else:
            st["failStreak"] = 0

        max_seen = st.get("maxId", 0)
        seen = set()
        for p in found:
            doc_id = f"{shop}-{p['pid']}" if p["pid"] else f"{shop}-{norm_url(p['url']).rsplit('/', 1)[-1][:120]}"
            known = by_id.get(doc_id) or by_url.get(norm_url(p["url"]))
            seen.add(known["id"] if known else doc_id)
            if known:
                was_orderable = known.get("status") in ORDERABLE
                old_price, old_status = known.get("price"), known.get("status")
                for k in ("price", "status", "name", "url"):
                    if p[k] is not None and known.get(k) != p[k]:
                        known[k] = p[k]
                if p["pid"] and not known.get("pid"):
                    known["pid"] = p["pid"]
                change = describe_change(old_price, old_status, known)
                if change:  # for the page's "Recently updated" sort
                    known["changedAt"], known["change"] = stamp, change
                if known["status"] in ORDERABLE and not was_orderable and shop not in QUIET_SHOPS:
                    restocked.append(known)  # back in stock, or its preorder opened (a release isn't a restock)
                if shop not in QUIET_SHOPS:
                    if kind := status_event(old_status, known["status"]):
                        pending.append((kind, known, None))
                    if (old_price is not None and known.get("price") is not None
                            and abs(old_price - known["price"]) >= PRICE_STEP):
                        pending.append(("price", known, old_price))
                continue
            # Unknown product. Shop ids only grow, so an id at or below the
            # highest one we've seen is an old listing we simply missed before.
            catch_up = (first_run or shop_first or shop in QUIET_SHOPS
                        or (p["pid"] is not None and p["pid"] <= st.get("maxId", 0)))
            deck = {"id": doc_id, "shop": shop, **p, "firstSeen": stamp, "baseline": catch_up,
                    "changedAt": stamp, "change": "listed"}
            decks.append(deck)
            by_id[doc_id] = deck
            by_url[norm_url(p["url"])] = deck
            if not catch_up:
                new_decks.append(deck)
                pending.append(("new", deck, None))
            if p["pid"]:
                max_seen = max(max_seen, p["pid"])
        st["maxId"] = max(st.get("maxId", 0), max_seen)
        if shop in VANISHING_SHOPS and found and not errors:
            for d in decks:
                if d["shop"] == shop and d["id"] not in seen and d.get("status") in ORDERABLE:
                    old_status = d["status"]
                    d["status"] = "out_of_stock"
                    d["changedAt"], d["change"] = stamp, describe_change(d.get("price"), old_status, d)
                    pending.append((status_event(old_status, d["status"]), d, None))
        if shop in QUIET_SHOPS and found and not errors:
            # CardTrader is only priced for decks shops sell; drop listings it no longer prices,
            # so a stale one can't keep a deck's CardTrader slot. Shop listings are never dropped.
            decks[:] = [d for d in decks if d["shop"] != shop or d["id"] in seen]

    # Weekly heartbeat keeps the repo "active" so GitHub doesn't pause the schedule.
    hb = state.get("heartbeat")
    if not hb or date.fromisoformat(hb) < (now - timedelta(days=7)).date():
        state["heartbeat"] = now.date().isoformat()

    assign_groups(decks)
    lost: list[dict] = []
    cheapest = cardtrader_newly_cheapest(decks, lost)
    pending += [("cheapest", ct, None) for ct, _ in cheapest] + [("notcheapest", ct, None) for ct in lost]
    decks.sort(key=lambda d: (d["shop"], d["id"]))
    save(DECKS_FILE, decks)
    save(STATE_FILE, state)
    append_changes(finish_events(pending, decks, stamp), now)
    history = load(history_file(), {})
    record_history(history, decks, now.date().isoformat())
    save_history(history)
    run_telegram(prices_before, deck_prices(decks))

    if new_decks:
        lines = [deck_line(d) for d in new_decks]
        title = "New Commander deck" if len(new_decks) == 1 else f"{len(new_decks)} new Commander decks"
        print(f"{title}:", *lines, sep="\n  ")
        notify(title, lines)
    else:
        print("No new Commander decks.")
    if restocked:
        lines = [deck_line(d) for d in restocked]
        title = "Commander deck back in stock" if len(restocked) == 1 else f"{len(restocked)} Commander decks back in stock"
        print(f"{title}:", *lines, sep="\n  ")
        notify(title, lines, tags="package")
    if cheapest:
        lines = [f"{ct['groupName']} — {ct['price']:.2f} zł"
                 + (f" (next: {SHOPS[nxt['shop']][0]} {nxt['price']:.2f} zł)" if nxt else " (no shop has it now)")
                 for ct, nxt in cheapest]
        title = "Cheapest on CardTrader" if len(cheapest) == 1 else f"{len(cheapest)} decks cheapest on CardTrader"
        print(f"{title}:", *lines, sep="\n  ")
        notify(title, lines, tags="moneybag")
    if alerts:
        notify("Commander Watch: shop check failing", alerts, tags="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
