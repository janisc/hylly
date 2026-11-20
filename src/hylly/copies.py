"""Copies into the library from elsewhere (hylly add), each one checked against its source.

A copy is written under a temporary name, compared with the source's MD5 and only then given its
real name, and is recorded in the database (manifest.add_copy) once it is on the disk. "hylly verify --hash" reads every copy again
and compares it with the MD5 of its source: run that before removing any originals. Sources are only read.

This is a copy engine of its own, beside HyllyCopy's (hyllycopy.py), for what hylly add is: a few books at a time into
the library, picked by a list or by a folder's books, their names changed by strip_suffix, each recorded with the MD5
that verify --hash and the Inbox go by, and the run summed up (NAME TAKEN, GONE, FAILED). HyllyCopy copies whole
folders between disks, with a record and SHA-256 checksums of its own on the target and its parts in a folder of their
own there. What the two have in common is copying one file: written apart, its checksum made as it is read (here
hashed_copy: each book is read once), read back, and named only once whole. That is to become one routine both use.
Until then a copy here is made beside its place (NAME.copying, which the catalog and a backup pass over) and tried
again for a couple of minutes (with_retries), where HyllyCopy waits out a share for as long as it takes.

hylly add FOLDER copies books into one of your own folders (folders.py) from the folder its copy_from names.
With a list (file,category rows), each listed book goes to FOLDER\\<category>\\<name>; without one, every book
there is copied with the subfolders it is in. Names stay as they are, minus the folder's strip_suffix when
your files carry one. A book already copied is skipped, even if its original is gone by then; a listed one whose copy
is gone as well is passed over and said, as a book you removed, until its line leaves the list, unless what's gone
looks more like a folder moved or a disk away: then the run stops, as it did for any such line before.

Books are told apart by their source as well as their place, as two can get one name (a list keeps only the file
name, and strip_suffix can make two names one): two that would get one stop the copy before anything is copied, and a book
whose place another book's copy has stays out, said as NAME TAKEN on every run, until it gets another name. So the
originals can go once a run has nothing left to copy and no NAME TAKEN, and "hylly verify --hash" is clean: verify
itself checks only what is recorded, as copy_from is emptied or unplugged once the copies are checked.

An Inbox ([inbox] in the settings, folders.Inbox) is emptied instead: the server's job passes over it once a minute
(cli._inbox_pass), and this is its engine. A look (look) takes a book once it has stayed the same for STEADY seconds,
as one still coming over the network grows; knows one the library has already by the size and MD5 its copy was
recorded with, whatever its name; places each in the folder of yours its kind belongs in, as its folders in the Inbox
say (folders.placed); and sets aside what can't be taken: not a book Hylly reads, damaged or cut short (damaged), or a
name another book in the library has. The books taken are copied as hylly add copies them (take_in),
what couldn't be is moved into the Inbox's Not added folder with a note beside it (set_aside), and once the catalog has
them the originals go (remove), with the folders that leaves empty, never the Inbox itself. Nothing in the library is
ever removed or written over.
"""

from __future__ import annotations

import contextlib
import csv
import errno
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import struct
import sys
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .exits import LOCAL, NOT_SET_UP, PARTLY, USAGE, Exit
from . import db, folders, paths
from .download import CHUNK, HEAD, NO_ROOM, NO_ROOM_WINDOWS, md5_of, verify_files, wrong_kind
from .manifest import add_copy, copies as recorded
from .removed import by_you as removed_by_you
from .shoplist import SEP_LINE
from .storage import atomic_write, is_link, retrying, share_is_gone, sync
from .words import many, size_words


@dataclass
class Op:
    src: Path
    dest: str  # relative to the library root, forward slashes
    line: str = ""  # a list's file column, as written there: what names the book's line in it


GIVE_UP_AFTER = 3  # books in a row that trying again didn't help, a couple of minutes each


class GaveUp(OSError):
    """Tried again and again: the trouble is the disk's or the share's rather than one book's."""


class NotTheSame(OSError):
    """A different file has the book's name in the library, which trying again won't change."""


def with_retries(fn, what: str, attempts: int = 6):
    """Network shares fail now and then and work a moment later: for a book's copy, tried again after a longer wait
    than Hylly's own small writes have (storage.retrying), each try said with what it was for, and the last error a
    GaveUp. A full disk (no_room) is no passing trouble: its error is raised at once, as it is."""
    def told(e: OSError) -> None:
        print(f"    retrying ({e}) {what}", file=sys.stderr, flush=True)

    try:
        return retrying(fn, attempts, 5, longest=60, told=told, hopeless=no_room)
    except OSError as e:
        if no_room(e):
            raise
        raise GaveUp(str(e)) from e


def hashed_copy(src: Path, tmp: Path) -> tuple[str, int]:
    """src copied to tmp, with the MD5 and the size of what was read, worked out on the way: the source is read once,
    not once to hash it and again to copy it, and a record of both describes the copy, even of a book that grew since
    it was looked at."""
    h, size = hashlib.md5(), 0
    with src.open("rb") as fh, tmp.open("wb") as out:
        for block in iter(lambda: fh.read(CHUNK), b""):
            out.write(block)
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def copy_in(src: Path, dest: Path, md5: str | None, what: str = "",
            before: Callable[[], object] | None = None) -> tuple[str | None, int | None]:
    """A copy of src at dest, which isn't there yet or is given way to (before): written under a temporary name, the
    source hashed as it is read (hashed_copy), on the disk, read back and compared, and only then given its name, all
    of it tried again while a share drops out (with_retries, raising GaveUp in the end; a full disk at once). md5: what
    the source must be (seed's: the shop's), or None to take it as it is read. Returns the copy's MD5 and size, as they
    were read (hashed_copy); the size is None when it wasn't read, its copy found in place at once (seed's). A copy that
    failed, half written or not matching, isn't left in the library, and a try that finds the copy in its place already
    is done: a swap that went through though the share said it failed. what: the copy's name in the tries said. before:
    called once the copy checks, before it takes the name (seed's, moving an older version aside)."""
    tmp = dest.with_name(dest.name + ".copying")
    known = {"md5": md5, "size": None}  # the MD5 given, or both as the first try that read it whole found them

    def there() -> bool:
        try:
            return (known["md5"] is not None and dest.stat().st_size == src.stat().st_size
                    and md5_of(dest).hexdigest() == known["md5"])
        except OSError:
            return False

    def copy() -> None:
        if there():  # a file in the place has its MD5 read only when it has the book's size
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            read, size = hashed_copy(src, tmp)
            if known["md5"] is not None and read != known["md5"]:  # it changed since it was looked at (seed's)
                raise OSError("the copy does not match the source")
            known["md5"], known["size"] = read, size
            sync(tmp)  # on the disk before the record says it's safe
            if md5_of(tmp).hexdigest() != read:  # what reached the disk, read back
                raise OSError("the copy does not match the source")
            if before is not None:
                before()
            os.replace(tmp, dest)
        except BaseException:
            with contextlib.suppress(OSError):  # a copy that failed, half written or not checking, isn't left in the library
                tmp.unlink()
            raise

    with_retries(copy, what or str(dest))
    return known["md5"], known["size"]


def copy_one(root: Path, op: Op) -> str:
    """'copied', or 'already there' when an identical file has the name; raises OSError otherwise. The source is read
    once for a copy, hashed as it is copied (copy_in), and its record has the MD5 and the size of what was read: a book
    that grew since it was looked at is recorded as its copy is. Its MD5 alone is read only when its name is taken, to
    compare."""
    dest = root / op.dest
    size = op.src.stat().st_size
    if dest.is_file():
        if dest.stat().st_size == size:
            src_md5 = md5_of(op.src).hexdigest()
            if with_retries(lambda: md5_of(dest).hexdigest(), op.dest) == src_md5:
                add_copy(root, {"src": str(op.src), "dest": op.dest, "size": size, "md5": src_md5,
                                "copied_at": db.now(), "already_there": True})
                return "already there"
        raise NotTheSame(f"a different file already has this name: {op.dest}")
    src_md5, size = copy_in(op.src, dest, None, op.dest)
    add_copy(root, {"src": str(op.src), "dest": op.dest, "size": size, "md5": src_md5,
                    "copied_at": db.now()})
    return "copied"


