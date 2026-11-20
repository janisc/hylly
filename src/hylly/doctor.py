"""hylly doctor: what shape this Hylly is in, in one list, with the next step for whatever is wrong. It only looks: it
writes nothing and talks to no site, and it never shows a login, key or ntfy topic.

Each check is a line: its id (CHECKS), what it is about, its state, what it says, and for what is to be done, next, the
step in words for people. Beside next, for a script or an AI assistant: run, the exact command, only when the step is one
command an assistant may run as it stands, one of RUNS (never sudo, a password or login, a --yes or a --run, a shop, nor
anything that opens the library to more people), and owner: true when the step is the owner's alone: it takes sudo, a
password or a login cookie, the owner's login at a shop, a choice only they can make (which folder, who gets in, what to
install, keep or delete), or someone at the server computer. A check has one or the other, or neither (_check)."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import archives, paths
from .commands import version
from .logins import SITES, session_path
from .shops import SHOPS
from .words import many, size_words, time_words

if TYPE_CHECKING:  # for the type hints alone: each look imports it as it runs, as the doctor's start needn't
    from .library import Catalog

OK, INFO, UPDATE, PROBLEM = "ok", "info", "update", "problem"  # update: it works, but something is older than it should be

# Every check the doctor makes, by its id, with what its line is about (its "what", which several share). An id stays
# the same between versions, whichever of the doctor's looks made it and whatever its state, so that a script can go
# by it; one made for each of several things (each shop's failed files, each Inbox, each job running) is that id each
# time. A look that trips over a mistake of its own is a problem under its first check's id (_safely).
CHECKS = {
    "version": "Hylly",  # which Hylly, on which Python and system
    "settings": "Settings",  # whether settings.toml, and the secrets it points to, can be read
    "settings-old-secrets": "Settings",  # the alarms' secrets still under [bell], where they are no longer read
    "settings-app-copy": "Settings",  # a packaged app's own copy of the settings folder, on Windows
    "settings-owner": "Settings",  # what in Hylly's own folders isn't the user's own
    "settings-rights": "Settings",  # what in Hylly's own folders others on this computer can get at
    "places": "Places",  # where Hylly keeps its own files on this computer, by kind, and what chose each folder
    "rules": "Rules",
    "library": "Library",  # whether the library is set, there and marked
    "library-rights": "Library",  # a library folder anyone on this computer can change
    "catalog": "Catalog",
    "catalog-files": "Catalog's files",  # book files the catalog hasn't taken in, and its files gone from the disk
    "catalog-unseen": "Catalog's files",  # files and folders in the library that are there but can't be looked at
    "records": "Records",  # this computer's database has the library's records, but the marker names another install's;
    # or the marker can't be read or names no database
    "books-unopenable": "Books",  # e-books no reader can open: locked by a shop, or damaged
    "books-refused": "Books",  # the catalog's files the server refuses
    "downloads-failed": "Downloads",  # a shop's files that failed last time
    "questions": "Questions",  # the questions the downloads and the catalog left to the owner
    "state-copies": "State",  # the nightly copies in the library's state folder, and who can read them
    "database": "Database",  # the database, and its last nightly copy
    "backup": "Backup",
    "reader-memory": "Reader",  # pages kept in memory, held back by what's free
    "reader-cache": "Reader",  # the reader's cache folder
    "reader-not-ours": "Reader",  # what isn't Hylly's in the reader's cache folder
    "rights": "Rights",  # as root, or an administrator's terminal
    "remote": "Remote",  # which computer the commands typed here run on
    "address": "Address",  # the address people use, without https, while the server listens beyond this computer
    "address-unset": "Address",  # listening on every address, with no address set
    "proxy-headers-ignored": "Address",  # forwarding headers from an address not in trusted_proxies
    "proxy-sender-unnamed": "Address",  # wrong logins from a trusted proxy that names no sender
    "trusted-proxies-wide": "Address",  # trusted_proxies beyond this computer
    "logins": "Logins",  # the shops' logins and the lookups' keys saved, never shown
    "server": "Server",  # whether the library server runs, and starts at logon or with the computer
    "logon-start": "Logon start",  # what the start at logon, or the system-wide service, starts
    "access": "Access",  # where the server listens, the address people use, and who can log in
    "walls": "Walls",  # the walls around the server, or what it can reach without them
    "inbox": "Inbox",  # each Inbox, and what waits in its Not added folder
    "inbox-missing": "Inbox",  # an Inbox's folder that isn't there
    "inbox-in-library": "Inbox",  # an Inbox that is the library or inside it
    "inbox-overlap": "Inbox",  # an Inbox that is another, or inside it, or holds it
    "inbox-runner": "Inbox",  # what runs the Inboxes: the server, or a timer
    "job": "Job",  # a job running now
    "alarms": "Alarms",  # where the alarms go, and how strong the ntfy topic is
    "alarm-command": "Alarms",  # the program named as command under [bell]
    "7-zip": "7-Zip",  # 7-Zip and its RAR part, for CBR comics
    "pdfs": "PDFs",  # the part of Hylly that draws PDFs
    "updates": "Updates",  # what is older than it should be, when the look itself fails
    "update-pymupdf": "PyMuPDF",  # each thing older than it should be (updates.behind): updates.PACKAGES, Python, 7-Zip
    "update-pillow": "Pillow",
    "update-python": "Python",
    "update-7-zip": "7-Zip",
}


# What a check's run may be, by its words, and nothing else: hylly's commands that only read, or write no more than
# what Hylly makes again itself (the catalog), the catalog's for the library the doctor was given with --library by its
# full path; and taking the rights of others away from Hylly's own folders, also by their full paths (PRIVATE, on Linux
# and macOS). Any other is a mistake of the doctor's (_command).
RUNS = {("hylly", "catalog"): "library", ("hylly", "catalog", "--rebuild"): "library", ("hylly", "rules"): ""}
PRIVATE = ("chmod", "-R", "go-rwx")


def _command(words: tuple[str, ...]) -> str:
    """A check's run from its words, as this system's shell takes them; ValueError for one that isn't in RUNS, or
    PRIVATE on full paths."""
    words = tuple(str(w) for w in words)
    if words[:3] == PRIVATE:
        allowed = len(words) > 3 and all(map(os.path.isabs, words[3:]))
    elif words[-2:-1] == ("--library",):
        allowed = RUNS.get(words[:-2]) == "library" and os.path.isabs(words[-1])
    else:
        allowed = words in RUNS
    if not allowed:
        raise ValueError(f"A run the doctor may not give: {words}")
    return subprocess.list2cmdline(words) if paths.WINDOWS else shlex.join(words)


def _check(key: str, state: str, says: str, then: str = "", run: tuple[str, ...] = (), owner: bool = False) -> dict:
    """One line, by its id in CHECKS: then is its next step in words; run the words of the one command for it an
    assistant may run (RUNS), owner whether that step is the owner's alone (the module's docstring): never both."""
    if run and owner:
        raise ValueError(f"A step that is the owner's has no run: {key}")
    return {"id": key, "what": CHECKS[key], "state": state, "says": says, **({"next": then} if then else {}),
            **({"run": _command(run)} if run else {}), **({"owner": True} if owner else {})}


def _absolute(*places) -> tuple[str, ...]:
    """Paths as a run gives them: in full, wherever the command is run from."""
    return tuple(os.path.abspath(place) for place in places)


def _library_option(library: str | None) -> tuple[str, ...]:
    """A run's --library, for the library the doctor was given with --library, if it was: the same one, by its full path."""
    return ("--library", *_absolute(library)) if library else ()


