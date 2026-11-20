"""What's kept about a person: the first thing here that can't be made again from the books, so what matters most is what
happens when things go wrong. It's in the database (db.py)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath

import httpx
import pytest

from hylly import access, catalog, cli, db, dbcopy, exits, matching, people, serve, shelves

from helpers import EVERY_ROLE, HOME, TITLE, again, build_library, changes, logged_in, made_before_the_library_was_kept
from helpers import newer_database, outbox_in_node, rows


OTHER = {"id": "s:7", "t": "Another", "f": "storybundle/b/another.epub"}


def every_place(person, title_id="h:novel"):
    """The places reached in a title that the database keeps, in the order they came: the latest HISTORY_KEPT."""
    return [r for r in person.export()["places"] if r["id"] == title_id]


# ---- places, and how they're kept --------------------------------------------------------------

def test_a_place_is_kept_and_comes_back(person):
    person.place(TITLE, 12, 0.25, "humble/b/novel.epub", "iPad")
    person.place(OTHER, "epubcfi(/6/4!/4/2)", 0.5)
    assert person.place(TITLE, 40, 0.8)["at"] == 40
    found = again().latest()
    assert found["h:novel"]["at"] == 40 and found["h:novel"]["pct"] == 0.8 and found["s:7"]["at"].startswith("epubcfi")
    assert found["h:novel"]["t"] == "The Novel" and found["h:novel"]["when"]
    assert [r["at"] for r in every_place(again())] == [12, 40]  # the way back is there too
    with db.database().read() as c:
        me = c.execute("SELECT * FROM person WHERE name = 'rory'").fetchone()
    assert me["since"] and me["removed"] is None


def test_the_same_place_on_the_same_device_isnt_written_twice(person):
    person.place(TITLE, 12, 0.25, device="iPad")
    person.place(TITLE, 12, 0.25, device="iPad")
    assert len(every_place(person)) == 1
    person.place(TITLE, 12, 0.25, device="iPhone")  # another device there is news
    assert len(every_place(person)) == 2


def test_a_place_sent_late_doesnt_beat_a_newer_one(person):
    person.place(TITLE, 40, 0.8, device="iPad")
    person.place(TITLE, 12, 0.25, device="iPhone", age=3600)  # an hour old: the phone was offline
    assert person.latest()["h:novel"]["at"] == 40
    assert person.latest()["h:novel"]["device"] == "iPad"
    assert [r["at"] for r in every_place(person)] == [40, 12]  # it's kept, all the same
    person.place(TITLE, 13, 0.26, device="iPhone")  # and reading on there is the latest
    assert person.latest()["h:novel"]["at"] == 13
    assert again().latest()["h:novel"]["at"] == 13


def test_a_comics_pages_and_a_books_chapter_are_kept_with_a_place(person):
    person.place(TITLE, 12, 0.25, of=48)
    person.place(OTHER, "epubcfi(/6/4)", 0.5, note="Chapter 3")
    found = again().latest()
    assert found["h:novel"]["of"] == 48 and "note" not in found["h:novel"]
    assert found["s:7"]["note"] == "Chapter 3" and "of" not in found["s:7"]


def test_finished_comes_back_and_stays_through_more_reading(person):
    person.place(TITLE, 12, 0.25)
    person.mark(TITLE, True)
    assert again().latest()["h:novel"]["done"] and again().latest()["h:novel"]["finished"]
    person.place(TITLE, 13, 0.26)
    assert again().latest()["h:novel"]["done"]
    person.mark(TITLE, False)
    assert "done" not in again().latest()["h:novel"]


def test_a_title_can_be_finished_with_no_place_in_it(person):
    person.mark(TITLE, True)
    found = again().latest()["h:novel"]
    assert found["done"] and found["at"] is None and found["t"] == "The Novel"
    person.mark(TITLE, False)
    assert again().latest() == {}


def test_a_mark_that_is_no_news_isnt_written(person):
    person.mark(TITLE, False)
    assert rows("finished") == 0  # not finished, as it wasn't
    person.mark(TITLE, True)
    person.mark(TITLE, True)
    assert rows("finished") == 1


def test_a_change_of_the_mark_is_news_to_other_devices(person):
    person.place(TITLE, 12, 0.25)
    seen = person.latest()["h:novel"]["time"]
    person.mark(TITLE, True, age=-1)  # never into the future
    person.mark(TITLE, False)
    assert person.latest()["h:novel"]["time"] >= seen


def test_starting_over(person):
    person.place(TITLE, 40, 0.8)
    person.mark(TITLE, True)
    person.start_over(TITLE, "iPad")
    found = again().latest()["h:novel"]
    assert found["at"] == 0 and found["pct"] == 0 and "done" not in found
    assert [r["at"] for r in every_place(again())] == [40, 0]  # where it was stays


def test_the_latest_places_and_marks_of_a_title_are_kept_and_memory_keeps_the_latest(person):
    """Every place reached once stayed for ever, so ordinary reading, or a script, grew the database and its nightly
    copies without end. Now each title keeps its latest HISTORY_KEPT places and changes of its mark, the older ones
    going in the change that adds one: the latest place and the way back from it are always among them."""
    for done in (True, False) * 12 + (True,):
        person.mark(TITLE, done)
    for n in range(people.HISTORY_KEPT + 5):
        person.place(TITLE, n, n / 100)
    person.place(OTHER, 3, 0.03)
    people.Person("lucy").place(TITLE, 7, 0.07)
    fresh = again()
    assert fresh.latest()["h:novel"]["done"] and fresh.latest()["h:novel"]["at"] == people.HISTORY_KEPT + 4
    assert [r["at"] for r in every_place(fresh)] == list(range(5, people.HISTORY_KEPT + 5))  # the oldest five went
    assert len([r for r in fresh.export()["finished"] if r["id"] == "h:novel"]) == people.HISTORY_KEPT
    assert fresh.export()["finished"][-1]["done"] and len(fresh._places["h:novel"]) == people.PLACES_KEPT
    assert every_place(fresh, "s:7")[0]["at"] == 3 and people.Person("lucy").latest()["h:novel"]["at"] == 7  # each title's, each person's
    assert rows("place") == people.HISTORY_KEPT + 2 and rows("finished") == people.HISTORY_KEPT


def test_one_person_s_changes_have_a_limit_a_minute_on_all_their_devices_together():
    """A script with a login could keep the database busy for ever; a person reading on two devices at once sends a
    few changes a minute. Past the limit the next waits until the oldest counted is a minute old; each person has
    their own."""
    now = [1000.0]
    pace = people.Pace(3, clock=lambda: now[0])
    assert [pace.wait("rory") for _ in range(3)] == [0, 0, 0]
    assert pace.wait("rory") == 60 and pace.wait("lucy") == 0  # one person's, not everyone's
    now[0] += 20
    assert pace.wait("rory") == 40  # a refused one isn't counted: the wait doesn't grow by asking
    now[0] += 40
    assert pace.wait("rory") == 0 and pace.wait("rory") == 0 and pace.wait("rory") == 0 and pace.wait("rory") > 0
    assert people.WRITES_PER_MINUTE >= 60  # a place a few seconds after a page settles, on two devices, and lists and bookmarks besides


def test_the_help_says_how_much_is_kept_and_how_many_changes_a_minute_go():
    text = (Path(cli.__file__).parent / "help.html").read_text(encoding="utf-8")
    assert (f"The latest {people.HISTORY_KEPT} places you reached in each title stay, and the latest {people.HISTORY_KEPT} "
            "times you marked it finished or not") in text
    assert f"up to {people.WRITES_PER_MINUTE} changes a minute from each person" in text and "Every place you reach stays" not in text


def test_a_mark_follows_a_title_whose_id_changed(person):
    person.mark({"id": "f:old", "t": "The Novel", "f": "folder/novel.epub"}, True)
    titles = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["folder/renamed/novel.epub", "epub", 1]]}])
    assert person.latest(titles)["f:new"]["done"] and "f:old" not in person.latest(titles)


def test_how_far_someone_has_seen_whats_new(person):
    assert person.new_seen() == ""
    person.saw_new("2026-11-05T20:00:00")
    assert again().new_seen() == "2026-11-05T20:00:00"
    person.saw_new("2026-11-01T20:00:00")  # it only ever moves on: a device that was offline can't undo it
    assert again().new_seen() == "2026-11-05T20:00:00"
    before = changes()
    person.saw_new("2026-11-05T20:00:00")
    assert changes() == before  # no news: nothing written


def test_which_of_the_readers_guides_someone_has_had(person):
    assert person.guides() == []
    person.saw_guide("pages")
    assert again().guides() == ["pages"]
    before = changes()
    person.saw_guide("pages")
    assert changes() == before and rows("guide_seen") == 1  # no news: nothing written
    person.saw_guide("book")
    assert again().guides() == ["book", "pages"]
    assert again().export()["guides"] == ["book", "pages"]


def test_who_has_read_in_which_title_is_each_person_and_title_once_with_the_words_of_its_latest_place(person):
    """people.readers, asked by each reading app's start page for Most read: each person and title once, the title's
    words from its latest place, whatever older ones named it (a file renamed since, a title corrected), and nobody whose
    account was removed. Asked of the place table's index, by person and title, never by sorting every place there."""
    for n, (title, file) in enumerate((("The Novel", "humble/b/novel (old).epub"), ("The Novel, corrected", "humble/b/novel.epub"))):
        person.place({**TITLE, "t": title, "f": file}, n, n / 100)
    person.place(OTHER, 3, 0.03)
    people.Person("lucy").place(TITLE, 7, 0.07)
    access.add_user("bea", "correct horse", role="reader")
    people.Person("bea").place(TITLE, 1, 0.01)
    access.remove_user("bea")
    assert sorted((r["name"], r["id"], r["t"], r["f"]) for r in people.readers()) == [
        ("lucy", "h:novel", "The Novel", "humble/b/novel.epub"), ("rory", "h:novel", "The Novel, corrected", "humble/b/novel.epub"),
        ("rory", "s:7", "Another", "storybundle/b/another.epub")]
    with db.database().read() as c:
        plan = [row[3] for row in c.execute("EXPLAIN QUERY PLAN " + people.READERS)]
    assert "SCAN place USING COVERING INDEX place_by_title" in plan and not [step for step in plan if "TEMP B-TREE" in step], plan


def test_forgetting_someones_places_forgets_the_guides_had_too(person):
    person.place(TITLE, 3, .3)
    person.saw_guide("book")
    person.forget_places()
    assert person.guides() == [] and again().guides() == []


def on_list(person):
    return [e["id"] for e in person.lists().get("reading", [])]


A, B, C = ({"id": i, "t": i.upper(), "f": f"{i}.epub"} for i in "abc")


def test_the_reading_list_keeps_its_order(person):
    for title in (A, B, C):
        assert person.change_list("reading", "add", title)
    assert on_list(person) == ["a", "b", "c"]  # new ones at the bottom
    assert person.change_list("reading", "move", C, "a")
    assert on_list(person) == ["c", "a", "b"]
    assert person.change_list("reading", "move", C)  # before nothing: last
    assert on_list(person) == ["a", "b", "c"] == on_list(again())
    assert person.change_list("reading", "remove", B)
    assert on_list(again()) == ["a", "c"]
    assert again().lists()["reading"][0]["t"] == "A" and again().lists()["reading"][0]["added"]


def test_a_change_that_is_no_change_isnt_written(person):
    person.change_list("reading", "add", A)
    before = changes()
    assert not person.change_list("reading", "add", A)  # on it already
    assert not person.change_list("reading", "remove", B) and not person.change_list("reading", "move", B, "a")  # not on it
    assert changes() == before


def test_two_devices_cant_overwrite_each_others_changes(person):
    other = again()
    person.change_list("reading", "add", A)
    other.change_list("reading", "add", B)
    person.change_list("reading", "add", C)
    other.change_list("reading", "move", C, "a")
    assert on_list(again()) == ["c", "a", "b"] == on_list(person) == on_list(other)


def test_a_full_list_says_so(person, monkeypatch):
    monkeypatch.setattr(people, "MAX_ON_A_LIST", 2)
    person.change_list("reading", "add", A)
    person.change_list("reading", "add", B)
    with pytest.raises(people.ListFull):
        person.change_list("reading", "add", C)
    assert on_list(person) == ["a", "b"]


def test_a_title_on_the_list_follows_a_new_id_and_stays_when_gone(person):
    person.change_list("reading", "add", {"id": "f:old", "t": "The Novel", "f": "folder/novel.epub"})
    person.change_list("reading", "add", {"id": "f:lost", "t": "Sold On", "f": "folder/sold.epub"})
    titles = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["folder/renamed/novel.epub", "epub", 1]]}])
    rows = person.lists(titles)["reading"]
    assert [r["id"] for r in rows] == ["f:new", "f:lost"] and "gone" not in rows[0] and rows[1]["gone"]
    # asked about by its new id, it's found: taken off, and moved
    assert person.change_list("reading", "move", {"id": "f:new"}, None, ["f:new"], titles)
    assert [r["id"] for r in person.lists(titles)["reading"]] == ["f:lost", "f:new"]
    assert person.change_list("reading", "remove", {"id": "f:new"}, None, ["f:new"], titles)
    assert [r["id"] for r in person.lists(titles)["reading"]] == ["f:lost"]
    assert person.change_list("reading", "remove", {"id": "f:lost"}, None, ["f:lost"], titles)  # one gone can go


# ---- lists of their own: the same rules as the reading list ---------------------------------------

def own(person, list_id="l1"):
    return [e["id"] for e in person.lists().get(list_id, [])]


def test_their_own_lists_are_made_filled_ordered_and_renamed(person):
    assert person.name_list("l1", "Tabletop night")
    assert person.name_list("l2", "Winter")
    for title in (A, B, C):
        assert person.change_list("l1", "add", title)
    assert person.change_list("l2", "add", A)  # a title can be on many
    assert person.change_list("l1", "move", C, "a")
    assert person.change_list("l1", "remove", B)
    assert person.name_list("l1", "Game night")
    assert again().list_names() == [{"id": "l1", "name": "Game night"}, {"id": "l2", "name": "Winter"}]
    assert own(again()) == ["c", "a"] and own(again(), "l2") == ["a"] and "reading" not in again().lists()
    assert person.change_list("reading", "add", B) and own(again()) == ["c", "a"]  # the reading list is apart


def test_a_list_they_dont_have_says_so(person):
    with pytest.raises(people.NoSuchList):
        person.change_list("l1", "add", A)
    with pytest.raises(people.NoSuchList):
        person.name_list("reading", "Another name")
    assert rows("list") == 0


def test_a_list_named_twice_is_written_once(person):
    person.name_list("l1", "Tabletop night")
    before = changes()
    assert not person.name_list("l1", "Tabletop night") and not person.delete_list("no such") and not person.restore_list("no such")
    assert changes() == before


def test_a_deleted_list_comes_back_whole(person):
    person.name_list("l1", "Tabletop night")
    person.change_list("l1", "add", A)
    person.change_list("l1", "add", B)
    person.name_list("l2", "Winter")
    assert person.delete_list("l1")
    assert again().list_names() == [{"id": "l2", "name": "Winter"}] and "l1" not in again().lists()
    with pytest.raises(people.NoSuchList):
        person.change_list("l1", "add", C)  # another device, that hadn't heard
    with pytest.raises(people.NoSuchList):
        person.name_list("l1", "Renamed meanwhile")
    assert person.restore_list("l1")
    assert own(again()) == ["a", "b"] and {"id": "l1", "name": "Tabletop night"} in again().list_names()
    assert again().list_names()[-1] == {"id": "l1", "name": "Tabletop night"}  # brought back, it's the newest
    person.delete_list("l1")
    fresh = again()
    assert fresh.list_names() == [{"id": "l2", "name": "Winter"}] and fresh.restore_list("l1") and own(again()) == ["a", "b"]


def test_a_deleted_list_goes_for_good_a_day_later_and_the_export_has_it_until_then(person):
    """A deleted list is kept for its Undo, and the export has it meanwhile; a day after the delete reached the
    server, their next change takes it, with its titles, so no list someone deleted stays in the database."""
    person.name_list("l1", "Tabletop night")
    person.change_list("l1", "add", A)
    person.change_list("l1", "add", B)
    person.show_list("l1", True)
    person.delete_list("l1")
    person.place(TITLE, 12, 0.25)  # a change soon after: the Undo still works
    deleted = again().export()["deleted_lists"]
    assert [(d["id"], d["name"], [e["id"] for e in d["titles"]], d.get("apps")) for d in deleted] == [("l1", "Tabletop night", ["a", "b"], True)]
    assert person.restore_list("l1") and own(again()) == ["a", "b"] and again().export()["deleted_lists"] == []
    person.delete_list("l1")
    with db.database().write() as c:  # as if deleted a day and a minute ago
        c.execute("UPDATE list SET deleted = ? WHERE id = 'l1'",
                  ((datetime.now() - timedelta(days=1, minutes=1)).astimezone().isoformat(timespec="seconds"),))
    assert rows("list_entry") == 2 and again().export()["deleted_lists"]  # still there, and in the export, until a change
    person.place(TITLE, 13, 0.26)
    assert rows("list") == 0 and rows("list_entry") == 0 and again().export()["deleted_lists"] == []
    assert not person.restore_list("l1") and not again().restore_list("l1") and again().list_names() == []


def test_too_many_lists_say_so(person, monkeypatch):
    monkeypatch.setattr(people, "MAX_LISTS", 1)
    person.name_list("l1", "One")
    with pytest.raises(people.ListsFull):
        person.name_list("l2", "Two")
    assert person.name_list("l1", "Still one")  # a new name isn't another list
    person.delete_list("l1")
    assert person.name_list("l2", "Two")


def test_their_own_lists_follow_a_new_id_stay_when_gone_and_are_exported(person):
    person.name_list("l1", "Tabletop night")
    person.change_list("l1", "add", {"id": "f:old", "t": "The Novel", "f": "folder/novel.epub"})
    person.change_list("l1", "add", {"id": "f:lost", "t": "Sold On", "f": "folder/sold.epub"})
    titles = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["folder/renamed/novel.epub", "epub", 1]]}])
    rows = person.lists(titles)["l1"]
    assert [r["id"] for r in rows] == ["f:new", "f:lost"] and rows[1]["gone"]
    assert person.export()["list_names"] == {"l1": "Tabletop night"} and len(person.export()["lists"]["l1"]) == 2


def test_forgetting_places_keeps_the_list_and_exports_it(person):
    person.place(TITLE, 12, 0.25)
    person.change_list("reading", "add", A)
    assert person.export()["lists"]["reading"][0]["id"] == "a"
    person.forget_places()
    assert on_list(again()) == ["a"]


# ---- bookmarks ----------------------------------------------------------------------------------

SPOT = {"at": 12, "pct": 0.25, "file": "", "of": 48}


def marks(person):
    return [(b["key"], b["why"]) for b in person.bookmarks()]


