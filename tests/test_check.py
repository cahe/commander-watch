"""Offline tests: parsers against saved shop HTML, and the new/known/catch-up logic.

Run: python -m pytest -q   (or: python tests/test_check.py)
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def test_shoper_parser():
    items, soup = check.parse_shoper((FIX / "shoper.html").read_text(), "https://time4magic.pl", category="Commander Decki")
    by = {i["pid"]: i for i in items}
    assert set(by) == {978, 766, 1012, 700}  # sidebar booster excluded
    assert by[978]["status"] == "out_of_stock" and by[978]["price"] == 229
    assert by[766]["status"] == "in_stock"
    assert by[1012]["status"] == "preorder"
    assert by[978]["url"] == "https://time4magic.pl/mtg-reality-fracture-multiverse-reforged-commander-deck"
    assert check.page_numbers(soup, re.compile(r"/commander-decki-magic-the-gathering/(\d+)$")) == 4


def test_presta_parser():
    items, _ = check.parse_presta((FIX / "presta.html").read_text(), "https://cardstore.pl")
    by = {i["pid"]: i for i in items}
    # "[PRZEDSPRZEDAŻ]" in the name, but schema.org OutOfStock: Cardstore won't take the order.
    assert by[31606]["status"] == "out_of_stock" and by[31606]["price"] == 179
    assert by[31606]["name"].startswith("Commander Reality Fracture: Multiverse Reforged")
    assert by[29972]["status"] == "in_stock"


def test_presta_xjoy():
    items, soup = check.parse_presta((FIX / "xjoy.html").read_text(encoding="utf-8"), "https://www.xjoy.pl")
    by = {i["pid"]: i for i in items}
    assert by[26763]["status"] == "in_stock" and by[26763]["price"] == 249.99  # schema.org InStock
    assert by[21149]["status"] == "out_of_stock"
    assert by[25942]["status"] == "preorder"  # "Przedsprzedaż" label, though schema says InStock
    assert check.page_numbers(soup, re.compile(r"371-mtg-commander\?(?:.*&)?p=(\d+)")) == 2
    items, _ = check.parse_presta((FIX / "xjoy.html").read_text(encoding="utf-8"), "https://www.xjoy.pl",
                                  match=check.COMMANDER_DECK)
    assert 25942 not in {i["pid"] for i in items}  # board game in the preorder category


def test_presta17_wargamer():
    html = (FIX / "wargamer.html").read_text(encoding="utf-8")
    items, soup = check.parse_presta17(html, "https://sklep.wargamer.pl")
    by = {i["pid"]: i for i in items}
    assert by[17060]["status"] == "in_stock" and by[17060]["price"] == 287
    assert by[17058]["status"] == "out_of_stock"  # disabled cart button
    assert by[18103]["status"] == "preorder"      # "Przedsprzedaż" flag
    assert check.page_numbers(soup, re.compile(r"43-magic-the-gathering\?(?:.*&)?page=(\d+)")) == 4
    items, _ = check.parse_presta17(html, "https://sklep.wargamer.pl", match=re.compile("commander", re.I))
    assert {i["pid"] for i in items} == {17060, 17058}  # theme deck and preorder wargame filtered out


def test_sstore_dragoneye():
    html = (FIX / "sstore.html").read_text(encoding="utf-8")
    items, soup = check.parse_sstore(html, "https://dragoneye.pl")
    by = {i["pid"]: i for i in items}
    assert by[9549]["status"] == "in_stock" and by[9549]["price"] == 199.9
    assert by[9946]["status"] == "out_of_stock"
    assert by[9944]["price"] == 189.9             # sale price, not the struck-through one
    assert 12368 in by                            # "Secrets of Strixhaven - Lorehold Spirit": no "commander" in the name
    assert 11510 not in by                        # bundle
    assert 12526 in by                            # no Magic filter outside the preorder category
    assert check.page_numbers(soup, re.compile(r"c-16_211\.html\?(?:.*&)?page=(\d+)")) == 2
    items, _ = check.parse_sstore(html, "https://dragoneye.pl", preorder_category=True)
    assert 12526 not in {i["pid"] for i in items}  # board game in the preorder category
    assert {i["status"] for i in items if i["pid"] in (9549, 9944)} == {"preorder"}


def test_shoper_classic_panmysza():
    items, soup = check.parse_shoper_classic((FIX / "shoper_classic.html").read_text(encoding="utf-8"),
                                             "https://panmysza.pl")
    by = {i["pid"]: i for i in items}
    assert set(by) == {8236, 10754}  # Commander's Bundle and a booster filtered out
    assert by[8236]["status"] == "in_stock" and by[8236]["price"] == 168
    assert by[10754]["status"] == "out_of_stock"  # "notify me" instead of a basket button
    assert by[8236]["url"] == "https://panmysza.pl/pl/p/MtG-Final-Fantasy-Commander-Deck-Revival-Trance/8236"
    assert check.page_numbers(soup, re.compile(r"/pl/c/Magic-The-Gathering/53/(\d+)$")) == 4


def test_woo_magiccafe():
    items = check.parse_woo((FIX / "woo.json").read_text(encoding="utf-8"), "https://magiccafe.eu")
    by = {i["pid"]: i for i in items}
    assert by[29308]["status"] == "preorder" and by[29308]["price"] == 209.99  # "Przedsprzedaż" tag, orderable
    assert by[29308]["name"] == "Foundations: “Tramplesaurus Rex” Commander Deck"  # entities decoded
    assert by[28540]["status"] == "in_stock"
    assert by[27079]["status"] == "out_of_stock"
    assert 28221 in by  # the five-deck set is kept; grouping files it apart from single decks
    assert not any("commander" not in i["name"].lower() for i in items)  # other ready-made decks dropped


def test_sote_wilczek():
    items, soup = check.parse_sote((FIX / "sote.html").read_text(encoding="utf-8"), "https://wilczek.poznan.pl")
    by = {i["pid"]: i for i in items}
    assert set(by) == {15239, 15023, 14019, 15232}  # Dragon Shield sleeves dropped
    assert by[15239]["status"] == "preorder" and by[15239]["price"] == 349.99  # "Przedsprzedaż" label
    assert by[15239]["name"] == "Magic: the Gathering - Star Trek - Commander Deck - Klingon Fury"  # full name, not cut
    assert by[15023]["status"] == "in_stock"
    assert by[14019]["status"] == "out_of_stock"  # "Brak", though it still has a basket button
    assert by[15232]["url"] == "https://wilczek.poznan.pl/magic-the-gathering-star-trek-commander-deck-set.html"
    assert check.page_numbers(soup, re.compile(r"/product/search/(\d+)/long/")) == 4


def test_frostmagic():
    fx = json.loads((FIX / "frostmagic.json").read_text(encoding="utf-8"))
    pages = {
        "https://frostmagic.pl/api/presales": fx["presales"],
        "https://frostmagic.pl/api/presales/magic-the-gathering-star-trek/products?pageSize=100": fx["presale:magic-the-gathering-star-trek"],
        "https://frostmagic.pl/api/products?category=commander-decks&pageSize=100&page=1": fx["products"],
    }
    real_fetch = check.fetch
    asked = []
    check.fetch = lambda url, s: asked.append(url) or json.dumps(pages[url])
    try:
        items, errors = check.scan_frostmagic(None)
    finally:
        check.fetch = real_fetch
    assert errors == []
    assert not any("riftbound" in u or "naruto" in u for u in asked)  # other games' presales aren't fetched
    by = {i["url"].rsplit("/", 1)[-1]: i for i in items}
    display = by["magic-the-gathering-star-trek-commander-deck-display"]
    assert display["status"] == "preorder" and display["price"] == 1029  # in both lists; the presale wins
    assert by["magic-the-gathering-star-trek-commander-display-collector-s-edition"]["status"] == "out_of_stock"
    assert by["mtg-secrets-of-strixhaven-prismari-artistry-commander-deck"]["status"] == "in_stock"
    assert not any("Bundle" in i["name"] or "Booster" in i["name"] for i in items)  # presale's other products
    assert display["url"] == "https://frostmagic.pl/pl/products/magic-the-gathering-star-trek-commander-deck-display"


def test_grouping():
    names = [
        ("time4magic", "Lorwyn Eclipsed: \"Dance of the Elements\" Commander Deck"),
        ("dragoneye", "Magic the Gathering: Lorwyn Eclipsed - Commander Deck - Dace of The Elements"),  # shop typo
        ("mrpuggy", "Lorwyn Eclipsed Commander Deck: Blight Curse"),
        ("panmysza", "MtG Final Fantasy Commander Deck - Scions and Spellcraft"),
        ("cardstore", "Commander MtG Final Fantasy - Scions & Spellcraft"),
        ("xjoy", "MTG: Final Fantasy Collector's Commander Deck - Scions & Spellcraft"),
        ("mrpuggy", "Magic: The Gathering - Final Fantasy - Commander Deck Display (4)"),
        ("time4magic", "Commander Deck Lord of the Rings: Tales of Middle-earth - Elven Council"),
        ("cardstore", "Final Fantasy Commander Deck: Scions & Spellcraft"),  # same shop twice: only one joins
        ("wargamer", "Lorwyn Eclipsed Commander Deck: Dance of the Elements"),
    ]
    decks = [{"id": f"d{i}", "shop": s, "name": n} for i, (s, n) in enumerate(names)]
    check.assign_groups(decks)
    g = [d["group"] for d in decks]
    assert g[0] == g[1] == g[9] != g[2]          # typo tolerated, different deck kept apart
    assert (g[3] == g[4]) != (g[3] == g[8])      # "and" vs "&"; only one Cardstore listing per group
    assert g[5] not in (g[3], g[6])              # collector's edition and display stay separate
    assert decks[0]["groupName"] == "Lorwyn Eclipsed – Dance of the Elements"  # the spelling shops agree on
    assert decks[7]["groupName"] == "Lord of the Rings – Tales of Middle-earth – Elven Council"
    assert decks[3]["groupName"] in ("Final Fantasy – Scions & Spellcraft", "Final Fantasy – Scions and Spellcraft")


def test_set_only_names():
    decks = [{"id": f"d{i}", "shop": s, "name": n} for i, (s, n) in enumerate([
        ("time4magic", "Reality Fracture - Multiverse Reforged Commander Deck"),
        ("xjoy", "MTG: Reality Fracture - Commander Deck: Multiverse Reforged"),
        ("cardtrader", "Reality Fracture Commander Deck"),            # the set's only deck: joins it
        ("time4magic", "Doctor Who Commander Deck: Blast from the Past"),
        ("time4magic", "Doctor Who Commander Deck: Paradox Power"),
        ("mrpuggy", "Doctor Who Commander Deck"),                    # several decks: stays apart
    ])]
    check.assign_groups(decks)
    g = [d["group"] for d in decks]
    assert g[0] == g[1] == g[2] and decks[2]["groupName"] == "Reality Fracture – Multiverse Reforged"
    assert g[5] not in (g[3], g[4])


def test_cardtrader_offers():
    zero = {"can_sell_via_hub": True}
    offer = lambda cents, cur="EUR", **kw: {"price": {"cents": cents, "currency": cur}, "quantity": 1,
                                            "user": kw.pop("user", zero), **kw}
    rates = {"EUR": 4.0, "PLN": 1.0}
    offers = [
        offer(3000, user={"can_sell_via_hub": False}),          # not CardTrader Zero
        offer(3100, properties_hash={"mtg_language": "de"}),     # German
        offer(3200, on_vacation=True),
        offer(3300, bundle_size=2),
        offer(3400, quantity=0),
        offer(3500, cur="XYZ"),                                  # no exchange rate
        offer(5000),
        offer(4500, properties_hash={"mtg_language": "en"}),
    ]
    assert check.cheapest_zero_offer(offers, rates) == 180.0     # 45.00 EUR * 4.0
    assert check.cheapest_zero_offer(offers[:6], rates) is None


def test_cardtrader_is_quiet():
    """CardTrader listings never announce new decks or restocks."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    check.DECKS_FILE, check.STATE_FILE = tmp / "data" / "decks.json", tmp / "data" / "state.json"
    check.STATE_FILE.write_text(json.dumps({"shops": {"cardtrader": {"maxId": 0, "failStreak": 0}}}))
    check.DECKS_FILE.write_text("[]")
    sent = []
    check.notify = lambda title, lines, tags="": sent.append(title)
    listing = {"pid": 389398, "name": 'Foundations: "Calling All Angels" Commander Deck', "price": None,
               "url": "https://www.cardtrader.com/en/cards/389398", "status": "out_of_stock"}
    check.SHOPS = {"cardtrader": ("CardTrader", lambda s: ([dict(listing)], []))}
    check.main()                                   # new deck: would normally push (maxId 0)
    listing.update(price=180.0, status="in_stock")
    check.main()                                   # back in stock: would normally push
    assert sent == []
    d = json.loads(check.DECKS_FILE.read_text(encoding="utf-8"))[0]
    assert d["baseline"] is True and d["status"] == "in_stock" and d["change"] == "back in stock"
    # a CardTrader listing it no longer prices is dropped (shop listings never are)
    other = dict(listing, pid=389399, name='Foundations: "Keen Engineering" Commander Deck',
                 url="https://www.cardtrader.com/en/cards/389399")
    check.SHOPS = {"cardtrader": ("CardTrader", lambda s: ([dict(listing), dict(other)], []))}
    check.main()
    check.SHOPS = {"cardtrader": ("CardTrader", lambda s: ([dict(other)], []))}
    check.main()
    assert [d["id"] for d in json.loads(check.DECKS_FILE.read_text(encoding="utf-8"))] == ["cardtrader-389399"]


