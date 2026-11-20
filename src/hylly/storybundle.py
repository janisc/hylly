"""StoryBundle (storybundle.com): every bundle on the account's "My Book Bundles" page.

How the site works (2026-10-25):
- `/users/<id>` lists the bundles, each linking to its download page `/downloads/<key>`.
- A download page lists the books, one file each, with a rounded size. A book link is a Rails
  POST (`data-method="post"`, with the page's CSRF token) that redirects to an S3 link valid for
  60 seconds. S3 sends the exact length and an ETag (MD5), which the downloader checks.
- Not logged in, the site redirects to its front page.
- There is no format choice: the publisher's file comes as it is (mostly EPUB, some PDF).

On the shops' core (shopping.py): what is its own is its web session, its bundles and their books as its
pages list them, and the three things the core asks of it (Library).
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from . import paths
from .logins import SITES, AuthError, ShopError
from .shopsession import TRIES, ShopSession

BASE_URL = "https://storybundle.com"
HOSTS = {"storybundle.com", "www.storybundle.com"}
FILES = ".amazonaws.com"  # where StoryBundle keeps its files, Amazon S3: a book's link leads there, nowhere else
LOGIN_PAGE = re.compile(r"(?i)log_?in|sign_?in|session")
EXPIRED = (
    "StoryBundle did not accept the cookie ({what} redirected). Log in on storybundle.com, "
    "copy a fresh _storybundle31_session value and run 'hylly login storybundle' again."
)

BUNDLE_ROW = re.compile(
    r'<a href="(https://storybundle\.com/downloads/[^"]+)">([^<]+)</a>\s*</label>\s*<em>\s*Purchased on ([^<]+)</em>'
)
def my_bundles_path() -> str:
    """The account's own "My Book Bundles" page; it redirects to the front page when not logged in.
    Shop pages don't link to it, so its number is a setting: storybundle_user in settings.toml, or HYLLY_STORYBUNDLE_USER."""
    user = paths.setting("storybundle_user", "HYLLY_STORYBUNDLE_USER")
    if not user:
        raise AuthError(f"Your StoryBundle account number is not set. Add storybundle_user = '<number>' to {paths.settings_file()}: "
                        "it is the number in the address of your My Book Bundles page (storybundle.com/users/<number>).")
    return f"/users/{user}"


BOOK_LINK = re.compile(r'href="(https://storybundle\.com/books/(\d+)/download/[^"]+)"')
SIZE = re.compile(r"\(([\d.]+)\s*(KB|MB|GB)\)")
UNITS = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}


@dataclass
class Book:
    id: str
    title: str
    approx_size: int
    link: str  # secret: the POST target
    cover: str = ""


@dataclass
class Bundle:
    name: str
    purchased: str  # YYYY-MM-DD
    page: str  # secret: the bundle's download page
    books: list[Book] = field(default_factory=list)

    @property
    def bonus(self) -> bool:
        return "newsletter bonus" in self.name.lower()


def parse_library(page: str) -> list[Bundle]:
    bundles = []
    for url, name, date in BUNDLE_ROW.findall(page):
        try:
            purchased = datetime.strptime(date.strip(), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            purchased = date.strip()
        bundles.append(Bundle(name=html.unescape(name).strip(), purchased=purchased, page=url))
    return bundles


def title_from_cover(url: str) -> str:
    """Newsletter bonus pages name the book only in the cover file: The_Sisters_Example_Cover_Final.jpg."""
    stem = unquote(PurePosixPath(urlsplit(url).path).stem)
    stem = re.sub(r"(?i)([_\- ]+(cover|final|front|web|ebook|hi-?res|print|v\d+|\d{3,}))+$", "", stem)
    return re.sub(r"_+", " ", stem).strip() or "Untitled"


def parse_books(page: str) -> list[Book]:
    """Bundle pages (<li class="book"> with a title label and size) and newsletter bonus pages
    (plain <li> with only a cover) alike."""
    books = []
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", page, re.S):
        chunk = m.group(1)
        link = BOOK_LINK.search(chunk)
        if not link:
            continue
        title, size = re.search(r'<label title="([^"]*)"', chunk), SIZE.search(chunk)
        cover = re.search(r'<img src="([^"]+)"', chunk)
        cover_url = cover.group(1) if cover else ""
        books.append(Book(
            id=link.group(2), link=link.group(1), cover=cover_url,
            title=html.unescape(title.group(1)).strip() if title else title_from_cover(cover_url),
            approx_size=int(float(size.group(1)) * UNITS[size.group(2)]) if size else 0,
        ))
    return books


def csrf_token(page: str) -> str | None:
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)
    return m.group(1) if m else None


