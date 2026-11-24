"""Metadata extraction against small files built on the fly."""

from __future__ import annotations

import io
import struct
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from hylly import meta, rules

from helpers import A_SHOP_S_LOCK, ENCRYPTED, OPF, locked_epub, make_epub, mobi, png, real_mobi


def test_epub(tmp_path):
    p = tmp_path / "dust.epub"
    make_epub(p)
    m = meta.read(p)
    assert (m.title, m.authors, m.publisher, m.year, m.isbn) == ("The Long Dust", ["Marlow Ellery"], "Lantern Press", "2014", "9780000000002")
    assert m.description == "A post-apocalyptic novella ."
    assert m.subjects == ["FICTION / Science Fiction / General"]
    assert (m.series, m.number) == ("Dust and Ember", "1")
    assert m.cover == png()


def test_pdf(tmp_path):
    import pymupdf as fitz

    p = tmp_path / "Microsoft Word - draft.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=450)
    page.insert_text((40, 80), "Field Rules")
    doc.set_metadata({"title": "Microsoft Word - draft.doc", "author": "Administrator"})
    doc.save(p)
    m = meta.read(p)
    assert m.title == "Microsoft Word - draft" and m.authors == [] and m.pages == 1  # junk info ignored; "Microsoft Word" is no author
    assert m.cover[:4] == b"\x89PNG"


def test_series_in_brackets():
    assert meta.series_in_brackets("The Lantern Keeper (Harbor Lights Book 1)") == ("Harbor Lights", "1")
    assert meta.series_in_brackets("The Forecast (N.B. TIDEWATER Book 317)") == ("N.B. TIDEWATER", "317")
    assert meta.series_in_brackets("Moby-Dick (Harbor Critical Editions)") == ("", "")
    assert meta.series_in_brackets("The Lantern Keeper (Harbor Lights Book 0.5)") == ("Harbor Lights", "0.5")  # not ".5"
    assert meta.series_in_brackets("The Lantern Keeper (Harbor Lights Book 007)") == ("Harbor Lights", "7")


def test_junk_pdf_titles():
    for junk in (".docDocument7", "*0923_ch00_FINAL", "<A1B2C3D4E5F60718>", r"\\SERVER\share\Scans3", "draft.dvi",
                 "cover", "Cover - Pocket Guide", "Front.psd", "(2)__", "Untitled-1", "test", "00 1234_FM_Intro",
                 "http://www.example.com/library/volume_ii.htm", "file:///C|/Scans/Legal.htm",
                 r"Z:\Games\Some Game\graph\world_map.jpg", "Slides_of_a_Talk.ppt", "XY_book.indb",
                 "Adobe Photoshop PDF", "ACDSee PDF Image.", "Full page photo", "Full Page Fax Print", "PowerPoint Presentation",
                 "Microsoft PowerPoint Presentation", "Layout 1", "Page 12"):
        assert not meta.good_title(junk), junk
    for good in ("Book of Small Poems", '"Hello, World!" Said the Robot', "2084: A Novel", "Covered Bridges",
                 "Test-Driven Gardening", "Monthly Games Magazine", "Adobe Photoshop PDF Workflows", "Layout 1: A Primer",
                 "Page 12 and Other Tales", "Adobe Illustrator: A Course"):
        assert meta.good_title(good), good
    assert meta.junk_author("test") and meta.junk_author("Administrator") and not meta.junk_author("Testa Rossa")


def test_cbz_with_comicinfo(tmp_path):
    p = tmp_path / "lw1.cbz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("page10.jpg", png((0, 0, 255)))
        z.writestr("page2.jpg", png((0, 255, 0)))
        z.writestr("ComicInfo.xml", "<ComicInfo><Series>Lantern Wolf</Series><Number>1</Number><Writer>Cy Scribe</Writer>"
                                    "<Summary>The first howl.</Summary><Genre>Horror, Supernatural</Genre></ComicInfo>")
    m = meta.read(p)
    assert (m.series, m.number, m.authors, m.subjects, m.pages) == ("Lantern Wolf", "1", ["Cy Scribe"], ["Horror", "Supernatural"], 2)
    assert m.title == "Lantern Wolf 1" and m.cover == png((0, 255, 0))  # natural order: page2 before page10


# Entities declared inside each other, as a "billion laughs" file has them; this one, of three levels, grows to a thousand.
LAUGHS = """<?xml version="1.0" encoding="{encoding}"?>
<!DOCTYPE package [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
"""


