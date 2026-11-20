"""Pages kept on the server's disk, and drawn ahead of the reader: memory, then disk, then drawn once; guesses only in free
room and only while nobody waits; how far each reader asks; and the counts on the server page. And the pages kept in
memory: no more than memory_mb, nor than half of what is free (once with the settings of how far the browser reads
ahead, test_reading_ahead.py, and the PDF worker's, test_pdfworker.py)."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import namedtuple

import pytest

from hylly import catalog, doctor, reader, settings
from hylly.library import Catalog

from helpers import build_library, make_comic, picture


Disk = namedtuple("Disk", "total used free")


def webp(size: int = 1000, mark: int = 0) -> bytes:
    """What a drawn page looks like to the cache: a WebP's first bytes, then anything."""
    return b"RIFF\0\0\0\0WEBP" + bytes([mark % 256]) * size


@pytest.fixture
def drawing(monkeypatch):
    """Pages drawn by a counter, not by Pillow, in memory and on a disk of their own, with plenty free."""
    made = []

    def make(path, stamp, kind, n, width):
        if n >= 100:
            raise IndexError(n)
        made.append((path, stamp, n, width))
        return webp(1000, n), "image/webp"

    monkeypatch.setattr(reader, "_make_page", make)
    monkeypatch.setattr(reader, "_kept", type(reader._kept)())
    monkeypatch.setattr(reader, "_kept_size", [0])
    monkeypatch.setattr(reader, "STATS", dict.fromkeys(reader.STATS, 0))
    monkeypatch.setattr(reader, "WIDTHS_ASKED", {})
    monkeypatch.setattr(reader, "_drawn_ahead", type(reader._drawn_ahead)())
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    disk = {"free": 500 << 30, "total": 1000 << 30}
    monkeypatch.setattr(shutil, "disk_usage", lambda where: Disk(disk["total"], 0, disk["free"]))
    return made, disk


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    """Room on the disk, whatever this computer's has free; drawing below has a disk of its own."""
    monkeypatch.setattr(shutil, "disk_usage", lambda where: Disk(1000 << 30, 0, 500 << 30))


def forget_memory():
    reader._kept.clear()
    reader._kept_size[0] = 0


def test_a_page_drawn_once_comes_from_disk_after(drawing):
    made, _ = drawing
    first = reader._page("book.cbz", (1, 2), "zip", 3, 1700)
    forget_memory()  # as after a restart
    again = reader._page("book.cbz", (1, 2), "zip", 3, 1700)
    assert again == first and len(made) == 1
    assert reader.STATS["drawn"] == 1 and reader.STATS["disk"] == 1
    reader._page("book.cbz", (1, 2), "zip", 3, 1700)
    assert reader.STATS["memory"] == 1  # and from memory once it's there again
    assert list(reader.drawn_dir().rglob("*.webp"))[0].name == "3-1700.webp"
    assert not list(reader.drawn_dir().rglob("*.part"))


def test_a_changed_file_gets_new_pages(drawing):
    made, _ = drawing
    reader._page("book.cbz", (1, 2), "zip", 0, 1700)
    forget_memory()
    reader._page("book.cbz", (1, 3), "zip", 0, 1700)  # its time or size changed
    assert len(made) == 2


def test_each_width_is_a_page_of_its_own(drawing):
    made, _ = drawing
    reader._page("book.cbz", (1, 2), "zip", 0, 1700)
    reader._page("book.cbz", (1, 2), "zip", 0, 2000)
    assert len(made) == 2 and dict(reader.status()["widths"]) == {1700: 1, 2000: 1}


def test_only_a_drawn_page_is_read_back(drawing, monkeypatch):
    made, _ = drawing
    reader._page("book.cbz", (1, 2), "zip", 0, 1700)
    forget_memory()
    target = next(reader.drawn_dir().rglob("*.webp"))
    target.write_bytes(b"not a page")  # spoilt on the disk somehow: drawn again, not sent
    reader._page("book.cbz", (1, 2), "zip", 0, 1700)
    assert len(made) == 2 and reader._webp(target.read_bytes())
    monkeypatch.setattr(reader, "_make_page", lambda *key: (b"x" * 100, "image/webp"))  # what isn't a WebP isn't kept
    reader._page("other.cbz", (1, 2), "zip", 0, 1700)
    assert len(list(reader.drawn_dir().rglob("*.webp"))) == 1


