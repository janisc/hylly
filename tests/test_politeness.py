"""Politeness to the services Hylly asks: keys stay out of messages, nothing asks harder than it should, nothing floods."""

from __future__ import annotations

import argparse
import dataclasses
import email.utils
import functools
import inspect
import math
import subprocess
import sys
import threading
import time
import types

import httpx
import pytest

import hylly
from hylly import access, bell, cli, commands, humble, itchio, itchio_survey, logins, lookups, settings, shopsession, storage
from hylly import download as dl
from hylly import drivethrurpg as dtm
from hylly import storybundle as sbm
from hylly.logins import SITES, AuthError, ShopError
from hylly.lookups import metron, openlibrary

from helpers import Clock, FakeCDN, FakeItch, ItchSite as FakeSite, blob, itch, make_job, session_for, survey, watch
from helpers import web


KEY = "SECRETGAMEKEY1"


def humble_client(handler, *purchases: str) -> humble.HumbleClient:
    """Humble's client against the handler, with the list's items of these purchases kept, as order_keys keeps them:
    an item with files each, so that orders asks for each purchase's page."""
    client = humble.HumbleClient("session", transport=httpx.MockTransport(handler))
    client.listed = {k: [{"id": f"{k}-item", "contentId": "item", "productEntitlementId": k, "type": "download_drm_free"}]
                     for k in purchases}
    return client


def purchase_page() -> httpx.Response:
    """A purchase's page with nothing in it, as Next.js sends it."""
    return httpx.Response(200, headers={"content-type": "text/x-component"},
                          content=b'0:{"b":"x"}\n1:["$","$L2",null,{"userLibraryItem":{"entitlements":{"downloads":[]}}}]\n')


# ---- keys stay out of messages -------------------------------------------------------------------

def test_humble_s_errors_never_show_an_order_key(monkeypatch):
    """A purchase's page in trouble (busy, or refusing to answer) stops the listing with words that say what was asked
    for, never its key; a 400 is about that purchase alone, which is left out (Humble gave no data for it)."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for status in (503, 429):
        with pytest.raises(ShopError) as e:
            humble_client(lambda request: httpx.Response(status), KEY).orders([KEY])
        assert KEY not in str(e.value) and str(status) in str(e.value) and "a purchase" in str(e.value)
    assert humble_client(lambda request: httpx.Response(400), KEY).orders([KEY]) == ({}, [KEY])


def test_an_order_humble_won_t_give_is_still_only_left_out(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert humble_client(lambda request: httpx.Response(404), KEY).orders([KEY]) == ({}, [KEY])


def test_no_answer_after_every_try_is_a_shop_error_without_the_address():
    """The shops' session: the network failing every time is a ShopError with no_answer (passing: worth asking again
    later), naming the error by its kind alone, as its message may have the address. A ShopError raised without a
    status isn't passing trouble: it's about the thing asked for."""
    asked = []

    def down(request):
        asked.append(request)
        raise httpx.ConnectError(f"no route to {request.url}", request=request)

    with shopsession.ShopSession("Lantern Shop", "https://shop.example.com/", pace=0, backoff=0,
                                 transport=httpx.MockTransport(down)) as s, pytest.raises(ShopError) as e:
        s.request("GET", f"orders/{KEY}")
    assert len(asked) == shopsession.TRIES and e.value.status == 0 and e.value.no_answer and e.value.passing
    assert KEY not in str(e.value) and "No answer from Lantern Shop after 5 tries (ConnectError)." == str(e.value)
    assert not ShopError("DriveThruRPG sent something other than JSON for a download link.").passing
    assert ShopError("x", 503).passing and ShopError("x", 429).passing and not ShopError("x", 404).passing


def test_no_answer_says_what_to_do_and_a_login_s_check_asks_once(monkeypatch, capsys):
    """No answer from a shop ends a command as the network's trouble did: what was done is kept; try again later. A
    login's check asks once, so hylly login says at once that it couldn't check, and in words that read right."""
    def outage(args):
        raise ShopError("No answer from Humble Bundle after 5 tries (ConnectError).", no_answer=True)

    monkeypatch.setattr(cli, "cmd_bell", outage)
    with pytest.raises(SystemExit) as e:
        cli.main(["bell"])
    assert e.value.code == 1 and str(e.value) == ("Error: No answer from Humble Bundle after 5 tries (ConnectError). "
                                                  "What was done is kept; try again later.")
    asked = []

    def down(request):
        asked.append(request.url.path)
        raise httpx.ConnectError("no network", request=request)

    transport = httpx.MockTransport(down)
    monkeypatch.setattr(humble, "HumbleClient", functools.partial(humble.HumbleClient, transport=transport))
    monkeypatch.setattr(cli, "read_clipboard", lambda: "c" * 80)
    with pytest.raises(SystemExit) as e:
        cli.main(["login", "humble", "--clipboard"])
    assert e.value.code == 1 and str(e.value).startswith("No answer from Humble Bundle (ConnectError). So it couldn't be checked.")
    monkeypatch.setattr(sbm, "StoryBundle", functools.partial(sbm.StoryBundle, transport=transport))
    monkeypatch.setattr(dtm, "DriveThruRPG", functools.partial(dtm.DriveThruRPG, transport=transport))
    for check in (lambda: sbm.library_page("c" * 80), lambda: dtm.check_key("k" * 32),
                  lambda: itchio.check_key("k" * 40, transport=transport)):
        with pytest.raises(ShopError) as e:
            check()
        assert e.value.no_answer
    assert len(asked) == 4  # each asked once


def test_humble_takes_any_redirect_for_a_login_it_doesn_t_take_and_asks_a_400_once():
    """Humble's own way, kept: a login it doesn't take is sent on to its sign-in page, so any redirect means the cookie
    has expired, as 401 and 403 do, and isn't asked again. A 400 is an answer, asked once: only DriveThruRPG's API
    gives its answer next time."""
    asked = []
    for answer in (httpx.Response(302, headers={"location": "https://auth.humblebundle.com/login"}), httpx.Response(401),
                   httpx.Response(403), httpx.Response(200, json={"status": "anonymous"})):
        asked.clear()
        with pytest.raises(AuthError, match="expired"):
            humble_client(lambda request: asked.append(request) or answer).order_keys()
        assert len(asked) == 1
    asked.clear()
    with pytest.raises(ShopError) as e:
        humble_client(lambda request: asked.append(request) or httpx.Response(400)).order_keys()
    assert e.value.status == 400 and len(asked) == 1


def test_storybundle_s_errors_never_show_a_bundle_s_page():
    with sbm.StoryBundle("c" * 80, transport=httpx.MockTransport(lambda request: httpx.Response(429)), delay=0, backoff=0) as sb:
        with pytest.raises(ShopError) as e:
            sb._get("/downloads/SECRETBUNDLEKEY", "a bundle's download page")
    assert "SECRETBUNDLEKEY" not in str(e.value) and "429" in str(e.value) and "a bundle's download page" in str(e.value)


def test_a_command_shows_no_address_when_a_shop_or_the_network_fails(monkeypatch, capsys):
    request = httpx.Request("GET", f"https://shop.test/api?gamekeys={KEY}")

    def status_error(args):
        raise httpx.HTTPStatusError(f"Server error for url {request.url}", request=request, response=httpx.Response(503, request=request))

    monkeypatch.setattr(cli, "cmd_bell", status_error)
    with pytest.raises(SystemExit) as e:
        cli.main(["bell"])
    assert KEY not in str(e.value) and "HTTPStatusError" in str(e.value)

    def shop_error(args):
        raise ShopError("Humble Bundle answered HTTP 503 when asked about your orders.")

    monkeypatch.setattr(cli, "cmd_bell", shop_error)
    with pytest.raises(SystemExit) as e:
        cli.main(["bell"])
    assert e.value.code == 1 and "HTTP 503" in capsys.readouterr().err


def test_a_crash_s_report_keeps_only_the_site_s_name():
    script = ("from hylly import cli; cli.hide_addresses_in_tracebacks(); "
              f"raise RuntimeError('fetching https://shop.test/orders/{KEY}/x?gamekeys={KEY} failed')")
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert done.returncode == 1 and "https://shop.test/..." in done.stderr and "failed" in done.stderr
    assert KEY not in done.stderr.replace(script, "")  # the -c line itself isn't a traceback's source


def test_every_command_keeps_addresses_out_of_a_crash_s_report_also_from_a_thread():
    script = f"""
