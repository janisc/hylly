"""The shop list (docs/downloads.md, "The shop list"): everything you own at a shop that a download takes or could
take, a row per file, with what a download would fetch ticked, written as a CSV that a spreadsheet or any editor opens
(hylly SHOP list, hylly list), and read back as a download's whole choice (hylly SHOP download LIST, hylly download
LIST). It is written once, for every shop: the rows are the shops' core's (shopping.select's rows), so a shop gives
nothing of its own for it.

One code path: a plain download is a download of the list the rules make, never written out; a download with a list
goes by the same select, given the list's marks (its marks). Only your changes count: every row keeps what the rules
ticked when the list was written (picked), so a row whose mark is as written goes by the rules as they are at the run
(a file held back then and answered since is fetched), and a row whose mark you changed is yours: ticked, fetched in
that run; unticked where the rules had ticked it, left out by you from then on. A row deleted is not taken. So a list
written and read back unchanged chooses exactly what the plain run chooses.

The file: UTF-8 with a byte order mark, semicolons between values (Finnish and other European Excels open it by a
double-click), the columns of HEADER, the mark last: (X) ticked, ( ) not. MB has a decimal comma, as those Excels read a
number (4.1 would be a day in January). listed is the day the list was written, on every row, as a spreadsheet keeps
no comment line. A cell of the shop's own words that a spreadsheet would take for a formula (=, +, -, @, a tab or a
carriage return first) gets a ' before it: a title is anyone's to name, at itch.io. Read back with a semicolon, a comma
or a tab between values (an Excel's sep= line first is skipped), with or without the mark, any line ending, as UTF-8
(else as Windows' own code page, or UTF-16 with its mark, as an Excel may save it): only the id, picked and download
columns count, in any place; rows in any order, and any other column moved, changed or gone. What can't be read stops
before anything is fetched (Exit, with words: never a traceback); a row that can't be taken is a problem of its own
(Given.problems), and the rest goes on."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

from .exits import USAGE, Exit
from .words import size_words

HEADER = ("shop", "bundle or publisher", "title", "kind", "format", "role", "MB", "in library", "why", "listed", "id",
          "picked", "download")
ON, OFF = "(X)", "( )"
PICKED = "yes"  # in picked: the rules ticked it as the list was written; blank, they didn't
TICKED = frozenset({"(x)", "[x]", "x", "yes", "1"})  # read in any case, its spaces aside
UNTICKED = frozenset({"()", "[]", "", "no", "0"})
OURS = frozenset({"shop", "listed", "id", "picked", "download", "MB"})  # the cells Hylly writes itself: never a formula
FORMULA = ("=", "+", "-", "@", "\t", "\r")  # what a spreadsheet takes a cell for a formula by, at its start
EVERY = "hylly-list.csv"  # hylly list's file, as it comes; a shop's is <shop>-list.csv (name)
NOTHING = "Nothing was downloaded."
# The longest list read: the owner's biggest shop's list, 7,146 rows, is about 1.5 MB; 50 MB is room for a library more
# than thirty times that, and keeps a file that is no list (a film, say) from being read whole into memory
MOST = 50 * 2**20  # 52.4 MB, as the docs say it (in thousands: words.py)
TOLERANCE = 0.051  # MB: a size shown to one decimal (as written, or as a spreadsheet saved it: 88,0 as 88) is the same


def name(shop_id: str | None) -> str:
    """The list's file as it comes, in the current folder: <shop>-list.csv, or hylly-list.csv for every shop."""
    return f"{shop_id}-list.csv" if shop_id else EVERY


def mb(size) -> str:
    """A size in MB of 2**20 bytes, as every list has had it, since a list's MB is read back (same_size), with a decimal
    comma: "" for none listed. What the commands say of a size counts in thousands (words.py)."""
    return f"{size / 2**20:.1f}".replace(".", ",") if isinstance(size, int) and size > 0 else ""


def same_size(written: str, size) -> bool:
    """Whether a row's MB, as the list has it now (a spreadsheet may have saved 88,0 as 88, or 4,1 as 4.1), is the size
    the shop lists: within the rounding it was shown with. True where either can't be told."""
    try:
        then = float(written.strip().replace(",", "."))
    except ValueError:
        return True
    return not (isinstance(size, int) and size > 0) or abs(then - size / 2**20) <= TOLERANCE


