"""Hylly's own files, written so that a write cut short costs as little as possible.

Power goes, a process is ended, a network share drops out: the library lives on a NAS, and all of these happen.

- Record files (one JSON line per record, appended) survive being cut anywhere: each line is decoded on its own, so a
  cut-off letter costs only its line, and an append first ends a line left unfinished, so it can't swallow the next.
  The caches kept so, the later line for a key winning, are one class (Records; TidiedRecords for the catalog's).
- Whole files are swapped in complete, under a temporary name only this write uses (atomic_write).
- Every log is written one way (append_log), moved aside beyond a limit, and what came from elsewhere goes into a line
  of it only as shown makes it.
- Writes to a network share fail now and then and work a moment later (retrying).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

RUN_LOG_LIMIT = 5 << 20  # a run's log (download, lookup, Inbox) moves aside beyond this
MOVE_AGAIN = 3600  # seconds before a log whose move aside failed is tried again: each try may copy the whole log back
LEFT_AGE = 60  # seconds after which a log's NAME.<hex> beside it is what a move cut short left, not one under way
# What could start a line of its own, move a terminal's cursor or ring it: the control characters (C0, DEL, C1, of which
# U+0085 ends a line for Python's splitlines) and the line and paragraph separators
CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
JOINERS = "\u200c\u200d"  # the only format characters kept: Persian, the Indic scripts and emoji need them
LEFT = re.compile(r"\.[0-9a-f]{8}")  # what follows a log's name in the name of its NAME.<hex> (_take_aside)
_not_moved: dict[str, float] = {}  # a log whose move aside failed -> not tried again before this (time.monotonic())


def shown(text) -> str:
    """Text that came from elsewhere (a file's name, what a stranger typed or sent), fit for one line of a log, a
    terminal or a note: whatever could start a line of its own or move the cursor is ?, and every invisible format
    character (Unicode's Cf: the marks that turn text round, the zero widths, the tags, the soft hyphen) is dropped, so
    it can't read as the line's own words, reorder them or hide words in them; but the two joiners, which Persian,
    the Indic scripts and emoji need. Ordinary letters, other alphabets and spaces of every kind stay. A half of a
    letter that can't be written (a Linux file name that isn't UTF-8) is ? too. The one cleaner for every log line and
    the names a copy or a build shows."""
    text = CONTROLS.sub("?", str(text))
    if not text.isascii():  # no format character is ASCII: most lines need no look at each letter
        text = "".join(c for c in text if c in JOINERS or unicodedata.category(c) != "Cf")
    return text.encode("utf-8", "replace").decode("utf-8")


def aside(path: Path) -> Path:
    """Where a log goes beyond its limit (append_log), the one before that going: access.log.1, as log tools name it,
    for a .log; NAME.1.txt for a run's log, which keeps what opens it."""
    return path.with_suffix(".log.1") if path.suffix == ".log" else path.with_name(f"{path.stem}.1{path.suffix}")


def move_aside(path: Path, limit: int) -> bool:
    """A log found over limit moved aside (aside), and the one before that goes; True if it was. Trouble with it is no
    reason not to write: a log or an older file held open by another program (on Windows), or a share gone, and the
    move is tried again only after MOVE_AGAIN, as AccessLog does its old lines, since each try could copy the whole log
    back; the log grows past its limit meanwhile. A log another process has just moved aside is no failure: nothing to
    wait for."""
    if time.monotonic() < _not_moved.get(str(path), 0.0):
        return False
    try:
        if path.stat().st_size <= limit:
            return False
    except OSError:  # none yet, or a share gone: nothing to move
        return False
    try:
        return _take_aside(path, limit)
    except OSError:
        _not_moved[str(path)] = time.monotonic() + MOVE_AGAIN
        return False


def _take_aside(path: Path, limit: int) -> bool:
    """move_aside's move, once the log was found over limit. Two processes may find it so at once (the server and a
    second start, or two shops' runs): each first moves whatever is there now to a name of its own, NAME.<hex>, so only
    one gets the big one, and a fresh log the other has just begun, moved so by mistake, goes back, every line of it.
    The big one moved aside, what an earlier move cut short left beside the log goes into it too (_take_back). Raises
    OSError when the older file couldn't be replaced, its lines back in the log."""
    mine = path.with_name(f"{path.name}.{secrets.token_hex(4)}")
    try:
        path.replace(mine)
    except FileNotFoundError:  # another process moved it first: done, and the next line begins a fresh one
        return False
    try:
        if mine.stat().st_size > limit:
            mine.replace(aside(path))
            _take_back(path)
            return True
        failed = None
    except OSError as e:  # the older one held open elsewhere: the lines go back
        failed = e
    with contextlib.suppress(OSError):  # cut short (a share gone, a full disk): NAME.<hex> stays, for the next move
        with mine.open("rb") as moved, path.open("ab") as log:
            shutil.copyfileobj(moved, log)
        mine.unlink()
    if failed:
        raise failed
    return False


def _take_back(path: Path) -> None:
    """What a move cut short left beside the log, NAME.<hex>, LEFT_AGE old (not another process's move under way), taken
    back in: its lines at the end of the file just moved aside, then it goes. Safe, as no line is lost: a copy back cut
    short may so give some lines twice. Removing it outright would be safe only for a whole copy, which only comparing
    could tell. Each is claimed first, renamed NAME.<hex>.taking, which no other take-back looks at: of two at once,
    only one gets it, so it can't come twice or torn. A copy that fails gives it its name back, for the next move;
    one cut short by the process ending leaves the .taking file, which nothing takes back."""
    with contextlib.suppress(OSError):
        for left in sorted(path.parent.iterdir()):
            if not (left.name.startswith(path.name) and LEFT.fullmatch(left.name[len(path.name):])):
                continue
            st = left.stat()
            if time.time() - max(st.st_mtime, st.st_ctime) < LEFT_AGE:
                continue
            claimed = left.with_name(f"{left.name}.taking")
            try:
                left.rename(claimed)
            except OSError:  # another take-back has it, or it's held open (Windows)
                continue
            try:
                with claimed.open("rb") as moved, aside(path).open("ab") as older:
                    shutil.copyfileobj(moved, older)
                claimed.unlink()
            except OSError:
                with contextlib.suppress(OSError):
                    claimed.rename(left)


def _torn(path: Path) -> bool:
    """Whether a log's last line was left unfinished (a write cut short), so that the next starts a line of its own.
    Only a file's: a pipe has no end to look at, and opening one to read would wait for a writer."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False
        with path.open("rb") as fh:
            if not fh.seek(0, os.SEEK_END):
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def append_log(path: Path, lines: str | list[str], limit: int | None = None, attempts: int = 1, wait: float = 0.5) -> bool:
    """A line at the end of a log, or several in one write: access.log, bell.log, server.log, and a run's download,
    lookup or Inbox log, all written so. Each line is cleaned (shown), so nothing in it can start another, whoever
    wrote it. Beyond limit the log first moves aside (move_aside): True when it did, so whoever keeps the older file
    short can see to it. A line left torn is ended first (_torn), looked at before the log is opened to append, so the
    log is open only for the write itself: a line written while another process moves the log aside is lost only as
    with any append to a file renamed meanwhile, as before. A log that is a pipe gets its lines too. A request or a run
    doesn't wait on it: one try, unless attempts says more (a share that drops out now and then). Raises OSError when it
    can't be written; whether that matters, and who is told, is the caller's."""
    data = "".join(shown(line) + "\n" for line in ([lines] if isinstance(lines, str) else lines)).encode("utf-8")
    moved = limit is not None and move_aside(path, limit)

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        torn = _torn(path)
        with path.open("ab") as fh:
            fh.write(b"\n" + data if torn else data)

    retrying(write, attempts, wait)
    return moved


def retrying(fn: Callable, attempts: int = 6, wait: float = 0.5, *, longest: float = 8,
             told: Callable[[OSError], None] | None = None, hopeless: Callable[[OSError], bool] | None = None):
    """Writes to a network share fail now and then with 'Invalid argument' and work a moment later. So fn is tried up to
    attempts times, the wait between doubling from wait to at most longest, and the last try's error is raised. told:
    given each error that is tried again. hopeless: says which errors no wait will mend (a full disk, for a copy), raised
    at once. A path below a file (NotADirectoryError: --library given below one, say) is always raised at once: no wait
    mends that, and a share that's away answers with another error, which still gets its tries. Hylly's own small writes
    take the defaults; a book's copy waits longer, and says so (copies.with_retries)."""
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as e:
            if attempt == attempts - 1 or isinstance(e, NotADirectoryError) or (hopeless is not None and hopeless(e)):
                raise
            if told is not None:
                told(e)
            time.sleep(min(wait * 2**attempt, longest))


def share_is_gone(e: OSError) -> bool:
    """Whether a whole network share went away. Windows has its own numbers for that (the path, the name), and Python
    calls them "file not found" too: it doesn't mean there is no such file. Opening a file gives no such number, only
    asking about it (os.stat, os.scandir)."""
    return getattr(e, "winerror", None) in (53, 67)


def read_lines(path: Path) -> list[str]:
    """A record file's lines; none if it isn't there. A line that isn't valid UTF-8 (cut off inside a letter) is left out.
    A share that is gone raises at once: what comes next mustn't decide on having no records, and it isn't worth a wait.
    So does a path below a file (NotADirectoryError, as retrying raises it), which is no passing trouble either."""
    def read() -> bytes | OSError:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            try:
                os.stat(path)
            except OSError as e:
                return e if share_is_gone(e) else b""
            raise  # it's there after all: read it again

    data = retrying(read)
    if isinstance(data, OSError):
        raise data
    lines = []
    for raw in data.split(b"\n"):
        try:
            line = raw.decode("utf-8").rstrip("\r")
        except UnicodeDecodeError:
            continue
        if line:
            lines.append(line)
    return lines


def read_json(path: Path) -> dict:
    """A small JSON file that holds a table (a note, a record of what was done, a cache): {} when it isn't there, can't
    be read, is damaged (cut short, not JSON, not UTF-8) or holds something else (a list, a number). The one reading for
    them all, so each doesn't decide anew what a damaged file means. Nothing is raised, a share that has gone neither:
    every reader of such a file takes none for an answer. The library's owner marker, which must tell a share gone from
    no marker, has its own (db.owner)."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def read_records(path: Path) -> Iterator[dict]:
    """A record file's records, in order. A damaged line costs only itself."""
    for line in read_lines(path):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def append_line(path: Path, line: str, durable: bool = False, attempts: int = 6, wait: float = 0.5) -> None:
    """One line at the end of a record file. durable: on the disk before this returns, for what must not be lost."""
    data = (line + "\n").encode("utf-8")

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as fh:
            if fh.seek(0, os.SEEK_END):
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":  # the last write was cut short: that line stays broken, this one doesn't
                    fh.write(b"\n")
            fh.write(data)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())

    retrying(write, attempts, wait)


