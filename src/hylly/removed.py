"""The files you removed from the library on the Server page, and what became of them: To decide's Keep only this one
and Remove both, in Compare the files, for two copies that may be one book, move a copy's files out of the library into
_Removed, a folder at the top of the library, each at its own place below the library, never deleting them; the Server
page's Removed part lists them, puts one back in its place, and empties _Removed for good. A folder at the top whose
name starts with _ is passed over by every build, the server never sends what is in it, and a backup leaves it out, so
a file there is out of the library until it's put back.

Each removal is kept in the database, a row a file for good (the table removed: migrations/0005_removed.sql): where it
was, where it went, and whether it was put back or emptied since. hylly verify reads them, and so does hylly add (by_you),
so that a file you removed isn't taken for one missing, also once _Removed is emptied, and a copy of your own folders'
isn't copied in again from its original. A shop's file gets its title's answer that it was moved as it goes (curation's
answers, as hylly decide's "I moved it by hand: never fetch it again" gives it), so that no download fetches it again,
then or after _Removed is emptied; Put back takes that answer back exactly. Only files of the library are removed or
put back (place): never one in Hylly's own folders (a shop's state among them), in _Removed itself, or at the top of the
library.

Nothing is ever written over, nor done through a link: a file goes to a name nothing has (_free), and is moved there in
a way that refuses a name taken meanwhile (_new_name; on a disk that keeps no hard links, but for a moment's window);
_Removed, and every folder of it on the way, must be no link or junction (_plain_way), or a removal could land in
Hylly's own folders, and an Empty delete books."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from . import curation, db, folders, paths, settings, storage
from .shops import SHOPS

FOLDER = "_Removed"  # at the top of the library: a name starting with _, which no build reads
LISTED_MOST = 2000  # files the Server page's Removed part lists at most; its count and its size are of every one
WALKED_MOST = 100_000  # files a look into _Removed counts at most: one past it says there are more
PACKED = re.compile(r"(.+?\.zip)/(.+)", re.IGNORECASE)  # a file inside a ZIP package, as reader.PACKED has it
NO_HARD_LINKS = {getattr(errno, name) for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "ENOSYS")  # a disk that
                 if hasattr(errno, name)}  # keeps none, or won't make one of this file
REMOVED = db.Shape("removed", (("path", "path", "text"), ("held", "held", "text"), ("size", "size", "int"),
                               ("title", "title", "text"), ("t", "title_t", "text"), ("shop", "shop", "text"),
                               ("moved", "moved", "json"), ("at", "removed_at", "text"), ("state", "state", "text"),
                               ("ended", "ended_at", "text")))


class Refused(Exception):
    """What can't be done, in words for the Server page: nothing was changed by it."""


def folder(root: Path) -> Path:
    return Path(root) / FOLDER


def _hyllys(root: Path, top: str) -> bool:
    """Whether the folder at the top of the library called top is one of Hylly's own (settings.HYLLYS: catalog, state,
    curation, superseded) or one every build passes over (a name starting with _ or a dot: _Removed among them): by its
    name, case folded, and failing that by the disk, as the same folder may be reached by another name or a link."""
    if top.casefold() in settings.HYLLYS or top.startswith(("_", ".")):
        return True
    try:
        asked = os.stat(Path(root) / top)
    except OSError:
        return False
    for own in (*settings.HYLLYS, FOLDER):
        try:
            there = os.stat(Path(root) / own)
        except OSError:
            continue
        if asked.st_ino and (asked.st_dev, asked.st_ino) == (there.st_dev, there.st_ino):
            return True
    return False


def place(root: Path, rel) -> Path | None:
    """The library's file at rel (its place below the library, with / between the folders) as a path, where a file may
    be removed from or put back in: inside the library by its words and by where any link on the way leads
    (paths.library_file), in a folder (never at the top, where the library's marker is), not in one of Hylly's own
    folders or one a build passes over (_hyllys), and nothing hidden on the way (a name starting with a dot). None for
    any other."""
    if not paths.inside_library(rel) or (paths.WINDOWS and paths.renamed_on_windows(rel)):
        return None
    parts = rel.split("/")
    if len(parts) < 2 or _hyllys(root, parts[0]) or any(p.startswith(".") for p in parts):
        return None
    target = paths.library_file(root, rel, resolved=True)
    if target is None:
        return None
    inside = target.relative_to(Path(root).resolve()).parts
    if len(inside) < 2 or _hyllys(root, inside[0]):
        return None
    return Path(root) / rel


