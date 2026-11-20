"""Downloading every shop's files, resumable and checked against an MD5: the engine each shop's download gives its jobs
to, with a way to fetch a file's fresh link (refresh). What a shop has of its own, how it lists its files and asks for
their links, is in its own module, named by its id (shops.py).

A shop's listing is sometimes stale (Humble's: the CDN serves a newer file than the size and
MD5 it lists). The server's own answer therefore wins: its length decides when a file is
complete, and its ETag (an MD5, or an S3-style multipart MD5) is the checksum when the
shop's does not match. The manifest records which one confirmed each file.

But the server only vouches for what it sent, which may be a sign-in or error page. So an
empty answer is never taken for the file, and unless the shop's own MD5 says it is the file,
neither is a web page (the server says so, or it starts as one), one whose first bytes
aren't its kind's (FIRST_BYTES), nor one far smaller than the shop lists, unless the server's checksum vouches for it
and it is no mere page in size (VOUCHED_LEAST). A link, and each redirect it leads on to, is
followed only over https and only to the internet, never to this computer or the home network (fetching.py): that
file fails, and nothing of it is fetched.

The manifest (manifest.py, in the database) gets one record per finished file; the latest
for a path counts.

When the shop's own file is at fault (FileAtFault), a title's other formats are tried, best
first (a job's fallbacks: Humble's titles have them); the one that works is recorded with
"instead_of" (the chosen file's path), a substitute. Any other failure (a network or share
outage, too many retries, a shop turning it away, an old version in use) leaves the title failed, to be
tried again with the same choice. A substitute doesn't make its title done (shopping.select):
every run gives the chosen file one try, and the substitute stands until it comes, and stays
after (substitute). A full disk is no file's failure: it stops the run, as a library gone away does.

A file already in a job's place is recorded rather than fetched (adopted) only when it is the
file: by the shop's listing, where it gives a size or an MD5, else by the place's own record;
with no MD5 to go by, its own is recorded, as found (md5_source "adopted").

No shop's file of a kind on the never-list (never: programs, scripts and shortcuts) is downloaded, whatever the shop:
the selection leaves them out where the name is known before the run (shopping.select), and the Downloader leaves out
any that reaches it, a format behind a chosen file too. A file the shop names only in its link (StoryBundle's) is named
as its download starts (Downloader's name: shopping.Naming), by the link that then fetches it, and left out then if
that names one. Every run lists them (left_out), in its words and its log, and nowhere else: they are no failures, and
no record is made of them.
"""

from __future__ import annotations

import contextlib
import csv
import errno
import hashlib
import io
import itertools
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

import httpx

from . import USER_AGENT, db, fetching, paths
from .logins import AuthError, ShopError, retry_after
from .manifest import Manifest  # noqa: F401  (the record of what was downloaded: manifest.py, for those who only read it)
from .storage import atomic_write, sync
from .words import many, mb, size_words, time_words

# In 1,024s, for what is counted and that no person names: the pieces a file is read and written in, and the parts a
# shop's file was uploaded in (PART_SIZES_MIB). What a run says of a size, and every size a person sets, is in
# thousands: words.py.
MIB = 1024 * 1024
CHUNK = MIB
NETWORK_RETRIES = 8  # with backoff capped at 60 s: rides out about 4 minutes of network or share trouble
LINK_REFRESHES = 2
REFUSED_IN_A_ROW = 5  # files the shop turns away (REFUSED) one after another: it's blocking the run, so the run stops
# What the downloads count as a file turned away, in a run's words: a 401 or 403 still refused after the fresh links, a
# 429 past the retries, and a 403 or 429 when asked for a fresh link. A shop whose link requests are turned away with
# other answers too says its own (Library.refused: StoryBundle's)
REFUSED = "401, 403 or 429"
# files whose fresh link the shop didn't answer for at all, or only with its own trouble (5xx), each asked again first:
# it's down
UNANSWERED_IN_A_ROW = 3
LONGEST_PAUSE = 900  # the longest wait a shop's Retry-After is taken at; every download waits it out together


# Kinds no download takes from any shop, whatever the rules say: programs, scripts and shortcuts that Windows (or another
# system) runs or follows from a folder someone browses. Here, not in the rules, so that no rules file can let one in;
# the rules' [media] programs come on top of these (never).
NEVER = frozenset({
    "exe", "msi", "bat", "cmd", "com", "scr", "pif", "cpl", "lnk", "url", "scf", "ps1", "psm1", "vbs", "vbe", "js", "jse",
    "wsf", "wsh", "hta", "reg", "jar", "msp", "appref-ms", "application", "gadget", "inf", "settingcontent-ms",
    "library-ms", "searchconnector-ms", "sh", "desktop", "app", "dmg", "pkg", "deb", "rpm", "apk", "appimage",
    "msix", "msixbundle", "appx", "appxbundle", "appinstaller", "chm", "msc", "sct", "wsc", "ws", "xll", "website",
    "diagcab", "iso", "img", "vhd", "vhdx", "docm", "dotm", "xlsm",
    "xltm", "xlam", "pptm", "potm", "ppam", "ppsm", "sldm", "rdp", "command", "workflow", "scpt", "applescript"})
PROGRAM = "a program, script or shortcut"  # why a download leaves a file of a kind on the never-list


def kind_of(name: str) -> str:
    """A file's kind: the extension of the name it is saved under (paths.safe_filename), in small letters; "" for
    none. Windows drops the dots and spaces at a name's end, so "Setup.exe." and "Setup.exe " are both an EXE."""
    return PurePosixPath(paths.safe_filename(name).lower()).suffix.lstrip(".")


def never(kind: str) -> bool:
    """Whether no download takes a file of this kind (kind_of): NEVER, and the rules' [media] programs."""
    from . import rules

    return kind in NEVER or kind in rules.current().programs


@dataclass
class Left:
    """A shop's file a download leaves out: no failure, and listed by every run. Its kind on the never-list (PROGRAM),
    or the shop's own reason (why: itch.io's list of kinds to fetch, its own types, platform tags)."""
    title: str
    name: str  # the shop's name for it
    kind: str  # its extension as it would be saved (kind_of); "" for none
    there: bool = False  # downloaded by an earlier run, and still in the library: it stays, nothing is deleted
    key: object = None  # what the selection knows it by: shopping.select's (title's key, file's key); the downloader's, a path
    why: str = PROGRAM  # why no download takes it, in a run's words

    def line(self) -> str:
        """Its title, its name, its kind and why; in quotes, as a shop may write anything in them."""
        notes = [self.kind.upper() or "no extension", self.why] + (["downloaded before, and still there"] if self.there else [])
        return f"  {self.title!r}: {self.name!r} ({', '.join(notes)})"


def left_out(left: list[Left]) -> list[str]:
    """What a run says of the files it leaves out, every run, a dry run too: so that a character generator or a
    shortcut among a title's files doesn't go unnoticed, nor a file the shop's own rules leave."""
    if not left:
        return []
    n = len(left)
    what = (" of a kind no download takes (programs, scripts and shortcuts)" if all(x.why == PROGRAM for x in left)
            else " no download takes")
    return [f"Left out, not failures: {many(n, 'file')}{what}:"] + [x.line() for x in left]


def turned_away(how: str = REFUSED) -> str:
    """Why a run stopped: the shop turned REFUSED_IN_A_ROW requests in a row away. how: the HTTP answers counted."""
    return (f"The shop turned {REFUSED_IN_A_ROW} files in a row away (HTTP {how}), so the run stopped rather than go on "
            "asking. Try again later; what was downloaded is kept.")


