"""hylly serve: the library page, the reader and the library feeds (OPDS) over HTTP.

One server at a time (instance.py): starting another says where the running one is, and leaves it
as it is. Its options are the settings ([server] of settings.toml); what's typed on hylly serve is
for that one run in the foreground. It listens where [server] listen says, on one port; this
computer only as it comes. It runs until stopped, and writes its messages to server.log itself
(instance.logged). It serves the library's files and the catalog, and changes no book. What those
allowed change through the page (tags, corrections and suggestions, and each person's places,
lists and reviews) is kept in hylly.db in the data folder (db.py).
In the library it writes these: curation\\tags.toml, with examples, when Find suggestions finds
none there; state\\database.json the first time its database takes the library on (db.own); and
the nightly copies of the database and the settings in state\\ (dbcopy.copy_tonight), one of its
periodic jobs (periodic.py). The library page's data.js is its own, made from catalog\\data.js
with the tags and corrections laid over it (library.Catalog.page_script), and written nowhere.

Every address it answers is a row of its routes (Handler.GET, Handler.POST), with who may ask there (Guard) and what
answers, run by one dispatcher (Handler._route): without a login 401, logged in but not for your role 403, and not
found (404) only for what isn't there. A person's own writes (stars, reviews, a featured list) that their role or the
settings don't allow are refused with 409 instead, which the page's outbox lets go: a 403 there means a stale key.

The library feeds (OPDS) for reading apps, /opds and /ereader/opds and the same under /me, are opds.py's: the server
admits the request and says who is asking, and the feed's page is made there (Handler._asking, opds.answer).
The library page's account menu, the login page and the help's Licences and source link to this Hylly's source
([server] source_url: Hylly's own as it comes). The licences of the work by others it includes are served as plain
text at /licences/ (LICENCES), to whoever may read the help, a borrower too.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import functools
import gzip
import io
import hashlib
import hmac
import html
import ipaddress
import json
import mimetypes
import os
import queue
import re
import secrets
import selectors
import signal
import socket
import socketserver
import sqlite3
import stat
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from dataclasses import dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from types import EllipsisType
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .exits import BUSY, LOCAL, NEEDS_YOU, OK, Exit
from . import PROJECT_URL, access, bell, curation, dashboard, db, dbpage, instance, library, matching, meta, opds, paths, people, periodic, reader, removed, reports, reviews, rules, settings, shelves, storage, themes
from .access import here, home_too, lan_address, other_devices  # the library's addresses, beside who is inside
from .words import fill, many, size_words

PORT = 8770
# Sent gzipped: the feeds and lists, and Hylly's own files (Handler._send_file's own), never a file of the library
PACKABLE = re.compile(r"text/|application/(javascript|json|xml|atom\+xml|opensearchdescription\+xml)|image/svg\+xml")


# ---- the server ----------------------------------------------------------------------------

LOCAL_NAMES = ("127.0.0.1", "localhost", "::1")
# The library page, its data and its help: this version's own, made by the server (Handler._own_file), never a file of
# the library of that name, which anyone who may write to the library could have put there
OWN_FILES = {"catalog/index.html", "catalog/data.js", "catalog/help.html"}
# A cover asked for by the version its file has (library.cover_version, in the library page's data): that address is
# that picture for good, so the browser keeps it, as it keeps the reader's pages (/api/page), and asks for it no more.
# Private: like every answer but the login page's, it's for whoever is logged in, never for a proxy to keep and hand on
KEPT_COVER = "private, max-age=31536000, immutable"
# The licences of the work by others that Hylly includes, as THIRD-PARTY-NOTICES.md's first table names them: each file
# by its path beside this file, served as plain text at LICENCE_PATH + that path (Handler._licence), for the help's
# Licences and source. These files, and nothing else of their folders.
LICENCE_PATH = "/licences/"
LICENCES = ("foliate/LICENSE", "foliate/vendor/LICENSE-zip.js.txt", "foliate/vendor/LICENSE-fflate.txt",
            "fonts/OFL-Literata.txt", "fonts/OFL-AtkinsonHyperlegible.txt", "LICENSE-lucide.txt",
            "LICENSE-feather.txt")
PRIVATE_FOLDERS = ("state", "curation")  # at the top of the library: never served, by any name (_private_folder)
# A file of the library is sent in a sandbox of its own, where nothing runs and it can't act as the library, unless
# its type is one a browser never runs: books, comics, PDFs, pictures. Decided by the type sent, not the name, so an
# XML file under any name (.rdf, .xsd: Windows' own list of types decides those) is sandboxed too.
INERT = ("application/pdf", "application/epub+zip", "application/vnd.comicbook", "application/x-mobipocket-ebook",
         "application/vnd.amazon.ebook", "application/zip", "application/x-rar", "application/x-7z-compressed",
         "application/x-fictionbook", "text/plain", "image/jpeg", "image/png", "image/gif",
         "image/webp", "image/bmp", "image/vnd.djvu", "audio/", "video/", "font/")
SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'self'"
SCRIPTS = {"js", "mjs", "cjs"}  # a library's script files go as plain text, so no page can run them (with nosniff)
SCRIPT_TYPE = re.compile(r"javascript|ecmascript|jscript|livescript", re.I)  # every type a browser runs as a script
# What a page asks a library file for that would run it: as a script or a worker (Fetch's script-like destinations),
# or as a style-sheet transformation (XSLT runs as the page). Refused, whatever the file's type.
SCRIPT_DESTS = {"script", "worker", "sharedworker", "serviceworker", "audioworklet", "paintworklet", "xslt"}
MAX_BODY = 1 << 20  # the largest request body: the edit questions are small
MAX_LOGIN_BODY = 4096
MAX_PLACE = 4000  # a book's location, as long as one gets
NEW_DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")  # as catalog.first_seen writes them
MAX_PAGES = 10_000_000
MAX_WORDS = 300  # of what a bookmark is known by: the first words of its page, or those around the word marked
MAX_MARKED = 100  # of the word or the passage marked itself, for finding it in those
BOOKMARK_KEY = re.compile(r"[A-Za-z0-9_-]{1,40}")
LIST_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
MAX_AGE = 90 * 86400  # how late a device that was offline can say it was somewhere
# What says the records (each person's, and the library's: tags, corrections, shelves, decisions) can't be read or kept
# just now: the database's own errors (another command holding it longer than db.BUSY_SECONDS, a full disk, a damaged
# file), one this Python's SQLite can't use, one that doesn't keep this library's records, and a file's or a folder's.
# Every handler of the records answers these as the page expects (Handler._not_kept), never as a failure of the server.
RECORDS_FAILED = (sqlite3.Error, db.Unusable, db.NotHere, OSError)


def records(words: str = "It couldn't be kept", whose: str = "What's kept for you") -> Callable:
    """A handler of the records, their errors answered in one place, as the library page expects them, so that no
    handler writes them out again: a database this version leaves alone (people.Newer: Handler._left_alone, 503, its
    words naming whose), a list that isn't there (404), a change the rules don't allow where it stands
    (people.NotAllowed: 409, in its own words), and what couldn't be read or kept just now (RECORDS_FAILED:
    Handler._not_kept, 500, words and the error's kind). What a handler answers itself (a full list, a shelf that isn't
    one) it still catches itself; whatever else it raises goes on to the server's own answer (Handler._failed)."""
    def wrap(handler: Callable) -> Callable:
        @functools.wraps(handler)
        def answered(self) -> None:
            try:
                return handler(self)
            except people.Newer as e:
                return self._left_alone(e, whose)
            except people.NoSuchList:
                return self._json({"ok": False, "error": "No such list."}, 404)
            except people.NotAllowed as e:
                return self._json({"ok": False, "error": f"Not now: {e}."}, 409)
            except RECORDS_FAILED as e:
                return self._not_kept(e, words)
        return answered
    return wrap


# ---- who may ask at an address: the routes' guards (Handler.GET, Handler.POST) -------------------------------

@dataclass(frozen=True)
class Guard:
    """One step of who may ask at an address, run in turn by the one dispatcher (Handler._route) after the login every
    address but OPEN's needs (401 without one; a browser opening a page goes to the login page: Handler._admitted). A
    step lets the request on, or refuses it with 403 and says why (Handler._refused): first the least role it lets on
    (access.ROLES), in the address's own words; then, where it has one, a check of its own, the Handler method named
    check, which lets the request on or sends its refusal: a key's, above all. Not found (404) is never a guard's
    answer: only an address no table has, or what an address asks for that isn't there (a title, a page), is."""
    role: str = "borrower"
    words: str = ""
    check: str = ""


OPEN = Guard(role="")  # before any login, by the address as sent: the login page and its form, logging out, the
# health check, the theme and what a phone fetches to put the library on its home screen
ANYONE = Guard()  # whoever is logged in, a borrower too: the page's questions, the reader's program, the licences
LENT = Guard(check="_lent")  # a reader or more: the library's files and its feeds; a borrower, the page and covers only
FOR_THE_APPS = Guard(check="_for_the_apps")  # a feed is a reading app's: a browser opening one goes to the library page
PERSONAL = Guard(check="_personal")  # a person's own writes: their own key of this start, from this site, JSON, paced
CHANGE = Guard("librarian", "Changes are for a librarian or the owner; log in as one.", "_library_key")  # with its key
SERVER_PAGE = Guard("owner", "The Server page is for the library's owner.")
DATABASE_PAGE = Guard("owner", "The Database page is for the library's owner.")
SERVER_KEY = Guard(check="_server_key")  # the Server and Database pages' own key, from this site's own pages


@dataclass
class Caller:
    """Who is asking, worked out once a request (Handler._who), and read wherever a rule or a handler asks it: the one
    logged in (user), as the login check found them (Gate.check, never looked up again), or None before it, or
    without a login; the user of the browser login the cookie names (session: Gate.session_user, asked once), who is the
    one logged in unless a reading app logged in with Basic; and the accounts as this request read them, once, each
    user's password hash and role by name (Gate.accounts), so that a role asked again, or another person's, reads
    nothing more. And the keys the library page is given for them, each their name signed with this start's secret
    (signing: the personal key's, the editors'), so that a page left open for one person, with another logged in since,
    sends nothing as the other (Handler._personal, Handler._library_key): made when first asked."""
    user: str | None
    session: str | None
    users: dict[str, str] = field(repr=False)  # password hashes: never in a printout
    roles: dict[str, str]
    signing: tuple[bytes, bytes] = field(repr=False)  # this start's secrets: never in a printout

    def role_of(self, name: str | None) -> str:
        """A user's role as this request read the accounts (Gate.role); a name that isn't a user's, or none, the
        least."""
        return self.roles.get(name, access.ROLES[0]) if name else access.ROLES[0]

    @property
    def role(self) -> str:
        return self.role_of(self.user)

    @property
    def editor(self) -> str | None:
        """Who may change things, as the edits name them (curation.py): a librarian or the owner, by name; None else."""
        return self.user if self.role in ("librarian", "owner") else None

    @functools.cached_property
    def personal_key(self) -> str:
        """Their key for their own writes (X-Personal-Key)."""
        return hmac.new(self.signing[0], (self.user or "").encode("utf-8"), hashlib.sha256).hexdigest()

    @functools.cached_property
    def editors_key(self) -> str:
        """An editor's key for the library's changes (X-Server-Key), tags and corrections: the library page has it only
        for a librarian or the owner (Handler._api_session), and the role is asked besides (CHANGE)."""
        return hmac.new(self.signing[1], (self.user or "").encode("utf-8"), hashlib.sha256).hexdigest()


def librarian(words: str) -> Guard:
    """For a librarian or the owner; anyone else is told so, in the address's own words."""
    return Guard("librarian", words)


def owner(words: str) -> Guard:
    """For the library's owner; anyone else is told so, in the address's own words."""
    return Guard("owner", words)