def test_cardtrader_cheapest_push():
    ct = {"id": "cardtrader-1", "shop": "cardtrader", "group": "g", "status": "in_stock", "price": 200.0}
    shop = {"id": "xjoy-1", "shop": "xjoy", "group": "g", "status": "in_stock", "price": 190.0}
    sold = {"id": "time4magic-1", "shop": "time4magic", "group": "g", "status": "out_of_stock", "price": 150.0}
    decks = [ct, shop, sold]
    assert check.cardtrader_newly_cheapest(decks) == [] and ct["cheapest"] is False  # dearer than XJoy
    ct["price"] = 185.0
    assert check.cardtrader_newly_cheapest(decks) == [(ct, shop)]     # now cheapest -> push
    assert check.cardtrader_newly_cheapest(decks) == []               # still cheapest -> no repeat
    shop["status"] = "out_of_stock"                                   # sold-out shops don't count...
    ct["cheapest"] = False
    assert check.cardtrader_newly_cheapest(decks) == [(ct, None)]     # ...so CardTrader is the only option
    fresh = {"id": "cardtrader-2", "shop": "cardtrader", "group": "h", "status": "in_stock", "price": 1.0}
    other = {"id": "xjoy-2", "shop": "xjoy", "group": "h", "status": "in_stock", "price": 99.0}
    assert check.cardtrader_newly_cheapest([fresh, other]) == []      # first evaluation is silent
    assert fresh["cheapest"] is True
    alone = {"id": "cardtrader-3", "shop": "cardtrader", "group": "k", "status": "in_stock", "price": 1.0}
    assert check.cardtrader_newly_cheapest([alone]) == [] and "cheapest" not in alone  # no shop sells it