def _resolved(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _shown(root: Path, path: Path) -> str:
    """A path in the library as the page names it, below the library's top, with /."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _plain_way(root: Path, below: Path) -> None:
    """That _Removed, and every folder of it on the way down to below that is there already, is no link or junction
    (folders.leads_elsewhere, which takes what can't be looked at for one), and that below, as its links lead, is inside
    _Removed as it leads: Refused, in words, if not, and nothing is done through it. As copies._plain_way keeps an
    Inbox's way: a link there could lead a removal into Hylly's own folders, a put back to what isn't in _Removed, or an
    Empty to the books."""
    base = folder(root)
    for part in [below, *below.parents]:
        if part != base and base not in part.parents:
            break
        if os.path.lexists(part) and folders.leads_elsewhere(part):
            raise Refused(f"{_shown(root, part)} is a link or a junction, which could lead anywhere: nothing is moved "
                          "through it, nor deleted. Take it away, and try again.")
    if os.path.lexists(base) and not _resolved(below).is_relative_to(_resolved(base)):
        raise Refused(f"{_shown(root, below)} leads out of {FOLDER}: nothing is moved there.")


def files_of(root: Path, item: dict) -> list[str]:
    """A title's files (its record's f), each by its place in the library, unquoted: a file inside a ZIP package by the
    package's, each once."""
    found: list[str] = []
    for f in item.get("f") or []:
        if not (isinstance(f, list) and f and isinstance(f[0], str)):
            continue
        rel = unquote(f[0])
        packed = PACKED.fullmatch(rel)
        if packed and not (Path(root) / rel).is_file():
            rel = packed[1]
        if rel.casefold() not in {x.casefold() for x in found}:
            found.append(rel)
    return found


def going(root: Path, titles: list[dict]) -> list[dict]:
    """What removing these titles' files moves: each of their files that is in the library now, {path, size, title (its
    catalog id), t (its name)}. Refused, naming it, for a file that may not be removed (place), or that is a link (only
    a file is moved, never what a link stands for): then none is."""
    out = []
    for item in titles:
        for rel in files_of(root, item):
            path = place(root, rel)
            if path is None:
                raise Refused(f"{rel} isn't a file of the library that may be removed (it is in one of Hylly's own "
                              "folders, or outside the library): nothing was removed.")
            try:
                st = os.lstat(path)
            except OSError:  # gone already
                continue
            if storage.is_link_stat(st):
                raise Refused(f"{rel} is a link, not a file: nothing was removed.")
            if stat.S_ISREG(st.st_mode) and all(g["path"] != rel for g in out):
                out.append({"path": rel, "size": st.st_size, "title": str(item.get("id") or ""), "t": str(item.get("t") or "")})
    return out


def _free(path: Path) -> Path:
    """path, or the first of NAME (2).EXT, NAME (3).EXT and so on that has nothing there, a link or anything else, as
    copies._free_name finds one: by the disk's own answer, whatever its idea of two names being one (letters of either
    size, or é written as one letter or two)."""
    n, found = 2, path
    while os.path.lexists(found):
        found = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return found


def _new_name(src: Path, dest: Path) -> None:
    """src given the name dest, which must have nothing there: never written over (FileExistsError, nothing changed).
    A hard link and then the old name's removal, where the disk keeps hard links (of the name itself, never what a link
    leads to); on Windows a rename, which refuses a name taken. On a disk that keeps no hard links (a share, say), a
    rename, after a look made at the last moment: Python has no rename that refuses a name taken there (Linux's
    RENAME_NOREPLACE), so a file another program puts in that very place between the look and the rename, a moment's
    window, could still be written over. Where dest is on another disk, which neither reaches, a copy into a file made
    new (_copy_aside)."""
    if os.name == "nt":
        try:
            os.rename(src, dest)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            _copy_aside(src, dest)
        return
    try:
        if os.link in os.supports_follow_symlinks:
            os.link(src, dest, follow_symlinks=False)  # the name itself (src is a file anyway: going, put_back)
        else:
            os.link(src, dest)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno == errno.EXDEV:
            return _copy_aside(src, dest)
        if e.errno not in NO_HARD_LINKS:
            raise
        _rename_after_a_look(src, dest)
        return
    try:
        os.unlink(src)
    except BaseException:
        with contextlib.suppress(OSError):  # the old name kept: the new one goes again
            os.unlink(dest)
        raise


def _rename_after_a_look(src: Path, dest: Path) -> None:
    """src renamed dest on a disk that keeps no hard links, once a look at the last moment finds nothing at dest
    (FileExistsError if it does): the narrow window _new_name tells of remains between the two."""
    if os.path.lexists(dest):
        raise FileExistsError(errno.EEXIST, "a file has that name", str(dest))
    os.rename(src, dest)


def _copy_aside(src: Path, dest: Path) -> None:
    """src moved to dest on another disk: copied into a file made new (never one there: FileExistsError, nothing
    changed), its time kept, then removed; a copy cut short, or one whose original can't be removed (in use), goes
    again, so the file is in one place only (download._copy_aside)."""
    made = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)  # never one there
    try:
        with os.fdopen(made, "wb") as out, open(src, "rb") as fh:
            shutil.copyfileobj(fh, out, 1 << 20)
        with contextlib.suppress(OSError):
            st = os.stat(src)
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.unlink(src)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise


