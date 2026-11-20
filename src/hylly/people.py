"""What the server remembers about a named person: where they are in each title, what they've finished, their lists,
bookmarks, shelves, reviews and reports, and small things such as the theme they picked.

It's kept in the database (db.py, migrations/0001_start.sql), a table for each kind. The rules of what a change does
stay here in Python: a person's things are read into memory, a change goes through the rules there, and what it changed
is written in the same transaction.

- Each change is in the database before the answer goes out.
- A change holds the person's lock and the database's write lock, and first reads the person again if anyone else (a
  command, say) has changed them meanwhile: their row's rev counts their changes.
- A database of a newer version of Hylly is left alone: nothing is read from it or written to it, and the owner is told.
- What one person can add has limits: lists, bookmarks and shelves by their number, places and finished marks by
  keeping the latest HISTORY_KEPT of each title, and everything by how many changes a minute the server takes from
  them (Pace), so neither reading for years nor a script with a login grows the database and its nightly copies
  without end.

Every night the server copies the database to the library's disk (dbcopy.py, db.nightly).
"""

from __future__ import annotations

import collections
import contextlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from . import access, db, reviews, shelves
from .db import Shape, dump
from .matching import Titles

READING_LIST = "reading"  # always there, and can't be renamed or deleted
GUIDES = ("book", "pages")  # the reader's first-time guide comes in two: for e-books, and for what comes as pages, comics and PDFs
PLACES_KEPT = 5  # of each title, in memory: the latest, and the way back from it
HISTORY_KEPT = 20  # of each title, in the database: the latest places, and the latest changes of its finished mark
WRITES_PER_MINUTE = 120  # of one person's changes, all their devices together (Pace)
MAX_ON_A_LIST = 2000
MAX_LISTS = 50  # of their own, besides the reading list
LIST_NAME_MAX = 60
LIST_UNDO_DAYS = 1  # a deleted list is kept this long, for its Undo (restore_list), and goes for good at their next change after
MAX_BOOKMARKS = 2000
BOOKMARK_REASONS = ("inspiring", "clever", "useful", "funny", "infuriating")  # kept by name: the page has their words and signs, which can change

Told = Callable[[str, str], None]  # a person's name, and what happened to what's kept of them

TITLE = (("id", "title", "text"), ("t", "title_t", "text"), ("f", "title_f", "text"))
SPOT = (("at", "at", "json"), ("pct", "pct", "real"), ("file", "file", "text"))
WHEN = (("when", "local_time", "text"), ("time", "time", "real"))
PLACE = Shape("place", (*TITLE, *SPOT, ("device", "device", "text"), *WHEN, ("of", "pages", "int"), ("note", "note", "text")), op="place")
FINISHED = Shape("finished", (*TITLE, ("done", "done", "bool"), *WHEN), op="finished")
GUIDE = Shape("guide_seen", (("kind", "kind", "text"), *WHEN), op="guide-seen")
ENTRY = Shape("list_entry", (*TITLE[:3], ("added", "added", "text")))
BOOKMARK = Shape("bookmark", (("key", "key", "text"), *TITLE, ("why", "why", "text"), *SPOT, ("of", "pages", "int"), ("page", "page", "int"),
                              ("part", "part", "int"), ("inside", "inside", "real"), ("note", "note", "text"), ("text", "text", "text"),
                              ("word", "word", "text"), *WHEN))
REPORT = Shape("report", (*TITLE, ("reasons", "reasons", "json"), ("comment", "comment", "text"), *WHEN))


class ListFull(Exception):
    """A list holds MAX_ON_A_LIST titles."""


class ListsFull(Exception):
    """A person keeps MAX_LISTS lists of their own."""


class NoSuchList(Exception):
    """A list they don't have, perhaps deleted on another device."""


class BookmarksFull(Exception):
    """A person keeps MAX_BOOKMARKS bookmarks."""


class NotAllowed(Exception):
    """A change the rules don't allow where it stands (reviews.Refused), said in words."""


class Newer(Exception):
    """The database is from a newer version of Hylly, or (older) from a test version before its first release that this
    version doesn't take on (db.Older): it's left alone."""

    def __init__(self, name: str = "", older: bool = False) -> None:
        super().__init__(name)
        self.older = older


def _seconds(when: str) -> float:
    """A record's time (db.moment reads it), in seconds since 1970; one that can't be read is never past (inf)."""
    try:
        return db.moment(when).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def _under_its_id(record: dict, titles: Titles | None) -> dict:
    """Something kept of a title's (a place, a finished mark, a list's entry, a bookmark, a review, a report), under the
    id its title has now. With titles: one kept by an id no title has any more goes by the title that has taken over,
    if one has (titles.find), and is marked gone if none has. The one way every reader of them follows a title that
    moved or merged, so that lists, bookmarks and reviews never disagree about the same title."""
    now = record["id"]
    if titles is not None and now not in titles:
        now = titles.find(record) or now
    return {**record, "id": now, **({"gone": True} if titles is not None and now not in titles else {})}


def _kept_by(kept: dict[str, dict], ids: list[str], titles: Titles | None) -> str | None:
    """The id a person's review or report of a title is kept by, in kept (theirs, by id): one of its ids (a merged
    title's copies), or one it no longer has, found by titles."""
    for title_id in kept:
        if title_id in ids:
            return title_id
    if titles is not None:
        for title_id, r in kept.items():
            if title_id not in titles and titles.find(r) in ids:
                return title_id
    return None


def store(where: Path | None = None) -> db.Database:
    """The database: the data folder's, unless named."""
    return db.database(where)


