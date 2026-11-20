"""Reading in the library page: a comic's or a PDF's pages one at a time, so a file of any size opens at once.

- **CBZ** is a ZIP archive, whose table of contents is at its end: the server reads that, then only the page asked
  for, never the whole file.
- **CBR** is a RAR archive (or, often, a ZIP named .cbr: the first bytes say which). RAR has no table at the end, so
  listing reads the header of each page (seeking past the pictures); a page is unpacked by 7-Zip.
- **PDF** pages are drawn by MuPDF (PyMuPDF, already used for the catalog), which reads only the parts of the file a
  page needs, in a process of its own (pdfworker.py). Text in them is a picture, so it can't be selected.

Each page is scaled to the width the screen asks for and sent as WebP; the latest are kept in memory, every one on
the server's disk too, and the browser keeps them for a week (the file's time is in the address, so a changed file gets
new ones). The browser says which pages it expects to want next, and those are drawn while nobody waits for one.
- **EPUB, MOBI, FB2** are read in the page itself (foliate-js), except by a borrower, who gets no files: for them
  MuPDF lays the book out as pages of a fixed size, and its pages come as pictures like a PDF's.
"""

from __future__ import annotations

import functools
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
import zlib
from collections import OrderedDict, deque
from pathlib import Path, PurePosixPath

from . import archives, pdfworker, rules, settings
from .archives import Unreadable  # here too, where the rest of the reader's errors are looked for
from .storage import is_link
from .words import GB, size_words

READABLE = ("cbz", "cbr", "pdf")  # a title with several files opens the first of these it has (a book: its PDF first,
# as a kind whose e-book opens first has it, [kinds.X] text_first)
BOOKS = ("epub", "mobi", "fb2")  # e-books MuPDF lays out as pages, for a borrower
# One more when MuPDF's layout of an e-book's pages changes (pdfworker.BOOK_PAGE: their size and font), so that the
# pictures of a book's pages kept a week in browsers go (2: vh and vw, pdfworker)
BOOK_LAYOUT = 2
PACKED = re.compile(r"(.+?\.zip)/(.+)", re.IGNORECASE)  # a file inside a ZIP package, '.../book.zip/book.pdf'
# The page widths made: a screen gets the next one up. 240 is the page strip's; from 1200 each step is about a quarter
# wider than the one before, so no screen gets much more than it shows (a 2048-wide tablet 2400, not 3200); 3200 is zoomed
# in. The library page rounds up to them itself, as /api/session gives them, so a page's address stays the same in a
# window made a little larger or smaller, and the browser has it already. Their pictures are kept by width (_drawn_file):
# those of widths no longer made are never asked for again, and go with their book's folder.
WIDTHS = (240, 600, 900, 1200, 1500, 1900, 2400, 3200)


# Limits for files that came from somewhere else (a bundle, a folder of your own): a page or a packed file can't
# make the server use memory, disk or processes beyond reason, whatever the archive claims.
MAX_PICTURE = 64 << 20  # one comic page, unpacked
MAX_INNER = 4 << 30  # one file unpacked from a ZIP package into the cache
MAX_PIXELS = pdfworker.MAX_PIXELS  # one page drawn or decoded: a PDF page, a comic page, a cover (real ones are under 25)
MAX_UNPACKED = 2 << 30  # a solid RAR unpacked into the cache, everything it holds together: 2.1 GB, as the help says it
MAX_ENTRIES = 5000  # files in an archive that is unpacked whole
MAX_PAGES = pdfworker.MAX_PAGES  # pages a file may say it has: a PDF can claim any number, and the page strip has a button for each
FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")  # the only kinds of picture decoded, whatever a file is named
# As many 7-Zips at a time as PDF workers, and twice as many pictures decoded, by what this computer has room for
# (pdfworker.capacity): 2 and 4 on 4 cores and 8 GB, 1 and 2 on a computer of 1 or 2 GB, up to 4 and 8 on a big one
UNPACKS = threading.BoundedSemaphore(pdfworker.WORKERS)  # 7-Zip processes at a time
DECODES = threading.BoundedSemaphore(2 * pdfworker.WORKERS)  # pictures decoded at a time


def readable(item: dict, books: bool = False) -> str | None:
    """The file of a title the reader shows as pages (its path in the library, as the catalog quotes it), or None.
    Each kind from the title's own files first, then from inside its ZIP package (the smallest), and last a ZIP that
    is pictures. With books (a borrower), e-books too, after those. The library page's readFile picks the same way;
    otherwise a book with an EPUB, say, isn't asked about."""
    found = rules.current().kinds.get(item.get("k") or "")
    kinds = (("pdf", "cbz", "cbr") if found and found.text_first else READABLE) + (BOOKS if books else ())
    return _first_of(item, kinds) or item.get("zp") or None


def readable_book(item: dict) -> str | None:
    """A title's e-book, for the pictures of its pages in the page strip; the reader lays out the book itself."""
    return _first_of(item, BOOKS)


def _first_of(item: dict, kinds: tuple) -> str | None:
    bad = item.get("bad", {})  # locked or damaged: no reader can open those (meta.cant_open)
    for kind in kinds:
        own = next((path for path, ext, _ in item.get("f", []) if ext == kind and path not in bad), None)
        packed = sorted((size, path) for path, ext, size in item.get("z", []) if ext == kind)
        if own or packed:
            return own or packed[0][1]
    return None


def width_for(asked: int) -> int:
    return next((w for w in WIDTHS if w >= asked), WIDTHS[-1])


def _stamp(path: Path) -> tuple:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


@functools.lru_cache(maxsize=512)
def _kind(path: str, stamp: tuple) -> str:
    """zip, rar or pdf, by the file's first bytes, not its name; book for an e-book (an EPUB is a ZIP too)."""
    if PurePosixPath(path).suffix.lower().lstrip(".") in BOOKS:
        return "book"
    with open(path, "rb") as fh:
        head = fh.read(1024)
    if head.startswith(b"PK"):
        return "zip"
    if head.startswith(b"Rar!"):
        return "rar"
    if b"%PDF" in head:
        return "pdf"
    raise Unreadable("This isn't a comic archive or a PDF inside.")