def test_cardtrader_only_decks_shops_sell():
    tmp = Path(tempfile.mkdtemp())
    check.DECKS_FILE = tmp / "decks.json"
    check.DECKS_FILE.write_text(json.dumps([
        {"id": "time4magic-1", "shop": "time4magic", "name": "Doctor Who Commander Deck: Blast from the Past"},
        {"id": "panmysza-1", "shop": "panmysza",
         "name": "The Lord of the Rings: Tales of Middle-earth Commander Deck Food and Fellowship"},
        {"id": "xjoy-1", "shop": "xjoy", "name": "MTG: Marvel Super Heroes - Commander Deck - Avengers Assemble"},
    ]))
    bps = [
        {"id": 1, "name": 'Universes Beyond: Doctor Who "Blast from the Past" Commander Deck'},
        {"id": 2, "name": 'Commander: The Lord of the Rings | "Food And Fellowship" Commander Deck'},  # 3+ words contained
        {"id": 3, "name": 'Marvel Super Heroes | "Avengers Assemble" Commander Deck'},
        {"id": 4, "name": "Marvel Super Heroes | \"Avengers Assemble\" Commander Deck Collector's Edition"},  # version
        {"id": 5, "name": 'Commander Legends: "Arm for Battle" Commander Deck'},  # no shop sells it
    ]
    assert [b["id"] for b in check.sold_by_shops(bps)] == [1, 2, 3]


