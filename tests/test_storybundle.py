"""StoryBundle source against a fake site shaped like the real pages (2026-10-25)."""

from __future__ import annotations

import csv
import functools
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from hylly import cli, exits, shopping, shopsession
from hylly import download as dl
from hylly import storybundle as sbm
from hylly.logins import AuthError, ShopError

from helpers import FILES, PAGES, StoryBundleSite as FakeSite, bonus_li, book_li


@pytest.fixture
def site():
    s = FakeSite()
    return s, httpx.MockTransport(s.handler)


def test_reads_bundles_and_books(site):
    _, transport = site
    with sbm.StoryBundle("c" * 80, transport=transport, delay=0) as sb:
        bundles = sb.bundles()
    assert [(b.name, b.purchased, b.bonus) for b in bundles] == [
        ("The Example Game Book Bundle", "2020-01-01", False), ("The Example Newsletter Bonus", "2010-03-01", True)]
    assert [(k.id, k.title, k.approx_size) for k in bundles[0].books] == [("1", "Opening the Box", int(0.4 * 1024**2)), ("2", "Day of the Lantern", int(0.73 * 1024**2))]
    assert [(k.id, k.title, k.approx_size) for k in bundles[1].books] == [("2", "Day of the Lantern", 0), ("3", "Hexes and Charms", 0)]
    assert bundles[0].books[0].cover.endswith("c.jpg?17")
    assert sbm.title_from_cover("https://x/The_Sisters_Example_Cover_Final.jpg?17") == "The Sisters Example"


def test_logged_out_is_an_auth_error(site):
    s, transport = site
    s.logged_in = False
    with sbm.StoryBundle("c" * 80, transport=transport, delay=0) as sb, pytest.raises(AuthError):
        sb.bundles()


def test_downloads_each_book_once_and_resumes_nothing_next_time(site, tmp_path, monkeypatch):
    """On the shops' core: a book in two bundles comes once, in the oldest (Title.place), named by the link its
    download starts with, which then fetches it (shopping.Naming): one link request a book, and a fresh one only for a
    link already dead; recorded as every shop's files are, its rounded size never as what StoryBundle listed; a run
    again finds every book done."""
    s, transport = site
    s.expired_once.add(2)  # this link is already dead when the download starts: a fresh one is fetched
    monkeypatch.setattr(sbm, "StoryBundle", functools.partial(sbm.StoryBundle, transport=transport, delay=0))
    manifest = dl.Manifest(tmp_path, "storybundle")
    with sbm.Library("c" * 80) as lib:
        titles = lib.titles()
        sel = shopping.select("storybundle", titles, manifest, tmp_path)
        assert len(sel.pending) == 3 and sel.again == 1  # Day of the Lantern is in both bundles
        assert sel.pending[0][2] == "storybundle/The Example Newsletter Bonus/Day of the Lantern"  # oldest bundle first
        assert [f.name for _, f, _, _ in sel.pending] == ["", "", ""] and not s.posts  # named only as the run asks
        jobs = [shopping.make_job("storybundle", t, f, path) for t, f, path, _ in sel.pending]
        chosen = {id(job): (t, f) for job, (t, f, _, _) in zip(jobs, sel.pending)}
        d = dl.Downloader(tmp_path, manifest, refresh=lambda j: lib.link(*chosen[id(j)]), transport=transport,
                          log=lambda line: None, backoff=0.0, link_refreshes=5,
                          name=shopping.Naming("storybundle", tmp_path, manifest, lib.link, chosen))
        results = d.run(jobs)
    assert len(results["ok"]) == 3, results["failed"]
    assert s.posts == 3 + 1  # a link for each book, which named and fetched it, and a fresh one for the dead one
    entry = manifest.get("storybundle/The Example Game Book Bundle/Opening the Box/Opening_the_Box_-_Ann_Writer.epub")
    assert entry["source"] == "storybundle" and (entry["item"], entry["file"]) == ("1", 0) and entry["md5_source"] == "cdn"
    assert entry["shop_id"] == "storybundle:1:0" and entry["listed_md5"] is None and "listed_size" not in entry  # rounded
    assert (entry["kind"], entry["device"], entry["bonus"], entry["bundle"]) == ("book", "ereader", False, "The Example Game Book Bundle")
    assert not {"humble_id", "humble_md5", "humble_size", "storybundle_id"} & set(entry)
    assert (tmp_path / entry["path"]).read_bytes() == FILES[1][1]
    again = shopping.select("storybundle", titles, dl.Manifest(manifest.root, manifest.shop), tmp_path)
    assert again.pending == [] and again.done == 3
    nothing = dl.Manifest(tmp_path, "storybundle")
    nothing.entries.clear()  # as if nothing were downloaded yet
    assert shopping.select("storybundle", titles, nothing, tmp_path, skip=sbm.bonus).skipped == 2


