"""Stars for titles: the rules as a table, tried with every state and event; each person's, as the database keeps them;
and what the server lets each role do, with stars on and off."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest

import httpx

from hylly import access, bell, matching, paths, people, reports, reviews, settings

from helpers import EVERY_ROLE, HOME, TITLE, again, changes, logged_in, newer_database, outbox_in_node, rows


@pytest.fixture
def users():
    return EVERY_ROLE  # the library fixture's: bea a borrower, rory a reader, lucy a librarian, owen an owner


def on_disk():
    """All the database has on the disk: its file, and its WAL."""
    from hylly import db

    return b"".join(p.read_bytes() for p in (db.file(), db.file().with_name("hylly.db-wal")) if p.exists())


def in_the_table():
    """Every review the database's table review holds, of everyone: the only place a review is kept."""
    from hylly import db

    with db.database().read() as c:
        return [json.loads(row[0]) for row in c.execute("SELECT body FROM review ORDER BY seq")]

RATE = {"op": "rate", "id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub", "stars": 4, "when": "2026-11-09T10:00:00+02:00", "time": 1.0}
STATES = (None, *dict.fromkeys(after for after in reviews.STEPS.values() if after))  # every state the table can lead to, and none
ALL_EVENTS = (*reviews.EVENTS, "sparkle", "", None)  # those it has, and some it hasn't
WRITE = {**RATE, "op": "write", "text": "Lovely to read aloud.", "spoiler": False}
SUBMIT = {**WRITE, "op": "submit"}
PATHS = {  # how a person and an owner get a review to each state
    None: [], reviews.RATED: [RATE], reviews.WAITING: [SUBMIT], reviews.SHOWN: [WRITE], reviews.EDIT_WAITING: [WRITE, SUBMIT],
    reviews.DECLINED: [SUBMIT, {"op": "decline", "id": "h:novel", "rev": 1, "note": "Not this"}],
    reviews.EDIT_DECLINED: [WRITE, SUBMIT, {"op": "decline", "id": "h:novel", "rev": 2, "note": "Not this"}],
}


def review_in(state):
    """A review of h:novel standing in this state, got to the way a person and an owner get there."""
    review = None
    for event in PATHS[state]:
        review = reviews.step(review, event)
    assert reviews.state_of(review) == state
    return review


# ---- the rules ----------------------------------------------------------------------------------

def test_every_state_is_reached():
    assert set(PATHS) == {None, *reviews.STATES}


@pytest.mark.parametrize("state", PATHS)
@pytest.mark.parametrize("op", ALL_EVENTS)
def test_every_state_with_every_event_does_what_the_table_says(state, op):
    before = review_in(state)
    kept = json.dumps(before)
    event = {**RATE, "op": op, "stars": 2, "text": "Changed words.", "spoiler": True, "rev": (before or {}).get("rev"), "note": "A note."}
    if (state, op) not in reviews.STEPS:
        with pytest.raises(reviews.Refused):
            reviews.step(before, event)
        assert json.dumps(before) == kept
        return
    after = reviews.step(before, event)
    assert reviews.state_of(after) == reviews.STEPS[state, op]
    assert json.dumps(before) == kept  # what it was given is left as it was
    if after is None:
        return
    words_before = (before or {}).get("text")
    if op == "rate":
        assert after["stars"] == 2 and after.get("text") == words_before
    elif op == "write":
        assert (after["text"], after["spoiler"], after["change"], after["note"]) == ("Changed words.", True, "", "")
    elif op == "submit" and after["state"] == reviews.EDIT_WAITING:
        assert after["text"] == words_before and after["change"] == "Changed words." and after["change_spoiler"]  # the shown words stay shown
    elif op == "submit":
        assert after["text"] == "Changed words." and after["rev"] == (before or {}).get("rev", 0) + 1
    elif op in ("unwrite", "remove"):
        assert after["stars"] == before["stars"] and not {"text", "change", "note"} & set(after)  # the words gone, the stars kept
    elif op == "release":
        assert after["text"] == (before["change"] if state == reviews.EDIT_WAITING else words_before) and not after["change"]
    elif op == "decline":
        assert after["note"] == "A note." and after["text"] == words_before


def test_the_table_only_leads_to_states_it_starts_from():
    starts = {state for state, _ in reviews.STEPS}
    assert all(after in starts for after in reviews.STEPS.values())  # nowhere to be stuck
    assert {op for _, op in reviews.STEPS} == set(reviews.EVENTS)
    assert set(reviews.SEEN) < set(reviews.WORDS) and set(reviews.OWNERS) < set(reviews.EVENTS)


@pytest.mark.parametrize("op", ["release", "decline"])
@pytest.mark.parametrize("state", [reviews.WAITING, reviews.EDIT_WAITING])
def test_words_that_changed_since_the_owner_saw_them_arent_released_or_declined(op, state):
    review = review_in(state)
    with pytest.raises(reviews.Refused, match="changed"):
        reviews.step(review, {"op": op, "id": "h:novel", "rev": review["rev"] - 1})
    with pytest.raises(reviews.Refused):
        reviews.step(review, {"op": op, "id": "h:novel"})


@pytest.mark.parametrize("stars", [0, 6, -1, 2.5, True, False, "4", None])
def test_only_whole_stars_from_one_to_five(stars):
    for op in ("rate", "write", "submit"):
        with pytest.raises(reviews.Refused):
            reviews.step(None, {**WRITE, "op": op, "stars": stars})
    with pytest.raises(reviews.Refused):
        reviews.step(review_in(reviews.RATED), {**RATE, "stars": stars})


@pytest.mark.parametrize("words", ["", "   ", None, 7, "x" * 1001])
def test_words_are_one_to_a_thousand_characters(words):
    with pytest.raises(reviews.Refused):
        reviews.step(None, {**WRITE, "text": words})


def test_what_others_see_and_what_the_writer_sees():
    for state in reviews.STATES:
        review = review_in(state)
        seen = reviews.public(review)
        assert seen["stars"] == 4 and ("text" in seen) == (state in reviews.SEEN)
        assert "change" not in seen and "note" not in seen
        assert reviews.own(review)["state"] == state
    assert reviews.public(review_in(reviews.EDIT_WAITING))["text"] == "Lovely to read aloud."  # the earlier words, while the change waits


@pytest.mark.parametrize("typed, kept", [
    ("  Good.  ", "Good."), ("One\r\nTwo\rThree", "One\nTwo\nThree"), ("A\n\n\n\n\nB", "A\n\nB"),
    ("bell\x07 and back\x08space", "bell and backspace"), ("evil\u202edcba", "evildcba"), ("zero\u200bwidth", "zerowidth"),
    ("<script>alert(1)</script>", "<script>alert(1)</script>"),  # kept as typed: the page shows text as text
])
def test_words_are_cleaned_not_changed(typed, kept):
    assert reviews.clean(typed) == kept


FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # three faces joined by U+200D: one emoji, five code points
PERSIAN = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"  # a Persian word, a zero-width non-joiner in it


@pytest.mark.parametrize("typed, kept", [
    (f"Read it to the {FAMILY}", f"Read it to the {FAMILY}"), (PERSIAN, PERSIAN),  # the joiners stay
    ("soft\u00adhyphen, \ue000 and \u200fa mark of direction", "soft\u00adhyphen, \ue000 and \u200fa mark of direction"),
    ("\ufeffturned \u2067round\u2069 \u202aand\u202c over\u202d", "turned round and over"),  # isolates, overrides, U+FEFF
    ("half \ud83d a letter", "half  a letter"),  # a lone surrogate, which JSON can carry and no database can keep
])
def test_the_one_cleaner_keeps_what_emoji_and_persian_need_and_takes_what_turns_text_round(typed, kept):
    """One cleaner for what people write, reviews' and reports' alike: the joiners emoji and Persian need stay, and so
    do a soft hyphen, a private character and a mark of direction alone; what overrides or isolates the direction of
    the text, the zero-width space, U+FEFF and half of a character go."""
    assert reviews.clean(typed) == kept and reviews.clean(typed).encode("utf-8")
    assert reports.clean(["else"], typed) == (["else"], kept)


def test_what_the_cleaner_takes_can_be_read_in_its_source():
    """Written with escapes: an invisible character in the code itself could be changed or lost without anyone
    seeing."""
    source = Path(reviews.__file__).read_text(encoding="utf-8")
    assert not [hex(ord(c)) for c in source
                if unicodedata.category(c) in ("Cf", "Co", "Cs") or (c != "\n" and unicodedata.category(c) == "Cc")]


def test_words_too_long_or_not_text_are_refused():
    with pytest.raises(reviews.Refused):
        reviews.clean("x" * 1001)
    with pytest.raises(reviews.Refused):
        reviews.clean(["a list"])
    assert reviews.clean("x" * 1000) == "x" * 1000


@pytest.mark.parametrize("moderation, role, waits", [
    ("off", "reader", False), ("on", "reader", True), ("on", "borrower", True), ("on", "librarian", True), ("on", "owner", False),
    ("borrowers", "borrower", True), ("borrowers", "reader", False), ("borrowers", "owner", False)])
def test_whose_words_wait(moderation, role, waits):
    assert reviews.waits(moderation, role) is waits


@pytest.mark.parametrize("who", reviews.WHO)
@pytest.mark.parametrize("role", access.ROLES)
def test_who_may_give_stars(who, role):
    lowest = {"off": None, "borrower": 0, "reader": 1, "librarian": 2, "owner": 3}[who]
    assert reviews.may_rate(who, role) is (lowest is not None and access.ROLES.index(role) >= lowest)
    assert not reviews.may_rate(who, "stranger")


def test_the_setting_and_the_rules_know_the_same_values():
    assert settings.CHOICES["reviews", "who"] == reviews.WHO and settings.DEFAULTS["reviews"]["who"] == "off"
    assert settings.CHOICES["reviews", "moderation"] == ("off", "on", "borrowers")  # the values waits() knows (above)
    assert settings.DEFAULTS["reviews"]["moderation"] == "off"


# ---- each person's ------------------------------------------------------------------------------

def test_stars_are_kept_changed_and_taken_back(person):
    assert person.rate(TITLE, 4)
    assert [(r["id"], r["stars"], r["state"]) for r in again().reviews()] == [("h:novel", 4, "rated")]
    assert again().reviews()[0]["t"] == "The Novel"  # what the title was, for finding it again
    before = changes()
    assert not person.rate(TITLE, 4) and changes() == before  # sent again: nothing written
    assert person.rate(TITLE, 2) and again().reviews()[0]["stars"] == 2
    assert person.rate(TITLE, 0) and again().reviews() == []
    before = changes()
    assert not person.rate(TITLE, 0) and changes() == before


def test_stars_follow_a_new_id_and_a_merged_titles_copies(person):
    person.rate(TITLE, 5)
    renamed = matching.Titles([{"id": "f:new", "t": "The Novel", "f": [["humble/b/novel.epub", "epub", 1]]}])
    assert again().reviews(renamed)[0]["id"] == "f:new"
    assert person.rate({"id": "f:new", "t": "The Novel", "f": "humble/b/novel.epub"}, 3, titles=renamed)
    assert [(r["id"], r["stars"]) for r in again().reviews(renamed)] == [("f:new", 3)]  # the same review, not a second
    gone = matching.Titles([])
    assert again().reviews(gone)[0].get("gone")
    person.rate(TITLE, 0, titles=renamed)
    person.rate({"id": "h:copy", "t": "The Novel"}, 4)
    assert person.rate({"id": "h:main", "t": "The Novel"}, 2, ids=["h:main", "h:copy"])
    assert [(r["id"], r["stars"]) for r in again().reviews()] == [("h:copy", 2)]


def test_stars_changed_many_times_are_one_review_with_the_latest(person):
    person.rate(TITLE, 4)
    for n in range(12):
        person.rate(TITLE, n % 5 + 1)
    person.rate({"id": "s:7", "t": "Another"}, 1)
    assert sorted((r["id"], r["stars"]) for r in again().reviews()) == [("h:novel", 2), ("s:7", 1)]
    assert rows("review") == 2  # each as it now stands: the changes that made it so aren't kept


def test_a_newer_database_leaves_the_stars_alone(person):
    person.rate(TITLE, 4)
    newer_database()
    with pytest.raises(people.Newer):
        again().rate(TITLE, 1)


def test_stars_are_exported_kept_through_forgetting_places_and_go_with_a_renamed_user(person):
    access.add_user("rory", "correct horse")
    person.place(TITLE, 3, 0.1)
    person.rate(TITLE, 5)
    assert [r["stars"] for r in person.export()["reviews"]] == [5]
    person.forget_places()
    assert again().reviews()[0]["stars"] == 5
    assert access.rename_user("rory", "rory2")
    assert people.Person("rory2").reviews()[0]["stars"] == 5 and people.Person("rory").reviews() == []


def test_everyones_stars_are_read_for_those_named(person):
    person.rate(TITLE, 4)
    people.Person("lucy").rate(TITLE, 2)
    people.Person("owen").place(TITLE, 1, 0.1)  # nothing said of titles
    found = people.People().everyones_reviews({"rory", "lucy", "owen", "nobody"})
    assert {name: [r["stars"] for r in said] for name, said in found.items()} == {"lucy": [2], "rory": [4]}
    assert list(people.People().everyones_reviews({"lucy"})) == ["lucy"]  # a name that isn't asked for isn't read


