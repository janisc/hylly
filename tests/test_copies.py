"""hylly add, into a folder of your own, and the copies part of hylly verify; and what a copy costs when the disk or the
share has trouble, each way found to lose one reproduced (once among the tests of writes cut short, test_storage.py)."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from pathlib import Path

import pytest
import subprocess
import time

from hylly import cli, copies, exits, manifest
from hylly import download as dl

HELP = Path(__file__).resolve().parents[1] / "src" / "hylly" / "help.html"


def setup(tmp_path):
    source = tmp_path / "bought-elsewhere"
    source.mkdir()
    (source / "The Lantern Keeper (Harbor Lights Book 2).epub").write_bytes(b"book one")
    (source / "Small Tools, Big Habits, 2nd Edition.epub").write_bytes(b"book two")
    listed = tmp_path / "add-list.csv"
    listed.write_text('file,category\nThe Lantern Keeper (Harbor Lights Book 2).epub,Science Fiction\n'
                      '"Small Tools, Big Habits, 2nd Edition.epub",Programming\n', encoding="utf-8")
    return source, listed, tmp_path / "library"


def settings(tmp_path, monkeypatch, text: str) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(path))


def test_add_copies_by_the_list_checks_and_verifies(tmp_path):
    source, listed, root = setup(tmp_path)
    quiet = lambda s: None  # noqa: E731
    assert copies.add_books(root, "ebooks", source, listed, dry_run=True, log=quiet) == 2 and not (root / "ebooks").exists()
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 2
    assert (root / "ebooks" / "Science Fiction" / "The Lantern Keeper (Harbor Lights Book 2).epub").read_bytes() == b"book one"
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 0  # already copied: the list can just grow
    assert copies.verify(root, check_hash=True) == (2, [])
    gone = root / "ebooks" / "Science Fiction" / "The Lantern Keeper (Harbor Lights Book 2).epub"
    gone.unlink()  # recorded, then deleted or lost: copied again, not taken for done
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 1 and gone.read_bytes() == b"book one"
    assert copies.verify(root, check_hash=True) == (2, [])

    (root / "ebooks" / "Programming" / "Small Tools, Big Habits, 2nd Edition.epub").write_bytes(b"book 2!!")
    assert copies.verify(root) == (2, [])  # same size: only the MD5 check notices
    assert copies.verify(root, check_hash=True)[1] == [("ebooks/Programming/Small Tools, Big Habits, 2nd Edition.epub", "MD5 mismatch")]


def test_verify_says_a_copy_s_problems_in_the_words_it_says_a_download_s(tmp_path):
    """One check for both (download.verify_files), as hylly verify lists them together: missing, the size against the
    record's, MD5 mismatch."""
    root, source = tmp_path / "library", tmp_path / "from"
    source.mkdir()
    for name in ("gone", "longer", "changed"):
        (source / f"{name}.epub").write_bytes(b"book " + name.encode())
    copies.add_books(root, "own", source, log=lambda s: None)
    record = dl.Manifest(root, "humble")
    for name in ("gone", "longer", "changed"):
        data = b"book " + name.encode()
        (root / "humble").mkdir(parents=True, exist_ok=True)
        (root / "humble" / f"{name}.epub").write_bytes(data)
        record.add({"path": f"humble/{name}.epub", "size": len(data), "md5": hashlib.md5(data).hexdigest()})
    for top in ("own", "humble"):
        (root / top / "gone.epub").unlink()
        (root / top / "longer.epub").write_bytes(b"book longer, and more")
        (root / top / "changed.epub").write_bytes(b"book chang3d")  # the same size: only the MD5 tells
    n, copied = copies.verify(root, check_hash=True)

    def said(problems):
        return sorted((path.split("/", 1)[1], problem) for path, problem in problems)

    assert n == 3 and said(copied) == said(dl.verify(record, root, check_hash=True)) == [
        ("changed.epub", "MD5 mismatch"), ("gone.epub", "missing"), ("longer.epub", "size 21, expected 11")]


def test_add_takes_the_folder_s_settings(tmp_path, monkeypatch, capsys):
    source, listed, root = setup(tmp_path)
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    assert "2 books in the list, 2 to copy" in capsys.readouterr().out
    assert (root / "ebooks" / "Programming").is_dir()
    with pytest.raises(SystemExit) as nowhere:
        cli.main(["add", "shelf"])  # no copy_from for it
    assert "[folders.shelf]" in str(nowhere.value)
    with pytest.raises(SystemExit) as shop:
        cli.main(["add", "humble", "--from", str(source)])
    assert "a shop's folder" in str(shop.value)


def test_a_source_that_isnt_there_is_no_success_with_nothing_to_copy(tmp_path, capsys):
    """A mistyped or unmounted source said as such, with code 6, in the dry run too, as hylly copy says it: not "0 books",
    which a scheduled run would take for success."""
    source, _, root = setup(tmp_path)
    root.mkdir()
    for dry in ([], ["--dry-run"]):
        for missing in (tmp_path / "nosuchsrc", source / "Small Tools, Big Habits, 2nd Edition.epub"):  # gone, or a file
            with pytest.raises(SystemExit) as e:
                cli.main(["add", "ebooks", "--from", str(missing), "--library", str(root), *dry])
            err = capsys.readouterr()
            assert e.value.code == exits.LOCAL and "Can't read the source" in err.err and "Is the disk there" in err.err
            assert "0 books" not in err.out and not (root / "ebooks").exists()


def test_without_a_list_everything_comes_in_its_subfolders(tmp_path, monkeypatch, capsys):
    source, _, root = setup(tmp_path)
    (source / "Folklore & Myth").mkdir()
    (source / "Folklore & Myth" / "Hedge Lore_copy.epub").write_bytes(b"book three")
    (source / "notes.txt").write_text("not a book", encoding="utf-8")
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.shelf]\nstrip_suffix = '_copy'\n")
    cli.main(["add", "shelf", "--from", str(source)])
    assert "3 books in " in capsys.readouterr().out
    assert (root / "shelf" / "Folklore & Myth" / "Hedge Lore.epub").read_bytes() == b"book three"  # the suffix dropped
    assert (root / "shelf" / "Small Tools, Big Habits, 2nd Edition.epub").is_file() and not (root / "shelf" / "notes.txt").exists()


def test_strip_suffix_is_read_but_offered_neither_in_the_settings_example_nor_in_the_help():
    """A suffix every copy's name drops is one person's way of naming files: a folder's settings that have it
    still work (above), but neither settings.example.toml nor the help offers it."""
    repo = HELP.parents[2]
    assert "strip_suffix" not in (repo / "settings.example.toml").read_text(encoding="utf-8")
    assert "strip_suffix" not in HELP.read_text(encoding="utf-8")


