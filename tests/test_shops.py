"""Every shop the same: hylly <shop> <action>, logins named by their shop, and hylly download for all of them. Each shop
is named once, in shops.py: the table's walk finds all a shop must have, and no list of the shops kept by hand."""

from __future__ import annotations

import ast
import dataclasses
import functools
import hashlib
import importlib.util
import inspect
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import conftest
from hylly import catalog, cli, commands, effects, logins, parts, paths, rules, runs, shopping, shops, snapshot
from hylly.library import SHOP_TRAITS, Catalog, Device
from hylly.logins import save_session
from hylly.sites import SITES

from helpers import CLOCK_TIMES, DT_OLD, SB_OLD, at_the_first_step

SRC = Path(shops.__file__).parent


def test_every_shop_has_the_same_shape(tmp_path, capsys):
    parser = cli.build_parser()
    assert parser.parse_args(["humble", "seed", "D:/old", "E:/older"]).folders == ["D:/old", "E:/older"]
    for shop in ("humble", "storybundle", "drivethrurpg", "itchio"):  # each says what it can do
        with pytest.raises(SystemExit) as what:
            cli.main([shop])
        assert what.value.code == 2 and f"usage: hylly {shop} [-h] {{" in capsys.readouterr().err
        assert parser.parse_args([shop, "download"]).leaf == f"{shop} download"
    with pytest.raises(SystemExit) as which:
        cli.main(["login"])
    assert "humble, storybundle, drivethrurpg" in str(which.value)  # no shop is the default
    with pytest.raises(SystemExit) as what:
        cli.main(["humble"])
    assert "{download,seed,list}" in capsys.readouterr().err  # the shop list: every shop's, from the table


def test_download_runs_every_shop_with_a_login(tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "cmd_humble_download", lambda args: ran.append(("humble", args.dry_run)))
    monkeypatch.setattr(cli, "cmd_storybundle", lambda args: ran.append(("storybundle", args.dry_run)))
    monkeypatch.setattr(cli, "cmd_itchio", lambda args: ran.append(("itchio", args.dry_run)))  # never the real one

    def broken(args):
        raise SystemExit("DriveThruRPG answered HTTP 500 for page 1 of your library.")

    monkeypatch.setattr(cli, "cmd_drivethrurpg", broken)
    save_session("h" * 80, "humble")
    save_session("d" * 40, "drivethrurpg")
    with pytest.raises(SystemExit) as stopped:  # one shop's trouble doesn't stop the others, but it is told
        cli.main(["download", "--dry-run", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert stopped.value.code == 1 and ran == [("humble", True)]
    assert "StoryBundle: no login saved (hylly login storybundle)" in out
    assert "itch.io: no login saved (hylly login itchio)" in out
    assert "DriveThruRPG: stopped: DriveThruRPG answered HTTP 500" in out and "Humble: done." in out


def test_no_test_has_your_logins_or_reaches_past_this_computer(no_network):
    """conftest.py: a shop login or lookup key in your environment is not the tests', and a real request fails the
    test."""
    assert not [variable for _, _, variable in SITES.values() if os.environ.get(variable)]
    assert ("HYLLY_ALLOW_ROOT" in os.environ) == conftest.AS_ROOT  # set only for a suite run as root, as in a container
    made_up = httpx.MockTransport(lambda request: httpx.Response(200, text="a made-up shop"))
    assert httpx.Client(transport=made_up).get("https://shop.example.com/library").text == "a made-up shop"
    for address in ("https://shop.example.com/library", "http://192.168.1.5:8770/opds"):  # not even the home network
        with pytest.raises(pytest.fail.Exception, match="A test asked the network"):
            httpx.get(address)
    assert no_network == ["GET https://shop.example.com/library", "GET http://192.168.1.5/opds"]
    no_network.clear()  # asked on purpose


# ---- the table of the shops ----------------------------------------------------------------------------------------

def test_the_table_s_walk_finds_all_a_shop_must_have():
    """Each shop in shops.py has a module named by its id, its command with the table's actions, its job, a row of
    effects for each action, a reader in the catalog whose titles' ids start with its letter (its own, or on the shops'
    core the core's), its command's handler, its logins, a kind there is, and a folder of its own; and hylly download,
    verify and the e-reader feed go by the table."""
    parser = commands.build_parser()
    top = next(a for a in parser._actions if a.__class__.__name__ == "_Commands")
    letters = [shop.letter for shop in shops.SHOPS.values()]
    assert len(set(letters)) == len(letters) and "f" not in letters  # f: your own folders' titles
    for shop in shops.SHOPS.values():
        assert importlib.util.find_spec(f"hylly.{shop.id}") and shop.id in parts.PARTS["shops"], shop.id
        actions = commands.actions_of(top.choices[shop.id])
        assert set(actions) == {*shop.actions, "list"} and "download" in shop.actions and "list" not in shop.actions
        row = effects.EFFECTS[f"{shop.id} list"]  # the shop list, every shop's from the table and its download (shoplist.py)
        assert row["online"] == [shop.site] and row["job"] is None and row["dry_run"] == effects.SITE, shop.id
        assert any(a.dest == "list" and not a.option_strings for a in actions["download"]._actions), shop.id
        takes = {a.dest for a in actions["list"]._actions}
        assert {"file", "force", "title", "kind"} <= takes and not takes & {"limit", "jobs", "update", "max_gb", "json"}, shop.id
        assert {a.dest for a in actions["download"]._actions} - commands.RUN_OPTIONS <= takes, shop.id
        assert runs.JOBS[shop.id] == (shop.job, f"hylly jobs stop {shop.id}")
        for action in shop.actions:
            row = effects.EFFECTS[f"{shop.id} {action}"]
            assert row["online"] in ([], [shop.site]) and row["job"] in (None, shop.id), (shop.id, action)
        assert shop.site in effects.EFFECTS["download"]["online"]
        reader = catalog.reader(shop.id)  # its own, or for a shop on the shops' core the core's, given its id
        if isinstance(reader, functools.partial):
            assert reader.func is catalog.shop_items and reader.args == (shop.id,), shop.id
            assert 'id=f"{shop.letter}:' in inspect.getsource(catalog.shop_items)
        else:
            assert f'id=f"{shop.letter}:' in inspect.getsource(reader), shop.id
        assert cli.HANDLERS[shop.id] == f"cmd_{shop.id}" and callable(getattr(cli, f"cmd_{shop.id}"))
        assert all(login in SITES for login in shop.logins) and shop.kind in rules.current().kinds
        assert shop.folder == shop.id and shop.id not in ("catalog", "state", "curation", "superseded")
        assert shop.id in cli.NEEDS_THE_LIBRARY and shop.id in commands.AREAS["Getting books"]
        download = commands.actions_of(top.choices[shop.id])["download"]
        assert any("--update" in a.option_strings for a in download._actions) == shop.update, shop.id  # hylly download --update
        assert any("--kind" in a.option_strings for a in download._actions), shop.id  # every shop's, itch.io's too
    assert Device().pdf_from == tuple(s.id for s in shops.SHOPS.values() if s.trusted_pdfs)
    assert "itchio" not in Device().pdf_from  # anyone can upload there


ALLOWED = {  # a module's own lists that name shops one by one on purpose: per site, or per command
    ("sites", "SITES"), ("sites", "KEYS"), ("sites", "ENDED_THERE"),  # the logins: a shop's and a lookup source's, each its own
    ("cli", "COOKIES"),  # the logins that are a cookie, with the site's own name for it
    ("commands", "EXAMPLES"),  # each command's own examples, as its parser is its own
}
SHOP_WORD = re.compile(r"\b(?:" + "|".join(map(re.escape, shops.SHOPS)) + r")\b")


def _hand_lists(path: Path) -> list[str]:
    """Lists, tuples, sets and dicts naming two shops or more, and texts naming three or more, outside ALLOWED."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any((path.stem, getattr(t, "id", "")) in ALLOWED for t in targets):
            allowed |= {id(n) for n in ast.walk(node.value)}
    found = []
    for node in ast.walk(tree):
        if id(node) in allowed:
            continue
        items = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else node.keys if isinstance(node, ast.Dict) else None
        named = {n.value for n in items or () if isinstance(n, ast.Constant) and n.value in shops.SHOPS}
        if len(named) >= 2:
            found.append(f"{path.name}:{node.lineno}: {sorted(named)}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(set(SHOP_WORD.findall(node.value))) >= 3:
            found.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
    return found


def test_every_shop_s_download_returns_how_its_run_ended(monkeypatch):
    """A shop's command hands back what the shops' core returns (cli._shop_download): how its run ended, cli.Ended,
    which its phone note and hylly jobs are made of. One that dropped it would leave a long download's counts unsaid."""
    ran = cli.Ended(text="3 files done, 0 failed.")
    monkeypatch.setattr(cli, "_shop_download", lambda args, shop_id, library, skip=None: ran)
    for shop in shops.SHOPS.values():
        args = commands.build_parser().parse_args([shop.id, "download", "--library", "x"])
        assert getattr(cli, cli.HANDLERS[shop.id])(args) is ran, shop.id


def test_no_list_of_the_shops_is_kept_by_hand_anywhere_else():
    """Every list and label of the shops comes from shops.py, so a shop added there reaches them all; the library
    page gets theirs with the catalog (library.SHOP_TRAITS), and names no shop of its own."""
    found = [line for path in sorted(SRC.rglob("*.py")) if path.name != "shops.py" for line in _hand_lists(path)]
    assert not found, "\n".join(found)
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    assert not [shop for shop in shops.SHOPS if f'"{shop}"' in page or f"'{shop}'" in page]
    assert SHOP_TRAITS == {shop.id: {"bundles": shop.bundles} for shop in shops.SHOPS.values()}


def test_the_library_page_gets_the_shops_traits_with_the_catalog(tmp_path):
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog" / "data.js").write_text("window.CATALOG = " + json.dumps(
        {"generated": "2026-11-15T10:00:00", "sources": shops.labels(), "items": []}) + ";", encoding="utf-8")
    _, text, _ = Catalog(tmp_path).page_script()
    text = text.decode("utf-8")
    sent = json.loads(text[text.index("{"):text.rindex("}") + 1])
    assert sent["shops"]["drivethrurpg"] == {"bundles": False} and sent["shops"]["humble"] == {"bundles": True}
    assert sent["sources"]["humble"] == "Humble Bundle"


def test_a_cookie_without_a_check_of_its_own_is_a_bug_and_goes_to_no_shop(monkeypatch):
    """Each cookie login is checked by its own site; one with no check (a new shop's, forgotten) is a bug, and is
    never sent to Humble, or anywhere, to try, nor saved."""
    monkeypatch.setitem(SITES, "lanternshop", ("_lantern_session", "lanternshop-session.txt", "HYLLY_LANTERNSHOP_SESSION"))
    monkeypatch.setitem(cli.COOKIES, "lanternshop", ("_lantern_session", "shop.example.com"))
    monkeypatch.setattr("hylly.humble.HumbleClient", lambda session: pytest.fail("a lanternshop cookie went to Humble"))
    monkeypatch.setattr(cli, "read_clipboard", lambda: "l" * 80)
    with pytest.raises(RuntimeError, match="no way to check a lanternshop cookie"):
        cli.cmd_login(SimpleNamespace(site="lanternshop", cookies=None, stdin=False, clipboard=True, dry_run=False))
    assert not logins.session_path("lanternshop").exists()


def test_a_key_without_a_branch_of_its_own_in_the_login_is_a_bug_said_plainly(monkeypatch):
    """A new shop's key login that cmd_login has no branch for is said to be that bug, by its name and what it lacks,
    before anything is asked for: never a KeyError from the cookies' table, nor a key read and then dropped."""
    monkeypatch.setitem(SITES, "lanternshop", ("key", "lanternshop-key.txt", "HYLLY_LANTERNSHOP_KEY"))
    monkeypatch.setattr(cli, "read_clipboard", lambda: pytest.fail("a key read for a login with no way to take it"))
    with pytest.raises(RuntimeError, match="no way to take a lanternshop key: its branch is missing from cmd_login"):
        cli.cmd_login(SimpleNamespace(site="lanternshop", cookies=None, stdin=False, clipboard=True, dry_run=False))
    assert not logins.session_path("lanternshop").exists()