def app_copies() -> list[Path]:
    """Copies of Hylly's folders that packaged apps keep to themselves, on Windows: such an app gets a private view of
    LOCALAPPDATA and APPDATA, so what a program started from one writes (an AI assistant in a desktop app, say) lands in
    its copy, and nothing outside sees it. Only of the folders there, however HYLLY_HOME may name them (through a link,
    say): the one folder of all in one in LOCALAPPDATA, or the data folder in it and the settings folder in APPDATA.
    Folders elsewhere have none. hylly user says so too (cli._mention_app_copies)."""
    base, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if os.name != "nt" or not base:
        return []
    data, ours = paths._resolved(paths.place("data")), paths._resolved(Path(base) / "Hylly")
    found = []
    if data == ours or data == paths._resolved(Path(base, "Hylly", "data")):
        found += Path(base, "Packages").glob("*/LocalCache/Local/Hylly")
    if roaming and data != ours and paths._resolved(paths.config_dir()) == paths._resolved(Path(roaming) / "Hylly"):
        found += Path(base, "Packages").glob("*/LocalCache/Roaming/Hylly")
    return sorted(found)


def _settings() -> list[dict]:
    from . import access, bell, rules, settings

    out = []
    file = paths.settings_file()
    try:
        paths.section("server")
        for section in settings.DEFAULTS:
            settings.get(section)
        bell.settings()
        access.settings()
    except SystemExit as e:  # a secret's file that can't be read names itself (settings.secret_from)
        return [_check("settings", PROBLEM, str(e), f"Put it right in {getattr(e, 'path', None) or file}.", owner=True)]
    out.append(_check("settings", OK, f"{file} reads fine." if file.is_file() else f"No {file.name} yet, so all are as they come."))
    if settings.old_secrets(paths.section("bell")):  # the alarms' secrets set by hand where an older Hylly kept them
        out.append(_check("settings-old-secrets", PROBLEM, f"{file} still has the alarms' ntfy address or token under "
                          "[bell], where they are no longer read: the phone gets no alarms from there, and backups leave "
                          "the file out.", f"Put the address in {settings.secrets_dir() / 'ntfy-url'}, and a token, if "
                          "there is one, in ntfy-token beside it; then take the lines out of settings.toml.", owner=True))
    for copy in app_copies():
        out.append(_check("settings-app-copy", INFO, f"An app keeps its own copy of this folder: {copy}. Logins and users made from inside it are there and not here."))
    try:
        rules.current()
        out.append(_check("rules", OK, "They read fine."))
    except SystemExit as e:
        out.append(_check("rules", PROBLEM, str(e), "hylly rules shows where they come from.", run=("hylly", "rules")))
    return out


def _library(library: str | None) -> tuple[list[dict], Path | None]:
    try:
        root = paths.library_root(library)
    except SystemExit as e:  # which folder is the library is the owner's to say
        return [_check("library", PROBLEM, str(e), "hylly demo makes a small one to try things on, apart from anything else.",
                       owner=True)], None
    except (OSError, ValueError) as e:
        return [_check("library", PROBLEM, f"The settings can't be read: {e}")], None
    try:
        if not root.is_dir():  # a disk or a share to connect, at the server computer
            return [_check("library", PROBLEM, f"{root} isn't there. Is the disk or the share up, and is the path right?",
                           owner=True)], None
        if paths.library_missing(root):  # never marked to get past it: only the owner knows it's a new library
            return [_check("library", PROBLEM, f"{root} is there, but isn't marked as a Hylly library: it may be an empty folder under an unmounted disk.",
                           f"Is the library's disk or share there? Mount or connect it, and the library is found again: "
                           f"don't mark this folder for it. Only for a new library: hylly init {paths.typed(root)}", owner=True)], None
        free = shutil.disk_usage(root).free
    except OSError as e:
        return [_check("library", PROBLEM, f"{root} can't be read: {e}")], None
    return [_check("library", OK, f"{root}, with {size_words(free)} free.")], root


def _catalog(root: Path, library: str | None = None, cat: Catalog | None = None) -> dict:
    """The catalog, as cat reads it (a library.Catalog the doctor's looks at it share: report), else one of its own."""
    from .library import Catalog

    page = paths.catalog_dir(root) / "data.js"  # what a build writes, and the server reads
    try:
        if not page.is_file():
            return _check("catalog", PROBLEM, "Not built yet.", "hylly catalog", run=("hylly", "catalog", *_library_option(library)))
        days = (time.time() - page.stat().st_mtime) / 86400
        titles = len((cat or Catalog(root)).get())
    except (OSError, ValueError, SystemExit) as e:
        return _check("catalog", PROBLEM, f"It can't be read: {e}", "hylly catalog --rebuild",
                      run=("hylly", "catalog", "--rebuild", *_library_option(library)))
    built = "today" if days < 1 else f"{many(round(days), 'day')} ago"
    return _check("catalog", OK, f"{many(titles, 'title')}, built {built}.")


def _elsewhere(marker: dict) -> str:
    """Another computer keeps the library's records (the marker names it): so its tags and corrections too, which a
    library server here can't lay over the catalog."""
    return (f"The library's own records are kept in the database on {marker.get('computer', 'another computer')}. A library "
            "server on this computer shows the catalog as built there, without its tags and corrections.")


def _records_refused(root: Path, ours: str | None, library: str | None) -> dict:
    """This computer's database has the library's records, but the library's marker names them by another install's token
    (db.own): every change to them is refused here, as a copy's are. Its token gone from the data folder, or a copy."""
    from . import db

    token = db.token_file()
    why = (f"names them by this install's token, and {token} {'is missing' if ours is None else 'holds no token'}" if not ours
           else "names another install's database, of which this one is a copy")
    return _check("records", PROBLEM, f"This computer's database has the library's records, but {db.owner_file(root)} {why}: "
                                      "every change to them is refused here.",
                  f"If this install has kept them all along, put {token.name} back beside {db.FILE}, or run "
                  f"{db.TAKE_OVER}{' ' + ' '.join(_library_option(library)) if library else ''}, which asks first. If the "
                  "records are kept on another computer, run the commands there, or stop its server first before taking "
                  "them over here.", owner=True)


def _token_spoiled(root: Path, library: str | None) -> dict:
    """This install's token file holds no token, beside a marker from before markers named an install, which names this
    database: as a crash can leave it, on a disk without hard links. Each program that opens the records makes it again
    and ties the marker (db._token), so one still here is one that couldn't be made: a data folder that can't be
    written, say. The records aren't refused meanwhile."""
    from . import db

    token = db.token_file()
    return _check("records", PROBLEM, f"{token}, this install's token, holds none, as a crash while it was made can leave "
                                      f"it, so {db.owner_file(root)} isn't tied to this install yet, and every command "
                                      "that opens the library's records waits a moment for it.",
                  f"Run {db.TAKE_OVER}{' ' + ' '.join(_library_option(library)) if library else ''}: it makes the token "
                  f"again and ties the marker to it, asking nothing, as the records are this install's. If {token.name} "
                  f"still holds none after that, see that {token.parent} can be written.", owner=True)


MARKER_AGAIN = 0.05  # seconds between looks at a marker that reads as damaged: db.READ_BACK looks, as db._read_back


def _marker_of(root: Path) -> dict:
    """The library's marker as db.owner reads and judges it; one that reads as damaged looked at again a moment apart,
    as db._read_back does, as another computer may be writing it in place just now: only one still damaged after that
    raises its ValueError. FileNotFoundError for none, and any other OSError, at once."""
    from . import db

    for _ in range(db.READ_BACK - 1):
        try:
            return db.owner(root)
        except ValueError:
            time.sleep(MARKER_AGAIN)
    return db.owner(root)


def _marker_damaged(root: Path, e: ValueError, library: str | None) -> dict:
    """The library's marker is there and read, but doesn't say which database keeps the records (db.owner): cut short,
    damaged, a list, no id. Every command that opens the records is refused them until it's written anew, which only
    the computer whose server has kept them may do. Which computer that is, nothing here can tell for sure: a copy of
    its database (a restored backup, a nightly copy put in place) and a former keeper have a library row for this
    folder too, and the same library may be reached under another name. So the step says where, in words."""
    from . import db

    return _check("records", PROBLEM, f"The library's owner marker ({db.owner_file(root)}) can't be read: {e}. Every "
                                      "command that opens the library's records refuses them until it's written anew.",
                  f"Run {db.TAKE_OVER}{' ' + ' '.join(_library_option(library)) if library else ''} on the computer "
                  "whose server has kept this library's records lately, and only there: it writes the marker anew, "
                  "naming that computer's database, and asks first. On any other computer it would start the records "
                  "afresh or put older ones in place, and the one that keeps them would be refused them.", owner=True)


