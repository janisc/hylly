"""The downloader against a fake CDN: resume, checksums, expired links, stopping and the manifest; and what a download
or a seed costs when it is cut short, each way found to lose one reproduced (once among the tests of writes cut short,
test_storage.py)."""

from __future__ import annotations

import csv
import dataclasses
import errno
import functools
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx
import os
import pytest
import shutil

from hylly import cli, copies, exits, humble, itchio, paths, plan, settings, shopping, shops, words
from hylly import download as dl
from hylly import drivethrurpg as dtm
from hylly import storybundle as sbm

from helpers import BODIES, DRIVETHRURPG_KEY, DriveThruRPGAPI, FakeCDN, FakeItch, Gone, ITCHIO_KEY, ITCH_FILES
from helpers import LAST_MODIFIED, ORDERS, STARTS, HumbleSite, StoryBundleSite, blob, make_job, order, struct, sub


MIB = 1024 * 1024


def humble_file(j: dl.Job) -> humble.File:
    """A job as one of a Humble title's files on the shops' core: named in its folder by its place, its fallbacks behind it."""
    return humble.File(key=j.path.rpartition("/")[2], name=j.filename, size=j.size, md5=j.md5, label=j.label, role=j.role,
                       ext=j.ext, url=j.url, where=j.device, fallbacks=[humble_file(f) for f in j.fallbacks])


def titled(*jobs: dl.Job) -> list[humble.Title]:
    """Jobs as Humble's titles on the shops' core, one title a folder, in the order given (Title.place)."""
    out: dict[str, humble.Title] = {}
    for j in jobs:
        t = out.setdefault(j.folder, humble.Title(key=j.shop_id, name=j.title, by="", kind=j.kind, under=j.bundle,
                                                 purchased=j.purchased, place=(len(out),), folder=j.folder, device=j.device))
        t.files.append(humble_file(j))
    return list(out.values())


def chosen(jobs: list[dl.Job], manifest, root, **options):
    """What Humble's download chooses of these jobs (shopping.select, as for every shop)."""
    return shopping.select("humble", titled(*jobs), manifest, root, **options)


def places(sel) -> list[str]:
    return [path for _, _, path, _ in sel.pending]


def jobs_of(sel) -> list[dl.Job]:
    """The downloader's jobs for what select chose, as the run makes them (shopping.make_job)."""
    return [shopping.make_job("humble", t, f, path, newer) for t, f, path, newer in sel.pending]


@pytest.fixture
def setup(tmp_path):
    def make(files: dict[str, bytes], refresh=None):
        cdn = FakeCDN({f"/{k}": v for k, v in files.items()})
        manifest = dl.Manifest(tmp_path, "humble")
        lines: list[str] = []
        d = dl.Downloader(
            tmp_path, manifest, refresh=refresh or (lambda job: job.url), transport=httpx.MockTransport(cdn.handler),
            log=lines.append, backoff=0.0,
        )
        return cdn, manifest, d, lines
    return make


def test_download_then_skip(setup, tmp_path):
    files = {"a.epub": blob(5000), "b.pdf": blob(9000, 3, "pdf"), "c.cbz": blob(100, 5, "cbz")}
    cdn, manifest, d, _ = setup(files)
    jobs = [make_job(n, data) for n, data in files.items()]
    results = d.run(jobs)
    assert len(results["ok"]) == 3 and not results["failed"]
    for j in jobs:
        assert (tmp_path / j.path).read_bytes() == files[j.filename]
        entry = manifest.get(j.path)
        assert entry["md5_ok"] and entry["last_modified"] == LAST_MODIFIED
        assert entry["md5_source"] == "shop" and entry["shop_id"] == j.filename  # the shop's listed MD5 vouched for it
    again = chosen(jobs, dl.Manifest(manifest.root, manifest.shop), tmp_path)
    assert again.pending == [] and again.done == 3


def test_resumes_a_partial_file(setup, tmp_path):
    data = blob(10000)
    cdn, manifest, d, _ = setup({"a.epub": data})
    job = make_job("a.epub", data)
    part = tmp_path / (job.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(data[:4000])
    assert len(d.run([job])["ok"]) == 1
    assert cdn.requests == [("/a.epub", "bytes=4000-")]
    assert (tmp_path / job.path).read_bytes() == data and not part.exists()


class Replaced:
    """A server whose file is replaced by another after its first answer, which it cut short: each version with its
    ETag header (or none) and Last-Modified. honours: whether it heeds If-Range, sending the whole file when it names
    another version; without, a range is the current version's, whatever the If-Range."""

    def __init__(self, old: bytes, new: bytes, etags=(None, None), honours: bool = False):
        self.versions = [(old, etags[0], "Thu, 01 Jan 2026 00:00:00 GMT"), (new, etags[1], "Fri, 02 Jan 2026 00:00:00 GMT")]
        self.honours, self.asked = honours, []  # asked: each request's (Range, If-Range)

    def __call__(self, request, _):
        data, etag, modified = self.versions[min(len(self.asked), 1)]
        rng, since = request.headers.get("range"), request.headers.get("if-range")
        self.asked.append((rng, since))
        headers = {"last-modified": modified, **({"etag": etag} if etag else {})}
        if len(self.asked) == 1:  # the connection ends early, the full length announced
            return httpx.Response(200, content=data[:1000], headers={**headers, "content-length": str(len(data))})
        if rng and not (self.honours and since and since not in (etag, modified)):
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=data[start:], headers={
                **headers, "content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return httpx.Response(200, content=data, headers=headers)


@pytest.mark.parametrize("etags, new_size, honours, if_range", [
    (('"v1-opaque"', '"v2-opaque"'), 3000, False, '"v1-opaque"'),  # another ETag in the answer to the rest
    ((None, None), 3600, False, "Thu, 01 Jan 2026 00:00:00 GMT"),  # no ETag, another total length
    (('"v1-opaque"', '"v2-opaque"'), 3000, True, '"v1-opaque"'),  # a server that heeds If-Range sends it whole
    ((None, None), 3000, True, "Thu, 01 Jan 2026 00:00:00 GMT"),  # by Last-Modified, where it gives no ETag
    (('W/"v1"', 'W/"v2"'), 3000, False, None),  # a weak ETag is no validator for a range: none is named
])
def test_a_file_changed_at_the_server_between_two_tries_in_one_run_comes_whole_never_spliced(setup, tmp_path, etags, new_size,
                                                                                             honours, if_range):
    """A connection cut, then the shop's file replaced before the rest is asked for: the resumed request names the
    first answer's version (If-Range: its ETag, else its Last-Modified), and an answer of another ETag or total length
    throws the part away, so the file comes whole. Nothing else would tell the splice of two files from the file here:
    no MD5 is listed, and the ETags are none."""
    old, new = blob(3000, 3), blob(new_size, 7)
    cdn, manifest, d, _ = setup({"a.epub": new})
    server = Replaced(old, new, etags, honours)
    cdn.fail["/a.epub"] = [server] * 3
    job = make_job("a.epub", new, with_md5=False)
    assert len(d.run([job])["ok"]) == 1
    assert (tmp_path / job.path).read_bytes() == new and not (tmp_path / (job.path + ".part")).exists()
    assert manifest.get(job.path)["md5"] == hashlib.md5(new).hexdigest()
    assert server.asked[1] == ("bytes=1000-", if_range)
    assert server.asked[2:] in ([], [(None, None)])  # whole at once, or asked for again from its start


class CutOff(httpx.SyncByteStream):
    """An answer whose connection drops before its first chunk is whole: some bytes come, none reach the part."""

    def __init__(self, some: bytes):
        self.some = some

    def __iter__(self):
        yield self.some
        raise httpx.ReadError("connection reset")


@pytest.mark.parametrize("etags", [('"v1-opaque"', '"v2-opaque"'), (None, None)], ids=["etag", "last-modified"])
def test_a_whole_new_answer_cut_off_before_it_was_written_leaves_the_part_held_to_its_own_version(setup, tmp_path, etags):
    """The rest asked for, the server says the file changed (If-Range) and sends the new one whole, but the connection
    drops before any of it is written: the part still holds the old version's start, so the next try names the old
    version, and the new one comes whole. It used to be held to the new one's version by then, and got the new one's
    rest, appended to the old start: a splice recorded as done."""
    old, new = blob(3000, 3), blob(3000, 7)
    cdn, manifest, d, _ = setup({"a.epub": new})
    modified = ["Thu, 01 Jan 2026 00:00:00 GMT", "Fri, 02 Jan 2026 00:00:00 GMT"]
    asked = []

    def server(request, _):
        rng, since = request.headers.get("range"), request.headers.get("if-range")
        asked.append((rng, since))
        version = 0 if len(asked) == 1 else 1
        headers = {"last-modified": modified[version], **({"etag": etags[version]} if etags[version] else {})}
        if version == 0:  # cut short, the full length announced
            return httpx.Response(200, content=old[:1000], headers={**headers, "content-length": "3000"})
        if rng and since in (etags[1], modified[1]):  # heeds If-Range: the rest of the version named
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=new[start:], headers={**headers, "content-range": f"bytes {start}-2999/3000"})
        if len(asked) == 2:
            return httpx.Response(200, headers={**headers, "content-length": "3000"}, stream=CutOff(new[:500]))
        return httpx.Response(200, content=new, headers={**headers, "content-length": "3000"})

    cdn.fail["/a.epub"] = [server] * 4
    job = make_job("a.epub", new, with_md5=False)  # nothing but the versions to tell a splice by
    assert len(d.run([job])["ok"]) == 1
    assert (tmp_path / job.path).read_bytes() == new and manifest.get(job.path)["md5"] == hashlib.md5(new).hexdigest()
    assert asked[2] == ("bytes=1000-", etags[0] or modified[0])  # still the old version's start: held to it


def test_a_part_the_disk_changed_between_tries_is_read_again_not_taken_by_its_size(setup, tmp_path, monkeypatch):
    """The MD5 kept from one try to the next stands for what was written. After the disk's own trouble (a share
    dropping out), what the part holds is read again, whatever its size: bytes the share lost while it kept the size
    were recorded as the shop's file, checked by its MD5, which the file doesn't have."""
    import pathlib

    data = blob(3000)
    cdn, manifest, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = ["short"]  # half of it, then the rest asked for
    job = make_job("a.epub", data)
    part = tmp_path / (job.path + ".part")
    real, spoilt = pathlib.Path.open, []

    def opening(self, mode="r", *a, **k):
        if self == part and mode == "ab" and not spoilt:  # the share drops out, and loses 500 bytes as it goes
            with real(self, "r+b") as fh:
                fh.write(bytes(500))
            spoilt.append(mode)
            raise OSError(errno.EINVAL, "Invalid argument")
        return real(self, mode, *a, **k)

    monkeypatch.setattr(pathlib.Path, "open", opening)
    assert len(d.run([job])["ok"]) == 1 and spoilt
    got = (tmp_path / job.path).read_bytes()
    assert got == data and manifest.get(job.path)["md5"] == hashlib.md5(got).hexdigest()


def test_a_part_cut_again_and_again_is_not_read_back_from_the_disk_between_tries(setup, tmp_path, monkeypatch):
    """The MD5 of what a run has written into a part is kept from one try to the next: a part on a share isn't read again
    whole after every dropped connection. A part from an earlier run is read once, at the first try, to go on from."""
    data = blob(12000)
    cdn, manifest, d, _ = setup({"a.epub": data})

    def a_little(request, whole):
        rng = request.headers.get("range")
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        if not rng:
            return httpx.Response(200, content=whole[:1000], headers={"content-length": str(len(whole))})
        return httpx.Response(206, content=whole[start:start + 1000],
                              headers={"content-range": f"bytes {start}-{start + 999}/{len(whole)}"})

    cdn.fail["/a.epub"] = [a_little] * 12
    job = make_job("a.epub", data)
    part = tmp_path / (job.path + ".part")
    read, real = [], dl.md5_of
    monkeypatch.setattr(dl, "md5_of", lambda path: read.append(path) or real(path))
    assert len(d.run([job])["ok"]) == 1 and (tmp_path / job.path).read_bytes() == data
    assert len(cdn.requests) == 12 and part not in read  # cut eleven times, never read back
    assert manifest.get(job.path)["md5_source"] == "shop"

    (tmp_path / job.path).unlink()
    part.write_bytes(data[:3000])  # an earlier run's
    cdn, manifest, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = [a_little] * 9
    assert len(d.run([job])["ok"]) == 1 and (tmp_path / job.path).read_bytes() == data
    assert len(cdn.requests) == 9 and read.count(part) == 1  # once, to go on from


def test_server_ignoring_the_range_starts_over(setup, tmp_path):
    data = blob(10000)
    cdn, _, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = ["ignore-range"]
    job = make_job("a.epub", data)
    part = tmp_path / (job.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(data[:4000])
    assert len(d.run([job])["ok"]) == 1
    assert (tmp_path / job.path).read_bytes() == data


def test_short_response_is_resumed(setup):
    data = blob(10000)
    cdn, _, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = ["short"]
    job = make_job("a.epub", data)
    assert len(d.run([job])["ok"]) == 1
    assert cdn.requests == [("/a.epub", None), ("/a.epub", "bytes=5000-")]


def test_bad_md5_is_retried_once_then_fails(setup, tmp_path):
    data = blob(3000)
    cdn, manifest, d, _ = setup({"a.epub": data, "b.epub": data})
    cdn.fail["/a.epub"] = ["corrupt"]
    cdn.fail["/b.epub"] = ["corrupt", "corrupt"]
    a, b = make_job("a.epub", data), make_job("b.epub", data)
    results = d.run([a, b])
    assert [j.filename for j, _ in results["ok"]] == ["a.epub"]
    assert [j.filename for j, _ in results["failed"]] == ["b.epub"]
    assert "MD5" in results["failed"][0][1]
    assert not (tmp_path / b.path).exists() and not (tmp_path / (b.path + ".part")).exists()
    assert manifest.get(b.path) is None


def test_expired_link_gets_a_fresh_one(setup):
    data = blob(3000)
    calls = []

    def refresh(job):
        calls.append(job.filename)
        return job.url.replace("t=sig", "t=fresh")

    cdn, _, d, _ = setup({"a.epub": data}, refresh=refresh)
    cdn.fail["/a.epub"] = [403]
    assert len(d.run([make_job("a.epub", data)])["ok"]) == 1
    assert calls == ["a.epub"]


def test_file_gone_from_humble_fails_cleanly(setup):
    data = blob(3000)
    cdn, _, d, _ = setup({"a.epub": data}, refresh=lambda job: None)
    cdn.fail["/a.epub"] = [403]
    results = d.run([make_job("a.epub", data)])
    assert "no longer lists" in results["failed"][0][1]


@pytest.mark.parametrize("where", ["http://cdn.test/a.epub?gamekey=K&t=sig", "https://192.168.1.20/a.epub?gamekey=K",
                                   "https://[::1]:8770/a.epub?gamekey=K", "https://files.example.com/a.epub?gamekey=K"])
def test_a_link_or_its_redirect_over_plain_http_or_to_an_address_at_home_fails_the_file_and_nothing_is_fetched(
        setup, tmp_path, monkeypatch, where):
    """Only a shop that is hostile or broken would give one: a download follows a link, and each redirect, only over
    https and only to the internet, so nothing at home is asked, nothing comes over plain http, and nothing of it is
    filed as the book. The failure never says the address, where an order key travels."""
    from hylly import fetching

    monkeypatch.setattr(fetching, "resolve", lambda host, port: ["10.0.0.7"] if host == "files.example.com" else
                        ["11.22.33.44"])  # a name at home, and one on the internet
    data = blob(3000)
    cdn, manifest, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = [lambda request, data: httpx.Response(302, headers={"location": where})]
    sent_on, given = make_job("a.epub", data), make_job("b.epub", data)
    given.url = where
    results = d.run([sent_on, given])
    assert not results["ok"] and cdn.requests == [("/a.epub", None)]  # the shop's link, and nothing after it
    for job, why in results["failed"]:
        assert ("isn't https" if where.startswith("http:") else "the home network") in why and "gamekey" not in why
        assert "192.168" not in why and "files.example" not in why and "::1" not in why
        assert not (tmp_path / job.path).exists() and not (tmp_path / (job.path + ".part")).exists()
        assert manifest.get(job.path) is None


def test_the_address_a_connection_reached_and_this_computer_s_own_name_are_refused_but_not_through_a_proxy(monkeypatch):
    """The address a connection really reached is looked at too, as a name may answer otherwise a moment later, and one
    on the internet passes; an answer that says none (a made-up one) passes too. A name is resolved as the connection
    would resolve it: this computer's own, in any case, is refused. Through a proxy for https only the name is looked
    at, as the connection is the proxy's."""
    from types import SimpleNamespace

    from hylly import fetching

    class Reached:
        def __init__(self, address):
            self.address = address

        def get_extra_info(self, what):
            return (self.address, 443) if what == "server_addr" else None

    answer = lambda address: SimpleNamespace(extensions={"network_stream": Reached(address)} if address else {})  # noqa: E731
    fetching.check_answer(answer("11.22.33.44"))
    fetching.check_answer(answer(None))
    for home in ("192.168.1.20", "127.0.0.1", "::ffff:10.0.0.7", "fe80::1", "100.100.100.100", "0.0.0.0"):
        with pytest.raises(fetching.Refused):
            fetching.check_answer(answer(home))
    monkeypatch.setattr(fetching, "resolve", fetching.resolve.real)  # this computer's own name, which asks no network
    for here in ("https://localhost/a.epub", "https://LocalHost:8770/a.epub", "https://127.0.0.1/a.epub"):
        with pytest.raises(fetching.Refused):
            fetching.check(httpx.Request("GET", here))
    monkeypatch.setattr(fetching.urllib.request, "getproxies", lambda: {})
    assert fetching.hooks() == {"request": [fetching.check], "response": [fetching.check_answer]}
    monkeypatch.setattr(fetching.urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:3128"})
    assert fetching.hooks() == {"request": [fetching.check], "response": []}


def test_a_connection_gone_before_its_address_is_read_is_network_trouble_and_the_file_comes_at_the_next_try(setup, tmp_path,
                                                                                                          monkeypatch):
    """Not a refusal, which would fail the file for good, nor a slip of the code: httpx's own ReadError, asked again as
    any network trouble is, and never with the address."""
    from hylly import fetching

    monkeypatch.setattr(fetching.urllib.request, "getproxies", lambda: {})  # no proxy: the address reached is looked at
    with pytest.raises(httpx.ReadError, match="before its address could be checked") as gone:
        fetching.check_answer(httpx.Response(200, extensions={"network_stream": Gone()},
                                             request=httpx.Request("GET", "https://cdn.test/a.epub?gamekey=K")))
    assert "gamekey" not in str(gone.value) and "cdn.test" not in str(gone.value)
    data = blob(3000)
    cdn, manifest, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = [lambda request, data: httpx.Response(200, content=data, extensions={"network_stream": Gone()})]
    job = make_job("a.epub", data)
    assert len(d.run([job])["ok"]) == 1 and len(cdn.requests) == 2 and (tmp_path / job.path).read_bytes() == data


def test_a_name_whose_lookup_hangs_is_waited_for_only_as_long_as_the_request_may_take_to_connect(monkeypatch):
    """The system's resolver keeps no time of httpx's: one that doesn't answer would make each request wait twice, for the
    check and then as it connects. Past the request's own time to connect, the name is let through, as one that can't
    be resolved is, and the connection then fails on its own."""
    import threading
    import time as clock

    from hylly import fetching

    answer, asked = threading.Event(), []

    def hangs(host, port):
        asked.append(host)
        answer.wait(10)
        return ["10.0.0.7"]  # what it would say, too late

    monkeypatch.setattr(fetching, "resolve", hangs)
    request = httpx.Client(timeout=httpx.Timeout(30.0, connect=0.2)).build_request("GET", "https://files.example.com/a.epub")
    started = clock.monotonic()
    try:
        fetching.check(request)  # let through
        assert 0.15 < clock.monotonic() - started < 3 and asked == ["files.example.com"]
    finally:
        answer.set()
    monkeypatch.setattr(fetching, "resolve", lambda host, port: ["10.0.0.7"])  # one that answers in time is gone by
    with pytest.raises(fetching.Refused):
        fetching.check(request)


def test_server_errors_are_retried(setup):
    data = blob(3000)
    cdn, _, d, _ = setup({"a.epub": data})
    cdn.fail["/a.epub"] = [500, 500, 500]
    assert len(d.run([make_job("a.epub", data)])["ok"]) == 1


def test_stopping_keeps_the_part_and_the_next_run_resumes(setup, tmp_path):
    data = blob(3 * MIB)
    cdn, manifest, d, _ = setup({"big.cbz": data})
    job = make_job("big.cbz", data, kind="comic")

    def stop_after_first_chunk(request, body):
        def stream():
            yield body[:MIB]
            d.stop.set()
            yield body[MIB:]
        return httpx.Response(200, content=stream())

    cdn.fail["/big.cbz"] = [stop_after_first_chunk]
    results = d.run([job])
    assert results["stopped"] and not results["ok"]
    part = tmp_path / (job.path + ".part")
    assert part.stat().st_size >= MIB and manifest.get(job.path) is None

    _, manifest2, d2, _ = setup({"big.cbz": data})
    assert len(d2.run([job])["ok"]) == 1
    assert (tmp_path / job.path).read_bytes() == data
    assert manifest2.get(job.path)["md5_ok"]


def stale(job: dl.Job, listed: bytes) -> dl.Job:
    """The job as Humble lists it: another file's size and MD5."""
    job.size, job.md5 = len(listed), hashlib.md5(listed).hexdigest()
    return job


def test_stale_humble_listing_is_checked_against_the_server(setup, tmp_path):
    served, listed = blob(5000, 3), blob(2000)
    cdn, manifest, d, _ = setup({"a.epub": served, "b.epub": blob(1000), "c.epub": blob(9000, 5)})
    cdn.etags["/a.epub"] = hashlib.md5(served).hexdigest()
    a = stale(make_job("a.epub", served), listed)  # newer file is larger than listed
    b = stale(make_job("b.epub", blob(1000)), blob(1500))  # ... or smaller
    cdn.etags["/b.epub"] = hashlib.md5(blob(1000)).hexdigest()
    c = stale(make_job("c.epub", blob(9000, 5)), listed)
    cdn.etags["/c.epub"] = "not-an-md5"  # nothing to compare with
    results = d.run([a, b, c])
    assert len(results["ok"]) == 3, results["failed"]
    assert manifest.get(a.path)["md5_source"] == "cdn" and manifest.get(a.path)["listed_md5"] == a.md5
    assert manifest.get(b.path)["listed_size"] == 1500
    assert manifest.get(c.path)["md5_source"] == "transfer" and not manifest.get(c.path)["md5_ok"]
    assert chosen([a, b, c], dl.Manifest(manifest.root, manifest.shop), tmp_path).done == 3  # not "changed" next time


def test_complete_part_with_a_stale_listing_starts_over(setup, tmp_path):
    # Seen live: the part already holds the whole (smaller) server file, Humble lists a larger
    # size, and the server answers the resume with a bare 416.
    served = blob(4000, kind="pdf")
    cdn, manifest, d, _ = setup({"a.pdf": served})
    cdn.etags["/a.pdf"] = hashlib.md5(served).hexdigest()
    cdn.fail["/a.pdf"] = ["bare-416"]
    job = stale(make_job("a.pdf", served), blob(4500, kind="pdf"))
    part = tmp_path / (job.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(served)
    results = d.run([job])
    assert len(results["ok"]) == 1, results["failed"]
    assert cdn.requests == [("/a.pdf", "bytes=4000-"), ("/a.pdf", None)]
    assert manifest.get(job.path)["md5_source"] == "cdn"


def test_corrupt_bytes_fail_against_the_server_checksum(setup):
    data = blob(4000)
    cdn, _, d, _ = setup({"a.epub": data})
    cdn.etags["/a.epub"] = hashlib.md5(data).hexdigest()
    cdn.fail["/a.epub"] = ["corrupt", "corrupt"]
    job = stale(make_job("a.epub", data), blob(10))
    job.size = len(data)
    results = d.run([job])
    assert "server's" in results["failed"][0][1]


def multipart_etag(data: bytes, part: int) -> str:
    digests = b"".join(hashlib.md5(data[i:i + part]).digest() for i in range(0, len(data), part))
    return f"{hashlib.md5(digests).hexdigest()}-{-(-len(data) // part)}"


def test_multipart_etags(tmp_path):
    import random

    data = random.Random(1).randbytes(12 * MIB + 123)
    f = tmp_path / "x.bin"
    f.write_bytes(data)
    assert dl.multipart_etag_matches(f, multipart_etag(data, 5 * MIB))
    assert dl.multipart_etag_matches(f, multipart_etag(data, 8 * MIB))
    assert dl.multipart_etag_matches(f, multipart_etag(data, len(data)))  # a single part
    assert not dl.multipart_etag_matches(f, multipart_etag(data[:-1] + b"!", 5 * MIB))


def test_multipart_etag_confirms_a_stale_listing(setup):
    import random

    data = STARTS["cbz"] + random.Random(2).randbytes(6 * MIB)
    cdn, manifest, d, _ = setup({"big.cbz": data})
    cdn.etags["/big.cbz"] = multipart_etag(data, 5 * MIB)
    job = stale(make_job("big.cbz", data, kind="comic"), b"old")
    assert len(d.run([job])["ok"]) == 1
    assert manifest.get(job.path)["md5_source"] == "cdn"


def test_share_errors_are_retried(setup, tmp_path, monkeypatch):
    data = blob(3000)
    _, manifest, d, _ = setup({"a.epub": data})
    real_replace, calls = dl.os.replace, []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise OSError(64, "The specified network name is no longer available")
        real_replace(src, dst)

    monkeypatch.setattr(dl.os, "replace", flaky_replace)
    job = make_job("a.epub", data)
    assert len(d.run([job])["ok"]) == 1 and len(calls) == 2
    assert (tmp_path / job.path).read_bytes() == data


def test_existing_file_is_adopted_without_downloading(setup, tmp_path):
    data = blob(3000)
    cdn, manifest, d, _ = setup({"a.epub": data})
    job = make_job("a.epub", data)
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    assert len(d.run([job])["ok"]) == 1
    assert cdn.requests == [] and manifest.get(job.path)["adopted"]


def test_changed_file_needs_update_and_old_one_is_kept(setup, tmp_path):
    old, new = blob(3000), blob(3000, 7)
    cdn, manifest, d, _ = setup({"a.epub": new})
    job = make_job("a.epub", new)
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old)
    manifest.add({"path": job.path, "md5": hashlib.md5(old).hexdigest(), "size": len(old)})

    sel = chosen([job], manifest, tmp_path)
    assert sel.pending == [] and sel.changed == 1
    sel = chosen([job], manifest, tmp_path, update=True)
    assert len(d.run(jobs_of(sel))["ok"]) == 1
    assert dest.read_bytes() == new
    assert (tmp_path / "superseded" / job.path).read_bytes() == old


def test_select_filters_and_limits_by_title(tmp_path):
    jobs = [make_job("a.epub", b"1", folder="A"), make_job("a-code.zip", b"2", folder="A"),
            make_job("b.epub", b"3", folder="B"), make_job("c.cbz", b"4", kind="comic", folder="C")]
    m = dl.Manifest(tmp_path, "humble")
    assert [f.name for _, f, _, _ in chosen(jobs, m, tmp_path, limit=1).pending] == ["a.epub", "a-code.zip"]
    assert [f.name for _, f, _, _ in chosen(jobs, m, tmp_path, kinds={"comic"}).pending] == ["c.cbz"]
    assert [f.name for _, f, _, _ in chosen(jobs, m, tmp_path, title="b.ep").pending] == ["b.epub"]


def test_no_shop_s_download_takes_a_program_a_script_or_a_shortcut(setup, tmp_path):
    """The never-list is every shop's (download.never: NEVER, which no rules file changes, and the rules' [media]
    programs). Humble's selection leaves such an extra out and names it; one that reaches the downloader anyway (a
    StoryBundle book's name comes only with its link) is never asked for, and is named too."""
    def extra(name: str, data: bytes) -> dl.Job:
        job = make_job(name, data, folder="Atlas of Moonwake")
        job.role, job.title = "extra", "Atlas of Moonwake"
        return job

    jobs = [extra("atlas.epub", blob(3000)), extra("Setup.exe.", b"MZ" + bytes(90)), extra("Read me.URL", b"[InternetShortcut]"),
            extra("maps.zip", b"PK\x03\x04" + bytes(50)), extra("moonwake.love", b"PK\x03\x04" + bytes(9))]
    sel = chosen(jobs, dl.Manifest(tmp_path, "humble"), tmp_path)
    assert [f.name for _, f, _, _ in sel.pending] == ["atlas.epub", "maps.zip"]
    assert dl.left_out(sel.left) == [
        "Left out, not failures: 3 files of a kind no download takes (programs, scripts and shortcuts):",
        "  'Atlas of Moonwake': 'Setup.exe.' (EXE, a program, script or shortcut)",
        "  'Atlas of Moonwake': 'Read me.URL' (URL, a program, script or shortcut)",
        "  'Atlas of Moonwake': 'moonwake.love' (LOVE, a program, script or shortcut)"]  # a program by the rules alone
    assert dl.left_out([]) == [] and not dl.never("pdf") and dl.never(dl.kind_of("notes.pdf:run.bat"))

    cdn, manifest, d, _ = setup({"installer.msi": b"x" * 10, "tale.epub": blob(2000)})
    sneaked = make_job("installer.msi", b"x" * 10)  # as a StoryBundle book whose link named a program
    results = d.run([sneaked, make_job("tale.epub", blob(2000))])
    assert [j.filename for j, _ in results["ok"]] == ["tale.epub"] and not results["failed"]
    assert [(x.name, x.kind, x.there) for x in results["left"]] == [("installer.msi", "msi", False)]
    assert not [path for path, _ in cdn.requests if path.endswith(".msi")] and manifest.get(sneaked.path) is None
    assert not (tmp_path / sneaked.path).exists() and not (tmp_path / (sneaked.path + ".part")).exists()


def humble_jobs(planned, root) -> list[dl.Job]:
    """A plan's files as Humble's download would fetch them all, in its order: shopping.select on the plan's titles
    (humble.titles_of), each made a job by shopping.make_job."""
    return jobs_of(shopping.select("humble", humble.titles_of(planned), dl.Manifest(root, "humble"), root))


def test_humble_s_jobs_come_small_and_useful_kinds_first(tmp_path):
    jobs = humble_jobs(plan.build(ORDERS), tmp_path)
    kinds = [j.kind for j in jobs]
    assert kinds == sorted(kinds, key=lambda k: humble.kind_order()[k])  # books first, comics last: the kinds' order
    assert list(humble.kind_order()) == ["book", "tech", "rpg", "comic", "manual"]  # as kinds.toml lists them
    pss = [j for j in jobs if j.shop_id == "pss"]
    assert [(j.role, j.filename) for j in pss] == [("book", "pss.epub"), ("extra", "pss_code.zip")]
    assert all(j.url == "" for j in jobs)  # no link from the orders: Humble's site lists none, and a snapshot's is stale


def on_the_site(key: str, o: dict) -> dict:
    """An order of the order API's shape as Humble's client reads it from the site now (humble.order_of): with the
    purchase's id and its items' entitlement ids, and each file's name where its link was (the site lists no link)."""
    import copy

    o = copy.deepcopy(o)
    o["entitlement"] = key
    for s in o.get("subproducts") or []:
        s["entitlement"] = f"{key}-{s.get('machine_name')}"
        for d in s.get("downloads") or []:
            for x in d.get("download_struct") or []:
                if (x.get("url") or {}).get("web"):
                    x["url"] = {"web": x["url"]["web"].split("?")[0].rsplit("/", 1)[1]}
    return o


class Answers:
    """Humble's site for a Library, as a stand-in for its client: its purchases from these orders with their items'
    entitlement ids (on_the_site), or as answers say, one listing at a time ((data, the keys it gave none for) each
    time); and an item's links (link_set) from the orders given or answered, signed afresh each time, or as links say,
    one set at a time (an exception, raised; None, the orders' own). asked: the sets of links asked for."""

    def __init__(self, orders: dict | None = None, answers: list | None = None, links: list | None = None):
        self.given, self.answers, self.links, self.asked = dict(orders or {}), answers, links, 0
        self.listed: dict = {}

    def orders(self, keys, progress=None):
        data, missing = (self.answers.pop(0) if self.answers is not None else
                         ({k: self.given[k] for k in keys if k in self.given}, [k for k in keys if k not in self.given]))
        self.given.update(data)
        return {k: on_the_site(k, o) for k, o in data.items()}, missing

    def link_set(self, entitlement: str):
        self.asked += 1
        planned = self.links.pop(0) if self.links else None
        if isinstance(planned, Exception):
            raise planned
        key, _, machine = entitlement.partition("-")
        for s in (self.given.get(key) or {}).get("subproducts") or []:
            if s.get("machine_name") == machine:
                return {machine: {f"{machine}_{d.get('platform')}": {
                    x["url"]["web"].split("?")[0].rsplit("/", 1)[1]: x["url"]["web"].split("?")[0] + f"?t=fresh{self.asked}"
                    for x in d.get("download_struct") or []} for d in s.get("downloads") or []}}, 86400.0
        raise humble.ShopError("Humble Bundle made no download link (not_found): Hylly may need an update.", 400)


def library_of(planned, root, client) -> tuple[humble.Library, list[humble.Title]]:
    """Humble's Library with a stand-in for its client, the orders it gives as the run read them at its start, and the
    plan's titles as the download has them."""
    lib = humble.Library("c" * 80, root)
    lib.client.session.close()
    lib.client = client
    lib.saved = {k: on_the_site(k, o) for k, o in getattr(client, "given", {}).items()}
    return lib, humble.titles_of(planned)


def test_a_fresh_link_finds_its_file_by_the_name_the_plan_read_from_it(tmp_path):
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "Lantern%20Wolf.epub", 1), ("PDF", "Lantern%20Wolf.pdf", 2)])])}
    lib, (t,) = library_of(plan.build(orders), tmp_path, Answers(orders))
    (f,) = t.files
    job = shopping.make_job("humble", t, f, f"{t.folder}/{f.key}")
    assert (job.filename, job.path.rsplit("/", 1)[1]) == ("Lantern Wolf.epub", "Lantern Wolf.epub")
    assert lib.link(t, f).startswith("https://dl.humble.com/Lantern%20Wolf.epub?")  # the link as Humble wrote it
    assert lib.link(t, f.fallbacks[0]).startswith("https://dl.humble.com/Lantern%20Wolf.pdf?")
    assert lib.client.asked == 1  # its item's links asked for once: kept for every file of it
    gone = humble.File(key="x.epub", name="Gone.epub")
    assert lib.link(t, gone) == ""  # a file Humble no longer lists: no link


