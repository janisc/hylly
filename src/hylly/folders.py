"""Your own folders in the library: files you put there yourself, copied in with hylly add, or taken in from the Inbox.

Every folder at the top of the library that isn't a shop's (each shop's own, as shops.py names them) or
Hylly's own (catalog, state, curation, superseded, and any whose name starts with _ or .) is read as it is:
one title per book, same-name files in several formats together, and the first subfolder shown as the
title's collection. The folder's name is its source in the library page. A [folders.<name>] section in the
settings says more about one folder; every key is optional:

    label     the source's name in the library page and the feeds (default: the folder's name)
    kind      what its files are: one of the kinds in the rules, kinds.toml's [kinds.X] (default: book)
    kinds     subfolders whose files are another kind: { "Graphic Novels" = "comic" }, by name in any case; an entry
              that names no subfolder there is told once (own_folders)
    genres    the first subfolder is the genre, instead of a collection that genres are guessed from: the genre its
              words find in the rules, else its name (catalog.record); a book outside a subfolder is guessed as any
    named     the file names are the books' own titles, any series in brackets: "Title (Series Book 2)"
    first     its copy leads when a title is in several places, and it is looked up first
    copy_from, list: for hylly add (see copies.py); strip_suffix too, read where a folder's settings have it, but
              not offered in the settings' example or the help: one person's way of naming files

A folder a kind names in the rules ([kinds.X] folders: comics, manuals) is of that kind unless the settings say
otherwise (defaults); that is all that is built in. Where each title is best read is worked out for every title, as at
the shops (catalog.device_of).

The Inbox ([inbox] in the settings: one folder, or several) is no folder's own: a book dropped there, from wherever
it came, goes to the folder of yours its kind belongs in (Inbox.folder_for), and its subfolders there say more
(placed): the first one is its collection; one named for a kind (Comics, RPG) says the book's kind, the next one down
then its collection; one whose name starts with # gives it that tag; deeper ones stay folders. Its words are here, as
the server's job (periodic.py) needs them to know whether a pass has anything to do before it starts one, and the pass
itself (copies.py) to walk it: what in it is left alone (passed_over), where what can't be taken goes (NOT_ADDED), and
where the passes keep what they saw (state_file, in the library's state folder).
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, rules
from .curation import TAG_MAX
from .exits import NOT_SET_UP, Exit
from .settings import INTO_DEFAULT, _tell, get as settings_section, own_name
from .shops import SHOPS, labels as shop_labels
from .storage import is_link


def defaults() -> dict[str, dict]:
    """The folders of a kind of their own by their names, as the rules name them ([kinds.X] folders: comics and
    manuals, as they come): {"comics": {"kind": "comic"}}. The settings win."""
    return {name: {"kind": kind.name} for kind in rules.current().kinds.values() for name in kind.folders}


@dataclass
class Folder:
    name: str  # the folder in the library, and the key of its source
    label: str
    kind: str = "book"
    kinds: dict[str, str] = field(default_factory=dict)
    genres: bool = False
    named: bool = False
    first: bool = False
    copy_from: str = ""
    list: str = ""
    strip_suffix: str = ""

    def kind_of(self, subfolder: str) -> str:
        """A subfolder's kind: its entry in kinds, the names compared without regard to case or Unicode form, as Windows
        and macOS see a folder's name ("Graphic novels" is "Graphic Novels"); else the folder's own."""
        name = folded(subfolder)
        return next((kind for sub, kind in self.kinds.items() if folded(sub) == name), self.kind)


def folded(name: str) -> str:
    """A folder's name as names compare (matching.folded): in one Unicode form, without regard to case."""
    return unicodedata.normalize("NFC", name).casefold()


def configured() -> dict[str, dict]:
    """The [folders.<name>] sections of the settings, by folder name in lower case, with the keys each sets: each
    checked, and one a folder doesn't have told, as in every section (settings.py's FOLDER). genres = "false" in
    quotes thus stops, rather than count as true."""
    return {name: {k: v for k, v in options.items() if not k.startswith("_")}
            for name, options in settings_section("folders").items()}


def folder(name: str, settings: dict[str, dict] | None = None) -> Folder:
    """A folder's options: built-in defaults, then the settings. It needn't exist yet (hylly add makes it)."""
    options = {**defaults().get(name.lower(), {}),
               **(configured() if settings is None else settings).get(name.lower(), {})}
    known = rules.current().kinds
    kind = str(options.get("kind") or rules.DEFAULT_KIND)
    if kind not in known:
        raise Exit(NOT_SET_UP, f"[folders.{name}] kind = {kind!r} in the settings: use one of {', '.join(known)}.")
    kinds = {str(k): str(v) for k, v in (options.get("kinds") or {}).items()}
    for sub, k in kinds.items():
        if k not in known:
            raise Exit(NOT_SET_UP, f"[folders.{name}] kinds: {sub!r} = {k!r} in the settings: use one of "
                                   f"{', '.join(known)}.")
    return Folder(name=name, label=str(options.get("label") or name[:1].upper() + name[1:]), kind=kind, kinds=kinds,
                  genres=bool(options.get("genres")), named=bool(options.get("named")),
                  first=bool(options.get("first")), copy_from=str(options.get("copy_from") or ""),
                  list=str(options.get("list") or ""), strip_suffix=str(options.get("strip_suffix") or ""))