# ---- the server ---------------------------------------------------------------------------------

def settings_say(text: str) -> None:
    """The owner changes the settings, which the server reads afresh."""
    paths.settings_file().write_text(text, encoding="utf-8")
    settings.reload()


def stars_are(who):
    settings_say(f'[reviews]\nwho = "{who}"\n')


def giving(browser, key=None, **body):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/stars", json={"id": "h:novel", "stars": 4, **body}, headers={"X-Personal-Key": key})


def test_stars_are_off_until_the_owner_turns_them_on(server):
    with logged_in(server, "owen") as browser:
        assert browser.get("/api/session").json()["stars"] == {"see": False, "give": False, "waits": False}
        assert browser.get("/api/stars").json() == {"ok": True, "on": False}
        answer = giving(browser)
        assert answer.status_code == 409 and "off" in answer.json()["error"]
    assert people.Person("owen").reviews() == []


@pytest.mark.parametrize("who, givers", [("borrower", {"bea", "rory", "lucy", "owen"}), ("reader", {"rory", "lucy", "owen"}),
                                         ("librarian", {"lucy", "owen"}), ("owner", {"owen"})])
def test_who_may_give_stars_and_everyone_sees_them(server, who, givers):
    stars_are(who)
    for n, name in enumerate(("bea", "rory", "lucy", "owen"), start=1):
        with logged_in(server, name) as browser:
            assert browser.get("/api/session").json()["stars"] == {"see": True, "give": name in givers, "waits": False}
            answer = giving(browser, stars=n)
            assert (answer.status_code, answer.json()["ok"]) == ((200, True) if name in givers else (409, False))
    expected = {name: n for n, name in enumerate(("bea", "rory", "lucy", "owen"), start=1) if name in givers}
    for name in ("bea", "rory"):
        with logged_in(server, name) as browser:
            assert browser.get("/api/stars").json() == {"ok": True, "on": True, "stars": {"h:novel": expected}, "words": []}


def test_a_device_without_a_login_sees_no_stars_and_gives_none_at_home_either(server):
    stars_are("reader")
    with logged_in(server, "rory") as browser:
        giving(browser, stars=5)
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as browser:
        assert browser.get("/api/session").status_code == 401 and browser.get("/api/stars").status_code == 401
        answer = browser.post("/api/me/stars", json={"id": "h:novel", "stars": 1}, headers={"X-Personal-Key": "x"})
        assert answer.status_code == 401  # no name, so nowhere to keep them
    through = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}  # a tunnel on this computer, on the one port
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=through) as outside:  # from outside, with none: nothing
        assert outside.get("/api/stars").status_code == 401


def test_stars_are_taken_back_and_turning_them_off_keeps_them(server):
    stars_are("reader")
    with logged_in(server, "rory") as browser:
        assert giving(browser, stars=3).json()["ok"]
        assert giving(browser, stars=0).json()["ok"] and browser.get("/api/stars").json()["stars"] == {}
        giving(browser, stars=5)
        stars_are("off")
        assert browser.get("/api/stars").json() == {"ok": True, "on": False}
        stars_are("reader")
        assert browser.get("/api/stars").json()["stars"] == {"h:novel": {"rory": 5}}


@pytest.mark.parametrize("stars", [6, -1, 2.5, True, "4", None])
def test_what_isnt_a_number_of_stars_isnt_kept(server, stars):
    stars_are("reader")
    with logged_in(server, "rory") as browser:
        answer = giving(browser, stars=stars)
        assert answer.status_code == 400 and not answer.json()["ok"]
    assert people.Person("rory").reviews() == []


def test_stars_need_a_title_the_key_the_origin_and_json(server):
    stars_are("reader")
    with logged_in(server, "rory") as browser:
        assert giving(browser, id="h:nothing").status_code == 404
        assert giving(browser, id=["h:novel"]).status_code == 404
        assert giving(browser, key="wrong").status_code == 403
        key = browser.get("/api/session").json()["me"]["key"]
        assert browser.post("/api/me/stars", content=b'{"id":"h:novel","stars":4}', headers={"X-Personal-Key": key, "Content-Type": "text/plain"}).status_code == 415
        assert browser.post("/api/me/stars", json={"id": "h:novel", "stars": 4}, headers={"X-Personal-Key": key, "Origin": "http://elsewhere.example"}).status_code == 403
    assert people.Person("rory").reviews() == []


def test_someone_without_an_account_any_more_isnt_counted(server):
    stars_are("reader")
    for name in ("rory", "lucy"):
        with logged_in(server, name) as browser:
            giving(browser, stars=4)
    access.remove_user("lucy")
    with logged_in(server, "rory") as browser:
        assert browser.get("/api/stars").json()["stars"] == {"h:novel": {"rory": 4}}


def test_a_newer_format_answers_so(server):
    stars_are("reader")
    with logged_in(server, "rory") as browser:
        giving(browser, stars=4)
        personal = browser.get("/api/session").json()["me"]["key"]
        newer_database()
        answer = giving(browser, personal, stars=1)
        assert answer.status_code in (401, 403)  # nobody is known to a database of a newer version


# ---- words: each person's, and the server -------------------------------------------------------

def test_words_written_waiting_released_changed_and_deleted_from_the_file(person):
    assert person.write(TITLE, 4, "First words.", False, wait=True)["state"] == reviews.WAITING
    assert person.write(TITLE, 4, "First words.", False, wait=True) is None  # sent again
    assert again().check_review("h:novel", "release", 1, "", "owen")["state"] == reviews.SHOWN
    changed = person.write(TITLE, 5, "Second words.", True, wait=True)
    assert (changed["state"], changed["text"], changed["change"]) == (reviews.EDIT_WAITING, "First words.", "Second words.")
    with pytest.raises(people.NotAllowed, match="changed"):
        again().check_review("h:novel", "release", 1, "", "owen")
    assert again().check_review("h:novel", "decline", 2, "Not the ending, please.", "owen")["note"] == "Not the ending, please."
    assert person.write(TITLE, 5, "Third words.", False, wait=False)["state"] == reviews.SHOWN
    with pytest.raises(people.NotAllowed):
        person.rate(TITLE, 0)  # a review with words: delete it
    assert person.write(TITLE, 3, "", False, wait=False)["state"] == reviews.RATED  # the words taken away, the stars kept
    assert [(r["state"], r["stars"]) for r in in_the_table()] == [(reviews.RATED, 3)] and "words." not in json.dumps(in_the_table())
    assert b"words." not in on_disk() and b"ending" not in on_disk()  # gone from the database's file, not only from what's shown
    person.write(TITLE, 3, "Fourth words.", False, wait=False)
    assert b"Fourth" in on_disk()
    assert person.delete_review(["h:novel"]) and again().reviews() == [] and in_the_table() == []
    assert b"Fourth" not in on_disk()
    assert not person.delete_review(["h:novel"])