def test_bookmarks_are_kept_changed_and_removed(person):
    assert person.add_bookmark(TITLE, "k1", "useful", SPOT)
    assert person.add_bookmark(OTHER, "k2", "funny", {**SPOT, "at": "epubcfi(/6/4!/4/2)", "text": "Some words"})
    assert person.change_bookmark("k1", "clever")
    assert marks(again()) == [("k1", "clever"), ("k2", "funny")]
    kept = again().bookmarks()[1]
    assert kept["t"] == "Another" and kept["at"].startswith("epubcfi") and kept["text"] == "Some words" and kept["when"] and kept["time"]
    assert person.remove_bookmark("k1")
    assert marks(again()) == [("k2", "funny")]


def test_a_bookmark_sent_again_counts_once(person):
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    before = changes()
    assert not person.add_bookmark(TITLE, "k1", "funny", SPOT)  # a device that didn't hear the answer
    assert not person.change_bookmark("k1", "useful")  # has that reason
    assert not person.change_bookmark("no such", "funny") and not person.remove_bookmark("no such")
    assert changes() == before and marks(person) == [("k1", "useful")]


def test_a_removed_bookmark_can_come_back_by_undo(person):
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    person.remove_bookmark("k1")
    assert person.add_bookmark(TITLE, "k1", "useful", SPOT)
    assert marks(again()) == [("k1", "useful")]


def test_two_devices_bookmarks_both_stay(person):
    other = again()
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    other.add_bookmark(TITLE, "k2", "funny", SPOT)  # the same page, marked on another device
    person.change_bookmark("k2", "clever")
    assert marks(again()) == [("k1", "useful"), ("k2", "clever")]


def test_a_late_bookmark_is_dated_back(person):
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    person.add_bookmark(TITLE, "k2", "useful", SPOT, age=3600)
    found = {b["key"]: b for b in person.bookmarks()}
    assert found["k1"]["time"] - found["k2"]["time"] > 3500


def test_full_bookmarks_say_so(person, monkeypatch):
    monkeypatch.setattr(people, "MAX_BOOKMARKS", 2)
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    person.add_bookmark(TITLE, "k2", "useful", SPOT)
    with pytest.raises(people.BookmarksFull):
        person.add_bookmark(TITLE, "k3", "useful", SPOT)
    assert len(marks(person)) == 2
    assert person.remove_bookmark("k1") and person.add_bookmark(TITLE, "k3", "useful", SPOT)


def test_a_bookmark_follows_a_new_id_and_stays_when_gone(person):
    person.add_bookmark({"id": "f:old", "t": "The Novel", "f": "folder/novel.epub"}, "k1", "useful", SPOT)
    person.add_bookmark({"id": "f:lost", "t": "Sold On", "f": "folder/sold.epub"}, "k2", "funny", SPOT)
    titles = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["folder/renamed/novel.epub", "epub", 1]]}])
    rows = person.bookmarks(titles)
    assert [r["id"] for r in rows] == ["f:new", "f:lost"] and "gone" not in rows[0] and rows[1]["gone"]
    assert person.remove_bookmark("k2") and [r["key"] for r in person.bookmarks(titles)] == ["k1"]  # one gone can go


def test_forgetting_places_keeps_the_bookmarks_and_exports_them(person):
    person.place(TITLE, 12, 0.25)
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    assert person.export()["bookmarks"][0]["key"] == "k1"
    person.forget_places()
    assert marks(again()) == [("k1", "useful")]
    assert people.everyone()[0]["bookmarks"] == 1


def test_the_page_offers_the_servers_reasons_and_says_its_limit():
    from pathlib import Path
    import re

    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    offered = re.search(r"const REASONS = (\[.*\]);", page)[1]
    assert re.findall(r'\["(\w+)", "', offered) == list(people.BOOKMARK_REASONS)
    assert f"BOOKMARKS_MOST = {people.MAX_BOOKMARKS}" in page


# ---- their own shelves ----------------------------------------------------------------------------

SHELF = {"id": "m1", "name": "No games", "show": {}, "hide": {"src": ["itch"]}}


def test_shelves_are_kept_changed_and_deleted(person):
    assert person.change_shelf("set", SHELF)
    assert person.change_shelf("set", {**SHELF, "id": "m2", "name": "Comics", "show": {"k": ["comic"]}, "hide": {}})
    assert person.change_shelf("set", {**SHELF, "name": "Nothing odd"})
    assert person.change_shelf("open", shelf_id="m2")
    mine, opens = again().shelves()
    assert [s["name"] for s in mine] == ["Nothing odd", "Comics"] and opens == "m2"  # the order made, and the name changed in place
    assert person.change_shelf("delete", shelf_id="m1")
    assert [s["id"] for s in again().shelves()[0]] == ["m2"]
    assert person.change_shelf("open", shelf_id="") and again().shelves()[1] == ""


def test_a_shelf_sent_again_changes_nothing(person):
    person.change_shelf("set", SHELF)
    person.change_shelf("open", shelf_id="m1")
    before = changes()
    assert not person.change_shelf("set", dict(SHELF)) and not person.change_shelf("open", shelf_id="m1") and not person.change_shelf("delete", shelf_id="no such")
    assert changes() == before


def test_full_shelves_say_so(person, monkeypatch):
    monkeypatch.setattr(shelves, "MAX_SHELVES", 1)
    person.change_shelf("set", SHELF)
    with pytest.raises(shelves.Full):
        person.change_shelf("set", {**SHELF, "id": "m2"})
    assert person.change_shelf("set", {**SHELF, "name": "A new name"})  # one already there can still change


def test_forgetting_places_keeps_shelves_and_exports_them(person):
    person.place(TITLE, 12, 0.25)
    person.change_shelf("set", SHELF)
    person.change_shelf("open", shelf_id="m1")
    found = person.export()
    assert found["shelves"][0]["id"] == "m1" and found["opens_with"] == "m1"
    person.forget_places()
    assert again().shelves()[1] == "m1"


# ---- a newer database, and two at once ----------------------------------------------------------

def test_a_newer_database_is_left_alone(person, told):
    person.place(TITLE, 12, 0.25)
    person.change_list("reading", "add", A)
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    person.change_shelf("set", SHELF)
    newer_database()
    before = db.file().read_bytes()
    fresh = again(told)
    assert fresh.latest() == {} and fresh.lists() == {} and fresh.export()["places"] == [] and fresh.newer
    assert len(told) == 1 and "newer version" in told[0][1]
    for change in (lambda p: p.place(TITLE, 13, 0.3), lambda p: p.mark(TITLE, True), lambda p: p.change_list("reading", "add", B),
                   lambda p: p.name_list("l2", "Two"), lambda p: p.delete_list("l1"), lambda p: p.add_bookmark(TITLE, "k2", "useful", SPOT),
                   lambda p: p.remove_bookmark("k1"), lambda p: p.change_shelf("delete", shelf_id="m1"), lambda p: p.rate(TITLE, 3),
                   lambda p: p.saw_guide("book"), lambda p: p.pick_look("x"), lambda p: p.forget_places()):
        with pytest.raises(people.Newer):
            change(fresh)
    assert len(told) == 1  # once for each
    with pytest.raises(people.Newer):
        person.place(TITLE, 13, 0.3)  # one that has them in memory finds out too
    assert len(told) == 2 and db.file().read_bytes() == before
    assert people.People().everyones_reviews({"rory"}) == {} and people.People().everyones_reports({"rory"}) == {}
    with pytest.raises(SystemExit):
        cli.main(["people", "list"])


def test_two_at_once_lose_nothing(person):
    """The server and a command, say. Each change holds the lock, and each sees what the other wrote."""
    other = again()

    def write(who, start):
        for n in range(start, start + 25):
            who.place({**TITLE, "id": f"t{n}"}, n, 0.5)

    threads = [threading.Thread(target=write, args=(who, start)) for who, start in ((person, 0), (other, 100))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(again().latest()) == 50 and len(person.latest()) == 50 and len(other.latest()) == 50


def test_a_change_from_a_command_is_seen_by_the_server(person):
    person.place(TITLE, 12, 0.25)
    assert person.latest()
    again().forget_places()
    assert person.latest() == {}
    assert access.add_user("rory", "correct horse")  # a new user of that name: what was kept before isn't theirs
    assert person.latest() == {}
    person.place(TITLE, 1, 0.01)  # a new one by that name starts afresh
    assert list(person.latest()) == ["h:novel"]


# ---- titles whose id changed ---------------------------------------------------------------------

def test_titles_are_found_again_by_the_same_rules_as_tags():
    records = [{"id": "f:new", "t": "Renamed", "f": [["folder/novel.epub", "epub", 1]]},
               {"id": "f:two", "t": "Twin", "f": [["a/same.pdf", "pdf", 1]]}, {"id": "f:three", "t": "Twin", "f": [["b/same.pdf", "pdf", 1]]}]
    titles = matching.Titles(records)
    assert titles.find({"t": "Old name", "f": "elsewhere/NOVEL.epub"}) == "f:new"  # the file name first
    assert titles.find({"t": "renamed", "f": "gone.epub"}) == "f:new"  # then the title
    assert titles.find({"t": "Twin", "f": "gone.pdf"}) is None and titles.find({"t": "x", "f": "same.pdf"}) is None  # two is no answer
    assert titles.find({}) is None and "f:new" in titles and "f:old" not in titles


def test_a_place_follows_a_title_whose_id_changed(person):
    person.place({"id": "f:old", "t": "The Novel", "f": "folder/novel.epub"}, 12, 0.25)
    person.place({"id": "f:lost", "t": "Sold On", "f": "folder/sold.epub"}, 3, 0.1)
    titles = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["folder/renamed/novel.epub", "epub", 1]]}])
    found = person.latest(titles)
    assert found["f:new"]["at"] == 12 and "gone" not in found["f:new"] and "f:old" not in found
    assert found["f:lost"]["gone"] and found["f:lost"]["t"] == "Sold On"  # kept, and shown as not in the library now
    person.place({"id": "f:new", "t": "The Novel", "f": "folder/renamed/novel.epub"}, 30, 0.6)
    assert person.latest(titles)["f:new"]["at"] == 30  # the latest of both


def test_every_reader_of_what_s_kept_follows_a_title_whose_id_changed_alike_and_marks_one_gone_alike(person):
    """One way for them all (people._under_its_id): a list's entry, a bookmark, a place, a finished mark, a review and a
    report of a title whose id changed go by its new id, and those of a title gone are marked so, every one alike."""
    moved = {"id": "f:old", "t": "The Novel", "f": "folder/novel.epub"}
    lost = {"id": "f:lost", "t": "Sold On", "f": "folder/sold.epub"}
    for title in (moved, lost):
        person.change_list("reading", "add", title)
        person.add_bookmark(title, f"k-{title['id']}", "useful", SPOT)
        person.rate(title, 4)
        person.report(title, ["wrong"], "")
    person.place(moved, 12, 0.25)
    person.mark(lost, True)  # finished, with no place in it
    titles = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["folder/renamed/novel.epub", "epub", 1]]}])
    readers = {"lists": lambda t: person.lists(t)["reading"], "bookmarks": person.bookmarks, "reviews": person.reviews,
               "reports": person.reports, "latest": lambda t: list(person.latest(t).values())}
    for what, read in readers.items():
        assert sorted((r["id"], r.get("gone", False)) for r in read(titles)) == [("f:lost", True), ("f:new", False)], what
        assert sorted((r["id"], r.get("gone", False)) for r in read(None)) == [("f:lost", False), ("f:old", False)], what
    renamed = {"id": "f:new", "t": "The Novel", "f": "folder/renamed/novel.epub"}
    assert person.rate(renamed, 5, ["f:new"], titles) and person.unreport(["f:new"], titles)  # found by the new id
    assert [(r["id"], r["stars"]) for r in person.reviews(titles) if r["id"] == "f:new"] == [("f:new", 5)]
    assert [r["id"] for r in person.reports(titles)] == ["f:lost"]


# ---- the commands --------------------------------------------------------------------------------

def test_export_and_forget(person):
    person.place(TITLE, 12, 0.25)
    person.place(TITLE, 13, 0.26)
    person.mark(TITLE, True)
    data = person.export()
    assert data["name"] == "rory" and [r["at"] for r in data["places"]] == [12, 13] and data["finished"][0]["done"]
    assert person.forget_places() == 1 and person.latest() == {} and rows("place") == 0 and again().export()["finished"] == []
    assert person.kept()  # who they are stays: only the places were asked to go


def test_the_export_has_all_that_s_kept_and_the_logins_without_their_digests(person, capsys):
    """hylly people export is all that's kept about one person: how far they've seen what's new, every change of a
    finished mark (as every place), and every browser login and sign-in link of theirs kept, by when each began and
    ends, marked whether it still lets someone in, and why not; never what's made from a token. Forget's dry run still
    counts the finished marks by title."""
    access.add_user("rory", "correct horse")
    person.saw_new("2026-11-10")
    person.place(TITLE, 12, 0.25)
    person.mark(TITLE, True)
    person.mark(TITLE, False)
    person.mark(TITLE, True)
    access.Sessions().start("rory", access.load_accounts()["rory"][1])
    access.add_user("rory", "a new password")  # that browser's login has ended
    access.Sessions().start("rory", access.load_accounts()["rory"][1])
    access.sign_in_link("rory")
    access.Sessions(now=lambda: datetime.now().timestamp() - 40 * 86400).start("rory", access.load_accounts()["rory"][1])  # ended
    found = person.export()
    assert found["new_seen"]["upto"] == "2026-11-10" and "op" not in found["new_seen"]
    assert [m["done"] for m in found["finished"]] == [True, False, True]
    assert [(login["kind"], login["works"], login.get("ended")) for login in found["logins"]] == [
        ("browser", False, "its time is up"), ("browser", False, "a new password since"), ("browser", True, None),
        ("sign-in link", True, None)] and all(login["since"] < login["until"] for login in found["logins"])
    with db.database().read() as c:
        made = [value for row in c.execute("SELECT digest, stamp FROM browser_login") for value in row]
    assert len(made) == 8 and not any(value in json.dumps(found) for value in made)
    cli.main(["people", "forget", "rory", "--dry-run"])
    assert "1 place and 1 finished mark," in capsys.readouterr().out  # one title marked, three times


def test_the_help_says_all_the_export_holds_and_both_ways_from_a_removed_user_s():
    """All the export holds, a deleted list's day, and a removed user restored or erased, in the help's What's kept
    about you."""
    kept = re.search(r'<section id="kept">(.*?)</section>', (Path(people.__file__).with_name("help.html")).read_text(encoding="utf-8"), re.S)[1]
    for words in ("the shelf you open with", "your own theme's colours", "how far you've been shown what's new",
                  "the browsers you've logged in on", "A list you delete is kept a day", "hylly user restore NAME",
                  "hylly people erase NAME"):
        assert words in kept, words


def test_hylly_people(person, capsys):
    person.place(TITLE, 12, 0.25)
    person.change_list("reading", "add", A)
    cli.main(["people", "list"])
    out = capsys.readouterr().out
    assert "rory" in out and "1 title" in out and "1 on the reading list" in out and "0 bookmarks" in out
    cli.main(["people", "export", "rory"])
    assert json.loads(capsys.readouterr().out)["places"][0]["at"] == 12
    cli.main(["people", "forget", "rory", "--yes"])
    assert "rory" in capsys.readouterr().out and person.latest() == {}
    with pytest.raises(SystemExit):
        cli.main(["people", "export", "nobody"])


def test_a_removed_user_is_set_aside_and_a_new_one_of_that_name_starts_afresh(person, capsys):
    access.add_user("rory", "correct horse")
    person.place(TITLE, 12, 0.25)
    cli.main(["user", "remove", "rory", "--yes"])
    assert "kept aside" in capsys.readouterr().out
    assert rows("person", "removed IS NOT NULL") == 1 and rows("place") == 1  # kept, no longer theirs
    assert people.everyone() == []
    assert again().latest() == {}


def test_a_renamed_user_keeps_it_all(person, capsys):
    access.add_user("rory", "correct horse", role="librarian")
    person.place(TITLE, 12, 0.25)
    stored = access.load_accounts()["rory"][1]
    cli.main(["user", "rename", "rory", "rowan"])
    assert "rowan" in capsys.readouterr().out
    assert access.load_accounts() == {"rowan": ("librarian", stored)}  # role and password too
    assert people.Person("rowan").latest()["h:novel"]["at"] == 12 and person.latest() == {}
    assert rows("person", "name = 'rowan'") == 1 and rows("person") == 1
    assert [p["name"] for p in people.everyone()] == ["rowan"]
    access.add_user("someone", "correct horse")
    with pytest.raises(SystemExit):  # a name in use isn't taken over
        cli.main(["user", "rename", "rowan", "someone"])
    with pytest.raises(SystemExit):
        cli.main(["user", "rename", "nobody", "new"])
    with pytest.raises(SystemExit):
        cli.main(["user", "rename", "rowan", "not a name"])
    assert "rowan" in access.load_accounts() and people.Person("rowan").latest()


def test_a_rename_takes_what_the_library_s_records_name_them_by_along_in_one_change(library, capsys, monkeypatch):
    """What the library's records name a person by goes with a rename, in the same change as their account and what's
    kept about them: the shelves for everyone they made, their changes to the catalog, their word on reported titles and
    their answers to the questions. So the server, which tells whose they are by the name logged in with, finds them
    theirs still, and a later user given the old name has none of it; a change that fails half way leaves it all as it
    was."""
    from hylly import curation, reports, shelves

    novel = {"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}
    people.Person("lucy").place(TITLE, 12, 0.25)
    shelves.Shared(library).set(shelves.clean({"id": "poems", "name": "Poems"}), by="lucy")
    curation.change(library, "add", "Knitting", [novel], by="lucy")
    reports.Decisions().decide(TITLE, "fine", "lucy", 0.0)
    curation.keep_questions(library, "drivethrurpg", [{"kind": "moved", "item": "71", "file": "0", "title": "Lantern Wolf",
                                                       "options": list(curation.MOVED), "guess": 0, "held": False, "why": ""}],
                            lambda q: True)
    (asked,) = curation.questions(library)
    curation.answer(library, asked["id"], "1", by="lucy")

    def theirs(name: str) -> list:
        return [shelves.Shared(library).read()[0]["poems"]["by"] == name,
                [e["by"] for e in curation.history(library) if e.get("tag") == "Knitting"] == [name],
                reports.Decisions().latest()["h:novel"]["by"] == name,
                curation.questions(library, answered=True)[0]["answer"]["by"] == name,
                "h:novel" in people.Person(name).latest()]
    assert theirs("lucy") == [True] * 5
    cli.main(["user", "rename", "lucy", "lena"])
    assert "lucy is now lena" in capsys.readouterr().out
    assert theirs("lena") == [True] * 5
    access.add_user("lucy", "battery staple", role="librarian")  # someone new given the old name: none of it is theirs
    assert theirs("lucy") == [False] * 5
    with monkeypatch.context() as m:  # a change that fails half way, after the account and the person, before the rest
        m.setattr(access, "NAMED_BY", (*access.NAMED_BY, ("no_such_table", "who")))
        with pytest.raises(sqlite3.OperationalError):
            access.rename_user("lena", "lina")
    assert "lena" in access.load_accounts() and "lina" not in access.load_accounts() and theirs("lena") == [True] * 5


def test_no_user_is_renamed_to_a_name_whose_earlier_user_is_kept_aside(library, capsys):
    """What was kept of a removed user waits under their name for hylly user restore, and the library's records say
    what they did under it: a rename to that name is refused, saying why and what to do, and nothing changes; once
    what's kept aside is erased for good, the name is free."""
    people.Person("bea").place(TITLE, 3, 0.1)
    shelves.Shared(library).set(shelves.clean({"id": "poems", "name": "Poems"}), by="bea")
    access.remove_user("bea")
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "rename", "rory", "bea"])
    said = capsys.readouterr().err
    assert e.value.code == exits.USAGE and "bea was a user before" in said and "(hylly people erase bea)" in said
    assert "rory" in access.load_accounts() and "bea" not in access.load_accounts()
    assert [a["name"] for a in people.aside("bea")] == ["bea"] and shelves.Shared(library).read()[0]["poems"]["by"] == "bea"
    cli.main(["people", "erase", "bea", "--yes"])
    cli.main(["user", "rename", "rory", "bea"])
    assert "rory is now bea" in capsys.readouterr().out


