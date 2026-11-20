"""Every address the library server answers, asked by each kind of caller, every answer pinned; and who is asking, as
each of the server's rules works it out, in one table.

The walk: the made-up library with a little of everything in it (an owner's list featured in the reading apps, a
reader's own list, place, stars, review and report, a shelf for everyone in the apps, a tag, and a few files of other
kinds), served, and asked for every address the server answers: by nobody, by a browser with no login, by a borrower, a
reader, a librarian and the owner, by the owner's browser, by a reading app with its Basic login, here and there by one
with a wrong password, by a reader through a proxy (over https, from home, from outside), by the owner under a name the
server doesn't answer to and from another site's page; what needs a key, without it and with it; a file, a cover and the
library page's parts also by a range and by what the asker holds already. Each answer's status, the headers that matter
(its type, its caching, its packing, its policy, its cookies, where it leads, its range, the headers every answer
carries) and a fingerprint of its body are pinned in tests/routes.json, so that moving the server's parts around changes
none of them unseen. The addresses are the server's own: every row of its routes (Handler.GET, Handler.POST) is asked,
an address by itself and with the queries QUERIES gives it, a beginning (a row ending in *) by addresses under it; and
every address its code compares a request's path with, and every page of the feeds' table, must be among those asked
too (a test reads the code for them, and holds the reading to what it finds today), so that a new address can't be left
out of the walk. Who may ask is one rule, read off the routes' guards: without a login 401, logged in but not for your
role 403, and not found (404) only for what isn't there. After a change to what the server answers that is meant, and
reviewed, write the file again and commit it with the change:
    uv run python -m pytest tests/test_routes.py --new-snapshot

A fingerprint leaves out what differs between runs or computers: the server's keys, its port, the test's folders, the
process's number and the times, and the made-up files' sizes, as each computer's zlib packs their pictures its own way.
Hylly's own files (its icons, its fonts) are pinned by their bytes in full, a file of the library by being byte for byte
the file it holds (or the part a range asked for), and a page the reader draws (by this computer's Pillow) by its kind
and size. The server page's status, which changes by the second, is pinned by its keys alone; whether an answer that
names the test's folders goes packed (over 1024 bytes) hangs on their length, and isn't pinned. And an error page
Python's http.server writes itself, not Hylly (send_error: the 501 for a method the server doesn't take, or a request it
can't read), by its status and type alone: each version of Python words it its own way, and the tests run on several."""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import inspect
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, replace
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
from PIL import Image

from hylly import access, library, opds, people, reader, serve
from hylly.serve import OPEN

from helpers import EVERY_ROLE, WINDOWS_FIREFOX, make_epub, real_mobi, with_settings

ROUTES = Path(__file__).with_name("routes.json")
PASSWORD = "correct horse"  # every made-up user's (conftest.PASSWORD)

APP = {"User-Agent": "ReadingApp/1.0", "Accept": "*/*"}  # a reading app, as the server sees one: no browser's headers
BROWSER = {"User-Agent": WINDOWS_FIREFOX, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",  # opening a page, on
           "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document"}  # this computer
CALLERS = {  # who asks: (the user, how they log in, the headers their requests have)
    "nobody": (None, None, APP),
    "a browser, no login": (None, None, BROWSER),
    "a borrower": ("bea", "session", APP),
    "a reader": ("rory", "session", APP),
    "a librarian": ("lucy", "session", APP),
    "the owner": ("owen", "session", APP),
    "the owner's browser": ("owen", "session", BROWSER),
    "a reading app": ("rory", "basic", {**APP, "Accept": "application/atom+xml;profile=opds-catalog, */*"}),
    "a wrong password": ("rory", "wrong", APP),  # a few asks only: ten wrong logins lock this computer's address out
    # through a proxy (this computer's own, trusted as it comes), which names the sender and how the browser came
    "a reader through an https proxy": ("rory", "session", {**APP, "Host": "testpc.tail1234.ts.net",
                                                            "X-Forwarded-For": "203.0.113.9",
                                                            "X-Forwarded-Proto": "https"}),
    "a reader at home through a proxy": ("rory", "session", {**APP, "Host": "192.168.1.5:8770",
                                                             "X-Forwarded-For": "192.168.1.30"}),
    "a reader outside through a proxy": ("rory", "session", {**APP, "Host": "testpc.tail1234.ts.net",
                                                             "X-Forwarded-For": "198.51.100.7"}),
    "the owner, by a name not its own": ("owen", "session", {**APP, "Host": "rebind.example:8770"}),
    "the owner, from another site's page": ("owen", "session", {**APP, "Sec-Fetch-Site": "cross-site",
                                                                "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
                                                                "Origin": "https://elsewhere.example"}),
}
EVERYONE = tuple(c for c in CALLERS if c != "a wrong password")
KEYED = ("a borrower", "a reader", "a librarian", "the owner")  # what needs a key, they also send with their own keys
NOVEL = "/humble/Humble%20Book%20Bundle%20-%20Fantasy%20by%20Lantern%20Press/The%20Novel/novel.epub"
COVER = "/catalog/covers/c3e07f37bffaff4e.webp"
KEPT = COVER + "?v={v}"  # by the version its file has, as the library page's data names it (Walk.send fills it in)


@dataclass(frozen=True)
class Ask:
    """One address asked: by method, with a form or a JSON body ({} unless form), and by whom."""
    method: str
    address: str
    said: str = ""  # what tells two asks of one address apart
    form: dict | None = None
    keyed: bool = False  # sent again with the keys, by each of KEYED
    callers: tuple[str, ...] = EVERYONE
    extra: tuple[tuple[str, str], ...] = ()  # headers of its own; {etag}: the ETag this address gave this caller


def program_files() -> list[str]:
    """The reader's program files (Handler.PROGRAM_FILES): one of each kind each folder serves, a versioned one, one of
    a kind it doesn't serve, and one that isn't there."""
    out = []
    for prefix, (folder, kinds) in serve.Handler.PROGRAM_FILES.items():
        there = sorted(p.name for p in Path(serve.__file__).with_name(folder).iterdir() if p.is_file())
        out += [prefix + next(n for n in there if n.endswith(kind)) for kind in kinds]
        out += [prefix + next(n for n in there if not n.endswith(tuple(kinds))), f"{prefix}nonesuch{next(iter(kinds))}"]
        if prefix == "/foliate/":
            out.append(prefix + "0123abc/" + next(n for n in there if n.endswith(".js")))
    return out