def test_the_example_list_is_named_for_hylly_add_and_reads_as_the_help_shows_it(tmp_path):
    """The example of a list hylly add copies by is examples/add-list.csv, no longer shelves.csv, which read as
    the library page's Shelves (docs/glossary.md); the settings example and the help name it, and its rows go into
    the subfolders they name, as the help's example of it shows."""
    repo = HELP.parents[2]
    example = repo / "examples" / "add-list.csv"
    assert example.is_file() and not (repo / "examples" / "shelves.csv").exists()
    assert "examples\\add-list.csv" in (repo / "settings.example.toml").read_text(encoding="utf-8")
    page = html.unescape(HELP.read_text(encoding="utf-8"))
    assert "add-list.csv" in page and "shelves.csv" not in page
    shown = re.search(r"<pre>(file,category\n.*?)</pre>", page, re.S)[1]
    assert example.read_text(encoding="utf-8").startswith(shown)  # the help's rows are the example's first
    ops = copies.list_ops(example, tmp_path, "ebooks")
    assert [op.dest for op in ops][:2] == ["ebooks/Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub",
                                           "ebooks/Programming/Small Tools, Big Habits, 2nd Edition.epub"]


def test_a_list_saved_from_a_spreadsheet_reads_and_one_without_its_columns_is_said_with_its_line(tmp_path):
    """Excel's "CSV UTF-8" puts a byte order mark first, which made the first column's name another, and a KeyError
    traceback of hylly add; a row without a category went to a folder named None. Now the mark, capitals, other
    columns, empty rows, a semicolon between the columns and Excel's own sep= line read as the list meant; a list
    without the two columns, or naming one twice, a book's line without a file or without a cell for its category, or a
    list that isn't UTF-8 text stops the run before anything is copied, with exit code 2, naming the list and the line."""
    source, _, root = setup(tmp_path)
    listed = tmp_path / "from-a-spreadsheet.csv"
    wanted = ["ebooks/Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub",
              "ebooks/Programming/Small Tools, Big Habits, 2nd Edition.epub"]
    for text in ('\ufeffFile;Category;Notes\r\nThe Lantern Keeper (Harbor Lights Book 2).epub;Science Fiction;\r\n;;\r\n'
                 'Small Tools, Big Habits, 2nd Edition.epub;Programming;a gift\r\n',
                 '\ufeffcategory,file\nScience Fiction,The Lantern Keeper (Harbor Lights Book 2).epub\n,\n'
                 'Programming,"Small Tools, Big Habits, 2nd Edition.epub"\n',
                 'sep=;\nfile;category\nThe Lantern Keeper (Harbor Lights Book 2).epub;Science Fiction\n'
                 'Small Tools, Big Habits, 2nd Edition.epub;Programming\n'):
        listed.write_text(text, encoding="utf-8", newline="")
        assert [op.dest for op in copies.list_ops(listed, source, "ebooks")] == wanted, text
    for text, says in (("name,folder\nA Book.epub,Poetry\n", "line 1: it should name the two columns, file and category"),
                       ("file,category\nThe Lantern Keeper (Harbor Lights Book 2).epub,Science Fiction\nA Book.epub\n",
                        "line 3: no category"),
                       ("file,category\n,Poetry\n", "line 2: no file"), ("file\nA Book.epub\n", "line 1:"),
                       ("sep=,\nfile,category\nA Book.epub\n", "line 3: no category"),
                       ("File,category,file\nA Book.epub,Poetry,B Book.epub\n", "line 1: it names the file column twice")):
        listed.write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit) as refused:
            copies.add_books(root, "ebooks", source, listed, log=lambda s: None)
        assert refused.value.code == exits.USAGE and f"{listed}, {says}" in str(refused.value), text
        assert "Nothing was copied" in str(refused.value) and not (root / "ebooks").exists()
    listed.write_bytes("file,category\nCafé Lantern.epub,Poésie\n".encode("cp1252"))  # Excel's plain CSV, on Windows
    with pytest.raises(SystemExit) as refused:
        copies.add_books(root, "ebooks", source, listed, log=lambda s: None)
    assert refused.value.code == exits.USAGE and "isn't UTF-8 text: save it from the spreadsheet as CSV UTF-8" in str(refused.value)


def test_a_list_that_worked_before_puts_each_book_where_it_did(tmp_path):
    """A category is taken as written, as it always was: a space after the comma is part of the folder's name, and an
    empty category is the top of the folder. Trimmed, a list run before would copy its books a second time, elsewhere."""
    source, _, _ = setup(tmp_path)
    listed = tmp_path / "add-list.csv"
    listed.write_text("file,category\nThe Lantern Keeper (Harbor Lights Book 2).epub, Science Fiction\n"
                      '"Small Tools, Big Habits, 2nd Edition.epub",\n', encoding="utf-8")
    assert [op.dest for op in copies.list_ops(listed, source, "ebooks")] == [
        "ebooks/ Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub", "ebooks//Small Tools, Big Habits, 2nd Edition.epub"]


def test_copied_books_need_no_original(tmp_path):
    source, listed, root = setup(tmp_path)
    quiet = lambda s: None  # noqa: E731
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 2
    for f in source.iterdir():  # the books' own folder emptied after copying
        f.unlink()
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 0  # nothing missing that matters
    listed.write_text("file,category\nGone Book.epub,Folklore & Myth\n", encoding="utf-8")
    with pytest.raises(SystemExit) as gone:  # a listed book not copied yet and not there stops the copy
        copies.add_books(root, "ebooks", source, listed, log=quiet)
    assert "Gone Book.epub" in str(gone.value)