@pytest.mark.parametrize("codec, encoding", [("utf-8", "UTF-8"), ("utf-16", "UTF-16"), ("utf-16-be", "UTF-16")])
def test_a_book_s_xml_that_declares_entities_isn_t_read_however_it_is_written(tmp_path, codec, encoding):
    head = LAUGHS.format(encoding=encoding)
    opf = OPF.replace('<?xml version="1.0"?>\n', head).replace("<dc:title>The Long Dust</dc:title>", "<dc:title>&lol3;</dc:title>")
    book = tmp_path / "Ann Writer - The Long Dust.epub"
    make_epub(book, opf=opf.encode(codec))  # UTF-16 with its byte order mark, and without
    m = meta.read(book)
    assert (m.title, m.source) == ("The Long Dust", "unreadable") and "lol" not in m.description  # from its name instead
    comic = tmp_path / "Lantern Wolf 2.cbz"
    with zipfile.ZipFile(comic, "w") as z:
        z.writestr("ComicInfo.xml", (head + "<ComicInfo><Series>&lol3;</Series></ComicInfo>").encode(codec))
        z.writestr("01.png", png())
    assert "lol" not in meta.read(comic).series
    with pytest.raises(ET.ParseError):
        meta.xml_of((head + "<package/>").encode(codec))


def test_an_old_epub_s_doctype_and_the_usual_encodings_still_read(tmp_path):
    doctype = '<!DOCTYPE package PUBLIC "-//Moonwake Press//DTD Package 1.0//EN" "package.dtd">\n'
    book = tmp_path / "dust.epub"
    make_epub(book, opf=OPF.replace('<?xml version="1.0"?>\n', '<?xml version="1.0"?>\n' + doctype))
    assert meta.read(book).title == "The Long Dust" and meta.cant_open(book) == ""
    for codec, declared in (("utf-8", "UTF-8"), ("utf-8-sig", "UTF-8"), ("utf-16", "UTF-16"), ("cp1252", "windows-1252"),
                            ("latin-1", "ISO-8859-1")):
        text = f'<?xml version="1.0" encoding="{declared}"?>\n<ComicInfo><Title>Café Noir</Title></ComicInfo>'
        assert meta.xml_of(text.encode(codec)).find("Title").text == "Café Noir", codec
    assert meta.xml_of("<ComicInfo><Title>Café Noir</Title></ComicInfo>".encode()).find("Title").text == "Café Noir"
    with pytest.raises(ET.ParseError):  # not what it says it is
        meta.xml_of('<?xml version="1.0" encoding="UTF-8"?><ComicInfo><Title>Caf\xe9</Title></ComicInfo>'.encode("latin-1"))


def test_only_an_entity_declared_in_the_doctype_is_refused_not_one_a_comment_or_a_description_names():
    named = "<!ENTITY nbsp \"&#160;\">"
    comment = f'<?xml version="1.0"?>\n<!-- made with {named} in mind -->\n<ComicInfo><Title>Café Noir</Title></ComicInfo>'
    cdata = f"<ComicInfo><Title>Café Noir</Title><Summary><![CDATA[Ann Writer on {named}]]></Summary></ComicInfo>"
    declared = f'<!DOCTYPE ComicInfo [ {named} ]>\n<ComicInfo><Title>Caf&nbsp;Noir</Title></ComicInfo>'
    hidden = f'<!DOCTYPE ComicInfo [ <!-- ]> --> {named} ]>\n<ComicInfo/>'  # where a ]> in a comment isn't its end
    assert meta.xml_of(comment.encode()).find("Title").text == "Café Noir"
    assert named in meta.xml_of(cdata.encode()).find("Summary").text
    for refused in (declared, hidden):
        with pytest.raises(ET.ParseError, match="entity declared"):
            meta.xml_of(refused.encode())


def test_a_container_whose_comment_names_an_entity_isn_t_damaged(tmp_path):
    book = tmp_path / "dust.epub"
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<!-- no <!ENTITY here --><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", OPF)
    assert meta.cant_open(book) == "" and meta.read(book).title == "The Long Dust"


def test_mobi(tmp_path):
    p = tmp_path / "x.mobi"
    p.write_bytes(mobi())
    m = meta.read(p)
    assert (m.title, m.authors, m.description, m.cover) == ("The Silent Orbit", ["Ann Writer"], "Mara, a pilot.", b"COVERIMG")


