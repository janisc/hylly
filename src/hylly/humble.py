"""Humble Bundle: the read-only client for its site, and its Library on the shops' core (shopping.py): your titles from
your purchases by Humble's plan (plan.py: each title's one file in the format its list keeps, its other formats behind
it, its extras), fresh signed links for their files, the purchases Humble gave no data for, what a download says of the
whole library, per kind (summary), and what the plan leaves to you: the file of a title it was unsure of
(File.unsure, which the core asks), and the items of extras only that may be titles after all (items_to_ask).

How the site works. Humble rebuilt its site as a Next.js app, and the order API Hylly read
before (/api/v1/user/order and /api/v1/orders, as smbl64/humble-cli does) answers 404 since. Hylly asks it as its own
library page does, a second between requests; none of it is documented, so a change shows as "Hylly may need an update":
- The login: the cookie of Humble's sign-in (Ory), ory_session_stoiccohens510ht9xek, on www.humblebundle.com; the old
  _simpleauth_sess is still set, but the site no longer takes it as a login. /api/auth/session-status answers
  {"status": "authenticated"}, or "anonymous" for a login it doesn't take (check).
- Your library: the page's server action filterUserLibrary, a POST to /home/library with the action's id (Next-Action)
  and the page's own form, LIMIT items a page, newest first (the page asks 5; 100 are answered as well). Each item: its
  entitlement id, title, bundle (subTitle), contentId and its purchase (productEntitlementId, shared by a bundle's
  items); no files. contentId is the machine name the order API gave the item (its subproduct's), but where that had a
  "-" or capitals: those come changed ("-" as "___"), which remap reads back.
- A page as the router asks for it (RSC: 1) wants Next.js's check value, _rsc: asked without it, the site sends the
  request on to the same page with it (a 307), the same value for every page Hylly asks for, which is kept for the rest
  of the run (HumbleClient.rsc).
- A purchase's files: its page, /home/library/<productEntitlementId>, as the router asks for it, holds each
  item's downloads (key: the order API's download machine name; platform) and their files (label, file name, MD5,
  SHA1, size), as the order API listed them. A purchase of keys alone has no files, and its page isn't asked for.
- A file's link: the server action getSignedUrlsForEntitlements, posted for the item's entitlement id as the title's
  own page posts it: links on cdn.humble.com signed with t, for a day, by subproduct machine name, download machine name
  and file name. The page tells Humble of each download besides (recordDownload): Hylly doesn't.
- The actions' ids change with each version of Humble's site: a run finds them by name in the scripts its pages list
  (Actions), the list's in the library page's, the link's in a purchase's or a title's.
What it can't see: the purchase's date, the publisher, a cover and the order key, which the order API gave. Those of a
purchase it listed are kept from the newest snapshot (remap: a title keeps its id, h:<machine name>, its folder and its
purchase's key); a purchase no snapshot has is dated the day a run first saw it, keyed by its productEntitlementId, and
its titles have no publisher or cover from Humble.
"""

from __future__ import annotations

import csv
import json
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from . import shopping
from .logins import SITES, AuthError, ShopError
from .shops import SHOPS
from .shopsession import TRIES, ShopSession, busy
from .words import gb, many

COOKIE_NAME = SITES["humble"][0]  # the login cookie Humble's site sets

BASE_URL = "https://www.humblebundle.com"
PAUSE = 1.0  # seconds between requests, however many downloads ask for fresh links at once
LIMIT = 100  # items a page of the library's list
MOST_PAGES = 1000  # of the list, whatever it says: 100,000 items
LIST, LINK = "filterUserLibrary", "getSignedUrlsForEntitlements"  # the page's server actions, by their names
LINK_EARLY = 3600.0  # seconds before a set of links expires that it is asked for anew, as the page does

EXPIRED = (
    "Humble Bundle did not accept the session cookie; it has probably expired. "
    "Log in on humblebundle.com, copy the value of its ory_session_stoiccohens510ht9xek cookie again and run 'hylly login humble' "
    "again."
)
CHANGED = "Humble's site has changed ({}): Hylly may need an update."
# A purchase's page that holds no list of its files (an error page of Humble's own, say) is one Humble gave no data for,
# while the others' pages are read: more than FEW_UNREAD such pages in a run, or one in UNREAD_SHARE of those read,
# whichever is more, or none read at all, and Humble's site has changed
FEW_UNREAD = 3
UNREAD_SHARE = 20

SCRIPT = re.compile(r"/_next/static/chunks/[A-Za-z0-9_.~-]+\.js")  # a script a page lists
ACTION = re.compile(rb'createServerReference\)\(\s*"([0-9a-f]{40,})"[^"]{0,200}"([A-Za-z_$][\w$]*)"')  # its actions