def test_a_listed_book_whose_copy_and_original_are_both_gone_is_passed_over_and_said(tmp_path, monkeypatch, capsys):
    """A book hylly add copied in, then removed from the library on purpose, its original gone too: no failure, and no
    stop. Every run names it, counts it and says how to have it looked for no more, the others are copied, and it ends
    well; a listed book never copied and not there still stops the run."""
    source, listed, root = setup(tmp_path)
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    removed = root / "ebooks" / "Science Fiction" / "The Lantern Keeper (Harbor Lights Book 2).epub"
    removed.unlink()  # taken out of the library on purpose
    (source / removed.name).unlink()  # and its original with it
    (source / "Hedge Lore.epub").write_bytes(b"book three")
    listed.write_text(listed.read_text(encoding="utf-8") + "Hedge Lore.epub,Folklore & Myth\n", encoding="utf-8")
    capsys.readouterr()
    for dry in (["--dry-run"], []):
        cli.main(["add", "ebooks", *dry, "--json"])
        out, err = capsys.readouterr()
        found = json.loads(out)
        assert found["exit_code"] == 0 and found["gone"] == ["ebooks/Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub"]
        assert found["would_copy" if dry else "copied"] == 1 and "3 books in the list, 1 to copy" in err
        assert "GONE: ebooks/Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub" in err
        assert ("Passed over 1 listed book whose copy in the library and original are both gone" in err
                and "The Lantern Keeper (Harbor Lights Book 2).epub. To have it looked for no more, take its line out of "
                f"{listed}." in err)
    assert (root / "ebooks" / "Folklore & Myth" / "Hedge Lore.epub").read_bytes() == b"book three" and not removed.exists()
    quiet, said = (lambda s: None), []  # noqa: E731
    assert copies.add_books(root, "ebooks", source, listed, log=said.append) == 0  # and again: said, with nothing to copy
    assert any(line.startswith("Passed over 1 listed book") for line in said) and said[-1].startswith("Nothing left to copy")
    listed.write_text(listed.read_text(encoding="utf-8") + "Never Copied.epub,Folklore & Myth\n", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        copies.add_books(root, "ebooks", source, listed, log=quiet)
    assert stopped.value.code == exits.NOT_SET_UP and "Listed in add-list.csv but not found" in str(stopped.value)
    assert "Never Copied.epub" in str(stopped.value) and "Lantern Keeper" not in str(stopped.value)
    listed.write_text("file,category\nThe Lantern Keeper (Harbor Lights Book 2).epub,Science Fiction\n"
                      '"Small Tools, Big Habits, 2nd Edition.epub",Programming\n', encoding="utf-8")
    (source / removed.name).write_bytes(b"book one")  # its original back: copied again, as before
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 1 and removed.read_bytes() == b"book one"


def three_copied(tmp_path, monkeypatch):
    """A list of three books, each copied in by hylly add, their originals then cleared away, as copy_from is once the
    copies are checked: (the library's folder for them, the list)."""
    source, listed, root = setup(tmp_path)
    (source / "Hedge Lore.epub").write_bytes(b"book three")
    listed.write_text(listed.read_text(encoding="utf-8") + "Hedge Lore.epub,Folklore & Myth\n", encoding="utf-8")
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    for book in source.iterdir():
        book.unlink()
    return root / "ebooks", listed


def stopped(capsys, *dry, code: int = exits.NOT_SET_UP) -> str:
    """hylly add ebooks, which must stop, as every list with books gone once did: with code 5 (or the code given), and
    nothing copied."""
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["add", "ebooks", *dry])
    said = capsys.readouterr()
    assert e.value.code == code and ("Nothing was copied" in said.err or code != exits.NOT_SET_UP), said
    assert "GONE" not in said.out and "take" not in said.err  # no book is said removed, nor a line to be taken out
    return said.err


def test_a_list_s_folder_moved_away_stops_the_run_and_isn_t_taken_for_books_removed(tmp_path, monkeypatch, capsys):
    """Passing over a book gone is for a book you removed, not a folder moved or renamed, or a disk away: the list's
    own folder in the library not there stops the run, nothing copied, the dry run too, with a word for each, and none
    of taking lines out of the list; so does one that can't be read, which is never "gone"."""
    folder, _ = three_copied(tmp_path, monkeypatch)
    folder.rename(tmp_path / "moved elsewhere")
    for dry in ([], ["--dry-run"]):
        words = stopped(capsys, *dry)
        assert f"{folder}, where the books add-list.csv lists were copied, isn't there" in words
        assert "moved or renamed" in words and "disk away" in words and not folder.exists()
    (tmp_path / "moved elsewhere").rename(folder)
    one = folder / "Science Fiction" / "The Lantern Keeper (Harbor Lights Book 2).epub"
    one.unlink()  # one of three removed on purpose: passed over, as before
    cli.main(["add", "ebooks"])
    assert "Passed over 1 listed book" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="a folder's rights on Linux and macOS")
def test_a_folder_that_can_t_be_read_is_never_gone(tmp_path, monkeypatch, capsys):
    """A folder that can't be read hides the books in it, which aren't gone for all that, on every Python (pathlib's
    is_file says no file for it since 3.14): the list's own folder in the library stops the run with code 5, nothing
    copied, saying it can't be read; a folder in it, or the originals' folder, as a folder that can't be read stops any
    command, with code 6 and the path."""
    folder, _ = three_copied(tmp_path, monkeypatch)
    (folder / "Programming" / "Small Tools, Big Habits, 2nd Edition.epub").unlink()  # one removed: all else would pass
    source = tmp_path / "bought-elsewhere"
    for sealed in (folder, folder / "Programming", folder / "Science Fiction", source):
        os.chmod(sealed, 0)
        try:
            words = stopped(capsys) if sealed == folder else stopped(capsys, code=exits.LOCAL)
        finally:
            os.chmod(sealed, 0o755)
        assert (f"{folder}, where the books add-list.csv lists are copied, can't be read" in words if sealed == folder
                else "Permission denied" in words and str(sealed) in words)
    cli.main(["add", "ebooks"])
    assert "Passed over 1 listed book" in capsys.readouterr().out and not (folder / "Programming" / "Small Tools, Big Habits, 2nd Edition.epub").exists()


def test_more_than_half_of_a_list_s_books_gone_at_once_stops_the_run(tmp_path, monkeypatch, capsys):
    """Two or more of the books a list has copied, more than half of them, gone at once with their originals: more like a
    folder moved or a disk away than books removed, as the catalog stops at a source down by more than half. The run
    stops, nothing copied; one of three, or the one book of a list of one, is passed over as removed."""
    folder, listed = three_copied(tmp_path, monkeypatch)
    for gone in ("Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub", "Folklore & Myth/Hedge Lore.epub"):
        (folder / gone).unlink()
    words = stopped(capsys)
    assert "2 of the 3 books add-list.csv lists as copied before are gone" in words and "more than half" in words
    assert "folder moved or renamed, or a disk that isn't there" in words
    listed.write_text("file,category\nHedge Lore.epub,Folklore & Myth\n", encoding="utf-8")  # a list of one, its book removed
    cli.main(["add", "ebooks"])
    assert "Passed over 1 listed book" in capsys.readouterr().out


def test_wherever_the_help_says_the_originals_can_go_it_says_checked_with_verify_hash_first():
    """The check made during a run isn't proof that a copy arrived, as a NAS may answer from what it caches:
    wherever the help says the originals may go (their folder emptied, or before removing them), it says checked with
    hylly verify --hash first."""
    text = html.unescape(re.sub(r"<[^>]+>", "", HELP.read_text(encoding="utf-8")))
    said = [s for s in re.split(r"(?<=[.!?])\s+", text)
            if re.search(r"originals'? (folder )?can be emptied|before removing any originals", s, re.I)]
    assert said and all("hylly verify --hash" in s for s in said), said