def test_changelog_events():
    tmp = Path(tempfile.mkdtemp())
    check.DECKS_FILE, check.STATE_FILE = tmp / "decks.json", tmp / "state.json"
    check.notify = lambda title, lines, tags="": None
    deck = lambda pid, name, price, status, url: {"pid": pid, "name": name, "price": price, "status": status, "url": url}
    shops = {
        "time4magic": [deck(1, "Foundations Commander Deck: Keen Engineering", 129.0, "out_of_stock", "https://t/1"),
                       deck(2, "Final Fantasy Commander Deck: Limit Break", 249.0, "in_stock", "https://t/2")],
        "xjoy": [deck(5, "MTG: Foundations Starter Commander Deck - Keen Engineering", 139.99, "in_stock", "https://x/5")],
        "cardtrader": [deck(9, 'Foundations: "Keen Engineering" Commander Deck', 150.0, "in_stock", "https://c/9")],
    }
    check.SHOPS = {k: (k, (lambda k: lambda s: ([dict(x) for x in shops[k]], []))(k)) for k in shops}
    old = {"at": "2020-01-01T00:00:00Z", "kind": "restock"}                     # older than 30 days
    check.changes_file().write_text(json.dumps([old]))
    check.main()                                                               # first run: baseline only
    assert json.loads(check.changes_file().read_text()) == []                  # and the old event is pruned
    shops["time4magic"][0].update(status="in_stock", price=125.0)              # restock, cheaper than XJoy
    shops["time4magic"][1].update(status="out_of_stock")                       # sold out
    shops["time4magic"].append(deck(3, "Final Fantasy Commander Deck: Revival Trance", 199.0, "in_stock", "https://t/3"))
    shops["xjoy"][0].update(price=145.0)                                       # price change
    shops["cardtrader"][0].update(price=120.0)                                 # CardTrader now cheapest
    check.main()
    events = {(e["kind"], e["shop"]): e for e in json.loads(check.changes_file().read_text(encoding="utf-8"))}
    assert set(events) == {("restock", "time4magic"), ("price", "time4magic"), ("soldout", "time4magic"),
                           ("new", "time4magic"), ("price", "xjoy"), ("cheapest", "cardtrader")}
    r = events[("restock", "time4magic")]
    assert r["deck"].endswith("Keen Engineering") and r["cheapest"] is True and r["best"]["shop"] == "xjoy"
    assert events[("price", "xjoy")]["old"] == 139.99 and events[("price", "xjoy")]["price"] == 145.0
    assert events[("soldout", "time4magic")]["inStock"] == 0
    assert events[("cheapest", "cardtrader")]["best"] == {"shop": "time4magic", "price": 125.0}
    shops["cardtrader"][0].update(price=130.0)                                 # small CardTrader move: one "notcheapest"
    check.main()
    last = json.loads(check.changes_file().read_text(encoding="utf-8"))[-1]
    assert last["kind"] == "notcheapest" and len(json.loads(check.changes_file().read_text())) == 7


