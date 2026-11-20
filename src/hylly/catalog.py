"""The catalog. One record per title across every part of the library, kept in the database (the tables
title, title_file and title_alias; keep) and written out as catalog\\data.js (page), which the library server reads
and lays your tags and corrections over before the library page gets it (library.Catalog), with cover thumbnails in
catalog\\covers\\.

Sources: Humble (plan + manifest), StoryBundle, DriveThruRPG and itch.io (snapshot + manifest), and your own
folders (folders.py; one record per book; same-name files in several formats are one, and so
are copies of one book in several places: see merge_copies). A shop's title is of the kind the rules find for it, by
the same rule at every shop (rules.Rules.kind_of, kind_of here), and where any title is best read is worked out once,
after it is read, by one function (device_of), with your correction of its kind if you made one.
Metadata from each title's best file is cached in catalog\\cache\\files.jsonl by path, size and date,
so a run only reads new or changed files; that of a Humble title's other book files, read for what its main file
lacks (fill_details), in catalog\\cache\\details.jsonl, and whether a file can be opened at all in
catalog\\cache\\opens.jsonl. hylly catalog --rebuild sets the first two aside, with the covers, until its build is kept
(rebuilding). Online lookups (hylly lookup, or
--lookup here) are cached in state\\lookups\\lookup-<source>.jsonl (see lookups/); the build only reads those
answers, so it works offline.
What the build's rules can't settle by themselves (two copies that may be one book, a lookup's near miss, edits whose
title is gone, files of a kind the rules don't take in a folder of yours) it guesses as before and keeps as questions
(questions_of, in curation's list), and your answers steer the next build (same_pairs, lookups.Answers, folder_items).
Everything under catalog\\ can be deleted; the next run rebuilds it.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import hashlib
import io
import json
import os
import re
import shutil
import stat
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from . import curation, db, folders, lookups, matching, meta, paths, plan, rules, settings, snapshot
from .shops import SHOPS, labels as shop_labels
from .exits import LOCAL, NEEDS_YOU, Exit
from .library import COVER_SITE
from .paths import CLUTTER_FOLDERS, TOOLS_FOLDERS
from .storage import CONTROLS, TidiedRecords, atomic_write, retrying, share_is_gone, shown  # shown: a path for a line
from .words import many

THUMB_WIDTH = 200
# Which files are books, the genres and the games of RPG titles are rules: formats.toml, genres.toml and games.toml
# (rules.current()); which of a title's files is its main one goes by the settings' order ([download]). The game of an
# RPG title is DriveThruRPG's own Rule System when it lists one, else a game's name in the title, series or bundle;
# DriveThruRPG products, and the titles of every shop on the shops' core, also look in their description, and take their
# bundle's game (see shop_games).
ISBN_IN_URL = re.compile(r"97[89]\d{10}")


def genres_of(*texts: str) -> list[str]:
    """The genres whose words the texts have (genres.toml), in its order."""
    return rules.current().genres_of(*texts)


def game_name(name: str) -> str:
    """A shop's name for a game, as the Game filter shows it (games.toml)."""
    return rules.current().game_name(name)


def game_names(*texts: str, prose: bool = False) -> list[str]:
    """Known game names in the texts; in prose (a description), not those that are ordinary words too."""
    return rules.current().game_names(*texts, prose=prose)


def games_of(item: Item, *texts: str, kind: str | None = None) -> list[str]:
    """The games of an RPG title (kind: your correction of the kind, if any): the shop's own (DriveThruRPG's
    Rule System), else known names in the texts."""
    if (kind or item.kind) != "rpg":
        return []
    if item.games:
        return list(dict.fromkeys(game_name(g) for g in item.games))
    return game_names(*texts)


def shop_games(products: list[dict]) -> dict[str, list[str]]:
    """Each title's games, of a shop on the shops' core (a DriveThruRPG product, say): the shop's own (DriveThruRPG's
    Rule System), else names in its title, else in its description. A title still without one takes its bundle's, when
    at least half of the bundle has a game and all of them the same."""
    own = {p["id"]: [game_name(g) for g in p.get("games") or []] or game_names(p["name"])
           or game_names(p.get("description", ""), prose=True) for p in products}
    bundles: dict[int, list[str]] = {}
    for p in products:
        if p.get("bundle"):
            bundles.setdefault(p["bundle"], []).append(p["id"])
    for ids in bundles.values():
        found = {tuple(own[i]) for i in ids if own[i]}
        if len(found) == 1 and 2 * sum(1 for i in ids if own[i]) >= len(ids):
            shared = list(found.pop())
            for i in ids:
                own[i] = own[i] or shared
    return {i: list(dict.fromkeys(g)) for i, g in own.items()}


@dataclass
class Item:
    id: str
    source: str  # a shop (shops.SHOPS) or one of your folders, by its name
    kind: str  # one of the kinds in the rules ([kinds.X]), as found: your correction is laid over it (library.Catalog)
    title: str
    files: list[tuple[str, str, int]]  # (path relative to the library root, ext, size)
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    bundle: str = ""
    purchased: str = ""
    device: str = ""  # where it is best read (device_of), once it is read: build
    collection: str = ""
    isbn: str = ""
    cover_url: str = ""  # the shop's own cover, used when the file has none
    bonus: bool = False
    games: list[str] = field(default_factory=list)  # the shop's own game names (DriveThruRPG's Rule System)
    subjects: list[str] = field(default_factory=list)  # the shop's own subjects, before the file's
    description: str = ""  # the shop's, used when the file has none
    named: bool = False  # the file name is the book's own title (folders.Folder.named)
    genre_folder: bool = False  # the collection folder is the genre, as named (folders.Folder.genres)
    meta: dict = field(default_factory=dict)  # filled from the best file
    again: bool = False  # it couldn't be read just now: the next run tries it again
    bad: dict[str, str] = field(default_factory=dict)  # its files that can't be opened, and why (meta.cant_open)
    prefer: str = ""  # the format of the file its download stands on: Humble's chosen one, or what came in its place
    extras: set[str] = field(default_factory=set)  # its files that are extras, not the book (Humble's maps, bonus PDFs)
    one_book: bool = False  # its book files are one book in several formats (Humble's): fill_details may join them

    @property
    def primary(self) -> tuple[str, str, int]:
        """The main file, read for its details and its cover, of its book files (an extra only when it has none): of
        those Hylly can read (meta.READERS, a ZIP package among them; of all, when it can read none), the one in the
        format its download stands on (prefer), else in the format its kind is kept in first, as a download picks it
        ([download] in the settings: settings.pick_file). Of that format the first of its files as its source lists
        them: Humble's chosen one, the others' largest."""
        books = [f for f in self.files if f[0] not in self.extras] or self.files
        ext = settings.pick_file(self.kind, readable(books), self.prefer)[0][1]
        return next(f for f in books if f[1] == ext)


def readable(files: list) -> list:
    """The files, or a ZIP package's books, whose details and cover Hylly can read (meta.READERS); all of them when it
    can read none: never an itch.io item's readme or picture beside its book, nor an FB2 or DjVu beside a PDF or CBZ."""
    return [f for f in files if f".{f[1]}" in meta.READERS] or list(files)


# ---- sources ------------------------------------------------------------------------------

def kind_of(default: str, bundles: list[str], maker: str, files: list[tuple[str, str, int]]) -> str:
    """The kind of a shop's title (not Humble's, which its plan finds, by every format Humble offers), as its download
    finds it (shopping.classify): the rules' (Rules.kind_of), by the bundles it came in, who made it and its files as
    downloaded (path, format, size), over default: its shop's own word for it (itch.io's), else its shop's kind."""
    return rules.current().kind_of(default, [b for b in bundles if isinstance(b, str) and b], maker,
                                   [(ext, size) for _, ext, size in files])[0]


def humble_items(root: Path) -> list[Item]:
    from .manifest import Manifest

    state = paths.state_dir(root)
    source = snapshot.latest(state, "humble")
    if source is None:
        return []
    orders = source[1]
    result = plan.for_library(orders, root)
    recorded = Manifest(root, "humble").entries
    in_folder: dict[str, list[str]] = {}
    for rel in recorded:
        in_folder.setdefault(str(PurePosixPath(rel).parent), []).append(rel)
    items = []
    for t in result.titles:
        # every recorded file in the title's folder: the chosen one first, then other formats (a fallback, or an
        # earlier choice of format), then extras
        found = sorted(in_folder.get(t.folder, []), key=lambda rel: (rel != t.path, recorded[rel].get("role", "book") != "book", rel))
        files = [(rel, recorded[rel].get("ext") or PurePosixPath(rel).suffix[1:].lower(), recorded[rel]["size"]) for rel in found]
        if not files:
            continue
        p = t.product
        # the file its download stands on: the chosen one, or the one that came in its place (instead_of) until it comes,
        # or the very file of it under the name it had before Humble renamed it (its MD5: shopping.select's same file)
        md5 = (t.choice.md5 or "").lower() if t.choice else ""
        stands = t.path if t.path in recorded else next(
            (rel for rel in found if recorded[rel].get("instead_of") == t.path), "") or next(
            (rel for rel in found if md5 and str(recorded[rel].get("md5") or "").lower() == md5), "")
        items.append(Item(  # its kind as the rules find it: your correction is laid over it (library.Catalog)
            id=f"h:{p.machine_name}", source="humble", kind=t.found or t.kind, title=p.title, files=files,
            publisher=p.publisher, bundle=p.bundle, purchased=p.purchased, cover_url=_big_icon(p.icon),
            prefer=(recorded[stands].get("ext") or PurePosixPath(stands).suffix[1:]).lower() if stands else "",
            extras={rel for rel in found if recorded[rel].get("role", "book") != "book"}, one_book=True,
        ))
    _humble_isbns(orders, items)
    return items


def _humble_isbns(orders: dict, items: list[Item]) -> None:
    """Publisher links in Humble's order data (the newest snapshot's orders, as humble_items read them) often contain
    the ISBN: /book/9781942270799/..."""
    urls = {}
    for order in orders.values():
        for sub in (order or {}).get("subproducts") or []:
            hit = ISBN_IN_URL.search(sub.get("url") or "")
            if hit:
                urls[f"h:{sub.get('machine_name')}"] = hit[0]
    for item in items:
        item.isbn = urls.get(item.id, "")


def _big_icon(url: str | None) -> str:
    return re.sub(r"[?&].*$", "", url or "")


def shop_cover(url: str | None) -> str:
    """A shop's cover address as it is when it is one, else none: an https address with a host, by the pattern the
    server checks every cover by (library.COVER_SITE; the catalog's part may read the records' part, where it is).
    Anything else, a broken or hostile answer from a shop (http to a device at home, a file: or share address), is no
    cover at all, so the library page never asks for it."""
    return url if isinstance(url, str) and COVER_SITE.match(url) else ""


def storybundle_items(root: Path) -> list[Item]:
    """One item per StoryBundle book with its downloaded files, in its oldest bundle by purchase date (StoryBundle lists
    a book in every bundle it came in), from the newest snapshot of either shape: the shops' core's (shopping.snapshot:
    a book once for each bundle, the bundle as its under) or StoryBundle's own from before it moved onto the core (its
    bundles, each with its books). Its kind by its bundle's words and its file (kind_of), over a book."""
    from .manifest import Manifest

    state = paths.state_dir(root)
    source = snapshot.latest(state, "storybundle")
    if source is None:
        return []
    listed = []  # (bundle, purchased, bonus, book) for each book of each bundle, as the snapshot has them
    for e in source[1]:
        if "books" in e:  # its own shape, from before the core: a bundle with its books
            listed += [(e["name"], e["purchased"], e.get("bonus", False), {**k, "name": k["title"]}) for k in e["books"]]
        else:  # the core's: a book of a bundle
            listed.append((e.get("under", ""), e["purchased"], e.get("bonus", False), e))
    by_book = {}
    for e in Manifest(root, "storybundle").entries.values():
        by_book.setdefault(e.get("item"), []).append(e)
    items, seen = [], set()
    for bundle, purchased, bonus, k in sorted(listed, key=lambda b: b[1]):  # each bundle's books stay in their order
        if k["id"] in seen or k["id"] not in by_book:
            continue
        seen.add(k["id"])
        files = [(e["path"], e["ext"], e["size"]) for e in by_book[k["id"]]]
        kind = kind_of(SHOPS["storybundle"].kind, [bundle], "", files)
        items.append(Item(id=f"s:{k['id']}", source="storybundle", kind=kind, title=k["name"], files=files, bundle=bundle,
                          purchased=purchased, cover_url=k.get("cover", ""), bonus=bonus))
    return items