class Person:
    def __init__(self, name: str, told: Told | None = None, where: Path | None = None) -> None:
        self.name = name
        self._told = told or (lambda name, what: None)
        self._where = where  # the database: the data folder's, unless named
        self._lock = threading.RLock()
        self._seen: tuple | None | object = object()  # their row and its rev, as last read: nothing yet
        self._pid: int | None = None
        self._wipe = False  # words were taken away: the WAL goes too, once the change is in
        self.newer = False
        self.older = False  # the database left alone is one from before the first release (db.Older)
        self._clear()

    def _clear(self) -> None:
        self._places: dict[str, list[dict]] = {}  # the latest PLACES_KEPT of each title
        self._marks: dict[str, dict] = {}  # the latest "finished" of each title
        self._new_seen: dict = {}  # how far they've been shown what's new in the library
        self._guides: set[str] = set()  # which of the reader's guides they've had
        self._look: str = ""  # the theme they picked (themes.py); "": the library's own; themes.OWN: their own
        self._own: dict | None = None  # their own theme's colours, over the theme it was made from (themes.own_theme)
        self._lists: dict[str, list[dict]] = {}  # each list's titles, in order: the reading list, and their own by their ids
        self._list_names: dict[str, str] = {}  # their own lists, by id, in their order
        self._dropped: dict[str, tuple[str, list[dict]]] = {}  # deleted, for Undo
        self._apps: set[str] = set()  # their lists ticked for the reading apps' feeds, deleted ones among them
        self._featured: str | None = None  # theirs that's the library's Featured, if one is
        self._bookmarks: dict[str, dict] = {}  # by each one's own key, the oldest first
        self._shelves: dict[str, dict] = {}  # their own, by id, in the order made
        self._opens: str = ""  # the shelf they open with: a pointer, to one of theirs or one for everyone
        self._reviews: dict[str, dict] = {}  # what they've said of each title, by the id it was given (reviews.step)
        self._reports: dict[str, dict] = {}  # their report of each title's file, by the id it was given (reports.py)

    # ---- the database ----
    def _usable(self) -> db.Database | None:
        """The database, or None if it's from a newer version of Hylly (told of, once), which opening it says."""
        try:
            found = store(self._where)
            with found.read():
                return found
        except db.Newer as e:
            if not self.newer:
                self.newer = True
                self._told(self.name, str(e))
            self.older = isinstance(e, db.Older)  # for the words of a change refused (_changing)
            self._clear()
            self._seen = object()
            return None

    def _fresh(self, c: sqlite3.Connection) -> None:
        """What's in memory, as the database now has it: a command may have changed it, or set them aside."""
        row = c.execute("SELECT id, rev FROM person WHERE name = ? AND removed IS NULL", (self.name,)).fetchone()
        now = (row["id"], row["rev"]) if row else None
        if now != self._seen:
            self._load(c, row["id"] if row else None)
            self._seen = now

    @contextlib.contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection | None]:
        """The lock held, and what's in memory as the database has it. None for a database of a newer version."""
        with self._lock:
            found = self._usable()
            if found is None:
                yield None
                return
            with found.read() as c:
                self._fresh(c)
                yield c

    @contextlib.contextmanager
    def _changing(self) -> Iterator[sqlite3.Connection]:
        """For a change: the person's lock and the database's write lock held, and what's in memory as it has it."""
        with self._lock:
            found = self._usable()
            if found is None:
                raise Newer(self.name, self.older)
            try:
                with found.write() as c:
                    self._fresh(c)
                    self._drop_deleted(c)
                    yield c
            except BaseException:
                self._seen = object()  # read again next time: memory may be ahead of what was kept
                raise
            if self._wipe:
                self._wipe = False
                found.checkpoint()

    def _me(self, c: sqlite3.Connection) -> int:
        """Their row, made at their first change."""
        if self._seen is None:
            self._pid = c.execute("INSERT INTO person (name, since) VALUES (?, ?)", (self.name, db.now())).lastrowid
            self._seen = (self._pid, 0)
        return self._pid

    def _changed(self, c: sqlite3.Connection) -> None:
        """One more change of theirs, for whoever has them in memory."""
        c.execute("UPDATE person SET rev = rev + 1 WHERE id = ?", (self._pid,))
        self._seen = (self._pid, self._seen[1] + 1)

    def _drop_deleted(self, c: sqlite3.Connection) -> None:
        """Their lists deleted more than LIST_UNDO_DAYS ago go for good, with their titles, in the change they make now.
        The page's Undo lasts seconds, but a device offline in between sends it later: a day is long past that, as a
        delete and its Undo are both dated when they reach the server. Their names and titles go from the database's file
        too, as words taken away do."""
        if not self._dropped:
            return
        before = time.time() - LIST_UNDO_DAYS * 86400
        gone = [row["id"] for row in c.execute("SELECT id, deleted FROM list WHERE person = ? AND deleted IS NOT NULL", (self._pid,))
                if _seconds(row["deleted"]) < before]
        for list_id in gone:
            c.execute("DELETE FROM list WHERE person = ? AND id = ?", (self._pid, list_id))  # its titles go with it (list_entry)
            self._dropped.pop(list_id, None)
            self._apps.discard(list_id)
            if self._featured == list_id:
                self._featured = None
        if gone:
            self._changed(c)
            self._wipe = True

    def _load(self, c: sqlite3.Connection, pid: int | None) -> None:
        self._clear()
        self._pid = pid
        if pid is None:
            return
        me = c.execute("SELECT * FROM person WHERE id = ?", (pid,)).fetchone()
        self._new_seen = json.loads(me["new_seen"]) if me["new_seen"] else {}
        self._look, self._opens = me["look"], me["opens"]
        try:
            self._own = json.loads(me["own_look"]) if me["own_look"] else None
        except ValueError:
            self._own = None
        for row in c.execute("SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY title ORDER BY seq DESC) AS n FROM place "
                             "WHERE person = ?) WHERE n <= ? ORDER BY seq", (pid, PLACES_KEPT)):
            record = PLACE.record(row)
            self._places.setdefault(record["id"], []).append(record)
        for row in c.execute("SELECT * FROM finished WHERE seq IN (SELECT max(seq) FROM finished WHERE person = ? GROUP BY title) ORDER BY seq", (pid,)):
            record = FINISHED.record(row)
            self._marks[record["id"]] = record
        self._guides = {row["kind"] for row in c.execute("SELECT kind FROM guide_seen WHERE person = ?", (pid,))}
        for row in c.execute("SELECT * FROM list WHERE person = ? ORDER BY made", (pid,)).fetchall():
            entries = [ENTRY.record(e) for e in c.execute("SELECT * FROM list_entry WHERE person = ? AND list = ? ORDER BY position", (pid, row["id"]))]
            if row["apps"]:
                self._apps.add(row["id"])
            if row["featured"]:
                self._featured = row["id"]
            if row["name"] is None:
                self._lists[READING_LIST] = entries
            elif row["deleted"] is not None:
                self._dropped[row["id"]] = (row["name"], entries)
            else:
                self._list_names[row["id"]], self._lists[row["id"]] = row["name"], entries
        for row in c.execute("SELECT * FROM bookmark WHERE person = ? ORDER BY seq", (pid,)):
            mark = BOOKMARK.record(row)
            self._bookmarks[mark["key"]] = mark
        for row in c.execute("SELECT id, body FROM shelf WHERE person = ? ORDER BY seq", (pid,)):
            self._shelves[row["id"]] = json.loads(row["body"])
        for row in c.execute("SELECT title, body FROM review WHERE person = ? ORDER BY seq", (pid,)):
            self._reviews[row["title"]] = json.loads(row["body"])
        for row in c.execute("SELECT * FROM report WHERE person = ? ORDER BY seq", (pid,)):
            report = REPORT.record(row)
            self._reports[report["id"]] = report

    # ---- the rules of each kind of change ----
    def _shelf_change(self, record: dict) -> None:
        op, shelf = record.get("op"), record.get("shelf")
        if op == "set" and isinstance(shelf, dict) and isinstance(shelf.get("id"), str):
            self._shelves[shelf["id"]] = shelf
        elif op == "delete":
            self._shelves.pop(record.get("id"), None)
        elif op == "open" and isinstance(record.get("id"), str):
            self._opens = record["id"]

    def _bookmark_change(self, record: dict) -> None:
        """One small change to the bookmarks. Each has a key of its own, made where it was marked, so that two devices can't
        overwrite each other's, and one sent twice counts once."""
        key, op = record.get("key"), record.get("op")
        if not isinstance(key, str):
            return
        if op == "add" and isinstance(record.get("id"), str):
            self._bookmarks.setdefault(key, {k: v for k, v in record.items() if k != "op"})
        elif op == "change" and key in self._bookmarks:
            self._bookmarks[key]["why"] = record.get("why")
        elif op == "remove":
            self._bookmarks.pop(key, None)

    def _list_change(self, record: dict) -> None:
        """One small change to a list. Small, so that two devices can't overwrite each other's order: what each did both stay."""
        name, op, title_id = record.get("list"), record.get("op"), record.get("id")
        if not isinstance(name, str):
            return
        if op == "name":  # made, or renamed
            self._list_names[name] = str(record.get("name", ""))
            self._lists.setdefault(name, [])
        elif op == "delete" and name in self._list_names:
            self._dropped[name] = (self._list_names.pop(name), self._lists.pop(name, []))
        elif op == "restore" and name in self._dropped:
            self._list_names[name], self._lists[name] = self._dropped.pop(name)
        if not isinstance(title_id, str) or (name != READING_LIST and name not in self._list_names):
            return
        titles = self._lists.setdefault(name, [])
        found = next((e for e in titles if e["id"] == title_id), None)
        if op == "add" and found is None:
            titles.append({"id": title_id, "t": record.get("t", ""), "f": record.get("f", ""), "added": record.get("when", "")})
        elif op == "remove" and found is not None:
            titles.remove(found)
        elif op == "move" and found is not None:
            titles.remove(found)
            at = next((i for i, e in enumerate(titles) if e["id"] == record.get("before")), len(titles))  # before that one, or last
            titles.insert(at, found)

    def _keep_list_change(self, c: sqlite3.Connection, record: dict) -> None:
        """What a list change did in memory (_list_change), into the database."""
        pid, name, op = self._me(c), record["list"], record["op"]
        if op == "name":
            c.execute("INSERT INTO list (person, id, name, made) VALUES (?, ?, ?, (SELECT coalesce(max(made), 0) + 1 FROM list WHERE person = ?)) "
                      "ON CONFLICT (person, id) DO UPDATE SET name = excluded.name", (pid, name, self._list_names[name], pid))
        elif op == "delete":
            c.execute("UPDATE list SET deleted = ? WHERE person = ? AND id = ?", (record["when"], pid, name))
        elif op == "restore":
            c.execute("UPDATE list SET deleted = NULL, made = (SELECT max(made) + 1 FROM list WHERE person = ?) WHERE person = ? AND id = ?", (pid, pid, name))
        elif op == "add":
            if name == READING_LIST:
                c.execute("INSERT OR IGNORE INTO list (person, id, name, made) VALUES (?, ?, NULL, 0)", (pid, name))
            last = c.execute("SELECT coalesce(max(position), 0) FROM list_entry WHERE person = ? AND list = ?", (pid, name)).fetchone()[0]
            ENTRY.insert(c, self._lists[name][-1], person=pid, list=name, position=last + 1)
        elif op == "remove":
            c.execute("DELETE FROM list_entry WHERE person = ? AND list = ? AND title = ?", (pid, name, record["id"]))
        elif op == "move":
            for position, entry in enumerate(self._lists[name], 1):
                c.execute("UPDATE list_entry SET position = ? WHERE person = ? AND list = ? AND title = ?", (position, pid, name, entry["id"]))
        self._changed(c)

    # ---- what the server asks ----
    def place(self, title: dict, at, pct: float, file: str = "", device: str = "", of: int = 0, note: str = "", age: float = 0.0) -> dict:
        """A place reached in a title (curation.snapshot of it): kept, unless it's where this device already was. of: a
        comic's or a PDF's pages; note: a book's chapter. age: how many seconds ago it was reached, by the sender's own
        measure, as a device that was offline sends late: a day-old place doesn't beat a newer one. The clock is the server's."""
        reached = time.time() - max(0.0, age)
        record = {"op": "place", "id": title["id"], "t": title.get("t", ""), "f": title.get("f", ""), "at": at, "pct": round(pct, 5),
                  "file": file, "device": device, "when": datetime.fromtimestamp(reached).astimezone().isoformat(timespec="seconds"),
                  "time": round(reached, 3), **({"of": of} if of else {}), **({"note": note} if note else {})}
        with self._changing() as c:
            mine = self._places.get(title["id"], [])
            if mine and mine[-1].get("at") == at and mine[-1].get("device") == device:
                return mine[-1]
            PLACE.insert(c, record, person=self._me(c))
            self._keep_latest(c, "place", title["id"])
            self._changed(c)
            mine = self._places.setdefault(title["id"], [])
            mine.append(record)
            del mine[:-PLACES_KEPT]
        return record

    def _keep_latest(self, c: sqlite3.Connection, table: str, title_id: str) -> None:
        """Of a title's places, or the changes of its finished mark, only the latest HISTORY_KEPT that came stay: the
        older go in the change that adds one. What memory goes by (the latest PLACES_KEPT places, the last mark) is
        always among them, so a server start reads back where they are as it was. The way back the page offers is
        the browser's own place, kept in the browser (catalog_page.html, placeHere): none of it hangs on older rows."""
        c.execute(f"DELETE FROM {table} WHERE person = ? AND title = ? AND seq NOT IN "
                  f"(SELECT seq FROM {table} WHERE person = ? AND title = ? ORDER BY seq DESC LIMIT ?)",
                  (self._pid, title_id, self._pid, title_id, HISTORY_KEPT))

    def mark(self, title: dict, done: bool, age: float = 0.0) -> dict:
        """A title finished, or no longer: kept if that's news. It stays finished through more reading, until it's
        unmarked or started over."""
        reached = time.time() - max(0.0, age)
        record = {"op": "finished", "id": title["id"], "t": title.get("t", ""), "f": title.get("f", ""), "done": done,
                  "when": datetime.fromtimestamp(reached).astimezone().isoformat(timespec="seconds"), "time": round(reached, 3)}
        with self._changing() as c:
            was = self._marks.get(title["id"])
            if bool(was and was.get("done")) == done:
                return was or record
            FINISHED.insert(c, record, person=self._me(c))
            self._keep_latest(c, "finished", title["id"])
            self._changed(c)
            self._marks[title["id"]] = record
        return record

    def change_list(self, name: str, op: str, title: dict, before: str | None = None, ids: list[str] | None = None, titles: Titles | None = None) -> bool:
        """add (at the bottom), remove or move (before another, or last) a title (curation.snapshot of it). ids: all it is known
        by, a merged title's copies; titles: to find one whose id has changed. False if there was nothing to do."""
        ids = ids or [title["id"]]

        def stored(wanted: list[str]) -> str | None:
            for e in self._lists.get(name, []):
                if e["id"] in wanted or (titles is not None and e["id"] not in titles and titles.find(e) in wanted):
                    return e["id"]
            return None

        with self._changing() as c:
            if name != READING_LIST and name not in self._list_names:
                raise NoSuchList(name)
            there = stored(ids)
            if (op == "add") == (there is not None):
                return False  # on it already, or not on it to take off or move
            if op == "add" and len(self._lists.get(name, [])) >= MAX_ON_A_LIST:
                raise ListFull(name)
            record = {"op": op, "list": name, "id": there or title["id"], "when": db.now(),
                      **({"t": title.get("t", ""), "f": title.get("f", "")} if op == "add" else {}),
                      **({"before": stored([before]) or before} if op == "move" and before else {})}
            self._list_change(record)
            self._keep_list_change(c, record)
        return True

    def _list_itself(self, c: sqlite3.Connection, record: dict) -> bool:
        record = {**record, "when": db.now()}
        self._list_change(record)
        self._keep_list_change(c, record)
        return True

    def name_list(self, list_id: str, name: str) -> bool:
        """Make a list of their own, or give one another name. False if there was nothing to do: a device that didn't hear
        the answer sends it again."""
        with self._changing() as c:
            if list_id == READING_LIST or list_id in self._dropped:
                raise NoSuchList(list_id)
            if self._list_names.get(list_id) == name:
                return False
            if list_id not in self._list_names and len(self._list_names) >= MAX_LISTS:
                raise ListsFull(self.name)
            return self._list_itself(c, {"op": "name", "list": list_id, "name": name})

    def delete_list(self, list_id: str) -> bool:
        """It can come back by restore_list for LIST_UNDO_DAYS, and then goes for good (_drop_deleted)."""
        with self._changing() as c:
            return self._list_itself(c, {"op": "delete", "list": list_id}) if list_id in self._list_names else False

    def restore_list(self, list_id: str) -> bool:
        with self._changing() as c:
            return self._list_itself(c, {"op": "restore", "list": list_id}) if list_id in self._dropped else False

    def show_list(self, list_id: str, on: bool) -> bool:
        """A list of theirs in the reading apps' feeds, or not: an owner's for everyone (opds.py). Off, it's no longer
        the library's Featured either. False if there was nothing to do."""
        with self._changing() as c:
            if list_id not in self._list_names:
                raise NoSuchList(list_id)
            if (list_id in self._apps) == on:
                return False
            c.execute("UPDATE list SET apps = ?, featured = featured AND ? WHERE person = ? AND id = ?", (int(on), int(on), self._me(c), list_id))
            if on:
                self._apps.add(list_id)
            else:
                self._apps.discard(list_id)
                if self._featured == list_id:
                    self._featured = None
            self._changed(c)
        return True

    def feature_list(self, list_id: str, on: bool) -> bool:
        """A list of theirs as the library's Featured, ticked for the apps as well, in place of whichever was, anyone's; or
        not. Only an owner's shows (the server's say). False if there was nothing to do."""
        with self._changing() as c:
            if list_id not in self._list_names:
                raise NoSuchList(list_id)
            if (self._featured == list_id) == on:
                return False
            pid = self._me(c)
            if on:  # whoever had it learns at their next look: one more change of theirs
                c.execute("UPDATE person SET rev = rev + 1 WHERE id IN (SELECT person FROM list WHERE featured = 1 AND person != ?)", (pid,))
                c.execute("UPDATE list SET featured = 0 WHERE featured = 1")
                c.execute("UPDATE list SET apps = 1, featured = 1 WHERE person = ? AND id = ?", (pid, list_id))
                self._featured = list_id
                self._apps.add(list_id)
            else:
                c.execute("UPDATE list SET featured = 0 WHERE person = ? AND id = ?", (pid, list_id))
                self._featured = None
            self._changed(c)
        return True

    def list_names(self) -> list[dict]:
        """Their own lists, in their order, with apps and featured when they're ticked."""
        with self._reading():
            return [{"id": list_id, "name": name, **({"apps": True} if list_id in self._apps else {}),
                     **({"featured": True} if list_id == self._featured else {})} for list_id, name in self._list_names.items()]

    def lists(self, titles: Titles | None = None) -> dict[str, list[dict]]:
        """Each list's titles, in order. With titles: one whose id has changed goes by its new one, and one that has gone is marked so."""
        with self._reading():
            out: dict[str, list[dict]] = {}
            for name, entries in self._lists.items():
                seen, rows = set(), []
                for e in entries:
                    row = _under_its_id(e, titles)
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        rows.append(row)
                out[name] = rows
            return out

    def add_bookmark(self, title: dict, key: str, why: str, spot: dict, age: float = 0.0) -> bool:
        """A spot in a title (curation.snapshot of it), with a reason. spot: what the page needs to find it again and to show it
        in a list. age: how many seconds ago, by the sender's own measure. False if it's here already: a device that didn't hear
        the answer sends it again."""
        made = time.time() - max(0.0, age)
        record = {"op": "add", "key": key, "id": title["id"], "t": title.get("t", ""), "f": title.get("f", ""), "why": why, **spot,
                  "when": datetime.fromtimestamp(made).astimezone().isoformat(timespec="seconds"), "time": round(made, 3)}
        with self._changing() as c:
            if key in self._bookmarks:
                return False
            if len(self._bookmarks) >= MAX_BOOKMARKS:
                raise BookmarksFull(self.name)
            self._bookmark_change(record)
            BOOKMARK.insert(c, self._bookmarks[key], person=self._me(c))
            self._changed(c)
        return True

    def change_bookmark(self, key: str, why: str) -> bool:
        """Another reason for it. False if there was nothing to do: it's gone, or has that reason."""
        with self._changing() as c:
            found = self._bookmarks.get(key)
            if found is None or found.get("why") == why:
                return False
            self._bookmark_change({"op": "change", "key": key, "why": why})
            BOOKMARK.update(c, self._bookmarks[key], "person = ? AND key = ?", self._pid, key)
            self._changed(c)
        return True

    def remove_bookmark(self, key: str) -> bool:
        with self._changing() as c:
            if key not in self._bookmarks:
                return False
            self._bookmark_change({"op": "remove", "key": key})
            c.execute("DELETE FROM bookmark WHERE person = ? AND key = ?", (self._pid, key))
            self._changed(c)
        return True

    def bookmarks(self, titles: Titles | None = None) -> list[dict]:
        """All of them, the oldest first. With titles: one whose id has changed goes by its new one, and one that has gone is marked so."""
        with self._reading():
            return [_under_its_id(b, titles) for b in self._bookmarks.values()]

    def change_shelf(self, op: str, shelf: dict | None = None, shelf_id: str = "") -> bool:
        """set a shelf (shelves.clean of it), delete one by its id, or open with one, or with none. False if there was nothing to do."""
        with self._changing() as c:
            if op == "set":
                if self._shelves.get(shelf["id"]) == shelf:
                    return False
                if shelf["id"] not in self._shelves and len(self._shelves) >= shelves.MAX_SHELVES:
                    raise shelves.Full
                record = {"op": op, "shelf": shelf}
            elif (shelf_id not in self._shelves) if op == "delete" else (shelf_id == self._opens):
                return False
            else:
                record = {"op": op, "id": shelf_id}
            pid = self._me(c)
            self._shelf_change(record)
            if op == "set":
                c.execute("INSERT INTO shelf (person, id, body) VALUES (?, ?, ?) ON CONFLICT (person, id) DO UPDATE SET body = excluded.body",
                          (pid, shelf["id"], dump(shelf)))
            elif op == "delete":
                c.execute("DELETE FROM shelf WHERE person = ? AND id = ?", (pid, shelf_id))
            else:
                c.execute("UPDATE person SET opens = ? WHERE id = ?", (self._opens, pid))
            self._changed(c)
        return True

    def shelves(self) -> tuple[list[dict], str]:
        """Their own, in the order made, and the one they open with."""
        with self._reading():
            return list(self._shelves.values()), self._opens

    def _review_event(self, c: sqlite3.Connection, event: dict, before: dict | None, forget: bool = False) -> dict | None:
        """One event, through the rules, and the review it makes into the database and memory: only the review as it
        now stands is kept, not the event. forget: words were taken away, so they must be gone from the database's file
        too, not only from what's shown (secure_delete overwrites them, and the WAL is emptied once the change is in).
        Raises NotAllowed where the rules (reviews.STEPS) don't allow it."""
        try:
            after = reviews.step(before, event)
        except reviews.Refused as e:
            raise NotAllowed(str(e)) from None
        pid, title_id = self._me(c), event["id"]
        if after is None:
            self._reviews.pop(title_id, None)
            c.execute("DELETE FROM review WHERE person = ? AND title = ?", (pid, title_id))
        else:
            self._reviews[title_id] = after
            c.execute("INSERT INTO review (person, title, body) VALUES (?, ?, ?) ON CONFLICT (person, title) DO UPDATE SET body = excluded.body",
                      (pid, title_id, dump(after)))
        if forget:
            self._wipe = True
        self._changed(c)
        return after

    def _stamp(self, op: str, title_id: str, title: dict | None = None, **more) -> dict:
        when = time.time()
        return {"op": op, "id": title_id, **({"t": title.get("t", ""), "f": title.get("f", "")} if title else {}), **more,
                "when": db.now(), "time": round(when, 3)}

    def rate(self, title: dict, stars: int, ids: list[str] | None = None, titles: Titles | None = None) -> bool:
        """Stars for a title (curation.snapshot of it), or 0 to take theirs back. ids: all it is known by; titles: to find
        theirs by an id it no longer has. False if there was nothing to do: a device that didn't hear the answer sends it
        again. Raises NotAllowed where the rules (reviews.STEPS) don't allow it: stars taken back from a review with words."""
        with self._changing() as c:
            kept_by = _kept_by(self._reviews, ids or [title["id"]], titles)
            before = self._reviews.get(kept_by) if kept_by else None
            if (before or {}).get("stars", 0) == stars:
                return False
            event = self._stamp("unrate", kept_by) if not stars else self._stamp("rate", kept_by or title["id"], title, stars=stars)
            self._review_event(c, event, before)
        return True

    def write(self, title: dict, stars: int, text: str, spoiler: bool, wait: bool, ids: list[str] | None = None, titles: Titles | None = None) -> dict | None:
        """A review's words (reviews.clean of them) with its stars: out at once, or waiting for an owner (wait). Words of
        nothing take the words away and keep the stars. The review as it now stands, or None if it was so already: a device
        that didn't hear the answer sends it again. Raises NotAllowed where the rules don't allow it."""
        with self._changing() as c:
            kept_by = _kept_by(self._reviews, ids or [title["id"]], titles)
            before = self._reviews.get(kept_by) if kept_by else None
            title_id = kept_by or title["id"]
            if not text:
                if before and before.get("state") in reviews.WORDS:
                    before = self._review_event(c, self._stamp("unwrite", title_id), before, forget=True)
                if (before or {}).get("stars", 0) == stars:
                    return before
                return self._review_event(c, self._stamp("rate", title_id, title, stars=stars), before)
            if before and before.get("stars") == stars and (  # sent again: the same words, the same way
                    (before.get("state") in (reviews.SHOWN, reviews.WAITING) and before.get("text") == text and bool(before.get("spoiler")) == spoiler
                     and (before["state"] == reviews.WAITING) == wait)
                    or (before.get("state") == reviews.EDIT_WAITING and wait and before.get("change") == text and bool(before.get("change_spoiler")) == spoiler)):
                return None
            event = self._stamp("submit" if wait else "write", title_id, title, stars=stars, text=text, spoiler=spoiler)
            return self._review_event(c, event, before)

    def delete_review(self, ids: list[str], titles: Titles | None = None) -> bool:
        """Their review of a title, stars and words, gone: from the database's file too. False if there was none."""
        with self._changing() as c:
            kept_by = _kept_by(self._reviews, ids, titles)
            if not kept_by:
                return False
            self._review_event(c, self._stamp("delete", kept_by), self._reviews[kept_by], forget=True)
        return True

    def check_review(self, title_id: str, op: str, rev: int | None, note: str, by: str) -> dict | None:
        """An owner's word on this person's review of a title, by the id it's kept by: release, decline (with a note only
        the writer and owners see) or remove its words. rev: the version the owner saw. The review as it now stands.
        Raises NotAllowed where the rules don't allow it, as for words that have changed since."""
        with self._changing() as c:
            before = self._reviews.get(title_id)
            if before is None:
                raise NotAllowed("there's no such review any more")
            return self._review_event(c, self._stamp(op, title_id, rev=rev, note=note, by=by), before, forget=op == "remove")

    def report(self, title: dict, reasons: list[str], comment: str, ids: list[str] | None = None, titles: Titles | None = None) -> dict | None:
        """What's wrong with a title's file (reports.clean of it), in place of their earlier report on it. The report, or
        None if it said the same already: a device that didn't hear the answer sends it again."""
        with self._changing() as c:
            kept_by = _kept_by(self._reports, ids or [title["id"]], titles)
            before = self._reports.get(kept_by) if kept_by else None
            if before and before.get("reasons") == reasons and before.get("comment") == comment:
                return None
            record = self._stamp("report", kept_by or title["id"], title, reasons=reasons, comment=comment)
            report = {k: v for k, v in record.items() if k != "op"}
            pid = self._me(c)
            if record["id"] in self._reports:
                REPORT.update(c, report, "person = ? AND title = ?", pid, record["id"])
            else:
                REPORT.insert(c, report, person=pid)
            self._reports[record["id"]] = report
            self._changed(c)
            return report

    def unreport(self, ids: list[str], titles: Titles | None = None) -> bool:
        """Their report of a title taken back. False if there was none."""
        with self._changing() as c:
            kept_by = _kept_by(self._reports, ids, titles)
            if not kept_by:
                return False
            self._reports.pop(kept_by, None)
            c.execute("DELETE FROM report WHERE person = ? AND title = ?", (self._pid, kept_by))
            self._changed(c)
        return True

    def reports(self, titles: Titles | None = None) -> list[dict]:
        """Their reports. With titles: one whose id has changed goes by its new one, and one that has gone is marked so."""
        with self._reading():
            return [_under_its_id(r, titles) for r in self._reports.values()]

    def reviews(self, titles: Titles | None = None) -> list[dict]:
        """What they've said of titles. With titles: one whose id has changed goes by its new one, and one that has gone is marked so."""
        with self._reading():
            return [_under_its_id(r, titles) for r in self._reviews.values()]

    def new_seen(self) -> str:
        """The date of the newest title they've been shown as new. One point, not a list: titles come in batches, and it
        keeps being one short value however many there are."""
        with self._reading():
            return str(self._new_seen.get("upto", ""))

    def saw_new(self, upto: str) -> None:
        """Shown as new, up to this date: not again, on any device. It only ever moves on."""
        with self._changing() as c:
            if upto <= str(self._new_seen.get("upto", "")):
                return
            record = {"op": "new-seen", "upto": upto, "when": db.now(), "time": round(time.time(), 3)}
            c.execute("UPDATE person SET new_seen = ? WHERE id = ?", (dump(record), self._me(c)))
            self._changed(c)
            self._new_seen = record

    def guides(self) -> list[str]:
        """Which of the reader's guides they've had."""
        with self._reading():
            return sorted(self._guides)

    def saw_guide(self, kind: str) -> None:
        """Had this guide: not again, on any device."""
        with self._changing() as c:
            if kind in self._guides:
                return
            GUIDE.insert(c, {"op": "guide-seen", "kind": kind, "when": db.now(), "time": round(time.time(), 3)}, person=self._me(c))
            self._changed(c)
            self._guides.add(kind)

    def look(self) -> str:
        """The theme they picked, on any device: "" for the library's own."""
        with self._reading():
            return self._look

    def own_look(self) -> dict | None:
        """Their own theme's colours, {"base", "light", "dark"}, or None."""
        with self._reading():
            return self._own

    def pick_look(self, theme_id: str, own=...) -> bool:
        """A theme picked, or "" for the library's own: the same on every device. With own, their own theme's colours
        too, already checked (themes.own_theme); None forgets them. False if nothing changed."""
        with self._changing() as c:
            if theme_id == self._look and (own is ... or own == self._own):
                return False
            if own is ...:
                c.execute("UPDATE person SET look = ? WHERE id = ?", (theme_id, self._me(c)))
            else:
                c.execute("UPDATE person SET look = ?, own_look = ? WHERE id = ?", (theme_id, dump(own) if own else None, self._me(c)))
                self._own = own or None
            self._changed(c)
            self._look = theme_id
        return True

    def start_over(self, title: dict, device: str = "", age: float = 0.0) -> dict:
        """Not finished, and at its start, on every device. The places before it stay, as many as HISTORY_KEPT keeps."""
        self.mark(title, False, age)
        return self.place(title, 0, 0.0, "", device, age=age)

    def latest(self, titles: Titles | None = None) -> dict[str, dict]:
        """Where they are in each title, the latest place of each. With titles: a place of an id no title has any more
        goes with the title that has taken over, if one has, and is marked gone if none has."""
        with self._reading():
            out: dict[str, dict] = {}
            for records in self._places.values():
                record = max(reversed(records), key=lambda r: r.get("time", 0))  # the one reached last, whichever came in last
                found = _under_its_id(record, titles)
                now = found["id"]
                if now not in out or found.get("time", 0) > out[now].get("time", 0):
                    out[now] = found
            for title_id, mark in self._marks.items():  # finished, and a title marked so with no place in it
                alone = _under_its_id({"id": title_id, "t": mark.get("t", ""), "f": mark.get("f", ""), "at": None,
                                       "pct": 1.0, "when": mark.get("when", ""), "time": mark.get("time", 0)}, titles)
                found = out.get(alone["id"])
                if found is None:
                    if not mark.get("done"):
                        continue
                    found = out[alone["id"]] = alone
                found["time"] = max(found.get("time", 0), mark.get("time", 0))  # a change of the mark is news too
                if mark.get("done"):
                    found.update(done=True, finished=mark.get("when", ""))
            return out

    def kept(self) -> bool:
        """Whether anything is kept about them."""
        with self._reading():
            return self._pid is not None

    def forget_places(self) -> int:
        """All their places and finished marks are gone from here, with the guides had, how far they've seen what's new and
        the theme picked (the nightly copies made before keep them till they go: once BACKUPS_KEPT newer ones are there,
        or with the first good copy made after they're NIGHTS_DAYS old, db.nightly). How many titles it was."""
        with self._changing() as c:
            count = len(self._places.keys() | self._marks.keys())
            if self._pid is not None:
                for table in ("place", "finished", "guide_seen"):
                    c.execute(f"DELETE FROM {table} WHERE person = ?", (self._pid,))
                c.execute("UPDATE person SET new_seen = NULL, look = '', own_look = NULL WHERE id = ?", (self._pid,))
                self._changed(c)
            self._places, self._marks, self._new_seen, self._guides, self._look, self._own = {}, {}, {}, set(), "", None
            self._wipe = True
        return count

    def export(self) -> dict:
        """All that's kept about them, as the database has it: the places reached and the changes of a finished mark
        (the latest HISTORY_KEPT of each title; more, kept from before that bound, stay until the title's next place
        or mark, which cuts them: _keep_latest), their lists deleted and not yet gone for good (_drop_deleted), how far
        they've seen what's new, and the browsers logged in as them (access.logins)."""
        with self._reading() as c:
            places, finished, deleted, since, logins = [], [], [], "", []
            if c is not None and self._pid is not None:
                pid = self._pid
                places = [PLACE.record(r) for r in c.execute("SELECT * FROM place WHERE person = ? ORDER BY seq", (pid,))]
                finished = [FINISHED.record(r) for r in c.execute("SELECT * FROM finished WHERE person = ? ORDER BY seq", (pid,))]
                since = c.execute("SELECT since FROM person WHERE id = ?", (pid,)).fetchone()["since"]
                deleted = [{"id": r["id"], "name": r["name"], "deleted": r["deleted"], **({"apps": True} if r["apps"] else {}),
                            "titles": [ENTRY.record(e) for e in c.execute("SELECT * FROM list_entry WHERE person = ? AND list = ? "
                                                                          "ORDER BY position", (pid, r["id"]))]}
                           for r in c.execute("SELECT id, name, deleted, apps FROM list WHERE person = ? AND deleted IS NOT NULL "
                                              "ORDER BY made", (pid,)).fetchall()]
            if c is not None:
                logins = access.logins(c, self.name)
            return {"name": self.name, "since": since,
                    "places": places, "finished": finished, "lists": self._lists, "list_names": self._list_names,
                    "deleted_lists": deleted,
                    "in_reading_apps": [i for i in self._list_names if i in self._apps], "featured": self._featured,
                    "bookmarks": list(self._bookmarks.values()), "shelves": list(self._shelves.values()), "opens_with": self._opens, "guides": sorted(self._guides),
                    "reviews": list(self._reviews.values()), "reports": list(self._reports.values()), "look": self._look,
                    "own_look": self._own, "new_seen": {k: v for k, v in self._new_seen.items() if k != "op"} or None,
                    "logins": logins}


