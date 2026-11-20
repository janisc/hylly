"""Awkward file names, from the folder to a served link. A book whose name has # % + ; brackets, a dot at its start, an
emoji, right-to-left text or an accent in either Unicode form, or whose folder ends in a space, is listed by a build and
served byte for byte: by the link the library page gives it, as a download under its own name, by the reading apps'
feeds, and for a comic its pages counted for the reader. A file the server won't serve must be one hylly doctor names
(serve.refused_files). The same after a file is renamed to its accents apart between two builds, as a Mac-made disk or a
sync tool may give it back. A change to the quoting, serve.library_path or Handler._servable that breaks a family of
names fails here."""

from __future__ import annotations

import os
import re
import threading
import unicodedata
import zipfile
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import httpx
import pytest

from hylly import catalog, serve

from helpers import OPF, make_epub, png


NFC = lambda text: unicodedata.normalize("NFC", text)  # noqa: E731
NFD = lambda text: unicodedata.normalize("NFD", text)  # noqa: E731

NAMES = {  # what's awkward about each name: a book's path in the library
    "a hash": "ebooks/Lantern Wolf #2.epub",
    "a percent sign, and one before two hex digits": "ebooks/100% Moonwake %41%2F.epub",
    "a plus": "ebooks/Tide + Harbor.epub",
    "a semicolon": "ebooks/Ann Writer; Short Tales.epub",
    "square brackets": "comics/[Moonwake] Quillfern 001.cbz",
    "curly brackets": "comics/{Quillfern} Annual 002.cbz",
    "an ampersand, an equals sign and an apostrophe": "ebooks/Wolf & Lantern = Ann's Tide.epub",
    "a question mark": "ebooks/Who Keeps the Lantern?.epub",
    "a file starting with a dot": "ebooks/.Harbor Notes.epub",
    "a folder starting with a dot": "comics/.Quillfern Extras/Quillfern Extras 003.cbz",
    "an emoji": "ebooks/Lantern \U0001F4DA Wolf.epub",
    "right-to-left text": "ebooks/\u05e1\u05e4\u05e8 \u05d4\u05de\u05d2\u05d3\u05dc\u05d5\u05e8.epub",  # Hebrew
    "an accent, composed": NFC("ebooks/Café Lantern.epub"),
    "an accent, decomposed": NFD("comics/Crème Tide 004.cbz"),
    "a folder ending in a space": "ebooks/Ann Writer /The Harbor Light.epub",
}
NOT_HELD = {  # what this system can't hold, and why: skipped here, by the kind of name
    "a question mark": "Windows holds no ? in a name" if os.name == "nt" else "",
    "a folder ending in a space": "Windows drops a space at a name's end" if os.name == "nt" else "",
}
HELD = {kind: rel for kind, rel in NAMES.items() if not NOT_HELD.get(kind)}
EACH = [pytest.param(kind, id=kind, marks=pytest.mark.skipif(bool(NOT_HELD.get(kind)), reason=NOT_HELD.get(kind, "")))
        for kind in NAMES]
ACQUISITION = "http://opds-spec.org/acquisition"


def make(root: Path, rel: str, pages: int) -> Path:
    """A book of its own under that name: a comic of so many pages (their number keeps the comics apart, so that none is
    taken for another's copy), or an e-book titled as its file is named."""
    f = root.joinpath(*rel.split("/"))
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.suffix == ".cbz":
        with zipfile.ZipFile(f, "w") as z:
            for n in range(pages):
                z.writestr(f"{n:02}.png", png())
    else:
        opf = re.sub(r"\s*<dc:identifier[^>]*>[^<]*</dc:identifier>", "", OPF)  # no ISBN shared between them
        make_epub(f, opf.replace("The Long Dust", escape(f.stem)).replace("Marlow Ellery", "Ann Writer"), cover=False)
    return f


def library(root: Path) -> dict[str, Path]:
    return {kind: make(root, rel, pages) for pages, (kind, rel) in enumerate(HELD.items(), 1)}


def on_disk(root: Path, f: Path) -> str:
    """A file's path in the library as the disk spells its name, which may be in either Unicode form, whatever form it
    was made in: a build lists it so."""
    return f.parent.relative_to(root).as_posix() + "/" + next(n for n in os.listdir(f.parent) if NFC(n) == NFC(f.name))