def _move(src: Path, dest: Path, rel: str) -> None:
    """src to dest, never deleted and never over another file (_new_name), as download.supersede moves a file aside, on
    the library's own disk where it can, so that a refusal leaves nothing behind. Refused, in words naming rel, for a
    name taken meanwhile, a file in use, or one whose folder's rights don't let it move."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _new_name(src, dest)
    except FileExistsError as e:
        raise Refused(f"{rel} couldn't be moved: another file took the name it was to have, just then. Try again.") from e
    except OSError as e:
        why = ("it is in use: close it and try again" if isinstance(e, PermissionError) and os.name == "nt"
               or e.errno == errno.EBUSY else f"it can't be moved ({e.strerror or e}): see to the rights of its folder")
        raise Refused(f"{rel} couldn't be moved, as {why}.") from e


def _recorded(c, rel: str) -> tuple[str, dict]:
    """The shop whose download recorded the file at rel, and its latest record there; ("", {}) for none: a file of your
    own folders, or one a shop's download never recorded."""
    row = c.execute("SELECT shop, body FROM downloaded WHERE path = ? ORDER BY seq DESC LIMIT 1", (rel,)).fetchone()
    return (row["shop"], json.loads(row["body"])) if row else ("", {})


def _moved_key(rel: str, record: dict) -> str:
    """A shop's file's key among its title's answers, as a download asks for it (curation.moved): moved:<its file>, the
    record's file, else the last part of its path (Humble's, from before records had it)."""
    return f"moved:{record.get('file', PurePosixPath(rel).name)}"


