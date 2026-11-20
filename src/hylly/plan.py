"""Humble's plan: one master file per title (the rules', or the file or format you chose; where the rules were unsure,
which, as a question for you: unsure), the title's other formats behind it and its extras, what kind of book it
is (the rules' Rules.kind_of, as every shop's titles, by every format Humble offers of it, or your correction), where it
is read, and the folder its files go in, from the orders (a snapshot's or Humble's answer), for Humble's download
(humble.titles_of) and the catalog (catalog.humble_items). Where a title of any shop is best read (device_for) is here
too, worked out by that one function for every title, and so is how a title's new folder or file gets a name none has
yet (free_folder, free_name); the names themselves are every shop's (paths.clean, paths.safe_filename)."""

from __future__ import annotations

from pathlib import PurePosixPath
from dataclasses import dataclass, field, replace

from . import rules, settings
from .inventory import FileOption, Product, parse_orders
from .paths import clean, safe_filename
from .shops import SHOPS
from .words import MB, size_words


@dataclass
class Title:
    product: Product  # the copy that gets downloaded
    copies: list[Product]  # every purchase of this title, the downloaded copy included
    kind: str = ""  # one of the kinds in the rules ([kinds.X]): the rules' (found), or your correction (hylly set kind)
    kind_reason: str = ""
    found: str = ""  # the kind the rules find, before your correction: the catalog's (catalog.humble_items)
    choice: FileOption | None = None
    choice_reason: str = ""
    device: str = ""  # ereader | either | tablet
    device_reason: str = ""
    extras: list[FileOption] = field(default_factory=list)
    alternatives: list[FileOption] = field(default_factory=list)  # the other formats, best first: tried when the choice fails
    folder: str = ""  # relative to the library root, forward slashes
    better: str = ""  # kept in the format it was downloaded in, where its list would take this one now (kept_behind)
    # Where the rules are unsure of its file (unsure, a question for you): "several" files of the format chosen,
    # or "none" of its kind's formats offered; "" where they are sure, or you chose (a file or a format of yours). With
    # the files the choice was between (options)
    unsure: str = ""
    options: list[FileOption] = field(default_factory=list)
    reopen: bool = False  # the file you kept as the rules took it is now offered among other files: asked again

    @property
    def path(self) -> str:
        return f"{self.folder}/{safe_filename(self.choice.filename)}" if self.choice else ""


@dataclass
class Plan:
    products: list[Product]  # every item of every purchase
    titles: list[Title]


def for_library(orders: dict, root) -> Plan:
    """The plan for this library, with the files, the formats and the kinds you chose (hylly set file, hylly set format,
    hylly set kind, or your answers: hylly decide) and the formats already downloaded."""
    from . import curation
    from .manifest import Manifest

    entries = list(Manifest(root, "humble").entries.values())
    return build(orders, formats=curation.formats(root), folders=recorded_folders(root, entries),
                 kept=kept_formats(entries), kinds=curation.kinds(root, "h"), files=curation.files(root),
                 keeps=curation.kept_files(root))


def recorded_folders(root, entries: list[dict] | None = None) -> dict[str, str]:
    """Where each Humble id's files are, from Humble's download records (shop_id is the Humble id there): a title stays
    there whatever changes around it. entries: those records, read already."""
    if entries is None:
        from .manifest import Manifest

        entries = list(Manifest(root, "humble").entries.values())
    return {e["shop_id"]: str(PurePosixPath(e["path"]).parent) for e in entries if e.get("shop_id") and e.get("path")}


def kept_formats(entries: list[dict]) -> dict[str, set[str]]:
    """The formats each Humble id is downloaded in as its own choice, from its download records: a title's file, not an
    extra, and not one downloaded instead of the choice (instead_of), which a later run tries again. Such a title stays
    on its format while Humble offers it (settings.pick_file), so a change of the order downloads nothing again."""
    out: dict[str, set[str]] = {}
    for e in entries:  # by the record alone, no file looked at: a share away, or a file gone, changes no pick
        if e.get("shop_id") and e.get("path") and e.get("role", "book") == "book" and not e.get("instead_of"):
            out.setdefault(e["shop_id"], set()).add((e.get("ext") or PurePosixPath(e["path"]).suffix[1:]).lower())
    return out