def is_own(name: str) -> bool:
    """One folder's plain name, never a path, that is none of the shops' or Hylly's own (settings.own_name, as into
    under [inbox] is checked by it too)."""
    return own_name(name)


def own_folders(root: Path) -> list[Folder]:
    """The library's own folders that exist, in name order. A kinds entry that names no subfolder of its folder, a
    name written wrong say, is told once, as a key a section doesn't have is (settings.py)."""
    settings = configured()
    if not root.is_dir():
        return []
    found = [folder(p.name, settings) for p in sorted(root.iterdir(), key=lambda p: p.name.lower()) if p.is_dir() and is_own(p.name)]
    for own in found:
        if own.kinds:
            _tell_unmatched(own, root / own.name)
    return found


def _tell_unmatched(own: Folder, base: Path) -> None:
    try:
        with os.scandir(base) as entries:
            there = {folded(e.name) for e in entries if e.is_dir()}
    except OSError:  # one that can't be listed just now: the build says so (catalog.folder_items)
        return
    for sub in own.kinds:
        if folded(sub) not in there:
            _tell(("folders", own.name.lower(), "kinds", folded(sub)),
                  f"[folders.{own.name}] kinds: {sub!r} names no subfolder of {own.name} in the library, so it "
                  "changes nothing for now. Check how the subfolder is spelt, or take the entry out.")


def labels(root: Path) -> dict[str, str]:
    """Every source's name as shown: the shops, then your folders."""
    return {**shop_labels(), **{f.name: f.label for f in own_folders(root)}}


def lead_order(root: Path) -> list[str]:
    """Whose copy leads when a title is in several places: your folders marked first, the shops, the rest of yours."""
    own = own_folders(root)
    return [f.name for f in own if f.first] + list(SHOPS) + [f.name for f in own if not f.first]


def is_book(name: str, books) -> bool:
    """A file, by its name, that is a book to take in from a folder (your own, as the catalog walks them, or the one
    hylly add copies from): its suffix one of books (the rules' book formats, rules.Rules.book_suffixes, and a folder's
    own), in any case, and not the ._ file macOS writes beside each one it puts on a share or another disk, which has
    the book's name and holds only the Finder's notes on it."""
    return Path(name).suffix.lower() in books and not name.startswith("._")


# ---- the Inbox: [inbox] in the settings, which the server's job empties into the library -----------------------------

NOT_ADDED = "Not added"  # inside an Inbox: what a pass couldn't take, with a .txt beside each saying why
SYSTEMS_OWN = frozenset({".ds_store", "thumbs.db", "desktop.ini"})  # what the systems leave in a folder they have shown
LOOKUP_AGAIN = 15 * 60.0  # seconds after which a pass asks again about the books whose lookups had to wait
BOOKS = "books"  # the folder of yours every kind's books go to that neither into nor the rules give a folder
TAG_MARK = "#"  # a folder in an Inbox whose name starts with it gives its books a tag: the rest of its name


@dataclass
class Inbox:
    """One Inbox, as [inbox] names it: its folder, as written there, which is also what the passes know it by (key), and
    what every Inbox has alike: the folder of yours each kind's books go to (into: every kind in the rules), whether its
    books are looked up, and what their file names drop (strip_suffix, as a folder's)."""

    folder: str
    into: dict[str, str]
    lookup: bool = True
    strip_suffix: str = ""

    @property
    def key(self) -> str:
        return self.folder

    def folder_for(self, kind: str) -> str:
        """The folder of yours a book of this kind goes to (into)."""
        return self.into.get(kind) or self.into[INTO_DEFAULT]

    def where(self) -> str:
        """Where its books go, by their kind, in words, as the rules call the kinds: "comics to comics, manuals to
        manuals, the rest to books"."""
        default, kinds = self.into[INTO_DEFAULT], rules.current().kinds
        own = [f"{kinds[kind].plural.lower()} to {folder}" for kind, folder in self.into.items()
               if kind in kinds and folder != default]
        return ", ".join([*own, f"{'the rest' if own else 'every book'} to {default}"])