def ticked(verdict: str, out: bool) -> bool:
    """Whether the rules tick a row (shopping.Selection.rows): a file a plain run takes, done or not, and not one you
    left out."""
    return verdict in ("done", "pending", "changed") and not out


def _why(t, f, verdict: str, out: bool) -> str:
    """Why the rules chose as they did, in a few words, so that the mark can be moved: a guess between two files (File
    .unsure, a which-file question's) above all."""
    unsure = getattr(f, "unsure", ())
    guess = f"the rules' guess: {unsure[2]}" if unsure else ""
    if out:
        return "left out by you: tick it to take it again"
    if verdict == "held":
        return "held back for your answer (hylly decide)" + (f"; {guess}" if guess else "")
    if verdict == "moved":
        return "you moved it by hand (hylly decide): tick it to fetch it again"
    if verdict == "changed":
        return "changed at the shop: --update fetches the new one" + (f"; {guess}" if guess else "")
    if verdict == "behind":
        return "another of its files, behind the one chosen: tick it, and untick the chosen one, to keep this one instead"
    if guess:
        return f"{guess}: move the mark to keep another"
    if f.role != "book":
        return "extras are kept"
    return str(getattr(t, "why", "") or "")


def no_formula(cell: str) -> str:
    """A cell of the shop's own words as a spreadsheet takes it: one that would start a formula gets a ' first. Every
    CSV Hylly writes from a shop's words goes through it: this list, the failures file, itch.io's survey."""
    return f"'{cell}" if cell[:1] in FORMULA else cell


def rows(shop_id: str, sel, written: str) -> list[list[str]]:
    """The list's rows of a shop's run (shopping.select with no marks): one per file, in the run's order, with what the
    rules tick in picked and in download alike. written: the day it is written (YYYY-MM-DD), its listed."""
    from .download import kind_of
    from .shopping import kind_found, under

    out = []
    for t, f, _, verdict, mine, rid, there in sel.rows:
        ext = getattr(f, "ext", "") or (kind_of(f.name) if f.name else "")
        here = "moved by you" if verdict == "moved" else ("yes" if there else "")
        if mine:
            here = f"{here}, left out" if here else "left out"
        on = ticked(verdict, mine)
        cells = dict(zip(HEADER, [shop_id, under(shop_id, t), t.name, kind_found(shop_id, t), f.label or ext.upper(),
                                  f.role, mb(f.size), here, _why(t, f, verdict, mine), written, rid,
                                  PICKED if on else "", ON if on else OFF]))
        out.append([cell if column in OURS else no_formula(cell) for column, cell in cells.items()])
    return out


def text(found: list[list[str]]) -> str:
    """The list as text, its header first: semicolons between values, each quoted where it must be (an id with a
    semicolon or a quote in it), never cut."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADER)
    writer.writerows(found)
    return buf.getvalue()


def data(found: list[list[str]]) -> bytes:
    """The list as its file has it: UTF-8 with a byte order mark."""
    return text(found).encode("utf-8-sig")


def merged(texts: list[bytes]) -> bytes:
    """Lists of several shops as one (hylly list): each one's rows under one header."""
    out: list[list[str]] = []
    for raw in texts:
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";")
        out += [r for r in list(reader)[1:] if any(c.strip() for c in r)]
    return data(out)


def shop_of(rid: str) -> str:
    """The shop a row's id names, as Given.of matches it: the word before its first colon ("" for an id without one),
    whether or not the rest can be read (itchio:123 is itch.io's row, matching nothing there)."""
    shop, colon, _ = rid.partition(":")
    return shop if colon else ""


def parts(rid: str) -> tuple[str, str, str] | None:
    """An id's shop, title key and file key: the shop before the first colon, the file after the last; None for one that
    has no such three parts."""
    shop, _, rest = rid.partition(":")
    key, _, file = rest.rpartition(":")
    return (shop, key, file) if shop and key and file else None


