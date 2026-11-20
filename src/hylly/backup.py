"""hylly backup: a second copy of the library on another disk, that can be left running, and read without Hylly.

    TARGET/
      library/            the books as they are, by HyllyCopy (hyllycopy.py): each copy read back and checked, nothing ever
                          deleted, and what a stop leaves goes on from where it was. Plug the disk in, and they can be read.
                          The catalog's covers too: thousands of tiny files that don't change, far too slow to zip each time.
      changed/<day>/      a book's earlier copy, set aside when it changed in the library, so that neither version is lost:
                          only once the new copy is whole and checked, which then takes its name at once; its checksum as
                          it was copied goes into changed/manifest.sha256, which hylly backup check reads too
      saved/<when>.zip    what changes all the time, whole, one zip for each backup: state (the database's nightly copy,
                          with the records, what people have read and the users with their password hashes; the server's
                          nightly copies of its settings; the shops' orders as last fetched, with Humble's order keys;
                          the lookups' answers), curation (the words that suggest tags, and the rules if kept there) and
                          the catalog's data, plus this computer's settings, rules and the owner's themes. All of them
                          stay, unless keep under [backup] in the settings thins them (to_thin), as restic's forget
                          does: only these zips, by their names, and saved/thinned.txt says which went, when and why.
                          Each is whole by itself, so older ones may be deleted by hand too; hylly backup check reads
                          back the newest few (CHECKED_ZIPS), or with --all every one
      HYLLY-BACKUP.txt    how to get it all back, by command or by hand, and what the zips hold, to keep private: by a
                          name of its own, as the target may be the top of a disk, or a folder with a README.txt of
                          someone's own (NOTE)

Books are added and almost never changed, so copying them once is nearly all of it, and the first run is the long one. The
rest is small and always changing, so it is simply taken whole, last, and an older one is there to go back to.

hylly backup TARGET runs one (run), hylly backup check TARGET reads it again (check) and hylly restore BACKUP NEW gets it
back (restore); with --dry-run each only says what it would do (plan, check_plan, restore_plan). Without TARGET, the
backup and its check go to target under [backup] in the settings (target_of), so that a timer runs a plain hylly backup
(examples/hylly-backup.timer). The order of the work is here; cli.py parses the command, holds the backup's lock for the
whole of a run or a restore, the zip and the records too, runs the copy (the copy given to run and restore) and turns
the Outcome into an exit code.

Never in a backup: logins, cookies and API keys, nor the phone's ntfy address and token, which are kept apart from
settings.toml in secrets/ in the settings folder (settings.secrets_dir): the topic is as good as a password for the
alarms, and the ntfy app on the phone shows it again. A settings.toml that still holds them, as an older Hylly's did,
isn't copied at all (no_secrets). The browsers kept logged in are in the database's nightly copy, by a digest only, which
lets no one in. After a disaster you log in again to the shops, and set the address again. So the disk needs no password,
and nothing but a zip tool to read; it isn't encrypted, so password hashes, order keys and the rest should stay at home
with it. On Linux and macOS, on a disk that keeps rights, saved/ and its zips, and the copies of the settings in the
library's state folder, are their owner's only, as the database's copies are: by the mode each is made with, never by
a chmod after, as a disk that keeps modes as extended attributes (Linux's ntfs3) would store one for each, and one that
refuses chmod would stop the backup. Windows' rights are left as they are: a mode there only sets the read-only flag.
Nothing is made on the backup's disk while the mark its books have there is gone (hyllycopy.Side.answers), as an
unmounted disk leaves a folder that would take it all gladly: the books wait for it (HyllyCopy), and so do an earlier
copy's place in changed/, saved/ and its zip, and the note; its folders are made below the target only, never
makedirs's way from the top. A restore's unpacking goes by the new folder's mark the same way.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import sys
import time
import tomllib
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import hyllycopy as hc
from . import paths, rules, runs, settings as the_settings
from .exits import LOCAL, NOT_SET_UP, USAGE, Exit
from .storage import append_line, atomic_write, is_link, read_json, retrying, share_is_gone
from .words import many as _many, size_words  # _many: a loop here has its own many

ZIPPED = ("state", "curation", "catalog")  # the folders of the library that go into the zip, and not among the books
IN_THE_ZIP = "in the zip"  # what is left out of the books for that, as the report lists it
COVERS = "catalog/covers"  # ...but for these, which do go among the books
COVERS_ASIDE = "catalog/covers.old"  # nor the covers a catalog --rebuild set aside (catalog.rebuilding), made again by any build
LOCKS = "state/locks"  # ...nor the jobs' locks and their notes, which are this moment's only (runs.py): one a running job
# holds can't even be read on Windows, and a restored library would carry a note of a job running on the old computer
SETTINGS = ("rules", "themes")  # with settings.toml. By name, so that a new file of logins can't slip in by itself. The
# users are in the database, which goes in by its nightly copy in state
SETTINGS_KEPT = 7  # nightly copies on the library's disk
RESTORING = ".hylly-restoring.json"  # in a restore's folder until it's whole: what lets the same command go on there
ZIP_TRIES = 3
PART_LEFT = 3600.0  # seconds after which a zip's part of the backups' own name, not written to since, is one a drop left
ZIP_WAIT = 60.0  # seconds between goes at the zip, when a share goes away
FRESH_WAIT = 600.0  # seconds the server may take over a fresh copy of the database
RECORDS = "the library's records (tags, corrections, accounts, what was downloaded)"
NOTE = "HYLLY-BACKUP.txt"  # the backup's own note at the top of its disk (README): never README.txt, which may be someone's
FIRST_WORDS = "A backup of a library, made by Hylly."  # how the note starts, as an older backup's README.txt did too
MANIFEST = "manifest.sha256"  # in changed/: each earlier copy's checksum as it was copied, as sha256sum writes them
CHECKED_ZIPS = 3  # the newest zips hylly backup check reads back, unless --all: they're whole each, and many build up
OWN_ZIP = re.compile(r"(\d{4}-\d\d-\d\d \d{6})(?: \((\d+)\))?\.zip")  # a backup's own zip, by the name save_the_rest gives it
THINNED = "thinned.txt"  # in saved/: the zips keep took away, when and why
PERIODS = {"daily": ("day", "days", lambda at: at.date()), "weekly": ("week", "weeks", lambda at: at.isocalendar()[:2]),
           "monthly": ("month", "months", lambda at: (at.year, at.month))}  # keep's counts (settings.BACKUP_KEEPS)


def target_of(given: str | None) -> str:
    """The backup's folder: the one given, else target under [backup] in the settings, so that a timer runs a
    plain hylly backup. NOT_SET_UP when neither names one: a setting missing, which a timer's run tells the phone of,
    as it does a disk that isn't there (a mistyped command line is USAGE, which it doesn't)."""
    target = given or the_settings.get("backup")["target"]
    if not target:
        raise Exit(NOT_SET_UP, f"Which folder is the backup in? Give it, as hylly backup TARGET, or set target = '...' "
                               f"under [backup] in {paths.settings_file()} for every backup (hylly settings edit). Nothing "
                               "was done.")
    return target


def library_of(top) -> Path:
    return Path(top) / "library"


def saved_of(top) -> Path:
    return Path(top) / "saved"


def record_file(root: Path) -> Path:
    return paths.state_dir(root) / "backup.json"


def last(root: Path) -> dict | None:
    """The last backup whose books all went, as the library remembers it, for hylly doctor: "missed" says what its zip
    lacked, if it wasn't whole."""
    found = read_json(record_file(root))
    return found if isinstance(found.get("when"), str) else None