import threading
from hylly import cli
K = "{KEY}"
def crash(args=None):
    raise RuntimeError(f"at https://shop.test/orders/{{K}}")
def command(args):
    worker = threading.Thread(target=crash)
    worker.start()
    worker.join()
    crash()
cli.cmd_bell = command
cli.main(["bell"])
"""
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    # at least: newer Pythons also quote the line, and that goes through the same filter. A bug's own code, 70
    assert done.returncode == 70 and done.stderr.count("at https://shop.test/...") >= 2 and KEY not in done.stderr


def test_hylly_download_goes_on_after_trouble_with_one_shop(tmp_path, monkeypatch, capsys):
    for shop in ("humble", "storybundle", "drivethrurpg", "itchio"):
        monkeypatch.setenv(SITES[shop][2], "a login")
    reached = []

    def trouble(args):
        with dtm.DriveThruRPG("key", transport=httpx.MockTransport(lambda request: httpx.Response(503)), delay=0, wait=0, backoff=0) as client:
            client.raw_products()

    def fine(args):
        reached.append(args.command)

    monkeypatch.setattr(cli, "cmd_humble", fine)
    monkeypatch.setattr(cli, "cmd_storybundle", fine)
    monkeypatch.setattr(cli, "cmd_drivethrurpg", trouble)
    monkeypatch.setattr(cli, "cmd_itchio", fine)
    with pytest.raises(SystemExit) as e:
        cli.main(["download", "--library", str(tmp_path)])
    assert e.value.code == 1 and reached == ["humble", "storybundle", "itchio"]
    assert "HTTP 503" in capsys.readouterr().err


def run_with_links_from(tmp_path, refresh, jobs=20) -> tuple[dl.Downloader, dict]:
    cdn = FakeCDN({})
    manifest = dl.Manifest(tmp_path, "humble")
    d = dl.Downloader(tmp_path, manifest, refresh=refresh, transport=httpx.MockTransport(cdn.handler), log=lambda s: None, backoff=0.0, workers=1)
    return d, d.run([dataclasses.replace(make_job(f"{n}.epub", blob(100)), url="") for n in range(jobs)])


def test_a_shop_turning_away_every_link_request_stops_the_run(tmp_path):
    asked = []

    def refresh(job):
        asked.append(job)
        raise ShopError("Humble Bundle answered HTTP 429 when asked about your orders.", 429)

    d, _ = run_with_links_from(tmp_path, refresh)
    assert d.blocked and len(asked) == dl.REFUSED_IN_A_ROW


def test_a_shop_having_a_bad_moment_doesn_t_stop_the_run(tmp_path):
    """A 5xx for a file's link, after its session asked again, between other ends of files (here, a file the shop no
    longer lists): that file's failure alone, as the shop is no longer down by the time the next one ends otherwise."""
    asked = []

    def refresh(job):
        asked.append(job)
        if len(asked) % 3 == 0:
            return ""  # no link for this one any more: another end, which starts the count again
        raise ShopError("Humble Bundle answered HTTP 503 when asked about your orders.", 503)

    d, results = run_with_links_from(tmp_path, refresh)
    assert not d.blocked and len(results["failed"]) == 20 and "HTTP 503" in results["failed"][0][1]