def is_link(path: Path | str) -> bool:
    """A symbolic link or a Windows junction: it leads somewhere else, perhaps back to where it is."""
    return is_link_stat(os.stat(path, follow_symlinks=False))


def is_link_stat(st: os.stat_result) -> bool:
    """is_link, of what os.stat or a folder's listing said without following it: the one definition of a link that
    HyllyCopy, the backup's zip and the rest go by, so that a junction isn't a link to one and a folder to another."""
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT


def sync(path: Path) -> None:
    """A file just written, on the disk: to be called before anything that says the file is safe."""
    with path.open("rb+") as fh:
        os.fsync(fh.fileno())


def atomic_write(path: Path, data: str | bytes, *, mode: int | None = None, durable: bool = True, encoding: str = "utf-8",
                 attempts: int = 6) -> None:
    """The whole file or none of it. Its permissions on Linux and macOS are given as it's made, never changed after: a
    chmod on a disk that keeps them as extended attributes (Linux's ntfs3) writes one more, and one that refuses chmod
    would stop the write. mode: for what only you may read; without it, a file keeps those it has, and a new one gets
    what your umask says, which may take from either, never add. A disk that doesn't do permissions (a network share,
    often) is no reason to fail, unless mode was asked for: a file it shows the group or others may read or write,
    beyond what mode gives them, then isn't put in place (Unkept, a PermissionError), so a secret isn't left readable.
    That is the disk's way, not a moment's trouble, so it isn't tried again. One that shows every file as its owner's
    (0700, as some shares do) is no such disk. Windows goes by the folder's."""
    raw = data.encode(encoding) if isinstance(data, str) else data

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Our own name, not tempfile's: on Windows it takes a refusal for a name in use, and tries the next, for ever.
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        made = 0o600 if mode is not None else 0o666
        if os.name != "nt":
            if mode is not None:
                made = mode
            else:
                with contextlib.suppress(OSError):
                    made = stat.S_IMODE(path.stat().st_mode) & 0o777
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), made)
        try:
            with os.fdopen(fd, "wb") as fh:
                if mode is not None and os.name != "nt" and stat.S_IMODE(os.fstat(fh.fileno()).st_mode) & 0o077 & ~mode:
                    raise Unkept(errno.EPERM, "This disk doesn't keep a file's permissions, and shows it to others on "
                                              "this computer", str(path))
                fh.write(raw)
                fh.flush()
                if durable:
                    os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    retrying(write, attempts, hopeless=lambda e: isinstance(e, Unkept))


