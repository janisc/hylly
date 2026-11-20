"""The library feeds, OPDS 1.2 (Atom), for reading apps on a tablet or a phone, and e-readers: each feed address answered
(answer) from the catalog as the server has it (library.Catalog) and who is asking (Asking), by one function for each kind
of page, found by the address under the feed's own (PAGES). What a page shows of the person's records (the lists in the
apps, theirs, their shelves, Best rated and Most read) is worked out only for a page that shows it.

  /opds                      navigation: new, by source, kind, game, genre, series, search
  /opds/added                what came into the library in the last week
  /opds/new                  recently bought
  /opds/{src|kind|game|genre}/X   one facet value, 50 per page (?page=N)
  /opds/series[/KEY]         every series, and one series in volume order
  /opds/search?q=...         search (OpenSearch description at /opds/opensearch.xml)
  /opds/list/KEY             an owner's list ticked for the reading apps, in its own order; the Featured one first at the start
  /opds/shelf/ID/...         a shelf ticked for the reading apps, as a feed of its own: the same tree, with only what's on it
  /opds/best, /opds/popular  Best rated (while stars are on) and Most read (titles at least MOST_READ_FROM people have read in)
  A listing of more than AZ_FROM titles, or series, starts with an A to Z index: ?letter=X for one letter, ?all=1 for all
  /ereader/opds/...          the same, listing only books that suit an e-ink reader, each with its one
                             best file and an information text ([ereader_feed] in settings.toml)
Every feed is the logged-in person's own: it starts with what you're reading (/continue), your reading list (/reading)
and your own lists ticked for the apps (/mine/ID), then the whole library as above. /me/opds/... and
/me/ereader/opds/... are the same feeds, under the addresses they had when only they were your own. The server sends
every feed page in a sandbox of its own (serve.SANDBOX), as a browser shows one as an XML document.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape as _escape

from . import rules, shelves
from .library import MIME, Catalog, Device, file_size, kinds_table, series_key, sources, whole
from .shops import labels as shop_labels
from .words import many, size_words

PAGE = 50
NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"

# What XML 1.0 can't hold at all, not even written as a reference: the control characters but tab and the line ends, a
# half of a surrogate pair standing alone (a JSON file can hold one, as \ud800), and U+FFFE and U+FFFF
NOT_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def escape(text: str) -> str:
    """For the feeds' XML, in text and in attributes alike: a quote in a tag or a genre can't end an attribute. What XML
    can't hold (NOT_XML) is left out, so a feed page stays one a reading app's XML reader takes, whatever a book file's
    title or a search holds; the catalog cleans its records as it builds them, and this is the second wall."""
    return _escape(NOT_XML.sub("", text), {'"': "&quot;"})


def details(it: dict, f: list, labels: dict[str, str] | None = None) -> str:
    """The information text a reading app shows for a title (KOReader: Book information). labels: each source's name
    (Catalog.labels), the shops' when none are given."""
    labels = shop_labels() if labels is None else labels
    lines = [f"{f[1].upper()}, {size_words(file_size(f))}"]
    if it.get("s"):
        lines.append(f"Series: {it['s']}" + (f", #{it['n']}" if it.get("n") else ""))
    for label, value in (("Genres", ", ".join(it.get("g", []))), ("Tags", ", ".join(it.get("tg", []))),
                         ("Published", it.get("y")), ("Publisher", it.get("p")),
                         ("From", ", ".join(x for x in (labels.get(it.get("src"), ""), it.get("b") or it.get("col")) if x))):
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) + (f"\n\n{it['d']}" if it.get("d") else "")