def test_humble_s_files_are_named_as_every_shop_s_and_only_those_names_are_read(tmp_path):
    """No shop is anyone's default, and Humble's login, snapshots, failures and log lines are named as the
    others' are, each shop's files in its own folder of the state folder. The old names (session.txt, orders-*.json,
    download-failures.csv) and the old places (state/humble-<when>.json, state/<shop>-failures.csv) are read no more: a
    script moves them once."""
    assert {shop: logins.session_path(shop).name for shop in ("humble", "storybundle")} == {
        "humble": "humble-session.txt", "storybundle": "storybundle-session.txt"}
    for function in (logins.session_path, logins.load_session, logins.save_session, logins.usable, logins.normalize):
        assert all(p.default is inspect.Parameter.empty for p in inspect.signature(function).parameters.values()), function
    old = paths.logins_dir() / "session.txt"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("h" * 80, encoding="utf-8")  # an old login, as it was named
    with pytest.raises(logins.AuthError, match="hylly login humble"):
        logins.load_session("humble")
    shop = shops.SHOPS["humble"]
    state = tmp_path / "state"
    state.mkdir()
    (state / "orders-2026-01-01_0000.json").write_text("{}", encoding="utf-8")
    (state / "humble-2026-01-02_0000.json").write_text("{}", encoding="utf-8")  # where an older Hylly kept them
    (state / "humble").mkdir()
    (state / "humble" / "humble-2026-01-03_0000.json").write_text("{}", encoding="utf-8")  # nor a name of another shape
    assert snapshot.latest(state, shop.id) is None  # neither old name nor old place is Humble's snapshot
    saved = snapshot.save({"K1": {"gamekey": "K1"}}, state, "humble")
    assert saved.parent == state / "humble" and re.fullmatch(r"2\d{3}-\d\d-\d\d_\d{4}\.json", saved.name)
    assert snapshot.latest(state, shop.id) == (saved, {"K1": {"gamekey": "K1"}})
    assert {s.failures for s in shops.SHOPS.values()} == {f"{s}/failures.csv" for s in shops.SHOPS}
    assert {s.state for s in shops.SHOPS.values()} == set(shops.SHOPS) and not {"locks", "lookups", "db-backup"} & set(shops.SHOPS)
    assert {s.tag for s in shops.SHOPS.values()} == {f"[{s}] " for s in shops.SHOPS}


# ---- a shop on the shops' core -------------------------------------------------------------------------------------

LANTERN = shops.Shop("lanternshop", "Lantern Shop", "Lantern Shop", "shop.example.com", "l", ("lanternshop",), ("download",),
                     "A Lantern Shop download", kind="rpg", update=True, bundles=False)
LANTERN_KEY = "l" * 32
PAGES = {("101", 0): b"%PDF-1.4 Lantern Wolf Field Guide, made up." * 20, ("101", 1): b"%PDF-1.4 the same guide, for print." * 9,
         ("101", 2): b"PK\x03\x04 maps of Moonwake, made up." * 4, ("102", 0): b"%PDF-1.4 a second guide by that name." * 5,
         ("103", 0): b"PK\x03\x04 Clan Ledger, an EPUB made up." * 6}


def lantern_titles(pages: dict) -> list[shopping.Title]:
    """What the made-up shop lists: its own key for each title and file, an MD5 for all but the maps (a size only), two
    files whose names differ only by their letters' size, two titles of one name, a program, and a book among RPGs."""
    def f(title: str, index: int, name: str, md5: bool = True) -> shopping.File:
        body = pages[(title, index)]
        return shopping.File(index, name, size=len(body), md5=hashlib.md5(body).hexdigest() if md5 else None)

    guide = [f("101", 0, "guide.pdf"), f("101", 1, "GUIDE.pdf"), f("101", 2, "maps.zip", md5=False),
             shopping.File(3, "Character Maker.exe", size=40)]
    return [shopping.Title("101", "Lantern Wolf Field Guide", "Moonwake Press", guide, purchased="2026-01-02",
                           details={"cover": "https://shop.example.com/c/101.jpg", "games": ["Traveller"],
                                    "description": "A made-up guide."}),
            shopping.Title("102", "Lantern Wolf Field Guide", "Moonwake Press", [f("102", 0, "guide.pdf")], purchased="2026-02-03"),
            shopping.Title("103", "Clan Ledger", "Duskwater Games", [f("103", 0, "ledger.epub")], kind="book")]


class Lantern:
    """A made-up shop's three things, and nothing else of its own: what the shops' core asks of a shop."""

    listed: list = []

    def __init__(self, login: str):
        self.login, self.unreadable = login, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def check(self) -> None:
        if self.login != LANTERN_KEY:
            raise logins.AuthError("Lantern Shop did not take the key.")

    def titles(self, progress=None) -> list:
        return Lantern.listed

    def link(self, title, file) -> str:
        return f"https://files.example.com/{title.key}/{file.key}"


@pytest.fixture
def lantern(monkeypatch, tmp_path):
    """The made-up shop in the table, with its login, its files served by a stand-in, its records in the database as
    every shop's are (the table takes any shop's, with no migration of its own), and a run with its options."""
    from hylly import download as dl

    monkeypatch.setitem(shops.SHOPS, LANTERN.id, LANTERN)
    monkeypatch.setitem(SITES, LANTERN.id, ("key", "lanternshop-key.txt", "HYLLY_LANTERNSHOP_KEY"))
    monkeypatch.setenv("HYLLY_LANTERNSHOP_KEY", LANTERN_KEY)
    pages = dict(PAGES)

    def serve_file(request: httpx.Request) -> httpx.Response:
        title, index = request.url.path.strip("/").split("/")
        return httpx.Response(200, content=pages[(title, int(index))])

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=httpx.MockTransport(serve_file), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)  # the room on this computer's disk is no part of it
    monkeypatch.setattr(Lantern, "listed", lantern_titles(pages))

    def run(**given) -> None:
        options = {"library": str(tmp_path), "dry_run": False, "title": None, "by": None, "kind": None, "limit": None,
                   "update": False, "jobs": 2, **given}
        cli._shop_download(SimpleNamespace(**options), LANTERN.id, Lantern)

    return SimpleNamespace(run=run, pages=pages, root=tmp_path)


def test_a_made_up_shop_comes_through_the_core_with_only_its_three_things_and_its_row(lantern, capsys):
    """A shop gives its row and three things (check, titles, link); the core chooses, names, downloads,
    records and snapshots, the catalog builds its titles, the program is left out and named, and a changed file, by the
    shop's MD5 or, where it lists none, its size, waits for --update."""
    from hylly import download as dl

    lantern.run(dry_run=True)
    out = capsys.readouterr().out
    state, folder = lantern.root / "state", "lanternshop/Moonwake Press/Lantern Wolf Field Guide"
    assert not (state / "lanternshop").exists()  # a dry run leaves nothing
    assert ("3 titles, 6 files. To download: 3 titles, 5 files, 1.7 KB. Already done: 0 files.\n"
            "  lanternshop/Duskwater Games/Clan Ledger/ledger.epub\n"
            f"  {folder}/guide.pdf\n  {folder}/GUIDE (2).pdf\n  {folder}/maps.zip\n  {folder} (2)/guide.pdf\n"
            "Left out, not failures: 1 file of a kind no download takes (programs, scripts and shortcuts):\n"
            "  'Lantern Wolf Field Guide': 'Character Maker.exe' (EXE, a program, script or shortcut)\n") in out

    lantern.run()
    manifest = dl.Manifest(lantern.root, LANTERN.id)
    assert sorted(manifest.entries) == sorted([f"{folder}/guide.pdf", f"{folder}/GUIDE (2).pdf", f"{folder}/maps.zip",
                                               f"{folder} (2)/guide.pdf", "lanternshop/Duskwater Games/Clan Ledger/ledger.epub"])
    maps = manifest.get(f"{folder}/maps.zip")
    assert (maps["shop_id"], maps["item"], maps["file"], maps["listed_size"], maps["listed_md5"]) == (
        "lanternshop:101:2", "101", 2, len(PAGES[("101", 2)]), None)
    guide = manifest.get(f"{folder}/guide.pdf")
    assert guide["listed_md5"] == hashlib.md5(PAGES[("101", 0)]).hexdigest() and "listed_size" not in guide
    assert not [k for e in manifest.entries.values() for k in e if k.startswith("lanternshop")]  # no field named by the shop
    assert (guide["kind"], guide["bundle"], guide["publisher"], guide["md5_source"]) == ("rpg", "Moonwake Press", "Moonwake Press", "shop")
    assert not (lantern.root / folder / "Character Maker.exe").exists()
    snap, = (state / "lanternshop").glob("*.json")
    assert "files.example.com" not in snap.read_text(encoding="utf-8") and LANTERN_KEY not in snap.read_text(encoding="utf-8")
    items = {i.id: i for i in catalog.reader(LANTERN.id)(lantern.root)}
    assert sorted(items) == ["l:101", "l:102", "l:103"] and (items["l:101"].kind, items["l:103"].kind) == ("rpg", "book")
    assert (items["l:101"].games, items["l:101"].description, items["l:101"].publisher) == (["Traveller"], "A made-up guide.",
                                                                                           "Moonwake Press")
    assert items["l:101"].primary[0] == f"{folder}/guide.pdf"  # the biggest file leads
    capsys.readouterr()

    lantern.run(dry_run=True)
    assert "To download: 0 titles, 0 files, 0 bytes. Already done: 5 files." in capsys.readouterr().out
    lantern.pages[("101", 0)] = b"%PDF-1.4 Lantern Wolf Field Guide, the second printing." * 20  # a new MD5
    lantern.pages[("101", 2)] = b"PK\x03\x04 maps of Moonwake, with one more." * 4  # a new size, and no MD5 listed
    Lantern.listed = lantern_titles(lantern.pages)
    lantern.run(dry_run=True)
    assert "Already done: 3 files. Changed on Lantern Shop: 2 files (run with --update to fetch them)." in capsys.readouterr().out
    lantern.run(update=True)
    assert (lantern.root / folder / "guide.pdf").read_bytes() == lantern.pages[("101", 0)]
    assert (lantern.root / "superseded" / folder / "guide.pdf").read_bytes() == PAGES[("101", 0)]  # kept, never deleted
    assert (lantern.root / folder / "maps.zip").read_bytes() == lantern.pages[("101", 2)]
    capsys.readouterr()
    lantern.run(dry_run=True)
    assert "To download: 0 titles, 0 files, 0 bytes. Already done: 5 files." in capsys.readouterr().out
    lantern.run(dry_run=True, kind=frozenset({"book"}), title="ledger")
    assert "To download: 0 titles, 0 files, 0 bytes. Already done: 1 file." in capsys.readouterr().out
    twice = [shopping.Title("104", "Lantern Wolf", "Moonwake Press"), shopping.Title("104", "Lantern Wolf", "Moonwake Press")]
    assert shopping.Selection([(t, None, f"{t.key}/{n}", False) for n, t in enumerate(twice)]).titles == 1  # by its key


def test_a_title_the_shop_lists_twice_is_downloaded_once(lantern, monkeypatch, capsys):
    """A title a shop lists twice by its key (a second purchase of it, or a StoryBundle book in a second bundle) is
    taken once, the first, and the run says how many were: its files came twice before, the second time under names
    of their own ("ledger (2).epub")."""
    from hylly import download as dl

    ledger = shopping.Title("103", "Clan Ledger", "Duskwater Games", [shopping.File(0, "ledger.epub", size=len(PAGES[("103", 0)]))],
                            kind="book")
    monkeypatch.setattr(Lantern, "listed", [*Lantern.listed, ledger])
    lantern.run()
    out = capsys.readouterr().out
    assert "To download: 3 titles, 5 files" in out and "Listed more than once: 1, each taken once." in out
    recorded = sorted(dl.Manifest(lantern.root, LANTERN.id).entries)
    assert [p for p in recorded if "Clan Ledger" in p] == ["lanternshop/Duskwater Games/Clan Ledger/ledger.epub"]


