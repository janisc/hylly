"""PDFs and e-books drawn in a process of their own, so that a bad book can cost only its own page.

MuPDF is a big C library reading files from anywhere. Here, unlike inside the server:
- a crash costs one page: the worker goes, and the next page starts another;
- a page that never ends is stopped after LIMIT seconds, and one that wants all the memory gets an error (MEMORY);
- a slow file holds up nobody else: it gets one worker at a time, of two or more (WORKERS: but one on a computer of
  under 3 GB, capacity), and MuPDF would keep all of Python waiting while it drew.

It isn't a sandbox. A worker runs as you, and whatever a flaw in MuPDF let a file do, it could do to what you can
reach; keeping MuPDF new is still what stops that (updates.py). So what is checked here, what a worker says (JSON and
bytes, of so much at most, none of it ever run as pickle would be) and how long it takes, is for the server to stay up
whatever becomes of a worker.

What stopped a worker is left alone for a while, so that a browser asking again can't keep starting workers, or keep
them busy until the limit: a page that crashed one for FORGET seconds, at any width, which is the file's doing, and one
that took too long for FORGET_LATE, at that width, which a share's stall can be. MAX_STOPS different pages within FORGET
seconds, and the whole file is left alone. The file changing ends that at once. A worker that has already read other
files is given no blame: a new one tries again first.

Only the standard library at the top, and archives, which brings no more than the core: every command imports this.
"""

from __future__ import annotations

import atexit
import contextlib
import faulthandler
import io
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path

from . import archives
from .archives import Unreadable

LIMIT = 120.0  # seconds for anything asked of a worker: far over any real page, and more than a share's stall
START_LIMIT = 60.0  # seconds for a worker to start: its libraries may have to come off a slow disk
PATIENCE = 120.0  # seconds a reader's page waits for its turn: after that it is told to try again. The catalog just waits
IDLE = 300.0  # seconds unused until a worker goes, and with it its memory and the files it keeps open
RECYCLE = 2000  # things asked of one worker: a long catalog build gets a fresh one now and then
FULL = 3 << 30  # a worker's memory where the computer has it: a page drawn is at most 120 MB, a book from inside a ZIP at most 1 GB
FORGET = 600.0  # seconds a page or a file that crashed a worker is left alone
FORGET_LATE = 120.0  # and one that only took too long
MAX_STOPS = 3  # different pages of a file stopping a worker within FORGET seconds, and the whole file is left alone
OPEN_DOCUMENTS = 4  # kept open by a worker, so that turning pages doesn't parse the file again
MAX_HEAD = 16 << 20  # what a worker may answer: a head (a long table of contents), and bytes (a page)
MAX_ANSWER = 128 << 20
MAX_ASKED = 2 << 30  # what a worker is sent: a book from inside a ZIP package
_SIZES = struct.Struct(">IQ")  # the size of the head, and of the bytes after it
OUTLINE = "outline"  # a stop while reading a file's table of contents, which the file reads without
MAX_SECTIONS = 20000  # of a book, as a worker tells them: more than this says nothing useful
MAX_PAGES = 100_000  # a file's own word on its pages, past which the catalog and the reader take it for a broken or a made-up one
MAX_PIXELS = 40_000_000  # one page drawn or decoded: a PDF page, a comic page, a cover (real ones are under 25)
BOOK_PAGE = (420, 640, 11)  # an e-book page laid out by MuPDF: width and height in points, and the font size
MAX_SIZED = 500 << 20  # an EPUB's files unpacked, all together, past which it isn't copied to be sized by the page
# What a file begins with, so that it is opened as what its name says: MuPDF goes by what it finds in a file, whatever
# its name or the type it's told, and would open a picture, an SVG, an XPS or an office file named .pdf or .epub too. A
# PDF has its "%PDF-" whole within its first HEADER bytes, as reader._kind looks for it: a few of something else may come
# first (a line end, a Mac's or a mail's header), up to 1,019 bytes, and MuPDF then takes it for a PDF, whatever else it
# holds. An e-book has, by its name, one of these at that byte; an FB2 is XML in any encoding, with nothing certain at
# its start. What MuPDF made of a file says the rest (_opened: is_pdf, is_reflowable).
HEADER = 1024
E_BOOKS = {"epub": ((0, b"PK"),), "fb2": (),
           **dict.fromkeys(("mobi", "azw3", "azw", "prc"), ((60, b"BOOKMOBI"), (60, b"TEXtREAd")))}  # a PalmDOC is a MOBI to MuPDF