def into(settings: dict | None = None, root: Path | None = None) -> dict[str, str]:
    """Which folder of yours each kind's books go to, by the kind, and INTO_DEFAULT for one the rules add later: what
    into under [inbox] names; else the kind's own folder, as the rules give one ([kinds.X] folders: comics, manuals),
    the first that is a name for a folder of yours (own_name, as into's are checked: never a shop's or Hylly's own, as
    a rules file of yours could name); else into's default, else BOOKS. With root, each as the library spells its
    folder, if it has one: a disk that tells case apart would get a second folder beside the first."""
    given = (settings_section("inbox") if settings is None else settings).get("into") or {}
    default = given.get(INTO_DEFAULT) or BOOKS
    found = {kind.name: given.get(kind.name) or next((f for f in kind.folders if own_name(f)), default)
             for kind in rules.current().kinds.values()}
    found[INTO_DEFAULT] = default
    if root is not None:
        try:
            spelt = {p.name.lower(): p.name for p in root.iterdir() if p.is_dir()}
        except OSError:
            spelt = {}
        found = {kind: spelt.get(name.lower(), name) for kind, name in found.items()}
    return found


def inboxes(settings: dict | None = None, root: Path | None = None) -> list[Inbox]:
    """The Inboxes [inbox] names, folder first and then folders, each once (the same folder written twice is one Inbox),
    whether each is there or not; none without either. root: the library, to name each folder of yours as it spells it
    (into)."""
    section = settings_section("inbox") if settings is None else settings
    found: dict[str, str] = {}
    for written in [section.get("folder") or "", *(section.get("folders") or [])]:
        if written:
            found.setdefault(os.path.normcase(os.path.normpath(written)), written)
    if not found:
        return []
    where = into(section, root)
    return [Inbox(folder, where, lookup=section.get("lookup", True) is not False,
                  strip_suffix=str(section.get("strip_suffix") or "")) for folder in found.values()]


def kind_named(name: str) -> str:
    """The kind a folder's name names, as the rules name and call each kind: its name (rpg), with an s (rpgs), its label
    (RPG book), its plural (RPG books) or a folder of its own (comics), without regard to case or Unicode form; "" for
    none."""
    name = folded(name.strip())
    for kind in rules.current().kinds.values():
        if name in {folded(n) for n in (kind.name, f"{kind.name}s", kind.label, kind.plural, *kind.folders)}:
            return kind.name
    return ""


@dataclass
class Placed:
    """Where a book in an Inbox goes, and what its folders there say of it (placed): its place in the library, its kind,
    the kind to keep as a correction when the folder it goes to would give it another (""), its collection, the tags its
    folders give it, and those of its folders whose names no tag can be (too long, or nothing after the mark), by their
    paths in the Inbox."""

    dest: str
    kind: str
    fix: str = ""
    collection: str = ""
    tags: list[str] = field(default_factory=list)
    no_tags: list[str] = field(default_factory=list)


def tag_of(name: str) -> str:
    """The tag a folder's name gives (one starting with TAG_MARK), as a tag is written (curation.tag_name): its spaces
    made one, 1 to TAG_MAX letters, no control character; and in one Unicode form, as a Mac's share may write an accent
    as a mark of its own, which would make a second tag beside the one with the accent written whole. "" for a name
    that gives none."""
    if not name.startswith(TAG_MARK):
        return ""
    tag = rules.nfc(" ".join(name[len(TAG_MARK):].split()))
    return "" if not tag or len(tag) > TAG_MAX or any(ord(c) < 32 or ord(c) == 127 for c in tag) else tag


def placed(inbox: Inbox, rel: str, size: int) -> Placed:
    """Where a book in an Inbox goes, by its path there (rel, with /) and its size. A folder whose name starts with #
    gives it a tag, the rest of the name, and is passed over for its place; of the others, the first is its collection,
    unless it names a kind (kind_named): then that is its kind, and the next one down its collection; deeper ones are
    kept as folders. Without a folder naming it, its kind is what its format makes it (Rules.kind_of by the file alone:
    a CBZ is a comic), else a book. It goes to the folder of yours its kind belongs in (Inbox.folder_for), in its
    collection and the folders below. Only a kind a folder named is kept as your correction of it would be (fix), where
    the folder it goes to would give it another, so the catalog shows it as that kind: an RPG book in a folder of books.
    One from its format only places it, and one nothing named is the folder's to say, by its own settings (kind,
    kinds) and the build, as for a book you put there yourself: no word of a collection's makes a kind of its own."""
    *folders_in, name = rel.split("/")
    tags, no_tags, kept = [], [], []
    for n, d in enumerate(folders_in):
        if not d.startswith(TAG_MARK):
            kept.append(d)
            continue
        tag = tag_of(d)
        if not tag:
            no_tags.append("/".join(folders_in[:n + 1]))
        elif tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
    named = kind_named(kept[0]) if kept else ""
    if named:
        kept = kept[1:]
    collection = kept[0] if kept else ""
    kind = named or rules.current().kind_of(rules.DEFAULT_KIND, [], "", [(Path(name).suffix.lower()[1:], size)])[0]
    stem = Path(name).stem
    name = (stem.removesuffix(inbox.strip_suffix) if inbox.strip_suffix else stem) + Path(name).suffix
    into_folder = inbox.folder_for(kind)
    fix = named if named and folder(into_folder).kind_of(collection) != named else ""
    return Placed("/".join([into_folder, *kept, name]), kind, fix, collection, tags, no_tags)