def test_no_new_user_is_given_a_name_whose_earlier_user_is_kept_aside_until_an_erase_frees_it(library, capsys, monkeypatch):
    """A new user of a removed user's name would take over what the library's records say that user did: hylly user add
    refuses it before it asks for a password, and so do the dry runs of add and rename, with what to do; nothing
    changes, and a user's own name still takes a new password. hylly people erase takes the name out of those records too,
    in the same change, putting a word no user can have in its place: then the name is free, and a new user of it has
    none of that."""
    from hylly import curation, reports

    novel = {"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}
    people.Person("lucy").place(TITLE, 12, 0.25)
    shelves.Shared(library).set(shelves.clean({"id": "poems", "name": "Poems"}), by="lucy")
    curation.change(library, "add", "Knitting", [novel], by="lucy")
    reports.Decisions().decide(TITLE, "fine", "lucy", 0.0)
    curation.keep_questions(library, "drivethrurpg", [{"kind": "moved", "item": "71", "file": "0", "title": "Lantern Wolf",
                                                       "options": list(curation.MOVED), "guess": 0, "held": False, "why": ""}],
                            lambda q: True)
    (asked,) = curation.questions(library)
    curation.answer(library, asked["id"], "1", by="lucy")

    def records(name: str) -> list:
        return [shelves.Shared(library).read()[0]["poems"]["by"] == name,
                [e["by"] for e in curation.history(library) if e.get("tag") == "Knitting"] == [name],
                reports.Decisions().latest()["h:novel"]["by"] == name,
                curation.questions(library, answered=True)[0]["answer"]["by"] == name]
    named = access.named("lucy")
    cli.main(["user", "remove", "lucy", "--yes"])
    capsys.readouterr()
    monkeypatch.setattr(cli, "_hidden", lambda prompt, command: pytest.fail("a password was asked for"))
    for words, way in ((["user", "add", "lucy"], "Bring them back with hylly user restore lucy"),
                       (["user", "add", "lucy", "--dry-run"], "Bring them back with hylly user restore lucy"),
                       (["user", "rename", "rory", "lucy", "-n"], "renamed to it, rory would take over")):
        with pytest.raises(SystemExit) as e:
            cli.main(words)
        said = capsys.readouterr()
        assert e.value.code == exits.USAGE and "lucy was a user before" in said.err and way in said.err, words
        assert "(hylly people erase lucy)" in said.err and not said.out, words
    with pytest.raises(SystemExit, match="lucy was a user before"):
        access.add_user("lucy", "battery staple")
    assert "lucy" not in access.load_accounts() and records("lucy") == [True] * 4 and access.named("lucy") == named
    assert not access.add_user("owen", "a new password")  # a user's own name: a new password, as ever
    cli.main(["people", "erase", "lucy", "--dry-run"])
    said = capsys.readouterr().out
    assert (f"The library's records that name lucy, {named} of them (shelves for everyone, changes, the word on reported "
            f'titles, answers to questions), would say "{access.FORMER}" instead. Then lucy is free for a new user.') in said
    assert records("lucy") == [True] * 4
    cli.main(["people", "erase", "lucy", "--yes"])
    said = capsys.readouterr().out
    assert f'The library\'s records that named lucy, {named} of them, say "{access.FORMER}" now. lucy is free for a new user.' in said
    assert records(access.FORMER) == [True] * 4 and access.named("lucy") == 0
    assert not access.NAME.fullmatch(access.FORMER)  # no user can be given it, so no one takes these over
    access.add_user("lucy", "battery staple", role="librarian")
    assert records("lucy") == [False] * 4 and people.Person("lucy").latest() == {}


def test_a_user_removed_with_nothing_kept_but_their_name_in_the_records_is_kept_aside_too(library):
    """Someone who only ever changed the catalog, made a shelf for everyone say, has no row of their own: removed, one is
    kept aside for their name all the same, since when they were a user. So a new user of the name is refused as for
    anyone kept aside, hylly user restore brings them back to what the records say they did, and an erase would free
    it. With nothing kept and nothing named, the name is free at once."""
    access.add_user("lena", "correct horse", role="librarian")
    shelves.Shared(library).set(shelves.clean({"id": "poems", "name": "Poems"}), by="lena")
    assert rows("person", "name = 'lena'") == 0
    assert access.remove_user("lena") == "set aside"
    (kept,) = people.aside("lena")
    assert people.kept_in_words(kept["kept"]) == "only their name and since when" and kept["since"] and kept["removed"]
    with pytest.raises(SystemExit, match="lena was a user before"):
        access.add_user("lena", "battery staple")
    access.restore_user("lena", "battery staple", role="librarian")
    assert people.aside("lena") == [] and shelves.Shared(library).read()[0]["poems"]["by"] == "lena"
    access.add_user("gus", "correct horse")
    assert access.remove_user("gus") is True and people.aside("gus") == []
    assert access.add_user("gus", "battery staple")


def test_a_removed_user_comes_back_with_all_that_was_kept_aside_while_the_name_is_free(person, capsys, monkeypatch):
    """hylly user restore NAME: a user removed by mistake comes back with their places, lists and bookmarks, and
    a new password, as theirs went with them. hylly people list shows what's kept aside apart, with when. Only while no
    user has the name: else it says what to do first."""
    monkeypatch.setattr(cli, "_hidden", lambda prompt, command: "correct horse")
    access.add_user("owen", "correct horse")
    access.add_user("rory", "correct horse", role="librarian")
    person.place(TITLE, 12, 0.25)
    person.change_list("reading", "add", A)
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    cli.main(["user", "remove", "rory", "--yes"])
    assert "hylly user restore rory" in capsys.readouterr().out
    cli.main(["people", "list"])
    out = capsys.readouterr().out
    assert re.search(r"^Kept aside.*\n^rory\s+1 title, 1 on the reading list, 1 bookmark, since \d{4}-\d\d-\d\d, "
                     r"removed \d{4}-\d\d-\d\d$", out, re.M), out
    cli.main(["people", "list", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed["people"] == [] and [(a["name"], a["kept"]["place"]) for a in listed["kept_aside"]] == [("rory", 1)]
    cli.main(["user", "list"])
    assert "Removed, with what was kept about them kept aside: rory (" in capsys.readouterr().out
    cli.main(["user", "restore", "rory", "--dry-run"])
    out = capsys.readouterr().out
    assert "It would ask for a new password for rory, and bring them back as reader" in out and "1 place" in out
    assert "rory" not in access.load_accounts()
    cli.main(["user", "restore", "rory", "--role", "librarian"])
    assert "Brought back rory as librarian" in capsys.readouterr().out
    assert access.load_accounts()["rory"][0] == "librarian" and access.check_password("correct horse", access.load_accounts()["rory"][1])
    fresh = again()
    assert fresh.latest()["h:novel"]["at"] == 12 and [e["id"] for e in fresh.lists()["reading"]] == ["a"] and fresh.bookmarks()[0]["key"] == "k1"
    assert person.latest()["h:novel"]["at"] == 12  # one that had them in memory, as the server has, finds them again too
    assert people.aside() == [] and [p["name"] for p in people.everyone()] == ["rory"]
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "restore", "rory"])
    assert e.value.code == exits.USAGE and "Nothing is kept aside of rory" in capsys.readouterr().err

    cli.main(["user", "remove", "rory", "--yes"])
    people.Person("rory").place(OTHER, 3, 0.1)  # a place that came in late, after the removal: a row with no account
    cli.main(["user", "restore", "rory"])  # brings back what was kept aside, and sets that row aside, an earlier rory's
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "restore", "rory"])
    said = capsys.readouterr().err
    assert e.value.code == exits.USAGE and "rory is a user now" in said and "hylly user rename rory NEW" in said
    assert list(people.Person("rory").latest()) == ["h:novel"]  # theirs, as it was
    cli.main(["user", "rename", "rory", "rowan"])
    cli.main(["user", "restore", "rory"])
    assert list(again().latest()) == ["s:7"] and list(people.Person("rowan").latest()) == ["h:novel"]


def test_a_name_no_one_can_have_is_refused_and_the_empty_one_is_no_one_s_not_everyone_s(person, capsys):
    """An empty name, or one no user can have (access.NAME), names nobody: refused with words, exit 2, before anything is
    looked up; and what's kept aside of "" is nothing, not everyone's."""
    access.add_user("rory", "correct horse")
    person.place(TITLE, 12, 0.25)
    cli.main(["user", "remove", "rory", "--yes"])
    capsys.readouterr()
    for words in (["people", "erase", "", "--yes"], ["people", "erase", "ro%", "--yes"], ["people", "export", ""],
                  ["people", "forget", "", "--yes"], ["user", "restore", ""], ["user", "restore", "", "-n"], ["user", "restore", "a b"]):
        with pytest.raises(SystemExit) as e:
            cli.main(words)
        said = capsys.readouterr()
        assert e.value.code == exits.USAGE and "A user name is 1 to 64 letters" in said.err and not said.out, words
    assert people.aside("") == [] and [a["name"] for a in people.aside()] == ["rory"] and rows("person", "removed IS NOT NULL") == 1


def test_two_removals_in_one_second_come_back_in_the_order_they_happened(person, capsys, monkeypatch):
    """The latest removal is the one hylly user restore brings back, and hylly people list shows first, even within one
    second: never a row of that name with no account that a restore set aside itself."""
    class Clock(datetime):  # one second, as a fast computer goes through all of it
        ticks = 0

        @classmethod
        def now(cls, tz=None):
            Clock.ticks += 1
            return datetime(2026, 11, 14, 10, 0, 0, Clock.ticks).astimezone(tz)

    monkeypatch.setattr(access, "datetime", Clock)
    monkeypatch.setattr(cli, "_hidden", lambda prompt, command: "correct horse")
    access.add_user("owen", "correct horse")
    access.add_user("rory", "correct horse")
    person.place(TITLE, 12, 0.25)
    cli.main(["user", "remove", "rory", "--yes"])
    people.Person("rory").place(OTHER, 3, 0.1)  # a place that came in late, after the removal: a row with no account
    cli.main(["user", "restore", "rory"])  # brings back the removed one, and sets that row aside
    assert list(again().latest()) == ["h:novel"]
    cli.main(["user", "remove", "rory", "--yes"])
    assert len(people.aside("rory")) == 2
    assert people.aside("rory")[0]["removed"] > people.aside("rory")[1]["removed"]
    cli.main(["user", "restore", "rory"])
    assert list(again().latest()) == ["h:novel"]  # the latest removal, not the late place's row set aside before it
    capsys.readouterr()


def test_what_s_kept_aside_is_erased_for_good_and_never_what_s_a_user_s_now(person, capsys):
    """hylly people erase NAME: someone who has left can have it all erased, the foreign keys taking every row of
    theirs with them. It says what goes and asks first, and leaves the user of that name now, and everyone else, as
    they are."""
    person.place(TITLE, 12, 0.25)  # kept about a rory with no account, as from before the accounts
    person.mark(TITLE, True)
    person.name_list("l1", "Tabletop night")
    person.change_list("l1", "add", A)
    person.add_bookmark(TITLE, "k1", "useful", SPOT)
    person.change_shelf("set", SHELF)
    person.rate(TITLE, 4)
    person.report(TITLE, ["damaged"], "")
    people.Person("owen").place(TITLE, 30, 0.5)
    access.add_user("rory", "correct horse")  # a user of that name now: what was kept of that rory is kept aside
    people.Person("rory").place(OTHER, 3, 0.1)
    capsys.readouterr()
    tables = ("person", "place", "finished", "list", "list_entry", "bookmark", "shelf", "review", "report")
    before = {table: rows(table) for table in tables}
    cli.main(["people", "erase", "rory", "--dry-run"])
    said = capsys.readouterr().out
    assert said.startswith("It would erase for good what's kept aside of rory: 1 place, 1 finished mark, 1 list of their own, "
                           "1 title on their lists, 1 bookmark, 1 shelf, 1 review and 1 report, removed 20") and "Nothing changed" in said
    with pytest.raises(SystemExit) as e:  # nobody at the keyboard to say yes
        cli.main(["people", "erase", "rory"])
    assert e.value.code == exits.NEEDS_YOU and {table: rows(table) for table in tables} == before
    cli.main(["people", "erase", "rory", "--yes"])
    assert "Erased for good what's kept aside of rory" in capsys.readouterr().out
    assert {table: rows(table) for table in tables} == {"person": 2, "place": 2, "finished": 0, "list": 0, "list_entry": 0,
                                                       "bookmark": 0, "shelf": 0, "review": 0, "report": 0}
    assert list(people.Person("rory").latest()) == ["s:7"] and people.Person("owen").latest()["h:novel"]["at"] == 30
    with pytest.raises(SystemExit) as e:  # nothing aside now: the user rory's own is theirs, not kept aside
        cli.main(["people", "erase", "rory", "--yes"])
    said = capsys.readouterr().err
    assert e.value.code == exits.USAGE and "Nothing is kept aside of rory" in said and "hylly user remove rory" in said
    assert list(people.Person("rory").latest()) == ["s:7"]


def test_an_erase_names_all_that_goes_and_says_erased_only_for_what_went(person, capsys, monkeypatch):
    """What hylly people erase says goes is all of it, the small things too; and "Erased for good" is said only of what
    went: what another run brought back meanwhile stays, and is said so (exit 1)."""
    access.add_user("owen", "correct horse")
    access.add_user("rory", "correct horse")
    access.add_user("ann", "correct horse")
    person.saw_guide("book")
    person.pick_look("dusk")
    people.Person("ann").place(TITLE, 3, 0.1)
    people.Person("ann").forget_places()  # nothing of hers is left but her row
    for name in ("rory", "ann"):
        cli.main(["user", "remove", name, "--yes"])
    capsys.readouterr()
    cli.main(["people", "erase", "rory", "-n"])
    assert "what's kept aside of rory: the reader's guides they've had and the theme they picked, removed 20" in capsys.readouterr().out
    cli.main(["people", "erase", "ann", "-n"])
    assert "what's kept aside of ann: only their name and since when, removed 20" in capsys.readouterr().out

    def meanwhile(question):  # yes, said while another run brings rory back
        access.restore_user("rory", "correct horse")
        return True

    monkeypatch.setattr(cli, "_confirm", meanwhile)
    with pytest.raises(SystemExit) as e:
        cli.main(["people", "erase", "rory"])
    said = capsys.readouterr()
    assert e.value.code == exits.PARTLY and "Nothing was erased: what was kept aside of rory had changed meanwhile" in said.err
    assert "Erased" not in said.out and again().guides() == ["book"] and again().look() == "dusk"


# ---- the nightly copy ----------------------------------------------------------------------------

def test_the_nightly_copy(person, tmp_path):
    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    (root / "catalog").mkdir()
    assert db.nightly(root, datetime(2026, 11, 5, 4)) is None  # nothing to copy yet
    person.place(TITLE, 12, 0.25)
    db.own(db.database(), root)  # as the server does when it starts: only the database that keeps the library's records is copied
    first = db.nightly(root, datetime(2026, 11, 5, 4))
    assert first == root / "state" / "db-backup" / "2026-11-05"
    check = json.loads((first / "check.json").read_text(encoding="utf-8"))
    assert check["integrity"] == "ok" and check["rows"]["place"] == 1 and check["schema"] == len(db.migrations())
    assert json.loads((first / "place.jsonl").read_text(encoding="utf-8").splitlines()[0])["title"] == "h:novel"  # readable without Hylly
    readme = (first / "README.txt").read_text(encoding="utf-8")
    assert "To go back to this night" in readme and "the users' password hashes: keep it private" in readme
    copy = sqlite3.connect(f"{(first / 'hylly.db').as_uri()}?mode=ro", uri=True)  # a plain database, that opens anywhere
    assert copy.execute("SELECT count(*) FROM place").fetchone()[0] == 1 and copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    copy.close()
    assert not list(db.file().parent.glob("*.tonight"))
    person.place(TITLE, 13, 0.26)
    assert db.nightly(root, datetime(2026, 11, 5, 23)) is None  # one a day
    day = datetime(2026, 11, 6, 4)
    for n in range(10):
        assert db.nightly(root, day + timedelta(days=n))
    days = sorted(p.name for p in db.backups(root).iterdir())
    assert len(days) == db.BACKUPS_KEPT and days[-1] == "2026-11-15" and db.last_backup(root, now=day + timedelta(days=9))[0] == "2026-11-15"
    assert db.last_backup(root, now=datetime(2026, 11, 12, 4))[0] == "2026-11-12"  # one dated after the day it is doesn't count

    # going back to a night's copy, as its README says
    person.place(TITLE, 99, 0.99)
    db.close_all()
    shutil.copyfile(db.backups(root) / days[-1] / "hylly.db", db.file())
    for extra in ("hylly.db-wal", "hylly.db-shm"):
        (db.file().parent / extra).unlink(missing_ok=True)
    assert again().latest()["h:novel"]["at"] == 13


def test_the_copy_waits_for_its_hour_and_a_failure_is_said(person, tmp_path, monkeypatch):
    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    (root / "catalog").mkdir()
    person.place(TITLE, 12, 0.25)
    db.own(db.database(), root)
    said = []
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 5, 2))
    assert not said and db.last_backup(root) is None
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 5, 3))
    assert db.last_backup(root)[0] == "2026-11-05" and "copied" in said[-1]

    def no_room(*args, **kwargs):
        raise OSError("no room")

    monkeypatch.setattr(db, "_onto_the_library", no_room)
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 6, 3))  # never stops the server
    assert "couldn't be copied" in said[-1] and db.last_backup(root)[0] == "2026-11-05"
    assert not list(db.file().parent.glob("*.tonight"))