def unanswered() -> str:
    """Why a run stopped: the shop didn't answer, or answered only with its own trouble (HTTP 5xx), when asked for
    UNANSWERED_IN_A_ROW files' links in a row."""
    return (f"The shop didn't answer, or answered only with its own trouble (HTTP 5xx), when asked for the links of "
            f"{UNANSWERED_IN_A_ROW} files in a row, each asked again first, so the run stopped rather than go on asking. "
            "Try again later; what was downloaded is kept.")


class DownloadError(Exception):
    """This file failed for good in this run."""


class Refused(DownloadError):
    """The shop turned this file away (HTTP 401, 403 or 429, even after a fresh link and waiting)."""


class FileAtFault(DownloadError):
    """The shop's own file is at fault: gone (HTTP 404 or 410 even with a fresh link), no longer listed (no link), or
    what came was wrong twice (its checksum, its first bytes or its size). Only then are the title's other formats
    tried: any other failure would hit them just the same, and would leave a second choice in the title's place."""


class NotTheFile(DownloadError):
    """What came isn't the file: a web page, or nothing at all. Nothing of it is kept; written: whether it went into the
    part file (else a part from before stays as it was)."""

    def __init__(self, why: str, written: bool = True):
        super().__init__(why)
        self.written = written


class LinkExpired(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class Retryable(Exception):
    def __init__(self, why: str, after: float = 0.0, status: int = 0):
        super().__init__(why)
        self.after, self.status = after, status  # after: the server's Retry-After, in seconds


class Stopped(Exception):
    pass


@dataclass
class Job:
    path: str  # relative to the library root, forward slashes
    folder: str  # the title's folder: one title, one folder
    url: str
    filename: str  # the shop's own name for the file (a fresh link from Humble is found again by it)
    size: int
    md5: str | None
    role: str  # book | extra
    label: str
    ext: str
    title: str
    bundle: str
    shop_id: str  # the shop's own id for what the file belongs to: Humble's machine name, else "<shop>:<id>[:<file>]"
    kind: str
    device: str
    purchased: str
    replace: bool = False  # an older version is on disk: move it to superseded\ first
    exact: bool = True  # size and md5 are the shop's exact listing (StoryBundle only gives a rounded size)
    extra: dict = field(default_factory=dict)  # more fields for the manifest entry
    fallbacks: list[Job] = field(default_factory=list)  # other formats, best first, tried if this one fails


def recorded_state(entry: dict | None, job: Job, dest: Path) -> str | None:
    """'done', 'changed' (the shop now lists another file) or None (not usable: download it). Done while the shop lists
    the MD5 it listed when the file was downloaded, or the file's own: a shop that corrects a stale MD5 to the one the
    file already has lists no other file (as shopping.current has it)."""
    if not entry or not dest.is_file() or dest.stat().st_size != entry.get("size"):
        return None
    if job.md5 is None:  # the shop lists no checksum: present with the recorded size is all we can know
        return "done"
    return "done" if job.md5 in (entry.get("listed_md5"), entry.get("md5")) else "changed"


def substitute(job: Job, manifest: Manifest, root: Path) -> Job | None:
    """The best of the job's fallbacks downloaded instead of it (its record's instead_of is the job's path) and still
    there as recorded, or None. One the shop has changed since counts too, and stands as it is: a substitute is never
    fetched again, --update or not."""
    for f in job.fallbacks:
        entry = manifest.get(f.path)
        if entry and entry.get("instead_of") == job.path and recorded_state(entry, f, root / f.path):
            return f
    return None


def md5_of(path: Path) -> "hashlib._Hash":
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h


PLAIN_MD5 = re.compile(r"^[0-9a-f]{32}$")
MULTIPART_MD5 = re.compile(r"^([0-9a-f]{32})-(\d+)$")
PART_SIZES_MIB = (5, 8, 10, 15, 16, 25, 32, 50, 64, 100, 128, 256, 512)


def clean_etag(value: str | None) -> str:
    value = (value or "").strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"').lower()


def multipart_etag_matches(path: Path, etag: str) -> bool:
    """Whether the file matches an S3-style ETag: MD5 of the parts' MD5s, then -<parts>."""
    m = MULTIPART_MD5.match(etag)
    if not m:
        return False
    want, parts = m.group(1), int(m.group(2))
    size = path.stat().st_size
    candidates = {mib * MIB for mib in PART_SIZES_MIB if math.ceil(size / (mib * MIB)) == parts}
    if parts > 1:
        guess = math.ceil(size / parts / MIB) * MIB
        if math.ceil(size / guess) == parts:
            candidates.add(guess)
    elif size:
        candidates.add(size)
    for part_size in sorted(candidates):
        digests = b""
        with path.open("rb") as fh:
            while True:
                h, left = hashlib.md5(), part_size
                while left:
                    block = fh.read(min(CHUNK, left))
                    if not block:
                        break
                    h.update(block)
                    left -= len(block)
                if left == part_size:
                    break
                digests += h.digest()
                if left:
                    break
        if hashlib.md5(digests).hexdigest() == want:
            return True
    return False


# How a file of each kind starts: (where, the bytes) for each way it may. A kind not here (txt, fb2 and md among them)
# has no mark of its own to go by, so it may be anything but a web page. A picture or an archive is often another kind
# than its name says, and what opens it goes by what's in it, so any picture will do for a picture and any archive for
# an archive or a comic: no web page starts as either does.
HEAD = 1024  # the bytes of a file looked at; a PDF's mark may come after a little leading matter, but within these
ANYWHERE = -1  # where: anywhere in the first HEAD bytes
AFTER_SPACE = -2  # where: at the start, after a byte order mark and spaces, as a text may have them
_ZIP = ((0, b"PK\x03\x04"), (0, b"PK\x05\x06"), (0, b"PK\x07\x08"))  # with files, an empty one, one in pieces
_ARCHIVE = _ZIP + ((0, b"Rar!\x1a\x07"), (0, b"7z\xbc\xaf\x27\x1c"))  # ZIP, RAR, 7z
_PICTURE = ((0, b"\xff\xd8\xff"), (0, b"\x89PNG\r\n\x1a\n"), (0, b"GIF87a"), (0, b"GIF89a"), (8, b"WEBP"))  # JPEG, PNG, GIF, WebP
_OFFICE = _ZIP + ((0, b"\xd0\xcf\x11\xe0"),)  # today's Office files, or the old ones under the new name
_MOBI = ((60, b"BOOKMOBI"), (60, b"TEXtREAd"))  # a Palm database's type and maker: Mobipocket's, or a plain PalmDOC's
FIRST_BYTES: dict[str, tuple[tuple[int, bytes], ...]] = {
    "pdf": ((ANYWHERE, b"%PDF-"),),
    "epub": _ZIP, "odt": _ZIP, "ods": _ZIP, "docx": _OFFICE, "xlsx": _OFFICE,
    "zip": _ARCHIVE, "rar": _ARCHIVE, "7z": _ARCHIVE, "cbz": _ARCHIVE, "cbr": _ARCHIVE, "cb7": _ARCHIVE,
    "mobi": _MOBI, "azw": _MOBI, "azw3": _MOBI, "prc": _MOBI,
    "djvu": ((0, b"AT&TFORM"),),
    "jpg": _PICTURE, "jpeg": _PICTURE, "png": _PICTURE, "gif": _PICTURE, "webp": _PICTURE,
    "rtf": ((AFTER_SPACE, b"{\\rtf"),),
}
WEB_PAGES = frozenset({"html", "htm", "xhtml", "xht", "shtml"})  # kinds that are web pages: for them, a page is the file
WEB_PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml"})
EMPTY_MD5 = hashlib.md5(b"").hexdigest()  # a shop that lists this for a file means an empty one
EMPTY = "an empty answer came instead of the file: nothing of it was kept"
FAR_SMALLER = 10  # a file under a tenth of the size the shop lists isn't the file it lists...
# ...unless the server's own checksum vouches for it whole and it is at least this big (a disk's MB): Humble's listings
# go stale by more than ten times (a comic's PDF made lighter, its listing left as it was), and no sign-in or error page
# is a megabyte
VOUCHED_LEAST = 1_000_000
# A file the shop lists with a placeholder where its name should be (<FILE_NAME>, _FILE_NAME_): broken at the shop's end
PLACEHOLDER = re.compile(r"(?:<|%3C|_)*FILE_?NAME(?:>|%3E|_)*", re.IGNORECASE)


def _text_start(head: bytes) -> bytes:
    return head.removeprefix(b"\xef\xbb\xbf").lstrip()


def starts_like_a_web_page(head: bytes) -> bool:
    """A web page's start, after a byte order mark and spaces: a sign-in or error page where a file should be."""
    return _text_start(head)[:14].lower().startswith((b"<!doctype html", b"<html"))


def _has(head: bytes, at: int, mark: bytes) -> bool:
    if at == ANYWHERE:
        return mark in head
    if at == AFTER_SPACE:
        return _text_start(head).startswith(mark)
    return head[at:at + len(mark)] == mark


def wrong_kind(ext: str, path: Path) -> tuple[str, str] | None:
    """('page', why) when the file starts as a web page does, ('bad', why) when its first bytes aren't its kind's (its
    extension's), or None."""
    ext = ext.lower()
    if ext in WEB_PAGES:
        return None
    with path.open("rb") as fh:
        head = fh.read(HEAD)
    if starts_like_a_web_page(head):
        return "page", "what came is a web page, not the file"
    marks = FIRST_BYTES.get(ext)
    if marks and not any(_has(head, at, mark) for at, mark in marks):
        return "bad", f"what came doesn't start as {ext.upper()} files do"
    return None


NO_ROOM = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}  # a full disk, for a download and a copy alike
NO_ROOM_WINDOWS = {39, 112}  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
NOT_A_PASSING_PROBLEM = NO_ROOM | {errno.ENAMETOOLONG}
NOT_A_PASSING_PROBLEM_WINDOWS = NO_ROOM_WINDOWS | {123, 206}  # and a name it won't take, a path too long
DISK_FULL = "The library's disk is full, so the run stopped"  # the start of a run's words for it (Downloader.blocked)