def test_a_mobi_record_of_size_0_or_a_count_of_billions_ends_the_reading_at_once(tmp_path, monkeypatch):
    """Each EXTH record says its own size, and the header how many there are: neither is taken on trust."""
    calls = []

    def unpack(fmt, buffer):  # counted, and stopped long before a reading that never steps on would end by itself
        calls.append(fmt)
        if len(calls) > 1000:
            raise RuntimeError("still reading")
        return struct.unpack(fmt, buffer)

    monkeypatch.setattr(meta, "struct", SimpleNamespace(unpack=unpack))
    book = tmp_path / "Ann Writer - The Silent Orbit.mobi"
    for exth in ([(100, b"", 0)], [(999, b"", 8)] * 3):  # an author of size 0; three empty records, said to be billions
        book.write_bytes(mobi(exth=exth, claimed=0xFFFFFFFF))
        calls.clear()
        m = meta.read(book)
        assert (m.source, m.title, m.authors) == ("mobi", "Short name", ["Ann Writer"]) and len(calls) < 100, exth


def test_only_a_mobi_s_first_record_and_its_cover_are_read():
    class Counted(io.BytesIO):
        given = 0

        def read(self, size=-1):
            data = super().read(size)
            self.given += len(data)
            return data

    book = mobi(text=bytes(1 << 20))  # a megabyte of text before the cover, which says nothing of the book
    fh = Counted(book)
    m = meta.read_mobi(fh, len(book))
    assert (m.title, m.authors, m.cover) == ("The Silent Orbit", ["Ann Writer"], b"COVERIMG") and fh.given < 10_000


def test_a_mobi_inside_a_zip_package_is_read_as_it_unpacks_not_whole(tmp_path, monkeypatch):
    from hylly import archives

    package = tmp_path / "Moonwake Press - Orbit Package.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("The Silent Orbit.mobi", mobi(text=bytes(1 << 20)))
    whole, real = [], archives.read_member
    monkeypatch.setattr(archives, "read_member", lambda z, name, limit: whole.append(name) or real(z, name, limit))
    m = meta.read(package)
    assert (m.source, m.title, m.authors, m.cover) == ("mobi", "The Silent Orbit", ["Ann Writer"], b"COVERIMG")
    assert whole == []