def test_a_failing_copy_raises_one_alarm_is_logged_once_a_day_and_its_mending_is_said(person, tmp_path, monkeypatch):
    """A copy that failed every hour once wrote a line every hour, raised no alarm, and anything unforeseen ended the
    copies until a restart. Now: one alarm as it starts, a line a day while it lasts, whatever went wrong, and a word
    when a copy is good again; a copy made but not good by its check counts as failed."""
    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    person.place(TITLE, 12, 0.25)
    db.own(db.database(), root)
    lines, told, kept = [], [], {}

    def tonight(day, hour):
        dbcopy.copy_tonight(root, lines.append, datetime(2026, 11, day, hour), said=kept, tell=lambda *note: told.append(note))

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(db, "_onto_the_library", full)
        for day in (5, 6):
            for hour in range(3, 9):
                tonight(day, hour)
        m.setattr(db, "_onto_the_library", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unforeseen")))
        tonight(7, 3)  # caught, said, and tried again: never the end of the copies
    failed = [line for line in lines if "couldn't be copied" in line]
    assert len(failed) == 3 and "No space left" in failed[0] and "RuntimeError: unforeseen" in failed[2]
    assert [note[0] for note in told] == ["The nightly copy failed"] and "No space left" in told[0][1]
    assert "/" not in told[0][2]  # nothing of the folders for the phone
    tonight(7, 4)
    assert db.last_backup(root, datetime(2026, 11, 7, 5))[0] == "2026-11-07" and "works again" in lines[-1]
    assert [note[0] for note in told] == ["The nightly copy failed", "The nightly copy works again"]
    real = db.export

    def damaged(source, folder):  # as SQLite's check of a damaged database says, in check.json too
        checked = {**real(source, folder), "integrity": "*** in database main ***"}
        (folder / "check.json").write_text(json.dumps(checked), encoding="utf-8")
        return checked

    monkeypatch.setattr(db, "export", damaged)
    tonight(8, 3)  # made, but not good
    assert "SQLite's check of the copy says: *** in database main ***" in lines[-1] and told[-1][0] == "The nightly copy failed"
    monkeypatch.setattr(db, "export", real)
    tonight(9, 3)
    assert told[-1][0] == "The nightly copy works again" and len(told) == 4


def test_no_copy_onto_the_folder_an_unmounted_disk_leaves(person, tmp_path, monkeypatch):
    person.place(TITLE, 12, 0.25)
    monkeypatch.setattr(db.paths, "library_missing", lambda root: True)
    assert db.nightly(tmp_path / "gone", datetime(2026, 11, 5, 4)) is None and not (tmp_path / "gone").exists()


def test_the_doctor_calls_a_copy_never_made_a_problem_once_people_have_been_kept_for_days(person, tmp_path):
    """A copy that never once worked was only information, while one that stopped was a problem."""
    from hylly import doctor

    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    person.place(TITLE, 12, 0.25)
    said = doctor._people(root)[0]
    assert said["state"] == doctor.INFO and "last copied to the library's disk never" in said["says"]  # kept only since now
    with db.database().write() as c:
        c.execute("UPDATE person SET since = ?", ((datetime.now() - timedelta(days=3)).astimezone().isoformat(timespec="seconds"),))
    said = doctor._people(root)[0]
    assert said["state"] == doctor.PROBLEM and "never" in said["says"] and "Its log says why" in said["next"]


def a_library(tmp_path, name="library"):
    """A library this computer's database keeps the records of, as the server takes it on when it starts."""
    root = tmp_path / name
    (root / "state").mkdir(parents=True)
    (root / "catalog").mkdir()
    db.own(db.database(), root)
    return root


def settings_folder(monkeypatch, where):
    """Another settings folder from here on, as another computer has: its own database."""
    monkeypatch.setenv("HYLLY_HOME", str(where))
    db.close_all()


def test_a_second_settings_folder_on_the_library_copies_nothing_and_the_first_ones_copies_stay(person, tmp_path, monkeypatch):
    person.place(TITLE, 12, 0.25)
    root, home, first = a_library(tmp_path), os.environ["HYLLY_HOME"], datetime(2026, 11, 1, 4)
    for n in range(db.BACKUPS_KEPT):
        db.nightly(root, first + timedelta(days=n))
    ours, marker = db.nights(root, "2026-11-30"), db.owner_file(root).read_bytes()
    settings_folder(monkeypatch, tmp_path / "another-computer")  # a new computer pointed at the library, say after a reinstall
    again().place(TITLE, 1, 0.01)  # a database of its own, without the library's records
    with pytest.raises(db.NotHere):
        db.own(db.database(), root)  # as its server starts
    said = []
    for n in range(db.BACKUPS_KEPT, db.BACKUPS_KEPT + 8):
        for hour in (0, 1, 2):  # every hour: why is said once
            dbcopy.copy_tonight(root, said.append, first + timedelta(days=n, hours=hour))
    assert len(said) == 1 and "wasn't copied" in said[0] and "kept in the database on" in said[0]
    assert db.nights(root, "2026-11-30") == ours and sorted(db.backups(root).iterdir()) == ours  # none taken, none pruned
    assert db.owner_file(root).read_bytes() == marker  # looked at, never taken on
    with pytest.raises(db.NotHere):
        db.nightly(root, first + timedelta(days=20), fresh=True)
    settings_folder(monkeypatch, home)  # the computer that keeps them goes on as before
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 20, 4))
    assert "copied" in said[-1]  # and those more than 14 days old went with it: 2026-11-01 to 2026-11-05
    assert db.nights(root, "2026-11-30") == [p for p in ours if p.name >= "2026-11-06"] + [db.backups(root) / "2026-11-20"]


def test_once_tonights_copy_is_there_the_database_isnt_asked_whose_it_is_and_why_not_is_still_said_once(person, tmp_path, monkeypatch):
    person.place(TITLE, 12, 0.25)
    root, home = a_library(tmp_path), os.environ["HYLLY_HOME"]
    settings_folder(monkeypatch, tmp_path / "another-computer")
    again().place(TITLE, 1, 0.01)
    asked, real = [], db._copied_here
    monkeypatch.setattr(db, "_copied_here", lambda *a: asked.append(a) or real(*a))
    said = []
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 5, 4))  # refused, and said why
    settings_folder(monkeypatch, home)
    db.nightly(root, datetime(2026, 11, 6, 4))  # the computer that keeps the records copies first the next night
    settings_folder(monkeypatch, tmp_path / "another-computer")
    asked.clear()
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 6, 5))
    assert not asked  # tonight's is there: nothing to ask
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 7, 4))  # refused again: nothing new to say
    assert asked and len(said) == 1 and "wasn't copied" in said[0]
    assert [p.name for p in db.nights(root, "2026-11-30")] == ["2026-11-06"]


def test_a_copy_names_its_library_and_those_made_before_it_did_still_count(person, tmp_path):
    person.place(TITLE, 12, 0.25)
    root = a_library(tmp_path)
    first = datetime(2026, 11, 1, 4)
    for n in range(db.BACKUPS_KEPT):
        db.nightly(root, first + timedelta(days=n))
    for night in db.nights(root, "2026-11-30"):  # as copies made before check.json named the library
        check = json.loads((night / "check.json").read_text(encoding="utf-8"))
        assert check.pop("library") == db.marked(root)
        (night / "check.json").write_text(json.dumps(check), encoding="utf-8")
    assert len(db.nights(root, "2026-11-30")) == db.BACKUPS_KEPT and not db.strangers(root, "2026-11-30")
    assert db.last_backup(root, datetime(2026, 11, 30))[0] == "2026-11-07"
    db.nightly(root, datetime(2026, 11, 8, 4))
    assert [p.name for p in db.nights(root, "2026-11-30")] == [f"2026-11-{n:02d}" for n in range(2, 9)]  # the oldest went, as one of them


def test_another_databases_copies_are_never_counted_nor_removed_and_said_once(person, tmp_path, monkeypatch):
    person.place(TITLE, 12, 0.25)
    root, home = a_library(tmp_path), os.environ["HYLLY_HOME"]
    settings_folder(monkeypatch, tmp_path / "another-computer")
    again().place(TITLE, 3, 0.03)
    theirs = db.nightly(a_library(tmp_path, "elsewhere"), datetime(2026, 11, 2, 4))
    shutil.copytree(theirs, db.backups(root) / "2026-11-02")  # as another computer's server left them before this was fixed
    nobodys = db.backups(root) / "2026-11-03"  # and one of a database that kept no library's records
    shutil.copytree(theirs, nobodys)
    check = json.loads((nobodys / "check.json").read_text(encoding="utf-8"))
    check.pop("library", None)  # as a copy made before check.json named its library: its own database is asked
    (nobodys / "check.json").write_text(json.dumps(check), encoding="utf-8")
    with contextlib.closing(sqlite3.connect(nobodys / db.FILE)) as c:
        c.execute("DELETE FROM library")
        c.commit()
    settings_folder(monkeypatch, home)
    said = []
    for n in range(4, 14):
        for hour in (4, 5):
            dbcopy.copy_tonight(root, said.append, datetime(2026, 11, n, hour))
    ours = [f"2026-11-{n:02d}" for n in range(7, 14)]
    assert sorted(p.name for p in db.backups(root).iterdir()) == ["2026-11-02", "2026-11-03"] + ours  # none removed, a week kept
    assert [p.name for p in db.nights(root, "2026-11-30")] == ours  # theirs never counted
    left = [s for s in said if s.startswith("Left as they are")]
    assert len(left) == 1 and ("the copies of 2026-11-02, whose database names another library than database.json does, and of "
                               "2026-11-03, whose database names no library.") in left[0]
    assert [p.name for p in db.strangers(root, "2026-11-30")] == ["2026-11-02", "2026-11-03"]
    assert db.last_backup(root, datetime(2026, 11, 30))[0] == "2026-11-13"
    shutil.copytree(theirs, db.backups(root) / "2026-11-14")  # in tonight's place: left there, and no copy tonight
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 14, 4))
    assert "wasn't copied" in said[-1] and "holds a copy of another database" in said[-1]
    assert db.whose(db.backups(root) / "2026-11-14") == db.whose(theirs) != db.marked(root)


def test_a_copy_from_before_the_database_kept_the_library_s_records_is_the_library_s_own_and_goes_in_its_turn(person, tmp_path):
    person.place(TITLE, 12, 0.25)
    root = a_library(tmp_path)
    for n in (1, 2, 3, 4):
        db.nightly(root, datetime(2026, 11, n, 4))
    for n in (1, 2):  # as the server made them before the library's records moved into the database
        made_before_the_library_was_kept(db.backups(root) / f"2026-11-0{n}")
    nameless = db.backups(root) / "2026-11-04"  # and one of a database that never took the library on, though it could
    check = json.loads((nameless / "check.json").read_text(encoding="utf-8"))
    (nameless / "check.json").write_text(json.dumps({**check, "library": None}), encoding="utf-8")
    assert db.whose(db.backups(root) / "2026-11-01") == db.EARLIER
    assert [p.name for p in db.nights(root, "2026-11-30")] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert [p.name for p in db.strangers(root, "2026-11-30")] == ["2026-11-04"]
    said = []
    for n in range(5, 10):
        dbcopy.copy_tonight(root, said.append, datetime(2026, 11, n, 4))
    assert [p.name for p in db.nights(root, "2026-11-30")] == ["2026-11-02", "2026-11-03"] + [f"2026-11-0{n}" for n in range(5, 10)]
    assert (nameless / db.FILE).is_file()  # the oldest went, as one of them; the nameless one stays
    left = [s for s in said if s.startswith("Left as they are")]
    assert len(left) == 1 and "the copies of 2026-11-04, whose database names no library." in left[0] and "2026-11-02" not in left[0]


def test_a_library_without_its_marker_counts_every_copy_as_before(person, tmp_path, monkeypatch):
    from hylly import doctor

    person.place(TITLE, 12, 0.25)
    root, home = a_library(tmp_path), os.environ["HYLLY_HOME"]
    for n in (1, 2):
        db.nightly(root, datetime(2026, 11, n, 4))
    made_before_the_library_was_kept(db.backups(root) / "2026-11-01")
    settings_folder(monkeypatch, tmp_path / "another-computer")
    again().place(TITLE, 3, 0.03)
    theirs = db.nightly(a_library(tmp_path, "elsewhere"), datetime(2026, 11, 3, 4))
    shutil.copytree(theirs, db.backups(root) / "2026-11-03")  # another database's, as copies were made before the fix
    settings_folder(monkeypatch, home)
    db.owner_file(root).unlink()  # no database has taken the library on: nothing tells these apart
    assert [p.name for p in db.nights(root, "2026-11-30")] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert not db.strangers(root, "2026-11-30") and db.last_backup(root, datetime(2026, 11, 30))[0] == "2026-11-03"
    assert "last copied to the library's disk 2026-11-03" in doctor._people(root)[0]["says"]
    said = []
    dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 4, 4))  # and none is made, nor any removed
    assert "isn't there" in said[-1] and "This computer's database is copied once it keeps the records, which" in said[-1]
    assert len(list(db.backups(root).iterdir())) == 3


def test_an_older_copy_on_a_windows_share_can_be_read(person, tmp_path):
    assert db._address(PureWindowsPath(r"\\server\share\library\hylly.db")) == "file:////server/share/library/hylly.db"
    assert db._address(PureWindowsPath(r"C:\library\hylly.db")) == "file:///C:/library/hylly.db"  # as before
    person.place(TITLE, 12, 0.25)
    root = a_library(tmp_path)
    night = made_before_the_library_was_kept(db.nightly(root, datetime(2026, 11, 1, 4)))
    share = Path(f"\\\\localhost\\{str(night)[0].lower()}$" + str(night)[2:]) if os.name == "nt" else None
    if share is None or not share.is_dir():
        pytest.skip("this computer's disk can't be reached as a share here")
    assert db.whose(share) == db.EARLIER  # its database read, on the share


# ---- the server ----------------------------------------------------------------------------------

@pytest.fixture
def users():
    return EVERY_ROLE  # the library fixture's: bea a borrower, rory a reader, lucy a librarian, owen an owner


def place(browser, key=None, **body):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/place", json={"id": "h:novel", "at": 12, "pct": 0.25, **body}, headers={"X-Personal-Key": key})


@pytest.mark.parametrize("name", ["bea", "rory", "lucy", "owen"])
def test_every_role_keeps_a_place_of_its_own(server, name):
    with logged_in(server, name) as browser:
        session = browser.get("/api/session").json()
        assert session["me"]["key"]
        answer = place(browser, at=7 + len(name), pct=0.1)
        assert answer.status_code == 200 and answer.json()["ok"]
        found = browser.get("/api/me/places").json()["places"]["h:novel"]
        assert found["at"] == 7 + len(name) and found["t"] and found["f"]  # what the title is, from the server's own list
    assert people.Person(name).latest()["h:novel"]["pct"] == 0.1


def test_one_persons_places_are_theirs_alone(server):
    with logged_in(server, "rory") as rory, logged_in(server, "lucy") as lucy:
        assert place(rory, at=1, pct=0.01).status_code == 200
        assert place(lucy, at=2, pct=0.02).status_code == 200
        assert rory.get("/api/me/places").json()["places"]["h:novel"]["at"] == 1
        assert lucy.get("/api/me/places").json()["places"]["h:novel"]["at"] == 2


def test_the_servers_clock_and_the_servers_word_on_the_device(server):
    with logged_in(server, "rory") as browser:
        key = browser.get("/api/session").json()["me"]["key"]
        got = browser.post("/api/me/place", json={"id": "h:novel", "at": 3, "pct": 0.1, "when": "1999-01-01", "time": 1, "t": "Forged", "device": "forged"},
                           headers={"X-Personal-Key": key, "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) Version/26.6 Mobile/15E148 Safari/604.1"})
        assert got.status_code == 200
    found = people.Person("rory").latest()["h:novel"]
    assert found["when"] > "2020" and found["t"] != "Forged" and "iPhone" in found["device"]


def test_no_name_no_place(server):
    """Without a login nothing is answered, at home or through a tunnel, and nothing is kept."""
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:  # at home, with no login
        assert home.get("/api/session").status_code == 401
        assert home.post("/api/me/place", json={"id": "h:novel", "at": 1, "pct": 0.1}).status_code == 401
        assert home.get("/api/me/places").status_code == 401
    through = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}  # a tunnel on this computer, on the one port
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=through) as outside:  # from outside, with none
        assert outside.post("/api/me/place", json={"id": "h:novel", "at": 1, "pct": 0.1}).status_code == 401
        assert outside.get("/api/me/places").status_code == 401
    assert people.everyone() == []


def test_the_key_the_origin_and_json_are_all_needed(server):
    with logged_in(server, "rory") as browser:
        key = browser.get("/api/session").json()["me"]["key"]
        assert place(browser, key="wrong").status_code == 403
        assert place(browser, key="ää".encode()).status_code == 403  # a key that isn't plain letters: no crash
        assert browser.post("/api/me/place", json={"id": "h:novel", "at": 1, "pct": 0.1}).status_code == 403  # none
        other = {"X-Personal-Key": key, "Origin": "https://elsewhere.example"}
        assert browser.post("/api/me/place", json={"id": "h:novel", "at": 1, "pct": 0.1}, headers=other).status_code == 403
        form = {"X-Personal-Key": key, "Content-Type": "application/x-www-form-urlencoded"}
        assert browser.post("/api/me/place", content=b'{"id":"h:novel","at":1,"pct":0.1}', headers=form).status_code == 415
        assert people.Person("rory").latest() == {}


@pytest.mark.parametrize("body", [
    {"id": "no such title"}, {"id": 7}, {"id": None}, {"id": "x" * 500},
    {"at": None}, {"at": -1}, {"at": True}, {"at": 1.5}, {"at": ""}, {"at": "x" * 5000}, {"at": [1]}, {"at": serve.MAX_PAGES + 1},
    {"pct": None}, {"pct": -0.1}, {"pct": 1.1}, {"pct": "half"}, {"pct": True},
])
def test_what_isnt_a_place_isnt_kept(server, body):
    with logged_in(server, "rory") as browser:
        answer = place(browser, **body)
        assert 400 <= answer.status_code < 500 and not answer.json()["ok"]
    assert people.Person("rory").latest() == {}


def test_not_a_table_and_not_a_number_arent_kept(server):
    with logged_in(server, "rory") as browser:
        key = {"X-Personal-Key": browser.get("/api/session").json()["me"]["key"], "Content-Type": "application/json"}
        for raw in (b"[1,2]", b"{not json", b"", b'{"id":"h:novel","at":1,"pct":NaN}', b'{"id":"h:novel","at":1,"pct":Infinity}'):
            assert browser.post("/api/me/place", content=raw, headers=key).status_code == 400, raw
    assert people.Person("rory").latest() == {}