class BackingUp(hc.Copying):
    """HyllyCopy, with what makes it a backup: the zipped folders stay out of the books, and a changed book gets its new
    copy, with the old one set aside instead of being reported and left."""

    NAME = "Hylly backup"
    STOP = "hylly jobs stop backup"
    THEN = ""  # no talk of deleting the originals
    HAS_EVERYTHING = False
    SETS_ASIDE = True

    def __init__(self, root: Path, top, *, new_disk: bool = False, now: datetime | None = None, **kw) -> None:
        super().__init__(root, library_of(top), new_disk=new_disk, **kw)
        self.top = hc.long_path(top)
        self.day = (now or datetime.now()).strftime("%Y-%m-%d")
        self.set_aside: list[str] = []

    def first_words(self) -> str:
        return (f"{hc.shown(str(Path(self.target.given).parent))} has no backup of {hc.shown(self.source.given)} yet: this first one copies all of the "
                "library there (--dry-run shows how much, and the room there).")

    def _leave_out(self, rel: str) -> str | None:
        """What stays out of the books, and why: the zipped folders, Hylly's leftovers at the top, and a file Hylly is
        still writing beside its place (paths.TEMPORARY: a download's .part, a copy's .copying), which would come out
        half made, or change while it's copied and fail the backup, and would stay in it for good. A folder of such a
        name is no such file (asked of only those names, as folders and files alike come here)."""
        if rel.lower().endswith(paths.TEMPORARY) and not os.path.isdir(self.source.path(rel)):
            return "still being downloaded or copied"
        if rel == "catalog" or rel == COVERS or rel.startswith(COVERS + "/"):
            return None
        if rel == COVERS_ASIDE:
            return "the covers a catalog rebuild set aside"
        if rel.split("/")[0] in ZIPPED:
            return IN_THE_ZIP
        return "Hylly's leftovers" if "/" not in rel and rel.startswith(("_", ".")) else None

    def _left_out_words(self, n: int, what: str) -> str:
        """What goes into the zip is left out of the books, and no loss: the plan says where it goes."""
        if what == IN_THE_ZIP:
            return f"{_many(n, 'file or folder', 'files and folders')} {'goes' if n == 1 else 'go'} into the zip instead"
        return super()._left_out_words(n, what)

    def looks_short(self, result: hc.Result) -> str:
        """Most of what was backed up isn't in the library now: nothing is lost here, but it's something to know about, soon."""
        now, known = len(result.listing.files), len(self.record.entries)
        if hc.lost_most(known, now):
            return (f"The library has {_many(now, 'file')} now, and this backup holds {known:,}. Is all of it there? "
                    "Nothing was deleted from the backup.")
        return ""

    def _set_aside(self, rel: str, dst: str, name_it: Callable[[], None]) -> None:
        """The earlier copy to changed/<day>/, in another name if it has changed twice today, and the new one, whole and
        checked by now, in its place by name_it: one right after the other, so that a stop between them leaves the earlier
        copy in changed/ and the new one whole in the parts, where the next run takes it. If the new one can't take the
        name, the earlier one is put back. Once it stays in changed/, its checksum as it was copied, by the record, goes
        into changed/manifest.sha256 (noted): the record forgets it, as library/ has the new one by that name."""
        entry = self.record.entries.get(rel)
        old = entry["h"] if entry else self._read_whole(self.target, dst)[0].hex()  # a record lost: as it is
        folder, name = os.path.split(os.path.join(self.top, "changed", self.day, *rel.split("/")))
        stem, dot, ending = name.rpartition(".")
        where, n = os.path.join(folder, name), 1
        while os.path.lexists(where):
            n += 1
            where = os.path.join(folder, f"{stem} ({n}).{ending}" if dot and stem else f"{name} ({n})")
        last = (0, 0)
        while True:
            try:
                if not self.target.answers():  # the place an unmounted disk leaves would get changed/<day>/
                    self._hold(self.target, None)
                self.target.call(hc._make_below, self.top, os.path.relpath(folder, self.top).replace(os.sep, "/"))
                self.target.call(os.replace, dst, where)
                break
            except hc._Trouble as t:
                if isinstance(t.error, FileNotFoundError) and self.target.answers() and self._look(self.target, dst) is None:
                    if os.path.lexists(where):
                        break  # it went there, with the answer lost as the share went away
                    return name_it()  # it went meanwhile: nothing is in the new one's way
                self._wait_out(t)  # or it's this file's problem, and the earlier copy stays where it is
                last = self._another_go(t.error, 0, last)
        try:
            name_it()
        except hc.FileProblem:
            try:
                self.target.call(hc._no_overwrite_name, where, dst)
            except hc._Trouble:
                self.record.forget(rel)  # it has left, and nothing took its place
                self.set_aside.append(rel)
                self._noted(where, old)
                self.say(f"  {hc.shown(rel)}: its earlier copy is in changed/{self.day}, and the new one couldn't take its place.")
            raise
        self.set_aside.append(rel)
        self._noted(where, old)
        self.say(f"  {hc.shown(rel)} has changed in the library: its earlier copy is now in changed/{self.day}.")

    def _noted(self, where: str, sha: str) -> None:
        """An earlier copy's checksum, one line at the end of changed/manifest.sha256, by its path in changed/, as any
        sha256sum reads it there. A moment's trouble is waited out; lasting trouble only said, as the copy is set aside."""
        changed = os.path.join(self.top, "changed")
        line = hc._manifest_line(os.path.relpath(where, changed).replace(os.sep, "/"), sha)

        def add() -> None:
            with open(os.path.join(changed, MANIFEST), "a+b", buffering=0) as fh:
                end = fh.seek(0, os.SEEK_END)
                fh.seek(max(end - 1, 0))
                hc._write_all(fh, line if not end or fh.read(1) == b"\n" else b"\n" + line)  # after a line cut short
                hc._keep_to_disk(fh)

        p = self._keep_at_it(add)
        if p is not None:
            self.say(f"  Its checksum couldn't be noted in changed/{MANIFEST} ({p.why}), so hylly backup check can't "
                     "read it again.")


# ---- what goes into the zip -----------------------------------------------------------------------------------------------

def themes_folder() -> Path:
    """The owner's own themes, where themes.owners_dir() keeps them: that's the server's, which a copy may not import."""
    return paths.config_dir() / "themes"


def _settings_to_save() -> tuple[list[tuple[Path, str]], list[str]]:
    """This computer's settings, by name only, each with its name in a copy: settings.toml (a settings file elsewhere
    goes by that name), the rules where Hylly reads them (rules.folder(): beside settings.toml, or wherever rules = or
    HYLLY_RULES puts them, a folder or one file), and the owner's themes: of Hylly's own folders by kind (paths.PLACES),
    only these of the settings folder. The database goes in by its fresh copy in the library (fresh_records), and
    nothing of the state folder (the logs, the locks and notes, what long jobs said), the cache or the logins ever
    does. Of a folder only its .toml files, as Hylly reads them, and no links, which may lead anywhere. Never, by those
    roads, settings.toml itself, nor a file Hylly's own folders keep for themselves, among the logins, the server's
    secrets, what long jobs said or the cache. Also returns what couldn't be looked at, in words."""
    settings, found, missed = paths.settings_file(), [], []

    def real(path) -> str:
        return os.path.normcase(os.path.realpath(path))

    own = {*(real(paths.place(kind)) for kind in paths.PLACES), real(settings.parent)}
    inside = real(paths.config_dir()) + os.sep  # all in one, the state folder is the settings folder: not apart, then
    apart = tuple(real(path) + os.sep for path in (paths.logins_dir(), the_settings.secrets_dir(), paths.place("cache"),
                                                   paths.place("state"), paths.job_output_dir())
                  if not inside.startswith(real(path) + os.sep))

    def may(path: Path) -> bool:
        at = real(path)
        return at != real(settings) and os.path.dirname(at) not in own and not at.startswith(apart)

    if settings.is_file():
        found.append((settings, "settings.toml"))
    try:
        mine = rules.folder()
    except Exit as e:  # settings.toml that can't be read: nothing then says where the rules are
        missed.append(f"rules: {e}")
        mine = None
    for name, where in zip(SETTINGS, (mine, themes_folder())):
        try:
            if where is None:
                continue
            if where.is_dir():
                files = [p for p in (rules._tomls(where) if name == "rules" else sorted(where.glob("*.toml")))
                         if p.is_file() and not _a_link(str(p))]
            elif where.is_file() and name == "rules":  # rules = names one file
                files = [where]
            else:
                if name == "rules" and paths.setting("rules", "HYLLY_RULES"):
                    missed.append(f"rules, from {where}: there's no such file or folder (rules = in {settings}, or HYLLY_RULES)")
                continue
        except OSError as e:
            missed.append(f"{name}, from {where}: {e.strerror or e}")
            continue
        found += [(p, f"{name}/{p.name}") for p in files if may(p)]
        if any(not may(p) for p in files):
            missed.append(f"{name}, from {where}: the files of Hylly's own folders and the logins never go in a copy, so "
                          f"keep the {name} in a folder of their own")
    return found, missed


def what_of_the_settings() -> list[str]:
    """For a plan: what the zip will take of this computer's settings, and from where."""
    found, missed = _settings_to_save()
    bits = ["settings.toml" for _, inside in found if inside == "settings.toml"]
    for name, one, many in (("rules", "rules file", "rules files"), ("themes", "theme of your own", "themes of your own")):
        files = [path for path, inside in found if inside.startswith(f"{name}/")]
        if files:
            bits.append(f"{_many(len(files), one, many)} from {files[0].parent}")
    return ([f"Of this computer's settings, it takes {'; '.join(bits) if bits else 'nothing: there are none here'}."]
            + [f"  Can't be taken: {line}" for line in missed])


