"""Commander Watch: hourly check of MTG Commander precon listings in Polish shops.

Reads each shop's listing pages, compares them with data/decks.json and
sends a push notification (ntfy.sh) when a shop lists a deck for the first time.
Run by .github/workflows/check.yml; also runnable locally: `python check.py`.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote

import requests
from bs4 import BeautifulSoup

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

# Words that mark accessories rather than sealed decks.
NOT_A_DECK = re.compile(
    r"sleeve|koszulk|deck ?box|pudełk|playmat|mata|dragon shield|ultra pro|booster|binder|album|bundle",
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
                     re.compile(r"130-przedsprzedaz\?(?:.*&)?page=(\d+)"), max_pages=3)
    return merge_scans(mtg, pre)


def scan_xjoy(s):
    base = "https://www.xjoy.pl"
    decks = scan_paged(s, lambda n: f"{base}/371-mtg-commander" + (f"?p={n}" if n > 1 else ""),
                       lambda h: parse_presta(h, base), re.compile(r"371-mtg-commander\?(?:.*&)?p=(\d+)"))
    pre = scan_paged(s, lambda n: f"{base}/66-przedsprzedaz" + (f"?p={n}" if n > 1 else ""),
                     lambda h: parse_presta(h, base, match=COMMANDER_DECK),
                     re.compile(r"66-przedsprzedaz\?(?:.*&)?p=(\d+)"))
    return merge_scans(decks, pre)


def scan_dragoneye(s):
    base = "https://dragoneye.pl"
    mtg = scan_paged(s, lambda n: f"{base}/magic-the-gathering-c-16_211.html" + (f"?page={n}" if n > 1 else ""),
                     lambda h: parse_sstore(h, base), re.compile(r"c-16_211\.html\?(?:.*&)?page=(\d+)"))
    # All games' preorders; sort=4d lists the newest additions first.
    pre = scan_paged(s, lambda n: f"{base}/przedsprzedaz-c-75.html?sort=4d&page={n}",
                     lambda h: parse_sstore(h, base, preorder_category=True),
                     re.compile(r"c-75\.html\?(?:.*&)?page=(\d+)"), max_pages=3)
    return merge_scans(mtg, pre)


def merge_scans(*scans):
    """Combine (products, errors) from several categories; a deck in both appears once."""
    products, errors = {}, []
    for items, errs in scans:
        for it in items:
            products.setdefault(norm_url(it["url"]), it)
        errors += errs
    return list(products.values()), errors


def scan_paged(s, url_for, parse, page_re, max_pages=MAX_PAGES):
    """Fetch page 1, read how many pages exist, fetch the rest. Returns (products, errors)."""
    products, errors = {}, []
    last_page, n = 1, 1
    while n <= min(last_page, max_pages):
        try:
            items, soup = parse(fetch(url_for(n), s))
            last_page = max(last_page, page_numbers(soup, page_re))
            for it in items:
                products.setdefault(norm_url(it["url"]), it)
        except Exception as e:  # keep going; a failed page is not a removal
            errors.append(str(e))
            if n == 1:
                break
        n += 1
    return list(products.values()), errors


SHOPS = {
    "time4magic": ("Time4Magic", scan_time4magic),
    "cardstore": ("Cardstore", scan_cardstore),
    "mrpuggy": ("Mr. Puggy", scan_mrpuggy),
    "wargamer": ("Wargamer", scan_wargamer),
    "xjoy": ("XJoy", scan_xjoy),
    "dragoneye": ("Dragoneye", scan_dragoneye),
    "panmysza": ("Pan Mysza", scan_panmysza),
}


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

    pairs = []
    for i in range(len(decks)):
        for j in range(i + 1, len(decks)):
            if decks[i]["shop"] != decks[j]["shop"] and kinds[i] == kinds[j] and toks[i] and toks[j]:
                s = score(toks[i], toks[j])
                if s >= GROUP_THRESHOLD:
                    pairs.append((s, i, j))
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

    members: dict[int, list[int]] = {}
    for i in range(len(decks)):
        members.setdefault(find(i), []).append(i)
    for idx in members.values():
        names = [clean_name(decks[i]["name"]) for i in idx]
        # The name whose words most shops agree on (so one shop's typo or missing deck name loses), then the shortest.
        agree = Counter(t for i in idx for t in toks[i])
        best = max(range(len(idx)), key=lambda k: (sum(agree[t] for t in toks[idx[k]]), -len(names[k])))
        gid = min(decks[i]["id"] for i in idx)
        for i in idx:
            decks[i]["group"] = gid
            decks[i]["groupName"] = names[best]


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


def describe_change(old_price: float | None, old_status: str | None, d: dict) -> str:
    """Short note for the page, e.g. "back in stock, price 239.00 → 219.00 zł"; "" if nothing changed."""
    parts = []
    if old_status != d["status"]:
        parts.append({"in_stock": "back in stock" if old_status == "out_of_stock" else "now in stock",
                      "preorder": "pre-order opened", "out_of_stock": "sold out"}.get(d["status"], d["status"]))
    if old_price is not None and d["price"] is not None and abs(old_price - d["price"]) >= 0.01:
        parts.append(f"price {old_price:.2f} → {d['price']:.2f} zł")
    return ", ".join(parts)


def deck_line(d: dict) -> str:
    shop = SHOPS[d["shop"]][0]
    if d["price"] is None:
        return f"{d['name']} — {shop}"
    return f"{d['name']} — {shop} — {d['price']:.2f} zł ({d['status'].replace('_', ' ')})"


def main() -> int:
    if "--test-push" in sys.argv:  # only send a test notification; no scan, nothing saved
        if not os.environ.get("NTFY_TOPIC", "").strip():
            print("NTFY_TOPIC is not set.")
            return 1
        notify("Commander Watch test", ["Test push. Notifications are working."], tags="white_check_mark")
        return 0
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.isoformat().replace("+00:00", "Z")
    decks = load(DECKS_FILE, [])
    state = load(STATE_FILE, {"shops": {}})
    first_run = not decks
    by_id = {d["id"]: d for d in decks}
    by_url = {norm_url(d["url"]): d for d in decks}

    session = requests.Session()
    new_decks, restocked, alerts = [], [], []

    for shop, (label, scan) in SHOPS.items():
        shop_first = shop not in state["shops"]  # a newly added shop: its first scan is silent
        st = state["shops"].setdefault(shop, {"maxId": 0, "failStreak": 0})
        found, errors = scan(session)
        print(f"{label}: {len(found)} commander products, {len(errors)} page errors")
        for e in errors:
            print("   ", e)

        if errors and not found:
            st["failStreak"] = st.get("failStreak", 0) + 1
            if st["failStreak"] == FAIL_ALERT_AFTER:
                alerts.append(f"{label}: no data for {FAIL_ALERT_AFTER} checks in a row ({errors[0]})")
        else:
            st["failStreak"] = 0

        max_seen = st.get("maxId", 0)
        for p in found:
            doc_id = f"{shop}-{p['pid']}" if p["pid"] else f"{shop}-{norm_url(p['url']).rsplit('/', 1)[-1][:120]}"
            known = by_id.get(doc_id) or by_url.get(norm_url(p["url"]))
            if known:
                was_in_stock = known.get("status") == "in_stock"
                old_price, old_status = known.get("price"), known.get("status")
                for k in ("price", "status", "name", "url"):
                    if p[k] is not None and known.get(k) != p[k]:
                        known[k] = p[k]
                if p["pid"] and not known.get("pid"):
                    known["pid"] = p["pid"]
                change = describe_change(old_price, old_status, known)
                if change:  # for the page's "Recently updated" sort
                    known["changedAt"], known["change"] = stamp, change
                if known["status"] == "in_stock" and not was_in_stock:
                    restocked.append(known)  # back in stock (or preorder became available)
                continue
            # Unknown product. Shop ids only grow, so an id at or below the
            # highest one we've seen is an old listing we simply missed before.
            catch_up = first_run or shop_first or (p["pid"] is not None and p["pid"] <= st.get("maxId", 0))
            deck = {"id": doc_id, "shop": shop, **p, "firstSeen": stamp, "baseline": catch_up,
                    "changedAt": stamp, "change": "listed"}
            decks.append(deck)
            by_id[doc_id] = deck
            by_url[norm_url(p["url"])] = deck
            if not catch_up:
                new_decks.append(deck)
            if p["pid"]:
                max_seen = max(max_seen, p["pid"])
        st["maxId"] = max(st.get("maxId", 0), max_seen)

    # Weekly heartbeat keeps the repo "active" so GitHub doesn't pause the schedule.
    hb = state.get("heartbeat")
    if not hb or date.fromisoformat(hb) < (now - timedelta(days=7)).date():
        state["heartbeat"] = now.date().isoformat()

    assign_groups(decks)
    decks.sort(key=lambda d: (d["shop"], d["id"]))
    save(DECKS_FILE, decks)
    save(STATE_FILE, state)

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
    if alerts:
        notify("Commander Watch: shop check failing", alerts, tags="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