class Pace:
    """How many changes each person has sent in the last minute, all their devices together. Past the limit the next is
    refused until the oldest of those is a minute old; a refused one isn't counted, so asking again doesn't make the
    wait longer. A place goes a few seconds after a page settles, so someone reading on two devices at once sends a
    few a minute, with lists and bookmarks besides: only a script meets the limit, and then can't keep the database,
    its disk and the nightly copies busy. In memory only: a server start begins anew."""

    def __init__(self, most: int = WRITES_PER_MINUTE, clock: Callable[[], float] = time.monotonic) -> None:
        self.most = most
        self._clock = clock
        self._sent: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def wait(self, name: str) -> int:
        """0 when a change of theirs may go ahead now, and it's counted; else how many seconds until one may."""
        with self._lock:
            now, sent = self._clock(), self._sent.setdefault(name, collections.deque())
            while sent and sent[0] <= now - 60:
                sent.popleft()
            if len(sent) >= self.most:
                return max(1, math.ceil(sent[0] + 60 - now))
            sent.append(now)
            return 0


class People:
    """The people in use, for the server. A person's lock keeps one change at a time; pace, how many a minute."""

    def __init__(self, told: Told | None = None, where: Path | None = None) -> None:
        self._told = told
        self._where = where
        self._all: dict[str, Person] = {}
        self._lock = threading.Lock()
        self.pace = Pace()

    def of(self, name: str) -> Person:
        with self._lock:
            if name not in self._all:
                self._all[name] = Person(name, self._told, self._where)
            return self._all[name]

    def _having(self, table: str) -> set[str]:
        """The names of the people with something in that table."""
        try:
            with store(self._where).read() as c:
                return {row[0] for row in c.execute(f"SELECT DISTINCT p.name FROM {table} x JOIN person p ON p.id = x.person WHERE p.removed IS NULL")}
        except db.Newer:
            return set()

    def _everyones(self, table: str, names: set[str], read: Callable[[Person], list[dict]]) -> dict[str, list[dict]]:
        """What read gives of each of these people with something in that table, by name, where it gives anything."""
        out = {}
        for name in sorted(set(names) & self._having(table)):
            found = read(self.of(name))
            if found:
                out[name] = found
        return out

    def everyones_reviews(self, names: set[str], titles: Titles | None = None) -> dict[str, list[dict]]:
        """What each of these people has said of titles, for those who have said anything: the people with an account, as
        a removed one's may still be kept."""
        return self._everyones("review", names, lambda person: person.reviews(titles))

    def everyones_reports(self, names: set[str], titles: Titles | None = None) -> dict[str, list[dict]]:
        """Each of these people's reports, for those who have made any (the people with an account)."""
        return self._everyones("report", names, lambda person: person.reports(titles))