# ---- two books, one name ------------------------------------------------------------------------------

def twins(tmp_path):
    """Two different books with one file name, in two folders, and a list for them."""
    source = tmp_path / "bought-elsewhere"
    for shelf, words in (("first", b"one book"), ("second", b"another book")):
        (source / shelf).mkdir(parents=True)
        (source / shelf / "Lantern Wolf.epub").write_bytes(words)
    return source, tmp_path / "add-list.csv", tmp_path / "library"


def test_two_listed_books_that_would_get_one_name_stop_the_copy_before_anything_is_copied(tmp_path):
    source, listed, root = twins(tmp_path)
    quiet = lambda s: None  # noqa: E731
    listed.write_text("file,category\nfirst/Lantern Wolf.epub,Novels\nsecond/Lantern Wolf.epub,Novels\n", encoding="utf-8")
    for dry_run in (True, False):  # the dry run says so too
        with pytest.raises(SystemExit) as refused:
            copies.add_books(root, "ebooks", source, listed, dry_run=dry_run, log=quiet)
        words = str(refused.value)
        assert refused.value.code == exits.NOT_SET_UP and "ebooks/Novels/Lantern Wolf.epub" in words
        assert str(source / "first" / "Lantern Wolf.epub") in words and str(source / "second" / "Lantern Wolf.epub") in words
        assert "Nothing was copied" in words and "another category in add-list.csv" in words
    assert not (root / "ebooks").exists()

    listed.write_text("file,category\nfirst/Lantern Wolf.epub,Novels\nsecond/lantern wolf.EPUB,Novels\n"  # case aside, one name
                      "first/Lantern Wolf.epub,Novels\n", encoding="utf-8")  # one book listed twice is no clash, though
    with pytest.raises(SystemExit, match="Nothing was copied"):
        copies.add_books(root, "ebooks", source, listed, log=quiet)
    listed.write_text("file,category\nfirst/Lantern Wolf.epub,Novels\nfirst/Lantern Wolf.epub,Novels\n", encoding="utf-8")
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 2  # the second time it's already there

    listed.write_text("file,category\nfirst/Lantern Wolf.epub,Novels\nsecond/Lantern Wolf.epub,Novels (other)\n", encoding="utf-8")
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 1  # another category: both are in
    assert (root / "ebooks" / "Novels (other)" / "Lantern Wolf.epub").read_bytes() == b"another book"
    assert copies.verify(root, check_hash=True) == (2, [])


def test_a_clash_names_the_first_few_and_how_many_more(tmp_path):
    source, listed, root = twins(tmp_path)
    rows = []
    for n in range(copies.SHOW_CLASHES + 2):
        for shelf in ("first", "second"):
            (source / shelf / f"Book {n}.epub").write_bytes(f"{shelf} {n}".encode())
            rows.append(f"{shelf}/Book {n}.epub,Novels")
    listed.write_text("file,category\n" + "\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        copies.add_books(root, "ebooks", source, listed, log=lambda s: None)
    assert "Book 0.epub" in str(refused.value) and f"Book {copies.SHOW_CLASHES}.epub" not in str(refused.value)
    assert "(and 2 more such names)" in str(refused.value)


def test_a_strip_suffix_that_makes_two_names_one_stops_the_copy(tmp_path, monkeypatch):
    source = tmp_path / "bought-elsewhere"
    source.mkdir()
    (source / "Moonwake.epub").write_bytes(b"the book")
    (source / "Moonwake_copy.epub").write_bytes(b"its earlier edition")
    root = tmp_path / "library"
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.shelf]\ncopy_from = '{source}'\nstrip_suffix = '_copy'\n")
    for dry_run in (["--dry-run"], []):
        with pytest.raises(SystemExit) as refused:
            cli.main(["add", "shelf", *dry_run])
        assert refused.value.code == exits.NOT_SET_UP
        assert "shelf/Moonwake.epub" in str(refused.value) and "Moonwake_copy.epub" in str(refused.value)
        assert "Rename one of them" in str(refused.value)
    assert not (root / "shelf").exists()


def test_a_book_whose_name_another_book_s_copy_has_is_never_reported_as_done(tmp_path, capsys):
    source, listed, root = twins(tmp_path)
    listed.write_text("file,category\nfirst/Lantern Wolf.epub,Novels\n", encoding="utf-8")
    assert copies.add_books(root, "ebooks", source, listed, log=lambda s: None) == 1
    listed.write_text("file,category\nsecond/Lantern Wolf.epub,Novels\n", encoding="utf-8")  # the other book, the same place
    for run in (["dry run"], [], []):  # said every time, and never a success
        with pytest.raises(SystemExit) as stays_out:
            copies.add_books(root, "ebooks", source, listed, dry_run=bool(run))
        said = capsys.readouterr().out
        assert "NAME TAKEN: ebooks/Novels/Lantern Wolf.epub" in said and str(source / "first" / "Lantern Wolf.epub") in said
        assert "1 has a name another book's copy has" in str(stays_out.value) and "another category in add-list.csv" in str(stays_out.value)
        assert "tries those again" not in str(stays_out.value)
    assert (root / "ebooks" / "Novels" / "Lantern Wolf.epub").read_bytes() == b"one book"  # left as it was


def test_the_same_book_from_a_folder_moved_since_is_already_there(tmp_path):
    source, listed, root = setup(tmp_path)
    quiet = lambda s: None  # noqa: E731
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 2
    moved = source.rename(tmp_path / "moved")
    assert copies.add_books(root, "ebooks", moved, listed, log=quiet) == 0  # the same books as recorded, by size and MD5
    assert all(e["src"].startswith(str(source)) for e in copies.recorded(root).values())  # their records as they were
    for f in moved.iterdir():
        f.unlink()
    assert copies.add_books(root, "ebooks", tmp_path / "elsewhere", listed, log=quiet) == 0  # gone: nothing left to lose
    assert copies.verify(root, check_hash=True) == (2, [])


