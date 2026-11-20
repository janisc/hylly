"""Online lookups: the details files lack (authors, descriptions, series, genres), from metadata sources.

Each source is a plugin in this package (metron.py, comicvine.py, openlibrary.py, googlebooks.py), named by its row in
sites.LOOKUPS, which holds its name, its key and its budgets. It knows only its own site: which kinds of titles it is
for, how to search it, and how sure a match is. Everything else is here and shared by all of them:

- **Session:** throttled, with at least `interval` seconds between requests plus per-hour and per-day budgets
  kept on disk, a line per request before it is sent (state\\lookups\\lookup-budget-<source>.txt), so neither a restart
  nor a killed run can overspend them. Network trouble
  and 5xx are retried with backoff. Retry-After and Metron-style X-RateLimit-* headers are honoured. A redirect is
  followed only over https and only to the internet, never to this computer or the home network (fetching.py):
  one elsewhere is Changed.
- **Cache:** state\\lookups\\lookup-<source>.jsonl, one line per answer, appended at once. A stopped run loses
  nothing and the next one continues where it stopped. Answers are "found", "none" (asked again after
  none_days in [lookups] of the settings) or "error" (asked again next run).
- **Typed problems:**
  - Refused: the login doesn't work. Stops this source.
  - Quota: the budget is spent. Stops this source until a later run.
  - Changed: the site's answers changed shape (three in a row). Stops this source; it needs an update. Anything
    else a plugin lets out of find() (a KeyError, a TypeError) counts as Changed, so one odd answer never ends a run.
  - Transient: no answer, or server errors, after the retries (three titles in a row). Stops this source for
    this run: the site seems down. One title that fails alone is only recorded and asked again next run.
  - A local problem, such as a full disk, stops the whole run.
  Whatever stops a source or the run is logged to state\\lookup-log.txt; all but a spent quota (expected
  pacing) also pop up a notification.
- **run():** asks only about titles without an answer (incremental); again=True asks all of them again. Each source's
  turn is planned first (Plan: who is asked about what, with no request, each cache read once), then asked (ask);
  hylly lookup --dry-run is the plan alone (dry_run). The catalog build only reads the caches (Answers), so it stays
  fast and works offline. Both read a cached answer one way (usable), so what the catalog leaves out of one source's
  answer is asked of the next.
- **Your word** on a source's answer for a title (yours: curation's answers, a match question's or hylly set refused):
  a near miss you said to use is used, while the source's answer is that match (match_mark); a source you refused gives
  the title nothing and isn't asked about it again. A near miss at or above NEAR is put to you (Answers.near).

How to add a source: docs\\lookups.md. Secrets (API keys and tokens) are never logged or cached, and never put
into an error message: requests are logged by path only.
"""

from __future__ import annotations

import difflib
import contextlib
import html
import json
import math
import re
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx

from ..exits import NOT_SET_UP, Exit
from ..logins import retry_after  # the one reading of Retry-After, the shops' too
from ..sites import LOOKUPS
from ..words import time_words
from .. import USER_AGENT, db, fetching, paths, rules, storage
# [lookups] in the settings: order (the sources asked, first one first; one left out isn't asked; none listed, as it
# comes: every one, in the order of sites.LOOKUPS), accept (a match below this score is not used: 0.8) and none_days (a
# "none" is asked again after this long, as the databases grow: 180).
RETRIES = 6
SOON = 900.0  # seconds to a free place in the day's budget that are worth waiting for, instead of ending the source's turn
SLOWED_IN_A_ROW = 3  # answers in a row asking to slow down, with its waits in between: then it means it
MAX_WAIT = 180.0  # a Retry-After longer than this ends the source's turn (Quota) instead of waiting
LONG_WAIT = 60.0  # a wait this long is told, so that a quiet run doesn't look hung
CHANGED_IN_A_ROW = 3
LIBRARY_EVERY = 10  # titles between looks whether the library is still there (paths.library_missing): an answer kept
# for a library whose disk went is kept on the disk an unmounted one leaves behind, and lost to it


class LookupProblem(Exception):
    """A problem with one source; the message is for people and never contains a secret."""


class Refused(LookupProblem):
    """The source refused the login (key or token)."""


class Quota(LookupProblem):
    """The source's budget is spent for now."""


class Changed(LookupProblem):
    """The source answered something this plugin can't read: it may need an update."""


class Transient(LookupProblem):
    """Network trouble that outlasted the retries."""


# ---- what is asked, and what a source answers ----------------------------------------------

@dataclass
class Query:
    id: str  # the catalog id; copies merged into one title share an answer
    kind: str
    title: str
    series: str = ""
    number: str = ""
    authors: list[str] = field(default_factory=list)
    isbn: str = ""
    publisher: str = ""
    has: frozenset[str] = frozenset()  # fields the title already has: "authors", "description", "subjects", "series"
    bought: str = ""  # the year it was bought, when the shop says: an answer can't be about a later edition
    year: str = ""  # the year its file or shop gives: with the publisher, what lets a book asked by its title alone
    # take a match's authors (borrowed)

    @staticmethod
    def from_record(rec: dict) -> Query:
        """The question about a title, with its own details. A catalog's record also holds what lookups found, and says
        which fields those are (lf): asked with them, a title's question would change with every answer, and the new
        answer to the new question would replace a good one."""
        own = {k: v for k, v in rec.items() if k not in (rec.get("lf") or ())}
        has = {k for k, key in (("authors", "a"), ("description", "d"), ("subjects", "sub"), ("series", "s")) if own.get(key)}
        authors = list(own.get("a", []))
        if not authors and own.get("src") == "itchio" and own.get("p"):  # its publisher is the item's creator. Asked by
            authors = [own["p"]]  # title alone, a game called The Moon Garden takes the author of any book called that
        return Query(id=rec["id"], kind=own.get("k", "book"), title=own.get("t", ""), series=own.get("s", ""),
                     number=str(own.get("n", "")), authors=authors, isbn=own.get("isbn", ""), publisher=own.get("p", ""),
                     has=frozenset(has), bought=str(own.get("dt") or "")[:4], year=str(own.get("y") or "")[:4])


FILLED_IN = ("a", "s", "n", "d", "sub", "p", "y", "isbn")  # the fields of a catalog's record a lookup can fill in


def norm(text: str) -> str:
    """For comparing names, and in a question's key: its words as rules.words_of reads them (small letters without
    accents, letters and digits of any script, so a title in Cyrillic, Greek or Japanese has a key of its own), one space
    apart, & as and, without a leading "the". In NFKC first, as before: a ligature or a full-width letter is its plain
    one. A name in plain ASCII gives what it always did, so its question keeps its answer. Apart from the words a search
    compares (rules.words_of) on purpose: a source may write a name with "and" for "&", without its "The", or with
    plain letters for a ligature; and the cache keeps every answer by this key, so a change here asks each title
    again."""
    words = rules.words_of(unicodedata.normalize("NFKC", text or "").replace("&", " and "))
    return re.sub(r"^the ", "", " ".join(words))