class HumbleClient:
    """Humble's site, read only, as its library page reads it (the module's docstring). order_keys lists your
    purchases and keeps their items (listed); orders reads their files; link_set makes a title's links."""

    def __init__(self, session: str, timeout: float = 60.0, transport: httpx.BaseTransport | None = None,
                 tries: int = TRIES):
        """tries: of each request; 1 for a login's check, which says at once when Humble doesn't answer."""
        self.session = ShopSession(
            "Humble Bundle", BASE_URL, pace=PAUSE, tries=tries, timeout=timeout, transport=transport,
            headers={"Cookie": f"{COOKIE_NAME}={session}", "Origin": BASE_URL},
            # a login Humble doesn't take is sent on to its sign-in page: any redirect says so, as 401 and 403 do
            refused_when=self._turned_away, refused=EXPIRED,
        )
        self.http = self.session.http
        self.how = ""  # how Humble last turned the login away, in a few words of Hylly's: for the login's check
        self.rsc: str | None = None  # Next.js's check value for a page asked for as the router asks (_rsc), once the
        # site has said it: "" for requests with none of the router's headers it goes by, as Hylly's are
        self.actions = Actions(self)
        self.listed: dict[str, list[dict]] = {}  # each purchase's items as the list gave them, by purchase id
        self.unread_pages = 0  # the purchases whose page held no list of its files, in the last orders()

    def __enter__(self) -> HumbleClient:
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    def _turned_away(self, r: httpx.Response) -> bool:
        if r.is_redirect and not _same_page(r):
            self.how = "it sent the request on to its sign-in page"
        elif r.status_code in (401, 403):
            self.how = f"it answered HTTP {r.status_code}"
        else:
            return False
        return True

    def check(self) -> None:
        """Whether Humble takes the login: a logins.AuthError when it doesn't, which says how Humble answered (turned
        away, a page instead of JSON, or "anonymous"), never the login."""
        try:
            r = self._get("/api/auth/session-status", "your login", accept="application/json")
        except AuthError:
            raise AuthError(f"{EXPIRED} (Humble's answer: {self.how}.)") from None
        if "json" not in r.headers.get("content-type", ""):
            raise AuthError(f"{EXPIRED} (Humble's answer: a page, not its login's status.)")
        answer = _json_of(r, "your login")
        status = answer.get("status") if isinstance(answer, dict) else None
        if status != "authenticated":
            said = re.sub(r"[^a-z_ -]", "", str(status).lower())[:30] if isinstance(status, str) else "no status"
            raise AuthError(f"{EXPIRED} (Humble's answer: {said or 'no status'}, not signed in.)")

    def order_keys(self) -> list[str]:
        """Your purchases, newest first, by Humble's id for each (productEntitlementId), the login checked first; each
        one's items kept (listed) for orders. Every page of the list is read, up to the last it names; a page with
        nothing new on it ends it too (a list that goes round). A list that can't be the whole of your library stops it,
        rather than give a poorer one: pages without their counts, fewer items than it says it has, an item with files
        but no contentId (the machine name every title's id is made of), or items with no files at all."""
        self.check()
        listed: dict[str, dict[str, dict]] = {}
        seen: set[str] = set()
        page, pages, total = 1, 1, 0
        while page <= min(pages, MOST_PAGES):
            answer = self.actions.call(LIST, "/home/library", files=_list_form(page), what="your library's list")
            results = answer.get("results") if isinstance(answer, dict) else None
            paging = answer.get("pagination") if isinstance(answer, dict) else None
            if (not isinstance(results, list) or not isinstance(paging, dict) or answer.get("success") is False
                    or not isinstance(paging.get("totalPages"), int) or not isinstance(paging.get("totalDocs"), int)):
                raise ShopError(CHANGED.format("its library's list"))
            new = [r for r in _list(results) if _text(r.get("id")) and r["id"] not in seen]
            if page > 1 and not new:
                break
            for item in new:
                if _has_files(item) and not _text(item.get("contentId")):
                    raise ShopError(CHANGED.format("an item of its library's list has no contentId"))
                seen.add(item["id"])
                purchase = _text(item.get("productEntitlementId")) or item["id"]  # an item bought alone: its own
                listed.setdefault(purchase, {})[item["id"]] = item
            pages, total = paging["totalPages"], paging["totalDocs"]
            page += 1
        if len(seen) < total:
            raise ShopError(f"Humble's list of your library ended early ({len(seen)} of {total} items). Running it again "
                            "asks again; if it stays so, Hylly may need an update.")
        if seen and not any(_has_files(i) for v in listed.values() for i in v.values()):
            raise ShopError(CHANGED.format("no item of its library's list has files to download"))
        self.listed = {k: list(v.values()) for k, v in listed.items()}
        return list(self.listed)

    def orders(self, keys: list[str], progress: Callable[[int, int], None] | None = None
               ) -> tuple[dict[str, dict], list[str]]:
        """Each purchase as the order API gave it (order_of), by Humble's id for it, its files from its page; and the
        purchases Humble gave no data for this time (a page refused, or gone, or one that holds no list of its files, as
        an error page of Humble's own does: unread_pages counts those). No answer at all, or Humble busy after every try,
        stops it (ShopError.passing), as do pages that can't be read when no page could be, or more than a few of them
        (FEW_UNREAD, UNREAD_SHARE): Humble's site has changed then."""
        result: dict[str, dict] = {}
        missing: list[str] = []
        read = self.unread_pages = 0
        for n, key in enumerate(keys, 1):
            items = self.listed.get(key, [])
            if not any(_has_files(i) for i in items):
                result[key] = order_of(key, items, None)  # keys alone: nothing to read
            else:
                try:
                    result[key] = order_of(key, items, self.purchase(key))
                    read += 1
                except PageUnread:
                    self.unread_pages += 1
                    if self.unread_pages > max(FEW_UNREAD, read // UNREAD_SHARE):
                        raise
                    missing.append(key)
                except ShopError as e:
                    if e.passing or e.status == 0:
                        raise
                    missing.append(key)
            if progress:
                progress(n, len(keys))
        if self.unread_pages and not read:
            raise PageUnread(CHANGED.format("a purchase's page holds no list of its files"))
        return result, missing

    def purchase(self, key: str) -> dict:
        """A purchase's page as Humble's router asks for it: its userLibraryItem, with every item's files."""
        r = self._get(f"/home/library/{quote(key, safe='')}", "a purchase", accept="text/x-component", rsc=True)
        self.actions.seen(r.text)  # the link's action is in a script it lists
        item = find(rows(r.content), "userLibraryItem")
        if not isinstance(item, dict) or not isinstance(item.get("entitlements"), dict):
            raise PageUnread(CHANGED.format("a purchase's page holds no list of its files"))
        return item

    def link_set(self, entitlement: str) -> tuple[dict, float]:
        """An item's links, as its own page asks for them: {subproduct machine name: {download machine name: {file
        name: link}}}, and how many seconds they last. A refusal while the login is still taken (session-status) says
        Humble's site has changed; one after it has run out, that the login has."""
        path = f"/home/library/content/{quote(entitlement, safe='')}"
        answer = self.actions.call(LINK, path, content=json.dumps([[entitlement]]).encode(), tree=content_tree(entitlement),
                                   what="a download link")
        if not isinstance(answer, dict):
            raise ShopError(CHANGED.format("its answer for a download link"))
        if not answer.get("ok"):
            code = re.sub(r"[^a-z_]", "", str(answer.get("code") or "").lower())[:40] or "no reason given"
            if code == "not_authenticated":
                self.check()  # the login run out raises AuthError here
            if answer.get("retryable"):
                raise ShopError(f"Humble Bundle made no download link ({code}) this time. Running it again asks again.", 400)
            raise ShopError(f"Humble Bundle made no download link ({code}): Hylly may need an update.", 400)
        data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        links = data.get("signedUrls")
        if not isinstance(links, dict):
            raise ShopError(CHANGED.format("its answer for a download link"))
        return links, _seconds_left(data.get("expiresAt"))

    def _get(self, path: str, what: str, *, accept: str, rsc: bool = False) -> httpx.Response:
        """The page, or an answer of Humble's; asked as the router asks for it (rsc), with Next.js's check value, which
        the site's first redirect to the same page gives. Sent on to the same page otherwise: Humble's site changed."""
        headers = {"Accept": accept, **({"RSC": "1"} if rsc else {})}
        for _ in range(2):
            ask = {"_rsc": self.rsc} if rsc and self.rsc is not None else None
            r = self.session.request("GET", path, headers=headers, params=ask)
            if not r.is_redirect:
                return _answered(r, what)
            mark = r.request.url.join(r.headers.get("location", "")).params.get("_rsc")
            if not rsc or mark is None or mark == self.rsc:
                break
            self.rsc = mark
        raise ShopError(CHANGED.format(f"it sends its page for {what} on to itself"))


class PageUnread(ShopError):
    """A purchase's page that holds no list of its files (HumbleClient.purchase): Humble's own error page, say."""


class Actions:
    """The page's server actions by name, their ids found in the scripts its pages list, each script read once a run:
    the list's in the library page's, the link's in those a purchase's page listed (seen), else a title's page's. An id
    Humble no longer knows (its site changed since they were found) is looked for once again."""

    def __init__(self, client: HumbleClient):
        self.client, self.ids, self.read, self.scripts = client, {}, set(), []
        self.again: set[str] = set()  # the actions looked for again this run: once each

    def seen(self, page: str) -> None:
        """The scripts a page listed, for the link's action."""
        self.scripts = list(dict.fromkeys(SCRIPT.findall(page)))

    def call(self, name: str, path: str, *, what: str, files: list | None = None, content: bytes | None = None,
             tree: str = "") -> object:
        """The action's answer, as its rows read (rows): the value its first row points to."""
        for again in (False, True):
            headers = {"Next-Action": self.id(name, path), "Accept": "text/x-component"}
            if tree:
                headers["Next-Router-State-Tree"] = tree
            if content is not None:
                headers["Content-Type"] = "text/plain;charset=UTF-8"
            r = self.client.session.request("POST", path, headers=headers, files=files, content=content)
            if r.status_code == 404 and not again and name not in self.again:  # an action it doesn't know: its site
                self.again.add(name)  # changed since; once a run, as a page that is gone answers 404 as well
                self.ids.pop(name, None)
                self.read.clear()  # its scripts read again, whatever their names
                continue
            return answer_of(rows(_answered(r, what).content), what)
        raise AssertionError("unreachable")

    def id(self, name: str, path: str) -> str:
        if name not in self.ids:
            for scripts in ([self.scripts] if name == LINK and self.scripts else []) + [None]:  # None: the page's own
                if self._find(name, self._page(path) if scripts is None else scripts):
                    break
            else:
                raise ShopError(CHANGED.format(f"no {name} in its pages' scripts"))
        return self.ids[name]

    def _page(self, path: str) -> list[str]:
        r = self.client._get(path, "a page of its library", accept="text/x-component", rsc=True)
        return list(dict.fromkeys(SCRIPT.findall(r.text)))

    def _find(self, name: str, scripts: list[str]) -> bool:
        """Whether one of the scripts names the action: the last first, as a page lists its own last."""
        for script in reversed(scripts):
            if name in self.ids:
                break
            if script in self.read:
                continue
            self.read.add(script)
            r = self.client._get(script, "a script of its site", accept="*/*")
            for found, called in ACTION.findall(r.content):
                self.ids.setdefault(called.decode(), found.decode())
        return name in self.ids


def _same_page(r: httpx.Response) -> bool:
    """A redirect to the very page asked for (as Next.js adds its _rsc check value): no refusal of the login."""
    try:
        to = r.request.url.join(r.headers.get("location", ""))
    except (httpx.InvalidURL, RuntimeError):
        return False
    return to.host == r.request.url.host and to.path == r.request.url.path


def _answered(r: httpx.Response, what: str) -> httpx.Response:
    """The answer, when it's one to read: busy after every try, or any other error, a ShopError that says so."""
    if busy(r.status_code):  # still, after asking again, or asking to wait longer than a run waits
        raise ShopError(f"Humble Bundle answered HTTP {r.status_code} when asked for {what}. Try again later.", r.status_code)
    if r.status_code >= 400:
        raise ShopError(f"Humble Bundle answered HTTP {r.status_code} when asked for {what}.", r.status_code)
    return r


def _json_of(r: httpx.Response, what: str):
    if "json" not in r.headers.get("content-type", ""):
        raise AuthError(EXPIRED)  # its sign-in page, say
    try:
        return r.json()
    except ValueError:
        raise ShopError(CHANGED.format(f"its answer for {what} isn't JSON")) from None


def _number(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _seconds_left(expires) -> float:
    """How long a set of links lasts, by its expiresAt; two hours when it says none (kept for one, LINK_EARLY)."""
    from datetime import datetime

    try:
        return max(0.0, datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp() - time.time())
    except ValueError:
        return 2 * LINK_EARLY


def _list_form(page: int) -> list:
    """The library page's own form for a page of its list (filterUserLibrary): newest first, everything, LIMIT a page;
    the state before it (the action's first argument) empty, as the page's first asks it."""
    fields = {"requestId": "1", "query": "", "sort": "mostRecent", "cursor": str(LIMIT * page), "page": str(page),
              "limit": str(LIMIT), "hideRevealed": "false", "hideExpired": "false", "dateExpires": ""}
    before = [{"results": [], "pagination": {"page": 1, "totalPages": 1, "totalDocs": 0}, "success": True}, "$K1"]
    return [(f"_1_{k}", (None, v)) for k, v in fields.items()] + [("0", (None, json.dumps(before)))]


def content_tree(entitlement: str) -> str:
    """The router's state on a title's page (Next-Router-State-Tree), as the page sends it with its actions."""
    def node(segment, child, flags: int) -> list:
        return [segment, {"children": child}, None, None, flags]

    tree = ["__PAGE__", {}, None, None, 0]
    for segment, flags in ((["id", entitlement, "d", None], 0), ("content", 0), ("library", 4), ("home", 8),
                           ("(account)", 8), ("(default)", 8), ("", 24)):
        tree = node(segment, tree, flags)
    return quote(json.dumps(tree, separators=(",", ":")), safe="")


def _has_files(item: dict) -> bool:
    """Whether a listed item has files to download (not keys alone)."""
    methods = item.get("deliveryMethods")
    return item.get("type") == "download_drm_free" or (isinstance(methods, list) and "download" in methods)


# ---- the pages' rows (React's flight rows, as Next.js sends a page's data and an action's answer) ---------------------

def rows(body: bytes) -> dict[str, object]:
    """A page's or an action's rows by id: <id>:<JSON> a line each, or a text, tagged T with its length in bytes, which
    may hold line ends. Rows of other tags (scripts, hints, errors) are kept as None; an error row is said by answer_of."""
    out: dict[str, object] = {}
    i, n = 0, len(body)
    while i < n:
        colon = body.find(b":", i)
        end = body.find(b"\n", i)
        if colon < 0 or (0 <= end < colon) or not re.fullmatch(rb"[0-9a-f]+", body[i:colon]):
            i = n if end < 0 else end + 1  # not a row: the next line
            continue
        rid, j = body[i:colon].decode(), colon + 1
        sized = re.match(rb"([A-Za-z])([0-9a-f]+),", body[j:j + 20])
        if sized:  # a text of so many bytes (T), or a binary one (another letter), which may hold line ends
            start, length = j + sized.end(), int(sized[2], 16)
            out[rid] = body[start:start + length].decode("utf-8", "replace") if sized[1] == b"T" else None
            i = start + length
            continue
        end = n if end < 0 else end
        line = body[j:end]
        if line[:1] == b"E":
            out[rid] = _Error()
        elif line[:1].isupper():  # a script, a hint, a mark of the stream's own
            out[rid] = None
        else:
            try:
                out[rid] = json.loads(line)
            except ValueError:
                out[rid] = None
        i = end + 1
    return out


class _Error:
    """An error row: the action failed at Humble's end."""


def plain(value, table: dict, depth: int = 0):
    """A row's value with its references read: "$undefined" none, "$$x" the text "$x", "$D<date>" the date, "$<id>"
    and "$@<id>" (with a :path) the row it names."""
    if depth > 60:
        return None
    if isinstance(value, dict):
        return {k: plain(v, table, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v, table, depth + 1) for v in value]
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    if value.startswith("$$"):
        return value[1:]
    if value.startswith("$D"):
        return value[2:]
    ref = re.fullmatch(r"\$[@LQW]?([0-9a-f]+)((?::[^:]+)*)", value)
    if not ref or ref[1] not in table:
        return None  # "$undefined", a symbol, a module: nothing Hylly reads
    target = table[ref[1]]
    for part in filter(None, ref[2].split(":")):
        target = target.get(part) if isinstance(target, dict) else (
            target[int(part)] if isinstance(target, list) and part.isdigit() and int(part) < len(target) else None)
    return plain(target, table, depth + 1)


def answer_of(table: dict, what: str):
    """An action's answer: the value row 0 points to (its "a"), else row 1's."""
    first = table.get("0")
    value = first.get("a") if isinstance(first, dict) and "a" in first else table.get("1")
    if isinstance(value, str) and value.startswith("$@"):
        value = table.get(value[2:])
    if isinstance(value, _Error):
        raise ShopError(f"Humble Bundle's site failed to answer for {what}. Try again later.", 500)
    return plain(value, table)


def find(table: dict, key: str):
    """The value under key in the first row that has it, read whole (plain); None when no row does."""
    def look(value, depth=0):
        if depth > 40:
            return None
        if isinstance(value, dict):
            if key in value:
                return value[key]
            values = value.values()
        elif isinstance(value, list):
            values = value
        else:
            return None
        for v in values:
            hit = look(v, depth + 1)
            if hit is not None:
                return hit
        return None

    for value in table.values():
        hit = look(value)
        if hit is not None:
            return plain(hit, table)
    return None


# ---- the order API's shape, from the site's ---------------------------------------------------------------------------

def order_of(key: str, items: list[dict], page: dict | None) -> dict:
    """A purchase as the order API gave it, which the plan, the catalog and the snapshots read (inventory.parse_orders):
    each listed item a subproduct, by its contentId (machine_name) and entitlement id (entitlement), with its downloads
    from the purchase's page, each file's link its name alone (url.web: no link lasts); the bundle's name; no date,
    publisher or cover, which remap takes from the newest snapshot. A field of another type than the site's is taken as
    none, so that a change at Humble's end shows as titles or files missing, never as a crash."""
    on_page = {}
    for e in _list((_dict(page).get("entitlements") or {}).get("downloads") if isinstance(_dict(page).get("entitlements"), dict) else []):
        if _text(e.get("id")):
            on_page[e["id"]] = e
    subs = []
    for item in items:
        ent = on_page.get(_text(item.get("id"))) or {}
        downloads = []
        for d in _list(ent.get("downloads")):
            files = [{"name": _text(s.get("name")), "url": {"web": _text(_dict(s.get("url")).get("web"))},
                      "md5": _text(s.get("md5")) or None, "sha1": _text(s.get("sha1")) or None,
                      "file_size": _number(s.get("fileSize")) or _number(s.get("size"))}
                     for s in _list(d.get("downloadStruct"))]
            downloads.append({"machine_name": _text(d.get("key")), "platform": _text(d.get("platform")),
                              "download_struct": files})
        subs.append({"machine_name": _text(item.get("contentId")) or _text(item.get("id")),
                     "human_name": _text(item.get("title")) or _text(ent.get("title")), "entitlement": _text(item.get("id")),
                     "payee": {}, "icon": None, "url": "", "downloads": downloads,
                     **({"unlisted": True} if page is not None and _has_files(item) and not downloads else {})})
    bundle = next((_text(i.get("subTitle")) for i in items if _text(i.get("subTitle"))), "") or _text(_dict(page).get("title"))
    return {"gamekey": key, "entitlement": key, "created": "", "product": {"human_name": bundle}, "subproducts": subs}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list[dict]:
    """The dicts of a list; [] for anything else."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _loose(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _md5s(sub: dict) -> set[str]:
    return {str(s.get("md5")).lower() for d in sub.get("downloads") or [] for s in d.get("download_struct") or []
            if s.get("md5")}


def remap(orders: dict, earlier: dict | None, today: str) -> tuple[dict, dict[str, str]]:
    """The purchases read from the site (order_of), keyed and named as the newest snapshot has them (earlier), so that
    each title keeps its id (h:<machine name>), its folder and its answers; and which key each purchase got. An order
    without an entitlement is the order API's own (a test's, or one carried over), and stays as it is.
    - An item's machine name: the one a snapshot from the site gave its entitlement id; else its contentId, where the
      snapshot has it; else the one machine name whose letters and digits are the same (a "-" or capitals the site
      changed) that shares a file's MD5 with it, else the one of its title (a file replaced since), or the one there
      is for an item with no files; else, renamed whole, the one machine name the site no longer lists whose item
      lists every one of its files (their MD5s), or one of them under the same title.
    - A purchase's key: the one a snapshot from the site gave it; else the order key of the snapshot's order that holds
      the most of its items (ties to the one of its bundle's name), each order key given once, never one keep_back put
      back whole; else its own id.
    - Its date and its bundle's name: that order's; else the day it was first seen (today), and the site's name. Its
      items' publisher, cover and link (an ISBN in it) are kept from the snapshot, which the site doesn't give; and an
      item the site lists with files whose purchase's page listed none (unlisted) keeps the snapshot's files.
    earlier: the snapshots' orders, the newest of each key (history), so that a poorer snapshot loses no id. Returns
    a copy: what it was given stays as it was."""
    orders = json.loads(json.dumps(orders))  # its own copy: what it was given stays as it was
    earlier = {k: o for k, o in (earlier or {}).items() if isinstance(o, dict)}
    named: dict[str, list[tuple[str, dict]]] = defaultdict(list)  # machine name -> (order key, subproduct)
    loose: dict[str, list[str]] = defaultdict(list)
    of_item: dict[str, str] = {}  # entitlement id -> machine name, from a snapshot of the site's
    of_purchase = {o["entitlement"]: k for k, o in earlier.items() if o.get("entitlement")}
    for k, o in earlier.items():
        for s in o.get("subproducts") or []:
            if isinstance(s, dict) and s.get("machine_name"):
                named[s["machine_name"]].append((k, s))
                if s["machine_name"] not in loose[_loose(s["machine_name"])]:
                    loose[_loose(s["machine_name"])].append(s["machine_name"])
                if s.get("entitlement"):
                    of_item[s["entitlement"]] = s["machine_name"]
    fresh = {k: o for k, o in orders.items() if isinstance(o, dict) and o.get("entitlement")}
    exact = {s.get("machine_name") for o in fresh.values() for s in o.get("subproducts") or []}
    owners: dict[str, set[str]] = defaultdict(set)  # a file's MD5 -> the machine names whose items list it
    for m, entries in named.items():
        for md5 in set().union(*(_md5s(s) for _, s in entries)):
            owners[md5].add(m)

    def machine(sub: dict) -> str:
        given = sub.get("machine_name") or ""
        if sub.get("entitlement") in of_item:
            return of_item[sub["entitlement"]]
        if not given or given in named:
            return given
        near, mine = [m for m in loose.get(_loose(given), []) if m not in exact], _md5s(sub)
        sure = [m for m in near if mine & set().union(*(_md5s(s) for _, s in named[m]))]
        titled = [m for m in near if any(_loose(s.get("human_name") or "") == _loose(sub.get("human_name") or "")
                                         for _, s in named[m])]
        for found in (sure, titled, near if not mine else []):  # a file of its own; else its title; with no files, the one
            if len(found) == 1:
                return found[0]
        same = {m for md5 in mine for m in owners.get(md5, ()) if m not in exact}  # renamed whole: by its very files,
        same = {m for m in same if mine <= set().union(*(_md5s(s) for _, s in named[m])) or any(  # every one, or its title
            _loose(s.get("human_name") or "") == _loose(sub.get("human_name") or "") for _, s in named[m])}
        return next(iter(same)) if len(same) == 1 else given

    for o in fresh.values():
        for s in o.get("subproducts") or []:
            s["machine_name"] = machine(s)
    # each purchase's key: the snapshot's order holding the most of its items, an order key given once
    keys: dict[str, str] = {k: of_purchase[k] for k in fresh if k in of_purchase}
    taken = set(keys.values())
    scores = []
    for k, o in fresh.items():
        if k in keys:
            continue
        held = Counter(ok for s in o.get("subproducts") or [] for ok, _ in named.get(s["machine_name"], [])
                       if not earlier[ok].get("entitlement") and not earlier[ok].get("kept_back"))
        bundle = (o.get("product") or {}).get("human_name") or ""
        for ok, count in held.items():
            same = ((earlier[ok].get("product") or {}).get("human_name") or "") == bundle
            scores.append((-count, not same, ok, k))
    for _, _, ok, k in sorted(scores):
        if k not in keys and ok not in taken:
            keys[k] = ok
            taken.add(ok)
    out = {k: o for k, o in orders.items() if k not in fresh}
    for k, o in fresh.items():
        key = keys.get(k, k)
        before = earlier.get(key) or {}
        o["gamekey"], o["created"] = key, before.get("created") or o.get("created") or today
        for name in ("machine_name", "human_name"):
            if (before.get("product") or {}).get(name):
                o["product"][name] = before["product"][name]
        mine = {s.get("machine_name"): s for s in before.get("subproducts") or [] if isinstance(s, dict)}
        for s in o.get("subproducts") or []:
            was = mine.get(s["machine_name"]) or next((x for _, x in named.get(s["machine_name"], [])), {})
            for name in ("payee", "icon", "url", "library_family_name"):
                if was.get(name):
                    s[name] = was[name]
            if s.pop("unlisted", False):
                if any(d.get("download_struct") for d in was.get("downloads") or [] if isinstance(d, dict)):
                    s["was_unlisted"] = True  # said by the run (unlisted), and kept with the snapshot's files
                    s["downloads"] = was["downloads"]
                else:  # no snapshot had files of it either: nothing lost, said in a line of its own (fileless)
                    s["listed_without_files"] = True
            s["human_name"] = s.get("human_name") or was.get("human_name") or ""
        out[key] = o
    return out, keys


# ---- Humble on the shops' core ------------------------------------------------------------------------------------

def kind_order() -> dict[str, int]:
    """The kinds in the order a run fetches them: the rules' (kinds.toml), the most general first, as the small and
    useful are (books, tech books, RPG books, then comics)."""
    from . import rules

    return {name: n for n, name in enumerate(rules.current().kinds)}


def found_kinds() -> list[str]:
    """The kinds the plan can find a Humble title to be, in the rules' order: a book, and those its words or a format's
    layout make ([kinds.X] in kinds.toml). The summary has a line for each, and for any other a title has (yours)."""
    from . import rules

    return [k.name for k in rules.current().kinds.values() if k.name == rules.DEFAULT_KIND or k.words or k.publishers
            or k.layout]


@dataclass
class File(shopping.File):
    """One of a title's files as Humble's plan takes it, for the shops' core: its chosen file, with the title's other
    formats behind it (fallbacks: tried, best first, when Humble's own file is at fault), or an extra. Its key is its
    name in the title's folder (plan.free_name), as its records name it (file); its name Humble's (inventory.link_name),
    by which a fresh link finds it again."""
    ext: str = ""  # its format: by its name, or by its label where its name has none (inventory.file_ext)
    url: str = ""  # its link in the orders read at the start of the run, fresh: tried before a fresh one is asked for
    where: str = ""  # where it is best read: its title's (plan.device), an extra's too; one behind it, by its own format
    fallbacks: list[File] = field(default_factory=list)
    # (order key, machine name, its name there): another purchase of the title, one Humble answered for, that lists this
    # very file, whose link it takes when Humble gave no data for its own purchase this time (Library.titles)
    borrow: tuple = ()
    # the plan unsure which of the title's files to keep (plan.unsure), on its chosen file: (the files it chose between,
    # as a question's options; the one it took; why), which the core puts to you (shopping.select); () when it is sure
    unsure: tuple = ()
    reopen: bool = False  # the file you kept as the rules took it is offered among others now: asked again (plan.decide)

    def device(self, kind: str, ext: str) -> str:
        """Where it is best read, as the plan says (where), not by its size alone: an extra is read where its book is."""
        return self.where


@dataclass
class Title(shopping.Title):
    """One title of Humble's plan, for the shops' core: its key the downloaded purchase's machine name (its catalog id's,
    h:<machine name>; "" for an item with none), its own folder by the plan, its chosen file and its extras."""
    folder: str = ""  # by the plan (plan.assign_folders): where any of its purchases' files are, else a new one
    order: str = ""  # the order key of the purchase its files come from: its fresh links are found in it
    device: str = ""  # where it is best read (plan.device): --device chooses by it
    why: str = ""  # why it is this kind, has this file and is read there, as the plan says: for a dry run's --json
    planned: object = field(default=None, repr=False, compare=False)  # the plan's title (plan.Title), for the summary
    also: tuple = ()  # its other purchases' machine names: your answers given under any of them count (shopping.select)


def titles_of(planned) -> list[Title]:
    """The titles of a plan (plan.Plan) as the shops' core takes them, in the order a run fetches them (Title.place):
    small and useful kinds first (kind_order), then oldest purchase first, by bundle and folder. Each file named in its
    title's folder as the plan has it: the chosen file first, then the extras, then the other formats, each a name its
    folder hasn't got yet (plan.free_name)."""
    from .paths import safe_filename
    from .plan import device, free_name

    out, order = [], kind_order()
    for t in planned.titles:
        p, names = t.product, set()

        def file(f, role: str, where: str) -> File:
            return File(key=free_name(safe_filename(f.filename), names), name=f.filename, size=f.size, md5=f.md5,
                        label=f.label, role=role, ext=f.ext, url="", where=where)  # links come from link alone: no
                                                                                   # listing or snapshot has one that works

        chosen = file(t.choice, "book", t.device)
        extras = [file(x, "extra", t.device) for x in t.extras]
        chosen.fallbacks = [file(f, "book", device(t.kind, f)[0]) for f in t.alternatives]
        took = next((n for n, f in enumerate(t.options) if f is t.choice), None)
        if t.unsure and p.machine_name and took is not None:  # a question, by its Humble id
            chosen.unsure, chosen.reopen = ([option(f) for f in t.options], took, unsure_why(t)), t.reopen
        out.append(Title(
            key=p.machine_name, name=p.title, by=p.publisher, files=[chosen, *extras], kind=t.kind, purchased=p.purchased,
            under=p.bundle, place=(order.get(t.kind, len(order)), p.purchased, p.bundle.lower(), t.folder.lower()),
            folder=t.folder, order=p.gamekey, device=t.device, found=t.kind,  # the plan's: by every format offered
            why="; ".join(r for r in (t.kind_reason, t.choice_reason, t.device_reason) if r), planned=t,
            also=tuple(dict.fromkeys(c.machine_name for c in t.copies if c.machine_name and c.machine_name != p.machine_name))))
    return out


def option(f) -> dict:
    """One of Humble's files (inventory.FileOption) as a question's option (curation.file_option)."""
    from .curation import file_option

    return file_option(f.filename, f.ext, f.size, f.label)


def unsure_why(t) -> str:
    """Why the plan was unsure of a title's file (plan.unsure), in a few words."""
    from . import settings

    if t.unsure == "none":
        return (f"none of the formats on its kind's list ({', '.join(settings.formats_for(t.kind)[0])}: [download] in the "
                f"settings) is offered, so the largest file was taken")
    return f"Humble offers {len(t.options)} {t.choice.ext.upper()} files, and the largest was taken"


def items_to_ask(planned, answers: dict) -> list[dict]:
    """The items of your orders the plan leaves out that may be titles after all (is it a title?): each with a
    Humble id and no book of its own, all its files extras (an art book, a PDF labelled a bonus) or of a format the
    rules don't know; an item with any file the rules know as no book (a program, a game's, music or video) is none, as
    its other files go with those; nor the title of any of its purchases. Its options are those files, and leaving it
    out, the guess; one you answered so, while it offers the same files, is none (answers: by catalog id). Each item
    once, by its Humble id."""
    from . import curation, rules
    from .download import kind_of, never

    r, out, seen = rules.current(), [], {c.machine_name for t in planned.titles for c in t.copies}
    for p in planned.products:
        if p.books or not p.machine_name or p.machine_name in seen:
            continue
        seen.add(p.machine_name)
        files = p.files
        if not files or any(f.role not in ("extra", "other") or never(kind_of(f.filename)) or f.ext in r.audio_formats
                            or f.ext in r.video_formats for f in files):
            continue
        options = [option(f) for f in files] + [curation.LEAVE_OUT]
        if curation.settled(answers.get(f"h:{p.machine_name}"), "title", "", options):
            continue
        out.append({"kind": "title", "item": p.machine_name, "file": "", "title": p.title, "options": options,
                    "guess": len(options) - 1, "held": False,
                    "why": ("its only file is no book by the rules" if len(p.files) == 1 else
                            f"none of its {len(p.files)} files is a book by the rules") + ", so it was left out"})
    return out


class Library:
    """Humble on the shops' core, opened with the login cookie: check reads the list of your purchases (Humble turns a
    login it doesn't take away there), titles each purchase with its files (read only), keyed and named as the newest
    snapshot has them (remap), by the plan for this library (plan.for_library: the formats you chose, those downloaded
    already, the folders its files are in), and link a fresh link to a file, by its item's entitlement id, each item's
    links asked for again an hour before they expire, or when one of them was refused. A purchase Humble gives no data
    for this time is taken as the newest snapshot that has it had it (carry_over), in the plan as in the snapshot saved,
    so that the run, the catalog and the check of what a run would choose see the same orders: a title of it keeps its
    folder, and one bought again in it under another machine name isn't fetched again into a new one. It is said
    (unanswered), and the run ends partly done. A file of it still to download is asked for by its item's entitlement
    id, which a snapshot made from Humble's site keeps; one from before has none, and takes the link of another
    purchase of the title that lists the very same file, else it fails, and the next run tries it again."""

    def __init__(self, session: str, root: Path, *, note: Callable[[str], None] | None = None,
                 progress: Callable[[int, int], None] | None = None, max_age: float = 12 * 3600.0):
        """note and progress: what the run says as it reads the orders. max_age: seconds an item's links are used at
        most, however long Humble says they last."""
        from .paths import state_dir

        self.client = HumbleClient(session)
        self.root, self.state = root, state_dir(root)
        self.note, self.progress, self.max_age = note or (lambda line: None), progress, max_age
        self.unreadable: list[tuple[str, str]] = []  # the items whose files their purchase's page didn't list
        self.keys: list[str] = []
        self.saved: dict = {}  # the orders as the snapshot keeps them: Humble's answers, and the carried over
        self.missing: list[str] = []  # the order keys Humble gave no data for this time
        self.left: list[str] = []  # of those, the ones no snapshot had either
        self.everything: list[Title] = []  # the titles of every purchase saved, carried over too
        self.plan = None  # theirs: the plan (plan.Plan), for what it leaves to you besides (questions)
        self.kept_back: tuple[int, int] = (0, 0)  # titles with files the site no longer lists, kept (and purchases whole)
        self._cache: dict[str, tuple[float, dict]] = {}  # an item's links, until when, by its entitlement id
        self._given: dict[tuple[str, str], float] = {}  # a file's link given, by its links' time: asked again, it was refused
        self._lock = threading.Lock()

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    def check(self) -> None:
        self.keys = self.client.order_keys()

    def titles(self, progress=None) -> list[Title]:
        """The titles of the orders as saved: Humble's answers, and those it gave no data for as the newest snapshot
        that has them (carry_over). A file of a purchase carried over has no link of its own (the snapshot keeps none
        that works): it takes the link of another purchase of the title Humble answered for that lists the very same
        file (same_file: File.borrow), else it has none, and the run asks Humble for its own purchase (link). Each order's progress as it is read (the run's own, given when it was opened), in place of a count
        of titles."""
        from .plan import for_library
        from .snapshot import latest

        self.note(f"{many(len(self.keys), 'purchase')}: reading the files of each (read only)...")
        orders, missing = self.client.orders(self.keys, progress=self.progress)
        odd = getattr(self.client, "unread_pages", 0)
        if odd:
            self.note(f"Humble's page of {many(odd, 'purchase')} held no list of its files this time (an error page of its "
                      f"own, say): taken as one Humble gave no data for.")
        listed = getattr(self.client, "listed", {})
        unread = {k: order_of(k, listed.get(k, []), None) for k in missing}  # keyed as the others are
        self.saved, keys = remap({**orders, **unread}, history(self.state), time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.missing = [keys.get(k, k) for k in missing]
        listing = {key: self.saved.pop(key) for key in self.missing if key in self.saved}  # the list's, for their ids
        self.unreadable = unlisted(self.saved)
        quiet = fileless(self.saved)
        if quiet:
            self.note(f"{many(len(quiet), 'item')} Humble lists as downloads {'has' if len(quiet) == 1 else 'have'} no files "
                      f"on {'its' if len(quiet) == 1 else 'their'} purchase's page, nor in any earlier snapshot, so there is "
                      f"nothing of {'it' if len(quiet) == 1 else 'them'} to download: "
                      + ", ".join(quiet[:3]) + (" and more." if len(quiet) > 3 else "."))
        self.left = carry_over(self.saved, self.missing, self.state)
        for key, o in listing.items():  # carried over: its items' entitlement ids from the list, where it has none
            ids = {s.get("machine_name"): s.get("entitlement") for s in o.get("subproducts") or []}
            for s in (self.saved.get(key) or {}).get("subproducts") or []:
                if isinstance(s, dict) and not s.get("entitlement") and ids.get(s.get("machine_name")):
                    s["entitlement"] = ids[s["machine_name"]]
        newest = latest(self.state, "humble")
        lost = gone(newest[1] if newest else None, self.saved)
        if lost:
            raise ShopError(CHANGED.format(f"{lost[0]} of the {lost[1]} titles with files in your newest snapshot aren't "
                                           "in what its site listed") + " Nothing was changed.")
        self.kept_back = keep_back(self.saved, newest[1] if newest and isinstance(newest[1], dict) else {})
        if self.kept_back[0]:
            self.note(f"Humble's site doesn't list {many(self.kept_back[0], 'title')} of your earlier purchases "
                      f"({many(self.kept_back[1], 'purchase')} of them whole): they keep their place in the library, as the "
                      "newest snapshot had them.")
        self.plan = for_library(self.saved, self.root)
        self.everything = titles_of(self.plan)
        for t in self.everything:
            if ((t.order in self.missing or kept(self.saved.get(t.order), t.key))  # carried over, or kept back
                    and not entitlement(self.saved.get(t.order), t.key)):  # from a snapshot made before the site changed
                answered = [(c, o) for c in t.planned.copies if c.gamekey not in self.missing for o in c.files]
                for f in t.files:
                    for x in [f, *f.fallbacks]:
                        o = next(((c, o) for c, o in answered if same_file(x, o)), None)
                        x.url = ""
                        x.borrow = (o[0].gamekey, o[0].machine_name, o[1].filename) if o else ()
        return self.everything

    @property
    def incomplete(self) -> bool:
        """Whether a purchase Humble gave no data for is in no snapshot either: its titles aren't listed this time."""
        return bool(self.left)

    @property
    def covers(self) -> tuple[str, ...]:
        """The kinds of question its own (questions) finds over the whole plan, whatever a run chooses: none while a
        purchase Humble gave no data for is in no snapshot either, whose items' questions then stay as they were."""
        return () if self.left else ("title",)

    def questions(self, answers: dict) -> list[dict]:
        """What the plan leaves to you besides the core's questions: its items that may be titles after all
        (items_to_ask). answers: yours, by catalog id (curation.answers)."""
        return items_to_ask(self.plan, answers) if self.plan is not None else []

    @property
    def unanswered(self) -> str:
        """What the run says of the purchases Humble gave no data for, and what to do; "" when it gave them all."""
        return missing_words(self.missing, self.left) if self.missing else ""

    @property
    def no_data(self) -> int:
        return len(self.missing)

    def snapshot(self, state: Path, thinned=None) -> Path:
        """The orders as the new snapshot, which the catalog reads its Humble titles from (catalog.humble_items), those
        Humble gave no data for this time as the newest snapshot that had them; the older ones thinned (snapshot.put)."""
        from .snapshot import save

        return save(self.saved, state, "humble", thinned)

    def link(self, t: Title, f: File) -> str:
        """A fresh link to the file, from its item's links (HumbleClient.link_set, by the item's entitlement id), found
        by its name as the plan read it (inventory.link_name); "" when Humble no longer lists it. An item's links are
        asked for once, and again an hour before they expire (as Humble's page does), after max_age, or for a file whose
        link was given from them already: asked again, it was refused. A file of a purchase carried over from a snapshot
        made before Humble's site changed has no entitlement id: it takes another purchase's of the title that lists the
        very same file (File.borrow); without one the file fails (logins.ShopError), and the next run, which reads its
        purchase from the site, asks again; no other format is tried in its place."""
        from .inventory import link_name

        ent, name = entitlement(self.saved.get(t.order), t.key), f.name
        if not ent and getattr(f, "borrow", ()):
            key, machine, name = f.borrow
            ent = entitlement(self.saved.get(key), machine)
        if not ent and kept(self.saved.get(t.order), t.key):
            raise ShopError("Humble's site no longer lists this title, so no link can be asked for: what your library "
                            "has of it stays.")
        if not ent:
            raise ShopError("Humble Bundle gave no data for this purchase, so no fresh link could be asked for. Running "
                            "it again asks again.")
        with self._lock:
            until, links = self._cache.get(ent, (0.0, {}))
            if time.monotonic() >= until or self._given.get((ent, name)) == until:  # run out, or refused
                links, lasts = self.client.link_set(ent)
                until = time.monotonic() + min(self.max_age, max(0.0, lasts - LINK_EARLY))
                self._cache[ent] = (until, links)
            self._given[(ent, name)] = until
        for files in links.values():
            for by_name in (files or {}).values() if isinstance(files, dict) else ():
                for given, url in (by_name or {}).items() if isinstance(by_name, dict) else ():
                    if isinstance(url, str) and url.startswith("https://") and link_name(given) == name:
                        return url  # read as the plan read the name: decoded
        return ""

    def summary(self, manifest, root: Path) -> tuple[list[str], dict]:
        """What the run says of the whole library at its end, and its --json: summary, of every title saved."""
        return summary(self.everything, manifest, root)

    def replan(self) -> None:
        """The plan made again from the orders this run read, after the run has changed your choices (a shop list that
        answered a which-file question): what its summary at the end goes by, so that it counts the file you chose.
        The files being downloaded keep the titles and links they were chosen with."""
        from .plan import for_library

        self.plan = for_library(self.saved, self.root)
        self.everything = titles_of(self.plan)


def kept(order: dict | None, machine: str) -> bool:
    """Whether the order's item with that machine name is one keep_back put back: the site no longer lists it."""
    return any(isinstance(s, dict) and s.get("machine_name") == machine and s.get("kept_back")
               for s in (order or {}).get("subproducts") or [])


def entitlement(order: dict | None, machine: str) -> str:
    """The entitlement id of the order's item with that machine name: "" for an order the order API gave, or none."""
    for sub in (order or {}).get("subproducts") or []:
        if isinstance(sub, dict) and sub.get("machine_name") == machine and sub.get("entitlement"):
            return str(sub["entitlement"])
    return ""


def unlisted(orders: dict) -> list[tuple[str, str]]:
    """The items the site lists with files whose purchase's page listed none (remap kept the snapshot's files of
    each it has), as the core says them (shopping.unread): (machine name, title)."""
    return [(s.get("machine_name") or "", s.get("human_name") or s.get("machine_name") or "an item")
            for o in orders.values() if isinstance(o, dict) and o.get("entitlement")
            for s in o.get("subproducts") or [] if s.get("was_unlisted")]


def fileless(orders: dict) -> list[str]:
    """The items the site lists as downloads whose purchase's page lists no files, as no snapshot did before (remap):
    by name, each once. Nothing of them to download or keep, and no sign that Humble's site changed: an audio
    collection, say, whose files Humble's library page doesn't give either."""
    return list(dict.fromkeys(s.get("human_name") or s.get("machine_name") or "an item"
                              for o in orders.values() if isinstance(o, dict) and o.get("entitlement")
                              for s in o.get("subproducts") or [] if s.get("listed_without_files")))


def history(state: Path) -> dict:
    """Every snapshot's orders, the newest of each key: what remap goes by, so that one snapshot poorer than the one
    before it (a purchase the site skipped once) loses no title's id."""
    from .snapshot import newest_first

    out: dict = {}
    for _, data in newest_first(state, "humble"):
        if isinstance(data, dict):
            for key, o in data.items():
                out.setdefault(key, o)
    return out


TOO_MANY_GONE = 0.5  # of the newest snapshot's titles with files: more gone from the site's list stops a run


def keep_back(saved: dict, newest: dict) -> tuple[int, int]:
    """The titles with files of the newest snapshot that the site no longer lists (by machine name, in any purchase),
    put back as it had them: in their own purchase where the site lists that purchase, else with their purchase whole
    (carried over as carry_over does). Humble's own site, rebuilt, leaves out some of the oldest
    purchases' items: the library keeps them, and their files and folders. Returns (titles, purchases whole) put back."""
    def with_files(s) -> bool:
        return isinstance(s, dict) and bool(s.get("machine_name")) and any(
            d.get("download_struct") for d in s.get("downloads") or [] if isinstance(d, dict))

    listed = {s.get("machine_name") for o in saved.values() if isinstance(o, dict) for s in o.get("subproducts") or []
              if isinstance(s, dict)}
    titles = whole = 0
    for key, o in (newest or {}).items():
        back = [s for s in (o or {}).get("subproducts") or [] if with_files(s) and s["machine_name"] not in listed]
        if not back:
            continue
        back = [{**s, "kept_back": True} for s in back]  # marked: said so, and never claimed by another purchase
        if key in saved:
            saved[key].setdefault("subproducts", []).extend(back)
        else:
            saved[key] = {**o, "subproducts": back, "kept_back": True}
            whole += 1
        listed |= {s["machine_name"] for s in back}
        titles += len(back)
    return titles, whole


def gone(newest, saved: dict) -> tuple[int, int] | None:
    """(how many, of how many) of the newest snapshot's titles with files are in none of the orders saved now, when
    that is more than TOO_MANY_GONE of them and more than 20, and at least half as many titles with files are new: what
    a change of Humble's site that Hylly misreads looks like (every title under another name), rather than purchases
    Humble no longer lists (kept back). None when it isn't so."""
    def names(orders) -> set[str]:
        return {s.get("machine_name") for o in (orders or {}).values() if isinstance(o, dict)
                for s in o.get("subproducts") or [] if isinstance(s, dict) and s.get("machine_name")
                and any(d.get("download_struct") for d in s.get("downloads") or [] if isinstance(d, dict))}

    before, now = names(newest if isinstance(newest, dict) else {}), names(saved)
    lost, new = len(before - now), len(now - before)
    return (lost, len(before)) if lost > 20 and lost > TOO_MANY_GONE * len(before) and new >= lost / 2 else None


def same_file(f: File, o) -> bool:
    """Whether another purchase's file (inventory.FileOption) is the very file f is: by its MD5 where both list one,
    else, where neither does, by its name and its size."""
    if f.md5 or o.md5:
        return bool(f.md5 and o.md5 and f.md5.lower() == o.md5.lower())
    return o.filename == f.name and o.size == f.size


def carry_over(orders: dict, missing: list[str], state: Path) -> list[str]:
    """The purchases Humble gave no data for this time (HumbleClient.orders' missing), put in as the newest snapshot that
    has each had it, looking back to the oldest, so that the snapshot saved now isn't poorer for a failed request: the
    catalog lists only the titles of the newest one. Returns the order keys still without data, which no snapshot had."""
    from .snapshot import newest_first

    left = list(dict.fromkeys(missing))
    for _, earlier in newest_first(state, "humble") if left else ():
        if isinstance(earlier, dict):
            for key in [k for k in left if isinstance(earlier.get(k), dict) and earlier[k]]:
                orders[key] = earlier[key]
                left.remove(key)
        if not left:
            break
    return left


def missing_words(missing: list[str], left: list[str]) -> str:
    """What a download says of the purchases Humble gave no data for, and what to do."""
    kept = len(missing) - len(left)
    return (f"Humble gave no data for {many(len(missing), 'purchase')} this time"
            + (f"; {kept} kept{'' if kept == 1 else ', each'} from the newest snapshot that has it" if kept else "")
            + (f"; {len(left)} {'is' if len(left) == 1 else 'are'} in no snapshot yet" if left else "")
            + ". Running it again asks Humble again.")


def summary(titles: list[Title], manifest, root: Path) -> tuple[list[str], dict]:
    """What Humble's library has and still needs, per kind, by the core's own choice (shopping.select, without the run's
    options, with your answers: a file you moved by hand isn't pending, nor one a shop list left out, which is counted
    apart): done, pending and changed on Humble, files and bytes, a kind no download takes counted nowhere; the files
    that failed last time, the partial files (those no download goes on from named: download.left_behind, the folder
    walked once), the records whose file is missing, and the files downloaded that no title chooses any more (as its
    chosen file, an extra or a format behind the chosen one); the titles with another format standing in for their
    chosen file, which every download tries once (with can_keep: whether hylly set format with that format
    would make it the chosen one, as the plan picks it from what the title offers); and the titles kept in the format
    they were downloaded in, though a better one is offered now (plan.kept_behind). What Humble's status action said
    before it went: the lines for the run to say at its end, and their --json."""
    from . import curation
    from .download import left_behind
    from .paths import state_dir
    from .plan import choose

    sel = shopping.select("humble", titles, manifest, root, answers=curation.answers(root),  # a file you moved: not pending,
                          yours=curation.left_out(root))  # nor one you left out with a shop list
    rows = {k: {"done": [0, 0], "pending": [0, 0], "changed": [0, 0]} for k in found_kinds()}
    for t, f, _, state in sel.every:
        cell = rows.setdefault(t.kind, {"done": [0, 0], "pending": [0, 0], "changed": [0, 0]})[state]
        cell[0] += 1
        cell[1] += f.size
    takes = set(curation.single("format"))

    def can_keep(t: Title, instead: File) -> bool:
        if t.planned is None or instead.ext.lower() not in takes:  # without its plan, none can be told
            return False
        best, _ = choose(t.kind, t.planned.product.books, prefer=instead.ext.lower())  # as set format's choice is read
        return (best.filename, best.ext, best.size, best.md5) == (instead.name, instead.ext, instead.size, instead.md5)

    substitutes = [{"title": t.name, "id": f"h:{t.key}", "chosen": path, "chosen_format": f.ext.lower(), "chosen_label": f.label,
                    "instead": there, "instead_format": instead.ext.lower(), "instead_label": instead.label,
                    "can_keep": can_keep(t, instead)} for t, f, path, instead, there in sel.standing]
    # a title's files, and the formats behind its chosen one (one may have come in its place, or stay after it came)
    planned = {f"{t.folder}/{x.key}" for t in titles for f in t.files for x in [f, *f.fallbacks]}
    missing = sum(1 for p in manifest.entries if not (root / p).is_file())
    extra = sum(1 for p in manifest.entries if p not in planned and (root / p).is_file())
    partial, left_words = left_behind("humble", titles, manifest, root, sel=sel)  # humble\ walked once, for both
    parts = partial["partial_files"]
    failed = 0
    failures = state_dir(root) / SHOPS["humble"].failures
    if failures.is_file():
        try:
            with failures.open(encoding="utf-8-sig", newline="") as fh:
                failed = sum(1 for _ in csv.DictReader(fh))
        except (csv.Error, ValueError):
            failed = 0  # a file cut short: the next run writes it anew
    behind = [t for t in titles if t.planned is not None and t.planned.better]
    data = {"kinds": {kind: {state: {"files": cell[0], "bytes": cell[1]} for state, cell in r.items()} for kind, r in rows.items()},
            "failed_last_time": failed, **partial, "missing_on_disk": missing, "no_longer_chosen": extra,
            "left_out_by_you": sel.yours, "substitutes": substitutes,
            "kept_behind": [{"title": t.name, "id": f"h:{t.key}", "kept_format": t.planned.choice.ext,
                             "better_format": t.planned.better} for t in behind]}

    lines = [f"{'kind':6} {'done':>18} {'pending':>18} {'changed on Humble':>20}"]
    for kind, r in rows.items():
        lines.append(
            f"{kind:6} {r['done'][0]:6} {gb(r['done'][1]):>11} {r['pending'][0]:6} {gb(r['pending'][1]):>11}"
            f" {r['changed'][0]:8} {gb(r['changed'][1]):>11}"
        )
    lines.append(f"failed last time: {failed}   partial files: {parts}   recorded but missing on disk: {missing}   "
                 f"downloaded but no longer chosen: {extra}" + (f"   left out by you: {sel.yours}" if sel.yours else ""))
    if substitutes:
        n, kept = len(substitutes), sum(s["can_keep"] for s in substitutes)
        lines.append(f"another format in place of the chosen file: {many(n, 'title')}, counted as pending. "
                     "Every hylly humble download gives the chosen file one try; the one that came instead stays meanwhile.")
        if kept:
            lines.append(f"  To keep the one that came for good ({kept} of them): hylly set format FORMAT --id ID "
                         "(hylly humble download --dry-run --json lists each title, its id and the format that came).")
        if kept < n:
            lines.append(f"  {n - kept} of them can't be kept that way, as hylly set format with the format that came would "
                         "choose another file, or doesn't take that format: their chosen file goes on being tried once a run.")
    if behind:  # a better format offered now, which nothing downloads by itself (plan.kept_behind)
        lines.append(f"{many(len(behind), 'title')} stay{'s' if len(behind) == 1 else ''} in the format downloaded before, "
                     "though one earlier on the list under [download] is offered now: hylly set format FORMAT --id ID "
                     "downloads it for one (hylly humble download --dry-run --json lists each title, its id, its format "
                     "and the better one).")
    return lines + left_words, data  # the partial files no download goes on from, named (download.left_behind)