def test_a_book_from_copy_from_spelled_another_way_is_already_there_though_its_copy_has_changed(tmp_path, monkeypatch, capsys):
    source, listed, root = setup(tmp_path)
    quiet = lambda s: None  # noqa: E731
    assert copies.add_books(root, "ebooks", source, listed, log=quiet) == 2
    copy = root / "ebooks" / "Science Fiction" / "The Lantern Keeper (Harbor Lights Book 2).epub"
    copy.write_bytes(b"book one, with its tags written in")  # changed in the library since
    elsewhere = source.rename(tmp_path / "E-drive")  # copy_from as another drive letter, a mount or a junction spells it
    for dry_run in (True, False):
        assert copies.add_books(root, "ebooks", elsewhere, listed, dry_run=dry_run) == 0
    assert "NAME TAKEN" not in capsys.readouterr().out and copy.read_bytes() == b"book one, with its tags written in"

    read, real = [], copies.md5_of
    monkeypatch.setattr(copies, "md5_of", lambda path: read.append(path) or real(path))
    book = elsewhere / "The Lantern Keeper (Harbor Lights Book 2).epub"
    book.write_bytes(b"book 1!!")  # another book of the recorded one's size: its MD5 tells them apart
    with pytest.raises(SystemExit):
        copies.add_books(root, "ebooks", elsewhere, listed, dry_run=True)
    assert "NAME TAKEN: ebooks/Science Fiction/The Lantern Keeper" in capsys.readouterr().out and book in read
    read.clear()
    book.write_bytes(b"a book of another size")  # not even read
    with pytest.raises(SystemExit):
        copies.add_books(root, "ebooks", elsewhere, listed, dry_run=True)
    assert "NAME TAKEN: ebooks/Science Fiction/The Lantern Keeper" in capsys.readouterr().out and book not in read


def test_a_book_is_read_once_to_be_copied_and_hashed_and_its_copy_read_back_once(tmp_path, monkeypatch):
    """hylly add hashes a book as it copies it (copies.hashed_copy), and checks the copy, read back, against that: the
    book isn't read a second time only to be hashed first. Its record has the MD5 of what was read, which verify --hash
    reads every copy against; a copy that doesn't read back as the book isn't kept."""
    source, listed, root = setup(tmp_path)
    read, real = [], copies.md5_of
    monkeypatch.setattr(copies, "md5_of", lambda path: read.append(path) or real(path))
    assert copies.add_books(root, "ebooks", source, listed, log=lambda s: None) == 2
    assert not [p for p in read if p.parent == source]  # the books: read only to be copied
    assert sorted(p.name for p in read) == sorted(f"{f.name}.copying" for f in source.iterdir())  # each copy, read back
    assert {e["md5"] for e in copies.recorded(root).values()} == {hashlib.md5(b"book one").hexdigest(),
                                                                 hashlib.md5(b"book two").hexdigest()}
    assert copies.verify(root, check_hash=True) == (2, [])

    another = tmp_path / "another"
    another.mkdir()
    (another / "Another.epub").write_bytes(b"book three")
    monkeypatch.setattr(copies, "md5_of", lambda path: hashlib.md5(b"what the disk gave back") if path.suffix == ".copying"
                        else real(path))
    monkeypatch.setattr(copies.time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit, match="0 copied, 1 couldn't be"):
        copies.add_books(root, "more", another, log=lambda s: None)
    assert not (root / "more" / "Another.epub").exists() and not list(root.rglob("*.copying"))


def test_hylly_add_opens_each_book_once_whatever_would_read_it(tmp_path, monkeypatch):
    """However a book could be read (pathlib's open, the open everyone else calls, a copy by shutil), hylly add opens
    each one once, to copy and hash it together: a stand-in for open counts them."""
    import builtins
    import io

    source, listed, root = setup(tmp_path)
    opened, real = [], io.open

    def counted(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)) and Path(file).parent == source:
            opened.append((Path(file).name, mode))
        return real(file, mode, *a, **k)

    monkeypatch.setattr(io, "open", counted)
    monkeypatch.setattr(builtins, "open", counted)
    assert copies.add_books(root, "ebooks", source, listed, log=lambda s: None) == 2
    assert sorted(opened) == sorted((f.name, "rb") for f in source.iterdir())
    assert copies.verify(root, check_hash=True) == (2, [])


def test_a_book_that_grows_between_its_look_and_its_copy_is_recorded_as_its_copy_is(tmp_path, monkeypatch):
    """The record's size is what was read, counted as it's hashed (hashed_copy), as its MD5 is: a book that grew after
    hylly add looked at it (10 bytes, then 19) is recorded as the copy made of it, and verify, with --hash or without,
    finds nothing wrong. No refusal: what was read is what was copied, read back and checked."""
    root, source = tmp_path / "library", tmp_path / "from"
    source.mkdir()
    book = source / "Lantern Wolf.epub"
    book.write_bytes(b"ten bytes!")
    real, grown = copies.hashed_copy, []

    def growing(src, tmp):
        if not grown:
            with open(src, "ab") as fh:
                fh.write(b" and more")  # still coming in, say
            grown.append(src)
        return real(src, tmp)

    monkeypatch.setattr(copies, "hashed_copy", growing)
    assert copies.add_books(root, "own", source, log=lambda s: None) == 1
    (record,) = copies.recorded(root).values()
    assert (root / "own" / "Lantern Wolf.epub").read_bytes() == b"ten bytes! and more" == book.read_bytes()
    assert record["size"] == 19 and record["md5"] == hashlib.md5(b"ten bytes! and more").hexdigest()
    assert copies.verify(root) == copies.verify(root, check_hash=True) == (1, [])


def test_a_run_with_nothing_left_to_copy_says_so_and_verify_reads_only_the_records(tmp_path, monkeypatch, capsys):
    source, listed, root = setup(tmp_path)
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    assert "run 'hylly add ebooks' again: once it has nothing left to copy" in capsys.readouterr().out
    cli.main(["add", "ebooks"])
    said = capsys.readouterr().out
    assert "2 books in the list, 0 to copy" in said and "Nothing left to copy." in said and "'hylly verify --hash'" in said

    listed.write_text("file,category\n", encoding="utf-8")  # emptied by mistake
    cli.main(["add", "ebooks"])
    said = capsys.readouterr().out
    assert "0 books in the list" in said and "Nothing left to copy" not in said  # a list read as empty is no all-clear

    listed.write_text("name,shelf\nNot A Book.epub,Novels\n", encoding="utf-8")  # a list gone wrong, and the originals gone
    for f in source.iterdir():
        f.unlink()
    cli.main(["verify", "--hash"])  # neither is any of verify's business: the copies are what it checks
    assert "Checked 2 files with MD5: 0 problems." in capsys.readouterr().out


