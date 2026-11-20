"""Online lookups: the shared core (throttle, budgets, retries, cache, typed problems) and the four sources,
against fake sites shaped like the real ones (2026-10-31), and each source named in its one row (sites.LOOKUPS) and
wherever the docs list them."""

from __future__ import annotations

import json
import re
import time
import unicodedata

import httpx
import pytest

from hylly import catalog, curation, lookups
from hylly.library import Catalog
from hylly.lookups import comicvine, metron, openlibrary

from helpers import CLOCK_TIMES, Clock, Gone, OPF, ascii_titles, build_library, make_epub, session_for


TOKEN = "t" * 40


def q(id_, title, kind="comic", **kw):
    return lookups.Query(id=id_, kind=kind, title=title, **kw)


# ---- Metron, faked --------------------------------------------------------------------------

ISSUES = {  # id: (series, type, number, year_began, publisher, credits)
    1: ("Moonwake", "Trade Paperback", "3", 2013, "Lantern Press", [("Ann Writer", "Writer"), ("Bea Artist", "Artist")]),
    2: ("Moonwake", "Ongoing Series", "3", 2012, "Lantern Press", [("Ann Writer", "Writer")]),
    3: ("Ironclad Hearts", "Ongoing Series", "31", 1995, "Lantern Press", [("Cy Scribe", "Writer"), ("Dee Penciller", "Penciller")]),
}


class FakeMetron:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: list[httpx.Response] = []  # answers to give first, before the normal ones

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Invalid token."})
        path, p = request.url.path, request.url.params
        if path == "/api/issue/":
            words = p["series_name"].lower().split()
            rows = [{"id": i, "number": n, "issue": f"{s} #{n}", "cover_date": f"{y}-01-01", "image": None,
                     "series": {"id": 10 + i, "name": s, "volume": 1, "year_began": y}}
                    for i, (s, _, n, y, _, _) in ISSUES.items() if all(w in s.lower() for w in words) and n == p["number"]]
            return httpx.Response(200, json={"count": len(rows), "next": None, "previous": None, "results": rows},
                                  headers={"X-RateLimit-Sustained-Remaining": "4000", "X-RateLimit-Sustained-Reset": "50000"})
        if path.startswith("/api/issue/"):
            i = int(path.split("/")[3])
            s, kind, n, y, pub, credits = ISSUES[i]
            return httpx.Response(200, json={
                "id": i, "number": n, "cover_date": f"{y + 1}-03-01", "publisher": {"id": 1, "name": pub},
                "series": {"id": 10 + i, "name": s, "sort_name": s, "volume": 1, "year_began": y,
                           "series_type": {"id": 1, "name": kind}, "genres": [{"id": 1, "name": "Science Fiction"}]},
                "title": "", "name": [], "desc": f"The {kind.lower()} of {s} number {n}.", "isbn": "9780000000019" if i == 1 else "",
                "credits": [{"id": k, "creator": who, "role": [{"id": 1, "name": role}]} for k, (who, role) in enumerate(credits)],
                "resource_url": f"https://metron.cloud/issue/{s.lower()}-{n}/"})
        if path == "/api/publisher/":
            return httpx.Response(200, json={"count": 1, "next": None, "results": [{"id": 1, "name": "Lantern Press"}]})
        return httpx.Response(404)


def test_comic_names():
    cases = {
        "Moonwake Vol. 3": ("Moonwake", "3", True), "Ironclad Hearts #31": ("Ironclad Hearts", "31", False),
        "Neon Rangers 088": ("Neon Rangers", "88", False), "starlight-drifters-vol-4": ("starlight drifters", "4", True),
        "Glass Harbor Vol. 4: The Long Tide": ("Glass Harbor", "4", True), "LAST LIGHT HC (Digital)": ("LAST LIGHT", "", True),
        "Rust Cowboy (Vol. 1) #1": ("Rust Cowboy", "1", False), "The Wide Valley": ("The Wide Valley", "", None),
    }
    for title, want in cases.items():
        name = lookups.comic_name(title)
        assert (name.series, name.number, name.collected) == want, title
    assert lookups.comic_name("Ironclad (1990-2002) #212").year == "1990"
    assert lookups.comic_name("A Quiet Week, Part 6", "Neon Rangers 2", "6").series == "Neon Rangers 2"
    # zeros go only from the whole part of a number: a half issue is asked about as 0.5, not as ".5"
    for title, series, number, want in (("A Quiet Week", "Neon Rangers", "0.5", "0.5"), ("Neon Rangers #0.5", "", "", "0.5"),
                                        ("A Quiet Week", "Neon Rangers", "00.5", "0.5"), ("Neon Rangers #007", "", "", "7"),
                                        ("A Quiet Week", "Neon Rangers", "007", "7"), ("A Quiet Week", "Neon Rangers", "000", "0"),
                                        ("Neon Rangers #0", "", "", "0"), ("Neon Rangers 010", "", "", "10")):
        assert lookups.comic_name(title, series, number).number == want, (title, number)
    assert lookups.Source().key(q("h", "Neon Rangers #0.5")) == "comic:neon rangers|0.5|i|"  # the cache's key, as asked


def old_norm(text: str) -> str:
    """The old lookups.norm: only ASCII was kept, so a name in another script had no words at all."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


def test_a_name_in_plain_ascii_keeps_its_words_its_score_and_its_question(monkeypatch):
    names = ascii_titles()
    others = names[1:] + names[:1]
    books, comics = openlibrary.OpenLibrary(), metron.Metron(TOKEN)

    def read() -> list[tuple]:
        return [(lookups.norm(a), lookups.similar(a, b), books.key(q("x", a, kind="book", authors=[b])),
                 books.key(q("x", a, kind="book")), comics.key(q("x", a, publisher=b)), comics.key(q("x", a, series=b, number="3")))
                for a, b in zip(names, others)]

    now = read()
    monkeypatch.setattr(lookups, "norm", old_norm)  # similar, comic_name and Source.key all go by the module's name
    before = read()
    wrong = [(name, new, old) for name, new, old in zip(names, now, before) if new != old]
    assert not wrong, wrong[:5]  # so no title in plain ASCII is asked again, and none is matched otherwise


def test_a_title_in_any_script_has_words_a_question_and_a_score_of_its_own():
    assert lookups.norm("Книга первая") == "книга первая" and lookups.norm("Tähti") == "tahti" and lookups.norm("ガラスの港")
    assert lookups.norm("ΟΔΟΣ") == lookups.norm("Οδός") == "οδοσ"
    assert lookups.norm("ＴＨＥ Ｍｏｏｎ ＆ the Wolf") == lookups.norm("The Moon & the Wolf") == "moon and the wolf"  # full width
    books = openlibrary.OpenLibrary()
    keys = {books.key(q(str(n), t, kind="book")) for n, t in enumerate(["Книга первая", "Книга вторая", "Οδός", "ガラスの港", "ガラスの道"])}
    assert len(keys) == 5 and "t:|" not in keys  # not one question, and so one answer, for them all
    assert books.key(q("a", "Книга первая", kind="book", authors=["Анна Писатель"])) == "t:книга первая|анна писатель"
    assert metron.Metron(TOKEN).key(q("c", "Лунная река #3")) == "comic:лунная река|3|i|"
    assert lookups.similar("Книга первая", "книга  ПЕРВАЯ") == 1.0 and lookups.similar("Οδός", "ΟΔΟΣ") == 1.0
    assert 0 < lookups.similar("Книга первая", "Книга вторая") < lookups.accept() and lookups.similar("ガラスの港", "Moonwake") == 0


def test_titles_in_another_script_are_each_asked_about_and_matched_by_their_words(tmp_path):
    works = {"Книга первая": "Первая книга о море.", "Книга вторая": "Вторая книга о море.", "Οδός": "Ένας δρόμος."}
    pages = {f"/works/OL{n}W.json": text for n, text in enumerate(works.values())}
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, p = request.url.path, request.url.params
        if path == "/search.json":
            asked.append(p["title"])
            doc = {"key": f"/works/OL{list(works).index(p['title'])}W", "title": p["title"].upper(), "subject": ["Море"]}
            return httpx.Response(200, json={"docs": [doc]})
        return httpx.Response(200, json={"description": pages[path]})

    clock, source = Clock(), openlibrary.OpenLibrary()
    titles = [q(str(n), title, kind="book") for n, title in enumerate(works)]
    stats = lookups.run(tmp_path, titles, [source], transport=httpx.MockTransport(handler), sleep=clock.sleep, now=clock.now,
                        log=lambda t: None)["openlibrary"]
    assert sorted(asked) == sorted(works) and stats["found"] == 3  # each one asked, and its answer taken by its words
    answers = lookups.Answers(tmp_path)
    assert [answers.for_query(t)["description"] for t in titles] == list(works.values())


def test_metron_tells_a_collected_edition_from_an_issue(tmp_path):
    fake, clock, source = FakeMetron(), Clock(), metron.Metron(TOKEN)
    s = session_for(source, fake.handler, clock, tmp_path)
    tpb = source.find(s, q("h:mw3", "Moonwake Vol. 3", publisher="Lantern Press"))
    assert tpb["authors"] == ["Ann Writer", "Bea Artist"] and tpb["description"].startswith("The trade paperback")
    assert (tpb["series"], tpb["number"], tpb["year"], tpb["isbn"]) == ("Moonwake", "3", "2014", "9780000000019")
    assert tpb["url"] == "https://metron.cloud/issue/moonwake-3/" and tpb["score"] >= lookups.accept()
    issue = source.find(s, q("c:mw3", "Moonwake #3"))
    assert issue["description"].startswith("The ongoing series")
    assert source.find(s, q("x", "Ironclad Hearts #31"))["authors"] == ["Cy Scribe", "Dee Penciller"]
    assert source.find(s, q("y", "Nothing Like It Vol. 1")) is None
    waits = len(fake.requests) - 1  # never faster than its interval: a wait before every request but the first
    assert waits > 0 and len(clock.slept) == waits and all(w >= source.interval - 0.01 for w in clock.slept)
    assert all(r.headers["user-agent"].startswith("hylly/") for r in fake.requests)


def test_a_refused_token_and_a_full_day(tmp_path):
    fake, clock = FakeMetron(), Clock()
    with pytest.raises(lookups.Refused) as e:
        metron.Metron("wrong" * 5).find(session_for(metron.Metron("wrong" * 5), fake.handler, clock, tmp_path), q("a", "Moonwake Vol. 3"))
    assert "wrong" not in str(e.value)
    source = metron.Metron(TOKEN)
    fake.answers.append(httpx.Response(200, json={"count": 0, "next": None, "results": []},
                                       headers={"X-RateLimit-Sustained-Remaining": "0", "X-RateLimit-Sustained-Reset": "3600"}))
    s = session_for(source, fake.handler, clock, tmp_path)
    assert source.find(s, q("a", "Moonwake Vol. 3")) is None  # that answer was empty, and the day is now spent
    with pytest.raises(lookups.Quota):
        source.find(s, q("b", "Ironclad Hearts #31"))
    notes = []
    fake.answers.append(httpx.Response(429, headers={"Retry-After": "7200"}))
    stats = lookups.run(tmp_path, [q("c", "Glass Harbor Vol. 4")], [source], transport=httpx.MockTransport(fake.handler),
                        sleep=clock.sleep, now=clock.now, log=lambda t: None, notify=lambda t, x: notes.append(t))
    assert stats["metron"]["stopped"] and notes == []  # a spent quota is pacing, not a problem to pop up about


def test_session_waits_retries_and_keeps_its_budget(tmp_path):
    clock, fake = Clock(), FakeMetron()
    fake.answers += [httpx.Response(503), httpx.Response(429, headers={"Retry-After": "30"})]
    source = metron.Metron(TOKEN)
    s = session_for(source, fake.handler, clock, tmp_path)
    assert source.find(s, q("w", "Ironclad Hearts #31"))["authors"]  # through a 503 and a short 429
    assert 30 in [round(x) for x in clock.slept]
    fake.answers.append(httpx.Response(429, headers={"Retry-After": "7200"}))  # two hours: not worth waiting in a run
    with pytest.raises(lookups.Quota):
        source.find(s, q("w2", "Ironclad Hearts #31"))

    hourly = lookups.Budget(tmp_path / "hour.json", per_hour=3, per_day=0, now=clock.now)
    for _ in range(3):
        hourly.note()
    again = lookups.Budget(tmp_path / "hour.json", per_hour=3, per_day=0, now=clock.now)  # a restart remembers
    assert 3500 < again.wait() <= 3601
    daily = lookups.Budget(tmp_path / "day.json", per_hour=0, per_day=2, now=clock.now)
    daily.note(), daily.note()
    assert daily.wait() == float("inf")


def test_a_long_wait_is_told(tmp_path):
    clock, fake, logged = Clock(), FakeMetron(), []
    s = session_for(metron.Metron(TOKEN), fake.handler, clock, tmp_path, per_hour=3)
    s.log = logged.append
    for _ in range(3):
        s.get("https://metron.cloud/api/publisher/")
    assert not logged  # seconds apart, at its pace: nothing to tell
    s.get("https://metron.cloud/api/publisher/")
    assert len(logged) == 1 and "Metron: this hour's 3 requests are used; waiting until " in logged[0]
    assert logged[0].endswith(", then it goes on.") and max(clock.slept) > 3500
    fake.answers.append(httpx.Response(429, headers={"Retry-After": "120"}))
    s.get("https://metron.cloud/api/publisher/")
    assert "Metron asked to slow down; waiting until " in logged[-1]


def test_the_time_to_go_counts_the_hourly_budget(tmp_path, monkeypatch):
    clock = Clock()
    hourly = lookups.Budget(tmp_path / "pace.json", per_hour=3, per_day=0, now=clock.now)
    assert hourly.done_by(3, interval=2, last=0) == clock.t + 4  # three, two seconds apart
    for _ in range(3):
        hourly.note()
    assert hourly.done_by(1, interval=2, last=clock.t) == clock.t + 3601  # the hour's three are used
    assert hourly.done_by(4, interval=2, last=clock.t) == clock.t + 7202  # and the fourth waits for the next hour's

    class Slow(metron.Metron):
        per_hour = 2

    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    fake, logged = FakeMetron(), []
    titles = [q("h:mw3", "Moonwake Vol. 3"), q("h:ih31", "Ironclad Hearts #31"), q("h:none", "Nothing Like It Vol. 1")]
    lookups.run(tmp_path, titles, [Slow(TOKEN)], transport=httpx.MockTransport(fake.handler), sleep=clock.sleep,
                now=clock.now, log=logged.append, progress_every=0)
    first = next(line for line in logged if "1/3 looked up" in line)
    assert re.search(r"; about \d+\.\d hours to go, at 2 requests an hour\.$", first)  # hours, held back by the budget
    assert any("this hour's 2 requests are used" in line for line in logged)


def test_a_run_is_incremental_resumable_and_notifies(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    clock, fake, notes, logged = Clock(), FakeMetron(), [], []
    titles = [q("h:mw3", "Moonwake Vol. 3"), q("h:ih31", "Ironclad Hearts #31"), q("h:none", "Nothing Like It Vol. 1"),
              q("h:ih31-copy", "Ironclad Hearts #31"), q("h:has", "Moonwake #3", has=frozenset({"authors", "description"}))]
    chosen, skipped = lookups.available(["metron"])
    assert [s.id for s in chosen] == ["metron"] and not skipped
    run = lambda **kw: lookups.run(tmp_path, titles, chosen, transport=httpx.MockTransport(fake.handler), sleep=clock.sleep,  # noqa: E731
                                   now=clock.now, log=logged.append, notify=lambda t, x: notes.append((t, x)), **kw)
    first = run(limit=2)["metron"]  # the same title twice is asked once; one that has its details isn't asked
    assert (first["asked"], first["found"], first["left"]) == (2, 2, 1)
    second = run()["metron"]
    assert (second["asked"], second["none"]) == (1, 1)  # continues where the first stopped
    assert run()["metron"]["asked"] == 0 and "nothing new" in logged[-1]
    answers = lookups.Answers(tmp_path)
    moon = answers.for_query(titles[0])
    assert moon["authors"] == ["Ann Writer", "Bea Artist"] and moon["lk"] == [["Metron", "https://metron.cloud/issue/moonwake-3/"]]
    assert answers.for_query(titles[3])["authors"]  # the copy shares the answer
    text = (tmp_path / "state" / "lookups" / "lookup-metron.jsonl").read_text(encoding="utf-8")
    assert TOKEN not in text and TOKEN not in json.dumps(logged)

    fake.answers += [httpx.Response(401)]
    assert run(again=True)["metron"]["stopped"].startswith("Metron refused the login")
    assert notes and notes[-1][0] == "Library lookups: Metron stopped"


def test_three_unreadable_answers_stop_a_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    clock, fake, notes = Clock(), FakeMetron(), []
    fake.answers += [httpx.Response(200, text="<html>maintenance</html>")] * 3
    titles = [q(f"t{i}", f"Moonwake Vol. {i}") for i in range(1, 6)]
    stats = lookups.run(tmp_path, titles, lookups.available(["metron"])[0], transport=httpx.MockTransport(fake.handler),
                        sleep=clock.sleep, now=clock.now, log=lambda t: None, notify=lambda t, x: notes.append(t))["metron"]
    assert stats["errors"] == 3 and "may need an update" in stats["stopped"] and notes
    assert lookups.run(tmp_path, titles, lookups.available(["metron"])[0], transport=httpx.MockTransport(fake.handler),
                       sleep=clock.sleep, now=clock.now, log=lambda t: None)["metron"]["found"] >= 1  # errors are asked again


def test_a_local_problem_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    notes = []

    def broken(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("hylly.storage.append_line", broken)
    with pytest.raises(lookups.LookupProblem, match="Can't write the lookup cache"):
        lookups.run(tmp_path, [q("a", "Moonwake Vol. 3")], lookups.available(["metron"])[0], transport=httpx.MockTransport(FakeMetron().handler),
                    sleep=lambda s: None, log=lambda t: None, notify=lambda t, x: notes.append(t))
    assert not notes  # the job's own note says so: cli._watched, and test_bell_notes


def test_a_library_whose_disk_goes_mid_run_stops_the_run(tmp_path, monkeypatch):
    """A run once went on writing its answers, and spending the day's budget on them, onto the disk an unmounted one
    leaves behind. It looks every LIBRARY_EVERY titles, and stops as a download does; what it looked up is kept."""
    from hylly import paths

    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    paths.marker_file(tmp_path).parent.mkdir(parents=True)
    paths.marker_file(tmp_path).write_text('{"id": "made-up"}', encoding="utf-8")
    fake, logged = FakeMetron(), []
    titles = [q(f"h:mw{n}", f"Moonwake Vol. {n}") for n in range(1, 31)]

    def the_disk_goes(request):
        if len(fake.requests) == 12:
            paths.marker_file(tmp_path).unlink()  # the disk unplugged: on Linux and macOS, an empty folder is left
        return fake.handler(request)

    with pytest.raises(lookups.LookupProblem, match="isn't there any more, so the run stopped. What was looked up is kept."):
        lookups.run(tmp_path, titles, lookups.available(["metron"])[0], transport=httpx.MockTransport(the_disk_goes),
                    sleep=lambda s: None, log=logged.append)
    kept = (tmp_path / "state" / "lookups" / "lookup-metron.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(kept) == lookups.LIBRARY_EVERY * 2 and "isn't there any more" in logged[-1]


# ---- Comic Vine and Open Library, faked -----------------------------------------------------

def comicvine_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.params
    if p.get("api_key") != TOKEN:
        return httpx.Response(200, json={"status_code": 100, "error": "Invalid API Key", "results": []})
    path = request.url.path
    if path == "/api/search/":
        return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": [
            {"id": 7, "name": "Ironclad Hearts", "start_year": "1995", "publisher": {"name": "Lantern"}, "count_of_issues": 185},
            {"id": 8, "name": "Ironclad Hearts: Second Wind", "start_year": "2004", "publisher": {"name": "Lantern"}, "count_of_issues": 1}]})
    if path == "/api/issues/":
        assert p["filter"] == "volume:7,issue_number:31"
        return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": [{"id": 555, "issue_number": "31"}]})
    if path == "/api/issue/4000-555/":
        return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": {
            "issue_number": "31", "cover_date": "1999-03-01", "description": "<p>Mara &amp; the <i>shield</i>.</p>",
            "person_credits": [{"name": "Eve Plotter", "role": "writer"}, {"name": "Finn Inker", "role": "artist, cover"}],
            "volume": {"name": "Ironclad Hearts"}, "site_detail_url": "https://comicvine.gamespot.com/ironclad-hearts-31/4000-555/"}})
    return httpx.Response(404)


def test_comic_vine(tmp_path):
    clock, source = Clock(), comicvine.ComicVine(TOKEN)
    found = source.find(session_for(source, comicvine_handler, clock, tmp_path), q("x", "Ironclad Hearts #31", publisher="Lantern Press"))
    assert found["authors"] == ["Eve Plotter", "Finn Inker"] and found["description"] == "Mara & the shield ."
    assert (found["year"], found["publisher"], found["url"].endswith("/4000-555/")) == ("1999", "Lantern", True)
    with pytest.raises(lookups.Refused):
        bad = comicvine.ComicVine("w" * 40)
        bad.find(session_for(bad, comicvine_handler, clock, tmp_path), q("x", "Ironclad Hearts #31"))


def answered_otherwise(fake, where: str, change):
    """The fake site's answers, with those to paths starting with where changed by change (their JSON, as the site would
    send it): a site that has changed the shape of its answers."""
    def handler(request: httpx.Request) -> httpx.Response:
        r = fake(request)
        return httpx.Response(r.status_code, json=change(r.json())) if request.url.path.startswith(where) else r

    return handler


def asked_once(tmp_path, source, handler, title) -> tuple[dict, dict]:
    """One title asked of one source as hylly lookup asks it, through the run: its counts, and what the cache kept."""
    clock = Clock()
    stats = lookups.run(tmp_path, [title], [source], transport=httpx.MockTransport(handler), sleep=clock.sleep,
                        now=clock.now, log=lambda t: None, notify=lambda t, x: None)[source.id]
    kept = lookups.Cache(tmp_path / "state" / "lookups" / f"lookup-{source.id}.jsonl").get(source.key(title))
    return stats, kept or {}


def as_the_docs_say(stats: dict, kept: dict, outcome: str, words: str) -> None:
    """docs/lookups.md: an answer of a shape the plugin doesn't know is Changed, raised by the plugin itself with words
    that say what was odd, never a KeyError, TypeError or AttributeError it let out (which the run would count as
    Changed too, as a slip in the plugin); one that holds nothing is None. Either way the run goes on, and a found
    answer has names, not letters. words: what the error says."""
    if outcome == "changed":
        assert stats["errors"] == 1 and stats["found"] == stats["none"] == 0 and kept["status"] == "error", (stats, kept)
        let_out = "the plugin couldn't read an answer"  # the run's words for what a plugin let out (lookups.slip)
        assert words in kept["error"] and let_out not in kept["error"], kept["error"]
    elif outcome == "none":
        assert stats["none"] == 1 and stats["errors"] == stats["found"] == 0, (stats, kept)
    else:
        assert stats["found"] == 1 and stats["errors"] == 0, (stats, kept)
        answer = kept["found"]
        assert all(isinstance(names, list) and all(isinstance(n, str) and len(n) > 1 for n in names)
                   for names in (answer.get("authors", []), answer.get("subjects", []))), answer


CV_ODD = {  # what changed in Comic Vine's answers: (path, the change, as the docs say it ends, words its error has)
    "the answer a list, not an object": ("/api/search/", lambda d: [d], "changed", "in a shape this plugin doesn't know"),
    "the results an object, not a list": ("/api/search/", lambda d: {**d, "results": {"id": 7}}, "changed",
                                          "search answer looks different"),
    "a volume without its id": ("/api/search/", lambda d: {**d, "results": [{k: v for k, v in x.items() if k != "id"}
                                                                             for x in d["results"]]}, "none", ""),
    "a volume's id as text": ("/api/search/", lambda d: {**d, "results": [{**x, "id": "seven"} for x in d["results"]]},
                              "changed", "search answer looks different"),
    "a volume's id as a fraction": ("/api/search/", lambda d: {**d, "results": [{**x, "id": 7.5} for x in d["results"]]},
                                    "changed", "search answer looks different"),
    "a volume's name as a number": ("/api/search/", lambda d: {**d, "results": [{**x, "name": 31} for x in d["results"]]},
                                    "changed", "search answer looks different"),
    "its status as text": ("/api/search/", lambda d: {**d, "status_code": "1"}, "changed", "answered status 1 (OK)"),
    "an issue without its id": ("/api/issues/", lambda d: {**d, "results": [{"issue_number": "31"}]}, "changed",
                                "list of a volume's issues looks different"),
    "the issues an object, not a list": ("/api/issues/", lambda d: {**d, "results": {"id": 555}}, "changed",
                                         "list of a volume's issues looks different"),
    "the issue's details a list": ("/api/issue/4000-555/", lambda d: {**d, "results": [d["results"]]}, "none", ""),
    "the credits as text": ("/api/issue/4000-555/", lambda d: {**d, "results": {**d["results"], "person_credits": "Eve Plotter"}},
                            "changed", "issue's details look different"),
    "a credit's roles as a list": ("/api/issue/4000-555/", lambda d: {**d, "results": {**d["results"], "person_credits": [
        {"name": "Eve Plotter", "role": ["writer"]}]}}, "changed", "issue's details look different"),
    "the description as a number": ("/api/issue/4000-555/", lambda d: {**d, "results": {**d["results"], "description": 5}},
                                    "changed", "issue's details look different"),
    "a count of issues as text": ("/api/search/", lambda d: {**d, "results": [{**x, "count_of_issues": "many"}
                                                                              for x in d["results"]]}, "found", ""),
}


@pytest.mark.parametrize("odd", CV_ODD)
def test_comic_vine_answering_in_another_shape_is_changed_or_nothing_and_the_run_goes_on(tmp_path, odd):
    where, change, outcome, words = CV_ODD[odd]
    stats, kept = asked_once(tmp_path, comicvine.ComicVine(TOKEN), answered_otherwise(comicvine_handler, where, change),
                             q("x", "Ironclad Hearts #31", publisher="Lantern Press"))
    as_the_docs_say(stats, kept, outcome, words)


@pytest.mark.parametrize("code, error, outcome", [(107, "Rate limit exceeded", "quota"), (101, "Object Not Found", "none"),
                                                   (104, "Filter Error", "changed"), (105, "Subscriber only video", "changed")])
def test_comic_vine_s_own_status_codes(tmp_path, code, error, outcome):
    """107 is its rate limit, which stops it for the run as a spent budget does (no failure); 101 is nothing found; any
    other but 1 is an answer this plugin doesn't know, said with Comic Vine's own words."""
    source = comicvine.ComicVine(TOKEN)
    handler = answered_otherwise(comicvine_handler, "/api/search/", lambda d: {**d, "status_code": code, "error": error, "results": []})
    stats, kept = asked_once(tmp_path, source, handler, q("x", "Ironclad Hearts #31"))
    if outcome == "quota":
        assert stats["quota"] and "rate limit is reached" in stats["stopped"] and stats["errors"] == 0 and not kept
    else:
        as_the_docs_say(stats, kept, outcome, f"answered status {code} ({error})")


def each_issue(change):
    """A change to each issue of Metron's list of them, its details left as they are."""
    return lambda d: {**d, "results": [change(x) for x in d["results"]]} if "results" in d else d


