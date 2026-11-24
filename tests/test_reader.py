"""The reader: a comic's pages one at a time, in reading order, scaled for the screen; nothing else from the file. And the
page's own reader, run under node where a browser isn't needed: its dialogs in full screen, Lift, fixed-layout books, its
keys and notes, a big EPUB's pieces and an AZW3's parts."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image
import os

from hylly import archives, catalog, matching, meta, paths, pdfworker, reader, serve
from hylly.library import Catalog, TitleIndex, moved_ids

from helpers import COLOURS, build_library, in_the_page, locked_epub, make_comic, make_pdf, picture


REAL_SEVENZIP = archives.sevenzip  # tests/conftest.py stands in for it, as none found: this computer's isn't asked


@pytest.fixture
def served(tmp_path, serving):
    build_library(tmp_path)
    make_comic(tmp_path / "comics" / "Moonwake" / "Moonwake 001.cbz")
    catalog.build(tmp_path, log=lambda s: None)
    _, client = serving(tmp_path)
    comic = next(it for it in Catalog(tmp_path).get() if "Moonwake" in (reader.readable(it) or ""))
    other = next(it for it in Catalog(tmp_path).get() if not reader.readable(it))
    return client, tmp_path, comic, other


def test_pages_in_reading_order(tmp_path):
    path = tmp_path / "c.cbz"
    make_comic(path)
    assert reader.count(path) == 4  # no hidden or Mac files
    seen = []
    for n in range(4):
        with Image.open(io.BytesIO(reader.page(path, n, 240)[0])) as im:
            seen.append(im.convert("RGB").getpixel((im.width // 2, im.height // 2)))
    wanted = [COLOURS["page1.jpg"], COLOURS["page2.jpg"], COLOURS["page10.png"], (255, 255, 0)]  # 2 before 10, then small.jpg
    assert all(abs(a - b) < 40 for got, want in zip(seen, wanted) for a, b in zip(got, want)), seen


def test_a_page_scaled_for_the_screen(served):
    client, _, comic, _ = served
    info = client.get("/api/pages", params={"id": comic["id"]}).json()
    assert info["ok"] and info["pages"] == 4 and info["v"]
    first = client.get("/api/page", params={"id": comic["id"], "n": 0, "w": 700, "v": info["v"]})
    assert first.status_code == 200 and first.headers["content-type"] == "image/webp"
    assert "immutable" in first.headers["cache-control"]
    with Image.open(io.BytesIO(first.content)) as im:
        assert im.width == 900 and im.height == 1350  # 700 asked: the next of the widths made, the shape kept
        assert im.convert("RGB").getpixel((450, 600))[0] > 150  # page1: red
    third = client.get("/api/page", params={"id": comic["id"], "n": 2, "w": 2000})
    with Image.open(io.BytesIO(third.content)) as im:
        assert im.width == 1800 and im.convert("RGB").getpixel((900, 900))[2] > 150  # page10, as it is: not made larger
    small = client.get("/api/page", params={"id": comic["id"], "n": 3, "w": 1200})
    assert small.headers["content-type"] == "image/webp"  # smaller than asked: not made larger, but made again here,
    with Image.open(io.BytesIO(small.content)) as im:  # so only pictures Hylly made leave the server, never a book's bytes
        assert im.width < 1200


def test_nothing_else_is_read(served):
    client, _, comic, other = served
    assert client.get("/api/page", params={"id": comic["id"], "n": 4}).status_code == 404  # past the last page
    assert client.get("/api/page", params={"id": comic["id"], "n": -1}).status_code == 404
    assert client.get("/api/page", params={"id": comic["id"], "n": "x"}).status_code == 404
    assert client.get("/api/pages", params={"id": other["id"]}).status_code == 404  # an EPUB: not yet
    assert client.get("/api/pages", params={"id": "../../state"}).status_code == 404
    assert client.get("/api/pages").status_code == 404


def test_pdf_pages_drawn_on_the_server(tmp_path):
    path = tmp_path / "rules.pdf"
    make_pdf(path, painted=True)
    assert reader.count(path) == 3
    data, ctype = reader.page(path, 2, 1100)
    assert ctype == "image/webp"
    with Image.open(io.BytesIO(data)) as im:
        assert im.width == 1200 and im.height == 1800 and im.convert("RGB").getpixel((600, 900))[2] > 200  # page 3: blue
    with pytest.raises(IndexError):
        reader.page(path, 3, 1200)
    locked = tmp_path / "locked.pdf"
    make_pdf(locked, password="secret", painted=True)
    with pytest.raises(reader.Unreadable, match="password"):
        reader.count(locked)


def test_a_page_is_made_at_webp_effort_2_not_pillows_slower_default():
    """A page's WebP once took Pillow's default effort (method 4), most of the time it took to draw a page: on a small
    home server 230-570 ms of it, where effort 2 takes 55-60% less for files 2-4% larger. A comic's page and a PDF's go
    through the same webp()."""
    noisy = Image.effect_noise((320, 480), 64).convert("RGB")  # a scan's grain: the efforts make different files of it

    def made(**how) -> bytes:
        out = io.BytesIO()
        noisy.save(out, "WEBP", quality=82, **how)
        return out.getvalue()

    assert pdfworker.webp(noisy) == made(method=2) != made()


def test_a_pdfs_outline_is_its_contents(tmp_path):
    import pymupdf as fitz

    path = tmp_path / "rules.pdf"
    make_pdf(path, painted=True)
    assert reader.outline(path) == []  # none in it
    doc = fitz.open(path)
    doc.set_toc([[1, "Basics", 1], [2, " Dice ", 2], [1, "Magic", 3]])
    doc.save(tmp_path / "with.pdf")
    doc.close()
    assert reader.outline(tmp_path / "with.pdf") == [[1, "Basics", 0], [2, "Dice", 1], [1, "Magic", 2]]
    comic = tmp_path / "c.cbz"
    with zipfile.ZipFile(comic, "w") as z:
        z.writestr("01.jpg", b"x")
    assert reader.outline(comic) == []


def test_the_kind_comes_from_the_file_not_its_name(tmp_path):
    named_cbr = tmp_path / "really a zip.cbr"
    make_comic(named_cbr)  # many .cbr files are ZIPs
    assert reader.count(named_cbr) == 4
    fake = tmp_path / "fake.cbz"
    fake.write_bytes(b"\x00" * 64)
    with pytest.raises(reader.Unreadable):
        reader.count(fake)
    assert reader.readable({"f": [["a.epub", "epub", 1], ["a.pdf", "pdf", 1], ["a.cbz", "cbz", 1]]}) == "a.cbz"
    assert reader.readable({"f": [["a.epub", "epub", 1]]}) is None


def test_a_file_inside_a_zip_when_the_title_has_none():
    packed = [["p.zip/big.pdf", "pdf", 900], ["p.zip/small.pdf", "pdf", 300], ["p.zip/a.cbz", "cbz", 500]]
    assert reader.readable({"f": [["p.zip", "zip", 1]], "z": packed}) == "p.zip/a.cbz"  # a comic's CBZ first, wherever it is
    assert reader.readable({"k": "book", "f": [["p.zip", "zip", 1]], "z": packed}) == "p.zip/small.pdf"  # a book's PDF; the smallest
    assert reader.readable({"k": "book", "f": [["own.pdf", "pdf", 1], ["p.zip", "zip", 1]], "z": packed}) == "own.pdf"  # its own first
    assert reader.readable({"f": [["p.zip", "zip", 1]], "zp": "p.zip"}) == "p.zip"  # a ZIP that is pictures
    both = [["a.cbz", "cbz", 1], ["a.pdf", "pdf", 1]]  # which comes first goes by the kind's text_first ([kinds.X])
    assert [reader.readable({"k": k, "f": both}) for k in ("book", "tech", "rpg", "comic", "manual")] == [
        "a.pdf", "a.cbz", "a.cbz", "a.cbz", "a.cbz"]
    assert reader.readable({"f": [["p.zip", "zip", 1]], "z": [["p.zip/a.epub", "epub", 1]]}) is None  # laid out in the page instead


def test_reading_what_a_zip_holds(tmp_path, monkeypatch, log_in):
    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path / "cache")
    build_library(tmp_path)
    packs = tmp_path / "collections" / "Packs"
    packs.mkdir(parents=True)
    make_pdf(tmp_path / "rules.pdf", painted=True)
    with zipfile.ZipFile(packs / "Rule Pack.zip", "w") as z:  # a PDF inside, as StoryBundle ships some books
        z.write(tmp_path / "rules.pdf", "Rule Pack/Rule Pack.pdf")
        z.writestr("Rule Pack/readme.txt", "Thanks for buying")
    (tmp_path / "rules.pdf").unlink()
    with zipfile.ZipFile(packs / "Sketchbook.zip", "w") as z:  # pages as pictures, not named .cbz
        for n in range(9):
            z.writestr(f"scan{n + 1}.jpg", picture((n * 20, 0, 0), size=(300, 450)))
        z.writestr("notes.txt", "Scanned")
    catalog.build(tmp_path, log=lambda s: None)
    items = Catalog(tmp_path).get()
    rules = next(it for it in items if (reader.readable(it) or "").endswith("/Rule%20Pack.pdf"))
    sketches = next(it for it in items if it.get("zp"))
    assert not rules.get("zp") and sketches["zp"].endswith("Sketchbook.zip")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            info = client.get("/api/pages", params={"id": rules["id"]}).json()
            assert info["ok"] and info["pages"] == 3
            third = client.get("/api/page", params={"id": rules["id"], "n": 2, "w": 600})
            with Image.open(io.BytesIO(third.content)) as im:
                assert im.convert("RGB").getpixel((300, 450))[2] > 200  # page 3: blue
            assert client.get("/api/pages", params={"id": sketches["id"]}).json()["pages"] == 9
            href = "/" + reader.readable(rules)
            whole = client.get(href).content  # unpacked as it's sent
            part = client.get(href, headers={"Range": "bytes=10-19"})  # a part: from the copy unpacked into the cache
            assert part.status_code == 206 and part.content == whole[10:20]
            assert part.headers["content-range"] == f"bytes 10-19/{len(whole)}"
            saved = client.get(href, params={"dl": 1}, headers={"Range": "bytes=10-19"})  # a download going on
            assert 'filename="Rule Pack.pdf"' in saved.headers["content-disposition"]  # by its name, not the cache's
    finally:
        srv.shutdown()
    kept = list((tmp_path / "cache").rglob("*.pdf"))
    assert [p.name for p in kept] == ["file.pdf"]  # unpacked once, for the pages and the part, by a name of the cache's


def test_the_cache_keeps_the_latest(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(reader, "cache_limit", lambda: 250)
    monkeypatch.setattr(reader, "disk_limit", lambda held: 250)  # whatever this computer's disk has free
    old, mid, new = ("a" * 20, "b" * 20, "c" * 20)  # named as Hylly names a book's folder
    for n, age in ((old, 300), (mid, 200), (new, 100), ("my-scanned-letters", 900)):
        (tmp_path / n).mkdir()
        (tmp_path / n / "p.jpg").write_bytes(b"x" * 100)
        os.utime(tmp_path / n, (time.time() - age, time.time() - age))
    reader._trim_cache(keep=tmp_path / old)  # the oldest goes, unless it's the one being read
    assert {p.name for p in tmp_path.iterdir()} == {old, new, "my-scanned-letters"}  # and one Hylly didn't make never does
    assert (tmp_path / "my-scanned-letters" / "p.jpg").exists()


def test_a_broken_archive(served):
    client, root, comic, _ = served
    (root / "comics" / "Moonwake" / "Moonwake 001.cbz").write_bytes(b"PK\x03\x04" + b"broken" * 20)  # a ZIP's start, then rubbish
    answer = client.get("/api/pages", params={"id": comic["id"]})
    assert answer.status_code == 500 and "couldn't be read" in answer.json()["error"]


@pytest.mark.skipif(not paths.WINDOWS, reason="a path that counts as a full one on Windows")
def test_7zip_by_its_full_path_on_windows(monkeypatch, tmp_path):
    from hylly import paths, settings

    monkeypatch.setattr(archives, "sevenzip", REAL_SEVENZIP)

    monkeypatch.setattr(paths, "WINDOWS", True)
    for name in ("7z", "7z.exe", "7-Zip\\7z.exe"):  # Windows would look in the server's own folder first
        paths.settings_file().write_text(f"[tools]\nsevenzip = '{name}'\n", encoding="utf-8")
        settings.reload()
        with pytest.raises(SystemExit, match=r"\[tools\] sevenzip = .*: 7-Zip by its full path, such as C:\\Program Files"):
            archives.sevenzip()
    seven = tmp_path / "7z.exe"
    seven.write_bytes(b"")
    paths.settings_file().write_text(f"[tools]\nsevenzip = '{seven}'\n", encoding="utf-8")
    settings.reload()
    assert archives.sevenzip() == str(seven)
    paths.settings_file().write_text(f"[tools]\nsevenzip = '{tmp_path / 'nowhere.exe'}'\n", encoding="utf-8")
    settings.reload()
    assert archives.sevenzip() == ""
    with pytest.raises(reader.Unreadable, match="needs 7-Zip on the computer the server runs on: install it from 7-zip.org"):
        archives.open_rar(str(tmp_path / "Moonwake 001.cbr"))


def test_7zip_on_the_path_elsewhere(monkeypatch):
    import shutil

    from hylly import paths, settings

    monkeypatch.setattr(archives, "sevenzip", REAL_SEVENZIP)
    monkeypatch.setattr(paths, "WINDOWS", False)
    monkeypatch.setitem(settings.DEFAULTS["tools"], "sevenzip", "")
    on_path = {"7zz": "/opt/7zip/7zz"}
    monkeypatch.setattr(shutil, "which", lambda name: on_path.get(name))
    assert archives.sevenzip() == "/opt/7zip/7zz"  # unset: 7z, else 7zz
    on_path["7z"] = "/usr/bin/7z"
    assert archives.sevenzip() == "/usr/bin/7z"
    paths.settings_file().write_text("[tools]\nsevenzip = '7zz'\n", encoding="utf-8")  # a name alone: on the PATH
    settings.reload()  # as the next command reads them
    assert archives.sevenzip() == "/opt/7zip/7zz"
    paths.settings_file().write_text("[tools]\nsevenzip = 'bin/7z'\n", encoding="utf-8")
    settings.reload()
    with pytest.raises(SystemExit, match=r"7-Zip by its full path, such as /usr/bin/7z, or by its name alone"):
        archives.sevenzip()
    paths.settings_file().write_text("", encoding="utf-8")
    settings.reload()
    on_path.clear()
    with pytest.raises(reader.Unreadable, match="install it, so that 7z or 7zz is on the PATH"):
        archives.open_rar("Moonwake 001.cbr")


def test_7zip_is_set_up_once_and_rarfile_has_no_other_tool_to_fall_back_on(monkeypatch, tmp_path):
    import rarfile

    for name in ("UNRAR_TOOL", "UNAR_TOOL", "BSDTAR_TOOL", "SEVENZIP_TOOL", "SEVENZIP2_TOOL", "CURRENT_SETUP", "tool_setup"):
        monkeypatch.setattr(rarfile, name, getattr(rarfile, name))  # put back as they were after the test
    real_setup, setups = rarfile.tool_setup, []
    monkeypatch.setattr(rarfile, "tool_setup", lambda **asked: setups.append(asked))
    monkeypatch.setattr(rarfile, "RarFile", lambda path: path)
    monkeypatch.setattr(archives, "_rar_tool", [None])
    seven = tmp_path / "tools" / "7z"
    monkeypatch.setattr(archives, "sevenzip", lambda: str(seven))
    for _ in range(3):  # a page each: 7-Zip isn't run to be checked for each
        archives.open_rar("Moonwake 001.cbr")
    assert len(setups) == 1 and setups[0]["force"] and not setups[0]["unrar"] and rarfile.SEVENZIP_TOOL == str(seven)
    other = tmp_path / "elsewhere" / "7zz"
    monkeypatch.setattr(archives, "sevenzip", lambda: str(other))  # the settings name another 7-Zip
    archives.open_rar("Moonwake 001.cbr")
    assert len(setups) == 2 and rarfile.SEVENZIP_TOOL == str(other)
    tried = []

    def run(cmd):
        tried.append(cmd[0])
        raise rarfile.RarCannotExec("not there")

    monkeypatch.setattr(rarfile, "custom_popen", run)
    monkeypatch.setattr(rarfile, "CURRENT_SETUP", None)  # as a page asked for meanwhile finds it: rarfile tries every tool
    with pytest.raises(rarfile.RarCannotExec):
        real_setup()
    assert len(tried) == 5 and str(other) in tried
    assert all(Path(tool).is_absolute() and not Path(tool).exists() for tool in tried)  # never a bare unrar on the PATH


def test_a_file_that_says_it_has_more_pages_than_any_book_isn_t_opened(tmp_path, monkeypatch):
    path = tmp_path / "rules.pdf"
    make_pdf(path, painted=True)
    monkeypatch.setattr(reader.pdfworker, "pages", lambda path, stamp: 2_500_000)  # a PDF says how many it has
    with pytest.raises(reader.Unreadable, match="2,500,000 pages"):
        reader.count(path)
    monkeypatch.setattr(reader.pdfworker, "pages", lambda path, stamp: reader.MAX_PAGES)
    assert reader.count(path) == reader.MAX_PAGES


def test_a_solid_rar_that_couldn_t_be_unpacked_is_read_at_once_and_tried_again_later(monkeypatch, tmp_path):
    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path / "pages")
    monkeypatch.setattr(reader, "_failed", type(reader._failed)())
    monkeypatch.setattr(reader, "_unpacking", set())
    monkeypatch.setattr(reader, "_names", lambda path, stamp, kind: tuple(f"{n:03}.jpg" for n in range(40)))
    monkeypatch.setattr(reader, "_solid", lambda path, stamp: True)
    monkeypatch.setattr(reader, "_rar_read", lambda path, name: picture((0, 0, 200), size=(300, 450)))
    tries = []
    monkeypatch.setattr(reader, "_unpackable", lambda path: tries.append(path))  # None: not to be unpacked, as on a full disk
    path, stamp = str(tmp_path / "Moonwake 003.cbr"), (1, 2)
    began = time.monotonic()
    for n in (25, 30, 35):  # deep in the archive, where a page waits a while for the unpacking
        assert reader._make_page(path, stamp, "rar", n, 240)[1] == "image/webp"
    assert time.monotonic() - began < 5 and len(tries) == 1  # from the archive at once, not after 20 seconds each
    assert not reader._being_unpacked(path, stamp)  # so drawing ahead draws its pages from the archive too
    for key in reader._failed:  # a while later
        reader._failed[key] -= reader.FAILED_FOR
    began = time.monotonic()
    reader._make_page(path, stamp, "rar", 36, 240)
    assert len(tries) == 2 and time.monotonic() - began < 5  # tried again, as the disk may have room now; failed again


def test_a_comic_in_a_folder_named_like_a_zip_is_read(tmp_path, log_in):
    build_library(tmp_path)
    make_comic(tmp_path / "comics" / "Unpacked.zip" / "Moonwake 002.cbz")
    catalog.build(tmp_path, log=lambda s: None)
    comic = next(it for it in Catalog(tmp_path).get() if "Unpacked.zip/Moonwake" in (reader.readable(it) or ""))
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            info = client.get("/api/pages", params={"id": comic["id"]}).json()
            assert info["ok"] and info["pages"] == 4
            assert client.get("/api/page", params={"id": comic["id"], "n": 3, "w": 240}).status_code == 200
    finally:
        srv.shutdown()
        srv.server_close()


ORDERED = {"Chapter 1/Page 9.jpg": (200, 0, 0), "chapter 1/PAGE 10.png": (0, 200, 0), "Chapter 2/Page 1.JPG": (0, 0, 200),
           "Chapter 2/Page 2.jpg": (200, 200, 0)}  # mixed case, numbers without zeros, a folder for each chapter
UNSHOWN = [".thumbs/Page 1.jpg", "Chapter 1/.Page 0.jpg", "__MACOSX/Chapter 1/._Page 9.jpg", "Chapter 1/notes.txt"]


def test_the_catalog_and_the_reader_count_the_same_pages_and_begin_at_the_same_one(tmp_path):
    comic = tmp_path / "Moonwake 004.cbz"
    with zipfile.ZipFile(comic, "w") as z:
        z.writestr(".thumbs/", b"")
        for name in reversed(UNSHOWN):  # the thumbnails' folder sorts first: the catalog took its picture for the cover
            z.writestr(name, picture((9, 9, 9), size=(100, 150)))
        for name, colour in reversed(ORDERED.items()):
            z.writestr(name, picture(colour, size=(300, 450), fmt="PNG" if name.endswith(".png") else "JPEG"))
    found = meta.read(comic)
    assert found.pages == reader.count(comic) == 4
    with zipfile.ZipFile(comic) as z:
        assert found.cover == z.read("Chapter 1/Page 9.jpg")
    with Image.open(io.BytesIO(reader.page(comic, 0, 240)[0])) as im:
        assert im.convert("RGB").getpixel((120, 180))[0] > 150  # Page 9 of chapter 1: red
    assert archives.comic_pages([*UNSHOWN, *reversed(ORDERED)]) == list(ORDERED)
    assert archives.comic_pages([n.replace("/", "\\") for n in (*UNSHOWN, *ORDERED)]) == [n.replace("/", "\\") for n in ORDERED]


def test_a_comic_whose_pages_are_all_in_a_hidden_folder_still_has_them(tmp_path):
    comic = tmp_path / "Lantern Wolf v01.cbz"
    with zipfile.ZipFile(comic, "w") as z:
        z.writestr(".Lantern Wolf v01/._001.jpg", b"what a Mac adds")
        z.writestr("__MACOSX/.Lantern Wolf v01/._002.jpg", b"what a Mac adds")
        for n, colour in ((10, (0, 0, 200)), (2, (0, 200, 0)), (1, (200, 0, 0))):
            z.writestr(f".Lantern Wolf v01/{n:03}.jpg", picture(colour, size=(300, 450)))
    found = meta.read(comic)
    assert found.pages == reader.count(comic) == 3  # the hidden pictures, as there's nothing else; never the Mac's
    with zipfile.ZipFile(comic) as z:
        assert found.cover == z.read(".Lantern Wolf v01/001.jpg")
    with Image.open(io.BytesIO(reader.page(comic, 2, 240)[0])) as im:
        assert im.convert("RGB").getpixel((120, 180))[2] > 150  # 010.jpg: blue
    assert archives.comic_pages([".thumbs/001.jpg", "001.jpg"]) == ["001.jpg"]  # beside a page that isn't hidden, left out
    assert archives.comic_pages(["../001.jpg", ".x/._001.jpg", "__MACOSX/001.jpg"]) == []


def test_pages_numbered_in_any_script_come_in_order_and_a_superscript_between_numbers_is_a_letter():
    """page2 before page10, in full-width and Arabic-Indic digits as in ASCII ones; a superscript or a circled digit
    between two numbers (page1²2) is no number, as re's \\d has it, and never stops the sort, which it once did."""
    pages = ["page10.jpg", "page１.jpg", "page٣.jpg", "page2.jpg", "page1²2.jpg", "page1①2.jpg", "²³.jpg"]
    assert archives.comic_pages(pages) == ["page１.jpg", "page1²2.jpg", "page1①2.jpg", "page2.jpg", "page٣.jpg",
                                           "page10.jpg", "²³.jpg"]