def shop_items(shop_id: str, root: Path) -> list[Item]:
    """One item per title of a shop on the shops' core (shopping.py) with its downloaded files, the biggest first: a
    DriveThruRPG product's main book is usually its biggest PDF, next to character sheets, maps and printer-friendly
    copies. From the newest snapshot, as the core writes it (shopping.snapshot) and DriveThruRPG's always were, and the
    shop's download records. Its kind by the words of its bundle (under, where the shop names one) and who made it, and
    its files (kind_of), over its own word for it as the snapshot keeps it, else its shop's kind."""
    from .manifest import Manifest

    shop = SHOPS[shop_id]
    state = paths.state_dir(root)
    source = snapshot.latest(state, shop.id)
    if source is None:
        return []
    by_title: dict[str, list[dict]] = {}
    for e in Manifest(root, shop.id).entries.values():
        by_title.setdefault(e.get("item"), []).append(e)
    items = []
    titles = source[1]
    games = shop_games(titles)
    for t in titles:
        found = sorted(by_title.get(t["id"], []), key=lambda e: -e["size"])
        if not found:
            continue
        files = [(e["path"], e["ext"], e["size"]) for e in found]
        items.append(Item(
            id=f"{shop.letter}:{t['id']}", source=shop.id,
            kind=kind_of(t.get("kind") or shop.kind, [t.get("under")], t["publisher"], files),  # its own word, if any
            title=t["name"], publisher=t["publisher"], files=files, purchased=t.get("purchased", ""),
            cover_url=t.get("cover", ""), isbn=t.get("isbn", ""), games=games[t["id"]],
            subjects=t.get("types", []) + t.get("genres", []), description=t.get("description", ""),
            extras={e["path"] for e in found if e.get("role") == "extra"},
        ))
    return items


def itchio_items(root: Path) -> list[Item]:
    """One item per itch.io item with its downloaded files, the biggest first; its creator stands as the publisher
    (often the author too, and the file usually says). From the newest snapshot, as the shops' core writes it (name,
    publisher: shopping.snapshot) or, one from before itch.io was on the core, as itch.io's own did (title, creator).
    Its kind by the words of the bundles it came in and its creator, and its files (kind_of), over the kind of its
    classification ([kinds.X] itchio in the rules: Rules.itchio_kind), else its shop's kind."""
    from .manifest import Manifest

    state = paths.state_dir(root)
    source = snapshot.latest(state, "itchio")
    if source is None:
        return []
    by_item: dict[int, list[dict]] = {}
    for e in Manifest(root, "itchio").entries.values():
        if not e.get("gone"):  # a file itch.io no longer has, and not here (itchio.Library.gone): no file of the title's
            by_item.setdefault(e.get("item"), []).append(e)
    items = []
    for it in source[1]:
        found = sorted(by_item.get(it["id"], []), key=lambda e: -e["size"])
        if not found:
            continue
        files, maker = [(e["path"], e["ext"], e["size"]) for e in found], it.get("publisher", it.get("creator", ""))
        default = rules.current().itchio_kind(it.get("group")) or it.get("kind") or SHOPS["itchio"].kind  # as title_of
        items.append(Item(
            id=f"i:{it['id']}", source="itchio", kind=kind_of(default, list(it.get("bundles") or []), maker, files),
            title=it.get("name", it.get("title")), publisher=maker, files=files,
            bundle=(it.get("bundles") or [""])[0], cover_url=it.get("cover", ""), description=it.get("description", ""),
        ))
    return items


def reader(shop_id: str) -> Callable[[Path], list[Item]]:
    """A shop's reader of its titles: its own, <id>_items, or for a shop on the shops' core, which has none, the core's
    (shop_items)."""
    return globals().get(f"{shop_id}_items") or functools.partial(shop_items, shop_id)


def device_of(item: Item, kind: str | None = None) -> str:
    """Where a title is best read: the one way for every title, of every shop and every folder of yours, worked out
    once, after it is read (build), by plan.device_for. kind: your correction of its kind (hylly set kind), else its
    own. By its main file as that kind picks it (Item.primary, by that kind's list), of the files Hylly can read and
    those an e-reader takes (a book's flowing formats: an FB2 beside a PDF); a ZIP package by the book inside it once
    that is known (read_item), the one the kind's order picks; else by its kind alone: a package of several formats is
    big for its formats, not for its pages."""
    kind = kind or item.kind
    books = [f for f in item.files if f[0] not in item.extras] or item.files
    flowing = rules.current().reflowable & rules.current().book_formats  # what an e-reader takes, as the feed offers it
    ext = settings.pick_file(kind, readable(books) + [f for f in books if f[1] in flowing and f not in readable(books)],
                             item.prefer)[0][1]  # an FB2 too, which Hylly can't read, beside a PDF it can
    _, ext, size = next(f for f in books if f[1] == ext)
    if ext == "zip":
        inner = item.meta.get("_inner") or []
        _, ext, size = settings.pick_file(kind, readable(inner))[0] if inner else (None, ext, None)
    return plan.device_for(kind, ext, size)[0]


def folder_id(rel: str) -> str:
    """A folder title's id, from its first file's path (matching.folder_id)."""
    return matching.folder_id(rel)


USERS_TRASH = re.compile(r"\.trash-\d+")  # a desktop's trash on a disk of its own, one for each user: .Trash-1000


def systems_own(name: str) -> bool:
    """A folder of the systems' own (System Volume Information, $RECYCLE.BIN, lost+found and the like:
    paths.CLUTTER_FOLDERS), which a library at a disk's root has at its top, or of a tool that keeps files beside yours,
    at any depth (paths.TOOLS_FOLDERS, USERS_TRASH): version control's (.git), trash (.Trash-1000, Calibre's .caltrash),
    sync tools' (Syncthing's .stversions, old copies of books changed or deleted). It is never read for books: not by a
    build (book_files, own_folders), not by hylly doctor (unlisted), and its titles aren't missed (shrunk). A shop's title
    whose folder starts with a dot is another thing: its files come from the shop's records, and stay."""
    low = name.lower()
    return low in CLUTTER_FOLDERS or low in TOOLS_FOLDERS or USERS_TRASH.fullmatch(low) is not None


def in_systems_own(path: str) -> bool:
    """A path in the library inside such a folder, at any depth."""
    return any(systems_own(part) for part in PurePosixPath(path).parts[:-1])


def own_folders(root: Path) -> list[folders.Folder]:
    """Your own folders that are read for books: not those of the systems' own."""
    return [own for own in folders.own_folders(root) if not systems_own(own.name)]


# What the system answers of one file it listed that can't be looked at: not there (on Windows, a path longer than it
# takes), a folder on the way that isn't one, a loop of links, a name too long, no right to look, a broken handle; on
# Windows, a name it can't take, a link it can't resolve
CANT_LOOK = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG, errno.EACCES, errno.EPERM, errno.EBADF)
CANT_LOOK_ON_WINDOWS = (123, 1921)


def a_file(f: Path, unseen: list[tuple[str, str]] | None = None, root: Path | None = None) -> os.stat_result | None:
    """What a name a folder lists is, its links followed, when it is a file; None when it isn't. One that can't then be
    looked at goes into unseen as (its path, why), rather than being passed over in silence, as Path.is_file would pass
    over it (on Python 3.14, whatever the trouble): on Windows, a path longer than Windows takes, or a name another
    system wrote that Windows can't take; anywhere, a link that leads nowhere (CANT_LOOK), or one damaged file the disk
    can't answer for (an error of the disk, say) while its folder and the library (root) still answer. A share gone
    (which Windows calls not there too), or a folder or a library that no longer answers, raises: that says nothing of
    the file, and a build stops rather than lose its titles. So each book file is looked at once, though a listing on
    Windows has its size: there that size comes even for a path Windows can't then open."""
    try:
        found = f.stat()
        return found if stat.S_ISREG(found.st_mode) else None
    except OSError as e:
        if share_is_gone(e):
            raise
        if not (e.errno in CANT_LOOK or getattr(e, "winerror", None) in CANT_LOOK_ON_WINDOWS):
            try:  # one file's trouble, or the disk's: told by whether what holds it still answers
                for folder in (f.parent, *([root] if root is not None else [])):
                    folder.stat()
            except OSError:
                raise e from None
        if unseen is not None:
            unseen.append((str(f), e.strerror or type(e).__name__))
        return None


UNSEEN_SHOWN = 3  # of the files and folders that can't be looked at, this many are named


def unseen_words(unseen: list[tuple[str, str]]) -> str:
    """What of the library can't be looked at (book_files' unseen, a folder's path ending in a slash), in words: the
    first few, each with why, as hylly copy names what it can't read. For the build's line and hylly doctor's."""
    n = len(unseen)
    return (", ".join(f"{shown(shown_name(rel))} ({shown(why)})" for rel, why in sorted(unseen)[:UNSEEN_SHOWN])
            + (f" and {n - UNSEEN_SHOWN} more" if n > UNSEEN_SHOWN else ""))


UNSEEN_NEXT = ("On Windows, a path longer than Windows takes can't be looked at, nor anywhere a link that leads nowhere "
               "or a file the disk can't answer for. Renamed, moved or mended, or once Hylly may read them, the next "
               "hylly catalog takes them in")
# A folder that can't be read (book_files' passed, unlisted's), which hylly doctor names with what's in it
FOLDERS_NEXT = ("On Windows, a folder whose name ends in a dot or a space, as Linux and macOS may write it, can't be read "
                "at all: renamed, or once Hylly may read it, the next hylly catalog reads it")


def book_files(base: Path, books: set[str], must: Callable[[str], bool] = lambda folder: True,
               passed: list[str] | None = None, others: dict[str, list] | None = None,
               unseen: list[tuple[str, str]] | None = None, sizes: dict[str, int] | None = None) -> list[Path]:
    """Every book file under one of your folders, in path order, past the systems' own folders (systems_own) and links
    to folders, as os.walk would go. A folder in it that can't be listed raises OSError if it is the folder itself or
    must(its path) says it matters, as when it held titles: Path.rglob passes over one in silence (Python 3.12), and the
    titles of a share that stalled, or of a disk that isn't mounted under it, would quietly leave the catalog. Any other
    is put in passed; without passed, every one raises. others: the files of other suffixes the rules might be unsure of
    (not_a_question leaves out the rest), counted by suffix, each with the first one's path: [count, path]. unseen: a
    book file listed that can't be looked at, with why (a_file). sizes: each book file's size, by its path, as it was
    looked at. Each folder is listed once (os.scandir), what is a folder comes with the listing, and each subfolder is
    asked once whether it is a link, as os.walk asks: on a share, every other look is a round trip."""
    def unreadable(e: OSError) -> None:
        where = Path(e.filename) if e.filename else base
        if where == base or must(str(where)) or passed is None:
            raise e
        passed.append(str(where))

    found = []
    waiting = [(os.fspath(base), False)]  # the folders to list, each with whether it is in a KOReader .sdr folder
    while waiting:
        folder, sidecar = waiting.pop()
        try:
            with os.scandir(folder) as listing:
                entries = list(listing)
        except OSError as e:  # one that can't be listed, or read to its end: none of it, as os.walk has it
            unreadable(e)
            continue
        subs = []
        for entry in entries:
            name = entry.name
            try:
                is_folder = entry.is_dir()
            except OSError:
                is_folder = False
            if is_folder:
                if not systems_own(name):
                    subs.append(entry)
                continue
            if folders.is_book(name, books):
                f = Path(entry.path)
                st = a_file(f, unseen, base.parent)
                if st is not None:
                    found.append(f)
                    if sizes is not None:
                        sizes[str(f)] = st.st_size
                    continue
            if others is not None and not sidecar and not not_a_question(name):
                seen = others.setdefault(Path(name).suffix.lower(), [0, entry.path])
                seen[0] += 1
                seen[1] = min(seen[1], entry.path)
        for entry in reversed(subs):  # in the listing's order, each below its folder: as os.walk goes
            # A link to a folder isn't followed (os.walk's followlinks), by what the disk says of it, as os.walk asks:
            # the listing's own type for a reparse point (a junction, a link Windows made) may not be the disk's, as on
            # Linux's ntfs driver, and a link taken for a folder would be followed, a folder taken for a link lost
            if not os.path.islink(entry.path):
                waiting.append((entry.path, sidecar or entry.name.lower().endswith(".sdr")))  # KOReader's own
    return sorted(found)


PICTURES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".heic", ".avif"})
BESIDE = frozenset({".opf", ".nfo", ".sfv", ".md5", ".sha1", ".sha256", ".ini", ".log", ".bak",
                    ".tmp", ".copying", ".part", ".crdownload", ".partial", ".download", ".ds_store",
                    # the reading apps' own beside a book (Kindle's .mbp, .mbp1, .mbs, .tan, .apnx, .phl), a
                    # file_id.diz, and a stylesheet
                    ".mbp", ".mbp1", ".mbs", ".tan", ".apnx", ".phl", ".diz", ".css"})