def test_status_and_verify(setup, tmp_path):
    data = blob(2000)
    _, manifest, d, _ = setup({"a.epub": data, "b.epub": data})
    a, b = make_job("a.epub", data), make_job("b.epub", data)
    d.run([a])
    lines, said = humble.summary(titled(a, b), manifest, tmp_path)
    assert lines[1].startswith("book ") and "partial files: 0" in "\n".join(lines)
    assert said["kinds"]["book"]["done"]["files"] == said["kinds"]["book"]["pending"]["files"] == 1
    assert dl.verify(manifest, tmp_path, check_hash=True) == []
    (tmp_path / a.path).unlink()
    assert dl.verify(manifest, tmp_path) == [(a.path, "missing")]


def test_verify_checks_the_downloads_of_every_shop(tmp_path, capsys):
    gone = []
    for shop in shops.SHOPS:
        record = dl.Manifest(tmp_path, shop)
        for name in ("here.pdf", "gone.pdf"):
            path, data = f"{shop}/Moonwake Press/Lantern Wolf/{name}", blob(1000, kind="pdf")
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_bytes(data)
            record.add({"path": path, "size": len(data), "md5": hashlib.md5(data).hexdigest()})
        (tmp_path / path).unlink()
        gone.append(path)
    assert "itchio" in shops.SHOPS
    with pytest.raises(SystemExit) as e:
        cli.main(["verify", "--json", "--library", str(tmp_path)])
    said = json.loads(capsys.readouterr().out)
    assert e.value.code == exits.PARTLY and said["checked"] == 2 * len(shops.SHOPS)
    assert sorted(p["path"] for p in said["problems"]) == sorted(gone) and {p["problem"] for p in said["problems"]} == {"missing"}
    with pytest.raises(SystemExit):
        cli.main(["verify", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert f"Checked {2 * len(shops.SHOPS)} files: {len(gone)} problems." in out and "missing: itchio/" in out


def test_hylly_download_walks_the_same_list_of_shops(tmp_path, monkeypatch):
    from hylly.logins import SITES

    reached = []
    every = dict(shops.SHOPS)
    monkeypatch.setattr(shops, "SHOPS", {"itchio": every["itchio"], "humble": every["humble"]})  # whatever the table, the
    for shop in every:                                                                              # download goes by it
        monkeypatch.setenv(SITES[shop][2], "a login")
        monkeypatch.setattr(cli, f"cmd_{shop}", lambda args: reached.append(args.command))
    cli.main(["download", "--library", str(tmp_path)])
    assert reached == ["itchio", "humble"]


def test_seed_copies_matching_files_and_leaves_sources(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "lw1.cbz").write_bytes(blob(3000))  # a planned file
    (old / "lw2.cbz").write_bytes(blob(3001))  # same name as a planned file, other content
    (old / "notes.txt").write_bytes(b"hello")
    root = tmp_path / "lib"
    jobs = [make_job("lw1.cbz", blob(3000), kind="comic"), make_job("lw2.cbz", blob(5000, 9), kind="comic")]
    m = dl.Manifest(root, "humble")
    report = dl.seed([old], jobs, root, m, log=lambda s: None)
    assert [j.filename for _, j in report["copied"]] == ["lw1.cbz"]
    assert len(report["other_version"]) == 1 and len(report["unknown"]) == 1
    assert (root / jobs[0].path).read_bytes() == blob(3000) and (old / "lw1.cbz").exists()
    assert dl.seed([old], jobs, root, m, log=lambda s: None)["already"]  # second time: nothing to copy


def test_a_listing_corrected_to_the_md5_the_file_has_is_no_change(tmp_path):
    """Humble's listed MD5 was stale when the file came: its record keeps that one apart (listed_md5) and the file's own
    as md5. Humble corrected its listing since, to the MD5 the file has: the file is done, not changed, as the
    selection has it too (shopping.current), so a seed doesn't copy it in again and a substitute stands as it is."""
    data = blob(1000)
    job = make_job("lantern.epub", data)  # Humble's listing as corrected: the file's own MD5
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    entry = {"path": job.path, "size": len(data), "md5": job.md5, "listed_md5": "0" * 32}  # stale when it came
    assert dl.recorded_state(entry, job, dest) == "done"
    assert dl.recorded_state(entry, dataclasses.replace(job, md5="0" * 32), dest) == "done"  # still the stale one
    assert dl.recorded_state(entry, dataclasses.replace(job, md5="f" * 32), dest) == "changed"  # another file


def test_failures_file_drops_fixed_entries(tmp_path):
    a, b = make_job("a.epub", b"1"), make_job("b.epub", b"2")
    path = tmp_path / "failures.csv"
    assert dl.write_failures(path, {"ok": [], "failed": [(a, "boom"), (b, "boom")], "stopped": []}) == 2
    assert dl.write_failures(path, {"ok": [(a, None)], "failed": [], "stopped": []}) == 1
    m = dl.Manifest(tmp_path, "humble")
    m.add({"path": b.path, "md5": "x", "size": 1})  # finished by a run that was stopped before it wrote failures
    assert dl.write_failures(path, {"ok": [], "failed": [], "stopped": []}, m) == 0


def test_the_failures_file_s_cells_that_would_start_a_formula_get_a_quote_first_and_are_read_back_without(tmp_path):
    """A spreadsheet opens the failures file, and a title or a bundle's name is anyone's to name: a cell that would
    start a formula is written with a ' before it. A later run reads it back without, so a file that now works is
    dropped, and no cell gets a second '."""
    odd = make_job("odd.epub", b"1")
    odd.title, odd.bundle = '=HYPERLINK("http://example.invalid/","Velvet Jinx")', "+Moonwake Press Bundle"
    odd.path = odd.folder = "-Bundle/odd.epub"
    plain = make_job("plain.epub", b"2")
    path = tmp_path / "failures.csv"
    assert dl.write_failures(path, {"ok": [], "failed": [(odd, "@refused"), (plain, "boom")], "stopped": []}) == 2
    assert dl.write_failures(path, {"ok": [], "failed": [], "stopped": []}) == 2  # read back, written again
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = {r["title"]: r for r in csv.DictReader(fh)}
    assert (rows["'" + odd.title]["path"], rows["'" + odd.title]["bundle"], rows["'" + odd.title]["error"]) == (
        "'-Bundle/odd.epub", "'+Moonwake Press Bundle", "'@refused")
    assert rows["plain.epub"]["path"] == plain.path and rows["plain.epub"]["error"] == "boom"
    assert dl.write_failures(path, {"ok": [(odd, None)], "failed": [], "stopped": []}) == 1  # it worked: it goes


def fallback_pair(epub: bytes, pdf: bytes) -> tuple[dl.Job, dl.Job]:
    master = make_job("a.epub", epub, folder="A")
    alt = make_job("a.pdf", pdf, folder="A")
    alt.label, alt.device, alt.extra = "PDF", "tablet", {"instead_of": master.path}
    master.fallbacks = [alt]
    return master, alt


def test_a_failed_format_falls_back_to_the_next(setup, tmp_path):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, lines = setup({"a.epub": epub, "a.pdf": pdf}, refresh=lambda job: None)  # Humble lists no fresh link
    cdn.fail["/a.epub"] = [403]
    master, alt = fallback_pair(epub, pdf)
    results = d.run([master])
    (job, note), = results["ok"]
    assert job is alt and "EPUB: " in note and "got the PDF instead" in note and not results["failed"]
    assert (tmp_path / alt.path).read_bytes() == pdf and not (tmp_path / master.path).exists()
    assert manifest.get(alt.path)["instead_of"] == master.path and manifest.get(alt.path)["device"] == "tablet"
    assert any("got the PDF instead" in line for line in lines)
    sel = chosen([master], manifest, tmp_path)  # the PDF stands in, but the next run tries the EPUB again
    assert places(sel) == [master.path] and sel.done == 0 and sel.substituted == 1
    failures = tmp_path / "failures.csv"
    assert dl.write_failures(failures, {"ok": [], "failed": [(master, "boom")], "stopped": []}) == 1  # an earlier run's failure
    assert dl.write_failures(failures, results, manifest) == 0


def test_a_downloaded_other_format_does_not_stand_in_for_your_choice(setup, tmp_path):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    _, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf})
    master, alt = fallback_pair(epub, pdf)
    alt.extra = {}
    d.run([alt])  # the PDF was downloaded as the choice before: not "instead of" the EPUB
    assert [f.name for _, f, _, _ in chosen([master], manifest, tmp_path).pending] == ["a.epub"]


def test_every_format_failing_is_one_failure(setup):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, _, d, _ = setup({"a.epub": epub, "a.pdf": pdf}, refresh=lambda job: None)
    cdn.fail["/a.epub"], cdn.fail["/a.pdf"] = [403], [403]
    results = d.run([fallback_pair(epub, pdf)[0]])
    (job, error), = results["failed"]
    assert job.filename == "a.epub" and "EPUB: " in error and "PDF: " in error and not results["ok"]


@pytest.mark.parametrize("fault, says", [
    ([404] * 3, "link refused (HTTP 404) even after fetching a fresh one"),  # gone, even with a fresh link
    ([410] * 3, "link refused (HTTP 410) even after fetching a fresh one"),
    (["corrupt"] * 3, "MD5 does not match the shop's and the server gives no checksum, twice"),
    ([403, "no link"], "the shop no longer lists this file"),
])
def test_another_format_comes_only_when_the_shop_s_own_file_is_at_fault(setup, tmp_path, fault, says):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    master, alt = fallback_pair(epub, pdf)
    listed = "no link" not in fault
    refresh = lambda job: job.url if listed or job is alt else None  # noqa: E731
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf}, refresh=refresh)
    cdn.fail["/a.epub"] = [b for b in fault if b != "no link"]
    part = tmp_path / (master.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(epub[:500])  # an earlier run's part of the EPUB: no start for a file gone or broken at the shop
    (job, note), = d.run([master])["ok"]
    assert job is alt and f"EPUB: {says}" in note and note.endswith("got the PDF instead")
    assert manifest.get(alt.path)["instead_of"] == master.path and not part.exists()


def went_down(request, body):
    raise httpx.ConnectError("the network is down", request=request)


def disk_full(request, body):
    raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize("trouble, says", [
    ([went_down] * 9, f"gave up after {dl.NETWORK_RETRIES} retries"),  # a network or share outage of some minutes
    ([500] * 9, f"gave up after {dl.NETWORK_RETRIES} retries"),
    ([429] * 9, f"gave up after {dl.NETWORK_RETRIES} retries: HTTP 429"),  # the shop asking to wait, again and again
    ([403] * 3, "link refused (HTTP 403) even after fetching a fresh one"),  # turned away
])
def test_an_outage_a_429_or_a_refusal_leaves_the_title_failed_for_its_chosen_file(setup, tmp_path, trouble, says):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf})
    cdn.fail["/a.epub"] = list(trouble)
    master, alt = fallback_pair(epub, pdf)
    part = tmp_path / (master.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(epub[:500])
    results = d.run([master])
    (job, error), = results["failed"]
    assert job is master and says in error and not results["ok"]
    assert "/a.pdf" not in [path for path, _ in cdn.requests] and manifest.get(alt.path) is None  # no other format tried
    assert part.read_bytes() == epub[:500]  # stays, for the next run to go on from
    sel = chosen([master], manifest, tmp_path)
    assert places(sel) == [master.path] and sel.substituted == 0  # tried again with the same choice


def test_a_full_disk_stops_the_run_says_the_room_the_rest_need_and_tries_no_other_format(setup, tmp_path):
    """A full disk is the library's trouble, not one file's: the run stops at once, as for a library gone away, rather
    than ask the shop for every file left and fail each at its first write. No other format is tried, nothing is a
    failure (each is tried again with the same choice), the part stays to go on from, and the words say how much room
    the files not downloaded yet need, less what their parts hold."""
    epub, pdf, more = blob(2000, 1), blob(3000, 2, "pdf"), blob(4000, 3)
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf, "b.epub": more, "c.epub": more})
    d.workers = 1  # one after another: the full disk comes first
    cdn.fail["/a.epub"] = [disk_full]
    master, alt = fallback_pair(epub, pdf)
    part = tmp_path / (master.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(epub[:500])
    jobs = [master, make_job("b.epub", more), make_job("c.epub", more)]
    results = d.run(jobs)
    assert not results["ok"] and not results["failed"] and [j for j, _ in results["stopped"]] == jobs
    assert [path for path, _ in cdn.requests] == ["/a.epub"]  # nothing more asked of the shop
    assert d.disk_full and d.blocked.startswith("The library's disk is full, so the run stopped: the 3 files not "
                                                  "downloaded yet need about 9.5 KB of room")
    assert part.read_bytes() == epub[:500] and manifest.get(alt.path) is None
    assert places(chosen([master], manifest, tmp_path)) == [master.path]  # tried again with the same choice


WINDOWS_IN_USE = PermissionError(13, "The process cannot access the file because it is being used by another process")
SHARE_BUSY = OSError(errno.EBUSY, "Resource busy")  # a file open elsewhere, as a share says it on macOS and Linux


def in_use(monkeypatch, path, error: OSError = WINDOWS_IN_USE) -> None:
    """The file at path as one another program holds open (a viewer, the server drawing its pages): it can be read and
    copied, but neither renamed nor deleted; refused as error says."""
    import os

    def refused(real):
        def call(src, *a, **k):
            if os.fspath(src) == os.fspath(path):
                raise type(error)(error.errno, error.strerror, os.fspath(src))
            return real(src, *a, **k)
        return call

    for name in ("rename", "replace", "unlink", "remove"):
        monkeypatch.setattr(os, name, refused(getattr(os, name)))


@pytest.mark.parametrize("error", [WINDOWS_IN_USE, SHARE_BUSY], ids=["windows", "busy share"])
def test_an_old_version_in_use_fails_its_file_once_leaves_nothing_in_superseded_and_brings_no_other_format(setup, tmp_path,
                                                                                                          monkeypatch, error):
    """Superseding is a rename on the library's own disk: an old version another program holds open stays where it was,
    with no copy of it left in superseded, and its new version's download fails at once, as trying again won't help
    (it used to be tried nine times, each leaving another copy) nor another format, which would meet the same. A share
    that calls it busy (macOS and Linux) is the same: it was tried nine times, asking the shop each time."""
    old, new, pdf = blob(3000), blob(3000, 7), blob(3000, 2, "pdf")
    cdn, manifest, d, _ = setup({"a.epub": new, "a.pdf": pdf})
    master, alt = fallback_pair(new, pdf)
    master.replace = True
    dest = tmp_path / master.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old)
    in_use(monkeypatch, dest, error)
    (job, error), = d.run([master])["failed"]
    assert job is master and "couldn't be moved into superseded" in error
    assert [path for path, _ in cdn.requests] == ["/a.epub"] and manifest.get(master.path) is None  # once, and only it
    assert dest.read_bytes() == old and not [p for p in (tmp_path / "superseded").rglob("*") if p.is_file()]  # no copy
    assert (tmp_path / (master.path + ".part")).read_bytes() == new  # the new one, checked, waits for the next run