def looked_up(it: dict) -> list[tuple[str, str]]:
    """Where a title's looked-up details came from, as the build kept it (lk: each source's name and its page for the
    title): (name, page), the page only an https address, as the library page links it. The feeds credit them as that
    page's Details from does, as the sources' terms ask (THIRD-PARTY-NOTICES.md). What an older catalog, or one changed
    by hand, holds in another shape, a number or a word in place of the list or of one of its pairs, gives nothing."""
    lk = it.get("lk")
    return [(str(x[0]), x[1] if len(x) > 1 and isinstance(x[1], str) and x[1].startswith("https://") else "")
            for x in (lk if isinstance(lk, list) else []) if isinstance(x, list) and x and x[0]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def feed(self_url: str, title: str, kind: str, entries: list[str], links: str = "", base: str = "/opds") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog" '
        'xmlns:dc="http://purl.org/dc/terms/">\n'
        f"<id>urn:hylly:{escape(self_url)}</id><title>{escape(title)}</title><updated>{_now()}</updated>\n"
        f'<link rel="self" href="{escape(self_url)}" type="{kind}"/>\n'
        f'<link rel="start" href="{base}" type="{NAV}"/>\n'
        f'<link rel="search" href="{base}/opensearch.xml" type="application/opensearchdescription+xml"/>\n'
        f"{links}{''.join(entries)}</feed>\n"
    )


def nav_entry(title: str, href: str, content: str = "", kind: str = ACQ) -> str:
    return (f"<entry><title>{escape(title)}</title><id>urn:hylly:nav:{escape(href)}</id><updated>{_now()}</updated>"
            f'<content type="text">{escape(content)}</content><link rel="subsection" href="{escape(href)}" type="{kind}"/></entry>\n')


def book_entry(it: dict, device: Device | None = None, labels: dict[str, str] | None = None) -> str:
    authors = "".join(f"<author><name>{escape(a)}</name></author>" for a in it.get("a", []))
    cats = "".join(f'<category term="{escape(g)}" label="{escape(g)}"/>' for g in it.get("g", []) + it.get("tg", []))
    title = it["t"]  # comic titles carry their volume already ("Lantern Wolf Vol. 1"); a novel's series number would only confuse
    cover = it.get("c", "")
    cover = cover if cover.startswith("http") else (f"/catalog/{cover}" if cover else "")
    images = (f'<link rel="http://opds-spec.org/image" href="{escape(cover)}" type="{MIME.get(cover.rsplit(".", 1)[-1], "image/jpeg")}"/>'
              f'<link rel="http://opds-spec.org/image/thumbnail" href="{escape(cover)}" type="{MIME.get(cover.rsplit(".", 1)[-1], "image/jpeg")}"/>'
              if cover else "")
    offered = [device.best_file(it)] if device else [f for f in it.get("f", []) if f[0] not in it.get("bad", {})]
    files = "".join(
        f'<link rel="http://opds-spec.org/acquisition" href="/{escape(path)}" type="{MIME.get(ext, "application/octet-stream")}" length="{size}"/>'
        for path, ext, size in offered
    )
    series = f"{it['s']}{' #' + it['n'] if it.get('n') else ''}" if it.get("s") else ""
    summary = (details(it, offered[0], labels) if device
               else (f"Series: {series}. " if series else "") + (it.get("d") or ", ".join(x for x in (it.get("b"), it.get("col"), it.get("p")) if x)))
    credited = looked_up(it)  # credited at the summary's end, and in the tablets' feed each source's page linked
    if credited:
        summary += ("\n\n" if summary else "") + "Details from " + ", ".join(name for name, _ in credited)
    # With no type: an app that offers every link of a type it can open (KOReader, which opens HTML) would take a
    # source's page for a download. Nor in the e-reader's feed, where the summary is all its app shows of these
    related = "" if device else "".join(f'<link rel="related" href="{escape(page)}" title="{escape(name)}"/>'
                                        for name, page in credited if page)
    issued = f"<dc:issued>{escape(it['y'])}</dc:issued>" if it.get("y") else ""
    publisher = f"<dc:publisher>{escape(it['p'])}</dc:publisher>" if it.get("p") else ""
    return (f"<entry><title>{escape(title)}</title><id>urn:hylly:{escape(it['id'])}</id><updated>{_now()}</updated>"
            f"{authors}{issued}{publisher}{cats}<summary type=\"text\">{escape(summary)}</summary>{images}{files}{related}</entry>\n")


def something_to_offer(it: dict) -> bool:
    """Not a title whose every file is locked or damaged: no device could read what a feed would hand it."""
    bad = it.get("bad")
    return not bad or bool(it.get("z")) or any(f[0] not in bad for f in it.get("f", []))


