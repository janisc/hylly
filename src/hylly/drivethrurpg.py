"""DriveThruRPG (drivethrurpg.com): every file in the account's library, on the shops' core (shopping.py): what is its
own is its API session, its products and their files as the API lists them, and the three things the core asks of it
(Library).

How the site works (2026-10-28; the API DriveThruRPG's own Library app uses, which the drpg tool uses too):
- An application key, made under "Library App Keys" in the account settings, buys a token:
  POST api/vBeta/auth_key?applicationKey=<key> answers {"token": ...}, sent as the Authorization header.
- GET order_products?library=1&archived=0&getChecksum=1&getFilters=1&page=N&pageSize=50 lists the products,
  a page at a time, until an empty page. Each has its publisher, order date, ISBN, a short description, a
  cover (product.image, under /images/) and its files: index, filename, size and MD5 checksums with dates
  (the newest is the file as it is now). getFilters adds the shop's own facets: Rule System (the game,
  such as "Traveller"), Product Type and Genre.
- A file's link: GET order_products/<orderProductId>/prepare?siteId=10&index=<i>. While the answer's status
  starts with "Preparing", ask order_products/<orderProductId>/check with the same parameters. The link
  doesn't live long, so it is fetched right before the download.
- The API is undocumented and may change. It answers 400, 429 or 5xx now and then, so calls are retried.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from .logins import AuthError, ShopError
from .shopsession import TRIES, ShopSession, busy
from .words import many

API_URL = "https://api.drivethrurpg.com/api/vBeta/"
IMAGES = "https://www.drivethrurpg.com/images/"
SITE_ID = 10
PAGE_SIZE = 50
MAX_PAGES = 2000  # of the library: far more than anyone owns
REFUSED = (
    "DriveThruRPG did not accept the application key. Make a new one on drivethrurpg.com (Account > Settings > "
    "Library App Keys) and run 'hylly login drivethrurpg' again."
)


@dataclass
class File:
    """A product's file; for the shops' core, a shopping.File by the names it reads (key, name, label, role)."""
    index: int
    filename: str
    md5: str | None  # the newest checksum DriveThruRPG lists; None if it lists none
    size: int = 0  # 0: not listed, known once downloaded
    label = ""  # DriveThruRPG has no word for a file's format: its extension's
    role = "book"  # every file of a product is downloaded, as the product's

    @property
    def key(self) -> int:
        return self.index

    @property
    def name(self) -> str:
        return self.filename


@dataclass
class Product:
    """A product in the library; for the shops' core, a shopping.Title by the names it reads (key, by, kind, details)."""
    id: str  # productId: the same product in any account
    order_id: int  # orderProductId: this account's copy, the handle for download links
    name: str
    publisher: str
    modified: str = ""  # fileLastModified
    files: list[File] = field(default_factory=list)
    cover: str = ""
    purchased: str = ""  # YYYY-MM-DD
    isbn: str = ""
    description: str = ""  # DriveThruRPG's short description, cut off by them
    games: list[str] = field(default_factory=list)  # the Rule System filter: "Traveller"
    types: list[str] = field(default_factory=list)  # Product Type: "Game Master Guides", "Adventures"
    genres: list[str] = field(default_factory=list)  # DriveThruRPG's Genre: "Space Opera"
    bundle: int = 0  # the bundle it was bought in (bundleId), 0 if bought alone
    kind = ""  # its own word for its kind: none, so its shop's, an RPG book, which its publisher and its files go over
    found = ""  # the kind it is found to be, worked out once by the shops' core (shopping.kind_found)

    @property
    def key(self) -> str:
        return self.id

    @property
    def by(self) -> str:
        return self.publisher

    @property
    def details(self) -> dict:
        """What the snapshot keeps of it besides its files, for the catalog: never order_id, this account's own."""
        return {"modified": self.modified, "cover": self.cover, "isbn": self.isbn, "description": self.description,
                "games": self.games, "types": self.types, "genres": self.genres, "bundle": self.bundle}


def newest_checksum(checksums) -> str | None:
    dated = [c for c in checksums or [] if isinstance(c, dict) and c.get("checksum")]
    return max(dated, key=lambda c: str(c.get("checksumDate") or ""))["checksum"].lower() if dated else None