@pytest.mark.parametrize("removable", [True, False])
def test_superseded_on_another_disk_gets_the_old_version_copied_and_never_in_two_places(setup, tmp_path, monkeypatch,
                                                                                         removable):
    """superseded\\ as a link or a mount to another disk, which no rename reaches: the old version is copied there and
    removed, as shutil.move did. One that can't be removed (in use) has its copy go again, and the file fails as one in
    use does: it is in one place only, never left in both."""
    import os

    old, new = blob(3000), blob(3000, 7)
    cdn, manifest, d, _ = setup({"a.epub": new})
    job = make_job("a.epub", new)
    job.replace = True
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old)
    real = os.replace

    def across(src, dst, *a, **k):
        if Path(dst).is_relative_to(tmp_path / "superseded"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", across)
    if not removable:
        in_use(monkeypatch, dest, SHARE_BUSY)
    results = d.run([job])
    aside = [p.read_bytes() for p in (tmp_path / "superseded").rglob("*") if p.is_file()]
    if removable:
        assert len(results["ok"]) == 1 and dest.read_bytes() == new and aside == [old]
    else:
        (_, error), = results["failed"]
        assert "in use: close it and run again" in error and dest.read_bytes() == old and aside == []
        assert len(cdn.requests) == 1  # no retries


def test_a_later_run_tries_the_chosen_file_again_while_the_substitute_stands_and_keeps_it_when_it_comes(setup, tmp_path):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf})
    master, alt = fallback_pair(epub, pdf)
    cdn.fail["/a.epub"] = [404] * 3
    assert d.run([master])["ok"][0][0] is alt
    substitute = manifest.get(alt.path)

    def next_run():
        record = dl.Manifest(tmp_path, "humble")  # read again, as the next command does
        later = dl.Downloader(tmp_path, record, refresh=lambda job: job.url, transport=httpx.MockTransport(cdn.handler),
                              log=lambda line: None, backoff=0.0)
        sel = chosen([master], record, tmp_path)
        assert places(sel) == [master.path] and sel.substituted == 1 and [s[3].name for s in sel.standing] == ["a.pdf"]
        cdn.requests.clear()
        return later.run(jobs_of(sel)), record

    cdn.fail["/a.epub"] = [404] * 3  # still gone: the PDF stands, and isn't fetched or recorded again
    results, record = next_run()
    (job, note), = results["kept"]  # nothing came: not a file done, nor a failure
    assert job.path == master.path and not results["ok"] and not results["failed"]
    assert note == "EPUB: link refused (HTTP 404) even after fetching a fresh one; the PDF that came instead stays"
    assert {path for path, _ in cdn.requests} == {"/a.epub"} and record.get(alt.path) == substitute
    cdn.fail["/a.epub"] = [went_down]  # an outage: the PDF stands all the same, and the title isn't a failure
    (job, note), = next_run()[0]["kept"]
    assert job.path == master.path and note == "EPUB: the network is down; the PDF that came instead stays"
    cdn.fail["/a.epub"] = []
    results, record = next_run()  # and when the EPUB comes at last, the PDF stays where it is, with its record
    (job, note), = results["ok"]
    assert job.path == master.path and note == "the EPUB came at last; the PDF that came instead stays in the folder"
    assert (tmp_path / master.path).read_bytes() == epub and (tmp_path / alt.path).read_bytes() == pdf
    assert record.get(alt.path)["instead_of"] == master.path
    done = chosen([master], dl.Manifest(tmp_path, "humble"), tmp_path)
    assert done.pending == [] and done.done == 1 and not done.standing


def test_humble_s_summary_lists_the_titles_with_another_format_in_place_of_their_chosen_file(setup, tmp_path):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf})
    master, alt = fallback_pair(epub, pdf)
    cdn.fail["/a.epub"] = [404] * 3
    d.run([master])
    lines, data = humble.summary(titled(master), manifest, tmp_path)
    assert data["substitutes"] == [{"title": "a.epub", "id": "h:a.epub", "chosen": master.path, "chosen_format": "epub",
                                    "chosen_label": "EPUB", "instead": alt.path, "instead_format": "pdf",
                                    "instead_label": "PDF", "can_keep": False}]  # without the plan, none can be told
    assert data["kinds"]["book"]["pending"]["files"] == 1 and data["no_longer_chosen"] == 0  # the PDF is the title's yet
    report = "\n".join(lines)
    assert "another format in place of the chosen file: 1 title, counted as pending" in report
    assert "1 of them can't be kept that way" in report and "hylly set format FORMAT" not in report
    (tmp_path / alt.path).unlink()  # a substitute no longer there isn't one
    lines, data = humble.summary(titled(master), manifest, tmp_path)
    assert data["substitutes"] == [] and "another format" not in "\n".join(lines)


def test_a_format_that_came_instead_and_stays_after_the_chosen_file_came_is_still_the_title_s(setup, tmp_path):
    """The formats behind a title's chosen file are its own, in the summary as in the downloads: the PDF that came
    instead of a gone EPUB is no file "no longer chosen", neither while it stands in nor once the EPUB came at
    last and it stays beside it."""
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf})
    master, alt = fallback_pair(epub, pdf)
    cdn.fail["/a.epub"] = [404] * 3
    d.run([master])
    assert humble.summary(titled(master), manifest, tmp_path)[1]["no_longer_chosen"] == 0  # standing in
    d.run([master])  # the EPUB came at last: the PDF stays in the folder, as ever
    assert (tmp_path / master.path).is_file() and (tmp_path / alt.path).is_file()
    lines, data = humble.summary(titled(master), dl.Manifest(tmp_path, "humble"), tmp_path)
    assert data["no_longer_chosen"] == 0 and data["kinds"]["book"]["done"]["files"] == 1 and not data["substitutes"]
    assert "downloaded but no longer chosen: 0" in "\n".join(lines)


def test_set_format_naming_the_substitute_keeps_it_and_naming_the_chosen_file_has_it_tried_at_once(tmp_path):
    from hylly import curation

    def novel() -> tuple:
        t = next(t for t in humble.titles_of(plan.for_library(ORDERS, tmp_path)) if t.key == "novel")
        return t, t.files[0], f"{t.folder}/{t.files[0].key}"

    t, epub, epub_path = novel()
    pdf = next(f for f in epub.fallbacks if f.ext == "pdf")
    pdf_path = f"{t.folder}/{pdf.key}"
    (tmp_path / pdf_path).parent.mkdir(parents=True)
    (tmp_path / pdf_path).write_bytes(b"%PDF-1.7 Lantern Wolf")
    record = dl.Manifest(tmp_path, "humble")
    record.add({"path": pdf_path, "size": (tmp_path / pdf_path).stat().st_size, "md5": pdf.md5, "role": "book", "ext": "pdf",
                "instead_of": epub_path})  # downloaded instead of the EPUB
    curation.change_single(tmp_path, "format", "epub", [{"id": "h:novel"}], by=curation.COMMAND_LINE)
    sel = shopping.select("humble", [novel()[0]], record, tmp_path)  # the first choice, named: tried by the very next download
    assert places(sel) == [epub_path] and sel.substituted == 1
    curation.change_single(tmp_path, "format", "pdf", [{"id": "h:novel"}], by=curation.COMMAND_LINE)
    t, picked, path = novel()  # the PDF chosen: the one that came is the title's file, and the EPUB is no longer tried
    assert path == pdf_path and shopping.select("humble", [t], record, tmp_path).pending == []
    assert humble.summary([t], record, tmp_path)[1]["substitutes"] == []


def test_a_kind_you_choose_steers_a_humble_title_not_downloaded_yet_and_never_one_downloaded(tmp_path):
    """hylly set kind on a Humble title, by its catalog id (h: and its machine name, as the library page has it):
    not downloaded yet, it is downloaded as that kind, its file by that kind's list and read where it is, and --kind
    chooses it by it; downloaded, it keeps its format and nothing comes again (hylly set format still moves one)."""
    from hylly import curation

    def novel():
        return next(t for t in humble.titles_of(plan.for_library(ORDERS, tmp_path)) if t.key == "novel")

    t = novel()
    assert (t.kind, t.found, t.files[0].ext, t.device) == ("book", "book", "epub", "ereader")
    curation.change_single(tmp_path, "kind", "rpg", [{"id": "h:novel"}], by=curation.COMMAND_LINE)
    t, manifest = novel(), dl.Manifest(tmp_path, "humble")
    assert (t.kind, shopping.kind_found("humble", t), t.files[0].ext, t.device) == ("rpg", "rpg", "pdf", "tablet")
    job = shopping.make_job("humble", t, t.files[0], f"{t.folder}/{t.files[0].key}")
    assert (job.kind, job.device, job.filename) == ("rpg", "tablet", "novel.pdf")
    assert places(shopping.select("humble", [t], manifest, tmp_path, kinds={"rpg"})) == [job.path]
    assert shopping.select("humble", [t], manifest, tmp_path, kinds={"book"}).pending == []

    curation.change_single(tmp_path, "kind", "auto", [{"id": "h:novel"}], by=curation.COMMAND_LINE)
    t = novel()
    path = f"{t.folder}/{t.files[0].key}"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_bytes(b"PK\x03\x04 the novel")
    manifest.add({"path": path, "size": (tmp_path / path).stat().st_size, "md5": t.files[0].md5, "shop_id": "novel",
                  "item": "novel", "file": t.files[0].key, "role": "book", "ext": "epub"})  # downloaded as its EPUB
    curation.change_single(tmp_path, "kind", "rpg", [{"id": "h:novel"}], by=curation.COMMAND_LINE)
    t = novel()
    assert (t.kind, t.files[0].ext, t.device, t.planned.better) == ("rpg", "epub", "tablet", "pdf")  # kept as it is
    assert shopping.select("humble", [t], dl.Manifest(tmp_path, "humble"), tmp_path).pending == []


def standing_in(setup, tmp_path):
    """A title whose EPUB was gone at the shop, so its PDF stands in, and a downloader for the next run."""
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, lines = setup({"a.epub": epub, "a.pdf": pdf})
    master, alt = fallback_pair(epub, pdf)
    cdn.fail["/a.epub"] = [404] * 3
    assert d.run([master])["ok"][0][0] is alt
    cdn.requests.clear()
    later = dl.Downloader(tmp_path, dl.Manifest(tmp_path, "humble"), refresh=lambda job: job.url,
                          transport=httpx.MockTransport(cdn.handler), log=lines.append, backoff=0.0)
    return cdn, later, master, alt, lines


@pytest.mark.parametrize("trouble, before, asked, says, part_stays", [
    (["corrupt"] * 2, False, 1, "MD5 does not match the shop's", False),  # wrong once: thrown away, not fetched again
    (["corrupt"] * 2, True, 1, "a part from an earlier run, and no checksum vouches", False),  # nor fetched whole now
    ([went_down] * 9, True, 1, "the network is down", True),  # no ladder of retries: the part stays for the next run
    ([429] * 9, True, 1, "HTTP 429", True),
    ([404] * 3, True, 3, "link refused (HTTP 404) even after fetching a fresh one", False),  # the usual fresh links; gone
])
def test_while_a_substitute_stands_the_chosen_file_gets_one_try_a_run(setup, tmp_path, trouble, before, asked, says,
                                                                      part_stays):
    cdn, later, master, alt, _ = standing_in(setup, tmp_path)
    cdn.fail["/a.epub"] = list(trouble)
    part = tmp_path / (master.path + ".part")
    if before:
        part.write_bytes(blob(2000, 1)[:500])
    results = later.run([master])
    (job, note), = results["kept"]
    assert job is master and says in note and not results["failed"]
    assert [path for path, _ in cdn.requests] == ["/a.epub"] * asked
    assert part.exists() == part_stays
    assert not (tmp_path / master.path).exists()


def test_a_standing_substitute_is_counted_apart_from_the_files_done(tmp_path, monkeypatch, capsys):
    answer = lambda request: httpx.Response(404 if request.url.path.endswith(".epub") else 500)  # noqa: E731
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(monkeypatch, answer), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    remembered = []  # what hylly jobs, and a phone note, would tell of the run
    monkeypatch.setattr(cli.runs, "remember", lambda job, started, took, code, said: remembered.append((code, said)))
    lantern = next(j for j in humble_jobs(plan.build(LANTERN), tmp_path) if j.role == "book")
    pdf = lantern.fallbacks[0]
    (tmp_path / pdf.path).parent.mkdir(parents=True)
    (tmp_path / pdf.path).write_bytes(blob(3000, 2, "pdf"))  # the PDF that came before, instead of the gone EPUB
    dl.Manifest(tmp_path, "humble").add({"path": pdf.path, "size": 3000, "md5": pdf.md5, "role": "book", "ext": "pdf",
                                          "instead_of": lantern.path})
    cli.main(["humble", "download", "--library", str(tmp_path), "--json"])  # no failure: no exit code 1
    out = capsys.readouterr()
    said = json.loads(out.out)
    assert (said["done"], said["bytes_done"], said["failed"], said["kept_instead"], said["substituted"]) == (0, 0, 0, 1, 1)
    assert ("Run ended: 0 files done (0 bytes), 0 failed, 0 not finished, 1 chosen file didn't come, another format "
            "standing in.") in out.err
    assert "Lantern Wolf: EPUB: link refused (HTTP 404)" in out.err and " MB  Lantern Wolf" not in out.err  # no size: none came
    assert remembered == [(0, "0 files done, 0 failed. 1 chosen file didn't come, another format standing in.")]
    with (tmp_path / "state" / shops.SHOPS["humble"].failures).open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.DictReader(fh)) == []  # no failure row
    assert said["kinds"]["book"]["pending"]["files"] == 1 and said["substitutes"][0]["id"] == "h:lantern"


def test_a_real_run_ends_with_the_summary_of_the_library_as_it_leaves_it(tmp_path, monkeypatch, capsys):
    """The summary per kind comes at the end of a run, the files it downloaded done, and so it does for a run that
    failed: in its words and its --json, after its last line."""
    books = {"lantern.epub": blob(2000), "moths.epub": blob(3000, 2)}
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub(n[:-5], n[:-5].title(), "Moonwake Press", [("EPUB", n, len(d) / MIB, hashlib.md5(d).hexdigest())])
        for n, d in books.items()])})
    gone = set()
    serve = lambda request: httpx.Response(404) if request.url.path[1:] in gone else httpx.Response(200, content=books[request.url.path[1:]])  # noqa: E731
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(monkeypatch, serve), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    gone.add("moths.epub")  # one of the two fails
    with pytest.raises(SystemExit) as e:
        cli.main(["humble", "download", "--library", str(tmp_path), "--json"])
    out = capsys.readouterr()
    said = json.loads(out.out)
    assert e.value.code == exits.PARTLY and (said["done"], said["failed"]) == (1, 1)
    assert (said["kinds"]["book"]["done"]["files"], said["kinds"]["book"]["pending"]["files"]) == (1, 1)  # as it left it
    assert said["failed_last_time"] == 1  # the failures file this run wrote
    err = out.err.splitlines()
    ended = next(i for i, line in enumerate(err) if line.startswith("Run ended:"))
    header = next(i for i, line in enumerate(err) if line.split()[:2] == ["kind", "done"])
    assert ended < header and err[header + 1].split()[:4] == ["book", "1", "0.0", "GB"]
    gone.clear()
    cli.main(["humble", "download", "--library", str(tmp_path)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[-5].split()[:6] == ["book", "2", "0.0", "GB", "0", "0.0"] and "failed last time: 0" in lines[-1]


def test_a_429_on_each_of_five_standing_substitutes_does_not_stop_the_run(setup, tmp_path, monkeypatch):
    new = blob(1500, 7)
    files = {"new.epub": new, **{f"t{i}.epub": blob(2000, i + 1) for i in range(dl.REFUSED_IN_A_ROW)}}
    cdn, manifest, _, _ = setup(files)
    jobs = []
    for i in range(dl.REFUSED_IN_A_ROW):  # five titles whose EPUB is gone, a PDF of each standing in
        master = make_job(f"t{i}.epub", files[f"t{i}.epub"], folder=f"T{i}")
        alt = make_job(f"t{i}.pdf", blob(900, i + 1, "pdf"), folder=f"T{i}")
        alt.extra = {"instead_of": master.path}
        master.fallbacks = [alt]
        (tmp_path / alt.path).parent.mkdir(parents=True)
        (tmp_path / alt.path).write_bytes(blob(900, i + 1, "pdf"))
        manifest.add({"path": alt.path, "size": 900, "md5": alt.md5, "instead_of": master.path})
        cdn.fail[f"/t{i}.epub"] = [429]  # each told once to wait: no shop turning file after file away
        jobs.append(master)
    jobs.append(make_job("new.epub", new, folder="New"))  # after them in the queue, to be stopped if they blocked the run
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=httpx.MockTransport(cdn.handler),
                                                            backoff=0))
    ended = cli._run_jobs(tmp_path, manifest, jobs, refresh=lambda job: job.url, workers=1, failures="f.csv")  # no Exit: code 0
    assert (tmp_path / jobs[-1].path).read_bytes() == new and not ended.stopped
    assert ended.text == "1 file done, 0 failed. 5 chosen files didn't come, another format standing in."


def test_with_a_substitute_standing_any_failure_of_the_chosen_file_is_no_failure(setup, tmp_path):
    cdn, later, master, alt, _ = standing_in(setup, tmp_path)

    def odd(job):
        raise ValueError("an answer nobody expected")

    later.refresh = odd
    cdn.fail["/a.epub"] = [403]  # the link refused, and asking for a fresh one goes wrong in a way not foreseen
    results = later.run([master])
    (job, note), = results["kept"]
    assert job is master and "ValueError: an answer nobody expected" in note and not results["failed"]


def test_titles_with_a_substitute_come_after_every_other_pending_file(setup, tmp_path):
    _, later, master, _, _ = standing_in(setup, tmp_path)
    new = make_job("b.epub", blob(1000), folder="B")
    record = dl.Manifest(tmp_path, "humble")
    assert places(chosen([master, new], record, tmp_path)) == [new.path, master.path]  # though listed first
    sel = chosen([master, new], record, tmp_path, limit=1)
    assert places(sel) == [new.path] and sel.substituted == 0 and len(sel.standing) == 1  # --limit reaches the new title first
    assert places(chosen([master, new], record, tmp_path, max_gb=1000 / words.GB)) == [new.path]
    assert places(chosen([master, new], record, tmp_path, max_gb=0)) == [new.path]  # one file at least


def standing_titles(root, offers: dict[str, list], came: dict[str, str]):
    """The titles of a plan of titles that offer these files ((label, file name, MB) each), and for each title named in
    came the file that stands in for its chosen one, on the disk and recorded so."""
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press",
                          [sub(machine, f"Lantern Wolf {machine}", "Moonwake Press", files) for machine, files in offers.items()])}
    titles = humble.titles_of(plan.build(orders))
    record = dl.Manifest(root, "humble")
    for t in titles:
        for f in t.files[0].fallbacks:
            if f.name == came.get(t.key):
                path = f"{t.folder}/{f.key}"
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(b"%PDF-1.7 Lantern Wolf, by Ann Writer")
                record.add({"path": path, "size": (root / path).stat().st_size, "md5": f.md5,
                            "instead_of": f"{t.folder}/{t.files[0].key}"})
    return titles, record


def test_humble_status_offers_set_format_only_where_it_would_pick_the_substitute(tmp_path):
    from hylly import curation

    refused = next(x for x in ("djvu", "azw", "txt") if x not in curation.single("format"))  # one set format doesn't take
    pdfs = [("EPUB", "small.epub", 2), ("PDF", "small.pdf", 6), ("PDF (Small)", "small_small.pdf", 1)]
    titles, record = standing_titles(tmp_path, {
        "small": pdfs,  # the EPUB and the PDF gone, the small PDF stands in: set format pdf would pick the PDF again
        "fine": [("EPUB", "fine.epub", 2), ("PDF", "fine.pdf", 6), ("PDF (Small)", "fine_small.pdf", 1)],
        "odd": [("EPUB", "odd.epub", 2), (refused.upper(), f"odd.{refused}", 3)],
    }, {"small": "small_small.pdf", "fine": "fine.pdf", "odd": f"odd.{refused}"})
    lines, data = humble.summary(titles, record, tmp_path)
    assert {s["id"]: (s["can_keep"], s["instead_label"]) for s in data["substitutes"]} == {
        "h:small": (False, "PDF (Small)"), "h:fine": (True, "PDF"), "h:odd": (False, refused.upper())}
    report = "\n".join(lines)
    assert "another format in place of the chosen file: 3 titles" in report and "(1 of them): hylly set format" in report
    assert "2 of them can't be kept that way" in report and "goes on being tried once a run" in report
    only = "\n".join(humble.summary([t for t in titles if t.key == "small"], record, tmp_path)[0])
    assert "hylly set format FORMAT" not in only and "1 of them can't be kept that way" in only


def test_a_moment_of_missing_order_data_is_no_file_gone(setup, tmp_path):
    trouble = humble.ShopError("Humble Bundle made no download link (busy) this time. Running it again asks again.", 400)
    lib, (t,) = library_of(plan.build(LANTERN), tmp_path, Answers(LANTERN, links=[trouble]))  # none, then links
    master = shopping.make_job("humble", t, t.files[0], f"{t.folder}/{t.files[0].key}")
    files = {master.path: t.files[0], **{f"{t.folder}/{x.key}": x for x in t.files[0].fallbacks}}
    refresh = lambda job: lib.link(t, files[job.path])  # noqa: E731  (as the run asks the Library)
    cdn, manifest, d, _ = setup({"lantern.epub": blob(2000), "lantern.pdf": blob(3000, 2, "pdf")}, refresh=refresh)
    cdn.fail["/lantern.epub"] = [403]
    part = tmp_path / (master.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(blob(2000)[:500])
    (job, error), = d.run([master])["failed"]
    assert job is master and "made no download link" in error and "Running it again asks again" in error
    assert "/lantern.pdf" not in [path for path, _ in cdn.requests] and part.exists()  # no other format, and the part stays
    assert refresh(master).startswith("https://dl.humble.com/lantern.epub?")  # nothing empty was kept: asked again


def test_update_of_a_chosen_file_that_is_there_takes_no_old_substitute_for_it(setup, tmp_path):
    epub, pdf = blob(2000, 1), blob(3000, 2, "pdf")
    cdn, manifest, d, _ = setup({"a.epub": epub, "a.pdf": pdf})
    master, alt = fallback_pair(epub, pdf)
    for job, data, extra in ((master, b"PK\x03\x04 the old EPUB", {}), (alt, pdf, {"instead_of": master.path})):
        (tmp_path / job.path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / job.path).write_bytes(data)
        manifest.add({"path": job.path, "size": len(data), "md5": "an older version" if job is master else alt.md5, **extra})
    sel = chosen([master], manifest, tmp_path, update=True)  # the shop has a newer EPUB than the one here
    assert [(path, newer) for _, _, path, newer in sel.pending] == [(master.path, True)] and sel.substituted == 0
    assert not sel.standing  # one to be fetched again in its place has nothing standing in for it
    master.replace = True
    cdn.fail["/a.epub"] = [404] * 3
    results = d.run([master])
    (job, error), = results["failed"]  # a failed refetch is a failure, as for any file: the old EPUB stays
    assert job is master and "HTTP 404" in error and not results["kept"] and not results["ok"]
    assert [path for path, _ in cdn.requests] == ["/a.epub"] * 3 and (tmp_path / master.path).read_bytes().endswith(b"old EPUB")
    (job, note), = d.run([master])["ok"]  # and the new one comes as any new version: no "came at last"
    assert job is master and note is None and (tmp_path / master.path).read_bytes() == epub


def test_humble_s_jobs_carry_a_title_s_other_formats_as_fallbacks(tmp_path):
    jobs = {j.shop_id: j for j in humble_jobs(plan.build(ORDERS), tmp_path) if j.role == "book"}
    novel = jobs["novel"]
    assert novel.filename == "novel.epub"
    assert [(f.filename, f.device, f.extra) for f in novel.fallbacks] == [
        ("novel.mobi", "ereader", {"item": "novel", "file": "novel.mobi", "instead_of": novel.path}),
        ("novel.pdf", "tablet", {"item": "novel", "file": "novel.pdf", "instead_of": novel.path})]
    assert novel.extra == {"item": "novel", "file": "novel.epub"} and novel.exact  # named as every shop's records name them
    assert (novel.label, novel.ext, novel.bundle, novel.kind) == ("EPUB", "epub", "Humble Book Bundle: Fantasy by Lantern Press", "book")
    assert jobs["pss"].fallbacks[0].filename == "pss.mobi" and "pss_code.zip" not in [f.filename for f in jobs["pss"].fallbacks]
    assert jobs["con"].fallbacks == []  # one format only


# ---- Humble on the shops' core ---------------------------------------------------------------------------------

def test_a_humble_file_is_known_by_its_place_whatever_its_record_names(tmp_path):
    """Humble's download places its files itself (Title.folder, by the plan), so the core knows each by its place:
    a record without an item (a purchase Humble gave no id for) counts as done, as before, and so does one under another
    purchase of the title's (its downloaded copy is now one bought later, with more formats)."""
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("", "Lantern Notes", "Moonwake Press", [("PDF", "notes.pdf", 1)]),
        sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1)])])}
    titles = {t.name: t for t in humble.titles_of(plan.for_library(orders, tmp_path))}
    record = dl.Manifest(tmp_path, "humble")
    for t in titles.values():
        path = f"{t.folder}/{t.files[0].key}"
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(b"x" * t.files[0].size)
        record.add({"path": path, "shop_id": t.key, "size": t.files[0].size, "md5": t.files[0].md5, "role": "book",
                    "ext": t.files[0].ext, **({"item": t.key, "file": t.files[0].key} if t.key else {})})
    assert titles["Lantern Notes"].key == "" and "item" not in dl.Manifest(tmp_path, "humble").get(
        f"{titles['Lantern Notes'].folder}/notes.pdf")
    sel = shopping.select("humble", list(titles.values()), dl.Manifest(tmp_path, "humble"), tmp_path)
    assert (sel.pending, sel.done) == ([], 2)
    again = {"K2": order("K2", "2025-01-01T10:00:00", "Humble Book Bundle: Lantern Again", [  # bought again, more formats
        sub("lantern_again", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1), ("MOBI", "lantern.mobi", 2)])])}
    later = [t for t in humble.titles_of(plan.for_library({**orders, **again}, tmp_path)) if t.name == "Lantern Wolf"]
    assert [(t.key, t.folder) for t in later] == [("lantern_again", titles["Lantern Wolf"].folder)]  # the same folder
    sel = shopping.select("humble", later, dl.Manifest(tmp_path, "humble"), tmp_path)
    assert (sel.pending, sel.done) == ([], 1)  # its EPUB there: nothing to download again