def test_what_comes_with_a_place_is_checked_and_a_late_one_is_dated_back(server):
    with logged_in(server, "rory") as browser:
        fresh = place(browser, at=40, pct=0.8, of=48, note="Chapter 3").json()
        late = place(browser, at=12, pct=0.25, age=3600).json()
        assert 3590 < fresh["time"] - late["time"] < 3610  # by the server's own clock
        found = browser.get("/api/me/places").json()["places"]["h:novel"]
        assert found["at"] == 40 and found["of"] == 48 and found["note"] == "Chapter 3"
        for body in ({"of": -1}, {"of": "x"}, {"of": True}, {"of": 10**12}, {"note": 7}, {"age": -5}, {"age": "x"}, {"age": True}):
            assert place(browser, at=99, pct=0.9, **body).status_code == 200  # left out, and no reason to lose the place
        last = every_place(people.Person("rory"))[-1]
        assert "of" not in last and "note" not in last and last["time"] > late["time"] + 3000
        assert len(place(browser, at=98, pct=0.9, note="x" * 1000).json()) and len(every_place(people.Person("rory"))[-1]["note"]) == 300
        place(browser, at=97, pct=0.9, age=10**12)  # no further back than a device could really have been away
        assert every_place(people.Person("rory"))[-1]["time"] > fresh["time"] - serve.MAX_AGE - 5


def test_past_the_limit_a_minute_a_change_gets_429_and_the_page_sends_it_again_later(server, monkeypatch):
    """Every change of one's own counts (places, marks, lists, bookmarks, shelves, stars, reviews): past the limit the
    answer is 429 with when to try again, and nothing is kept. The page's outbox keeps such a change and sends it later
    by itself, as it does when the server is away, instead of letting it go as one refused for good."""
    monkeypatch.setattr(server.RequestHandlerClass.people, "pace", people.Pace(3))
    with logged_in(server, "rory") as browser:
        key = browser.get("/api/session").json()["me"]["key"]
        assert [place(browser, key, at=n).status_code for n in (1, 2, 3)] == [200, 200, 200]
        refused = place(browser, key, at=4)
        assert refused.status_code == 429 and 0 < int(refused.headers["retry-after"]) <= 60 and not refused.json()["ok"]
        assert "minute" in refused.json()["error"] and people.Person("rory").latest()["h:novel"]["at"] == 3
        assert mark(browser, {"id": "h:novel", "mark": "finished"}, key).status_code == 429
        assert place(browser, "wrong", at=5).status_code == 403  # what's refused for another reason says so, as ever
    assert "unnamed_by_proxy" not in server.RequestHandlerClass.noted.counts  # a logged-in person's: not a lockout from a proxy
    with logged_in(server, "lucy") as other:
        assert place(other, at=9).status_code == 200  # each person has their own
    waits = outbox_in_node("""put({ "h:novel": { at: 4, pct: 0.1, file: "", t: Date.now() } });
answer = () => ({ status: 429, body: { ok: false, error: "Too many changes in a minute: try again in 9 seconds." } });
sendPlaces().then(() => process.stdout.write(JSON.stringify(Object.keys(box()))));""")
    assert waits == ["h:novel"]  # kept in the outbox, and sent again later


def test_one_title_can_be_asked_for(server):
    with logged_in(server, "rory") as browser:
        place(browser, at=1, pct=0.1)
        key = {"X-Personal-Key": browser.get("/api/session").json()["me"]["key"]}
        browser.post("/api/me/place", json={"id": "s:7", "at": 2, "pct": 0.2}, headers=key)
        assert set(browser.get("/api/me/places").json()["places"]) == {"h:novel", "s:7"}
        assert set(browser.get("/api/me/places", params={"id": "s:7"}).json()["places"]) == {"s:7"}
        assert browser.get("/api/me/places", params={"id": "no such"}).json()["places"] == {}


def mark(browser, body, key=None):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/mark", json=body, headers={"X-Personal-Key": key})


@pytest.mark.parametrize("name", ["bea", "rory", "lucy", "owen"])
def test_every_role_can_mark_a_title(server, name):
    with logged_in(server, name) as browser:
        assert mark(browser, {"id": "h:novel", "mark": "finished"}).json()["ok"]
        assert browser.get("/api/me/places").json()["places"]["h:novel"]["done"]
        assert mark(browser, {"id": "h:novel", "mark": "unfinished"}).status_code == 200
        assert browser.get("/api/me/places").json()["places"] == {}
        place(browser, at=9, pct=0.5)
        assert mark(browser, {"id": "h:novel", "mark": "over"}).status_code == 200
        found = browser.get("/api/me/places").json()["places"]["h:novel"]
        assert found["at"] == 0 and found["pct"] == 0


def test_a_mark_needs_what_a_place_needs(server):
    with logged_in(server, "rory") as browser:
        for body in ({"id": "h:novel"}, {"id": "h:novel", "mark": "burnt"}, {"id": "nope", "mark": "finished"}, {"id": 7, "mark": "finished"}, {"mark": "finished"}):
            answer = mark(browser, body)
            assert 400 <= answer.status_code < 500 and not answer.json()["ok"], body
        assert mark(browser, {"id": "h:novel", "mark": "finished"}, key="wrong").status_code == 403
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/mark", json={"id": "h:novel", "mark": "finished"}).status_code == 401
    assert people.Person("rory").latest() == {}


def test_what_has_been_seen_of_whats_new_goes_with_the_person(server):
    with logged_in(server, "rory") as browser:
        session = browser.get("/api/session").json()
        assert session["me"]["newSeen"] == ""
        key = {"X-Personal-Key": session["me"]["key"]}
        assert browser.post("/api/me/new-seen", json={"upto": "2026-11-05T20:00:00"}, headers=key).json()["ok"]
        assert browser.get("/api/session").json()["me"]["newSeen"] == "2026-11-05T20:00:00"
        for bad in ({}, {"upto": 7}, {"upto": "soon"}, {"upto": "2026-11-05T20:00:00'; x"}):
            assert browser.post("/api/me/new-seen", json=bad, headers=key).status_code == 400, bad
        assert browser.post("/api/me/new-seen", json={"upto": "2026-11-06T20:00:00"}).status_code == 403  # no key
    with logged_in(server, "lucy") as other:
        assert other.get("/api/session").json()["me"]["newSeen"] == ""  # another person hasn't seen it
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/new-seen", json={"upto": "2026-11-06T20:00:00"}).status_code == 401


def test_the_guides_had_go_with_the_person(server):
    with logged_in(server, "rory") as browser:
        session = browser.get("/api/session").json()
        assert session["me"]["guides"] == []
        key = {"X-Personal-Key": session["me"]["key"]}
        assert browser.post("/api/me/guide-seen", json={"kind": "book"}, headers=key).json()["ok"]
        assert browser.get("/api/session").json()["me"]["guides"] == ["book"]
        for bad in ({}, {"kind": 7}, {"kind": ["book"]}, {"kind": "audiobook"}, []):
            assert browser.post("/api/me/guide-seen", json=bad, headers=key).status_code == 400, bad
        assert browser.post("/api/me/guide-seen", json={"kind": "pages"}).status_code == 403  # no key
        assert browser.get("/api/session").json()["me"]["guides"] == ["book"]
    with logged_in(server, "lucy") as other:
        assert other.get("/api/session").json()["me"]["guides"] == []  # another person hasn't had it
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/guide-seen", json={"kind": "pages"}).status_code == 401


def test_the_login_page_says_when_there_is_nobody_to_log_in_as(server):
    """With no user at all, the login page says what to do, on the server computer: any login would be wrong anyway."""
    nobody = "Nobody can log in yet: on the server computer, add a user with hylly user add NAME."
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert nobody not in home.get("/login").text
        for name in ("bea", "rory", "lucy", "owen"):
            access.remove_user(name)
        assert nobody in home.get("/login").text and home.get("/api/session").status_code == 401
        wrong = home.post("/login", data={"username": "owen", "password": "correct horse", "next": "/"})
        assert wrong.status_code == 401 and nobody in wrong.text


def test_the_page_can_ask_whether_the_catalog_is_new(server, log_in):
    with log_in(httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME), "reader") as home:
        assert home.get("/api/catalog").json()["generated"]


def listing(browser, key=None, **body):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/list", json={"list": "reading", **body}, headers={"X-Personal-Key": key})


@pytest.mark.parametrize("name", ["bea", "rory", "lucy", "owen"])
def test_every_role_has_a_reading_list_of_its_own(server, name):
    with logged_in(server, name) as browser:
        assert browser.get("/api/me/lists").json()["lists"] == {}
        answer = listing(browser, op="add", id="h:novel").json()
        assert answer["ok"] and answer["changed"]
        rows = browser.get("/api/me/lists").json()["lists"]["reading"]
        assert [r["id"] for r in rows] == ["h:novel"] and rows[0]["t"]  # what it is, from the server's own list
        assert listing(browser, op="add", id="h:novel").json()["changed"] is False
        assert listing(browser, op="remove", id="h:novel").json()["changed"] is True


def test_one_persons_list_is_theirs_alone(server):
    with logged_in(server, "rory") as rory, logged_in(server, "lucy") as lucy:
        listing(rory, op="add", id="h:novel")
        assert lucy.get("/api/me/lists").json()["lists"] == {}


def test_a_list_change_needs_what_a_place_needs(server):
    with logged_in(server, "rory") as browser:
        for body in ({"op": "add"}, {"op": "add", "id": 7}, {"op": "burn", "id": "h:novel"}, {"op": "add", "id": "no such title"},
                     {"op": "add", "id": "h:novel", "list": "other"}, {"op": "move", "id": "h:novel", "before": 7}, {"op": "add", "id": "x" * 500}):
            answer = listing(browser, **body)
            assert 400 <= answer.status_code < 500 and not answer.json()["ok"], body
        assert listing(browser, key="wrong", op="add", id="h:novel").status_code == 403
        assert browser.post("/api/me/list", json={"list": "reading", "op": "add", "id": "h:novel"}).status_code == 403  # no key
        assert people.Person("rory").lists() == {}
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/list", json={"list": "reading", "op": "add", "id": "h:novel"}).status_code == 401
        assert home.get("/api/me/lists").status_code == 401


def test_every_role_has_lists_of_its_own_and_they_keep_to_their_person(server):
    for name in ("bea", "rory", "lucy", "owen"):
        with logged_in(server, name) as browser:
            assert browser.get("/api/me/lists").json()["names"] == []
            key = browser.get("/api/session").json()["me"]["key"]
            send = lambda **body: browser.post("/api/me/list", json=body, headers={"X-Personal-Key": key})  # noqa: E731
            assert send(list="l1", op="name", name="  Tabletop   night ").json()["changed"]
            assert send(list="l1", op="add", id="h:novel").json()["changed"]
            found = browser.get("/api/me/lists").json()
            assert found["names"] == [{"id": "l1", "name": "Tabletop night"}] and found["lists"]["l1"][0]["id"] == "h:novel"
            assert send(list="l1", op="delete").json()["changed"] and browser.get("/api/me/lists").json()["names"] == []
            assert send(list="l1", op="restore").json()["changed"] and browser.get("/api/me/lists").json()["lists"]["l1"][0]["id"] == "h:novel"
    with logged_in(server, "rory") as rory, logged_in(server, "lucy") as lucy:
        key = lucy.get("/api/session").json()["me"]["key"]
        assert lucy.post("/api/me/list", json={"list": "l2", "op": "name", "name": "Lucy's"}, headers={"X-Personal-Key": key}).json()["ok"]
        assert {"id": "l2", "name": "Lucy's"} not in rory.get("/api/me/lists").json()["names"]
        key = rory.get("/api/session").json()["me"]["key"]
        assert rory.post("/api/me/list", json={"list": "l2", "op": "add", "id": "h:novel"}, headers={"X-Personal-Key": key}).status_code == 404  # hers isn't theirs


def test_a_list_itself_is_checked_on_the_way_in(server):
    with logged_in(server, "rory") as browser:
        key = browser.get("/api/session").json()["me"]["key"]
        send = lambda **body: browser.post("/api/me/list", json=body, headers={"X-Personal-Key": key})  # noqa: E731
        for body, status in (({"list": "l1", "op": "name"}, 400), ({"list": "l1", "op": "name", "name": ""}, 400), ({"list": "l1", "op": "name", "name": "   "}, 400),
                             ({"list": "l1", "op": "name", "name": "x" * 61}, 400), ({"list": "l1", "op": "name", "name": "two\nlines"}, 400), ({"list": "l1", "op": "name", "name": 7}, 400),
                             ({"list": "../x", "op": "name", "name": "A"}, 400), ({"list": "x" * 41, "op": "name", "name": "A"}, 400), ({"list": 7, "op": "name", "name": "A"}, 400),
                             ({"op": "name", "name": "A"}, 400), ({"list": "reading", "op": "name", "name": "Mine"}, 400), ({"list": "reading", "op": "delete"}, 400),
                             ({"list": "l9", "op": "add", "id": "h:novel"}, 404), ({"list": "l1", "op": "burn"}, 400)):
            assert send(**body).status_code == status, body
        assert browser.get("/api/me/lists").json()["names"] == []
        assert send(list="l1", op="name", name="<b>Bold</b>").json()["ok"]
        assert browser.get("/api/me/lists").json()["names"][0]["name"] == "<b>Bold</b>"  # kept as text: the page shows it as text
        assert browser.post("/api/me/list", json={"list": "l1", "op": "delete"}).status_code == 403  # no key
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/list", json={"list": "l1", "op": "delete"}).status_code == 401


def test_too_many_lists_answer_so(server, monkeypatch):
    monkeypatch.setattr(people, "MAX_LISTS", 1)
    with logged_in(server, "rory") as browser:
        key = browser.get("/api/session").json()["me"]["key"]
        assert browser.post("/api/me/list", json={"list": "l1", "op": "name", "name": "One"}, headers={"X-Personal-Key": key}).status_code == 200
        answer = browser.post("/api/me/list", json={"list": "l2", "op": "name", "name": "Two"}, headers={"X-Personal-Key": key})
        assert answer.status_code == 409 and "1 lists" in answer.json()["error"]


def test_the_page_knows_the_limit_on_lists():
    from pathlib import Path

    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    assert f"LISTS_MOST = {people.MAX_LISTS}" in page and f'maxlength="{people.LIST_NAME_MAX}"' in page


def bookmarking(browser, personal=None, **body):
    personal = personal if personal is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/bookmark", json=body, headers={"X-Personal-Key": personal})


NEW = {"op": "add", "key": "k1", "id": "h:novel", "why": "useful", "at": 12, "pct": 0.25}


@pytest.mark.parametrize("name", ["bea", "rory", "lucy", "owen"])
def test_every_role_has_bookmarks_of_its_own(server, name):
    with logged_in(server, name) as browser:
        assert browser.get("/api/me/bookmarks").json()["bookmarks"] == []
        answer = bookmarking(browser, **NEW).json()
        assert answer["ok"] and answer["changed"]
        rows = browser.get("/api/me/bookmarks").json()["bookmarks"]
        assert [(r["key"], r["id"], r["why"], r["at"]) for r in rows] == [("k1", "h:novel", "useful", 12)] and rows[0]["t"]  # what it is, from the server's own list
        assert bookmarking(browser, **NEW).json()["changed"] is False
        assert bookmarking(browser, op="change", key="k1", why="funny").json()["changed"] is True
        assert browser.get("/api/me/bookmarks").json()["bookmarks"][0]["why"] == "funny"
        assert bookmarking(browser, op="remove", key="k1").json()["changed"] is True
        assert browser.get("/api/me/bookmarks").json()["bookmarks"] == []


def test_one_persons_bookmarks_are_theirs_alone(server):
    with logged_in(server, "rory") as rory, logged_in(server, "lucy") as lucy:
        bookmarking(rory, **NEW)
        assert lucy.get("/api/me/bookmarks").json()["bookmarks"] == []
        assert bookmarking(lucy, op="remove", key="k1").json()["changed"] is False  # another's key is no key here
        assert len(rory.get("/api/me/bookmarks").json()["bookmarks"]) == 1


def test_a_bookmark_needs_what_a_place_needs(server):
    with logged_in(server, "rory") as browser:
        for body in ({}, {"op": "burn", "key": "k1"}, {**NEW, "key": 7}, {**NEW, "key": ""}, {**NEW, "key": "../k"}, {**NEW, "key": "x" * 41},
                     {**NEW, "why": "bad"}, {**NEW, "why": ["useful"]}, {k: v for k, v in NEW.items() if k != "why"}, {"op": "change", "key": "k1", "why": "bad"},
                     {**NEW, "id": 7}, {**NEW, "id": "no such title"}, {**NEW, "id": "x" * 500}, {**NEW, "at": -1}, {**NEW, "at": True}, {**NEW, "at": ""}, {**NEW, "at": "x" * 5000},
                     {**NEW, "at": None}, {**NEW, "pct": 2}, {**NEW, "pct": True}, {**NEW, "pct": "half"}):
            answer = bookmarking(browser, **body)
            assert 400 <= answer.status_code < 500 and not answer.json()["ok"], body
        assert bookmarking(browser, personal="wrong", **NEW).status_code == 403
        assert browser.post("/api/me/bookmark", json=NEW).status_code == 403  # no key
        assert people.Person("rory").bookmarks() == []
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/bookmark", json=NEW).status_code == 401
        assert home.get("/api/me/bookmarks").status_code == 401


def test_what_comes_with_a_bookmark_is_checked_and_a_long_text_cut(server):
    with logged_in(server, "rory") as browser:
        novel = next(i for i in serve_items(server) if i["id"] == "h:novel")
        bookmarking(browser, **{**NEW, "file": novel["f"][0][0], "of": 48, "page": 11, "part": 3, "inside": 0.5, "note": "Chapter", "text": "w" * 900, "word": "m" * 900,
                                "age": 3600, "t": "Forged", "time": 1, "extra": "kept out"})
        bookmarking(browser, **{**NEW, "key": "k2", "file": "../../secret.txt", "of": -1, "page": "x", "part": True, "inside": 7, "note": 5, "text": ["w"], "word": 3, "age": "soon"})
        good, bad = people.Person("rory").bookmarks()
        assert good["file"] == novel["f"][0][0] and (good["of"], good["page"], good["part"], good["inside"], good["note"]) == (48, 11, 3, 0.5, "Chapter")
        assert len(good["text"]) == serve.MAX_WORDS and len(good["word"]) == serve.MAX_MARKED and good["t"] == novel["t"] and good["time"] > 1000 and "extra" not in good
        assert bad["time"] - good["time"] > 3500  # the first was dated back, the second wasn't
        assert bad["file"] == "" and not {"of", "page", "part", "inside", "note", "text", "word"} & set(bad)


def test_a_full_set_of_bookmarks_and_a_newer_format_answer_so(server, monkeypatch):
    with logged_in(server, "rory") as browser:
        monkeypatch.setattr(people, "MAX_BOOKMARKS", 1)
        personal = browser.get("/api/session").json()["me"]["key"]
        assert bookmarking(browser, **NEW).status_code == 200
        answer = bookmarking(browser, **{**NEW, "key": "k2"})
        assert answer.status_code == 409 and "1 bookmarks" in answer.json()["error"]
        newer_database()
        answer = bookmarking(browser, personal, op="remove", key="k1")
        assert answer.status_code in (401, 403)  # nobody is known to a database of a newer version: not even who this is