def verify(root: Path, check_hash: bool = False, progress=None) -> tuple[int, list[tuple[str, str]]]:
    """(copies checked, [(path, problem)]): each recorded copy present with its size, and its MD5 with check_hash, as
    the downloads are checked (download.verify_files)."""
    rows = [(e["dest"], e["size"], e["md5"]) for e in recorded(root).values()]
    return len(rows), verify_files(root, rows, check_hash, progress)


LIST_COLUMNS = ("file", "category")  # hylly add's list: the book, and the subfolder of the folder it goes into
LIST_WANTS = "a line for each book with its file and its category, under a first line that names them: file,category"


def list_ops(list_file: Path, source: Path, into: str, strip: str = "") -> list[Op]:
    """The listed books as copies into <into>\\<category>\\, each category as written: an empty one is the top of the
    folder, as it always was. A list a spreadsheet saved reads too: the byte order mark first (Excel's "CSV UTF-8"),
    the columns' names in capitals, other columns beside them, empty rows, a semicolon or a tab between the columns,
    as a spreadsheet may save it where the comma is the decimal mark, and Excel's own sep= line first, as the shop lists
    take it (shoplist.SEP_LINE). A list that isn't UTF-8 text, doesn't name the two columns once each on its first line,
    or has a book's line without a file or without a cell for its category stops the run, with the list and the line
    named (USAGE): nothing is copied, and no book goes to a folder named None."""
    try:
        with list_file.open(encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        raise Exit(USAGE, f"{list_file} isn't UTF-8 text: save it from the spreadsheet as CSV UTF-8, and run this again. "
                          "Nothing was copied.") from None
    lines = text.splitlines(keepends=True)
    told = SEP_LINE.fullmatch(lines[0].rstrip("\r\n")) if lines else None
    skip = 1 if told else 0  # Excel's line, naming its separator: no line of the list
    first = lines[skip].rstrip("\r\n") if len(lines) > skip else ""

    def heads(sep: str) -> list[str]:
        return [c.strip().lower() for c in next(csv.reader([first], delimiter=sep), [])]

    seps = [told.group(1)] if told and told.group(1) in ",;\t" else [",", ";", "\t"]
    sep = next((s for s in seps if set(LIST_COLUMNS) <= set(heads(s))), None)
    if sep is None:
        raise Exit(USAGE, f"{list_file}, line {skip + 1}: it should name the two columns, file and category; it has "
                          f"{first[:80]!r}. The list has {LIST_WANTS}, as examples/add-list.csv does. Nothing was copied.")
    twice = [name for name in LIST_COLUMNS if heads(sep).count(name) > 1]
    if twice:
        raise Exit(USAGE, f"{list_file}, line {skip + 1}: it names the {twice[0]} column twice ({first[:80]!r}), so which "
                          f"is meant can't be told: keep one. Nothing was copied.")
    reader = csv.DictReader(io.StringIO("".join(lines[skip:]), newline=""), delimiter=sep)
    reader.fieldnames = [(name or "").strip().lower() for name in reader.fieldnames or []]  # its header read, as heads()
    ops = []
    for row in reader:
        if not any(value.strip() for name, value in row.items() if isinstance(name, str) and isinstance(value, str)):
            continue  # an empty row, as a spreadsheet may save one with its commas
        file, category = row.get("file"), row.get("category")  # None: the line has no cell for it
        missing = [what for what, given in (("file", (file or "").strip()), ("category", category is not None)) if not given]
        if missing:
            raise Exit(USAGE, f"{list_file}, line {reader.line_num + skip}: no {' and no '.join(missing)}. The list has "
                              f"{LIST_WANTS}. Nothing was copied.")
        src = source / file
        name = (src.stem.removesuffix(strip) if strip else src.stem) + src.suffix
        ops.append(Op(src, f"{into}/{category}/{name}", file))
    return ops


def tree_ops(source: Path, into: str, strip: str = "", passed: list[str] | None = None) -> list[Op]:
    """Every book under source (a format with book = true in formats.toml), as copies into <into> with the
    subfolders it is in."""
    from . import rules

    books = rules.current().book_suffixes
    ops = []
    def only_this_is_gone(e: OSError) -> bool:  # any other trouble mustn't pass for "no such book": the books would just be left out
        return isinstance(e, FileNotFoundError) and not share_is_gone(e)

    def leads_elsewhere(path: Path) -> bool:
        try:
            return is_link(path)
        except OSError as e:
            if only_this_is_gone(e):
                return True
            raise

    def unreadable(e: OSError) -> None:
        if isinstance(e, PermissionError):  # such as a system folder: it won't change, and it's no reason to stop
            print(f"    passed over, not allowed to read it: {e.filename}", file=sys.stderr, flush=True)  # as it's met
            if passed is not None:  # and named again with the result (add_books)
                passed.append(str(e.filename))
        elif not only_this_is_gone(e):
            raise e

    for folder, subfolders, names in os.walk(source, onerror=unreadable):
        subfolders[:] = [d for d in subfolders if not leads_elsewhere(Path(folder) / d)]  # one may lead back here, or anywhere
        for book in names:
            src = Path(folder) / book
            if folders.is_book(book, books) and not leads_elsewhere(src):
                rel = src.relative_to(source)
                name = (src.stem.removesuffix(strip) if strip else src.stem) + src.suffix
                sub = "/".join(rel.parts[:-1])
                ops.append(Op(src, f"{into}/{sub}/{name}" if sub else f"{into}/{name}"))
    return sorted(ops, key=lambda op: str(op.src))


def stays_inside(dest: str, into: str) -> bool:
    """Whether a copy's place is inside its folder: a list's category can say anything. On Windows a colon means a
    drive or a hidden part of a file; elsewhere it's a letter like any other, and books have it in their titles."""
    parts = dest.replace("\\", "/").split("/")
    return parts[0] == into and not any(part in (".", "..") or (os.name == "nt" and ":" in part) for part in parts)


def _reachable(root: Path) -> bool:
    try:
        os.stat(root)
        return True
    except OSError:
        return False


def _takes_writes(root: Path) -> bool:
    """Whether the library's disk takes a small write, which tells its trouble from that of the books just tried."""
    probe = paths.state_dir(root) / "copy-probe.tmp"
    try:
        atomic_write(probe, "x")
        probe.unlink()
        return True
    except OSError:
        return False


def room_for(root: Path, sizes: Iterable[int]) -> None:
    """A run of copies into the library refused (Exit) when its disk can't be reached, or hasn't room for them all: for
    hylly add and seed alike. sizes: what each copy takes, counted once the disk has answered."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free
        need = sum(sizes)
    except OSError as e:
        raise Exit(LOCAL, f"The library at {root} can't be reached: {e}") from None
    if need > free:
        raise Exit(LOCAL, f"These need {size_words(need)}, and the library's disk has {size_words(free)} free.")


FULL, AWAY = "the library's disk is full", "the library's disk isn't taking copies now"  # why a run of copies stops


class DiskWatch:
    """When a run of copies into the library stops for its disk, for hylly add and seed alike: before a copy when the
    library isn't there any more (still_there); at once when it is full (no_room) or out of reach (_reachable); and after
    GIVE_UP_AFTER copies in a row that trying again didn't help (GaveUp) when it doesn't take a small write either
    (_takes_writes). If it does, it was those books: the count starts again."""

    def __init__(self, root: Path) -> None:
        self.root, self.in_a_row = root, 0

    def still_there(self, done: int, failed: int) -> None:
        """Before each copy: a library whose disk was unmounted (paths.library_missing) stops the run (Exit), or the rest
        would land on the disk it leaves behind, the system's on the server. A share gone for a moment is waited for."""
        if paths.library_missing(self.root, time.sleep):
            raise Exit(LOCAL, f"{done} copied, {failed} couldn't be, and the library at {self.root} isn't there any more, so "
                              "the rest wait. Running it again carries on from there.")

    def fine(self) -> None:
        """A copy made, or one that failed for a trouble of its own, which says nothing about the disk."""
        self.in_a_row = 0

    def failed(self, e: OSError, done: int, failed: int, left: int) -> None:
        """A copy that failed: when the run stops here, Exit with how many were copied (done), how many couldn't be
        (failed) and how many wait (left)."""
        self.in_a_row = self.in_a_row + 1 if isinstance(e, GaveUp) else 0  # a book's own problem says nothing about the disk
        if no_room(e):
            why = FULL
        elif not _reachable(self.root) or (self.in_a_row >= GIVE_UP_AFTER and not _takes_writes(self.root)):
            why = AWAY
        else:
            if self.in_a_row >= GIVE_UP_AFTER:  # the disk is fine, so it was those books: the run needn't end at them, nor the next
                self.in_a_row = 0
            return
        raise Exit(LOCAL, f"{done} copied, {failed} couldn't be, and {why}, so the other {left} wait. Running it again "
                          f"{'once it has room ' if why == FULL else ''}carries on from there.")


def _size(path: Path) -> int:
    """A book's size; none if it has gone by now, which its copy will say in its turn."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _same_file(a, b) -> bool:
    """Whether two paths name one file, as this computer spells them."""
    return isinstance(a, str) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _as_recorded(record: dict, src: Path) -> bool:
    """Whether a book is the one its place's record says was copied there, by the record's size, then its MD5, read only
    when the sizes are equal: the same book from another spelling of copy_from (another drive letter, a mount, a
    junction), even when the library's copy has changed since."""
    try:
        return (isinstance(record.get("md5"), str) and src.stat().st_size == record.get("size")
                and md5_of(src).hexdigest() == record["md5"])
    except OSError:
        return False


def _double(root: Path, op: Op) -> bool:
    """Whether the file at a book's place in the library is the same as the book, by size and MD5."""
    dest = root / op.dest
    try:
        return dest.stat().st_size == op.src.stat().st_size and md5_of(dest).hexdigest() == md5_of(op.src).hexdigest()
    except OSError:
        return False


SHOW_CLASHES = 3
SHOW_GONE = 10  # listed books whose copy and original are gone, named in the run's last words (each has its line before)


def _clashes(ops: list[Op]) -> list[list[Op]]:
    """The books that would get one place, compared as a disk that doesn't tell case apart sees them, each place's
    different sources together; one source listed twice is no clash."""
    by_place: dict[str, dict[str, Op]] = {}
    for op in ops:
        by_place.setdefault(op.dest.lower(), {}).setdefault(os.path.normcase(os.path.abspath(op.src)), op)
    return [list(books.values()) for books in by_place.values() if len(books) > 1]


def plan(root: Path, into: str, source: Path, list_file: Path | None = None, strip: str = "",
         passed: list[str] | None = None, gone: list[Op] | None = None,
         yours: list[Op] | None = None) -> tuple[list[Op], list[Op], dict[str, dict]]:
    """(the books asked for, those to copy, and for those whose place has another book's copy, its record by place in
    lower case), or Exit when it can't be done as asked; nothing is written. A book is done when its place is recorded
    from the same source and its file is there, or from another source when this one is gone by now: nothing of it is
    left to lose, and copy_from may have moved since its originals were removed. So is one recorded from another
    source with its size and MD5: the same book, from copy_from spelled another way. Without a list, a source that isn't
    a folder it can list is refused, after the overlap, which says more: a mistyped or unmounted one isn't "nothing new
    to copy". A list names its books, each looked for: one copied before needs no source any more, and one never copied
    that isn't there stops it. One copied before whose copy and original are both gone by now is a book you removed: it
    isn't copied, nor does it stop the others, and gone gets it, for the run to say so; unless it looks more like
    a folder moved or a disk away (_removed). One copied before whose copy you removed (removed.by_you: into _Removed,
    after it was copied, emptied from there since or not) isn't copied again, its original there or not, and yours gets
    it, for the run to say so. passed: the folders it wasn't allowed to read, for the result."""
    here, there = source.resolve(), root.resolve()
    if here.is_relative_to(there) or there.is_relative_to(here):
        raise Exit(USAGE, f"{source} and the library {root} overlap: the copies would land among the books being copied.")
    if list_file is None:
        try:
            with os.scandir(source):
                pass
        except OSError as e:  # as hylly copy says it
            raise Exit(LOCAL, f"Can't read the source, {source}: {e.strerror or e}. Is the disk there, and is the path right? "
                              "It should be a folder. Nothing was copied.") from None
    done = recorded(root)
    try:
        ops = list_ops(list_file, source, into, strip) if list_file else tree_ops(source, into, strip, passed)
    except OSError as e:
        raise Exit(LOCAL, f"Couldn't read the books in {source}: {e}. Nothing was copied.") from None
    outside = [op.dest for op in ops if not stays_inside(op.dest, into)]
    if outside:
        raise Exit(NOT_SET_UP, f"This would copy outside {Path(into)}: {outside[0]}. Check the category in {list_file}.")
    twins = _clashes(ops)
    if twins:  # only one of each could ever be copied, and the other would pass for done
        shown = "; ".join(f"{books[0].dest} from " + " and ".join(str(op.src) for op in books) for books in twins[:SHOW_CLASHES])
        more = f" (and {many(len(twins) - SHOW_CLASHES, 'more such name')})" if len(twins) > SHOW_CLASHES else ""
        fix = f"Give one of them another category in {list_file.name}, or rename it" if list_file else "Rename one of them"
        raise Exit(NOT_SET_UP, f"Books would get one name in the library, and only one of them could be copied: {shown}{more}. "
                               f"Nothing was copied. {fix}, and run it again.")
    folder_there = _list_s_folder(root / into, list_file) if list_file else True
    todo, taken, again = [], {}, []
    for op in ops:
        record = done.get(op.dest.lower())
        if record is None or not _a_file(root / op.dest):  # recorded, but gone: again, unless you removed it
            todo.append(op)
            if record is not None:
                again.append(op)
        elif not _same_file(record.get("src"), op.src) and op.src.is_file() and not _as_recorded(record, op.src):
            todo.append(op)  # another book than the one copied there
            taken[op.dest.lower()] = record
    if again:  # a copy you removed (into _Removed, emptied from it since or not) stays out, its original there or not
        mine = removed_by_you(root, {done[op.dest.lower()]["dest"]: ("", done[op.dest.lower()]) for op in again})
        out = [op for op in again if done[op.dest.lower()]["dest"] in mine]
        todo = [op for op in todo if not any(op is o for o in out)]
        if yours is not None:
            yours.extend(out)
    away = [op for op in todo if not _a_file(op.src)] if list_file else []
    missing = [op.src for op in away if op.dest.lower() not in done]
    if missing:
        more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
        raise Exit(NOT_SET_UP, f"Listed in {list_file.name} but not found: {missing[0]}{more}")
    copied = {op.dest.lower() for op in ops if op.dest.lower() in done}
    removed = _removed(root, into, folder_there, source, list_file, [op for op in away if op.dest.lower() in done], len(copied))
    if removed:
        left_out = {id(op) for op in removed}
        todo = [op for op in todo if id(op) not in left_out]
        if gone is not None:
            gone.extend(removed)
    return ops, todo, taken


def _a_file(path: Path) -> bool:
    """Whether a file is at path; False when nothing is. Trouble asking, as for a folder on the way that can't be read, is
    raised as it is, on every Python: pathlib's is_file takes it for no file since 3.14, and a copy behind a folder that
    can't be read would pass for gone."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP) and not share_is_gone(e):
            return False
        raise


def _list_s_folder(folder: Path, list_file: Path) -> bool:
    """Whether a list's own folder in the library is there; False when it isn't (yet, or any more: _removed tells). One
    that is there and can't be read stops the run (Exit), nothing copied: its books can't be told there or gone."""
    try:
        with os.scandir(folder):
            return True
    except OSError as e:
        if isinstance(e, FileNotFoundError) and not share_is_gone(e):
            return False
        raise Exit(NOT_SET_UP, f"{folder}, where the books {list_file.name} lists are copied, can't be read "
                               f"({e.strerror or e}). Is its disk there, and may Hylly read it? Nothing was copied.") from None


def _surely_gone(path: Path) -> str:
    """"" when nothing is at path for certain; else why that can't be said: something else is there, or asking failed
    otherwise, as for a folder on the way that can't be read, or a share or disk that isn't there."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError) as e:
        return "its share isn't there" if share_is_gone(e) else ""
    except OSError as e:
        return e.strerror or str(e)
    return "something that isn't a file has its name"


def _removed(root: Path, into: str, folder_there: bool, source: Path, list_file: Path, away: list[Op], copied: int) -> list[Op]:
    """Of the listed books copied before whose original isn't there (away), the ones removed on purpose: their copy and
    their original both gone for certain, as a book you take out of the library and out of copy_from. What looks more
    like a folder moved or renamed, or a disk away, stops the run instead, nothing copied: the list's own
    folder in the library not there (folder_there; one that can't be read stopped the run already, _list_s_folder); a
    copy or an original that can't be told gone; or more than half of the books the list has copied gone at once
    (copied: how many), two or more, as the catalog stops at a source down by more than half."""
    if not away:
        return []
    folder, gone = root / into, len({op.dest.lower() for op in away})
    if not folder_there:
        raise Exit(NOT_SET_UP, f"{folder}, where the books {list_file.name} lists were copied, isn't there, and {gone} of "
                               f"them aren't in {source} either. Has the folder been moved or renamed, or is its disk away? "
                               "Nothing was copied.")
    unsure = [(op, why) for op in away for why in [_surely_gone(root / op.dest) or _surely_gone(op.src)] if why]
    if unsure:
        more = f" (and {len(unsure) - 1} more)" if len(unsure) > 1 else ""
        raise Exit(NOT_SET_UP, f"Listed in {list_file.name} and copied before, but neither its copy nor its original is "
                               f"found, and they can't be told gone: {unsure[0][0].dest}, {unsure[0][1]}{more}. A folder "
                               "that can't be read, or a disk or share that isn't there, is no book removed. Nothing was copied.")
    if gone >= 2 and 2 * gone > copied:
        raise Exit(NOT_SET_UP, f"{gone} of the {copied} books {list_file.name} lists as copied before are gone, from {folder} "
                               f"and from {source}: more than half at once looks like a folder moved or renamed, or a disk "
                               f"that isn't there. Are the books in {folder}, and is its disk there? Nothing was copied.")
    return away


def _gone_words(gone: list[Op], list_file: Path) -> str:
    """What a run says of the listed books whose copy and original are both gone: how many, which, and how to have them
    looked for no more."""
    one = len(gone) == 1
    named = ", ".join(op.line or op.src.name for op in gone[:SHOW_GONE])
    named += f" and {len(gone) - SHOW_GONE} more" if len(gone) > SHOW_GONE else ""
    return (f"Passed over {many(len(gone), 'listed book')} whose copy in the library and original are both gone, "
            f"as when you remove {'a book' if one else 'books'} on purpose: {named}. To have {'it' if one else 'them'} looked "
            f"for no more, take {'its line' if one else 'their lines'} out of {list_file}.")


def _stay_out(named: list[str], list_file: Path | None) -> str:
    fix = f"another category in {list_file.name} or another name" if list_file else "another name"
    return (f"{len(named)} {'has' if len(named) == 1 else 'have'} a name another book's copy has (NAME TAKEN above), and "
            f"stay{'s' if len(named) == 1 else ''} out, run after run, until each gets {fix}.")


def add_books(root: Path, into: str, source: Path, list_file: Path | None = None, strip: str = "",
              dry_run: bool = False, log=print, passed: list[str] | None = None, gone: list[Op] | None = None) -> int:
    """Copy the books that aren't in the library yet (the listed ones, or all of them); returns how many were (or would be).
    A book that can't be copied doesn't stop the others: they are listed at the end, and the command says it failed.
    A source that can't be read, or overlaps the library, is refused first (plan). passed: filled with the folders it
    wasn't allowed to read, which the summary names too. gone: filled with the listed books whose copy and original are
    both gone, each said, and how many with what to do, but no failure: such a book was removed on purpose (plan)."""
    passed = [] if passed is None else passed
    gone, yours = [] if gone is None else gone, []
    ops, todo, taken = plan(root, into, source, list_file, strip, passed, gone, yours)
    log(f"{many(len(ops), 'book')} {'in the list' if list_file else 'in ' + str(source)}, {len(todo)} to copy into {root / into}")
    if passed:  # part of what it did: with the result, as each was said on the way
        log(f"Passed over {many(len(passed), 'folder')} it isn't allowed to read: "
            + ", ".join(passed[:5]) + (f" and {len(passed) - 5} more" if len(passed) > 5 else ""))
    for op in gone:
        log(f"  GONE: {op.dest}: copied before, and its copy and its original, {op.src}, are both gone, so it's passed over")
    for op in yours:
        log(f"  REMOVED: {op.dest}: copied before, and you removed its copy (into _Removed), so it isn't copied again")
    if dry_run:
        named = []
        for op in todo:
            if op.dest.lower() in taken and not _double(root, op):
                named.append(op.dest)
                log(f"  NAME TAKEN: {op.dest} is the copy of {taken[op.dest.lower()].get('src')}, so {op.src} wouldn't be copied")
            else:
                log(f"  {op.dest}")
        if gone:
            log(_gone_words(gone, list_file))
        if named:
            raise Exit(PARTLY, f"Of these, {_stay_out(named, list_file)}")
        return len(todo)
    room_for(root, (_size(op.src) for op in todo if not (root / op.dest).exists()))  # one in place takes no more room
    failed, named, watch = [], [], DiskWatch(root)
    for i, op in enumerate(todo, 1):
        watch.still_there(i - 1 - len(failed), len(failed))
        try:
            log(f"  {i}/{len(todo)} {copy_one(root, op)}: {op.dest}")
            watch.fine()
        except OSError as e:
            failed.append(op.dest)
            if isinstance(e, NotTheSame) and op.dest.lower() in taken:  # no trouble of the disk's, and no try will help
                named.append(op.dest)
                log(f"  {i}/{len(todo)} NAME TAKEN: {op.dest} is the copy of {taken[op.dest.lower()].get('src')}, "
                    f"so {op.src} isn't copied")
                watch.fine()
                continue
            log(f"  {i}/{len(todo)} FAILED: {op.dest}: {e}")
            watch.failed(e, i - len(failed), len(failed), len(todo) - i)
    if gone:  # said whatever else the run ends in, before the end
        log(_gone_words(gone, list_file))
    if failed:
        again = " see FAILED above. Running it again tries those again." if len(failed) > len(named) else ""
        raise Exit(PARTLY, f"{len(todo) - len(failed)} copied, {len(failed)} couldn't be:{again}"
                           + (f" {'Of them, ' if again else ''}{_stay_out(named, list_file)}" if named else ""))
    if ops and not todo:  # the run the originals wait for; a list read as empty is none
        log("Nothing left to copy. Before removing any originals, run 'hylly verify --hash': it reads every copy again.")
    return len(todo)


# ---- an Inbox: the books pass through ([inbox] in the settings) ------------------------------------------------------

STEADY = 120.0  # seconds a book in an Inbox stays the same before it's taken: one still coming over the network grows
NOTE_HEAD = "Hylly didn't add this to the library"  # how each note in Not added starts, which tells Hylly's own
JUNK_BESIDE = "._"  # macOS's file beside each one it copies to a share, which goes with it
ZERO_TAIL = 4096  # a book whose last this many bytes are all zero is a copy stalled at its full size: not finished


@dataclass
class Waiting:
    """A file in an Inbox as a pass sees it: its Inbox (by its key, folders.Inbox) and the Inbox's folder, the file and
    its path below the Inbox, its place in the library, what the look saw of it, whether the Inbox's books are looked
    up, and what its folders there say of it (folders.placed): its kind, the kind to keep as your correction would be
    ("": none), and its tags."""

    inbox: str
    source: Path
    src: Path
    rel: str
    dest: str
    size: int
    mtime: float
    md5: str = ""
    lookup: bool = True
    kind: str = ""
    fix: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def shown(self) -> str:
        """The file as it is in the Inbox, by its whole path, as a person finds it there: Inboxes may be several."""
        return str(self.source / self.rel)


@dataclass
class Look:
    """What a look found, before anything is done: the books to take in (take); those the library has already, each with
    its copy's place (already); those that can't be taken, each with its kind of trouble and the details (aside); those
    still coming in, or not seen long enough yet (later), and of those, the ones that couldn't be read, with why
    (unread); those stalled at their full size, their end all zeros (unfinished); the Inboxes whose folder isn't there
    (missing), that are or hold the library by any spelling of it (overlap), where a probe couldn't be written to tell,
    with why (unprobed), or that are one inside another (nested); the folders in them it couldn't read (unreadable); the
    folders whose names no tag can be (no_tags: the Inbox's key and the folder's path in it); and what it saw of each
    file, for the next look (seen)."""

    take: list[Waiting] = field(default_factory=list)
    already: list[tuple[Waiting, str]] = field(default_factory=list)
    aside: list[tuple[Waiting, str, str]] = field(default_factory=list)
    later: list[Waiting] = field(default_factory=list)
    unread: list[tuple[Waiting, str]] = field(default_factory=list)
    unfinished: list[Waiting] = field(default_factory=list)
    missing: list = field(default_factory=list)
    overlap: list = field(default_factory=list)
    unprobed: list[tuple] = field(default_factory=list)
    nested: list = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    no_tags: list[tuple[str, str]] = field(default_factory=list)
    seen: dict = field(default_factory=dict)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _identity(path) -> tuple[int, int] | None:
    """A file's or a folder's identity on its disk, (device, file number), however its path is spelt: \\\\?\\, another
    share name, a link or a bind mount lead to the same one. None if it can't be looked at, or its disk gives no numbers
    (FAT, some shares): then only its path can tell."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _resolved(path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _up(path) -> list[Path]:
    """A path and every folder above it, as it is written and with its links followed."""
    found: list[Path] = []
    for p in (Path(os.path.abspath(path)), _resolved(path)):
        found += [p, *p.parents]
    return found


def _within(inner, outer) -> bool:
    """Whether inner is outer, or inside it, by any spelling of either: their paths with links followed, or the
    identity of outer (_identity) among those of inner and the folders above it."""
    a, b = _resolved(inner), _resolved(outer)
    if os.path.normcase(str(a)) == os.path.normcase(str(b)) or a.is_relative_to(b):
        return True
    target = _identity(outer)
    return target is not None and any(_identity(p) == target for p in _up(inner))


def _one_file(a, b) -> bool:
    """Whether two paths are one file, by its identity, or where that can't tell, by their paths with links followed."""
    first, second = _identity(a), _identity(b)
    if first is not None and second is not None:
        return first == second
    return os.path.normcase(str(_resolved(a))) == os.path.normcase(str(_resolved(b)))


PROBE = ".hylly-probe-"  # a file's name a pass writes for a moment, to tell whether two folders are one (Guard.probed)
FOLDERS_KEPT = 3600.0  # seconds the library's folders' identities are kept for the passes, unless its top folders change


@contextlib.contextmanager
def _probe(folder: Path):
    """A file of a name of its own, made in a folder for as long as the block runs, and then removed: its name. OSError
    if it can't be made, as in a folder or on a share that takes no writes."""
    name = f"{PROBE}{secrets.token_hex(8)}"
    path = folder / name
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        yield name
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _spelt_up(path) -> list[Path]:
    """A folder and every folder above it, up to its drive or its share, as its own path spells them: no link followed,
    as a second share name or mount of a folder is what this is for."""
    path = Path(os.path.abspath(path))
    return [path, *path.parents]


class Guard:
    """What an Inbox's pass may never touch: the library, by any spelling of it. Told three ways, each catching what
    the others can't: by the paths with links followed; by the folders' identities on the disk (copy_from as
    \\\\?\\C:\\..., a link, a bind mount of one of the library's folders); and by a probe file written for a moment in
    one folder and looked for in the other (probed): a second share name or mount of one folder has identities of its
    own. The identities of the library's folders, gathered only when needed, on the library's own disk, are kept for
    FOLDERS_KEPT, or until its top folders change, in the state folder (folders_file)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._folders: set | None = None
        self._dirs: list[str] = []
        self._probed: dict[tuple[str, str], bool] = {}

    def _top(self) -> list[str]:
        try:
            return sorted(e.name for e in os.scandir(self.root) if e.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def folders(self) -> set:
        """The identities of the library's folders, its top too (_listing)."""
        return self._listing()[0]

    def folder_paths(self) -> list[Path]:
        """Every folder of the library below its top, by the library's own spelling, as the walk found them (_listing)."""
        return [self.root / rel for rel in self._listing()[1]]

    def _listing(self) -> tuple[set, list[str]]:
        """The library's folders, walked without following a link or a junction: their identities, and their paths
        below the top. Kept for FOLDERS_KEPT in folders_file, as the passes come every minute while books wait, unless
        the library's top folders have changed since."""
        if self._folders is None:
            now, top, kept = time.time(), self._top(), {}
            with contextlib.suppress(OSError, ValueError):
                kept = json.loads(folders_file().read_text(encoding="utf-8"))
            if (isinstance(kept, dict) and kept.get("root") == str(self.root) and kept.get("top") == top
                    and isinstance(kept.get("at"), (int, float)) and 0 <= now - kept["at"] < FOLDERS_KEPT
                    and isinstance(kept.get("ids"), list) and isinstance(kept.get("dirs"), list)):
                self._folders = {tuple(i) for i in kept["ids"] if isinstance(i, list) and len(i) == 2}
                self._dirs = [d for d in kept["dirs"] if isinstance(d, str) and paths.inside_library(d)]
                return self._folders, self._dirs
            found, dirs = {i for i in (_identity(self.root),) if i}, []
            for here, subfolders, _ in os.walk(self.root, onerror=lambda e: None):
                subfolders[:] = [d for d in subfolders if not folders.leads_elsewhere(os.path.join(here, d))]
                for d in subfolders:
                    dirs.append(Path(here, d).relative_to(self.root).as_posix())
                    found.update(i for i in (_identity(os.path.join(here, d)),) if i)
            self._folders, self._dirs = found, dirs
            with contextlib.suppress(OSError):  # gathered again at the next pass, if it can't be kept
                atomic_write(folders_file(), json.dumps({"root": str(self.root), "top": top, "at": now, "dirs": dirs,
                                                         "ids": sorted(list(i) for i in found)}), durable=False)
        return self._folders, self._dirs

    def probed(self, here: Path, there: Path) -> bool:
        """Whether a probe file made in the folder here shows in the folder there: the two are one folder, however each
        is spelt. Once a pass for each pair. OSError if the probe can't be made."""
        key = (os.path.normcase(os.path.abspath(here)), os.path.normcase(os.path.abspath(there)))
        if key not in self._probed:
            with _probe(Path(here)) as name:
                self._probed[key] = os.path.lexists(Path(there) / name)
        return self._probed[key]

    def probed_overlap(self, source: Path) -> bool:
        """Whether an Inbox and the library overlap by a spelling that only a probe tells: one made at the library's top
        found in the Inbox's folder or a folder above it, by the Inbox's own spelling, up to its drive or share (the
        Inbox is the library, or inside it); or one made in the Inbox's folder found at the library's top or above it, or
        in any of the library's folders (folder_paths), by the library's spelling (the library is inside the Inbox, or
        the Inbox is a folder of the library's, reached by a second share name or mount). That last is a look at each
        folder of the library, kept an hour between passes: on a library of a few thousand folders, a few thousand
        looks at a name, about a quarter of a second for 3,000 on a Windows computer's own disk (less on Linux), a
        second or more over a network share, and only for a pass that has books waiting. OSError, with words, if a probe can't be written: the pass
        then takes nothing from that Inbox."""
        try:
            with _probe(self.root) as name:
                if any(os.path.lexists(folder / name) for folder in _spelt_up(source)):
                    return True
        except OSError as e:
            raise OSError(f"the library, {self.root}, takes no file just now ({e.strerror or e}), and Hylly writes one there "
                          "for a moment to tell whether the Inbox is the library by another name") from None
        try:
            with _probe(Path(source)) as name:
                return any(os.path.lexists(folder / name) for folder in [*_spelt_up(self.root), *self.folder_paths()])
        except OSError as e:
            raise OSError(f"the Inbox takes no file just now ({e.strerror or e}), and Hylly writes one there for a moment to "
                          "tell whether it is the library by another name") from None

    def holds(self, path) -> bool:
        """Whether a file or a folder is the library or in it."""
        if _within(path, self.root):
            return True
        top = _identity(self.root)
        ups = [i for i in (_identity(p) for p in _up(path)) if i]
        if top is None or not any(dev == top[0] for dev, _ in ups):
            return False  # on another disk, nothing of it can be a folder of the library
        return any(i in self.folders() for i in ups)

    def overlaps(self, source) -> bool:
        """Whether an Inbox and the library overlap: one is the other, or inside it."""
        return self.holds(source) or _within(self.root, source)


def folders_file() -> Path:
    """Where the passes keep the identities of the library's folders between them (Guard.folders): the state folder."""
    return paths.place("state") / "inbox-library-folders.json"


def _in_library(root: Path, records: list[dict]) -> str:
    """The place of a recorded copy, of those with a book's size and MD5, that the library still has as it was recorded:
    its size, and its MD5 read again now. "" if none."""
    for record in records:
        dest = record.get("dest")
        copy = paths.library_file(root, dest)
        try:
            if copy is None or not copy.is_file() or copy.stat().st_size != record["size"]:
                continue
            if md5_of(copy).hexdigest() == record["md5"]:
                return dest
        except OSError:
            continue
    return ""


def _same(a: Path, b: Path) -> bool:
    """Whether two files are the same by their size, then their MD5, both read now."""
    try:
        return a.stat().st_size == b.stat().st_size and md5_of(a).hexdigest() == md5_of(b).hexdigest()
    except OSError:
        return False


def _zero_tail(path: Path, size: int) -> bool:
    """Whether a file's last ZERO_TAIL bytes (all of it, if it is shorter) are zeros: a copy that set its full size first
    and stalled, as Explorer's to a share may, which no kind of book ends with."""
    if size <= 0:
        return False
    with open(path, "rb") as fh:
        fh.seek(max(0, size - ZERO_TAIL))
        tail = fh.read(ZERO_TAIL)
    return not tail.strip(b"\0")


def nested(inboxes: list) -> list[tuple]:
    """The Inboxes that are another one, by another spelling of it, or inside it or hold it, each with that other
    (_within): a book in them would be taken twice, so a pass takes nothing from either, and hylly doctor says so."""
    return [(inbox, other) for inbox in inboxes for other in inboxes
            if other is not inbox and (_within(inbox.folder, other.folder) or _within(other.folder, inbox.folder))]


def look(root: Path, inboxes: list, seen: dict, now: float, by_hand: bool = False, guard: Guard | None = None) -> Look:
    """Each Inbox (folders.inboxes), looked at: nothing is written, and of the books only what tells them apart is read.
    Each book's place is the folder of yours its kind belongs in, as its folders in the Inbox say (folders.placed).
    A book is taken once the look before saw it the same, by its size and time, and either it has stayed so for STEADY
    seconds or its time is that old (a copy that keeps the time of its original); by_hand (hylly add typed by a person),
    also one seen for the first time whose time is that old. One the library has already, by the size and MD5 a copy of
    it was recorded with, is known at once, whatever its name: a book cut short has another MD5. Both MD5s are read now,
    never taken from what an earlier look kept, as the library's state folder is one a walled server may write. Books
    whose places in the library would be one: the first is taken, and the others set aside. An Inbox that is, or holds,
    the library by any spelling of it (guard), or is inside another Inbox or holds one, is passed over whole."""
    from . import rules

    guard = guard or Guard(root)
    books = rules.current().book_suffixes
    done = recorded(root)
    by_sum: dict[tuple[int, str], list[dict]] = {}
    for record in done.values():
        if isinstance(record.get("size"), int) and isinstance(record.get("md5"), str):
            by_sum.setdefault((record["size"], record["md5"]), []).append(record)
    sizes = {size for size, _ in by_sum}
    found, places, there = Look(), {}, []
    for inbox in inboxes:
        source = Path(inbox.folder)
        try:
            ok = source.is_dir()
        except OSError:
            ok = False
        if not ok:
            found.missing.append(inbox)
        elif guard.overlaps(source):
            found.overlap.append(inbox)
        else:
            try:
                if guard.probed_overlap(source):
                    found.overlap.append(inbox)
                    continue
            except OSError as e:
                found.unprobed.append((inbox, str(e)))
                continue
            there.append(inbox)
    inside = [inbox for inbox, _ in nested(there)]
    found.nested = [inbox for inbox in there if any(inbox is other for other in inside)]
    for inbox in (inbox for inbox in there if inbox not in found.nested):
        source = Path(inbox.folder)
        before = seen.get(inbox.key) if isinstance(seen.get(inbox.key), dict) else {}
        now_seen = found.seen[inbox.key] = {}
        for path in folders.inbox_files(source, found.unreadable):
            rel = path.relative_to(source).as_posix()
            try:
                st = path.stat()
            except OSError:
                continue  # gone meanwhile
            where = folders.placed(inbox, rel, st.st_size)
            found.no_tags += [(inbox.key, d) for d in where.no_tags if (inbox.key, d) not in found.no_tags]
            book = Waiting(inbox.key, source, path, rel, where.dest, st.st_size, st.st_mtime, lookup=inbox.lookup,
                           kind=where.kind, fix=where.fix, tags=where.tags)
            last = before.get(rel) if isinstance(before.get(rel), dict) else {}
            same = bool(last) and last.get("size") == book.size and last.get("mtime") == book.mtime
            entry = now_seen[rel] = {"size": book.size, "mtime": book.mtime,
                                     "since": last["since"] if same and _number(last.get("since")) else now}
            try:
                if book.size in sizes:  # read only when a recorded copy has its size; read now, every time
                    book.md5 = md5_of(path).hexdigest()
                    copy = _in_library(root, by_sum.get((book.size, book.md5), []))
                    if copy:
                        found.already.append((book, copy))
                        continue
                steady = (same and (now - entry["since"] >= STEADY or now - book.mtime >= STEADY)
                          or by_hand and not last and now - book.mtime >= STEADY)
                if not steady:
                    found.later.append(book)
                    continue
                if _zero_tail(path, book.size):
                    found.unfinished.append(book)
                    continue
                why = _why_not(root, book, books, done, places)
            except OSError as e:
                found.later.append(book)  # it can't be read just now: the next look tries again
                found.unread.append((book, e.strerror or str(e)))
                continue
            if why:
                found.aside.append((book, *why))
            else:
                found.take.append(book)
                places[book.dest.lower()] = book
    return found


def _why_not(root: Path, book: Waiting, books, done: dict[str, dict], places: dict[str, Waiting]) -> tuple[str, str] | None:
    """Why a steady book can't be taken, as (its kind, the details), or None: not a book Hylly reads, damaged, its place
    in the library another book's, or that of another book in the Inbox taken before it. The same book already at its
    place is no trouble: its copy, as hylly add's, records it there."""
    ext = book.src.suffix.lower()
    if ext not in books:
        return "not a book", ext or "no ending at all"
    trouble = damaged(book.src)
    if trouble:
        return "damaged", trouble
    other = places.get(book.dest.lower())
    if other is not None:
        return "twin", other.shown  # by its whole path: it may be in another Inbox
    record = done.get(book.dest.lower())
    for place in (book.dest, record.get("dest") if record else None):
        there = paths.library_file(root, place) if place else None
        if there is not None and there.exists():
            return None if there.is_file() and _same(there, book.src) else ("name taken", place)
    return None


def _vint(data: bytes, at: int) -> tuple[int | None, int]:
    """RAR 5's variable-length number at a place: (its value, its length in bytes), or (None, 0)."""
    value, shift = 0, 0
    for n, byte in enumerate(data[at:at + 10], 1):
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, n
        shift += 7
    return None, 0


def _rar_ends(head: bytes, tail: bytes) -> bool:
    """Whether a RAR archive ends with its end-of-archive block, as one copied whole does: RAR 4's (type 0x7B, its size
    reaching the end), or RAR 5's (a CRC, then its size, reaching the end, and type 5)."""
    if head.startswith(b"Rar!\x1a\x07\x01\x00"):
        for length in range(7, 40):
            block = tail[-length:]
            size, n = _vint(block, 4)
            if len(block) == length and size is not None and 4 + n + size == length and block[4 + n] == 5:
                return True
        return False
    for size in range(7, 40):
        block = tail[-size:]
        if len(block) == size and block[2] == 0x7B and int.from_bytes(block[5:7], "little") == size:
            return True
    return False


def damaged(path: Path) -> str:
    """Why a book isn't whole, or what its name says, in a few words; "" when nothing says so. Empty; its first bytes not
    its kind's; a ZIP (an EPUB, a CBZ, a package) without the table of contents at its end, as a copy cut short is; a
    RAR (a CBR) without its end-of-archive block; an EPUB or a MOBI no reader could open (meta.cant_open: one locked by
    the shop that sold it is no damage); a PDF without the mark it ends with; a MOBI, AZW or AZW3 shorter than its list
    of records, or whose last record begins past its end; a 7z or a DjVu shorter than its start says; an FB2 that doesn't
    end. Raises OSError when it can't be read just now, which says nothing about the book."""
    from . import meta

    ext = path.suffix.lower()[1:]
    size = path.stat().st_size
    if size == 0:
        return "it is empty"
    if wrong_kind(ext, path):
        return f"it doesn't start as {ext.upper()} files do"
    short = "it is cut short: its end is missing, as a copy that stopped half way has it"
    with open(path, "rb") as fh:
        head = fh.read(HEAD)
        fh.seek(max(0, size - 4096))
        tail = fh.read()
        if ext in ("mobi", "azw", "azw3", "prc") and len(head) >= 78:
            count = struct.unpack(">H", head[76:78])[0]
            if 78 + 8 * count > size:
                return short
            if count:
                fh.seek(78 + 8 * (count - 1))
                last = fh.read(4)
                if len(last) < 4 or struct.unpack(">I", last)[0] >= size:
                    return short
    if head.startswith(b"PK"):
        try:
            zipfile.ZipFile(path).close()  # reads its table of contents, at its end
        except (zipfile.BadZipFile, ValueError, EOFError, struct.error):
            return short
    if head.startswith(b"Rar!\x1a\x07") and not _rar_ends(head, tail):
        return short
    if ext in meta.OPEN_CHECKED and meta.cant_open(path) == meta.DAMAGED:
        return "no reader could open it: what every reader needs is missing from it"
    if ext == "pdf" and b"%%EOF" not in tail:
        return short
    if head.startswith(b"7z\xbc\xaf\x27\x1c") and len(head) >= 28:
        offset, length = struct.unpack("<QQ", head[12:28])
        if 32 + offset + length > size:
            return short
    if ext == "djvu" and len(head) >= 12 and 12 + struct.unpack(">I", head[8:12])[0] > size:
        return short
    if ext == "fb2" and b"</FictionBook>" not in tail:
        return short
    return ""


def no_room(e: BaseException | None) -> bool:
    """Whether an error, or one that led to it (a GaveUp's), is a disk with no room left: no book's own trouble."""
    for _ in range(6):
        if e is None:
            return False
        if isinstance(e, OSError) and (e.errno in NO_ROOM or getattr(e, "winerror", None) in NO_ROOM_WINDOWS):
            return True
        e = e.__cause__ or e.__context__
    return False


def take_in(root: Path, take: list[Waiting], log=print) -> tuple[list[tuple[Waiting, str]], list[tuple[Waiting, str, str]], str]:
    """The books to take, copied into the library as hylly add copies them, each copy checked and recorded (copy_one):
    (those copied, or there already as the same book, each with which; those that couldn't be, each with the kind of
    trouble and its details, to be set aside; and the library's own trouble that stopped it, in words, or "": the books
    not tried yet then wait in the Inbox for a later pass). A disk with no room is the library's trouble, never a book's:
    the books wait."""
    copied: list[tuple[Waiting, str]] = []
    failed: list[tuple[Waiting, str, str]] = []
    try:
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free
    except OSError as e:
        return copied, failed, f"The library at {root} can't be reached ({e.strerror or e}): the books wait in the Inbox."
    need = sum(book.size for book in take if not (root / book.dest).exists())
    full = (f"The library's disk has no room for the Inbox's books ({size_words(need)}, with {size_words(free)} free): "
            "they wait in the Inbox until there is room.")
    if need > free:
        return copied, failed, full
    for book in take:
        if paths.library_missing(root):  # or they would land on the disk an unmounted one leaves behind
            return copied, failed, f"The library at {root} isn't there any more: the books not copied yet wait in the Inbox."
        try:
            how = copy_one(root, Op(book.src, book.dest))
        except NotTheSame:
            failed.append((book, "name taken", book.dest))
            continue
        except OSError as e:
            if no_room(e):
                return copied, failed, ("The library's disk is full: the books not copied yet wait in the Inbox until "
                                        "there is room.")
            if not _reachable(root) or not _takes_writes(root):
                return copied, failed, (f"The library's disk isn't taking copies now ({e}): the books not copied yet wait in "
                                        "the Inbox.")
            failed.append((book, "copy", str(e)))
            continue
        log(f"  {how}: {book.dest}")
        copied.append((book, how))
    return copied, failed, ""


ASIDE = {  # what a note in Not added says of each kind of trouble ({}: its details), and what to do
    "not a book": ("It isn't a kind of file Hylly reads as a book ({}).",
                   "If it is a book, convert it to one of the kinds Hylly reads (EPUB, PDF, MOBI, AZW3, CBZ, CBR and the "
                   "others the help names), and put that in the Inbox. If it isn't, it can go."),
    "damaged": ("It is damaged, or another kind of file than its name says: {}.",
                "If it was still being copied in when it was taken, put it back in the Inbox. Otherwise a good copy of it, "
                "put in the Inbox, is taken in."),
    "unopened": ("Its copy in the library can't be opened by any reader: {}.",
                 "The library keeps that copy. If this one was still being copied in, put it back in the Inbox; otherwise "
                 "a good copy of it in the library's folder, in place of the one there, is what to put right."),
    "name taken": ("The library has another book by this name, {}, which isn't this one.",
                   "Rename this one, or put it in another folder of the Inbox, and put it back. The library keeps the book "
                   "it has: if this one should take its place, put it there by hand."),
    "twin": ("Another book, {}, gets this one's name in the library, and was taken first.",
             "Rename this one, or put it in another folder of the Inbox, and put it back."),
    "copy": ("Its copy in the library didn't check against it ({}).",
             "If it was still being copied in when it was taken, put it back in the Inbox, and it is tried again."),
}


def aside_why(kind: str, details: str) -> str:
    """Why a book was set aside, in a sentence (ASIDE)."""
    return ASIDE[kind][0].format(details)


def _free_name(path: Path) -> Path:
    """A place for a file that has nothing there yet, nor a note: NAME (2).EXT, and so on, beside one that has."""
    n, found = 2, path
    while os.path.lexists(found) or os.path.lexists(found.with_name(found.name + ".txt")):
        found = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return found


def _plain_way(source: Path, folder: Path) -> None:
    """That the way from an Inbox down to a folder of it holds no link or junction that is there already, Not added
    included, and stays inside it: OSError if it doesn't, and nothing is done through it."""
    for part in [folder, *folder.parents]:
        if part == source or len(part.parts) <= len(source.parts):
            break
        if os.path.lexists(part) and folders.leads_elsewhere(part):
            raise OSError(f"{part} is a link or a junction, which could lead out of the Inbox: nothing is moved through it")
    if not _resolved(folder).is_relative_to(_resolved(source)):
        raise OSError(f"{folder} leads out of the Inbox {source}: nothing is moved there")


def set_aside(book: Waiting, kind: str, details: str) -> Path:
    """A book that can't be taken, moved into its Inbox's Not added folder at its path below the Inbox, never over
    another, with a note beside it (NAME.txt) saying why and what to do; then the folders that leaves empty go. Not added,
    or a folder in it, that is a link or a junction is refused: nothing is moved there. Where it went; OSError if it
    couldn't be moved, and it stays where it is."""
    target = _free_name(book.source / folders.NOT_ADDED / book.rel)
    _plain_way(book.source, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _plain_way(book.source, target.parent)  # nothing made a link of it meanwhile
    os.rename(book.src, target)
    why, then = ASIDE[kind]
    note = (f"{NOTE_HEAD} on {datetime.now().strftime('%Y-%m-%d %H:%M')}: {book.rel}, as it was in the Inbox.\n\n"
            f"{why.format(details)}\n\n{then}\n\n"
            f"Put back anywhere in the Inbox ({book.source}), it is tried again at the next pass. This note goes by itself "
            "once the book is gone from beside it.\n")
    with contextlib.suppress(OSError):  # set aside all the same: the pass's log says why
        atomic_write(target.with_name(target.name + ".txt"), note, durable=False)
    _prune(book.source, book.src.parent)
    return target


def _only_junk(folder: Path) -> list[Path] | None:
    """What a folder holds, if that's only what the systems leave there (folders.SYSTEMS_OWN, and macOS's ._ files):
    those files; None if it holds anything else, a link or a junction among it."""
    found = []
    for entry in folder.iterdir():
        if folders.leads_elsewhere(entry) or not entry.is_file() or not (entry.name.lower() in folders.SYSTEMS_OWN
                                                                         or entry.name.startswith(JUNK_BESIDE)):
            return None
        found.append(entry)
    return found


def standing(source: Path, folder: Path) -> bool:
    """Whether a folder of an Inbox is one of those kept for the books to come, empty or not: at its top, named for a
    kind (folders.kind_named: Comics, RPG) or giving a tag (folders.tag_of: #To read). One starting with # that gives
    no tag (#, or one too long) goes as a collection's folder does."""
    return (len(folder.parts) == len(source.parts) + 1
            and (bool(folders.tag_of(folder.name)) or bool(folders.kind_named(folder.name))))


def _prune(source: Path, folder: Path) -> None:
    """The folders a pass has left empty, from folder up to the Inbox: each removed if it holds nothing, or only what the
    systems leave there; never the Inbox itself, nor one of its folders kept for the books to come (standing), nor a
    link or a junction, nor anything outside the Inbox. Trouble with that leaves the folder."""
    try:
        top = _resolved(source)
        while (not folders.leads_elsewhere(folder) and _resolved(folder) != top and _resolved(folder).is_relative_to(top)
               and len(folder.parts) > len(source.parts) and not standing(source, folder)):
            junk = _only_junk(folder)
            if junk is None:
                return
            for path in junk:
                path.unlink()
            folder.rmdir()
            folder = folder.parent
    except OSError:
        return


def remove(book: Waiting, copy: Path, guard: Guard) -> str:
    """A book's original, removed from the Inbox once the library has it, with macOS's ._ file beside it, and then the
    folders that leaves empty. Only while it is as the look saw it, by its size and time ("changed" if not); never if it
    is the library's own file, by any spelling of it, or in the library, or in the folder its copy is in, as a probe file
    tells ("library"); and only if it and its copy in the
    library have the same size and MD5, both read now ("differs" if not), which also says the copy is there. "removed"
    when it is gone; OSError if it couldn't be removed."""
    try:
        st = book.src.stat()
    except FileNotFoundError:
        return "removed"  # gone already, by hand, say
    if (st.st_size, st.st_mtime) != (book.size, book.mtime):
        return "changed"
    if _one_file(book.src, copy) or guard.holds(book.src) or guard.probed(book.src.parent, copy.parent):
        return "library"
    _plain_way(book.source, book.src.parent)
    if folders.leads_elsewhere(book.src) or not _same(book.src, copy):
        return "differs"
    book.src.unlink()
    beside = book.src.with_name(JUNK_BESIDE + book.src.name)
    with contextlib.suppress(OSError):
        if beside.is_file() and not folders.leads_elsewhere(beside):
            beside.unlink()
    _prune(book.source, book.src.parent)
    return "removed"


def tidy(source: Path) -> int:
    """An Inbox's Not added folder, once a person has taken books out of it: each of Hylly's notes whose book is gone
    from beside it, what the systems leave there, and the folders that leaves empty, removed (the folder itself too, which
    a pass makes again when it needs it). Never through a link or a junction: Not added as one is left as it is. How many
    notes went."""
    folder = source / folders.NOT_ADDED
    gone = 0
    try:
        if not folder.is_dir() or folders.leads_elsewhere(folder):
            return 0
        walked = []
        for here, subfolders, names in os.walk(folder):
            subfolders[:] = [d for d in subfolders if not folders.leads_elsewhere(os.path.join(here, d))]
            walked.append((Path(here), names))
        for here, names in reversed(walked):  # the deepest first, so that a folder left empty can go
            for name in names:
                note = here / name
                if not name.endswith(".txt") or folders.leads_elsewhere(note) or os.path.lexists(here / name[:-4]):
                    continue
                try:
                    with open(note, encoding="utf-8", errors="replace") as fh:
                        mine = fh.read(len(NOTE_HEAD)) == NOTE_HEAD
                    if mine:
                        note.unlink()
                        gone += 1
                except OSError:
                    continue
            with contextlib.suppress(OSError):
                junk = _only_junk(here)
                if junk is not None:
                    for path in junk:
                        path.unlink()
                    here.rmdir()
    except OSError:
        pass
    return gone