def _whole_page(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= MAX_PAGES


def _age(value) -> float:
    """How long ago a device says it was somewhere, in seconds: a number from 0 to MAX_AGE, capped before it's made a
    float, so no number in the JSON is too big for one; anything else, NaN too, is now (0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0:
        return 0.0
    return float(min(value, MAX_AGE))


def same_key(given: str | None, key: str) -> bool:
    """Whether a key a request sent (a header's value) is this key, compared in the same time whatever was sent: both
    as UTF-8 bytes, so a value that isn't ASCII is just another wrong key (hmac.compare_digest compares text only when
    it is ASCII, and fails otherwise)."""
    return hmac.compare_digest((given or "").encode("utf-8", "replace"), key.encode("utf-8", "replace"))


# An inline script that a page's own first script runs, once what it waits for is in, rather than the browser as it
# reads the page: so the library page's script, after its data.js, may load the reader's engine (page_policy)
LATER = re.compile(r'<script(?: type="text/hylly-later" id="[a-z]+")?>(.*?)</script>', re.S)


def script_hashes(text: str) -> str:
    """Each inline script of a page by its hash, as a policy's script-src names those that may run: no other, however
    it got into the page. One to be run later (LATER) as well."""
    return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode("ascii") + "'"
                    for s in LATER.findall(text))


@functools.lru_cache(maxsize=4)
def login_policy(template: str) -> str:
    """The login page's Content-Security-Policy, and the one-time link's page's: nothing but the form, no other site may
    frame it, and no script but the template's own two (the colours this browser chose, and the address's part after
    #), by their hashes, worked out from the template, before anything is filled in. Styles inline, for the theme.
    Pictures: its own icons ('self', which answer without a login) and data:; and the app description it links, for a
    phone that puts the library on its home screen from here (it tells nothing about the library either)."""
    return (f"default-src 'none'; style-src 'unsafe-inline'; script-src {script_hashes(template)}; img-src 'self' data:; "
            "manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


@functools.lru_cache(maxsize=16)
def page_policy(text: str, covers: tuple[str, ...] = (), book_fonts: bool = True, ancestors: str = "'none'",
                meanings: bool = True) -> str:
    """The Content-Security-Policy of one of Hylly's own pages. Scripts: only its own inline scripts, by their hash, and
    the files those load ('strict-dynamic': the library page's data.js, the reader's engine under /foliate/), never a
    script file for its address alone, not even this server's: whatever the server sends as a script, a library's file
    or a book's, can't run by being named in a page. (A module imported by a script allowed by its hash alone, Chrome
    refuses all the same: so the library page's own script is run by its first one, LATER.) Pictures: the library's,
    and on the library page those of the sites its shop covers come from (covers), by name. A book opened in the reader
    shows in frames that inherit this, so nothing a book brings can run, read the library in your name, or send anything
    elsewhere: the only other sites the page may ask anything are Wikipedia and Wiktionary, for the reader's word
    lookup, unless meanings ([reader] word_meanings) is off, and those covers' (the reader narrows each page of a book
    to the book's own pictures: catalog_page.html, withoutScripts). A book's fonts reach the page as data: or blob:
    addresses, and its own by /fonts/, so without book_fonts the browser refuses only a book's. ancestors: who may show
    the page in a frame. Nobody, except that the reader's page says 'self': Safari hands a page's policy on to the blob:
    frames it makes, this line too, so under 'none' it refuses to show a book's pages, and the book waits at 100% for
    ever. Other sites still can't frame the page."""
    return (f"default-src 'self'; script-src 'strict-dynamic' {script_hashes(text)}; "
            "style-src 'self' 'unsafe-inline' blob:; "
            f"img-src 'self' data: blob:{''.join(f' https://{site}' for site in covers)}; "
            f"font-src 'self'{' data: blob:' if book_fonts else ''}; media-src 'self' data: blob:; "
            f"connect-src 'self' blob:{' https://*.wikipedia.org https://*.wiktionary.org' if meanings else ''}; frame-src 'self' blob:; "
            "worker-src 'self' blob:; manifest-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            f"frame-ancestors {ancestors}")


def library_name() -> str:
    """What the library calls itself, [server] name in the settings: read when the server starts."""
    name = settings.get("server")["name"]
    return name.strip()[:60] if name.strip() else "Library"


def source_url() -> str:
    """Where this Hylly's source is, [server] source_url in the settings, read when the server starts: the one address
    the Source links of its pages have (the login page's, the library page's from /api/session, the help's)."""
    return settings.get("server")["source_url"]


def page_headers(text: str, covers: tuple[str, ...] = (), reader: bool = False) -> dict:
    """reader: the page shows books in frames of its own (page_policy, ancestors). Cross-Origin-Opener-Policy: another
    site's tab, whether one of Hylly's pages opened it or it opened one of them, has no hold on that page, so it can't
    send the library's tab to a false login page (the reader opens a book's link so as well: catalog_page.html,
    bookLinkOut). Browsers heed it over HTTPS and on this computer; at home over plain HTTP, the reader's way is what
    stops it."""
    said = settings.get("reader")
    policy = page_policy(text, covers, said["book_fonts"], "'self'" if reader else "'none'", said["word_meanings"])
    return {"Content-Security-Policy": policy, "X-Frame-Options": "DENY", "Cross-Origin-Opener-Policy": "same-origin"}


def plain_words(rel: str) -> bool:
    """Whether a path in the library (without its leading /) can name a file the server sends, by its words alone,
    before the disk is asked anything: as a record's path can (paths.inside_library: no . or .., no empty part, and on
    Windows no backslash, which could make \\\\other-pc\\share of it, nor a colon), and on Windows none that Windows
    quietly changes, somewhere else (paths.renamed_on_windows: a device name, a name ending in a dot or a space). On
    Linux and macOS those are ordinary names, and their books open."""
    return paths.inside_library(rel) and not (paths.WINDOWS and paths.renamed_on_windows(rel))


def library_path(root: Path, path: str) -> Path | None:
    """A request's path (decoded, with or without its leading /) as a file under root, resolved, or None when it can't
    be one: by its words first (plain_words), then by where it leads, as a record's file is (paths.library_file: a link
    inside the library that leads out of it is refused)."""
    rel = path.lstrip("/")
    if not plain_words(rel):
        return None
    return paths.library_file(root, rel, resolved=True)


def _private_folder(root: Path, name: str) -> bool:
    """Whether the folder called name at the top of the library is state\\ or curation\\ (PRIVATE_FOLDERS): by its name,
    case folded (the long s of ſtate is an s then, as lower() never made it), and failing that by the disk. A
    filesystem may open a folder by names no rule here knows (one that ignores case or folds letters its own way, as
    macOS's may), and state\\ may be a link to another folder of the library: the same device and file number is the
    same folder, whatever it is called. (A file number of 0 is a disk that keeps none.) Only refusing goes by this:
    what is served, catalog\\'s own files among them, is matched by its plain name, as Handler._file matches it."""
    if name.casefold() in PRIVATE_FOLDERS:
        return True
    try:
        asked = os.stat(root / name)
    except OSError:
        return False
    for private in PRIVATE_FOLDERS:
        try:
            there = os.stat(root / private)
        except OSError:
            continue
        if asked.st_ino and (asked.st_dev, asked.st_ino) == (there.st_dev, there.st_ino):
            return True
    return False


def refused_files(root: Path, cat: library.Catalog) -> list[str]:
    """The files the catalog lists that the server refuses, in its order: offered in the page and the feeds, they would
    never open, so hylly doctor names them. By the server's own checks: every path by its words (plain_words), and one
    with a name starting with a dot, or at the top of the library a name the server never serves, by the disk too
    (library_path, Handler._servable). Any other it serves while the file is there; that a link might lead it out of
    the library isn't asked here, as a large library on a share would take a question of the disk for every file. A
    file that isn't there isn't counted: it's gone, not refused (catalog.unlisted)."""
    root = root.resolve()
    files = list(library.catalog_files(cat.get()))
    listed = frozenset(files).__contains__  # read once for the whole look, as library.Catalog.lists would for each
    out = []
    for rel in files:
        if plain_words(rel):
            parts = rel.split("/")
            top = parts[0].casefold()
            if not any(p.startswith(".") for p in parts) and not top.startswith("_") and top not in (*PRIVATE_FOLDERS, "catalog"):
                continue
            target = library_path(root, rel)
            if target is not None and (not target.is_file() or Handler._servable(root, target, listed)):
                continue
        out.append(rel)
    return out


def _host_of(address: str) -> str | None:
    """The host an address from a request names (an Origin, a Host), or None when it can't be read as one: urlsplit
    refuses some, such as http://[not-an-address]/, with an error that quotes them."""
    try:
        return urlsplit(address).hostname
    except ValueError:
        return None


def own_name() -> str:
    """This computer's own name, as the system has it (gethostname): no name server is asked, so nothing waits on one."""
    return socket.gethostname().lower().rstrip(".")


def _an_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Hosts:
    """The names a request may call this server by (its Host header). Without the check, a web page whose own name
    merely points at this computer (DNS rebinding) could read the library through the browser of anyone at home. So:
    localhost; any address itself, as a rebinding needs a name, and an address is no one's to point elsewhere; this
    computer's own name as the system gives it (own_name), plain, with a home network's suffixes (SUFFIXES) and as
    its Tailscale name (<name>.<tailnet>.ts.net: only in address's tailnet when address is a Tailscale name); the
    name in [server] address; and any you list in [server] allowed_hosts. Nothing is looked up, neither as it starts
    nor in a request: the name is read once, the first time a request needs it, and a name is only compared."""

    SUFFIXES = ("local", "lan", "home", "home.arpa", "localdomain")

    def __init__(self, extra=(), tailnet: str | None = None) -> None:
        self.extra = {str(h).lower().rstrip(".") for h in extra if h}  # address's name, and allowed_hosts
        self.tailnet = tailnet.lower().rstrip(".") if tailnet else None  # tail1234.ts.net: the only one, when known
        self._own: frozenset[str] | None = None

    @classmethod
    def from_settings(cls) -> Hosts:
        saved = settings.get("server")  # allowed_hosts a list of names, address a whole address, checked there
        named = (_host_of(saved["address"]) or "").lower().rstrip(".")
        tailnet = named.partition(".")[2] if named.endswith(".ts.net") else None
        return cls([named, *saved["allowed_hosts"]], tailnet)

    def own(self) -> frozenset[str]:
        """This computer's own name, whole and its first part (a system may give lantern-pc.lan for lantern-pc). No lock:
        two requests that read it at once get the same answer."""
        if self._own is None:
            name = own_name()
            self._own = frozenset({name, name.partition(".")[0]} - {""})
        return self._own

    def allows(self, host_header: str | None) -> bool:
        if not host_header:
            return True  # an old app that names no host: no browser, so no rebinding
        if "@" in host_header or "/" in host_header or "\\" in host_header:  # a host is a name and a port, nothing more
            return False
        try:
            host = (urlsplit("//" + host_header).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if not host:
            return False
        if host in LOCAL_NAMES or host in self.extra or _an_address(host) or host in self.own():
            return True
        first, _, rest = host.partition(".")
        tailscale = rest.endswith(".ts.net") and rest == (self.tailnet or rest)
        return first in self.own() and (rest in self.SUFFIXES or tailscale)


# What a proxy set up wrong looks like, noted for hylly doctor and server.log (Noted, Handler._note), by the key it has
# in the server's note: forwarding headers from an address not in [server] trusted_proxies, which were ignored, so its
# requests counted as its own; and a wrong login or a lockout from a trusted proxy's own address, which no forwarding
# header named another sender for. Either way all a proxy's visitors are one sender to the lockout.
NOTED = {"headers_ignored_from": "Forwarding headers came from {address}, which isn't one of trusted_proxies under [server], "
                                 "so they were ignored and its requests counted as its own. If it is a proxy of yours, list "
                                 "it there and restart the server.",
         "unnamed_by_proxy": "A wrong login or a lockout came from {address}, one of trusted_proxies, with no forwarding "
                             "header naming another sender. If a proxy or tunnel there hands on none, all its visitors are "
                             "one sender to the lockout, and a stranger's wrong passwords lock everyone out: make it set or "
                             "add to X-Forwarded-For (a browser on that computer itself looks the same)."}


class Noted:
    """Who did what NOTED tells of: each address told once for each kind (told: server.log, and the server's note that
    hylly doctor reads), the first KEPT of them, and counted in memory."""

    KEPT = 5

    def __init__(self, told: Callable[[str, str, list[str]], None] = lambda key, address, addresses: None) -> None:
        self.told = told  # (the kind, the new address, all of them so far)
        self.counts: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def saw(self, key: str, address: str) -> None:
        with self._lock:
            counts = self.counts.setdefault(key, {})
            new = address not in counts and len(counts) < self.KEPT
            if address in counts or new:
                counts[address] = counts.get(address, 0) + 1
            addresses = sorted(counts) if new else None
        if addresses is not None:  # outside the lock: no other request waits on what telling takes
            self.told(key, address, addresses)



# The home-screen icons' version, their own digest: in their addresses (Handler.MANIFEST, the pages' apple-touch-icon)
ICON_VERSION = hashlib.sha256(b"".join(Path(__file__).with_name(f"icon-{n}.png").read_bytes() for n in (180, 192, 512))).hexdigest()[:8]

class Handler(BaseHTTPRequestHandler):
    root: Path
    catalog: library.Catalog
    last_request: list[float]
    stats: dashboard.Stats
    editors_secret: bytes  # signs each editor's key for the library's changes from the library page (Caller); new at every start
    server_key: str  # for the server and database pages, which only owners see; another one, so an edit key can't stop the server
    personal_secret: bytes  # signs each person's key for their own writes (Caller); new at every start
    info: dict = {}  # the server's note (instance.py), once it runs
    gate: access.Gate
    watch: bell.Watch
    access_log: access.AccessLog
    ereader: library.Device
    proxy_header: str = "X-Forwarded-For"  # the one header naming the sender behind a proxy ([server] proxy_header)
    proxies: tuple = access.LOOPBACK  # whose forwarding headers count ([server] trusted_proxies): _trusted
    hosts: Hosts = Hosts()
    address: str = ""  # the address people use ([server] address), for the links made for other devices
    noted: Noted = Noted()  # what a proxy set up wrong looks like (NOTED), for the log and hylly doctor
    server_version, sys_version = "Hylly", ""  # the Server header names no program or version
    timeout = 60  # a connection that sends nothing for a minute is closed: slow senders can't hold every thread
    REQUEST_DEADLINE = 20  # seconds for the request line, headers and body, all together: a slow sender can't stretch them
    # One request to a connection, then it closes: protocol_version stays Python's HTTP/1.0, with no keep-alive. Keep it
    # so. A body is read by its Content-Length alone (do_POST), and one in chunks (Transfer-Encoding) is refused unread
    # (parse_request): on a connection kept open, a body left unread would be read as the next request, with headers its
    # sender chose, and, through a proxy on this computer, without its forwarding headers, as this computer's own.

    def setup(self) -> None:
        super().setup()
        self._incoming = _Deadline(self.connection, time.monotonic() + self.REQUEST_DEADLINE)
        self.rfile = io.BufferedReader(self._incoming)

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if self.command != "POST":  # a POST's body comes under the same deadline (do_POST)
            self._request_in()
        if ok:  # the address, split once, and its query read once, before anything looks at them
            try:
                self._url = urlsplit(self.path)
                self._query = parse_qs(self._url.query)
            except ValueError:  # such as GET http://[not-an-address]/: refused, as nothing could answer it
                self.close_connection = True
                self._send(400, "text/plain", b"bad address", cache=False)
                return False
            if "Transfer-Encoding" in self.headers:  # a body in chunks, which nothing here reads: refused, and closed
                self.close_connection = True
                self._send(400, "text/plain", b"a body in chunks isn't taken: send it with its Content-Length", cache=False)
                return False
        return ok

    def _request_in(self) -> None:
        """The whole request has come: from here on the answer takes as long as it takes."""
        self._incoming.done = True
        self.connection.settimeout(self.timeout)
    UNLOGGED = ("/ping", "/server/status", "/healthz")  # the pages' own checks, every minute or every few seconds, and a health check's

    def log_message(self, fmt, *args) -> None:  # quiet: requests go to the access log instead
        pass

    def handle_one_request(self) -> None:
        self._status, self._length, self._user, self._renewal, self._sent = None, None, None, None, set()
        self._paced = False  # its 429 is a person's changes a minute (_personal), not a lockout (_note)
        self._url = None  # the address, split (parse_request); None when the request had none that could be read
        self._query: dict[str, list[str]] = {}  # and its query, read (parse_request)
        self._path = ""  # the address's path as the dispatcher routed it (_route): decoded, once logged in
        # Who is asking (_who): made anew for each request, here, never once for the connection, so that nothing of one
        # request's is the next one's, should a connection ever carry several (protocol_version)
        self._caller: Caller | None = None
        try:
            super().handle_one_request()
        except ConnectionError:
            raise  # the other end went away: nothing to answer
        except (SystemExit, Exception) as e:  # noqa: BLE001 - what nothing else caught still gets an answer
            self._failed(e)
        path = self._url.path if self._url else ""
        headers = getattr(self, "headers", None) or {}  # none when the request could not be read
        if self._url is not None:  # a request read now: at a kept-alive connection's end, the headers are the last one's
            self._note(headers)
        if self._status is not None and path not in self.UNLOGGED:
            sender = access.client_address(self.client_address[0], headers, self.proxy_header, self.proxies)  # self.sender needs headers
            # The alarms first: the access log can't be written on a full disk, and that mustn't quiet them
            self.watch.request(sender, self._user, getattr(self, "command", None) or "", unquote(path), self._status)
            self.access_log.write(sender, self._user, self.requestline, self._status, self._length,
                                  headers.get("Referer"), headers.get("User-Agent"))

    FAILED: set[tuple] = set()  # the kinds of failure that nothing else caught, in server.log once each (first_failure)
    FAILED_MAX = 200  # kinds remembered: a sender can't make the set, or server.log, grow for ever
    FAILED_SAID: dict[tuple, list] = {}  # of those, when each was last said, and how often it came since (failed_again)
    FAILED_AGAIN = 3600  # seconds: a failure that lasts is said again after this, with how often it came (failed_again)
    FAILED_TEXT = b"Something went wrong in the server. Its log says what."

    UNUSABLE = "The library's database can't be changed from here: the server's log says why."
    LEFT_ALONE = ("{whose} is from a newer version of Hylly, and is left alone until this one is updated.",
                  "{whose} is from a test version of Hylly from before its first release, and is left alone: hylly doctor, "
                  "on the server's computer, says what to do.")  # people.Newer, and one of those (older)

    def _left_alone(self, e: people.Newer, whose: str = "What's kept for you") -> None:
        """A change refused, as the database is one this version leaves alone: a newer version's, or one from before
        the first release, each in its own words. 503, not refused for good: the library page's outbox keeps such a
        change and sends it again later, as an update (or the database put right) lets it be kept."""
        return self._json({"ok": False, "error": self.LEFT_ALONE[e.older].format(whose=whose)}, 503)

    def _failed(self, e: BaseException) -> None:
        """A request that failed where nothing else caught it, such as a mistake in the settings: a fixed answer, as
        the message may name this computer's folders; the message goes to server.log, once for each kind of failure
        (first_failure), and again each hour it goes on, with how often (failed_again). The database of a newer version
        (or of a test version, left alone as one) is said so with 503, as the page's outbox waits on that and sends the
        change again once it's updated. One that doesn't keep this library's records is a change refused (409): that
        stays so until the owner moves them (hylly server take-over), and only the library's own records, the editors'
        changes, meet it, never what's kept about a person, which the outbox sends."""
        if isinstance(e, (db.Newer, db.NotHere)) and self._status is None:
            if not isinstance(e, db.Moved) and first_failure(e):  # records that moved away: said once, as it was noticed
                _stamp(f"A change was refused: {e}")
            try:
                return self._json({"ok": False, "error": ("The library's database is from a newer version of Hylly: nothing can be "
                                                          "changed until it's updated." if isinstance(e, db.Newer) and not isinstance(e, db.Older)
                                                          else self.UNUSABLE)}, 503 if isinstance(e, db.Newer) else 409)
            except Exception:  # noqa: BLE001 - the connection is past answering
                return
        said = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {str(e)[:300]}"  # the text may be what was sent
        if tell_failure(e, "A request failed", said) and not isinstance(e, SystemExit):  # the first time, and not one of
            print(_trace(e), end="", file=sys.stderr, flush=True)  # Hylly's own messages: where it came from, for a fix
        self.close_connection = True
        if self._status is None:  # nothing sent yet
            try:
                self._send(500, "text/plain; charset=utf-8", self.FAILED_TEXT, cache=False)
            except Exception:  # noqa: BLE001 - the connection is past answering
                pass

    @staticmethod
    def _not_read(e: BaseException) -> None:
        """What's kept that a part of an answer couldn't read just now (RECORDS_FAILED), left out of it (a count, what's
        yours in /api/session): said in server.log as the record handlers say it (tell_failure), once for each kind and
        again each hour it goes on. A database that doesn't keep this library's records is said where it's noticed."""
        if not isinstance(e, db.NotHere):
            tell_failure(e, "What's kept couldn't be read or kept", f"{type(e).__name__}: {str(e)[:300]}")

    def _not_kept(self, e: BaseException, words: str = "It couldn't be kept") -> None:
        """What's kept that couldn't be read or kept just now (RECORDS_FAILED), answered as the library page expects it,
        in JSON: words, and the error's kind, never its own words, which may name this computer's folders. A database
        that doesn't keep this library's records is a change refused, as anywhere (_failed). server.log says what it
        was, once for each kind, and again each hour it goes on, with how often (tell_failure). An error that comes once
        an answer is on its way is the connection's, not the records': the client went, and there's no one to answer."""
        if self._status is not None:
            raise e
        if isinstance(e, db.NotHere):
            return self._failed(e)
        tell_failure(e, "What's kept couldn't be read or kept", f"{type(e).__name__}: {str(e)[:300]}")
        return self._json({"ok": False, "error": f"{words} ({type(e).__name__})."}, 500)

    def send_response(self, code: int, message: str | None = None) -> None:
        self._status = code
        super().send_response(code, message)

    def send_header(self, keyword: str, value: str) -> None:
        if keyword.lower() == "content-length":
            self._length = value
        self._sent.add(keyword.lower())
        super().send_header(keyword, value)

    @property
    def sender(self) -> str:
        """The sender's address, the real one behind a trusted proxy (access.client_address)."""
        return access.client_address(self.client_address[0], self.headers, self.proxy_header, self.proxies)

    def _admitted(self) -> bool:
        """The login every request needs, wherever it comes from (access.Gate.check); refuses the request otherwise. A
        browser opening a page goes to the login page; a page's own requests just get 401, never the browser's password
        dialog; reading apps are asked for their Basic login."""
        headers = self._without_basic() if "Sec-Fetch-Mode" in self.headers or self._from_own_page() or self._for_the_reader() else self.headers
        users, roles = self.gate.accounts()  # once for the whole request: who is asking (Caller) keeps them
        session = self.gate.session_user(headers, users)
        verdict, self._user = self.gate.check(self.sender, headers, session=session, users=users)
        self._caller = self._caller_of(self._user if verdict == "ok" else None, session, users, roles)
        if verdict == "locked":
            self._send(429, "text/plain", b"too many wrong logins; try again later", headers={"Retry-After": "900"})
        elif verdict == "slow":  # access.Gate._turn: no turn now for that name, or for anyone
            self._send(429, "text/plain", b"too many wrong logins lately; try again in a few seconds",
                       headers={"Retry-After": f"{access.SLOW_WAIT:.0f}"})
        elif verdict == "login" and "Authorization" not in headers and self._browser():
            if self._opening_a_page():
                self._redirect("/login?" + urlencode({"next": self.path}))
            else:
                self._send(401, "text/plain", b"login needed")
        elif verdict == "login":
            self._send(401, "text/plain", b"login needed",
                       headers={"WWW-Authenticate": 'Basic realm="Library", charset="UTF-8"'})
        if verdict == "ok" and session and self._renews():
            self._renewal = self.gate.sessions.renew(access.cookie_token(headers.get("Cookie")))
        return verdict == "ok"

    def _caller_of(self, user: str | None, session: str | None, users: dict[str, str], roles: dict[str, str]) -> Caller:
        return Caller(user, session, users, roles, (self.personal_secret, self.editors_secret))

    def _who(self) -> Caller:
        """Who is asking (Caller), worked out once a request: by the login check every address but an OPEN one's takes
        first (_admitted); at an OPEN one, on first use, as nobody logged in yet, with the browser login its cookie
        names, if any (the login page goes straight on for one)."""
        if self._caller is None:
            users, roles = self.gate.accounts()
            self._caller = self._caller_of(None, self.gate.session_user(self.headers, users), users, roles)
        return self._caller

    def _renews(self) -> bool:
        """Where using the library moves a browser's login on: opening a page, and the page's own questions (the
        renewed cookie goes with their answers, which no cache keeps), not the reader's program files."""
        return self._opening_a_page() or self._url.path.startswith(("/api/", "/ping"))

    def end_headers(self) -> None:
        if self._renewal:  # the browser keeps its cookie as long as the login now lasts
            for cookie in self._cookies(access.cookie_token(self.headers.get("Cookie")), self._renewal):
                self.send_header("Set-Cookie", cookie)
            self._renewal = None
        if "x-content-type-options" not in self._sent:  # a file is what its type says, never guessed into a page
            self.send_header("X-Content-Type-Options", "nosniff")
        if "referrer-policy" not in self._sent:
            self.send_header("Referrer-Policy", "same-origin")
        if "cross-origin-resource-policy" not in self._sent:  # another site's page can't take in an answer (_cross_site)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()
        if self.command == "HEAD":  # the headers only: whatever the answer would write next goes nowhere
            self.wfile = _Nowhere(self.wfile)

    def _without_basic(self) -> HTTPMessage:
        """A browser's headers without its Basic login. Browsers log in only with the login page, so Log out means
        logged out, even when a browser (Safari, for one) goes on sending a password it remembers from a password
        dialog. Reading apps, which send no Sec-Fetch-Mode and show none of the library's pages (_from_own_page), keep
        Basic."""
        kept = HTTPMessage()
        for name, value in self.headers.items():
            if name.lower() != "authorization":
                kept[name] = value
        return kept

    def _browser(self) -> bool:
        """A web browser, not a reading app: browsers send Sec-Fetch-Mode over HTTPS and to this computer, and what one
        of the library's own pages asks says which page it came from (_from_own_page); a browser opening a page at a
        plain http address says Mozilla and takes HTML."""
        h = self.headers
        return ("Sec-Fetch-Mode" in h or self._from_own_page() or self._for_the_reader()
                or ("Mozilla/" in (h.get("User-Agent") or "") and "text/html" in (h.get("Accept") or "")))

    def _for_the_reader(self) -> bool:
        """One of the reader's own program files or the fonts it brings (PROGRAM_FILES): only a browser asks for these,
        also from a book's own frame, a blob: one, which sends no Referer (its fonts). No reading app does."""
        return self._url.path.startswith(tuple(self.PROGRAM_FILES))

    FEEDS = ("/opds", "/ereader/opds", "/me/")  # where a reading app's requests come from, if it says at all
    FEED_PATHS = ("/opds", "/ereader/opds", "/me/opds", "/me/ereader/opds")  # the feeds' own addresses

    def _asks_for_a_feed(self) -> bool:
        """The request names a feed's type in its Accept (Atom, OPDS), as a reading app may, whatever else it says it
        takes: never sent on to the library page, a Mozilla one with text/html in it too."""
        accept = (self.headers.get("Accept") or "").lower()
        return "atom" in accept or "opds" in accept

    def _from_own_page(self) -> bool:
        """Asked by a page of this library, opened in a browser: the library page and its reader, the help, the server
        and database pages, a file opened in a tab. Its Referer is that page's address on this server, which a browser
        sends with whatever a page asks (end_headers: Referrer-Policy same-origin), fetches and pictures alike. That
        holds at a plain http address at home too, where browsers send no Sec-Fetch headers; behind a trusted proxy that
        calls the server by another name, the name the browser used is its X-Forwarded-Host. A reading app shows none of
        these pages, so a feed it came from doesn't count (FEEDS). This only ever makes a request stricter: no password
        dialog, and no Basic login."""
        try:
            came = urlsplit(self.headers.get("Referer") or "")
        except ValueError:
            return False
        return bool(came.netloc) and came.netloc in self._own_hosts() and not came.path.startswith(self.FEEDS)

    def _opening_a_page(self) -> bool:
        """A browser opening a page, not a page's own request: as Sec-Fetch-Mode says, or where a browser sends none (a
        plain http address), by taking HTML, which a page's own questions and pictures don't ask for."""
        mode = self.headers.get("Sec-Fetch-Mode")
        return self.command == "GET" and self._browser() and (mode == "navigate" if mode else "text/html" in (self.headers.get("Accept") or ""))

    def _cross_site(self) -> bool:
        """A request another site's page made, other than opening a page: a script tag for catalog/data.js, a picture,
        a fetch, and any form it sends (a POST navigates too, but none of the library's comes from another site; one
        to /login could log your browser in as someone else). Refused (403), so a web page you happen to visit can't
        read or change the library through your browser's login; and every answer says it is for the
        library's own pages only (end_headers), for browsers that don't say where a request comes from. A link or a
        bookmark from elsewhere still opens the library (a GET or HEAD that navigates: without a login, at the login
        page), and so does an address typed (none); another port or name of the same site (same-site) is let through, as
        that header still keeps its pages from taking anything in. Reading apps send no Sec-Fetch headers at all.
        Browsers send these headers only over
        https and to this computer's own addresses, so on the home network's plain http the Origin check of the posts
        (_same_origin) is what refuses another site's form: the two are not the same check twice. Returns True when it
        was refused."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        opens = self.command in ("GET", "HEAD") and (self.headers.get("Sec-Fetch-Mode") or "").strip().lower() == "navigate"
        if site != "cross-site" or opens:
            return False
        self._send(403, "text/plain", b"Another site's page can't use this library: open it by its own address.", cache=False)
        return True

    # ---- the login page, for browsers ----
    # The login page's: no other site may frame it; its Content-Security-Policy, by its scripts' hashes, comes with it
    # (login_policy, _send_login)
    LOGIN_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin"}
    # The page that asks to use a one-time link: its address has the link in it, which no request after may carry on, so
    # its button's form and what follows say only where they came from by the site. Not "no-referrer": a browser then
    # sends the form's Origin as null, and this site's own form would be refused as another's (_same_origin).
    LINK_HEADERS = {**LOGIN_HEADERS, "Referrer-Policy": "origin"}
    NO_USERS = "Nobody can log in yet: on the server computer, add a user with hylly user add NAME."
    NO_DATABASE = "Nobody can log in: the library's database can't be used from here. The server's log says why."
    LINK_GONE = ("This sign-in link has been used, or is too old. Log in with your name and password, or ask for a new "
                 "link: hylly user link NAME on the server computer.")
    LOCKED = f"Too many wrong passwords from here: try again in {access.WINDOW // 60} minutes."
    SLOW = "Too many wrong passwords lately: try again in a few seconds."  # access.Gate._turn
    THEME_MARK = "/* every theme, from the server */"  # where the login page's theme goes (themes.current), as it is

    def _trusted(self) -> bool:
        """The connection comes from one of [server] trusted_proxies, whose forwarding headers count: for the sender's
        address (sender), the HTTPS flag (_https) and the name the browser used (_own_hosts). From anyone else they are
        ignored (and noted: Noted). Who may come in never hangs on it: every request logs in."""
        return access.is_trusted(self.client_address[0], self.proxies)

    def _note(self, headers) -> None:
        """What a proxy set up wrong looks like (NOTED), from the request just answered: forwarding headers from an
        address not in trusted_proxies; a wrong login (a Basic login or the login page's refused) or a lockout from a
        trusted proxy's own address, which no forwarding header named another sender for."""
        peer = access.unmapped(self.client_address[0])
        if not self._trusted():
            if any(h in headers for h in access.FORWARDED):
                self.noted.saw("headers_ignored_from", peer)
            return
        tried = self._status == 401 and ("Authorization" in headers or (self.command == "POST" and self._url.path == "/login"))
        locked = self._status == 429 and not self._paced  # a lockout or a slowdown, not a person's changes a minute
        if (locked or tried) and access.client_address(peer, headers, self.proxy_header, self.proxies) == peer:
            self.noted.saw("unnamed_by_proxy", peer)

    def _own_hosts(self) -> set[str]:
        """The names this request calls the server by: its Host, and behind a trusted proxy the X-Forwarded-Host it
        hands on, the name the browser used, when it is one of the server's own names (Hosts) as the Host must be."""
        forwarded = self.headers.get("X-Forwarded-Host") if self._trusted() else None
        hosts = {self.headers.get("Host"), forwarded if forwarded and self.hosts.allows(forwarded) else None}
        return hosts - {None, ""}

    def _host_ok(self) -> bool:
        """The request calls this server by one of its own names (Hosts); else 421, and nothing else is looked at."""
        if self.hosts.allows(self.headers.get("Host")):
            return True
        self._send(421, "text/plain", b"This server doesn't answer to that name. If it should, add the name to "
                                      b"allowed_hosts under [server] in settings.toml.")
        return False

    def _https(self) -> bool:
        """Whether the browser came over HTTPS, as a trusted proxy says: in X-Forwarded-Proto (Tailscale Serve and
        Funnel, cloudflared, Caddy and nginx set up for it send it), or else in Forwarded's proto. The first one there
        is what the browser used. Nobody else's word counts, and without one the request is plain HTTP, as direct ones
        are: a Secure cookie over plain HTTP would never come back."""
        if not self._trusted():
            return False
        said = self.headers.get("X-Forwarded-Proto") or ""
        if not said:
            found = re.findall(r'proto="?(\w+)', self.headers.get("Forwarded") or "", re.I)
            said = found[0] if found else ""
        return said.split(",")[0].strip().lower() == "https"

    def _same_origin(self) -> bool:
        """A form from this site's own pages: the Origin a browser sends with it names this server. Another site's
        form can neither log you in as someone else nor log you out."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # not a browser's form
        try:
            return urlsplit(origin).netloc in self._own_hosts()
        except ValueError:  # not an address at all: no page of this site sent it
            return False

    def _cookies(self, token: str | None, seconds: int = access.SESSION_DAYS * 86400) -> list[str]:
        """Set-Cookie for a new or renewed session, or to end one (token None). Over HTTPS the name only HTTPS can set."""
        name = access.COOKIE_HTTPS if self._https() else access.COOKIE
        flags = "; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if self._https() else "")
        if token:
            return [f"{name}={token}; Max-Age={seconds}{flags}"]
        return [f"{n}=; Max-Age=0{flags}" for n in ((access.COOKIE_HTTPS, access.COOKIE) if self._https() else (access.COOKIE,))]

    def _see_other(self, location: str, cookies: list[str]) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, status: int = 200, error: str = "", name: str = "", wanted: str | None = None) -> None:
        if wanted is None:
            wanted = (self._query.get("next") or [""])[0]
            if (once := self._once()) is not None:
                return self._link_page(once, wanted)
            if self._who().session:  # logged in already: straight on
                return self._see_other(_next_page(wanted), [])
        if not self._who().users:  # whatever else is wrong, this is what to put right first
            error = self.NO_DATABASE if self.gate.unusable else self.NO_USERS
        values = {"error": error, "name": name[:200], "next": _next_page(wanted), "library": self.library_name,
                  "name_focus": "" if name else "autofocus", "password_focus": "autofocus" if name else ""}
        self._send_login(status, values, _login_template(), self.LOGIN_HEADERS)

    def _send_login(self, status: int, values: dict, template: str, headers: dict) -> None:
        """The login page, or the one that asks to use a sign-in link, with its values filled in, escaped, and the themes
        (its policy takes no stylesheet; checked colours only), the library's own worn unless this browser has picked
        one, all in one pass over the template: what was typed is never looked at again, so the theme's marker typed as
        a name stays text."""
        look, _, theme = themes.current()
        # its Source link, the library's own theme, and how long a login lasts unused, as access.py has it
        values = {**values, "source": self.source_url, "look": look, "days": str(access.SESSION_DAYS)}
        text = fill(template, values, {self.THEME_MARK: theme})  # one pass: words.fill
        self._send(status, "text/html; charset=utf-8", text.encode("utf-8"), cache=False,
                   headers={**headers, "Content-Security-Policy": login_policy(template)})

    def _once(self) -> str | None:
        """The one-time sign-in link's token in /login?once=..., exactly so written, as the access log masks it
        (access.masked); None when there's none."""
        found = re.search(r"(?:^|&)once=([^&]*)", self._url.query)
        return found[1] if found else None

    LINK_FORM = """<form method="post" action="/login">
  <h1>{{library}}</h1>
  <p>A one-time link to log in as <b>{{user}}</b>.</p>
  <input type="hidden" name="once" value="{{once}}">
  <input type="hidden" name="next" value="{{next}}">
  <button type="submit" autofocus>Log in as {{user}}</button>
  <p class="note">The link works once. This browser then stays logged in for {{days}} days, or until you log out.</p>
</form>"""

    def _link_page(self, once: str, wanted: str) -> None:
        """A one-time sign-in link opened: a page with one button that uses it. Opening the link uses nothing up, so a
        program that only looks at what's behind a link (a chat app's preview, say) can neither use it up nor log
        itself in; and another site can't send your browser to it to log you in as someone else, as the button's form
        goes only from this page (_login_post). A link that doesn't work gets the login page, which says so."""
        user = self.gate.sessions.link_user(once)
        if user is None:
            return self._login_page(410, self.LINK_GONE, "", wanted)
        values = {"library": self.library_name, "user": user, "once": once, "next": _next_page(wanted)}
        page = re.sub(r"<form .*?</form>", lambda m: self.LINK_FORM, _login_template(), count=1, flags=re.S)
        self._send_login(200, values, page, self.LINK_HEADERS)

    def _login_post(self) -> None:
        if not self._same_origin():
            return self._send(403, "text/plain", b"not allowed")
        try:
            form = parse_qs(self._body.decode("utf-8", errors="replace"), max_num_fields=8)
        except ValueError:  # more fields than the form has
            return self._send(400, "text/plain", b"bad form")
        if form.get("once"):  # a one-time sign-in link, from the page that asks to use it
            return self._link_post(form["once"][0], (form.get("next") or [""])[0])
        name, password, wanted = ((form.get(k) or [""])[0] for k in ("username", "password", "next"))
        name, password = name.strip()[:200], password[:1024]  # long enough for any real one; scrypt gets no giant inputs
        verdict, token = self.gate.log_in(self.sender, name, password, self.headers.get("User-Agent") or "")
        self._user = self.gate.tried(name)  # the access log names who logged in, or the name tried if it's a user's
        if verdict == "ok":
            return self._see_other(_next_page(wanted), self._cookies(token))
        if verdict in ("locked", "slow"):
            return self._login_page(429, self.LOCKED if verdict == "locked" else self.SLOW, name, wanted)
        self._login_page(401, "Wrong name or password.", name, wanted)

    def _link_post(self, once: str, wanted: str) -> None:
        """A one-time sign-in link used: logged in as its user, and the link gone (access.Gate.use_link)."""
        verdict, found = self.gate.use_link(self.sender, once, self.headers.get("User-Agent") or "")
        if verdict == "ok":
            self._user = found[0]  # the access log names who logged in
            return self._see_other(_next_page(wanted), self._cookies(found[1]))
        if verdict == "locked":
            return self._login_page(429, self.LOCKED, "", wanted)
        self._login_page(410, self.LINK_GONE, "", wanted)

    def _logout(self) -> None:
        if not self._same_origin():
            return self._send(403, "text/plain", b"not allowed")
        self.gate.sessions.end(access.cookie_token(self.headers.get("Cookie")))
        self._see_other("/login", self._cookies(None))

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # What a phone fetches to put the library on its home screen, login or not: it tells nothing about the library.
    # The pages and the manifest name the icons with ICON_VERSION, the icons' own digest: kept a week as they are, by a
    # browser and by what puts a library on a home screen or in a Dock from its cache, a changed icon comes at a new
    # address at once (the pages' links carry it written in, test_serve holds them to it).
    APP_FILES = {"/icon-180.png": "icon-180.png", "/apple-touch-icon.png": "icon-180.png",
                 "/apple-touch-icon-precomposed.png": "icon-180.png", "/icon-192.png": "icon-192.png",
                 "/icon-512.png": "icon-512.png", "/favicon.ico": "icon-192.png"}
    MANIFEST = {"name": "Library", "short_name": "Library", "start_url": "/catalog/index.html", "scope": "/",
                "display": "standalone", "background_color": "#16171a", "theme_color": "#16171a",
                "icons": [{"src": f"/icon-192.png?v={ICON_VERSION}", "sizes": "192x192", "type": "image/png"},
                          {"src": f"/icon-512.png?v={ICON_VERSION}", "sizes": "512x512", "type": "image/png",
                           "purpose": "any maskable"}]}

    library_name = "Library"
    source_url = PROJECT_URL  # [server] source_url, as the server read it at its start (make_server)

    def _named(self, text: str) -> str:
        """One of Hylly's own pages, its tab saying what the library is called. (The library page itself asks, in /api/session.)"""
        return text.replace("<title>Library", f"<title>{html.escape(self.library_name)}", 1)

    def _licence(self) -> None:
        """One of the licences in LICENCES, by its path exactly as the table has it, as plain text: the help's links to
        the licences of the work by others Hylly includes. Only the table's own paths are read, so no other file of
        those folders, nor anywhere an address could lead, is ever sent."""
        licence = self._path.removeprefix(LICENCE_PATH)
        if licence not in LICENCES:
            return self._send(404, "text/plain", b"not found")
        body = Path(__file__).parent.joinpath(*licence.split("/")).read_bytes()
        self._send(200, "text/plain; charset=utf-8", body, headers={"Cache-Control": "no-cache"})

    def _app_file(self) -> None:
        body = Path(__file__).with_name(self.APP_FILES[self._path]).read_bytes()
        self._send(200, "image/png", body, headers={"Cache-Control": "public, max-age=604800"})

    def _manifest(self) -> None:
        named = {**self.MANIFEST, "name": self.library_name, "short_name": self.library_name}
        self._send(200, "application/manifest+json", json.dumps(named).encode("utf-8"))

    def _healthz(self) -> None:
        """For a health check, without a login: it answers, and says nothing else."""
        self._send(200, "text/plain", b"ok", cache=False)

    def _theme(self) -> None:
        """The themes' colours, nothing of the library's: before any login, as the login page has them."""
        self._send(200, "text/css; charset=utf-8", themes.current()[2].encode("utf-8"),
                   headers={"Cache-Control": "no-cache"})

    def _start(self) -> None:
        """The library's own address: after a login at the root, on to the page that was asked for (_admitted)."""
        self.stats.note(self.sender, self.headers.get("User-Agent", ""))
        self._redirect(_next_page((self._query.get("next") or [""])[0]))

    def _ping(self) -> None:
        """The pages' own check, every minute, that the login and the server are still there."""
        self._send(200, "text/plain", b"ok")

    def _not_there(self) -> None:
        self._send(404, "text/plain", b"not found")

    def _feed(self) -> None:
        """A page of a feed (opds.answer), yours, as every request has logged in: the e-reader's under /ereader/opds and
        /me/ereader/opds, by the address decoded, as the page is read; the address as asked goes to the feed for its own
        links."""
        self.stats.note(self.sender, self.headers.get("User-Agent", ""))
        device = self.ereader if self._path.startswith(("/ereader/", "/me/ereader/")) else None
        status, ctype, body = opds.answer(self.catalog, self.path, device, self.library_name, self._asking())
        # In a sandbox, as an XML file of the library is (library_file): a browser shows a feed as an XML document,
        # which could run a script, were a value ever to miss opds.escape(). Reading apps ignore the header.
        self._send(status, ctype + "; charset=utf-8", body.encode("utf-8"),
                   headers={"Content-Security-Policy": SANDBOX})

    def _library_file(self) -> None:
        """A file of the library (_file), or the library page, its data or its help, this version's own, whatever the
        library holds under their names (_own_file)."""
        self.stats.note(self.sender, self.headers.get("User-Agent", ""))
        if self._path.lstrip("/").lower() in OWN_FILES:
            return self._own_file(self._path.lstrip("/").lower())
        self._file(self._path)

    def do_GET(self) -> None:  # noqa: N802
        if self._host_ok() and not self._cross_site():
            self._route(self.GET)

    do_HEAD = do_GET  # the same answer without its body: reading apps ask before a download. Where an answer is a file, it's
    # left unread (_send_file, _zipped, _program_file); whatever else would be written goes nowhere (end_headers)

    def _route(self, table: dict[str, tuple]) -> None:
        """The one dispatcher, of every request (GET and HEAD by GET, POST by POST: Handler.GET, Handler.POST): an
        address OPEN's, before any login, by the address as sent; any other after the login it needs (401 without one:
        _admitted), by its path decoded: its own row in the table, or the longest beginning it starts with (a row
        ending in *), or not found (404), whoever asks. Then the row's guards in turn, each letting the request on or
        sending its refusal (403: Guard), and then its handler."""
        row = table.get(self._url.path)
        if row is not None and row[0] is OPEN:
            self._path = self._url.path
        else:
            if not self._admitted():
                return
            if self.command != "POST":
                self.last_request[0] = time.monotonic()
            self._path = unquote(self._url.path) or "/"
            row = table.get(self._path)
            if row is None or row[0] is OPEN:  # OPEN's only by the address as sent: written otherwise, it's another
                begins = [key for key in table if key.endswith("*") and self._path.startswith(key[:-1])]
                row = table[max(begins, key=len)] if begins else None
            if row is None:
                return self._not_there()
        guards, handler = row
        for guard in guards if isinstance(guards, tuple) else (guards,):
            if not self._let_on(guard):
                return
        handler(self)

    def _let_on(self, guard: Guard) -> bool:
        """One of a route's guards (Guard): the request let on (True), or its refusal sent."""
        if guard.role and access.ROLES.index(self._role()) < access.ROLES.index(guard.role):
            self._refused(guard.words)
            return False
        return not guard.check or getattr(self, guard.check)()

    def _refused(self, words: str) -> None:
        """A request refused for who is asking (403), in words: in JSON for the library page's questions (/api/), which
        it reads as {ok, error}; as plain text for a page, a file or a feed."""
        if self._path.startswith("/api/"):
            return self._json({"ok": False, "error": words}, 403)
        self._send(403, "text/plain", words.encode("utf-8"))

    def _lent(self) -> bool:
        """The library's files and its feeds are a reader's or more. A borrower reads in the library page, and of these
        gets only what the page itself needs (_borrower_may), however the address is written."""
        if self._role() != "borrower" or self._borrower_may(self._path):
            return True
        self._refused("A borrower reads in the library page; files and feeds aren't lent.")
        return False

    def _for_the_apps(self) -> bool:
        """A feed is for reading apps, which log in with Basic: its address typed into a browser, logged in by the login
        page, opens the library page, not its XML."""
        who = self._who()
        if self._opening_a_page() and not self._asks_for_a_feed() and who.session == who.user:
            self._redirect("/")
            return False
        return True

    def _page_key_ok(self, key: str, which: str = "X-Server-Key") -> bool:
        """Every key check's one test: the request carries key, one of the pages' keys, in its header which, from this
        site's own pages (_same_origin). The header compared by its bytes (same_key), so a value that isn't ASCII is a
        wrong key, refused as one, never a failure."""
        return same_key(self.headers.get(which), key) and self._same_origin()

    def _library_key(self) -> bool:
        """An editor's change carries their own key from the library page (X-Server-Key: Caller.editors_key, new at
        every start), from this site's own pages: no other site's page can make one; a page left open across a restart,
        or for another editor than the one logged in since, gets 403, and then asks /api/session for the key it should
        have (catalog_page.html, keyRenewed): the same person's, it's sent again; another's, the page starts afresh as
        theirs, and nothing goes as them."""
        if self._page_key_ok(self._who().editors_key):
            return True
        self._json({"ok": False, "error": "Not from the library page; reload it and try again."}, 403)
        return False

    def _server_key(self) -> bool:
        """The Server and Database pages' questions and buttons carry their own key (X-Server-Key: Handler.server_key),
        from this site's own pages, so that no other site can ask them, and an edit key can't stop the server."""
        if self._page_key_ok(self.server_key):
            return True
        self._send(403, "text/plain", b"not allowed")
        return False

    def do_POST(self) -> None:  # noqa: N802
        """What the pages send: logging in and out, then, for whoever is logged in, the library page's changes
        (/api/...), the Database page's queries and the Server page's buttons, each address to its handler by the one
        dispatcher (_route, POST)."""
        limit = MAX_LOGIN_BODY if self._url.path in ("/login", "/logout") else MAX_BODY
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > limit:  # read nothing: a body of no stated or a giant length isn't waited for
            self.close_connection = True
            return self._send(413 if length > limit else 400, "text/plain", b"bad request body")
        self._body = self.rfile.read(length)  # read first: answering before it, as a refusal does, resets the connection
        self._request_in()
        if self._host_ok() and not self._cross_site():
            self._route(self.POST)

    def _ask(self) -> dict | None:
        """What a change sends: its JSON body, an object; None for anything else (not JSON, or not an object), which
        each handler refuses in its own words (400)."""
        try:
            ask = json.loads(self._body or b"null")
        except ValueError:
            return None
        return ask if isinstance(ask, dict) else None

    def _title(self, title_id) -> dict | None:
        """The title asked about, by its id as sent (a merged title's copy's, or one a rebuild moved, too: _index); None
        when no title has it, or what was sent is no id, with the 404 sent."""
        item = self._index().get(title_id) if isinstance(title_id, str) else None
        if item is None:
            self._json({"ok": False, "error": "No such title."}, 404)
        return item

    # ---- editing tags from the library page ----
    EDITS = threading.Lock()  # one change at a time

    def _role(self) -> str:
        """What the logged-in user may do (access.ROLES), read afresh at each request (Caller), the same wherever they
        are."""
        return self._who().role

    BORROWER_PAGE = OWN_FILES

    def _borrower_may(self, path: str) -> bool:
        """What a borrower may fetch of what's lent (LENT): the library page with its data and help, and the covers. No
        other file of the library and no feed, however the address is written. (The questions the page asks, its
        reader's pages among them, pictures, never a file, the reader's program and the licences the help links to are
        anyone's who is logged in: Handler.GET.)"""
        root = self.root.resolve()
        target = library_path(root, path)
        if target is None:
            return False
        parts = [p.lower() for p in target.relative_to(root).parts]
        return "/".join(parts) in self.BORROWER_PAGE or (len(parts) == 3 and parts[:2] == ["catalog", "covers"])

    def _editor(self) -> str | None:
        """Who may change things, as the edits name them (curation.py): a librarian or the owner, by name; readers
        don't (Caller.editor)."""
        return self._who().editor

    def _for_owner(self, chosen: list) -> bool:
        """Changing several titles at once is the owner's: refuses it for anyone else (sends the 403)."""
        if len(chosen) > 1 and self._role() != "owner":
            self._json({"ok": False, "error": "Changing several titles at once is for the library's owner."}, 403)
            return False
        return True

    def _index(self) -> library.TitleIndex:
        """Every id, a merged title's copies' too, to its title; and an id a rebuild took away while the server ran, to
        the title that took it over, for a page left open across the rebuild: what its reader saves is kept, and its
        pages come, though the page still has the ids it was given (library.TitleIndex). Kept in the catalog, made again
        only when its titles are (Catalog.index)."""
        return self.catalog.index()

    def _read_target(self, query: dict) -> Path | None:
        """The comic file of the title asked for (?id=), if it has one the reader can open and it's in the library;
        one inside a ZIP package comes unpacked from the cache. With &book=1, its e-book: the pictures in the page strip."""
        item = self._index().get((query.get("id") or [""])[0])
        if not item:
            return None
        found = reader.readable_book(item) if query.get("book") else reader.readable(item, books=self._role() == "borrower")  # a borrower: e-books as pages
        if not found:
            return None
        root, found = self.root.resolve(), unquote(found)
        whole = library_path(root, found)  # a file first, as _file has it: one in a folder named like a.zip is no package's
        packed = None if whole is not None and whole.is_file() else reader.PACKED.fullmatch(found)
        target = library_path(root, packed[1]) if packed else whole
        if target is None or not self._servable(root, target, self.catalog.lists):
            return None
        return reader.inner(target, packed[2]) if packed else target

    def _api_pages(self) -> None:
        self._api_read(count=True)

    def _api_page(self) -> None:
        self._api_read(count=False)

    def _api_read(self, count: bool) -> None:
        """/api/pages?id= (count): how many pages a title has; /api/page?id=&n=&w=: one page, at most w wide. Anyone who
        may see the library may read in it."""
        query = self._query
        try:
            target = self._read_target(query)  # a file inside a ZIP: unpacked now, the first time
            if target is None:
                return self._json({"ok": False, "error": "Nothing here the reader can open."}, 404)
            if count:
                # v, in every page's address, which browsers keep a week: the file's time, and how its pages are drawn
                stamp = f"{int(target.stat().st_mtime)}-{reader.DRAWN}"
                return self._json({"ok": True, "pages": reader.count(target), "v": f"{stamp}-{reader.BOOK_LAYOUT}" if query.get("book") else stamp,
                                   "toc": reader.outline(target), **({"sections": reader.sections(target)} if query.get("book") else {})})
            n, width = int((query.get("n") or ["0"])[0]), int((query.get("w") or ["1200"])[0])
            data, ctype = reader.page(target, n, width)
        except (IndexError, ValueError):
            return self._json({"ok": False, "error": "No such page."}, 404)
        except reader.Unreadable as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        except OSError as e:
            return self._json({"ok": False, "error": f"The file couldn't be read ({type(e).__name__})."}, 500)
        self._send(200, ctype, data, headers={"Cache-Control": "private, max-age=604800, immutable"})  # v= changes with the file

    READER_ID = re.compile(r"[a-z0-9]{6,24}")

    def _api_ahead(self) -> None:
        """/api/ahead?id=&n=&w=&k=&d=&t=: the k pages after page n, going d (1 on, -1 back), at most w wide, drawn ahead
        for reader t, a number the page made up for the book it opened (reader.draw_ahead). Whoever may read the pages may
        ask; nothing is sent back but how many will be drawn."""
        query = self._query
        word = lambda name, default: (query.get(name) or [default])[0]  # noqa: E731
        try:
            n, width, count, way = int(word("n", "0")), int(word("w", "1200")), int(word("k", "0")), int(word("d", "1"))
            if n < 0 or width < 1 or count < 0 or way not in (1, -1) or not self.READER_ID.fullmatch(word("t", "")):
                raise ValueError
        except ValueError:
            return self._json({"ok": False, "error": "Not a way of reading ahead."}, 400)
        try:
            target = self._read_target(query)
            if target is None:
                return self._json({"ok": False, "error": "Nothing here the reader can open."}, 404)
            drawn = reader.draw_ahead(target, n, width, count, way, word("t", ""))
        except reader.Unreadable as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        except OSError as e:
            return self._json({"ok": False, "error": f"The file couldn't be read ({type(e).__name__})."}, 500)
        self._json({"ok": True, "pages": drawn})

    # ---- what's kept about a person: people.py ----
    def _personal(self) -> bool:
        """A write of a person's own, by the one logged in (every request is): with their key of this start, from this
        site's own pages, as JSON, within the changes a minute one person may send (people.Pace). A role check is for
        the handler; every role has a name, and so places. Refuses, and returns False, if not. The key (X-Personal-Key:
        Caller.personal_key) is each person's own: a page left open for one, with another logged in since in that
        browser, sends with the first one's key and the other's login, and is refused (403) rather than keeping the
        first one's changes as the other's (the page then asks /api/session, sees another name, and starts afresh)."""
        if not self._page_key_ok(self._who().personal_key, "X-Personal-Key"):
            self._json({"ok": False, "error": "not allowed"}, 403)
        elif self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._json({"ok": False, "error": "JSON, please."}, 415)
        elif wait := self.people.pace.wait(self._user):  # past their changes a minute (people.Pace): the page sends it later
            self._paced = True
            said = json.dumps({"ok": False, "error": f"Too many changes in a minute: try again in {wait} seconds."})
            self._send(429, "application/json", said.encode("utf-8"), cache=False, headers={"Retry-After": str(wait)})
        else:
            return True
        return False

    def _place_in(self, ask: dict) -> tuple[dict, dict] | None:
        """Where in a title a place or a bookmark says it is: {id, at: a page number or a book's location, pct: 0 to 1,
        file?: one of its files}, as (the title, {at, pct, file: "" for none of its own}); None with the refusal sent,
        400 for what isn't a place and 404 for a title that isn't there. One check for both, so that each holds the
        other's bounds."""
        title_id, at, pct = ask.get("id"), ask.get("at"), ask.get("pct")
        if not isinstance(title_id, str) or len(title_id) > 200:
            return self._json({"ok": False, "error": "Which title?"}, 400)
        if not (_whole_page(at) or (isinstance(at, str) and 0 < len(at) <= MAX_PLACE)):
            return self._json({"ok": False, "error": "Where in it?"}, 400)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 1:  # NaN is neither
            return self._json({"ok": False, "error": "How far in it?"}, 400)
        item = self._title(title_id)
        if item is None:
            return None
        own = {f[0] for f in item.get("f", [])} | {f[0] for f in item.get("z", [])}
        file = ask["file"] if isinstance(ask.get("file"), str) and ask["file"] in own else ""
        return item, {"at": at, "pct": pct, "file": file}

    @records()
    def _api_place(self) -> None:
        """A place reached: {id, at: a page number or a book's location, pct: 0 to 1, file?, of?: pages, note?: a chapter,
        age?: seconds since}. What the title is and the time come from the server, and the device from what it says it is."""
        ask = self._ask()
        if ask is None:
            return self._json({"ok": False, "error": "A place, please."}, 400)
        found = self._place_in(ask)
        if found is None:
            return
        item, spot = found
        of, note, age = ask.get("of"), ask.get("note"), ask.get("age")
        of = of if isinstance(of, int) and not isinstance(of, bool) and 0 < of <= MAX_PAGES else 0
        note = note[:300] if isinstance(note, str) else ""
        kept = self.people.of(self._user).place(curation.snapshot(item), spot["at"], float(spot["pct"]), spot["file"],
                                                bell.device(self.headers.get("User-Agent") or ""), of, note, _age(age))
        return self._json({"ok": True, "when": kept["when"], "time": kept["time"]})

    @records()
    def _api_mark(self) -> None:
        """{id, mark: finished, unfinished or over (start over), age?}"""
        ask = self._ask()
        if ask is None or ask.get("mark") not in ("finished", "unfinished", "over"):
            return self._json({"ok": False, "error": "Which mark?"}, 400)
        item = self._title(ask.get("id"))
        if item is None:
            return
        age = _age(ask.get("age"))
        person, title = self.people.of(self._user), curation.snapshot(item)
        if ask["mark"] == "over":
            kept = person.start_over(title, bell.device(self.headers.get("User-Agent") or ""), age)
        else:
            kept = person.mark(title, ask["mark"] == "finished", age)
        return self._json({"ok": True, "time": kept["time"]})

    @records()
    def _api_list(self) -> None:
        """A small change to a list of yours: {list: "reading" or the id of one of your own, made in the browser, op: add, remove or
        move, id, before?}. Or to one of your own itself: {list, op: name (makes it, or renames it), name}, {list, op: delete or restore},
        {list, op: apps (in the reading apps' feeds) or feature (the library's Featured, an owner's), on: true or false}."""
        ask = self._ask()
        if ask is None or ask.get("op") not in ("add", "remove", "move", "name", "delete", "restore", "apps", "feature"):
            return self._json({"ok": False, "error": "What change?"}, 400)
        list_id = ask.get("list")
        if not isinstance(list_id, str) or not LIST_ID.fullmatch(list_id):
            return self._json({"ok": False, "error": "Which list?"}, 400)
        person = self.people.of(self._user)
        if ask["op"] in ("apps", "feature"):  # in the reading apps' feeds, or the library's Featured
            if list_id == people.READING_LIST:
                return self._json({"ok": False, "error": "The reading list stays yours alone."}, 400)
            if not isinstance(ask.get("on"), bool):
                return self._json({"ok": False, "error": "On or off?"}, 400)
            if ask["op"] == "feature" and self._role() != "owner":  # 409, refused for good: a 403 is the key's, and waits
                return self._json({"ok": False, "error": "Only an owner picks the library's Featured list."}, 409)
            changed = person.show_list(list_id, ask["on"]) if ask["op"] == "apps" else person.feature_list(list_id, ask["on"])
            return self._json({"ok": True, "changed": changed})
        if ask["op"] in ("name", "delete", "restore"):
            if list_id == people.READING_LIST:
                return self._json({"ok": False, "error": "The reading list is always there, and keeps its name."}, 400)
            try:
                changed = (person.name_list(list_id, shelves.line_of(ask.get("name"), "A list's name", people.LIST_NAME_MAX)) if ask["op"] == "name"
                           else person.delete_list(list_id) if ask["op"] == "delete" else person.restore_list(list_id))
            except shelves.NotAShelf as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except people.ListsFull:
                return self._json({"ok": False, "error": f"You can keep {people.MAX_LISTS} lists of your own."}, 409)
            return self._json({"ok": True, "changed": changed})
        title_id, before = ask.get("id"), ask.get("before")
        if not isinstance(title_id, str) or not 0 < len(title_id) <= 200 or not (before is None or (isinstance(before, str) and len(before) <= 200)):
            return self._json({"ok": False, "error": "Which title?"}, 400)
        index = self._index()
        item = index.get(title_id)
        if item is None and ask["op"] == "add":
            return self._json({"ok": False, "error": "No such title."}, 404)
        if before in index:  # one a rebuild moved: as its title is now, never an id no title has
            before = index.current(before)
        try:  # a title gone from the library can still be moved and taken off
            changed = person.change_list(list_id, ask["op"], curation.snapshot(item) if item else {"id": title_id}, before,
                                         curation.ids_of(item) if item else [title_id], self.catalog.titles())
        except people.ListFull:
            return self._json({"ok": False, "error": f"A list holds {people.MAX_ON_A_LIST} titles."}, 409)
        return self._json({"ok": True, "changed": changed})

    @records()
    def _api_bookmark(self) -> None:
        """A small change to your bookmarks: {op: add, change or remove, key: its own, made in the browser, why: a reason of
        people.BOOKMARK_REASONS}. An add also says where, as a place does: {id, at, pct, file?, of?, note?: a chapter, age?}, and what
        the page wants back: {page?, part?, inside?: where the page strip has it, text?: the words it's known by, word?: the words marked, if not a whole page}. What the title
        is and the time come from the server."""
        ask = self._ask()
        if ask is None or ask.get("op") not in ("add", "change", "remove"):
            return self._json({"ok": False, "error": "What change?"}, 400)
        op, key, why = ask["op"], ask.get("key"), ask.get("why")
        if not isinstance(key, str) or not BOOKMARK_KEY.fullmatch(key):
            return self._json({"ok": False, "error": "Which bookmark?"}, 400)
        if op != "remove" and why not in people.BOOKMARK_REASONS:
            return self._json({"ok": False, "error": "For what reason?"}, 400)
        if op == "add":
            found = self._place_in(ask)
            if found is None:
                return
            item, spot = found
            age, inside = _age(ask.get("age")), ask.get("inside")
            spot["pct"] = round(float(spot["pct"]), 5)
            spot.update({name: ask[name] for name in ("of", "page", "part") if _whole_page(ask.get(name))})
            if isinstance(inside, (int, float)) and not isinstance(inside, bool) and 0 <= inside <= 1:
                spot["inside"] = round(float(inside), 5)
            spot.update({name: ask[name][:most] for name, most in (("note", 300), ("text", MAX_WORDS), ("word", MAX_MARKED)) if isinstance(ask.get(name), str) and ask[name]})
        person = self.people.of(self._user)
        try:
            changed = (person.add_bookmark(curation.snapshot(item), key, why, spot, age) if op == "add"
                       else person.change_bookmark(key, why) if op == "change" else person.remove_bookmark(key))
        except people.BookmarksFull:
            return self._json({"ok": False, "error": f"You can keep {people.MAX_BOOKMARKS} bookmarks."}, 409)
        return self._json({"ok": True, "changed": changed})

    @records()
    def _api_my_shelf(self) -> None:
        """A change to your own shelves: {op: "set", shelf}, {op: "delete", id}, or {op: "open", id: a shelf, or "" for none}."""
        ask = self._ask()
        if ask is None:
            return self._json({"ok": False, "error": "A shelf, please."}, 400)
        try:
            if ask.get("op") not in ("set", "delete", "open"):
                raise shelves.NotAShelf("What change?")
            shelf = shelves.clean(ask.get("shelf")) if ask["op"] == "set" else None
            shelf_id = ask.get("id", "")
            if ask["op"] != "set" and (not isinstance(shelf_id, str) or not (shelves.ID.fullmatch(shelf_id) or (ask["op"] == "open" and not shelf_id))):
                raise shelves.NotAShelf("Which shelf?")
        except ValueError as e:  # NotAShelf, and whatever else a shelf that isn't one raises
            return self._json({"ok": False, "error": str(e) if isinstance(e, shelves.NotAShelf) else "A shelf, please."}, 400)
        try:
            changed = self.people.of(self._user).change_shelf(ask["op"], shelf, shelf_id)
        except shelves.Full:
            return self._json({"ok": False, "error": f"You can keep {shelves.MAX_SHELVES} shelves."}, 409)
        return self._json({"ok": True, "changed": changed})

    @records()
    def _api_shared_shelf(self) -> None:
        """A change to the shelves for everyone: {op: "set", shelf}, {op: "delete", id}, and the owner's {op: "open", id: a shelf
        for everyone, or "" for none}. A librarian changes their own, and the owner any. Answers with them all."""
        shared, who = shelves.Shared(self.root), self._who()
        owner = who.role == "owner"
        ask = self._ask()
        if ask is None:
            return self._json({"ok": False, "error": "A shelf, please."}, 400)
        try:
            if ask.get("op") not in ("set", "delete", "open"):
                raise shelves.NotAShelf("What change?")
            shelf = shelves.clean(ask.get("shelf")) if ask["op"] == "set" else None
            shelf_id = shelf["id"] if shelf else ask.get("id", "")
            if not isinstance(shelf_id, str) or not (shelves.ID.fullmatch(shelf_id) or (ask["op"] == "open" and not shelf_id)):
                raise shelves.NotAShelf("Which shelf?")
        except ValueError as e:
            return self._json({"ok": False, "error": str(e) if isinstance(e, shelves.NotAShelf) else "A shelf, please."}, 400)
        try:
            found, _ = shared.read()
            if ask["op"] == "open":
                if not owner:
                    return self._json({"ok": False, "error": "Which shelf the library opens with is for its owner."}, 403)
                if shelf_id and shelf_id not in found:
                    return self._json({"ok": False, "error": "No such shelf."}, 404)
                shared.opens_with(shelf_id)
            else:
                if not owner and shelf_id in found and found[shelf_id].get("by") != who.user:
                    return self._json({"ok": False, "error": "That shelf is someone else's: the library's owner can change it."}, 403)
                if ask["op"] == "set":
                    shared.set(shelf, who.user)
                else:
                    shared.delete(shelf_id)
            found, opens = shared.read()
        except shelves.Full:
            return self._json({"ok": False, "error": f"The library can keep {shelves.MAX_SHELVES} shelves for everyone."}, 409)
        return self._json({"ok": True, "shared": list(found.values()), "standard": opens})

    @records("They couldn't be read")
    def _api_shelves(self) -> None:
        """The shelves for everyone, and for someone logged in, their own. Who may see the library may see these; who
        made each one (by), only a reader or more: a borrower, a guest's account, learns no one's account name here.
        With them, the limits a shelf is checked by (shelves.limits), which the page's Save checks before anything goes."""
        found, opens = shelves.Shared(self.root).read()
        mine, own = self.people.of(self._user).shelves()
        shared = list(found.values())
        if self._role() == "borrower":
            shared = [{k: v for k, v in s.items() if k != "by"} for s in shared]
        return self._json({"ok": True, "shared": shared, "standard": opens, "mine": mine, "open": own, "limits": shelves.limits()})

    @records("It couldn't be read")
    def _api_my_bookmarks(self) -> None:
        found = self.people.of(self._user).bookmarks(self.catalog.titles())
        return self._json({"ok": True, "bookmarks": found})

    @staticmethod
    def _reading_ahead() -> dict:
        """How far the browser reads ahead, from the settings: [reader]."""
        said = settings.get("reader")
        return {"pages": said["pages_ahead"], "fast": max(said["pages_ahead"], said["pages_ahead_fast"]),
                "sharp": said["sharp_ahead"], "tiles": said["tiles_ahead"], "drawn": said["draw_ahead"]}

    def _asking(self) -> opds.Asking:
        """Who asks for a feed (opds.Asking): the one logged in, whose feed it is, and what of theirs a page may show,
        each read from the records only when a page shows it: the lists for everyone (_feed_lists), theirs (_my_feed,
        which leaves out their lists that everyone's feed has), the shelves (_feed_shelves, as the page has them for
        them) and Best rated and Most read (_feed_ranks)."""
        asking = opds.Asking(self._who().user, self._feed_lists, lambda: self._my_feed(asking.lists),
                             lambda: self._feed_shelves(asking.mine["context"], asking.mine["shelves"]), self._feed_ranks)
        return asking

    def _feed_lists(self) -> list[dict]:
        """The owners' lists ticked for the reading apps, the Featured one first, each by the titles' ids now: what the
        feeds show everyone. A database that can't be read leaves them out, not the feed."""
        try:
            ticked = people.in_the_apps()
        except (*RECORDS_FAILED, db.Newer):
            return []
        titles, out = self.catalog.titles(), []
        for chosen in ticked:
            if self._who().role_of(chosen["by"]) != "owner":  # a reader's own: in their own feed alone (_my_feed)
                continue
            ids, seen = [], set()
            for e in chosen["entries"]:
                now = e["id"] if e["id"] in titles else titles.find(e)
                if now and now not in seen:
                    seen.add(now)
                    ids.append(now)
            out.append({"key": f"{chosen['person']}-{chosen['id']}", "name": chosen["name"], "featured": chosen["featured"], "ids": ids,
                        "by": chosen["by"], "id": chosen["id"]})
        return out

    def _my_feed(self, shared: list[dict]) -> dict:
        """What's yours in your own feed: the titles you're in the middle of, the latest first, as the page's Continue
        reading has them; your reading list; and your own lists ticked for the apps that everyone's feed doesn't have
        already; and for your shelves in the apps (_feed_shelves), how the page has your reading and lists (context) and
        those shelves. What can't be read leaves those empty, not the feed."""
        mine = {"continue": [], "reading": [], "lists": [], "context": None, "shelves": []}
        try:
            person, titles = self.people.of(self._user), self.catalog.titles()
            places, lists, names = person.latest(titles), person.lists(titles), person.list_names()
            own_shelves = [s for s in person.shelves()[0] if s.get("apps")]
        except (*RECORDS_FAILED, db.Newer, people.Newer):
            return mine
        going = sorted((p for p in places.values() if not p.get("done") and not p.get("gone") and (p.get("pct") or 0) > 0),
                       key=lambda p: p.get("time", 0), reverse=True)
        everyones = {(chosen["by"], chosen["id"]) for chosen in shared}
        alive = lambda rows: [e["id"] for e in rows if not e.get("gone")]  # noqa: E731
        mine.update({"continue": [p["id"] for p in going], "reading": alive(lists.get(people.READING_LIST, [])),
                     "lists": [{"id": n["id"], "name": n["name"], "ids": alive(lists.get(n["id"], []))} for n in names
                               if n.get("apps") and (self._user, n["id"]) not in everyones]})
        lists_of: dict[str, list[str]] = {}
        for n in names:
            for title_id in alive(lists.get(n["id"], [])):
                lists_of.setdefault(title_id, []).append(n["id"])
        mine["context"] = {"reading": {p["id"]: "finished" if p.get("done") else "started" if (p.get("pct") or 0) > 0 else "not" for p in places.values()},
                           "on_list": set(mine["reading"]), "lists_of": lists_of}  # Reading and List, as the page has them for you
        mine["shelves"] = own_shelves
        return mine

    def _feed_shelves(self, context: dict | None = None, own: list[dict] | tuple = ()) -> list[dict]:
        """The shelves ticked for the reading apps: those for everyone, and with someone's, theirs, each with its rule as the page
        has it for them (shelves.rule). What can't be read leaves them out, not the feed."""
        try:
            shared = [s for s in shelves.Shared(self.root).read()[0].values() if s.get("apps")]
        except (*RECORDS_FAILED, db.Newer):
            shared = []
        seen, out = set(), []
        for s in [*shared, *own]:
            if s["id"] not in seen:
                seen.add(s["id"])
                out.append({"id": s["id"], "name": s["name"], "rule": shelves.rule(s, context, self.ereader.suits)})
        return out

    @records("It couldn't be read")
    def _api_my_lists(self) -> None:
        person = self.people.of(self._user)
        lists, names = person.lists(self.catalog.titles()), person.list_names()
        return self._json({"ok": True, "lists": lists, "names": names})

    def _new_seen_so_far(self) -> str:
        """What /api/session says of yours, as far as it can be read: what can't be read just now is left at its start,
        not the whole answer, which would leave the page without its key."""
        try:
            return self.people.of(self._user).new_seen()
        except RECORDS_FAILED as e:
            self._not_read(e)
            return ""

    @records()
    def _api_new_seen(self) -> None:
        """{upto: the date of the newest title shown as new}"""
        upto = (self._ask() or {}).get("upto")
        if not isinstance(upto, str) or not NEW_DATE.fullmatch(upto):
            return self._json({"ok": False, "error": "Up to when?"}, 400)
        self.people.of(self._user).saw_new(upto)
        return self._json({"ok": True})

    def _guides_so_far(self) -> list[str]:
        try:
            return self.people.of(self._user).guides()
        except RECORDS_FAILED as e:
            self._not_read(e)
            return []

    @records()
    def _api_guide_seen(self) -> None:
        """{kind: which of the reader's guides they've had}"""
        kind = (self._ask() or {}).get("kind")
        if kind not in people.GUIDES:
            return self._json({"ok": False, "error": "Which guide?"}, 400)
        self.people.of(self._user).saw_guide(kind)
        return self._json({"ok": True})

    # ---- stars: reviews.py, kept with what's kept about each person (people.py) ----
    @staticmethod
    def _stars_who() -> str:
        """[reviews] who: the lowest role that may give stars, or off."""
        return settings.get("reviews")["who"]

    def _stars_may(self) -> bool:
        return bool(self._user) and reviews.may_rate(self._stars_who(), self._role())

    @records()
    def _api_stars(self) -> None:
        """Your stars for a title: {id, stars: 1 to 5, or 0 to take yours back}. Stars that are off, or not for your role,
        are refused with 409, as they won't be allowed by asking again: the page's outbox lets a 409 go, and keeps a
        403, which is its key's (PERSONAL)."""
        if not self._stars_may():
            return self._json({"ok": False, "error": "Stars are off in this library." if self._stars_who() == "off"
                               else "Giving stars isn't for your account here."}, 409)
        ask = self._ask() or {}
        stars = ask.get("stars")
        if isinstance(stars, bool) or stars not in (0, *reviews.STARS):
            return self._json({"ok": False, "error": "1 to 5 stars, or 0 to take yours back."}, 400)
        item = self._title(ask.get("id"))
        if item is None:
            return
        self.people.of(self._user).rate(curation.snapshot(item), stars, curation.ids_of(item), self.catalog.titles())
        return self._json({"ok": True})

    def _words_wait(self) -> bool:
        """Whether your words wait for an owner ([reviews] moderation)."""
        return reviews.waits(settings.get("reviews")["moderation"], self._role())

    def _everyones(self) -> dict[str, list[dict]]:
        return self.people.everyones_reviews(set(self._who().users), self.catalog.titles())

    def _reviews_waiting(self) -> int:
        try:
            return sum(r.get("state") in (reviews.WAITING, reviews.EDIT_WAITING) for said in self._everyones().values() for r in said if not r.get("gone"))
        except RECORDS_FAILED as e:
            self._not_read(e)
            return 0

    @records()
    def _api_review(self) -> None:
        """Your review of a title: {id, stars, text, spoiler} (text "" takes the words away and keeps the stars), or
        {id, op: "delete"}: stars and words gone. Words wait for an owner where [reviews] moderation says so. 409 for what
        the rules don't allow, and while reviews are off or not for your role."""
        if not self._stars_may():
            return self._json({"ok": False, "error": "Reviews are off in this library." if self._stars_who() == "off"
                               else "Reviews aren't for your account here."}, 409)
        ask = self._ask() or {}
        item = self._title(ask.get("id"))
        if item is None:
            return
        person, ids, titles = self.people.of(self._user), curation.ids_of(item), self.catalog.titles()
        if ask.get("op") == "delete":
            person.delete_review(ids, titles)
            return self._json({"ok": True, "review": None})
        stars = ask.get("stars")
        if isinstance(stars, bool) or stars not in reviews.STARS:
            return self._json({"ok": False, "error": "1 to 5 stars go with the words."}, 400)
        try:
            text = reviews.clean(ask.get("text", ""))
        except reviews.Refused as e:
            return self._json({"ok": False, "error": f"The words: {e}."}, 400)
        wait = self._words_wait()
        after = person.write(curation.snapshot(item), stars, text, bool(ask.get("spoiler")), wait, ids, titles)
        if after is None:  # sent again: as it was
            after = next((r for r in person.reviews(titles) if r["id"] in ids), None)
        elif after.get("state") in (reviews.WAITING, reviews.EDIT_WAITING):
            self.watch.event("review", waiting=self._reviews_waiting())
        return self._json({"ok": True, "review": reviews.own(after) if after else None})

    @records("They couldn't be read")
    def _api_title_reviews(self) -> None:
        """/api/reviews?id=: a title's reviews for whoever may see the library: each person's stars and the words that are
        out, by name; yours whole, waiting or not. Someone without an account isn't counted."""
        if self._stars_who() == "off":
            return self._json({"ok": True, "on": False})
        item = self._title((self._query.get("id") or [""])[0])
        if item is None:
            return
        ids, out = set(curation.ids_of(item)), []
        for name, said in self._everyones().items():
            mine = [r for r in said if r["id"] in ids and not r.get("gone")]
            if mine:
                r = max(mine, key=lambda x: x.get("time", 0))
                out.append({"name": name, **reviews.public(r), **({"own": reviews.own(r)} if name == self._user else {})})
        out.sort(key=lambda r: r.get("when", ""), reverse=True)  # the newest first
        return self._json({"ok": True, "on": True, "reviews": out})

    @records("They couldn't be read")
    def _api_my_reviews(self) -> None:
        """Yours, every one, the newest first, by the title's id now: for Ratings and reviews in the account menu."""
        if self._stars_who() == "off":
            return self._json({"ok": True, "on": False})
        index = self._index()
        said = self.people.of(self._user).reviews(self.catalog.titles())
        out = [{"title": index[r["id"]]["id"], **reviews.own(r)} for r in said if r["id"] in index and not r.get("gone")]
        out.sort(key=lambda r: r.get("when", ""), reverse=True)
        return self._json({"ok": True, "on": True, "reviews": out})

    @records("They couldn't be read")
    def _api_waiting_reviews(self) -> None:
        """The owners' queue: words waiting to be looked at, the oldest first, with the words shown now for a change."""
        index, out = self._index(), []
        for name, said in self._everyones().items():
            for r in said:
                item = index.get(r["id"])
                if r.get("state") in (reviews.WAITING, reviews.EDIT_WAITING) and item and not r.get("gone"):
                    out.append({"name": name, "id": r["id"], "title": item["id"], "t": item.get("t", ""), "state": r["state"], "stars": r["stars"],
                                "text": r.get("text", ""), "spoiler": bool(r.get("spoiler")), "change": r.get("change", ""),
                                "change_spoiler": bool(r.get("change_spoiler")), "rev": r.get("rev", 0), "when": r.get("when", "")})
        out.sort(key=lambda r: r["when"])
        return self._json({"ok": True, "items": out})

    @records(whose="The database")
    def _api_check_review(self) -> None:
        """An owner's word on someone's review: {name, id (as the queue gave it), rev, op: release, decline or remove,
        note?}. Words changed since the owner saw them are refused with 409, and come again in the queue."""
        ask = self._ask()
        if (ask is None or ask.get("op") not in reviews.OWNERS or not isinstance(ask.get("id"), str)
                or not isinstance(ask.get("name"), str) or ask["name"] not in self._who().users):
            return self._json({"ok": False, "error": "Whose review, and what of it?"}, 400)
        try:
            note = reviews.clean(ask.get("note", ""), reviews.NOTE_MAX)
        except reviews.Refused as e:
            return self._json({"ok": False, "error": f"The note: {e}."}, 400)
        after = self.people.of(ask["name"]).check_review(ask["id"], ask["op"], ask.get("rev"), note, self._user)
        return self._json({"ok": True, "state": after.get("state") if after else None, "waiting": self._reviews_waiting()})

    # ---- reports of a title's file: anyone logged in reports, owners and librarians go through them (reports.py) ----
    def _report_states(self) -> dict[str, dict]:
        """Each reported or marked title's state (reports.states): the reports and the library's word on them, each by
        the id its title has now, so that a title whose file moved keeps both."""
        titles = self.catalog.titles()
        return reports.states(self.people.everyones_reports(set(self._who().users), titles), self._decided(titles))

    def _decided(self, titles: matching.Titles | None = None) -> dict[str, dict]:
        """The library's last word on each reported title, by the id its title has now (reports.following)."""
        return reports.following(self.decisions.latest(), titles if titles is not None else self.catalog.titles())

    def _reports_waiting(self) -> int:
        """How many titles have a report no one has decided on yet; 0 when that can't be worked out (_count)."""
        def waiting() -> int:
            index = self._index()
            return sum(s["state"] == reports.WAITING and title_id in index for title_id, s in self._report_states().items())
        return self._count(waiting)

    @staticmethod
    def _count(count: Callable[[], int]) -> int:
        """A number the library page shows, such as the titles waiting, or 0 when it can't be worked out: a record that
        can't be read mustn't fail the whole of /api/session, which would leave every editor's page read-only and
        unable to say why. A database of a newer version says so elsewhere; records that can't be read are said as the
        record handlers say them (_not_read); anything else goes to server.log, once for each kind (first_failure)."""
        try:
            return count()
        except db.Newer:
            return 0
        except RECORDS_FAILED as e:
            Handler._not_read(e)
            return 0
        except Exception as e:  # noqa: BLE001 - a count is never worth the whole answer
            if first_failure(e):
                _stamp(f"A count for the library page failed: {type(e).__name__}: {str(e)[:300]}")
                print(_trace(e), end="", file=sys.stderr, flush=True)
            return 0

    @records()
    def _api_report(self) -> None:
        """Your report of a title's file: {id, reasons: [keys of reports.REASONS], comment?}, in place of any earlier one of
        yours on it; or {id, op: "take back"}."""
        ask = self._ask() or {}
        item = self._title(ask.get("id"))
        if item is None:
            return
        person, ids, titles = self.people.of(self._user), curation.ids_of(item), self.catalog.titles()
        if ask.get("op") == "take back":
            person.unreport(ids, titles)
            return self._json({"ok": True, "report": None})
        try:
            reasons, comment = reports.clean(ask.get("reasons"), ask.get("comment", ""))
        except reports.Refused as e:
            return self._json({"ok": False, "error": f"The report: {e}."}, 400)
        before = self._reports_waiting()
        made = person.report(curation.snapshot(item), reasons, comment, ids, titles)
        if made is None:  # sent again: as it was
            made = next((r for r in person.reports(titles) if r["id"] in ids), None)
        else:
            waiting = self._reports_waiting()
            if waiting > before:  # a title newly waiting: how many, never which or whose
                self.watch.event("report", waiting=waiting)
        return self._json({"ok": True, "report": made})

    @records("It couldn't be read")
    def _api_my_reports(self) -> None:
        """Your reports, {title id: {reasons, comment, when}}: a title's details say you reported it."""
        mine = self.people.of(self._user).reports(self.catalog.titles())
        return self._json({"ok": True, "reports": {r["id"]: {"reasons": r["reasons"], "comment": r.get("comment", ""), "when": r.get("when", "")}
                                                   for r in mine if not r.get("gone")}})

    @records("They couldn't be read")
    def _api_reports(self) -> None:
        """For owners and librarians: the reported titles, waiting ones first, the oldest report first, and the ones marked
        for a better copy; each with its reports (who, when, why) and the last decision."""
        index, out = self._index(), []
        for title_id, s in self._report_states().items():
            item = index.get(title_id)
            if not item:
                continue
            out.append({"id": item["id"], "t": item.get("t", ""), "state": s["state"], "newest": s["newest"],
                        "reports": [{"by": r["by"], "reasons": r["reasons"], "comment": r.get("comment", ""), "when": r.get("when", ""), "time": r["time"]} for r in s["reports"]],
                        "decided": {k: s["decided"][k] for k in ("decision", "by", "when")} if s["decided"] else None})
        out.sort(key=lambda e: (e["state"] != reports.WAITING, min((r["time"] for r in e["reports"]), default=0)))
        return self._json({"ok": True, "items": out, "reasons": reports.REASONS, "decisions": reports.DECISIONS})

    @records()
    def _api_decide_report(self) -> None:
        """An owner's or librarian's word on a reported title: {id, decision: "better" or "fine", seen: the newest report's
        time as they saw it}. A report newer than that is refused with 409, and the title waits on with it. What the
        decision covers is the server's own newest report time on the title, never the number sent: seen only tells
        whether a report came meanwhile, and a seen that is no time the server could have given is refused."""
        ask = self._ask()
        seen = ask.get("seen") if ask is not None else None
        if (ask is None or not isinstance(ask.get("decision"), str) or ask["decision"] not in reports.DECISIONS
                or isinstance(seen, bool) or not isinstance(seen, (int, float))):
            return self._json({"ok": False, "error": "Which title, and what of it?"}, 400)
        item = self._title(ask.get("id"))
        if item is None:
            return
        state = self._report_states().get(item["id"])
        newest = state["newest"] if state else None
        # a report's time as this server gave it: not before 1970, nor later than now or the newest report (a clock put
        # back since); NaN, an infinity or a number past every time is none of these
        if not 0 <= seen <= max(time.time(), newest or 0) + 1:
            return self._json({"ok": False, "error": "Which title, and what of it?"}, 400)
        if state and newest > seen + 0.001:
            return self._json({"ok": False, "error": "Not now: a new report has come meanwhile. Have a look at it first."}, 409)
        if state is None and ask["decision"] == "better":
            return self._json({"ok": False, "error": "Not now: there's nothing reported on it."}, 409)
        # nothing waiting nor marked: whatever the last decision covered, it still does
        upto = newest if state else reports.upto_of(self._decided().get(item["id"]))
        self.decisions.decide(curation.snapshot(item), ask["decision"], self._user, upto)
        return self._json({"ok": True, "waiting": self._reports_waiting()})

    @records("They couldn't be read")
    def _api_all_stars(self) -> None:
        """Everyone's stars, {title id: {name: stars}}, for whoever may see the library, while they're on. A title's merged
        copies and changed ids go by its id now; one that has gone is left out, and so is someone without an account."""
        if self._stars_who() == "off":
            return self._json({"ok": True, "on": False})
        out, words = self._everyones_stars()
        return self._json({"ok": True, "on": True, "stars": out, "words": sorted(words)})  # words: titles with reviews others may read

    def _feed_ranks(self) -> dict:
        """Best rated and Most read for the feeds, {best, most: ids in order}: the stars ranked as the page's Best rated does
        (an average pulled towards everyone's by two ratings), while they're on; and the titles that opds.MOST_READ_FROM or
        more people have a place in, the most first. Counts only: no name leaves. What can't be read leaves them empty."""
        index, out = self._index(), {"best": [], "most": []}
        try:
            if self._stars_who() != "off":
                stars = self._everyones_stars()[0]
                every = [n for given in stars.values() for n in given.values()]
                mean = sum(every) / len(every) if every else 3
                rank = {i: ((sum(g.values()) + mean * 2) / (len(g) + 2), len(g)) for i, g in stars.items() if g}
                out["best"] = sorted(rank, key=lambda i: (-rank[i][0], -rank[i][1], index[i]["t"].lower()))
            titles, users, readers = self.catalog.titles(), set(self._who().users), {}
            for r in people.readers():
                if r["name"] not in users:
                    continue
                item = index.get(r["id"]) or index.get(titles.find(r) or "")
                if item is not None:
                    readers.setdefault(item["id"], set()).add(r["person"])
            out["most"] = sorted((i for i, who in readers.items() if len(who) >= opds.MOST_READ_FROM), key=lambda i: (-len(readers[i]), index[i]["t"].lower()))
        except (*RECORDS_FAILED, db.Newer, people.Newer):
            pass
        return out

    def _everyones_stars(self) -> tuple[dict[str, dict[str, int]], set[str]]:
        """Everyone's stars by title id now, {id: {name: stars}}, and the titles with words others may read."""
        titles, index = self.catalog.titles(), self._index()
        found = self.people.everyones_reviews(set(self._who().users), titles)
        out: dict[str, dict[str, int]] = {}
        latest: dict[tuple[str, str], float] = {}
        words: set[str] = set()
        for name, said in found.items():
            for r in said:
                item = index.get(r["id"])
                if item is None or r.get("gone"):
                    continue
                key = (item["id"], name)  # one of each person per title, the latest, if they gave two of its copies stars
                if r.get("time", 0) >= latest.get(key, -1):
                    latest[key] = r.get("time", 0)
                    out.setdefault(item["id"], {})[name] = r["stars"]
                if r.get("state") in reviews.SEEN:
                    words.add(item["id"])
        return out, words

    # ---- themes: themes.py ----
    @staticmethod
    def _looks() -> dict:
        """The themes that may be picked, for the Look sheet, each with all its colours, and the rules for one's own."""
        default, picked, _ = themes.current()
        return {"look": default, **themes.for_page(picked)}

    def _look_so_far(self) -> str:
        try:
            return self.people.of(self._user).look()
        except RECORDS_FAILED as e:
            self._not_read(e)
            return ""

    def _own_so_far(self) -> dict | None:
        """Their own theme, as kept: the colours they picked, and all its colours worked out, as the page shows them.
        One whose theme may not be picked any more, or that no longer passes the checks, is left out."""
        try:
            own = self.people.of(self._user).own_look()
        except RECORDS_FAILED as e:
            self._not_read(e)
            return None
        base = {t.id: t for t in themes.current()[1]}.get((own or {}).get("base"))
        if not base:
            return None
        try:
            made = themes.own_theme(base, {m: own.get(m) or {} for m in themes.MODES})
        except themes.NotATheme:
            return None
        return {"base": base.id, **{m: own.get(m) or {} for m in themes.MODES}, "colours": made.colours}

    @records()
    def _api_look(self) -> None:
        """{look: a theme's id, "" for the library's own, or "yours"; own: your own theme's colours, {"base": a theme's
        id, "light": {key: colour}, "dark": {...}}, or null to forget them}: yours on every device. A theme that may not
        be picked (any more), or colours that don't pass themes.own_theme, are refused with 409, as asking again won't
        change that."""
        ask = self._ask() or {}
        look = ask.get("look")
        picked = {t.id: t for t in themes.current()[1]}
        own = ask.get("own", ...)
        if own is not ... and own is not None:
            base = picked.get(own.get("base")) if isinstance(own, dict) and isinstance(own.get("base"), str) else None
            if not base or set(own) - {"base", *themes.MODES}:
                return self._json({"ok": False, "error": "Your own theme is made from a theme that may be picked here."}, 409)
            try:
                themes.own_theme(base, {m: own.get(m) or {} for m in themes.MODES})
            except themes.NotATheme as e:
                return self._json({"ok": False, "error": str(e)}, 409)
            own = {"base": base.id, **{m: dict(own.get(m) or {}) for m in themes.MODES}}
        if not isinstance(look, str) or (look and look != themes.OWN and look not in picked):
            return self._json({"ok": False, "error": "No such theme here."}, 409)
        if look == themes.OWN and (own is None or own is ... and not self._own_so_far()):
            return self._json({"ok": False, "error": "There's no theme of your own to wear."}, 409)
        self.people.of(self._user).pick_look(look, own)
        return self._json({"ok": True})

    @records("It couldn't be read")
    def _api_my_places(self) -> None:
        wanted = self._query.get("id")  # just one title, a merged title's copies with it
        found = self.people.of(self._user).latest(self.catalog.titles())
        if wanted:
            item = self._index().get(wanted[0])
            ids = curation.ids_of(item) if item else wanted
            found = {i: place for i, place in found.items() if i in ids}
        return self._json({"ok": True, "places": found})

    def _api_catalog(self) -> None:
        """Has the catalog been built again? The page, back on screen, asks."""
        try:
            self.catalog.get()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return self._json({"generated": self.catalog.generated})

    def _api_session(self) -> None:
        """What the library page offers whoever is logged in, and their keys; other sites can't read the answer."""
        who = self._who()
        editor, role = who.editor, who.role
        owner = role == "owner"
        waiting = self._count(lambda: len(curation.pending(self.root, self._index())["items"])) if owner else 0
        borrower = role == "borrower"  # reads in the page, and gets no files: no links to them, and no feeds
        ereader = None if borrower else [it["id"] for it in self.catalog.get() if self.ereader.suits(it)]  # the feed filter
        return self._json({"edit": bool(editor), "by": who.user, "role": role, "files": not borrower, "name": self.library_name,
                           "can": {"one": bool(editor), "many": owner, "manage": owner},
                           "key": who.editors_key if editor else None, "suggestions": waiting,  # each editor's own
                           "me": {"key": who.personal_key, "newSeen": self._new_seen_so_far(), "guides": self._guides_so_far(), "look": self._look_so_far(),
                                  "own": self._own_so_far()} if who.user else None,  # none: this browser only
                           **self._looks(),
                           "book_fonts": settings.get("reader")["book_fonts"],  # the reader takes what the policy can't reach
                           "ahead": self._reading_ahead(),
                           "widths": reader.WIDTHS,  # the pages' widths: the page asks for one of them (reader.WIDTHS)
                           # what a report and a review are held to here, which the page offers as they are: it keeps no copy
                           "report_reasons": reports.REASONS,
                           "limits": {"review": reviews.TEXT_MAX, "note": reviews.NOTE_MAX, "report": reports.MOST_WORDS},
                           "word_meanings": settings.get("reader")["word_meanings"],  # off: the word box has no meanings
                           "stars": {"see": self._stars_who() != "off", "give": self._stars_may(), "waits": self._words_wait()},  # give: needs a name
                           "reviewsWaiting": self._count(self._reviews_waiting) if owner and self._stars_who() != "off" else 0,
                           "reportsWaiting": self._reports_waiting() if editor else 0,  # reported titles, for owners and librarians
                           **({"toDecide": self._to_decide()} if owner else {}),  # how many questions: the owner's alone
                           "source": self.source_url,  # the account menu's Source link
                           "ereader": ereader, "session": bool(who.session)})  # Log out, or Log in

    @records("They couldn't be read")
    def _api_edits(self) -> None:
        """Recent changes: the owner sees everyone's, a librarian their own; ?n= of them, the newest first."""
        n, who = min(library.whole(self._query.get("n"), 30), 200), self._who()
        owner = who.role == "owner"
        edits = curation.history(self.root, 2000 if not owner else n)
        self._json({"edits": edits[:n] if owner else [e for e in edits if e.get("by") == who.user][:n]})

    @records("They couldn't be read")
    def _api_suggestions(self) -> None:
        """The tags suggested for titles (curation.pending), to say yes or no to: the owner's."""
        self._json(curation.pending(self.root, self._index()))

    @records("They couldn't be read")
    def _api_declined(self) -> None:
        """The suggestions said no to, to change one's mind: the owner's."""
        self._json({"items": curation.declined(self.root, self._index())})

    @records()
    def _api_suggest(self) -> None:
        """Find suggestions again (what hylly tag suggest does): how many titles have some. The owner's."""
        with self.EDITS:
            found = curation.find_suggestions(self.root, self.catalog.get())
        return self._json({"ok": True, "titles": len(found), "tags": sum(len(s["tags"]) for s in found)})

    BY_ANSWER = ("copies", "refused")  # corrections made as answers, without a question: a card two books, a match refused

    FIX_FIELDS = ("kind, ereader, title, authors, series, number, year or publisher; the owner's: format, file, answers, "
                  "catalog, left-out, copies or refused")  # what /api/fix takes, as its 400 says it

    @records()
    def _api_fix(self) -> None:
        """{"field": "kind" | "ereader" | "title" | "authors" | ..., "value": "tech" | "always" | "A Title" | [...] | "auto",
        "ids": [...], "undo": true for the page's Undo}: the titles afterwards, and what each changed one had before (for
        undo). The owner's alone: the format and the file to keep; a title's answers, the downloads' (answers) or the
        catalog's (catalog), auto as hylly set takes them, or with undo the answers before (a dict, as Recent changes has
        it; "update": true, a few keys on each id as given, the undo of an edit of a few answers: curation.undo_update);
        and "copies": "apart" or "auto", "refused": a lookup source or "auto" (curation.set_copies, set_refused), for a card
        whose copies are two books, or whose lookup's match is another book, with "update" in the answer, the undo of just
        that (keys, was). ids: the titles' ids, as text; anything else is refused (400), naming every field it takes."""
        editor = self._user
        try:
            body = self._ask() or {}
            field, value, ids, undo = body["field"], body["value"], body["ids"], body.get("undo", False) is True
            update = body.get("update", False) is True
            # checked by raising, not by assert, which python -O leaves out
            if not ((field in curation.SINGLE or field in curation.TEXT or field in curation.ANSWERED or field in self.BY_ANSWER)
                    and isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                raise ValueError("which field, and which titles")
            if not (isinstance(value, str) or (field == "authors" and isinstance(value, list) and all(isinstance(v, str) for v in value))
                    or (field in curation.ENTRIES and isinstance(value, dict) and undo)):  # a dict: only an undo's
                raise ValueError("what value")
            if update and not (undo and field in curation.ENTRIES and isinstance(value, dict)):
                raise ValueError("an update is an undo's")
            if field in curation.ENTRIES and isinstance(value, str) and value != "auto":
                raise ValueError("answers are auto")
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._json({"ok": False, "error": f"A correction needs field ({self.FIX_FIELDS}), value and ids, each id "
                                                     "as text. A title's answers are auto, or with undo what they were."}, 400)
        if field in ("format", *curation.ANSWERED, *self.BY_ANSWER) and self._role() != "owner":  # it steers what is
            return self._json({"ok": False, "error": "The format and the file to keep, and the answers to the questions "  # downloaded
                                                     "(copies, a lookup's match), are for the library's owner."}, 403)
        index = self._index()
        if update:  # an undo of a few answers: on the very ids it names, a copy's too, one a rebuild moved as its title's now
            chosen = [{"id": i, "t": index[i].get("t", ""), "f": index[i].get("f") or [[""]]}
                      for i in dict.fromkeys(index.current(i) for i in ids if i in index)]
        else:
            chosen = list({it["id"]: it for it in (index[i] for i in ids if i in index)}.values())  # a merged title once
        if not self._for_owner(chosen):
            return
        back = {}
        try:
            with self.EDITS:
                if field in self.BY_ANSWER:
                    change = curation.set_copies if field == "copies" else curation.set_refused
                    (changed, back), was = change(self.root, chosen, value, by=editor), {}
                elif update:
                    changed, was = curation.change_answers(self.root, value, chosen, by=editor, field=field, undo=True)
                else:
                    changed, was = curation.change_single(self.root, field, value, chosen, by=editor, undo=undo)
        except rules.RulesError:
            raise  # names the owner's own files: the fixed answer, and the message in server.log (_failed)
        except SystemExit as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        index = self._index()  # the titles with the new edit laid over them
        now = [index.get(it["id"], it) for it in chosen]
        titles = self.catalog.for_page(now)
        named = curation.source_name(curation.lookup_source(value) or value) if field == "refused" and value != "auto" else ""
        return self._json({"ok": True, "field": field, "value": value, "changed": [it["id"] for it in changed], "was": was,
                           **({"update": back} if back else {}),  # the undo of copies or refused: just that (undo_update)
                           **({"named": named} if named else {}),  # a source as people know it: Open Library
                           "items": {it["id"]: {"title": title, "in_ereader": self.ereader.suits(it)} for it, title in zip(now, titles)}})

    @records()
    def _api_tags(self) -> None:
        """{"op": "add" | "remove" | "decline" | "undecline", "tag": "Knitting", "ids": [...]}: the titles' tags afterwards."""
        try:
            body = self._ask() or {}
            op, ids, tag = body["op"], body["ids"], curation.tag_name(str(body["tag"]))
            if not (op in curation.OPS and isinstance(ids, list) and all(isinstance(i, str) for i in ids)):  # not an assert,
                raise ValueError("which change, and which titles")  # which python -O leaves out
        except (ValueError, KeyError, TypeError):
            return self._json({"ok": False, "error": "A change needs op (add, remove, decline or undecline), tag and ids."}, 400)
        except SystemExit as e:  # tag_name's own message
            return self._json({"ok": False, "error": str(e)}, 400)
        index = self._index()
        chosen = list({it["id"]: it for it in (index[i] for i in ids if i in index)}.values())  # a merged title once
        if not self._for_owner(chosen):
            return
        with self.EDITS:
            changed = curation.change(self.root, op, tag, chosen, by=self._user)
        index = self._index()  # the titles with the new edit laid over them: laid anew, not over those chosen
        return self._json({"ok": True, "op": op, "tag": curation.load(self.root).names.get(tag.casefold(), tag),
                           "changed": [it["id"] for it in changed], "tags": {it["id"]: index.get(it["id"], it).get("tg", []) for it in chosen}})

    def _owners_page(self, name: str, **values: str) -> None:
        """One of the owners' own pages, the Server page or the Database page (dashboard.page), with the key their
        questions and buttons carry (SERVER_KEY) and the values they show, its tab named for the library."""
        text = self._named(dashboard.page(name, self.server_key, **values))
        self._send(200, "text/html; charset=utf-8", text.encode("utf-8"), cache=False, headers=page_headers(text))

    def _server_page(self) -> None:
        """The Server page (dashboard.py): an owner's, wherever they log in from. Its buttons need its own key."""
        self._owners_page("server_page.html", **dashboard.which_server(self.info))

    def _server_status(self) -> None:
        """How the server is doing, for the Server page, every few seconds (dashboard.status), its users' roles as this
        request read them."""
        self._json(dashboard.status(self, self._who().roles))

    def _button(self, press: Callable[[], None]) -> None:
        """One of the Server page's buttons pressed: should what it runs stop as a command would (SystemExit), what the
        command would have printed is the answer (500)."""
        try:
            press()
        except SystemExit as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _server_stop(self) -> None:
        """Stop: the answer first, then the server stops."""
        def stop() -> None:
            _stamp("Stopped from the server page.")
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        self._button(stop)

    def _server_restart(self) -> None:
        """Restart, by a hylly server restart of its own (dashboard.restart_later)."""
        def restart() -> None:
            dashboard.restart_later()
            self._json({"ok": True})
        self._button(restart)

    def _server_decide(self) -> None:
        """An answer to one of To decide's questions (_answer_question)."""
        self._button(self._answer_question)

    def _questions(self) -> None:
        """The questions the downloads and the catalog left to the owner (curation.questions), for the Server page, each
        with its number, its options and the rules' guess; and apart, those answered whose answers stand (answered), each
        with its answer, to take back. None where another computer keeps the library's records (the page then shows no
        list: they're that computer's to answer), and one short line where they can't be read just now."""
        try:
            found, answered = self._open_questions(), curation.questions(self.root, answered=True)
        except db.NotHere:
            return self._json({"ok": True, "items": [], "answered": [], "elsewhere": True})
        except (*RECORDS_FAILED, db.Newer):
            return self._json({"ok": False, "error": "The questions can't be read just now.", "items": [], "answered": []})
        return self._json({"ok": True, "items": found, "answered": answered})

    def _open_questions(self) -> list[dict]:
        """The open questions (curation.questions), but a same question one of whose copies' files has left the library
        since the build that asked it (removed into _Removed on this page, say): settled, though the build that drops it
        takes a moment, so neither the list, Compare, a removal nor the library page's count offers it again meanwhile."""
        root = self.root.resolve()

        def gone(q: dict) -> bool:
            if q.get("kind") != "same":
                return False
            for side in q.get("subjects") or []:
                f = side.get("f") if isinstance(side, dict) else ""
                if isinstance(f, str) and f:
                    path = library_path(root, unquote(f))
                    if path is None or not path.exists():
                        return True
            return False

        return [q for q in curation.questions(self.root) if not gone(q)]

    COMPARED = ("same", "which-file")  # the questions whose files the Server page shows side by side (_compare)

    def _compare(self) -> None:
        """GET /server/compare?q=NUMBER, the owner's, as To decide's list is: what the Server page shows to tell one open
        question's files apart, side by side (COMPARED). A same question: each copy as the reader shows it (as pages, else
        as a book); and how many of its copies aren't a card of their own just now (unshown: gone from the catalog, or
        merged with the other by a build since), which aren't shown. A which-file question: each file it offers that its
        title has on the disk (_on_disk), the reader's pages of it where the reader shows that very file; and what the shop
        lists of each it hasn't (listed). Each side: its title's id, which the reader's pages go by (/api/pages,
        /api/page, with &book=1 for an e-book, drawn as the reader lays it out: reads, "" for none), the file's name,
        format and size, its date on the disk (a download or a copy sets it), its title and its cover. Nothing for a
        question of another kind; 404 for no open question of that number, 400 for no number."""
        try:
            number = int((self._query.get("q") or [""])[0][:12])
        except ValueError:
            return self._json({"ok": False, "error": "Which question? ?q=NUMBER, as To decide numbers them."}, 400)
        try:
            found = next((q for q in self._open_questions() if q.get("id") == number), None)
        except db.NotHere:
            found = None
        except (*RECORDS_FAILED, db.Newer):
            return self._json({"ok": False, "error": "The questions can't be read just now."})
        if found is None:
            return self._json({"ok": False, "error": f"No question {number} is open."}, 404)
        if found.get("kind") not in self.COMPARED:
            return self._json({"ok": True, "sides": [], "listed": [], "unshown": 0})
        if found["kind"] == "same":
            titles, asked = self._same_sides(found)
            sides = [side for side in (self._compared(item) for item in titles) if side]
            return self._json({"ok": True, "sides": sides, "listed": [], "unshown": asked - len(sides)})
        item = self._index().get(found.get("catalog_id") or "")
        entries, sides, listed = list((item or {}).get("f") or []), [], []
        for o in (x for x in found.get("options") or [] if isinstance(x, dict) and x.get("file")):
            entry = self._on_disk(o, entries)
            side = self._compared(item, entry) if entry is not None else None
            if side:
                entries.remove(entry)
                sides.append(side)
            else:
                listed.append({k: o.get(k) for k in ("file", "format", "size", "label")})
        self._json({"ok": True, "sides": sides, "listed": listed, "unshown": 0})

    def _same_sides(self, found: dict) -> tuple[list[dict], int]:
        """A same question's copies as their cards are now, in the order of its subjects (left the first, right the
        second), as /server/compare shows them and /server/remove removes them: each card once (two merged by a build
        since are one), one gone from the catalog left out, and only a card with files; and how many copies it asks
        about."""
        ids = [s.get("id") for s in found.get("subjects") or [] if isinstance(s, dict)]
        index = self._index()
        items = [index.get(i) if isinstance(i, str) else None for i in ids]
        titles = [item for n, item in enumerate(items) if item and item.get("f") and not any(x is item for x in items[:n])]
        return titles, len(ids)

    @staticmethod
    def _on_disk(option: dict, entries: list) -> list | None:
        """The entry of a title's files (its record's f) that a file a question offers was saved as: the shop's name made
        safe for the disk (paths.safe_filename), or that with " (2)" and so on, as plan.free_name names a second file of
        the same name; of several, the one of its size. None for one the title hasn't got."""
        safe = paths.safe_filename(str(option["file"])).lower()
        stem, dot, ext = safe.rpartition(".") if "." in safe else (safe, "", "")
        twin = re.compile(re.escape(stem) + r" \(\d+\)" + (re.escape("." + ext) if dot else ""))
        named = [e for e in entries if isinstance(e, list) and e and isinstance(e[0], str)
                 and ((n := PurePosixPath(unquote(e[0])).name.lower()) == safe or twin.fullmatch(n))]
        return next((e for e in named if len(e) > 2 and e[2] == option.get("size")), named[0] if named else None)

    def _compared(self, item: dict | None, entry: list | None = None) -> dict | None:
        """One side as /server/compare shows it: a file of a title (entry: one of its record's f), its pages where the
        reader shows that very file; else the file the reader would show of the title (as pages, else as a book; one in
        a ZIP package by its own entry, z), or its first. Its date and, where its record has none, its size are the
        disk's: a file in a ZIP package, the package's. Its language, in words, where the file or its name says one
        (meta.language, "" for none), and how many files its title has (files: a copy removed is all of them). None for
        no title."""
        if not item or not item.get("f"):
            return None
        pages, book = reader.readable(item), reader.readable_book(item)
        shown = entry[0] if entry else pages or book or item["f"][0][0]
        if entry is None:
            entry = next((f for f in [*item.get("f", []), *item.get("z", [])] if f and f[0] == shown), item["f"][0])
        root, found = self.root.resolve(), unquote(shown)
        path = library_path(root, found)  # a file in a ZIP package: the package's own date and size on the disk
        if path is None or not path.is_file():
            packed = reader.PACKED.fullmatch(found)
            path = library_path(root, packed[1]) if packed else path
        try:
            st = path.stat() if path is not None else None
            date = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)) if st else ""
        except (OSError, OverflowError, ValueError):  # gone, or a date the system can't put in words (Windows, before 1970)
            st, date = None, ""
        cover = item.get("c")
        return {"id": item["id"], "t": item.get("t", ""), "name": PurePosixPath(found).name,
                "format": entry[1] if len(entry) > 1 else "",
                "size": entry[2] if len(entry) > 2 and isinstance(entry[2], int) else (st.st_size if st else 0),
                "date": date, "reads": "page" if shown == pages else "book" if shown == book else "",
                "cover": "/catalog/" + cover if isinstance(cover, str) and library.OWN_COVER.match(cover) else "",
                "language": meta.language(path, PurePosixPath(found).name) if path is not None else meta.named_language(found),
                "files": len(item.get("f") or [])}

    # ---- removing a copy's files into _Removed, and putting them back (removed.py) ----
    KEEPS = {"left": (1,), "right": (0,), "none": (0, 1)}  # what's kept of two copies: the sides whose files go

    def _build(self, back: bool = False) -> tuple[bool, str]:
        """A catalog build for files removed or put back, by the server's own (periodic.Builds): started at once, or once
        the one running has ended. Whether it started at once, and what follows, in words."""
        card = "comes back to" if back else "leaves"
        if self.builds.want(self.root, _stamp):
            return True, f"A catalog build has started: the card {card} the library page once it's done."
        return False, (f"A catalog build is running just now: another starts once it has ended, and the card {card} the "
                       "library page once that one is done.")

    def _records_refused(self, e: BaseException) -> None:
        """The words for what kept a removal, a put back or an empty from the records (409): another computer's, a
        newer Hylly's, or a database that can't be written just now."""
        said = str(e) if isinstance(e, (db.NotHere, db.Newer, db.Unusable, SystemExit)) else "The records can't be written just now."
        self._json({"ok": False, "error": said or "The records can't be written just now."}, 409)

    def _server_remove(self) -> None:
        """POST /server/remove, the owner's, with the page's key (SERVER_KEY), from To decide: for an open same question,
        {id: its number, keep: left, right or none, offered: what it offered as the page showed it}, keep left and keep
        right (Keep only this one, in Compare) move the other copy's files into _Removed, and Remove both both copies'
        (removed.remove: a copy's files are its card's, each moved, never deleted, its removal kept, a shop's file with
        its title's answer that it was moved); the copies are the cards /server/compare shows (_same_sides). Then a
        catalog build is started
        (_build). Refused, nothing moved: 400 for what is no question's number or keep, 404 for no open same question
        of that number, 409 when it offers other options now than the page showed, when its copies aren't two cards
        just now, for a file that may not be removed (removed.place, and one the server wouldn't send: library_path,
        _servable), and for records that can't be written, in words the page shows. A file that won't move (in use)
        stops it there, those before it moved and kept: 409, saying so."""
        ask = self._ask() or {}
        number, keep, offered = ask.get("id"), ask.get("keep"), ask.get("offered")
        if (isinstance(number, bool) or not isinstance(number, int) or not 0 < number < curation.NUMBER_MOST
                or not isinstance(keep, str) or keep not in self.KEEPS or not isinstance(offered, str) or len(offered) > 64):
            return self._json({"ok": False, "error": "Which question, and which copy to keep: left, right or none?"}, 400)
        try:
            found = next((q for q in self._open_questions() if q.get("id") == number and q.get("kind") == "same"), None)
        except (*RECORDS_FAILED, db.Newer) as e:
            return self._records_refused(e)
        if found is None:
            return self._json({"ok": False, "error": f"No question {number} about two copies is open."}, 404)
        if found.get("offered") != offered:
            return self._json({"ok": False, "error": f"Question {number} offers other options now than those the page "
                                                     "showed: nothing was removed. Reload the page and look again."}, 409)
        titles, _ = self._same_sides(found)
        if len(titles) != 2:
            return self._json({"ok": False, "error": f"Question {number}'s copies aren't two cards just now (one gone, or "
                                                     "one with the other since the last build): nothing was removed."}, 409)
        with self.EDITS:
            try:
                files = removed.going(self.root, [titles[n] for n in self.KEEPS[keep]])
                if not files:
                    return self._json({"ok": False, "error": "Its files are gone from the library already: the next "
                                                             "catalog build takes the card away."}, 409)
                root = self.root.resolve()  # and each a file the server would send, by its own rules too
                unsent = next((f["path"] for f in files if (target := library_path(root, f["path"])) is None
                               or not self._servable(root, target, self.catalog.lists)), None)
                if unsent is not None:
                    return self._json({"ok": False, "error": f"{unsent} isn't a file of the library that may be removed: "
                                                             "nothing was removed."}, 409)
                done, trouble = removed.remove(self.root, files, by=self._user)
            except removed.Refused as e:
                return self._json({"ok": False, "error": str(e)}, 409)
            except (*RECORDS_FAILED, db.Newer, SystemExit) as e:
                return self._records_refused(e)
        with contextlib.suppress(OSError):
            self.catalog.get()  # lays the new answers over the titles
        built, then = self._build() if done else (False, "")
        moved = (f"Moved {many(len(done), 'file')} ({size_words(sum(f['size'] for f in done))}) into {removed.FOLDER}, "
                 f"where Put back, under Removed, brings {'it' if len(done) == 1 else 'them'} back.") if done else ""
        if trouble:
            return self._json({"ok": False, "error": f"{trouble} {moved} {then}".strip(), "removed": len(done),
                               "build": built}, 409)
        self._json({"ok": True, "removed": len(done), "build": built, "did": f"{moved} {then}"})

    def _removed_list(self) -> None:
        """GET /server/removed, the owner's: what _Removed holds (removed.listing), for the Server page's Removed part,
        which it shows only when there's something there. One short line where the records can't be read just now, or
        _Removed is a link or a junction (removed.Refused)."""
        try:
            found = removed.listing(self.root)
        except removed.Refused as e:
            return self._json({"ok": False, "error": str(e), "files": [], "count": 0, "bytes": 0, "more": False})
        except (*RECORDS_FAILED, db.Newer):
            return self._json({"ok": False, "error": f"What {removed.FOLDER} holds can't be read just now.", "files": [],
                               "count": 0, "bytes": 0, "more": False})
        self._json({"ok": True, "folder": removed.FOLDER, **found})

    def _put_back(self) -> None:
        """POST /server/put-back, the owner's, with the page's key: {held: a file's place below _Removed, as
        /server/removed gives it} back in its place in the library (removed.put_back: its removal's answer taken back),
        then a catalog build started (_build). Refused, nothing changed: 400 for no place, 409 for one that isn't a file
        in _Removed, a place that is taken or isn't the library's, or records that can't be written, in words."""
        held = (self._ask() or {}).get("held")
        if not isinstance(held, str) or not held or len(held) > 4096:
            return self._json({"ok": False, "error": f"Which file? Its place below {removed.FOLDER}."}, 400)
        with self.EDITS:
            try:
                done = removed.put_back(self.root, held, by=self._user)
            except removed.Refused as e:
                return self._json({"ok": False, "error": str(e)}, 409)
            except (*RECORDS_FAILED, db.Newer, SystemExit) as e:
                return self._records_refused(e)
        with contextlib.suppress(OSError):
            self.catalog.get()
        built, then = self._build(back=True)
        self._json({"ok": True, "path": done["path"], "build": built, "did": f"Put {done['path']} back in its place. {then}"})

    def _empty_removed(self) -> None:
        """POST /server/empty-removed, the owner's, with the page's key: {digest: what _Removed held as the page showed it,
        as /server/removed gives it} every file in _Removed deleted for good (removed.empty), once it holds just that,
        each by its place and size. Refused, nothing deleted: 400 for no digest, 409 for any other file there now, a
        link or a junction in the way, or records that can't be written."""
        digest = (self._ask() or {}).get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return self._json({"ok": False, "error": f"What did the page show in {removed.FOLDER}? Reload it, and empty it then."}, 400)
        with self.EDITS:
            try:
                done = removed.empty(self.root, digest)
            except removed.Refused as e:
                return self._json({"ok": False, "error": str(e)}, 409)
            except (*RECORDS_FAILED, db.Newer, SystemExit) as e:
                return self._records_refused(e)
        left = done["left"]
        said = (f"Deleted {many(done['deleted'], 'file')} ({size_words(done['bytes'])}) from {removed.FOLDER} for good."
                + (f" {many(len(left), 'file')} couldn't be deleted (in use?): {', '.join(left[:5])}"
                   + (" and more" if len(left) > 5 else "") + "." if left else "")
                + (" There were more than could be counted at once: empty it again." if done["more"] else ""))
        self._json({"ok": not left, **done, "did": said, **({"error": said} if left else {})})

    def _to_decide(self) -> int:
        """For the library page, the owner's alone: how many questions are open on the Server page's To decide (the quiet
        kinds left out, as there); 0 where they can't be read. A title's own come with its details (_title_questions)."""
        try:
            return len(self._open_questions())
        except db.Newer:
            return 0
        except RECORDS_FAILED as e:
            self._not_read(e)
            return 0

    def _title_questions(self) -> None:
        """GET /api/questions?id=ID, the owner's alone: a title's open questions, of every kind, a lookup's near matches
        too (the quiet ones, each answered from its details), by any of its ids, a copy's too, and those whose answers
        were taken back otherwise than by their questions (lapsed), at once; with the library page's key, so that its
        details may answer them (/api/decide)."""
        title = (self._query.get("id") or [""])[0]
        index = self._index()
        rec = index.get(title)
        if rec is None:
            return self._json({"ok": True, "items": []})
        ids = set(curation.ids_of(rec))
        try:
            found = curation.questions(self.root, kinds=set(curation.QUESTIONS), lapsed=True)
        except (*RECORDS_FAILED, db.Newer):
            return self._json({"ok": True, "items": []})
        mine = [q for q in found if q["catalog_id"] in ids
                or ids & {s.get("id") for s in q.get("subjects") or [] if isinstance(s, dict)}]
        return self._json({"ok": True, "items": mine})

    @records()
    def _answer_question(self) -> None:
        """The owner's answer to a question, from the Server page (/server/decide, with its key) or from a title's
        details in the library page (/api/decide, a near match's Use it, with the page's key), from this site, as the
        routes' guards have it: {id: its number, choice: an option's number from 1, a file's name, "keep" or "auto" to
        take it back, offered: what the question offered as the page showed it}, by the same edit as hylly decide
        (curation.answer). Refused, nothing changed: 400 for what is no question's number or answer (bounded, so no
        number can be too big to look up), 409 when the question offers other options now than the page showed, with
        words the page shows."""
        ask = self._ask() or {}
        number, choice, offered = ask.get("id"), ask.get("choice"), ask.get("offered")
        if (isinstance(number, bool) or not isinstance(number, int) or not 0 < number < curation.NUMBER_MOST
                or isinstance(choice, bool) or not isinstance(choice, (int, str)) or len(str(choice)) > curation.FILE_NAME_MOST
                or not (offered is None or (isinstance(offered, str) and len(offered) <= 64))):
            return self._json({"ok": False, "error": "Which question, and which of its answers?"}, 400)
        try:
            with self.EDITS:
                done = curation.answer(self.root, number, str(choice), by=self._user, offered=offered)
        except SystemExit as e:  # curation's own words: no such question or option (USAGE), or one that changed meanwhile
            return self._json({"ok": False, "error": str(e)}, 409 if getattr(e, "code", None) == NEEDS_YOU else 400)
        except (db.NotHere, db.Newer, db.Unusable) as e:
            return self._json({"ok": False, "error": str(e)}, 409)
        with contextlib.suppress(OSError):
            self.catalog.get()  # lays the new edit over the titles
        return self._json({"ok": True, "field": done["field"], "id": done["id"], "did": done["did"],
                           **({"took_back": True} if done["option"].get("answer") == "auto" and "file" not in done["option"] else {})})

    def _database_page(self) -> None:
        """The Database page (dbpage.py): an owner's, as the Server page; its questions carry the page's key, and come
        from this site's own pages, so no other site can ask them (SERVER_KEY). Only reading, whatever is asked."""
        self._owners_page("database_page.html")

    def _database_examples(self) -> None:
        self._json({"ok": True, "examples": dbpage.EXAMPLES})

    def _database_tables(self) -> None:
        """Every table, and the limits a question and a table's pages are held to (dbpage.limits), which the page
        says."""
        self._database_answer(lambda c, schema: {"file": str(db.file()), "schema": schema, "tables": dbpage.tables(c),
                                                 "limits": dbpage.limits()})

    def _database_table(self) -> None:
        ask = self._query
        name, page = (ask.get("name") or [""])[0], library.whole(ask.get("page"), 0)
        self._database_answer(lambda c, schema: dbpage.table(c, name, page))

    def _database_query(self) -> None:
        sql = (self._ask() or {}).get("sql")
        self._database_answer(lambda c, schema: dbpage.question(c, sql))

    def _database_answer(self, answer: Callable) -> None:
        """One of the Database page's questions, answered on a connection of its own (dbpage.connect), closed after:
        answer(connection, schema) gives what goes with ok. A database that can't be opened is said (409), and what the
        page may not ask (dbpage.Refused), 400."""
        try:
            c, schema = dbpage.connect()
        except sqlite3.Error as e:
            return self._json({"ok": False, "error": f"The database can't be opened: {e}"}, 409)
        try:
            return self._json({"ok": True, **answer(c, schema)})
        except dbpage.Refused as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        finally:
            c.close()

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(status, "application/json", json.dumps(data).encode("utf-8"), cache=False)

    def _send(self, status: int, ctype: str, body: bytes, cache: bool = True, headers: dict | None = None) -> None:
        packed = len(body) > 1024 and PACKABLE.match(ctype) and _accepts_gzip(self.headers)
        if packed:  # feeds and lists are text: a fifth of the size or less
            body = gzip.compress(body, compresslevel=5, mtime=0)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if packed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _servable(root: Path, target: Path, listed: Callable[[str], bool] = lambda rel: False) -> bool:
        """A file of the library (root and target resolved): not outside it, not missing, nothing hidden, and of Hylly's
        own folders only what the library page needs. Hidden: at the top, a name starting with a dot or _, which no build
        reads (folders.is_own); below it, a name starting with a dot, unless the last build listed that very file
        (listed: Catalog.lists), by its path as resolved, exactly: on Windows the disk's own spelling, on Linux and
        macOS the spelling asked, so that there the same path in other capitals is refused. So a shop's title starting
        with a dot, whose folder is named so (paths.clean keeps the dot), and a folder of yours named so open, while .git,
        .Trash-1000, a sync tool's .stversions, at any depth (no build reads them: catalog.systems_own), and whatever
        else no build listed stay hidden. state\\ is private (logins' traces, orders, the lookups' answers, the
        database's nightly copies), curation\\ is the owner's (the words that suggest tags, and the rules if kept
        there), and catalog\\ also holds the files' metadata cache and the list of everything bought. The first two are
        refused by the folder itself, whatever the address calls it (_private_folder); the rest goes by the plain name,
        as _file sends it."""
        if not (target.is_relative_to(root) and target.is_file()):
            return False
        asked = target.relative_to(root).parts
        parts = [p.lower() for p in asked]
        if parts[0].startswith((".", "_")) or _private_folder(root, asked[0]):
            return False
        if any(p.startswith(".") for p in asked) and not listed("/".join(asked)):
            return False
        if parts[0] == "catalog":  # its covers; the page, its data and its help are the server's own (_own_file)
            return len(parts) == 3 and parts[1] == "covers"
        return True

    PROGRAM_FILES = {  # address: the folder beside this file, and the kinds of file in it that are served
        "/foliate/": ("foliate", {".js": "text/javascript; charset=utf-8"}),
        "/fonts/": ("fonts", {".woff2": "font/woff2", ".css": "text/css; charset=utf-8"}),
    }

    def _program_file(self) -> None:
        """The reader's own files, from the program folder: its engine for EPUB, MOBI, AZW3 and FB2 (foliate-js),
        and the fonts a book can be read in."""
        prefix = next(p for p in self.PROGRAM_FILES if self._path.startswith(p))
        folder, kinds = self.PROGRAM_FILES[prefix]
        base = Path(__file__).with_name(folder).resolve()
        rest = self._path.removeprefix(prefix)
        # /foliate/<its commit>/view.js: the page names the version it wants, so a browser keeps the files for good
        versioned = re.match(r"[0-9a-f]{7,40}/", rest) if prefix == "/foliate/" else None
        target = library_path(base, rest[versioned.end():] if versioned else rest)
        if target is None or target.suffix not in kinds or not target.is_file():
            return self._send(404, "text/plain", b"not found")
        cache = ("public, max-age=31536000, immutable" if versioned
                 else "public, max-age=604800" if target.suffix == ".woff2" else "no-cache")
        if self.command == "HEAD":  # the headers alone, the file unread: its length as it is, as a HEAD may leave packing out
            self.send_response(200)
            self.send_header("Content-Type", kinds[target.suffix])
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("Cache-Control", cache)
            return self.end_headers()
        self._send(200, kinds[target.suffix], target.read_bytes(), headers={"Cache-Control": cache})

    def _saved(self, name: str) -> dict:
        """?dl=1 (the library page's file links): saved as a file under its own name, not shown in the browser."""
        if self._query.get("dl") != ["1"]:
            return {}
        plain = re.sub(r'[^ !#-\[\]-~]', "_", name)  # for browsers that don't read the UTF-8 name
        return {"Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"}

    def _file(self, path: str) -> None:
        root = self.root.resolve()
        target = library_path(root, path)
        packed = None if target is not None and target.is_file() else reader.PACKED.fullmatch(path.lstrip("/"))
        if packed:
            return self._zipped(root, *packed.groups())
        if target is None or not self._servable(root, target, self.catalog.lists):
            if path.strip("/").lower() in ("catalog", "catalog/index.html") and (what := missing(self.root)):
                return self._not_ready(what)  # where / leads: a word on what's missing, not a bare "not found"
            return self._send(404, "text/plain", b"not found")
        if self._as_script():
            return self._send(404, "text/plain", b"not found")
        cover = target.relative_to(root).parts[0].lower() == "catalog"  # catalog\covers: no other is served (_servable)
        version = (self._query.get("v") or [None])[0] if cover else None
        self._send_file(target, **library_file(target.name), version=version)

    def _own_file(self, rel: str) -> None:
        """The library page (OWN_FILES), its data.js and its help, whatever the library holds under those names: this
        version's page and help, from the program; and data.js made from the catalog as the server has read it, your
        tags and corrections laid over it (Catalog.page_script). Before there's a catalog, the page's address says
        what's missing (missing), and its data is not found."""
        if rel == "catalog/help.html":  # the owner's names this computer's folders; everyone else's says them in words
            text = self._named(_filled_help_page(self.root, self._role() != "owner", self.source_url))
            return self._send(200, "text/html; charset=utf-8", text.encode("utf-8"), cache=False, headers=page_headers(text))
        what = missing(self.root)
        if what:
            return self._not_ready(what) if rel == "catalog/index.html" else self._send(404, "text/plain", b"not found")
        if rel == "catalog/data.js":
            return self._page_data()
        target = Path(__file__).with_name("catalog_page.html")
        st = target.stat()
        headers = page_headers(_page_text(str(target), st.st_mtime_ns, st.st_size), self.catalog.cover_sites(), reader=True)
        self._send_file(target, headers=headers, own=True)

    def _page_data(self) -> None:
        """The library page's data.js (Catalog.page_script), asked for again at every load (no-cache): unchanged, by its
        tag (If-None-Match), it's a 304 with nothing more; else whole, packed when the browser takes that. A HEAD gets the
        headers alone (end_headers)."""
        tag, body, packed = self.catalog.page_script()
        if tag in (t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")):
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            return self.end_headers()
        gzipped = _accepts_gzip(self.headers)
        self.send_response(200)
        self.send_header("Content-Type", library.MIME["js"])
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("ETag", tag)
        self.send_header("Cache-Control", "no-cache")  # the browser asks each time, so a change shows on a reload
        self.send_header("Content-Length", str(len(packed if gzipped else body)))
        self.end_headers()
        try:
            self.wfile.write(packed if gzipped else body)
        except (ConnectionError, OSError):
            pass

    def _not_ready(self, what: str) -> None:
        """The library page's address before there's a page (missing): still not found, but a short page says what's
        missing and what to run. With the headers of Hylly's own pages, and kept by no browser, so a reload once it's
        mended shows the library."""
        text = not_ready_page(self.library_name, what)
        self._send(404, "text/html; charset=utf-8", text.encode("utf-8"), cache=False, headers=page_headers(text))

    def _as_script(self) -> bool:
        """Asked for as something that runs (a script, a worker, a style-sheet transformation): the library page never
        asks a library file for that, so only a book or a file trying to run another as the library would."""
        return (self.headers.get("Sec-Fetch-Dest") or "").strip().lower() in SCRIPT_DESTS

    def _send_file(self, target: Path, headers: dict | None = None, ctype: str | None = None, own: bool = False,
                   version: str | None = None) -> None:
        """A file in the library (or the cache), whole or the part a Range header asks for, in pieces. own: one of
        Hylly's own (the library page), which goes packed, and is kept packed (_gzipped); a file of the library never
        is, as its size is anyone's. Hylly's own goes from memory however it's asked for (_own_bytes), packed,
        plain or a part, so no client, however slow, holds it open: on Windows an open file can't be replaced. A HEAD
        gets the headers alone: reading apps send one before each download, and the file isn't read for it. Hylly's own
        goes with its tag (ETag), and is unchanged (304) only for a browser that holds that very page (_same_page).
        version: what a cover's address says of its version (?v=, as the library page's data has it): the file's own as
        it's opened and read (library.cover_version, _by_version), and the browser keeps it (KEPT_COVER), as that
        address is that picture for good; any other file, or another version, the browser asks about each time, so a
        rebuilt catalog shows on reload."""
        held, cache = None, "no-cache"
        if version is None:
            st = target.stat()
        else:  # a cover goes as it was read, judged by the file as opened, never by a look before
            st, held, kept = _by_version(target, version, read=self.command != "HEAD")
            cache = KEPT_COVER if kept else cache
        tag = _own_tag(str(target), st.st_mtime_ns, st.st_size) if own else None
        saved = {**self._saved(target.name), **(headers or {}), **({"ETag": tag} if tag else {})}
        if own and self.command != "HEAD":
            held = _own_bytes(str(target), st.st_mtime_ns, st.st_size)
        size, modified = len(held) if held is not None else st.st_size, formatdate(st.st_mtime, usegmt=True)
        rng = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", self.headers.get("Range", ""))  # a longer number: no range
        if not rng and (_same_page(self.headers, tag, st.st_mtime) if tag else _unchanged_since(self.headers.get("If-Modified-Since"), st.st_mtime)):
            self.send_response(304)
            self.send_header("Last-Modified", modified)
            if tag:
                self.send_header("ETag", tag)
            if cache != "no-cache":  # kept again for as long
                self.send_header("Cache-Control", cache)
            for name, value in (headers or {}).items():  # the browser keeps these with its copy: a page's policy may be new
                self.send_header(name, value)
            self.end_headers()
            return
        start, end = 0, size - 1
        if rng and (rng[1] or rng[2]):
            if rng[1]:  # bytes=START- or START-END, the end no further than the file
                start, end = int(rng[1]), min(int(rng[2]), size - 1) if rng[2] else size - 1
            else:  # bytes=-N: the last N bytes, at most the whole file
                start, end = max(0, size - int(rng[2])), size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        ctype = ctype or library.MIME.get(target.suffix.lower().lstrip(".")) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if own and not rng and size > 1024 and PACKABLE.match(ctype) and _accepts_gzip(self.headers):  # the page: sent packed
            body = _gzipped(str(target), st.st_mtime_ns, st.st_size)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Last-Modified", modified)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            for name, value in saved.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass
            return
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        for name, value in saved.items():
            self.send_header(name, value)
        self.send_header("Last-Modified", modified)
        self.send_header("Cache-Control", cache)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        if held is not None:
            try:
                self.wfile.write(held[start:end + 1])
            except (ConnectionError, OSError):
                pass
            return
        with target.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionError, OSError):
                    return
                left -= len(chunk)

    def _zipped(self, root: Path, package: str, member: str) -> None:
        """A file inside a ZIP package (a StoryBundle download), '.../book.zip/book.epub': unpacked as it is
        sent, whole. A part of it (a big book read in the page) comes from a copy unpacked once into the cache,
        since a range of a packed file would mean unpacking up to it, every time. A HEAD unpacks nothing: its length is
        in the package's list of what it holds, and a range is only a GET's (RFC 9110), so it gets the whole one's."""
        target = library_path(root, package)
        if target is None or not self._servable(root, target, self.catalog.lists) or self._as_script():
            return self._send(404, "text/plain", b"not found")
        if self.headers.get("Range") and self.command != "HEAD":
            try:
                cached = reader.inner(target, member)
            except (reader.Unreadable, OSError):
                return self._send(404, "text/plain", b"not found")
            policy = library_file(member)  # saved under its name in the ZIP: the cache keeps it under one of its own
            return self._send_file(cached, {**self._saved(member.rsplit("/", 1)[-1]), **policy.get("headers", {})}, policy["ctype"])
        st = target.stat()
        modified = formatdate(st.st_mtime, usegmt=True)
        if _unchanged_since(self.headers.get("If-Modified-Since"), st.st_mtime):
            self.send_response(304)
            self.send_header("Last-Modified", modified)
            self.end_headers()
            return
        try:
            z = zipfile.ZipFile(target)
            info = z.getinfo(member)
        except (KeyError, OSError, zipfile.BadZipFile):
            return self._send(404, "text/plain", b"not found")
        with z:
            policy = library_file(member)  # by its type, as a file of the library is: never a script's
            self.send_response(200)
            self.send_header("Content-Type", policy["ctype"])
            for name, value in {**self._saved(member.rsplit("/", 1)[-1]), **policy.get("headers", {})}.items():
                self.send_header(name, value)
            self.send_header("Last-Modified", modified)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(info.file_size))
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with z.open(info) as fh:
                    while chunk := fh.read(1 << 20):
                        self.wfile.write(chunk)
            except (ConnectionError, OSError, zipfile.BadZipFile):
                return  # the reader went away, or the package is damaged: the length tells the app it came up short

    # ---- the routes: every address the server answers, who may ask (Guard), and what answers ----------------------
    # One row an address, its guards (one, or several in turn) and its handler: a new address is a new row, and the
    # walk of every address (tests/test_routes.py) asks each row, for every kind of caller. A row ending in * is a
    # beginning: the longest that fits takes the addresses under it that have no row of their own. An address no row
    # takes is not found (404), whoever asks; a logged-in caller whose role a row's guard doesn't let on is told why
    # (403); without a login, 401 (_admitted). Only the login page and its form, logging out, the health check, the
    # theme and the home screen's icons are OPEN, by the address exactly as sent.
    GET: dict[str, tuple] = {
        "/login": (OPEN, _login_page),
        "/healthz": (OPEN, _healthz),
        **dict.fromkeys(APP_FILES, (OPEN, _app_file)),
        "/manifest.webmanifest": (OPEN, _manifest),
        "/theme.css": (OPEN, _theme),
        "/": (ANYONE, _start),
        "/ping": (ANYONE, _ping),
        "/server": (SERVER_PAGE, _server_page),
        "/server/status": (SERVER_PAGE, _server_status),
        "/server/questions": (SERVER_PAGE, _questions),
        "/server/compare": (SERVER_PAGE, _compare),
        "/server/removed": (SERVER_PAGE, _removed_list),
        "/server/*": (ANYONE, _not_there),
        "/database": (DATABASE_PAGE, _database_page),
        "/database/examples": ((DATABASE_PAGE, SERVER_KEY), _database_examples),
        "/database/tables": ((DATABASE_PAGE, SERVER_KEY), _database_tables),
        "/database/table": ((DATABASE_PAGE, SERVER_KEY), _database_table),
        "/database/*": (ANYONE, _not_there),
        "/api/pages": (ANYONE, _api_pages),
        "/api/page": (ANYONE, _api_page),
        "/api/ahead": (ANYONE, _api_ahead),
        "/api/me/places": (ANYONE, _api_my_places),
        "/api/me/lists": (ANYONE, _api_my_lists),
        "/api/me/bookmarks": (ANYONE, _api_my_bookmarks),
        "/api/shelves": (ANYONE, _api_shelves),
        "/api/stars": (ANYONE, _api_all_stars),
        "/api/reviews": (ANYONE, _api_title_reviews),
        "/api/me/reports": (ANYONE, _api_my_reports),
        "/api/reports": (librarian("Reported titles are for the library's owners and librarians."), _api_reports),
        "/api/reviews/waiting": (owner("Reviews to check are for the library's owner."), _api_waiting_reviews),
        "/api/reviews/mine": (ANYONE, _api_my_reviews),
        "/api/catalog": (ANYONE, _api_catalog),
        "/api/session": (ANYONE, _api_session),
        "/api/questions": (owner("A title's questions are for the library's owner."), _title_questions),
        "/api/edits": (librarian("Recent changes are for the library's owners and librarians."), _api_edits),
        "/api/suggestions": (owner("Tag suggestions are for the library's owner."), _api_suggestions),
        "/api/declined": (owner("Tag suggestions are for the library's owner."), _api_declined),
        "/api/*": (ANYONE, _not_there),
        LICENCE_PATH + "*": (ANYONE, _licence),
        **dict.fromkeys((prefix + "*" for prefix in PROGRAM_FILES), (ANYONE, _program_file)),
        **dict.fromkeys((feed + "*" for feed in FEED_PATHS), ((FOR_THE_APPS, LENT), _feed)),
        "/*": (LENT, _library_file),
    }
    POST: dict[str, tuple] = {
        "/login": (OPEN, _login_post),
        "/logout": (OPEN, _logout),
        "/api/me/place": (PERSONAL, _api_place),
        "/api/me/mark": (PERSONAL, _api_mark),
        "/api/me/new-seen": (PERSONAL, _api_new_seen),
        "/api/me/guide-seen": (PERSONAL, _api_guide_seen),
        "/api/me/list": (PERSONAL, _api_list),
        "/api/me/bookmark": (PERSONAL, _api_bookmark),
        "/api/me/shelf": (PERSONAL, _api_my_shelf),
        "/api/me/stars": (PERSONAL, _api_stars),
        "/api/me/look": (PERSONAL, _api_look),
        "/api/me/review": (PERSONAL, _api_review),
        "/api/me/report": (PERSONAL, _api_report),
        "/api/reviews/check": ((CHANGE, owner("Reviews are checked by the library's owner.")), _api_check_review),
        "/api/reports/decide": (CHANGE, _api_decide_report),
        "/api/shelves": (CHANGE, _api_shared_shelf),
        "/api/tags": (CHANGE, _api_tags),
        "/api/fix": (CHANGE, _api_fix),
        "/api/suggest": ((CHANGE, owner("Tag suggestions are for the library's owner.")), _api_suggest),
        "/api/decide": ((CHANGE, owner("The questions are the library's owner's to answer.")), _answer_question),
        "/database/query": ((DATABASE_PAGE, SERVER_KEY), _database_query),
        "/server/stop": ((SERVER_PAGE, SERVER_KEY), _server_stop),
        "/server/restart": ((SERVER_PAGE, SERVER_KEY), _server_restart),
        "/server/decide": ((SERVER_PAGE, SERVER_KEY), _server_decide),
        "/server/remove": ((SERVER_PAGE, SERVER_KEY), _server_remove),
        "/server/put-back": ((SERVER_PAGE, SERVER_KEY), _put_back),
        "/server/empty-removed": ((SERVER_PAGE, SERVER_KEY), _empty_removed),
    }


def library_file(name: str) -> dict:
    """How a file of the library is sent, so that none of it runs as the library: a script as plain text, a packed
    file as bytes, and anything whose type a browser could run (HTML, SVG, any XML, an unknown type) in a sandbox of
    its own. Decided by the type worked out, not the last suffix alone: mimetypes looks past a packing suffix (to it
    x.js.gz is JavaScript, packed), and this computer's own list of types decides the rest (on some Linux systems .es
    is JavaScript too)."""
    ext = PurePosixPath(name).suffix.lower().lstrip(".")
    guessed, packing = mimetypes.guess_type(name)
    ctype = "application/octet-stream" if packing else library.MIME.get(ext) or guessed or "application/octet-stream"
    if ext in SCRIPTS or SCRIPT_TYPE.search(ctype):
        return {"ctype": "text/plain; charset=utf-8"}
    if ctype.startswith(INERT):
        return {"ctype": ctype}
    return {"ctype": ctype, "headers": {"Content-Security-Policy": SANDBOX}}


@functools.lru_cache(maxsize=8)
def _page_text(path: str, mtime_ns: int, size: int) -> str:
    """One of Hylly's own pages as text, once per version, for its policy's script hashes."""
    return Path(path).read_text(encoding="utf-8")


_filled_helps: dict[tuple[Path, bool], str] = {}  # each library's help as last made, kept while settings.toml has a mistake


def _filled_help_page(root: Path, generic: bool, source: str) -> str:
    """The help of the version that's running, its Source link to source ([server] source_url as the server read it).
    Not generic: with this computer's folders and files filled in, for the owner alone. Made afresh when the help
    template or settings.toml changes, or the catalog (each hylly catalog writes its data.js: own folders made since).
    A mistake in settings.toml keeps the help as last made, as the server keeps its settings; before any, the help in
    words, made from the program's own template as everyone else gets it. Never a file of the library named help.html:
    it would go as one of Hylly's own pages, its scripts allowed, and a file of that name can be put in the library by
    anyone who may write there."""
    template = Path(__file__).with_name("help.html").stat()
    try:
        built = (paths.catalog_dir(root) / "data.js").stat().st_mtime_ns
    except OSError:  # no catalog yet, or a share that isn't there just now: the help all the same
        built = 0
    try:
        text = _filled_help(root, built, (template.st_mtime_ns, template.st_size), settings._stamp(), generic, source)
    except (SystemExit, Exception):  # noqa: BLE001 - the help from before, put right with the settings
        kept = _filled_helps.get((root, generic))
        if kept is not None:
            return kept
        if generic:  # not even the words, which need no settings: the program itself is broken, and the log says how
            raise
        return _filled_help_page(root, True, source)
    _filled_helps[root, generic] = text
    return text


@functools.lru_cache(maxsize=4)
def _filled_help(root: Path, built_ns: int, template: tuple, settings_file: tuple, generic: bool, source: str) -> str:
    from . import helppage

    return helppage.help_page(root, generic=generic, source=source)


class _Deadline(io.RawIOBase):
    """What a request sends, read against one deadline for the whole request: each read waits only for the time
    that is left. (A read waiting on a socket can't be woken from another thread on Windows, so the wait itself
    has to be short.) Once the request is in (done), reads go on with the usual timeout."""

    def __init__(self, sock, until: float) -> None:
        self.sock, self.until, self.done = sock, until, False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self.done:
            left = self.until - time.monotonic()
            if left <= 0:
                raise TimeoutError("the request took too long to arrive")
            self.sock.settimeout(left)
        return self.sock.recv_into(buffer)


class _Nowhere:
    """Where a HEAD request's body goes: nowhere, while the connection itself is flushed and closed as usual."""

    def __init__(self, real) -> None:
        self.real = real

    def write(self, data) -> int:
        return len(data)

    def flush(self) -> None:
        self.real.flush()

    def close(self) -> None:
        self.real.close()

    @property
    def closed(self) -> bool:
        return self.real.closed


def _login_template() -> str:
    return Path(__file__).with_name("login_page.html").read_text(encoding="utf-8")


def _next_page(wanted: str) -> str:
    """Where a login at the root goes on to: the page asked for, on this server only, else the library."""
    if re.fullmatch(r"/(?![/\\])[!-\[\]-~]+", wanted):  # one leading /, no //other.site, printable ASCII without \ only
        return wanted
    return "/catalog/index.html"


def _accepts_gzip(headers) -> bool:
    for part in (headers.get("Accept-Encoding") or "").split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() == "gzip":
            return not re.fullmatch(r"\s*q\s*=\s*0(\.0*)?\s*", params)
    return False


COVER_READ_MOST = 8 << 20  # bytes of a cover asked for by its version, read whole to be judged (a thumbnail's: kB)


def _by_version(target: Path, version: str, read: bool) -> tuple[os.stat_result, bytes | None, bool]:
    """A cover asked for by a version (?v=: Handler._send_file): the file as it was opened (os.fstat), its bytes read
    whole for a GET (read), and whether the browser may keep it: only when the version asked is the one the file has
    (library.cover_version) from its opening to the end of the read. A cover a build writes again meanwhile, in its
    place or as a new file, goes as it was read, and the browser asks about it again: it is never kept under the version
    it had. One too big for a thumbnail goes as any file does."""
    with target.open("rb") as fh:
        opened = os.fstat(fh.fileno())
        if not read:
            return opened, None, library.cover_version(opened.st_mtime_ns) == version
        if opened.st_size > COVER_READ_MOST:
            return opened, None, False
        data = fh.read(COVER_READ_MOST + 1)
        after = os.fstat(fh.fileno())
    if len(data) > COVER_READ_MOST:  # it grew past that as it was read
        return after, None, False
    same = opened.st_mtime_ns == after.st_mtime_ns and len(data) == after.st_size
    return after, data, same and library.cover_version(after.st_mtime_ns) == version


@functools.lru_cache(maxsize=8)
def _own_bytes(path: str, mtime_ns: int, size: int) -> bytes:
    """One of Hylly's own files, read once per version (the key holds its time and size) and sent from memory, so none
    is held open while a client takes it (Handler._send_file). Never a file of the library, whose size is anyone's."""
    return Path(path).read_bytes()


@functools.lru_cache(maxsize=8)
def _gzipped(path: str, mtime_ns: int, size: int) -> bytes:
    """One of Hylly's own files packed once per version (the key holds its time and size): the library page shrinks
    to a fifth, which on a phone is a second less of empty page (its data.js is packed as it's made: Catalog.page_script).
    Never a file of the library, whose size is anyone's: whole in memory, and kept."""
    return gzip.compress(_own_bytes(path, mtime_ns, size), compresslevel=6, mtime=0)


@functools.lru_cache(maxsize=8)
def _own_tag(path: str, mtime_ns: int, size: int) -> str:
    """One of Hylly's own files' tag (ETag), by its bytes, once per version (the key holds its time and size)."""
    return '"' + hashlib.sha256(_own_bytes(path, mtime_ns, size)).hexdigest()[:32] + '"'


def _same_page(headers, tag: str, mtime: float) -> bool:
    """Whether the browser holds this very page of Hylly's own, so that a 304 leaves it with its copy: by the page's tag
    (If-None-Match, which decides alone when sent), else by its date, exactly (If-Modified-Since). Not by a date that
    is no older, as a file of the library goes (_unchanged_since): a browser holding a newer version's page, the
    server gone back a version since, would keep that page under this version's policy, which lets none of its
    scripts run, and the library would stay empty."""
    asked = headers.get("If-None-Match")
    if asked is not None:
        return tag in (t.strip() for t in asked.split(","))
    since = headers.get("If-Modified-Since")
    try:
        return bool(since) and parsedate_to_datetime(since).timestamp() == int(mtime)
    except (TypeError, ValueError):
        return False


def _unchanged_since(header: str | None, mtime: float) -> bool:
    try:
        return bool(header) and int(mtime) <= parsedate_to_datetime(header).timestamp()
    except (TypeError, ValueError):
        return False


class Server(ThreadingHTTPServer):
    # On Windows this option lets a second server take the same port unnoticed; without it, a busy port is an error.
    allow_reuse_address = os.name != "nt"
    # Connections waiting to be taken. Python's 5 was too few: for a screenful of covers a tunnel (Tailscale Funnel)
    # opens dozens at once, and those turned away came back as 502, so Safari showed titles instead of covers.
    request_queue_size = 256
    MAX_CONNECTIONS = 128  # served at once; more are answered "busy" at once, so a flood can't start a thread for each
    # Of those, from one address. Not for a trusted proxy's (trusted_proxies): its connections carry many senders, and
    # who they are only its headers say, which come after the connection is taken. MAX_CONNECTIONS still holds.
    PER_ADDRESS = 32
    BUSY_WAIT = 0.2  # seconds a connection answered "busy" has to send its request, read before the answer (_turn_away)
    BUSY_HELD = 64  # those waiting so at once, and as many more handed over; any more are closed without an answer
    proxies: tuple = access.LOOPBACK

    def __init__(self, server_address, *args, **kwargs) -> None:
        # Where it listens ([server] listen) says the kind: "::" or another IPv6 address takes an IPv6 socket
        self.address_family = socket.AF_INET6 if ":" in server_address[0] else socket.AF_INET
        self._slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        self._open: dict[str, int] = {}
        self._open_lock = threading.Lock()
        self._turned_away: queue.Queue = queue.Queue(self.BUSY_HELD)  # for _turn_away; None wakes it to look at _closing
        self._turning_away: threading.Thread | None = None
        self._closing = threading.Event()  # the server has closed: _turn_away ends
        super().__init__(server_address, *args, **kwargs)

    def server_bind(self) -> None:
        """On "::", IPv4 too where the system allows it (Windows, Linux, macOS do): its senders come as ::ffff:192.0.2.7,
        which access.unmapped makes plain again. TCPServer's own bind, not HTTPServer's, which asks a name server for a
        name (getfqdn) that nothing here uses."""
        if self.address_family == socket.AF_INET6 and ipaddress.ip_address(self.server_address[0]).is_unspecified:
            with contextlib.suppress(OSError, AttributeError):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]

    def process_request(self, request, client_address) -> None:
        """Never waits: the accept loop must go on taking connections, or a few slow ones would stop everyone."""
        ip = access.unmapped(client_address[0])
        with self._open_lock:
            crowded = self._open.get(ip, 0) >= self.PER_ADDRESS and not access.is_trusted(ip, self.proxies)
        if crowded or not self._slots.acquire(blocking=False):
            return self._busy(request)
        with self._open_lock:
            self._open[ip] = self._open.get(ip, 0) + 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(ip)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(access.unmapped(client_address[0]))

    def _release(self, ip: str) -> None:
        with self._open_lock:
            self._open[ip] = self._open.get(ip, 1) - 1
            if self._open[ip] <= 0:
                del self._open[ip]
        self._slots.release()

    def handle_error(self, request, client_address) -> None:
        """What got past a request's own handling (Handler.handle_one_request answers the rest): one line in server.log
        for each kind (first_failure), not socketserver's traceback for every request; nothing for a connection the
        other end dropped, which is nobody's mistake."""
        e = sys.exc_info()[1]
        if e is None or isinstance(e, ConnectionError):
            return
        if first_failure(e):
            frames = traceback.extract_tb(e.__traceback__)
            where = f" in {Path(frames[-1].filename).name}, line {frames[-1].lineno}" if frames else ""
            _stamp(f"A connection from {client_address[0]} failed: {type(e).__name__}{where}.")

    def _busy(self, request) -> None:
        """Busy: handed to the one thread that answers those (_turn_away), and nothing waited for here, where the
        accept loop must go on taking connections. With that thread's hands full, closed at once, unanswered: a flood
        holds up nobody, not even itself."""
        if self._turning_away is None or not self._turning_away.is_alive():  # only the accept loop comes here: started
            # the first time it's needed, and again should it have ended (it says why in server.log)
            self._turning_away = threading.Thread(target=self._turn_away, name="busy", daemon=True)
            self._turning_away.start()
        try:
            self._turned_away.put_nowait(request)
        except queue.Full:
            self.shutdown_request(request)

    def _turn_away(self) -> None:
        """503 for each connection handed over (_busy), once its request is in or BUSY_WAIT has passed: what it sent is
        read first, as closing over what was sent and not read resets the connection before the client reads the
        answer. Each waits by its own clock, all at once, so one that sends nothing holds up none of the others."""
        waiting = selectors.DefaultSelector()
        until: dict = {}  # each connection waited on, and until when
        try:
            while not self._closing.is_set():  # looked at between waits of 0.02 seconds, or woken by a None
                while len(until) < self.BUSY_HELD:  # those handed over meanwhile; when none are waited on, a wait for one
                    try:
                        request = self._turned_away.get(block=not until)
                    except queue.Empty:
                        break
                    if request is None:  # woken: the server has closed
                        break
                    try:
                        request.setblocking(False)
                        waiting.register(request, selectors.EVENT_READ)
                    except (OSError, ValueError):  # gone already
                        self.shutdown_request(request)
                        continue
                    until[request] = time.monotonic() + self.BUSY_WAIT
                if not until:  # woken with none to wait on: to look whether the server has closed
                    continue
                ready = {key.fileobj for key, _ in waiting.select(timeout=0.02)}  # short: those handed over meanwhile
                now = time.monotonic()  # wait no longer than that to be taken in, nor the server's close to be seen
                for request in [r for r in until if r in ready or until[r] <= now]:
                    del until[request]
                    waiting.unregister(request)
                    self._answer_busy(request)
        except Exception as e:  # noqa: BLE001 - said in server.log; the next connection turned away starts another (_busy)
            if tell_failure(e, "Answering connections busy failed", f"{type(e).__name__}: {str(e)[:300]}"):
                print(_trace(e), end="", file=sys.stderr, flush=True)
        finally:  # none left open: those waited on, and once the server has closed, those still handed over
            for left in until:
                self.shutdown_request(left)
            waiting.close()
            while self._closing.is_set():
                try:
                    left = self._turned_away.get_nowait()
                except queue.Empty:
                    break
                if left is not None:
                    self.shutdown_request(left)

    def _answer_busy(self, request) -> None:
        """503, after reading what has come (none of it waited for: _turn_away waited), and the connection closed."""
        try:
            with contextlib.suppress(OSError):  # nothing there, or the client already gone
                request.recv(65536)
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
        self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        self._closing.set()  # the busy answers' thread ends (_turn_away): it looks between its short waits, and one
        with contextlib.suppress(queue.Full):  # waiting for a connection is woken; a full queue means it isn't waiting
            self._turned_away.put_nowait(None)


def _open_database(watch, root: Path) -> None:
    """The database opened before anyone asks, and the library's own records taken on where this computer keeps them
    (db.own): anything wrong goes in the log, and raises the alarm. So does another database taking them on while it
    runs, once: from then on the server writes them no more."""
    try:
        found = db.database()
        found.on_lost = lambda words: (_stamp(f"The database: {words}"), watch.event("people", name="everyone", what=words))
        found.on_note = lambda words: _stamp(f"The database: {words}")  # what own did on its own: a marker tied here
        with found.read():
            pass  # opened: brought up to this version, or found to be a newer version's
        if not paths.library_missing(root):  # the disk isn't there yet: taken on when it is, at the first change or look
            db.own(found, root)
    except (*RECORDS_FAILED, db.Newer) as e:
        _stamp(f"The database: {e}")
        watch.event("people", name="everyone", what=str(e))


class CantListen(OSError):
    """The server couldn't listen at its address and port, as the system said (cant_listen): the one trouble at its start
    that the port's own words fit. Any other, such as a settings folder it may not write, keeps its own words."""


def make_server(root: Path, host: str, port: int, *, access_log: bool = False, proxy_header: str = "X-Forwarded-For",
                hosts: Hosts | None = None, proxies: tuple = access.LOOPBACK, address: str = "",
                noted: Callable[[str, str, list[str]], None] = lambda key, address, addresses: None
                ) -> tuple[ThreadingHTTPServer, list[float]]:
    """The server, listening at host ([server] listen) on port, not serving yet. proxies: whose forwarding headers count
    ([server] trusted_proxies); address: the address people use ([server] address); noted: told what a proxy set up
    wrong looks like (NOTED, Noted). CantListen when it can't listen there."""
    last = [time.monotonic()]
    reader.SERVED[0] = root  # the reader's cache is never kept inside it
    gate, watch = access.Gate(log=lambda m: _stamp(m)), bell.Watch(log=lambda m: _stamp(m), library=root)
    gate.on_event = watch.event  # logins and wrong passwords go to the alarms
    person_told = lambda name, what: (_stamp(f"What's kept for {name}: {what}"), watch.event("people", name=name, what=what))  # noqa: E731
    _open_database(watch, root)
    handler = type("LibraryHandler", (Handler,), {
        "root": root, "catalog": library.Catalog(root, say=lambda m: _stamp(m), failed=lambda e, what, said: tell_failure(e, what, said)),
        "last_request": last, "stats": dashboard.Stats(),
        "editors_secret": secrets.token_bytes(32), "server_key": secrets.token_urlsafe(24), "gate": gate, "watch": watch,
        "access_log": access.AccessLog(access_log, log=lambda m: _stamp(m)), "ereader": library.ereader_feed(), "proxy_header": proxy_header, "library_name": library_name(),
        "source_url": source_url(),
        "people": people.People(person_told), "personal_secret": secrets.token_bytes(32), "decisions": reports.Decisions(),
        "hosts": hosts or Hosts(), "proxies": tuple(proxies), "address": address, "noted": Noted(noted),
        "builds": periodic.Builds()})  # the catalog builds the Server page's removals want
    try:
        server = Server((host, port), handler)
    except OSError as e:
        raise CantListen(e.errno, e.strerror) from e
    server.proxies = tuple(proxies)
    return server, last


def cant_listen(e: OSError, listen: str, port: int) -> str:
    """Why the server couldn't listen, as the system said, with what to do: a port another program has is only
    one of the reasons. A service with walls may bind only the ports its unit names (SocketBindAllow)."""
    where = f"{listen} port {port}"
    other = f"Choose another with --port, or port in [server] of {paths.settings_file()}."
    if e.errno == errno.EADDRINUSE:
        return f"Port {port} is taken by another program ({e.strerror}). {other}"
    if e.errno in (errno.EACCES, errno.EPERM, getattr(errno, "WSAEACCES", errno.EACCES)) and port < 1024 and os.name != "nt":
        return (f"The system refused to let the server listen at {where} ({e.strerror}): a port below 1024 takes rights of "
                f"its own, which the server doesn't have, nor should. Choose one above it, with --port or port in [server] "
                f"of {paths.settings_file()}; 8770 as it comes.")
    if e.errno in (errno.EACCES, errno.EPERM, getattr(errno, "WSAEACCES", errno.EACCES)):
        return (f"The system refused to let the server listen at {where} ({e.strerror}). In a service with walls, the "
                f"unit's SocketBindAllow lines name the ports it may use (examples/hylly.service): {port} must be one of "
                f"them. On Linux a port below 1024 takes rights of its own; on Windows another program may hold the port, "
                f"or the system keep it back. {other}")
    if e.errno == errno.EADDRNOTAVAIL:
        return (f'listen = "{listen}" under [server] in {paths.settings_file()} isn\'t an address of this computer '
                f'({e.strerror}): "127.0.0.1", "0.0.0.0", "::" or one of its own.')
    if e.errno == errno.EAFNOSUPPORT:
        return (f'This computer takes no IPv6 here, so the server can\'t listen at {listen} ({e.strerror}): '
                f'listen = "0.0.0.0" under [server] in {paths.settings_file()} serves every IPv4 address.')
    return f"The server couldn't listen at {where}: {e.strerror or e}."


# ---- options, and the one running server ------------------------------------------------------

def options(typed: dict | None = None) -> tuple[int, str]:
    """Port and where it listens: what was typed for this one run in the foreground, else [server] in settings.toml, else
    8770 and this computer only (127.0.0.1). --lan, typed, is every IPv4 address ("0.0.0.0") for that run, and --no-lan
    this computer only. Nothing remembers what was typed: a restart, or a start at logon, takes the settings as they are
    then. A setting of the wrong kind stops it (settings.py)."""
    saved, typed = settings.get("server"), typed or {}
    lan = typed.get("lan")
    listen = saved["listen"] if lan is None else "0.0.0.0" if lan else "127.0.0.1"
    return int(typed["port"] if typed.get("port") is not None else saved["port"]), listen


def listens(info: dict) -> str | None:
    """Where the running server listens, from its note; one from before listen said only lan; None when unknown."""
    if info.get("listen"):
        return info["listen"]
    return {True: "0.0.0.0", False: "127.0.0.1"}.get(info.get("lan"))


def a_desktop() -> bool:
    """Whether a browser may be opened here: on Windows and macOS, or where a desktop is (DISPLAY or
    WAYLAND_DISPLAY set). Over ssh to a Linux server, Python's webbrowser would start a text browser in your session."""
    return instance.SYSTEM in ("windows", "mac") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def who_logs_in() -> tuple[str, str]:
    """Who can log in, for the server's start, hylly server status and the doctor: (the users and owners in words, and
    what to do, or "" when nothing needs doing). Read without changing anything (access.peek_accounts)."""
    try:
        accounts = access.peek_accounts()
    except (db.Newer, db.Unusable, sqlite3.Error, OSError) as e:  # then nobody can log in
        return f"the users can't be read ({e})", "hylly doctor says more"
    owners = access.owners(accounts)
    if not accounts:
        return "no users yet, so nobody can log in", "hylly user add NAME, on the server computer: the first user is the owner"
    said = f"{many(len(accounts), 'user')}, " + (
        f"{len(owners)} of them an owner" if len(owners) == 1 else f"{len(owners)} of them owners" if owners else "none of them an owner")
    return said, "" if owners else "hylly user role NAME owner, for the library's management"


def sign_in_links(token: str, port: int) -> list[str]:
    """A one-time sign-in link at each address the library has: the one other devices use (other_devices: [server]
    address, or this computer's own on the home network), the plain one at home too with address set (home_too), and
    this computer's. Where the running server listens, and the address it was started with; else as the settings say."""
    saved, running = settings.get("server"), instance.running() or {}
    listen = listens(running) or saved["listen"]
    address = running.get("address", saved["address"]) if running else saved["address"]
    found = [other_devices(listen, port, address), home_too(listen, port, address), here(listen, port)]
    return [f"{base}/login?once={token}" for base in dict.fromkeys(b for b in found if b)]


def in_a_terminal() -> bool:
    """Whether what's printed goes to a person at a terminal, not into a file: nohup, a service's or a container's log,
    a supervisor's, hylly serve > log. Only there may a sign-in link be shown: a file would keep a live one."""
    return getattr(sys.stdout, "isatty", lambda: False)()


LINK_ELSEWHERE = "hylly user link NAME, at a terminal, makes a one-time link that logs a browser in as NAME"


def owners_link(port: int, listen: str = "127.0.0.1") -> tuple[str | None, str]:
    """What hylly serve and hylly demo show, and open in a browser: (a one-time sign-in link on this computer for the
    library's owner, or None; what to say instead). The link only at a terminal (in_a_terminal), and only for the one
    owner; no link is even made otherwise, and the words say how to make one (LINK_ELSEWHERE). (None, "") with no
    users at all, which the start's Login line already says what to do about. listen: where the server listens (here)."""
    try:
        accounts = access.load_accounts()
        found = access.owners(accounts)
        if len(found) == 1 and in_a_terminal():
            return f"{here(listen, port)}/login?once={access.sign_in_link(found[0])}", ""
    except (db.Newer, db.Unusable, sqlite3.Error, OSError, SystemExit):  # no link then: the login page still works
        return None, ""
    return None, LINK_ELSEWHERE if accounts else ""


def describe(info: dict, stops: str = "") -> str:
    """The running server, as its start, hylly server status and hylly serve say it. stops: how this one stops, for its
    Stops line, when not as every server does (hylly demo's own: hylly server stop, typed elsewhere, reaches yours)."""
    if not info:
        return "  (it has not said where it listens yet)"
    port = info.get("port", PORT)
    listen = listens(info) or "127.0.0.1"
    others = other_devices(listen, port, info.get("address") or "")
    home = home_too(listen, port, info.get("address") or "")
    users, then = who_logs_in()
    how = f"in full, with {others.partition(':')[0]}://" if others else ""
    lines = [f"  Since:             {info.get('started', '?').replace('T', ' ')[:16]}  (process {info.get('pid', '?')})",
             f"  On this computer:  {here(listen, port)}/",
             *([f"  In a browser:      {others}/  (the library page, on another device)",
                "  Reading apps (OPDS):",
                f"    For tablets:     {others}/opds  (your feed; {how})",
                f"    For e-readers:   {others}/ereader/opds  (only books that suit an e-ink reader)"] if others else
               ["  Other devices:     " + (f"none: it listens on this computer only ({listen}; listen under [server], or --lan)"
                                          if ipaddress.ip_address(listen).is_loopback else
                                          "none just now: this computer is on no network")]),
             *([f"  Home network:      {home}/  (plain http, for a device that can't use the address above, such as "
                "an e-reader without Tailscale; its feed: /ereader/opds)"] if home else []),
             f"  Stops:             {stops or 'never by itself; hylly server stop stops it'}",
             f"  Login:             always: {users}",
             *([f"                     {then}"] if then else []),
             f"  Library:           {info.get('library', '?')}",
             f"  Messages:          {instance.log_file()}"]  # however it runs: it writes them there itself
    return "\n".join(lines)


def missing(root: Path) -> str | None:
    """What keeps the library page from showing: "library" when the folder isn't a Hylly library, or isn't there just
    now (a disk not mounted, a share not up yet: paths.library_missing), "catalog" when the catalog hasn't been built
    yet, None when the page is there. The server starts all the same, as a share may come up later; its start, hylly
    server status and the page's own address say what's missing and what to run (missing_line, Handler._not_ready)."""
    looking = "library"
    try:  # by stat itself, as is_dir and is_file are False on any trouble in newer Pythons
        if not stat.S_ISDIR(root.stat().st_mode) or paths.library_missing(root):
            return "library"
        looking = "catalog"
        (paths.catalog_dir(root) / "data.js").stat()  # what a build writes, and the server reads
    except OSError as e:
        if storage.share_is_gone(e):  # first: Windows calls a share gone away "not found" too, whatever was asked
            return "library"
        # not there; other trouble (no right to look, say) can't tell, so says nothing
        return looking if isinstance(e, (FileNotFoundError, NotADirectoryError)) else None
    return None


def missing_line(root: Path, told: bool) -> str:
    """What's missing (missing), for the start, server.log and hylly server status, with the command to run; '' when
    nothing is. told: the server was given its library with --library, so the command needs it too."""
    what = missing(root)
    if what == "library":
        return (f"No Hylly library at {root}: the library page says so until there is one. Is the disk or the share "
                f"there, and is the path right? The server shows the library once it's there; only for a new library: "
                f"hylly init {paths.typed(root)}")
    if what == "catalog":
        return (f"No catalog yet at {root}: the library page says so until there is one. Run hylly catalog"
                + (f" --library {paths.typed(root)}" if told else "") + ", then reload the page.")
    return ""


NOT_READY = {  # the page at the library page's address while missing says why: it names no folder, as anyone let in sees it
    "library": "The library isn't there just now: its disk or share may not be up yet, or the server was started on a "
               "folder that isn't a Hylly library. On the server computer, <code>hylly server status</code> says which "
               "folder it is, and <code>hylly init</code> makes a new library.",
    "catalog": "The library has no catalog yet. On the server computer, run <code>hylly catalog</code>, then reload this "
               "page.",
}


def not_ready_page(name: str, what: str) -> str:
    """The short page in place of the library page while missing says what (NOT_READY): no scripts, nothing fetched."""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(name)}</title>"
            "<style>:root { color-scheme: light dark; } body { max-width: 36em; margin: 12vh auto; padding: 0 16px; "
            'font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }</style></head>\n'
            f"<body><h1>{html.escape(name)}</h1><p>{NOT_READY[what]}</p></body></html>\n")