FEED_PAGES = ("", "/", "/opensearch.xml", "/opensearchXxml", "/search?q=lantern", "/search", "/best", "/popular",
              "/added", "/added?page=1", "/new", "/kind", "/game", "/genre", "/tag", "/src/humble",
              "/src/comics?letter=N", "/kind/comic", "/kind/comic/more", "/kind/book?all=1", "/kind/book?page=abc",
              "/genre/Science%20Fiction", "/tag/Favourite", "/game/Traveller", "/series", "/series?letter=D",
              "/series?all=1", "/series/lanternwolf", "/series/nonesuch", "/list/{list}", "/list/1-nonesuch",
              "/continue", "/reading", "/mine/rainy", "/mine/nonesuch", "/shelf/comics", "/shelf/comics/added",
              "/shelf/comics/search?q=lantern", "/shelf/nonesuch", "/nonesuch")
YOURS = ("", "/opensearch.xml", "/continue", "/mine/rainy", "/shelf/comics")  # under /me: the same pages, the same code
# How the walk asks a row of the routes where the address by itself isn't enough: with the queries (a title's, a page's,
# a link's, where to go next), each an ask of its own ("" the address by itself)
QUERIES = {
    "/login": ("", "?next=%2Fopds", "?once=" + "x" * 43),
    "/": ("", "?next=%2Fopds%2Fnew"),
    "/database/table": ("?name=edit",),
    "/api/pages": ("?id=h:lwolf1", "?id=nonesuch"),
    "/api/page": ("?id=h:lwolf1&n=0&w=200",),
    "/api/ahead": ("?id=h:lwolf1&n=0&w=200&k=0&d=1&t=abcdef12", "?id=h:lwolf1&d=2"),
    "/api/me/places": ("", "?id=h:lwolf1"),
    "/api/reviews": ("?id=h:novel",),
    "/api/questions": ("?id=h:novel",),
    "/api/edits": ("", "?n=1"),
}
BY_EVERY_CALLER = {"/api/session"}  # a wrong password too (and among the feeds, /opds)
KEYS = {"_personal", "_library_key", "_server_key"}  # the guards that take a key: their rows are asked with keys too
NOT_WITH_KEYS = {"/server/stop", "/server/restart"}  # with its key the owner's stops the server
ROLE = {"a borrower": "borrower", "a reader": "reader", "a librarian": "librarian", "the owner": "owner",
        "the owner's browser": "owner", "a reading app": "reader", "a reader through an https proxy": "reader",
        "a reader at home through a proxy": "reader", "a reader outside through a proxy": "reader"}


def steps(row: tuple) -> tuple:
    """A row's guards, one or several, as a tuple."""
    return row[0] if isinstance(row[0], tuple) else (row[0],)


def rows(table: dict) -> tuple[list[str], list[str]]:
    """A table's addresses, and its beginnings (each without its *), in its order."""
    return [a for a in table if not a.endswith("*")], [a[:-1] for a in table if a.endswith("*")]


def keyed(table: dict, address: str) -> bool:
    """Asked with keys too: a row whose guards take a key, or an address no row has under a beginning whose rows do."""
    if address in table:
        return any(g.check in KEYS for g in steps(table[address])) and address not in NOT_WITH_KEYS
    return any(keyed(table, a) for a in rows(table)[0] if a.startswith(address.removesuffix("nonesuch")))


def asks() -> list[Ask]:
    """Every address asked, in this order: the routes' own addresses (Handler.GET), by themselves and with their
    queries (QUERIES), and under each beginning that has nothing else under it an address that isn't there; the
    licences and the reader's program files; the library page's own files and the library's; the feeds; HEAD and the
    methods the server doesn't take; then the posts (Handler.POST), logging in first and out last."""
    h = serve.Handler
    out = []
    for address, row in h.GET.items():
        if address.endswith("*"):
            if row[1] is h._not_there:  # an owner's or the page's: nothing else there
                out.append(Ask("GET", address[:-1] + "nonesuch", keyed=keyed(h.GET, address[:-1] + "nonesuch")))
            continue
        out += [Ask("GET", address + query, keyed=keyed(h.GET, address),
                    callers=tuple(CALLERS) if address in BY_EVERY_CALLER else EVERYONE)
                for query in QUERIES.get(address, ("",))]
    out.append(Ask("GET", "/database/query", keyed=True))  # a post's address, asked by GET
    out += [Ask("GET", serve.LICENCE_PATH + rel) for rel in (*serve.LICENCES, "nonesuch.txt")]
    out += [Ask("GET", a) for a in program_files()]
    out += [Ask("GET", "/" + rel) for rel in sorted(serve.OWN_FILES)]
    out += [Ask("GET", a) for a in (
        "/CATALOG/Index.html", "/catalog", "/catalog/cache/files.jsonl", COVER, KEPT, COVER + "?v=1",
        "/catalog/covers/nonesuch.webp", "/catalog/covers/nonesuch.webp?v=1", NOVEL, NOVEL + "?dl=1",
        "/comics/Neon%20Comics/Neon%20Rangers%20002.cbz",
        "/manuals/PC/Wasteland%20Rover/Manual.pdf", "/extras/moonwake.mobi", "/extras/pack.zip",
        "/extras/pack.zip/inner.epub", "/extras/pack.zip/nonesuch.epub", "/extras/notes.txt", "/extras/page.html",
        "/extras/script.js", "/extras/picture.svg", "/extras/.hidden/notes.txt", "/_own/notes.txt",
        "/state/database.json", "/curation/notes.txt", "/humble", "/nonesuch")]
    for base in h.FEED_PATHS:
        pages = YOURS if base.startswith("/me/") else FEED_PAGES
        out += [Ask("GET", base + page, callers=tuple(CALLERS) if base + page == "/opds" else EVERYONE)
                for page in pages]
    out += [Ask("GET", a) for a in ("/%65reader/opds", "/opdsx", "/me/")]
    for a in (NOVEL, COVER, KEPT, "/extras/pack.zip/inner.epub", "/catalog/index.html", "/catalog/data.js"):
        # a file of the library, a cover (by its version too), a file in a ZIP package, and the page's own parts
        out += [Ask("GET", a, "bytes 0-9", extra=(("Range", "bytes=0-9"),)),
                Ask("GET", a, "a range past its end", extra=(("Range", "bytes=1000000000-"),)),
                Ask("GET", a, "the ETag it gave", extra=(("If-None-Match", "{etag}"),)),
                Ask("GET", a, "not changed since", extra=(("If-Modified-Since", "Fri, 01 Jan 2100 00:00:00 GMT"),))]
    out.append(Ask("GET", "/catalog/index.html", "its last 5 bytes", extra=(("Range", "bytes=-5"),)))
    out += [Ask("HEAD", a) for a in ("/opds", "/catalog/index.html", "/catalog/data.js", NOVEL,
                                     "/extras/pack.zip/inner.epub", "/foliate/view.js", "/api/session")]
    out += [Ask(m, "/", callers=("nobody", "the owner")) for m in ("PUT", "OPTIONS")]
    owners, gone = {"username": "owen", "password": PASSWORD, "next": "/opds"}, {"once": "x" * 43, "next": "/"}
    out += [Ask("POST", "/login", "an empty form", form={}, callers=("nobody", "a browser, no login")),
            Ask("POST", "/login", "the owner's password", form=owners,
                callers=("nobody", "a reader through an https proxy")),
            Ask("POST", "/login", "a link that doesn't work", form=gone, callers=("nobody",))]
    out += [Ask("POST", a, keyed=keyed(h.POST, a)) for a, row in h.POST.items() if row[0] is not OPEN]
    out += [Ask("POST", a, keyed=True) for a in ("/server/nonesuch", "/api/nonesuch", "/nonesuch")]
    out.append(Ask("POST", "/logout"))  # last: it ends each caller's login
    return out


