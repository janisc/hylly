"""What an attacker might try, and what stops it: paths that lead elsewhere,
scripts in books and files, names the server isn't called by, other sites' pages, proxies' headers, oversized requests and
archives, a login wherever a request comes from, ranges, feeds, logs and alarms, and who may do what."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import ntpath
import os
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import pytest

from hylly import access, archives, bell, catalog, opds, paths, reader, serve, settings
from hylly.library import MIME, Catalog

from helpers import HOME, OPF, build_library, make_epub, png, start


@pytest.fixture
def server(library, serving):
    srv, client = serving(library)  # an owner, who may see anything a request may
    return srv, client, library


# ---- paths -------------------------------------------------------------------

def test_a_path_that_leads_elsewhere_is_refused_before_windows_sees_it(server, monkeypatch):
    srv, client, root = server
    monkeypatch.setattr(paths, "WINDOWS", True)  # Windows' rules, whatever system this is: as they are on Windows itself
    seen = []
    real = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: seen.append(str(self)) or real(self, *a, **k))
    for path in ("/%5C%5Cattacker.example%5Cshare%5Cx.epub", "/foliate/%5C%5Cattacker.example%5Cs%5Cx.js",
                 "/C:/Windows/win.ini", "/x%3A%3A%24DATA", "/con.epub", "/catalog/../state/x", "/books./x",
                 "/library%00.epub", "/%2e%2e/x"):
        assert client.get(path).status_code == 404, path
    assert not [s for s in seen if "attacker" in s or s.startswith("\\\\")]  # never asked about another PC's share
    assert serve.library_path(root, "/catalog/index.html") == (root / "catalog" / "index.html").resolve()


WINDOWS_ONLY = {  # a name of each kind Windows takes for something else, where Linux and macOS hold it as it is
    "a colon": "ebooks/Moonwake: Second Tide.epub",
    "a backslash": "comics/Quill\\fern 001.cbz",
    "a name ending in a dot": "comics/S.H.E.L.F./Quillfern 002.cbz",
    "a name ending in a space": "ebooks/Ann Writer /The Harbor Light.epub",
    "a device's name": "ebooks/aux.epub",
    "a device's name as a folder": "comics/con/Quillfern 003.cbz",
    "a device's name before a dot": "ebooks/Nul.Moonwake Press/Tide.epub",
}


@pytest.mark.parametrize("windows", [True, False], ids=["on-windows", "on-linux-and-macos"])
def test_names_only_windows_takes_for_something_else_are_refused_on_windows_alone(tmp_path, monkeypatch, windows):
    """Each kind of name, by each system's rules, whatever system runs this: resolve gives a path back as it is, so the
    disk is never asked, and no name here reaches a real Windows. On Windows each is refused before anything is asked;
    on Linux and macOS each names its file. A record's path goes by the same rule for a colon and a backslash, which
    lead elsewhere on Windows, and keeps the names Windows 11 holds (paths.renamed_on_windows). '..', an empty part and
    what no name may hold are refused on every system."""
    monkeypatch.setattr(paths, "WINDOWS", windows)
    asked = []
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: asked.append(str(self)) or self)
    files = paths.LibraryFiles(tmp_path)  # a catalog build's look: the library resolved once, at its start
    for kind, rel in WINDOWS_ONLY.items():
        asked.clear()
        found = serve.library_path(tmp_path, "/" + rel)
        if windows:
            assert found is None and not asked, kind
        else:
            assert found == tmp_path.joinpath(*rel.split("/")) and asked, kind
        if windows and kind in ("a colon", "a backslash"):  # a record's path, by the same words as library_file
            assert files.file(rel) is None and not asked, kind
        assert serve.plain_words(rel) is not windows, kind
        assert paths.inside_library(rel) is not (windows and kind in ("a colon", "a backslash")), kind
    for rel in ("/../outside.epub", "/ebooks/../../x.epub", "/ebooks//x.epub", "/ebooks/./x.epub", "/ebooks/x\x00.epub",
                "/ebooks/\x1b[2Jx.epub", "/", ""):
        asked.clear()
        assert serve.library_path(tmp_path, rel) is None and not asked, rel


DEVICES = {  # a name, and whether Windows takes it for one of its devices (paths.windows_cant_hold)
    "CON": True, "con.epub": True, "Con. Artist": True, "NUL .epub": True, "nul.tar.gz": True, "COM0.cbz": True,
    "LPT0": True, "com9.pdf": True, "CONIN$": True, "conout$.txt": True, "AUX": True, "prn.": True, "COM\u00b9": True,
    "com\u00b2.cbz": True, "LPT\u00b3.pdf": True, "COM\u2074": False, "LPT\u2081": False,
    "console.txt": False, "COM10": False, "COM": False, "LPT.pdf": False, " CON": False, "Ｃｏｎ": False, "Prn Garden.pdf": False,
    "Auxiliary.pdf": False, "Moonwake.epub": False,
}


@pytest.mark.parametrize("name", DEVICES)
def test_windows_device_names_are_one_list_and_one_test_for_every_part_that_names_files(name, monkeypatch):
    """One list and one test (paths.windows_cant_hold): a shop's folder and file are named otherwise (paths.clean,
    paths.safe_filename), the server refuses such a name on Windows (serve.plain_words), and a copy to a disk with
    Windows' rules says it can't hold it (hyllycopy.name_problem); any other name is as it was."""
    from hylly import hyllycopy

    device = DEVICES[name]
    assert paths.windows_cant_hold(name) is device
    for named in (paths.clean(name), paths.safe_filename(name)):  # neither ever names a folder or a file so
        assert not paths.windows_cant_hold(named), named
    ends = name != name.rstrip(". ")  # which a copy says first
    assert hyllycopy.name_problem(f"ebooks/{name}", True) == (
        "its name ends in a dot or a space, which this disk can't hold" if ends
        else "its name is one that Windows keeps for itself" if device else None)
    assert hyllycopy.name_problem(f"ebooks/{name}", False) is None
    for windows in (True, False):
        monkeypatch.setattr(paths, "WINDOWS", windows)
        assert serve.plain_words(f"ebooks/{name.rstrip('. ')}/x.epub") is not (device and windows)
    if hasattr(ntpath, "isreserved") and name == name.strip(". "):  # Python 3.13's own list of them is within ours
        assert not ntpath.isreserved(name) or device, name


def test_the_server_finds_a_file_as_a_record_s_file_is_found_its_links_followed(tmp_path):
    """serve.library_path is paths.library_file, resolved, with the names Windows can't hold refused on Windows: a link
    inside the library is the file it leads to, so a link to a hidden file is seen to be hidden (Handler._servable),
    while a record's file keeps the name the library gives it; a link out of the library, to a file or a folder, is
    neither, and links that go round in a circle are no file."""
    root, outside = tmp_path / "library", tmp_path / "outside"
    (root / "ebooks").mkdir(parents=True)
    outside.mkdir()
    (root / "ebooks" / ".hidden.epub").write_bytes(b"x")
    (outside / "away.epub").write_bytes(b"x")
    try:
        (root / "ebooks" / "seen.epub").symlink_to(root / "ebooks" / ".hidden.epub")
        (root / "ebooks" / "out.epub").symlink_to(outside / "away.epub")
        (root / "elsewhere").symlink_to(outside, target_is_directory=True)
        (root / "ebooks" / "round").symlink_to(root / "ebooks" / "about")
        (root / "ebooks" / "about").symlink_to(root / "ebooks" / "round")
    except OSError:
        pytest.skip("this computer won't make links")
    assert serve.library_path(root, "/ebooks/seen.epub") == (root / "ebooks" / ".hidden.epub").resolve()
    assert paths.library_file(root, "ebooks/seen.epub") == root / "ebooks" / "seen.epub"
    assert not serve.Handler._servable(root.resolve(), serve.library_path(root, "/ebooks/seen.epub"))
    for rel in ("ebooks/out.epub", "elsewhere/away.epub"):
        assert serve.library_path(root, "/" + rel) is None and paths.library_file(root, rel) is None, rel
    # Links that go round in a circle are never a file, and never an error: Python 3.13 resolves them to a path that
    # isn't one, 3.11 and 3.12 raise RuntimeError, which paths.library_file takes for None, so that there a request
    # through such a link is refused (404) rather than reaching the server's answer to what went wrong
    for rel in ("ebooks/round", "ebooks/round/x.epub"):
        for found in (serve.library_path(root, "/" + rel), paths.library_file(root, rel)):
            assert found is None or not found.is_file(), rel
    # A catalog build's look (paths.LibraryFiles) is library_file's: a link out refused, to a file or through a folder,
    # links round in a circle no file, and a link inside the file it leads to, named as the library names it
    files = paths.LibraryFiles(root)
    for rel in ("ebooks/out.epub", "elsewhere/away.epub", "ebooks/round", "ebooks/round/x.epub", "../outside/away.epub",
                "ebooks/../../outside/away.epub", "/ebooks/seen.epub", ""):
        assert files.file(rel) is None, rel
    seen, st = files.file("ebooks/seen.epub")
    assert seen == root / "ebooks" / "seen.epub" and st.st_ino == (root / "ebooks" / ".hidden.epub").stat().st_ino


def test_a_build_resolves_the_library_and_each_folder_once_and_a_file_only_when_it_is_a_link(tmp_path, monkeypatch):
    """paths.library_file resolves the library and every folder on the way at each call: on a share, a round trip each,
    twice a title. A build's look resolves the library once, each folder once, and a file itself only when it is a
    link, which can lead anywhere; each file is looked at once in the build, and comes out as library_file has it."""
    root = tmp_path / "library"
    (root / "ebooks" / "Stories").mkdir(parents=True)
    names = [f"Tale {n}.epub" for n in range(4)]
    for name in names:
        (root / "ebooks" / "Stories" / name).write_bytes(b"x")
    try:
        (root / "ebooks" / "Stories" / "Linked.epub").symlink_to(root / "ebooks" / "Stories" / names[0])
    except OSError:
        pytest.skip("this computer won't make links")
    rels = [f"ebooks/Stories/{name}" for name in [*names, "Linked.epub", *names]]
    resolved, looked, real = [], [], Path.resolve
    with monkeypatch.context() as m:
        m.setattr(Path, "resolve", lambda self, strict=False: resolved.append(self) or real(self, strict))
        m.setattr(paths.os, "lstat", lambda path: looked.append(path) or os.stat(path, follow_symlinks=False))
        files = paths.LibraryFiles(root)
        found = [files.file(rel) for rel in rels]
    assert resolved == [root, root / "ebooks" / "Stories", root / "ebooks" / "Stories" / "Linked.epub"]
    stories = root / "ebooks" / "Stories"
    assert [sum(Path(p) == stories / name for p in looked) for name in names[1:]] == [1, 1, 1]  # each once, asked twice
    assert [f for f, _ in found] == [paths.library_file(root, rel) for rel in rels]
    assert [(st.st_size, st.st_mtime_ns) for _, st in found] == [(os.stat(f).st_size, os.stat(f).st_mtime_ns)
                                                                for f, _ in found]


def odd_named(root: Path, rel: str) -> None:
    """A book under a name of WINDOWS_ONLY: a comic of one page, or an e-book."""
    f = root.joinpath(*rel.split("/"))
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.suffix == ".cbz":
        with zipfile.ZipFile(f, "w") as z:
            z.writestr("01.png", png())
    else:  # a title of its own, so that no two are taken for copies of one book
        make_epub(f, OPF.replace("The Long Dust", f.stem).replace("Marlow Ellery", "Ann Writer"), cover=False)


def acquisitions(client, feed: str) -> list[str]:
    """The files a feed offers, as their links."""
    return re.findall(r'rel="http://opds-spec.org/acquisition" href="([^"]+)"', client.get(feed).text)


def test_a_book_whose_name_only_windows_takes_for_something_else_opens_on_linux_and_macos(library, log_in):
    """By its link, as a download, in the reader and from the feeds. On Windows no such file can be made, and its address
    is refused before Windows is asked anything, as before (the two tests above)."""
    root = library
    if os.name != "nt":
        for rel in WINDOWS_ONLY.values():
            odd_named(root, rel)
        catalog.build(root, log=lambda s: None)
    srv, client = start(root)
    log_in(client)
    try:
        listed = {unquote(f[0]): it for it in srv.RequestHandlerClass.catalog.get() for f in it.get("f", [])}
        offered = acquisitions(client, "/opds/src/ebooks") + acquisitions(client, "/opds/src/comics")
        for kind, rel in WINDOWS_ONLY.items():
            link = "/" + quote(rel)
            if os.name == "nt":
                assert rel not in listed and client.get(link).status_code == 404, kind
                continue
            assert link in offered, kind
            answer = client.get(link, params={"dl": "1"})
            assert answer.status_code == 200 and answer.content == root.joinpath(*rel.split("/")).read_bytes(), kind
            assert answer.headers["content-disposition"].startswith("attachment;"), kind
            if rel.endswith(".cbz"):
                assert client.get("/api/pages", params={"id": listed[rel]["id"]}).json()["pages"] == 1, kind
    finally:
        srv.shutdown()


def test_only_what_the_page_needs_of_hyllys_own_folders(server):
    _, client, root = server
    (root / "curation").mkdir(exist_ok=True)
    (root / "curation" / "edits.jsonl").write_text('{"by": "lucy"}\n', encoding="utf-8")
    (root / "catalog" / "owned.csv").write_text("title,key\n", encoding="utf-8")
    (root / "state" / "lookups").mkdir(parents=True, exist_ok=True)
    (root / "state" / "lookups" / "lookup-openlibrary.jsonl").write_text("{}\n", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.pdf").write_bytes(b"%PDF")
    for path in ("/curation/edits.jsonl", "/catalog/owned.csv", "/state/lookups/lookup-openlibrary.jsonl", "/.hidden/x.pdf"):
        assert client.get(path).status_code == 404, path
    for path in ("/catalog/index.html", "/catalog/data.js", "/catalog/help.html"):
        assert client.get(path).status_code == 200, path
    assert client.get("/catalog/data.js").headers["content-type"].startswith("text/javascript")  # Hylly's own, as a script
    (root / "catalog" / "tags.js").write_text("window.TAGS = {};\n", encoding="utf-8")  # as an earlier version left it
    assert client.get("/catalog/tags.js").status_code == 404  # no page asks for it now, and the library's isn't Hylly's


def rewrite_data(root: Path, change) -> None:
    """data.js changed by hand (change takes its items and gives them back), a little newer, so a server reads it again."""
    path = root / "catalog" / "data.js"
    text = path.read_text(encoding="utf-8")
    data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    data["items"] = change(data["items"])
    before = path.stat().st_mtime
    path.write_text("window.CATALOG = " + json.dumps(data) + ";\n", encoding="utf-8")
    os.utime(path, (before + 5, before + 5))


TOOLS_AT_DEPTH = [  # what tools keep among your books: no build reads these folders (catalog.systems_own), at any depth
    "comics/Neon Comics/.stversions/Neon Rangers 002~20261101-120000.cbz", "collections/Poetry/.Trash-1000/files/Old Tide.epub",
    "collections/.caltrash/Moonwake Tide.epub", "comics/.Moonwake/.git/Moonwake 000.cbz", "collections/Poetry/.sync/Archive/Tide.epub"]


def test_a_name_starting_with_a_dot_is_served_only_when_the_catalog_lists_that_very_file(tmp_path, monkeypatch, log_in):
    """A shop's title starting with a dot ('.Lantern Tales') gets a folder so named (paths.clean keeps the dot), and a
    folder of yours may have one: listed in the page and the feeds, each opens by its link, as a download, as a part
    (Range), by HEAD, in the reader, from the feeds, and a book inside a ZIP package there. What no build listed stays
    hidden: at the top .git, a trash or a sync tool's folder; theirs at any depth, which no build reads; and beside a
    listed file one put there since. The catalog is asked by the path as resolved: on Windows the disk's own spelling,
    on Linux and macOS the spelling asked."""
    from hylly.download import Manifest

    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path / "cache")
    build_library(tmp_path)
    bundle = {"name": "The Lantern Video Game Bundle", "purchased": "2020-01-01", "bonus": False,
              "books": [{"id": "7", "title": "Arcade Worlds", "approx_size": 1, "cover": ""},
                        {"id": "8", "title": ".Lantern Tales", "approx_size": 1, "cover": ""}]}
    (tmp_path / "state" / "storybundle" / "2026-01-02_0000.json").write_text(json.dumps([bundle]), encoding="utf-8")
    shop = tmp_path / "storybundle" / "The Lantern Video Game Bundle" / ".Lantern Tales" / "Lantern_Tales_-_Ann_Writer.epub"
    shop.parent.mkdir(parents=True)
    make_epub(shop, OPF.replace("The Long Dust", ".Lantern Tales").replace("Marlow Ellery", "Ann Writer"), cover=False)
    Manifest(tmp_path, "storybundle").add({"path": shop.relative_to(tmp_path).as_posix(), "size": shop.stat().st_size,
                                           "item": "8", "file": 0, "ext": "epub", "md5": "z"})
    odd_named(tmp_path, "comics/.Moonwake/Moonwake 001.cbz")  # a folder of yours
    odd_named(tmp_path, "comics/Neon Comics/.Neon Rangers 009.cbz")  # a file of yours
    odd_named(tmp_path, "packed.epub")
    package = tmp_path / "collections" / ".Moonwake Press" / "Moonwake Extras.zip"  # a ZIP package, with a book inside
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package, "w") as z:
        z.write(tmp_path / "packed.epub", "Moonwake Extras.epub")
    (tmp_path / "packed.epub").unlink()
    for rel in TOOLS_AT_DEPTH:
        odd_named(tmp_path, rel)
    catalog.build(tmp_path, log=lambda s: None)
    dotted = ["storybundle/The Lantern Video Game Bundle/.Lantern Tales/Lantern_Tales_-_Ann_Writer.epub",
              "comics/.Moonwake/Moonwake 001.cbz", "comics/Neon Comics/.Neon Rangers 009.cbz",
              "collections/.Moonwake Press/Moonwake Extras.zip"]
    srv, client = start(tmp_path)
    log_in(client)
    try:
        cat = srv.RequestHandlerClass.catalog
        listed = {unquote(f[0]): it for it in cat.get() for f in it.get("f", [])}
        offered = acquisitions(client, "/opds/src/storybundle") + acquisitions(client, "/opds/src/comics")
        for rel in dotted:
            link = "/" + quote(rel)
            assert rel in listed and (link in offered or rel.endswith(".zip")), rel
            whole = (tmp_path / rel).read_bytes()
            for answer in (client.get(link), client.get(link, params={"dl": "1"})):
                assert answer.status_code == 200 and answer.content == whole, rel
            part = client.get(link, headers={"Range": "bytes=0-9"})
            assert part.status_code == 206 and part.content == whole[:10], rel
            head = client.head(link)
            assert head.status_code == 200 and head.headers["content-length"] == str(len(whole)) and not head.content, rel
        assert client.get("/api/pages", params={"id": listed[dotted[1]]["id"]}).json()["pages"] == 1
        inside = listed[dotted[3]]["z"][0][0]  # the book inside the package, as the page and the feeds offer it
        assert unquote(inside) == "collections/.Moonwake Press/Moonwake Extras.zip/Moonwake Extras.epub"
        with zipfile.ZipFile(package) as z:
            book = z.read("Moonwake Extras.epub")
        assert client.get("/" + inside).content == book
        assert client.get("/" + inside, headers={"Range": "bytes=10-19"}).content == book[10:20]
        for rel in TOOLS_AT_DEPTH:  # never listed, so never served
            assert rel not in listed and client.get("/" + quote(rel)).status_code == 404, rel
        unlisted = [".git/config", ".Trash-1000/files/Old Tide.epub", ".stfolder/Moonwake 002.cbz", ".hidden.epub",
                    "comics/.Moonwake/Moonwake 002.cbz", "comics/.Moonwake/notes.txt", "comics/Neon Comics/.Neon Rangers 010.cbz",
                    "storybundle/The Lantern Video Game Bundle/.Lantern Tales/.DS_Store"]
        for rel in unlisted:
            if rel.endswith((".cbz", ".epub")):
                odd_named(tmp_path, rel)
            else:
                (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp_path / rel).write_text("not for anyone", encoding="utf-8")
            link = "/" + quote(rel)
            for answer in (client.get(link), client.head(link), client.get(link, headers={"Range": "bytes=0-9"})):
                assert answer.status_code == 404, rel
        shutil.copy(package, tmp_path / "comics" / ".Moonwake" / "Unlisted.zip")  # a package no build listed, nor what's in it
        assert client.get("/" + quote("comics/.Moonwake/Unlisted.zip/Moonwake Extras.epub")).status_code == 404
        # by the path as resolved, exactly, and nothing like it
        root, target = tmp_path.resolve(), (tmp_path / dotted[1]).resolve()
        asked = []
        assert not serve.Handler._servable(root, target, lambda rel: asked.append(rel) or False) and asked == [dotted[1]]
        assert serve.Handler._servable(root, target, cat.lists) and serve.Handler._servable(root, target, {dotted[1]}.__contains__)
        for near in (dotted[1].lower(), dotted[1].upper(), "/" + dotted[1], quote(dotted[1]), dotted[1].replace("/", "\\")):
            assert not cat.lists(near) and not serve.Handler._servable(root, target, {near}.__contains__), near
        assert not cat.lists("comics/Neon Comics/Neon Rangers 002.cbz")  # listed, but only those with a dot are kept to ask
        # a title gone from the catalog goes from the server with it; and at the top of the library, nothing with a dot
        rewrite_data(tmp_path, lambda items: [it for it in items if it["id"] != listed[dotted[1]]["id"]]
                     + [{"id": "planted", "t": "Planted", "k": "book", "src": "comics", "f": [[".git/config", "epub", 1]]}])
        assert client.get("/" + quote(dotted[1])).status_code == 404
        assert client.get("/.git/config").status_code == 404
        assert client.get("/" + quote(dotted[0])).status_code == 200  # the rest as before
    finally:
        srv.shutdown()


def test_hylly_doctor_names_the_catalogs_files_the_server_refuses(library, monkeypatch):
    """Up to three, shown so that no name can start a line or move the cursor, and how many more. Asked of the disk only
    where the server would look closer: names Windows takes for something else are refused by their words alone."""
    from hylly import doctor

    monkeypatch.setattr(paths, "WINDOWS", False)  # first by the rules of Linux and macOS, whatever system this is
    assert doctor._refused(library) == []  # every file the catalog lists opens
    odd = "ebooks/\x1b]0;Odd\x07\u2028Tide.epub"
    odd_named(library, ".git/Planted.epub")  # at the top: a name the server never serves
    odd_named(library, "comics/.Moonwake/Moonwake 001.cbz")  # listed in other letters than the disk has them
    planted = [
        {"id": "x1", "t": "Planted", "k": "book", "src": "ebooks", "f": [[quote(".git/Planted.epub"), "epub", 1]]},
        {"id": "x2", "t": "Moonwake", "k": "comic", "src": "comics", "f": [[quote("comics/.MOONWAKE/Moonwake 001.cbz"), "cbz", 1]]},
        {"id": "x3", "t": "Gone", "k": "comic", "src": "comics", "f": [[quote("comics/.Gone/Gone 001.cbz"), "cbz", 1]]},
        {"id": "x4", "t": "Odd", "k": "book", "src": "ebooks", "f": [[quote(odd), "epub", 1]]},
        *({"id": f"w{n}", "t": kind, "k": "book", "src": "ebooks", "f": [[quote(rel), "epub", 1]]}
          for n, (kind, rel) in enumerate(WINDOWS_ONLY.items()))]
    rewrite_data(library, lambda items: items + planted)
    # Windows opens .MOONWAKE as .Moonwake, and the server asks by the disk's own spelling, which no build listed; on
    # Linux there's no such folder, and a Mac's disk keeps the spelling asked. The one gone isn't refused, but gone.
    other_letters = ["comics/.MOONWAKE/Moonwake 001.cbz"] if os.name == "nt" else []
    found = serve.refused_files(library, Catalog(library))
    assert found == [".git/Planted.epub", *other_letters, odd]
    shown = ", ".join([".git/Planted.epub", *other_letters, "ebooks/?]0;Odd??Tide.epub"])
    assert doctor._refused(library) == [{
        "id": "books-refused", "what": "Books", "state": "info",
        "says": f"{len(found)} files the catalog lists are refused by the server, so no link, reader or reading app can open them: {shown}.",
        "next": "hylly catalog lists them again as they are on the disk now. A file reached through a link that leads out of the library "
                "is never served.", "run": "hylly catalog"}]
    monkeypatch.setattr(paths, "WINDOWS", True)  # by Windows' rules, those names are refused too, whether on the disk or not
    found = serve.refused_files(library, Catalog(library))
    assert found == [".git/Planted.epub", *other_letters, odd, *WINDOWS_ONLY.values()]
    first = ", ".join(catalog.shown(rel) for rel in found[:3])
    said = doctor._refused(library)[0]
    assert said["says"] == (f"{len(found)} files the catalog lists are refused by the server, so no link, reader or reading app "
                            f"can open them: {first}, and {len(found) - 3} more.")
    assert said["next"] == ("Windows takes a name with a colon or a backslash, one ending in a dot or a space, or one named as a "
                            "device (CON, AUX, NUL, COM1) for something else: rename those, then run hylly catalog.")
    assert said["owner"] is True and "run" not in said  # the library's files renamed: the owner's
    assert not re.search(r"[\x00-\x1f\u2028]", doctor.text({"checks": [said], "problems": 0, "updates": 0}).replace("\n", ""))
    built = lambda items: [it for it in items if it not in planted]  # noqa: E731
    rewrite_data(library, lambda items: built(items) + planted[:1])  # one of them alone, in words for one
    assert doctor._refused(library) == [{
        "id": "books-refused", "what": "Books", "state": "info",
        "says": "1 file the catalog lists is refused by the server, so no link, reader or reading app can open it: .git/Planted.epub.",
        "next": "hylly catalog lists it again as it is on the disk now. A file reached through a link that leads out of the library "
                "is never served.", "run": "hylly catalog"}]
    rewrite_data(library, lambda items: built(items) + planted[4:5])
    said = doctor._refused(library)[0]
    assert said["says"].startswith("1 file the catalog lists is refused") and said["next"].endswith(": rename it, then run hylly catalog.")


def link_to(real: Path, link: Path) -> None:
    """A link to a folder, as anyone may make one: a junction on Windows, which needs no rights of its own."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
    else:
        os.symlink(real, link, target_is_directory=True)