# ---- e-books that can't be opened ----------------------------------------------------------------------------

def test_the_reader_passes_over_a_file_that_can_t_be_opened():
    epub, mobi, pdf = ["t.epub", "epub", 1], ["t.mobi", "mobi", 1], ["t.pdf", "pdf", 1]
    item = {"k": "book", "f": [epub, mobi, pdf], "bad": {"t.epub": "locked"}}
    assert reader.readable_book(item) == "t.mobi"
    assert reader.readable(item) == "t.pdf"
    assert reader.readable({**item, "bad": {"t.pdf": "damaged"}}, books=True) == "t.epub"  # a borrower, who gets e-books as pages
    assert reader.readable_book({**item, "f": [epub]}) is None and reader.readable({**item, "f": [epub]}, books=True) is None


def test_a_locked_book_isn_t_drawn_but_can_still_be_downloaded(tmp_path, log_in):
    build_library(tmp_path)
    locked_epub(tmp_path / "collections" / "Poetry" / "Ann Writer - The Locked Tale.epub", title="The Locked Tale")
    catalog.build(tmp_path, log=lambda s: None)
    tale = next(i for i in Catalog(tmp_path).get() if i["t"] == "The Locked Tale")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            assert client.get("/api/pages", params={"id": tale["id"], "book": 1}).status_code == 404
            assert client.get("/" + tale["f"][0][0]).status_code == 200
    finally:
        srv.shutdown()
        srv.server_close()


# ---- the reader in the page, under node: what it does needs no browser here (the browsers' checks aren't in these tests yet) ----


# The page's body as far as its dialogs go: elements with an id, an order, children, a z-index and what a dialog asks of them
FAKE_PAGE = """
class N {
  constructor() { this.parentNode = null; }
  remove() { const p = this.parentNode; if (p) { p.kids.splice(p.kids.indexOf(this), 1); this.parentNode = null; } }
  replaceWith(n) { const p = this.parentNode; n.remove(); p.kids.splice(p.kids.indexOf(this), 1, n); n.parentNode = p; this.parentNode = null; }
}
class El extends N {
  constructor(id, { tag = "DIV", cls = [], z = 0, kids = [] } = {}) {
    super();
    Object.assign(this, { id, tagName: tag, z, inert: false, hidden: false, kids: [] });
    const c = new Set(cls);
    this.classList = { contains: x => c.has(x), add: x => c.add(x), remove: x => c.delete(x), toggle: (x, on) => { on ? c.add(x) : c.delete(x); } };
    for (const k of kids) this.append(k);
  }
  get children() { return this.kids.filter(k => k instanceof El); }
  get isConnected() { let e = this; while (e.parentNode) e = e.parentNode; return e === html; }
  append(n) { n.remove(); n.parentNode = this; this.kids.push(n); }
  contains(n) { for (let e = n; e; e = e.parentNode) if (e === this) return true; return false; }
  matches(sel) { return sel.split(",").map(s => s.trim()).some(s => s[0] === "#" ? s.slice(1) === this.id : s[0] === "." && this.classList.contains(s.slice(1))); }
  closest(sel) { for (let e = this; e instanceof El; e = e.parentNode) if (sel === "[inert]" ? e.inert : e.matches(sel)) return e; return null; }
  querySelectorAll(sel) { if (sel !== ":scope > .modal") throw new Error(sel); return this.children.filter(k => k.matches(".modal")); }
  focus() { document.activeElement = this; }
  compareDocumentPosition(other) { const all = everything(); return all.indexOf(other) > all.indexOf(this) ? 4 : 2; }
}
const everything = (e = html) => [e, ...e.children.flatMap(k => everything(k))];
const Node = { DOCUMENT_POSITION_FOLLOWING: 4 };
const getComputedStyle = el => ({ zIndex: String(el.z) });
const modal = (id, z) => new El(id, { cls: ["modal"], z });
const field = new El("listpickname", { tag: "INPUT" }), lists = new El("listpick", { cls: ["modal"], z: 45, kids: [field] });
const listsButton = new El("rlists", { tag: "BUTTON" }), tagger = modal("tagger", 25);
const reader = new El("reader", { z: 40, kids: [new El("rbar", { kids: [listsButton] }), new El("rbook")] });
const body = new El("body", { tag: "BODY", kids: [new El("layout"), new El("detail", { z: 10 }), reader, tagger, modal("repq", 20), lists,
  modal("shelfsheet", 20), new El("toast"), new El("", { tag: "SCRIPT" })] });
const html = new El("html", { tag: "HTML", kids: [body] });
const document = { documentElement: html, body, activeElement: body, addEventListener() {}, createComment: text => Object.assign(new N(), { text }) };
const $ = id => everything().find(e => e.id === id);
reader.hidden = true;
"""


def test_a_dialog_over_the_book_is_inside_the_reader_while_it_s_open_so_full_screen_shows_it():
    """Full screen draws only the reader and what's inside it: the lists, opened from the reader's bar, sat beside it
    and weren't drawn, and the reader went inert behind them, so the book stopped answering until full
    screen was left. Now such a dialog goes inside the reader while it's open, the reader's own parts are what lies
    behind it, and it's back in its own place after; one that opens behind the reader stays where it is."""
    script = FAKE_PAGE + """
/* part 1 */
const sibling = (el, by) => { const s = el.parentNode.children; return (s[s.indexOf(el) + by] || {}).id; };
const state = el => ({ in: el.parentNode.id, open: el.classList.contains("open"), between: [sibling(el, -1), sibling(el, 1)],
  inert: everything().filter(e => e.inert).map(e => e.id), held: html.classList.contains("held"), focus: document.activeElement.id });
const open = (el, on) => { on ? el.classList.add("open") : el.classList.remove("open"); dialogChanged(el, on); return state(el); };
const out = {};
reader.hidden = false; dialogChanged(reader, true);
listsButton.focus(); KEYS = true;  // the lists button, tapped
out.lists = open(lists, true);
out.closed = open(lists, false);
lists.classList.add("open"); field.focus(); dialogChanged(lists, true);  // what opened it put the focus on its field
out.field = state(lists);
reader.hidden = true; dialogChanged(reader, false);  // the book closed under it (the browser's Back)
out.bookGone = state(lists);
out.allClosed = open(lists, false);
reader.hidden = false; dialogChanged(reader, true);
out.behind = open(tagger, true);  // a slow answer's, behind the reader
out.behind.reader = state(reader).inert.includes("reader");
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const DIALOGS = \[\];", r"// A drag on a dialog's dimmed part"))
    assert out["lists"] == {"in": "reader", "open": True, "between": ["rbook", None], "inert": ["layout", "rbar", "rbook"],
                            "held": False, "focus": "listpick"}  # the reader itself not inert, nor the room for a scroll bar
    assert out["closed"] == {"in": "body", "open": False, "between": ["repq", "shelfsheet"], "inert": ["layout"],
                             "held": False, "focus": "rlists"}  # back where it was, and the focus where it came from
    assert out["field"]["in"] == "reader" and out["field"]["focus"] == "listpickname"  # moved, it keeps the focus it had
    del out["bookGone"]["focus"]
    assert out["bookGone"] == {"in": "body", "open": True, "between": ["repq", "shelfsheet"], "inert": ["layout"],
                               "held": True}  # over the library now, open and answering, as it was before full screen
    assert out["allClosed"]["inert"] == [] and not out["allClosed"]["held"]
    assert out["behind"]["in"] == "body" and "tagger" in out["behind"]["inert"] and not out["behind"]["reader"]
    assert out["behind"]["held"]  # the reader stays on top of it, as before


def test_a_lift_turn_taken_back_to_where_it_began_ends_and_the_book_turns_again():
    """A finger that started a Lift turn and came back to where it began had nowhere to go: the card's motion was asked
    for with offsets 0, .2, 0, which browsers refuse, and the turn never ended, so every turn after was refused. The
    motion now stays where it is, and the way back always ends the turn, even when a step of it fails; so does the way on."""
    script = """