@dataclass
class Given:
    """A list as read: what each row says, by its id, and what couldn't be read of it."""
    # by id: (ticked, picked): its mark now, and what the rules ticked as it was written (None where its picked can't be
    # read, or the list has none: its mark then counts for that run alone). Not those with a problem
    marks: dict[str, tuple[bool, bool | None]] = field(default_factory=dict)
    named: dict[str, dict] = field(default_factory=dict)  # by id: its row's title and MB, for the words
    problems: list[tuple[str, str, str]] = field(default_factory=list)  # (its title, its id, why), in the list's order
    rows: dict[str, int] = field(default_factory=dict)  # how many rows each shop has, by the shop its ids start with
    written: str = ""  # the day the list was written (its oldest listed), YYYY-MM-DD; "" where it can't be told
    # every row as read, (its shop, title, MB, listed, id, picked, download), in the list's order: what hylly download
    # LIST gives each shop's run of its own
    entries: list[tuple] = field(default_factory=list)
    unpicked: dict[str, int] = field(default_factory=dict)  # by shop: its rows whose picked can't be read, or is gone

    def of(self, shop_id: str) -> dict[str, tuple[bool, bool | None]]:
        """The marks of one shop's rows (shop_of)."""
        return {rid: m for rid, m in self.marks.items() if shop_of(rid) == shop_id}

    def entries_of(self, shop_id: str) -> list[list[str]]:
        """One shop's rows as the list had them, a list of their own (hylly download LIST: each shop's run its own)."""
        return [[shop, "", title, "", "", "", size, "", "", listed, rid, picked, said]
                for shop, title, size, listed, rid, picked, said in self.entries if shop == shop_id]


def line(title: str, rid: str, why: str) -> str:
    """A row that couldn't be taken, in words: "Velvet Jinx Core Rules (drivethrurpg:123456:1): not at the shop any more"."""
    return f"  {title} ({rid}): {why}" if title and rid else f"  {title or rid or 'a row'}: {why}"


def _decoded(raw: bytes, where: str, writes: str) -> str:
    if len(raw) > MOST:
        raise Exit(USAGE, f"{where} is larger than any shop list ({size_words(MOST)} at most), so it isn't one: give the "
                          f"list {writes} wrote, or a copy of it your spreadsheet saved as CSV. {NOTHING}")
    if raw[:2] == b"PK" or raw[:4] == b"\xd0\xcf\x11\xe0":  # a spreadsheet's own file (XLSX, XLS), not its CSV
        raise Exit(USAGE, f"{where} is a spreadsheet's own file, not a CSV: save it as CSV (UTF-8) and give that one. "
                          f"{NOTHING}")
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # Excel's "Unicode text"
            return raw.decode("utf-16")
    except UnicodeDecodeError:
        raise Exit(USAGE, f"{where} can't be read: it starts as UTF-16 text, and isn't that all through. Save it as CSV "
                          f"(UTF-8) and give that one. {NOTHING}") from None
    if b"\x00" in raw:
        raise Exit(USAGE, f"{where} isn't a shop list: it isn't text. {writes} writes one. {NOTHING}")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # an Excel's own code page, as one may save a CSV
        return raw.decode("cp1252", errors="replace")


def _day(given: str) -> str:
    """A day as a spreadsheet may have written it back (2026-11-16, 16.11.2026, 16/11/2026, 11/16/2026), as YYYY-MM-DD;
    "" for none. A day and month either way round goes as Europe writes them."""
    found = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", given)
    if found:
        y, m, d = (int(x) for x in found.groups())
    else:
        found = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", given)
        if not found:
            return ""
        a, b, y = (int(x) for x in found.groups())
        d, m = (b, a) if "/" in found.group(0) and b > 12 >= a else (a, b)
        y += 2000 if y < 100 else 0
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def unformula(cell: str) -> str:
    """A cell of the shop's own words as it was before its ' (no_formula), for the words a run says of its row."""
    return cell[1:] if cell[:1] == "'" and cell[1:2] in FORMULA else cell


SEPARATORS = (";", ",", "\t")
SEP_LINE = re.compile(r"\s*\"?sep=(.)\"?\s*", re.I)  # Excel's own first line, naming the separator


