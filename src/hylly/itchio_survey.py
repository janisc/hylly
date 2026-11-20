"""itch.io's own steps before a download: what the account owns and what its bundles hold, counted before anything is
claimed or downloaded (hylly itchio survey), with its report and its list to sort, and the bundles' items claimed
(hylly itchio claim). The shop itself, its API session and its downloads, is itchio.py.

itch.io is mostly games and software, the bar for what is offered is low, and its charity bundles can hold so
much that their items never reach a library: each has to be claimed on the bundle's page. So the first
step only counts: every item's kind, its files and their sizes, and how much of it is books, comics and
tabletop games, against the space left in the library.

How the bundles' pages work (seen 2026-11-02):
- Bundles aren't in the API. my-purchases/bundles (logged in with the browser's itchio cookie) links each
  bundle as <a class="collection_title" href="/bundle/download/<key>">, sometimes with https://itch.io in front;
  itch.io writes a tag's attributes in any order, which changes from one page load to the next.
- A bundle's page lists 30 items a page (?page=N; the last page number is among the pager's links), each a
  <div class="game_row">: the game's link and title, short text, author, "N files", "Available for Windows"
  icons for programs, and either a claim form (csrf_token, game_id, a button value="claim") or, once
  claimed, a link to its download page (game_download_btn). The bundle's key and the download pages' keys
  are never logged or saved.
- None of this is documented, so it may change: the pages are asked for one every two seconds, retried, and never
  logged with their parameters.
"""

from __future__ import annotations

import csv
import html as htmllib
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from . import runs
from .exits import NOT_SET_UP, Exit
from .itchio import PAGES, UPLOADS, WANTED, Item, Itch, Lines, ask_each, ask_files, parse_game
from .logins import AuthError, ShopError
from .shops import SHOPS
from .shopsession import ShopSession
from .sites import COOKIE_PLACES
from .words import GB, MB, many, size_words, time_words

WEB_URL = "https://itch.io/"
COOKIE_REFUSED = ("itch.io did not accept the cookie: it sent the login page instead. Log in on itch.io, copy the "
                  f"value of the itchio cookie (under https://itch.io: {COOKIE_PLACES}) and run "
                  "'hylly login itchio-bundles' again.")

LABELS = {"book": "Books", "comic": "Comics", "tabletop": "Tabletop (RPGs, board and card games)",
          "other": "Other (zines and the like)", "game": "Games", "program": "Games and software with programs",
          None: "Tools, assets, mods and soundtracks"}
ORDER = (*WANTED, "other", "game", "program", None)

# Its working files, in itch.io's folder in the library's state folder (shops.Shop.state), as paths in the state folder,
# beside those the downloads read too (itchio.UPLOADS, itchio.PAGES):
GAMES = f"{SHOPS['itchio'].state}/games.jsonl"  # what itch.io said of each item not claimed: its kind
CLAIMS = f"{SHOPS['itchio'].state}/claims.jsonl"  # each claim, as it was made
SURVEY = f"{SHOPS['itchio'].state}/survey.txt"  # the survey's report (hylly itchio survey), and every item in a CSV
SURVEY_CSV = f"{SHOPS['itchio'].state}/survey.csv"


@dataclass
class Bundle:
    name: str
    link: str  # /bundle/download/<key>: in memory only, never logged or saved
    items: int = 0
    claimed: int = 0
    gone: int = 0  # on its page without a claim button: deleted, or no longer offered


# ---- the bundles' pages ---------------------------------------------------------------------

# itch.io writes a tag's attributes in any order, from one page load to the next: each tag's attributes are read apart.
BUNDLE_LINK = re.compile(r'<a\b([^>]*\bclass="collection_title"[^>]*)>\s*<span[^>]*collection_title_wrap[^>]*>(.*?)</span>', re.S)
BUNDLE_HREF = re.compile(r'(?:^|\s)href="(?:https://itch\.io)?(/bundle/download/[A-Za-z0-9_-]+)"')


def _attr(tag: str, name: str) -> str:
    found = re.search(rf'(?:^|\s){name}="([^"]*)"', tag)
    return htmllib.unescape(found[1]) if found else ""