# ---- for the commands: hylly people, hylly user --------------------------------------------------

# Each person and title once, as the place table's index (place_by_title) has them, without reading the table or sorting
# it, and the title's words from its latest place alone: one row of the table for each
READERS = ("SELECT latest.person, person.name, place.title AS id, place.title_t AS t, place.title_f AS f "
           "FROM (SELECT person, title, max(seq) AS seq FROM place GROUP BY person, title) AS latest "
           "JOIN place ON place.seq = latest.seq JOIN person ON person.id = latest.person WHERE person.removed IS NULL")


def readers(where: Path | None = None) -> list[dict]:
    """Who has a place in which title, once each: {person (their row), name, id, t, f}, for counting readers (serve's Most
    read, asked by each reading app's start page), t and f the title's words as its latest place has them, for a title
    whose id has changed since (READERS). The names only tell accounts from those set aside; nobody's place leaves the
    server."""
    with store(where).read() as c:
        return [dict(r) for r in c.execute(READERS)]


def in_the_apps(where: Path | None = None) -> list[dict]:
    """Every list ticked for the reading apps, of everyone still here, the Featured one first: {person (their row's id), by,
    id, name, featured, entries (in its order)}. Whose show, and to whom, is the server's say (opds.py)."""
    with store(where).read() as c:
        rows = c.execute("SELECT list.person, person.name AS by, list.id, list.name, list.featured FROM list JOIN person ON person.id = list.person "
                         "WHERE list.apps = 1 AND list.deleted IS NULL AND list.name IS NOT NULL AND person.removed IS NULL "
                         "ORDER BY list.featured DESC, person.name, list.made").fetchall()
        return [{"person": r["person"], "by": r["by"], "id": r["id"], "name": r["name"], "featured": bool(r["featured"]),
                 "entries": [ENTRY.record(e) for e in c.execute("SELECT * FROM list_entry WHERE person = ? AND list = ? ORDER BY position", (r["person"], r["id"]))]}
                for r in rows]


