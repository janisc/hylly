"""The shop list's file (shoplist.py, docs/downloads.md): written as a European spreadsheet opens it by a double-click,
read back however a spreadsheet or an editor saved it, and what can't be read either stopped at before anything is
fetched (with words, never a traceback) or said as a problem of its row while the rest goes on. A list's run at a shop
is in test_shops.py and test_download.py."""

from __future__ import annotations

import csv
import io

import pytest

from hylly import exits, shoplist
from hylly.exits import Exit

HEADER = "shop;bundle or publisher;title;kind;format;role;MB;in library;why;listed;id;picked;download"
ROWS = [
    ["humble", "Made-Up Book Bundle", "Lantern Wolf", "book", "EPUB", "book", "4,1", "", "first in your e-reader order",
     "2026-11-14", "humble:lanternwolf_book:Lantern Wolf.epub", "yes", "(X)"],
    ["humble", "Made-Up Book Bundle", "Lantern Wolf", "book", "PDF", "book", "12,0", "", "", "2026-11-14",
     "humble:lanternwolf_book:Lantern Wolf.pdf", "", "( )"],
    ["drivethrurpg", "Moonwake Press", "Velvet Jinx Core Rules", "rpg", "PDF", "book", "88,0", "yes", "", "2026-11-14",
     "drivethrurpg:123456:1", "yes", "(X)"],
    ["drivethrurpg", "Moonwake Press", "Velvet Jinx Core Rules", "rpg", "PDF", "extra", "2,1", "", "extras are kept",
     "2026-11-14", "drivethrurpg:123456:2", "yes", "(X)"],
]
MARKS = {"humble:lanternwolf_book:Lantern Wolf.epub": (True, True), "humble:lanternwolf_book:Lantern Wolf.pdf": (False, False),
         "drivethrurpg:123456:1": (True, True), "drivethrurpg:123456:2": (True, True)}


def as_text(rows: list[list[str]], sep: str = ";", header: str = HEADER, end: str = "\r\n") -> str:
    buf = io.StringIO()
    buf.write(header.replace(";", sep) + end)
    csv.writer(buf, delimiter=sep, lineterminator=end).writerows(rows)
    return buf.getvalue()


def stops(raw: bytes, words: str) -> Exit:
    with pytest.raises(Exit) as stopped:
        shoplist.read(raw, "my-list.csv", "hylly humble list")
    assert stopped.value.code == exits.USAGE and words in stopped.value.message, stopped.value.message
    assert stopped.value.message.startswith("my-list.csv ") and stopped.value.message.endswith("Nothing was downloaded.")
    return stopped.value


def test_a_list_is_written_as_a_european_excel_opens_it_by_a_double_click():
    """UTF-8 with a byte order mark, semicolons between values, the plan's header words with picked before the mark (what
    the rules ticked as it was written), the mark last as (X) or ( ), MB with a decimal comma (4.1 would be the 4th of
    January there), and a line ending a spreadsheet keeps."""
    raw = shoplist.data(ROWS)
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert text.split("\r\n")[0] == HEADER == ";".join(shoplist.HEADER) and text.endswith("\r\n")
    assert text.split("\r\n")[1] == ("humble;Made-Up Book Bundle;Lantern Wolf;book;EPUB;book;4,1;;first in your e-reader "
                                     "order;2026-11-14;humble:lanternwolf_book:Lantern Wolf.epub;yes;(X)")
    assert (shoplist.mb(4_300_000), shoplist.mb(0), shoplist.mb(None)) == ("4,1", "", "")
    assert shoplist.read(raw, "list.csv").marks == MARKS


def test_a_cell_of_the_shop_s_words_that_would_start_a_formula_gets_a_quote_first_and_ours_never():
    """A title is anyone's to name at itch.io: a cell a spreadsheet would take for a formula (=, +, -, @, a tab or a
    carriage return first) is written with a ' before it, but never the id, picked or download columns, which a run
    reads back as they are; the run's words name the title without it."""
    from types import SimpleNamespace

    from hylly import shopping

    def title(name: str, under: str) -> shopping.Title:
        return shopping.Title("71", name, "@Ann Writer", [shopping.File(0, "=cmd.pdf", size=10, label="+PDF")], under=under,
                              kind="book")

    odd = ['=HYPERLINK("http://example.invalid/","Velvet Jinx")', "+Velvet Jinx", "-Velvet Jinx", "\tVelvet Jinx", "Velvet Jinx"]
    sel = SimpleNamespace(rows=[(title(name, "=1+2 Made-Up Bundle"), title(name, "").files[0], "x", "pending", False,
                                 f"drivethrurpg:71:{n}", False) for n, name in enumerate(odd)])
    found = shoplist.rows("drivethrurpg", sel, "2026-11-16")
    assert [r[2] for r in found] == [f"'{name}" for name in odd[:4]] + ["Velvet Jinx"]
    assert {r[1] for r in found} == {"'=1+2 Made-Up Bundle"} and {r[4] for r in found} == {"'+PDF"}
    assert {(r[10][:15], r[11], r[12]) for r in found} == {("drivethrurpg:71", "yes", "(X)")}  # ours as they are
    given = shoplist.read(shoplist.data(found), "list.csv")
    assert given.named["drivethrurpg:71:1"]["title"] == "+Velvet Jinx" and given.marks["drivethrurpg:71:0"] == (True, True)


