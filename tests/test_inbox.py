"""The Inbox: [inbox] in the settings, a folder (or several) whose books pass through, from wherever they came. Each
pass takes a book once it has stayed the same for two minutes, copies it in as hylly add does into the folder of yours
its kind belongs in, its subfolders there read as its collection, its kind and its tags, checks the copy, asks the
lookup sources about the new books, builds the catalog, and only then removes the original from the Inbox; what can't
be taken goes into the Inbox's Not added folder with a note why. Made-up books, a made-up library and a made-up lookup
source that answers without any network."""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pytest

from helpers import make_comic, make_pdf
from hylly import bell, catalog, cli, copies, curation, doctor, exits, folders, instance, lookups, paths, periodic, runs
from hylly import settings
from hylly.library import Catalog

OLD = 600  # seconds: a book whose time is this old has stayed the same long enough, at the second look


def epub(path: Path, title: str, author: str = "") -> Path:
    """A small EPUB a reader can open, with its title and author."""
    path.parent.mkdir(parents=True, exist_ok=True)
    creator = f"<dc:creator>{author}</dc:creator>" if author else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>{creator}'
                   '<dc:language>en</dc:language></metadata><manifest/></package>')
    return path


def aged(path: Path, seconds: float = OLD) -> Path:
    """Its time put back, as a copy that keeps the time of its original has it."""
    then = time.time() - seconds
    os.utime(path, (then, then))
    return path


class Lantern(lookups.Source):
    """A made-up lookup source: each question gets what answers holds, in turn (an exception is raised), and then a
    found answer with a description. asked: the titles it was asked about."""

    id, name, kinds, interval = "lantern", "Lantern DB", frozenset({"book"}), 0.0
    answers: list = []
    asked: list = []

    def find(self, s, q):
        Lantern.asked.append(q.title)
        what = Lantern.answers.pop(0) if Lantern.answers else {"description": f"A made-up tale of {q.title}.", "score": 1.0}
        if isinstance(what, BaseException):
            raise what
        return what


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    """A library, an Inbox whose books go to its folder ebooks, but for those of a kind the rules give a folder of its
    own (comics, manuals), the made-up source as the only one, and the phone's notes: (the library, the Inbox, the
    notes, and write(**keys) to write [inbox] again, its folder and into as given, and keys after them)."""
    root, box = tmp_path / "library", tmp_path / "inbox" / "ebooks"
    root.mkdir()
    box.mkdir(parents=True)
    monkeypatch.setattr(lookups, "in_order", lambda: {"lantern": Lantern})
    monkeypatch.setattr(Lantern, "answers", [])
    monkeypatch.setattr(Lantern, "asked", [])
    notes: list[tuple] = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: notes.append((title, text, public)) or True)

    def write(folder: Path | str = box, extra: str = "", into: str = '{ default = "ebooks" }', **keys) -> None:
        more = "".join(f"{k} = {json.dumps(v)}\n" for k, v in keys.items())
        paths.settings_file().write_text(f"library = '{root}'\n[inbox]\nfolder = '{folder}'\ninto = {into}\n{more}"
                                         f"{extra}", encoding="utf-8")
        settings.reload()

    write()
    return root, box, notes, write


def a_pass(capsys, root: Path) -> str:
    """One pass, as the server's job starts it: what it said on standard output."""
    cli.main(["internal", "inbox", "--library", str(root)])
    return capsys.readouterr().out


def state(root: Path) -> dict:
    return json.loads(folders.state_file(root).read_text(encoding="utf-8"))


def titles(root: Path) -> dict[str, dict]:
    return {unquote(rec["f"][0][0]): rec for rec in Catalog(root).get()}