def test_verify_says_how_many_files_in_your_own_folders_it_has_no_record_to_check_against(tmp_path, monkeypatch, capsys):
    """Only hylly add's copies are recorded: the files put in your own folders by hand can't be checked, and its 0
    problems mustn't read as a clean bill of health for them. The last catalog build counts them."""
    import json

    from hylly import catalog

    source, listed, root = setup(tmp_path)
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    (root / "ebooks" / "Science Fiction" / "Lantern Wolf.epub").write_bytes(b"put there by hand")
    (root / "comics").mkdir()
    (root / "comics" / "Moonwake 001.cbz").write_bytes(b"and so was this")
    capsys.readouterr()
    cli.main(["verify"])  # before the first build: how many isn't known, that there are some is
    assert capsys.readouterr().out == ("Checked 2 files: 0 problems.\nFiles you put in your own folders yourself have no record "
                                       "to check against: verify checks downloaded and copied files (after hylly catalog, it "
                                       "says how many).\n")
    catalog.build(root, log=lambda s: None)
    cli.main(["verify"])
    assert capsys.readouterr().out == ("Checked 2 files: 0 problems.\n2 files in your own folders have no record to check "
                                       "against: verify checks downloaded and copied files.\n")
    cli.main(["verify", "--json"])
    assert json.loads(capsys.readouterr().out) == {"checked": 2, "with_md5": False, "problems": [], "removed": 0, "no_record": 2}
    (root / "comics" / "Moonwake 001.cbz").unlink()
    catalog.build(root, log=lambda s: None)
    cli.main(["verify", "--hash"])
    assert "Checked 2 files with MD5: 0 problems.\n1 file in your own folders has no record to check against" in capsys.readouterr().out
    (root / "ebooks" / "Science Fiction" / "Lantern Wolf.epub").unlink()
    catalog.build(root, log=lambda s: None)
    cli.main(["verify"])
    assert capsys.readouterr().out == "Checked 2 files: 0 problems.\n"  # every one of them is a copy, checked


def test_a_copy_you_removed_is_neither_missing_nor_copied_in_again_until_it_is_recorded_anew(tmp_path, monkeypatch, capsys):
    """A copy moved into _Removed by hand, at its own place below it, isn't missing for hylly verify, and hylly add
    doesn't copy it in again from its original, which is still there, saying so; nor once the Server page has emptied
    _Removed, which keeps its removal in the database. One recorded anew later (copied in again, after Put back say),
    and gone again, is missing: its own record is newer than its removal."""
    import json

    from hylly import db, manifest, removed

    source, listed, root = setup(tmp_path)
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    rel = "ebooks/Science Fiction/The Lantern Keeper (Harbor Lights Book 2).epub"
    (root / "_Removed" / rel).parent.mkdir(parents=True)
    (root / rel).rename(root / "_Removed" / rel)
    capsys.readouterr()
    cli.main(["verify"])
    assert capsys.readouterr().out.startswith("Checked 2 files: 0 problems.\n1 file you removed (in _Removed, emptied from it, "
                                              "or moved by hand as you answered) is no problem.\n")
    for _ in range(2):  # in _Removed, then emptied from it
        cli.main(["add", "ebooks"])
        said = capsys.readouterr().out
        assert f"REMOVED: {rel}: copied before, and you removed its copy (into _Removed), so it isn't copied again" in said
        assert "0 to copy" in said and not (root / rel).exists()
        found = removed.listing(root)
        if found["count"]:
            assert [(f["held"], f["path"], f["size"], f["at"]) for f in found["files"]] == [(rel, rel, 8, "")]  # by hand
            assert removed.empty(root, found["digest"]) == {"deleted": 1, "bytes": 8, "left": [], "more": False}
    assert not (root / "_Removed").exists()
    cli.main(["verify", "--json"])
    said = json.loads(capsys.readouterr().out)
    assert (said["problems"], said["removed"]) == ([], 1)
    (root / rel).write_bytes(b"book one")  # recorded anew, later than it was emptied, as hylly add records a copy
    manifest.add_copy(root, {"src": str(source / "The Lantern Keeper (Harbor Lights Book 2).epub"), "dest": rel, "size": 8,
                             "md5": "x", "copied_at": "2099-01-01T00:00:00+00:00"})
    (root / rel).unlink()
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["verify"])
    assert f"missing: {rel}" in capsys.readouterr().out
    with monkeypatch.context() as m:  # and hylly add copies it in again: a loss, not a removal
        m.setattr(db, "now", lambda: "2099-01-01T00:00:01+00:00")
        cli.main(["add", "ebooks"])
    assert (root / rel).read_bytes() == b"book one"


def test_a_copy_removed_on_the_server_page_isn_t_copied_in_again_nor_asked_about_again(tmp_path, monkeypatch, capsys):
    """A copy of a folder of yours removed on the Server page (removed.remove, its removal kept) isn't copied in again
    from its original by hylly add, so its card, and a question of two copies it was one of, don't come back; put back,
    it is there again, and nothing more is copied."""
    from urllib.parse import quote

    from hylly import removed

    source, listed, root = setup(tmp_path)
    settings(tmp_path, monkeypatch, f"library = '{root}'\n[folders.ebooks]\ncopy_from = '{source}'\nlist = '{listed}'\n")
    cli.main(["add", "ebooks"])
    rel = "ebooks/Programming/Small Tools, Big Habits, 2nd Edition.epub"
    card = {"id": "f:smalltools", "t": "Small Tools, Big Habits", "f": [[quote(rel), "epub", 8]]}
    (done,), trouble = removed.remove(root, removed.going(root, [card]), by="owner")
    assert trouble == "" and done["held"] == rel and not (root / rel).exists()
    capsys.readouterr()
    cli.main(["add", "ebooks"])
    assert f"REMOVED: {rel}" in capsys.readouterr().out and not (root / rel).exists()
    removed.put_back(root, rel, by="owner")
    cli.main(["add", "ebooks"])
    assert (root / rel).read_bytes() == b"book two" and "0 to copy" in capsys.readouterr().out


def test_before_the_first_build_verify_says_nothing_of_own_folders_with_no_book(tmp_path, capsys):
    """A library at a disk's root has the systems' own folders at its top, and tools leave theirs beside your books: a
    build reads no book in them (catalog.own_folders, book_files), so neither has one verify can't check. Nor has an own
    folder that is empty, or holds no book."""
    root = tmp_path / "library"
    for folder in ("$RECYCLE.BIN", "System Volume Information", "ebooks/.stversions"):
        (root / folder).mkdir(parents=True)
        (root / folder / "Lantern Wolf.epub").write_bytes(b"an old copy, not a book of yours")
    (root / "ebooks" / "Science Fiction").mkdir()
    (root / "ebooks" / "notes.txt").write_text("not a book", encoding="utf-8")
    (root / "comics").mkdir()
    cli.main(["verify", "--library", str(root)])
    assert capsys.readouterr().out == "Checked 0 files: 0 problems.\n"
    (root / "ebooks" / "Science Fiction" / "The Lantern Keeper.epub").write_bytes(b"put there by hand")
    cli.main(["verify", "--library", str(root)])
    assert "\nFiles you put in your own folders yourself have no record to check against" in capsys.readouterr().out


# ---- a copy cut short costs as little as possible: each way found to lose one (once in test_storage.py) --------------