# A comic packed with RAR's own methods, as most CBR comics are, needs 7-Zip's RAR part, which some systems' 7-Zip leaves
# out (archives._WITH_RAR). Without it every page fails with an error of 7-Zip's alone; so when a page couldn't be
# unpacked, and only then, 7-Zip is asked whether it lacks the part (archives.sevenzip_info, asked again after
# RAR_PART_LOOK seconds, so that one installed meanwhile is seen), and if it does, and the page is packed, the page's
# refusal says what to install. A page 7-Zip unpacks is read whatever it says of itself.
RAR_PART_LOOK = 60.0
_rar_part: dict = {"tool": None, "at": -1e9, "has": None}


def lacks_rar_part() -> bool:
    """Whether the 7-Zip in use says it has no RAR part; False when it can't be told, or there is no 7-Zip."""
    tool, now = archives.sevenzip(), time.monotonic()
    if not tool:
        return False
    if _rar_part["tool"] != tool or now - _rar_part["at"] >= RAR_PART_LOOK:
        _rar_part.update(tool=tool, at=now, has=archives.sevenzip_info(tool)[1])
    return _rar_part["has"] is False


def _unpacks(path: str, name: str) -> None:
    """For a page that couldn't be unpacked: refuses it in words that say what to install, where 7-Zip lacks its RAR
    part and the page is packed in a way only that part unpacks. Else nothing, and the failure goes on as it was."""
    import rarfile

    if not lacks_rar_part():
        return
    with archives.open_rar(path) as r:
        packed = r.getinfo(name).compress_type != rarfile.RAR_M0
    if packed:
        raise Unreadable("This comic's pages are packed with RAR, and the 7-Zip on the computer the server runs on can't "
                         f"unpack them: it lacks its RAR part. {archives.no_rar_part()}")


def open_picture(data: bytes):
    """A picture from a book, opened (not yet decoded) if it is one of FORMATS and no bigger than MAX_PIXELS;
    else Unreadable. Pillow alone would read some forty kinds, some through other programs, and decode up to
    179 million pixels, which a 13 KB file can claim."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data), formats=FORMATS)
    except Exception as e:  # noqa: BLE001 - not a picture of an allowed kind
        raise Unreadable("This picture can't be read.") from e
    if im.width * im.height > MAX_PIXELS:
        im.close()
        raise Unreadable("This picture is far bigger than a page can be.")
    return im


# A "solid" RAR packs all pages as one stream, so page 140 means unpacking pages 1 to 139 first, every time. Such a
# comic is unpacked once, in the background, into a cache of at most cache_limit(); meanwhile its first pages (quick
# ones) come straight from the archive.
def cache_limit() -> int:
    """The cache's size on disk, the comics unpacked whole and the pages drawn together: [reader] cache_gb in the settings
    (10 GB unless set). The disk keeps some room free whatever it is: disk_limit."""
    return int(settings.get("reader")["cache_gb"] * GB)  # a disk's GB, as the setting is named: words.py


UNPACK_WAIT = 20.0  # seconds a page deep in a solid RAR waits for its unpacking, before it's read from the archive
FAILED_FOR = 600.0  # seconds a solid RAR that couldn't be unpacked is read page by page, before it's tried again
_unpacking: set[str] = set()
_failed: dict[str, float] = {}  # couldn't be unpacked (no 7-Zip, a broken archive, a full disk), and when
_unpack_lock = threading.Lock()
UNLISTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029*?]")  # in a page's name, what 7-Zip's list of names would take
# for two names or a pattern: a line break, another control character, * or ?


def _has_links(folder: Path) -> bool:
    """A link among what 7-Zip unpacked: no comic needs one, and it could lead out of the cache. Older 7-Zips let
    archives do that: 25.00 or newer is safe from it."""
    return any(is_link(Path(where) / name) for where, dirs, files in os.walk(folder) for name in (*dirs, *files))


def _as_listed(folder: Path, names: list[str], size: int) -> bool:
    """What 7-Zip unpacked is what the archive listed: every page there, by the name the reader asks for, as many files
    as names and no more, size bytes at most, and no link. 7-Zip may have left a page out (it trims a name's spaces in
    its list), or a disk that ignores case may have kept two pages as one."""
    if _has_links(folder):
        return False
    wanted, found, held = {name.replace("\\", "/") for name in names}, 0, 0
    for where, _, files in os.walk(folder):
        for name in files:
            found += 1
            held += os.lstat(os.path.join(where, name)).st_size
            if found > len(wanted) or held > size:
                return False
    return found == len(wanted) and all(_unpacked_page(folder, name) for name in wanted)


def _unpacked_page(folder: Path, name: str) -> bool:
    """Whether a page is in the folder as the reader will read it (_inside)."""
    try:
        return _inside(folder, name).is_file()
    except Unreadable:
        return False


SERVED: list[Path | None] = [None]  # the library the server serves (serve.make_server): the cache is never kept in it
_cache_look: dict = {"key": None, "at": -1e9, "root": None, "why": None}


def cache_root() -> Path | None:
    """Where the cache is kept: [reader] cache_folder, or Hylly's cache folder (paths.place) while that's "". None while the
    folder set can't be used, and cache_problem says why: it isn't there, as when its disk isn't connected (Hylly makes its
    own folders inside it, never the folder itself, so a missing disk isn't filled in its place), it can't be written, or
    it's in the library, which the server serves. Looked at again every LOOK_EVERY seconds, so a disk connected later is
    used; told once in the server's log."""
    from . import paths

    asked = settings.get("reader")["cache_folder"]
    if not asked:
        return paths.place("cache")
    key, now = (asked, str(SERVED[0])), time.monotonic()
    if _cache_look["key"] != key or now - _cache_look["at"] >= LOOK_EVERY:
        root, why = _usable(Path(asked))
        if why and (key, why) != (_cache_look["key"], _cache_look["why"]):
            print(f"[reader] cache_folder = {asked!r}: {why}.", file=sys.stderr, flush=True)
        _cache_look.update(key=key, at=now, root=root, why=why)
    return _cache_look["root"]


def cache_problem() -> str | None:
    """What keeps [reader] cache_folder from being used, if anything."""
    return None if cache_root() is not None else _cache_look["why"]


def _usable(folder: Path) -> tuple[Path | None, str | None]:
    from . import paths

    if not folder.is_dir():
        return None, ("it isn't there (is its disk connected?), so the pages drawn aren't kept on disk until it is, and "
                      "comics unpacked to be read go to Hylly's own cache folder meanwhile")
    where = folder.resolve()
    libraries = [SERVED[0]]
    try:
        libraries.append(paths.library_root())
    except (SystemExit, Exception):  # noqa: BLE001 - no library set: nothing to be inside
        pass
    if any(where.is_relative_to(Path(lib).resolve()) for lib in libraries if lib):
        return None, "it's inside the library, whose files the server serves: give a folder outside it"
    try:
        (folder / "drawn").mkdir(exist_ok=True)
        probe = folder / "drawn" / f".written-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        return None, (f"it can't be written ({e.strerror or type(e).__name__}); a server walled in by systemd needs "
                      "BindPaths= for it in its service")
    return folder, None


