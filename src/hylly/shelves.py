"""Shelves: a saved part of the library, which is then the library for now: the filters and the search work inside it.

A shelf says which titles are on it: rows of a field and its values, to show only and never to show. The page decides what's on
one (shelfRule in catalog_page.html), as it has the whole catalog anyway; rule() here is the same, for a shelf ticked for the
reading apps, which then has a feed of its own (opds.py). A test (test_shelf_rule.py) keeps the two the same. It's tidiness,
not a lock: Everything is a tap away. The server keeps them, and checks them on the way in, as their names are other people's text.

Everyone's own are with what's kept about them (people.py), and those for everyone are here (Shared), in the library's database
with the tags and the library's other records.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import curation, db, rules

FIELDS = ("k", "src", "gm", "g", "tg", "dev", "a", "s", "p", "t", "rd", "ls", "kf", "miss")  # the page's SHELF_FIELDS
SORTS = ("rel", "title", "author", "new", "read", "list")
MAX_SHELVES = 50
NAME_MAX = 40
MAX_ROWS = 16
MAX_VALUES = 50
VALUE_MAX = 100
ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def limits() -> dict[str, int]:
    """What clean() lets through, for the library page (with /api/shelves): its Save checks a shelf by these before it
    sends it, rather than finding out from a refusal after the shelf showed as saved, and keeps no numbers of its own."""
    return {"shelves": MAX_SHELVES, "name": NAME_MAX, "rows": MAX_ROWS, "values": MAX_VALUES, "value": VALUE_MAX}


class NotAShelf(ValueError):
    """What came isn't a shelf; its text says why, for the person who sent it."""


class Full(Exception):
    """As many as can be kept."""


def line_of(text, what: str, most: int) -> str:
    if not isinstance(text, str) or CONTROL.search(text):
        raise NotAShelf(f"{what} is text on one line.")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > most:
        raise NotAShelf(f"{what} is 1 to {most} characters.")
    return text


def rows_of(raw) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_ROWS:
        raise NotAShelf(f"A shelf has at most {MAX_ROWS} rows of each sort.")
    rows = {}
    for field, values in raw.items():
        if field not in FIELDS:
            raise NotAShelf("A shelf can't go by that.")
        if not isinstance(values, list) or len(values) > MAX_VALUES:
            raise NotAShelf(f"A row has at most {MAX_VALUES} values.")
        kept = list(dict.fromkeys(line_of(v, "A value", VALUE_MAX) for v in values))
        if kept:
            rows[field] = kept
    return rows


def clean(raw) -> dict:
    """A shelf as it's kept, with nothing else in it, or NotAShelf."""
    if not isinstance(raw, dict):
        raise NotAShelf("A shelf, please.")
    shelf_id = raw.get("id")
    if not isinstance(shelf_id, str) or not ID.fullmatch(shelf_id):
        raise NotAShelf("Which shelf?")
    shelf = {"id": shelf_id, "name": line_of(raw.get("name"), "A shelf's name", NAME_MAX), "show": rows_of(raw.get("show")), "hide": rows_of(raw.get("hide"))}
    if raw.get("sort") in SORTS:
        shelf["sort"] = raw["sort"]
    if raw.get("group") is False:
        shelf["group"] = False
    if raw.get("apps") is True:  # in the reading apps: for everyone if it's for everyone, else in its owner's own feed
        shelf["apps"] = True
    return shelf


# ---- what's on a shelf: the page's shelfRule, values and missing, as they are there ----
TEXTUAL = {"a": lambda it: it.get("a") or [], "s": lambda it: [it["s"]] if it.get("s") else [],
           "p": lambda it: [it["p"]] if it.get("p") else [], "t": lambda it: [it.get("t") or ""]}  # by their words: a name may be part of one
MISSING = ("t", "a", "d", "c", "p", "y")  # the page's FACET.miss.labels, but all


