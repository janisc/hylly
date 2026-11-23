"""HyllyCopy (hylly copy): a copy you can leave running, of terabytes or of tens of thousands of small files.

It doesn't need to be fast. It has to finish in the end, and to be something you can count on.

- The source is only read. Nothing is deleted, moved or overwritten anywhere, and the target is never "mirrored": a file
  only the target has stays, and a different file with the same name stays too, and is reported.
- A file is written in .hyllycopy/parts on the target, and gets its name only when complete and checked, so a stop costs
  at most the time spent on one file. The next run compares what is there with the source, and goes on from it.
- Every copy is read back from the disk, past the system's cache as far as the system allows, and compared with a
  checksum made while the source was read. Lists the source already has (.sfv, .md5, .sha1, .sha256) are compared too.
- Disks and shares go away and come back, on either side. A short drop is waited out, with longer and longer pauses. A
  long one puts the job on hold with a note, and it goes on by itself when the disk answers: an unmounted disk doesn't
  count as an answer, so nothing lands on the disk it leaves behind. A read that hangs is noticed all the same. A target
  copied to before whose mark is gone or another, or a source that isn't the one copied from before, is refused, as its
  disk may not be mounted; so is, on Linux, a target at or below a place /etc/fstab mounts a disk at, while none is.
- One file that can't be read or written is noted, and the rest goes on. Files in a row failing although the disk
  answers mean trouble with the disk: then it holds, with a note, trying one file every half hour rather than wearing the
  disk out, and it's back at full speed as soon as one goes through.
- "Done" comes only after the source is listed again at the end and every file on it checks out. A file added meanwhile
  gets another round, one changing while it's copied is copied again, and one changed after its copy was made is listed,
  as nothing is written over. A listing gone short isn't believed: a file that dropped out of it is asked about by itself,
  and a source that has lost most of its files isn't done.
- At the end, .hyllycopy holds a report and a manifest that any sha256sum can check: hylly copy check TARGET reads every
  copy again, any time later.

Each file costs a few trips over the network and a flush to the disk, which is what makes it safe; the rest is kept small
for the sake of tens of thousands of small ones: the record is written in batches, and a folder is listed instead of each
file being asked about. A record lost with a stop costs only reading those files again, as a file that is there but not
in the record is compared with the source, and never overwritten.

Why not rsync or robocopy: docs/architecture.md.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import hashlib
import itertools
import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
import unicodedata
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, runs
from .exits import LOCAL, NOT_SET_UP, USAGE, Exit
from .storage import atomic_write, is_link_stat, read_json, read_records, share_is_gone, shown  # shown: a name fit for a terminal or a note
from .words import many, size_words, time_words

FOLDER = ".hyllycopy"     # on the target: what this copy knows, travelling with the disk
CHUNK = 4 << 20           # a multiple of any disk's sector, as reading past the cache on Windows needs
ON_DISK_EVERY = 128 << 20  # bytes between flushes: a slow share's backlog stays short, and a drop costs at most this much
WAITS = (5, 15, 30, 60, 120, 300, 600)  # seconds between tries while a disk is away; the last one repeats
STRIKES = 5               # errors in a row on one file while its disk answers, before they are the file's problem
IN_A_ROW = 5              # files in a row failing that way, before they are the disk's
COOL_DOWN = 1800          # seconds between files while they do
GOES_AT_ONE_SPOT = 8      # goes at one place in a file, drops of the disk included, before it's that file's problem
TRIES = 3                 # goes at one file in a run
ROUNDS = 3                # passes over the source, each ending with a new listing
SHRUNK_FROM = 4           # files a source had, at least, for losing most of them to say more than a few removed
TELL_AFTER = 15 * 60      # seconds of no progress before a note says it's on hold
TELL_AGAIN = 24 * 3600    # and again, while it lasts
TELL_STARTED = 120        # seconds in, when the speed says something: a copy shorter than this sends no notes
TICK = 15.0               # how often the watchdog looks
LINE_EVERY = 60           # seconds between lines saying how far it has come
RECORD_FILES, RECORD_SECONDS, RECORD_BYTES = 500, 30, 1 << 30  # the record is written after this much, whichever comes first
FAR_MORE = 1 << 30        # bytes a run may write beyond all copied to its target before, without asking first: 1.1 GB
RESEND_FOR = 3600         # at the end, how long a phone note that couldn't go is tried again: the way to the internet may be down too
MAX_LIST = 64 << 20       # a list of checksums bigger than this isn't read
SAMPLE = 32 << 20         # bytes read from the source for an estimate, in a plan
NOTE_WAIT = 60            # seconds a note may take: this computer's notification and then the phone's, each with a limit of its own
SYNC_WAIT = 120           # seconds the end waits for the system's own flush, which a stuck disk elsewhere can hold up
NOT_THERE_YET = ("/mnt", "/media", "/run/media", "/Volumes")  # where an unmounted disk leaves an empty folder
ON_LINUX = sys.platform.startswith("linux")  # where /etc/fstab names the places disks are mounted at (unmounted_place)
FSTAB = "/etc/fstab"  # only read
MOUNTED_NOW = "/proc/self/mountinfo"  # the kernel's own list of what is mounted where, bind mounts too

CLUTTER_FILES = {".ds_store", ".localized", ".apdisk", "thumbs.db", "ehthumbs.db", "desktop.ini"}
CLUTTER_FOLDERS = paths.CLUTTER_FOLDERS  # the systems' own folders: in paths.py, as the catalog passes them by too
NOT_ON_WINDOWS = set('<>:"\\|?*') | {chr(c) for c in range(32)}
LIST_KINDS = {".sfv": "crc32", ".md5": "md5", ".md5sum": "md5", ".sha1": "sha1", ".sha256": "sha256", ".sha256sum": "sha256"}
LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64}

GONE_ERRNOS = {errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.EEXIST, errno.ENAMETOOLONG, errno.EFBIG, errno.ELOOP, errno.EILSEQ}
GONE_WINERRORS = {2, 3, 80, 123, 161, 183, 206, 223, 267}
FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
FULL_WINERRORS = {39, 112}
HELD_WINERRORS = {32, 33}  # in use by another program for a moment, as an antivirus scan is


class FileProblem(Exception):
    """One file can't be copied, and the others go on. again: another try may help. disk: not the file's own trouble, it seems.
    gone: it isn't on the source any more, which is no failure of the copy."""

    def __init__(self, why: str, again: bool = True, disk: bool = False, gone: bool = False) -> None:
        super().__init__(why)
        self.why, self.again, self.disk, self.gone = why, again, disk, gone


class _Different(FileProblem):
    """Another file has its name on the target."""


class _Trouble(Exception):
    def __init__(self, side: Side, error: OSError) -> None:
        super().__init__(str(error))
        self.side, self.error = side, error


def long_path(path) -> str:
    """An absolute path, on Windows in the form that isn't stopped by its 260 letters."""
    full = os.path.abspath(str(path))
    if os.name != "nt" or full.startswith("\\\\?\\"):
        return full
    return "\\\\?\\" + ("UNC" + full[1:] if full.startswith("\\\\") else full)


def _why(e: BaseException | None) -> str:
    if isinstance(e, OSError) and e.strerror:
        return shown(e.strerror)
    return shown(type(e).__name__) if e else "no answer"


def _is_full(e: OSError) -> bool:
    return e.errno in FULL_ERRNOS or getattr(e, "winerror", None) in FULL_WINERRORS


def _is_its_own(e: OSError) -> bool:
    """Trouble that a disk answering says something about this one file: gone, too long, too big, in the way."""
    if share_is_gone(e):
        return False
    return (isinstance(e, (FileNotFoundError, NotADirectoryError, IsADirectoryError, FileExistsError))
            or e.errno in GONE_ERRNOS or getattr(e, "winerror", None) in GONE_WINERRORS)


def _not_there(e: OSError) -> bool:
    """Whether a disk says nothing is at a path, as a look takes it: not a share gone, which Windows calls "not found"
    too (share_is_gone)."""
    return isinstance(e, (FileNotFoundError, NotADirectoryError)) and not share_is_gone(e)


_TROUBLE = object()  # _keep_trying's missing when nothing there is trouble like any other


def lost_most(before: int, now: int) -> bool:
    """Whether a source has lost most of its files: SHRUNK_FROM or more before, under half of them now. For HyllyCopy
    and the backup alike, as more like a disk away, or another one there, than files removed on purpose."""
    return before >= SHRUNK_FROM and now < before / 2


def _keep_to_disk(fh) -> None:
    """What was written, on the disk. macOS's plain fsync leaves it in the drive's own cache, and Windows' says "bad file"
    whatever went wrong, a full disk too. A disk that can't do this is no reason to fail, as the copy is read back anyway."""
    if os.name == "nt":
        import ctypes
        import msvcrt

        if not _kernel32().FlushFileBuffers(msvcrt.get_osfhandle(fh.fileno())):
            error = ctypes.get_last_error()
            if error not in (1, 50, 87):  # ways of saying this disk can't
                raise ctypes.WinError(error)
        return
    if sys.platform == "darwin":
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.fcntl(fh.fileno(), getattr(fcntl, "F_FULLFSYNC", 51))
            return
    try:
        os.fsync(fh.fileno())
    except OSError as e:
        if e.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL):
            raise


def _write_all(fh, data) -> None:
    view = memoryview(data)
    while view:
        wrote = fh.write(view)
        if not wrote:  # some disks' way of saying no, and it would go round for ever
            raise OSError(errno.EIO, "The disk took nothing of what was written")
        view = view[wrote:]


def _write_here(path: str, data: str) -> None:
    """A whole file swapped in, in a folder that has to be there. atomic_write would make the folder, and the place an
    unmounted disk leaves must not get one."""
    raw = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb", buffering=0) as fh:
            _write_all(fh, raw)
            _keep_to_disk(fh)
        try:
            os.replace(tmp, path)
        except PermissionError:  # Windows, with it open somewhere: a viewer, or another computer on the share
            with open(path, "r+b", buffering=0) as fh:
                fh.truncate(0)
                _write_all(fh, raw)
                _keep_to_disk(fh)
            os.unlink(tmp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _make_below(root: str, folder: str, mode: int = 0o777) -> None:
    """A folder and those on the way to it, below a root that has to be there: makedirs would make that one too, on the
    place an unmounted disk leaves. mode: theirs as they're made (your umask may take more away), never changed after."""
    path = root.rstrip(os.sep)
    for part in folder.split("/"):
        path += os.sep + part
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, mode)
    if not os.path.isdir(path):
        raise NotADirectoryError(errno.ENOTDIR, "A file has this folder's name")


@functools.cache
def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    k.VirtualAlloc.restype = wintypes.LPVOID
    k.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    return k