def test_the_books_read_longest_ago_go_first(drawing, monkeypatch):
    monkeypatch.setattr(reader, "cache_limit", lambda: 10_000)  # about nine pages
    for book in ("old", "mid", "new"):
        for n in range(3):
            reader._page(f"{book}.cbz", (1, 2), "zip", n, 1700)
        folder = reader._drawn_file(f"{book}.cbz", (1, 2), "zip", 0, 1700).parent
        age = {"old": 300, "mid": 200, "new": 100}[book]
        os.utime(folder, (time.time() - age, time.time() - age))
    forget_memory()
    reader._page("old.cbz", (1, 2), "zip", 0, 1700)  # read again: now the latest
    for n in range(3):
        reader._page("fourth.cbz", (1, 2), "zip", n, 1700)
    left = {p.name for p in reader.drawn_dir().iterdir()}
    gone = reader._drawn_file("mid.cbz", (1, 2), "zip", 0, 1700).parent.name
    assert gone not in left and len(left) == 3  # mid went: old was read again, and the rest came later
    assert reader.disk_used() == sum(p.stat().st_size for p in reader.drawn_dir().rglob("*") if p.is_file())


def test_a_guess_only_takes_free_room(drawing, monkeypatch):
    made, _ = drawing
    monkeypatch.setattr(reader, "cache_limit", lambda: 3_100)  # three pages
    for n in range(3):
        reader._page("read.cbz", (1, 2), "zip", n, 1700)
    reader._page("guess.cbz", (1, 2), "zip", 0, 1700, ahead=True)
    assert reader._drawn_file("guess.cbz", (1, 2), "zip", 0, 1700).exists() is False  # no room: not kept on disk
    assert len(list(reader.drawn_dir().rglob("*.webp"))) == 3  # and nothing read was pushed out
    reader._page("guess.cbz", (1, 2), "zip", 0, 1700)  # in memory, so it was worth drawing
    assert len(made) == 4
    reader._page("read2.cbz", (1, 2), "zip", 0, 1700)  # a page read does push the oldest out
    assert reader._drawn_file("read2.cbz", (1, 2), "zip", 0, 1700).exists()


def test_the_disk_keeps_room_free(drawing):
    _, disk = drawing
    disk.update(total=100 << 30, free=(5 << 30) + 4000)  # a twentieth of it free, and a few pages more
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    reader._page("a.cbz", (1, 2), "zip", 1, 1700)
    assert reader.disk_used() == 2024
    disk["free"] = (5 << 30) - 1  # the disk fills up with other things: the cache gives room back
    reader._page("b.cbz", (1, 2), "zip", 0, 1700)
    assert reader.disk_used() == 1012 and not reader._drawn_file("a.cbz", (1, 2), "zip", 0, 1700).exists()
    disk["free"] = 1 << 30  # and with less than that free, nothing more is kept
    reader._page("c.cbz", (1, 2), "zip", 0, 1700)
    assert not reader._drawn_file("c.cbz", (1, 2), "zip", 0, 1700).exists()
    assert reader.disk_limit(reader.disk_used()) == 0


def test_a_page_drawn_while_the_cache_is_counted_comes_at_once_and_is_kept_as_the_count_ends(drawing, monkeypatch):
    """The server counts the cache at its start, in a thread of its own, and tens of thousands of files on a slow or
    sleeping disk take a while. A page drawn meanwhile comes at once, without waiting for the count, waits in memory, and
    is kept on disk as the count ends, counted with the rest."""
    reader._page("old.cbz", (1, 2), "zip", 0, 1700)  # a page of before, on the disk
    forget_memory()
    monkeypatch.setattr(reader, "_disk", {"tops": None, "books": {}, "counting": False, "waiting": []})  # as at a start
    counting, done = threading.Event(), threading.Event()
    real = reader._size_of

    def slow(folder):  # a sleeping disk: the count goes on until the test says
        counting.set()
        assert done.wait(10), "the count was never let go on"
        return real(folder)

    monkeypatch.setattr(reader, "_size_of", slow)
    start = threading.Thread(target=reader.disk_used, daemon=True, name="cache-count")  # as serve.run starts it
    start.start()
    assert counting.wait(10)
    began = time.monotonic()
    page = reader._page("new.cbz", (1, 2), "zip", 0, 1700)
    took = time.monotonic() - began
    target = reader._drawn_file("new.cbz", (1, 2), "zip", 0, 1700)
    kept_meanwhile = target.exists()
    done.set()
    start.join(10)
    assert took < 5 and page == (webp(1000, 0), "image/webp") and not kept_meanwhile  # not held up by the count
    assert target.exists() and not reader._disk["waiting"]  # kept as it ended
    assert reader.disk_used() == sum(p.stat().st_size for p in reader.drawn_dir().rglob("*.webp")) == 2024