def status(info: dict | None | EllipsisType = ...) -> str:
    """hylly server status in words: the running server (its note, info, as the command read it; read here when not
    given), why one may not be, what's missing for the library page, and its start at logon."""
    info = instance.running() if info is ... else info
    text = "Library server: not running" if info is None else "Library server: running\n" + describe(info)
    if info is None and (last := access.tail(instance.log_file(), 1)):  # why it isn't, maybe: a start at logon has no window
        text += f"\n  Its last message ({instance.log_file()}):\n    {last[0]}"
    if info and info.get("library") and (note := missing_line(Path(info["library"]), "library" in (info.get("given") or {}))):
        text += f"\n{note}"
    logon = instance.autostart_state()
    if logon is not None:
        when = "Starts with the computer (a system-wide service)" if instance.system_service() else "Starts at logon"
        text += f"\n{when}: {'yes' if logon else 'no'}"
    return text


_failed_lock = threading.Lock()


def _failure_key(e: BaseException) -> tuple:
    """A kind of failure: the error's type and the place in the code it came from (each frame's file and line), never
    the text a request sent, which could make every one new. Hylly's own messages (Exit, the database's) count by their
    words too, so a mistake in the settings put right and another made is said again."""
    place = tuple((frame.f_code.co_filename, line) for frame, line in traceback.walk_tb(e.__traceback__))
    return (type(e).__name__, place, str(e) if isinstance(e, (SystemExit, db.Newer, db.NotHere)) else "")