def test_after_the_records_are_named_alike_every_shop_finds_what_it_downloaded_before(tmp_path):
    """The records of the first release, rewritten as the database opens, are found as they were by each shop's
    choice of what to download: a file downloaded before is done, at every shop, and nothing comes a second time."""
    from hylly import db, download as dl, itchio, storybundle

    lantern_wolf = {"path": "itchio/Moonwake Press/Lantern Wolf/lantern-wolf.pdf", "size": 45, "md5": "e" * 32, "itchio_id": 21,
                    "itchio_upload": 2100, "itchio_md5": "e" * 32, "itchio_size": 45, "creator": "Moonwake Press"}
    at_the_first_step(db.file(), [("drivethrurpg", DT_OLD, {}), ("storybundle", SB_OLD, {}), ("itchio", lantern_wolf, {})])
    for e in (DT_OLD, SB_OLD, lantern_wolf):
        (tmp_path / e["path"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / e["path"]).write_bytes(b"x" * e["size"])
    guide = shopping.Title("1001", "Lantern Wolf Field Guide", "Moonwake Press", [shopping.File(0, "field_guide.pdf", 88, "d" * 32)])
    found = shopping.select("drivethrurpg", [guide], dl.Manifest(tmp_path, "drivethrurpg"), tmp_path)
    assert (found.pending, found.done, found.changed) == ([], 1, 0)
    tide = storybundle.Bundle("The Moonwake Bundle", "2024-05-01", "", [storybundle.Book("78", "Second Tide", 30, "")])
    found = shopping.select("storybundle", storybundle.titles_of([tide]), dl.Manifest(tmp_path, "storybundle"), tmp_path)
    assert (found.pending, found.done) == ([], 1)  # on the shops' core now: its record's item, a string, and file 0
    wolf = itchio.Item(21, "Lantern Wolf", "Moonwake Press", "physical_game", key_id=1,
                       uploads=[itchio.Upload(2100, "lantern-wolf.pdf", 45, "e" * 32)])
    found = shopping.select("itchio", [itchio.title_of(wolf)], dl.Manifest(tmp_path, "itchio"), tmp_path)  # by its numbers
    assert (found.pending, found.done, found.noted) == ([], 1, [])


# ---- the core's traits, each a shop's own --------------------------------------------------------------------------

MARKET = shops.Shop("moonwake", "Moonwake Market", "Moonwake Market", "market.example.com", "m", ("moonwake",), ("download",),
                    "A Moonwake Market download", exact=True, link_refreshes=4, beside=True, maker="Makers unknown")
TIDE = b"PK\x03\x04 Second Tide, made up for the test." * 8
QUIET = b"%PDF-1.4 Quiet Engineers, made up." * 6


class Listed(shopping.File):
    """A file of a shop with rules of its own (File.left): it fetches no sound."""

    def left(self, kind: str) -> str:
        return "not on the shop's list to fetch" if kind == "mp3" else ""


class Market(Lantern):
    """A made-up shop whose titles come in bundles, one of them with no maker named, and one it can't read."""

    def __init__(self, login: str):
        super().__init__(login)
        self.unreadable = [("203", "Lantern Almanac")]

    def check(self) -> None:
        pass

    def titles(self, progress=None) -> list:
        return Market.listed


def market_titles(pages: dict) -> list[shopping.Title]:
    book = lambda key, name: Listed(key, name, size=len(pages[key]), md5=hashlib.md5(pages[key]).hexdigest())  # noqa: E731
    return [shopping.Title("201", "Second Tide", "Ann Writer", [book("201a", "tide.epub"), Listed("201b", "tide.mp3", size=50)],
                           under="The Moonwake Bundle"),
            shopping.Title("202", "Quiet Engineers", "", [book("202a", "quiet.pdf")])]


def test_a_shop_s_own_ways_are_its_traits_and_the_core_follows_them(monkeypatch, tmp_path, capsys):
    """The core's DriveThruRPG-shaped bits, a shop's own traits (shops.SHOPS) now, for StoryBundle, itch.io and Humble to
    follow: the folder a title's own goes in apart from who made it (Title.under: its bundle), the shop's word for a maker
    it doesn't name (Shop.maker), how exact its listing is (Shop.exact) and how often a refused link is asked for afresh
    (Shop.link_refreshes), a changed file coming beside the old one, every run (Shop.beside), and a reason of the
    shop's own to leave a file out (File.left), named with the never-list's."""
    from hylly import download as dl

    pages = {"201a": TIDE, "202a": QUIET}
    monkeypatch.setitem(shops.SHOPS, MARKET.id, MARKET)
    monkeypatch.setitem(SITES, MARKET.id, ("key", "moonwake-key.txt", "HYLLY_MOONWAKE_KEY"))
    monkeypatch.setenv("HYLLY_MOONWAKE_KEY", "m" * 32)
    monkeypatch.setattr(Market, "listed", market_titles(pages))
    asked = []

    def downloader(*args, **kw):
        asked.append(kw["link_refreshes"])
        served = httpx.MockTransport(lambda request: httpx.Response(200, content=pages[request.url.path.rsplit("/", 1)[1]]))
        return real(*args, **{**kw, "transport": served, "backoff": 0})

    real = dl.Downloader
    monkeypatch.setattr(dl, "Downloader", downloader)
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    options = {"library": str(tmp_path), "dry_run": True, "title": None, "by": None, "kind": None, "limit": None, "jobs": 1}

    cli._shop_download(SimpleNamespace(**options), MARKET.id, Market)
    out = capsys.readouterr().out
    tide, quiet = "moonwake/The Moonwake Bundle/Second Tide", "moonwake/Makers unknown/Quiet Engineers"
    assert f"  {quiet}/quiet.pdf\n  {tide}/tide.epub\n" in out  # by the folder each goes in, a title's bundle or its maker
    assert ("Left out, not failures: 1 file no download takes:\n"
            "  'Second Tide': 'tide.mp3' (MP3, not on the shop's list to fetch)\n") in out

    cli._shop_download(SimpleNamespace(**{**options, "dry_run": False}), MARKET.id, Market)
    assert asked == [4]  # its own number of fresh links
    record = dl.Manifest(tmp_path, MARKET.id).get(f"{tide}/tide.epub")
    assert (record["item"], record["file"], record["bundle"], record["publisher"], record["listed_md5"]) == (
        "201", "201a", "The Moonwake Bundle", "Ann Writer", hashlib.md5(TIDE).hexdigest())
    second, epub = Market.listed[0], Market.listed[0].files[0]
    assert shopping.make_job(MARKET.id, second, epub, f"{tide}/tide.epub").exact  # its listing taken as exact
    assert not shopping.make_job("drivethrurpg", second, epub, "drivethrurpg/x/tide.epub").exact
    almanac, = [e for e in snapshot.latest(tmp_path / "state", MARKET.id)[1] if e["id"] == "203"]
    assert almanac["publisher"] == "Makers unknown"  # what the shop can't read, with its word for a maker unknown
    capsys.readouterr()

    pages["201a"] = TIDE + b" A second printing."  # the shop changes the file: it comes beside the old one, every run
    monkeypatch.setattr(Market, "listed", market_titles(pages))
    cli._shop_download(SimpleNamespace(**options), MARKET.id, Market)
    out = capsys.readouterr().out
    assert "To download: 1 title, 1 file" in out and "--update" not in out and "Changed on Moonwake Market: " not in out
    assert ("Changed on Moonwake Market:\n  'Second Tide': 'tide.epub' was replaced on Moonwake Market: the new one comes "
            "as 'tide (2).epub', beside the old one, 'tide.epub', which stays\n") in out
    cli._shop_download(SimpleNamespace(**{**options, "dry_run": False}), MARKET.id, Market)
    assert (tmp_path / tide / "tide.epub").read_bytes() == TIDE and (tmp_path / tide / "tide (2).epub").read_bytes() == pages["201a"]
    capsys.readouterr()
    cli._shop_download(SimpleNamespace(**options), MARKET.id, Market)
    assert "To download: 0 titles, 0 files, 0 bytes. Already done: 2 files." in capsys.readouterr().out  # the newer counts


def test_of_a_file_s_records_the_newer_counts_kept_with_its_offset_or_before_records_had_one():
    """Downloads are recorded with their time's offset (db.now), those from before with the local time alone, read as
    this computer's then: of one file's records side by side, the one downloaded later counts, by the moment."""
    from datetime import timedelta, timezone

    for when, base in CLOCK_TIMES.items():  # in summer, in winter, and in the hour the clocks go back
        before = {"downloaded_at": (base - timedelta(hours=3)).isoformat()}
        after = {"downloaded_at": (base.astimezone() - timedelta(hours=1)).isoformat()}
        elsewhere = {"downloaded_at": base.astimezone(timezone(timedelta(hours=-9))).isoformat()}  # another zone's
        assert sorted([elsewhere, after, before], key=shopping._downloaded) == [before, after, elsewhere], when
    assert shopping._downloaded({}) == shopping._downloaded({"downloaded_at": "soon"}) == float("-inf")  # the oldest


def two_titles(shop_id: str) -> list:
    """Two titles of a shop, as its own code makes them, each with one EPUB: "Moonwake" and "Second Tide" (keys 71 and
    72, as the shop keys them), the first from a bundle whose name has no word of a kind, by a maker with none."""
    from hylly import drivethrurpg, itchio, storybundle

    body = 2 << 20  # a small EPUB: a book's, on an e-reader
    if shop_id == "storybundle":
        books = [storybundle.Book(key, name, body, "") for key, name in (("71", "Moonwake"), ("72", "Second Tide"))]
        titles = storybundle.titles_of([storybundle.Bundle("The Lantern Tales Bundle", "2020-01-01", "", books)])
        return [dataclasses.replace(t, files=[dataclasses.replace(t.files[0], name=f"{t.name}.epub")]) for t in titles]
    if shop_id == "drivethrurpg":
        return [drivethrurpg.Product(id=key, order_id=1, name=name, publisher="Duskwater Press",
                                     files=[drivethrurpg.File(0, f"{name}.epub", None, body)])
                for key, name in (("71", "Moonwake"), ("72", "Second Tide"))]
    return [itchio.title_of(itchio.Item(game_id=key, title=name, creator="Ann Writer", classification="book", key_id=1,
                                        uploads=[itchio.Upload(key * 10, f"{name}.epub", body, f"md5-{key}")]))
            for key, name in ((71, "Moonwake"), (72, "Second Tide"))]


@pytest.mark.parametrize("shop_id", ["storybundle", "drivethrurpg", "itchio"])
def test_a_kind_you_choose_finds_its_title_by_the_catalog_id_and_steers_only_what_isn_t_downloaded(shop_id, tmp_path):
    """A correction made on the library page or with hylly set kind names a title by its catalog id, its shop's
    letter and its key (curation.kinds), which the catalog's reader gives it and the core reads it by (corrected): a title
    not downloaded yet is downloaded as that kind, read where that kind is (make_job's kind and device) and chosen by
    --kind by it; one downloaded keeps its files, as a download goes by the records, so nothing comes again."""
    from hylly import curation
    from hylly import download as dl

    shop, titles = shops.SHOPS[shop_id], two_titles(shop_id)
    downloaded, waiting = titles
    path = f"{shop.folder}/x/{downloaded.name}/{downloaded.files[0].name}"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_bytes(b"PK\x03\x04 made up")
    manifest = dl.Manifest(tmp_path, shop.id)
    manifest.add({"path": path, "size": (tmp_path / path).stat().st_size, "item": downloaded.key, "file": downloaded.files[0].key,
                  "ext": "epub", "md5": downloaded.files[0].md5, "listed_md5": downloaded.files[0].md5})
    shopping.snapshot(shop.id, titles, tmp_path / "state")
    (item,) = catalog.reader(shop.id)(tmp_path)  # the one downloaded: its id on the library page
    own = "rpg" if shop_id == "drivethrurpg" else "book"  # its shop's kind, no word saying otherwise
    assert item.id == f"{shop.letter}:{downloaded.key}" and item.kind == shopping.kind_found(shop.id, downloaded) == own
    before = shopping.make_job(shop.id, waiting, waiting.files[0], "x/y.epub")
    assert (before.kind, before.device) == (own, "tablet" if own == "rpg" else "ereader")

    ids = [item.id, f"{shop.letter}:{waiting.key}"]  # the library page's, and one of a title not in the catalog yet
    curation.change_single(tmp_path, "kind", "tech", [{"id": i, "t": "", "f": [[""]]} for i in ids], by=curation.COMMAND_LINE)
    titles = two_titles(shop_id)
    assert shopping.corrected(shop.id, titles, curation.kinds(tmp_path)) == 2
    downloaded, waiting = titles
    assert shopping.kind_found(shop.id, waiting) == "tech" and shopping.kind_found(shop.id, downloaded) == "tech"
    sel = shopping.select(shop.id, titles, manifest, tmp_path, kinds={"tech"})
    assert [(t.key, f.key) for t, f, _, _ in sel.pending] == [(waiting.key, waiting.files[0].key)] and sel.done == 1  # kept
    job = shopping.make_job(shop.id, waiting, waiting.files[0], sel.pending[0][2])
    assert (job.kind, job.device) == ("tech", "either")  # read where a tech book is
    assert shopping.select(shop.id, titles, manifest, tmp_path, kinds={own}).pending == []


def test_every_shop_s_titles_are_found_their_kind_by_their_words_over_the_shop_s_own(tmp_path):
    """The bundle words and the publishers go over each shop's own kind, the same at every shop: a StoryBundle
    book in a comics bundle is a comic, a DriveThruRPG product by a comics publisher a comic, an itch.io book in a
    tabletop bundle an RPG book (by its PDF: a small EPUB would be a novel), and the snapshot keeps the shop's own word."""
    from hylly import drivethrurpg, itchio, storybundle

    rules_folder = paths.settings_file().parent / "rules"
    rules_folder.mkdir(exist_ok=True)
    (rules_folder / "mine.toml").write_text('[kinds.comic]\npublishers_add = ["moonwake comics"]\n', encoding="utf-8")
    rules.reset()
    (book,) = storybundle.titles_of([storybundle.Bundle("The Lantern Comics Bundle", "2020-01-01", "",
                                                        [storybundle.Book("9", "Moonwake", 1 << 20, "")])])
    product = drivethrurpg.Product(id="9", order_id=1, name="Moonwake", publisher="Moonwake Comics",
                                   files=[drivethrurpg.File(0, "moonwake.pdf", None, 30 << 20)])
    item = itchio.Item(game_id=9, title="Moonwake", creator="Ann Writer", classification="book", key_id=1,
                       uploads=[itchio.Upload(90, "moonwake.pdf", 30 << 20, "md5-9")], bundles=["Tabletop Games for Moonwake"])
    zine = itchio.title_of(item)
    assert zine.kind == "book"  # its own word, its classification's kind, which the snapshot keeps
    assert [shopping.classify(s, t) for s, t in (("storybundle", book), ("drivethrurpg", product), ("itchio", zine))] == [
        ("comic", 'bundle "Comics"'), ("comic", "publisher Moonwake Comics"), ("rpg", 'bundle "Tabletop"')]
    tabletop = itchio.title_of(dataclasses.replace(item, classification="physical_game", bundles=[]))
    assert (tabletop.kind, shopping.kind_found("itchio", tabletop)) == ("rpg", "rpg")  # itch.io's classification, mapped
    shopping.snapshot("itchio", [zine], tmp_path / "state")
    assert snapshot.latest(tmp_path / "state", "itchio")[1][0]["kind"] == "book" and "found" not in str(
        snapshot.latest(tmp_path / "state", "itchio")[1])


def test_a_kind_you_choose_steers_a_run_at_a_shop_on_the_core(lantern, capsys):
    """Through hylly SHOP download: --kind takes a title by the kind you chose for it, its record keeps that kind
    and where it is read, the snapshot keeps what the shop said, and a kind chosen for a title downloaded already
    downloads nothing again."""
    from hylly import curation
    from hylly import download as dl

    curation.change_single(lantern.root, "kind", "comic", [{"id": "l:103", "t": "Clan Ledger", "f": [[""]]}],
                           by=curation.COMMAND_LINE)  # the shop's own word for it is "book"
    lantern.run(kind=frozenset({"comic"}), dry_run=True)
    out = capsys.readouterr().out
    assert "To download: 1 title, 1 file" in out and "lanternshop/Duskwater Games/Clan Ledger/ledger.epub" in out
    lantern.run(kind=frozenset({"comic"}))
    ledger = dl.Manifest(lantern.root, LANTERN.id).get("lanternshop/Duskwater Games/Clan Ledger/ledger.epub")
    assert (ledger["kind"], ledger["device"]) == ("comic", "tablet")  # where a comic is read, not a book's e-reader
    snap = snapshot.latest(lantern.root / "state", LANTERN.id)[1]
    assert next(e for e in snap if e["id"] == "103")["kind"] == "book"  # what the shop said: yours is in the database
    lantern.run()
    capsys.readouterr()
    curation.change_single(lantern.root, "kind", "tech", [{"id": "l:101", "t": "", "f": [[""]]}], by=curation.COMMAND_LINE)
    lantern.run(dry_run=True)
    assert "To download: 0 titles, 0 files, 0 bytes. Already done: 5 files." in capsys.readouterr().out


# ---- what the rules can't settle: the questions --------------------------------------------------------------------

FIELD_GUIDE, LEDGER = "lanternshop/Moonwake Press/Lantern Wolf Field Guide", "lanternshop/Duskwater Games/Clan Ledger"


def test_a_file_gone_from_its_place_is_put_to_you_and_comes_again_as_the_guess(lantern, capsys):
    """A file, or a title's whole folder, gone from where it was downloaded (moved by hand,
    say) comes again to its place as it always has, and is put to you, one question for a folder; a dry run shows them
    and keeps none; once the files are back a run finds them no more, and they are gone from the list."""
    import shutil

    from hylly import curation

    lantern.run()
    (lantern.root / FIELD_GUIDE / "maps.zip").unlink()
    shutil.rmtree(lantern.root / LEDGER)
    capsys.readouterr()
    lantern.run(dry_run=True)
    out = capsys.readouterr().out
    assert "To download: 2 titles, 2 files" in out and curation.questions(lantern.root) == []
    assert out.endswith("2 questions to decide, all new; a dry run keeps no new one, so hylly decide lists them once a "
                        "run has found them.\n")
    lantern.run()
    assert capsys.readouterr().out.endswith("2 questions to decide, all new: hylly decide lists them and takes your answers.\n")
    found = curation.questions(lantern.root)
    assert [(q["kind"], q["catalog_id"], q["file"], q["guess"], q["held"]) for q in found] == [
        ("moved", "l:103", "", 0, False), ("moved", "l:101", "2", 0, False)]  # the ledger's folder; the guide's maps
    assert found[0]["why"] == f"its folder, {LEDGER}, is gone from the library"
    assert found[1]["options"] == list(curation.MOVED) and found[1]["why"] == f"maps.zip is gone from its folder, {FIELD_GUIDE}"
    assert (lantern.root / FIELD_GUIDE / "maps.zip").is_file() and (lantern.root / LEDGER / "ledger.epub").is_file()  # the guess
    lantern.run()
    assert "question" not in capsys.readouterr().out and curation.questions(lantern.root) == []  # found no more: gone


def test_with_unsure_ask_a_moved_file_waits_for_your_answer_which_is_an_edit(lantern, capsys):
    """With [download] unsure = "ask" a file gone from its place is held back, and the run says how many; hylly
    decide lists the question (and --json), hylly doctor counts it, and an answer is an edit: "I moved it" never fetches
    the file again, which the runs count, until hylly set answers auto takes it back and the run asks again. A value
    other than guess or ask is a mistake hylly settings check names."""
    from hylly import curation, doctor, settings

    lantern.run()
    paths.settings_file().write_text('[download]\nunsure = "ask"\n', encoding="utf-8")
    settings.reload()  # as the next run reads them
    (lantern.root / FIELD_GUIDE / "maps.zip").unlink()
    capsys.readouterr()
    lantern.run()
    out = capsys.readouterr().out
    assert ("To download: 0 titles, 0 files, 0 bytes. Already done: 4 files. Held back for your answer: 1 file (unsure = "
            '"ask" under [download]).') in out and not (lantern.root / FIELD_GUIDE / "maps.zip").exists()
    (q,) = curation.questions(lantern.root)
    assert (q["kind"], q["catalog_id"], q["held"], q["options"][q["guess"]]["answer"]) == ("moved", "l:101", True, "again")
    (said,) = [c for c in doctor.report(str(lantern.root))["checks"] if c["what"] == "Questions"]
    assert (said["says"], said["next"]) == ("1 question to decide from the downloads (Lantern Shop), 1 of them with a file "
                                            "held back for your answer.", "hylly decide lists them and takes your answers.")
    cli.main(["decide", "--library", str(lantern.root)])
    out = capsys.readouterr().out
    assert (f"1 question to decide, from the downloads:\n{q['id']:>4}  Lantern Shop: Lantern Wolf Field Guide (l:101, 2). "
            "Its file is gone from where it was downloaded: download it again? (maps.zip is gone from its folder, "
            f"{FIELD_GUIDE})\n        1  Download it again  <- the guess, held back for your answer\n"
            "        2  I moved it by hand: never fetch it again\n") in out
    cli.main(["decide", "--json", "--library", str(lantern.root)])
    assert json.loads(capsys.readouterr().out)["questions"] == [q]
    cli.main(["decide", str(q["id"]), "2", "--dry-run", "--library", str(lantern.root)])
    assert "It would answer question" in capsys.readouterr().out and curation.questions(lantern.root) == [q]
    cli.main(["decide", str(q["id"]), "2", "--library", str(lantern.root)])
    assert (f"Answered question {q['id']}, Lantern Wolf Field Guide (l:101), with I moved it by hand: never fetch it again, "
            f"which never fetches it again: it stays where you moved it (hylly decide {q['id']} auto takes it "
            "back).") in capsys.readouterr().out  # that one answer alone
    assert curation.questions(lantern.root) == [] and curation.answers(lantern.root)["l:101"]["moved:2"]["a"] == "moved"
    lantern.run()
    out = capsys.readouterr().out
    assert "Moved by hand, as you said, and not fetched again: 1 file." in out and "Held back" not in out
    assert "question" not in out and not (lantern.root / FIELD_GUIDE / "maps.zip").exists()
    cli.main(["set", "answers", "auto", "--id", "l:101", "--yes", "--library", str(lantern.root)])  # taken back
    assert "Took back your answers to its questions for 1 title" in capsys.readouterr().out
    lantern.run()
    assert "Held back for your answer: 1 file" in capsys.readouterr().out and len(curation.questions(lantern.root)) == 1
    paths.settings_file().write_text('[download]\nunsure = "maybe"\n', encoding="utf-8")
    assert any("unsure = 'maybe': use guess or ask" in m for m in settings.problems()[0])


def test_a_file_the_shop_renames_is_the_same_file_and_comes_not_again(lantern, monkeypatch, capsys):
    """A file the shop lists anew under another key and name, the very file at its recorded
    place (its MD5; where the shop lists none, the stem of its name and its size), isn't fetched again under the new
    name: its record keeps its place, and every run says so. No question is asked."""
    lantern.run()
    guide, *others = lantern_titles(lantern.pages)
    pdf, print_copy, maps, program = guide.files
    renamed = dataclasses.replace(guide, files=[dataclasses.replace(pdf, key=10, name="guide-2nd.pdf"), print_copy,
                                                dataclasses.replace(maps, key=12, name="MAPS.zip"), program])
    monkeypatch.setattr(Lantern, "listed", [renamed, *others])
    capsys.readouterr()
    lantern.run(dry_run=True)
    out = capsys.readouterr().out
    assert "To download: 0 titles, 0 files, 0 bytes. Already done: 5 files." in out and "question" not in out
    assert ("Changed on Lantern Shop:\n"
            "  'Lantern Wolf Field Guide': 'guide-2nd.pdf' is the same file as 'guide.pdf', downloaded before: not fetched again\n"
            "  'Lantern Wolf Field Guide': 'MAPS.zip' is the same file as 'maps.zip', downloaded before: not fetched again\n") in out
    bigger = dataclasses.replace(maps, key=12, name="maps.zip", size=maps.size + 1)  # no MD5, another size: another file
    monkeypatch.setattr(Lantern, "listed", [dataclasses.replace(guide, files=[pdf, print_copy, bigger, program]), *others])
    lantern.run(dry_run=True)
    assert "To download: 1 title, 1 file" in capsys.readouterr().out


@pytest.mark.parametrize("shop_id", ["humble", "drivethrurpg", "itchio", "storybundle"])
def test_the_same_file_under_another_name_is_found_at_every_shop(shop_id, tmp_path):
    """At every shop a file listed anew under another key and name that is the very file of the title's here (its
    MD5, or where the shop lists none, the stem of its name and its size) is done, and said; Humble's by its title's
    folder, as its records go by their places. StoryBundle's listing has no MD5, names a file only in its link and
    rounds its size: nothing tells a book listed anew from another there, so it is fetched."""
    from hylly import download as dl
    from hylly import drivethrurpg, humble, itchio, storybundle

    body = b"%PDF-1.4 Lantern Wolf, made up." * 40
    md5, shop = hashlib.md5(body).hexdigest(), shops.SHOPS[shop_id]
    folder = f"{shop.folder}/Moonwake Press/Lantern Wolf"
    (tmp_path / folder).mkdir(parents=True)
    (tmp_path / folder / "lantern.pdf").write_bytes(body)
    record = {"path": f"{folder}/lantern.pdf", "size": len(body), "md5": md5, "ext": "pdf"}
    if shop_id == "humble":
        record.update(item="lantern", file="lantern.pdf")
        titles = [humble.Title("lantern", "Lantern Wolf", "Moonwake Press", folder=folder, files=[
            humble.File("lantern-v2.pdf", "lantern-v2.pdf", len(body), md5, ext="pdf")])]
    elif shop_id == "drivethrurpg":
        record.update(item="71", file=0, listed_md5=md5)
        titles = [drivethrurpg.Product("71", 1, "Lantern Wolf", "Moonwake Press",
                                       files=[drivethrurpg.File(5, "lantern-v2.pdf", md5, len(body))])]
    elif shop_id == "itchio":
        record.update(item=71, file=700, listed_md5=md5, listed_size=len(body))
        titles = [itchio.title_of(itchio.Item(71, "Lantern Wolf", "Moonwake Press", "book", key_id=1,
                                              uploads=[itchio.Upload(7100, "lantern-v2.pdf", len(body), md5)]))]
    else:
        record.update(item="78", file=0)
        titles = storybundle.titles_of([storybundle.Bundle("Moonwake Press", "2024-01-01", "", [
            storybundle.Book("79", "Lantern Wolf", len(body), "")])])
    manifest = dl.Manifest(tmp_path, shop_id)
    manifest.add(record)
    sel = shopping.select(shop_id, titles, manifest, tmp_path)
    if shop_id == "storybundle":
        assert (len(sel.pending), sel.done, sel.noted) == (1, 0, [])
        return
    assert (sel.pending, sel.done, sel.questions) == ([], 1, [])
    assert sel.noted == ["  'Lantern Wolf': 'lantern-v2.pdf' is the same file as 'lantern.pdf', downloaded before: not "
                         "fetched again"]
    if shop_id == "drivethrurpg":  # no MD5 listed, now nor then: the stem of its name, and its size
        del record["listed_md5"]
        manifest.add(record)
        for name, same in (("LANTERN.pdf", True), ("lantern-v2.pdf", False), ("lantern.epub", False)):  # never another format
            listed = [drivethrurpg.Product("71", 1, "Lantern Wolf", "Moonwake Press",
                                           files=[drivethrurpg.File(5, name, None, len(body))])]
            assert shopping.select(shop_id, listed, manifest, tmp_path).done == same, name


@pytest.mark.parametrize("shop_id", ["humble", "drivethrurpg", "itchio", "storybundle"])
def test_a_file_gone_from_its_place_is_a_question_at_every_shop_and_your_answer_settles_it(shop_id, tmp_path,
                                                                                          monkeypatch):
    """At every shop, as its own code makes its titles: a file recorded here but gone from its place comes again
    there, the guess, and is a question; with ask it is held back; I moved it never fetches it again, and download it
    again asks no more; the title's whole folder gone is one question for the title; and a library that isn't there
    asks nothing."""
    from hylly import curation
    from hylly import download as dl
    from hylly import drivethrurpg, humble, itchio, storybundle

    body = b"%PDF-1.4 Lantern Wolf, made up." * 40
    md5, shop = hashlib.md5(body).hexdigest(), shops.SHOPS[shop_id]
    folder = f"{shop.folder}/Moonwake Press/Lantern Wolf"
    (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "library.json").write_text("{}", encoding="utf-8")
    record = {"path": f"{folder}/lantern.pdf", "size": len(body), "md5": md5, "ext": "pdf", "listed_md5": md5}
    if shop_id == "humble":
        record.update(item="lantern", file="lantern.pdf")
        titles = [humble.Title("lantern", "Lantern Wolf", "Moonwake Press", folder=folder, files=[
            humble.File("lantern.pdf", "lantern.pdf", len(body), md5, ext="pdf")])]
    elif shop_id == "drivethrurpg":
        record.update(item="71", file=0)
        titles = [drivethrurpg.Product("71", 1, "Lantern Wolf", "Moonwake Press",
                                       files=[drivethrurpg.File(0, "lantern.pdf", md5, len(body))])]
    elif shop_id == "itchio":
        record.update(item=71, file=700, listed_size=len(body))
        titles = [itchio.title_of(itchio.Item(71, "Lantern Wolf", "Moonwake Press", "book", key_id=1,
                                              uploads=[itchio.Upload(700, "lantern.pdf", len(body), md5)]))]
    else:
        record.update(item="71", file=0, listed_md5=None)
        titles = storybundle.titles_of([storybundle.Bundle("Moonwake Press", "2024-01-01", "", [
            storybundle.Book("71", "Lantern Wolf", len(body), "")])])
    manifest = dl.Manifest(tmp_path, shop_id)
    manifest.add(record)
    (tmp_path / folder / "notes.txt").write_text("the folder stays", encoding="utf-8")  # the file alone is gone
    cid = f"{shop.letter}:71" if shop_id != "humble" else "h:lantern"
    guess = shopping.select(shop_id, titles, manifest, tmp_path)
    (q,) = guess.questions
    assert (q["kind"], q["file"], q["guess"], q["held"], q["options"]) == ("moved", str(record["file"]), 0, False,
                                                                         list(curation.MOVED))
    assert [p for _, _, p, _ in guess.pending] == [record["path"]]  # the guess: again, to its place
    held = shopping.select(shop_id, titles, manifest, tmp_path, ask=True)
    assert (held.pending, held.held, held.questions[0]["held"]) == ([], 1, True)
    for word, pending, forgotten in (("moved", [], 1), ("again", [record["path"]], 0)):
        answers = {cid: {f"moved:{record['file']}": {"a": word, "o": curation.fingerprint(list(curation.MOVED))}}}
        said = shopping.select(shop_id, titles, manifest, tmp_path, ask=True, answers=answers)
        assert ([p for _, _, p, _ in said.pending], said.forgotten, said.questions, said.held) == (pending, forgotten, [], 0)
    (tmp_path / folder / "notes.txt").unlink()
    (tmp_path / folder).rmdir()  # its whole folder gone: one question for the title
    assert [(q["kind"], q["file"]) for q in shopping.select(shop_id, titles, manifest, tmp_path).questions] == [("moved", "")]
    (tmp_path / "state" / "library.json").unlink()  # the library itself not there: no question, as before
    with monkeypatch.context() as m:
        m.setattr(paths, "REQUIRE_MARKER", True)
        assert shopping.select(shop_id, titles, manifest, tmp_path).questions == []


def moved_by_hand(tmp_path, shop_id: str, keys=(0, 1)):
    """A title of the shop's with two files downloaded and recorded in its folder, as its own code makes it: (titles,
    the manifest, the folder, the files' records, its catalog id, a function giving the title with its files listed
    under other keys and names, the very files)."""
    from hylly import download as dl
    from hylly import drivethrurpg, humble

    shop = shops.SHOPS[shop_id]
    folder = f"{shop.folder}/Moonwake Press/Lantern Wolf"
    (tmp_path / folder).mkdir(parents=True)
    bodies = [b"%PDF-1.4 Lantern Wolf, made up." * (40 + n) for n in range(2)]
    names = ["lantern.pdf", "maps.pdf"]
    manifest, records = dl.Manifest(tmp_path, shop_id), []
    for n, (name, body) in enumerate(zip(names, bodies)):
        (tmp_path / folder / name).write_bytes(body)
        md5 = hashlib.md5(body).hexdigest()
        record = {"path": f"{folder}/{name}", "size": len(body), "md5": md5, "ext": "pdf", "listed_md5": md5,
                  "item": "lantern" if shop_id == "humble" else "71", "file": name if shop_id == "humble" else keys[n]}
        manifest.add(record)
        records.append(record)

    def listed(renamed: bool = False, md5: bool = True, rekey: bool = False) -> list:
        given = [(f"{name[:-4]}-v2.pdf" if renamed else name, body, hashlib.md5(body).hexdigest() if md5 else None)
                 for name, body in zip(names, bodies)]
        if shop_id == "humble":
            return [humble.Title("lantern", "Lantern Wolf", "Moonwake Press", folder=folder, files=[
                humble.File(name, name, len(body), digest, ext="pdf") for name, body, digest in given])]
        return [drivethrurpg.Product("71", 1, "Lantern Wolf", "Moonwake Press", files=[
            drivethrurpg.File(k + (10 if renamed or rekey else 0), name, digest, len(body))
            for k, (name, body, digest) in zip(keys, given)])]

    cid = "h:lantern" if shop_id == "humble" else "d:71"
    return listed, manifest, folder, records, cid


def moved_answer(word: str) -> dict:
    from hylly import curation

    return {"a": word, "o": curation.fingerprint(list(curation.MOVED))}


@pytest.mark.parametrize("shop_id", ["humble", "drivethrurpg"])
def test_your_moved_answer_holds_whether_the_folder_is_there_or_not(shop_id, tmp_path):
    """The answer "I moved it" for one file holds when the rest of its folder goes too (then asked of
    the title, but for that file); for the whole title, asked while its folder was gone, it covers each of its files once
    the folder is back (a new file there, say). Download it again likewise."""
    listed, manifest, folder, records, cid = moved_by_hand(tmp_path, shop_id)
    first, second = (str(r["file"]) for r in records)
    (tmp_path / records[0]["path"]).unlink()  # one file moved away by hand
    answers = {cid: {f"moved:{first}": moved_answer("moved")}}
    sel = shopping.select(shop_id, listed(), manifest, tmp_path, answers=answers)
    assert (sel.pending, sel.forgotten, sel.questions) == ([], 1, [])
    (tmp_path / records[1]["path"]).unlink()
    (tmp_path / folder).rmdir()  # the rest of the folder away too: the title is asked about, that file not counted
    sel = shopping.select(shop_id, listed(), manifest, tmp_path, answers=answers, ask=True)
    assert sel.forgotten == 1 and [(q["file"], q["held"]) for q in sel.questions] == [("", True)] and sel.held == 1
    title = {cid: {"moved:": moved_answer("moved")}}  # asked of the title while its folder was gone
    sel = shopping.select(shop_id, listed(), manifest, tmp_path, answers=title)
    assert (sel.pending, sel.forgotten, sel.questions) == ([], 2, [])
    (tmp_path / folder).mkdir()
    (tmp_path / folder / "notes.txt").write_text("the folder is back", encoding="utf-8")
    sel = shopping.select(shop_id, listed(), manifest, tmp_path, answers=title)
    assert (sel.pending, sel.forgotten, sel.questions) == ([], 2, [])  # each file of the title, its folder there or not
    again = {cid: {"moved:": moved_answer("again")}}
    sel = shopping.select(shop_id, listed(), manifest, tmp_path, answers=again, ask=True)
    assert len(sel.pending) == 2 and (sel.forgotten, sel.questions, sel.held) == (0, [], 0)


@pytest.mark.parametrize("shop_id", ["humble", "drivethrurpg"])
def test_a_file_you_moved_stays_forgotten_when_the_shop_renames_it(shop_id, tmp_path):
    """A file you said you moved by hand, listed anew under another key and name, is the
    very file (its MD5, or where neither listing has one, its name and size): not downloaded, and no question; a file
    of another name and no MD5 is another file."""
    listed, manifest, folder, records, cid = moved_by_hand(tmp_path, shop_id)
    for r in records:
        (tmp_path / r["path"]).unlink()
    answers = {cid: {f"moved:{records[0]['file']}": moved_answer("moved"), f"moved:{records[1]['file']}": moved_answer("moved")}}
    sel = shopping.select(shop_id, listed(renamed=True), manifest, tmp_path, answers=answers, ask=True)
    assert (sel.pending, sel.forgotten, sel.questions, sel.held) == ([], 2, [], 0)
    sel = shopping.select(shop_id, listed(renamed=True), manifest, tmp_path)  # no answer: both are other files now
    assert len(sel.pending) == 2 and sel.forgotten == 0
    if shop_id == "drivethrurpg":  # no MD5 listed, now nor then: by its whole name and size
        for r in records:
            manifest.add({**r, "listed_md5": None})
        sel = shopping.select(shop_id, listed(rekey=True, md5=False), manifest, tmp_path, answers=answers)  # other keys
        assert (sel.pending, sel.forgotten, sel.questions) == ([], 2, [])  # the same names and sizes: the very files
        sel = shopping.select(shop_id, listed(renamed=True, md5=False), manifest, tmp_path, answers=answers)
        assert len(sel.pending) == 2 and sel.forgotten == 0  # another name and no MD5: another file


def test_a_reworded_answer_still_holds_as_answers_go_by_their_codes(tmp_path, monkeypatch):
    """An answer is kept by the question's codes and its files' names and sizes, never
    the words people read: put better (a full stop more), "I moved it" and "Leave it out" still hold, and nothing is
    downloaded again or asked again."""
    from hylly import curation

    listed, manifest, folder, records, cid = moved_by_hand(tmp_path, "drivethrurpg")
    (tmp_path / records[0]["path"]).unlink()
    answers = {cid: {f"moved:{records[0]['file']}": moved_answer("moved")}}
    reworded = tuple({**o, "word": o["word"] + "."} for o in curation.MOVED)
    monkeypatch.setattr(curation, "MOVED", reworded)
    sel = shopping.select("drivethrurpg", listed(), manifest, tmp_path, answers=answers)
    assert (sel.pending, sel.forgotten, sel.questions) == ([], 1, [])
    files = [curation.file_option("prints.pdf", "pdf", 500)]
    kept = curation.fingerprint([*files, curation.LEAVE_OUT])
    monkeypatch.setattr(curation, "LEAVE_OUT", {"word": "Leave it out, as it isn't a title.", "answer": "keep"})
    assert curation.settled({"title:": {"a": "keep", "o": kept}}, "title", "", [*files, curation.LEAVE_OUT]) == "keep"
    assert curation.fingerprint([*files, curation.LEAVE_OUT]) == kept
    assert curation.fingerprint([curation.file_option("prints.pdf", "pdf", 501), curation.LEAVE_OUT]) != kept  # another size


def test_a_run_that_read_the_whole_listing_lets_the_questions_of_titles_no_longer_listed_go(lantern, monkeypatch, capsys):
    """A question of a title the shop no longer lists goes with a run that read its whole
    listing, whatever that run's options; a run that couldn't read all of it keeps it."""
    import shutil

    from hylly import curation

    lantern.run()
    shutil.rmtree(lantern.root / LEDGER)
    lantern.run(title="nothing of the kind")
    assert curation.questions(lantern.root) == []  # a run that chose none of the titles looked at none
    lantern.run()
    (q,) = curation.questions(lantern.root)
    assert q["catalog_id"] == "l:103"
    monkeypatch.setattr(Lantern, "listed", [t for t in Lantern.listed if t.key != "103"])  # the shop lists it no more

    class Partial(Lantern):
        def titles(self, progress=None) -> list:
            self.unreadable = [("104", "Lantern Almanac")]  # a title it couldn't read: not the whole listing
            return Lantern.listed

    run = SimpleNamespace(library=str(lantern.root), dry_run=False, title=None, by=None, kind=None, limit=None,
                          update=False, jobs=2)
    cli._shop_download(run, LANTERN.id, Partial)
    assert [q["catalog_id"] for q in curation.questions(lantern.root)] == ["l:103"]  # kept
    lantern.run(title="guide")  # the whole listing read, with options: let go
    capsys.readouterr()
    assert curation.questions(lantern.root) == []


def test_a_storybundle_bundle_page_with_no_books_is_no_whole_reading_and_its_books_questions_stay(lantern, monkeypatch,
                                                                                                capsys):
    """A StoryBundle bundle page that comes back with no books (changed, or shown amiss)
    makes the reading not whole (Library.incomplete), so a run lets no question of a title it no longer lists go; one
    with every bundle's books is whole. The core keeps the questions of any shop's run so (a made-up shop here)."""
    import shutil

    from hylly import curation, storybundle

    class Pages:
        def __init__(self, bundles):
            self.given = bundles

        def bundles(self, progress=None):
            return self.given

    lib = storybundle.Library("s" * 40)
    full = storybundle.Bundle("The Moonwake Bundle", "2024-05-01", "", [storybundle.Book("78", "Second Tide", 2048, "")])
    lib.sb = Pages([full, storybundle.Bundle("The Lantern Bundle", "2025-01-01", "", [])])
    assert [t.key for t in lib.titles()] == ["78"] and lib.incomplete
    lib.sb = Pages([full])
    assert lib.titles() and not lib.incomplete

    lantern.run()
    shutil.rmtree(lantern.root / LEDGER)
    lantern.run()
    (q,) = curation.questions(lantern.root)
    monkeypatch.setattr(Lantern, "listed", [t for t in Lantern.listed if t.key != "103"])  # listed no more, or not read

    class Unwhole(Lantern):
        incomplete = True  # as StoryBundle's when a bundle's page listed no book

    run = SimpleNamespace(library=str(lantern.root), dry_run=False, title=None, by=None, kind=None, limit=None,
                          update=False, jobs=2)
    cli._shop_download(run, LANTERN.id, Unwhole)
    assert [x["id"] for x in curation.questions(lantern.root)] == [q["id"]]  # kept, with its number
    lantern.run()
    capsys.readouterr()
    assert curation.questions(lantern.root) == []  # read whole: gone


# ---- the shop list (shoplist.py, docs/downloads.md) ----------------------------------------------------------------

def read_back(shop_id: str, sel) -> dict:
    """The marks of the list a run would write of sel, written and read back unchanged."""
    from hylly import shoplist

    return shoplist.read(shoplist.data(shoplist.rows(shop_id, sel, "2026-11-16")), "list.csv").of(shop_id)


def chose(sel) -> tuple:
    """What a selection chooses and counts, for comparing two."""
    return ([(str(t.key), str(f.key), path, newer) for t, f, path, newer in sel.pending], sel.done, sel.changed, sel.held,
            sel.forgotten, sel.yours, sorted((q["kind"], q["item"], q["file"], q["held"]) for q in sel.questions),
            [(str(t.key), str(f.key), path, state) for t, f, path, state in sel.every])


@pytest.mark.parametrize("ask", [False, True])
def test_a_list_written_and_read_back_unchanged_chooses_what_the_plain_run_chooses(lantern, ask, capsys):
    """One code path: a plain run is a download of the list the rules make, and that list written out and read back
    unchanged chooses exactly the same files, done, pending, changed and held alike, with --limit and --update, a file
    gone from its place, one you said you moved and one you left out; and asks the same questions."""
    from hylly import curation
    from hylly import download as dl

    root = lantern.root

    def both(**given) -> None:
        plain = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(root, LANTERN.id), root, ask=ask, **given)
        marks = read_back(LANTERN.id, plain)
        listed = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(root, LANTERN.id), root, ask=ask,
                                 marks=marks, **given)
        assert chose(listed) == chose(plain), given
        assert (listed.unticked, listed.ticked_back) == ([], []) and len(listed.matched) == len(plain.rows)

    both()
    both(limit=1)
    lantern.run()
    capsys.readouterr()
    both()
    lantern.pages[("101", 0)] = b"%PDF-1.4 Lantern Wolf Field Guide, the second printing." * 20  # changed at the shop
    both()
    both(update=True)
    (root / FIELD_GUIDE / "maps.zip").unlink()  # gone from its place: a question, held back with ask
    both()
    moved = {"l:101": {"moved:2": {"a": "moved", "o": curation.fingerprint(list(curation.MOVED))}}}
    both(answers=moved)
    both(yours={"l:103": {"0"}, "l:101": {"1"}})  # left out by you: done, and not