def _text(fragment: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse_bundles(page: str) -> list[Bundle]:
    seen: dict[str, Bundle] = {}
    for attrs, name in BUNDLE_LINK.findall(page):
        link = BUNDLE_HREF.search(attrs)
        if link:
            seen.setdefault(link[1], Bundle(name=_text(name), link=link[1]))
    return list(seen.values())


def last_page(page: str) -> int:
    return max([int(n) for n in re.findall(r"[?&]page=(\d+)", page)] or [1])


def parse_rows(page: str, bundle: str) -> list[Item]:
    """A bundle page's items. A claimed one has no game id on the page, and its link goes to its download page,
    whose key is cut off."""
    items = []
    for row in page.split('<div class="game_row">')[1:]:
        title = re.search(r'<h2[^>]*\bgame_title\b[^>]*>\s*<a\b([^>]*)>(.*?)</a>', row, re.S)
        if not title:
            continue
        game_id = next((_attr(tag, "value") for tag in re.findall(r'<input\b[^>]*\bname="game_id"[^>]*>', row)), "")
        files = re.search(r'file_count">\s*(\d+) file', row)
        creator = re.search(r'game_author">\s*by <a[^>]*>(.*?)</a>', row, re.S)
        short = re.search(r'game_short_text">(.*?)</div>', row, re.S)
        claimed = "game_download_btn" in row
        items.append(Item(
            game_id=int(game_id) if game_id.isdigit() else 0, title=_text(title[2]) or "Untitled",
            creator=_text(creator[1]) if creator else "Unknown", url=_attr(title[1], "href").split("/download/")[0],
            short_text=_text(short[1]) if short else "", key_id=-1 if claimed else 0, bundles=[bundle],
            listed_files=int(files[1]) if files else 0, platforms=re.findall(r'title="Available for ([^"]+)"', row)))
    return items


def _to_login(r: httpx.Response) -> bool:
    """itch.io sending a page's answer on to its login page: the cookie wasn't taken."""
    return r.is_redirect and "login" in r.headers.get("location", "")


class Web:
    """itch.io's own pages, logged in with the browser's cookie: the bundles, which the API doesn't have."""

    def __init__(self, cookie: str, transport: httpx.BaseTransport | None = None, delay: float = 2.0,
                 backoff: float = 2.0, sleep=None):
        self.session = ShopSession("itch.io", WEB_URL, pace=delay, backoff=backoff, transport=transport, sleep=sleep,
                                   cookies={"itchio": cookie}, headers={"Accept": "text/html"},
                                   refused_when=_to_login, refused=COOKIE_REFUSED)
        self.http = self.session.http

    def __enter__(self) -> Web:
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    def page(self, path: str, params: dict | None = None, what: str = "a page") -> str:
        r = self.session.request("GET", path, params=params)
        if r.status_code != 200:
            raise ShopError(f"itch.io answered HTTP {r.status_code} for {what}.", r.status_code)
        return r.text

    def bundles(self) -> list[Bundle]:
        page = self.page("my-purchases/bundles", what="your bundles page")
        found = parse_bundles(page)
        if not found and "my-purchases" not in page:
            raise AuthError(COOKIE_REFUSED)
        return found

    def offered(self, path: str, params: dict | None, bundle: str, page: int) -> tuple[str, set[int]]:
        """A bundle's page as it is now: its own token, which its forms send back (one for all of them), and the items
        it still offers to claim."""
        html = self.page(path, params, what=f"page {page} of {bundle!r}")
        return csrf_token(html), {it.game_id for it in parse_rows(html, bundle) if it.game_id and not it.claimed}

    def claim(self, path: str, params: dict | None, token: str, game_id: int) -> tuple[bool, str]:
        """Presses an item's Download on its bundle's page: the form posts to the page itself, and itch.io answers a
        claim by sending the browser on to the item's download page. (True, "") then; else (False, what it said)."""
        referer = WEB_URL + path + (f"?page={params['page']}" if params else "")
        try:  # in its turn, and asked once, never again: a claim changes the account
            r = self.session.send("POST", path, params=params, data={"csrf_token": token, "game_id": str(game_id), "action": "claim"},
                                  headers={"Origin": WEB_URL.rstrip("/"), "Referer": referer})
        except httpx.TransportError as e:
            return False, f"no answer ({type(e).__name__})"
        where = r.headers.get("location", "")
        if _to_login(r):
            raise AuthError(COOKIE_REFUSED)
        if r.is_redirect and "/download/" in where:
            return True, ""
        return False, f"HTTP {r.status_code}" + (f", on to {re.sub(r'/download/.*', '/download/…', where)}" if where else "")


def csrf_token(page: str) -> str:
    """The page's own token, which its forms send back (one for all of them)."""
    return next((_attr(tag, "value") for tag in re.findall(r'<input\b[^>]*\bname="csrf_token"[^>]*>', page)), "")


def check_cookie(cookie: str, transport: httpx.BaseTransport | None = None) -> tuple[int, int]:
    """For 'hylly login itchio-bundles': the bundles page's size and how many bundles it lists, asked once."""
    with Web(cookie, transport) as web:
        r = web.session.send("GET", "my-purchases/bundles")
    if _to_login(r):
        raise AuthError(COOKIE_REFUSED)
    if r.status_code != 200:  # itch.io's own trouble, which says nothing about the cookie, as Web.page has it
        raise ShopError(f"itch.io answered HTTP {r.status_code} for your bundles page; try again later.", r.status_code)
    return len(r.content), len(parse_bundles(r.text))


# ---- the survey -----------------------------------------------------------------------------

def survey(itch: Itch, state: Path, web: Web | None = None, *, again: bool = False, log=print,
           progress_every: float = 60.0) -> tuple[list[Item], list[Bundle]]:
    """The library and, with web, the bundles: every item once, with its kind and, where itch.io shows them,
    its files. Software and items with programs aren't looked into; answers from earlier runs are reused."""
    log("Your itch.io library:")
    owned = itch.owned(progress=runs.counting("items"))
    log("")  # the end of the counting's line
    log(f"  {many(len(owned), 'item')}.")
    files = Lines(state / UPLOADS, "game")
    ask_files(itch, [it for it in owned if it.group is not None], files, again=again, log=log, progress_every=progress_every)
    if web is None:
        return owned, []

    log("Your bundles:")
    bundles = web.bundles()
    pages = Lines(state / PAGES, "bundle,page")
    by_id = {it.game_id: it for it in owned}
    by_url = {it.url.rstrip("/").lower(): it for it in owned if it.url}
    unclaimed: dict[int, Item] = {}
    for b in bundles:
        page, last, seen = 1, 1, 0
        while page <= last:
            row = None if again else pages.rows.get((b.name, page))
            if row is None:
                html = web.page(b.link.lstrip("/"), {"page": page} if page > 1 else None, what=f"page {page} of {b.name!r}")
                row = {"bundle": b.name, "page": page, "last": last_page(html), "items": [asdict(it) for it in parse_rows(html, b.name)]}
                pages.put(row)
            last = max(last, row["last"])
            rows = [Item(**{**it, "uploads": None}) for it in row["items"]]
            if not rows:
                break
            for it in rows:
                seen += 1
                mine = by_id.get(it.game_id) or by_url.get(it.url.rstrip("/").lower())
                if mine or it.key_id:  # claimed: it's in the library
                    b.claimed += 1
                    if mine and b.name not in mine.bundles:
                        mine.bundles.append(b.name)
                    continue
                if not it.game_id:  # no claim button: deleted, or no longer offered
                    b.gone += 1
                    continue
                known = unclaimed.setdefault(it.game_id, it)
                if b.name not in known.bundles:
                    known.bundles.append(b.name)
            page += 1
        b.items = seen
        log(f"  {b.name}: {seen} items on {last} pages, {b.claimed} claimed" + (f", {b.gone} no longer offered." if b.gone else "."))

    games = Lines(state / GAMES, "id")
    ask = [it for it in unclaimed.values() if not it.platforms
           and (again or it.game_id not in games.rows or games.rows[it.game_id].get("again"))]
    if ask:
        log(f"  Asking what kind {len(ask)} of the items without programs are, one a second: {time_words(len(ask), about=True)}.")
    ask_each(ask, lambda it: games.put({"id": it.game_id, "game": itch.game(it.game_id)}),
             # itch.io won't describe this one (hidden or taken down, say): its kind stays unknown; busy: asked again
             lambda it, e: games.put({"id": it.game_id, "game": {}, "error": str(e), **({"again": True} if e.passing else {})}),
             log=log, progress_every=progress_every)
    for it in unclaimed.values():
        game = (games.rows.get(it.game_id) or {}).get("game")
        if game and not it.platforms:
            about = parse_game(game)
            it.classification, it.price, it.cover, it.published = about.classification, about.price, about.cover, about.published
            it.short_text = it.short_text or about.short_text
    free = [it for it in unclaimed.values() if it.group in (*WANTED, "other") and it.classification and it.price == 0]
    ask_files(itch, free, files, again=again, log=log, progress_every=progress_every)
    return owned + list(unclaimed.values()), bundles


# ---- claiming -------------------------------------------------------------------------------
# This claims only the kinds asked for, one at a time at a person's pace (every 20 seconds unless told otherwise), so a
# big bundle asks no more of itch.io's servers than a person pressing the buttons would. A claim itch.io doesn't take
# is left for the next run, after a longer pause; several in a row stop it.

CLAIMABLE = (*WANTED, "other")  # what hylly itchio claim takes unless told --all
GONE = "not offered on its page any more"
STRIKES = 3  # claims not taken in a row before claiming stops: then it's the site or the login, not the item
PAUSE = 5  # after a claim that wasn't taken, this many times the usual wait
GIVE_UP_AFTER = 3  # runs in which itch.io didn't take an item before it's left for good (listed, not tried again)


def claim_targets(state: Path, groups: tuple[str | None, ...] | None) -> tuple[dict[int, dict], dict[int, bool]]:
    """What a survey found to claim: each unclaimed item of the groups asked for (None: all of them) once, with the
    bundle and page it's on. Only what the survey classified: an item with files for a system by its bundle's page
    (a program), the rest by what itch.io said it is. A claim is hard to take back, so an item whose kind isn't known
    is left whatever the groups; the second dict holds those, True for the ones a survey still asks about (never
    asked, as when one stopped part-way, or itch.io busy when asked), False for the ones itch.io wouldn't describe."""
    pages = Lines(state / PAGES, "bundle,page")
    games = Lines(state / GAMES, "id")
    if not pages.rows:
        raise Exit(NOT_SET_UP, "No survey of the bundles yet: run hylly itchio survey first.")
    found: dict[int, dict] = {}
    unknown: dict[int, bool] = {}
    for (bundle, page), row in sorted(pages.rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        for raw in row["items"]:
            it = Item(**{**raw, "uploads": None})
            if it.claimed or not it.game_id or it.game_id in found:
                continue
            if not it.platforms:  # a program is never asked about: its bundle's page says what it is
                answer = games.rows.get(it.game_id) or {}
                kind = str((answer.get("game") or {}).get("classification") or "")
                if not kind or answer.get("again") or answer.get("error"):
                    unknown[it.game_id] = not answer or bool(answer.get("again"))
                    continue
                it.classification = kind
            if groups is None or it.group in groups:
                found[it.game_id] = {"bundle": bundle, "page": page, "title": it.title, "group": it.group}
    return found, unknown


def claim(itch: Itch, web: Web, state: Path, *, groups: tuple[str | None, ...] | None = CLAIMABLE, every: float = 20.0,
          limit: int | None = None, dry_run: bool = False, ask=None, log=print, sleep=time.sleep,
          now=time.monotonic) -> int:
    """Claims the survey's unclaimed items of the groups asked for, page by page; returns how many it claimed. Only the
    items it classified: it says how many it leaves for not knowing their kind. ask, given
    how many there are once they've been told: whether to go ahead, as this changes the account. Each
    claim is written to state/itchio/claims.jsonl (CLAIMS) as it's made, so a stopped run goes on where it was. One
    that itch.io didn't take in an earlier run is tried again after all the others, and left for good after GIVE_UP_AFTER runs;
    those repeats don't count towards STRIKES, which is for the site or the login failing, not single items."""
    owned = {it.game_id for it in itch.owned()}
    claims = Lines(state / CLAIMS, "game")
    targets, unknown = claim_targets(state, groups)
    todo = {gid: t for gid, t in targets.items() if gid not in owned and not (claims.rows.get(gid) or {}).get("ok")}
    unknown = {gid: asks for gid, asks in unknown.items() if gid not in owned and not (claims.rows.get(gid) or {}).get("ok")}

    def refusals(gid: int) -> int:
        row = claims.rows.get(gid) or {}
        return 0 if not row or row.get("ok") or row.get("why") == GONE else int(row.get("refused", 1))

    left = [todo[gid]["title"] for gid in todo if refusals(gid) >= GIVE_UP_AFTER]
    again = {gid for gid in todo if 0 < refusals(gid) < GIVE_UP_AFTER}
    todo = {gid: todo[gid] for gid in [*(g for g in todo if not refusals(g)), *(g for g in todo if g in again)]}
    if limit is not None:  # after the order: earlier refusals don't take up the places of new items
        todo = dict(list(todo.items())[:limit])
    if left:
        log(f"  Left for good: {many(len(left), 'item')} itch.io didn't take in {GIVE_UP_AFTER} runs "
            f"({', '.join(map(repr, left[:5]))}{', …' if len(left) > 5 else ''}); they are in state/{CLAIMS}.")
    waiting = sum(unknown.values())
    if waiting:
        log(f"  Not claimed until a survey has finished (hylly itchio survey): {many(waiting, 'item')} "
            "whose kind it hasn't learnt yet, as it stopped part-way or itch.io was busy.")
    if len(unknown) > waiting:
        n = len(unknown) - waiting
        log(f"  Not claimed: {many(n, 'item')} whose kind itch.io wouldn't tell (hidden or taken down, say). "
            "hylly itchio survey --again asks about every item once more.")
    by_bundle: dict[str, int] = {}
    for t in todo.values():
        by_bundle[t["bundle"]] = by_bundle.get(t["bundle"], 0) + 1
    log(f"To claim: {many(len(todo), 'item')}, one every {every:g} seconds: {time_words(len(todo) * every, about=True)}."
        + "".join(f"\n  {name}: {n}" for name, n in by_bundle.items()))
    if dry_run or not todo or (ask and not ask(len(todo))):
        return 0
    links = {b.name: b for b in web.bundles()}
    done, refused, strikes, last, reported = 0, [], 0, None, now()
    for ids_now in ([gid for gid in todo if gid not in again], [gid for gid in todo if gid in again]):
        pages: dict[tuple[str, int], list[int]] = {}
        for gid in ids_now:
            pages.setdefault((todo[gid]["bundle"], todo[gid]["page"]), []).append(gid)
        for (bundle, page), ids in sorted(pages.items()):
            if bundle not in links:
                log(f"  {bundle}: not among your bundles any more; its items are left.")
                continue
            path, params = links[bundle].link.lstrip("/"), ({"page": page} if page > 1 else None)
            token, offered = web.offered(path, params, bundle, page)
            for gid in ids:
                title = todo[gid]["title"]
                if gid not in offered:  # claimed meanwhile, or no longer on this page: a new survey sees where it went
                    claims.put({"game": gid, "bundle": bundle, "ok": False, "why": GONE})
                    continue
                if last is not None and last + every > now():  # at a person's pace
                    sleep(last + every - now())
                ok, why = web.claim(path, params, token, gid)
                last = now()
                claims.put({"game": gid, "bundle": bundle, "title": title, "ok": ok,
                            **({} if ok else {"why": why, "refused": refusals(gid) + 1})})
                if ok:
                    done, strikes = done + 1, 0
                    if now() - reported >= 300:
                        reported = now()
                        log(f"  {done}/{len(todo)} claimed; {time_words((len(todo) - done - len(refused)) * every, about=True)} to go.")
                    continue
                strikes += gid not in again  # one refused before is that item's trouble, not the site's
                refused.append(title)
                if strikes >= STRIKES:
                    raise RuntimeError(f"itch.io didn't take {STRIKES} claims in a row, the last of them {title!r} ({why}), "
                                       f"so claiming stopped. {done} were claimed in this run.")
                log(f"  itch.io didn't take the claim of {title!r} ({why}); going on after a pause. "
                    f"Running it again tries it once more.")
                sleep(every * PAUSE)
                token, offered = web.offered(path, params, bundle, page)  # a fresh token, and what is still offered
                last = now()
    log(f"  {done} claimed." + (f" itch.io didn't take {len(refused)}: {', '.join(map(repr, refused[:5]))}"
                                f"{', …' if len(refused) > 5 else ''}. Running it again tries them once more." if refused else ""))
    return done


# ---- the report -----------------------------------------------------------------------------

def _gb(n: float) -> str:
    """A size in the report's columns of GB (words.GB), to two decimals: the unit is the column's."""
    return f"{n / GB:,.2f}"


def estimates(items: list[Item]) -> dict[str | None, int]:
    """A typical file's size for each group: the median of the files whose size is known (else of all of them)."""
    sizes: dict[str | None, list[int]] = {}
    for it in items:
        sizes.setdefault(it.group, []).extend(u.size for u in it.files() if u.size)
    everything = [s for group in WANTED + ("other",) for s in sizes.get(group, [])]
    fallback = int(statistics.median(everything)) if everything else 0
    return {group: int(statistics.median(sizes[group])) if sizes.get(group) else fallback for group in ORDER}


def guess(item: Item, typical: dict[str | None, int]) -> int:
    """An item's size if its files are known, else its number of files times its group's typical file."""
    return item.size() if item.uploads is not None else item.listed_files * typical.get(item.group, 0)


def report(items: list[Item], free: int, bundles: list[Bundle] | None = None, when: datetime | None = None) -> str:
    """The survey's summary: per group, items, claimed, files and GB (known, and estimated for the rest);
    the bundles; file types; the biggest; the space left."""
    when = when or datetime.now()
    typical = estimates(items)
    lines = [f"itch.io survey, {when:%Y-%m-%d %H:%M}. Nothing was claimed or downloaded.", ""]
    if bundles:
        lines.append(f"Your bundles: {len(bundles)}")
        lines += [f"  {b.name:50} {b.items:>6,} items, {b.claimed:>4} claimed" + (f", {b.gone} no longer offered" if b.gone else "")
                  for b in bundles]
        lines.append("")
    claimed = sum(1 for it in items if it.claimed)
    lines += [f"Every item once: {len(items):,} ({claimed} in your library, {len(items) - claimed:,} still to claim in bundles)", "",
              f"  {'':42} {'items':>6} {'claimed':>8} {'files':>6} {'GB known':>9} {'size unknown':>13} {'GB guessed':>11}"]
    by_group: dict[str | None, list[Item]] = {}
    for it in items:
        by_group.setdefault(it.group, []).append(it)
    for group in ORDER:
        rows = by_group.get(group, [])
        if not rows:
            continue
        mine = sum(1 for it in rows if it.claimed)
        if group in ("program", None):
            lines.append(f"  {LABELS[group]:42} {len(rows):>6,} {mine:>8,}   (not looked into)")
            continue
        counted = rows if group != "game" else [it for it in rows if it.only_reading()]
        known = [it for it in counted if it.uploads is not None]
        unknown = [it for it in rows if it.uploads is None and (group != "game")]
        lines.append(f"  {LABELS[group]:42} {len(rows):>6,} {mine:>8,} {sum(len(it.files()) for it in known):>6,} "
                     f"{_gb(sum(it.size() for it in known)):>9} {len(unknown):>13,} {_gb(sum(guess(it, typical) for it in unknown)):>11}")
        if group == "game":
            lines.append(f"  {'':42} (files counted only for the {many(len(counted), 'game')} whose files are all books or comics:"
                         " rulebooks and the like)")
    if any(it.uploads is None and it.group in WANTED + ("other",) for it in items):
        lines.append(f"  Guessed: a paid item's files show only once it's claimed, so its size is its number of files times a "
                     f"typical file of its kind (books {size_words(typical['book'])}, comics {size_words(typical['comic'])}, "
                     f"tabletop {size_words(typical['tabletop'])}, other {size_words(typical['other'])}).")

    wanted = [it for it in items if it.group in WANTED]
    other = by_group.get("other", [])
    reading_games = [it for it in by_group.get("game", []) if it.only_reading()]
    total = sum(guess(it, typical) for it in wanted)
    more = sum(guess(it, typical) for it in other) + sum(it.size() for it in reading_games)
    lines += ["", f"Books, comics and tabletop together: {many(len(wanted), 'item')}, about {size_words(total)}.",
              f"With 'other' and the games whose files are all books: about {size_words(total + more)}."]
    undescribed = [it for it in other if not it.classification and not it.claimed]
    if undescribed:
        lines.append(f"Items itch.io wouldn't describe (hidden or taken down, say), counted under other: {len(undescribed)}.")
    elsewhere = [it for it in wanted + other if any(u.elsewhere for u in it.uploads or [])]
    empty = [it for it in wanted + other if it.uploads is not None and not it.files() and it not in elsewhere]
    if elsewhere:
        lines.append(f"Items with files only linked elsewhere (Google Drive and the like; can't be fetched): {len(elsewhere)}.")
    if empty:
        lines.append(f"Items with nothing to fetch (no files, or only programs): {len(empty)}.")

    types: Counter = Counter()
    sizes: Counter = Counter()
    for it in wanted + other + reading_games:
        for u in it.files():
            types[u.ext or "(none)"] += 1
            sizes[u.ext or "(none)"] += u.size
    if types:
        lines += ["", "File types, where the files are known:"]
        lines += [f"  {ext.upper():8} {types[ext]:>6,} files {_gb(size):>9} GB" for ext, size in sizes.most_common()]
    biggest = sorted((it for it in wanted + other + reading_games if it.size()), key=lambda it: -it.size())[:20]
    if biggest:
        lines += ["", "The biggest, where the files are known:"]
        lines += [f"  {it.size() / MB:>9,.0f} MB  {LABELS[it.group].split(' (')[0]:9} {it.title} ({it.creator})" for it in biggest]
    lines += ["", f"Free in the library: {size_words(free)}. After all books, comics and tabletop: about {size_words(free - total)}."]
    return "\n".join(lines) + "\n"


def write_csv(items: list[Item], path: Path) -> Path:
    """Every item but the software and the programs, to sort and filter in a spreadsheet. Its words are strangers' (a
    charity bundle holds a thousand creators' items): a cell that would start a formula gets a ' before it."""
    from .shoplist import no_formula

    typical = estimates(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["group", "title", "creator", "claimed", "bundles", "free", "files", "MB", "MB is", "file types",
                    "linked elsewhere", "only books or comics", "published", "url", "description"])
        rows = [it for it in items if it.group not in ("program", None)]
        for it in sorted(rows, key=lambda it: (ORDER.index(it.group), it.title.lower())):
            files = it.files()
            row = [it.group, it.title, it.creator, "yes" if it.claimed else "", "; ".join(it.bundles),
                   "yes" if it.price == 0 and it.classification else "",
                   len(files) if it.uploads is not None else it.listed_files,
                   round(guess(it, typical) / MB, 1), "known" if it.uploads is not None else "estimate",
                   " ".join(sorted({u.ext.upper() for u in files if u.ext})),
                   sum(1 for u in it.uploads or [] if u.elsewhere), "yes" if it.only_reading() else "",
                   it.published, it.url, it.short_text]
            w.writerow([no_formula(c) if isinstance(c, str) else c for c in row])  # the numbers stay numbers
    return path