def similar(a: str, b: str) -> float:
    """0 to 1: how alike two names are, as words in any order and as text, in any script."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    wx, wy = set(x.split()), set(y.split())
    words = len(wx & wy) / len(wx | wy)
    return max(words, difflib.SequenceMatcher(None, x, y).ratio())


def bare_title(title: str) -> str:
    """A title as a source asks for it by name: on one line, without a label in brackets before it ("(ebook -
    psychology) Life After Death") and without what follows a colon or a bracket (a subtitle, an edition). A run of
    spaces is one first, so the patterns don't backtrack over it."""
    title = re.sub(r"\s+", " ", str(title or ""))
    title = re.sub(r"^\s*[\[(][^\])]*[\])]\s*", "", title)
    return re.sub(r"\s*[:(].*$", "", title).strip(" -")


def accept() -> float:
    from .. import settings

    return settings.get("lookups")["accept"]


@dataclass
class ComicName:
    series: str
    number: str  # "" when the title names no number
    collected: bool | None  # True: a collected edition (Vol., TPB, HC); False: a single issue; None: can't tell
    subtitle: str = ""
    year: str = ""  # a year in brackets: usually when the series began


def comic_name(title: str, series: str = "", number: str = "") -> ComicName:
    """What a comic's title says: "Moonwake Vol. 3", "Ironclad Hearts #31", "Neon Rangers 088",
    "starlight-drifters-vol-4", "Glass Harbor Vol. 4: The Long Tide", "Neon Rangers 010 (2010)", "Rust Cowboy (Vol. 1) #1".
    A series and number the file itself gave (ComicInfo.xml) win when the title doesn't name that series:
    "A Quiet Week, Part 6" is issue 6 of "Neon Rangers 2". What makes a collected edition, and the notes and words a
    name drops, are [series] in series.toml."""
    r = rules.current()
    if series and number and not norm(title).startswith(norm(series)):
        return ComicName(series=series.strip(), number=re.sub(r"^0+(?=\d)", "", number),
                         collected=bool(r.collected.search(title)) or None)
    t = title.strip()
    if " " not in t and "-" in t:  # a file name: "starlight-drifters-vol-4"
        t = t.replace("-", " ")
    t = r.comic_notes.sub(" ", t)  # "(Digital Edition)", "(of 6)"
    t = re.sub(r"\(\s*vol(?:ume)?\.?\s*\d+\s*\)", " ", t, flags=re.I)  # the run of a series, not a collected volume
    year = ""
    m = re.search(r"\((\d{4})(?:\s*-\s*(?:\d{4})?)?\)", t)
    if m:
        year, t = m[1], (t[:m.start()] + t[m.end():])
    collected: bool | None = bool(r.collected.search(t)) or None
    vol = re.search(r"\b(?:vol(?:ume)?|book|tpb|hc)\b\.?\s*(\d+)\b", t, re.I) or re.search(r"\bvol(\d+)\b", t, re.I)
    issue = re.search(r"(?:#|\bissue\s+|\bno\.\s*)(\d+(?:\.\d+)?)\b", t, re.I) or re.search(r"\s0*(\d{1,4})\s*$", t)
    subtitle, number = "", ""
    if vol:
        number, series, subtitle = vol[1], t[:vol.start()], t[vol.end():]
        collected = True
    elif issue:
        number, series = issue[1], t[:issue.start()]
        collected = collected or False
    else:
        series = t
        series, _, subtitle = series.partition(":") if collected else (series, "", "")
    series = r.comic_noise.sub(" ", series)
    series = re.sub(r"\s+", " ", series).strip(" -:,(")  # a strip: a pattern ending in $ backtracks over a long run
    number = re.sub(r"^0+(?=\d)", "", number)  # "007" is 7, "0.5" stays 0.5: zeros off the whole part only
    return ComicName(series=series or t.strip(), number=number, collected=collected,
                     subtitle=subtitle.strip(" :-,"), year=year)


# ---- the source plugin interface ------------------------------------------------------------

FROM_ITS_ROW = ("name", "per_hour", "per_day", "credential")  # what a Source takes from its row in sites.LOOKUPS


class Source:
    """One metadata source. Subclasses set the class attributes and write find(); see docs\\lookups.md. A source's name,
    key and budgets are its row in sites.LOOKUPS, by its id, where the parts that mustn't load this code read them too:
    a class takes them from there (FROM_ITS_ROW), and only one that isn't a row's, as a test's made-up source, sets them
    itself."""

    id = ""  # "metron": the cache's and the settings' name, and its row's in sites.LOOKUPS
    name = ""  # "Metron": what people see (its row's)
    kinds: frozenset[str] = frozenset()  # the kinds of titles it is asked about
    wants: frozenset[str] = frozenset({"authors", "description"})  # a title missing any of these is asked about
    interval = 1.0  # seconds between requests at least
    per_hour = 0  # budget of requests per rolling hour, 0: none of its own (its row's)
    per_day = 0  # and per rolling day (its row's)
    credential: str | None = None  # the sites.SITES site whose saved key it needs, its id; None: no login (its row's key)
    home = ""  # the site, for people
    terms = ""  # one line: what its terms ask (attribution, non-commercial)
    verified = ""  # the last date it was checked against the real site ("" = never yet)

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        row = LOOKUPS.get(cls.id)
        if row is None:
            return
        facts = {"name": row.name, "per_hour": row.per_hour, "per_day": row.per_day, "credential": cls.id if row.key else None}
        for attribute in FROM_ITS_ROW:
            if attribute not in vars(cls):  # a class's own word stands; test_lookups.py holds the real ones to their rows
                setattr(cls, attribute, facts[attribute])

    def __init__(self, key: str = "") -> None:
        self.key_value = key  # the API key or token, when the source needs one; never logged
        self.headers: dict[str, str] = {}  # sent with every request (a login header, say)

    def about(self) -> str:
        """Its site, its terms and how fresh this plugin's knowledge of the site is, for hylly lookup --dry-run: the
        owner is the one who keeps to the terms (Comic Vine's are non-commercial use only)."""
        checked = f"Checked against the site on {self.verified}." if self.verified else "Not yet checked against the site."
        said = " ".join(part for part in (self.terms, checked) if part)
        return f"{self.home}: {said}" if self.home else said

    def needs(self, q: Query) -> bool:
        return q.kind in self.kinds and not self.wants <= q.has

    def key(self, q: Query) -> str:
        """The cache key: titles asked the same way share an answer."""
        if q.kind == "comic":
            name = comic_name(q.title, q.series, q.number)
            return f"comic:{norm(name.series)}|{name.number}|{'c' if name.collected else 'i' if name.collected is False else '?'}|{norm(q.publisher)}"
        return f"isbn:{q.isbn}" if q.isbn else f"t:{norm(q.title)}|{norm(q.authors[0]) if q.authors else ''}"

    def check(self, s: Session) -> str:
        """A cheap request that proves the login works; returns a few words for people. Raises Refused."""
        return "no login needed"

    def find(self, s: Session, q: Query) -> dict | None:
        """The details for one title, or None when nothing matches well enough. A found dict may have:
        authors, description, subjects, series, number, year, publisher, isbn, url (the source's page for it,
        shown with attribution), score (0 to 1) and match (what was matched, for people)."""
        raise NotImplementedError


