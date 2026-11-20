"""itch.io (itch.io): the books, comics and tabletop games in your itch.io library, on the shops' core (shopping.py):
what is its own is its API session, the items and their files as the API lists them, the answers kept about each
item's files, and the three things the core asks of it (Library), with itch.io's own rules for which items and files a
download takes. What the library and the bundles hold, counted before anything is claimed or downloaded, and the
bundles' items claimed, are in itchio_survey.py.

How the site works (seen 2026-11-02; the API the itch app uses, which itch-dl and others use too):
- An API key, made under Settings > API keys, is the Authorization header (bare) of every api.itch.io call.
- GET profile: the account. GET profile/owned-keys?page=N: the library, 50 a page, until an empty page. An
  owned key has its id (the download key) and its game: id, title, short_text, url, classification (the
  creator's choice: game, tool, assets, game_mod, physical_game for tabletop games, soundtrack, comic, book or
  other), min_price (cents), cover_url, published_at and user (the creator).
- GET games/<id>: the same game record, for any game, owned or not. GET games/<id>/uploads, with
  download_key_id=<owned key id> for one you own: the files: id, filename, size, md5_hash, type (default, book,
  soundtrack, graphical_assets...), storage ("hosted"; anything else is a link elsewhere that can't be fetched)
  and traits (p_windows and the like: a program; a list, or a dict when there are none). For a paid game you
  don't own, "uploads" is an empty dict: its files can't be seen until it is claimed.
- GET uploads/<id>/download?download_key_id=<owned key id>: a redirect to the file on itch.io's storage (as
  itch-dl uses it). The link lives a short while, so it is asked for right before each download, and the
  answer's body is never read: only where it points. Links are never logged or saved.
- The bundles aren't in the API: their pages are itchio_survey.py's.
- None of this is documented, so it may change: requests are paced (one a second), retried, and never logged with
  their parameters.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

import httpx

from . import db, shopping
from .exits import INTERRUPTED, PARTLY, Exit
# Kinds a download never takes, whatever the rules say, are every shop's (download.never), and a file's kind is the
# extension of the name it is saved under (download.kind_of)
from .download import NEVER, kind_of, never  # noqa: F401  (NEVER: as the tests name it)
from .logins import AuthError, ShopError
from .shops import SHOPS
from .shopsession import TRIES, ShopSession
from .storage import Records, append_line
from .words import many, time_words

API_URL = "https://api.itch.io/"
MAX_PAGES = 2000  # of the library, 50 items each: far more than anyone owns
FAILS_IN_A_ROW = 3  # questions about items itch.io doesn't answer one after another: it's itch.io, so asking stops
REFUSED = ("itch.io did not accept the API key. Make a new one (itch.io > Settings > API keys > Generate new "
           "API key) and run 'hylly login itchio' again.")

GROUPS = {  # itch.io's classification: the survey's group (None: software, its files are not looked into)
    "book": "book", "comic": "comic", "physical_game": "tabletop", "other": "other", "game": "game",
    "tool": None, "assets": None, "game_mod": None, "soundtrack": None,
}
WANTED = ("book", "comic", "tabletop")  # what the library is for; "other" may hold zines, "game" now and then a rulebook
# Which files are books, which are programs and which kinds a download takes are rules: formats.toml (a book's own
# format, [media] programs, and [itchio] fetch).
PLATFORMS = {"p_windows", "p_linux", "p_osx", "p_android"}
SKIP_TYPES = {"soundtrack", "audio_assets", "video", "graphical_assets", "sourcecode", "mod", "flash", "unity", "java", "html"}
PROGRAM, NOT_LISTED = "a program", "not on the list to fetch"  # why a download leaves a file


@dataclass
class Upload:
    id: int
    filename: str
    size: int = 0  # 0: not listed (a link elsewhere has none)
    md5: str = ""
    type: str = ""  # itch.io's: default, book, soundtrack, graphical_assets, ...
    storage: str = "hosted"
    traits: list[str] = field(default_factory=list)

    @property
    def ext(self) -> str:
        return kind_of(self.filename)

    @property
    def program(self) -> bool:
        return never(self.ext) or bool(PLATFORMS & set(self.traits))

    @property
    def elsewhere(self) -> bool:
        return self.storage != "hosted"

    def left(self, kind: str | None = None) -> str:
        """Why hylly itchio download leaves this file, or "" when it takes it. kind: another name's, such as the one
        it was saved under before. Only the kinds on the list ([itchio] fetch) are taken, and of those never a
        program (download.never: the rules' [media] programs, and NEVER, which no rules file changes), nor music,
        video or art assets by itch.io's own type."""
        from . import rules

        r = rules.current()
        kind = self.ext if kind is None else kind
        if never(kind) or PLATFORMS & set(self.traits):
            return PROGRAM
        if kind not in r.itchio_fetch:
            return NOT_LISTED
        return f"itch.io's type {self.type}" if self.type in SKIP_TYPES else ""


@dataclass
class Item:
    game_id: int
    title: str
    creator: str
    classification: str = ""  # "": not known (a bundle item with programs isn't asked about)
    key_id: int = 0  # the owned key: the handle for its files; 0 if not claimed
    url: str = ""
    short_text: str = ""
    cover: str = ""
    published: str = ""  # YYYY-MM-DD
    price: int = 0  # itch.io's min_price, in cents: 0 is free
    uploads: list[Upload] | None = None  # None: not known (not asked, or not visible before claiming)
    bundles: list[str] = field(default_factory=list)
    listed_files: int = 0  # "N files" on the bundle's page
    platforms: list[str] = field(default_factory=list)  # "Windows" and the like, from the bundle's page

    @property
    def claimed(self) -> bool:
        return bool(self.key_id)

    @property
    def group(self) -> str | None:
        if not self.classification:
            return "program" if self.platforms else "other"
        return GROUPS.get(self.classification, "other")

    def files(self) -> list[Upload]:
        """The files worth keeping: on itch.io itself, and not a program."""
        return [u for u in self.uploads or [] if not u.elsewhere and not u.program]

    def size(self) -> int:
        return sum(u.size for u in self.files())

    def only_reading(self) -> bool:
        """Every file is a book or a comic: a rulebook filed as a game, say. (A book's own format in formats.toml:
        a book with a layout, not a ZIP that could hold anything.)"""
        from . import rules

        readable = rules.current().readable
        files = [u for u in self.uploads or [] if not u.elsewhere]
        return bool(files) and all(u.ext in readable for u in files)


def parse_upload(raw: dict) -> Upload:
    traits = raw.get("traits") or []
    if isinstance(traits, dict):
        traits = [k for k, v in traits.items() if v]
    return Upload(id=int(raw["id"]), filename=str(raw.get("filename") or raw.get("display_name") or f"file {raw['id']}"),
                  size=int(raw.get("size") or 0), md5=str(raw.get("md5_hash") or ""), type=str(raw.get("type") or ""),
                  storage=str(raw.get("storage") or "hosted"), traits=[str(t) for t in traits])


def parse_game(game: dict, key_id: int = 0) -> Item:
    user = game.get("user") or {}
    return Item(game_id=int(game["id"]), key_id=key_id, title=str(game.get("title") or "Untitled").strip(),
                creator=str(user.get("display_name") or user.get("username") or "Unknown").strip(),
                classification=str(game.get("classification") or "other"), url=str(game.get("url") or ""),
                short_text=str(game.get("short_text") or "").strip(), cover=str(game.get("cover_url") or ""),
                published=str(game.get("published_at") or game.get("created_at") or "")[:10],
                price=int(game.get("min_price") or 0))


def parse_owned(raw: dict) -> Item:
    return parse_game({"id": raw["game_id"], **(raw.get("game") or {})}, key_id=int(raw["id"]))


class Itch:
    """The API session: paced, retried, and with typed problems instead of raw errors (which would carry the URL). A
    call a second at most, however many downloads ask, and a file's link asked one at a time, its tries too."""

    def __init__(self, key: str, transport: httpx.BaseTransport | None = None, delay: float = 1.0,
                 backoff: float = 2.0, sleep=None, tries: int = TRIES):
        self.session = ShopSession("itch.io", API_URL, pace=delay, backoff=backoff, tries=tries, transport=transport,
                                   sleep=sleep, headers={"Accept": "application/json", "Authorization": key},
                                   refused_when=lambda r: r.status_code in (401, 403), refused=REFUSED)
        self.http = self.session.http
        self._links = threading.Lock()  # downloads ask for links from more than one thread: still one at a time

    def __enter__(self) -> Itch:
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self.session.request("GET", path, params=params)

    def _json(self, r: httpx.Response, what: str) -> dict:
        """The answer's JSON; for anything else a ShopError with the status, 200 for one that isn't JSON: itch.io did
        answer, so it's no passing trouble (ShopError.passing)."""
        if r.status_code != 200:
            raise ShopError(f"itch.io answered HTTP {r.status_code} for {what}.", r.status_code)
        try:
            data = r.json()
        except json.JSONDecodeError:
            raise ShopError(f"itch.io sent something other than JSON for {what}.", 200) from None
        if isinstance(data, dict) and data.get("errors"):
            if any("authentication" in str(e).lower() or "api key" in str(e).lower() for e in data["errors"]):
                raise AuthError(REFUSED)
            raise ShopError(f"itch.io answered {what} with an error: {'; '.join(map(str, data['errors']))[:200]}", r.status_code)
        return data if isinstance(data, dict) else {}

    def profile(self) -> dict:
        return self._json(self._get("profile"), "your profile").get("user") or {}

    def owned(self, progress=None) -> list[Item]:
        """The library: every owned key, page by page until a page that is empty or not full. A page the same as the
        one before, or more pages than any library has, stops it: itch.io would be ignoring the page asked for."""
        items, page, before = [], 1, None
        while True:
            data = self._json(self._get("profile/owned-keys", {"page": page}), f"page {page} of your library")
            batch = data.get("owned_keys") or []
            if not batch:
                return items
            ids = [raw.get("id") for raw in batch]
            if ids == before:
                raise ShopError(f"itch.io sent page {page} of your library the same as page {page - 1}, so reading it stopped.")
            if page > MAX_PAGES:
                raise ShopError(f"Your itch.io library went on past {MAX_PAGES} pages, so reading it stopped.")
            items += [parse_owned(raw) for raw in batch]
            if progress:
                progress(len(items))
            per_page = data.get("per_page")
            if isinstance(per_page, int) and len(batch) < per_page:
                return items
            page, before = page + 1, ids

    def game(self, game_id: int) -> dict:
        return self._json(self._get(f"games/{game_id}"), f"game {game_id}").get("game") or {}

    def uploads(self, item: Item) -> list[Upload] | None:
        """The item's files; None when itch.io shows them only once it's claimed."""
        params = {"download_key_id": item.key_id} if item.key_id else None
        listed = self._json(self._get(f"games/{item.game_id}/uploads", params), f"the files of {item.title!r}").get("uploads")
        return [parse_upload(raw) for raw in listed] if isinstance(listed, list) else None

    def link(self, item: Item, upload: Upload) -> str:
        """A fresh link to one of an owned item's files, right before its download; '' when itch.io no longer has it.
        Only the redirect's address is read, never its body: the file itself would come with it. Asked again after
        network trouble, 429 and 5xx, with itch.io's Retry-After waited out (up to logins.LONGEST_WAIT) as for every
        other call; still turned away after that, a 429 counts as refused (download.Refused). No answer at all is the
        session's ShopError, which the downloads count (download.UNANSWERED_IN_A_ROW)."""
        from .download import DownloadError, Refused

        with self._links:
            r = self.session.request("GET", f"uploads/{upload.id}/download", params={"download_key_id": item.key_id},
                                     read=_an_address)
        status, where = r.status_code, r.headers.get("location", "")
        if r.is_redirect and where.startswith("https://"):
            return where
        if _an_address(r):  # a JSON answer: an address, or itch.io's errors
            try:
                answer = json.loads(r.content)
            except ValueError:
                answer = {}
            if isinstance(answer, dict) and str(answer.get("url") or "").startswith("https://"):
                return str(answer["url"])
            if isinstance(answer, dict) and answer.get("errors"):
                raise DownloadError(f"itch.io wouldn't give the file: {'; '.join(map(str, answer['errors']))[:200]}")
        if status in (404, 410):
            return ""
        raise (Refused if status == 429 else DownloadError)(f"itch.io answered HTTP {status} for the link to {upload.filename!r}")


def _an_address(r: httpx.Response) -> bool:
    """Whether a link's answer is read: only a JSON one, which may hold the file's address. Any other body (the file
    itself, with a 200) never comes."""
    return r.status_code == 200 and "json" in r.headers.get("content-type", "")


def check_key(key: str, transport: httpx.BaseTransport | None = None) -> str:
    """For 'hylly login': the account's name; raises AuthError when the key is refused."""
    with Itch(key, transport=transport, delay=0, tries=1) as itch:  # asked once: no answer is said at once
        user = itch.profile()
    if not user:
        raise AuthError(REFUSED)
    return str(user.get("display_name") or user.get("username") or "your account")


# ---- the answers kept -----------------------------------------------------------------------
# What itch.io said of each item's files, kept by the survey and the downloads alike, and the bundles' pages as the
# survey read them (itchio_survey.py), which a download reads an item's bundles from. In itch.io's folder in the
# library's state folder (shops.Shop.state), as paths in the state folder; the survey's own files are its module's.

UPLOADS = f"{SHOPS['itchio'].state}/uploads.jsonl"  # the answers kept about each item's files
PAGES = f"{SHOPS['itchio'].state}/bundle-pages.jsonl"  # the bundles' pages, as the survey read them


class Lines(Records):
    """A cache of answers, one JSON line each, appended at once: a stopped survey goes on. A later line wins
    (storage.Records), and each answer has the time it was had. key: a field, or several with commas ("bundle,page").
    A write that fails ends the run. keep: False for a dry run, which writes nothing down."""

    def __init__(self, path: Path, key: str, keep: bool = True) -> None:
        super().__init__(path, tuple(key.split(",")) if "," in key else key, keep=keep)

    def put(self, row: dict) -> None:
        super().put({**row, "at": db.now()})


@contextlib.contextmanager
def stops_saying(kept: str = "What was asked so far is kept"):
    """Asking itch.io for a while (a download's listing, a survey, claiming): Ctrl-C (INTERRUPTED) and the trouble that
    stops it (RuntimeError: PARTLY) end it saying so, what is kept, and that running it again goes on from there."""
    again = f"{kept}; running it again goes on from there."
    try:
        yield
    except KeyboardInterrupt:
        raise Exit(INTERRUPTED, f"Stopped. {again}") from None
    except RuntimeError as e:
        raise Exit(PARTLY, f"{e} {again}") from None


def _older(row: dict, seconds: float, now: datetime) -> bool:
    """Whether a kept answer was had longer ago than that (now: with its offset, as db.now has it); one with no time of
    its own, or a wrong one, counts as old. One kept before answers had their offset is read as this computer's time."""
    try:
        return (now - db.moment(str(row.get("at")))).total_seconds() > seconds
    except ValueError:
        return True


def ask_each(items: list[Item], ask, failed, *, log, progress_every: float) -> None:
    """Asks itch.io about each item in turn, at the session's pace (a second each): ask(item) asks and keeps the answer;
    failed(item, e) keeps what is known when itch.io answers it with a ShopError. When that is itch.io busy or not
    answering (ShopError.passing) for FAILS_IN_A_ROW items in a row, it's itch.io's trouble, not the items', so asking
    stops, saying so; what was kept stays, for the next run to go on from. Every progress_every seconds it says how far
    it is."""
    last, failing = time.monotonic(), 0
    for n, it in enumerate(items, 1):
        try:
            ask(it)
            failing = 0
        except ShopError as e:
            failing = failing + 1 if e.passing else 0
            if failing >= FAILS_IN_A_ROW:
                raise ShopError(f"itch.io didn't answer about {FAILS_IN_A_ROW} items in a row ({e}), so asking stopped.", e.status,
                                no_answer=e.no_answer) from None
            failed(it, e)
        if time.monotonic() - last >= progress_every:
            last = time.monotonic()
            log(f"  {n}/{len(items)} asked; {time_words(len(items) - n, about=True)} to go.")


def ask_files(itch: Itch, items: list[Item], cache: Lines, *, again: bool, log, progress_every: float,
              stale: set[int] | frozenset[int] = frozenset(), asked=None) -> None:
    """Each item's files, from the cache or itch.io; for a paid item not claimed, itch.io shows none. One claimed
    since it was asked about is asked again, and so is one marked to be (a file itch.io no longer had, say: Library.gone),
    and one whose game id is in stale. asked: which of them itch.io may be asked about at all (None: every one); the
    others have what the cache holds. An item itch.io won't list the files of stays unknown, as a paid one's do; one it
    was busy for is asked again next time."""
    todo = [it for it in items if (asked is None or asked(it))
            and (again or it.game_id not in cache.rows or cache.rows[it.game_id].get("again")
                 or (it.claimed and not cache.rows[it.game_id].get("visible", True)) or it.game_id in stale)]
    if todo:
        log(f"  Asking for the files of {many(len(todo), 'item')}, one a second: {time_words(len(todo), about=True)}.")

    def ask(it: Item) -> None:
        uploads = itch.uploads(it)
        cache.put({"game": it.game_id, "visible": uploads is not None, "uploads": [asdict(u) for u in uploads or []]})

    def failed(it: Item, e: ShopError) -> None:  # busy, not a no: asked again next time
        cache.put({"game": it.game_id, "visible": False, "uploads": [], **({"again": True} if e.passing else {})})

    ask_each(todo, ask, failed, log=log, progress_every=progress_every)
    for it in items:
        row = cache.rows.get(it.game_id)
        if row and row.get("visible", True):
            it.uploads = [Upload(**u) for u in row["uploads"]]


# ---- downloading ----------------------------------------------------------------------------
# What the library is for: the books, comics and tabletop games in your itch.io library, and from the other kinds
# the items whose files are all books or comics ("reading"). Of their files only the kinds on a list are taken
# ([itchio] fetch in formats.toml): anyone can put anything into an item, and a script or a shortcut in the library's
# folders could be run by whoever opens them. Programs, music, video and art assets are left, and each run lists
# every file it left.

DOWNLOADS = (*WANTED, "reading")  # what hylly itchio download takes unless told otherwise
# The kind of an item by its classification, as the survey groups them, is the rules' ([kinds.X] itchio in kinds.toml:
# Rules.itchio_kind), which the catalog reads too: its words go over it (title_of, shopping.kind_found)
KEPT_DAYS = 14  # a download asks again about an item's files once its kept answer is this many days old: creators revise
SPREAD_DAYS = 7  # and up to this many days more, by the item's id, so that answers had in one survey don't all age at once


def kept_for(item: Item) -> float:
    """How long, in seconds, an item's kept answer about its files serves a download: KEPT_DAYS, and a few days more
    by its id (SPREAD_DAYS)."""
    return (KEPT_DAYS + item.game_id % SPREAD_DAYS) * 86400.0


def library(itch: Itch, state: Path, *, again: bool = False, log=print, progress_every: float = 60.0,
            keep: bool = True, kinds: tuple[str, ...] = DOWNLOADS, title: str | None = None, creator: str | None = None,
            limit: int | None = None, of_kind=None, keys: set[str] | None = None) -> list[Item]:
    """Your itch.io library, each item once, with the bundles it came from and, for the kinds worth looking into,
    its files: from the answers kept (the survey's and earlier downloads') while they are fresh (kept_for) and none of
    the item's files has gone (Library.gone), else from itch.io (one a second), and from itch.io for every item with
    again. So a file a creator has added or replaced is found. Only the items the run may take are asked about (kinds,
    title and creator, as the core's select has them), and of the answers that have aged only those of the first limit
    of them, of the kinds --kind names where it does (of_kind: given the item and its kept answer, whether it is of
    them, as the core finds an item's kind); a dry run (keep False) asks as a run does about what isn't known, lets no
    answer age, and keeps none. keys: a shop list's items alone, by their ids as text (hylly itchio download LIST), of
    whatever kind: only those are asked about."""
    owned: dict[int, Item] = {}
    for it in itch.owned():
        owned.setdefault(it.game_id, it)  # owned twice (bought, and in a bundle): once is enough
    bundles: dict = {}
    platforms: dict = {}
    for (bundle, _), row in sorted(Lines(state / PAGES, "bundle,page").rows.items()):
        for raw in row["items"]:
            for key in (raw.get("game_id"), str(raw.get("url") or "").rstrip("/").lower()):
                if key:
                    if bundle not in bundles.setdefault(key, []):
                        bundles[key].append(bundle)
                    if raw.get("platforms"):
                        platforms[key] = raw["platforms"]
    items = list(owned.values())
    for it in items:
        url = it.url.rstrip("/").lower()
        it.bundles = bundles.get(it.game_id) or bundles.get(url) or []
        it.platforms = platforms.get(it.game_id) or platforms.get(url) or []
    files = Lines(state / UPLOADS, "game", keep=keep)  # every download's answers are kept, a dry run's not
    ask = [it for it in items if it.group in WANTED or (it.group in ("other", "game") and not it.platforms)]

    def taken(it: Item) -> bool:  # as the core chooses, but for what only the files can tell (reading)
        return ((it.group in kinds or ("reading" in kinds and it.group in ("other", "game")))
                and (not title or title.lower() in it.title.lower()) and (not creator or creator.lower() in it.creator.lower())
                and (keys is None or str(it.game_id) in keys))

    now, stale = datetime.now().astimezone(), set()
    if keep:
        order = sorted((it for it in ask if taken(it) and it.game_id in files.rows
                        and (of_kind is None or of_kind(it, files.rows[it.game_id]))),
                       key=lambda it: (it.creator.lower(), it.title.lower(), it.game_id))
        stale = {it.game_id for it in order[:limit] if _older(files.rows[it.game_id], kept_for(it), now)}
    ask_files(itch, ask, files, again=again, log=log, progress_every=progress_every, stale=stale, asked=taken)
    return items


REPLACED = ("itch.io no longer has it (its creator may have replaced it): the next hylly itchio download asks about the "
            "item's files again and fetches what it has now")


def chosen(item: Item, kinds: tuple[str, ...]) -> bool:
    """Whether a download of these kinds (--types) takes the item: by its group, or for reading, an item of the other
    kinds whose files are all books or comics."""
    if item.group in kinds:
        return True
    return "reading" in kinds and item.group in ("other", "game") and item.only_reading()


def replaced(e: dict, u: Upload) -> bool:
    """Whether itch.io now lists another file under an upload downloaded before: its MD5, or without one its size,
    isn't what it listed when that was downloaded (the record's listed_md5 and listed_size), nor the file's own. A
    record from before those were kept goes by its file's MD5 when itch.io's vouched for it then (md5_source shop), and
    else counts as the same: a listing that never matched its file isn't fetched again and again."""
    if u.md5:
        listed = e.get("listed_md5") or (e.get("md5") if e.get("md5_source") == "shop" else "")
        return bool(listed) and u.md5 not in (listed, e.get("md5"))
    before = e.get("listed_size")
    return bool(u.size) and isinstance(before, int) and before > 0 and u.size != before


def left_out(left: list) -> list[str]:
    """What a run says of the files it leaves out (download.Left), every run, a dry run too, in itch.io's words: where
    the list of kinds a download takes is shown, and how to take a kind too. None of it is a failure, and a copy an
    earlier run downloaded stays where it is."""
    if not left:
        return []
    n = len(left)
    lines = [f"Left out, not failures: {many(n, 'file')} (hylly rules show itchio lists the kinds a "
             "download takes):"] + [x.line() for x in left]
    if any(x.why == NOT_LISTED for x in left):
        lines.append('To take a kind too, write fetch_add = ["mp3"] under [itchio] in a rules file of your own '
                     "(hylly rules copy formats makes one, with that line ready).")
    return lines


# ---- for the shops' core ------------------------------------------------------------------

@dataclass
class File(shopping.File):
    """One of an item's files on itch.io itself, for the shops' core: a shopping.File by its upload (key: the upload's
    id), with itch.io's own rules for it, which the core asks of a file that has them (left, current, device). Its
    record keeps the size listed, an MD5 listed or not, as itch.io's always have (keep_size): should itch.io stop
    listing a file's MD5, a new size still tells a replaced file (replaced)."""
    upload: Upload | None = None
    keep_size = True

    @classmethod
    def of(cls, u: Upload) -> File:
        return cls(u.id, u.filename, size=u.size, md5=u.md5 or None, upload=u)

    def left(self, kind: str) -> str:
        return self.upload.left(kind)

    def current(self, entry: dict) -> bool:
        """Whether itch.io still lists the file downloaded as entry: not replaced, older records going by its rule."""
        return not replaced(entry, self.upload)

    def device(self, kind: str, ext: str) -> str:
        """Where it is best read, saved with this extension: a ZIP package by its formats, as its size says nothing of
        its pages (plan.device_for)."""
        from .plan import device_for

        return device_for(kind, ext, None if ext == "zip" else self.size)[0]


def title_of(it: Item) -> shopping.Title:
    """An item as the shops' core takes it: its key the game's id, a number, as its records have it; who made it its
    creator, the folder its own goes in (itchio/<creator>/<title>/); its files those on itch.io itself (a link elsewhere
    has nothing to fetch); its own word for its kind, its classification's (Rules.itchio_kind), which the words of its
    bundles and its creator go over, as at every shop (shopping.kind_found); and in its details what the catalog reads
    of it (catalog.itchio_items)."""
    from . import rules

    return shopping.Title(it.game_id, it.title, it.creator, [File.of(u) for u in it.uploads or [] if not u.elsewhere],
                          kind=rules.current().itchio_kind(it.group),
                          details={"group": it.group, "url": it.url, "cover": it.cover, "description": it.short_text,
                                   "published": it.published, "bundles": it.bundles})


class Library:
    """itch.io on the shops' core (shopping.py): its three things, by the API session, and what is its own, which the
    core asks of a shop that has it: the items a run takes (chosen: --types), its words for the files it leaves out
    (left_out), and what it does about a file it no longer has (gone). Made with the run's options, as which items are
    asked about depends on them (library), and a dry run keeps no answers. What is downloaded, where it goes, its
    records and its snapshot are the core's, as for every shop on it."""

    def __init__(self, key: str, *, state: Path, again: bool = False, dry_run: bool = False,
                 types: tuple[str, ...] = DOWNLOADS, title: str | None = None, creator: str | None = None,
                 limit: int | None = None, log=print, kinds: frozenset[str] | None = None,
                 yours: dict[str, str] | None = None, keys: set[str] | None = None):
        """kinds: --kind's, by which the answers aged are asked again first with --limit, each item of the kind the core
        finds it to be, by its files as its kept answer has them and your kind (yours, by catalog id:
        curation.kinds). keys: a shop list's items alone, by their ids as text: only those are asked about (library)."""
        self.itch = Itch(key)
        self.state, self.again, self.keep, self.types, self.log = state, again, not dry_run, types, log
        self.title, self.creator, self.limit, self.kinds, self.yours = title, creator, limit, kinds, yours or {}
        self.keys = keys
        self.unreadable: list[tuple[str, str]] = []  # itch.io's listing is read whole, or not at all
        self.items: dict[int, Item] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc) -> None:
        self.itch.__exit__(*exc)

    def check(self) -> None:
        """Nothing to ask first: the key goes with every call, and the library's first page refuses it as the profile
        would (AuthError)."""

    def titles(self, progress=None) -> list[shopping.Title]:
        """The items whose files are known (library), each as the core takes it (title_of): what the snapshot keeps, and
        what a run chooses from."""
        with stops_saying():
            items = library(self.itch, self.state, again=self.again, keep=self.keep, log=self.log, kinds=self.types,
                            title=self.title, creator=self.creator, limit=self.limit,
                            of_kind=self._of_kind if self.kinds else None, keys=self.keys)
        self.items = {it.game_id: it for it in items}
        return [title_of(it) for it in items if it.uploads is not None]

    def _of_kind(self, it: Item, row: dict) -> bool:
        """Whether an item, by its files as its kept answer has them, is of --kind's kinds, as the core will find it."""
        try:
            uploads = [Upload(**u) for u in row.get("uploads") or []]
        except TypeError:  # an answer of another shape: asked again in its turn, as any
            return True
        t = title_of(replace(it, uploads=uploads))
        shopping.corrected("itchio", [t], self.yours)
        return shopping.kind_found("itchio", t) in self.kinds

    @property
    def listed(self) -> set[str]:
        """Every item of your library, by its key as text: titles holds only those whose files are known."""
        return {str(key) for key in self.items}

    def link(self, title: shopping.Title, file: File) -> str:
        return self.itch.link(self.items[title.key], file.upload)

    def chosen(self, title: shopping.Title) -> bool:
        """Whether this run takes the item, besides the core's --title and --creator: one in your library of the kinds
        --types names (chosen)."""
        it = self.items[title.key]
        return it.claimed and chosen(it, self.types)

    def left_out(self, left: list) -> list[str]:
        return left_out(left)

    def gone(self, title: shopping.Title, file: File, job, manifest) -> str:
        """For the downloads (download.Downloader's gone): a file itch.io no longer has, its link or the file at its end
        answering 404 or 410, has most likely been replaced by its creator. So its item is marked in the kept answers to
        be asked about again by the next run, at once, whatever the answer's age, and the failure says so (REPLACED). A
        record of that upload's file, from an earlier run, is marked gone in the manifest: the file it names isn't here,
        and won't come, so hylly verify and the catalog no longer count it."""
        it, u = self.items[title.key], file.upload
        now = db.now()
        row = {"game": it.game_id, "visible": True, "uploads": [asdict(x) for x in it.uploads or []], "again": True, "at": now}
        with self._lock:  # the downloads run side by side
            append_line(self.state / UPLOADS, json.dumps(row, ensure_ascii=False))
            before = manifest.get(job.path)
            if before and before.get("file") == u.id and not before.get("gone"):
                manifest.add({**before, "gone": now})
        return REPLACED