def shelving(browser, personal=None, **body):
    personal = personal if personal is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/shelf", json=body, headers={"X-Personal-Key": personal})


def sharing(browser, key=None, **body):
    key = key if key is not None else browser.get("/api/session").json()["key"]
    return browser.post("/api/shelves", json=body, headers={"X-Server-Key": key or "none"})


@pytest.mark.parametrize("name", ["bea", "rory", "lucy", "owen"])
def test_every_role_has_shelves_of_its_own(server, name):
    with logged_in(server, name) as browser:
        assert browser.get("/api/shelves").json()["mine"] == []
        assert shelving(browser, op="set", shelf={**SHELF, "extra": "kept out", "by": "forged"}).json()["changed"]
        assert shelving(browser, op="open", id="m1").json()["changed"]
        found = browser.get("/api/shelves").json()
        assert found["mine"] == [SHELF] and found["open"] == "m1"
        assert shelving(browser, op="delete", id="m1").json()["changed"] is True
        assert browser.get("/api/shelves").json()["mine"] == []


def test_ones_own_shelves_are_theirs_alone(server):
    with logged_in(server, "rory") as rory, logged_in(server, "lucy") as lucy:
        shelving(rory, op="set", shelf=SHELF)
        assert lucy.get("/api/shelves").json()["mine"] == []
        assert shelving(lucy, op="delete", id="m1").json()["changed"] is False
        assert len(rory.get("/api/shelves").json()["mine"]) == 1


def test_a_shelf_is_checked_on_the_way_in(server):
    bad = [None, 7, [], {}, {"id": "m1"}, {"name": "No id"}, {**SHELF, "id": "../x"}, {**SHELF, "id": 7}, {**SHELF, "id": "x" * 41}, {**SHELF, "name": ""}, {**SHELF, "name": "   "},
           {**SHELF, "name": 7}, {**SHELF, "name": "x" * 41}, {**SHELF, "name": "two\nlines"}, {**SHELF, "show": []}, {**SHELF, "show": {"nope": ["x"]}}, {**SHELF, "show": {"k": "comic"}},
           {**SHELF, "show": {"k": [7]}}, {**SHELF, "hide": {"k": [""]}}, {**SHELF, "hide": {"k": ["x" * 101]}}, {**SHELF, "hide": {"k": ["x"] * 51}}]
    with logged_in(server, "rory") as browser:
        for shelf in bad:
            answer = shelving(browser, op="set", shelf=shelf)
            assert answer.status_code == 400 and not answer.json()["ok"], shelf
        for body in ({}, {"op": "burn"}, {"op": "delete"}, {"op": "delete", "id": ""}, {"op": "delete", "id": 7}, {"op": "open", "id": "../x"}):
            assert shelving(browser, **body).status_code == 400, body
        assert browser.get("/api/shelves").json()["mine"] == []
        assert shelving(browser, op="set", shelf={**SHELF, "name": "  Two   spaces ", "sort": "nonsense", "group": "no"}).status_code == 200
        kept = browser.get("/api/shelves").json()["mine"][0]
        assert kept["name"] == "Two spaces" and "sort" not in kept and "group" not in kept  # tidied, and only what it knows
        assert shelving(browser, op="set", shelf={**SHELF, "name": "<b>Bold</b>", "sort": "author", "group": False}).status_code == 200
        kept = browser.get("/api/shelves").json()["mine"][0]
        assert kept["name"] == "<b>Bold</b>" and kept["sort"] == "author" and kept["group"] is False  # kept as text: the page shows it as text
        assert shelving(browser, personal="wrong", op="set", shelf=SHELF).status_code == 403
        assert browser.post("/api/me/shelf", json={"op": "set", "shelf": SHELF}).status_code == 403  # no key
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.post("/api/me/shelf", json={"op": "set", "shelf": SHELF}).status_code == 401


def test_a_shared_shelf_is_for_librarians_and_the_owner_and_all_can_see_it(server, log_in):
    with logged_in(server, "bea") as borrower, logged_in(server, "rory") as reader, logged_in(server, "lucy") as librarian, logged_in(server, "owen") as owner:
        for someone in (borrower, reader):
            assert sharing(someone, op="set", shelf=SHELF).status_code == 403
        answer = sharing(librarian, op="set", shelf=SHELF).json()
        assert answer["ok"] and answer["shared"][0]["name"] == "No games" and answer["shared"][0]["by"] == "lucy"
        for someone in (borrower, reader, librarian, owner):
            assert someone.get("/api/shelves").json()["shared"][0]["id"] == "m1"
        assert sharing(librarian, key="wrong", op="set", shelf=SHELF).status_code == 403
        assert sharing(owner, op="set", shelf={**SHELF, "name": "Renamed"}).json()["shared"][0]["by"] == "lucy"  # still theirs
        assert sharing(librarian, op="open", id="m1").status_code == 403  # the library's default is the owner's
        assert sharing(owner, op="open", id="no-such").status_code == 404
        assert sharing(owner, op="open", id="m1").json()["standard"] == "m1"
        assert reader.get("/api/shelves").json()["standard"] == "m1"
        other = {**SHELF, "id": "s2", "name": "The owner's"}
        sharing(owner, op="set", shelf=other)
        answer = sharing(librarian, op="delete", id="s2")
        assert answer.status_code == 403 and "someone else" in answer.json()["error"]
        assert sharing(librarian, op="set", shelf={**other, "name": "Taken"}).status_code == 403
        assert sharing(owner, op="delete", id="m1").json()["standard"] == ""  # deleted, it no longer opens anything
        assert [s["id"] for s in sharing(owner, op="delete", id="m1").json()["shared"]] == ["s2"]
    with log_in(httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME), "reader") as home:  # to read them
        found = home.get("/api/shelves").json()
        assert [s["id"] for s in found["shared"]] == ["s2"] and found["mine"] == []
        assert home.post("/api/shelves", json={"op": "delete", "id": "s2"}, headers={"X-Server-Key": "wrong"}).status_code == 403


def test_a_borrower_sees_the_shelves_for_everyone_but_not_who_made_them(server):
    """A borrower's account is a guest's: a shelf for everyone is theirs to use, but who made it, an account name of the
    household, is for readers and up, who may also make and change such shelves."""
    with logged_in(server, "bea") as borrower, logged_in(server, "rory") as reader, logged_in(server, "lucy") as librarian:
        sharing(librarian, op="set", shelf=SHELF)
        seen = borrower.get("/api/shelves").json()["shared"]
        assert [s["id"] for s in seen] == ["m1"] and "by" not in seen[0] and seen[0]["name"] == "No games"
        assert "lucy" not in borrower.get("/api/shelves").text
        assert reader.get("/api/shelves").json()["shared"][0]["by"] == "lucy"
        assert librarian.get("/api/shelves").json()["shared"][0]["by"] == "lucy"


def test_a_shelf_for_everyone_whose_maker_was_erased_stays_and_only_an_owner_may_change_it(server, capsys):
    """Its maker removed and erased for good, a shelf for everyone stays, made by a former user, which no user is: so
    only an owner may change it, and a new user given the old name may not, as it was never theirs."""
    with logged_in(server, "lucy") as librarian:
        sharing(librarian, op="set", shelf=SHELF)
    cli.main(["user", "remove", "lucy", "--yes"])
    cli.main(["people", "erase", "lucy", "--yes"])
    capsys.readouterr()
    access.add_user("lucy", "correct horse", role="librarian")
    with logged_in(server, "owen") as owner, logged_in(server, "lucy") as librarian, logged_in(server, "rory") as reader:
        assert reader.get("/api/shelves").json()["shared"] == [{**SHELF, "by": access.FORMER}]
        answer = sharing(librarian, op="set", shelf={**SHELF, "name": "Taken"})
        assert answer.status_code == 403 and "someone else" in answer.json()["error"]
        assert sharing(librarian, op="delete", id="m1").status_code == 403
        assert sharing(owner, op="set", shelf={**SHELF, "name": "Renamed"}).json()["shared"] == [
            {**SHELF, "name": "Renamed", "by": access.FORMER}]


def test_shared_shelves_keep_who_made_them_and_their_limit(tmp_path, monkeypatch):
    library = tmp_path
    build_library(library)
    catalog.build(library, log=lambda s: None)
    for name, role in (("owen", "owner"),):
        access.add_user(name, "correct horse", role=role)
    shared = shelves.Shared(library)
    shared.set(SHELF, "lucy")
    shared.set({**SHELF, "id": "gone"}, "lucy")
    assert shared.delete("gone")
    shared.opens_with("m1")
    assert shared.read() == ({"m1": {**SHELF, "by": "lucy"}}, "m1")
    shared.set({**SHELF, "id": "s2"}, "someone")
    assert list(shared.read()[0]) == ["m1", "s2"]
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server = srv
    monkeypatch.setattr(shelves, "MAX_SHELVES", 2)
    with pytest.raises(shelves.Full):
        shared.set({**SHELF, "id": "s3"}, "someone")
    with logged_in(server, "owen") as owner:
        assert sharing(owner, op="set", shelf={**SHELF, "id": "s3"}).status_code == 409
    srv.shutdown()


def test_the_page_offers_the_fields_the_server_keeps():
    from pathlib import Path
    import re

    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    offered = re.search(r"const SHELF_FIELDS = \{(.*?)\};", page)[1]
    assert re.findall(r"(\w+): \"", offered) == list(shelves.FIELDS)
    assert "SHELVES_MOST" not in page and "limits: d.limits || null" in page  # the server's limits come with the shelves
    assert f'maxlength="{shelves.NAME_MAX}"' in page


def test_a_shelf_by_rating_or_reported_is_refused_so_the_page_leaves_them_out_and_says_so(server):
    with logged_in(server, "rory") as browser:
        for key in ("rt", "rp"):
            answer = shelving(browser, op="set", shelf={**SHELF, "show": {key: ["4"]}})
            assert answer.status_code == 400 and answer.json()["error"] == "A shelf can't go by that."
        assert browser.get("/api/shelves").json()["mine"] == []
    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    filters = re.findall(r'^  (\w+): \{ name: "', page[page.index("const FACET = {"):page.index("\n};", page.index("const FACET = {"))], re.M)
    assert sorted(set(filters) - set(shelves.FIELDS)) == ["rp", "rt"]  # a filter a shelf can't go by
    sheet = page[page.index("function openShelfSheet("):page.index("function shelfAppsRow(")]
    assert "if (!shelfGoesBy(key)) {" in sheet and '$("shelfleft").hidden = !unkept.length;' in sheet  # left out, and said
    assert 'id="shelfleft"' in page


def test_a_shelf_change_the_server_refuses_is_said_and_not_shown_as_kept():
    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    kinds = page[page.index("const WAITING_KINDS = ["):page.index("const kindOf = ")]
    assert '["shelf:", "/api/me/shelf", p => p.change, (key, p, d) => shelfRefused(key, p, d)],' in kinds  # a 400 or 409 was let go without a word
    sending = page[page.index("async function sendOnePlace("):page.index("let placesGoing")]
    assert "if (refused) refused(key, p, d);" in sending
    refused = page[page.index("function shelfRefused("):page.index("function shelvesRefusedSaid(")]
    assert "withdraw(key, p);" in refused and "toast(" not in refused  # out of the outbox at once, or it comes back; said later
    places = page[page.index("async function sendPlaces("):page.index("function ownPlace(")]
    assert places.index("} finally { placesGoing = false; }") < places.index("shelvesRefusedSaid();")  # once the outbox has been through
    said = page[page.index("function shelvesRefusedSaid("):page.index("async function syncShelves(")]
    assert said.count("toast(") == 1 and "syncShelves();" in said  # several refused at a login: one toast names them all


def test_an_old_shelf_by_rating_comes_along_at_login_without_that_row():
    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    along = page[page.index("function shelvesComeAlong("):page.index("// Refused for good")]
    assert "rows.filter(([key]) => shelfGoesBy(key))" in along  # the rest of the shelf is kept, not refused whole
    assert 'shelfChange({ op: "set", shelf: { ...shelf, show: kept(shelf.show), hide: kept(shelf.hide) } });' in along
    assert along.count("toast(") == 1 and "left out of" in along  # said once, for all of them
    assert along.index("if (cut.has(shelfId)) {") < along.index("useShelf(shelfId, false);") < along.index("compute();")  # the one showing too


def test_a_file_named_must_be_the_titles_own(server):
    with logged_in(server, "rory") as browser:
        novel = next(i for i in serve_items(server) if i["id"] == "h:novel")
        place(browser, at=1, pct=0.1, file=novel["f"][0][0])
        assert people.Person("rory").latest()["h:novel"]["file"] == novel["f"][0][0]
        place(browser, at=2, pct=0.1, file="../../secret.txt")
        assert people.Person("rory").latest()["h:novel"]["file"] == ""


def serve_items(srv):
    return srv.RequestHandlerClass.catalog.get()


def test_a_newer_format_answers_so_and_changes_nothing(server):
    with logged_in(server, "rory") as browser:
        assert place(browser, at=1, pct=0.1).status_code == 200
        personal = browser.get("/api/session").json()["me"]["key"]
        newer_database()
        before = db.file().read_bytes()
        answer = place(browser, personal, at=2, pct=0.2)
        assert answer.status_code in (401, 403)  # nobody is known to a database of a newer version
        assert db.file().read_bytes() == before


def test_the_nightly_copy_isnt_served(server, library, log_in):
    assert place_for(server, "rory").status_code == 200
    copy = db.nightly(library, datetime(2026, 11, 5, 4))
    with logged_in(server, "owen") as owner, log_in(httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}"), name="owen") as pc:
        for client in (owner, pc):
            for inside in ("hylly.db", "place.jsonl", "check.json"):
                assert client.get(f"/state/db-backup/{copy.name}/{inside}").status_code in (403, 404)


def place_for(srv, name):
    with logged_in(srv, name) as browser:
        return place(browser)


def test_the_new_files_are_private_where_a_disk_can_say_so(person):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("Windows goes by the folder's rights")
    person.place(TITLE, 12, 0.25)
    wal = db.file().with_name("hylly.db-wal")
    assert stat.S_IMODE(db.file().stat().st_mode) == 0o600 and wal.exists() and stat.S_IMODE(wal.stat().st_mode) == 0o600


def test_a_list_goes_in_the_reading_apps_and_one_of_all_is_featured(person):
    person.name_list("l1", "Picks")
    person.change_list("l1", "add", TITLE)
    assert person.show_list("l1", True) and not person.show_list("l1", True)
    assert again().list_names() == [{"id": "l1", "name": "Picks", "apps": True}]
    owen = people.Person("owen")
    owen.name_list("o1", "November")
    owen.change_list("o1", "add", OTHER)
    assert owen.feature_list("o1", True)
    assert owen.list_names() == [{"id": "o1", "name": "November", "apps": True, "featured": True}]  # Featured is ticked for the apps too
    assert person.feature_list("l1", True)  # one for the library: it moves
    assert owen.list_names() == [{"id": "o1", "name": "November", "apps": True}]  # and the one who had it knows at their next look
    assert [(shelf["by"], shelf["id"], shelf["featured"]) for shelf in people.in_the_apps()] == [("rory", "l1", True), ("owen", "o1", False)]
    assert [e["id"] for e in people.in_the_apps()[0]["entries"]] == [TITLE["id"]]
    assert person.show_list("l1", False) and again().list_names() == [{"id": "l1", "name": "Picks"}]  # off: no longer Featured either
    assert [shelf["id"] for shelf in people.in_the_apps()] == ["o1"]
    owen.delete_list("o1")
    assert people.in_the_apps() == []
    owen.restore_list("o1")
    assert [shelf["id"] for shelf in people.in_the_apps()] == ["o1"]  # back, ticked as it was
    for bad in (lambda: person.show_list("reading", True), lambda: person.feature_list("nope", True)):
        with pytest.raises(people.NoSuchList):
            bad()
    assert again().export()["in_reading_apps"] == [] and people.Person("owen").export()["in_reading_apps"] == ["o1"]
    with db.database().read() as c:
        assert c.execute("SELECT count(*) FROM list WHERE featured = 1").fetchone()[0] == 0


def test_an_owner_s_ticked_lists_are_in_everyone_s_feed_with_featured_first(server, log_in):
    def send(browser, **body):
        key = browser.get("/api/session").json()["me"]["key"]
        return browser.post("/api/me/list", json=body, headers={"X-Personal-Key": key})
    with logged_in(server, "owen") as owen, logged_in(server, "rory") as rory:
        for who, list_id, name in ((owen, "o1", "Picks of the month"), (owen, "o2", "Not ticked"), (rory, "r1", "A reader's own")):
            assert send(who, list=list_id, op="name", name=name).json()["ok"] and send(who, list=list_id, op="add", id="h:novel").json()["ok"]
        assert send(rory, list="r1", op="apps", on=True).json()["changed"]  # kept, for a feed of her own later: not everyone's
        refused = send(rory, list="r1", op="feature", on=True)  # refused for good (409): a 403 is the key's, which the page waits on
        assert refused.status_code == 409 and refused.json()["error"] == "Only an owner picks the library's Featured list."
        for bad, status in (({"list": "o1", "op": "apps", "on": "yes"}, 400), ({"list": "reading", "op": "apps", "on": True}, 400),
                            ({"list": "nope", "op": "apps", "on": True}, 404)):
            assert send(owen, **bad).status_code == status, bad
        assert send(owen, list="o1", op="feature", on=True).json()["changed"]
        assert owen.get("/api/me/lists").json()["names"][0] == {"id": "o1", "name": "Picks of the month", "apps": True, "featured": True}
    with log_in(httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME), "reader") as home:
        start = home.get("/opds").text
        assert start.index("<title>Featured</title>") < start.index("<title>Recently added</title>")
        assert "Picks of the month: 1 title" in start and 'rel="http://opds-spec.org/featured"' in start
        assert "own</title>" not in start and "Not ticked" not in start  # a reader's, and one not ticked
        shown = home.get(re.search(r'href="(/opds/list/[^"]+)"', start)[1])
        assert shown.status_code == 200 and "Picks of the month (1)" in shown.text and "urn:hylly:h:novel" in shown.text
        assert home.get("/opds/list/999-nope").status_code == 404