def not_a_question(name: str) -> bool:
    """A file in your folders that is no question of whether it's a book (files_to_ask): a name starting with a dot
    (hidden, or macOS's ._ beside each file), the systems' own (Thumbs.db, desktop.ini, .DS_Store: folders.passed_over,
    an open Office file's ~$), a name with no suffix, a suffix the rules know (formats.toml's formats, books or not: .txt;
    [media]'s audio, video and programs; a package's [packages] ignore), a picture (a cover or a scan beside a book), and
    what tools and reading apps keep beside books (BESIDE: Calibre's metadata.opf, a Kindle's .mbp and .apnx, a
    checksum, file_id.diz, a stylesheet, a copy or download still under way: its .copying, .part). What is inside a
    KOReader .sdr folder isn't asked about either (book_files). A web page, an office document, a .chm, .pdb or .rar
    may be a book: asked."""
    from . import folders

    suffix = PurePosixPath(name).suffix.lower()
    if name.startswith((".", "~$")) or folders.passed_over(name) or not suffix or suffix in PICTURES or suffix in BESIDE:
        return True
    r, bare = rules.current(), suffix[1:]
    return (bare in r.formats or bare in r.audio_formats or bare in r.video_formats or bare in r.programs
            or suffix in r.package_ignore)


def held_folders(before: matching.Before) -> set[str]:
    """Every folder the last build had a title's file in, at any depth: in the library, compared as matching.folded."""
    return {matching.folded(str(folder)) for files in before.files.values() for path, _ in files
            for folder in PurePosixPath(path).parents if str(folder) != "."}


def folder_items(root: Path, held: set[str] = frozenset(), passed: list[str] | None = None,
                 misnamed: list[str] | None = None, others: dict[str, dict[str, list]] | None = None,
                 taken: dict[str, set[str]] | None = None, unseen: list[tuple[str, str]] | None = None) -> list[Item]:
    """Your own folders (folders.py): one item per book, same-name files in several formats together (_stem_key), the
    first in path_order leading on every system; the first subfolder is the collection, or with genres the genre, and
    with kinds it can set the kind. A folder of the systems' own at the top is none of yours. One of yours that can't be
    listed, or a folder in it that held a title in the last build (held: held_folders), stops the build (OSError);
    another folder in it is passed over and put in passed, as a path in the library, for the build to say so (without
    passed, it stops too). A file whose path isn't proper text (proper_name) is passed over too, before any title is
    made, and put in misnamed as it can be shown (shown_name). taken: the suffixes you said are a folder's books too
    (curation.folder_books: a files question answered), by its name; else read from your answers. others: the files of
    each folder the rules might be unsure of, by its name and their suffix (book_files). unseen: a book file listed that
    can't be looked at (a_file), as a path in the library, with why: passed over, for the build to say so."""
    items = []
    books = rules.current().book_suffixes
    if taken is None:
        try:
            taken = curation.folder_books(curation.load(root))
        except (OSError, db.NotHere, db.Newer, db.Unusable):  # records kept elsewhere: as the rules have it alone
            taken = {}

    def in_library(folder: str) -> str:
        try:
            return Path(folder).relative_to(root).as_posix()
        except ValueError:
            return folder

    def mattered(folder: str) -> bool:  # it held titles, or it can't be told: then it stops the build
        where = in_library(folder)
        return where == folder or matching.folded(where) in held

    for own in own_folders(root):
        base = root / own.name
        if not base.is_dir():
            continue
        groups: dict[tuple[str, str], list[tuple[Path, str]]] = {}
        unread: list[str] | None = None if passed is None else []
        looked: list[tuple[str, str]] | None = None if unseen is None else []
        found, sizes = [], {}  # each file with its path in the library, made once; each one's size as the walk found it
        theirs = None if others is None else others.setdefault(own.name, {})
        for f in book_files(base, books | taken.get(own.name, set()), mattered, unread, theirs, looked, sizes):
            rel = f.relative_to(root).as_posix()
            if proper_name(rel):
                found.append((f, rel))
            elif misnamed is not None:
                misnamed.append(shown_name(rel))
        for f, rel in sorted(found, key=lambda one: path_order(one[1])):
            key = (str(f.parent).lower(), _stem_key(f.stem))
            groups.setdefault(key, []).append((f, rel))
        if passed is not None and unread:
            passed += [in_library(folder) for folder in unread]
        if unseen is not None and looked:
            unseen += [(in_library(path), why) for path, why in looked]
        for files in groups.values():
            # a file of a suffix you took as books never leads a title that has one of the rules' own formats: its id and
            # main file stay as they were before your answer
            files = sorted(files, key=lambda one: one[0].suffix.lower() not in books)
            parts = PurePosixPath(files[0][1]).parts
            collection = parts[1] if len(parts) > 2 else ""
            item = Item(
                id=folder_id(files[0][1]), source=own.name,
                kind=own.kind_of(collection), title="",
                files=[(rel, f.suffix.lower()[1:], sizes[str(f)]) for f, rel in files],
                collection=collection, named=own.named, genre_folder=own.genres,
            )
            items.append(item)
    return items


def folder_title_id(root: Path, rel: str) -> str:
    """The id the next build gives the title of a file in a folder of yours (rel: its path in the library), as
    folder_items groups the files: that of the first, in path_order, of the book files beside it whose names make one
    title with its own (_stem_key). For the Inbox's pass, which gives a book just copied in its tags and kind before
    the build that catalogues it. A folder that can't be listed just now: the file's own."""
    here = PurePosixPath(rel)
    books, key = rules.current().book_suffixes, _stem_key(here.stem)
    try:
        with os.scandir(root / here.parent) as listing:
            beside = [f"{here.parent}/{e.name}" for e in listing if folders.is_book(e.name, books)
                      and _stem_key(PurePosixPath(e.name).stem) == key and e.is_file()]
    except OSError:
        beside = []
    return folder_id(min([rel, *beside], key=path_order))


def proper_name(rel: str) -> bool:
    """A path that is proper text. On Linux a name is bytes, and one written in an older encoding (Latin-1, say) isn't
    UTF-8: Python then gives its odd bytes as stand-ins that no id, database or page can take."""
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def shown_name(rel: str) -> str:
    """A path that isn't proper text, as it can be shown and printed: its odd bytes as \\xff and the like."""
    try:
        return rel.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    except UnicodeEncodeError:  # a stand-in of another kind, as a name from Windows may have
        return rel.encode("utf-8", "backslashreplace").decode("utf-8")


def path_order(rel: str) -> tuple[list[str], list[str]]:
    """Where a file comes among your folders' files, the same on every system: by its path in one Unicode form (NFC),
    folder by folder as paths sort, without regard to case, then as written. A title's first file gives it its id
    (folder_id), and Windows sorts paths without regard to case where Linux and macOS go by code point: MOONWAKE.pdf and
    Moonwake.epub would be one title with two ids."""
    parts = unicodedata.normalize("NFC", rel).split("/")
    return [part.casefold() for part in parts], parts


def _stem_key(stem: str) -> str:
    """Which files in one folder are one title: their names' words run together, as _words has them but with the
    accents kept, so that case, spaces, punctuation and the Unicode form a name is written in don't count ("Some_Book",
    "Some Book"), in any script ("Книга 1" and "Другая 1" are two). Two books whose names differ by an accent alone stay
    apart (Tähti and Tahti, ガラス and カラス, Мой and Мои): a book's files are named alike."""
    return "".join(_name_words(rules.nfc(rules.nfc(stem).casefold())))  # casefold can take a letter's accent apart


# ---- file metadata and covers --------------------------------------------------------------

class FileCache:
    """Metadata per file path, valid while the file's size and modification time are unchanged: a record file by path
    (storage.TidiedRecords), the last line of a file cached again counting. A write that fails is let go: the file is
    simply read again next time. Tidied at the end of each build."""

    def __init__(self, path: Path):
        self.records = TidiedRecords(path, "path", quiet=True)
        self.path, self.data = path, self.records.rows

    def get(self, rel: str, size: int, mtime: float) -> dict | None:
        e = self.data.get(rel)
        return e if e and e.get("size") == size and abs(e.get("mtime", 0) - mtime) < 2 else None

    def put(self, entry: dict) -> None:
        self.records.put(entry)

    def tidy(self) -> None:
        self.records.tidy()


def thumb_name(item_id: str) -> str:
    return hashlib.sha1(item_id.encode()).hexdigest()[:16] + ".webp"


UNPACKING = "cache/unpacking"  # in the catalog folder: where a book inside a ZIP package is unpacked to be read
LEFT_FOR = 3600  # seconds after which a file there is a leftover of a build that stopped, not one being read


def tidy_unpacking(catalog: Path) -> None:
    """Before and after a build's reading: what a build that stopped (a power cut, a process ended) left unpacked
    removed, a book up to a gigabyte that would otherwise stay in the catalog folder for good, and the folder too once
    it's empty, as a build leaves it. A file still being read, by another computer's build of a library on a share, is
    left alone, and so is its folder. What can't be removed just now is left for the next build."""
    folder = catalog / UNPACKING
    if os.path.islink(folder):  # nothing is removed through a link, which may lead anywhere (meta.unpacked refuses it)
        return
    with contextlib.suppress(OSError), os.scandir(folder) as listing:
        for entry in listing:
            with contextlib.suppress(OSError):
                if entry.name.endswith(meta.UNPACKED) and entry.is_file(follow_symlinks=False) \
                        and time.time() - entry.stat(follow_symlinks=False).st_mtime > LEFT_FOR:
                    os.unlink(entry.path)
    with contextlib.suppress(OSError):  # not there, or not empty
        os.rmdir(folder)


def cover_names(catalog: Path) -> set[str] | None:
    """The covers in catalog/covers, by name, listed once for a build (read_item): on a share, looking for each title's
    cover is a round trip. None when the folder can't be listed just now: each cover is then looked for as needed."""
    try:
        return set(os.listdir(catalog / "covers"))
    except FileNotFoundError:  # none yet, or set aside by a rebuild
        return set()
    except OSError:
        return None


def save_thumbnail(image: bytes, dest: Path) -> bool:
    """False when the image can't be decoded (no cover). Raises OSError when it can't be written:
    the caller then leaves it uncached, so the next run tries again."""
    from .reader import open_picture

    try:
        with open_picture(image) as im:  # only the usual kinds of picture, and no picture bombs
            im.draft("RGB", (THUMB_WIDTH, THUMB_WIDTH * 2))  # a JPEG decodes at the size needed
            im = im.convert("RGB")
            im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=72)
    except Exception:  # noqa: BLE001 - an odd image just means no cover
        return False

    def write():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.getvalue())
    retrying(write)
    return True


def renamed(rel: str, m: dict) -> dict:
    """A file's cached details with what came from its name read again, by the rules in effect now: a change to
    [filename], [junk] or [names] then shows without reading the file again. (A ZIP package's came from the name
    of the book inside, so it stays as read.)"""
    named = list(m.get("named") or [])
    if not m.get("authors") and "authors" not in named:  # none in its details, cached by an early Hylly whose cache
        named.append("authors")  # didn't say what came from the name: such a line isn't read again while its file
        # stays as it was, and would otherwise never take the authors its name gives by the rules now
    if not named or rel.lower().endswith(".zip"):
        return m
    title, authors = meta.from_filename(PurePosixPath(rel).stem)
    m = dict(m)
    if "title" in named:
        m["title"] = title
    if "authors" in named:
        if authors:
            m["authors"] = authors
        else:
            m.pop("authors", None)
    return m


def read_item(root: Path, catalog: Path, cache: FileCache, item: Item, retry_covers: bool = False,
              files: paths.LibraryFiles | None = None, covers: set[str] | None = None) -> None:
    """A title's details and cover, from its main file, each file read once while it stays the same (cache). files: the
    build's look at the library's files (paths.LibraryFiles); covers: the names in catalog/covers as the build listed
    them, each it makes added, so that no title's cover is looked for on the disk (without them, each is)."""
    rel, ext, _ = item.primary
    found = (files or paths.LibraryFiles(root)).file(rel)  # a record's path leading out of the library is never opened
    if found is None:
        return
    f, st = found
    hit = cache.get(rel, st.st_size, st.st_mtime)
    name = thumb_name(item.id)
    cover = catalog / "covers" / name
    there = cover.exists if covers is None else lambda: name in covers
    again = retry_covers and hit is not None and not hit.get("cover") and f.suffix.lower() in meta.READERS \
        and hit["meta"].get("source") != "filename"  # those found unreadable too: an older Hylly kept a share's stall so
    if hit is None or again or (hit.get("cover") and not there()):
        m = meta.read(f, item.kind, catalog / UNPACKING)  # a package's book unpacked on the library's disk
        if m.not_now:  # the file may be fine: what is known of it stays, and it's read again next run
            item.again = True
            if hit is None:
                item.meta = m.to_dict()
                return
        else:
            try:
                has_cover = bool(m.cover) and save_thumbnail(m.cover, cover)
            except OSError:  # couldn't write it even after retries: use the metadata, try the cover next run
                item.meta = m.to_dict()
                return
            if has_cover and covers is not None:
                covers.add(name)
            hit = {"path": rel, "size": st.st_size, "mtime": st.st_mtime, "meta": m.to_dict(), "cover": has_cover}
            cache.put(hit)
    if ext == "zip" and ("inner" not in hit or "pics" not in hit):  # what the package holds: the book inside, for a
        try:                                                           # feed and the reader, or pages as pictures
            listed = {"inner": meta.package_contents(f), "pics": meta.package_pictures(f)}
        except Exception as e:  # noqa: BLE001 - whatever the package, the build goes on
            if meta.passing(e):  # a share's stall says nothing of the package: it's listed again next run
                listed, item.again = {}, True
            else:  # one that can't be listed (not a ZIP, a name said to be UTF-8 that isn't) holds no book
                listed = {"inner": [], "pics": False}
        if listed:
            hit = {**hit, **listed}
            cache.put(hit)
    item.meta = dict(renamed(rel, hit["meta"]))
    item.meta["_cover"] = hit.get("cover", False) and there()
    item.meta["_inner"] = hit.get("inner", [])
    item.meta["_pics"] = hit.get("pics", False)