def _filters(raw: dict, group: str) -> list[str]:
    names = [str(f.get("name") or "").strip() for f in raw.get("filters") or [] if isinstance(f, dict) and f.get("parentName") == group]
    return list(dict.fromkeys(n for n in names if n))


def _what_it_is(raw) -> tuple[str, str]:
    """(productId, a name for people) of a product that can't be read: as much as can be told of it, "" for an id that
    can't be read either."""
    raw = raw if isinstance(raw, dict) else {}
    pid, name = str(raw.get("productId") if isinstance(raw.get("productId"), (int, str)) else ""), raw.get("name")
    pid = pid if re.fullmatch(r"\d{1,20}", pid) else ""  # as parse_product writes it: str() of DriveThruRPG's number
    if isinstance(name, str) and name.strip():
        return pid, name.strip()[:100]
    return pid, f"DriveThruRPG product {pid}" if pid else "a product without a name"


def parse_product(raw: dict) -> Product:
    publisher = raw.get("publisher") or {}
    about = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    description = (about.get("description") or {}) if isinstance(about.get("description"), dict) else {}
    image = str(about.get("image") or "")
    return Product(
        id=str(raw["productId"]), order_id=int(raw["orderProductId"]), name=str(raw.get("name") or "Untitled").strip(),
        publisher=str((publisher.get("name") if isinstance(publisher, dict) else publisher) or "Others").strip(),
        modified=str(raw.get("fileLastModified") or ""),
        files=[File(index=int(f["index"]), filename=str(f.get("filename") or f"file {f['index']}"),
                    md5=newest_checksum(f.get("checksums")), size=int(f.get("size") or 0))
               for f in raw.get("files") or []],
        cover=IMAGES + image if image and not image.startswith("http") else image,
        purchased=str(raw.get("datePurchased") or (raw.get("order") or {}).get("dateCreated") or "")[:10],
        isbn=str(raw.get("isbn") or ""),
        description=str(description.get("shortDescription") or "").replace("�", " ").strip(),
        games=_filters(raw, "Rule System"), types=_filters(raw, "Product Type"), genres=_filters(raw, "Genre"),
        bundle=int(raw.get("bundleId") or 0),
    )