def _windows_chunks(path: str, at: int) -> Iterator[bytes]:
    """A file read with Windows' cache bypassed, which wants whole sectors into page-aligned memory."""
    import ctypes
    from ctypes import wintypes

    k = _kernel32()
    generic_read, share_all, open_existing, no_buffering, sequential = 0x80000000, 3, 3, 0x20000000, 0x08000000
    handle = k.CreateFileW(path, generic_read, share_all, None, open_existing, no_buffering | sequential, None)
    if handle in (wintypes.HANDLE(-1).value, None):
        raise ctypes.WinError(ctypes.get_last_error())
    memory, as_usual = k.VirtualAlloc(None, CHUNK, 0x3000, 0x04), False
    try:
        if at and not k.SetFilePointerEx(handle, at, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        size = ctypes.c_longlong()
        if not k.GetFileSizeEx(handle, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        got = wintypes.DWORD()
        while True:
            if not k.ReadFile(handle, memory, CHUNK, ctypes.byref(got), None):
                error = ctypes.get_last_error()
                if error != 87:
                    raise ctypes.WinError(error)
                as_usual = True  # a disk with other ideas about sectors
                break
            if not got.value:
                return
            yield ctypes.string_at(memory, got.value)
            at += got.value
            if got.value < CHUNK:
                if at >= size.value:
                    return
                if at % 4096:  # a server giving less than it was asked for, before the end, and asking from there isn't on a whole sector
                    as_usual = True
                    break
    finally:
        k.CloseHandle(handle)
        k.VirtualFree(memory, 0, 0x8000)
    if as_usual:
        yield from _usual_chunks(path, at)


def _usual_chunks(path: str, at: int) -> Iterator[bytes]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with contextlib.suppress(OSError):
            if sys.platform == "darwin":
                import fcntl

                fcntl.fcntl(fd, getattr(fcntl, "F_NOCACHE", 48), 1)
            elif hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # what was just flushed isn't kept
        if at:
            os.lseek(fd, at, os.SEEK_SET)
        while chunk := os.read(fd, CHUNK):
            yield chunk
    finally:
        os.close(fd)


def past_the_cache(path: str, at: int = 0) -> Iterator[bytes]:
    """A file's contents from byte at, read from the disk rather than from what the system remembers of it, as far as
    the system allows. A share's own computer may still answer from its memory: hylly copy check, later, is for that."""
    if os.name == "nt" and not at % 4096:
        return _windows_chunks(path, at)
    return _usual_chunks(path, at)


def _no_overwrite_name(part: str, target: str) -> None:
    """The name, unless something has it by now. Windows' rename refuses that by itself; elsewhere rename would replace
    it, so a link is made instead where the disk can. Where it can't, this looks first: only another writer's file arriving
    in that very moment could be replaced, and the target is held by this run."""
    if os.name == "nt":
        os.rename(part, target)
        return
    try:
        os.link(part, target)
    except FileExistsError:
        raise
    except OSError:
        try:
            os.lstat(target)
        except FileNotFoundError:
            os.rename(part, target)
        else:
            raise FileExistsError(errno.EEXIST, "already there") from None
    else:
        os.unlink(part)


TOO_LONG = "its path is too long for this share, or not allowed"


def long_for_a_share(path: str, windows: bool = os.name == "nt") -> bool:
    """A path on a Windows share so long that a refusal to name a file there may be for its length: some shares say
    "access denied" past some 510 letters, which is also what a real lack of rights says. So it's said as either
    (TOO_LONG), and not tried again, as neither goes away by waiting."""
    return windows and path.startswith("\\\\?\\UNC") and len(path) > 500


def name_problem(rel: str, windows: bool) -> str | None:
    """Why a disk can't hold this name, if it can't: reported, never renamed behind your back."""
    for part in rel.split("/"):
        long = len(part.encode("utf-16-le", "surrogatepass")) // 2 if os.name == "nt" else len(os.fsencode(part))
        if long > 255:
            return "its name is too long for a disk"
        if windows:
            bad = NOT_ON_WINDOWS.intersection(part)
            if bad:
                return f"its name has a letter this disk can't hold ({shown(sorted(bad)[0])!r})"
            if part != part.rstrip(". "):
                return "its name ends in a dot or a space, which this disk can't hold"
            if paths.windows_cant_hold(part):  # CON, con.txt, COM0.epub: one of its devices
                return "its name is one that Windows keeps for itself"
    return None


def _manifest_line(rel: str, sha: str) -> bytes:
    """A line as sha256sum writes it: a name with a backslash or a line break starts the line with a backslash, as is its way."""
    try:
        name = rel.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        name = rel.encode("utf-8", "surrogatepass")
    if any(c in name for c in (b"\\", b"\n", b"\r")):
        name = name.replace(b"\\", b"\\\\").replace(b"\n", b"\\n").replace(b"\r", b"\\r")
        return b"\\" + sha.encode() + b"  " + name + b"\n"
    return sha.encode() + b"  " + name + b"\n"


def read_list(text: str, kind: str) -> Iterator[tuple[str, str]]:
    """(name, checksum) from a list of checksums of this kind; lines that aren't that are passed over."""
    for line in text.splitlines():
        line = line.strip("\ufeff \t")
        if not line or line.startswith((";", "#")):
            continue
        if kind == "crc32":
            m = re.fullmatch(r"(.+?)\s+([0-9a-fA-F]{8})", line)
            if m:
                yield m[1], m[2].lower()
            continue
        m = re.fullmatch(rf"([0-9a-fA-F]{{{LENGTHS[kind]}}}) [ *]?(.+)", line)
        if m:
            yield m[2], m[1].lower()
            continue
        m = re.fullmatch(rf"{kind}\s*\((.+)\)\s*=\s*([0-9a-fA-F]{{{LENGTHS[kind]}}})", line, re.I)
        if m:
            yield m[1], m[2].lower()


class _Digest:
    """The SHA-256 of what was read, and the kind a list on the source gives, if that's another."""

    def __init__(self, other: str | None = None) -> None:
        self.sha, self.other_kind = hashlib.sha256(), other
        self.other = hashlib.new(other, usedforsecurity=False) if other in ("md5", "sha1") else None
        self.crc = 0

    def update(self, data: bytes) -> None:
        self.sha.update(data)
        if self.other is not None:
            self.other.update(data)
        elif self.other_kind == "crc32":
            self.crc = zlib.crc32(data, self.crc)

    def copy(self) -> _Digest:
        twin = _Digest.__new__(_Digest)
        twin.sha, twin.other_kind, twin.crc = self.sha.copy(), self.other_kind, self.crc
        twin.other = self.other.copy() if self.other is not None else None
        return twin

    def hex(self) -> str:
        return self.sha.hexdigest()

    def of_other(self) -> str:
        if self.other_kind == "crc32":
            return f"{self.crc:08x}"
        return self.other.hexdigest() if self.other is not None else self.sha.hexdigest()


def _same_folder(side: Side, numbers, before, names: list[str]) -> bool:
    """Whether a side's folder is the one it was: by its numbers (device, file number) now and before, or, as a disk
    mounted again may get new ones, by most of the names seen at its top before being there now. With no names to go
    by, nothing says it isn't. For a side that answers (Side.answers) and a source copied from before (_another_source)
    alike."""
    if numbers and before and list(numbers) == list(before):
        return True
    return not names or 2 * sum(os.path.lexists(side.path(name)) for name in names) > len(names)


class Side:
    """One end of a copy, and how to tell whether it is there: not just a folder of that name, as an unmounted disk leaves
    one. The target has a file of its own to show for it. The source is the same folder by its numbers, or, as a disk
    mounted again may get new ones, by most of the names seen at its top."""

    def __init__(self, given, word: str) -> None:
        self.given, self.word, self.root = str(given), word, long_path(given)
        self.mark: tuple[str, str] | None = None
        self.seen: list[str] = []
        self.numbers: tuple[int, int] | None = None

    def path(self, rel: str = "") -> str:
        return self.root.rstrip(os.sep) + os.sep + rel.replace("/", os.sep) if rel else self.root

    def answers(self) -> bool:
        try:
            st = os.stat(self.root)
            if self.mark:
                return _read_mark(self.path(self.mark[0])) == self.mark[1]
            if not self.numbers and os.name == "nt":  # no numbers to go by, and no unmounted disk's folder to mistake it for
                return True
            return _same_folder(self, (st.st_dev, st.st_ino), self.numbers, self.seen)
        except (OSError, ValueError):
            return False

    def call(self, fn: Callable, *args, **kw):
        """A thing done on this disk: an OSError from it says which disk it was."""
        try:
            return fn(*args, **kw)
        except OSError as e:
            raise _Trouble(self, e) from e


class Notes:
    """Notes through HyllyBell. One that couldn't reach the phone goes again later, oldest first: the way to the internet
    may be down too, whatever kept the disk away."""

    def __init__(self) -> None:
        self.waiting: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
        self._sending = threading.Lock()  # one sender of the waiting at a time (again), apart from _lock: a new note waits for no send

    def tell(self, title: str, text: str, public: str = "") -> None:
        from . import bell

        if not bell.job_note(title, text, public or text, wait=NOTE_WAIT):  # long enough to know: going again would send it twice
            with self._lock:
                self.waiting.append((title, text, public or text))

    def again(self) -> None:
        """The waiting notes, oldest first, until one still can't go. One sender at a time: the watchdog's tick, a moment
        after its job has ended, and the end's send_the_rest would otherwise both send the first, and one take away the
        next, never sent, or none at all (IndexError). The second waits its turn, then sends what is left."""
        from . import bell

        with self._sending:
            while self.waiting:
                title, text, public = self.waiting[0]
                if not bell.phone_again(title, text, public):
                    return
                with self._lock:
                    self.waiting.pop(0)


class _Pace:
    """How long what's left of a copy takes, as it goes. A file costs time of its own (its name on both sides, its entry,
    its check) and so does each of its bytes, so the rest is counted by both: files_left x a file's cost + bytes_left x a
    byte's. Both are learned from what has passed, a look at a time (least squares over them, the latest counting most):
    thousands of small files teach the one, a big file the other. Going by the bytes or the files alone, whichever said
    longer, gave hundreds of hours while a copy that began with thousands of covers had yet to reach its first big file.
    Until a cost has been seen it's a usual disk's (FILE_COST, BYTE_COST), a guess as heavy as PRIOR_FILES files and
    PRIOR_BYTES bytes; a look in which nothing moved, as on hold, says nothing."""

    FILE_COST, BYTE_COST = 0.02, 1 / 50e6  # seconds: a file's, and a byte's, as at 50 MB a second
    PRIOR_FILES, PRIOR_BYTES = 200, 500e6
    KEPT = 0.995  # how much each look leaves of what was learned before: half of it in some 35 minutes of looks

    def __init__(self) -> None:
        n, b = self.PRIOR_FILES, self.PRIOR_BYTES
        self.ff, self.bb, self.fb = float(n * n), b * b, 0.0
        self.ft, self.bt = n * n * self.FILE_COST, b * b * self.BYTE_COST
        self.last: tuple[float, int, int] | None = None

    def seen(self, now: float, files: int, size: int, began: float) -> None:
        last, self.last = self.last or (began, 0, 0), (now, files, size)
        took, more_files, more_bytes = now - last[0], files - last[1], size - last[2]
        if took <= 0 or (more_files <= 0 and more_bytes <= 0):
            return
        for name in ("ff", "bb", "fb", "ft", "bt"):
            setattr(self, name, getattr(self, name) * self.KEPT)
        self.ff += more_files * more_files
        self.bb += more_bytes * more_bytes
        self.fb += more_files * more_bytes
        self.ft += more_files * took
        self.bt += more_bytes * took

    def costs(self) -> tuple[float, float]:
        """Seconds a file and seconds a byte cost. Neither can be less than nothing: if one comes out so, the other is all."""
        det = self.ff * self.bb - self.fb * self.fb
        if det <= 1e-9 * self.ff * self.bb:
            return 0.0, self.bt / self.bb
        a, b = (self.ft * self.bb - self.bt * self.fb) / det, (self.bt * self.ff - self.ft * self.fb) / det
        if a < 0:
            return 0.0, self.bt / self.bb
        if b < 0:
            return self.ft / self.ff, 0.0
        return a, b

    def left(self, files: int, size: int) -> float:
        a, b = self.costs()
        return a * max(files, 0) + b * max(size, 0)


class _Job:
    """What copying and checking have in common: the waiting, and the watchdog with its notes."""

    NAME = "HyllyCopy"
    STOP = "hylly jobs stop copy"  # what ends one that has hung

    def __init__(self, say: Callable = print, sleep: Callable = time.sleep, now: Callable = time.monotonic, every: float = 0) -> None:
        self._print, self.sleep, self.now, self.every = say, sleep, now, every
        # its timed lines, how far it has come and what holds it, are progress: on standard error, unless told where (say)
        self._progress = say if say is not print else functools.partial(print, file=sys.stderr)
        self.notes = Notes()
        self.strikes, self.why_held, self.told_at, self.full_said = 0, "", None, False
        self.began = self.last_move = self.last_line = self.last_note = self.last_again = now()
        self.started_told = False
        self.moved_bytes = 0
        self._ended = threading.Event()

    def out(self, line: str = "") -> None:
        """A line for whoever is watching. A closed pipe, or a log on a share that went away, is no reason to stop a copy."""
        with contextlib.suppress(OSError, ValueError):
            self._print(line, flush=True)

    def say(self, line: str) -> None:
        with contextlib.suppress(OSError, ValueError):  # as out: a closed pipe is no reason to stop a copy
            self._progress(time.strftime("%H:%M:%S ") + line, flush=True)

    # ---- waiting ----
    def _touch(self) -> None:
        self.last_move = self.now()

    def _moved_on(self) -> None:
        """Real progress, not bytes read over again: what was holding it is over."""
        self._touch()
        self.strikes, self.why_held, self.full_said = 0, "", False

    @staticmethod
    def _another_go(e: OSError, at: int, last: tuple[int, int]) -> tuple[int, int]:
        """Counts the goes at one place in a file. A disk going away each time it's read there would otherwise go on for ever,
        each drop looking like a new start. A full disk has no such limit: it waits for room."""
        spot, goes = last
        if _is_full(e):
            return last
        goes = goes + 1 if at <= spot else 1
        if goes >= GOES_AT_ONE_SPOT:
            raise FileProblem(f"it keeps failing at the same place in the file, with the disk going away each time: {_why(e)}", disk=True)
        return max(at, spot), goes

    def _wait_out(self, t: _Trouble) -> None:
        """After something failed on a disk: back from here means try it again. FileProblem means it's this file's."""
        side, e = t.side, t.error
        if _is_full(e):
            self._hold_for_room(side, e)
        elif not side.answers():
            self._hold(side, e)
        elif not share_is_gone(e) and _is_its_own(e):
            raise FileProblem(_why(e), again=isinstance(e, FileNotFoundError), gone=isinstance(e, FileNotFoundError) and side.word == "the source")
        else:
            self._strike(e)

    def _strike(self, e: OSError) -> None:
        self.strikes += 1
        if isinstance(e, PermissionError) and getattr(e, "winerror", None) not in HELD_WINERRORS:
            if os.name != "nt" or self.strikes >= 3:  # Windows says the same of a file an antivirus scan holds for a moment
                raise FileProblem(_why(e))
            self.sleep(2 if self.strikes == 1 else 5)
            return
        if self.strikes >= STRIKES:
            raise FileProblem(f"it keeps failing, although the disk answers: {_why(e)}", disk=not isinstance(e, PermissionError))
        self.sleep(WAITS[self.strikes - 1])

    def _hold(self, side: Side, e: OSError | None) -> None:
        began = self.now()
        self.why_held = f"{side.word} isn't answering"
        self.say(f"{side.word.capitalize()} isn't answering ({_why(e)}): waiting for it.")
        for pause in itertools.chain(WAITS, itertools.repeat(WAITS[-1])):
            self.sleep(pause)
            if side.answers():
                break
        self.say(f"{side.word.capitalize()} is back after {time_words(max(self.now() - began, 60))}: going on.")
        self.why_held, self.strikes = "", 0  # the watchdog goes by movement: a disk that goes away at every try hasn't any

    def wait_for(self, side: Side) -> None:
        """After a run, before more is written on that disk (a backup's zip and its note, a restore's unpacking): held while
        it doesn't answer, as during the run, with the watchdog's notes, till it does. Its mark gone is a disk that went
        away, and the place it leaves would take what comes next, password hashes and all."""
        if side.answers():
            return
        told, self.started_told = self.started_told, True  # the run's "started" note isn't this hold's
        try:
            with self._watched():
                self._hold(side, None)
        finally:
            self.started_told = told

    def _hold_for_room(self, side: Side, e: OSError) -> None:
        self.why_held = f"{side.word} is full"
        if not self.full_said:
            self.full_said = True
            self.say(f"{side.word.capitalize()} is full ({_why(e)}): this goes on by itself when there is room.")
        self.sleep(WAITS[3])

    def _keep_trying(self, side: Side, fn: Callable, *args, missing=_TROUBLE,
                     gone: Callable[[OSError], bool] = _not_there, hold: bool = False, **kw):
        """fn(*args, **kw) on a disk that may go away meanwhile, until it's done: trouble waited out (_wait_out), with
        the same limit as the contents of a file (_another_go), as a bad spot can make the disk go away every time, and
        each drop looks like a new start. missing: what it returns when nothing is there, as fn's error says (gone),
        believed only when the disk answers, asked then (side.answers); one that doesn't is held for at once (hold, a
        look's way), or waited out as any trouble, which asks it again. Without missing, nothing there is trouble like
        any other: the file's own while its disk answers."""
        last = (0, 0)
        while True:
            try:
                return side.call(fn, *args, **kw)
            except _Trouble as t:
                nothing = missing is not _TROUBLE and gone(t.error)
                if nothing and side.answers():
                    return missing
                if nothing and hold:
                    self._hold(side, t.error)
                else:
                    self._wait_out(t)
                last = self._another_go(t.error, 0, last)

    def _look(self, side: Side, path: str) -> os.stat_result | None:
        """What is at this path, or None if nothing, on a disk known to answer: one that doesn't is held for at once."""
        return self._keep_trying(side, os.stat, path, follow_symlinks=False, missing=None, hold=True)

    # ---- the watchdog ----
    def _status(self) -> str:
        return ""

    def _started_note(self) -> str:
        return ""

    def _tell(self, heading: str, text: str, public: str = "") -> None:
        self.say(f"NOTE {heading}: {text}")
        self.notes.tell(f"{self.NAME}: {heading}", text, public or text)

    def _keep_pace(self) -> None:
        """What a look of the watchdog can learn of the speed. Copying has some."""

    def _tick(self) -> None:
        self._keep_pace()
        now = self.now()
        idle = now - self.last_move
        if idle >= TELL_AFTER and (self.told_at is None or now - self.told_at >= TELL_AGAIN):
            first = self.told_at is None
            self.told_at = now
            why = self.why_held or "a read or a write is hanging"
            since = time.strftime("%H:%M", time.localtime(time.time() - idle))
            self._tell("on hold", f"Nothing has moved since {since}: {why}. What was copied is kept, and this goes on by itself "
                                  "when it can." if first else f"Still on hold, since {since}: {why}. {self.STOP} ends it.")
        elif self.told_at is not None and idle < 2 * TICK:
            self.told_at = None
            self._tell("going on", f"It's moving again, as of {time.strftime('%H:%M')}.")
        if now - self.last_line >= LINE_EVERY:
            self.last_line = now
            line = self._status()
            if line:
                self.say(line)
                runs.progress(line)
        if not self.started_told and now - self.began >= TELL_STARTED:
            text = self._started_note()
            if text:
                self.started_told = True
                self._tell("started", text)
        if self.every and now - self.last_note >= self.every and idle < TELL_AFTER:
            self.last_note = now
            line = self._status().strip()
            if line:
                self._tell("how far", line)
        if now - self.last_again >= 120:
            self.last_again = now
            self.notes.again()

    def _watch(self, ended: threading.Event) -> None:
        while not ended.wait(TICK):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 - the copy matters more than telling about it
                self.say(f"(The watchdog: {type(e).__name__}.)")

    @contextlib.contextmanager
    def _watched(self):
        """A watchdog for as long as this lasts, with an end of its own: one caught in a tick as an earlier one ended
        (the run's, before a hold after it) ends all the same, and never watches beside the next."""
        self.began = self.last_move = self.last_line = self.last_note = self.now()
        from . import db

        ended = self._ended = threading.Event()
        thread = threading.Thread(target=db.carried(self._watch), args=(ended,), daemon=True, name="watchdog")  # with this run's context
        thread.start()
        try:
            yield
        finally:
            ended.set()

    def send_the_rest(self) -> None:
        """Notes that couldn't go yet, tried for a while before this ends: what they say has happened."""
        if not self.notes.waiting:
            return
        self.say("A note for your phone couldn't be sent yet. It's tried for up to an hour more, and Ctrl-C leaves it; the job itself has ended.")
        until = self.now() + RESEND_FOR
        with contextlib.suppress(KeyboardInterrupt):
            while self.notes.waiting and self.now() < until:
                self.sleep(120)
                self.notes.again()

    # ---- reading a whole file, going on after a drop ----
    def _read_whole(self, side: Side, path: str, other: str | None = None) -> tuple[_Digest, int]:
        """Its checksums and its length, past the cache, on a disk that may go away meanwhile."""
        digest, at, last = _Digest(other), 0, (0, 0)
        while True:
            try:
                for chunk in past_the_cache(path, at):
                    digest.update(chunk)
                    at += len(chunk)
                    self._moved_on()
                return digest, at
            except OSError as e:
                self._wait_out(_Trouble(side, e))
                last = self._another_go(e, at, last)


class Record:
    """What was copied and checked, one line per file, the last for a file counting: in batches on the disk."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self.waiting: list[str] = []
        self.sources: dict[str, dict] = {}  # what was copied from each: how to tell it's the same one
        self.since, self.bytes = 0.0, 0

    def load(self) -> None:
        for r in list(read_records(Path(self.path))):
            if isinstance(r.get("source"), str):
                self.sources[r["source"]] = r
            elif r.get("gone") is True and isinstance(r.get("p"), str):
                self.entries.pop(r["p"], None)
            elif isinstance(r.get("p"), str) and isinstance(r.get("h"), str):
                self.entries[r["p"]] = r

    def add(self, rel: str, size: int, mtime: int, sha: str, source: str, other: str = "") -> None:
        entry = {"p": rel, "s": size, "t": mtime, "h": sha, "f": source, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **({"x": other} if other else {})}
        self.entries[rel] = entry
        self.waiting.append(json.dumps(entry))
        self.bytes += size

    def note_source(self, about: dict) -> None:
        self.sources[about["source"]] = about
        self.waiting.append(json.dumps(about))

    def forget(self, rel: str) -> None:
        self.entries.pop(rel, None)
        self.waiting.append(json.dumps({"p": rel, "gone": True}))

    def write(self) -> None:
        """A batch at the end of the file, starting on a new line in case the last write was cut short; the same line twice
        does no harm."""
        data = ("\n" + "\n".join(self.waiting) + "\n").encode("ascii")
        with open(self.path, "ab", buffering=0) as fh:
            _write_all(fh, data)
            _keep_to_disk(fh)
        self.waiting.clear()
        self.bytes = 0


@dataclass
class Listing:
    """What a side holds."""
    files: dict[str, tuple[int, int]] = field(default_factory=dict)  # relative path, forward slashes -> (size, modified, in ns)
    folders: list[str] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)  # (path, what it is)
    unreadable: dict[str, str] = field(default_factory=dict)  # folder (ending in a slash) or file -> why


@dataclass
class Result:
    """How a run ended."""
    listing: Listing
    done: bool = False
    copied: int = 0
    copied_bytes: int = 0
    there: int = 0
    earlier: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    disagree: dict[str, str] = field(default_factory=dict)
    no_times: int = 0
    report: str = ""
    took: float = 0.0
    shrunk: str = ""  # the source has lost most of its files, or all
    went: set[str] = field(default_factory=set)  # files that left the source before they could be copied

    @property
    def size(self) -> int:
        return sum(s for s, _ in self.listing.files.values())


def _entries(path: str) -> tuple[list[tuple[str, os.stat_result]], list[tuple[str, OSError]]]:
    """What a folder holds, and what of it couldn't be looked at: that much, and not the rest of the folder."""
    found, bad = [], []
    with os.scandir(path) as folder:
        for entry in folder:
            try:
                found.append((entry.name, entry.stat(follow_symlinks=False)))
            except FileNotFoundError as e:
                if share_is_gone(e):
                    raise  # otherwise it just went
            except OSError as e:
                bad.append((entry.name, e))
    return found, bad


def _below_a_file(given: str) -> str:
    """The file a path is below, or "" if none is in its way. The system says only "not a directory" of a place below a
    file (Windows: "the path isn't there"), which a copy would otherwise take for a disk gone away and wait out, though
    no wait makes a folder in a file. Only what answers counts: trouble asking (a share that's away) is left to what
    comes next, and the path itself a file is said by check as it is."""
    asked = here = os.path.abspath(given)
    while True:
        try:
            st = os.stat(long_path(here))
        except (FileNotFoundError, NotADirectoryError) as e:
            up = os.path.dirname(here)
            if share_is_gone(e) or up == here:
                return ""
            here = up
            continue
        except (OSError, ValueError):
            return ""
        return "" if here == asked or stat.S_ISDIR(st.st_mode) else here


def _read_mark(path: str) -> str:
    """A target's mark, or nothing if it has none. Trouble reading it isn't the same as having none: making a new one would
    leave a run elsewhere with no target."""
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except (FileNotFoundError, NotADirectoryError) as e:
        if share_is_gone(e):
            raise
        return ""


def _targets_file() -> Path:
    return paths.place("data") / "hyllycopy-targets.json"


def _known_targets() -> dict:
    """The targets this computer has copied to, with their marks: one whose mark has gone isn't there, whatever its folder says."""
    return read_json(_targets_file())


def _sync_names() -> None:
    """Names on the disk too, on Linux and macOS, as the flushes saw to the contents. It's for the whole system, so a stuck
    disk elsewhere gets no say in how this ends."""
    if hasattr(os, "sync"):
        syncing = threading.Thread(target=os.sync, daemon=True)
        syncing.start()
        syncing.join(SYNC_WAIT)


def _is_clutter(name: str, folder: bool) -> bool:
    low = name.lower()
    return low in CLUTTER_FOLDERS if folder else low in CLUTTER_FILES or name.startswith("._")


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:  # a share that can't be asked right now: what comes next says so in words
        return os.path.abspath(path)


def overlap(source: str, target: str) -> str | None:
    """Why these two can't be a source and a target: the same place, by whatever way it's reached, would be every file
    "there already" and "checked" against itself. Copies landing inside the source would be copied in turn."""
    a, b = os.path.normcase(_real(source)), os.path.normcase(_real(target))
    if a == b:
        return "they are the same folder"
    if Path(b).is_relative_to(a):
        return "the target is inside the source"
    if Path(a).is_relative_to(b):
        return "the source is inside the target"

    def ancestors(path: str) -> Iterator[os.stat_result]:
        for p in (Path(path), *Path(path).parents):
            with contextlib.suppress(OSError):
                st = os.stat(p)
                if st.st_ino:  # some disks don't number their files
                    yield st

    def numbered(path: str) -> os.stat_result | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return st if st.st_ino else None

    here, there = numbered(source), numbered(target)
    if here and there and os.path.samestat(here, there):
        return "they are the same folder, by another way"
    if here and any(os.path.samestat(here, st) for st in ancestors(target)):
        return "the target is inside the source, by another way"
    if there and any(os.path.samestat(there, st) for st in ancestors(source)):
        return "the source is inside the target, by another way"
    return None


def looks_unmounted(target: str) -> bool:
    """Linux and macOS leave an empty folder where a disk isn't mounted, and what is copied there lands on the system's own
    disk: the first folder there is on the way to the target is empty, in the places disks are mounted, and isn't a disk."""
    if os.name == "nt":
        return False
    here = Path(os.path.abspath(target))
    while not here.exists() and here != here.parent:
        here = here.parent
    if not any(str(here) == place or str(here).startswith(place + "/") for place in NOT_THERE_YET):
        return False
    try:
        return not os.path.ismount(here) and here.is_dir() and not any(here.iterdir())
    except OSError:
        return False


def _places(table: str, column: int) -> list[str] | None:
    """The places in one column of a table written as fstab and mountinfo are, a line to a row, with a space in a name as
    \\040 (and a tab, a newline or a backslash so too); None when it can't be read."""
    try:
        with open(table, encoding="utf-8", errors="surrogateescape") as fh:
            rows = [line.split() for line in fh if not line.lstrip().startswith("#")]
    except OSError:
        return None
    return [os.path.normpath(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), row[column]))
            for row in rows if len(row) > column and row[column].startswith("/")]