def no_secrets(raw: bytes, where: str = "settings.toml") -> bytes:
    """settings.toml as a copy holds it: as it is, as it holds no secrets. One that still sets the alarms' ntfy address
    or token under [bell], where an older Hylly kept them before they had files of their own (settings.old_secrets),
    isn't copied rather than copied with them in it; nor is one that can't be read as TOML, as nothing then says what
    it holds. An empty one is no secret: the server's nightly copies made before have ntfy = ""  # left out of this
    copy, as the old rewriter left them. OSError if so."""
    try:
        found = tomllib.loads(raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8"))  # Notepad's start
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise OSError(f"{where} can't be read as TOML ({e}), so it wasn't copied: put it right, and it's copied next "
                      "time") from None
    bell = found.get("bell")
    if isinstance(bell, dict) and the_settings.old_secrets(bell):
        raise OSError(f"{where} still has the alarms' ntfy address or token under [bell], so it wasn't copied: put them "
                      "in secrets/ntfy-url and secrets/ntfy-token in the settings folder, take the lines out, and it's "
                      "copied next time")
    return raw


def _private_folder(folder: Path) -> None:
    """A folder for copies of the settings or the library's records: on Linux and macOS its owner's only, where the
    disk keeps rights (a share may not), as the database's copies are, by the mode it's made with. One there already
    is left as it is, as a chmod on the library's disk may be stored as extended attributes, or refused: hylly doctor
    says when others can read what's in it. On Windows a mode only sets the read-only flag."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)


def _private_file(path: Path):
    """A file written new, on Linux and macOS its owner's only where the disk keeps rights: the mode is given as it's
    made, so it never fails."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600), "wb")


def _a_link(path: str) -> bool:
    """A link or a Windows junction, as storage.is_link tells them for HyllyCopy too: never followed into the zip. One
    that can't be looked at is no link: reading it says what's wrong."""
    try:
        return is_link(path)
    except OSError:
        return False


def _walk(folder: str, base: str, missed: list[str] | None = None):
    """Every file below a folder of the library at base, its links and junctions left out, as they may lead anywhere,
    and the covers, which go among the books, and the jobs' locks (LOCKS). A folder that can't be read goes into
    missed, not quietly out of the zip; one that went while it was read has gone. Without missed (a guess at the
    size), it's passed over."""
    def unreadable(e: OSError) -> None:
        if isinstance(e, FileNotFoundError) or missed is None:
            return
        if share_is_gone(e):
            raise e
        missed.append(f"{os.path.relpath(e.filename or folder, base).replace(os.sep, '/')}/: {e.strerror or e}")

    apart = {os.path.join(base, *rel.split("/")) for rel in (COVERS, COVERS_ASIDE, LOCKS)}
    for here, folders, files in os.walk(folder, onerror=unreadable):
        folders[:] = [f for f in folders if os.path.join(here, f) not in apart and not _a_link(os.path.join(here, f))]
        for name in files:
            path = os.path.join(here, name)
            if not _a_link(path):
                yield path


def _write_zip(root: Path, part: Path, night: str | None = None) -> list[str]:
    """Written whole; what is lost is lost. A file that goes while it's being read has gone, and a journal being added to
    is a beginning of itself, which is all a crash would have left. Of the database's nightly copies only night's, the
    day's folder fresh_records named; without one, the newest by its name, whatever today is."""
    missed = []
    base = hc.long_path(root)
    from . import db

    newest = night or _newest_night(root)
    nights = os.path.relpath(db.backups(root), root).replace(os.sep, "/") + "/"
    copies = os.path.relpath(settings_backups(root), root).replace(os.sep, "/") + "/"

    def settings_copy(path, inside: str, where: Path) -> None:  # never with the phone's topic: a nightly copy's either
        info = zipfile.ZipInfo.from_file(path, inside, strict_timestamps=False)
        z.writestr(info, no_secrets(Path(path).read_bytes(), str(where)), zipfile.ZIP_DEFLATED, 1)

    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True, strict_timestamps=False) as z:
        for name in ZIPPED:
            for path in _walk(os.path.join(base, name), base, missed):
                inside = os.path.relpath(path, base).replace(os.sep, "/")
                if inside.endswith((".part", ".tmp")):
                    continue
                if inside.startswith(nights) and not (newest and inside.startswith(f"{nights}{newest}/")):
                    continue  # the database's nightly copies: the newest is enough, as each holds the whole catalog
                try:
                    if inside.startswith(copies) and inside.count("/") == 3 and inside.endswith("/settings.toml"):
                        settings_copy(path, inside, root / inside)  # named as written, without the long path's \\?\
                    else:
                        z.write(path, inside)
                except FileNotFoundError:
                    continue
                except OSError as e:
                    if share_is_gone(e):
                        raise
                    missed.append(f"{inside}: {e.strerror or e}")
        found, unseen = _settings_to_save()
        missed += [f"settings/{line}" for line in unseen]
        for path, inside in found:
            try:
                if inside == "settings.toml":
                    settings_copy(path, f"settings/{inside}", path)
                else:
                    z.write(path, f"settings/{inside}")
            except OSError as e:
                missed.append(f"settings/{inside}: {e.strerror or e}")
    return missed


def _newest_night(root: Path) -> str | None:
    """The day of the newest copy of the library's records in state/db-backup, by its name, up to db.AHEAD_DAYS after
    this computer's day: a server in another time zone may have dated tonight's copy tomorrow, but a copy dated
    further ahead was made under a clock that was wrong, and would be taken for ever (passed_over)."""
    from . import db

    newest = db.last_backup(root, ahead=True)
    return newest[0] if newest else None


def passed_over(root: Path) -> str:
    """Words for the copies of the library's records dated more than db.AHEAD_DAYS after this computer's day, which a
    backup and a restore never take; "" when there are none."""
    from . import db

    ahead = db.dated_ahead(root)
    if not ahead:
        return ""
    return (f"Passed over: {_many(len(ahead), 'copy', 'copies')} of {RECORDS} in {db.backups(root)} dated more than "
            f"{db.AHEAD_DAYS} days after this computer's day ({', '.join(p.name for p in ahead[:3])}"
            f"{', …' if len(ahead) > 3 else ''}): made under a clock that was wrong, they're never taken. Move them out "
            "of that folder when they aren't wanted.")


def fresh_records(root: Path, now: datetime | None = None) -> tuple[str | None, str | None]:
    """Before the zip: the database's copy for today made again, so that the zip has the library's records as they are,
    not as they were last night. Made here when this computer's database keeps them (db.own), or by the server when
    another computer's does and [remote] ssh names it. Returns the day of the copy the zip takes, the fresh one by the
    day it was made under (the server's, which may be tomorrow here), else the newest by its name; and None when that's
    the fresh one, or no database keeps the library's records yet, otherwise the words of why not, and of the older
    copy the zip has instead."""
    from . import db, remote

    # a server named wrong (HYLLY_REMOTE set and empty, say) stops it, as elsewhere, when it's asked: not swallowed below
    server = remote.server() if needs_the_server(root) else None
    try:
        marker = db.owner(root)
    except FileNotFoundError:
        return _newest_night(root), None  # no database has taken on the library's records: nothing tells copies apart
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        marker = {}
    try:
        if db.file().is_file():
            try:
                db.own(db.database(), root)
            except db.NotHere:
                pass
            else:
                made = db.nightly(root, now, fresh=True)
                return (made.name if made else _newest_night(root)), None
        if server is not None:
            said = remote.ask(["internal", "fresh-copy"], timeout=FRESH_WAIT)  # "Copied: .../db-backup/<day>."
            day = re.search(r"(\d{4}-\d\d-\d\d)\W*$", said.strip())
            ours = {p.name for p in db.nights(root, db.ANY_DAY)}
            return (day[1] if day and day[1] in ours else _newest_night(root)), None
        computer = marker.get("computer")
        if computer == socket.gethostname():
            why = f"this computer's database ({db.file()}) doesn't keep them"
        else:
            computer = computer if isinstance(computer, str) and db.COMPUTER.fullmatch(computer) else "another computer"
            why = f"they are kept by {computer}, and this computer's settings have no [remote] ssh to ask it for a fresh copy"
    except (OSError, sqlite3.Error, db.Newer, db.Unusable, db.NotHere, Exit) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        why = f"a fresh copy couldn't be made ({e})"
    newest = db.last_backup(root, ahead=True)
    if newest is None:
        return None, f"Not in the zip: {RECORDS}: {why}, and there's no earlier copy of them in {db.backups(root)}."
    day, made = newest[0], newest[1].get("made")
    when = made[:16].replace("T", " ") if isinstance(made, str) and made.startswith(day) else day
    return day, f"In the zip, {RECORDS} are their copy of {when}, without what changed since: {why}."


def needs_the_server(root: Path) -> bool:
    """Whether the library's records are kept by another computer's database, which a backup asks for its fresh copy
    (fresh_records): only then does a server named wrong, an empty HYLLY_REMOTE say, matter to a backup. One made on the
    computer that keeps them needs none."""
    from . import db

    try:
        if not db.owner_file(root).is_file():
            return False  # no database has taken the records on: there's no fresh copy to ask for
        if not db.file().is_file():
            return True
        db.own(db.database(), root)
    except db.NotHere:
        return True
    except (OSError, sqlite3.Error, db.Newer, db.Unusable):  # this computer's own trouble, which fresh_records tells of
        return False
    return False


NOT_THERE = ("the disk isn't answering: the mark HyllyCopy left there is gone, as when the disk isn't mounted, so nothing "
             "more was written there")
ZIP_DAMAGE = (EOFError, zipfile.BadZipFile, zlib.error)  # a zip cut short or damaged: a damaged stream can stop zlib
# itself, before any check finds a file in it wrong


def _read_back(path: Path) -> tuple[str | None, Exception | None]:
    """A zip read back whole, each file in it against its checksum, when it's made, at a check and before a restore:
    (the first file in it that isn't as it was written, or None; what was said of it cut short or damaged, ZIP_DAMAGE,
    or None). Trouble reading it at all (OSError, as a disk that went away) is raised as it is, for each to deal with
    in its own way: the zip is made again, a check lists it, a restore tells it, or stops at a share gone."""
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip(), None
    except ZIP_DAMAGE as e:
        return None, e