def test_a_count_that_fails_costs_a_request_only_what_it_would_keep(drawing, monkeypatch):
    """A count of the cache that fails (its disk in trouble, or a folder gone as it's walked, which Path.rglob raises on
    Python 3.11 and 3.12) fails no request: a page drawn is sent and not kept, the server page still says the rest, a
    file to unpack is judged by its disk's free room, and nothing is trimmed until a count goes through."""
    made, _ = drawing
    reader._page("old.cbz", (1, 2), "zip", 0, 1700)  # a book's folder, for the count to walk
    forget_memory()
    monkeypatch.setattr(reader, "_disk", {"tops": None, "books": {}, "counting": False, "waiting": []})  # as at a start

    def failing(folder):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(reader, "_size_of", failing)
    assert reader._page("new.cbz", (1, 2), "zip", 0, 1700) == (webp(1000, 0), "image/webp") and len(made) == 2
    assert not reader._drawn_file("new.cbz", (1, 2), "zip", 0, 1700).exists() and not reader._disk["counting"]
    said = reader.status()
    assert said["disk_held"] == 0 and said["drawn"] == 2
    unpacked = reader.cache_dir() / ("e" * 20)
    unpacked.mkdir(parents=True)
    assert reader._room_for(1000) is None and reader._trim_cache(keep=unpacked) is None and unpacked.is_dir()
    with pytest.raises(OSError):  # the server's own count at its start, in a thread of its own, says it in its log
        reader.disk_used()


def test_a_slow_write_holds_up_no_other_page_and_the_cache_keeps_and_trims_as_it_would_one_at_a_time(drawing, monkeypatch):
    """Two readers' pages drawn at once, the first one's file slow to write (a slow or sleeping disk): the other's are
    kept meanwhile, as the cache's lock is taken for the bookkeeping alone, never while a file is written. What is kept
    and trimmed is what it would be one at a time: once past the limit, the book read longest ago goes, the books just
    read stay, and the count is what the disk holds."""
    monkeypatch.setattr(reader, "cache_limit", lambda: 10_000)  # about nine pages
    for n in range(3):
        reader._page("old.cbz", (1, 2), "zip", n, 1700)
    old = reader._drawn_file("old.cbz", (1, 2), "zip", 0, 1700).parent
    os.utime(old, (time.time() - 300, time.time() - 300))
    slow = reader._drawn_file("slow.cbz", (1, 2), "zip", 0, 1700)
    writing, others_kept = threading.Event(), threading.Event()
    real = type(slow).write_bytes

    def write_bytes(path, data):
        if path.parent == slow.parent:
            writing.set()
            assert others_kept.wait(10), "the other pages were held up by this write"
        return real(path, data)

    monkeypatch.setattr(type(slow), "write_bytes", write_bytes)
    first = threading.Thread(target=reader._page, args=("slow.cbz", (1, 2), "zip", 0, 1700), daemon=True)
    first.start()
    assert writing.wait(10)
    for n in range(6):
        reader._page("fast.cbz", (1, 2), "zip", n, 1700)
    fast = reader._drawn_file("fast.cbz", (1, 2), "zip", 0, 1700).parent
    kept_meanwhile = len(list(fast.glob("*.webp")))
    others_kept.set()
    first.join(10)
    assert kept_meanwhile == 6 and slow.exists()
    assert sorted(p.name for p in reader.drawn_dir().iterdir()) == sorted([fast.name, slow.parent.name])  # old went
    assert reader.disk_used() == sum(p.stat().st_size for p in reader.drawn_dir().rglob("*.webp")) == 7 * 1012


def test_a_cache_of_nothing_keeps_nothing(drawing, monkeypatch):
    monkeypatch.setattr(reader, "cache_limit", lambda: 0)
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    assert not reader.drawn_dir().exists() or not list(reader.drawn_dir().rglob("*.webp"))


class Watched(dict):
    """reader._drawing, telling of each asker who finds a page being drawn already and so waits for it (found), so
    that a test goes by that, not by sleeps that stand in for the order of threads."""

    def __init__(self) -> None:
        super().__init__()
        self.found = threading.Semaphore(0)

    def get(self, key, default=None):
        going = super().get(key, default)
        if going is not None:
            self.found.release()
        return going