DETAILS = ("description", "series", "publisher", "isbn")  # what a main file may lack that another of the title's has


def fill_details(root: Path, main: FileCache, details: FileCache, item: Item,
                 files: paths.LibraryFiles | None = None) -> None:
    """The details the main file lacks (DETAILS; a series with its number; the shop's own description counts), from
    the same book in its other formats, first in the e-reader's order ([download] ereader): a Humble comic's or RPG
    book's EPUB beside its CBZ or PDF keeps its description, series, publisher and ISBN. Only a title whose book files
    are one book (one_book: Humble's, never its extras), as a folder of itch.io's or yours may hold other books; and only
    the formats whose reader gives these (meta.DETAILED), never a PDF. Each file read once while it stays the same: its
    details from the main files' cache if it was one, else from this one of its own (details), with no cover. files: the
    build's look at the library's files (read_item)."""
    if not item.one_book:
        return
    files = files or paths.LibraryFiles(root)
    missing = [key for key in DETAILS if not item.meta.get(key) and not (key == "description" and item.description)]
    order = settings.get("download")["ereader"]
    first = item.primary[1]
    others = sorted((f for f in item.files if f[1] != first and f[0] not in item.extras and f".{f[1]}" in meta.DETAILED),
                    key=lambda f: order.index(f[1]) if f[1] in order else len(order))
    for rel, _, _ in others if missing else ():
        found = files.file(rel)
        if found is None:
            continue
        f, st = found
        hit = main.get(rel, st.st_size, st.st_mtime) or details.get(rel, st.st_size, st.st_mtime)
        if hit is None:
            m = meta.read(f, item.kind)
            if m.not_now:  # read again next run
                continue
            hit = {"path": rel, "size": st.st_size, "mtime": st.st_mtime, "meta": m.to_dict()}
            details.put(hit)
        found = hit.get("meta") or {}
        for key in [key for key in missing if found.get(key)]:
            item.meta[key] = found[key]
            if key == "series" and found.get("number"):
                item.meta["number"] = found["number"]
            missing.remove(key)
        if not missing:
            break


def check_files(root: Path, cache: FileCache, item: Item, files: paths.LibraryFiles | None = None) -> None:
    """Which of a title's e-books can't be opened: every file of it, as a reader or a feed may choose any of them, and each
    looked at once while it stays the same. One that can't be read just now is looked at next time. files: the build's
    look at the library's files (read_item)."""
    files = files or paths.LibraryFiles(root)
    for rel, ext, _ in item.files:
        found = files.file(rel) if ext in meta.OPEN_CHECKED else None
        if found is None:
            continue
        f, st = found
        try:
            hit = cache.get(rel, st.st_size, st.st_mtime)
            if hit is None:
                hit = {"path": rel, "size": st.st_size, "mtime": st.st_mtime, "why": meta.cant_open(f)}
                cache.put(hit)
        except OSError:
            continue
        if hit.get("why"):
            item.bad[rel] = hit["why"]


# ---- assembling records ---------------------------------------------------------------------

NAME_CHARS = rules.InWords("+#")  # as words read them, with + and # kept
NAME_WORD = re.compile(r"[^+#]+(?:[+#]+$)?")  # in one of those: a + or # at the end of a word counts, elsewhere not


def _words(text: str) -> list[str]:
    """A name's words, as copies of one book compare them: rules.words_of's, letters and digits of any script, case and
    accents not counting; and a + or # that ends a word counts, so C, C++ and C# are three names. Apart from the words
    a search compares on purpose: there, "C++" finds C."""
    return _name_words(rules.fold(rules.nfc(text or "")))


def _name_words(text: str) -> list[str]:
    """The words of a name already in small letters (_words, _stem_key): an accent that is a mark of its own (one that
    goes on a letter, which no letter holds written together with it) left out, as rules.InWords does."""
    return [w for part in text.translate(NAME_CHARS).split() for w in NAME_WORD.findall(part)]


def tidy_authors(names: list[str]) -> list[str]:
    """'Brennan, Jo' -> 'Jo Brennan' (but 'Acme, Inc.' stays: [names] in names.toml), 'A; B' -> two authors,
    duplicates dropped."""
    keep = rules.current().keep_comma
    out = []
    for name in (part for entry in names for part in entry.split(";")):
        name = re.sub(r"\s+", " ", one_line(meta.mend(name))).strip()  # mended first: mend reads C1 back as letters
        m = re.fullmatch(r"([^,]+),\s*([^,]+)", name)
        if m and not (keep and keep.search(m[2])):
            name = f"{m[2]} {m[1]}"
        if name and name.lower() not in (o.lower() for o in out):
            out.append(name)
    return out


def folder_series_author(item: Item) -> str:
    """'Marshal Kane - The Complete Trail Logs 006.cbr' in comics\\Marshal Kane The Complete Trail Logs\\: the file
    name's "author" is how the folder's name starts, so it is the series, not a person."""
    authors = item.meta.get("authors") or []
    if item.kind != "comic" or item.title or len(authors) != 1:
        return ""
    rel = PurePosixPath(item.primary[0])
    who, folder = _stem_key(authors[0]), _stem_key(rel.parent.name)
    return authors[0] if who and _stem_key(rel.stem).startswith(who) and folder.startswith(who) and folder != who else ""


def titled(item: Item) -> tuple[str, str, str]:
    """(title, series, number): the shop's title, else the file's. In a folder whose files are named by their
    titles, a name like "The Lantern Keeper (Harbor Lights Book 2)" gives the series, which then leaves the title."""
    m = item.meta
    stem = PurePosixPath(item.primary[0]).stem
    own = rules.current().strip_number(m.get("title", "")).strip()  # a list's number: "83.05 The Silent Orbit"
    if own and not meta.good_title(own):  # "test", "00 1234_FM_Intro", a web address: the file name says more
        own = meta.from_filename(stem)[0]
    if folder_series_author(item):
        own = f"{folder_series_author(item)} {own}"
    title = item.title or own or stem
    series, number = m.get("series", ""), m.get("number", "")
    if not series and item.named:
        series, number = meta.series_in_brackets(stem)
        if series:
            title = re.sub(r"\s*\(" + re.escape(series) + r"[^()]*\)\s*$", "", title, flags=re.I) or title
    return title, series, number


def title_from_file(item: Item) -> bool:
    """Whether the title is the file name's: no shop title, and the file's own is missing, unusable or the same
    (reading a file fills a missing title from its name). A folder's named files are named by their titles."""
    if item.title or item.named:
        return False
    r = rules.current()
    own = r.strip_number(item.meta.get("title", "")).strip()
    name = r.strip_number(meta.from_filename(PurePosixPath(item.primary[0]).stem)[0]).strip()
    return not own or not meta.good_title(own) or own == name


def authors_of(item: Item) -> list[str]:
    series = folder_series_author(item)
    return [a for a in item.meta.get("authors") or [] if not meta.junk_author(a) and a != series]  # "Unknown": the lookup fills it


# What a record's text may not keep (one_line): the marks that turn text round or set its direction (the Arabic letter
# mark, LRM and RLM, the embeddings, overrides and isolates), the tags that can hide text no one sees (U+E0000 to
# U+E007F), and the two noncharacters XML can't hold. Not storage.shown's list, the logs' cleaner, which drops every
# format character but the joiners, as nothing may hide in a line a person reads to see what happened: a record is read,
# searched and sent on in every script, and the zero-width space between Thai or Khmer words, the word joiner, the soft
# hyphen and U+180E belong to its text. Nor does a single no-break, narrow no-break or ideographic space go, as French
# and CJK spacing has them.
UNSAFE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufffe\uffff\U000e0000-\U000e007f]")
SPACES = re.compile(r"\s{2,}")


def one_line(text):
    """A text of a record, cleaned once as it came from a file, a shop or a lookup (record): a control character (C0,
    DEL, C1: storage.CONTROLS) dropped, as XML holds none of most of them, but one that is white space (a tab, a line
    break) a space; what UNSAFE names dropped; a run of white space one space; the ends trimmed. What isn't text stays
    as it is. Mended text only: meta.mend reads C1 characters back as the letters they were (tidy_authors,
    meta.clean_text)."""
    if not isinstance(text, str):
        return text
    text = UNSAFE.sub("", CONTROLS.sub(lambda c: " " if c[0].isspace() else "", text))
    return SPACES.sub(" ", text).strip()


def record(item: Item, lookup: dict | None, kind_fix: str | None = None) -> dict:
    m = item.meta
    title, series, number = map(one_line, titled(item))
    # a title of only marks: its file's name; that too (" .epub"): a word and its id, as every card needs a title
    title = title or one_line(PurePosixPath(item.primary[0]).stem) or f"Untitled ({item.id})"
    authors = tidy_authors(authors_of(item))  # mended and cleaned now: a name of only marks leaves room for a lookup's
    comic = item.kind == "comic"
    if not series:
        series, number = meta.series_of(title, trailing_number=comic)
    if series and rules.current().bare_volume.match(title):  # "Volume 8" alone says little outside its series
        title = f"{series} {title}"
    subjects = list(dict.fromkeys(item.subjects + list(m.get("subjects") or [])))
    desc = m.get("description", "") or item.description
    publisher, year, isbn = item.publisher or m.get("publisher", ""), m.get("year", ""), item.isbn or m.get("isbn", "")
    if lookup:  # fills what the files and the shop left empty; never replaces it
        subjects += [s for s in lookup.get("subjects", []) if s not in subjects]
        desc = desc or lookup.get("description", "")
        authors = authors or tidy_authors(lookup.get("authors", []))
        if not series and lookup.get("series") and lookup.get("number"):
            series, number = lookup["series"], lookup["number"]
        publisher, year, isbn = publisher or lookup.get("publisher", ""), year or lookup.get("year", ""), isbn or lookup.get("isbn", "")
    series, number, publisher, year, isbn = map(one_line, (series, number, publisher, year, isbn))  # the lookup's too
    subjects = [s for s in dict.fromkeys(map(one_line, subjects)) if s]
    bundle, collection, purchased = one_line(item.bundle), one_line(item.collection), one_line(item.purchased)
    if item.genre_folder and collection:  # the subfolder you filed it in is the genre: one its words find, else its name
        genres = genres_of(collection)[:1] or [collection]
    else:  # a book outside a subfolder too: by its words, as any title; its title's but where the rules leave them unread
        unread = item.source.casefold() in rules.current().titles_unread  # ([genres] titles_unread: none, shipped)
        genres = genres_of(" ".join(subjects), bundle, collection, "" if unread else title)
    genres = kind_genre(item.kind, genres)
    games = [g for g in dict.fromkeys(map(one_line, games_of(item, title, series, item.title, bundle, collection,
                                                             kind=kind_fix))) if g]  # a shop's own names too
    rec = {
        "id": item.id, "t": title, "a": authors, "s": series, "n": number, "k": item.kind, "src": item.source,
        "g": genres, "gm": games, "sub": subjects[:12], "d": one_line(meta.clean_text(desc)), "p": publisher,
        "y": year, "b": bundle, "dt": purchased, "dev": item.device, "col": collection,
        "bonus": item.bonus, "isbn": isbn, "lk": (lookup or {}).get("lk", []),  # where looked-up details came from
        "tf": title_from_file(item),  # for the page's Missing filter
        "f": [[quote(r), e, s] for r, e, s in item.files],
        "z": [[f"{quote(item.primary[0])}/{quote(n)}", e, s] for n, e, s in m.get("_inner", [])],  # inside the ZIP
        "bad": {quote(r): why for r, why in item.bad.items()},  # no reader can open these (meta.cant_open)
        "zp": quote(item.primary[0]) if m.get("_pics") else "",  # the ZIP is pages as pictures: read like a CBZ
        "c": f"covers/{thumb_name(item.id)}" if m.get("_cover") else shop_cover(item.cover_url),
        "_file": item.primary[1:], "_trust": confirmed(item, title),  # for merge_copies; not written out
    }
    return {k: v for k, v in rec.items() if v not in ("", [], {}, False, None)}


def sort_keys(rec: dict) -> dict:
    """What the library page sorts and groups a title by, made here by the rules so that the page needn't know them: sk,
    its series' key (Rules.series_key: spellings of one series together, as the series feed groups them), and ak, its
    first author's name surname first (Rules.author_key). The server makes them again for a series or an author you
    corrected (library.series_key, library.author_key)."""
    r, out = rules.current(), {}
    if rec.get("s") and (key := r.series_key(rec["s"])):
        out["sk"] = key
    if rec.get("a") and (key := r.author_key(rec["a"][0])):
        out["ak"] = key
    return out