def _marker_unreadable(root: Path, e: OSError) -> dict:
    """The library's marker is there, but the disk won't give it now (no right to read it, a share's own trouble): the
    marker itself may be whole, so nothing is to be written anew, only the disk and its rights looked at."""
    from . import db

    return _check("records", PROBLEM, f"The library's owner marker ({db.owner_file(root)}) can't be read now: {e}. "
                                      "Every command that opens the library's records refuses them meanwhile.",
                  f"Is the library's disk or share there and answering, and may this account read "
                  f"{db.owner_file(root).parent}? The marker itself may be whole: write nothing over it.", owner=True)


def _underneath(root: Path, library: str | None = None) -> list[dict]:
    """Book files the catalog hasn't taken in yet, and its files no longer on the disk (catalog.unlisted); and what in
    the library is there but can't be looked at, so that no build can take it in (catalog-unseen). Read from this
    computer's database, opened read-only, if it keeps the library's records: this only looks, and moves nothing in."""
    from . import catalog, db

    try:
        marker = _marker_of(root)
    except FileNotFoundError:  # no database has taken the library's records on yet: nothing to say of them
        return []
    except ValueError as e:  # there, but cut short, damaged, a list or naming no id, still after a moment
        return [_marker_damaged(root, e, library)]
    except OSError as e:  # there, but not to be read now: the disk's trouble, or its rights, not the marker's
        return [_marker_unreadable(root, e)]
    if not db.file().is_file():
        return [_check("catalog-files", INFO, _elsewhere(marker))]
    c = db.connect(db.file(), readonly=True)
    try:
        try:  # opened read-only, so not brought up to date: only read if this version knows its tables
            schema = db.standing(c, db.file())[0]
        except db.Older:  # the database's own check says what to do
            return [_check("catalog-files", PROBLEM, "The database is from a test version of Hylly from before its first release.")]
        except db.Newer:
            return [_check("catalog-files", PROBLEM, "The database is from a newer version of Hylly.", "Update Hylly.", owner=True)]
        except db.Unusable as e:
            return [_check("catalog-files", PROBLEM, str(e))]
        mine = c.execute("SELECT id FROM library").fetchone() if schema else None
        if mine is None or mine[0] != marker.get("id"):
            return [_check("catalog-files", INFO, _elsewhere(marker))]
        token, ours = marker.get("token"), db._read_token(db.token_file())
        if token is not None and token != ours:  # a copy of the database, or this install's token gone
            return [_records_refused(root, ours, library)]
        spoiled = [_token_spoiled(root, library)] if token is None and ours == "" else []  # left so by a crash
        unseen: list[tuple[str, str]] = []
        found = catalog.unlisted(root, c, unseen)
    finally:
        c.close()
    if found is None:
        return spoiled
    new, gone = found
    out = []
    if not new and not gone:
        out.append(_check("catalog-files", OK, "The disk has what the catalog has" + (", of what can be looked at." if unseen else ".")))
    else:
        says = []
        if new:
            says.append(f"{many(len(new), 'book file')} the catalog hasn't taken in yet, such as {new[0]}.")
        if gone:
            says.append(f"{len(gone):,} of its files {'is' if len(gone) == 1 else 'are'} no longer on the disk, such as {gone[0]}.")
        out.append(_check("catalog-files", INFO, " ".join(says), "hylly catalog takes in what's new and lets go of what's gone.",
                          run=("hylly", "catalog", *_library_option(library))))
    if unseen:  # listed, and then nothing could be told of them: else the books in them would be missing without a word
        folders = sum(rel.endswith("/") for rel, _ in unseen)
        counted = [many(n, word) for n, word in ((len(unseen) - folders, "file"), (folders, "folder")) if n]
        then = " ".join(f"{words}." for words, n in ((catalog.UNSEEN_NEXT, len(unseen) - folders), (catalog.FOLDERS_NEXT, folders)) if n)
        out.append(_check("catalog-unseen", INFO, f"{' and '.join(counted)} in the library can't be looked at, so the catalog "
                                                  f"can't tell what's in {'it' if len(unseen) == 1 else 'them'}: "
                                                  f"{catalog.unseen_words(unseen)}.", then, owner=True))
    return spoiled + out


def _cant_open(root: Path, cat: Catalog | None = None) -> list[dict]:
    """The e-books the last catalog found no reader can open (meta.cant_open), as cat reads it (_catalog). A catalog
    that can't be read is _catalog's to say."""
    from urllib.parse import unquote

    from . import meta
    from .library import Catalog

    if not (paths.catalog_dir(root) / "data.js").is_file():
        return []
    try:
        items = (cat or Catalog(root)).get()
        found = sorted((unquote(path), why) for it in items for path, why in it.get("bad", {}).items())
    except (OSError, ValueError, SystemExit):
        return []
    if not found:
        return []
    shown = ", ".join(f"{path} ({why})" for path, why in found[:10]) + (f", and {len(found) - 10} more" if len(found) > 10 else "")
    then = "They stay in the library, but aren't offered for reading or in the feeds."
    if any(why == meta.LOCKED for _, why in found):  # only the fact, as the page and the help say it
        then += " A locked one can only be read in the app of the shop that sold it."
    if any(why != meta.LOCKED for _, why in found):
        then += " To read a damaged one, the library needs another copy in its place: the next hylly catalog sees it."
    return [_check("books-unopenable", INFO, f"{many(len(found), 'e-book file')} can't be opened, being locked by the shop that sold {'it' if len(found) == 1 else 'them'} or damaged: {shown}.",
                   then, owner=True)]  # another copy, or the shop's own app: only the owner has those


REFUSED_SHOWN = 3  # of the catalog's files the server refuses, this many are named


def _refused(root: Path, library: str | None = None, cat: Catalog | None = None) -> list[dict]:
    """The catalog's files the server refuses (serve.refused_files), as cat reads it (_catalog): shown in the page and
    the feeds, they never open, and nothing else would say why. A catalog that can't be read is _catalog's to say."""
    from . import catalog, serve
    from .library import Catalog

    if not (paths.catalog_dir(root) / "data.js").is_file():
        return []
    try:
        found = serve.refused_files(root, cat or Catalog(root))
    except (OSError, ValueError, SystemExit):
        return []
    if not found:
        return []
    n = len(found)
    one = n == 1
    shown = ", ".join(catalog.shown(catalog.shown_name(rel)) for rel in found[:REFUSED_SHOWN])
    more = f", and {n - REFUSED_SHOWN:,} more" if n > REFUSED_SHOWN else ""
    windows = sum(not serve.plain_words(rel) for rel in found) if paths.WINDOWS else 0
    if windows:  # the library's files renamed: the owner's
        then = ("Windows takes a name with a colon or a backslash, one ending in a dot or a space, or one named as a device "
                f"(CON, AUX, NUL, COM1) for something else: rename {'it' if windows == 1 else 'those'}, then run hylly catalog.")
        run = ()
    else:
        then = (f"hylly catalog lists {'it' if one else 'them'} again as {'it is' if one else 'they are'} on the disk now. A file "
                "reached through a link that leads out of the library is never served.")
        run = ("hylly", "catalog", *_library_option(library))
    return [_check("books-refused", INFO, f"{many(n, 'file')} the catalog lists {'is' if one else 'are'} refused by the "
                                          f"server, so no link, reader or reading app can open {'it' if one else 'them'}: {shown}{more}.",
                   then, run=run, owner=not run)]