@pytest.mark.parametrize("bom", [True, False])
@pytest.mark.parametrize("sep", [";", ",", "\t"])
def test_either_separator_is_read_with_or_without_the_byte_order_mark(sep, bom):
    text = as_text(ROWS, sep)
    raw = text.encode("utf-8-sig" if bom else "utf-8")
    given = shoplist.read(raw, "list.csv")
    assert given.marks == MARKS and given.problems == [] and given.rows == {"humble": 2, "drivethrurpg": 2}
    assert given.written == "2026-11-14" and given.unpicked == {}


@pytest.mark.parametrize("end", ["\r\n", "\n", "\r"])
def test_any_line_ending_is_read_and_an_excel_s_sep_line_first_is_skipped(end):
    """Excel for Mac's "CSV (Macintosh)" ends its lines with a carriage return alone; an Excel may write sep=; first."""
    assert shoplist.read(as_text(ROWS, end=end).encode("utf-8"), "list.csv").marks == MARKS
    assert shoplist.read(("sep=;" + end + as_text(ROWS, end=end)).encode("utf-8"), "list.csv").marks == MARKS
    assert shoplist.read(('"sep=,"\n' + as_text(ROWS, ",")).encode("utf-8"), "list.csv").marks == MARKS


@pytest.mark.parametrize("said, ticked", [("(X)", True), ("(x)", True), ("[X]", True), ("[x]", True), ("X", True),
                                           ("x", True), ("yes", True), ("YES", True), ("1", True), (" ( X ) ", True),
                                           ("( )", False), ("()", False), ("[ ]", False), ("", False), ("no", False),
                                           ("No", False), ("0", False)])
def test_every_mark_is_read_in_any_case(said, ticked):
    given = shoplist.read(as_text([[*ROWS[0][:-1], said]]).encode("utf-8"), "list.csv")
    assert given.marks == {ROWS[0][10]: (ticked, True)} and given.problems == []


@pytest.mark.parametrize("said, picked", [("yes", True), ("YES", True), ("", False), (" ", False), ("maybe", None),
                                           ("(X)", None)])
def test_picked_says_what_the_rules_ticked_as_it_was_written_and_one_that_can_t_be_read_is_none(said, picked):
    """picked is what tells your changes from the rules': yes, or blank; anything else, and a list without the column, is
    none, whose marks then count for that run alone."""
    given = shoplist.read(as_text([[*ROWS[0][:11], said, "(X)"]]).encode("utf-8"), "list.csv")
    assert given.marks == {ROWS[0][10]: (True, picked)} and given.unpicked == ({} if picked is not None else {"humble": 1})
    gone = shoplist.read(as_text([[r[10], r[12]] for r in ROWS], header="id;download").encode("utf-8"), "list.csv")
    assert gone.marks == {rid: (m[0], None) for rid, m in MARKS.items()} and gone.unpicked == {"humble": 2, "drivethrurpg": 2}


@pytest.mark.parametrize("said", ["maybe", "(?)", "X X", "2", "ticked"])
def test_a_mark_that_can_t_be_read_is_a_problem_of_its_row_and_the_rest_goes_on(said):
    given = shoplist.read(as_text([[*ROWS[0][:-1], said], ROWS[2]]).encode("utf-8"), "list.csv")
    assert given.marks == {ROWS[2][10]: (True, True)}  # the row that can't be read is never taken, nor left out
    (title, rid, why), = given.problems
    assert (title, rid) == ("Lantern Wolf", ROWS[0][10]) and why.startswith("a mark Hylly can't read: ")
    assert shoplist.line(title, rid, why) == f"  Lantern Wolf ({ROWS[0][10]}): {why}"