def test_a_file_listed_without_an_md5_whose_stale_size_humble_corrects_is_the_same_file(tmp_path):
    """A file Humble listed with no MD5 and a size that wasn't the file's (its record keeps the size listed then, beside
    its own): when Humble lists the file's own size later, it is still the file there, so --update fetches nothing;
    another size again is a changed file."""
    data = blob(3000, kind="pdf")
    job = make_job("post2.pdf", data, with_md5=False)
    (tmp_path / job.path).parent.mkdir(parents=True)
    (tmp_path / job.path).write_bytes(data)
    record = dl.Manifest(tmp_path, "humble")
    record.add({"path": job.path, "size": len(data), "md5": hashlib.md5(data).hexdigest(), "listed_size": len(data) + 7})
    for listed, changed in ((len(data) + 7, 0), (len(data), 0), (len(data) + 99, 1)):  # as then, corrected, another
        job.size = listed
        sel = chosen([job], record, tmp_path, update=True)
        assert ([newer for _, _, _, newer in sel.pending], sel.done) == ([True] * changed, 1 - changed), listed


def test_a_humble_title_with_a_blank_bundle_is_recorded_with_none(tmp_path):
    """The record's bundle is the order's name as Humble gives it, blank too, as before the core: never who made it."""
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "   ", [sub("lantern", "Lantern Wolf", "Moonwake Press",
                                                                 [("EPUB", "lantern.epub", 1)])])}
    (job,) = humble_jobs(plan.build(orders), tmp_path)
    assert job.bundle == "" and job.folder.startswith("humble/")


def test_a_purchase_humble_gives_no_data_for_is_planned_as_saved_so_nothing_comes_again(tmp_path, monkeypatch, capsys):
    """Humble gives no data for purchase K1 this time. The run plans from the orders as saved, K1 as the newest
    snapshot has it: a title bought in K1 and again in K2 under another machine name keeps K1's folder, its file done
    there, and a title only in K1 stays done; nothing is fetched. A file of K1 still to download gets no link (Humble
    doesn't answer for K1), and fails, for the next run."""
    lantern, moths = blob(2000), blob(3000, 2)
    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
                  sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", len(lantern) / MIB, hashlib.md5(lantern).hexdigest())]),
                  sub("moths", "A Field Guide to Moths", "Moonwake Press", [("EPUB", "moths.epub", len(moths) / MIB, hashlib.md5(moths).hexdigest())])]),
              "K2": order("K2", "2025-01-01T10:00:00", "Humble Book Bundle: Lantern Again", [
                  sub("lantern_again", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", len(lantern) / MIB, hashlib.md5(lantern).hexdigest()),
                                                                          ("PDF", "lantern.pdf", 1)])])}
    answered = {"K1"}  # at first K1 alone: both titles downloaded from it
    humble_answering(monkeypatch, {"K1": orders["K1"]}, answered)
    cli.main(["humble", "download", "--limit", "0", "--library", str(tmp_path)])
    for t in plan.for_library({"K1": orders["K1"]}, tmp_path).titles:
        (tmp_path / t.path).parent.mkdir(parents=True, exist_ok=True)
        data = lantern if t.product.machine_name == "lantern" else moths
        (tmp_path / t.path).write_bytes(data)
        dl.Manifest(tmp_path, "humble").add({"path": t.path, "shop_id": t.product.machine_name, "item": t.product.machine_name,
                                              "file": t.path.rpartition("/")[2], "size": len(data), "md5": hashlib.md5(data).hexdigest(),
                                              "role": "book", "ext": "epub"})
    folders = {t.product.machine_name: t.folder for t in plan.for_library({"K1": orders["K1"]}, tmp_path).titles}
    humble_answering(monkeypatch, orders, {"K2"})  # now: bought again in K2, and no data for K1 this time
    monkeypatch.setattr(dl, "Downloader", lambda *a, **k: pytest.fail("nothing is fetched"))
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["humble", "download", "--library", str(tmp_path), "--json"])
    said = json.loads(capsys.readouterr().out)
    assert e.value.code == exits.PARTLY and said["no_data"] == 1
    assert said["to_download"]["files"] == 0 and said["already_done"] == 2  # both done where they are
    (t,) = [t for t in humble.titles_of(plan.for_library({**orders}, tmp_path)) if t.name == "Lantern Wolf"]
    assert (t.key, t.folder) == ("lantern_again", folders["lantern"])  # K1's folder, under K2's purchase
    lib, _ = library_of(plan.build(orders), tmp_path, Answers({"K2": orders["K2"]}))  # its titles, as the run had them
    lib.keys = ["K1", "K2"]
    titles = {t.key: t for t in lib.titles()}
    assert lib.missing == ["K1"] and "1 kept from the newest snapshot" in lib.unanswered
    assert titles["moths"].files[0].url == titles["lantern_again"].files[0].url == ""  # links come from link alone
    assert humble.entitlement(lib.saved["K1"], "moths") == "K1~moths"  # carried over with its item's entitlement id,
    with pytest.raises(humble.ShopError, match="made no download link"):  # by which its link is asked for: none now
        lib.link(titles["moths"], titles["moths"].files[0])


def test_a_file_of_a_purchase_carried_over_takes_its_link_from_another_purchase_humble_answered_for(tmp_path):
    """Humble gives no data for K1, the purchase a title's files come from (the one with the most formats), but answers
    for K2, where the same title was bought again with the very same EPUB (its MD5): the EPUB's link comes from K2's
    order, and its PDF, only in K1, asks K1's order again, which gives no data this time either: it fails for this run
    without another format tried in its place (no data says nothing of the file)."""
    from hylly import paths, snapshot

    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
                  sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1), ("PDF", "lantern.pdf", 2)])]),
              "K2": order("K2", "2025-01-01T10:00:00", "Humble Book Bundle: Lantern Again", [
                  sub("lantern_again", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1)])])}
    snapshot.save(orders, paths.state_dir(tmp_path), "humble")  # K1 as the snapshot before has it
    lib, _ = library_of(plan.build(orders), tmp_path, Answers({"K2": orders["K2"]}))
    lib.keys = ["K1", "K2"]
    (t,) = lib.titles()
    assert (t.key, t.order) == ("lantern", "K1") and lib.missing == ["K1"]
    epub, pdf = t.files[0], t.files[0].fallbacks[0]
    assert epub.url == pdf.url == ""  # an old snapshot's: no entitlement id, and no link
    assert (epub.borrow, pdf.borrow) == (("K2", "lantern_again", "lantern.epub"), ())
    assert lib.link(t, epub).startswith("https://dl.humble.com/lantern.epub?") and lib.client.asked == 1  # K2's item's
    with pytest.raises(humble.ShopError, match="gave no data for this purchase"):
        lib.link(t, pdf)
    assert lib.client.asked == 1  # nothing to ask for K1's PDF by: the next run reads K1 from the site


def carried(stale: str, epub: bytes, pdf: bytes, mobi: bytes) -> dict:
    """K1, the plan's purchase (three formats), its EPUB as last saved with another MD5 (stale) than K2's, where the
    same title was bought again with a newer EPUB of the same name and the same PDF."""
    h = lambda d: hashlib.md5(d).hexdigest()  # noqa: E731
    return {
        "K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
            sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", len(epub) / MIB, stale),
                                                               ("PDF", "lantern.pdf", len(pdf) / MIB, h(pdf)),
                                                               ("MOBI", "lantern.mobi", len(mobi) / MIB, h(mobi))])]),
        "K2": order("K2", "2025-01-01T10:00:00", "Humble Book Bundle: Lantern Again", [
            sub("lantern_again", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", len(epub) / MIB, h(epub)),
                                                                     ("PDF", "lantern.pdf", len(pdf) / MIB, h(pdf))])]),
    }


@pytest.mark.parametrize("etag", [True, False])
def test_a_carried_over_file_takes_no_link_of_another_file_of_the_same_name(tmp_path, monkeypatch, capsys, etag):
    """No data for K1's page this time; K2 lists another EPUB under the same name. K2's link is never taken for K1's
    EPUB: K1's own is asked for, by the entitlement id the list gave K1's item (the snapshot K1 is carried over from,
    made before Humble's site changed, has none). What comes by it is kept as K1's EPUB only where the CDN's checksum
    vouches for it (etag), as K1's stale listing can't. (With no entitlement id at all, see the test above.)"""
    from hylly import paths, snapshot

    epub, pdf, mobi = blob(2000, 3), blob(3000, 2, "pdf"), bytes(60) + b"BOOKMOBI" * 100
    orders = carried("0" * 32, epub, pdf, mobi)
    snapshot.save(orders, paths.state_dir(tmp_path), "humble")
    site = humble_answering(monkeypatch, orders, {"K2"})
    served, asked = {"lantern.epub": epub, "lantern.pdf": pdf, "lantern.mobi": mobi}, []

    def cdn(request):
        asked.append(request.url.path)
        data = served[request.url.path[1:]]
        return httpx.Response(200, content=data, headers={"etag": f'"{hashlib.md5(data).hexdigest()}"'} if etag else {})

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=httpx.MockTransport(cdn), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["humble", "download", "--library", str(tmp_path), "--json"])
    said = json.loads(capsys.readouterr().out)
    assert e.value.code == exits.PARTLY and said["no_data"] == 1 and asked[0] == "/lantern.epub"  # carried over, partly
    assert {x for x in site.asked if x[0] == "link"} == {("link", "K1~lantern")}  # by K1's own item, never K2's
    assert bool([p for p in dl.Manifest(tmp_path, "humble").entries if p.endswith("/lantern.epub")]) == etag


def test_a_carried_over_title_s_substitute_is_told_whether_set_format_could_keep_it(tmp_path):
    """can_keep goes by what the file is (its name, format, size and MD5, as the plan picks it), not by its link, which
    a purchase carried over doesn't have: its PDF standing in for its gone EPUB can be kept with hylly set format."""
    from hylly import paths, snapshot

    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1), ("PDF", "lantern.pdf", 2)])])}
    snapshot.save(orders, paths.state_dir(tmp_path), "humble")
    lib, _ = library_of(plan.build(orders), tmp_path, Answers({}))  # no data for K1 this time
    lib.keys = ["K1"]
    (t,) = lib.titles()
    pdf = t.files[0].fallbacks[0]
    assert pdf.url == ""
    path = f"{t.folder}/{pdf.key}"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_bytes(b"%PDF-1.7 Lantern Wolf")
    record = dl.Manifest(tmp_path, "humble")
    record.add({"path": path, "size": (tmp_path / path).stat().st_size, "md5": pdf.md5, "instead_of": f"{t.folder}/{t.files[0].key}"})
    (sub_,) = humble.summary([t], record, tmp_path)[1]["substitutes"]
    assert (sub_["instead_format"], sub_["can_keep"]) == ("pdf", True)


def test_a_summary_that_can_t_be_made_after_a_good_run_leaves_the_run_s_own_end(tmp_path, monkeypatch, capsys):
    """The summary is words on top of the run: when it can't be worked out (a share dropping out), the run says so in
    a line and ends as it went, exit 0, the run's record and its note as without it."""
    book = blob(2000)
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", len(book) / MIB, hashlib.md5(book).hexdigest())])])})
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(
        monkeypatch, lambda request: httpx.Response(200, content=book)), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    remembered = []
    monkeypatch.setattr(cli.runs, "remember", lambda job, started, took, code, said: remembered.append((code, said)))

    def gone_share(*a, **k):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(humble, "summary", gone_share)
    cli.main(["humble", "download", "--library", str(tmp_path)])  # no SystemExit: exit 0
    out = capsys.readouterr().out
    assert "The summary of the Humble library couldn't be made this time (OSError: [Errno 5] Input/output error)" in out
    assert remembered == [(0, "1 file done, 0 failed.")] and len(dl.Manifest(tmp_path, "humble").entries) == 1


def test_a_run_whose_library_went_away_gives_no_summary(tmp_path, monkeypatch, capsys):
    """The library gone mid-run (a disk unmounted): the run stops saying so, and gives no summary, which would count
    every file missing."""
    from hylly import paths

    books = {f"b{i}.epub": blob(2000 + i, i + 1) for i in range(3)}
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub(n[:-5], n[:-5].upper(), "Moonwake Press", [("EPUB", n, len(d) / MIB, hashlib.md5(d).hexdigest())]) for n, d in books.items()])})
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(
        monkeypatch, lambda request: httpx.Response(200, content=books[request.url.path[1:]])), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    real, cls, running = paths.library_missing, dl.Downloader.func, []
    real_run = cls.run

    def run(self, jobs):
        running.append(1)
        return real_run(self, jobs)

    monkeypatch.setattr(cls, "run", run)
    gone = lambda root, wait=None: bool(running) or real(root)  # noqa: E731 - gone once the run starts
    monkeypatch.setattr(paths, "library_missing", gone)
    with pytest.raises(SystemExit) as e:
        cli.main(["humble", "download", "--library", str(tmp_path), "--jobs", "1"])
    said = capsys.readouterr()
    assert e.value.code == exits.LOCAL and "isn't there any more" in said.err
    assert "recorded but missing" not in said.out + said.err and "kind " not in said.out


def test_humble_s_run_counts_what_is_done_past_its_limit_and_stops_at_max_gb(tmp_path):
    """One way for every shop: done, changed and left out are counted for every title chosen, past --limit too, so a
    run of five says how much of the library is done; --max-gb stops adding files where the next would pass it."""
    data = {n: blob(1000 * (i + 1), i + 1) for i, n in enumerate(("a.epub", "b.epub", "c.epub", "d.epub"))}
    jobs = [make_job(n, d, folder=n[0].upper()) for n, d in data.items()]
    record = dl.Manifest(tmp_path, "humble")
    for j in jobs[2:]:  # the last two downloaded
        (tmp_path / j.path).parent.mkdir(parents=True)
        (tmp_path / j.path).write_bytes(data[j.filename])
        record.add({"path": j.path, "size": j.size, "md5": j.md5})
    sel = chosen(jobs, record, tmp_path, limit=1)
    assert places(sel) == [jobs[0].path] and sel.done == 2 and len(sel.every) == 4  # done past the limit too
    assert [state for _, _, _, state in sel.every] == ["pending", "pending", "done", "done"]
    assert places(chosen(jobs, record, tmp_path, max_gb=2500 / words.GB)) == [jobs[0].path]  # 1000, then 2000 would pass
    assert places(chosen(jobs, record, tmp_path, max_gb=3000 / words.GB)) == [jobs[0].path, jobs[1].path]


def humble_orders(monkeypatch, orders: dict) -> None:
    """Humble's order API answering with these orders, for a dry run: it fetches no file."""
    monkeypatch.setitem(globals(), "LANTERN", orders)
    humble_shop(monkeypatch, lambda request: pytest.fail("a dry run fetches no file"))


def test_humble_s_dry_run_reads_the_orders_takes_its_own_options_and_says_why(tmp_path, monkeypatch, capsys):
    """hylly humble download --dry-run asks Humble as the run does, leaves nothing behind, takes every shop's
    options and Humble's own (--device, --bundle, --max-gb), and its --json says why each title is chosen so (plan.csv's
    why, before) and the summary per kind."""
    humble_orders(monkeypatch, {
        "K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
            sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1), ("PDF", "lantern.pdf", 2)])]),
        "K2": order("K2", "2024-02-01T10:00:00", "Humble Comics Bundle: Gloamlight", [
            sub("lw1", "Lantern Wolf Vol. 1", "Gloamlight Comics", [("CBZ", "lw1.cbz", 100)])])})
    dry = ["humble", "download", "--dry-run", "--library", str(tmp_path)]
    cli.main([*dry, "--json"])
    said = json.loads(capsys.readouterr().out)
    assert (said["titles"], said["files"], said["to_download"]["files"], said["snapshot"]) == (2, 2, 2, None)
    book, comic = said["pending"]  # books first, comics last
    assert (book["kind"], book["bytes"], book["why"]) == ("book", MIB, "EPUB preferred for text")
    assert comic["kind"] == "comic" and "CBZ" in comic["why"] and "tablet" not in book["why"]
    assert said["kinds"]["comic"]["pending"]["files"] == 1 and said["kinds"]["book"]["pending"]["files"] == 1
    assert not (tmp_path / "state" / "humble").exists() and dl.Manifest(tmp_path, "humble").entries == {}  # nothing left
    for options, wanted in ((["--device", "tablet"], ["lw1.cbz"]), (["--bundle", "moonwake"], ["lantern.epub"]),
                            (["--kind", "comic"], ["lw1.cbz"]), (["--max-gb", "0.01"], ["lantern.epub"]),
                            (["--title", "vol. 1"], ["lw1.cbz"]), (["--limit", "1"], ["lantern.epub"])):
        cli.main([*dry, "--json", *options])
        assert [p["path"].rpartition("/")[2] for p in json.loads(capsys.readouterr().out)["pending"]] == wanted, options
    cli.main(dry)
    out = capsys.readouterr().out
    assert "2 titles, 2 files. To download: 2 titles, 2 files, 105.9 MB. Already done: 0 files." in out
    lines = out.splitlines()  # the summary per kind last, after the files to download
    header = next(i for i, line in enumerate(lines) if line.split() == ["kind", "done", "pending", "changed", "on", "Humble"])
    assert [line.strip().rpartition("/")[2] for line in lines[1:header]] == ["lantern.epub", "lw1.cbz"]
    with pytest.raises(SystemExit) as refused:
        cli.main([*dry, "--device", "phone"])
    assert refused.value.code == exits.USAGE and "phone? Use ereader, tablet and either" in capsys.readouterr().err


def test_a_shop_that_turns_file_after_file_away_stops_the_run(setup):
    """Its words name every answer the run counts as turned away (download.REFUSED): a 401 still refused after the
    fresh links counts as a 403 does, and a 429 past the retries."""
    files = {f"f{i}.epub": blob(500, i + 1) for i in range(12)}
    cdn, _, d, lines = setup(files)
    unauthorised = lambda request, data: httpx.Response(401)  # noqa: E731
    for n, name in enumerate(files):
        cdn.fail[f"/{name}"] = [unauthorised if n % 2 else 403] * 10  # refused even with a fresh link: blocked
    d.workers = 1
    results = d.run([make_job(n, data) for n, data in files.items()])
    assert len(results["failed"]) == dl.REFUSED_IN_A_ROW and d.blocked == dl.turned_away()
    assert "(HTTP 401, 403 or 429)" in d.blocked
    assert len(cdn.requests) == dl.REFUSED_IN_A_ROW * (dl.LINK_REFRESHES + 1)  # not the whole queue at full speed
    assert len(results["stopped"]) == len(files) - dl.REFUSED_IN_A_ROW and not results["ok"]  # the rest: not finished


def test_a_run_stopped_early_counts_every_file_it_did_not_finish_or_start(setup, tmp_path, monkeypatch, capsys):
    """The downloads a stop cancelled before they started are not finished either, so a run stopped at the third
    of six files doesn't look nearly done: its end line, --json and how it ended for its watch (cli.Ended) count them
    with the stopped ones."""
    files = {f"f{i}.epub": blob(500, i + 1) for i in range(6)}
    cdn, manifest, _, _ = setup(files)
    jobs = [make_job(n, data) for n, data in files.items()]
    links = {j.path: j.url for j in jobs}

    def refresh(job):
        if job.filename == "f2.epub":
            raise dl.AuthError("The login has expired.")  # the third: the shop stops the run
        return links[job.path]

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=httpx.MockTransport(cdn.handler),
                                                            backoff=0))
    for j in jobs:
        j.url = ""  # each asks for its link: the third is refused
    with pytest.raises(dl.AuthError) as ended:
        cli._run_jobs(tmp_path, manifest, jobs, refresh=refresh, workers=1, failures="f.csv")
    said = capsys.readouterr().out
    assert "Run ended: 2 files done (1.0 KB), 0 failed, 4 not finished." in said
    assert ended.value.ended.stopped == 4 and len(manifest.entries) == 2


def test_a_ctrl_c_while_one_finished_file_is_said_loses_none_of_the_others_that_finished_with_it(setup, tmp_path, monkeypatch):
    """A Ctrl-C lands in the main thread wherever it is, here while the first of several files that finished in
    the same wait is said: every file is counted once all the same, those that came as done (and recorded), the rest as
    not finished."""
    files = {f"f{i}.epub": blob(500, i + 1) for i in range(8)}
    cdn, manifest, d, _ = setup(files)
    jobs = [make_job(n, data) for n, data in files.items()]
    real = dl.wait

    def together(fs, timeout=None, return_when=None):  # the first wait returns once three have finished
        deadline = time.monotonic() + 10
        while sum(f.done() for f in fs) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        return real(fs, timeout=timeout, return_when=return_when)

    said = []

    def log(line):
        said.append(line)
        if line.startswith("[1/"):
            raise KeyboardInterrupt  # as the first of them is said

    monkeypatch.setattr(dl, "wait", together)
    d.workers, d.log = 3, log
    results = d.run(jobs)
    counted = [j.path for key in ("ok", "failed", "stopped", "kept") for j, _ in results[key]]
    assert sorted(counted) == sorted(j.path for j in jobs)  # each once
    assert len(results["ok"]) == len(dl.Manifest(tmp_path, "humble").entries) >= 3  # every one that came, done
    assert any(line.startswith("Stopping:") for line in said)


def test_a_job_whose_own_error_ends_the_run_leaves_no_connection_open(setup, monkeypatch):
    """A job that fails in a way nothing foresaw (reading the records for its substitute, say) ends the run with that
    error, as before; the run's connections are closed all the same."""
    files = {f"f{i}.epub": blob(500, i + 1) for i in range(4)}
    _, _, d, _ = setup(files)
    real = dl.substitute

    def odd(job, manifest, root):
        if job.filename == "f1.epub":
            raise RuntimeError("an answer nobody expected")
        return real(job, manifest, root)

    monkeypatch.setattr(dl, "substitute", odd)
    d.workers = 1
    with pytest.raises(RuntimeError, match="an answer nobody expected"):
        d.run([make_job(n, data) for n, data in files.items()])
    assert d._clients and all(c.is_closed for c in d._clients)


def test_one_refused_file_among_good_ones_does_not_stop_the_run(setup):
    files = {f"f{i}.epub": blob(500, i + 1) for i in range(8)}
    cdn, _, d, _ = setup(files)
    for i in (1, 3, 5, 7):  # every other file: never five in a row
        cdn.fail[f"/f{i}.epub"] = [403] * 10
    d.workers = 1
    results = d.run([make_job(n, data) for n, data in files.items()])
    assert len(results["ok"]) == 4 and len(results["failed"]) == 4 and not d.blocked


def test_retry_after_pauses_every_download(setup, monkeypatch):
    data = blob(3000)
    cdn, _, d, lines = setup({"a.epub": data})
    cdn.fail["/a.epub"] = [lambda request, body: httpx.Response(429, headers={"retry-after": "7"})]
    waited, clock = [], [1000.0]
    monkeypatch.setattr(dl.time, "monotonic", lambda: clock[0])

    def wait(s=None):  # the pause passes as the clock moves on
        waited.append(s)
        clock[0] += s or 0
        return False

    monkeypatch.setattr(d.stop, "wait", wait)
    assert len(d.run([make_job("a.epub", data)])["ok"]) == 1
    assert sum(w for w in waited if w) >= 7 and any("wait 7 s" in line for line in lines)
    assert dl.retry_after(httpx.Response(429, headers={"retry-after": "120"})) == 120
    assert dl.retry_after(httpx.Response(429)) is None  # none: a caller that waits writes `or 0.0`


# ---- not the file: a web page, an empty answer -------------------------------------------------------------