BOOK = sbm.Book(id="1", title="Opening the Box", approx_size=410000, link="https://storybundle.com/books/1/download/K1")
ELSEWHERE = ("https://storybundle.s3.us-west-2.amazonaws.com/system/downloads/media/1/Opening_the_Box_-_Ann_Writer.epub"
             "?X-Amz-Signature=s1")  # on Amazon S3, where StoryBundle keeps its files


def answering(s: FakeSite, *answers: httpx.Response) -> tuple[sbm.StoryBundle, list[str]]:
    """A session whose book downloads StoryBundle answers with these, one after another (the last one stays), and the
    downloads asked for."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            asked.append(request.url.path)
            return answers[min(len(asked), len(answers)) - 1]
        return s.handler(request)

    sb = sbm.StoryBundle("c" * 80, transport=httpx.MockTransport(handler), delay=0, backoff=0)
    sb.token = "TOKEN"
    return sb, asked


def test_a_book_s_link_is_taken_only_on_amazon_s3_where_storybundle_keeps_its_files(site):
    """By the address's own host, not by its words anywhere in it: a link to another site, with amazonaws.com in its
    path, its query or its name's start, is the book's trouble, and isn't followed."""
    s, _ = site
    sb, asked = answering(s, httpx.Response(302, headers={"location": ELSEWHERE}))
    with sb:
        assert sb.resolve(BOOK) == ELSEWHERE and asked == ["/books/1/download/K1"]
    for odd in ("https://files.example.com/amazonaws.com/a.epub", "https://files.example.com/a.epub?at=x.amazonaws.com",
                "https://amazonaws.com.example.com/a.epub", "https://files.example.com/a.epub"):
        sb, _ = answering(s, httpx.Response(302, headers={"location": odd}))
        with sb, pytest.raises(ShopError, match="to another site than the one it keeps its files on") as e:
            sb.resolve(BOOK)
        assert "example" not in str(e.value) and "K1" not in str(e.value)


def test_only_a_way_back_to_the_login_or_the_front_page_is_a_refused_cookie(site):
    s, _ = site
    for back in ("https://storybundle.com/", "/", "https://storybundle.com/login?next=x", "https://www.storybundle.com/users/sign_in"):
        sb, _ = answering(s, httpx.Response(302, headers={"location": back}))
        with sb, pytest.raises(AuthError, match="did not accept the cookie"):
            sb.resolve(BOOK)
    for odd in ("https://storybundle.com/downloads/KEYA", "http://files.example.com/media/1/a.epub"):
        sb, _ = answering(s, httpx.Response(302, headers={"location": odd}))
        with sb, pytest.raises(ShopError) as e:  # the book's trouble: the run goes on with the others
            sb.resolve(BOOK)
        assert "KEYA" not in str(e.value) and "K1" not in str(e.value) and "files.example.com" not in str(e.value)


