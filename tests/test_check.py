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
    assert len(sent) == 1 and "back in stock" in sent[0][0] and "766" not in sent[0][0]
    after = {d["id"]: d for d in json.loads(check.DECKS_FILE.read_text(encoding="utf-8"))}
    assert after["time4magic-766"]["change"].startswith("back in stock, price 250.00 → ")  # for "Recently updated"
    assert after["time4magic-1012"]["change"] == "listed"
    unchanged_at = after["time4magic-766"]["changedAt"]
    sent.clear()
    check.main()
    assert sent == []
    # nothing changed on that run, so the change time stays put
    assert {d["id"]: d for d in json.loads(check.DECKS_FILE.read_text(encoding="utf-8"))}["time4magic-766"]["changedAt"] == unchanged_at


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