SIGN_IN = b"<!DOCTYPE html><html><head><title>Sign in</title></head><body>Please sign in.</body></html>"
GOOD_STARTS = {  # how a real file of each kind starts: every kind the shops' files come in (formats.toml), and some more
    "epub": b"PK\x03\x04\x14\x00\x00\x08\x00\x00mimetypeapplication/epub+zip", "zip": b"PK\x03\x04\x14\x00",
    "cbz": b"PK\x03\x04\n\x00", "docx": b"PK\x03\x04\x14\x00\x06\x00", "xlsx": b"PK\x03\x04\x14\x00\x06\x00",
    "odt": b"PK\x03\x04\x14\x00\x00\x08\x00\x00mimetypeapplication/vnd.oasis", "ods": b"PK\x03\x04\x14\x00\x00\x08",
    "cbr": b"Rar!\x1a\x07\x00\xcf\x90s", "rar": b"Rar!\x1a\x07\x01\x00", "7z": b"7z\xbc\xaf\x27\x1c\x00\x04",
    "cb7": b"PK\x03\x04\x14\x00",  # a comic packed otherwise than named: readers open it all the same
    "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "djvu": b"AT&TFORM\x00\x00\x10\x00DJVMDIRM",
    "mobi": b"Lantern_Wolf".ljust(60, b"\x00") + b"BOOKMOBI", "azw3": b"Lantern_Wolf".ljust(60, b"\x00") + b"BOOKMOBI",
    "azw": b"Lantern_Wolf".ljust(60, b"\x00") + b"BOOKMOBI", "prc": b"Lantern_Wolf".ljust(60, b"\x00") + b"TEXtREAd",
    "jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "jpeg": b"\xff\xd8\xff\xe1\x00\x16Exif\x00", "png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    "gif": b"GIF89a\x10\x00", "webp": b"RIFF\x24\x08\x00\x00WEBPVP8 ", "rtf": b"{\\rtf1 \\ansi \\deff0",
    # no mark of their own: anything but a web page will do
    "txt": b"\xef\xbb\xbfLantern Wolf\r\nby Ann Writer\r\n", "md": b"# Lantern Wolf\n\n<!-- by Ann Writer -->\n",
    "fb2": b'<?xml version="1.0" encoding="utf-8"?>\n<FictionBook>\n<description>',
    "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00",  # a kind more, added to itch.io's list in a rules file of your own
    "html": SIGN_IN,  # a web page is all a web page can be
}


def like(kind: str) -> bytes:
    """A real-looking file of that kind: as such a file starts, and more after it."""
    return GOOD_STARTS[kind] + b" Lantern Wolf, by Ann Writer, from Moonwake Press." * 40


def answering(data: bytes, content_type: str, etag: str = ""):
    """What a link that ends at a sign-in page, or nowhere, brings: HTTP 200 all the same."""
    return lambda request, body=b"": httpx.Response(200, content=data, headers={"content-type": content_type,
                                                                               **({"etag": f'"{etag}"'} if etag else {})})


def test_a_web_page_or_an_empty_answer_is_never_kept_as_the_book(setup, tmp_path):
    page_md5, real = hashlib.md5(SIGN_IN).hexdigest(), blob(5000)
    unannounced = lambda request, body=b"": httpx.Response(200, content=iter([SIGN_IN]),  # noqa: E731
                                                            headers={"content-type": "application/octet-stream"})
    answers = {  # the file asked for: (the server's answer, what its failure says)
        "page.epub": (answering(SIGN_IN, "text/html; charset=utf-8", page_md5),
                      "a web page (text/html) came instead of the file, perhaps a sign-in page: nothing of it was kept"),
        "bare.epub": (answering(SIGN_IN, "text/html"), "a web page (text/html) came instead of the file"),
        "xhtml.epub": (answering(SIGN_IN, "application/xhtml+xml", "not-an-md5"), "a web page (application/xhtml+xml)"),
        "unlabelled.epub": (answering(SIGN_IN, "application/octet-stream", page_md5),
                            "what came is a web page, not the file: nothing of it was kept"),
        "unannounced.epub": (unannounced, "what came is a web page, not the file: nothing of it was kept"),  # no length
        "empty.epub": (answering(b"", "application/epub+zip", hashlib.md5(b"").hexdigest()),
                       "an empty answer came instead of the file: nothing of it was kept"),
    }
    cdn, manifest, d, _ = setup({name: real for name in answers})
    jobs = []
    for name, (answer, _) in answers.items():
        cdn.fail[f"/{name}"] = [answer] * 9
        job = make_job(name, real, with_md5=name.startswith(("page", "unannounced")))  # as Humble lists it, or StoryBundle
        job.exact = job.md5 is not None
        jobs.append(job)
    results = d.run(jobs)
    assert not results["ok"] and len(results["failed"]) == len(answers)
    for job, error in results["failed"]:
        assert answers[job.filename][1] in error, (job.filename, error)
        assert not (tmp_path / job.path).exists() and not (tmp_path / (job.path + ".part")).exists()  # nothing of it kept
    assert sorted(path for path, _ in cdn.requests) == sorted(f"/{name}" for name in answers)  # each asked once: no use again
    assert dl.Manifest(tmp_path, "humble").entries == {}  # never recorded as good: the next run tries them again
    assert len(chosen(jobs, dl.Manifest(tmp_path, "humble"), tmp_path).pending) == len(jobs)


def test_a_resumed_download_keeps_its_part_when_a_page_or_nothing_comes_back(setup, tmp_path):
    real = blob(5000)
    answers = {"page.epub": (answering(SIGN_IN, "text/html"), "a web page (text/html) came instead of the rest of the file"),
               "empty.epub": (answering(b"", "application/epub+zip"), "an empty answer came instead of the file")}
    cdn, manifest, d, _ = setup({name: real for name in answers})
    jobs = [make_job(name, real) for name in answers]
    for job in jobs:
        cdn.fail[f"/{job.filename}"] = [answers[job.filename][0]] * 9
        (tmp_path / job.path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / (job.path + ".part")).write_bytes(real[:2000])  # an earlier run's, to go on from
    results = d.run(jobs)
    assert not results["ok"] and manifest.entries == {}
    for job, error in results["failed"]:
        assert answers[job.filename][1] in error, error
        assert (tmp_path / (job.path + ".part")).read_bytes() == real[:2000]  # nothing of the answer went into it
    assert sorted(cdn.requests) == [("/empty.epub", "bytes=2000-"), ("/page.epub", "bytes=2000-")]


def test_a_file_the_shop_s_md5_vouches_for_is_kept_whatever_the_server_calls_it(setup, tmp_path):
    real = blob(5000)
    cdn, manifest, d, _ = setup({"vouched.epub": real, "unvouched.epub": real})
    for name in ("vouched.epub", "unvouched.epub"):
        cdn.fail[f"/{name}"] = [answering(real, "text/html", hashlib.md5(real).hexdigest())] * 9
    vouched, unvouched = make_job("vouched.epub", real), stale(make_job("unvouched.epub", real), blob(4000))
    results = d.run([vouched, unvouched])
    assert [j for j, _ in results["ok"]] == [vouched] and manifest.get(vouched.path)["md5_source"] == "shop"
    assert (tmp_path / vouched.path).read_bytes() == real
    assert results["failed"] == [(unvouched, "a web page (text/html) came instead of the file, perhaps a sign-in page: "
                                             "nothing of it was kept")]  # the server's ETag vouches only for what it sent


def test_a_page_left_in_a_part_by_an_earlier_run_is_fetched_again_whole(setup, tmp_path):
    real = blob(5000)
    cdn, manifest, d, _ = setup({"a.epub": real})
    job = make_job("a.epub", real)
    (tmp_path / job.path).parent.mkdir(parents=True)
    (tmp_path / (job.path + ".part")).write_bytes(SIGN_IN)
    assert len(d.run([job])["ok"]) == 1 and (tmp_path / job.path).read_bytes() == real
    assert cdn.requests == [("/a.epub", f"bytes={len(SIGN_IN)}-"), ("/a.epub", None)]


OTHERWISE = {  # a file whose name says one kind and whose bytes another of its sort, as what opens it goes by the bytes
    "png-as.jpg": GOOD_STARTS["png"], "jpeg-as.png": GOOD_STARTS["jpg"], "webp-as.gif": GOOD_STARTS["webp"],
    "rar-as.zip": GOOD_STARTS["rar"], "zip-as.rar": GOOD_STARTS["zip"], "7z-as.zip": GOOD_STARTS["7z"],
    "zip-as.7z": GOOD_STARTS["zip"], "old.docx": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "old.xlsx": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    "bom.rtf": b"\xef\xbb\xbf{\\rtf1 \\ansi", "blank.rtf": b"\r\n  {\\rtf1 \\ansi",
}


def test_files_of_every_kind_the_shops_sell_still_pass_and_a_page_in_their_place_does_not(setup):
    from hylly import rules

    assert set(rules.current().formats) | rules.current().itchio_fetch <= set(GOOD_STARTS)  # every kind, as shipped
    good = {f"good.{kind}": like(kind) for kind in GOOD_STARTS} | {name: start + like("txt") for name, start in OTHERWISE.items()}
    files = good | {f"page.{kind}": SIGN_IN for kind in GOOD_STARTS if kind != "html"}  # a page without a word that it is one
    files |= {f"other.{kind}": b"\x00" * 2000 for kind in dl.FIRST_BYTES}  # starting as no file of its kind does
    cdn, manifest, d, _ = setup(files)
    results = d.run([make_job(name, data, with_md5=False) for name, data in files.items()])  # no MD5 vouches for any
    assert sorted(j.filename for j, _ in results["ok"]) == sorted(good)
    assert all(manifest.get(j.path)["md5_source"] == "transfer" for j, _ in results["ok"])
    assert len(results["failed"]) == len(files) - len(good)
    for job, error in results["failed"]:
        assert error == ("what came is a web page, not the file: nothing of it was kept" if job.filename.startswith("page.")
                         else f"what came doesn't start as {job.ext.upper()} files do, twice"), job.filename


def test_a_file_far_smaller_than_listed_is_not_the_file_unless_the_shop_s_md5_says_so(setup):
    small = blob(3000, kind="pdf")
    cdn, _, d, _ = setup({"small.pdf": small, "vouched.pdf": small, "newer.pdf": small})
    for name in ("small.pdf", "vouched.pdf", "newer.pdf"):
        cdn.etags[f"/{name}"] = hashlib.md5(small).hexdigest()  # the server's checksum vouches for what it sent
    listed_bigger = stale(make_job("small.pdf", small), blob(40000, kind="pdf"))  # more than ten times what came
    vouched = make_job("vouched.pdf", small)
    vouched.size = 40000  # ... but the shop's own MD5 is this file's
    newer = stale(make_job("newer.pdf", small), blob(20000, kind="pdf"))  # a newer, smaller file than Humble lists
    results = d.run([listed_bigger, vouched, newer])
    assert sorted(j.filename for j, _ in results["ok"]) == ["newer.pdf", "vouched.pdf"]
    assert results["failed"] == [(listed_bigger, "3000 bytes, where the shop lists 40000: too small to be the file, twice")]


def test_an_empty_file_is_kept_only_when_the_shop_lists_one(setup, tmp_path):
    cdn, manifest, d, _ = setup({"empty.txt": b"", "asked.txt": b"Lantern Wolf"})
    cdn.fail["/asked.txt"] = [answering(b"", "text/plain")] * 2
    empty, asked = make_job("empty.txt", b""), make_job("asked.txt", b"Lantern Wolf")
    results = d.run([empty, asked])
    assert [j for j, _ in results["ok"]] == [empty] and (tmp_path / empty.path).read_bytes() == b""
    assert [j for j, _ in results["failed"]] == [asked] and manifest.get(asked.path) is None


LANTERN = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
    sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1), ("PDF", "lantern.pdf", 2)])])}


def test_humble_s_dry_run_and_run_name_the_extras_the_never_list_leaves_out(tmp_path, monkeypatch, capsys):
    """The never-list for Humble: a program among a title's extras (a map maker, say) is left out by its dry run, which
    reads the orders as the run does, and by its run, and each names it; the title's book comes as ever."""
    book = blob(20000)
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("atlas", "Atlas of Moonwake", "Moonwake Press", [("EPUB", "atlas.epub", len(book) / MIB, hashlib.md5(book).hexdigest()),
                                                              ("Map tool", "Map Maker.exe", 0.01)])])})
    named = "  'Atlas of Moonwake': 'Map Maker.exe' (EXE, a program, script or shortcut)\n"
    asked = []
    serve = lambda request: asked.append(request.url.path) or httpx.Response(200, content=book)  # noqa: E731
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(monkeypatch, serve), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert "To download: 1 title, 1 file" in out and named in out and "Map Maker" not in out.replace(named, "")
    assert asked == [] and not (tmp_path / "state" / "humble").exists()  # nothing fetched, and no snapshot
    cli.main(["humble", "download", "--library", str(tmp_path)])
    assert named in capsys.readouterr().out and asked == ["/atlas.epub"]
    assert named.strip() in (tmp_path / "state" / "download-log.txt").read_text(encoding="utf-8")
    assert [e["path"].rpartition("/")[2] for e in dl.Manifest(tmp_path, "humble").entries.values()] == ["atlas.epub"]


MAP_TOOL, ATLAS_BOOK = b"MZ map maker, made up" * 195 + b"x" * 1, b"PK\x03\x04" + bytes(8188)  # 4096 and 8192 bytes


def atlas_with_a_program() -> dict:
    """Humble's order of a title with a program among its extras, the sizes in MB as Humble lists them exactly."""
    return {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("atlas", "Atlas of Moonwake", "Moonwake Press", [
            ("EPUB", "atlas.epub", len(ATLAS_BOOK) / MIB, hashlib.md5(ATLAS_BOOK).hexdigest()),
            ("Map tool", "Map Maker.exe", len(MAP_TOOL) / MIB, hashlib.md5(MAP_TOOL).hexdigest())])])}


def test_humble_s_summary_counts_no_program_among_the_extras_as_pending(tmp_path, monkeypatch, capsys):
    """A Humble extra of a kind no download takes is never pending, in the summary of hylly humble download and
    its --json (hylly humble status's before), nor a file no longer chosen should an earlier version have
    downloaded it; the title's book counts as ever."""
    assert len(MAP_TOOL) == 4096 and len(ATLAS_BOOK) == 8192
    monkeypatch.setitem(globals(), "LANTERN", atlas_with_a_program())
    humble_shop(monkeypatch, lambda request: pytest.fail("a dry run fetches no file"))
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path), "--json"])
    kinds = json.loads(capsys.readouterr().out)["kinds"]["book"]
    assert (kinds["pending"]["files"], kinds["pending"]["bytes"], kinds["done"]["files"]) == (1, len(ATLAS_BOOK), 0)
    (t,) = humble.titles_of(plan.for_library(atlas_with_a_program(), tmp_path))
    tool = next(f for f in t.files if f.name == "Map Maker.exe")
    path = f"{t.folder}/{tool.key}"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_bytes(MAP_TOOL)  # as an earlier version downloaded it
    dl.Manifest(tmp_path, "humble").add({"path": path, "shop_id": "atlas", "role": "extra", "ext": "exe", "size": len(MAP_TOOL),
                                          "md5": tool.md5})
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert "book        0      0.0 GB      1      0.0 GB" in out and "downloaded but no longer chosen: 0" in out


def test_humble_seed_copies_no_program_in_and_names_it(tmp_path, capsys):
    """hylly humble seed takes no file of a kind no download takes into the library, though the plan lists it as
    an extra and the old folder has the very file; it names it, as the downloads do, and copies the book as ever."""
    from hylly import paths, snapshot

    root, old = tmp_path / "library", tmp_path / "old books"
    old.mkdir()
    (old / "atlas.epub").write_bytes(ATLAS_BOOK)
    (old / "Map Maker.exe").write_bytes(MAP_TOOL)
    snapshot.save(atlas_with_a_program(), paths.state_dir(root), "humble")
    cli.main(["humble", "seed", str(old), "--library", str(root)])
    out = capsys.readouterr().out
    assert "copied: 1 file" in out and "not chosen: 1" in out
    assert "Left out, not failures: 1 file of a kind no download takes (programs, scripts and shortcuts):\n" \
           "  'Atlas of Moonwake': 'Map Maker.exe' (EXE, a program, script or shortcut)\n" in out
    assert [p.name for p in root.rglob("*") if p.is_file() and p.relative_to(root).parts[0] == "humble"] == ["atlas.epub"]
    assert (old / "Map Maker.exe").read_bytes() == MAP_TOOL  # the folder is only read


def humble_shop(monkeypatch, answer) -> httpx.MockTransport:
    site = HumbleSite(LANTERN, answer)  # its site, as its library page reads it; its files from answer
    monkeypatch.setattr(humble, "HumbleClient", site.client)  # the download's (humble.Library's), and the login's check
    monkeypatch.setattr(humble, "PAUSE", 0)
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", "c" * 80)
    return httpx.MockTransport(site.handler)


def storybundle_shop(monkeypatch, answer) -> httpx.MockTransport:
    site = StoryBundleSite()
    transport = httpx.MockTransport(lambda request: answer(request) if request.url.host.endswith("amazonaws.com") else site.handler(request))
    real = sbm.StoryBundle
    monkeypatch.setattr(sbm, "StoryBundle", lambda session: real(session, transport=transport, delay=0))
    monkeypatch.setenv("HYLLY_STORYBUNDLE_SESSION", "c" * 80)
    return transport


def drivethrurpg_shop(monkeypatch, answer) -> httpx.MockTransport:
    api = DriveThruRPGAPI()
    transport = httpx.MockTransport(lambda request: answer(request) if request.url.host == "files.example.com" else api.handler(request))
    real = dtm.DriveThruRPG
    monkeypatch.setattr(dtm, "DriveThruRPG", lambda key: real(key, transport=transport, delay=0, wait=0, backoff=0))
    monkeypatch.setenv("HYLLY_DRIVETHRURPG_KEY", DRIVETHRURPG_KEY)
    return transport


def itchio_shop(monkeypatch, answer) -> httpx.MockTransport:
    api = FakeItch()
    transport = httpx.MockTransport(lambda request: answer(request) if request.url.host == ITCH_FILES else api.handler(request))
    real = itchio.Itch
    monkeypatch.setattr(itchio, "Itch", lambda key: real(key, transport=transport, delay=0, sleep=lambda s: None))
    monkeypatch.setenv("HYLLY_ITCHIO_KEY", ITCHIO_KEY)
    return transport


STAND_INS = {"humble": humble_shop, "storybundle": storybundle_shop, "drivethrurpg": drivethrurpg_shop, "itchio": itchio_shop}


def test_every_shop_has_a_stand_in_here():
    """The tests that run every shop's download against made-up answers (STAND_INS) reach every shop in the table: a
    new shop isn't tested by them until it has one (docs/downloads.md, Writing a shop), and the run that downloads
    needs its fake's good answers (GOOD_ANSWERS) and the files a first run brings (EVERY_SHOP_S_FILES) as well."""
    assert set(STAND_INS) == set(shops.SHOPS)
    for table in ("GOOD_ANSWERS", "EVERY_SHOP_S_FILES"):
        missing = set(STAND_INS) - set(globals()[table])
        assert not missing, f"{table} in tests/test_download.py has no row for {', '.join(sorted(missing))}: give it one"

LANTERN_BOOK, LANTERN_PDF = like("epub"), like("pdf")  # Humble's files for the run that downloads: as listed, to the byte


def humble_answers(monkeypatch):
    """Humble's: a purchase whose sizes and checksums are those of its files, and those files."""
    files = {"/lantern.epub": LANTERN_BOOK, "/lantern.pdf": LANTERN_PDF}
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("lantern", "Lantern Wolf", "Moonwake Press", [(kind, name.lstrip("/"), len(data) / MIB, hashlib.md5(data).hexdigest())
                                                          for kind, (name, data) in zip(("EPUB", "PDF"), files.items())])])})
    return lambda request: httpx.Response(200, content=files[request.url.path])


GOOD_ANSWERS = {  # what each shop's file host answers when all is well: each fake's own files, Humble's made to match
    "humble": humble_answers, "storybundle": lambda monkeypatch: StoryBundleSite().handler,
    "drivethrurpg": lambda monkeypatch: DriveThruRPGAPI().handler, "itchio": lambda monkeypatch: FakeItch().handler,
}


def good_answers(shop: str, monkeypatch):
    """What a shop's file host answers when all is well (GOOD_ANSWERS), and a word on which table wants a new shop."""
    for table in (GOOD_ANSWERS, EVERY_SHOP_S_FILES):
        name = next(n for n, t in globals().items() if t is table)
        assert shop in table, f"{name} in tests/test_download.py has no row for {shop}: give it one (docs/downloads.md)"
    return GOOD_ANSWERS[shop](monkeypatch)


EVERY_SHOP_S_FILES = {  # what a first run to an empty library brings from each shop's fake, by name
    "humble": ["lantern.epub"],  # the e-reader's format: the PDF only when the EPUB fails
    "storybundle": ["Day_of_the_Lantern.epub", "Hexes_and_Charms.pdf", "Opening_the_Box_-_Ann_Writer.epub"],  # the one in
    # both bundles, once
    "drivethrurpg": ["Crew Sheet.pdf", "Referee Rulebook.pdf", "Referee Screen.pdf"],
    "itchio": ["guide.epub", "guide.pdf", "hexcrawl.pdf", "issue1.cbz", "maps.zip", "rules.pdf"],
}


@pytest.mark.parametrize("shop", STAND_INS)
def test_every_shop_s_download_command_fetches_its_files_records_them_and_ends_well(shop, tmp_path, monkeypatch, capsys):
    """hylly SHOP download from start to end, as a person runs it: the shop's own client and the downloader, with the
    fakes as the shops and their file hosts. Its files land whole, each recorded with its size and checksum, nothing
    fails, and the command ends with 0; a second run finds them all done and fetches nothing."""
    fetched, answer = [], good_answers(shop, monkeypatch)

    def files(request):
        fetched.append(request.url.path)
        return answer(request)

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=STAND_INS[shop](monkeypatch, files), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)  # the room on this computer's disk: a test of its own
    for run in ("first", "again"):
        try:
            cli.main([shop, "download", "--library", str(tmp_path)])
        except SystemExit as e:
            assert e.code == exits.OK, (run, capsys.readouterr())
        if run == "first":
            asked = len(fetched)
    assert len(fetched) == asked == len(EVERY_SHOP_S_FILES[shop])  # once each, and nothing the second time
    entries = list(dl.Manifest(tmp_path, shop).entries.values())
    assert sorted(e["path"].rpartition("/")[2] for e in entries) == EVERY_SHOP_S_FILES[shop]
    for e in entries:
        data = (tmp_path / e["path"]).read_bytes()
        assert e["size"] == len(data) and e["md5"] == hashlib.md5(data).hexdigest() and e["md5_ok"] is True, e
        assert e["path"].startswith(f"{shop}/") and e["title"] and e["shop_id"], e  # in its shop's folder, by its title
    on_disk = [p for p in (tmp_path / shop).rglob("*") if p.is_file()]
    assert len(on_disk) == len(entries) and not [p for p in on_disk if p.suffix == ".part"]  # no part left behind
    failures = tmp_path / "state" / shops.SHOPS[shop].failures
    assert not failures.exists() or list(csv.DictReader(failures.open(encoding="utf-8-sig", newline=""))) == []
    assert "0 failed" in (tmp_path / "state" / "download-log.txt").read_text(encoding="utf-8")


def test_humble_s_download_command_asks_for_a_fresh_link_when_one_has_expired(tmp_path, monkeypatch):
    """Humble's links live a while only: one that answers 403 is asked for again through the command's own refresh
    (humble.Library.link, which asks for its item's links once more, as the one it gave was refused), and the file comes
    by the fresh one."""
    answer, links = good_answers("humble", monkeypatch), []

    def expiring(request):
        links.append(str(request.url))
        return httpx.Response(403) if len(links) == 1 else answer(request)

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(monkeypatch, expiring), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    cli.main(["humble", "download", "--library", str(tmp_path)])
    (entry,) = dl.Manifest(tmp_path, "humble").entries.values()
    assert links == ["https://dl.humble.com/lantern.epub?t=fresh1", "https://dl.humble.com/lantern.epub?t=fresh2"]
    assert (tmp_path / entry["path"]).read_bytes() == LANTERN_BOOK  # by the second set of links its item was given


def test_a_download_needs_the_room_it_will_take_and_a_gigabyte_more(tmp_path, monkeypatch):
    """_check_space, before any file is fetched: what the files take and 1.1 GB besides for the disk's own needs (2**30
    bytes, as it always was), or code 6 with what's needed and what's free, in a disk's GB (words.size_words); a library
    folder not made yet is made to be asked."""
    from types import SimpleNamespace

    root, free = tmp_path / "library", {"bytes": 5 * 2**30}
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda where: SimpleNamespace(total=10 * 2**30, used=0, free=free["bytes"]))
    cli._check_space(root, 4 * 2**30)  # 4 and the 1 more, in 1,024s: just fits
    assert root.is_dir()
    with pytest.raises(exits.Exit) as e:
        cli._check_space(root, 4 * 2**30 + 1)
    assert e.value.code == exits.LOCAL and "Not enough space: 4.3 GB needed, 5.4 GB free" in e.value.message
    free["bytes"] = 0
    with pytest.raises(exits.Exit):
        cli._check_space(root, 0)  # a full disk takes not even a small file


WRONG_ANSWERS = {  # each with HTTP 200: (the answer, what each failure says)
    "a sign-in page with an ETag": (answering(SIGN_IN, "text/html; charset=utf-8", hashlib.md5(SIGN_IN).hexdigest()), "a web page"),
    "a sign-in page without one": (answering(SIGN_IN, "text/html"), "a web page"),
    "a sign-in page that doesn't say it is one": (answering(SIGN_IN, "application/octet-stream", hashlib.md5(SIGN_IN).hexdigest()),
                                                  "what came is a web page"),
    "an empty answer": (answering(b"", "binary/octet-stream", hashlib.md5(b"").hexdigest()), "an empty answer"),
}


