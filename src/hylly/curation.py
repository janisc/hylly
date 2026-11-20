"""Your own changes to the catalog, kept apart from everything generated: in the database (db.py, the tables edit and
edit_title). The database that keeps them is the one on the computer the library is on (db.own).

One change, one edit, only ever added: when (db.now: an edit from before has the local time alone), who, what, and
which titles.
  {"at": "2026-10-28T12:00:00+02:00", "by": "reader", "field": "tag", "op": "add", "value": "Knitting",
   "titles": [{"id": "h:...", "t": "Title", "f": "humble/.../file.epub"}]}
Who: a login's name; "this PC" in edits from before every request logged in; "command line" for hylly tag and
hylly set (COMMAND_LINE); "hylly catalog" for a reattach; "the Inbox" for the tags and kinds an Inbox's folders gave
the books it took in (INBOX). A login's name has no spaces, so it is never one of these.
Catalog runs never change them, so a rebuild can't lose a change; the current state is the edits
replayed in order. A title is named by its catalog id, with its title and file kept beside it, so
that a title whose id changed (a folder title moved or renamed) can be found again: hylly catalog then
adds a "reattach" edit. Edits of an id no title has any more are kept, and apply again if it returns.
A merged title is read by all its ids (its own and its copies'), so an edit that takes something away from one (a
tag removed, a no taken back, a correction cleared) also names its copies' ids, as "copies": {"h:...": ["f:..."]},
and takes it from those too; what an edit gives goes on the title's own id, as it always has.

Tags: add and remove; decline, a "no" to a suggested tag, remembered so it isn't suggested again;
and undecline, which asks again. Adding a tag always overrides an earlier no. Tags compare without
regard to case and show as first written. The library server lays them, and the corrections below,
over the catalog it sends the library page and the feeds (library.Catalog), afresh after every edit:
the page shows what the database has, with nothing written in between to fall behind.

One-value corrections: field "kind" (one of the kinds in the rules: kinds.toml's [kinds.X]), "ereader"
(always or never in the e-reader feed) or "format" (the file to keep of a Humble title, used by the download plan),
op "set" with a value, or "clear" for automatic again. Each edit also keeps what each title had before ("was"),
so a change can be undone exactly. A kind steers the downloads too (kinds): a title of any shop not downloaded yet
is downloaded as that kind (the format its kind's list keeps, where it is read), found by its catalog id, which is
its shop's id for it after the shop's letter (h:, s:, d:, i:; for a title in several places, the one leading).

What a title shows, the same way: "title", "authors" (a list), "series", "number", "year" and "publisher",
any text (TEXT); an empty value means none (no series, say), "clear" what the catalog found again. They
change what the library page, the search and the reading apps' feeds show; the files are never touched.

Suggestions: tags.toml in the library's curation folder lists words that suggest each tag (a tag's own name
always counts). hylly tag suggest, or Find suggestions on the library page, keeps the queue in the database (the
table suggestion), leaving out every title and tag already decided: added, removed or declined.

Questions: what a download's or the catalog's rules can't settle by themselves (QUESTIONS), kept in the database
as each run or build finds them (the table question: keep_questions; the catalog's under the shop CATALOG), shown by
hylly decide, the runs and builds, hylly doctor and the server page, and answered with an ordinary edit, one of the
corrections above (ANSWERED): "file", the file to keep of a title by its name as its shop lists it; "answers", what no
other correction says of a download (keep the guess, I moved it); "catalog", the catalog's in a field of their own (the
same book or two, a lookup's match used or refused, a folder's files of a suffix taken as books or not), so that an
older Hylly reads and writes the downloads' as it always did; or a reattach of a title's tags and corrections to the
title you say it became, or forgetting them (the ops reattach and forget). So an answer replays, follows a title's new
id as any edit does, and the question is settled by the edit, not by its row; its row stays on the answered list while
the edit stands and its subject is there, and hylly decide NUMBER auto takes that one answer back (take_back: exactly
what was there before it). An edit of answers is a whole title's (op set), or a few keys of several titles' at once
(op update: the same book or two names both; its undo is one too, key by key: undo_update). A quiet kind (a lookup's
near match) is found and kept as any other, but listed only when asked for by its kind. Tag suggestions stay a queue
of their own; they could be one kind of question more, with a tag as an option, answered by a tag edit as now.

Files left out (LEFT, "left-out"): a shop list's rows the rules tick that you unticked (hylly SHOP download LIST,
shoplist.py), each by its file's key in the title's entries, an edit of a few of them at once (leave_out: op update),
in a field of its own as the catalog's answers are, so that an older Hylly never drops them. No download fetches such a
file (left_out: shopping.select's yours); a list that ticks it again, hylly set left-out auto, or an undo takes it back.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .exits import NEEDS_YOU, NOT_SET_UP, USAGE, Exit
from . import db, paths
from .words import many
from .matching import Titles, folder_id, ids_of
from .rules import words_of

TAG_MAX = 40
OPS = ("add", "remove", "decline", "undecline")
SINGLE = ("kind", "ereader", "format")  # one value each, or automatic
# What answering a question makes (questions, below), one value each too, or none again: "file", the file to keep of a
# title, by its name as its shop lists it (Humble's plan keeps it, over a format); "answers", the answers to a title's
# download questions that no other correction says, by the question ({"moved:guide.pdf": {"a": "moved", "o": ...}});
# and "catalog", the answers to the catalog's questions, in a field of their own ({"same:f:a|h:b": {"a": "apart"}}),
# so that a Hylly from before them reads the downloads' answers as it always did, and its writes of those never drop
# these. And, made by no question but kept the same way, "left-out": the files a shop list left out (hylly SHOP
# download LIST: a row the rules tick that the list unticks), in a field of its own for the same reason, each by its
# file's key ({"out:2": {"a": "out", "f": "maps.zip"}}), which a download then never fetches (left_out, leave_out)
LEFT = "left-out"
ENTRIES_MOST = 400  # a title's entries one edit changes at most (answer_value takes up to 500): more go in more edits
ANSWERED = ("file", "answers", "catalog", LEFT)
ENTRIES = ("answers", "catalog", LEFT)  # those whose value is a title's entries, each by its key: a few changed at once
COMMAND_LINE = "command line"  # who made a change with hylly tag or hylly set: not the computer's account name
INBOX = "the Inbox"  # who gave a book the tag or the kind its folder in the Inbox named (cli._give): no login's name


def single(field: str, recorded: bool = False) -> tuple[str, ...]:
    """The values a one-value correction may have: the kinds in the rules (kinds.toml), always or never, or a format
    marked keep in the rules (formats.toml). recorded: a choice made before (an edit), which stands while the rules
    know its format (or its kind), even with keep off for it now: keep says what can be chosen."""
    from . import rules

    if field == "kind":
        return tuple(rules.current().kinds)
    if field == "ereader":
        return ("always", "never")
    if field == "format":
        r = rules.current()
        return tuple(r.formats) if recorded else tuple(r.keep_formats)
    return ()
TEXT = {"title": ("t", 300), "authors": ("a", 120), "series": ("s", 200), "number": ("n", 20),  # field: (record key, longest)
        "year": ("y", 4), "publisher": ("p", 200)}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def text_value(field: str, value) -> str | list[str]:
    """A correction of what a title shows, tidied and checked. Authors are a list: given as one, or as text with
    the names separated by ";" or new lines (a name may contain a comma: "Brennan, Jo")."""
    if field == "authors":
        names = value if isinstance(value, list) else re.split(r"\s*[;\n]\s*", str(value))
        names = [re.sub(r"\s+", " ", str(n)).strip() for n in names]
        names = [n for n in names if n]
        if len(names) > 30 or any(len(n) > TEXT[field][1] or CONTROL.search(n) for n in names):
            raise Exit(USAGE, f"Authors: up to 30 names of up to {TEXT[field][1]} characters each.")
        return names
    if isinstance(value, list):
        raise Exit(USAGE, f"The {field} is one piece of text.")
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > TEXT[field][1] or CONTROL.search(str(value).replace("\n", " ")):
        raise Exit(USAGE, f"The {field} is up to {TEXT[field][1]} characters.")
    if field == "title" and not text:
        raise Exit(USAGE, "A title can't be empty; use auto for the one the catalog found.")
    if field == "year" and text and not re.fullmatch(r"1[0-9]{3}|20[0-9]{2}", text):
        raise Exit(USAGE, "A year is four digits, such as 2017.")
    return text


FILE_NAME_MOST = 300  # a file's name as a shop lists it, at most
# An answer's codes, by the field that keeps them: the downloads' (MOVED, LEAVE_OUT, keep), and the catalog's (two copies
# the same book or apart, a lookup's match used or refused, waiting kept, a folder's files of a suffix books or not)
CODES = {"answers": ("again", "moved", "keep"), "catalog": ("same", "apart", "use", "refused", "keep", "not", "books"),
         LEFT: ("out",)}
MARK_MOST = 64  # what an answer keeps of what was offered (o) or matched (m), in letters, at most


def _a_name(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= FILE_NAME_MOST and not CONTROL.search(value)


def _an_answer(k, v, field: str = "answers") -> bool:
    """One of a title's answers as they are kept: by its question's key (its kind, a colon, its subject), an answer's code,
    and what the question offered (o) or the match it took (m), a file's name (f: a file kept as the rules took it), and
    for two copies the first file of each by its id (w: its name and size), each within bounds."""
    def mark(x) -> bool:
        return x is None or (isinstance(x, str) and len(x) <= MARK_MOST)

    sides = v.get("w") if isinstance(v, dict) else None
    return (isinstance(k, str) and ":" in k and len(k) <= FILE_NAME_MOST + 40 and isinstance(v, dict)
            and v.get("a") in CODES[field]
            and mark(v.get("o")) and mark(v.get("m")) and ("f" not in v or _a_name(v["f"]))
            and (sides is None or (isinstance(sides, dict) and len(sides) <= 2 and all(
                isinstance(i, str) and len(i) <= FILE_NAME_MOST and isinstance(w, list) and len(w) == 2
                and isinstance(w[0], str) and len(w[0]) <= FILE_NAME_MOST and isinstance(w[1], int)
                and not isinstance(w[1], bool) for i, w in sides.items()))))


def _tidy_answer(v: dict) -> dict:
    return {key: v[key] for key in ("a", "o", "m", "f", "w") if v.get(key) is not None}


def answer_value(field: str, value, update: bool = False):
    """One of the corrections an answer makes (ANSWERED), checked: a file's name, on one line; or a title's answers (the
    downloads', answers, or the catalog's, catalog), each by its question (its kind, a colon, its subject) an answer's
    code (CODES: its field's own) with what the question offered (o: fingerprint) or the match it took (m), for a file
    kept as the rules took it its name (f), and for two copies their first files (w). update: a few of them, to change
    on each title of the edit (op update), None for one taken away."""
    if field == "file":
        if not _a_name(value):
            raise Exit(USAGE, f"The file is its name as the shop lists it, on one line, up to {FILE_NAME_MOST} characters.")
        return value
    if (not isinstance(value, dict) or len(value) > 500
            or not all((update and v is None and isinstance(k, str) and ":" in k and len(k) <= FILE_NAME_MOST + 40)
                       or _an_answer(k, v, field) for k, v in value.items())):
        raise Exit(USAGE, "A title's answers are hylly decide's to make: hylly set answers auto takes them back.")
    return {k: None if v is None else _tidy_answer(v) for k, v in value.items()}


def folder(root: Path) -> Path:
    return root / "curation"


EDIT = db.Shape("edit", (("at", "at", "text"), ("by", "who", "text"), ("field", "field", "text"), ("op", "op", "text"),
                         ("value", "value", "json"), ("was", "was", "json"), ("ids", "ids", "json")))
EDIT_TITLE = db.Shape("edit_title", (("id", "title", "text"), ("t", "title_t", "text"), ("f", "title_f", "text")))
SUGGESTION = db.Shape("suggestion", (("id", "title", "text"), ("t", "title_t", "text"), ("tags", "tags", "json")))


def store(root: Path) -> db.Database:
    """The database that keeps this library's own records (db.own: else db.NotHere)."""
    found = db.database()
    db.own(found, root)
    return found


def _put(c, entry: dict) -> None:
    """One edit into its tables: the titles in a table of their own, unless they aren't titles (kept in more as they are)."""
    record, titles = dict(entry), entry.get("titles")
    whole = isinstance(titles, list) and all(isinstance(t, dict) and isinstance(t.get("id"), str) for t in titles)
    if whole:
        del record["titles"]
    seq = EDIT.insert(c, record, titles=len(titles) if whole else None)
    for position, title in enumerate(titles if whole else []):
        EDIT_TITLE.insert(c, title, edit=seq, position=position)


def _lines(c) -> list[dict]:
    """Every edit, in order, as it went in."""
    titles: dict[int, list[dict]] = {}
    for row in c.execute("SELECT * FROM edit_title ORDER BY edit, position"):
        titles.setdefault(row["edit"], []).append(EDIT_TITLE.record(row))
    out = []
    for row in c.execute("SELECT * FROM edit ORDER BY seq"):
        record = EDIT.record(row)
        if row["titles"] is not None:
            record["titles"] = titles.get(row["seq"], [])
        out.append(record)
    return out


def revision(root: Path) -> int:
    """How many edits there have been: the server lays them over the catalog again when it changes."""
    with store(root).read() as c:
        return c.execute("SELECT coalesce(max(seq), 0) FROM edit").fetchone()[0]


def tag_name(text: str) -> str:
    name = re.sub(r"\s+", " ", text or "").strip()
    if not name or len(name) > TAG_MAX or re.search(r"[\x00-\x1f\x7f]", name):
        raise Exit(USAGE, f"A tag is 1 to {TAG_MAX} characters on one line: {text!r}")
    return name


def snapshot(rec: dict) -> dict:
    """How an edit names a title: its id, plus the title and first file for finding it again."""
    first = rec.get("f") or [[""]]
    return {"id": rec["id"], "t": rec.get("t", ""), "f": unquote(first[0][0])}


def copies_of(records: list[dict]) -> dict:
    """{"copies": {title id: its copies' ids}} for the merged titles among records, else nothing: for an edit that takes
    something away, which Edits.apply then takes from every id the title is read by."""
    found = {r["id"]: list(r["ids"]) for r in records if r.get("ids")}
    return {"copies": found} if found else {}


def _copies(e: dict) -> list[str]:
    """The copies' ids an edit names (copies_of). An edit made before has none, and replays as it always did."""
    found = e.get("copies")
    if not isinstance(found, dict):
        return []
    return [i for ids in found.values() if isinstance(ids, list) for i in ids if isinstance(i, str)]


class Edits:
    """The edits replayed: each id's tags, its declined tags, what was decided for it (added, removed or
    declined: not to be suggested), and the latest title and file seen for each id."""

    def __init__(self, lines: list[dict]) -> None:
        self.names: dict[str, str] = {}  # casefolded tag -> as first written
        self.tags: dict[str, dict[str, None]] = {}  # id -> its tags (casefolded, in order added)
        self.declined: dict[str, dict[str, None]] = {}
        self.decided: dict[str, set[str]] = {}
        self.fixed: dict[str, dict] = {}  # id -> {"kind": "tech", "ereader": "never", "authors": ["..."]}
        self.when: dict[str, dict[str, int]] = {}  # id -> each correction's place in the order replayed: the newer wins
        self.seen: dict[str, dict] = {}
        # an id reattached: to which, with what came from it (for detach), and its place in the order replayed (OWN)
        self.moved: dict[str, tuple[str, dict, tuple[int, int]]] = {}
        self.forgotten: dict[str, dict] = {}  # an id whose tags and corrections you said to forget: them, for unforget
        self.file_format: dict[str, str] = {}  # id -> the format of the file you chose, as its question's option had it
        self.prior: dict[tuple[str, str], tuple] = {}  # (id, field) -> its value and place before its last change
        for n, e in enumerate(lines):
            self.apply(e, n)

    KEPT = ("tags", "declined", "fixed", "when")  # what an id has, each a dict by the id (and decided, a set)
    # A move's place in the order replayed: (the edit's, the move's among those that edit makes). OWN is before them
    # all: what an id had of its own; (n, -1) is just before the edit n, where an answer's edit came (_since).
    OWN = (-1, -1)

    def _move(self, old: str, new: str, at: tuple[int, int] = OWN) -> None:
        """What old has goes to new, as a reattach moves it: old's over new's where both have one. What came is kept, with
        what new had in its place, so that a detach puts it back; and the marks of what was decided for old, with those
        new had already, so that old has them all again then. at: the move's place in the order replayed."""
        came: dict[str, tuple[dict, dict]] = {}
        for name in self.KEPT:
            kept = getattr(self, name)
            had, target = kept.pop(old, {}), kept.setdefault(new, {})
            came[name] = (had, {k: target[k] for k in had if k in target})
            target.update(had)
        decided = self.decided.pop(old, set())
        came["decided"] = (decided, decided & self.decided.get(new, set()))
        self.decided.setdefault(new, set()).update(decided)
        if old in self.file_format:
            self.file_format[new] = self.file_format.pop(old)
        if old in self.seen:
            self.seen[new] = self.seen.pop(old)
        self.moved[old] = (new, came, at)

    def chain(self, title_id: str, since: tuple[int, int] = OWN) -> list[str]:
        """An id, and each id reattaches moved its edits to after it, in order: only a reattach made after they came
        (since: their place in the order replayed, OWN for an id's own) takes them on, each one later than the one
        before, so that one made before (of an id that was a title, went, and is one again) leads nowhere they are."""
        out = [title_id]
        while (to := self.moved.get(title_id)) is not None and to[2] > since:
            title_id, since = to[0], to[2]
            out.append(title_id)
        return out

    def now(self, title_id: str, since: tuple[int, int] = OWN) -> str:
        """The id an id's edits are under now (chain): where the reattaches made after them moved them, in order."""
        return self.chain(title_id, since)[-1]

    def _move_back(self, old: str, new: str) -> None:
        """A reattach of old to new taken back (detach): what came from old goes back to it, and the title that has it
        now (new, or where a later reattach moved new) has again what it had in its place; what changed there since stays.
        A tag or a no is kept as None: one there is told by its key, never by its value."""
        to, came, when = self.moved.get(old, (None, {}, self.OWN))
        if to != new:
            return
        del self.moved[old]
        holder = self.now(new, when)  # where what came is: a reattach of new made before it came led elsewhere
        for name in self.KEPT:
            had, under = came.get(name, ({}, {}))
            target = getattr(self, name).get(holder, {})
            for k, v in had.items():
                if k in target and target[k] == v:
                    target.pop(k)
                    if k in under:
                        target[k] = under[k]
            if had:
                getattr(self, name).setdefault(old, {}).update(had)
        decided, under = came.get("decided", (set(), set()))
        self.decided.get(holder, set()).difference_update(decided - under)  # what the title had itself stays
        if decided:
            self.decided.setdefault(old, set()).update(decided)
        if holder in self.seen and old not in self.seen:
            self.seen[old] = self.seen[holder]

    def _forget(self, old: str, back: bool) -> None:
        """An id's tags and corrections forgotten (forget), as you answered for a title gone; or brought back (unforget)."""
        if not back:
            self.forgotten[old] = {name: getattr(self, name).pop(old, {}) for name in self.KEPT}
            self.forgotten[old]["decided"] = self.decided.pop(old, set())
            return
        was = self.forgotten.pop(old, None)
        for name, had in (was or {}).items():
            if had:
                (getattr(self, name).setdefault(old, set() if name == "decided" else {})).update(had)

    def apply(self, e: dict, n: int = 0) -> None:
        op = e.get("op")
        if op in ("reattach", "detach"):
            ids = e.get("ids")
            for k, (old, new) in enumerate(ids.items() if isinstance(ids, dict) else ()):
                if isinstance(old, str) and isinstance(new, str) and old != new:
                    self._move(old, new, (n, k)) if op == "reattach" else self._move_back(old, new)
            return
        if op in ("forget", "unforget"):
            for old in e.get("ids") if isinstance(e.get("ids"), list) else ():
                if isinstance(old, str):
                    self._forget(old, back=op == "unforget")
            return
        if e.get("field") in ENTRIES and op == "update":  # a few answers (or files left out) of several titles at once
            field = e["field"]
            try:
                value = answer_value(field, e.get("value"), update=True)
            except SystemExit:
                return
            for t in e.get("titles", []) if isinstance(e.get("titles"), list) else ():
                if not isinstance(t, dict) or not isinstance(t.get("id"), str):
                    continue
                self.seen[t["id"]] = t
                fixed = self.fixed.setdefault(t["id"], {})
                now = {**(fixed.get(field) or {}), **value}
                now = {k: v for k, v in now.items() if v is not None}
                if now:
                    fixed[field] = now
                    self.when.setdefault(t["id"], {})[field] = n
                else:
                    fixed.pop(field, None)
            return
        if e.get("field") in (*SINGLE, *TEXT, *ANSWERED) and op in ("set", "clear"):
            value, field = e.get("value"), e["field"]
            if op == "set" and field in SINGLE and value not in single(field, recorded=True):
                return
            if op == "set" and (field in TEXT or field in ANSWERED):
                try:
                    value = text_value(field, value) if field in TEXT else answer_value(field, value)
                except SystemExit:
                    return  # an edit this version can't read: ignored
            for t in e.get("titles", []):
                self.seen[t["id"]] = t
                fixed, when = self.fixed.setdefault(t["id"], {}), self.when.setdefault(t["id"], {})
                before = (fixed.get(field), when.get(field), self.file_format.get(t["id"]) if field == "file" else None)
                if op == "set":
                    fixed[field] = value
                    # an undo (the page's) brings back the value as it was, in its place: a format set again by an undo
                    # is as old as it was, so a file you chose after it stays the one in effect (in_effect); and a file,
                    # with the format its question's option had
                    old = self.prior.get((t["id"], field))
                    back = bool(e.get("undo") and old and old[0] == value)
                    when[field] = old[1] if back and old[1] is not None else n
                    if field == "file":
                        given = e.get("format") if isinstance(e.get("format"), str) and e["format"] else None
                        had = given or (old[2] if back and len(old) > 2 else None)
                        if had:
                            self.file_format[t["id"]] = had
                        else:
                            self.file_format.pop(t["id"], None)
                else:
                    fixed.pop(field, None)
                    when.pop(field, None)
                self.prior[(t["id"], field)] = before
            if op == "clear":  # automatic again: a copy's correction, made before it was merged, goes too
                for i in _copies(e):
                    self.fixed.get(i, {}).pop(e["field"], None)
            return
        if e.get("field") != "tag" or e.get("op") not in OPS or not isinstance(e.get("value"), str):
            return  # a kind of edit this version doesn't know: kept, ignored here
        titles = e.get("titles") or []
        if not isinstance(titles, list) or not all(isinstance(t, dict) and isinstance(t.get("id"), str) for t in titles):
            return
        key, op = e["value"].casefold(), e["op"]
        self.names.setdefault(key, e["value"])
        for t in e.get("titles", []):
            i = t["id"]
            self.seen[i] = t
            have, no, decided = self.tags.setdefault(i, {}), self.declined.setdefault(i, {}), self.decided.setdefault(i, set())
            if op == "add":
                have[key] = None
                no.pop(key, None)
            elif op == "remove":
                have.pop(key, None)
            elif op == "decline":
                no[key] = None
            else:  # undecline: ask again
                no.pop(key, None)
                if key not in have:
                    decided.discard(key)
                continue
            decided.add(key)
        for i in _copies(e):  # a merged title's copies: what the edit takes away goes from their ids too
            if op == "remove":
                self.tags.get(i, {}).pop(key, None)
            elif op in ("add", "undecline"):  # adding overrides a no, and asking again takes it back
                self.declined.get(i, {}).pop(key, None)
                if op == "undecline" and key not in self.tags.get(i, {}):
                    self.decided.get(i, set()).discard(key)

    def of(self, ids: list[str]) -> list[str]:
        """The tags of a title known by these ids (a merged title: every copy's id)."""
        keys = dict.fromkeys(k for i in ids for k in self.tags.get(i, {}))
        return sorted((self.names[k] for k in keys), key=str.casefold)

    def declined_of(self, ids: list[str]) -> set[str]:
        return {k for i in ids for k in self.declined.get(i, {})}

    def decided_of(self, ids: list[str]) -> set[str]:
        return {k for i in ids for k in self.decided.get(i, set())}

    def fixes_of(self, ids: list[str]) -> dict:
        """A title's one-value corrections; for a merged title, the lead's win over its copies'."""
        out: dict[str, str] = {}
        for i in reversed(ids):
            out.update(self.fixed.get(i, {}))
        return out

    def overridden(self, title_id: str) -> bool:
        """Whether the file you chose for a title (file) gives way to a format set since, of another format than that
        file's: a format that agrees with it (Format to keep: PDF, after choosing one of its PDFs) keeps the file. The
        file's format is the one its question's option had (file_format), as a shop's file may have no extension in its
        name; else the one its name says."""
        from .inventory import file_ext

        when, has = self.when.get(title_id, {}), self.fixed.get(title_id, {})
        ext = self.file_format.get(title_id) or file_ext(str(has.get("file") or ""), "")
        return (bool(has.get("file")) and bool(has.get("format")) and when.get("format", -1) > when.get("file", -1)
                and ext != has["format"])

    def in_effect(self, ids: list[str], field: str):
        """A title's correction of field as it counts: as fixes_of has it, but a file you chose (file) is none while a
        format set since overrides it (overridden), so choosing it again makes it the newer, and so the one in effect."""
        value = self.fixes_of(ids).get(field)
        if field == "file" and value is not None and ids and self.overridden(ids[0]):
            return None
        return value


def load_lines(root: Path) -> list[dict]:
    with store(root).read() as c:
        return _lines(c)


# ---- the shelves for everyone (shelves.Shared), kept with the library's other records ----------

def shared_read(c) -> tuple[dict[str, dict], str]:
    """The shelves for everyone by id, in order, and the one the library opens with."""
    found = {row["id"]: {**json.loads(row["body"]), "by": row["made_by"]} for row in c.execute("SELECT * FROM shared_shelf ORDER BY seq")}
    opens = c.execute("SELECT opens_with FROM library").fetchone()
    opens = opens[0] if opens else ""
    return found, opens if opens in found else ""


def load(root: Path) -> Edits:
    return Edits(load_lines(root))


def _append(root: Path, entry: dict) -> None:
    if paths.library_missing(root):  # a change kept for a library whose disk isn't there would be lost to you
        raise OSError(f"The library at {root} isn't there")
    with store(root).write() as c:
        _put(c, entry)


def formats(root: Path) -> dict[str, str]:
    """The formats you chose for Humble titles (hylly set format), by Humble id: what the download plan keeps."""
    fixed = load(root).fixed
    return {i[2:]: f["format"] for i, f in fixed.items() if i.startswith("h:") and isinstance(f, dict) and f.get("format")}


def files(root: Path) -> dict[str, str]:
    """The files you chose for Humble titles (hylly set file, or answering a question: hylly decide), by Humble id, each
    by its name as Humble lists it: what the download plan keeps, over a format, unless you set a format of another
    sort since (hylly set format, or the library page's Format to keep, after a file answer: Edits.overridden); one that
    agrees with the file keeps it, and set back to auto, the file you chose counts again."""
    edits = load(root)
    return {i[2:]: f["file"] for i, f in edits.fixed.items()
            if i.startswith("h:") and isinstance(f, dict) and f.get("file") and not edits.overridden(i)}


def answers(root: Path) -> dict[str, dict]:
    """Your answers to the questions that no other correction says (keep the guess, I moved it), by catalog id: each
    title's, by the question (settled)."""
    return {i: f["answers"] for i, f in load(root).fixed.items() if isinstance(f, dict) and f.get("answers")}


def left_out(root: Path) -> dict[str, set[str]]:
    """The files you left out with a shop list (LEFT), by their title's catalog id, each by its key as text: what a
    download never fetches (shopping.select's yours), and a new list shows unticked, "left out"."""
    return {i: {k[4:] for k, v in f[LEFT].items() if k.startswith("out:") and isinstance(v, dict) and v.get("a") == "out"}
            for i, f in load(root).fixed.items() if isinstance(f, dict) and f.get(LEFT)}


def leave_out(root: Path, changes: dict[str, tuple[str, dict]], by: str) -> int:
    """The files a shop list leaves out from now on, and those it takes again: changes, by the title's catalog id, its
    name and {its file's key: its name (as the shop lists it, or "") to leave it out, None to take it again}. Edits of
    a few entries each (op update, change_answers), titles with the same changes in one, so that each replays, follows a
    title's new id as any correction does, and is undone exactly (undo_update; the library page's Recent changes); hylly
    set left-out auto --id ID takes all of a title's back. How many titles changed."""
    groups: dict[str, tuple[dict, list[dict]]] = {}
    for cid, (title, files) in changes.items():
        entries = [(f"out:{key}", None if name is None else {"a": "out", **({"f": name} if _a_name(name) else {})})
                   for key, name in files.items()]
        for at in range(0, len(entries), ENTRIES_MOST):  # a title of many files in a few edits: each within what one holds
            part = dict(entries[at:at + ENTRIES_MOST])
            groups.setdefault(json.dumps(part, sort_keys=True), (part, []))[1].append({"id": cid, "t": title, "f": [[""]]})
    changed = set()
    for entries, titles in groups.values():
        changed |= {r["id"] for r in change_answers(root, entries, titles, by, field=LEFT)[0]}
    return len(changed)


def kinds(root: Path, letter: str = "") -> dict[str, str]:
    """The kinds you chose (hylly set kind, or the library page), by catalog id: what the downloads go by for a title
    of a shop (shopping.corrected; Humble's plan), and the catalog for where a title is read. letter: only a shop's
    titles (shops.Shop.letter), by its own id for them, the catalog id without its letter: a Humble id for "h"."""
    found = {i: f["kind"] for i, f in load(root).fixed.items() if isinstance(f, dict) and f.get("kind")}
    return {i[len(letter) + 1:]: k for i, k in found.items() if i.startswith(f"{letter}:")} if letter else found


def change(root: Path, op: str, tag: str, records: list[dict], by: str) -> list[dict]:
    """Adds the tag to the records that lack it, removes it from those that have it, declines it for those
    that neither have nor declined it, or asks again where it was declined. One edit. Returns the records changed. A
    record is judged by all its ids, and what is taken away (the tag, a no) goes from all of them: a copy's too, kept
    from before it was merged under another lead. An added tag or a no goes on the title's own id, which counts for the
    whole title."""
    tag = tag_name(tag)
    edits = load(root)
    key = tag.casefold()

    def applies(r: dict) -> bool:
        has = key in {t.casefold() for t in edits.of(ids_of(r))}
        no = key in edits.declined_of(ids_of(r))
        return {"add": not has, "remove": has, "decline": not has and not no, "undecline": no}[op]

    changed = [r for r in records if applies(r)]
    if changed:
        _append(root, {"at": db.now(), "by": by, "field": "tag", "op": op,
                       "value": edits.names.get(key, tag), "titles": [snapshot(r) for r in changed],
                       **(copies_of(changed) if op != "decline" else {})})
    return changed


def change_single(root: Path, field: str, value, records: list[dict], by: str, undo: bool = False,
                  also: dict | None = None) -> tuple[list[dict], dict]:
    """Sets kind, ereader or format, or what a title shows (TEXT), on the records whose value differs, or with
    "auto" clears it. One edit, with each title's previous value; returns the records changed and those
    values. A value is set on a title's own id, which wins over its copies'; "auto" clears it from all its ids. undo: the
    page's Undo, which brings a value back in its place (Edits.apply). also: more for the edit to keep (a file's format,
    as its question's option had it)."""
    if field in TEXT:
        want = None if value == "auto" else text_value(field, value)
    elif field in ANSWERED:
        want = None if value == "auto" else answer_value(field, value)
    elif field not in SINGLE or value not in (*single(field), "auto"):
        raise Exit(USAGE, f"{field} is one of: {', '.join((*single(field), 'auto'))}")
    else:
        want = None if value == "auto" else value
    edits = load(root)
    # set: as it counts, so a file chosen again over a format makes an edit; cleared (auto): as recorded, so it goes
    was = {r["id"]: edits.in_effect(ids_of(r), field) if want is not None else edits.fixes_of(ids_of(r)).get(field)
           for r in records}
    changed = [r for r in records if was[r["id"]] != want]
    if changed:
        _append(root, {"at": db.now(), "by": by, "field": field,
                       "op": "clear" if want is None else "set", **({} if want is None else {"value": want}),
                       "was": {r["id"]: was[r["id"]] for r in changed}, "titles": [snapshot(r) for r in changed],
                       **(copies_of(changed) if want is None else {}), **({"undo": True} if undo else {}),
                       **({k: v for k, v in (also or {}).items() if want is not None})})
    return changed, {r["id"]: was[r["id"]] for r in changed}


def change_answers(root: Path, entries: dict, records: list[dict], by: str, field: str = "catalog",
                   undo: bool = False) -> tuple[list[dict], dict]:
    """A few of the titles' answers changed at once, on every one of the records, by their very ids (op update): each by
    its question's key, an answer (answer_value) or None to take it away; the catalog's (catalog) as it comes, else the
    downloads' (answers). One edit, with each title's answers before it (was), so that an undo puts back exactly what
    was, key by key (undo_update). Returns the records changed, and what each had before."""
    entries = answer_value(field, entries, update=True)
    edits = load(root)
    was = {r["id"]: edits.fixed.get(r["id"], {}).get(field) for r in records}
    changed = [r for r in records if any((was[r["id"]] or {}).get(k) != v for k, v in entries.items())]
    if changed:
        _append(root, {"at": db.now(), "by": by, "field": field, "op": "update",
                       "value": entries, "was": {r["id"]: was[r["id"]] for r in changed},
                       "titles": [snapshot(r) for r in changed], **({"undo": True} if undo else {})})
    return changed, {r["id"]: was[r["id"]] for r in changed}


def undo_update(keys, was: dict) -> list[tuple[dict, list[str]]]:
    """The undo of an edit of a few answers (op update) on several titles, exactly: for each title, each of the keys
    back to what it was before (None: none), grouped by what goes back, a change_answers each: [(entries, ids)]."""
    groups: dict[str, tuple[dict, list[str]]] = {}
    for i, had in was.items():
        back = {k: (had or {}).get(k) for k in keys}
        groups.setdefault(json.dumps(back, sort_keys=True), (back, []))[1].append(i)
    return list(groups.values())


def _bare(i: str, rec: dict) -> dict:
    return {"id": i, "t": rec.get("t", ""), "f": rec.get("f") or [[""]]}


def _first_files(rec: dict) -> dict[str, list]:
    """Each of a card's folder copies by its id, with its first file's name and size (its id comes from that file's path:
    matching.folder_id), for an answer about two copies to find one again whose id changed (catalog.same_pairs); a
    shop's copy keeps its id, and needs none."""
    out = {}
    for f in rec.get("f") or []:
        if isinstance(f, list) and f and isinstance(f[0], str):
            i = folder_id(unquote(f[0]))
            if i in ids_of(rec) and i not in out and len(f) > 2 and isinstance(f[2], int) and not isinstance(f[2], bool):
                out[i] = [PurePosixPath(unquote(f[0])).name, f[2]]
    return out


def set_copies(root: Path, records: list[dict], value: str, by: str) -> tuple[list[dict], dict]:
    """hylly set copies, and a card's details in the library page: apart, a card that is two books or more (its copies
    by their ids, every two of them apart, each with its first file as a question's answer keeps it: merge_copies keeps
    them so in every build, a copy moved too, and no same question asks about them); auto, the rules decide again (your
    answers about the copies of these cards, the same or apart, taken back). One edit (op update) on every id of each
    card; returns the cards changed, and the undo of all of it ({keys, was}: undo_update)."""
    if value not in ("apart", "auto"):
        raise Exit(USAGE, "copies is apart (a card's copies are two books or more) or auto (as the rules have them).")
    edits = load(root)
    changed, keys, before = [], set(), {}
    for rec in records:
        ids = ids_of(rec)
        if value == "apart":
            if len(ids) < 2:
                continue
            sides = _first_files(rec)
            entries = {f"same:{'|'.join(sorted((a, b)))}": {"a": "apart", **({"w": {i: sides[i] for i in (a, b) if i in sides}}
                                                                              if any(i in sides for i in (a, b)) else {})}
                       for n, a in enumerate(ids) for b in ids[n + 1:]}
            holders = [_bare(i, rec) for i in ids]
        else:
            entries = {k: None for i in ids for k in (edits.fixed.get(i, {}).get("catalog") or {})
                       if k.startswith("same:") and set(k[len("same:"):].split("|")) & set(ids)}
            holders = [_bare(i, rec) for i, f in edits.fixed.items() if set(f.get("catalog") or {}) & set(entries)]
        done, was = change_answers(root, entries, holders, by) if entries else ([], {})
        if done:
            changed.append(rec)
            keys |= set(entries)
            before = {**was, **before}
    return changed, {"keys": sorted(keys), "was": before}


def set_refused(root: Path, records: list[dict], source: str, by: str) -> tuple[list[dict], dict]:
    """hylly set refused, and a card's details in the library page: a lookup source whose match for these titles is
    another book (its id or its name), whose answers they get no more, nor is it asked about them again (lookups.Answers,
    lookups.run), on every id of each card; auto, your refusals of these titles taken back. One edit (op update) for
    each card; returns the cards changed, and the undo of all of it ({keys, was}: undo_update)."""
    from .sites import LOOKUPS

    if source != "auto" and lookup_source(source) is None:
        raise Exit(USAGE, f"refused is a lookup source, {', '.join(LOOKUPS)}, or auto.")
    edits, changed, keys, before = load(root), [], set(), {}
    for rec in records:
        ids = ids_of(rec)
        if source == "auto":
            entries = {k: None for i in ids for k, v in (edits.fixed.get(i, {}).get("catalog") or {}).items()
                       if k.startswith("lookup:") and isinstance(v, dict) and v.get("a") == "refused"}
        else:
            entries = {f"lookup:{lookup_source(source)}": {"a": "refused"}}
        done, was = change_answers(root, entries, [_bare(i, rec) for i in ids], by) if entries else ([], {})
        if done:
            changed.append(rec)
            keys |= set(entries)
            before = {**was, **before}
    return changed, {"keys": sorted(keys), "was": before}


def lookup_source(said: str) -> str | None:
    """A lookup source's id, by its id or its name in letters of either size (sites.LOOKUPS); None for no source's."""
    from .sites import LOOKUPS

    named = {sid.lower(): sid for sid in LOOKUPS} | {row.name.lower(): sid for sid, row in LOOKUPS.items()}
    return named.get(str(said).strip().lower())


KEY_WORDS = {"lookup": "a lookup's match"}  # an answer's key, by its kind, where it is no question's kind


def _said_answers(given: dict, taken: list[str]) -> str:
    """An edit of answers in words, for the library page's Recent changes: what each answer given says, by its question,
    and which were taken back."""
    words = {"again": "download it again", "moved": "moved by hand", "keep": "keep the guess", "same": "one book",
             "apart": "two books", "use": "its match used", "refused": "its match refused, another book",
             "not": "not books", "books": "books"}

    def named(key: str) -> str:
        kind, _, about = key.partition(":")
        about = source_name(about) if kind == "lookup" else about
        return (QUESTIONS[kind].short if kind in QUESTIONS else KEY_WORDS.get(kind, kind)) + (
            f" ({about})" if about and kind != "same" else "")

    answered = ["answered " + "; ".join(named(k) + ": " + words.get(v["a"], v["a"]) for k, v in given.items())] if given else []
    back = ["took back the answer about " + "; ".join(named(k) for k in taken)] if taken else []
    return "; ".join(answered + back) or "answered nothing new"


OP_WORDS = {"reattach": "put the tags and corrections of a title gone on the title it became",
            "detach": "took back where the tags and corrections of a title gone were put",
            "forget": "forgot the tags and corrections of a title gone",
            "unforget": "brought back the tags and corrections of a title gone"}


def history(root: Path, n: int = 30) -> list[dict]:
    """The latest changes, newest first: what, when, by whom, and on which titles (for undoing). The answers to questions
    (a file chosen, a title's answers given or taken back) too, in words (said), and where a title gone's tags and
    corrections were put or forgotten (no Undo for those: undo False; hylly decide NUMBER auto takes an answer back)."""
    out, names = [], {}
    for e in load_lines(root):
        field, op = e.get("field"), e.get("op")
        for t in e.get("titles") if isinstance(e.get("titles"), list) else ():
            if isinstance(t, dict) and isinstance(t.get("id"), str) and t.get("t"):
                names[t["id"]] = t["t"]
        if op in OP_WORDS:  # a title gone's tags and corrections, put on another title or forgotten
            ids = e.get("ids")
            ids = list(ids) if isinstance(ids, (dict, list)) else []
            said = (f"followed {many(len(ids), 'title')} that moved or were renamed, with their tags "
                    "and corrections") if op == "reattach" and e.get("by") == "hylly catalog" else OP_WORDS[op]
            out.append({"at": e.get("at", ""), "by": e.get("by", ""), "field": op, "op": op, "tag": "", "was": {},
                        "said": said, "undo": False, "ids": [i for i in ids if isinstance(i, str)],
                        "titles": [names.get(i, "") for i in ids[:3] if isinstance(i, str)], "count": len(ids)})
            continue
        if (field == "tag" and op in OPS) or ((field in SINGLE or field in TEXT or field in ANSWERED) and op in ("set", "clear")) \
                or (field in ENTRIES and op == "update"):
            titles = e.get("titles", [])
            said = {}
            if field == LEFT:  # files a shop list left out, or took again: how many, in words
                value = e.get("value") if isinstance(e.get("value"), dict) else {}
                given, taken = sum(isinstance(v, dict) for v in value.values()), sum(v is None for v in value.values())
                words = ([f"left {many(given, 'file')} out with a shop list"] if given else []) + (
                    [f"took back {many(taken, 'file')} left out"] if taken else [])
                said = {"said": "; ".join(words) if op == "update" else
                        "took back the files left out with a shop list" if op == "clear" else "left files out with a shop list"}
            elif field in ("answers", "catalog"):  # what changed, in words: the answers given, and those taken back
                value = e.get("value") if isinstance(e.get("value"), dict) else {}
                if op == "update":
                    given, taken = {k: v for k, v in value.items() if isinstance(v, dict)}, [k for k, v in value.items() if v is None]
                else:
                    was = next(iter((e.get("was") or {}).values()), None) or {}
                    given = {k: v for k, v in value.items() if isinstance(v, dict) and was.get(k) != v}
                    taken = [k for k in was if k not in value]
                said = {"said": _said_answers(given, taken)}
            elif field == "file":
                said = {"said": f"chose {e['value']} as the file to keep" if op == "set" else "took back the file chosen"}
            out.append({"at": e.get("at", ""), "by": e.get("by", ""), "field": field, "op": op,
                        "tag": e.get("value", "auto"), "was": e.get("was", {}), **said,
                        "ids": [t["id"] for t in titles], "titles": [t.get("t", "") for t in titles[:3]], "count": len(titles)})
    return out[::-1][:n]


def placing(edits: Edits, titles: Titles, listed) -> tuple[dict[str, str], list[dict]]:
    """Where the tags and corrections of ids no title has any more go, worked out alone, nothing written: the moves (to
    the title each has become in this build: titles.follow, a title new in this build with the same file, or a file of
    the same name and size), and the ids whose edits stay unplaced, with what is kept for each ("kept"), the first title
    first. listed: called once, if at all, for the ids the shops list and their questions name (_listed_at_shops)."""
    lost = [i for i in {*edits.tags, *edits.declined, *edits.fixed}
            if (edits.tags.get(i) or edits.declined.get(i) or edits.fixed.get(i)) and i not in titles]
    moves, unplaced, known = {}, [], None
    for old in lost:
        was = edits.seen.get(old, {})
        found = titles.follow(old, was)
        if found:
            moves[old] = found
            continue
        if not edits.tags.get(old) and not edits.declined.get(old):  # corrections alone: of a shop's title not
            if not set(edits.fixed.get(old, {})) - set(ENTRIES):  # downloaded yet, which its shop or a question names, or
                continue  # answers or files left out alone (an item left out is in no catalog, a folder's files), they
                # wait with that id
            known = listed() if known is None else known
            if old in known:
                continue
        unplaced.append({**was, "id": old, "kept": _kept(edits, old)})
    return moves, sorted(unplaced, key=lambda u: (str(u.get("t") or u["id"]).casefold(), u["id"]))


def reattach(root: Path, titles: Titles, log=print) -> list[dict]:
    """Moves the tags and corrections of ids no title has any more to the title each has become in this build (placing),
    with one reattach edit. Returns the titles whose edits stay unplaced, with what is kept for each ("kept"): they apply
    again should the title return, and the catalog asks where each went (catalog.questions_of: a place question)."""
    moves, unplaced = placing(load(root), titles, lambda: _listed_at_shops(root))
    if moves:
        _append(root, {"at": db.now(), "by": "hylly catalog", "op": "reattach", "ids": moves})
        log(f"  Your tags and corrections followed {many(len(moves), 'title')} that moved or were renamed.")
    return unplaced


def _listed_at_shops(root: Path) -> set[str]:
    """The catalog ids of every title the shops' newest snapshots list, and those the questions name: a correction made
    by --id of one not downloaded yet (hylly set kind, file, or an answer) isn't a title gone from the library."""
    from . import snapshot
    from .inventory import parse_orders
    from .shops import SHOPS

    state, found = paths.state_dir(root), set()
    for shop in SHOPS.values():
        try:
            source = snapshot.latest(state, shop.id)
            data = source[1] if source else None
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):  # orders (Humble's)
            found |= {f"{shop.letter}:{p.machine_name}" for p in parse_orders(data)[0] if p.machine_name}
        for e in data if isinstance(data, list) else []:
            for t in (e.get("books") or [] if isinstance(e, dict) and "books" in e else [e]):  # a bundle of before, or a title
                if isinstance(t, dict) and t.get("id") is not None:
                    found.add(f"{shop.letter}:{t['id']}")
    try:  # a shop's questions: the catalog's own name titles gone, which are what this is asked about
        found |= {q["catalog_id"] for q in questions(root) if q["shop"] != CATALOG}
    except (OSError, db.Newer, db.NotHere, db.Unusable):
        pass
    return found