@pytest.mark.parametrize("packed", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_a_mobi_packed_slowly_or_greedily_inside_a_zip_isn_t_unpacked(tmp_path, packed):
    """Stored or deflated only, as everywhere a book is read from a ZIP: BZIP2 unpacks slowly, LZMA takes the memory
    the file asks for."""
    package = tmp_path / "Ann Writer - The Silent Orbit.zip"
    with zipfile.ZipFile(package, "w", packed) as z:
        z.writestr("The Silent Orbit.mobi", mobi())
    m = meta.read(package)
    assert (m.source, m.cover, m.not_now) == ("unreadable", None, False)


def test_a_book_inside_a_package_is_unpacked_to_a_file_of_its_own_and_read_from_there(tmp_path, monkeypatch):
    """Not whole into memory, as each of a build's readers may hold one at once, up to a gigabyte: an EPUB is read
    from its own file, and a PDF's file is what the PDF worker is given, not its bytes. Named after the package, in the
    folder given, and gone once read."""
    from hylly import archives, pdfworker

    inner = tmp_path / "inner.epub"
    make_epub(inner)
    package = tmp_path / "Moonwake Press - Orbit Package.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(inner, "The Long Dust.epub")
    folder = tmp_path / "unpacking"
    whole, real = [], archives.read_member
    monkeypatch.setattr(archives, "read_member", lambda z, name, limit: whole.append(name) or real(z, name, limit))
    m = meta.read(package, unpack_in=folder)
    assert (m.source, m.title, m.cover) == ("epub", "The Long Dust", png()) and "The Long Dust.epub" not in whole
    assert list(folder.iterdir()) == []
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("The Long Dust.pdf", b"%PDF-1.7 made up")
    given = []

    def details(path, stamp, width, data=None):
        given.append((Path(path), Path(path).read_bytes(), stamp, data))
        return {}, 4, b"COVER"
    monkeypatch.setattr(pdfworker, "details", details)
    m = meta.read(package, unpack_in=folder)
    ((path, held, stamp, data),) = given
    assert (m.source, m.pages, m.cover, held, data) == ("pdf", 4, b"COVER", b"%PDF-1.7 made up", None)
    assert stamp == (package.stat().st_mtime_ns, package.stat().st_size)  # the package's, as before
    assert path.parent == folder and path.name.startswith("Moonwake_Press_-_Orbit_Package-")
    assert path.name.endswith(".pdf" + meta.UNPACKED) and list(folder.iterdir()) == []


def test_a_package_s_book_is_never_unpacked_through_a_link_in_the_folder_s_place(tmp_path):
    """It may lead anywhere, out of the library: refused, as trouble of the moment (not_now), so that the book is read
    again once the folder is a folder, and nothing is written where the link leads."""
    inner = tmp_path / "inner.epub"
    make_epub(inner)
    package = tmp_path / "Ann Writer - The Long Dust.zip"
    with zipfile.ZipFile(package, "w") as z:
        z.write(inner, "The Long Dust.epub")
    elsewhere, folder = tmp_path / "elsewhere", tmp_path / "unpacking"
    elsewhere.mkdir()
    try:
        folder.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("this computer won't make links")
    m = meta.read(package, unpack_in=folder)
    assert (m.source, m.not_now) == ("unreadable", True) and list(elsewhere.iterdir()) == []


def test_a_package_s_book_bigger_than_it_says_is_refused_as_it_unpacks_and_leaves_nothing(tmp_path, monkeypatch):
    from hylly import archives

    inner = tmp_path / "inner.epub"
    make_epub(inner)
    package = tmp_path / "Ann Writer - The Long Dust.zip"
    with zipfile.ZipFile(package, "w") as z:
        z.write(inner, "The Long Dust.epub")
    monkeypatch.setattr(meta, "MAX_PACKED_BOOK", inner.stat().st_size - 1)
    monkeypatch.setattr(archives, "not_a_bomb", lambda info, limit: None)  # as if its header said less than it holds
    folder = tmp_path / "unpacking"
    m = meta.read(package, unpack_in=folder)
    assert (m.source, m.not_now, m.title) == ("unreadable", False, "The Long Dust") and list(folder.iterdir()) == []


def test_storybundle_package_reads_the_epub_inside(tmp_path):
    inner = tmp_path / "inner.epub"
    make_epub(inner)
    p = tmp_path / "Arcade_Worlds_-_Ann_Writer.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.write(inner, "Arcade Worlds - Ann Writer.epub")
        z.writestr("Arcade Worlds - Ann Writer.mobi", b"not read")
    assert meta.read(p).title == "The Long Dust"


def test_the_book_read_inside_a_package_is_the_one_its_kind_is_kept_in(tmp_path):
    """Of the books in a ZIP package, the one read for its details goes by the settings' order, as a download picks a
    title's file and the catalog its main file ([download], settings.pick_file): a book's EPUB first, an RPG book's PDF,
    and with MOBI first on the e-reader's list, the MOBI."""
    from hylly import paths, settings

    inner = tmp_path / "inner.epub"
    make_epub(inner)
    p = tmp_path / "Ann Writer - The Silent Orbit.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("The Silent Orbit.mobi", mobi())
        z.write(inner, "The Silent Orbit.epub")
        z.writestr("The Silent Orbit.pdf", b"not a PDF")
    assert meta.read(p).title == "The Long Dust" == meta.read(p, "comic").title  # a comic's list: of PDF and EPUB, the larger
    assert meta.read(p, "rpg").source == "unreadable"  # the PDF, which is none
    paths.settings_file().write_text('[download]\nereader = ["mobi", "epub"]\n', encoding="utf-8")
    settings.reload()  # as the next command reads them
    assert (meta.read(p).source, meta.read(p).title) == ("mobi", "The Silent Orbit")


def test_a_zip_of_pictures_is_read_as_its_pages(tmp_path):
    """Its pages were read with what read_package imported for a book only: every ZIP of pictures read as unreadable,
    with no cover."""
    p = tmp_path / "Lantern Wolf 001.zip"
    with zipfile.ZipFile(p, "w") as z:
        for n in range(10):
            z.writestr(f"{n:02}.png", png())
    m = meta.read(p)
    assert m.source != "unreadable" and m.cover and m.title == "Lantern Wolf 001"


def test_unreadable_file_falls_back_to_the_name(tmp_path):
    p = tmp_path / "Opening_the_Box_-_Ann_Writer.epub"
    p.write_bytes(b"not a zip")
    m = meta.read(p)
    assert (m.title, m.authors, m.source) == ("Opening the Box", ["Ann Writer"], "unreadable") and not m.not_now


@pytest.mark.parametrize("name", ["Ann Writer - The Long Dust.epub", "Ann Writer - The Long Dust.mobi"])
def test_a_file_gone_or_held_just_then_isn_t_called_unreadable_for_good(tmp_path, monkeypatch, name):
    book, reader = tmp_path / name, "read_mobi" if name.endswith(".mobi") else "read_epub"
    m = meta.read(book)  # gone since the folder was listed
    assert m.not_now and (m.source, m.title, m.authors) == ("unreadable", "The Long Dust", ["Ann Writer"])
    if reader == "read_mobi":
        book.write_bytes(mobi())
    else:
        make_epub(book)
    for trouble in (OSError(5, "Input/output error"), PermissionError(13, "Held by another program"), MemoryError()):
        def held(*args, trouble=trouble):  # a share's stall, a file another program holds, a computer short of memory
            raise trouble
        monkeypatch.setattr(meta, reader, held)
        assert meta.read(book).not_now, trouble


def test_an_epub_that_can_t_be_read_just_then_isn_t_damaged_though_zipfile_says_so(tmp_path, monkeypatch):
    book = tmp_path / "Ann Writer - Held Book.epub"
    make_epub(book)

    def stalled(fp):  # the end of the file, where a ZIP's list of files is, can't be read just then
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(zipfile, "_EndRecData", stalled)
    with pytest.raises(zipfile.BadZipFile, match="not a zip file"):
        zipfile.ZipFile(book)
    assert meta.read(book).not_now
    with pytest.raises(OSError):
        meta.cant_open(book)


@pytest.mark.skipif(sys.platform != "win32", reason="a lock on a file's bytes, as Windows programs hold them")
def test_a_book_another_program_holds_is_read_once_it_lets_go(tmp_path):
    import msvcrt

    book = tmp_path / "Ann Writer - Held Book.epub"
    make_epub(book)
    size = book.stat().st_size
    with open(book, "rb+") as other:
        msvcrt.locking(other.fileno(), msvcrt.LK_NBLCK, size)
        try:
            m = meta.read(book)
            assert m.not_now and (m.title, m.authors) == ("Held Book", ["Ann Writer"])
            with pytest.raises(OSError):
                meta.cant_open(book)
        finally:
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_UNLCK, size)
    m = meta.read(book)
    assert not m.not_now and (m.title, m.authors) == ("The Long Dust", ["Marlow Ellery"]) and meta.cant_open(book) == ""