def build(orders: dict, formats: dict[str, str] | None = None, folders: dict[str, str] | None = None,
          kept: dict[str, set[str]] | None = None, kinds: dict[str, str] | None = None,
          files: dict[str, str] | None = None, keeps: dict[str, tuple[str, str]] | None = None) -> Plan:
    """formats: Humble id -> the format to keep for that title instead of the automatic choice. folders: Humble id -> the
    folder its files are in already. kept: Humble id -> the formats it is downloaded in already (kept_formats). kinds:
    Humble id -> the kind you chose for it instead of the rules' (hylly set kind). files: Humble id -> the file to keep,
    by its name as Humble lists it (hylly set file, or your answer to a question: hylly decide), over a format; for an
    item with no book of its own, the file that makes it a title (as_books). keeps: Humble id -> a file you kept as the
    rules took it (keep, on a which-file question) and what the question offered then (curation.kept_files)."""
    products, _ = parse_orders(orders)
    as_books(products, files or {})
    titles = []
    for group in group_titles(products):
        rep = max(group, key=lambda p: (len({f.ext for f in p.books}), sum(f.size for f in p.books)))
        titles.append(decide(Title(product=rep, copies=group), formats or {}, kept or {}, kinds or {}, files or {}, keeps or {}))
    titles.sort(key=lambda t: (t.product.purchased, t.product.bundle.lower(), t.product.title.lower()))
    assign_folders(titles, folders or {})
    return Plan(products=products, titles=titles)


def as_books(products: list[Product], files: dict[str, str]) -> None:
    """An item with no book of its own (only extras, or files of formats the rules don't know as books: never a title by
    itself) whose file you chose (files: its Humble id -> the file's name as Humble lists it) has that file as its book,
    so it becomes a title, downloaded and in the catalog as any: the answer to whether it is one (hylly decide)."""
    for p in products:
        name = files.get(p.machine_name)
        if name and not p.books and any(f.filename == name for f in p.files):
            p.files = [replace(f, role="book") if f.filename == name else f for f in p.files]


def kept_behind(plan: Plan) -> list[Title]:
    """The titles kept in the format they were downloaded in, though one earlier on their list is offered now (a CBZ
    for a comic downloaded as CBR, an EPUB Humble added to a book downloaded as MOBI): nothing downloads it by itself,
    so a change of the order downloads nothing again; hylly set format moves one (hylly humble download counts them)."""
    return [t for t in plan.titles if t.better]


def norm_title(title: str) -> str:
    """A Humble title as titles are compared: its words run together, & as and, as rules.words_of reads words (small
    letters without accents, of any script), so "Tähti" is "tahti" and a Cyrillic or Japanese title isn't empty. One in
    plain ASCII gives what it always did, so no grouping of those changes. No NFKC here, unlike lookups.norm: it would
    make ™ "tm", and two purchases of one file titled "Foo™" and "Foo" would stop being one title and download twice."""
    return "".join(rules.words_of(title.replace("&", "and")))