MT_ODD = {  # what changed in Metron's answers: (path, the change, as the docs say it ends, words its error has)
    "an issue's id as text": ("/api/issue/", each_issue(lambda x: {**x, "id": "seven"}), "changed", "issue list looks different"),
    "an issue without its id": ("/api/issue/", each_issue(lambda x: {k: v for k, v in x.items() if k != "id"}), "changed",
                                "issue list looks different"),
    "an issue's id as plain digits": ("/api/issue/", each_issue(lambda x: {**x, "id": str(x["id"])}), "found", ""),
}


@pytest.mark.parametrize("odd", MT_ODD)
def test_metron_answering_with_ids_of_another_shape_is_changed_and_the_run_goes_on(tmp_path, odd):
    """An issue's id goes in the path of the question about its details: one that isn't a whole number is an answer the
    plugin doesn't know, said in its own words."""
    where, change, outcome, words = MT_ODD[odd]
    stats, kept = asked_once(tmp_path, metron.Metron(TOKEN), answered_otherwise(FakeMetron().handler, where, change),
                             q("x", "Moonwake Vol. 3"))
    as_the_docs_say(stats, kept, outcome, words)


def test_comic_vine_is_asked_only_what_metron_did_not_find(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    monkeypatch.setenv("HYLLY_COMICVINE_KEY", TOKEN)
    asked = []

    def both(request):
        asked.append(request.url.host)
        return (FakeMetron().handler if request.url.host == "metron.cloud" else comicvine_handler)(request)

    titles = [q("a", "Moonwake Vol. 3"), q("b", "Nothing Like It Vol. 1")]
    lookups.run(tmp_path, titles, lookups.available(["metron", "comicvine"])[0], transport=httpx.MockTransport(both),
                sleep=lambda s: None, log=lambda t: None)
    assert asked.count("comicvine.gamespot.com") == 1  # only "Nothing Like It": Metron found Moonwake


def openlibrary_handler(request: httpx.Request) -> httpx.Response:
    path, p = request.url.path, request.url.params
    if path == "/search.json":
        title = p["title"].lower()
        docs = [{"key": "/works/OL1W", "title": "The Novel", "subject": ["Fiction"], "author_name": ["Ann Writer"]}] if "novel" in title \
            else [{"key": "/works/OL2W", "title": "Something Else Entirely", "subject": ["X"]}]
        return httpx.Response(200, json={"docs": docs})
    if path == "/isbn/9780000000002.json":
        return httpx.Response(200, json={"works": [{"key": "/works/OL1W"}]})
    if path.startswith("/works/"):
        return httpx.Response(200, json={"description": {"value": "A novel about novels."}, "subjects": ["Books"]})
    return httpx.Response(404)


def test_open_library_checks_titles_and_asks_once(tmp_path):
    clock, source = Clock(), openlibrary.OpenLibrary()
    cache = tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True)
    old = q("o", "Old Book", kind="book", authors=["Ann Writer"])
    cache.write_text(json.dumps({"key": source.key(old), "ids": ["o"], "status": "found", "at": "2026-11-03T10:00:00",
                                 "found": {"description": "Asked before.", "subjects": ["Old"], "score": 1.0}}) + "\n"
                     + '{"key": "cut short', encoding="utf-8")
    assert lookups.Answers(tmp_path).for_query(old)["description"] == "Asked before."
    s = session_for(source, openlibrary_handler, clock, tmp_path)
    found = source.find(s, q("n", "The Novel", kind="book", authors=["Ann Writer"]))
    assert found["description"] == "A novel about novels." and found["authors"] == ["Ann Writer"] and found["score"] >= lookups.accept()
    assert source.find(s, q("i", "Whatever", kind="book", isbn="9780000000002"))["description"] == "A novel about novels."
    stats = lookups.run(tmp_path, [old, q("w", "A Different Book", kind="book")], [source],
                        transport=httpx.MockTransport(openlibrary_handler), sleep=clock.sleep, now=clock.now, log=lambda t: None)
    assert stats["openlibrary"]["asked"] == 1 and stats["openlibrary"]["none"] == 1  # not the old one; a far-off title isn't taken


OL_ODD = {  # what changed in Open Library's answers: (path, the change, as the docs say it ends, words its error has)
    "the search's answer a list, not an object": ("/search.json", lambda d: [d], "changed", "search answer looks different"),
    "its docs an object, not a list": ("/search.json", lambda d: {"docs": d["docs"][0]}, "changed", "search answer looks different"),
    "a work found without its key": ("/search.json", lambda d: {"docs": [{k: v for k, v in d["docs"][0].items() if k != "key"}]},
                                     "none", ""),
    "the authors as text": ("/search.json", lambda d: {"docs": [{**d["docs"][0], "author_name": "Ann Writer"}]}, "changed",
                            "search answer looks different"),
    "the subjects as text": ("/search.json", lambda d: {"docs": [{**d["docs"][0], "subject": "Fiction"}]}, "changed",
                             "search answer looks different"),
    "the year as text": ("/search.json", lambda d: {"docs": [{**d["docs"][0], "first_publish_year": "1999"}]}, "found", ""),
    "a title as a number": ("/search.json", lambda d: {"docs": [{**d["docs"][0], "title": 1984}]}, "changed",
                            "search answer looks different"),
    "an author as a number": ("/search.json", lambda d: {"docs": [{**d["docs"][0], "author_name": ["Ann Writer", 5]}]},
                              "changed", "search answer looks different"),
    "the work's answer a list": ("/works/", lambda d: [d], "changed", "answer about a work looks different"),
    "the work's subjects as text": ("/works/", lambda d: {**d, "subjects": "Books"}, "changed", "answer about a work looks different"),
    "the description's value a number": ("/works/", lambda d: {**d, "description": {"value": 5}}, "changed",
                                         "answer about a work looks different"),
    "the description a number": ("/works/", lambda d: {**d, "description": 5}, "changed", "answer about a work looks different"),
    "the description as text": ("/works/", lambda d: {**d, "description": "A novel about novels."}, "found", ""),
    "an edition's works an object, not a list": ("/isbn/", lambda d: {"works": d["works"][0]}, "changed",
                                                 "answer about an edition looks different"),
}


