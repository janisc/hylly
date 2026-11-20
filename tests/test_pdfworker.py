"""PDFs drawn in a process of their own: a worker that crashes, hangs or talks nonsense costs a page, not the server."""

from __future__ import annotations

import contextlib
import io
import os
import struct
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image

from hylly import doctor, meta, pdfworker, reader, serve

from helpers import build_library, make_pdf, write_book


# A stand-in for a worker, to see what the server does when one misbehaves. Only the first thing it is asked counts.
HEAD = """
import json, os, struct, sys, time
def say(head, data=b""):
    text = json.dumps(head).encode()
    sys.stdout.buffer.write(struct.pack(">IQ", len(text), len(data)) + text + data)
    sys.stdout.buffer.flush()
def asked():
    sizes = sys.stdin.buffer.read(12)
    if len(sizes) < 12:
        sys.exit()
    n, m = struct.unpack(">IQ", sizes)
    head = json.loads(sys.stdin.buffer.read(n))
    sys.stdin.buffer.read(m)
    return head
"""
READY = 'say({"ready": True})\n'


@pytest.fixture
def workers(monkeypatch):
    """A pool of its own, and the given script as its worker. Without one, real workers."""
    pool = pdfworker.Pool()
    monkeypatch.setattr(pdfworker, "POOL", pool)
    monkeypatch.setattr(pdfworker, "_stops", {})
    monkeypatch.setattr(pdfworker, "_turns", {})

    def behaving(script):
        monkeypatch.setattr(pdfworker, "_command", lambda: [sys.executable, "-I", "-c", HEAD + script])

    yield behaving
    pool.close()


# ---- what it does when all is well ----------------------------------------------------------

def test_a_book_says_how_many_pages_each_part_takes(tmp_path, workers):
    path = tmp_path / "a.epub"
    write_book(path)
    counts = reader.sections(path)
    assert counts and sum(counts) == reader.count(path) and all(n >= 1 for n in counts)
    assert reader.sections(make_pdf(tmp_path / "a.pdf")) == []  # a PDF's pages are its own


def test_a_worker_s_odd_answer_about_parts_is_no_answer(monkeypatch):
    for odd in ({"sections": "lots"}, {"sections": [1, -1]}, {"sections": [1.5]}, {}):
        monkeypatch.setattr(pdfworker, "_ask", lambda *a, **k: (odd, b""))  # noqa: B023
        assert pdfworker.sections("p", (1, 2)) == []