def _failures(root: Path) -> list[dict]:
    out = []
    for shop in SHOPS.values():
        path = paths.state_dir(root) / shop.failures
        try:
            rows = max(sum(1 for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()) - 1, 0) if path.is_file() else 0
        except OSError:
            continue
        if rows:
            # another download at the shop, on the owner's saved login: theirs to start
            out.append(_check("downloads-failed", INFO, f"{many(rows, 'file')} failed last time, listed in "
                                                        f"state/{shop.failures}.", "Running that shop's download again tries them again.",
                              owner=True))
    return out


def _questions(root: Path) -> list[dict]:
    """The questions the downloads and the catalog left to you (curation.questions), once the database keeps the
    library's records: how many are open, and at which shops, or the catalog. A library no database has taken on yet has
    none, and isn't taken on here."""
    from . import curation, db

    if not db.file().is_file() or db.marked(root) is None:
        return []
    try:
        found = curation.questions(root)
    except (db.NotHere, db.Newer, db.Unusable):  # another computer's questions, or a database the other checks speak of
        return []
    if not found:
        return []
    at = sorted({SHOPS[q["shop"]].label for q in found if q["shop"] in SHOPS})
    where = " and ".join(x for x in (f"the downloads ({', '.join(at)})" if at else "",  # and the catalog's own
                                     "the catalog" if any(q["shop"] == curation.CATALOG for q in found) else "") if x)
    held = sum(q["held"] for q in found)
    n = len(found)
    return [_check("questions", INFO, f"{many(n, 'question')} to decide from {where}"
                                      + (f", {held} of them with a file held back for your answer" if held else "") + ".",
                   "hylly decide lists them and takes your answers.", owner=True)]  # the answers are the owner's


def _logins() -> dict:
    have = [s for s, (_, _, variable) in SITES.items() if os.environ.get(variable) or session_path(s).is_file()]
    lacking = [s for s in SITES if s not in have]
    return _check("logins", INFO, f"Saved: {', '.join(have) or 'none'}. Not saved: {', '.join(lacking) or 'none'}. "
                                  "Whether a site still takes one shows when it's used; hylly login SITE checks a new one.")


def _server() -> list[dict]:
    from . import instance

    running = instance.running()
    says = "Not running." if running is None else f"Running on port {running.get('port', '?')}, process {running.get('pid', '?')}."
    logon = instance.autostart_state()
    when = "Starts with the computer" if instance.system_service() else "Starts at logon"
    return [_check("server", INFO, says + ("" if logon is None else f" {when}: {'yes' if logon else 'no'}.")), *_logon_start()]


def _logon_start() -> list[dict]:
    """The start at logon, or the system-wide service: the program it starts, and whether that is gone, or the
    start is one an older Hylly made, with that version's options for the server. Each step is the owner's: what
    starts with the computer is the whole user's, and a system-wide service takes sudo."""
    from . import instance

    found = instance.logon_found()
    if found is None:
        return []
    where, program = found["where"], found["program"]
    if found["gone"]:
        then = (f"Put ExecStart right in {where}, or take the service away: sudo systemctl disable --now hylly, then sudo rm "
                f"{where} and sudo systemctl daemon-reload" if found["walled"] else
                "hylly server autostart off, and then on again if you still want it"
                + ("" if instance.SYSTEM == "windows" else " (each prints the commands to run)"))
        return [_check("logon-start", PROBLEM, f"It starts {program} ({where}), which isn't there any more: it fails at every start.", then,
                       owner=True)]
    if found["fails"]:  # the server's options of before, given to one in the background: refused at every start now
        said = (f"{'The system-wide service' if found['walled'] else 'The start at logon'} ({where}) fails at every start now: "
                "it gives the server options of its own, which a server in the background no longer takes, as the settings "
                "are its only options.")
        then = ("Take the options off its ExecStart, leaving hyllyd --in-background, and put them in the settings (port and "
                "listen under [server]; library and rules at the top); then sudo systemctl daemon-reload and sudo systemctl "
                "restart hylly" if found["walled"] else
                "Run hylly server autostart on again: it makes the task anew, with the settings" if instance.SYSTEM == "windows" else
                "Run hylly server autostart on again, and the commands it prints: the system then runs the server, with the settings")
        return [_check("logon-start", PROBLEM, said, then, owner=True)]
    if found["old"] and found["walled"]:  # what it lacks of today's example, each said only where it's so
        shelled = found.get("shelled", False)
        no_restart = found.get("no_restart", not shelled)
        why = [*(["a shell writes the server's log, which the server writes itself, with a size limit"] if shelled else []),
               *(["it lacks RestartForceExitStatus=75, so a Restart from the server page counts as a failure there"]
                 if no_restart else [])]
        take = [*(["ExecStart"] if shelled else []), *(["SuccessExitStatus and RestartForceExitStatus"] if no_restart else [])]
        return [_check("logon-start", UPDATE, f"The system-wide service ({where}) isn't the one examples/hylly.service gives "
                       f"today: {'; and '.join(why)}.",
                       f"Take {' and '.join(take)} from examples/hylly.service into it (hylly serve --service-file prints "
                       "it for this computer), then: sudo systemctl daemon-reload, sudo systemctl restart hylly", owner=True)]
    if found["old"]:
        then = ("hylly server autostart on: it makes the task again, with the settings as the server's options" if instance.SYSTEM == "windows"
                else "hylly server autostart on, and the commands it prints: the system then runs the server, with the settings")
        return [_check("logon-start", UPDATE, f"The start at logon ({where}) is one an older Hylly made, with that version's "
                       "options for the server" + ("" if instance.SYSTEM == "windows" else ", and a log that only grows")
                       + ". The settings are the server's only options now.", then, owner=True)]
    return [_check("logon-start", INFO, f"It starts {program} ({where}).")]


def _where(running: dict | None) -> tuple[str, str]:
    """Where the server listens and the address people use ([server] listen and address): the running server's own,
    else the settings'."""
    from . import serve, settings

    saved = settings.get("server")
    if running:
        return serve.listens(running) or serve.options()[1], running.get("address", saved["address"])
    return serve.options()[1], saved["address"]


def _listening(listen: str) -> str:
    import ipaddress

    found = ipaddress.ip_address(listen)
    if found.is_loopback:
        return f"this computer only ({listen})"
    if found.is_unspecified:
        return f"every {'IPv4 ' if found.version == 4 else ''}address of this computer's ({listen})"
    return f"one address, {listen}"


def _access() -> list[dict]:
    """Who can reach the server, in today's terms: where it listens ([server] listen: the running server's own, else
    the settings'), the address people use ([server] address), and who can log in, which every request must. No
    users, or no owner, is a problem: nobody gets in, or nobody has the library's management. The same in --json, under
    "access"."""
    import sqlite3

    from . import access, db, instance, serve

    listen, address = _where(instance.running())
    says = (f"It listens on {_listening(listen)}" + (f", and the address people use is {address}" if address else ", with no address set")
            + ". Every request logs in: ")
    found = {"listens": listen, "address": address or None, "users": None, "owners": None}
    try:
        accounts = access.peek_accounts()
    except (db.Newer, db.Unusable, sqlite3.Error, OSError) as e:  # the users can't be read: nobody can log in
        return [{**_check("access", PROBLEM, f"{says}nobody can, as the users can't be read from the database ({e}).",
                          "Look at the Database line and the server's log; the database may need help."), "access": found}]
    users, then = serve.who_logs_in()
    found.update(users=len(accounts), owners=len(access.owners(accounts)))  # a first user, or an owner: who gets in is theirs
    return [{**_check("access", PROBLEM if then else OK, f"{says}{users}.", then, owner=bool(then)), "access": found}]


def _elevated() -> bool:
    """Windows' root: a terminal opened with Run as administrator."""
    if os.name != "nt":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _rights() -> list[dict]:
    if paths.is_root():
        if paths.running_as_root():
            return [_check("rights", PROBLEM, "This runs as root: no other command of Hylly's will.", "Run it as your own user, without sudo.",
                           owner=True)]
        return [_check("rights", INFO, "This runs as root, as HYLLY_ALLOW_ROOT lets it. Anything it writes is root's, and a flaw in it could do anything on this computer.")]
    if _elevated():
        return [_check("rights", PROBLEM, "This terminal runs as administrator, and a server started from it would have the same rights.", "Use a normal terminal.",
                       owner=True)]
    return []