def save_the_rest(root: Path, top, *, now: datetime | None = None, sleep=time.sleep,
                  night: str | None = None, there: Callable[[], bool] | None = None) -> tuple[Path, list[str]]:
    """The zip for this backup, under another name until it's complete and has been read back, with night's copy of the
    library's records (fresh_records). Returns it and what couldn't be read. On Linux and macOS, where the disk keeps
    rights, the zips and their folder are their owner's only, by the mode they're made with: they hold password hashes.
    there: whether the backup's disk is still the one its books went to (the copy's Side.answers), asked before saved/
    is made, which is never made above the target (hyllycopy._make_below), and again before the zip takes its name: a
    zip made where an unmounted disk was is removed, as it holds the password hashes, and the next go, a minute later,
    asks again."""
    folder = saved_of(top)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H%M%S")
    error: OSError | None = None
    for attempt in range(ZIP_TRIES):
        if attempt:
            sleep(ZIP_WAIT)
        part = None
        try:
            if there is not None and not there():
                raise OSError(NOT_THERE)
            hc._make_below(hc.long_path(top), "saved", 0o700)
            _parts_left(folder)
            final, n = folder / f"{stamp}.zip", 1
            while final.exists():  # never over an earlier one
                n += 1
                final = folder / f"{stamp} ({n}).zip"
            part = final.with_name(final.name + ".part")
            part.unlink(missing_ok=True)
            _private_file(part).close()  # made with its mode, which writing the zip into it keeps
            missed = _write_zip(root, part, night)
            bad, damaged = _read_back(part)
            if damaged is not None:  # damaged on its way to the disk: made again, as below
                raise OSError(f"it read back damaged ({damaged})")
            if bad:
                raise OSError(f"{bad} read back differently")
            with open(part, "rb+") as fh:
                os.fsync(fh.fileno())
            if there is not None and not there():
                raise OSError(NOT_THERE)
            os.replace(part, final)
            return final, missed
        except OSError as e:
            error = e
            if part is not None:
                with contextlib.suppress(OSError):
                    part.unlink(missing_ok=True)
    raise error if error else OSError("the zip couldn't be made")


def _parts_left(folder: Path) -> None:
    """A zip's part that a drop or a stop left in saved/, removed: only of the backups' own name (OWN_ZIP), and only
    once nothing has written to it for PART_LEFT, as one another computer's backup to this disk is writing is not."""
    with contextlib.suppress(OSError):
        for path in folder.glob("*.zip.part"):
            with contextlib.suppress(OSError):
                if OWN_ZIP.fullmatch(path.name[:-len(".part")]) and time.time() - path.stat().st_mtime > PART_LEFT:
                    path.unlink()


def saved_zips(top) -> list[Path]:
    """The oldest first: by name, which is when, and a second one in the same second after the first."""
    def order(path: Path) -> tuple[str, int]:
        m = re.fullmatch(r"(.*?)(?: \((\d+)\))?\.zip", path.name)
        return (m[1], int(m[2] or 1)) if m else (path.name, 0)

    return sorted((p for p in saved_of(top).glob("*.zip") if p.is_file()), key=order)


def _made_at(path: Path) -> datetime | None:
    """When a backup's own zip was made, by its name (OWN_ZIP); None for any other file."""
    found = OWN_ZIP.fullmatch(path.name)
    if found:
        with contextlib.suppress(ValueError):
            return datetime.strptime(found[1], "%Y-%m-%d %H%M%S")
    return None


def to_thin(top, keep: dict, coming: datetime | None = None, made: Path | None = None) -> list[Path]:
    """The zips keep (under [backup]: daily, weekly and monthly counts) takes away, oldest first, as restic's forget
    does: going from the newest, a zip stays as the newest of each of the latest days, weeks and months that have one,
    as many of each as keep says, and the oldest too while a count has some left; the rest go. Only the backups' own
    zips, by their names (OWN_ZIP): another file is never touched, nor counted. Never the newest; none when keep is
    {}, as it comes. made: the zip this backup has just made, and coming: the one a plan's backup would make, each the
    newest of all whatever the others are named, so that one named ahead by a clock that was wrong never takes its
    place, and the plan and the run agree."""
    if not any(keep.values()):
        return []
    own: list[tuple[Path | None, datetime]] = [(p, at) for p in saved_zips(top)
                                               if (made is None or p.name != made.name) and (at := _made_at(p))]
    if made is not None:
        own.append((made, _made_at(made) or datetime.now()))
    if coming is not None:
        own.append((None, coming))
    left, last, gone = {kind: keep.get(kind, 0) for kind in PERIODS}, {}, []
    for n, (path, at) in enumerate(reversed(own)):
        stays = n == 0
        for kind, (_, _, period) in PERIODS.items():
            if left[kind] > 0 and (period(at) != last.get(kind) or n == len(own) - 1):
                last[kind], left[kind], stays = period(at), left[kind] - 1, True
        if not stays and path is not None and path != made:
            gone.append(path)
    return gone[::-1]


def keep_words(keep: dict) -> str:
    """What keep says, in words, as the output and thinned.txt give it."""
    written = ", ".join(f"{kind} = {keep[kind]}" for kind in PERIODS if keep.get(kind))
    each = [f"the latest {one}" if keep[kind] == 1 else f"each of the {keep[kind]} latest {many}"
            for kind, (one, many, _) in PERIODS.items() if keep.get(kind)]
    return (f"keep = {{ {written} }} under [backup] keeps the newest zip of "
            f"{', '.join(each[:-1]) + ' and ' + each[-1] if len(each) > 1 else each[0]}, counting only those with a zip")


def thin(top, keep: dict, made: Path | None = None) -> tuple[list[str], list[str]]:
    """The zips keep takes away (to_thin, made the newest), removed, each written down in saved/thinned.txt with when
    and why. Returns the lines that say so, and those of what couldn't be removed: neither stops the backup, whose zip
    is made."""
    from . import db

    gone, said, failed = to_thin(top, keep, made=made), [], []
    if not gone:
        return [], []
    why, size = keep_words(keep), 0
    for path in gone:
        try:
            bytes_ = path.stat().st_size
            path.unlink()
        except OSError as e:
            failed.append(f"  {path.name} couldn't be removed: {e.strerror or e}")
            continue
        size += bytes_
        said.append(f"  {path.name}")
        with contextlib.suppress(OSError):  # a zip gone is gone: the line is its record, not a reason to stop
            append_line(saved_of(top) / THINNED, f"{db.now()}  {path.name} went, as {why}.")
    if said:
        said.insert(0, f"Thinned: {_many(len(said), 'older zip')} went, {size_words(size)}, as {why}:")
    return said, failed


def to_check(top, every_zip: bool = False) -> tuple[list[Path], int]:
    """The zips hylly backup check reads back, the newest CHECKED_ZIPS of them, or every_zip (--all) every one, and how
    many there are: each holds the whole of what it took, so the newest are what a restore takes, and reading back
    every one of hundreds would make each check longer than the last."""
    zips = saved_zips(top)
    return (zips if every_zip else zips[-CHECKED_ZIPS:]), len(zips)


def check_saved(top, every_zip: bool = False) -> tuple[int, list[str]]:
    """The zips read back (to_check). Returns how many were read and which have something wrong."""
    wrong = []
    zips, _ = to_check(top, every_zip)
    for path in zips:
        try:
            bad, damaged = _read_back(path)
        except OSError as e:
            bad, damaged = None, e
        if damaged is not None:
            wrong.append(f"{path.name}: {damaged}")
        elif bad:
            wrong.append(f"{path.name}: {bad} isn't as it was")
    return len(zips), wrong


README = FIRST_WORDS + """ {last}

library/     The books, as they are in the library. These are plain files: any of them can be opened from here.
changed/     A book's earlier copy, by the day it was set aside, when the book changed in the library. Nothing is ever
             deleted here. manifest.sha256 has the checksum of each as it was copied: sha256sum -c manifest.sha256, in
             changed/, checks them.
saved/       One zip for each backup: the library's state, curation and catalog folders, and the settings. The newest is
             the latest; older ones are there to go back to, unless keep under [backup] in the settings thins them:
             thinned.txt beside them says which went, when and why. Each zip is whole by itself, so older ones may also
             be deleted by hand, as the disk fills up.

Keep this disk private, at home or somewhere you trust: it isn't encrypted, and the zips hold what no one else should
read. Their state folder has the library's records, with everyone's reading (where each person is in each title, their
lists, bookmarks and reviews) and the users' password hashes, and the shops' orders, with Humble's order keys, which
open your Humble download pages. The library's own state folder holds the same: keep it, and the share it's on,
private too.

Not here, on purpose: logins, cookies, API keys and sessions, nor the phone's ntfy address and token, which the
settings folder keeps in its secrets folder. You'll log in to the shops again, and put the address back in
secrets/ntfy-url in the settings folder (the ntfy app on your phone shows it).

Where Hylly keeps its own files on a computer (hylly settings places says it there): all in one folder for an install
that has them so, %LOCALAPPDATA%\\Hylly on Windows, ~/.local/share/Hylly on Linux and macOS, or the folder that
HYLLY_HOME names. Otherwise by kind: the settings folder, with settings.toml, the rules, the themes and secrets/, is
%APPDATA%\\Hylly on Windows, ~/.config/hylly on Linux, ~/Library/Application Support/Hylly on macOS; the data folder,
with hylly.db, is %LOCALAPPDATA%\\Hylly\\data on Windows, ~/.local/share/hylly on Linux, and on macOS the same as the
settings folder.

To get it all back, with the program installed:

    hylly restore "{top}" NEW-FOLDER --dry-run         (shows what it would do)
    hylly restore "{top}" NEW-FOLDER

It also makes the library's records (tags, corrections, accounts, what was downloaded) this computer's database, if it
has none yet, and then says what's left to do: the settings are in NEW-FOLDER/_settings-from-backup, to copy to the
settings folder of a new computer, and the library setting to point at NEW-FOLDER.

Or by hand, with no program: copy library/ somewhere, then unzip the newest zip from saved/ into that same folder. The
library's records are then in its state/db-backup/<newest day>/hylly.db: copy that to the data folder, if it has no
hylly.db yet, and run hylly server take-over --library <that folder>, which asks first and ties the records to this
install (its records-token): without it they are refused here, as to a copy.

To check that nothing here has changed since it was copied: hylly backup check "{top}"
"""


