"""Sizes, how long and counts are worded one way, words.py's, wherever Hylly says them; and a page's values go in one
way, words.fill, for the help, the login page and the owners' own pages alike."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hylly import words
from hylly.words import fill, gb, many, mb, size_words, time_words

SOURCES = Path(words.__file__).parent


@pytest.mark.parametrize(("n", "said"), [
    (0, "0 bytes"), (1, "1 byte"), (999, "999 bytes"), (1000, "1.0 KB"),
    (1250, "1.3 KB"), (999.5, "1.0 KB"),  # a half rounds up, as the pages' Math.round: a byte is whole
    (950_000, "950.0 KB"), (999_949, "999.9 KB"), (999_950, "1.0 MB"),  # a unit up only from 999.95 of this one
    (949_999_999, "950.0 MB"), (950_000_000, "950.0 MB"), (999_949_999, "999.9 MB"),
    (999_950_000, "1.0 GB"), (999_999_999, "1.0 GB"), (1_000_000_000, "1.0 GB"),  # never "1,000.0 MB"
    (1_500_000, "1.5 MB"), (4 * 2**30, "4.3 GB"),  # four of Windows' GB are 4.3 of a disk's
    (2_500_000_000_000, "2.5 TB"), (1_234_000_000_000_000, "1,234.0 TB"), (-1_500_000_000, "-1.5 GB")])
def test_a_size_is_said_in_thousands_as_disks_are_sold_in_the_largest_unit_it_has_one_of(n, said):
    assert size_words(n) == said


def test_a_column_of_sizes_keeps_to_one_unit_whatever_the_size():
    assert (gb(0), gb(123_456_789), gb(1_234_500_000_000)) == ("0.0 GB", "0.1 GB", "1,234.5 GB")
    assert (mb(0), mb(4_300_000), mb(2_000_000_000)) == ("0.0 MB", "4.3 MB", "2,000.0 MB")
    assert words.GB == 1_000_000_000 and words.MB == 1_000_000 and words.KB == 1000


@pytest.mark.parametrize(("seconds", "said"), [
    (0, "under a minute"), (59.9, "under a minute"), (60, "1 minute"), (89, "1 minute"), (90, "2 minutes"),
    (150, "3 minutes"),  # a half up, as sizes
    (89 * 60, "89 minutes"), (89 * 60 + 30, "89 minutes"), (89 * 60 + 59, "89 minutes"),  # minutes up to 89
    (90 * 60, "1.5 hours"), (91 * 60, "1.5 hours"), (99 * 60, "1.7 hours"), (3 * 3600, "3.0 hours")])
def test_how_long_is_said_in_minutes_up_to_an_hour_and_a_half_then_in_hours(seconds, said):
    assert time_words(seconds) == said


def test_how_long_a_job_took_is_said_in_words_a_few_seconds_too():
    """hylly jobs and a long job's note: a 2-second catalog build took "under a minute", not "1 minute"."""
    assert [time_words(s) for s in (0, 2, 59.9, 60, 89, 150, 5400)] == [
        "under a minute", "under a minute", "under a minute", "1 minute", "1 minute", "3 minutes", "1.5 hours"]


def test_an_estimate_says_about_but_not_of_under_a_minute():
    """A copy's or itch.io's time to go: "about 5 minutes to go", and "under a minute to go", never "about under"."""
    assert [time_words(s, about=True) for s in (30, 300, 2 * 3600)] == ["under a minute", "about 5 minutes", "about 2.0 hours"]


def test_a_count_has_its_word_in_the_plural_unless_it_is_one():
    assert (many(0, "file"), many(1, "file"), many(2, "file")) == ("0 files", "1 file", "2 files")
    assert (many(1, "copy", "copies"), many(2, "copy", "copies")) == ("1 copy", "2 copies")
    assert many(1204, "file") == "1,204 files"  # a comma between the thousands


def test_a_page_s_values_go_in_escaped_and_a_name_not_given_stays_as_it_is():
    page = "<h1>{{library}}</h1><p>{{user}} {{user}}</p><script>const x = '{{not_given}}';</script>"
    assert fill(page, {"library": "Moonwake & Co", "user": "<Ann Writer>"}) == (
        "<h1>Moonwake &amp; Co</h1><p>&lt;Ann Writer&gt; &lt;Ann Writer&gt;</p><script>const x = '{{not_given}}';</script>")


def test_a_mark_goes_in_as_it_is_in_the_same_pass_so_nothing_put_in_is_read_again():
    """The login page's theme goes where its mark stands, unescaped, as the server's own CSS; a name typed as the mark,
    or as another value's {{name}}, stays text: what was put in is never looked at again."""
    mark = "/* every theme, from the server */"
    page = f"<style>{mark}</style><p>{{{{name}}}}</p><p>{{{{next}}}}</p>"
    out = fill(page, {"name": mark, "next": "{{name}}"}, {mark: "body { color: #123 }"})
    assert out == "<style>body { color: #123 }</style><p>/* every theme, from the server */</p><p>{{name}}</p>"


SIZES = [0, 1, 2, 999, 999.4, 999.5, 1000, 1049, 1050, 1250, 1349, 99_949, 99_950, 950_000, 999_949, 999_950,
         949_999_999, 950_000_000, 999_949_999, 999_950_000,
         999_999_999, 1_000_000_000, 1_234_567_890, 4 * 2**30, 12 * 2**20, 2_500_000_000_000, 1_234_000_000_000_000, -1_500_000_000,
         -0.3, 4_718_592.3, 9_007_199_254_740]


@pytest.mark.parametrize("page", ["catalog_page.html", "server_page.html"])
def test_the_pages_say_a_size_exactly_as_the_commands_do(page):
    """The library page's file sizes and the Server page's (its disk cache, the files a question offers) come from the
    page's own sizeWords, the same in both pages, which says each size as words.size_words does: a half rounded up,
    the unit after rounding, the thousands with commas. Run under node, over the same sizes."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the pages' script runs only in a browser here")
    text = (SOURCES / page).read_text(encoding="utf-8")
    found = re.search(r"^const sizeWords = .*?^};$", text, re.S | re.M)[0]
    assert found == re.search(r"^const sizeWords = .*?^};$", (SOURCES / "catalog_page.html").read_text(encoding="utf-8"),
                              re.S | re.M)[0]  # one helper, written once in each page
    script = found + f"\nprocess.stdout.write(JSON.stringify({json.dumps(SIZES)}.map(sizeWords)));\n"
    said = json.loads(subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert said == [size_words(n) for n in SIZES]