def paged(self_url: str, title: str, items: list[dict], page: int, device: Device | None = None, base: str = "/opds",
          labels: dict[str, str] | None = None) -> str:
    start = page * PAGE
    entries = [book_entry(it, device, labels) for it in items[start: start + PAGE]]
    here, _, query = self_url.partition("?")
    query = re.sub(r"(^|&)page=[^&]*", "", query).strip("&")
    link = lambda p: f"{here}?{query + '&' if query else ''}page={p}"  # noqa: E731
    links = ""
    if start + PAGE < len(items):
        links += f'<link rel="next" href="{escape(link(page + 1))}" type="{ACQ}"/>\n'
    if page:
        links += f'<link rel="previous" href="{escape(link(page - 1))}" type="{ACQ}"/>\n'
    return feed(self_url, f"{title} ({len(items)})", ACQ, entries, links, base=base)


ADDED_DAYS = 7  # how long a title stays in the feeds' Recently added
AZ_FROM = 100  # a listing longer than this starts with an A to Z index, for apps without a good search
MOST_READ_FROM = 2  # Most read lists a title once this many people have a place in it: one person's reading isn't shown to everyone
AZ_LAST = ("0–9", "#")  # after A to Z, in this order


def letter_of(it: dict) -> str:
    """A title's letter in the A to Z index: the first of what its listing goes by, its series or else its title."""
    first = shelves.norm(it.get("s") or it.get("t") or "")[:1]
    return first.upper() if first.isalpha() else "0–9" if first.isdigit() else "#"


def titles(n: int) -> str:
    """A feed's count of titles, as words.many says it."""
    return many(n, "title")


def letter_order(letter: str) -> tuple:
    return (AZ_LAST.index(letter) + 1 if letter in AZ_LAST else 0, letter)


def with_query(url: str, extra: str) -> str:
    """The address with one more parameter, and no page: a letter or All starts at its first page."""
    here, _, query = url.partition("?")
    query = re.sub(r"(^|&)(page|letter|all)=[^&]*", "", query).strip("&")
    return f"{here}?{query + '&' if query else ''}{extra}"


def recently_added(items: list[dict], now: datetime | None = None) -> list[dict]:
    """What came into the library in the last week, by the date catalog.first_seen gave it, the newest first and a batch
    by title. It stays for the week: unlike the page's New, a feed doesn't know who's asking or what they've seen."""
    since = ((now or datetime.now()) - timedelta(days=ADDED_DAYS)).isoformat(timespec="seconds")
    came = [i for i in items if isinstance(i.get("nw"), str) and i["nw"] >= since]
    return sorted(sorted(came, key=lambda i: i["t"].lower()), key=lambda i: i["nw"], reverse=True)


# ---- who is asking, and the page asked for ------------------------------------------------------------------------

class Asking:
    """Who asks for a feed, and what of theirs it may show: each part a function, called the first time a page needs it
    and once at most. lists: those ticked for the reading apps that everyone's feed shows, {key, name, featured, ids (in
    its order)}, the Featured one first (Handler._feed_lists); mine: what's theirs, {continue, reading: ids in order,
    lists: [{id, name, ids}]} (Handler._my_feed); shelves: those ticked for the reading apps, {id, name, rule}
    (Handler._feed_shelves); ranks: Best rated and Most read, {best, most: ids in order} (Handler._feed_ranks).
    by: whose feed it is, named in its title; None for a feed of nobody's own, which shows nothing of anyone's. The
    start page reads them all, a shelf's address the shelves, and the search's description or an address that is no
    page none."""

    def __init__(self, by: str | None = None, lists: Callable[[], list[dict]] = list, mine: Callable[[], dict] = dict,
                 shelves: Callable[[], list[dict]] = list, ranks: Callable[[], dict] = dict) -> None:
        self.by = by
        self._lists, self._mine, self._shelves, self._ranks = lists, mine, shelves, ranks

    @functools.cached_property
    def lists(self) -> list[dict]:
        return self._lists()

    @functools.cached_property
    def mine(self) -> dict:
        return self._mine()

    @functools.cached_property
    def shelves(self) -> list[dict]:
        return self._shelves()

    @functools.cached_property
    def ranks(self) -> dict:
        return self._ranks()