def cache_dir() -> Path:
    """Comics unpacked whole and files from inside ZIP packages, which reading them needs: in cache_root(), or the
    cache folder of Hylly's own (paths.place) while that can't be used."""
    from . import paths

    return (cache_root() or paths.place("cache")) / "pages"


def _unpack_key(path: str, stamp: tuple) -> str:
    return hashlib.sha1(f"{path}|{stamp}".encode()).hexdigest()[:20]


def _unpacked(path: str, stamp: tuple, wait: float = 0.0) -> Path | None:
    """The folder a solid RAR has been unpacked into, the unpacking begun here if need be. None while it's being
    unpacked, after waiting up to wait seconds; and at once when it couldn't be, for FAILED_FOR (then it's tried again),
    as it's read page by page meanwhile."""
    key = _unpack_key(path, stamp)
    folder, end = cache_dir() / key, time.monotonic() + wait
    while not (folder / ".done").exists():
        with _unpack_lock:
            failed = _failed.get(key)
            if failed is not None and time.monotonic() - failed < FAILED_FOR:
                return None
            begin = key not in _unpacking
            if begin:
                _failed.pop(key, None)
                _unpacking.add(key)
        if begin:
            threading.Thread(target=_unpack, args=(path, folder, key), daemon=True, name="unpack").start()
        if time.monotonic() >= end:
            return None
        time.sleep(0.3)
    return folder


def _being_unpacked(path: str, stamp: tuple) -> bool:
    with _unpack_lock:
        return _unpack_key(path, stamp) in _unpacking


def _unpack(path: str, folder: Path, key: str) -> None:
    part, ok = folder.with_name(folder.name + ".part"), False
    listing = part.with_name(part.name + ".list")
    try:
        found = _unpackable(path)
        if found is None:  # not one to unpack whole: read page by page instead
            return
        names, size = found
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True)
        listing.write_text("\n".join(names) + "\n", encoding="utf-8")
        with UNPACKS:  # a burst of requests for many comics doesn't start a 7-Zip for each
            # only the pages, no password asked for (-p: an empty one), nothing read from a console; after --, the archive
            # is a name, whatever it starts with
            done = subprocess.run([archives.sevenzip(), "x", "-y", "-p", "-scsUTF-8", f"-o{part}", f"-i@{listing}", "--", path],
                                  capture_output=True, timeout=1800, stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode == 0 and _as_listed(part, names, size):
            (part / ".done").write_text("", encoding="utf-8")
            part.replace(folder)
            _trim_cache(keep=folder)
            ok = True
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        try:
            listing.unlink(missing_ok=True)
        except OSError:  # held just then (Windows): it goes as the cache is first counted at the next start
            pass
        if not ok:
            shutil.rmtree(part, ignore_errors=True)
        with _unpack_lock:
            _unpacking.discard(key)
            if not ok:
                now = time.monotonic()
                for old in [k for k, when in _failed.items() if now - when >= FAILED_FOR]:
                    del _failed[old]
                _failed[key] = now


_inner_locks: dict[str, threading.Lock] = {}
INNER_SUFFIXES = (".pdf", ".epub", ".mobi", ".azw3", ".azw", ".fb2", ".cbz", ".cbr")  # a file unpacked from a ZIP package
# is kept as "file" and its own suffix if it's one of these (the reader and MuPDF go by it), never by its name in the ZIP


def inner(package: Path, member: str) -> Path:
    """A file inside a ZIP package, unpacked once into the cache: a PDF must be sought in, and a big book is sent a
    part at a time, neither of which a file still packed allows. Its folder is touched as it's used, so the files
    read longest ago leave the cache first. One the cache hasn't room for is Unreadable, as is one the ZIP can't
    give (damaged, locked, packed in a way zipfile doesn't open); nothing of it is left behind."""
    if PurePosixPath(member).name in ("", ".", ".."):
        raise Unreadable("The file inside the ZIP has a name that isn't a file's.")
    # "file": kept as INNER_SUFFIXES says, under keys of their own; a file kept by its name in the ZIP isn't found by
    # these, and goes as the cache is trimmed
    key = hashlib.sha1(f"{package}|{_stamp(package)}|{member}|file".encode()).hexdigest()[:20]
    folder = cache_dir() / key
    suffix = PurePosixPath(member).suffix.lower()
    target = folder / ("file" + (suffix if suffix in INNER_SUFFIXES else ""))
    with _unpack_lock:
        lock = _inner_locks.setdefault(key, threading.Lock())
    with lock:
        if not (folder / ".done").exists():
            try:
                with zipfile.ZipFile(package) as z:
                    info = z.getinfo(member)
                    archives.not_a_bomb(info, MAX_INNER)
                    _room_for(info.file_size)
                    folder.mkdir(parents=True, exist_ok=True)
                    try:
                        if not (target.exists() and target.stat().st_size == info.file_size):
                            part = target.with_name(target.name + ".part")
                            with z.open(info) as src, open(part, "wb") as out:
                                shutil.copyfileobj(src, out, 1 << 20)
                            os.replace(part, target)
                    except BaseException:  # a full disk, a damaged ZIP, the server stopping: nothing half-made stays
                        shutil.rmtree(folder, ignore_errors=True)
                        raise
            except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError) as e:
                raise Unreadable("The file inside the ZIP couldn't be read.") from e  # RuntimeError: locked by a password
            (folder / ".done").write_text("", encoding="utf-8")
            _trim_cache(keep=folder)
        folder.touch()
    return target


def _room_for(size: int) -> None:
    """Unreadable when a file of size bytes, to be unpacked into the cache, is more than the cache may hold (disk_limit),
    or than its disk has free, keeping a gigabyte, as _unpackable does."""
    pages = cache_dir()
    pages.mkdir(parents=True, exist_ok=True)
    try:
        _count_cache()
    except OSError:  # a count that fails (its disk in trouble, a folder gone midway): by what's counted, if anything
        pass
    with _disk_lock:
        held = sum(_disk["books"].values())
    if size > disk_limit(held) or shutil.disk_usage(pages).free < size + (1 << 30):
        raise Unreadable(f"Reading the file inside the ZIP means unpacking it ({size_words(size)}), and the reader's "
                         "cache has no room for that: make room on its disk, or raise [reader] cache_gb in the settings.")