@pytest.mark.parametrize("answer", WRONG_ANSWERS)
@pytest.mark.parametrize("shop", STAND_INS)
def test_no_shop_s_download_keeps_a_sign_in_page_or_an_empty_answer(shop, answer, tmp_path, monkeypatch):
    stand_in, failures = STAND_INS[shop], shops.SHOPS[shop].failures  # Humble's too: humble-failures.csv
    respond, says = WRONG_ANSWERS[answer]
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=stand_in(monkeypatch, respond), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)  # the room on this computer's disk is no part of it
    with pytest.raises(SystemExit) as e:
        cli.main([shop, "download", "--library", str(tmp_path)])
    assert e.value.code == exits.PARTLY  # files failed
    assert dl.Manifest(tmp_path, shop).entries == {}  # none of them recorded as good
    assert not [p for p in (tmp_path / shop).rglob("*") if p.is_file()]  # nor kept, not even as a part
    with (tmp_path / "state" / failures).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and all(says in row["error"] for row in rows), rows  # listed, for the next run to try again


@pytest.mark.parametrize("shop", STAND_INS)
def test_at_every_shop_a_full_disk_ends_the_run_with_exit_code_6_and_no_file_failed(shop, tmp_path, monkeypatch, capsys):
    stand_in, failures = STAND_INS[shop], shops.SHOPS[shop].failures
    asked = []

    def full(request):
        asked.append(request.url.path)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=stand_in(monkeypatch, full), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)  # the room on this computer's disk is no part of it
    with pytest.raises(SystemExit) as e:
        cli.main([shop, "download", "--library", str(tmp_path), "--jobs", "1"])
    assert e.value.code == exits.LOCAL and len(asked) == 1  # one file met it: the shop isn't asked for the rest
    assert "The library's disk is full, so the run stopped" in capsys.readouterr().err
    with (tmp_path / "state" / failures).open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.DictReader(fh)) == []  # not failures: each is tried again by the next run


# ---- in the database -------------------------------------------------------------------------------------

def test_every_record_is_kept_in_order_and_the_latest_of_a_path_counts(tmp_path):
    from hylly import db, manifest

    for shop in shops.SHOPS:
        m = dl.Manifest(tmp_path, shop)
        for entry in ({"path": f"{shop}/A/a.epub", "shop_id": "a", "size": 1, "md5": "1", "title": "Ä title"},
                      {"path": f"{shop}/B/b.pdf", "shop_id": "b", "size": 2, "md5": None, "extra": {"x": [1, 2.5, True]}},
                      {"path": f"{shop}/A/a.epub", "shop_id": "a", "size": 3, "md5": "3", "instead_of": f"{shop}/A/a.pdf"}):
            m.add(entry)
        then = dl.Manifest(tmp_path, shop).entries  # read again, as the next command does
        assert list(then) == [f"{shop}/A/a.epub", f"{shop}/B/b.pdf"] and then[f"{shop}/A/a.epub"]["size"] == 3  # the latest counts
        assert then[f"{shop}/B/b.pdf"]["extra"] == {"x": [1, 2.5, True]}  # as it went in
    copies = [{"src": "D:/Books/a.epub", "dest": "own/A/a.epub", "size": 1, "md5": "1", "copied_at": "2026-10-01T10:00:00"},
              {"src": "D:/Books/a.epub", "dest": "own/a/A.EPUB", "size": 1, "md5": "1", "copied_at": "2026-10-02T10:00:00", "already_there": True}]
    for entry in copies:
        manifest.add_copy(tmp_path, entry)
    assert manifest.copies(tmp_path) == {"own/a/a.epub": copies[1]}  # by its place, whatever its case: the latest counts
    with db.database().read() as c:  # every record kept, in order, and SQL sees the latest of each path
        assert [json.loads(r[0])["size"] for r in c.execute("SELECT body FROM download WHERE shop = 'itchio' ORDER BY seq")] == [1, 2, 3]
        assert c.execute("SELECT size FROM downloaded WHERE shop = 'humble' AND path = 'humble/A/a.epub'").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM downloaded").fetchone()[0] == 2 * len(shops.SHOPS)


def test_downloads_are_recorded_only_where_the_librarys_records_are_kept(tmp_path, monkeypatch, tmp_path_factory):
    from hylly import cli, db, manifest

    dl.Manifest(tmp_path, "humble").add({"path": "humble/A/a.epub", "size": 1})
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path_factory.mktemp("another-computer")))  # another computer's settings
    db.close_all()
    with pytest.raises(db.NotHere, match="what was downloaded"):
        dl.Manifest(tmp_path, "humble")
    with pytest.raises(db.NotHere):
        manifest.copies(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["verify", "--library", str(tmp_path)])


def test_records_that_lead_out_of_the_library_are_left_out(tmp_path, capsys):
    """What the walled server could write into the database: records naming a place outside the library. No command
    goes by them, and a link inside the library that leads out isn't opened either."""
    import os

    from hylly import db, manifest, paths

    windows_only = ("comics/Ä: name/b.pdf", "comics/Lantern\\Wolf.pdf")  # on Windows a drive or a stream, another PC's share
    for rel in ("humble/A/a.epub", *(windows_only if os.name != "nt" else ["comics/Ä name/b.pdf"])):
        assert paths.inside_library(rel), rel
    for rel in ("../outside.pdf", "/etc/passwd", "humble/../../x.pdf", "", None, 5, "a//b.pdf", "a/./b.pdf", "a\x00b",
                *(windows_only if os.name == "nt" else [])):
        assert not paths.inside_library(rel), rel
    m = dl.Manifest(tmp_path, "humble")
    m.add({"path": "humble/A/a.epub", "size": 1})
    with db.database().write() as c:
        for bad in ({"path": "../outside.pdf", "size": 1}, {"path": "/etc/passwd", "size": 1}, {"path": "humble/B/b.epub", "size": "1"}):
            c.execute("INSERT INTO download (shop, body) VALUES ('humble', ?)", (db.dump(bad),))
        c.execute("INSERT INTO copied (body) VALUES (?)", (db.dump({"dest": "../../.ssh/authorized_keys", "size": 1}),))
    assert list(dl.Manifest(tmp_path, "humble").entries) == ["humble/A/a.epub"]
    assert manifest.copies(tmp_path) == {}
    assert "Left out 3 records of humble's downloads" in capsys.readouterr().err
    outside = tmp_path.parent / f"{tmp_path.name}-outside.pdf"
    outside.write_bytes(b"%PDF-1.4 not yours to read")
    (tmp_path / "humble" / "A").mkdir(parents=True)
    try:
        os.symlink(outside, tmp_path / "humble" / "A" / "a.pdf")
    except OSError:
        return  # this computer won't make links: the words alone are checked above
    assert paths.library_file(tmp_path, "humble/A/a.pdf") is None
    assert paths.library_file(tmp_path, "humble/A/a.epub") == tmp_path / "humble/A/a.epub"


# ---- a file in its place, and Humble's purchases it gives no data for ------------------------------------------------

def test_a_recorded_file_cut_short_is_fetched_again_though_the_shop_lists_neither_size_nor_md5(setup, tmp_path):
    """A shop that lists a file with no size and no MD5 (DriveThruRPG and itch.io may) gives nothing to judge the
    file in its place by, but the place's record does: one cut short is fetched again, and its good record stays the
    latest. A file the record vouches for, or one with no record at all, is taken as it is."""
    data = blob(3000, kind="pdf")
    cdn, manifest, d, _ = setup({"cut.pdf": data, "whole.pdf": data, "found.pdf": data})
    jobs = {}
    for name in ("cut.pdf", "whole.pdf", "found.pdf"):
        jobs[name] = job = make_job(name, data, with_md5=False)
        job.size = 0  # nothing listed
        (tmp_path / job.path).parent.mkdir(parents=True)
        (tmp_path / job.path).write_bytes(data[:1000] if name == "cut.pdf" else data)
    for name in ("cut.pdf", "whole.pdf"):  # downloaded before, and recorded
        manifest.add({"path": jobs[name].path, "size": len(data), "md5": hashlib.md5(data).hexdigest(), "md5_source": "shop"})
    results = d.run(list(jobs.values()))
    assert len(results["ok"]) == 3 and [path for path, _ in cdn.requests] == ["/cut.pdf"]  # only the one cut short
    assert (tmp_path / jobs["cut.pdf"].path).read_bytes() == data
    cut, whole, found = (manifest.get(jobs[name].path) for name in ("cut.pdf", "whole.pdf", "found.pdf"))
    assert cut["size"] == len(data) and cut["md5"] == hashlib.md5(data).hexdigest() and not cut.get("adopted")
    assert whole["adopted"] and whole["md5"] == hashlib.md5(data).hexdigest()  # its record's MD5, not none
    assert whole["md5_source"] == "shop" and whole["md5_ok"]  # as its record vouched for it
    assert found["adopted"] and found["md5"] == hashlib.md5(data).hexdigest()  # nothing to compare with: its own MD5,
    assert found["md5_source"] == "adopted" and not found["md5_ok"]  # as found, and no check claimed


def test_a_file_adopted_with_no_md5_listed_is_recorded_with_its_own_as_unchecked_and_verify_hash_reads_it(setup, tmp_path):
    """A file found in its place where the shop lists its size but no MD5 (DriveThruRPG's, itch.io's): its MD5 is read
    and recorded as found (md5_source adopted, md5_ok false), not as one the shop's checksum confirmed with none; so
    hylly verify --hash can tell when it changes after. A file the shop's MD5 matched is recorded as the shop's."""
    data = blob(3000, kind="pdf")
    cdn, manifest, d, _ = setup({"bare.pdf": data, "listed.pdf": data})
    bare, listed = make_job("bare.pdf", data, with_md5=False), make_job("listed.pdf", data)
    for job in (bare, listed):
        (tmp_path / job.path).parent.mkdir(parents=True)
        (tmp_path / job.path).write_bytes(data)
    assert len(d.run([bare, listed])["ok"]) == 2 and cdn.requests == []
    entry = manifest.get(bare.path)
    assert entry["adopted"] and entry["md5"] == hashlib.md5(data).hexdigest()
    assert (entry["md5_source"], entry["md5_ok"]) == ("adopted", False)
    assert (manifest.get(listed.path)["md5_source"], manifest.get(listed.path)["md5_ok"]) == ("shop", True)
    (tmp_path / bare.path).write_bytes(data[:-1] + b"!")  # changed since, at the same size
    assert dl.verify(manifest, tmp_path, check_hash=True) == [(bare.path, "MD5 mismatch")]


@pytest.mark.parametrize("workers", [1, 2])
def test_a_shop_s_file_named_as_another_s_part_is_saved_apart_and_both_come(setup, tmp_path, workers):
    """A shop's file whose name ends in one of Hylly's own temporary endings (book.pdf.part beside book.pdf, as any
    itch.io creator may name them) is saved with a _ after it (paths.safe_filename): it was taken for book.pdf's partial
    download, which went on from it and deleted it, or moved it into superseded."""
    assert [paths.safe_filename(n) for n in ("book.pdf.part", "Notes.TMP", "a.copying", "b.seed", "Setup.exe.part ")] == [
        "book.pdf.part_", "Notes.TMP_", "a.copying_", "b.seed_", "Setup.exe.part_"]
    assert [paths.safe_filename(n) for n in ("book.partial", "Tmpl.tmpl", "Ordinary Book.epub")] == [
        "book.partial", "Tmpl.tmpl", "Ordinary Book.epub"]  # others stay as they are
    long = paths.safe_filename("x" * 300 + ".part")
    assert long.endswith(".part_") and len(long) <= paths.MAX_FILE_NAME
    assert dl.kind_of("Setup.exe.part") == "part_" and dl.kind_of("Setup.exe") == "exe"
    named, book = blob(2000, 5, "pdf"), blob(3000, 3, "pdf")
    jobs = [make_job(paths.safe_filename(name), data, folder="Book") for name, data in (("book.pdf.part", named),
                                                                                       ("book.pdf", book))]
    cdn, manifest, d, _ = setup({j.filename: data for j, data in zip(jobs, (named, book))})
    d.workers = workers
    assert len(d.run(jobs)["ok"]) == 2
    assert [(tmp_path / j.path).read_bytes() for j in jobs] == [named, book] and not (tmp_path / "superseded").exists()
    assert all(manifest.get(j.path) for j in jobs) and [rng for _, rng in cdn.requests] == [None, None]


def test_every_shop_s_run_counts_its_partial_files_and_names_those_no_download_goes_on_from(tmp_path):
    """download.left_behind: a part beside a file done, or of a format behind a title's file that is done, stays for
    good, so it is named, for you to delete; one a download still goes on from isn't (a file to come, or a format behind
    one, should that fail at the shop), nor one in a folder of no title the shop lists now (its file may still come)."""
    data = {name: blob(2000, n) for n, name in enumerate(("a.epub", "b.epub", "c.epub", "c.pdf", "d.epub", "d.pdf"), 1)}
    jobs = {name: make_job(name, data[name], folder=name[0].upper()) for name in data}
    for title in ("c", "d"):
        jobs[f"{title}.epub"].fallbacks = [jobs[f"{title}.pdf"]]
    manifest = dl.Manifest(tmp_path, "humble")
    for name in ("a.epub", "d.epub"):  # done
        (tmp_path / jobs[name].path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / jobs[name].path).write_bytes(data[name])
        manifest.add({"path": jobs[name].path, "size": len(data[name]), "md5": hashlib.md5(data[name]).hexdigest()})
    parts = {name: tmp_path / (jobs[name].path + ".part") for name in ("a.epub", "b.epub", "c.pdf", "d.pdf")}
    parts["elsewhere"] = tmp_path / "humble" / "Bundle" / "Gone Title" / "gone.pdf.part"
    for part in parts.values():
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"x" * 1000)
    own = jobs["a.epub"].path.rpartition("/")[0] + "/notes.pdf.part"  # a shop's own file, recorded under that name
    (tmp_path / own).write_bytes(b"x" * 1000)  # before such names got a _ (paths.safe_filename)
    manifest.add({"path": own, "size": 1000, "md5": hashlib.md5(b"x" * 1000).hexdigest()})
    titles = titled(*(jobs[name] for name in ("a.epub", "b.epub", "c.epub", "d.epub")))
    found, lines = dl.left_behind("humble", titles, manifest, tmp_path)
    left = [jobs["a.epub"].path + ".part", jobs["d.pdf"].path + ".part"]
    assert found == {"partial_files": 5, "left_behind": left}
    assert lines[0].startswith("Partial files no download goes on from: 2 (2.0 KB)") and lines[1:] == [f"  {p!r}" for p in left]
    for name in ("a.epub", "d.pdf"):
        parts[name].unlink()
    assert dl.left_behind("humble", titles, manifest, tmp_path) == ({"partial_files": 3}, [])


def test_a_dry_run_names_a_partial_file_left_beside_a_file_done(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(globals(), "LANTERN", atlas_with_a_program())
    serve = lambda request: httpx.Response(200, content=ATLAS_BOOK)  # noqa: E731
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(monkeypatch, serve), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    cli.main(["humble", "download", "--library", str(tmp_path)])
    assert "Partial files" not in capsys.readouterr().out
    part = placed(tmp_path, "atlas.epub").with_name("atlas.epub.part")
    part.write_bytes(ATLAS_BOOK[:100])  # left by a download cut short, before the file came another way
    import pathlib

    real, walked = pathlib.Path.rglob, []
    monkeypatch.setattr(pathlib.Path, "rglob", lambda self, *a, **k: walked.append(self) or real(self, *a, **k))
    cli.main(["humble", "download", "--dry-run", "--json", "--library", str(tmp_path)])
    said = json.loads(capsys.readouterr().out)
    assert said["partial_files"] == 1 and said["left_behind"] == [part.relative_to(tmp_path).as_posix()]
    assert walked.count(tmp_path / "humble") == 1  # Humble's folder walked once a run: its summary has them
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
    assert "Partial files no download goes on from: 1 (100 bytes)" in capsys.readouterr().out


def humble_answering(monkeypatch, orders: dict, answered: set) -> HumbleSite:
    """Humble's site as a stand-in: every purchase in its list, and only those in answered with a page."""
    site = HumbleSite(orders, answered=answered)
    monkeypatch.setattr(humble, "HumbleClient", site.client)
    monkeypatch.setattr(humble, "PAUSE", 0)
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", "c" * 80)
    return site


def test_a_purchase_humble_gives_no_data_for_keeps_its_titles_and_the_run_says_so(tmp_path, monkeypatch, capsys):
    """Humble's answer may lack a purchase, and the request for it alone fail too. A download then saves it as the
    last snapshot had it, so the catalog keeps its titles (it lists only the newest snapshot's), says so, and ends as
    partly done; a dry run says so too, and saves nothing; asked again with an answer, all is well."""
    from hylly import catalog, snapshot

    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
                  sub("lantern", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1)])]),
              "K2": order("K2", "2024-02-01T10:00:00", "Humble Book Bundle: Ann Writer", [
                  sub("moths", "A Field Guide to Moths", "Moonwake Press", [("EPUB", "moths.epub", 1)])])}
    answered = set(orders)
    humble_answering(monkeypatch, orders, answered)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", "--limit", "0", *library])  # the orders read and saved, nothing downloaded
    for t in plan.build(orders).titles:  # both downloaded before
        (tmp_path / t.path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / t.path).write_bytes(b"a book")
        dl.Manifest(tmp_path, "humble").add({"path": t.path, "shop_id": t.product.machine_name, "size": 6, "role": "book", "ext": "epub"})
    titles = lambda: sorted(i.title for i in catalog.humble_items(tmp_path))  # noqa: E731
    assert titles() == ["A Field Guide to Moths", "Lantern Wolf"]
    answered.discard("K2")
    capsys.readouterr()
    saved = lambda: {p: p.stat().st_mtime_ns for p in (tmp_path / "state" / "humble").glob("*.json")}  # noqa: E731
    for command in (["humble", "download", "--title", "nothing of the kind"], ["humble", "download", "--dry-run"],
                    ["humble", "download", "--json"]):
        before = saved()
        with pytest.raises(SystemExit) as e:
            cli.main([*command, *library])
        said = capsys.readouterr()
        assert e.value.code == exits.PARTLY, command
        assert "Humble gave no data for 1 purchase this time; 1 kept from the newest snapshot that has it." in said.err, command
        assert sorted(snapshot.latest(tmp_path / "state", "humble")[1]) == ["K1", "K2"]
        assert titles() == ["A Field Guide to Moths", "Lantern Wolf"]  # the catalog loses neither
        assert saved() == before, command  # the orders as the newest snapshot has them: not saved again (nor by a dry run)
        if "--json" in command:
            assert json.loads(said.out)["no_data"] == 1 and json.loads(said.out)["exit_code"] == exits.PARTLY
    answered.add("K2")
    cli.main(["humble", "download", "--limit", "0", *library])  # answered again: done, with nothing to say
    assert "no data" not in capsys.readouterr().err
    assert humble.missing_words(["K2", "K3"], ["K3"]) == ("Humble gave no data for 2 purchases this time; 1 kept from the "
                                                          "newest snapshot that has it; 1 is in no snapshot yet. Running it "
                                                          "again asks Humble again.")
    state = tmp_path / "state"  # one missing in the newest snapshot too is found further back, in the newest that has it
    (state / "humble" / "2000-01-01_0000.json").write_text(json.dumps({"K3": {"gamekey": "K3", "created": "an old one"}}), encoding="utf-8")
    (state / "humble" / "2000-01-02_0000.json").write_text(json.dumps({"K3": {"gamekey": "K3", "created": "a newer one"}}), encoding="utf-8")
    found = {}
    assert humble.carry_over(found, ["K2", "K3", "K9"], state) == ["K9"]
    assert found["K3"]["created"] == "a newer one" and found["K2"] == snapshot.latest(state, "humble")[1]["K2"]  # as saved


# ---- what Humble's rules can't settle: the questions ---------------------------------------------------------------

IRON_STRIDE = {"/fr.pdf": blob(3000, 1, "pdf"), "/fr_hq.pdf": blob(9000, 2, "pdf"), "/cl.pdf": blob(4000, 3, "pdf"),
               "/cl_hq.pdf": blob(12000, 4, "pdf"), "/cl_maps.pdf": blob(2000, 5, "pdf")}


def humble_files(monkeypatch, orders: dict, files: dict) -> None:
    """Humble's API answering with these orders, and its CDN with these files, by their names."""
    monkeypatch.setitem(globals(), "LANTERN", orders)
    served = humble_shop(monkeypatch, lambda request: httpx.Response(200, content=files[request.url.path]))
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=served, backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)


def iron_stride() -> dict:
    """Two RPG books Humble offers each as a PDF and a PDF (HQ), one of them with bonus maps."""
    f = lambda label, name: (label, name[1:], len(IRON_STRIDE[name]) / MIB, hashlib.md5(IRON_STRIDE[name]).hexdigest())  # noqa: E731
    return {"K1": order("K1", "2024-01-01T10:00:00", "Humble RPG Bundle: Iron Stride", [
        sub("fieldrules", "Iron Stride Field Rules", "Forgeworks Games", [f("PDF", "/fr.pdf"), f("PDF (HQ)", "/fr_hq.pdf")]),
        sub("clanledger", "Clan Ledger", "Duskwater Games", [f("PDF", "/cl.pdf"), f("PDF (HQ)", "/cl_hq.pdf"),
                                                            f("Bonus maps", "/cl_maps.pdf")])])}


def downloaded(root) -> list[str]:
    return sorted(e["path"].rpartition("/")[2] for e in dl.Manifest(root, "humble").entries.values())


def test_humble_puts_which_file_to_you_where_its_rules_are_unsure_and_an_answer_is_a_file_correction(tmp_path, monkeypatch,
                                                                                                    capsys):
    """An RPG book Humble offers as a PDF and a PDF (HQ) is downloaded in the larger, as the rules have always
    guessed, and put to you: a dry run's --json lists the question, its options and the guess, and keeps it not; the
    run keeps it, and it stays open once the guess is downloaded, as it was only a guess; answering with the other
    file is a file correction (hylly set file), which the next run downloads, and the question is gone; keeping the
    guess, another title's, keeps that file with the title's answers, and nothing comes again."""
    from hylly import curation

    humble_files(monkeypatch, iron_stride(), IRON_STRIDE)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", "--dry-run", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert said["questions_new"] == 2 and said["held_back"] == 0 and curation.questions(tmp_path) == []
    ledger, rules_q = said["questions"]  # in the order the run takes the titles: by their bundles' folders and names
    assert (rules_q["kind"], rules_q["catalog_id"], rules_q["file"], rules_q["id"], rules_q["guess"]) == (
        "which-file", "h:fieldrules", "", None, 1)
    assert rules_q["options"] == [{"file": "fr.pdf", "format": "pdf", "size": 3000, "label": "PDF"},
                                  {"file": "fr_hq.pdf", "format": "pdf", "size": 9000, "label": "PDF (HQ)"}]
    assert rules_q["why"] == "Humble offers 2 PDF files, and the largest was taken" and ledger["catalog_id"] == "h:clanledger"
    cli.main(["humble", "download", *library])
    assert "2 questions to decide, all new: hylly decide lists them" in capsys.readouterr().out
    assert downloaded(tmp_path) == ["cl_hq.pdf", "cl_maps.pdf", "fr_hq.pdf"]  # the guesses, as before, and the maps
    cli.main(["humble", "download", *library])
    assert "2 questions to decide: hylly decide lists them" in capsys.readouterr().out  # downloaded as guessed: still open
    ledger, rules_q = curation.questions(tmp_path)
    cli.main(["decide", str(rules_q["id"]), "1", *library])
    cli.main(["decide", str(ledger["id"]), "keep", *library])
    capsys.readouterr()
    assert curation.files(tmp_path) == {"fieldrules": "fr.pdf"}  # another file: a file correction
    assert curation.kept_files(tmp_path) == {"clanledger": ("cl_hq.pdf", ledger["offered"])}  # the guess kept, with its options
    cli.main(["humble", "download", *library])
    out = capsys.readouterr().out
    assert "To download: 1 title, 1 file" in out and "question" not in out and curation.questions(tmp_path) == []
    assert downloaded(tmp_path) == ["cl_hq.pdf", "cl_maps.pdf", "fr.pdf", "fr_hq.pdf"]  # the one you chose; nothing taken away


def test_with_unsure_ask_humble_holds_an_unsure_file_back_and_its_extras_come(tmp_path, monkeypatch, capsys):
    """[download] unsure = "ask" holds back a title's file the rules are unsure of, with the other formats behind
    it, until you answer; the run says how many it held back, its --json too; the title's extras come as ever, and an
    answer lets the file come at the next run."""
    from hylly import curation, paths

    paths.settings_file().write_text('[download]\nunsure = "ask"\n', encoding="utf-8")
    humble_files(monkeypatch, iron_stride(), IRON_STRIDE)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert said["held_back"] == 2 and said["questions_new"] == 2 and [q["held"] for q in said["questions"]] == [True, True]
    assert downloaded(tmp_path) == ["cl_maps.pdf"]
    cli.main(["humble", "download", *library])
    out = capsys.readouterr().out
    assert "Held back for your answer: 2 files (unsure = \"ask\" under [download])." in out
    assert "2 questions to decide: hylly decide lists them" in out
    rules_q = next(q for q in curation.questions(tmp_path) if q["catalog_id"] == "h:fieldrules")
    curation.answer(tmp_path, rules_q["id"], "2", by=curation.COMMAND_LINE)
    cli.main(["humble", "download", *library])
    assert "Held back for your answer: 1 file " in capsys.readouterr().out
    assert downloaded(tmp_path) == ["cl_maps.pdf", "fr_hq.pdf"]