@pytest.mark.parametrize("odd", OL_ODD)
def test_open_library_answering_in_another_shape_is_changed_or_nothing_and_the_run_goes_on(tmp_path, odd):
    where, change, outcome, words = OL_ODD[odd]
    title = q("i", "Whatever", kind="book", isbn="9780000000002") if where == "/isbn/" else q("n", "The Novel", kind="book")
    stats, kept = asked_once(tmp_path, openlibrary.OpenLibrary(), answered_otherwise(openlibrary_handler, where, change), title)
    as_the_docs_say(stats, kept, outcome, words)


@pytest.mark.parametrize("key", [".example.invalid/x", "/works/OL1W/../../people", "/authors/OL2A", "/works/OL1Wx", 7])
def test_an_open_library_work_named_otherwise_than_its_own_keys_are_is_changed_and_never_asked(tmp_path, key):
    """A work's key goes straight after Open Library's address: ".example.invalid/x" would send the next request to
    another host, whose answer would be taken as the description. Only /works/OL<digits>W is asked about."""
    clock, source, asked = Clock(), openlibrary.OpenLibrary(), []

    def odd(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/isbn/9780000000002.json":
            return httpx.Response(200, json={"works": [{"key": key}]})
        return openlibrary_handler(request)

    with pytest.raises(lookups.Changed, match="named a work in a way this plugin doesn't know"):
        source.find(session_for(source, odd, clock, tmp_path), q("i", "Whatever", kind="book", isbn="9780000000002"))
    assert asked == ["openlibrary.org/isbn/9780000000002.json"]


@pytest.mark.parametrize("where, resolved", [("http://openlibrary.org/works/OL1W.json", []),
                                             ("https://192.168.1.20/works/OL1W.json", []),
                                             ("https://[fd7a:115c:a1e0::7]/works/OL1W.json", []),
                                             ("https://lantern.example.org/works/OL1W.json", ["127.0.0.1"])])
def test_a_source_sending_hylly_on_over_plain_http_or_to_an_address_at_home_is_changed_and_never_followed(
        tmp_path, monkeypatch, where, resolved):
    """A lookup follows a source's redirect only over https and only to the internet: never to this computer, the home
    network or the tailnet, whose answer would be taken as a title's details. What's said names the path asked, never
    the address it was sent on to."""
    from hylly import fetching

    monkeypatch.setattr(fetching, "resolve", lambda host, port: resolved if host == "lantern.example.org" else [])
    clock, source, asked = Clock(), openlibrary.OpenLibrary(), []

    def moved(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.url.scheme}://{request.url.host}{request.url.path}")
        if request.url.path == "/works/OL1W.json" and request.url.host == "openlibrary.org":
            return httpx.Response(301, headers={"location": where})
        return openlibrary_handler(request)

    with pytest.raises(lookups.Changed) as changed:
        source.find(session_for(source, moved, clock, tmp_path), q("i", "Whatever", kind="book", isbn="9780000000002"))
    assert asked == ["https://openlibrary.org/isbn/9780000000002.json", "https://openlibrary.org/works/OL1W.json"]
    said = str(changed.value)
    assert "/works/OL1W.json led to an address" in said and "192.168" not in said and "lantern" not in said
    assert ("isn't https" in said) == where.startswith("http:")
    monkeypatch.setattr(fetching, "resolve", lambda host, port: ["11.22.33.44"])  # an address on the internet: followed
    asked.clear()
    found = source.find(session_for(source, lambda request: (
        httpx.Response(301, headers={"location": "https://lantern.example.org/works/OL1W.json"})
        if request.url.host == "openlibrary.org" and request.url.path == "/works/OL1W.json" else openlibrary_handler(request)),
        clock, tmp_path), q("i", "Whatever", kind="book", isbn="9780000000002"))
    assert found["description"] == "A novel about novels."


def test_a_connection_gone_before_the_address_it_reached_is_read_is_network_trouble_not_a_slip(tmp_path, monkeypatch):
    """The system can't say where a reset or closed connection went (getpeername): that is asked again as network trouble,
    and after every try the source is Transient, as for any connection that fails, never the plugin's own slip."""
    from hylly import fetching

    monkeypatch.setattr(fetching.urllib.request, "getproxies", lambda: {})  # no proxy: the address reached is looked at
    clock, source, asked = Clock(), openlibrary.OpenLibrary(), []

    def gone(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json={"docs": []}, extensions={"network_stream": Gone()})

    with pytest.raises(lookups.Transient, match="no answer from /search.json after 6 tries \\(ReadError\\)"):
        source.find(session_for(source, gone, clock, tmp_path), q("t", "The Novel", kind="book"))
    assert asked == ["/search.json"] * lookups.RETRIES


def test_a_question_asks_with_the_titles_own_details_not_with_what_a_lookup_found():
    source = openlibrary.OpenLibrary()
    own = {"id": "x", "t": "A Lone Title", "k": "book"}
    found = {**own, "a": ["Ann Writer"], "isbn": "9780000000002", "d": "Found.", "lf": ["a", "isbn", "d"]}
    question = lookups.Query.from_record(found)
    assert source.key(question) == source.key(lookups.Query.from_record(own))  # the same question after an answer as before
    assert question.authors == [] and question.isbn == "" and not question.has  # and what it found isn't counted as its own
    mine = lookups.Query.from_record({**found, "lf": ["isbn", "d"]})
    assert mine.authors == ["Ann Writer"] and "authors" in mine.has  # the file's own author is its own


def test_a_catalog_built_with_answers_asks_nothing_again_and_your_fix_is_your_own(tmp_path):
    build_library(tmp_path)
    folder = tmp_path / "Notes"
    folder.mkdir()
    bare = re.sub(r"\s*<dc:(creator|identifier|publisher)\b.*?</dc:\1>", "", OPF.replace("The Long Dust", "A Lone Title"), flags=re.S)
    make_epub(folder / "A Lone Title.epub", opf=bare, cover=False)
    catalog.build(tmp_path, log=lambda t: None)
    source = openlibrary.OpenLibrary()
    asked = {}
    lines = []
    for it in Catalog(tmp_path).get():
        if it["t"] == "A Lone Title":
            assert not it.get("a") and not it.get("isbn") and not it.get("lf")
            asked[it["id"]] = source.key(lookups.Query.from_record(it))
            lines.append(json.dumps({"key": asked[it["id"]], "ids": [it["id"]], "status": "found", "at": "2026-11-04T10:00:00",
                                     "found": {"authors": ["Ann Writer"], "isbn": "9780000000002", "score": 1.0,
                                               "year": "2014",  # the book's own year: asked by title alone, what vouches
                                               "url": "https://openlibrary.org/works/OL1W"}}))  # for the match's authors
    assert asked
    cache = tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(lines) + "\n", encoding="utf-8")
    catalog.build(tmp_path, log=lambda t: None)  # the answers are in the records now, and the records say so
    titles = {it["id"]: it for it in Catalog(tmp_path).get()}
    for id_, key in asked.items():
        assert titles[id_]["a"] == ["Ann Writer"] and titles[id_]["isbn"] == "9780000000002" and {"a", "isbn"} <= set(titles[id_]["lf"])
        question = lookups.Query.from_record(titles[id_])
        assert not question.authors and not question.isbn and source.key(question) == key  # asked as before the answer

    def nothing_more(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"asked again: {request.url}")

    clock = Clock()
    stats = lookups.run(tmp_path, [lookups.Query.from_record(titles[id_]) for id_ in asked], [source],
                        transport=httpx.MockTransport(nothing_more), sleep=clock.sleep, now=clock.now, log=lambda t: None)
    assert stats["openlibrary"]["asked"] == 0

    fixed = titles[next(iter(asked))]
    curation.change_single(tmp_path, "authors", "Bo Writer", [fixed], by="this PC")
    fixed = {it["id"]: it for it in Catalog(tmp_path).get()}[fixed["id"]]
    assert fixed["a"] == ["Bo Writer"] and "a" not in fixed["lf"] and "isbn" in fixed["lf"]
    assert lookups.Query.from_record(fixed).authors == ["Bo Writer"]  # what you corrected is its own


def test_a_dry_run_counts_what_a_real_run_would_ask(tmp_path, monkeypatch, capsys):
    from hylly import cli

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda t: None)
    data = tmp_path / "catalog" / "data.js"
    text = data.read_text(encoding="utf-8")
    start, end = text.index("{"), text.rindex("}") + 1
    catalogued = json.loads(text[start:end])
    catalogued["items"] = [{"id": "a", "t": "Answered Title", "k": "book", "src": "Notes"},
                           {"id": "b", "t": "Waiting Title", "k": "book", "src": "Notes"}]
    data.write_text(text[:start] + json.dumps(catalogued) + text[end:], encoding="utf-8")
    source = openlibrary.OpenLibrary()
    answered = source.key(lookups.Query.from_record(catalogued["items"][0]))
    cache = tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": answered, "ids": ["a"], "status": "found", "at": "2026-11-04T10:00:00",
                                 "found": {"authors": ["Ann Writer"], "description": "Found.", "score": 1.0}}) + "\n", encoding="utf-8")
    monkeypatch.setattr(lookups, "in_order", lambda: {"openlibrary": openlibrary.OpenLibrary})
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([source], []))
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("a dry run asks nothing"))
    cli.main(["lookup", "--library", str(tmp_path), "--dry-run"])
    assert "Open Library: 1 titles to look up." in capsys.readouterr().out  # the one without an answer


def test_a_dry_run_shows_each_source_s_site_its_terms_and_when_it_was_checked(tmp_path, monkeypatch, capsys):
    from hylly import cli

    class Unchecked(lookups.Source):
        id, name, home, terms = "unchecked", "Unchecked DB", "https://example.org", "Data CC BY 4.0: link back."

    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "Moonwake #3", "k": "comic"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([comicvine.ComicVine("k"), Unchecked()], []))
    monkeypatch.setattr(lookups, "dry_run", lambda root, queries, chosen, **k: {s.id: {"left": 1, "waiting": 0} for s in chosen})
    cli.main(["lookup", "--library", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out
    assert (f"  https://comicvine.gamespot.com: Non-commercial use only; shown with a link to Comic Vine. "
            f"Checked against the site on {comicvine.ComicVine.verified}.") in out
    assert "  https://example.org: Data CC BY 4.0: link back. Not yet checked against the site." in out
    cli.main(["lookup", "--library", str(tmp_path), "--dry-run", "--json"])
    said = {s["id"]: s for s in json.loads(capsys.readouterr().out)["sources"]}
    assert said["comicvine"]["terms"].startswith("Non-commercial use only") and said["unchecked"]["verified"] == ""
    assert said["comicvine"]["home"] == "https://comicvine.gamespot.com" and said["comicvine"]["verified"] == comicvine.ComicVine.verified


def test_a_lookup_with_a_search_reads_the_catalog_once(tmp_path, monkeypatch, capsys):
    """hylly lookup --search asks about the titles its search finds among those it has read: data.js is read once, not
    once for the titles and again for the search."""
    from pathlib import Path

    from hylly import cli

    data = tmp_path / "catalog" / "data.js"
    data.parent.mkdir(parents=True)
    data.write_text("window.CATALOG = " + json.dumps({"items": [{"id": "c1", "t": "Moonwake #1", "k": "comic"},
                                                               {"id": "b1", "t": "The Quiet Orchard", "k": "book"}]}) + ";\n",
                    encoding="utf-8")
    reads, real = [], Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: (reads.append(path.name) if path == data else None) or real(path, *a, **k))
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([openlibrary.OpenLibrary()], []))
    asked = []
    monkeypatch.setattr(lookups, "dry_run", lambda root, queries, chosen, **k: asked.extend(queries) or {s.id: {"left": 1, "waiting": 0} for s in chosen})
    cli.main(["lookup", "--library", str(tmp_path), "--dry-run", "--search", "moonwake"])
    assert [query.id for query in asked] == ["c1"] and reads == ["data.js"]


def test_a_dry_run_is_the_plan_alone_and_counts_what_a_run_would_ask(tmp_path, monkeypatch, capsys):
    """hylly lookup --dry-run plans each source's turn as a run does (lookups.Plan), and asks nothing: no run, no
    request, nothing written. Over several sources, some titles answered, it counts what a run that may ask none (limit
    0) leaves to ask and to wait for, in the words it always had: a title the first found isn't asked of the next, one
    it found nothing for is, and one it hasn't answered yet waits."""
    from datetime import datetime

    from hylly import cli

    class First(lookups.Source):
        id, name, kinds = "first", "First DB", frozenset({"comic"})

    class Second(lookups.Source):
        id, name, kinds, home, terms = "second", "Second DB", frozenset({"comic"}), "https://example.org", "Data CC BY 4.0."

    class Third(lookups.Source):
        id, name, kinds, wants = "third", "Third DB", frozenset({"book", "comic"}), frozenset({"description"})

    titles = [q("c1", "Moonwake #1"), q("c2", "Moonwake #2"), q("c3", "Moonwake #3"),
              q("b1", "The Quiet Orchard", kind="book", authors=["Ann Writer"]), q("b2", "Salt and Ember", kind="book")]
    at = datetime.now().isoformat(timespec="seconds")
    folder = tmp_path / "state" / "lookups"
    folder.mkdir(parents=True)
    (folder / "lookup-first.jsonl").write_text("".join(json.dumps(line) + "\n" for line in (
        {"key": First().key(titles[0]), "ids": ["c1"], "status": "found", "at": at,
         "found": {"authors": ["Ann Writer"], "description": "Found.", "score": 1.0}},
        {"key": First().key(titles[1]), "ids": ["c2"], "status": "none", "at": at})), encoding="utf-8")
    monkeypatch.setattr(lookups, "in_order", lambda: {"first": First, "second": Second, "third": Third})
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([First(), Second(), Third()], []))
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": t.id, "t": t.title, "k": t.kind, **({"a": t.authors} if t.authors else {})}
                                                      for t in titles])
    with monkeypatch.context() as m:
        m.setattr(lookups, "run", lambda *a, **k: pytest.fail("a dry run runs nothing"))
        m.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("a dry run asks nothing"))
        cli.main(["lookup", "--library", str(tmp_path), "--dry-run"])
        assert capsys.readouterr().out.splitlines() == [
            "First DB: 1 titles to look up.", "  Not yet checked against the site.",
            "Second DB: 1 titles to look up, 1 more once the sources before it have answered.",
            "  https://example.org: Data CC BY 4.0. Not yet checked against the site.",
            "Third DB: 2 titles to look up, 2 more once the sources before it have answered.", "  Not yet checked against the site."]
        cli.main(["lookup", "--library", str(tmp_path), "--dry-run", "--json"])
        said = json.loads(capsys.readouterr().out)["sources"]
        assert [(s["id"], s["left"], s["waiting"]) for s in said] == [("first", 1, 0), ("second", 1, 1), ("third", 2, 2)]
        assert list(lookups.dry_run(tmp_path, titles, [Third(), First(), Second()])) == ["first", "second", "third"]
    for kw in ({}, {"again": True}, {"kinds": {"book"}}):
        ran = lookups.run(tmp_path, titles, [First(), Second(), Third()], limit=0, log=lambda line: None, **kw)
        assert lookups.dry_run(tmp_path, titles, [First(), Second(), Third()], **kw) == {
            sid: {"left": st["left"], "waiting": st["waiting"]} for sid, st in ran.items()}, kw
    assert [f.name for f in folder.iterdir()] == ["lookup-first.jsonl"]  # nor a budget's file, nor another cache