def test_a_shop_answering_only_its_own_trouble_for_link_after_link_stops_the_run_as_no_answer_does(tmp_path):
    """A shop that still answers 5xx after every try, UNANSWERED_IN_A_ROW files in a row, is down as much as one that
    doesn't answer: the run stops, rather than fail the rest one after another, each with its tries and its waits (a
    StoryBundle book's first link once stopped such a run at once)."""
    asked = []

    def refresh(job):
        asked.append(job)
        raise ShopError("Lantern Shop answered HTTP 503.", 503)

    d, results = run_with_links_from(tmp_path, refresh)
    assert d.blocked == dl.unanswered() and len(asked) == dl.UNANSWERED_IN_A_ROW and "HTTP 5xx" in d.blocked
    assert len(results["failed"]) == dl.UNANSWERED_IN_A_ROW and len(results["stopped"]) == 20 - dl.UNANSWERED_IN_A_ROW


def test_a_shop_down_stops_the_run_after_a_few_files_counting_no_answer_and_its_own_trouble(tmp_path):
    """No answer at all when asked for a file's link, or only the shop's own trouble (5xx), after the shop's session
    asked again, UNANSWERED_IN_A_ROW files in a row: the shop is down, so the run stops. Any other end of a file (a
    file it no longer lists, one turned away) starts the count again."""
    ends = ["none", "busy", "gone", "none", "busy", "gone", "busy", "none", "none", "none", "none", "none"]
    asked = []

    def refresh(job):
        asked.append(job)
        end = ends[len(asked) - 1]
        if end == "none":
            raise ShopError("No answer from Lantern Shop after 5 tries (ConnectError).", no_answer=True)
        if end == "busy":
            raise ShopError("Lantern Shop answered HTTP 503.", 503)
        return ""  # gone: no link for it any more

    d, results = run_with_links_from(tmp_path, refresh, jobs=len(ends))
    assert d.blocked == dl.unanswered() and len(asked) == 6 + dl.UNANSWERED_IN_A_ROW  # the third in a row after "gone"
    assert len(results["failed"]) == len(asked)


def test_itch_io_turning_away_a_link_request_counts_as_refused():
    api = itchio.Itch("key", transport=httpx.MockTransport(lambda request: httpx.Response(429)), delay=0, sleep=lambda s: None)
    with api, pytest.raises(dl.Refused):
        api.link(types.SimpleNamespace(key_id=1), types.SimpleNamespace(id=2, filename="a.pdf"))


ITEM, UPLOAD, LINK = types.SimpleNamespace(key_id=1), types.SimpleNamespace(id=2, filename="a.pdf"), "https://files.example.com/2?sig=s"


def test_itch_io_s_link_waits_out_its_retry_after_as_every_other_call_does():
    """A link's tries once ignored itch.io's Retry-After. Now it's waited out, up to logins.LONGEST_WAIT, and a longer
    one ends the request at once, turned away (download.Refused), as for every other request to a shop."""
    slept, answers, asked = [], [], []

    def handler(request):
        asked.append(request)
        return answers.pop(0)

    with itchio.Itch("key", transport=httpx.MockTransport(handler), delay=0, backoff=0, sleep=slept.append) as api:
        answers += [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(302, headers={"location": LINK})]
        assert api.link(ITEM, UPLOAD) == LINK and len(slept) == 1 and 6.9 < slept[0] <= 7  # from the answer's time
        asked.clear()
        answers.append(httpx.Response(429, headers={"Retry-After": "3600"}))
        with pytest.raises(dl.Refused):
            api.link(ITEM, UPLOAD)
    assert len(asked) == 1


def test_itch_io_s_links_are_asked_one_at_a_time_however_many_downloads_ask():
    """itch.io's own way, kept: a file's link is asked for only once the one before it has its answer, its tries too,
    however many downloads ask at once."""
    in_flight, most, count = [0], [0], threading.Lock()

    def handler(request):
        with count:
            in_flight[0] += 1
            most[0] = max(most[0], in_flight[0])
        time.sleep(0.1)
        with count:
            in_flight[0] -= 1
        return httpx.Response(302, headers={"location": LINK})

    with itchio.Itch("key", transport=httpx.MockTransport(handler), delay=0) as api:
        workers = [threading.Thread(target=api.link, args=(ITEM, UPLOAD)) for _ in range(3)]
        [w.start() for w in workers]
        [w.join() for w in workers]
    assert most[0] == 1


def test_itch_io_s_claim_is_sent_once_whatever_comes_back():
    """A claim changes the account, so it's never sent again: not after a 503, a 429, nor no answer at all."""
    for trouble in (httpx.Response(503), httpx.Response(429, headers={"Retry-After": "1"}), "no answer"):
        posted = []

        def handler(request, trouble=trouble):
            posted.append(request)
            if trouble == "no answer":
                raise httpx.ConnectError("no answer", request=request)
            return trouble

        with itchio_survey.Web("cookie", transport=httpx.MockTransport(handler), delay=0, sleep=lambda s: None) as site:
            ok, why = site.claim("bundle/download/AAAkey111", None, "tok", 3)
        assert not ok and why and len(posted) == 1, trouble