def read(raw: bytes, where: str, writes: str = "hylly list") -> Given:
    """A list, as read back: each row's mark and its picked by its id. Stops (Exit, before anything is fetched, with
    words) on a file that isn't a list, or has no id or download column, saying which; a row with a mark that can't be
    read, no id, or an id two rows give different marks is a problem of its own, never taken, and the rest goes on.
    writes: the command that writes such a list, for the words (hylly humble list)."""
    text_ = _decoded(raw, where, writes)
    lines = text_.splitlines()
    at_first = next((n for n, x in enumerate(lines) if x.strip()), None)
    if at_first is None:
        raise Exit(USAGE, f"{where} is empty: no shop list in it. {writes} writes one. {NOTHING}")
    told, skip = SEP_LINE.fullmatch(lines[at_first]), 0
    if told:  # sep=; first: Excel's word for its separator, no row of the list
        skip, at_first = 1, next((n for n, x in enumerate(lines) if x.strip() and n > at_first), None)
        if at_first is None:
            raise Exit(USAGE, f"{where} is empty: no shop list in it. {writes} writes one. {NOTHING}")
    first = lines[at_first]
    try:
        heads = {s: [c.strip().lower() for c in next(csv.reader([first], delimiter=s), [])] for s in SEPARATORS}
    except csv.Error as e:  # a first line longer than any list's (the csv module's own limit), say
        raise Exit(USAGE, f"{where} can't be read as a shop list ({e}). {writes} writes one. {NOTHING}") from None
    sep = told.group(1) if told and told.group(1) in heads and {"id", "download"} <= set(heads[told.group(1)]) else next(
        (s for s, h in heads.items() if "id" in h and "download" in h), None)
    if sep is None:
        named = {c for h in heads.values() for c in ("id", "download") if c in h}
        if not named:
            raise Exit(USAGE, f"{where} isn't a shop list: its first line names no id or download column. {writes} writes "
                              f"one. {NOTHING}")
        if named == {"id", "download"}:
            raise Exit(USAGE, f"{where} can't be read: its first line names the id and the download columns with different "
                              f"separators between them. Save it with one, a semicolon or a comma. {NOTHING}")
        missing = ({"id", "download"} - named).pop()
        raise Exit(USAGE, f"{where} has no {missing} column: a shop list's first line names its columns, and the {missing} "
                          f"column is one that counts ({'which file' if missing == 'id' else 'its mark'}). {NOTHING}")
    head = heads[sep]
    at = {c: head.index(c) for c in ("id", "download", "picked", "title", "mb", "listed") if c in head}
    try:
        found = [r for r in csv.reader(io.StringIO(text_, newline=""), delimiter=sep) if any(c.strip() for c in r)]
    except csv.Error as e:  # a cell longer than any list's (the csv module's own limit), say
        raise Exit(USAGE, f"{where} can't be read as a shop list ({e}). {writes} writes one. {NOTHING}") from None
    found = found[1 + skip:]  # the header's, and Excel's sep= line before it
    out, seen, days, bad = Given(), {}, [], set()
    for cells in found:
        cell = lambda c: cells[at[c]].strip() if c in at and at[c] < len(cells) else ""  # noqa: E731
        rid, said, title, then = cell("id"), cell("download"), unformula(cell("title")), cell("picked").lower()
        shop = shop_of(rid)
        out.rows[shop] = out.rows.get(shop, 0) + 1
        out.entries.append((shop, title, cell("mb"), cell("listed"), rid, cell("picked"), said))
        if cell("listed"):
            days.append(_day(cell("listed")))
        if not rid:
            out.problems.append((title, "", "no id"))
            continue
        mark = re.sub(r"\s+", "", said).lower()
        if mark in TICKED or mark in UNTICKED:
            m = mark in TICKED
        else:
            out.problems.append((title, rid, f"a mark Hylly can't read: {said!r} (use (X) or ( ))"))
            bad.add(rid)
            continue
        picked = None if "picked" not in at else True if then == PICKED else False if then == "" else None
        if picked is None:
            out.unpicked[shop] = out.unpicked.get(shop, 0) + 1
        out.named.setdefault(rid, {"title": title, "mb": cell("mb")})
        if rid in seen and seen[rid][0] != m:
            if rid not in bad:
                out.problems.append((title, rid, "two rows with this id, one ticked and one not"))
            bad.add(rid)
            continue
        seen.setdefault(rid, (m, picked))  # the same mark twice: one row, the first, as its title is (and its picked)
    out.marks = {rid: m for rid, m in seen.items() if rid not in bad}
    out.written = min((d for d in days if d), default="")
    return out