def _unpackable(path: str) -> tuple[list[str], int] | None:
    """The pages of a solid RAR to unpack whole, and how many bytes they take; or None to read it page by page instead:
    one with a password, links or references, too many files, more than MAX_UNPACKED all told (or more than the disk
    has free), or a page whose name 7-Zip's list of names can't hold (UNLISTABLE)."""
    try:
        with archives.open_rar(path) as r:
            if r.needs_password():
                return None
            infos = [i for i in r.infolist() if not i.is_dir()]
            if len(infos) > MAX_ENTRIES or any(getattr(i, "file_redir", None) or i.is_symlink() for i in infos):
                return None
            listed = set(archives.comic_pages([i.filename for i in infos]))
            pages = [i for i in infos if i.filename in listed]
            total = sum(i.file_size for i in infos)  # everything in it, though only the pages are unpacked
            if (total > MAX_UNPACKED or any(i.file_size > MAX_PICTURE for i in pages)
                    or any(UNLISTABLE.search(i.filename) for i in pages)):
                return None
    except Exception:  # noqa: BLE001 - a broken archive: page by page, where its errors come one at a time
        return None
    cache_dir().mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(cache_dir()).free < total + (1 << 30):
        return None
    return [i.filename for i in pages], sum(i.file_size for i in pages)


def _inside(folder: Path, name: str) -> Path:
    """An unpacked page by the name the archive gave it: one that leads out of the folder (../) isn't read."""
    target = (folder / name).resolve()
    if not target.is_relative_to(folder.resolve()):
        raise Unreadable("A page in the archive has a name that leads outside it.")
    return target


def _trim_cache(keep: Path) -> None:
    """The books read longest ago go, until the cache is under its limit: keep, just unpacked, stays. Its files are
    counted before the lock is taken, which is held for the bookkeeping alone. A count that fails (its disk in trouble,
    a folder gone midway) trims nothing now: the next page kept, or file unpacked, tries again."""
    try:
        size = _size_of(keep)
        _count_cache()
    except OSError:
        return
    with _disk_lock:
        books = _disk["books"]
        books[keep] = size
        gone = _over(books, keep, disk_limit(sum(books.values())))
    _remove(gone)


# ---- Pages drawn, kept on disk ---------------------------------------------------------------------------------------------
# Drawing a page takes the server up to a second or two, reading one it drew before a millisecond. So every page drawn, asked
# for or drawn ahead, is kept as a file in the cache (cache_root), a folder for each book, and the books read longest ago
# go first once the cache (these and the comics unpacked whole) is past its limit. They're kept by book, not by who read
# them, and found again by the file's time and size, so a changed file gets new pages. The computer keeps the latest of
# them in its own memory too.
DRAWN = 1  # one more when pages are drawn differently (their quality, say), so that those kept go, here and in browsers (v)
KEEP_DISK_FREE = (2 << 30, 20 << 30)  # the disk keeps a twentieth of it free, whatever cache_gb says: 2.1 to 21.5 GB
TRIM_TO = 0.9  # a cache past its limit is trimmed to this share of it, so that it isn't trimmed again at the next page
# The cache's folders and what each holds: counted once (_count_cache), then kept up to date; while it's being counted,
# the pages drawn meanwhile (waiting), kept on disk once it is
_disk: dict = {"tops": None, "books": {}, "counting": False, "waiting": []}
_disk_lock = threading.Lock()  # for that bookkeeping alone: never held while the cache is counted or a page is written
_counting = threading.Lock()  # one count at a time: another that needs it waits for the one going on
WAITING_MOST = 64 << 20  # bytes of pages waiting for the count: past that, one drawn meanwhile is kept in memory only
# What Hylly makes in pages and drawn: a folder for each book, named by its key (_unpacked, inner, _drawn_file). Only these
# are counted and removed; cache_folder may be a folder where other things are kept too, and those are left as they are.
BOOK_FOLDER = re.compile(r"[0-9a-f]{20}")
AT_WORK = re.compile(r"[0-9a-f]{20}\.part(\.list)?|\.written-\d+")  # what Hylly leaves beside them for a moment as it works
CUT_SHORT = re.compile(r"([0-9a-f]{20})\.part(?:\.list)?")  # of those, in pages: a solid RAR's unpacking (_unpack), by its key
SYSTEM_LEFT = frozenset({".ds_store", "thumbs.db", "desktop.ini"})  # what a Mac or Windows browsing the folder leaves: not news
NOT_OURS_NEXT = "If it's yours, keep it somewhere else: cache_folder is best a folder of Hylly's own."


def drawn_dir() -> Path | None:
    """The pages drawn: in cache_root(); None while that can't be used, and then they're kept in memory only."""
    root = cache_root()
    return root / "drawn" if root else None


def _drawn_file(path: str, stamp: tuple, kind: str, n: int, width: int) -> Path | None:
    folder = drawn_dir()
    book = hashlib.sha1(f"{path}|{stamp}|{kind}|{DRAWN}|{BOOK_LAYOUT}".encode()).hexdigest()[:20]
    return folder / book / f"{n}-{width}.webp" if folder else None


def _webp(data: bytes) -> bool:
    return len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _size_of(folder: Path) -> int:
    total = 0
    for p in folder.rglob("*"):
        try:
            total += p.stat().st_size if p.is_file() else 0
        except OSError:  # gone meanwhile
            pass
    return total


def _looked_through(top: Path) -> tuple[list[Path], list[str]]:
    """In pages or drawn: the books' folders, which Hylly made, and the names of anything else, which it didn't (what it
    leaves there for a moment as it works is neither). Nothing in a folder not made yet."""
    books, others = [], []
    try:
        for f in top.iterdir():
            if BOOK_FOLDER.fullmatch(f.name) and f.is_dir():
                books.append(f)
            elif not AT_WORK.fullmatch(f.name) and f.name.lower() not in SYSTEM_LEFT:
                others.append(f.name)
    except OSError:  # not made yet, or gone meanwhile
        pass
    return books, sorted(others)


def _not_ours(top: Path, names: list[str]) -> str:
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    return (f"The cache's folder {top} holds what Hylly didn't make there ({shown}): it's left as it is, never counted in "
            "cache_gb nor removed, as Hylly removes only the folders it made")