class Untold(httpx.SyncByteStream):
    """An answer's body that tells whether anyone read it."""

    def __init__(self) -> None:
        self.read = False

    def __iter__(self):
        self.read = True
        yield b"%PDF-1.4 the file itself"


def test_itch_io_s_link_is_read_from_the_redirect_and_the_file_never_comes():
    """itch.io's own way, kept: a file's link is where its answer redirects to, and the answer's body is never read,
    nor is a file sent at once instead of a redirect."""
    bodies = []

    def handler(request):
        bodies.append(Untold())
        if len(bodies) == 1:
            return httpx.Response(302, headers={"location": LINK}, stream=bodies[-1])
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=bodies[-1])

    with itchio.Itch("key", transport=httpx.MockTransport(handler), delay=0, sleep=lambda s: None) as api:
        assert api.link(ITEM, UPLOAD) == LINK
        with pytest.raises(dl.DownloadError, match="HTTP 200"):
            api.link(ITEM, UPLOAD)
    assert len(bodies) == 2 and not any(body.read for body in bodies)


def test_a_shop_s_retry_after_is_waited_out_or_ends_the_request(monkeypatch):
    slept, answers, asked = [], [], []
    monkeypatch.setattr(time, "sleep", slept.append)

    def handler(request):
        asked.append(request)
        return answers.pop(0)

    client = humble_client(handler)
    answers += [httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200, json={"status": "authenticated"})]
    assert client.check() is None and any(29.9 < s <= 30 for s in slept)  # the wait, from the answer's time
    asked.clear()
    answers.append(httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(ShopError) as e:
        client.check()
    assert e.value.status == 429 and len(asked) == 1

    asked.clear()
    answers.append(httpx.Response(429, headers={"Retry-After": "3600"}))
    with dtm.DriveThruRPG("key", transport=httpx.MockTransport(handler), delay=0, wait=0, backoff=0) as client, pytest.raises(ShopError):
        client.raw_products()
    assert len(asked) == 1

    for value in ("99999", "Wed, 21 Oct 2099 07:28:00 GMT"):  # a date, too, which used to crash it
        asked.clear()
        answers.append(httpx.Response(429, headers={"Retry-After": value}))
        site = itchio_survey.Web("cookie", transport=httpx.MockTransport(handler), delay=0, sleep=lambda s: None)
        with site, pytest.raises(ShopError):
            site.page("my-purchases/bundles")
        assert len(asked) == 1


def test_a_429_from_humble_for_one_order_isn_t_taken_for_a_missing_order(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(ShopError):
        humble_client(lambda request: httpx.Response(429), KEY).orders([KEY])


def test_no_answer_for_one_order_isn_t_taken_for_a_missing_order_either(monkeypatch):
    """No answer, after every try, says nothing about the order: it stops the listing, as the network's error did
    before the shops shared one session, and isn't kept as a purchase Humble gave no data for."""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def handler(request):
        raise httpx.ConnectError("no answer", request=request)

    with pytest.raises(ShopError) as e:
        humble_client(handler, KEY).orders([KEY])
    assert e.value.no_answer and KEY not in str(e.value)


def test_humble_in_trouble_for_one_order_isn_t_taken_for_a_missing_order_either(monkeypatch):
    """A 5xx after every try says Humble is in trouble, not that it has no data for the order: the listing stops, as
    for a 429 or no answer (ShopError.passing), rather than keep the purchase as one Humble gave no data for."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    asked = []

    def handler(request):
        asked.append(request)
        return httpx.Response(503)

    with pytest.raises(ShopError) as e:
        humble_client(handler, KEY).orders([KEY])
    assert e.value.status == 503 and len(asked) == shopsession.TRIES


# ---- the pace --------------------------------------------------------------------------------------

def test_a_source_that_keeps_asking_to_slow_down_ends_its_turn(tmp_path):
    clock, asked = Clock(), []

    def handler(request):
        asked.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    s = session_for(openlibrary.OpenLibrary(), handler, clock, tmp_path)
    with pytest.raises(lookups.Quota, match="keeps asking to slow down"):
        s.get("https://openlibrary.org/search.json")
    assert len(asked) == lookups.SLOWED_IN_A_ROW


def test_a_slow_down_now_and_then_is_only_waited_for(tmp_path):
    clock, answers = Clock(), []

    def handler(request):
        return answers.pop(0)

    s = session_for(openlibrary.OpenLibrary(), handler, clock, tmp_path)
    for _ in range(4):  # never so many in a row: what comes between shows it's getting through
        answers += [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200)]
        assert s.get("https://openlibrary.org/search.json").status_code == 200


def test_the_budget_survives_a_kill_and_counts_what_failed(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.txt"
    first = lookups.Budget(path, 0, 0, clock.now)
    first.note(), first.note()  # and no chance to save anything: a killed run
    assert len(lookups.Budget(path, 0, 0, clock.now).times) == 2

    def down(request):
        raise httpx.ConnectError("no")

    s = session_for(openlibrary.OpenLibrary(), down, clock, tmp_path)
    with pytest.raises(lookups.Transient):
        s.get("https://openlibrary.org/search.json")
    assert len(s.budget.times) == lookups.RETRIES == s.requests


def test_two_runs_add_to_one_budget_and_neither_writes_over_the_other(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.txt"
    one, other = lookups.Budget(path, 0, 0, clock.now), lookups.Budget(path, 0, 0, clock.now)
    for _ in range(5):
        one.note(), other.note()
    assert len(lookups.Budget(path, 0, 0, clock.now).times) == 10


def test_a_budget_file_cut_or_old_costs_nothing(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.txt"
    path.write_text(f"{clock.t - 100:.1f}\nnot a time\n{clock.t - 90:.1f}\n{clock.t - 80:.1f}\n\xe4", encoding="latin-1")
    assert len(lookups.Budget(path, 0, 0, clock.now).times) == 3
    path.write_text("".join(f"{clock.t - 200000 - i}.0\n" for i in range(6000)) + f"{clock.t - 5:.1f}\n", encoding="utf-8")
    assert len(lookups.Budget(path, 0, 0, clock.now).times) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1  # days old: dropped, so the file stays small


def test_a_missing_or_unwritable_budget_file_is_no_reason_to_stop(tmp_path, monkeypatch):
    clock = Clock()
    budget = lookups.Budget(tmp_path / "a folder, not a file", 0, 0, clock.now)
    (tmp_path / "a folder, not a file").mkdir()
    slept = []
    monkeypatch.setattr(storage.time, "sleep", slept.append)
    budget.note()
    assert len(budget.times) == 1 and not slept  # nor a reason to wait: a request is held up by this


def test_open_library_has_a_day_s_limit():
    assert openlibrary.OpenLibrary.per_day == 10000


def own(slept: list):
    """A time.sleep that counts only this test's own thread: one left running by another test sleeps for real."""
    me, real = threading.get_ident(), time.sleep
    return lambda s: slept.append(s) if threading.get_ident() == me else real(s)


def test_humble_is_asked_at_most_once_a_second_also_for_each_purchase(monkeypatch):
    slept, asked = [], []
    monkeypatch.setattr(time, "sleep", own(slept))

    def handler(request):
        asked.append(request)
        return purchase_page()

    humble_client(handler, "one", "two", "three").orders(["one", "two", "three"])
    assert len(asked) == 3 and len(slept) == 2 and all(0.9 < s <= humble.PAUSE for s in slept)


def test_storybundle_is_asked_at_most_once_a_second(monkeypatch):
    """A second between one request and the next, as for every shop (it used to sleep a second after each page: the
    same pace, now the session's)."""
    slept = []
    monkeypatch.setattr(time, "sleep", own(slept))
    with sbm.StoryBundle("c" * 80, transport=httpx.MockTransport(lambda request: httpx.Response(200, text="page"))) as sb:
        sb._get("/x", "a page")
        sb._get("/y", "a page")
    assert len(slept) == 1 and 0.9 < slept[0] <= 1.0


def test_storybundle_s_messages_don_t_show_your_account_s_number():
    with sbm.StoryBundle("c" * 80, transport=httpx.MockTransport(lambda request: httpx.Response(503)), delay=0, backoff=0) as sb:
        with pytest.raises(ShopError) as e:
            sb.library_page()
    assert "12345" not in str(e.value) and "your bundles page" in str(e.value)  # 12345: the made-up account in conftest


def test_drivethrurpg_keeps_its_pace_whatever_number_of_downloads_ask():
    at = []

    def handler(request):
        at.append(time.monotonic())
        return httpx.Response(200, json=[])

    with dtm.DriveThruRPG("key", transport=httpx.MockTransport(handler), delay=0.1, wait=0, backoff=0) as client:
        workers = [threading.Thread(target=client._call, args=("GET", "order_products", {})) for _ in range(4)]
        [w.start() for w in workers]
        [w.join() for w in workers]
    gaps = [b - a for a, b in zip(sorted(at), sorted(at)[1:])]
    assert len(at) == 4 and min(gaps) > 0.05


def test_itch_io_keeps_its_pace_whatever_number_of_threads_ask():
    """itch.io's calls were once paced without a lock, so threads asking at once went out together. The shops'
    session paces under one: the API's calls and the bundles' pages alike."""
    at = []

    def handler(request):
        at.append(time.monotonic())
        return httpx.Response(200, json={"user": {}})

    for client, ask in ((itchio.Itch("key", transport=httpx.MockTransport(handler), delay=0.1), lambda c: c.profile()),
                        (itchio_survey.Web("cookie", transport=httpx.MockTransport(handler), delay=0.1), lambda c: c.page("x"))):
        at.clear()
        with client:
            workers = [threading.Thread(target=ask, args=(client,)) for _ in range(4)]
            [w.start() for w in workers]
            [w.join() for w in workers]
        gaps = [b - a for a, b in zip(sorted(at), sorted(at)[1:])]
        assert len(at) == 4 and min(gaps) > 0.05, type(client).__name__


def test_the_pace_counts_from_the_answer_too(monkeypatch):
    """A slow answer doesn't bring the next request closer: the pace runs from when the last request went out or its
    answer came, whichever is later, as StoryBundle's and itch.io's did (Humble's and DriveThruRPG's ran from when it
    went out)."""
    monkeypatch.setattr(humble, "PAUSE", 0.1)
    times = []

    def slow(request):
        times.append(time.monotonic())
        time.sleep(0.15)
        times.append(time.monotonic())
        return httpx.Response(200, json={"status": "authenticated"})

    client = humble_client(slow)
    client.check()
    client.check()
    asked, answered, asked_again = times[0], times[1], times[2]
    assert asked_again - answered >= 0.08 and asked_again - asked >= 0.2


def test_a_shop_s_wait_holds_the_requests_already_waiting_their_turn():
    """A shop asking one download's request to wait asks it of every request to it: those already waiting their turn
    when its answer comes don't go out until the wait is over (as itch.io's links and StoryBundle's didn't)."""
    asking, queued, at, answered = threading.Event(), threading.Event(), {}, []

    def sleep(seconds):
        queued.set()  # a request is waiting its turn: only now does the shop answer the first
        time.sleep(seconds)

    def handler(request):
        at.setdefault(request.url.path, []).append(time.monotonic())
        if request.url.path == "/a" and len(at["/a"]) == 1:
            asking.set()
            assert queued.wait(10)
            answered.append(time.monotonic())
            return httpx.Response(429, headers={"Retry-After": "0.5"})
        return httpx.Response(200)

    with shopsession.ShopSession("Lantern Shop", "https://shop.example.com/", pace=0.2, backoff=0, sleep=sleep,
                                 transport=httpx.MockTransport(handler)) as s:
        workers = [threading.Thread(target=s.request, args=("GET", "/a"))]
        workers[0].start()
        assert asking.wait(10)
        workers += [threading.Thread(target=s.request, args=("GET", path)) for path in ("/b", "/c")]
        [w.start() for w in workers[1:]]
        [w.join() for w in workers]
    later = sorted([*at["/a"][1:], *at["/b"], *at["/c"]])
    assert len(later) == 3 and later[0] - answered[0] >= 0.45  # none before the wait was over
    assert min(b - a for a, b in zip(later, later[1:])) >= 0.15  # and then at the pace, not all at once


def test_waits_told_at_once_run_side_by_side_rather_than_one_after_another():
    """Two requests told to wait at the same moment, their answers on their way together, wait it out together: the
    waits overlap rather than add up."""
    together, at = threading.Barrier(2, timeout=10), []

    def handler(request):
        at.append(time.monotonic())
        if len(at) <= 2:
            together.wait()  # both first requests are out at once
            return httpx.Response(429, headers={"Retry-After": "0.4"})
        return httpx.Response(200)

    with shopsession.ShopSession("Lantern Shop", "https://shop.example.com/", pace=0, backoff=0,
                                 transport=httpx.MockTransport(handler)) as s:
        workers = [threading.Thread(target=s.request, args=("GET", path)) for path in ("/a", "/b")]
        started = time.monotonic()
        [w.start() for w in workers]
        [w.join() for w in workers]
    assert len(at) == 4 and at[-1] - started < 0.7  # one wait of 0.4 s for both, not 0.8


def test_a_shop_busy_for_every_download_is_given_up_on_after_about_as_many_waits_as_one_would_sit_out():
    """Six downloads asking a shop whose every answer is a quick 429: the waits go one after another, as each request
    goes out only once the wait before is over, so each wait a request sits out is one of its tries. The run then gives
    up after about the waits one request would sit out (4 of 0.3 s, and the last tries at the pace), as before the shops
    shared one session, when each download waited on its own side by side: not after six times as many."""
    sent, lock = [], threading.Lock()

    def handler(request):
        with lock:
            sent.append(time.monotonic())
        return httpx.Response(429, headers={"Retry-After": "0.3"})

    got = []
    with shopsession.ShopSession("Lantern Shop", "https://shop.example.com/", pace=0.05, backoff=0.001,
                                 transport=httpx.MockTransport(handler)) as s:
        workers = [threading.Thread(target=lambda i=i: got.append(s.request("GET", f"/{i}").status_code)) for i in range(6)]
        started = time.monotonic()
        [w.start() for w in workers]
        [w.join(30) for w in workers]
        took = time.monotonic() - started
    assert got == [429] * 6 and len(sent) <= 6 * shopsession.TRIES
    assert took < 3.0  # about 1.5 s; each sitting out a whole wait of its own, one after another: 24 of them, 7.5 s
    assert min(b - a for a, b in zip(sorted(sent), sorted(sent)[1:])) >= 0.04  # and never faster than the pace


def test_retry_after_is_read_one_way_for_the_shops_the_lookups_and_the_downloads():
    """Two readings of Retry-After once gave different answers, and only the lookups' took "1.5". One reading now, in
    logins: seconds or a date, never below 0; None when there's none or it can't be read."""
    now = 1_800_000_000.0

    def told(value=None) -> float | None:
        return logins.retry_after(httpx.Response(429, headers={} if value is None else {"Retry-After": value}), now)

    assert told("120") == 120 and told(" 1.5 ") == 1.5 and told("0") == 0
    assert told(email.utils.formatdate(now + 90, usegmt=True)) == 90 and told(email.utils.formatdate(now - 90, usegmt=True)) == 0
    assert told(email.utils.formatdate(now + 60)) == 60  # "-0000": UTC, the sender's own zone unknown
    assert all(told(value) is None for value in (None, "", "soon", "-5", "²".encode()))  # "²" passes isdigit, not float()
    assert told("9" * 400) == math.inf  # longer than anything is waited out
    assert lookups.retry_after is logins.retry_after and dl.retry_after is logins.retry_after


def test_a_slow_down_doesn_t_shorten_the_longer_pause_the_site_asked_for(tmp_path):
    clock, answers = Clock(), [httpx.Response(429, headers={"Retry-After": "1", "X-RateLimit-Burst-Remaining": "0", "X-RateLimit-Burst-Reset": "40"}),
                               httpx.Response(200)]
    s = session_for(metron.Metron("token"), lambda request: answers.pop(0), clock, tmp_path)
    assert s.get("https://metron.cloud/api/publisher/").status_code == 200
    assert max(clock.slept) >= 40


def test_every_shop_asks_at_most_once_a_second_by_default():
    assert inspect.signature(sbm.StoryBundle.__init__).parameters["delay"].default == 1.0
    assert inspect.signature(dtm.DriveThruRPG.__init__).parameters["delay"].default == 1.0
    assert inspect.signature(itchio.Itch.__init__).parameters["delay"].default == 1.0
    assert inspect.signature(itchio_survey.Web.__init__).parameters["delay"].default == 2.0
    assert humble.PAUSE == 1.0


def test_parallel_downloads_stay_within_reason():
    assert commands.download_jobs("1") == 1 and commands.download_jobs("6") == 6
    for wrong in ("0", "7", "100", "abc", "-1", "2.5"):
        with pytest.raises(argparse.ArgumentTypeError, match="1 to 6"):
            commands.download_jobs(wrong)
    for command in (["humble", "download"], ["storybundle", "download"], ["drivethrurpg", "download"], ["itchio", "download"]):
        with pytest.raises(SystemExit) as e:
            cli.main([*command, "--jobs", "50"])
        assert e.value.code == 2


# ---- how it names itself -----------------------------------------------------------------------------

def test_hylly_names_itself_with_its_version_and_page_to_everyone(tmp_path, monkeypatch):
    assert hylly.USER_AGENT.startswith(f"hylly/{hylly.__version__} (") and "github.com/janisc/hylly" in hylly.USER_AGENT
    assert lookups.user_agent() == hylly.USER_AGENT  # no contact unless you set one
    manifest = dl.Manifest(tmp_path, "humble")
    for headers in (humble.HumbleClient("session").http.headers, sbm.StoryBundle("c" * 80).http.headers,
                    dtm.DriveThruRPG("key").http.headers, itchio.Itch("key").http.headers, itchio_survey.Web("cookie").http.headers,
                    dl.Downloader(tmp_path, manifest, refresh=lambda job: "", log=lambda s: None)._client().headers,
                    session_for(openlibrary.OpenLibrary(), lambda request: httpx.Response(200), Clock(), tmp_path).http.headers):
        assert headers["user-agent"] == hylly.USER_AGENT
    posted = []
    monkeypatch.setattr(httpx, "post", lambda url, **k: posted.append(k["headers"]) or httpx.Response(200))
    bell.Watch({**bell.DEFAULTS, "toast": False, "ntfy": "https://my.example/alarms"}, log=lambda m: None).push("t", "x", "high")
    assert posted[0]["User-Agent"] == hylly.USER_AGENT


def test_a_contact_goes_only_if_set_and_is_never_quietly_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "get", lambda section: {"contact": " reader@example.com "})
    agent = lookups.user_agent()
    assert agent.endswith("; reader@example.com)")
    assert session_for(openlibrary.OpenLibrary(), lambda request: httpx.Response(200), Clock(), tmp_path).http.headers["user-agent"] == agent
    for wrong in ("r\u00e4der@example.com", "reader@example.com\r\nX-Other: 1", "x" * 101):
        monkeypatch.setattr(settings, "get", lambda section, value=wrong: {"contact": value})
        with pytest.raises(SystemExit, match="plain"):
            lookups.user_agent()


# ---- itch.io claiming asks first ---------------------------------------------------------------------

def test_claiming_goes_ahead_only_on_a_yes(tmp_path):
    fake, site = FakeItch(), FakeSite()
    survey(tmp_path, fake, site)
    told = []

    def no(how_many):
        told.append(how_many)
        return False

    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, ask=no, log=lambda s: None, sleep=lambda s: None) == 0
        assert told == [3] and site.claimed == {3}  # told how many (not the item itch.io wouldn't describe), nothing claimed
        assert itchio_survey.claim(api, pages, tmp_path, ask=lambda n: True, log=lambda s: None, sleep=lambda s: None) == 3