FIELD_WORDS = {"ereader": "e-reader feed", "file": "file to keep", "answers": "answers",  # a correction's field, as said
               "catalog": "answers to the catalog's questions", LEFT: "files left out with a shop list"}


def _kept(edits: Edits, title_id: str) -> str:
    """What is kept for an id, in words: its tags, how many suggested tags it said no to, what of it you corrected."""
    out = []
    if edits.tags.get(title_id):
        out.append("tags " + ", ".join(edits.names.get(k, k) for k in edits.tags[title_id]))
    if edits.declined.get(title_id):
        n = len(edits.declined[title_id])
        out.append(f"{many(n, 'suggested tag')} declined")
    if edits.fixed.get(title_id):
        out.append("your " + ", ".join(FIELD_WORDS.get(f, f) for f in edits.fixed[title_id]))
    return "; ".join(out)


# ---- suggestions ---------------------------------------------------------------------------

STARTER = """# Words that suggest your tags: hylly tag suggest, or Find suggestions on the library page.
# Each tag's own name always counts. A keyword matches the start of words, so "knit" also finds
# knitting and knitted; a phrase matches words in a row. A title matching a "not" word is never
# suggested for that tag. The title, series, subjects, bundle, folder and collection are looked at;
# description = true also looks in the description (more finds, more wrong ones). Names with
# spaces go in quotes: ["Weird Fiction"].
#
# [Knitting]
# keywords = ["knit", "purl", "yarn"]
# not = ["tightly knit"]
#
# [Crochet]
# keywords = ["crochet", "amigurumi", "granny square"]
"""
GROUPING = ("bundle", "folder", "collection", "series")  # a match here is shared by the group's titles