def norm(text) -> str:
    """The page's norm: its words (words), one space apart."""
    return " ".join(words(text))


def words(text) -> list[str]:
    """The page's words: small letters without accents, letters and digits of any script (rules.words_of). Under the
    page's own names, as norm is, so that this part reads line for line as the page's shelfRule does
    (tests/test_shelf_rule.py holds the two to the same answers); the words themselves are the one rules.words_of."""
    return rules.words_of(text or "")


def missing(it: dict) -> list[str]:
    """The details a title lacks; a title from its file name counts unless you've corrected it; all: no title, author or description."""
    out = [k for k in MISSING if (bool(it.get("tf")) and "title" not in it.get("_fixed", ()) if k == "t" else not it.get(k))]
    return ["all", *out] if all(k in out for k in ("t", "a", "d")) else out


def values(it: dict, key: str, me: dict | None = None, in_ereader: Callable[[dict], bool] | None = None) -> list:
    """A title's values of a field, as the page's values() has them. me: {reading: {id: not, started or finished}, on_list: ids,
    lists_of: {id: [list ids]}} for someone; without, Reading and List have none, as in the page without a login."""
    if key == "rd":
        return [me["reading"].get(it["id"], "not")] if me is not None else []
    if key == "ls":
        return ((["on"] if it["id"] in me["on_list"] else []) + list(me["lists_of"].get(it["id"], []))) if me is not None else []
    if key in ("g", "gm", "tg"):
        return list(it.get(key) or [])
    if key == "src":
        return [it.get("src")] + [c[0] for c in it.get("cp", [])]
    if key == "kf":
        found = ("yes" if in_ereader(it) else "no") if it.get("kf") and in_ereader else it.get("kf")
        return [found] if found else []
    if key == "miss":
        return missing(it)
    return [it.get(key)]


def rule(shelf: dict, me: dict | None = None, in_ereader: Callable[[dict], bool] | None = None) -> Callable[[dict], bool]:
    """Whether a title is on the shelf: any value within a row, every row of Show only, and then none of Never show."""
    def rows(side) -> list[tuple]:
        return [(key, None, [w for w in (words(v) for v in vals) if w]) if key in TEXTUAL else (key, set(vals), None)
                for key, vals in (side or {}).items() if vals]

    def holds(it: dict, row: tuple) -> bool:
        key, chosen, wanted = row
        if chosen is not None:
            return any(v in chosen for v in values(it, key, me, in_ereader))
        have = [words(text) for text in TEXTUAL[key](it)]
        return any(all(x in h for x in w) for w in wanted for h in have)

    show, hide = rows(shelf.get("show")), rows(shelf.get("hide"))
    return lambda it: all(holds(it, r) for r in show) and not any(holds(it, r) for r in hide)


class Shared:
    """Those for everyone, in the database (shared_shelf) with the library's other records (curation.store): there are
    few, and they seldom change."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self) -> tuple[dict[str, dict], str]:
        """The shelves by id, in order, and the one the library opens with."""
        with curation.store(self.root).read() as c:
            return curation.shared_read(c)

    def set(self, shelf: dict, by: str) -> None:
        with curation.store(self.root).write() as c:
            found, _ = curation.shared_read(c)
            if shelf["id"] not in found and len(found) >= MAX_SHELVES:
                raise Full
            c.execute("INSERT INTO shared_shelf (id, body, made_by) VALUES (?, ?, ?) ON CONFLICT (id) DO UPDATE SET body = excluded.body",
                      (shelf["id"], db.dump(shelf), by))

    def delete(self, shelf_id: str) -> bool:
        with curation.store(self.root).write() as c:
            return c.execute("DELETE FROM shared_shelf WHERE id = ?", (shelf_id,)).rowcount > 0

    def opens_with(self, shelf_id: str) -> None:
        with curation.store(self.root).write() as c:
            c.execute("UPDATE library SET opens_with = ?", (shelf_id,))