def test_a_page_comes_from_a_worker_not_from_this_process(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    assert reader.count(path) == 3
    data, ctype = reader.page(path, 1, 600)
    assert ctype == "image/webp"
    with Image.open(io.BytesIO(data)) as im:
        assert im.width == 600
    assert [w.process.pid != os.getpid() for w in pdfworker.POOL.idle] == [True]


def test_a_worker_is_used_again_and_keeps_the_file_open(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    reader.count(path)
    first = pdfworker.POOL.idle[0].process.pid
    reader.page(path, 0, 240)
    reader.page(path, 1, 240)
    assert [w.process.pid for w in pdfworker.POOL.idle] == [first]


def test_printing_mupdf_cant_upset_the_answers(tmp_path, workers):
    """A broken file makes MuPDF write warnings. Were they to land among the answers, every page after it would be wrong."""
    good = make_pdf(tmp_path / "good.pdf")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(good.read_bytes()[:200] + b"\nnot what it was\n" + good.read_bytes()[300:])
    try:
        reader.count(broken)
    except reader.Unreadable:
        pass
    assert reader.count(good) == 3 and reader.page(good, 0, 240)[1] == "image/webp"


def test_the_wrong_page_and_the_locked_file_are_answers_not_crashes(tmp_path, workers):
    import pymupdf

    path = make_pdf(tmp_path / "a.pdf")
    with pytest.raises(IndexError):
        reader.page(path, 3, 240)
    locked = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="x")
    doc.close()
    with pytest.raises(reader.Unreadable, match="password"):
        reader.count(locked)
    not_a_pdf = tmp_path / "not.pdf"
    not_a_pdf.write_bytes(b"%PDF-1.7\n" + b"nothing" * 50)
    with pytest.raises(reader.Unreadable):
        reader.page(not_a_pdf, 0, 240)
    worker = pdfworker.POOL.idle[0]
    assert worker.process.poll() is None and not pdfworker._stops  # all that cost no worker


def test_a_name_that_isnt_proper_text_goes_there_and_back(tmp_path, workers):
    path = make_pdf(tmp_path / "Tähti ☆ 星.pdf")
    assert reader.count(path) == 3
    if sys.platform == "win32":
        return
    odd = tmp_path / os.fsdecode(b"odd-\xff.pdf")
    try:
        os.replace(make_pdf(tmp_path / "plain.pdf"), odd)
    except OSError:
        pytest.skip("this file system takes no such name")
    worker = pdfworker.POOL.idle[0]
    try:
        assert reader.count(odd) == 3
    except reader.Unreadable:  # MuPDF can't open it by that name: an answer, as it was before workers
        pass
    assert pdfworker.POOL.idle == [worker] and not pdfworker._stops  # and either way the name got there and back


def test_a_file_planted_where_the_server_started_isnt_loaded(tmp_path, workers, monkeypatch):
    (tmp_path / "pymupdf.py").write_text("raise SystemExit('planted')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert reader.count(make_pdf(tmp_path / "a.pdf")) == 3


def test_details_for_the_catalog_come_from_a_worker(tmp_path, workers):
    m = meta.read_pdf(make_pdf(tmp_path / "a.pdf", title="The Moonwake Atlas"))
    assert (m.title, m.authors, m.year, m.pages) == ("The Moonwake Atlas", ["Pat Sample"], "2019", 3) and m.subjects == ["dice", "maps"]
    with Image.open(io.BytesIO(m.cover)) as im:
        assert im.format == "PNG" and im.width == 480


def test_a_pdf_inside_a_zip_gets_there_as_a_file_of_its_own(tmp_path, workers):
    import zipfile

    pdf = make_pdf(tmp_path / "inner.pdf", pages=2)
    package = tmp_path / "book.zip"
    with zipfile.ZipFile(package, "w") as z:
        z.write(pdf, "Inner Book.pdf")
    pdf.unlink()
    m = meta.read_package(package)
    assert m.pages == 2 and m.cover.startswith(b"\x89PNG")


def test_two_can_draw_at_once(tmp_path, workers):
    """One stuck page leaves the other worker free for everybody else."""
    workers(READY + """
while True:
    head = asked()
    if "stuck" in head.get("path", ""):
        time.sleep(30)
    say({"pages": 3})
""")
    stuck = make_pdf(tmp_path / "stuck.pdf")
    other = make_pdf(tmp_path / "other.pdf")

    def ask_it():
        with contextlib.suppress(reader.Unreadable):  # it never does get an answer worth having
            reader.count(stuck)

    threading.Thread(target=ask_it, daemon=True).start()
    time.sleep(0.5)
    began = time.monotonic()
    assert reader.count(other) == 3 and time.monotonic() - began < 15


# ---- when a worker fails ----------------------------------------------------------------------

CRASHES = READY + "asked()\nos._exit(3)\n"


def test_a_crash_costs_that_page_and_the_next_one_starts_a_worker(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    workers(CRASHES)
    with pytest.raises(reader.Unreadable, match="stopped the part of Hylly"):
        reader.page(path, 0, 240)
    assert pdfworker.POOL.idle == []
    workers(READY + """
while True:
    asked()
    say({}, b"a page")
""")
    with pytest.raises(reader.Unreadable, match="left alone"):  # that page isn't tried again for now
        reader.page(path, 0, 240)
    assert reader.page(path, 1, 240)[0] == b"a page"  # the others are


def test_a_crash_is_told_in_the_log_with_the_file(tmp_path, workers, capsys):
    path = make_pdf(tmp_path / "a.pdf")
    workers(CRASHES)
    with pytest.raises(reader.Unreadable):
        reader.page(path, 4, 240)
    said = capsys.readouterr().err
    assert str(path) in said and "page 5" in said


def test_three_pages_and_the_whole_file_is_left_alone_until_it_changes(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    workers(CRASHES)
    for n in range(3):
        with pytest.raises(reader.Unreadable, match="stopped the part"):
            reader.page(path, n, 240)
    with pytest.raises(reader.Unreadable, match="left alone"):
        reader.page(path, 7, 240)
    make_pdf(path, pages=2)  # a new file is a new file
    os.utime(path, (time.time() + 5, time.time() + 5))
    with pytest.raises(reader.Unreadable, match="stopped the part"):
        reader.page(path, 7, 240)


def test_a_file_that_cant_even_be_opened_is_left_alone_altogether(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    workers(CRASHES)
    with pytest.raises(reader.Unreadable, match="stopped the part"):
        reader.count(path)
    with pytest.raises(reader.Unreadable, match="left alone"):
        reader.page(path, 0, 240)
    assert reader.outline(path) == []  # a book without its outline still reads


def test_what_stopped_a_worker_is_forgotten_after_a_while(tmp_path, workers, monkeypatch):
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "FORGET", -1.0)
    workers(CRASHES)
    with pytest.raises(reader.Unreadable):
        reader.count(path)
    workers(READY + 'asked()\nsay({"pages": 3})\n')
    assert reader.count(path) == 3


def test_a_hang_is_stopped_by_the_time_limit(tmp_path, workers, monkeypatch):
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "LIMIT", 1.0)
    workers(READY + "asked()\ntime.sleep(60)\n")
    began = time.monotonic()
    with pytest.raises(reader.Unreadable, match="too long"):
        reader.page(path, 0, 240)
    assert time.monotonic() - began < 15
    assert pdfworker.POOL.idle == []


def test_a_worker_keeps_to_the_limit_by_itself_should_the_server_be_gone():
    """A worker stuck in MuPDF can't hear that the server has gone, so it has a clock of its own, which runs apart from
    Python, and nobody here stops it: it ends by itself. The real main, with a page that never ends."""
    script = ("import time\n"
              "from hylly import pdfworker\n"
              "pdfworker._do = lambda *a: time.sleep(60)\n"
              "pdfworker.main()\n")
    worker = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert pdfworker._receive(worker.stdout, 100)[0] == {"ready": True}
        began = time.monotonic()
        pdfworker._send(worker.stdin, {"do": "page", "seconds": -4})  # the worker adds its 5 seconds: one second
        assert worker.wait(30) == 1 and time.monotonic() - began < 15
    finally:
        worker.kill()
        worker.wait(10)
        worker.stdin.close()
        worker.stdout.close()


@pytest.mark.parametrize("nonsense", [
    'sys.stdout.buffer.write(struct.pack(">IQ", 1 << 31, 0)); sys.stdout.buffer.flush(); time.sleep(5)',  # far too long a head
    'sys.stdout.buffer.write(struct.pack(">IQ", 2, 1 << 40) + b"{}"); sys.stdout.buffer.flush(); time.sleep(5)',  # or far too many bytes
    'sys.stdout.buffer.write(struct.pack(">IQ", 2, 200 << 20) + b"{}"); sys.stdout.buffer.flush(); time.sleep(5)',  # more than any page
    'sys.stdout.buffer.write(struct.pack(">IQ", 5, 0) + b"[[[[["); sys.stdout.buffer.flush(); time.sleep(5)',  # not JSON
    'say(["not", "a", "head"])',
    'text = b"[" * 200000; sys.stdout.buffer.write(struct.pack(">IQ", len(text), 0) + text); sys.stdout.buffer.flush(); time.sleep(5)',  # too deep to read
])
def test_nonsense_from_a_worker_is_a_stopped_worker(tmp_path, workers, nonsense):
    path = make_pdf(tmp_path / "a.pdf")
    workers(READY + f"asked()\n{nonsense}\n")
    with pytest.raises(reader.Unreadable, match="stopped the part"):
        reader.page(path, 0, 240)
    assert pdfworker.POOL.idle == []


@pytest.mark.parametrize("answer, ask", [
    ('{"pages": "many"}', "count"), ('{"pages": -1}', "count"), ('{"pages": true}', "count"), ('{"toc": 7}', "outline"),
    ('{"toc": [["a", 1]]}', "outline"), ('{"toc": [[1, 5, 1]]}', "outline"),
])
def test_an_answer_of_the_wrong_shape_goes_no_further(tmp_path, workers, answer, ask):
    path = make_pdf(tmp_path / "a.pdf")
    workers(READY + f"while True:\n    asked()\n    say({answer})\n")
    if ask == "count":
        with pytest.raises(reader.Unreadable):
            reader.count(path)
    else:
        assert reader.outline(path) == []


def test_a_worker_that_cant_start_is_no_fault_of_the_file(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    workers("sys.exit(1)\n")
    with pytest.raises(reader.Unreadable, match="can't start"):
        reader.page(path, 0, 240)
    workers('say({"cannot": "ImportError: no MuPDF here"})\n')
    with pytest.raises(reader.Unreadable, match="no MuPDF here"):
        reader.page(path, 0, 240)
    assert not pdfworker._stops
    workers(READY + "while True:\n    asked()\n    say({}, b'a page')\n")
    assert reader.page(path, 0, 240)[0] == b"a page"


def test_a_worker_gone_while_idle_is_no_fault_of_the_next_file(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    assert reader.count(path) == 3
    worker = pdfworker.POOL.idle[0]
    worker.process.kill()
    worker.process.wait(10)
    assert reader.page(path, 0, 240)[1] == "image/webp"
    assert not pdfworker._stops


def test_the_catalog_goes_on_past_a_file_that_crashes(tmp_path, workers):
    path = make_pdf(tmp_path / "Moonwake Atlas.pdf")
    workers(CRASHES)
    m = meta.read(path)
    assert m.source == "unreadable" and m.title == "Moonwake Atlas" and m.cover is None


# ---- their keeping ----------------------------------------------------------------------------

def test_an_unused_worker_goes_and_lets_go_of_its_files(tmp_path, workers, monkeypatch):
    reader.count(make_pdf(tmp_path / "a.pdf"))
    worker = pdfworker.POOL.idle[0]
    pdfworker.POOL.tidy()
    assert pdfworker.POOL.idle == [worker]  # not yet
    monkeypatch.setattr(pdfworker, "IDLE", 0.0)
    pdfworker.POOL.tidy()
    assert pdfworker.POOL.idle == [] and worker.process.wait(10) == 0  # it stopped by itself


def test_seeing_to_the_idle_ones_waits_and_ends_with_the_pool(tmp_path, workers, monkeypatch):
    """Not by time.sleep, which tests replace with something instant: it would run flat out, and it did. And a pool that
    is closed leaves no thread behind."""
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setattr(pdfworker, "IDLE", 0.0123)  # a time nothing else sleeps for: waiting for a process does, elsewhere than Windows
    before = set(threading.enumerate())
    reader.count(make_pdf(tmp_path / "a.pdf"))
    seeing_to_them = [t for t in set(threading.enumerate()) - before if t.name == "pdf-workers"]
    assert len(seeing_to_them) == 1
    end = time.monotonic() + 10
    while pdfworker.POOL.idle and time.monotonic() < end:
        threading.Event().wait(0.02)
    assert pdfworker.POOL.idle == [] and 0.0123 not in slept  # gone when unused, and not by sleeping
    pdfworker.POOL.close()
    seeing_to_them[0].join(5)
    assert not seeing_to_them[0].is_alive()
    assert reader.count(tmp_path / "a.pdf") == 3  # usable after that
    assert 0.0123 not in slept


def test_a_worker_that_has_done_a_lot_is_replaced(tmp_path, workers, monkeypatch):
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "RECYCLE", 2)
    reader.count(path)
    worker = pdfworker.POOL.idle[0]
    reader.page(path, 0, 240)
    assert worker not in pdfworker.POOL.idle and worker.process.wait(10) == 0


def test_no_more_than_two_at_once(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    threads = [threading.Thread(target=reader.page, args=(path, n % 3, 240 + n)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert 1 <= len(pdfworker.POOL.idle) <= pdfworker.WORKERS


def test_a_worker_ends_when_the_server_does(tmp_path):
    script = ("import sys, time\n"
              "from hylly import pdfworker\n"
              "pdfworker.versions()\n"
              "print(pdfworker.POOL.idle[0].process.pid, flush=True)\n"
              "import os; os._exit(0)\n")  # no goodbye: as if it had crashed
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    import psutil

    pid = int(done.stdout.split()[-1])

    def still_there():
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    end = time.monotonic() + 20
    while still_there() and time.monotonic() < end:
        time.sleep(0.1)
    assert not still_there()


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS doesn't keep to it")
def test_a_worker_cant_have_all_the_memory():
    script = ("from hylly import pdfworker\n"
              "print(pdfworker.limit_memory(600 << 20))\n"
              "try:\n"
              "    held = bytearray(1500 << 20)\n"
              "    print('got it')\n"
              "except MemoryError:\n"
              "    print('refused')\n")
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.stdout.split() == ["True", "refused"], done.stderr


def test_how_many_workers_and_their_memory_go_by_the_computer():
    """Two workers at 3 GB each, whatever the computer: on 1 or 2 GB the limit protected nothing, and with many cores
    two were the most. Now from its cores and memory: as before on 4 cores and 7.5 GB, one with nine tenths of the
    memory at most under 3 GB, up to four with 8 cores and 16 GB, never more than the memory has room for; and two at
    3 GB on one that won't say. The reader's 7-Zips and pictures decoded at a time go with them."""
    gb = 1 << 30
    assert pdfworker.capacity(4, int(7.5 * gb)) == (2, 3 * gb)  # 4 cores and 7.5 GB, by either count of its memory
    assert pdfworker.capacity(4, int(7.5 * 10**9)) == (2, 3 * gb)
    for memory in (1, 2):
        for cores in (1, 2, 4, 8, 16):
            workers, each = pdfworker.capacity(cores, memory * gb)
            assert workers == 1 and memory * gb * 0.85 <= each <= memory * gb * 0.9
    assert {pdfworker.capacity(cores, 4 * gb) for cores in (1, 2, 4, 8, 16)} == {(2, 4 * gb * 9 // 10 // 2)}
    assert {pdfworker.capacity(cores, 8 * gb) for cores in (1, 2, 4, 8, 16)} == {(2, 3 * gb)}
    assert [pdfworker.capacity(cores, 16 * gb)[0] for cores in (1, 2, 4, 6, 8, 16)] == [2, 2, 2, 3, 4, 4]
    assert pdfworker.capacity(64, 256 * gb) == (4, 3 * gb) and pdfworker.capacity(None, 16 * gb) == (2, 3 * gb)
    assert pdfworker.capacity(8, None) == pdfworker.capacity(None, None) == (2, 3 * gb)
    assert pdfworker.capacity(4, 1) == pdfworker.capacity(4, 100 << 20) == (2, 3 * gb)  # a reading that can't be right
    for cores in (1, 2, 4, 8, 16):
        for memory in (1, 2, 4, 8, 16):
            workers, each = pdfworker.capacity(cores, memory * gb)
            assert workers * each <= memory * gb * 9 // 10 and each <= 3 * gb
    cores, memory = pdfworker.this_computer()  # what this computer says, as the server goes by it
    assert (cores is None or cores >= 1) and (memory is None or memory > 256 << 20)
    assert (pdfworker.WORKERS, pdfworker.MEMORY) == pdfworker.capacity(cores, memory)
    assert reader.UNPACKS._initial_value == pdfworker.WORKERS and reader.DECODES._initial_value == 2 * pdfworker.WORKERS


# ---- what the doctor says draws the PDFs, and a worker's messages ----------------------------------------------------


def test_the_doctor_says_what_really_draws_the_pdfs(workers):
    found = [c for c in doctor.report()["checks"] if c["what"] == "PDFs"]
    import pymupdf

    assert found[0]["state"] == doctor.OK and pymupdf.mupdf_version in found[0]["says"]
    pdfworker.POOL.close()
    workers("sys.exit(1)\n")
    found = [c for c in doctor.report()["checks"] if c["what"] == "PDFs"]
    assert found[0]["state"] == doctor.PROBLEM and "can't start" in found[0]["says"]


def test_a_message_is_a_head_and_bytes():
    out = io.BytesIO()
    pdfworker._send(out, {"a": "ä"}, b"bytes")
    out.seek(0)
    assert pdfworker._receive(out, 100) == ({"a": "ä"}, b"bytes")
    out.seek(0)
    with pytest.raises(ValueError):
        pdfworker._receive(out, 2)
    with pytest.raises(EOFError):
        pdfworker._receive(io.BytesIO(struct.pack(">IQ", 5, 0) + b"{}"), 100)
    out = io.BytesIO()
    pdfworker._send(out, {"path": "odd-\udcff"})  # how Python holds a file's name that isn't proper text
    out.seek(0)
    assert pdfworker._receive(out, 100)[0] == {"path": "odd-\udcff"}


# ---- files that hang or crash, workers left behind, and the blame laid where it belongs ---------

def main_with(script):
    """The real main of a worker, with something of a test's put in first."""
    return [sys.executable, "-c", "import os, sys, time\nfrom hylly import pdfworker\n" + script + "\npdfworker.main()\n"]


@pytest.mark.slow
def test_a_file_that_hangs_takes_one_worker_and_only_three_turns(tmp_path, workers, monkeypatch):
    """Twelve pages asked of a file that hangs: three of them run to the limit, not twelve, and another file goes on
    meanwhile. With the real proportions: each is forgiven as soon as the limit has gone by again."""
    monkeypatch.setattr(pdfworker, "LIMIT", 2.0)
    monkeypatch.setattr(pdfworker, "FORGET_LATE", 2.0)
    workers(READY + """
while True:
    head = asked()
    if "stuck" in head.get("path", ""):
        time.sleep(30)
    say({"pages": 3})
""")
    stuck = make_pdf(tmp_path / "stuck.pdf")
    said = []

    def ask(n):
        try:
            reader.page(stuck, n, 240)
        except reader.Unreadable as e:
            said.append(str(e))

    began = time.monotonic()
    threads = [threading.Thread(target=ask, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    assert reader.count(make_pdf(tmp_path / "other.pdf")) == 3 and any(t.is_alive() for t in threads)  # those still at it
    for t in threads:
        t.join(30)
    assert sum("too long" in s for s in said) == 3 and sum("left alone" in s for s in said) == 9
    assert time.monotonic() - began < 30


def test_waiting_for_a_turn_has_an_end_for_a_reader_and_none_for_the_catalog(tmp_path, workers, monkeypatch):
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "PATIENCE", 0.2)
    pool = pdfworker.Pool(size=1)
    monkeypatch.setattr(pdfworker, "POOL", pool)
    pool.room.acquire()  # the only room is taken
    with pytest.raises(pdfworker.NotNow, match="busy"):
        reader.count(path)
    key = (str(path), reader._stamp(path))
    with pdfworker._turn(key, None):  # this file's turn is taken
        with pytest.raises(pdfworker.NotNow, match="still busy"):
            reader.count(path)
    pool.room.release()
    assert not pdfworker._stops and not pdfworker._turns


def test_a_program_a_worker_left_behind_cant_jam_the_drawing(tmp_path, workers, monkeypatch):
    """Stopping the worker doesn't end the waiting if something else holds its pipe open. The time is kept all the same."""
    monkeypatch.setattr(pdfworker, "LIMIT", 1.0)
    path = make_pdf(tmp_path / "a.pdf")
    workers(READY + """
import subprocess
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"], stdin=subprocess.DEVNULL, stdout=sys.stdout.fileno(), stderr=subprocess.DEVNULL)
asked()
time.sleep(60)
""")
    began = time.monotonic()
    with pytest.raises(pdfworker.NotNow, match="too long"):
        reader.page(path, 0, 240)
    assert time.monotonic() - began < 8  # before that program has ended
    assert pdfworker.POOL.room.acquire(timeout=0)  # and the room is free
    pdfworker.POOL.room.release()


def test_a_slow_page_is_forgiven_sooner_than_a_crash(tmp_path, workers, monkeypatch):
    """A share's stall looks just like it, and says nothing about the file."""
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "LIMIT", 0.5)
    monkeypatch.setattr(pdfworker, "FORGET_LATE", 0.0)
    workers(READY + "asked()\ntime.sleep(60)\n")
    with pytest.raises(pdfworker.NotNow, match="too long"):
        reader.count(path)
    workers(READY + 'asked()\nsay({"pages": 3})\n')
    assert reader.count(path) == 3  # opening it took too long once: not held against the file
    assert pdfworker.FORGET > pdfworker.FORGET_LATE


def test_a_stop_at_the_outline_leaves_the_book(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    workers(CRASHES)
    assert reader.outline(path) == []
    workers(READY + 'while True:\n    asked()\n    say({"pages": 3})\n')
    assert reader.count(path) == 3  # the book still reads
    assert reader.outline(path) == []  # and its outline is left alone


def test_a_worker_that_has_read_other_things_gets_the_blame_not_the_next_file(tmp_path, workers):
    one, two = make_pdf(tmp_path / "one.pdf"), make_pdf(tmp_path / "two.pdf")
    workers(READY + """
asked()
say({"pages": 3})
if asked().get("path"):
    os._exit(3)  # whatever the first did, it ends now
""")
    assert reader.count(one) == 3
    worker = pdfworker.POOL.idle[0]
    assert reader.count(two) == 3 and not pdfworker._stops  # a new one read it well
    assert worker not in pdfworker.POOL.idle


def test_a_file_that_also_stops_a_new_worker_gets_the_blame(tmp_path, workers):
    good, bad = make_pdf(tmp_path / "good.pdf"), make_pdf(tmp_path / "bad.pdf")
    workers(READY + """
while True:
    if "bad" in asked().get("path", ""):
        os._exit(3)
    say({"pages": 3})
""")
    assert reader.count(good) == 3
    with pytest.raises(reader.Unreadable, match="stopped the part"):
        reader.count(bad)
    assert list(pdfworker._stops) == [(str(bad), reader._stamp(bad))]


def test_an_empty_pdf_inside_a_zip_isnt_the_zip(tmp_path, workers):
    """Which MuPDF would otherwise read as a comic, of its pictures."""
    import zipfile

    picture = io.BytesIO()
    Image.new("RGB", (30, 40), "red").save(picture, "PNG")
    package = tmp_path / "book.zip"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("Empty.pdf", b"")
        for n in range(3):
            z.writestr(f"p{n}.png", picture.getvalue())
    m = meta.read(package)
    assert m.source == "unreadable" and not m.pages and not m.cover


def make_item(root, path):
    from hylly import catalog

    return catalog.Item(id="t", source="own", kind="book", title="Moonwake", files=[(path.relative_to(root).as_posix(), "pdf", path.stat().st_size)])


def test_the_catalog_doesnt_keep_a_workers_trouble_as_the_files_own(tmp_path, workers):
    from hylly import catalog

    library = tmp_path / "library"
    (library / "own").mkdir(parents=True)
    (library / "catalog" / "covers").mkdir(parents=True)
    path = make_pdf(library / "own" / "Moonwake.pdf", title="The Moonwake Atlas")
    cache = catalog.FileCache(library / "catalog" / "cache.jsonl")
    rel = path.relative_to(library).as_posix()
    workers("sys.exit(1)\n")  # no worker: a run in which nothing could be drawn
    item = make_item(library, path)
    catalog.read_item(library, library / "catalog", cache, item)
    assert item.meta["source"] == "unreadable" and "not_now" not in item.meta
    assert cache.get(rel, path.stat().st_size, path.stat().st_mtime) is None  # not kept, unlike a broken file
    workers(CRASHES)  # a worker stopped by this very file is another matter
    crashed = make_item(library, path)
    catalog.read_item(library, library / "catalog", cache, crashed)
    assert cache.get(rel, path.stat().st_size, path.stat().st_mtime)["meta"]["source"] == "unreadable"


def test_a_stall_while_reading_isnt_kept_either(tmp_path, workers, monkeypatch):
    monkeypatch.setattr(pdfworker, "LIMIT", 0.5)
    workers(READY + "asked()\ntime.sleep(60)\n")
    m = meta.read(make_pdf(tmp_path / "a.pdf"))
    assert m.not_now and m.source == "unreadable" and "not_now" not in m.to_dict()


def test_a_document_that_failed_is_opened_anew():
    class Doc:
        closed = False

        def close(self):
            self.closed = True

    doc = Doc()
    open_ones = type(reader._kept)({("p", (1, 2)): doc})
    pdfworker._forget_document({"path": "p", "stamp": [1, 2]}, open_ones)
    assert not open_ones and doc.closed
    pdfworker._forget_document({"do": "versions"}, open_ones)  # nothing to forget


def test_a_worker_has_its_memory_limit_and_says_so(workers):
    assert pdfworker.versions()["limited"] is (sys.platform != "darwin")


def test_what_mupdf_prints_goes_nowhere_near_the_answers(tmp_path, workers, monkeypatch):
    """Both as Python prints and as C does, to the same place the answers would go."""
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "_command", lambda: main_with(
        "def noisy(ask, data, documents):\n"
        "    print('from Python', flush=True)\n"
        "    os.write(1, b'from C')\n"
        "    return {'pages': 9}, b''\n"
        "pdfworker._do = noisy"))
    assert reader.count(path) == 9


@pytest.mark.skipif(sys.platform == "win32", reason="no such signal to send")
def test_ctrl_c_in_a_terminal_isnt_for_a_worker(workers):
    import signal

    worker = subprocess.Popen(main_with(""), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert pdfworker._receive(worker.stdout, 100)[0]["ready"]
        worker.send_signal(signal.SIGINT)
        pdfworker._send(worker.stdin, {"do": "versions", "seconds": 30})
        assert "mupdf" in pdfworker._receive(worker.stdout, 100)[0]
    finally:
        worker.kill()
        worker.wait(10)
        worker.stdin.close()
        worker.stdout.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux and macOS")
def test_a_crashed_worker_leaves_no_copy_of_its_memory():
    script = "import resource\nfrom hylly import pdfworker\npdfworker.limit_memory()\nprint(resource.getrlimit(resource.RLIMIT_CORE))\n"
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "(0, 0)", done.stderr


def test_a_book_for_a_borrower_is_laid_out_at_its_page_size(tmp_path, workers):
    import zipfile

    text = "".join(f"<p>Paragraph {n}: the lantern road ran on past the harbour lights.</p>" for n in range(200))
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Lantern Road</dc:title></metadata><manifest>'
                   '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/>'
                   '</spine></package>')
        z.writestr("c1.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>One</h1>{text}</body></html>')
    assert reader.count(path) > 1
    with Image.open(io.BytesIO(reader.page(path, 0, 600)[0])) as im:
        width, height, _ = pdfworker.BOOK_PAGE
        assert abs(im.height / im.width - height / width) < 0.01


def test_a_name_in_the_log_cant_start_a_line_of_its_own(capsys):
    pdfworker._note_stop(("a b\u0085c\nd", (1, 2)), (0, 240), "why", False)
    pdfworker._stops.pop(("a b\u0085c\nd", (1, 2)))
    said = capsys.readouterr().err
    assert len(said.splitlines()) == 1 and said.startswith("a?b?c?d, page 1: why")
    serve._stamp("tried as a b\u0085c")
    assert len(capsys.readouterr().err.splitlines()) == 1


# ---- one bad page at any width, retries that end, and a share's trouble that isn't the file's ------

def test_one_bad_page_is_one_bad_page_at_any_width(tmp_path, workers):
    """The reader asks for a page as a thumbnail, at reading width and zoomed: that is one page, not three."""
    path = make_pdf(tmp_path / "a.pdf")
    workers(READY + """
while True:
    if asked().get("n") == 0:
        os._exit(3)
    say({}, b"a page")
""")
    with pytest.raises(reader.Unreadable, match="stopped the part"):
        reader.page(path, 0, 240)
    for width in (1200, 3200):
        with pytest.raises(reader.Unreadable, match="left alone"):  # a crash is the page's, at any width
            reader.page(path, 0, width)
    assert reader.page(path, 1, 240)[0] == b"a page" and reader.page(path, 5, 600)[0] == b"a page"


@pytest.mark.slow
def test_a_page_too_slow_at_three_widths_is_still_one_page(tmp_path, workers, monkeypatch):
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "LIMIT", 1.0)
    workers(READY + """
while True:
    if asked().get("n") == 0:
        time.sleep(60)
    say({}, b"a page")
""")
    for width in (240, 1200, 3200):
        with pytest.raises(pdfworker.NotNow, match="too long"):
            reader.page(path, 0, width)
    assert reader.page(path, 1, 240)[0] == b"a page"


def test_taking_too_long_at_one_width_leaves_the_others(tmp_path, workers, monkeypatch):
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "LIMIT", 1.0)
    workers(READY + """
while True:
    if asked().get("width") == 3200:
        time.sleep(60)
    say({}, b"a page")
""")
    with pytest.raises(pdfworker.NotNow, match="too long"):
        reader.page(path, 0, 3200)
    assert reader.page(path, 0, 600)[0] == b"a page"
    with pytest.raises(pdfworker.NotNow, match="left alone"):
        reader.page(path, 0, 3200)


def test_a_stop_at_the_outline_doesnt_count_towards_the_three_pages(tmp_path, workers):
    path = make_pdf(tmp_path / "a.pdf")
    workers(CRASHES)
    assert reader.outline(path) == []
    workers(READY + "while True:\n    asked()\n    os._exit(3)\n")
    for n in range(2):
        with pytest.raises(reader.Unreadable, match="stopped the part"):
            reader.page(path, n, 240)
    workers(READY + "while True:\n    asked()\n    say({}, b'a page')\n")
    assert reader.page(path, 2, 240)[0] == b"a page"  # two pages and an outline aren't three pages


def test_the_room_and_the_workers_are_all_back_when_the_retry_cant_start(tmp_path, workers):
    import psutil

    one, two = make_pdf(tmp_path / "one.pdf"), make_pdf(tmp_path / "two.pdf")
    before = {p.pid for p in psutil.Process().children()}
    workers(READY + 'asked()\nsay({"pages": 3})\nasked()\nos._exit(3)\n')
    assert reader.count(one) == 3
    workers("sys.exit(1)\n")
    with pytest.raises(pdfworker.NotNow, match="can't start"):
        reader.count(two)
    for _ in range(pdfworker.WORKERS):
        assert pdfworker.POOL.room.acquire(timeout=0)
    for _ in range(pdfworker.WORKERS):
        pdfworker.POOL.room.release()
    assert pdfworker.POOL.idle == [] and not ({p.pid for p in psutil.Process().children()} - before)


def test_a_retried_worker_that_stops_too_is_gone_as_well(tmp_path, workers):
    import psutil

    good, bad = make_pdf(tmp_path / "good.pdf"), make_pdf(tmp_path / "bad.pdf")
    before = {p.pid for p in psutil.Process().children()}
    workers(READY + """
while True:
    if "bad" in asked().get("path", ""):
        os._exit(3)
    say({"pages": 3})
""")
    assert reader.count(good) == 3
    with pytest.raises(reader.Unreadable):
        reader.count(bad)
    assert pdfworker.POOL.idle == [] and not ({p.pid for p in psutil.Process().children()} - before)


def test_taking_too_long_isnt_tried_all_over_again_on_a_new_worker(tmp_path, workers, monkeypatch):
    """Which would only double the wait."""
    marker = tmp_path / "asked.txt"
    monkeypatch.setattr(pdfworker, "LIMIT", 1.0)
    one, two = make_pdf(tmp_path / "one.pdf"), make_pdf(tmp_path / "two.pdf")
    workers(READY + f"""
count = 0
while True:
    asked()
    with open({str(marker)!r}, "a") as out:
        out.write("x")
    count += 1
    if count == 2:
        time.sleep(60)
    say({{"pages": 3}})
""")
    assert reader.count(one) == 3
    with pytest.raises(pdfworker.NotNow, match="too long"):
        reader.count(two)
    assert marker.read_text(encoding="utf-8") == "xx"


def test_a_worker_lets_go_of_a_document_that_failed(tmp_path, workers, monkeypatch):
    """Its share may have dropped, and what it was opened by with it."""
    path = make_pdf(tmp_path / "a.pdf")
    monkeypatch.setattr(pdfworker, "_command", lambda: main_with(
        "class Doc:\n"
        "    def close(self):\n"
        "        pass\n"
        "def do(ask, data, documents):\n"
        "    if not getattr(do, 'failed', False):\n"
        "        do.failed = True\n"
        "        documents[(ask['path'], tuple(ask['stamp']))] = Doc()\n"
        "        raise ValueError('a share dropped')\n"
        "    return {'pages': len(documents)}, b''\n"
        "pdfworker._do = do"))
    with pytest.raises(reader.Unreadable, match="ValueError"):
        reader.count(path)
    assert reader.count(path) == 0


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS takes no such limit")
def test_a_real_worker_is_refused_more_than_its_memory(tmp_path, workers, monkeypatch):
    monkeypatch.setattr(pdfworker, "_command", lambda: main_with(
        "pdfworker._do = lambda ask, data, documents: (bytearray(4 << 30), ({'pages': 1}, b''))[1]"))
    with pytest.raises(reader.Unreadable, match="MemoryError"):
        reader.count(make_pdf(tmp_path / "a.pdf"))


@pytest.mark.parametrize("failed, kept", [("FzErrorSystem", False), ("FileNotFoundError", False), ("OSError", False), ("FileDataError", True)])
def test_a_shares_trouble_isnt_kept_as_the_files_own(tmp_path, workers, failed, kept):
    workers(READY + f'asked()\nsay({{"failed": "{failed}"}})\n')
    assert meta.read(make_pdf(tmp_path / "a.pdf")).not_now is not kept


def test_a_known_title_keeps_its_details_when_it_cant_be_read_just_now(tmp_path, workers):
    from hylly import catalog

    library = tmp_path / "library"
    (library / "own").mkdir(parents=True)
    (library / "catalog" / "covers").mkdir(parents=True)
    path = make_pdf(library / "own" / "file name.pdf", title="The Moonwake Atlas")
    cache = catalog.FileCache(library / "catalog" / "cache.jsonl")
    known = make_item(library, path)
    catalog.read_item(library, library / "catalog", cache, known)
    assert known.meta["title"] == "The Moonwake Atlas" and known.meta["_cover"]
    (library / "catalog" / "covers" / catalog.thumb_name("t")).unlink()  # its picture has gone: it's read again
    pdfworker.POOL.close()
    workers("sys.exit(1)\n")
    again = make_item(library, path)
    catalog.read_item(library, library / "catalog", cache, again)
    assert again.again and again.meta["title"] == "The Moonwake Atlas"


def test_a_catalog_run_says_what_it_will_try_again(tmp_path, workers):
    from hylly import catalog

    build_library(tmp_path)
    workers("sys.exit(1)\n")
    said = []
    catalog.build(tmp_path, log=said.append)
    assert any("couldn't be read just now" in line and "next hylly catalog" in line for line in said), said


# ---- a file opened only as what its name says, and a PDF's word on its pages ---------------------------------------

# A picture, as MuPDF would draw it: an SVG with "%PDF" in a remark, which takes it as far as the worker as a PDF does
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="400"><!-- %PDF --><rect width="9" height="9"/></svg>'


@pytest.mark.parametrize("name", ["picture.pdf", "picture.epub", "picture.fb2", "picture.mobi"])
def test_a_picture_named_as_a_pdf_or_an_e_book_isnt_opened_as_a_picture(tmp_path, workers, name):
    """MuPDF goes by what it finds in a file, whatever its name: an SVG named .pdf or .epub was opened as a picture,
    with all of MuPDF's readers of pictures, SVG and office files to be reached that way. A worker opens a PDF or an
    e-book, and nothing else, and says what the file isn't."""
    path = tmp_path / name
    path.write_bytes(SVG)
    with pytest.raises(reader.Unreadable, match="isn't"):
        reader.count(path)
    assert pdfworker.POOL.idle and not pdfworker._stops  # an answer, which costs no worker


def test_the_catalog_reads_only_a_pdf_as_a_pdf(tmp_path, workers):
    """The catalog's details, from a file and from inside a ZIP package (as bytes, told they're a PDF): a picture is
    neither, though MuPDF would draw it as a cover."""
    picture = tmp_path / "cover.pdf"
    picture.write_bytes(SVG)
    with pytest.raises(pdfworker.Unreadable, match="isn't a PDF"):
        pdfworker.details(str(picture), (1, 2), 480)
    png = io.BytesIO()
    Image.new("RGB", (30, 40), "red").save(png, "PNG")
    with pytest.raises(pdfworker.Unreadable, match="isn't a PDF"):
        pdfworker.details(str(tmp_path / "book.zip"), (1, 2), 480, png.getvalue())
    assert meta.read(picture).source == "unreadable"


def test_a_pdf_may_have_some_junk_before_its_header_as_readers_allow(tmp_path, workers):
    """Some real PDFs have a few bytes before their %PDF-: a line end, a Mac's or a mail's header. Up to 1,019 bytes
    are allowed, its five letters whole within the first 1,024, as reader._kind looks for it; past that a file is no
    PDF to the reader or the catalog, which agree."""
    pdf = make_pdf(tmp_path / "a.pdf").read_bytes()
    for n, junk in enumerate((b"\r\n", b"x" * 128, b"x" * 1019)):
        (tmp_path / f"junk{n}.pdf").write_bytes(junk + pdf)
        assert reader.count(tmp_path / f"junk{n}.pdf") == 3
        assert pdfworker.details(str(tmp_path / f"junk{n}.pdf"), (n, n), 480)[1] == 3
    (tmp_path / "more.pdf").write_bytes(b"x" * 1020 + pdf)  # "%PDF" within them, which takes it as far as the worker
    with pytest.raises(reader.Unreadable, match="isn't a PDF"):
        reader.count(tmp_path / "more.pdf")
    with pytest.raises(pdfworker.Unreadable, match="isn't a PDF"):
        pdfworker.details(str(tmp_path / "more.pdf"), (5, 5), 480)


def claiming(pages: int) -> bytes:
    """A small PDF of one page that says it has pages: its page tree's count, with a table of objects that long (packed,
    and all free places but four), which is all MuPDF asks of such a number."""
    import zlib

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count %d >>" % pages,
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>"]
    out, at = bytearray(b"%PDF-1.7\n"), []
    for n, body in enumerate(objects, 1):
        at.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    table_at = len(out)  # the table is object 4
    rows = b"\x00\x00\x00\x00\xff\xff" + b"".join(b"\x01" + a.to_bytes(4, "big") + b"\x00" for a in (*at, table_at))
    table = zlib.compress(rows + b"\x00" * 6 * (pages - len(objects) - 1), 9)
    out += (b"4 0 obj\n<< /Type /XRef /Size %d /W [1 4 1] /Root 1 0 R /Filter /FlateDecode /Length %d >>\nstream\n"
            % (pages + 1, len(table)) + table + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % table_at)
    return bytes(out)


def test_a_pdf_saying_it_has_more_pages_than_any_book_is_unreadable_to_the_catalog(tmp_path, workers):
    """A PDF's number of pages is its own word: a small file said 200,000, and the catalog kept and showed that. Over
    MAX_PAGES it is unreadable, with the number it said; a long book is still read."""
    path = tmp_path / "endless.pdf"
    path.write_bytes(claiming(200_000))
    with pytest.raises(pdfworker.Unreadable, match="^This file says it has 200,000 pages, more than any book has.$"):
        pdfworker.details(str(path), (1, 2), 480)
    assert meta.read(path).pages == 0
    path.write_bytes(claiming(5000))
    assert pdfworker.details(str(path), (3, 4), 480)[1] == 5000