def kind_genre(kind: str, genres: list[str]) -> list[str]:
    """The genre every title of a kind gets ([kind_genres] in genres.toml): first, unless it has one of those
    named (unless, case not counting: "manga" is the genre Manga); only that one, with only = true."""
    g = rules.current().kind_genres.get(kind)
    if not g:
        return genres
    if g.get("only"):
        return [g["genre"]]
    if g["genre"] not in genres and not {u.casefold() for u in g.get("unless", [])} & {x.casefold() for x in genres}:
        return [g["genre"], *genres]
    return genres


def from_name(name: str, bundle: str = "", publisher: str = "", kind: str = "", size: int = 0) -> dict:
    """What the rules make of a file's name, and why, for hylly rules try, which only prints it. Its title, authors,
    series, genres, games and device are the record a build makes of that one file, by record() itself, so what try
    says and what a build does can't differ: a file of a folder of yours named by its book's title (named = true:
    "Title (Series Book 2)" gives the series), with no details of its own, its authors tidied and a title that is only
    "Vol. 3" with its series in front. Its kind is found as a shop's title's is, by its format, bundle and publisher
    (Rules.kind_of), unless kind says; size in bytes, 0 for none. Besides the record, what it doesn't keep: why the
    kind and the device, the format as the rules know it, and what its words would make a shop's file labelled so."""
    from . import inventory

    r = rules.current()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    path = PurePosixPath(name)
    ext = path.suffix.lower().lstrip(".") if path.suffix[1:].isalnum() else ""
    stem = path.stem if ext else name
    label = ext.upper()  # a shop's label is the format's name: "CBZ", "PDF"
    role = inventory.file_role("ebook", ext, label)
    why = ""
    if not kind:
        kind, why = (r.kind_of(rules.DEFAULT_KIND, [bundle], publisher, [(ext, size)]) if role == "book"
                     else (rules.DEFAULT_KIND, ""))
    item = Item(id="try", source="try", kind=kind, title="", files=[(name, ext, size)], bundle=bundle,
                publisher=publisher, named=True)
    item.meta = meta.complete(meta.Meta(source="filename"), stem).to_dict()  # read from its name alone, as no details
    item.device = device_of(item)
    rec = record(item, None)
    said, device_why = plan.device_for(kind, ext, None if ext == "zip" else size)  # why, which device_of doesn't say
    # the name's words in a shop's label instead, such as "PDF (Companion)": [labels] in file_role's order; [languages]
    labelled = next(((x, w) for x in ("audio", "video", "extra") if (w := r.labels[x].find(stem)) is not None), None)
    return {"name": name, "format": ext, "title": rec["t"], "authors": rec.get("a", []), "series": rec.get("s", ""),
            "number": rec.get("n", ""), "kind": rec["k"], "why_kind": why, "known": r.formats.get(ext), "role": role,
            "label": label, "labelled": labelled,
            "language": next((lang for lang, w in r.languages.items() if w.find(stem) is not None), ""),
            "genres": rec.get("g", []), "games": rec.get("gm", []), "device": rec.get("dev", ""),
            "why_device": device_why if said == rec.get("dev", "") else ""}


def confirmed(item: Item, title: str) -> bool:
    """A title good enough to match copies by: the shop's, or one the file name repeats. (A magazine's
    PDFs can all carry the magazine's name as their title; their file names say which issue.)"""
    if item.title or item.named:
        return True
    own = {w for w in _words(title) if len(w) > 2}
    return bool(own) and own <= set(_words(PurePosixPath(item.primary[0]).stem))


# ---- copies of one book in several places ---------------------------------------------------

COPY_MARK = re.compile(r"\s*\(\d\)$")  # Windows' second copy: "Rules Digest(1)"


def same_book(a: dict, b: dict) -> bool:
    """The very same file under one title, or a title both copies confirm with no author against it.
    Manuals only match as the same file: five PDFs titled "Map" can be five different maps."""
    if a["_file"] == b["_file"]:
        return True
    if a["k"] == "manual" or not (a.get("_trust") and b.get("_trust")) or len(" ".join(_words(a["t"]))) < 8:
        return False
    surnames = lambda r: {_words(x)[-1] for x in r.get("a", []) if _words(x)}  # noqa: E731 - "A. B. Smith" ~ "Anna Beth Smith"
    return not (surnames(a) and surnames(b)) or bool(surnames(a) & surnames(b))


def copy_words(title: str) -> str:
    """The words copies of one book share, as merge_copies buckets them: Windows' "(1)" copy mark doesn't count."""
    return " ".join(_words(COPY_MARK.sub("", title)))


def card_kind(kind: str) -> str:
    """The kind a copy is bucketed by: a kind that joins a book's card ([kinds.X] joins_books: tech, RPG) as a book."""
    kinds = rules.current().kinds
    return rules.DEFAULT_KIND if kind in kinds and kinds[kind].joins_books else kind


def merge_copies(records: list[dict], order: list[str] | None = None, pairs: list[tuple[str, str, str]] = ()) -> list[dict]:
    """One record per book: copies of a title in several places become one record listing every file.
    The copy from the source first in order (folders.lead_order) leads, the others fill its gaps, and "cp" says
    where the others are (source, bundle or collection). A copy of a kind that joins a book's card ([kinds.X]
    joins_books: tech books, RPG books) is one with a book's; the others only with their own kind's. pairs: your answers
    about two copies by their ids (same_pairs: id, id, "same" or "apart"), over the rules: the same book's copies are
    one card whatever their kinds and titles say, the lead as the order has it; two apart never share one."""
    order = order or list(SHOPS)
    rank = lambda src: order.index(src) if src in order else len(order)  # noqa: E731
    together = {frozenset(p[:2]) for p in pairs if p[2] == "same" and p[0] != p[1]}
    apart = {frozenset(p[:2]) for p in pairs if p[2] == "apart" and p[0] != p[1]}
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        buckets.setdefault((card_kind(r["k"]), copy_words(r["t"])), []).append(r)

    def joins(group: list[dict], r: dict) -> bool:
        if any(frozenset((x["id"], r["id"])) in apart for x in group):
            return False
        return any(frozenset((x["id"], r["id"])) in together for x in group) or same_book(group[0], r)

    every: list[list[dict]] = []
    for bucket in buckets.values():
        bucket.sort(key=lambda r: (rank(r["src"]), -len(r)))
        groups: list[list[dict]] = []
        for r in bucket:
            group = next((g for g in groups if joins(g, r)), None)
            group.append(r) if group else groups.append([r])
        every += groups
    if together:  # the same book in two buckets (kinds that don't join, titles of other words): one card, in order
        held = {r["id"]: n for n, g in enumerate(every) for r in g}
        for a, b in sorted(tuple(sorted(p)) for p in together):  # in order: the same card whatever the hash seed
            x, y = held.get(a), held.get(b)
            if x is None or y is None or x == y or any(frozenset((p["id"], q["id"])) in apart for p in every[x] for q in every[y]):
                continue
            first, last = min(x, y), max(x, y)
            every[first] = sorted(every[first] + every[last], key=lambda r: (rank(r["src"]), -len(r)))
            every[last] = []
            held.update({r["id"]: first for r in every[first]})
    return [_merged(g) for g in every if g]


def same_pairs(records: list[dict], answered: dict[str, dict]) -> list[tuple[str, str, str, dict, str]]:
    """Your answers about two copies (curation.same_answers: by "same:A|B") as merge_copies takes them, by the ids of
    these records (a build's before they are merged): each side by its id, else, its id changed (a folder title moved or
    renamed), by the one record whose first file has the name and size its answer kept, as a title's edits follow it,
    but for the other side (two copies of one file are named and sized alike). Both sides' ids changed at once, and two
    copies of one file: the two records whose first files have that name and size, together, where no other has them,
    as the answer is of the two whichever is which. Each: the two ids, the answer's code (same or apart), the answer as
    kept, and its key."""
    ids = {r["id"] for r in records}
    by_file: dict[tuple[str, int], set[str]] = {}
    for r in records:
        f = (r.get("f") or [[""]])[0]
        if len(f) > 2 and isinstance(f[2], int):
            by_file.setdefault((matching.file_name(unquote(f[0])), f[2]), set()).add(r["id"])
    out = []
    for key, v in answered.items():
        sides = key[len("same:"):].split("|")
        if len(sides) != 2:
            continue
        found = [side if side in ids else "" for side in sides]
        kept = [(matching.folded(w[0]), w[1]) if isinstance(w := (v.get("w") or {}).get(side), list) and len(w) == 2 else None
                for side in sides]  # each side's first file as the answer kept it: its name and size
        if not any(found) and kept[0] is not None and kept[0] == kept[1]:
            hits = by_file.get(kept[0], set())
            found = sorted(hits) if len(hits) == 2 else found
        for n in range(2):
            if found[n]:
                continue
            hits = (by_file.get(kept[n], set()) if kept[n] is not None else set()) - {found[1 - n]}
            found[n] = next(iter(hits)) if len(hits) == 1 else ""
        if all(found) and found[0] != found[1]:
            out.append((found[0], found[1], v["a"], v, key))
    return out


FRIENDLIEST = ("ereader", "either", "tablet")  # a card's device, of its copies' in other places: the first of these


def _merged(group: list[dict]) -> dict:
    """The lead's record, the others filling its gaps. Its device is the friendliest of its copies' (FRIENDLIEST) where
    they are in more than one place: a title the e-reader suits in one of them is the e-reader's, so the feed, which
    offers the best of every copy's files, lists it. A title in one place keeps the lead's."""
    lead = dict(group[0])
    here = [lead["src"], lead.get("b") or lead.get("col", "")]
    devices = [r["dev"] for r in group if r.get("dev") in FRIENDLIEST]
    if len({r["src"] for r in group}) > 1 and devices:
        lead["dev"] = min(devices, key=FRIENDLIEST.index)
    for other in group[1:]:
        for k in ("a", "s", "n", "d", "sub", "p", "y", "isbn", "c", "dev", "lk", "zp"):
            if k not in lead and k in other:
                lead[k] = other[k]
        for k in ("g", "gm"):  # genres and games: every copy's
            joined = lead.get(k, []) + [g for g in other.get(k, []) if g not in lead.get(k, [])]
            if joined:
                lead[k] = joined
        lead["f"] = lead["f"] + [f for f in other["f"] if f not in lead["f"]]
        if other.get("bad"):
            lead["bad"] = {**lead.get("bad", {}), **other["bad"]}
        if other.get("z"):
            lead["z"] = lead.get("z", []) + [f for f in other["z"] if f not in lead.get("z", [])]
        lead["ids"] = lead.get("ids", []) + [other["id"]]  # your tags on any copy stay with the title
        where = [other["src"], other.get("b") or other.get("col", "")]
        if where != here and where not in lead.get("cp", []):
            lead["cp"] = lead.get("cp", []) + [where]
    return {k: v for k, v in lead.items() if not k.startswith("_")}


# ---- the whole build ----------------------------------------------------------------------

REBUILT = ("covers", "cache/files.jsonl", "cache/details.jsonl")  # what --rebuild sets aside: all that a file is read by again
ASIDE = ".old"  # added to their names while they're set aside (covers.old: backup.COVERS_ASIDE leaves it out of the zip)


@contextlib.contextmanager
def rebuilding(root: Path):
    """hylly catalog --rebuild, around its build: every file read again, as the covers and the caches of the files'
    details (REBUILT) are set aside under their names with .old, not deleted. Only once the library's records are open
    and the rules read, as the build will need both: on a computer that doesn't keep the records (db.NotHere), or with
    a mistake in the rules, nothing is moved; a stop while they're moved puts back what was. Once the build is kept (the
    database has a build more than before), what was set aside goes; if it isn't, as when it's stopped, refused or
    fails, it's put back as it was, and the page has its covers again. What a rebuild that never ended (a power cut)
    set aside stays the one to go back to, and what it made goes; a plain build takes it away (leftovers)."""
    rules.current()
    records = store(root)
    with records.read() as c:
        before = c.execute("SELECT max(seq) FROM catalog_build").fetchone()[0]
    moved: list[tuple[Path, Path, bool]] = []  # each place, its name set aside, and whether there was something there
    doing: tuple[Path, Path] | None = None
    try:
        for place, old in _set_aside_as(root):
            doing, had = (place, old), os.path.lexists(old) or os.path.lexists(place)
            if os.path.lexists(old):
                _gone(place)
            elif had:
                os.rename(place, old)
            moved.append((place, old, had))
            doing = None
    except BaseException as e:  # Ctrl-C among them too: never the covers set aside and the caches not
        _after_the_rebuild(moved, kept=False)
        if doing and not os.path.lexists(doing[0]) and os.path.lexists(doing[1]):  # stopped just as it went aside
            with contextlib.suppress(OSError):
                os.rename(doing[1], doing[0])
        if not isinstance(e, OSError):
            raise
        raise Exit(LOCAL, f"{shown(e.filename or root / 'catalog')} couldn't be set aside for the rebuild ({e.strerror or e}), "
                          "so nothing was rebuilt, and the catalog is as it was. On Windows, another program may have it open: "
                          "close it, and run this again.") from None
    try:
        yield
    except BaseException:
        try:
            with records.read() as c:
                kept = c.execute("SELECT max(seq) FROM catalog_build").fetchone()[0] != before
        except Exception:  # noqa: BLE001 - not known to be kept: put back
            kept = False
        _after_the_rebuild(moved, kept)
        raise
    _after_the_rebuild(moved, kept=True)