def test_an_owner_removing_words_keeps_the_stars_and_the_file_forgets(person):
    person.write(TITLE, 2, "Rude words.", False, wait=False)
    assert again().check_review("h:novel", "remove", None, "", "owen")["state"] == reviews.RATED
    assert again().reviews()[0]["stars"] == 2 and "Rude" not in json.dumps(in_the_table()) and b"Rude" not in on_disk()
    with pytest.raises(people.NotAllowed):
        again().check_review("s:7", "release", 1, "", "owen")


def reviewing(browser, key=None, **body):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/review", json={"id": "h:novel", "stars": 4, "text": "Lovely to read aloud.", **body}, headers={"X-Personal-Key": key})


def checking(browser, **body):
    key = browser.get("/api/session").json()["key"]
    return browser.post("/api/reviews/check", json=body, headers={"X-Server-Key": key})


def the_reviews(browser):
    return {r["name"]: r for r in browser.get("/api/reviews", params={"id": "h:novel"}).json()["reviews"]}


def test_words_show_at_once_without_moderation(server):
    stars_are("reader")
    with logged_in(server, "rory") as rory:
        answer = reviewing(rory, spoiler=True).json()
        assert answer["ok"] and answer["review"]["state"] == "shown"
    with logged_in(server, "bea") as bea:  # a borrower reads them, and may write none
        seen = the_reviews(bea)["rory"]
        assert (seen["text"], seen["spoiler"], seen["stars"]) == ("Lovely to read aloud.", True, 4) and "own" not in seen
        assert reviewing(bea).status_code == 409


def test_with_moderation_words_wait_for_an_owner_who_hears_of_it(server, monkeypatch):
    heard = []
    monkeypatch.setattr(bell.Watch, "_on_review", lambda self, waiting: heard.append(waiting))
    settings_say('[reviews]\nwho = "reader"\nmoderation = "on"\n')
    with logged_in(server, "rory") as rory:
        assert rory.get("/api/session").json()["stars"]["waits"]
        assert reviewing(rory).json()["review"]["state"] == "waiting"
        assert the_reviews(rory)["rory"]["own"]["text"] == "Lovely to read aloud."  # the writer sees their own
    assert heard == [1]
    with logged_in(server, "lucy") as lucy:
        assert "text" not in the_reviews(lucy)["rory"] and the_reviews(lucy)["rory"]["stars"] == 4  # the others: the stars only
        assert lucy.get("/api/reviews/waiting").status_code == 403
        assert checking(lucy, name="rory", id="h:novel", op="release", rev=1).status_code == 403
    with logged_in(server, "owen") as owen:
        assert owen.get("/api/session").json()["reviewsWaiting"] == 1
        item = owen.get("/api/reviews/waiting").json()["items"][0]
        assert (item["name"], item["state"], item["text"], item["rev"]) == ("rory", "waiting", "Lovely to read aloud.", 1)
        assert checking(owen, name="rory", id=item["id"], op="release", rev=0).status_code == 409  # not the words seen
        released = checking(owen, name="rory", id=item["id"], op="release", rev=1).json()
        assert released["state"] == "shown" and released["waiting"] == 0
        assert reviewing(owen, text="An owner's own.").json()["review"]["state"] == "shown"  # never waits
    with logged_in(server, "rory") as rory:
        changed = reviewing(rory, text="Changed my mind.").json()["review"]
        assert changed["state"] == "edit-waiting" and changed["change"] == "Changed my mind."
    with logged_in(server, "lucy") as lucy:
        assert the_reviews(lucy)["rory"]["text"] == "Lovely to read aloud."  # the earlier words, while the change waits
    with logged_in(server, "owen") as owen:
        assert checking(owen, name="rory", id="h:novel", op="decline", rev=2, note="Keep the first.").json()["state"] == "edit-declined"
        assert checking(owen, name="nobody", id="h:novel", op="release", rev=2).status_code == 400
        assert checking(owen, name="rory", id="h:novel", op="sparkle", rev=2).status_code == 400
    with logged_in(server, "rory") as rory:
        assert the_reviews(rory)["rory"]["own"]["note"] == "Keep the first."


def test_moderation_for_borrowers_only(server):
    settings_say('[reviews]\nwho = "borrower"\nmoderation = "borrowers"\n')
    with logged_in(server, "bea") as bea:
        assert reviewing(bea).json()["review"]["state"] == "waiting"
    with logged_in(server, "rory") as rory:
        assert reviewing(rory).json()["review"]["state"] == "shown"


@pytest.mark.parametrize("body, status", [
    ({"text": "x" * 1001}, 400), ({"stars": 0}, 400), ({"stars": None}, 400), ({"text": ["a list"]}, 400),
    ({"id": "h:nothing"}, 404), ({"id": None}, 404)])
def test_what_isnt_a_review_isnt_kept(server, body, status):
    stars_are("reader")
    with logged_in(server, "rory") as rory:
        assert reviewing(rory, **body).status_code == status
    assert people.Person("rory").reviews() == []


def test_a_review_deleted_from_the_page_is_gone(server):
    stars_are("reader")
    with logged_in(server, "rory") as rory:
        reviewing(rory, text="<script>alert(1)</script>")
        assert the_reviews(rory)["rory"]["text"] == "<script>alert(1)</script>"  # as text: the page shows it as text
        assert reviewing(rory, op="delete").json() == {"ok": True, "review": None}
        assert the_reviews(rory) == {}
    assert in_the_table() == [] and b"alert" not in on_disk()


def test_reviews_off_hide_everything_and_keep_it(server):
    stars_are("reader")
    with logged_in(server, "rory") as rory:
        reviewing(rory)
        stars_are("off")
        assert rory.get("/api/reviews", params={"id": "h:novel"}).json() == {"ok": True, "on": False}
        assert reviewing(rory, text="More.").status_code == 409
        stars_are("reader")
        assert the_reviews(rory)["rory"]["text"] == "Lovely to read aloud."


# ---- your list, the filter's data, the diagram, the year's best ------------------------------------

def test_the_docs_draw_the_table_as_it_is():
    doc = (Path(__file__).parents[1] / "docs" / "reviews.md").read_text(encoding="utf-8")
    drawn = re.search(r"<!-- reviews\.diagram\(\) -->\n```mermaid\n(.*?)\n```", doc, re.S)
    assert drawn and drawn[1] == reviews.diagram(), "draw it again: reviews.diagram() into docs/reviews.md"
    for (state, op), after in reviews.STEPS.items():
        assert op in reviews.diagram()