def not_ours() -> list[str]:
    """Anything in the cache's pages and drawn that Hylly didn't make, as hylly doctor says it; the server's log says it
    once, as the cache is counted."""
    return [_not_ours(top, names) for top in (cache_dir(), drawn_dir()) if top and (names := _looked_through(top)[1])]


def _tops() -> tuple[Path, ...]:
    """The cache's folders: pages, and drawn while cache_root can be used."""
    return tuple(top for top in (cache_dir(), drawn_dir()) if top)


def _counted(tops: tuple[Path, ...]) -> dict[Path, int] | None:
    """The cache's folders (tops), each with what it holds, as counted and kept up to date since; None while they
    haven't been counted, or are being counted (_count_cache). Called with _disk_lock held."""
    return _disk["books"] if _disk["tops"] == tops else None


def _count_cache(tops: tuple[Path, ...] | None = None) -> None:
    """The cache counted, the first time (and if it has moved): each of its folders with what it holds, kept up to date
    from then on as pages are written. Counted without _disk_lock, as tens of thousands of files on a slow or sleeping
    disk take a while (the server counts at its start, in a thread of its own): meanwhile pages are drawn and sent, and
    those drawn are kept on disk as the count ends (waiting: _keep_on_disk); only what needs the count waits for it. One
    count at a time: another asker waits for the one going on. Only the folders Hylly made are counted, so only they are
    ever removed; anything else there is named in the server's log. Unpackings cut short go then (_cut_short)."""
    tops = tops or _tops()
    waiting = []
    with _counting:
        with _disk_lock:
            known = _counted(tops) is not None
            _disk["counting"] = not known
        try:
            if not known:
                _cut_short(tops[0])
                books = {}
                for top in tops:
                    folders, others = _looked_through(top)
                    books.update((f, _size_of(f)) for f in folders)
                    if others:
                        print(f"[reader] {_not_ours(top, others)}. {NOT_OURS_NEXT}", file=sys.stderr, flush=True)
                with _disk_lock:
                    _disk.update(tops=tops, books=books)
        finally:
            with _disk_lock:
                _disk["counting"] = False
                waiting, _disk["waiting"] = _disk.get("waiting", []), []
    for key, found, ahead in waiting:  # the count is in: kept now, by the same rules
        _keep_on_disk(key, found, ahead)


def _cut_short(pages: Path) -> None:
    """What a solid RAR's unpacking leaves in pages while it works, a .part folder and its .part.list, when it was cut
    short (the server stopped meanwhile): removed as the cache is first counted, as the server starts, since no count sees
    it. By its name alone (CUT_SHORT), as Hylly made it; one this server is unpacking now stays. Listed before the
    unpackings going on are, as one joins them before its .part is made."""
    try:
        found = list(pages.iterdir())
    except OSError:  # not made yet, or gone meanwhile
        return
    with _unpack_lock:
        busy = set(_unpacking)
    for f in found:
        hit = CUT_SHORT.fullmatch(f.name)
        try:
            if not hit or hit[1] in busy or is_link(f):
                continue
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)
        except OSError:  # gone meanwhile, or held by another program: tried again at the next start
            pass


def disk_used() -> int:
    """What the cache holds on disk now, counted the first time it's asked (and waited for while it's being counted)."""
    _count_cache()
    with _disk_lock:
        return sum(_disk["books"].values())