def test_two_asking_for_one_page_draw_it_once(drawing, monkeypatch):
    made, _ = drawing
    drawn, watched = reader._make_page, Watched()
    monkeypatch.setattr(reader, "_drawing", watched)

    def make(*key):
        for _ in range(2):  # the page is drawn only once both others are waiting for it
            assert watched.found.acquire(timeout=10), "the others never came to wait"
        return drawn(*key)

    monkeypatch.setattr(reader, "_make_page", make)
    got = []
    threads = [threading.Thread(target=lambda: got.append(reader._page("a.cbz", (1, 2), "zip", 5, 1700))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1 and len(got) == 3 and len(set(got)) == 1


def test_a_page_that_fails_is_tried_again_by_whoever_waited(drawing, monkeypatch):
    calls, watched, begun, failed = [], Watched(), threading.Event(), threading.Event()
    monkeypatch.setattr(reader, "_drawing", watched)

    def make(*key):
        calls.append(key)
        begun.set()
        if len(calls) == 1:
            assert watched.found.acquire(timeout=10), "the second never came to wait"
            failed.set()
            raise reader.Unreadable("broken once")  # once the second is waiting for this drawing
        assert failed.is_set(), "the second drew it beside the first instead of waiting for it"
        return webp(), "image/webp"

    monkeypatch.setattr(reader, "_make_page", make)
    errors, got = [], []

    def ask():
        try:
            got.append(reader._page("a.cbz", (1, 2), "zip", 0, 1700))
        except reader.Unreadable as e:
            errors.append(e)

    first = threading.Thread(target=ask)
    first.start()
    assert begun.wait(10)  # the first is drawing it before the second asks
    second = threading.Thread(target=ask)
    second.start()
    first.join()
    second.join()
    assert len(errors) == 1 and len(got) == 1 and len(calls) == 2


# ---- drawing ahead ----

@pytest.fixture
def guesses(monkeypatch):
    """The readers' guesses of this test only, with nothing being drawn for anyone, and a worker of its own: one running
    from before ends as it sees it's no longer the one."""
    monkeypatch.setattr(reader, "_guesses", type(reader._guesses)())
    monkeypatch.setattr(reader, "_asked_now", [0])
    monkeypatch.setattr(reader, "_ahead_worker", [None])
    return reader._guesses


def test_the_nearest_page_of_everyones_comes_first(guesses, monkeypatch):
    monkeypatch.setattr(reader, "_ahead_worker", [threading.current_thread()])  # as if one were running: none is started
    comic = ("a.cbz", (1, 2), "zip", 1700)
    with reader._ahead_cond:
        for rid, start in (("readerone", 10), ("readertwo", 50)):
            guesses[rid] = {"at": time.monotonic(), "book": comic, "pages": reader.deque((i, start + i) for i in range(1, 4))}
        order = [reader._next_guess()[1][3] for _ in range(6)]
        assert reader._next_guess() is None
    assert order == [11, 51, 12, 52, 13, 53]  # one each, nearest first
    assert not guesses  # nothing is kept of a reader once its pages are on their way


def test_nothing_is_guessed_while_a_page_asked_for_is_drawn(guesses):
    with reader._ahead_cond:
        guesses["readerone"] = {"at": time.monotonic(), "book": ("a.cbz", (1, 2), "zip", 1700), "pages": reader.deque([(1, 1)])}
        reader._asked_now[0] = 1
        assert reader._next_guess() is None
    reader._asked_now[0] = 0
    with reader._ahead_cond:
        assert reader._next_guess()[1][3] == 1


def comic_file(tmp_path):
    path = tmp_path / "c.cbz"
    make_comic(path)  # four pages
    return path


def wait_for(check, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_pages_asked_for_ahead_are_drawn_and_then_come_at_once(tmp_path, guesses, monkeypatch):
    monkeypatch.setattr(reader, "STATS", dict.fromkeys(reader.STATS, 0))
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    path = comic_file(tmp_path)
    assert reader.draw_ahead(path, 0, 600, 2, 1, "abcdef12") == 2
    stamp = reader._stamp(path)
    assert wait_for(lambda: reader.STATS["ahead"] == 2)
    assert reader._have((str(path), stamp, "zip", 2, 600)) and not reader._have((str(path), stamp, "zip", 3, 600))
    reader.page(path, 1, 600)
    assert reader.STATS["used"] == 1 and reader.STATS["drawn"] == 0 and reader.STATS["memory"] == 1


def test_past_the_last_page_it_stops(tmp_path, guesses, monkeypatch):
    monkeypatch.setattr(reader, "STATS", dict.fromkeys(reader.STATS, 0))
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    path = comic_file(tmp_path)
    reader.draw_ahead(path, 2, 600, 20, 1, "abcdef12")  # page 3 is the last; 4 and on aren't there
    assert wait_for(lambda: not guesses)
    assert reader.STATS["ahead"] == 1


def test_back_is_drawn_backwards_and_never_before_the_first(tmp_path, guesses, monkeypatch):
    monkeypatch.setattr(reader, "_ahead_worker", [threading.current_thread()])
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    path = comic_file(tmp_path)
    assert reader.draw_ahead(path, 2, 600, 8, -1, "abcdef12") == 2
    assert [n for _, n in guesses["abcdef12"]["pages"]] == [1, 0]


def test_a_reader_says_again_and_its_old_guesses_go(tmp_path, guesses, monkeypatch):
    monkeypatch.setattr(reader, "_ahead_worker", [threading.current_thread()])
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    path = comic_file(tmp_path)
    reader.draw_ahead(path, 0, 600, 3, 1, "abcdef12")
    reader.draw_ahead(path, 2, 1900, 1, 1, "abcdef12")  # a jump, on a turned screen
    assert [n for _, n in guesses["abcdef12"]["pages"]] == [3] and guesses["abcdef12"]["book"][3] == 1900
    reader.draw_ahead(path, 2, 600, 0, 1, "abcdef12")  # nothing more
    assert not guesses


def test_a_solid_rar_that_couldn_t_be_unpacked_is_drawn_ahead_from_the_archive(tmp_path, guesses, monkeypatch):
    monkeypatch.setattr(reader, "STATS", dict.fromkeys(reader.STATS, 0))
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    monkeypatch.setattr(reader, "cache_dir", lambda: tmp_path / "pages")
    monkeypatch.setattr(reader, "_failed", type(reader._failed)())
    monkeypatch.setattr(reader, "_unpacking", set())
    path = tmp_path / "Moonwake 005.cbr"
    path.write_bytes(b"Rar!\x1a\x07\x00" + bytes(100))  # a RAR's first bytes: the rest is the stand-ins below
    monkeypatch.setattr(reader, "_names", lambda path, stamp, kind: tuple(f"{n:03}.jpg" for n in range(40)))
    monkeypatch.setattr(reader, "_solid", lambda path, stamp: True)
    monkeypatch.setattr(reader, "_rar_read", lambda path, name: picture((0, 0, 200), size=(300, 450)))
    monkeypatch.setattr(reader, "_unpackable", lambda path: None)  # not to be unpacked: too little room on the disk, say
    stamp = reader._stamp(path)
    reader._unpacked(str(path), stamp)
    assert wait_for(lambda: not reader._unpacking)  # tried, and failed
    assert reader.draw_ahead(path, 24, 240, 2, 1, "abcdef12") == 2
    assert wait_for(lambda: reader.STATS["ahead"] == 2)  # not left for ever as if it were being unpacked
    assert reader._have((str(path), stamp, "rar", 26, 240))


def test_at_most_draw_ahead_and_none_on_battery(tmp_path, guesses, monkeypatch, toml):
    monkeypatch.setattr(reader, "_ahead_worker", [threading.current_thread()])
    path = tmp_path / "lib" / "c.cbz"
    make_comic(path)
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    toml("draw_ahead = 1\n")
    assert reader.draw_ahead(path, 0, 600, 30, 1, "abcdef12") == 1
    toml("draw_ahead = 0\n")
    assert reader.draw_ahead(path, 0, 600, 30, 1, "abcdef12") == 0
    toml("")
    monkeypatch.setattr(reader, "on_battery", lambda: True)
    assert reader.draw_ahead(path, 0, 600, 30, 1, "abcdef12") == 0 and not guesses


def test_the_readers_kept_at_once_have_a_limit(tmp_path, guesses, monkeypatch):
    monkeypatch.setattr(reader, "_ahead_worker", [threading.current_thread()])
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    path = comic_file(tmp_path)
    for i in range(reader.AHEAD_READERS + 5):
        reader.draw_ahead(path, 0, 600, 2, 1, f"reader{i:04d}")
    assert len(guesses) == reader.AHEAD_READERS and "reader0000" not in guesses  # the one heard from longest ago went


# ---- through the server ----

@pytest.fixture
def served(tmp_path, guesses, monkeypatch, serving):
    monkeypatch.setattr(reader, "on_battery", lambda: False)
    build_library(tmp_path)
    make_comic(tmp_path / "comics" / "Moonwake" / "Moonwake 001.cbz")
    catalog.build(tmp_path, log=lambda s: None)
    _, client = serving(tmp_path)
    comic = next(it for it in Catalog(tmp_path).get() if "Moonwake" in (reader.readable(it) or ""))
    return client, comic


def test_the_page_asks_the_server_to_draw_ahead(served):
    client, comic = served
    said = client.get("/api/ahead", params={"id": comic["id"], "n": 0, "w": 700, "k": 2, "d": 1, "t": "abcdef12"})
    assert said.status_code == 200 and said.json() == {"ok": True, "pages": 2} and said.headers["cache-control"] == "no-store"
    assert wait_for(lambda: reader.disk_used() > 0 and not reader._guesses)
    pages = client.get("/server/status").json()["pages"]
    assert pages["ahead"] >= 1 and pages["draw_ahead"] == 32 and pages["disk_limit"] > 0
    assert client.get("/api/session").json()["ahead"]["drawn"] == 32


@pytest.mark.parametrize("bad", [{"t": "x"}, {"t": "ABCDEF12"}, {"t": "abc/../def"}, {"d": 2}, {"n": -1}, {"k": -1}, {"w": 0}, {"n": "x"}])
def test_what_isnt_a_way_of_reading_ahead_is_refused(served, bad):
    client, comic = served
    ask = {"id": comic["id"], "n": 0, "w": 700, "k": 2, "d": 1, "t": "abcdef12", **bad}
    assert client.get("/api/ahead", params=ask).status_code == 400


def test_only_what_the_reader_can_open(served):
    client, _ = served
    assert client.get("/api/ahead", params={"id": "nothing", "n": 0, "w": 700, "k": 2, "d": 1, "t": "abcdef12"}).status_code == 404


# ---- a folder of its own: [reader] cache_folder ----

@pytest.fixture
def cache_folder(monkeypatch):
    """[reader] cache_folder set to what a test says, looked at afresh."""
    from hylly import paths

    monkeypatch.setattr(reader, "_cache_look", {"key": None, "at": -1e9, "root": None, "why": None})

    def put(folder) -> None:
        paths.settings_file().write_text(f"[reader]\ncache_folder = '{folder}'\n", encoding="utf-8")
        settings.reload()
        reader._cache_look["at"] = -1e9

    yield put
    settings.reload()


def test_a_folder_of_its_own_holds_the_cache(drawing, cache_folder, tmp_path):
    fast = tmp_path / "fast"
    fast.mkdir()
    cache_folder(fast)
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    assert list((fast / "drawn").rglob("*.webp")) and reader.cache_dir() == fast / "pages"
    said = reader.status()
    assert said["folder"] == str(fast) and said["problem"] is None and said["disk_held"] == 1012


def test_a_folder_that_isnt_there_isnt_made_and_pages_stay_in_memory(drawing, cache_folder, tmp_path, capsys):
    from hylly import paths

    made, _ = drawing
    away = tmp_path / "disk" / "hylly-cache"  # a disk not connected
    cache_folder(away)
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    forget_memory()
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    assert len(made) == 2 and not away.exists() and not away.parent.exists()  # drawn again, and nothing made in its place
    assert reader.cache_dir() == paths.config_dir() / "cache" / "pages"  # what must be unpacked to be read, meanwhile
    assert "isn't there" in reader.cache_problem() and "isn't there" in reader.status()["problem"]
    reader.cache_root()
    assert capsys.readouterr().err.count("cache_folder") == 1  # told once
    away.mkdir(parents=True)  # connected again: used at the next look
    reader._cache_look["at"] = -1e9
    reader._page("a.cbz", (1, 2), "zip", 1, 1700)
    assert list((away / "drawn").rglob("*.webp")) and reader.cache_problem() is None


def test_never_inside_the_library(drawing, cache_folder, tmp_path, monkeypatch):
    library = tmp_path / "lib"
    (library / "cache").mkdir(parents=True)
    monkeypatch.setattr(reader, "SERVED", [library])
    cache_folder(library / "cache")
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    assert "inside the library" in reader.cache_problem() and not (library / "cache" / "drawn").exists()


def test_a_folder_that_cant_be_written(drawing, cache_folder, tmp_path):
    from hylly import doctor

    stuck = tmp_path / "stuck"
    stuck.mkdir()
    (stuck / "drawn").write_text("a file where its folder would go", encoding="utf-8")
    cache_folder(stuck)
    assert "can't be written" in reader.cache_problem() and "BindPaths" in reader.cache_problem()
    found = [c for c in doctor._pages_memory() if "cache_folder" in c["says"]]
    assert found and found[0]["state"] == doctor.PROBLEM


def test_a_folder_by_its_full_path_only(cache_folder):
    cache_folder("hylly-cache")
    with pytest.raises(SystemExit, match="full path"):
        settings.get("reader")


def your_own(shared, *names):
    """Folders of your own under pages and drawn in the folder cache_folder names, each with a file, and read longer ago
    than anything Hylly keeps there."""
    for name in names:
        folder = shared / name
        folder.mkdir(parents=True)
        (folder / "letter.txt").write_bytes(b"x" * 50_000)
        os.utime(folder, (time.time() - 900, time.time() - 900))


def test_only_the_folders_hylly_made_are_counted_and_trimmed(drawing, cache_folder, tmp_path, monkeypatch, capsys):
    from hylly import doctor

    shared = tmp_path / "shared"  # a disk's top, say, whose pages and drawn hold things of your own
    your_own(shared, "pages/my-scanned-letters", "drawn/Lantern Wolf sketches")
    (shared / "drawn" / "notes.txt").write_text("mine", encoding="utf-8")
    unpacking = shared / "pages" / ("0" * 20 + ".part")  # a comic Hylly is unpacking: neither counted nor named
    unpacking.mkdir()
    monkeypatch.setattr(reader, "_unpacking", {"0" * 20})  # nor removed, as one cut short is
    cache_folder(shared)
    monkeypatch.setattr(reader, "cache_limit", lambda: 10_000)  # about nine pages
    for book, age in (("old", 300), ("mid", 200), ("new", 100)):
        for n in range(3):
            reader._page(f"{book}.cbz", (1, 2), "zip", n, 1700)
        folder = reader._drawn_file(f"{book}.cbz", (1, 2), "zip", 0, 1700).parent
        os.utime(folder, (time.time() - age, time.time() - age))
    for n in range(3):
        reader._page("fourth.cbz", (1, 2), "zip", n, 1700)
    left = {book for book in ("old", "mid", "new", "fourth")
            if reader._drawn_file(f"{book}.cbz", (1, 2), "zip", 0, 1700).exists()}
    assert left == {"mid", "new", "fourth"}  # Hylly's own still go, read longest ago first
    for mine in ("pages/my-scanned-letters", "drawn/Lantern Wolf sketches"):
        assert (shared / mine / "letter.txt").stat().st_size == 50_000  # older, but never Hylly's to remove
    assert (shared / "drawn" / "notes.txt").exists() and unpacking.exists()
    assert reader.disk_used() == sum(p.stat().st_size for p in (shared / "drawn").rglob("*.webp"))  # nor counted
    said = capsys.readouterr().err
    assert said.count("(my-scanned-letters)") == 1 and said.count("(Lantern Wolf sketches, notes.txt)") == 1  # said once
    assert "left as it is" in said and "0" * 20 not in said
    found = [c for c in doctor._pages_memory() if "my-scanned-letters" in c["says"]]
    assert found and found[0]["state"] == doctor.INFO and "cache_folder" in found[0]["next"]


def test_a_disk_nearly_full_takes_room_back_only_from_hyllys_folders(drawing, cache_folder, tmp_path):
    _, disk = drawing
    shared = tmp_path / "shared"
    your_own(shared, "pages/my-scanned-letters", "drawn/Lantern Wolf sketches")
    cache_folder(shared)
    disk.update(total=100 << 30, free=50 << 30)
    reader._page("a.cbz", (1, 2), "zip", 0, 1700)
    reader._page("b.cbz", (1, 2), "zip", 0, 1700)
    disk["free"] = 1 << 30  # under the twentieth the disk keeps free: every book in the cache goes
    reader._page("c.cbz", (1, 2), "zip", 0, 1700)
    assert reader.disk_used() == 0 and not list((shared / "drawn").rglob("*.webp"))
    for mine in ("pages/my-scanned-letters", "drawn/Lantern Wolf sketches"):
        assert (shared / mine / "letter.txt").stat().st_size == 50_000  # but nothing of yours


# ---- pages kept in memory: [reader] memory_mb, and never more than half of what is free ------------------------------

@pytest.fixture
def memory(monkeypatch):
    """Pages of a size, in a memory of its own, with so much free."""
    monkeypatch.setattr(reader, "_kept", type(reader._kept)())
    monkeypatch.setattr(reader, "_kept_size", [0])
    monkeypatch.setattr(reader, "_looked", {"at": -1e9, "room": None})
    monkeypatch.setattr(reader, "_make_page", lambda path, stamp, kind, n, width: (b"x" * (10 << 20), "image/webp"))
    free = {"now": 64 << 30}
    monkeypatch.setattr(reader, "memory_free", lambda: free["now"])
    return free


def pages(count: int) -> int:
    for n in range(count):
        reader._page("p", (1, 2), "pdf", n, 240)
    return reader._kept_size[0]


def test_the_pages_keep_to_what_is_set(toml, memory):
    toml("memory_mb = 32\n")
    assert pages(10) <= 32 << 20 and len(reader._kept) == 3


def test_a_big_setting_doesnt_take_more_than_half_of_whats_free(toml, memory):
    toml("memory_mb = 16384\n")
    memory["now"] = 100 << 20
    assert pages(30) <= 50 << 20
    assert len(reader._kept) == 5


def test_when_memory_runs_short_later_it_gives_back(toml, memory, monkeypatch):
    toml("memory_mb = 256\n")
    assert pages(20) == 200 << 20
    memory["now"] = 0
    monkeypatch.setitem(reader._looked, "at", -1e9)  # the next look
    reader._page("p", (1, 2), "pdf", 99, 240)
    assert reader._kept_size[0] <= 100 << 20  # half of what it held: what it holds counts as free
    monkeypatch.setitem(reader._looked, "at", -1e9)
    reader._page("p", (1, 2), "pdf", 98, 240)
    assert reader._kept_size[0] <= 50 << 20  # and so on, while nothing else is free


def test_they_settle_at_half_and_dont_creep_up(toml, memory, monkeypatch):
    toml("memory_mb = 16384\n")
    total = 1000 << 20
    for round_ in range(8):  # what they take is what's no longer free
        memory["now"] = total - reader._kept_size[0]
        monkeypatch.setitem(reader._looked, "at", -1e9)
        for n in range(40):
            reader._page("p", (1, 2), "pdf", 100 * round_ + n, 240)
        assert reader._kept_size[0] <= (total // 2)


def test_with_nothing_free_it_still_keeps_a_little(toml, memory):
    toml("memory_mb = 1024\n")
    memory["now"] = 0
    pages(10)
    assert 0 < reader._kept_size[0] <= reader.KEEP_LEAST


def test_a_computer_that_wont_say_gets_what_is_set(toml, memory, monkeypatch):
    toml("memory_mb = 32\n")
    monkeypatch.setattr(reader, "memory_free", lambda: None)
    assert pages(10) <= 32 << 20 and reader.memory_note() is None


def test_this_computer_says_whats_free():
    free = reader.memory_free()
    assert free is None or free > 1 << 20


def test_what_is_free_is_psutil_s_available_with_no_program_run_for_it(monkeypatch):
    """No vm_stat run while the pages' lock is held: a slow one held up every page request."""
    import subprocess

    import psutil

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no program is run to say what's free"))
    monkeypatch.setattr(psutil, "virtual_memory", lambda: namedtuple("Memory", "total available")(8 << 30, 3 << 30))
    assert reader.memory_free() == 3 << 30

    def wont_say():
        raise psutil.Error("not on this system")

    monkeypatch.setattr(psutil, "virtual_memory", wont_say)
    assert reader.memory_free() is None


def test_the_note_says_when_the_setting_is_too_much(toml, memory):
    toml("memory_mb = 1000\n")
    memory["now"] = 1 << 30
    assert "1,000 MB" in reader.memory_note() and "half" in reader.memory_note()
    assert "half" in str(doctor._pages_memory())
    memory["now"] = 8 << 30
    assert reader.memory_note() is None and doctor._pages_memory() == []


def test_pages_kept_in_memory_are_kept_by_their_size(memory, monkeypatch):
    """By what they take, to the limit keep_limit gives: with this computer's memory made up (64 GB free, as memory has
    it), so that one short of memory, where the limit is less, answers the same: the default memory_mb's worth."""
    made = []

    def make(path, stamp, kind, n, width):
        made.append(n)
        return (b"x" * (40 << 20) if n == 99 else b"x" * (20 << 20)), "image/webp"

    monkeypatch.setattr(reader, "_make_page", make)
    for n in range(10):
        reader._page("p", (1, 2), "pdf", n, 240)
    limit = reader.keep_limit()
    assert limit == settings.DEFAULTS["reader"]["memory_mb"] << 20
    assert reader._kept_size[0] <= limit and len(reader._kept) == limit // (20 << 20)
    reader._page("p", (1, 2), "pdf", 9, 240)
    assert made.count(9) == 1  # from memory
    reader._page("p", (1, 2), "pdf", 99, 240)
    reader._page("p", (1, 2), "pdf", 99, 240)
    assert made.count(99) == 2  # too big to keep
    assert sum(len(found[0]) for found in reader._kept.values()) == reader._kept_size[0]