def no_room(e: OSError) -> bool:
    """A disk with no room left: the library's trouble, never one file's (copies.no_room follows what led to it too)."""
    return e.errno in NO_ROOM or getattr(e, "winerror", None) in NO_ROOM_WINDOWS


def no_use_retrying(e: OSError) -> bool:
    """A full disk or a name the system refuses: trying again, with a new request to the shop each time, changes nothing.
    Opening a file on Windows gives only a vague number for a refused name; asking about the file says which."""
    if e.errno in NOT_A_PASSING_PROBLEM or getattr(e, "winerror", None) in NOT_A_PASSING_PROBLEM_WINDOWS:
        return True
    if os.name == "nt" and e.filename and e.errno in (errno.EINVAL, errno.ENOENT):
        try:
            os.stat(e.filename)
        except OSError as why:
            return why.winerror in NOT_A_PASSING_PROBLEM_WINDOWS
    return False


def _format(job: Job) -> str:
    """A file's format in a run's words: the shop's label for it, or its extension."""
    return job.label or job.ext.upper()


class InUse(DownloadError):
    """The version in a file's place couldn't be moved into superseded\\: another program has it open (a viewer, the
    server drawing its pages: refused on Windows, "busy" from a share on macOS and Linux). That file's own trouble,
    which trying again at once won't change: no retries, and no other format in its place."""