def disk_limit(held: int) -> int:
    """How much the cache may hold, holding held now: cache_limit(), unless the disk would then have less than a twentieth
    of it free (KEEP_DISK_FREE). A disk filling up with other things gets its room back."""
    where = drawn_dir() or cache_dir()
    while not where.exists() and where.parent != where:
        where = where.parent
    try:
        disk = shutil.disk_usage(where)
    except OSError:
        return cache_limit()
    least, most = KEEP_DISK_FREE
    return max(0, min(cache_limit(), held + disk.free - min(max(least, disk.total // 20), most)))


def _over(books: dict[Path, int], keep: Path, limit: int) -> list[Path]:
    """The folders to go, read longest ago first, when the cache is past limit, until it's under TRIM_TO of it: taken out of
    books here, and removed by the caller, without the lock. Called with _disk_lock held."""
    total = sum(books.values())
    if total <= limit:
        return []

    def last_read(folder: Path) -> float:
        try:
            return folder.stat().st_mtime
        except OSError:  # gone already
            return 0.0

    gone = []
    for folder in sorted(books, key=last_read):
        if total <= limit * TRIM_TO:
            break
        if folder != keep:
            total -= books.pop(folder)
            gone.append(folder)
    return gone


def _remove(folders: list[Path]) -> None:
    for folder in folders:
        if BOOK_FOLDER.fullmatch(folder.name):  # never anything but a book's folder, which _counted alone takes in
            shutil.rmtree(folder, ignore_errors=True)


def _from_disk(key: tuple) -> bytes | None:
    """A page drawn before, if it's on disk; its book's folder is touched, as read lately, the last to go."""
    target = _drawn_file(*key)
    if target is None:
        return None
    try:
        data = target.read_bytes()
    except OSError:
        return None
    if not _webp(data):
        return None
    try:
        os.utime(target.parent)
    except OSError:
        pass
    return data


def _keep_on_disk(key: tuple, found: tuple[bytes, str], ahead: bool = False) -> None:
    """A page just drawn, kept as a file, and the books read longest ago taken out if the cache is now past its limit. One
    drawn ahead only takes room that is free: a guess never pushes out what someone has read (in memory it may, as that's
    on disk too). One that can't be kept isn't: it's only quicker. The file is written without _disk_lock, which is
    taken for the bookkeeping alone, so a slow disk holds up no other page; and while the cache is being counted, the
    page waits in memory for the count, and is kept as it ends (_count_cache), as the reader has it already. A cache
    not counted yet, with no count going on (its folder has just come back, say), is counted first."""
    data, ctype = found
    if ctype != "image/webp" or not _webp(data) or len(data) > KEEP_PAGE:
        return
    target = _drawn_file(*key)
    if target is None:
        return
    try:
        tops = _tops()
        with _disk_lock:
            books = _counted(tops)
            if books is None and _disk.get("counting"):  # kept as the count ends, with others' drawn meanwhile
                waiting = _disk.setdefault("waiting", [])
                if sum(len(w[1][0]) for w in waiting) + len(data) <= WAITING_MOST:
                    waiting.append((key, found, ahead))
                return
            held = sum(books.values()) if books is not None else None
        if held is None:  # not counted, and no count going on: counted now
            _count_cache(tops)
            with _disk_lock:
                books = _counted(tops)
                if books is None:  # it moved again meanwhile: its own count finds what it holds
                    return
                held = sum(books.values())
    except OSError:  # a count that fails (its disk in trouble, a folder gone midway): the page is sent, and not kept
        return
    limit, before, kept = disk_limit(held), 0, False
    try:
        if limit >= len(data) and not (ahead and held + len(data) > limit):  # else no room: a full disk, or a guess
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(f"{target.name}.{threading.get_ident()}.part")
            part.write_bytes(data)
            before = target.stat().st_size if target.exists() else 0
            os.replace(part, target)
            kept = True
    except OSError:
        return
    with _disk_lock:
        books = _counted(tops)  # as kept up to date meanwhile
        if books is None:  # the cache moved meanwhile: its own count finds the page
            return
        if kept:
            books[target.parent] = books.get(target.parent, 0) + len(data) - before
        gone = _over(books, target.parent, limit)  # also when a disk filling up with other things wants room back
    _remove(gone)


@functools.lru_cache(maxsize=256)
def _names(path: str, stamp: tuple, kind: str) -> tuple[str, ...]:
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            names = [i.filename for i in z.infolist() if not i.is_dir()]
    else:
        with archives.open_rar(path) as r:
            names = [i.filename.replace("\\", "/") for i in r.infolist() if not i.is_dir()]
    return tuple(archives.comic_pages(names))


def count(path: Path) -> int:
    """How many pages. For an archive only its list of contents is read; for a PDF, its table of objects. More than
    MAX_PAGES is Unreadable: a PDF says how many it has, which needn't be how many are there."""
    stamp = _stamp(path)
    try:
        kind = _kind(str(path), stamp)
        pages = pdfworker.pages(str(path), stamp) if kind in ("pdf", "book") else len(_names(str(path), stamp, kind))
        if pages > MAX_PAGES:
            raise Unreadable(f"This file says it has {pages:,} pages, more than any book has.")
        return pages
    except Unreadable:
        raise
    except Exception as e:  # noqa: BLE001 - a broken file, in whichever library noticed
        raise Unreadable(f"The file couldn't be read ({type(e).__name__}).") from e


def outline(path: Path) -> list[list]:
    """A PDF's or an e-book's own table of contents, as [level, title, page from 0]; a comic has none."""
    stamp = _stamp(path)
    try:
        if _kind(str(path), stamp) not in ("pdf", "book"):
            return []
        toc = pdfworker.outline(str(path), stamp)
        return [[level, title.strip(), page - 1] for level, title, page in toc[:2000] if page >= 1 and title.strip()]
    except Exception:  # noqa: BLE001 - a book without its outline still reads
        return []


def sections(path: Path) -> list[int]:
    """How many pages each part of an e-book takes as it's drawn here; nothing for a kind that has none. A book without
    them still reads: the page strip then goes by how far through the text you are."""
    stamp = _stamp(path)
    try:
        return pdfworker.sections(str(path), stamp) if _kind(str(path), stamp) == "book" else []
    except Exception:  # noqa: BLE001 - as outline
        return []


def _scaled(data: bytes, width: int) -> tuple[bytes, str]:
    """A picture at most width wide, as WebP. Every page is decoded and made again here, so only pictures Pillow
    made leave the server, never a book's own bytes; one it can't read is an error, not passed on as it was."""
    from PIL import Image

    with DECODES, open_picture(data) as im:
        try:
            w = min(width, im.width)
            height = max(1, round(im.height * w / im.width))
            if im.format == "JPEG":
                im.draft("RGB", (w, height))  # decodes a JPEG at a fraction of its size: many times faster
            see_through = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if see_through else "RGB")
            if im.width != w:
                im = im.resize((w, height), Image.Resampling.LANCZOS)
            return pdfworker.webp(im), "image/webp"
        except Unreadable:
            raise
        except Exception as e:  # noqa: BLE001 - a broken picture
            raise Unreadable("This page's picture can't be read.") from e


# Pages made lately, by how much memory they take rather than how many they are: a noisy page at full size can be 30 MB.
# All of them together take at most [reader] memory_mb, or less when this computer is short of memory: keep_limit.
KEEP_PAGE = 32 << 20  # and none bigger than this
KEEP_LEAST = 16 << 20  # what they may always take, however little is free
MEMORY_SHARE = 0.5  # of what's free, counting what they hold as free: they settle there, and give back when others need it
LOOK_EVERY = 30.0  # seconds between looks at what's free
_looked = {"at": -1e9, "room": None}
_kept: OrderedDict[tuple, tuple[bytes, str]] = OrderedDict()
_kept_size = [0]
_kept_lock = threading.Lock()


def memory_free() -> int | None:
    """What this computer has free for programs, by its own count, in bytes; None if it won't say. The files it merely keeps
    in memory count as free, as it gives those up when asked: psutil's available, which each system answers at once,
    with no program run for it, as keep_limit looks while it holds _kept_lock."""
    try:
        import psutil  # here, as on_battery has it

        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001 - a system that won't say (psutil's own errors among them): the setting as it is
        return None


def keep_limit() -> int:
    """How much memory the pages may take now: what the settings ask, if half of what's free can spare it. Looked at again
    now and then, so that a computer short of memory later gets some of it back."""
    now = time.monotonic()
    if now - _looked["at"] >= LOOK_EVERY:
        free = memory_free()
        _looked.update(at=now, room=None if free is None else int((free + _kept_size[0]) * MEMORY_SHARE))
    asked = settings.get("reader")["memory_mb"] << 20
    room = _looked["room"]
    return asked if room is None else min(asked, max(KEEP_LEAST, room))


def memory_note() -> str | None:
    """What to tell, when the settings ask for more than can be spared."""
    asked, free = settings.get("reader")["memory_mb"], memory_free()
    if free is None or (asked << 20) <= free * MEMORY_SHARE:
        return None
    return (f"[reader] memory_mb asks for {asked:,} MB for pages, and this computer has {free >> 20:,} MB free: "
            "the pages will keep to half of what's free.")


# How the pages asked for came, since the server started: from memory, from disk, or drawn then (and how long that took
# all told); and those drawn ahead, and how many of them were asked for later. The server page shows them (status).
STATS = {"memory": 0, "disk": 0, "drawn": 0, "waited": 0.0, "ahead": 0, "used": 0}
WIDTHS_ASKED: dict[int, int] = {}  # how many pages were asked for at each width: which are worth drawing ahead and keeping
_drawn_ahead: OrderedDict[tuple, None] = OrderedDict()  # pages drawn ahead not yet asked for, the latest AHEAD_REMEMBERED
AHEAD_REMEMBERED = 20000
_drawing: dict[tuple, threading.Event] = {}  # pages being drawn now: whoever wants one of them too waits for it
DRAW_PATIENCE = 120.0  # seconds to wait for a page someone else is drawing, before drawing it here
_stats_lock = threading.Lock()


def _page(path: str, stamp: tuple, kind: str, n: int, width: int, ahead: bool = False) -> tuple[bytes, str]:
    """A page from memory, else from disk, else drawn (once, however many want it). ahead: drawn before it's asked for,
    which isn't counted as asked for, and doesn't hold up drawing ahead (as a page asked for does)."""
    key = (path, stamp, kind, n, width)
    if not ahead:
        with _stats_lock:
            WIDTHS_ASKED[width] = WIDTHS_ASKED.get(width, 0) + 1
    with _kept_lock:
        found = _kept.get(key)
        if found is not None:
            _kept.move_to_end(key)
    if found is None:
        data = _from_disk(key)
        if data is not None:
            found = (data, "image/webp")
            _remember(key, found)
            if not ahead:
                _count("disk", key)
            return found
    elif not ahead:
        _count("memory", key)
    if found is not None:
        return found
    if ahead:
        found = _draw_once(key, ahead=True)
        with _stats_lock:
            STATS["ahead"] += 1
            _drawn_ahead[key] = None
            while len(_drawn_ahead) > AHEAD_REMEMBERED:
                _drawn_ahead.popitem(last=False)
        return found
    began = time.monotonic()
    with _ahead_cond:
        _asked_now[0] += 1
    try:
        found = _draw_once(key)
    finally:
        with _ahead_cond:
            _asked_now[0] -= 1
            _ahead_cond.notify_all()
    with _stats_lock:
        STATS["drawn"] += 1
        STATS["waited"] += time.monotonic() - began
        _drawn_ahead.pop(key, None)  # it was being drawn ahead as it was asked for: not one the guesses saved
    return found


def _count(source: str, key: tuple) -> None:
    with _stats_lock:
        STATS[source] += 1
        if key in _drawn_ahead:
            del _drawn_ahead[key]
            STATS["used"] += 1


def _remember(key: tuple, found: tuple[bytes, str]) -> None:
    """A page kept in memory, the oldest going once they're past keep_limit()."""
    if len(found[0]) > KEEP_PAGE:
        return
    with _kept_lock:
        if key not in _kept:
            _kept[key] = found
            _kept_size[0] += len(found[0])
        limit = keep_limit()
        while _kept and _kept_size[0] > limit:
            _kept_size[0] -= len(_kept.popitem(last=False)[1][0])


def _draw_once(key: tuple, ahead: bool = False) -> tuple[bytes, str]:
    """A page drawn and kept, in memory and on disk; or, if it's being drawn already (asked for while it's drawn ahead,
    say), that drawing waited for. Drawn ahead, it's kept on disk only in free room (_keep_on_disk)."""
    while True:
        with _kept_lock:
            if key in _kept:
                return _kept[key]
            going = _drawing.get(key)
            mine = going is None
            if mine:
                going = _drawing[key] = threading.Event()
        if mine:
            break
        going.wait(DRAW_PATIENCE)
        with _kept_lock:
            if key in _kept:
                return _kept[key]
        data = _from_disk(key)
        if data is not None:
            return data, "image/webp"
        # it failed there, or was too big to keep: drawn here, or waited for again if another has begun meanwhile
    try:
        found = _make_page(*key)
        _remember(key, found)
        _keep_on_disk(key, found, ahead)
        return found
    finally:
        with _kept_lock:
            _drawing.pop(key, None)
        going.set()


def _have(key: tuple) -> bool:
    """Whether a page is in memory or on disk, or being drawn: then there's nothing to draw ahead."""
    with _kept_lock:
        if key in _kept or key in _drawing:
            return True
    target = _drawn_file(*key)
    return target is not None and target.is_file()


# ---- Drawing ahead -----------------------------------------------------------------------------------------------------------
# With each turn the browser says which pages it expects to want next (/api/ahead, draw_ahead): those are drawn here, one at
# a time, while nobody waits for a page, so that they're in memory and on disk when they're asked for. Everyone's next page
# comes before anyone's page further on, so readers share the time without being counted, and the more people read, the
# less is drawn ahead. A reader is a number the browser makes up for the book it has opened, not a name, kept in memory only
# while there are pages to draw for it, and at most AHEAD_FOR. The browser decides how far: drawAhead in catalog_page.html.
# Drawing ahead is at a low priority for the processor and the disk, which also plays films, and stops on battery.
AHEAD_READERS = 32  # readers whose pages are drawn ahead at once; the one heard from longest ago goes
AHEAD_FOR = 900.0  # seconds
_guesses: OrderedDict[str, dict] = OrderedDict()  # reader -> {"at", "book": (path, stamp, kind, width), "pages": deque of (distance, n)}
_asked_now = [0]  # pages being drawn for someone who asked for them: drawing ahead waits for them
_ahead_cond = threading.Condition()
_ahead_worker: list[threading.Thread | None] = [None]


def draw_ahead(path: Path, n: int, asked_width: int, count: int, way: int, reader_id: str) -> int:
    """The count pages after page n, going way (1 on, -1 back), at the width asked for, to be drawn ahead for this reader:
    in place of what it asked for before. At most [reader] draw_ahead pages; how many it will be is returned."""
    stamp = _stamp(path)
    kind = _kind(str(path), stamp)
    count = 0 if on_battery() else max(0, min(count, settings.get("reader")["draw_ahead"]))
    way = -1 if way < 0 else 1
    pages = deque((i, n + way * i) for i in range(1, count + 1) if n + way * i >= 0)
    with _ahead_cond:
        _guesses.pop(reader_id, None)
        now = time.monotonic()
        for old in [r for r, g in _guesses.items() if now - g["at"] > AHEAD_FOR]:
            del _guesses[old]
        if pages:
            _guesses[reader_id] = {"at": now, "book": (str(path), stamp, kind, width_for(asked_width)), "pages": pages}
            while len(_guesses) > AHEAD_READERS:
                _guesses.popitem(last=False)
            worker = _ahead_worker[0]
            if worker is None or not worker.is_alive():
                _ahead_worker[0] = threading.Thread(target=_ahead_loop, daemon=True, name="draw-ahead")
                _ahead_worker[0].start()
            _ahead_cond.notify_all()
    return len(pages)


def _next_guess() -> tuple[str, tuple] | None:
    """The page nearest to where its reader is, of all the readers', taken from its list; None while someone waits for a
    page they asked for, or when there's nothing to draw. Called with _ahead_cond held."""
    if _asked_now[0]:
        return None
    waiting = [(g["pages"][0][0], i, r) for i, (r, g) in enumerate(_guesses.items()) if g["pages"]]
    if not waiting:
        return None
    reader_id = min(waiting)[2]  # the nearest; of those as near, the reader heard from longest ago
    guess = _guesses[reader_id]
    _, n = guess["pages"].popleft()
    if not guess["pages"]:
        del _guesses[reader_id]  # all its pages are on their way: nothing more is kept of it
    path, stamp, kind, width = guess["book"]
    return reader_id, (path, stamp, kind, n, width)


def _ahead_loop() -> None:
    me = threading.current_thread()
    _lower_priority()
    while True:
        with _ahead_cond:
            job = None
            while _ahead_worker[0] is me and (job := _next_guess()) is None:
                _ahead_cond.wait(60)
            if _ahead_worker[0] is not me:
                return  # another has taken over
        reader_id, key = job
        try:
            if _have(key):
                continue
            path, stamp, kind = key[:3]
            if kind == "rar" and _solid(path, stamp) and _unpacked(path, stamp) is None and _being_unpacked(path, stamp):
                continue  # being unpacked: its pages come quickly once it is, and slowly from the archive before (one that
                # couldn't be unpacked is drawn from the archive, as its pages asked for are)
            _page(*key, ahead=True)
        except IndexError:  # past the last page: none further on either
            with _ahead_cond:
                guess = _guesses.get(reader_id)
                if guess and guess["book"][:3] == key[:3]:
                    del _guesses[reader_id]
        except Exception:  # noqa: BLE001 - a page that can't be drawn ahead is drawn, or fails, when it's asked for
            pass


IOPRIO_SET = {"x86_64": 251, "aarch64": 30}  # Linux's ioprio_set, by the processor's kind


def _lower_priority() -> None:
    """Drawing ahead gets less of the processor and the disk than anything else, where the system allows that for one
    thread (Linux and Windows); but never none, as someone may be waiting for the page it's drawing. A PDF's page is drawn
    by a worker of its own (pdfworker.py), which this doesn't reach."""
    try:
        if sys.platform.startswith("linux"):
            import ctypes
            import platform

            me = threading.get_native_id()
            os.setpriority(os.PRIO_PROCESS, me, 10)
            if platform.machine() in IOPRIO_SET:  # IOPRIO_WHO_PROCESS (1), for one thread: best effort (2), its lowest (7)
                ctypes.CDLL(None, use_errno=True).syscall(IOPRIO_SET[platform.machine()], 1, me, (2 << 13) | 7)
        elif os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -2)  # THREAD_PRIORITY_LOWEST
    except (OSError, AttributeError):
        pass


_power = {"at": -1e9, "battery": False}


def on_battery() -> bool:
    """Whether this computer runs on its battery now (looked at every LOOK_EVERY): then nothing is drawn ahead."""
    now = time.monotonic()
    if now - _power["at"] >= LOOK_EVERY:
        try:
            import psutil

            said = psutil.sensors_battery()
            battery = bool(said and said.power_plugged is False)
        except Exception:  # noqa: BLE001 - one that won't say runs on the mains, as a desktop does
            battery = False
        _power.update(at=now, battery=battery)
    return _power["battery"]


def status() -> dict:
    """How the pages came (STATS), what the cache holds on disk and may hold, and how far pages are drawn ahead."""
    with _stats_lock:
        said, widths = dict(STATS), sorted(WIDTHS_ASKED.items(), key=lambda kv: -kv[1])
    try:
        held = disk_used()
    except OSError:  # a count that fails (its disk in trouble, a folder gone midway): the rest is said all the same
        held = 0
    root = cache_root()
    return {**said, "widths": widths[:6], "disk_held": held, "disk_limit": disk_limit(held),
            "folder": str(root or ""), "problem": cache_problem(), "draw_ahead": settings.get("reader")["draw_ahead"],
            "battery": on_battery()}


def _make_page(path: str, stamp: tuple, kind: str, n: int, width: int) -> tuple[bytes, str]:
    if kind in ("pdf", "book"):
        return pdfworker.page(path, stamp, n, width), "image/webp"
    names = _names(path, stamp, kind)
    if not 0 <= n < len(names):
        raise IndexError(n)
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            data = archives.read_member(z, names[n], MAX_PICTURE)
    else:  # a page deep in a solid archive waits a little for its unpacking; one that couldn't be unpacked doesn't
        folder = _unpacked(path, stamp, UNPACK_WAIT if n > 20 else 0) if _solid(path, stamp) else None
        if folder is not None:
            folder.touch()  # read lately: the last to leave the cache
        data = _picture(_inside(folder, names[n])) if folder is not None else _rar_read(path, names[n])
    return _scaled(data, width)


def _picture(path: Path) -> bytes:
    if path.stat().st_size > MAX_PICTURE:
        raise Unreadable("A page in the archive is far bigger than a page can be.")
    return path.read_bytes()


def _rar_read(path: str, name: str) -> bytes:
    import rarfile

    try:
        with archives.open_rar(path) as r:
            return archives.rar_member(r, name, MAX_PICTURE)
    except rarfile.Error:  # couldn't be unpacked: for want of 7-Zip's RAR part, it says so; any other trouble is as it was
        _unpacks(path, name)
        raise


@functools.lru_cache(maxsize=256)
def _solid(path: str, stamp: tuple) -> bool:
    with archives.open_rar(path) as r:
        return r.is_solid()


def page(path: Path, n: int, asked_width: int) -> tuple[bytes, str]:
    """Page n (from 0), at most asked_width wide (rounded up to one of WIDTHS)."""
    stamp = _stamp(path)
    try:
        return _page(str(path), stamp, _kind(str(path), stamp), n, width_for(asked_width))
    except (IndexError, Unreadable):
        raise
    except Exception as e:  # noqa: BLE001
        raise Unreadable(f"The page couldn't be read ({type(e).__name__}).") from e
