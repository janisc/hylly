"""When a title first came into the library, for the library page's New."""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote

import pytest

from hylly import catalog

NOW = datetime(2026, 11, 5, 20, 0, 0)
STAMP = NOW.isoformat(timespec="seconds")


def rec(title_id, title="A title", *files, **more):
    return {"id": title_id, "t": title, "f": [[f, f.rsplit(".", 1)[-1], 1] for f in files], **more}


@pytest.fixture
def library(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "catalog").mkdir()
    return tmp_path


def seed(library, *items):
    """The catalog as it was before any of this: the library's first, kept, with these titles."""
    catalog.first_seen(library, records := [dict(item) for item in items], NOW - timedelta(days=60))
    catalog.keep(library, records)


def test_after_the_first_catalog_what_it_had_is_known(library):
    seed(library, rec("h:old", "Old", "humble/old.epub"))
    records = [rec("h:old", "Old", "humble/old.epub"), rec("h:new", "New", "humble/new.epub")]
    assert catalog.first_seen(library, records, NOW) == 1
    assert "nw" not in records[0] and records[1]["nw"] == STAMP


def test_a_librarys_very_first_catalog_is_no_news(library):
    (library / "catalog" / "data.js").write_text("window.CATALOG = " + json.dumps({"items": []}) + ";\n", encoding="utf-8")
    records = [rec("h:a", "A", "a.epub"), rec("h:b", "B", "b.epub")]
    assert catalog.first_seen(library, records, NOW) == 0  # whatever the catalog folder holds: the database says what's known
    assert all("nw" not in r for r in records)


def test_it_keeps_the_day_it_came(library):
    seed(library)
    catalog.first_seen(library, [], NOW)
    assert catalog.first_seen(library, first := [rec("h:a", "A", "a.epub")], NOW) == 1
    assert first[0]["nw"] == STAMP
    later = NOW + timedelta(days=2)
    assert catalog.first_seen(library, second := [rec("h:a", "A", "a.epub"), rec("h:b", "B", "b.epub")], later) == 1
    assert second[0]["nw"] == STAMP and second[1]["nw"] == later.isoformat(timespec="seconds")


def test_after_a_month_it_no_longer_says_so(library):
    seed(library)
    catalog.first_seen(library, [], NOW)
    catalog.first_seen(library, [rec("h:a", "A", "a.epub")], NOW)
    assert catalog.first_seen(library, records := [rec("h:a", "A", "a.epub")], NOW + timedelta(days=31)) == 0
    assert "nw" not in records[0]


def test_a_title_that_goes_and_comes_back_is_no_news(library):
    seed(library, rec("h:a", "A", "a.epub"))
    catalog.first_seen(library, [], NOW)  # a share that dropped out, say
    assert catalog.first_seen(library, records := [rec("h:a", "A", "a.epub")], NOW) == 0
    assert "nw" not in records[0]


def test_a_new_copy_of_a_title_is_no_news(library):
    seed(library, rec("h:a", "A", "a.epub"))
    assert catalog.first_seen(library, records := [rec("s:a", "A", "other.epub", ids=["h:a"])], NOW) == 0
    assert "nw" not in records[0]


def test_a_folder_title_renamed_is_no_news(library):
    seed(library, rec("f:old", "A", "folder/a%20book.pdf"))
    assert catalog.first_seen(library, records := [rec("f:new", "A", "moved/there/a%20book.pdf")], NOW) == 0
    assert "nw" not in records[0]
    assert catalog.first_seen(library, more := [rec("f:other", "B", "moved/another.pdf")], NOW) == 1
    assert more[0]["nw"] == STAMP


def test_a_title_the_build_found_under_a_new_id_keeps_the_day_it_came(library):
    seed(library)
    catalog.first_seen(library, [], NOW)
    catalog.first_seen(library, [rec("f:old", "A", "folder/a.pdf")], NOW)
    later = NOW + timedelta(days=3)
    assert catalog.first_seen(library, records := [rec("f:new", "A", "folder/a.pdf")], later, moved={"f:old": "f:new"}) == 0
    assert records[0]["nw"] == STAMP  # its own day: not "known from before", as its file name alone would say


def test_a_file_name_with_its_accents_apart_is_the_same_name(library):
    composed, apart = "Pääkallo.pdf", unicodedata.normalize("NFD", "Pääkallo.pdf")
    seed(library, rec("f:old", "A", "folder/" + quote(composed)))
    assert catalog.first_seen(library, records := [rec("f:new", "A", "moved/" + quote(apart))], NOW) == 0
    assert "nw" not in records[0]
    with catalog.store(library).write() as c:  # a name kept as builds kept it before they put names in one form
        c.execute("INSERT INTO seen_file (name) VALUES (?)", (unicodedata.normalize("NFD", "Öljy.pdf").casefold(),))
    assert catalog.first_seen(library, [rec("f:third", "B", "elsewhere/" + quote("Öljy.pdf"))], NOW) == 0