def first_failure(e: BaseException) -> bool:
    """Whether this kind of failure (_failure_key) is new in server.log since the server started (Handler.FAILED). At
    most FAILED_MAX kinds: the last of them is a line saying so."""
    key = _failure_key(e)
    with _failed_lock:
        if key in Handler.FAILED or len(Handler.FAILED) >= Handler.FAILED_MAX:
            return False
        if len(Handler.FAILED) == Handler.FAILED_MAX - 1:
            Handler.FAILED.add(("full",))
            _stamp(f"{Handler.FAILED_MAX} kinds of failure in requests: any more aren't logged until the server restarts.")
            return False
        Handler.FAILED.add(key)
        Handler.FAILED_SAID[key] = [time.monotonic(), 0]
        return True


def failed_again(e: BaseException) -> int:
    """For a kind of failure server.log has said (first_failure), once Handler.FAILED_AGAIN has passed since it was last
    said: how many times it has come since, this one too, and the count starts again; 0 before then, and for a kind not
    remembered (FAILED_MAX). So a failure that goes on, a database another command keeps locked or a disk that stays
    full, shows in the log for as long as it lasts, an hour apart, not only as it began."""
    key = _failure_key(e)
    with _failed_lock:
        seen = Handler.FAILED_SAID.get(key)
        if seen is None or key not in Handler.FAILED:
            return 0
        seen[1] += 1
        if time.monotonic() - seen[0] < Handler.FAILED_AGAIN:
            return 0
        times, seen[:] = seen[1], [time.monotonic(), 0]
        return times