@pytest.mark.parametrize("status", [404, 500, 429])
def test_storybundle_s_error_fails_that_book_with_its_status_and_a_busy_one_is_asked_again(site, status):
    s, _ = site
    sb, asked = answering(s, httpx.Response(status))
    with sb, pytest.raises(ShopError) as e:  # not an AuthError: the cookie was fine
        sb.resolve(BOOK)
    assert e.value.status == status and f"HTTP {status}" in str(e.value) and "K1" not in str(e.value)
    assert len(asked) == (1 if status == 404 else shopsession.TRIES)  # 429 and 5xx: asked again first, as the other shops do


def test_storybundle_s_wait_is_waited_out_and_then_the_link_comes(site, monkeypatch):
    s, _ = site
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    sb, asked = answering(s, httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503),
                          httpx.Response(302, headers={"location": ELSEWHERE}))
    with sb:
        assert sb.resolve(BOOK) == ELSEWHERE and len(asked) == 3
    assert any(6.9 < s <= 7 for s in slept)  # the wait, from the answer's time


def test_a_dropped_connection_or_a_busy_moment_is_asked_again_for_pages_and_links_alike(site):
    """StoryBundle's pages once weren't asked again at all, nor a book's link after a dropped connection, so one such
    moment ended a run part-way. Now each is asked again, as with every shop; no answer after every try is a ShopError
    of status 0 (passing), without the book's link."""
    s, _ = site
    trouble: list = []  # what the next requests meet first, one each: "dropped", or a status

    def handler(request: httpx.Request) -> httpx.Response:
        if trouble:
            what = trouble.pop(0)
            if what == "dropped":
                raise httpx.ConnectError("the connection dropped", request=request)
            return httpx.Response(what)
        return s.handler(request)

    with sbm.StoryBundle("c" * 80, transport=httpx.MockTransport(handler), delay=0, backoff=0) as sb:
        trouble += ["dropped", 503, 429]  # the bundles page meets all three
        bundles = sb.bundles()
        assert len(bundles) == 2 and not trouble
        trouble += ["dropped", 502]
        assert sb.resolve(bundles[0].books[0]).startswith("https://storybundle.s3.")
        trouble += ["dropped"] * shopsession.TRIES
        with pytest.raises(ShopError) as e:
            sb.resolve(bundles[0].books[0])
    assert e.value.status == 0 and e.value.passing and "K1" not in str(e.value) and not trouble