def leftovers(root: Path, log=print) -> None:
    """After a plain build is kept: what a rebuild that never ended (a power cut, a process ended) set aside, removed,
    and said once, as this build has read again all that one hadn't, and made each missing cover. What can't be
    removed is said, and stays for the next build."""
    left = [old for _, old in _set_aside_as(root) if os.path.lexists(old)]
    if not left:
        return
    names = ", ".join(shown(p.relative_to(root).as_posix()) for p in left)
    try:
        for old in left:
            _gone(old)
    except OSError as e:
        return log(f"  {names}: set aside by a catalog --rebuild that never ended, and no longer needed, as this build "
                   f"read again what it hadn't; but they couldn't be removed ({e.strerror or e}). Delete them by hand.")
    log(f"  Removed {names}: set aside by a catalog --rebuild that never ended, and no longer needed, as this build read "
        "again what it hadn't.")


def _set_aside_as(root: Path) -> list[tuple[Path, Path]]:
    """Each of REBUILT in the library's catalog folder, with its name while a rebuild has it set aside."""
    places = [paths.catalog_dir(root).joinpath(*rel.split("/")) for rel in REBUILT]
    return [(place, place.with_name(place.name + ASIDE)) for place in places]


def _gone(path: Path) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _after_the_rebuild(moved: list[tuple[Path, Path, bool]], kept: bool) -> None:
    """What a rebuild set aside removed, once its build is kept; else put back in place of what it made. What can't be
    is left: the next rebuild goes back to it, and a plain build makes a missing cover again."""
    for place, old, had in moved:
        with contextlib.suppress(OSError):
            if kept:
                if had:
                    _gone(old)
            else:
                _gone(place)
                if had:
                    os.rename(old, place)


def build(root: Path, *, lookup: bool = False, lookup_limit: int | None = None, workers: int = 6,
          retry_covers: bool = False, log=print, fewer: Callable[[str], bool] | None = None,
          ask: Callable[[list, dict[str, list[str]]], int] | None = None) -> dict:
    """fewer: asked, with what is missing in words, when this build has far fewer titles than the last one (shrunk);
    True keeps it all the same. Without it, or with False, such a build isn't kept (Exit NEEDS_YOU). ask: instead of
    lookup, given the questions about the titles and each title's files by its id, it asks the sources what it will (the
    Inbox, about its new books only: cli._inbox_pass), and says how many it asked."""
    catalog = paths.catalog_dir(root)
    catalog.mkdir(parents=True, exist_ok=True)
    log("Collecting titles...")
    last, passed, misnamed, others, unseen = last_build(root), [], [], {}, []
    edits = curation.load(root)  # your answers about copies, a folder's files, the lookups' matches
    try:
        items = [item for shop in SHOPS for item in reader(shop)(root)]  # each shop's titles, by its reader
        items += folder_items(root, held_folders(last.before), passed, misnamed, others, curation.folder_books(edits),
                              unseen)
    except OSError as e:  # a folder, or the shops' snapshots, that can't be read just now: not a folder without titles
        raise Exit(LOCAL, f"{e.filename or 'A folder of the library'} can't be read just now ({e.strerror or e}), so the "
                          "catalog wasn't built, and it is as it was. Check that its disk or share is there and can be "
                          "read, then run hylly catalog again.") from None
    if passed:
        n = len(passed)
        log(f"  Passed over {many(n, 'folder')} that can't be read and had no titles in the last build: "
            + ", ".join(passed[:3]) + (f" and {n - 3} more" if n > 3 else "") + ".")
    if misnamed:
        n = len(misnamed)
        log(f"  Passed over {many(n, 'file')} whose name isn't proper text (written in an older encoding, "
            "its odd bytes shown as \\x..): " + ", ".join(map(shown, misnamed[:3])) + (f" and {n - 3} more" if n > 3 else "")
            + f". Renamed, the next hylly catalog takes {'it' if n == 1 else 'them'} in.")
    if unseen:  # listed, and then nothing could be told of them: as a folder ending in a dot is on Windows
        n = len(unseen)
        log(f"  {many(n, 'file')} can't be looked at, so the catalog doesn't have {'it' if n == 1 else 'them'}: "
            f"{unseen_words(unseen)}. {UNSEEN_NEXT}.")
    log(f"  {many(len(items), 'title')}. Reading metadata and covers (cached files are skipped)...")
    cache = FileCache(catalog / "cache" / "files.jsonl")
    opens = FileCache(catalog / "cache" / "opens.jsonl")
    details = FileCache(catalog / "cache" / "details.jsonl")  # the details of files besides the main ones (fill_details)
    done, started = [0], time.monotonic()
    kinds = curation.kinds(root)  # your corrections decide who has a game, and where a title is read
    files, covers = paths.LibraryFiles(root), cover_names(catalog)  # each looked at once in this build
    tidy_unpacking(catalog)

    def one(item: Item) -> None:
        try:
            read_item(root, catalog, cache, item, retry_covers, files, covers)
            fill_details(root, cache, details, item, files)
            check_files(root, opens, item, files)
        except Exception as e:  # noqa: BLE001 - one odd file mustn't stop every build: it's named, and read again next run
            item.again, rel = True, item.primary[0]
            if not item.meta:  # the title by its file's name, as for any file that can't be read
                item.meta = meta.complete(meta.Meta(source="unreadable"), PurePosixPath(rel).stem).to_dict()
            log(f"  {shown(rel)} couldn't be read ({type(e).__name__}): the next hylly catalog tries it again.")
        item.device = device_of(item, kinds.get(item.id))  # read: a ZIP package's book inside is known now
        done[0] += 1
        if done[0] % 250 == 0:
            log(f"  {done[0]}/{len(items)} ({time.monotonic() - started:.0f} s)")

    with ThreadPoolExecutor(workers) as ex:  # each in the build's context, so that a dry run's databases would go with it
        for done_one in [ex.submit(db.carried(one), item) for item in items]:
            done_one.result()
    tidy_unpacking(catalog)
    cache.tidy()
    opens.tidy()
    details.tidy()
    bad = sum(len(item.bad) for item in items)
    if bad:
        log(f"  {many(bad, 'e-book file')} can't be opened, being locked or damaged: hylly doctor lists {'it' if bad == 1 else 'them'}.")
    again = [shown(item.primary[0]) for item in items if item.again]  # named, as one that never can be is counted every run
    if again:
        n = len(again)
        log(f"  {many(n, 'file')} couldn't be read just now, so the next hylly catalog tries "
            f"{'it' if n == 1 else 'them'} again: " + ", ".join(again[:3]) + (f" and {n - 3} more" if n > 3 else "") + ".")

    unanswered = [record(item, None, kinds.get(item.id)) for item in items]  # each as its files and its shop have it
    first = {item.id: rec for item, rec in zip(items, unanswered)}  # what each title is asked about
    asked_first = {f.name for f in folders.own_folders(root) if f.first}
    queries = [lookups.Query.from_record(first[item.id]) for item in sorted(items, key=lambda i: i.source not in asked_first)]
    fetched = 0
    if ask is not None:
        fetched = ask(queries, {item.id: [rel for rel, _, _ in item.files] for item in items})
    elif lookup:  # the folders marked first are asked about first: they're being read now
        chosen, skipped = lookups.available()
        for why in skipped:
            log(f"  {why}")
        from . import bell

        stats = lookups.run(root, queries, chosen, limit=lookup_limit, log=lambda t: log("  " + t), notify=bell.job_note)
        fetched = sum(st["asked"] for st in stats.values())
    def assemble(edits) -> tuple:
        """The titles, from what was read: the lookups' answers as you said (lookups.Answers), your answers about copies
        (same_pairs), the copies merged, and what the page sorts by."""
        answers = lookups.Answers(root)
        found = {q.id: answers.for_query(q) for q in queries}
        # before they are merged; one with no answer as it was made for its question (merge_copies copies each record)
        copies = [record(item, found[item.id], kinds.get(item.id)) if found.get(item.id) else own
                  for item, own in zip(items, unanswered)]
        pairs = same_pairs(copies, curation.same_answers(edits))  # your answers: the same book, or two
        records = merge_copies(copies, folders.lead_order(root), pairs)
        for rec in records:
            if rec["src"] == "itchio" and not rec.get("a") and rec.get("p"):  # nothing names an author: its creator, as written
                rec["a"] = [rec["p"]]  # there (not tidied: "A Name, A Press" is no "Surname, First"). A person, or now and then a studio
            # what isn't the title's own: the next question about it asks without (lookups.Query.from_record)
            own = first[rec["id"]]
            rec["lf"] = [k for k in lookups.FILLED_IN if rec.get(k) and not own.get(k)]
            if not rec["lf"]:
                del rec["lf"]
            rec.update(sort_keys(rec))
        records.sort(key=lambda r: (r.get("s", "").lower() or r["t"].lower(), float(r["n"]) if re.fullmatch(r"\d+(\.\d+)?", r.get("n", "")) else 0, r["t"].lower()))
        return answers, copies, pairs, records

    answers, copies, pairs, records = assemble(edits)
    titles = matching.Titles(records, last.before)
    labels = folders.labels(root)
    said = shrunk(last, records, titles, labels, not_books_now(root, curation.folder_books(edits)))
    if said and not (fewer and fewer(said)):  # before anything is changed: your edits, the days titles came, the titles
        raise Exit(NEEDS_YOU, f"{said}\n{FEWER_NEXT}")
    unplaced = curation.reattach(root, titles, log)
    after = curation.load(root)
    if curation.lookup_answers(after) != curation.lookup_answers(edits):  # your word on a match followed a title that
        edits = after  # moved: this very build goes by it, not the next
        answers, copies, pairs, records = assemble(edits)
        titles = matching.Titles(records, last.before)
    moved = {old: new for old in last.before.files if old not in titles and (new := titles.follow(old))}
    fresh = first_seen(root, records, moved=moved)
    if fresh:
        log(f"  {many(fresh, 'new title')} in the library.")
    keep(root, records, labels)
    stats = {
        "titles": len(records), "copies_merged": len(items) - len(records), "with_cover": sum(1 for r in records if r.get("c")),
        "with_author": sum(1 for r in records if r.get("a")), "with_description": sum(1 for r in records if r.get("d")),
        "with_genre": sum(1 for r in records if r.get("g")), "lookups_fetched": fetched,
    }
    log("  " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items()))
    tell_unplaced(unplaced, log)
    asked = questions_of(copies, records, pairs, {q.id: q for q in queries}, answers, unplaced, last.before, titles.fresh,
                         others, curation.load(root), labels)
    passed_in = {PurePosixPath(folder).parts[0] for folder in passed if PurePosixPath(folder).parts}  # read in part just now
    gone = subject_gone({p[4] for p in pairs}, titles, {u["id"] for u in unplaced}, {f.name for f in own_folders(root)})
    open_now, new = curation.keep_questions(root, curation.CATALOG, asked,
                                            lambda q: not (q["kind"] == "files" and q["item"] in passed_in), gone=gone)
    loud = [q for q in open_now if not curation.QUESTIONS[q["kind"]].quiet]
    said = curation.to_decide(len(loud), new)
    if said:  # as a shop's run ends: how many of the catalog's questions are open, the new ones apart
        log(f"  {said}: hylly decide lists them and takes your answers.")
    near = curation.near_line(len(open_now) - len(loud), after=bool(said))  # the quiet ones: a line of their own
    if near:
        log(f"  {near}.")
    return stats


def not_books_now(root: Path, taken: dict[str, set[str]]) -> Callable[[str], bool]:
    """Whether a file in the library (its path there) is no book now: in one of your folders, of a suffix neither the
    rules nor your answers (taken: curation.folder_books) take as books. A shop's file is always one: its title comes
    from the shop's records."""
    own, books = {f.name for f in own_folders(root)}, rules.current().book_suffixes

    def no_book(path: str) -> bool:
        parts = PurePosixPath(path).parts
        return bool(parts) and parts[0] in own and PurePosixPath(path).suffix.lower() not in books | taken.get(parts[0], set())

    return no_book


def subject_gone(pairs: set[str], titles: matching.Titles, waiting: set[str], folders_now: set[str]) -> Callable[[dict], bool]:
    """Whether an answered catalog question's subject is gone at this build, so that it leaves the answered list (its
    answer's edit stays): two copies no longer found by their ids nor their first files (pairs: the keys same_pairs
    found), a title not in the library (match), a title gone back in the library or no longer waiting (place: one its
    answer reattached or forgot stays), a folder of yours no longer there (files)."""
    def gone(row: dict) -> bool:
        kind, item = row.get("kind"), str(row.get("item", ""))
        made = ((row.get("answer") or {}).get("made") or {}) if isinstance(row.get("answer"), dict) else {}
        if kind == "same":
            return f"same:{item}" not in pairs
        if kind == "match":
            return item not in titles
        if kind == "place":
            return made.get("op") not in ("reattach", "forget") and (item in titles or item not in waiting)
        if kind == "files":
            return item not in folders_now
        return False

    return gone