def test_your_own_and_which_titles_have_words(server):
    settings_say('[reviews]\nwho = "reader"\nmoderation = "on"\n')
    with logged_in(server, "rory") as rory:
        reviewing(rory, text="Waits a while.")
        mine = rory.get("/api/reviews/mine").json()["reviews"]
        assert [(r["title"], r["state"], r["text"]) for r in mine] == [("h:novel", "waiting", "Waits a while.")]
        assert rory.get("/api/stars").json()["words"] == []  # waiting words aren't others' to read
    with logged_in(server, "owen") as owen:
        checking(owen, name="rory", id="h:novel", op="release", rev=1)
        assert owen.get("/api/stars").json()["words"] == ["h:novel"]
        assert owen.get("/api/reviews/mine").json()["reviews"] == []
    import httpx as _httpx
    with _httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.get("/api/reviews/mine").status_code == 401


def test_the_best_of_a_year():
    titles = {"h:a": {"id": "h:a", "t": "Alpha", "a": ["Ann Author"]}, "h:b": {"id": "h:b", "t": "Beta"}, "h:old": {"id": "h:b", "t": "Beta"}}
    def r(title, stars, when, state="rated", text="", spoiler=False):
        return {"id": title, "stars": stars, "when": when, "state": state, "text": text, "spoiler": spoiler}
    found = {"lucy": [r("h:a", 4, "2026-03-01"), r("h:old", 5, "2026-05-01", "shown", "Loved it.\nTwice.")],
             "owen": [r("h:b", 5, "2026-06-01", "waiting", "Not out yet."), r("h:a", 5, "2025-12-31", "shown", "Last year's.")]}
    text = reviews.best_of(found, titles, "2026")
    assert text.index("## Beta") < text.index("## Alpha, by Ann Author")  # 5.0 before 4.0
    assert "5.0 from 2: lucy 5 · owen 5" in text and "> Loved it.  \n> Twice.  \n> *lucy*" in text
    assert "Not out yet" not in text and "Last year's" not in text  # words not released, and another year's
    assert reviews.best_of(found, titles, "1999").endswith("No titles were rated in 1999.\n")


def test_the_best_of_a_year_shows_words_and_titles_as_they_are_never_as_markup():
    """A review's words, a title and its authors go into the Markdown escaped: no HTML, picture, link, heading or list
    of their own, and a title on its heading's one line, whatever line ends it holds. The file is a text to share or
    print too: a \\ (CommonMark's way) only where a character would make markup, so 3.5 and a lone & read as written."""
    titles = {"h:v": {"id": "h:v", "t": "Velvet Jinx\n# Not a heading <b> #",
                      "a": ["Ann *Writer*", "[Moonwake](http://example.invalid/)"]}}
    words = ('<img src="http://example.invalid/pixel.png"> ![a picture](http://example.invalid/p.png)\n'
             "# Shouting\n- a list\n1. or a numbered one\n3.5 stars, not a list\n---\n> not a quote\n"
             "salt & pepper, &amp; as typed, `code`, _soft_ and a back\\slash | ~~struck~~ 2 > 1")
    found = {"lucy": [{"id": "h:v", "stars": 5, "when": "2026-03-01", "state": "shown", "text": words, "spoiler": False}]}
    text = reviews.best_of(found, titles, "2026")
    heading = "\n## Velvet Jinx # Not a heading \\<b> \\#, by Ann \\*Writer\\*, \\[Moonwake\\](http://example.invalid/)\n"
    assert heading in text  # its # at the end kept, as Markdown would take it away from a heading
    assert ('> \\<img src="http://example.invalid/pixel.png"> !\\[a picture\\](http://example.invalid/p.png)  \n'
            "> \\# Shouting  \n> \\- a list  \n> 1\\. or a numbered one  \n> 3.5 stars, not a list  \n> \\---  \n"
            "> \\> not a quote  \n> salt & pepper, \\&amp; as typed, \\`code\\`, \\_soft\\_ and a back\\\\slash \\| "
            "\\~\\~struck\\~\\~ 2 > 1  \n> *lucy*") in text
    assert "&lt;" not in text and not re.search(r"(?<!\\)<", text)  # no tag, and no entity to read in plain text


def test_the_best_of_a_year_keeps_a_review_s_lines_and_paragraphs():
    """A reviewer's lines stay lines, a list of theirs too, each ended with a hard break (two spaces), and an empty
    line between them a new paragraph, inside the quote; the reviewer's name on a line of its own after them."""
    titles = {"h:v": {"id": "h:v", "t": "Velvet Jinx"}}
    words = "Three things:\n- the lamps\n- the harbour\n\nAnd one more."
    found = {"lucy": [{"id": "h:v", "stars": 4, "when": "2026-03-01", "state": "shown", "text": words, "spoiler": True}]}
    text = reviews.best_of(found, titles, "2026")
    assert ("> Three things:  \n> \\- the lamps  \n> \\- the harbour  \n>\n> And one more.  \n"
            "> *lucy* (tells how it ends)\n") in text