TITLE_OR_NOT = {"/atlas.epub": blob(5000, 1), "/prints.pdf": blob(3000, 2, "pdf"), "/walls.zip": blob(2000, 3),
                "/posters.pdf": blob(1000, 4, "pdf"), "/posters_large.pdf": blob(1500, 5, "pdf")}


def moonwake_items(posters: tuple = ("/posters.pdf",)) -> dict:
    """A book, and items of no book of their own: two of extras only, a soundtrack, a game, a program, and an extra with a
    game's files beside it and one with music beside it (no question either)."""
    f = lambda label, name: (label, name[1:], len(TITLE_OR_NOT[name]) / MIB, hashlib.md5(TITLE_OR_NOT[name]).hexdigest())  # noqa: E731
    both = lambda one, other: {**one, "downloads": one["downloads"] + other["downloads"]}  # noqa: E731
    return {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        both(sub("gamebook", "Moonwake Game Book", "Moonwake Press", [("Bonus art book", "gameart.pdf", 1)]),
             sub("gamebook", "", "", [("Download", "moonwake.dmg", 1), ("Download", "moonwake.tar.gz", 1)], platform="mac")),
        both(sub("songbook", "Moonwake Songs", "Moonwake Press", [("Bonus lyrics", "lyrics.pdf", 1)]),
             sub("songbook", "", "", [("MP3", "songs.zip", 1)], platform="audio")),
        sub("atlas", "Atlas of Moonwake", "Moonwake Press", [f("EPUB", "/atlas.epub")]),
        sub("prints", "Moonwake Art Prints", "Moonwake Press", [f("Bonus art book", "/prints.pdf"), f("Wallpapers", "/walls.zip")]),
        sub("posters", "Moonwake Posters", "Moonwake Press", [f("Bonus posters", name) for name in posters]),
        sub("ost", "Moonwake Soundtrack", "Moonwake Press", [("MP3", "ost.zip", 1)], platform="audio"),
        sub("game", "Moonwake: The Game", "Moonwake Press", [("Download", "moonwake.exe", 1)], platform="windows"),
        sub("maptool", "Map Maker", "Moonwake Press", [("Map tool", "Map Maker.exe", 1)])])}


def test_an_item_of_only_extras_is_put_to_you_whether_it_is_a_title_and_a_file_answer_makes_it_one(tmp_path, monkeypatch,
                                                                                                   capsys):
    """A Humble item with no book of its own, of extras only, is left out as ever, and put to you with its files
    as the options and leaving it out as the guess; one with any file the rules know as no book (music, a game's, a
    program), its extras beside them too, is no question. Answering with a file makes the item a title (a file correction), which the next run
    downloads, its extras with it; hylly set file auto takes that back, and the question comes again. Keeping the guess
    asks no more, until the item offers other files."""
    from hylly import curation

    orders = moonwake_items()
    humble_files(monkeypatch, orders, TITLE_OR_NOT)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", "--dry-run", "--json", *library])
    questions = json.loads(capsys.readouterr().out)["questions"]
    assert [(q["kind"], q["catalog_id"]) for q in questions] == [("title", "h:prints"), ("title", "h:posters")]
    prints = questions[0]
    assert [o.get("file") or o["word"] for o in prints["options"]] == ["prints.pdf", "walls.zip", "Leave it out: not a title"]
    assert prints["guess"] == 2 and prints["why"] == "none of its 2 files is a book by the rules, so it was left out"
    cli.main(["humble", "download", *library])
    assert "2 questions to decide, all new" in capsys.readouterr().out and downloaded(tmp_path) == ["atlas.epub"]
    prints, posters = curation.questions(tmp_path)
    cli.main(["decide", str(prints["id"]), "1", *library])
    cli.main(["decide", str(posters["id"]), "keep", *library])
    capsys.readouterr()
    cli.main(["humble", "download", *library])
    assert "question" not in capsys.readouterr().out
    assert downloaded(tmp_path) == ["atlas.epub", "prints.pdf", "walls.zip"]  # a title now, with its extra
    cli.main(["set", "file", "auto", "--id", "h:prints", "--yes", *library])
    assert "1 title, from Humble Bundle (not in the catalog yet) (1)\n    Moonwake Art Prints\n" in capsys.readouterr().out
    cli.main(["humble", "download", "--dry-run", "--json", *library])
    assert [q["catalog_id"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["h:prints"]  # asked again
    orders.update(moonwake_items(posters=("/posters.pdf", "/posters_large.pdf")))  # what Humble answers from now on
    cli.main(["humble", "download", "--dry-run", "--json", *library])  # other files now: the answer kept is for those before
    assert [q["catalog_id"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["h:prints", "h:posters"]


def placed(root, name: str):
    """A Humble file's place in the library, by its name, from its record."""
    return root / next(e["path"] for e in dl.Manifest(root, "humble").entries.values() if e["path"].endswith(f"/{name}"))


def test_a_title_downloaded_before_fetched_again_is_no_question_and_never_held_back(tmp_path, monkeypatch, capsys):
    """A which-file question opens for a title not downloaded yet; one downloaded before (as every title an older
    Hylly downloaded was: guessed alike, and no question) whose file is fetched again, cut short say,
    keeps its way: fetched, no question, never held back, unsure = "ask" or not."""
    from hylly import curation, paths

    humble_files(monkeypatch, iron_stride(), IRON_STRIDE)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", *library])
    curation.keep_questions(tmp_path, "humble", [], lambda q: True)  # as an older Hylly left it: none kept
    paths.settings_file().write_text('[download]\nunsure = "ask"\n', encoding="utf-8")
    settings.reload()  # as the next command reads them
    cut = placed(tmp_path, "fr_hq.pdf")
    cut.write_bytes(IRON_STRIDE["/fr_hq.pdf"][:100])
    capsys.readouterr()
    cli.main(["humble", "download", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert (said["to_download"]["files"], said["held_back"], said["questions"]) == (1, 0, [])
    assert cut.read_bytes() == IRON_STRIDE["/fr_hq.pdf"]


def test_keeping_the_guess_asks_again_once_the_title_offers_other_files(tmp_path, monkeypatch, capsys):
    """The answer "keep" on a which-file question keeps the file the rules took, with what the
    question offered; once Humble offers another file of that sort, the question is open again, that file still the
    guess, and nothing new comes, nor is anything held back."""
    from hylly import curation, paths

    orders = iron_stride()
    humble_files(monkeypatch, orders, IRON_STRIDE)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", *library])
    ledger = next(q for q in curation.questions(tmp_path) if q["catalog_id"] == "h:clanledger")
    cli.main(["decide", str(ledger["id"]), "keep", *library])
    assert "keeps the file the rules took: asked again only if the title offers others" in capsys.readouterr().out
    cli.main(["humble", "download", "--json", *library])
    assert [q["catalog_id"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["h:fieldrules"]
    print_copy = blob(1000, 6, "pdf")
    orders["K1"]["subproducts"][1]["downloads"][0]["download_struct"].append(
        struct("PDF (Print)", "cl_print.pdf", len(print_copy) / MIB, hashlib.md5(print_copy).hexdigest()))
    paths.settings_file().write_text('[download]\nunsure = "ask"\n', encoding="utf-8")
    settings.reload()  # as the next command reads them
    cli.main(["humble", "download", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    again = next(q for q in said["questions"] if q["catalog_id"] == "h:clanledger")
    # the same question, kept answered on the answered list meanwhile, found again: its number kept
    assert again["options"][again["guess"]]["file"] == "cl_hq.pdf" and not again["held"] and again["id"] == ledger["id"]
    assert len(again["options"]) == 3 and said["to_download"]["files"] == 0 and said["held_back"] == 0


def test_a_file_you_moved_is_not_pending_in_humble_s_summary_nor_seeded_back(tmp_path, monkeypatch, capsys):
    """Humble's end-of-run summary and hylly humble seed go by your answers: a file you
    said you moved by hand isn't counted to download, nor copied back into the library from elsewhere."""
    from hylly import curation

    humble_files(monkeypatch, moonwake_items(), TITLE_OR_NOT)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", *library])
    atlas = placed(tmp_path, "atlas.epub")
    atlas.unlink()
    capsys.readouterr()
    cli.main(["humble", "download", "--dry-run", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert said["kinds"]["book"]["pending"]["files"] == 1  # the guess: again, until you say
    cli.main(["humble", "download", "--limit", "0", *library])  # a run keeps the question
    (moved,) = [q for q in curation.questions(tmp_path) if q["kind"] == "moved"]
    cli.main(["decide", str(moved["id"]), "2", *library])
    capsys.readouterr()
    cli.main(["humble", "download", "--dry-run", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert (said["kinds"]["book"]["pending"]["files"], said["moved_by_hand"], said["to_download"]["files"]) == (0, 1, 0)
    old = tmp_path.parent / f"{tmp_path.name}-old"
    old.mkdir()
    (old / "atlas.epub").write_bytes(TITLE_OR_NOT["/atlas.epub"])
    cli.main(["humble", "seed", str(old), *library])
    assert "copied: 0 files" in capsys.readouterr().out and not atlas.exists()


def test_the_page_reads_a_renamed_chosen_file_in_the_format_its_download_stands_on(tmp_path, monkeypatch, capsys):
    """A Humble title whose chosen file Humble renamed (the very file, by its MD5) is
    read in the format of the file it has, as before the rename (catalog.humble_items' prefer)."""
    from hylly import catalog

    orders = iron_stride()
    humble_files(monkeypatch, orders, IRON_STRIDE)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", *library])
    for dl_ in orders["K1"]["subproducts"][0]["downloads"]:
        for s in dl_["download_struct"]:
            s["url"]["web"] = s["url"]["web"].replace("/fr_hq.pdf", "/fr_hq_v2.pdf")
    cli.main(["humble", "download", *library])
    out = capsys.readouterr().out
    assert "'fr_hq_v2.pdf' is the same file as 'fr_hq.pdf', downloaded before: not fetched again" in out
    (rules_item,) = [i for i in catalog.humble_items(tmp_path) if i.id == "h:fieldrules"]
    assert rules_item.prefer == "pdf"


def test_a_title_bought_again_keeps_your_moved_answer_whichever_purchase_leads_it(tmp_path, monkeypatch, capsys):
    """The answer "I moved it" is given under the catalog id of the purchase that led the title then; a
    purchase more of the same title (the very file, more formats) takes the lead, and the answer counts all the same,
    as the title's file, format and keep do under any of its purchases: never downloaded again, no new question, never
    held back."""
    from hylly import curation, paths

    orders = moonwake_items()
    humble_files(monkeypatch, orders, TITLE_OR_NOT)
    library = ["--library", str(tmp_path)]
    cli.main(["humble", "download", *library])
    atlas = placed(tmp_path, "atlas.epub")
    atlas.unlink()
    cli.main(["humble", "download", "--limit", "0", *library])
    (moved,) = [q for q in curation.questions(tmp_path) if q["kind"] == "moved"]
    assert moved["catalog_id"] == "h:atlas"
    cli.main(["decide", str(moved["id"]), "2", *library])
    book = TITLE_OR_NOT["/atlas.epub"]
    orders["K1"]["subproducts"].append(sub("atlas_again", "Atlas of Moonwake", "Moonwake Press", [
        ("EPUB", "atlas.epub", len(book) / MIB, hashlib.md5(book).hexdigest()), ("PDF", "atlas.pdf", 1)]))  # leads now
    paths.settings_file().write_text('[download]\nunsure = "ask"\n', encoding="utf-8")
    settings.reload()  # as the next command reads them
    capsys.readouterr()
    cli.main(["humble", "download", "--json", *library])
    said = json.loads(capsys.readouterr().out)
    assert [t.key for t in humble.titles_of(plan.for_library(orders, tmp_path)) if t.name == "Atlas of Moonwake"] == ["atlas_again"]
    assert (said["to_download"]["files"], said["held_back"], said["moved_by_hand"]) == (0, 0, 1)
    assert [q for q in said["questions"] if q["kind"] == "moved"] == [] and not atlas.exists()


# ---- the shop list at every shop (shoplist.py, docs/downloads.md) ---------------------------------------------------

def list_rows(path) -> list[list[str]]:
    return list(csv.reader(path.read_bytes().decode("utf-8-sig").splitlines(), delimiter=";"))[1:]


def put_list(path, rows: list[list[str]]) -> None:
    from hylly import shoplist

    path.write_bytes(shoplist.data(rows))


@pytest.mark.parametrize("shop", STAND_INS)
def test_at_every_shop_a_list_written_and_read_back_unchanged_chooses_what_its_plain_run_chooses(shop, tmp_path,
                                                                                               monkeypatch, capsys):
    """One code path, at each shop's stand-in, its own code making its titles: hylly SHOP list, read back by hylly SHOP
    download LIST, chooses the very files a plain run chooses, in its order, and counts them the same; neither the list
    nor a dry run fetches a file, and the list keeps nothing (no snapshot, no question)."""
    STAND_INS[shop](monkeypatch, lambda request: pytest.fail("a list and a dry run fetch no file"))
    out = tmp_path / f"{shop}-list.csv"
    lib = tmp_path / "library"
    cli.main([shop, "list", str(out), "--library", str(lib)])
    said = capsys.readouterr()
    assert said.out == "" and f"to {out}" in said.err and not (lib / "state" / shop).exists()
    rows = list_rows(out)
    assert rows and {r[0] for r in rows} == {shop} and all(r[10].startswith(f"{shop}:") for r in rows)
    cli.main([shop, "download", "--dry-run", "--json", "--library", str(lib)])
    plain = json.loads(capsys.readouterr().out)
    cli.main([shop, "download", str(out), "--dry-run", "--json", "--library", str(lib)])
    listed = json.loads(capsys.readouterr().out)
    assert plain["pending"] and listed["pending"] == plain["pending"]
    for count in ("to_download", "already_done", "changed", "held_back", "left_out_by_you"):
        assert listed[count] == plain[count], count
    if shop != "itchio":  # itch.io is asked about the list's items alone (and of every type, as the list chooses)
        assert (listed["titles"], listed["files"]) == (plain["titles"], plain["files"])
    assert listed["list_problems"] == [] and listed["exit_code"] == 0


def test_hylly_list_writes_every_shop_s_rows_in_one_file_and_hylly_download_takes_each_shop_s_own(tmp_path, monkeypatch,
                                                                                                 capsys):
    """hylly list: every shop with a login in one file; hylly download LIST runs every shop it has rows for with its
    own rows alone, says the rows of a shop you aren't logged in to and those naming no shop, and ends partly done."""
    for stand_in in STAND_INS.values():
        stand_in(monkeypatch, lambda request: pytest.fail("a list and a dry run fetch no file"))
    lib, out = tmp_path / "library", tmp_path / "hylly-list.csv"
    cli.main(["list", str(out), "--library", str(lib)])
    rows = list_rows(out)
    assert {r[0] for r in rows} == set(STAND_INS) and "Humble: in the list." in capsys.readouterr().err
    with pytest.raises(SystemExit) as refused:  # a second run writes over nothing of yours
        cli.main(["list", str(out), "--library", str(lib)])
    assert refused.value.code == exits.USAGE and "add --force to write over it" in str(refused.value)
    stories = sum(r[0] == "storybundle" for r in rows)
    rows.append(["", "", "A note of mine", "", "", "", "", "", "", "", "my own words", "", "(X)"])
    put_list(out, rows)
    monkeypatch.delenv("HYLLY_STORYBUNDLE_SESSION")  # logged out of one
    with pytest.raises(SystemExit) as ended:
        cli.main(["download", str(out), "--dry-run", "--json", "--library", str(lib)])
    said = capsys.readouterr()
    result = json.loads(said.out)
    assert ended.value.code == exits.PARTLY
    assert f"StoryBundle: {stories} rows of StoryBundle's, a shop you aren't logged in to" in said.err
    ran = {s["shop"]: s for s in result["shops"]}
    assert ran["storybundle"]["ended"] == "no login saved" and ran["humble"]["exit_code"] == 0
    assert ran["humble"]["list_problems"] == [] and ran["drivethrurpg"]["to_download"]["files"] > 0
    assert result["list_problems"] == [  # the rows naming no shop, each; a shop not logged in to, once with its count
        {"title": "A note of mine", "id": "my own words", "why": (
            "matching nothing: an id is the shop's name, the title's key and the file's, as a list gives it")},
        {"title": "", "id": "storybundle", "why": f"{stories} rows of StoryBundle's, a shop you aren't "
                                                  "logged in to (hylly login storybundle): not taken"}]


def test_a_list_from_standard_input_and_to_standard_output(tmp_path, monkeypatch, capsysbinary):
    """- for FILE writes the list to standard output alone (what it says goes to standard error), and - for LIST reads it
    from standard input: so both go through ssh when the command runs on the server computer."""
    import io

    drivethrurpg_shop(monkeypatch, lambda request: pytest.fail("no file is fetched"))
    cli.main(["drivethrurpg", "list", "-", "--library", str(tmp_path)])
    said = capsysbinary.readouterr()
    assert said.out.startswith(b"\xef\xbb\xbfshop;bundle or publisher;title;") and b"drivethrurpg:1001:0" in said.out
    assert b"Reading your DriveThruRPG library" in said.err
    rows = list(csv.reader(said.out.decode("utf-8-sig").splitlines(), delimiter=";"))[1:]
    rows = [r for r in rows if r[10] != "drivethrurpg:1001:1"]  # the crew sheet's row deleted: this run's alone
    from hylly import shoplist

    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(shoplist.data(rows))))
    cli.main(["drivethrurpg", "download", "-", "--dry-run", "--json", "--library", str(tmp_path)])
    result = json.loads(capsysbinary.readouterr().out)
    assert [p["path"].rsplit("/", 1)[1] for p in result["pending"]] == ["Referee Rulebook.pdf", "Referee Screen.pdf"]


def test_the_jobs_timer_s_plain_download_never_fetches_a_file_you_left_out_and_counts_it_apart(tmp_path, monkeypatch,
                                                                                             capsys):
    """A plain hylly download, as a timer runs it with nobody at the keyboard, honours "left out by you": the file is not
    fetched, no failure and nothing pending, and counted apart in the run's end line, its --json, hylly jobs and the
    phone note's words."""
    from hylly import curation, runs

    def serve(request):
        order_id, index = (int(x) for x in request.url.path.strip("/").split("/"))
        return httpx.Response(200, content=BODIES[(order_id, index)])

    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=drivethrurpg_shop(monkeypatch, serve),
                                                            backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    monkeypatch.setattr(cli, "at_keyboard", lambda: False)
    curation.leave_out(tmp_path, {"d:1001": ("Far Stars: Referee Rulebook", {"1": "Crew Sheet.pdf"})}, by="command line")
    cli.main(["download", "--json", "--library", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    (drpg,) = [s for s in result["shops"] if s["shop"] == "drivethrurpg"]
    assert (drpg["done"], drpg["failed"], drpg["left_out_by_you"], drpg["to_download"]["files"]) == (2, 0, 1, 2)
    assert sorted(e["path"].rsplit("/", 1)[1] for e in dl.Manifest(tmp_path, "drivethrurpg").entries.values()) == [
        "Referee Rulebook.pdf", "Referee Screen.pdf"]
    (last,) = [r for r in runs.last_runs() if r["what"].startswith("A DriveThruRPG")]
    assert "2 files done, 0 failed. 1 left out by you." in last["said"]
    assert "1 left out by you" in (tmp_path / "state" / "download-log.txt").read_text(encoding="utf-8")


def test_a_humble_format_ticked_in_a_list_comes_as_the_title_s_own_and_a_title_left_out_is_never_pending(tmp_path,
                                                                                                       monkeypatch, capsys):
    """Humble: a format behind the chosen one, ticked in a list, is downloaded in that run beside it, and is the title's
    file from then on, so no file "no longer chosen" in the summary; a title whose rows are unticked is left out by you,
    never pending in the summary, which counts it apart."""
    epub, pdf, guide = blob(6000), blob(9000, 3, "pdf"), blob(7000, 5)
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("atlas", "Atlas of Moonwake", "Moonwake Press", [("EPUB", "atlas.epub", len(epub) / MIB, hashlib.md5(epub).hexdigest()),
                                                              ("PDF", "atlas.pdf", len(pdf) / MIB, hashlib.md5(pdf).hexdigest())]),
        sub("guide", "Lantern Wolf Guide", "Moonwake Press", [("EPUB", "guide.epub", len(guide) / MIB,
                                                                hashlib.md5(guide).hexdigest())])])})
    bodies, asked = {"/atlas.epub": epub, "/atlas.pdf": pdf, "/guide.epub": guide}, []
    serve = lambda request: asked.append(request.url.path) or httpx.Response(200, content=bodies[request.url.path])  # noqa: E731
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(monkeypatch, serve), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    out = tmp_path / "humble-list.csv"
    cli.main(["humble", "list", str(out), "--library", str(tmp_path)])
    rows = list_rows(out)
    assert [(r[2], r[4], r[12]) for r in rows] == [("Atlas of Moonwake", "EPUB", "(X)"), ("Atlas of Moonwake", "PDF", "( )"),
                                                    ("Lantern Wolf Guide", "EPUB", "(X)")]
    assert rows[1][8].startswith("another of its files, behind the one chosen") and rows[0][10] == "humble:atlas:atlas.epub"
    rows[1][12], rows[2][12] = "(X)", "( )"  # the PDF too; the guide not at all
    put_list(out, rows)
    capsys.readouterr()
    cli.main(["humble", "download", str(out), "--library", str(tmp_path)])
    said = capsys.readouterr().out
    assert sorted(asked) == ["/atlas.epub", "/atlas.pdf"] and "Your list left out 1 file" in said
    asked.clear()
    cli.main(["humble", "download", "--dry-run", "--json", "--library", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert (result["no_longer_chosen"], result["left_out_by_you"], result["to_download"]["files"]) == (0, 1, 0)
    assert result["kinds"]["book"]["pending"] == {"files": 0, "bytes": 0} and result["kinds"]["book"]["done"]["files"] == 1
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
    said = capsys.readouterr().out
    assert "left out by you: 1" in said and "Left out by you: 1 file, not fetched (hylly humble list shows it)." in said
    assert asked == []


def test_hylly_list_s_dry_run_says_how_many_rows_it_would_write_and_writes_none(tmp_path, monkeypatch, capsys):
    for stand_in in STAND_INS.values():
        stand_in(monkeypatch, lambda request: pytest.fail("a list fetches no file"))
    monkeypatch.chdir(tmp_path)
    cli.main(["list", "--dry-run", "--library", str(tmp_path / "library")])
    said = capsys.readouterr().err
    assert "It would write " in said and " rows of 4 shops to hylly-list.csv." in said and "Wrote" not in said
    assert not (tmp_path / "hylly-list.csv").exists() and not list(tmp_path.glob("*.csv"))


def two_pdfs(monkeypatch, serve) -> None:
    """Humble's orders of a title in two PDFs (a which-file question: the larger the guess), served as serve says."""
    hq, plain_pdf = blob(9000, 3, "pdf"), blob(8000, 4, "pdf")
    monkeypatch.setitem(globals(), "LANTERN", {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("notes", "Field Notes", "Moonwake Press", [("PDF (HQ)", "notes_hq.pdf", len(hq) / MIB, hashlib.md5(hq).hexdigest()),
                                                       ("PDF", "notes.pdf", len(plain_pdf) / MIB,
                                                        hashlib.md5(plain_pdf).hexdigest())])])})
    bodies = {"/notes_hq.pdf": hq, "/notes.pdf": plain_pdf}
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=humble_shop(
        monkeypatch, lambda request: serve(request, bodies)), backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)


def test_moving_the_mark_in_a_list_answers_the_which_file_question_with_that_file(tmp_path, monkeypatch, capsys):
    """A list that moves the mark of a which-file question from the guess to the other file answers the question with it,
    as hylly decide NUMBER FILE does (a file answer: the same edit), but not in a dry run: that file comes, the
    question closes, and the guess is never fetched later, with unsure = "ask" too; no file is left out for it."""
    from hylly import curation, paths

    asked = []
    two_pdfs(monkeypatch, lambda request, bodies: asked.append(request.url.path) or httpx.Response(
        200, content=bodies[request.url.path]))
    out = tmp_path / "humble-list.csv"
    cli.main(["humble", "list", str(out), "--library", str(tmp_path)])
    rows = list_rows(out)
    assert [(r[4], r[11], r[12]) for r in rows] == [("PDF (HQ)", "yes", "(X)"), ("PDF", "", "( )")]
    assert rows[0][8].startswith("the rules' guess: Humble offers 2 PDF files, and the largest was taken")
    rows[0][12], rows[1][12] = "( )", "(X)"  # the mark moved
    put_list(out, rows)
    capsys.readouterr()
    cli.main(["humble", "download", str(out), "--dry-run", "--library", str(tmp_path)])
    said = capsys.readouterr().out
    assert "Your list would answer the question of which file to keep for Field Notes (h:notes): notes.pdf" in said
    assert "question" not in said.split("would answer the question")[-1]  # not counted as one to decide
    assert curation.files(tmp_path) == {} and asked == []  # a dry run keeps nothing, and fetches nothing
    cli.main(["humble", "download", str(out), "--json", "--library", str(tmp_path)])
    said = capsys.readouterr()
    result, said = json.loads(said.out), said.err
    assert asked == ["/notes.pdf"] and "Your list answered the question of which file to keep for Field Notes" in said
    assert result["kinds"]["book"] == {"done": {"files": 1, "bytes": 8000}, "pending": {"files": 0, "bytes": 0},
                                       "changed": {"files": 0, "bytes": 0}}  # the summary goes by the answer kept
    assert curation.files(tmp_path) == {"notes": "notes.pdf"} and curation.left_out(tmp_path) == {}
    assert curation.questions(tmp_path) == [] and "question" not in said.split("answered the question")[-1]
    (answered,) = curation.questions(tmp_path, answered=True)
    assert answered["kind"] == "which-file" and answered["answer"]["by"] == curation.COMMAND_LINE
    paths.settings_file().write_text('[download]\nunsure = "ask"\n', encoding="utf-8")
    settings.reload()  # as the next command reads them
    cli.main(["humble", "download", "--library", str(tmp_path)])
    said = capsys.readouterr().out
    assert asked == ["/notes.pdf"] and "To download: 0 titles, 0 files" in said and "Held back" not in said


def test_a_ticked_format_behind_is_fetched_once_though_its_chosen_file_fails_at_the_shop(tmp_path, monkeypatch, capsys):
    """A format behind the chosen file, ticked in a list, comes as a job of its own: never also as the chosen file's
    fallback to the same place in the same run, should the chosen one fail at the shop."""
    asked = []

    def serve(request, bodies):
        asked.append(request.url.path)
        return httpx.Response(404) if request.url.path == "/notes_hq.pdf" else httpx.Response(200, content=bodies[request.url.path])

    two_pdfs(monkeypatch, serve)
    out = tmp_path / "humble-list.csv"
    cli.main(["humble", "list", str(out), "--library", str(tmp_path)])
    rows = list_rows(out)
    rows[1][12] = "(X)"  # the other PDF too, the guess kept ticked: nothing answered, both taken
    put_list(out, rows)
    with pytest.raises(SystemExit):
        cli.main(["humble", "download", str(out), "--library", str(tmp_path)])
    assert asked.count("/notes.pdf") == 1 and "/notes_hq.pdf" in asked


def test_an_itchio_list_with_an_id_of_two_parts_says_it_matches_nothing_and_goes_on(tmp_path, monkeypatch, capsys):
    itchio_shop(monkeypatch, lambda request: pytest.fail("a dry run fetches no file"))
    out = tmp_path / "itchio-list.csv"
    cli.main(["itchio", "list", str(out), "--library", str(tmp_path)])
    rows = list_rows(out)
    rows.append(["itchio", "", "Velvet Jinx", "", "", "", "", "", "", "", "itchio:123", "", "(X)"])
    put_list(out, rows)
    with pytest.raises(SystemExit) as ended:
        cli.main(["itchio", "download", str(out), "--dry-run", "--json", "--library", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert ended.value.code == exits.PARTLY and result["list_problems"] == [{"title": "Velvet Jinx", "id": "itchio:123", "why": (
        "matching nothing: an id is the shop's name, the title's key and the file's, as a list gives it")}]


def test_a_left_out_file_is_taken_back_by_a_row_s_id_or_any_of_its_title_s_ids_whichever_purchase_kept_it(tmp_path, capsys):
    """hylly set left-out auto --id takes a list row's id (that file) or a title's catalog id (all its files left out),
    for a title never downloaded too, and takes them back under every purchase of the title's (a Humble title bought
    again leads under the fuller purchase's id), as the downloads read them."""
    from hylly import curation, paths, snapshot

    orders = {"K1": order("K1", "2024-01-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
                  sub("lanternA", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1)])]),
              "K2": order("K2", "2025-01-01T10:00:00", "Humble Book Bundle: Lantern Press", [
                  sub("lanternB", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1), ("PDF", "lantern.pdf", 2)])])}
    snapshot.save(orders, paths.state_dir(tmp_path), "humble")
    (lead,) = humble.titles_of(plan.for_library(orders, tmp_path))
    assert (lead.key, lead.also) == ("lanternB", ("lanternA",))
    keep = lambda: curation.leave_out(tmp_path, {"h:lanternA": ("Lantern Wolf", {"lantern.epub": "lantern.epub"})},  # noqa: E731
                                      by=curation.COMMAND_LINE)
    for given in ("humble:lanternB:lantern.epub", "h:lanternB", "humble:lanternA:lantern.epub"):
        keep()
        cli.main(["set", "left-out", "auto", "--id", given, "--yes", "--library", str(tmp_path)])
        assert "Took back the files left out with a shop list for 1 title" in capsys.readouterr().out, given
        assert curation.left_out(tmp_path) == {}, given
    keep()
    cli.main(["set", "left-out", "auto", "--id", "humble:lanternB:lantern.pdf", "--yes", "--library", str(tmp_path)])
    assert "Nothing to do" in capsys.readouterr().out and curation.left_out(tmp_path) == {"h:lanternA": {"lantern.epub"}}
    with pytest.raises(SystemExit) as refused:
        cli.main(["set", "left-out", "out", "--id", "h:lanternB", "--library", str(tmp_path)])
    assert refused.value.code == exits.USAGE and "left-out takes auto alone" in str(refused.value)


def test_a_list_given_on_a_terminal_is_refused_and_one_read_by_a_reader_that_stops_ends_quietly(tmp_path, monkeypatch, capsys):
    """- as LIST reads a pipe or a file, never the keyboard, where it would wait silently for one typed; and a list
    written to standard output for a reader that has read enough (| head) ends quietly, as every command's output does."""
    import io
    import os

    from hylly import shoplist

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    drivethrurpg_shop(monkeypatch, lambda request: pytest.fail("no file is fetched"))
    monkeypatch.setattr("sys.stdin", Terminal())
    with pytest.raises(SystemExit) as refused:
        cli.main(["drivethrurpg", "download", "-", "--library", str(tmp_path)])
    assert refused.value.code == exits.USAGE and "nothing is piped in" in capsys.readouterr().err
    with pytest.raises(SystemExit) as missing:
        cli.main(["drivethrurpg", "download", str(tmp_path / "none.csv"), "--library", str(tmp_path)])
    assert "There is no list at" in str(missing.value) and "hylly drivethrurpg list writes one" in str(missing.value)
    read, write = os.pipe()
    os.close(read)  # the reader gone, as head is once it has its lines
    stream = io.TextIOWrapper(io.FileIO(write, "wb"), encoding="utf-8")
    monkeypatch.setattr("sys.stdout", cli._Reader(stream))
    cli._write_list("-", shoplist.data([["drivethrurpg", "", "Velvet Jinx", *[""] * 7, "drivethrurpg:1:0", "", "( )"]] * 20000),
                    False, "rows")
    assert sys.stdout.gone


def test_hylly_list_says_plainly_when_a_shop_s_part_couldn_t_be_written(tmp_path, monkeypatch, capsys):
    for stand_in in STAND_INS.values():
        stand_in(monkeypatch, lambda request: pytest.fail("a list fetches no file"))

    def broken(args):
        raise SystemExit("DriveThruRPG answered HTTP 500 for page 1 of your library.")

    monkeypatch.setattr(cli, "cmd_drivethrurpg", broken)
    with pytest.raises(SystemExit) as ended:
        cli.main(["list", str(tmp_path / "hylly-list.csv"), "--library", str(tmp_path / "library")])
    said = capsys.readouterr().err
    assert ended.value.code == exits.PARTLY and "DriveThruRPG: stopped: DriveThruRPG answered HTTP 500" in said
    assert "Its rows aren't in the list, so a download of it leaves DriveThruRPG out" in said
    assert {r[0] for r in list_rows(tmp_path / "hylly-list.csv")} == set(STAND_INS) - {"drivethrurpg"}


def test_moving_the_mark_from_a_guess_downloaded_already_answers_its_open_question_and_leaves_the_guess_as_it_is(
        tmp_path, monkeypatch, capsys):
    """The usual case: a plain run downloaded the guess (unsure = "guess") and opened its question. A list that moves
    the mark answers it with the other file, which comes; the guess stays in the library as it is, nothing is left out
    for it, and the run's own summary at its end goes by the answer (nothing pending)."""
    from hylly import curation

    asked = []
    two_pdfs(monkeypatch, lambda request, bodies: asked.append(request.url.path) or httpx.Response(
        200, content=bodies[request.url.path]))
    cli.main(["humble", "download", "--library", str(tmp_path)])
    assert asked == ["/notes_hq.pdf"] and len(curation.questions(tmp_path)) == 1
    (hq,) = [p for p in (tmp_path / "humble").rglob("notes_hq.pdf")]
    out = tmp_path / "humble-list.csv"
    cli.main(["humble", "list", str(out), "--library", str(tmp_path)])
    rows = list_rows(out)
    assert [(r[4], r[7], r[11], r[12]) for r in rows] == [("PDF (HQ)", "yes", "yes", "(X)"), ("PDF", "", "", "( )")]
    rows[0][12], rows[1][12] = "( )", "(X)"
    put_list(out, rows)
    capsys.readouterr()
    cli.main(["humble", "download", str(out), "--json", "--library", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert asked == ["/notes_hq.pdf", "/notes.pdf"] and hq.is_file()  # nothing deleted
    assert curation.files(tmp_path) == {"notes": "notes.pdf"} and curation.left_out(tmp_path) == {}
    assert curation.questions(tmp_path) == [] and result["questions"] == []
    assert result["kinds"]["book"]["pending"]["files"] == 0 and result["left_out_by_you"] == 0
    cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
    assert "To download: 0 titles, 0 files" in capsys.readouterr().out and asked == ["/notes_hq.pdf", "/notes.pdf"]


# ---- a download cut short costs as little as possible: each way found to lose one (once in test_storage.py) ----------

def downloader(tmp_path, files: dict[str, bytes]):
    cdn = FakeCDN({f"/{k}": v for k, v in files.items()})
    manifest = dl.Manifest(tmp_path, "humble")
    return cdn, manifest, dl.Downloader(tmp_path, manifest, refresh=lambda job: job.url,
                                        transport=httpx.MockTransport(cdn.handler), log=lambda s: None, backoff=0.0)


def test_the_old_file_stays_until_the_new_one_is_here(tmp_path):
    old, new = blob(3000), blob(3000, 7)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": new})
    job = make_job("a.epub", new)
    job.replace = True
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old)
    cdn.fail["/a.epub"] = [403] * 9
    assert len(d.run([job])["failed"]) == 1
    assert dest.read_bytes() == old and not (tmp_path / "superseded").exists()  # no hole in the library

    cdn, manifest, d = downloader(tmp_path, {"a.epub": new})
    assert len(d.run([job])["ok"]) == 1
    assert dest.read_bytes() == new and (tmp_path / "superseded" / job.path).read_bytes() == old


def test_an_unrecorded_good_file_stays_and_isnt_moved_aside(tmp_path):
    """StoryBundle gives a rounded size and no checksum, so a file on disk can't be recognised before it's fetched."""
    data = blob(3000)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})
    job = make_job("a.epub", data, with_md5=False)
    job.exact, job.size = False, 3100
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    assert len(d.run([job])["ok"]) == 1
    assert dest.read_bytes() == data and not (tmp_path / "superseded").exists()
    assert manifest.get(job.path)["adopted"] and not list(dest.parent.glob("*.part"))


def test_a_new_version_of_such_a_file_moves_the_old_one_aside(tmp_path):
    old, new = blob(3000), blob(3000, 7)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": new})
    job = make_job("a.epub", new, with_md5=False)
    job.exact, job.size = False, 3100
    dest = tmp_path / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old)
    assert len(d.run([job])["ok"]) == 1
    assert dest.read_bytes() == new and (tmp_path / "superseded" / job.path).read_bytes() == old


def test_a_file_reaches_the_disk_before_its_record(tmp_path, monkeypatch):
    data = blob(3000)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})
    seen = []
    real_sync, real_add = dl.sync, manifest.add
    monkeypatch.setattr(dl, "sync", lambda p: (seen.append("on the disk"), real_sync(p))[1])
    monkeypatch.setattr(manifest, "add", lambda e, **k: (seen.append("recorded"), real_add(e, **k))[1])
    assert len(d.run([make_job("a.epub", data)])["ok"]) == 1
    assert seen == ["on the disk", "recorded"]


def test_a_full_disk_isnt_retried_as_network_trouble(tmp_path, monkeypatch):
    data = blob(3000)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})
    job = make_job("a.epub", data)

    asked = []

    def full(*a, **k):
        asked.append(1)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(d, "_stream", full)
    result = d.run([job])  # nor taken for one file's trouble: the run stops (test_download's full disk)
    assert not result["failed"] and len(result["stopped"]) == 1 and len(asked) == 1
    assert d.disk_full and d.blocked.startswith("The library's disk is full")
    assert dl.no_use_retrying(OSError(28, "x")) and not dl.no_use_retrying(OSError(22, "Invalid argument"))


