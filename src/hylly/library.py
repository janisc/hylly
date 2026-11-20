"""The loaded catalog: catalog/data.js as the last build wrote it, your tags and corrections laid over it (Catalog), and
what the server, the feeds and the commands ask of its titles: their words and sources, the keys the library page sorts
and groups them by, the page's data made from them, the covers it may show, the files they list, and which titles the
e-reader feed lists and with which file (Device); and a number from a query string, read alike by the server's pages and
its feeds (whole). It reads the catalog and the library's records and nothing of the
server's, so that the commands that need the catalog as the server shows it (hylly tag, set, lookup, reviews and
reports, and the doctor) read it without loading the server."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from . import curation, db, matching, paths, rules, settings, storage
from .shops import SHOPS, labels as shop_labels
from .words import MB

# The types the library's files go out as, in the feeds' links and in the server's answers alike
MIME = {
    "epub": "application/epub+zip", "pdf": "application/pdf", "cbz": "application/vnd.comicbook+zip",
    "cbr": "application/vnd.comicbook-rar", "mobi": "application/x-mobipocket-ebook", "azw3": "application/vnd.amazon.ebook",
    "zip": "application/zip", "djvu": "image/vnd.djvu", "webp": "image/webp",
    "js": "text/javascript; charset=utf-8",  # the same on every system (Windows' own list may say otherwise)
}

# What the library page needs to know of each shop, sent with the catalog (page_script): whether its titles come in
# bundles, which the details then name after the shop, or else their publisher
SHOP_TRAITS = {shop.id: {"bundles": shop.bundles} for shop in SHOPS.values()}


def kinds_table() -> dict[str, dict]:
    """The kinds a title may be, in their order, as the library page names them, reads where each is read and opens
    its e-book first or its pages (the rules' [kinds.X]: label, plural, device, text_first), sent with the catalog
    (page_script): the page keeps no list of its own."""
    return {k.name: {"label": k.label, "plural": k.plural, "device": k.device, "text_first": k.text_first}
            for k in rules.current().kinds.values()}


def sources(it: dict) -> list[str]:
    """A book with copies in several places (catalog.merge_copies) is listed under each of them."""
    return [it.get("src")] + [c[0] for c in it.get("cp", [])]


def moved_ids(before: list[dict], now: list[dict], moved: dict[str, str]) -> dict[str, str]:
    """Each id a rebuild took away, to the title that took it over (matching.Titles.find on the title it was: by its
    first file, else its file's name, else its title, where one title alone has it), with those of the rebuilds before
    (moved) led on to where their title is now: for a page left open across a rebuild, which still writes places and
    asks for pages by the ids it was given (Catalog.index). Kept only while the server runs; an id no title took over,
    or one that is back, isn't in it."""
    have = {i for it in now for i in matching.ids_of(it)}
    gone = {i: it for it in before for i in matching.ids_of(it) if i not in have}
    if not gone and not moved:
        return {}
    titles = matching.Titles(now) if gone else None  # only for what went this time: a rebuild that moved nothing is quick
    found = {old: to for old, it in gone.items() if (to := titles.find(curation.snapshot(it)))}
    earlier = {old: to if to in have else found.get(to) for old, to in moved.items() if old not in have}
    return {old: to for old, to in {**earlier, **found}.items() if to}


class TitleIndex(dict):
    """Every id, a merged title's copies' too, to its title (Catalog.index); and an id a rebuild took away while the
    server ran (Catalog.moved), to the title that took it over. Those answer a look-up by that id ([], get, in) but
    aren't among its keys, so whatever goes through them all finds each title by its own ids, as before."""

    def __init__(self, titles: dict[str, dict], moved: dict[str, str]) -> None:
        super().__init__(titles)
        self.moved = moved

    def __missing__(self, key: str) -> dict:
        to = self.moved.get(key)
        if to is not None and dict.__contains__(self, to):
            return dict.__getitem__(self, to)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key) -> bool:
        return dict.__contains__(self, key) or dict.__contains__(self, self.moved.get(key))

    def current(self, key: str) -> str:
        """The id to write for one a page sent: as it is, where a title has it (a copy's too); for one a rebuild took
        away, the id of the title that took it over, never an id no title has. KeyError when neither."""
        return key if dict.__contains__(self, key) else self[key]["id"]


COVER_SITE = re.compile(r"https://([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d{1,5})?)(?=[/?#]|$)", re.I)
OWN_COVER = re.compile(r"covers/[0-9a-f]{16}\.webp", re.I)  # the catalog's own thumbnails (catalog.thumb_name)
VERSIONED = re.compile(r"\?v=[0-9]{1,20}")  # after one of those, in the page's data from the server (Catalog._for_page)


def good_cover(cover) -> bool:
    """A cover the build writes (catalog.record): one of the catalog's own thumbnails, or a shop's https address with a
    host (catalog.shop_cover, COVER_SITE). Anything else, as an older catalog or one changed by hand may hold (http to a
    device at home, a file: or share address), Catalog.get leaves out, so neither the page, the feeds nor the page's
    policy (cover_sites) ever has it. The page checks the same (catalog_page.html, COVER), and takes one of the
    catalog's own with its version too, as the server sends it (shown_cover)."""
    return isinstance(cover, str) and bool(OWN_COVER.fullmatch(cover) or COVER_SITE.match(cover))


def shown_cover(cover) -> bool:
    """A cover the library page keeps (catalog_page.html, COVER): one the build writes (good_cover), or one of the
    catalog's own thumbnails with its version, as the server sends those in the page's data (Catalog._for_page)."""
    own = OWN_COVER.match(cover) if isinstance(cover, str) else None
    return good_cover(cover) or bool(own and VERSIONED.fullmatch(cover, own.end()))


def cover_version(mtime_ns: int) -> str:
    """A thumbnail's version, after ?v= in its address in the library page's data: its file's time, in whole
    nanoseconds (st_mtime_ns), which changes only when a build makes the thumbnail again. An address with the version
    the file has now is the same picture for as long as it stands, so a browser keeps it and doesn't ask again (serve's
    Handler._send_file); one with another, or none, is asked about each time, as before. A time before 1970, as a copy
    may give a file, is 0."""
    return str(max(mtime_ns, 0))


def cover_sites(items: list[dict]) -> tuple[str, ...]:
    """The sites the shops' covers come from, as a policy may name them; a file's own cover is the library's."""
    return tuple(sorted({m[1].lower() for it in items if (m := COVER_SITE.match(str(it.get("c") or "")))}))


def catalog_files(items: list[dict]):
    """Every file the titles list, by its path in the library: data.js quotes it, the database's title_file keeps it
    unquoted (catalog.keep), and so does this. A file inside a ZIP package is the package's (Handler._zipped)."""
    for it in items:
        files = it.get("f") if isinstance(it, dict) else None
        for f in files if isinstance(files, list) else ():
            if isinstance(f, list) and f and isinstance(f[0], str):
                yield unquote(f[0])


def _on_stderr(message: str) -> None:
    """A message of the catalog's where no server says it (Catalog.say): on standard error, cleaned as the server's
    messages are (storage.shown)."""
    sys.stderr.write(storage.shown(message) + "\n")
    sys.stderr.flush()


class Catalog:
    """catalog/data.js as the last build wrote it, loaded once and again whenever the file changes, with your tags and
    corrections (the database's edit table, curation.py) laid over it, again whenever they change: your title, authors,
    series and so on replace what the catalog found, here and so in every feed and the library page, whose data.js is
    made from this (page_script), never the file of that name as it is. Where another computer keeps the library's
    records, its tags and corrections can't be read from here: the catalog shows as built. What other requests may be
    reading as it changes, the titles and the sources' names, is made anew and put in place whole, never changed where
    it stands: a request goes on with the list it was given, and a feed with the names it began with. say: where its
    messages go, the server's log in a server (serve._stamp), standard error in a command; failed: where a failure goes
    (an error, what failed, the words), in a server once for each kind (serve.tell_failure), else said as a message."""

    def __init__(self, root: Path, say: Callable[[str], None] = _on_stderr,
                 failed: Callable[[BaseException, str, str], object] | None = None):
        self.root = root
        self.say = say
        self.failed = failed or (lambda e, what, said: say(f"{what}: {said}"))
        self.path = paths.catalog_dir(root) / "data.js"
        self.mtime = 0.0
        self.edits_stamp: tuple | None = None
        self.items: list[dict] = []  # the titles as read, your tags and corrections laid over copies of them (get)
        self._read: list[dict] = []  # the titles as data.js has them, with what the laying needs (_k0, _found, ...)
        self.generated = ""
        self.sources: dict[str, str] = {}
        self.labels: dict[str, str] = shop_labels()  # each source's name, the shops' and your folders', for the feeds
        self._titles: tuple[list[dict], matching.Titles] | None = None
        self._index: tuple[list[dict], TitleIndex] | None = None  # every id to its title, as the titles were last laid (index)
        self.covers: tuple[str, ...] = ()  # the sites its shop covers come from: the library page's policy names them
        self.dotted: frozenset[str] = frozenset()  # its files with a name starting with a dot in their path (lists)
        self.moved: dict[str, str] = {}  # the ids rebuilds took away since the server started, each to its title's now (moved_ids)
        self._script: tuple[tuple, tuple[str, bytes, bytes]] | None = None  # the page's data.js, as last made (page_script)
        self._versions: tuple[float | None, dict[str, str]] | None = None  # the thumbnails' versions (_cover_versions)
        self._lock = threading.Lock()

    def titles(self) -> matching.Titles:
        """The titles now, for finding one whose id has changed; made anew when the list is."""
        items = self.get()
        with self._lock:
            if self._titles is None or self._titles[0] is not items:
                self._titles = (items, matching.Titles(items))
            return self._titles[1]

    def index(self) -> TitleIndex:
        """Every id, a merged title's copies' too, to its title, and an id a rebuild took away to the title that took it
        over (TitleIndex, with moved): made when the titles are laid anew, and kept until then, as titles() is, never
        made again for each look-up (the server's every page, page turn and change asks it). The ids are data.js's,
        which edits never change; a title, with your edits laid over it, is laid anew at every edit, so it's then made
        again with them (beside laying every title, a far bigger job)."""
        self.get()
        with self._lock:  # the titles and moved as laid together, should another request lay them anew meanwhile
            items = self.items
            if self._index is None or self._index[0] is not items:
                self._index = (items, TitleIndex({i: it for it in items for i in curation.ids_of(it)}, self.moved))
            return self._index[1]

    def cover_sites(self) -> tuple[str, ...]:
        """The covers' sites as they stand; a data.js that can't be read keeps those it had, and the page still opens."""
        try:
            self.get()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return self.covers

    def lists(self, rel: str) -> bool:
        """Whether the last build listed this file, one with a name starting with a dot in its path (Handler._servable
        asks of no other): by its path in the library, exactly as data.js has it once unquoted, which is how the
        database's title_file keeps it (catalog.keep), with no folding of case or letters. A data.js that can't be read
        just now keeps what it listed. Read again for each file asked about, as the server answers one at a time; a look
        at many at once (refused_files) reads the list once."""
        try:
            self.get()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return rel in self.dotted

    def get(self) -> list[dict]:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError as e:
                if storage.share_is_gone(e):  # not the same as no catalog yet: what it has stays for when the share is back
                    raise
                if paths.library_missing(self.root):  # and on Linux and macOS, an unmounted disk is an empty folder
                    raise OSError(f"The library at {self.root} isn't there") from None
                mtime = 0
            reading = True  # data.js, before your tags and corrections are laid over what it holds
            try:
                reloaded = mtime != self.mtime
                if reloaded:
                    text = self.path.read_text(encoding="utf-8") if mtime else "window.CATALOG = {\"items\": []};"
                    data = json.loads(text[text.index("{"): text.rindex("}") + 1])
                    read = data["items"]
                    for it in read:  # a cover the build wouldn't write, as an older catalog may have one: none at all
                        if "c" in it and not good_cover(it["c"]):
                            del it["c"]
                    covers = cover_sites(read)
                    dotted = frozenset(rel for rel in catalog_files(read) if any(part.startswith(".") for part in rel.split("/")))
                    sources = dict(data.get("sources") or {})
                    try:
                        moved = moved_ids(self._read, read, self.moved)
                    except Exception as e:  # noqa: BLE001 - only a help for pages left open: the catalog reloads all the same
                        moved = self.moved
                        self.failed(e, "Ids that went in a rebuild couldn't be followed, so a page left open across it "
                                        "may lose what it saves until it's reloaded", repr(e))
                    for it in read:
                        it["_k0"] = it.get("k")  # the kind the catalog found, before your correction
                        it["_found"] = {key: it[key] for key, _ in curation.TEXT.values() if key in it}  # likewise the title etc.
                        it["_lf0"] = it.get("lf", [])  # the fields lookups filled in, before your corrections
                        it["_keys"] = {key: it[key] for key in ("sk", "ak") if key in it}  # its sort keys, as the build made them
                    # read whole: each put in place at once, the names in a dict of their own, never added to one in use
                    self._read, self.generated, self.covers, self.dotted = read, data.get("generated", ""), covers, dotted
                    self.moved = moved
                    self.sources, self.labels = sources, {**shop_labels(), **sources}
                    self.mtime = mtime
                reading = False
                try:
                    stamp = curation.revision(self.root)  # one more with every edit
                except db.NotHere as e:  # another computer keeps the library's records, and the server's log has said so
                    stamp = ("not here", str(e))
                except (db.Newer, db.Unusable) as e:  # the catalog as found, without your corrections, and said once
                    stamp = ("unusable", str(e))
                    if stamp != self.edits_stamp:
                        self.say(f"Your corrections and tags can't be laid over the catalog: {e}")
                if reloaded or stamp != self.edits_stamp:
                    edits = curation.Edits([]) if isinstance(stamp, tuple) else curation.load(self.root)
                    laid = []
                    for found in self._read:  # each title laid anew, over a copy: a request still on the list from
                        it = dict(found)  # before goes on with it as it was
                        fixes = edits.fixes_of(curation.ids_of(it))
                        it["k"] = fixes.get("kind", it["_k0"])
                        if fixes.get("ereader"):
                            it["ereader"] = fixes["ereader"]
                        else:
                            it.pop("ereader", None)
                        for field, (key, _) in curation.TEXT.items():
                            value = fixes[field] if field in fixes else it["_found"].get(key)
                            if value in (None, "", []):
                                it.pop(key, None)
                            else:
                                it[key] = value
                        yours = {key for field, (key, _) in curation.TEXT.items() if field in fixes}
                        it["_fixed"] = {field for field in curation.TEXT if field in fixes}  # a title you corrected isn't the file name's (shelves.missing)
                        it["_fixes"] = fixes  # for the library page: what you corrected (page_item)
                        for key, field in (("sk", "series"), ("ak", "authors")):  # a corrected one's is made anew (series_key)
                            if field in fixes or key not in it["_keys"]:
                                it.pop(key, None)
                            else:
                                it[key] = it["_keys"][key]
                        it["lf"] = [key for key in it["_lf0"] if key not in yours]  # a title you fixed is asked about as you wrote it
                        tags = edits.of(curation.ids_of(it))
                        if tags:
                            it["tg"] = tags
                        else:
                            it.pop("tg", None)
                        it["_w"] = set(rules.words_of(" ".join([it.get("t", ""), it.get("s", ""), " ".join(it.get("a", [])), it.get("p", ""),
                                                             it.get("b", ""), it.get("col", ""), " ".join(it.get("g", [])), " ".join(it.get("gm", [])),
                                                             " ".join(c[1] for c in it.get("cp", [])), " ".join(tags)])))
                        laid.append(it)
                    self.items, self.edits_stamp = laid, stamp
            except BaseException:  # whatever stopped it, a share that dropped out or a database error, nothing stays half done:
                if reading:  # data.js half read: none of it was taken, and the next request reads it again
                    self.mtime = None
                else:  # the titles stay as last laid, and the next request lays them again, without reading data.js
                    self.edits_stamp = None  # again while the database fails
                raise
            return self.items

    def page_script(self) -> tuple[str, bytes, bytes]:
        """The library page's data.js: the catalog as laid here (_for_page), one JSON value written by json.dumps, so
        nothing in a title, an author or a tag can end its text and run as the page's code, whatever catalog/data.js
        holds (it is read, never sent). Made again when the catalog or the edits change, and kept until then, packed
        too, once (the server sends that to every browser that takes it: Handler._page_data): its tag (ETag, by its
        bytes), the text, and the text packed."""
        self.get()
        with self._lock:
            key = (self.mtime, self.edits_stamp, rules.generation())  # the kinds' names are the rules': read again
            if self._script is None or self._script[0] != key:
                payload = {"generated": self.generated, "sources": self.sources, "shops": SHOP_TRAITS,
                           "kinds": kinds_table(), "items": self._for_page(self.items)}
                text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                # U+2028 and U+2029 end a line in an older browser's script, even inside a string; a lone half of a
                # surrogate pair, which a JSON file can hold (as \ud800), goes back as that escape, never as bytes
                # that aren't UTF-8
                text = "window.CATALOG = " + text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + ";\n"
                body = text.encode("utf-8", "backslashreplace")
                tag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
                self._script = (key, (tag, body, gzip.compress(body, compresslevel=6, mtime=0)))
            return self._script[1]

    def for_page(self, items: list[dict]) -> list[dict]:
        """These titles as the library page gets them (_for_page), under the catalog's lock, as page_script makes the
        page's data. A title no longer changes once laid (get lays your edits over copies, put in place whole), so the
        lock guards nothing here now but the covers' versions; it stays so that a correction's answer and the page's
        data are made one way, one at a time, and costs an answer nothing it would notice."""
        with self._lock:
            return self._for_page(items)

    def _for_page(self, items: list[dict]) -> list[dict]:
        """The titles as the library page gets them (page_item), each of the catalog's own thumbnails with its version
        in its address (?v=, cover_version), so that a browser keeps the covers it has shown and asks for none again
        until a build makes one anew. The feeds' covers go as the build wrote them: what reading apps get stays as it
        was. Called with the lock held."""
        versions, out = self._cover_versions(), [page_item(it) for it in items]
        for it in out:
            cover = it.get("c", "")
            if OWN_COVER.fullmatch(cover) and (version := versions.get(cover.split("/", 1)[1])):
                it["c"] = f"{cover}?v={version}"
        return out

    def _cover_versions(self) -> dict[str, str]:
        """The catalog's own thumbnails, each by its file's name, with its version (cover_version): listed once for each
        data.js read, as a build writes them before it writes that, one listing of catalog/covers. One that can't be
        looked at has none, and its address goes as the build wrote it. Called with the lock held."""
        if self._versions is None or self._versions[0] != self.mtime:
            found: dict[str, str] = {}
            try:
                with os.scandir(paths.catalog_dir(self.root) / "covers") as listing:
                    for entry in listing:
                        try:
                            found[entry.name] = cover_version(entry.stat().st_mtime_ns)
                        except OSError:  # gone meanwhile
                            pass
            except OSError:  # none made yet, or the disk or share isn't there just now: as the build wrote them
                pass
            self._versions = (self.mtime, found)
        return self._versions[1]

    def search(self, q: str) -> list[dict]:
        qw = rules.words_of(q)  # as the page's search has them: letters and digits of any script
        if not qw:
            return []
        return [it for it in self.get() if all(any(w.startswith(x) for w in it["_w"]) for x in qw)]


def series_key(it: dict) -> str:
    """A title's series key (Rules.series_key), which the library page and the series feed group its volumes by: as
    the build made it (catalog.sort_keys), else, for a series you corrected or a catalog built before titles had one,
    by the rules in effect."""
    if not it.get("s"):
        return ""
    return it.get("sk") or rules.current().series_key(it["s"])


def author_key(it: dict) -> str:
    """A title's first author, surname first, as the library page sorts by author (Rules.author_key): as the build
    made it, else by the rules in effect, as series_key."""
    authors = it.get("a")
    if not authors or not isinstance(authors, list):
        return ""
    return it.get("ak") or rules.current().author_key(str(authors[0]))


def page_item(it: dict) -> dict:
    """A title as the library page gets it (Catalog.page_script, and the answer to a correction): as the server has it,
    your tags and corrections laid over it, without the server's own keys (_...), with its sort keys (series_key,
    author_key); for a title you corrected, fx, your corrections, and o, what the catalog found of those it shows."""
    out = {k: v for k, v in it.items() if not k.startswith("_") and not (k == "lf" and not v)}
    for key, made in (("sk", series_key(it)), ("ak", author_key(it))):
        if made:
            out[key] = made
        else:
            out.pop(key, None)
    # your answers to the questions (a file, a title's answers) are the owner's, and nothing the page shows: left out
    fixes = {k: v for k, v in (it.get("_fixes") or {}).items() if k not in curation.ANSWERED}
    if fixes:
        out["fx"] = fixes
        found = it.get("_found") or {}
        was = {key: found[key] for field, (key, _) in curation.TEXT.items() if field in fixes and key in found}
        if "kind" in fixes and it.get("_k0"):
            was["k"] = it["_k0"]
        if was:
            out["o"] = was
    return out

# ---- a feed for one device ------------------------------------------------------------------

@dataclass(frozen=True)
class Device:
    """Which titles the e-reader feed (/ereader/opds, "Library for e-readers") lists, and the one file each offers:
    [ereader_feed] in settings.toml, and the e-reader's formats under [download]."""
    # Each default is [ereader_feed]'s in settings.DEFAULTS, written there once.
    kinds: tuple[str, ...] = tuple(settings.DEFAULTS["ereader_feed"]["kinds"])  # not comics, manuals or RPG books
    # the first one a title has wins; (): those of ereader_order that flow (offers)
    formats: tuple[str, ...] = tuple(settings.DEFAULTS["ereader_feed"]["formats"])
    max_mb: float = settings.DEFAULTS["ereader_feed"]["max_mb"]  # bigger files are mostly image-heavy (a disk's MB)
    skip_tablet: bool = settings.DEFAULTS["ereader_feed"]["skip_tablet"]  # titles Hylly suggests for a tablet
    # An e-reader has no sandbox, and KOReader's own MuPDF is older than the one with the fix for CVE-2026-3308. So PDFs
    # go in only from the shops whose publishers are known, not from itch.io, where anyone can upload, or your own
    # folders. "any": from everywhere. To be dropped once KOReader's stable release has MuPDF 1.28.
    pdf_from: tuple[str, ...] = tuple(settings.DEFAULTS["ereader_feed"]["pdf_from"])
    # The e-reader's formats, best first, as a download picks a book's: [download] ereader, said there once
    ereader_order: tuple[str, ...] = tuple(settings.DEFAULTS["download"]["ereader"])

    def offers(self) -> tuple[str, ...]:
        """The formats the feed offers, the first a title has: yours, else the e-reader's that are books and flow to
        the screen (book and reflowable in formats.toml), as an e-ink screen shows them best: no PDF, no comic, and no
        plain text, which beside a ZIP of pictures is its readme."""
        r = rules.current()
        return self.formats or tuple(f for f in self.ereader_order if f in r.reflowable and f in r.book_formats)

    def best_file(self, it: dict) -> list | None:
        """The first allowed format the title has, as a file of its own or else inside its ZIP package. For a
        title you put in the feed yourself (ereader = always), any file of formats or of the e-reader's, of any size,
        will do: a PDF or a comic, say."""
        found = _pick(it, self.offers(), self.max_mb, self.pdf_from)
        if found is None and it.get("ereader") == "always":
            found = _pick(it, (*self.formats, *self.ereader_order), None, self.pdf_from)
        return found

    def suits(self, it: dict) -> bool:
        if it.get("ereader") in ("always", "never"):  # your own say (curation) beats the rules
            return it["ereader"] == "always" and self.best_file(it) is not None
        return (it.get("k") in self.kinds and not (self.skip_tablet and it.get("dev") == "tablet")
                and self.best_file(it) is not None)


def _source_of(f: list) -> str:
    """Where a file came from: its top folder in the library, whose name a path in the catalog has as in an address. (A
    title's src won't do: copies of it from elsewhere are among its files.)"""
    return unquote(str(f[0]).replace("\\", "/").split("/", 1)[0]).lower()


def _pick(it: dict, formats: tuple[str, ...], max_mb: float | None, pdf_from: tuple[str, ...] = ("any",)) -> list | None:
    for fmt in formats:
        for files in (it.get("f", []), it.get("z", [])):
            fits = [f for f in files if f[1] == fmt and f[0] not in it.get("bad", {})
                    and (max_mb is None or file_size(f) <= max_mb * MB)  # a disk's MB, as max_mb is named
                    and (fmt != "pdf" or "any" in pdf_from or _source_of(f) in pdf_from)]
            if fits:
                return min(fits, key=file_size)
    return None


def file_size(f: list) -> float:
    """A title's file's size in bytes, as the catalog lists it ([path, format, size]); 0 where it lists none."""
    return f[2] if len(f) > 2 and isinstance(f[2], (int, float)) else 0


def whole(values: list[str] | None, default: int) -> int:
    """A whole number from a query string (parse_qs's list for a name), for the server's pages and its feeds alike, as
    someone's address bar may hold anything: default for anything else, and for one too long to be a page's."""
    return int(values[0]) if values and values[0].isdecimal() and len(values[0]) < 19 else default


def ereader_feed() -> Device:
    """[ereader_feed] in settings.toml over the defaults, each key checked and one it doesn't have told, as in every
    section (settings.py); with the e-reader's formats, [download] ereader."""
    feed = settings.get("ereader_feed")
    values = {key: tuple(feed[key]) if isinstance(feed[key], list) else feed[key]
              for key in settings.DEFAULTS["ereader_feed"]}
    return Device(**{**values, "max_mb": float(values["max_mb"]),
                     "ereader_order": tuple(settings.get("download")["ereader"])})