def rules_file(root: Path) -> Path:
    return folder(root) / "tags.toml"


@dataclass
class Rule:
    tag: str
    keywords: list[tuple[str, list[str]]]  # as written, and as words: a tag's keywords and a title's words compare
    # alike, as searches and series do (rules.words_of: letters and digits of any script, case and accents not counting)
    nots: list[list[str]]
    description: bool = False


def load_rules(root: Path, edits: Edits | None = None) -> list[Rule]:
    """tags.toml, plus every tag in use: each with its own name as a keyword. Read as a rules file is (rules.read_file),
    and each tag's table checked, so that a mistake names the file, the tag and the key: keywords = "knit", a text and
    no list, would be read letter by letter, and its tag suggested for nearly every title."""
    from .rules import bare  # a tag's name as TOML writes it: ["Weird Fiction"]

    path = rules_file(root)
    try:
        data = paths.read_toml(path)  # a BOM at its start, as Notepad may write, is fine
    except FileNotFoundError:
        data = {}
    except paths.NotToml as e:
        raise Exit(NOT_SET_UP, e.said() + (" A tag's name with spaces goes in quotes: [\"Weird Fiction\"]."
                                           if e.kind == "syntax" else "")) from None
    except OSError as e:
        raise Exit(NOT_SET_UP, f"{path}: can't be read ({e.strerror or e}).") from None
    for tag, c in data.items():
        if not isinstance(c, dict):
            continue
        where = f"{path}, [{bare(tag)}]"
        for key in ("keywords", "not"):  # a number is a word too, as it was always read: [Year] keywords = [1984]
            if key in c and not (isinstance(c[key], list) and all(
                    isinstance(w, str) or isinstance(w, (int, float)) and not isinstance(w, bool) for w in c[key])):
                raise Exit(NOT_SET_UP, f"{where} {key}: a list of words in quotes, such as [\"knit\", \"purl\"].")
        if "description" in c and not isinstance(c["description"], bool):
            raise Exit(NOT_SET_UP, f"{where} description: true or false.")
    edits = edits or load(root)
    conf = {k.casefold(): (k, v) for k, v in data.items() if isinstance(v, dict)}
    rules = []
    for key in dict.fromkeys([*edits.names, *conf]):
        name, c = conf.get(key, (edits.names.get(key, key), {}))
        name = edits.names.get(key, name)
        written = [name, *[str(w) for w in c.get("keywords", [])]]
        rules.append(Rule(name, [(w, words_of(w)) for w in written if words_of(w)],
                          [words_of(str(w)) for w in c.get("not", []) if words_of(str(w))], bool(c.get("description", False))))
    return rules