class DriveThruRPG:
    """A logged-in API session. The token is fetched again if DriveThruRPG stops taking it mid-run."""

    def __init__(self, key: str, transport: httpx.BaseTransport | None = None, delay: float = 1.0,
                 wait: float = 2.0, max_wait: float = 180.0, backoff: float = 1.0, tries: int = TRIES):
        self.session = ShopSession(
            "DriveThruRPG", API_URL, pace=delay, backoff=backoff, tries=tries, transport=transport,
            headers={"Accept": "application/json"},
            again=lambda status: status == 400 or busy(status),  # its API answers 400 now and then, and the answer next time
            relogin=self._login_again,  # the token can run out mid-run: a new one, once for each request
            refused_when=lambda r: r.status_code in (401, 403), refused=REFUSED,
        )
        self.http = self.session.http
        self.key = key
        self.wait, self.max_wait = wait, max_wait
        self._lock = threading.Lock()

    def __enter__(self) -> DriveThruRPG:
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    def login(self) -> None:
        # Never raise_for_status here: httpx's message would carry the address, and with it the key. A 401 or 403 is the
        # session's AuthError (REFUSED).
        r = self._call("POST", "auth_key", {"applicationKey": self.key}, auth=False)
        try:
            token = r.json().get("token") if r.status_code == 200 else None
        except (json.JSONDecodeError, AttributeError):
            token = None
        if not token:
            raise AuthError(REFUSED if r.status_code in (200, 401, 403) else f"DriveThruRPG's login answered HTTP {r.status_code}; try again later.")
        self.http.headers["Authorization"] = token

    def _login_again(self) -> None:
        with self._lock:  # one login at a time: downloads turned away at once each log in again, in turn
            self.login()

    def _call(self, method: str, url: str, params: dict, auth: bool = True) -> httpx.Response:
        """auth: False for the login itself, which a 401 doesn't send to log in again."""
        return self.session.request(method, url, params=params, login_again=auth)

    def _json(self, r: httpx.Response, what: str):
        if r.status_code != 200:
            raise ShopError(f"DriveThruRPG answered HTTP {r.status_code} for {what}.", r.status_code)
        try:
            return r.json()
        except json.JSONDecodeError:
            raise ShopError(f"DriveThruRPG sent something other than JSON for {what}.") from None

    def raw_products(self, progress=None) -> list[dict]:
        """The library as DriveThruRPG lists it, page by page until a page that is empty or not full. A page the same
        as the one before, or more pages than any library has, stops it: the site would be ignoring the page asked for."""
        rows, page, before = [], 1, None
        while True:
            params = {"library": 1, "archived": 0, "getChecksum": 1, "getFilters": 1, "page": page, "pageSize": PAGE_SIZE}
            batch = self._json(self._call("GET", "order_products", params), f"page {page} of your library")
            if not batch:
                return rows
            ids = [r.get("orderProductId") for r in batch if isinstance(r, dict)]
            if ids == before:
                raise ShopError(f"DriveThruRPG sent page {page} of your library the same as page {page - 1}, so reading it stopped.")
            if page > MAX_PAGES:
                raise ShopError(f"Your DriveThruRPG library went on past {MAX_PAGES} pages, so reading it stopped.")
            rows += batch
            if progress:
                progress(len(rows))
            if len(batch) < PAGE_SIZE:
                return rows
            page, before = page + 1, ids

    def products(self, progress=None) -> list[Product]:
        """The library's products. One listed in a shape that can't be read (a field missing, or of another kind) is
        left out of the run, so that one odd product doesn't stop the whole listing, and put in self.unreadable as
        (its id, or "" when even that can't be read; a name for people): snapshot() keeps it in the catalog as it was.
        If none can be read, the listing itself has changed: that stops, so that an empty list never stands for the
        library."""
        rows, out, self.unreadable = self.raw_products(progress), [], []
        for raw in rows:
            try:
                out.append(parse_product(raw))
            except Exception:  # noqa: BLE001 - kept as unreadable: whatever an odd field makes int() or str() say,
                # ValueError, OverflowError for Infinity
                self.unreadable.append(_what_it_is(raw))
        if rows and not out:
            raise ShopError(f"None of the {many(len(rows), 'product')} in DriveThruRPG's listing could be read, so nothing was "
                            "changed: the listing may have changed shape, and Hylly may need an update.")
        return out

    def link(self, product: Product, file: File) -> str:
        """A fresh download link; '' when DriveThruRPG no longer has the file."""
        from .download import DownloadError

        params = {"siteId": SITE_ID, "index": file.index, "getChecksums": 0}
        r = self._call("GET", f"order_products/{product.order_id}/prepare", params)
        waited = 0.0
        while True:
            if r.status_code == 404:
                return ""
            data = self._json(r, "a download link")
            if not str(data.get("status", "")).startswith("Preparing"):
                return str(data.get("url") or "")
            if waited >= self.max_wait:
                raise DownloadError(f"DriveThruRPG was still preparing the file after {self.max_wait:.0f} s")
            time.sleep(self.wait)
            waited += self.wait
            r = self._call("GET", f"order_products/{product.order_id}/check", params)


def check_key(key: str) -> int:
    """For 'hylly login': the number of products in the library; raises AuthError when the key is refused. No answer
    at all is said at once (asked once); a 400, 429 or 5xx is asked again as in a run, as its API answers 400 now and
    then, so that one such answer doesn't refuse a good key."""
    with DriveThruRPG(key, delay=0) as api:
        api.session.unanswered = 1
        api.login()
        return len(api.raw_products())


# ---- for the shops' core ------------------------------------------------------------------

class Library:
    """DriveThruRPG on the shops' core (shopping.py): its three things, by the API session. What is downloaded, where it
    goes, its records and its snapshot are the core's, as for every shop on it."""

    def __init__(self, key: str):
        self.api = DriveThruRPG(key)
        self.unreadable: list[tuple[str, str]] = []

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc) -> None:
        self.api.__exit__(*exc)

    def check(self) -> None:
        self.api.login()

    def titles(self, progress=None) -> list[Product]:
        products = self.api.products(progress=progress)
        self.unreadable = self.api.unreadable
        return products

    def link(self, title: Product, file: File) -> str:
        return self.api.link(title, file)