def test_file_names():
    f = meta.from_filename
    assert f("Herman Melville - Bartleby the Scrivener") == ("Bartleby the Scrivener", ["Herman Melville"])
    assert f("53.01 The Silent Orbit - Ann Writer") == ("53.01 The Silent Orbit", ["Ann Writer"])  # no list's number
    assert f("1.5 Degrees of Moonwake") == ("1.5 Degrees of Moonwake", [])  # nor any number, as the rules come
    assert f("[R_J_Quill]_Gathered_essays_by_R._J._Quill") == ("Gathered essays by R. J. Quill", ["R J Quill"])
    assert f("The Guide to Classic Pixel Puzzles") == ("The Guide to Classic Pixel Puzzles", [])
    assert f("The.Hero's.Journey") == ("The Hero's Journey", [])


def test_a_name_in_a_file_name_has_only_the_name_particles():
    f = meta.from_filename
    assert f("Tour du Monde - Ann Writer") == ("Tour du Monde", ["Ann Writer"])  # "du" is no name's: a title
    assert f("Ann Writer - Tour du Monde") == ("Tour du Monde", ["Ann Writer"])
    assert f("Lions den - Ann Writer") == ("Lions den", ["Ann Writer"])
    assert f("Anna van Berg - The Moon Garden") == ("The Moon Garden", ["Anna van Berg"])
    assert f("Jan ten Berg - The Moon Garden") == ("Jan ten Berg - The Moon Garden", [])  # as always: neither side is sure
    assert f("Anna van den Berg - The Book") == ("Anna van den Berg - The Book", [])