NOTE_KEYS = ("when", "target", "computer", "files", "bytes", "saved", "set_aside")
NOTE_MAY = ("missed",)  # only in the note of a backup that isn't whole: what its zip lacks, or has older


def finish(root: Path, top, result: hc.Result, made: Path, set_aside: int, missed: list[str] | None = None,
           there: Callable[[], bool] | None = None) -> None:
    """What the disk says about itself (NOTE), and what the library remembers of it. missed: what the zip lacks, or has
    only older, in words; then neither says it's a finished backup, and hylly doctor tells. A library this computer may
    only read (its share read-only, as the server owns its data) gets the note through the server (remote.ask). there:
    as for save_the_rest; the note is written only while the disk answers, in a folder that has to be there."""
    from . import db

    when = db.now()
    last = (f"Last finished: {when}." if not missed else
            f"Last made: {when}, and NOT WHOLE: its zip lacks this, or has it only older:\n"
            + "".join(f"  - {line}\n" for line in missed)
            + "Once that's put right, back up again: the books won't be copied twice.")
    note_on_the_disk(top, README.format(last=last, top=top), there)
    note = {"when": when, "target": str(top), "computer": socket.gethostname(), "files": len(result.listing.files),
            "bytes": result.size, "saved": made.name, "set_aside": set_aside, **({"missed": list(missed)} if missed else {})}
    try:
        keep_note(root, note)
    except OSError:
        from . import remote

        if remote.server() is None:
            raise
        remote.ask(["internal", "backup-noted", str(top)], json.dumps(note))


def note_on_the_disk(top, text: str, there: Callable[[], bool] | None = None) -> None:
    """The backup's own note at the top of its disk, NOTE, whole or not at all. A README.txt there is someone's, and is
    left alone; but one an older backup wrote, which starts with the note's own first words, goes once the note is
    there under its own name. OSError when the disk doesn't answer (there), with nothing written; a moment's trouble is
    tried again, as atomic_write does."""
    base = hc.long_path(top)

    def write() -> None:
        if there is not None and not there():
            raise OSError(f"the backup's own note, {NOTE}, wasn't written: {NOT_THERE}")
        hc._write_here(os.path.join(base, NOTE), text)

    retrying(write)
    older = os.path.join(base, "README.txt")
    with contextlib.suppress(OSError):
        if not _a_link(older) and os.path.isfile(older):
            with open(older, "rb") as fh:
                ours = fh.read(len(FIRST_WORDS.encode())) == FIRST_WORDS.encode()
            if ours:
                os.unlink(older)


def check_note(note) -> None:
    """ValueError unless it's a backup's note, as finish makes it, and nothing besides."""
    if (not isinstance(note, dict) or not set(NOTE_KEYS) <= set(note) <= {*NOTE_KEYS, *NOTE_MAY}
            or not isinstance(note.get("when"), str)
            or not isinstance(note.get("missed", []), list) or not all(isinstance(m, str) for m in note.get("missed", []))):
        raise ValueError("not a backup's note")


def keep_note(root: Path, note: dict) -> None:
    """A backup's note in the library's records: what hylly doctor and the next backup read."""
    check_note(note)
    atomic_write(record_file(root), json.dumps(note, indent=1))


# ---- the server's own settings, which only the server's computer can reach ---------------------------------------------------

def settings_backups(root: Path) -> Path:
    return paths.state_dir(root) / "settings-backup"


def settings_tonight(root: Path, now: datetime | None = None) -> Path | None:
    """Today's copy of the settings on the library's disk, where a backup run on another computer finds it. As with
    people's data, the server does this every night. By name, as the zip takes them (_settings_to_save): never the
    logins, keys, sessions or the server's secrets, and settings.toml only while it holds none (no_secrets). On Linux and
    macOS, where the disk keeps rights, the copies are their owner's only, as the database's are; the folder too, which
    keeps those made before from others as well. OSError when something couldn't be taken: the rules not found, say,
    after the copy is made of the rest."""
    if paths.library_missing(root):
        return None
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    target = settings_backups(root) / day
    if target.is_dir():
        return None
    found, missed = _settings_to_save()
    copies = [(inside, no_secrets(path.read_bytes(), str(path)) if inside == "settings.toml" else path.read_bytes())
              for path, inside in found]  # all of it read first: one that can't be copied safely leaves no copy at all
    if copies:
        _private_folder(settings_backups(root))
        part = target.with_name(f"{day}.part")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(mode=0o700, exist_ok=True)
        for inside, data in copies:
            to = part / inside
            to.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _private_file(to) as fh:
                fh.write(data)
        part.rename(target)
        for old in sorted(p for p in settings_backups(root).iterdir() if p.is_dir() and not p.name.endswith(".part"))[:-SETTINGS_KEPT]:
            shutil.rmtree(old, ignore_errors=True)
    if missed:
        raise OSError(f"not all of them: {'; '.join(missed)}")
    return target if copies else None


# ---- getting it back ----------------------------------------------------------------------------------------------------------

def choose_zip(top, name: str | None) -> Path:
    zips = saved_zips(top)
    if not zips:
        raise Exit(NOT_SET_UP, f"No zip in {saved_of(top)}: is this a backup, and did one finish?")
    if not name:
        return zips[-1]
    found = [p for p in zips if p.name == name or p.stem == name or p.name.startswith(name)]
    if len(found) != 1:
        raise Exit(USAGE, f"--saved {name}: {'more than one zip goes by that' if found else 'no such zip'}. The zips: {', '.join(p.name for p in zips[-5:])}.")
    return found[0]


def unpack(made: Path, to: Path, there: Callable[[], bool] | None = None) -> tuple[int, int]:
    """The zip into the new library: its settings to one side, as they belong to a computer, not to a library. Only ever
    new files, under another name until whole. Those of state and the settings, with password hashes and order keys,
    are their owner's only on Linux and macOS, where the disk keeps rights, by the mode they're made with. Its folders
    are made below the new library only (hyllycopy._make_below). there: whether the new library's disk is still the
    one the books went to (the restore's copy's Side.answers), asked before each file; without it, the mark the copy
    left there, as it is now, must stay, and one that can't be read is a disk gone. While it doesn't answer, nothing
    more is written: Exit, and the same restore goes on once it's back. Returns how many were written and how many
    were there already."""
    written = kept = 0
    gone = Exit(LOCAL, f"The books are copied, but {to} isn't answering, so {made.name} wasn't unpacked there, or not all of "
                       f"it: {NOT_THERE}. Once the disk is back, the same command goes on, and the books aren't copied twice.")
    if there is None:
        mark_at = os.path.join(hc.long_path(to), hc.FOLDER, "id")
        try:
            mark = hc._read_mark(mark_at)
        except OSError:
            raise gone from None

        def there() -> bool:
            try:
                return hc._read_mark(mark_at) == mark
            except OSError:
                return False

    with zipfile.ZipFile(made) as z:
        for info in z.infolist():
            parts = info.filename.split("/")
            if info.is_dir() or info.filename.startswith("/") or ".." in parts or ":" in info.filename or "\\" in info.filename:
                continue
            if parts[0] == "settings":
                parts = ["_settings-from-backup", *parts[1:]]
            elif parts[0] not in ZIPPED or "/".join(parts[:2]) == LOCKS:  # those of a zip made before they were left out
                continue
            path = to.joinpath(*parts)
            if not there():
                raise gone
            if os.path.lexists(path):
                kept += 1
                continue
            if len(parts) > 1:
                hc._make_below(str(to), "/".join(parts[:-1]))
            part = path.with_name(path.name + ".part")
            part.unlink(missing_ok=True)  # one a stop left would keep its mode: made again, with the right one
            private = parts[0] in ("state", "_settings-from-backup")
            with z.open(info) as src, (_private_file(part) if private else open(part, "wb")) as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.replace(part, path)
            written += 1
            with contextlib.suppress(OSError):
                at = time.mktime((*info.date_time, 0, 0, -1))
                os.utime(path, (at, at))
    return written, kept


def _library_in(path: Path, check: bool = False) -> str | None:
    """The id of the library whose records a database keeps (None for none), only read. check: the whole file first,
    and that this version takes it on (db.standing: Newer, or Older, for one it leaves alone)."""
    from . import db

    c = db.connect(path, readonly=True)
    try:
        if check:
            said = [row[0] for row in c.execute("PRAGMA quick_check")]
            if said != ["ok"]:
                raise ValueError("; ".join(said)[:200])
            db.standing(c, path)
        if "library" not in db.tables(c):  # from before a database kept a library's records
            return None
        row = c.execute("SELECT id FROM library").fetchone()
        return row[0] if row else None
    finally:
        c.close()