def test_a_steady_book_is_taken_copied_looked_up_catalogued_and_removed_with_the_folders_it_leaves_empty(inbox, capsys):
    root, box, notes, _ = inbox
    book = aged(epub(box / "Ann Writer" / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer"))
    (box / "Ann Writer" / ".DS_Store").write_bytes(b"what a Mac leaves")
    assert folders.waiting(root)
    assert a_pass(capsys, root) == "" and book.is_file()  # seen once: not taken yet, however old its time
    said = a_pass(capsys, root)
    copy = root / "ebooks" / "Ann Writer" / "Lantern Wolf.epub"
    assert copy.read_bytes() and not book.exists() and not (box / "Ann Writer").exists() and box.is_dir()  # never the Inbox itself
    assert said.endswith("Inbox: 1 book added from the Inbox, details found for 1.\n")
    assert copies.recorded(root)["ebooks/ann writer/lantern wolf.epub"]["md5"] == copies.md5_of(copy).hexdigest()
    assert copies.verify(root, check_hash=True) == (1, [])
    found = titles(root)["ebooks/Ann Writer/Lantern Wolf.epub"]
    assert found["t"] == "Lantern Wolf" and found["d"] == "A made-up tale of Lantern Wolf." and Lantern.asked == ["Lantern Wolf"]
    assert notes == [(cli.INBOX_NOTE, "1 book added from the Inbox, details found for 1.\nAdded: ebooks/Ann Writer/Lantern Wolf.epub",
                      "1 book added from the Inbox, details found for 1.")]
    log = folders.log_file(root).read_text(encoding="utf-8")
    assert "Removed from the Inbox, as the library has it as ebooks/Ann Writer/Lantern Wolf.epub" in log
    assert state(root)["pending"] == {} and state(root)["done"][-1][1:] == [1, 1, 0, 0]
    assert not folders.waiting(root) and a_pass(capsys, root) == ""  # nothing more to do


# ---- where each book goes: its kind's folder, and its folders in the Inbox read as its collection, kind and tags ----

DROPPED = {  # a book dropped in the Inbox at its path there, and where it goes with into = { default = "collections" }
    "Lantern Wolf.epub": "collections/Lantern Wolf.epub",  # R1: at the top, no collection, its kind by its file
    "Moonwake 001.cbz": "comics/Moonwake 001.cbz",  # a comic by its file, to the folder the rules give comics
    "Harbor Lights/The Lantern Keeper.epub": "collections/Harbor Lights/The Lantern Keeper.epub",  # R2: a collection
    "Harbor Lights/Charts/Tide Tables.epub": "collections/Harbor Lights/Charts/Tide Tables.epub",  # R5: a folder below
    "Comics/Ironclad Hearts/Ironclad Hearts 007.cbz": "comics/Ironclad Hearts/Ironclad Hearts 007.cbz",  # R3: a kind
    "rpgs/Starfall Station/Starfall Core Rules.epub": "collections/Starfall Station/Starfall Core Rules.epub",
    "MANUALS/Kettle/Kettle Guide.pdf": "manuals/Kettle/Kettle Guide.pdf",
    "#To read/Hedge Lore.epub": "collections/Hedge Lore.epub",  # R4: a tag, and no place
    "Science Fiction/#Favourite/Star Seeds.epub": "collections/Science Fiction/Star Seeds.epub",
}


def drop(box: Path, rel: str) -> Path:
    """A made-up book of its path's kind, its time old enough."""
    path = box / rel
    if path.suffix == ".cbz":
        make_comic(path)
    elif path.suffix == ".pdf":
        make_pdf(path, title=path.stem)
    else:
        epub(path, path.stem, "Ann Writer")
    return aged(path)


def test_each_book_goes_where_its_kind_belongs_and_its_folders_say_its_collection_kind_and_tags(inbox, capsys):
    """One Inbox for books of every kind, from wherever they came: a comic goes to comics and a manual to manuals, as
    the rules give those kinds folders, and every other book to into's default. Its first folder in the Inbox is its
    collection, and the genre its words name; one named for a kind (in any case: Comics, rpgs, MANUALS) says its kind,
    and the folder below it is its collection then; a # folder gives it a tag and no place; deeper ones stay folders. An
    RPG book in a folder of books is kept as an RPG book, as your correction of its kind would be, by the Inbox."""
    root, box, notes, write = inbox
    write(into='{ default = "collections" }')
    for rel in DROPPED:
        drop(box, rel)
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    # details found for the books, the RPG book among them: the lookups go by the kind of its folder, as for every
    # correction of a kind
    assert said.endswith(f"Inbox: {len(DROPPED)} books added from the Inbox, details found for 6.\n")
    assert not [p for p in box.rglob("*") if p.is_file()] and box.is_dir()  # emptied, its own folder kept, and the
    # folders at its top kept for the books to come: those named for a kind, and the # ones; the collections' gone
    assert sorted(p.name for p in box.iterdir()) == ["#To read", "Comics", "MANUALS", "rpgs"]
    found = titles(root)
    for rel, dest in DROPPED.items():
        assert (root / dest).is_file() and dest in found, (rel, dest)
    kind = {dest: found[dest]["k"] for dest in DROPPED.values()}
    assert kind == {**dict.fromkeys(DROPPED.values(), "book"), "comics/Moonwake 001.cbz": "comic",
                    "comics/Ironclad Hearts/Ironclad Hearts 007.cbz": "comic",
                    "collections/Starfall Station/Starfall Core Rules.epub": "rpg",
                    "manuals/Kettle/Kettle Guide.pdf": "manual"}
    rpg = found["collections/Starfall Station/Starfall Core Rules.epub"]
    assert rpg["dev"] == "tablet" and found["collections/Lantern Wolf.epub"]["dev"] == "ereader"  # the build went by it
    assert found["collections/Harbor Lights/Charts/Tide Tables.epub"]["col"] == "Harbor Lights"
    assert found["comics/Ironclad Hearts/Ironclad Hearts 007.cbz"]["col"] == "Ironclad Hearts"
    assert "col" not in found["collections/Lantern Wolf.epub"] and "col" not in found["collections/Hedge Lore.epub"]
    assert "Science Fiction" in found["collections/Science Fiction/Star Seeds.epub"]["g"]
    tagged = {dest: rec["tg"] for dest, rec in found.items() if rec.get("tg")}
    assert tagged == {"collections/Hedge Lore.epub": ["To read"],
                      "collections/Science Fiction/Star Seeds.epub": ["Favourite"]}
    changes = {(c["by"], c["field"], c["tag"], c["count"]) for c in curation.history(root)}
    assert changes == {(curation.INBOX, "tag", "To read", 1), (curation.INBOX, "tag", "Favourite", 1),
                       (curation.INBOX, "kind", "rpg", 1)}
    log = folders.log_file(root).read_text(encoding="utf-8")
    assert "tagged To read: 1 title" in log and "kind RPG book, as its folder in the Inbox says: 1 title" in log


def test_only_a_kind_s_folder_in_the_inbox_makes_a_correction_the_rest_is_the_folder_s_own_settings(inbox, capsys):
    """Nothing in the Inbox naming a book's kind, the folder it goes to says it, by its own settings and the build, as
    for a book put there by hand: its kind (tech, for every book there), its kinds (an RPG collection), and no word of
    a collection's (Cloud Harbor, whose "cloud" a shop's bundle would take for tech) makes a correction of the Inbox's.
    A comic by its format goes where comics go, and is no correction either."""
    root, box, _, write = inbox
    write(into='{ default = "collections" }',
          extra='[folders.collections]\nkind = "tech"\nkinds = { "Starfall Station" = "rpg", "Cloud Harbor" = "book" }\n')
    drop(box, "Deep Code.epub")
    drop(box, "Starfall Station/Starfall Supplement.epub")
    drop(box, "Cloud Harbor/Sky Notes.epub")
    drop(box, "Moonwake 001.cbz")
    a_pass(capsys, root)
    a_pass(capsys, root)
    kinds = {dest: rec["k"] for dest, rec in titles(root).items()}
    assert kinds == {"collections/Deep Code.epub": "tech", "collections/Starfall Station/Starfall Supplement.epub": "rpg",
                     "collections/Cloud Harbor/Sky Notes.epub": "book", "comics/Moonwake 001.cbz": "comic"}
    assert curation.history(root) == []  # nothing of the Inbox's over the owner's own settings
    write(into='{ default = "ebooks" }')
    drop(box, "Cloud Harbor/Rain Gauge.epub")  # without kinds for it: a book, whatever its collection's words
    a_pass(capsys, root)
    a_pass(capsys, root)
    assert titles(root)["ebooks/Cloud Harbor/Rain Gauge.epub"]["k"] == "book" and curation.history(root) == []


def test_a_tag_is_one_whatever_unicode_form_its_folder_s_name_is_written_in(inbox, capsys):
    """A Mac's share may write a name's accent as a mark of its own: a # folder so written gives the tag the library
    has already, never a second one beside it."""
    import unicodedata

    root, box, _, _ = inbox
    whole, apart = unicodedata.normalize("NFC", "#Café"), unicodedata.normalize("NFD", "#Café")
    assert whole != apart
    drop(box, f"{whole}/Lantern Wolf.epub")
    drop(box, f"{apart}/Moonwake.epub")
    a_pass(capsys, root)
    a_pass(capsys, root)
    found = titles(root)
    assert found["ebooks/Lantern Wolf.epub"]["tg"] == found["ebooks/Moonwake.epub"]["tg"] == ["Café"]
    assert list(curation.load(root).names) == ["café"]


def test_a_rules_file_s_kind_folder_that_is_hylly_s_own_is_never_where_the_inbox_s_books_go(inbox, capsys):
    """into's folders are checked to be your own; the rules' folders of a kind, which a rules file of yours may name,
    go by the same check: a comic goes to into's default rather than into the catalog's folder."""
    from hylly import rules

    root, box, _, _ = inbox
    mine = paths.settings_file().parent / "rules"
    mine.mkdir(exist_ok=True)
    (mine / "mine.toml").write_text('[kinds.comic]\nfolders = ["catalog", "state"]\n', encoding="utf-8")
    rules.reset()
    assert folders.inboxes()[0].folder_for("comic") == "ebooks"
    drop(box, "Moonwake 001.cbz")
    a_pass(capsys, root)
    a_pass(capsys, root)
    assert (root / "ebooks" / "Moonwake 001.cbz").is_file() and not (root / "catalog" / "Moonwake 001.cbz").exists()


def test_a_book_the_library_has_dropped_under_a_tag_s_folder_gives_its_card_the_tag(inbox, capsys, tmp_path):
    """Removed from the Inbox as any book the library has, and the card it is in tagged, by the Inbox, to undo as any
    edit; two formats of one title under one # folder are one card, one edit."""
    root, box, _, write = inbox
    epub(tmp_path / "bought-elsewhere" / "Hedge Lore.epub", "Hedge Lore")
    copies.add_books(root, "ebooks", tmp_path / "bought-elsewhere", log=lambda line: None)  # a recorded copy
    catalog.build(root, log=lambda line: None)
    twin = box / "#Keepers" / "Hedge Lore, again.epub"
    twin.parent.mkdir()
    twin.write_bytes((tmp_path / "bought-elsewhere" / "Hedge Lore.epub").read_bytes())
    aged(twin)
    drop(box, "#Two Formats/Tide Tables.epub")
    make_pdf(box / "#Two Formats" / "Tide Tables.pdf", title="Tide Tables")
    aged(box / "#Two Formats" / "Tide Tables.pdf")
    cli.main(["add", "--inbox", "-n"])
    assert "as ebooks/Hedge Lore.epub (tagged Keepers): " in capsys.readouterr().out
    said = a_pass(capsys, root)  # the library's book goes at the first pass, as it always has
    assert not twin.exists() and said == "Inbox: 1 book already in the library removed from the Inbox.\n"
    a_pass(capsys, root)
    found = titles(root)
    assert found["ebooks/Hedge Lore.epub"]["tg"] == ["Keepers"] and found["ebooks/Tide Tables.epub"]["tg"] == ["Two Formats"]
    counts = {(c["by"], c["tag"]): c["count"] for c in curation.history(root)}
    assert counts == {(curation.INBOX, "Keepers"): 1, (curation.INBOX, "Two Formats"): 1}


def test_a_kind_s_folder_of_its_own_in_into_and_a_tag_on_a_book_s_copies_are_given_by_one_edit_each(inbox, capsys):
    """into names a folder for RPG books: they go there, of their kind already as [folders.rpg] says, and no correction
    is kept. Two books under one # folder get its tag by one edit, which the library page's Recent changes lists, by the
    Inbox, and which goes as any tag: taken off, it stays off."""
    root, box, _, write = inbox
    write(into='{ default = "ebooks", rpg = "rpg" }', extra='[folders.rpg]\nkind = "rpg"\n')
    drop(box, "RPG/Starfall Station/Starfall Core Rules.pdf")
    drop(box, "#Weird Fiction/Moonwake.epub")
    drop(box, "#Weird Fiction/Lantern Wolf.epub")
    a_pass(capsys, root)
    a_pass(capsys, root)
    found = titles(root)
    assert found["rpg/Starfall Station/Starfall Core Rules.pdf"]["k"] == "rpg"
    (edit,) = curation.history(root)
    assert (edit["by"], edit["field"], edit["tag"], edit["count"]) == (curation.INBOX, "tag", "Weird Fiction", 2)
    assert found["ebooks/Moonwake.epub"]["tg"] == found["ebooks/Lantern Wolf.epub"]["tg"] == ["Weird Fiction"]
    curation.change(root, "remove", "Weird Fiction", [found["ebooks/Moonwake.epub"]], by=curation.COMMAND_LINE)
    now = titles(root)
    assert "tg" not in now["ebooks/Moonwake.epub"] and now["ebooks/Lantern Wolf.epub"]["tg"] == ["Weird Fiction"]


def test_an_original_that_can_t_be_removed_gets_its_tag_once_and_a_tag_taken_off_stays_off(inbox, capsys, monkeypatch,
                                                                                            tmp_path):
    """A book the library has, and one just copied in, each under a # folder, whose originals can't be removed (a share
    that takes no changes): each tag is given once, by the Inbox, for that original, by its path, size and MD5; taken
    off by its owner, it isn't given back at the passes after, nor once the original goes at last."""
    root, box, _, _ = inbox
    epub(tmp_path / "bought-elsewhere" / "Hedge Lore.epub", "Hedge Lore")
    copies.add_books(root, "ebooks", tmp_path / "bought-elsewhere", log=lambda line: None)  # a recorded copy
    catalog.build(root, log=lambda line: None)
    had = box / "#Keepers" / "Hedge Lore, again.epub"
    had.parent.mkdir()
    had.write_bytes((tmp_path / "bought-elsewhere" / "Hedge Lore.epub").read_bytes())
    aged(had)
    new = drop(box, "#Fresh/Lantern Wolf.epub")

    def stuck(*a, **k):
        raise PermissionError(13, "Permission denied")

    real = copies.remove
    monkeypatch.setattr(copies, "remove", stuck)

    def inbox_edits() -> list[tuple[str, str]]:
        return [(c["op"], c["tag"]) for c in curation.history(root, n=100)[::-1] if c["by"] == curation.INBOX]

    a_pass(capsys, root)
    said = a_pass(capsys, root)
    assert had.is_file() and new.is_file() and "couldn't be removed from the Inbox" in said
    assert inbox_edits() == [("add", "Keepers"), ("add", "Fresh")]
    found = titles(root)
    for dest in ("ebooks/Hedge Lore.epub", "ebooks/Lantern Wolf.epub"):  # taken off by the owner
        curation.change(root, "remove", found[dest]["tg"][0], [found[dest]], by=curation.COMMAND_LINE)
    a_pass(capsys, root)
    a_pass(capsys, root)
    assert had.is_file() and new.is_file() and inbox_edits() == [("add", "Keepers"), ("add", "Fresh")]
    monkeypatch.setattr(copies, "remove", real)
    a_pass(capsys, root)
    assert not had.exists() and not new.exists() and inbox_edits() == [("add", "Keepers"), ("add", "Fresh")]
    assert not any("tg" in rec for rec in titles(root).values()) and state(root)["tagged"] == {str(box): {}}


def test_a_folder_that_names_no_tag_is_said_once_and_its_books_come_in_without_one(inbox, capsys):
    """Too long for a tag, or nothing after the #: said once while books are in it, the books taken in all the same,
    and the folder, emptied, goes as a collection's does: only one that gives a tag is kept for the books to come."""
    root, box, _, _ = inbox
    long = "#" + "x" * (curation.TAG_MAX + 1)
    book = drop(box, f"{long}/Moonwake.epub")
    drop(box, "#/Lantern Wolf.epub")
    spaced = os.name != "nt"  # Windows makes no folder whose name ends in a space
    if spaced:
        drop(box, "# /Hedge Lore.epub")
    drop(box, "#Kept/Tide Tables.epub")
    said = a_pass(capsys, root) + a_pass(capsys, root)
    assert said.count("names no tag a book can have") == 2 + spaced and f"{box / long} names no tag" in said
    assert not book.exists() and "tg" not in titles(root)["ebooks/Moonwake.epub"]
    assert [p.name for p in box.iterdir()] == ["#Kept"]  # #, "# " and the long one gone with their books
    drop(box, f"{long}/Hedge Lore, again.epub")  # its folder made again: said again, as of a book that comes back
    assert "names no tag" in a_pass(capsys, root) + a_pass(capsys, root)


def test_several_inboxes_each_empty_into_the_library_by_the_same_rules(inbox, capsys, tmp_path):
    """folder and folders together: each Inbox read the same way, its own Not added and state, each said by its own
    path."""
    root, box, _, write = inbox
    scans = tmp_path / "scans"
    write(folders=[str(scans), str(box)])
    assert [i.folder for i in folders.inboxes()] == [str(box), str(scans)]
    drop(box, "Harbor Lights/The Lantern Keeper.epub")
    drop(scans, "Comics/Moonwake/Moonwake 001.cbz")
    (scans / "notes.docx").write_bytes(b"PK\x03\x04 a document")
    aged(scans / "notes.docx")
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    assert (root / "ebooks" / "Harbor Lights" / "The Lantern Keeper.epub").is_file()
    assert (root / "comics" / "Moonwake" / "Moonwake 001.cbz").is_file() and f"{scans / 'notes.docx'} set aside" in said
    assert (scans / folders.NOT_ADDED / "notes.docx").is_file() and not (box / folders.NOT_ADDED).exists()
    assert set(state(root)["seen"]) == {str(box), str(scans)}
    drop(box, "Same Name.epub")
    drop(scans, "Same Name.epub").write_bytes(epub(tmp_path / "other.epub", "Another Book").read_bytes())
    aged(scans / "Same Name.epub")
    a_pass(capsys, root)
    a_pass(capsys, root)
    note = next((p for p in (box, scans) if (p / folders.NOT_ADDED / "Same Name.epub.txt").is_file()))
    other = box if note == scans else scans
    assert f"Another book, {other / 'Same Name.epub'}, gets this one's name" in (
        note / folders.NOT_ADDED / "Same Name.epub.txt").read_text(encoding="utf-8")  # by which Inbox it is in


def test_into_and_the_inbox_s_folders_are_checked_and_the_old_inbox_key_is_told_where_it_went(inbox, capsys):
    """into names kinds and folders of yours, never a shop's, Hylly's own or a path; the Inbox is a folder by its full
    path. inbox = true under a folder's section is no longer read: hylly settings check says, in one line, that the
    Inbox is [inbox] now, and the folder's copy_from stays for hylly add, which only reads it."""
    root, box, _, write = inbox
    for into, wrong in (('{ fiction = "novels" }', "'fiction' isn't a kind"),
                        ('{ default = "humble" }', "'humble' isn't a name"), ('{ comic = "catalog" }', "'catalog' isn't a"),
                        ('{ default = "a/b" }', "'a/b' isn't a name"), ('"collections"', "a table in braces")):
        write(into=into)
        with pytest.raises(SystemExit) as e:
            cli.main(["settings", "check"])
        assert e.value.code == exits.NOT_SET_UP and wrong in capsys.readouterr().out, into
    write(folder="inbox")
    with pytest.raises(SystemExit):
        cli.main(["settings", "check"])
    assert "'inbox' isn't a folder by its full path" in capsys.readouterr().out
    book = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    paths.settings_file().write_text(f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{box}'\ninbox = true\n"
                                     "lookup = false\n", encoding="utf-8")
    settings.reload()
    cli.main(["settings", "check"])
    said = capsys.readouterr().out
    assert said.count("[folders.ebooks] inbox is no longer used, so it is left out. It moved to [inbox], a section of "
                      "its own") == 1 and "[folders.ebooks] lookup is no longer used" in said
    assert 'into = { default = "ebooks" } there sends its books into ebooks as before' in said  # where they went
    assert folders.inboxes() == [] and not folders.waiting(root)
    cli.main(["add", "ebooks"])  # a plain hylly add from copy_from now: the original only read
    assert book.is_file() and (root / "ebooks" / "Lantern Wolf.epub").is_file()


def test_the_pass_s_build_finds_the_catalog_s_questions_as_hylly_catalog_does(inbox, capsys):
    """The Inbox's pass builds the catalog as hylly catalog does, so it finds the catalog's questions too
    (here, files of a kind the rules don't take as books, beside the books it adds), once."""
    from hylly import curation

    root, box, _, _ = inbox
    (root / "ebooks").mkdir()
    (root / "ebooks" / "notes.xyz").write_bytes(b"x")
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer"))
    a_pass(capsys, root)
    a_pass(capsys, root)
    assert (root / "ebooks" / "Lantern Wolf.epub").is_file()
    (q,) = [q for q in curation.questions(root) if q["shop"] == curation.CATALOG]
    assert (q["kind"], q["title"], q["file"]) == ("files", "1 .xyz file in Ebooks", ".xyz")


def test_a_book_still_coming_in_is_left_for_a_later_pass(inbox, capsys):
    """Taken once it is the same at two looks two minutes apart, or its time is that old and it's the same as at the look
    before: a file that grows, as one copied over the network does, waits."""
    root, box, _, _ = inbox
    book = epub(box / "Moonwake.epub", "Moonwake")  # its time is now
    a_pass(capsys, root)
    with open(book, "ab") as fh:
        fh.write(b"\0" * 10)  # still coming
    assert a_pass(capsys, root) == "" and book.is_file()
    assert a_pass(capsys, root) == "" and book.is_file()  # the same, but only for a moment
    saved = state(root)
    saved["seen"][str(box)]["Moonwake.epub"]["since"] -= 200  # the look two minutes ago saw it so too
    folders.state_file(root).write_text(json.dumps(saved), encoding="utf-8")
    assert "1 set aside" not in a_pass(capsys, root)
    assert not book.exists() and (root / "ebooks" / "Moonwake.epub").is_file()


def test_what_cant_be_taken_is_set_aside_in_not_added_with_a_note_saying_why_and_isnt_tried_again(inbox, capsys):
    root, box, notes, write = inbox
    write(strip_suffix="_copy")
    (root / "ebooks").mkdir()
    theirs = epub(root / "ebooks" / "Taken.epub", "Another book", "Rory Penn").read_bytes()  # put in the library by hand
    books = {"notes.docx": None, "Shelf/Broken.epub": None, "Cut.epub": None, "Taken.epub": None, "Moonwake.epub": None,
             "Moonwake_copy.epub": None}
    (box / "notes.docx").write_bytes(b"PK\x03\x04 a document")
    (box / "Shelf").mkdir()
    (box / "Shelf" / "Broken.epub").write_bytes(b"not really a book")
    whole = epub(box / "Cut.epub", "Cut")
    whole.write_bytes(whole.read_bytes()[:-30])  # its end missing, as a copy that stopped half way
    epub(box / "Taken.epub", "Taken", "Ann Writer")
    epub(box / "Moonwake.epub", "Moonwake")
    epub(box / "Moonwake_copy.epub", "Moonwake, an earlier edition")  # strip_suffix makes its name the other's
    for name in books:
        aged(box / name)
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    aside = box / folders.NOT_ADDED
    for name, why in (("notes.docx", "isn't a kind of file Hylly reads as a book"), ("Shelf/Broken.epub", "It is damaged"),
                      ("Cut.epub", "cut short"), ("Taken.epub", "The library has another book by this name, ebooks/Taken.epub"),
                      ("Moonwake_copy.epub", f"Another book, {box / 'Moonwake.epub'}, gets this one's name")):
        note = (aside / f"{name}.txt").read_text(encoding="utf-8")
        assert (aside / name).is_file() and note.startswith(copies.NOTE_HEAD) and why in note, (name, note)
        assert f"{box / name} set aside in Not added" in said and not (box / name).exists()
    assert not (box / "Shelf").exists()  # left empty
    assert (root / "ebooks" / "Taken.epub").read_bytes() == theirs  # nothing in the library is written over
    assert (root / "ebooks" / "Moonwake.epub").is_file() and said.endswith(
        "Inbox: 1 book added from the Inbox, details found for 1, and 5 set aside in Not added.\n")
    assert notes[0][2] == "1 book added from the Inbox, details found for 1, and 5 set aside in Not added."
    assert not folders.waiting(root) and a_pass(capsys, root) == ""  # never tried every minute


def test_a_book_put_back_from_not_added_is_tried_again_and_its_note_goes(inbox, capsys):
    root, box, _, _ = inbox
    (root / "ebooks").mkdir()
    epub(root / "ebooks" / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer")
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf", "Rory Penn"))
    a_pass(capsys, root)
    a_pass(capsys, root)
    aside = box / folders.NOT_ADDED / "Lantern Wolf.epub"
    assert aside.is_file()
    aside.rename(aged(aside).with_name("Lantern Wolf (Rory Penn).epub"))  # renamed by its owner, still in Not added
    (box / folders.NOT_ADDED / "Lantern Wolf (Rory Penn).epub").rename(box / "Lantern Wolf (Rory Penn).epub")  # put back
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    assert said.endswith("Inbox: 1 book added from the Inbox, details found for 1.\n")
    assert (root / "ebooks" / "Lantern Wolf (Rory Penn).epub").is_file() and not (box / folders.NOT_ADDED).exists()


def test_a_book_already_in_the_library_goes_from_the_inbox_at_the_first_pass(inbox, capsys):
    """As the books hylly add copied from the Inbox before it was one: known by the size and MD5 their copies were
    recorded with, and removed at the first pass, said as already in the library, whatever their names or times."""
    root, box, notes, write = inbox
    book = epub(box / "Ann Writer" / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer")
    other = epub(box / "Hedge Lore.epub", "Hedge Lore")
    paths.settings_file().write_text(f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{box}'\n", encoding="utf-8")
    settings.reload()
    cli.main(["add", "ebooks"])  # not an Inbox yet: copied, the originals only read
    catalog.build(root, log=lambda line: None)
    capsys.readouterr()
    write()
    renamed = other.rename(box / "Hedge Lore, a copy.epub")
    said = a_pass(capsys, root)
    assert not book.exists() and not renamed.exists() and said == "Inbox: 2 books already in the library removed from the Inbox.\n"
    assert len(copies.recorded(root)) == 2 and not (root / "ebooks" / "Hedge Lore, a copy.epub").exists()
    assert notes[0][2] == "2 books already in the library removed from the Inbox." and Lantern.asked == []


def test_a_lookup_out_of_budget_leaves_the_book_in_and_asks_again_at_a_later_pass(inbox, capsys):
    root, box, notes, _ = inbox
    Lantern.answers.append(lookups.Quota("Lantern DB: today's budget of 10 requests is spent."))
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer"))
    a_pass(capsys, root)
    assert a_pass(capsys, root).endswith("Inbox: 1 book added from the Inbox, details found for 0.\n")
    assert not (box / "Lantern Wolf.epub").exists() and "d" not in titles(root)["ebooks/Lantern Wolf.epub"]  # in, as its file says
    assert list(state(root)["pending"]) == ["ebooks/Lantern Wolf.epub"]
    assert not folders.waiting(root)  # asked again at a later pass, not the next minute
    saved = state(root)
    saved["pending"]["ebooks/Lantern Wolf.epub"]["asked"] -= folders.LOOKUP_AGAIN
    folders.state_file(root).write_text(json.dumps(saved), encoding="utf-8")
    assert folders.waiting(root)
    a_pass(capsys, root)
    assert titles(root)["ebooks/Lantern Wolf.epub"]["d"] == "A made-up tale of Lantern Wolf." and state(root)["pending"] == {}
    assert Lantern.asked == ["Lantern Wolf", "Lantern Wolf"] and len(notes) == 1


def test_a_wait_longer_than_a_pass_gives_a_source_ends_its_turn_as_a_spent_budget_does(monkeypatch):
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    cli._patient(3.2)
    with pytest.raises(lookups.Quota, match="asks again at a later pass"):
        cli._patient(cli.PATIENCE + 1)
    assert slept == [3.2]


def test_with_lookup_false_no_source_is_asked(inbox, capsys):
    root, box, notes, write = inbox
    write(lookup=False)
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    a_pass(capsys, root)
    assert a_pass(capsys, root).endswith("Inbox: 1 book added from the Inbox.\n") and Lantern.asked == []
    assert state(root)["pending"] == {} and (root / "ebooks" / "Lantern Wolf.epub").is_file()


def test_with_lookups_off_in_the_settings_the_pass_says_so_once_and_asks_no_source(inbox, capsys):
    root, box, notes, write = inbox
    write(extra="[lookups]\nenabled = false\n")
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    a_pass(capsys, root)
    cli.main(["internal", "inbox", "--library", str(root)])  # the pass that takes it: its notes on standard error
    said = capsys.readouterr()
    assert said.err.count(lookups.OFF) == 1 and said.out.endswith("Inbox: 1 book added from the Inbox.\n") and Lantern.asked == []
    assert folders.log_file(root).read_text(encoding="utf-8").count(lookups.OFF) == 1  # and in the passes' log
    assert state(root)["pending"] == {} and (root / "ebooks" / "Lantern Wolf.epub").is_file()  # nor asked at a later pass


def test_a_pass_leaves_the_inbox_to_a_hylly_add_and_one_typed_meanwhile_is_refused(inbox, capsys):
    root, box, _, _ = inbox
    book = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    a_pass(capsys, root)
    with runs.only_one("add", root):  # a hylly add typed by a person, or the pass, runs
        cli.main(["internal", "inbox", "--library", str(root)])
        said = capsys.readouterr()
        assert said.out == "" and "A copy into the library is already running" in said.err and "The Inboxes wait for it" in said.err
        with pytest.raises(SystemExit) as e:
            cli.main(["add", "--inbox", "--yes"])
        assert e.value.code == exits.BUSY
    assert book.is_file()


def test_hylly_add_inbox_says_so_asks_the_first_time_and_makes_a_pass(inbox, capsys):
    """By hand, a book whose time is two minutes old is taken without a second look: whoever typed it is the second."""
    root, box, notes, _ = inbox
    book = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    with pytest.raises(SystemExit) as e:
        cli.main(["add", "--inbox"])
    said = capsys.readouterr()
    assert e.value.code == exits.NEEDS_YOU and f"The Inbox's books ({box}) are copied into the library" in said.err
    assert "comics to comics, manuals to manuals, the rest to ebooks" in said.err and "--yes" in said.err
    assert book.is_file() and not (root / "ebooks").exists()
    cli.main(["add", "--inbox", "--yes"])
    said = capsys.readouterr()
    assert "Going on, as --yes says" in said.err and said.out.endswith("Inbox: 1 book added from the Inbox, details found for 1.\n")
    assert not book.exists() and (root / "ebooks" / "Lantern Wolf.epub").is_file()
    later = aged(epub(box / "Hedge Lore.epub", "Hedge Lore"))
    cli.main(["add", "--inbox"])  # asked once: not again
    assert not later.exists() and "Go ahead" not in capsys.readouterr().err
    cli.main(["add", "ebooks", "--from", str(box)])  # from a folder named for this run: a plain hylly add, only read
    assert "0 books in" in capsys.readouterr().out
    for words in (["add", "ebooks", "--inbox"], ["add", "--inbox", "--from", str(box)]):
        with pytest.raises(SystemExit) as e:
            cli.main(words)
        assert e.value.code == exits.USAGE and "--inbox takes no folder" in capsys.readouterr().err


def test_the_dry_runs_say_what_a_pass_would_do_and_change_nothing(inbox, capsys):
    root, box, _, _ = inbox
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    epub(box / "Moonwake.epub", "Moonwake")  # its time now
    (box / "notes.docx").write_bytes(b"PK\x03\x04 a document")
    aged(box / "notes.docx")
    a_pass(capsys, root)
    before = {str(p): p.stat().st_mtime_ns for p in [*box.rglob("*"), *root.rglob("*")]}
    cli.main(["add", "--inbox", "-n"])
    said = capsys.readouterr().out
    assert ("would copy into the library as ebooks/Lantern Wolf.epub, then remove from the Inbox: "
            f"{box / 'Lantern Wolf.epub'}") in said
    assert f"would set aside in Not added: {box / 'notes.docx'}: It isn't a kind of file" in said
    assert "left for a later pass" in said and str(box / "Moonwake.epub") in said and said.endswith("Nothing was changed.\n")
    cli.main(["internal", "inbox", "-n", "--library", str(root)])
    assert "would copy into the library as ebooks/Lantern Wolf.epub" in capsys.readouterr().out
    assert {str(p): p.stat().st_mtime_ns for p in [*box.rglob("*"), *root.rglob("*")]} == before


def test_an_inbox_whose_folder_isnt_there_is_said_once_and_nothing_is_made_there(inbox, capsys):
    """As the owner emptied it by hand, its folders too, or as an unmounted disk leaves it: a pass makes nothing on what
    may be a disk's empty mount point, says so once, and looks again at each later pass; hylly doctor says it too."""
    root, box, _, _ = inbox
    box.rmdir()
    assert folders.waiting(root)
    said = a_pass(capsys, root)
    assert f"The Inbox, {box}, isn't there" in said and "Make the folder" in said and not box.exists()
    assert not folders.waiting(root) and a_pass(capsys, root) == ""  # said once
    result = doctor.report(str(root))
    assert any(c["what"] == "Inbox" and c["state"] == doctor.PROBLEM and "isn't there" in c["says"] for c in result["checks"])
    with pytest.raises(SystemExit) as e:
        cli.main(["add", "--inbox", "--yes"])
    assert e.value.code == exits.LOCAL and not box.exists()
    capsys.readouterr()
    box.mkdir()
    assert folders.waiting(root)  # back: a pass forgets it was gone
    a_pass(capsys, root)
    assert folders.missing_said(str(box)) not in state(root)["said"] and not folders.waiting(root)
    box.rmdir()
    assert "isn't there" in a_pass(capsys, root)  # gone again: said again


def test_the_phone_note_and_the_daily_summary_say_it_in_counts(tmp_path):
    assert bell.inbox_said(3, 2, 0, 0) == "3 books added from the Inbox, details found for 2."
    assert bell.inbox_said(3, 2, 1, 0) == "3 books added from the Inbox, details found for 2, and 1 set aside in Not added."
    assert bell.inbox_said(1, None, 0, 7) == "1 book added from the Inbox, and 7 already in the library removed from it."
    assert bell.inbox_said(0, None, 1, 0) == "1 book from the Inbox set aside in Not added."
    assert bell.inbox_said(0, None, 0, 0) == ""
    assert bell.INBOX_DONE == folders.state_file(tmp_path).name
    now = time.time()
    paths.state_dir(tmp_path).mkdir()
    folders.state_file(tmp_path).write_text(json.dumps({"done": [[now - 90000, 5, 5, 0, 0], [now - 3600, 2, 1, 0, 0],
                                                                 [now - 60, 1, -1, 1, 0], "not a row"]}), encoding="utf-8")
    assert bell.inbox_day(tmp_path, now) == "Inbox: 3 books added from the Inbox, details found for 1, and 1 set aside in Not added."
    sent = []
    w = bell.Watch({**bell.DEFAULTS, "toast": False}, send=lambda title, text, prio, public=None: sent.append((text, public)),
                   now=lambda: now, log=lambda m: None, state=tmp_path / "bell.json", library=tmp_path)
    w.conf["daily_summary"] = time.localtime(now).tm_hour
    w.maybe_summary()
    (text, public), = sent
    assert text.endswith("\nInbox: 3 books added from the Inbox, details found for 1, and 1 set aside in Not added.")
    assert public[1].endswith("\nInbox: 3 books added from the Inbox, details found for 1, and 1 set aside in Not added.")


def test_the_doctor_says_when_nothing_runs_the_inbox(inbox, monkeypatch):
    """A server walled in leaves the Inbox to hylly server jobs on a timer: without one, nothing runs it, and the doctor
    says what to install; once the timer has run it lately, it says so. A server that isn't walled runs it itself."""
    root, _, _, _ = inbox

    def inbox_checks() -> list[dict]:
        return [c for c in doctor.report(str(root))["checks"] if c["what"] == "Inbox"]

    monkeypatch.setattr(instance, "running", lambda: None)
    assert [c["state"] for c in inbox_checks()] == [doctor.INFO, doctor.INFO] and "isn't running" in inbox_checks()[1]["says"]
    monkeypatch.setattr(instance, "system_service", lambda: True)
    found = inbox_checks()[1]
    assert found["state"] == doctor.PROBLEM and "this server is walled in" in found["says"] and "hylly-jobs.timer" in found["next"]
    job = next(j for j in periodic.JOBS if j.name == "inbox")
    periodic._keep(job, {"last": time.time() - 60, "said": {}})
    assert inbox_checks()[1]["state"] == doctor.OK and "hylly server jobs runs it from a timer" in inbox_checks()[1]["says"]


# ---- what a pass never touches, and what makes it wait ---------------------------------------------------------------

def link_to(link: Path, target: Path) -> Path:
    """A folder that leads to another: a junction on Windows, a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)
    return link


def test_an_inbox_that_is_the_library_by_another_spelling_takes_nothing_and_removes_nothing(inbox, capsys, tmp_path):
    """[inbox] folder as \\\\?\\ and the library's own folder on Windows, or through a link elsewhere: another way to
    the same folder, told by the folders' identities on the disk and refused before anything is looked at. Neither the
    book put there by hand nor the one copied in is removed."""
    root, box, _, write = inbox
    epub(tmp_path / "bought-elsewhere" / "Hedge Lore.epub", "Hedge Lore")
    copies.add_books(root, "ebooks", tmp_path / "bought-elsewhere", log=lambda line: None)  # a recorded copy
    copied = aged(root / "ebooks" / "Hedge Lore.epub")
    by_hand = aged(epub(root / "ebooks" / "Lantern Wolf.epub", "Lantern Wolf"))
    spelled = ("\\\\?\\" + str(root / "ebooks")) if os.name == "nt" else str(link_to(tmp_path / "alias", root) / "ebooks")
    write(folder=spelled)
    assert copies.Guard(root).overlaps(Path(spelled)) and copies.Guard(root).holds(Path(spelled) / "Lantern Wolf.epub")
    said = a_pass(capsys, root) + a_pass(capsys, root)
    assert copied.is_file() and by_hand.is_file() and said.count("and the library") == 1 and "overlap" in said
    assert sorted(p.name for p in (root / "ebooks").iterdir()) == ["Hedge Lore.epub", "Lantern Wolf.epub"]
    assert any(c["what"] == "Inbox" and c["state"] == doctor.PROBLEM and "is the library or inside it" in c["says"]
               for c in doctor.report(str(root))["checks"])


def test_an_original_that_is_the_library_s_own_file_or_whose_copy_reads_otherwise_now_stays(inbox):
    """The last word before an original goes: never the library's own file, by any spelling of it; and its copy in the
    library read again now, with it, by size and MD5."""
    root, box, _, _ = inbox
    guard = copies.Guard(root)
    own = epub(root / "ebooks" / "Lantern Wolf.epub", "Lantern Wolf")
    spelled = Path("\\\\?\\" + str(own)) if os.name == "nt" else own
    st = own.stat()
    book = copies.Waiting("ebooks", box, spelled, "Lantern Wolf.epub", "ebooks/Lantern Wolf.epub", st.st_size, st.st_mtime)
    assert copies.remove(book, own, guard) == "library" and own.is_file()
    mine = epub(box / "Moonwake.epub", "Moonwake")
    data = mine.read_bytes()
    copy = root / "ebooks" / "Moonwake.epub"
    copy.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))  # changed since, at the same size
    st = mine.stat()
    book = copies.Waiting("ebooks", box, mine, "Moonwake.epub", "ebooks/Moonwake.epub", st.st_size, st.st_mtime)
    assert copies.remove(book, copy, guard) == "differs" and mine.is_file()
    copy.write_bytes(data)
    assert copies.remove(book, copy, guard) == "removed" and not mine.exists()


def test_what_the_state_folder_says_of_a_book_is_never_taken_for_its_checksum(inbox, tmp_path):
    """state/inbox.json is in the library, which a walled server may write: a look reads both MD5s itself, so a book
    can't be called one the library has by what that file says."""
    root, box, _, _ = inbox
    (tmp_path / "bought-elsewhere").mkdir()
    (tmp_path / "bought-elsewhere" / "Old.pdf").write_bytes(b"%PDF-1.4 Moonwake\n%%EOF\n")
    copies.add_books(root, "ebooks", tmp_path / "bought-elsewhere", log=lambda line: None)
    record = copies.recorded(root)["ebooks/old.pdf"]
    new = box / "New.pdf"
    new.write_bytes(b"%PDF-1.4 Lanterns\n%%EOF\n")  # another book of the same size
    st = new.stat()
    assert st.st_size == record["size"]
    seen = {str(box): {"New.pdf": {"size": st.st_size, "mtime": st.st_mtime, "since": 0, "md5": record["md5"],
                                   "in": record["dest"]}}}  # as a forged state file would have it
    found = copies.look(root, folders.inboxes(root=root), seen, time.time())
    assert not found.already and [book.rel for book in found.take] == ["New.pdf"]


def test_a_link_or_junction_in_the_inbox_leads_the_pass_nowhere(inbox, capsys, tmp_path):
    root, box, _, _ = inbox
    book = aged(epub(tmp_path / "outside" / "Lantern Wolf.epub", "Lantern Wolf"))
    link_to(box / "Elsewhere", tmp_path / "outside")
    a_pass(capsys, root)
    a_pass(capsys, root)
    assert book.is_file() and not (root / "ebooks").exists() and not folders.waiting(root)


def test_a_not_added_that_is_a_link_or_junction_is_refused_and_nothing_goes_through_it(inbox, capsys, tmp_path):
    root, box, _, _ = inbox
    away = tmp_path / "away"
    away.mkdir()
    (away / "Old.epub.txt").write_text(f"{copies.NOTE_HEAD}: a note from before.", encoding="utf-8")
    (away / ".DS_Store").write_bytes(b"what a Mac leaves")
    link_to(box / folders.NOT_ADDED, away)
    notes = box / "notes.docx"
    notes.write_bytes(b"PK\x03\x04 a document")
    aged(notes)
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    assert notes.is_file() and "couldn't be moved into Not added either" in said and "a link or a junction" in said
    assert sorted(p.name for p in away.iterdir()) == [".DS_Store", "Old.epub.txt"]  # nor tidied through it
    assert os.path.lexists(box / folders.NOT_ADDED)
    assert a_pass(capsys, root) == ""  # said once


def test_a_lookup_offline_asks_again_at_a_later_pass_which_builds_only_once_a_source_answers(inbox, capsys, monkeypatch):
    root, box, _, _ = inbox
    Lantern.answers += [lookups.Transient("Lantern DB: no answer from /search after 6 tries (ConnectError)."),
                        lookups.Quota("Lantern DB: today's budget of 10 requests is spent.")]
    aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer"))
    a_pass(capsys, root)
    assert a_pass(capsys, root).endswith("Inbox: 1 book added from the Inbox, details found for 0.\n")
    assert list(state(root)["pending"]) == ["ebooks/Lantern Wolf.epub"]
    builds, real = [], catalog.build
    monkeypatch.setattr(catalog, "build", lambda *a, **k: (builds.append(1), real(*a, **k))[1])

    def due() -> None:
        saved = state(root)
        saved["pending"]["ebooks/Lantern Wolf.epub"]["asked"] -= folders.LOOKUP_AGAIN
        folders.state_file(root).write_text(json.dumps(saved), encoding="utf-8")

    due()
    a_pass(capsys, root)  # out of budget now: nothing found, and the book is in the catalog already
    assert builds == [] and Lantern.asked == ["Lantern Wolf"] * 2 and "ebooks/Lantern Wolf.epub" in state(root)["pending"]
    due()
    a_pass(capsys, root)
    assert builds == [1] and titles(root)["ebooks/Lantern Wolf.epub"]["d"] == "A made-up tale of Lantern Wolf."
    assert state(root)["pending"] == {}


def test_a_copy_stalled_at_its_full_size_waits_and_a_cut_mobi_or_cbr_is_set_aside(inbox, capsys, tmp_path):
    import struct

    root, box, _, _ = inbox
    stalled = box / "Stalled.azw3"
    stalled.write_bytes(b"\0" * 60 + b"BOOKMOBI" + b"\0" * 8000)  # its size set first, its end not written yet
    mobi = bytearray(300)
    mobi[60:68], mobi[76:78], mobi[-1] = b"BOOKMOBI", struct.pack(">H", 50), 1  # 50 records listed: they don't fit
    (box / "Cut.mobi").write_bytes(bytes(mobi))
    (box / "Cut.cbr").write_bytes(b"Rar!\x1a\x07\x00" + b"\x01" * 200)  # no end-of-archive block
    (box / "Moonwake.epub.crdownload").write_bytes(b"a browser's download, still on its way")
    for path in box.iterdir():
        aged(path)
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    assert stalled.is_file() and "Stalled.azw3 ends in zeros" in said and (box / "Moonwake.epub.crdownload").is_file()
    for name in ("Cut.mobi", "Cut.cbr"):
        assert (box / folders.NOT_ADDED / name).is_file() and "cut short" in (box / folders.NOT_ADDED / f"{name}.txt").read_text(encoding="utf-8")
    assert "ends in zeros" not in a_pass(capsys, root)  # said once; it waits
    for name, data in (("Whole.cbr", b"Rar!\x1a\x07\x00" + b"\x01" * 200 + b"\xc4\x3d\x7b\x00\x40\x07\x00"),
                       ("Whole5.cbr", b"Rar!\x1a\x07\x01\x00" + b"\x01" * 200 + b"\x1d\x77\x56\x51\x03\x05\x04\x00")):
        (tmp_path / name).write_bytes(data)
        assert copies.damaged(tmp_path / name) == ""


def test_a_book_that_can_t_be_read_stays_in_the_inbox_said_once_and_is_taken_once_it_can_be(inbox, capsys, monkeypatch):
    """A file the user Hylly runs as may not read, as a share may write one: it waits, never set aside, and the pass
    says so once, with what to put right; the dry run says why it waits."""
    root, box, _, _ = inbox
    book = aged(epub(box / "Locked.epub", "Locked"))
    real = copies._zero_tail

    def locked(path, size):
        if Path(path).name == "Locked.epub":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, size)

    monkeypatch.setattr(copies, "_zero_tail", locked)
    said = a_pass(capsys, root) + a_pass(capsys, root) + a_pass(capsys, root)
    assert said.count(f"{book} can't be read (Permission denied): it stays in the Inbox") == 1 and book.is_file()
    cli.main(["add", "--inbox", "-n"])
    assert f"left for a later pass, as it can't be read (Permission denied): {book}" in capsys.readouterr().out
    monkeypatch.setattr(copies, "_zero_tail", real)
    assert a_pass(capsys, root).endswith("Inbox: 1 book added from the Inbox, details found for 1.\n")
    assert not book.exists() and not any(key.startswith("unread|") for key in state(root)["said"])  # gone: forgotten


def test_a_book_the_catalog_cant_open_stays_and_is_set_aside_at_the_next_pass(inbox, capsys, monkeypatch):
    """A damage only the catalog's build finds: the original isn't removed; at the next pass, still so, it goes into Not
    added, and the library keeps its copy, as it never removes anything."""
    root, box, _, _ = inbox
    monkeypatch.setattr(copies, "damaged", lambda path: "")  # as if the pass's own look had missed it
    broken = box / "Broken.epub"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="gone.opf"/></rootfiles></container>')
    aged(broken)
    a_pass(capsys, root)
    said = a_pass(capsys, root)
    assert broken.is_file() and "no reader can open that copy" in said and (root / "ebooks" / "Broken.epub").is_file()
    said = a_pass(capsys, root)
    assert not broken.exists() and (box / folders.NOT_ADDED / "Broken.epub").is_file()
    assert "can't be opened by any reader" in (box / folders.NOT_ADDED / "Broken.epub.txt").read_text(encoding="utf-8")
    assert (root / "ebooks" / "Broken.epub").is_file() and "set aside in Not added" in said


def test_a_full_disk_makes_the_books_wait_and_leaves_no_half_copy(inbox, capsys, monkeypatch):
    import errno

    root, box, _, _ = inbox
    book = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    a_pass(capsys, root)
    real = copies.hashed_copy

    def full(src, dst, *a, **k):
        Path(dst).write_bytes(b"half a book")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(copies, "hashed_copy", full)
    monkeypatch.setattr(copies.time, "sleep", lambda seconds: None)
    said = a_pass(capsys, root)
    assert book.is_file() and "The library's disk is full" in said and not (box / folders.NOT_ADDED).exists()
    assert not list(root.rglob("*.copying"))
    assert a_pass(capsys, root) == ""  # said once, and it waits
    monkeypatch.setattr(copies, "hashed_copy", real)
    assert a_pass(capsys, root).endswith("Inbox: 1 book added from the Inbox, details found for 1.\n") and not book.exists()


def test_a_build_kept_whose_data_js_can_t_be_written_says_so_once_and_its_books_leave_the_inbox(inbox, capsys, monkeypatch):
    """The pass's build is kept in the database although catalog/data.js can't be written just then: its books are
    catalogued, so they leave the Inbox, and the pass says what happened in one message, true, not that nothing was built."""
    root, box, notes, _ = inbox
    book = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf", "Ann Writer"))
    a_pass(capsys, root)
    real = catalog.atomic_write

    def held_open(path, text, **more):
        if path.name == "data.js":
            raise PermissionError(13, "Access is denied", str(path))
        return real(path, text, **more)
    monkeypatch.setattr(catalog, "atomic_write", held_open)
    said = a_pass(capsys, root)
    assert "The catalog was built and kept in the database, but " in said and "hylly catalog --page writes it again" in said
    assert "wasn't built" not in said and "stay in the Inbox" not in said
    assert not book.exists() and (root / "ebooks" / "Lantern Wolf.epub").is_file()
    assert said.endswith("Inbox: 1 book added from the Inbox, details found for 1.\n")
    assert not (root / "catalog" / "data.js").exists()  # the server's copy: written by the next build, or hylly catalog --page
    assert said.count("The catalog was built and kept") == 1 and a_pass(capsys, root) == ""  # once, and nothing left to do
    monkeypatch.setattr(catalog, "atomic_write", real)
    cli.main(["catalog", "--page", "--library", str(root)])
    assert "Lantern Wolf" in (root / "catalog" / "data.js").read_text(encoding="utf-8")


def test_inboxes_one_inside_another_are_refused(inbox, capsys):
    root, box, _, write = inbox
    write(folders=[str(box / "comics")])
    inner = aged(epub(box / "comics" / "Moonwake.epub", "Moonwake"))
    outer = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    said = a_pass(capsys, root) + a_pass(capsys, root)
    assert inner.is_file() and outer.is_file() and said.count("and another Inbox overlap") == 2
    assert not (root / "ebooks").exists() and not (root / "comics").exists()
    told = [c for c in doctor.report(str(root))["checks"] if c["id"] == "inbox-overlap"]  # the doctor says it too
    assert [c["state"] for c in told] == [doctor.PROBLEM] * 2 and f"and another, {box / 'comics'}," in told[0]["says"]


def second_name(monkeypatch, tmp_path, root: Path) -> str:
    """The library's folder by a second name, as a second share name or a second mount of one share gives it: the same
    folder, but its own identities on the disk (another st_dev), and no path that leads to the first. Made as \\\\?\\ on
    Windows and a link elsewhere, with what tells them apart faked to say they're different."""
    alias = ("\\\\?\\" + str(root)) if os.name == "nt" else str(link_to(tmp_path / "second-share", root))
    real_identity = copies._identity

    def identity(path):
        found = real_identity(path)
        return (found[0] + 1, found[1]) if found and str(path).startswith(alias) else found

    monkeypatch.setattr(copies, "_identity", identity)
    monkeypatch.setattr(copies, "_resolved", lambda path: Path(os.path.abspath(path)))  # no link followed, as for a share
    return alias


def test_a_second_share_name_for_the_library_is_told_by_a_probe_and_nothing_is_removed(inbox, capsys, tmp_path, monkeypatch):
    root, box, _, write = inbox
    epub(tmp_path / "bought-elsewhere" / "Hedge Lore.epub", "Hedge Lore")
    copies.add_books(root, "ebooks", tmp_path / "bought-elsewhere", log=lambda line: None)
    copied = aged(root / "ebooks" / "Hedge Lore.epub")
    by_hand = aged(epub(root / "ebooks" / "Lantern Wolf.epub", "Lantern Wolf"))
    spelled = os.path.join(second_name(monkeypatch, tmp_path, root), "ebooks")
    guard = copies.Guard(root)
    assert not guard.overlaps(Path(spelled)) and guard.probed_overlap(Path(spelled))  # only the probe can tell
    write(folder=spelled)
    said = a_pass(capsys, root) + a_pass(capsys, root)
    assert copied.is_file() and by_hand.is_file() and said.count("found in the other") == 1
    assert not list(root.rglob(f"{copies.PROBE}*"))  # each probe gone at once
    st = by_hand.stat()
    book = copies.Waiting("ebooks", Path(spelled), Path(spelled) / "Lantern Wolf.epub", "Lantern Wolf.epub",
                          "ebooks/Lantern Wolf.epub", st.st_size, st.st_mtime)
    assert copies.remove(book, by_hand, copies.Guard(root)) == "library" and by_hand.is_file()  # the last word, too


def test_a_library_that_takes_no_probe_has_nothing_taken_from_the_inbox(inbox, capsys, monkeypatch):
    root, box, _, _ = inbox
    book = aged(epub(box / "Lantern Wolf.epub", "Lantern Wolf"))
    real = copies._probe

    def read_only(folder):
        if Path(folder) == root:
            raise PermissionError(13, "Permission denied")
        return real(folder)

    monkeypatch.setattr(copies, "_probe", read_only)
    said = a_pass(capsys, root) + a_pass(capsys, root)
    assert book.is_file() and said.count("the library, ") == 1 and "takes no file just now" in said
    assert not (root / "ebooks").exists()
    monkeypatch.setattr(copies, "_probe", real)
    a_pass(capsys, root)  # its first look at the Inbox
    a_pass(capsys, root)
    assert not book.exists() and (root / "ebooks" / "Lantern Wolf.epub").is_file()


def test_the_library_s_folders_are_walked_once_an_hour_or_when_its_top_folders_change(inbox, monkeypatch):
    root, _, _, _ = inbox
    (root / "ebooks" / "Novels").mkdir(parents=True)
    first = copies.Guard(root).folders()
    assert copies.folders_file().is_file() and copies._identity(root / "ebooks" / "Novels") in first
    walked = []
    real = copies.os.walk
    monkeypatch.setattr(copies.os, "walk", lambda *a, **k: (walked.append(1), real(*a, **k))[1])
    assert copies.Guard(root).folders() == first and walked == []  # kept from the last pass
    (root / "comics").mkdir()
    assert copies._identity(root / "comics") in copies.Guard(root).folders() and walked == [1]
    saved = json.loads(copies.folders_file().read_text(encoding="utf-8"))
    copies.folders_file().write_text(json.dumps({**saved, "at": saved["at"] - copies.FOLDERS_KEPT}), encoding="utf-8")
    copies.Guard(root).folders()
    assert walked == [1, 1]  # an hour on: walked again


def test_an_inbox_that_is_a_library_folder_by_a_second_share_name_is_told_by_a_probe(inbox, capsys, tmp_path, monkeypatch):
    """A folder of the library's, not its top, reached by a second share name or mount: no folder above it is the
    library's by its spelling, and its identities are its own. A probe made in it is found in that folder of the
    library's, by the library's spelling (the folders kept for the hour), and the Inbox is refused: the library's books
    in it are never taken in again elsewhere and removed."""
    root, _, _, _ = inbox
    book = aged(epub(root / "ebooks" / "Novels" / "Lantern Wolf.epub", "Lantern Wolf"))
    alias = str(link_to(tmp_path / "second-share", root / "ebooks"))
    real_identity = copies._identity
    monkeypatch.setattr(copies, "_identity", lambda path: (lambda found: (found[0] + 1, found[1]) if found and str(path)
                                                           .startswith(alias) else found)(real_identity(path)))
    monkeypatch.setattr(copies, "_resolved", lambda path: Path(os.path.abspath(path)))  # no link followed, as for a share
    guard = copies.Guard(root)
    assert not guard.overlaps(Path(alias)) and guard.probed_overlap(Path(alias))
    paths.settings_file().write_text(f"library = '{root}'\n[inbox]\nfolder = '{alias}'\n", encoding="utf-8")
    settings.reload()
    said = a_pass(capsys, root) + a_pass(capsys, root)
    assert book.is_file() and not (root / "comics").exists() and said.count("found in the other") == 1
    assert not list(root.rglob(f"{copies.PROBE}*"))


def test_an_inbox_s_record_of_what_was_given_of_another_kind_is_forgotten_not_a_crash(tmp_path):
    """read_state keeps each part of the kind it should be: an Inbox's record of the originals whose tags were given
    that isn't a table (a file edited by hand, say) is forgotten, so a pass never stops on it for good."""
    from hylly import folders

    state = folders.state_file(tmp_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    kept = {"Moonwake.epub": {"size": 1, "md5": "00"}}
    state.write_text(json.dumps({"tagged": {"/inbox/a": ["not", "a", "table"], "/inbox/b": kept}}), encoding="utf-8")
    assert folders.read_state(tmp_path)["tagged"] == {"/inbox/b": kept}