UNFINISHED = (".part", ".crdownload", ".partial", ".tmp", ".download")  # a browser's download, or a copy, still under way


def passed_over(name: str) -> bool:
    """A file or folder an Inbox's pass leaves where it is, untouched and unsaid: a hidden one (a copy still under a
    temporary name, as rsync's starts with a dot), one still on its way under a name that says so (a browser's .part,
    .crdownload, .partial or .download, a .tmp), and what the systems leave (.DS_Store, Thumbs.db, desktop.ini, macOS's
    ._ beside each file, an open Office file's ~$)."""
    return name.startswith((".", "~$")) or name.lower() in SYSTEMS_OWN or name.lower().endswith(UNFINISHED)


def leads_elsewhere(path: Path | str) -> bool:
    """A link or a Windows junction (storage.is_link), or what can't be looked at: an Inbox's pass never follows one,
    nor writes or removes anything through it, as it could lead out of the Inbox, into the library say."""
    try:
        return is_link(path)
    except OSError:
        return True


def inbox_files(source: Path, unreadable: list[str] | None = None) -> Iterator[Path]:
    """Every file waiting in an Inbox, in name order: all below it but its Not added folder, what's passed over, and
    links and junctions, which could lead anywhere (leads_elsewhere): neither a file nor a folder of them is taken. A
    folder below it that can't be read is put in unreadable, if given, and passed over."""
    def refused(e: OSError) -> None:
        if unreadable is not None and e.filename and str(e.filename) != str(source):
            unreadable.append(str(e.filename))

    for here, subfolders, names in os.walk(source, onerror=refused):
        top = Path(here) == source
        subfolders[:] = sorted(d for d in subfolders if not passed_over(d) and not leads_elsewhere(os.path.join(here, d))
                               and not (top and d.casefold() == NOT_ADDED.casefold()))
        for name in sorted(names):
            path = Path(here) / name
            if not passed_over(name) and not leads_elsewhere(path):
                yield path


def state_file(root: Path) -> Path:
    """What the Inbox's passes keep between them, in the library's state folder: what each look saw, the books whose
    lookups wait, the books copied in whose originals are still to go, the originals whose tags and kind were given
    (once for each, by its path, size and MD5), what was said once, the folders a person has said yes for, and what
    each pass of the last two days did, for the daily summary."""
    return paths.state_dir(root) / "inbox.json"


def log_file(root: Path) -> Path:
    """The passes' lines, each with its time: hylly logs inbox."""
    return paths.state_dir(root) / "inbox-log.txt"


def read_state(root: Path) -> dict:
    """The passes' state, each part of the kind it should be: what isn't, or a file that can't be read, is forgotten (a
    look then starts afresh, which only means a book waits a pass longer)."""
    try:
        saved = json.loads(state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    out = {key: saved[key] if isinstance(saved.get(key), dict) else {}
           for key in ("seen", "pending", "said", "agreed", "taken", "tagged")}
    # each Inbox's record of what was given is a table, or it is forgotten
    out["tagged"] = {inbox: given for inbox, given in out["tagged"].items() if isinstance(given, dict)}
    out["done"] = [row for row in saved.get("done") or [] if isinstance(row, list) and len(row) == 5
                   and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in row)]
    return out


def missing_said(key: str) -> str:
    """The key under which a pass says once that an Inbox's folder isn't there: by the Inbox's key, its folder as
    [inbox] writes it."""
    return f"missing|{key}"


def waiting(root: Path, now: float | None = None) -> bool:
    """Whether a pass has anything to do: a book in an Inbox, a lookup that waited and may be asked again now, or an
    Inbox's folder gone, or back, since a pass last said so. Cheap, as the server's job asks it every minute before it
    starts a pass: nothing is opened but the folders and the state file."""
    now = time.time() if now is None else now
    found = inboxes()
    if not found:
        return False
    state = read_state(root)
    for inbox in found:
        source = Path(inbox.folder)
        try:
            there = source.is_dir()
        except OSError:
            there = False
        if there != (missing_said(inbox.key) not in state["said"]):  # gone and not said yet, or back and said gone
            return True
        if there and next(inbox_files(source), None) is not None:
            return True
    return any(isinstance(p, dict) and now - (p.get("asked") or 0) >= LOOKUP_AGAIN for p in state["pending"].values())