def _own_folders() -> list[Path]:
    """Hylly's own folders on this computer (paths.PLACES) that are there, each once, and none that is inside another of
    them, as the logins and the cache are in the one folder of all in one: what's inside a private folder is looked at
    with it."""
    there = [f for f in dict.fromkeys(paths.place(kind) for kind in paths.PLACES) if f.is_dir()]
    real = {f: paths._resolved(f) for f in there}
    return [f for f in there if not any(o != f and real[f] != real[o] and real[f].is_relative_to(real[o]) for o in there)]


def _private() -> list[dict]:
    """Hylly's own folders hold logins, keys and password hashes. What's readable by others, or isn't yours, every command has
    already tried to put right as far as it could (paths.keep_private): what is left takes someone with the rights."""
    folders = _own_folders()
    if os.name == "nt" or paths.is_root() or not folders:
        return []
    me, foreign, loose, own, opened = os.getuid(), set(), set(), [], []
    for folder in folders:
        reachable = folder.stat().st_mode & 0o077  # inside a private folder, nobody gets at anything, whatever its own rights say
        for path in (folder, *folder.iterdir()):
            found = path.lstat()
            if found.st_uid != me:
                foreign.add(path.name)
                own += [folder] if folder not in own else []
            elif reachable and not path.is_symlink() and found.st_mode & 0o077:
                loose.add(path.name)
                opened += [folder] if folder not in opened else []
    out = []
    if foreign:
        out.append(_check("settings-owner", PROBLEM, f"{len(foreign)} of what's in Hylly's own folders isn't yours: {', '.join(sorted(foreign)[:5])}. A command run with sudo leaves that behind, "
                                                     "and Hylly may not be able to write it.", f"sudo chown -R $USER {' '.join(map(paths.typed, own))}", owner=True))
    if loose:  # only taking rights away, from what is the user's own
        out.append(_check("settings-rights", PROBLEM, f"Others on this computer can get at {', '.join(sorted(loose)[:5])} in Hylly's own folders, where logins and keys are.",
                          f"chmod -R go-rwx {' '.join(map(paths.typed, opened))}", run=(*PRIVATE, *_absolute(*opened))))
    return out


def _places() -> list[dict]:
    """Where Hylly keeps its own files on this computer: each kind's folder and what chose it (paths.places), as hylly
    settings places says it. All in one, as an install made before the kinds had folders of their own keeps it, is no
    fault: a note, and how to split it if one wants, which the help tells (Where things are). The same in --json, under
    "places"."""
    found = paths.places()
    one = paths.all_in_one()
    apart = [f"{p['name']} in {p['folder']} ({p['said']})" for p in found if p["from"] not in ("HYLLY_HOME", paths.ONE_FOLDER)]
    if one is not None:
        why = "the one HYLLY_HOME names" if found[0]["from"] == "HYLLY_HOME" else "as this install has them"
        says = (f"Hylly keeps its own files all in one folder, {one}, {why}." + (f" Apart from it: {'; '.join(apart)}." if apart else "")
                + " To keep them apart by kind instead, each in a folder of its own, if you want: the help's Where things are says how.")
    else:
        says = "Hylly keeps its own files by kind: " + "; ".join(apart) + "."
    return [{**_check("places", INFO, says), "places": found}]


def _people(root: Path) -> list[dict]:
    """The database, and its nightly copy: what's kept about people is the one thing here the books can't bring back. A
    problem when the copy isn't good, is over two days old, or was never made though people have been kept for
    NEVER_COPIED_DAYS: a server whose copy has never worked is as late as one that stopped."""
    from . import db, people

    if not db.file().is_file():
        return []
    try:
        found = people.everyone()
    except (db.Newer, db.Unusable) as e:  # one from before the first release (Older): its words say what to do
        newer = isinstance(e, db.Newer) and not isinstance(e, db.Older)
        return [_check("database", PROBLEM, str(e), "Update Hylly." if newer else "", owner=newer)]
    if not found:
        return []
    last = db.last_backup(root)
    day, said = last if last else (None, {})
    says = f"{len(found)} people kept in the database, last copied to the library's disk {day or 'never'}."
    if day and said.get("integrity") != "ok":
        return [_check("database", PROBLEM, f"{says} That copy's check says: {said.get('integrity')}.", "Look at the server's log; the database may need help.")]
    late = (date.today() - date.fromisoformat(day)).days > 2 if day else (  # never copied, though kept for days by now
        time.time() - min(people._seconds(p["since"]) for p in found) > NEVER_COPIED_DAYS * 86400)
    then = "Does the server run, and can it write to the library's state folder? Its log says why a copy failed."
    return [_check("database", PROBLEM if late else INFO, says, then if late else "")]


BACKUP_LATE_DAYS = 30
NEVER_COPIED_DAYS = 2  # people kept this long, with no nightly copy of the database ever made: a problem, as a late one is


def _backup(root: Path) -> list[dict]:
    from . import backup

    done = backup.last(root)  # each step names a target, the owner's to choose, and a first backup there asks them first
    if not done:
        return [_check("backup", INFO, "None made yet. Everything here is on one disk.", "hylly backup TARGET --dry-run shows what a backup to that folder would do.",
                       owner=True)]
    day = done["when"][:10]
    late = (date.today() - date.fromisoformat(day)).days > BACKUP_LATE_DAYS
    says = f"Last on {day}, to {done.get('target', 'a disk')}, {many(done.get('files', 0), 'file')}."
    missed = [m for m in done["missed"] if isinstance(m, str)] if isinstance(done.get("missed"), list) else []
    if missed:  # the books went, but not all of the rest: a scheduled backup could go on so for weeks
        more = f" (and {len(missed) - 1:,} more)" if len(missed) > 1 else ""
        return [_check("backup", PROBLEM, f"{says} It isn't whole: {missed[0]}{more}",
                       "Put right what it names, and back up again: hylly backup TARGET, which won't copy the books twice.", owner=True)]
    return [_check("backup", PROBLEM if late else INFO, says, f"Over {BACKUP_LATE_DAYS} days ago: is it time for another? hylly backup TARGET" if late else "",
                   owner=late)]


def _pages_memory() -> list[dict]:
    from . import reader, settings

    note = reader.memory_note()
    # each step a setting to change, a disk to connect or another's files to move: the owner's
    out = [_check("reader-memory", INFO, note, "Lower memory_mb under [reader] in the settings, or free some memory.", owner=True)] if note else []
    problem = reader.cache_problem()
    if problem:
        out.append(_check("reader-cache", PROBLEM, f"[reader] cache_folder = {settings.get('reader')['cache_folder']!r}: {problem}.",
                          'Connect its disk, or set cache_folder = "" to keep the cache in Hylly\'s own cache folder.', owner=True))
    out += [_check("reader-not-ours", INFO, f"{says}.", reader.NOT_OURS_NEXT, owner=True) for says in reader.not_ours()]
    return out


def _library_rights(root: Path) -> list[dict]:
    if os.name == "nt" or not root.stat().st_mode & 0o002:
        return []
    # the owner's: a share's own user, say, may write the library through it
    return [_check("library-rights", INFO, "Anyone on this computer can change the library's folder.", f"chmod o-w {root}", owner=True)]


