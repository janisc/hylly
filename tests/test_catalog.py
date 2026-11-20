"""The catalog end to end on a small library: Humble, StoryBundle, collections, comics, manuals."""

from __future__ import annotations

import json
import os
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import pytest

from hylly import catalog, cli, exits, helppage, meta, paths, plan, snapshot
from hylly.download import Manifest

from helpers import OPF, ORDERS, build_library, damage_a_mobi, data, locked_epub, make_epub, order, page_data, png, rec
from helpers import sub, with_settings


MB = 1024 * 1024


def test_catalog_end_to_end(tmp_path, monkeypatch):
    build_library(tmp_path)
    stats = catalog.build(tmp_path, log=lambda s: None)
    items = {i["id"]: i for i in data(tmp_path)}
    novel = items["h:novel"]
    assert novel["t"] == "The Novel" and novel["src"] == "humble" and novel["a"] == ["Marlow Ellery"]  # shop title, file author
    assert novel["c"].startswith("covers/") and (tmp_path / "catalog" / novel["c"]).is_file()
    assert "Science Fiction" in novel["g"] and novel["dev"] == "ereader"
    assert items["h:lwolf1"]["k"] == "comic" and "Comics & Graphic Novels" in items["h:lwolf1"]["g"]
    assert (items["h:lwolf1"]["s"], items["h:lwolf1"]["n"]) == ("Lantern Wolf", "1")
    sb = items["s:7"]
    assert (sb["t"], sb["src"], sb["b"]) == ("Arcade Worlds", "storybundle", "The Lantern Video Game Bundle")
    folder = [i for i in items.values() if i["src"] == "collections"]
    assert len(folder) == 1 and len(folder[0]["f"]) == 2 and folder[0]["col"] == "Poetry"  # EPUB+MOBI = one book
    rangers = sorted((i for i in items.values() if i.get("col") == "Neon Comics"), key=lambda i: float(i["n"]))
    assert [(i["s"], i["n"]) for i in rangers] == [("Neon Rangers", "2"), ("Neon Rangers", "8")]
    kane = [i for i in items.values() if i.get("col") == "Marshal Kane The Complete Trail Logs"][0]
    assert (kane["t"], kane["s"], kane["n"], kane.get("a")) == \
        ("Marshal Kane The Complete Trail Logs 006 (Deluxe Colour Edition)", "Marshal Kane The Complete Trail Logs", "6", None)
    help_page = helppage.help_page(tmp_path, generic=True)  # everyone's but the owner's: in words
    assert "{{" not in help_page and str(tmp_path) not in help_page and "Yours now: your own folders." in help_page
    assert "Yours now: collections, comics, ebooks (My e-books), manuals" in helppage.help_page(tmp_path)  # the owner's
    manual = [i for i in items.values() if i["src"] == "manuals"][0]
    assert manual["g"] == ["Game Manuals"] and manual["t"] == "Manual"
    ebooks = {i["t"]: i for i in items.values() if i["src"] == "ebooks"}
    keeper = ebooks["The Lantern Keeper"]  # "(Harbor Lights)" moved from the title into the series
    assert (keeper["s"], keeper["n"], keeper["a"], keeper["g"], keeper["dev"], keeper["k"]) == \
        ("Harbor Lights", "2", ["Jo Brennan"], ["Science Fiction"], "ereader", "book")
    jinx = ebooks["Velvet Jinx 1"]
    assert (jinx["k"], jinx["dev"], jinx["g"], jinx.get("a")) == ("comic", "tablet", ["Comics & Graphic Novels"], None)
    assert stats["titles"] == len(items)
    sources = page_data(tmp_path)["sources"]
    assert sources["ebooks"] == "My e-books" and sources["comics"] == "Comics" and sources["humble"] == "Humble Bundle"
    # what the page sorts and groups by, made here by the rules: the series' key and the first author's, surname first
    assert {i["sk"] for i in rangers} == {"neonrangers"} and items["h:lwolf1"]["sk"] == "lanternwolf"
    assert keeper["ak"] == "brennan jo" and novel["ak"] == "ellery marlow" and novel["sk"] == "dustandember" and "ak" not in jinx
    assert "rules" not in page_data(tmp_path)  # the page needs none of them now

    calls = []
    real = meta.read
    monkeypatch.setattr(meta, "read", lambda p, *kind: calls.append(p) or real(p, *kind))
    catalog.build(tmp_path, log=lambda s: None)
    assert calls == []  # nothing changed: everything came from the cache


def test_nas_write_errors_are_retried_and_never_cached_as_no_cover(tmp_path, monkeypatch):
    from pathlib import Path

    build_library(tmp_path)
    monkeypatch.setattr(catalog.time, "sleep", lambda s: None)
    real = Path.write_bytes
    failures = {"left": 1}

    def flaky(self, data):
        if self.suffix == ".webp" and failures["left"]:
            failures["left"] -= 1
            raise OSError(22, "Invalid argument")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    catalog.build(tmp_path, log=lambda s: None, workers=1)
    assert all(i.get("c", "").startswith("covers/") for i in data(tmp_path) if i["id"] == "h:novel")  # retried and saved

    # A cover that can't be written at all is not recorded as "no cover": the next run tries again.
    (tmp_path / "catalog").rename(tmp_path / "catalog-old")
    failures["left"] = 10**6
    catalog.build(tmp_path, log=lambda s: None, workers=1)
    cached = [json.loads(line) for line in (tmp_path / "catalog" / "cache" / "files.jsonl").read_text(encoding="utf-8").splitlines()]
    assert not [e for e in cached if e["path"].endswith("novel.epub")]  # left uncached rather than "no cover"
    failures["left"] = 0
    catalog.build(tmp_path, log=lambda s: None, workers=1)
    assert [i for i in data(tmp_path) if i["id"] == "h:novel"][0]["c"].startswith("covers/")


def test_merge_copies():
    m = catalog.merge_copies
    road = m([rec("The Lantern Road", "collection", 1, ext="mobi", a=["Jo Brennan"], col="SF Top 100", d="A novel."),
              rec("The Lantern Road", "humble", 2, a=["Jo Brennan"], b="Humble Book Bundle")])
    assert len(road) == 1 and road[0]["src"] == "humble" and len(road[0]["f"]) == 2  # the shop's copy leads
    assert road[0]["d"] == "A novel." and road[0]["cp"] == [["collection", "SF Top 100"]] and "_file" not in road[0]
    assert len(m([rec("A Maze of Mirrors", "collection", 1, a=["Mary K. Smith"]),
                  rec("A Maze of Mirrors", "collection", 2, ext="azw3", a=["Mary K Smith"])])) == 1
    # the very same file twice, under any title; Windows' "(1)" copy marker doesn't count
    assert len(m([rec("00 1234", "collection", 5, ext="pdf", trust=False), rec("00 1234", "collection", 5, ext="pdf", trust=False)])) == 1
    assert len(m([rec("Rules Digest", "manuals", 7, ext="pdf", k="manual"),
                  rec("Rules Digest(1)", "manuals", 7, ext="pdf", k="manual")])) == 1
    # kept apart: one magazine's issues (the title isn't in their file names), different maps, different series, authors
    assert len(m([rec("Monthly Games Magazine", "collection", n, ext="pdf", trust=False) for n in (1, 2, 3)])) == 3
    assert len(m([rec("Campaign Map", "manuals", n, ext="pdf", k="manual") for n in (1, 2)])) == 2
    assert len(m([rec("Velvet Jinx (2013-2016) #1", "ebooks", 1, k="comic"), rec("Velvet Jinx (2016-) #1", "ebooks", 2, k="comic")])) == 2
    assert len(m([rec("Gathered Verses", "collection", 1, a=["Ann Poet"]), rec("Gathered Verses", "collection", 2, a=["Ben Verse"])])) == 2


def test_a_card_of_copies_your_answers_joined_leads_alike_whatever_the_hash_seed(tmp_path):
    """Copies your "same" answers join, a chain of them across kinds, with copies tied in rank: the card's lead, and
    so its id, is the same in every run, whatever PYTHONHASHSEED orders a set by."""
    import os
    import subprocess
    import sys

    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); from hylly import catalog\n"
        "r = lambda i, k: {'id': i, 't': 'Moonwake', 'src': 'shelf', 'k': k, 'f': [[f'shelf/{i}.epub', 'epub', 1]], "
        "'_file': ('epub', len(i)), '_trust': True}\n"
        "recs = [r('f:aaaa', 'comic'), r('f:bbbb', 'manual'), r('f:cccc', 'book'), r('f:dddd', 'tech')]\n"
        "pairs = [('f:bbbb', 'f:cccc', 'same', {}), ('f:aaaa', 'f:bbbb', 'same', {}), ('f:cccc', 'f:dddd', 'same', {})]\n"
        "print(json.dumps([c['id'] for c in catalog.merge_copies(recs, None, pairs)]))\n")
    src = str(Path(catalog.__file__).parents[1])
    leads = set()
    for seed in range(8):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        done = subprocess.run([sys.executable, "-c", script, src], capture_output=True, text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr
        leads.add(done.stdout.strip())
    assert len(leads) == 1, leads


def test_titles():
    def item(title, stem="file", **m):
        return catalog.Item(id="f:x", source="comics", kind="comic", title="", files=[(f"comics/Some Folder/{stem}.cbz", "cbz", 1)],
                            meta={"title": title, **m})
    vol = catalog.record(item("Volume 8", series="Marshal Kane The Complete Trail Logs", number="8"), None)
    assert (vol["t"], vol["n"]) == ("Marshal Kane The Complete Trail Logs Volume 8", "8")
    assert catalog.record(item("test", stem="Quiet Hours - Chosen Poems"), None)["t"] == "Quiet Hours - Chosen Poems"
    assert catalog.record(item("test", stem="Quiet Hours - Chosen Poems"), None)["tf"]  # only the file name: Missing title
    assert "tf" not in catalog.record(item("Quiet Hours"), None)
    assert catalog.record(item("moonwake-vol-1", stem="moonwake-vol-1"), None)["tf"]  # filled in from the file name
    assert catalog.confirmed(item("The Lantern Road", stem="The Lantern Road - Jo Brennan"), "The Lantern Road")
    assert not catalog.confirmed(item("Monthly Games Magazine", stem="MGM_042_Jun_1994"), "Monthly Games Magazine")
    assert catalog.tidy_authors(["Ann Writer; Bob K. Author", "Brennan, Jo"]) == ["Ann Writer", "Bob K. Author", "Jo Brennan"]


def test_a_comma_turns_a_name_round_unless_a_suffix_or_a_company_follows():
    t = catalog.tidy_authors
    assert t(["Smith, John IV"]) == ["Smith, John IV"] and t(["Smith, John III"]) == ["Smith, John III"]  # IV as III
    assert t(["Brennan, Jo Jr."]) == ["Brennan, Jo Jr."] and t(["Acme, Inc."]) == ["Acme, Inc."]
    assert t(["Doe, Jane"]) == ["Jane Doe"]


@pytest.mark.parametrize("subfolder, name, mb, device", [
    ("Novels", "The Moon Garden.epub", 1, "ereader"),
    ("Novels", "The Lantern Keeper.epub", 30, "tablet"),  # a text this big is mostly pictures
    ("Novels", "Old Maps of Harbor.djvu", 1, "tablet"),  # pages as printed
    ("Novels", "Neon Rangers 002.cbz", 1, "tablet"),
    ("Code", "Knots in Practice.epub", 1, "either"),  # a tech book
    ("Games", "Iron Stride Field Rules.epub", 1, "tablet"),  # an RPG book
])
def test_your_own_folders_suggest_a_device_by_the_shops_rule(tmp_path, subfolder, name, mb, device):
    """Every folder's titles, with no setting of the folder's own: by the one rule for every title."""
    book = tmp_path / "shelf" / subfolder / name
    book.parent.mkdir(parents=True)
    with book.open("wb") as f:
        f.truncate(mb * MB)
    with_settings('[folders.shelf]\nkinds = { "Code" = "tech", "Games" = "rpg" }\n')
    (item,) = catalog.folder_items(tmp_path)
    assert catalog.device_of(item) == device


@pytest.mark.parametrize("inside, device", [(["keeper.epub", "keeper.pdf"], "ereader"), (["keeper.pdf"], "tablet")])
def test_a_zip_package_in_your_own_folder_goes_by_the_book_inside(tmp_path, inside, device):
    package = tmp_path / "shelf" / "Novels" / "The Lantern Keeper.zip"
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as z:
        for name in inside:
            z.writestr(name, b"not read" * (500 if name.endswith(".pdf") else 1))
    with_settings('[devices]\nimage_heavy_mb = 0.001\n')  # the ZIP is over it
    (item,) = catalog.folder_items(tmp_path)
    assert item.primary[2] > 0.001 * MB and catalog.device_of(item) == "ereader"  # big for its formats, not for its pages
    catalog.read_item(tmp_path, tmp_path / "catalog", catalog.FileCache(tmp_path / "files.jsonl"), item)
    assert len(item.meta["_inner"]) == len(inside) and catalog.device_of(item) == device


def test_the_cache_of_files_is_written_anew_once_most_of_it_is_old(tmp_path):
    path = tmp_path / "files.jsonl"
    cache = catalog.FileCache(path)
    for size in (1, 2):  # the first catalog after an update caches each file again
        for name in ("a.epub", "b.epub"):
            cache.put({"path": name, "size": size, "mtime": 0, "meta": {}})
    cache.tidy()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["size"] for x in lines] == [2, 2] and catalog.FileCache(path).data == cache.data
    cache.put({"path": "a.epub", "size": 3, "mtime": 0, "meta": {}})
    cache.tidy()  # one old line of three: it stays as it is
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3 and catalog.FileCache(path).get("a.epub", 3, 0)


def itchio_item(root, kind: str, files: list) -> catalog.Item:
    """A made-up itch.io item as the catalog finds it: in the last snapshot, with its downloaded files."""
    state = root / "state"
    (state / "itchio").mkdir(parents=True, exist_ok=True)
    (state / "itchio" / "2026-01-01_0000.json").write_text(json.dumps(
        [{"id": 1, "title": "The Lantern Keeper", "creator": "Ann Writer", "kind": kind}]), encoding="utf-8")
    manifest = Manifest(state.parent, "itchio")
    for name, mb in files:
        manifest.add({"path": f"itchio/Ann Writer/The Lantern Keeper/{name}", "ext": name.rpartition(".")[2],
                      "size": int(mb * MB), "item": 1})
    (item,) = catalog.itchio_items(root)
    return item