# ---- the library, the server and the walk ----------------------------------------------------------------------------

@pytest.fixture
def users():
    return EVERY_ROLE  # bea a borrower, rory a reader, lucy a librarian, owen the owner


class Walk:
    """The server, each caller's login, and how to ask it as each of them."""

    def __init__(self, srv, root: Path) -> None:
        self.srv, self.root, self.handler = srv, root, srv.RequestHandlerClass
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", timeout=60)
        accounts = access.load_accounts()
        self.tokens = {c: access.Sessions().start(user, accounts[user][1]) for c, (user, login, _) in CALLERS.items()
                       if login == "session"}

    def personal_key(self, user: str) -> str:
        return hmac.new(self.handler.personal_secret, user.encode("utf-8"), hashlib.sha256).hexdigest()

    def editors_key(self, user: str) -> str:
        return hmac.new(self.handler.editors_secret, user.encode("utf-8"), hashlib.sha256).hexdigest()

    def headers(self, caller: str, address: str, keyed: bool = False) -> dict:
        user, login, own = CALLERS[caller]
        out = {**own, "Accept-Encoding": "gzip"}
        if login == "session":  # over HTTPS by the name only HTTPS can set, as a browser would have it
            name = access.COOKIE_HTTPS if own.get("X-Forwarded-Proto") == "https" else access.COOKIE
            out["Cookie"] = f"{name}={self.tokens[caller]}"
        elif login:
            word = PASSWORD if login == "basic" else "wrong horse"
            out["Authorization"] = "Basic " + base64.b64encode(f"{user}:{word}".encode()).decode()
        if keyed:
            owners = urlsplit(address).path.startswith(("/server", "/database"))
            out["X-Server-Key"] = self.handler.server_key if owners else self.editors_key(user)  # each editor's own
            out["X-Personal-Key"] = self.personal_key(user)
        return out

    def send(self, caller: str, ask: Ask, keyed: bool = False, body=None) -> httpx.Response:
        if "{v}" in ask.address:  # a cover by the version its file has now, which differs from one run to the next
            cover = self.root.joinpath(*unquote(urlsplit(ask.address).path).lstrip("/").split("/"))
            ask = replace(ask, address=ask.address.replace("{v}", library.cover_version(cover.stat().st_mtime_ns)))
        headers = self.headers(caller, ask.address, keyed)
        for name, value in ask.extra:
            if "{etag}" in value:  # the ETag this address gives this caller, asked for first; with none, a made-up one
                tag = self.send(caller, Ask("GET", ask.address)).headers.get("etag", '"none"')
                value = value.replace("{etag}", tag)
            headers[name] = value
        if ask.form is not None:
            content = "&".join(f"{k}={quote(v)}" for k, v in ask.form.items()).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif ask.method == "POST":
            content = json.dumps({} if body is None else body).encode()
            headers["Content-Type"] = "application/json"
        else:
            content = None
        try:
            return self.client.request(ask.method, ask.address, headers=headers, content=content)
        finally:
            self.client.cookies.clear()  # each caller's login is its own: none kept from an answer

    def setup(self, caller: str, address: str, body: dict) -> dict:
        """A change as the library page makes it, with the caller's keys: it must be taken."""
        answer = self.send(caller, Ask("POST", address), keyed=True, body=body)
        assert answer.status_code == 200, (address, body, answer.text)
        return answer.json()


def a_little_of_everything(walk: Walk) -> None:
    """What the feeds and the page's questions show besides the catalog, made as the library page makes it: the owner's
    list, featured in the reading apps; a reader's place, reading list, own list in the apps, stars, review and report;
    the owner's place in the same title, so that it is Most read, and stars; a shelf for everyone in the apps, by a
    librarian; and a tag."""
    for body in ({"list": "fav", "op": "name", "name": "Favourites"}, {"list": "fav", "op": "add", "id": "h:novel"},
                 {"list": "fav", "op": "add", "id": "s:7"}, {"list": "fav", "op": "apps", "on": True},
                 {"list": "fav", "op": "feature", "on": True}):
        walk.setup("the owner", "/api/me/list", body)
    for body in ({"list": "rainy", "op": "name", "name": "Rainy days"}, {"list": "rainy", "op": "add", "id": "h:pss"},
                 {"list": "rainy", "op": "apps", "on": True}, {"list": "reading", "op": "add", "id": "f:44883458b699"}):
        walk.setup("a reader", "/api/me/list", body)
    for caller in ("a reader", "the owner"):
        walk.setup(caller, "/api/me/place", {"id": "h:lwolf1", "at": 1, "pct": 0.5})
        walk.setup(caller, "/api/me/stars", {"id": "h:novel", "stars": 4 if caller == "a reader" else 5})
    walk.setup("a reader", "/api/me/review", {"id": "h:novel", "stars": 4, "text": "A good read.", "spoiler": False})
    walk.setup("a reader", "/api/me/report", {"id": "h:pss", "reasons": ["quality"]})
    walk.setup("a librarian", "/api/shelves", {"op": "set", "shelf": {"id": "comics", "name": "Comics only",
                                                                       "show": {"k": ["comic"]}, "apps": True}})
    walk.setup("the owner", "/api/tags", {"op": "add", "tag": "Favourite", "ids": ["h:novel"]})