def supersede(root: Path, rel: str) -> Path:
    """Move a file out of the way into superseded\\, never deleting it. A rename on the library's own disk, so that a
    refusal leaves nothing behind (InUse): a copy, as shutil.move falls back to, stayed there each time the file in
    use couldn't be deleted after it. Only where superseded\\ is on another disk (a link or a mount there), which no
    rename reaches, is the file copied and then removed, as shutil.move does; and if it can't be removed, its copy
    goes again, so that it is in one place only."""
    src = root / rel
    dest = root / "superseded" / rel
    if dest.exists():
        dest = dest.with_name(f"{dest.stem} {datetime.now():%Y-%m-%d_%H%M%S}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            os.replace(src, dest)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            _copy_aside(src, dest)
    except OSError as e:
        if not (isinstance(e, PermissionError) or e.errno == errno.EBUSY):
            raise
        why = ("it is in use: close it and run again" if os.name == "nt" or e.errno == errno.EBUSY else
               f"it can't be moved ({e.strerror or e}): see to the rights of its folder and run again")
        raise InUse(f"the version there couldn't be moved into superseded, as {why}") from e
    return dest


def _copy_aside(src: Path, dest: Path) -> None:
    """src moved to dest on another disk: copied, then removed. A copy cut short, or one whose original can't be
    removed (in use), goes again: the file stays where it was, and nothing of it is left in superseded\\. Its contents
    only, and its time, as a move keeps it: no copystat, whose chmod a disk that keeps modes as extended attributes
    (Linux's ntfs3) would store, and one that refuses chmod would fail."""
    try:
        shutil.copyfile(src, dest)
        with contextlib.suppress(OSError):
            st = os.stat(src)
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.unlink(src)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise


class Downloader:
    def __init__(
        self, root: Path, manifest: Manifest, *, refresh: Callable[[Job], str | None], workers: int = 4,
        transport: httpx.BaseTransport | None = None, log: Callable[[str], None] = print, report_every: float = 30.0,
        backoff: float = 2.0, link_refreshes: int = LINK_REFRESHES, gone: Callable[[Job], str] | None = None,
        name: Callable[[Job], Left | None] | None = None, refused: str = REFUSED,
    ):
        self.root = root
        self.link_refreshes = link_refreshes
        self.gone = gone  # told of a file the shop no longer has (HTTP 404 or 410, or no link): its words join the failure's
        # A job the shop names only in its link (its filename ""), named as its download starts (shopping.Naming): its
        # place settled and its link given to it (Job.url), which then fetches it, so that the link is asked for
        # once; or what it is left out as (a Left). It raises as a fresh link does (Refused, ShopError, AuthError), and
        # FileAtFault for no link. refused: the answers counted as turned away, in the words of a run they stop
        self.name, self.refused = name, refused
        self.manifest = manifest
        self.refresh = refresh
        self.workers = workers
        self.transport = transport
        self.log = log
        self.report_every = report_every
        self.backoff = backoff
        self.stop = threading.Event()
        self.auth_failed: str | None = None
        self.blocked: str | None = None  # why the run stopped: the shop turned file after file away, a full disk
        self.disk_full = False  # the run stopped as the library's disk is full (_disk_full)
        self._refused_in_a_row = 0
        self._unanswered_in_a_row = 0
        self._paused_until = 0.0  # a shop's Retry-After: no download starts a request before then
        self._bytes = 0
        self._bytes_lock = threading.Lock()
        self._local = threading.local()
        self._clients: list[httpx.Client] = []

    # ---- one file -------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(
                transport=self.transport, timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},  # exact bytes and lengths
                event_hooks=fetching.hooks(),  # https only, the first address and each redirect, and never one at home
            )
            self._local.client = client
            self._clients.append(client)
        return client

    def _count(self, n: int) -> None:
        with self._bytes_lock:
            self._bytes += n

    def _stream(self, job: Job, url: str, part: Path, ours: dict | None = None) -> dict:
        """Fetch into the part file, resuming it. Returns the MD5 of the whole part and what the
        server said: total length (None if unknown), ETag and Last-Modified.

        ours: what fetch knows of the part from its earlier tries in this run, kept up to date here. The MD5 of what it
        wrote, and the size that stands for (md5, at): a part of that size isn't read again from the disk (a share's,
        say) after every dropped connection, only one of another size (and none after the disk's own trouble: fetch).
        And the answer the part's bytes began with in this run (began): its ETag and total length, and its
        validator (its ETag, unless a weak one, else its Last-Modified), which a resumed request names (If-Range), so
        that a server whose file has changed since sends it whole instead of the rest; a part answered with another
        ETag or another total length all the same is thrown away and the file fetched again, whole. A whole new
        answer (200) becomes what the part began with only once the part is emptied for it: one cut off before any of
        it was written leaves the part, and what it is held to, as they were. So no two versions are spliced into one,
        where the server tells them apart."""
        ours = {} if ours is None else ours
        offset = part.stat().st_size if part.is_file() else 0
        if offset and ours.get("at") == offset:
            h = ours["md5"].copy()  # what this run wrote into it: already hashed
        else:
            h = md5_of(part) if offset else hashlib.md5()
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        if offset and ours.get("validator"):
            headers["If-Range"] = ours["validator"]
        with self._client().stream("GET", url, headers=headers) as r:
            info = {"etag": clean_etag(r.headers.get("etag")), "last_modified": r.headers.get("last-modified"), "total": None,
                    "resumed": bool(offset) and r.status_code != 200}
            total = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
            if r.status_code in (401, 403, 404, 410):
                raise LinkExpired(r.status_code)
            if r.status_code == 416 and offset:
                # Nothing left to send from this offset: the part is complete, or longer than the file.
                info["md5"] = h.hexdigest()
                if total:
                    info["total"] = int(total.group(1))
                elif PLAIN_MD5.match(info["etag"]) and info["md5"] == info["etag"]:
                    info["total"] = offset
                if info["total"] is None or offset > info["total"]:
                    part.unlink()  # can't tell which: start over
                    raise Retryable("the server refused to resume; starting over")
                self._same_file(ours, info, part)
                return info
            if r.status_code == 429 or r.status_code >= 500:
                raise Retryable(f"HTTP {r.status_code}", after=retry_after(r) or 0.0, status=r.status_code)
            if r.status_code == 200:
                mode, h = "wb", hashlib.md5()  # a fresh start, or the server ignored the range (or the file changed)
                length = r.headers.get("content-length")
                encoded = r.headers.get("content-encoding", "identity") != "identity"
                info["total"] = int(length) if length and length.isdigit() and not encoded else None
            elif r.status_code == 206 and offset:
                mode = "ab"
                info["total"] = int(total.group(1)) if total else None
                self._same_file(ours, info, part)
            else:
                raise DownloadError(f"unexpected HTTP {r.status_code}")
            etag = (r.headers.get("etag") or "").strip()  # this answer's version, which later tries hold the server to
            began = {"began": True, "etag": info["etag"], "total": info["total"],
                     "validator": etag if etag and not etag.startswith("W/") else
                     None if etag else r.headers.get("last-modified")}
            media = r.headers.get("content-type", "").partition(";")[0].strip().lower()
            info["page"] = media if media in WEB_PAGE_TYPES and job.ext.lower() not in WEB_PAGES else ""  # for check()
            if offset and info["page"]:  # the rest was asked for: a part from before isn't spoiled by a sign-in page
                raise NotTheFile(f"a web page ({media}) came instead of the rest of the file, perhaps a sign-in page: "
                                 "nothing of it was kept", written=False)
            chunks = r.iter_bytes(CHUNK)
            first = next(chunks, b"")
            if not first and mode == "wb" and job.md5 != EMPTY_MD5:  # nor by nothing at all: looked at before opening it
                raise NotTheFile(EMPTY, written=False)
            with part.open(mode) as fh:
                if mode == "wb":  # emptied: from here on the part holds this answer's bytes, and nothing before them
                    ours.clear()
                if "began" not in ours:  # the first answer in this run that the part's bytes are of
                    ours.update(began)
                ours["md5"], ours["at"] = h, offset if mode == "ab" else 0
                for chunk in itertools.chain((first,), chunks):
                    if self.stop.is_set():
                        raise Stopped()
                    fh.write(chunk)
                    h.update(chunk)
                    ours["at"] += len(chunk)  # as the hash goes: a write cut short leaves the part another size
                    self._count(len(chunk))
            info["md5"] = h.hexdigest()
            return info

    @staticmethod
    def _same_file(ours: dict, info: dict, part: Path) -> None:
        """A resumed answer against the one the part began with in this run (_stream's ours): another ETag, or another
        total length, is another file, which the part's start isn't of. The part goes; the file comes again, whole."""
        if not ours.get("began"):
            return
        etag, total = ours.get("etag"), ours.get("total")
        if (etag and info["etag"] and info["etag"] != etag) or (total and info["total"] and info["total"] != total):
            part.unlink()
            ours.clear()
            raise Retryable("the file changed at the server since its download began; starting over")

    @staticmethod
    def check(job: Job, part: Path, info: dict) -> tuple[str, str]:
        """('ok', source) | ('short', why) | ('bad', why) | ('page', why). Source: shop (the shop's listed MD5), cdn (the
        server's ETag) or transfer (nothing to compare with). The server's checksum and length vouch only for what it
        sent, so they count only for a file that starts as its kind does; one far smaller than the shop lists only when
        the server's checksum vouches for it and it is at least VOUCHED_LEAST. A
        web page (the server says so, or it starts as one) is no use going on with or fetching again."""
        size = part.stat().st_size
        expected = info.get("total") or (job.size if job.exact else 0)
        wrong = wrong_kind(job.ext, part)
        if info.get("page"):
            wrong = "page", f"a web page ({info['page']}) came instead of the file, perhaps a sign-in page"
        if expected and size < expected:
            return wrong if wrong and wrong[0] == "page" else ("short", f"{size} of {expected} bytes")
        if expected and size > expected:
            return "bad", f"{size} bytes, the server's file has {expected}"
        if job.md5 and info["md5"] == job.md5:
            return "ok", "shop"  # the very file the shop lists, whatever it looks like or is said to be
        if wrong:
            return wrong
        far = bool(job.size) and size * FAR_SMALLER < job.size
        if far and size < VOUCHED_LEAST:
            return "bad", f"{size} bytes, where the shop lists {job.size}: too small to be the file"
        etag = info.get("etag") or ""
        if PLAIN_MD5.match(etag):
            return ("ok", "cdn") if info["md5"] == etag else ("bad", "MD5 does not match the server's")
        if MULTIPART_MD5.match(etag) and multipart_etag_matches(part, etag):
            return "ok", "cdn"
        if far:  # only the server's checksum could vouch for a file this much smaller than its listing
            return "bad", f"{size} bytes, where the shop lists {job.size}, and the server gives no checksum for it: too small to be the file"
        if info.get("from_before"):  # two files spliced would look just like this
            return "bad", "a part from an earlier run, and no checksum vouches for the whole"
        if etag or not job.md5:
            return "ok", "transfer"  # nothing to compare with: HTTPS and the exact length vouch for it
        return "bad", "MD5 does not match the shop's and the server gives no checksum"

    def already_there(self, job: Job, dest: Path) -> tuple[bool, str | None, str]:
        """Whether the file in the job's place is the one wanted, to be recorded instead of fetched, its MD5, and what
        vouches for that (its record's md5_source). By the shop's listing, where it gives a size or an MD5; else by the
        place's latest record. So a file recorded before that no longer matches its record (cut short, say) is fetched
        again even when the shop lists neither, as DriveThruRPG and itch.io may, and its record isn't overruled by a
        poorer one. A file with no record, where the shop lists nothing to compare with, is taken as it is. Where
        neither the shop's MD5 nor its record's vouches for it, its MD5 is read now and recorded as found ("adopted",
        md5_ok false): nothing checked it, but hylly verify --hash can tell from then on whether it has changed."""
        if not dest.is_file() or job.replace:
            return False, None, ""
        size, record, known = dest.stat().st_size, self.manifest.get(job.path), {}

        def md5() -> str:
            if "md5" not in known:
                known["md5"] = md5_of(dest).hexdigest()
            return known["md5"]

        if ((job.size or job.md5 or record is None) and (not job.size or size == job.size)
                and (not job.md5 or md5() == job.md5)):
            return (True, job.md5, "shop") if job.md5 else (True, md5(), "adopted")
        if record is not None and size == record.get("size") and (not record.get("md5") or md5() == record["md5"]):
            if not record.get("md5"):
                return True, md5(), "adopted"
            return True, record["md5"], record.get("md5_source") or ("shop" if record.get("md5_ok") else "adopted")
        return False, None, ""

    def fetch(self, job: Job, once: bool = False) -> dict:
        """The job's file, checked, as its record (not yet added). once: one try, for a format better than a substitute
        that stands in (_one): one download at most, within the usual fresh links, and no retries. What comes wrong is
        thrown away, and a part cut short by network or share trouble stays: either way the next run tries again. A
        full disk stops the run (_disk_full), the part staying for the next run to go on from."""
        # The library gone stops the run, or what follows would fill the disk an unmounted one leaves behind; its share
        # gone for a moment is waited for, and a run stopped meanwhile is only stopped
        if paths.library_missing(self.root, self.stop.wait):
            if not self.stop.is_set():
                self.blocked = (f"The library at {self.root} isn't there any more, so the run stopped. What was "
                                "downloaded is kept.")
            self.stop.set()
            raise Stopped()
        dest = self.root / job.path
        part = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if no_room(e):
                self._disk_full()
                raise Stopped() from e
            raise
        there, digest, source = self.already_there(job, dest)
        if there:
            return self._entry(job, dest, digest, None, source=source, adopted=True)  # already there, just not recorded

        if self.stop.is_set():  # before asking the shop for anything: it may be why the run stopped
            raise Stopped()
        url = job.url or self.refresh(job) or ""  # StoryBundle links live 60 s: fetched right before use
        if not url:
            raise FileAtFault("no download link" + self._gone(job))
        refreshes, network_errors, bad = 0, 0, 0
        from_before = part.is_file()  # from an earlier run: the server's file may have changed since
        got_to = part.stat().st_size if from_before else 0
        ours: dict = {}  # what this run's tries know of the part, from one to the next (_stream)
        while True:
            self._wait_out_pause()
            if self.stop.is_set():
                raise Stopped()
            try:
                info = self._stream(job, url, part, ours)
                from_before = from_before and info["resumed"]
                verdict, detail = self.check(job, part, {**info, "from_before": from_before})
                if verdict == "ok":
                    size = part.stat().st_size
                    if job.size and size * FAR_SMALLER < job.size:
                        self.log(f"  {job.title}: kept the {mb(size)} file the server sends, which its checksum vouches for, "
                                 f"though the shop lists {mb(job.size)}: its listing is out of date")
                    return self._finish(job, part, dest, info, detail)
                if verdict == "page" and not from_before:  # this answer's: asking again brings the same page
                    raise NotTheFile(f"{detail}: nothing of it was kept")
                if verdict in ("bad", "page"):
                    part.unlink()
                    got_to = 0
                    if once:  # not fetched a second time in this run
                        raise DownloadError(detail)
                    if from_before:  # no fault of this file's: fetched whole, it gets its fair chance
                        from_before = False
                        continue
                    bad += 1
                    if bad > 1:
                        raise FileAtFault(f"{detail}, twice")
                    continue
                raise Retryable(f"file stays short ({detail})")  # cut short without an error: resume
            except NotTheFile as e:
                if e.written:  # nothing of it stays; a part from before it never reached stays for the next run
                    with contextlib.suppress(OSError):
                        part.unlink()
                raise
            except fetching.Refused as e:  # the shop's link: asking again brings the same
                raise DownloadError(f"its link went to {e}, so it wasn't fetched") from None
            except LinkExpired as e:
                if refreshes >= self.link_refreshes:
                    error = Refused if e.status in (401, 403) else FileAtFault  # else 404 or 410: the file is gone
                    gone = self._gone(job) if e.status in (404, 410) else ""
                    if PLACEHOLDER.fullmatch(job.filename):
                        gone = (f": the shop lists this file with a placeholder where its name should be ({job.filename}), "
                                "so its data for it is broken, which only the shop can put right")
                    raise error(f"link refused ({e}) even after fetching a fresh one{gone}")
                refreshes += 1
                self.stop.wait(self.backoff)  # not straight back: a shop that refuses shouldn't get a burst
                url = self.refresh(job) or ""
                if not url:
                    raise FileAtFault("the shop no longer lists this file" + self._gone(job))
            except (httpx.TransportError, Retryable, OSError) as e:  # OSError: also a network share dropping out
                if isinstance(e, OSError) and no_room(e):  # the library's trouble: every file after this one meets it
                    self._disk_full()
                    raise Stopped() from e
                if isinstance(e, OSError) and no_use_retrying(e):
                    raise DownloadError(str(e)) from e
                # The disk's own trouble (a share dropping out), not the connection's: the part is read again at
                # the next try, whatever its size, as what it holds may not be what was written. A dropped
                # connection keeps the MD5.
                if isinstance(e, OSError):
                    ours.pop("md5", None)
                    ours.pop("at", None)
                if once:  # no retries: what came of it stays in the part, for the next run's try
                    if getattr(e, "after", 0):
                        self._pause(e.after)  # the shop's wait is still every download's
                    # A plain failure, a 429 too: one answer is no shop turning file after file away (REFUSED_IN_A_ROW),
                    # so standing substitutes that each get one never stop the run.
                    raise DownloadError(str(e) or type(e).__name__) from e
                with contextlib.suppress(OSError):
                    if part.stat().st_size > got_to:  # it's getting there, however many connections that takes
                        got_to, network_errors = part.stat().st_size, 0
                network_errors += 1
                status = getattr(e, "status", 0)
                if network_errors > NETWORK_RETRIES:
                    raise (Refused if status == 429 else DownloadError)(f"gave up after {NETWORK_RETRIES} retries: {e}")
                if getattr(e, "after", 0):
                    self._pause(e.after)
                self.stop.wait(min(self.backoff * 2 ** (network_errors - 1), 60))

    def _gone(self, job: Job) -> str:
        """What the shop does about a file it no longer has (gone), as the failure's last words; '' for a shop without."""
        return f": {self.gone(job)}" if self.gone else ""

    def _disk_full(self) -> None:
        """A full disk is the library's trouble, never one file's: the run stops, as for a library gone away, rather
        than fail every file left at its first write, each after asking the shop for it (blocked; how much room the
        rest need is added at the run's end: run)."""
        with self._bytes_lock:
            self.disk_full = True
            self.blocked = self.blocked or DISK_FULL
        self.stop.set()

    def _room_words(self, stopped: list) -> str:
        """Why the run stopped, for a full disk: with the room the files it didn't finish or start need, less what their
        parts hold already, where their sizes are listed."""
        need = 0
        for job, _ in stopped:
            try:
                need += max(job.size - (self.root / (job.path + ".part")).stat().st_size, 0)
            except OSError:
                need += job.size
        known = all(job.size for job, _ in stopped)
        room = (f": the {many(len(stopped), 'file')} not downloaded yet need {'about' if known else 'at least'} "
                f"{size_words(need)} of room" if need else "")
        return (f"{DISK_FULL}{room}. Make room there and run again: what was downloaded is kept, and a file cut short "
                "goes on from where it stopped.")

    def _finish(self, job: Job, part: Path, dest: Path, info: dict, source: str) -> dict:
        """The new file is whole and checked: only now does it take the place of an old one, so that a refused link or
        a share dropping out never leaves a hole in the library. An old one that can't be moved aside (InUse) fails
        this file, and the new one stays in its part for the next run."""
        sync(part)  # on the disk before the record says it's safe
        if dest.is_file():
            if md5_of(dest).hexdigest() == info["md5"]:  # the same file (an unrecorded one, say): it stays where it is
                part.unlink()
                return self._entry(job, dest, info["md5"], info.get("last_modified"), source=source, adopted=True)
            supersede(self.root, job.path)
        os.replace(part, dest)
        return self._entry(job, dest, info["md5"], info.get("last_modified"), source=source)

    def _pause(self, seconds: float) -> None:
        """A shop's Retry-After: every download waits it out, not just this one."""
        seconds = min(seconds, LONGEST_PAUSE)
        with self._bytes_lock:
            until = time.monotonic() + seconds
            if until <= self._paused_until:
                return
            self._paused_until = until
        self.log(f"  The shop asks to wait {seconds:.0f} s; every download waits.")

    def _wait_out_pause(self) -> None:
        while not self.stop.is_set():
            left = self._paused_until - time.monotonic()
            if left <= 0:
                return
            self.stop.wait(min(left, 5.0))

    def _turned_away(self, refused: bool, no_answer: bool = False) -> None:
        """Counts files the shop turned away one after another, and those whose fresh link it didn't answer for at all,
        or only with its own trouble after every try (no_answer: an outage, not one file's trouble); a file that gets
        through resets both."""
        with self._bytes_lock:
            self._refused_in_a_row = self._refused_in_a_row + 1 if refused else 0
            self._unanswered_in_a_row = self._unanswered_in_a_row + 1 if no_answer else 0
            if self.blocked:
                return
            if self._refused_in_a_row >= REFUSED_IN_A_ROW:
                self.blocked = turned_away(self.refused)
            elif self._unanswered_in_a_row >= UNANSWERED_IN_A_ROW:
                self.blocked = unanswered()
            else:
                return
        self.stop.set()

    def _entry(self, job: Job, dest: Path, digest: str | None, last_modified: str | None, source: str = "shop", adopted: bool = False) -> dict:
        entry = {
            "path": job.path, "shop_id": job.shop_id, "title": job.title, "bundle": job.bundle, "kind": job.kind,
            "device": job.device, "role": job.role, "label": job.label, "ext": job.ext, "size": dest.stat().st_size,
            "md5": digest, "md5_ok": source in ("shop", "cdn"), "md5_source": source, "last_modified": last_modified,
            "downloaded_at": db.now(),
        }
        if job.exact and digest != job.md5:
            entry["listed_md5"] = job.md5  # the shop's listing was stale; kept to recognise it next time
        if job.exact and entry["size"] != job.size:
            entry["listed_size"] = job.size
        if adopted:
            entry["adopted"] = True  # found on the disk already: md5_source says by what ("adopted": nothing)
        entry.update(job.extra)
        return entry

    # ---- a whole run ----------------------------------------------------------------

    def _one(self, job: Job) -> tuple[str, Job, str | Left | None]:
        """('ok', the job or the fallback that worked, a note if a fallback), ('kept', job, why: it didn't come, and a
        substitute stands in), ('failed', job, why), ('stopped', job, None) or ('left', job, the Left: its link named a
        kind no download takes, for a job named as it starts, name). The formats are tried best first, the
        next only when the shop's own file is at fault (FileAtFault), as far as a substitute downloaded before
        (substitute): that one stands and isn't fetched again, the ones before it get one try each (fetch's once), and
        however they fail, it's no failure of the title's. A chosen file that is there, to be replaced (--update), has
        neither a substitute nor another format to fall back on."""
        errors: list[str] = []
        at_fault: list[Job] = []
        refused = no_answer = False
        stands = None if job.replace else substitute(job, self.manifest, self.root)
        for j in [job] if job.replace else [job, *(f for f in job.fallbacks if not never(kind_of(f.path)))]:
            if j is stands:
                break
            try:
                if self.name is not None and not j.filename:  # named by its first link, which then fetches it
                    if self.stop.is_set():  # before asking the shop for anything, as fetch is
                        raise Stopped()
                    left = self.name(j)
                    if left is not None:  # a kind no download takes: never fetched, and no failure
                        self._turned_away(False)
                        return "left", job, left
                entry = self.fetch(j, once=stands is not None)
                self.manifest.add(entry)
                j.size = j.size or entry["size"]  # DriveThruRPG lists no sizes: the log shows the real one
            except Stopped:
                return "stopped", job, None
            except AuthError as e:
                self.auth_failed = str(e)
                self.stop.set()
                return "stopped", job, None
            except (DownloadError, ShopError) as e:  # ShopError: the shop's answer when asked for a fresh link
                refused = refused or isinstance(e, Refused) or getattr(e, "status", 0) in (403, 429)
                # the shop's session, asked for a fresh link: no answer at all, or still its own trouble (5xx), after
                # every try; a 429 is a refusal (above)
                no_answer = no_answer or getattr(e, "no_answer", False) or getattr(e, "status", 0) >= 500
                errors.append(f"{_format(j)}: {e}" if job.fallbacks else str(e))
                if not isinstance(e, FileAtFault):  # an outage, a refusal, a full disk: another format fares no better
                    break
                at_fault.append(j)
                continue
            except Exception as e:  # noqa: BLE001 - one bad file must not end the run
                errors.append(f"{type(e).__name__}: {e}")
                if stands is None:
                    return "failed", job, "; ".join(errors)
                break
            self._turned_away(False)
            if j is job:
                return "ok", j, None if stands is None else (
                    f"the {_format(job)} came at last; the {_format(stands)} that came instead stays in the folder")
            self._drop_parts(at_fault)
            return "ok", j, f"{' and '.join(errors)}; got the {_format(j)} instead"
        self._turned_away(refused, no_answer)
        if stands is not None:
            self._drop_parts(at_fault)
            return "kept", job, f"{'; '.join(errors)}; the {_format(stands)} that came instead stays"
        return "failed", job, "; ".join(errors)

    def _drop_parts(self, jobs: list[Job]) -> None:
        """The part files of formats the shop's own file failed for, once another stands in: what came of a file that's
        gone or broken at the shop is no start for the one that may come back (a part from before needs a checksum for
        the whole anyway), and would only take room and show as a partial file for as long as the substitute stands."""
        for j in jobs:
            with contextlib.suppress(OSError):
                (self.root / (j.path + ".part")).unlink()

    def run(self, jobs: list[Job]) -> dict[str, list]:
        # kept: chosen files that didn't come while another format stands in for them (_one): neither done nor failed.
        # left: files of a kind no download takes (never), not asked for at all, and those a job's first link names so
        # (name: a StoryBundle book's name comes only with its link, and so its kind); the selection leaves out the
        # rest before (shopping.select). stopped: those a run stopped early didn't finish, or didn't start
        later = lambda j: self.name is not None and not j.filename  # noqa: E731 - its place is its folder until named
        left = [Left(j.title, j.filename, kind_of(j.path), (self.root / j.path).is_file(), j.path) for j in jobs
                if not later(j) and never(kind_of(j.path))]
        jobs = [j for j in jobs if later(j) or not never(kind_of(j.path))]
        results: dict[str, list] = {"ok": [], "failed": [], "stopped": [], "kept": [], "left": left}
        total = sum(j.size for j in jobs)
        done_bytes, started, last_report = 0, time.monotonic(), time.monotonic()
        ex = ThreadPoolExecutor(self.workers)
        from . import db

        of = {ex.submit(db.carried(self._one), j): j for j in jobs}  # each in this run's context, a dry run's too
        # got: each one's end as it was counted, in the order they ended: one finished is counted the moment it is
        # taken, so that a Ctrl-C while one is said loses none of the others that finished in the same wait
        futures, got, counted = set(of), {}, 0
        try:
            while futures:
                finished, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in finished:
                    got[f] = f.result()
                    status, job, error = got[f]
                    counted += status in ("ok", "failed", "kept")
                    n = counted
                    if status == "ok":
                        done_bytes += job.size
                        self.log(f"[{n}/{len(jobs)}] {mb(job.size):>11}  {job.title} ({job.ext.upper()}{', extra' if job.role == 'extra' else ''})"
                                 + (f": {error}" if error else ""))
                    elif status == "kept":  # nothing came: no size, and none left to come in this run
                        total -= job.size
                        self.log(f"[{n}/{len(jobs)}] {job.title}: {error}")
                    elif status == "failed":
                        self.log(f"FAILED {job.title} ({job.path}): {error}")
                if self.stop.is_set() and (self.auth_failed or self.blocked):
                    break
                if time.monotonic() - last_report >= self.report_every and futures:
                    last_report = time.monotonic()
                    self.log(self._progress(started, done_bytes, total))
        except KeyboardInterrupt:
            self.log("Stopping: finishing the current chunks. Partial files stay and resume next time.")
            self.stop.set()
        finally:
            try:
                stopping = self.stop.is_set()
                if stopping:
                    for f in of:
                        if f not in got:
                            f.cancel()
                ex.shutdown(wait=True, cancel_futures=stopping)
                for f, job in of.items():  # each once: one still running then finishes as it ends, one never started
                    if f not in got:      # is not finished either, nor one cut off by a Ctrl-C before counted
                        got[f] = f.result() if f.done() and not f.cancelled() else ("stopped", job, None)
                for status, job, error in got.values():
                    results[status].append(error if status == "left" else (job, error))
            finally:  # however the run ends, a job's own unforeseen error raised again here too
                for c in self._clients:
                    c.close()
        if self.disk_full and self.blocked == DISK_FULL:
            self.blocked = self._room_words(results["stopped"])
        return results

    def _progress(self, started: float, done_bytes: int, total: int) -> str:
        elapsed = max(time.monotonic() - started, 1e-6)
        with self._bytes_lock:
            moved = self._bytes
        speed = moved / elapsed
        left = max(total - done_bytes, 0)
        eta = time_words(left / speed) if speed > 0 else "?"
        if not total:  # sizes not listed in advance (DriveThruRPG)
            return f"  ... {size_words(moved)} fetched, {size_words(speed)}/s"
        return f"  ... {size_words(moved)} fetched, {size_words(speed)}/s, about {size_words(left)} and {eta} to go"


