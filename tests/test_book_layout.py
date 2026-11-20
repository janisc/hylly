"""An e-book's pages as the server draws them (pdfworker, with MuPDF): sizes given by the screen, vh and vw, which MuPDF
takes for points, come out as a browser would have them on its page. A cover of 99vh had been a sixth of the page."""

from __future__ import annotations

import io
import struct
import zipfile

import pymupdf
import pytest
from PIL import Image

from hylly import pdfworker
from hylly.pdfworker import BOOK_PAGE

STYLE = "img { max-width: 100%; max-height: 100%; } img#cover_image { height: 99vh; } p.wide { width: 50vw; margin: .5vmin; }"


def picture() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (900, 1200), (40, 60, 200)).save(out, "JPEG")
    return out.getvalue()


def book(path, style=STYLE, words='<p class="wide" style="height: 10vh">Some words.</p>'):
    """A made-up EPUB: a cover page of one picture, and a page of words."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles>'
                   '<rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/package.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier><dc:title>Stone Lantern Stitches</dc:title>'
                   '<dc:language>en</dc:language></metadata><manifest><item id="c" href="cover.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="t" href="text.xhtml" media-type="application/xhtml+xml"/><item id="s" href="style.css" media-type="text/css"/>'
                   '<item id="i" href="cover.jpg" media-type="image/jpeg"/></manifest><spine><itemref idref="c"/><itemref idref="t"/></spine></package>')
        page = '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" type="text/css" href="style.css"/></head><body>{}</body></html>'
        z.writestr("OEBPS/cover.xhtml", page.format('<div><img id="cover_image" src="cover.jpg" alt="Cover"/></div>'))
        z.writestr("OEBPS/text.xhtml", page.format(words))
        z.writestr("OEBPS/style.css", style)
        z.writestr("OEBPS/cover.jpg", picture())


def cover_height(doc) -> float:
    width, height, size = BOOK_PAGE
    doc.layout(width=width, height=height, fontsize=size)
    return doc[0].get_image_info()[0]["bbox"][3] - doc[0].get_image_info()[0]["bbox"][1]


def test_a_cover_sized_by_the_screen_fills_the_page(tmp_path):
    path = tmp_path / "stitches.epub"
    book(path)
    width, height, _ = BOOK_PAGE
    sized = pdfworker.sized_by_the_page(path, width, height)
    assert sized is not None
    with zipfile.ZipFile(io.BytesIO(sized)) as z:
        style, text = z.read("OEBPS/style.css").decode(), z.read("OEBPS/text.xhtml").decode()
        assert "height: 633.60pt" in style and "width: 210.00pt" in style and "margin: 2.10pt" in style  # 99% of 640, 50% of 420, .5% of 420
        assert "height: 64.00pt" in text and z.read("OEBPS/cover.jpg") == picture()
    with pymupdf.open(path) as before, pymupdf.open(stream=sized, filetype="epub") as after:
        assert cover_height(before) < 120  # as MuPDF reads 99vh: 99 points, a sixth of the page
        assert cover_height(after) > 400


def test_a_worker_draws_a_book_sized_by_the_screen_from_its_copy(tmp_path, monkeypatch):
    """Through a worker, as the page strip and a borrower's pages get it (pdfworker._document): the copy sized by the
    page is opened in place of the book, and the cover of 99vh fills its page, not a sixth of it."""
    from hylly import reader

    pool = pdfworker.Pool()
    monkeypatch.setattr(pdfworker, "POOL", pool)
    monkeypatch.setattr(pdfworker, "_stops", {})
    monkeypatch.setattr(pdfworker, "_turns", {})
    path = tmp_path / "stitches.epub"
    book(path)
    try:
        assert reader.count(path) >= 2
        with Image.open(io.BytesIO(reader.page(path, 0, 420)[0])) as im:
            page = im.convert("RGB")
        blue = [y for y in range(page.height) if (lambda r, g, b: b > 150 and r < 100)(*page.getpixel((page.width // 2, y)))]
        assert len(blue) > page.height / 2
    finally:
        pool.close()


def test_a_book_without_screen_sizes_opens_as_it_is(tmp_path):
    path = tmp_path / "plain.epub"
    book(path, style="img { max-width: 100%; } p { margin: 1em; }", words="<p>Some words.</p>")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("OEBPS/text2.xhtml", "<p>The heavy tax is 5vhs</p>")  # a word, not a size
    assert pdfworker.sized_by_the_page(path, *BOOK_PAGE[:2]) is None
    assert pdfworker.sized_by_the_page(tmp_path / "comic.cbz", *BOOK_PAGE[:2]) is None
    (tmp_path / "broken.epub").write_bytes(b"not a zip")
    assert pdfworker.sized_by_the_page(tmp_path / "broken.epub", *BOOK_PAGE[:2]) is None


@pytest.mark.parametrize("text, wanted", [(b"height:99vh", b"height:633.60pt"), (b"width: 12.5VW", b"width: 52.50pt"), (b"top: .5vmax", b"top: 3.20pt"),
                                          (b"x1vh", b"x1vh"), (b"1vhx", b"1vhx"), (b"a.5vh", b"a.5vh")])
def test_only_whole_screen_sizes_change(text, wanted):
    width, height, _ = BOOK_PAGE
    each = {"vh": height, "vw": width, "vmin": min(width, height), "vmax": max(width, height)}
    assert pdfworker.VIEWPORT.sub(lambda m: b"%.2fpt" % (float(m[1]) * each[m[2].lower().decode()] / 100), text) == wanted


def claim(path, sizes: dict[str, int]) -> None:
    """The ZIP's own list of its files changed to say these files unpack to these sizes, as a crafted book may say."""
    data = bytearray(path.read_bytes())
    at = data.find(b"PK\x01\x02")
    while at != -1:
        length, extra, remark = struct.unpack_from("<HHH", data, at + 28)
        name = bytes(data[at + 46:at + 46 + length]).decode()
        if name in sizes:
            struct.pack_into("<I", data, at + 24, sizes[name])
        at = data.find(b"PK\x01\x02", at + 46 + length + extra + remark)
    path.write_bytes(data)