def test_hyllys_own_folders_are_known_by_what_they_are_not_by_how_an_address_spells_them(server):
    # whether this disk folds a spelling or not: a Mac's may take ſtate (a long s) for state
    _, client, root = server
    (root / "state" / "library.json").write_text("{}", encoding="utf-8")
    (root / "curation").mkdir(exist_ok=True)
    (root / "curation" / "edits.jsonl").write_text("{}\n", encoding="utf-8")
    for path in ("/state/library.json", "/State/library.json", "/STATE/library.json", "/%C5%BFtate/library.json",
                 "/Curation/edits.jsonl", "/CURATION/edits.jsonl", "/Catalog/owned.csv"):
        assert client.get(path).status_code == 404, path
    for name in ("ſtate", "ſtATE"):  # a folder of a look-alike name, where this disk keeps it apart: as state\ all the same
        (root / name).mkdir(exist_ok=True)  # where the disk folds the long s, this is state\ itself
        (root / name / "notes.txt").write_text("nothing here", encoding="utf-8")
        assert client.get(f"/{name}/notes.txt").status_code == 404, name
    for path in ("/catalog/index.html", "/catalog/data.js", "/collections/Poetry/Herman Melville - Bartleby the Scrivener.epub"):
        assert client.get(path).status_code == 200, path  # every ordinary path as before
    (root / "Statements").mkdir()
    (root / "Statements" / "notes.txt").write_text("nothing here", encoding="utf-8")
    assert client.get("/Statements/notes.txt").status_code == 200
    # catalog's own files go by their plain names, as they are sent: a look-alike is neither one of them nor a cover
    (root / "catalog" / "coverſ").mkdir(exist_ok=True)  # where the disk folds the long s, these are its own
    for name in ("catalog/tagſ.js", "catalog/coverſ/x.webp"):
        (root / name).write_bytes(b"nothing here")
        assert client.get(f"/{name}").status_code == 404, name


def test_a_link_that_makes_another_folder_the_state_folder_doesnt_open_it_by_that_name(server):
    _, client, root = server
    kept = root / "Kept"  # state\ kept in another folder of the library, with state\ a link to it
    shutil.move(root / "state", kept)
    link_to(kept, root / "state")
    (kept / "library.json").write_text("{}", encoding="utf-8")
    for path in ("/Kept/library.json", "/kept/library.json", "/state/library.json"):
        assert client.get(path).status_code == 404, path
    assert serve._private_folder(root, "Kept") and not serve._private_folder(root, "collections")
    assert not serve._private_folder(root, "catalog")  # served in part: by its plain name (_servable)


def test_the_help_shows_this_computers_folders_only_to_the_owner(library):
    access.add_user("rory", "correct horse", role="reader")
    access.add_user("bea", "correct horse", role="borrower")
    access.add_user("owen", "correct horse", role="owner")
    from hylly import db

    users = str(db.file())
    from hylly import helppage

    copy = helppage.help_page(library, generic=True).encode("utf-8")  # the help in words, as everyone but the owner gets it
    assert users.encode() not in copy and str(library).encode() not in copy and b"hylly.db (in the data folder)" in copy

    def owners_help(answer):
        assert answer.status_code == 200 and users in answer.text and str(library) in answer.text
        assert answer.headers["cache-control"] == "no-store"  # kept by no browser or proxy
        policy = answer.headers["content-security-policy"]
        for inline in re.findall(r"<script>(.*?)</script>", answer.text, re.S):  # its own script still runs
            assert "'sha256-" + base64.b64encode(hashlib.sha256(inline.encode("utf-8")).digest()).decode() + "'" in policy
        return answer.text

    srv, client = start(library)
    owen = ("owen", "correct horse")
    try:
        for name in ("rory", "bea"):  # a reader and a borrower get the help in words
            theirs = client.get("/catalog/help.html", auth=(name, "correct horse"), headers=HOME)
            assert theirs.status_code == 200 and theirs.content == copy, name
            assert client.get("/catalog/help.html", auth=(name, "correct horse")).content == copy  # at this computer too
        owners_help(client.get("/catalog/help.html", auth=owen, headers=HOME))
        assert "(not set: see settings.example.toml)" in owners_help(client.get("/catalog/help.html", auth=owen))
        paths.settings_file().write_text('[server]\naddress = "https://library.example.org/"\n', encoding="utf-8")
        assert "https://library.example.org" in owners_help(client.get("/catalog/help.html", auth=owen))  # made afresh
        assert client.get("/catalog/help.html").status_code == 401  # this computer is nobody without a login
    finally:
        srv.shutdown()


def test_a_mistake_in_the_settings_keeps_the_owners_help(library, monkeypatch, log_in):
    from hylly import helppage

    copy = helppage.help_page(library, generic=True)
    srv, client = start(library)
    log_in(client)
    try:
        paths.settings_file().write_text('[server]\naddress = "https://library.example.org/"\n', encoding="utf-8")
        before = client.get("/catalog/help.html")
        assert before.status_code == 200 and "https://library.example.org" in before.text
        paths.settings_file().write_text("[server\nbroken", encoding="utf-8")  # while the server runs
        kept = client.get("/catalog/help.html")
        assert kept.status_code == 200 and kept.text == before.text  # the help as last made
        monkeypatch.setattr(serve, "_filled_helps", {})  # none made since the start: the help in words, as everyone gets it
        plain = client.get("/catalog/help.html")
        assert plain.status_code == 200 and plain.text == copy
        assert plain.headers["cache-control"] == "no-store"
        # made from the program's own page, never the library's copy, whatever a file of that name in the library says
        (library / "catalog" / "help.html").write_text(copy.replace("</body>", "<script>/* put here */</script></body>"), encoding="utf-8")
        monkeypatch.setattr(serve, "_filled_helps", {})
        plain = client.get("/catalog/help.html")
        assert plain.status_code == 200 and plain.text == copy and "put here" not in plain.text
        assert "'sha256-" + base64.b64encode(hashlib.sha256(b"/* put here */").digest()).decode() + "'" not in plain.headers["content-security-policy"]
        from hylly import helppage  # and if not even those words can be made: a failure the log tells of, not the copy

        monkeypatch.setattr(helppage, "help_page", lambda root, generic=False, **kw: 1 / 0)
        monkeypatch.setattr(serve, "_filled_helps", {})
        serve._filled_help.cache_clear()
        broken = client.get("/catalog/help.html")
        assert broken.status_code == 500 and "put here" not in broken.text
    finally:
        srv.shutdown()
        serve._filled_help.cache_clear()


# ---- scripts in books and files ---------------------------------------