def everyone(where: Path | None = None) -> list[dict]:
    """Whoever has anything kept, by name: how many titles, and since when. Raises db.Newer."""
    with store(where).read() as c:
        found = [(row["name"], row["since"]) for row in c.execute("SELECT name, since FROM person WHERE removed IS NULL ORDER BY name")]
    out = []
    for name, since in found:
        person = Person(name, where=where)
        out.append({"name": name, "since": since, "titles": len(person.latest()), "listed": len(person.lists().get("reading", [])),
                    "bookmarks": len(person.bookmarks())})
    return out


KEPT = (  # what's kept about a person, kind by kind, as rows of a table, in words: what hylly people erase says goes
    ("place", "", "place", "places"),
    ("finished", "", "finished mark", "finished marks"),
    ("list", " AND name IS NOT NULL", "list of their own", "lists of their own"),
    ("list_entry", "", "title on their lists", "titles on their lists"),
    ("bookmark", "", "bookmark", "bookmarks"),
    ("shelf", "", "shelf", "shelves"),
    ("review", "", "review", "reviews"),
    ("report", "", "report", "reports"),
)
SMALL = (  # and the small things kept with them, each said when it's there (aside's kept, in SQL on their row and guide_seen)
    ("guides", "(SELECT count(*) FROM guide_seen WHERE person = p.id)", "the reader's guides they've had"),
    ("new_seen", "new_seen IS NOT NULL", "how far they've seen what's new"),
    ("opens", "opens != ''", "the shelf they open with"),
    ("look", "look != ''", "the theme they picked"),
    ("own_look", "own_look IS NOT NULL", "their own theme's colours"),
)


