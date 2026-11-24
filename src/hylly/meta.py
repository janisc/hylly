"""Metadata and a cover from ebook and comic files. Files are only read, never changed.

EPUB: the OPF metadata (title, creators, description, subjects, ISBN, series) and its cover image.
PDF: the document info (junk values filtered out) and the first page rendered as the cover.
CBZ/CBR: ComicInfo.xml when present, and the first page image as the cover.
MOBI/AZW3: the EXTH records of the MOBI header, and its cover image record.
ZIP (StoryBundle's several-formats packages): the EPUB inside, else the PDF.
Whatever is missing falls back to the file name: "Title - Author", "Author - Title", "[Author] Title".
What a name says, and what a file's details say that means nothing, are rules: names.toml and series.toml.
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import html
import io
import os
import posixpath
import re
import secrets
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from xml.parsers import expat

from . import archives, pdfworker, rules, settings
from .archives import Unreadable

ISBN = re.compile(r"(?:97[89][- ]?)?(?:\d[- ]?){9}[\dXx]")
TRAILING_NUMBER = re.compile(r"^(?P<series>.*\D)\s+0*(?P<num>\d{1,4})$")
# The files whose details and cover read() can read, by which of the readers below.
READERS = {".epub": "epub", ".pdf": "pdf", ".cbz": "comic", ".cbr": "comic", ".cb7": "comic", ".mobi": "mobi",
           ".azw3": "mobi", ".azw": "mobi", ".prc": "mobi", ".zip": "package"}
# The files whose reader can give a book's description, series, publisher or ISBN: an EPUB's OPF, a MOBI's EXTH, a
# comic's ComicInfo. read_pdf gives none of them, and a package's are its book's, which may be a PDF.
DETAILED = frozenset(ext for ext, reader in READERS.items() if reader in ("epub", "mobi", "comic"))


def junk_author(name: str) -> bool:
    """An author in a file's details that is no person ("Administrator", "calibre"): [junk] in names.toml."""
    return rules.current().is_junk_author(name)


@dataclass
class Meta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    description: str = ""
    subjects: list[str] = field(default_factory=list)
    publisher: str = ""
    year: str = ""
    isbn: str = ""
    series: str = ""
    number: str = ""
    pages: int = 0
    source: str = ""  # where the fields came from: epub, pdf, comicinfo, mobi, filename
    named: list[str] = field(default_factory=list)  # the fields that came from the file's name: title, authors
    cover: bytes | None = field(default=None, repr=False)
    not_now: bool = field(default=False, repr=False)  # couldn't be read just now, which says nothing lasting about the file

    def to_dict(self) -> dict:
        # named even when empty: each entry of the catalog's cache says what came from the name, if only that nothing did
        d = {k: v for k, v in self.__dict__.items() if k not in ("cover", "not_now") and (v not in ("", [], 0) or k == "named")}
        return d


def passing(error: BaseException | None) -> bool:
    """Whether error is trouble of the moment, which says nothing lasting about the file: no PDF worker, a share's
    stall, a file another program holds, a computer short of memory. Also when a library made its own error of it, as
    zipfile does: "File is not a zip file" when the end of a ZIP can't be read just then."""
    for _ in range(8):  # what led to it, a short chain
        if error is None:
            return False
        if isinstance(error, (pdfworker.NotNow, OSError, MemoryError)):
            return True
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return False


def read(path: Path, kind: str = "book", unpack_in: Path | None = None) -> Meta:
    """Best metadata for one file; never raises (an unreadable file gets file-name metadata, with not_now when it
    couldn't be read just then). kind: the title's, for the book read inside a ZIP package (read_package); unpack_in:
    the folder that book is unpacked into to be read, else this computer's folder for temporary files."""
    reader = READERS.get(path.suffix.lower())
    try:
        if reader == "epub":
            with zipfile.ZipFile(path) as z:
                m = read_epub(z)
        elif reader == "pdf":
            m = read_pdf(path)
        elif reader == "comic":
            m = read_comic(path)
        elif reader == "mobi":
            with open(path, "rb") as fh:
                m = read_mobi(fh, fh.seek(0, io.SEEK_END))
        elif reader == "package":
            m = read_package(path, kind, unpack_in)
        else:
            m = Meta()
    except Exception as e:  # noqa: BLE001 - a broken file must not stop a catalog run. Trouble of the moment (passing)
        m = Meta(source="unreadable", not_now=passing(e))  # isn't kept as the file's own: it's read again next time
    return complete(m, path.stem)