def verify_files(root: Path, rows: list[tuple[str, int | None, str | None]], check_hash: bool = False,
                 progress: Callable[[int, int], None] | None = None) -> list[tuple[str, str]]:
    """hylly verify's one check, of downloads and copies alike: each (path in the library, size, MD5) row's file there
    with that size, and (with check_hash, where an MD5 is recorded) that MD5. Returns [(path, problem)], each problem in
    the same words whatever the file is: missing, its size against the record's, MD5 mismatch."""
    problems = []
    for i, (rel, size, md5) in enumerate(rows, 1):
        p = root / rel
        if not p.is_file():
            problems.append((rel, "missing"))
        elif (actual := p.stat().st_size) != size:
            problems.append((rel, f"size {actual}, expected {size}"))
        elif check_hash and md5 and md5_of(p).hexdigest() != md5:
            problems.append((rel, "MD5 mismatch"))
        if progress:
            progress(i, len(rows))
    return problems


def verify(manifest: Manifest, root: Path, check_hash: bool = False, progress: Callable[[int, int], None] | None = None) -> list[tuple[str, str]]:
    """Problems with recorded files (verify_files). A record marked gone (a file the shop no longer has,
    itchio.Library.gone) isn't checked: its file won't come."""
    rows = [(e["path"], e.get("size"), e.get("md5")) for e in manifest.entries.values() if not e.get("gone")]
    return verify_files(root, rows, check_hash, progress)