def test_a_name_with_letters_of_any_script_that_has_capitals_is_a_name():
    """A person's name in a file's name is told by Unicode letters, not A to Z alone: Finnish, French and Polish
    names split off as authors, written whole or with each accent as a mark of its own (as a Mac may name a file);
    a word in small letters, a digit, or a script without capitals is still no name's."""
    import unicodedata

    f = meta.from_filename
    assert f("Tuulen talo - Sävel Ränninmäki") == ("Tuulen talo", ["Sävel Ränninmäki"])
    assert f("Éloïse Marchelune - La Maison des lanternes") == ("La Maison des lanternes", ["Éloïse Marchelune"])
    assert f("Latarnik - Zdzisław Księżycki") == ("Latarnik", ["Zdzisław Księżycki"])
    assert f("[Åsa Lyktberg] Solvarv") == ("Solvarv", ["Åsa Lyktberg"])
    decomposed = unicodedata.normalize("NFD", "Tuulen talo - Sävel Ränninmäki")
    assert f(decomposed) == (unicodedata.normalize("NFD", "Tuulen talo"), [unicodedata.normalize("NFD", "Sävel Ränninmäki")])
    for name in ("Éloïse Marchelune", "Zdzisław Księżycki", "Jean-Luc O’Moon", "A. Writer", "Ann K Writer", "Anna van Berg"):
        assert meta.looks_like_name(name), name
    for words in ("Éloïse marchelune", "Ann Writer2", "月の 庭", "Ann", "Kuu ja tähdet"):
        assert not meta.looks_like_name(words), words


def test_a_title_of_capitalised_words_in_any_script_keeps_the_author_the_letters_a_to_z_found():
    """A title of capitalised words with a letter outside A to Z looks like a name too, so where both
    sides of "A - B" could be names, the letters A to Z decide, as they did before: no author found before is lost, and
    a pair both or neither of which is a name in A to Z stays as it was ("Ann Writer - Moon Garden" whole)."""
    f = meta.from_filename
    assert f("Ann Writer - Café Noir") == ("Café Noir", ["Ann Writer"])
    assert f("Les Misères de Moonwake - Ann Writer") == ("Les Misères de Moonwake", ["Ann Writer"])
    assert f("Jo Quill - Lumière Moonwake Tales") == ("Lumière Moonwake Tales", ["Jo Quill"])
    assert f("Ann Writer - Тихая Гавань") == ("Тихая Гавань", ["Ann Writer"])
    assert f("Ann Writer - Moon Garden") == ("Ann Writer - Moon Garden", [])  # neither side sure, as always
    assert f("Tuulen talo - Sävel Ränninmäki") == ("Tuulen talo", ["Sävel Ränninmäki"])  # and such names still found
    assert f("Latarnik - Zdzisław Księżycki") == ("Latarnik", ["Zdzisław Księżycki"])
    assert meta.looks_like_name("Ann Writer", True) and not meta.looks_like_name("Sävel Ränninmäki", True)


def test_a_file_without_authors_in_its_details_takes_the_names_or_none():
    m = meta.complete(meta.Meta(title="The Moon Garden", source="pdf"), "Moon Garden")
    assert (m.authors, m.named) == ([], ["authors"])  # none from the name either: read again from it later
    m = meta.complete(meta.Meta(title="The Moon Garden", authors=["Ann Writer"], source="pdf"), "Moon Garden - Bo Reed")
    assert (m.authors, m.named) == (["Ann Writer"], []) and m.to_dict()["named"] == []  # its own, and the cache says so


def test_series():
    s = meta.series_of
    assert s("The Rust Orchard Vol. 20") == ("The Rust Orchard", "20")
    assert s("Marshal Kane: The Complete Trail Logs Vol. 5") == ("Marshal Kane: The Complete Trail Logs", "5")
    assert s("Marshal Kane Trail Logs 05", trailing_number=True) == ("Marshal Kane Trail Logs", "5")
    assert s("Platform 22") == ("", "")  # only comics take a bare trailing number
    assert s("Neon Rangers 060", trailing_number=True) == ("Neon Rangers", "60")
    assert s("Marshal Kane The Complete Trail Logs 006 (Deluxe Colour Edition)", trailing_number=True) == \
        ("Marshal Kane The Complete Trail Logs", "6")
    assert s("Driftwood (2012)", trailing_number=True) == ("", "")
    assert s("Lantern Wolf Vol. 0.5") == ("Lantern Wolf", "0.5") and s("Neon Rangers #00.5") == ("Neon Rangers", "0.5")
    assert s("Lantern Wolf Vol. 007") == ("Lantern Wolf", "7") and s("Lantern Wolf Vol. 0") == ("Lantern Wolf", "0")
    assert s("Neon Rangers 000", trailing_number=True) == ("Neon Rangers", "0")  # zeros go from the whole part only
    key = rules.current().series_key
    assert key("Marshal Kane: The Complete Trail Logs") == key("Marshal Kane Trail Logs")


