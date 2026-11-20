"""Sizes, how long, and counts, in words: one way wherever Hylly says them, in the commands' lines, their logs and
their phone notes, the feeds and the pages (whose sizeWords says a size as size_words does); and a page's values filled
into it (fill).

A size counts as disks are sold and as macOS shows them: 1 KB is 1,000 bytes, 1 MB 1,000,000, 1 GB 1,000,000,000 and
1 TB 1,000,000,000,000. (Windows' Explorer counts in 1,024s, and calls those GB too: it shows the same file about 7%
smaller.) In a sentence a size is said in the largest unit it has at least one of, to one decimal (size_words); a
column of sizes keeps to one unit for all its rows (gb, mb). A setting or an option that sets a size of files or a
disk (cache_gb, max_mb, --max-gb) counts so too, as do the limits the help names. The help says so, where it tells how
the commands are run. Memory is sold, and every system shows it, in 1,024s, so what is said or set of memory counts so;
so does a shop list's MB column, which is read back (shoplist.mb), and what is counted that no person names (the
pieces a file is read in, download.MIB).

How long: under a minute is "under a minute"; then whole minutes, up to an hour and a half ("1 minute", "89
minutes"); from 90 minutes on, hours to one decimal ("1.5 hours"). An estimate says "about" (time_words).

A count is said with its word, one or many, the number with a comma between the thousands: "1 file", "1,204 files"
(many). What came from elsewhere, to be shown on one line, is storage.shown's.

Only the standard library, and nothing of Hylly's: every part may use it, the library server and HyllyBell too."""

from __future__ import annotations

import html
import re

KB, MB, GB, TB = 1000, 1000**2, 1000**3, 1000**4
UNITS = ((KB, "KB"), (MB, "MB"), (GB, "GB"), (TB, "TB"))  # the smallest first
HOUR_FROM = 90 * 60  # seconds from which how long is said in hours


def size_words(n: float) -> str:
    """A size in bytes, in the largest unit it has at least one of, to one decimal: the smallest unit in which it
    rounds to under 1,000.0, so 950,000,000 bytes are "950.0 MB" and only 999,950,000 and up "1.0 GB", never "1,000.0
    MB"; under 1,000 bytes, in bytes. One below nothing (room that's short) has its minus. Rounded in whole numbers, a
    half up, so that the pages' sizeWords (catalog_page.html, server_page.html) says each size exactly so:
    tests/test_words.py runs both."""
    if n < 0:
        return "-" + size_words(-n)
    n = int(n + 0.5)  # a byte is whole: a half up, as the pages' Math.round has it
    if n < KB:
        return many(n, "byte")
    for unit, word in UNITS:
        tenths = (n * 10 + unit // 2) // unit
        if tenths < 10_000 or unit == TB:  # under 1,000.0 of it, or the largest there is
            return f"{tenths // 10:,}.{tenths % 10} {word}"


def gb(n: float) -> str:
    """A size in GB, to one decimal, whatever it is: for a column of sizes, or totals that are read side by side."""
    return f"{n / GB:,.1f} GB"


def mb(n: float) -> str:
    """A size in MB, to one decimal, whatever it is: for a column of sizes."""
    return f"{n / MB:,.1f} MB"


def time_words(seconds: float, about: bool = False) -> str:
    """How long, in words: "under a minute", whole minutes up to 89 below 90 of them, hours to one decimal from there,
    each rounded a half up, as sizes are (150 seconds are 3 minutes, 89 minutes and 59 seconds are 89). about: it is an
    estimate, said as one ("about 5 minutes"); "under a minute" is one already."""
    if seconds < 60:
        return "under a minute"
    if seconds >= HOUR_FROM:
        tenths = int(seconds / 360 + 0.5)
        said = f"{tenths // 10:,}.{tenths % 10} hours"
    else:
        said = many(min(int(seconds / 60 + 0.5), HOUR_FROM // 60 - 1), "minute")
    return f"about {said}" if about else said


def many(n: int, one: str, many: str = "") -> str:
    """A count and its word, in the plural unless it is one: many(1, "file") is "1 file", many(2, "copy", "copies")
    "2 copies". The plural is one + "s" unless given."""
    return f"{n:,} {one if n == 1 else many or one + 's'}"


NAMED = re.compile(r"\{\{(\w+)\}\}")  # a value's place in a page: {{name}}


def fill(page: str, values: dict[str, str], marks: dict[str, str] | None = None) -> str:
    """A page with its values put in, each where {{name}} stands, escaped for HTML; a name not among them stays as it
    is. marks: text put in as it is, where its mark stands (a login page's theme, in a comment), in the same pass, so
    that nothing put in, by a value or a mark, is read again: a value that holds a mark, or {{name}}, stays as written."""
    found = NAMED if not marks else re.compile("|".join([NAMED.pattern, *map(re.escape, marks)]))

    def put(m: re.Match) -> str:
        if m[1] is None:
            return marks[m[0]]
        return html.escape(values[m[1]]) if m[1] in values else m[0]

    return found.sub(put, page)