def test_only_the_id_picked_and_download_columns_count_wherever_they_are_and_in_any_row_order():
    """Other columns moved, changed, dropped or added; rows in any order or blank between; a row deleted is simply not
    there."""
    rows = [[r[12], "a note of mine", r[10], r[11], r[2]] for r in reversed(ROWS)]
    text = as_text(rows, header="download;my notes;id;picked;title")
    given = shoplist.read(("\r\n\r\n" + text.replace("\r\n", "\r\n;;;\r\n", 1)).encode("utf-8"), "list.csv")
    assert given.marks == MARKS and given.problems == [] and given.written == ""  # no listed column: no day
    bare = shoplist.read(as_text([[r[10], r[11], r[12]] for r in ROWS[:2]], header="ID;Picked;Download").encode("utf-8"),
                         "list.csv")
    assert bare.marks == {k: v for k, v in MARKS.items() if k.startswith("humble:")}  # the header's words in any case
    edited = [list(r) for r in ROWS]
    edited[0][2], edited[0][6], edited[0][8] = "My own title", "", "I changed this"
    assert shoplist.read(as_text(edited).encode("utf-8"), "list.csv").marks == MARKS


def test_an_id_with_a_semicolon_or_quotes_is_quoted_where_it_must_be_never_cut_and_read_back_whole():
    odd = 'humble:moonwake_press_book:Lantern; "Wolf".epub'
    row = [*ROWS[0][:10], odd, "yes", "(X)"]
    raw = shoplist.data([row])
    assert '"humble:moonwake_press_book:Lantern; ""Wolf"".epub"' in raw.decode("utf-8-sig")
    assert shoplist.read(raw, "list.csv").marks == {odd: (True, True)}
    assert shoplist.parts(odd) == ("humble", "moonwake_press_book", 'Lantern; "Wolf".epub')
    assert shoplist.parts("humble:humble/Made-Up Bundle/Lantern Wolf:Lantern Wolf.epub") == (  # a title known by its folder
        "humble", "humble/Made-Up Bundle/Lantern Wolf", "Lantern Wolf.epub")
    assert shoplist.parts("drivethrurpg:123456:1") == ("drivethrurpg", "123456", "1")
    assert [shoplist.parts(x) for x in ("lanternwolf", "humble:", "humble:x", ":x:y", "itchio:123")] == [None] * 5


# Short names: pytest names each test in an environment variable, which Windows holds to 32,767 letters
@pytest.mark.parametrize("raw, words", [
    (b"", "is empty"),
    (b"\n\n  \n", "is empty"),
    (b"sep=;\n\n", "is empty"),
    (b"Lantern Wolf, Ann Writer\nThe Moonwake Saga, Ann Writer\n", "isn't a shop list: its first line names no id or download"),
    (b"PK\x03\x04 an XLSX, made up", "is a spreadsheet's own file, not a CSV"),
    (b"\x00\x01\x02 not text", "isn't a shop list: it isn't text. hylly humble list writes one"),
    (b"shop;title;id\nhumble;Lantern Wolf;humble:a:b\n", "has no download column"),
    (b"shop;title;download\nhumble;Lantern Wolf;(X)\n", "has no id column"),
    (b"id;title,download\nhumble:a:b;Lantern Wolf,(X)\n", "with different separators"),
    (b"\xff\xfe" + "id;download\n".encode("utf-16-le") + b"\x00", "isn't that all through"),  # an odd byte count
    (b"\xff\xfe" + "id;download\n".encode("utf-16-le") + b"\x00\xd8\x41\x00", "isn't that all through"),  # a lone surrogate
    (b"id;download\n" + b'"humble:a:' + b"b" * 140_000 + b'";(X)\n', "can't be read as a shop list"),  # past the csv limit
    (b'{"id": "' + b"x" * 300_000 + b'", "download": 1}', "can't be read as a shop list"),  # a first line past it
], ids=lambda value: repr(value)[:40])
def test_a_file_that_isn_t_a_list_or_has_no_id_or_download_column_stops_before_anything_and_says_which(raw, words):
    stops(raw, words)


def test_a_file_bigger_than_any_list_is_stopped_before_it_is_read_whole(monkeypatch):
    monkeypatch.setattr(shoplist, "MOST", 1000)
    stops(as_text(ROWS * 10).encode("utf-8"), "is larger than any shop list (1.0 KB at most), so it isn't one: give the "
                                              "list hylly humble list wrote, or a copy of it your spreadsheet saved as CSV")
    assert shoplist.read(as_text(ROWS).encode("utf-8"), "list.csv").marks == MARKS  # one within it is read


def test_two_rows_with_one_id_and_different_marks_are_a_problem_and_neither_is_taken():
    twice = [ROWS[0], [*ROWS[0][:-1], "( )"], ROWS[2], ROWS[2], ROWS[0]]  # the same mark twice is one row
    given = shoplist.read(as_text(twice).encode("utf-8"), "list.csv")
    assert given.marks == {ROWS[2][10]: (True, True)}
    assert given.problems == [("Lantern Wolf", ROWS[0][10], "two rows with this id, one ticked and one not")]