def test_a_none_answer_ages_by_its_moment_kept_with_its_offset_or_before_answers_had_one(tmp_path, monkeypatch):
    """An answer is kept with its time's offset (db.now); one kept before has the local time alone, read as this
    computer's then: the two side by side, and one kept in another zone, stand for none_days (180) alike, looked at in
    summer, in winter and in the hour the clocks go back."""
    from datetime import datetime, timedelta, timezone

    from hylly import settings

    days = settings.get("lookups")["none_days"]
    for when, base in CLOCK_TIMES.items():
        class Then(datetime):  # the clock, at base
            @classmethod
            def now(cls, tz=None):
                return base if tz is None else base.astimezone(tz)

        now = base.astimezone()
        forms = {"before": lambda age: (base - age).isoformat(),
                 "with its offset": lambda age: (now - age).isoformat(),
                 "another zone's": lambda age: (now - age).astimezone(timezone(timedelta(hours=-9))).isoformat()}
        with monkeypatch.context() as m:
            m.setattr(lookups, "datetime", Then)
            for form, at in forms.items():
                assert lookups.settled({"status": "none", "at": at(timedelta(days=days - 1))}), (when, form)
                assert not lookups.settled({"status": "none", "at": at(timedelta(days=days + 1))}), (when, form)
    cache = lookups.Cache(tmp_path / "lookup-x.jsonl")
    cache.put("k", ["x"], "none")
    assert datetime.fromisoformat(cache.get("k")["at"]).utcoffset() is not None and lookups.settled(cache.get("k"))
    assert not lookups.settled({"status": "none", "at": "soon"})  # one that can't be read is asked again


def test_a_refused_answer_stays_out_and_a_correction_is_asked_about(tmp_path):
    source = openlibrary.OpenLibrary()
    book = q("x", "The Moon Garden", kind="book")
    key = source.key(book)
    cache = tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True)
    line = {"key": key, "ids": ["x"], "at": "2026-11-04T10:00:00"}
    cache.write_text(json.dumps({**line, "status": "found", "found": {"authors": ["Ann Writer"], "score": 1.0, "url": "u"}}) + "\n"
                     + json.dumps({**line, "status": "refused", "why": "another book of the same name"}) + "\n", encoding="utf-8")
    assert lookups.Answers(tmp_path).for_query(book) == {}
    assert lookups.settled(lookups.Cache(cache).get(key)) and lookups.settled(lookups.Cache(cache).get(key), again=True)

    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"docs": []})

    clock = Clock()
    run = lambda title, **kw: lookups.run(tmp_path, [title], [source], transport=httpx.MockTransport(handler), sleep=clock.sleep,  # noqa: E731
                                          now=clock.now, log=lambda t: None, **kw)["openlibrary"]["asked"]
    assert run(book) == 0 and run(book, again=True) == 0  # not asked again, --again or not
    assert run(q("x", "The Moon Garden", kind="book", authors=["Bo Writer"])) == 1 and asked[0]["author"] == "Bo Writer"  # as corrected


def test_a_near_miss_used_or_refused_by_your_answer_is_used_or_left_and_neither_is_asked_again(tmp_path):
    """A near miss (a match under accept, kept as none with it) at or above NEAR is put to you (Answers.near),
    one under it isn't; a match you said to use fills the title though it scored under accept, while the source's answer
    is that match (m); a source you refused gives the title nothing, its found answer neither; and lookups.run asks
    neither source about that title again, --again or not, as a refused line in the cache stood before."""
    source = openlibrary.OpenLibrary()
    book, other = q("s:7", "The Moon Garden", kind="book"), q("s:8", "Lantern Days", kind="book")
    cache = tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True)
    near = {"authors": ["Ann Writer"], "score": 0.7, "match": "The Moon Gardens", "url": "https://openlibrary.org/works/OL7W"}
    far = {"authors": ["Bo Writer"], "score": 0.5, "match": "Lantern Nights", "url": "https://openlibrary.org/works/OL8W"}
    cache.write_text("".join(json.dumps({"key": source.key(t), "ids": [t.id], "status": "none", "at": "2099-01-01T00:00:00",
                                         "found": {"near": n}}) + "\n" for t, n in ((book, near), (other, far))),
                     encoding="utf-8")
    answers = lookups.Answers(tmp_path)
    assert [(s.id, n["match"]) for s, n in answers.near(book)] == [("openlibrary", "The Moon Gardens")]
    assert answers.near(other) == [] and answers.for_query(book) == {}  # under the floor; and none used, as before
    use = {"answers": {"lookup:openlibrary": {"a": "use", "m": lookups.match_mark(near)}}}
    curation.change_answers(tmp_path, use["answers"], [{"id": "s:7", "t": "The Moon Garden", "f": [[""]]}], by="rory")
    answers = lookups.Answers(tmp_path)
    assert answers.for_query(book)["authors"] == ["Ann Writer"] and answers.near(book) == []
    curation.change_answers(tmp_path, {"lookup:openlibrary": {"a": "use", "m": "0" * 12}},
                            [{"id": "s:7", "t": "The Moon Garden", "f": [[""]]}], by="rory")  # another match: not this one
    assert lookups.Answers(tmp_path).for_query(book) == {} and lookups.Answers(tmp_path).near(book)
    curation.change_answers(tmp_path, {"lookup:openlibrary": {"a": "refused"}},
                            [{"id": "s:7", "t": "The Moon Garden", "f": [[""]]}], by="rory")
    assert lookups.Answers(tmp_path).for_query(book) == {} and lookups.Answers(tmp_path).near(book) == []

    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"docs": []})

    clock = Clock()
    run = lambda title, **kw: lookups.run(tmp_path, [title], [source], transport=httpx.MockTransport(handler), sleep=clock.sleep,  # noqa: E731
                                          now=clock.now, log=lambda t: None, **kw)["openlibrary"]["asked"]
    assert run(book, again=True) == 0 and run(other, again=True) == 1  # yours stands; the other is asked


def test_a_refusal_made_as_an_edit_lets_the_next_source_try_as_a_refused_line_does(tmp_path, monkeypatch):
    """A source you refused (hylly set refused, an edit) gives the title nothing, so the next source in the order is
    asked about it, as with a refused line written into the first source's cache: the two say the same."""
    from hylly import sites

    class First(lookups.Source):
        id, name, kinds, interval = "first", "First DB", frozenset({"book"}), 0.0

        def find(self, s, q):
            raise AssertionError("not asked: refused")

    class Second(lookups.Source):
        id, name, kinds, interval = "second", "Second DB", frozenset({"book"}), 0.0
        asked: list[str] = []

        def find(self, s, q):
            Second.asked.append(q.id)
            return None

    monkeypatch.setattr(lookups, "in_order", lambda: {"first": First, "second": Second})
    monkeypatch.setitem(sites.LOOKUPS, "first", sites.LOOKUPS["openlibrary"])
    book, other = q("s:1", "The Moon Garden", kind="book"), q("s:2", "Lantern Days", kind="book")
    cache = tmp_path / "state" / "lookups" / "lookup-first.jsonl"
    cache.parent.mkdir(parents=True)
    found = {"authors": ["Ann Writer"], "description": "Another book.", "score": 0.95, "match": "The Moon Garden"}
    lines = [{"key": First().key(t), "ids": [t.id], "status": "found", "at": "2099-01-01T00:00:00", "found": found}
             for t in (book, other)]
    lines.append({"key": First().key(other), "ids": [other.id], "status": "refused", "at": "2099-01-02T00:00:00"})  # by hand
    cache.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    curation.change_answers(tmp_path, {"lookup:first": {"a": "refused"}}, [{"id": "s:1", "t": "The Moon Garden", "f": [[""]]}],
                            by="rory")  # by an edit
    assert lookups.Answers(tmp_path).for_query(book) == {} and lookups.Answers(tmp_path).for_query(other) == {}
    clock = Clock()
    lookups.run(tmp_path, [book, other], [First(), Second()], transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                sleep=clock.sleep, now=clock.now, log=lambda t: None)
    assert sorted(Second.asked) == ["s:1", "s:2"]


def test_an_itch_io_title_with_no_author_is_asked_with_its_creator(tmp_path):
    made = {"id": "i", "src": "itchio", "t": "Moss Harbour", "k": "rpg", "p": "Ann Maker"}
    query = lookups.Query.from_record(made)
    assert query.authors == ["Ann Maker"] and "authors" not in query.has  # still lacking one: an answer can fill it in
    assert lookups.Query.from_record({**made, "a": ["Bo Writer"]}).authors == ["Bo Writer"]  # one it has wins
    assert lookups.Query.from_record({**made, "src": "humble"}).authors == []  # elsewhere a publisher is a publisher

    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"docs": []})  # with the creator, a book of the same name by someone else isn't found

    source = openlibrary.OpenLibrary()
    stats = lookups.run(tmp_path, [query], [source], transport=httpx.MockTransport(handler), sleep=Clock().sleep,
                        now=Clock().now, log=lambda t: None)["openlibrary"]
    assert stats["none"] == 1 and asked[0]["author"] == "Ann Maker" and asked[0]["title"] == "Moss Harbour"


def test_one_failing_title_does_not_stop_a_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    clock, fake = Clock(), FakeMetron()
    fake.answers += [httpx.Response(500)] * lookups.RETRIES  # the first title fails on every try ...
    titles = [q("a", "Moonwake Vol. 3"), q("b", "Ironclad Hearts #31")]
    stats = lookups.run(tmp_path, titles, lookups.available(["metron"])[0], transport=httpx.MockTransport(fake.handler),
                        sleep=clock.sleep, now=clock.now, log=lambda t: None)["metron"]
    assert (stats["errors"], stats["found"], stats["stopped"]) == (1, 1, "")  # ... and the next is still asked
    fake.answers += [httpx.Response(503)] * (lookups.RETRIES * 3)  # three in a row: the site is down
    stats = lookups.run(tmp_path, titles + [q("c", "Glass Harbor Vol. 4"), q("d", "Rust Cowboy #1")], lookups.available(["metron"])[0],
                        transport=httpx.MockTransport(fake.handler), sleep=clock.sleep, now=clock.now, log=lambda t: None)["metron"]
    assert "seems down" in stats["stopped"]


def test_open_library_skips_a_title_that_is_only_a_label(tmp_path):
    clock, source = Clock(), openlibrary.OpenLibrary()
    assert source.find(session_for(source, openlibrary_handler, clock, tmp_path), q("x", "(ebook - psychology)", kind="book")) is None


# ---- Google Books, faked --------------------------------------------------------------------

def google_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.params
    if p.get("key") == "spent" * 8:
        return httpx.Response(403, json={"error": {"code": 403, "message": "Quota exceeded", "errors": [{"reason": "dailyLimitExceeded"}]}})
    if p.get("key") != TOKEN:
        return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "errors": [{"reason": "badRequest"}],
                                                   "details": [{"reason": "API_KEY_INVALID"}]}})
    query = p["q"].lower()
    items = []
    if "the novel" in query or query == "isbn:9780000000002":
        items = [{"id": "g1", "volumeInfo": {"title": "The Novel", "authors": ["Ann Writer"], "publisher": "Small Press",
                                             "publishedDate": "2011-05-01", "description": "<p>A <b>novel</b> about novels.</p>",
                                             "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780000000002"}],
                                             "categories": ["Fiction"], "canonicalVolumeLink": "https://books.google.com/books?id=g1"}}]
    elif "moonwake" in query:
        items = [{"id": "g2", "volumeInfo": {"title": "Moonwake Vol. 2", "authors": ["Ann Writer"], "description": "Volume two."}},
                 {"id": "g3", "volumeInfo": {"title": "Moonwake Vol. 3", "subtitle": "Tides", "authors": ["Ann Writer", "Bea Artist"],
                                             "description": "Volume three.", "canonicalVolumeLink": "https://books.google.com/books?id=g3"}}]
    return httpx.Response(200, json={"totalItems": len(items), **({"items": items} if items else {})})


def test_google_books(tmp_path):
    from hylly.lookups import googlebooks

    clock, source = Clock(), googlebooks.GoogleBooks(TOKEN)
    s = session_for(source, google_handler, clock, tmp_path)
    book = source.find(s, q("n", "The Novel", kind="book", authors=["Ann Writer"]))
    assert (book["description"], book["isbn"], book["year"], book["subjects"]) == ("A novel about novels.", "9780000000002", "2011", ["Fiction"])
    assert source.find(s, q("w", "The Novel", kind="book", authors=["Someone Else"]))["score"] < lookups.accept()  # wrong author
    tpb = source.find(s, q("c", "Moonwake Vol. 3", kind="comic"))
    assert tpb["description"] == "Volume three." and tpb["authors"] == ["Ann Writer", "Bea Artist"]  # not volume two
    assert source.find(s, q("i", "Moonwake #3", kind="comic")) is None  # single issues aren't books
    with pytest.raises(lookups.Refused):
        bad = googlebooks.GoogleBooks("w" * 40)
        bad.find(session_for(bad, google_handler, clock, tmp_path), q("n", "The Novel", kind="book"))
    with pytest.raises(lookups.Quota):  # Google says a spent quota with 403: not a refused key
        spent = googlebooks.GoogleBooks("spent" * 8)
        spent.find(session_for(spent, google_handler, clock, tmp_path), q("n", "The Novel", kind="book"))


def volume_info(change):
    return lambda d: {**d, "items": [{**i, "volumeInfo": change(i["volumeInfo"])} for i in d.get("items", [])]}


GB_ODD = {  # what changed in Google Books' answers: (the change, as the docs say it ends, words its error has)
    "the answer a list, not an object": (lambda d: [d], "changed", "answer looks different"),
    "its items an object, not a list": (lambda d: {**d, "items": {"id": "g1"}}, "changed", "answer looks different"),
    "a volume's details a list": (lambda d: {**d, "items": [{**i, "volumeInfo": [i["volumeInfo"]]} for i in d["items"]]}, "none", ""),
    "a volume without its details": (lambda d: {**d, "items": [{"id": i["id"]} for i in d["items"]]}, "none", ""),
    "the authors as text": (volume_info(lambda v: {**v, "authors": "Ann Writer"}), "changed", "authors or categories"),
    "the categories as text": (volume_info(lambda v: {**v, "categories": "Fiction"}), "changed", "authors or categories"),
    "an author as a number": (volume_info(lambda v: {**v, "authors": ["Ann Writer", 1984]}), "changed", "authors or categories"),
    "a title as a number": (volume_info(lambda v: {**v, "title": 1984}), "changed", "answer looks different"),
    "the description as a number": (volume_info(lambda v: {**v, "description": 5}), "changed", "answer looks different"),
    "the identifiers as text": (volume_info(lambda v: {**v, "industryIdentifiers": "9780000000002"}), "changed",
                                "answer looks different"),
    "an identifier's number as a number": (volume_info(lambda v: {**v, "industryIdentifiers": [
        {"type": "ISBN_13", "identifier": 9780000000002}]}), "changed", "answer looks different"),
    "the date as a number": (volume_info(lambda v: {**v, "publishedDate": 2011}), "found", ""),
}


@pytest.mark.parametrize("odd", GB_ODD)
def test_google_books_answering_in_another_shape_is_changed_or_nothing_and_the_run_goes_on(tmp_path, odd):
    """Asked by its title alone too, where a match's authors are taken as they come: never letters for names."""
    from hylly.lookups import googlebooks

    change, outcome, words = GB_ODD[odd]
    for n, title in enumerate((q("n", "The Novel", kind="book", authors=["Ann Writer"]), q("t", "The Novel", kind="book"))):
        stats, kept = asked_once(tmp_path / str(n), googlebooks.GoogleBooks(TOKEN), answered_otherwise(google_handler, "/books/", change), title)
        as_the_docs_say(stats, kept, outcome, words)


def test_a_later_source_is_asked_only_for_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_GOOGLEBOOKS_KEY", TOKEN)
    asked = []

    def both(request):
        asked.append(request.url.host)
        if request.url.host == "openlibrary.org":
            if request.url.path == "/search.json":  # Open Library knows the book, but has no description for it
                return httpx.Response(200, json={"docs": [{"key": "/works/OL9W", "title": "The Novel", "subject": ["Fiction"]}]})
            return httpx.Response(200, json={"subjects": ["Fiction"]})
        return google_handler(request)

    titles = [q("n", "The Novel", kind="book", authors=["Ann Writer"]),
              q("d", "Described Already", kind="book", has=frozenset({"authors", "description"}))]
    stats = lookups.run(tmp_path, titles, lookups.available(["openlibrary", "googlebooks"])[0], transport=httpx.MockTransport(both),
                        sleep=lambda s: None, log=lambda t: None)
    assert stats["openlibrary"]["found"] == 1 and stats["googlebooks"]["found"] == 1 and "www.googleapis.com" in asked
    found = lookups.Answers(tmp_path).for_query(titles[0])
    assert found["description"] == "A novel about novels." and found["subjects"] == ["Fiction"]  # Open Library's first, then Google's
    assert [name for name, _ in found["lk"]] == ["Open Library", "Google Books"]


