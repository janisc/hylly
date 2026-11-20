"""A borrower reads in the library page and gets nothing else: no file, however it's asked for, and no feed. E-books
reach a borrower as pages drawn on the server, like a PDF's, so the file itself never leaves."""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest

from hylly import access

from helpers import HOME, change, fix, items, write_book


@pytest.fixture
def users():
    return {"bea": "borrower", "rory": "reader"}  # the library fixture's


@pytest.fixture
def server(library, serving):
    srv, _ = serving(library, login=None)
    return srv, library


def client_for(srv, name):
    return httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", auth=(name, "correct horse"), headers=HOME)


def test_no_file_and_no_feed_however_asked(server):
    srv, root = server
    novel = items(root)["h:novel"]
    path = "/" + novel["f"][0][0]  # as the catalog quotes it
    bea, rory = client_for(srv, "bea"), client_for(srv, "rory")
    assert rory.get(path).status_code == 200  # a reader downloads it
    tries = [path, path + "?dl=1", path.upper(), path.replace("/", "%2F", 1), "/catalog/.." + path, "/catalog/%2E%2E" + path,
             "/opds", "/opds/search?q=novel", "/ereader/opds", "/ereader/opds/kind/book", "/catalog/cache/files.jsonl",
             "/" + quote("comics/Neon Comics/Neon Rangers 002.cbz")]
    for asked in tries:
        assert bea.get(asked).status_code == 403, asked
    assert bea.get(path, headers={"Range": "bytes=0-99"}).status_code == 403  # not even a part of it
    page = bea.get("/catalog/index.html")
    assert page.status_code == 200 and bea.get("/catalog/data.js").status_code == 200  # the library page itself
    cover = novel["c"]
    assert bea.get("/catalog/" + cover).status_code == 200 and bea.get("/catalog/help.html").status_code == 200


def test_a_borrower_reads_e_books_as_pages(server):
    srv, root = server
    from urllib.parse import unquote

    write_book(root / unquote(items(root)["h:novel"]["f"][0][0]))
    bea, rory = client_for(srv, "bea"), client_for(srv, "rory")
    session = bea.get("/api/session").json()
    assert (session["role"], session["files"], session["ereader"], session["edit"]) == ("borrower", False, None, False)
    assert rory.get("/api/session").json()["files"] is True
    pages = bea.get("/api/pages", params={"id": "h:novel"}).json()  # an EPUB: laid out on the server
    assert pages["ok"] and pages["pages"] >= 1
    first = bea.get("/api/page", params={"id": "h:novel", "n": 0, "w": 600})
    assert first.status_code == 200 and first.headers["content-type"] == "image/webp"
    assert rory.get("/api/pages", params={"id": "h:novel"}).status_code == 404  # a reader reads the EPUB itself, in the page


def test_the_page_strip_gets_pictures_of_a_books_pages(server):
    srv, root = server
    from urllib.parse import unquote

    from hylly import reader

    write_book(root / unquote(items(root)["h:novel"]["f"][0][0]))
    rory = client_for(srv, "rory")
    pages = rory.get("/api/pages", params={"id": "h:novel", "book": 1}).json()  # only when asked: a reader reads the EPUB itself
    assert pages["ok"] and pages["pages"] >= 1
    tile = rory.get("/api/page", params={"id": "h:novel", "n": 0, "w": 240, "book": 1})
    assert tile.status_code == 200 and tile.headers["content-type"] == "image/webp"
    assert rory.get("/api/page", params={"id": "no such title", "n": 0, "book": 1}).status_code == 404
    only_azw3 = {"f": [["a.azw3", "azw3", 1]]}  # a kind MuPDF doesn't lay out: the tiles stay lines
    assert reader.readable_book(only_azw3) is None and reader.readable_book({"f": [["a.pdf", "pdf", 1]]}) is None
    both = {"k": "book", "f": [["a.pdf", "pdf", 1], ["a.epub", "epub", 1]]}
    assert reader.readable_book(both) == "a.epub" and reader.readable(both) == "a.pdf"  # the page's reader keeps to its own


def test_a_borrower_changes_nothing(server):
    srv, _ = server
    bea = client_for(srv, "bea")
    assert change(bea, ["h:novel"]).status_code == 403
    assert fix(bea, ["h:novel"]).status_code == 403
    assert access.load_accounts()["bea"][0] == "borrower"