def offered(client: httpx.Client, items: list[dict]) -> dict[str, str]:
    """Every file the tablets' feeds offer, source by source: its link as a reading app reads it from the feed's XML,
    by the path in the library it names."""
    links = {}
    for source in sorted({it["src"] for it in items}):
        feed = ET.fromstring(client.get(f"/opds/src/{quote(source, safe='')}").content)
        for link in feed.iter("{http://www.w3.org/2005/Atom}link"):
            if link.get("rel") == ACQUISITION:
                links[unquote(link.get("href")).lstrip("/")] = link.get("href")
    return links


def served(client: httpx.Client, srv, root: Path) -> tuple[dict[str, dict], list[str]]:
    """Every file the catalog lists, fetched as the page, the reader and the feeds fetch it: the titles by their files'
    paths, and what went wrong, by path. A file the server refuses is passed over when hylly doctor names it."""
    cat = srv.RequestHandlerClass.catalog
    items = cat.get()
    refused, links, wrong, listed = set(serve.refused_files(root, cat)), offered(client, items), [], {}
    for it in items:
        for link, _, _ in it.get("f", []):
            rel = unquote(link)
            listed[rel] = it
            if rel in refused:
                continue
            whole = root.joinpath(*rel.split("/")).read_bytes()
            read = client.get("/" + link)  # as the reader asks for it
            saved = client.get("/" + link, params={"dl": "1"})  # as the title's details link it
            fed = client.get(links[rel]) if rel in links else None  # as a reading app asks for it, by the feed's link
            if read.status_code != 200 or read.content != whole:
                wrong.append(f"{rel}: the page's link answered {read.status_code}")
            elif saved.status_code != 200 or saved.content != whole:
                wrong.append(f"{rel}: as a download, {saved.status_code}")
            elif unquote(saved.headers["content-disposition"].split("filename*=UTF-8''")[1]) != PurePosixPath(rel).name:
                wrong.append(f"{rel}: saved as {saved.headers['content-disposition']}")
            elif fed is None or fed.status_code != 200 or fed.content != whole:
                wrong.append(f"{rel}: by its feed's link, {fed.status_code if fed else 'not offered'}")
            elif rel.endswith(".cbz") and client.get("/api/pages", params={"id": it["id"]}).json().get("pages", 0) < 1:
                wrong.append(f"{rel}: the reader found no pages")
    return listed, wrong


@pytest.fixture
def start(log_in):
    started_here = []

    def started(root: Path) -> tuple[object, httpx.Client]:
        srv, _ = serve.make_server(root, "127.0.0.1", 0)
        # asked every 50 ms whether to stop, not every half second: it ends with its test, one of many
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        client = log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}"))
        started_here.append((srv, client))
        return srv, client

    yield started
    for srv, client in started_here:
        client.close()
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("kind", EACH)
def test_an_awkward_name_this_system_holds_is_listed_and_served_as_it_is(tmp_path, start, kind):
    made = make(tmp_path, NAMES[kind], 2)
    catalog.build(tmp_path, log=lambda s: None)
    srv, client = start(tmp_path)
    listed, wrong = served(client, srv, tmp_path)
    assert not wrong, wrong
    assert list(listed) == [on_disk(tmp_path, made)]  # listed, as the disk spells it
    assert serve.refused_files(tmp_path, srv.RequestHandlerClass.catalog) == []  # nor refused: an ordinary name here


def test_a_name_renamed_to_its_accents_apart_between_builds_is_the_same_title_and_still_served(tmp_path, start):
    """Among all the names this system holds, in one library: each served before and after."""
    made = library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    srv, client = start(tmp_path)
    composed = made["an accent, composed"]
    before, _ = served(client, srv, tmp_path)
    title = before[on_disk(tmp_path, composed)]
    apart = composed.rename(composed.with_name(NFD(composed.name)))
    catalog.build(tmp_path, log=lambda s: None)
    after, wrong = served(client, srv, tmp_path)
    assert not wrong, wrong
    assert after[on_disk(tmp_path, apart)]["id"] == title["id"] and len(after) == len(before)  # as the disk has it now