@pytest.mark.parametrize("kind, files, device", [
    ("book", [("keeper.epub", 1)], "ereader"),
    ("book", [("keeper.epub", 30)], "tablet"),
    ("book", [("keeper.pdf", 30), ("keeper.epub", 2)], "ereader"),  # by its main file, the EPUB, not the biggest
    ("book", [("keeper.djvu", 5)], "tablet"),
    ("book", [("keeper.zip", 40)], "ereader"),  # a ZIP package is big for its formats, not for its pages
    ("comic", [("keeper.cbz", 1)], "tablet"),
    ("rpg", [("keeper.epub", 1)], "tablet"),
])
def test_itch_io_titles_suggest_a_device_by_the_shops_rule(tmp_path, kind, files, device):
    assert catalog.device_of(itchio_item(tmp_path, kind, files)) == device


@pytest.mark.parametrize("kind, files, main", [
    ("book", [("a.pdf", 30), ("a.epub", 2)], "a.epub"),  # the e-reader's list: EPUB first, whatever the sizes
    ("book", [("a.mobi", 3), ("a.azw3", 2)], "a.azw3"),
    ("tech", [("a.cbz", 9), ("a.pdf", 30)], "a.pdf"),
    ("comic", [("a.epub", 5), ("a.cbr", 9), ("a.cbz", 3)], "a.cbz"),  # the tablet's: CBZ first
    ("comic", [("a.epub", 50), ("a.pdf", 49)], "a.pdf"),  # of PDF and EPUB the larger; about as large, the PDF
    ("rpg", [("a.epub", 2), ("a.pdf", 30)], "a.pdf"),  # an RPG book's own list: the PDF first
    ("rpg", [("a.pdf", 120), ("maps.pdf", 200)], "a.pdf"),  # of its format, the first as its shop lists them: Humble's chosen
    ("book", [("a.zip", 40), ("a.epub", 1)], "a.epub"),  # a ZIP package is on no list: an EPUB beside it comes first
    ("book", [("a.fb2", 1), ("a.pdf", 30)], "a.pdf"),  # of the files Hylly can read: an FB2 it can't
    ("book", [("a.djvu", 9), ("a.cbz", 3)], "a.cbz"),  # nor a DjVu
    ("book", [("a.djvu", 9), ("a.fb2", 3)], "a.fb2"),  # none it can read: of all of them, by the order
    ("book", [("a.zip", 40), ("readme.txt", 1)], "a.zip"),  # a ZIP package, not the readme beside it
    ("comic", [("pages.zip", 5), ("cover.png", 9)], "pages.zip"),  # nor a picture
])
def test_a_title_s_main_file_goes_by_the_order_of_its_kind(kind, files, main):
    """The one read for its details and its cover: of the format its kind is kept in first ([download] in the settings),
    as a download picks it, no longer [main_file] in the rules."""
    item = catalog.Item(id="f:x", source="shelf", kind=kind, title="",
                        files=[(f"shelf/{name}", name.rpartition(".")[2], int(mb * MB)) for name, mb in files])
    assert item.primary[0] == f"shelf/{main}"


def test_the_format_its_download_chose_is_the_main_file():
    """hylly set format's choice, as Humble's plan takes it (Item.prefer), and never an extra beside it: an RPG book
    kept as its EPUB, with a PDF of maps, keeps the EPUB as its main file."""
    files = [("a.epub", "epub", 2), ("a.pdf", "pdf", 30)]
    assert catalog.Item(id="h:x", source="humble", kind="book", title="", files=files).primary[1] == "epub"
    assert catalog.Item(id="h:x", source="humble", kind="book", title="", files=files, prefer="pdf").primary[1] == "pdf"
    assert catalog.Item(id="h:x", source="humble", kind="rpg", title="", files=files, prefer="epub").primary[1] == "epub"
    assert catalog.Item(id="h:x", source="humble", kind="rpg", title="", files=files, prefer="cbz").primary[1] == "pdf"


def test_a_change_of_the_e_reader_s_list_moves_the_main_file_too():
    item = catalog.Item(id="f:x", source="shelf", kind="book", title="", files=[("a.epub", "epub", 1), ("a.mobi", "mobi", 2)])
    assert item.primary[1] == "epub"
    with_settings('[download]\nereader = ["mobi", "epub"]\n')
    assert item.primary[1] == "mobi"


@pytest.mark.parametrize("kind, inside, device", [("book", ["keeper.pdf"], "tablet"), ("book", ["keeper.pdf", "keeper.epub"], "ereader"),
                                                  ("tech", ["keeper.pdf", "keeper.epub"], "either")])
def test_an_itch_io_zip_package_goes_by_the_book_inside(tmp_path, kind, inside, device):
    item = itchio_item(tmp_path, kind, [("keeper.zip", 40)])
    package = tmp_path / item.primary[0]
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package, "w") as z:
        for name in inside:
            z.writestr(name, b"not read")
    catalog.read_item(tmp_path, tmp_path / "catalog", catalog.FileCache(tmp_path / "files.jsonl"), item)
    assert len(item.meta["_inner"]) == len(inside) and catalog.device_of(item) == device


STORYBUNDLE_BEFORE = [  # StoryBundle's snapshot as its own flow wrote it, before the shops' core: its bundles with their books
    {"name": "The Moonwake Newsletter Bonus", "purchased": "2010-03-01", "bonus": True,
     "books": [{"id": "2", "title": "Day of the Lantern", "approx_size": 0, "cover": "https://storybundle.com/c/2.jpg"}]},
    {"name": "The Lantern Tales Bundle", "purchased": "2020-01-01", "bonus": False,
     "books": [{"id": "1", "title": "Opening the Box", "approx_size": 419430, "cover": "https://storybundle.com/c/1.jpg"},
               {"id": "2", "title": "Day of the Lantern", "approx_size": 765460, "cover": "https://storybundle.com/c/2.jpg"},
               {"id": "5", "title": "Clan Ledger", "approx_size": 524288, "cover": ""},
               {"id": "6", "title": "Quiet Engineers", "approx_size": 31457280, "cover": ""}]}]


def storybundle_library(root: Path, snapshot_data: list) -> None:
    """A made-up StoryBundle library: its newest snapshot, and four books downloaded, each in its oldest bundle."""
    (root / "state" / "storybundle").mkdir(parents=True, exist_ok=True)
    (root / "state" / "storybundle" / "2026-01-01_0000.json").write_text(json.dumps(snapshot_data), encoding="utf-8")
    manifest = Manifest(root, "storybundle")
    for item, path, mb in (("2", "The Moonwake Newsletter Bonus/Day of the Lantern/Day_of_the_Lantern.epub", 1),
                           ("1", "The Lantern Tales Bundle/Opening the Box/Opening_the_Box_-_Ann_Writer.epub", 0.4),
                           ("5", "The Lantern Tales Bundle/Clan Ledger/Clan Ledger.pdf", 0.5),
                           ("6", "The Lantern Tales Bundle/Quiet Engineers/Quiet_Engineers.zip", 30)):
        manifest.add({"path": f"storybundle/{path}", "ext": path.rpartition(".")[2], "size": int(mb * MB), "item": item, "file": 0})


def test_storybundle_s_books_are_read_alike_from_a_snapshot_of_either_shape(tmp_path):
    """The catalog reads StoryBundle's newest snapshot as the shops' core writes it (shopping.snapshot: a book once for
    each bundle it came in, its bundle as under) and as StoryBundle's own flow wrote it before (its bundles with their
    books): the same items, each book once, in its oldest bundle. Each suggests where it is best read by its main file
    now, as the other shops' books do: an EPUB on an e-reader, a PDF on a tablet, a ZIP package by its kind until the
    book inside is known (it had no device before)."""
    from hylly import shopping, storybundle

    storybundle_library(tmp_path, STORYBUNDLE_BEFORE)
    before = [vars(i) for i in catalog.reader("storybundle")(tmp_path)]
    (tmp_path / "state" / "storybundle" / "2026-01-01_0000.json").unlink()
    shopping.snapshot("storybundle", storybundle.from_snapshot(STORYBUNDLE_BEFORE), tmp_path / "state")
    items = catalog.reader("storybundle")(tmp_path)
    now = [vars(i) for i in items]
    assert now == before and [(i.id, i.bundle, i.purchased, i.bonus, catalog.device_of(i)) for i in items] == [
        ("s:2", "The Moonwake Newsletter Bonus", "2010-03-01", True, "ereader"), ("s:1", "The Lantern Tales Bundle", "2020-01-01", False, "ereader"),
        ("s:5", "The Lantern Tales Bundle", "2020-01-01", False, "tablet"), ("s:6", "The Lantern Tales Bundle", "2020-01-01", False, "ereader")]
    assert now[0]["cover_url"] == "https://storybundle.com/c/2.jpg" and now[0]["title"] == "Day of the Lantern"


@pytest.mark.parametrize("inside, device", [(["engineers.pdf"], "tablet"), (["engineers.pdf", "engineers.epub"], "ereader")])
def test_a_storybundle_zip_package_goes_by_the_book_inside(tmp_path, inside, device):
    """A StoryBundle book that comes as a ZIP package of several formats is best read where the book inside it is, once
    that is known, as itch.io's: never by the package's own size."""
    storybundle_library(tmp_path, STORYBUNDLE_BEFORE)
    item = next(i for i in catalog.reader("storybundle")(tmp_path) if i.id == "s:6")
    package = tmp_path / item.primary[0]
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package, "w") as z:
        for name in inside:
            z.writestr(name, b"not read")
    catalog.read_item(tmp_path, tmp_path / "catalog", catalog.FileCache(tmp_path / "files.jsonl"), item)
    assert len(item.meta["_inner"]) == len(inside) and catalog.device_of(item) == device


def test_an_itch_io_zip_package_beside_a_readme_is_read_and_offered_as_before(tmp_path):
    """itch.io keeps every file of an item: the ZIP package is its main file, not the readme beside it, and the EPUB
    inside it is listed, and offered by the e-reader feed, as before the order of [download]."""
    from hylly.library import Device

    item = itchio_item(tmp_path, "book", [("keeper.zip", 40), ("readme.txt", 1)])
    package = tmp_path / item.primary[0]
    package.parent.mkdir(parents=True)
    inner = tmp_path / "inner.epub"
    make_epub(inner)
    with zipfile.ZipFile(package, "w") as z:
        z.write(inner, "The Lantern Keeper.epub")
    (package.parent / "readme.txt").write_text("Thanks for reading", encoding="utf-8")
    catalog.read_item(tmp_path, tmp_path / "catalog", catalog.FileCache(tmp_path / "files.jsonl"), item)
    rec = catalog.record(item, None)
    assert item.primary[0].endswith("keeper.zip") and rec["t"] == "The Lantern Keeper" and rec["z"][0][1] == "epub"
    assert Device().best_file(rec)[0].endswith("The%20Lantern%20Keeper.epub")


def test_an_itch_io_zip_of_pictures_beside_a_larger_picture_is_read_as_its_pages(tmp_path):
    item = itchio_item(tmp_path, "comic", [("pages.zip", 5), ("cover.png", 9)])
    package = tmp_path / item.primary[0]
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package, "w") as z:
        for n in range(10):
            z.writestr(f"{n:02}.png", png())
    catalog.read_item(tmp_path, tmp_path / "catalog", catalog.FileCache(tmp_path / "files.jsonl"), item)
    assert item.primary[0].endswith("pages.zip") and item.meta["_pics"] and item.meta["_cover"]
    assert item.meta["source"] != "unreadable"