def tell_unplaced(unplaced: list[dict], log=print) -> None:
    """How many titles gone from the library have tags and corrections kept, should they return (curation.reattach): a
    place question each asks where it went (places_to_ask), hylly decide lists them."""
    if not unplaced:
        return
    n = len(unplaced)
    log(f"  Your tags and corrections of {many(n, 'title')} no longer in the library are kept, should "
        f"{'it' if n == 1 else 'they'} return: hylly decide asks where {'it' if n == 1 else 'each'} went.")


# ---- the catalog's questions: what its rules can't settle by themselves --------------------------------------------
# Each kind's finder is a plain function of what a build has (its copies before and after merge_copies, the lookups'
# answers, your edits, the titles gone, the files of your folders it passed over), so the check tool can run them over
# the last build, offline, with nothing written.

SHORT_TITLE = 8  # a title of fewer letters than this matches copies only as the very same file (same_book)
PLACES_SHOWN = 3  # the titles a place question offers, at most, best first
PLACE_LEAST = 0.8  # how alike a title or a file's name must be to be offered (lookups.similar), but for one of the
# same file name or size
COMMON_WORD = 200  # a word this many titles have says nothing of which one is meant


def _side(rec: dict) -> dict:
    """A copy as a same question names it: its id, title and first file (quoted, as its record has it), that file's
    name and size, and its source."""
    f = (rec.get("f") or [[""]])[0]
    return {"id": rec["id"], "t": rec.get("t", ""), "f": f[0], "name": PurePosixPath(unquote(f[0])).name,
            "size": f[2] if len(f) > 2 and isinstance(f[2], int) and not isinstance(f[2], bool) else 0,
            "src": rec.get("src", "")}


def _surnames(rec: dict) -> set[str]:
    return {_words(x)[-1] for x in rec.get("a", []) if _words(x)}  # "A. B. Smith" ~ "Anna Beth Smith", as same_book


def copies_to_ask(copies: list[dict], records: list[dict], pairs: list[tuple], labels: dict[str, str] | None = None) -> list[dict]:
    """Same book? Two cards whose copies have the same title words that merge_copies keeps apart where its rule is
    unsure: a title one of them doesn't confirm (_trust: neither a shop's nor in its file's name), a short one (under
    SHORT_TITLE letters), or kinds that don't join (a shop's copy whose words made it another kind than its folder
    copy). Not where the rule is sure: a manual (one only as the very same file), or authors on both sides with no
    surname in common; nor where three cards or more have the words (a magazine's issues, a game's maps: other books).
    One question for the two, by their leads' ids; settled by your answer (pairs: same_pairs), the same book, or two
    while they are the same files (m: their files, in the options)."""
    labels = labels or {}
    card = {i: rec["id"] for rec in records for i in matching.ids_of(rec)}
    lead, shown = {r["id"]: r for r in copies}, {rec["id"]: rec for rec in records}
    alike: dict[str, set[str]] = {}
    for r in copies:
        alike.setdefault(copy_words(r["t"]), set()).add(card.get(r["id"], r["id"]))
    answered = {frozenset((card.get(p[0], p[0]), card.get(p[1], p[1]))): p for p in pairs}
    out = []
    for words, cards in alike.items():
        if len(cards) != 2 or not words:
            continue
        p, q = sorted(cards)
        a, b = lead.get(p), lead.get(q)
        if a is None or b is None or "manual" in (a["k"], b["k"]) or (_surnames(a) and _surnames(b)
                                                                      and not _surnames(a) & _surnames(b)):
            continue
        why = [*(["the title of one isn't in its file's name"] if not (a.get("_trust") and b.get("_trust")) else []),
               *(["its title is short"] if len(" ".join(_words(a["t"]))) < SHORT_TITLE else []),
               *([f"one is a {a['k']}, the other a {b['k']}"] if card_kind(a["k"]) != card_kind(b["k"]) else [])]
        if not why:  # the rule is sure they are one book: two cards only by your answer
            continue
        files = sorted([f[0], f[2] if len(f) > 2 else 0] for rec in (shown[p], shown[q]) for f in rec.get("f", []))
        options = [{**o, "m": curation.mark(files)} for o in curation.SAME]
        given = answered.get(frozenset((p, q)))  # apart by hylly set copies holds whatever their files (no o)
        if given and (given[2] == "same" or given[3].get("o") in (None, curation.fingerprint(options))):
            continue
        sides = [_side(a), _side(b)]
        out.append({"kind": "same", "item": f"{p}|{q}", "file": "", "title": a.get("t", ""), "options": options,
                    "guess": 1, "held": False, "subjects": sides,
                    "why": "; ".join(why) + ": " + " and ".join(f"{labels.get(s['src'], s['src'])}, {s['name']}" for s in sides)})
    return out


def matches_to_ask(records: list[dict], queries: dict[str, lookups.Query], answers: lookups.Answers) -> list[dict]:
    """Right match? A title no lookup's answer is used for (no lk), whose sources answered with a near miss, a match that
    scored under [lookups] accept, at or above lookups.NEAR (Answers.near): one question for the title, its options
    each source's near match and none of these (the guess: none used, as before). Settled by your answer: a match used,
    or those refused, which aren't asked again either."""
    out = []
    for rec in records:
        if rec.get("lk"):
            continue
        q = queries.get(rec["id"]) or lookups.Query.from_record(rec)
        near = answers.near(q)
        if not near:
            continue
        options = [{"word": f"{s.name}: {n.get('match') or 'a match'} ({float(n.get('score', 0)):.2f})", "answer": "use",
                    "source": s.id, "score": n.get("score"), "m": lookups.match_mark(n),
                    **({"url": n["url"]} if isinstance(n.get("url"), str) and n["url"] else {})} for s, n in near]
        options.append(curation.NONE_OF_THESE)
        out.append({"kind": "match", "item": rec["id"], "file": "", "title": rec.get("t", ""), "options": options,
                    "guess": len(options) - 1, "held": False,
                    "subjects": [{"id": i, "t": rec.get("t", ""), "f": (rec.get("f") or [[""]])[0][0]} for i in matching.ids_of(rec)],
                    "why": f"{'its best match' if len(near) == 1 else 'every match'} scored under {answers.accept:g}, so none was used"})
    return out


def places_to_ask(unplaced: list[dict], records: list[dict], before: matching.Before, fresh: set[str], edits) -> list[dict]:
    """Which title for an edit? A title gone from the library whose tags and corrections are kept (unplaced: curation.
    placing): one question each, its options the titles that may be it (new in this build, or of the same file name,
    size, or alike in their title or file's name: at most PLACES_SHOWN, best first), then keep waiting (the guess, as
    before) and forget them. Settled by your answer: put on a title (a reattach), forgotten, or waiting while the same
    titles are offered."""
    by_name: dict[str, set[str]] = {}
    by_size: dict[int, set[str]] = {}
    by_word: dict[str, set[str]] = {}
    shown = {rec["id"]: rec for rec in records}
    for rec in records:
        for f in rec.get("f", []):
            by_name.setdefault(matching.file_name(unquote(f[0])), set()).add(rec["id"])
            if len(f) > 2 and isinstance(f[2], int):
                by_size.setdefault(f[2], set()).add(rec["id"])
        for w in {w for w in rules.words_of(rec.get("t", "")) if len(w) > 3}:
            by_word.setdefault(w, set()).add(rec["id"])
    out = []
    for u in unplaced:
        old, was = u["id"], u.get("f") or ""
        name, stem = matching.file_name(was), PurePosixPath(was).stem
        sizes = {size for _, size in before.files.get(old, []) if isinstance(size, int)}
        words = {w for w in rules.words_of(u.get("t", "")) if len(w) > 3}
        hits: dict[str, int] = {}
        for w in words:
            if len(by_word.get(w, ())) < COMMON_WORD:
                for i in by_word.get(w, ()):
                    hits[i] = hits.get(i, 0) + 1
        maybe = ({i for i, n in hits.items() if 2 * n >= len(words)} | by_name.get(name, set())
                 | {i for z in sizes for i in by_size.get(z, ())}) if (name or words or sizes) else set()
        scored = []
        for i in maybe:
            rec = shown[i]
            files = rec.get("f", [])
            alike = max(lookups.similar(u.get("t", ""), rec.get("t", "")),
                        max((lookups.similar(stem, PurePosixPath(unquote(f[0])).stem) for f in files), default=0.0))
            named = bool(name) and any(matching.file_name(unquote(f[0])) == name for f in files)
            sized = any(len(f) > 2 and f[2] in sizes for f in files)
            if alike >= PLACE_LEAST or named or sized:
                scored.append((-(alike + named + 0.5 * sized + 0.25 * (i in fresh)), rec.get("t", "").casefold(), i))
        options = [{"word": f"{shown[i].get('t', '')} ({unquote(shown[i]['f'][0][0]) if shown[i].get('f') else i})",
                    "answer": "to", "id": i, "t": shown[i].get("t", ""), "m": i} for _, _, i in sorted(scored)[:PLACES_SHOWN]]
        options += list(curation.WAITING)
        waits = ((edits.fixed.get(old, {}).get("catalog") or {}).get("place:") or {})
        if waits.get("a") == "keep" and waits.get("o") == curation.fingerprint(options):
            continue
        out.append({"kind": "place", "item": old, "file": "", "title": u.get("t") or old, "options": options,
                    "guess": len(options) - 2, "held": False,
                    "subjects": [{"id": old, "t": u.get("t", ""), "f": was, "kept": u.get("kept", "")}],
                    "why": f"kept: {u.get('kept') or 'nothing now'}; its file then: {was or 'unknown'}"})
    return out


def files_to_ask(others: dict[str, dict[str, list]], labels: dict[str, str], answered: dict[tuple[str, str], str]) -> list[dict]:
    """Unknown files in a folder: in one of your folders, files of a suffix the rules don't take as books and might be
    (book_files' others: not_a_question leaves out the rest), passed over today: one question for each folder and
    suffix, never for each file. Settled by your answer: not books, or books (then a build takes them, and they are
    none of these)."""
    out = []
    for folder, found in sorted(others.items()):
        label = labels.get(folder, folder)
        for suffix, (n, path) in sorted(found.items()):
            if (folder, suffix) in answered:
                continue
            out.append({"kind": "files", "item": folder, "file": suffix, "title": f"{many(n, suffix + ' file')} in {label}",
                        "options": list(curation.FILES), "guess": 0, "held": False,
                        "why": f"such as {PurePosixPath(Path(path).as_posix()).name}"})
    return out


def questions_of(copies: list[dict], records: list[dict], pairs: list[tuple], queries: dict, answers: lookups.Answers,
                 unplaced: list[dict], before: matching.Before, fresh: set[str], others: dict, edits,
                 labels: dict[str, str] | None = None) -> list[dict]:
    """The catalog's questions, of every kind, from what a build has: its copies (before merge_copies) and records
    (after), your answers about copies (pairs), the questions each title was asked by (queries, by id) and the lookups'
    answers, the titles gone with what waits for them (unplaced), the last build and the titles new in this one, the
    files of your folders it passed over (others), and your edits. Nothing is written."""
    labels = labels or {}
    return (copies_to_ask(copies, records, pairs, labels) + matches_to_ask(records, queries, answers)
            + places_to_ask(unplaced, records, before, fresh, edits)
            + files_to_ask(others, labels, curation.folder_answers(edits)))


# ---- this build next to the last one: what it has become, and what it is missing ---------------------------------------

@dataclass
class Last:
    """The last build kept: its titles' files (matching.Before, copies' ids too), its titles' own ids, and the sources
    each title had a copy in (places)."""

    before: matching.Before
    titles: list[str] = field(default_factory=list)
    places: dict[str, set[str]] = field(default_factory=dict)


IN_EACH_SOURCE = """SELECT id, source FROM title
    UNION SELECT title.id, json_extract(cp.value, '$[0]') FROM title, json_each(title.body, '$.cp') AS cp"""


def last_build(root: Path) -> Last:
    """The last build, as the database keeps it; an empty one before the first."""
    with store(root).read() as c:
        heads = [row[0] for row in c.execute("SELECT id FROM title ORDER BY position")]
        files: dict[str, list[tuple[str, int | None]]] = {i: [] for i in heads}
        for row in c.execute("SELECT title, path, size FROM title_file ORDER BY title, position"):
            files.setdefault(row[0], []).append((row[1], row[2]))
        together = {i: {i} for i in heads}  # each title's ids: its own and its copies'
        for alias, title_id in c.execute("SELECT alias, title FROM title_alias").fetchall():
            files.setdefault(alias, []).extend(files.get(title_id, []))
            together.setdefault(title_id, {title_id}).add(alias)
        places: dict[str, set[str]] = {}
        for title_id, place in c.execute(IN_EACH_SOURCE):
            if isinstance(place, str):
                places.setdefault(title_id, set()).add(place)
    merged = {i: ids for ids in together.values() if len(ids) > 1 for i in ids}
    return Last(matching.Before(files, merged), heads, places)