def partial_files(root: Path, folder: str) -> list[str]:
    """The partial downloads (<file>.part) under a shop's folder, by their places in the library, forward slashes."""
    try:
        return sorted(p.relative_to(root).as_posix() for p in (root / folder).rglob("*.part") if p.is_file())
    except OSError:  # a share gone a moment: no count is worth failing a run for
        return []


SHOWN = 10  # the partial files no download goes on from that a run names, before "and N more"


def left_behind(shop_id: str, titles: list, manifest: Manifest, root: Path, *, answers: dict | None = None,
                yours: dict | None = None, sel=None) -> tuple[dict, list[str]]:
    """A shop's partial downloads (.part), counted, and those no download goes on from, named: what its run (a dry
    run's too) has for --json (partial_files, left_behind) and its words, the shop's folder walked once. A part is
    gone on from by its file's download while that file is to come (shopping.select's rows, as a run without options
    has them: pending, held for your answer or left out by you, as either may change; changed, for --update), or as a
    format behind such a file (File.fallbacks); by its folder, for a file the shop names only in its link. The rest
    stay for good, taking room: beside a file done, or of a format its title no longer takes. Only the parts in the
    folder of a title the shop lists now are judged, as another's file may still come. A shop's own file recorded under
    such a name (downloaded before paths.safe_filename gave those a _) is no part at all. Nothing is deleted: they're
    named, for you to delete. sel: that selection, where the caller has made it already (Humble's summary)."""
    from .shopping import select
    from .shops import SHOPS

    parts = [p for p in partial_files(root, SHOPS[shop_id].folder) if manifest.get(p) is None]
    if not parts:
        return {"partial_files": 0}, []
    if sel is None:
        sel = select(shop_id, titles, manifest, root, answers=answers, yours=yours)
    folders, going, wanted, behind = set(), set(), set(), []
    for _, f, place, verdict, *_ in sel.rows:
        folder = place if not f.name else place.rpartition("/")[0]  # named only in its link: its place is its folder
        folders.add(folder)
        if verdict in ("pending", "held", "changed"):
            going.add(place)
            wanted.add(folder)
        elif verdict == "behind":
            behind.append((folder, place))
    going |= {place for folder, place in behind if folder in wanted}  # what may come should the file before it fail
    left = [p for p in parts if (folder := p.rpartition("/")[0]) in folders and folder not in going
            and p.removesuffix(".part") not in going]
    if not left:
        return {"partial_files": len(parts)}, []
    size = 0
    for p in left:
        with contextlib.suppress(OSError):
            size += (root / p).stat().st_size
    n = len(left)
    them, they = ("it", "it") if n == 1 else ("them", "they")
    return {"partial_files": len(parts), "left_behind": left}, [
        f"Partial files no download goes on from: {n} ({size_words(size)}), beside a file done or of a format its "
        f"title no longer takes. Nothing deletes {them}: {they} can go."
    ] + [f"  {p!r}" for p in left[:SHOWN]] + ([f"  ... and {n - SHOWN} more"] if n > SHOWN else [])


