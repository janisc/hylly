"""What came into the library, and from where: the files the downloads fetched, each shop's apart (Manifest), and the
books hylly add copied in from folders of yours (copies, add_copy). Every record is kept, in order, and the latest of a
path is the one that counts. They're in the database that keeps the library's own records (db.own), in the tables
download and copied. The downloads write them (download.py, copies.py); the catalog reads them to know which file of a
shop's title is which. Kept apart from the downloader, so that reading them doesn't bring in the code that talks to the
shops (parts.py)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from . import curation, db, paths
from .shops import SHOPS
from .words import many


def store(root: Path) -> db.Database:
    """The database that keeps the library's own records (curation.store: else db.NotHere)."""
    return curation.store(root)


def _downloads(c, shop: str) -> list[dict]:
    return [json.loads(row[0]) for row in c.execute("SELECT body FROM download WHERE shop = ? ORDER BY seq", (shop,))]


def _copies(c) -> list[dict]:
    return [json.loads(row[0]) for row in c.execute("SELECT body FROM copied ORDER BY seq")]


def _sound(entry: dict) -> bool:
    """A record the commands may go by: its path inside the library (paths.inside_library), and its size a number."""
    size = entry.get("size", 0)
    return paths.inside_library(entry.get("path")) and isinstance(size, int) and not isinstance(size, bool)


def _left_out(count: int, one: str, more: str) -> None:
    if count:
        print(f"Left out {many(count, one, more)} that {'names' if count == 1 else 'name'} no place inside the library, or "
              "isn't whole: the database has it as it is.", file=sys.stderr, flush=True)


class Manifest:
    """One shop's downloads into the library: entries holds the latest record of each path, as of when it was made, and
    what's added since. A record whose path leads out of the library, or that isn't whole, is left out (_sound). The
    shop is always named: none is the default."""

    def __init__(self, root: Path, shop: str):
        if shop not in SHOPS:
            raise ValueError(f"No such shop: {shop}")
        self.root, self.shop = root, shop
        self._store = store(root)
        self._lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        left = 0
        with self._store.read() as c:
            for entry in _downloads(c, shop):
                if "path" not in entry:
                    continue
                if _sound(entry):
                    self.entries[entry["path"]] = entry
                else:
                    left += 1
        _left_out(left, f"record of {shop}'s downloads", f"records of {shop}'s downloads")

    def get(self, path: str) -> dict | None:
        return self.entries.get(path)

    def add(self, entry: dict) -> None:
        """One file's record, kept before this returns."""
        with self._lock:
            with self._store.write() as c:
                c.execute("INSERT INTO download (shop, body) VALUES (?, ?)", (self.shop, db.dump(entry)))
            self.entries[entry["path"]] = entry


def copies(root: Path) -> dict[str, dict]:
    """The books hylly add copied in, by their place in the library (lower case): the latest record of a place counts.
    One whose place leads out of the library is left out."""
    with store(root).read() as c:
        found = _copies(c)
    kept = {e["dest"].lower(): e for e in found if paths.inside_library(e.get("dest"))}
    _left_out(sum(1 for e in found if isinstance(e.get("dest"), str) and not paths.inside_library(e["dest"])), "record of copies", "records of copies")
    return kept


def add_copy(root: Path, entry: dict) -> None:
    """One copy's record, kept before this returns."""
    with store(root).write() as c:
        c.execute("INSERT INTO copied (body) VALUES (?)", (db.dump(entry),))