def tell_failure(e: BaseException, what: str, said: str) -> bool:
    """what, then said, in server.log: the first time this kind of failure comes (first_failure), and again once an hour
    has passed, if it came meanwhile, with how often (failed_again). True the first time."""
    if first_failure(e):
        _stamp(f"{what}: {said}")
        return True
    times = failed_again(e)
    if times:
        _stamp(f"{what}, {many(times, 'more time')} since it was last said: {said}")
    return False


def _trace(e: BaseException) -> str:
    """A failure's traceback for server.log, as Python writes it, every frame kept, but each error's own words cut to
    300 characters and their control characters made ?, as _stamp does: the words may be what a request sent, up to
    64 KB, and a line break in them could start a line that looks like the server's own."""
    told, seen = [], set()
    while e is not None and id(e) not in seen:  # the error, then the one it came from, and so on (a loop is cut)
        seen.add(id(e))
        try:
            words = str(e)
        except Exception:  # noqa: BLE001 - an error that can't say what it is: its frames still help
            words = "(its words couldn't be read)"
        words = storage.shown(words[:300])
        told.append("Traceback (most recent call last):\n" + "".join(traceback.format_tb(e.__traceback__))
                    + f"{type(e).__name__}{': ' + words if words else ''}\n")
        if e.__cause__ is not None:
            told.append("\nThe above exception was the direct cause of the following exception:\n\n")
            e = e.__cause__
        elif e.__context__ is not None and not e.__suppress_context__:
            told.append("\nDuring handling of the above exception, another exception occurred:\n\n")
            e = e.__context__
        else:
            e = None
    if e is not None:
        told.pop()  # the joining words of an error already told
    return "".join(reversed(told))