def sources() -> dict[str, type[Source]]:
    """Every source there is, by its id, in the order of sites.LOOKUPS: each row's code is the Source of its id in
    lookups/<id>.py, so a new source is its row and its module, with no list here to add it to."""
    import importlib

    found = {}
    for sid in LOOKUPS:
        module = importlib.import_module(f"{__name__}.{sid}")
        found[sid] = next((v for v in vars(module).values() if isinstance(v, type) and issubclass(v, Source)
                           and v.__module__ == module.__name__ and v.id == sid), None)
        if found[sid] is None:
            raise ImportError(f"sites.LOOKUPS names {sid}, and lookups/{sid}.py has no Source whose id is {sid}.")
    return found


def in_order() -> dict[str, type[Source]]:
    """The sources asked, in the order they are asked: [lookups] order in the settings, and with none listed there (as
    it comes), every source in the order of sites.LOOKUPS. The first found answer for a field wins in the catalog; a
    source left out of a list isn't asked."""
    from .. import paths, settings

    known = sources()
    order = settings.get("lookups")["order"] or list(known)
    unknown = [sid for sid in order if sid not in known]
    if unknown:
        raise Exit(NOT_SET_UP, f"{paths.settings_file()}: [lookups] order: {', '.join(unknown)}? The sources are "
                         f"{', '.join(known)}.")
    return {sid: known[sid] for sid in order}


# ---- the session: throttled, retried, within budget ------------------------------------------

class Budget:
    """Requests of the last day, a line each, kept on disk as they are made. So neither a restart nor a run that's
    killed can overspend the hour or the day, and a run after another, also on another computer, adds to the count
    instead of writing over it. (Two at the same time could still lose lines, so runs.only_one keeps them apart.)"""

    def __init__(self, path: Path, per_hour: int, per_day: int, now: Callable[[], float] = time.time) -> None:
        self.path, self.per_hour, self.per_day, self.now = path, per_hour, per_day, now
        self.times: list[float] = []
        try:
            lines = storage.read_lines(path)
        except OSError:
            lines = []  # the in-memory count still holds for this run
        for line in lines:
            with contextlib.suppress(ValueError):
                self.times.append(float(line))
        self._prune()
        if len(lines) > len(self.times) + 5000 and not db.in_dry_run():  # days old: not worth keeping, nor reading each time;
            # a dry run leaves them, as it changes nothing
            with contextlib.suppress(OSError):
                storage.atomic_write(path, "".join(f"{t:.1f}\n" for t in self.times), durable=False)

    def _prune(self) -> None:
        cut = self.now() - 86400
        self.times = [t for t in self.times if t > cut]

    def wait(self) -> float:
        """Seconds until the next request is within budget: 0 now; math.inf when the day is spent, and no place in it comes
        free soon. A rolling day: a run at the same hour the next day finds the oldest ones only just gone, so waiting for
        the next place goes on at yesterday's pace."""
        self._prune()
        now, wait = self.now(), 0.0
        if self.per_day and len(self.times) >= self.per_day:
            wait = self.next_slot() - now
            if wait > SOON:
                return math.inf
        if self.per_hour:
            hour = [t for t in self.times if t > now - 3600]
            if len(hour) >= self.per_hour:
                wait = max(wait, hour[len(hour) - self.per_hour] + 3600 - now + 1)
        return wait

    def next_slot(self) -> float:
        """When a place in the day's budget comes free."""
        return sorted(self.times)[len(self.times) - self.per_day] + 86400

    def done_by(self, requests: int, interval: float, last: float) -> float:
        """When `requests` more would have been sent, at most one every `interval` seconds and within the hour's
        budget: the time to go, which the pace alone would put far too short. (A spent day ends a run instead.)"""
        self._prune()
        now = self.now()
        recent = sorted(t for t in self.times if t > now - 3600)
        at = max(now, last + interval)
        for _ in range(requests):
            if self.per_hour and len(recent) >= self.per_hour:
                at = max(at, recent[-self.per_hour] + 3600 + 1)
            recent.append(at)
            at += interval
        return recent[-1] if requests else now

    def note(self) -> None:
        """Before each request: one that fails was asked all the same, and a killed run has counted it."""
        at = self.now()
        self.times.append(at)
        with contextlib.suppress(OSError):  # the in-memory count still holds for this run
            storage.append_line(self.path, f"{at:.1f}", attempts=1)


def user_agent() -> str:
    """How Hylly names itself, with the contact you set under [lookups]: some sources ask for one."""
    from .. import settings

    contact = str(settings.get("lookups")["contact"]).strip()
    if not re.fullmatch(r"[\x20-\x7e]{0,100}", contact):  # not quietly changed: it could become someone else's address
        raise Exit(NOT_SET_UP, "contact under [lookups] in the settings goes in each request's header, so it can only have plain "
                         "letters, digits and punctuation, at most 100 of them.")
    return f"{USER_AGENT[:-1]}; {contact})" if contact else USER_AGENT