def records_in_place(to: Path) -> tuple[bool, str]:
    """After a restore: the newest copy of the library's records, unpacked with the zip, made this computer's database
    if it has none. One it has is never written over, and a copy of another database is passed over (db.nights).
    Returns whether the library has its records here now, and the words of what was done, or of what to do."""
    from . import db

    latest = db.latest_day()  # a server's in another time zone may be dated tomorrow here, but no further
    days, dest, ahead = db.nights(to, latest), db.file(), passed_over(to)
    if not days:
        others = db.strangers(to, latest)
        return False, (f"The zip had no copy of {RECORDS} in {db.backups(to)}, so they start afresh here."
                       + (f" The copies there of {', '.join(p.name for p in others)} are of another database, and "
                          f"were left as they are." if others else "") + (f" {ahead}" if ahead else ""))
    copy = days[-1] / db.FILE
    try:
        theirs = _library_in(copy, check=True)
    except db.Newer as e:  # never put in place to be refused: its words say what to do
        return False, f"The copy of {RECORDS} in the zip wasn't made this computer's database: {e}"
    except (sqlite3.Error, ValueError, db.Unusable) as e:
        return False, (f"The copy of {RECORDS} in the zip, {copy}, can't be used ({e}). An older zip may have a good one: "
                       f"--saved picks it.")
    instead = f"stop Hylly's server here, move {dest} aside, and copy {copy} in its place"
    if dest.exists():
        try:
            mine = _library_in(dest)
        except (sqlite3.Error, ValueError, db.Unusable) as e:
            mine = f"? {e}"
        if mine == theirs and theirs is not None:
            with contextlib.suppress(OSError, ValueError):  # the same records: the restored marker names this install
                db.tie(to, dest)  # (its token), whichever install the backup was made on, or a later command refuses it
            return True, (f"This computer's database, {dest}, keeps this library's records already, so it was left as it is. "
                          f"To go back to the backup's copy of them, from {days[-1].name}, instead: {instead}.")
        whose = ("no library's records yet" if mine is None else f"some that can't be read ({mine[2:]})" if mine.startswith("? ")
                 else "another library's records")
        return False, (f"This computer's database, {dest}, has {whose}, so it was left as it is, and the restored library "
                       f"has no records here yet. To give it them: {instead}. Or, to keep both, give the restored library "
                       f"a data folder of its own, on this computer's own disk (HYLLY_HOME set to a folder for all its "
                       f"Hylly files, for its commands and its server), and copy it there as {db.FILE}.")
    for extra in ("-wal", "-shm"):  # what a database left behind, with no database
        dest.with_name(dest.name + extra).unlink(missing_ok=True)
    part = dest.with_name(dest.name + ".restoring")
    try:
        part.unlink(missing_ok=True)
        db._private(part)
        with open(copy, "rb") as src, open(part, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, dest)
    except OSError as e:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        return False, f"{RECORDS[0].upper()}{RECORDS[1:]} couldn't be put in place here ({e}): copy {copy} to {dest} by hand."
    with contextlib.suppress(OSError, ValueError):  # else the next command says why, and what to do
        db.tie(to, dest)  # the restored marker names the install the backup was made on: this one keeps them now
    return True, f"{RECORDS[0].upper()}{RECORDS[1:]}, as copied on {days[-1].name}, are this computer's database now: {dest}."


def what_is_there(top) -> str:
    """For a plan: what the disk holds besides the books, the zips with their size all told, as they build up."""
    zips = saved_zips(top)
    days = set_aside_days(top)
    bits = [f"{_many(len(zips), 'zip')} in saved"]
    if zips:
        sizes = [p.stat().st_size for p in zips]
        bits[0] += (f", {size_words(sum(sizes))} in all" if len(zips) > 1 else "") + \
            f", the newest {zips[-1].stem}, {size_words(sizes[-1])}"
    if days:
        bits.append(f"earlier copies set aside on {_many(len(days), 'day')}")
    return "; ".join(bits)


def _sums_set_aside(top) -> dict[str, str]:
    """changed/manifest.sha256 as sha256sum reads it: each earlier copy's path in changed/, with its checksum as it was
    copied, the last line for a path counting. A line that isn't one is passed over, and so is a path that would lead
    out of changed/."""
    try:
        raw = (Path(hc.long_path(Path(top) / "changed")) / MANIFEST).read_bytes()
    except FileNotFoundError:
        return {}
    sums = {}
    for line in raw.split(b"\n"):
        escaped = line.startswith(b"\\")  # a name with a backslash or a line break in it, as _manifest_line writes it
        found = re.fullmatch(rb"([0-9a-f]{64}) [ *](.+)", (line[1:] if escaped else line).rstrip(b"\r"))
        if not found:
            continue
        name = found[2]
        if escaped:
            name = re.sub(rb"\\(.)", lambda c: {b"n": b"\n", b"r": b"\r"}.get(c[1], c[1]), name)
        rel = name.decode("utf-8", "surrogateescape")
        parts = re.split(r"[/\\]", rel) if os.name == "nt" else rel.split("/")  # Windows takes a backslash for a slash
        if not {"", ".", ".."} & set(parts) and not (os.name == "nt" and ":" in rel):
            sums[rel] = found[1].decode()
    return sums


def earlier_copies_to_check(top) -> tuple[dict[str, tuple[str, dict]], str, int]:
    """The earlier copies in changed/, by changed/manifest.sha256, for the check of the books to read again with them
    (hyllycopy.Verifying.run's more): a disk going away is waited out as it is for the books, and their problems are in
    its checked.txt too. Returns them, by "../changed/<their path there>" (from library/, where checked.txt is, and no
    name a book in the record can have), with their place and checksum; the words of why the manifest can't be read,
    or ""; and how many earlier copies there have no checksum in it, set aside by a Hylly from before they were noted,
    or by a stop between setting one aside and noting it."""
    changed = hc.long_path(Path(top) / "changed")
    try:
        sums = _sums_set_aside(top)
    except OSError as e:
        if share_is_gone(e):
            raise
        return {}, f"changed/{MANIFEST}: it can't be read ({e.strerror or e})", 0
    more = {f"../changed/{rel}": (os.path.join(changed, *rel.split("/")), {"h": sha}) for rel, sha in sums.items()}
    there = {os.path.relpath(os.path.join(here, name), changed).replace(os.sep, "/")
             for here, _, files in os.walk(changed) for name in files}
    return more, "", len(there - set(sums) - {MANIFEST})


def set_aside_days(top) -> dict[str, int]:
    """The days in changed/, the oldest first, each with how many earlier copies it holds; a day with none isn't one."""
    changed = Path(hc.long_path(Path(top) / "changed"))
    try:
        days = sorted(p for p in changed.iterdir() if p.is_dir())
    except OSError:
        return {}
    counted = {day.name: sum(len(files) for _, _, files in os.walk(day)) for day in days}
    return {day: n for day, n in counted.items() if n}


def earlier_copies(top) -> str:
    """For a restore, in its plan and at its end: a restore takes the books from library/ only, and a book's good copy
    may be among those set aside, by an earlier Hylly that did so before the new copy was whole, or by a stop between
    the two."""
    days = set_aside_days(top)
    if not days:
        return ""
    names = list(days)
    on = ", ".join(names) if len(names) <= 5 else f"{len(names)} days, from {names[0]} to {names[-1]}"
    return (f"Not restored: {Path(top) / 'changed'} holds {_many(sum(days.values()), 'earlier copy', 'earlier copies')} of "
            f"books that changed in the library, set aside on {on}. If a book in the restored library is missing or not "
            f"as it should be, its earlier copy may be there.")


def size_of_the_rest(root: Path) -> int:
    """What the next zip takes at most, before it's packed."""
    total = 0
    for name in ZIPPED:
        for path in _walk(os.path.join(hc.long_path(root), name), hc.long_path(root)):
            with contextlib.suppress(OSError):
                st = os.stat(path)
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
    return total


def not_a_backup(top) -> None:
    if not library_of(top).is_dir():
        raise Exit(LOCAL, f"{top} has no library folder: is this a backup? Is the disk there?")


# ---- the commands' order of work: hylly backup, backup check and restore -------------------------------------------------

@dataclass
class Outcome:
    """How a backup, a check or a restore ended: words for hylly jobs and the phone's note, the failures counted, and whether
    all of it is as it should be (exit code 0) or not (1, PARTLY); data, the same for --json."""
    words: str
    failed: int
    whole: bool
    data: dict = field(default_factory=dict)


def on_the_same_disk(root: Path, top) -> str:
    """Words for a backup onto the library's own disk, or "": when that disk fails, the backup goes with it. Told by the
    disk numbers of the library and of the target, or of the folder it's to be made in; a 0 (some shares) tells nothing.
    Said, not refused: a second copy there still helps against mistakes."""
    there = Path(os.path.abspath(top))
    while not there.exists() and there != there.parent:
        there = there.parent
    try:
        here, far = os.stat(root).st_dev, os.stat(there).st_dev
    except OSError:
        return ""
    if not here or here != far:
        return ""
    return (f"Note: {top} is on the library's own disk, so this is no second copy on another disk: when that disk fails, the "
            "backup goes with it. It helps against a book deleted by mistake, not against a disk that breaks.")