def unmounted_place(target: str) -> str | None:
    """On Linux, the place at or above the target where /etc/fstab mounts a disk, while none is mounted there: what is
    copied there would land on the system's own disk, wherever that place is (/srv/books, a folder in the home folder),
    full or empty, first copy or not. The deepest such place on the way to the target counts, and only when nothing is
    mounted deeper on that way: /srv listed with noauto says nothing against a target on /srv/books, mounted, nor /mnt
    against one on a disk mounted by hand at /mnt/usb. Mounted is what the kernel's list says (MOUNTED_NOW), which knows
    a bind mount that os.path.ismount takes for an ordinary folder; without it, os.path.ismount of each folder on the
    way. fstab is only read; one that can't be, and every other system, says nothing."""
    if not ON_LINUX:
        return None
    listed = _places(FSTAB, 1)
    if not listed:
        return None
    mounted = _places(MOUNTED_NOW, 4)

    def on_the_way(place: str, here: str) -> bool:
        return here == place or here.startswith(place.rstrip("/") + "/")

    for here in dict.fromkeys((os.path.abspath(target), os.path.realpath(target))):  # as typed, and with links followed
        place = max((p for p in listed if on_the_way(p, here)), key=len, default=None)
        if place is None:
            continue
        if mounted is None:  # no list to go by: each folder on the way asked, the deepest first
            deepest = next((p for p in (here, *map(str, Path(here).parents)) if os.path.ismount(p)), "/")
        else:
            deepest = max((p for p in mounted if on_the_way(p, here)), key=len, default="/")
        if len(place) > len(deepest) and os.path.realpath(place) not in (mounted or ()) and not os.path.ismount(place):
            return place  # (a place that is a link is mounted where it leads)
    return None