WATCHED = 10  # a source, or the whole catalog, with at least this many titles in the last build is watched for a fall:
SOURCE_FALL = 0.5  # a source left with fewer than half of them,
TOTAL_FALL = 0.8  # or the whole catalog with fewer than four fifths, stops the build before it keeps anything (shrunk)
FEWER_NEXT = ("Nothing was kept, and the catalog is as it was. A folder that isn't there just now does this (a disk not mounted, "
              "a share gone away, a shop's snapshot gone from the state folder): once it's back, run hylly catalog again. If "
              "those titles are gone on purpose, hylly catalog --yes keeps the build.")


def shrunk(last: Last, records: list[dict], titles: matching.Titles, labels: dict[str, str] | None = None,
           no_book=lambda path: False) -> str:
    """What this build is missing next to the last one, in words, when a source of WATCHED titles or more has lost more
    than half of them, or the whole catalog more than a fifth; else "". A folder that isn't there just now looks like
    that, and keeping such a build would take its titles off the page and the feeds until the next good one. A title
    counts in every source it has a copy in. The systems' own folders are left out (systems_own): titles that a build
    from before took in there, all of whose files are there, aren't lost when a build passes those folders by. Nor are
    titles all of whose files are no books now (no_book: not_books_now, a "books" answer of yours taken back): they left
    by your own word."""
    labels = labels or {}

    def passed_by(title_id: str) -> bool:
        files = last.before.files.get(title_id)
        return bool(files) and all(in_systems_own(path) or no_book(path) for path, _ in files)

    counted = [t for t in last.titles if not passed_by(t)]
    had = Counter(place for t in counted for place in last.places.get(t, ()) if not systems_own(place))
    now = Counter(place for r in records for place in {r["src"], *(cp[0] for cp in r.get("cp", []) if cp)})
    lines = [f"  {labels.get(source, source)}: {many(now[source], 'title')}, {n} in the last build"
             for source, n in sorted(had.items()) if n >= WATCHED and now[source] < n * SOURCE_FALL]
    if len(counted) >= WATCHED and len(records) < len(counted) * TOTAL_FALL:
        lines.append(f"  The whole catalog: {many(len(records), 'title')}, {len(counted)} in the last build")
    if not lines:
        return ""
    where: dict[str, Counter] = {}  # the folders the missing titles were in, by their first file: in each top folder,
    for title_id in counted:        # the subfolder they were all in, else the top folder itself
        files = last.before.files.get(title_id)
        if files and not any(i in titles for i in last.before.merged.get(title_id, {title_id})):  # no copy of it left
            parts = PurePosixPath(files[0][0]).parts
            where.setdefault(parts[0], Counter())["/".join(parts[:2]) if len(parts) > 2 else parts[0]] += 1
    found = sorted(((next(iter(subs)) if len(subs) == 1 else top, sum(subs.values())) for top, subs in where.items()),
                   key=lambda f: -f[1])
    most = ", ".join(f"{folder} ({n})" for folder, n in found[:3])
    return ("This build has far fewer titles than the last one:\n" + "\n".join(lines)
            + (f"\n  Most of those missing were in {most}." if most else ""))


# ---- in the database: the titles, and when each first came ---------------------------------------

def store(root: Path) -> db.Database:
    """The database that keeps the library's own records (curation.store: else db.NotHere)."""
    return curation.store(root)


def _seen(c) -> tuple[dict[str, str], set[str]]:
    """The ids seen so far with the day each came, and the file names seen, in one Unicode form as file_name gives them
    now (a name kept by an earlier version may not be)."""
    return ({row[0]: row[1] for row in c.execute("SELECT id, came FROM seen_title")},
            {matching.folded(row[0]) for row in c.execute("SELECT name FROM seen_file")})


def _keep_seen(c, ids: dict[str, str], files: set[str]) -> None:
    c.execute("DELETE FROM seen_title")
    c.execute("DELETE FROM seen_file")
    c.executemany("INSERT INTO seen_title (id, came) VALUES (?, ?)", [(str(i), str(came)) for i, came in ids.items()])
    c.executemany("INSERT INTO seen_file (name) VALUES (?)", [(str(name),) for name in sorted(files)])


def titles(c) -> list[dict]:
    """The titles as the last build kept them, in the page's order."""
    return [json.loads(row[0]) for row in c.execute("SELECT body FROM title ORDER BY position")]


class PageNotWritten(Exit):
    """A build kept in the database whose catalog/data.js couldn't be written just then (keep): the build has gone
    through, and only the server's copy of it lags. Of its own kind, so the Inbox's pass can tell it from a build that
    stopped, and its books, catalogued, leave the Inbox."""


def keep(root: Path, records: list[dict], sources: dict[str, str] | None = None) -> None:
    """A build's titles into the database, in place of the last build's, in one transaction, and checked to come back
    as they went in; then catalog/data.js made from them, for the server (page). The rules column, kept for older
    databases, holds nothing now: each title has its sort keys (sort_keys)."""
    sources = sources or shop_labels()
    want = json.loads(db.dump(records))  # as JSON has them: what data.js holds
    with store(root).write() as c:
        for table in ("title_alias", "title_file", "title"):
            c.execute(f"DELETE FROM {table}")
        for position, rec in enumerate(want):
            c.execute("INSERT INTO title (id, position, body) VALUES (?, ?, ?)", (rec["id"], position, db.dump(rec)))
            c.executemany("INSERT INTO title_file (title, position, path, format, size) VALUES (?, ?, ?, ?, ?)",
                          [(rec["id"], n, unquote(str(f[0])), f[1] if len(f) > 1 and isinstance(f[1], str) else None,
                            f[2] if len(f) > 2 and isinstance(f[2], int) and not isinstance(f[2], bool) else None)
                           for n, f in enumerate(rec.get("f") or []) if isinstance(f, list) and f])
            c.executemany("INSERT OR IGNORE INTO title_alias (alias, title) VALUES (?, ?)",
                          [(i, rec["id"]) for i in rec.get("ids") or [] if isinstance(i, str) and i != rec["id"]])
        c.execute("INSERT INTO catalog_build (generated, titles, sources, rules) VALUES (?, ?, ?, ?)",
                  (db.now(), len(want), db.dump(sources), db.dump({})))
        if titles(c) != want:
            raise db.NotKept("The catalog didn't come back from the database as it was built, so it wasn't kept, and the "
                             "page is as it was.")
    try:
        page(root)
    except OSError as e:  # on Windows, say, while another program has it open: the build is kept all the same
        raise PageNotWritten(LOCAL, f"The catalog was built and kept in the database, but {shown(root / 'catalog' / 'data.js')} "
                                    f"couldn't be written ({e}), so the library page and the feeds still show the last build. "
                                    f"hylly catalog --page writes it again from the database, without reading the books.") from None


def unlisted(root: Path, c=None, unseen: list[tuple[str, str]] | None = None) -> tuple[list[str], list[str]] | None:
    """What changed underneath since the last build, as the disk is still an ordinary disk: book files in the library's
    folders that no title has (the next hylly catalog takes them in), and title files no longer there. Its folders are
    walked as a build walks them (book_files), so the systems' own aren't, as no build takes them in. Only looks. None
    before the first build kept in the database. c: a connection of its own to ask (hylly doctor's, read-only). unseen:
    what is there but can't be looked at, by its path in the library, with why: a book file listed that can't be
    (a_file), and a folder that can't be read, its path ending in a slash. None of what is in them counts as new or
    gone, as nothing can be told of it."""
    with contextlib.nullcontext(c) if c is not None else store(root).read() as c:
        if c.execute("SELECT count(*) FROM catalog_build").fetchone()[0] == 0:
            return None
        listed = {row[0] for row in c.execute("SELECT path FROM title_file")}
        taken = curation.folder_books(curation.Edits(curation._lines(c)))  # the suffixes you said are a folder's books too
    books = rules.current().book_suffixes
    found, looked, unread = set(), [], []
    for name in dict.fromkeys([*(shop.folder for shop in SHOPS.values()), *(f.name for f in own_folders(root))]):
        base = root / name
        if base.is_dir():
            try:  # it only looks: a folder that can't be read just now is passed over, and said
                found.update(f.relative_to(root).as_posix() for f in book_files(base, books | taken.get(name, set()),
                                                                                  lambda folder: False, unread, None, looked))
            except OSError:
                unread.append(str(base))
    blind = sorted({*((Path(folder).relative_to(root).as_posix() + "/", "this folder can't be read") for folder in unread),
                    *((Path(path).relative_to(root).as_posix(), why) for path, why in looked)})
    if unseen is not None:
        unseen += blind

    def unknown(path: str) -> bool:  # a file that can't be looked at, or one in a folder that can't be read
        return any(path == rel or (rel.endswith("/") and path.startswith(rel)) for rel, _ in blind)

    gone = sorted(p for p in listed - found if paths.inside_library(p) and not unknown(p) and not (root / p).exists())
    return sorted(found - listed), gone


def page(root: Path) -> bool:
    """catalog/data.js made from the database: what the last build kept, exactly, for the server to read. False if
    there's no build in it yet. hylly catalog --page does this alone."""
    with store(root).read() as c:
        last = c.execute("SELECT generated, sources FROM catalog_build ORDER BY seq DESC LIMIT 1").fetchone()
        if last is None:
            return False
        found = titles(c)
    write_page(paths.catalog_dir(root), found, json.loads(last["sources"]), generated=last["generated"])
    return True


NEW_KEPT_DAYS = 30  # how long a title carries the date it came, in the catalog: the library page goes by the last week of it


def first_seen(root: Path, records: list[dict], now: datetime | None = None, moved: dict[str, str] | None = None) -> int:
    """When each title first came into the library, for the page's New: not the day it was bought, as a bundle may be
    downloaded long after. Kept for good in the database by id (seen_title): a title that goes and comes back isn't
    news. A folder title's id comes from its first file's path, so one renamed or moved is known by its file names
    (seen_file); and one the build found to be an id of the last build under a new one (moved: old id to the title it
    became, matching.Titles.follow) keeps the day that id came. A library's first catalog is no news: until a build is
    kept, what one finds counts as known. Returns how many are new."""
    found = store(root)
    with found.read() as c:
        ids, files = _seen(c)
        first = not ids and not files and c.execute("SELECT count(*) FROM catalog_build").fetchone()[0] == 0
    if first:  # a library's first catalog: nothing in it is news
        for rec in records:
            ids.update(dict.fromkeys(matching.ids_of(rec), ""))
            if rec["id"].startswith("f:"):
                files.update(matching.file_name(unquote(f[0])) for f in rec.get("f", []))
    # the local time alone, not db.now's: the library page, the feeds and each person's mark of how far they've seen New
    # (people.saw_new) compare these as text, and the server takes a mark only in this form (serve.NEW_DATE)
    moment = now or datetime.now()
    stamp, recent = moment.isoformat(timespec="seconds"), (moment - timedelta(days=NEW_KEPT_DAYS)).isoformat(timespec="seconds")
    fresh = 0
    was: dict[str, list[str]] = {}
    for old, new in (moved or {}).items():
        was.setdefault(new, []).append(old)
    for rec in records:
        known = [ids[i] for i in [*matching.ids_of(rec), *was.get(rec["id"], [])] if i in ids]
        names = [matching.file_name(unquote(f[0])) for f in rec.get("f", [])] if rec["id"].startswith("f:") else []
        if known:
            came = min(known)
        elif any(name in files for name in names):
            came = ""
        else:
            came, fresh = stamp, fresh + 1
        ids.update(dict.fromkeys(matching.ids_of(rec), came))
        files.update(names)
        if came >= recent:  # "" isn't: known from before all this
            rec["nw"] = came
    with found.write() as c:
        _keep_seen(c, ids, files)
    return fresh


FOLDER_PAGE = ("index.html", "help.html", "tags.js")  # what earlier versions wrote here to open the page from the folder


def write_page(catalog: Path, records: list[dict], sources: dict[str, str] | None = None, generated: str | None = None) -> None:
    """catalog/data.js: the titles as the database keeps them, and the sources' names, which the server reads (library.Catalog)
    and never sends as it is: the library page's data.js is the server's own, made from what it read with your tags and
    corrections laid over it. Earlier versions' copies of the page, its help and tags.js go: the page comes only from the
    server now, and a copy left in the folder would show old tags and run old code."""
    catalog.mkdir(parents=True, exist_ok=True)
    payload = {"generated": generated or db.now(), "sources": sources or shop_labels(),
               "items": records}
    text = "window.CATALOG = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    atomic_write(catalog / "data.js", text)
    for name in FOLDER_PAGE:
        with contextlib.suppress(OSError):  # one that can't go just now is gone the next time
            (catalog / name).unlink(missing_ok=True)