def test_a_long_run_in_a_title_is_read_for_its_series_at_once():
    import time

    assert meta.series_of("Lantern Wolf Vol. 1") == ("Lantern Wolf", "1")  # the rules are read now, not in the time below
    for run in (" " * 4000, "-" * 4000, ", " * 2000, " " * 100_000, " " * (meta.SERIES_TITLE - 20)):  # each took a second
        for title in (f"Lantern Wolf{run}x", f"Lantern Wolf{run}Vol. 1"):
            started = time.perf_counter()
            meta.series_of(title, trailing_number=True)  # at every catalog build, for a title the file gives
            assert time.perf_counter() - started < 0.2, (run[:3], len(run))
    assert meta.series_of("Lantern Wolf" + " " * 200 + "Vol. 1") == ("Lantern Wolf", "1")  # a title of a sane length, as ever


def test_text_read_in_the_wrong_encoding_is_put_right():
    wrong_once = lambda s: s.encode("utf-8").decode("cp1252")  # noqa: E731  (UTF-8 read as Windows-1252)
    on_a_mac = lambda s: s.encode("utf-8").decode("mac_roman")  # noqa: E731
    assert meta.mend(wrong_once("Lise Carré")) == "Lise Carré"  # 'Lise CarrÃ©'
    assert meta.mend(on_a_mac(wrong_once("Lise Carré"))) == "Lise Carré"  # 'Lise Carr√É¬©': wrong twice over
    assert meta.mend(wrong_once("It doesn’t glow")) == "It doesn’t glow"  # 'doesnâ€™t'
    assert meta.mend(on_a_mac("doesn’t, “perfect”").replace("\u201a", "'")) == "doesn’t, “perfect”"  # 'doesn'Äôt'
    assert meta.mend("\u00e2\u20ac\u0153Moonwake,\u00e2\u20ac? she said") == "“Moonwake,” she said"  # the lost last byte
    for real in ("café", "Mötley Crüe", "naïve résumé", "À la carte", "L’épée m’a montré", "√2 and ¬", "進撃", "Ånd ça"):
        assert meta.mend(real) == real
    assert meta.clean_text("DIVDIVOnce upon a time it " + wrong_once("wasn’t") + "./divDIV") == "Once upon a time it wasn’t."


def test_an_author_read_in_the_wrong_encoding_is_put_right():
    from hylly import catalog

    assert catalog.tidy_authors(["Lise Carr\u00c3\u00a9", "Ann Writer"]) == ["Lise Carré", "Ann Writer"]


# ---- e-books that can't be opened ----------------------------------------------------------------------------


def test_an_epub_with_nothing_wrong_can_be_opened(tmp_path):
    make_epub(tmp_path / "plain.epub")
    assert meta.cant_open(tmp_path / "plain.epub") == ""


def test_an_epub_whose_text_is_encrypted_is_locked_though_its_details_read_fine(tmp_path):
    book = locked_epub(tmp_path / "locked.epub")
    assert meta.cant_open(book) == meta.LOCKED
    assert meta.read(book).title == "The Long Dust"


@pytest.mark.parametrize("method, uri", [("http://www.idpf.org/2008/embedding", "OEBPS/fonts/a.otf"),
                                         ("http://ns.adobe.com/pdf/enc#RC", "OEBPS/fonts/a.obf"),
                                         (A_SHOP_S_LOCK, "OEBPS/fonts/A.TTF")])
def test_a_book_s_own_fonts_being_encrypted_locks_nothing(tmp_path, method, uri):
    assert meta.cant_open(locked_epub(tmp_path / "fonts.epub", ENCRYPTED % (method, uri))) == ""


@pytest.mark.parametrize("listing", ["this isn't XML", "<encryption/>"])
def test_a_list_of_encrypted_files_that_says_nothing_locks_nothing(tmp_path, listing):
    assert meta.cant_open(locked_epub(tmp_path / "odd.epub", listing)) == ""