def test_only_your_changes_count_and_what_you_change_lasts_where_it_should(lantern, capsys):
    """Only your changes count: a row whose mark is as written goes by the rules; a mark you changed is yours. A row the
    rules had ticked that you untick is left out from now on (and said, to be kept), one you left out ticked again is
    taken back, a row deleted is not taken and nothing is kept of it, a file done is done whatever its mark (and,
    unticked, it is left out should it go, though no file of the library is said left out), and the titles the list has
    no row of are said."""
    from hylly import download as dl

    root = lantern.root
    manifest = dl.Manifest(root, LANTERN.id)
    left = {"l:101": {"1"}}  # the print copy, left out before the list was written
    plain = shopping.select(LANTERN.id, lantern_titles(lantern.pages), manifest, root, yours=left)
    marks = read_back(LANTERN.id, plain)
    assert (marks["lanternshop:101:2"], marks["lanternshop:101:1"]) == ((True, True), (False, False))  # as written
    marks["lanternshop:101:2"] = (False, True)  # the maps unticked: the rules had ticked them
    marks["lanternshop:101:1"] = (True, False)  # the print copy ticked again
    del marks["lanternshop:102:0"]  # a row deleted: that title has no row now
    sel = shopping.select(LANTERN.id, lantern_titles(lantern.pages), manifest, root, marks=marks, yours=left)
    assert sorted((t.key, f.key) for t, f, _, _ in sel.pending) == [("101", 0), ("101", 1), ("103", 0)]
    assert [(t.key, f.key, there) for t, f, there in sel.unticked] == [("101", 2, False)]
    assert [(t.key, f.key) for t, f, _ in sel.ticked_back] == [("101", 1)]
    assert [t.key for t in sel.unlisted] == ["102"] and sel.yours == 1  # the maps, left out in this run already
    lantern.run()
    capsys.readouterr()
    marks = {rid: (False, True) for rid in read_back(LANTERN.id, plain)}  # every row unticked: what's here stays
    sel = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(root, LANTERN.id), root, marks=marks)
    assert (sel.pending, sel.done, sel.yours) == ([], 5, 0) and {there for _, _, there in sel.unticked} == {True}