class Feed:
    """One page of a feed asked for, as the page functions take it: the address (url, as asked; path, decoded and
    without a / at its end; its query), the feed's own address (base: /opds or /ereader/opds, under /me if asked so,
    inside a shelf when one is asked for) and the page under it (rest), the titles it lists (items: those the device
    suits, or that have something to offer, and inside a shelf those on it), the sources' names as it began, and who
    is asking: inside a shelf nobody's own, as a shelf's feed shows no lists, nothing of yours and no shelves. What it
    shows of theirs is worked out when a page first asks."""

    def __init__(self, cat: Catalog, url: str, path: str, qs: dict, base: str, rest: str | None, device: Device | None,
                 library: str, asking: Asking, shelf: dict | None, wanted: Callable[[dict], bool]) -> None:
        self.cat, self.url, self.path, self.qs, self.base, self.rest = cat, url, path, qs, base, rest
        self.device, self.asking, self.shelf, self.wanted = device, asking, shelf, wanted
        self.items = [i for i in cat.get() if wanted(i)]
        self.labels = cat.labels  # the sources' names: a reload while this feed is made puts others in place, not here
        self.by = None if shelf else asking.by
        self.page, self.letter = whole(qs.get("page"), 0), (qs.get("letter") or [""])[0]
        self.name = ((f"{library} for e-readers" if device else library)
                     + (f" · {self.by}" if self.by is not None else "") + (f": {shelf['name']}" if shelf else ""))

    @functools.cached_property
    def by_id(self) -> dict[str, dict]:
        return {i["id"]: i for i in self.items}

    def _titles(self, ids: list[str]) -> list[dict]:
        return [self.by_id[i] for i in ids if i in self.by_id]

    def _offered(self, chosen: list[dict]) -> list[dict]:
        """Lists with their titles in this feed, in their own order; one with none of them isn't offered."""
        return [c for c in ({**c, "items": self._titles(c["ids"])} for c in chosen) if c["items"]]

    @functools.cached_property
    def shown(self) -> list[dict]:
        """The lists everyone's feed shows (Asking.lists)."""
        return [] if self.shelf else self._offered(self.asking.lists)

    @functools.cached_property
    def mine(self) -> dict:
        return {} if self.by is None else self.asking.mine

    @functools.cached_property
    def going(self) -> list[dict]:
        return self._titles(self.mine.get("continue", []))

    @functools.cached_property
    def reading(self) -> list[dict]:
        return self._titles(self.mine.get("reading", []))

    @functools.cached_property
    def own(self) -> list[dict]:
        return self._offered(self.mine.get("lists", []))

    @functools.cached_property
    def shelves(self) -> list[dict]:
        return [] if self.shelf else self.asking.shelves

    @functools.cached_property
    def ranked(self) -> tuple[list[dict], list[dict]]:
        """Best rated and Most read, the titles of this feed in their order."""
        return self._titles(self.asking.ranks.get("best", [])), self._titles(self.asking.ranks.get("most", []))

    def paged(self, title: str, chosen: list[dict]) -> str:
        return paged(self.url, title, chosen, self.page, self.device, self.base, self.labels)


Answer = tuple[int, str, str]  # status, content type, body
NOT_FOUND: Answer = (404, "text/plain", "not found")


def _number(it: dict) -> float:
    """A title's number, by its start: 93b is 93, and 1977-03 is 1977."""
    m = re.match(r"\d+(\.\d+)?", it.get("n", ""))
    return float(m[0]) if m else 0


def by_series_and_title(chosen: list[dict]) -> list[dict]:
    return sorted(chosen, key=lambda it: (it.get("s", "").lower() or it["t"].lower(), _number(it), it["t"].lower()))


def a_to_z(f: Feed, title: str, letters: Counter, every: tuple[str, str], count: Callable[[int], str],
           kind: str = ACQ) -> str:
    """An A to Z index, for apps without a good search: all of them first (every: its title and words), then each
    letter with how many it has (count), A to Z, then digits and the rest (letter_order)."""
    entries = [nav_entry(every[0], with_query(f.url, "all=1"), every[1], kind=kind)]
    entries += [nav_entry(k, with_query(f.url, "letter=" + quote(k)), count(n), kind=kind)
                for k, n in sorted(letters.items(), key=lambda kv: letter_order(kv[0]))]
    return feed(f.url, title, NAV, entries, base=f.base)