class Unkept(PermissionError):
    """A file atomic_write was to make its owner's only, on a disk that shows it to others all the same."""


class Records:
    """A cache in a record file: one JSON line per record, appended at once, so a run stopped part way keeps what it had,
    and a later line for a key wins. Read as read_records reads it, a damaged line costing only itself. The lines are the
    caller's own, as they were before this class, so a file written then reads as it is.

    key: the field that tells the records apart, or a tuple of fields, the key then a tuple of their values; a record
    without it is passed over. quiet: a write that fails is let go, the record simply made again next time (the
    catalog's caches); otherwise its error is raised once append_line's tries are spent, which ends the run (a lookup
    source's answers, itch.io's). keep: False writes nothing, for a dry run. each: given every record taken, read or put,
    in order: an index of one's own (lookups.Cache's by catalog id)."""

    def __init__(self, path: Path, key: str | tuple[str, ...], *, quiet: bool = False, keep: bool = True,
                 each: Callable[[dict], None] | None = None) -> None:
        self.path, self.key, self.quiet, self.keep, self.each = path, key, quiet, keep, each
        self.rows: dict = {}
        self.lines = 0  # records in the file: one put again adds a line, and tidy goes by how many are older ones
        self._lock = threading.Lock()
        for row in read_records(path):
            self.lines += 1
            try:
                self._take(row)
            except (KeyError, TypeError):  # without its key, or with one that can't be a key (a list)
                continue

    def key_of(self, row: dict):
        return tuple(row[k] for k in self.key) if isinstance(self.key, tuple) else row[self.key]

    def _take(self, row: dict) -> None:
        self.rows[self.key_of(row)] = row
        if self.each is not None:
            self.each(row)

    def get(self, key) -> dict | None:
        return self.rows.get(key)

    def put(self, row: dict) -> None:
        """A record, at the end of the file and then in memory."""
        self.key_of(row)  # one without its key is a slip of the caller's: it raises before anything is written
        with self._lock:  # the catalog's are put from several threads
            if self.keep:
                try:
                    append_line(self.path, json.dumps(row, ensure_ascii=False))
                except OSError:
                    if not self.quiet:
                        raise
            self._take(row)
            self.lines += 1


class TidiedRecords(Records):
    """Records whose file is written anew now and then with only the last of each (tidy): only the catalog build's
    caches. No other run writes them meanwhile, and nothing reads their older lines. Neither holds for a lookup source's
    answers (another run asks too, and an id goes by the last line that names it, an older one too) nor for itch.io's
    (a download marks an item to be asked again while its answers are held): what another run appends between the read
    and the tidy would be lost, so they are plain Records."""

    def tidy(self) -> None:
        """Once half its lines or more are older ones of records put again (the first catalog after an update adds one to
        most files), the file is written anew with only the last of each. A tidy that fails is let go, whatever quiet
        says: the file stays as it was, and is still read the same."""
        with self._lock:
            if not self.keep or not self.rows or self.lines < 2 * len(self.rows):
                return
            text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows.values())
            try:
                atomic_write(self.path, text)
                self.lines = len(self.rows)
            except OSError:
                pass