def _state_copies(root: Path) -> list[dict]:
    """The nightly copies in the library's state folder: the database's, with the users' password hashes, and the
    settings'. On Linux and macOS Hylly makes them their owner's only; this says when others on this computer can read one
    all the same, as far as the rights tell: not on Windows, whose rights are of another kind. On a disk that keeps no
    rights of its own (exFAT, a share, NTFS without permissions), where nothing there has the rights Hylly gave it, chmod
    can't help, and the disk's mount options decide: that's said, not counted as a problem."""
    from . import backup, db

    if os.name == "nt":
        return []
    state, found, kept = paths.state_dir(root), [], []  # kept: what has the private rights Hylly gives, so the disk keeps them

    def reach(modes: list[int]) -> list[int]:  # who can pass through all these folders: the group, others (their bits' places)
        return [shift for shift in (3, 0) if all(mode >> shift & 1 for mode in modes)]

    def look(folder: Path, modes: list[int]) -> None:
        if not reach(modes):
            return
        for path in sorted(folder.iterdir()):
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            if not mode & 0o077:
                kept.append(path)
            if path.is_dir():
                look(path, [*modes, mode])
            elif path.name != "README.txt" and any(mode >> shift & 4 for shift in reach(modes)):  # it says only what they are
                found.append(path.relative_to(state).as_posix())

    for copies in (db.backups(root), backup.settings_backups(root)):
        try:
            if copies.is_dir() and not copies.is_symlink():
                mode = copies.stat().st_mode
                if not mode & 0o077:
                    kept.append(copies)
                look(copies, [root.stat().st_mode, state.stat().st_mode, mode])
        except OSError:  # what can't be looked into, it can't tell of
            continue
    if not found:
        return []
    says = f"{many(len(found), 'file')} of the nightly copies in {state}, such as {found[0]}"
    if not kept:
        return [_check("state-copies", INFO, f"Others on this computer may read {says}, and nothing there has the rights Hylly gave it: this disk "
                                             "seems to keep no rights of its own (exFAT, a share, NTFS without permissions), so its mount options decide.",
                       "Mount it for your user alone: uid= with umask=077 (or fmask and dmask), or for a share, its file_mode and dir_mode.",
                       owner=True)]  # the mount's options: sudo
    return [_check("state-copies", PROBLEM, f"Others on this computer can read {says}: the database's copy has the users' password hashes.",
                   f'chmod -R go-rwx "{db.backups(root)}" "{backup.settings_backups(root)}"',
                   run=(*PRIVATE, *_absolute(db.backups(root), backup.settings_backups(root))))]


def _address() -> list[dict]:
    """What applies wherever the server runs, as notes, never as problems: how it is reached is a choice, and
    who can log in is _access's to say. The address people use ([server] address) not https while the server listens
    beyond this computer: logins and books cross the network as they are (an e-reader's browser may know no other way).
    Every address listened on, with no address set: other devices are shown this computer's own, over plain http.
    Forwarding headers from an address not in trusted_proxies, and wrong logins from a trusted proxy's own address
    that no header named another sender for: the running server noted both (serve.NOTED); either way all a proxy's
    visitors are one sender to the lockout. And trusted_proxies holding more than this computer. Each step is the
    owner's: how the server is reached, and whom it trusts, is theirs to choose."""
    import ipaddress

    from . import access, instance

    running = instance.running()
    listen, address = _where(running)
    found = ipaddress.ip_address(listen)
    out = []
    if address and urlsplit(address).scheme != "https" and not found.is_loopback:
        out.append(_check("address", INFO, f"The address people use, {address}, isn't https: names, passwords and books cross the "
                                           "network as they are, which anyone on it could read.",
                          "Fine at home for a device that knows no other way; elsewhere, an https address through Tailscale Serve "
                          "or a proxy that gives it, set as address under [server].", owner=True))
    if found.is_unspecified and not address:
        out.append(_check("address-unset", INFO, f"It listens on every address ({listen}), and no address is set: other devices are "
                                                 "shown this computer's own on the home network, over plain http.",
                          'Set address under [server] to the one people use, such as "https://books.example.org".', owner=True))
    ignored = (running or {}).get("headers_ignored_from") or []
    if ignored:
        out.append(_check("proxy-headers-ignored", INFO, f"Forwarding headers came from {', '.join(ignored)}, not in trusted_proxies under "
                                                         "[server]: they were ignored, so those requests counted as the sender's own.",
                          "If it is a proxy of yours, add it to trusted_proxies and restart the server: otherwise all its "
                          "visitors are one sender to the lockout after wrong passwords.", owner=True))
    unnamed = (running or {}).get("unnamed_by_proxy") or []
    if unnamed:
        out.append(_check("proxy-sender-unnamed", INFO, f"Wrong logins or a lockout came from {', '.join(unnamed)}, in trusted_proxies, with "
                                           "no forwarding header naming another sender: if a proxy or tunnel there hands on "
                                           "none, all its visitors are one sender to the lockout, and a stranger's wrong "
                                           "passwords lock everyone out. A browser on that computer itself looks the same.",
                          "Make the proxy set or add to X-Forwarded-For (nginx: proxy_set_header X-Forwarded-For "
                          "$proxy_add_x_forwarded_for), never pass on the one the visitor sent.", owner=True))
    loopback = {network.version: network for network in access.LOOPBACK}
    wider = [str(n) for n in access.trusted_proxies() if not n.subnet_of(loopback[n.version])]
    if wider:
        out.append(_check("trusted-proxies-wide", INFO, f"trusted_proxies under [server] holds {', '.join(wider)} beyond this computer: any "
                                                        "address there may name another sender in its headers, and is spared the cap on "
                                                        "connections from one address.",
                          "Fine for your proxy's own address or network; nothing wider, such as the whole home network.", owner=True))
    return out


def _walls() -> list[dict]:
    """A server reached at an address set under [server], with the rights of your whole account, and so within reach of
    the shops' logins and the lookups' keys saved here. On Linux, whether the walls around it are there, and hold
    it (examples/hylly.service), which hide them; outside them a note whenever an address is set. Windows and macOS have
    no such walls: a note when an address is set and logins are saved here, with the help's two ways round it."""
    from . import instance

    linux = instance.SYSTEM == "linux"
    if linux and instance.system_service():
        running = instance.running()
        out = []
        if running and not instance.system_owns(running.get("pid")):
            out.append(_check("walls", PROBLEM, f"A server is running outside the walls of the system-wide service (process {running.get('pid', '?')}).",
                              "hylly server stop, and then sudo systemctl start hylly", owner=True))
        out += _walled_lines()
        if any(c["state"] == PROBLEM for c in out):
            return out
        return [*out, _check("walls", OK, "The server is a system-wide service, with walls around it.")]
    address = str(paths.section("server").get("address") or "")
    saved = [s for s in SITES if session_path(s).is_file()]  # in the logins folder: what a server here could read
    if not address or (not linux and not saved):
        return []
    who = ("with Tailscale Funnel by the whole internet, with Serve by your own devices"
           if urlsplit(address).scheme == "https" else "by every device that reaches that address")
    logins = f" The logins and keys saved here, of {', '.join(saved)}, are within its reach too." if saved else ""
    then = ("examples/hylly.service walls it in: the help tells how (The library server)." if linux else
            "A separate standard account for Hylly, the server and its downloads alike, keeps your own files out of its "
            "reach, though not the logins the downloads use there; and its start at logon runs only while that account is "
            "logged in. Keeping the shops' logins on another computer, given only to the download that needs it, keeps "
            "them out. The help tells how (Open to the internet).")
    return [_check("walls", INFO, f"The server can be reached at the address set under [server], with the rights of your whole account: {who}.{logins}",
                   then, owner=True)]  # a service with sudo, another account, or the logins elsewhere: the owner's


def _walled_lines() -> list[dict]:
    """The walled service installed against the settings (instance.service_differences): each line that stops the server
    or loosens its walls, by the user it runs it as, the library, the port, a problem naming its file and line and the
    line wanted, hylly serve --service-file printing the whole service as the settings have it. A file or a drop-in
    folder of it that can't be read is a note, and nothing is compared then; what can't be told here is a note too,
    and the rest is compared. Each step takes sudo: the owner's."""
    from . import instance

    found = instance.service_differences()
    if not found:
        return []
    then = ("Make it {want} in {file}: hylly serve --service-file prints the whole service as the settings have it, to "
            "compare. Then sudo systemctl daemon-reload and sudo systemctl restart hylly.")
    out = [_check("walls", PROBLEM, f"The system-wide service ({d['file']}) doesn't match the settings: {d['says']}.",
                  then.format(**d), owner=True) for d in found["differences"]]
    if found["unread"]:
        out.append(_check("walls", INFO, f"{', '.join(found['unread'])} can't be read here, so the system-wide service isn't "
                          "compared with the settings (the user, the library, the port).",
                          "Run hylly doctor as the user the service names, or make the file readable by it.", owner=True))
    if found.get("skipped"):
        out.append(_check("walls", INFO, "Of the system-wide service, not compared with the settings here: "
                          f"{'; '.join(found['skipped'])}."))
    return out