def test_the_claim_command_asks_unless_told_not_to(tmp_path, monkeypatch):
    class Session:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    asked = []
    monkeypatch.setattr(itchio, "Itch", Session)
    monkeypatch.setattr(itchio_survey, "Web", Session)
    monkeypatch.setattr(itchio_survey, "claim", lambda *a, **k: asked.append(k["ask"]) or 0)
    monkeypatch.setattr(cli, "load_session", lambda *a: "a login")
    cli.main(["itchio", "claim", "--library", str(tmp_path)])
    cli.main(["itchio", "claim", "--yes", "--library", str(tmp_path)])
    assert asked == [cli._may_claim, None]


def test_with_no_one_to_ask_it_doesn_t_claim(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", type("Piped", (), {"isatty": lambda self: False})())
    with pytest.raises(SystemExit, match="--yes"):
        cli._may_claim(4)
    monkeypatch.setattr(cli.sys, "stdin", type("NulOnWindows", (), {"isatty": lambda self: True})())

    def closed(question):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    with pytest.raises(SystemExit, match="--yes"):
        cli._may_claim(4)


def test_at_the_keyboard_only_a_yes_will_do(monkeypatch, capsys):
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    for answer, went in (("y", True), ("YES", True), ("", False), ("n", False), ("maybe", False)):
        monkeypatch.setattr("builtins.input", lambda question: answer)
        assert cli._may_claim(4) is went
    assert "Nothing was claimed" in capsys.readouterr().out


# ---- alarms can't flood ----------------------------------------------------------------------------------

def test_alarms_together_have_a_limit_and_the_rest_are_mentioned_once():
    w, sent, clock = watch(daily_summary=-1)
    for n in range(25):  # one address after another, each its own alarm
        w.alarm("downloads", f"203.0.113.{n}", "Many downloads", "text")
    assert len(sent) == 10
    assert sum(1 for line in bell.log_file().read_text(encoding="utf-8").splitlines() if "Many downloads" in line) == 25
    w.tick()  # the once-a-minute look says so soon, not an hour later
    assert len(sent) == 11 and "15 more alarms" in sent[-1][1] and "bell.log" in sent[-1][1]
    w.tick()
    assert len(sent) == 11  # once
    for n in range(5):
        w.alarm("downloads", f"198.51.100.{n}", "Many downloads", "text")
    w.tick()
    assert len(sent) == 11  # no room for another just yet
    clock[0] += 3601
    w.tick()
    assert len(sent) == 12 and "5 more alarms" in sent[-1][1]


def test_a_real_problem_gets_through_a_flood():
    w, sent, clock = watch()
    for n in range(12):
        w.alarm("downloads", f"203.0.113.{n}", "Many downloads", "text")
    assert w.alarm("library", "", "The library can't be read", "text") and w.alarm("users", "x", "Users changed", "text")
    assert len(sent) == 12


def test_a_restart_doesn_t_start_the_count_again():
    w, sent, clock = watch()
    for n in range(10):
        w.alarm("downloads", str(n), "Many downloads", "text")
    again = bell.Watch(w.conf, send=lambda *a: sent.append(a), now=lambda: clock[0], log=lambda m: None)
    assert not again.alarm("downloads", "new", "Many downloads", "text") and len(sent) == 10


def test_a_day_has_its_limit_too():
    w, sent, clock = watch()
    for n in range(300):
        w.alarm("downloads", str(n), "Many downloads", "text")
        clock[0] += 360  # ten an hour: never over the hour's limit
    assert len(sent) == 100 + 60  # a hundred in the first ten hours, then none until the first of them are a day old


def test_held_alarms_are_told_of_also_when_the_day_s_limit_is_spent():
    w, sent, clock = watch(alarms_per_hour=1, daily_summary=-1)
    for n in range(10):
        w.alarm("downloads", str(n), "Many downloads", "text")
        clock[0] += 3601
    assert len(sent) == 10  # the day's ten
    w.alarm("wrong", "reader", "Wrong passwords", "text")
    w.tick()
    assert "1 more alarm held back" in sent[-1][1]


def test_times_from_a_clock_that_ran_ahead_don_t_hold_alarms_back():
    w, sent, clock = watch()
    w._state["sent"] = [clock[0] + 1e7] * 50
    assert w.alarm("library", "", "The library can't be read", "text")


def test_nothing_goes_out_with_a_limit_of_none():
    w, sent, clock = watch(alarms_per_hour=0)
    assert not w.alarm("library", "", "The library can't be read", "text")
    clock[0] += 7200
    w.tell_of_held()
    assert not sent and "The library can't be read" in bell.log_file().read_text(encoding="utf-8")


def test_a_state_that_can_t_be_written_doesn_t_lose_the_alarm(monkeypatch):
    w, sent, clock = watch()

    def refused():
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(access, "load_accounts", lambda: {"reader": ("reader", "hash")})
    w.check_users()  # the first look: nothing to compare with
    monkeypatch.setattr(w, "_save", refused)
    assert w.alarm("library", "", "The library can't be read", "text") and len(sent) == 1
    monkeypatch.setattr(access, "load_accounts", lambda: {"reader": ("reader", "hash"), "other": ("reader", "hash")})
    w.check_users()
    assert sent[-1][0] == "Users changed"
    w.event("login", user="reader", ip="203.0.113.9", agent="KOReader/2026.07")
    assert sent[-1][0] == "New device: reader"


def test_a_state_that_can_t_be_written_doesn_t_keep_requests_waiting(monkeypatch):
    bell.state_file().mkdir(parents=True)  # a folder where the file should be: every write is refused
    w, sent, clock = watch()
    slept = []
    monkeypatch.setattr(storage.time, "sleep", own(slept))
    assert w.alarm("library", "", "The library can't be read", "text") and not slept


def test_a_message_is_cut_by_bytes_and_never_inside_a_letter():
    _, body = bell.ntfy_message("https://ntfy.sh/" + "x" * 24, "ä" * 300, "ä" * 3000, "high")
    assert len(body["message"].encode()) <= 3500 and len(body["title"].encode()) <= 200
    assert set(body["message"]) == {"ä"} and set(body["title"]) == {"ä"}


def test_a_topic_short_enough_to_guess_is_warned_about(monkeypatch, capsys):
    conf = {**bell.DEFAULTS}
    assert bell.weak_topic({**conf, "ntfy": "https://ntfy.sh/alarms"})
    assert not bell.weak_topic({**conf, "ntfy": "https://ntfy.sh/" + "x" * 24})
    assert not bell.weak_topic({**conf, "ntfy": "https://ntfy.sh/alarms", "ntfy_token": "t"})  # behind a login
    assert not bell.weak_topic({**conf, "ntfy": "https://my.example/alarms"})  # your own server
    assert not bell.weak_topic(conf)
    mistyped = {**conf, "ntfy": "https://[oops/alarms"}
    assert not bell.weak_topic(mistyped) and any("mistyped" in c for c in bell.channels(mistyped))  # no reason to stop the server
    monkeypatch.setattr(bell, "settings", lambda: {**conf, "ntfy": "https://ntfy.sh/alarms"})
    cli.main(["bell"])
    said = capsys.readouterr()
    assert "short enough to guess" in said.err and "at most 10 sent out an hour" in said.out  # the warning, and the result
    logged = []
    w = bell.Watch({**conf, "ntfy": "https://ntfy.sh/alarms"}, send=lambda *a: None, log=logged.append)
    w.start()  # as the server starts
    assert "alarms" not in "".join(logged).replace("the alarms", "")  # says so, without the topic
    assert any("short enough to guess" in line for line in logged)