def listing(f: Feed, title: str, chosen: list[dict], index: bool = True) -> tuple[str, str]:
    """(kind, body): the titles by series and title, or first an A to Z index when there are many."""
    ordered = by_series_and_title(chosen)
    if index and len(ordered) > AZ_FROM and not f.letter and "all" not in f.qs:
        every = (f"All {titles(len(ordered))}", "From A to Z")
        return NAV, a_to_z(f, f"{title} ({len(ordered)})", Counter(letter_of(it) for it in ordered), every, titles)
    if f.letter:
        ordered, title = [it for it in ordered if letter_of(it) == f.letter], f"{title}: {f.letter}"
    return ACQ, f.paged(title, ordered)


# ---- the pages: each takes the page asked for and what its address matched -----------------------------------------

def start(f: Feed, m: re.Match) -> Answer:
    """The feed's start: what's yours first, the Featured list and the others, the shelves, then the library's ways
    in."""
    added = len(recently_added(f.items))
    featured = next((chosen for chosen in f.shown if chosen["featured"]), None)
    links = (f'<link rel="http://opds-spec.org/featured" href="{f.base}/list/{featured["key"]}" type="{ACQ}" title="{escape(featured["name"])}"/>\n'
             if featured else "")  # OPDS's own word for it, which some apps show apart
    entries = ([nav_entry("Continue reading", f"{f.base}/continue", f"{titles(len(f.going))} you're in the middle of, the latest first"),
                nav_entry("Reading list", f"{f.base}/reading", titles(len(f.reading)))]
               + [nav_entry(chosen["name"], f"{f.base}/mine/{chosen['id']}", titles(len(chosen["items"]))) for chosen in f.own]) if f.by is not None else []
    entries += [nav_entry("Featured", f"{f.base}/list/{featured['key']}", f"{featured['name']}: {titles(len(featured['items']))}")] if featured else []
    entries += [nav_entry(chosen["name"], f"{f.base}/list/{chosen['key']}", titles(len(chosen["items"]))) for chosen in f.shown if chosen is not featured]
    for s in f.shelves:  # a shelf with nothing for this feed isn't offered
        n = sum(1 for it in f.items if s["rule"](it))
        if n:
            entries.append(nav_entry(s["name"], f"{f.base}/shelf/{s['id']}", f"A shelf: {titles(n)}", kind=NAV))
    best, most = f.ranked
    entries += [nav_entry("Recently added", f"{f.base}/added", f"{titles(added)} from the last {ADDED_DAYS} days"),
                nav_entry("Recently bought", f"{f.base}/new", "The newest purchases first")]
    entries += [nav_entry("Best rated", f"{f.base}/best", f"{titles(len(best))} with stars, the best first")] if best else []
    entries += [nav_entry("Most read", f"{f.base}/popular", f"{titles(len(most))} that most of you have read in")] if most else []
    links += f'<link rel="http://opds-spec.org/sort/new" href="{f.base}/added" type="{ACQ}" title="Recently added"/>\n'  # OPDS's own words for them
    links += f'<link rel="http://opds-spec.org/sort/popular" href="{f.base}/popular" type="{ACQ}" title="Most read"/>\n' if most else ""
    entries += [nav_entry(f"{label}", f"{f.base}/src/{key}", titles(sum(1 for i in f.items if key in sources(i))))
                for key, label in f.labels.items() if any(key in sources(i) for i in f.items)]
    entries += [nav_entry("By kind", f"{f.base}/kind", kind=NAV)]
    entries += [nav_entry("By game", f"{f.base}/game", "Role-playing games", kind=NAV)] if any(i.get("gm") for i in f.items) else []
    entries += [nav_entry("By genre", f"{f.base}/genre", kind=NAV)]
    entries += [nav_entry("By tag", f"{f.base}/tag", "Your own tags", kind=NAV)] if any(i.get("tg") for i in f.items) else []
    entries += [nav_entry("Series", f"{f.base}/series", "Series, volumes in order", kind=NAV)]
    return 200, NAV, feed(f.base, f"{f.name} ({len(f.items)})" if f.device or f.shelf else f.name, NAV, entries, links, base=f.base)