def test_every_feed_is_your_own_and_starts_with_what_s_yours_under_either_address(server, log_in):
    """With every request logged in, /opds and /ereader/opds start with what's the person's own, and /me/opds and
    /me/ereader/opds, the addresses only those had before, are the same feeds: apps set up with either keep working."""
    base = f"http://127.0.0.1:{server.server_port}"
    with httpx.Client(base_url=base, headers=HOME) as home:
        for path in ("/opds", "/me/opds", "/ereader/opds", "/me/ereader/opds"):
            asked = home.get(path)
            assert asked.status_code == 401 and asked.headers["WWW-Authenticate"].startswith("Basic"), path  # at home too
        assert home.get("/me/opds", auth=("rory", "wrong")).status_code == 401
    with logged_in(server, "rory") as rory:
        key = {"X-Personal-Key": rory.get("/api/session").json()["me"]["key"]}
        assert rory.post("/api/me/place", json={"id": "h:novel", "at": 12, "pct": 0.25}, headers=key).json()["ok"]
        for body in ({"list": "r1", "op": "name", "name": "Rainy days"}, {"list": "r1", "op": "add", "id": "h:novel"},
                     {"list": "r1", "op": "apps", "on": True}, {"list": "reading", "op": "add", "id": "h:novel"}):
            assert rory.post("/api/me/list", json=body, headers=key).json()["ok"], body
    with httpx.Client(base_url=base, headers=HOME, auth=("rory", "correct horse")) as app:
        for feed in ("/opds", "/me/opds", "/ereader/opds", "/me/ereader/opds"):
            start = app.get(feed).text
            assert (start.index("<title>Continue reading</title>") < start.index("<title>Reading list</title>") < start.index("<title>Rainy days</title>")
                    < start.index("<title>Recently added</title>")) and "1 title you're in the middle of" in start, feed
            assert f'href="{feed}/continue"' in start and f'href="{feed}/mine/r1"' in start, feed  # links stay under the address asked
            for path in ("/continue", "/reading", "/mine/r1"):
                assert "urn:hylly:h:novel" in app.get(feed + path).text, feed + path
    with httpx.Client(base_url=base, headers=HOME, auth=("owen", "correct horse")) as other:
        for feed in ("/opds", "/me/opds"):
            theirs = other.get(feed).text
            assert "Rainy days" not in theirs and "0 titles you're in the middle of" in theirs  # rory's are hers alone
            assert other.get(feed + "/mine/r1").status_code == 404
    with httpx.Client(base_url=base, headers=HOME, auth=("bea", "correct horse")) as borrower:
        assert borrower.get("/me/opds").status_code == 403 and borrower.get("/opds").status_code == 403  # a borrower has no feeds


def test_shelves_ticked_for_the_apps_are_feeds_for_everyone_or_their_owner(server, log_in):
    base = f"http://127.0.0.1:{server.server_port}"
    with logged_in(server, "owen") as owen, logged_in(server, "rory") as rory:
        assert sharing(owen, op="set", shelf={"id": "s1", "name": "From Humble", "show": {"src": ["humble"]}, "hide": {}, "apps": True}).json()["ok"]
        assert sharing(owen, op="set", shelf={"id": "s2", "name": "Not ticked", "show": {"src": ["humble"]}, "hide": {}}).json()["ok"]
        assert shelving(rory, op="set", shelf={"id": "m7", "name": "My started ones", "show": {"rd": ["started"]}, "hide": {}, "apps": True}).json()["changed"]
        key = {"X-Personal-Key": rory.get("/api/session").json()["me"]["key"]}
        assert rory.post("/api/me/place", json={"id": "h:novel", "at": 12, "pct": 0.25}, headers=key).json()["ok"]
        assert rory.get("/api/shelves").json()["mine"][0]["apps"] is True
    with log_in(httpx.Client(base_url=base, headers=HOME), "reader") as home:
        start = home.get("/opds").text
        assert "<title>From Humble</title>" in start and "Not ticked" not in start and "My started ones" not in start
        shelf = home.get("/opds/shelf/s1")
        assert shelf.status_code == 200 and "From Humble" in shelf.text and "urn:hylly:h:novel" in home.get("/opds/shelf/s1/src/humble").text
        assert "urn:hylly:s:7" not in home.get("/opds/shelf/s1/search?q=a").text  # only what's on it, a search too
        assert home.get("/opds/shelf/m7").status_code == 404  # hers isn't everyone's
    with httpx.Client(base_url=base, headers=HOME, auth=("rory", "correct horse")) as app:
        mine = app.get("/me/opds").text
        assert "<title>My started ones</title>" in mine and "<title>From Humble</title>" in mine
        assert "My started ones (1)" in app.get("/opds/shelf/m7").text  # hers, at the address everyone's feed has too
        assert "My started ones (1)" in app.get("/me/opds/shelf/m7").text  # it goes by her reading: the one she's started
        assert "urn:hylly:h:novel" in app.get("/me/opds/shelf/m7/search?q=novel").text  # its search too
        assert "urn:hylly:h:lwolf1" in app.get("/me/opds/search?q=wolf").text and "urn:hylly:h:lwolf1" not in app.get("/me/opds/shelf/m7/search?q=wolf").text
    with httpx.Client(base_url=base, headers=HOME, auth=("owen", "correct horse")) as other:
        assert "My started ones" not in other.get("/me/opds").text


def test_best_rated_goes_by_everyone_s_stars_and_most_read_by_how_many_have_read_in_it(server, log_in):
    from hylly import paths, settings
    paths.settings_file().write_text('[reviews]\nwho = "reader"\n', encoding="utf-8")
    settings.reload()  # read afresh, as the server finds them changed
    base = f"http://127.0.0.1:{server.server_port}"
    for name, stars in (("rory", 5), ("owen", 4)):
        with logged_in(server, name) as browser:
            key = {"X-Personal-Key": browser.get("/api/session").json()["me"]["key"]}
            assert browser.post("/api/me/stars", json={"id": "h:novel", "stars": stars}, headers=key).json()["ok"]
            assert browser.post("/api/me/place", json={"id": "h:novel", "at": 12, "pct": 0.25}, headers=key).json()["ok"]
            if name == "rory":  # hers alone: not shown to everyone
                assert browser.post("/api/me/place", json={"id": "h:lwolf1", "at": 2, "pct": 0.1}, headers=key).json()["ok"]
    with log_in(httpx.Client(base_url=base, headers=HOME), "reader") as home:
        start = home.get("/opds").text
        assert "1 title with stars, the best first" in start and "1 title that most of you have read in" in start
        assert "urn:hylly:h:novel" in home.get("/opds/best").text
        popular = home.get("/opds/popular").text
        assert "urn:hylly:h:novel" in popular and "urn:hylly:h:lwolf1" not in popular
        assert "rory" not in popular and "owen" not in popular  # counts only
    paths.settings_file().write_text('[reviews]\nwho = "off"\n', encoding="utf-8")
    settings.reload()
    with log_in(httpx.Client(base_url=base, headers=HOME), "reader") as home:
        assert "Best rated" not in home.get("/opds").text and "Most read" in home.get("/opds").text  # stars off: no Best rated


def test_each_person_s_key_is_their_own_so_a_page_left_open_for_one_never_writes_as_another(server, library):
    """The key for one's own writes was one for the whole server: a page left open for rory, with lucy logged in since
    in that browser, sent rory's waiting changes with lucy's login, and they were kept as lucy's. Each person's key is
    their own now, their name signed with the server's secret: rory's page gets 403 (and, asking /api/session, finds
    lucy there and starts afresh as hers), and nothing is kept as anyone's. A restart makes every key anew."""
    with logged_in(server, "rory") as browser:
        rory = browser.get("/api/session").json()["me"]["key"]
        assert browser.get("/api/session").json()["me"]["key"] == rory  # the same all through this start
        assert browser.post("/login", data={"username": "lucy", "password": "correct horse", "next": "/"}).status_code == 303
        session = browser.get("/api/session").json()  # lucy, in the same browser, as in another tab
        assert session["by"] == "lucy" and session["me"]["key"] != rory
        assert place(browser, rory, at=5).status_code == 403  # rory's page, with lucy's login
        assert mark(browser, {"id": "h:novel", "mark": "finished"}, rory).status_code == 403
        assert people.Person("lucy").latest() == {} and people.Person("rory").latest() == {}
        assert place(browser, session["me"]["key"], at=6).status_code == 200  # lucy's own page
    again, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=again.serve_forever, daemon=True).start()
    try:
        with logged_in(again, "rory") as browser:
            assert browser.get("/api/session").json()["me"]["key"] != rory
    finally:
        again.shutdown()


def test_the_shelves_come_with_the_limits_a_shelf_is_checked_by(server):
    with logged_in(server, "rory") as browser:
        assert browser.get("/api/shelves").json()["limits"] == shelves.limits() == {
            "shelves": shelves.MAX_SHELVES, "name": shelves.NAME_MAX, "rows": shelves.MAX_ROWS, "values": shelves.MAX_VALUES,
            "value": shelves.VALUE_MAX}


# ---- the page's outbox: what waits for the network, and its one sender ---------------------------


def test_changes_waiting_in_a_browser_from_the_page_before_still_go_each_where_it_did():
    """The sender picks a change's address and body from one table now, by how its key starts, and not from two
    nine-way chains. Keys and changes stay as they were, so what waits in a browser from the page before goes as it
    would have: one of each kind, written as that page wrote them, goes in order to the address it went to, with the
    body it had, and the outbox is empty after."""
    out = outbox_in_node("""
const T = Date.now() - 60000;
put({
  "h:novel": { at: 12, pct: 0.25, file: "humble/b/novel.epub", of: 48, note: "Chapter 3", t: T },
  "s:7": { at: "epubcfi(/6/4!/4/2/1:0)", pct: 0.4, file: "storybundle/b/another.epub", note: "Two", t: T },
  "mark:h:novel": { mark: "finished", t: T },
  "new-seen": { upto: "2026-11-18T08:00:00", t: T },
  "list:1763400000000:1": { op: "add", id: "h:novel", before: null, list: "reading", t: T },
  "list:1763400000000:2": { op: "move", id: "h:novel", before: "s:7", list: "l1", t: T },
  "list:1763400000000:3": { op: "name", list: "l1", name: "Picks", t: T },
  "list:1763400000000:4": { op: "feature", list: "l1", on: true, t: T },
  "list:1763400000000:5": { op: "restore", list: "l1", was: { id: "l1", name: "Picks", rows: [] }, t: T },
  "bm:1763400000000:1": { change: { op: "add", key: "k1", id: "h:novel", why: "useful", at: 12, pct: 0.25 }, t: T },
  "shelf:1763400000000:1": { change: { op: "set", shelf: { id: "m1", name: "No games", show: {}, hide: { src: ["itch"] } } }, t: T },
  "guide:book": { t: T },
  "stars:h:novel": { n: 4, t: T },
  "look": { id: "yours", own: { base: "hylly", light: { bg: "#fbfaf6" }, dark: {} }, t: T },
});
sendPlaces().then(() => process.stdout.write(JSON.stringify({ sent: SENT, left: box(), said })));
""")
    ages = [body.pop("age") for _, body, _, _ in out["sent"] if "age" in body]
    assert len(ages) == 4 and all(59 < age < 120 for age in ages)  # the two places, the mark and the bookmark: how long they waited
    assert [[url, body] for url, body, _, _ in out["sent"]] == [
        ["/api/me/place", {"id": "h:novel", "at": 12, "pct": 0.25, "file": "humble/b/novel.epub", "of": 48, "note": "Chapter 3"}],
        ["/api/me/place", {"id": "s:7", "at": "epubcfi(/6/4!/4/2/1:0)", "pct": 0.4, "file": "storybundle/b/another.epub", "note": "Two"}],
        ["/api/me/mark", {"id": "h:novel", "mark": "finished"}],
        ["/api/me/new-seen", {"upto": "2026-11-18T08:00:00"}],
        ["/api/me/list", {"list": "reading", "op": "add", "id": "h:novel", "before": None}],
        ["/api/me/list", {"list": "l1", "op": "move", "id": "h:novel", "before": "s:7"}],
        ["/api/me/list", {"list": "l1", "op": "name", "before": None, "name": "Picks"}],
        ["/api/me/list", {"list": "l1", "op": "feature", "before": None, "on": True}],
        ["/api/me/list", {"list": "l1", "op": "restore", "before": None}],
        ["/api/me/bookmark", {"op": "add", "key": "k1", "id": "h:novel", "why": "useful", "at": 12, "pct": 0.25}],
        ["/api/me/shelf", {"op": "set", "shelf": {"id": "m1", "name": "No games", "show": {}, "hide": {"src": ["itch"]}}}],
        ["/api/me/guide-seen", {"kind": "book"}],
        ["/api/me/stars", {"id": "h:novel", "stars": 4}],
        ["/api/me/look", {"look": "yours", "own": {"base": "hylly", "light": {"bg": "#fbfaf6"}, "dark": {}}}],
    ]
    assert {(key, keepalive) for _, _, key, keepalive in out["sent"]} == {("k1", True)} and out["left"] == {} and out["said"] == []


def test_a_change_is_done_only_when_the_server_keeps_it_or_refuses_it_for_good_and_a_403_is_let_go_after_a_new_key():
    """A change waiting in the outbox was let go on any answer but 401, 403, 429 and 5xx: a 409 of a newer version, a
    421, an answer that wasn't the server's at all, and it was gone without a word. Now it's done only when the server
    kept it, or refused it for good in its own words (400, 404, 409); the rest waits, and holds those behind it, which
    keep their order. A 403 asks for this login's key and tries once more; still refused then, it's let go and said,
    rather than holding up the outbox for ever. Another login here since: nothing more is sent, and the page starts
    afresh as theirs."""
    out = outbox_in_node("""
const one = () => put({ "h:novel": { at: 3, pct: 0.1, file: "", t: Date.now() }, "mark:h:novel": { mark: "finished", t: Date.now() } });
const cases = [[200, { ok: true }], [400, { ok: false, error: "Which title?" }], [404, { ok: false, error: "No such title." }],
  [409, { ok: false, error: "A list holds 1000 titles." }], [409, "<html>a proxy's own page</html>"], [400, "bad request body"],
  [200, "<html>a hotel's Wi-Fi login</html>"], [401, { ok: false, error: "Log in, please." }],
  [429, { ok: false, error: "Too many changes in a minute: try again in 9 seconds." }], [500, "Something went wrong in the server."],
  [503, { ok: false, error: "What's kept for you is from a newer version of Hylly, and is left alone until this one is updated." }],
  [421, "This server doesn't answer to that name."], ["away"]];
(async () => {
  const out = { cases: [] };
  for (const [status, body] of cases) {
    one();
    SENT.length = 0;
    answer = url => status === "away" ? "away" : url === "/api/me/place" ? { status, body } : { status: 200, body: { ok: true } };
    await sendPlaces();
    out.cases.push([status, Object.keys(box()), SENT.length]);
  }
  for (const [name, replies, who] of [["renewed", [403, 200], "rory"], ["still refused", [403, 403], "rory"],
                                       ["someone else", [403], "lucy"], ["no answer", [403], "away"]]) {
    one();
    SENT.length = said.length = reloaded = 0;
    ME.key = "k1";
    let n = 0;
    answer = url => url !== "/api/me/place" ? { status: 200, body: { ok: true } }
      : replies[n++] === 403 ? { status: 403, body: { ok: false, error: "not allowed" } } : { status: 200, body: { ok: true } };
    session = () => who === "away" ? "away" : { by: who, me: { key: "k2" } };
    await sendPlaces();
    out[name] = { left: Object.keys(box()), keys: SENT.map(s => s[2]), said: [...said], reloaded };
  }
  process.stdout.write(JSON.stringify(out));
})();
""")
    done, waits = [], ["h:novel", "mark:h:novel"]
    assert out["cases"] == [
        [200, done, 2], [400, done, 2], [404, done, 2], [409, done, 2],  # kept, or refused for good: the mark goes after
        [409, waits, 1], [400, waits, 1], [200, waits, 1],  # not the server's own answer: it waits, and the mark behind it
        [401, waits, 1], [429, waits, 1], [500, waits, 1], [503, waits, 1], [421, waits, 1], ["away", waits, 1]]
    assert out["renewed"] == {"left": [], "keys": ["k1", "k2", "k2"], "said": [], "reloaded": 0}  # a restarted server's new key
    assert out["still refused"]["left"] == [] and out["still refused"]["keys"] == ["k1", "k2", "k2"]  # let go: the mark went on
    assert out["still refused"]["said"] == ["A change of yours wasn't saved: the library refused it from this page (not allowed)."]
    assert out["someone else"] == {"left": waits, "keys": ["k1"], "said": [], "reloaded": 1}  # nothing as lucy's; rory's wait here
    assert out["no answer"] == {"left": waits, "keys": ["k1"], "said": [], "reloaded": 0}


def test_a_change_taken_out_or_made_again_while_the_outbox_goes_isn_t_sent_as_it_was():
    """The sender went through a copy of the outbox taken as it started: a title's stars a review had just taken out (as
    the review's are newer) were still sent after it, and replaced the review's count. Each change is read again just
    before it goes now: one taken out isn't sent, and one made again since goes in a later round, as it is then."""
    out = outbox_in_node("""
put({ "h:novel": { at: 3, pct: 0.1, file: "", t: 1 }, "stars:h:novel": { n: 2, t: 2 }, "mark:h:novel": { mark: "finished", t: 3 } });
let release;
const gate = new Promise(done => { release = done; });
answer = async url => { if (url === "/api/me/place") await gate; return { status: 200, body: { ok: true } }; };
(async () => {
  const going = sendPlaces();
  await tick();  // the place is on its way
  withdraw("stars:h:novel");  // as a review does with its title's stars
  queue("mark:h:novel", { mark: "unfinished" }, false);  // Not yet, at once
  release();
  await going;
  const first = SENT.map(([url, body]) => [url, body.stars ?? body.mark ?? body.at]), left = Object.keys(box());
  await sendPlaces();
  process.stdout.write(JSON.stringify({ first, left, then: SENT.map(([url, body]) => [url, body.stars ?? body.mark ?? body.at]), after: box() }));
})();
""")
    assert out["first"] == [["/api/me/place", 3]] and out["left"] == ["mark:h:novel"]
    assert out["then"] == [["/api/me/place", 3], ["/api/me/mark", "unfinished"]] and out["after"] == {}  # never the earlier mark


def test_with_storage_refusing_writes_changes_still_go_while_the_page_is_open_and_that_is_said_once():
    """The outbox lived only in this browser's storage: one that refused writes (full, or turned off) kept nothing, and a
    logged-in reader's places, marks and list changes were never sent while the page showed them saved. The page keeps
    the outbox too now: they go while it's open, and it says once that this browser can't keep them for later."""
    out = outbox_in_node("""
(async () => {
  const out = {};
  for (const how of ["full", "off"]) {
    STORE.clear();
    SENT.length = said.length = 0;
    REFUSE = how === "full";
    OFF = how === "off";
    queue("h:novel", { at: 5, pct: 0.2, file: "" }, false);
    queue("mark:h:novel", { mark: "finished" }, false);
    const held = Object.keys(outbox());
    queue("list:1:1", { op: "add", id: "h:novel", before: null, list: "reading" }, false);
    await sendPlaces();
    out[how] = { held, sent: SENT.map(s => s[0]), said: said.length, kept: STORE.size, left: Object.keys(outbox()) };
    OUTBOX.said = false;  // a page of its own each time
  }
  process.stdout.write(JSON.stringify(out));
})();
""")
    for how in ("full", "off"):
        assert out[how] == {"held": ["h:novel", "mark:h:novel"], "sent": ["/api/me/place", "/api/me/mark", "/api/me/list"],
                            "said": 1, "kept": 0, "left": []}, how