def plan(root: Path, top, *, new_disk: bool = False, every: float = 0) -> dict:
    """hylly backup TARGET --dry-run: what it would copy, what would go in the zip, and which older zips keep would
    take away then. Nothing is written, nor removed. Returns the same, for --json."""
    keep = the_settings.get("backup")["keep"]
    job = BackingUp(root, top, new_disk=new_disk, every=every)
    job.check()
    if note := on_the_same_disk(root, top):
        print(note, file=sys.stderr)
    job.plan()
    rest, settings = size_of_the_rest(root), what_of_the_settings()
    print(f"With it goes a zip of {', '.join(ZIPPED)} and the settings (never logins, API keys or the phone's ntfy topic), "
          f"at most {size_words(rest)} before packing.")
    print("\n".join(settings))
    there = what_is_there(top) if Path(top).is_dir() else ""
    if there:
        print(f"On the disk now: {there}.")
    going = [p.name for p in to_thin(top, keep, coming=datetime.now())] if Path(top).is_dir() else []
    if going:
        print(f"Then {_many(len(going), 'older zip')} would go, as {keep_words(keep)}:")
        print("\n".join(f"  {name}" for name in going))
    elif any(keep.values()):
        print(f"No zip would go yet: {keep_words(keep)}.")
    return {**job.planned, "zip_bytes_at_most": rest, "settings": settings, "on_the_disk": there,
            "on_the_library_s_disk": bool(note), "would_thin": going}


def run(root: Path, top, *, copy: Callable[[BackingUp], hc.Result], ask: Callable[[str], None] | None = None,
        new_disk: bool = False, every: float = 0) -> Outcome:
    """hylly backup TARGET, in its order: the books (copy runs the copy, one at a time; ask is put the question of a first
    backup there, or of one that would write far more than all before, and raises if the answer is no), then the
    library's records made fresh, the zip, the older zips keep takes away, the library and the disk told of it, and the
    phone told of what needs a look. A server named wrong is refused before anything when the records come from it
    (needs_the_server), and so are settings keep can't be read from. Older zips go only after a zip that's whole: one
    that lacks something, or has older records, leaves them all."""
    from . import remote

    keep = the_settings.get("backup")["keep"]
    if needs_the_server(root):
        remote.server()
    job = BackingUp(root, top, new_disk=new_disk, every=every)
    job.check()
    if note := on_the_same_disk(root, top):  # before the question, so that whoever answers it knows
        print(note, file=sys.stderr, flush=True)
    if ask is not None:
        job.ask_before(ask)
    result = copy(job)
    job.out("Saving the library's records, your edits and the settings in a zip.")
    try:
        night, stale = fresh_records(root)
        if ahead := passed_over(root):
            job.out(ahead)
        if stale:
            job.out(stale)
            job.notes.tell("Hylly backup: older records in it", stale, "The zip has an older copy of the library's records: "
                                                                      "the backup's output says why.")
        job.wait_for(job.target)  # the disk the books went to, still there: what an unmounted one leaves would take the zip
        made, missed = save_the_rest(root, top, night=night, there=job.target.answers)
    except OSError as e:
        words = f"The books went fine, but the zip couldn't be made: {e}. Run this again: the books won't be copied twice."
        job.out(words)
        job.notes.tell("Hylly backup: not all of it", words)
        job.send_the_rest()
        return Outcome(words, 1, False, {**hc.result_data(result), "zip": None})
    job.out(f"Saved: {Path(made).name}, {size_words(Path(made).stat().st_size)}, read back and checked.")
    for line in missed:
        job.out(f"  Not in the zip: {line}")
    if missed:
        job.notes.tell("Hylly backup: not all in the zip", "Not in the zip: " + "; ".join(missed),
                       "Something couldn't be read into the zip: the backup's output says what.")
    thinned, unthinned = thin(top, keep, made) if not (missed or stale) else ([], [])
    if any(keep.values()) and (missed or stale) and to_thin(top, keep, made=made):
        job.out("No older zip went, though keep under [backup] would thin them: this zip isn't whole.")
    for line in thinned + unthinned:
        job.out(line)
    if job.set_aside:
        job.out(f"Changed in the library since the last backup: {len(job.set_aside):,}. Their earlier copies are in changed/{job.day}.")
    short = job.looks_short(result)
    if result.done and not short:
        try:  # one that isn't whole says so, to its note on the disk and the doctor alike, rather than to an exit code alone
            finish(root, top, result, made, len(job.set_aside), [f"Not in the zip: {line}" for line in missed] + [stale] * bool(stale),
                   there=job.target.answers)
        except OSError as e:
            job.out(f"The library couldn't be told of this backup: {e}." + ("" if missed or stale else " The backup itself is fine."))
    words = job.tell_of_the_end(result)
    if short:
        job.out(short)
        job.notes.tell("Hylly backup: is the library all there?", short)
        job.send_the_rest()
    failed = len(result.failed) + len(result.disagree) + len(result.went) + len(missed) + bool(short) + bool(stale)
    whole = result.done and not (result.disagree or result.went or missed or short or stale)
    return Outcome(short or stale or (f"Not in the zip: {missed[0]}" if missed else "") or words, failed, whole,
                   {**hc.result_data(result), "zip": Path(made).name, "not_in_the_zip": missed, "older_records": stale or "",
                    "looks_short": short, "set_aside": len(job.set_aside), "thinned": [line.strip() for line in thinned[1:]]})


def _checking(top, every: float) -> hc.Verifying:
    not_a_backup(top)
    checking = hc.Verifying(library_of(top), every=every)
    checking.NAME, checking.STOP = BackingUp.NAME, BackingUp.STOP
    checking.check()
    return checking


def _zips_words(read: int, there: int) -> str:
    """Which zips a check reads back, in words: all, or the newest of how many."""
    if read == there:
        return _many(read, "zip")
    return f"the newest {read:,} of the {there:,} zips (--all reads every one)"


def check_plan(top, *, every: float = 0, every_zip: bool = False) -> dict:
    """hylly backup check TARGET --dry-run; the same for --json."""
    checking = _checking(top, every)
    checking.plan()
    zips, there = to_check(top, every_zip)
    earlier = 0
    with contextlib.suppress(OSError):  # the check itself says what's wrong with it
        earlier = len(_sums_set_aside(top))
    print(f"And it would read back {_zips_words(len(zips), there)} in {saved_of(top)}"
          + (f", and {_many(earlier, 'earlier copy', 'earlier copies')} in {Path(top) / 'changed'}." if earlier else "."))
    return {**checking.planned, "zips": len(zips), "zips_there": there, "earlier_copies": earlier}


def check(top, *, every: float = 0, every_zip: bool = False) -> Outcome:
    """hylly backup check TARGET: every book read again and compared with its checksum, and so is every earlier copy in
    changed/ (earlier_copies_to_check); the newest zips read back, or with every_zip every one (to_check). Only reads,
    but for the check's own report on the disk; one at a time, as a backup is."""
    checking = _checking(top, every)
    with runs.only_one("backup"):
        more, unreadable, unnoted = earlier_copies_to_check(top)
        problems = checking.run(more)
        zips, wrong = check_saved(top, every_zip)
    unsure = [f"{name}: {why}" for name, why in sorted(checking.problems.items()) if name in more]
    if more or unnoted or unreadable:
        print(f"Earlier copies in changed read back: {len(more):,}."
              + (f" {len(unsure):,} of them not as they were, as listed above." if unsure else ""))
        if unreadable:
            print(f"  {unreadable}")
        if unnoted:
            one = unnoted == 1
            print(f"  {_many(unnoted, 'earlier copy', 'earlier copies')} there {'has' if one else 'have'} no checksum in "
                  f"changed/{MANIFEST} to go by (set aside by an older Hylly, or just before a stop), so "
                  f"{'it was' if one else 'they were'}n't checked.")
    there = to_check(top, every_zip)[1]
    print(f"Zips read back: {zips:,}" + (f", the newest of {there:,} (--all reads every one)" if zips < there else "") + "."
          + (f" {len(wrong):,} of them not as they were:" if wrong else ""))
    for line in wrong:
        print(f"  {line}")
    found = problems + bool(unreadable) + len(wrong)
    return Outcome(f"{_many(found, 'problem')} found.", found, not found,
                   {**checking.checked, "zips": zips, "zips_not_as_they_were": wrong, "earlier_copies": len(more),
                    "earlier_copies_not_as_they_were": unsure + [unreadable] * bool(unreadable),
                    "earlier_copies_unchecked": unnoted})