def complete(m: Meta, stem: str) -> Meta:
    """Fill gaps from the file name. (Series come later, from series_of, once the kind is known.) named says which
    came from the name, so that the catalog can read the name again by the rules in effect then."""
    name_title, name_authors = from_filename(stem)
    if not m.title:
        m.title, m.source = name_title, m.source or "filename"
        m.named.append("title")
    if not m.authors:  # none in its details: the name's, or none, read again by the rules then
        m.authors = name_authors
        if "authors" not in m.named:  # a ZIP package's book was completed by its own name first
            m.named.append("authors")
    m.description = clean_text(m.description)
    return m


# ---- a book's XML -------------------------------------------------------------------------

XML_DECLARED = re.compile(rb"""<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z][\w.:-]*)["']""")


def xml_of(data: bytes) -> ET.Element:
    """A book's XML as a tree: an EPUB's container, OPF and list of encrypted files, a comic's ComicInfo. One that
    declares an entity is refused (ET.ParseError, as for one that isn't XML), however it's encoded: entities declared
    inside each other grow a small file into gigabytes of text as it's read, and no book needs its own. A plain
    DOCTYPE, as old EPUBs have, passes, and so do the words <!ENTITY in a comment or a description. The text looked
    at is the text read."""
    text = _xml_text(data)
    if "<!ENTITY" in text and _declares_entity(text):
        raise ET.ParseError("an entity declared in a book's own XML, which could grow beyond any size as it's read")
    return ET.fromstring(text)


class _Declared(Exception):
    pass


class _Begun(Exception):
    pass


def _declares_entity(text: str) -> bool:
    """Whether the DOCTYPE's own list of declarations (its [...]) declares an entity, as the XML reader itself finds
    them: it says so at the declaration, before any entity is used, and the look ends where the document's first
    element begins, which no declaration comes after. A comment or a CDATA that only names one declares none."""
    def declared(*_):
        raise _Declared

    def begun(*_):
        raise _Begun

    look = expat.ParserCreate()
    look.EntityDeclHandler, look.StartElementHandler = declared, begun
    try:
        look.Parse(text, True)
    except _Declared:
        return True
    except (_Begun, expat.ExpatError):  # no declaration before the first element; one that isn't XML fails as it's read
        return False
    return False


def _xml_text(data: bytes) -> str:
    """XML's bytes as text, as the XML standard has a reader tell how they're written: by a byte order mark, else by
    how its first character is written, else by the encoding its declaration names; UTF-8 when none says."""
    if data.startswith(codecs.BOM_UTF8):
        codec = "utf-8-sig"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        codec = "utf-16"
    elif data.startswith((b"<\0", b"\0<")):
        codec = "utf-16-le" if data.startswith(b"<") else "utf-16-be"
    else:
        declared = XML_DECLARED.match(data)
        codec = declared[1].decode("ascii") if declared else "utf-8"
    try:
        return data.decode(codec)
    except (LookupError, UnicodeError) as e:
        raise ET.ParseError(f"not text in the encoding it says ({e})") from e


# ---- EPUB --------------------------------------------------------------------------------

def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def read_epub(z: zipfile.ZipFile) -> Meta:
    container = xml_of(archives.read_member(z, "META-INF/container.xml", MAX_COMICINFO))
    opf_path = container.find(".//{*}rootfile").get("full-path")
    opf = xml_of(archives.read_member(z, opf_path, MAX_COMICINFO))
    md = opf.find("{*}metadata")
    m = Meta(source="epub")
    if md is None:
        return m
    m.title = _text(md.find("{*}title"))
    for c in md.findall("{*}creator"):
        role = next((v for k, v in c.attrib.items() if k.endswith("role")), "aut")
        if role in ("aut", "") and _text(c):
            m.authors.append(_text(c))
    m.description = _text(md.find("{*}description"))
    m.subjects = [_text(s) for s in md.findall("{*}subject") if _text(s)]
    m.publisher = _text(md.find("{*}publisher"))
    m.year = (re.search(r"\d{4}", _text(md.find("{*}date"))) or [""])[0]
    for ident in md.findall("{*}identifier"):
        hit = ISBN.search(_text(ident))
        if hit and ("isbn" in (_text(ident) + str(ident.attrib)).lower() or len(re.sub(r"\D", "", hit[0])) == 13):
            m.isbn = re.sub(r"[^\dXx]", "", hit[0])
            break
    metas = {(e.get("name") or "").lower(): e.get("content") or "" for e in md.findall("{*}meta")}
    m.series = metas.get("calibre:series", "")
    m.number = metas.get("calibre:series_index", "").removesuffix(".0")
    for e in md.findall("{*}meta"):
        if e.get("property") == "belongs-to-collection" and not m.series:
            m.series = _text(e)
    m.cover = _epub_cover(z, opf, opf_path, metas.get("cover"))
    return m