HUMBLE_FOLDER = "humble/Moonwake Press/Lantern Wolf"
PDFS = [{"file": "Lantern Wolf (HQ).pdf", "format": "pdf", "size": 9000}, {"file": "Lantern Wolf.pdf", "format": "pdf", "size": 8800},
        {"file": "Lantern Wolf (print).pdf", "format": "pdf", "size": 8600}]


def unsure_lantern(key: str = "lantern", also: tuple = (), chosen: int | None = 0) -> list:
    """A Humble title whose rules were unsure between three PDFs (a which-file question, the HQ the guess), with an EPUB
    and the other PDFs behind the chosen one, and its maps; chosen None: as once answered with the plain PDF, which is
    then the chosen file, sure, and the HQ behind it."""
    from hylly import humble

    def pdf(n: int) -> humble.File:
        o = PDFS[n]
        return humble.File(o["file"], o["file"], o["size"], "abc"[n] * 32, label="PDF (HQ)" if n == 0 else "PDF", ext="pdf")

    first = pdf(0 if chosen is not None else 1)
    if chosen is not None:
        first.unsure = (PDFS, chosen, "Humble offers 3 PDF files, and the largest was taken")
    first.fallbacks = [*(pdf(n) for n in ((1, 2) if chosen is not None else (0, 2))),
                       humble.File("Lantern Wolf.epub", "Lantern Wolf.epub", 500, "e" * 32, label="EPUB", ext="epub")]
    maps = humble.File("maps.zip", "maps.zip", 300, "d" * 32, label="Maps", role="extra", ext="zip")
    return [humble.Title(key, "Lantern Wolf", "Moonwake Press", [first, maps], folder=HUMBLE_FOLDER, purchased="2026-01-02",
                         also=also)]