def _hit(tokens: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(all(tokens[i + j].startswith(phrase[j]) for j in range(n)) for i in range(len(tokens) - n + 1))


def _fields(rec: dict) -> list[tuple[str, str]]:
    """Where keywords are looked for. The fields a group shares come first, so that a bundle's titles share
    one reason (and can be reviewed together) even when a title also has the word itself."""
    first = (rec.get("f") or [[""]])[0][0]
    return [("bundle", rec.get("b", "")), ("folder", unquote(first).rpartition("/")[0]), ("collection", rec.get("col", "")),
            ("series", rec.get("s", "")), ("title", rec.get("t", "")), ("subjects", " | ".join(rec.get("sub", [])))]


def suggest(root: Path, records: list[dict]) -> list[dict]:
    """Every title some rule matches, with the tags it suggests and why, leaving out what was decided."""
    edits = load(root)
    rules = load_rules(root, edits)
    out = []
    for rec in records:
        decided = edits.decided_of(ids_of(rec))
        todo = [r for r in rules if r.tag.casefold() not in decided]
        if not todo:
            continue
        fields = [(label, text, words_of(text)) for label, text in _fields(rec)]
        found = []
        for r in todo:
            look = fields + ([("description", "", words_of(rec.get("d", "")))] if r.description else [])
            if any(_hit(toks, p) for _, _, toks in look for p in r.nots):
                continue
            why = next(((label, text, w) for label, text, toks in look for w, p in r.keywords if _hit(toks, p)), None)
            if why:
                label, text, w = why
                found.append({"tag": r.tag, "why": f'"{w}" in the {label}', "group": f"{label}: {text}" if label in GROUPING else ""})
        if found:
            out.append({"id": rec["id"], "t": rec.get("t", ""), "tags": found})
    return out


def find_suggestions(root: Path, records: list[dict]) -> list[dict]:
    """suggest(), kept as the queue for review in place of the one before; tags.toml is started with examples if missing."""
    if not rules_file(root).exists():
        rules_file(root).parent.mkdir(parents=True, exist_ok=True)
        rules_file(root).write_text(STARTER, encoding="utf-8")
    found = suggest(root, records)
    with store(root).write() as c:
        c.execute("DELETE FROM suggestion")
        for s in found:
            SUGGESTION.insert(c, s)
        c.execute("UPDATE library SET suggestions_made = ?", (db.now(),))
    return found


def _queue(c) -> dict:
    made = c.execute("SELECT suggestions_made FROM library").fetchone()
    return {"made": made[0] if made else "", "items": [SUGGESTION.record(row) for row in c.execute("SELECT * FROM suggestion ORDER BY seq")]}


def pending(root: Path, index: dict[str, dict]) -> dict:
    """The suggestions still waiting for an answer (index: every id, copies' too, to its title)."""
    with store(root).read() as c:
        data = _queue(c)
    edits = load(root)
    out = []
    for s in data.get("items", []):
        rec = index.get(s["id"])
        if rec is None:
            continue  # gone from the library since
        decided = edits.decided_of(ids_of(rec))
        left = [t for t in s["tags"] if t["tag"].casefold() not in decided]
        if left:
            out.append({**s, "id": rec["id"], "tags": left})
    return {"made": data.get("made", ""), "items": out}


def declined(root: Path, index: dict[str, dict]) -> list[dict]:
    """Every title and tag you said no to, for changing your mind."""
    edits = load(root)
    seen, out = set(), []
    for i, keys in edits.declined.items():
        rec = index.get(i)
        for key in keys:
            if rec and (rec["id"], key) not in seen:
                seen.add((rec["id"], key))
                out.append({"id": rec["id"], "t": rec.get("t", ""), "tag": edits.names.get(key, key)})
    return sorted(out, key=lambda d: (d["tag"].casefold(), d["t"].casefold()))


# ---- questions: what a download's or the catalog's rules can't settle by themselves, put to you ----------------------

@dataclass(frozen=True)
class Question:
    """A kind of question: how it is asked, what keeping the rules' guess means, as hylly decide says them, and in a few
    words, as the library page's Recent changes says an answer (short). catalog: one a catalog build finds, whose
    subject is the catalog's (CATALOG): its titles, a title gone, or one of your folders. quiet: found and kept as any
    other, but left out of the list and its counts unless asked for by its kind (hylly decide --kind), shown instead
    with its title (a lookup's near match: hundreds on a big library, most of them another edition)."""
    ask: str
    keep: str
    short: str
    catalog: bool = False
    quiet: bool = False


CATALOG = "catalog"  # the shop column of the catalog's questions: their subjects are the catalog's own
# A kind of question more is a row here, the code that finds it (a shop's run: the shops' core's select, or a shop's own,
# Humble's items; a catalog build: catalog.questions_of), and, for an answer that is none of those below, what it makes
# (_made): a file correction, a few of a title's answers, or the reattach or forgetting of a title's tags and
# corrections. Each is read where the code that finds it asks: settled, moved, kept_files; the catalog's same_answers
# (merge_copies), lookup_answers (lookups.Answers) and folder_books (catalog.folder_items).
QUESTIONS = {
    "which-file": Question("Which of its files to keep?", "the file the rules took stays the one kept", "which file"),
    "title": Question("Is it a title, to download and catalog?", "it stays left out, as the rules have it", "a title"),
    "moved": Question("Its file is gone from where it was downloaded: download it again?",
                      "it is downloaded again whenever it is missing, as before", "a file gone"),
    "same": Question("Are these two copies one book?", "they stay two cards, as the rules have them", "two copies", True),
    "match": Question("Is a lookup's near miss the right book?", "none of them is used, as the rules have it",
                      "a lookup's match", True, quiet=True),
    "place": Question("Its title is gone from the library: are your tags and corrections of it for one of these?",
                      "they keep waiting for it, should it come back", "a title gone", True),
    "files": Question("Files of a kind the rules don't take as books: are they books?",
                      "they are passed over, as before", "files in a folder", True),
}
MOVED = ({"word": "Download it again", "answer": "again"},  # a "moved" question's options, the guess first
         {"word": "I moved it by hand: never fetch it again", "answer": "moved"})
LEAVE_OUT = {"word": "Leave it out: not a title", "answer": "keep"}  # a "title" question's guess, after its files
SAME = ({"word": "The same book: one card", "answer": "same"},  # a "same" question's options, the guess (apart) last
        {"word": "Two books: keep them apart", "answer": "apart"})
NONE_OF_THESE = {"word": "None of these: they are other books", "answer": "none"}  # a "match" question's guess, last
WAITING = ({"word": "Keep them waiting: it may come back", "answer": "keep"},  # a "place" question's, after its titles
           {"word": "Forget them", "answer": "forget"})
FILES = ({"word": "Not books: pass them over", "answer": "not"},  # a "files" question's options, the guess first
         {"word": "Books: take them into the catalog", "answer": "books"})
QUESTION = db.Shape("question", (("kind", "kind", "text"), ("shop", "shop", "text"), ("item", "item", "text"),
                                 ("file", "file", "text"), ("title", "title_t", "text"), ("options", "options", "json"),
                                 ("guess", "guess", "int"), ("held", "held", "bool"), ("first", "first_found", "text"),
                                 ("last", "last_found", "text"), ("answer", "answer", "json")))
NUMBER_MOST = 2**63  # a question's number is below this (SQLite's): anything else is no question's
_told: set[str] = set()  # the shops this Hylly doesn't know whose questions were left out, said once


def file_option(name: str, ext: str, size: int, label: str = "") -> dict:
    """One of a title's files as an option of a question: its name as its shop lists it (answering with it keeps it: a
    file correction), its format and its size, and the shop's label for it, where it has one."""
    return {"file": name, "format": ext, "size": size, **({"label": label} if label else {})}


def fingerprint(options) -> str:
    """What a question offered, in a few letters: only what tells each option apart, a file's name and size, or an
    answer's code (keep, again, moved) with what the option is about where it says (m: a lookup's match, the title it
    names, the files of two copies), never the words people read, which may be put better in a later version. An
    answer kept by it (keep the guess) stands while the question offers the same; and an answer is refused when the
    question offers other options than it was given for (answer's offered)."""
    told = [[o.get("file"), o.get("size")] if "file" in o else [o.get("answer"), *([o["m"]] if "m" in o else [])]
            for o in options if isinstance(o, dict)]
    return hashlib.sha1(json.dumps(told, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def mark(*parts) -> str:
    """What an option is about, in a few letters (an option's m): a match's address or words, two copies' files."""
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]


def settled(answered: dict | None, kind: str, file: str, options) -> str:
    """The answer code of yours (an option's: LEAVE_OUT's keep, say) that settles a question of a title, from its
    answers (answers: by catalog id), while the question offers what it did then; "" for none."""
    found = (answered or {}).get(f"{kind}:{file}")
    return found["a"] if isinstance(found, dict) and found.get("o") == fingerprint(options) else ""


def moved(answered: dict | None, file) -> str:
    """Your answer for a file of a title gone from its place (MOVED's codes: again, moved), from the title's answers: the
    file's own, else the whole title's, however it was asked (its folder there, or gone); "" for none. Its options
    never change, so no fingerprint is asked of it."""
    for key in (f"moved:{file}", "moved:"):
        found = (answered or {}).get(key)
        if isinstance(found, dict) and found.get("a") in ("again", "moved"):
            return found["a"]
    return ""


def kept_files(root: Path) -> dict[str, tuple[str, str]]:
    """The files you kept as the rules took them (keep, on a which-file question), by Humble id: (its name as Humble
    lists it, the fingerprint of what the question offered then). Humble's plan keeps that file while it is offered,
    and asks again, with it as the guess, once the title offers other files of that sort (plan.decide)."""
    out = {}
    for i, found in answers(root).items():
        kept = found.get("which-file:") if i.startswith("h:") else None
        if isinstance(kept, dict) and kept.get("a") == "keep" and kept.get("f"):
            out[i[2:]] = (kept["f"], kept["o"])
    return out


# ---- the catalog's answers, as the build reads them ------------------------------------------------------------------

def folder_key(name: str) -> str:
    """One of your folders as the edits name it (a folder isn't a title): its answers about the files in it."""
    return f"folder:{name}"


def folder_books(edits: Edits) -> dict[str, set[str]]:
    """The suffixes you said are books in a folder of yours (a files question, books), by the folder's name: a build takes
    them as that folder's books from then on."""
    out: dict[str, set[str]] = {}
    for i, fixed in edits.fixed.items():
        if i.startswith("folder:"):
            for key, v in (fixed.get("catalog") or {}).items():
                if key.startswith("files:") and isinstance(v, dict) and v.get("a") == "books":
                    out.setdefault(i[len("folder:"):], set()).add(key[len("files:"):])
    return out


def folder_answers(edits: Edits) -> dict[tuple[str, str], str]:
    """Your answers about the files of a suffix in a folder (books or not), by (folder, suffix)."""
    return {(i[len("folder:"):], key[len("files:"):]): v["a"] for i, fixed in edits.fixed.items() if i.startswith("folder:")
            for key, v in (fixed.get("catalog") or {}).items() if key.startswith("files:") and isinstance(v, dict)}


def same_answers(edits: Edits) -> dict[str, dict]:
    """Your answers about two copies (the same book, or apart: a same question's, or hylly set copies apart), by their
    key, the two ids in it apart ("same:A|B"): each with its code, what was offered then (o) and each copy's first file
    by its id (w: its name and size), by which the build finds a copy whose id changed."""
    out: dict[str, dict] = {}
    for fixed in edits.fixed.values():
        for key, v in (fixed.get("catalog") or {}).items():
            if key.startswith("same:") and isinstance(v, dict) and v.get("a") in ("same", "apart"):
                out[key] = v
    return out


def lookup_answers(edits: Edits) -> dict[str, dict[str, dict]]:
    """Your word on the lookups' answers for a title (a match question's, or hylly set refused): by catalog id, each
    source's by its id, use (that match, by its m) or refused (none of that source's answers for it, nor asked again)."""
    out: dict[str, dict[str, dict]] = {}
    for i, fixed in edits.fixed.items():
        for key, v in (fixed.get("catalog") or {}).items():
            if key.startswith("lookup:") and isinstance(v, dict) and v.get("a") in ("use", "refused"):
                out.setdefault(i, {})[key[len("lookup:"):]] = v
    return out


def source_name(source: str) -> str:
    """A lookup source's name as people know it (sites.LOOKUPS), by its id; an id it doesn't know as it is."""
    from .sites import LOOKUPS

    row = LOOKUPS.get(source)
    return row.name if row else source


def catalog_id(shop: str, item) -> str:
    """A shop's title by its catalog id: the shop's letter, a colon and its id there. A catalog's question's subject is
    the catalog's own: a title's id (two copies': the first's), or one of your folders, which is none ("")."""
    from .shops import SHOPS

    if shop == CATALOG:
        return str(item).split("|")[0] if ":" in str(item) else ""
    return f"{SHOPS[shop].letter}:{item}"


def _rows(c, where: str, args: tuple) -> list[dict]:
    """The questions as kept, but those of a shop this Hylly doesn't know (an older or newer one's: said once, left out)."""
    from .shops import SHOPS

    found = [{**QUESTION.record(row), "seq": row["seq"]} for row in c.execute(f"SELECT * FROM question {where} ORDER BY seq", args)]
    strange = sorted({r["shop"] for r in found if r["shop"] not in SHOPS and r["shop"] != CATALOG} - _told)
    if strange:
        _told.update(strange)
        print(f"Questions of a shop this Hylly doesn't know ({', '.join(strange)}) are left out of the list.", file=sys.stderr)
    return [r for r in found if r["shop"] in SHOPS or (r["shop"] == CATALOG and r["kind"] in QUESTIONS)]


def _shown(r: dict) -> dict:
    """A question as hylly decide, a run's --json and the server page show it: its number (id) to answer it by, and what
    it offers in a few letters (offered: its fingerprint), which an answer may be given with, to be refused should the
    question offer other options by then. The catalog's say what it is about (subjects: the titles, each with its first
    file and its size). An answered one says its answer too (answer: the option's number, who, when)."""
    kind = QUESTIONS.get(r["kind"])
    given = r.get("answer") if isinstance(r.get("answer"), dict) else None
    return {"id": r.get("seq"), "kind": r["kind"], "shop": r["shop"], "catalog_id": catalog_id(r["shop"], r["item"]),
            "file": r.get("file", ""), "title": r.get("title", ""), "ask": kind.ask if kind else r["kind"],
            "why": r.get("why", ""), "options": r["options"], "guess": r.get("guess"), "held": bool(r.get("held")),
            "keep": kind.keep if kind else "", "offered": fingerprint(r["options"]), "first_found": r.get("first", ""),
            "last_found": r.get("last", ""), **({"subjects": r["subjects"]} if r.get("subjects") else {}),
            **({"answer": {k: given[k] for k in ("option", "by", "at") if k in given}} if given else {})}


def keep_questions(root: Path, shop: str, found: list[dict], covered, keep: bool = True,
                   gone=lambda row: False) -> tuple[list[dict], int]:
    """A run's questions at a shop (each: kind, item, file, title, options, guess, held, why), or a catalog build's (shop
    CATALOG, and subjects), kept with the library's records, each by its kind and its subject (the shop, the title's id
    there, the file's key): one found again keeps its number and when it was first found, and loses an answer that
    didn't settle it; one the run covered (covered(row): its title looked at, a kind the run finds wholly, or after a
    complete run a title the shop no longer lists; for a build, every one) but found no more goes, settled or gone from
    the shop, but for one answered whose answer stands (its edit not taken back: the answered list), while its subject
    is there (gone(row): the catalog's two copies, title or folder gone; its answer's edit stays as it is). A new one gets
    a number no question has had (AUTOINCREMENT), so an answer typed from an older list never reaches another question.
    The shop's open questions afterwards (_shown), every kind's, and how many of them are new, of the kinds not quiet.
    keep False: a dry run's, only worked out."""
    now = db.now()
    with (store(root).write() if keep else store(root).read()) as c:
        rows = {(r["kind"], r["item"], r["file"]): r for r in _rows(c, "WHERE shop = ?", (shop,))}
        new, seen = 0, set()
        for q in found:
            key = (q["kind"], str(q["item"]), str(q.get("file", "")))
            if key in seen:
                continue
            seen.add(key)
            had = rows.get(key)
            new += had is None and not (key[0] in QUESTIONS and QUESTIONS[key[0]].quiet)  # the quiet ones aren't said new
            record = {"kind": key[0], "shop": shop, "item": key[1], "file": key[2], "title": q.get("title", ""),
                      "options": q["options"], "guess": q.get("guess"), "held": bool(q.get("held")),
                      "first": had["first"] if had else now, "last": now, **({"why": q["why"]} if q.get("why") else {}),
                      **({"subjects": q["subjects"]} if q.get("subjects") else {})}
            if keep and had:
                QUESTION.update(c, record, "seq = ?", had["seq"])
            elif keep:
                had = {"seq": QUESTION.insert(c, record)}
            rows[key] = {**record, "seq": had["seq"] if had else None}
        edits = None
        for key, r in list(rows.items()):
            if key not in seen and covered(r):
                if r.get("answer") is not None:  # answered: it stays, on the answered list, while its answer stands
                    edits = load(root) if edits is None else edits
                    if stands(r, edits) and not gone(r):
                        continue
                del rows[key]
                if keep:
                    c.execute("DELETE FROM question WHERE seq = ?", (r["seq"],))
    return [_shown(r) for r in rows.values() if r.get("answer") is None], new


def questions(root: Path, shop: str | None = None, answered: bool = False, kinds=None, lapsed: bool = False) -> list[dict]:
    """The questions open, waiting for your answer (_shown), the oldest first: every shop's and the catalog's, or one's.
    answered: those answered instead, whose answers stand (the answered list), each with its answer: one taken back
    another way (Recent changes' Undo, hylly set ... auto, an older Hylly's writes) is on neither list until the next run
    or build finds it again or lets it go. lapsed: with the open ones, those too (a title's details show them again at
    once). kinds: those of these kinds alone; as it comes, every kind but the quiet ones (Question.quiet: a lookup's near
    match)."""
    where = "WHERE answer IS NULL" if not answered and not lapsed else "" if lapsed else "WHERE answer IS NOT NULL"
    shop_where = (" AND " if where else "WHERE ") + "shop = ?" if shop else ""
    with store(root).read() as c:
        rows = _rows(c, where + shop_where, (shop,) if shop else ())
    if answered or lapsed:
        edits = load(root) if any(r.get("answer") is not None for r in rows) else None
        rows = [r for r in rows if (r.get("answer") is not None and stands(r, edits)) == answered
                or (lapsed and r.get("answer") is None)]
    found = [_shown(r) for r in rows]
    return [q for q in found if (q["kind"] in kinds if kinds is not None else not QUESTIONS[q["kind"]].quiet)]


def quiet_count(root: Path) -> int:
    """How many of the quiet questions are open (a lookup's near matches), each shown with its title."""
    return len(questions(root, kinds={k for k, q in QUESTIONS.items() if q.quiet}))


def near_line(n: int, after: bool) -> str:
    """The short line that ends the build's say and hylly decide's list, of the quiet questions: "" for none. after:
    following a count of the others."""
    if not n:
        return ""
    said = (f"{many(n, 'near match', 'near matches')} from the lookups, in {'its' if n == 1 else 'each'} title's details "
            "(hylly decide --kind match)")
    return f"and {said}" if after else said[:1].upper() + said[1:]


def asked(root: Path, shop: str) -> set[tuple[str, str, str]]:
    """A shop's open questions by their subjects, (kind, item, file): a run finds one again even where it opens no new
    one (a title's file the rules were unsure of, downloaded since as they guessed)."""
    return {(q["kind"], q["catalog_id"].split(":", 1)[1], q["file"]) for q in questions(root, shop)}


def to_decide(n: int, new: int) -> str:
    """How many questions are open, the new ones apart, in words: "" for none."""
    if not n:
        return ""
    return f"{many(n, 'question')} to decide" + (
        "" if not new else (" (new)" if n == 1 else ", all new") if new == n else f", {new} of them new")


def did(question: dict, option: dict) -> str:
    """What an answer does, in words: the file chosen, a title made, a file never fetched again, two copies one card."""
    if "file" in option:
        return (f"makes it a title, with {option['file']} as its book: the next download fetches it" if question["kind"] == "title"
                else f"keeps {option['file']} as its file: the next download fetches it, if it isn't there")
    if question["kind"] == "match" and option.get("answer") == "use":
        return f"uses {source_name(option.get('source', ''))}'s details for it from the next hylly catalog"
    if question["kind"] == "place" and option.get("answer") == "to":
        return f"puts your tags and corrections of it on {option.get('t') or option.get('id')}"
    return {("title", "keep"): "leaves it out, as the rules have it: not asked again while it offers the same files",
            ("moved", "moved"): "never fetches it again: it stays where you moved it",
            ("moved", "again"): "downloads it again whenever it is missing, as before: not asked again",
            ("which-file", "keep"): "keeps the file the rules took: asked again only if the title offers others",
            ("same", "same"): "makes them one card from the next hylly catalog, in every build",
            ("same", "apart"): "keeps them two cards: not asked again while they are the same files",
            ("match", "none"): "uses none of them, and those sources aren't asked about it again",
            ("place", "keep"): "keeps them waiting, should it come back: asked again only if other titles may be it",
            ("place", "forget"): "forgets them: they no longer wait for it, nor apply should it come back",
            ("files", "not"): "passes them over, as before: not asked again",
            ("files", "books"): "takes them as that folder's books from the next hylly catalog"}.get(
        (question["kind"], option.get("answer")), "keeps your answer")


def _made(row: dict, n: int) -> dict:
    """The edit answering a question with its option n makes: {field: file, value, format, titles}, a file correction;
    {field: answers, entries, titles}, a few of the titles' answers, by their questions' keys; or {op: reattach or forget,
    ids}, a title's tags and corrections put on another title or forgotten. titles: the titles it is on, each as edits
    name one (snapshot's shape), a shop's title by its catalog id; a folder of yours by folder_key."""
    kind, options = row["kind"], row["options"]
    option, cid, offered = options[n], catalog_id(row["shop"], row["item"]), fingerprint(options)
    title = {"id": cid, "t": row.get("title", ""), "f": ""}
    if kind == "which-file" and n == row.get("guess") and "file" in option:  # kept as the rules took it
        return {"field": "answers", "entries": {f"{kind}:{row['file']}": {"a": "keep", "o": offered, "f": option["file"]}},
                "titles": [title]}
    if "file" in option:
        return {"field": "file", "value": option["file"], "format": option.get("format") or "", "titles": [title]}
    code, subjects = option.get("answer"), [s for s in row.get("subjects") or [] if isinstance(s, dict) and s.get("id")]
    every = [{"id": s["id"], "t": s.get("t", ""), "f": s.get("f", "")} for s in subjects] or [title]
    if kind == "same":
        sides = {s["id"]: [s.get("name", ""), s.get("size", 0)] for s in subjects}
        return {"field": "catalog", "entries": {f"same:{row['item']}": {"a": code, "o": offered, "w": sides}}, "titles": every}
    if kind == "match":
        chosen = [option] if code == "use" else [o for o in options if o.get("answer") == "use"]
        return {"field": "catalog", "titles": every, "entries": {
            f"lookup:{o['source']}": {"a": "use" if code == "use" else "refused", "m": o.get("m", "")} for o in chosen}}
    if kind == "place":
        if code == "to":
            return {"op": "reattach", "ids": {row["item"]: option["id"]}}
        if code == "forget":
            return {"op": "forget", "ids": [row["item"]]}
        return {"field": "catalog", "entries": {"place:": {"a": "keep", "o": offered}}, "titles": [title]}
    if kind == "files":
        return {"field": "catalog", "entries": {f"files:{row['file']}": {"a": code}},
                "titles": [{"id": folder_key(row["item"]), "t": row.get("title", ""), "f": ""}]}
    return {"field": "answers", "entries": {f"{kind}:{row['file']}": {"a": code, "o": offered}}, "titles": [title]}


def _since(given: dict) -> tuple[int, int]:
    """Where in the order of the edits an answer's own came (its "after": how many there were before it), for
    Edits.chain: only a reattach from there on can have moved it. Edits.OWN, every reattach, for an answer that kept
    none."""
    after = given.get("after")
    return (after, -1) if isinstance(after, int) and not isinstance(after, bool) else Edits.OWN


def _held(edits: Edits, made: dict, title_id: str, entry: tuple[str, dict] | None = None) -> bool:
    """Whether an id holds what an answer's edit (made: a file chosen, or a title's answers) put on its title: the file,
    or each of its answers (or entry, one of them) as given."""
    fixed = edits.fixed.get(title_id, {})
    if made.get("field") == "file":
        return fixed.get("file") == made["value"]
    held = fixed.get(made.get("field")) or {}
    return all(((held.get(k) or {}).get("a"), (held.get(k) or {}).get("m")) == (v.get("a"), v.get("m"))
               for k, v in ([entry] if entry else (made.get("entries") or {}).items()))


def _now_id(edits: Edits, title_id: str, made: dict | None = None, since: tuple[int, int] = Edits.OWN) -> str:
    """The id an answer's edit on a title (made) is under now: of the id and those the reattaches made after it moved
    its edits to (Edits.chain), the first that holds it (_held), else the last. An id may have been reattached before
    the answer was made (a title gone, and back again), and then the answer is on the id itself, not where that
    reattach led."""
    ids = edits.chain(title_id, since)
    return next((i for i in ids if _held(edits, made, i)), ids[-1]) if made else ids[-1]


def stands(row: dict, edits: Edits) -> bool:
    """Whether an answered question's answer stands: the edit it made is still in effect, under whatever id its title
    has now (not taken back, with hylly decide NUMBER auto, hylly set ... auto, or the page's Undo). An answer from before
    this version kept no account of its edit: worked out from its option."""
    given = row.get("answer") if isinstance(row.get("answer"), dict) else {}
    made = given.get("made")
    if not isinstance(made, dict):
        n = given.get("option")
        if not isinstance(n, int) or not 0 < n <= len(row.get("options") or []):
            return False
        made = _made(row, n - 1)
    if made.get("op") == "reattach":
        return all(edits.moved.get(old, ("",))[0] == new for old, new in made["ids"].items())
    if made.get("op") == "forget":
        return all(old in edits.forgotten for old in made["ids"])
    ids = [i for t in made.get("titles") or [] for i in edits.chain(t["id"], _since(given))]  # wherever its edit may be
    if made.get("field") == "file":
        return any(_held(edits, made, i) for i in ids)
    return all(any(_held(edits, made, i, (k, v)) for i in ids) for k, v in (made.get("entries") or {}).items())


def _keep_answers(root: Path, field: str, entries_by_title: dict[str, dict], titles: list[dict], by: str) -> None:
    """Each title's answers of the field (answers, the downloads'; catalog, the catalog's) changed by the entries given
    for it (None takes one away): a title alone as a whole title's answers (op set, as an answer always was), several at
    once in as few edits as their entries allow (op update)."""
    if len(titles) == 1:
        t = titles[0]
        had = dict(load(root).fixed.get(t["id"], {}).get(field) or {})
        now = {k: v for k, v in {**had, **entries_by_title[t["id"]]}.items() if v is not None}
        change_single(root, field, now or "auto", [{**t, "f": [[t.get("f") or ""]]}], by)
        return
    groups: dict[str, list[dict]] = {}
    for t in titles:
        groups.setdefault(json.dumps(entries_by_title[t["id"]], sort_keys=True), []).append(t)
    for key, group in groups.items():
        change_answers(root, json.loads(key), [{**t, "f": [[t.get("f") or ""]]} for t in group], by, field=field)


def _make(root: Path, made: dict, by: str) -> dict:
    """The edit an answer makes (_made); what was there before it, for take_back to put back exactly: each title's
    answers under the keys it changes, or the file chosen before."""
    if "op" in made:
        _append(root, {"at": db.now(), "by": by, "op": made["op"], "ids": made["ids"]})
        return {}
    edits, titles = load(root), made["titles"]
    if made["field"] == "file":  # the file chosen before, with the format its option had: put back in its place
        i = titles[0]["id"]
        before = {"file": edits.fixed.get(i, {}).get("file"), "format": edits.file_format.get(i)}
        change_single(root, "file", made["value"], [{**titles[0], "f": [[""]]}], by,
                      also={"format": made["format"]} if made.get("format") else None)
        return before
    field = made["field"]
    before = {t["id"]: {k: (edits.fixed.get(t["id"], {}).get(field) or {}).get(k) for k in made["entries"]} for t in titles}
    _keep_answers(root, field, {t["id"]: made["entries"] for t in titles}, titles, by)
    return {"answers": before}


def answer(root: Path, number, choice, by: str, dry_run: bool = False, offered: str | None = None) -> dict:
    """Answers an open question (questions: its id, number) with one of its options: by its number from 1, by a file
    option's name, or "keep" for the rules' guess; by the edit that settles it (_made): an option with a file, a file
    correction (hylly set file); on a which-file question the guess's file, kept as the rules took it (its code keep with
    its file, in the title's answers, which asks again should the title offer other files); a catalog's question's, as
    its kind says (two copies one card or two, a match used or refused, a title's tags put on another or forgotten, a
    folder's files books or not); any other, its code in the title's answers. "auto": an answered question's answer
    taken back (take_back). offered (what hylly decide --json and the server page give as offered): refused, nothing
    made, when the question offers other options now. The question's row keeps the answer too, with the edit it made:
    the answered list, while it stands. What was done: the question, the option, the correction's field (or the op),
    the title's catalog id and in words what it did. dry_run: nothing made. A number or a choice that can't be one, a
    mistake (Exit USAGE), never a crash."""
    if isinstance(number, bool) or not isinstance(number, int) or not 0 < number < NUMBER_MOST:
        raise Exit(USAGE, f"{str(number)[:20]}? A question's number, as hylly decide lists them.")
    if not isinstance(choice, (str, int)) or isinstance(choice, bool) or len(str(choice)) > FILE_NAME_MOST:
        raise Exit(USAGE, "An answer is an option's number, a file's name or keep.")
    said = str(choice).strip()
    if said.lower() == "auto":
        return take_back(root, number, by, dry_run=dry_run)
    with store(root).read() as c:
        row = next(iter(_rows(c, "WHERE seq = ?", (number,))), None)
    if row is not None and row.get("answer") is not None and stands(row, load(root)):
        row = None  # answered, and its answer stands: hylly decide NUMBER auto takes it back first
    if row is None:
        raise Exit(USAGE, f"No question {number} is open: hylly decide lists those that are.")
    options = row["options"]
    if offered is not None and offered != fingerprint(options):
        raise Exit(NEEDS_YOU, f"Question {number} offers other options now than those your answer was for: nothing changed. "
                              "Look at it again (hylly decide, or reload the page) and answer anew.")
    if said.lower() == "keep":
        n = row.get("guess")
    elif said.isdecimal() and len(said) <= 6:
        n = int(said) - 1 if 1 <= int(said) <= len(options) else None
    else:  # a file's name, as listed: as written, else in letters of either size
        names = [o.get("file") for o in options]
        n = names.index(said) if said in names else next((k for k, name in enumerate(names) if isinstance(name, str)
                                                           and name.lower() == said.lower()), None)
    if n is None or not 0 <= n < len(options):
        raise Exit(USAGE, f"Answer question {number} with the number of one of its options, 1 to {len(options)}, a file's "
                          "name among them, or keep for the rules' guess.")
    option, cid, made = options[n], catalog_id(row["shop"], row["item"]), _made(row, n)
    if made.get("field") == "answers" and any(v.get("a") == "keep" and "f" in v for v in made["entries"].values()):
        option = {**option, "answer": "keep"}  # a which-file question's guess, kept as the rules took it
    if not dry_run:
        # the answer's edit and its question's row in one change (_make's writes join it); after: the edits before it,
        # as only a later reattach can move it (_since)
        with store(root).write() as c:
            after = c.execute("SELECT count(*) FROM edit").fetchone()[0]
            before = _make(root, made, by)
            c.execute("UPDATE question SET answer = ? WHERE seq = ?",
                      (db.dump({"option": n + 1, "by": by, "at": db.now(),
                                "made": made, "before": before, "after": after}), number))
    shown = _shown(row)
    return {"question": shown, "option": option, "field": made.get("field") or made["op"], "id": cid,
            "did": did(shown, option if "file" not in option or option.get("answer") != "keep" else {"answer": "keep"})}


def take_back(root: Path, number, by: str, dry_run: bool = False) -> dict:
    """An answered question's answer taken back alone (hylly decide NUMBER auto, the Server page's Take back): the edit
    that puts back exactly what was there before it, under whatever id its title has now (a title's answers under the
    keys it changed, the file chosen before, a reattach detached, what was forgotten brought back). The question is open
    again until the next run or build, which asks it again where the rules are still unsure. What was done: the
    question, and in words what it did."""
    with store(root).read() as c:
        row = next(iter(_rows(c, "WHERE seq = ? AND answer IS NOT NULL", (number,))), None)
    if row is None:
        raise Exit(USAGE, f"No question {number} has an answer to take back: hylly decide --answered lists those that have.")
    given = row["answer"] if isinstance(row["answer"], dict) else {}
    n = given.get("option")
    made = given.get("made") if isinstance(given.get("made"), dict) else (
        _made(row, n - 1) if isinstance(n, int) and 0 < n <= len(row["options"]) else None)
    if made is None:
        raise Exit(USAGE, f"Question {number}'s answer can't be told from what is kept of it: hylly set answers auto --id "
                          f"{catalog_id(row['shop'], row['item'])} takes back that title's answers.")
    before = given.get("before") if isinstance(given.get("before"), dict) else {}
    edits = load(root)
    if not dry_run and stands(row, edits):
        if made.get("op") in ("reattach", "forget"):
            _append(root, {"at": db.now(), "by": by,
                           "op": "detach" if made["op"] == "reattach" else "unforget", "ids": made["ids"]})
        elif made["field"] == "file":  # back exactly: the file before, with its format, in its place (an undo)
            t = made["titles"][0]
            change_single(root, "file", before.get("file") or "auto",
                          [{**t, "id": _now_id(edits, t["id"], made, _since(given)), "f": [[""]]}], by,
                          undo=True, also={"format": before["format"]} if before.get("format") else None)
        else:
            had = before.get("answers") or {}
            titles = [{**t, "id": _now_id(edits, t["id"], made, _since(given))} for t in made["titles"]]
            _keep_answers(root, made["field"], {now["id"]: {k: (had.get(t["id"]) or {}).get(k) for k in made["entries"]}
                                                for t, now in zip(made["titles"], titles)}, titles, by)
    if not dry_run:
        with store(root).write() as c:
            c.execute("UPDATE question SET answer = NULL WHERE seq = ?", (number,))
    return {"question": _shown(row), "option": {"word": "taken back", "answer": "auto"},
            "field": made.get("field") or made["op"], "id": catalog_id(row["shop"], row["item"]),
            "did": "takes your answer back: the next run or hylly catalog asks again where the rules are still unsure"}