def test_an_epub_that_isn_t_one_is_damaged(tmp_path):
    for name, content in (("empty.epub", b""), ("zeros.epub", bytes(4096)), ("cut.epub", b"PK\x03\x04 and no more")):
        (tmp_path / name).write_bytes(content)
        assert meta.cant_open(tmp_path / name) == meta.DAMAGED, name
    with zipfile.ZipFile(tmp_path / "bare.epub", "w") as z:
        z.writestr("chapter.html", "<p>")
    assert meta.cant_open(tmp_path / "bare.epub") == meta.DAMAGED  # nothing says where the book is
    with zipfile.ZipFile(tmp_path / "lost.epub", "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="gone.opf"/></rootfiles></container>')
    assert meta.cant_open(tmp_path / "lost.epub") == meta.DAMAGED  # and what it says isn't there
    with zipfile.ZipFile(tmp_path / "bad.epub", "w") as z:
        z.writestr("META-INF/container.xml", "this isn't XML")
    assert meta.cant_open(tmp_path / "bad.epub") == meta.DAMAGED


@pytest.mark.parametrize("name", ["book.mobi", "book.azw3", "book.AZW", "book.prc"])
def test_a_mobi_by_how_it_says_it_is(tmp_path, name):
    book = tmp_path / name
    book.write_bytes(real_mobi())
    assert meta.cant_open(book) == ""
    book.write_bytes(real_mobi(encryption=2))
    assert meta.cant_open(book) == meta.LOCKED


def test_a_mobi_with_its_start_gone_or_cut_short_is_damaged(tmp_path):
    book = tmp_path / "book.mobi"
    whole = real_mobi()
    for content in (b"", b"x", bytes(4096), bytes(17000) + whole[17000:], whole[:60], whole[:90]):
        book.write_bytes(content)
        assert meta.cant_open(book) == meta.DAMAGED, content[:20]


def test_a_file_that_can_t_be_read_just_now_says_nothing_about_the_file(tmp_path):
    for name in ("gone.epub", "gone.mobi"):
        with pytest.raises(OSError):
            meta.cant_open(tmp_path / name)


def test_a_file_s_language_is_its_own_word_else_its_name_s_in_words(tmp_path):
    """Compare's language: an EPUB's first dc:language, a CBZ's (or a ZIP of pictures') ComicInfo.xml LanguageISO, each
    said in words with its region dropped; else the language its name names by the rules ([languages]); a code the
    table doesn't know as it is; none, or a file that isn't what its name says, "" or its name's."""
    def epub(name: str, said: str):
        make_epub(tmp_path / name, OPF.replace("<dc:language>en</dc:language>", said))
        return tmp_path / name

    def comic(name: str, info: str | None):
        with zipfile.ZipFile(tmp_path / name, "w") as z:
            z.writestr("01.jpg", png())
            if info is not None:
                z.writestr("ComicInfo.xml", f"<ComicInfo><Series>Lantern Wolf</Series>{info}</ComicInfo>")
        return tmp_path / name

    assert meta.language(epub("dust.epub", "<dc:language>es-ES</dc:language><dc:language>en</dc:language>")) == "Spanish"
    assert meta.language(epub("moonwake.epub", "<dc:language>fre</dc:language>")) == "French"
    assert meta.language(epub("hedge.epub", "<dc:language>tlh</dc:language>")) == "tlh"  # a code it doesn't know
    assert meta.language(epub("Hedge Lore (Deutsch).epub", "")) == "German"  # none of its own: its name's
    assert meta.language(epub("Hedge Lore.epub", "<dc:language>und</dc:language>")) == ""
    assert meta.language(comic("Lantern Wolf 001.cbz", "<LanguageISO>pt_BR</LanguageISO>")) == "Portuguese"
    assert meta.language(comic("Lantern Wolf 002 (Italian).zip", None)) == "Italian"
    assert meta.language(comic("Lantern Wolf 003.cbz", "<LanguageISO></LanguageISO>")) == ""
    (tmp_path / "broken (Polish).epub").write_bytes(b"not a zip at all")
    assert meta.language(tmp_path / "broken (Polish).epub") == "Polish" and meta.language(tmp_path / "gone.epub") == ""
    assert meta.language(tmp_path / "x.pdf", "Moonwake (Japanese).pdf") == "Japanese"  # the name given, not looked inside
    assert [meta.language_name(c) for c in ("ja", "ZH-hant", "nob", "", None, "English", "a\x00b")] == [
        "Japanese", "Chinese", "Norwegian", "", "", "English", "a b"]