def remove(root: Path, files: list[dict], by: str) -> tuple[list[dict], str]:
    """Each file of files (going) moved into _Removed at its own place below the library, a name taken there getting
    " (2)" and so on (_free), by a move that never writes over a file (_move), and only along a way through _Removed
    with no link or junction on it (_plain_way, looked at before the folders are made and again after); and kept, once
    all have moved, as rows of the table removed, in one change with, for a shop's file, its title's answer that it was
    moved, by by (curation.change_answers, an edit like any other), so that no download fetches it again. Should one
    not move (in use, say), those before it stay moved and kept, and why is said. Should the records not be kept, every
    file moved goes back. What was moved, each with its place below _Removed (held); and "" or why one didn't move."""
    store = curation.store(root)  # another computer's records (db.NotHere): refused before anything moves
    with store.read() as c:
        for f in files:
            f["shop"], f["record"] = _recorded(c, f["path"])
    done, trouble, base = [], "", folder(root)
    for f in files:
        try:
            _plain_way(root, (base / f["path"]).parent)
            dest = _free(base / f["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            _plain_way(root, dest.parent)  # nothing made a link of it meanwhile
            _move(Path(root) / f["path"], dest, f["path"])
        except Refused as e:
            trouble = str(e)
            break
        except OSError as e:  # its folder there couldn't be made
            trouble = f"{f['path']} couldn't be moved ({e.strerror or e}): see to the rights of {FOLDER}."
            break
        done.append({**f, "held": dest.relative_to(base).as_posix()})
    if done:
        try:
            _keep(root, done, by)
        except BaseException:
            for f in reversed(done):  # nothing kept: nothing moved
                with contextlib.suppress(Refused):
                    _move(base / f["held"], Path(root) / f["path"], f["held"])
            raise
    return [{k: f[k] for k in ("path", "held", "size", "title", "t", "shop")} for f in done], trouble


def _keep(root: Path, done: list[dict], by: str) -> None:
    """The rows of the files moved, and the answers of the shops' titles, in one change."""
    now, edits = db.now(), curation.load(root)
    answers: dict[str, tuple[str, dict]] = {}  # by a shop's title's catalog id: its name, and its answers to give
    with curation.store(root).write() as c:
        for f in done:
            shop, record, moved = f["shop"], f["record"], None
            if shop in SHOPS and record.get("item") not in (None, ""):
                cid, key = curation.catalog_id(shop, record["item"]), _moved_key(f["path"], record)
                moved = {"id": cid, "key": key, "was": (edits.fixed.get(cid, {}).get("answers") or {}).get(key)}
                answers.setdefault(cid, (str(record.get("title") or f["t"]), {}))[1][key] = {"a": "moved"}
            REMOVED.insert(c, {"path": f["path"], "held": f["held"], "size": f["size"], "title": f["title"], "t": f["t"],
                               "shop": shop, **({"moved": moved} if moved else {}), "at": now, "state": "removed"})
        for cid, (name, entries) in answers.items():  # within the change above (Database.write)
            curation.change_answers(root, entries, [{"id": cid, "t": name, "f": [[""]]}], by, field="answers")


def _walk(root: Path) -> tuple[list[tuple[str, int]], list[Path], bool]:
    """Every file in _Removed, each by its place below it and its size; the folders it went through, the deepest last;
    and whether there were more files than WALKED_MOST, which aren't in it. A link or a junction is never gone into
    (storage.is_link_stat, as the backup and HyllyCopy tell one): it is a file of its own here, which Empty takes away,
    never what it leads to."""
    base, found, seen, stack = folder(root), [], [], [folder(root)]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:  # not there (none removed yet), or gone a moment
            continue
        seen.append(here)
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) and not storage.is_link_stat(st):
                stack.append(Path(e.path))
                continue
            if len(found) >= WALKED_MOST:
                return found, seen, True
            found.append((Path(e.path).relative_to(base).as_posix(), st.st_size))
    return found, seen, False


def _digest(found: list[tuple[str, int]]) -> str:
    """What _Removed holds, in a few letters: each file's place and size. Empty goes by it, so it deletes only what the
    page showed."""
    return hashlib.sha256(json.dumps(sorted(found), ensure_ascii=False).encode("utf-8")).hexdigest()


def _held_rows(c) -> dict[str, dict]:
    """The removals whose files are in _Removed still, by their place there: the latest of each."""
    return {row["held"]: {**REMOVED.record(row), "seq": row["seq"]}
            for row in c.execute("SELECT * FROM removed WHERE state = 'removed' ORDER BY seq")}


def _when(stamp: str) -> float:
    try:
        return db.moment(stamp).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def listing(root: Path) -> dict:
    """What _Removed holds, for the Server page's Removed part: its files, the latest removed first and those put there
    by hand after them, at most LISTED_MOST, each by its place below _Removed (held), the place in the library it goes
    back to (path: its removal's, else the same as below _Removed), its size, when it was removed (at, "" for one put
    there by hand) and its title's name (t); and of them all, how many (count), their size (bytes), how many were put
    there by hand, with no removal of the Server page's (by_hand), whether there are more than were counted (more), and
    what they are in a few letters (digest), which Empty is given back. Refused when _Removed is a link or a junction."""
    _plain_way(root, folder(root))
    found, _, more = _walk(root)
    with curation.store(root).read() as c:
        rows = _held_rows(c)
    files = [{"held": held, "path": rows[held]["path"] if held in rows else held, "size": size,
              "at": rows[held]["at"] if held in rows else "", "t": rows[held].get("t", "") if held in rows else ""}
             for held, size in found]
    files.sort(key=lambda f: (not f["at"], -_when(f["at"]), f["path"].lower()))
    return {"files": files[:LISTED_MOST], "count": len(found), "bytes": sum(size for _, size in found),
            "by_hand": sum(1 for held, _ in found if held not in rows), "more": more, "digest": _digest(found)}


def put_back(root: Path, held, by: str) -> dict:
    """The file at held below _Removed (as listing gives it) back in its place in the library: the place its removal
    names, else the same place below the library as below _Removed (a file put there by hand); never over another file.
    Refused, in words, nothing changed, for what isn't a file in _Removed (a link neither), a way to it through a link or
    a junction (_plain_way), a place a file may not be put back in (place), or a place taken by another file now. Its
    removal is kept as put back, and the answer it gave a shop's title taken back, as it was before, while that answer
    is still the removal's own. What was done: {path, held}."""
    if not isinstance(held, str) or not paths.inside_library(held) or len(held) > 4096:
        raise Refused("Which file? Its place below _Removed, as the list gives it.")
    src = folder(root) / held
    _plain_way(root, src.parent)
    try:
        st = os.lstat(src)
    except OSError:
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        raise Refused(f"{held} isn't a file in {FOLDER} (put back, or emptied, since?): reload the page.")
    store = curation.store(root)
    with store.read() as c:
        row = _held_rows(c).get(held)
    rel = row["path"] if row else held
    dest = place(root, rel)
    if dest is None:
        raise Refused(f"{rel} isn't a place in the library a file may be put back in (one of Hylly's own folders, or "
                      f"the top of the library): it stays in {FOLDER}.")
    if os.path.lexists(dest):
        raise Refused(f"{rel} is taken: another file is in its place now. Move that one away first, or leave this one in "
                      f"{FOLDER}.")
    _move(src, dest, held)
    try:
        moved = row.get("moved") if row else None
        with store.write() as c:
            if row:
                c.execute("UPDATE removed SET state = 'back', ended_at = ? WHERE seq = ?", (db.now(), row["seq"]))
            if isinstance(moved, dict) and isinstance(moved.get("id"), str) and isinstance(moved.get("key"), str):
                now = (curation.load(root).fixed.get(moved["id"], {}).get("answers") or {}).get(moved["key"])
                if isinstance(now, dict) and now.get("a") == "moved":  # the removal's own still: else a later answer stands
                    curation.change_answers(root, {moved["key"]: moved.get("was")},
                                            [{"id": moved["id"], "t": row.get("t", ""), "f": [[""]]}], by, field="answers")
    except BaseException:
        with contextlib.suppress(Refused):  # nothing kept: it goes back to _Removed
            _move(dest, src, rel)
        raise
    _tidy(root, src.parent)
    return {"path": rel, "held": held}


def _tidy(root: Path, emptied: Path) -> None:
    """The folders of _Removed that a file left empty taken away, from emptied up to _Removed itself: each only if
    empty."""
    base = folder(root)
    while emptied == base or base in emptied.parents:
        try:
            emptied.rmdir()
        except OSError:  # not empty, or not there
            return
        if emptied == base:
            return
        emptied = emptied.parent


def _delete(path: Path) -> None:
    """A file of _Removed deleted for good, a link or a junction as itself (never what it leads to: a junction, or a
    link to a folder on Windows, is taken away as a folder is, which leaves what it leads to), one marked read-only on
    Windows too."""
    try:
        os.unlink(path)
    except PermissionError:
        if os.name != "nt":
            raise
        if storage.is_link(path):
            os.rmdir(path)
            return
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except IsADirectoryError:
        os.rmdir(path)  # a link to a folder, which some systems take away so


def empty(root: Path, digest) -> dict:
    """Every file in _Removed deleted for good, links and junctions as they are (never what they lead to), and then its
    folders, once it holds just what the page showed (digest: listing's, each file's place and size); else refused, in
    words, nothing deleted. Refused too when _Removed, or a folder of it, is a link or a junction (_plain_way). The
    removals of the files deleted are kept as emptied, and a file put there by hand gets one of its own, as removed
    from the same place below the library, so that hylly verify and hylly add still take none of them for one missing.
    What was done: {deleted: how many, bytes: their size, left: those that couldn't be deleted (in use, say), each by
    its place below _Removed, and more: whether there were more than were counted}."""
    if not isinstance(digest, str):
        raise Refused("What did the page show in _Removed?")
    _plain_way(root, folder(root))
    found, seen, more = _walk(root)
    if _digest(found) != digest:
        raise Refused(f"{FOLDER} holds other files now than the page showed: nothing was deleted. Look again, and empty "
                      "it then.")
    store = curation.store(root)  # another computer's records (db.NotHere): refused before anything is deleted
    base, gone, left, freed, plain = folder(root), [], [], 0, set()
    for held, n in found:
        path = base / held
        try:
            if path.parent not in plain:  # nothing made a link of a folder on its way since the look
                _plain_way(root, path.parent)
                plain.add(path.parent)
            _delete(path)
        except FileNotFoundError:
            pass
        except (OSError, Refused):
            left.append(held)
            continue
        gone.append(held)
        freed += n
    if gone:
        now, sizes = db.now(), dict(found)
        with store.write() as c:
            for held in gone:
                emptied = c.execute("UPDATE removed SET state = 'emptied', ended_at = ? WHERE held = ? AND state = 'removed'",
                                    (now, held)).rowcount
                if not emptied and paths.inside_library(held):  # one put there by hand: kept as its own place's removal
                    REMOVED.insert(c, {"path": held, "held": held, "size": sizes.get(held, 0), "title": "", "t": "",
                                       "shop": "", "at": now, "state": "emptied", "ended": now})
    for there in reversed(seen):  # the deepest first, each only if empty: never through a link, which the walk didn't enter
        with contextlib.suppress(OSError):
            there.rmdir()
    return {"deleted": len(gone), "bytes": freed, "left": left, "more": more}


def _same_file(path: Path, record: dict, check_hash: bool) -> bool:
    """Whether the file put into _Removed at path is the file record recorded: its size, and with check_hash its MD5
    where the record has one (read again, as hylly verify --hash reads every file). Its times say nothing: a move keeps
    them on Windows, and on Linux and macOS a change of its details (a share's own notes, its rights) sets them anew."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size != record.get("size"):
        return False
    md5 = str(record.get("md5") or "")
    if not (check_hash and re.fullmatch(r"[0-9a-fA-F]{32}", md5)):
        return True
    found = hashlib.md5(usedforsecurity=False)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                found.update(chunk)
    except OSError:
        return False
    return found.hexdigest() == md5.lower()


def by_you(root: Path, missing: dict[str, tuple[str, dict]], check_hash: bool = False) -> set[str]:
    """Of the recorded files missing from their places in the library (missing: by their place, each with the shop whose
    download recorded it, "" for a copy in your own folders, and its latest record), those you removed: by a removal of
    the Server page's, in _Removed or emptied from it since, made after the file's own record (a file fetched or copied
    in again since, and gone again, is missing); or put into _Removed by hand at their own place, when that file is the
    one recorded (_same_file: its size, and with check_hash its MD5), whenever it went there; or a shop's file whose title
    has your answer that you moved it (curation.moved, as hylly decide's "I moved it by hand" gives it, and a removal
    too). hylly verify takes these for no problem, and hylly add copies none of them in again."""
    if not missing:
        return set()
    base = folder(root)
    plain = not (os.path.lexists(base) and folders.leads_elsewhere(base))  # nothing by hand counts through a link
    with curation.store(root).read() as c:
        last = {row["path"]: (row["removed_at"], row["state"]) for row in
                c.execute("SELECT path, removed_at, state FROM removed ORDER BY seq")}
    answers, out = None, set()
    for rel, (shop, record) in missing.items():
        own = _when(str(record.get("downloaded_at") or record.get("copied_at") or ""))  # 0.0: none, or not a time
        at, state = last.get(rel, ("", ""))
        if state in ("removed", "emptied") and _when(at) >= own:
            out.add(rel)
        elif plain and paths.inside_library(rel) and _same_file(base / rel, record, check_hash):
            out.add(rel)
        elif shop in SHOPS and record.get("item") not in (None, ""):
            answers = curation.answers(root) if answers is None else answers
            if curation.moved(answers.get(curation.catalog_id(shop, record["item"])),
                              record.get("file", PurePosixPath(rel).name)) == "moved":
                out.add(rel)
    return out
