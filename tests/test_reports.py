"""Reports of a title's file (reports.py): the six reasons and the words; each person's own journal; a title waiting, then
marked for a better copy or found fine, and waiting again on a newer report; the server: anyone logged in reports, owners
and librarians see and decide, a report that came meanwhile isn't decided over, and the alarm says how many, never what."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hylly import backup, bell, db, paths, people, reports, serve
from hylly.library import Catalog

from helpers import EVERY_ROLE, TITLE, logged_in, rows


@pytest.fixture
def users():
    return EVERY_ROLE  # the library fixture's: bea a borrower, rory a reader, lucy a librarian, owen an owner


# ---- the words
def test_reasons_are_known_ones_in_their_order_and_words_are_plain():
    assert reports.clean(["wrong", "quality", "wrong"], "  Blurry\x07 pages\n4 to 9  ") == (["quality", "wrong"], "Blurry pages\n4 to 9")
    assert reports.clean(["else"], "The cover is upside down") == (["else"], "The cover is upside down")
    assert list(reports.REASONS.values()) == ["Bad quality", "Low resolution", "Pages missing or out of order", "Wrong file",
                                              "Questionable content", "Something else"]


def test_the_page_offers_the_servers_reasons_and_limit_and_has_no_copy_of_them(server):
    """The page had its own copy of the reasons and of the 500 characters, which nothing held to these: a reason or
    the limit changed here, and the page went on offering the old ones, which the server then refused. /api/session
    sends them, the reasons in their order, and the page has none of its own (what it does with them: test_reviews.py)."""
    with logged_in(server, "rory") as rory:
        session = rory.get("/api/session").json()
    assert list(session["report_reasons"].items()) == list(reports.REASONS.items()) and session["limits"]["report"] == reports.MOST_WORDS
    page = Path(reports.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    # a copy of the table: a reason's words as a string of the script, alone or by its key; a sentence may say them
    script = page[page.index('<script type="text/hylly-later" id="pagescript">'):]
    copied = [words for key, words in reports.REASONS.items()
              if re.search(rf"""(["'`]){re.escape(words)}\1|\b{key}\s*:\s*["'`]""", script)]
    assert copied == []
    most = reports.MOST_WORDS
    assert f'maxlength="{most}"' not in page and f"slice(0, {most})" not in page


def test_the_pages_decisions_and_the_states_it_filters_by_are_the_servers():
    """The page has the decisions' words and the states' names of its own (its buttons and the Reported filter), but
    what it sends and goes by are the server's keys: a decision renamed here, and its buttons would be refused."""
    page = Path(reports.__file__).with_name("catalog_page.html").read_text(encoding="utf-8")
    assert set(re.findall(r'data-decide="(\w+)"', page)) == set(reports.DECISIONS)
    assert re.findall(r"^  rp: \{.*\n    labels: \{ (\w+): [^,]+, (\w+): ", page, re.M) == [(reports.WAITING, reports.BETTER)]  # FACET.rp's


@pytest.mark.parametrize("reasons, comment, why", [([], "", "at least one"), (None, "", "at least one"), (["sparkle"], "", "no such"),
                                                   (["else"], "  ", "say what"), (["quality"], "x" * 501, "at most"), (["quality"], 5, "words"),
                                                   ([1], "", "at least one")])
def test_what_isnt_a_report_is_refused(reasons, comment, why):
    with pytest.raises(reports.Refused, match=why):
        reports.clean(reasons, comment)


# ---- a person's own
def test_a_persons_report_replaces_their_earlier_one_and_can_be_taken_back():
    rory = people.Person("rory")
    first = rory.report(TITLE, ["quality"], "")
    assert first["reasons"] == ["quality"] and first["t"] == "The Novel"
    assert rory.report(TITLE, ["quality"], "") is None  # sent again: nothing new
    second = rory.report(TITLE, ["quality", "resolution"], "Hard to read")
    assert [r["reasons"] for r in people.Person("rory").reports()] == [["quality", "resolution"]]  # read back from the disk
    assert second["time"] >= first["time"]
    assert "reports" in rory.export() and rory.export()["reports"][0]["comment"] == "Hard to read"
    assert rory.unreport(["h:novel"]) and not rory.unreport(["h:novel"])
    assert people.Person("rory").reports() == [] and rows("report") == 0


# ---- a title's state
def report(name, title_id, t, reasons=("quality",)):
    return name, {"id": title_id, "reasons": list(reasons), "comment": "", "time": t, "when": ""}


def test_waiting_then_better_then_waiting_again_then_fine():
    everyone = {"rory": [report("rory", "a", 10)[1]], "bea": [report("bea", "a", 20)[1], report("bea", "b", 15)[1]]}
    s = reports.states(everyone, {})
    assert s["a"]["state"] == reports.WAITING and [r["by"] for r in s["a"]["reports"]] == ["rory", "bea"] and s["a"]["newest"] == 20
    better = {"a": {"id": "a", "decision": "better", "upto": 20}}
    s = reports.states(everyone, better)
    assert s["a"]["state"] == reports.BETTER and len(s["a"]["reports"]) == 2 and s["b"]["state"] == reports.WAITING
    everyone["rory"] = [report("rory", "a", 30)[1]]  # rory says more, later
    s = reports.states(everyone, better)
    assert s["a"]["state"] == reports.WAITING and [r["by"] for r in s["a"]["reports"]] == ["rory"]
    s = reports.states(everyone, {"a": {"id": "a", "decision": "fine", "upto": 30}})
    assert "a" not in s  # found fine, nothing newer
    s = reports.states({}, {"c": {"id": "c", "decision": "better", "upto": 5}})
    assert s["c"]["state"] == reports.BETTER and s["c"]["reports"] == []  # marked, though its reporter has gone


@pytest.mark.parametrize("upto", ["missing", None, float("inf"), float("nan"), pytest.param(10 ** 400, id="10**400"), "30", True])
def test_a_decision_kept_without_a_time_covers_nothing_so_its_reports_wait_again(upto):
    # the database keeps a NaN as nothing; an infinity, or a number past every time kept as JSON, would hide every later report
    kept = {"id": "a", "decision": "fine", **({} if upto == "missing" else {"upto": upto})}
    s = reports.states({"rory": [report("rory", "a", 10)[1]]}, {"a": kept})
    assert s["a"]["state"] == reports.WAITING and s["a"]["newest"] == 10 and s["a"]["decided"] == kept
    assert reports.upto_of(kept) == -1.0 and reports.upto_of(None) == -1.0 and reports.upto_of({"upto": 12.5}) == 12.5


@pytest.mark.parametrize("upto", [float("nan"), float("inf"), float("-inf"), pytest.param(10 ** 400, id="10**400"), "12.5", None])
def test_a_decision_is_kept_only_with_a_time(upto):
    with pytest.raises(reports.Refused, match="no such time"):
        reports.Decisions().decide(TITLE, "fine", "owen", upto)
    assert rows("report_decision") == 0
    assert reports.Decisions().decide(TITLE, "fine", "owen", 12)["upto"] == 12.0  # a whole number is a time too


def test_decisions_are_kept_and_the_latest_counts():
    d = reports.Decisions()
    assert d.latest() == {}
    d.decide(TITLE, "better", "owen", 12.5)
    other = reports.Decisions()  # another process, say, a command
    assert other.latest()["h:novel"]["decision"] == "better"
    other.decide(TITLE, "fine", "lucy", 14.0)
    assert d.latest()["h:novel"]["decision"] == "fine" and d.latest()["h:novel"]["by"] == "lucy"
    with pytest.raises(reports.Refused):
        d.decide(TITLE, "maybe", "owen", 1.0)
    assert rows("report_decision") == 2 and not (paths.config_dir() / "reports.jsonl").exists()  # in the database only


def test_the_decisions_go_into_backups_with_the_database(tmp_path):
    assert "reports.jsonl" not in backup.SETTINGS
    reports.Decisions().decide(TITLE, "better", "owen", 1.0)
    (tmp_path / "state").mkdir()
    (tmp_path / "catalog").mkdir()
    db.own(db.database(), tmp_path)
    copy = db.nightly(tmp_path)
    assert json.loads((copy / "report_decision.jsonl").read_text(encoding="utf-8"))["decision"] == "better"


# ---- the server
def sending(browser, **body):
    key = browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/report", json={"id": "h:novel", "reasons": ["quality"], **body}, headers={"X-Personal-Key": key})


def deciding(browser, **body):
    key = browser.get("/api/session").json()["key"] or "none: not an editor"
    return browser.post("/api/reports/decide", json={"id": "h:novel", **body}, headers={"X-Server-Key": key})


def test_anyone_logged_in_reports_and_owners_and_librarians_decide(server, monkeypatch):
    heard = []
    monkeypatch.setattr(bell.Watch, "_on_report", lambda self, waiting: heard.append(waiting))
    with logged_in(server, "bea") as bea:  # a borrower too
        answer = sending(bea, comment="Pages 4 to 9 are blurry").json()
        assert answer["ok"] and answer["report"]["reasons"] == ["quality"]
        assert bea.get("/api/me/reports").json()["reports"]["h:novel"]["comment"] == "Pages 4 to 9 are blurry"
        assert bea.get("/api/session").json()["reportsWaiting"] == 0  # not hers to go through
        assert bea.get("/api/reports").status_code == 403
    assert heard == [1]
    with logged_in(server, "rory") as rory:
        assert sending(rory, reasons=["resolution"]).json()["ok"]
        assert rory.get("/api/reports").status_code == 403
        assert deciding(rory, decision="fine", seen=1e12).status_code == 403
    assert heard == [1]  # the same title: still one waiting, no second alarm
    with logged_in(server, "lucy") as lucy:  # a librarian
        assert lucy.get("/api/session").json()["reportsWaiting"] == 1
        items = lucy.get("/api/reports").json()["items"]
        assert [(i["id"], i["state"], [r["by"] for r in i["reports"]]) for i in items] == [("h:novel", "waiting", ["bea", "rory"])]
        seen = items[0]["reports"][0]["time"]  # as if only bea's had been seen
        assert deciding(lucy, decision="better", seen=seen).status_code == 409  # rory's came meanwhile
        newest = max(r["time"] for r in items[0]["reports"])
        done = deciding(lucy, decision="better", seen=newest).json()
        assert done["ok"] and done["waiting"] == 0
        marked = lucy.get("/api/reports").json()["items"][0]
        assert marked["state"] == "better" and marked["decided"]["by"] == "lucy" and len(marked["reports"]) == 2
    with logged_in(server, "rory") as rory:
        assert sending(rory, reasons=["resolution", "pages"]).json()["ok"]  # more to say: waiting again
    assert heard == [1, 1]
    with logged_in(server, "owen") as owen:  # the owner
        item = owen.get("/api/reports").json()["items"][0]
        assert item["state"] == "waiting" and [r["by"] for r in item["reports"]] == ["rory"] and item["decided"]["decision"] == "better"
        assert deciding(owen, decision="fine", seen=item["newest"]).json()["ok"]
        assert owen.get("/api/reports").json()["items"] == []
        assert deciding(owen, decision="better", seen=item["newest"]).status_code == 409  # nothing reported to mark
    with db.database().read() as c:
        assert [row[0] for row in c.execute("SELECT decision FROM report_decision ORDER BY seq")] == ["better", "fine"]


@pytest.mark.parametrize("body, status", [({"reasons": []}, 400), ({"reasons": ["sparkle"]}, 400), ({"reasons": ["else"]}, 400),
                                          ({"id": "nothing"}, 404), ({"id": 5}, 404)])
def test_what_isnt_a_report_isnt_kept(server, body, status):
    with logged_in(server, "rory") as rory:
        assert sending(rory, **body).status_code == status
        assert rory.get("/api/me/reports").json()["reports"] == {}


def test_a_report_needs_a_login_and_the_personal_key(server):
    with logged_in(server, "rory") as rory:
        assert rory.post("/api/me/report", json={"id": "h:novel", "reasons": ["quality"]}).status_code == 403
        assert sending(rory, op="take back").json() == {"ok": True, "report": None}


def test_a_decision_needs_the_editors_key_and_says_what(server):
    with logged_in(server, "owen") as owen:
        assert owen.post("/api/reports/decide", json={"id": "h:novel", "decision": "fine", "seen": 0}).status_code == 403
        assert deciding(owen, decision="maybe", seen=0).status_code == 400
        assert deciding(owen, decision="fine", seen=True).status_code == 400
        assert deciding(owen, decision="fine").status_code == 400
        assert deciding(owen, id="nothing", decision="fine", seen=0).status_code == 404


@pytest.mark.parametrize("seen", [float("nan"), float("inf"), float("-inf"), 1e300, pytest.param(10 ** 400, id="10**400"), -1, "1e12", None, [1]])
def test_a_seen_that_is_no_time_the_server_gave_is_refused_and_nothing_is_kept(server, seen):
    with logged_in(server, "rory") as rory:
        sending(rory)
    with logged_in(server, "lucy") as lucy:
        key = lucy.get("/api/session").json()["key"]
        body = json.dumps({"id": "h:novel", "decision": "fine", "seen": seen})  # NaN and Infinity as Python's JSON writes them
        sent = lucy.post("/api/reports/decide", content=body, headers={"X-Server-Key": key, "Content-Type": "application/json"})
        assert sent.status_code == 400 and sent.json()["error"] == "Which title, and what of it?"
        assert lucy.get("/api/reports").json()["items"][0]["state"] == "waiting"
    assert rows("report_decision") == 0


def test_a_decision_keeps_the_servers_newest_report_time_never_the_number_sent(server):
    with logged_in(server, "rory") as rory:
        sending(rory)
    with logged_in(server, "lucy") as lucy:
        newest = lucy.get("/api/reports").json()["items"][0]["newest"]
        assert deciding(lucy, decision="fine", seen=newest + 0.5).json()["ok"]  # a little past it: still a time, not kept
        assert deciding(lucy, decision="fine", seen=newest).json()["ok"]  # again, with nothing waiting: covers what it did
        assert deciding(lucy, id="h:lwolf1", decision="fine", seen=0).json()["ok"]  # never reported: covers nothing
    with db.database().read() as c:
        assert [tuple(row) for row in c.execute("SELECT title, upto FROM report_decision ORDER BY seq")] == [
            ("h:novel", round(newest, 3)), ("h:novel", round(newest, 3)), ("h:lwolf1", -1.0)]
    with logged_in(server, "bea") as bea:
        assert sending(bea).json()["ok"]  # a later report waits, as it always should
    with logged_in(server, "lucy") as lucy:
        assert [(i["id"], [r["by"] for r in i["reports"]]) for i in lucy.get("/api/reports").json()["items"]] == [("h:novel", ["bea"])]


def test_a_decision_kept_without_a_time_doesnt_take_the_editors_pages_down(server):
    with logged_in(server, "rory") as rory:
        sending(rory)
    reports.Decisions().decide(TITLE, "fine", "lucy", 1.0)
    with db.database().write() as c:  # a NaN sent before the server checked: kept as nothing
        c.execute("UPDATE report_decision SET upto = NULL")
    for name in ("owen", "lucy"):
        with logged_in(server, name) as editor:
            session = editor.get("/api/session")
            assert session.status_code == 200 and session.json()["edit"] and session.json()["reportsWaiting"] == 1, name
            assert editor.get("/api/reports").json()["items"][0]["state"] == "waiting"  # decided anew
    with logged_in(server, "bea") as bea:
        assert sending(bea).json()["ok"]


def test_a_count_that_cant_be_worked_out_is_0_and_the_session_still_answers(server, monkeypatch, capsys):
    monkeypatch.setattr(serve.Handler, "_report_states", lambda self: {}["a record that can't be read"])
    with logged_in(server, "owen") as owen:
        for _ in range(2):
            session = owen.get("/api/session")
            assert session.status_code == 200 and session.json()["edit"] and session.json()["reportsWaiting"] == 0
    with logged_in(server, "rory") as rory:
        assert sending(rory).json()["ok"]  # nor does it stop anyone reporting
    told = capsys.readouterr()
    assert told.err.count("A count for the library page failed: KeyError") == 1 and "Traceback" in told.err  # the server's words: standard error


def test_a_removed_persons_report_no_longer_counts_but_a_mark_stays(server):
    from hylly import access
    with logged_in(server, "rory") as rory:
        sending(rory)
    with logged_in(server, "owen") as owen:
        item = owen.get("/api/reports").json()["items"][0]
        deciding(owen, decision="better", seen=item["newest"])
    access.remove_user("rory")
    with logged_in(server, "owen") as owen:
        item = owen.get("/api/reports").json()["items"][0]
        assert item["state"] == "better" and item["reports"] == []


def test_the_library_s_word_on_a_reported_title_follows_it_when_its_file_moves(server, library, capsys):
    """A decision is kept by the id its title had then, with its name and first file beside it. Once the file moves and
    the title's id changes, its reports follow it, and so does the library's word: a title found fine isn't waiting
    again, and one marked for a better copy keeps its mark (reports.following). hylly reports says the same."""
    import shutil
    from urllib.parse import unquote

    from hylly import catalog, cli

    def by_file(name: str) -> dict:
        return next(i for i in Catalog(library).get() if unquote(i["f"][0][0]).endswith(name))

    poem, comic = by_file("Bartleby the Scrivener.epub"), by_file("Neon Rangers 008.cbz")  # folder titles: ids by path
    with logged_in(server, "rory") as rory:
        assert sending(rory, id=poem["id"]).json()["ok"] and sending(rory, id=comic["id"]).json()["ok"]
    with logged_in(server, "lucy") as lucy:
        newest = {i["id"]: i["newest"] for i in lucy.get("/api/reports").json()["items"]}
        assert deciding(lucy, id=poem["id"], decision="fine", seen=newest[poem["id"]]).json()["ok"]
        assert deciding(lucy, id=comic["id"], decision="better", seen=newest[comic["id"]]).json()["ok"]
    shutil.move(library / "collections" / "Poetry", library / "collections" / "Verse")
    shutil.move(library / "comics" / "Neon Comics", library / "comics" / "Neon")
    catalog.build(library, log=lambda s: None)
    poem_now, comic_now = by_file("Bartleby the Scrivener.epub"), by_file("Neon Rangers 008.cbz")
    assert poem_now["id"] != poem["id"] and comic_now["id"] != comic["id"]
    with logged_in(server, "lucy") as lucy:
        assert lucy.get("/api/session").json()["reportsWaiting"] == 0
        items = lucy.get("/api/reports").json()["items"]
        assert [(i["id"], i["state"], i["decided"]["by"]) for i in items] == [(comic_now["id"], "better", "lucy")]
        assert deciding(lucy, id=poem_now["id"], decision="fine", seen=0).json()["ok"]  # nothing waits: it covers what it did
    with db.database().read() as c:
        last = c.execute("SELECT title, upto FROM report_decision ORDER BY seq DESC LIMIT 1").fetchone()
    assert tuple(last) == (poem_now["id"], round(newest[poem["id"]], 3))
    cli.main(["reports", "--library", str(library), "--json"])
    assert [(t["id"], t["state"]) for t in json.loads(capsys.readouterr().out)["titles"]] == [(comic_now["id"], "better")]


def test_the_alarm_says_how_many_never_what_or_whose(tmp_path):
    sent = []
    watch = bell.Watch(send=lambda *a, **k: sent.append((a, k)), state=tmp_path / "state.json")
    paths.settings_file().write_text('[bell]\ntopic = "made-up-topic-for-tests"\n', encoding="utf-8")
    watch.event("report", waiting=2)
    said = json.dumps(sent, default=str)
    assert "2 reported titles" in said and "The Novel" not in said and "rory" not in said


# ---- hylly reports
def test_the_command_lists_them_with_what_humbles_orders_have(library, capsys):
    from hylly import cli
    people.Person("rory").report(TITLE, ["quality", "else"], "The cover is upside down")
    people.Person("bea").report({"id": "h:lwolf1", "t": "Lantern Wolf 1", "f": ""}, ["resolution"], "")
    reports.Decisions().decide({"id": "h:lwolf1", "t": "Lantern Wolf 1"}, "better", "owen", 1e12)
    cli.main(["reports", "--library", str(library), "--json"])
    said = json.loads(capsys.readouterr().out)
    assert said["orders_saved"] == "2026-01-01"
    by = {t["id"]: t for t in said["titles"]}
    novel = by["h:novel"]
    assert novel["state"] == "waiting" and novel["reports"][0]["reasons"] == ["Bad quality", "Something else"] and novel["reports"][0]["by"] == "rory"
    assert novel["files"] and novel["shop"] and all({"label", "format", "size", "here"} <= set(f) for f in novel["shop"])
    assert by["h:lwolf1"]["state"] == "better" and by["h:lwolf1"]["decided"]["by"] == "owen"
    assert [t["state"] for t in said["titles"]] == ["waiting", "better"]  # waiting first
    cli.main(["reports", "--library", str(library)])
    text = capsys.readouterr().out
    assert "1 waiting for an owner or a librarian, 1 marked for a better copy." in text and "Humble has, in the orders saved 2026-01-01:" in text
    assert '"The cover is upside down"' in text and "Go through them in the library page: Manage, Reported titles." in text


def test_the_command_prints_a_reader_s_comment_on_one_line(library, capsys):
    """A comment keeps its line ends (clean), and any reader may write one: the plain listing prints it on one line, its
    line ends as " / " (as bell.log has an alarm's), so no comment can make the listing show a title or a report that
    isn't there. --json keeps it whole, in a field of its own."""
    from hylly import cli
    reasons, comment = reports.clean(["else"], "Fine.\nLantern Wolf 1: waiting\n  owen, 2026-01-01: Bad quality Hylly: done")
    people.Person("rory").report(TITLE, reasons, comment)
    cli.main(["reports", "--library", str(library)])
    text = capsys.readouterr().out
    assert '. "Fine. / Lantern Wolf 1: waiting /   owen, 2026-01-01: Bad quality / Hylly: done"\n' in text
    assert not [line for line in text.splitlines() if line.startswith(("Lantern Wolf 1", "  owen", "Hylly"))]
    cli.main(["reports", "--library", str(library), "--json"])
    assert json.loads(capsys.readouterr().out)["titles"][0]["reports"][0]["comment"] == comment


def test_the_command_with_nothing_reported(library, capsys):
    from hylly import cli
    cli.main(["reports", "--library", str(library)])
    assert capsys.readouterr().out.strip() == "No title is reported, and none is marked for a better copy."