def test_the_cookie_storybundle_rewrites_is_the_one_sent_next():
    """StoryBundle's own way, kept: Rails rewrites its session cookie as you go, so its session keeps a cookie jar
    (the other shops send their login as it was given) and sends the new one back."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("cookie", ""))
        return httpx.Response(200, text="page", headers={"set-cookie": f"_storybundle31_session={'n' * 80}; path=/"})

    with sbm.StoryBundle("c" * 80, transport=httpx.MockTransport(handler), delay=0) as sb:
        sb._get("/x", "a page")
        sb._get("/y", "a page")
    assert sent == [f"_storybundle31_session={'c' * 80}", f"_storybundle31_session={'n' * 80}"]


@pytest.fixture
def command(site, tmp_path, monkeypatch):
    """hylly storybundle download against the fake site. refuse: a book's id and the status StoryBundle answers its link
    requests with; again: the status each book's link requests after its first are answered with (0 for either: no
    answer at all); posts: the link requests made, by book id; run(*options): the exit code and message; failures(): the paths
    listed in state/storybundle/failures.csv; done(): the ids of the books recorded; log(): state/download-log.txt."""
    s, _ = site
    state = tmp_path / "state"
    it = SimpleNamespace(refuse={}, again=None, posts=[])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.startswith("/books/"):
            book = request.url.path.split("/")[2]
            it.posts.append(book)
            status = it.again if it.again is not None and it.posts.count(book) > 1 else it.refuse.get(book)
            if status == 0:
                raise httpx.ConnectError("no answer", request=request)
            if status:
                return httpx.Response(status)
        return s.handler(request)

    transport, real = httpx.MockTransport(handler), sbm.StoryBundle
    monkeypatch.setattr(sbm, "StoryBundle", lambda session: real(session, transport=transport, delay=0, backoff=0))
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=transport, backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)  # the room on this computer's disk is no part of it
    monkeypatch.setenv("HYLLY_STORYBUNDLE_SESSION", "c" * 80)

    def run(*options: str) -> tuple[int, str]:
        try:
            cli.main(["storybundle", "download", "--library", str(tmp_path), *options])
        except SystemExit as e:
            return e.code, str(e)
        return 0, ""

    def failures() -> list[str]:
        with (state / "storybundle" / "failures.csv").open(encoding="utf-8-sig", newline="") as fh:
            return [row["path"] for row in csv.DictReader(fh)]

    it.run, it.failures, it.log = run, failures, lambda: (state / "download-log.txt").read_text(encoding="utf-8")
    it.root = tmp_path
    it.done = lambda: sorted(e["item"] for e in dl.Manifest(tmp_path, "storybundle").entries.values())
    return it


HEXES = "storybundle/The Example Newsletter Bonus/Hexes and Charms"  # book 3's folder


def test_each_book_s_link_is_asked_for_once_and_both_names_and_fetches_it(command, capsys):
    """A book's name comes only with its link, which lives a minute: the link its download starts with names the file
    and fetches it, so a run makes one link request a book, not one to name it and one more to fetch it, and its first
    download starts at once rather than after a naming pass of a second a book."""
    assert command.run("--jobs", "1") == (0, "") and command.posts == ["2", "3", "1"]  # oldest bundle first
    assert "Getting the name" not in capsys.readouterr().err and command.done() == ["1", "2", "3"]


def test_left_out_json_that_can_t_be_written_never_takes_the_place_of_how_the_run_ended(command, monkeypatch):
    """What a run learnt of the books left out by their links' names (state/storybundle/left-out.json) is kept for the
    runs after it; not written (a share dropping out, say), those books are only asked about again, and the run ends as
    it would have, its own exit and words."""
    monkeypatch.setitem(FILES, 3, ("Hexes and Charms Setup.exe", b"MZ" + bytes(9000)))
    command.refuse = {"1": 404}

    def cant(*args, **kwargs):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(shopping, "_keep_left_out", cant)
    assert command.run("--jobs", "1") == (exits.PARTLY, "1 file failed: see state/storybundle/failures.csv. Running it "
                                                       "again tries them again.")
    assert command.done() == ["2"] and not (command.root / "state" / "storybundle" / "left-out.json").exists()


def test_a_book_without_a_link_fails_alone_and_the_run_goes_on(command, capsys):
    command.refuse = {"3": 404}
    assert command.run() == (exits.PARTLY, "1 file failed: see state/storybundle/failures.csv. Running it again tries them again.")
    said = f"FAILED Hexes and Charms ({HEXES}): StoryBundle answered HTTP 404 when asked for a book's download link."
    assert said in capsys.readouterr().err and said in command.log()  # on the screen, as the run's progress, and in the log
    assert command.done() == ["1", "2"] and command.failures() == [HEXES]  # listed by its folder: its file isn't known
    command.refuse = {}  # StoryBundle has the file again
    assert command.run() == (0, "") and command.done() == ["1", "2", "3"] and command.failures() == []


def test_no_answer_for_a_book_s_first_link_after_every_try_fails_that_book_alone_as_for_a_fresh_link(command):
    """The link a book's download starts with is asked again first, as every link is: no answer after every try is
    that book's failure, and the others come, as for a fresh link; only a few books in a row unanswered stop the run
    (below)."""
    command.refuse = {"2": 0}  # the first book asked about: books 2, 3 and 1, in that order
    code, said = command.run("--jobs", "1")
    assert (code, said) == (exits.PARTLY, "1 file failed: see state/storybundle/failures.csv. Running it again tries "
                                          "them again.")
    assert command.posts == ["2"] * shopsession.TRIES + ["3", "1"] and command.done() == ["1", "3"]
    assert "No answer from StoryBundle" in command.log()


@pytest.mark.parametrize("status", [0, 503])
def test_an_outage_stops_the_run_after_a_few_books_not_after_every_one(command, monkeypatch, status):
    """StoryBundle not answering a book's link at all, or only with its own trouble (503), each asked again first, a few
    books in a row: it's down, so the run stops, as for books turned away, rather than fail the rest one after another
    (download.UNANSWERED_IN_A_ROW)."""
    monkeypatch.setattr(dl, "UNANSWERED_IN_A_ROW", 2)  # three books here
    command.refuse = {"1": status, "2": status, "3": status}
    code, said = command.run("--jobs", "1")
    assert (code, said) == (exits.PARTLY, dl.unanswered()) and "2 files in a row" in said and said in command.log()
    assert command.posts == ["2"] * shopsession.TRIES + ["3"] * shopsession.TRIES  # book 1 isn't asked
    assert command.done() == [] and len(command.failures()) == 2


def test_a_book_whose_link_names_a_program_is_not_downloaded_and_the_run_names_it(command, monkeypatch, capsys):
    """A StoryBundle book's name comes only with its link, so a run leaves out one of a kind no download takes
    (download.never) as its link names it, without fetching it, and names it, on the screen and in the log: no failure.
    It is kept as left out by that name (state/storybundle/left-out.json), so the runs after it, a dry run too, name it
    at once, neither asking for its link again nor counting it to download; until StoryBundle lists the book with
    another size, when its link is asked for again."""
    monkeypatch.setitem(FILES, 3, ("Hexes and Charms Setup.exe", b"MZ" + bytes(9000)))
    assert command.run() == (0, "") and command.done() == ["1", "2"] and command.failures() == []
    named = "  'Hexes and Charms': 'Hexes and Charms Setup.exe' (EXE, a program, script or shortcut)"
    said = f"Left out, not failures: 1 file of a kind no download takes (programs, scripts and shortcuts):\n{named}\n"
    out = capsys.readouterr()
    assert said in out.out and named in command.log() and not (command.root / HEXES).exists()
    assert "Run started: 3 files" in out.err and "Run ended: 2 files done" in command.log()  # named only by its link
    assert sorted(command.posts) == ["1", "2", "3"]  # one link request a book
    kept = json.loads((command.root / "state" / "storybundle" / "left-out.json").read_text(encoding="utf-8"))
    assert kept == [{"item": "3", "file": 0, "name": "Hexes and Charms Setup.exe", "size": 0}]
    for again in ([], ["--dry-run"]):
        command.posts.clear()
        assert command.run(*again) == (0, "") and command.posts == []  # not asked again: the others are done
        out = capsys.readouterr().out
        assert said in out and "To download: 0 titles, 0 files" in out
    monkeypatch.setitem(PAGES, "/downloads/KEYB", PAGES["/downloads/KEYB"].replace(  # a size for it now: asked again
        bonus_li(3, "Hexes_and_Charms_Cover_Final.jpg"), book_li(3, "Hexes and Charms", "0.01MB")))
    assert command.run() == (0, "") and command.posts == ["3"] and said in capsys.readouterr().out
    kept = json.loads((command.root / "state" / "storybundle" / "left-out.json").read_text(encoding="utf-8"))
    assert kept == [{"item": "3", "file": 0, "name": "Hexes and Charms Setup.exe", "size": int(0.01 * 1024**2)}]


def test_a_book_recorded_but_missing_or_cut_short_comes_again_to_its_own_place(command):
    """As every shop's on the core: a book whose file is gone from the disk, or cut short, comes back to the folder and
    under the name it was recorded with, never into a new "Title (2)" folder beside the old one (StoryBundle's own flow
    did that). Its link is asked what it names now first, as before, and the file comes there while that is a file of
    the same kind. The one cut short is kept in superseded/."""
    assert command.run() == (0, "")
    box = command.root / "storybundle" / "The Example Game Book Bundle" / "Opening the Box" / FILES[1][0]
    lantern = command.root / "storybundle" / "The Example Newsletter Bonus" / "Day of the Lantern" / FILES[2][0]
    box.unlink()
    lantern.write_bytes(FILES[2][1][:1000])
    command.posts.clear()
    assert command.run() == (0, "") and sorted(command.posts) == ["1", "2"]  # one link each: its name, and its download
    assert box.read_bytes() == FILES[1][1] and lantern.read_bytes() == FILES[2][1]
    assert not [p for p in (command.root / "storybundle").rglob("*") if "(2)" in p.name]
    assert (command.root / "superseded" / lantern.relative_to(command.root)).read_bytes() == FILES[2][1][:1000]
    assert len(dl.Manifest(command.root, "storybundle").entries) == 3


LANTERN = "storybundle/The Example Newsletter Bonus/Day of the Lantern"  # book 2's folder


def test_a_missing_book_whose_link_names_another_format_now_comes_under_that_name_in_its_folder(command, monkeypatch):
    """A book recorded but gone from the disk, whose link names another kind of file now (StoryBundle put a PDF in the
    EPUB's place): it comes under the link's name in the book's own folder, once, rather than under the old name, whose
    kind would refuse it every run; and with that, the book is done."""
    assert command.run() == (0, "")
    (command.root / LANTERN / FILES[2][0]).unlink()
    monkeypatch.setitem(FILES, 2, ("Day_of_the_Lantern.pdf", b"%PDF-1.4\n" + b"p" * 760000))
    command.posts.clear()
    assert command.run() == (0, "") and command.posts == ["2"] and command.failures() == []
    assert (command.root / LANTERN / "Day_of_the_Lantern.pdf").read_bytes() == FILES[2][1]
    assert not [p for p in (command.root / "storybundle").rglob("*") if "(2)" in p.name]
    command.posts.clear()
    assert command.run() == (0, "") and command.posts == []


def test_a_missing_book_whose_link_names_a_program_now_is_left_out_without_fetching_it(command, monkeypatch, capsys):
    """The same, its link naming a program now: left out and named, never fetched, and kept as left out, so the next run
    names it without asking for its link (StoryBundle's own flow asked every run)."""
    assert command.run() == (0, "")
    (command.root / LANTERN / FILES[2][0]).unlink()
    monkeypatch.setitem(FILES, 2, ("Day of the Lantern Setup.exe", b"MZ" + bytes(760000)))
    capsys.readouterr()
    command.posts.clear()
    named = "  'Day of the Lantern': 'Day of the Lantern Setup.exe' (EXE, a program, script or shortcut)"
    assert command.run() == (0, "") and command.posts == ["2"] and named in capsys.readouterr().out
    assert sorted(p.name for p in (command.root / LANTERN).iterdir()) == []
    command.posts.clear()
    assert command.run() == (0, "") and command.posts == [] and named in capsys.readouterr().out


def test_a_book_with_a_whole_copy_among_its_records_is_done(command):
    """A book with two records (StoryBundle's own flow fetched one missing or cut short again, into "Title (2)"): done
    while any of its copies is whole, as before, though the newer one is gone, so nothing comes again."""
    from datetime import datetime, timedelta

    assert command.run() == (0, "")
    manifest = dl.Manifest(command.root, "storybundle")
    first = manifest.get(f"{LANTERN}/{FILES[2][0]}")
    manifest.add({**first, "path": f"{LANTERN} (2)/{FILES[2][0]}",
                  "downloaded_at": (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")})  # gone since
    command.posts.clear()
    assert command.run() == (0, "") and command.posts == [] and not (command.root / f"{LANTERN} (2)").exists()


def test_no_bonus_and_bundle_take_a_book_in_the_first_bundle_they_leave_and_it_stays_there(command, capsys):
    """--no-bonus leaves out the newsletter bonus bundles' books, and --bundle takes only the bundles whose name has the
    text, as before: a book also in a bundle they leave in comes there, the first of them by purchase date; a later run
    with others takes the rest, and leaves that book where it is."""
    assert command.run("--no-bonus") == (0, "") and command.done() == ["1", "2"]
    assert "Bonus books skipped: 2." in capsys.readouterr().out
    assert (command.root / "storybundle" / "The Example Game Book Bundle" / "Day of the Lantern" / FILES[2][0]).is_file()
    command.posts.clear()
    assert command.run("--bundle", "newsletter") == (0, "") and command.done() == ["1", "2", "3"] and command.posts == ["3"]
    out = capsys.readouterr().out
    assert "To download: 1 title, 1 file" in out and "Already done: 1 file." in out  # Day of the Lantern, where it is
    assert not (command.root / "storybundle" / "The Example Newsletter Bonus" / "Day of the Lantern").exists()


def test_the_oldest_bundle_s_books_come_first_and_a_book_in_two_comes_in_the_oldest(command):
    """StoryBundle's own order (Title.place), not the core's by folder and name: so --limit takes the oldest bundle's
    books first, and a book in two bundles goes in the older one's folder, however their names sort."""
    assert command.run("--limit", "1") == (0, "") and command.done() == ["2"]
    assert (command.root / "storybundle" / "The Example Newsletter Bonus" / "Day of the Lantern" / FILES[2][0]).is_file()


def test_a_run_says_how_many_books_are_in_more_than_one_bundle(command, capsys):
    assert command.run("--dry-run") == (0, "")
    out = capsys.readouterr().out
    assert ("4 titles, 4 files. To download: 3 titles, 3 files, about 419.4 KB. Already done: 0 files. Listed more than once: 1, "
            "each taken once.\n  storybundle/The Example Newsletter Bonus/Day of the Lantern\n") in out and not command.posts


def test_storybundle_turning_every_link_request_away_stops_the_run_as_the_downloads_do(command, monkeypatch):
    """In the run's words, every answer it counts: StoryBundle's own refusals of a link request (401, 403, 422) and the
    downloads' (download.REFUSED)."""
    monkeypatch.setattr(dl, "REFUSED_IN_A_ROW", 2)  # three books here
    command.refuse = {"1": 403, "2": 403, "3": 403}
    code, said = command.run("--jobs", "1")
    assert (code, said) == (exits.PARTLY, dl.turned_away("401, 403, 422 or 429")) and "2 files in a row away" in said
    assert command.posts == ["2", "3"]  # not one for every book
    assert said in command.log() and len(command.failures()) == 2 and command.done() == []


def test_a_run_stopped_by_books_turned_away_counts_those_never_asked_about_as_not_finished(command, monkeypatch,
                                                                                           capsys):
    """StoryBundle turning a book's link request away stops the run there: the book before it came (its link named and
    fetched it), the one after it was never asked about and is not finished, in the run's last line and its --json,
    beside the one turned away, which failed."""
    monkeypatch.setattr(dl, "REFUSED_IN_A_ROW", 1)  # books 2, 3 and 1, in that order: 2 comes, 3 is turned away
    command.refuse = {"3": 403}
    code, said = command.run("--json", "--jobs", "1")
    found = json.loads(capsys.readouterr().out)
    assert code == exits.PARTLY and said == dl.turned_away(sbm.Library.refused) and command.posts == ["2", "3"]
    assert (found["done"], found["failed"], found["not_finished"]) == (1, 1, 1) and command.done() == ["2"]
    assert "Run ended: 1 file done (760.0 KB), 1 failed, 1 not finished." in command.log()


@pytest.mark.parametrize("status, stops", [(422, True), (401, True), (403, True), (404, False), (410, False)])
def test_fresh_links_turned_away_mid_run_stop_it_after_a_few_books(command, site, monkeypatch, status, stops):
    """Each book's first link is dead by the time it's used (S3 turns it away), and StoryBundle turns the fresh one
    away: a session run out mid-run stops the run after a few books, in the words of every answer it counts."""
    monkeypatch.setattr(dl, "REFUSED_IN_A_ROW", 2)  # three books here
    site[0].expired_once.update({1, 2, 3})
    command.again = status  # each book's first link is answered; the fresh one isn't
    code, said = command.run("--jobs", "1")
    assert code == exits.PARTLY and command.done() == [] and len(command.failures()) == (2 if stops else 3)
    if stops:  # two books turned away in a row stop the run, and the third isn't asked about
        assert said == dl.turned_away(sbm.Library.refused) and said in command.log()
        assert command.posts == ["2", "2", "3", "3"]
    else:  # a file StoryBundle no longer has is that book's own trouble: the others are still asked about
        assert said.startswith("3 files failed") and command.posts == ["2", "2", "3", "3", "1", "1"]


def test_a_good_link_or_another_answer_breaks_a_row_of_books_turned_away(command, monkeypatch):
    monkeypatch.setattr(dl, "REFUSED_IN_A_ROW", 2)
    command.refuse = {"2": 422, "1": 401}  # books 2, 3 and 1, in that order: 3 has its link
    failed = "2 files failed: see state/storybundle/failures.csv. Running it again tries them again."
    assert command.run("--jobs", "1") == (exits.PARTLY, failed)
    assert command.done() == ["3"]
    command.refuse = {"2": 403, "1": 404}  # a missing file is that book's own trouble, not a refusal
    command.posts.clear()
    code, said = command.run("--jobs", "1")
    assert code == exits.PARTLY and "turned" not in said and command.posts == ["2", "1"]


def test_snapshot_has_no_secret_links_and_gives_back_the_titles_it_was_made_from(site, tmp_path, monkeypatch):
    """The core's snapshot (shopping.snapshot): a book once for each bundle it came in, without its link; read back
    (storybundle.from_snapshot), it gives the titles the listing gave, as does a snapshot of StoryBundle's own shape
    from before the core."""
    _, transport = site
    monkeypatch.setattr(sbm, "StoryBundle", functools.partial(sbm.StoryBundle, transport=transport, delay=0))
    with sbm.Library("c" * 80) as lib:
        titles = lib.titles()
        text = shopping.snapshot("storybundle", titles, tmp_path).read_text(encoding="utf-8")
    assert "KEYA" not in text and "/download/" not in text and "Opening the Box" in text
    shape = lambda ts: [(t.key, t.name, t.under, t.purchased, t.place, t.record, [(f.key, f.name, f.size, f.rough) for f in t.files])  # noqa: E731
                        for t in ts]
    assert shape(sbm.from_snapshot(json.loads(text))) == shape(titles) and len(titles) == 4
    before = [{"name": "The Example Newsletter Bonus", "purchased": "2010-03-01", "bonus": True,
               "books": [{"id": "2", "title": "Day of the Lantern", "approx_size": 0, "cover": "c.jpg"},
                         {"id": "3", "title": "Hexes and Charms", "approx_size": 0, "cover": "c.jpg"}]},
              {"name": "The Example Game Book Bundle", "purchased": "2020-01-01", "bonus": False,
               "books": [{"id": "1", "title": "Opening the Box", "approx_size": int(0.4 * 1024**2), "cover": "c.jpg"},
                         {"id": "2", "title": "Day of the Lantern", "approx_size": int(0.73 * 1024**2), "cover": "c.jpg"}]}]
    assert shape(sbm.from_snapshot(before)) == shape(titles)  # as its own flow wrote it: the same titles
