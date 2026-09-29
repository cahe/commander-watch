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
    assert by[31606]["status"] == "preorder" and by[31606]["price"] == 179
    assert by[31606]["name"].startswith("Commander Reality Fracture: Multiverse Reforged")
    assert by[29972]["status"] == "in_stock"


def test_woo_parser():
    items, soup = check.parse_woo((FIX / "woo.html").read_text(), "https://tcg-zielona.pl")
    assert [i["pid"] for i in items] == [3414]  # prerelease event and sleeves filtered out
    assert items[0]["price"] == 195 and items[0]["status"] == "out_of_stock"
    assert check.page_numbers(soup, re.compile(r"/magic-the-gathering/page/(\d+)/?$")) == 2


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