def _remote() -> list[dict]:
    """Which computer the commands typed here run on: [remote] ssh, HYLLY_REMOTE or -H. A name that can't be used stops
    every command but this one, which runs here to say so (cli._here_or_there)."""
    from . import remote
    from .exits import Exit

    try:
        found = remote.server()
    except Exit as e:
        return [_check("remote", PROBLEM, e.message, "Write [remote] ssh as the server computer's name for ssh, such as "
                       "ssh = 'server' (an alias in ~/.ssh/config) or 'user@host', or ssh = '' to run commands here; unset "
                       "an empty HYLLY_REMOTE.", owner=True)]
    if found is None:
        return []
    return [_check("remote", INFO, f"Commands typed here run on {found[0]}, over ssh: hylly --here runs one here.")]


def _inbox(root: Path | None = None) -> list[dict]:
    """Each Inbox ([inbox], folders.inboxes): whether its folder is there, whether it is the library or inside it by any
    spelling of it (copies.Guard), or another Inbox or inside one (copies.nested), which the pass refuses, where its
    books go by their kind, and how many wait in its Not added folder; and whether anything runs it: the library
    server, or, when that is walled in or leaves its jobs to a timer, hylly server jobs, which must have run it within
    ten minutes (periodic.nobody_runs_the_inbox)."""
    from . import copies, folders, instance, periodic

    found = folders.inboxes(root=root)
    if not found:
        return []
    out, there = [], [inbox for inbox in found if Path(inbox.folder).is_dir()]
    for inbox in found:
        source = Path(inbox.folder)
        if not source.is_dir():
            out.append(_check("inbox-missing", PROBLEM, f"The Inbox, {inbox.folder}, isn't there, so its books can't "
                              "come in.", "Make the folder, or connect the disk or share it is on: Hylly makes nothing "
                              "there itself, as an unmounted disk's place must stay empty.", owner=True))
            continue
        if root is not None and copies.Guard(root).overlaps(source):
            out.append(_check("inbox-in-library", PROBLEM, f"The Inbox, {inbox.folder}, is the library or inside it, by "
                              "its path or another way to the same folder, so nothing is taken from it.",
                              "Set folder under [inbox] to a folder outside the library.", owner=True))
            continue
        other = next((other for one, other in copies.nested(there) if one is inbox), None)
        if other is not None:  # as the pass refuses them, and says in its log
            out.append(_check("inbox-overlap", PROBLEM, f"The Inbox, {inbox.folder}, and another, {other.folder}, are one "
                              "folder, or one is inside the other, so a book in it would be taken twice: nothing is "
                              "taken from either.", "Name Inboxes apart under [inbox], neither inside another.",
                              owner=True))
            continue
        aside = source / folders.NOT_ADDED
        held = sum(1 for p in aside.rglob("*") if p.is_file() and p.suffix != ".txt") if aside.is_dir() else 0
        out.append(_check("inbox", INFO, f"{inbox.folder}: {inbox.where()}" + (f"; {many(held, 'file')} "
                          f"set aside in {folders.NOT_ADDED}, each with a note saying why" if held else "") + ".",
                          f"Read the notes in {aside}, put each book right, and put it back in the Inbox." if held else "",
                          owner=bool(held)))  # the books moved by hand
    walled = instance.system_service()
    nobody = periodic.nobody_runs_the_inbox(walled)
    if nobody is not None:  # a timer to install, with sudo
        out.append(_check("inbox-runner", PROBLEM, *nobody, owner=True))
    elif walled or periodic.external():
        since = time.time() - (periodic.inbox_last() or time.time())
        out.append(_check("inbox-runner", OK, f"hylly server jobs runs it from a timer: last {time_words(max(since, 60))} ago."))
    elif instance.running():
        out.append(_check("inbox-runner", OK, "The library server runs it, once a minute."))
    else:  # whether and how the owner's server runs is theirs: two ways, and either starts it where the settings say
        out.append(_check("inbox-runner", INFO, "The library server runs it, and isn't running.",
                          "hylly serve --background, or hylly server autostart on", owner=True))
    return out


def _jobs() -> list[dict]:
    from . import runs

    return [_check("job", INFO, f"{job['what']} is running ({runs.describe(job)}).") for job in runs.running()]


def _alarms() -> list[dict]:
    from . import bell

    try:
        conf = bell.settings()
    except SystemExit as e:
        return [_check("alarms", PROBLEM, str(e))]
    if bell.weak_topic(conf):  # the topic is a secret: the owner's own to set
        out = [_check("alarms", PROBLEM, "The ntfy topic is short enough to guess, and whoever does can read the alarms.", "hylly bell, in a terminal, suggests a stronger one.",
                      owner=True)]
    else:
        out = [_check("alarms", INFO, f"They go to: {', '.join(bell.channels(conf))}.")]
    return out + _alarm_command(conf["command"])


def _alarm_command(words: list[str]) -> list[dict]:
    """[bell] command, said once however often it failed: its program, or a file it's given, where the server can write
    (bell.command_refused), so it isn't run; its program not found here (by the PATH of whoever runs this, which a
    service's may not be); else bell.log's latest word on it, when that is a failure (bell.command_trouble)."""
    from . import bell

    if not words:
        return []
    refused = bell.command_refused(words)
    if refused:
        return [_check("alarm-command", PROBLEM, f"The alarm command: {refused}.", "Put it outside Hylly's own folders and the "
                       "library, and name it there in command under [bell].", owner=True)]
    program = words[0]
    if any(c in program for c in "\\/"):
        found = Path(program).is_file() and os.access(program, os.X_OK)
    else:
        found = shutil.which(program) is not None
    if not found:
        return [_check("alarm-command", PROBLEM, f"The alarm command's program, {program}, isn't found on this computer, so it can't "
                       "run for an alarm.", "Install it, or name it by its full path in command under [bell]; hylly bell test "
                       "tries it.", owner=True)]
    trouble = bell.command_trouble()
    if trouble:  # no run: hylly bell test sends the owner's phone a note too
        return [_check("alarm-command", PROBLEM, f"The alarm command failed at {trouble[0]}: {trouble[1].rstrip('.')}.", "hylly bell test runs "
                       "it again and says what went wrong; bell.log has each time it failed.")]
    return []


LOOKED_INTO = 200  # of the catalog's CBR files, at most this many are opened, for their first four bytes


def _cbr_comics(root: Path | None, cat: Catalog | None = None) -> tuple[int, str, int] | None:
    """The catalog's CBR files, by its paths, never by walking the library: how many it lists, the first that is a RAR
    archive by its first bytes, as only those need 7-Zip and its RAR part (a ZIP named .cbr, as many are, needs
    neither), and how many weren't read, beyond the first LOOKED_INTO or not readable now, which may be RAR archives
    too. A file gone, or one outside the library, is the catalog's own checks' to say. None without a catalog to go by.
    The catalog as cat reads it (_catalog)."""
    from .library import Catalog, catalog_files

    if root is None or not (paths.catalog_dir(root) / "data.js").is_file():
        return None
    try:
        listed = [rel for rel in catalog_files((cat or Catalog(root)).get()) if rel.lower().endswith(".cbr")]
    except (OSError, ValueError, SystemExit):
        return None
    unread = max(len(listed) - LOOKED_INTO, 0)
    for rel in listed[:LOOKED_INTO]:
        f = paths.library_file(root, rel)
        try:
            if f is None or not f.is_file():  # nor a pipe, which would wait for a writer
                continue
            with open(f, "rb") as fh:
                if fh.read(4) == b"Rar!":
                    return len(listed), rel, 0
        except OSError:
            unread += 1
    return len(listed), "", unread