def opensearch(f: Feed, m: re.Match) -> Answer:
    """The search's description (OpenSearch), which apps read to search this feed."""
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
            f"<ShortName>{escape(f.name)}</ShortName><Description>Search the library</Description>"
            f'<Url type="{ACQ}" template="{f.base}/search?q={{searchTerms}}"/></OpenSearchDescription>\n')
    return 200, "application/opensearchdescription+xml", body


def search(f: Feed, m: re.Match) -> Answer:
    q = (f.qs.get("q") or [""])[0]
    found = [i for i in f.cat.search(q) if f.wanted(i)]
    return 200, *listing(f, f"Search: {q}", found, index=False)  # a search's answer stays a list, as the apps expect


def ranked(f: Feed, m: re.Match) -> Answer:
    """Best rated (/best) and Most read (/popular), in their order."""
    best, most = f.ranked
    return 200, ACQ, f.paged("Best rated" if m[1] == "best" else "Most read", best if m[1] == "best" else most)


def added(f: Feed, m: re.Match) -> Answer:
    return 200, ACQ, f.paged("Recently added", recently_added(f.items))


def bought(f: Feed, m: re.Match) -> Answer:
    newest = sorted((i for i in f.items if i.get("dt")), key=lambda i: i["dt"], reverse=True)
    return 200, ACQ, f.paged("Recently bought", newest)


FACETS = {"kind": "k", "game": "gm", "genre": "g", "tag": "tg"}  # a facet's address, and the field of a title it lists
LISTED = ("g", "gm", "tg")  # the fields that hold a list: genres, games and tags