def test_empty_category_is_a_failure():
    block_page = "<html><head><title>Just a moment...</title></head><body></body></html>"
    real_fetch = check.fetch
    check.fetch = lambda url, s: block_page
    try:
        parse = lambda h: check.parse_presta(h, "https://www.xjoy.pl")
        page_re = re.compile(r"p=(\d+)")
        items, errors = check.scan_paged(None, lambda n: "https://www.xjoy.pl/371-mtg-commander", parse, page_re)
        assert items == [] and len(errors) == 1 and "'Just a moment...'" in errors[0]
        items, errors = check.scan_paged(None, lambda n: "https://www.xjoy.pl/66-przedsprzedaz", parse, page_re,
                                         may_be_empty=True)
        assert items == [] and errors == []  # preorder categories may hold no Commander deck
    finally:
        check.fetch = real_fetch


def test_rate_limit_spacing():
    limit = check.RateLimit(50)
    from concurrent.futures import ThreadPoolExecutor
    started = check.time.monotonic()
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: limit.wait(), range(8)))
    assert check.time.monotonic() - started >= 7 * 0.02 - 0.005  # 8 calls, 4 threads, still 20 ms apart


def test_diff_new_vs_catch_up(monkeypatch=None):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    shutil.copy(Path(check.ROOT) / "data" / "decks.json", tmp / "data" / "decks.json")
    shutil.copy(Path(check.ROOT) / "data" / "state.json", tmp / "data" / "state.json")
    check.DECKS_FILE, check.STATE_FILE = tmp / "data" / "decks.json", tmp / "data" / "state.json"
    sent = []
    check.notify = lambda title, lines, tags="": sent.append((title, lines))
    shoper = (FIX / "shoper.html").read_text()
    check.SHOPS = {
        "time4magic": ("Time4Magic", lambda s: (check.parse_shoper(shoper, "https://time4magic.pl", category="Commander Decki")[0], [])),
        "mrpuggy": ("Mr. Puggy", lambda s: ([], ["https://mrpuggy.pl/...: HTTP 504"])),
    }
    check.main()
    decks = {d["id"]: d for d in json.loads(check.DECKS_FILE.read_text())}
    assert decks["time4magic-1012"]["baseline"] is False       # id above maxId 998 -> new
    assert decks["time4magic-700"]["baseline"] is True         # id below maxId -> missed earlier, silent
    assert decks["time4magic-766"]["status"] == "in_stock"
    assert len(sent) == 1 and "Brand New" in sent[0][1][0]
    state = json.loads(check.STATE_FILE.read_text())
    assert state["shops"]["time4magic"]["maxId"] == 1012
    assert state["shops"]["mrpuggy"]["failStreak"] == 1
    # second run: nothing new, no push
    sent.clear()
    check.main()
    assert sent == []
    # a known deck that was out of stock and is now listed in stock -> "back in stock" push
    data = json.loads(check.DECKS_FILE.read_text())
    d766 = next(d for d in data if d["id"] == "time4magic-766")
    d766["status"], d766["price"] = "out_of_stock", 250.0
    check.DECKS_FILE.write_text(json.dumps(data))
    check.main()
    # (the live data can make other pushes due on the same run, e.g. a CardTrader price; only the restock matters here)
    restock = [t for t in sent if "back in stock" in t[0]]
    assert len(restock) == 1 and "766" not in restock[0][0]
    after = {d["id"]: d for d in json.loads(check.DECKS_FILE.read_text(encoding="utf-8"))}
    assert after["time4magic-766"]["change"].startswith("back in stock, price 250.00 → ")  # for "Recently updated"
    assert after["time4magic-1012"]["change"] == "listed"
    unchanged_at = after["time4magic-766"]["changedAt"]
    sent.clear()
    check.main()
    assert sent == []
    # nothing changed on that run, so the change time stays put
    assert {d["id"]: d for d in json.loads(check.DECKS_FILE.read_text(encoding="utf-8"))}["time4magic-766"]["changedAt"] == unchanged_at