def kept_in_words(kept: dict[str, int]) -> str:
    """aside's kept as a sentence says it, all of it: "12 places, 1 bookmark and the theme they picked". The person's
    row itself, their name and since when, goes with it."""
    said = [f"{kept[table]} {one if kept[table] == 1 else many}" for table, _, one, many in KEPT if kept.get(table)]
    said += [words for key, _, words in SMALL if kept.get(key)]
    return (", ".join(said[:-1]) + " and " + said[-1] if len(said) > 1 else said[0]) if said else "only their name and since when"


def aside(name: str | None = None, where: Path | None = None) -> list[dict]:
    """What's kept aside of removed users (access.remove_user; of that name only, if given, "" being a name like any
    other), the latest removed first (access.latest_removed_first): {id (the row's), name, since, removed, titles,
    listed, bookmarks}, counted as everyone() counts them, and kept: the rows of each table of KEPT, and which of SMALL
    are there. Raises db.Newer."""
    small = ", ".join(f"{sql} AS {key}" for key, sql, _ in SMALL)
    with store(where).read() as c:
        rows = access.latest_removed_first(c.execute(
            f"SELECT id, name, since, removed, {small} FROM person p WHERE removed IS NOT NULL"
            + (" AND name = ?" if name is not None else ""), (name,) if name is not None else ()).fetchall())
        out = []
        for row in rows:
            pid = row["id"]
            titles = c.execute("SELECT count(*) FROM (SELECT title FROM place WHERE person = ? UNION SELECT title FROM finished WHERE "
                               "seq IN (SELECT max(seq) FROM finished WHERE person = ? GROUP BY title) AND done = 1)", (pid, pid)).fetchone()[0]
            listed = c.execute("SELECT count(*) FROM list_entry WHERE person = ? AND list = ?", (pid, READING_LIST)).fetchone()[0]
            kept = {table: c.execute(f"SELECT count(*) FROM {table} WHERE person = ?{more}", (pid,)).fetchone()[0]
                    for table, more, _, _ in KEPT}
            kept.update({key: int(row[key]) for key, _, _ in SMALL})
            out.append({"id": pid, "name": row["name"], "since": row["since"], "removed": row["removed"], "titles": titles,
                        "listed": listed, "bookmarks": kept["bookmark"], "kept": kept})
    return out