def _cbr_said(found: tuple[int, str, int] | None) -> tuple[bool, str]:
    """For a 7-Zip that can't open RAR archives, or none: whether the catalog's CBR files make it a problem, and the
    words that end its line, from ": " or ". "; of no catalog at all, nothing."""
    from . import catalog

    if found is None:
        return False, "."
    n, rar, unread = found
    if not n:
        return False, ". The catalog lists no CBR file now."
    if rar:
        rar = catalog.shown(catalog.shown_name(rar))
        return True, (f": the catalog lists {many(n, 'CBR file')}, a RAR archive, {rar}." if n == 1 else
                      f": the catalog lists {many(n, 'CBR file')}, and RAR archives among them, such as {rar}.")
    if unread:
        return True, (f": the catalog lists {many(n, 'CBR file')}, and those read are ZIPs, which don't need it, but {unread:,} "
                      f"{'was' if unread == 1 else 'were'}n't read, and may be RAR archives.")
    return False, f". The catalog's {many(n, 'CBR file')} {'is a ZIP' if n == 1 else 'are ZIPs'}, which {'doesn' if n == 1 else 'don'}'t need it."


def _tools(root: Path | None = None, cat: Catalog | None = None) -> dict:
    """7-Zip, which a CBR comic that is a RAR archive needs, as most are, and its RAR part, which most of those need
    too: without it only a comic whose pages are stored uncompressed opens. Some systems' packages leave the part out
    (archives.no_rar_part). Either is a problem when the last catalog lists CBR files that are RAR archives, and a note
    otherwise. 7-Zip is run once, for its version and what it unpacks with (the update check runs it once more, for
    its version alone: updates.seven_zip_version). What to install, and a look at a comic, are the owner's. The catalog
    as cat reads it (_catalog)."""
    from . import instance

    tool = archives.sevenzip()
    if not tool:
        problem, end = _cbr_said(_cbr_comics(root, cat))
        return _check("7-zip", PROBLEM if problem else INFO, "Not found: a CBR comic that is a RAR archive, as most are, "
                      f"can't be read in the page, or get its cover{end}" + ("" if problem else " Other formats don't need it."),
                      archives.no_sevenzip(), owner=True)
    have, rar = archives.sevenzip_info(tool)  # 7-Zip's version and its RAR part: the reader asks too
    numbered = f" ({have[0]}.{have[1]:02d})" if have else ""
    if rar:
        return _check("7-zip", OK, f"It's there{numbered}, with its RAR part, for CBR comics.")
    if rar is None:
        then = "Open a CBR comic in the library page to see."
        if instance.SYSTEM == "mac":
            then += (" On macOS, a 7zz from 7-zip.org may be held back the first time it runs, as a program from the "
                     "internet: allow it in System Settings, Privacy & Security (Open Anyway), or on an older macOS open it "
                     "once from Finder (right-click, Open), then run hylly doctor again.")
        return _check("7-zip", INFO, f"It's there{numbered}, for CBR comics, but didn't list what it unpacks with, so "
                      "whether it has the RAR part most of them need isn't known.", then, owner=True)
    problem, end = _cbr_said(_cbr_comics(root, cat))
    return _check("7-zip", PROBLEM if problem else INFO, f"It's there{numbered}, but without its RAR part, so a CBR comic "
                  "that is a RAR archive opens, and gets its cover, only if its pages are stored uncompressed, as most "
                  f"aren't{end}", archives.no_rar_part(), owner=True)


def _drawing() -> dict:
    """That a worker starts, and what it draws with: the libraries really in use, whatever else is installed."""
    from . import pdfworker, updates

    try:
        found = pdfworker.versions()
    except pdfworker.NoWorker as e:
        return _check("pdfs", PROBLEM, f"The part of Hylly that draws them can't start: {e}", updates._sync())
    except (pdfworker.Stopped, pdfworker.NotNow) as e:
        return _check("pdfs", PROBLEM, f"The part of Hylly that draws them didn't answer ({type(e).__name__}).", f"Try again; if it goes on: {updates._sync()}")
    limit = "" if found.get("limited") else " This system takes no limit on its memory."
    return _check("pdfs", OK, f"Drawn apart from the server, by MuPDF {found.get('mupdf', '?')} (PyMuPDF {found.get('pymupdf', '?')}).{limit}")


def _behind() -> list[dict]:
    """What is older than it should be (updates.behind), each by its own id: update- and its name, as CHECKS has them.
    A 7-Zip to install is the owner's; installing Hylly again is any one's who installed it, but no one command an
    assistant may run as it stands."""
    from . import updates

    return [_check("update-" + re.sub(r"[^a-z0-9]+", "-", b["what"].lower()).strip("-"), UPDATE,
                   f"{b['have']}, older than {b['want']}. {b['why']}", b["next"], owner=b["what"] == "7-Zip")
            for b in updates.behind()]


def _safely(key: str, look) -> list[dict]:
    """A mistake one look trips over is a finding of the doctor's, under the id of the look's first check, and the
    other looks still go on."""
    try:
        found = look()
        return found if isinstance(found, list) else [found]
    except (SystemExit, Exception) as e:  # noqa: BLE001
        return [_check(key, PROBLEM, str(e) or type(e).__name__)]


def report(library: str | None = None) -> dict:
    checks = [_check("version", INFO, f"{version()}, on Python {platform.python_version()}, {platform.system()} {platform.machine()}.")]
    checks += _safely("settings", _settings)
    found, root = _library(library)
    checks += found
    cat = None
    if root:
        from .library import Catalog

        cat = Catalog(root)  # data.js read once for every look at it, each saying what it finds wrong with it
        checks += _safely("catalog", lambda: _catalog(root, library, cat))
        checks += _safely("catalog-files", lambda: _underneath(root, library))
        checks += _safely("books-unopenable", lambda: _cant_open(root, cat))
        checks += _safely("books-refused", lambda: _refused(root, library, cat))
        checks += _safely("downloads-failed", lambda: _failures(root))
        checks += _safely("questions", lambda: _questions(root))
        checks += _safely("library-rights", lambda: _library_rights(root))
        checks += _safely("state-copies", lambda: _state_copies(root))
        checks += _safely("database", lambda: _people(root))
        checks += _safely("backup", lambda: _backup(root))
    for key, look in (("places", _places), ("reader-memory", _pages_memory), ("rights", _rights), ("settings-owner", _private), ("remote", _remote),
                      ("address", _address), ("logins", _logins), ("server", _server), ("access", _access), ("walls", _walls),
                      ("inbox", lambda: _inbox(root)), ("job", _jobs), ("alarms", _alarms), ("7-zip", lambda: _tools(root, cat)),
                      ("pdfs", _drawing), ("updates", _behind)):
        checks += _safely(key, look)
    seen = set()
    checks = [c for c in checks if c["state"] != PROBLEM or not (c["says"] in seen or seen.add(c["says"]))]  # one mistake, said once
    return {"version": version(), "python": platform.python_version(), "system": sys.platform,
            "problems": sum(c["state"] == PROBLEM for c in checks), "updates": sum(c["state"] == UPDATE for c in checks), "checks": checks}


def text(result: dict) -> str:
    mark = {OK: "ok  ", INFO: "    ", UPDATE: "old ", PROBLEM: "NOT "}
    lines = [f"{mark[c['state']]}{c['what']}: {c['says']}" + (f"\n      Next: {c['next']}" if c.get("next") else "") for c in result["checks"]]
    n, old = result["problems"], result["updates"]
    lines += ["", *([f"{many(n, 'thing')} to put right."] if n else []), *([f"{many(old, 'thing')} to update."] if old else []),
              *([] if n or old else ["All well."]),
              "This shows no logins or keys, but it does show your folders: look before you paste it anywhere."]
    return "\n".join(lines)