def _stamp(message: str) -> None:
    """A message of the server's, on standard error: in a terminal, the system's journal under a service, and in
    server.log with the time while the server runs (instance.logged). A name someone typed at the login page can't
    start a line of its own there, nor turn the text round: storage.shown."""
    sys.stderr.write(storage.shown(message) + "\n")  # in one piece: a line from another thread can't come between
    sys.stderr.flush()


def _interrupt(*_) -> None:
    raise KeyboardInterrupt


_start_again = threading.Event()


def _restart_asked(*_) -> None:
    """A server in a system-wide service: it ends with instance.RESTART_CODE, and the system starts it again."""
    _start_again.set()
    raise KeyboardInterrupt


# A rules folder or file that isn't there when the server starts, as on a share that comes up after the logon start
NOT_THERE_YET = ("The server starts all the same, and reads the rules once they are there; until then, what needs "
                 "them gets an error page.")


def run(root: Path, *, port: int = PORT, listen: str = "127.0.0.1", open_browser: bool = True, background: bool = False,
        given: dict | None = None, stops: str = "") -> None:
    """Serves until Ctrl-C or hylly server stop. listen: where, as options() has it ([server] listen, or --lan). given:
    what was typed for this one run (hylly serve, hylly demo), for what it says: nothing keeps it for a restart. stops:
    the words of its Stops line, when not every server's (describe). Its messages also go into server.log, however it
    runs (instance.logged). The shops' logins and the lookup sources' keys leave its environment first, however it was
    started (instance.logins_out): it needs none of them."""
    instance.logins_out()
    with instance.logged():
        _run(root, port=port, listen=listen, open_browser=open_browser, background=background, given=given or {},
             stops=stops)