def _file_link(r: httpx.Response) -> str:
    """The file's link from StoryBundle's answer to a book's download. Only a redirect back to its login or front page
    says the cookie wasn't accepted; the rest is about the book, or StoryBundle busy, and fails only that book."""
    status, location = r.status_code, r.headers.get("location", "")
    if status in (301, 302, 303, 307, 308) and location:
        try:
            where = urlsplit(location)
            host = where.hostname or "storybundle.com"  # an address without a host is on StoryBundle's own site
        except ValueError:
            raise ShopError("StoryBundle sent a book's download to an address that can't be read.", status) from None
        if host in HOSTS:
            if not where.path.strip("/") or LOGIN_PAGE.search(where.path):
                raise AuthError(EXPIRED.format(what="a book download"))
            raise ShopError("StoryBundle sent a book's download to one of its own pages instead of the file.", status)
        if where.scheme != "https":
            raise ShopError("StoryBundle sent a book's download to an address that isn't https, so it wasn't followed.",
                            status)
        if not host.endswith(FILES):
            raise ShopError("StoryBundle sent a book's download to another site than the one it keeps its files on, so it "
                            "wasn't followed: Hylly may need an update.", status)
        return location
    if status == 429 or status >= 500:
        raise ShopError(f"StoryBundle answered HTTP {status} when asked for a book's download link. Try again later.", status)
    if status >= 400:
        raise ShopError(f"StoryBundle answered HTTP {status} when asked for a book's download link.", status)
    raise ShopError(f"StoryBundle sent no link to a book's file (HTTP {status}).", status)


class StoryBundle:
    """A logged-in session, a second between requests. Its pages and its books' links alike are asked again after
    network trouble, 429 and 5xx. Which answers mean the cookie wasn't taken differs between them (_get, _file_link),
    so each reads its own."""

    def __init__(self, session: str, transport: httpx.BaseTransport | None = None, delay: float = 1.0, backoff: float = 2.0,
                 tries: int = TRIES):
        jar = httpx.Cookies()  # a jar, not a header: it keeps whatever the site updates (Rails rewrites its session)
        jar.set(SITES["storybundle"][0], session, domain="storybundle.com")
        self.session = ShopSession("StoryBundle", BASE_URL, pace=delay, backoff=backoff, tries=tries, transport=transport,
                                   cookies=jar)
        self.http = self.session.http
        self.token: str | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> StoryBundle:
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    def _get(self, url: str, what: str) -> str:
        r = self.session.request("GET", url)
        if r.status_code in (301, 302, 303):  # not logged in: a page sends you on to the front page
            raise AuthError(EXPIRED.format(what=what))
        if r.status_code >= 400:  # not raise_for_status: its message has the address, and a bundle's page is its secret
            raise ShopError(f"StoryBundle answered HTTP {r.status_code} for {what}.", r.status_code)
        return r.text

    def library_page(self) -> str:
        page = my_bundles_path()
        return self._get(page, "your bundles page")  # not its address, which has your account's number

    def bundles(self, progress=None) -> list[Bundle]:
        """Every bundle with its books, a page each. progress: told the number of books read so far, after each page."""
        bundles = parse_library(self.library_page())
        for b in bundles:
            page = self._get(b.page, "a bundle's download page")
            b.books = parse_books(page)
            self.token = csrf_token(page) or self.token
            if progress:
                progress(sum(len(x.books) for x in bundles))
        return bundles

    def resolve(self, book: Book) -> str:
        """A fresh link to the book's file, valid about 60 seconds: StoryBundle's answer sends the browser on to it, on
        Amazon S3 (FILES), over https; anywhere else isn't followed (a ShopError). Sent back to StoryBundle's login or
        front page instead, the cookie wasn't accepted (AuthError). Any other answer is about this one book, or
        StoryBundle being busy: a ShopError with the status, after asking again on network trouble, 429 and 5xx as the
        other shops do (no answer at all: status 0). The book's link is a secret: no message has it."""
        with self._lock:  # one at a time, whatever the number of downloads asking
            if not self.token:
                raise AuthError("No CSRF token: read a bundle page first.")
            r = self.session.request("POST", book.link, data={"_method": "post", "authenticity_token": self.token})
        return _file_link(r)


def library_page(session: str) -> str:
    """For 'hylly login': raises AuthError when the cookie is not accepted. Asked once: no answer is said at once."""
    with StoryBundle(session, delay=0, tries=1) as sb:
        return sb.library_page()


# ---- for the shops' core ------------------------------------------------------------------