def humble_title(root, kind_bundle: str, offered: list, there: dict, formats: dict | None = None, monkeypatch=None):
    """A made-up Humble title, "lw", as the catalog finds it: offered (label, file, MB), and there, the files downloaded
    by name with what their record says more (a role, instead_of); on disk too when given bytes or a writer."""
    from hylly import curation

    orders = {"K1": order("K1", "2024-01-01T10:00:00", kind_bundle, [sub("lw", "Lantern Wolf Vol. 1", "Gloamlight Comics", offered)])}
    (root / "state" / "humble").mkdir(parents=True, exist_ok=True)
    (root / "state" / "humble" / "2026-01-01_0000.json").write_text(json.dumps(snapshot.sanitize(orders)), encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setattr(curation, "formats", lambda root: formats or {})
    (t,) = plan.build(orders).titles
    record = Manifest(root, "humble")
    for name, (more, made) in there.items():
        rel = f"{t.folder}/{name}"
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        made(f) if callable(made) else f.write_bytes(made)
        record.add({"path": rel, "shop_id": "lw", "size": f.stat().st_size, "ext": name.rpartition(".")[2], "md5": "x",
                    **({"role": "book"} | more)})
    return t


def pictures(path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("01.png", png())


def test_a_humble_comic_keeps_the_details_of_its_epub_and_never_reads_a_pdf_for_them(tmp_path, monkeypatch):
    """Its main file is the CBZ, by the tablet's order, and a CBZ without ComicInfo has no description: the same book
    as an EPUB gives what the CBZ lacks (catalog.fill_details), read once while it stays the same. A PDF, which gives
    none of it, is never read for it, nor the extra."""
    humble_title(tmp_path, "Humble Comics Bundle: Lantern Wolf",
                 [("CBZ", "lw.cbz", 1), ("EPUB", "lw.epub", 1), ("PDF", "lw.pdf", 1), ("Maps (PDF)", "maps.pdf", 1)],
                 {"lw.cbz": ({}, pictures), "lw.epub": ({}, make_epub), "lw.pdf": ({}, b"%PDF-1.4"),
                  "maps.pdf": ({"role": "extra"}, b"%PDF-1.4")})
    pdfs = []
    monkeypatch.setattr(meta, "read_pdf", lambda path, *a: pdfs.append(path.name) or meta.Meta(source="pdf"))
    (item,) = catalog.humble_items(tmp_path)
    assert item.kind == "comic" and item.primary[1] == "cbz" and item.one_book
    for _ in range(2):  # the second time from the caches alone
        catalog.build(tmp_path, log=lambda s: None)
        (rec,) = data(tmp_path)
        assert (rec["d"], rec["s"], rec["n"], rec["p"], rec["isbn"]) == (  # the publisher Humble names, as ever
            "A post-apocalyptic novella .", "Dust and Ember", "1", "Gloamlight Comics", "9780000000002")
        assert pdfs == []
        monkeypatch.setattr(meta, "read", lambda *a: pytest.fail("read again"))


def test_another_book_beside_an_itch_io_item_lends_it_nothing(tmp_path, monkeypatch):
    """A folder of itch.io's, DriveThruRPG's, StoryBundle's or yours may hold other books: only a Humble title's book
    files are one book. The item keeps its shop's own text."""
    item = itchio_item(tmp_path, "rpg", [("rules.pdf", 30), ("companion.epub", 2)])
    item.description, item.meta = "The item's own words", {"title": "Rules"}
    monkeypatch.setattr(meta, "read", lambda *a: pytest.fail("read"))
    catalog.fill_details(tmp_path, catalog.FileCache(tmp_path / "a.jsonl"), catalog.FileCache(tmp_path / "b.jsonl"), item)
    rec = catalog.record(item, None)
    assert rec["d"] == "The item's own words" and "s" not in rec and "isbn" not in rec and not item.one_book


def test_a_rebuild_reads_the_other_files_for_their_details_again_too(tmp_path, capsys):
    humble_title(tmp_path, "Humble Comics Bundle: Lantern Wolf", [("CBZ", "lw.cbz", 1), ("EPUB", "lw.epub", 1)],
                 {"lw.cbz": ({}, pictures), "lw.epub": ({}, make_epub)})
    catalog.build(tmp_path, log=lambda s: None)
    details = tmp_path / "catalog" / "cache" / "details.jsonl"
    older = json.loads(details.read_text(encoding="utf-8").splitlines()[0])
    details.write_text(json.dumps({**older, "meta": {"description": "An older reading"}}) + "\n", encoding="utf-8")
    catalog.build(tmp_path, log=lambda s: None)
    assert data(tmp_path)[0]["d"] == "An older reading"  # as the cache has it
    cli.main(["catalog", "--rebuild", "--dry-run", "--library", str(tmp_path)])
    assert "the caches of the files' details set aside first" in capsys.readouterr().out and details.is_file()
    cli.main(["catalog", "--rebuild", "--library", str(tmp_path)])
    assert data(tmp_path)[0]["d"] == "A post-apocalyptic novella ."


def test_a_rebuild_moves_nothing_till_it_can_build_and_puts_it_all_back_unless_its_build_is_kept(tmp_path, monkeypatch, capsys):
    """--rebuild reads every file again, as the covers and the caches of the files' details are set aside, not deleted:
    only once the library's records are open and the rules read, so that a computer that doesn't keep the records, or a
    mistake in the rules, leaves them as they were. A build that isn't kept (stopped, refused, failed) puts them back,
    as the page still goes by the last build; one that is kept removes them."""
    from hylly import db, rules

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    cat = tmp_path / "catalog"
    (cat / "covers" / "made-up.webp").write_bytes(b"a cover no title has now")
    (cat / "cache" / "details.jsonl").write_text('{"path": "made-up.epub"}\n', encoding="utf-8")

    def as_it_is() -> dict[str, bytes]:
        return {p.relative_to(cat).as_posix(): p.read_bytes() for p in cat.rglob("*")
                if p.is_file() and (p.parent.name.startswith("covers") or p.name.startswith(("files.jsonl", "details.jsonl")))}

    before = as_it_is()
    assert "covers/made-up.webp" in before and "cache/files.jsonl" in before
    rebuild = ["catalog", "--rebuild", "--library", str(tmp_path)]

    def not_here(root):
        raise db.NotHere("The library's records are kept in the database on another computer, not in this one.")

    def wrong():
        raise exits.Exit(exits.USAGE, "A made-up mistake in the rules.")

    def stopped(*a, **k):
        raise KeyboardInterrupt

    for name, stand_in in (("store", not_here), ("rules", wrong), ("read", stopped)):
        with monkeypatch.context() as m:
            m.setattr(*{"store": (catalog, "store"), "rules": (rules, "current"), "read": (meta, "read")}[name], stand_in)
            with pytest.raises((SystemExit, KeyboardInterrupt, db.NotHere)):
                cli.main(rebuild)
        assert as_it_is() == before, name
    with monkeypatch.context() as m:  # far fewer titles, and nobody to say keep it: not kept
        m.setattr(catalog, "shrunk", lambda *a, **k: "This build has far fewer titles than the last one.")
        with pytest.raises(SystemExit):
            cli.main(rebuild)
    assert as_it_is() == before
    cli.main(rebuild)
    after = as_it_is()
    assert "covers/made-up.webp" not in after and not [p for p in after if ".old" in p]
    assert after["cache/files.jsonl"] and [p for p in after if p.startswith("covers/")]
    assert all((cat / rec["c"]).is_file() for rec in data(tmp_path) if rec.get("c", "").startswith("covers/"))


def test_a_stop_while_a_rebuild_sets_things_aside_puts_them_back_and_a_plain_build_removes_what_a_hard_stop_left(
        tmp_path, monkeypatch, capsys):
    """Ctrl-C between setting the covers aside and the caches leaves neither aside: the page's covers are back. What a
    rebuild ended by a power cut set aside, a plain build makes whole again and then removes, and says so once."""
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    cat = tmp_path / "catalog"
    before = sorted(p.relative_to(cat).as_posix() for p in cat.rglob("*") if p.is_file())
    rename = os.rename

    def stopped(src, dst, *a, **k):
        if str(src).endswith("files.jsonl"):
            raise KeyboardInterrupt
        return rename(src, dst, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(os, "rename", stopped)
        with pytest.raises((KeyboardInterrupt, SystemExit)):
            cli.main(["catalog", "--rebuild", "--library", str(tmp_path)])
    assert sorted(p.relative_to(cat).as_posix() for p in cat.rglob("*") if p.is_file()) == before
    covers = len(list((cat / "covers").iterdir()))
    (cat / "covers").rename(cat / "covers.old")  # as a rebuild cut off by a power cut leaves them
    (cat / "cache" / "files.jsonl").rename(cat / "cache" / "files.jsonl.old")
    capsys.readouterr()
    cli.main(["catalog", "--library", str(tmp_path)])
    said = capsys.readouterr().err
    assert "Removed catalog/covers.old, catalog/cache/files.jsonl.old: set aside by a catalog --rebuild that never ended" in said
    assert len(list((cat / "covers").iterdir())) == covers and not [p for p in cat.rglob("*.old")]
    cli.main(["catalog", "--library", str(tmp_path)])
    assert "never ended" not in capsys.readouterr().err


def test_the_main_file_is_never_an_extra(tmp_path, monkeypatch):
    """When the chosen file isn't there, the title's book files: the one standing in for it first (its
    record's instead_of), else by the order; an extra only when it has no book file at all."""
    t = humble_title(tmp_path, "Humble RPG Bundle: Iron Stride", [("PDF", "rules.pdf", 50), ("EPUB", "rules.epub", 20),
                                                                  ("Maps (PDF)", "maps.pdf", 90)],
                     {"rules.epub": ({}, b"PK"), "maps.pdf": ({"role": "extra"}, b"%PDF")})
    assert (t.kind, t.choice.ext) == ("rpg", "pdf")
    record = Manifest(tmp_path, "humble")
    record.add({**record.get(f"{t.folder}/rules.epub"), "instead_of": t.path})  # the PDF failed: the EPUB stands in
    (item,) = catalog.humble_items(tmp_path)
    assert item.primary[0].endswith("rules.epub") and item.prefer == "epub"  # the EPUB that came in the PDF's place


def test_the_main_file_is_the_book_there_while_the_chosen_format_isn_t(tmp_path, monkeypatch):
    humble_title(tmp_path, "Humble Book Bundle: Moonwake Press", [("EPUB", "novel.epub", 2), ("PDF", "novel.pdf", 6),
                                                                  ("Bonus (PDF)", "bonus.pdf", 9)],
                 {"novel.epub": ({}, b"PK"), "bonus.pdf": ({"role": "extra"}, b"%PDF")}, formats={"lw": "pdf"},
                 monkeypatch=monkeypatch)
    (item,) = catalog.humble_items(tmp_path)
    assert item.prefer == "" and item.primary[0].endswith("novel.epub")  # hylly set format pdf: the PDF not there yet
    only_extra = catalog.Item(id="h:x", source="humble", kind="book", title="", files=[("b/bonus.pdf", "pdf", 1)],
                              extras={"b/bonus.pdf"}, one_book=True)
    assert only_extra.primary[0] == "b/bonus.pdf"  # no book file at all: the extra, rather than nothing


def test_genres():
    g = catalog.genres_of
    assert g("FICTION / Science Fiction / General") == ["Science Fiction"]
    assert "Tabletop RPG" in g("", "Humble RPG Bundle: Iron Stride")
    assert "Science & Math" in g("Physics", "Humble Book Bundle: STEM by Moonwake Press")
    assert "History" not in g("Starship Wars")
    assert g("The Complete Outdoors Manual") == ["Home, Garden & Outdoors"]
    assert g("The Necromancer") == [] and g("Harbor Classics") == ["Classics"]
    assert g("Classic BattleTech") == ["Tabletop RPG"]
    assert g("Tales in the Lovecraftian Style") == ["Horror"]  # whole words: "craft" inside a name is no craft
    assert g("Muñequitos y tiras cómicas") == ["Comics & Graphic Novels"]  # accents don't count
    assert "Science Fiction" in g("Time-Travel Stories") and g("Sci-Fi Shorts") == ["Science Fiction"]
    assert g("Garden Notes", "Humble Book Bundle: Moonwake | Code") == ["Home, Garden & Outdoors"]  # not across texts


def test_help_lists_every_command_and_its_links_work(tmp_path):
    import re

    from hylly import cli

    page = helppage.help_page(tmp_path)
    commands = [a for a in cli.build_parser()._actions if a.dest == "command"][0].choices
    assert "<!--commands-->" not in page and all(f'id="cmd-{name}"' in page for name in commands)
    assert "hylly server autostart on" in page and "hylly set" in page and 'id="cmd-tag-suggest"' in page  # each action its own
    ids = set(re.findall(r'id="([^"]+)"', page))
    assert all(link in ids for link in re.findall(r'href="#([^"]+)"', page))  # every contents and "More" link has its section


def test_the_help_for_everyone_but_the_owner_names_no_folder_of_this_computer(tmp_path):
    from hylly import rules

    copy = helppage.help_page(tmp_path, generic=True)  # what the server sends anyone but the owner
    here = [str(tmp_path), str(Path(catalog.__file__).resolve().parents[2]), str(paths.settings_file().parent),
            str(paths.config_dir()), str(rules.folder()), str(Path.home())]
    assert "{{" not in copy and not [folder for folder in here if folder in copy]
    assert "settings.toml (in the settings folder)" in copy and 'id="cmd-catalog"' in copy  # in words, every command still there
    owners = helppage.help_page(tmp_path)  # what the server sends the owner
    from hylly import db

    assert str(tmp_path) in owners and str(paths.settings_file()) in owners and str(db.file()) in owners


def test_the_owners_help_has_the_port_the_server_listens_on_and_the_address_people_use(tmp_path):
    """The addresses the owner's help shows (the Server page's, the feeds', Tailscale's) have the server's own
    port: the running server's, as its note says, since --port wins over the settings; else [server] port. The copy in
    the library has the port as it comes, 8770, and no address."""
    from hylly import instance

    generic = helppage.help_page(tmp_path, generic=True)
    assert "http://127.0.0.1:8770/server" in generic and "tailscale serve --bg 8770 " in generic
    assert "{{" not in generic and "The address people use, as this server has it: shown to owners in this page." in generic
    paths.settings_file().write_text('[server]\nport = 8790\naddress = "https://books.example.org/"\n', encoding="utf-8")
    owners = helppage.help_page(tmp_path)
    assert "http://127.0.0.1:8790/server" in owners and "http://192.0.2.10:8790/opds" in owners
    sections = owners[owners.index('<section id="running">'):]  # the sections, past the head and the contents
    assert "tailscale funnel --bg 8790 " in owners and ":8770/" not in sections
    assert "The address people use, as this server has it: https://books.example.org." in owners
    instance.info_file().parent.mkdir(parents=True, exist_ok=True)
    instance.info_file().write_text(json.dumps({"pid": 1, "port": 8814}), encoding="utf-8")  # a server on --port 8814
    owners = helppage.help_page(tmp_path)
    assert "http://127.0.0.1:8814/server" in owners and ":8790/" not in owners and "funnel --bg 8814 " in owners
    instance.info_file().write_text("{half a note", encoding="utf-8")  # being written: the settings' port meanwhile
    assert "http://127.0.0.1:8790/server" in helppage.help_page(tmp_path)


def test_the_catalog_folder_keeps_what_the_server_reads_and_no_page_of_its_own(tmp_path):
    """The page comes from the server alone: a build writes catalog/data.js, the titles for the server to read, with no
    rules in it, and takes away what earlier versions left there for opening the page from the folder."""
    from hylly import shops

    folder = tmp_path / "catalog"
    folder.mkdir()
    for name in ("index.html", "help.html", "tags.js"):
        (folder / name).write_text("left by an earlier version", encoding="utf-8")
    (folder / "covers").mkdir()
    items = [{"id": "f:1", "t": "Moonwake", "k": "book", "s": "Moonwake", "sk": "moonwake"}]
    catalog.write_page(folder, items, generated="2026-11-14T12:00:00")
    assert sorted(p.name for p in folder.iterdir()) == ["covers", "data.js"]
    text = (folder / "data.js").read_text(encoding="utf-8")
    assert json.loads(text[text.index("{"): text.rindex("}") + 1]) == {
        "generated": "2026-11-14T12:00:00", "sources": shops.labels(), "items": items}


def test_a_shop_cover_is_kept_only_as_an_https_address_with_a_host():
    """What a shop gives as a cover is kept only when it is an https address with a host, as the server's policy
    names the covers' sites (library.COVER_SITE, which the catalog checks by): http to a device at home, a file: or a
    share's address, or none with no host is no cover, so the page never asks for it."""
    from hylly import library

    assert catalog.COVER_SITE is library.COVER_SITE  # one pattern, the server's
    kept = ["https://img.example.net/a.png?w=1", "https://IMG.example.net/b.png", "https://art.example.org:8443/c.png",
            "https://img.example.net"]
    gone = ["http://127.0.0.1/cover.png", "http://192.168.1.20/cover.png", "http://plain.example/d.png",
            "file:///C:/Users/Public/cover.png", "file://other-pc/share/cover.png", "\\\\other-pc\\share\\cover.png",
            "//no-scheme.example/f.png", "https://who@hidden.example/e.png", "https:///cover.png", "ftp://files.example/c.png",
            "covers/0123456789abcdef.webp", "", None]
    assert [catalog.shop_cover(url) for url in kept] == kept and {catalog.shop_cover(url) for url in gone} == {""}
    item = catalog.Item(id="s:7", source="storybundle", kind="book", title="Moonwake Tide",
                        files=[("storybundle/Moonwake Tide.epub", "epub", 10)], cover_url="http://192.168.1.20/cover.png")
    assert "c" not in catalog.record(item, None)
    item.cover_url = kept[0]
    assert catalog.record(item, None)["c"] == kept[0]


def test_games_of_rpg_titles():
    def rpg(kind="rpg", **kw):
        return catalog.Item(id="x", source="humble", kind=kind, title="", files=[], **kw)

    bundle = "Humble RPG Bundle: Traveller Starter Shelf"
    assert catalog.games_of(rpg(bundle=bundle), "The Far Stars Sourcebook", bundle) == ["Traveller"]  # the bundle names it
    assert catalog.games_of(rpg(), "Grave Psalms for Mork Borg") == ["Mörk Borg"]
    assert catalog.games_of(rpg(), "Pocket Picnic: A Game of Ants") == []  # a game of its own, with no name to go by
    assert catalog.games_of(rpg(kind="book"), "Stories of the Dune Sea") == []  # fiction, not the game
    assert catalog.games_of(rpg(kind="book"), "Starfinder: Night Market", kind="rpg") == ["Starfinder"]  # you made it an RPG title
    # the shop's own names win, shown as the filter names them; one it doesn't know stays as it is
    assert catalog.games_of(rpg(games=["Mothership 1e", "Ironsworn"]), "Traveller Missions") == ["Mothership", "Ironsworn"]


def test_drivethrurpg_games_from_titles_descriptions_and_bundles():
    def p(i, name, games=(), description="", bundle=0):
        return {"id": str(i), "name": name, "games": list(games), "description": description, "bundle": bundle}

    found = catalog.shop_games([
        p(1, "Far Stars Core", ["Traveller 2e"], bundle=7), p(2, "Starport Guide", bundle=7),  # the bundle's game
        p(3, "Night Shift", description="Five scenarios for Delta Green agents"),
        p(4, "Mixed Bag One", ["Mothership 1e"], bundle=8), p(5, "Mixed Bag Two", ["Ironsworn"], bundle=8),
        p(6, "Mixed Bag Three", bundle=8),  # the bundle's games disagree: none
        p(7, "Lone Title", description="A tale of paranoia and dune buggies"),  # ordinary words in prose
    ])
    assert found == {"1": ["Traveller"], "2": ["Traveller"], "3": ["Delta Green"], "4": ["Mothership"], "5": ["Ironsworn"],
                     "6": [], "7": []}


def test_any_folder_of_your_own_is_a_source_and_its_settings_say_more(tmp_path):
    book = tmp_path / "shelf" / "Folklore & Myth" / "Hedge Lore.epub"
    book.parent.mkdir(parents=True)
    make_epub(book)
    (tmp_path / "state").mkdir()
    (tmp_path / "_ToBeRemoved" / "old").mkdir(parents=True)  # not a source: Hylly's own, as is anything starting with _
    make_epub(tmp_path / "_ToBeRemoved" / "old" / "Gone.epub")
    catalog.build(tmp_path, log=lambda s: None)
    (rec,) = data(tmp_path)
    assert (rec["src"], rec["k"], rec["col"], rec["dev"]) == ("shelf", "book", "Folklore & Myth", "ereader")  # every title's
    with_settings('[folders.shelf]\nlabel = "Second shelf"\ngenres = true\n'
                  'kinds = { "Folklore & Myth" = "tech" }\n')
    catalog.build(tmp_path, log=lambda s: None)
    (rec,) = data(tmp_path)
    assert (rec["src"], rec["g"], rec["dev"], rec["k"]) == ("shelf", ["Folklore & Myth"], "either", "tech")  # as the shops' tech books


def test_a_book_outside_a_subfolder_of_a_genre_folder_gets_its_genre_by_its_words_never_a_nameless_one(tmp_path):
    """With genres = true the first subfolder is the genre: the one in the rules its words find, else its own
    name. A book in the folder itself has no subfolder: it gets its genre as any title does, by its words, where it got
    the genre "" before, an empty row among the genres in the page and the feeds."""
    for where, name in (("", "Quiet Hours"), ("Dystopias", "Hedge Lore"), ("Folklore & Myth", "Lantern Road")):
        book = tmp_path / "shelf" / where / f"{name}.epub"
        book.parent.mkdir(parents=True, exist_ok=True)
        make_epub(book, OPF.replace("The Long Dust", name).replace("978-0-00-000000-2", ""))  # three books, not copies
    (tmp_path / "state").mkdir()
    with_settings('[folders.shelf]\ngenres = true\n')
    catalog.build(tmp_path, log=lambda s: None)
    genres = {r["t"]: r.get("g") for r in data(tmp_path)}
    assert genres == {"Quiet Hours": ["Science Fiction"],  # by its subjects (FICTION / Science Fiction), as any title
                      "Hedge Lore": ["Science Fiction"],  # the folder's words find a genre of the rules: that one
                      "Lantern Road": ["Folklore & Myth"]}  # they find none: the folder's name


def test_whose_titles_give_no_genre_is_a_rule_none_as_shipped():
    """A title's own words help find its genre at every source but those the rules' [genres] titles_unread
    names: none, as shipped, as words go by whole words. A rules file of your own can leave a shop's or a folder's out,
    by its name in any case, and take one back; and a titles_unread that isn't a list stops, with where it is."""
    def genres(source: str) -> list[str] | None:
        item = catalog.Item(id="x", source=source, kind="book", title="Moonwake Cookbook", files=[("x/b.epub", "epub", 1)])
        return catalog.record(item, None).get("g")

    from hylly import rules

    garden = ["Home, Garden & Outdoors"]
    assert [genres(s) for s in ("humble", "storybundle", "Shelf")] == [garden, garden, garden]
    rules_folder = paths.settings_file().parent / "rules"
    rules_folder.mkdir()
    (rules_folder / "mine.toml").write_text('[genres]\ntitles_unread_add = ["humble", "shelf"]\n', encoding="utf-8")
    rules.reset()
    assert [genres(s) for s in ("humble", "storybundle", "Shelf")] == [None, garden, None]
    (rules_folder / "theirs.toml").write_text('[genres]\ntitles_unread_drop = ["Humble"]\n', encoding="utf-8")
    rules.reset()
    assert [genres(s) for s in ("humble", "storybundle", "Shelf")] == [garden, garden, None]
    (rules_folder / "theirs.toml").unlink()
    (rules_folder / "mine.toml").write_text('[genres]\ntitles_unread = "humble"\n', encoding="utf-8")
    rules.reset()
    with pytest.raises(SystemExit, match=r"mine\.toml, \[genres\] titles_unread: a list of words in quotes"):
        rules.current()


def test_every_text_of_a_record_is_cleaned_once_so_every_feed_s_xml_can_hold_it():
    """What a file, a shop or a lookup gave is cleaned once, in record (one_line): a control character dropped,
    as XML 1.0 holds none of most of them (one in a title made every feed listing it invalid), but one that is white
    space a space; the marks that set or turn the text's direction, the tags and the two noncharacters dropped; a run
    of white space one space; the ends trimmed. What a script needs stays (the next test)."""
    import re

    item = catalog.Item(id="s:7", source="storybundle", kind="book", title="Moonwake\x00 Tide\u202e",
                        files=[("storybundle/b.epub", "epub", 1)], bundle="The\tLantern\nBundle",
                        purchased="2026-01-01\x07", publisher="Moonwake\u200e  Press",
                        subjects=["Sea\x1bStories", "Sea\x01Stories", "Harbor  Life"],
                        meta={"authors": ["Ann\x01  Writer", "Brennan, Jo"], "series": "Harbor\x85Lights", "number": "2\u200f",
                              "description": "One\x0bline.\u2029Two\x9b. <b>Bold\x02</b>\ufffe"})
    rec = catalog.record(item, {"subjects": ["Lighthouse\u2066 Keepers\u2069"], "isbn": "978\x00-0-00-000000-2\uffff"})
    assert (rec["t"], rec["a"], rec["s"], rec["n"], rec["p"]) == ("Moonwake Tide", ["Ann Writer", "Jo Brennan"],
                                                                 "Harbor Lights", "2", "Moonwake Press")
    assert (rec["b"], rec["dt"], rec["isbn"], rec["d"]) == ("The Lantern Bundle", "2026-01-01", "978-0-00-000000-2",
                                                           "One line. Two. Bold")
    assert rec["sub"] == ["SeaStories", "Harbor Life", "Lighthouse Keepers"]  # two of them one, once cleaned
    unsafe = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufffe\uffff"
                        "\U000e0000-\U000e007f]")
    assert not unsafe.search(json.dumps(rec, ensure_ascii=False))
    assert catalog.one_line("Moonwake Tide") == "Moonwake Tide" and catalog.one_line(2026) == 2026  # as they were


@pytest.mark.parametrize("given, cleaned", [
    ("\u0e2b\u0e19\u0e31\u0e07\u0e2a\u0e37\u0e2d\u200b\u0e17\u0e30\u0e40\u0e25", None),  # Thai: words a zero-width space apart
    ("Lantern Wolf\u202f: tome 2", None),  # French: a narrow no-break space before the colon
    ("\u706f\u7c60\u306e\u72fc\u3000\u7b2c\u4e8c\u5dfb", None),  # Japanese: an ideographic space
    ("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645", None),  # Persian: a zero-width non-joiner
    ("\U0001f469\u200d\U0001f467 Moon\u00adwake\u2060Tide\u180e", None),  # a joiner, a soft hyphen, a word joiner, U+180E
    ("\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f Harbor", "\U0001f3f4 Harbor"),  # a flag's tags
    ("Moonwake \u202eediT ehT", "Moonwake ediT ehT"),  # a right-to-left override
    ("Lantern\u00a0\u00a0Wolf\t\u3000Tales", "Lantern Wolf Tales"),  # a run of white space of any kind is one space
])
def test_a_record_s_text_keeps_what_its_script_needs_and_loses_only_what_turns_or_hides_it(given, cleaned):
    """one_line takes out only what is harmful: the zero-width space between Thai words, the joiners, the word
    joiner, the soft hyphen and a single no-break, narrow no-break or ideographic space are the text's own; a mark that
    turns it round or a tag that hides text in it isn't."""
    assert catalog.one_line(given) == (given if cleaned is None else cleaned)
    item = catalog.Item(id="s:7", source="storybundle", kind="book", title=given, files=[("storybundle/b.epub", "epub", 1)])
    assert catalog.record(item, None)["t"] == (given if cleaned is None else cleaned)


def test_a_title_cleaned_to_nothing_is_untitled_with_its_id_and_the_build_goes_on(tmp_path):
    """A file whose own title and name are only white space or marks (" .epub", or direction marks alone) has a
    title of none once cleaned, which stopped every build (merge_copies goes by every card's title): it is Untitled,
    with its id, so that two such are two cards, and the page's Missing filter lists it."""
    for where, name in (("One", " .epub"), ("Two", "\u200e\u202e.epub")):  # in a folder each: one name, as words go
        (tmp_path / "shelf" / where).mkdir(parents=True)
        make_epub(tmp_path / "shelf" / where / name, OPF.replace("<dc:title>The Long Dust</dc:title>", "<dc:title> </dc:title>")
                  .replace("978-0-00-000000-2", ""))
    (tmp_path / "state").mkdir()
    catalog.build(tmp_path, log=lambda s: None)
    recs = data(tmp_path)
    assert sorted(r["t"] for r in recs) == sorted(f"Untitled ({r['id']})" for r in recs) and len(recs) == 2
    assert all(r["tf"] for r in recs)


def test_a_name_read_in_the_wrong_encoding_is_mended_before_it_is_cleaned():
    """meta.mend reads the C1 characters that UTF-8 read as Windows-1252 or Latin-1 leaves back as the letters
    they were; cleaned before, "\u00c3\x81ngel" would lose its \u00c1 for good. Authors and descriptions are mended."""
    for letter in "\u00c1\u00cd\u00cf\u00d0\u00dd\u0142\u010d\u0151":
        misread = letter.encode("utf-8").decode("latin-1")  # "\u00c3\x81" for \u00c1
        assert catalog.tidy_authors([f"{misread}ngel Writer"]) == [f"{letter}ngel Writer"], letter
    item = catalog.Item(id="s:7", source="storybundle", kind="book", title="Moonwake Tide",
                        files=[("storybundle/b.epub", "epub", 1)],
                        meta={"authors": ["\u00c3\x81ngel Writer"], "description": "\u00c3\x81ngel's tale."})
    rec = catalog.record(item, {"authors": ["Ann Writer"]})
    assert (rec["a"], rec["d"]) == (["\u00c1ngel Writer"], "\u00c1ngel's tale.")


def test_an_author_or_a_series_of_only_marks_leaves_room_for_the_lookup_s():
    """Authors and series are cleaned before a lookup fills what the file left empty: a name of only marks is
    none, and the lookup's is taken."""
    item = catalog.Item(id="s:7", source="storybundle", kind="book", title="Moonwake Tide",
                        files=[("storybundle/b.epub", "epub", 1)],
                        meta={"authors": ["\u200e\u202e"], "series": "\u2066\u2069", "number": "2"})
    rec = catalog.record(item, {"authors": ["Ann Writer"], "series": "Harbor Lights", "number": "3"})
    assert (rec["a"], rec["s"], rec["n"]) == (["Ann Writer"], "Harbor Lights", "3")


def test_a_folder_s_kinds_name_its_subfolders_in_any_case_and_an_entry_naming_none_is_told_once(tmp_path, capsys):
    """kinds names a subfolder as Windows and macOS see its name, capitals and Unicode form aside, where "Graphic
    novels" stayed books before; an entry that names no subfolder there, a name written wrong say, is told once however
    often the folders are read."""
    import unicodedata

    from hylly import folders

    for folder, name in (("Graphic novels", "Moonwake.epub"), ("Novels", "Lantern Road.epub")):
        (tmp_path / "shelf" / folder).mkdir(parents=True)
        make_epub(tmp_path / "shelf" / folder / name)
    with_settings('[folders.shelf]\nkinds = { "Graphic Novels" = "comic", "Programing" = "tech" }\n')
    kinds = {item.collection: item.kind for item in catalog.folder_items(tmp_path)}
    assert kinds == {"Graphic novels": "comic", "Novels": "book"}
    catalog.folder_items(tmp_path)  # read again, as a build does several times
    err = capsys.readouterr().err
    assert err.count("[folders.shelf] kinds: 'Programing' names no subfolder of shelf in the library") == 1
    assert "Graphic" not in err and "Check how the subfolder is spelt" in err
    shelf = folders.Folder("shelf", "Shelf", kinds={"Bandes Dessinées": "comic"})
    assert shelf.kind_of(unicodedata.normalize("NFD", "bandes dessinées")) == "comic" and shelf.kind_of("Other") == "book"


def test_a_folder_s_kinds_written_as_a_text_stops_with_its_section_and_key(tmp_path):
    """kinds = "comic", an easy slip for kind, stops the build with the section and the key and what to write,
    as a wrong kind does, never with a Python traceback."""
    (tmp_path / "shelf").mkdir()
    make_epub(tmp_path / "shelf" / "Moonwake.epub")
    with_settings('[folders.shelf]\nkinds = "comic"\n')
    with pytest.raises(SystemExit) as stopped:
        catalog.folder_items(tmp_path)
    assert "[folders.shelf] kinds = 'comic': a table in braces, such as" in str(stopped.value)


def test_every_title_gets_its_device_once_by_its_kind_your_correction_too(tmp_path):
    """Where a title is best read is worked out once, after it is read, by one function, for every title of every
    folder (no setting of its own), and by the kind you chose for it at the next build; its kind in the catalog stays
    the one found, which the server lays yours over."""
    from hylly import curation

    for name in ("Hedge Lore", "Moon Garden"):
        book = tmp_path / "shelf" / "Folklore" / f"{name}.epub"
        book.parent.mkdir(parents=True, exist_ok=True)
        make_epub(book, OPF.replace("The Long Dust", name).replace("978-0-00-000000-2", ""))  # two books, not two copies
    (tmp_path / "state").mkdir()
    catalog.build(tmp_path, log=lambda s: None)
    recs = {r["f"][0][0].rpartition("/")[2]: r for r in data(tmp_path)}
    assert [(r["k"], r["dev"]) for r in recs.values()] == [("book", "ereader")] * 2
    hedge = recs["Hedge%20Lore.epub"]
    curation.change_single(tmp_path, "kind", "rpg", [hedge], by=curation.COMMAND_LINE)
    catalog.build(tmp_path, log=lambda s: None)
    recs = {r["f"][0][0].rpartition("/")[2]: r for r in data(tmp_path)}
    assert (recs["Hedge%20Lore.epub"]["k"], recs["Hedge%20Lore.epub"]["dev"]) == ("book", "tablet")  # an RPG book's device
    assert (recs["Moon%20Garden.epub"]["k"], recs["Moon%20Garden.epub"]["dev"]) == ("book", "ereader")


def test_a_humble_title_s_kind_in_the_catalog_is_the_one_found_and_its_device_yours(tmp_path):
    """The plan takes your kind for a Humble title's download; the catalog keeps the kind the rules found (k), as for
    every title, and works out its device by yours (device_of)."""
    from hylly import curation

    (tmp_path / "state" / "humble").mkdir(parents=True)
    (tmp_path / "state" / "humble" / "2026-01-01_0000.json").write_text(json.dumps(ORDERS), encoding="utf-8")
    t = next(t for t in plan.for_library(ORDERS, tmp_path).titles if t.product.machine_name == "novel")
    path = t.path
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_bytes(b"PK\x03\x04 the novel")
    Manifest(tmp_path, "humble").add({"path": path, "size": 8, "shop_id": "novel", "role": "book", "ext": "epub"})
    curation.change_single(tmp_path, "kind", "rpg", [{"id": "h:novel"}], by=curation.COMMAND_LINE)
    (item,) = [i for i in catalog.humble_items(tmp_path) if i.id == "h:novel"]
    assert item.kind == "book" and catalog.device_of(item, curation.kinds(tmp_path)["h:novel"]) == "tablet"
    assert catalog.device_of(item) == "ereader"


def card(id_: str, src: str, dev: str, k: str = "book", file=("epub", 1 << 20)) -> dict:
    """A record as the build makes it, of what merge_copies goes by: the same file (_file) is the same title."""
    return {"id": id_, "src": src, "k": k, "t": "Moonwake Atlas", "dev": dev, "_file": file,
            "f": [[f"{src}/Moonwake Atlas.{file[0]}", file[0], file[1]]]}


def test_a_title_in_several_places_is_read_where_the_friendliest_of_its_copies_is():
    """A card's device, of its copies' in other places, is the friendliest (e-reader, either, tablet): a folder's 25 MB
    EPUB leading a shop's 1 MB one no longer takes the card off the e-reader feed, which offers the 1 MB one. A title
    in one place alone, Humble's or StoryBundle's, keeps its lead's device, as before."""
    (merged,) = catalog.merge_copies([card("f:1", "shelf", "tablet"), card("h:x", "humble", "ereader")], ["shelf", "humble"])
    assert (merged["id"], merged["dev"], merged["ids"]) == ("f:1", "ereader", ["h:x"])
    for shop in ("humble", "storybundle"):  # two purchases of one shop, one file: the lead's, as before
        (one,) = catalog.merge_copies([card(f"{shop[0]}:a", shop, "tablet"), card(f"{shop[0]}:b", shop, "ereader")])
        assert (one["id"], one["dev"]) == (f"{shop[0]}:a", "tablet")
    (either,) = catalog.merge_copies([card("f:1", "shelf", "tablet"), card("d:1", "drivethrurpg", "either", "tech")],
                                     ["shelf", "drivethrurpg"])
    assert either["dev"] == "either"  # a tech book's copy joins a book's card, and reads as it does


def test_which_kinds_join_a_book_s_card_is_the_table_s():
    """A copy of a kind with joins_books ([kinds.X]: tech books, RPG books) is one title with a book's in another place;
    a comic's never is. A kind your rules add with it joins too."""
    assert len(catalog.merge_copies([card("f:1", "shelf", "ereader"), card("d:1", "drivethrurpg", "tablet", "rpg")])) == 1
    assert len(catalog.merge_copies([card("f:1", "shelf", "ereader"), card("i:1", "itchio", "tablet", "comic")])) == 2
    assert len(catalog.merge_copies([card("f:1", "shelf", "ereader"), card("i:1", "itchio", "ereader", "zine")])) == 2
    folder = paths.settings_file().parent / "rules"
    folder.mkdir(exist_ok=True)
    (folder / "zines.toml").write_text('[kinds.zine]\njoins_books = true\n', encoding="utf-8")
    from hylly import rules

    rules.reset()
    assert len(catalog.merge_copies([card("f:1", "shelf", "ereader"), card("i:1", "itchio", "ereader", "zine")])) == 1


def test_an_fb2_beside_a_pdf_is_read_where_the_fb2_is():
    """Where a title is best read goes by the file an e-reader would take, an FB2 among them, which Hylly can't read
    for its details: an FB2 beside a PDF is an e-reader's, and the feed, which offers the FB2, lists it. Its main file,
    read for its details and its cover, is the PDF still."""
    item = catalog.Item(id="f:x", source="shelf", kind="book", title="",
                        files=[("shelf/a.pdf", "pdf", 2 * MB), ("shelf/a.fb2", "fb2", 1 * MB)])
    assert catalog.device_of(item) == "ereader" and item.primary[1] == "pdf"
    only = catalog.Item(id="f:y", source="shelf", kind="book", title="", files=[("shelf/b.pdf", "pdf", 2 * MB)])
    assert catalog.device_of(only) == "tablet"
    rpg = catalog.Item(id="f:z", source="shelf", kind="rpg", title="", files=item.files)
    assert catalog.device_of(rpg) == "tablet"  # an RPG book's device is a tablet, whatever its files


# ---- e-books that can't be opened ----------------------------------------------------------------------------


def test_a_file_that_can_t_be_opened_is_noted_with_its_title(tmp_path):
    build_library(tmp_path)
    damage_a_mobi(tmp_path)
    locked_epub(tmp_path / "collections" / "Poetry" / "Ann Writer - The Locked Tale.epub", title="The Locked Tale")
    said = []
    catalog.build(tmp_path, log=said.append)
    items = data(tmp_path)
    tale = next(i for i in items if i["t"] == "The Locked Tale")
    assert tale["bad"] == {tale["f"][0][0]: meta.LOCKED}
    with_bartleby = next(i for i in items if any("Bartleby" in f[0] for f in i["f"]))  # not only a title's main file
    assert [(path.rsplit(".", 1)[1], why) for path, why in with_bartleby["bad"].items()] == [("mobi", meta.DAMAGED)]
    assert not [i for i in items if "bad" in i and i not in (tale, with_bartleby)]
    assert any("2 e-book files can't be opened" in line and "hylly doctor" in line for line in said)


def test_each_file_is_looked_at_once_while_it_stays_the_same(tmp_path, monkeypatch):
    build_library(tmp_path)
    book = tmp_path / "collections" / "Poetry" / "Ann Writer - The Torn Tale.epub"
    book.write_bytes(b"PK\x03\x04 and no more")  # damaged

    def tale() -> dict:
        return next(i for i in data(tmp_path) if any("Torn" in f[0] for f in i["f"]))
    catalog.build(tmp_path, log=lambda s: None)
    looked, real = [], meta.cant_open
    monkeypatch.setattr(meta, "cant_open", lambda path: looked.append(path.name) or real(path))
    catalog.build(tmp_path, log=lambda s: None)
    assert not looked and tale()["bad"] == {tale()["f"][0][0]: meta.DAMAGED}
    make_epub(book, OPF.replace("The Long Dust", "The Torn Tale"))  # a whole one in its place
    catalog.build(tmp_path, log=lambda s: None)
    assert looked == [book.name] and "bad" not in tale()


def test_a_file_that_can_t_be_read_just_now_is_looked_at_again(tmp_path, monkeypatch):
    build_library(tmp_path)
    locked_epub(tmp_path / "collections" / "Poetry" / "Ann Writer - The Locked Tale.epub", title="The Locked Tale")
    real = meta.cant_open

    def stalled(path):
        raise OSError("the share dropped out")
    monkeypatch.setattr(meta, "cant_open", stalled)
    catalog.build(tmp_path, log=lambda s: None)
    assert not any("bad" in i for i in data(tmp_path))
    monkeypatch.setattr(meta, "cant_open", real)
    catalog.build(tmp_path, log=lambda s: None)
    assert "bad" in next(i for i in data(tmp_path) if i["t"] == "The Locked Tale")


def novel(root) -> dict:
    return next(i for i in data(root) if i["id"] == "h:novel")


def novel_s_epub_fails(error: Exception):
    """meta.read_epub that fails on the Humble novel's EPUB, the first time it is read."""
    real, failed = meta.read_epub, []

    def read_epub(z):
        if str(z.filename).endswith("novel.epub") and not failed:
            failed.append(z.filename)
            raise error
        return real(z)
    return read_epub


def test_a_share_s_stall_while_reading_a_book_isn_t_kept_and_the_next_build_reads_it(tmp_path, monkeypatch):
    build_library(tmp_path)
    monkeypatch.setattr(meta, "read_epub", novel_s_epub_fails(OSError(5, "Input/output error")))
    said = []
    catalog.build(tmp_path, log=said.append)
    assert "a" not in novel(tmp_path) and not novel(tmp_path).get("c", "").startswith("covers/")  # its name's, this once
    rel = unquote(novel(tmp_path)["f"][0][0])
    assert f"  1 file couldn't be read just now, so the next hylly catalog tries it again: {rel}." in said
    catalog.build(tmp_path, log=lambda s: None)
    assert novel(tmp_path)["a"] == ["Marlow Ellery"] and novel(tmp_path)["c"].startswith("covers/")


def test_files_that_can_t_be_read_just_now_are_named_three_at_most(tmp_path, monkeypatch):
    """As one that never can be is counted at every build, and hylly doctor can't say more of it."""
    build_library(tmp_path)

    def stalled(z):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(meta, "read_epub", stalled)
    said = []
    catalog.build(tmp_path, log=said.append)
    (line,) = [line for line in said if "couldn't be read just now" in line]
    n = int(line.split()[0])
    named = line.split(" again: ", 1)[1].removesuffix(f" and {n - 3} more.").split(", ")
    assert n > 3 and len(named) == 3 and all(name.endswith(".epub") and (tmp_path / name).is_file() for name in named)
    assert line.startswith(f"  {n} files couldn't be read just now, so the next hylly catalog tries them again: ")
    assert catalog.shown("Odd\nname\x1b[2J\u2028.epub") == "Odd?name?[2J?.epub"  # no line, nor a terminal's command, of its own


def test_retry_covers_reads_again_a_file_found_unreadable(tmp_path, monkeypatch):
    build_library(tmp_path)
    with monkeypatch.context() as then:  # as an older Hylly kept a share's stall: as the file's own
        then.setattr(meta, "read_epub", novel_s_epub_fails(ValueError("not what it seemed")))
        catalog.build(tmp_path, log=lambda s: None)
    catalog.build(tmp_path, log=lambda s: None)
    assert "a" not in novel(tmp_path)  # kept: a build alone doesn't read it again
    catalog.build(tmp_path, log=lambda s: None, retry_covers=True)
    assert novel(tmp_path)["a"] == ["Marlow Ellery"] and novel(tmp_path)["c"].startswith("covers/")


def misnamed_zip(path):
    """A ZIP whose file's name says it is UTF-8 and isn't, as some old tools wrote them."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Café.epub", b"not read")  # saying "the name is UTF-8", as zipfile does for any name not ASCII
    path.write_bytes(path.read_bytes().replace("Café".encode(), b"Caf\xe9!"))  # in Latin-1 all the same


def test_a_zip_whose_names_can_t_be_read_is_a_title_by_its_name_and_the_build_goes_on(tmp_path, monkeypatch):
    build_library(tmp_path)
    misnamed_zip(tmp_path / "collections" / "Poetry" / "Ann Writer - The Moonwake Tales.zip")
    catalog.build(tmp_path, log=lambda s: None)
    tales = next(i for i in data(tmp_path) if i["t"] == "The Moonwake Tales")
    assert tales["a"] == ["Ann Writer"] and "z" not in tales and "zp" not in tales
    calls = []
    monkeypatch.setattr(meta, "package_contents", lambda path: calls.append(path) or [])
    catalog.build(tmp_path, log=lambda s: None)
    assert calls == []  # kept as the package's own: not looked at again


def test_a_package_that_can_t_be_listed_just_then_is_listed_by_the_next_build(tmp_path, monkeypatch):
    build_library(tmp_path)
    with zipfile.ZipFile(tmp_path / "collections" / "Poetry" / "Ann Writer - The Moonwake Tales.zip", "w") as z:
        z.writestr("The Moonwake Tales.epub", b"a book")
    real = meta.package_contents

    def stalled(path):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(meta, "package_contents", stalled)
    said = []
    catalog.build(tmp_path, log=said.append)
    assert "z" not in next(i for i in data(tmp_path) if i["t"] == "The Moonwake Tales")
    assert ("  1 file couldn't be read just now, so the next hylly catalog tries it again: "
            "collections/Poetry/Ann Writer - The Moonwake Tales.zip.") in said
    monkeypatch.setattr(meta, "package_contents", real)
    catalog.build(tmp_path, log=lambda s: None)
    (inside,) = next(i for i in data(tmp_path) if i["t"] == "The Moonwake Tales")["z"]
    assert inside[0].endswith(".zip/The%20Moonwake%20Tales.epub")


def test_a_title_that_trips_up_the_build_is_named_and_the_build_goes_on(tmp_path, monkeypatch):
    build_library(tmp_path)
    rel, real = "collections/Poetry/Herman Melville - Bartleby the Scrivener.epub", meta.read

    def read(path, *kind):
        if path.name == PurePosixPath(rel).name:
            raise RuntimeError("a fault of Hylly's own")
        return real(path, *kind)
    monkeypatch.setattr(meta, "read", read)
    said = []
    catalog.build(tmp_path, log=said.append)
    assert f"  {rel} couldn't be read (RuntimeError): the next hylly catalog tries it again." in said
    bartleby = next(i for i in data(tmp_path) if any("Bartleby" in f[0] for f in i["f"]))
    assert (bartleby["t"], bartleby["a"]) == ("Bartleby the Scrivener", ["Herman Melville"])  # by its name, this once
    monkeypatch.setattr(meta, "read", real)
    said.clear()
    catalog.build(tmp_path, log=said.append)
    bartleby = next(i for i in data(tmp_path) if any("Bartleby" in f[0] for f in i["f"]))
    assert bartleby["t"] == "The Long Dust" and not [line for line in said if "couldn't be read" in line]


def test_copies_of_a_title_keep_what_is_wrong_with_each():
    one = rec("The Lantern Road", "collection", 1, a=["Jo Brennan"], bad={"collection/The Lantern Road.epub": meta.LOCKED})
    two = rec("The Lantern Road", "humble", 2, ext="mobi", a=["Jo Brennan"], bad={"humble/The Lantern Road.mobi": meta.DAMAGED})
    (merged,) = catalog.merge_copies([one, two])
    assert merged["bad"] == {"collection/The Lantern Road.epub": meta.LOCKED, "humble/The Lantern Road.mobi": meta.DAMAGED}
    (merged,) = catalog.merge_copies([rec("The Lantern Road", "humble", 2, a=["Jo Brennan"]), one])
    assert merged["bad"] == one["bad"]


# ---- in the database --------------------------------------------------------------------------------

def test_the_catalog_is_kept_in_the_database_and_the_page_is_made_from_it(tmp_path, capsys):
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    data = (tmp_path / "catalog" / "data.js").read_bytes()
    text = data.decode("utf-8")
    items = json.loads(text[text.index("{"): text.rindex("}") + 1])["items"]
    with catalog.store(tmp_path).read() as c:
        assert catalog.titles(c) == items
        files = c.execute("SELECT count(*) FROM title_file").fetchone()[0]
        kinds = dict(c.execute("SELECT kind, count(*) FROM title GROUP BY kind").fetchall())  # its columns, for SQL
        poem = c.execute("SELECT path FROM title_file WHERE path LIKE 'collections/Poetry/%' ORDER BY path").fetchall()
    assert files == sum(len(i["f"]) for i in items) and sum(kinds.values()) == len(items) and len(poem) == 2
    (tmp_path / "catalog" / "data.js").unlink()
    cli.main(["catalog", "--page", "--library", str(tmp_path)])
    assert "from the database" in capsys.readouterr().out
    assert (tmp_path / "catalog" / "data.js").read_bytes() == data  # the same, to the byte


def test_what_changed_underneath_since_the_build_is_told_of(tmp_path):
    from hylly import doctor

    build_library(tmp_path)
    assert catalog.unlisted(tmp_path) is None  # no build yet
    catalog.build(tmp_path, log=lambda s: None)
    assert catalog.unlisted(tmp_path) == ([], [])
    make_epub(tmp_path / "collections" / "Poetry" / "A New One.epub")
    gone = tmp_path / "collections" / "Poetry" / "Herman Melville - Bartleby the Scrivener.mobi"
    gone.unlink()
    assert catalog.unlisted(tmp_path) == (["collections/Poetry/A New One.epub"], ["collections/Poetry/Herman Melville - Bartleby the Scrivener.mobi"])
    said = doctor._underneath(tmp_path)[0]["says"]
    assert "1 book file the catalog hasn't taken in yet" in said and "1 of its files is no longer on the disk" in said
    catalog.build(tmp_path, log=lambda s: None)
    assert catalog.unlisted(tmp_path) == ([], [])


def refusing(monkeypatch, part: str, errno: int = 2, why: str = "The system cannot find the path specified") -> None:
    """A stand-in for a file listed that the system then says nothing of: every path with this part (a file's name, or
    a folder's, and so the folder itself and what's in it) is refused, as Windows refuses a path longer than it takes,
    or a damaged disk one file."""
    real = Path.stat

    def stat(self, *, follow_symlinks=True):
        if part in self.parts:
            raise OSError(errno, why, str(self))
        return real(self, follow_symlinks=follow_symlinks)
    monkeypatch.setattr(Path, "stat", stat)


def test_a_book_listed_that_can_t_be_looked_at_is_named_by_the_build_and_by_hylly_doctor(tmp_path, monkeypatch):
    """A file listed that can't then be looked at (on Windows, with a path longer than Windows takes; anywhere, one
    damaged file the disk can't answer for, while its folder and the library still answer) isn't passed over in
    silence, nor does it stop the build: the build names it and says why, and hylly doctor does too, rather than saying
    the disk has what the catalog has; a title's file that can't be looked at isn't counted as gone. Only a folder that
    no longer answers stops the build."""
    from hylly import doctor

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    deep = tmp_path / "collections" / "Long Path Press" / "Lantern Wolf.epub"
    deep.parent.mkdir(parents=True)
    make_epub(deep, OPF.replace("The Long Dust", "Moonwake Tide"))
    titles = len(data(tmp_path))
    with monkeypatch.context() as m:
        refusing(m, "Lantern Wolf.epub")
        said = []
        catalog.build(tmp_path, log=said.append)
        (line,) = [line for line in said if "looked at" in line]  # said once, with the file and why
        assert line == ("  1 file can't be looked at, so the catalog doesn't have it: collections/Long Path Press/"
                        f"Lantern Wolf.epub (The system cannot find the path specified). {catalog.UNSEEN_NEXT}.")
        assert len(data(tmp_path)) == titles
        assert catalog.unlisted(tmp_path) == ([], [])
        files, unseen = doctor._underneath(tmp_path)
        assert (files["id"], files["state"], files["says"]) == (
            "catalog-files", "ok", "The disk has what the catalog has, of what can be looked at.")
        assert (unseen["id"], unseen["state"], unseen.get("owner"), unseen["next"]) == (
            "catalog-unseen", "info", True, f"{catalog.UNSEEN_NEXT}.")
        assert unseen["says"] == ("1 file in the library can't be looked at, so the catalog can't tell what's in it: "
                                  "collections/Long Path Press/Lantern Wolf.epub (The system cannot find the path specified).")
    catalog.build(tmp_path, log=lambda s: None)  # read where it can be: a title, and then one whose file can't be
    assert len(data(tmp_path)) == titles + 1
    with monkeypatch.context() as m:
        refusing(m, "Lantern Wolf.epub")
        found: list = []
        assert catalog.unlisted(tmp_path, unseen=found) == ([], [])  # not gone: nothing can be told of it
        assert found == [("collections/Long Path Press/Lantern Wolf.epub", "The system cannot find the path specified")]
    with monkeypatch.context() as m:  # one damaged file, its folder and the library answering: named, and the build goes on
        refusing(m, "Lantern Wolf.epub", 5, "Input/output error")
        said = []
        catalog.build(tmp_path, log=said.append)
        assert [line[:120] for line in said if "looked at" in line] == [
            "  1 file can't be looked at, so the catalog doesn't have it: collections/Long Path Press/Lantern Wolf.epub "
            "(Input/output"]
        assert len(data(tmp_path)) == titles
        found = []
        assert catalog.unlisted(tmp_path, unseen=found) == ([], [])  # that file, not its whole folder
        assert found == [("collections/Long Path Press/Lantern Wolf.epub", "Input/output error")]
    catalog.build(tmp_path, log=lambda s: None)
    with monkeypatch.context() as m:  # its folder no longer answers: the disk's trouble, not the file's, so the build stops
        refusing(m, "Long Path Press", 5, "Input/output error")
        with pytest.raises(exits.Exit) as stop:
            catalog.build(tmp_path, log=lambda s: None)
        assert stop.value.code == exits.LOCAL and len(data(tmp_path)) == titles + 1


def test_hylly_doctor_names_a_folder_it_can_t_read_and_counts_nothing_in_it_as_gone(tmp_path, monkeypatch):
    import os

    from hylly import doctor

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    shut, real = tmp_path / "comics" / "Neon Comics", os.scandir

    def scandir(path="."):
        if Path(path) == shut:  # a folder of yours shut to Hylly, or one Windows can't open, its name ending in a dot
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    files, unseen = doctor._underneath(tmp_path)
    assert files["says"] == "The disk has what the catalog has, of what can be looked at."
    assert unseen["id"] == "catalog-unseen" and unseen["says"] == (
        "1 folder in the library can't be looked at, so the catalog can't tell what's in it: comics/Neon Comics/ (this "
        "folder can't be read).") and unseen["next"] == f"{catalog.FOLDERS_NEXT}."
    found: list = []
    assert catalog.unlisted(tmp_path, unseen=found) == ([], [])  # its titles' files aren't gone: they can't be seen
    assert found == [("comics/Neon Comics/", "this folder can't be read")]


# ---- what a build looks at, and how often ----------------------------------------------------------------------

def walked_by_os_walk(base, books, must, passed, others, unseen):
    """catalog.book_files as it was before it listed each folder itself: by os.walk, each book file looked at by a_file,
    and then each one's size looked at again."""
    from hylly import folders

    def unreadable(e):
        where = Path(e.filename) if e.filename else base
        if where == base or must(str(where)) or passed is None:
            raise e
        passed.append(str(where))

    found = []
    for folder, subs, names in os.walk(base, onerror=unreadable):
        subs[:] = [s for s in subs if not catalog.systems_own(s)]
        sidecar = any(part.lower().endswith(".sdr") for part in Path(folder).relative_to(base).parts)
        for name in names:
            f = Path(folder, name)
            if folders.is_book(name, books) and catalog.a_file(f, unseen, base.parent):
                found.append(f)
            elif others is not None and not sidecar and not catalog.not_a_question(name):
                seen = others.setdefault(f.suffix.lower(), [0, str(f)])
                seen[0] += 1
                seen[1] = min(seen[1], str(f))
    return sorted(found), {str(f): f.stat().st_size for f in found}


def test_your_folders_are_walked_as_os_walk_walked_them_each_book_file_looked_at_once(tmp_path, monkeypatch):
    """The walk lists each folder once and takes what is a folder from the listing: the same books in the same order,
    with the same sizes, the same folders passed over and the same files named or asked about as by os.walk, with links
    in, out, nowhere and round in a circle, a link to a folder (never followed), hidden files and folders, the systems'
    and the tools' folders, a KOReader folder and a folder that can't be listed; and each book file looked at once, not
    again for its size."""
    shelf, outside = tmp_path / "shelf", tmp_path / "_outside"  # beside the library's folders, and no folder of yours
    for rel in ("Stories/Ann Writer - Tale.epub", "Stories/Deep/Deeper/Lantern Wolf.cbz", "Stories/.hidden.epub",
                "Stories/._Tale.epub", ".attic/Moonwake.pdf", "Stories/notes.docx", "Stories/help.chm",
                "Stories/a.txt", "Stories/Kept.sdr/metadata.epub.lua", "Stories/Kept.sdr/notes.docx",
                ".git/objects/Old.epub", ".Trash-1000/files/Gone.epub", "Shut/Inside.epub",
                "Ünïcødé 📚/Tähti - Ann Writer.EPUB"):
        (shelf / rel).parent.mkdir(parents=True, exist_ok=True)
        (shelf / rel).write_bytes(b"x" * len(rel))
    (outside / "away").mkdir(parents=True)
    (outside / "away" / "Far.epub").write_bytes(b"far")
    try:
        (shelf / "Stories" / "Inside Link.epub").symlink_to(shelf / "Stories" / "Ann Writer - Tale.epub")
        (shelf / "Stories" / "Out Link.epub").symlink_to(outside / "away" / "Far.epub")
        (shelf / "Stories" / "Nowhere.epub").symlink_to(shelf / "Stories" / "missing.epub")
        (shelf / "Stories" / "Round.epub").symlink_to(shelf / "Stories" / "About.epub")
        (shelf / "Stories" / "About.epub").symlink_to(shelf / "Stories" / "Round.epub")
        (shelf / "Stories" / "Folder Link").symlink_to(shelf / "Stories" / "Deep", target_is_directory=True)
        (shelf / "Stories" / "Away").symlink_to(outside / "away", target_is_directory=True)
    except OSError:
        pytest.skip("this computer won't make links")
    shut, real = shelf / "Shut", os.scandir

    def scandir(path="."):
        if Path(path) == shut:  # a folder of yours shut to Hylly
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)  # os.walk lists by it too
    books = catalog.rules.current().book_suffixes
    before = [], {}, []
    found_before, sizes_before = walked_by_os_walk(shelf, books, lambda folder: False, *before)
    now, sizes = ([], {}, []), {}
    found = catalog.book_files(shelf, books, lambda folder: False, *now, sizes)
    assert found == found_before and sizes == sizes_before and now == before
    assert [p.name for p in found] == ["Moonwake.pdf", ".hidden.epub", "Ann Writer - Tale.epub", "Lantern Wolf.cbz",
                                       "Inside Link.epub", "Out Link.epub", "Tähti - Ann Writer.EPUB"]
    assert now[0] == [str(shut)]  # passed over, and three links that lead nowhere or round in a circle named
    assert sorted(Path(path).name for path, _ in now[2]) == ["About.epub", "Nowhere.epub", "Round.epub"]
    assert sorted(now[1]) == [".chm", ".docx"] and now[1][".docx"][0] == 1  # a KOReader folder's isn't asked about
    looked, real_stat = Counter(), Path.stat
    with monkeypatch.context() as m:
        m.setattr(Path, "stat", lambda self, **k: (looked.update([self]), real_stat(self, **k))[1])
        items = catalog.folder_items(tmp_path, passed=[], unseen=[])
    assert sorted(tmp_path / rel for item in items for rel, _, _ in item.files) == found
    assert {looked[f] for f in [*found, *(Path(path) for path, _ in now[2])]} == {1}  # each book file once, size too


def test_a_build_lists_the_covers_once_and_makes_again_one_that_went(tmp_path, monkeypatch):
    """No title's cover is looked for on the disk, a round trip each on a share: the covers folder is listed once, and
    a cover gone since the last build is made again, as ever."""
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    covers = tmp_path / "catalog" / "covers"
    gone = sorted(covers.iterdir())[0]
    gone.unlink()
    listed, looked_for, real_exists, real_listdir = [], [], Path.exists, os.listdir
    with monkeypatch.context() as m:
        m.setattr(Path, "exists", lambda self, **k: (looked_for.append(self), real_exists(self, **k))[1])
        m.setattr(os, "listdir", lambda path=".": (listed.append(Path(path)), real_listdir(path))[1])
        catalog.build(tmp_path, log=lambda s: None)
    assert listed.count(covers) == 1 and not [p for p in looked_for if p.parent == covers] and gone.is_file()
    made = [r["c"] for r in data(tmp_path) if r.get("c", "").startswith("covers/")]
    assert made and all((tmp_path / "catalog" / c).is_file() for c in made)


def test_each_shop_s_newest_snapshot_is_read_once_for_its_titles(tmp_path, monkeypatch):
    """Read whole to tell that it's whole, and then gone by as it was read (snapshot.latest): Humble's orders aren't
    read again for its titles, nor a third time for their ISBNs."""
    build_library(tmp_path)
    read, real = Counter(), snapshot.load
    monkeypatch.setattr(snapshot, "load",
                        lambda path: (read.update([Path(path).relative_to(tmp_path).as_posix()]), real(path))[1])
    items = [item for shop in catalog.SHOPS for item in catalog.reader(shop)(tmp_path)]
    assert {item.source for item in items} == {"humble", "storybundle"}
    assert read == {"state/humble/2026-01-01_0000.json": 1, "state/storybundle/2026-01-01_0000.json": 1}


def test_a_title_with_no_lookup_answer_has_its_record_made_once(tmp_path, monkeypatch):
    """Made once to be asked about, and that same record merged with its copies (merge_copies copies each): only a
    title a lookup answered is made again, with the answer."""
    from helpers import near_miss

    build_library(tmp_path)
    near_miss(tmp_path, "s:7", 0.9)  # found: Arcade Worlds
    made, real = [], catalog.record
    monkeypatch.setattr(catalog, "record",
                        lambda item, lookup, kind_fix=None: made.append(item.id) or real(item, lookup, kind_fix))
    stats = catalog.build(tmp_path, log=lambda s: None)
    assert len(made) == stats["titles"] + stats["copies_merged"] + 1 and made.count("s:7") == 2
    assert "About Arcade Worlds II" in next(r for r in data(tmp_path) if r["id"] == "s:7")["sub"]  # the answer, as ever


def test_a_subfolder_is_a_link_by_what_the_disk_says_of_it_not_by_the_listing_alone(tmp_path, monkeypatch):
    """Whether a subfolder is a link, never followed, is asked of the disk for each one (os.path.islink), as os.walk
    asks: a listing's own type for a reparse point (a junction, a link Windows made, on Linux's ntfs driver) may not be
    the disk's, and a link taken for a folder would be followed, a folder taken for a link lost with its titles."""
    for rel in ("shelf/Stories/Ann Writer - Tale.epub", "shelf/Stories/Deep/Lantern Wolf.epub",
                "shelf/Other/Moonwake.epub"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
    deep, real, asked = tmp_path / "shelf" / "Stories" / "Deep", os.path.islink, []
    monkeypatch.setattr(os.path, "islink", lambda path: asked.append(Path(path)) or Path(path) == deep or real(path))
    found = catalog.book_files(tmp_path / "shelf", catalog.rules.current().book_suffixes)
    assert [p.name for p in found] == ["Moonwake.epub", "Ann Writer - Tale.epub"]  # the disk says Deep is a link
    assert sorted(asked) == sorted([tmp_path / "shelf" / "Other", tmp_path / "shelf" / "Stories", deep])  # once each


def test_nothing_is_unpacked_or_removed_through_a_link_where_a_package_s_book_is_unpacked(tmp_path):
    """The folder a package's book is unpacked into, if it is a link (which may lead out of the library), is never
    followed: the book is read again next time, as one that couldn't be read just now, and nothing where the link
    leads is written or removed."""
    import time

    package = tmp_path / "shelf" / "Packages" / "Ann Writer - Arcade Package.zip"
    package.parent.mkdir(parents=True)
    inner = tmp_path / "inner.epub"
    make_epub(inner, OPF.replace("The Long Dust", "Arcade Worlds"))
    with zipfile.ZipFile(package, "w") as z:
        z.write(inner, "Arcade Worlds.epub")
    elsewhere, unpacking = tmp_path / "_elsewhere", tmp_path / "catalog" / catalog.UNPACKING
    elsewhere.mkdir()
    old = elsewhere / f"Old-0a1b2c3d.epub{meta.UNPACKED}"
    old.write_bytes(b"not the build's")
    then = time.time() - catalog.LEFT_FOR - 60
    os.utime(old, (then, then))
    unpacking.parent.mkdir(parents=True)
    try:
        unpacking.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("this computer won't make links")
    said = []
    catalog.build(tmp_path, log=said.append)
    assert [line for line in said if "couldn't be read just now" in line] == [
        "  1 file couldn't be read just now, so the next hylly catalog tries it again: "
        "shelf/Packages/Ann Writer - Arcade Package.zip."]
    assert unpacking.is_symlink() and [p.name for p in elsewhere.iterdir()] == [old.name]


def test_what_a_stopped_build_left_unpacked_goes_and_a_build_leaves_no_folder_for_it(tmp_path, monkeypatch):
    """A package's book is unpacked into the catalog folder, on the library's own disk, to be read; what a build that
    stopped left there goes at the next build, but not a file another computer's build may be reading just now, and a
    build leaves no folder of it behind."""
    import time

    package = tmp_path / "shelf" / "Packages" / "Ann Writer - Arcade Package.zip"
    package.parent.mkdir(parents=True)
    inner = tmp_path / "inner.epub"
    make_epub(inner, OPF.replace("The Long Dust", "Arcade Worlds"))
    with zipfile.ZipFile(package, "w") as z:
        z.write(inner, "Arcade Worlds.epub")
    unpacking = tmp_path / "catalog" / catalog.UNPACKING
    unpacking.mkdir(parents=True)
    old, fresh = unpacking / f"Old-0a1b2c3d.epub{meta.UNPACKED}", unpacking / f"Other-0a1b2c3e.pdf{meta.UNPACKED}"
    old.write_bytes(b"left by a power cut")
    then = time.time() - catalog.LEFT_FOR - 60
    os.utime(old, (then, then))
    fresh.write_bytes(b"being read")
    asked, real = [], meta.read
    monkeypatch.setattr(meta, "read", lambda path, kind="book", unpack_in=None: asked.append(unpack_in)
                        or real(path, kind, unpack_in))
    catalog.build(tmp_path, log=lambda s: None)
    assert [r["t"] for r in data(tmp_path)] == ["Arcade Worlds"] and asked == [unpacking]
    assert not old.exists() and fresh.exists()
    fresh.unlink()
    catalog.build(tmp_path, log=lambda s: None)
    assert not unpacking.exists() and (tmp_path / "catalog" / "cache").is_dir()


# ---- a build that misses a folder ------------------------------------------------------------------------------

def shelf(root, n=12):
    """A folder of your own with n books, in shelf/Stories."""
    for i in range(n):
        book = root / "shelf" / "Stories" / f"Ann Writer - Tale {i:02}.pdf"
        book.parent.mkdir(parents=True, exist_ok=True)
        book.write_bytes(b"%PDF-1.4 " + b"x" * i)


def as_kept(root):
    """What the last build kept: the page's data and the database's titles."""
    with catalog.store(root).read() as c:
        return (root / "catalog" / "data.js").read_bytes(), catalog.titles(c)


def test_a_folder_that_can_t_be_listed_stops_the_build_and_changes_nothing(tmp_path, monkeypatch):
    import os

    from hylly.exits import LOCAL, Exit

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    kept = as_kept(tmp_path)
    stalled, real = tmp_path / "comics" / "Neon Comics", os.scandir

    def scandir(path="."):
        if Path(path) == stalled:  # a share that stalls, or a disk not mounted under it
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(Exit) as stop:
        catalog.build(tmp_path, log=lambda s: None)
    assert stop.value.code == LOCAL and str(stalled) in str(stop.value) and "run hylly catalog again" in str(stop.value)
    assert as_kept(tmp_path) == kept
    monkeypatch.setattr(os, "scandir", real)
    state, real_list = tmp_path / "state", os.listdir

    def listdir(path="."):
        if Path(path).parent == state:  # a shop's snapshots can't be listed: that isn't "none"
            raise PermissionError(13, "Access is denied", str(path))
        return real_list(path)
    monkeypatch.setattr(os, "listdir", listdir)
    with pytest.raises(Exit) as stop:
        catalog.build(tmp_path, log=lambda s: None)
    assert stop.value.code == LOCAL and str(state) in str(stop.value) and as_kept(tmp_path) == kept


def test_the_systems_folders_are_passed_by_and_one_that_can_t_be_listed_stops_only_if_it_had_titles(tmp_path, monkeypatch):
    import os

    from hylly.exits import LOCAL, Exit

    build_library(tmp_path)
    shelf(tmp_path)
    private, recycled = tmp_path / "shelf" / "Private", tmp_path / "$RECYCLE.BIN" / "S-1-5-21-1001"
    for folder in (private, recycled, tmp_path / "System Volume Information", tmp_path / "shelf" / "lost+found"):
        folder.mkdir(parents=True)
    (tmp_path / "$RECYCLE.BIN" / "$RA1B2C3.pdf").write_bytes(b"%PDF-1.4 deleted")  # a library at a disk's root
    denied = {private, recycled, tmp_path / "System Volume Information", tmp_path / "shelf" / "lost+found"}
    real = os.scandir

    def scandir(path="."):
        if Path(path) in denied:  # another user's, the system's, or a folder of yours shut to Hylly
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    said = []
    catalog.build(tmp_path, log=said.append)
    assert [line for line in said if "can't be read" in line] == [
        "  Passed over 1 folder that can't be read and had no titles in the last build: shelf/Private."]
    sources = [i["src"] for i in data(tmp_path)]
    assert sources.count("shelf") == 12 and not {"$RECYCLE.BIN", "System Volume Information"} & set(sources)
    kept = as_kept(tmp_path)
    for shut in (tmp_path / "shelf" / "Stories", tmp_path / "archive"):  # it had titles last time; one of your folders itself
        shut.mkdir(exist_ok=True)
        denied.add(shut)
        with pytest.raises(Exit) as stop:
            catalog.build(tmp_path, log=lambda s: None)
        assert stop.value.code == LOCAL and str(shut) in str(stop.value) and as_kept(tmp_path) == kept
        denied.discard(shut)


def test_hylly_doctor_s_look_at_the_disk_passes_the_systems_folders_by_as_a_build_does(tmp_path):
    build_library(tmp_path)
    for clutter in ("$RECYCLE.BIN/S-1-5-21-1001/$RA1B2C3.epub", "#recycle/Old Tale.pdf", "comics/Neon Comics/@eaDir/Neon Rangers 009.cbz",
                    "collections/lost+found/#1234.epub"):
        (tmp_path / clutter).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / clutter).write_bytes(b"x")
    catalog.build(tmp_path, log=lambda s: None)
    files = [unquote(f[0]).lower() for i in data(tmp_path) for f in i["f"]]
    assert files and not any(word in f for f in files for word in ("@eadir", "lost+found", "recycle"))
    assert catalog.unlisted(tmp_path) == ([], [])  # nothing a build would take in: nothing for hylly doctor to tell of


def test_titles_a_build_from_before_found_in_the_systems_folders_aren_t_missed(tmp_path, monkeypatch):
    build_library(tmp_path)
    for i in range(12):  # a library at a disk's root, built by a Hylly that read them
        for folder, name in (("$RECYCLE.BIN", "Binned Tale"), ("collections/lost+found", "Found Tale")):
            book = tmp_path / folder / f"{name} {i:02}.pdf"
            book.parent.mkdir(parents=True, exist_ok=True)
            book.write_bytes(b"%PDF-1.4 " + b"x" * i)
    with monkeypatch.context() as earlier:
        earlier.setattr(catalog, "systems_own", lambda name: False)
        catalog.build(tmp_path, log=lambda s: None)
    before = data(tmp_path)
    assert sum(i["src"] == "$RECYCLE.BIN" for i in before) == 12 and sum(i["src"] == "collections" for i in before) == 13
    catalog.build(tmp_path, log=lambda s: None)  # no "$RECYCLE.BIN: 0 titles, 12 in the last build": they aren't lost
    after = data(tmp_path)
    assert len(after) == len(before) - 24 and not any("lost+found" in unquote(f[0]) for i in after for f in i["f"])


def test_the_folders_tools_keep_beside_your_books_are_passed_by_at_any_depth_and_their_titles_aren_t_missed(tmp_path, monkeypatch):
    """Version control's, trash and sync tools' folders (Syncthing's old copies, Calibre's trash) hold no book of yours: a
    build doesn't read them, nor does hylly doctor's look at the disk, and the titles a build from before found in them
    leave without the build stopping for far fewer titles. Any other folder starting with a dot is read as before."""
    build_library(tmp_path)
    tools = [".git", ".svn", ".hg", ".Trash", ".Trash-1000", ".Trashes", ".caltrash", ".stversions", ".stfolder", ".sync",
             ".dropbox.cache", ".AppleDouble", ".Spotlight-V100", ".fseventsd", ".TemporaryItems"]
    for n, name in enumerate(tools):  # at the top of a folder of yours, and deeper
        for place in (f"collections/{name}", f"collections/Poetry/{name}/files"):
            book = tmp_path / place / f"Kept Tale {n:02}.pdf"
            book.parent.mkdir(parents=True, exist_ok=True)
            book.write_bytes(b"%PDF-1.4 " + b"x" * n)
    yours = tmp_path / "collections" / "Poetry" / ".Moonwake Press" / "Moonwake Tide.pdf"  # a folder of yours, named so
    yours.parent.mkdir(parents=True)
    yours.write_bytes(b"%PDF-1.4 tide")
    with monkeypatch.context() as earlier:  # a Hylly that read them all
        earlier.setattr(catalog, "systems_own", lambda name: False)
        catalog.build(tmp_path, log=lambda s: None)
    before = [unquote(f[0]) for i in data(tmp_path) for f in i["f"]]
    assert sum("Kept Tale" in f for f in before) == 2 * len(tools)
    assert catalog.unlisted(tmp_path) == ([], [])  # hylly doctor before the next build: nothing new, nothing gone
    catalog.build(tmp_path, log=lambda s: None)  # no "collections: 2 titles, 32 in the last build": they aren't missed
    after = [unquote(f[0]) for i in data(tmp_path) for f in i["f"]]
    assert not any("Kept Tale" in f for f in after) and "collections/Poetry/.Moonwake Press/Moonwake Tide.pdf" in after
    assert len(after) == len(before) - 2 * len(tools) and catalog.unlisted(tmp_path) == ([], [])
    for name in tools:
        assert catalog.systems_own(name) and catalog.systems_own(name.upper()), name
    for name in (".Moonwake Press", ".Trash-x", ".Trash-", ".gitignore", ".sync-conflict", "Trash"):  # not theirs
        assert not catalog.systems_own(name), name


def test_a_build_missing_most_of_a_source_stops_before_keeping_anything(tmp_path):
    import shutil

    from hylly.exits import NEEDS_YOU, Exit

    build_library(tmp_path)
    shelf(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    kept = as_kept(tmp_path)
    shutil.move(tmp_path / "shelf", tmp_path / "_away")  # its disk isn't mounted
    with pytest.raises(Exit) as stop:
        catalog.build(tmp_path, log=lambda s: None)
    said = str(stop.value)
    assert stop.value.code == NEEDS_YOU and "  shelf: 0 titles, 12 in the last build" in said
    assert "Most of those missing were in shelf/Stories (12)." in said and "hylly catalog --yes" in said
    assert as_kept(tmp_path) == kept
    asked = []
    catalog.build(tmp_path, log=lambda s: None, fewer=lambda said: asked.append(said) or True)  # a yes
    assert len(asked) == 1 and "shelf: 0 titles" in asked[0] and not any(i["src"] == "shelf" for i in data(tmp_path))


def test_a_build_missing_a_fifth_of_everything_stops_too_and_a_smaller_change_doesn_t(tmp_path):
    from hylly.exits import NEEDS_YOU, Exit

    build_library(tmp_path)
    shelf(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    stories = sorted((tmp_path / "shelf" / "Stories").iterdir())
    for book in stories[:4]:
        book.unlink()
    catalog.build(tmp_path, log=lambda s: None)  # 4 of the shelf's 12, and of 24 in all: kept without a word
    assert len(data(tmp_path)) == 20
    for book in stories[4:9]:
        book.unlink()
    with pytest.raises(Exit) as stop:
        catalog.build(tmp_path, log=lambda s: None)
    said = str(stop.value)
    assert stop.value.code == NEEDS_YOU and "  The whole catalog: 15 titles, 20 in the last build" in said
    assert "shelf:" not in said  # 8 titles in the last build: too few to be watched by itself
    assert len(data(tmp_path)) == 20


def test_hylly_catalog_asks_and_with_nobody_there_says_what_to_run(tmp_path, monkeypatch, capsys):
    import shutil

    from hylly import bell, runs
    from hylly.exits import NEEDS_YOU

    build_library(tmp_path)
    shelf(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    shutil.move(tmp_path / "shelf", tmp_path / "_away")
    notes = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: notes.append((title, text, public)) or True)
    run = lambda *more: cli.main(["catalog", "--library", str(tmp_path), *more])  # noqa: E731
    with pytest.raises(SystemExit) as stop:
        run()  # a scheduled build: nobody to ask, nor to read what it says
    assert stop.value.code == NEEDS_YOU and "hylly catalog --yes keeps the build" in capsys.readouterr().err
    (title, text, public), = notes
    assert title == "A catalog build stopped" and "shelf: 0 titles, 12 in the last build" in text and "hylly catalog --yes" in text
    assert "shelf" not in public and "hylly catalog --yes" in public  # the phone's: no names
    (last,) = [r for r in runs.last_runs() if r["job"] == "catalog"]  # hylly jobs: this run, not the last good one
    assert last["exit"] == NEEDS_YOU and last["said"].startswith("This build has far fewer titles than the last one")
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr("builtins.input", lambda question: "n")
    with pytest.raises(SystemExit) as stop:
        run()
    out, err = capsys.readouterr()
    assert stop.value.code == NEEDS_YOU and "shelf: 0 titles" in err and "Nothing was kept" in err  # with its question
    assert len(notes) == 1 and [r["exit"] for r in runs.last_runs() if r["job"] == "catalog"] == [NEEDS_YOU]  # said there
    assert any(i["src"] == "shelf" for i in data(tmp_path))
    run("--yes")
    assert "Kept all the same, as --yes says." in capsys.readouterr().err and not any(i["src"] == "shelf" for i in data(tmp_path))


def test_a_folder_title_s_id_is_one_in_either_unicode_form_and_the_usual_form_keeps_its_id():
    import hashlib
    import unicodedata

    usual = "comics/Neon Comics/Neon Rangers 008.cbz"
    assert catalog.folder_id(usual) == "f:" + hashlib.sha1(usual.lower().encode()).hexdigest()[:12]  # as it always was
    composed = "ebooks/Folklore & Myth/Pääkallo.epub"
    assert catalog.folder_id(unicodedata.normalize("NFD", composed)) == catalog.folder_id(composed) \
        == "f:" + hashlib.sha1(composed.lower().encode()).hexdigest()[:12]


# ---- which files are one title, and which of them leads it: by their names alone, in any script, on every system ----

def stories(root, *names: str) -> list[catalog.Item]:
    """Empty book files in shelf/Stories (a folder of your own), and the titles they make: only the names count."""
    for name in names:
        book = root / "shelf" / "Stories" / name
        book.parent.mkdir(parents=True, exist_ok=True)
        book.write_bytes(b"")
    return catalog.folder_items(root)


def test_files_named_in_any_script_are_one_title_per_name(tmp_path):
    items = stories(tmp_path, "Книга 1.epub", "Книга 1.pdf", "Другая 1.epub", "月の庭.epub", "ガラスの港.epub", "ΟΔΟΣ.epub",
                    "Οδος.pdf", "Οδός.mobi", "C.epub", "C++.epub", "C++.pdf", "C#.epub", "Café Lantern.epub",
                    "Cafe\u0301 Lantern.pdf", "Cafe Lantern.mobi", "Some_Book.epub", "SomeBook.pdf", "Issue#1.epub", "Issue 1.pdf")
    assert {tuple(Path(rel).name for rel, _, _ in item.files) for item in items} == {
        ("Книга 1.epub", "Книга 1.pdf"), ("Другая 1.epub",), ("月の庭.epub",), ("ガラスの港.epub",), ("ΟΔΟΣ.epub", "Οδος.pdf"),
        ("Οδός.mobi",), ("C.epub",), ("C#.epub",), ("C++.epub", "C++.pdf"), ("Café Lantern.epub", "Cafe\u0301 Lantern.pdf"),
        ("Cafe Lantern.mobi",), ("Some_Book.epub", "SomeBook.pdf"),
        ("Issue 1.pdf", "Issue#1.epub")}  # the same name in another Unicode form is one; a # inside a word is punctuation


def test_names_that_differ_by_an_accent_alone_are_two_titles(tmp_path):
    items = stories(tmp_path, "Tähti.epub", "TÄHTI.pdf", "Ta\u0308hti.mobi", "Tahti.epub", "ガラス.epub", "カラス.epub",
                    "Мой путь.epub", "Мои путь.epub", "Ёлка.epub", "Елка.epub")
    assert {tuple(Path(rel).name for rel, _, _ in item.files) for item in items} == {
        ("Tähti.epub", "Ta\u0308hti.mobi", "TÄHTI.pdf"), ("Tahti.epub",), ("ガラス.epub",), ("カラス.epub",),
        ("Мой путь.epub",), ("Мои путь.epub",), ("Ёлка.epub",), ("Елка.epub",)}  # a dakuten, й and ё are accents to Unicode


def test_a_title_s_first_file_and_so_its_id_are_the_same_on_every_system(tmp_path, monkeypatch):
    from pathlib import PureWindowsPath

    stories(tmp_path, "MOONWAKE.pdf", "Moonwake.epub", "Some_Book.epub", "SomeBook.pdf", "lantern wolf.cbz", "Lantern Wolf.pdf")
    listed = catalog.book_files
    found = []
    for key_of in (lambda f: f.as_posix(), lambda f: str(PureWindowsPath(f)).lower(), lambda f: f.as_posix()[::-1]):  # Linux,
        monkeypatch.setattr(catalog, "book_files", lambda *given, key_of=key_of: sorted(listed(*given), key=key_of))  # Windows,
        found.append({item.id: [rel for rel, _, _ in item.files] for item in catalog.folder_items(tmp_path)})  # any order
    assert found[0] == found[1] == found[2]
    assert {Path(files[0]).name for files in found[0].values()} == {"lantern wolf.cbz", "Moonwake.epub", "Some_Book.epub"}
    assert [Path(rel).name for rel in found[0][catalog.folder_id("shelf/Stories/Moonwake.epub")]] == ["Moonwake.epub", "MOONWAKE.pdf"]


def test_a_file_whose_name_isn_t_proper_text_is_passed_over_and_told_once(tmp_path, monkeypatch):
    shelf(tmp_path, n=2)
    catalog.build(tmp_path, log=lambda s: None)
    before = {i["id"]: i["f"] for i in data(tmp_path)}
    # as os.walk gives a name written in Latin-1 on Linux: the byte that isn't UTF-8 as a stand-in. Its letters and digits
    # are those of Tale 00, whose title it would have joined; a folder of such a name gives its files such paths too.
    odd = [tmp_path / "shelf" / "Stories" / "Ann Writer - Tale 00\udce9.pdf", tmp_path / "shelf" / "Caf\udce9" / "Lantern.pdf"]
    listed = catalog.book_files
    monkeypatch.setattr(catalog, "book_files", lambda base, *more: [*listed(base, *more), *(odd if base.name == "shelf" else [])])
    said = []
    catalog.build(tmp_path, log=said.append)
    assert {i["id"]: i["f"] for i in data(tmp_path)} == before  # no other title's files or id changed
    (told,) = [line for line in said if "isn't proper text" in line]
    assert "Passed over 2 files" in told and "shelf/Stories/Ann Writer - Tale 00\\xe9.pdf, shelf/Caf\\xe9/Lantern.pdf." in told
    misnamed = []
    assert len(catalog.folder_items(tmp_path, misnamed=misnamed)) == 2 and len(misnamed) == 2
    assert catalog.shown_name("x/\ud800.pdf") == "x/\\ud800.pdf"  # a stand-in of another kind: as itself