def _epub_cover(z: zipfile.ZipFile, opf, opf_path: str, cover_id: str | None) -> bytes | None:
    base = posixpath.dirname(opf_path)
    items = opf.findall(".//{*}manifest/{*}item")

    def load(href: str) -> bytes | None:
        name = posixpath.normpath(posixpath.join(base, unquote(href))).lstrip("/")
        try:
            return archives.read_member(z, name, MAX_COVER)
        except KeyError:
            return None

    images = [i for i in items if (i.get("media-type") or "").startswith("image/")]
    for i in images:
        if "cover-image" in (i.get("properties") or ""):
            return load(i.get("href"))
    if cover_id:
        for i in images:
            if i.get("id") == cover_id:
                return load(i.get("href"))
    for i in images:
        if "cover" in (i.get("id", "") + i.get("href", "")).lower():
            return load(i.get("href"))
    return load(images[0].get("href")) if images else None


# ---- PDF ---------------------------------------------------------------------------------

def good_title(title: str) -> bool:
    """A document-info title worth showing: not a file name, path, placeholder or code like "(2)__"."""
    return len(title) > 2 and not rules.current().is_junk_title(title) and re.search(r"[^\W\d_]{3}", title) is not None


def split_authors(author: str) -> list[str]:
    """A PDF's author field as names: [names] author_separators."""
    r = rules.current()
    parts = r.author_split.split(author) if r.author_split else [author]
    return [a.strip() for a in parts if a.strip()]


def read_pdf(path: Path, width: int = 480) -> Meta:
    """In a process of its own: a PDF that crashes MuPDF costs the catalog its cover and details, not the whole run."""
    st = path.stat()
    info, pages, cover = pdfworker.details(str(path), (st.st_mtime_ns, st.st_size), width)
    m = Meta(source="pdf", pages=pages, cover=cover or None)
    title, author = info.get("title", "").strip(), info.get("author", "").strip()
    if good_title(title):
        m.title = title
    if author and not junk_author(author):
        m.authors = split_authors(author)
    m.subjects = [s.strip() for s in info.get("keywords", "").split(",") if s.strip()][:10]
    m.year = (re.search(r"(19|20)\d{2}", info.get("creationDate", "")) or [""])[0]
    return m


# ---- comics ------------------------------------------------------------------------------

MAX_COVER = 32 << 20  # a comic's first page, read for its cover; a book's cover
MAX_COMICINFO = 4 << 20  # ComicInfo.xml; an EPUB's OPF, a MOBI's first record: what a book says of itself


def read_comic(path: Path) -> Meta:
    if zipfile.is_zipfile(path):  # many .cbr files are really ZIPs
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            return _comic_meta(names, lambda name, limit: archives.read_member(z, name, limit))
    with open(path, "rb") as fh:
        if not fh.read(4).startswith(b"Rar!"):
            return Meta(source="comic")  # neither a ZIP nor a RAR, whatever its name says
    with archives.open_rar(str(path)) as r:
        return _comic_meta(r.namelist(), lambda name, limit: archives.rar_member(r, name, limit))


def _comic_meta(names: list[str], read_member) -> Meta:
    m = Meta(source="comic")
    info = next((n for n in names if posixpath.basename(n).lower() == "comicinfo.xml"), None)
    if info:
        x = xml_of(read_member(info, MAX_COMICINFO))
        get = lambda tag: _text(x.find(tag))  # noqa: E731
        m.source = "comicinfo"
        m.series, m.number, m.title = get("Series"), get("Number"), get("Title")
        m.authors = [a.strip() for a in get("Writer").split(",") if a.strip()]
        m.description, m.publisher, m.year = get("Summary"), get("Publisher"), get("Year")
        m.subjects = [g.strip() for g in get("Genre").split(",") if g.strip()]
        if m.series and not m.title:
            m.title = f"{m.series} {m.number}".strip()
    pages = archives.comic_pages(names)  # as the reader shows them: the count, and the first for the cover
    m.pages = len(pages)
    if pages:
        m.cover = read_member(pages[0], MAX_COVER)
    return m