def test_a_download_and_a_copy_know_a_full_disk_by_one_list():
    """download.NO_ROOM and NO_ROOM_WINDOWS, which copies.no_room goes by too: a download hitting Windows' "the disk is
    full" for a file being written (39) tried again until then, a copy didn't."""
    import errno

    def from_windows(number: int) -> OSError:
        e = OSError(22, "Invalid argument")
        e.winerror = number
        return e

    full = [OSError(errno.ENOSPC, "No space left on device"), OSError(getattr(errno, "EDQUOT", errno.ENOSPC), "Disk quota exceeded"),
            from_windows(39), from_windows(112)]
    assert all(copies.no_room(e) and dl.no_use_retrying(e) for e in full)
    too_long = OSError(errno.ENAMETOOLONG, "File name too long")  # no use retrying either, but no full disk
    assert dl.no_use_retrying(too_long) and not copies.no_room(too_long)
    assert not copies.no_room(from_windows(64)) and not dl.no_use_retrying(from_windows(64))  # a share gone a moment


def test_a_part_from_an_earlier_run_isnt_spliced_onto_a_changed_file(tmp_path):
    old, new = blob(6000, 3), blob(6000, 7)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": new})
    cdn.etags["/a.epub"] = "0123456789abcdef0123456789abcdef-2"  # a checksum of its parts, which doesn't tell here
    job = make_job("a.epub", old)  # and the shop still lists the old file's
    part = tmp_path / (job.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(old[:2000])
    assert not d.run([job])["failed"]
    assert (tmp_path / job.path).read_bytes() == new and not part.exists()
    assert [rng for _, rng in cdn.requests] == ["bytes=2000-", None]


def test_a_server_ignoring_the_resume_gives_a_whole_new_file(tmp_path):
    data = blob(6000, 7)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})
    cdn.fail["/a.epub"] = ["ignore-range"]
    job = make_job("a.epub", data, with_md5=False)  # no checksum, and that's fine: nothing of the old part is in it
    part = tmp_path / (job.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(blob(2000, 3))
    assert not d.run([job])["failed"]
    assert (tmp_path / job.path).read_bytes() == data and len(cdn.requests) == 1


def test_a_wrong_part_from_an_earlier_run_isnt_held_against_the_new_download(tmp_path):
    data = blob(6000, 7)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})
    cdn.etags["/a.epub"] = hashlib.md5(data).hexdigest()
    cdn.fail["/a.epub"] = [None, "corrupt"]  # the part is wrong, and so is the first try without it: that's the one strike
    job = make_job("a.epub", blob(6000, 3))
    part = tmp_path / (job.path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(blob(2000, 3))
    assert not d.run([job])["failed"]
    assert (tmp_path / job.path).read_bytes() == data and len(cdn.requests) == 3


def test_a_file_that_keeps_getting_cut_gets_there_if_it_keeps_growing(tmp_path):
    data = blob(12000)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})

    def a_little(request, whole):
        rng = request.headers.get("range")
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        if not rng:
            return httpx.Response(200, content=whole[:1000], headers={"content-length": str(len(whole))})
        return httpx.Response(206, content=whole[start:start + 1000], headers={"content-range": f"bytes {start}-{start + 999}/{len(whole)}"})

    cdn.fail["/a.epub"] = [a_little] * 12  # more cuts than one file may have in a row
    job = make_job("a.epub", data, with_md5=False)
    assert not d.run([job])["failed"]
    assert (tmp_path / job.path).read_bytes() == data


def test_a_connection_cut_in_this_run_is_carried_on_from(tmp_path):
    data = blob(6000)
    cdn, manifest, d = downloader(tmp_path, {"a.epub": data})
    cdn.fail["/a.epub"] = ["short"]
    job = make_job("a.epub", data, with_md5=False)  # no checksum from anyone
    assert not d.run([job])["failed"]
    assert (tmp_path / job.path).read_bytes() == data
    assert [rng for _, rng in cdn.requests] == [None, "bytes=3000-"]


def test_seed_keeps_the_older_version(tmp_path):
    older, new = blob(2000), blob(3000, 3)
    source = tmp_path / "old"
    source.mkdir()
    job = make_job("lw1.cbz", new, kind="comic")
    (source / "lw1.cbz").write_bytes(new)
    root = tmp_path / "lib"
    dest = root / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(older)
    report = dl.seed([source], [job], root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert [j.filename for _, j in report["copied"]] == ["lw1.cbz"]
    assert dest.read_bytes() == new and (root / "superseded" / job.path).read_bytes() == older


def test_seed_tries_again_when_the_share_drops_out_once(tmp_path, monkeypatch):
    """As hylly add does (copies.copy_in): until then one "Invalid argument" from the share failed the file. Here it comes
    as the copy takes its name, after the older version has gone aside, which a try after finds done."""
    older, new = blob(2000), blob(3000, 3)
    source = tmp_path / "old"
    source.mkdir()
    (source / "lw1.cbz").write_bytes(new)
    job = make_job("lw1.cbz", new, kind="comic")
    root = tmp_path / "lib"
    dest = root / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(older)
    real, swaps = os.replace, []

    def drops_out_once(src, dst, *a, **k):
        if Path(dst) == dest:
            swaps.append(dst)
            if len(swaps) == 1:
                raise OSError(22, "Invalid argument")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", drops_out_once)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    report = dl.seed([source], [job], root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert [j.filename for _, j in report["copied"]] == ["lw1.cbz"] and not report["failed"] and len(swaps) == 2
    assert dest.read_bytes() == new and [p.read_bytes() for p in (root / "superseded").rglob("*") if p.is_file()] == [older]
    assert not list(root.rglob("*.copying"))


def test_seed_takes_a_copy_whose_name_came_through_the_share_said_it_failed_as_done(tmp_path, monkeypatch):
    """The share says a swap failed that went through: the try after finds the file in its place with the job's size and
    MD5, and moves nothing into superseded but the older version (it used to move the new one too, as older)."""
    older, new = blob(2000), blob(3000, 3)
    source = tmp_path / "old"
    source.mkdir()
    (source / "lw1.cbz").write_bytes(new)
    job = make_job("lw1.cbz", new, kind="comic")
    root = tmp_path / "lib"
    dest = root / job.path
    dest.parent.mkdir(parents=True)
    dest.write_bytes(older)
    real, swaps = os.replace, []

    def went_through_but_said_no(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if Path(dst) == dest:
            swaps.append(dst)
            if len(swaps) == 1:
                raise OSError(22, "Invalid argument")

    monkeypatch.setattr(os, "replace", went_through_but_said_no)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    report = dl.seed([source], [job], root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert [j.filename for _, j in report["copied"]] == ["lw1.cbz"] and not report["failed"] and len(swaps) == 1
    assert dest.read_bytes() == new and [p.read_bytes() for p in (root / "superseded").rglob("*") if p.is_file()] == [older]
    assert not list(root.rglob("*.copying"))


def planned(tmp_path, n: int):
    """n files of a shop's, in a folder of their own, as seed finds them: (the folder, their jobs, the library)."""
    source = tmp_path / "old"
    source.mkdir()
    jobs = []
    for i in range(n):
        data = blob(1000 + i, i + 2)
        (source / f"lw{i}.cbz").write_bytes(data)
        jobs.append(make_job(f"lw{i}.cbz", data, kind="comic"))
    return source, jobs, tmp_path / "lib"


def test_seed_stops_at_once_when_the_library_s_disk_is_full(tmp_path, monkeypatch, capsys):
    """As hylly add does: no waiting, and the run ends with the rest left for later (it used to try each file six times,
    a couple of minutes each, and every file after it too)."""
    source, jobs, root = planned(tmp_path, 3)
    tried, slept = [], []

    def full(src, dst, *a, **k):
        tried.append(src)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(copies, "hashed_copy", full)
    monkeypatch.setattr(time, "sleep", slept.append)
    with pytest.raises(SystemExit) as e:
        dl.seed([source], jobs, root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert e.value.code == exits.LOCAL and "0 copied, 1 couldn't be, and the library's disk is full, so the other 2 wait" in str(e.value)
    assert "once it has room" in str(e.value) and len(tried) == 1 and slept == [] and "retrying" not in capsys.readouterr().err
    assert not list(root.rglob("*.copying"))


def test_seed_stops_when_the_library_s_disk_stops_taking_copies(tmp_path, monkeypatch):
    source, jobs, root = planned(tmp_path, copies.GIVE_UP_AFTER + 2)
    tried = []

    def refused(src, dst, *a, **k):
        tried.append(src)
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(copies, "hashed_copy", refused)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(copies, "_takes_writes", lambda r: False)
    with pytest.raises(SystemExit, match=f"0 copied, {copies.GIVE_UP_AFTER} couldn't be, and the library's disk isn't taking "
                                         "copies now, so the other 2 wait. Running it again carries on"):
        dl.seed([source], jobs, root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert len(set(tried)) == copies.GIVE_UP_AFTER


def test_seed_copies_nothing_when_the_library_s_disk_has_no_room_for_it_all(tmp_path, monkeypatch):
    source, jobs, root = planned(tmp_path, 2)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10_000, 10_000 - 1500, 1500))
    with pytest.raises(SystemExit, match="These need 2.0 KB, and the library's disk has 1.5 KB free"):
        dl.seed([source], jobs, root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert not list(root.rglob("*.cbz"))
    assert len(dl.seed([source], jobs, root, dl.Manifest(root, "humble"), dry_run=True, log=lambda s: None)["copied"]) == 2


def test_a_file_the_shop_lists_with_a_placeholder_for_its_name_says_whose_fault_it_is(setup):
    data = blob(2000, 1)
    cdn, _, d, _ = setup({"_FILE_NAME_": data}, refresh=lambda job: job.url)
    cdn.fail["/_FILE_NAME_"] = [404] * 9
    job = make_job("_FILE_NAME_", data)
    (got, error), = d.run([job])["failed"]
    assert got is job and error.startswith("link refused (HTTP 404) even after fetching a fresh one: the shop lists this "
                                           "file with a placeholder where its name should be (_FILE_NAME_)")
    assert all(dl.PLACEHOLDER.fullmatch(n) for n in ("<FILE_NAME>", "%3CFILE_NAME%3E", "_FILE_NAME_", "file_name"))
    assert not any(dl.PLACEHOLDER.fullmatch(n) for n in ("profile_name.pdf", "FILE_NAMES.epub", "my_file_name.cbz"))


def test_a_file_far_smaller_than_listed_is_kept_when_the_server_s_checksum_vouches_and_it_is_no_mere_page(setup):
    """Humble lists 626 MB and serves 56 MB, a lighter PDF: whole by the server's length and its checksum, it is kept, and
    said; with no checksum of the server's, or under VOUCHED_LEAST, it still isn't the file."""
    lighter = blob(dl.VOUCHED_LEAST + 1000, kind="pdf")
    cdn, manifest, d, _ = setup({"lighter.pdf": lighter, "unvouched.pdf": lighter})
    cdn.etags["/lighter.pdf"] = hashlib.md5(lighter).hexdigest()
    listed = blob(dl.FAR_SMALLER * len(lighter) + 1000, kind="pdf")
    vouched, unvouched = stale(make_job("lighter.pdf", lighter), listed), stale(make_job("unvouched.pdf", lighter), listed)
    said: list[str] = []
    d.log = said.append
    results = d.run([vouched, unvouched])
    assert [j for j, _ in results["ok"]] == [vouched]
    record = manifest.get(vouched.path)
    assert record["md5_source"] == "cdn" and record["listed_size"] == len(listed) and record["listed_md5"] == vouched.md5
    assert any("kept the" in line and "its listing is out of date" in line for line in said)
    assert dl.recorded_state(record, vouched, d.root / vouched.path) == "done"  # not fetched again while it lists the same
    (job, error), = results["failed"]
    assert job is unvouched and "and the server gives no checksum for it: too small to be the file, twice" in error