def test_a_copy_tries_again_for_longer_says_so_and_gives_up_in_the_end(monkeypatch, capsys):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)

    def never():
        raise OSError(22, "Invalid argument")

    def full():
        raise OSError(28, "No space left on device")

    with pytest.raises(copies.GaveUp, match="Invalid argument"):
        copies.with_retries(never, "own/a.epub")
    assert slept == [5, 10, 20, 40, 60] and capsys.readouterr().err.count("retrying ([Errno 22] Invalid argument) own/a.epub") == 5
    with pytest.raises(OSError) as e:  # a full disk is no passing trouble
        copies.with_retries(full, "own/a.epub")
    assert e.value.errno == 28 and not isinstance(e.value, copies.GaveUp) and len(slept) == 5 and not capsys.readouterr().err


def test_a_copy_in_is_checked_before_it_takes_its_name_and_one_that_doesnt_check_leaves_nothing(tmp_path, monkeypatch):
    source = tmp_path / "a.epub"
    source.write_bytes(b"book a")
    dest = tmp_path / "library" / "own" / "a.epub"
    seen = []
    copies.copy_in(source, dest, hashlib.md5(b"book a").hexdigest(), before=lambda: seen.append(dest.exists()))
    assert dest.read_bytes() == b"book a" and seen == [False] and not list(tmp_path.rglob("*.copying"))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    other = dest.with_name("b.epub")
    with pytest.raises(copies.GaveUp, match="does not match"):
        copies.copy_in(source, other, "0" * 32, "own/b.epub", before=lambda: seen.append("never"))
    assert not other.exists() and not list(tmp_path.rglob("*.copying")) and seen == [False]


# ---- hylly add, when the disk or the share has trouble (once in test_storage.py) -------------------------------------

def library_and_source(tmp_path):
    source = tmp_path / "from"
    source.mkdir()
    (source / "a.epub").write_bytes(b"book a")
    (source / "b.epub").write_bytes(b"book b")
    return tmp_path / "library", source


