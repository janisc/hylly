"""The shops' core: what every shop on it has in common, so that a shop gives only what is its own (docs/downloads.md,
"Writing a shop"). That is its facts in shops.SHOPS, its traits among them (how exact its listing is, how often a
refused link is asked for afresh, whether a changed file comes beside the old one, who made a title it doesn't say), and
in its module, named by its id, a Library opened with its login (a context manager) that does three things:

- check(): checks the login, raising logins.AuthError when the shop refuses it;
- titles(progress): what you own there, as Titles with their Files, and in unreadable, as (its key or "", a name for
  people), each one the listing gave in a shape that can't be read, left out of the titles;
- link(title, file): a fresh link to the file, asked for right before its download; "" when the shop no longer has it.

The rest is here, and the same for every shop on it: what kind each title is (kind_found: the rules' one way, classify,
by its bundles, who made it and its files, over its shop's own word for it or its kind, worked out once; your correction
over it, corrected, by the title's catalog id), which files to download (select: by the shop's download records, each
title in <shop>/<who made it, or its bundle>/<its name>/, keeping the folder and the names it got the first time; a
title listed again, by its key, taken once; a file the shop has changed waits for --update, or comes beside the old one;
the never-list and the shop's own reasons leave files out, and each is named), the names of the files a shop names only
in their links, as their downloads start (Naming), the downloader's jobs and their records (make_job: every shop's
records name the title and the file by the same fields, item and file, with what the shop listed of it, listed_md5 and
listed_size), the snapshot the catalog reads its titles from (snapshot; catalog.shop_items), and the words for what the
listing couldn't read (unread).
cli._shop_download runs a download with them, with the options every shop on the core takes (commands.shop_download).
hylly verify and hylly doctor go by the table already. The shop list (shoplist.py: hylly SHOP list, and a LIST after
hylly SHOP download) is select's rows written out, and a list's marks read back go to the same select (marks), so a
plain run is a download of the list the rules make; the files a list left out (yours) a plain run never takes.

A shop with ways of its own gives them as the same names on its Library and its Files, which the core asks of a shop
that has them and otherwise goes without (itch.io's and StoryBundle's, each said where the core asks): Library.chosen,
the titles a run takes besides the core's options; Library.left_out, its words for what a run leaves out; Library.gone,
what it does about a file it no longer has; Library.refused, the answers a run of its counts as turned away, its
link requests' and the downloads' (download.REFUSED), in its words; File.left, File.current and File.device, its own
reasons to leave a file out, its own rule for a file still listed as it was downloaded, and where a file is best read
where its size says nothing of that; File.keep_size, true where its record keeps the size listed even with an MD5;
File.name "", a file it names only in its link, and File.rough, a size it only rounds; Title.place, its own order, and
Title.record, what its records keep besides. cli._shop_download takes a shop's own option that leaves titles out of a
run (skip: StoryBundle's --no-bonus), and select chooses by Title.under (bundle: StoryBundle's --bundle).

Humble's are these (humble.py): Title.folder, the folder a shop gives each title itself, by a plan of its own (Humble's,
plan.py), each file named in it by its key, so that its files are known by their places and recorded as Humble's were
before the core (make_job): a title with a folder is taken so whatever its shop, so a shop gives one only to place its
own files; File.fallbacks, the title's other formats behind its chosen file, tried when the shop's own file is at fault,
and a pending file with one of them standing in for it (instead_of) taken after every other (Selection.standing);
File.url, a link the listing gave, tried before a fresh one; File.ext, a file's format where its name doesn't say;
File.unsure, the plan unsure which of a title's files to keep (its options, its guess and why: a question, select);
Title.also, its other purchases' ids, under which your answers count too (select);
Title.device, where a title is best read (select's devices: Humble's --device), Title.why, why it is chosen so, for a
dry run's --json, and Title.found, the kind its plan found it to be, by every format Humble offers (kind_found takes it
as it is). And on its Library: snapshot, its own snapshot of what it listed; unanswered and no_data, what the listing
lacked this time, after which a run ends partly done; summary, its own words on the whole library after the run's
totals; questions, what its rules leave to you besides the core's (its items of only extras: is it a title?), with the
kinds of question it finds wholly. select's max_gb stops adding files at that many GB (Humble's --max-gb).

On the core: every shop, DriveThruRPG, StoryBundle, itch.io and Humble, sharing its free names (plan.free_name,
plan.free_folder), the never-list (download.never) and the record's fields."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from typing import TYPE_CHECKING

from . import db
from .shops import SHOPS
from .words import GB

if TYPE_CHECKING:  # named in an annotation only: download imports it where it's used
    from .download import Left

LEFT_OUT = "left-out.json"  # in a shop's folder in state/: the files their links named as a kind no download takes


@dataclass
class File:
    """One of a title's files as the shop lists it. A shop's own class may stand in, with these names (DriveThruRPG's),
    but for a file named only in its link: that one is a File of this class, as the core gives it its name
    (dataclasses.replace), which a class whose name is a property can't take."""
    key: int | str  # the shop's id for it among its title's files: with the title's key, what its record knows it by
    # the shop's name for it, which it is saved under (paths.safe_filename); "" for a file the shop names only in its
    # link (StoryBundle's), whose name its link gives as its download starts (Naming)
    name: str
    size: int = 0  # as listed; 0 when the shop doesn't list it
    md5: str | None = None  # as listed, where the shop lists one
    label: str = ""  # the shop's word for its format; "" for its extension's
    role: str = "book"  # book, or extra (a map, a soundtrack): the catalog reads a title's details from a book
    # its size is the shop's rounded one ("0.4 MB", StoryBundle's): never kept as what the shop listed (listed_size), as
    # every change in the rounding would look like a changed file
    rough: bool = False

    def left(self, kind: str) -> str:
        """Why the shop's own rules leave this file out, saved as a file of this kind (download.kind_of): "" for none.
        A shop with rules of its own gives a File that says (itch.io's list of kinds to fetch, its own types, platform
        tags); the never-list is every shop's, and needs none, though the shop's words for it are said where it has
        some (itch.io's "a program"). One without this method has none (DriveThruRPG's)."""
        return ""


@dataclass
class Title:
    """One title you own at the shop, with its files, which are all downloaded but those left out (select). A shop's own
    class may stand in, with these names."""
    # The shop's id for it, the same in any account: its records' item, and its catalog id's. As the shop's records have
    # it, a string or a number (itch.io's game): a number as a string would look like another title, and every file new
    key: int | str
    name: str
    by: str  # who made it, its publisher or creator ("" when the shop doesn't say): the catalog's publisher
    files: list[File] = field(default_factory=list)
    kind: str = ""  # the shop's own word for what it is, a kind in the rules (itch.io's classification); "" for none
    purchased: str = ""  # YYYY-MM-DD
    # The rest the shop says of it, kept in the snapshot as it is, for the catalog (catalog.shop_items reads them):
    # cover (an address), isbn, description, games (the shop's own names for the game), types and genres (subjects),
    # bundle (the shop's number for the bundle it came in: a title without a game takes its bundle's)
    details: dict = field(default_factory=dict)
    under: str = ""  # the folder its own goes in, under the shop's, when that isn't who made it's: its bundle (StoryBundle's)
    # where the shop's own order puts it, before the core's (its folder and name), for a shop whose order counts:
    # StoryBundle's, its bundles oldest first, as a book listed again in a later bundle is taken in the first (select)
    place: tuple = ()
    record: dict = field(default_factory=dict)  # what its records keep of it besides every shop's fields (StoryBundle's bonus)
    # the kind it is found to be (kind_found): the rules' over its own word, or your correction; a shop whose own plan
    # finds it by more than its titles here show gives it (Humble's, by every format offered): never in a snapshot
    found: str = ""


@dataclass
class Selection:
    pending: list[tuple]  # (title, file, its place in the library, whether it replaces an older version there)
    done: int = 0
    changed: int = 0  # the shop lists another file than the one downloaded: only --update fetches it
    left: list = field(default_factory=list)  # download.Left: files no download takes, which every run names
    # files the shop changed, coming beside the old ones (Shop.beside), and those it lists anew that are here already
    noted: list[str] = field(default_factory=list)
    again: int = 0  # titles listed again by their key (a StoryBundle book in a later bundle too): each is taken once
    skipped: int = 0  # titles a shop's own option left out of the run (select's skip: StoryBundle's --no-bonus)
    # Every file of the titles chosen that a download takes, past --limit too, in their order: (title, file, its place,
    # its state: "done", "pending", or "changed", which only --update fetches; with --update, "changed" and pending)
    every: list[tuple] = field(default_factory=list)
    # The files still to download with another of the title's formats standing in for them (File.fallbacks), past
    # --limit too: (title, file, its place, that format's File, its place)
    standing: list[tuple] = field(default_factory=list)
    substituted: int = 0  # of pending, those with another format standing in for them: tried again, after every other
    # What the rules couldn't settle by themselves (curation.QUESTIONS), as curation.keep_questions takes them: a
    # title's file the shop's own rules were unsure of (File.unsure: Humble's), and a file gone from where it was
    # downloaded; with [download] unsure = "ask", the files held back for your answer (held), and those you moved by
    # hand, never fetched again (forgotten)
    questions: list[dict] = field(default_factory=list)
    held: int = 0
    forgotten: int = 0
    looked: set = field(default_factory=set)  # the titles chosen, by their keys as text: those whose questions it found
    # The shop list's rows (shoplist.py): every file of the titles chosen that a download takes or could take, past
    # --limit too, in their order, as the rules make them: (title, file, its place, the rules' verdict, left out by you,
    # its row's id, whether it is in the library). The verdict: done, pending, changed (only --update fetches it), held
    # (for your answer), moved (by you, as you said) or behind (a format behind the chosen file: File.fallbacks)
    rows: list[tuple] = field(default_factory=list)
    yours: int = 0  # files the rules take that you left out (curation.left_out, or the list's unticked rows): not fetched
    # With a list (select's marks): the files the rules had ticked that it unticks, and those you left out that it ticks
    # again, each (title, file, whether it is in the library), for the edits that keep them (cli); the which-file
    # questions whose mark it moved to one other file, (title, the option's number from 0, the options), which the cli
    # answers so; the files it unticks for that run alone (no picked to tell yours by); the ids it matched; the titles
    # chosen it has no row of
    unticked: list[tuple] = field(default_factory=list)
    ticked_back: list[tuple] = field(default_factory=list)
    answers: list[tuple] = field(default_factory=list)
    only_now: int = 0
    matched: set = field(default_factory=set)
    unlisted: list = field(default_factory=list)

    @property
    def titles(self) -> int:
        """How many titles have something to download, by their keys (and their folders, where the shop gives them)."""
        return len({_ident(t) for t, _, _, _ in self.pending})


def _ident(t) -> tuple:
    """A title as a run tells titles apart: by its key, and by its folder where the shop gives it one itself (Humble's:
    a title of its with no id of Humble's own is one by its folder)."""
    return t.key, getattr(t, "folder", "")


def row_id(shop_id: str, key, f) -> str:
    """A file's id in the shop list (shoplist.py): the shop, the title's key and the file's key, as its records name the
    title and the file (item, file: make_job), colons between. key: the title's key, or for a title the shop gives no key
    of its own the folder it gives it (Humble's: a folder has a slash, which no key of Humble's has)."""
    return f"{shop_id}:{key}:{f.key}"


def row_keys(t) -> list:
    """The keys a title's rows may be named by in a list: its key (or its folder, for a title with none), then those of
    its other purchases (Title.also: Humble's), as a title bought again may lead under another one since."""
    first = t.key if t.key not in ("", None) else getattr(t, "folder", "")
    return [first, *(k for k in getattr(t, "also", ()) if k not in ("", None))]


def classify(shop_id: str, t) -> tuple[str, str]:
    """What kind a title is found to be, and why in a few words, as every shop's titles are (rules.Rules.kind_of, which
    the catalog goes by too): by the bundles it came in (Title.under, and those its details name: itch.io's), who made
    it (Title.by) and its files as the shop lists them, by the names they are listed under (a file the shop names only
    in its link says nothing of its format), over its shop's own word for it (Title.kind: itch.io's classification),
    else its shop's kind (Shop.kind)."""
    from . import rules
    from .download import kind_of as format_of

    bundles = [getattr(t, "under", ""), *((getattr(t, "details", None) or {}).get("bundles") or [])]
    return rules.current().kind_of(t.kind or SHOPS[shop_id].kind, [b for b in bundles if isinstance(b, str) and b],
                                   t.by, [(format_of(f.name), f.size) for f in t.files if f.name])


def kind_found(shop_id: str, t) -> str:
    """The kind a title is, worked out once (Title.found): your correction (corrected), or what its shop's own plan
    found (Humble's), else classify. What --kind chooses by, and what a download records and is read where."""
    if not getattr(t, "found", ""):
        t.found = classify(shop_id, t)[0]
    return t.found


def corrected(shop_id: str, titles: list, kinds: dict[str, str]) -> int:
    """Your corrections of the titles' kinds (hylly set kind, or the library page: curation.kinds), each found by its
    title's catalog id, the shop's letter and its key ("d:1001"), laid over the kind the title is found to be
    (kind_found): a title not downloaded yet is downloaded as that kind (by its kind's list of formats, where the shop
    gives a choice: Humble's plan, which takes yours itself, and read where that kind is read: make_job's device); one
    downloaded keeps the files it has, as select goes by the records and the files alone. How many titles it changed."""
    letter, n = SHOPS[shop_id].letter, 0
    for t in titles:
        yours = kinds.get(f"{letter}:{t.key}")
        if yours and yours != kind_found(shop_id, t):
            t.found, n = yours, n + 1
    return n


def under(shop_id: str, t) -> str:
    """The folder a title's own goes in, under the shop's, as named by the shop: its bundle, or who made it, or when
    the shop doesn't say who, its word for that (Shop.maker)."""
    return getattr(t, "under", "") or t.by or SHOPS[shop_id].maker


def current(entry: dict, f) -> bool:
    """Whether the shop still lists the file downloaded as entry: by its MD5, the one listed then or the file's own
    (shops correct a stale checksum now and then, to the one the file already has); without one, by the size listed
    then or the file's own, as recorded (a stale size corrected to the file's is the same file: --update would fetch
    the same bytes again); with neither, it is."""
    if f.md5:
        return f.md5 in (entry.get("listed_md5"), entry.get("md5"))
    listed = entry.get("listed_size")
    return not (f.size and isinstance(listed, int) and listed > 0 and f.size not in (listed, entry.get("size")))


def _same(entry: dict, f) -> bool:
    """current, or the shop's own rule where its File has one (File.current: itch.io's, by which a record from before
    the listed MD5 was kept counts as the same file unless itch.io vouched for its MD5 then)."""
    own = getattr(f, "current", None)
    return own(entry) if callable(own) else current(entry, f)


def _downloaded(e: dict) -> float:
    """When a record's file was downloaded, to tell the newer of two: one from before records had their offset read as
    this computer's time (db.moment), so the two forms side by side go by the moments they are; one with none, or none
    that reads, the oldest."""
    try:
        return db.moment(str(e.get("downloaded_at") or "")).timestamp()
    except ValueError:
        return float("-inf")


def _why(f, kind: str) -> str:
    """Why no download takes the file, saved as kind: a kind on the never-list, in the shop's words where it has some,
    the shop's own rules (File.left), or "" when it is taken."""
    from .download import PROGRAM, never

    own = getattr(f, "left", None)
    why = own(kind) if callable(own) else ""
    return (why or PROGRAM) if never(kind) else why


def select(shop_id: str, titles: list, manifest, root: Path, *, title: str | None = None, by: str | None = None,
           kinds: set[str] | None = None, devices: set[str] | None = None, limit: int | None = None,
           max_gb: float | None = None, update: bool = False, chosen=None, bundle: str | None = None,
           skip=None, answers: dict | None = None, ask: bool = False, asked: set | None = None,
           yours: dict | None = None, marks: dict | None = None) -> Selection:
    """The files still to download, title by title, in the shop's own order where it has one (Title.place), else by the
    folder their own goes in and by name. A new title's folder is <shop>/<its bundle or who made it>/<name> (under), and
    a new file's name the shop's (paths.clean and paths.safe_filename; plan.free_folder and plan.free_name where that is
    taken); a shop that gives each title its folder itself (Title.folder: Humble's plan) names each file in it by its
    key, and a file of such a title is known by that place alone: its record is the one of that path, whatever its item
    says (Humble's records of before may have none, and a title's files may be another purchase's of it). A title keeps
    the folder and the file names it got the first time, so a file recorded but missing or cut
    short on the disk comes to the same place again. A file is done when its newest record is whole on the disk and the
    shop lists it as then (current, or the shop's own rule, File.current); at a shop whose changed files don't come
    beside the old ones, when any of its records is (a copy fetched again elsewhere, the first one whole again). A title
    listed again by its key (a StoryBundle book in a later bundle too) is taken once, the first of it that the choices
    below leave (Selection.again). A file the shop names only in its link (File.name "") takes the name its whole copy
    has, or one its link gave a run before that left it out (LEFT_OUT, while the shop lists it with the same size);
    without either it is chosen by the title's folder alone, or with a copy recorded but missing or cut short by that
    copy's place, to be named by its link as its download starts (Naming), which says what it names now. A file the
    shop no longer lists as it was downloaded is changed: fetched only with update, in the old one's place, or with a
    shop whose changed files come beside the old ones (Shop.beside), every run, under a name of its own, which is said
    (Selection.noted); its newer record then counts. A file the shop lists anew that is the very file of one of the
    title's here (its MD5, as listed or as it came; where the shop lists none, now nor then, its whole name, letters of
    either size, and its size: a file the shop renamed), whole, isn't fetched again, which is said too, at every shop:
    its record keeps its place, and a title with a folder of its own (Humble's) looks for it among that folder's
    records. The very file of one you moved by hand (your answer, below) is never fetched either, renamed or not.

    What the rules can't settle by themselves becomes a question (curation.QUESTIONS, Selection.questions): a file the
    shop's own rules were unsure of (File.unsure: Humble's chosen file, which the shop says why of and between which),
    when it is to download for a title no book of which is recorded here yet (a title downloaded keeps its way: a file
    fetched again is no question), or while its question is open (asked: the shop's open questions by their subjects),
    or opened again (File.reopen); and a file recorded here but gone from its place, its folder there (or the title's
    whole folder gone: one question for the title), which comes again to that place as the guess, unless the library
    itself can't be reached or the shop no longer has it (a record marked gone). Each is the guess as before, but with
    ask ([download] unsure = "ask") held back until you answer (Selection.held), and a question settled by an answer of
    yours (answers: each title's, by catalog id: curation.answers, under any purchase of it, Title.also: Humble's; for
    a file gone, its own or the whole title's, whether its folder is there or not: curation.moved) is none: "again",
    come again as before; "moved", never fetched again, counted (Selection.forgotten). A title without a key of the
    shop's is asked nothing. A file of a kind on the never-list
    (download.never), or that the shop's own rules leave out (File.left), by the shop's name for it or the name it was
    saved under, is left out, and named with why (Selection.left). title, by, kinds, devices and bundle choose titles by
    their name, who made them, their kind, where they are best read (Title.device: Humble's) and the folder their own
    goes in (Title.under: their bundle), and chosen by the shop's own choice (Library.chosen: itch.io's --types); skip: a
    test of a shop's own option for the titles it leaves out (StoryBundle's --no-bonus), counted (Selection.skipped).
    A file still to download with another of its title's formats standing in for it (File.fallbacks: downloaded instead,
    its record's instead_of this file's place, and whole) is tried again, but after every other file (Selection.standing,
    Selection.substituted), so that limit and max_gb reach the new ones first. limit: at most this many titles with
    something to download; max_gb: no more files once the next would pass this many GB (one at least). Done, changed and
    left out are counted, and every file listed (Selection.every), for every title chosen, past limit too.

    The shop list (shoplist.py, docs/downloads.md): a plain run is a download of the list the rules make, never
    written out, and a run with a list goes by it (your changes), by this one code path. Every file of the titles chosen
    that a download takes or could take is a row (Selection.rows), with the rules' verdict, and so are the formats behind
    a chosen file (File.fallbacks), which the rules never tick. yours: the files you left out (curation.left_out: by
    catalog id, under any purchase of the title, each by its key), which the rules then don't take (Selection.yours), and
    which ask no question. marks: a list's, by row id (row_id; under any purchase of the title): (its mark, what the
    rules ticked as it was written, picked: None where that can't be read). Only your changes count: a row as written
    goes by the rules as they are now (a file held back then and answered since is fetched; a format behind that has
    become the chosen file is fetched); a mark changed is yours, ticked fetched in this run, unticked where the rules had
    ticked it left out by you from now on (Selection.unticked; one you left out ticked again, ticked_back); a mark whose
    picked can't be read is yours for this run alone; a row the list lacks (deleted) is not taken. A file done is done
    whatever its mark, a changed one waits for update all the same. A which-file question whose mark the list moved to
    exactly one other of its files (the guess unticked) is answered with that file (Selection.answers, which the cli
    makes the answer of): the guess is neither fetched nor left out. The questions are found as a plain run finds them.
    So a list written and read back unchanged chooses what the plain run chooses. matched: the ids it used; unlisted: the
    titles chosen that it has no row of."""
    from . import curation
    from .download import Left, kind_of
    from .paths import clean, library_missing, safe_filename
    from .plan import free_folder, free_name

    shop = SHOPS[shop_id]
    recorded, every, folder_of, used, names_of, kept, learnt, seen = {}, {}, {}, set(), {}, {}, None, set()
    in_folder: dict[str, list[dict]] = {}  # every record, by its folder: what a title with a folder of its own has there
    for e in manifest.entries.values():
        if not e.get("gone"):
            in_folder.setdefault(str(PurePosixPath(e["path"]).parent), []).append(e)
        if e.get("item"):
            key = (e["item"], e.get("file"))
            every.setdefault(key, []).append(e)
            if key not in recorded or _downloaded(e) >= _downloaded(recorded[key]):
                recorded[key] = e  # a file fetched again beside the old one (Shop.beside): the newer counts
            folder = str(PurePosixPath(e["path"]).parent)
            folder_of.setdefault(e["item"], folder)
            used.add(folder.lower())
            names_of.setdefault(e["item"], set()).add(PurePosixPath(e["path"]).name.lower())
            if not e.get("gone"):  # what a file listed anew may be already
                kept.setdefault(e["item"], []).append(e)
    whole = lambda e: (root / e["path"]).is_file() and (root / e["path"]).stat().st_size == e.get("size")  # noqa: E731
    answers, asked, lost = answers or {}, asked or set(), library_missing(root)

    def here(t, f, own: str, answered: dict) -> tuple[str, bool]:
        """The place of the title's file recorded here that is the very file f is, or "": by its MD5, as listed or as
        it came; where the shop lists none, nor did as it came, by its whole name, letters of either size, and its size
        (never for a size the shop only rounds, nor a name it gives only in its link). The latest record of the
        title's, by its folder for one with a folder of its own: whole on the disk, or gone from it as you moved it by
        hand (your answer: moved), and then True besides, so a file you moved stays where you put it, renamed or not."""
        name = safe_filename(f.name).lower() if f.name and not getattr(f, "rough", False) else ""
        for e in reversed(in_folder.get(own, ()) if own else kept.get(t.key, ())):
            if f.md5:
                same = f.md5.lower() in (str(e.get("md5") or "").lower(), str(e.get("listed_md5") or "").lower())
            else:
                same = bool(name) and f.size > 0 and not e.get("listed_md5") and e.get("size") == f.size \
                    and PurePosixPath(e["path"]).name.lower() == name
            if same and whole(e):
                return e["path"], False
            if same and not (root / e["path"]).exists() and \
                    curation.moved(answered, e.get("file", PurePosixPath(e["path"]).name)) == "moved":
                return e["path"], True
        return "", False

    sel, rows = Selection(pending=[]), []  # rows: each title chosen, with its files to download and its notes
    order = lambda t: (getattr(t, "place", ()), under(shop.id, t).lower(), t.name.lower(), t.key)  # noqa: E731
    for t in sorted(titles, key=order):
        if (title and title.lower() not in t.name.lower()) or (by and by.lower() not in t.by.lower()) \
                or (kinds and kind_found(shop.id, t) not in kinds) or (chosen and not chosen(t)) \
                or (devices and getattr(t, "device", "") not in devices) \
                or (bundle and bundle.lower() not in getattr(t, "under", "").lower()):
            continue
        if skip and skip(t):
            sel.skipped += 1
            continue
        if _ident(t) in seen:
            sel.again += 1
            continue
        seen.add(_ident(t))
        sel.looked.add(str(t.key))
        own = getattr(t, "folder", "")  # the shop's own place for it (Humble's plan): its files known by their places
        folder = own or folder_of.get(t.key)
        if folder is None:
            folder = folder_of[t.key] = free_folder(f"{shop.folder}/{clean(under(shop.id, t))}/{clean(t.name)}", used)
        names = names_of.setdefault(t.key, set())
        wanted, left, noted = [], [], []
        cid = curation.catalog_id(shop.id, t.key) if t.key not in ("", None) else ""  # how an answer names it
        answered: dict = {}  # your answers to its questions, given under any purchase of it (Title.also: Humble's); the
        mine: set = set()  # and the files you left out (curation.left_out), each by its key, under any of them too
        for key in reversed([t.key, *getattr(t, "also", ())]):  # one that leads it now wins, as the plan has a title's
            if key not in ("", None):  # choices
                answered.update(answers.get(curation.catalog_id(shop.id, key)) or {})
                mine |= (yours or {}).get(curation.catalog_id(shop.id, key)) or set()
        fresh = not any(r.get("role", "book") == "book" for r in (in_folder.get(own) if own else kept.get(t.key)) or ())  # no
        # book of it recorded here yet (its extras may be: they come whatever is asked)
        keys, listed_any = row_keys(t), False

        def question(kind: str, which: str, options: list, guess: int, why: str, pending: bool) -> bool:
            """A question of this title's found (which: the file's key, or "" for the title's), and True when its file,
            to download (pending), is held back for your answer (ask)."""
            if not cid:
                return False
            sel.questions.append({"kind": kind, "item": str(t.key), "file": which, "title": t.name, "options": options,
                                  "guess": guess, "held": ask and pending, "why": why})
            sel.held += ask and pending
            return ask and pending

        def want(x: tuple) -> None:
            wanted.append(x)
            sel.every.append((*x[:3], "changed" if x[3] else "pending"))

        def look(f) -> tuple | None:
            """The list's row of the file (marks: by id, under any purchase of the title): (its id, its mark, its
            picked), or None for a row the list lacks."""
            for key in keys:
                rid = row_id(shop.id, key, f)
                if rid in marks:
                    return rid, *marks[rid]
            return None

        def mark(f) -> tuple[str, bool | None, bool, bool]:
            """The file's row id and the list's say on it: (id, say, lasting, present). say None: the rules' (no list,
            or the row's mark as it was written: what the rules ticked then is no choice of yours); True or False: yours
            (a mark changed since it was written, or one whose picked can't be read), and False too for a row the list
            lacks; lasting: a mark you changed, which your choices keep (its picked was read); present: the list has
            the row."""
            nonlocal listed_any
            found = look(f) if marks is not None else None
            if found is None:
                return row_id(shop.id, keys[0], f), (None if marks is None else False), False, False
            rid, on, picked = found
            sel.matched.add(rid)
            listed_any = True
            if picked is None:  # no picked to tell yours by: the mark counts, for this run alone
                return rid, on, False, True
            return (rid, None, False, True) if on == picked else (rid, on, True, True)

        def row(x: tuple, verdict: str, rid: str, out: bool, there: bool = False) -> None:
            sel.rows.append((t, x[1], x[2], verdict, out, rid, there or verdict in ("done", "changed")))

        # A which-file question's mark moved by a list: you unticked the guess, and ticked exactly one other of its
        # files, both marks changed from what the rules ticked as it was written (picked; with none to tell by, no
        # answer: that run's alone). That answers the question with that file (the cli makes the answer, as hylly
        # decide does, by the option's number): the guess isn't fetched (one here already stays as it is) nor left
        # out, and the file ticked is (behind). (the guess's key, the option's number from 0, the options)
        moved_to = None
        open_q = ("which-file", str(t.key), "") in asked
        for f in t.files if marks is not None and cid else ():
            unsure = getattr(f, "unsure", ())
            if not unsure or not (fresh or open_q or getattr(f, "reopen", False)):
                continue
            options, guess = unsure[0], unsure[1]
            if not (isinstance(guess, int) and 0 <= guess < len(options)):
                continue
            left_over = [f, *getattr(f, "fallbacks", ())]  # each option's file, by its name and size, one each

            def file_of(o: dict):
                x = next((x for x in left_over if x.name == o.get("file") and x.size == o.get("size")), None) or next(
                    (x for x in left_over if x.name == o.get("file")), None)
                if x is not None:
                    left_over.remove(x)
                return x

            by_option = [file_of(o) for o in options]
            changed = lambda x, on: x is not None and (look(x) or ("", None, None))[1:] == (on, not on)  # noqa: E731
            to = [n for n, x in enumerate(by_option) if n != guess and changed(x, True)]
            if changed(by_option[guess], False) and len(to) == 1:
                moved_to = (str(by_option[guess].key), to[0], options)

        def take(x: tuple, verdict: str, ask_of=None) -> bool:
            """A file the rules would fetch (verdict pending, or changed: the shop's newer one), or hold back for your
            answer: by your choices (left out, a question's hold), or where a list says so, by yours. ask_of: its
            question, found as a plain run finds it: (kind, which, options, guess, why), or None for none. Whether it
            is wanted (to download)."""
            f = x[1]
            rid, say, lasting, present = mark(f)
            out = str(f.key) in mine
            unsure = getattr(f, "unsure", ())  # the shop's own rules unsure of the file: (options, guess, why)
            # its question: a title's file with no book of it here yet, or while the question is open (a book of it may
            # have come since, another of its files a list ticked: the guess stays held back with ask until answered)
            ask_of = ask_of or (("which-file", "", *unsure) if unsure and (fresh or open_q) else None)
            holds = bool(ask_of) and bool(cid) and ask  # the rules hold it back for your answer (unsure = "ask")
            row(x, "held" if holds else verdict, rid, out)
            if moved_to and str(f.key) == moved_to[0]:  # the guess, its mark moved: the question answered, not fetched
                if ask_of:
                    question(*ask_of, pending=False)
                sel.answers.append((t, moved_to[1], moved_to[2]))
                return False
            if say is True:  # yours, ticked: taken in this run, its question found, never held back; one you left out
                if ask_of:  # is yours again
                    question(*ask_of, pending=False)
                if out and lasting:
                    sel.ticked_back.append((t, f, False))
                want(x)
                return True
            if say is False:  # yours, unticked, or a row the list lacks: not taken in this run
                if ask_of:
                    question(*ask_of, pending=False)
                if lasting and not out:  # the rules had ticked it: left out by you from now on
                    sel.unticked.append((t, f, False))
                if out or lasting:
                    sel.yours += 1
                else:  # no picked to tell yours by: for this run alone (a row deleted: nothing)
                    sel.only_now += present
                return False
            if out:  # left out by you: no question, never fetched
                sel.yours += 1
                return False
            if ask_of and question(*ask_of, pending=True):  # held back, as the rules have it with ask
                return False
            want(x)
            return True

        def done_row(x: tuple) -> None:
            """A file here already: done, whatever its mark; unticked in a list where the rules had ticked it, left out
            by you from now on (should it go, it isn't fetched again)."""
            rid, say, lasting, _ = mark(x[1])
            out = str(x[1].key) in mine
            row(x, "done", rid, out)
            sel.done += 1
            sel.every.append((*x[:3], "done"))
            unsure = getattr(x[1], "unsure", ())
            if moved_to and str(x[1].key) == moved_to[0]:  # the guess, here already, its mark moved: it stays as it is,
                if unsure:  # and the question is answered with the file ticked; nothing is left out
                    question("which-file", "", *unsure, pending=False)
                sel.answers.append((t, moved_to[1], moved_to[2]))
            elif say is False and lasting and not out:
                sel.unticked.append((t, x[1], True))
            elif say is True and lasting and out:
                sel.ticked_back.append((t, x[1], True))

        def moved_row(x: tuple) -> None:
            """A file you said you moved by hand: never fetched again, unless a list ticks it."""
            rid, say, lasting, _ = mark(x[1])
            out = str(x[1].key) in mine
            row(x, "moved", rid, out)
            if say is True:
                if out and lasting:
                    sel.ticked_back.append((t, x[1], False))
                want(x)
            elif out:
                sel.yours += 1
            else:
                sel.forgotten += 1

        def behind(x: tuple) -> None:
            """The formats behind a chosen file (File.fallbacks: Humble's), each a row of its own the rules never tick,
            at its place in the title's folder (make_job's): fetched only when a list ticks it and it isn't here."""
            parent = own or (folder if x[2] == folder else str(PurePosixPath(x[2]).parent))
            for other in getattr(x[1], "fallbacks", ()):
                if _why(other, kind_of(other.name)):
                    continue
                at = f"{parent}/{other.key}"
                rid, say, lasting, _ = mark(other)
                out, e2 = str(other.key) in mine, manifest.get(at)
                there = bool(e2) and whole(e2)
                sel.rows.append((t, other, at, "behind", out, rid, there))
                if say is True and not there:
                    if out and lasting:
                        sel.ticked_back.append((t, other, False))
                    want((t, other, at, False))

        for f in t.files:
            place = f"{own}/{f.key}" if own else ""
            e = manifest.get(place) if own else recorded.get((t.key, f.key))
            # done: its newest record whole, as the shop lists it now (_same); or, but where a changed file comes beside
            # the old one, any of its records so (a copy fetched again elsewhere, the first one whole again); by its
            # place, the one record of that path
            copies = [] if not e else [e] if shop.beside or own else every[(t.key, f.key)]
            done = next((r for r in copies if _same(r, f) and whole(r)), None)
            if not f.name:  # named only in its link: by the name its whole copy has, or the one its link gave before
                if done:
                    f = replace(f, name=PurePosixPath(done["path"]).name)
                else:  # new, or its copy missing or cut short: its link is asked for what it names now (Naming)
                    learnt = left_out(root, shop.id) if learnt is None else learnt
                    known = learnt.get((t.key, f.key))
                    given = known["name"] if known and known.get("size") == f.size else ""  # listed as then, or asked again
                    if given and (not e or _why(replace(f, name=given), kind_of(given))):  # recorded: only a name left out
                        f = replace(f, name=given)
            kind = kind_of(f.name)
            why = _why(f, kind)
            if e and not why:  # saved before under a name of its own: that name's kind counts too
                kind = kind_of(PurePosixPath(e["path"]).name)
                why = _why(f, kind)
            if why:
                left.append(Left(t.name, f.name, kind, bool(e) and (root / e["path"]).is_file(), (t.key, f.key), why))
                continue
            if e:
                same = _same(e, f)
                if done:
                    x = (t, f, done["path"], False)
                    done_row(x)
                elif same or not (shop.beside or whole(e)):  # recorded, but missing or cut short on the disk: the same place
                    x, ask_of = (t, f, e["path"], False), None  # (Naming asks its link, for one named only in it)
                    if not lost and not e.get("gone") and not (root / e["path"]).exists() \
                            and str(f.key) not in mine:  # gone: moved by hand? (one you left out asks nothing)
                        which = str(f.key) if (root / (own or folder)).is_dir() else ""  # its folder gone: the title's
                        said = curation.moved(answered, f.key)  # the file's answer, else the title's
                        if said == "moved":  # you moved it: never fetched again
                            moved_row(x)
                            behind(x)
                            continue
                        why = (f"{PurePosixPath(e['path']).name} is gone from its folder, {own or folder}" if which else
                               f"its folder, {own or folder}, is gone from the library")
                        ask_of = None if said else ("moved", which, list(curation.MOVED), 0, why)
                    take(x, "pending", ask_of)
                elif shop.beside:  # changed: the new one beside the old, which stays
                    name = free_name(safe_filename(f.name), names)
                    x = (t, f, f"{folder}/{name}", False)
                    if take(x, "changed"):
                        noted.append(f"  {t.name!r}: {f.name!r} was replaced on {shop.label}: the new one comes as {name!r}, "
                                     f"beside the old one, {PurePosixPath(e['path']).name!r}, which stays")
                elif update:
                    x = (t, f, e["path"], True)
                    take(x, "changed")
                else:
                    x = (t, f, e["path"], False)
                    rid, say, lasting, present = mark(f)
                    out = str(f.key) in mine
                    row(x, "changed", rid, out)
                    if say is False or (out and say is not True):  # left out by you, or unticked by the list
                        if out or lasting:
                            sel.yours += 1
                        else:
                            sel.only_now += present
                        if say is False and lasting and not out:
                            sel.unticked.append((t, f, True))
                    else:  # only --update fetches it, ticked or not
                        sel.changed += 1
                        sel.every.append((t, f, e["path"], "changed"))
                        if say is True and out and lasting:
                            sel.ticked_back.append((t, f, True))
                behind(x)
                continue
            there, moved = here(t, f, own, answered)
            if moved:  # the very file you moved by hand, listed anew: where you put it
                x = (t, f, there, False)
                moved_row(x)
            elif there:  # listed anew as it was, or under another name: the file is here already
                x = (t, f, there, False)
                done_row(x)
                noted.append(f"  {t.name!r}: {f.name!r} is the same file as {PurePosixPath(there).name!r}, downloaded "
                             "before: not fetched again")
            elif not f.name:  # its name comes with its link, and its place in the title's folder with it (Naming)
                x = (t, f, folder, False)
                take(x, "pending")
            else:
                x = (t, f, place or f"{folder}/{free_name(safe_filename(f.name), names)}", False)
                take(x, "pending")
            behind(x)
        for f in t.files:  # a question still open, or opened again (File.reopen: what you kept is offered among others
            unsure = getattr(f, "unsure", ())  # now), of a file not to download now: found, never held back
            if unsure and (("which-file", str(t.key), "") in asked or getattr(f, "reopen", False)) and not any(
                    q["kind"] == "which-file" and q["item"] == str(t.key) for q in sel.questions):
                question("which-file", "", *unsure, pending=False)
        if marks is not None and not listed_any and any(r[0] is t for r in sel.rows):
            sel.unlisted.append(t)
        rows.append((t, wanted, noted))
        sel.left += left

    # In their order, but a file with another format standing in for it after every other, then up to limit titles,
    # and up to max_gb: what a title past them would say of a file it changed comes with that file, in a later run
    standing = set()
    for _, wanted, _ in rows:
        for x in wanted:
            stands = _instead(x, manifest, whole)
            if stands:
                standing.add(id(x))
                sel.standing.append((*x[:3], *stands))
    queue = [x for _, wanted, _ in rows for x in wanted if id(x) not in standing]
    queue += [x for _, wanted, _ in rows for x in wanted if id(x) in standing]
    taken = list(dict.fromkeys(_ident(x[0]) for x in queue))
    taken = set(taken if limit is None else taken[:limit])
    sel.pending = [x for x in queue if _ident(x[0]) in taken]
    sel.noted = [line for t, wanted, noted in rows if not wanted or _ident(t) in taken for line in noted]
    if max_gb is not None:
        budget, kept = max_gb * GB, []  # a disk's GB, as --max-gb is named: words.py
        for x in sel.pending:
            if budget - x[1].size < 0 and kept:
                break
            budget -= x[1].size
            kept.append(x)
        sel.pending = kept
    sel.substituted = sum(id(x) in standing for x in sel.pending)
    return sel


def _instead(x: tuple, manifest, whole) -> tuple | None:
    """The first of the title's other formats (File.fallbacks) standing in for a file still to download: downloaded
    instead of it (its record's instead_of is the file's place) and whole on the disk, with its place; None for none,
    and for a file to be fetched again in its place (--update), which nothing stands in for. One the shop has changed
    since stands too, as it is (download.substitute)."""
    t, f, path, newer = x
    folder = str(PurePosixPath(path).parent)
    for other in [] if newer else getattr(f, "fallbacks", ()):
        e = manifest.get(f"{folder}/{other.key}")
        if e and e.get("instead_of") == path and whole(e):
            return other, f"{folder}/{other.key}"
    return None


def noted(shop_id: str, sel: Selection) -> list[str]:
    """What a run says, every run, of the files the shop changed that come beside the old ones (Shop.beside), and those
    it lists anew that are here already."""
    return [f"Changed on {SHOPS[shop_id].label}:", *sel.noted] if sel.noted else []


class Naming:
    """The files select chose that the shop names only in their links (File.name ""), each named as its download starts
    (download.Downloader's name), by the link it asks for then (inventory.link_name), which the download then takes: a
    link request a file, asked for right before its download as every file's is (StoryBundle's live a minute), so that
    the first one also fetches it. One select chose by the place of its copy recorded but missing or cut short (a path
    in manifest, the shop's download records) goes back to that place when its link names a file of the same kind, and
    else comes under the link's name in that folder: the shop may have put another format in its place, which the old
    name's kind would refuse (download.FIRST_BYTES). One its link names as a kind no download takes, or the shop's own
    rules leave out (File.left), is left out and named (a download.Left), never fetched, and kept by that name in the
    shop's LEFT_OUT (keep), so that the runs after it, dry runs too, leave it out at once, without asking for its link
    or counting it, for as long as the shop lists it with the same size (one listed otherwise is asked about again). One
    the shop gives no link for fails (download.FileAtFault), listed by its title's folder (its name isn't known) or its
    copy's place, and the next run asks again; a link turned away, or not answered, counts as a fresh one's does
    (download.REFUSED_IN_A_ROW, UNANSWERED_IN_A_ROW). link: the shop's Library.link; of: each job's title and file, by
    the job's id (the run's map of them, which the named file then takes its place in)."""

    def __init__(self, shop_id: str, root: Path, manifest, link, of: dict[int, tuple]):
        self.shop_id, self.root, self.manifest, self.link, self.of = shop_id, root, manifest, link, of
        self.kept = left_out(root, shop_id)
        self._before = dict(self.kept)
        self._taken: dict[str, set[str]] = {}  # each folder's file names: its records', and those given in this run
        self._lock = threading.Lock()  # the downloads name their files side by side

    def __call__(self, job) -> Left | None:
        """The job named by its first link, in place: its name, its place and what they tell (make_job), and the link,
        which its download takes (Job.url); or, for a file no download takes, what it is left out as."""
        from .download import FileAtFault, Left, kind_of
        from .inventory import link_name
        from .paths import safe_filename
        from .plan import free_name

        t, f = self.of[id(job)]
        with self._lock:
            self.kept.pop((t.key, f.key), None)  # asked about again: whatever it is now
        given = self.link(t, f)
        if not given:
            raise FileAtFault("no download link")
        f = replace(f, name=link_name(given))
        kind, path = kind_of(f.name), job.path
        why = _why(f, kind)
        copy = self.manifest.get(path)  # chosen by its copy's place: recorded there, but missing or cut short
        folder = str(PurePosixPath(path).parent) if copy else path
        if why:  # never fetched, nor its copy's place written
            there = (self.root / path).is_file() if copy else (self.root / path / safe_filename(f.name)).is_file()
            with self._lock:
                self.kept[(t.key, f.key)] = {"name": f.name, "size": f.size}
            return Left(t.name, f.name, kind, there, (t.key, f.key), why)
        if not (copy and kind == kind_of(PurePosixPath(path).name)):
            with self._lock:
                if folder not in self._taken:  # the names its folder's records have: the old copy stays as it is
                    recorded = tuple(self.manifest.entries)  # at once: the downloads beside it add to them
                    self._taken[folder] = {PurePosixPath(p).name.lower() for p in recorded
                                           if str(PurePosixPath(p).parent) == folder}
                path = f"{folder}/{free_name(safe_filename(f.name), self._taken[folder])}"
        named = make_job(self.shop_id, t, f, path, job.replace)
        named.url = given  # its first link fetches it
        vars(job).update(vars(named))  # the run's own job, named
        self.of[id(job)] = (t, f)
        return None

    def keep(self) -> None:
        """What the run learnt of the files left out by their links' names, written to LEFT_OUT once it is over, for the
        runs after it; only while the library is there, or it would go where an unmounted disk was."""
        from .paths import library_missing

        with self._lock:
            if self.kept != self._before and not library_missing(self.root):
                _keep_left_out(self.root, self.shop_id, self.kept)
                self._before = dict(self.kept)


def left_out(root: Path, shop_id: str) -> dict:
    """The files a shop names only in their links that a run left out by the names their links gave (Naming): {(the
    title's key, the file's key): {"name": that name, "size": the size the shop listed then}}. {} for none, and for a
    file that can't be read just now: those are asked about again."""
    from .paths import state_dir

    try:
        rows = json.loads((state_dir(root) / SHOPS[shop_id].state / LEFT_OUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ok = lambda r: (isinstance(r, dict) and isinstance(r.get("item"), (str, int)) and isinstance(r.get("file"), (str, int))  # noqa: E731
                    and isinstance(r.get("name"), str))
    return {(r["item"], r["file"]): {"name": r["name"], "size": r.get("size")} for r in rows if ok(r)} if isinstance(rows, list) else {}


def _keep_left_out(root: Path, shop_id: str, kept: dict) -> None:
    """What left_out reads, written whole: by the title's and the file's key, the name its link gave and the size then."""
    from .paths import state_dir
    from .storage import atomic_write

    rows = [{"item": item, "file": file, **kept[(item, file)]} for item, file in sorted(kept, key=lambda k: (str(k[0]), str(k[1])))]
    atomic_write(state_dir(root) / SHOPS[shop_id].state / LEFT_OUT, json.dumps(rows, indent=1, ensure_ascii=False))


def make_job(shop_id: str, t, f, path: str, replace: bool = False):
    """The downloader's job for a file select chose (for a file the shop names only in its link, made again by Naming
    once its first link has named it). Its link is asked for right before the download starts. Its record names the
    title and the file as every shop's records do (item, file), who made it (publisher), and what the shop listed: its
    MD5 (listed_md5), and its size where it lists no MD5 and the size isn't a rounded one (listed_size; File.rough),
    which tell a changed file next time (current); its size always where its File keeps it (File.keep_size: itch.io's,
    so that a new size still tells a changed file if the shop stops listing its MD5); and what the title's own records
    keep (Title.record, never in place of those).
    bundle: the folder the title's own is in, under the shop's (under). kind: the kind it is found to be, or yours
    (kind_found). device: where it is best read, by that kind, its extension and its size (plan.device_for), or as the
    shop's File says where its size says nothing of that (File.device: itch.io's ZIP package, by its formats; Humble's,
    by its plan). ext: by the place's name, or as the shop's File says (File.ext: Humble's file named without one, by its
    label). url: a link the listing gave (File.url: Humble's, fresh from its orders), tried before a fresh one is asked
    for.

    A shop that places its files itself (Title.folder: Humble's) keeps its records as they were before the core: the
    title's key as its shop_id, item and file, its label as the shop gives it, its bundle as the shop names it (blank
    too, not who made it), and what the shop listed recorded by the download where what came differs, as for any shop
    that lists each file exactly (Shop.exact: download.py's _entry). Any title with a folder is taken so: a new shop
    gives its titles one only to place its files itself and keep this shape of record. The title's other formats behind the file (File.fallbacks) come as the job's fallbacks, each at its place
    in the title's folder and recorded as instead of this file (instead_of), should it come in its place."""
    from .download import Job
    from .plan import device_for

    shop = SHOPS[shop_id]
    ext, found = getattr(f, "ext", "") or PurePosixPath(path).suffix.lower().lstrip("."), kind_found(shop_id, t)
    own, folder, placed = getattr(f, "device", None), str(PurePosixPath(path).parent), bool(getattr(t, "folder", ""))
    if placed:
        ident, label, extra = str(t.key), f.label, {"item": t.key, "file": f.key}
    else:
        ident, label = f"{shop.id}:{t.key}:{f.key}", f.label or ext.upper()
        extra = {**getattr(t, "record", {}),  # first: what every shop's records have is the core's, whatever a title says
                 "source": shop.id, "item": t.key, "file": f.key, "listed_md5": f.md5, "publisher": t.by,
                 **({"listed_size": f.size} if getattr(f, "keep_size", False)
                    or (f.size and not f.md5 and not getattr(f, "rough", False)) else {})}
    job = Job(
        path=path, folder=folder, url=getattr(f, "url", ""), filename=f.name, size=f.size, md5=f.md5, role=f.role,
        label=label, ext=ext, title=t.name, bundle=t.under if placed else under(shop.id, t), shop_id=ident, kind=found,
        device=own(found, ext) if callable(own) else device_for(found, ext, f.size)[0], purchased=t.purchased,
        exact=shop.exact, replace=replace, extra=extra,
    )
    for other in getattr(f, "fallbacks", ()):
        behind = make_job(shop_id, t, other, f"{folder}/{other.key}")
        behind.extra["instead_of"] = path
        job.fallbacks.append(behind)
    return job


def entry(t) -> dict:
    """A title as the snapshot keeps it: no links, nor anything of the account's own (its order numbers). A file the
    shop names only in its link has no name here ("")."""
    return {"id": t.key, "name": t.name, "publisher": t.by, **({"kind": t.kind} if t.kind else {}), "purchased": t.purchased,
            **({"under": t.under} if getattr(t, "under", "") else {}), **t.details,
            "files": [{"index": f.key, "filename": f.name, "md5": f.md5, "size": f.size,
                       **({"label": f.label} if f.label else {}), **({"role": f.role} if f.role != "book" else {}),
                       **({"rough": True} if getattr(f, "rough", False) else {})}
                      for f in t.files]}


def snapshot(shop_id: str, titles: list, state: Path, unreadable: list[tuple[str, str]] = (), manifest=None,
             thinned=None) -> Path:
    """What the shop listed, for the catalog, which builds the shop's titles from the newest snapshot alone; the older
    ones thinned, which thinned says (snapshot.put). A title the listing gave in a shape that couldn't be read (Library.unreadable) keeps
    its entry from the snapshot before, and so stays in the catalog as it was. manifest: the downloads' records, which
    know who made one that has no entry before."""
    from . import snapshot as snapshots

    data = [entry(t) for t in titles]
    if unreadable:
        makers = {e.get("item"): e["publisher"] for e in (manifest.entries.values() if manifest else ())
                  if isinstance(e.get("publisher"), str) and e["publisher"].strip()}
        data += _kept(shop_id, state, {t.key for t in titles}, unreadable, makers)  # read before the new one may take its name
    return snapshots.put(state, shop_id, json.dumps(data, indent=1, ensure_ascii=False), thinned)


def _kept(shop_id: str, state: Path, listed: set[str], unreadable: list[tuple[str, str]], makers: dict[str, str]) -> list[dict]:
    """The entries of the snapshot before for the titles that couldn't be read. One with no entry there gets a bare
    one, with its name and who made it as its downloads were recorded, or the shop's word for none (Shop.maker): the
    catalog takes its files from the records. When even a title's key couldn't be read, every entry of the snapshot
    before that this listing lacks is kept, as any of them may be that title; so a title gone from the shop then stays
    in the catalog until the listing can be read whole again."""
    from .snapshot import latest

    before = latest(state, shop_id)
    earlier = before[1] if before else []
    wanted, every, seen, kept = {key for key, _ in unreadable if key}, any(not key for key, _ in unreadable), set(listed), []
    for e in earlier if isinstance(earlier, list) else []:
        key = e.get("id") if isinstance(e, dict) else None
        if isinstance(key, str) and key not in seen and (every or key in wanted):
            kept.append(e)
            seen.add(key)
    for key, name in unreadable:
        if key and key not in seen:
            kept.append({"id": key, "name": name, "publisher": makers.get(key, SHOPS[shop_id].maker), "purchased": "",
                         "files": []})
            seen.add(key)
    return kept


def unread(shop_id: str, unreadable: list[tuple[str, str]]) -> str:
    """What a run says of the titles the listing gave in a shape that can't be read: up to five by name."""
    names = ", ".join(name for _, name in unreadable[:5]) + (" and more" if len(unreadable) > 5 else "")
    return (f"Left out of this run's downloads, as {SHOPS[shop_id].label} lists them in a shape Hylly can't read "
            f"({len(unreadable)}): {names}. What the library page had of them stays there; if this stays, Hylly may need an "
            "update.")