REFUSALS = (401, 403, 422)  # StoryBundle turning a book's link request away: a session run out may answer any of them


def _pairs(bundles: list[Bundle]):
    """Each book of each bundle as the shops' core takes it, with the book: oldest bundle first (titles_of)."""
    from .shopping import File, Title

    n = 0
    for b in sorted(bundles, key=lambda b: b.purchased):
        for k in b.books:
            yield Title(key=k.id, name=k.title, by="", files=[File(0, "", k.approx_size, rough=True)], purchased=b.purchased,
                        details={"cover": k.cover.split("?")[0], "bonus": b.bonus}, under=b.name, place=(n,),
                        record={"bonus": b.bonus}), k
            n += 1


def titles_of(bundles: list[Bundle]) -> list:
    """The books as the shops' core takes them (shopping.Title), each in the folder of its bundle (under): one file, its
    name StoryBundle gives only in its link, and its size StoryBundle's rounded one (File.name "", File.rough); of the
    kind its bundle's words make it, over a book (shopping.kind_found), as its file says nothing of its format yet. A
    book is listed once for each bundle it came in, in StoryBundle's own order, its bundles oldest first by purchase
    date (place), so that the core takes it once, in the first of them a run's choices leave (shopping.select), as its
    own flow did. Who made a book StoryBundle doesn't say (by "")."""
    return [t for t, _ in _pairs(bundles)]


def from_snapshot(data) -> list:
    """The titles a snapshot of StoryBundle's keeps, as Library.titles gives them, without their links: of either shape,
    the core's (shopping.snapshot: a book once for each bundle it came in, its bundle as under) or its own from before
    it moved onto the core (its bundles, each with its books)."""
    bundles = []
    for e in data if isinstance(data, list) else []:
        if "books" in e:  # its own, from before: a bundle with its books
            books = [Book(str(k["id"]), str(k.get("title") or ""), int(k.get("approx_size") or 0), "", str(k.get("cover") or ""))
                     for k in e["books"]]
            bundles.append(Bundle(str(e.get("name") or ""), str(e.get("purchased") or ""), "", books))
        else:  # the core's: a book of a bundle, in the order of the bundles
            size = int(((e.get("files") or [{}])[0]).get("size") or 0)
            book = Book(str(e["id"]), str(e.get("name") or ""), size, "", str(e.get("cover") or ""))
            bundles.append(Bundle(str(e.get("under") or ""), str(e.get("purchased") or ""), "", [book]))
    return titles_of(bundles)


def bonus(t) -> bool:
    """Whether a book is listed in a newsletter bonus bundle (Title.under): what --no-bonus leaves out of a run."""
    return bool(t.record.get("bonus"))


class Library:
    """StoryBundle on the shops' core (shopping.py): its three things, by its web session. What is downloaded, where
    it goes, its records and its snapshot are the core's, as for every shop on it; a book's file is named by the link
    its download starts with, which then fetches it (shopping.Naming), and the file its link names as a kind no
    download takes is kept as left out."""

    # What a run of its counts as turned away (download.REFUSED_IN_A_ROW), in the words of a run they stop: its link
    # requests' REFUSALS, and the downloads' own (download.REFUSED)
    refused = "401, 403, 422 or 429"

    def __init__(self, session: str):
        self.sb = StoryBundle(session)
        self.unreadable: list[tuple[str, str]] = []  # a book without a link is none: nothing is read that can't be
        self._books: dict[int, Book] = {}  # each title's book, with its secret link
        # a bundle's page that listed no book (one changed, or shown amiss): not the whole library read, so the questions
        # of its books stay (cli._shop_download)
        self.incomplete = False

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc) -> None:
        self.sb.__exit__(*exc)

    def check(self) -> None:
        """Nothing of its own: the cookie is checked by the first page titles reads, the bundles page (AuthError), as
        asking for that page twice would be a request more."""

    def titles(self, progress=None) -> list:
        bundles = self.sb.bundles(progress=progress)
        self.incomplete = any(not b.books for b in bundles)
        pairs = list(_pairs(bundles))
        self._books = {id(t): k for t, k in pairs}
        return [t for t, _ in pairs]

    def link(self, title, file) -> str:
        """A fresh link to the book's file (StoryBundle.resolve), right before its download: the first one names the
        file too (shopping.Naming). Turned away (REFUSALS), it is download.Refused, as the downloads count a refusal: so
        a session that runs out mid-run stops the run after a few books, not after a request for every one."""
        from .download import Refused

        try:
            return self.sb.resolve(self._books[id(title)])
        except ShopError as e:
            if e.status in REFUSALS:
                raise Refused(str(e)) from None
            raise