def test_a_humble_title_s_formats_behind_are_rows_the_rules_never_tick_and_a_tick_fetches_one(tmp_path):
    """The formats behind a chosen file (File.fallbacks: Humble's) are rows of their own, never ticked by the rules; a
    tick fetches one to its place in the title's folder, as its own file. The list's why says a which-file question's
    guess and why, so the mark can be moved."""
    from hylly import download as dl
    from hylly import shoplist

    manifest = dl.Manifest(tmp_path, "humble")
    plain = shopping.select("humble", unsure_lantern(), manifest, tmp_path)
    rows = shoplist.rows("humble", plain, "2026-11-16")
    behind = "another of its files, behind the one chosen: tick it, and untick the chosen one, to keep this one instead"
    assert [(r[4], r[5], r[8], r[10], r[11], r[12]) for r in rows] == [
        ("PDF (HQ)", "book", "the rules' guess: Humble offers 3 PDF files, and the largest was taken: move the mark to "
         "keep another", "humble:lantern:Lantern Wolf (HQ).pdf", "yes", "(X)"),
        ("PDF", "book", behind, "humble:lantern:Lantern Wolf.pdf", "", "( )"),
        ("PDF", "book", behind, "humble:lantern:Lantern Wolf (print).pdf", "", "( )"),
        ("EPUB", "book", behind, "humble:lantern:Lantern Wolf.epub", "", "( )"),
        ("Maps", "extra", "extras are kept", "humble:lantern:maps.zip", "yes", "(X)")]
    assert [(q["kind"], q["held"]) for q in plain.questions] == [("which-file", False)]
    held = shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=True)
    first = shoplist.rows("humble", held, "2026-11-16")[0]
    assert (first[11], first[12]) == ("", "( )") and first[8].startswith("held back for your answer (hylly decide); the rules")
    marks = read_back("humble", plain)
    marks["humble:lantern:Lantern Wolf.epub"] = (True, False)  # the EPUB too
    sel = shopping.select("humble", unsure_lantern(), manifest, tmp_path, marks=marks)
    assert [(f.name, path) for _, f, path, _ in sel.pending] == [("Lantern Wolf (HQ).pdf", f"{HUMBLE_FOLDER}/Lantern Wolf (HQ).pdf"),
                                                                   ("Lantern Wolf.epub", f"{HUMBLE_FOLDER}/Lantern Wolf.epub"),
                                                                   ("maps.zip", f"{HUMBLE_FOLDER}/maps.zip")]
    job = shopping.make_job("humble", *sel.pending[1][:3])
    assert (job.extra, job.fallbacks) == ({"item": "lantern", "file": "Lantern Wolf.epub"}, [])  # its own file, no instead_of
    assert (sel.unticked, sel.answers) == ([], [])


@pytest.mark.parametrize("ask", [False, True])
def test_moving_the_mark_of_a_which_file_question_answers_it_with_that_file(tmp_path, ask):
    """A list (written with unsure = "guess", the guess ticked) that ticks exactly one other of a which-file question's
    files and unticks the guess, both marks changed from their picked, answers the question with that file by its
    option's number (Selection.answers, which the run makes the answer of): that file is fetched, the guess neither
    fetched nor left out, nor held back with ask; a file ticked besides that isn't one of the question's (the EPUB) is
    fetched too. Two others ticked answer nothing: each is fetched, the guess left out, the question as it was."""
    from hylly import download as dl

    manifest = dl.Manifest(tmp_path, "humble")
    marks = read_back("humble", shopping.select("humble", unsure_lantern(), manifest, tmp_path))
    on = (True, False)
    moved = {**marks, "humble:lantern:Lantern Wolf (HQ).pdf": (False, True), "humble:lantern:Lantern Wolf.pdf": on,
             "humble:lantern:Lantern Wolf.epub": on}
    sel = shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=ask, marks=moved)
    assert [(t.key, n, options[n]["file"]) for t, n, options in sel.answers] == [("lantern", 1, "Lantern Wolf.pdf")]
    assert sorted(f.name for _, f, _, _ in sel.pending) == ["Lantern Wolf.epub", "Lantern Wolf.pdf", "maps.zip"]
    assert (sel.unticked, sel.held, sel.yours) == ([], 0, 0)
    assert [(q["kind"], q["held"]) for q in sel.questions] == [("which-file", False)]  # found, to be answered
    two = {**moved, "humble:lantern:Lantern Wolf (print).pdf": on}
    sel = shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=ask, marks=two)
    assert sel.answers == [] and sorted(f.name for _, f, _, _ in sel.pending) == [
        "Lantern Wolf (print).pdf", "Lantern Wolf.epub", "Lantern Wolf.pdf", "maps.zip"]
    assert [f.name for _, f, _ in sel.unticked] == ["Lantern Wolf (HQ).pdf"]