def test_hylly_reviews_lists_and_exports(library, capsys, tmp_path):
    people.Person("rory").write({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 4, "A good one.", False, wait=False)
    people.Person("lucy").write({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 2, "Not for me.", False, wait=True)
    from hylly import cli
    cli.main(["reviews", "--library", str(library)])
    said = capsys.readouterr().out
    assert "rory" in said and "1 rated, 1 with words" in said and "1 waiting for an owner" in said
    assert "1 waiting: Reviews to check, in the library page's Manage menu." in said  # where the page has it
    out = tmp_path / "best.md"
    cli.main(["reviews", "export", "--year", datetime_year(), "--out", str(out), "--library", str(library)])
    text = out.read_text(encoding="utf-8")
    assert "A good one." in text and "Not for me." not in text and "3.0 from 2" in text


def datetime_year() -> str:
    import datetime as dt
    return str(dt.date.today().year)


def test_stars_from_the_outbox_and_the_server_go_into_the_page_only_as_whole_numbers():
    """The outbox is this browser's storage, which anything in it could have written: stars laid over the server's from
    there count only as a whole 1 to 5, or 0 taken back; any other entry is dropped from the outbox, and the server's
    stars stay as they are. A review's stars, the server's, go into its line as a whole 0 to 5, so nothing but stars
    reaches the markup."""
    box = {"stars:f:1": {"n": '"><img src=x>'}, "stars:f:2": {"n": 2.5}, "stars:f:3": {"n": 5}, "stars:f:4": {"n": 0},
           "stars:f:5": None, "stars:f:6": {"n": "4"}, "place:f:1": {"pct": 0.5}}
    server = {"f:1": {"ann": 4, "rory": 2}, "f:4": {"rory": 3}, "f:6": {"rory": 1}}
    all_, kept, one, left, lines = outbox_in_node(f"""
/* part 5 */
/* part 6 */
/* part 7 */
put({json.dumps(box)});
const all = {json.dumps(server)};
outboxStars(all);
const kept = box();
put({json.dumps(box)});
const one = {json.dumps(server)};
outboxStars(one, "f:1");
const lines = ['"><b>', 4, 9, -2].map(starLine);
process.stdout.write(JSON.stringify([all, kept, one, Object.keys(box()), lines]));
""", (r"const starsWord = ", r"const starsOf = "), (r"function setMine\(", r"async function syncStars\("),
        (r"function starLine\(", r"function whenSaid\("))
    assert all_ == {"f:1": {"ann": 4, "rory": 2}, "f:3": {"rory": 5}, "f:6": {"rory": 1}}  # the server's where it isn't stars
    assert kept == {"stars:f:3": {"n": 5}, "stars:f:4": {"n": 0}, "place:f:1": {"pct": 0.5}}  # the rest dropped
    assert one == server and "stars:f:1" not in left and "stars:f:2" in left  # one title's: only its own entry looked at
    assert 'aria-label="0 stars"' in lines[0] and "<b>" not in lines[0]  # nothing but stars reaches the markup
    assert lines[1].count("★") == 5 and 'aria-label="4 stars"' in lines[1]  # four on, one off
    assert 'aria-label="5 stars"' in lines[2] and 'aria-label="0 stars"' in lines[3]


def test_stars_on_their_way_are_kept_until_the_review_with_them_is():
    """A review's stars are newer than the title's stars still on their way, which would arrive after it. Those were
    taken out of the outbox as the review went, and put back if it wasn't kept, so the page closing meanwhile lost them.
    Now they stay in the outbox, held back while the review goes, and go out of it only once it's kept; refused
    (past the changes a minute, say) or not sent at all, they go as they would have, and newer stars given meanwhile go
    after the review, as they're newer still."""
    said = []
    for answer, newer in (("kept", False), ("429", False), ("away", False), ("429", True), ("kept", True)):
        said.append(outbox_in_node(f"""
let starsTimer = 0, during = null;
const RF = {{ it: {{ id: "h:novel" }} }}, STARS_WAIT = 1;
const sendKeyed = async () => {{
  await sendPlaces();  // the outbox goes meanwhile
  during = Object.keys(box());
  {'queue("stars:h:novel", { n: 2 }, false);' if newer else ''}
  if ({json.dumps(answer)} === "away") throw new TypeError("offline");
  return {json.dumps(answer)} === "kept" ? {{ r: {{ status: 200 }}, d: {{ ok: true }} }}
    : {{ r: {{ status: 429 }}, d: {{ ok: false, error: "Too many changes in a minute: try again in 9 seconds." }} }};
}};
/* part 5 */
/* part 6 */
put({{ "stars:h:novel": {{ n: 4, t: 1 }} }});
sendReview({{ stars: 5, text: "" }}).then(d => process.stdout.write(JSON.stringify([d.error || "ok", SENT.length, during,
  Object.values(box()).map(p => p.n), starsTimer])));
""", (r"async function sendReview\(", r'\$\("revsave"\)\.onclick'), (r"async function sendChange\(", r"async function postJson\(")))
    late = "Too many changes in a minute: try again in 9 seconds."
    assert said == [["ok", 0, ["stars:h:novel"], [], 0],  # held, not sent, while the review went; out once it was kept
                    [late, 0, ["stars:h:novel"], [4], 7],  # kept, and sent a moment later
                    ["Not saved: the library can't be reached. Try again in a moment.", 0, ["stars:h:novel"], [4], 7],
                    [late, 0, ["stars:h:novel"], [2], 0],  # newer ones given meanwhile: theirs, with their own timer
                    ["ok", 0, ["stars:h:novel"], [2], 0]]  # and after a review that was kept, they still go


def test_reviews_reports_and_both_queues_send_by_one_helper_and_say_what_went_wrong_in_their_own_words():
    """sendReview, sendReport and the two queues' decisions each had the same POST and its error words, written four
    times (and the key renewal after a restart was missing from all four once, for that reason). One helper sends them
    now; a form says "Not saved" or "Not sent" before why, and a queue's toast only that the library can't be reached."""
    out = outbox_in_node("""
let reply = null;
const sendKeyed = async (path, body, personal) => { if (reply === "away") throw new TypeError("Failed to fetch"); return reply; };
/* part 5 */
(async () => {
  const out = {};
  for (const [name, r] of [["away", "away"], ["refused", { r: { status: 429 }, d: { ok: false, error: "Too many changes in a minute." } }],
                           ["unread", { r: { status: 502 }, d: null }], ["kept", { r: { status: 200 }, d: { ok: true, review: {} } }]]) {
    reply = r;
    out[name] = [(await sendChange("/api/me/review", {}, true, "Not saved")).error, (await sendChange("/api/reports/decide", {}, false, "Not done")).error];
  }
  process.stdout.write(JSON.stringify(out));
})();
""", (r"async function sendChange\(", r"async function postJson\("))
    assert out["away"] == ["Not saved: the library can't be reached. Try again in a moment.", "The library can't be reached. Try again in a moment."]
    assert out["refused"] == ["Too many changes in a minute."] * 2  # the server's own words
    assert out["unread"] == ["Not saved (502).", "Not done (502)."] and out["kept"] == ["", ""]


def test_a_review_saved_before_everyone_s_stars_have_come_closes_its_form():
    """Write a review is offered once giving stars is allowed, before /api/stars has answered: Save and Delete laid the
    review's stars over a table that wasn't there yet, and threw, so the form stayed open without a word although the
    review was saved. With no table yet there's nothing to lay them over."""
    out = outbox_in_node("""
/* part 5 */
setMine(null, "h:novel", 4);  // none yet: nothing to do, and no error
const table = {};
setMine(table, "h:novel", 4);
process.stdout.write(JSON.stringify(table));
""", (r"function setMine\(", r"function outboxStars\("))
    assert out == {"h:novel": {"rory": 4}}


def test_a_failed_look_at_the_reviews_waiting_says_so_and_leaves_the_count_as_it_was():
    """No answer, or a refusal, opened Reviews to check on 'Nothing waits for you.' and set the Manage button's count to
    0, while readers' words waited. As Reported titles does, it says what went wrong now, and the count and the
    panel stay as they were."""
    out = outbox_in_node("""
const els = {}, $ = id => els[id] || (els[id] = { classList: { added: [], add(c) { this.added.push(c); } } });
const closeMenus = () => {}, counts = [], shown = [];
const setWaiting = (kind, n) => counts.push(n), showQueueItem = () => shown.push(RQ.items.length);
let RQ = null;
/* part 5 */
(async () => {
  const out = {};
  for (const [name, reply] of [["away", "away"], ["refused", { status: 403, body: { ok: false, error: "Reviews to check are for the library's owner." } }],
                               ["came", { status: 200, body: { ok: true, items: [{ name: "lucy" }, { name: "ann" }] } }]]) {
    said.length = counts.length = shown.length = $("revq").classList.added.length = 0;
    RQ = { items: [{ name: "lucy" }], i: 0 };
    answer = () => reply;
    await openReviewQueue();
    out[name] = { said: [...said], counts: [...counts], shown: [...shown], opened: $("revq").classList.added.length, items: RQ.items.length };
  }
  process.stdout.write(JSON.stringify(out));
})();
""", (r"async function openReviewQueue\(", r"function showQueueItem\("))
    assert out["away"] == {"said": ["The library can't be reached. Try again in a moment."], "counts": [], "shown": [], "opened": 0, "items": 1}
    assert out["refused"] == {"said": ["Reviews to check are for the library's owner."], "counts": [], "shown": [], "opened": 0, "items": 1}
    assert out["came"] == {"said": [], "counts": [2], "shown": [2], "opened": 1, "items": 2}


FORMS = """
const observers = [], SS = new Map();
class MutationObserver { constructor(f) { this.f = f; } observe(el) { observers.push([el, this.f]); } }
const sessionStorage = { getItem: k => SS.has(k) ? SS.get(k) : null, setItem: (k, v) => SS.set(k, String(v)), removeItem: k => SS.delete(k) };
const element = id => {
  const classes = new Set(), listeners = {};
  const e = { id, value: "", checked: false, hidden: false, textContent: "", innerHTML: "", onclick: null,
    classList: { add: c => { classes.add(c); changed(e); }, remove: c => { classes.delete(c); changed(e); }, contains: c => classes.has(c) },
    addEventListener: (kind, f) => { (listeners[kind] = listeners[kind] || []).push(f); },
    fire: kind => (listeners[kind] || []).forEach(f => f({})), focus() {}, querySelector: () => e, querySelectorAll: () => [] };
  return e;
};
const changed = e => observers.filter(([o]) => o === e).forEach(([, f]) => f());
const els = {}, $ = id => els[id] || (els[id] = element(id));
const closeDialog = id => $(id).classList.remove("open");  // the page's own, as at its top
const esc = s => String(s), STAR = "*", STARS_WAITS = false;
const keepFocus = (box, draw) => draw();  // the focus kept where a part is drawn anew: nothing has it here
let mine = { stars: 3, text: "Saved words.", spoiler: false, state: "shown" }, GIVE = true;
const SHEET = { view: "", it: null, list: null, back: null };
const ownReview = () => mine, myStars = () => 0;
let REPORT_REASONS = { quality: "Bad quality", pages: "Pages missing or out of order", else: "Something else" };
let MY_REPORTS = new Map(), REPORT_FOR = null, RF = null;
const LIMITS = { review: 0, note: 0, report: 0 };  // as the page has them until /api/session has answered
"""


def test_the_limits_on_a_reviews_words_and_an_owners_note_come_from_the_server_and_the_page_has_no_copy(server):
    """The page had its own copy of the limits on a review's words (1,000 characters) and an owner's note (300), which
    nothing held to reviews.py: one changed there, and the page went on allowing what the server then refused.
    /api/session sends them with the rest (with a report's: test_reports.py), and the page has none of its own."""
    with logged_in(server, "rory") as rory:
        limits = rory.get("/api/session").json()["limits"]
    assert limits == {"review": reviews.TEXT_MAX, "note": reviews.NOTE_MAX, "report": reports.MOST_WORDS}
    page = (Path(reviews.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    for most in (reviews.TEXT_MAX, reviews.NOTE_MAX):
        assert f'maxlength="{most}"' not in page and f"slice(0, {most})" not in page and f"/ {most:,}" not in page, most


def test_the_page_holds_its_forms_to_the_limits_and_reasons_the_session_sends():
    """What /api/session says goes into the forms: each textarea's limit, the count under the review's words, a draft
    cut to the limit, the note's limit in the owners' queue, and the report's reasons in the server's order. What isn't
    a limit leaves the page without one, rather than with a guess, and the server's own refusal says the rest."""
    out = outbox_in_node(FORMS + """
/* part 5 */
/* part 6 */
/* part 7 */
/* part 8 */
/* part 9 */
const it = { id: "h:novel", t: "The Novel" }, out = {};
limitsCame({ limits: { review: "a lot", note: 0, report: -5 }, report_reasons: "quality" });  // nothing it can keep
out.odd = [{ ...LIMITS }, $("revtext").maxLength, $("reporttext").maxLength, REPORT_REASONS.quality];
limitsCame({ limits: { review: 12, note: 7, report: 9 }, report_reasons: { pages: "Pages missing", blur: "Blurry", odd: 5 } });
out.limits = [{ ...LIMITS }, $("revtext").maxLength, $("reporttext").maxLength];
SS.set("hylly.draft.rory.review.h:novel", JSON.stringify({ stars: 4, text: "Typed on a phone, and long." }));
openReviewForm(it);
out.review = [$("revtext").value, $("revcount").textContent];
$("revform").classList.remove("open");
openReport(it);
out.reasons = [...$("reportrows").innerHTML.matchAll(/data-reason="(\\w+)"><span>([^<]*)/g)].map(m => [m[1], m[2]]);
process.stdout.write(JSON.stringify(out));
""", (r"const starsWord = ", r"const starsOf = "), (r"// ---- Drafts: ", r"// ---- Reviews: "),
        (r"function revStarsHtml\(", r"async function sendReview\("), (r"function openReport\(", r"async function sendReport\("),
        (r"function limitsCame\(", r"function reportLine\("))
    assert out["odd"] == [{"review": 0, "note": 0, "report": 0}, None, None, "Bad quality"]
    assert out["limits"] == [{"review": 12, "note": 7, "report": 9}, 12, 9]
    assert out["review"] == ["Typed on a p", "12 / 12"]
    assert out["reasons"] == [["pages", "Pages missing"], ["blur", "Blurry"]]  # the server's, in its order
    page = (Path(reviews.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    queue = page[page.index("function showQueueItem("):page.index("async function checkReview(")]
    assert '<textarea id="revqnote" rows="3"${LIMITS.note ? ` maxlength="${LIMITS.note}"` : ""}></textarea>' in queue
    start = page[page.index("async function startSession() {"):]
    assert start.index("limitsCame(s);") < start.index("formsBack();")  # before a form left open comes back


def test_a_session_without_reasons_offers_no_report_and_a_draft_waits_as_it_is_until_they_come():
    """The page's file is read anew for each request, so a page loaded while a newer Hylly is unpacked can meet the
    older server, whose session sends no reasons: Report a problem still showed, the form opened with nothing to tick,
    Send could only say to tick one, and a draft opened again lost its ticked reasons at the first keystroke. Without
    reasons the line offers no report and the form doesn't open, by a tap or by itself; the draft and the form left open
    wait as they are, and come back, ticked, once a session has the reasons."""
    out = outbox_in_node(FORMS + """
/* part 5 */
/* part 6 */
/* part 7 */
/* part 8 */
/* part 9 */
let REPORTS_SEE = false, REPORTED = new Map();
const whenSaid = () => "", reportLineAgain = () => {};
const it = { id: "h:novel", t: "The Novel" }, opened = [];
const openItem = x => { opened.push(x.id); SHEET.it = x; };  // titleById: the page's own, over the made-up ITEMS
$("reader").hidden = true;
REPORT_REASONS = {};  // an older server's session: none
const draft = JSON.stringify({ reasons: ["pages"], comment: "Pages 4 to 9 are blurry." });
SS.set("hylly.draft.rory.report.h:novel", draft);
SS.set("hylly.formopen", JSON.stringify({ by: "rory", form: "report", id: "h:novel" }));  // open as the page left
const out = { line: reportLine(it) };
formsBack();
out.back = [opened.splice(0), formBack];
formBack = { form: "report", id: it.id };  // its details open anyway, and your reports on their way
SHEET.it = it;
answer = () => ({ status: 200, body: { ok: true, reports: {} } });
(async () => {
  await syncMyReports();
  openReport(it);  // a tap on a line drawn before
  out.none = [$("report").classList.contains("open"), formBack, SS.get("hylly.draft.rory.report.h:novel") === draft, SS.has("hylly.formopen")];
  limitsCame({ report_reasons: { quality: "Bad quality", pages: "Pages missing or out of order" } });  // a newer session
  out.offered = reportLine(it).includes("Report a problem");
  await syncMyReports();
  out.came = [$("report").classList.contains("open"), formBack, $("reportrows").innerHTML.includes('data-reason="pages" checked'), $("reporttext").value];
  process.stdout.write(JSON.stringify(out));
})();
""", (r"// ---- Drafts: ", r"// ---- Reviews: "), (r"function limitsCame\(", r"function reportLine\("),
        (r"function reportLine\(", r"function wireReportLine\("), (r"async function syncMyReports\(", r"function reportedCame\("),
        (r"function openReport\(", r"async function sendReport\("))
    assert out["line"] == "" and out["back"] == [[], None]  # nothing offered, and the form left open isn't opened
    assert out["none"] == [False, {"form": "report", "id": "h:novel"}, True, True]  # the draft and the open form wait
    assert out["offered"] and out["came"] == [True, None, True, "Pages 4 to 9 are blurry."]


def test_a_typed_review_or_report_closed_by_escape_or_a_tap_beside_it_comes_back_until_cancel():
    """Escape, or a tap on the dimmed edge of the screen (16 px on a phone), closed the review and report forms and threw
    away what was typed, and a login that ended while one was open took the page to the login page and lost it
    too. What's typed is kept in this tab, by person and title, until Save or the form's own Cancel: the form
    opened again has it, saying it isn't saved yet. The form open as the page left opens again after the login, for the
    same person only."""
    out = outbox_in_node(FORMS + """
/* part 5 */
/* part 6 */
/* part 7 */
/* part 8 */
/* part 9 */
/* part 10 */
const it = { id: "h:novel", t: "The Novel" }, out = {};
openReviewForm(it);
out.opened = [$("revtext").value, RF.stars, $("revdraft").hidden, JSON.parse(SS.get("hylly.formopen"))];
$("revtext").value = "Better words, typed on a phone.";
$("revtext").fire("input");
RF.stars = 5;
revDraft();  // as a star tapped in the form does
$("revform").classList.remove("open");  // Escape, or a tap beside the box
out.closed = [SS.has("hylly.formopen"), JSON.parse(SS.get("hylly.draft.rory.review.h:novel"))];
openReviewForm(it);
out.again = [$("revtext").value, RF.stars, $("revdraft").hidden];
$("revcancel").onclick();  // the form's own Cancel: they go
openReviewForm(it);
out.cancelled = [$("revtext").value, RF.stars, $("revdraft").hidden, SS.has("hylly.draft.rory.review.h:novel")];
$("revform").classList.remove("open");
openReport(it);
$("reportrows").querySelectorAll = () => [{ dataset: { reason: "pages" } }];  // ticked
$("reporttext").value = "Pages 4 to 9 are blurry.";
$("reporttext").fire("input");
$("report").classList.remove("open");
openReport(it);
out.report = [$("reportrows").innerHTML.includes('data-reason="pages" checked'), $("reporttext").value, $("reportdraft").hidden];
$("reportcancel").onclick();
openReport(it);
out.reportCancelled = [$("reporttext").value, $("reportdraft").hidden];
$("report").classList.remove("open");
let opened = [];
const openItem = x => opened.push(x.id);
$("reader").hidden = true;
for (const by of ["rory", "lucy"]) {  // the page went to the login page with it open; then logged in as rory, or as lucy
  formBack = null;
  SS.set("hylly.formopen", JSON.stringify({ by, form: "review", id: "h:novel" }));
  formsBack();
  out[by] = [opened.splice(0), formBack];
}
process.stdout.write(JSON.stringify(out));
""", (r"const starsWord = ", r"const starsOf = "), (r"// ---- Drafts: ", r"// ---- Reviews: "),
        (r"function revStarsHtml\(", r"async function sendReview\("), (r"function openReport\(", r"async function sendReport\("),
        (r'\$\("reportcancel"\)\.onclick', r"// Owners' and librarians' list"), (r"function limitsCame\(", r"function reportLine\("))
    assert out["opened"] == ["Saved words.", 3, True, {"by": "rory", "form": "review", "id": "h:novel"}]
    assert out["closed"] == [False, {"stars": 5, "text": "Better words, typed on a phone.", "spoiler": False}]
    assert out["again"] == ["Better words, typed on a phone.", 5, False]  # with the word that they aren't saved yet
    assert out["cancelled"] == ["Saved words.", 3, True, False]
    assert out["report"] == [True, "Pages 4 to 9 are blurry.", False] and out["reportCancelled"] == ["", True]
    assert out["rory"] == [["h:novel"], {"form": "review", "id": "h:novel"}]  # the title, and the form once its reviews come
    assert out["lucy"] == [[], None]  # another person's: never shown to this one
    page = (Path(reviews.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    loading = page[page.index("async function loadReviews("):page.index("const ownReview = ")]
    assert loading.index("showReviews(it);") < loading.index("openReviewForm(it);")
    assert 'id="revdraft"' in page and 'id="reportdraft"' in page