def test_price_history():
    h = {}
    deck = {"id": "xjoy-1", "price": 250.0, "status": "in_stock"}
    check.record_history(h, [deck], "2026-10-01")
    deck["price"] = 240.0
    check.record_history(h, [deck], "2026-10-01")   # same day: keeps the day's low
    deck["price"] = 260.0
    check.record_history(h, [deck], "2026-10-01")
    check.record_history(h, [deck], "2026-10-02")   # next day at 260: a change
    check.record_history(h, [deck], "2026-10-03")   # unchanged: no new point
    deck["status"] = "out_of_stock"
    check.record_history(h, [deck], "2026-10-04")   # can't be ordered: None
    deck["status"], deck["price"] = "preorder", 260.0
    check.record_history(h, [deck], "2026-10-04")   # orderable again later that day: the day's low is 260
    check.record_history(h, [deck], "2026-10-05")
    deck["price"] = 260.4
    check.record_history(h, [deck], "2026-10-06")   # under 1 zł: not a change
    assert h["listings"]["xjoy-1"] == [["2026-10-01", 240.0], ["2026-10-02", 260.0]]  # the 4th's 260 merged away
    tmp = Path(tempfile.mkdtemp())
    check.DECKS_FILE = tmp / "decks.json"
    check.save_history(h)
    assert json.loads(check.history_file().read_text(encoding="utf-8")) == h


