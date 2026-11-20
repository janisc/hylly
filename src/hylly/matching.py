"""Finding a title again when its id has changed. Shop ids are stable, but a folder title's id comes from its first
file's path, so a rename or a new format can change it. What was kept about a title (its name and its file) then finds
it, by the rules here, in two strengths:

    find, for what a person keeps (places, lists, bookmarks, reviews), looked up each time it is read: the one title with
    the very same file, else one with the same file name, else one with the same title; more than one is no answer, as a
    wrong title would be worse than none. Nothing is moved, so a title that comes back has its own again.

    follow, for what a build moves for good to the new id (your tags and corrections, the day a title first came): to the
    title that holds the copies the last build had merged with it, as that is the same title, unless it also holds a
    copy that was another title's then (a copy left can join another book); else only to a title new
    in this build, never one already there, and only by its very file, or by a file of the same name and the same size
    as the last build had. A folder away for one build thus never hands its 001.cbz's tags to another folder's 001.cbz;
    a file renamed waits, kept, to be placed by hand.

Names compare in one Unicode form (NFC) and without regard to case: a Mac-made disk, some sync tools and archives write
an accented letter as the letter and its accent apart, where Windows writes one letter, and it is the same name.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote


def ids_of(rec: dict) -> list[str]:
    return [rec["id"], *rec.get("ids", [])]


def folder_id(rel: str) -> str:
    """A folder title's id, from its first file's path: in one Unicode form (NFC) and lower case, so that a name written
    with its accents apart (as a Mac-made disk, some sync tools and archives write it) is the same title. A path already
    in that form, as nearly every one is, keeps the id it always had. Here, as your answers about a card's copies tell
    each folder copy by it too (curation.set_copies)."""
    return "f:" + hashlib.sha1(unicodedata.normalize("NFC", rel).lower().encode()).hexdigest()[:12]


def folded(text: str) -> str:
    """A name or path as they compare: in one Unicode form, without regard to case. One already in that form, as nearly
    every name is, compares exactly as it always did."""
    return unicodedata.normalize("NFC", text).casefold()


def file_name(path: str) -> str:
    return folded(PurePosixPath(path).name)


@dataclass
class Before:
    """What the last build kept: every id it had (its titles' and their copies'), each with its files as (path in the
    library, size), and with the ids of the one title it was part of (merged: the title's own and its copies'). A copy's
    files are those of the title it was merged into. Empty before the first build."""

    files: dict[str, list[tuple[str, int | None]]] = field(default_factory=dict)
    merged: dict[str, set[str]] = field(default_factory=dict)


class Titles:
    """The titles now in the library, by what can find them again. before: the last build, for follow."""

    def __init__(self, records: list[dict], before: Before | None = None) -> None:
        self.holder = {i: r["id"] for r in records for i in ids_of(r)}  # every id, a copy's too: the title that has it
        self.held = {r["id"]: set(ids_of(r)) for r in records}  # and each title's ids
        self.present = set(self.holder)
        self.by_path: dict[str, set[str]] = {}
        self.by_file: dict[str, set[str]] = {}
        self.by_size: dict[tuple[str, int], set[str]] = {}  # (file name, size)
        self.by_title: dict[str, set[str]] = {}
        for r in records:
            for f in r.get("f", []):
                path = unquote(f[0])
                name = file_name(path)
                self.by_path.setdefault(folded(path), set()).add(r["id"])
                self.by_file.setdefault(name, set()).add(r["id"])
                if len(f) > 2 and isinstance(f[2], int):
                    self.by_size.setdefault((name, f[2]), set()).add(r["id"])
            self.by_title.setdefault(folded(r.get("t", "")), set()).add(r["id"])
        self.before = before or Before()
        # new in this build: a title with an id the last build didn't have, its own or a copy's (a renamed copy merged
        # into a title that was there brings it a new id)
        self.fresh = {r["id"] for r in records if any(i not in self.before.files for i in ids_of(r))}

    def __contains__(self, title_id: str) -> bool:
        return title_id in self.present

    def find(self, seen: dict) -> str | None:
        """The title that something kept about an id no title has any more belongs to, or None. seen: {"t": title, "f": file}."""
        path = seen.get("f", "")
        same_path = self.by_path.get(folded(path), set()) if path else set()
        same_file = self.by_file.get(file_name(path), set())
        same_title = self.by_title.get(folded(seen.get("t", "")), set()) if seen.get("t") else set()
        found = next((ids for ids in (same_path, same_file, same_title) if len(ids) == 1), set())
        return next(iter(found), None)

    def follow(self, old: str, seen: dict | None = None) -> str | None:
        """The title an id no title has any more has become, for moving what is kept by it for good; else None. The
        title that now holds the ids the last build had merged with it (a copy gone, the others still there: the lead
        or another), if every id it holds that the last build knew was of that same title: a copy left may have joined
        another book's title, and that book isn't this one. Else, new in this build, the one with the very same file, as
        the last build had its files or seen ({"f": file}) names one, else the one with a file of the same name and size
        as the last build had."""
        group = self.before.merged.get(old, set())
        holders = {self.holder[i] for i in group if i != old and i in self.holder}
        if holders:
            known = {i for h in holders for i in self.held[h] if i in self.before.files}
            return next(iter(holders)) if len(holders) == 1 and known <= group else None
        files = self.before.files.get(old, [])
        paths = {folded(p) for p, _ in files} | ({folded(seen["f"])} if seen and seen.get("f") else set())
        found = {i for p in paths for i in self.by_path.get(p, ())} & self.fresh
        if not found:
            sized = {(file_name(p), size) for p, size in files if isinstance(size, int)}
            found = {i for key in sized for i in self.by_size.get(key, ())} & self.fresh
        return next(iter(found)) if len(found) == 1 else None