def test_the_library_page_runs_only_its_own_scripts(server):
    _, client, root = server
    page = client.get("/catalog/index.html")
    policy = page.headers["content-security-policy"]
    scripts = re.search(r"script-src ([^;]+)", policy)[1].split()
    assert "'unsafe-inline'" not in scripts and "'unsafe-eval'" not in scripts and "blob:" not in scripts
    own = Path(serve.__file__).with_name("catalog_page.html")
    assert page.content == own.read_bytes()  # the program's page, whatever the library's folder holds
    text = own.read_text(encoding="utf-8")  # as a browser reads it: its line ends as one
    for inline in re.findall(r"<script>(.*?)</script>", text, re.S):  # each of its own inline scripts by its hash
        digest = base64.b64encode(hashlib.sha256(inline.encode("utf-8")).digest()).decode()
        assert f"'sha256-{digest}'" in scripts
    assert "frame-ancestors 'self'" in policy and "object-src 'none'" in policy and "base-uri 'none'" in policy
    assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
    assert not re.search(r"<[a-z]+\b[^>]*\son\w+=", text)  # no inline handlers in its markup: the policy would stop them
    assert "scriptless(view.book, ext)" in text  # and the reader takes a book's scripts out as well


def test_no_script_file_runs_for_its_address_alone_only_what_a_page_s_own_scripts_load(server):
    """'self' in script-src let any address of the server that answers as a script run in the page, and in a book's
    frames, which inherit its policy: one slip in what the server sends as a script would be the reader's session. Only
    a page's own inline scripts run now, by their hashes, and the files they load ('strict-dynamic'): no tag asks for
    the library page's data.js any more, its first script does, and then runs the page's own script (a module that a
    script allowed by its hash alone imports, the reader's engine, Chrome refuses: one run by a script may)."""
    _, client, root = server
    policies = {path: client.get(path).headers["content-security-policy"] for path in ("/catalog/index.html", "/catalog/help.html")}
    for path, policy in policies.items():
        scripts = re.search(r"script-src ([^;]+)", policy)[1].split()
        assert scripts[0] == "'strict-dynamic'" and all(s.startswith("'sha256-") for s in scripts[1:]), path
    text = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert not re.search(r"<script\b[^>]*\bsrc\s*=", text, re.I)
    first = [s for s in re.findall(r"<script>(.*?)</script>", text, re.S) if '"data.js"' in s]
    later = re.findall(r'<script type="text/hylly-later" id="pagescript">(.*?)</script>', text, re.S)
    assert len(first) == 1 and 'data.src = "data.js";' in first[0] and "page.text = own.text;" in first[0]
    assert len(later) == 1 and "const RD = " in later[0]
    digest = base64.b64encode(hashlib.sha256(later[0].encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in policies["/catalog/index.html"]


def test_a_book_s_frames_may_run_scripts_only_in_webkit_which_needs_that_for_their_events():
    """foliate gave each book's frame allow-scripts beside allow-same-origin, which makes the frame the library itself,
    where only the page's policy stopped a script that got past the cleaning. WebKit needs it for the frame's events to
    reach the reader; Chrome, Edge and Firefox don't. Both places that make a frame give it to WebKit alone
    (foliate/README.md)."""
    folder = Path(serve.__file__).with_name("foliate")
    line = "setAttribute('sandbox', navigator.vendor.startsWith('Apple') ? 'allow-same-origin allow-scripts' : 'allow-same-origin')"
    for name in ("paginator.js", "fixed-layout.js"):
        text = folder.joinpath(name).read_text(encoding="utf-8")
        assert text.count("setAttribute('sandbox'") == 1 and line in text, name
    others = [p.name for p in folder.glob("*.js") if p.name not in ("paginator.js", "fixed-layout.js")]
    assert others and not [name for name in others if "allow-scripts" in folder.joinpath(name).read_text(encoding="utf-8")]


def test_the_login_page_runs_only_its_own_scripts_and_a_typed_theme_marker_stays_text(library):
    """The login page once let any inline script run, and its theme went in after the typed name, inside it too, so
    the marker typed as a name came back with the theme's quotes ending the value. Now the theme and the values go in
    in one pass, and the page's policy names its own two scripts by their hashes, worked out from the template."""
    access.add_user("reader", "correct horse")
    marker = serve.Handler.THEME_MARK
    srv, client = start(library)
    try:
        page = client.post("/login", data={"username": marker, "password": "guess", "next": "/catalog/index.html"})
        link = client.get("/login", params={"once": access.sign_in_link("reader")})
        icon = client.get("/icon-180.png")  # the page's own icon, without a login
    finally:
        srv.shutdown()
    assert page.status_code == 401 and f'name="username" autocomplete="username" autocapitalize="none" autocorrect="off" ' \
                                       f'spellcheck="false" required value="{marker}" ' in page.text
    theme = serve.themes.current()[2]
    assert page.text.count(theme) == 1 and theme.splitlines()[0] in page.text.split(marker)[0]  # in the style only
    template = Path(serve.__file__).with_name("login_page.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", template, re.S)
    assert len(scripts) == 2 and all(s in page.text and s in link.text for s in scripts)
    for answer in (page, link):
        policy = answer.headers["content-security-policy"]
        allowed = re.search(r"script-src ([^;]+)", policy)[1].split()
        assert sorted(allowed) == sorted("'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
                                         for s in scripts)
        assert "style-src 'unsafe-inline'" in policy and "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
        assert "img-src 'self' data:;" in policy and f'href="/icon-180.png?v={serve.ICON_VERSION}"' in answer.text  # its icon, which its policy refused
        assert answer.headers["x-frame-options"] == "DENY" and not re.search(r"<[a-z]+\b[^>]*\son\w+=", answer.text)
    assert link.headers["referrer-policy"] == "origin" and page.headers["referrer-policy"] == "same-origin"
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"


def pictures(answer) -> list[str]:
    return re.search(r"img-src ([^;]+)", answer.headers["content-security-policy"])[1].split()


def test_pictures_come_only_from_the_library_and_the_sites_of_its_shop_covers(server):
    _, client, root = server
    own = ["'self'", "data:", "blob:"]
    path = root / "catalog" / "data.js"
    text = path.read_text(encoding="utf-8")
    start, end = text.index("{"), text.rindex("}") + 1
    data = json.loads(text[start:end])
    model = data["items"][0]
    covers = ["https://img.example.net/a.png?w=1", "https://IMG.example.net/b.png", "https://art.example.org:8443/c.png",
              "http://plain.example/d.png", "https://who" + "@hidden.example/e.png", "https://bad.example;script-src *",
              "//no-scheme.example/f.png", "covers/own.webp", ""]
    data["items"] = [{**model, "c": cover} for cover in covers]

    def write(body: str, tick: int) -> None:
        path.write_text(body, encoding="utf-8")
        os.utime(path, (tick, tick))  # a new data.js is read again, even within one tick of the clock

    write(text[:start] + json.dumps(data) + text[end:], 2_000_000_000)
    named = own + ["https://art.example.org:8443", "https://img.example.net"]
    page = client.get("/catalog/index.html")
    assert pictures(page) == named
    assert "https:" not in pictures(page)  # never any site at all
    for other in ("/catalog/help.html", "/server"):
        assert pictures(client.get(other)) == own, other  # and on the other pages, none

    again = client.get("/catalog/index.html", headers={"If-Modified-Since": page.headers["last-modified"]})
    assert again.status_code == 304 and pictures(again) == named  # the browser's copy gets a policy that has changed

    write("window.CATALOG = {", 2_000_000_010)  # can't be read: the page still opens, with the sites it knew
    page = client.get("/catalog/index.html")
    assert page.status_code == 200 and pictures(page) == named


def test_the_library_page_is_unchanged_only_for_a_browser_holding_that_very_page(server):
    """A browser holding another version's page, one the server has gone back from since, would keep it under this
    version's policy, which runs none of its scripts, and the library would stay empty: so the page is a 304 only for
    its own tag, or else its own date exactly, never for a date that is merely no older."""
    import time
    from email.utils import formatdate

    _, client, _ = server
    page = client.get("/catalog/index.html")
    tag, modified = page.headers["etag"], page.headers["last-modified"]
    assert page.status_code == 200 and client.head("/catalog/index.html").headers["etag"] == tag
    plain = client.get("/catalog/index.html", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.headers["etag"] == tag
    same = client.get("/catalog/index.html", headers={"If-None-Match": tag})
    assert same.status_code == 304 and same.headers["etag"] == tag and same.headers["content-security-policy"]
    newer = formatdate(time.time() + 365 * 86400, usegmt=True)  # a newer version's page
    for asked in ({"If-Modified-Since": newer}, {"If-None-Match": '"another"', "If-Modified-Since": modified}):
        again = client.get("/catalog/index.html", headers=asked)
        assert again.status_code == 200 and again.headers["etag"] == tag and again.text == page.text, asked
    assert client.get("/catalog/index.html", headers={"If-Modified-Since": modified}).status_code == 304


def test_the_reader_narrows_every_format_of_book_to_its_own_pictures(server):
    _, client, _ = server
    text = client.get("/catalog/index.html").text
    book = re.search(r'BOOK_POLICY = "([^"]+)"', text)[1]
    assert re.search(r"img-src ([^;]+)", book)[1].split() == ["data:", "blob:"] and "media-src data: blob:" in book
    assert "doc.head.prepend(meta)" in text  # first in each page, so that nothing before it is fetched
    assert '"x-dns-prefetch-control", "off"' in text  # nor a link's site looked up before a tap
    assert "section.load = " in text  # AZW3, MOBI and FB2 make their pages without the hook an EPUB's come through
    assert 'querySelectorAll("link")' in text  # a preconnect reaches out, and no policy stops it


# What the reader makes of a book's part by the type the book gives it, and the type a page of it goes out as
PART_TYPES = {
    "application/xhtml+xml": ("page", "application/xhtml+xml"), "text/html": ("page", "text/html"),
    "image/svg+xml": ("page", "image/svg+xml"), "application/xml": ("page", "application/xml"),
    "text/xml": ("page", "application/xml"), "application/x-anything+xml": ("page", "application/xml"),  # shown as XML
    " Application/XHTML+XML; charset=iso-2022-jp": ("page", "application/xhtml+xml"),  # never the book's own characters
    "text/x-html": ("page", "text/html"), "image/svg": ("page", "text/html"), "application/xml-dtd": ("page", "text/html"),
    "html": ("page", "text/html"),
    "": ("unknown", "text/html"), None: ("unknown", "text/html"), "chapter": ("unknown", "text/html"),
    "unknown/unknown": ("unknown", "text/html"), "*/*": ("unknown", "text/html"),  # a browser guesses at these
    "text/css": ("style", None), "TEXT/CSS; charset=latin1": ("style", None),
    "image/png": ("file", None), "image/jpeg": ("file", None), "font/woff2": ("file", None),
    "application/x-font-ttf": ("file", None), "application/vnd.ms-opentype": ("file", None),
    "audio/mpeg": ("file", None), "video/mp4": ("file", None),
    "text/plain": ("bytes", None), "application/pdf": ("bytes", None), "application/javascript": ("bytes", None),
    "application/octet-stream": ("bytes", None), "text/xsl": ("bytes", None), "application/vnd.ms-opentypex": ("bytes", None),
}
# A part of no known type, by its first bytes: a picture or a font goes on as bytes (an AZW3 may give its fonts no type)
FIRST_BYTES = {"png": ([0x89, 0x50, 0x4E, 0x47, 13, 10], True), "jpeg": ([0xFF, 0xD8, 0xFF, 0xE0], True),
               "woff2": (list(b"wOF2\0\1"), True), "truetype": ([0, 1, 0, 0, 0, 9], True), "opentype": (list(b"OTTO"), True),
               "a page": (list(b"<html><p>"), False), "a style": (list(b"@import url(x);"), False), "nothing": ([], False)}
XHTML, SVG, MATHML = "http://www.w3.org/1999/xhtml", "http://www.w3.org/2000/svg", "http://www.w3.org/1998/Math/MathML"
LINKS = {(XHTML, "area"): True, (XHTML, "a"): False, (SVG, "a"): True, (MATHML, "mi"): True, (SVG, "image"): False,
         (XHTML, "img"): False}  # only an HTML <a> keeps its address: foliate follows it, in the book or in a new tab
# A tap, by the elements it went through (innermost first; namespace, name, attributes, type, in a form): whether the
# reader stops what the browser would do with it. Only what would take the book's frame to another address is stopped.
TAPS = {
    "an image map's area": ([(XHTML, "area", ["href"], "", False)], True),
    "an area without an address": ([(XHTML, "area", [], "", False)], True),
    "words in an SVG link": ([(SVG, "text", [], "", False), (SVG, "a", ["xlink:href"], "", False)], True),
    "a MathML link": ([(MATHML, "mi", ["href"], "", False)], True),
    "anything else with an address": ([(XHTML, "span", ["foo:href"], "", False), (XHTML, "p", [], "", False)], True),
    "a form's button of no type": ([(XHTML, "button", [], "submit", True)], True),
    "a word in a form's button": ([(XHTML, "b", [], "", False), (XHTML, "button", ["type"], "submit", True)], True),
    "a form's submit field": ([(XHTML, "input", ["type"], "submit", True)], True),
    "a form's picture button": ([(XHTML, "input", ["type", "src"], "image", True)], True),
    "an HTML link (foliate's to follow)": ([(XHTML, "span", [], "", False), (XHTML, "a", ["href"], "", False)], False),
    "a details box's summary": ([(XHTML, "summary", [], "", False), (XHTML, "details", [], "", False)], False),
    "a checkbox": ([(XHTML, "input", ["type"], "checkbox", True)], False),
    "a form's reset button": ([(XHTML, "button", ["type"], "reset", True)], False),
    "a button outside any form": ([(XHTML, "button", [], "submit", False)], False),
    "a sound's own controls": ([(XHTML, "audio", ["controls", "src"], "", False)], False),
    "plain text": ([(XHTML, "p", ["id"], "", False), (XHTML, "body", [], "", False)], False),
}


def test_the_reader_cleans_every_part_a_browser_could_show_as_a_page_whatever_type_the_book_gives_it():
    """The book states its parts' types, and the browser goes by them: a chapter called application/xml, or nothing at
    all, once reached the frame uncleaned. Now only pictures, fonts, sounds and videos pass as they are, anything else
    that isn't a page or a style goes as plain bytes, and a page goes out as the browser will read it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the reader's type rules run only in a browser here")
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    code = re.search(r"^const XHTML_NS = .*?^const utf8 = [^\n]*\n", page, re.S | re.M)[0]  # the rules, and what they need
    sniff = [f"pictureOrFont(new Uint8Array({json.dumps(b)}))" for b, _ in FIRST_BYTES.values()]
    sniff += ["pictureOrFont(new Blob([new Uint8Array([137, 80, 78, 71])]))", "pictureOrFont(new Uint8Array(4).buffer)",
              "pictureOrFont('<p>')"]  # as a file, as bytes of nothing, as text
    script = (f"{code}\nPromise.all([{', '.join(sniff)}]).then(sniffed => process.stdout.write(JSON.stringify({{\n"
              f"  kinds: {json.dumps(list(PART_TYPES))}.map(t => [partKind(t), pageType(t)]), sniffed,\n"
              f"  utf8: utf8('<p>a</p>', 'text/html'),\n"
              f"  links: {json.dumps(list(LINKS))}.map(([namespaceURI, localName]) => FRAME_LINK({{ namespaceURI, localName }})),\n"
              f"  taps: {json.dumps([path for path, _ in TAPS.values()])}.map(path => movesFrame([...path.map(\n"
              f"    ([namespaceURI, localName, names, type, form]) => ({{ nodeType: 1, namespaceURI, localName, type, form: form ? {{}} : null,\n"
              f"      attributes: names.map(name => ({{ name, localName: name.split(':').pop() }})) }})), {{ nodeType: 9 }}, {{}}])),\n"
              f"}})));\n")
    out = json.loads(subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    for type_, (kind, read), (want_kind, want_read) in zip(PART_TYPES, out["kinds"], PART_TYPES.values()):
        assert kind == want_kind and (want_read is None or read == want_read), type_
    assert out["sniffed"] == [want for _, want in FIRST_BYTES.values()] + [True, False, False]
    assert out["utf8"] == {"data": "\ufeff<p>a</p>", "type": "text/html;charset=utf-8"}  # a byte-order mark, and so it says
    assert out["links"] == list(LINKS.values())
    for what, stopped, (_, want) in zip(TAPS, out["taps"], TAPS.values()):
        assert stopped == want, what


def test_a_cleaned_page_is_read_as_the_cleaner_read_it(server):
    """A page that named a character set of its own was read by the browser otherwise than by the cleaner, and an
    element the cleaner saw only in a comment stood in the page. Instructions to the browser go too."""
    _, client, _ = server
    text = client.get("/catalog/index.html").text
    frames = re.search(r'const FRAMES = "([^"]+)"', text)[1].split(", ")
    assert {"script", "noscript", "meta[http-equiv]", "meta[charset]"} <= set(frames)  # noscript reads otherwise in a frame
    clean = text[text.index("function withoutScripts("):text.index("const withoutOutside = ")]
    assert clean.index("NodeFilter.SHOW_PROCESSING_INSTRUCTION") < clean.index("BOOK_FONTS")  # every page, book fonts or not:
    assert "PROCESSING_INSTRUCTION_NODE" not in clean  # an XSLT sheet could write any cleaned page anew
    assert r'.replace(/^<\?xml\s[^?]*\?>/, "")' in clean  # the declaration names the book's own characters
    assert 'root.prepend(doc.createElementNS(XHTML_NS, "head"))' in clean  # the book's policy has its place in every XHTML page
    assert 'doc = read(as = "text/html")' in clean and "return utf8(out, as);" in clean  # sent as it was read, saying UTF-8
    listener = text[text.index('t.addEventListener("data"'):text.index("// An EPUB's pages come through")]
    assert 'detail.type = "application/octet-stream"' in listener and "detail.type = done.then(made => made.type," in listener
    assert "new Blob([clean.data], { type: clean.type })" in text  # AZW3, MOBI and FB2 pages too


def test_a_tap_on_a_link_in_a_picture_cant_take_a_books_page_elsewhere(server):
    """An image map's area or an SVG's link was followed by the book's frame itself, past foliate (which follows only an
    HTML <a>), and in an AZW3 to markup nothing had cleaned."""
    _, client, _ = server
    text = client.get("/catalog/index.html").text
    clean = text[text.index("function withoutScripts("):text.index("const withoutOutside = ")]
    assert '(frameLink && (name === "href" || name.endsWith(":href")))' in clean
    first = text[text.index("function bookDoc(doc) {"):text.index("  const upright = ")]  # before its body is looked at
    assert 'doc.addEventListener("click", e => { if (movesFrame(e.composedPath())) e.preventDefault(); }, true);' in first
    assert 'doc.addEventListener("submit", e => e.preventDefault(), true);' in first  # a form sent by Enter in a field
    assert 'e.target.closest("a[href]"))) e.preventDefault()' not in text  # every other tap does what it did: a details box opens


# A book's link to another site, as foliate hands it over (its href as the book writes it): the reader's note, and what
# its Open then opens. None: no note, and nothing opens.
LINKS_OUT = {
    "https://moonwake.example/books?id=1": ("A link to moonwake.example", "https://moonwake.example/books?id=1"),
    "HTTP://Lantern.Example:8080/wolf": ("A link to lantern.example", "http://lantern.example:8080/wolf"),
    "  https://moonwake.example/ ": ("A link to moonwake.example", "https://moonwake.example/"),
    "https://library.example@other.example/login": ("A link to other.example", "https://library.example@other.example/login"),
    "//moonwake.example/books": ("A link to moonwake.example", "https://moonwake.example/books"),  # no scheme: https:
    " //Lantern.Example:8080/wolf": ("A link to lantern.example", "https://lantern.example:8080/wolf"),
    "mailto:ann.writer@example.org?subject=Hi": ("An email to ann.writer@example.org", "mailto:ann.writer@example.org?subject=Hi"),
    "mailto:": ("An email", "mailto:"),
    # what the note shows of an address has no marks or overrides of the text's direction, and no line breaks
    "mailto:%E2%80%AEann.writer@example.org%0A": ("An email to ann.writer@example.org", "mailto:%E2%80%AEann.writer@example.org%0A"),
    "mailto:%C2%85ann%E2%80%8E.writer%E2%81%A6@example.org%E2%81%A9": (
        "An email to ann.writer@example.org", "mailto:%C2%85ann%E2%80%8E.writer%E2%81%A6@example.org%E2%81%A9"),
    "tel:+3580000000": None, "sms:+3580000000": None, "file:///C:/Users/Public/notes.txt": None,
    "ftp://files.example/book.txt": None, "javascript:void(0)": None, "data:text/plain,hello": None,
    "blob:https://moonwake.example/1": None, "some-app:open": None, "chapter2.xhtml": None, "/OEBPS/chapter2.xhtml": None,
    "#note-3": None, "?page=2": None, "": None, None: None,
}


def test_a_books_link_to_another_site_opens_only_as_a_web_or_email_address_with_no_hold_on_the_library():
    """foliate opened a book's link to another site itself, with a bare window.open: the site got a hold on the library's
    tab (window.opener), and could send it to a false login page; and any kind of address went, one that starts a
    program too. Now the reader takes every such link over: a web or an email address shows where it goes first, the
    site it really goes to, and Open opens it with noopener and noreferrer; anything else does nothing at all. A note
    that was showing (a book's end, a way back) steps aside for the link's and is back once it goes."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the reader's links run only in a browser here")
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    code = re.search(r"^const NO_SITE = .*?^function linkNoteGone\(\) \{.*?^\}\n", page, re.S | re.M)[0]
    script = (f"{code}\nconst opened = [], els = {{}};\n"
              "var window = { open: (...args) => { opened.push(args); return null; } };\n"
              "const $ = id => els[id] || (els[id] = (() => { const on = new Set();\n"
              "  return { hidden: true, textContent: '', classList: { add: c => on.add(c), remove: c => on.delete(c), contains: c => on.has(c) } }; })());\n"
              f"process.stdout.write(JSON.stringify({json.dumps(list(LINKS_OUT))}.map(href => {{\n"
              "  let stopped = false;\n"
              "  opened.length = 0;\n"
              "  for (const id in els) delete els[id];\n"
              "  $('rwayback').hidden = false;  // a note already there: a book's end, say\n"
              "  bookLinkOut({ detail: { a: {}, href_: href }, preventDefault: () => { stopped = true; } });\n"
              "  const note = $('rlinkout').hidden ? null : $('rlinktext').textContent, held = $('rwayback').classList.contains('held');\n"
              "  const before = opened.length;\n"
              "  if (note !== null) $('rlinkopen').onclick();  // Open, tapped\n"
              "  return { stopped, before, note, held, opened: opened.slice(),\n"
              "    after: [$('rlinkout').hidden, $('rwayback').classList.contains('held'), $('rwayback').hidden] };\n"
              "})));\n")
    out = json.loads(subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    for href, got, want in zip(LINKS_OUT, out, LINKS_OUT.values()):
        assert got["stopped"] and got["before"] == 0, href  # never foliate's own window.open; nothing before Open
        assert got["after"] == [True, False, False], href  # the link's note gone, the one before it there as it was
        if want is None:
            assert got["note"] is None and not got["held"] and got["opened"] == [], href
        else:
            assert got["note"] == want[0] and got["held"], href  # the note before it steps aside meanwhile
            assert got["opened"] == [[want[1], "_blank", "noopener,noreferrer"]], href
    opening = page[page.index("async function openBook("):page.index("await view.open(book);")]
    assert 'view.addEventListener("external-link", bookLinkOut);' in opening  # before the book's first page
    # the link's note goes as its page goes, as another note comes, and as the book closes; the note before it comes back
    assert "linkNoteGone();" in page[page.index("function bookPlace(d) {"):page.index("  RD.place = d;")]
    assert "linkNoteGone();" in page[page.index("function showNote("):page.index('  $("rrate").hidden = true;')]
    assert "linkNoteGone();" in page[page.index("function leaveBook() {"):page.index("  RD.seq++;")]
    assert '<span id="rlinktext"></span><button id="rlinkopen">Open</button>' in page
    view = Path(serve.__file__).with_name("foliate").joinpath("view.js").read_text(encoding="utf-8")
    assert "this.#emit('external-link', { a, href_ }, true))" in view  # foliate's own window.open waits on the event,
    assert ".then(x => x ? globalThis.open(href_, '_blank') : null)" in view  # and a cancelled one opens nothing


def test_markup_in_a_template_never_reaches_a_books_page(server):
    """The cleaning doesn't look inside a <template>, and a browser makes one with shadowrootmode a live part of the
    page: an object, a frame or a picture from another site in it stood in the book's page."""
    _, client, _ = server
    text = client.get("/catalog/index.html").text
    assert "template" in re.search(r'const FRAMES = "([^"]+)"', text)[1].split(", ")  # every template, whatever it says


def test_a_page_or_script_in_the_library_cant_run_as_the_library(server):
    _, client, root = server
    folder = root / "Notes"
    folder.mkdir()
    (folder / "page.html").write_text("<script>fetch('/api/session')</script>", encoding="utf-8")
    (folder / "picture.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", encoding="utf-8")
    (folder / "code.js").write_text("fetch('/api/session')", encoding="utf-8")
    for name in ("page.html", "picture.svg"):
        r = client.get(f"/Notes/{name}")
        assert r.headers["content-security-policy"].startswith("sandbox;") and "nosniff" in r.headers["x-content-type-options"]
    js = client.get("/Notes/code.js")
    assert js.headers["content-type"].startswith("text/plain") and js.headers["x-content-type-options"] == "nosniff"
    package = root / "Notes" / "pack.zip"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("inside.html", "<script>1</script>")
    assert client.get("/Notes/pack.zip/inside.html").headers["content-security-policy"].startswith("sandbox;")


def test_a_news_page_put_in_the_catalog_folder_is_no_page_of_hyllys(server):
    # Hylly writes no catalog/news.html: one there is anybody's, so it goes as catalog/other.html does
    _, client, root = server
    planted = "<!doctype html><title>News</title><script>/* put here */</script><p>News</p>"
    for name in ("news.html", "other.html"):
        (root / "catalog" / name).write_text(planted, encoding="utf-8")
        assert (client.get(f"/catalog/{name}").status_code, client.get(f"/catalog/{name}").text) == (404, "not found"), name
    (root / "Notes").mkdir()
    (root / "Notes" / "news.html").write_text(planted, encoding="utf-8")  # elsewhere in the library: in a sandbox
    assert client.get("/Notes/news.html").headers["content-security-policy"].startswith("sandbox;")
    assert not [name for name in serve.OWN_FILES if "news" in name]


def test_safari_can_show_a_book_and_no_other_site_can_frame_the_library(server):
    # Safari gives a page's blob: frames the page's own policy, this line too. Under 'none' it refused a book's pages,
    # the book's first page never came, and every e-book waited at 100%. Comics and PDFs have no frames.
    _, client, _ = server
    ancestors = lambda path: re.search(r"frame-ancestors ([^;]+)", client.get(path).headers["content-security-policy"])[1]  # noqa: E731
    assert ancestors("/catalog/index.html") == "'self'"  # the one page with the reader
    assert ancestors("/catalog/help.html") == ancestors("/server") == "'none'"
    assert "BOOK_PATIENCE" in client.get("/catalog/index.html").text  # and a first page that never comes doesn't hang


def test_the_library_can_have_a_name_of_its_own(library, log_in):
    # the default is what it always was
    plain_srv, plain = start(library)
    log_in(plain)
    assert plain.get("/api/session").json()["name"] == "Library" and "<title>Library help</title>" in plain.get("/catalog/help.html").text
    plain_srv.shutdown()

    paths.settings_file().write_text('[server]\nname = "Harbour <Library>"\n', encoding="utf-8")
    settings.reload()  # as a server reads them at its start
    srv, client = start(library)
    log_in(client)
    try:
        escaped = "Harbour &lt;Library&gt;"
        assert client.get("/api/session").json()["name"] == "Harbour <Library>"  # the page sets it as text, not as markup
        assert client.get("/manifest.webmanifest").json()["short_name"] == "Harbour <Library>"
        help_page = client.get("/catalog/help.html").text
        assert f"<title>{escaped} help</title>" in help_page and "<title>Library" not in help_page
        login = client.get("/login", headers={"Cookie": ""}).text  # as one not logged in yet sees it
        assert f"<title>Log in · {escaped}</title>" in login and f"<h1>{escaped}</h1>" in login and "{{library}}" not in login
        assert "Harbour &lt;Library&gt; for e-readers" in client.get("/ereader/opds").text
    finally:
        srv.shutdown()


def test_word_meanings_off_leaves_wikimedia_out_of_the_page_s_policy_and_says_so_to_the_page(server):
    """The word box asked Wiktionary and Wikipedia, and the page's policy named both, with nothing in the settings to say
    no. [reader] word_meanings = false now takes both sites out of the policy of every page, and /api/session tells the
    page, whose word box then has no meanings to ask for."""
    _, client, _ = server
    connect = lambda path: re.search(r"connect-src ([^;]+)", client.get(path).headers["content-security-policy"])[1]  # noqa: E731
    assert connect("/catalog/index.html") == "'self' blob: https://*.wikipedia.org https://*.wiktionary.org"  # as it comes
    assert client.get("/api/session").json()["word_meanings"] is True
    paths.settings_file().write_text("[reader]\nword_meanings = false\n", encoding="utf-8")
    settings.reload()  # as a running server reads them again when they change
    for path in ("/catalog/index.html", "/catalog/help.html", "/server"):
        assert connect(path) == "'self' blob:", path
    assert client.get("/api/session").json()["word_meanings"] is False
    page = client.get("/catalog/index.html").text
    assert "MEANINGS = s.word_meanings !== false" in page and "const passage = !MEANINGS ||" in page  # only Bookmark, then
    paths.settings_file().write_text('[reader]\nword_meanings = "false"\n', encoding="utf-8")  # a text, in quotes
    settings.reload()
    with pytest.raises(SystemExit, match=r"word_meanings.*true or false, without quotes"):
        settings.get("reader")
    paths.settings_file().write_text("", encoding="utf-8")
    settings.reload()


def test_the_server_page_has_its_policy_too(server):
    _, client, _ = server
    page = client.get("/server")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"] and page.headers["x-frame-options"] == "DENY"


def test_no_tab_of_another_site_has_a_hold_on_hyllys_own_pages(server):
    """A site that a book's link or anything else opened from the library could send the library's tab to a false login
    page by window.opener. Hylly's own pages now tell the browser to cut that tie, both ways."""
    _, client, _ = server
    for path in ("/catalog/index.html", "/catalog/help.html", "/server", "/database", "/login"):
        page = client.get(path, headers={"Cookie": ""} if path == "/login" else {})  # the login page as one not logged in yet sees it
        assert page.status_code == 200 and page.headers["cross-origin-opener-policy"] == "same-origin", path
    page = client.get("/catalog/index.html")
    again = client.get("/catalog/index.html", headers={"If-Modified-Since": page.headers["last-modified"]})
    assert again.status_code == 304 and again.headers["cross-origin-opener-policy"] == "same-origin"  # the browser's copy too


# ---- names, proxies, and a login wherever a request comes from ------------------------------------

def test_a_name_the_server_isnt_called_by_is_refused(server):
    _, client, _ = server
    for host in ("evil.example", "rebound.example:8770", "testpc.evil.example", "other.tail1234.ts.net", "testpc.ts.net"):
        assert client.get("/catalog/data.js", headers={"Host": host}).status_code == 421, host
    for host in ("127.0.0.1:8770", "localhost", "192.168.1.5:8770", "testpc", "testpc.local", "testpc.lan", "testpc.home",
                 "TESTPC.home.arpa.", "testpc.localdomain", "[::1]:8770", "203.0.113.50:8770", "[2001:db8::5]"):
        assert client.get("/ping", headers={"Host": host}).status_code == 200, host  # any address: a rebinding needs a name
    by_tunnel = client.get("/ping", headers={"Host": "testpc.tail1234.ts.net"})  # answered to, by the tunnel's name
    assert by_tunnel.status_code == 200 and client.get("/ping", headers={"Host": "testpc.tail1234.ts.net", "Cookie": ""}).status_code == 401
    hosts = serve.Hosts(["books.example.org"])
    assert hosts.allows("books.example.org") and not hosts.allows("other.example.org") and hosts.allows(None)


def test_the_names_are_checked_without_a_lookup_in_a_request(library, monkeypatch, log_in):
    """No name server is asked in a request, so none waits on one. The computer's own name is the system's
    (gethostname), read once; address's name and allowed_hosts are compared as they are written."""
    import socket

    monkeypatch.setattr(serve, "own_name", serve.own_name.real)  # the real one, over the made-up one: the system's name below
    paths.settings_file().write_text('[server]\naddress = "https://books.example.org"\nallowed_hosts = ["library.example.net"]\n',
                                     encoding="utf-8")
    settings.reload()  # as a server reads them at its start
    srv, client = start(library, hosts=serve.Hosts.from_settings())
    log_in(client)
    asked, real = [], socket.getaddrinfo
    monkeypatch.setattr(socket, "gethostname", lambda: asked.append("gethostname") or "Lantern-PC")

    def no_lookup(name: str):
        def refused(*a, **k):
            raise AssertionError(f"{name} asked a name server in a request: {a}")
        return refused

    for name in ("getfqdn", "gethostbyaddr", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, no_lookup(name))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: real(host, *a, **k) if host in ("127.0.0.1", "localhost")
                        else no_lookup("getaddrinfo")(host))  # the test's own client connects to 127.0.0.1
    try:
        for host in ("lantern-pc", "Lantern-PC.local", "lantern-pc.tail1234.ts.net", "books.example.org", "library.example.net:443",
                     "192.168.1.5:8770", "[::1]:8770", "localhost"):
            assert client.get("/ping", headers={"Host": host}).status_code == 200, host
        for host in ("testpc", "elsewhere.example", "lantern-pc.example.com"):
            assert client.get("/ping", headers={"Host": host}).status_code == 421, host
        assert asked == ["gethostname"]  # once, at the first request that needed it
    finally:
        srv.shutdown()


# ---- other sites' pages ------------------------------------------------------

ELSEWHERE = {"Sec-Fetch-Site": "cross-site"}  # what a browser says of a request another site's page made
LINK = {**ELSEWHERE, "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "User-Agent": "Mozilla/5.0 (iPhone)"}


def test_every_answer_is_for_the_librarys_own_pages_only(server):
    _, client, root = server
    answers = [client.get(path) for path in ("/catalog/index.html", "/catalog/data.js", "/catalog/tags.js", "/catalog/help.html",
                                              "/opds", "/ereader/opds", "/api/session", "/login", "/ping", "/theme.css",
                                              "/icon-192.png", "/manifest.webmanifest", "/", "/nothing-here", "/state/x")]
    unchanged = client.get("/catalog/data.js", headers={"If-None-Match": answers[1].headers["etag"]})
    answers += [client.head("/catalog/data.js"), client.get("/ping", headers={"Host": "evil.example"}),
                client.get("/catalog/data.js", headers={**ELSEWHERE, "Sec-Fetch-Mode": "no-cors"}),
                client.post("/logout"), client.post("/api/fix", json={})]  # after Log out: 401
    for answer in [*answers, unchanged]:
        assert answer.headers["cross-origin-resource-policy"] == "same-origin", answer.request.url
    assert {a.status_code for a in answers} >= {200, 302, 303, 403, 404, 421} and unchanged.status_code == 304


def test_another_sites_page_cant_take_in_the_library_but_a_link_from_it_still_opens_it(server):
    _, client, root = server
    cover = next((root / "catalog" / "covers").iterdir()).name
    asked = (("/catalog/data.js", "script", "no-cors"), ("/catalog/tags.js", "script", "no-cors"), ("/api/session", "empty", "cors"),
             (f"/catalog/covers/{cover}", "image", "no-cors"), ("/opds", "empty", "cors"), ("/catalog/help.html", "style", "no-cors"),
             ("/collections/Poetry/Herman Melville - Bartleby the Scrivener.epub", "empty", "cors"), ("/ping", "empty", "no-cors"))
    for path, dest, mode in asked:  # a script tag, a fetch, a picture, a style sheet: whatever the page asks for
        for extra in ({}, {"Sec-Fetch-Mode": mode.upper()}, {"Sec-Fetch-Site": "CROSS-SITE"}):
            refused = client.get(path, headers={**ELSEWHERE, "Sec-Fetch-Dest": dest, "Sec-Fetch-Mode": mode, **extra})
            assert refused.status_code == 403 and "another site" in refused.text.lower(), (path, extra)
    form = client.post("/api/me/place", json={"id": "h:novel", "at": 1, "pct": 0.1}, headers={**ELSEWHERE, "Sec-Fetch-Mode": "cors"})
    assert form.status_code == 403 and "another site" in form.text.lower()
    for path in ("/catalog/index.html", "/catalog/help.html", "/catalog/data.js"):  # a link, a bookmark
        assert client.get(path, headers=LINK).status_code == 200, path
    assert client.get("/", headers=LINK).status_code == 302 and client.get("/login", headers=LINK).status_code == 303  # in already
    feed = client.get("/opds", headers=LINK)  # a feed's, opened in a browser: the library page, not the feed's XML
    assert feed.status_code == 302 and feed.headers["location"] == "/"


def test_the_librarys_own_pages_the_same_site_an_address_typed_and_reading_apps_are_answered(server):
    _, client, root = server
    cover = next((root / "catalog" / "covers").iterdir()).name
    view = next(p.name for p in (Path(serve.__file__).parent / "foliate").glob("*.js"))
    font = next(p.name for p in (Path(serve.__file__).parent / "fonts").glob("*.woff2"))
    asked = (("/catalog/data.js", "script", "no-cors"), ("/api/session", "empty", "cors"), (f"/catalog/covers/{cover}", "image", "no-cors"),
             (f"/foliate/{view}", "script", "cors"), (f"/fonts/{font}", "font", "cors"), ("/api/catalog", "empty", "cors"),
             ("/collections/Poetry/Herman Melville - Bartleby the Scrivener.epub", "empty", "cors"))
    for site in ("same-origin", "same-site", "none"):  # the library page and the reader; another port of this PC; typed
        for path, dest, mode in asked:
            r = client.get(path, headers={"Sec-Fetch-Site": site, "Sec-Fetch-Dest": dest, "Sec-Fetch-Mode": mode})
            assert r.status_code == 200, (site, path)
    for path in ("/catalog/data.js", "/opds", "/ereader/opds", "/collections/Poetry/Herman Melville - Bartleby the Scrivener.epub"):
        assert client.get(path, headers={"User-Agent": "KOReader/2026.10"}).status_code == 200, path  # no Sec-Fetch at all


def test_with_logins_a_link_from_another_site_leads_to_the_login_page_and_the_feeds_still_work(library):
    access.add_user("rory", "correct horse", role="reader")
    srv, client = start(library)
    try:
        to_login = client.get("/catalog/index.html", headers={**HOME, **LINK})
        assert to_login.status_code == 302 and to_login.headers["location"].startswith("/login?")
        assert client.get(to_login.headers["location"], headers={**HOME, **LINK}).status_code == 200
        own = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        sent = client.post("/login", data={"username": "rory", "password": "correct horse", "next": "/catalog/index.html"},
                           headers={**HOME, **own})
        assert sent.status_code == 303 and sent.headers["location"] == "/catalog/index.html"
        assert client.get("/catalog/data.js", headers={**HOME, **own, "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"}).status_code == 200
        script = {**HOME, **ELSEWHERE, "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"}  # the cookie is no help to another site
        assert client.get("/catalog/data.js", headers=script).status_code == 403
        client.cookies.clear()
        for path in ("/opds", "/ereader/opds", "/catalog/data.js"):  # a reading app, with its own login
            assert client.get(path, headers=HOME, auth=("rory", "correct horse")).status_code == 200, path
    finally:
        srv.shutdown()


def test_another_sites_form_is_refused_even_a_login_but_the_librarys_own_go_through(library):
    access.add_user("rory", "correct horse", role="reader")
    srv, client = start(library)
    try:
        login = {"username": "rory", "password": "correct horse", "next": "/catalog/index.html"}
        page = {"Host": HOME["Host"]}  # a form that says no Origin: the Origin check alone would have let it log you in
        form = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for extra in ({}, {"Origin": "http://elsewhere.example"}, {"Sec-Fetch-Mode": "no-cors"}):
            for path in ("/login", "/logout", "/api/me/place", "/api/fix"):
                refused = client.post(path, data=login, headers={**page, **form, **extra})
                assert refused.status_code == 403 and "another site" in refused.text.lower() and "set-cookie" not in refused.headers, (path, extra)
        own = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        assert client.post("/login", data=login, headers={**HOME, **own}).status_code == 303  # the login page's own form
        assert client.post("/logout", headers={**HOME, **own}).status_code == 303
        assert client.post("/login", data=login, headers=HOME).status_code == 303  # no Sec-Fetch headers at all
        key = client.get("/api/session", headers={**HOME, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}).json()["me"]["key"]
        place = client.post("/api/me/place", json={"id": "h:novel", "at": 3, "pct": 0.1},
                            headers={**HOME, "X-Personal-Key": key, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"})
        assert place.status_code == 200 and place.json()["ok"]  # the library page's own
    finally:
        srv.shutdown()


def test_the_server_and_database_pages_buttons_work_for_the_owner_from_their_own_pages(server, monkeypatch):
    # opened here, by localhost: the posts are its own, and another site's are refused
    from hylly import dashboard

    srv, client, _ = server
    pressed = []
    monkeypatch.setattr(dashboard, "restart_later", lambda: pressed.append("restart"))
    here = f"localhost:{srv.server_port}"
    own = {"Host": here, "Origin": f"http://{here}", "X-Server-Key": srv.RequestHandlerClass.server_key,
           "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    assert client.post("/server/restart", headers=own).json() == {"ok": True} and pressed == ["restart"]
    asked = client.post("/database/query", json={"sql": "SELECT 1 AS one"}, headers=own)
    assert asked.status_code == 200, asked.text
    assert client.post("/database/query", json={"sql": "SELECT 1 AS one"}, headers={**own, "Sec-Fetch-Site": "cross-site"}).status_code == 403


# What a proxy that adds no forwarding header (HAProxy, ssh -R, socat, funnel --tcp) may hand on as the Host: the
# address's name, allowed_hosts and a Tailscale name
NAMES = ("books.example.org", "library.example.net:443", "testpc.tail1234.ts.net")
LISTED = ["books.example.org", "library.example.net"]  # address's name and allowed_hosts


def seen_from(srv, address: str) -> None:
    """The server sees every request as sent from address: this PC's own home-network address, as a proxy here aimed at
    the server's home-network address connects, a proxy in another container, or another device at home."""
    base = srv.__dict__.setdefault("unseen", srv.RequestHandlerClass)  # the server's own, under any earlier seen_from

    def init(self, request, client_address, server):
        base.__init__(self, request, (address, client_address[1]), server)

    srv.RequestHandlerClass = type("SeenFrom", (base,), {"__init__": init})


def test_wherever_a_request_comes_from_and_whatever_it_calls_the_server_it_logs_in_and_has_its_users_role(library):
    """No address and no name makes a request anyone: this computer by localhost, its own address (a proxy here aimed at
    the home-network address), a phone at home, an address on another network (a router's port), a proxy handing on a
    public name or a forwarding header. Without a login: 401. A reader's login is a reader's there (the Server and
    Database pages: 403, the owner's), and an owner's has those pages, their posts too, from the owner's own pages."""
    access.add_user("rory", "correct horse", role="reader")
    access.add_user("owen", "correct horse", role="owner")
    srv, client = start(library, hosts=serve.Hosts(LISTED))
    key = srv.RequestHandlerClass.server_key
    try:
        for address in ("127.0.0.1", "192.168.1.5", "192.168.1.40", "198.51.100.7"):
            seen_from(srv, address)
            for host in ("localhost", "127.0.0.1:8770", "testpc", "192.168.1.5:8770", *NAMES):
                for extra in ({}, {"X-Forwarded-For": "203.0.113.9"}):
                    named = {"Host": host, **extra}
                    for path in ("/opds", "/catalog/index.html", "/server", "/database", "/api/session"):
                        assert client.get(path, headers=named).status_code == 401, (address, host, path)
                    session = client.get("/api/session", headers=named, auth=("rory", "correct horse")).json()
                    assert session["role"] == "reader" and not session["key"], (address, host)
                    for path in ("/server", "/database"):
                        assert client.get(path, headers=named, auth=("rory", "correct horse")).status_code == 403, (address, host)
                        assert client.get(path, headers=named, auth=("owen", "correct horse")).status_code == 200, (address, host)
                    own = {**named, "X-Server-Key": key, "Origin": f"http://{host}"}
                    asked = client.post("/database/query", json={"sql": "SELECT 1"}, headers=own, auth=("owen", "correct horse"))
                    assert asked.status_code == 200, (address, host)
                    elsewhere = {**own, "Origin": "http://evil.example"}
                    assert client.post("/database/query", json={"sql": "SELECT 1"}, headers=elsewhere,
                                       auth=("owen", "correct horse")).status_code == 403, (address, host)
    finally:
        srv.shutdown()


TUNNEL = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "testpc.tail1234.ts.net"}


def test_forwarding_headers_count_only_from_a_trusted_proxy(library):
    """trusted_proxies, this computer's own as they come: from them, the sender a header names (the lockout, the
    access log), the HTTPS flag (a Secure cookie) and the name the browser used (a form's Origin); from anyone else,
    nothing of these, and the address is noted, once, for the log and hylly doctor. Who may come in never hangs on
    them: every request logs in either way."""
    access.add_user("rory", "correct horse", role="reader")
    told = []
    srv, client = start(library, access_log=True, noted=lambda key, address, addresses: told.append((key, address, addresses)))
    login = {"username": "rory", "password": "correct horse", "next": "/"}
    try:
        trusted = client.post("/login", data=login, headers={**TUNNEL, "Origin": "https://testpc.tail1234.ts.net"})
        assert trusted.status_code == 303 and trusted.headers["set-cookie"].startswith("__Host-hylly=")
        assert "; Secure" in trusted.headers["set-cookie"]  # X-Forwarded-Proto from this computer's own proxy
        plain = client.post("/login", data=login, headers={**TUNNEL, "X-Forwarded-Proto": "http"})
        assert plain.headers["set-cookie"].startswith("hylly=") and "Secure" not in plain.headers["set-cookie"]
        nothing_said = client.post("/login", data=login, headers={"X-Forwarded-For": "203.0.113.9"})  # no proto: plain HTTP
        assert "Secure" not in nothing_said.headers["set-cookie"]
        assert told == []  # this computer is a trusted proxy as it comes: nothing to note
        seen_from(srv, "192.0.2.40")  # a device at home, sending the same headers
        refused = client.post("/login", data=login, headers={**TUNNEL, "Origin": "https://testpc.tail1234.ts.net"})
        assert refused.status_code == 403 and "set-cookie" not in refused.headers  # its X-Forwarded-Host counts for nothing
        mine = client.post("/login", data=login, headers={**TUNNEL, "Origin": f"http://127.0.0.1:{srv.server_port}"})
        assert mine.status_code == 303 and "Secure" not in mine.headers["set-cookie"]  # nor its X-Forwarded-Proto
        assert mine.headers["set-cookie"].startswith("hylly=")
        client.cookies.clear()  # a reading app's Basic login from here on
        for i in range(access.FAILS):  # a new made-up address at every try: all of them its own, so it is locked out
            client.get("/opds", auth=("rory", "guess"), headers={"X-Forwarded-For": f"203.0.113.{i}"})
        assert client.get("/opds", auth=("rory", "correct horse"), headers={"X-Forwarded-For": "198.51.100.20"}).status_code == 429
        assert told == [("headers_ignored_from", "192.0.2.40", ["192.0.2.40"])]  # noted once, however many times it sends them
        seen_from(srv, "127.0.0.1")  # through this computer's own proxy, each sender is its own: not locked out
        assert client.get("/opds", auth=("rory", "correct horse"), headers={"X-Forwarded-For": "198.51.100.20"}).status_code == 200
    finally:
        srv.shutdown()
    senders = {line.split(" ")[0] for line in access.access_log_file().read_text(encoding="utf-8").splitlines()}
    assert senders == {"203.0.113.9", "192.0.2.40", "198.51.100.20"}  # never a made-up one from the untrusted address


def test_a_forwarded_name_counts_only_when_it_is_one_of_the_servers_own(library):
    """A trusted proxy's X-Forwarded-Host stands for the name the browser used, for a form's Origin: only as the Host
    itself would, when it is one of the server's names (Hosts)."""
    access.add_user("rory", "correct horse", role="reader")
    srv, client = start(library)
    login = {"username": "rory", "password": "correct horse", "next": "/"}
    try:
        for name, status in (("evil.example", 403), ("testpc.evil.example", 403), ("testpc.tail1234.ts.net", 303)):
            sent = client.post("/login", data=login, headers={**TUNNEL, "X-Forwarded-Host": name, "Origin": f"https://{name}"})
            assert sent.status_code == status, name
    finally:
        srv.shutdown()


def test_a_tailscale_name_is_answered_only_in_the_addresss_own_tailnet():
    paths.settings_file().write_text('[server]\naddress = "https://lantern-pc.tail1234.ts.net/"\n', encoding="utf-8")
    settings.reload()  # as a server reads them at its start
    hosts = serve.Hosts.from_settings()
    assert hosts.allows("lantern-pc.tail1234.ts.net") and hosts.allows("testpc.tail1234.ts.net")  # its own, and this computer's
    assert not hosts.allows("testpc.tail9999.ts.net") and not hosts.allows("lantern-pc.tail9999.ts.net")  # another tailnet
    paths.settings_file().write_text('[server]\naddress = "https://books.example.org"\n', encoding="utf-8")
    settings.reload()
    assert serve.Hosts.from_settings().allows("testpc.tail9999.ts.net")  # no Tailscale address: this computer's in any
    assert not serve.Hosts.from_settings().allows("other.tail9999.ts.net")


def sent_by_hand_from(port: int, head: str) -> None:
    """A request written out whole, as httpx won't send it (a header in two lines, one folded): its answer is read."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"GET /opds HTTP/1.1\r\nHost: 127.0.0.1\r\n{head}Connection: close\r\n\r\n".encode("latin-1"))
        while s.recv(65536):
            pass


def test_a_forwarding_header_in_several_lines_or_with_a_forged_line_in_it_never_reaches_the_log(library):
    """A proxy that adds a line of its own: the sender's own line, first, doesn't pick the sender. An address with a
    scope id holding quotes and a folded line end: no sender at all, and nothing of it in access.log, where it would
    have started a line of its own."""
    for trust, head in (("X-Forwarded-For", "X-Forwarded-For: 203.0.113.77\r\nX-Forwarded-For: 198.51.100.25\r\n"),
                        ("X-Forwarded-For", 'X-Forwarded-For: fe80::1%x" 401 0 "-" "-"\r\n 203.0.113.99 - - '
                                            '[01/Jan/2026:00:00:00 +0000] "GET /forged HTTP/1.1" 200 0\r\n'),
                        ("Forwarded", 'Forwarded: for="[fe80::1%x\r\n 203.0.113.98 - - [01/Jan/2026] \\"GET /forged\\"]"\r\n'),
                        ("Forwarded", "Forwarded: for=203.0.113.77\r\nForwarded: for=198.51.100.26\r\n")):
        srv, _ = serve.make_server(library, "127.0.0.1", 0, access_log=True, proxy_header=trust)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            sent_by_hand_from(srv.server_port, head)
        finally:
            srv.shutdown()
            srv.server_close()
    lines = access.access_log_file().read_text(encoding="utf-8").splitlines()
    assert [line.split(" ")[0] for line in lines] == ["198.51.100.25", "127.0.0.1", "127.0.0.1", "198.51.100.26"]
    assert not any("forged" in line or "203.0.113.9" in line for line in lines)


def test_a_trusted_proxy_that_names_no_sender_is_noted_at_a_wrong_login(library):
    """Through a proxy that hands on no forwarding header, every visitor is the proxy's own address to the lockout, so a
    stranger's wrong passwords lock everyone out: a wrong login or a lockout from a trusted proxy's own address that no
    header named another sender for is noted, once, for the log and hylly doctor. Named by a header: nothing."""
    access.add_user("rory", "correct horse", role="reader")
    told = []
    srv, client = start(library, noted=lambda key, address, addresses: told.append((key, address, addresses)))
    try:
        assert client.get("/opds", auth=("rory", "guess"), headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401
        assert client.get("/opds", auth=("rory", "correct horse")).status_code == 200
        assert client.get("/opds").status_code == 401  # an app's first ask, before its login: no wrong login
        assert told == []
        assert client.post("/login", data={"username": "rory", "password": "guess", "next": "/"}).status_code == 401
        assert client.get("/opds", auth=("rory", "guess")).status_code == 401
        assert told == [("unnamed_by_proxy", "127.0.0.1", ["127.0.0.1"])]
    finally:
        srv.shutdown()


def test_every_lockout_through_a_trusted_proxy_is_noted_and_a_person_s_changes_a_minute_never(library):
    """A person past their changes a minute gets a 429 too (people.Pace), and they're logged in; but so is, as the access
    log has it, a name tried at the login form when it's a user's. So the server marks the pace's 429 itself: a wrong
    login or a lockout through 127.0.0.1, a trusted proxy that named no sender, is noted every time; the pace's never."""
    from hylly import people

    access.add_user("rory", "correct horse", role="reader")
    access.add_user("lucy", "correct horse", role="reader")
    told = []
    srv, client = start(library, noted=lambda key, address, addresses: told.append((key, address, addresses)))
    srv.RequestHandlerClass.people.pace = people.Pace(1)
    noted = srv.RequestHandlerClass.noted.counts
    try:
        assert client.post("/login", data={"username": "lucy", "password": "correct horse", "next": "/"}).status_code == 303
        key = {"X-Personal-Key": client.get("/api/session").json()["me"]["key"]}
        answers = [client.post("/api/me/place", json={"id": "h:novel", "at": n, "pct": 0.1}, headers=key).status_code for n in (1, 2, 3)]
        assert answers == [200, 429, 429] and noted == {} and told == []
        client.cookies.clear()
        tries = [client.post("/login", data={"username": "rory", "password": "guess", "next": "/"}).status_code
                 for _ in range(access.FAILS + 5)]
        assert tries.count(401) >= access.FAILS - 1 and tries[-5:] == [429] * 5  # locked out, by the form
        assert noted == {"unnamed_by_proxy": {"127.0.0.1": len(tries)}}  # each wrong login and each lockout
    finally:
        srv.shutdown()


def test_a_kept_alive_connection_that_ends_counts_its_last_request_once(library):
    """At a kept-alive connection's end the server reads no request, and the headers it still holds are the last
    request's: noted once for each request, not once more."""
    import time

    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen_from(srv, "192.0.2.40")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}") as client:  # one connection, kept alive
            for _ in range(2):
                client.get("/opds", headers={"X-Forwarded-For": "203.0.113.9"})
        time.sleep(0.5)  # the connection's end, read by the server
        assert srv.RequestHandlerClass.noted.counts == {"headers_ignored_from": {"192.0.2.40": 2}}
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_proxy_elsewhere_is_believed_once_listed_and_this_computer_then_only_if_listed_too(library):
    """A proxy in another container (172.18.0.0/16, say): without it in trusted_proxies, every visitor is one sender to
    the lockout, the proxy's; listed, each is its own. A list without loopback believes this computer no more."""
    import ipaddress

    access.add_user("rory", "correct horse", role="reader")
    proxies = (ipaddress.ip_network("172.18.0.0/16"),)
    srv, client = start(library, access_log=True, proxies=proxies)
    try:
        seen_from(srv, "172.18.0.3")
        for i in range(access.FAILS):  # a stranger guessing through the proxy is locked out ...
            client.get("/opds", auth=("rory", "guess"), headers={"X-Forwarded-For": "203.0.113.9"})
        assert client.get("/opds", auth=("rory", "correct horse"), headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
        ok = client.get("/opds", auth=("rory", "correct horse"), headers={"X-Forwarded-For": "198.51.100.20"})
        assert ok.status_code == 200  # ... and you, through the same proxy, are not
        seen_from(srv, "127.0.0.1")
        client.get("/opds", auth=("rory", "correct horse"), headers={"X-Forwarded-For": "198.51.100.30"})
    finally:
        srv.shutdown()
    senders = [line.split(" ")[0] for line in access.access_log_file().read_text(encoding="utf-8").splitlines()]
    assert set(senders) == {"203.0.113.9", "198.51.100.20", "127.0.0.1"}  # this computer's headers: not believed now


def test_the_connection_cap_spares_only_the_trusted_proxies(library, monkeypatch, log_in):
    """Connections at once from one address are capped, as who sent them only the headers would say, which come after:
    a trusted proxy's carry many senders, and are spared; this computer only while it is one of them."""
    import ipaddress

    monkeypatch.setattr(serve.Server, "PER_ADDRESS", 0)  # any connection over the cap
    for proxies, spared in ((access.LOOPBACK, True), ((ipaddress.ip_network("172.18.0.0/16"),), False)):
        srv, client = start(library, proxies=proxies)
        log_in(client)
        try:
            assert client.get("/ping").status_code == (200 if spared else 503), proxies
        finally:
            srv.shutdown()
            srv.server_close()


# ---- oversized requests and archives ----------------------------------------

def test_a_request_body_of_no_or_a_giant_length_isnt_waited_for(server):
    _, client, _ = server
    import socket

    port = int(str(client.base_url).rsplit(":", 1)[1].strip("/"))
    for length, status in (("-1", b" 400 "), (str(serve.MAX_BODY + 1), b" 413 ")):  # sent by hand: httpx won't send these
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(f"POST /api/tags HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: {length}\r\n\r\n".encode())
            assert status in s.recv(200), length
    assert serve.Handler.timeout and serve.Server.MAX_CONNECTIONS


def test_two_passwords_are_checked_at_a_time_and_a_third_waits_its_turn(monkeypatch):
    """Each check takes scrypt's memory (128 MB at Hylly's own cost), so a flood of logins waits rather than taking this
    computer's memory: three checks at once, the third begins only once one of the first two has ended."""
    stored, real = access.hash_password("correct horse"), hashlib.scrypt
    begun, finish, lock, now, most = threading.Semaphore(0), threading.Event(), threading.Lock(), [0], [0]

    def scrypt(*a, **k):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        begun.release()
        try:
            assert finish.wait(10)
            return real(*a, **k)
        finally:
            with lock:
                now[0] -= 1

    monkeypatch.setattr(access.hashlib, "scrypt", scrypt)
    said = []
    checks = [threading.Thread(target=lambda: said.append(access.check_password("correct horse", stored))) for _ in range(3)]
    for t in checks:
        t.start()
    try:
        assert begun.acquire(timeout=10) and begun.acquire(timeout=10)  # two at once
        assert not begun.acquire(timeout=0.5) and most[0] == 2  # and the third waits
    finally:
        finish.set()
        for t in checks:
            t.join(10)
    assert said == [True, True, True] and most[0] == 2 and begun.acquire(timeout=0)  # it had its turn


def test_a_request_with_a_transfer_encoding_is_refused_and_its_connection_closed(server):
    """A body in chunks isn't read, so it is refused, whatever it was sent to: on a connection kept open, its bytes would
    be read as the next request. One request to a connection, as Python's HTTP/1.0 has it, and so it stays."""
    srv, _, _ = server
    for request in (b"POST /api/tags HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n"
                    b"Content-Type: application/json\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
                    b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"):
        answer = sent_by_hand(srv.server_port, request)  # read until the server closes the connection
        assert answer.split(b"\r\n")[0].endswith(b" 400 Bad Request") and answer.endswith(b"send it with its Content-Length"), answer
    assert serve.Handler.protocol_version == "HTTP/1.0"


def sent_by_hand(port: int, data: bytes) -> bytes:
    """The whole answer to a request httpx wouldn't send, read until the server closes the connection."""
    import socket

    got = b""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(data)
        while chunk := s.recv(4096):
            got += chunk
    return got


def test_an_address_that_cant_be_read_is_refused_logged_and_leaves_no_traceback(library, monkeypatch, capsys, log_in):
    """Python refuses an address such as http://[x1]/ with an error quoting it: every new one used to put two tracebacks
    in server.log and a new entry in what the server remembers, and none reached the access log or the alarms."""
    import time

    srv, client = start(library, access_log=True)
    log_in(client)
    heard = []
    monkeypatch.setattr(srv.RequestHandlerClass.watch, "request", lambda *a, **k: heard.append(a[4]))
    known = set(serve.Handler.FAILED)
    try:
        for i in range(30):  # a new value every time, as a sender may make it
            answer = sent_by_hand(srv.server_port, b"GET http://[x%d]/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % i)
            assert answer.split(b"\r\n")[0].endswith(b" 400 Bad Request") and answer.endswith(b"bad address"), answer
        garbage = sent_by_hand(srv.server_port, b"GARBAGE\r\n\r\n")  # no headers at all: the sender is still known
        assert b" 400 " in garbage.split(b"\r\n")[0]
        assert client.get("/opds").status_code == 200  # and the server goes on as before
        for _ in range(50):
            if len(heard) == 32:
                break
            time.sleep(0.1)
    finally:
        srv.shutdown()
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and "A request failed" not in out and "x29" not in out + err
    assert serve.Handler.FAILED == known  # nothing new remembered
    assert heard == [400] * 31 + [200]  # the alarms see each one
    lines = access.access_log_file().read_text(encoding="utf-8").splitlines()
    assert sum('"GET http://[x' in line and '" 400 ' in line for line in lines) == 30
    assert any('"GARBAGE" 400 ' in line for line in lines) and lines[-1].startswith("127.0.0.1 ")


def test_an_origin_that_cant_be_read_is_refused_not_a_failure(server, capsys):
    srv, client, _ = server
    login = client.post("/login", data={"username": "ann", "password": "x"}, headers={"Origin": "http://[x1]"})
    assert login.status_code == 403
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    assert client.get("/database/examples", headers={**key, "Origin": "http://[x3]"}).status_code == 403
    assert client.get("/database/examples", headers={**key, "Origin": "http://127.0.0.1"}).status_code == 403  # another port's
    assert client.get("/database/examples", headers={**key, "Origin": f"http://127.0.0.1:{srv.server_port}"}).status_code == 200
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and "A request failed" not in out


def test_odd_keys_and_values_are_refused_never_a_failure(server, capsys):
    """A key that isn't ASCII is a wrong key, wherever a key is asked for; a list where a word goes, or a number too big
    for any float, is refused or taken for nothing, as another wrong value is: never a failure, with a traceback in the
    server's log."""
    srv, client, _ = server
    session = client.get("/api/session").json()
    personal, page = {"X-Personal-Key": session["me"]["key"]}, {"X-Server-Key": session["key"]}
    odd = "é".encode("utf-8")  # a header's bytes, as a sender may put any
    place = {"id": "h:novel", "at": 3, "pct": 0.1}
    for path, body, key in (("/api/tags", {"op": "add", "tag": "Knitting", "ids": ["h:novel"]}, "X-Server-Key"),
                            ("/server/decide", {"id": 1, "choice": 1}, "X-Server-Key"),
                            ("/database/query", {"sql": "SELECT 1"}, "X-Server-Key"), ("/api/me/place", place, "X-Personal-Key")):
        assert client.post(path, json=body, headers={key: odd}).status_code == 403, path
    assert not serve.same_key("\u00c3\u00a9", "key") and not serve.same_key(None, "key") and serve.same_key("key", "key")
    for path, body, status in (("/api/me/place", {**place, "file": ["x"]}, 200),  # a list for its file: no file of its
                               ("/api/me/place", {**place, "age": 10 ** 310}, 200),  # as long ago as a place may be
                               ("/api/me/mark", {"id": "h:novel", "mark": "finished", "age": 10 ** 310}, 200),
                               ("/api/me/bookmark", {"op": "add", "key": "k1", "why": "useful", **place, "age": 10 ** 310}, 200),
                               ("/api/me/look", {"look": "", "own": {"base": ["x"]}}, 409),
                               ("/api/reviews/check", {"op": "release", "id": "h:novel", "name": ["owner"]}, 400),
                               ("/api/reports/decide", {"id": "h:novel", "decision": ["fine"], "seen": 0}, 400),
                               ("/api/tags", {"op": "add", "tag": "Knitting", "ids": [5]}, 400),  # a number where an id goes
                               ("/api/fix", {"field": "title", "value": 5, "ids": ["h:novel"]}, 400)):
        answer = client.post(path, json=body, headers=personal if path.startswith("/api/me/") else page)
        assert answer.status_code == status and answer.json()["ok"] is (status == 200), (path, body)
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and "A request failed" not in err


def test_every_key_a_page_carries_is_checked_by_one_test():
    """The pages' keys were checked in several places, written out again in each, three of them comparing text, which
    fails on a key that isn't ASCII. Every guard that takes a key (an editor's, a person's own, the Server and Database
    pages') asks one test now (Handler._page_key_ok), which compares the header's bytes and where the request came from,
    and nothing else in the server compares a key."""
    import inspect

    guards = {g.check for table in (serve.Handler.GET, serve.Handler.POST) for row in table.values()
              for g in (row[0] if isinstance(row[0], tuple) else (row[0],)) if g.check}
    keyed = {check for check in guards if "self._page_key_ok(" in inspect.getsource(getattr(serve.Handler, check))}
    assert keyed == {"_personal", "_library_key", "_server_key"}
    assert inspect.getsource(serve).count("compare_digest(") == 1  # same_key's, which _page_key_ok asks
    assert "same_key(" in inspect.getsource(serve.Handler._page_key_ok)


def test_the_servers_checks_dont_hang_on_how_python_was_started():
    """python -O leaves every assert out: none of the server's checks of what a request sends is one."""
    import ast

    tree = ast.parse(Path(serve.__file__).read_text(encoding="utf-8"))
    assert [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)] == []


def test_a_failure_is_remembered_by_its_kind_and_place_never_by_what_was_sent(monkeypatch, capsys):
    monkeypatch.setattr(serve.Handler, "FAILED", set())

    def fail(text):
        raise ValueError(text)

    firsts = []
    for i in range(300):
        try:
            fail(f"sent {i}")
        except ValueError as e:
            firsts.append(serve.first_failure(e))
    assert firsts.count(True) == 1 and len(serve.Handler.FAILED) == 1
    assert serve.first_failure(SystemExit("settings.toml: one mistake")) and serve.first_failure(SystemExit("settings.toml: another"))
    for i in range(300):  # many places in the code: the set stops growing at its cap, and says so once
        try:
            exec(compile("raise KeyError(1)", f"place{i}.py", "exec"))
        except KeyError as e:
            serve.first_failure(e)
    assert len(serve.Handler.FAILED) == serve.Handler.FAILED_MAX
    assert capsys.readouterr().err.count("any more aren't logged until the server restarts") == 1  # the server's messages


def test_a_failures_traceback_keeps_its_frames_but_not_all_the_words_a_request_sent(server, monkeypatch, capsys):
    """A mistake in the code, met by a request: its traceback says where, once, but the error's words may be the
    sender's own, up to 64 KB, with line breaks that would start lines looking like the server's."""
    srv, client, _ = server
    monkeypatch.setattr(serve.Handler, "FAILED", set())
    sent = "x" * 70000 + "\r\n2026-11-12 12:00:00 Library server started FAKE "

    def opds(*a, **k):
        try:
            raise KeyError(sent)
        except KeyError as e:
            raise ValueError(sent) from e

    monkeypatch.setattr("hylly.opds.answer", opds)  # a stand-in of that name: the traceback names it
    assert client.get("/opds").status_code == 500
    out, err = capsys.readouterr()
    assert "Traceback (most recent call last):" in err and 'in opds\n    raise ValueError(sent) from e' in err  # the frames
    assert "direct cause of the following exception" in err and "KeyError: 'xxx" in err and "ValueError: xxx" in err
    assert len(err) < 5000 and "x" * 301 not in err and "\nLibrary server started" not in out + err
    assert not [line for line in err.splitlines() if line.startswith("2026-")]


def test_a_dropped_connection_logs_nothing_and_another_error_one_line(server, monkeypatch, capsys):
    srv, _, _ = server
    monkeypatch.setattr(serve.Handler, "FAILED", set())
    for error in (ConnectionResetError("gone"), BrokenPipeError("gone"), RuntimeError("what was sent"), RuntimeError("again")):
        try:
            raise error
        except Exception:  # noqa: BLE001 - as socketserver calls it
            srv.handle_error(None, ("192.0.2.7", 50000))
    out, err = capsys.readouterr()  # one line, the server's own message, and nothing for a dropped connection
    assert out == "" and err.count("\n") == 1 and "what was sent" not in err and "Traceback" not in err
    assert "A connection from 192.0.2.7 failed: RuntimeError in test_security.py, line" in err


def test_an_archive_cant_make_the_reader_take_memory_or_leave_its_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_PICTURE", 1000)
    comic = tmp_path / "bomb.cbz"
    with zipfile.ZipFile(comic, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("001.png", b"\0" * 5000)  # unpacks far beyond the limit
    with pytest.raises(reader.Unreadable):
        reader.page(comic, 0, 600)
    folder = tmp_path / "unpacked"
    folder.mkdir()
    with pytest.raises(reader.Unreadable):
        reader._inside(folder, "../outside.png")
    package = tmp_path / "book.zip"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("dir/..", b"x")
    with pytest.raises(reader.Unreadable):
        reader.inner(package, "dir/..")


# ---- ranges, feeds, HEAD, the Server header (live) ------------------------

def test_ranges_at_and_past_the_edges(server):
    _, client, root = server
    (root / "Notes").mkdir()
    (root / "Notes" / "ten.pdf").write_bytes(b"0123456789")
    assert client.get("/Notes/ten.pdf", headers={"Range": "bytes=5-2"}).status_code == 416
    past = client.get("/Notes/ten.pdf", headers={"Range": "bytes=8-100"})
    assert past.status_code == 206 and past.content == b"89" and past.headers["content-range"] == "bytes 8-9/10"
    whole = client.get("/Notes/ten.pdf", headers={"Range": "bytes=-50"})
    assert whole.status_code == 206 and whole.content == b"0123456789"


def test_a_quote_in_a_tag_doesnt_break_the_feed():
    entry = opds.book_entry({"id": "x", "t": 'Say "hi"', "g": ['Genre "One"'], "tg": ['a"b'], "f": [], "a": []})
    from xml.dom import minidom

    minidom.parseString(f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>')  # well-formed


def sparse(path: Path, size: int) -> bool:
    """A file of size bytes that holds next to nothing on the disk, its holes read as zeros; False on a disk that can't
    make one."""
    with open(path, "wb") as fh:
        if os.name == "nt":  # NTFS makes a file sparse only when asked
            import ctypes
            import msvcrt
            from ctypes import wintypes

            done = wintypes.DWORD()
            if not ctypes.windll.kernel32.DeviceIoControl(wintypes.HANDLE(msvcrt.get_osfhandle(fh.fileno())), 0x900C4,  # FSCTL_SET_SPARSE
                                                          None, 0, None, 0, ctypes.byref(done), None):
                return False
        fh.seek(size - 1)  # not truncate, which on Windows writes the zeros out
        fh.write(b"\0")
    return True


def test_head_and_the_server_header(server, monkeypatch):
    """A HEAD gets the headers alone, as a GET would have them: reading apps send one before each download, so the
    file isn't read for it, nor unpacked from its package, however large. The server is done with it at once:
    it closes the connection when its thread is free again, which a read of 16 GB to nowhere would take seconds to."""
    import socket
    import time

    _, client, root = server
    head = client.head("/catalog/index.html")
    assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) > 0
    assert client.get("/ping").headers["server"] == "Hylly"
    (root / "Notes").mkdir()
    big, inside = root / "Notes" / "Lantern Wolf Omnibus.pdf", "Moonwake Extras.epub"
    if not sparse(big, 16 << 30):
        pytest.skip("this disk makes no sparse files")
    with zipfile.ZipFile(root / "Notes" / "pack.zip", "w") as z:
        z.writestr(inside, b"x" * 300_000)
    ours, read = {big.name, "epub.js"}, []
    real_open, real_bytes, real_member = Path.open, Path.read_bytes, zipfile.ZipFile.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (self.name in ours and read.append(self.name), real_open(self, *a, **k))[1])
    monkeypatch.setattr(Path, "read_bytes", lambda self: (self.name in ours and read.append(self.name), real_bytes(self))[1])
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, *a, **k: (read.append(inside), real_member(self, *a, **k))[1])
    port, cookie = client.base_url.port, "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    began = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=60) as s:  # a reading app's, read to the server's end of it
        s.sendall(f"HEAD /Notes/{quote(big.name)} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\n\r\n".encode())
        answer = b""
        while chunk := s.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.0 200 ") and f"Content-Length: {16 << 30}\r\n".encode() in answer
    assert answer.endswith(b"\r\n\r\n") and time.monotonic() - began < 2
    whole = client.head("/Notes/" + quote(big.name))
    assert whole.status_code == 200 and whole.content == b"" and whole.headers["content-length"] == str(16 << 30)
    assert whole.headers["accept-ranges"] == "bytes"
    part = client.head("/Notes/" + quote(big.name), headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.headers["content-length"] == "10" and part.content == b""
    packed = client.head("/Notes/pack.zip/" + quote(inside), headers={"Range": "bytes=0-9"})  # a range is a GET's: the whole
    assert packed.status_code == 200 and packed.headers["content-length"] == "300000" and packed.content == b""
    program = client.head("/foliate/epub.js")
    assert program.status_code == 200 and program.headers["content-length"] == str((Path(serve.__file__).parent / "foliate" / "epub.js").stat().st_size)
    assert read == [], read
    assert client.get("/Notes/pack.zip/" + quote(inside)).content == b"x" * 300_000 and read == [inside]  # a GET reads it


def test_only_hyllys_own_files_are_packed_and_kept_packed(server):
    """A text file of the library goes as it is, in pieces, whatever its size: packing a file whole in memory, and
    keeping it so, is for Hylly's own, data.js and the page."""
    _, client, root = server
    (root / "Notes").mkdir()
    notes = root / "Notes" / "Lantern Wolf notes.txt"
    notes.write_text("Moonwake Press, an index of the tides.\n" * 60_000, encoding="utf-8")  # 2 MB of text, which packs well
    packed = serve._gzipped.cache_info().misses
    got = client.get("/Notes/" + quote(notes.name))  # httpx asks for gzip
    assert got.status_code == 200 and "content-encoding" not in got.headers and got.content == notes.read_bytes()
    assert got.headers["content-length"] == str(notes.stat().st_size) and serve._gzipped.cache_info().misses == packed
    data = client.get("/catalog/data.js")  # made by the server, and kept packed while it stays the same (page_script)
    assert data.headers["content-encoding"] == "gzip" and data.content == Catalog(root).page_script()[1]


# ---- logs, alarms, who may do what ------------------------------------

def test_a_typed_name_cant_forge_a_log_line(capsys):
    serve._stamp("10 wrong logins from 203.0.113.9 (last tried as 'x\nLibrary server started FAKE')")
    out = capsys.readouterr().err
    assert out.count("\n") == 1 and "?Library server started FAKE" in out
    assert bell.typed("x\r\nevil") == "'x??evil'" and len(bell.typed("y" * 500)) < 50


def test_a_typed_name_can_neither_turn_a_log_line_round_nor_hide_in_it_nor_run_on(capsys):
    """A name quoted in plain quotes it could close, the direction-turning U+202E and the zero widths once passed, and
    a wrong Basic login's name wasn't cut at all. One cleaner now (storage.shown): the marks are dropped, controls and
    U+0085 are ?, and a typed name is quoted as repr quotes it, at most 40 characters, in the server's log and the
    alarms alike."""
    serve._stamp("tried as \u202eevil\u200b\u2066 Ann\u0085Writer\u2029x")
    out = capsys.readouterr().err
    assert out == "tried as evil Ann?Writer?x\n"
    for name in ("x'\u202e) said: all is well. ('", "Ann Writer of Moonwake Press\n" * 3000):
        quoted = access.typed(name)
        assert quoted == bell.typed(name) and len(quoted) <= 42 and "\u202e" not in quoted and "\n" not in quoted
        assert quoted[0] == quoted[-1] and quoted[0] not in quoted[1:-1].replace("\\" + quoted[0], "")  # can't be closed


def test_the_lockout_line_quotes_a_users_name_and_says_only_the_length_of_anyone_elses(library):
    """The server's line at a lockout once named the name as typed, in plain quotes it could close; a user's name is
    quoted as typed() does, even one that came into the database some other way than hylly user add, and any other is
    only its length, however long it ran in a Basic login."""
    access.add_user("ann.writer", "correct horse")
    with access.store().write() as c:  # a name hylly user add would refuse
        c.execute("INSERT INTO account (name, role, hash, since) VALUES (?, ?, ?, ?)",
                  ("o'moonwake\u202e) said: all is well. (", "reader", access.hash_password("correct horse"), "2026-11-16"))
    said = []
    gate = access.Gate(log=said.append)
    for i in range(access.FAILS - 1):
        gate.check("203.0.113.9", {"Authorization": "Basic " + base64.b64encode(f"{'x' * 50_000}:guess".encode()).decode()})
    gate.log_in("203.0.113.9", "ann.writer", "guess")
    assert said == [f"{access.FAILS} wrong logins from 203.0.113.9 (last tried as 'ann.writer'): refused for 15 minutes."]
    for i in range(access.FAILS):
        gate.log_in("203.0.113.6", "o'moonwake\u202e) said: all is well. (", "guess")
    assert f"""{access.FAILS} wrong logins from 203.0.113.6 (last tried as "o'moonwake) said: all is well. ("): refused for """ \
           "15 minutes." in said and not [line for line in said if "\u202e" in line]
    for i in range(access.FAILS):  # another name: its own count, so no slowdown here
        gate.check("203.0.113.7", {"Authorization": "Basic " + base64.b64encode(f"{'y' * 40_000}:guess".encode()).decode()})
    assert f"{access.FAILS} wrong logins from 203.0.113.7 (last tried as an unknown name of 40,000 characters): refused for " \
           "15 minutes." in said and not [line for line in said if "y" * 41 in line]


def test_lockouts_from_many_addresses_are_one_alarm():
    watch = bell.Watch(log=lambda m: None)
    sent = []
    watch._send = lambda title, text, prio, public=None: sent.append(title)
    for i in range(20):
        watch.event("locked", ip=f"203.0.113.{i}", name="admin")
    assert sent == ["An address locked out"]


def test_the_format_to_keep_is_the_owners_and_the_keys_are_apart(library):
    for name, role in (("lucy", "librarian"), ("owen", "owner")):
        access.add_user(name, "correct horse", role=role)
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        with httpx.Client(base_url=base, auth=("lucy", "correct horse"), headers=HOME) as lucy:
            key = lucy.get("/api/session").json()["key"]
            fix = lucy.post("/api/fix", json={"field": "format", "value": "pdf", "ids": ["h:novel"]}, headers={"X-Server-Key": key})
            assert fix.status_code == 403 and "owner" in fix.json()["error"]
            assert key != srv.RequestHandlerClass.server_key  # what a librarian gets can't press the server page's buttons
    finally:
        srv.shutdown()


# ---- library files, slow senders, ranges, pictures and archives ------------------------------------------------------

def test_a_library_file_is_sandboxed_unless_its_type_is_known_harmless():
    for name in ("notes.rdf", "schema.xsd", "page.xhtml", "picture.svg", "odd.zzz", "feed.atom", "settings.config"):
        assert serve.library_file(name).get("headers", {}).get("Content-Security-Policy", "").startswith("sandbox;"), name
    for name in ("book.pdf", "book.epub", "comic.cbz", "comic.cbr", "cover.jpg", "page.png", "book.mobi", "notes.txt"):
        assert "headers" not in serve.library_file(name), name
    for name in ("code.js", "code.mjs", "code.cjs", "code.JS"):
        assert serve.library_file(name)["ctype"].startswith("text/plain"), name
    for name in ("code.js.gz", "code.js.bz2", "code.js.xz", "code.js.br", "code.js.Z", "picture.svgz", "page.html.gz"):
        # mimetypes looks past the packing suffix and calls code.js.gz JavaScript: a packed file goes as bytes
        sent = serve.library_file(name)
        assert sent["ctype"] == "application/octet-stream", name
        assert sent["headers"]["Content-Security-Policy"].startswith("sandbox;"), name


def test_the_feeds_go_in_a_sandbox_as_an_xml_file_of_the_library_does(server):
    """A browser shows a feed as an XML document, on the library's own address: in a sandbox, nothing in one could run as
    the library, whatever a value held. Reading apps take no notice of the header."""
    _, client, _ = server
    for path in ("/opds", "/opds/opensearch.xml", "/opds/search?q=novel", "/opds/new", "/ereader/opds", "/me/opds/series"):
        answer = client.get(path)
        assert answer.status_code == 200 and answer.headers["content-security-policy"] == serve.SANDBOX, path


def test_a_script_type_from_this_computers_own_list_goes_as_plain_text(monkeypatch):
    import mimetypes

    table = mimetypes.MimeTypes()  # as a Linux system's own list may have them (Ubuntu's says .es is JavaScript)
    for ctype, ext in (("text/javascript", ".es"), ("application/x-ecmascript", ".ecma"), ("text/jscript", ".jse"),
                       ("text/livescript", ".ls"), ("application/javascript", ".jsm")):
        table.add_type(ctype, ext)
    monkeypatch.setattr(serve.mimetypes, "guess_type", table.guess_type)
    for name in ("code.es", "code.ecma", "code.jse", "code.ls", "code.jsm"):
        assert serve.library_file(name) == {"ctype": "text/plain; charset=utf-8"}, name
    assert serve.library_file("code.es.gz")["ctype"] == "application/octet-stream"
    assert serve.library_file("book.epub")["ctype"] == "application/epub+zip"  # the rest as before


def test_a_library_file_asked_for_as_a_script_worker_or_transformation_is_refused(server):
    _, client, root = server
    folder = root / "Notes"
    folder.mkdir()
    for name in ("notes.js.gz", "code.js"):
        (folder / name).write_text("// nothing here\n", encoding="utf-8")
    (folder / "book.pdf").write_bytes(b"%PDF-1.4 nothing here")
    with zipfile.ZipFile(folder / "pack.zip", "w") as z:
        z.writestr("inner.js.gz", "// nothing here\n")
        z.writestr("page.xsl", "<x/>")
    files = ("/Notes/notes.js.gz", "/Notes/code.js", "/Notes/book.pdf", "/Notes/pack.zip/inner.js.gz", "/Notes/pack.zip/page.xsl")
    for dest in ("script", "worker", "sharedworker", "serviceworker", "audioworklet", "paintworklet", "xslt", "Script"):
        for path in files:
            for extra in ({}, {"Range": "bytes=0-3"}):
                r = client.get(path, headers={"Sec-Fetch-Dest": dest, "Sec-Fetch-Mode": "no-cors", **extra})
                assert (r.status_code, r.text) == (404, "not found"), (dest, path, extra)
    for path in files:  # as the reader, a download link or a reading app asks for them
        for dest in ({"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"}, {"Sec-Fetch-Dest": "document"}, {}):
            r = client.get(path, headers=dest)
            assert r.status_code == 200 and "javascript" not in r.headers["content-type"], (path, dest)
    for path in ("/Notes/notes.js.gz", "/Notes/pack.zip/inner.js.gz"):
        r = client.get(path)
        assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-security-policy"].startswith("sandbox;")
    view = next(p.name for p in (Path(serve.__file__).parent / "foliate").glob("*.js"))
    for path in ("/catalog/data.js", f"/foliate/{view}"):  # Hylly's own scripts still run
        r = client.get(path, headers={"Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript"), path


def test_slow_senders_cant_stop_the_server(library, monkeypatch, log_in):
    import socket
    import time

    monkeypatch.setattr(serve.Server, "MAX_CONNECTIONS", 4)
    monkeypatch.setattr(serve.Handler, "REQUEST_DEADLINE", 1.0)
    srv, client = start(library)
    log_in(client)
    try:
        slow = []
        for _ in range(4):  # every slot taken by a request that never finishes its headers
            s = socket.create_connection(("127.0.0.1", srv.server_port))
            s.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            slow.append(s)
        time.sleep(0.2)
        t0 = time.monotonic()
        busy = client.get("/ping", timeout=5)
        assert busy.status_code == 503 and time.monotonic() - t0 < 2  # answered at once, not left waiting
        time.sleep(1.5)  # past the deadline for the headers: the slow ones are closed
        assert client.get("/ping", timeout=5).status_code == 200
        for s in slow:
            s.close()
    finally:
        srv.shutdown()


def test_connections_turned_away_that_send_nothing_hold_up_no_one(library, monkeypatch, log_in):
    """Every slot taken, each connection over the limit gets its busy answer from a thread of their own, after a moment
    for its request to come: dozens that send nothing keep neither the next one from its answer nor the server from
    taking connections, as each used to hold the accept loop for that moment."""
    import socket
    import time

    monkeypatch.setattr(serve.Server, "MAX_CONNECTIONS", 1)
    srv, client = start(library)
    log_in(client)
    held, silent = socket.create_connection(("127.0.0.1", srv.server_port)), []
    try:
        held.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # the one slot, its request never finished
        time.sleep(0.2)
        silent = [socket.create_connection(("127.0.0.1", srv.server_port)) for _ in range(30)]
        t0 = time.monotonic()
        busy = client.get("/ping", timeout=20)
        assert busy.status_code == 503 and busy.headers["retry-after"] == "5" and time.monotonic() - t0 < 2
        silent[-1].settimeout(5)
        assert silent[-1].recv(100).startswith(b"HTTP/1.0 503 ")  # and a silent one gets its own, after its moment
    finally:
        for s in [held, *silent]:
            s.close()
        srv.shutdown()
        srv.server_close()


def test_the_busy_answers_start_again_should_they_end_and_end_with_the_server_even_with_their_hands_full(library, monkeypatch,
                                                                                                    log_in, capsys):
    """The thread that answers "busy" says why in server.log should a mistake end it, and the next connection turned
    away starts another; when the server closes it ends, even with every connection it can hold waiting, and leaves
    none open."""
    import socket
    import time

    monkeypatch.setattr(serve.Server, "MAX_CONNECTIONS", 1)
    monkeypatch.setattr(serve.Server, "BUSY_HELD", 1)  # one waited on, and one handed over
    srv, client = start(library)
    log_in(client)
    held, silent = socket.create_connection(("127.0.0.1", srv.server_port)), []
    try:
        held.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # the one slot, its request never finished
        time.sleep(0.2)
        real = srv._answer_busy

        def mistaken(request):
            srv._answer_busy = real  # once
            request.close()
            raise RuntimeError("a mistake in the code")
        srv._answer_busy = mistaken
        with pytest.raises(httpx.TransportError):
            client.get("/ping", timeout=5)
        ended = srv._turning_away
        ended.join(5)
        assert not ended.is_alive() and "Answering connections busy failed: RuntimeError" in capsys.readouterr().err
        assert client.get("/ping", timeout=5).status_code == 503 and srv._turning_away is not ended  # another, at once
        silent = [socket.create_connection(("127.0.0.1", srv.server_port)) for _ in range(3)]  # its hands full, and more
        time.sleep(0.05)
    finally:
        srv.shutdown()
        srv.server_close()
    helper = srv._turning_away
    helper.join(5)
    assert not helper.is_alive() and srv._turned_away.empty()
    for s in silent:  # each answered or closed, none left hanging
        s.settimeout(5)
        assert s.recv(100) in (b"", b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\n\r\n")
        s.close()
    held.close()


def test_a_huge_range_number_is_no_range_and_a_host_is_only_a_name(server):
    _, client, root = server
    (root / "Notes").mkdir()
    (root / "Notes" / "ten.pdf").write_bytes(b"0123456789")
    r = client.get("/Notes/ten.pdf", headers={"Range": "bytes=" + "9" * 5000 + "-"})
    assert r.status_code == 200 and r.content == b"0123456789"
    assert client.get("/ping", headers={"Host": "evil.example@127.0.0.1"}).status_code == 421


def test_pictures_are_capped_and_only_the_usual_kinds(tmp_path):
    from PIL import Image

    comic = tmp_path / "huge.cbz"
    buf = io.BytesIO()
    Image.new("1", (13000, 13000)).save(buf, "PNG")  # a few KB that would decode to gigabytes
    tiff = io.BytesIO()
    Image.new("RGB", (10, 10)).save(tiff, "TIFF")
    with zipfile.ZipFile(comic, "w") as z:
        z.writestr("001.png", buf.getvalue())
        z.writestr("002.png", tiff.getvalue())  # a TIFF named .png
    for n in (0, 1):
        with pytest.raises(reader.Unreadable):
            reader.page(comic, n, 600)
    assert not catalog.save_thumbnail(buf.getvalue(), tmp_path / "t.webp")
    packed = tmp_path / "bz.cbz"
    with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_BZIP2) as z:
        z.writestr("001.png", b"x" * 100)
    with zipfile.ZipFile(packed) as z, pytest.raises(reader.Unreadable):
        archives.read_member(z, "001.png", 1000)


def test_a_tall_first_page_makes_a_small_cover(tmp_path):
    import pymupdf

    from hylly import meta

    doc = pymupdf.open()
    doc.new_page(width=200, height=200_000)  # drawn 480 wide, this would be gigapixels
    path = tmp_path / "tall.pdf"
    doc.save(path)
    m = meta.read_pdf(path)
    from PIL import Image

    with Image.open(io.BytesIO(m.cover)) as im:
        assert im.width * im.height <= reader.MAX_PIXELS


def test_chm_isnt_a_book_here():
    from hylly import rules, settings

    r = rules.shipped()
    assert "chm" not in r.formats and ".chm" not in r.book_suffixes and "chm" not in MIME
    assert not any("chm" in order for order in settings.DEFAULTS["download"].values() if isinstance(order, list))


def test_a_solid_rar_is_unpacked_whole_only_when_it_is_plain(monkeypatch, tmp_path):
    class Info:
        def __init__(self, name, size, redir=None):
            self.filename, self.file_size, self.file_redir = name, size, redir

        def is_dir(self):
            return False

        def is_symlink(self):
            return False

    class Rar:
        infos = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def needs_password(self):
            return False

        def infolist(self):
            return self.infos

    monkeypatch.setattr(archives, "open_rar", lambda path: Rar())
    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path)
    room = [1 << 40]  # not what this machine has free: a small temporary disk would answer for the book
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("Usage", (), {"free": room[0]})())
    Rar.infos = [Info("001.jpg", 1000), Info("002.jpg", 1000), Info("ComicInfo.xml", 500)]
    assert reader._unpackable("x.cbr") == (["001.jpg", "002.jpg"], 2000)  # the pages, and what they take
    room[0] = (1 << 30) + 2499  # the disk keeps 1.1 GB free (2**30 bytes)
    assert reader._unpackable("x.cbr") is None
    room[0] = 1 << 40
    Rar.infos = [Info("001.jpg", 1000), Info("big.jpg", 1000, redir=(1, 0, "001.jpg"))]  # a reference to another file
    assert reader._unpackable("x.cbr") is None
    Rar.infos = [Info(f"{i:04}.jpg", reader.MAX_PICTURE) for i in range(40)]  # 2.7 GB all told
    assert reader._unpackable("x.cbr") is None
    Rar.infos = [Info("001.jpg", 1000), Info("extras.bin", reader.MAX_UNPACKED)]  # what isn't a page counts too
    assert reader._unpackable("x.cbr") is None
    for odd in ("Page\n2.jpg", "Page\r2.jpg", "Page\u20282.jpg", "Page *.jpg", "Page ?.jpg"):  # two names, or a pattern,
        Rar.infos = [Info("001.jpg", 1000), Info(odd, 1000)]  # in 7-Zip's list of names: read page by page instead
        assert reader._unpackable("x.cbr") is None, odd
    Rar.infos = [Info("001.jpg", 1000), Info("Read me?.txt", 10)]  # not a page, so not in the list
    assert reader._unpackable("x.cbr") == (["001.jpg"], 1000)


def test_what_7_zip_unpacked_is_kept_only_when_it_is_what_the_archive_listed(monkeypatch, tmp_path):
    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path / "pages")
    monkeypatch.setattr(reader, "_failed", type(reader._failed)())
    monkeypatch.setattr(reader, "_unpacking", set())
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("Usage", (), {"free": 1 << 40, "total": 1 << 41})())
    listed = ["001.jpg", "Chapter 2/002.jpg"]
    monkeypatch.setattr(reader, "_unpackable", lambda path: (list(listed), 2000))
    monkeypatch.setattr(archives, "sevenzip", lambda: "/opt/7zip/7zz")
    asked, written = [], {}

    def seven(args, **kw):  # a 7-Zip that writes what the test says, into the folder it's given
        asked.append(args)
        out = Path(next(a for a in args if a.startswith("-o"))[2:])
        for name, size in written.items():
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes(b"x" * size)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(reader.subprocess, "run", seven)
    pages = tmp_path / "pages"
    for n, (wrote, kept) in enumerate([({"001.jpg": 1000, "Chapter 2/002.jpg": 1000}, True),
                                       ({"001.jpg": 1000, "Chapter 2/002.jpg": 1000, "Chapter 2/003.jpg": 10}, False),
                                       ({"001.jpg": 1000, "Chapter 2/002.jpg": 1001}, False),
                                       ({"001.jpg": 1000}, False),  # a page left out, as 7-Zip's list trims spaces
                                       ({"001.jpg": 1000, "Chapter 2/02.jpg": 1000}, False)]):  # one not as listed
        written.clear()
        written.update(wrote)
        key = f"{n:020}"
        reader._unpack(f"-Moonwake {n}.cbr", pages / key, key)
        assert asked[-1][-2:] == ["--", f"-Moonwake {n}.cbr"]  # after --, the archive is a name whatever it begins with
        assert (pages / key / ".done").exists() == kept and (key in reader._failed) != kept, wrote
        assert not (pages / f"{key}.part").exists() and not (pages / f"{key}.part.list").exists()
    listed[:] = ["Page 1.jpg", "page 1.jpg"]  # two pages a disk that ignores case keeps as one
    written.clear()
    written["Page 1.jpg"] = 1000
    reader._unpack("Moonwake 9.cbr", pages / ("9" * 20), "9" * 20)
    assert not (pages / ("9" * 20) / ".done").exists() and "9" * 20 in reader._failed


def test_an_unpacking_cut_short_goes_as_the_cache_is_first_counted(monkeypatch, tmp_path):
    pages = tmp_path / "pages"
    monkeypatch.setattr(reader, "cache_dir", lambda: pages)
    monkeypatch.setattr(reader, "drawn_dir", lambda: tmp_path / "drawn")
    monkeypatch.setattr(reader, "_disk", {"tops": None, "books": {}})
    cut, going, book = "a" * 20, "b" * 20, "c" * 20
    (pages / f"{cut}.part" / "Chapter 1").mkdir(parents=True)  # the server stopped while it unpacked
    (pages / f"{cut}.part" / "Chapter 1" / "001.jpg").write_bytes(b"x" * 1000)
    (pages / f"{cut}.part.list").write_text("Chapter 1/001.jpg\n", encoding="utf-8")
    (pages / f"{going}.part").mkdir()  # one being unpacked now
    monkeypatch.setattr(reader, "_unpacking", {going})
    (pages / book).mkdir()
    (pages / book / "001.jpg").write_bytes(b"x" * 10)
    (pages / "my-scans.part").mkdir()  # not a name Hylly gives
    assert reader.disk_used() == 10
    assert sorted(p.name for p in pages.iterdir()) == [f"{going}.part", book, "my-scans.part"]


def test_an_unpacking_begun_as_the_cache_is_counted_stays(monkeypatch, tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    monkeypatch.setattr(reader, "_unpacking", set())
    begun = "d" * 20
    listing = Path.iterdir

    def iterdir(folder):  # an unpacking begins just as pages is listed: it's among those going on before its .part is made
        if folder == pages and begun not in reader._unpacking:
            reader._unpacking.add(begun)
            (pages / f"{begun}.part").mkdir()
        return listing(folder)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    reader._cut_short(pages)
    assert (pages / f"{begun}.part").is_dir()


def test_a_file_from_a_zip_package_is_unpacked_only_into_room_and_never_left_half_made(monkeypatch, tmp_path):
    cache = tmp_path / "pages"
    monkeypatch.setattr(reader, "cache_dir", lambda: cache)
    room = {"free": 1 << 40}
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("Usage", (), {"free": room["free"], "total": 1 << 41})())
    package = tmp_path / "Rule Pack.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Rule Pack/Rule Pack: Second Edition.pdf", b"%PDF-1.4 " + b"rules " * 5000)
        z.writestr("Rule Pack/Damaged.pdf", b"%PDF-1.4 " + b"rules " * 5000)
        z.writestr("Rule Pack/Locked.pdf", b"%PDF-1.4 locked")
        z.writestr("Rule Pack/notes.html", b"<p>Thanks for buying</p>")
    data = bytearray(package.read_bytes())
    with zipfile.ZipFile(package) as z:
        damaged, locked = z.getinfo("Rule Pack/Damaged.pdf"), z.getinfo("Rule Pack/Locked.pdf")
    at = damaged.header_offset  # Damaged.pdf's packed data begins with a block of no kind there is
    data[at + 30 + int.from_bytes(data[at + 26:at + 28], "little") + int.from_bytes(data[at + 28:at + 30], "little")] = 0xFF
    data[locked.header_offset + 6] |= 1  # locked by a password, as both of the ZIP's lists of what it holds say
    data[data.find(b"Rule Pack/Locked.pdf", data.find(b"PK\x01\x02")) - 46 + 8] |= 1
    package.write_bytes(bytes(data))
    room["free"] = 1 << 20  # less than the gigabyte the disk keeps free
    with pytest.raises(reader.Unreadable, match="no room"):
        reader.inner(package, "Rule Pack/Rule Pack: Second Edition.pdf")
    room["free"] = 1 << 40
    monkeypatch.setattr(reader, "cache_limit", lambda: 1000)  # more than the cache may hold
    with pytest.raises(reader.Unreadable, match="cache_gb"):
        reader.inner(package, "Rule Pack/Rule Pack: Second Edition.pdf")
    monkeypatch.setattr(reader, "cache_limit", lambda: 10 << 30)
    for member in ("Rule Pack/Damaged.pdf", "Rule Pack/Locked.pdf"):
        with pytest.raises(reader.Unreadable, match="couldn't be read"):
            reader.inner(package, member)
    assert not [p for p in cache.rglob("*") if p.is_file()]  # nothing half-made left behind
    kept = reader.inner(package, "Rule Pack/Rule Pack: Second Edition.pdf")  # a name a Mac allows, which Windows doesn't
    assert kept.name == "file.pdf" and kept.read_bytes().startswith(b"%PDF-1.4 rules")
    assert reader.inner(package, "Rule Pack/notes.html").name == "file"  # by a name of the cache's, never the ZIP's