def test_preorder_counts_as_in_stock():
    assert check.status_event("preorder", "in_stock") is None    # released: still orderable
    assert check.status_event("in_stock", "preorder") is None
    assert check.status_event("out_of_stock", "preorder") == "preorder"
    assert check.status_event("out_of_stock", "in_stock") == "restock"
    assert check.status_event("preorder", "out_of_stock") == "soldout"
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    check.DECKS_FILE, check.STATE_FILE = tmp / "data" / "decks.json", tmp / "data" / "state.json"
    sent = []
    check.notify = lambda title, lines, tags="": sent.append(title)
    deck = lambda status: {"pid": 5, "name": "Star Trek Klingon Fury Commander Deck", "price": 349.99,
                           "url": "https://wilczek.poznan.pl/klingon-fury.html", "status": status}
    pushes = []
    for status in ("out_of_stock", "preorder", "in_stock", "out_of_stock", "in_stock"):
        check.SHOPS = {"wilczek": ("Wilczek", lambda s, status=status: ([deck(status)], []))}
        sent.clear()
        check.main()
        pushes.append(len(sent))
    # first scan silent; preorder opening pushes; release doesn't; sold out doesn't; back in stock pushes
    assert pushes == [0, 1, 0, 0, 1]


def test_vanished_listing_is_sold_out():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    check.DECKS_FILE, check.STATE_FILE = tmp / "data" / "decks.json", tmp / "data" / "state.json"
    check.notify = lambda title, lines, tags="": None
    listing = lambda slug, status="in_stock": {"pid": None, "name": f"Deck {slug} Commander Deck", "price": 200.0,
                                               "url": f"https://frostmagic.pl/pl/products/{slug}", "status": status}
    scans = [([listing("a"), listing("b")], []),
             ([listing("a")], []),                       # "b" sold out and dropped off the list
             ([], ["HTTP 503 for https://frostmagic.pl/api/products"]),  # failed scan: nothing changes
             ([listing("a"), listing("b")], [])]         # "b" is back
    statuses = []
    for scan in scans:
        check.SHOPS = {"frostmagic": ("Frost Magic", lambda s, scan=scan: scan)}
        check.main()
        statuses.append({d["url"][-1]: d["status"] for d in json.loads(check.DECKS_FILE.read_text())})
    assert statuses == [{"a": "in_stock", "b": "in_stock"}, {"a": "in_stock", "b": "out_of_stock"},
                        {"a": "in_stock", "b": "out_of_stock"}, {"a": "in_stock", "b": "in_stock"}]
    kinds = [e["kind"] for e in json.loads(check.changes_file().read_text(encoding="utf-8"))]
    assert kinds == ["soldout", "restock"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