# ---- a file's language, as To decide's Compare shows it -----------------------------------

LANGUAGE_NAMES = {  # ISO 639-1's codes, to the language's name in English
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "pl": "Polish",
    "ru": "Russian", "ja": "Japanese", "nl": "Dutch", "fi": "Finnish", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "nb": "Norwegian", "nn": "Norwegian", "zh": "Chinese", "ko": "Korean", "cs": "Czech", "el": "Greek", "et": "Estonian",
    "hu": "Hungarian", "tr": "Turkish", "uk": "Ukrainian", "ca": "Catalan", "la": "Latin",
}
LANGUAGE_THREE = {  # ISO 639-2's codes for the same, both of a language's where it has two, as older EPUBs give them
    "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "ita": "it", "por": "pt", "pol": "pl",
    "rus": "ru", "jpn": "ja", "dut": "nl", "nld": "nl", "fin": "fi", "swe": "sv", "dan": "da", "nor": "no", "nob": "nb",
    "nno": "nn", "chi": "zh", "zho": "zh", "kor": "ko", "cze": "cs", "ces": "cs", "gre": "el", "ell": "el", "est": "et",
    "hun": "hu", "tur": "tr", "ukr": "uk", "cat": "ca", "lat": "la",
}
LANGUAGE_SAID_MOST = 30  # letters of a language a file gives that the table doesn't know, shown as they are
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def language_name(code) -> str:
    """A language as a file gives it, in words: an ISO 639 code (es, spa, es-ES, pt_BR) as the language's name in
    English, its region dropped (Spanish); one the table doesn't know as it is (a name already, or a code of its own),
    on one line and cut short; "" for none, and for und, which says the language isn't known."""
    said = re.sub(r"\s+", " ", CONTROL_CHARACTERS.sub(" ", code if isinstance(code, str) else "")).strip()
    base = re.split(r"[-_]", said, maxsplit=1)[0].lower()
    if not said or base == "und":
        return ""
    return LANGUAGE_NAMES.get(LANGUAGE_THREE.get(base, base), said[:LANGUAGE_SAID_MOST])