class Copying(_Job):
    """hylly copy SOURCE TARGET."""

    THEN = " The originals are yours to delete when you are ready."
    HAS_EVERYTHING = True
    ask_first: Callable[[str], None] | None = None  # hylly copy's and backup's question to its owner, which raises if it's no
    asked = False  # whether it was asked before the run began (ask_before), so that the run doesn't ask the same again

    def __init__(self, source, target, everything: bool = False, new_disk: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.source, self.target = Side(source, "the source"), Side(target, "the target")
        self.everything, self.new_disk = everything, new_disk
        self.folder = self.target.path(FOLDER)
        self.record = Record(self.target.path(f"{FOLDER}/record.jsonl"))
        self.source_id = hashlib.sha256(os.path.normcase(self.source.root).encode("utf-8", "surrogatepass")).hexdigest()[:16]
        self.tries: Counter[str] = Counter()
        self.failed: dict[str, str] = {}
        self.final: set[str] = set()
        self.made: set[str] = set()
        self.windows_names, self.blind = os.name == "nt", os.name == "nt" or sys.platform == "darwin"
        self.one_form = sys.platform == "darwin"  # whether the disk takes é written in its two ways for one name
        self.lists: dict[str, tuple[str, str, str]] = {}
        self.in_a_row = self.listed = 0
        self.there_folders: set[str] = set()
        self.links_there: set[str] = set()
        self.target_short = False  # a folder on the target couldn't be listed, so not all of it is known
        self.last_listing: Listing | None = None
        self.ever_listed: set[str] = set()
        self.first_count = 0
        self.cooling, self.no_keeping, self.other_numbers = False, False, False
        self.total_files = self.total_bytes = self.done_files = self.done_bytes = self.current = 0
        self.pace = _Pace()

    def _keep_pace(self) -> None:
        self.pace.seen(self.now(), self.done_files, self.done_bytes + self.current, self.began)

    def _leave_out(self, rel: str) -> str | None:
        """A file or folder that a copy of this kind leaves out, and why. hylly backup has some."""
        return None

    def _left_out_words(self, n: int, what: str) -> str:
        """How many of a kind are left out, for the dry run's line: "3 × a link". hylly backup has words of its own for
        one of its kinds."""
        return f"{n:,} × {what}"

    SETS_ASIDE = False  # whether a different file with a file's name is moved out of its way (_set_aside): hylly backup's is

    def _set_aside(self, rel: str, dst: str, name_it: Callable[[], None]) -> None:
        """The different file that has this one's name moved out of its way, and name_it giving the new copy, whole and
        checked by then, the name. A copy never does (SETS_ASIDE): hylly backup does."""
        raise NotImplementedError

    # ---- before anything ----
    def ask_before(self, ask: Callable[[str], None]) -> None:
        """hylly copy's and backup's question, before anything is made on the target when it's the first copy there, and
        again during the run when it would write far more than before, or when nothing came from this source before
        (_far_more). ask raises when the answer is no, or nobody is there to give one."""
        if self.first_time():
            ask(self.first_words())
            self.asked = True
        self.ask_first = ask

    def first_time(self) -> bool:
        """Whether nothing has been copied from this source to this target before, by the target's record: read only, so
        that a mistyped target is asked about before anything is made there. A target this computer has copied to before
        is no first time: if its mark has gone, as with a disk not mounted, prepare refuses it, and says why."""
        if os.path.normcase(self.target.root) in _known_targets():
            return False
        record = Record(self.target.path(f"{FOLDER}/record.jsonl"))
        with contextlib.suppress(OSError):
            record.load()
        return not any(e.get("f") == self.source_id for e in record.entries.values())

    def first_words(self) -> str:
        """Why a first run asks."""
        return (f"Nothing has been copied from {shown(self.source.given)} to {shown(self.target.given)} before: this first run "
                "copies all of it (--dry-run shows how much, and the room there).")

    def _far_more(self, listing: Listing, todo: list[str], sizes: dict[str, int]) -> str:
        """Why this run asks, or "": nothing was copied from this source to this target before, though the target has had
        copies (of another source: a mistyped source, say), unless that was asked already (asked); or it would write more
        than all that was copied from this source before, and more than FAR_MORE, which a target holding a copy of something
        else, or a source that isn't what it was, can look like. Nothing to write asks nothing."""
        need = sum(listing.files[rel][0] for rel in todo if sizes.get(self._key(rel)) != listing.files[rel][0])
        before = sum(e.get("s", 0) for e in self.record.entries.values() if e.get("f") == self.source_id)
        if not need or (not before and self.asked):
            return ""
        if not before:
            return self.first_words()
        if need <= max(before, FAR_MORE):
            return ""
        return (f"This run would write {size_words(need)} to {shown(self.target.given)}, more than all {size_words(before)} "
                f"copied there from {shown(self.source.given)} before.")

    def check(self) -> None:
        """What can't work, said at once. A disk that isn't there at the start is more likely mistyped than away, and a
        place below a file can be neither: no wait makes a folder there."""
        for side in (self.source, self.target):
            below = _below_a_file(side.given)
            if below:
                raise Exit(USAGE, f"The {side.word.removeprefix('the ')} should be a folder: {shown(side.given)} is below "
                                  f"{shown(below)}, which is a file, and no folder can be made in a file. Nothing was copied.")
        try:
            here = os.stat(self.source.root)
        except OSError as e:
            raise Exit(LOCAL, f"Can't read the source, {shown(self.source.given)}: {_why(e)}.") from None
        if not stat.S_ISDIR(here.st_mode):
            raise Exit(USAGE, f"The source should be a folder: {shown(self.source.given)} isn't one.")
        with contextlib.suppress(OSError):
            if not stat.S_ISDIR(os.stat(self.target.root).st_mode):
                raise Exit(USAGE, f"The target should be a folder: {shown(self.target.given)} isn't one.")
        why = overlap(self.source.root, self.target.root)
        if why:
            raise Exit(USAGE, f"Not a source and a target for a copy: {why}. Nothing was copied.")
        away = unmounted_place(self.target.root)
        if away:
            raise Exit(NOT_SET_UP, f"{shown(self.target.given)} is on {shown(away)}, where /etc/fstab mounts a disk, and no disk is mounted there now: "
                                   "what is copied there would land on the system's own disk. Mount the disk first. Nothing was copied.")
        if looks_unmounted(self.target.root):
            raise Exit(NOT_SET_UP, f"{shown(self.target.given)} looks like a place where a disk isn't mounted: what is copied there would land on the "
                                   "system's own disk. Mount the disk first, or give a folder that has something in it. Nothing was copied.")
        self.source.numbers = (here.st_dev, here.st_ino) if here.st_ino else None
        try:
            self.source.seen = sorted(name for name, _ in _entries(self.source.root)[0] if name != FOLDER)[:8]
        except OSError as e:
            raise Exit(LOCAL, f"Can't read the source, {shown(self.source.given)}: {_why(e)}.") from None
        if not self.source.seen:
            raise Exit(LOCAL, f"There is nothing in the source, {shown(self.source.given)}. Is the disk there? Nothing was copied.")

    def prepare(self) -> None:
        """The target's own folder, and what the disk is like. Only the things of this folder are changed. A target copied to
        before has its mark, or it isn't the target: an unmounted disk leaves a folder that would take everything gladly."""
        mark, key, known = self.target.path(f"{FOLDER}/id"), os.path.normcase(self.target.root), _known_targets()
        try:
            token = _read_mark(mark)
            if key in known and token != known[key] and not self.new_disk:
                raise Exit(NOT_SET_UP, f"A copy was made to {shown(self.target.given)} before, and the mark it left there is {'another now' if token else 'gone'}. "
                                       "Is the disk mounted? Without it, what is copied would land on the system's own disk. If this really is a new disk, or "
                                       "a folder you emptied, add --new-disk. Nothing was copied.")
            os.makedirs(self.target.root, exist_ok=True)
            os.makedirs(self.folder, exist_ok=True)
            token = token or self._make_mark(mark)
            os.makedirs(self.target.path(f"{FOLDER}/parts"), exist_ok=True)
            self._probe()
        except OSError as e:
            raise Exit(LOCAL, f"Can't read or write the target, {shown(self.target.given)}: {_why(e)}. Nothing was copied.") from None
        self.target.mark = (f"{FOLDER}/id", token)
        if known.get(key) != token:
            with contextlib.suppress(OSError):
                atomic_write(_targets_file(), json.dumps({**known, key: token}, indent=1), durable=False)
        self._not_itself(FOLDER)
        self._not_around_the_source()

    def _make_mark(self, mark: str) -> str:
        """A new mark, unless another run has made one this moment: then that one counts."""
        token = secrets.token_hex(16)
        tmp = f"{mark}.{token[:8]}.tmp"
        _write_here(tmp, token + "\n")
        try:
            _no_overwrite_name(tmp, mark)
            return token
        except FileExistsError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            theirs = _read_mark(mark)
            if theirs:
                return theirs
            _write_here(mark, token + "\n")  # an empty one, which nobody could have gone by
            return token

    def _probe(self) -> None:
        """Whether the target goes by Windows' rules for names, and tells capitals apart: the disk says, not the system."""
        if os.name != "nt":
            odd = self.target.path(f"{FOLDER}/probe:name")
            try:
                with open(odd, "wb"):
                    pass
                self.windows_names = False
                os.unlink(odd)
            except OSError:
                self.windows_names = True
        probe = self.target.path(f"{FOLDER}/CaseProbe")
        with open(probe, "wb"):
            pass
        try:
            self.blind = os.path.lexists(self.target.path(f"{FOLDER}/caseprobe"))
        finally:
            os.unlink(probe)
        formed = self.target.path(f"{FOLDER}/probe-\u00e9")
        with contextlib.suppress(OSError):
            with open(formed, "wb"):
                pass
            try:
                self.one_form = os.path.lexists(self.target.path(f"{FOLDER}/probe-e\u0301"))
            finally:
                os.unlink(formed)

    def _not_itself(self, rel: str) -> None:
        """The target's own mark, seen from the source: the two are the same place after all, by a way only this shows."""
        if not self.target.mark:
            return
        with contextlib.suppress(OSError, ValueError), open(self.source.path(f"{rel}/id"), encoding="ascii") as fh:
            if fh.read().strip() == self.target.mark[1]:
                raise Exit(USAGE, "Not a source and a target for a copy: the target is inside the source, by a way only a look at the files shows. "
                                  "Nothing more was copied.")

    def _not_around_the_source(self) -> None:
        """The target's own mark above the source: a share that gives no file numbers can leave nothing else to go by."""
        for parent in Path(self.source.root).parents:
            with contextlib.suppress(OSError, ValueError), open(parent / FOLDER / "id", encoding="ascii") as fh:
                if fh.read().strip() == self.target.mark[1]:
                    raise Exit(USAGE, "Not a source and a target for a copy: the source is inside the target, by a way only a look at the files shows. "
                                      "Nothing was copied.")

    def _another_source(self) -> bool:
        """Whether this isn't the source this target was copied from before, though the path is the same: an unmounted disk's
        folder with something lying in it would otherwise be copied, and said to be done. By its numbers, or most of its names
        as a disk mounted again may get new numbers. A source whose files you've since removed is still the same folder."""
        before = self.record.sources.get(self.source_id)
        if not before:
            return False
        numbers = before.get("numbers")
        names = [name for name in before.get("seen", []) if isinstance(name, str)]
        return not _same_folder(self.source, self.source.numbers, numbers if isinstance(numbers, list) else None, names)

    def _not_the_source(self) -> Exit:
        return Exit(NOT_SET_UP, f"This doesn't look like the source that was copied to this target before: {shown(self.source.given)}. Is the disk mounted? "
                                "If this really is another disk now, add --new-disk. Nothing was copied.")

    def _note_the_source(self) -> None:
        if self._another_source() and not self.new_disk:
            raise self._not_the_source()
        before = self.record.sources.get(self.source_id)
        self.other_numbers = bool(before) and list(self.source.numbers or ()) != before.get("numbers")
        about = {"source": self.source_id, "numbers": list(self.source.numbers or ()), "seen": self.source.seen}
        if self.record.sources.get(self.source_id) != about:
            self.record.note_source(about)

    # ---- listing ----
    def _read_folder(self, side: Side, rel: str) -> tuple[list[tuple[str, os.stat_result]], list[tuple[str, OSError]]]:
        found = self._keep_trying(side, _entries, side.path(rel))
        self._moved_on()
        return found

    def _list_source(self) -> Listing:
        """The last listing is what "done" goes by, so it isn't believed lightly. An unmounted disk lists as an empty folder with
        no error to go by, before the listing or during it, and a file that dropped out of it is asked about by itself."""
        while True:
            if not self.source.answers():
                self._hold(self.source, None)
            found = self._walk_source()
            if self.source.answers():
                break
        self._look_for_what_dropped_out(found)
        self.last_listing = found
        self.ever_listed.update(found.files)
        return found

    @staticmethod
    def _same_to_a_disk(rel: str) -> str:
        return unicodedata.normalize("NFC", rel).lower()

    def _look_for_what_dropped_out(self, found: Listing) -> None:
        dropped = [(rel, now) for rel, now in (self.last_listing.files.items() if self.last_listing else ()) if rel not in found.files]
        if not dropped:
            return
        listed, folders_gone = {self._same_to_a_disk(rel) for rel in found.files}, set()
        folders = set(found.folders)
        for rel, now in dropped:
            self._touch()
            folder = rel.rpartition("/")[0]
            if folder in folders_gone or self._same_to_a_disk(rel) in listed:  # another spelling of a name, on a disk that takes both for one
                continue
            try:
                if folder and folder not in folders and self._look(self.source, self.source.path(folder)) is None:
                    folders_gone.add(folder)
                    continue
                st = self._look(self.source, self.source.path(rel))
            except FileProblem:
                found.files[rel] = now
            else:
                if st is not None and stat.S_ISREG(st.st_mode):
                    found.files[rel] = (st.st_size, st.st_mtime_ns)

    def _walk_source(self) -> Listing:
        found, stack = Listing(), [""]
        while stack:
            folder = stack.pop()
            try:
                entries, bad = self._read_folder(self.source, folder)
            except FileProblem as p:
                found.unreadable[f"{folder or '.'}/"] = f"this folder can't be read, so what's in it isn't known: {p.why}"
                continue
            for name, e in bad:
                found.unreadable[f"{folder}/{name}" if folder else name] = f"it can't be looked at: {_why(e)}"
            for name, st in entries:
                rel = f"{folder}/{name}" if folder else name
                if name == FOLDER and stat.S_ISDIR(st.st_mode):
                    self._not_itself(rel)  # the target's own, at any depth, is the target inside the source
                    if not folder:  # the source's own, as it was a copy's target: a copy of a copy gets one of its own
                        continue
                    # deeper, it's another copy's, as a backup's library/.hyllycopy: its record and checksums go along
                if is_link_stat(st):  # a link or a Windows junction, as storage tells them for the backup's zip too
                    found.left_out.append((rel, "a link"))
                elif stat.S_ISDIR(st.st_mode):
                    if why := self._leave_out(rel):
                        found.left_out.append((rel, why))
                    elif not self.everything and _is_clutter(name, True):
                        found.left_out.append((rel, "the system's own folder"))
                    else:
                        found.folders.append(rel)
                        stack.append(rel)
                elif not stat.S_ISREG(st.st_mode):
                    found.left_out.append((rel, "not a file"))
                elif why := self._leave_out(rel):
                    found.left_out.append((rel, why))
                elif not self.everything and _is_clutter(name, False):
                    found.left_out.append((rel, "the system's own file"))
                else:
                    found.files[rel] = (st.st_size, st.st_mtime_ns)
                    self.listed = len(found.files)
        return found

    def _on_target(self) -> dict[str, int]:
        """What the target holds, by name as this disk tells names apart: a folder listed costs a trip, not one per file."""
        sizes, stack = {}, [""]
        self.there_folders, self.links_there, self.target_short = set(), set(), False
        while stack:
            folder = stack.pop()
            try:
                entries, bad = self._read_folder(self.target, folder)
            except FileProblem:
                self.target_short = True
                continue  # not there yet, or not readable: what's in it counts as not there
            self.target_short = self.target_short or bool(bad)
            for name, st in entries:
                rel = f"{folder}/{name}" if folder else name
                if name == FOLDER and not folder:
                    continue
                if is_link_stat(st):
                    self.links_there.add(self._key(rel))
                elif stat.S_ISDIR(st.st_mode):
                    stack.append(rel)
                    self.there_folders.add(self._key(rel))
                elif stat.S_ISREG(st.st_mode):
                    sizes[self._key(rel)] = st.st_size
        return sizes

    def _key(self, rel: str) -> str:
        """A name as the target's disk tells names apart, and no further: Straße and Strasse are two files on any of them."""
        key = unicodedata.normalize("NFC", rel) if self.one_form else rel
        return key.lower() if self.blind else key

    def _through_a_link(self, rel: str) -> str:
        """Why nothing goes here: writing through a link on the target would land somewhere else, and never check out."""
        parts = rel.split("/")
        if self.links_there and any(self._key("/".join(parts[:i])) in self.links_there for i in range(1, len(parts))):
            return "on the target, a link has the name of a folder it goes in, and nothing is written through a link"
        return ""

    def _no_home(self, listing: Listing) -> dict[str, str]:
        """Files the target's disk can't hold, by their names, or next to another."""
        no_home, seen = {}, {}
        for rel in sorted(listing.files):
            why = name_problem(rel, self.windows_names) or self._through_a_link(rel)
            if why:
                no_home[rel] = why
            elif self.blind or self.one_form:
                key = self._key(rel)
                if key in seen:
                    no_home[rel] = f"this disk doesn't tell capitals or forms of a letter apart, and another file is the same name to it: {shown(seen[key])}"
                else:
                    seen[key] = rel
        return no_home

    def _settled(self, rel: str, now: tuple[int, int], sizes: dict[str, int]) -> bool:
        """A record's word holds while the source's file is unchanged, from the same source, and the copy has its full size."""
        entry = self.record.entries.get(rel)
        return bool(entry) and (entry.get("s"), entry.get("t")) == now and entry.get("f") == self.source_id and sizes.get(self._key(rel)) == now[0]

    # ---- the source's own lists of checksums ----
    def _read_lists(self, listing: Listing) -> None:
        self.lists = {}
        for rel, (size, _) in sorted(listing.files.items()):
            kind = LIST_KINDS.get(os.path.splitext(rel)[1].lower())
            if not kind or size > MAX_LIST:
                continue
            try:
                raw = self._read_small(rel)
            except FileProblem:
                continue
            folder = rel.rpartition("/")[0]
            for name, value in read_list(raw.decode("utf-8", "replace"), kind):
                listed = "/".join(p for p in name.replace("\\", "/").split("/") if p != ".")
                if ".." in listed.split("/"):
                    continue
                listed = f"{folder}/{listed}" if folder else listed
                if listed in listing.files:
                    self.lists.setdefault(listed, (kind, value, rel))

    def _read_small(self, rel: str) -> bytes:
        def read() -> bytes:
            with open(self.source.path(rel), "rb") as fh:
                return fh.read(MAX_LIST + 1)

        return self._keep_trying(self.source, read)

    # ---- one file ----
    def _part(self, rel: str) -> str:
        """Its place while it's being written. In a folder of their own: names near a disk's limit have no room for a suffix,
        a source may hold such a name itself, and a stop leaves nothing half-done among your files."""
        name = hashlib.sha256(rel.encode("utf-8", "surrogatepass")).hexdigest()[:32]
        return self.target.path(f"{FOLDER}/parts/{name}.part")

    def _ensure_folder(self, folder: str) -> None:
        if not folder or folder in self.made:
            return
        if not self.target.answers():  # or the folders would be made on the place an unmounted disk leaves
            self._hold(self.target, None)
        self._keep_trying(self.target, _make_below, self.target.root, folder)
        self.made.add(folder)

    def _go_on_from(self, inp, out, moving: _Moving) -> None:
        """What an earlier run left, compared with the source as far as it's the same: a power cut can leave nonsense at its
        end, and the checksum has to be of the source's bytes, not of what was there."""
        s, t = self.source, self.target
        moving.comparing = True
        have = t.call(os.fstat, out.fileno()).st_size
        compared = 0
        while moving.at < have:
            theirs = s.call(inp.read, CHUNK)
            ours = t.call(out.read, len(theirs)) if theirs else b""
            if not ours or ours != theirs[:len(ours)]:
                break
            moving.digest.update(ours)
            moving.at += len(ours)
            compared += len(ours)
            s.call(inp.seek, moving.at)
            self.current = moving.at
            if moving.at > moving.furthest:
                moving.furthest = moving.at
                self._moved_on()
            if compared >= ON_DISK_EVERY:  # it's on the disk, read from there: a drop needn't compare this much again
                moving.kept, compared = (moving.at, moving.digest.copy()), 0
        if moving.at >= 1 << 20:
            self.say(f"  going on from {size_words(moving.at)} that an earlier try copied")
        t.call(out.truncate, moving.at)
        t.call(out.seek, moving.at)
        s.call(inp.seek, moving.at)
        moving.kept = (moving.at, moving.digest.copy())
        moving.comparing = False

    def _move(self, src: str, part: str, moving: _Moving) -> None:
        s, t = self.source, self.target
        inp = s.call(open, src, "rb", 0)
        try:  # on Linux and macOS a part is made with the permissions of its original, which it then keeps (_open_part),
            # and always writable by its owner: a read-only one couldn't be opened again to go on after a drop
            mode = 0o666 if os.name == "nt" else stat.S_IMODE(s.call(os.fstat, inp.fileno()).st_mode) & 0o777 | 0o200
            out = t.call(_open_part, part, mode)
            try:
                if moving.kept is None or t.call(os.fstat, out.fileno()).st_size < moving.kept[0]:  # or the share lost what it said was safe
                    moving.at, moving.digest, moving.kept = 0, _Digest(moving.digest.other_kind), None
                    self._go_on_from(inp, out, moving)
                else:
                    moving.at, moving.digest = moving.kept[0], moving.kept[1].copy()
                    t.call(out.seek, moving.at)
                    s.call(inp.seek, moving.at)
                    if moving.comparing:  # or the rest of what was being compared, good as it may be, would be cut off
                        self._go_on_from(inp, out, moving)
                    else:
                        t.call(out.truncate, moving.at)
                since = 0
                while chunk := s.call(inp.read, CHUNK):
                    t.call(_write_all, out, chunk)
                    moving.digest.update(chunk)
                    moving.at += len(chunk)
                    since += len(chunk)
                    self.current = moving.at
                    if moving.at > moving.furthest:  # the same bytes written again after a drop aren't progress, or an error here would never add up
                        self.moved_bytes += moving.at - moving.furthest
                        moving.furthest = moving.at
                        self._moved_on()
                    else:  # catching up after a failure: flushed as it goes, so that a share failing now and then still gets ahead
                        t.call(_keep_to_disk, out)
                        moving.kept, since = (moving.at, moving.digest.copy()), 0
                        self._moved_on()
                    if since >= ON_DISK_EVERY:
                        t.call(_keep_to_disk, out)
                        moving.kept, since = (moving.at, moving.digest.copy()), 0
                t.call(_keep_to_disk, out)
                moving.kept = (moving.at, moving.digest.copy())
                t.call(out.close)
            except BaseException:
                with contextlib.suppress(OSError):
                    out.close()
                raise
        finally:
            with contextlib.suppress(OSError):
                inp.close()

    def _write_part(self, src: str, part: str, other: str | None) -> _Digest:
        moving, last = _Moving(_Digest(other)), (0, 0)
        while True:
            try:
                self._move(src, part, moving)
                return moving.digest
            except _Trouble as t:
                self._wait_out(t)
                last = self._another_go(t.error, moving.kept[0] if moving.kept else 0, last)

    def _drop(self, part: str) -> None:
        """A part removed, or found gone: by any "no such file" (a share gone's too), while the target answers, as one
        that is there after all costs nothing: a next try goes on from it as far as it is the same, and the end clears
        the parts away (_clean_parts)."""
        self._keep_trying(self.target, os.unlink, part, missing=None, gone=lambda e: isinstance(e, FileNotFoundError))

    def _keep_time(self, part: str, begin: os.stat_result, result: Result) -> None:
        """Its time. Its permissions it has had since it was made (_open_part): a chmod on the target, the library's next
        disk or a backup's, would be stored there as extended attributes by some (Linux's ntfs3), and fails on a disk
        that refuses it. A moment's trouble is tried again, as the next run won't mend the time; a disk that won't keep
        it is no reason not to have the file, and the report tells."""
        tries, holds = 3 if self.no_keeping else 0, 0  # a disk that has refused all tries at one file isn't asked so patiently again
        while True:
            try:
                self.target.call(os.utime, part, ns=(begin.st_mtime_ns, begin.st_mtime_ns))
                self.no_keeping = False
                return
            except _Trouble as t:
                if not self.target.answers() and holds < GOES_AT_ONE_SPOT:
                    self._hold(self.target, t.error)
                    holds += 1
                elif isinstance(t.error, PermissionError) or tries >= 3 or holds >= GOES_AT_ONE_SPOT:
                    result.no_times += 1
                    self.no_keeping = True
                    return
                else:
                    self.sleep(WAITS[tries])
                    tries += 1

    def _name_it(self, rel: str, part: str, dst: str, sha: str) -> None:
        self._ensure_folder(rel.rpartition("/")[0])
        last = (0, 0)
        while True:
            try:
                self.target.call(_no_overwrite_name, part, dst)
                return
            except _Trouble as t:
                e = t.error
                if isinstance(e, PermissionError) and long_for_a_share(dst):
                    raise FileProblem(TOO_LONG, again=False) from None
                if not isinstance(e, (FileExistsError, FileNotFoundError)) or not self.target.answers():
                    self._wait_out(t)
                    last = self._another_go(e, 0, last)
                    continue
                # It may have worked, with the answer lost as the share went away: is what has the name now this very file?
                there = self._look(self.target, dst)
                if there is not None and stat.S_ISREG(there.st_mode) and self._read_whole(self.target, dst)[0].hex() == sha:
                    self._drop(part)
                    return
                if isinstance(e, FileExistsError):
                    raise FileProblem("another file took its name on the target meanwhile; nothing was overwritten", again=False) from None
                self.made.clear()  # or its folder is what has gone
                raise FileProblem("what was being written is gone from the target") from None

    def _already_there(self, rel: str, listed: tuple[int, int], begin: os.stat_result, there: os.stat_result, result: Result) -> None:
        """Something has its name, and the record doesn't tell of it: only compared. Never overwritten."""
        conflict = "a different file has its name on the target; nothing was overwritten. If the one on the source is newer, " \
                   "delete or move the other, and copy again"
        if not stat.S_ISREG(there.st_mode):
            raise FileProblem("a folder or a link has its name on the target; nothing was overwritten", again=False)
        if there.st_size != begin.st_size:
            raise _Different(conflict, again=False)
        wants = self.lists.get(rel)
        ours, _ = self._read_whole(self.target, self.target.path(rel))
        theirs, _ = self._read_whole(self.source, self.source.path(rel), wants[0] if wants else None)
        if ours.hex() != theirs.hex():
            raise _Different(conflict, again=False)
        self.record.add(rel, listed[0], listed[1], ours.hex(), self.source_id, wants[2] if wants and theirs.of_other() != wants[1] else "")
        result.there += 1

    def _copy_file(self, rel: str, listed: tuple[int, int], result: Result) -> None:
        src, dst, part = self.source.path(rel), self.target.path(rel), self._part(rel)
        wants = self.lists.get(rel)
        self.strikes, self.current = 0, 0
        in_the_way = False  # a different file has its name, moved aside only once the new copy is whole: till then it's the good one
        while True:
            begin = self._look(self.source, src)
            if begin is None or not stat.S_ISREG(begin.st_mode):
                raise FileProblem("it's gone from the source", gone=True)
            there = self._look(self.target, dst)
            if there is not None and not (in_the_way and stat.S_ISREG(there.st_mode)):
                try:
                    return self._already_there(rel, listed, begin, there, result)
                except _Different:
                    if not self.SETS_ASIDE:
                        raise
                    in_the_way = True
            digest = self._write_part(src, part, wants[0] if wants else None)
            self._keep_time(part, begin, result)
            sha = digest.hex()
            back, length = self._read_whole(self.target, part)
            end = self._look(self.source, src)
            changed = end is None or (end.st_size, end.st_mtime_ns) != (begin.st_size, begin.st_mtime_ns)
            if changed or back.hex() != sha or length != begin.st_size:
                why = "it changed while it was being copied" if changed else \
                      f"the copy read back differently from what was read from the source ({length:,} bytes, {begin.st_size:,} expected)"
                self._drop(part)
                self.tries[rel] += 1
                if self.tries[rel] >= TRIES:
                    raise FileProblem(f"{why}, {TRIES} times")
                self.say(f"  {shown(rel)}: {why}. Going at it again.")
                continue
            if in_the_way:
                self._set_aside(rel, dst, lambda: self._name_it(rel, part, dst, sha))
            else:
                self._name_it(rel, part, dst, sha)
            self.record.add(rel, listed[0], listed[1], sha, self.source_id, wants[2] if wants and digest.of_other() != wants[1] else "")
            result.copied += 1
            result.copied_bytes += begin.st_size
            return None

    def _keep_at_it(self, fn: Callable, *args, held: str = "") -> FileProblem | None:
        """fn(*args) on the target, for the copy's own files there, until it's done (None): trouble waited out
        (_wait_out), with no limit of goes, as they are no file of the source's to give up on. Trouble of the file's
        own (FileProblem: another program holding it for a while, say) is waited out too, WAITS[3] at a time, while
        held says what is held (why_held), for the record, which the copy can't do without; without held, it ends the
        tries and is returned, for what can be done without (the report, a checksum noted)."""
        while True:
            try:
                self.target.call(fn, *args)
                return None
            except _Trouble as t:
                try:
                    self._wait_out(t)
                except FileProblem as p:
                    if not held:
                        return p
                    self.why_held = f"{held} ({p.why})"
                    self.sleep(WAITS[3])

    def _keep_record(self, force: bool = False) -> None:
        r = self.record
        if not r.waiting:
            return
        if not force and len(r.waiting) < RECORD_FILES and self.now() - r.since < RECORD_SECONDS and r.bytes < RECORD_BYTES:
            return
        self._keep_at_it(r.write, held="the record on the target can't be written")
        r.since = self.now()
        self._moved_on()

    # ---- a pass over the files ----
    def _copy_all(self, todo: list[str], listing: Listing, result: Result) -> None:
        for rel in todo:
            listed = listing.files[rel]
            if listed[0] >= 1 << 30:
                self.say(f"{shown(rel)} ({size_words(listed[0])})")
            try:
                self._copy_file(rel, listed, result)
                self.failed.pop(rel, None)
                self.in_a_row, self.cooling = 0, False
            except FileProblem as p:
                self.tries[rel] += 1
                if p.gone:  # a failure of nothing: if it never comes back, the report says it left
                    self.failed.pop(rel, None)
                else:
                    self.failed[rel] = p.why
                    if not p.again:
                        self.final.add(rel)
                    # most often only a share that dropped at that moment: what is really lost, the report lists at the end
                    self.say(f"  {'Will try again later' if p.again and self.tries[rel] < TRIES else 'FAILED'}: {shown(rel)}: {p.why}")
                self.in_a_row = self.in_a_row + 1 if p.disk else 0
                if self.in_a_row >= IN_A_ROW:
                    self._cool_down(p)
            self.done_files += 1
            self.done_bytes += listed[0]
            self.current = 0
            self._moved_on()
            self._keep_record()

    def _cool_down(self, p: FileProblem) -> None:
        """Files in a row failing although the disks answer sounds like trouble with a disk, which doesn't get better by being
        worn out. On hold, one file every half hour, until one goes through: nobody has to be there to start it again."""
        self._keep_record(force=True)
        if not self.cooling:
            self.cooling = True
            self._tell("trouble with a disk", f"{IN_A_ROW} files in a row failed although the disks answer ({p.why}). It tries one file every half hour now, so as "
                                              "not to wear out a disk in trouble, and is back at full speed as soon as one goes through. What was copied is kept, "
                                              f"and {self.STOP} ends it.")
        self.why_held = "files keep failing"
        self.sleep(COOL_DOWN)

    def _make_folders(self, listing: Listing) -> None:
        """Folders with nothing in them are made too. A file in one has made it already."""
        for folder in sorted(listing.folders):
            through = self._through_a_link(folder) or ("on the target, a link has its name" if self._key(folder) in self.links_there else "")
            try:
                if through:
                    raise FileProblem(through, again=False)
                if self._key(folder) not in self.there_folders:
                    self._ensure_folder(folder)
                self.failed.pop(folder + "/", None)
            except FileProblem as p:
                self.failed[folder + "/"] = p.why

    def _status(self) -> str:
        if not self.total_files:
            return f"Listing the source: {many(self.listed, 'file')} so far." if self.listed else ""
        done = self.done_bytes + self.current
        line = f"{100 * done / max(self.total_bytes, 1):5.1f}%  {size_words(done)} of {size_words(self.total_bytes)}, " \
               f"{self.done_files:,} of {many(self.total_files, 'file')}"
        left = self._left()
        return line + (f", {time_words(left, about=True)} to go" if left else "")

    def _left(self) -> float:
        """By the time a file costs and the time its bytes do, as they've been so far (_Pace)."""
        done = self.done_bytes + self.current
        if self.now() - self.began < 1 or (not done and not self.done_files):
            return 0
        return self.pace.left(self.total_files - self.done_files, self.total_bytes - done)

    def _started_note(self) -> str:
        if not self.total_files:
            return ""
        left = self._left()
        return f"{many(self.total_files, 'file')}, {size_words(self.total_bytes)}, to copy." + (f" {time_words(left, about=True).capitalize()} at the speed so far." if left else "")

    # ---- the whole copy ----
    def run(self) -> Result:
        with self._watched():
            try:
                return self._run()
            except BaseException as e:
                with contextlib.suppress(OSError):
                    if self.record.waiting:
                        self.record.write()
                if isinstance(e, KeyboardInterrupt):
                    self.say("Stopped with Ctrl-C. What was copied is kept: the same command goes on from there.")
                raise

    def _load_record(self) -> None:
        self._keep_at_it(self.record.load, held="the record on the target can't be read")

    def _not_finished(self) -> bytes | None:
        """Until this run has ended, the last one's "done" doesn't hold: written before anything else. Returns the report it
        replaced, for a run that ends before it has begun (its owner's no, ask_first), which puts it back."""
        before = None
        with contextlib.suppress(OSError), open(self.target.path(f"{FOLDER}/report.txt"), "rb") as fh:
            before = fh.read()
        try:
            _write_here(self.target.path(f"{FOLDER}/report.txt"),
                        f"HyllyCopy report, {time.strftime('%Y-%m-%d %H:%M')}\nFrom: {shown(self.source.given)}\nTo:   {shown(self.target.given)}\n\n"
                        "NOT FINISHED: a copy began then, and this report is written when it ends. It's running, or it was stopped: "
                        "the same command goes on from there. Don't delete the originals yet.\n")
        except OSError as e:
            self.say(f"{FOLDER}/report.txt couldn't be changed ({_why(e)}), so until this ends it may still tell of an earlier run.")
        return before

    def _run(self) -> Result:
        before = self._not_finished()
        self._load_record()
        self._note_the_source()
        self.say(f"{shown(self.source.given)} to {shown(self.target.given)}: listing the source.")
        listing = self._list_source()
        self.first_count = len(listing.files)
        if self.other_numbers and not self.new_disk:  # its names may be there by chance, but not its files: a removal of the originals keeps the numbers
            copied_from = sum(e.get("f") == self.source_id for e in self.record.entries.values())
            if lost_most(copied_from, self.first_count):
                raise self._not_the_source()
        result = Result(listing)
        for passes in itertools.count():
            sizes, no_home = self._on_target(), self._no_home(listing)
            todo = [rel for rel, now in sorted(listing.files.items(), key=lambda kv: kv[0].lower())
                    if rel not in no_home and not self._settled(rel, now, sizes) and rel not in self.final and self.tries[rel] < TRIES]
            if not passes:
                result.earlier = sum(self._settled(rel, now, sizes) for rel, now in listing.files.items())
                self._say_plan(listing, todo)
                self._has_room(listing, todo, sizes)
                why = self._far_more(listing, todo, sizes) if self.ask_first else ""
                try:
                    if why:
                        self.ask_first(why)  # nothing has been copied yet: a no ends it here
                except BaseException:
                    self.record.waiting.clear()  # nothing was copied: not even this source's note goes in the record
                    if before is not None:  # and the last run's report holds again
                        with contextlib.suppress(OSError):
                            _write_here(self.target.path(f"{FOLDER}/report.txt"), before)
                    raise
            self._make_folders(listing)
            if not todo or passes == ROUNDS:
                break
            self._read_lists(listing)
            self.total_files = self.done_files + len(todo)
            self.total_bytes = self.done_bytes + sum(listing.files[rel][0] for rel in todo)
            self._copy_all(todo, listing, result)
            self._keep_record(force=True)
            self.say(f"Listing the source again, to be sure of it{'' if not passes else f' ({passes + 1})'}.")
            listing = self._list_source()
            result.listing = listing
        return self._finish(result, sizes, no_home)

    def _has_room(self, listing: Listing, todo: list[str], sizes: dict[str, int]) -> None:
        need, free = self._room(listing, todo, sizes)
        if free is not None and need > free:
            raise Exit(LOCAL, f"Not enough room on the target: {size_words(need)} to copy, {size_words(free)} free. Nothing more was copied.")

    def _room(self, listing: Listing, todo: list[str], sizes: dict[str, int]) -> tuple[int, int | None]:
        """What is needed and what is free, if that can be told. One with its name and size in place takes no more room; a part
        goes on from what it has."""
        need = sum(listing.files[rel][0] for rel in todo if sizes.get(self._key(rel)) != listing.files[rel][0])
        with contextlib.suppress(OSError), os.scandir(self.target.path(f"{FOLDER}/parts")) as parts:
            for part in parts:
                need -= part.stat().st_size
        here = Path(self.target.root)
        while not here.exists() and here != here.parent:
            here = here.parent
        try:
            return max(need, 0), self._keep_trying(self.target, shutil.disk_usage, here).free
        except FileProblem:
            return max(need, 0), None

    def _say_plan(self, listing: Listing, todo: list[str]) -> None:
        size = sum(listing.files[rel][0] for rel in todo)
        self.say(f"{many(len(listing.files), 'file')}, {size_words(sum(s for s, _ in listing.files.values()))}, on the source; "
                 f"{many(len(todo), 'file')}, {size_words(size)}, to copy or check.")

    def _finish(self, result: Result, sizes: dict[str, int], no_home: dict[str, str]) -> Result:
        listing = result.listing
        n, folders = len(listing.files), set(listing.folders)
        not_settled = [rel for rel, now in listing.files.items() if not self._settled(rel, now, sizes)]
        failed = {}
        for rel in not_settled:
            failed[rel] = no_home.get(rel) or self.failed.get(rel) or "it was added or changed during the copy, and kept changing"
        failed.update(listing.unreadable)
        for rel, why in self.failed.items():
            if rel.endswith("/"):
                if rel[:-1] in folders:
                    failed[rel] = why
            elif rel not in listing.files:  # failed, and then left: nothing says it's safe anywhere
                failed[rel] = f"{why}; and then it left the source's listing"
        result.failed = failed
        left = [rel for rel in self.ever_listed if rel not in listing.files and rel not in failed and rel not in self.record.entries]
        if left:
            listed = {self._same_to_a_disk(rel) for rel in listing.files}
            result.went = {rel for rel in left if self._same_to_a_disk(rel) not in listed}
        result.disagree = {rel: self.record.entries[rel]["x"] for rel in listing.files if self.record.entries.get(rel, {}).get("x")}
        if self.first_count and (not n or lost_most(self.first_count, n)):
            result.shrunk = (f"the source had {many(self.first_count, 'file')} when this began, and has {n:,} now. Is all of the disk there? "
                             "If they were removed on purpose, run this again.")
        elif not n and not self.new_disk and any(e.get("f") == self.source_id for e in self.record.entries.values()):
            result.shrunk = ("the source has no files now, though files were copied from it before. Is the disk there? If you removed them on purpose, "
                             "--new-disk says so.")
        result.done = not failed and not result.shrunk
        result.took = self.now() - self.began
        self._clean_parts(failed)
        if not self.target_short and not result.shrunk:
            self._forget_what_left_both_sides(listing, sizes)
        self._keep_record(force=True)
        _sync_names()
        self._keep_extras(result)
        return result

    def _forget_what_left_both_sides(self, listing: Listing, sizes: dict[str, int]) -> None:
        """Nothing is left to check, and a manifest shouldn't say otherwise. Each is asked about by itself first: a listing that
        came up short mustn't lose the checksum of a file whose original may be gone for good."""
        for rel in [rel for rel in self.record.entries if rel not in listing.files and self._key(rel) not in sizes]:
            self._touch()
            try:
                if self._look(self.target, self.target.path(rel)) is None:
                    self.record.forget(rel)
            except FileProblem:
                pass

    def _clean_parts(self, failed: dict[str, str]) -> None:
        """Parts of what isn't to be gone on with: this copy's own, in its own folder."""
        keep = {os.path.basename(self._part(rel)) for rel in failed}
        with contextlib.suppress(OSError), os.scandir(self.target.path(f"{FOLDER}/parts")) as parts:
            for part in parts:
                if part.name not in keep:
                    with contextlib.suppress(OSError):
                        os.unlink(part.path)

    def _keep_extras(self, result: Result) -> None:
        manifest = b"".join(_manifest_line(rel, e["h"]) for rel, e in sorted(self.record.entries.items()))
        report = report_text(self.source.given, self.target.given, result)
        result.report = os.path.join(self.target.given, FOLDER, "report.txt")
        for name, data in (("report.txt", report), ("manifest.sha256", manifest)):  # the report first: one being open mustn't cost the other
            p = self._keep_at_it(_write_here, self.target.path(f"{FOLDER}/{name}"), data)
            if p is not None:
                self.say(f"{name} couldn't be written: {p.why}. The record is on the target, and hylly copy check goes by that.")
                if name == "report.txt":
                    result.report = ""

    # ---- a plan, nothing written ----
    def plan(self) -> None:
        """What a run would do: the count, the size, the free space, an estimate."""
        with contextlib.suppress(OSError):
            self.record.load()
        target_there = os.path.lexists(self.target.root)
        listing = self._list_source()
        mark = ""
        with contextlib.suppress(OSError):
            mark = _read_mark(self.target.path(f"{FOLDER}/id"))
        if mark:
            self.target.mark = (f"{FOLDER}/id", mark)
        sizes = self._on_target() if target_there else {}
        no_home = self._no_home(listing)
        settled = {rel for rel, now in listing.files.items() if self._settled(rel, now, sizes)}
        todo = sorted(rel for rel in listing.files if rel not in no_home and rel not in settled)
        need, free = self._room(listing, todo, sizes)
        size = sum(listing.files[rel][0] for rel in todo)
        out = self.out
        out(f"From: {shown(self.source.given)}")
        out(f"To:   {shown(self.target.given)}" + ("" if target_there else "  (a new folder)"))
        out(f"On the source: {many(len(listing.files), 'file')}, {size_words(sum(s for s, _ in listing.files.values()))}, in {many(len(listing.folders), 'folder')}.")
        if settled:
            out(f"Copied and checked already: {many(len(settled), 'file')}, {size_words(sum(listing.files[rel][0] for rel in settled))}.")
        there = [rel for rel in todo if self._key(rel) in sizes]
        out(f"To copy: {many(len(todo) - len(there), 'file')}, {size_words(need)}." + (
            f" To compare, having a file of that name on the target already: {many(len(there), 'file')}. Nothing is overwritten." if there else ""))
        known = _known_targets().get(os.path.normcase(self.target.root))
        if known and mark != known and not self.new_disk:
            out("A copy was made here before, and the mark it left isn't here now. Is the disk mounted? A run will refuse this until it is; --new-disk if it's a new disk.")
        if self._another_source() and not self.new_disk:
            out("This doesn't look like the source that was copied here before. Is the disk mounted? A run will refuse this until it is; --new-disk if it's another disk now.")
        out(f"Free on the target: {'not known'  if free is None else size_words(free)}." + ("  NOT ENOUGH ROOM." if free is not None and need > free else ""))
        for rel, why in sorted(listing.unreadable.items()):
            out(f"Can't be read: {shown(rel)}: {why}")
        if no_home:
            first = sorted(no_home)[0]
            out(f"Can't be copied there: {many(len(no_home), 'file')}, such as {shown(first)}: {no_home[first]}.")
        if listing.left_out:
            kinds = Counter(what for _, what in listing.left_out)
            out("Left out: " + ", ".join(self._left_out_words(n, what) for what, n in kinds.most_common()) + (
                ". --everything takes the system's own files and folders along too." if not self.everything and self.HAS_EVERYTHING else "."))
        sample = self._sample(listing, todo)
        if sample and size:
            out(f"Reading the source here: {size_words(sample)}/s. With writing and reading back, count on at least {time_words(max(size / sample, 60))}.")
        out()
        out("This was only a dry run: nothing was copied. Without --dry-run, it copies.")
        self.planned = {  # the same, for --json
            "source": self.source.given, "target": self.target.given, "target_there": target_there,
            "source_files": len(listing.files), "source_bytes": sum(s for s, _ in listing.files.values()), "folders": len(listing.folders),
            "checked_already": {"files": len(settled), "bytes": sum(listing.files[rel][0] for rel in settled)},
            "to_copy": {"files": len(todo) - len(there), "bytes": need}, "to_compare": len(there),
            "free": free, "enough_room": free is None or need <= free,
            "mark_gone": bool(known and mark != known and not self.new_disk),
            "another_source": bool(self._another_source() and not self.new_disk),
            "unreadable": dict(sorted(listing.unreadable.items())), "cannot_be_copied": dict(sorted(no_home.items())),
            "left_out": len(listing.left_out), "at_least_seconds": round(size / sample) if sample and size else None}

    def _sample(self, listing: Listing, todo: list[str]) -> float:
        """How fast the source reads, from a piece of the biggest file: no more than that, as a plan stays light."""
        if not todo:
            return 0
        biggest = max(todo, key=lambda rel: listing.files[rel][0])
        began, got = time.perf_counter(), 0
        with contextlib.suppress(OSError):
            for chunk in past_the_cache(self.source.path(biggest)):
                got += len(chunk)
                if got >= SAMPLE:
                    break
        took = time.perf_counter() - began
        return got / took if got >= 1 << 20 and took > 0 else 0

    def tell_of_the_end(self, result: Result) -> str:
        """Says how it ended, and with a note if the run was long enough for a note to have started it, or it needs a look."""
        size = size_words(result.size)
        n = len(result.listing.files)
        left = f" Left out: {len(result.listing.left_out):,} (the report lists them)." if result.listing.left_out else ""
        missing = sum(rel in result.listing.files for rel in result.failed)
        others = len(result.failed) - missing
        if result.shrunk:
            words = f"Not done: {result.shrunk} Don't delete anything yet."
            heading = "not done"
        elif result.done and not n:
            words = "The source has no files, only folders, and those are on the target now."
            heading = "done"
        elif result.done and not result.disagree and not result.went:
            words = f"All {many(n, 'file')}, {size}, are copied and checked.{left}{self.THEN}"
            heading = "done"
        elif result.done:
            looks = []
            if result.disagree:
                looks.append(f"{many(len(result.disagree), 'file')} didn't match the source's own lists of checksums: the original may be damaged, or was changed "
                             "after the list was made")
            if result.went:
                looks.append(f"{many(len(result.went), 'file')} left the source before it could be copied: the report lists them")
            words = f"All {many(n, 'file')}, {size}, are copied and checked, but {'; and '.join(looks)}.{left}"
            heading = "done, with something to look at"
        else:
            more = f", and {many(others, 'folder or file')} couldn't be read or made" if others else ""
            words = (f"{n - missing:,} of {n:,} files are copied and checked, {missing:,} aren't{more}: the report says why. "
                     f"Don't delete the originals yet.{left}")
            heading = "finished, not all of it"
        self.out(words)
        if result.report:
            self.out(f"Report: {shown(result.report)}")
        if self.started_told or not result.done or result.disagree or result.went:
            self.notes.tell(f"{self.NAME}: {heading}", words)
        self.send_the_rest()
        return words


@dataclass
class _Moving:
    """How far one file has got, and how far it is known to be on the disk: a drop goes on from there."""
    digest: _Digest
    at: int = 0
    kept: tuple[int, _Digest] | None = None
    furthest: int = 0  # the most this try has had: going over it again is no progress
    comparing: bool = False  # still going through what an earlier run left


def _open_part(path: str, mode: int = 0o666):
    """A part to write in, or to go on with: a new one gets mode as it's made (your umask may take more away), and it
    keeps that, with no chmod after, as the copy's file. One made read-only (by an older Hylly, which gave a part its
    original's permissions just before its name) can't be opened to go on with: it's made anew, as it's this copy's
    own, in its own folder. (On Windows a part is never read-only, and a refusal is another program holding it,
    waited out as any.)"""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, mode)
    except PermissionError:
        if os.name == "nt" or not os.path.lexists(path):
            raise
        os.unlink(path)
        fd = os.open(path, flags, mode)
    return os.fdopen(fd, "r+b", buffering=0)