class Session:
    """HTTP for one source: never faster than its interval and budgets, retried, and typed problems instead
    of raw errors. Errors and logs name the path only, never the query (where API keys travel). Every request carries
    the source's own headers (Metron's login)."""

    def __init__(self, source: Source, budget: Budget, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda line: None) -> None:
        self.source, self.budget, self.sleep, self.now, self.log = source, budget, sleep, now, log
        self.http = httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=True, transport=transport,
                                 headers={"User-Agent": user_agent(), "Accept": "application/json", **source.headers},
                                 event_hooks=fetching.hooks())  # https only, and never an address at home
        self._last = 0.0
        self._pause_until = 0.0  # from the site's own rate-limit headers
        self._quota_until = 0.0
        self._slowed = 0  # answers in a row asking to slow down
        self.requests = 0

    def close(self) -> None:
        self.http.close()

    def _ready(self) -> None:
        if self._quota_until > self.now():
            raise Quota(f"{self.source.name} says its limit for now is reached; it resets at "
                        f"{datetime.fromtimestamp(self._quota_until):%H:%M}.")
        budget = self.budget.wait()
        if budget == math.inf:
            raise Quota(f"{self.source.name}: today's budget of {self.source.per_day} requests is spent; "
                        f"more from {datetime.fromtimestamp(self.budget.next_slot()):%H:%M on %d %B}.")
        pause = self._pause_until - self.now()
        wait = max(budget, self._last + self.source.interval - self.now(), pause)
        if wait >= LONG_WAIT:
            until = f"{datetime.fromtimestamp(self.now() + wait):%H:%M}"
            if wait == budget:
                full_day = self.budget.per_day and len(self.budget.times) >= self.budget.per_day
                used = f"today's {self.budget.per_day}" if full_day else f"this hour's {self.budget.per_hour}"
                self.log(f"  {self.source.name}: {used} requests are used; "
                         f"waiting until {until}, then it goes on.")
            elif wait == pause:
                self.log(f"  {self.source.name} asked to slow down; waiting until {until}, then it goes on.")
        if wait > 0:
            self.sleep(wait)

    def done_by(self, requests: int) -> float:
        """When `requests` more would have been sent, at this source's pace and within its hourly budget."""
        return self.budget.done_by(requests, self.source.interval, self._last)

    def _note_limits(self, r: httpx.Response) -> None:
        """Metron-style headers: X-RateLimit-{Burst,Sustained}-{Remaining,Reset} (seconds to the reset)."""
        for kind in ("Burst", "Sustained"):
            left, reset = r.headers.get(f"X-RateLimit-{kind}-Remaining"), r.headers.get(f"X-RateLimit-{kind}-Reset")
            if left is not None and left.strip() == "0" and reset and reset.strip().isascii() and reset.strip().replace(".", "", 1).isdigit():
                seconds = float(reset)  # hundreds of digits make it infinite
                if kind == "Burst" and seconds <= MAX_WAIT:
                    self._pause_until = max(self._pause_until, self.now() + seconds + 1)
                else:  # longer than a run waits, as for a Retry-After: this source's turn ends at the next request
                    self._quota_until = max(self._quota_until, self.now() + min(seconds, 86400) + 1)

    def get(self, url: str, params: dict | None = None, own: frozenset[int] = frozenset()) -> httpx.Response:
        """own: statuses the source reads itself instead of the usual meaning (Google says a spent quota with 403)."""
        path = httpx.URL(url).path
        failures = 0
        while True:
            self._ready()
            self.budget.note()
            self.requests += 1
            try:
                r = self.http.get(url, params=params)
            except fetching.Refused as e:  # the site sent it on there: asking again brings the same
                raise Changed(f"{self.source.name}: {path} led to {e}, so it wasn't followed.") from None
            except httpx.TransportError as e:
                failures += 1
                if failures >= RETRIES:
                    raise Transient(f"{self.source.name}: no answer from {path} after {RETRIES} tries ({type(e).__name__}).") from None
                self.sleep(min(2 ** failures, 60))
                continue
            finally:
                self._last = self.now()
            self._note_limits(r)
            self._slowed = self._slowed + 1 if r.status_code in (429, 420) else 0
            if r.status_code in own:
                return r
            if r.status_code in (429, 420):
                wait = retry_after(r, self.now())
                if self._slowed >= SLOWED_IN_A_ROW:
                    raise Quota(f"{self.source.name} keeps asking to slow down (HTTP {r.status_code}, {self._slowed} times "
                                "in a row, after waiting as it asked); later runs continue.")
                if wait is not None and wait <= MAX_WAIT:
                    self._pause_until = max(self._pause_until, self.now() + wait)
                    continue
                raise Quota(f"{self.source.name} asks to slow down (HTTP {r.status_code}"
                            + (f", for {time_words(wait)}" if wait and wait <= 86400 else ", for more than a day" if wait
                               else "") + "); later runs continue.")
            if r.status_code in (401, 403):
                raise Refused(f"{self.source.name} refused the login (HTTP {r.status_code} on {path}).")
            if r.status_code >= 500:
                failures += 1
                if failures >= RETRIES:
                    raise Transient(f"{self.source.name}: HTTP {r.status_code} from {path} after {RETRIES} tries.")
                self.sleep(min(2 ** failures, 60))
                continue
            return r

    def json(self, url: str, params: dict | None = None, own: frozenset[int] = frozenset()):
        """The JSON answer; None for 404. Any other status but 200, or a 200 that isn't JSON, is Changed. own: statuses
        the source reads itself (as get has them: Google says a spent quota with 403), and then the status too, as
        (status, JSON), the JSON None for a 404 and for an own status whose answer isn't JSON."""
        r = self.get(url, params, own)
        if r.status_code in own:
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        if r.status_code == 404:
            return (404, None) if own else None
        if r.status_code != 200:
            raise Changed(f"{self.source.name}: HTTP {r.status_code} from {httpx.URL(url).path}.")
        try:
            data = r.json()
        except ValueError:
            raise Changed(f"{self.source.name}: {httpx.URL(url).path} answered something other than JSON.") from None
        return (200, data) if own else data


# ---- answers, cached ------------------------------------------------------------------------

def cache_dir(root: Path) -> Path:
    """Days of answers and what they spent of the sources' budgets: in state\\, since catalog\\ can be deleted and made again."""
    from .. import paths

    return paths.state_dir(root) / "lookups"


class Cache:
    """One source's answers: by key, and by catalog id (so a title edited since keeps its answer), each id by the last
    line that names it. A record file by key (storage.Records), a line cut short by a crash costing only itself; a write
    that fails, once its retries are spent, ends the run."""

    def __init__(self, path: Path) -> None:
        self.by_id: dict[str, dict] = {}
        self.records = storage.Records(path, "key", each=self._index)
        self.path, self.data = path, self.records.rows

    def _index(self, e: dict) -> None:
        for i in e.get("ids", []):
            self.by_id[i] = e

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, ids: list[str], status: str, found: dict | None = None, error: str = "") -> None:
        e = {"key": key, "ids": ids, "status": status, "at": db.now()}
        if found:
            e["found"] = found
        if error:
            e["error"] = error
        self.records.put(e)


def settled(e: dict | None, again: bool = False) -> bool:
    """Whether an answer stands: found does; none does for none_days (180); error is asked again. One refused (a person
    found it to be about another book) stands for good, so it doesn't come back with --again."""
    if e is not None and e.get("status") == "refused":
        return True
    if e is None or again or e.get("status") == "error":
        return False
    if e.get("status") == "none" and e.get("at"):
        from .. import settings

        try:
            # an answer kept before answers had their offset: its time read as this computer's (db.moment)
            kept = datetime.now().astimezone() - db.moment(e["at"])
            return kept < timedelta(days=settings.get("lookups")["none_days"])
        except ValueError:
            return False
    return True


NEAR = 0.6  # a near miss at or above this is put to you (a match question); one under it is too unlike to ask about.
# The sources' own line: Metron looks no closer at a series that names it under 0.6, and a title's words or letters
# that alike, less a wrong author's or edition's 0.3, are still about that title. So it comes from how the plugins
# score, not from a count of how the sources' real scores spread. Look again once a few dozen match questions are
# answered (hylly decide --answered lists them): if the ones refused crowd just above 0.6, raise it; if even those near
# 0.6 are mostly used, a lower line would find more. And again whenever a plugin's scoring changes.


def match_mark(found: dict) -> str:
    """A source's match in a few letters, by its page or else its words: what your "use it" names, so that it is used
    while the source's answer for the title is that match, and a later, other one is asked about anew."""
    import hashlib

    said = found.get("url") or f"{found.get('match', '')}|{found.get('year', '')}"
    return hashlib.sha1(str(said).encode("utf-8")).hexdigest()[:12]


def yours(root: Path) -> dict[str, dict[str, dict]]:
    """Your word on the sources' answers (curation.lookup_answers): by catalog id, each source's, use or refused; none
    where the library's records are another computer's, or can't be read just now."""
    from .. import curation

    try:
        return curation.lookup_answers(curation.load(root))
    except (OSError, db.NotHere, db.Newer, db.Unusable):
        return {}