def test_a_row_without_an_id_is_a_problem_of_its_own():
    given = shoplist.read(as_text([[*ROWS[0][:10], "", "yes", "(X)"], ROWS[2]]).encode("utf-8"), "list.csv")
    assert given.marks == {ROWS[2][10]: (True, True)} and given.problems == [("Lantern Wolf", "", "no id")]


@pytest.mark.parametrize("said, day", [("2026-11-14", "2026-11-14"), ("14.11.2026", "2026-11-14"), ("14.11.26", "2026-11-14"),
                                       ("14/11/2026", "2026-11-14"), ("11/14/2026", "2026-11-14"),
                                       ("2026-11-14 10:12", "2026-11-14"), ("someday", ""), ("31.02.2026", "")])
def test_the_day_a_list_was_written_is_read_however_a_spreadsheet_wrote_it_back(said, day):
    rows = [[*r[:9], said, *r[10:]] for r in ROWS]
    assert shoplist.read(as_text(rows).encode("utf-8"), "list.csv").written == day


def test_the_oldest_day_counts_and_a_list_saved_in_windows_own_code_page_is_read_too():
    rows = [list(r) for r in ROWS]
    rows[0][9], rows[0][2], rows[0][10] = "2026-10-01", "Lantern Wölf", "humble:lanternwolf_book:Lantern Wölf.epub"
    given = shoplist.read(as_text(rows).encode("cp1252"), "list.csv")  # as an Excel may save "CSV" on Windows
    assert given.written == "2026-10-01" and given.marks["humble:lanternwolf_book:Lantern Wölf.epub"] == (True, True)
    unicode_text = as_text(ROWS, "\t").encode("utf-16")  # Excel's "Unicode text"
    assert shoplist.read(unicode_text, "list.csv").marks == MARKS


@pytest.mark.parametrize("written, size, same", [("88,0", 88 * 2**20, True), ("88", 88 * 2**20 + 30_000, True),
                                                  ("88.0", 88 * 2**20, True), ("0,0", 20_000, True), ("0", 20_000, True),
                                                  ("4,1", int(4.14 * 2**20), True), ("4,1", int(4.2 * 2**20), False),
                                                  ("5,0", 2**20, False), ("", 2**20, True), ("lots", 2**20, True),
                                                  ("5,0", 0, True)])
def test_a_row_s_size_is_the_shop_s_within_the_rounding_it_was_shown_with(written, size, same):
    """A spreadsheet saves 88,0 as 88 and 0,0 as 0: the same size, as numbers, within the shown rounding; an MB that can't
    be read, or a size the shop doesn't list, is no change."""
    assert shoplist.same_size(written, size) == same


def test_lists_of_several_shops_are_merged_under_one_header_and_each_shop_s_rows_given_as_they_were():
    merged = shoplist.merged([shoplist.data(ROWS[:2]), shoplist.data(ROWS[2:]), shoplist.data([])])
    text = merged.decode("utf-8-sig")
    assert text.count(HEADER) == 1 and shoplist.read(merged, "hylly-list.csv").marks == MARKS
    assert shoplist.name(None) == "hylly-list.csv" and shoplist.name("humble") == "humble-list.csv"
    given = shoplist.read(as_text([*ROWS, [*ROWS[0][:10], "humble:x:y", "yes", "maybe"]]).encode("utf-8"), "list.csv")
    again = shoplist.read(shoplist.data(given.entries_of("humble")), "humble's rows")
    assert again.marks == {k: v for k, v in MARKS.items() if k.startswith("humble:")} and len(again.problems) == 1
    assert again.rows == {"humble": 3} and again.written == "2026-11-14"


def test_a_row_s_shop_is_counted_as_its_marks_are_found_and_the_first_of_two_alike_rows_counts():
    """A row's shop is the word before its id's first colon, however the rest reads (itchio:12345 is itch.io's, to be
    said there as matching nothing), the same for its count and its marks; two rows of one id and one mark are one,
    the first, its picked as its title."""
    rows = [["itchio", "", "Velvet Jinx", *[""] * 7, "itchio:12345", "", "(X)"], ROWS[2], [*ROWS[2][:11], "", "(X)"]]
    given = shoplist.read(as_text(rows).encode("utf-8"), "list.csv")
    assert given.rows == {"itchio": 1, "drivethrurpg": 2} and given.of("itchio") == {"itchio:12345": (True, False)}
    assert given.marks["drivethrurpg:123456:1"] == (True, True) and given.problems == []
    assert [e[0] for e in given.entries] == ["itchio", "drivethrurpg", "drivethrurpg"]