def seed(
    sources: list[Path], jobs: list[Job], root: Path, manifest: Manifest, *, dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, list]:
    """Copy files from other folders that are planned files (same size and the shop's MD5: Humble's, for hylly humble
    seed) into their place in the library, each checked as hylly add checks its copies, and tried again while a share
    drops out (copies.copy_in). The next download run then adopts them instead of fetching. Sources are only read.
    As with hylly add, the files found are copied only if the library's disk has room for them all, and the run stops
    (Exit) when the disk fills up, stops taking copies or isn't there any more (copies.DiskWatch)."""
    from .copies import DiskWatch, copy_in, room_for  # copies takes md5_of from here: imported once both are loaded

    by_size: dict[int, list[Job]] = {}
    for j in jobs:
        if j.md5 and j.size:
            by_size.setdefault(j.size, []).append(j)
    names = {j.filename.lower(): j for j in jobs}
    report: dict[str, list] = {"copied": [], "already": [], "other_version": [], "unknown": [], "failed": []}
    todo: dict[str, tuple[Path, Job]] = {}  # by the file's place: one met twice is copied once
    for src in sources:
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            size = f.stat().st_size
            candidates = by_size.get(size, [])
            match = None
            if candidates:
                digest = md5_of(f).hexdigest()
                match = next((j for j in candidates if j.md5 == digest), None)
            if not match:
                same_name = names.get(f.name.lower())
                report["other_version" if same_name else "unknown"].append((f, same_name))
                continue
            dest = root / match.path
            if (match.path in todo or recorded_state(manifest.get(match.path), match, dest) == "done"
                    or (dest.is_file() and dest.stat().st_size == size)):
                report["already"].append((f, match))
                continue
            todo[match.path] = (f, match)
    if dry_run:
        report["copied"] += todo.values()
        return report
    watch = DiskWatch(root)
    if todo:
        watch.still_there(0, 0)  # before the room is asked for, which makes the library's folder if it isn't there
        room_for(root, (match.size for _, match in todo.values()))  # an older version in the way is kept: no room comes free
    for n, (f, match) in enumerate(todo.values(), 1):
        watch.still_there(len(report["copied"]), len(report["failed"]))  # unmounted on the way, the system disk would fill
        dest = root / match.path

        def older_aside() -> None:  # called within this turn of the loop, before match and dest change
            if dest.exists():  # an older version: old versions are kept, here as everywhere
                supersede(root, match.path)

        try:
            copy_in(f, dest, match.md5, match.path, before=older_aside)
        except (OSError, InUse) as e:  # InUse: its older version, open in another program, can't go aside
            report["failed"].append((f, str(e)))
            log(f"FAILED to seed {f}: {e}")
            watch.failed(e, len(report["copied"]), len(report["failed"]), len(todo) - n)
            continue
        watch.fine()
        report["copied"].append((f, match))
        log(f"seeded {mb(match.size):>11}  {match.title} <- {f}")
    return report