class Answers:
    """Every source's cache, for the catalog build: what each title gets, the first source first, as you said where you
    did (yours): a source's match you said to use is used though it scored under accept, and a source you refused gives
    the title nothing."""

    def __init__(self, root: Path) -> None:
        self.sources = [cls() for cls in in_order().values()]
        self.caches = {s.id: Cache(cache_file(root, s)) for s in self.sources}
        self.accept = accept()
        self.yours = yours(root)

    def _answer(self, s: Source, q: Query) -> tuple[dict | None, dict]:
        """A source's cached answer for a title (by its id, else its key) and the match it holds: the found one, or a
        none's near miss."""
        c = self.caches[s.id]
        e = c.by_id.get(q.id) or c.get(s.key(q))
        return e, held(e)

    def near(self, q: Query) -> list[tuple[Source, dict]]:
        """The near misses for a title: each source's match that scored under accept, at NEAR or above, that you neither
        used nor refused (a used one whose source answers another match now is a near miss again), the best first. None
        that can't be right (too_late)."""
        out = []
        for s in self.sources:
            if q.kind not in s.kinds:
                continue
            e, found = self._answer(s, q)
            said = self.yours.get(q.id, {}).get(s.id) or {}
            if (not found or (e or {}).get("status") not in ("found", "none") or said.get("a") == "refused"
                    or (said.get("a") == "use" and said.get("m") == match_mark(found))):
                continue
            score = found.get("score", 1.0)
            if isinstance(score, (int, float)) and NEAR <= score < self.accept and not too_late(q, found):
                out.append((s, found))
        return sorted(out, key=lambda x: -x[1].get("score", 0))

    def for_query(self, q: Query) -> dict:
        """The fields found for a title: the first source's value wins; "lk" lists where they came from."""
        out: dict = {}
        for s in self.sources:
            found = usable(s, self._answer(s, q)[0], q, self.yours.get(q.id, {}).get(s.id) or {}, self.accept)
            if found is None:
                continue
            for k, v in found.items():
                if k not in ("url", "score", "match") and v and k not in out:
                    out[k] = v
            out.setdefault("lk", []).append([s.name, found.get("url", "")])
        return out


def held(e: dict | None) -> dict:
    """The match a cached answer holds: the found one, or a none's near miss; {} for none."""
    found = (e or {}).get("found") or {}
    if (e or {}).get("status") == "none":
        found = found.get("near") if isinstance(found.get("near"), dict) else {}
    return found if isinstance(found, dict) else {}


def usable(s: Source, e: dict | None, q: Query, said: dict, good: float) -> dict | None:
    """What a source's cached answer e gives a title: the one reading of it, for the catalog (Answers.for_query) and for
    a run, which asks a later source for what this leaves out (run). None when it gives nothing: a source not for this
    kind of title, or one you refused (said: your word on it, curation's), an answer that isn't found, that scored
    under accept (good) or that can't be right (too_late). Else its fields, less what it can't give: a description
    that is an advert or only a link (plain_description); and for a book asked by its title alone, whose match may be
    another book of that name (borrowed), all but its description and subjects, or nothing without them. A near miss
    you said to use gives what it holds, as a found answer would: you found it to be the book."""
    if q.kind not in s.kinds or said.get("a") == "refused":
        return None
    found = held(e)
    used = said.get("a") == "use" and bool(found) and said.get("m") == match_mark(found)
    if not used and ((e or {}).get("status") != "found" or found.get("score", 1.0) < good or too_late(q, found)):
        return None
    alone = not used and borrowed(q, found)
    found = {k: v for k, v in found.items() if (k in TITLE_ALONE if alone else k not in VOUCHING)}  # those fill nothing
    if "description" in found:
        found["description"] = plain_description(found["description"])
    if alone and not found.get("description") and not found.get("subjects"):
        return None  # nothing left of it (an advert for a description is nothing): not named where details came from
    return found


TITLE_ALONE = ("description", "subjects", "url", "score", "match")  # what a match that may be another book gives
VOUCHING = ("publishers", "years")  # all a source knows of its match's publishers and years: they vouch, fill nothing


def agrees(q: Query, publishers: list | tuple = (), years: list | tuple = ()) -> bool:
    """Whether one of a match's publishers or years is the title's own: a second sign, beside its name, that a match
    found by the title alone is this very book. A publisher agrees when one's name is the other's first words
    ("Moonwake Press" and "Moonwake Press, Inc.", but not "Moon Press"), but never a platform anyone publishes on
    (SELF_PUBLISHED); a year when it is the same year."""
    mine = norm(q.publisher).split()

    def same(publisher) -> bool:
        short, long = sorted((mine, norm(str(publisher or "")).split()), key=len)
        return bool(short) and long[:len(short)] == short and not self_published(long)

    return (any(same(p) for p in publishers)
            or len(q.year) == 4 and q.year.isdigit() and q.year in {str(y or "")[:4] for y in years})


def borrowed(q: Query, found: dict) -> bool:
    """Whether an answer may be about another book of the same name: a book (not a comic, which is asked by series and
    number) asked about by its title alone, with neither author nor ISBN, unless its match is vouched for. Asked so, a
    source gives the first book of that name it knows: under a short or common title, seldom the right one, and its
    authors, publisher, year and ISBN would be another book's; a wrong author is worse than none. Vouched for: the
    match's publisher or one of its years agrees with the title's (agrees; a source says them in its answer, and may
    add all it knows of them, publishers and years: VOUCHING, as every title asked the same way shares the answer), or
    the match has the very title asked, of three words or more (same_long_title)."""
    if q.kind == "comic" or q.authors or q.isbn or same_long_title(q, found.get("match")):
        return False
    publishers, years = found.get("publishers"), found.get("years")
    return not agrees(q, [found.get("publisher"), *(publishers if isinstance(publishers, list) else [])],
                      [found.get("year"), *(years if isinstance(years, list) else [])])


SELF_PUBLISHED = ("CreateSpace", "Create Space", "Independently published", "Self-published", "Self published",
                  "Lulu", "Smashwords", "Draft2Digital", "BookBaby", "IngramSpark", "Lightning Source", "Blurb",
                  "Xlibris", "AuthorHouse", "iUniverse", "Trafford", "Outskirts Press", "BookRix", "PublishDrive",
                  "Notion Press")
# the platforms where anyone publishes, by name ("CreateSpace Independent Publishing Platform", "Lulu.com"): thousands
# of books share each, so one never vouches for a match (agrees)


def self_published(words: list[str]) -> bool:
    """Whether a publisher's name, as norm's words, is a self-publishing platform's (SELF_PUBLISHED)."""
    return any(words[:len(p)] == p for p in (norm(name).split() for name in SELF_PUBLISHED))


ARTICLES = frozenset({"the", "a", "an"})  # not counted among a title's words: norm knows no other language's
LONG_TITLE = 3  # words at least, articles left out, in a title whose very match is taken to be the book