def erase(name: str, ids: list[int], where: Path | None = None) -> tuple[list[int], int]:
    """What was kept aside of a removed user of that name, these rows of it (aside's ids), gone for good: their row,
    and with it, by the foreign keys, every place, mark, list, bookmark, shelf, review, report and guide had of theirs.
    Never a row that isn't set aside, so never a user's of that name now, nor anyone else's. Overwritten in the
    database's file, and the WAL emptied, as words taken away are. Once nothing of that name is kept aside and no user
    has it, the name goes from the library's records too, in the same change (access.name_in_records): the shelves for
    everyone they made, their changes, their word on reported titles and their answers name access.FORMER, so the name
    is free, and a later user given it takes over none of that. While a user has the name, those records may be theirs,
    and stay as they are. The ids of the rows erased, one brought back or erased meanwhile not among them; and how many
    records no longer name them."""
    found = store(where)
    with found.write() as c:
        gone = [pid for pid in ids
                if c.execute("DELETE FROM person WHERE id = ? AND name = ? AND removed IS NOT NULL", (pid, name)).rowcount]
        free = (gone and not access.kept_aside(c, name)
                and c.execute("SELECT 1 FROM account WHERE name = ?", (name,)).fetchone() is None)
        unnamed = access.name_in_records(c, name, access.FORMER) if free else 0
    if gone:
        found.checkpoint()
    return gone, unnamed