def test_starting_over_from_another_device_takes_the_place_kept_under_any_of_the_title_s_ids():
    """A start-over that came from the server took away only the place kept under the title's id: one kept under a copy's
    id, from when that copy was the title's lead, was found again (placeKept looks under every id), and reading on sent it
    as the newest place, undoing the start-over everywhere. One clearLocalPlace takes it from under every id, as Start
    over on this device does."""
    out = outbox_in_node("""
const BOOKS = ["epub"], NOFILES = false, readFile = () => ["humble/b/novel.epub", "epub", 1];
/* part 5 */
/* part 6 */
const it = { id: "h:novel", ids: ["h:old"] };
STORE.set("hylly.book.h:old", JSON.stringify({ cfi: "epubcfi(/6/8)", pct: 0.6, ch: "Old" }));  // when the copy was the lead
STORE.set("hylly.book.h:novel", JSON.stringify({ cfi: "epubcfi(/6/4)", pct: 0.3, ch: "" }));
placeHere(it, { at: 0, pct: 0, time: 100 });  // started over on another device
process.stdout.write(JSON.stringify({ book: bookAt(it), left: [...STORE.keys()].filter(k => k.startsWith("hylly.book.")) }));
""", (r"const extOf = ", r"// ---- Your place on every device"),
        (r"function ownPlace\(", r"function showNote\("))
    assert out == {"book": None, "left": []}


def test_a_title_s_place_in_this_browser_is_read_and_kept_in_one_shape_whatever_asks():
    """A title's place in this browser (a page as "page/pages", a book's spot as {cfi, pct, ch}) was read and reshaped
    in five spots, each a little differently. localPlace gives it in the server's shape, with what's said of it, and
    keepLocalPlace keeps one; the differences each asker had are still theirs: the way back has a page by its number
    alone (pct 0), a page's first page is no place, and a book's how far alone is a place, though not one offered at a
    first login, which wants its spot."""
    out = outbox_in_node("""
const BOOKS = ["epub"], file = { "h:novel": ["humble/b/novel.epub", "epub", 1], "s:7": ["Comics/s7.cbz", "cbz", 1] };
let NOFILES = false;
const readFile = it => file[it.id] || null;
/* part 5 */
/* part 6 */
const novel = { id: "h:novel", ids: ["h:old"] }, comic = { id: "s:7" }, out = {};
const at = (it, what, value) => { STORE.clear(); if (value !== null) STORE.set(`hylly.${what}.${it.id}`, value); return [localPlace(it), ownPlace(it)]; };
out.page = at(comic, "at", "11/48");
out.first = at(comic, "at", "0/48");
out.uncounted = at(comic, "at", "4/");
out.book = at(novel, "book", JSON.stringify({ cfi: "epubcfi(/6/4)", pct: 0.25, ch: "Two" }));
out.howFar = at(novel, "book", JSON.stringify({ cfi: "", pct: 0.4, ch: "" }));
out.none = at(novel, "book", "{}");
STORE.clear();
STORE.set("hylly.book.h:old", JSON.stringify({ cfi: "epubcfi(/6/8)", pct: 0.6, ch: "Old" }));  // under a copy's id
out.copy = localPlace(novel);
STORE.clear();
keepLocalPlace(comic, { at: 7, of: 30, pct: 0.26 });
keepLocalPlace(novel, { at: "epubcfi(/6/2)", pct: 0.1, note: "One" });
keepLocalPlace(novel, { at: "epubcfi(/6/2)", pct: 0.1 });
out.kept = Object.fromEntries(STORE);
NOFILES = true;  // a borrower: the e-book as pages, which it has none of here
STORE.set("hylly.at.h:novel", "3/9");
out.borrower = [localPlace(novel), localPlace(novel, true)];  // as it's read here, and as a book's, as a first login offers it
process.stdout.write(JSON.stringify(out));
""", (r"const extOf = ", r"// ---- Your place on every device"), (r"function ownPlace\(", r"function placeHere\("))
    cbz, epub = "Comics/s7.cbz", "humble/b/novel.epub"
    assert out["page"] == [{"at": 11, "pct": 0.25, "of": 48, "file": cbz, "what": "Page 12 of 48"}, {"at": 11, "pct": 0}]
    assert out["first"] == [None, None]  # the first page is the start
    assert out["uncounted"][0] == {"at": 4, "pct": 0, "of": 0, "file": cbz, "what": "Page 5"}
    assert out["book"] == [{"at": "epubcfi(/6/4)", "pct": 0.25, "note": "Two", "file": epub, "what": "25% · Two"},
                           {"at": "epubcfi(/6/4)", "pct": 0.25}]
    assert out["howFar"][0] == {"at": "", "pct": 0.4, "note": "", "file": epub, "what": "40%"} and out["none"] == [None, None]
    assert out["copy"]["at"] == "epubcfi(/6/8)"
    assert out["kept"] == {"hylly.at.s:7": "7/30", "hylly.book.h:novel": '{"cfi":"epubcfi(/6/2)","pct":0.1,"ch":""}'}
    assert out["borrower"] == [{"at": 3, "pct": 4 / 9, "of": 9, "file": epub, "what": "Page 4 of 9"},  # read as pages now
                               {"at": "epubcfi(/6/2)", "pct": 0.1, "note": "", "file": epub, "what": "10%"}]  # its book's, asked for


def test_start_over_s_undo_and_a_first_login_bring_back_and_offer_the_place_as_it_was_kept():
    """Start over's Undo put back what this browser kept and sent it as the latest place, and a first login here offered
    what was kept for titles the server had nothing of: each read and reshaped it on its own. Both go by localPlace
    now, as kept: a page with how far it is, a book's spot with its chapter; a book's how far alone isn't offered."""
    import helpers

    page = helpers.OUTBOX_PAGE.replace("const toast = text => said.push(text), sayLater = toast;",
                                       "const undos = [], toast = (text, undo) => { said.push(text); if (undo) undos.push(undo); }, sayLater = toast;")
    out = helpers.in_the_page(page + """
const BOOKS = ["epub"], NOFILES = false, MY = new Map(), placesKnown = () => {}, readRowAgain = () => {};
const file = { "h:novel": ["humble/b/novel.epub", "epub", 1], "s:7": ["Comics/s7.cbz", "cbz", 1] }, readFile = it => file[it.id] || null;
/* part 5 */
/* part 6 */
/* part 7 */
const out = {}, novel = ITEMS[0], comic = ITEMS[1], sent = () => Object.fromEntries(Object.entries(box()).map(([k, p]) => [k, { ...p, t: 0 }]));
STORE.set("hylly.at.s:7", "11/48");
markHere(comic, "over");
out.over = [STORE.get("hylly.at.s:7") || null, said.splice(0)];
undos.pop()();
out.undone = [STORE.get("hylly.at.s:7"), sent()["s:7"]];
STORE.clear();
STORE.set("hylly.book.h:old", JSON.stringify({ cfi: "epubcfi(/6/4)", pct: 0.3, ch: "Two" }));  // under a copy's id
markHere(novel, "over");
undos.pop()();
out.book = [STORE.get("hylly.book.h:novel"), STORE.get("hylly.book.h:old") || null, sent()["h:novel"]];
STORE.clear();
STORE.set("hylly.at.s:7", "3/10");
STORE.set("hylly.book.h:novel", JSON.stringify({ cfi: "", pct: 0.5, ch: "" }));  // how far alone: not offered
offerOldPlaces({});
out.offered = sent();
STORE.set("hylly.offered.rory", "false");
STORE.set("hylly.book.h:novel", JSON.stringify({ cfi: "epubcfi(/6/6)", pct: 0.5, ch: "Three" }));
put({});
offerOldPlaces({ "s:7": { at: 9 } });  // what the server has already: not offered again
out.again = sent();
process.stdout.write(JSON.stringify(out));
""", (r"const PLACE_WAIT = ", r"// The outbox: your changes"), (r"const OUTBOX = ", r"function placeMoved\("),
        (r"// One change to the server\.", r"function ownPlace\("), (r"// The session as the server has it now", r"async function keyRenewed\("),
        (r"const extOf = ", r"// ---- Your place on every device"), (r"function markHere\(", r"// ---- Stars: "),
        (r"function offerOldPlaces\(", r"async function startPlaces\("))
    assert out["over"] == [None, ["Back to the start."]]
    assert out["undone"] == ["11/48", {"at": 11, "pct": 0.25, "of": 48, "file": "Comics/s7.cbz", "t": 0}]
    assert out["book"] == ['{"cfi":"epubcfi(/6/4)","pct":0.3,"ch":"Two"}', None,
                           {"at": "epubcfi(/6/4)", "pct": 0.3, "note": "Two", "file": "humble/b/novel.epub", "t": 0}]
    assert out["offered"] == {"s:7": {"at": 3, "pct": 0.4, "of": 10, "file": "Comics/s7.cbz", "t": 0}}
    assert out["again"] == {"h:novel": {"at": "epubcfi(/6/6)", "pct": 0.5, "note": "Three", "file": "humble/b/novel.epub", "t": 0}}


def test_save_checks_a_shelf_by_the_server_s_limits_before_anything_goes_or_is_deleted():
    """The page checked some of the server's limits, as numbers of its own: two rows of one field could together hold more
    values than a row may, and making a shelf for everyone your own while you had as many as can be kept skipped the
    count. The shelf for everyone was then deleted for everyone first, and the personal one refused and gone. Save checks
    the limits that come with the shelves now (shelves.limits), before anything is sent or deleted."""
    out = outbox_in_node("""
const SHELF_FIELDS = { k: "Kind", a: "Author" };
let SHELVES = { mine: Array.from({ length: 50 }, (_, i) => ({ id: `m${i}` })), limits: null };
/* part 5 */
const shelf = (show = {}) => ({ id: "m99", name: "Mine", show, hide: {} });
const many = Array.from({ length: 51 }, (_, i) => `Ann Writer ${i}`);
const out = { unknown: [shelfUnfit(shelf({ a: many }), true)] };
SHELVES.limits = { shelves: 50, name: 40, rows: 16, values: 50, value: 100 };
out.known = [shelfUnfit(shelf(), true), shelfUnfit(shelf(), false), shelfUnfit(shelf({ a: many }), false),
             shelfUnfit(shelf({ a: ["x".repeat(101)] }), false), shelfUnfit(shelf({ a: many.slice(0, 50), k: ["comic"] }), false)];
process.stdout.write(JSON.stringify(out));
""", (r"// What the server would refuse of a shelf", r'\$\("shelfaddshow"\)\.onclick'))
    assert out["unknown"] == [""]  # not known yet: the server's word alone
    assert out["known"] == ["You have 50 shelves, as many as can be kept. Delete one to save another.", "",
                            "A row has at most 50 values, and Author has 51 here: take some off.",
                            f"A value has at most 100 characters: “{'x' * 30}…” has more.", ""]
    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    save = page[page.index('$("shelfok").onclick'):page.index('$("shelfdelete").onclick')]
    assert save.index("shelfUnfit(shelf, !share && (!was || wasShared))") < save.index('sharedChange({ op: "delete", id: was.id })')


def test_a_change_too_big_to_go_as_the_page_closes_goes_while_it_s_open_and_holds_up_nothing():
    """Every change went with keepalive, which browsers refuse past 64 KB as if there were no network: a shelf that big
    was tried for ever and held up all behind it. One over 60 KB goes without keepalive now; the rest as before."""
    out = outbox_in_node("""
const many = Array.from({ length: 16 }, (_, i) => [`a${i}`, Array.from({ length: 50 }, (_, j) => `Ann Writer ${i} ${j} `.padEnd(100, "x"))]);
put({ "shelf:1:1": { change: { op: "set", shelf: { id: "m1", name: "Big", show: Object.fromEntries(many), hide: {} } }, t: 1 },
      "mark:h:novel": { mark: "finished", t: 2 } });
sendPlaces().then(() => process.stdout.write(JSON.stringify({ sent: SENT.map(s => [s[0], s[3]]), left: box() })));
""")
    assert out == {"sent": [["/api/me/shelf", False], ["/api/me/mark", True]], "left": {}}


def test_back_on_screen_the_page_asks_who_it_is_for_before_anything_of_someone_else_s_is_drawn_or_kept():
    """A page left open for rory, with lucy logged in since in that browser, drew lucy's places, lists, stars and shelves
    when it came back on screen, and kept her shelves in this browser as rory's, until rory changed something. Coming back
    asks the session first now: someone else's, and the page starts afresh as theirs, with nothing asked or drawn first;
    the same person's, and everything is asked again as before, no more often than every SYNC_EVERY."""
    out = outbox_in_node("""
const SYNC_EVERY = 20000, asked = [];
for (const name of ["syncPlaces", "syncStars", "refreshRevWaiting", "syncReported", "checkCatalog", "showNewOnce"]) globalThis[name] = () => asked.push(name);
const document = { addEventListener() {}, visibilityState: "visible" }, addEventListener = () => {};
/* part 5 */
(async () => {
  const out = {};
  session = () => ({ by: "rory", me: { key: "k1" } });
  await backOnScreen();
  out.same = [asked.splice(0), reloaded];
  await backOnScreen();  // again at once: no more often than every SYNC_EVERY
  out.soon = [asked.splice(0), reloaded];
  backAsked = -Infinity;
  session = () => ({ by: "lucy", me: { key: "k9" } });
  await backOnScreen();
  out.other = [asked.splice(0), reloaded, leaving];
  process.stdout.write(JSON.stringify(out));
})();
""", (r"let backAsked = ", r'document\.addEventListener\("visibilitychange", \(\) => document\.visibilityState'))
    assert out["same"] == [["syncPlaces", "syncStars", "refreshRevWaiting", "syncReported", "checkCatalog", "showNewOnce"], 0]
    assert out["soon"] == [[], 0]
    assert out["other"] == [[], 1, True]  # started afresh as lucy's, before any of hers was asked for


def test_log_out_sends_what_waits_then_this_browser_forgets_where_you_were_and_your_shelves():
    """Log out was a plain form: the places, the books' spots and the ways back stayed in the browser, and the library
    page showed "Continue: 40%" to whoever came next, on a family tablet where it opens without a login; so did the
    person's shelves and who was here last. Now what waits goes to the library first, then this browser forgets every
    place it keeps (each one opened while someone is logged in is theirs, turned or not), what was seen, the shelves and
    who was here last, and the form goes. A change still waiting then stays under that person's name alone, for their
    next login here; another's stays theirs, and this browser's own choices stay. The account menu closes and the page
    says "Logging out…" at once, as sending can take a few seconds, and an answer that comes after (a shelf sync, a place's
    time) writes nothing of theirs back. The library out of reach: nothing is forgotten, the page says so, and Log out
    can be pressed again."""
    def run(case: str) -> dict:
        return outbox_in_node("""
const handlers = {}, els = { logoutform: { addEventListener: (kind, f) => { handlers[kind] = f; } } }, $ = id => els[id];
const NET = { down: false }, netCheck = async () => { NET.down = ONLINE === false; };
let ONLINE = true, submitted = 0, prevented = 0, events = [];
const form = { submit: () => { events.push("submitted"); submitted++; } }, closeMenus = () => events.push("menus closed");
const keys = () => [...STORE.keys()].sort();
/* part 5 */
for (const [k, v] of Object.entries({ "hylly.at.h:novel": "12/48", "hylly.book.s:7": "{}", "hylly.back.h:novel": "{}", "hylly.at.h:opened": "3/10",
  "hylly.seen.rory": '{"h:novel": 1}', "hylly.shelfcache": '{"by": "rory"}', "hylly.me": '"rory"', "hylly.outbox.lucy": '{"h:x": {"t": 1}}',
  "hylly.theme": "dark", "hylly.bookstyle": "{}" })) STORE.set(k, v);
put({ "s:7": { at: "epubcfi(/6/4!/4/2/1:0)", pct: 0.4, file: "", t: Date.now() } });
const sent = () => SENT.map(([url]) => url);
(async () => {
""" + case + """
})();
""", (r"// Log out: this browser forgets", r'addEventListener\("online"'))

    out = run("""
  answer = url => { events.push(`sent ${url}`); return { status: 200, body: { ok: true, time: 5 } }; };
  handlers.submit({ preventDefault: () => prevented++, target: form });
  const atOnce = [...said];
  for (let i = 0; i < 20 && !submitted; i++) await tick();
  store("hylly.seen.rory", { "s:7": 9 }); store("hylly.shelfcache", { by: "rory" });  // answers that came after
  process.stdout.write(JSON.stringify({ events, atOnce, prevented, keys: keys(), leaving }));
""")
    assert out["events"] == ["menus closed", "sent /api/me/place", "submitted"] and out["prevented"] == 1 and out["leaving"]  # sent first, then the form
    assert out["atOnce"] == ["Logging out…"]
    assert out["keys"] == ["hylly.bookstyle", "hylly.outbox.lucy", "hylly.theme"]  # every place forgotten, the turned and the unturned alike
    out = run("""
  answer = () => ({ status: 503, body: "busy" });  // the library there, but too busy to keep it now
  handlers.submit({ preventDefault: () => prevented++, target: form });
  for (let i = 0; i < 20 && !submitted; i++) await tick();
  process.stdout.write(JSON.stringify({ submitted, keys: keys(), waiting: Object.keys(box()) }));
""")
    assert out == {"submitted": 1, "keys": ["hylly.bookstyle", "hylly.outbox.lucy", "hylly.outbox.rory", "hylly.theme"], "waiting": ["s:7"]}
    out = run("""
  ONLINE = false;
  answer = () => "away";
  handlers.submit({ preventDefault: () => prevented++, target: form });
  for (let i = 0; i < 20; i++) await tick();
  const first = { submitted, said: said.splice(0), kept: keys().length };
  ONLINE = true;
  answer = () => ({ status: 200, body: { ok: true } });
  handlers.submit({ preventDefault: () => prevented++, target: form });  // pressed again, the library back
  for (let i = 0; i < 20 && !submitted; i++) await tick();
  process.stdout.write(JSON.stringify({ first, then: submitted }));
""")
    assert out["first"] == {"submitted": 0, "said": ["Logging out…", "Not logged out: the library can't be reached. Try again once it can."], "kept": 11}  # nothing forgotten
    assert out["then"] == 1
    out = run("""
  ME = null;  // logged in by a password the browser keeps, with no records of their own here: the form as it is
  handlers.submit({ preventDefault: () => prevented++, target: form });
  process.stdout.write(JSON.stringify({ prevented, keys: keys().length }));
""")
    assert out == {"prevented": 0, "keys": 11}
    page = (Path(cli.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    button = re.search(r'<form id="logoutform"[^>]*><button class="acbtn" title="([^"]*)"', page)[1]
    assert "forgets the login, and what it kept for you: where you are in your books, and your shelves" in button