def same_long_title(q: Query, match) -> bool:
    """Whether a match found by the title alone has the very title asked, by norm's words, and that title is long
    enough to be one book's: three words or more, articles left out ("The Lantern Keeper of Moss Harbour", not "The
    Garden"). A subtitle after a colon may differ on either side: the title asked is the title before it (bare_title),
    and so is the match's; or the two are the same with their subtitles, as a source that gives the match's title and
    subtitle as one text has it."""
    asked = norm(bare_title(q.title))
    if len([w for w in asked.split() if w not in ARTICLES]) < LONG_TITLE or not isinstance(match, str):
        return False
    return norm(bare_title(match)) == asked or norm(match) == norm(q.title)


def too_late(q: Query, found: dict) -> bool:
    """A comic's answer dated more than a year after it was bought is another edition, and often another book: a
    reprint, a translation, a later series of the same name, a novel. (A year's slack, as a comic's cover date runs a
    few months ahead of the day it went on sale.) A book's later edition is still the same book, by the same author,
    so books are left alone. Checked when the catalog is built, so a cached answer drops out without a new request."""
    year, bought = str(found.get("year") or "")[:4], q.bought
    return (q.kind == "comic" and year.isascii() and year.isdigit() and bought.isascii() and bought.isdigit() and len(year) == len(bought) == 4
            and int(year) > int(bought) + 1)


PHONE = re.compile(r"\+?\d?[-\s.]?\(?\d{3}\)?[-\s.]\d{3}[-\s.]\d{4}")
LINK = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
SELLING = re.compile(r"customer (service|support|care)|toll[- ]free|helpline|whatsapp|casino|betting|\bbet\b|"
                     r"\bbonus\b|promo code|\bcrypto\b|24/7", re.I)
MARKED_LINK = re.compile(r"\[[^\[\]\n]*\]\([^()\[\]\s]*\)")  # [its words](its address): the link's words are the link's
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")  # where a sentence ends, for a selling word next to a link: one quick step a
# place, as a run of spaces is matched once from its start


def advert(text: str) -> bool:
    """Whether a description is an advert someone put on a wiki page: selling words with a phone number, or with a link
    and more than one sign of selling: two selling words, or one in the link's own sentence. A real description can
    have one: a bonus story, a bet, its code at a link a sentence on. Or three phone numbers, whatever the words.
    Selling words count only outside the links, words and address: a link to example.org/bonus, or a [bonus
    story](…), sells nothing."""
    phones = PHONE.findall(text)
    if len(phones) >= 3:
        return True
    selling = {m[0].lower() for m in SELLING.finditer(outside_links(text))}
    if not selling or not phones and not LINK.search(text):
        return False
    return (bool(phones) or len(selling) >= 2
            or any(LINK.search(part) and SELLING.search(outside_links(part)) for part in SENTENCE.split(text)))


def outside_links(text: str) -> str:
    """A text without its links: a Markdown link with its words, and a bare address."""
    return LINK.sub(" ", MARKED_LINK.sub(" ", text))


MAX_DESCRIPTION = 20_000  # characters of a looked-up description that are kept: far more than the page shows (1,500), and
# a bound on the work for one that a vandal or a bad paste made huge