def language(path: Path, name: str = "") -> str:
    """The language a book file is in, in words (language_name), where it says one: an EPUB's OPF, its first
    dc:language; a CBZ's, or a ZIP of pictures', ComicInfo.xml, its LanguageISO. Else one its name (name: else the
    file's own) names by the rules ([languages] in kinds.toml: "Lantern Wolf (Spanish).epub", Spanish). "" for none.
    Only what's needed is read (the container, then the OPF; or ComicInfo.xml), each up to MAX_COMICINFO, and it never
    raises: a file that can't be read is taken by its name."""
    said = ""
    suffix = path.suffix.lower()
    if suffix in (".epub", ".cbz", ".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                if suffix == ".epub":
                    container = xml_of(archives.read_member(z, "META-INF/container.xml", MAX_COMICINFO))
                    opf = xml_of(archives.read_member(z, container.find(".//{*}rootfile").get("full-path"), MAX_COMICINFO))
                    said = _text(opf.find("{*}metadata/{*}language"))
                else:
                    info = next((n for n in z.namelist() if posixpath.basename(n).lower() == "comicinfo.xml"), None)
                    said = _text(xml_of(archives.read_member(z, info, MAX_COMICINFO)).find("{*}LanguageISO")) if info else ""
        except Exception:  # noqa: BLE001 - a file that can't be read, or isn't what its name says: its name, then
            said = ""
    return language_name(said) or named_language(name or path.name)


def named_language(name: str) -> str:
    """The language a file's name names by the rules ([languages] in kinds.toml), in words, as language_name says one:
    "Lantern Wolf (Spanish).epub", Spanish; "" for none."""
    stem = PurePosixPath(name or "").stem
    return next((lang.capitalize() for lang, words in rules.current().languages.items() if words.find(stem) is not None), "")


# ---- MOBI / AZW3 -------------------------------------------------------------------------

def read_mobi(fh, size: int) -> Meta:
    """The MOBI file, open as fh and size bytes long: PalmDB records, MOBI header, EXTH records (100 author, 101 publisher,
    103 description, 104 ISBN, 105 subject, 106 date, 201 cover offset, 503 title). Only the list of records, the first
    record and the cover's are read, each up to a limit, and the sizes and counts the file gives are held to what is
    there: a damaged one (a record of size 0, a count of billions) can't keep a catalog build reading or fill its memory."""
    m = Meta(source="mobi")

    def read_at(start: int, length: int) -> bytes:
        fh.seek(start)
        return fh.read(length)

    count = struct.unpack(">H", read_at(76, 2))[0]
    table = read_at(78, 8 * count)
    offsets = [struct.unpack(">I", table[8 * i: 8 * i + 4])[0] for i in range(count)]

    def record(i: int, limit: int) -> bytes:
        start, end = offsets[i], offsets[i + 1] if i + 1 < count else size
        return read_at(start, min(end - start, limit)) if start < end else b""

    rec0 = record(0, MAX_COMICINFO)
    if rec0[16:20] != b"MOBI":
        return m
    header_len, encoding = struct.unpack(">I", rec0[20:24])[0], struct.unpack(">I", rec0[28:32])[0]
    codec = "utf-8" if encoding == 65001 else "cp1252"
    name_off, name_len = struct.unpack(">II", rec0[84:92])
    first_image = struct.unpack(">I", rec0[108:112])[0]
    m.title = rec0[name_off: name_off + name_len].decode(codec, "replace").strip()
    cover_offset = None
    if struct.unpack(">I", rec0[128:132])[0] & 0x40:
        pos = 16 + header_len
        if rec0[pos: pos + 4] == b"EXTH":
            n = min(struct.unpack(">I", rec0[pos + 8: pos + 12])[0], len(rec0) // 8)  # each takes 8 bytes at least
            pos += 12
            for _ in range(n):
                if pos + 8 > len(rec0):
                    break
                kind, length = struct.unpack(">II", rec0[pos: pos + 8])
                if length < 8:  # it would never step on
                    break
                value = rec0[pos + 8: pos + length]
                pos += length
                text = lambda: value.decode(codec, "replace").strip()  # noqa: E731
                if kind == 100:
                    m.authors.append(text())
                elif kind == 101:
                    m.publisher = text()
                elif kind == 103:
                    m.description = text()
                elif kind == 104:
                    m.isbn = re.sub(r"[^\dXx]", "", text())
                elif kind == 105:
                    m.subjects.append(text())
                elif kind == 106:
                    m.year = (re.search(r"\d{4}", text()) or [""])[0]
                elif kind == 201 and len(value) == 4:
                    cover_offset = struct.unpack(">I", value)[0]
                elif kind == 503:
                    m.title = text()
    if cover_offset is not None and first_image + cover_offset < count:
        m.cover = record(first_image + cover_offset, MAX_COVER)
    return m


# ---- e-books that can't be opened ---------------------------------------------------------

LOCKED, DAMAGED = "locked", "damaged"
OPEN_CHECKED = ("epub", "mobi", "azw3", "azw", "prc")  # the formats cant_open can say something about
# What an EPUB's list of encrypted files may hold and anyone can still read: its own fonts, made awkward to copy.
FONT_METHODS = ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC")
FONT_FILES = (".ttf", ".otf", ".woff", ".woff2")


def cant_open(path: Path) -> str:
    """Why no reader can open an e-book, from how the file is put together: LOCKED (the shop that sold it encrypted the
    text, and only that shop's app can read it), DAMAGED (it isn't what its name says, or what every reader needs is
    missing), or "" when nothing says so. A chapter gone bad inside isn't looked for. Raises OSError if the file can't be
    read just now, which says nothing about the file."""
    return _epub_cant_open(path) if path.suffix.lower() == ".epub" else _mobi_cant_open(path)


def _epub_cant_open(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            rootfile = xml_of(archives.read_member(z, "META-INF/container.xml", MAX_COMICINFO)).find(".//{*}rootfile")
            if rootfile is None or rootfile.get("full-path") not in names:
                return DAMAGED
            if "META-INF/encryption.xml" not in names:
                return ""
            try:
                listed = xml_of(archives.read_member(z, "META-INF/encryption.xml", MAX_COMICINFO))
            except (ET.ParseError, Unreadable):  # a list that can't be read says nothing of the book
                return ""
            return LOCKED if any(_locks_text(entry) for entry in listed.findall(".//{*}EncryptedData")) else ""
    except (zipfile.BadZipFile, KeyError, ET.ParseError, Unreadable, zlib.error, struct.error, ValueError, EOFError,
            RuntimeError, NotImplementedError) as e:
        if passing(e):  # zipfile's "not a zip file" for a share's stall, or a file another program holds
            raise OSError(f"{path} can't be read just now") from e
        return DAMAGED


def _locks_text(entry: ET.Element) -> bool:
    """One file in an EPUB's list of encrypted ones, by a method other than the ones for fonts, and not a font itself."""
    method, cipher = entry.find("{*}EncryptionMethod"), entry.find(".//{*}CipherReference")
    if method is None or method.get("Algorithm") in FONT_METHODS:
        return False
    return not (cipher.get("URI") or "").lower().endswith(FONT_FILES) if cipher is not None else True


def _mobi_cant_open(path: Path) -> str:
    """The kind a MOBI file says it is, and in its first record the kind of encryption, none being 0."""
    with open(path, "rb") as fh:
        head = fh.read(82)
        if len(head) < 82 or head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
            return DAMAGED
        fh.seek(struct.unpack(">I", head[78:82])[0])
        record = fh.read(14)
    if len(record) < 14:
        return DAMAGED
    return LOCKED if struct.unpack(">H", record[12:14])[0] else ""


# ---- StoryBundle-style packages ----------------------------------------------------------

MAX_PACKED_BOOK = 1 << 30  # a book inside a ZIP package, read for its details (an EPUB or a PDF, unpacked to a file)
# The ending of a book unpacked from a package (unpacked), after the book's own: Hylly's own for a file it writes, which
# no backup takes (paths.TEMPORARY), as one left by a run that stopped may be big
UNPACKED = ".tmp"


PACKAGE_READS = (".epub", ".pdf", ".mobi", ".azw3")  # the books inside a ZIP package read for its details


def read_package(path: Path, kind: str = "book", unpack_in: Path | None = None) -> Meta:
    """A ZIP holding the same book in several formats: read the one of them its kind is kept in first, as a download
    picks it and as a title's main file is ([download] in the settings: settings.pick_file); for a book the EPUB, else
    the AZW3 or MOBI, else the PDF. An EPUB or a PDF is unpacked into a file of its own in unpack_in (unpacked) and read
    from there, as a book up to a gigabyte held whole in memory, by each of a build's readers at once, could take all a
    small server has; a MOBI is read as it unpacks."""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if "__MACOSX" not in n]
        books = [[i.filename, Path(i.filename).suffix.lower()[1:], i.file_size] for i in z.infolist()
                 if "__MACOSX" not in i.filename and not i.is_dir() and i.filename.lower().endswith(PACKAGE_READS)]
        if books:
            inner = settings.pick_file(kind, books)[0][0]
            ext = Path(inner).suffix.lower()
            if ext == ".epub":
                with unpacked(z, inner, path, unpack_in) as book, zipfile.ZipFile(book) as e:
                    return complete(read_epub(e), Path(inner).stem)
            if ext == ".pdf":
                with unpacked(z, inner, path, unpack_in) as book:
                    st = path.stat()  # the package's, as when the worker was given the book's bytes
                    _, pages, cover = pdfworker.details(str(book), (st.st_mtime_ns, st.st_size), 480)
                return complete(Meta(source="pdf", pages=pages, cover=cover or None), Path(inner).stem)
            info = z.getinfo(inner)  # a MOBI is read as it unpacks, only what read_mobi needs, not whole into memory
            # stored or deflated only, as read_member: BZIP2 unpacks slowly, and LZMA takes the memory the file asks for
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Unreadable("A file inside the archive is packed in a way the reader doesn't open.")
            archives.not_a_bomb(info, MAX_PACKED_BOOK)
            with z.open(info) as fh:
                return complete(read_mobi(fh, info.file_size), Path(inner).stem)
        if archives.comic_pages(names):  # a ZIP of page images is a comic
            return _comic_meta(names, lambda name, limit: archives.read_member(z, name, limit))
    return Meta()


@contextlib.contextmanager
def unpacked(z: zipfile.ZipFile, name: str, package: Path, folder: Path | None = None):
    """One book of a ZIP package (z, the package at package), unpacked into a file of its own in folder, else this
    computer's folder for temporary files, a piece at a time, with read_member's limits: stored or deflated only, and no
    further than MAX_PACKED_BOOK, whatever its header claims. Named after the package, so that a line about it (the PDF
    worker's) says which; gone once it has been read, and a file left by a run that stopped ends in UNPACKED. A folder
    given that is a link is refused (OSError: read again next time), as it may lead anywhere: out of the library, where
    nothing of a build's may be written."""
    info = z.getinfo(name)
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise Unreadable("A file inside the archive is packed in a way the reader doesn't open.")
    archives.not_a_bomb(info, MAX_PACKED_BOOK)
    if folder is not None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)  # never through a link in its place: that one is there already
        if os.path.islink(folder):
            raise OSError(errno.ELOOP, "A link where a package's book is unpacked, which isn't followed", str(folder))
    else:
        folder = Path(tempfile.gettempdir())
        folder.mkdir(parents=True, exist_ok=True)
    # Our own name, not tempfile's, as storage.atomic_write's: on Windows tempfile takes a refusal for a name in use,
    # and tries the next, for ever. Of the package's name only its letters, digits, _ and -: none Windows can't hold.
    named = re.sub(r"[^\w-]+", "_", package.stem)[:40]
    made = folder / f"{named}-{secrets.token_hex(4)}{Path(name).suffix.lower()}{UNPACKED}"
    fd = os.open(made, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as out, z.open(info) as fh:
            left = MAX_PACKED_BOOK
            while piece := fh.read(1 << 20):
                left -= len(piece)
                if left < 0:
                    raise Unreadable("A file inside the archive is far bigger than it says.")
                out.write(piece)
        yield made
    finally:
        with contextlib.suppress(OSError):
            os.unlink(made)


def package_contents(path: Path) -> list[list]:
    """The book files inside a ZIP package as [name, ext, size] ([packages] books in formats.toml), from its table
    of contents alone (the end of the file), so listing a package on a network share is quick."""
    books = rules.current().package_books
    with zipfile.ZipFile(path) as z:
        return [[i.filename, Path(i.filename).suffix.lower()[1:], i.file_size] for i in z.infolist()
                if not i.is_dir() and "__MACOSX" not in i.filename and i.filename.lower().endswith(books)]


def package_pictures(path: Path) -> bool:
    """A ZIP that is pages as pictures (a comic or a scanned book zipped as it was, not named .cbz): nearly all it
    holds, a few notes aside ([packages] ignore), is pictures. From its table of contents alone."""
    ignore = rules.current().package_ignore
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and not n.lower().endswith(ignore)]
    pictures = archives.comic_pages(names)
    return len(pictures) >= 8 and len(pictures) >= 0.8 * len([n for n in names if "__MACOSX" not in n])


# ---- names, series, text -----------------------------------------------------------------

def looks_like_name(s: str, a_to_z: bool = False) -> bool:
    """A person's name, by [names] in names.toml: two to five words, each capitalised, in any script that has capitals,
    or a particle (de, van): Rules.is_name_word. a_to_z: each in the letters A to Z alone, as before any script
    counted."""
    r = rules.current()
    words = s.replace(",", " ").split()
    return (2 <= len(words) <= 5 and all(r.is_name_word(w, a_to_z) for w in words)
            and not ({w.lower() for w in words} & r.not_name_words))


def _tidied(stem: str) -> str:
    """A file's name without what [filename] in names.toml takes out, its _ as spaces."""
    r = rules.current()
    s = r.strip_written.sub("", stem) if r.strip_written else stem  # a strip item with a _, as written: "_retail"
    s = re.sub(r"[_]+", " ", s)
    if r.strip:
        s = r.strip.sub("", s)
    s = r.strip_number(s)  # a list's number, where a rules file of yours has one: "53.01 The Silent Orbit - Ann Writer"
    return re.sub(r"\s+", " ", s).strip(" -.")


def _spaced(name: str) -> str:
    if rules.current().dots_as_spaces and "." in name and " " not in name:  # Names.With.Dots
        return name.replace(".", " ")
    return name


def whole_name(stem: str) -> str:
    """A title from a file's name, whole: no part of it an author (a kind's whose titles go by their folder)."""
    return _spaced(_tidied(stem))


def from_filename(stem: str) -> tuple[str, list[str]]:
    """(title, authors) from a file's name, by [filename] in names.toml."""
    s = _tidied(stem)
    bracket = re.match(r"^\[([^\]]+)\]\s*(.+)$", s)
    if bracket and looks_like_name(bracket[1]):
        return bracket[2].strip(), [bracket[1].strip()]
    if " - " in s:
        left, right = s.split(" - ", 1)
        named = looks_like_name(left), looks_like_name(right)
        if all(named):  # "Ann Writer - Café Noir": a title of capitalised words in any script looks like a name too, so
            named = looks_like_name(left, True), looks_like_name(right, True)  # the letters A to Z decide, as they did
        if named == (False, True):
            return left.strip(), [right.strip()]
        if named == (True, False):
            return right.strip(), [left.strip()]
    return _spaced(s), []


def series_in_brackets(name: str) -> tuple[str, str]:
    """'The Lantern Keeper (Harbor Lights Book 1)' -> ('Harbor Lights', '1'): how some shops name their books' files."""
    hit = rules.current().bracket_series.search(name)
    return (hit["series"].strip(), re.sub(r"^0+(?=\d)", "", hit["num"])) if hit else ("", "")  # "0.5" stays 0.5


SERIES_TITLE = 300  # characters at most in a title read for its series: no "Vol. 3" title is longer, and the patterns
# backtrack over a long run of spaces, commas or dashes (4,000 spaces took a second, at every catalog build)


def series_of(title: str, trailing_number: bool = False) -> tuple[str, str]:
    """'Lantern Wolf Vol. 1' -> ('Lantern Wolf', '1'), by [series] words in series.toml. A bare trailing number
    ('Marshal Kane Trail Logs 05') only counts when trailing_number is set (comics): for novels it would split
    'Platform 22'. A note in brackets after it is ignored: 'The Complete Trail Logs 006 (Deluxe Colour Edition)'."""
    if len(title) > SERIES_TITLE:
        return "", ""
    hit = rules.current().series.match(title) or (
        TRAILING_NUMBER.match(re.sub(r"\s*\([^()]*\)$", "", title)) if trailing_number else None)
    if not hit:
        return "", ""
    series = hit["series"].strip(" ,:-#")
    return (series, re.sub(r"^0+(?=\d)", "", hit["num"])) if len(series) >= 2 else ("", "")  # "007" is 7, "0.5" stays


def clean_text(text: str) -> str:
    text = re.sub(r"<[^<>]+>", " ", html.unescape(text or ""))  # never across a <: a long run of them stays quick
    if "/div" in text:  # tags that lost their angle brackets on the way: 'DIVDIVOnce upon a time./divDIV'
        text = re.sub(r"(?:/div|DIV)+", " ", text)
    return re.sub(r"\s+", " ", mend(text)).strip()[:1500]


# ---- text read in the wrong encoding --------------------------------------------------------

_NOT_ASCII = re.compile(r"[^\x00-\x7f]+")


def _as_1252(text: str) -> bytes:
    """Windows-1252 as a wrong reading left it: its five unused bytes come back as the C1 characters latin-1 gives."""
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 255:
                raise
            out.append(ord(ch))
    return bytes(out)


def _likely(ch: str) -> bool:
    """What a wrongly read text in a library turns back into: accented letters and typographic marks."""
    o = ord(ch)
    return 0xA0 <= o <= 0x24F or 0x2010 <= o <= 0x206F or o in (0x20AC, 0x2122)


def _read_again(run: str) -> str:
    for encode in (_as_1252, lambda s: s.encode("mac_roman")):
        try:
            back = encode(run).decode("utf-8")
        except UnicodeError:
            continue
        if len(back) < len(run) and all(_likely(c) for c in back):
            return back
    return run


def mend(text: str) -> str:
    """UTF-8 that was read as Windows-1252 or Mac Roman, once or twice over, put right: 'CarrÃ©', 'Carr√É¬©',
    'doesnâ€™t' and 'doesn'Äôt' become 'Carré' and 'doesn’t'. Real accents (café, Crüe, l’épée) aren't UTF-8 when
    read that way, or read back as something no text has, so they stay as they are."""
    if not text or text.isascii():
        return text
    text = text.replace("â€?", "”")  # 'â€?': a closing quote whose last byte had no Windows letter
    text = re.sub("'(?=Ä[^\x00-\x7f])", "‚", text)  # Mac Roman's ‚ that a fix of quotes made a plain '
    for _ in range(3):  # read wrong twice over takes two rounds
        again = _NOT_ASCII.sub(lambda m: _read_again(m.group(0)), text)
        if again == text:
            break
        text = again
    return text.replace("‚Ä", "'Ä")  # one that couldn't be put right goes back as it was