# A worker's report of a failure that is the share's or the computer's and not the file's: the catalog doesn't keep those.
NOT_THE_FILES = {"FzErrorSystem", "OSError", "FileNotFoundError", "PermissionError", "TimeoutError", "ConnectionError", "MemoryError"}
_limited = False  # in a worker: whether limit_memory took


def this_computer() -> tuple[int | None, int | None]:
    """Its cores, and its memory in bytes, as the system says them; None for what it won't say."""
    memory = None
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32), ("total", ctypes.c_uint64),
                            ("rest", ctypes.c_uint64 * 6)]

            status = Status(length=ctypes.sizeof(Status))
            memory = int(status.total) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
        else:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        pass
    return os.cpu_count(), memory if memory and memory > 0 else None


def capacity(cores: int | None, memory: int | None) -> tuple[int, int]:
    """How many workers at a time, and each one's memory (MEMORY), for a computer of so many cores and so much memory:
    a book's pages drawn while another is stuck takes two, and more cores take more people reading at once, up to four
    with eight cores; as many as the memory has room for at FULL each, together at most nine tenths of it. Under 3 GB,
    one, with nine tenths of the memory at most, where FULL would protect nothing. A computer that won't say, or says
    less than SAID_LEAST (a reading that can't be right), gets two at FULL, as all did before."""
    if not memory or memory < SAID_LEAST:
        return 2, FULL
    room = memory * 9 // 10
    workers = 1 if memory < 3 << 30 else max(2, min(4, (cores or 4) // 2, room // FULL))
    return workers, min(FULL, room // workers)


SAID_LEAST = 512 << 20  # memory said below this is no reading to go by: Hylly's server alone takes more
WORKERS, MEMORY = capacity(*this_computer())  # 2 at 3 GB each on a computer of 4 cores and 7.5 GB


class NotNow(Unreadable):
    """It can't be read just now, which says nothing lasting about the file: no worker, too busy, too slow."""


class Stopped(Exception):
    """A worker that didn't answer: it crashed, took too long, or sent what no worker would."""

    def __init__(self, message: str, late: bool = False) -> None:
        super().__init__(message)
        self.late = late


class NoWorker(Exception):
    """A worker that can't even start, which says nothing about the file asked for."""


# ---- what goes between them -------------------------------------------------------------------

def _send(out, head: dict, data: bytes = b"") -> None:
    text = json.dumps(head).encode("ascii")  # a name that isn't proper text comes through as it is
    out.write(_SIZES.pack(len(text), len(data)) + text)
    if data:
        out.write(data)
    out.flush()


def _exactly(source, size: int) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise EOFError
    return data


def _receive(source, max_data: int) -> tuple[dict, bytes]:
    head_size, data_size = _SIZES.unpack(_exactly(source, _SIZES.size))
    if head_size > MAX_HEAD or data_size > max_data:
        raise ValueError("far too long")
    head = json.loads(_exactly(source, head_size))
    if not isinstance(head, dict):
        raise ValueError("not a head")
    return head, _exactly(source, data_size)


# ---- the worker's side ------------------------------------------------------------------------

def limit_memory(limit: int = MEMORY) -> bool:
    """So much memory for this process and no more, where the system lets it be said: asking for more then fails, and
    that is an error about one page. True if it was said. (macOS doesn't take such a limit.)"""
    try:
        if sys.platform == "win32":
            return _limit_memory_windows(limit)
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # and no copy of a crashed worker's memory on the disk
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return sys.platform != "darwin"
    except (OSError, ValueError, AttributeError):
        return False


def _limit_memory_windows(limit: int) -> bool:
    import ctypes
    from ctypes import c_longlong, c_size_t, c_ulonglong, wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", c_longlong), ("job_time", c_longlong), ("flags", wintypes.DWORD),
                    ("min_set", c_size_t), ("max_set", c_size_t), ("processes", wintypes.DWORD),
                    ("affinity", c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", c_ulonglong * 6), ("process_memory", c_size_t), ("job_memory", c_size_t),
                    ("peak_process", c_size_t), ("peak_job", c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)  # stays open as long as this process does
    if not job:
        return False
    info = Extended()
    info.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.process_memory = limit
    extended_limits = 9  # JobObjectExtendedLimitInformation
    return bool(kernel.SetInformationJobObject(job, extended_limits, ctypes.byref(info), ctypes.sizeof(info))
                and kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()))


VIEWPORT = re.compile(rb"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)(vh|vw|vmin|vmax)(?![\w-])", re.IGNORECASE)
STYLED = (".css", ".xhtml", ".html", ".htm", ".xml")


def sized_by_the_page(path, width: float, height: float) -> bytes | None:
    """An EPUB whose styles size things by the screen (vh, vw), which MuPDF takes for points: a cover of 99vh came out a
    sixth of the page. A copy, in memory, with those as points of the page MuPDF lays it out on, as a browser would have
    them; None for a book with none, which opens as it is. And None for one whose files would unpack past MAX_SIZED, or
    that has one a ZIP's reader refuses (archives.read_member: a bomb, a packing it doesn't open): the copy is kept as
    long as the book is open, so it isn't made of whatever the book says it holds. That book opens as it is too."""
    if not str(path).lower().endswith(".epub"):
        return None
    try:
        with zipfile.ZipFile(path) as book:
            files = book.infolist()
            if sum(i.file_size for i in files) > MAX_SIZED or len({i.filename for i in files}) < len(files):
                return None  # two files of one name: each is read by its name, which is the last of them
            styled = [i for i in files if i.filename.lower().endswith(STYLED) and i.file_size < 8 << 20]
            if not any(VIEWPORT.search(archives.read_member(book, i.filename, 8 << 20)) for i in styled):
                return None
            each = {"vh": height, "vw": width, "vmin": min(width, height), "vmax": max(width, height)}
            points = lambda m: b"%.2fpt" % (float(m[1]) * each[m[2].lower().decode()] / 100)  # noqa: E731
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as copy:  # stored: the pictures are packed already
                names = {i.filename for i in styled}
                for i in files:
                    data = archives.read_member(book, i.filename, MAX_SIZED)
                    copy.writestr(i.filename, VIEWPORT.sub(points, data) if i.filename in names else data)
            return out.getvalue()
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError, Unreadable):
        return None  # MuPDF opens it as it is, and says what's wrong with it


def _kind(path: str) -> str:
    """What a file is to be opened as, by its name: an e-book's kind (E_BOOKS), or "pdf" for anything else the reader
    sends, which is a PDF under whatever name (reader._kind)."""
    kind = os.path.splitext(path)[1].lower().lstrip(".")
    return kind if kind in E_BOOKS else "pdf"


def _opened(path: str, kind: str, data: bytes | None = None):
    """The file, or data (its bytes), opened by MuPDF as kind says, a PDF or an e-book: Unreadable if it doesn't begin as
    one (HEADER, E_BOOKS), or if MuPDF made something else of it all the same."""
    import pymupdf

    if data is None:
        with open(path, "rb") as fh:
            head = fh.read(HEADER)
    else:
        head = data[:HEADER]
    book = kind != "pdf"
    said = "This file isn't the e-book its name says." if book else "This file isn't a PDF inside."
    if book:
        begins = not E_BOOKS[kind] or any(head[at:at + len(start)] == start for at, start in E_BOOKS[kind])
    else:
        begins = b"%PDF-" in head
    if not begins:
        raise Unreadable(said)
    doc = pymupdf.open(path) if data is None else pymupdf.open(stream=data, filetype="pdf")
    if not (doc.is_reflowable if book else doc.is_pdf):
        doc.close()
        raise Unreadable(said)
    return doc


def _document(ask: dict, open_ones: OrderedDict):
    """An open document, a PDF or an e-book laid out as pages."""
    import pymupdf

    key = (ask["path"], tuple(ask["stamp"]))
    if key in open_ones:
        open_ones.move_to_end(key)
        return open_ones[key]
    doc = _opened(ask["path"], _kind(ask["path"]))
    if doc.needs_pass:
        doc.close()
        raise Unreadable("This PDF is locked with a password.")
    if doc.is_reflowable:
        width, height, size = BOOK_PAGE
        sized = sized_by_the_page(ask["path"], width, height)
        if sized is not None:
            doc.close()
            doc = pymupdf.open(stream=sized, filetype="epub")
        doc.layout(width=width, height=height, fontsize=size)
    open_ones[key] = doc
    while len(open_ones) > OPEN_DOCUMENTS:
        open_ones.popitem(last=False)[1].close()
    return doc


def _forget_document(ask: dict, open_ones: OrderedDict) -> None:
    """One that failed may go on failing: its share dropped, say, and took what it was opened by along. Next time it is
    opened anew."""
    doc = open_ones.pop((ask.get("path"), tuple(ask.get("stamp") or ())), None)
    if doc is not None:
        with contextlib.suppress(Exception):
            doc.close()


def page_zoom(page, width: int) -> float:
    """Points to pixels for a PDF page drawn width wide: a tiny page isn't blown up beyond reason (8 times), nor a
    very tall one into a picture of gigabytes (MAX_PIXELS at most)."""
    zoom = min(width / max(page.rect.width, 1), 8)
    return min(zoom, (MAX_PIXELS / max(page.rect.width * page.rect.height, 1)) ** 0.5)


WEBP_EFFORT = 2  # Pillow's method, 0-6. At its 4, the encoding was most of a page's drawing time; 2 takes about 55-60%
# less on real comic and PDF pages, for files 2-4% larger; 1 saves little more, for files 10-20% larger


def webp(im) -> bytes:
    """A page drawn here, or a comic's page scaled by the reader, as the WebP every page is sent as."""
    out = io.BytesIO()
    im.save(out, "WEBP", quality=82, method=WEBP_EFFORT)
    return out.getvalue()


def _page(doc, n: int, width: int) -> bytes:
    import pymupdf
    from PIL import Image

    page = doc[n]
    zoom = page_zoom(page, width)
    pixels = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return webp(Image.frombytes("RGB", (pixels.width, pixels.height), pixels.samples))


def _details(ask: dict, data: bytes) -> tuple[dict, bytes]:
    """A file's details and its first page, for the catalog. Not kept open: the next file is another. A PDF that says it
    has more than MAX_PAGES is unreadable: the number is only the file's word, and the catalog would show it as its own."""
    import pymupdf

    with _opened(ask["path"], "pdf", data if ask.get("packed") else None) as doc:
        if doc.page_count > MAX_PAGES:
            raise Unreadable(f"This file says it has {doc.page_count:,} pages, more than any book has.")
        head, cover = {"info": doc.metadata or {}, "pages": doc.page_count}, b""
        if doc.page_count:
            page = doc[0]
            zoom = page_zoom(page, ask["width"])  # a very tall first page isn't drawn into gigabytes
            cover = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    return head, cover


def _do(ask: dict, data: bytes, open_ones: OrderedDict) -> tuple[dict, bytes]:
    what = ask["do"]
    if what == "versions":
        import pymupdf

        return {"pymupdf": pymupdf.__version__, "mupdf": pymupdf.mupdf_version, "limited": _limited}, b""
    if what == "details":
        return _details(ask, data)
    doc = _document(ask, open_ones)
    if what == "pages":
        return {"pages": doc.page_count}, b""
    if what == "outline":
        return {"toc": doc.get_toc(simple=True)[:2000]}, b""
    if what == "sections":
        return {"sections": [doc.chapter_page_count(i) for i in range(doc.chapter_count)][:MAX_SECTIONS]}, b""
    n = ask["n"]
    if not 0 <= n < doc.page_count:
        return {"no_page": True}, b""
    return {}, _page(doc, n, ask["width"])


def main() -> None:
    """A worker: answers what it is asked, one thing at a time, until the server closes its end."""
    global _limited
    answers, asked = os.fdopen(os.dup(1), "wb"), os.fdopen(os.dup(0), "rb")
    nowhere = open(os.devnull, "r+b")  # MuPDF and PyMuPDF print: none of it may land among the answers
    os.dup2(nowhere.fileno(), 0)
    os.dup2(nowhere.fileno(), 1)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C in a terminal is for the server: it ends here when that does
    _limited = limit_memory()
    try:
        import pymupdf

        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception as e:  # noqa: BLE001 - it says why, and then there's nothing it can do
        _send(answers, {"cannot": f"{type(e).__name__}: {e}"})
        return
    _send(answers, {"ready": True})
    open_ones: OrderedDict = OrderedDict()
    while True:
        try:
            ask, data = _receive(asked, MAX_ASKED)
        except (EOFError, OSError, ValueError):  # the server has gone, in whatever way
            return
        # And should the server go while this is stuck, nobody would be left to stop it. This runs apart from Python,
        # which MuPDF keeps waiting.
        faulthandler.dump_traceback_later(float(ask.get("seconds", LIMIT)) + 5, exit=True, file=nowhere)
        try:
            head, body = _do(ask, data, open_ones)
        except Unreadable as e:
            head, body = {"says": str(e)}, b""
        except Exception as e:  # noqa: BLE001 - a broken file, in whichever library noticed: this one, not the worker
            head, body = {"failed": type(e).__name__}, b""
            _forget_document(ask, open_ones)
        faulthandler.cancel_dump_traceback_later()
        del data  # a book from inside a ZIP: not to be kept while idle
        try:
            _send(answers, head, body)
        except OSError:
            return


# ---- the server's side ------------------------------------------------------------------------

def _command() -> list[str]:
    """Isolated (-I), so nothing in the folder it starts from, in PYTHONPATH or among your own packages stands in for
    the libraries. With the folder this Hylly is in, as the worker must be this one."""
    return [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from hylly.pdfworker import main; main()",
            str(Path(__file__).resolve().parents[1])]


class _Worker:
    def __init__(self) -> None:
        self.late, self.stuck, self.asked, self.file, self.used = False, False, 0, None, time.monotonic()
        try:
            self.process = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            cwd=str(Path(sys.executable).parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise NoWorker(str(e)) from e
        try:
            hello = self._answer(START_LIMIT)[0]
        except Stopped as e:
            self.close(kill=True)
            raise NoWorker("it stopped as it started") from e
        if not hello.get("ready"):
            self.close(kill=True)
            raise NoWorker(str(hello.get("cannot") or "it said something else")[:300])

    def _answer(self, seconds: float, head: dict | None = None, data: bytes = b"") -> tuple[dict, bytes]:
        """Its answer, in a thread of its own so that the time is kept whatever happens: stopping a worker usually ends
        the waiting too, but not if something else holds its pipe open, a program it started or a share's stall."""
        said: list = []

        def talk() -> None:
            with contextlib.suppress(Exception):
                if head is not None:
                    _send(self.process.stdin, head, data)
                said.append(_receive(self.process.stdout, MAX_ANSWER))

        talking = threading.Thread(target=talk, daemon=True, name="pdf-answer")
        talking.start()
        talking.join(seconds)
        if talking.is_alive():
            self.late = True
            self.process.kill()
            talking.join(1)
            self.stuck = talking.is_alive()
        if said and not self.late:
            return said[0]
        raise Stopped("This file took too long to read, so Hylly gave up on it." if self.late else
                      "This file stopped the part of Hylly that draws PDFs. Nothing else is affected.", late=self.late)

    def ask(self, head: dict, data: bytes = b"") -> tuple[dict, bytes]:
        self.asked += 1
        return self._answer(LIMIT, {**head, "seconds": LIMIT}, data)

    def close(self, kill: bool = False) -> None:
        """Its end closed, it stops by itself; if not, or if it's to go now, it is stopped."""
        process = self.process
        try:
            if not kill and not self.stuck:
                process.stdin.close()
                process.wait(2)
            else:
                kill = True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            kill = True
        try:
            if kill:
                process.kill()
                process.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not self.stuck:  # the thread still waiting has those in hand, and closing them would wait for it
            for pipe in (process.stdin, process.stdout):
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()


class Pool:
    """The workers: started when wanted, going when unused for a while."""

    def __init__(self, size: int | None = None) -> None:
        self.room = threading.BoundedSemaphore(size or WORKERS)
        self.lock = threading.Lock()
        self.idle: list[_Worker] = []
        self.tidying = threading.Event()  # set while something is seeing to the idle ones
        self.closed = threading.Event()

    def ask(self, head: dict, data: bytes = b"", patience: float | None = None) -> tuple[dict, bytes]:
        if not self.room.acquire(timeout=patience):
            raise NotNow("Hylly is busy drawing other pages. Try again in a moment.")
        try:
            path = head.get("path")
            worker = self._take(path)
            fresh = worker.asked == 0
            try:
                answer = worker.ask(head, data)
            except Stopped as e:
                worker.close(kill=True)
                if fresh or e.late:
                    raise
                worker = _Worker()  # it had read other things first, and it may be those it ended by: a new one tells
                try:
                    answer = worker.ask(head, data)
                except Stopped:
                    worker.close(kill=True)
                    raise
            self._give(worker, path)
            return answer
        finally:
            self.room.release()

    def _take(self, path: str | None) -> _Worker:
        """One that has this file open if there is one, as it needn't parse it again; else any; else a new one. One
        that has gone meanwhile is no fault of the file's."""
        with self.lock:
            dead = [w for w in self.idle if w.process.poll() is not None]
            self.idle = [w for w in self.idle if w not in dead]
            worker = next((w for w in self.idle if w.file == path), self.idle[-1] if self.idle else None)
            if worker:
                self.idle.remove(worker)
        for w in dead:
            w.close(kill=True)
        return worker or _Worker()

    def _give(self, worker: _Worker, path: str | None) -> None:
        worker.file, worker.used = path, time.monotonic()
        if worker.asked >= RECYCLE:
            worker.close()
            return
        with self.lock:
            self.idle.append(worker)
            start = not self.tidying.is_set()
            self.tidying.set()
        if start:
            threading.Thread(target=self._keep_tidy, args=(self.closed,), daemon=True, name="pdf-workers").start()

    def tidy(self) -> None:
        with self.lock:
            now = time.monotonic()
            old = [w for w in self.idle if now - w.used >= IDLE]
            self.idle = [w for w in self.idle if w not in old]
        for w in old:
            w.close()

    def _keep_tidy(self, closed: threading.Event) -> None:
        while not closed.wait(min(IDLE, 30)):
            self.tidy()

    def close(self) -> None:
        """The idle ones go, and what sees to them; it can be used again after this."""
        with self.lock:
            workers, self.idle = self.idle, []
            closed, self.closed = self.closed, threading.Event()
            self.tidying.clear()
        closed.set()
        for w in workers:
            w.close()


POOL = Pool()
atexit.register(lambda: POOL.close())
# (path, stamp) -> what stopped a worker: until when that is left alone, and until when it counts towards MAX_STOPS.
# None: opening the file. OUTLINE: its table of contents. (page, width); (page, None): the page at any width.
_stops: dict[tuple, dict] = {}
_stops_lock = threading.Lock()
_turns: dict[tuple, list] = {}  # (path, stamp) -> a lock, and how many have it or wait for it
_turns_lock = threading.Lock()


def _left_alone(key: tuple, where) -> bool:
    with _stops_lock:
        seen = _stops.get(key)
        if not seen:
            return False
        now = time.monotonic()
        for what in [what for what, (_, counts) in seen.items() if counts <= now]:
            del seen[what]
        if not seen:
            del _stops[key]
            return False
        alone = {what for what, (until, _) in seen.items() if until > now}
        if None in alone or where in alone or (isinstance(where, tuple) and (where[0], None) in alone):
            return True
        return len({what[0] for what in seen if isinstance(what, tuple)}) >= MAX_STOPS


def _note_stop(key: tuple, where, why: str, late: bool) -> None:
    from .storage import shown  # the one cleaner of a log line; only the standard library at the top

    with _stops_lock:
        while len(_stops) > 500:
            del _stops[next(iter(_stops))]
        now = time.monotonic()
        said = (where[0], None) if isinstance(where, tuple) and not late else where  # a crash is the page's, whatever its width
        _stops.setdefault(key, {})[said] = (now + (FORGET_LATE if late else FORGET), now + FORGET)
    name = shown(key[0])  # a file's name can't start a line of its own in the server's log
    print(f"{name}{f', page {where[0] + 1}' if isinstance(where, tuple) else ''}: {why}", file=sys.stderr, flush=True)


@contextlib.contextmanager
def _turn(key: tuple, patience: float | None):
    """One thing of a file at a time, so a file that hangs can take up one worker and no more, whoever is asking."""
    with _turns_lock:
        turn = _turns.setdefault(key, [threading.Lock(), 0])
        turn[1] += 1
    try:
        if not turn[0].acquire(timeout=-1 if patience is None else patience):
            raise NotNow("Hylly is still busy with this file. Try again in a moment.")
        try:
            yield
        finally:
            turn[0].release()
    finally:
        with _turns_lock:
            turn[1] -= 1
            if not turn[1]:
                _turns.pop(key, None)


def _ask(path: str, stamp: tuple, where, head: dict, data: bytes = b"", patience: float | None = None) -> tuple[dict, bytes]:
    key = (path, tuple(stamp))
    with _turn(key, patience):
        if _left_alone(key, where):
            raise NotNow("This has stopped the part of Hylly that draws PDFs before, so it's left alone for now.")
        try:
            answer, body = POOL.ask({**head, "path": path, "stamp": list(stamp)}, data, patience)
        except Stopped as e:
            _note_stop(key, where, str(e), e.late)
            raise (NotNow if e.late else Unreadable)(str(e)) from e
        except NoWorker as e:
            raise NotNow(f"The part of Hylly that draws PDFs can't start ({e}). hylly doctor may say more.") from e
    if answer.get("says"):
        raise Unreadable(str(answer["says"])[:300])
    if answer.get("failed"):
        failed = str(answer["failed"])[:80]
        raise (NotNow if failed in NOT_THE_FILES else Unreadable)(f"The file couldn't be read ({failed}).")
    return answer, body


def pages(path: str, stamp: tuple) -> int:
    """How many pages. Reads a PDF's table of objects, not all of it."""
    count = _ask(path, stamp, None, {"do": "pages"}, patience=PATIENCE)[0].get("pages")
    if type(count) is not int or count < 0:
        raise Unreadable("The file couldn't be read.")
    return count


def outline(path: str, stamp: tuple) -> list:
    """A file's own table of contents, as [level, title, page from 1]. Check its shape: it is a worker's word."""
    toc = _ask(path, stamp, OUTLINE, {"do": "outline"}, patience=PATIENCE)[0].get("toc")
    return toc if isinstance(toc, list) else []


def sections(path: str, stamp: tuple) -> list[int]:
    """How many of its pages each part of a book takes, in the order of the file: an e-book's parts can be very unlike in
    length, so its pages can't be told from how far through its text you are. Check their shape: it is a worker's word."""
    found = _ask(path, stamp, OUTLINE, {"do": "sections"}, patience=PATIENCE)[0].get("sections")
    return found if isinstance(found, list) and all(isinstance(n, int) and n >= 0 for n in found) else []


def page(path: str, stamp: tuple, n: int, width: int) -> bytes:
    """Page n (from 0), width wide at most, as WebP. IndexError if the file has no such page. By its width too: a page
    that takes too long zoomed in may do well enough otherwise."""
    head, body = _ask(path, stamp, (n, width), {"do": "page", "n": n, "width": width}, patience=PATIENCE)
    if head.get("no_page"):
        raise IndexError(n)
    if not body:
        raise Unreadable("The page couldn't be read.")
    return body


def details(path: str, stamp: tuple, width: int, data: bytes | None = None) -> tuple[dict, int, bytes]:
    """A PDF's details, its number of pages and its first page as a PNG: from the file, or from data, its bytes (a PDF
    inside a ZIP package, which path is then). The catalog has time, and waits for its turn however long it takes."""
    ask = {"do": "details", "width": width, "packed": data is not None}
    head, cover = _ask(path, stamp, None, ask, data or b"")
    info, count = head.get("info"), head.get("pages")
    return ({k: v for k, v in info.items() if isinstance(v, str)} if isinstance(info, dict) else {},
            count if type(count) is int and count >= 0 else 0, cover)


def versions() -> dict:
    """What draws the PDFs, by asking a worker: hylly doctor's proof that one can start. Raises NoWorker or Stopped."""
    return POOL.ask({"do": "versions"}, patience=PATIENCE)[0]