def facets(f: Feed, m: re.Match) -> Answer:
    """A facet's values (/kind, /game, /genre, /tag), each with how many titles have it, the most first."""
    plurals = {name: k["plural"] for name, k in kinds_table().items()}
    field = FACETS[m[1]]
    named = plurals if field == "k" else {}
    counts: dict[str, int] = {}
    for it in f.items:
        for v in (it.get(field) if field in LISTED else [it.get(field)]) or []:
            counts[v] = counts.get(v, 0) + 1
    entries = [nav_entry(named.get(v, v), f"{f.path}/{quote(v)}", titles(n)) for v, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return 200, NAV, feed(f.path, {"k": "By kind", "gm": "By game", "g": "By genre", "tg": "By tag"}[field], NAV, entries, base=f.base)


def facet(f: Feed, m: re.Match) -> Answer:
    """The titles of one source (/src/X) or one facet's value (/kind/X, /game/X, /genre/X, /tag/X)."""
    field, value = {"src": "src", **FACETS}[m[1]], m[2]
    chosen = [i for i in f.items if (value in i.get(field, []) if field in LISTED else value in sources(i) if field == "src"
                                     else i.get(field) == value)]
    kind = rules.current().kinds.get(value) if field == "k" else None
    return 200, *listing(f, f.labels.get(value) or (kind.plural if kind else value), chosen)


def every_series(f: Feed, m: re.Match) -> Answer:
    """Every series of more than one volume, by name; first an A to Z index when there are many."""
    groups: dict[str, list[dict]] = {}
    for it in f.items:
        if it.get("s"):
            groups.setdefault(series_key(it), []).append(it)
    found = [(k, v) for k, v in sorted(groups.items(), key=lambda kv: kv[1][0]["s"].lower()) if len(v) > 1]
    if len(found) > AZ_FROM and not f.letter and "all" not in f.qs:
        letters = Counter(letter_of({"t": v[0]["s"]}) for _, v in found)
        every = ("All series", f"{len(found)} series, from A to Z")
        return 200, NAV, a_to_z(f, "Series", letters, every, lambda n: f"{n} series", NAV)
    if f.letter:
        found = [(k, v) for k, v in found if letter_of({"t": v[0]["s"]}) == f.letter]
    entries = [nav_entry(v[0]["s"], f"{f.base}/series/{quote(k)}", many(len(v), "volume")) for k, v in found]
    return 200, NAV, feed(f.url if f.letter else f.path, f"Series: {f.letter}" if f.letter else "Series", NAV, entries, base=f.base)


def one_series(f: Feed, m: re.Match) -> Answer:
    """One series, by its key (of any script: Rules.series_key, as quote() sent it), in volume order."""
    vols = [it for it in f.items if it.get("s") and series_key(it) == m[1]]
    return 200, *listing(f, vols[0]["s"] if vols else "Series", vols, index=False)


def shared_list(f: Feed, m: re.Match) -> Answer:
    """A list everyone's feed shows (Asking.lists), in its own order."""
    found = next((chosen for chosen in f.shown if chosen["key"] == m[1]), None)
    return (200, ACQ, f.paged(found["name"], found["items"])) if found else NOT_FOUND


def yours(f: Feed, m: re.Match) -> Answer:
    """What you're in the middle of, the latest first (/continue), and your reading list (/reading): in your own
    feed."""
    if f.by is None:
        return NOT_FOUND
    going = m[1] == "continue"
    return 200, ACQ, f.paged("Continue reading" if going else "Reading list", f.going if going else f.reading)


def own_list(f: Feed, m: re.Match) -> Answer:
    """A list of your own ticked for the apps, in your own feed, in its own order."""
    found = next((chosen for chosen in f.own if chosen["id"] == m[1]), None)
    return (200, ACQ, f.paged(found["name"], found["items"])) if found else NOT_FOUND


# Each page by its address under the feed's own, the first that matches the whole of it; any other is not found. A
# shelf's own tree is taken off first (SHELF, answer): its address leads to the same pages, with only what's on it.
PAGES: tuple[tuple[re.Pattern, Callable[[Feed, re.Match], Answer]], ...] = tuple((re.compile(address), page) for address, page in (
    ("", start),
    (r"/opensearch\.xml", opensearch),
    ("/search", search),
    ("/(best|popular)", ranked),
    ("/added", added),
    ("/new", bought),
    ("/(kind|game|genre|tag)", facets),
    ("/(src|kind|game|genre|tag)/(.+)", facet),
    ("/series", every_series),
    ("/series/([^/]+)", one_series),
    (r"/list/(\d+-[A-Za-z0-9_-]{1,40})", shared_list),
    ("/(continue|reading)", yours),
    ("/mine/([A-Za-z0-9_-]{1,40})", own_list),
))
SHELF = re.compile(r"/shelf/([A-Za-z0-9_-]{1,40})(?=/|$)")  # a shelf's own feed, the same tree with only what's on it


def answer(cat: Catalog, url: str, device: Device | None = None, library: str = "Library",
           asking: Asking | None = None) -> Answer:
    """(status, content type, body) for an /opds URL, or /ereader/opds with device; under /me too, the addresses they
    had when only those were your own, and links stay under whichever was asked. library: what the library calls
    itself; asking: who is asking, and what of theirs the page may show (Asking). A shelf asked for that isn't ticked
    for the apps is not found before the catalog is read; then the page asked for, by its address (PAGES)."""
    asking = asking or Asking()
    parts = urlsplit(url)
    path, qs = unquote(parts.path).rstrip("/"), parse_qs(parts.query)
    base = ("/me" if path.startswith("/me/") else "") + ("/ereader/opds" if device else "/opds")
    rest = path[len(base):] if path == base or path.startswith(base + "/") else None
    wanted = device.suits if device else something_to_offer
    m = SHELF.match(rest or "")
    shelf = next((s for s in asking.shelves if s["id"] == m[1]), None) if m else None
    if m and shelf is None:
        return NOT_FOUND
    if shelf:  # the same tree, inside the shelf: its own start and search, and none of the front page's lists
        base, rest = f"{base}/shelf/{shelf['id']}", rest[m.end():]
        wanted = (lambda first, rule: lambda it: first(it) and rule(it))(wanted, shelf["rule"])
    f = Feed(cat, url, path, qs, base, rest, device, library, asking, shelf, wanted)
    for pattern, page in PAGES if rest is not None else ():
        found = pattern.fullmatch(rest)
        if found:
            return page(f, found)
    return NOT_FOUND