def _restoring(top, to: Path, saved: str | None, new_disk: bool, every: float) -> tuple[hc.Copying, Path]:
    not_a_backup(top)
    made = choose_zip(top, saved)
    _reads_back(made, top)  # before hours of copying, not after: a damaged zip is said now, with the older ones
    copying = hc.Copying(library_of(top), to, new_disk=new_disk, every=every)
    copying.NAME, copying.STOP = "Hylly restore", BackingUp.STOP
    copying.THEN = ""
    copying.check()
    if not _has_files(to):
        return copying, made
    begun = _restore_begun(to)
    if begun is None:  # one stopped goes on where it was; nothing else is mixed in
        raise Exit(USAGE, f"{to} has files in it already, and no restore was left unfinished there: a restore goes into a new "
                          "or empty folder, so that nothing of the backup is mixed with what's there, and never into one it "
                          "finished, which may be a library in use by now (a book removed from it would come back). Give a "
                          "new folder, or empty this one first. Nothing was copied.")
    if not _same_backup(begun, top):
        whence = begun.get("from")
        raise Exit(USAGE, f"{to} holds a restore left unfinished " + (
            f"from another backup, {whence}: the same command, with that backup, goes on with it."
            if isinstance(whence, str) and whence else
            f"whose note, {to / RESTORING}, can't be read, so it can't be told which backup it's from: restore into a new "
            "folder, or empty this one first.") + " Nothing of this one is mixed in there. Nothing was copied.")
    ended = begun.get("ended")
    changed = _used_since(to, ended if isinstance(ended, (int, float)) and not isinstance(ended, bool) else None)
    if changed:  # a restore that ended with failed files may be in use by now, as a library: a book removed would come back
        raise Exit(USAGE, f"{to} holds a restore left unfinished, but what it copied there has changed since: {hc.shown(changed)} "
                          "is gone or isn't as copied. So it may be a library in use by now, and going on would bring back what "
                          f"was removed from it. Restore into a new folder; or, if that's as it should be, delete {to / RESTORING} "
                          "and use the library as it is. Nothing was copied.")
    return copying, made


def _has_files(folder: Path) -> bool:
    """Whether a folder has anything in it but what systems leave in any folder (desktop.ini, .DS_Store, Thumbs.db, and a
    disk's own $RECYCLE.BIN and the like), as a new disk's top folder has."""
    try:
        with os.scandir(folder) as found:
            return any(not hc._is_clutter(entry.name, entry.is_dir(follow_symlinks=False)) for entry in found)
    except OSError:  # not there, or not a folder: the copy's check says so
        return False


def _mark_of(top) -> str:
    """The mark HyllyCopy left in a backup's books, which goes with the disk under any drive letter; "" without one."""
    with contextlib.suppress(OSError):
        return hc._read_mark(os.path.join(hc.long_path(library_of(top)), hc.FOLDER, "id"))
    return ""


def _restore_begun(to: Path) -> dict | None:
    """The note of a restore left unfinished in this folder (RESTORING): from which backup, and when it began."""
    try:
        found = json.loads((to / RESTORING).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # there, but not readable: still a restore begun here, of a backup not known
        return {}
    return found if isinstance(found, dict) else {}


def _same_backup(begun: dict, top) -> bool:
    """Whether a restore begun in a folder was from this backup: by its disk's mark, which a drive letter changes nothing
    in, else by the path it was given."""
    mark, theirs = _mark_of(top), begun.get("mark")
    if mark and isinstance(theirs, str) and theirs:
        return mark == theirs
    given = begun.get("from")
    return isinstance(given, str) and os.path.normcase(os.path.abspath(given)) == os.path.normcase(os.path.abspath(top))


def _used_since(to: Path, ended: float | None = None) -> str:
    """A file the restore copied into the folder that's gone or changed since, by the copy's own record there, or "":
    one that ended with failed files may be a library in use by now. Changed: another size, or a time later than when
    the restore last ended (its note's "ended"), a disk's two seconds aside. Not just another time than the record's,
    as a disk that keeps no times gives a copy the time it was made."""
    record = hc.Record(os.path.join(hc.long_path(to), hc.FOLDER, "record.jsonl"))
    with contextlib.suppress(OSError):
        record.load()
    for rel, entry in sorted(record.entries.items()):
        try:
            st = os.stat(os.path.join(hc.long_path(to), *rel.split("/")))
            if st.st_size != entry.get("s"):
                return rel
            if ended is not None and st.st_mtime_ns != entry.get("t") and st.st_mtime > ended + 2:
                return rel
        except FileNotFoundError:
            return rel
        except OSError as e:
            if share_is_gone(e):
                raise
    return ""


def _reads_back(made: Path, top) -> None:
    """The zip a restore is to unpack, read back first: a damaged one is said before the books are copied, with the older
    ones to choose from (--saved)."""
    try:
        bad, damaged = _read_back(made)
    except OSError as e:  # a disk that went away
        if share_is_gone(e):
            raise
        bad, damaged = None, e
    if damaged is not None:  # cut short, damaged, or a disk that went away
        why = str(damaged) or type(damaged).__name__
    else:
        why = f"{bad} in it isn't as it was written" if bad else ""
    if why:
        raise Exit(LOCAL, f"{made.name}, in {saved_of(top)}, can't be read back: {why}. Nothing was copied. " + _older(made, top))


def _older(made: Path, top) -> str:
    older = [p.name for p in saved_zips(top) if p != made][-5:]
    if not older:
        return "There's no other zip there; hylly backup check reads the backup again."
    return (f"An older zip may be whole: --saved picks one, such as --saved \"{Path(older[-1]).stem}\". The zips there: "
            f"{', '.join(older)}.")


def restore_plan(top, to: Path, *, saved: str | None = None, new_disk: bool = False, every: float = 0) -> dict:
    """hylly restore BACKUP NEW --dry-run: what it would copy and unpack, and where. Nothing is written. Returns the same,
    for --json."""
    from . import db

    copying, made = _restoring(top, to, saved, new_disk, every)
    copying.plan()
    print(f"Then it unpacks {made.name} in the same folder, {size_words(made.stat().st_size)}, and makes the library's "
          f"records in it this computer's database ({db.file()}), if there's none there yet.")
    earlier = earlier_copies(top)  # changed/ isn't restored, but a book's good copy may be there
    if earlier:
        print(earlier)
    return {**copying.planned, "zip": made.name, "zip_bytes": made.stat().st_size, "database": str(db.file()), "set_aside": earlier}


def restore(top, to: Path, *, copy: Callable[[hc.Copying], hc.Result], saved: str | None = None, new_disk: bool = False,
            every: float = 0) -> Outcome:
    """hylly restore BACKUP NEW: the books copied to a new folder (copy runs the copy, one at a time), the zip unpacked
    in it, and the library's records made this computer's database if it has none, then what's left to do. Nothing there
    is written over."""
    from . import db

    copying, made = _restoring(top, to, saved, new_disk, every)
    earlier = earlier_copies(top)
    to.mkdir(parents=True, exist_ok=True)
    if not (to / RESTORING).is_file():  # until it's whole: a stop leaves it, so the same command goes on there
        atomic_write(to / RESTORING, json.dumps({"from": str(top), "mark": _mark_of(top),
                                                 "begun": db.now()}))
    try:
        return _restore(top, to, copying, made, earlier, copy)
    finally:
        _ended_here(to)


def _ended_here(to: Path) -> None:
    """A restore left unfinished, its note told when it ended: a file changed after that is the library's in use, not the
    copy's own (_used_since). One that's whole has no note."""
    note = read_json(to / RESTORING)
    if note:
        with contextlib.suppress(OSError):
            atomic_write(to / RESTORING, json.dumps({**note, "ended": time.time()}))


def _restore(top, to: Path, copying: hc.Copying, made: Path, earlier: str, copy: Callable[[hc.Copying], hc.Result]) -> Outcome:
    result = copy(copying)
    copying.wait_for(copying.target)  # the new library's disk, still there: what an unmounted one leaves would take the zip
    try:
        written, kept = unpack(made, to, there=copying.target.answers)  # and asked again before each file
    except ZIP_DAMAGE as e:  # read back whole before, and damaged since
        raise Exit(LOCAL, f"The books are copied, but {made.name} couldn't be unpacked: {e}. {_older(made, top)} The same "
                          "command with --saved goes on there, and the books aren't copied twice.") from None
    copying.out(f"Unpacked {made.name}: {_many(written, 'file')}" + (f", and {kept:,} were there already, and stayed." if kept else "."))
    placed, said = records_in_place(to)
    copying.out(said)
    words = copying.tell_of_the_end(result)
    if earlier:
        copying.out(earlier)
    data = {**hc.result_data(result), "zip": made.name, "unpacked": written, "there_already_unpacked": kept,
            "records_here": placed, "records": said}
    if not result.done:
        return Outcome(words, len(result.failed), False, data)
    (to / RESTORING).unlink(missing_ok=True)  # whole: from now it's a library, never restored into again
    steps = [*([f"On a new computer, the settings: {to / '_settings-from-backup'} has the backed-up settings.toml, rules and "
               f"themes, to copy to {paths.config_dir()}. Look them over: they name the old computer's folders, and rules = may "
               f"name a folder elsewhere, whose rules are in rules there now. A backup made on another computer has the "
               f"server's own in {to / 'state' / 'settings-backup'}."] if (to / "_settings-from-backup").is_dir() else []),
             f"The library: set library = '{to}' in {paths.settings_file()}, or try it first with hylly serve --library \"{to}\".",
             *([] if placed else ["The library's records: as said above."]),
             f"Alarms to your phone, if you had them: put the ntfy address in {the_settings.secrets_dir() / 'ntfy-url'}, and "
             f"a token, if your ntfy server wants one, in ntfy-token beside it. A backup never holds them, as the topic is as "
             f"good as a password; the ntfy app on your phone shows the address it's subscribed to. hylly bell test sends an "
             f"alarm to try it.",
             "You'll log in to the shops again: logins, cookies and API keys are never in a backup."]
    print("Next:\n" + "\n".join(f"  {n}. {step}" for n, step in enumerate(steps, 1)))
    return Outcome(words, len(result.failed), True, {**data, "next": steps})