_awake: subprocess.Popen | None = None  # what keeps the computer awake, where that takes a program of its own


def _without_asking() -> list[str]:
    """--no-ask-password, where this computer's systemd-inhibit knows it: over SSH a newer one asks for a password on
    the terminal to hold off sleep, in the middle of the run's own lines, and only someone at the screen may allow it."""
    try:
        said = subprocess.run(["systemd-inhibit", "--help"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return ["--no-ask-password"] if "--no-ask-password" in said else []


def keep_awake(on: bool, platform: str = sys.platform) -> None:
    """No sleeping while a run is active. The programs that see to it on macOS and Linux end with this process, should
    it end first. Without them, as on a server that never sleeps, nothing is lost."""
    global _awake
    if platform == "win32":
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))
    elif on and _awake is None:
        me = str(os.getpid())
        tries = ([["caffeinate", "-i", "-w", me]] if platform == "darwin" else
                 # over SSH or from a service only idling may be held off: sleep takes someone at the screen to allow it
                 [["systemd-inhibit", f"--what={what}", "--who=Hylly", "--why=A long job is running", *asking, "tail",
                   f"--pid={me}", "-f", "/dev/null"] for asking in [_without_asking()] for what in ("sleep:idle", "idle")])
        for tool in tries:
            try:
                _awake = subprocess.Popen(tool, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                _awake = None
                continue
            time.sleep(0.3)
            if _awake.poll() is None:
                break
            _awake = None
    elif not on and _awake is not None:
        _awake.terminate()
        _awake = None


def write_failures(path: Path, results: dict[str, list], manifest: Manifest | None = None) -> int:
    """Keep earlier failures that are still failing, drop the ones that now worked (in this run,
    or recorded in the manifest by an interrupted run), add new ones. A spreadsheet opens the file: a cell that would
    start a formula (a title is anyone's to name) is written with a ' before it, and read back without."""
    from .shoplist import no_formula, unformula

    rows: dict[str, list] = {}
    if path.is_file():
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    cells = [unformula(row[c]) for c in ("path", "title", "bundle", "error")]
                    rows[cells[0]] = [*cells, row["when"]]
        except (KeyError, csv.Error, ValueError):
            rows = {}  # a file cut short: this run's failures start it anew
    for job, _ in [*results["ok"], *results.get("kept", ())]:  # kept: another format stands in, which is no failure
        rows.pop(job.path, None)
        rows.pop(job.folder, None)  # a StoryBundle book that got no link is listed by its folder, its file unknown
        rows.pop(job.extra.get("instead_of", ""), None)
    if manifest is not None:
        covered = {e.get("instead_of") for e in manifest.entries.values()}  # a fallback was downloaded instead
        covered |= {p.rpartition("/")[0] for p in manifest.entries}  # a file in the folder of a book listed by it
        for p in list(rows):
            if p in manifest.entries or p in covered:
                rows.pop(p)
    now = datetime.now().isoformat(timespec="seconds")  # the local time alone, not db.now's, as a spreadsheet reads it
    for job, error in results["failed"]:
        rows[job.path] = [job.path, job.title, job.bundle, error or "", now]
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(["path", "title", "bundle", "error", "when"])
    writer.writerows([*(no_formula(str(c or "")) for c in row[:4]), row[4]] for row in rows.values())
    atomic_write(path, text.getvalue(), encoding="utf-8-sig")
    return len(rows)