def group_titles(products: list[Product]) -> list[list[Product]]:
    """Products that are the same title: the same Humble id, or an identical file under the same title."""
    books = [p for p in products if p.books]
    parent = list(range(len(books)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_seen: dict[tuple, int] = {}
    for i, p in enumerate(books):
        idents: list[tuple] = [("id", p.machine_name)] if p.machine_name else []
        idents += [("md5", f.md5, norm_title(p.title)) for f in p.books if f.md5]
        for ident in idents:
            if ident in first_seen:
                parent[find(i)] = find(first_seen[ident])
            else:
                first_seen[ident] = i
    groups: dict[int, list[Product]] = {}
    for i, p in enumerate(books):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def decide(t: Title, formats: dict[str, str] | None = None, kept: dict[str, set[str]] | None = None,
           kinds: dict[str, str] | None = None, files: dict[str, str] | None = None,
           keeps: dict[str, tuple[str, str]] | None = None) -> Title:
    """A title's kind, its file in the format its kind's list keeps (or yours: a file, then a format; or the one it is
    downloaded in already: a kind of yours never downloads one again), the formats behind it, its extras, where it is
    read, and whether the rules were unsure of its file (unsure). Your kind, given for any of its purchases, wins over
    the rules': it chooses by its kind's list, and is read where it is. A file you kept as the rules took it (keeps)
    stays the choice while it is among those they are unsure between; once they are unsure between other files than
    then (a file added), the question is asked again (reopen), that file still the guess."""
    p = t.product
    ids = [c.machine_name for c in [p, *t.copies]]  # the downloaded copy's first
    t.found, t.kind_reason = classify(p)
    yours = next((kinds[i] for i in ids if i in (kinds or {})), None)
    t.kind = yours or t.found
    if yours:
        found = f"{t.found}: {t.kind_reason}" if t.kind_reason else t.found
        t.kind_reason = "your correction" + (f" (the rules find {found})" if yours != t.found else "")
    prefer = next((formats[i] for i in ids if i in (formats or {})), None)
    had = next((kept[i] for i in ids if i in (kept or {})), ())
    wanted = next((files[i] for i in ids if i in (files or {})), None)
    mine = next((f for f in p.books if f.filename == wanted), None) if wanted else None
    if mine:  # the very file you chose, over a format and the rules, while Humble lists it so
        t.choice, t.choice_reason = mine, f"{mine.filename}: your choice"
    else:
        t.choice, t.choice_reason = choose(t.kind, p.books, prefer, had)
    if not mine and t.choice.ext in had and t.choice.ext != prefer:  # kept, not chosen by you: a better format offered now?
        by_order = choose(t.kind, p.books)[0].ext
        t.better = by_order if by_order != t.choice.ext else ""
    t.device, t.device_reason = device(t.kind, t.choice)
    t.extras = p.by_role("extra")
    r = rules.current()
    parts = [f for f in p.books if r.part_label.search(f.label)]
    if len(parts) > 1 and t.choice in parts:  # a book in several parts: all of them downloaded
        t.extras += [f for f in parts if f is not t.choice]
    elif not mine and not (prefer and any(f.ext == prefer for f in p.books)):  # the rules' choice, not yours
        t.unsure, t.options = unsure(t.kind, p.books, t.choice)
        kept = next((keeps[i] for i in ids if i in (keeps or {})), None)
        ours = next((f for f in t.options if f.filename == kept[0]), None) if kept and t.unsure else None
        if ours is not None and ours is not t.choice:  # kept as the rules took it then: it stays, while it is offered
            t.choice, t.choice_reason = ours, f"{ours.filename}: kept as the rules took it"
            t.device, t.device_reason = device(t.kind, t.choice)
        if kept and t.unsure:  # settled while it offers the same files; asked again once it offers others
            from .curation import fingerprint

            if fingerprint([{"file": f.filename, "size": f.size} for f in t.options]) == kept[1]:
                t.unsure, t.options = "", []
            else:
                t.reopen = True
    t.alternatives = [f for f in ranked(t.kind, p.books) if f is not t.choice and not any(f is x for x in t.extras)]
    return t


def unsure(kind: str, books: list[FileOption], choice: FileOption) -> tuple[str, list[FileOption]]:
    """Whether the rules were unsure of the file they chose for a title (a question for you, which a download
    guesses or holds back: [download] unsure), and between which files: "none", none of the formats on its kind's list
    is offered (the largest file of any is taken), between them all; "several", several files of the format chosen
    (the largest is taken: a PDF and a PDF (HQ), say), between those; else "". Only the files in the language kept
    count (settings.in_language). "largest" on a list chooses between formats, by your rule; several files of the
    format it takes are asked about all the same, as any several files of one format are."""
    files = settings.in_language(books)
    order = [fmt for fmt in settings.formats_for(kind)[0] if fmt != settings.LARGEST]
    if len(files) > 1 and not any(f.ext in order for f in files):
        return "none", files
    same = [f for f in files if f.ext == choice.ext]
    return ("several", same) if len(same) > 1 else ("", [])


def classify(p: Product) -> tuple[str, str]:
    """The kind the rules find (Rules.kind_of, as every shop's): by every format Humble offers of it, its bundle's name
    and its publisher, over Humble's own kind, a book."""
    return rules.current().kind_of(SHOPS["humble"].kind, [p.bundle], p.publisher, [(f.ext, f.size) for f in p.books])


def ranked(kind: str, books: list[FileOption]) -> list[FileOption]:
    """Every file, in the order choose() would prefer them."""
    left, out = list(books), []
    while left:
        best, _ = choose(kind, left)
        out.append(best)
        left = [f for f in left if f is not best]
    return out


def choose(kind: str, books: list[FileOption], prefer: str | None = None, had=()) -> tuple[FileOption, str]:
    """The master file, by its kind's list of formats under [download] in the settings, as every choice of a title's
    file goes (settings.pick_file); prefer: a format you chose for this title, used when Humble offers it; had: the
    formats it is downloaded in already."""
    return settings.pick_file(kind, books, prefer or "", had)


def device_for(kind: str, ext: str, size: int | None) -> tuple[str, str]:
    """Where a title is best read, and why in a few words, by its kind's device ([kinds.X] device in the rules) and its
    main file ([devices] in the settings): the one way for every title, Humble's, StoryBundle's, DriveThruRPG's,
    itch.io's and your own folders'. size None: its size says nothing of its pages (a ZIP package of several
    formats). Why, by its kind: its own words for it ([kinds.X] why_device), else the device's."""
    d, found = settings.get("devices"), rules.current().kinds.get(kind)
    own, label = (found.device, found.word) if found else ("ereader", kind)
    because = f"{label}: {found.why_device}" if found and found.why_device else ""
    if own == "tablet":
        return "tablet", because or label
    if ext in rules.current().fixed_formats and d["fixed_layout"] != "ereader":
        return d["fixed_layout"], f"{ext.upper()} only (fixed layout)"
    if size is not None and size > d["image_heavy_mb"] * MB:  # a disk's MB, as the setting is named: words.py
        return "tablet", f"{ext.upper()} is {size_words(size)}, probably image-heavy"
    if own == "either":
        return "either", because or f"{label}: fine on an e-reader, better on a tablet"
    return "ereader", because


def device(kind: str, f: FileOption) -> tuple[str, str]:
    return device_for(kind, f.ext, f.size)


def free_name(name: str, taken: set[str]) -> str:
    """name, or the first of "name (2).ext", "name (3).ext" and so on that a title's folder hasn't got yet (taken: its
    file names in small letters, as Windows and macOS don't tell a.pdf from A.PDF; it then has this one too), so that
    two of its files never share a place. Every shop's new files get their names so."""
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    found, n = name, 1
    while found.lower() in taken:
        n += 1
        found = f"{stem} ({n}).{ext}" if dot else f"{stem} ({n})"
    taken.add(found.lower())
    return found


def free_folder(base: str, used: set[str]) -> str:
    """base, or the first of "base (2)", "base (3)" and so on that no title has yet (used: the folders in small letters,
    which then has this one too): every shop's new titles get their folder so."""
    folder, n = base, 1
    while folder.lower() in used:
        n += 1
        folder = f"{base} ({n})"
    used.add(folder.lower())
    return folder


def assign_folders(titles: list[Title], recorded: dict[str, str] | None = None) -> None:
    """humble/<bundle>/<title> (Humble's folder); titles whose cleaned names collide get (2), (3) and so on. A title with files already
    keeps their folder, under any of its purchases: buying it again, a renamed title or a change in these rules moves
    nothing and downloads nothing again. A new title gets a folder none of them has."""
    used: set[str] = set()
    fresh = []
    for t in titles:
        # the downloaded copy's first: that is where its files are, if more than one purchase has a record
        folder = next((recorded[c.machine_name] for c in [t.product, *t.copies] if c.machine_name in (recorded or {})), None)
        if folder and folder.lower() not in used:
            t.folder = folder
            used.add(folder.lower())
        else:
            fresh.append(t)
    for t in fresh:
        t.folder = free_folder(f"{SHOPS['humble'].folder}/{clean(t.product.bundle)}/{clean(t.product.title)}", used)