def test_comic_vine_tells_a_collected_edition_from_an_issue(tmp_path):
    def handler(request):
        p, path = request.url.params, request.url.path
        if path == "/api/search/":  # the series run, then its collected editions: the same name
            return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": [
                {"id": 1, "name": "Moonwake", "start_year": "2013", "publisher": {"name": "Lantern"}, "count_of_issues": 36},
                {"id": 2, "name": "Moonwake", "start_year": "2014", "publisher": {"name": "Lantern"}, "count_of_issues": 8}]})
        if path == "/api/issues/":
            volume = p["filter"].split(",")[0].split(":")[1]
            return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": [{"id": 100 + int(volume)}]})
        detail = {"/api/issue/4000-101/": {"name": "Chapter One", "issue_number": "3", "description": "The tide turns.",
                                           "person_credits": [{"name": "Ann Writer", "role": "writer"}]},
                  "/api/issue/4000-102/": {"name": "Volume 3: Tides", "issue_number": "3", "description": "Collects issues #11-15.",
                                           "person_credits": [{"name": "Ann Writer", "role": "writer"}, {"name": "Bea Artist", "role": "artist"}]}}
        return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": detail[path]})

    clock, source = Clock(), comicvine.ComicVine(TOKEN)
    s = session_for(source, handler, clock, tmp_path)
    tpb = source.find(s, q("v", "Moonwake Vol. 3"))
    assert tpb["description"] == "Collects issues #11-15." and tpb["score"] >= lookups.accept()
    issue = source.find(s, q("i", "Moonwake #3"))
    assert issue["description"] == "The tide turns."
    collected = comicvine.COLLECTED.search
    assert collected("The hardcover collects issues #1-6") and collected("A trade paperback") and not collected("Chapter One")


def test_a_later_source_waits_for_the_earlier_ones_to_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_METRON_TOKEN", TOKEN)
    monkeypatch.setenv("HYLLY_COMICVINE_KEY", TOKEN)
    asked = []

    def both(request):
        asked.append(request.url.host)
        return (FakeMetron().handler if request.url.host == "metron.cloud" else comicvine_handler)(request)

    titles = [q("a", "Nothing Like It Vol. 1"), q("b", "Ironclad Hearts #31")]
    run = lambda **kw: lookups.run(tmp_path, titles, lookups.available(["metron", "comicvine"])[0],  # noqa: E731
                                   transport=httpx.MockTransport(both), sleep=lambda s: None, log=lambda t: None, **kw)
    first = run(limit=1)  # Metron answers only the first title ("none") in this run
    assert first["metron"]["asked"] == 1 and first["comicvine"]["asked"] == 1  # Comic Vine: only that one
    second = run()
    assert second["metron"]["asked"] == 1 and second["comicvine"]["asked"] == 0  # Metron found the second