def result_data(r: Result) -> dict:
    """How a run ended, for --json: what report_text says, as data."""
    return {"done": r.done, "source_files": len(r.listing.files), "source_bytes": r.size, "copied": r.copied,
            "copied_bytes": r.copied_bytes, "there_already": r.there, "checked_before": r.earlier,
            "failed": dict(sorted(r.failed.items())), "disagree": dict(sorted(r.disagree.items())), "left_the_source": sorted(r.went),
            "left_out": len(r.listing.left_out), "shrunk": r.shrunk, "report": r.report, "seconds": round(r.took)}


def report_text(source: str, target: str, r: Result) -> str:
    listing = r.listing
    n = len(listing.files)
    lines = [f"HyllyCopy report, {time.strftime('%Y-%m-%d %H:%M')}", f"From: {shown(source)}", f"To:   {shown(target)}", ""]
    lines.append("DONE: every file on the source is copied and checked." if r.done else
                 f"NOT DONE: {r.shrunk} Don't delete the originals yet." if r.shrunk else "NOT DONE: some files aren't on the target. Don't delete the originals yet.")
    lines += [f"On the source when listed last: {many(n, 'file')}, {size_words(r.size)}, in {many(len(listing.folders), 'folder')}.",
              f"Copied and checked in this run: {many(r.copied, 'file')}, {size_words(r.copied_bytes)}.",
              f"There already, and the same: {r.there:,}.", f"Checked by an earlier run: {r.earlier:,}.", f"Took: {time_words(r.took)}."]
    if r.no_times:
        lines.append(f"The target wouldn't keep the time of {many(r.no_times, 'file')}.")

    def part(heading: str, rows: list[str]) -> None:
        if rows:
            lines.extend(["", f"{heading} ({len(rows):,}):", *rows])

    part("Not copied", [f"  {shown(rel)}: {why}" for rel, why in sorted(r.failed.items())])
    part("Copied as they are, but not matching the source's own list of checksums: the original may be damaged, or changed after the list was made",
         [f"  {shown(rel)}: {shown(lists)}" for rel, lists in sorted(r.disagree.items())])
    part("Left the source before they could be copied", [f"  {shown(rel)}" for rel in sorted(r.went)])
    part("Left out", [f"  {shown(rel)}: {what}" for rel, what in sorted(listing.left_out)])
    lines += ["", f"To check it all again any time: hylly copy check \"{shown(target)}\"",
              f"Or with any sha256sum, in that folder: sha256sum -c {FOLDER}/manifest.sha256", ""]
    return "\n".join(lines)