def files_of_other_kinds(root: Path) -> None:
    """Files of the library the catalog doesn't list, of kinds a library may hold: a MOBI, a ZIP package with a book in
    it, text, a page, a script and a picture a browser could run; and in places never served: a hidden folder, one
    starting with _, and the library's private folders."""
    extras = root / "extras"
    (extras / ".hidden").mkdir(parents=True)
    (extras / "moonwake.mobi").write_bytes(real_mobi())
    make_epub(extras / "inner.epub")
    with zipfile.ZipFile(extras / "pack.zip", "w") as z:
        z.write(extras / "inner.epub", "inner.epub")
    (extras / "inner.epub").unlink()
    for name, text in (("notes.txt", "Notes."), ("page.html", "<p>A page.</p>"), ("script.js", "let a = 1;"),
                       ("picture.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>'), (".hidden/notes.txt", "Hidden.")):
        (extras / name).write_text(text, encoding="utf-8")
    for folder in ("_own", "curation"):
        (root / folder).mkdir(exist_ok=True)
        (root / folder / "notes.txt").write_text("Kept.", encoding="utf-8")


# ---- what an answer is pinned by -------------------------------------------------------------------------------------

PINNED = {"content-type": "type", "cache-control": "cache", "location": "to", "www-authenticate": "asks",  # by a
          "content-disposition": "saved", "retry-after": "retry", "accept-ranges": "ranges",  # short name
          "x-frame-options": "frames", "cross-origin-opener-policy": "opener", "content-encoding": "packed",
          "vary": "vary", "content-range": "range"}
# Every answer's, as a rule: pinned where an answer has another, or none ("-")
USUAL = {"x-content-type-options": "nosniff", "cross-origin-resource-policy": "same-origin",
         "referrer-policy": "same-origin"}
TEXT = re.compile(r"text/|application/(json|javascript|manifest\+json|atom\+xml|opensearchdescription\+xml|xml)"
                  r"|image/svg")
TIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?([+-]\d\d:\d\d|Z)?")
CHANGING = {"time", "when", "newest", "v"}  # a JSON answer's values that are the moment's: a place's time, a file's
WHOLE_SECOND_BY_SECOND = {"/server/status"}  # what changes by the second: by its keys alone


class Masks:
    """What a fingerprint leaves out of a body (the module's docstring)."""

    def __init__(self, walk: Walk, folders: list[Path]) -> None:
        h, self.root = walk.handler, walk.root
        self.folders = sorted({str(f) for folder in folders for f in (folder, folder.resolve())}, key=len, reverse=True)
        self.words = [(h.server_key, "{server-key}"), *((walk.editors_key(user), "{key}") for user in EVERY_ROLE),
                      *((walk.personal_key(user), "{personal-key}") for user in EVERY_ROLE),
                      *((f, "{folder}") for f in self.folders),
                      (f'"{os.getpid()} ', '"{pid} '), (f":{walk.srv.server_port}", ":{port}")]

    def text(self, text: str) -> str:
        for word, put in self.words:
            text = text.replace(word, put)
        # a cover's version, its file's time: in the page's data, and in where a browser with no login is sent on to
        text = re.sub(r"(covers(?:/|%2F)[0-9a-f]{16}\.webp)(\?v=|%3Fv%3D)\d+", r"\1\2{v}", text)
        return re.sub(r'length="\d+"', 'length="#"', TIME.sub("{time}", text))

    def json(self, value):
        """A JSON value, the moment's values and a file's size ([path, kind, size]) left out."""
        if isinstance(value, dict):
            return {k: "{now}" if k in CHANGING else self.json(v) for k, v in value.items()}
        if isinstance(value, list):
            if len(value) == 3 and all(isinstance(v, str) for v in value[:2]) and isinstance(value[2], (int, float)):
                return [*value[:2], "{size}"]
            return [self.json(v) for v in value]
        return self.text(value) if isinstance(value, str) else value


def pythons_own(answer: httpx.Response) -> bool:
    """Whether the answer is the error page Python's http.server writes itself (send_error), whatever this version of
    Python words it: its type, and its format with the status filled in, whatever the message and its explanation."""
    parts = re.split(r"%\((\w+)\)[a-z]", BaseHTTPRequestHandler.error_message_format)  # its text, and its fields' names
    form = "".join(re.escape(part) if n % 2 == 0 else str(answer.status_code) if part == "code" else ".*"
                   for n, part in enumerate(parts))
    return (answer.headers.get("content-type") == BaseHTTPRequestHandler.error_content_type
            and re.fullmatch(form, answer.text, re.S) is not None)


def on_disk(root: Path, path: str) -> bytes | None:
    """What the library holds at a path asked for: the file, or the file inside a ZIP package the path names; None
    when it holds neither."""
    rel = path.lstrip("/")
    target, packed = root.joinpath(*rel.split("/")), reader.PACKED.fullmatch(rel)
    if target.is_file():
        return target.read_bytes()
    if packed and root.joinpath(*packed[1].split("/")).is_file():
        with zipfile.ZipFile(root.joinpath(*packed[1].split("/"))) as z:
            return z.read(packed[2]) if packed[2] in z.namelist() else None
    return None


def body_of(answer: httpx.Response, masks: Masks, path: str) -> str:
    data, ctype = answer.content, answer.headers.get("content-type", "")
    if not data:
        return "-"
    if pythons_own(answer):
        return "Python's own error page"
    if not TEXT.match(ctype):  # Hylly's own by its bytes in full; the library's held to the file it holds
        if path in serve.Handler.APP_FILES or path.startswith(tuple(serve.Handler.PROGRAM_FILES)):
            return "Hylly's own, " + hashlib.sha256(data).hexdigest()[:12]
        held = on_disk(masks.root, path)
        if held is not None:
            part = re.fullmatch(r"bytes (\d+)-(\d+)/\d+", answer.headers.get("content-range", ""))
            if data == (held[int(part[1]):int(part[2]) + 1] if part else held):
                return f"bytes {part[1]}-{part[2]} of the file" if part else "the file"
            return f"not the file: {len(data)} bytes, {hashlib.sha256(data).hexdigest()[:12]}"
    if ctype.startswith("image/") and "svg" not in ctype:  # drawn by this computer's Pillow: its kind and size
        with Image.open(io.BytesIO(data)) as picture:
            return f"{picture.format} {picture.size[0]}x{picture.size[1]}"
    if not TEXT.match(ctype):
        return f"bytes {data[:4].hex()}"
    text = data.decode("utf-8", "replace")
    js = text.startswith("window.CATALOG = ")
    if js or ctype.startswith("application/json") or ctype.startswith("application/manifest+json"):
        value = json.loads(text[text.index("{"): text.rindex("}") + 1] if js else text)
        if path in WHOLE_SECOND_BY_SECOND:
            return "keys " + ",".join(sorted(value))
        text = json.dumps(masks.json(value), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(masks.text(text).encode("utf-8")).hexdigest()[:12]


def answer_of(answer: httpx.Response, masks: Masks, path: str) -> dict:
    headers = answer.headers
    out = {"status": answer.status_code}
    out |= {short: masks.text(headers[name]) for name, short in PINNED.items() if name in headers}
    if "range" in out:  # the file's size: its pictures are packed by each computer's zlib
        out["range"] = re.sub(r"/\d+$", "/#", out["range"])
    out |= {name: headers.get(name, "-") for name, usual in USUAL.items() if headers.get(name) != usual}
    if any(f.encode() in answer.content for f in masks.folders):  # its length, and so whether it goes packed, is the
        out.pop("packed", None), out.pop("vary", None)  # length of this computer's folders: unpinned
    if "content-security-policy" in headers:  # its scripts by their hashes, as many as there are: the body pins them
        policy = re.sub(r"'sha256-[^']*'", "'sha256'", headers["content-security-policy"])
        out["policy"] = hashlib.sha256(policy.encode()).hexdigest()[:8]
    cookies = [re.sub(r"^([^=;]+)=[^;]+", r"\1={token}", c) for c in headers.get_list("set-cookie")]  # its value alone
    if cookies:
        out["cookies"] = cookies
    if "etag" in headers:
        out["etag"] = True
    out["body"] = body_of(answer, masks, path)
    return out


def walked(walk: Walk, folders: list[Path]) -> dict[str, dict]:
    """Every ask (asks), by each of its callers, and with keys by each of KEYED where it's keyed: each answer by its
    key, METHOD ADDRESS [what tells it apart] | caller."""
    masks, out = Masks(walk, folders), {}
    person = next(c["person"] for c in people.in_the_apps() if c["id"] == "fav")
    for ask in asks():
        ask = replace(ask, address=ask.address.replace("{list}", f"{person}-fav"))
        path = unquote(urlsplit(ask.address).path)
        for keyed in (False, True) if ask.keyed else (False,):
            for caller in KEYED if keyed else ask.callers:
                said = ", ".join(s for s in (ask.said, "with keys" if keyed else "") if s)
                key = f"{ask.method} {ask.address}{f' [{said}]' if said else ''} | {caller}"
                out[key] = answer_of(walk.send(caller, ask, keyed), masks, path)
    return out


def lines(answers: dict[str, dict]) -> str:
    """The answers as tests/routes.json keeps them: one answer to a line, in the order asked."""
    return "{\n" + ",\n".join(f"{json.dumps(k)}: {json.dumps(v, sort_keys=True)}" for k, v in answers.items()) + "\n}\n"


def test_every_address_answers_each_caller_as_pinned(library, serving, request, tmp_path_factory):
    """The walk (the module's docstring), against tests/routes.json. An address written with an escape is the same
    address: the feeds' too, which pick the e-reader's feed by the address decoded, as its page is read."""
    with_settings('[reviews]\nwho = "reader"\n')  # stars and words on, for Best rated
    files_of_other_kinds(library)
    srv, _ = serving(library, login=None)
    walk = Walk(srv, library)
    try:
        a_little_of_everything(walk)
        # this test's own folder is numbered by how many tests ran before it in the same run (conftest's own_folders):
        # masked whole, so the settings' and the database's paths read the same whatever ran first
        own = Path(os.environ["HYLLY_SETTINGS"]).parent.parent
        answers = walked(walk, [own, tmp_path_factory.getbasetemp()])
    finally:
        walk.client.close()
    assert {c: answers[f"GET /%65reader/opds | {c}"]["status"] for c in EVERYONE} == {
        c: answers[f"GET /ereader/opds | {c}"]["status"] for c in EVERYONE}
    now = lines(answers)
    if request.config.getoption("--new-snapshot"):
        ROUTES.write_text(now, encoding="utf-8")
    pinned = json.loads(ROUTES.read_text(encoding="utf-8"))
    differ = [f"{key}: {pinned.get(key)} -> {answers.get(key)}" for key in dict.fromkeys([*pinned, *answers])
              if pinned.get(key) != answers.get(key)]
    assert not differ, (f"{len(differ)} of {len(answers)} answers aren't as tests/routes.json has them (was -> now). "
                        "If the change is meant, write it again and commit it with the change: uv run python -m "
                        "pytest tests/test_routes.py --new-snapshot.\n" + "\n".join(differ[:20]))


# ---- the walk leaves no address out ----------------------------------------------------------------------------------

def strings(node: ast.AST, module, handler, local: dict[str, list[str]] | None = None) -> list[str]:
    """The text a compared value holds: written there (a dict's by its keys), or what a name it gives holds: a constant
    the module's code gives that name (local: an assignment, or what a loop takes it through), the module's own, or the
    Handler's."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [s for n in node.elts for s in strings(n, module, handler, local)]
    if isinstance(node, ast.Dict):
        return [s for k in node.keys if k is not None for s in strings(k, module, handler, local)]
    if isinstance(node, ast.Starred):
        return strings(node.value, module, handler, local)
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("tuple", "set") and node.args:
        return strings(node.args[0], module, handler, local)
    if isinstance(node, ast.Name) and node.id in (local or {}):
        return local[node.id]
    own = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
    named = (getattr(module, node.id, None) if isinstance(node, ast.Name) else getattr(handler, node.attr, None) if own
             else None)
    if isinstance(named, str):
        return [named]
    return [s for s in named if isinstance(s, str)] if isinstance(named, (tuple, list, set, frozenset, dict)) else []


def compared(module, handler=serve.Handler) -> dict[str, set[str]]:
    """What the module's code compares a request's path with: whole addresses (==, in, a dict's get), their beginnings
    and endings, a feed's own pages under its address, and the patterns a feed's page is matched by. A path is what is
    named path or prefix, or read as .path (or ._path, the path as the server routed it); a feed's page is rest; a name
    made from a path without its beginning (removeprefix) has it put back; a name the code gives a constant, or takes
    through constants in a loop, is read as them."""
    tree = ast.parse(inspect.getsource(module))
    found: dict[str, set[str]] = {"whole": set(), "starts": set(), "ends": set(), "pages": set(), "patterns": set()}
    before: dict[str, str] = {}
    local: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, "attr", None) == "removeprefix":
                held = strings(value.args[0], module, handler) if value.args else []
                before.update({node.targets[0].id: s for s in held if s.startswith("/")})
            elif held := strings(value, module, handler):
                local.setdefault(node.targets[0].id, []).extend(held)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            local.setdefault(node.target.id, []).extend(strings(node.iter, module, handler))

    def kind(value: ast.AST) -> str | None:
        if isinstance(value, ast.Name):
            named = {"rest": "rest", "prefix": "start", "path": "path"}
            return named.get(value.id, "path" if value.id in before else None)
        return "path" if isinstance(value, ast.Attribute) and value.attr in ("path", "_path") else None

    def add(value: ast.AST, held: list[str], into: str) -> None:
        put = before.get(value.id, "") if isinstance(value, ast.Name) else ""
        for s in held:
            if kind(value) == "rest" and s.startswith("/"):
                found["pages"].add(s)
            elif kind(value) == "start" and s.startswith("/"):
                found["starts"].add(s)  # a beginning, whatever it's compared by
            elif kind(value) == "path" and (put or s).startswith("/" if into != "ends" else ""):
                found[into].add(put + s)

    equal = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and all(isinstance(op, equal) for op in node.ops):
            for right in node.comparators:
                add(node.left, strings(right, module, handler, local), "whole")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            on_rest = len(node.args) > 1 and "rest" in {getattr(n, "id", None) for n in ast.walk(node.args[1])}
            if node.func.attr in ("startswith", "endswith"):
                held = strings(node.args[0], module, handler, local)
                add(node.func.value, held, node.func.attr.replace("swith", "s"))
            elif node.func.attr == "get" and kind(node.args[0]):  # {address: ...}.get(path)
                add(node.args[0], strings(node.func.value, module, handler, local), "whole")
            elif (node.func.attr in ("match", "fullmatch") and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "re" and on_rest):
                found["patterns"].update(s for s in strings(node.args[0], module, handler, local) if s.startswith("/"))
    return found


# What the code reader finds in serve.py and opds.py today: each must stay found, so that a change to the code that the
# reader no longer follows (a lost address) fails here rather than going unwalked. The routes' own addresses are their
# tables' rows, which the walk asks as they are (test_the_walk_asks_every_row_of_the_routes): what's left to find is
# what the server's rules compare a path with besides (the login's, the log's, the reader's files', the feeds' own)
FOUND_TODAY = {
    "whole": ("/healthz", *(serve.LICENCE_PATH + rel for rel in serve.LICENCES), "/login", "/logout", "/ping",
              "/server/status"),
    "starts": ("/api/", "/ereader/", "/ereader/opds", "/foliate/", "/fonts/", "/me/", "/me/ereader/", "/opds", "/ping"),
}


def beginning_of(table: dict, path: str) -> str | None:
    """The beginning of a table's (a row ending in *, without it) that takes a path no row of its own has: the longest
    that fits, as the dispatcher has it (Handler._route)."""
    fits = [b for b in rows(table)[1] if path.startswith(b)]
    return max(fits, key=len) if fits else None


def test_the_walk_asks_every_row_of_the_routes():
    """Every row of the server's routes (Handler.GET, Handler.POST) is asked: an address by its own method, and a
    beginning by an address it takes, one no row has, so that a new route can't be left out of the walk however its
    handler is written."""
    h = serve.Handler
    for method, table in (("GET", h.GET), ("POST", h.POST)):
        asked = {unquote(urlsplit(ask.address).path) for ask in asks() if ask.method == method}
        addresses, beginnings = rows(table)
        assert [a for a in addresses if a not in asked] == [], method
        taken = {beginning_of(table, path) for path in asked if path not in table}
        assert [b for b in beginnings if b not in taken] == [], method


def test_not_for_your_role_is_403_and_only_what_isn_t_there_is_404():
    """One rule for who may ask, read off the routes' guards and held to every answer pinned: every address but an
    OPEN row's needs a login (401; a browser opening a page goes to the login page instead); logged in, a row whose
    guards want a higher role than the caller's is refused, 403, with keys or without; and an address no row has,
    under the server's, the database's and the page's own beginnings or by POST, is not found, 404, to whoever is
    logged in, with keys too. Before all of it, a name the server doesn't answer to is 421, and another site's page's
    request 403."""
    pinned, h = json.loads(ROUTES.read_text(encoding="utf-8")), serve.Handler
    seen = {"401": 0, "403": 0, "404": 0}
    for key, answer in pinned.items():
        method, address, said, caller = re.fullmatch(r"(\w+) (\S+)(?: \[(.*)\])? \| (.+)", key).groups()
        table, path, status = h.POST if method == "POST" else h.GET, unquote(urlsplit(address).path), answer["status"]
        row = table.get(path)
        if method not in ("GET", "HEAD", "POST") or (row is not None and row[0] is OPEN):
            continue
        if caller in ("nobody", "a browser, no login", "a wrong password"):
            opens = caller == "a browser, no login" and method == "GET"
            assert status == (302 if opens else 401), key
            seen["401"] += not opens
        elif caller == "the owner, by a name not its own":
            assert status == 421, key
        elif caller == "the owner, from another site's page":
            assert status == 403, key
        elif row is not None:
            wanted = max((g.role for g in steps(row) if g.role), key=access.ROLES.index)
            if access.ROLES.index(ROLE[caller]) < access.ROLES.index(wanted):
                assert status == 403, key
                seen["403"] += 1
        elif method == "POST" or table[beginning_of(table, path) + "*"][1] is h._not_there:
            assert status == 404, key
            seen["404"] += 1
    assert all(seen.values()), seen  # each part of the rule met somewhere


def test_the_walk_asks_every_address_the_server_s_code_compares_a_path_with():
    """Every address the server's code compares a request's path with besides its routes' rows, wholly or by its
    beginning, in serve.py and opds.py, and every page of the feeds' table (opds.PAGES) and a shelf's own feed
    (opds.SHELF): each is asked, so a new one can't be left out of the walk."""
    asked = {unquote(urlsplit(ask.address).path) for ask in asks()}
    rests = {path[len(base):].rstrip("/") for path in asked for base in serve.Handler.FEED_PATHS
             if path == base or path.startswith(base + "/")}
    found = {part: set().union(*(compared(module)[part] for module in (serve, opds))) for part in compared(serve)}
    lost = {part: sorted(set(today) - found[part]) for part, today in FOUND_TODAY.items()}
    assert lost == {part: [] for part in FOUND_TODAY}
    assert found["whole"] - asked == set()
    assert {s for s in found["starts"] if not any(a.startswith(s) for a in asked)} == set()
    assert {s for s in found["ends"] if not any(a.endswith(s) for a in asked)} == set()
    assert {p for p in found["pages"] if p not in rests} == set()  # a feed's page compared by its name, should one be
    assert {p for p in found["patterns"] if not any(re.match(p, rest) for rest in rests)} == set()
    assert [p.pattern for p, _ in opds.PAGES if not any(p.fullmatch(rest) for rest in rests)] == []
    assert any(opds.SHELF.match(rest) for rest in rests)


PLANTED = '''"""Made up."""


def answer(self, path):
    if path in {"/in-a-dict": 1}:
        pass
    if {"/got": 1}.get(path):
        pass
    begins = "/begins/"
    if path.startswith(begins):
        pass
    for start in ("/looped/", "/again/"):
        if path.startswith(start):
            pass
    if path.endswith(".end") or self._url.path == "/read-as-path":
        pass
'''


def test_the_code_reader_finds_an_address_however_it_is_written(tmp_path):
    """A made-up handler that names its addresses in a dict, gets by one from a dict, holds a beginning in a name of its
    own or takes the path through several in a loop, and asks how the path ends: each is found."""
    import importlib.util

    (tmp_path / "planted.py").write_text(PLANTED, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("planted", tmp_path / "planted.py")
    planted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planted)
    found = compared(planted)
    assert found["whole"] == {"/in-a-dict", "/got", "/read-as-path"} and found["ends"] == {".end"}
    assert found["starts"] == {"/begins/", "/looped/", "/again/"}


# ---- who is asking ---------------------------------------------------------------------------------------------------

class Asked(serve.Handler):
    """A request as the server's rules see it, with no connection: who sent it (peer), how, and its headers; the
    server listening on every address of this computer, at home 192.168.1.5 (conftest's), called library.example.org
    from outside too, with its proxies (trusted_proxies) as they come: this computer's own."""

    hosts = serve.Hosts(["library.example.org"])
    server = SimpleNamespace(server_address=("0.0.0.0", 8770))
    personal_secret = editors_secret = b"made up"

    def __init__(self, peer: str, method: str, path: str, headers: dict) -> None:  # no connection to take over
        self.client_address, self.command, self.path, self._url = (peer, 50123), method, path, urlsplit(path)
        self.headers = HTTPMessage()
        for name, value in headers.items():
            self.headers[name] = value
        self.sent: list[tuple[int, dict]] = []
        self.gate = SimpleNamespace(check=self._no_login, accounts=lambda: ({"owen": "-"}, {"owen": "owner"}),
                                    session_user=lambda headers, users=None: None)
        self.basic: bool | None = None
        self._caller = None

    def _no_login(self, sender: str, headers, session=None, users=None) -> tuple[str, None]:
        self.basic = ("Authorization" in headers) if "Authorization" in self.headers else None
        return "login", None

    def _send(self, status: int, ctype: str, body: bytes, cache: bool = True, headers: dict | None = None) -> None:
        self.sent.append((status, headers or {}))

    def _redirect(self, location: str) -> None:
        self.sent.append((302, {"Location": location}))


def standing(peer: str, method: str, path: str, headers: dict) -> dict:
    """What each of the server's rules makes of the request: its name (Hosts.allows), whether its forwarding headers
    count (_trusted), its sender, whether that is inside and how the lockout counts it, HTTPS, the names it calls the
    server by, a form from the library's own pages (_same_origin), a browser, one of the library's own pages asking, a
    page opened, the reader's own files, a feed asked for by its type, another site's request refused; without a
    login, what it is told (_admitted), and whether a Basic login it sent is kept; and the cookie a login gives it."""
    asked = Asked(peer, method, path, headers)
    sender = asked.sender
    out = {"name": asked.hosts.allows(asked.headers.get("Host")), "trusted": asked._trusted(), "sender": sender,
           "inside": access.inside(sender), "lockout": access.sender_key(sender), "https": asked._https(),
           "names": sorted(asked._own_hosts()), "same origin": asked._same_origin(), "browser": asked._browser(),
           "own page": asked._from_own_page(), "opens a page": asked._opening_a_page(),
           "the reader's": asked._for_the_reader(), "a feed": asked._asks_for_a_feed(), "refused": asked._cross_site()}
    asked.sent.clear()
    asked._admitted()
    status, said = asked.sent[0]
    out["no login"] = ("the login page" if status == 302 else "401, Basic asked" if "WWW-Authenticate" in said
                       else str(status))
    cookie = asked._cookies("t")[0]
    out |= {"Basic kept": asked.basic, "cookie": cookie.split("=")[0] + ("; Secure" if "Secure" in cookie else "")}
    asked.gate.check = lambda sender, headers, **asked_already: ("ok", "owen")
    assert asked._admitted() and asked._role() == "owner"  # a role is the same wherever its user is
    return out


BASIC = {"Authorization": "Basic " + base64.b64encode(b"rory:" + PASSWORD.encode()).decode()}
OPDS = {**BASIC, "User-Agent": "ReadingApp/1.0", "Accept": "application/atom+xml"}
TABLET = {"User-Agent": "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15", "Accept": "text/html,*/*"}
HERE, AT_HOME, OUTSIDE = {"Host": "localhost:8770"}, {"Host": "192.168.1.5:8770"}, {"Host": "library.example.org"}
PAGE = {"Referer": "http://192.168.1.5:8770/catalog/index.html"}  # what a page of the library at home asks
FUNNEL = {"Host": "testpc.tail1234.ts.net", "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https",
          "X-Forwarded-Host": "testpc.tail1234.ts.net", **BROWSER}
OUT = {"inside": False, "https": False, "same origin": True, "own page": False, "the reader's": False, "a feed": False,
       "refused": False, "Basic kept": None, "cookie": "hylly"}  # what most cases answer
INSIDE = {**OUT, "name": True, "inside": True}
SECURE = "__Host-hylly; Secure"
WHO = {  # a case: (who sent it, the method, the address, its headers), and what each rule answers
    "this computer, a browser": (
        ("127.0.0.1", "GET", "/catalog/index.html", {**HERE, **BROWSER}),
        {**INSIDE, "trusted": True, "sender": "127.0.0.1", "names": ["localhost:8770"], "browser": True,
         "opens a page": True, "no login": "the login page"}),
    "this computer, the library page asking": (
        ("127.0.0.1", "GET", "/api/session", {**HERE, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
                                              "Referer": "http://localhost:8770/catalog/index.html"}),
        {**INSIDE, "trusted": True, "sender": "127.0.0.1", "names": ["localhost:8770"], "browser": True,
         "own page": True, "opens a page": False, "no login": "401"}),
    "home, a reading app": (
        ("192.168.1.30", "GET", "/opds", {**AT_HOME, **OPDS}),
        {**INSIDE, "trusted": False, "sender": "192.168.1.30", "names": ["192.168.1.5:8770"], "browser": False,
         "opens a page": False, "a feed": True, "no login": "401, Basic asked", "Basic kept": True}),
    "home, a tablet's browser over plain http": (
        ("192.168.1.31", "GET", "/catalog/index.html", {**AT_HOME, **TABLET}),
        {**INSIDE, "trusted": False, "sender": "192.168.1.31", "names": ["192.168.1.5:8770"], "browser": True,
         "opens a page": True, "no login": "the login page"}),
    "home, the library page's change over plain http": (
        ("192.168.1.31", "POST", "/api/me/place", {**AT_HOME, "Origin": "http://192.168.1.5:8770", **TABLET,
                                                   "Accept": "*/*", **PAGE}),
        {**INSIDE, "trusted": False, "sender": "192.168.1.31", "names": ["192.168.1.5:8770"], "browser": True,
         "own page": True, "opens a page": False, "no login": "401"}),
    "home, the reader's font from a book's own frame": (
        ("192.168.1.31", "GET", "/fonts/faces.css", {**AT_HOME, **TABLET, "Accept": "text/css,*/*", **BASIC}),
        {**INSIDE, "trusted": False, "sender": "192.168.1.31", "names": ["192.168.1.5:8770"], "browser": True,
         "opens a page": False, "the reader's": True, "no login": "401", "Basic kept": False}),
    "outside, straight to a port left open": (
        ("203.0.113.9", "GET", "/opds", {"Host": "library.example.org:8770", **OPDS}),
        {**OUT, "name": True, "trusted": False, "sender": "203.0.113.9", "names": ["library.example.org:8770"],
         "browser": False, "opens a page": False, "a feed": True, "no login": "401, Basic asked", "Basic kept": True}),
    "outside over IPv6": (
        ("2001:db8::1:2", "GET", "/opds", {**OUTSIDE, **OPDS}),
        {**OUT, "name": True, "trusted": False, "sender": "2001:db8::1:2", "lockout": "2001:db8::/64",
         "names": ["library.example.org"], "browser": False, "opens a page": False, "a feed": True,
         "no login": "401, Basic asked", "Basic kept": True}),
    "through Tailscale Funnel": (
        ("127.0.0.1", "GET", "/catalog/index.html", FUNNEL),
        {**OUT, "name": True, "trusted": True, "sender": "203.0.113.9", "https": True,
         "names": ["testpc.tail1234.ts.net"], "browser": True, "opens a page": True, "no login": "the login page",
         "cookie": SECURE}),
    "through Tailscale Serve, a device on the tailnet": (
        ("127.0.0.1", "GET", "/catalog/index.html", {**FUNNEL, "X-Forwarded-For": "100.101.102.103"}),
        {**INSIDE, "trusted": True, "sender": "100.101.102.103", "https": True, "names": ["testpc.tail1234.ts.net"],
         "browser": True, "opens a page": True, "no login": "the login page", "cookie": SECURE}),
    "through cloudflared, a login": (
        ("127.0.0.1", "POST", "/login", {**OUTSIDE, "X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https",
                                          "Cf-Connecting-Ip": "198.51.100.7", "Origin": "https://library.example.org",
                                          **BROWSER, "Sec-Fetch-Site": "same-origin"}),
        {**OUT, "name": True, "trusted": True, "sender": "198.51.100.7", "https": True,
         "names": ["library.example.org"], "browser": True, "opens a page": False, "no login": "401",
         "cookie": SECURE}),
    "through a proxy that hands on no headers": (
        ("127.0.0.1", "GET", "/catalog/index.html", {**OUTSIDE, **TABLET}),
        {**INSIDE, "trusted": True, "sender": "127.0.0.1", "names": ["library.example.org"], "browser": True,
         "opens a page": True, "no login": "the login page"}),
    "through a proxy that isn't trusted": (
        ("192.168.1.40", "GET", "/catalog/index.html", {**AT_HOME, "X-Forwarded-For": "203.0.113.9",
                                                        "X-Forwarded-Proto": "https",
                                                        "X-Forwarded-Host": "library.example.org", **TABLET}),
        {**INSIDE, "trusted": False, "sender": "192.168.1.40", "names": ["192.168.1.5:8770"], "browser": True,
         "opens a page": True, "no login": "the login page"}),
    "another site's page, for the library's data": (
        ("127.0.0.1", "GET", "/catalog/data.js", {**HERE, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                                                  "Sec-Fetch-Dest": "script", "Referer": "https://elsewhere.example/"}),
        {**INSIDE, "trusted": True, "sender": "127.0.0.1", "names": ["localhost:8770"], "browser": True,
         "opens a page": False, "refused": True, "no login": "401"}),
    "another site's form, at home over plain http": (
        ("192.168.1.31", "POST", "/logout", {**AT_HOME, "Origin": "http://elsewhere.example", **TABLET}),
        {**INSIDE, "trusted": False, "sender": "192.168.1.31", "names": ["192.168.1.5:8770"], "same origin": False,
         "browser": True, "opens a page": False, "no login": "401"}),
    "a name of another's pointed here": (
        ("192.168.1.31", "GET", "/catalog/index.html", {"Host": "rebind.example:8770", **TABLET}),
        {**INSIDE, "name": False, "trusted": False, "sender": "192.168.1.31", "names": ["rebind.example:8770"],
         "browser": True, "opens a page": True, "no login": "the login page"}),
}


@pytest.mark.parametrize("case", list(WHO))
def test_who_is_asking_as_each_rule_works_it_out(case):
    """This computer, home, outside, the tunnels and proxies, another site, a name pointed here: what each of the
    server's rules about who is asking answers (standing), in one table. Every request needs a login wherever it comes
    from. Where it comes from decides only the sender (whom the lockout counts: an address, but outside over IPv6 its
    /64) and HTTPS (the cookie's name); what it sends decides whether it is a browser, sent to the login page, or a
    reading app, asked for its Basic login, and whether it is another site's, refused."""
    request, expected = WHO[case]
    assert standing(*request) == {"lockout": expected["sender"], **expected}


def test_open_addresses_are_open_only_as_written_and_every_other_is_read_decoded(library, serving):
    """What answers before any login (OPEN) does so only by the address exactly as sent: written with an escape, it's
    another address, which needs a login (401), and then isn't there (404). Every other address is read decoded, by
    the one dispatcher, a post's as a page's: /api/me/plac%65 keeps a place as /api/me/place does."""
    srv, _ = serving(library, login=None)
    walk = Walk(srv, library)
    try:
        assert walk.send("nobody", Ask("GET", "/healthz")).status_code == 200
        assert walk.send("nobody", Ask("GET", "/heal%74hz")).status_code == 401
        assert walk.send("a reader", Ask("GET", "/heal%74hz")).status_code == 404
        place = {"id": "h:lwolf1", "at": 1, "pct": 0.5}
        kept = walk.send("a reader", Ask("POST", "/api/me/plac%65"), keyed=True, body=place)
        assert kept.status_code == 200 and kept.json()["ok"]
        assert walk.send("a reader", Ask("POST", "/api/me/plac%65"), body=place).status_code == 403  # its key, as ever
    finally:
        walk.client.close()