def cached(root, source, *answers):
    """Answers as if a lookup had saved them: (catalog id, found fields)."""
    folder = root / "state" / "lookups"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"key": f"t:{i}", "ids": [i], "status": "found", "found": {"score": 1.0, "url": "", **found}})
             for i, found in answers]
    (folder / f"lookup-{source}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_an_answer_that_cant_be_right_is_left_out_when_the_catalog_is_built(tmp_path):
    later = {"authors": ["Nobody Real"], "year": "2026", "description": "A novel of the same name, years later."}
    cached(tmp_path, "googlebooks", ("h:mw12", later), ("h:mw13", later), ("h:old", later), ("h:orchard", later),
           ("h:lantern", {"description": "The Lantern Keeper, as Google Books tells it, in plain words."}),
           ("h:moss", {"description": "Moss Rangers, the second answer, in plain words."}))
    cached(tmp_path, "openlibrary",
           ("h:lantern", {"description": "Call our customer service at +1-(800)-555-0100 for bookings, 24/7."}),
           ("h:orchard", {"description": "**The Glass Orchard** is a [novel](https://openlibrary.org/works/OL1W) by "
                                         "[Ann Writer][1], *slow* and lovely. [1]: https://openlibrary.org/authors/OL2A"}),
           ("h:moss", {"description": "see https://openlibrary.org/works/OL3W/Moss_Rangers"}),
           ("h:bet", {"description": "You love to bet? Take a look at https://casino.example/ for a bonus."}))
    answers = lookups.Answers(tmp_path)
    # A comic answered with an edition from years after it was bought: not the one you have
    assert answers.for_query(q("h:mw12", "Moonwake Vol. 12", bought="2023")) == {}
    assert answers.for_query(q("h:mw13", "Moonwake Vol. 13", bought="2025"))["authors"] == ["Nobody Real"]  # a cover
    # date runs ahead of the day it went on sale, so the next year is still the same comic
    assert answers.for_query(q("h:old", "Moonwake Vol. 1"))["authors"] == ["Nobody Real"]  # no purchase date: kept
    book = answers.for_query(q("h:orchard", "The Glass Orchard", kind="book", bought="2015", authors=["Ann Writer"]))
    assert book["authors"] == ["Nobody Real"]  # a book's later edition is still the book
    alone = answers.for_query(q("h:orchard", "The Glass Orchard", kind="book", bought="2015"))  # but asked by its title
    assert not {"authors", "year"} & set(alone)  # alone, with nothing that agrees, they may be another book's (borrowed)
    assert book["description"] == "The Glass Orchard is a novel by Ann Writer, slow and lovely."  # Markdown as words
    # An advert put on a wiki page, or only a link: the next source's description fills in
    assert answers.for_query(q("h:lantern", "The Lantern Keeper", kind="book"))["description"].startswith("The Lantern Keeper,")
    assert answers.for_query(q("h:moss", "Moss Rangers", kind="book"))["description"].startswith("Moss Rangers,")
    assert "description" not in answers.for_query(q("h:bet", "Uncertain Odds", kind="book"))
    assert lookups.Query.from_record({"id": "h:x", "t": "X", "dt": "2023-05-01"}).bought == "2023"
    # A real description may have one selling word, and a link a sentence on: only more than that makes an advert
    real = {"A hands-on guide to applied cryptography in Python. The code is at https://example.org/code":
            "A hands-on guide to applied cryptography in Python.",
            "Twelve tales of the Lantern Coast, with a bonus story never printed before. More at www.example.org.":
            "Twelve tales of the Lantern Coast, with a bonus story never printed before.",
            "You bet your life on it. See the author's notes at https://example.org/notes": "You bet your life on it.",
            # a selling word in the link itself, its address or its words, sells nothing
            "A hands-on guide to applied cryptography. Code at https://example.org/crypto/":
            "A hands-on guide to applied cryptography.",
            "Twelve tales of the Lantern Coast. Extras at https://example.org/bonus": "Twelve tales of the Lantern Coast.",
            "Read the [bonus story](https://example.org/x).": "Read the bonus story."}
    for text, start in real.items():
        assert lookups.plain_description(text).startswith(start), text
    for text in ("Call our customer service at +1-(800)-555-0100 for bookings, 24/7.",  # a phone number
                 "You love to bet? Take a look at https://casino.example/ for a bonus.",  # selling words, more than one
                 "Best crypto tips, every day, at https://example.org/tips",  # one, beside the link
                 "A tale of the sea.\nYour bonus waits at www.example.org",
                 "Call +1 800 555 0100, +1 800 555 0101 or +1 800 555 0102."):  # three phone numbers
        assert lookups.plain_description(text) == "", text
    n = lookups.MAX_DESCRIPTION - 1000  # and the sentences are found at once, whatever runs of spaces they have
    for run in (" " * n, "\n" * n):
        assert quick(lookups.plain_description, f"A bonus tale of the sea.{run}More at www.example.org") == "A bonus tale of the sea."


def test_a_run_asks_the_next_source_for_what_the_catalog_leaves_out_of_an_answer(tmp_path, monkeypatch):
    """A run reads a cached answer as the catalog does (lookups.usable): an advert for a description, or a comic's
    answer dated years after it was bought, is left out, so the next source is asked about the title, and its
    description fills in. Before, the run counted them as had: "Google Books: nothing new to look up"."""
    for name in ("HYLLY_GOOGLEBOOKS_KEY", "HYLLY_METRON_TOKEN", "HYLLY_COMICVINE_KEY"):
        monkeypatch.setenv(name, TOKEN)
    asked = []

    def sites(request):
        asked.append(request.url.host)
        if request.url.host == "openlibrary.org":
            if request.url.path == "/search.json":
                return httpx.Response(200, json={"docs": [{"key": "/works/OL9W", "title": "The Novel", "subject": ["Fiction"],
                                                           "author_name": ["Ann Writer"]}]})
            return httpx.Response(200, json={"description": "Call our customer service at +1-(800)-555-0100, 24/7."})
        if request.url.host == "metron.cloud":
            return FakeMetron().handler(request)
        if request.url.host == "comicvine.gamespot.com":
            return comicvine_handler(request)
        return google_handler(request)

    novel = q("n", "The Novel", kind="book", authors=["Ann Writer"])
    issue = q("i", "Ironclad Hearts #31", bought="1990")  # Metron's answer is dated 1996: another edition
    stats = lookups.run(tmp_path, [novel, issue], lookups.available()[0], transport=httpx.MockTransport(sites),
                        sleep=lambda s: None, log=lambda t: None)
    assert stats["openlibrary"]["found"] == 1 and asked.count("www.googleapis.com") == 1  # the novel (a single issue
    assert stats["metron"]["found"] == 1 and stats["comicvine"]["asked"] == 1  # isn't sold as a book: not sent)
    found = lookups.Answers(tmp_path).for_query(novel)
    assert found["description"] == "A novel about novels." and [name for name, _ in found["lk"]] == ["Open Library", "Google Books"]


def test_a_book_asked_by_its_title_alone_takes_a_match_s_authors_only_when_its_publisher_or_year_agrees(tmp_path):
    """Asked with neither author nor ISBN, a book's question finds the first book of that name a source knows, and
    seldom the right one: the match's authors (and its publisher, year and ISBN) are used only when its publisher or
    its year is the title's own, or a long title is the match's very title (the next test) (lookups.borrowed), at every
    build, so an answer saved before drops them without a new request. Its description and subjects are used as
    before. A question with an author or an ISBN, and a comic's, by series and number, are as before; so is a match
    you said to use, which you found to be the book
    (test_a_near_miss_used_or_refused_by_your_answer_is_used_or_left_and_neither_is_asked_again)."""
    told = {"authors": ["Bo Stranger"], "description": "A book of that name.", "subjects": ["Fiction"]}
    cached(tmp_path, "googlebooks", ("h:none", told), ("h:pub", {**told, "publisher": "Moonwake Press, Inc."}),
           ("h:year", {**told, "year": "2014"}), ("h:moon", {**told, "publisher": "Moon Press", "year": "2009"}),
           ("h:own", told), ("h:isbn", told), ("h:comic", told), ("h:bare", {"authors": ["Bo Stranger"]}),
           ("h:self", {**told, "publisher": "CreateSpace Independent Publishing Platform"}),
           ("h:advert", {"authors": ["Bo Stranger"], "description": "Call our customer service at +1-(800)-555-0100, 24/7."}))
    answers = lookups.Answers(tmp_path)
    alone = answers.for_query(q("h:none", "The Glass Orchard", kind="book", publisher="Moonwake Press", year="2014"))
    assert "authors" not in alone and (alone["description"], alone["subjects"]) == ("A book of that name.", ["Fiction"])
    assert answers.for_query(q("h:pub", "The Glass Orchard", kind="book", publisher="Moonwake Press"))["authors"] == ["Bo Stranger"]
    assert answers.for_query(q("h:year", "The Glass Orchard", kind="tech", year="2014"))["authors"] == ["Bo Stranger"]
    other = answers.for_query(q("h:moon", "The Glass Orchard", kind="rpg", publisher="Moonwake Press", year="2014"))
    assert not {"authors", "publisher", "year"} & set(other) and other["description"]  # another press, another year: it
    # may be another book, whose publisher, year and ISBN would be as wrong as its authors
    assert answers.for_query(q("h:bare", "The Glass Orchard", kind="book")) == {}  # nothing left: not named as a source
    assert answers.for_query(q("h:advert", "The Glass Orchard", kind="book")) == {}  # an advert is nothing either
    # a platform anyone publishes on is no sign of the book: thousands share it
    assert "authors" not in answers.for_query(q("h:self", "The Glass Orchard", kind="book", publisher="CreateSpace"))
    for mine, theirs in (("Independently published", "Independently Published"), ("Lulu.com", "Lulu Press"),
                         ("Smashwords Edition", "Smashwords"), ("Draft2Digital", "Draft2Digital")):
        assert not lookups.agrees(q("h:x", "X", kind="book", publisher=mine), [theirs]), (mine, theirs)
    assert lookups.agrees(q("h:x", "X", kind="book", publisher="Moonwake Press"), ["Lulu", "Moonwake Press, Inc."])
    assert answers.for_query(q("h:own", "The Glass Orchard", kind="book", authors=["Ann Writer"]))["authors"] == ["Bo Stranger"]
    assert answers.for_query(q("h:isbn", "The Glass Orchard", kind="book", isbn="9780000000002"))["authors"] == ["Bo Stranger"]
    assert answers.for_query(q("h:comic", "Moonwake Vol. 3"))["authors"] == ["Bo Stranger"]
    assert lookups.Query.from_record({"id": "h:x", "t": "X", "y": "2014-03-04", "p": "Moonwake Press"}).year == "2014"
    assert not lookups.agrees(q("h:x", "X", kind="book"), [""], [""])  # nothing of its own: nothing agrees


def test_a_long_title_asked_alone_keeps_the_authors_of_a_match_with_the_very_same_title(tmp_path):
    """A title of three words or more, articles left out, whose match has the very same title (by norm's words; a
    subtitle after a colon on either side may differ) is taken to be the book with nothing else vouching for it, also
    in an answer saved before (Open Library's: the matched title in "match", no publishers or years). A shorter title,
    or a match whose title is only near, still needs a publisher or a year that agrees (lookups.borrowed)."""
    def answer(match: str) -> dict:
        return {"authors": ["Bo Stranger"], "subjects": ["Fiction"], "match": match}

    cached(tmp_path, "openlibrary", ("h:long", answer("The Silent Orbit Atlas")), ("h:near", answer("The Silent Orbit Atlases")),
           ("h:short", answer("The Glass Orchard")), ("h:article", answer("A Lantern Garden")),
           ("h:sub", answer("Silent Orbit Atlas")), ("h:theirs", answer("The Silent Orbit Atlas: Second Edition")))
    cached(tmp_path, "googlebooks", ("h:joined", answer("The Silent Orbit Atlas A Field Guide")),  # title and subtitle as one
           ("h:apart", answer("The Silent Orbit Atlas: A Field Guide")))  # as its answers have them now
    answers = lookups.Answers(tmp_path)
    authors = lambda id_, title: answers.for_query(q(id_, title, kind="book")).get("authors")  # noqa: E731
    assert authors("h:long", "The Silent Orbit Atlas") == ["Bo Stranger"]  # three words, the very title: nothing vouches
    assert authors("h:near", "The Silent Orbit Atlas") is None  # a near title is not the very title
    assert authors("h:short", "The Glass Orchard") is None  # two words: too common to go by
    assert authors("h:article", "A Lantern Garden") is None  # articles aren't counted
    assert authors("h:sub", "The Silent Orbit Atlas: A Field Guide") == ["Bo Stranger"]  # a subtitle on this side
    assert authors("h:theirs", "The Silent Orbit Atlas") == ["Bo Stranger"]  # or on the match's
    assert authors("h:joined", "The Silent Orbit Atlas: A Field Guide") == ["Bo Stranger"]  # the same with its subtitle
    assert authors("h:apart", "The Silent Orbit Atlas") == ["Bo Stranger"]
    assert authors("h:joined", "The Silent Orbit Atlas") is None  # one text of title and subtitle can't be told apart
    assert lookups.bare_title("(ebook - maps)  The Silent Orbit Atlas: A Field Guide (2nd ed.)") == "The Silent Orbit Atlas"


def test_open_library_vouches_by_the_year_a_work_first_came_out_not_by_any_edition_s(tmp_path):
    """A work printed for long has an edition in almost every year, and a PDF's own year is often the year its file was
    made: a title-only PDF "The Garden" made in 2012 would take the author of a work with editions from 1950 to 2024.
    Open Library's answer vouches by the year the work first came out, and by its publishers."""
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search.json":
            return httpx.Response(200, json={"docs": [{
                "key": "/works/OL6W", "title": "The Garden", "author_name": ["Bo Stranger"], "first_publish_year": 1950,
                "publish_year": list(range(1950, 2025)), "publisher": ["Harbor House"]}]})
        return httpx.Response(200, json={"description": "A garden of that name."})

    source = openlibrary.OpenLibrary()
    pdf = q("g", "The Garden", kind="book", year="2012")
    found = source.find(session_for(source, site, Clock(), tmp_path), pdf)
    assert found["years"] == ["1950"] and found["authors"] == ["Bo Stranger"] and lookups.borrowed(pdf, found)
    assert not lookups.borrowed(q("g", "The Garden", kind="book", year="1950"), found)  # the year it first came out
    assert not lookups.borrowed(q("g", "The Garden", kind="book", publisher="Harbor House"), found)

def test_open_library_s_answer_to_a_title_asked_alone_says_its_publishers_and_years_and_google_books_is_asked_for_the_authors(
        tmp_path, monkeypatch):
    """Asked by title alone, Open Library also asks for the work's publishers and years (fields), and keeps them in its
    answer, which every title asked the same way shares: a title one of them agrees with takes its authors. One with
    none that agrees gets no authors from it, so Google Books is asked, and its match's authors are used if its own
    publisher or year agrees; else the title has none, which is better than another book's."""
    monkeypatch.setenv("HYLLY_GOOGLEBOOKS_KEY", TOKEN)
    asked = []

    def sites(request):
        asked.append((request.url.host, dict(request.url.params)))
        if request.url.host == "openlibrary.org":
            if request.url.path == "/search.json":
                return httpx.Response(200, json={"docs": [{
                    "key": "/works/OL5W", "title": "The Novel", "author_name": ["Bo Stranger"], "first_publish_year": 2011,
                    "publisher": ["Harbor House", "Moonwake Press, Inc."], "publish_year": [2011, 2019]}]})
            return httpx.Response(200, json={"description": "A book of that name."})
        return google_handler(request)  # The Novel: Ann Writer's, Small Press, 2011

    titles = {"pub": q("pub", "The Novel", kind="book", publisher="Moonwake Press"),
              "year": q("year", "The Novel", kind="book", year="2011"),
              "neither": q("neither", "The Novel", kind="book", publisher="Lantern Books", year="2003"),
              "google": q("google", "The Novel", kind="book", publisher="Small Press")}
    stats = lookups.run(tmp_path, list(titles.values()), lookups.available(["openlibrary", "googlebooks"])[0],
                        transport=httpx.MockTransport(sites), sleep=lambda s: None, log=lambda t: None)
    searches = [p for host, p in asked if host == "openlibrary.org" and "title" in p]  # one question for all four
    assert len(searches) == 1 and {"publisher", "first_publish_year"} <= set(searches[0]["fields"].split(","))
    assert stats["googlebooks"]["asked"] == 1  # one question too, for the two Open Library's answer gives no authors
    answers = lookups.Answers(tmp_path)
    found = {name: answers.for_query(title) for name, title in titles.items()}
    assert found["pub"]["authors"] == ["Bo Stranger"] and found["year"]["authors"] == ["Bo Stranger"]
    assert not {"authors", "isbn"} & set(found["neither"]) and found["neither"]["description"] == "A book of that name."
    assert found["google"]["authors"] == ["Ann Writer"] and found["google"]["description"] == "A book of that name."
    assert found["google"]["isbn"] == "9780000000002" and "isbn" not in found["pub"]  # Google's match is its book alone
    assert not {"publishers", "years"} & set(found["pub"])  # they only vouch


# ---- odd text, odd answers ------------------------------------------------------------------

def quick(clean, text: str) -> str:
    """What clean makes of text, in well under a second: patterns that backtrack over a long run take minutes."""
    started = time.perf_counter()
    out = clean(text)
    assert time.perf_counter() - started < 0.5, f"{clean.__name__} took {time.perf_counter() - started:.1f} s on {text[6:12]!r}…"
    return out


def test_a_long_run_in_a_description_is_cleaned_at_once():
    from hylly import meta

    def catalog_build(text: str) -> str:  # what each build does with a saved description
        return meta.clean_text(lookups.plain_description(text))

    assert quick(catalog_build, "A tale" + " " * 100_000 + "of the sea.") == "A tale"  # cut to MAX_DESCRIPTION first
    n = lookups.MAX_DESCRIPTION - 1000  # runs the cut leaves whole
    runs = {" " * n: "A tale of the sea.", "\n" * n: "A tale of the sea.", " \n\t" * (n // 3): "A tale of the sea.",
            " (" + " " * n: "A tale ( of the sea.", " see" + " " * n: "A tale see of the sea.",
            "\n" + "---\n" * (n // 4): "A tale of the sea."}
    for run, cleaned in runs.items():
        assert quick(catalog_build, f"A tale{run}of the sea.") == cleaned
    for run in ("<" * n, "[" * n, "[a" * (n // 2), "[a](/" * (n // 5), "**a " * (n // 4), "__a " * (n // 4)):
        text = f"A tale{run}of the sea."
        assert quick(lookups.plain_description, text) == text  # nothing in them to change
    assert quick(meta.clean_text, "A tale" + "<" * 100_000).startswith("A tale<<<")  # and a book's own, which isn't cut
    assert lookups.plain_description("**The Glass Orchard** and __the Lantern__, *glowing*") == "The Glass Orchard and the Lantern, glowing"


def test_long_runs_in_names_are_read_at_once(tmp_path):
    from hylly.lookups import googlebooks

    assert lookups.comic_name("Moonwake #1").series == "Moonwake"  # the rules are read now, not in the time below
    for run in ("-" * 100_000, ", " * 50_000, ":(" * 50_000):  # in a comic's own ComicInfo.xml, say
        assert quick(lookups.comic_name, f"Moonwake{run} x").series.endswith(" x")
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params.get("title") or request.url.params.get("q"))
        return httpx.Response(200, json={"docs": [], "totalItems": 0})

    clock, ol, gb = Clock(), openlibrary.OpenLibrary(), googlebooks.GoogleBooks(TOKEN)
    s_ol, s_gb = session_for(ol, handler, clock, tmp_path), session_for(gb, handler, clock, tmp_path)
    for title in ("The Novel" + " " * 100_000 + "x", "The Novel" + ":" * 100_000 + "\nx", "The Novel (" + " " * 100_000):
        quick(lambda t: ol.find(s_ol, q("o", t, kind="book")), title)
        quick(lambda t: gb.find(s_gb, q("g", t, kind="book")), title)
    assert asked[2:] == ["The Novel", "intitle:The Novel"] * 2  # what comes after a colon or a bracket is left out, as ever
    long_title = {"title": "Moonwake" + " " * 100_000 + "Vol. 3"}
    gb_answer = {"totalItems": 1, "items": [{"id": "g", "volumeInfo": long_title}]}
    s = session_for(gb, lambda request: httpx.Response(200, json=gb_answer), clock, tmp_path)
    assert quick(lambda t: gb.find(s, q("c", t, kind="comic")), "Moonwake Vol. 3")["score"] >= lookups.accept()


def test_a_site_s_html_is_made_plain_in_one_place_for_every_source():
    """Comic Vine's descriptions and Google Books' are made plain by one function (plain_html), as each plugin did it on
    its own: each tag a space, then the entities, so a tag written as entities stays words; each run of spaces one; and
    a long run of < is read at once."""
    from hylly.lookups import googlebooks

    assert lookups.plain_html("<p>Mara &amp; the <i>shield</i>.</p>") == "Mara & the shield ."
    assert lookups.plain_html("A &lt;b&gt;bold&lt;/b&gt; claim\n\n<br/>\ttold ") == "A <b>bold</b> claim told"
    assert lookups.plain_html("") == "" and lookups.plain_html("Plain words.") == "Plain words."
    assert quick(lookups.plain_html, "A tale" + "<" * 100_000 + "x").startswith("A tale<<<")
    assert googlebooks.GoogleBooks._found({"description": "<p>A <b>novel</b>&nbsp;about novels.</p>"}, 1.0)["description"] == (
        "A novel about novels.")
    assert comicvine.ComicVine._found({"deck": "The <i>deck</i> &amp; more"}, {"name": "Moonwake"}, 1.0)["description"] == (
        "The deck & more")  # with no description, its deck


class Odd(lookups.Source):
    """A made-up source whose find() does, for each title in turn, what it's told: raise something, or answer."""

    id, name, kinds, interval = "odd", "Odd DB", frozenset({"comic"}), 0.0

    def __init__(self, key: str = "", does=()):
        super().__init__(key)
        self.does = list(does)

    def find(self, s, q):
        what = self.does.pop(0)
        if isinstance(what, BaseException):
            raise what
        return what


def test_one_odd_answer_never_ends_the_run_and_the_other_sources_go_on(tmp_path, monkeypatch):
    monkeypatch.setattr(lookups, "in_order", lambda: {"odd": Odd, "openlibrary": openlibrary.OpenLibrary})
    closed, close = [], lookups.Session.close
    monkeypatch.setattr(lookups.Session, "close", lambda self: (closed.append(self.source.id), close(self)))
    long = {"description": "A tale of the sea. " * 3000, "score": 1.0}  # 57,000 characters
    odd = Odd(does=[KeyError("series"), long, TypeError("'NoneType' object is not subscriptable"), ["not", "a", "dict"]])
    comics = [q(f"c{i}", f"Moonwake #{i}") for i in range(1, 5)]
    clock, logged = Clock(), []
    stats = lookups.run(tmp_path, comics + [q("n", "The Novel", kind="book", authors=["Ann Writer"])], [odd, openlibrary.OpenLibrary()],
                        transport=httpx.MockTransport(openlibrary_handler), sleep=clock.sleep, now=clock.now, log=logged.append)
    assert (stats["odd"]["errors"], stats["odd"]["found"], stats["odd"]["stopped"]) == (3, 1, "")  # never three in a row
    assert stats["openlibrary"]["found"] == 1 and closed == ["odd", "openlibrary"]  # the next source is asked as ever
    lines = [json.loads(line) for line in (tmp_path / "state" / "lookups" / "lookup-odd.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e.get("error") for e in lines if e["status"] == "error"] == [
        "Odd DB: the plugin couldn't read an answer (KeyError).", "Odd DB: the plugin couldn't read an answer (TypeError).",
        "Odd DB: the plugin gave an answer that isn't a dict with a number for its score; it needs a fix."]  # never its words
    assert len(next(e for e in lines if e["status"] == "found")["found"]["description"]) == lookups.MAX_DESCRIPTION
    assert "Odd DB: 1 looked up, 1 found, 0 not found, 3 errors." in logged
    for description in (["A tale"], {"value": "A tale"}, 3):  # the shape Open Library itself gives; a plugin passes on text
        with pytest.raises(lookups.Changed, match="the plugin gave a description that isn't text"):
            lookups.answer(odd, {"description": description, "score": 1.0})
    with pytest.raises(KeyboardInterrupt):  # Ctrl+C still ends the run, and the session is closed all the same
        lookups.run(tmp_path, comics, [Odd(does=[KeyboardInterrupt()])], transport=httpx.MockTransport(openlibrary_handler),
                    sleep=clock.sleep, now=clock.now, log=logged.append)
    assert closed[-1] == "odd"


def test_three_odd_answers_in_a_row_stop_that_source_as_answers_that_changed(tmp_path, monkeypatch):
    def slipping(value):  # a mistake in the plugin's own code, which no answer's shape explains
        raise KeyError(value)

    clock, notes, logged = Clock(), [], []
    with monkeypatch.context() as m:
        m.setattr(comicvine, "whole", slipping)
        cv = lookups.run(tmp_path, [q("x", "Ironclad Hearts #31")], [comicvine.ComicVine(TOKEN)],
                         transport=httpx.MockTransport(comicvine_handler), sleep=clock.sleep, now=clock.now, log=logged.append)["comicvine"]
    error = json.loads((tmp_path / "state" / "lookups" / "lookup-comicvine.jsonl").read_text(encoding="utf-8"))["error"]
    assert cv["errors"] == 1 and re.fullmatch(r"Comic Vine: the plugin couldn't read an answer \(KeyError at comicvine\.py:\d+\)\.", error)
    assert TOKEN not in error  # where the plugin slipped, for whoever mends it; never a word of the answer or the request

    monkeypatch.setattr(lookups, "in_order", lambda: {"odd": Odd})
    odd = Odd(does=[KeyError("id"), TypeError("x"), IndexError("y"), None])
    stats = lookups.run(tmp_path, [q(f"c{i}", f"Moonwake #{i}") for i in range(1, 5)], [odd], transport=httpx.MockTransport(comicvine_handler),
                        sleep=clock.sleep, now=clock.now, log=logged.append, notify=lambda t, x: notes.append(t))["odd"]
    assert (stats["errors"], stats["asked"], stats.get("changed"), odd.does) == (3, 0, True, [None])  # the fourth isn't asked
    assert stats["stopped"] == ("Odd DB: the plugin couldn't read an answer (IndexError). Three answers in a row couldn't be "
                                "read: the Odd DB plugin may need an update.")
    assert notes == ["Library lookups: Odd DB stopped"] and logged[-1].endswith(stats["stopped"])


NOTE = ["Library lookups: Odd DB stopped"]
ENDINGS = {  # what find() does for each title in turn: why the turn ends, its flag, errors and titles asked, its notes
    "a spent quota": ([lookups.Quota("Odd DB: today's budget is spent.")], "Odd DB: today's budget is spent.", "quota", 0,
                      1, []),
    "a refused login": ([lookups.Refused("Odd DB refused the login (HTTP 401 on /api/look).")],
                        "Odd DB refused the login (HTTP 401 on /api/look).", "refused", 0, 1, NOTE),
    "three answers that changed": ([lookups.Changed(f"Odd DB: odd {n}.") for n in (1, 2, 3)], "Odd DB: odd 3. Three answers "
                                   "in a row couldn't be read: the Odd DB plugin may need an update.", "changed", 3, 3, NOTE),
    "three slips": ([KeyError("x"), TypeError("y"), IndexError("z")], "Odd DB: the plugin couldn't read an answer "
                    "(IndexError). Three answers in a row couldn't be read: the Odd DB plugin may need an update.", "changed",
                    3, 3, NOTE),
    "three without an answer": ([lookups.Transient(f"Odd DB: no answer {n}.") for n in (1, 2, 3)], "Odd DB: no answer 3. "
                                "Three titles in a row failed: Odd DB seems down. The next run continues.", None, 3, 3, NOTE),
    "two without an answer, then one that changed": (
        [lookups.Transient("Odd DB: no answer."), lookups.Transient("Odd DB: none again."), lookups.Changed("Odd DB: odd.")],
        "Odd DB: odd. Three answers in a row couldn't be read: the Odd DB plugin may need an update.", "changed", 3, 3, NOTE),
    "one that changed and a slip, then one without an answer": (
        [lookups.Changed("Odd DB: odd."), KeyError("x"), lookups.Transient("Odd DB: no answer.")],
        "Odd DB: no answer. Three titles in a row failed: Odd DB seems down. The next run continues.", None, 3, 3, NOTE),
    "failures between answers": ([lookups.Changed("Odd DB: odd."), None, lookups.Transient("Odd DB: no answer."), KeyError("x"),
                                  None], "", None, 3, 6, []),
}


@pytest.mark.parametrize("ending", ENDINGS)
def test_a_source_s_turn_ends_as_its_failures_say_with_the_words_and_flags_hylly_lookup_reads(tmp_path, monkeypatch, ending):
    """The asking loop's one branch for Changed, Transient and anything else a plugin lets out: each title that fails is
    kept as an error, asked again next run, and the third in a row ends the turn, as the site down when that one is
    Transient, else as answers that changed; a spent quota or a refused login ends it at once, the quota with no
    notification. Each with its words and the flags hylly lookup reads (stopped, quota, refused, changed), and the
    session closed once."""
    does, stopped, flag, errors, asked, notes = ENDINGS[ending]
    monkeypatch.setattr(lookups, "in_order", lambda: {"odd": Odd})
    closed, close = [], lookups.Session.close
    monkeypatch.setattr(lookups.Session, "close", lambda self: (closed.append(self.source.id), close(self)))
    clock, logged, told = Clock(), [], []
    odd = Odd(does=does + [None] * (6 - len(does)))  # nothing found for the titles after them
    st = lookups.run(tmp_path, [q(f"c{n}", f"Moonwake #{n}") for n in range(1, 7)], [odd],
                     transport=httpx.MockTransport(openlibrary_handler), sleep=clock.sleep, now=clock.now, log=logged.append,
                     notify=lambda title, text: told.append(title))["odd"]
    assert st["stopped"] == stopped and {k for k in ("quota", "refused", "changed") if st.get(k)} == ({flag} if flag else set())
    assert (st["errors"], 6 - len(odd.does), told, closed) == (errors, asked, notes, ["odd"])
    assert logged[-1] == (f"Odd DB: {st['asked']} looked up, {st['found']} found, {st['none']} not found, {errors} errors"
                          + (f". Stopped: {stopped}" if stopped else "."))
    cache = tmp_path / "state" / "lookups" / "lookup-odd.jsonl"
    kept = [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines()] if cache.exists() else []
    assert [e["error"] for e in kept if e["status"] == "error"] == [
        str(e) if isinstance(e, lookups.LookupProblem) else f"Odd DB: the plugin couldn't read an answer ({type(e).__name__})."
        for e in does if isinstance(e, Exception) and not isinstance(e, (lookups.Quota, lookups.Refused))]


def test_a_run_reads_each_source_s_cache_once_and_the_next_source_sees_this_run_s_answers(tmp_path, monkeypatch):
    """Every source's cache file is read once in a run, and once in a dry run (Plan.cache), not again at each later
    source's turn: what a source answers in the run goes into that same cache, so the next source isn't asked about a
    title the one before it has just found, and is asked about one it found nothing for."""
    from pathlib import Path

    from hylly import storage

    class Second(Odd):
        id, name = "second", "Second DB"

    class Third(Odd):
        id, name = "third", "Third DB"

    monkeypatch.setattr(lookups, "in_order", lambda: {"odd": Odd, "second": Second, "third": Third})
    read, real = [], storage.read_records
    monkeypatch.setattr(storage, "read_records", lambda path: (read.append(Path(path).name), real(path))[1])
    titles = [q("c1", "Moonwake #1"), q("c2", "Moonwake #2")]
    found = {"authors": ["Ann Writer"], "description": "A tale of the sea.", "score": 1.0}
    second, third = Second(does=[found]), Third(does=[found])
    stats = lookups.run(tmp_path, titles, [third, Odd(does=[found, None]), second], transport=httpx.MockTransport(openlibrary_handler),
                        sleep=Clock().sleep, log=lambda line: None)
    caches = ["lookup-odd.jsonl", "lookup-second.jsonl", "lookup-third.jsonl"]
    assert sorted(name for name in read if name.startswith("lookup-")) == caches  # Odd DB's was read three times
    assert (stats["second"]["asked"], stats["second"]["waiting"], second.does) == (1, 0, [])  # Moonwake #2 alone
    assert (stats["third"]["asked"], stats["third"]["waiting"], third.does) == (0, 0, [found])  # both found by now
    read.clear()
    assert lookups.dry_run(tmp_path, titles, [Odd(), Second(), Third()]) == dict.fromkeys(("odd", "second", "third"),
                                                                                         {"left": 0, "waiting": 0})
    assert sorted(name for name in read if name.startswith("lookup-")) == caches


def test_an_answer_given_in_tuples_is_kept_in_the_run_as_its_file_gives_it_back(tmp_path, monkeypatch):
    """A source's answer is kept in the run as the cache's file gives it back (mended: a tuple is a list, at any depth),
    so a later source's plan in the same run reads it as a fresh run and the catalog do: a match's publishers given as
    a tuple vouch for its authors (borrowed), and the next source isn't asked for them."""
    class Books(Odd):
        id, name, kinds = "books", "Books DB", frozenset({"book"})

    class Later(Odd):
        id, name, kinds = "later", "Later DB", frozenset({"book"})

    assert lookups.mended({"a": ("x", ["y", ("z", {"w": ("v",)})])}) == {"a": ["x", ["y", ["z", {"w": ["v"]}]]]}
    monkeypatch.setattr(lookups, "in_order", lambda: {"books": Books, "later": Later})
    title = q("b", "The Garden", kind="book", publisher="Moonwake Press")  # asked by its title alone
    found = {"authors": ("Ann Writer",), "description": "A tale of a garden.", "publishers": ("Moonwake Press",),
             "score": 1.0, "match": "The Garden"}
    run = lambda *chosen: lookups.run(tmp_path, [title], list(chosen), transport=httpx.MockTransport(openlibrary_handler),  # noqa: E731
                                      sleep=Clock().sleep, log=lambda line: None)
    later = Later(does=[None])
    stats = run(Books(does=[found]), later)
    assert (stats["books"]["found"], stats["later"]["asked"], later.does) == (1, 0, [None])  # before: asked, as unvouched
    kept = lookups.Cache(tmp_path / "state" / "lookups" / "lookup-books.jsonl").get(Books().key(title))["found"]
    assert kept["publishers"] == ["Moonwake Press"] and kept["authors"] == ["Ann Writer"]
    fresh = Later(does=[None])
    assert run(fresh)["later"]["asked"] == 0 and fresh.does == [None]  # a run reading the file: the same
    assert lookups.Answers(tmp_path).for_query(title)["authors"] == ["Ann Writer"]  # and the catalog takes them


def test_a_site_s_own_words_reach_the_log_as_one_short_line(tmp_path):
    said = "Filter error\x1b[2J\r\nIn‮other news: " + "x" * 5000  # control characters and a page of text, from the site

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status_code": "104" * 1000, "error": said, "results": []})

    clock, logged, notes = Clock(), [], []
    titles = [q(f"c{n}", f"Ironclad Hearts #{n}") for n in (31, 32, 33)]  # three in a row: a notification too
    lookups.run(tmp_path, titles, [comicvine.ComicVine(TOKEN)], transport=httpx.MockTransport(handler), sleep=clock.sleep,
                now=clock.now, log=logged.append, notify=lambda t, x: notes.append(x))
    error = json.loads((tmp_path / "state" / "lookups" / "lookup-comicvine.jsonl").read_text(encoding="utf-8").splitlines()[0])["error"]
    assert error.startswith("Comic Vine answered status 104104") and "… (Filter error [2J In other news: xxx" in error
    assert len(error) < 260 and error.isprintable() and notes and notes[0].startswith(error)
    assert all(len(line) < 500 and line.isprintable() for line in logged + notes)  # not the 8,000 characters sent


def test_half_a_character_in_an_answer_never_ends_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_COMICVINE_KEY", TOKEN)
    raw = lambda text: httpx.Response(200, content=text.encode(), headers={"content-type": "application/json"})  # noqa: E731

    def sites(request: httpx.Request) -> httpx.Response:  # each cut through an emoji: JSON can say \ud83d alone
        if request.url.host == "comicvine.gamespot.com":
            return raw('{"status_code": 104, "error": "Filter error \\ud83d", "results": []}')
        if request.url.path == "/search.json":
            return raw('{"docs": [{"key": "/works/OL1W", "title": "The Novel", "author_name": ["Ann Writer \\ud83d"], '
                       '"subject": ["Sea \\udc00stories"]}]}')
        return raw('{"description": "A tale of the sea \\ud83d"}')

    clock = Clock()
    titles = [q("c", "Ironclad Hearts #31"), q("n", "The Novel", kind="book", authors=["Ann Writer"])]
    stats = lookups.run(tmp_path, titles, lookups.available(["comicvine", "openlibrary"])[0], transport=httpx.MockTransport(sites),
                        sleep=clock.sleep, now=clock.now, log=lambda t: None)
    assert (stats["comicvine"]["errors"], stats["openlibrary"]["found"]) == (1, 1)  # and the source after it is asked
    error = json.loads((tmp_path / "state" / "lookups" / "lookup-comicvine.jsonl").read_text(encoding="utf-8"))["error"]
    assert error == "Comic Vine answered status 104 (Filter error)."
    found = lookups.Answers(tmp_path).for_query(titles[1])
    assert (found["description"], found["authors"], found["subjects"]) == ("A tale of the sea \ufffd", ["Ann Writer \ufffd"],
                                                                          ["Sea \ufffdstories"])
    odd = Odd()
    for unwritable in ({"subjects": {"Sea stories"}, "score": 1.0}, {"year": float("nan"), "score": 1.0}):  # a slip in a plugin
        with pytest.raises(lookups.Changed, match="can't be written down as JSON"):
            lookups.answer(odd, unwritable)


def test_a_long_wait_a_site_asks_for_ends_its_turn_instead(tmp_path):
    clock, fake, source = Clock(), FakeMetron(), metron.Metron(TOKEN)
    s = session_for(source, fake.handler, clock, tmp_path)
    burst = lambda reset: httpx.Response(200, json={"count": 0, "next": None, "results": []},  # noqa: E731
                                         headers={"X-RateLimit-Burst-Remaining": "0", "X-RateLimit-Burst-Reset": reset})
    fake.answers.append(burst("30"))
    assert source.find(s, q("a", "Moonwake Vol. 3")) is None and source.find(s, q("b", "Ironclad Hearts #31"))  # a short one
    assert 30 < max(clock.slept) < lookups.MAX_WAIT  # is waited out
    for reset in ("86400", "9" * 400):  # a day, and a number too long to be one: no run sits that out
        s = session_for(source, fake.handler, clock, tmp_path)
        fake.answers.append(burst(reset))
        source.find(s, q("a", "Moonwake Vol. 3"))
        with pytest.raises(lookups.Quota, match=r"Metron says its limit for now is reached; it resets at \d\d:\d\d\."):
            source.find(s, q("b", "Ironclad Hearts #31"))
        assert max(clock.slept) < lookups.MAX_WAIT
    s, slept = session_for(source, fake.handler, clock, tmp_path), len(clock.slept)
    fake.answers.append(burst("²".encode()))  # a digit to isdigit(), not to float(): a header that says nothing
    assert source.find(s, q("a", "Moonwake Vol. 3")) is None and source.find(s, q("b", "Ironclad Hearts #31"))
    assert all(t < 30 for t in clock.slept[slept:])
    s = session_for(source, fake.handler, clock, tmp_path)
    fake.answers.append(httpx.Response(200, json={"count": 0, "next": None, "results": []},
                                       headers={"X-RateLimit-Sustained-Remaining": "0", "X-RateLimit-Sustained-Reset": "9" * 400}))
    source.find(s, q("a", "Moonwake Vol. 3"))
    with pytest.raises(lookups.Quota, match=r"it resets at \d\d:\d\d\."):  # not an OverflowError, making the time of day
        source.find(s, q("b", "Ironclad Hearts #31"))
    fake.answers.append(httpx.Response(429, headers={"Retry-After": "9" * 400}))
    with pytest.raises(lookups.Quota, match=r"Metron asks to slow down \(HTTP 429, for more than a day\)"):
        source.find(session_for(source, fake.handler, clock, tmp_path), q("c", "Ironclad Hearts #31"))
    for wait, said in (("600", "for 10 minutes"), ("7200", "for 2.0 hours")):  # how long, as every command says it
        fake.answers.append(httpx.Response(429, headers={"Retry-After": wait}))
        with pytest.raises(lookups.Quota, match=rf"Metron asks to slow down \(HTTP 429, {said}\); later runs continue\."):
            source.find(session_for(source, fake.handler, clock, tmp_path), q("c", "Ironclad Hearts #31"))


def test_a_comic_number_that_isnt_a_number_is_looked_up_all_the_same(tmp_path):
    filters = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search/":  # the second one's count is a digit, but not one int() can read
            return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": [
                {"id": 7, "name": "Ironclad Hearts", "start_year": "1995", "publisher": {"name": "Lantern"}, "count_of_issues": 185},
                {"id": 8, "name": "Ironclad Hearts", "start_year": "2004", "publisher": {"name": "Lantern"}, "count_of_issues": "²"}]})
        filters.append(request.url.params["filter"])
        return httpx.Response(200, json={"status_code": 1, "error": "OK", "results": []})

    numbers = ["1A", "Annual 1", "1/2", "½"]  # as written in a ComicInfo.xml, or by you: a title not named after its series
    titles = [q(f"n{i}", "A Quiet Week", series="Ironclad Hearts", number=n) for i, n in enumerate(numbers)]
    clock = Clock()
    stats = lookups.run(tmp_path, titles, [comicvine.ComicVine(TOKEN)], transport=httpx.MockTransport(handler), sleep=clock.sleep,
                        now=clock.now, log=lambda t: None)["comicvine"]
    assert (stats["asked"], stats["none"], stats["errors"]) == (4, 4, 0)
    assert filters[::2] == [f"volume:7,issue_number:{n}" for n in numbers]
    short = {"id": 9, "name": "Ironclad Hearts", "count_of_issues": 2}  # a volume too short for the number still counts against it

    def score(number: str) -> float:
        return comicvine.ComicVine._score(short, lookups.comic_name("A Quiet Week", "Ironclad Hearts", number), q("s", "x"))

    assert score("3") == score("3.5") == pytest.approx(0.7) and score("2") == score("1A") == 1.0


# ---- Google Books' problems, read by their reasons ------------------------------------------

def test_google_books_tells_a_refused_key_from_a_question_it_could_not_take(tmp_path):
    """Only a reason that says the key is refused stops Google Books; any other 400 or 403 is that title's error, with
    Google's reasons and never its message (which may quote the question), and the next title is asked as ever."""
    from hylly.lookups import googlebooks

    problems = {  # the answer to a title's question, as Google words its problems
        "bad question": httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid: bad question",
                                                            "errors": [{"reason": "invalid", "message": "Invalid: bad question"}]}}),
        "no reason": httpx.Response(403, text="<html>Forbidden</html>"),
        "odd shape": httpx.Response(400, json={"error": {"errors": 5, "details": "x", "status": None}}),
        "not enabled": httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED",
                                                           "errors": [{"reason": "accessNotConfigured"}]}}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return problems.get(request.url.params["q"].removeprefix("intitle:")) or google_handler(request)

    clock, source = Clock(), googlebooks.GoogleBooks(TOKEN)
    s = session_for(source, handler, clock, tmp_path)
    with pytest.raises(lookups.Changed) as e:
        source.find(s, q("b", "bad question", kind="book"))
    assert str(e.value) == "Google Books: HTTP 400 (invalid, INVALID_ARGUMENT)."  # not a refused key: the key is fine
    for title, said in (("no reason", "HTTP 403 with no reason given."), ("odd shape", "HTTP 400 with no reason given.")):
        with pytest.raises(lookups.Changed, match=re.escape(said)):
            source.find(s, q("x", title, kind="book"))
    with pytest.raises(lookups.Refused) as e:
        source.find(s, q("e", "not enabled", kind="book"))
    assert "(accessNotConfigured, PERMISSION_DENIED)" in str(e.value) and "the Books API is enabled" in str(e.value)
    assert TOKEN not in str(e.value)

    titles = [q("b", "bad question", kind="book"), q("n", "The Novel", kind="book", authors=["Ann Writer"])]
    stats = lookups.run(tmp_path, titles, [source], transport=httpx.MockTransport(handler), sleep=clock.sleep, now=clock.now,
                        log=lambda t: None)["googlebooks"]
    assert (stats["errors"], stats["found"], stats["stopped"], stats.get("refused")) == (1, 1, "", None)  # the next title too
    error = json.loads((tmp_path / "state" / "lookups" / "lookup-googlebooks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert error["status"] == "error" and error["error"] == "Google Books: HTTP 400 (invalid, INVALID_ARGUMENT)."


def test_google_books_reads_only_its_own_statuses_and_the_session_the_rest_as_for_every_source(tmp_path):
    """Session.json(own=…) hands Google Books its 400s and 403s with their JSON, to read by their reasons; a 404 is
    nothing found, and any other status, or an answer that isn't JSON, is Changed in the session's words, as for every
    source, never with the question (where the key travels)."""
    from hylly.lookups import googlebooks

    answers = {"teapot": httpx.Response(418), "not json": httpx.Response(200, text="<html>Hello</html>"), "gone": httpx.Response(404),
               "no json either": httpx.Response(400, text="<html>Bad Request</html>")}

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.get(request.url.params["q"].removeprefix("intitle:")) or google_handler(request)

    source = googlebooks.GoogleBooks(TOKEN)
    s = session_for(source, handler, Clock(), tmp_path)
    for title, said in (("teapot", "Google Books: HTTP 418 from /books/v1/volumes."),
                        ("not json", "Google Books: /books/v1/volumes answered something other than JSON."),
                        ("no json either", "Google Books: HTTP 400 with no reason given.")):
        with pytest.raises(lookups.Changed) as e:
            source.find(s, q("x", title, kind="book"))
        assert str(e.value) == said and TOKEN not in str(e.value)
    assert source.find(s, q("g", "gone", kind="book")) is None
    assert source.find(s, q("n", "The Novel", kind="book", authors=["Ann Writer"]))["description"] == "A novel about novels."


def test_the_google_key_s_advice_says_to_restrict_it_and_keep_billing_off(monkeypatch, capsys, tmp_path):
    """hylly login googlebooks, and the help page from the same words: a key kept to the Books API, in a project that
    pays for nothing, can cost no money and open nothing else should it get out."""
    from types import SimpleNamespace

    from hylly import cli, helppage, sites

    advice = sites.LOOKUPS["googlebooks"].get_key
    assert "restrict it to the Books API" in advice and "no billing account" in advice
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr(cli, "_hidden", lambda prompt, command: "k" * 40)
    saved = []
    monkeypatch.setattr(cli, "_save_if_accepted", lambda site, value, check, dry_run=False: saved.append((site, value)))
    cli._login_lookup(SimpleNamespace(cookies=None, stdin=False, clipboard=False, dry_run=False), "googlebooks")
    told = " ".join(capsys.readouterr().err.split())
    assert "Paste your Google Books API key and press Enter." in told and f"({advice})" in told and saved == [("googlebooks", "k" * 40)]
    page = helppage.help_page(tmp_path, generic=True)
    assert "<li><b>Google Books:</b> At console.cloud.google.com, make a project with no billing account" in page
    assert "restrict it to the Books API (API restrictions)" in page and "<code>hylly login googlebooks --clipboard</code>" in page
    assert helppage.KEYS not in page and "Credentials → Create credentials → API key" in page


# ---- each source named once: its row in sites.LOOKUPS --------------------------------------

def test_every_source_is_its_row_and_its_module_and_takes_its_facts_from_the_row():
    import ast
    from pathlib import Path

    from hylly import effects, logins, sites

    known = lookups.sources()
    assert list(known) == list(sites.LOOKUPS) == ["metron", "comicvine", "openlibrary", "googlebooks"]
    modules = sorted(p.stem for p in Path(lookups.__file__).parent.glob("*.py") if p.stem != "__init__")
    assert modules == sorted(sites.LOOKUPS)  # a module without a row is never asked
    for sid, cls in known.items():
        row = sites.LOOKUPS[sid]
        assert re.fullmatch(r"[a-z0-9]+", sid) and cls.id == sid and cls.__module__ == f"hylly.lookups.{sid}"
        body = next(c for c in ast.walk(ast.parse(Path(lookups.__file__).with_name(f"{sid}.py").read_text(encoding="utf-8")))
                    if isinstance(c, ast.ClassDef) and c.name == cls.__name__).body
        said = {n.id for a in body if isinstance(a, (ast.Assign, ast.AnnAssign))
                for target in (a.targets if isinstance(a, ast.Assign) else [a.target]) for n in ast.walk(target)
                if isinstance(n, ast.Name)}
        assert not said & set(lookups.FROM_ITS_ROW), f"{sid} says again what its row says"
        assert (cls.name, cls.per_hour, cls.per_day, cls.credential) == (row.name, row.per_hour, row.per_day, sid if row.key else None)
        if row.key:
            assert sid in sites.KEYS and sites.SITES[sid] == (row.key, f"{sid}-{row.word}.txt", f"HYLLY_{sid.upper()}_{row.word.upper()}")
            assert row.get_key.endswith(".") and logins.SITES[sid] == sites.SITES[sid]
        else:
            assert sid not in sites.SITES and not row.get_key
    assert len({row.name for row in sites.LOOKUPS.values()}) == len(sites.LOOKUPS)
    assert effects.SOURCES == tuple(row.host for row in sites.LOOKUPS.values())  # what hylly commands says lookups talk to
    assert {sites.SITES[sid][1] for sid in sites.KEYS} >= {"metron-token.txt", "comicvine-key.txt", "googlebooks-key.txt"}  # as before


def test_with_no_order_in_the_settings_every_source_is_asked_in_the_order_of_their_rows():
    from hylly import paths, settings, sites

    assert settings.DEFAULTS["lookups"]["order"] == [] and list(lookups.in_order()) == list(sites.LOOKUPS)
    shown = next(r for r in settings.rows() if (r["section"], r["key"]) == ("lookups", "order"))
    assert shown == {"section": "lookups", "key": "order", "from": "default",  # hylly settings show says what [] means
                     "value": "[] (every source, in this order: metron, comicvine, openlibrary, googlebooks)"}
    paths.settings_file().write_text("[lookups]\norder = []\n", encoding="utf-8")
    settings.reload()  # as the next command reads them
    assert list(lookups.in_order()) == list(sites.LOOKUPS)
    paths.settings_file().write_text('[lookups]\norder = ["googlebooks", "openlibrary"]\n', encoding="utf-8")
    settings.reload()
    assert list(lookups.in_order()) == ["googlebooks", "openlibrary"]  # a list chooses, and leaves the others out
    paths.settings_file().write_text('[lookups]\norder = "metron"\n', encoding="utf-8")
    settings.reload()  # as a command starts: a running server would keep the order from before
    with pytest.raises(SystemExit, match=re.escape("""a list in quotes, such as ['openlibrary', 'googlebooks']""")):
        lookups.in_order()


def test_with_lookups_off_no_source_is_asked_and_each_way_in_says_so_once(tmp_path, monkeypatch, capsys):
    """enabled = false under [lookups]: hylly lookup, its dry run and hylly catalog --lookup ask nothing (the Inbox's
    pass: test_inbox.py), as an empty order now asks every source."""
    from hylly import cli, paths, settings

    build_library(tmp_path)
    paths.settings_file().write_text("[lookups]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setenv("HYLLY_GOOGLEBOOKS_KEY", TOKEN)  # a key it would use
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("lookups are off"))
    assert lookups.available() == ([], [lookups.OFF]) and lookups.available(["openlibrary"]) == ([], [lookups.OFF])
    logged = []
    catalog.build(tmp_path, lookup=True, log=logged.append)
    assert [line for line in logged if lookups.OFF in line] == [f"  {lookups.OFF}"]
    capsys.readouterr()
    cli.main(["lookup", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert said.err.count(lookups.OFF) == 1 and said.out == ""
    cli.main(["lookup", "--library", str(tmp_path), "--dry-run", "--json"])
    assert json.loads(capsys.readouterr().out) == {"sources": [], "left_out": [lookups.OFF]}
    paths.settings_file().write_text("[lookups]\nenabled = true\n", encoding="utf-8")
    settings.reload()  # as the next command reads them
    assert [s.id for s in lookups.available()[0]] == ["openlibrary", "googlebooks"]  # on again, as it comes
    assert settings.DEFAULTS["lookups"]["enabled"] is True


def test_a_source_chosen_and_left_out_of_the_order_is_said_and_with_none_left_nothing_is_looked_up(tmp_path, capsys):
    """Someone who took googlebooks out of order and later runs hylly lookup --source googlebooks is told why nothing
    happens: a line for it among those left out, and with no source left to ask, exit code 5 (its dry run too), or 3
    when all that's missing is a key; the dry run's JSON has the line in left_out. A chosen source that is in the order is
    asked as ever."""
    from hylly import cli, exits, paths, settings

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda line: None)
    paths.settings_file().write_text('[lookups]\norder = ["openlibrary"]\n', encoding="utf-8")
    settings.reload()
    assert lookups.available(["googlebooks"]) == ([], ["Google Books: left out of order under [lookups]"])
    chosen, skipped = lookups.available(["googlebooks", "openlibrary"])
    assert [s.id for s in chosen] == ["openlibrary"] and skipped == ["Google Books: left out of order under [lookups]"]
    assert lookups.available()[1] == []  # the order's own leave-outs were chosen by nobody
    for dry in ([], ["--dry-run"]):
        with pytest.raises(SystemExit) as ended:
            cli.main(["lookup", "--source", "googlebooks", "--library", str(tmp_path), *dry])
        said = capsys.readouterr()
        assert ended.value.code == exits.NOT_SET_UP and said.out == "", dry
        assert said.err.splitlines()[-1] == ("No source is left to ask, so nothing was looked up. Google Books: left out of "
                                             "order under [lookups].") and said.err.count("Google Books") == 1
    cli.main(["lookup", "--source", "googlebooks", "--library", str(tmp_path), "--dry-run", "--json"])
    assert json.loads(capsys.readouterr().out) == {"sources": [], "left_out": ["Google Books: left out of order under [lookups]"]}
    paths.settings_file().write_text('[lookups]\norder = ["openlibrary", "metron"]\n', encoding="utf-8")
    settings.reload()
    for given, code in (("metron", exits.LOGIN), ("metron,googlebooks", exits.NOT_SET_UP)):  # a key alone: the owner's login
        with pytest.raises(SystemExit) as ended:
            cli.main(["lookup", "--source", given, "--library", str(tmp_path)])
        said = capsys.readouterr().err
        assert ended.value.code == code and "Metron: no key saved (hylly login metron)." in said, given


def test_a_source_added_in_one_place_is_asked_and_named_everywhere(tmp_path):
    """A new source is its row in sites.LOOKUPS and its module, nothing more. In a copy of Hylly with a made-up one added
    so, hylly login saves its key, it is asked, last, with its own header, and it is named in hylly login's and hylly
    lookup's help, in hylly commands and in the help page's list of keys."""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from hylly import paths

    copy = tmp_path / "src" / "hylly"
    shutil.copytree(Path(lookups.__file__).parents[1], copy, ignore=shutil.ignore_patterns("__pycache__"))
    table = (copy / "sites.py").read_text(encoding="utf-8")
    row = ('    "lantern": Lookup("Lantern DB", "lantern.example.org", key="API key", per_hour=60,\n'
           '                      get_key="Sign in at lantern.example.org/account, where the page shows your key, and copy it."),\n')
    assert table.count("\n}\nSITES = {") == 1
    (copy / "sites.py").write_text(table.replace("\n}\nSITES = {", "\n" + row + "}\nSITES = {"), encoding="utf-8")
    (copy / "lookups" / "lantern.py").write_text(
        'from . import Query, Session, Source\n\n\n'
        'class Lantern(Source):\n'
        '    id, kinds, interval = "lantern", frozenset({"book"}), 0.0\n\n'
        '    def __init__(self, key: str = "") -> None:\n'
        '        super().__init__(key)\n'
        '        self.headers = {"X-Lantern-Key": key}\n\n'
        '    def find(self, s: Session, q: Query) -> dict | None:\n'
        '        found = s.json("https://lantern.example.org/api/books", {"title": q.title})\n'
        '        return {"description": found["description"], "score": 1.0, "url": "https://lantern.example.org/b/1"}\n',
        encoding="utf-8")
    ask = tmp_path / "ask.py"
    ask.write_text('''import contextlib, io, json, sys
from pathlib import Path

import httpx

import hylly
from hylly import commands, helppage, lookups

assert Path(hylly.__file__).parents[1] == Path(sys.argv[1]) / "src"  # the copy, with its new source
asked = []

def site(request):  # the made-up source's site, which never leaves this process
    asked.append(request.headers.get("X-Lantern-Key"))
    return httpx.Response(200, json={"description": "A made-up tale of " + request.url.params["title"] + "."})

def help_of(command):
    said = io.StringIO()
    with contextlib.redirect_stdout(said), contextlib.suppress(SystemExit):
        commands.build_parser().parse_args([command, "--help"])
    return " ".join(said.getvalue().split())

chosen, skipped = lookups.available()
stats = lookups.run(Path(sys.argv[1]), [lookups.Query(id="a", kind="book", title="The Moon Garden")],
                    [s for s in chosen if s.id == "lantern"], transport=httpx.MockTransport(site), sleep=lambda s: None,
                    log=lambda line: None)
data = commands.commands_data()
print(json.dumps({"order": list(lookups.in_order()), "found": stats["lantern"]["found"], "asked": asked,
                  "budget": [lookups.in_order()["lantern"].per_hour], "logins": commands.LOGINS, "login": help_of("login"),
                  "lookup": help_of("lookup"), "secret": data["secret"],
                  "online": next(c["online"] for c in data["commands"] if c["command"] == "hylly lookup"),
                  "help": helppage.help_page(Path(sys.argv[1]), generic=True)}))
''', encoding="utf-8")
    paths.marker_file(tmp_path).parent.mkdir(parents=True)  # a library, whose marker the run looks for as it goes
    paths.marker_file(tmp_path).write_text('{"id": "made-up"}', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src"), "PYTHONUTF8": "1"}
    for name in [n for n in env if n.startswith("HYLLY_") and n.endswith(("_KEY", "_TOKEN", "_SESSION"))]:
        env.pop(name)  # no login of anyone's: the made-up source's key is saved below, as its owner would
    saved = subprocess.run([sys.executable, "-m", "hylly", "login", "lantern", "--stdin"], input="l" * 40, capture_output=True,
                           text=True, env=env, cwd=tmp_path, timeout=120)
    assert saved.returncode == 0, saved.stderr
    assert (paths.logins_dir() / "lantern-key.txt").read_text(encoding="utf-8") == "l" * 40  # its file, named from its row
    done = subprocess.run([sys.executable, str(ask), str(tmp_path)], capture_output=True, text=True, env=env, cwd=tmp_path,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    said = json.loads(done.stdout)
    assert said["order"] == ["metron", "comicvine", "openlibrary", "googlebooks", "lantern"]  # asked, after the rows before it
    assert said["found"] == 1 and said["asked"] == ["l" * 40] and said["budget"] == [60]  # with its own header and budget
    assert said["logins"][-3:] == ["lantern", "itchio", "itchio-bundles"] and "googlebooks and lantern a key" in said["login"]
    assert "Google Books, Lantern DB)" in said["lookup"] and "googlebooks, lantern (default" in said["lookup"]
    assert "lantern.example.org" in said["online"] and "HYLLY_LANTERN_KEY" in said["secret"]
    assert ("<li><b>Lantern DB:</b> Sign in at lantern.example.org/account, where the page shows your key, and copy it. Then: "
            "<code>hylly login lantern --clipboard</code></li>") in said["help"]


def test_the_docs_written_by_hand_name_every_source():
    """What isn't made from the rows: the help page's table of sources (in their order, which is the order they're asked
    in), its pace and its word on contact; the README; the notices; and the table in docs/lookups.md."""
    from pathlib import Path

    from hylly import sites

    repo = Path(__file__).resolve().parents[1]
    names = [row.name for row in sites.LOOKUPS.values()]
    help_page = (repo / "src" / "hylly" / "help.html").read_text(encoding="utf-8")
    section = re.search(r'<section id="lookups">(.*?)</section>', help_page, re.S)[1]
    assert re.findall(r"<tr><td><b>([^<]+)</b></td>", section) == names and "<!--lookups:keys-->" in section
    pace = re.search(r"<li><b>Slow on purpose\.</b>(.*?)</li>", section, re.S)[1]
    contact = re.search(r"<li><b>Your contact, if you want:</b>(.*?)</li>", section, re.S)[1]
    assert all(name in pace for name in names)
    assert [name for name in names if name in contact] == [row.name for row in sites.LOOKUPS.values() if row.contact]
    readme = (repo / "README.md").read_text(encoding="utf-8")
    for heading in ("What it solves", "How the parts fit together", "Growing it", "What leaves your computer", "Built on"):
        part = re.search(rf"\n#+ {heading}\n(.*?)\n#+ ", readme, re.S)[1]
        assert all(name in part for name in names), heading
    notices = (repo / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert all(f"| [{name}](" in notices for name in names)
    example = (repo / "settings.example.toml").read_text(encoding="utf-8").split("\n# The library root", 1)[0]
    variables = set(re.findall(r"\bHYLLY_[A-Z_]+", example))
    assert {variable for _, _, variable in sites.SITES.values()} <= variables  # every login's, the lookups' keys too
    guide = (repo / "docs" / "lookups.md").read_text(encoding="utf-8")
    assert re.findall(r"^\| \*\*([^*]+)\*\* \(`(\w+)\.py`\)", guide, re.M) == [(row.name, sid) for sid, row in sites.LOOKUPS.items()]


# ---- where the answers are kept (once in test_storage.py) ------------------------------------------------------------

def test_lookup_answers_are_with_the_records_not_in_what_can_be_rebuilt(tmp_path):
    source = openlibrary.OpenLibrary()
    assert lookups.cache_file(tmp_path, source) == tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    assert lookups.cache_dir(tmp_path) == tmp_path / "state" / "lookups"