class Verifying(_Job):
    """hylly copy check TARGET: every copy read again, and compared with the checksum in the record."""

    def __init__(self, target, **kw) -> None:
        super().__init__(**kw)
        self.target = Side(target, "the target")
        self.record = Record(self.target.path(f"{FOLDER}/record.jsonl"))
        self.total_files = self.done_files = self.total_bytes = self.done_bytes = 0
        self.problems: dict[str, str] = {}

    def check(self) -> None:
        try:
            self.record.load()
        except OSError as e:
            raise Exit(LOCAL, f"Can't read the target, {shown(self.target.given)}: {_why(e)}.") from None
        if not self.record.entries:
            raise Exit(NOT_SET_UP, f"No HyllyCopy record in {shown(self.target.given)}: this checks what hylly copy copied there.")
        with contextlib.suppress(OSError):
            mark = _read_mark(self.target.path(f"{FOLDER}/id"))
            if mark:
                self.target.mark = (f"{FOLDER}/id", mark)

    def plan(self) -> None:
        """What a check would read, for its dry run."""
        entries = self.record.entries.values()
        self.planned = {"target": self.target.given, "files": len(entries), "bytes": sum(e.get("s", 0) for e in entries)}
        self.out(f"It would read {many(len(entries), 'file')}, {size_words(sum(e.get('s', 0) for e in entries))}, on "
                 f"{shown(self.target.given)} again, and write what it finds in {FOLDER}/checked.txt there. Nothing was read again.")

    def _status(self) -> str:
        if not self.total_files:
            return ""
        return f"{100 * self.done_bytes / max(self.total_bytes, 1):5.1f}%  checked {self.done_files:,} of {many(self.total_files, 'file')}"

    def _started_note(self) -> str:
        return f"Checking {many(self.total_files, 'file')}, {size_words(self.total_bytes)}."

    def _check_one(self, rel: str, entry: dict, path: str | None = None) -> None:
        """One copy read again: by its place in the record, or at path, on the same disk. entry: its checksum ("h"), and
        its size ("s") when known."""
        path = path or self.target.path(rel)
        self.strikes = 0
        st = self._look(self.target, path)  # missing only while the disk answers, held for at once till it does
        if st is None:
            self.problems[rel] = "it's missing"
        elif not stat.S_ISREG(st.st_mode):
            self.problems[rel] = "it isn't a file any more"
        elif entry.get("s") is not None and st.st_size != entry["s"]:
            self.problems[rel] = f"its size has changed ({st.st_size:,} bytes, {entry.get('s'):,} when copied)"
        elif self._read_whole(self.target, path)[0].hex() != entry["h"]:
            self.problems[rel] = "its contents have changed"

    def run(self, more: dict[str, tuple[str, dict]] | None = None) -> int:
        """Every copy in the record read again. more: other copies on the same disk, each by the name it's told by, with
        its path and its entry (_check_one): a backup's earlier copies in changed/ (backup.earlier_copies_to_check),
        read the same way, a disk going away waited out, their problems this check's and in checked.txt too."""
        entries = [(rel, entry, None) for rel, entry in sorted(self.record.entries.items())]
        entries += [(name, entry, path) for name, (path, entry) in sorted((more or {}).items())]
        self.total_files, self.total_bytes = len(entries), sum(e.get("s") or 0 for _, e, _ in entries)
        with self._watched():
            self.out(f"Checking {many(self.total_files, 'file')}, {size_words(self.total_bytes)}, on {shown(self.target.given)}.")
            for rel, entry, path in entries:
                try:
                    self._check_one(rel, entry, path)
                except FileProblem as p:
                    self.problems[rel] = p.why
                if rel in self.problems:
                    self.out(f"  {shown(rel)}: {self.problems[rel]}")
                self.done_files += 1
                self.done_bytes += entry.get("s") or 0
                self._moved_on()
        text = [f"HyllyCopy check, {time.strftime('%Y-%m-%d %H:%M')}", f"Of: {shown(self.target.given)}",
                f"Checked: {many(self.total_files, 'file')}, {size_words(self.total_bytes)}.", f"Problems: {len(self.problems):,}.", ""]
        text += [f"  {shown(rel)}: {why}" for rel, why in sorted(self.problems.items())]
        with contextlib.suppress(OSError):  # a disk that only reads now can be checked too
            _write_here(self.target.path(f"{FOLDER}/checked.txt"), "\n".join(text) + "\n")
        words = (f"All {many(self.total_files, 'file')}, {size_words(self.total_bytes)}, read back the same as when they were copied." if not self.problems else
                 f"{many(len(self.problems), 'file')} of {self.total_files:,} aren't as they were when copied: they are listed in {FOLDER}/checked.txt.")
        self.checked = {"target": self.target.given, "files": self.total_files, "bytes": self.total_bytes,
                        "problems": dict(sorted(self.problems.items())), "words": words}  # for --json
        self.out(words)
        if self.started_told or self.problems:
            self.notes.tell(f"{self.NAME}: {'checked' if not self.problems else 'the check found problems'}", words)
        self.send_the_rest()
        return len(self.problems)