def plain_description(text: str) -> str:
    """A looked-up description as plain text, or "" when it can't be one, so that a later source's fills in: an
    advert someone put on a wiki page (advert), or nothing left but a link.
    Markdown links and emphasis, which Open Library's descriptions use, become their words. Every catalog build runs
    this over every saved description, and anyone can edit the sources' pages, so no pattern here may backtrack over a
    long run of one character: 3,200 spaces once took minutes (test_a_long_run_in_a_description_is_cleaned_at_once)."""
    text = (text or "")[:MAX_DESCRIPTION].strip()
    if not text or advert(text):
        return ""
    text = re.sub(r"\[\d+\]:\s*\S+", " ", text)  # [1]: https://… (a link's address, given apart)
    # a link's words and address end at the next [, where the next try begins
    text = re.sub(r"\[([^\[\]\n]+)\]\[\d+\]", r"\1", text)  # [The Lantern Keeper][1]
    text = re.sub(r"\[([^\[\]\n]+)\]\((?:https?://|/)[^)\[\s]*\)", r"\1", text)  # [The Harbour](https://…)
    text = re.sub(r"(\*\*|__)(?=\S)((?:(?!\1).)+?)(?<=\S)\1", r"\2", text)  # **bold**, up to the next ** at most
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", text)  # *emphasis*
    text = re.sub(r"(?m)^[ \t]*(#{1,6}\s+|[-*_]{3,}\s*$)", " ", text)  # ## headings and ---- lines
    linked = bool(LINK.search(text))
    text = re.sub(r"\s+", " ", LINK.sub(" ", text)).strip()  # a run of spaces as one, before the patterns with \s*
    text = re.sub(r"\s*[-–—]?\s*\b(see more at|read more at|more at|source|see)\s*:?\s*$", "", text, flags=re.I)
    text = re.sub(r"\(\s*\.?\s*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = len(re.findall(r"\w+", text))
    return "" if not words or linked and words < 4 else text  # 'see https://…' leaves nothing worth showing


TAG = re.compile(r"<[^<>]+>")  # an HTML tag; never across a <, so a run of them isn't scanned to the end from each one


def plain_html(text: str) -> str:
    """A site's text in HTML (Comic Vine's descriptions, Google Books') as plain text: each tag a space, then its
    entities the characters they stand for (&amp; is &), and each run of spaces one space."""
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", text))).strip()


def cache_file(root: Path, s: Source) -> Path:
    return cache_dir(root) / f"lookup-{s.id}.jsonl"


HALF = re.compile("[\ud800-\udfff]")  # half of a character beyond U+FFFF, which JSON's \ud83d can bring alone


def mended(value):
    """A site's text as it can be written: a half character alone (the end of a description cut through an emoji)
    becomes U+FFFD, in strings, lists and dicts alike; UTF-8 can't write it, so it would stop the cache's write. And a
    tuple, at any depth, is a list, as the cache's file gives it back: the answer a run keeps for a later source's plan
    is the one a fresh read would give (borrowed reads publishers and years only as lists)."""
    if isinstance(value, str):
        return HALF.sub("�", value)
    if isinstance(value, (list, tuple)):
        return [mended(v) for v in value]
    if isinstance(value, dict):
        return {(mended(k) if isinstance(k, str) else k): mended(v) for k, v in value.items()}  # other keys as they are:
    return value  # a tuple as a key stays one, and answer() finds it can't be written as JSON


def answer(src: Source, found) -> dict | None:
    """What find() gave, as it is cached: None, or a dict with a number for its score, its text mended (mended()) and
    its description cut to MAX_DESCRIPTION. Anything else, or what can't be written as JSON, is a slip in the plugin,
    raised as Changed so the run counts it and goes on."""
    if found is None:
        return None
    if not isinstance(found, dict) or not isinstance(found.get("score", 1.0), (int, float)):
        raise Changed(f"{src.name}: the plugin gave an answer that isn't a dict with a number for its score; it needs a fix.")
    description = found.get("description")
    if description is not None and not isinstance(description, str):
        raise Changed(f"{src.name}: the plugin gave a description that isn't text; it needs a fix.")
    if description and len(description) > MAX_DESCRIPTION:
        found = {**found, "description": description[:MAX_DESCRIPTION]}
    found = mended(found)
    try:
        json.dumps(found, ensure_ascii=False, allow_nan=False).encode("utf-8")  # as Cache.put writes it
    except (TypeError, ValueError):
        raise Changed(f"{src.name}: the plugin gave an answer that can't be written down as JSON; it needs a fix.") from None
    return found


def whole(value) -> int | None:
    """A site's id as a plugin puts it in its next question's path: a whole number, given as one or as plain digits;
    None for anything else (other text, a fraction, true or false, a number too long to be an id), which is an answer
    of a shape the plugin doesn't know."""
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**18 else None


def texts(d: dict, *keys: str) -> bool:
    """Whether each of these fields of a site's answer is text, or nothing (missing or null), as a plugin reads them:
    one of another shape (a number, a list) is an answer the plugin doesn't know, to be raised as Changed."""
    return all(isinstance(d.get(k), (str, type(None))) for k in keys)


def names(value) -> bool:
    """Whether a list in a site's answer is one of names (authors, subjects), or nothing: given as text, each of its
    letters would be taken for a name; a number among them is an answer the plugin doesn't know."""
    return isinstance(value or [], list) and all(isinstance(x, str) for x in value or [])


def their_words(value, limit: int = 200) -> str:
    """A site's own words, for a message that goes to the cache, the log and a notification: on one line, without
    control characters or half characters, and at most limit characters long, however much the site sent."""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") else c for c in str(value)[:2 * limit])
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def slip(e: BaseException) -> str:
    """An exception a plugin let out, for the cache and the log: its kind and the plugin's last line it went through
    ("KeyError at comicvine.py:85"), never its words, which can quote an answer, or a request with its key in it."""
    here = Path(__file__)
    plugins = [f for f in traceback.extract_tb(e.__traceback__) if Path(f.filename).parent == here.parent
               and Path(f.filename).name != here.name]
    return type(e).__name__ + (f" at {Path(plugins[-1].filename).name}:{plugins[-1].lineno}" if plugins else "")


# ---- a run ----------------------------------------------------------------------------------

OFF = ("Lookups are off (enabled = false under [lookups] in the settings): no source is asked. What was found before "
       "stays in the catalog.")


NO_KEY = "{name}: no key saved (hylly login {login})"  # why available() leaves a source out, when its key is missing


def key_missing(why: str) -> bool:
    """Whether available()'s reason for leaving a source out is its missing key (NO_KEY): its owner's to log in."""
    return NO_KEY.partition("{login}")[0].partition("{name}")[2] in why and why.endswith(")")


def available(chosen: list[str] | None = None) -> tuple[list[Source], list[str]]:
    """The sources to ask (chosen, or every one), with their keys; and why the others are left out: no key saved, or
    chosen and left out of order under [lookups] (not the order's own leave-outs, which nobody chose). With lookups off
    in the settings, none, and that why (OFF): hylly lookup, hylly catalog --lookup and the Inbox's pass all ask here,
    and each says it."""
    from .. import settings
    from ..logins import AuthError, load_session

    if not settings.get("lookups")["enabled"]:
        return [], [OFF]
    out, skipped = [], []
    order = in_order()
    known = sources() if chosen and set(chosen) - set(order) else {}
    for sid in chosen or ():  # chosen with hylly lookup --source, and left out of the settings' order: not asked
        if sid not in order:
            skipped.append(f"{known[sid].name if sid in known else sid}: left out of order under [lookups]")
    for sid, cls in order.items():
        if chosen and sid not in chosen:
            continue
        key = ""
        if cls.credential:
            try:
                key = load_session(cls.credential)
            except AuthError:
                skipped.append(NO_KEY.format(name=cls.name, login=cls.credential))
                continue
        out.append(cls(key))
    return out, skipped


class Plan:
    """Who is asked about what in a run, with no request: the sources in the order they're asked (chosen), and for each
    (of), the titles it is for and has no standing answer for, nor your word on (a match you used or refused: yours,
    which stands as a refused line in the cache does, --again or not), that still lack something it wants once the
    sources before it have answered. A run plans each source's turn as it comes; a dry run only plans (dry_run).
    Every source's cache is read once, the first time it's needed. A source's turn puts its answers in that same one, so
    a later source's plan sees the answers of this run, as a new read of the file would."""

    def __init__(self, root: Path, chosen: list[Source], *, again: bool = False, kinds: set[str] | None = None) -> None:
        self.root, self.again, self.kinds = root, again, kinds
        self.asked = in_order()
        self.rank = list(self.asked).index  # chosen are of these: available() gives no other
        self.asking = {s.id for s in chosen}
        self.good = accept()
        self.decided = yours(root)  # a source whose match for a title you used or refused isn't asked about it again
        self.chosen = sorted(chosen, key=lambda s: self.rank(s.id))
        self.caches: dict[str, Cache] = {}

    def cache(self, s: Source) -> Cache:
        """A source's cache, read from its file the first time it's needed in this run."""
        if s.id not in self.caches:
            self.caches[s.id] = Cache(cache_file(self.root, s))
        return self.caches[s.id]

    def of(self, src: Source, queries: list[Query]) -> tuple[dict[str, list[Query]], int]:
        """What src is asked: its questions by key, each with the titles asked that way, in the order of queries; and
        how many titles wait until a source before it has answered them."""
        cache = self.cache(src)
        earlier = [(e, self.cache(e)) for e in (cls() for sid, cls in self.asked.items()  # a later source is asked
                   if self.rank(sid) < self.rank(src.id) and cls.kinds & src.kinds)]  # only what the earlier ones lack
        todo: dict[str, list[Query]] = {}
        waiting = 0
        for q in queries:
            if (self.kinds and q.kind not in self.kinds) or not src.needs(q) or self.decided.get(q.id, {}).get(src.id):
                continue
            if earlier:
                have, pending = set(q.has), False
                for e_src, c in earlier:
                    e = c.by_id.get(q.id) or c.get(e_src.key(q))
                    said = self.decided.get(q.id, {}).get(e_src.id) or {}
                    if said.get("a") == "refused":  # refused by you: it gives the title nothing, so a later source tries,
                        continue  # as with a refused line in its cache
                    got = usable(e_src, e, q, said, self.good)  # what the catalog takes from it: what it
                    if got is not None:  # leaves out, a later source is asked for (an advert, a too late answer)
                        have |= {f for f, v in got.items() if v}
                    elif e_src.id in self.asking and e_src.needs(q) and not settled(e, again=False):
                        pending = True  # an earlier source hasn't answered it yet (stopped at its quota, say)
                if src.wants <= have:
                    continue
                if pending:
                    waiting += 1
                    continue
            key = src.key(q)  # by key only: a title edited since its answer is asked again
            if not settled(cache.get(key), self.again):
                todo.setdefault(key, []).append(q)
        return todo, waiting


def dry_run(root: Path, queries: list[Query], chosen: list[Source], *, again: bool = False,
            kinds: set[str] | None = None) -> dict[str, dict]:
    """What a run would ask, from its plan alone, for hylly lookup --dry-run: no request, and nothing written. By
    source, in the order they're asked: "left", the questions it would ask, and "waiting", the titles that wait for the
    sources before it. As nothing is answered, every title a source before it is still to answer waits."""
    plan = Plan(root, chosen, again=again, kinds=kinds)
    counted = {}
    for src in plan.chosen:
        todo, waiting = plan.of(src, queries)
        counted[src.id] = {"left": len(todo), "waiting": waiting}
    return counted


def run(root: Path, queries: list[Query], chosen: list[Source], *, limit: int | None = None, again: bool = False,
        kinds: set[str] | None = None, log: Callable[[str], None] = print, notify: Callable[[str, str], None] | None = None,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time, progress_every: float = 60.0) -> dict[str, dict]:
    """Ask each source about the titles it is for and has no standing answer for, nor your word on, that still lack
    something it wants once the sources before it have answered (Plan); returns counts per source. Each source's turn is
    planned as it comes, with the answers of those before it in this run, then asked (ask) and told (told).
    A problem with one source stops only that source; a local one (the cache can't be written, or the library isn't
    there any more, looked at every LIBRARY_EVERY titles) stops the run. Whatever else a plugin's find() lets out counts
    as Changed: one odd answer, or a slip in a plugin, never ends the run."""
    stats: dict[str, dict] = {}
    plan = Plan(root, chosen, again=again, kinds=kinds)
    for src in plan.chosen:
        todo, waiting = plan.of(src, queries)
        keys = list(todo)[:limit] if limit is not None else list(todo)
        st = stats[src.id] = {"asked": 0, "found": 0, "none": 0, "errors": 0, "left": len(todo) - len(keys), "stopped": "",
                              "waiting": waiting}
        if waiting:
            log(f"{src.name}: {waiting} titles wait until the sources before it have answered them.")
        if not keys:
            log(f"{src.name}: nothing new to look up.")
            continue
        log(f"{src.name}: {len(keys)} titles to look up" + (f" ({st['left']} more after this run)" if st["left"] else "")
            + f", at most one request every {src.interval:g} s" + (f", {src.per_hour} an hour" if src.per_hour else "")
            + (f", {src.per_day} a day" if src.per_day else "") + ".")
        ask(root, src, {key: todo[key] for key in keys}, st, plan.cache(src), plan.good, log=log, transport=transport,
            sleep=sleep, now=now, progress_every=progress_every)
        told(src, st, log, notify)
    return stats


def ask(root: Path, src: Source, todo: dict[str, list[Query]], st: dict, cache: Cache, good: float, *,
        log: Callable[[str], None], transport: httpx.BaseTransport | None, sleep: Callable[[float], None],
        now: Callable[[], float], progress_every: float) -> None:
    """A source's turn: each question of todo asked in turn, in one session closed however the turn ends, each answer
    put in the cache as it comes and counted in st. A spent quota or a refused login ends the turn; a title that fails
    is kept as an error, asked again next run, and three in a row end the turn: as the site down when the third is
    Transient, else as answers that changed (Changed, or anything else find() lets out). Why it ended goes in
    st["stopped"], with "quota", "refused" or "changed" for cli to read. Ctrl+C, the library gone and a cache that can't
    be written end the run: raised, the last two as LookupProblem."""
    budget = Budget(cache_dir(root) / f"lookup-budget-{src.id}.txt", src.per_hour, src.per_day, now)
    s = Session(src, budget, transport=transport, sleep=sleep, now=now, log=log)
    started, last_report, failed = now(), now(), 0  # failed: titles in a row
    try:
        for n, (key, qs) in enumerate(todo.items(), 1):
            # the library's share gone for a moment is waited for, as a download waits
            if (n - 1) % LIBRARY_EVERY == 0 and paths.library_missing(root, sleep):
                message = f"The library at {root} isn't there any more, so the run stopped. What was looked up is kept."
                log(message)
                raise LookupProblem(message)  # the job's note says so, as for a cache that can't be written
            ids = sorted({q.id for q in qs})
            try:
                found = answer(src, src.find(s, qs[0]))
                failed = 0
            except Quota as e:
                st["stopped"], st["quota"] = mended(str(e)), True  # expected pacing: logged, but no notification
                break
            except Refused as e:
                st["stopped"], st["refused"] = mended(str(e)), True
                break
            except Exception as e:  # noqa: BLE001 - kept as the title's error: Changed, Transient, or anything else
                # from the plugin, an answer it didn't foresee
                failed += 1
                st["errors"] += 1
                why = (mended(str(e)) if isinstance(e, LookupProblem)
                       else f"{src.name}: the plugin couldn't read an answer ({slip(e)}).")
                cache.put(key, ids, "error", error=why)
                if failed < CHANGED_IN_A_ROW:
                    continue
                if isinstance(e, Transient):  # the third says which: the site down, or answers this plugin can't read
                    st["stopped"] = f"{e} Three titles in a row failed: {src.name} seems down. The next run continues."
                else:
                    st["stopped"] = f"{why} Three answers in a row couldn't be read: the {src.name} plugin may need an update."
                    st["changed"] = True
                break
            st["asked"] += 1
            if found and found.get("score", 1.0) >= good:
                st["found"] += 1
                cache.put(key, ids, "found", found)
            else:
                st["none"] += 1
                cache.put(key, ids, "none", {"near": found} if found else None)
            if now() - last_report >= progress_every:
                last_report = now()
                log(time_to_go(src, s, n, len(todo), st["found"], started, now))
    except KeyboardInterrupt:
        st["stopped"] = "Stopped with Ctrl+C; the next run continues where this one stopped."
        log(f"{src.name}: {st['stopped']}")
        raise
    except OSError as e:
        message = f"Can't write the lookup cache ({e.__class__.__name__}: {e.strerror or e}). Nothing is lost; run it again later."
        log(message)
        raise LookupProblem(message) from None  # the job's note says so: both ways here are watched jobs
    finally:
        s.close()


def time_to_go(src: Source, s: Session, n: int, total: int, found: int, started: float,
               now: Callable[[], float]) -> str:
    """The progress line, n of total looked up: the time to go is the longer of the speed until now and the pace of the
    source within its hourly budget (Session.done_by), which it names when that is what holds the run back."""
    left = total - n
    so_far = left / (n / max(now() - started, 1e-6))  # at the speed until now
    paced = s.done_by(round(left * s.requests / n)) - now()  # the hourly budget may hold it back more
    return (f"  {src.name}: {n}/{total} looked up, {found} found; {time_words(max(so_far, paced), about=True)} to go"
            + (f", at {src.per_hour} requests an hour." if src.per_hour and paced > 1.2 * so_far else "."))


def told(src: Source, st: dict, log: Callable[[str], None], notify: Callable[[str, str], None] | None) -> None:
    """How a source's turn ended, in the log, and why it stopped if it did; that also as a notification, but for a spent
    quota, which is expected pacing. Ctrl+C never comes here: ask raises it again."""
    log(f"{src.name}: {st['asked']} looked up, {st['found']} found, {st['none']} not found, {st['errors']} errors"
        + (f". Stopped: {st['stopped']}" if st["stopped"] else "."))
    if st["stopped"] and notify and not st.get("quota"):
        notify(f"Library lookups: {src.name} stopped", st["stopped"])