def _run(root: Path, *, port: int, listen: str, open_browser: bool, background: bool, given: dict,
         stops: str = "") -> None:
    try:
        claim = instance.Claim()
    except instance.AlreadyRunning as e:
        # the system starting one while yours runs: all is well, and nothing to try again. But not for a service with walls:
        # the one running is outside them, and the system should show that this one failed
        if background and not instance.in_system_service():
            _stamp(str(e))
            raise Exit(OK) from None
        raise Exit(BUSY, str(e)) from None
    try:
        try:
            rules.WATCH = settings.WATCH = True  # a running server reads the rules and settings again as they change
            logged = access.settings()
            try:
                rules.current()  # so does a mistake in a rules file or in your choices; once running, the server
            except rules.Missing as e:  # keeps what it had, and tells the mistake once
                _stamp(f"{e} {NOT_THERE_YET}")  # a share that comes up after the logon start, say
            for section in settings.DEFAULTS:
                settings.get(section)
            hosts, saved = Hosts.from_settings(), settings.get("server")

            def noted(key: str, address: str, addresses: list[str]) -> None:  # a proxy set up wrong, maybe
                _stamp(NOTED[key].format(address=address))
                info[key] = addresses  # for hylly doctor
                claim.announce(**info)

            server, _ = make_server(root, listen, port, access_log=logged, proxy_header=access.proxy_header(), hosts=hosts,
                                    proxies=access.trusted_proxies(), address=saved["address"], noted=noted)
        except CantListen as e:  # only that: other trouble keeps its own words, and its file's name
            raise Exit(LOCAL, cant_listen(e, listen, port)) from None
        info = {"pid": os.getpid(), "port": port, "listen": listen, "address": saved["address"], "library": str(root),
                "started": db.now(), "began": time.time(),  # began: what a stop goes by
                "background": background, "given": given, "access_log": logged}
        claim.announce(**info)
        server.RequestHandlerClass.info = info
        threading.Thread(target=reader.disk_used, daemon=True, name="cache-count").start()  # counted now, not in a request
        lan_address()  # this computer's address at home, looked up now: no request waits on the adapters
        if background:  # one line in server.log; the command that started it shows the details
            alone = " (this computer only)" if ipaddress.ip_address(listen).is_loopback else ""
            _stamp(f"Library server started: port {port}, listening on {listen}{alone}, every request logs in "
                   f"(process {os.getpid()}).")
            link = None
        else:
            _stamp("Library server started (Ctrl-C stops it).")
            print(describe(info, stops), flush=True)
            link, words = owners_link(port, listen)  # at a terminal only: never into a file, and once, within minutes
            if link:
                print(f"  Log in:            {link}\n"
                      f"                     (as the owner, once, within {access.LINK_MINUTES} minutes)", flush=True)
            elif words:
                print(f"  Log in:            {words}", flush=True)
        if note := missing_line(root, "library" in given):  # a hidden one's in server.log, as the rules' is
            _stamp(note)
        users, then = who_logs_in()
        if then:  # no users, or no owner: one started at logon or by the system has only its log to say it
            _stamp(f"Logins: {users}. Next: {then}.")

        if note := reader.memory_note():
            _stamp(note)
        handler = server.RequestHandlerClass
        handler.watch.start()  # a word on a phone topic that's easy to guess
        jobs = periodic.start(periodic.Context(handler.root, _stamp, handler.watch))  # what's due, now and once a minute
        if os.name != "nt" and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _interrupt)  # hylly server stop: shut down cleanly
            if instance.in_system_service():
                signal.signal(signal.SIGUSR1, _restart_asked)
        if open_browser and a_desktop():
            webbrowser.open(link or f"{here(listen, port)}/")
        try:
            server.serve_forever(poll_interval=1.0)
        except KeyboardInterrupt:
            _stamp("Stopped.")
        finally:
            jobs.ended.set()  # no new round of its periodic jobs once it has stopped
            server.server_close()
    finally:
        claim.release()
    if _start_again.is_set():
        raise SystemExit(instance.RESTART_CODE)