def test_only_a_mark_you_moved_answers_the_question(tmp_path):
    """B goes by your changes alone: no answer from a list without picked (its marks are that run's alone), nor from an
    unchanged list whose guess moved to the other file since it was written (the rules' marks, not yours); and a guess
    held back for your answer as the list was written (unsure = "ask", unticked as written) answers nothing when
    another file is ticked: that one comes, and the guess stays held back while the question is open, though a book of
    the title is here now."""
    from hylly import download as dl

    manifest = dl.Manifest(tmp_path, "humble")
    marks = read_back("humble", shopping.select("humble", unsure_lantern(), manifest, tmp_path))
    moved = {**marks, "humble:lantern:Lantern Wolf (HQ).pdf": (False, True), "humble:lantern:Lantern Wolf.pdf": (True, False)}
    gone = {rid: (on, None) for rid, (on, _) in moved.items()}
    sel = shopping.select("humble", unsure_lantern(), manifest, tmp_path, marks=gone)
    assert sel.answers == [] and "Lantern Wolf.pdf" in [f.name for _, f, _, _ in sel.pending]  # this run's alone
    assert (sel.unticked, sel.only_now) == ([], 1)
    stale = {**marks, "humble:lantern:Lantern Wolf (HQ).pdf": (True, True), "humble:lantern:Lantern Wolf.pdf": (False, False)}
    since = unsure_lantern()
    since[0].files[0].unsure = (PDFS, 1, "the rules' guess is the plain PDF now")  # the guess moved since the list
    sel = shopping.select("humble", since, manifest, tmp_path, marks=stale)
    assert sel.answers == []
    held = read_back("humble", shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=True))
    assert held["humble:lantern:Lantern Wolf (HQ).pdf"] == (False, False)  # held back as written
    ticked = {**held, "humble:lantern:Lantern Wolf.pdf": (True, False)}
    sel = shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=True, marks=ticked)
    assert sel.answers == [] and sel.held == 1 and "Lantern Wolf.pdf" in [f.name for _, f, _, _ in sel.pending]
    body = b"%PDF-1.4 made up" * 550
    (tmp_path / HUMBLE_FOLDER).mkdir(parents=True)
    (tmp_path / HUMBLE_FOLDER / "Lantern Wolf.pdf").write_bytes(body)
    manifest.add({"path": f"{HUMBLE_FOLDER}/Lantern Wolf.pdf", "shop_id": "lantern", "item": "lantern", "file": "Lantern Wolf.pdf",
                  "role": "book", "ext": "pdf", "size": len(body), "md5": "b" * 32})  # it came
    after = shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=True,
                            asked={("which-file", "lantern", "")})  # the question open
    assert "Lantern Wolf (HQ).pdf" not in [f.name for _, f, _, _ in after.pending] and after.held == 1


def test_moving_the_mark_answers_the_question_of_a_guess_downloaded_already_and_leaves_it_as_it_is(tmp_path):
    """The usual case, unsure = "guess": the guess came, its question open. Moving the mark answers it with the file
    ticked, which is fetched; the guess stays done, in the library as it is, and nothing is left out for it."""
    from hylly import download as dl

    manifest = dl.Manifest(tmp_path, "humble")
    body = b"%PDF-1.4 the HQ, made up" * 375
    (tmp_path / HUMBLE_FOLDER).mkdir(parents=True)
    (tmp_path / HUMBLE_FOLDER / "Lantern Wolf (HQ).pdf").write_bytes(body)
    manifest.add({"path": f"{HUMBLE_FOLDER}/Lantern Wolf (HQ).pdf", "shop_id": "lantern", "item": "lantern",
                  "file": "Lantern Wolf (HQ).pdf", "role": "book", "ext": "pdf", "size": len(body), "md5": "a" * 32})
    asked = {("which-file", "lantern", "")}
    plain = shopping.select("humble", unsure_lantern(), manifest, tmp_path, asked=asked)
    marks = read_back("humble", plain)
    assert marks["humble:lantern:Lantern Wolf (HQ).pdf"] == (True, True) and plain.done == 1
    moved = {**marks, "humble:lantern:Lantern Wolf (HQ).pdf": (False, True), "humble:lantern:Lantern Wolf.pdf": (True, False)}
    sel = shopping.select("humble", unsure_lantern(), manifest, tmp_path, asked=asked, marks=moved)
    assert [(n, options[n]["file"]) for _, n, options in sel.answers] == [(1, "Lantern Wolf.pdf")]
    assert sorted(f.name for _, f, _, _ in sel.pending) == ["Lantern Wolf.pdf", "maps.zip"] and sel.done == 1
    assert (sel.unticked, sel.yours) == ([], 0) and [q["kind"] for q in sel.questions] == ["which-file"]


def test_moving_the_mark_tells_two_files_of_one_name_apart_by_the_option_s_number(tmp_path):
    """Two of a question's files may share a name (a PDF and its HQ under one): the answer is the option's number, the
    file ticked found by its name and its size."""
    from hylly import download as dl
    from hylly import humble

    twins = [{"file": "Lantern Wolf.pdf", "format": "pdf", "size": 9000}, {"file": "Lantern Wolf.pdf", "format": "pdf", "size": 8800}]
    big = humble.File("Lantern Wolf.pdf", "Lantern Wolf.pdf", 9000, "a" * 32, label="PDF (HQ)", ext="pdf",
                      unsure=(twins, 0, "Humble offers 2 PDF files, and the largest was taken"))
    big.fallbacks = [humble.File("Lantern Wolf (2).pdf", "Lantern Wolf.pdf", 8800, "b" * 32, label="PDF", ext="pdf")]
    titles = lambda: [humble.Title("lantern", "Lantern Wolf", "Moonwake Press", [big], folder=HUMBLE_FOLDER)]  # noqa: E731
    manifest = dl.Manifest(tmp_path, "humble")
    marks = read_back("humble", shopping.select("humble", titles(), manifest, tmp_path))
    moved = {"humble:lantern:Lantern Wolf.pdf": (False, True), "humble:lantern:Lantern Wolf (2).pdf": (True, False)}
    assert set(marks) == set(moved)
    sel = shopping.select("humble", titles(), manifest, tmp_path, marks=moved)
    assert [n for _, n, _ in sel.answers] == [1] and [p for _, _, p, _ in sel.pending] == [f"{HUMBLE_FOLDER}/Lantern Wolf (2).pdf"]


def test_a_row_as_written_goes_by_the_rules_at_the_run_so_a_file_answered_since_is_fetched(tmp_path):
    """Only your changes count: a list written while a file was held back for your answer (unsure = "ask"), its row
    unticked as written, fetches it once it has been answered since (here with the plain PDF, now the chosen file, its
    row unticked as written as it was behind then), and keeps nothing of the rows it didn't change; the HQ, behind now,
    isn't fetched, nor left out."""
    from hylly import download as dl

    manifest = dl.Manifest(tmp_path, "humble")
    then = shopping.select("humble", unsure_lantern(), manifest, tmp_path, ask=True)
    marks = read_back("humble", then)
    assert marks["humble:lantern:Lantern Wolf (HQ).pdf"] == (False, False) and then.held == 1
    now = shopping.select("humble", unsure_lantern(chosen=None), manifest, tmp_path, ask=True, marks=marks)
    assert sorted(f.name for _, f, _, _ in now.pending) == ["Lantern Wolf.pdf", "maps.zip"]
    assert (now.unticked, now.ticked_back, now.held, now.yours) == ([], [], 0, 0)


def test_a_list_without_picked_counts_for_that_run_alone_and_keeps_nothing(lantern):
    """A row whose picked is gone (a list from elsewhere, the column dropped) can't tell your change from the rules': its
    mark counts for that run, ticked fetched and unticked not, and nothing lasting is made of it."""
    from hylly import download as dl

    plain = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(lantern.root, LANTERN.id), lantern.root,
                            yours={"l:101": {"1"}})
    marks = {rid: (on, None) for rid, (on, _) in read_back(LANTERN.id, plain).items()}
    marks["lanternshop:101:2"], marks["lanternshop:101:1"] = (False, None), (True, None)
    sel = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(lantern.root, LANTERN.id), lantern.root,
                          marks=marks, yours={"l:101": {"1"}})
    assert sorted((t.key, f.key) for t, f, _, _ in sel.pending) == [("101", 0), ("101", 1), ("102", 0), ("103", 0)]
    assert (sel.unticked, sel.ticked_back, sel.yours, sel.only_now) == ([], [], 0, 1)  # the maps: this run's alone


def test_a_file_ticked_again_is_taken_back_under_every_purchase_it_was_left_out_under(tmp_path):
    """A Humble title bought again may lead under its other purchase's id since: a file left out under the first, ticked
    again in a list, is taken back under that id too (Title.also), so the next plain run fetches it."""
    from hylly import curation
    from hylly import download as dl

    (tmp_path / "state").mkdir()
    curation.leave_out(tmp_path, {"h:lanternA": ("Lantern Wolf", {"maps.zip": "maps.zip"})}, by=curation.COMMAND_LINE)
    titles = lambda: unsure_lantern("lanternB", ("lanternA",))  # noqa: E731
    manifest = dl.Manifest(tmp_path, "humble")
    plain = shopping.select("humble", titles(), manifest, tmp_path, yours=curation.left_out(tmp_path))
    assert plain.yours == 1 and "maps.zip" not in [f.name for _, f, _, _ in plain.pending]
    marks = {**read_back("humble", plain), "humble:lanternB:maps.zip": (True, False)}
    sel = shopping.select("humble", titles(), manifest, tmp_path, marks=marks, yours=curation.left_out(tmp_path))
    assert cli._keep_choices(tmp_path, shops.SHOPS["humble"], sel, curation.left_out(tmp_path)) == 0
    assert curation.left_out(tmp_path) == {}
    again = shopping.select("humble", titles(), manifest, tmp_path, yours=curation.left_out(tmp_path))
    assert again.yours == 0 and "maps.zip" in [f.name for _, f, _, _ in again.pending]


def test_a_file_you_moved_by_hand_shows_so_and_a_tick_fetches_it_again_while_your_answer_stays(lantern, capsys):
    """A file answered "I moved it by hand" is a row unticked with "moved by you" under "in library"; a tick fetches it
    again to its place, and the answer stays (a tick answers no question by itself): once here it is done, and should it
    go again, it isn't fetched behind your back."""
    from hylly import curation, shoplist
    from hylly import download as dl

    lantern.run()
    (lantern.root / FIELD_GUIDE / "maps.zip").unlink()
    moved = {"l:101": {"moved:2": {"a": "moved", "o": curation.fingerprint(list(curation.MOVED))}}}
    plain = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(lantern.root, LANTERN.id), lantern.root,
                            answers=moved)
    (row,) = [r for r in shoplist.rows(LANTERN.id, plain, "2026-11-16") if r[10] == "lanternshop:101:2"]
    assert (row[7], row[11], row[12]) == ("moved by you", "", "( )") and plain.forgotten == 1
    marks = {**read_back(LANTERN.id, plain), "lanternshop:101:2": (True, False)}
    sel = shopping.select(LANTERN.id, lantern_titles(lantern.pages), dl.Manifest(lantern.root, LANTERN.id), lantern.root,
                          answers=moved, marks=marks)
    assert [p for _, _, p, _ in sel.pending] == [f"{FIELD_GUIDE}/maps.zip"] and sel.forgotten == 0 and sel.unticked == []


def write_list(path: Path, rows: list[list[str]]) -> None:
    from hylly import shoplist

    path.write_bytes(shoplist.data(rows))


def listed_rows(path: Path) -> list[list[str]]:
    import csv
    import io

    return list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig")), delimiter=";"))[1:]