let refuse = false;
const made = [], jumps = [];
const document = { documentElement: { dataset: {}, animate(keys, opts) {
  if (refuse && opts.pseudoElement !== "::view-transition") throw new TypeError("made up: refused");
  const offsets = keys.map(k => k.offset);
  if (offsets.some((o, i) => o < 0 || o > 1 || (i && o < offsets[i - 1]))) throw new TypeError("Offsets must be monotonically non-decreasing.");
  const a = { keys, opts, finished: Promise.resolve(), cancel() {} };
  made.push(a);
  return a;
} } };
const matchMedia = () => ({ matches: false }), R = { w: 400 };
const RD = { view: { renderer: { bookDir: "ltr" }, prev: () => jumps.push("prev"), next: () => jumps.push("next") } };
/* part 1 */
const motion = ([from, to]) => {
  made.length = 0;
  try { liftMotion({ step: 1, anims: [] }, from, to, 100, "linear"); } catch (e) { return String(e); }
  return made.map(a => a.keys.map(k => [+k.offset.toFixed(3), k.transform]));
};
(async () => {
  const out = { motions: [[0, 0], [1, 1], [0, 1], [.5, 0], [.1, 0]].map(motion), ends: [] };
  for (const [off, verdict, refused] of [[0, "back", false], [.3, "back", true], [.6, "go", true], [1, "go", false]]) {
    jumps.length = 0;
    let skipped = 0, thrown = null;
    const run = { step: 1, off, anims: [], verdict, v: 0, landed: true, jumped: Promise.resolve(), vt: { skipTransition: () => skipped++ } };
    refuse = refused;
    try { liftEnd(run); } catch (e) { thrown = String(e); }
    await new Promise(done => setTimeout(done, 20));
    refuse = false;
    out.ends.push({ thrown, skipped, jumps: [...jumps] });
  }
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = in_the_page(script, (r"const LIFT_MS = ", r"function liftOver\("))
    # the card at rest over the new page, which lies a little aside; and the card gone leftwards, the new page in place
    still, aside, gone = "translate3d(0%, 0, 0)", "translate3d(18%, 0, 0)", "translate3d(-100%, 0, 0)"
    assert out["motions"][0] == [[[0, still], [1, still]], [[0, aside], [1, aside]]]
    assert out["motions"][1] == [[[0, gone], [1, gone]], [[0, still], [1, still]]]
    assert [k[0] for k in out["motions"][2][0]] == [0, .2, 1]  # the card's look settles at .2, as ever
    assert [k[0] for k in out["motions"][3][0]] == [0, .6, 1] and [k[0] for k in out["motions"][4][0]] == [0, 1]
    back, refused_back, refused_on, on = out["ends"]
    assert back == {"thrown": None, "skipped": 1, "jumps": ["prev"]}  # the page back under the card, and the turn over
    assert refused_back == {"thrown": None, "skipped": 1, "jumps": ["prev"]}  # no motion, but the same end
    assert refused_on == {"thrown": None, "skipped": 1, "jumps": []}  # the new page at once
    assert on == {"thrown": None, "skipped": 0, "jumps": []}  # all the way already: it ends as its motion does


def test_the_readers_names_say_which_part_they_belong_to_lift_and_held_among_them():
    """lift(e) was a comic's pointer let go, two hundred lines below the Lift turn whose functions all start with lift;
    held(dx) a comic's drag at its ends, beside R.held and LOOK.held, a word held; RD.liftHeld the place a Lift turn
    waits with. Someone searching the page for lift or held landed in the wrong part. Now every name that starts with
    lift is the Lift turn's, in its part of the page; held is a word held; and the others say what they are: pointerUp,
    heldBack, RD.liftPlace (and the guide's pointer let go, guidePointerUp). They do as they did."""
    script = """
const RD = { n: 0, pages: 3 }, placed = [];
const bookPlace = p => placed.push(p);
/* part 1 */
/* part 2 */
const out = { drag: [[0, 40], [0, -40], [2, -40], [2, 40], [1, 40]].map(([n, dx]) => { RD.n = n; return heldBack(dx); }) };
RD.liftPlace = { place: { cfi: "epubcfi(/6/4!/4/2)" } };  // what the book said while a finger could still take the turn back
liftFlush();
out.flushed = [placed.splice(0), RD.liftPlace];
RD.liftPlace = {};  // nothing said meanwhile
liftFlush();
out.none = [placed.length, RD.liftPlace];
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const heldBack = ", r"function pinchStart\("), (r"function liftFlush\(", r"function liftOver\("))
    assert out["drag"] == [10, -40, -10, 40, 40]  # a quarter as far past the first page or the last, as before
    assert out["flushed"] == [[{"cfi": "epubcfi(/6/4!/4/2)"}], None] and out["none"] == [0, None]
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    lift, turn = page.index("// ---- Lift: "), page.index("\nfunction turn(step")
    for m in re.finditer(r"^(?:async )?(?:function|const|let) ((?:lift|Lift|LIFT)\w*)", page, re.M | re.I):
        assert lift < m.start() < turn, m[1]  # the Lift turn's own, from its heading to a page's turn, which isn't
    assert "liftHeld" not in page and "function lift(" not in page and "guideLift" not in page
    assert not re.search(r"^const held\b", page, re.M)
    assert '$("reader").addEventListener("pointerup", pointerUp);' in page and "placeStrip(heldBack(dx))" in page


def test_a_fixed_layout_book_takes_no_text_settings_and_opens():
    """foliate draws a fixed-layout EPUB or AZW3 with a renderer of its own that has no setStyles: the reader called it all
    the same, and such a book never opened ("This book couldn't be opened"). Its pages are drawn as the book has them;
    the paper around them still follows Aa, and a book that flows gets its styles as before."""
    script = """
const localStorage = { getItem: () => null, setItem() {} }, stored = (name, none) => none;  // nothing kept in this browser
const els = {}, $ = id => els[id] || (els[id] = { style: {}, hidden: false });
const setChrome = () => {}, pageColours = () => ["#fbfaf6", "#1c1b19", "#6b6862", "#2a5db0", false];
const R = { w: 1000, h: 800 }, RD = {}, location = { origin: "http://127.0.0.1" }, fetch = async () => ({ ok: false });
/* part 1 */
/* part 2 */
const renderer = (localName, more = {}) => ({ localName, clientWidth: 1000, clientHeight: 800, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, ...more });
let styles = null;
(async () => {
  const out = {};
  for (const [name, r] of [["fixed", renderer("foliate-fxl")], ["flowing", renderer("foliate-paginator", { setStyles: s => { styles = s; } })]]) {
    RD.view = { renderer: r };
    try { await bookStyle(); out[name] = { fit: RD.fit, paper: $("rbook").style.background }; } catch (e) { out[name] = String(e); }
  }
  out.styled = !!styles && styles.includes("font-size: calc(118% * var(--hylly-text, 1))");
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = in_the_page(script, (r"const SIZES = ", r"// What the book engine asks"), (r"async function bookStyle\(\)", r"new ResizeObserver"))
    assert out["fixed"] == {"fit": "own", "paper": "#fbfaf6"}
    assert out["flowing"]["paper"] == "#fbfaf6" and out["styled"]


def test_a_books_fixed_text_sizes_follow_the_step_and_the_main_text_comes_out_at_its_size():
    """A step is a size of the pages' root. A book that set its text in px, pt or a word ("small") stayed as it was at
    every step: 13 px on an iPad, whatever was chosen. Those become rem, in its rules (an @import's and a @media's too),
    its tags' own styles and <font size>, but not in a drawing. And the size most of the text has is taken for the main
    text's, and the root goes by it, so a step is the same size in every book: the characters are counted as the parts
    come, a part once and no further than it takes, and too few say nothing."""
    script = """
const RD = {}, NodeFilter = { SHOW_TEXT: 4 };
/* part 1 */
/* part 2 */
const out = {};
out.rem = Object.fromEntries(["13px", "10pt", "1pc", "0.5in", "2.54cm", "25.4mm", "101.6q", "small", "Medium", "xxx-large", ".5px",
  "1em", "90%", "1.2rem", "larger", "normal", "1.4", "inherit", "calc(1em + 2px)", "", "constructor"].map(v => [v, inRem(v)]));
// a style as a browser's: its properties and which are !important
const style = props => ({ props, important: new Set(), getPropertyValue(n) { return this.props[n] || ""; }, getPropertyPriority(n) { return this.important.has(n) ? "important" : ""; },
  setProperty(n, v, p) { this.props[n] = v; if (p) this.important.add(n); }, get fontSize() { return this.props["font-size"] || ""; }, set fontSize(v) { this.props["font-size"] = v; } });
const rule = props => ({ style: style(props) });
const strong = rule({ "font-size": "small" });
strong.style.important.add("font-size");
const inSvg = { closest: sel => sel === "svg" ? {} : null }, plain = { closest: () => null };
const sheets = [
  { ownerNode: plain, cssRules: [rule({ "font-size": "13px", "line-height": "20px" }), strong, rule({ color: "red" }), { cssRules: [rule({ "font-size": "12pt" })] },
    { styleSheet: { cssRules: [rule({ "line-height": "16px" })] } }, rule({ "font-size": "1.5em" })] },
  { ownerNode: inSvg, cssRules: [rule({ "font-size": "20px" })] },
  { ownerNode: plain, get cssRules() { throw new Error("SecurityError"); } } ];
const tag = (props, where = null, size = "") => ({ style: style(props), closest: () => where, getAttribute: () => size });
const tags = [tag({ "font-size": "11px" }), tag({ "font-size": "24px" }, {})], fonts = [tag({}, null, "2"), tag({}, null, "+1"), tag({}, null, "-1"), tag({}, null, "99"), tag({ "font-size": "2em" }, null, "7"), tag({}, null, "big")];
sizesInRem({ styleSheets: sheets, querySelectorAll: sel => sel.startsWith("font") ? fonts : tags });
const rules = sheets[0].cssRules;
out.rules = [rules[0].style.props, rules[1].style.props, [...rules[1].style.important], rules[2].style.props, rules[3].cssRules[0].style.props, rules[4].styleSheet.cssRules[0].style.props, rules[5].style.props];
out.svg = [sheets[1].cssRules[0].style.props, tags[1].style.props];
out.tags = tags[0].style.props;
out.fonts = fonts.map(f => f.style.fontSize);
out.main = [mainSize(new Map()), mainSize(new Map([[.7, 499]])), mainSize(new Map([[.7, 500]])), mainSize(new Map([[.7, 900], [1, 4000], [1.5, 800]]))];
// a part of a book: its texts, each with its size on a root of 20 px, and whether it is code
const part = texts => {
  const nodes = texts.map(([data, size, code]) => ({ data, parentElement: { size, closest: () => code ? {} : null } }));
  const root = { style: { set: {}, setProperty(n, v) { this.set[n] = v; } }, size: 20 };
  const frame = { style: { display: "none" } };
  const doc = { styleSheets: [], querySelectorAll: () => [], body: {}, documentElement: root, createTreeWalker: () => { let i = 0; return { nextNode: () => nodes[i++] || null }; },
    defaultView: { frameElement: frame, getComputedStyle: el => { if (frame.style.display === "none") throw new Error("hidden"); return { fontSize: `${el.size}px` }; } } };
  return { doc, root, frame };
};
const read = (...parts) => {
  RD.text = { scale: 1, seen: 0, sizes: new Map(), parts: new Set() };
  RD.view = { isFixedLayout: false };
  return parts.map(([n, p]) => { textSize(p.doc, n); return [p.root.style.set["--hylly-text"], p.frame.style.display]; });
};
const words = n => "x".repeat(n);
out.small = read([0, part([[words(900), 16], [words(90), 30], [words(5000), 10, true]])]);
out.title = read([0, part([[words(200), 40]])], [1, part([[words(4000), 25]])]);
out.print = read([0, part([[words(3000), 14]])], [0, part([[words(3000), 14]])], [1, part([[words(6000), 20]])], [2, part([[words(400), 14]])]);
out.seen = (read([0, part(Array.from({ length: 50 }, () => [words(1000), 20]))]), RD.text.seen);
out.settled = (read(...[0, 1, 2, 3, 4, 5, 6].map(n => [n, part(Array.from({ length: 12 }, () => [words(1000), 20]))])), [RD.text.seen, RD.text.parts.size]);
out.held = read([0, part([[words(900), 4]])], [1, part([[words(90000), 80]])]);
RD.view = { isFixedLayout: true };
const fixed = part([[words(900), 16]]);
textSize(fixed.doc, 9);
out.fixed = fixed.root.style.set;
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const SIZE_WORDS = ", r"// Books set their main text"), (r"const TEXT_LEAST = ", r"// A book's pages reach"))
    assert out["rem"] == {"13px": "0.8125rem", "10pt": "0.8333rem", "1pc": "1rem", "0.5in": "3rem", "2.54cm": "6rem", "25.4mm": "6rem",
                          "101.6q": "6rem", "small": "0.8125rem", "Medium": "1rem", "xxx-large": "3rem", ".5px": "0.0313rem",
                          "1em": "", "90%": "", "1.2rem": "", "larger": "", "normal": "", "1.4": "", "inherit": "", "calc(1em + 2px)": "",
                          "": "", "constructor": ""}
    assert out["rules"] == [{"font-size": "0.8125rem", "line-height": "1.25rem"}, {"font-size": "0.8125rem"}, ["font-size"], {"color": "red"},
                            {"font-size": "1rem"}, {"line-height": "1rem"}, {"font-size": "1.5em"}]
    assert out["svg"] == [{"font-size": "20px"}, {"font-size": "24px"}] and out["tags"] == {"font-size": "0.6875rem"}
    assert out["fonts"] == ["0.8125rem", "1.125rem", "0.8125rem", "3rem", "2em", ""]  # 7 the largest; its own style wins; "big" is none
    assert out["main"] == [0, 0, 0.7, 1]
    assert out["small"] == [["1.250", "none"]]  # 16 of 20; code and the few larger words don't count; the frame hidden again
    assert out["title"] == [["1.000", "none"], ["0.800", "none"]]  # a title page says nothing
    assert [scale for scale, _ in out["print"]] == ["1.429", "1.429", "1.000", "1.000"]  # small print, counted once, then the story
    assert out["seen"] == 12000 and out["settled"] == [60000, 5]
    assert [scale for scale, _ in out["held"]] == ["2.000", "0.500"]  # never more than twice, nor less than half
    assert out["fixed"] == {}


def test_the_steps_of_a_books_text_only_grow_and_the_ones_kept_in_a_browser_stay_what_they_were():
    """A browser keeps the step by its place in SIZES, so a new one goes at the end."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    sizes = json.loads(re.search(r"^const SIZES = (\[[^\]]*\])", page, re.M)[1])
    assert sizes[:7] == [85, 95, 106, 118, 132, 148, 166] and sizes == sorted(sizes) and len(sizes) == 9
    help_page = Path(serve.__file__).with_name("help.html").read_text(encoding="utf-8")
    assert "<b>Size</b> in nine steps" in help_page


def test_space_on_a_focused_button_presses_it_and_a_panels_keys_stay_in_the_panel():
    """The reader once took Space and the arrows for page turns wherever the focus was: Space on Contents, an Aa button
    or the end note's Library turned the page instead of pressing the button, and Down with Contents open turned the page
    behind the list. Space on a button is the browser's now, and so are the keys that move through a list in the
    reader's own panels; on the page itself they turn it as before, and so does Space on a link, which no browser presses."""
    script = """
const turned = [], jumped = [];
const GUIDE = { open: false }, R = { menu: false }, ME = null;
const els = {}, $ = id => els[id] || (els[id] = { hidden: id !== "reader" });
const document = { querySelector: () => null, addEventListener() {} };
const turn = n => turned.push(n), reelJump = n => jumped.push(n), reelTotal = () => 10;
/* part 1 */
// what has the focus: an element of this kind, in this panel or none
const on = (tag, panel = "") => ({ closest: sel => sel.split(",").map(s => s.trim())
  .some(s => s === tag || s === `${tag}[href]` || s === `.${panel}`) ? {} : null });
const press = (key, target, shiftKey = false) => {
  turned.length = jumped.length = 0;
  let stopped = false;
  readerKey({ key, target, shiftKey, preventDefault: () => { stopped = true; }, stopImmediatePropagation() {} });
  return [...turned, ...jumped.map(n => `to ${n}`), stopped ? "taken" : "left"];
};
process.stdout.write(JSON.stringify({
  page: [press(" ", on("body")), press(" ", on("body"), true), press("ArrowDown", on("body")), press("Home", on("body"))],
  button: [press(" ", on("button")), press(" ", on("button", "rwayback")), press(" ", on("input"))],
  link: press(" ", on("a")),
  barArrow: press("ArrowRight", on("button")),  // an arrow on a button of the bar still turns: there's no list there
  panels: ["rtocp", "raap", "rfind", "rlook", "rbmp"].map(p => [press("ArrowDown", on("button", p)), press("PageDown", on("li", p)),
    press("End", on("button", p))]),
  contentsOpen: ($("rtocp").hidden = false,  // the bar's Contents still has the focus
    [press("ArrowDown", on("button")), press(" ", on("body")), press("Home", on("body"))]),
}));
"""
    out = in_the_page(script, (r"function readerKey\(e\)", r'document\.addEventListener\("keydown", readerKey, true\);'))
    assert out["page"] == [[1, "taken"], [-1, "taken"], [1, "taken"], ["to 0", "taken"]]
    assert out["button"] == [["left"]] * 3  # pressed by the browser; the page stays
    assert out["link"] == [1, "taken"]
    assert out["barArrow"] == [1, "taken"]
    assert out["panels"] == [[["left"]] * 3] * 5
    assert out["contentsOpen"] == [["left"]] * 3  # Contents lies over the book: the page behind it stays


def test_without_a_login_a_note_over_the_page_goes_as_the_page_turns():
    """placeMoved once returned for a reader with no login before endWayBack, the only thing that hid the note at a turn:
    a book's end with Next in the series stayed over the page until the book was closed. The note goes with its page now
    (hideNote, first thing), logged in or not; only a login's way back is forgotten from what this browser keeps
    (forgetWayBack, apart from hiding since endWayBack's two jobs were split)."""
    script = """
const els = {}, $ = id => els[id] || (els[id] = { hidden: true });
let ME = null, cleared = 0;
const RD = { it: { id: "t1" }, book: false, placeMark: "3" }, R = {}, MY = new Map(), PLACE_WAIT = 5000;
const forgetWayBack = () => { cleared++; }, readFile = () => ["comics/Lantern Wolf.cbz", "cbz"], queue = () => {}, sendPlaces = () => {};
/* part 1 */
/* part 2 */
const out = {};
$("rwayback").hidden = false;  // the last page's note: The end, Next in the series
out.same = [placeMoved(3, .5), $("rwayback").hidden];  // the page settled where it was: no news, and the note stays
out.turned = [placeMoved(2, .4), $("rwayback").hidden, cleared];  // a page back: the note goes; nothing else to do
ME = { by: "ann" };
$("rwayback").hidden = false;  // a way back: Back to page 12
out.loggedIn = [placeMoved(3, .5), $("rwayback").hidden, cleared];  // the note goes, and the way back is forgotten
clearTimeout(R.placeTimer);
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const hideNote = ", r"function showWayBack\("), (r"function placeMoved\(", r"async function sendOnePlace\("))
    assert out["same"] == [False, False]
    assert out["turned"] == [True, True, 0]
    assert out["loggedIn"] == [True, True, 1]


# The reader's note at the foot of the page, run under node: its box and what's in it, each element a plain object
NOTE_PAGE = """
const els = {}, did = [];
const $ = id => els[id] || (els[id] = { hidden: true, textContent: "", innerHTML: "", label: "",
  setAttribute(k, v) { if (k === "aria-label") this.label = v; }, click() { if (this.onclick) this.onclick(); } });
const shown = id => $(id).hidden ? null : $(id).textContent;
const note = () => $("rwayback").hidden ? "hidden" : { text: $("rwaytext").textContent, button: shown("rwaybtn"), other: shown("rwayoff"),
  library: !$("rwaylib").hidden, next: $("rnext").hidden ? null : $("rnext").label, stars: !$("rrate").hidden };
const STORE = new Map(), localStorage = { getItem: k => STORE.has(k) ? STORE.get(k) : null, setItem: (k, v) => STORE.set(k, String(v)),
  removeItem: k => STORE.delete(k) };
const stored = (name, none) => { try { return JSON.parse(localStorage.getItem(name)) ?? none; } catch { return none; } };
const esc = s => s, coverHtml = it => `[${it.t}]`, linkNoteGone = () => {};
const readNext = it => did.push(`read ${it.t}`), closeReader = () => did.push("closed"), turnTo = n => did.push(`to page ${n + 1}`);
const markHere = (it, mark) => did.push(`${mark}: ${it.t}`), takeOff = id => did.push(`off the list: ${id}`);
let rate = true;
const starsWanted = () => rate, showRate = it => { $("rrate").hidden = false; };
const reasonOf = () => ({ sign: "*", word: "Bookmark" }), backFromBookmark = () => did.push("back from the bookmark");
let ME = { by: "ann" };
const MY = new Map(), ONLIST = new Set();
const one = { id: "t1", t: "Lantern Wolf 1" }, two = { id: "t2", t: "Lantern Wolf 2" }, listed = { id: "t3", t: "Moonwake" };
let onList = null, inSeries = two;
const nextOnList = () => onList, nextInSeries = () => inSeries, titleOfList = () => "your reading list";
const RD = { it: one, book: false, pages: 20, n: 19 };
"""
NOTE_PARTS = ((r"function showNote\(", r"// ---- Finished\."), (r"function finishedHere\(", r"// What to read next:"),
              (r"function askFinished\(", r"function markHere\("), (r"const placeName = ", r"function visitBookmark\("))


def test_each_of_the_reader_s_notes_shows_its_own_words_and_buttons_and_does_its_own_thing():
    """One box at the foot of the reader says five things: a title marked as finished at its last page (Not yet, the
    next in the series or on the list, Take off the reading list), The end of one already finished, Finished? where a
    book's story seems to end, a place moved on another device with the way back, and a bookmark visited with the way
    back. Each shows what it shows and nothing left over from the one before; its button does its own thing, and the
    note goes."""
    script = NOTE_PAGE + """
/* part 1 */
/* part 2 */
/* part 3 */
/* part 4 */
const out = {};
finishedHere();
out.finished = [note(), did.splice(0)];
$("rwaybtn").click();  // Not yet
out.notYet = [note(), did.splice(0)];
MY.set(one.id, { done: true });
finishedHere();
out.theEnd = [note(), did.splice(0)];
$("rnext").click();
out.next = did.splice(0);
inSeries = null; rate = false; $("rwayback").hidden = true;
finishedHere();
out.nothingNext = note();  // nothing to say: no note
MY.delete(one.id); ONLIST.add(one.id); onList = listed; RD.list = "reading"; rate = true;
finishedHere();
out.onList = [note(), did.splice(0)];
$("rwayoff").click();  // Take off the reading list
out.tookOff = [note(), did.splice(0)];
ONLIST.clear(); onList = null; RD.list = null; inSeries = two;
askFinished();
out.asked = note();
$("rwaybtn").click();  // Mark as finished: as at the last page
out.askedYes = [note(), did.splice(0)];
STORE.set("hylly.back.t1", JSON.stringify({ at: 4, pct: 0, when: 1795070000, to: 11, toPct: 0 }));
showWayBack();
out.wayBack = note();
$("rwaybtn").click();  // Back to page 5
out.wentBack = [note(), did.splice(0), STORE.has("hylly.back.t1")];
finishedHere(); did.length = 0;
showWayBack();
out.noWayBack = note();  // none kept: the note goes
RD.from = { at: 2, pct: 0 };
bookmarkNote({ why: "remember" });
out.bookmark = note();
$("rwaybtn").click();
out.bookmarkBack = [note(), did.splice(0)];
finishedHere(); did.length = 0; RD.from = null;
bookmarkNote({ why: "remember" });
out.bookmarkNoWay = note();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, *NOTE_PARTS)
    series = "Next in the series: Lantern Wolf 2"
    finished = {"text": "Marked as finished", "button": "Not yet", "other": None, "library": True, "next": series, "stars": True}
    assert out["finished"] == [finished, ["finished: Lantern Wolf 1"]]
    assert out["notYet"] == ["hidden", ["unfinished: Lantern Wolf 1"]]
    assert out["theEnd"] == [{"text": "The end", "button": None, "other": None, "library": True, "next": series, "stars": True}, []]
    assert out["next"] == ["read Lantern Wolf 2"]
    assert out["nothingNext"] == "hidden"
    assert out["onList"] == [{**finished, "other": "Take off the reading list", "next": "Next on your reading list: Moonwake"},
                             ["finished: Lantern Wolf 1"]]
    assert out["tookOff"] == [{**finished, "next": "Next on your reading list: Moonwake"}, ["off the list: t1"]]
    assert out["asked"] == {"text": "Finished?", "button": "Mark as finished", "other": None, "library": True, "next": None, "stars": True}
    assert out["askedYes"] == [finished, ["finished: Lantern Wolf 1"]]
    went = out["wayBack"]
    assert went.pop("text").startswith("Went to page 12 (read ") and went == {"button": "Back to page 5", "other": None, "library": False,
                                                                              "next": None, "stars": False}
    assert out["wentBack"] == ["hidden", ["to page 5"], False]  # going back is the latest place: the way back is forgotten
    assert out["noWayBack"] == "hidden"
    assert out["bookmark"] == {"text": "* Bookmark", "button": "Back to page 3", "other": None, "library": False, "next": None, "stars": False}
    assert out["bookmarkBack"] == ["hidden", ["back from the bookmark"]]
    assert out["bookmarkNoWay"] == "hidden"


def test_closing_a_book_lets_a_big_epubs_pieces_go():
    """RD.loader, with a big EPUB's pieces (up to 128 MB), was once let go only when the next title opened: after the book
    was closed the library carried them all."""
    script = """
const noop = () => {};
const els = {}, $ = id => els[id] || (els[id] = { hidden: false, dataset: { for: "x" }, querySelectorAll: () => [], replaceChildren: noop });
const RD = { seq: 1, abort: { abort: noop }, view: null, made: [], first: null, loader: { pieces: "128 MB of them" } };
const R = { pointers: new Map() }, LIFT = null, slots = () => [];
for (const name of ["closeGuide", "sendPlaces", "placesKnown", "hideNote", "linkNoteGone", "loadingEnd", "closeToc", "showBar", "endFind",
  "unzoom", "aheadStop", "load"]) globalThis[name] = noop;
/* part 1 */
leaveBook();
process.stdout.write(JSON.stringify({ loader: RD.loader }));
"""
    assert in_the_page(script, (r"function leaveBook\(\)", r"function showBar\(on\)")) == {"loader": None}


def test_a_book_closed_in_the_middle_of_a_jump_leaves_foliate_nothing_to_do_after():
    """Closing a book closes foliate's view, and so its renderer, whose destroy let go of the renderer itself, which its
    size watcher never watched: a jump still loading its chapter as the book closed left the watcher a page whose frame
    was gone, and laying that out threw in columnize. A chapter that came in the very frame the book closed in threw
    too, reading its page's background a frame on (setStyles), and so did one a still-loading jump had made, which had
    no document. destroy lets go of all its watcher watches now, and setStyles' callbacks look for a page with a
    document first (foliate/README.md); the browser run shows the console clean."""
    script = """
const noop = () => {};
const els = {}, $ = id => els[id] || (els[id] = { hidden: false, dataset: { for: "x" }, querySelectorAll: () => [], replaceChildren: noop });
let closed = 0, destroyed = 0;
const RD = { seq: 1, abort: { abort: noop }, view: { book: { destroy() { destroyed++; } }, close() { closed++; } }, made: [], first: null, loader: null };
const R = { pointers: new Map() }, LIFT = null, slots = () => [];
for (const name of ["closeGuide", "sendPlaces", "placesKnown", "hideNote", "linkNoteGone", "loadingEnd", "closeToc", "showBar", "endFind",
  "unzoom", "aheadStop", "load"]) globalThis[name] = noop;
/* part 1 */
leaveBook();
process.stdout.write(JSON.stringify({ closed, destroyed, view: RD.view }));
"""
    assert in_the_page(script, (r"function leaveBook\(\)", r"function showBar\(on\)")) == {"closed": 1, "destroyed": 1, "view": None}
    paginator = (Path(serve.__file__).with_name("foliate") / "paginator.js").read_text(encoding="utf-8")
    renderer = paginator[paginator.index("export class Paginator"):]
    assert "    destroy() {\n        this.#observer.disconnect()\n" in renderer and "this.#observer.unobserve(this)" not in renderer
    styles = renderer[renderer.index("    setStyles(styles) {"):renderer.index("    focusView() {")]
    assert "requestAnimationFrame(() => this.#view?.document &&" in styles and ".then(() => this.#view?.document && this.#view.expand())" in styles


def test_the_last_finger_off_a_book_lets_a_lift_go_and_an_end_of_picked_words_in_one_listener():
    """bookDoc listened for touchend (and touchcancel) twice in a row on the book's window, with the same options: one
    let a Lift go, the other ended a drag of an end of the words picked. One listener does both now, in that order."""
    script = """
const on = [], R = { handle: false }, RD = { upright: false }, HOLD_PX = 12, HOLD_MS = 450, lets = [];
const listen = where => (kind, f, opts) => on.push({ where, kind, f, capture: !!(opts && opts.capture) });
const win = { addEventListener: listen("win"), getComputedStyle: () => ({ writingMode: "horizontal-tb" }) };
const doc = { addEventListener: listen("doc"), defaultView: win, body: {} };
for (const name of ["movesFrame", "swipeTurns", "bookStyle", "wordAt", "lookSelection", "hideLook", "onPickedEnd", "showBookmarkPick",
  "liftFinger", "frameOnScreen", "tapAt", "readerKey", "pressedWith", "menuClick"]) globalThis[name] = () => {};
const fixedLayout = () => false, lifts = () => true, liftLetGo = cancel => lets.push(cancel);
/* part 1 */
bookDoc(doc);
const ends = kind => on.filter(l => l.where === "win" && l.kind === kind);
const out = { listeners: ["touchend", "touchcancel"].map(k => [k, ends(k).length, ends(k).every(l => l.capture)]) };
const lift = (kind, touches, handle) => {
  R.handle = handle; lets.length = 0;
  let stopped = 0;
  ends(kind).forEach(l => l.f({ touches, stopPropagation: () => stopped++ }));
  return [[...lets], stopped, R.handle];
};
out.lastOff = lift("touchend", [], false);
out.cancelled = lift("touchcancel", [], false);
out.endDragged = lift("touchend", [], true);
out.oneLeft = lift("touchend", [{}], true);
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function bookDoc\(doc\)", r"function bookLine\(\)"))
    assert out["listeners"] == [["touchend", 1, True], ["touchcancel", 1, True]]
    assert out["lastOff"] == [[False], 0, False] and out["cancelled"] == [[True], 0, False]
    assert out["endDragged"] == [[False], 1, False]  # the Lift let go, and the end's drag over: the tablet's own
    assert out["oneLeft"] == [[], 1, True]  # a finger still down: neither


def test_a_big_epubs_pieces_are_copies_so_the_requests_they_came_in_go(tmp_path):
    """Each piece of a big EPUB was once a part of the request it came in (up to 32 pieces, 8 MB), so a piece kept held
    its whole request, and dropping the oldest piece freed nothing while a neighbour stayed: the cap of 512 pieces wasn't
    one. Each piece is a copy of its own now; once read, the requests' bytes go."""
    book = tmp_path / "plates.epub"
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("OEBPS/plate.bin", bytes(range(256)) * 8192, zipfile.ZIP_STORED)  # 2 MB: eight pieces
    foliate = Path(serve.__file__).with_name("foliate").as_uri()
    script = """
require("v8").setFlagsFromString("--expose-gc");
const gc = require("vm").runInNewContext("gc");
const file = new Uint8Array(require("fs").readFileSync(%s));
const FOLIATE = %s, RD = { abort: new AbortController() }, navigator = {};  // a browser's, which says nothing of its memory
const answers = [];  // what each request brought, watched without being kept
const fetch = async (url, { headers }) => {
  const [, from, to] = /bytes=(\\d+)-(\\d+)/.exec(headers.Range);
  const bytes = file.slice(+from, +to + 1).buffer;  // its own bytes, as a browser's answer has them
  answers.push(new WeakRef(bytes));
  return { status: 206, arrayBuffer: async () => bytes };
};
/* part 1 */
(async () => {
  const loader = await rangeLoader("/plates.epub", file.length, () => {});
  const plate = new Uint8Array(await (await loader.loadBlob("OEBPS/plate.bin")).arrayBuffer());
  for (let i = 0; i < 3; i++) {
    await new Promise(done => setTimeout(done, 0));
    gc();
  }
  process.stdout.write(JSON.stringify({ read: plate.length, right: plate[1000] === 1000 %% 256, answers: answers.length,
    kept: answers.filter(a => a.deref()).length, loader: typeof loader.loadBlob }));
})();
""" % (json.dumps(str(book)), json.dumps(foliate))
    out = in_the_page(script, (r"const BLOCK = ", r"// A book's own scripts never run"))
    assert out["read"] == 2 << 20 and out["right"] and out["answers"] >= 1 and out["loader"] == "function"
    assert out["kept"] == 0  # the pieces, still kept for a chapter come back to, hold only their own bytes


# What an AZW3's or an EPUB's part goes out as, by the data foliate hands over and the type the book gives it
PARTS = {
    "an AZW3's sheet with no type": ("azw3", "bytes", "@import url(x.css); body { caret-color: red }", None),
    "an AZW3's page with no type, as bytes of an ArrayBuffer": ("azw3", "buffer", "<p>words</p>", None),
    "an AZW3's sheet": ("azw3", "text", "body { color: red }", "text/css"),
    "an AZW3's sheet in capitals, as bytes": ("azw3", "bytes", "body { margin: 0 }", "TEXT/CSS"),
    "an AZW3's SVG picture": ("azw3", "text", "<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
    "an AZW3's picture": ("azw3", "bytes", "\x89PNG\r\n", "image/png"),
    "an AZW3's picture with no type": ("azw3", "bytes", "\x89PNG\r\n", None),
    "an EPUB's page": ("epub", "text", "<p>x</p>", "application/xhtml+xml"),
}


def test_an_azw3s_parts_go_out_as_the_reader_cleaned_them_and_one_with_no_type_is_no_style():
    """foliate's mobi.js wrote { newType } for { type: newType }, so every part of an AZW3 became a file with no
    type: an SVG picture didn't show, and a style sheet with no type was applied as a style sheet, as a browser takes one
    for what it looks like. Now each part goes out as the type the reader cleaned it as, bytes of any kind decoded first:
    a part with no type as a page (no style sheet, so refused as one), a picture or font that names none as plain bytes,
    and without the charset, which mobi.js would not know an SVG picture by. An EPUB's parts say it as before."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the reader's cleaning runs only in a browser here")
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    rules = re.search(r"^const XHTML_NS = .*?^const utf8 = [^\n]*\n", page, re.S | re.M)[0]
    listener = re.search(r"^const partText = .*?(?=^// A book's link to another site)", page, re.S | re.M)[0]
    script = rules + """
const withoutScripts = (text, type) => utf8(`cleaned: ${text}`, pageType(type)), withoutOutside = css => css, withoutSvgPictures = css => css;
const RD = { made: [] };
globalThis.CustomEvent ??= class extends Event { constructor(type, { detail } = {}) { super(type); this.detail = detail; } };  // node before 19
""" + listener + """
const books = { azw3: { mobi: {}, transformTarget: new EventTarget() }, epub: { transformTarget: new EventTarget() } };
Object.values(books).forEach(scriptless);
const as = { text: s => s, bytes: s => Uint8Array.from(s, c => c.charCodeAt(0)), buffer: s => new TextEncoder().encode(s).buffer };
(async () => {
  const out = [];
  for (const [book, how, data, type] of %s) {
    const detail = { data: as[how](data), type: type === null ? undefined : type };
    books[book].transformTarget.dispatchEvent(new CustomEvent("data", { detail }));
    const made = await detail.data, said = await detail.type;
    out.push([typeof made === "string" ? made.replace(/^\\ufeff/, "") : "as it came", said === undefined ? null : said]);
  }
  process.stdout.write(JSON.stringify(out));
})();
""" % json.dumps(list(PARTS.values()))
    out = json.loads(subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert dict(zip(PARTS, out)) == {
        "an AZW3's sheet with no type": ["cleaned: @import url(x.css); body { caret-color: red }", "text/html"],
        "an AZW3's page with no type, as bytes of an ArrayBuffer": ["cleaned: <p>words</p>", "text/html"],
        "an AZW3's sheet": ["body { color: red }", "text/css"],
        "an AZW3's sheet in capitals, as bytes": ["body { margin: 0 }", "text/css"],
        "an AZW3's SVG picture": ["cleaned: <svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"],
        "an AZW3's picture": ["as it came", "image/png"],
        "an AZW3's picture with no type": ["as it came", "application/octet-stream"],
        "an EPUB's page": ["cleaned: <p>x</p>", "application/xhtml+xml;charset=utf-8"],
    }
    folder = Path(serve.__file__).with_name("foliate")
    mobi = folder.joinpath("mobi.js").read_text(encoding="utf-8")
    assert "new Blob([newData], { type: newType })," in mobi and "{ newType }" not in mobi  # the line put right
    noted = folder.joinpath("README.md").read_text(encoding="utf-8")
    asked = re.search(r'const FOLIATE = "/foliate/([0-9a-f]+)"', page)[1]
    assert "{ type: newType }" in noted and f"`{asked}`" in noted  # and a new address, or browsers keep the old file


def test_a_book_whose_parts_foliate_no_longer_hands_through_a_transform_target_is_not_shown_and_the_console_says_why():
    """An EPUB's pages, and a KF8 book's style sheets and pictures (an AZW3's, or a newer MOBI's), are cleaned on their way
    through foliate's transformTarget and its "data" event, and scriptless skipped that without a word when the book had
    none: a newer foliate that renamed it would have shown them uncleaned. Such a book is refused now, loudly: the reader
    closes and says the book couldn't be opened, and the console names what's missing. A book foliate gives none to
    today, an FB2 or an older MOBI (or an AZW3 whose headers say it's one), makes its pages some other way and opens."""
    script = """
const said = [];
console.error = (...words) => said.push(words.join(" "));
/* part 1 */
const target = () => new EventTarget(), kf8 = { headers: { kf8: { fdst: 1 } } }, older = { headers: { kf8: null } };
const out = {};
for (const [name, book, ext] of [["epub", { transformTarget: target(), sections: [] }, "epub"],
                                 ["epub without", { sections: [{ load() {}, unload() {} }] }, "epub"],
                                 ["epub with no target", { transformTarget: {}, sections: [] }, "epub"],
                                 ["azw3", { mobi: kf8, transformTarget: target(), sections: [] }, "azw3"],
                                 ["azw3 without", { mobi: kf8, sections: [] }, "azw3"],
                                 ["azw3 without, its headers unread", { mobi: {}, sections: [] }, "azw3"],
                                 ["azw3 of the older kind", { mobi: older, sections: [] }, "azw3"],
                                 ["kf8 mobi without", { mobi: kf8, sections: [] }, "mobi"],
                                 ["older mobi", { mobi: older, sections: [] }, "mobi"],
                                 ["fb2", { sections: [] }, "fb2"]]) {
  try { scriptless(book, ext); out[name] = "shown"; } catch (e) { out[name] = `refused: ${e.message}`; }
}
out.said = said;
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function scriptless\(", r"// A book's link to another site"))
    refused = "refused: foliate's book has no transformTarget"
    assert out == {"epub": "shown", "epub without": refused, "epub with no target": refused, "azw3": "shown",
                   "azw3 without": refused, "azw3 without, its headers unread": refused, "azw3 of the older kind": "shown",
                   "kf8 mobi without": refused, "older mobi": "shown", "fb2": "shown", "said": [out["said"][0]] * 5}
    assert "no transformTarget" in out["said"][0] and "foliate/README.md" in out["said"][0]
    mobi = Path(serve.__file__).with_name("foliate").joinpath("mobi.js").read_text(encoding="utf-8")
    kinds = re.findall(r"^class (\w+) \{\n(?:    .*\n)*?    transformTarget = ", mobi, re.M)
    assert kinds == ["KF8"] and "return isKF8 ? new KF8(this).init() : new MOBI6(this).init()" in mobi  # foliate's, today
    assert "const kf8 = mobi.version >= 8 ? getStruct(KF8_HEADER, buf) : null" in mobi
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    opening = page[page.index("async function openBook("):page.index("async function bookStyle(")]
    assert opening.index("await view.open(book);") < opening.index("scriptless(view.book, ext);") < opening.index("view.init(")
    assert 'closeReader();' in opening[opening.index("} catch (e) {"):] and '"This book couldn\'t be opened."' in opening


def test_a_fixed_layout_books_strip_is_its_own_pages_and_aa_offers_only_what_works_there():
    """A fixed-layout book's strip once went by how the server lays its pages out (two tiles for each page of a made-up
    EPUB), or for an AZW3, which the server doesn't draw, by its locations, its mark stuck on the first; and Aa offered
    the text's font, size, spacing and margins and Lift, which do nothing there. Now its strip is its own pages, a tile
    for each, with the server's picture of that page where it has one; and Aa has the page's colour, the bottom line and
    the strip. A book that flows keeps the server's pages and every row."""
    script = """
const els = {}, $ = id => els[id] || (els[id] = { hidden: false, style: {}, setAttribute() {}, textContent: "", innerHTML: "" });
const rows = ["font", "size", "spacing and margins"].map(name => ({ name, hidden: false }));
const document = { querySelectorAll: sel => sel === "#raap [data-flows]" ? rows : [] };
const went = [], RD = { book: true, it: { id: "t" }, place: { section: { current: 3, total: 6 }, location: { current: 1, total: 3 } },
  view: { isFixedLayout: true, renderer: { page: 3, pages: 6, goTo: t => { went.push(t); return Promise.resolve(); } },
    goToFraction: f => { went.push(f); } },
  tiles: { id: "t", pages: 12, counts: [2, 2, 2, 2, 2, 2], before: [0, 2, 4, 6, 8, 10] } };  // the server's drawing: two of each
const R = {}, BS = { size: 3 }, SIZES = [1, 2, 3, 4, 5, 6, 7], PAPER_NAMES = {};
const bookPaper = () => "theme", themePage = () => ["#ffffff"], canLift = () => true;
const showBookmarkPick = () => {}, turnTo = () => {}, reelTo = () => {};
/* part 1 */
/* part 2 */
/* part 3 */
/* part 4 */
const look = () => {
  showStyle();
  reelJump(5);
  return { tiles: reelTotal(), here: reelNow(), marked: bookmarkTile({ part: 4, inside: .9, pct: .1 }), jump: went.splice(0),
    rows: rows.filter(g => g.hidden).map(g => g.name), lift: !$("rturnset").hidden };
};
const out = { fixed: look() };
RD.view.isFixedLayout = false;
out.flowing = look();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function bookPages\(\)", r"const reelCentre = "), (r"function reelJump\(n\)", r'\$\("rtiles"\)\.addEventListener'),
                      (r"function bookmarkTile\(b\)", r"function onThisPage\(b\)"), (r"function showStyle\(\)", r'\$\("raa"\)\.onclick'))
    assert out["fixed"] == {"tiles": 6, "here": 3, "marked": 4, "jump": [{"index": 5, "anchor": 0}],
                            "rows": ["font", "size", "spacing and margins"], "lift": False}
    assert out["flowing"] == {"tiles": 12, "here": 7, "marked": 9, "jump": [{"index": 2, "anchor": .5}], "rows": [], "lift": True}
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    aa = page[page.index('<section id="raap"'):page.index('<section id="rfind"')]
    for row in ("Font", "Size"):
        assert f'<div class="rgroup" data-flows><span>{row}</span>' in aa
    assert 'data-flows>\n      <div class="rgroup"><span>Line spacing</span>' in aa
    assert aa.count("data-flows") == 3  # the page's colour, the bottom line and the strip stay


def test_a_fixed_layout_books_pages_turn_with_a_swipe_and_a_tap_lands_where_it_was():
    """A fixed-layout book's renderer follows no finger, and the reader left a book's moves to it: a swipe turned
    nothing. A finger gone sideways turns the page now, the way it went, as in a book that flows; not a short one, one
    that went more up or down, a word held, two fingers, or the page zoomed in. And a tap on such a page, which is scaled to the screen,
    turned the page or not by where it would have been unscaled: on a phone, a tap on the left third often did nothing."""
    script = """
const turned = [], R = { held: false, handle: false }, RD = { view: { book: { dir: "ltr" } } };
globalThis.visualViewport = { scale: 1 };
const turn = n => turned.push(n);
/* part 1 */
/* part 2 */
const on = {}, target = { addEventListener: (type, f) => { on[type] = f; } };
swipeTurns(target);
const finger = (x, y) => ({ screenX: x, screenY: y });
const swipe = (dx, dy = 0, fingers = 1) => {
  on.touchstart({ touches: Array.from({ length: fingers }, () => finger(200, 300)) });
  on.touchend({ touches: [], changedTouches: [finger(200 + dx, 300 + dy)] });
};
swipe(-120); swipe(120); swipe(-20); swipe(-60, -80); swipe(-120, 0, 2);
R.held = true; swipe(-120); R.held = false;
visualViewport.scale = 2; swipe(-120); visualViewport.scale = 1;  // zoomed in: a finger moves the page about
RD.view.book.dir = "rtl"; swipe(-120); swipe(120);
const frame = width => ({ defaultView: { frameElement: { offsetWidth: 600, getBoundingClientRect: () => ({ left: 17, width }) } } });
process.stdout.write(JSON.stringify({ turned, scaled: frameOnScreen(frame(495))[1], unscaled: frameOnScreen(frame(600))[1] }));
"""
    out = in_the_page(script, (r"const SWIPE_PX = ", r"function bookDoc\(doc\)"), (r"function frameOnScreen\(doc\)", r"function sameRange\("))
    assert out["turned"] == [1, -1, -1, 1]  # leftwards on, rightwards back; read right to left, the other way round
    assert out["scaled"] == .825 and out["unscaled"] == 1
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert "if (view.isFixedLayout) swipeTurns(r);" in page and "if (fixedLayout()) swipeTurns(doc);" in page  # around its pages and on them
    assert "tapAt(f.left + e.clientX * s - box.left, f.top + e.clientY * s - box.top);" in page


def test_a_jump_to_the_other_page_open_in_a_fixed_layout_book_is_told():
    """foliate's renderer for a fixed-layout book, asked for the other page of the two it has open (a tile of the
    strip, a bookmark), shows it and tells nobody: the strip's mark, the bottom line and your place stayed on the first.
    The reader tells of it as foliate tells of a turn; a jump foliate tells of itself isn't told twice."""
    script = """
const RD = { place: { section: { current: 1, total: 6 } } };
globalThis.CustomEvent ??= class extends Event { constructor(type, { detail } = {}) { super(type); this.detail = detail; } };  // node before 19
const bookWait = going => going, jumpUntilTaken = (r, real, args) => real(...args);
/* part 1 */
class Fixed extends EventTarget {  // foliate's: another spread is told of, the one open isn't
  get localName() { return "foliate-fxl"; }
  async goTo(target) {
    const { index } = await target;
    if (index > 2 && index !== RD.place.section.current) this.dispatchEvent(new CustomEvent("relocate", { detail: { index } }));
  }
  next() {} prev() {}
}
class Flowing extends Fixed { get localName() { return "foliate-paginator"; } }
(async () => {
  const out = {};
  for (const [name, Kind] of [["fixed", Fixed], ["flowing", Flowing]]) {
    const r = new Kind(), told = [];
    r.addEventListener("relocate", e => { told.push(e.detail.index); RD.place.section.current = e.detail.index; });
    RD.place.section.current = 1;
    watchMoves(r);
    await r.goTo({ index: 2 });  // the other page of the two open
    await r.goTo(Promise.resolve({ index: 4, anchor: 0 }));  // another spread
    await r.goTo({ index: 4 });  // where it is
    out[name] = told;
  }
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = in_the_page(script, (r"function watchMoves\(r\)", r"const AHEAD_AFTER_MS = "))
    assert out == {"fixed": [2, 4], "flowing": [4]}


# A fixed-layout book's pages as foliate shows them: a document for each part laid out, and whether it's on the screen
FIXED_PAGES = """
const page = (words, shown = true) => ({ body: {}, defaultView: { frameElement: { parentElement: { style: { display: shown ? "block" : "none" } } } },
  createRange: () => ({ selectNodeContents(node) { this.node = node; }, toString() { return this.node ? words : ""; } }) });
const laidOut = (...pages) => { RD.view.renderer.getContents = () => pages.map(([part, doc]) => { PART_OF.set(doc, part); return { doc }; }); };
"""


def test_a_fixed_layout_books_bookmark_is_known_by_its_pages_words_never_by_null():
    """A bookmark on a fixed-layout book's page was once kept as "…null": foliate gives such a book's place no range of
    words, and the bookmark took the words of none. It's known by its page's own words now, and by none where the page
    isn't laid out: the lists then show the page and the chapter alone."""
    script = """
const RD = { book: true, it: { id: "t" }, view: { isFixedLayout: true, renderer: { getContents: () => [] } },
  place: { cfi: "epubcfi(/6/6)", fraction: .5, range: null, section: { current: 2, total: 6 }, tocItem: { label: "Part 2" } } };
const BOOKMARK_WORDS = 120, readFile = () => ["Pictures/Lantern Wolf.epub", "epub"], reelTotal = () => 6, reelNow = () => 2;
const PART_OF = new WeakMap();
/* part 1 */
/* part 2 */
/* part 3 */
""" + FIXED_PAGES + """
const words = () => newBookmark("useful", null).text;
const out = { none: words() };
laidOut([1, page("The lantern keeper waves, page 2.")], [2, page("  A wolf\\n by the pier,  page 3. ")]);
out.page = words();
const made = newBookmark("useful", null);
out.rest = { at: made.at, part: made.part, page: made.page, of: made.of, note: made.note };
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function pageWords\(range\)", r"function wordsAround\(range\)"),
                      (r"(?:function partRange\(|function newBookmark\()", r"function pickReason\("),
                      (r"const insideChapter = ", r"function reelNow\(\)"))
    assert out["none"] == ""  # not laid out: no words, rather than "…null"
    assert out["page"] == "A wolf by the pier, page 3."  # the page the place is on, not the one beside it
    assert out["rest"] == {"at": "epubcfi(/6/6)", "part": 2, "page": 2, "of": 6, "note": "Part 2"}


def test_a_fixed_layout_book_is_finished_at_its_own_last_page_and_both_pages_on_screen_are_marked():
    """A fixed-layout book was once marked finished by its locations, which its few words make few: "Marked as finished"
    came at page 4 of 6. It's finished when its own last page is on the screen now, one page or two side by side; and on
    the strip both pages of two side by side are where you are. A book that flows still goes by how far its text is."""
    script = """
let finished = 0;
const RD = { book: true, it: { id: "t" }, jumped: false, view: { isFixedLayout: true, renderer: { getContents: () => [] } } };
const els = {}, $ = id => els[id] || (els[id] = { hidden: id !== "rreel", textContent: "", innerHTML: "" });
const BS = { line: "page" }, localStorage = { setItem() {} }, keepLocalPlace = () => {}, finishedHere = () => finished++, askFinished = () => {};
for (const name of ["hideLook", "showBookmarkPick", "linkNoteGone", "bookLine", "bookmarkButton"]) globalThis[name] = () => {};
const placeMoved = () => true, reelTotal = () => 6, reelCentre = () => 3, reelNow = () => RD.place.section.current, shownPages = n => [n];
const esc = s => s, placeName = () => "";
const tiles = Array.from({ length: 6 }, () => { const on = new Set(); return { on, classList: { add: (...c) => c.forEach(x => on.add(x)), remove: (...c) => c.forEach(x => on.delete(x)) } }; });
els.rtiles = { children: tiles, querySelectorAll: () => tiles.filter(t => t.on.size), contains: () => false };
const document = { activeElement: null }, R = {};  // the strip's one Tab stop goes to the middle page
/* part 1 */
/* part 2 */
/* part 3 */
""" + FIXED_PAGES + """
const at = (current, ...shown) => {  // foliate's place, and the pages on the screen with it
  finished = 0;
  laidOut(...shown.map(([part, on]) => [part, page("", on)]));
  bookPlace({ cfi: `epubcfi(/6/${2 * current + 2})`, fraction: current / 6, section: { current, total: 6 }, location: { current: 2, next: 2, total: 3 } });
  reelLabel();
  return [finished, tiles.map((t, n) => t.on.has("now") ? n : -1).filter(n => n >= 0)];
};
const out = { spread: at(3, [3, true], [4, true]), last: at(4, [4, true], [5, true]), upright: at(4, [4, true], [5, false]), uprightLast: at(5, [4, false], [5, true]) };
RD.view.isFixedLayout = false;
out.flowing = [at(5), (finished = 0, bookPlace({ cfi: "x", fraction: .996, section: { current: 5, total: 6 }, location: {} }), finished)];
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const fixedLayout = ", r"const ownPages = "), (r"function bookPlace\(d\)", r"// Aa: the text's look"),
                      (r"function reelLabel\(\)", r"// A book's tiles: pictures of its pages"))
    assert out["spread"] == [0, [3, 4]]  # page 4 of 6, its locations at their last: not finished; both pages marked
    assert out["last"] == [1, [4, 5]]  # the last two side by side: the last is on the screen
    assert out["upright"] == [0, [4]] and out["uprightLast"] == [1, [5]]  # one at a time: only at the last itself
    assert out["flowing"] == [[1, [5]], 1]  # its locations, and its text, as before


def test_the_look_sheet_and_the_filters_over_the_titles_are_dialogs_with_the_page_behind_them_inert():
    """The Look sheet covered the screen with a hold of its own on the page and focus calls of its own, and Tab walked
    out of it into the library behind; the filters, as a sheet or a drawer over the titles, left the focus on the
    header's covered controls. Both are dialogs like the others now: the focus goes in, what lies behind is inert, and
    closing gives the focus back. The filters lie in the page's layout beside the titles, so the titles are inert there;
    their own shade stays free, as a tap on it closes them, and is inert behind any other dialog."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    watched = page[page.index("const isOpen = el =>"):page.index("// ---- the reader's panels and the keys")]
    assert 'for (const el of [$("detail"), $("reader"), $("looksheet"), $("fpanel"), ...document.querySelectorAll(".modal")]) {' in watched
    assert 'el.id === "fpanel" ? filtersOver()' in watched and 'el.matches("#reader, #looksheet") ? !el.hidden' in watched
    look = page[page.index("function openLook() {"):page.index("function pickColour(")]
    assert ".focus();" not in look.split("function lookBack()")[0] and "lsopen" not in page  # one way to hold the page: html.held
    assert '$("looksheet").addEventListener("keydown"' not in page and 'if (e.key === "Escape" && !$("looksheet").hidden) return lookBack();' in page
    setopen = page[page.index("function setOpen(on) {"):page.index("function toggleOpen(on) {")]
    assert "overflow" not in setopen
    script = FAKE_PAGE + """
const header = new El("header"), accountbtn = new El("accountbtn", { tag: "BUTTON" }), filtersbtn = new El("filtersbtn", { tag: "BUTTON" });
header.append(accountbtn); header.append(filtersbtn);
body.kids.unshift(header); header.parentNode = body;
const fpanel = new El("fpanel", { z: 15 }), main = new El("main"), looksheet = new El("looksheet", { z: 35 });
const fshade = new El("fshade");
fshade.dataset = { shadeOf: "fpanel" };
$("layout").append(fpanel); $("layout").append(main); body.append(fshade); body.append(looksheet);
/* part 1 */
const state = () => ({ inert: everything().filter(e => e.inert).map(e => e.id), held: html.classList.contains("held"), focus: document.activeElement.id });
const out = {};
KEYS = true;
accountbtn.focus();
looksheet.hidden = false; dialogChanged(looksheet, true); out.look = state();
looksheet.hidden = true; dialogChanged(looksheet, false); out.lookClosed = state();
filtersbtn.focus();
dialogChanged(fpanel, true); out.filters = state();
dialogChanged(fpanel, false); out.filtersClosed = state();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const DIALOGS = \[\];", r"// A drag on a dialog's dimmed part"))
    assert out["look"] == {"inert": ["header", "layout", "fshade"], "held": True, "focus": "looksheet"}
    assert out["lookClosed"] == {"inert": [], "held": False, "focus": "accountbtn"}
    assert out["filters"] == {"inert": ["header", "main"], "held": True, "focus": "fpanel"}  # the panel's own layout and shade free, the titles beside it not
    assert out["filtersClosed"] == {"inert": [], "held": False, "focus": "filtersbtn"}


# A reader's panel as far as the focus goes: its controls, which may be hidden, turned off, in its list or the part you're in
PANELS_PAGE = """
let KEYS = true, FOCUSED = [null, null];
const body = { id: "body" }, document = { body, activeElement: body };
class C {
  constructor(id, more = {}) { Object.assign(this, { id, disabled: false, gone: false, inList: false, current: false, panel: null }, more); }
  focus() { FOCUSED = [this, FOCUSED[0]]; document.activeElement = this; }
  closest(sel) { return sel === "[hidden]" ? (this.gone || (this.panel && this.panel.hidden) ? this : null) : null; }
  getClientRects() { return this.gone || (this.panel && this.panel.hidden) ? [] : [{}]; }
  get isConnected() { return true; }
}
const panel = (id, kids) => {
  const p = { id, hidden: true, kids, contains: x => kids.includes(x), querySelector: sel => sel === "[aria-current]" ? kids.find(k => k.current) || null : null,
              querySelectorAll: sel => sel === "ol button" ? kids.filter(k => k.inList) : kids };
  for (const k of kids) k.panel = p;
  return p;
};
const bar = { rtoc: new C("rtoc"), raa: new C("raa"), rsearch: new C("rsearch"), elsewhere: new C("elsewhere") };
const toc = panel("rtocp", [new C("rtocx"), new C("row1", { inList: true }), new C("row2", { inList: true, current: true })]);
const aa = panel("raap", [new C("font", { gone: true }), new C("literata"), new C("rlarger")]);
const find = panel("rfind", [new C("rq"), new C("rqdone")]);
const move = (p, open) => { p.hidden = !open; panelChanged(p); return document.activeElement.id; };
"""


def test_a_readers_panel_opened_with_the_keys_takes_the_focus_and_gives_it_back_as_it_closes():
    """Contents, Aa, a bookmark's reasons and the word box opened without taking the focus, so reaching them meant
    tabbing through the bar, and closing Contents or the search left the focus on the page. Opened while the keys are
    in use, a panel takes it now: Contents on the part you're in, the others on their first control there is; closed,
    it gives it back to what had it, unless the focus went elsewhere meanwhile. A finger or a mouse leaves it where it is,
    so that Space presses nothing it didn't mean to."""
    script = PANELS_PAGE + """
/* part 1 */
const out = {};
bar.rtoc.focus();
out.toc = [move(toc, true), move(toc, false)];
bar.raa.focus();
out.aa = [move(aa, true), move(aa, false)];  // its first control hidden (a fixed-layout book's): the next
bar.rsearch.focus(); find.kids[0].focus();  // the search puts the focus in its field itself
out.find = [move(find, true), move(find, false)];
bar.raa.focus();
move(aa, true); bar.elsewhere.focus();
out.elsewhere = move(aa, false);  // gone on elsewhere meanwhile: it stays there
KEYS = false;
bar.raa.focus();
out.mouse = [move(aa, true), move(aa, false)];
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const PANEL_FROM = new Map\(\);", r'for \(const id of \["rtocp", "raap"'))
    assert out["toc"] == ["row2", "rtoc"]
    assert out["aa"] == ["literata", "raa"]
    assert out["find"] == ["rq", "rsearch"]
    assert out["elsewhere"] == "elsewhere"
    assert out["mouse"] == ["raa", "raa"]
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert 'for (const id of ["rtocp", "raap", "rbmp", "rlook", "rfind"]) new MutationObserver(() => panelChanged($(id)))' in page
    tip = page[page.index("function showShelfTip() {"):page.index('$("shelfpop").onclick')]
    assert '!["Tab", "Shift"].includes(e.key)' in tip  # Tab goes into the Shelves tip, to Show me and Got it, rather than closing it


def test_the_page_strip_is_one_tab_stop_and_the_arrows_go_along_it_without_turning_the_book():
    """Every page of the strip was a Tab stop, before Start, the ribbon and End: 300 presses for a long comic. The strip
    is one stop now, its middle page's, and Left and Right on it move the focus along it, page by page, turning nothing;
    Home and End take the book to its ends as ever, with the focus. On the page itself the arrows turn it, as before."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    build = page[page.index("function buildReel() {"):page.index("function reelTo(")]
    assert 'data-n="${n}" tabindex="-1"' in build and "R.stop = null;" in build
    label = page[page.index("function reelLabel() {"):page.index("// A book's tiles: pictures")]
    assert "reelStop(held || box.children[n]);" in label
    script = """
const turned = [], jumped = [], rolled = [];
const GUIDE = { open: false }, R = { menu: true, stop: null }, ME = null;
const els = {}, $ = id => els[id] || (els[id] = { hidden: id !== "reader" });
const document = { querySelector: () => null, addEventListener() {}, activeElement: null };
const turn = n => turned.push(n), reelJump = n => jumped.push(n), reelTotal = () => 4, reelTo = n => rolled.push(n);
const strip = { children: [] };
for (let n = 0; n < 4; n++) strip.children.push({ n, tabIndex: n ? -1 : 0, dataset: { n: String(n) }, parentNode: strip, isConnected: true,
  focus() { document.activeElement = this; }, closest(sel) { return sel.split(",").map(s => s.trim()).includes(".rtile") ? this : null; } });
R.stop = strip.children[0];
const body = { closest: () => null };
/* part 1 */
/* part 2 */
const press = (key, target) => {
  if (target.focus) target.focus();
  turned.length = jumped.length = rolled.length = 0;
  let taken = false;
  readerKey({ key, target, shiftKey: false, preventDefault: () => { taken = true; }, stopImmediatePropagation() {} });
  return { focus: document.activeElement ? document.activeElement.n : null, stops: strip.children.map(t => t.tabIndex), turned: [...turned], jumped: [...jumped], rolled: [...rolled], taken };
};
const tile = n => strip.children[n];
process.stdout.write(JSON.stringify({
  right: press("ArrowRight", tile(0)), again: press("ArrowRight", tile(1)), left: press("ArrowLeft", tile(2)),
  first: press("ArrowLeft", tile(0)), end: press("End", tile(1)), home: press("Home", tile(3)), page: press("ArrowRight", body),
}));
"""
    out = in_the_page(script, (r"function readerKey\(e\)", r'document\.addEventListener\("keydown", readerKey, true\);'),
                      (r"// The strip is one Tab stop", r"// A book's tiles: pictures"))
    assert out["right"] == {"focus": 1, "stops": [-1, 0, -1, -1], "turned": [], "jumped": [], "rolled": [1], "taken": True}
    assert out["again"]["focus"] == 2 and out["again"]["stops"] == [-1, -1, 0, -1] and out["again"]["turned"] == []
    assert out["left"]["focus"] == 1 and out["left"]["stops"] == [-1, 0, -1, -1]
    assert out["first"] == {"focus": 0, "stops": [-1, 0, -1, -1], "turned": [], "jumped": [], "rolled": [], "taken": True}  # the start: nothing moves, nor the book
    assert out["end"]["jumped"] == [3] and out["end"]["focus"] == 3 and out["end"]["stops"] == [-1, -1, -1, 0]
    assert out["home"]["jumped"] == [0] and out["home"]["focus"] == 0
    assert out["page"]["turned"] == [1] and out["page"]["taken"]  # on the page the arrows turn it, as ever


def test_less_motion_asked_for_places_the_pages_at_once():
    """With reduced motion asked for, the decorations stopped but a comic's page still rolled across the screen, a book
    kept foliate's animated turn, and the page strip glided. Now a comic's page is in place at once, a book turns without
    the animation (and gets it back if the setting changes meanwhile), and the strip jumps; without it all moves as before."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert 'setAttribute("animated"' not in page and page.count("turnMotion(") == 3  # the helper, the book's opening and a Lift's end
    assert page.count('matchMedia("(prefers-reduced-motion: reduce)")') == 1  # one question, LESS_MOTION, for all of it
    script = """
const MOTION = { matches: true, listeners: [], addEventListener(kind, f) { this.listeners.push(f); } };
const matchMedia = () => MOTION;
const frames = [], placed = [];
const requestAnimationFrame = f => frames.push(f), setTimeout = () => 0;
const R = { x: 0, busy: false }, RD = { seq: 1, view: null }, placeStrip = x => { placed.push(x); R.x = x; };
let LIFT = null;
const scrolled = [], els = { rtiles: { scrollTo: o => scrolled.push(o.behavior) } }, $ = id => els[id];
let TILE = 76;
/* part 1 */
/* part 2 */
/* part 3 */
const renderer = { attrs: new Set(), toggleAttribute(name, on) { on ? this.attrs.add(name) : this.attrs.delete(name); } };
const out = {};
let done = 0;
rollTo(400, () => done++);
out.still = { done, placed: [...placed], frames: frames.length, busy: R.busy };
reelTo(3, true);
turnMotion(renderer);
out.still.strip = scrolled.splice(0);
out.still.book = [...renderer.attrs];
MOTION.matches = false;
RD.view = { renderer };
MOTION.listeners.forEach(f => f());  // the setting changed with a book open
out.moving = { book: [...renderer.attrs] };
placed.length = 0; done = 0;
rollTo(800, () => done++);
reelTo(3, true);
out.moving.rolling = { done, frames: frames.length > 0, strip: scrolled.splice(0) };
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const LESS_MOTION = ", r"// x: the strip's place"), (r"const ROLL_MS = ", r"function load\(slot, n\)"),
                      (r"function reelTo\(", r"function reelShown\("))
    assert out["still"] == {"done": 1, "placed": [400], "frames": 0, "busy": False, "strip": ["auto"], "book": []}
    assert out["moving"] == {"book": ["animated"], "rolling": {"done": 0, "frames": True, "strip": ["smooth"]}}


def test_a_control_aa_hides_or_turns_off_hands_the_focus_on_only_with_the_keys():
    """The largest size reached turns A+ off, and Back to the theme's hides itself: with the keys the focus goes to the
    control beside it (A−, the page picked), so that it isn't lost. A mouse or a finger had it moved too, so the next
    Space, which turns the page, pressed A− and made the text smaller. Now only the keys move it."""
    script = """
let KEYS = true;
const document = { activeElement: null };
class B { constructor(id, more = {}) { Object.assign(this, { id, disabled: false, gone: false }, more); } focus() { document.activeElement = this; }
  closest(sel) { return sel === "#rpapnote" ? (this.inNote ? {} : null) : null; } }
const larger = new B("rlarger"), smaller = new B("rsmaller"), back = new B("rpapback", { inNote: true }), paper = new B("paper");
const els = { rlarger: larger, rsmaller: smaller, raap: { contains: x => [larger, smaller, back, paper].includes(x), querySelector: () => paper } };
const $ = id => els[id], shownControl = el => !el.disabled && !el.gone;
const aaPressed = e => { if (e.target === larger) larger.disabled = true; if (e.target === back) back.gone = true; };
/* part 1 */
const press = (b, keys) => { KEYS = keys; larger.disabled = back.gone = false; b.focus(); $("raap").onclick({ target: b }); return document.activeElement.id; };
process.stdout.write(JSON.stringify({ keys: [press(larger, true), press(back, true)], mouse: [press(larger, false), press(back, false)] }));
"""
    out = in_the_page(script, (r'\$\("raap"\)\.onclick = e => \{', r"function aaPressed\(e\)"))
    assert out == {"keys": ["rsmaller", "paper"], "mouse": ["rlarger", "rpapback"]}


def test_closing_aa_or_the_search_with_the_menu_open_brings_its_page_strip_back():
    """Aa and the search hide the page strip, and closing them left the menu counted as open without it: M, or a tap
    at the top, then only closed a menu that wasn't showing, and a second one opened it. Closed while the menu is open,
    they bring its strip back now, so that the menu is as it was; with it closed, nothing comes."""
    script = """
const els = {}, $ = id => els[id] || (els[id] = { hidden: true, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } });
const R = { menu: false, docked: false };
let built = 0;
for (const name of ["showBookmarkPick", "hideLook", "reelTo", "reelShown", "reelHeight", "showStyle"]) globalThis[name] = () => {};
const reelTotal = () => 12, reelNow = () => 3, buildReel = () => built++;
/* part 1 */
/* part 2 */
/* part 3 */
const shown = () => ({ menu: R.menu, strip: !$("rreel").hidden, aa: !$("raap").hidden, find: !$("rfind").hidden, bar: !$("rbar").hidden });
const out = {};
showBar(true); out.menu = shown();
showAa(true); out.aa = shown();
showAa(false); out.aaClosed = shown();
$("rreel").hidden = true; $("rbar").hidden = true; $("rfind").hidden = false;  // the search, as openFind opens it
closeFind(); out.findClosed = shown();
showBar(false); showAa(true); showAa(false); out.closedMenu = shown();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function showBar\(on\)", r"function reelHeight\(\)"), (r"// Aa: the text's look", r"function showStyle\(\)"),
                      (r"function closeFind\(\)", r"function endFind\(\)"))
    assert out["menu"] == {"menu": True, "strip": True, "aa": False, "find": False, "bar": True}
    assert out["aa"] == {"menu": True, "strip": False, "aa": True, "find": False, "bar": True}
    assert out["aaClosed"] == {"menu": True, "strip": True, "aa": False, "find": False, "bar": True}
    assert out["findClosed"] == {"menu": True, "strip": True, "aa": False, "find": False, "bar": True}
    assert out["closedMenu"] == {"menu": False, "strip": False, "aa": False, "find": False, "bar": False}


def test_the_room_for_the_ipads_own_close_button_is_kept_only_where_ios_webkit_draws_one():
    """In full screen any touch screen got 100 px of empty room at the left of the reader's bar, its contents and the
    guide's bar, and no Leave full screen: for the ✕ that only the iPad's Safari draws. Android tablets draw none. The
    room is one custom property now, under iOS's WebKit alone (-webkit-touch-callout), and the script's two choices go
    by the same test."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    style = page[page.index("<style>"):page.index("</style>")]
    assert "padding-left: 100px" not in style and style.count("var(--closex)") == 3 and ":root { --closex: 100px; }" in style
    ios = style[style.index("@supports (-webkit-touch-callout: none) {"):]
    ios = ios[:ios.index("\n}\n")]
    assert "#reader:fullscreen :is(.rbar, .rtochead) { padding-left: var(--closex); }" in ios
    assert "#reader:-webkit-full-screen :is(.rbar, .rtochead) { padding-left: var(--closex); }" in ios
    assert ".gfull .gbar { padding-left: var(--closex); }" in style
    assert 'const IOS_CLOSE = CSS.supports("-webkit-touch-callout", "none");' in page
    assert '$("rfull").hidden = APP || !canFull() || (IOS_CLOSE && !!fullEl());' in page
    assert 'g.classList.toggle("gfull", !!fullEl() && IOS_CLOSE);' in page


# ---- the word box's questions to Wikimedia, a page's width, End, opening a book, an id a rebuild moved, RAR ----

WIKIMEDIA_STAND_IN = """
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
class DOMParser { parseFromString(html) { return { querySelectorAll: () => [], querySelector: () => null, body: { textContent: html.replace(/<[^>]*>/g, "") } }; } }
let TONGUE = "", asked = [];
const translations = async () => ({ body: "" });
// Wikimedia as these tests say it is: an answer by the address's last part, a status alone, or none at all
let answers = {};
const fetch = (url, { signal }) => {
  asked.push(url);
  const said = answers[decodeURIComponent(url.split("/").pop())] ?? answers[url];
  if (said === "never") return new Promise((_, no) => {  // node's time limit holds nothing open: this does, meanwhile
    const open = setTimeout(() => {}, 5000);
    signal.addEventListener("abort", () => { clearTimeout(open); no(signal.reason); });
  });
  if (typeof said === "number") return Promise.resolve({ ok: said < 400, status: said, json: async () => ({}) });
  if (said === undefined) return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  return Promise.resolve({ ok: true, status: 200, json: async () => said });
};
const shortly = AbortSignal.timeout.bind(AbortSignal), waits = [];
AbortSignal.timeout = ms => { waits.push(ms); return shortly(30); };  // the time limit asked for, kept to a moment here
const entry = (pos, meaning) => [{ partOfSpeech: pos, language: "English", definitions: [{ definition: meaning }] }];
"""


def test_the_word_box_s_questions_have_a_time_limit_and_only_an_answer_is_kept():
    """The word box's five questions had no time limit: on a network that swallowed them the box said "Looking it up…"
    until the browser gave up, and the same word asked again waited on the same question. A word Wiktionary lacks was
    asked again every time, while a refused Wikipedia question (429) stuck as "No Wikipedia article". Every question goes
    through one helper now, with a time limit of 8 seconds: a 404 is "not there", kept; any other refusal, or no answer
    in time, is "No answer", asked again next time; and the action API's addresses are made from its parameters."""
    script = WIKIMEDIA_STAND_IN + """
/* part 1 */
/* part 2 */
/* part 3 */
(async () => {
  const out = {};
  answers = { Lanternwolf: "never" };
  out.never = (await answerOf("dict", "Lanternwolf", "en")).body;
  out.neverAgain = (asked = [], await answerOf("dict", "Lanternwolf", "en"), asked.length);  // not kept: asked again
  answers = {};  // no page of it
  out.none = (await answerOf("dict", "zorbleflax", "en")).body;
  out.noneAgain = (asked = [], await answerOf("dict", "zorbleflax", "en"), asked.length);  // kept: not asked again
  answers = { Moonwake_Harbour: 429 };
  out.busy = (await answerOf("wiki", "Moonwake Harbour", "en")).body;
  answers = { Moonwake_Harbour: { title: "Moonwake Harbour", extract: "A made-up harbour." } };
  out.later = (await answerOf("wiki", "Moonwake Harbour", "en")).body;  // asked again, and kept now
  answers = { "Moonwake Island": 404 };
  out.noArticle = (await answerOf("wiki", "Moonwake Island", "en")).body;
  answers = { harbour: 503 };
  out.down = (await answerOf("dict", "harbour", "en")).body;
  out.waits = [...new Set(waits)];
  answers = {}; asked = [];
  await wikimedia(WIKTIONARY, { prop: "categories", clcategories: "Category:English lemmas", titles: "sotto voce|of course" });
  out.api = asked[0];
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = in_the_page(script, (r"const LOOKS = new Map\(\);", r"function bookLang\(\)"),
                      (r"// An answer, asked once", r"// A word held may be part of a term"),
                      (r"const plain = html =>", r"// A word in another language:"))
    assert out["never"] == "<p>No answer: Wikimedia didn't answer in time.</p>" and out["neverAgain"] == 1
    assert out["none"] == "<p>“zorbleflax” isn't in Wiktionary.</p>" and out["noneAgain"] == 0
    assert out["busy"] == "<p>No answer: Wikimedia turned the question down just now.</p>"
    assert out["later"] == "<p>A made-up harbour.</p>"
    assert out["noArticle"] == "<p>No Wikipedia article called “Moonwake Island”.</p>"
    assert out["down"] == "<p>No answer: Wikimedia turned the question down just now.</p>"
    assert out["waits"] == [8000]
    assert out["api"] == ("https://en.wiktionary.org/w/api.php?action=query&format=json&formatversion=2&origin=*&prop=categories"
                          "&clcategories=Category%3AEnglish+lemmas&titles=sotto+voce%7Cof+course")
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    box = page[page.index("// A word's meaning, beside it"):page.index("// The page strip at the bottom")]
    assert box.count("fetch(") == 1 and "fetch(url, { signal: AbortSignal.timeout(LOOK_WAIT_MS) })" in box  # the one way
    assert page.count("/w/api.php") == 1  # its addresses made in one place, from the parameters


# A made-up word's page in Wiktionary's own markup, as its translations tab reads it: its English part between other
# languages', tables of translations by sense under each part of speech, a language's line nested under another's, a
# word shown otherwise than its link (alt=), its letters in ours (tr=), a note before a word and one after, a table
# nobody has checked yet, and a part of speech whose translations are on a subpage of their own
GLIMMERLAMP = """==Translingual==
===Symbol===
{{trans-top|not English}}
* Finnish: {{t|fi|kääntäjä}}
{{trans-bottom}}

==English==

===Etymology===
Made up for this test, from {{m|en|glimmer}} + {{m|en|lamp}}.

===Noun===
{{en-noun}}

# A small [[lamp]] that glimmers.

====Translations====
{{trans-top|a small lamp that [[glimmer]]s}}
* Finnish: {{t+|fi|hohtolamppu}}, {{t|fi|[[hohto]]valo|alt=hohto-valo}} {{q|rare}}
* Chinese:
*: Mandarin: {{t|cmn|微光灯|tr=wēiguāngdēng}}
* Russian: {{t+|ru|мерца́лка|f|tr=mercálka}}
{{trans-bottom}}
{{trans-top|a festival's paper lamp}}
* Finnish: {{qualifier|festive}} {{t|fi|juhlalyhty}}
{{trans-bottom}}
{{checktrans-top}}
* Finnish: {{t-check|fi|tarkistamaton}}
{{trans-bottom}}

===Verb===
{{en-verb}}

# To light with a glimmerlamp.

====Translations====
{{see translation subpage|Verb}}

==Finnish==

===Noun===
{{fi-noun}}

# a made-up word
{{trans-top|not English either}}
* Finnish: {{t|fi|väärä}}
{{trans-bottom}}
"""
GLIMMERLAMPS = """==English==

===Noun===
{{head|en|noun form}}

# {{plural of|en|glimmerlamp}}
#: {{ux|en|Two {{l|en|glimmerlamps}}.}}
"""


def test_the_translations_tab_reads_wiktionary_s_own_markup():
    """englishPart, templatesIn, templateParts, translationTables and lemmaOf: what they make of a made-up page in
    Wiktionary's markup, under node. Only the English part counts; a language is found by its code, nested or not; a
    link's own words, alt= and tr= are read as the page shows them; a note goes with its word; a table nobody has
    checked is left out; a subpage of translations is named by its part of speech; and a form leads to its word."""
    script = f"""
/* part 1 */
const page = {json.dumps(GLIMMERLAMP)}, form = {json.dumps(GLIMMERLAMPS)};
const english = englishPart(page);
process.stdout.write(JSON.stringify({{
  english: [english.startsWith("\\n===Etymology==="), english.includes("Translingual"), english.includes("not English")],
  fi: translationTables(english, "fi"), cmn: translationTables(english, "cmn"), ru: translationTables(english, "ru"),
  nested: templatesIn("a {{{{t|fi|{{{{l|fi|x}}}}}}}} b {{{{q|y}}}}"), parts: templateParts("t+|fi|[[a|b]]|alt=c|tr = d"),
  lemma: [lemmaOf(englishPart(form)), lemmaOf(english), englishPart("==Finnish==\\n# no English here")],
}}));
"""
    out = in_the_page(script, (r"function englishPart\(text\)", r"async function translations\("))
    assert out["english"] == [True, False, False]  # from its English heading to the next language's
    noun, festive, verb = "a small lamp that glimmers", "a festival's paper lamp", {"sub": "verb"}
    assert out["fi"] == [{"kind": "noun", "gloss": noun, "words": [{"word": "hohtolamppu", "tr": "", "note": ""},
                                                                   {"word": "hohto-valo", "tr": "", "note": "rare"}]},
                         {"kind": "noun", "gloss": festive, "words": [{"word": "juhlalyhty", "tr": "", "note": "festive"}]},
                         verb]  # not the table nobody has checked
    assert out["cmn"][0]["words"] == [{"word": "微光灯", "tr": "wēiguāngdēng", "note": ""}] and out["cmn"][1]["words"] == []
    assert out["ru"][0]["words"] == [{"word": "мерца́лка", "tr": "mercálka", "note": ""}]
    assert out["nested"] == ["t|fi|{{l|fi|x}}", "q|y"]
    assert out["parts"] == {"name": "t+", "order": ["fi", "[[a|b]]"], "named": {"alt": "c", "tr": "d"}}
    assert out["lemma"] == ["glimmerlamp", "", ""]


def test_a_sentence_s_first_word_is_looked_up_in_small_letters_first_and_a_name_s_page_is_passed_over():
    """A sentence's first word is capitalised, and Wiktionary has pages for many such words that are names or other
    languages' words: "Will you come?" gave the given name, and in a Finnish book a capitalised page without a Finnish
    entry said the word wasn't there. Small letters are now tried when the page as picked has no entry in the book's
    language, and for a sentence's first word they are asked first."""
    script = WIKIMEDIA_STAND_IN + """
/* part 1 */
/* part 2 */
/* part 3 */
(async () => {
  const out = {};
  answers = { Will: { en: entry("Proper noun", "A given name.") }, will: { en: entry("Verb", "To be going to.") },
              Kuu: { en: entry("Proper noun", "A made-up name.") }, kuu: { fi: [{ partOfSpeech: "Noun", language: "Finnish", definitions: [{ definition: "moon" }] }] } };
  out.first = (await answerOf("dict", "Will", "en", true)).kind;
  out.firstAsked = asked.map(u => u.split("/").pop());
  asked = [];
  out.inside = (await answerOf("dict", "Will", "en", false)).kind;  // within a sentence: a name, as picked
  out.finnish = (await answerOf("dict", "Kuu", "fi")).body;
  out.small = (await answerOf("dict", "harbour", "en", true), asked.filter(u => u.endsWith("/harbour")).length);
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = in_the_page(script, (r"const LOOKS = new Map\(\);", r"function bookLang\(\)"),
                      (r"// An answer, asked once", r"// A word held may be part of a term"),
                      (r"const plain = html =>", r"// A word in another language:"))
    assert out["first"] == "verb" and out["firstAsked"] == ["will"]  # the small letters first, and found
    assert out["inside"] == "proper noun"
    assert out["finnish"] == "<p>1. moon</p>"  # the capitalised page had no Finnish entry
    assert out["small"] == 1  # a word in small letters already is asked once


def test_a_page_s_width_is_one_of_the_server_s_steps_so_a_resized_window_asks_for_no_page_again():
    """Page widths went 2000 then 3200, so a 2048-wide tablet or a 4K screen full screen got pages 3200 wide. And the page
    put the window's own width in every page's address, so after a resize or full screen the browser fetched the pages it
    had again. The steps are even now, each about a quarter wider from 1200; the server says them in /api/session, and
    the page rounds up to one itself, so a window a little larger or smaller has the same addresses."""
    assert reader.WIDTHS == (240, 600, 900, 1200, 1500, 1900, 2400, 3200)
    steps = reader.WIDTHS[3:]
    assert all(1.2 <= b / a <= 1.35 for a, b in zip(steps, steps[1:]))
    assert reader.width_for(2048) == reader.width_for(2160) == 2400 and reader.width_for(1640) == 1900
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert re.search(r"let PAGE_WIDTHS = \[([\d, ]+)\];", page)[1] == ", ".join(map(str, reader.WIDTHS))  # until the server says
    script = """
let innerWidth = 1280, innerHeight = 800, devicePixelRatio = 2;
const RD = { it: { id: "h:lwolf1" }, v: "1700000000-1" };
/* part 1 */
const out = [];
for (const [w, h, d] of [[1280, 800, 2], [1366, 860, 2], [1440, 900, 1], [1024, 1366, 2], [3840, 2160, 1]]) {
  [innerWidth, innerHeight, devicePixelRatio] = [w, h, d];
  out.push(new URLSearchParams(pageUrl(3).split("?")[1]).get("w"));
}
PAGE_WIDTHS = [300, 3000];  // as another server says
[innerWidth, innerHeight, devicePixelRatio] = [200, 100, 1];
out.push(wantedWidth());
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"// A page's width: about the screen's shorter side", r"// Full screen: by itself on phones"))
    assert out == ["1900", "1900", "900", "2400", "2400", 300]


def test_the_server_says_the_widths_and_a_page_s_address_changes_with_how_pages_are_drawn(served, monkeypatch):
    """The page's v, in every page's address, which browsers keep a week, was the file's time alone: a change in how
    pages are drawn (reader.DRAWN) reached browsers up to a week late. It is in v now, as BOOK_LAYOUT is for a book's."""
    client, _, comic, _ = served
    assert client.get("/api/session").json()["widths"] == list(reader.WIDTHS)
    comic_v = client.get("/api/pages", params={"id": comic["id"]}).json()["v"]
    book_v = client.get("/api/pages", params={"id": "h:novel", "book": 1}).json()["v"]
    assert comic_v.split("-")[1:] == [str(reader.DRAWN)] and book_v.split("-")[1:] == [str(reader.DRAWN), str(reader.BOOK_LAYOUT)]
    monkeypatch.setattr(reader, "DRAWN", reader.DRAWN + 1)
    assert client.get("/api/pages", params={"id": comic["id"]}).json()["v"] != comic_v
    assert client.get("/api/pages", params={"id": "h:novel", "book": 1}).json()["v"] != book_v


def test_resizing_the_window_lays_the_reader_out_once_for_each_frame():
    """Every resize event, which a window's edge dragged sends at each step, laid the reader out at once, its pages and
    strip too. Now that work waits for the next frame, once however many events came, and once more a moment later if
    the size changed since."""
    script = """
const handlers = {}, frames = [], timers = [];
const addEventListener = (ev, f) => { handlers[ev] = f; };
const requestAnimationFrame = f => frames.push(f), cancelAnimationFrame = () => {};
const setTimeout = f => timers.push(f), clearTimeout = () => {};
const reader = { hidden: false, clientWidth: 900, clientHeight: 700 }, $ = id => id === "reader" ? reader : { hidden: true };
const R = { w: 900, h: 700, top: 0 }, RD = { pages: 4, n: 2 };
let laid = 0, hidden = 0;
const dock = () => laid++, turnTo = () => {}, hideLook = () => hidden++;
/* part 1 */
for (let i = 0; i < 10; i++) handlers.resize();
const queued = frames.length;
frames.splice(0).forEach(f => f());
const out = { queued, laid, hidden };
timers.at(-1)();  // a moment later: the same size, nothing again
out.after = laid;
reader.clientWidth = 1000;
handlers.resize(); timers.at(-1)();
out.changed = laid;
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"// Turned, or full screen: the new size, once for each frame", r"for \(const ev of \[\"fullscreenchange\""))
    assert out == {"queued": 1, "laid": 1, "hidden": 10, "after": 1, "changed": 2}


def test_end_on_an_e_book_goes_to_its_very_end_and_is_only_looking():
    """For a book with its pages' pictures in the strip, End (the key, the button, a tap on the last tile) went to the
    start of the last tile's stretch: "8 of 9 · 89%", and one or two more turns to the end. The last tile goes to the
    book's end now, as one without the pictures did already; it's still only looking, not reading to the end."""
    script = """
const went = [], RD = { book: true, jumped: false, view: { goToFraction: f => went.push(["fraction", f]), renderer: { goTo: t => went.push(["part", t.index, t.anchor]) } } };
const $ = () => ({ hidden: true }), showBookmarkPick = () => {}, turnTo = () => {}, reelTo = () => {};
let sized = { pages: 9, counts: [1, 4, 4], before: [0, 1, 5] };
const reelTotal = () => sized ? sized.pages : 9, bookPages = () => sized, fixedLayout = () => false;
/* part 1 */
for (const n of [8, 7, 0, 99]) reelJump(n);
const jumped = RD.jumped;
sized = null;
reelJump(8); reelJump(3);
process.stdout.write(JSON.stringify({ went, jumped }));
"""
    out = in_the_page(script, (r"function reelJump\(n\)", r"\$\(\"rtiles\"\)\.addEventListener\(\"scroll\""))
    assert out["went"] == [["fraction", 1], ["part", 2, 0.5], ["part", 0, 0], ["fraction", 1], ["fraction", 1], ["fraction", 3.5 / 9]]
    assert out["jumped"] is True


def test_end_stays_only_looking_until_the_book_is_there():
    """A jump into another part of a book tells of two places, the part's start and then where it went, and the first
    used up the jump's "only looking": End then marked the book finished, as reading to its end would. A jump from the
    strip is only looking now until the book is there; a turn after that is reading again."""
    script = """
let finished = 0, asked = 0, arrive;
const RD = { book: true, it: { id: "t" }, jumped: false, landing: null, storyEnd: 2, view: { isFixedLayout: false } };
RD.view.goToFraction = () => new Promise(done => arrive = done);
const els = {}, $ = id => els[id] || (els[id] = { hidden: true, textContent: "" });
const BS = { line: "page" }, localStorage = { setItem() {} }, keepLocalPlace = () => {}, finishedHere = () => finished++, askFinished = () => asked++;
for (const name of ["hideLook", "showBookmarkPick", "linkNoteGone", "bookLine", "bookmarkButton", "reelLabel", "turnTo", "reelTo"]) globalThis[name] = () => {};
const placeMoved = () => true, fixedLayout = () => false, partsShown = () => [], reelTotal = () => 9, bookPages = () => null;
/* part 1 */
/* part 2 */
(async () => {
  reelJump(8);
  bookPlace({ cfi: "a", fraction: 0.67, section: { current: 2, total: 3 }, location: {} });  // the last part's start
  bookPlace({ cfi: "b", fraction: 1, section: { current: 2, total: 3 }, location: {} });  // and there
  const looking = [finished, asked, RD.jumped];
  arrive();
  await new Promise(done => setTimeout(done, 0));
  const landed = [RD.jumped, RD.landing];
  bookPlace({ cfi: "c", fraction: 1, section: { current: 2, total: 3 }, location: {} });  // a turn: reading to it
  process.stdout.write(JSON.stringify({ looking, landed, read: finished }));
})();
"""
    out = in_the_page(script, (r"function bookPlace\(d\)", r"// Aa: the text's look"),
                      (r"function reelJump\(n\)", r'\$\("rtiles"\)\.addEventListener\("scroll"'))
    assert out == {"looking": [0, 0, True], "landed": [False, None], "read": 1}


def test_a_book_s_download_starts_with_the_question_of_your_place_and_goes_by_its_own_opening():
    """Opening a book waited for your place (up to 1.5 seconds) and for the word on the book's fonts before the file was
    asked for, and the download then read the opening's number and signal from the reader as it started: a book closed
    and another opened meanwhile got the first one's download counted on its loading bar. The file, the place and the
    fonts' word are asked for together now, the download is handed the opening's own number and signal, and a book
    closed meanwhile stops there; after the place from another format, too."""
    script = """
const later = setTimeout;
globalThis.setTimeout = (f, ms) => { const t = later(f, ms); if (ms >= 1000) t.unref(); return t; };  // the fonts' 3 seconds: not waited for here
const RD = { seq: 1, goal: null, loader: null }, events = [];
let placeDone, fontsDone;
const askPlace = () => (events.push("place asked"), new Promise(done => placeDone = () => { events.push("place came"); done(); }));
const fontsKnown = new Promise(done => fontsDone = done);
const fetchBook = (path, name, size, seq, signal) => (events.push(`fetch ${name} ${seq} ${signal.id}`), new Promise(done => setTimeout(() => done("file"), 5)));
const warmBooks = async () => {}, loadingSay = w => events.push(w), loadingAt = () => {}, closeReader = () => events.push("closed");
const BOOK_WHOLE = 12 << 20, ownPlace = () => null, sameFormat = () => true, bookAt = () => null;
const netCheck = async () => {}, NET = {}, toast = w => events.push(w), canReadBooks = () => true;
/* part 1 */
(async () => {
  const opening = openBook({ id: "h:novel" }, ["humble/Ann%20Writer/Lantern%20Tale.epub", "epub", 1000], 1, { id: "first" });
  await new Promise(done => setTimeout(done, 20));
  const before = [...events];
  RD.seq = 2;  // closed meanwhile, and another opened
  placeDone(); fontsDone();
  await opening;
  process.stdout.write(JSON.stringify({ before, after: events.slice(before.length), loader: RD.loader }));
})();
"""
    out = in_the_page(script, (r"async function openBook\(it", r"async function bookStyle\(\)"))
    assert out["before"] == ["fetch Lantern Tale.epub 1 first", "place asked"]  # the file's on its way while the place is asked
    assert out["after"] == ["place came"]  # closed meanwhile: nothing more, not even Opening
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    opener = page[page.index("async function openReader("):page.index("async function firstPage(")]
    assert opener.index("const placed = askPlace(it, true);") < opener.index("/api/pages") < opener.index("await placed;")  # a comic's too
    book = page[page.index("async function openBook("):page.index("async function bookStyle()")]
    assert "if (at && !at.cfi && at.pct) await view.goToFraction(at.pct);  // a place from another format: about there\n    if (RD.seq !== seq) return;" in book
    assert "RD.abort" not in page[page.index("async function fetchBook("):page.index("// A big EPUB's parts, fetched")]
    assert "RD.abort" not in page[page.index("async function rangeLoader("):page.index("// A book's own scripts never run.")]


def test_a_book_that_comes_whole_opens_in_the_page_only_up_to_what_this_device_can_hold():
    """An AZW3, a MOBI or an FB2 comes over whole and is all in memory as it opens: up to 314.6 MB on any device, where
    a phone or a tablet could end the page without a word. Such a book opens in the page up to 80.0 MB on a computer and
    40.0 MB on a phone or tablet now; a bigger one doesn't open the reader at all: its details open, with its file, and
    the page says why, with the file to save at a tap, as its link in the details does (the home-screen app's share sheet
    too). An EPUB read a part at a time has no such limit."""
    script = """
const said = [], did = [];
let TOUCH = { matches: true }, IOS_APP = false, listPlay = "l:1";
const closeReader = () => did.push("closed"), openItem = it => did.push(`details ${it.t}`), saveInApp = a => did.push(`shared ${a.textContent} ${a.dataset.size} ${a.href}`);
const toast = (text, action, ms, word) => said.push({ text, action, ms, word });
const document = { createElement: () => ({ dataset: {}, click() { did.push(`saved ${this.href}`); } }) };
/* part 1 */
/* part 2 */
/* part 3 */
const tale = { t: "Lantern Tale" }, file = (ext, size) => [`humble/Ann%20Writer/Lantern%20Tale.${ext}`, ext, size];
const out = {};
out.phone = ["azw3", "mobi", "fb2", "epub"].map(ext => [ext, [39e6, 40e6, 40e6 + 1, 52.4e6, 300e6].map(n => tooBigWhole(file(ext, n)))]);
TOUCH = { matches: false };
out.computer = ["azw3", "epub"].map(ext => [ext, [52.4e6, 80e6, 85e6, 300e6].map(n => tooBigWhole(file(ext, n)))]);
out.small = tooBigWhole(file("epub", 12e6));  // a small EPUB comes whole too: under either limit
bookTooBig(tale, file("mobi", 85e6), false);
TOUCH = { matches: true };
bookTooBig(tale, file("azw3", 52.4e6), true);  // the next in a series, from the reader
out.said = said.map(({ text, ms, word }) => [text, ms, word]);
out.listPlay = listPlay;  // a list read through stops at the refused book
out.did = did.splice(0);
said[1].action();
IOS_APP = true;
said[1].action();
out.saved = did;
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const sizeWords = ", r"const DL = "), (r"const DL = ", r"// What the sheet shows"),
                      (r"const BOOK_WHOLE = ", r"const BOOK_PATIENCE = "))
    assert out["phone"] == [["azw3", [False, False, True, True, True]], ["mobi", [False, False, True, True, True]],
                            ["fb2", [False, False, True, True, True]], ["epub", [False, False, False, False, False]]]
    assert out["computer"] == [["azw3", [False, False, True, True]], ["epub", [False, False, False, False]]]
    assert out["small"] is False
    assert out["said"] == [
        ["“Lantern Tale” is too big to open in the page on this computer: a MOBI comes whole, up to 80.0 MB here, and this one "
         "is 85.0 MB. Save the file to read it in another app.", 16000, "Save the file"],
        ["“Lantern Tale” is too big to open in the page on a phone or tablet: an AZW3 comes whole, up to 40.0 MB here, and this "
         "one is 52.4 MB. Save the file to read it in another app.", 16000, "Save the file"]]
    assert out["did"] == ["details Lantern Tale", "closed", "details Lantern Tale"]  # the reader, open on the one before, closed first
    assert out["listPlay"] is None
    assert out["saved"] == ["saved ../humble/Ann%20Writer/Lantern%20Tale.azw3?dl=1",
                            "shared Lantern Tale.azw3 52400000 ../humble/Ann%20Writer/Lantern%20Tale.azw3?dl=1"]
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    opener = page[page.index("async function openReader("):page.index("async function firstPage(")]
    assert opener.index("if (wholeRefused(it, inPlace)) return;") < opener.index("$(\"reader\").hidden = false;")
    assert "BOOK_MAX" not in page  # its one use was this limit, 314.6 MB for every device
    assert 'function toast(text, undo, ms = 12000, word = "Undo")' in page and '<button class="btn" id="undo">${esc(word)}</button>' in page


def test_read_again_or_continue_on_a_book_too_big_for_this_device_change_nothing_it_keeps():
    """Read again on a finished book started it over (its mark and place cleared on the server, for every device, with no
    Undo) before the reader found it too big for a phone and refused it. The refusal comes first now, wherever the
    reader is asked for: Read again and Continue only say why and offer the file; a book this device can open goes on
    as ever."""
    script = """
const els = new Map(), $ = id => els.get(id) || els.set(id, { id }).get(id), document = { querySelectorAll: () => [],
  createElement: () => ({ dataset: {}, click() {} }) };
const ME = { by: "ann" }, MY = new Map([["h:done", { done: true, pct: 1 }], ["h:small", { done: true, pct: 1 }], ["h:tale", { pct: 0.63 }]]);
const changed = [], opened = [], said = [];
const markHere = (it, mark) => changed.push(`${mark} ${it.id}`), placeHere = it => changed.push(`place ${it.id}`);
const queue = id => changed.push(`sent ${id}`), seenPlaces = () => ({}), store = () => {};
const openReader = it => opened.push(it.id), openItem = it => opened.push(`details ${it.id}`), closeReader = () => {};
const toast = text => said.push(text), toggleList = () => {}, openListPick = () => {};
const files = { "h:done": ["humble/Moonwake.mobi", "mobi", 90e6], "h:tale": ["humble/Lantern%20Tale.azw3", "azw3", 52.4e6],
                "h:small": ["humble/Small.azw3", "azw3", 2e6] };
const ITEMS = Object.keys(files).map(id => ({ id, t: id.slice(2) }));
const titleById = id => ITEMS.find(it => it.id === id), readFile = it => files[it.id], canRead = it => !!files[it.id], asBook = () => true;
let TOUCH = { matches: true }, IOS_APP = false, listPlay = null;
/* part 1 */
/* part 2 */
/* part 3 */
/* part 4 */
const tap = id => { wireReadRow(titleById(id)); $("readbtn").onclick(); };
const step = () => { const now = { changed: changed.splice(0), opened: opened.splice(0), said: said.length }; said.length = 0; return now; };
const out = {};
tap("h:done"); out.readAgain = step();  // finished, too big for a phone
tap("h:tale"); out.continued = step();  // a place kept from a computer
TOUCH = { matches: false };  // a computer
tap("h:tale"); out.computer = step();
tap("h:small"); out.small = step();
tap("h:done"); out.computerTooBig = step();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const sizeWords = ", r"const DL = "), (r"const DL = ", r"// What the sheet shows"),
                      (r"const BOOK_WHOLE = ", r"const BOOK_PATIENCE = "), (r"function wireReadRow\(it\)", r"// ---- Your reading list"))
    refused = lambda id: {"changed": [], "opened": [f"details {id}"], "said": 1}  # noqa: E731
    assert out["readAgain"] == refused("h:done") and out["continued"] == refused("h:tale")
    assert out["computerTooBig"] == refused("h:done")  # 90.0 MB is over a computer's limit too
    assert out["computer"] == {"changed": [], "opened": ["h:tale"], "said": 0}  # 52.4 MB opens on a computer
    assert out["small"] == {"changed": ["over h:small"], "opened": ["h:small"], "said": 0}  # Read again, as ever


def test_a_book_too_big_for_this_device_says_so_in_its_details_with_a_place_kept_too():
    """Its details say where its file is beside Read, and beside Continue as well: someone who read it on a computer
    and opens it on a phone isn't sent to a Continue that can only refuse there. Where they are stays on the button,
    for another device; a book this device can open says what it always said."""
    script = """
const ME = { by: "ann" }, MY = new Map([["h:done", { done: true }]]), esc = s => String(s);
let TOUCH = { matches: true };
const files = { "h:tale": ["humble/Lantern%20Tale.azw3", "azw3", 52.4e6], "h:done": ["humble/Moonwake.mobi", "mobi", 90e6],
                "h:small": ["humble/Small.azw3", "azw3", 2e6] };
const places = { "h:tale": { pct: 0.63, note: "Moonwake Hill" }, "h:small": { pct: 0.2, note: "The Harbour" } };
const readFile = it => files[it.id], packed = () => false, asBook = () => true, localPlace = it => places[it.id] || null;
const listToggle = () => "", listsButton = () => "";
/* part 1 */
/* part 2 */
const row = id => { const html = readRow({ id }); return [/aria-label="([^"]*)"/.exec(html)[1], (/<span class="kv">([^<]*)<\\/span>/.exec(html) || [])[1]]; };
const out = { phone: ["h:tale", "h:done", "h:small"].map(row) };
TOUCH = { matches: false };
out.computer = ["h:tale", "h:done", "h:small"].map(row);
places["h:tale"] = null;
TOUCH = { matches: true };
out.unread = row("h:tale");
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const BOOK_WHOLE = ", r"const BOOK_PATIENCE = "), (r"function readRow\(it\)", r"// A page's width:"))
    too_big = "Too big to open in the page here: its file is below"
    assert out["phone"] == [["Continue: 63%", too_big], ["Read again", too_big], ["Continue: 20%", "The Harbour"]]
    assert out["computer"] == [["Continue: 63%", "Moonwake Hill"], ["Read again", too_big], ["Continue: 20%", "The Harbour"]]
    assert out["unread"] == ["Read", too_big]


# A big EPUB's loader (rangeLoader) over a made-up book, with zip.js's reading of it in its place: each part's text, what
# is unpacked, and which of the file's pieces are fetched. Each part lies in a piece of its own.
CHAPTERS_PAGE = r"""
const FOLIATE = "/foliate/test", TEXTS = {
  "OEBPS/ch1.xhtml": '<html><head><link rel="stylesheet" href="style.css"/></head><body><p>Lantern Tale</p><img src="images/a.png"/></body></html>',
  "OEBPS/ch2.xhtml": '<html><head><link href="style.css" rel="stylesheet"/></head><body><img src="images/b.png"/></body></html>',
  "OEBPS/ch3.xhtml": '<html><head><link rel="stylesheet" href="style.css"/></head><body><p>Moonwake</p></body></html>',
  "OEBPS/style.css": '@font-face { font-family: Tale; src: url(fonts/tale.woff2) } body { background: url("images/bg.png") }',
};
const NAMES = [...Object.keys(TEXTS), "OEBPS/images/a.png", "OEBPS/images/b.png", "OEBPS/images/bg.png", "OEBPS/fonts/tale.woff2", "OEBPS/images/unused.png"];
const unpacked = {}, fetched = new Set();
let failOnce = new Set(["OEBPS/ch3.xhtml"]);
class TextWriter {}
class BlobWriter {}
const ENTRIES = NAMES.map((filename, i) => ({ filename, offset: i * 256 * 1024, rawFilename: { length: filename.length }, rawExtraField: { length: 0 },
  compressedSize: 1000, uncompressedSize: 1000,
  getData(writer) {
    unpacked[filename] = (unpacked[filename] || 0) + 1;
    if (failOnce.delete(filename)) return Promise.reject(new Error("a piece didn't come"));
    return Promise.resolve(writer instanceof TextWriter ? TEXTS[filename] : "blob");
  } }));
const ZIPJS = { configure() {}, TextWriter, BlobWriter, BlobReader: class { constructor(blob) {} },
                ZipReader: class { constructor(reader) {} async getEntries() { return ENTRIES; } } };
const fetch = async (url, { headers }) => {
  const [from, to] = headers.Range.slice(6).split("-").map(Number);
  for (let n = Math.floor(from / (256 * 1024)); n <= Math.floor(to / (256 * 1024)); n++) fetched.add(NAMES[n]);
  return { status: 206, arrayBuffer: async () => new ArrayBuffer(to - from + 1) };
};
/* the loader */
"""


def test_a_chapter_and_its_style_sheets_are_unpacked_once_and_reach_foliate_as_they_are():
    """A big EPUB's chapter was unpacked three times on its way to the screen (to see what it refers to when it was
    fetched ahead, again when foliate asked for it, and then for foliate), and a style sheet every chapter shares once
    for each, on the page's own thread. What a chapter and a style sheet refer to is read once for the book now, and
    the chapter's text, read for that, goes to foliate as it is. The same pictures, fonts and style sheets are fetched,
    and a part that couldn't be read is read again the next time."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    loader = page[page.index("const BLOCK = "):page.index("// A book's own scripts never run.")]
    assert loader.count("await import(`${FOLIATE}/vendor/zip.js`)") == 1
    script = CHAPTERS_PAGE.replace("/* the loader */", loader.replace("await import(`${FOLIATE}/vendor/zip.js`)", "ZIPJS")) + """
(async () => {
  const tick = () => new Promise(done => setImmediate(done));
  const loader = await rangeLoader("/humble/Ann%20Writer/Lantern%20Tale.epub", 9 * 256 * 1024, () => {}, null);
  await loader.wantChapter("OEBPS/ch1.xhtml");  // the chapter the book opens at (openBook)
  const out = { first: [...fetched].sort() };
  const texts = [await loader.loadText("OEBPS/ch1.xhtml")];  // foliate's
  await loader.wantChapter("OEBPS/ch2.xhtml");  // fetched ahead while the first is read (bookAhead)
  texts.push(await loader.loadText("OEBPS/ch2.xhtml"), await loader.loadText("OEBPS/style.css"));  // the turn into it; foliate's own sheet
  await tick();
  out.texts = texts.map((t, i) => t === TEXTS[["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml", "OEBPS/style.css"][i]]);
  out.unpacked = { ...unpacked };
  out.again = await loader.loadText("OEBPS/ch1.xhtml") === TEXTS["OEBPS/ch1.xhtml"];  // back to the first: read again, its names known
  await tick();
  out.backUnpacked = unpacked["OEBPS/ch1.xhtml"];
  out.failed = await loader.wantChapter("OEBPS/ch3.xhtml").then(() => "read", e => e.message);
  await loader.wantChapter("OEBPS/ch3.xhtml");
  out.retried = unpacked["OEBPS/ch3.xhtml"];
  out.fetched = [...fetched].sort();
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = in_the_page(script)
    assert out["first"] == ["OEBPS/ch1.xhtml", "OEBPS/fonts/tale.woff2", "OEBPS/images/a.png", "OEBPS/images/bg.png", "OEBPS/style.css"]
    assert out["texts"] == [True, True, True]
    assert out["unpacked"] == {"OEBPS/ch1.xhtml": 1, "OEBPS/ch2.xhtml": 1, "OEBPS/style.css": 2}  # the sheet: its names once, and foliate's
    assert out["again"] is True and out["backUnpacked"] == 2
    assert out["failed"] == "a piece didn't come" and out["retried"] == 2
    assert out["fetched"] == sorted(set(out["first"]) | {"OEBPS/ch2.xhtml", "OEBPS/images/b.png", "OEBPS/ch3.xhtml"})  # never the unused picture


def test_a_reader_left_open_across_a_rebuild_that_moved_its_comic_keeps_its_place_and_its_pages(served):
    """A comic moved to another folder, and the catalog built again, gets a new id, while a page left open still has the
    old one: every place it sent was refused ("No such title.") and dropped, and its next page didn't come, until the page
    was reloaded. The server now keeps, from one catalog to the next, which title took each id that went over, and
    answers the old id through it; an id no title took over is refused as before."""
    client, root, comic, _ = served
    key = client.get("/api/session").json()["me"]["key"]
    old = comic["id"]
    assert client.get("/api/pages", params={"id": old}).json()["pages"] == 4  # the server has the catalog as it was
    moved = root / "comics" / "Moonwake Press" / "Moonwake 001.cbz"
    moved.parent.mkdir()
    (root / "comics" / "Moonwake" / "Moonwake 001.cbz").rename(moved)
    catalog.build(root, log=lambda s: None)
    now = next(it for it in Catalog(root).get() if "Moonwake%20Press" in (reader.readable(it) or ""))
    assert now["id"] != old  # its path is in its id
    kept = client.post("/api/me/place", json={"id": old, "at": 2, "pct": 0.5}, headers={"X-Personal-Key": key})
    assert kept.status_code == 200 and kept.json()["ok"]
    places = client.get("/api/me/places").json()["places"]
    assert places[now["id"]]["at"] == 2 and old not in places  # kept under the title as it is now
    assert client.get("/api/page", params={"id": old, "n": 1, "w": 240}).status_code == 200
    assert client.get("/api/pages", params={"id": old}).json()["pages"] == 4
    gone = client.post("/api/me/place", json={"id": "f:0123456789abcdef", "at": 2, "pct": 0.5}, headers={"X-Personal-Key": key})
    assert gone.status_code == 404


def test_moved_ids_follow_a_title_over_two_rebuilds_and_forget_one_that_came_back():
    first = [{"id": "a1", "t": "Lantern Wolf", "f": [["comics/Old/Lantern%20Wolf%201.cbz", "cbz", 9]]}]
    second = [{"id": "a2", "t": "Lantern Wolf", "f": [["comics/New/Lantern%20Wolf%201.cbz", "cbz", 9]]}]
    third = [{"id": "a3", "t": "Lantern Wolf", "f": [["comics/Newer/Lantern%20Wolf%201.cbz", "cbz", 9]]}]
    moved = moved_ids(first, second, {})
    assert moved == {"a1": "a2"}
    moved = moved_ids(second, third, moved)
    assert moved == {"a1": "a3", "a2": "a3"}  # led on to where the title is now
    assert moved_ids(third, first, moved) == {"a2": "a1", "a3": "a1"}  # a1 is back: its own again
    assert moved_ids(third, [], moved) == {}  # nothing took it over
    index = TitleIndex({"a3": third[0]}, {"a1": "a3"})
    assert index.get("a1") is third[0] and index["a1"] is third[0] and "a1" in index
    assert list(index) == ["a3"] and index.get("a9") is None and "a9" not in index  # its keys are the titles' own


def test_the_index_of_every_id_is_kept_until_the_titles_are_laid_anew(library, serving):
    """Every page turn, every picture of the page strip and every change looked its title up in an index of every id
    made anew for it, several times over in some requests: milliseconds each, more as the library grows. The catalog
    keeps the index now, as it keeps the titles' finder, and makes it again only when its titles are laid anew: a new
    data.js, or an edit, which lays every title anew with your change over it. The server's every look-up is that
    one."""
    from hylly import curation

    cat = Catalog(library)
    index = cat.index()
    assert cat.index() is index and index["h:novel"] is next(it for it in cat.get() if it["id"] == "h:novel")
    curation.change(library, "add", "Knitting", [index["h:novel"]], by="test")
    tagged = cat.index()
    assert tagged is not index and tagged["h:novel"]["tg"] == ["Knitting"] and cat.index() is tagged
    data = library / "catalog" / "data.js"
    os.utime(data, (data.stat().st_atime, data.stat().st_mtime + 10))  # a build that wrote it again
    assert cat.index() is not tagged and cat.index()["h:novel"]["tg"] == ["Knitting"]
    srv, client = serving(library)
    served = srv.RequestHandlerClass.catalog
    assert client.get("/api/pages", params={"id": "h:lwolf1"}).status_code == 200
    kept = served.index()
    for _ in range(3):
        assert client.get("/api/pages", params={"id": "h:lwolf1"}).status_code == 200
    assert served.index() is kept


def test_a_cbr_comic_that_7_zip_can_t_unpack_without_its_rar_part_says_what_to_install(tmp_path, monkeypatch):
    """A compressed CBR comic opened where 7-Zip lacks its RAR part, as some systems' packages leave it out, and then
    every page failed with an error of 7-Zip's alone. A page 7-Zip couldn't unpack now asks 7-Zip whether it lacks the
    part, and if it does, and the page is packed, the refusal says what to install, in the doctor's words. A page 7-Zip
    unpacks is read whatever 7-Zip says of itself, and 7-Zip isn't even asked; other trouble is as it was."""
    import types

    import rarfile

    path = tmp_path / "Moonwake 007.cbr"
    path.write_bytes(b"Rar!\x1a\x07\x00" + bytes(100))  # a RAR's first bytes: the rest is the stand-ins below
    monkeypatch.setattr(reader, "_names", lambda path, stamp, kind: ("001.jpg", "002.jpg"))
    packed, unpacks = {"001.jpg": rarfile.RAR_M3, "002.jpg": rarfile.RAR_M3}, {"yes": True}

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def getinfo(self, name):
            return types.SimpleNamespace(compress_type=packed[name], file_redir=None, is_symlink=lambda: False, file_size=10)

        def read(self, info):
            if unpacks["yes"]:
                return b"a page"
            raise rarfile.RarFatalError("Fatal error [2]")  # as 7-Zip without the part ends

    monkeypatch.setattr(archives, "open_rar", lambda path: Archive())
    monkeypatch.setattr(archives, "sevenzip", lambda: str(tmp_path / "7zz"))
    asked = []
    monkeypatch.setattr(archives, "sevenzip_info", lambda tool: (asked.append(tool), ((26, 2), False))[1])  # it says: no RAR part
    monkeypatch.setattr(reader, "_rar_part", {"tool": None, "at": -1e9, "has": None})
    assert reader.count(path) == 2 and reader._rar_read(str(path), "002.jpg") == b"a page"  # it unpacks: it opens
    assert not asked  # and 7-Zip isn't asked about itself
    unpacks["yes"] = False
    with pytest.raises(reader.Unreadable, match="lacks its RAR part") as said:
        reader._rar_read(str(path), "002.jpg")
    assert archives.no_rar_part() in str(said.value) and len(asked) == 1
    assert reader.count(path) == 2  # opening isn't refused: a page is, as it won't come
    packed["002.jpg"] = rarfile.RAR_M0  # stored as it is: the part isn't the trouble
    with pytest.raises(rarfile.RarFatalError):
        reader._rar_read(str(path), "002.jpg")
    packed["002.jpg"] = rarfile.RAR_M3
    monkeypatch.setattr(archives, "sevenzip_info", lambda tool: ((26, 2), True))
    reader._rar_part["at"] = -1e9  # a minute later: one installed meanwhile is seen
    with pytest.raises(rarfile.RarFatalError):
        reader._rar_read(str(path), "002.jpg")  # with the part: some other trouble, as before
    script = """
const RD = { seq: 1, abort: { signal: null }, pageSaid: null }, pageUrl = n => `/api/page?n=${n}`, loadingAt = () => {};
const fetch = async () => ({ ok: false, status: 500, json: async () => ({ ok: false, error: "This comic's pages are packed with RAR." }) });
/* part 1 */
(async () => {
  await firstPage(1, 0);
  const said = RD.pageSaid;
  RD.pageSaid = null; RD.seq = 2;  // closed meanwhile, and another opened
  await firstPage(1, 0);
  process.stdout.write(JSON.stringify([said, RD.pageSaid]));
})();
"""
    said = {"n": 0, "said": "This comic's pages are packed with RAR."}  # the page it was said of, which the reader says it for
    assert in_the_page(script, (r"async function firstPage\(", r"function closeReader\(")) == [said, None]


def test_the_server_s_words_on_a_page_that_won_t_come_are_said_for_that_page_only():
    """The words the server refused a comic's first page with were kept for the whole opening: a later page that
    failed for another reason was said to fail for the first one's. They go with the page they were said of now; any
    other page that doesn't come says only that it couldn't be shown."""
    script = """
const els = {}, $ = id => els[id] || (els[id] = { hidden: id === "rwait", textContent: "" });
$("reader").hidden = false;
const NET = { down: false, slow: 0 }, NET_SLOW = 8000, loadingEnd = () => {}, checkLogin = () => {}, netCheck = () => {};
let shown = [0];
const RD = { book: false, pages: 8, n: 0, pageSaid: { n: 0, said: "This comic's pages are packed with RAR." } };
const page = (src, width) => ({ getAttribute: () => src, complete: true, naturalWidth: width, classList: { contains: () => false } });
const SLOTS = { 0: page("/api/page?n=0", 0), 1: page("/api/page?n=1", 0), 2: page("/api/page?n=2", 600) };
const shownPages = () => shown, slotOf = k => ({ firstElementChild: SLOTS[k] });
/* part 1 */
const said = n => { RD.n = n; shown = [n]; showWait(); return [$("rwait").textContent, $("rwait").hidden]; };
const out = { first: said(0), later: said(1), fine: said(2) };
shown = [1, 0]; showWait(); out.two = $("rwait").textContent;  // two side by side, the one it was said of among them
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const pagesComing = ", r"function viewPick\(\)"))
    assert out["first"] == ["This page couldn't be shown. This comic's pages are packed with RAR.", False]
    assert out["later"] == ["This page couldn't be shown.", False]  # another's failing: not for the first one's reason
    assert out["fine"] == ["Loading…", True]
    assert out["two"] == "This page couldn't be shown. This comic's pages are packed with RAR."


def test_what_a_page_left_open_across_a_rebuild_sends_by_a_moved_id_is_written_under_the_title_s_id_now(served):
    """The server answers an id a rebuild took away by the title that took it over, but two writes still put the very
    id the page sent into the records: the Undo of a few answers (/api/fix with update), which then fixed an id no
    title has, and a list's move before another title. Both now write the title's id as it is now."""
    from hylly import curation

    client, root, comic, other = served
    session = client.get("/api/session").json()
    edit, mine = {"X-Server-Key": session["key"]}, {"X-Personal-Key": session["me"]["key"]}
    old = comic["id"]
    assert client.get("/api/pages", params={"id": old}).json()["pages"] == 4  # the server has the catalog as it was
    moved = root / "comics" / "Moonwake Press" / "Moonwake 001.cbz"
    moved.parent.mkdir()
    (root / "comics" / "Moonwake" / "Moonwake 001.cbz").rename(moved)
    catalog.build(root, log=lambda s: None)
    now = next(it for it in Catalog(root).get() if "Moonwake%20Press" in (reader.readable(it) or ""))["id"]
    undo = client.post("/api/fix", json={"field": "catalog", "value": {f"same:{now}|{other['id']}": {"a": "same"}}, "ids": [old],
                                          "undo": True, "update": True}, headers=edit)
    assert undo.status_code == 200 and undo.json()["changed"] == [now]
    fixed = curation.load(root).fixed
    assert now in fixed and old not in fixed  # never under an id no title has
    for title in (now, other["id"]):  # the reading list as a reloaded page has it: by the ids now
        assert client.post("/api/me/list", json={"list": "reading", "op": "add", "id": title}, headers=mine).status_code == 200
    move = client.post("/api/me/list", json={"list": "reading", "op": "move", "id": other["id"], "before": old}, headers=mine)
    assert move.status_code == 200 and [e["id"] for e in client.get("/api/me/lists").json()["lists"]["reading"]] == [other["id"], now]


def test_every_jump_in_a_book_is_only_looking_until_it_s_there():
    """Contents, a search's hit, a bookmark and the way back from it jump as the strip does, and a jump into another
    chapter tells of two places, its start and then there: the first used up the jump's "only looking", so a hit on the
    last page marked the book finished. They all go by one helper now, as the strip's End does."""
    script = """
let finished = 0, arrive;
const RD = { book: true, it: { id: "t" }, jumped: false, landing: null, storyEnd: null, view: { isFixedLayout: false } };
RD.view.goTo = () => new Promise(done => arrive = done);
const els = {}, $ = id => els[id] || (els[id] = { hidden: true, textContent: "" });
const BS = { line: "page" }, localStorage = { setItem() {} }, keepLocalPlace = () => {}, finishedHere = () => finished++, askFinished = () => {};
for (const name of ["hideLook", "showBookmarkPick", "linkNoteGone", "bookLine", "bookmarkButton", "reelLabel", "showBar", "closeFind"]) globalThis[name] = () => {};
const placeMoved = () => true, fixedLayout = () => false, partsShown = () => [];
/* part 1 */
/* part 2 */
/* part 3 */
(async () => {
  $("rhits").onclick({ target: { closest: () => ({ dataset: { cfi: "epubcfi(/6/8!/4/2)" } }) } });  // a hit on the last page
  bookPlace({ cfi: "a", fraction: 0.778, section: { current: 3, total: 4 }, location: {} });  // the chapter's start
  bookPlace({ cfi: "b", fraction: 1, section: { current: 3, total: 4 }, location: {} });  // and there
  const looking = [finished, RD.jumped];
  arrive();
  await new Promise(done => setTimeout(done, 0));
  bookPlace({ cfi: "c", fraction: 1, section: { current: 3, total: 4 }, location: {} });  // a turn after: reading
  process.stdout.write(JSON.stringify({ looking, read: finished }));
})();
"""
    out = in_the_page(script, (r"function bookPlace\(d\)", r"// Aa: the text's look"),
                      (r"// A book's jump \(the strip, Contents", r'\$\("rtiles"\)\.addEventListener\("scroll"'),
                      (r'\$\("rhits"\)\.onclick = ', r"// A word's meaning, beside it"))
    assert out == {"looking": [0, True], "read": 1}
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert "landing(RD.view.goTo(b.dataset.cfi));" in page  # a search's hit
    assert "if (RD.book) landing(RD.view.goTo(b.dataset.href));" in page  # Contents
    assert "landing(typeof b.at === \"string\" && sameFormat(b, it) ? RD.view.goTo(b.at) : RD.view.goToFraction(b.pct || 0));" in page  # a bookmark
    assert "landing(from.at ? RD.view.goTo(from.at) : RD.view.goToFraction(from.pct));" in page  # and back from it
    assert page.count("RD.landing = ") == 1  # one way


def test_the_guide_reads_the_lists_widths_from_the_page_s_own_rules_and_shows_the_button_when_it_can_t():
    """The guide reads where the reader's bar leaves out its lists button from #rlists's own two rules. It read them from
    the first style of the document, so a style an extension put ahead of the page's, or none to be read, stopped the
    page's script there. They're found wherever they are now, a sheet the page can't read passed over; and none found,
    the guide shows the button, as the worst that can come of it."""
    script = """
const matchMedia = q => ({ media: q, matches: false });
const rule = (text, media) => ({ cssText: text, media: media ? { mediaText: media } : undefined });
const own = { cssRules: [rule(".rbar { gap: 8px; }"),
  rule("@media (max-width: 399px) { .rbar:has(#rsearch:not([hidden])) #rlists { display: none; } }", "(max-width: 399px)"),
  rule("@media (prefers-reduced-motion: reduce) { .rguide .gtrack { transition: none; } }", "(prefers-reduced-motion: reduce)"),
  rule("@media (min-width: 700px) and (max-width: 819px) { .rbar:has(#rsearch:not([hidden])):has(#rfull:not([hidden])) #rlists { display: none; } }",
       "(min-width: 700px) and (max-width: 819px)")] };
const theirs = { cssRules: [rule("body { outline: 0; }")] }, unread = { get cssRules() { throw new Error("SecurityError"); } };
const out = {};
for (const [name, sheets] of [["page's", [own]], ["an extension's first", [theirs, unread, own]], ["none", []], ["unread", [unread]],
                              ["no sheets at all", null]]) {
  globalThis.document = { get styleSheets() { if (!sheets) throw new TypeError("no styleSheets"); return sheets; } };
  /* part 1 */
  out[name] = LISTS_TIGHT.map(t => [t.query.media, t.full]);
}
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const LISTS_TIGHT = ", r"const guideDocked = "))  # each time anew, in the loop's own block
    both = [["(max-width: 399px)", False], ["(min-width: 700px) and (max-width: 819px)", True]]
    assert out == {"page's": both, "an extension's first": both, "none": [], "unread": [], "no sheets at all": []}


def test_the_reader_s_buttons_start_alike_and_only_the_bar_s_own_are_filled():
    """One rule made every button in the reader's bars and panels a filled bar button, 44 px high, and thirteen later
    rules took that back for the page strip's tiles, the finds, the contents' rows, the tabs and the links, with ever
    longer selectors; a bookmark's row in the contents was styled a second time only to win over one of them. One rule
    starts them all alike now, weighing nothing, the bar's own buttons say so (.rb), and no rule of the reader's takes
    a height or a fill back."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    style = re.sub(r"/\*.*?\*/", "", page[page.index("<style>"):page.index("</style>")], flags=re.S)
    start = ":where(.rbar, .rreel, .rtocp, .raap, .rfind, .rlook, .rbmp) button {"
    assert style.count(start) == 1 and ":is(.rbar, .rreel" not in style
    assert re.search(r"\n\.rb \{ height: var\(--tap\); padding: 0 14px; background: color-mix\(in srgb, var\(--r-ink\) 10%, transparent\); \}", style)
    panels = re.compile(r"\.(rbar|rreel|rtocp|rtoclist|raap|rfind|rhits|rlook|rbmp|rbmpick|rbmfoot|rseg|rtile|rlookone)\b")
    undone = [s.strip() for s, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style)
              if panels.search(s) and not s.strip().startswith(":where(") and re.search(r"\bheight: auto|\bbackground: (none|transparent)\b", body)]
    assert undone == [], undone
    assert not re.search(r"\.rtoclist button\.bm|span\.bmwords", style)  # a bookmark's row is the library's, in the reader's colours
    bar = page[page.index('<div id="rbar" class="rbar">'):page.index('<section id="rreel"')]
    own = re.findall(r"<button ([^>]*)>", bar) + [b for b in re.findall(r"<button ([^>]*)>", page) if re.search(r'id="(rstart|rend|rqdone|rtocx|rlookbm)"', b)]
    assert len(own) == 14 and all(re.search(r'class="([\w ]* )?rb\b', b) for b in own), own


def test_reduced_motion_stops_every_one_of_the_guide_s_animations():
    """With less motion asked for, the guide's held word, double tap and two-finger zoom still pulsed: the reduced-motion
    rules named .grip and .ggi::after, less specific than .grip.gtwo or .ggi.ghold::after, which won. Each moving rule
    of the guide's is named again there now by its own selector, which comes later and so wins."""
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    css = page[page.index("/* the first-time guide: over the whole screen"):page.index("/* the contents, from the left */")]
    reduced = css[css.index("@media (prefers-reduced-motion: reduce) {"):]
    reduced = reduced[:reduced.index("\n}\n")]
    rules = [(sel, body) for sel, body in re.findall(r"^([^@{}\n][^{}\n]*?)\s*\{([^{}]*)\}", css.replace(reduced, ""), re.M)]
    moving = {s.strip() for sel, body in rules if re.search(r"\banimation:\s*(?!none)", body) for s in sel.split(",")}
    stopped = {s.strip() for sel, body in re.findall(r"^\s+([^{}\n]+?)\s*\{([^{}]*)\}", reduced, re.M)
               if re.search(r"\banimation:\s*none", body) for s in sel.split(",")}
    assert len(moving) == 8 and moving <= stopped, sorted(moving - stopped)
    assert all(s.startswith(".rguide") for s in stopped)
    assert ".rguide .gtrack { transition: none; }" in reduced and css.index(reduced) > max(css.index(s + " {") for s in moving)


def test_the_guide_promises_no_meanings_without_word_meanings():
    """The guide said "Hold a word to look it up … a box says what it means, from Wiktionary" with [reader]
    word_meanings = false too, where a word held only brings Bookmark. Now it says so, for those logged in; without a
    login there's nothing to hold a word for, and a book's own page of the guide is none."""
    script = """
let MEANINGS = true, ME = { by: "ann" };
const guideMouse = () => true, guideDocked = () => false, guidePaper = () => "", guideBar = () => "", RIBBON = "", TONGUE = "";
const esc = s => s, tongueName = s => s, GI = new Proxy({}, { get: () => "" });
const guideMenu = () => ({ key: "menu" }), guideZoom = () => ({ key: "zoom" }), guideMarks = () => ({ key: "marks" });
/* part 1 */
/* part 2 */
/* part 3 */
const look = () => { const w = guideWord(false), z = guideZones("book", false);
  return { word: w && [w.title, w.body, w.art.includes("Dictionary")], zones: /Double-click a word<small>([^<]*)/.exec(z.art)?.[1] || "",
           one: guidePages("book", "one").map(p => p.key), whole: guidePages("book", "whole").map(p => p.key) }; };
const out = { on: look() };
MEANINGS = false;
out.off = look();
ME = null;
out.alone = look();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function guideZones\(kind, all\)", r"function guideMenu\(kind\)"),
                      (r"function guideWord\(all\)", r"function guideZoom\(all\)"), (r"function guidePages\(", r"function drawGuide\(\)"))
    assert out["on"]["word"][0] == "Double-click a word to look it up" and "Wiktionary" in out["on"]["word"][1] and out["on"]["word"][2]
    assert out["on"]["zones"] == "to see what it means" and out["on"]["one"] == ["word"]
    assert out["off"]["word"][0] == "Double-click a word to bookmark it" and "Bookmark" in out["off"]["word"][1]
    assert not any(w in out["off"]["word"][1] for w in ("means", "Wiktionary", "Wikipedia")) and not out["off"]["word"][2]
    assert out["off"]["zones"] == "to bookmark it"
    assert out["alone"] == {"word": None, "zones": "", "one": [], "whole": ["zones", "menu"]}
    page = Path(serve.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert "if (guidePages(kind, whole).length) openGuide(kind, whole);" in page  # nothing to show: no guide


def test_a_reload_that_moved_nothing_follows_no_ids_and_a_failure_there_doesn_t_stop_the_reload(tmp_path, monkeypatch, capsys):
    import os

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    books = Catalog(tmp_path)
    books.get()
    books.moved = {"h:gone": "h:novel"}  # something moved before
    made = []
    real = matching.Titles
    monkeypatch.setattr(matching, "Titles", lambda *a, **k: made.append(1) or real(*a, **k))
    data = tmp_path / "catalog" / "data.js"
    os.utime(data, (data.stat().st_atime, data.stat().st_mtime + 5))  # read again, as after a rebuild that moved nothing
    books.get()
    assert not made and books.moved == {"h:gone": "h:novel"}
    monkeypatch.setattr("hylly.library.moved_ids", lambda *a: 1 / 0)
    os.utime(data, (data.stat().st_atime, data.stat().st_mtime + 5))
    assert any(it["id"] == "h:novel" for it in books.get()) and books.mtime == data.stat().st_mtime  # reloaded all the same
    assert books.moved == {"h:gone": "h:novel"} and "couldn't be followed" in capsys.readouterr().err


# ---- a link among the unpacked pages (once in test_storage.py) -------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows'")
def test_a_link_among_the_unpacked_pages_is_noticed(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "p1.jpg").write_bytes(b"x")
    assert not reader._has_links(tmp_path / "pages")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "pages" / "out"), str(tmp_path)], check=True, capture_output=True)
    assert reader._has_links(tmp_path / "pages")