def more_than_it_holds(path):  # twenty files, each saying 30 MB, none of them a bomb on its own: 600 MB together
    with zipfile.ZipFile(path, "a") as z:
        for n in range(20):
            z.writestr(f"OEBPS/extra{n}.bin", b"x" * 10, compress_type=zipfile.ZIP_STORED)
    claim(path, {f"OEBPS/extra{n}.bin": 30 << 20 for n in range(20)})


def a_bomb(path):  # 40 MB of nothing, packed to a few KB
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("OEBPS/nothing.bin", bytes(40 << 20), compress_type=zipfile.ZIP_DEFLATED)


def packed_as_the_reader_doesnt(path):  # a style sheet packed with BZIP2, which unpacks slowly
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("OEBPS/more.css", "p.tall { height: 50vh; }", compress_type=zipfile.ZIP_BZIP2)


@pytest.mark.parametrize("crafted", [more_than_it_holds, a_bomb, packed_as_the_reader_doesnt])
def test_a_book_whose_files_could_take_the_memory_isnt_copied_and_opens_as_it_is(tmp_path, crafted):
    """The copy sized by the page was made of every file of the book, in memory, as long as the book stays open, with
    none of a ZIP reader's checks: a book saying it unpacks to gigabytes was taken at its word. One whose files say
    more than MAX_SIZED together, or has one archives.read_member refuses, isn't copied: it opens as it is."""
    path = tmp_path / "stitches.epub"
    book(path)
    crafted(path)
    assert pdfworker.sized_by_the_page(path, *BOOK_PAGE[:2]) is None
    with pymupdf.open(path) as doc:
        assert doc.is_reflowable and cover_height(doc) > 0
