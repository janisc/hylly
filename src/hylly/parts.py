"""Hylly's parts, and which may use which. Each module belongs to one part, and imports only from the parts listed for
it (MAY), at the top or inside a function alike: tests/test_parts.py reads every import and holds them to this. What it
guards most is that the library server can't reach the shops' logins, nor the code that talks to the shops, however
the imports run (SERVER_NEVER). hyllyd, the server itself (daemon.py), is in the server's part, so neither does it, nor
load them when it runs. docs/architecture.md says the same in words.

Nor does a module reach itself again through the others, by any road, inside a function too: an import cycle hidden
behind an import in a function breaks the start of a program once that import is moved to the top, as a tidy
contributor or a tool would. What two modules both need goes below them both, as the archives' helpers are
(archives.py, below meta, reader and pdfworker). The cycles still there are listed in tests/test_parts.py (CYCLES_LEFT),
each to be undone, and none may be added.

Data only: nothing here is imported at run time."""

from __future__ import annotations

from .shops import SHOPS

PARTS: dict[str, tuple[str, ...]] = {
    # what everything stands on: folders, files, settings, rules, users and logins to the library, jobs, alarms, and
    # sizes, how long and counts in words (words)
    "core": ("__init__", "exits", "paths", "storage", "settings", "rules", "matching", "notify", "instance", "runs", "access", "bell", "sites", "shops", "parts", "db", "remote", "fetching", "words"),
    # reading book files: what's in them, their pages; the archives' helpers below them all (archives)
    "books": ("archives", "meta", "reader", "pdfworker"),
    # the shops' logins and the lookup sources' keys
    "logins": ("logins",),
    # what's recorded of the library: the shops' orders as kept on disk, the download record, your own folders and changes,
    # the catalog as the server and the commands read it, your changes laid over it (library), and the files you removed
    # into _Removed (removed)
    "records": ("snapshot", "inventory", "plan", "curation", "folders", "manifest", "library", "removed"),
    # the words: every command and option, what each may do, and the help page made from them
    "words": ("commands", "effects", "helppage"),
    # the catalog and the lookups of details
    "catalog": ("catalog", "lookups"),
    # the shops, each in a module named by its id (shops.py), itch.io's survey and claiming (itchio_survey), their
    # shared core (shopping), the shop list written and read on it (shoplist), the web session they ask their shops
    # through (shopsession), the downloads, and files copied in from elsewhere
    "shops": (*SHOPS, "itchio_survey", "shopping", "shoplist", "shopsession", "download", "copies"),
    # the library server, hyllyd (daemon), its feeds for reading apps (opds), and what it keeps of people
    "server": ("daemon", "serve", "opds", "dashboard", "dbpage", "people", "reviews", "reports", "shelves", "themes", "updates",
               "periodic"),
    # copies, backups, and the nightly copy of the database and the settings
    "copy": ("hyllycopy", "backup", "dbcopy"),
    # the front: the commands themselves, which bring the parts together
    "front": ("__main__", "cli", "doctor", "demo"),
}
EVERY = frozenset(PARTS)
MAY: dict[str, frozenset[str]] = {
    "core": frozenset({"core"}),
    "books": frozenset({"core", "books"}),
    "logins": frozenset({"core", "logins"}),
    "records": frozenset({"core", "records"}),
    "words": frozenset({"core", "records", "words"}),
    "catalog": frozenset({"core", "books", "logins", "records", "words", "catalog"}),
    "shops": frozenset({"core", "books", "logins", "records", "shops"}),
    "server": frozenset({"core", "books", "records", "words", "copy", "server"}),  # copy: the nightly copies, by name, never a login
    "copy": frozenset({"core", "copy"}),
    "front": EVERY,
}
SERVER_NEVER = frozenset({"logins", "shops", "catalog", "front"})  # nor anything that leads to them


def part_of(module: str) -> str | None:
    return next((part for part, modules in PARTS.items() if module in modules), None)
