"""Commander Watch: hourly check of MTG Commander precon listings in Polish shops.

Reads each shop's listing pages, compares them with data/decks.json and
sends a push notification (ntfy.sh) when a shop lists a deck for the first time.
Run by .github/workflows/check.yml; also runnable locally: `python check.py`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone, timedelta
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
FAIL_ALERT_AFTER = 6  # consecutive failed runs before a "shop is failing" alert

# Words that mark accessories rather than sealed decks.
NOT_A_DECK = re.compile(
    r"sleeve|koszulk|deck ?box|pudełk|playmat|mata|dragon shield|ultra pro|booster|binder|album",
    re.I,
)


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
            if r.status_code < 500 and r.status_code != 429:
                break
        except requests.RequestException as e:  # timeouts, resets
            last = type(e).__name__
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


# ------------------------------------------------------------------------ helpers

def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    t = text.replace("\xa0", " ").replace("zł", "").replace(" ", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def status_from(in_stock: bool, *texts: str) -> str:
    blob = " ".join(t or "" for t in texts).lower()
    if "przedsprzeda" in blob or "pre-order" in blob or "preorder" in blob:
        return "preorder"
    return "in_stock" if in_stock else "out_of_stock"


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


def parse_presta(html: str, base: str):
    """PrestaShop 1.6 (Cardstore)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("ul.product_list li.ajax_block_product"):
        a = li.select_one("a.product-name") or li.select_one("a.product_img_link")
        if not a:
            continue
        name = " ".join((a.get("title") or a.get_text()).split())
        if NOT_A_DECK.search(name):
            continue
        m = re.search(r"/(\d+)-", a["href"])
        desc = li.select_one(".product-desc")
        out.append({
            "pid": int(m.group(1)) if m else None,
            "name": name,
            "price": parse_price(li.select_one(".price").get_text() if li.select_one(".price") else None),
            "url": urljoin(base, a["href"]),
            "status": status_from(li.select_one(".availability .available-now") is not None,
                                  name, desc.get_text() if desc else ""),
        })
    return out, soup


def parse_woo(html: str, base: str):
    """WooCommerce (TCG Zielona). Category holds all MTG products, so filter by name."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.product"):
        title = li.select_one(".woocommerce-loop-product__title")
        a = li.select_one("a.woocommerce-LoopProduct-link") or li.find("a", href=True)
        if not title or not a:
            continue
        name = " ".join(title.get_text().split())
        if "commander" not in name.lower() or NOT_A_DECK.search(name):
            continue
        classes = li.get("class", [])
        pid = next((int(c[5:]) for c in classes if re.fullmatch(r"post-\d+", c)), None)
        bdi = li.select_one(".price ins bdi") or li.select_one(".price bdi")
        in_stock = "instock" in classes
        backorder = "onbackorder" in classes
        out.append({
            "pid": pid,
            "name": name,
            "price": parse_price(bdi.get_text() if bdi else None),
            "url": urljoin(base, a["href"]),
            "status": "preorder" if backorder else status_from(in_stock, name),
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


def scan_cardstore(s):
    base = "https://cardstore.pl"
    url = lambda n: f"{base}/157-commander" + (f"?p={n}" if n > 1 else "")
    return scan_paged(s, url, lambda h: parse_presta(h, base), re.compile(r"157-commander\?(?:.*&)?p=(\d+)"))


def scan_tcgzielona(s):
    base = "https://tcg-zielona.pl"
    url = lambda n: f"{base}/product-category/magic-the-gathering/" + (f"page/{n}/" if n > 1 else "")
    return scan_paged(s, url, lambda h: parse_woo(h, base),
                      re.compile(r"/product-category/magic-the-gathering/page/(\d+)/?$"))


def scan_paged(s, url_for, parse, page_re):
    """Fetch page 1, read how many pages exist, fetch the rest. Returns (products, errors)."""
    products, errors = {}, []
    last_page, n = 1, 1
    while n <= min(last_page, MAX_PAGES):
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
    "tcgzielona": ("TCG Zielona", scan_tcgzielona),
}


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


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    decks = load(DECKS_FILE, [])
    state = load(STATE_FILE, {"shops": {}})
    first_run = not decks
    by_id = {d["id"]: d for d in decks}
    by_url = {norm_url(d["url"]): d for d in decks}

    session = requests.Session()
    new_decks, alerts = [], []

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
                for k in ("price", "status", "name", "url"):
                    if p[k] is not None and known.get(k) != p[k]:
                        known[k] = p[k]
                if p["pid"] and not known.get("pid"):
                    known["pid"] = p["pid"]
                continue
            # Unknown product. Shop ids only grow, so an id at or below the
            # highest one we've seen is an old listing we simply missed before.
            catch_up = first_run or shop_first or (p["pid"] is not None and p["pid"] <= st.get("maxId", 0))
            deck = {"id": doc_id, "shop": shop, **p, "firstSeen": now.isoformat().replace("+00:00", "Z"),
                    "baseline": catch_up}
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

    decks.sort(key=lambda d: (d["shop"], d["id"]))
    save(DECKS_FILE, decks)
    save(STATE_FILE, state)

    if new_decks:
        lines = [f"{d['name']} — {SHOPS[d['shop']][0]} — "
                 f"{d['price']:.2f} zł ({d['status'].replace('_', ' ')})" if d["price"] is not None
                 else f"{d['name']} — {SHOPS[d['shop']][0]}" for d in new_decks]
        title = "New Commander deck" if len(new_decks) == 1 else f"{len(new_decks)} new Commander decks"
        print(title + ":\n  " + "\n  ".join(lines))
        notify(title, lines)
    else:
        print("No new Commander decks.")
    if alerts:
        notify("Commander Watch: shop check failing", alerts, tags="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