def test_a_row_unticked_is_left_out_by_you_until_ticked_again_or_taken_back(lantern, capsys, monkeypatch):
    """Choices that last: a row the rules tick that a list unticks is kept as your edit (left-out, a field of its own):
    a plain run never fetches it and counts it apart, a new list shows it unticked "left out", and it is taken back by
    ticking the row again, by hylly set left-out auto --id ID, or by undoing the edit, exactly."""
    from hylly import curation

    out = lantern.root / "lantern-list.csv"
    lantern.run(action="list", file=str(out), force=False)
    assert "3 titles at Lantern Shop: 5 rows, 5 ticked" in capsys.readouterr().err
    rows = listed_rows(out)
    assert [r[10] for r in rows] == ["lanternshop:103:0", "lanternshop:101:0", "lanternshop:101:1", "lanternshop:101:2",
                                     "lanternshop:102:0"] and {(r[11], r[12]) for r in rows} == {("yes", "(X)")}
    rows[3][12] = "( )"  # the maps
    write_list(out, rows)
    lantern.run(list=str(out))
    said = capsys.readouterr().out
    assert "To download: 3 titles, 4 files" in said and "Left out by you: 1 file, not fetched" in said
    assert "Your list left out 1 file that a download would take" in said and "1 left out by you." in said
    assert not (lantern.root / FIELD_GUIDE / "maps.zip").exists() and curation.left_out(lantern.root) == {"l:101": {"2"}}
    edit = curation.load_lines(lantern.root)[-1]
    assert (edit["field"], edit["op"], edit["value"], edit["by"]) == ("left-out", "update", {"out:2": {"a": "out", "f": "maps.zip"}},
                                                                     curation.COMMAND_LINE)
    lantern.run()  # a plain run, as the jobs' timer runs it: not fetched, counted apart, no failure
    said = capsys.readouterr().out
    assert "To download: 0 titles, 0 files" in said and "Left out by you: 1 file, not fetched" in said
    assert not (lantern.root / FIELD_GUIDE / "maps.zip").exists()
    lantern.run(action="list", file=str(out), force=True)
    capsys.readouterr()
    maps = next(r for r in listed_rows(out) if r[10] == "lanternshop:101:2")
    assert (maps[7], maps[11], maps[12], maps[8]) == ("left out", "", "( )", "left out by you: tick it to take it again")
    rows = listed_rows(out)
    for r in rows:
        r[12] = "(X)"  # ticked again
    write_list(out, rows)
    lantern.run(list=str(out))
    assert "Your list took back 1 file you had left out" in capsys.readouterr().out
    assert (lantern.root / FIELD_GUIDE / "maps.zip").is_file() and curation.left_out(lantern.root) == {}

    (lantern.root / FIELD_GUIDE / "maps.zip").unlink()
    rows[3][11], rows[3][12] = "yes", "( )"  # as a list written before it was left out
    write_list(out, rows)
    lantern.run(list=str(out))
    assert curation.left_out(lantern.root) == {"l:101": {"2"}}
    cli.main(["set", "left-out", "auto", "--id", "lanternshop:101:0", "--yes", "--library", str(lantern.root)])
    assert "Nothing to do: none of the files chosen is left out" in capsys.readouterr().out  # another row's id
    cli.main(["set", "left-out", "auto", "--id", "lanternshop:101:2", "--yes", "--library", str(lantern.root)])  # the row's
    assert "Took back the files left out with a shop list for 1 title" in capsys.readouterr().out
    assert curation.left_out(lantern.root) == {}
    lantern.run(list=str(out))
    cli.main(["set", "left-out", "auto", "--id", "l:101", "--dry-run", "--library", str(lantern.root)])  # the title's id
    said = capsys.readouterr().out
    assert "1 file left out with a shop list, of 1 title: l:101 (2)" in said
    assert "It would take it back, so that a download fetches it again. Nothing changed." in said
    assert curation.left_out(lantern.root) == {"l:101": {"2"}}
    cli.main(["set", "left-out", "auto", "--id", "l:101", "--yes", "--library", str(lantern.root)])
    assert curation.left_out(lantern.root) == {}
    lantern.run(list=str(out))  # left out again; then the edit undone, exactly, as the library page's Undo does it
    capsys.readouterr()
    (latest,) = [e for e in curation.history(lantern.root, 1)]
    assert latest["said"] == "left 1 file out with a shop list" and latest["field"] == "left-out"
    for entries, ids in curation.undo_update(list(latest["tag"]), latest["was"]):
        curation.change_answers(lantern.root, entries, [{"id": i, "t": "", "f": [[""]]} for i in ids], "owner",
                                field="left-out", undo=True)
    assert curation.left_out(lantern.root) == {}
    lantern.run()
    assert (lantern.root / FIELD_GUIDE / "maps.zip").is_file()


def test_a_list_run_says_its_rows_it_can_t_take_files_changed_since_and_titles_bought_since(lantern, capsys):
    """Old or broken lists: an id the shop no longer has, a row of another shop, one that names nothing and one with a
    mark that can't be read are each said with why (the rest goes on, partly done, exit code 1); a file the shop
    changed since (another size than the list says) comes as it is now, under the same id, which is said; and a title
    bought since the list was written, with no row in it, is noted (a shop that gives no purchase dates has no note)."""
    out = lantern.root / "old-list.csv"
    lantern.run(action="list", file=str(out), force=False)
    capsys.readouterr()
    rows = [r for r in listed_rows(out) if r[10] != "lanternshop:102:0"]  # the second guide, bought 2026-02-03
    for r in rows:
        r[9] = "2026-01-01"  # written before it
    rows[1][6] = "5,0"  # the guide's size then
    rows[2][6] = rows[0][6] = "0"  # 0,0 as a spreadsheet saves it: the same size, no change
    rows += [["lanternshop", "", "Velvet Jinx Core Rules", "", "", "", "", "", "", "", "lanternshop:999:0", "", "(X)"],
             ["drivethrurpg", "", "Moonwake Atlas", "", "", "", "", "", "", "", "drivethrurpg:123456:1", "", "(X)"],
             ["", "", "A note of mine", "", "", "", "", "", "", "", "my own words", "", "(X)"],
             ["lanternshop", "", "Clan Ledger", "", "", "", "", "", "", "", "lanternshop:103:7", "", "maybe"]]
    write_list(out, rows)
    with pytest.raises(SystemExit) as ended:
        lantern.run(list=str(out))
    said = capsys.readouterr().out
    assert ended.value.code == 1 and "3 titles, 6 files" in said and "To download: 2 titles, 4 files" in said
    assert ("Rows of your list not taken (4), the rest going on:\n"
            "  Clan Ledger (lanternshop:103:7): a mark Hylly can't read: 'maybe' (use (X) or ( ))\n"
            "  Velvet Jinx Core Rules (lanternshop:999:0): not at the shop any more\n"
            "  Moonwake Atlas (drivethrurpg:123456:1): a row of DriveThruRPG's, not Lantern Shop's: hylly download takes "
            "every shop's\n"
            "  A note of mine (my own words): matching nothing") in said
    assert ("Changed at Lantern Shop since your list (1): the file it lists now comes, under the same id:\n"
            "  Lantern Wolf Field Guide (lanternshop:101:0): another size than the list says") in said
    assert "This list was written" in said and "1 item bought since isn't in it: run hylly lanternshop list again" in said
    assert "Not taken from the list: Velvet Jinx Core Rules (lanternshop:999:0): not at the shop any more" in (
        lantern.root / "state" / "download-log.txt").read_text(encoding="utf-8")
    assert not (lantern.root / "lanternshop" / "Moonwake Press" / "Lantern Wolf Field Guide (2)").exists()  # its row deleted


def test_a_dry_run_with_a_list_says_its_notes_after_the_files_and_writes_nothing_not_even_the_log(lantern, capsys):
    """The files a dry run lists come first, the list's notes after them, so no file reads as one left out or changed;
    and a dry run writes nothing, the download log included."""
    out = lantern.root / "old-list.csv"
    lantern.run(action="list", file=str(out), force=False)
    rows = listed_rows(out)
    rows[3][12] = "( )"  # the maps unticked
    rows.append(["lanternshop", "", "Velvet Jinx Core Rules", "", "", "", "", "", "", "", "lanternshop:999:0", "", "(X)"])
    write_list(out, rows)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        lantern.run(list=str(out), dry_run=True)
    said = capsys.readouterr().out
    paths_at = said.index(f"  {FIELD_GUIDE}/guide.pdf")
    assert paths_at < said.index("Rows of your list not taken") < said.index("Your list would leave out 1 file")
    assert "a dry run keeps nothing" in said and not (lantern.root / "state" / "download-log.txt").exists()


def test_a_list_goes_to_standard_output_or_a_file_never_over_one_there_and_a_dry_run_writes_none(lantern, capsysbinary,
                                                                                               monkeypatch):
    """hylly SHOP list FILE: - writes to standard output (its words go to standard error), a file there already is
    written over only with --force, and a dry run writes nothing, saying where it would."""
    import io

    from hylly import shoplist

    lantern.run(action="list", file="-", force=False)
    said = capsysbinary.readouterr()
    assert said.out.startswith(b"\xef\xbb\xbf" + ";".join(shoplist.HEADER).encode()) and b"Wrote" not in said.err
    out = lantern.root / "mine.csv"
    out.write_text("my own edits", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        lantern.run(action="list", file=str(out), force=False)
    assert refused.value.code == 2 and "mine.csv is there already, and may be a list you have edited" in str(refused.value)
    assert out.read_text(encoding="utf-8") == "my own edits"
    lantern.run(action="list", file=str(out), force=False, dry_run=True)
    said = capsysbinary.readouterr().err
    assert b"It would write 5 rows, 5 ticked to " in said and b"(there already: only with --force)." in said
    lantern.run(action="list", file=str(out), force=True, dry_run=True)
    assert b"(there already: it would write over it, as --force says)." in capsysbinary.readouterr().err
    assert out.read_text(encoding="utf-8") == "my own edits"
    lantern.run(action="list", file=str(out), force=True)
    assert listed_rows(out)[0][10] == "lanternshop:103:0"
    monkeypatch.chdir(lantern.root)
    lantern.run(action="list", file=None, force=False)  # as it comes: <shop>-list.csv, here
    assert (lantern.root / "lanternshop-list.csv").is_file()
    rows = listed_rows(out)
    rows[0][12] = "( )"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(shoplist.data(rows))))
    lantern.run(list="-", dry_run=True)  # the list from standard input
    said = capsysbinary.readouterr().out.decode()
    assert "From your list: 5 rows, 4 ticked: 4 to download (1.5 KB), 0 in the library already." in said


def test_a_dry_run_says_what_the_list_would_bring_with_or_without_one(lantern, capsys):
    lantern.run(dry_run=True)
    assert "From the list the rules make: 5 rows, 5 ticked: 5 to download (1.7 KB), 0 in the library already." in (
        capsys.readouterr().out)
    lantern.run()
    capsys.readouterr()
    lantern.run(dry_run=True)
    assert "From the list the rules make: 5 rows, 5 ticked: 0 to download (0 bytes), 5 in the library already." in (
        capsys.readouterr().out)


def test_the_options_that_choose_titles_are_refused_beside_a_list_and_a_list_takes_them(lantern, capsys, tmp_path):
    """A list chooses the titles itself: beside a LIST, --title, --kind and a shop's own (--bundle, --types, --no-bonus)
    are refused, before the shop is asked; --limit, --update and --dry-run still apply. hylly SHOP list takes them, and
    writes the rows of the titles they choose."""
    out = tmp_path / "list.csv"
    lantern.run(action="list", file=str(out), force=False, title="ledger")
    assert [r[10] for r in listed_rows(out)] == ["lanternshop:103:0"]
    with pytest.raises(SystemExit) as refused:
        lantern.run(list=str(out), title="ledger", kind=frozenset({"book"}), choosers=(("title", "--title"), ("kind", "--kind")))
    assert refused.value.code == 2 and str(refused.value).startswith("--title, --kind choose titles, and a list chooses them")
    capsys.readouterr()
    lantern.run(list=str(out), limit=0, dry_run=True)
    assert "To download: 0 titles, 0 files" in capsys.readouterr().out


def test_a_list_without_picked_says_its_unticked_rows_count_for_this_run_alone(lantern, capsys):
    """With no picked to tell your marks by, an unticked row is left out of that run alone: its totals say so, apart
    from the files left out by you, and send nobody to a new list for it."""
    out = lantern.root / "plain.csv"
    lantern.run(action="list", file=str(out), force=False)
    rows = [[r[10], r[12]] for r in listed_rows(out)]
    rows[3][1] = "( )"  # the maps
    out.write_bytes(("id;download\r\n" + "".join(f"{a};{b}\r\n" for a, b in rows)).encode("utf-8"))
    capsys.readouterr()
    lantern.run(list=str(out), dry_run=True)
    said = capsys.readouterr().out
    assert "Unticked in your list, for this run alone: 1 file." in said and "Left out by you" not in said
    assert "Your list has no picked column: its marks there count for this run alone" in said