def test_a_copy_reaches_the_disk_before_its_record(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    seen = []
    real_sync, real_add = copies.sync, copies.add_copy
    monkeypatch.setattr(copies, "sync", lambda p: (seen.append("on the disk"), real_sync(p))[1])
    monkeypatch.setattr(copies, "add_copy", lambda *a: (seen.append("recorded"), real_add(*a))[1])
    copies.add_books(root, "own", source, log=lambda s: None)
    assert seen == ["on the disk", "recorded"] * 2


def test_one_problem_book_doesnt_stop_the_others(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    real = copies.copy_one

    def picky(r, op):
        if op.src.name == "a.epub":
            raise OSError("this one can't be read")
        return real(r, op)

    monkeypatch.setattr(copies, "copy_one", picky)
    with pytest.raises(SystemExit, match="1 copied, 1 couldn't be"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert (root / "own" / "b.epub").exists() and not (root / "own" / "a.epub").exists()


def test_add_stops_when_the_disk_stops_taking_copies(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    for n in range(8):
        (source / f"{n}.epub").write_bytes(b"book %d" % n)
    tried = []

    def refused(src, dst):
        tried.append(src)
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(copies, "hashed_copy", refused)
    monkeypatch.setattr(copies.time, "sleep", lambda s: None)
    monkeypatch.setattr(copies, "_takes_writes", lambda r: False)
    with pytest.raises(SystemExit, match="carries on"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert len(set(tried)) == copies.GIVE_UP_AFTER


def test_books_the_disk_refuses_for_good_dont_stop_every_run_at_the_same_place(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    for n in range(copies.GIVE_UP_AFTER):  # sorted before a.epub
        (source / f"{n}.epub").write_bytes(b"book %d" % n)
    real = copies.hashed_copy

    def refused(src, dst):
        if src.name[0].isdigit():
            raise OSError(22, "Invalid argument")
        return real(src, dst)

    monkeypatch.setattr(copies, "hashed_copy", refused)
    monkeypatch.setattr(copies.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match=f"2 copied, {copies.GIVE_UP_AFTER} couldn't"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert (root / "own" / "a.epub").is_file() and (root / "own" / "b.epub").is_file()
    assert not list((root / "state").glob("*probe*"))


def test_a_success_starts_the_count_of_failures_again(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    for n in range(4):  # copied in order: one refused, one copied, and so on
        (source / f"{n}a.epub").write_bytes(b"refused %d" % n)
        (source / f"{n}b.epub").write_bytes(b"copied %d" % n)
    real = copies.hashed_copy

    def refused(src, dst):
        if src.stem.endswith("a") and src.stem != "a":
            raise OSError(22, "Invalid argument")
        return real(src, dst)

    monkeypatch.setattr(copies, "hashed_copy", refused)
    monkeypatch.setattr(copies.time, "sleep", lambda s: None)
    probed = []
    monkeypatch.setattr(copies, "_takes_writes", lambda r: probed.append(1) or False)
    with pytest.raises(SystemExit, match="6 copied, 4 couldn't"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert not probed


def test_add_stops_at_once_when_the_share_is_gone(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    tried = []

    def refused(src, dst):
        tried.append(src)
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(copies, "hashed_copy", refused)
    monkeypatch.setattr(copies.time, "sleep", lambda s: None)
    monkeypatch.setattr(copies, "_reachable", lambda r: False)
    with pytest.raises(SystemExit, match="carries on"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert len(set(tried)) == 1


def test_add_stops_at_once_when_the_disk_is_full(tmp_path, monkeypatch, capsys):
    """A full disk is no passing trouble: no tries again, and the run ends at the first book it refuses (it used to try
    each book six times, and stop only after three of them, if the disk refused a small write too)."""
    root, source = library_and_source(tmp_path)
    tried, slept = [], []

    def full(src, dst):
        tried.append(src)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(copies, "hashed_copy", full)
    monkeypatch.setattr(copies.time, "sleep", slept.append)
    with pytest.raises(SystemExit) as e:
        copies.add_books(root, "own", source, log=lambda s: None)
    assert e.value.code == exits.LOCAL and str(e.value) == ("0 copied, 1 couldn't be, and the library's disk is full, so the "
                                                            "other 1 wait. Running it again once it has room carries on from there.")
    assert len(tried) == 1 and slept == [] and "retrying" not in capsys.readouterr().err


def test_books_with_problems_of_their_own_dont_stop_the_rest(tmp_path):
    root, source = library_and_source(tmp_path)
    (root / "own").mkdir(parents=True)
    for n in range(copies.GIVE_UP_AFTER + 2):  # sorted before a.epub, and each has a different file in its place
        (source / f"{n}.epub").write_bytes(b"book")
        (root / "own" / f"{n}.epub").write_bytes(b"another file")
    with pytest.raises(SystemExit, match="2 copied"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert (root / "own" / "a.epub").is_file() and (root / "own" / "b.epub").is_file()


def test_add_counts_no_room_for_books_already_in_place(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    (root / "own").mkdir(parents=True)
    (root / "own" / "a.epub").write_bytes(b"book a")
    (root / "own" / "b.epub").write_bytes(b"book b")
    monkeypatch.setattr(copies.shutil, "disk_usage", lambda p: type("U", (), {"free": 1})())
    assert copies.add_books(root, "own", source, log=lambda s: None) == 2
    assert all(e.get("already_there") for e in manifest.copies(root).values())


def test_a_book_gone_since_the_folder_was_listed_is_one_failure(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    listed = copies.tree_ops

    def then_gone(*a, **k):
        ops = listed(*a, **k)
        (source / "a.epub").unlink()
        return ops

    monkeypatch.setattr(copies, "tree_ops", then_gone)
    with pytest.raises(SystemExit, match="1 copied, 1 couldn't"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert (root / "own" / "b.epub").is_file()


def test_other_trouble_while_listing_stops_the_copy_rather_than_leaving_books_out(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    real = copies.is_link

    def hiccup(path):
        if str(path).endswith("a.epub"):
            raise OSError(22, "Invalid argument")
        return real(path)

    monkeypatch.setattr(copies, "is_link", hiccup)
    with pytest.raises(SystemExit, match="Nothing was copied"):
        copies.add_books(root, "own", source, log=lambda s: None)
    assert not (root / "own" / "b.epub").exists()


def test_a_folder_that_cant_be_read_is_named_and_others_are_copied(tmp_path, monkeypatch, capsys):
    _, source = library_and_source(tmp_path)
    (source / "sub").mkdir()
    (source / "sub" / "c.epub").write_bytes(b"book c")
    (source / "sealed").mkdir()
    real = os.scandir

    def sealed(path="."):
        if str(path).endswith("sealed"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(copies.os, "scandir", sealed)
    assert sorted(op.dest for op in copies.tree_ops(source, "own")) == ["own/a.epub", "own/b.epub", "own/sub/c.epub"]
    assert "sealed" in capsys.readouterr().err  # said as it's met, on standard error with the rest on the way
    root, said, passed = tmp_path / "library", [], []
    copies.add_books(root, "own", source, dry_run=True, log=said.append, passed=passed)
    assert passed == [str(source / "sealed")]  # and with the result: its summary, and hylly add --json's passed_over
    assert any(line.startswith("Passed over 1 folder it isn't allowed to read:") and "sealed" in line for line in said)
    monkeypatch.setattr(copies.os, "scandir", lambda path=".": (_ for _ in ()).throw(OSError(22, "Invalid argument", str(path))))
    with pytest.raises(OSError):
        copies.tree_ops(source, "own")


def test_a_book_gone_while_the_folder_is_listed_is_passed_over(tmp_path, monkeypatch):
    _, source = library_and_source(tmp_path)
    real = copies.is_link

    def gone(path):
        if str(path).endswith("a.epub"):
            raise FileNotFoundError(2, "gone")
        return real(path)

    monkeypatch.setattr(copies, "is_link", gone)
    assert [op.dest for op in copies.tree_ops(source, "own")] == ["own/b.epub"]


def test_a_book_is_told_by_its_name_one_way_by_hylly_add_and_by_the_catalog(tmp_path):
    """folders.is_book: a book format's suffix, in any case, and never the ._ file macOS writes beside each book it puts
    on a share, which has the book's name; hylly add's walk of a folder and the catalog's take the same files from it."""
    from hylly import catalog, folders, rules

    books = rules.current().book_suffixes
    assert [folders.is_book(name, books) for name in ("a.epub", "A.EPUB", "._a.epub", "a.txt", "epub", ".epub")] == [
        True, True, False, False, False, False]
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    for name in ("Moonwake.epub", "Lantern Wolf.PDF", "._Moonwake.epub", "notes.txt"):
        (source / "sub" / name).write_bytes(b"made up")
    added = sorted(op.src.name for op in copies.tree_ops(source, "own"))
    assert added == sorted(f.name for f in catalog.book_files(source, books)) == ["Lantern Wolf.PDF", "Moonwake.epub"]


def test_a_colon_in_a_name_is_only_a_problem_on_windows():
    assert copies.stays_inside("own/Lanterns: A Story.epub", "own") is (os.name != "nt")
    assert not copies.stays_inside("own/../Lanterns.epub", "own")


def test_add_says_when_the_disk_is_too_small(tmp_path, monkeypatch):
    root, source = library_and_source(tmp_path)
    monkeypatch.setattr(copies.shutil, "disk_usage", lambda p: type("U", (), {"free": 1})())
    with pytest.raises(SystemExit, match="These need 12 bytes, and the library's disk has 1 byte free"):  # words.size_words
        copies.add_books(root, "own", source, log=lambda s: None)
    assert not (root / "own").exists()


def test_add_refuses_a_library_and_a_source_that_overlap(tmp_path):
    root = tmp_path / "books" / "library"
    root.mkdir(parents=True)
    with pytest.raises(SystemExit, match="overlap"):
        copies.add_books(root, "own", tmp_path / "books", log=lambda s: None)
    with pytest.raises(SystemExit, match="overlap"):
        copies.add_books(root, "own", root / "own", log=lambda s: None)


def test_add_stays_inside_its_folder(tmp_path):
    root, source = library_and_source(tmp_path)
    listed = tmp_path / "list.csv"
    listed.write_text("file,category\na.epub," + chr(92).join(["..", "..", "outside"]) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="outside"):
        copies.add_books(root, "own", source, listed, log=lambda s: None)
    listed.write_text("file,category\na.epub,Science Fiction\n", encoding="utf-8")
    copies.add_books(root, "own", source, listed, log=lambda s: None)
    assert (root / "own" / "Science Fiction" / "a.epub").exists()
    for name in ("x/../../out", "C:\\elsewhere", "..", "humble"):
        with pytest.raises(SystemExit, match="plain name"):
            cli.main(["add", name, "--library", str(root), "--from", str(source)])


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows'")
def test_add_doesnt_follow_a_junction_back_into_itself(tmp_path):
    root, source = library_and_source(tmp_path)
    (source / "sub").mkdir()
    (source / "sub" / "c.epub").write_bytes(b"book c")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(source / "sub" / "loop"), str(source)], check=True, capture_output=True)
    names = sorted(op.dest for op in copies.tree_ops(source, "own"))
    assert names == ["own/a.epub", "own/b.epub", "own/sub/c.epub"]
