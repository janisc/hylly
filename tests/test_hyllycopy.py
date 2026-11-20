"""HyllyCopy, hylly copy: what it promises, and the disks and shares that go away while it keeps them."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from hylly import bell, cli, commands, exits, hyllycopy as hc, runs
from hylly.exits import Exit

from helpers import note_threads_waited_for


NO_SYMLINKS = os.name == "nt"


# ---- a source, a target, and disks that do as they are told ----------------------------------------------------------

def tree(folder: Path, files: dict[str, bytes | str]) -> Path:
    for rel, data in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
    return folder


def as_it_is(folder: Path) -> dict[str, tuple[bytes, int]]:
    """Every file with its contents and time; folders too, as a copy must leave no trace."""
    out = {}
    for path in folder.rglob("*"):
        rel = path.relative_to(folder).as_posix()
        out[rel] = (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else (b"", 0)
    return out


def contents(folder: Path) -> dict[str, bytes]:
    """The files of a copy, without what it keeps for itself."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*")
            if p.is_file() and hc.FOLDER not in p.relative_to(folder).parts}


def copied_says(target: Path) -> set[str]:
    """The files the record on the disk tells of."""
    return {e["p"] for e in hc.read_records(target / hc.FOLDER / "record.jsonl") if "p" in e}


def job(source, target, held=None, **kw) -> hc.Copying:
    """A copy whose lines (c.lines) and waits (c.slept) are kept, and whose waits are counted instead of slept: one that
    goes round for ever fails its test. held: called at each wait while the copy is on hold (c.why_held), a test's way
    of bringing a disk or a share back, which goes by what the copy is doing, never by the words it says."""
    lines, slept = [], []

    def sleep(seconds) -> None:
        slept.append(seconds)
        assert len(slept) < 300, "it goes round for ever"
        if held is not None and c.why_held:
            held()

    c = hc.Copying(source, target, say=lambda line="", **k: lines.append(line), sleep=sleep, **kw)
    c.lines, c.slept = lines, slept
    return c


def copy(source, target, **kw) -> tuple[hc.Copying, hc.Result]:
    c = job(source, target, **kw)
    c.check()
    c.prepare()
    return c, c.run()


class Faults:
    """A side's calls failing as told: which(name of the call, its arguments) gives an error, or None. away: how many
    times it then doesn't answer when asked."""

    def __init__(self, side: hc.Side, which, away: int = 0) -> None:
        self.side, self.which, self.after, self.away, self.calls = side, which, away, 0, []
        real_call, real_answers = side.call, side.answers

        def call(fn, *a, **k):
            name = getattr(fn, "__name__", str(fn))
            if name == "write" and not a:
                name = "the record"
            self.calls.append(name)
            error = self.which(name, a)
            if error is not None:
                self.away = self.after
                raise hc._Trouble(side, error)
            return real_call(fn, *a, **k)

        def answers() -> bool:
            if self.away > 0:
                self.away -= 1
                return False
            return real_answers()

        side.call, side.answers = call, answers


def nth(name: str, n: int, error: OSError, faults_away: int = 0):
    """The nth call of this name fails, once. For Faults: which."""
    seen = {"count": 0}

    def which(call: str, args) -> OSError | None:
        if call == name:
            seen["count"] += 1
            if seen["count"] == n:
                return error
        return None

    return which


def before_listing_again(c: hc.Copying, then) -> None:
    """then() runs as the copy lists its source again after copying (its second listing), just before that listing:
    found by what the copy does, not by what it says."""
    real, listed = c._list_source, {"n": 0}

    def listing():
        listed["n"] += 1
        if listed["n"] == 2:
            then()
        return real()

    c._list_source = listing


def link_to(real: Path, link: Path) -> None:
    """A link standing for a share: its removal is a share gone, for real, for every call."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
    else:
        os.symlink(real, link)


def unlink_link(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


def eio() -> OSError:
    return OSError(errno.EIO, "Input/output error")


@pytest.fixture
def source(tmp_path) -> Path:
    return tree(tmp_path / "from", {"a.epub": b"book a" * 100, "sub/b.epub": b"book b" * 200, "sub/deep/c.pdf": b"c" * 5000, "z.cbz": b"z" * 10})


@pytest.fixture
def target(tmp_path) -> Path:
    return tmp_path / "to"


# ---- what it promises ---------------------------------------------------------------------------------------------

def test_a_copy_has_every_file_and_time_and_folder_and_leaves_the_source_as_it_was(source, target):
    (source / "empty" / "inside").mkdir(parents=True)
    os.utime(source / "a.epub", (1_600_000_000, 1_600_000_000))
    before = as_it_is(source)
    c, r = copy(source, target)
    assert r.done and r.copied == 4 and not r.failed
    assert as_it_is(source) == before
    assert contents(target) == contents(source)
    assert (target / "empty" / "inside").is_dir()
    assert (target / "a.epub").stat().st_mtime_ns == (source / "a.epub").stat().st_mtime_ns
    assert not list((target / hc.FOLDER / "parts").iterdir())
    report = (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    assert "DONE" in report and "NOT DONE" not in report and "4 files" in report


def test_a_plan_writes_nothing_and_says_what_a_run_would_do(source, target):
    c = job(source, target)
    c.check()
    c.plan()
    said = "\n".join(c.lines)
    assert not target.exists()
    assert "To copy: 4 files" in said and "Free on the target" in said and "a new folder" in said and "Without --dry-run, it copies" in said


def test_the_manifest_is_what_sha256sum_reads(source, target):
    (source / "name with spaces & ä.txt").write_bytes(b"spaces")
    copy(source, target)
    lines = (target / hc.FOLDER / "manifest.sha256").read_bytes().decode("utf-8").splitlines()
    want = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    assert {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines} == want
    tool = shutil.which("sha256sum")
    if tool:
        done = subprocess.run([tool, "-c", f"{hc.FOLDER}/manifest.sha256"], cwd=target, capture_output=True)
        assert done.returncode == 0, done.stdout + done.stderr


def test_a_manifest_line_for_an_odd_name_is_as_sha256sum_writes_it():
    assert hc._manifest_line("a\\b\nc", "ab12") == b"\\ab12  a\\\\b\\nc\n"
    assert hc._manifest_line("plain", "ab12") == b"ab12  plain\n"


def test_a_different_file_with_the_name_stays_and_is_reported(source, target):
    tree(target, {"a.epub": b"my own a.epub, not the one being copied", "sub/b.epub": b"X" * 1200})  # the second has the same size
    before = as_it_is(target)
    c, r = copy(source, target)
    assert not r.done and set(r.failed) == {"a.epub", "sub/b.epub"} and "nothing was overwritten" in r.failed["a.epub"]
    after = as_it_is(target)
    assert after["a.epub"] == before["a.epub"] and after["sub/b.epub"] == before["sub/b.epub"]
    assert contents(target)["z.cbz"] == b"z" * 10  # the rest went on
    assert "a.epub" in (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    assert not list((target / hc.FOLDER / "parts").iterdir())  # no new copy begun that it couldn't name: only hylly backup sets aside


def test_a_folder_with_the_name_stays_and_is_reported(source, target):
    (target / "a.epub").mkdir(parents=True)
    _, r = copy(source, target)
    assert "a.epub" in r.failed and (target / "a.epub").is_dir() and not r.done


def test_the_same_file_already_there_is_compared_and_not_written(source, target):
    tree(target, {"a.epub": (source / "a.epub").read_bytes()})
    os.utime(target / "a.epub", (1_500_000_000, 1_500_000_000))
    _, r = copy(source, target)
    assert r.done and r.there == 1 and r.copied == 3
    assert (target / "a.epub").stat().st_mtime == 1_500_000_000  # untouched


def test_what_only_the_target_has_stays(source, target):
    tree(target, {"mine.txt": "mine"})
    _, r = copy(source, target)
    assert r.done and (target / "mine.txt").read_text() == "mine"


def test_a_run_with_nothing_to_do_reads_no_file_and_says_so(source, target, monkeypatch):
    copy(source, target)
    monkeypatch.setattr(hc, "past_the_cache", lambda *a, **k: pytest.fail("nothing needs reading"))
    c, r = copy(source, target)
    assert r.done and r.earlier == 4 and not r.copied and not r.there


def test_the_next_run_copies_what_is_new(source, target):
    copy(source, target)
    (source / "new.epub").write_bytes(b"new")
    _, r = copy(source, target)
    assert r.done and r.copied == 1 and (target / "new.epub").read_bytes() == b"new" and r.earlier == 4


def test_a_file_changed_on_the_source_since_is_not_written_over(source, target):
    copy(source, target)
    (source / "a.epub").write_bytes(b"a new edition, of another size")
    _, r = copy(source, target)
    assert not r.done and "nothing was overwritten" in r.failed["a.epub"]
    assert (target / "a.epub").read_bytes() == b"book a" * 100


def test_a_touched_file_is_compared_and_its_record_brought_up_to_date(source, target):
    copy(source, target)
    os.utime(source / "a.epub", (1_700_000_000, 1_700_000_000))
    _, r = copy(source, target)
    assert r.done and r.there == 1
    _, again = copy(source, target)
    assert again.done and again.earlier == 4 and not again.there


def test_a_record_of_another_source_isn_t_taken_at_its_word(tmp_path, source, target):
    copy(source, target)
    other = tree(tmp_path / "other", {"a.epub": b"book A" * 100})  # same size, and now the same time
    os.utime(other / "a.epub", ns=(os.stat(source / "a.epub").st_mtime_ns,) * 2)
    _, r = copy(other, target)
    assert not r.done and "nothing was overwritten" in r.failed["a.epub"]


def test_a_copy_gone_from_the_target_is_copied_again(source, target):
    copy(source, target)
    (target / "a.epub").unlink()
    _, r = copy(source, target)
    assert r.done and r.copied == 1 and (target / "a.epub").exists()


def test_a_damaged_record_costs_only_another_look(source, target):
    copy(source, target)
    record = target / hc.FOLDER / "record.jsonl"
    record.write_bytes(record.read_bytes()[:-40] + b"\xff\xfe nonsense")
    _, r = copy(source, target)
    assert r.done and r.there == 1 and r.earlier == 3


# ---- a stop, and going on ---------------------------------------------------------------------------------------------

def big(tmp_path, size: int = 3 * hc.CHUNK + 123) -> Path:
    data = bytes(range(256)) * (size // 256 + 1)
    return tree(tmp_path / "big", {"disk.iso": data[:size], "after.txt": b"after"})


def test_a_stop_loses_nothing_but_time_on_one_file_and_goes_on_from_there(tmp_path, target):
    source = big(tmp_path)
    size = (source / "disk.iso").stat().st_size
    c = job(source, target)
    c.check()
    c.prepare()
    writes = {"n": 0}

    def stop_at_the_third_write(name, a):
        if name == "_write_all":
            writes["n"] += 1
            if writes["n"] == 4:  # one for the small file before it, and two of this one
                raise KeyboardInterrupt
        return None

    Faults(c.target, stop_at_the_third_write)
    with pytest.raises(KeyboardInterrupt):
        c.run()
    assert not (target / "disk.iso").exists()  # no half a file under its name
    part = c._part("disk.iso")
    assert os.path.getsize(part) == 2 * hc.CHUNK

    d, r = copy(source, target)
    assert r.done and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()
    assert d.moved_bytes == size - 2 * hc.CHUNK  # only the rest was written
    assert any("going on from" in line for line in d.lines)


@pytest.mark.parametrize("left", ["nonsense at its end", "longer than the file", "nonsense from the start"])
def test_whatever_a_stopped_run_left_is_compared_with_the_source(tmp_path, target, left):
    source = big(tmp_path)
    data = (source / "disk.iso").read_bytes()
    c = job(source, target)
    c.check()
    c.prepare()
    part = Path(c._part("disk.iso"))
    part.write_bytes({"nonsense at its end": data[:hc.CHUNK] + b"\0" * hc.CHUNK, "longer than the file": data + b"more",
                      "nonsense from the start": b"\xff" * hc.CHUNK}[left])
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == data


def test_a_power_cut_after_the_name_but_before_the_record(source, target):
    copy(source, target)
    (target / hc.FOLDER / "record.jsonl").unlink()
    _, r = copy(source, target)
    assert r.done and r.there == 4 and not r.copied


# ---- every copy checked ------------------------------------------------------------------------------------------------

def spoil_the_part(side_of: hc.Copying, times: int):
    """What the disk holds differs from what was written, the first few times: flipped once it's flushed."""
    left = {"n": times}

    def which(name, a):
        if name == "utime" and left["n"] > 0:
            left["n"] -= 1
            with open(a[0], "r+b") as fh:
                first = fh.read(1)
                fh.seek(0)
                fh.write(bytes([first[0] ^ 1]))
        return None

    return which


def test_a_copy_that_reads_back_differently_is_copied_again(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, spoil_the_part(c, 2))
    r = c.run()
    assert r.done and contents(target) == contents(source)
    assert any("read back differently" in line for line in c.lines)


def test_a_copy_that_never_reads_back_right_never_gets_its_name(source, target):
    c = job(source, target)
    c.check()
    c.prepare()

    def always_a(name, a):  # only the first file in the order
        if name == "utime" and c.current == 600:
            spoil_the_part(c, 1)(name, a)
        return None

    Faults(c.target, always_a)
    r = c.run()
    assert not r.done and list(r.failed) == ["a.epub"] and "read back differently" in r.failed["a.epub"]
    assert not (target / "a.epub").exists() and (target / "sub" / "b.epub").exists()


def test_a_file_changing_while_it_is_copied_is_copied_again(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    changes = {"n": 0}

    def changing(name, a):
        if name == "utime" and changes["n"] == 0:
            changes["n"] = 1
            (source / "a.epub").write_bytes(b"book a" * 100 + b" and more")
        return None

    Faults(c.target, changing)
    r = c.run()
    assert (target / "a.epub").read_bytes() == b"book a" * 100 + b" and more"
    assert r.done and any("changed while" in line for line in c.lines)


def test_a_file_that_keeps_changing_is_reported(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    n = {"n": 0}

    def changing(name, a):
        if name == "utime":
            n["n"] += 1
            os.utime(source / "a.epub", (1_500_000_000 + n["n"], 1_500_000_000 + n["n"]))
        return None

    Faults(c.target, changing)
    r = c.run()
    assert not r.done and "a.epub" in r.failed and not (target / "a.epub").exists()


def test_done_comes_after_a_new_listing_which_catches_what_was_added(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    added = {"n": 0}

    def adding(name, a):
        if name == "utime" and not added["n"]:
            added["n"] = 1
            (source / "late.epub").write_bytes(b"late")
        return None

    Faults(c.target, adding)
    r = c.run()
    assert r.done and r.copied == 5 and (target / "late.epub").read_bytes() == b"late"


def test_a_file_gone_from_the_source_meanwhile_isn_t_a_failure(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    gone = {"n": 0}

    def removing(name, a):
        if name == "utime" and not gone["n"]:
            gone["n"] = 1
            (source / "z.cbz").unlink()
        return None

    Faults(c.target, removing)
    r = c.run()
    assert r.done and "z.cbz" not in r.listing.files and r.went == {"z.cbz"}
    assert "z.cbz" in (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    assert "left the source" in c.tell_of_the_end(r) and "originals" not in c.tell_of_the_end(r)


@pytest.mark.parametrize("kind,line", [
    (".sfv", "a.epub {crc}"), (".md5", "{md5}  a.epub"), (".md5", "{md5} *a.epub"), (".md5", "MD5 (a.epub) = {md5}"), (".sha256", "{sha}  a.epub"),
    (".sha1", "{sha1}  ./a.epub")])
def test_lists_the_source_has_are_compared_too(source, target, kind, line):
    data = (source / "a.epub").read_bytes()
    import zlib

    good = line.format(crc=f"{zlib.crc32(data):08X}", md5=hashlib.md5(data).hexdigest(), sha=hashlib.sha256(data).hexdigest(), sha1=hashlib.sha1(data).hexdigest())
    (source / f"list{kind}").write_text("; a comment\n" + good + "\n", encoding="utf-8")
    _, r = copy(source, target)
    assert r.done and not r.disagree


def test_a_file_already_there_is_compared_with_the_list_too(source, target):
    (source / "sub" / "list.md5").write_text("0" * 32 + "  b.epub\n", encoding="utf-8")
    copy(source, target)
    (target / hc.FOLDER / "record.jsonl").unlink()  # a record lost: found there, and the list still has its say
    _, r = copy(source, target)
    assert r.done and r.there == 5 and r.disagree == {"sub/b.epub": "sub/list.md5"}


def test_a_file_that_disagrees_with_the_list_is_copied_as_it_is_and_said(source, target):
    (source / "sub" / "list.md5").write_text("0" * 32 + "  b.epub\n", encoding="utf-8")
    c, r = copy(source, target)
    assert r.done and r.disagree == {"sub/b.epub": "sub/list.md5"}
    assert (target / "sub" / "b.epub").read_bytes() == (source / "sub" / "b.epub").read_bytes()
    assert "may be damaged" in (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    assert "didn't match" in c.tell_of_the_end(r)
    _, again = copy(source, target)  # and it stays said
    assert again.disagree == {"sub/b.epub": "sub/list.md5"}


def test_a_list_can_t_point_outside_the_source(tmp_path, target):
    source = tree(tmp_path / "from", {"a.txt": "a", "list.md5": "0" * 32 + "  ../a.txt\n"})
    _, r = copy(source, target)
    assert r.done and not r.disagree


# ---- disks that go away -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [eio(), TimeoutError(errno.ETIMEDOUT, "timed out"), ConnectionResetError(errno.ECONNRESET, "reset")])
def test_a_read_that_fails_is_waited_out_and_the_copy_goes_on(tmp_path, target, error):
    source = big(tmp_path)
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, nth("read", 3, error), away=3)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()
    assert c.slept == list(hc.WAITS[:3])
    assert any("isn't answering" in line for line in c.lines) and any("is back" in line for line in c.lines)


def test_a_write_that_fails_is_waited_out_and_goes_on_from_what_is_on_the_disk(tmp_path, target, monkeypatch):
    monkeypatch.setattr(hc, "ON_DISK_EVERY", hc.CHUNK)
    source = big(tmp_path)
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, nth("_write_all", 3, eio()), away=1)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()
    assert c.moved_bytes == (source / "disk.iso").stat().st_size + 5  # no chunk counted twice: it went on from the second


@pytest.mark.skipif(os.name == "nt", reason="permissions on Linux and macOS")
def test_a_read_only_original_goes_on_from_what_is_on_the_disk_after_a_drop_too(tmp_path, target, monkeypatch):
    """Its part is its owner's to write while it's a part, so a drop costs only what wasn't on the disk yet, as for any
    file, not the whole file again."""
    monkeypatch.setattr(hc, "ON_DISK_EVERY", hc.CHUNK)
    source = big(tmp_path)
    os.chmod(source / "disk.iso", 0o444)
    c = job(source, target)
    c.check()
    c.prepare()
    written, goes = [], []

    def which(name, a):
        if name == "_write_all":
            goes.append(1)
            if len(goes) == 3:
                return eio()
            written.append(len(a[1]))
        return None

    Faults(c.target, which, away=1)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()
    assert sum(written) == (source / "disk.iso").stat().st_size + 5  # nothing written twice: it went on from the second chunk
    assert (target / "disk.iso").stat().st_mode & 0o200


def test_a_hold_after_a_run_has_a_watchdog_of_its_own_and_the_run_s_ends(source, target, monkeypatch):
    """A backup's or a restore's hold after its copy (wait_for) is watched by a watchdog of its own: the run's, caught in
    a tick just as the run ended, ends all the same, and never watches beside it."""
    monkeypatch.setattr(hc, "TICK", 0.01)
    c = job(source, target)
    in_tick, go_on, ticks = threading.Event(), threading.Event(), []

    def tick(self):
        ticks.append(threading.current_thread())
        if len(ticks) == 1:
            in_tick.set()
            go_on.wait(5)

    monkeypatch.setattr(hc._Job, "_tick", tick)
    with c._watched():  # the run
        assert in_tick.wait(5)
    runs = ticks[0]

    def hold(self, side, e):
        go_on.set()
        runs.join(5)
        time.sleep(0.2)  # the hold's own watchdog ticks meanwhile

    monkeypatch.setattr(hc._Job, "_hold", hold)
    c.wait_for(hc.Side(target, "the target"))  # not there: held
    assert not runs.is_alive() and ticks[1:] and runs not in ticks[1:]


def test_the_share_restarting_twice_within_a_minute(source, target):
    """What the router does when the internet comes back."""
    c = job(source, target)
    c.check()
    c.prepare()
    drop = [OSError(errno.ENETRESET, "Network dropped connection on reset"), OSError(errno.EHOSTDOWN, "Host is down")]
    seen = {"n": 0}

    def which(name, a):
        if name in ("_open_part", "_no_overwrite_name"):
            seen["n"] += 1
            if seen["n"] in (2, 3):
                return drop.pop()
        return None

    Faults(c.target, which, away=2)
    r = c.run()
    assert r.done and contents(target) == contents(source)


def test_a_name_given_with_the_answer_lost(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    done = {"n": 0}

    def which(name, a):
        if name == "_no_overwrite_name" and not done["n"]:
            done["n"] = 1
            hc._no_overwrite_name(*a)  # it happened, but the share went away before saying so
            return OSError(errno.ECONNRESET, "reset")
        return None

    Faults(c.target, which, away=1)
    r = c.run()
    assert r.done and contents(target) == contents(source) and not list((target / hc.FOLDER / "parts").iterdir())


@pytest.mark.parametrize("which_side", ["source", "target"])
def test_a_disk_that_really_goes_away_and_comes_back(tmp_path, which_side):
    """No stand-ins: the share's path stops working for every call, and comes back."""
    real = tmp_path / "real"
    if which_side == "source":
        tree(real, {f"f{i}.txt": f"file {i}" for i in range(6)})
        s, t = tmp_path / "share", tmp_path / "to"
        link = s
    else:
        s = tree(tmp_path / "from", {f"f{i}.txt": f"file {i}" for i in range(6)})
        real.mkdir()
        t = tmp_path / "share"
        link = t
    link_to(real, link)
    state = {"gone": False, "back": False}

    def held():
        if state["gone"] and not state["back"]:
            state["back"] = True
            link_to(real, link)

    c = job(s, t, held=held)
    c.check()
    c.prepare()

    def goes_away(name, a):
        if name == "open" and c.done_files == 3 and not state["gone"]:
            state["gone"] = True
            unlink_link(link)
        return None

    Faults(c.source, goes_away)
    r = c.run()
    assert state["back"] and r.done and contents(real if which_side == "target" else t) == {f"f{i}.txt": f"file {i}".encode() for i in range(6)}


@pytest.mark.parametrize("when", ["_open_part", "_no_overwrite_name"])
def test_nothing_lands_where_an_unmounted_disk_was(tmp_path, when):
    """An unmounted disk leaves an empty folder of the same name, which takes files gladly."""
    source = tree(tmp_path / "from", {"a/1.txt": "1", "b/2.txt": "2", "c/3.txt": "3", "d/4.txt": "4"})
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    real.mkdir()
    link_to(real, mount)
    state = {"out": False, "while_out": None}

    def held():
        if state["out"] and state["while_out"] is None:
            state["while_out"] = os.listdir(mount)
            os.rmdir(mount)
            link_to(real, mount)

    c = job(source, mount, held=held)
    c.check()
    c.prepare()

    def unmounts(name, a):
        if name == when and c.done_files == 2 and not state["out"]:
            state["out"] = True
            unlink_link(mount)
            os.mkdir(mount)
        return None

    Faults(c.target, unmounts)
    r = c.run()
    assert state["while_out"] == [] and r.done and contents(real) == contents(source)


def test_a_source_unmounted_before_the_last_listing_isn_t_done_with_nothing(tmp_path, target):
    """It lists as an empty folder, without any error."""
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    tree(real, {"a.txt": "a", "b.txt": "b"})
    link_to(real, mount)
    state = {"out": False, "held": False}

    def held():
        if state["out"] and not state["held"]:
            state["held"] = True
            os.rmdir(mount)
            link_to(real, mount)

    def unmounted():
        state["out"] = True
        unlink_link(mount)
        os.mkdir(mount)

    c = job(mount, target, held=held)
    c.check()
    c.prepare()
    before_listing_again(c, unmounted)
    r = c.run()
    assert state["held"] and r.done and len(r.listing.files) == 2


def test_a_full_disk_holds_until_there_is_room(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    full = {"n": 0}

    def which(name, a):
        if name == "_write_all" and full["n"] < 3:
            full["n"] += 1
            return OSError(errno.ENOSPC, "No space left on device")
        return None

    Faults(c.target, which)
    r = c.run()
    assert r.done and full["n"] == 3 and contents(target) == contents(source)
    assert sum("is full" in line for line in c.lines) == 1


def test_no_room_from_the_start_is_said_at_the_start(source, target, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 10))
    c = job(source, target)
    c.check()
    c.prepare()
    with pytest.raises(Exit) as e:
        c.run()
    assert e.value.code == exits.LOCAL and "Not enough room" in e.value.message and not contents(target)


def test_one_file_that_can_t_be_read_is_noted_and_the_rest_goes_on(source, target):
    c = job(source, target)
    c.check()
    c.prepare()

    def refuse(name, a):
        return PermissionError(errno.EACCES, "Permission denied") if name == "open" and str(a[0]).endswith("b.epub") else None

    Faults(c.source, refuse)
    r = c.run()
    assert not r.done and list(r.failed) == ["sub/b.epub"] and "Permission denied" in r.failed["sub/b.epub"]
    assert set(contents(target)) == {"a.epub", "sub/deep/c.pdf", "z.cbz"}


def test_a_bad_spot_on_the_disk_is_one_file_s_problem(source, target):
    c = job(source, target)
    c.check()
    c.prepare()

    def bad_spot(name, a):
        return eio() if name == "open" and str(a[0]).endswith("c.pdf") else None

    Faults(c.source, bad_spot)
    r = c.run()
    assert not r.done and list(r.failed) == ["sub/deep/c.pdf"] and "keeps failing" in r.failed["sub/deep/c.pdf"]
    assert set(contents(target)) == {"a.epub", "sub/b.epub", "z.cbz"}


def test_files_in_a_row_failing_put_it_on_hold_until_one_goes_through(tmp_path, target, notes):
    source = tree(tmp_path / "from", {"good.txt": "good", **{f"z{i}.txt": "z" for i in range(7)}, "zz.txt": "and this one is fine"})
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, lambda name, a: eio() if name == "open" and os.path.basename(str(a[0])).startswith("z") and "zz" not in str(a[0]) else None)
    r = c.run()
    assert not r.done and len(r.failed) == 7 and (target / "zz.txt").exists() and (target / "good.txt").exists()
    assert hc.COOL_DOWN in c.slept and "HyllyCopy: trouble with a disk" in [n[0] for n in notes]
    assert "hylly jobs stop copy" in next(n[1] for n in notes if "trouble" in n[0])
    assert "good.txt" in copied_says(target)  # what it had is kept before it waits


def test_a_share_gone_for_a_moment_isn_t_the_file_s_fault(source, target):
    class GoneShare(FileNotFoundError):
        winerror = 53

    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, nth("open", 2, GoneShare(errno.ENOENT, "The network path was not found")))  # by now it answers again
    r = c.run()
    assert r.done and not r.failed and len(c.slept) == 1


def test_an_unreadable_folder_means_not_done(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, lambda name, a: PermissionError(errno.EACCES, "Permission denied")
           if name == "_entries" and str(a[0]).endswith("deep") else None)
    r = c.run()
    assert not r.done and "this folder can't be read" in r.failed["sub/deep/"]


def test_a_listing_cut_short_by_a_drop_is_made_again(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, nth("_entries", 2, eio()), away=1)
    r = c.run()
    assert r.done and len(r.listing.files) == 4


def test_the_record_waits_for_the_disk_too(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, nth("the record", 1, OSError(errno.ECONNRESET, "reset")), away=1)
    r = c.run()
    assert r.done
    assert copied_says(target) == set(contents(source))


# ---- what the last listing can and can't be believed about --------------------------------------------------------------

def swap_for_an_empty_folder(mount: Path, stays: list[str] = ()) -> None:
    """What an unmount leaves behind, with whatever was lying under the mount point."""
    unlink_link(mount)
    os.mkdir(mount)
    for name in stays:
        os.mkdir(mount / name)


def test_an_unmounted_source_that_still_has_one_of_its_names_isn_t_done_with_nothing(tmp_path, target):
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    tree(real, {f"f{i}.txt": "x" for i in range(8)})
    link_to(real, mount)
    state = {"out": False, "held": False}

    def held():
        if state["out"] and not state["held"]:
            state["held"] = True
            for name in os.listdir(mount):
                os.rmdir(mount / name)
            os.rmdir(mount)
            link_to(real, mount)

    def unmounted():
        state["out"] = True
        swap_for_an_empty_folder(mount, ["f0.txt"])  # an empty folder of that name, under the mount point

    c = job(mount, target, held=held)
    c.check()
    c.prepare()
    before_listing_again(c, unmounted)
    r = c.run()
    assert state["held"] and r.done and len(r.listing.files) == 8


def test_a_source_going_during_a_listing_is_listed_again(tmp_path, target):
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    tree(real, {"a.txt": "a", "b.txt": "b"})
    link_to(real, mount)
    state = {"listed": 0, "out": False, "held": False}

    def held():
        if state["out"]:
            state["out"], state["held"] = False, True
            os.rmdir(mount)
            link_to(real, mount)

    c = job(mount, target, held=held)
    c.check()
    c.prepare()
    real_walk = c._walk_source

    def walk():
        found = real_walk()
        state["listed"] += 1
        if state["listed"] == 2:  # just after this listing was made: the check before it has passed, and one after it is what's left
            found = hc.Listing()
            state["out"] = True
            swap_for_an_empty_folder(mount)
        return found

    c._walk_source = walk
    r = c.run()
    assert r.done and set(r.listing.files) == {"a.txt", "b.txt"} and state["held"]


def test_a_file_that_dropped_out_of_a_listing_doesn_t_stop_counting(tmp_path, target, monkeypatch):
    """Failed for good, and then missing from the last listing, as a mount going mid-folder can leave it: it was never copied."""
    source = tree(tmp_path / "from", {"a.txt": "a", "precious.txt": "precious", "z.txt": "z"})
    c = job(source, target)
    c.check()
    c.prepare()
    c.final.add("precious.txt")
    real = hc._entries
    listed = {"n": 0}

    def goes_short(path):
        found, bad = real(path)
        if path == c.source.root:
            listed["n"] += 1
            if listed["n"] == 2:
                found = [e for e in found if e[0] != "precious.txt"]
        return found, bad

    monkeypatch.setattr(hc, "_entries", goes_short)
    r = c.run()
    assert listed["n"] == 2 and "precious.txt" in r.listing.files and not r.done and "precious.txt" in r.failed


def test_a_source_that_has_lost_most_of_its_files_isn_t_done(tmp_path, target):
    source = tree(tmp_path / "from", {f"f{i}.txt": "x" for i in range(8)})
    c = job(source, target)
    c.check()
    c.prepare()
    gone = {"n": 0}

    def removing(name, a):
        if name == "utime" and not gone["n"]:
            gone["n"] = 1
            for i in range(2, 8):
                (source / f"f{i}.txt").unlink()
        return None

    Faults(c.target, removing)
    r = c.run()
    assert not r.done and "8 files when this began" in r.shrunk
    words = c.tell_of_the_end(r)
    assert "Not done" in words and "delete" in words and "NOT DONE" in (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    _, again = copy(source, target)  # removed on purpose: running it again is the way to say so
    assert again.done


def test_a_source_that_has_no_files_left_isn_t_done(tmp_path, target):
    source = tree(tmp_path / "from", {"f.txt": "x", "keep/g.txt": "y"})
    copy(source, target)
    (source / "f.txt").unlink()
    (source / "keep" / "g.txt").unlink()
    c, r = copy(source, target)
    assert not r.done and "no files now" in r.shrunk and "originals" not in c.tell_of_the_end(r)


def test_only_folders_are_no_reason_to_speak_of_originals(tmp_path, target):
    source = tmp_path / "from"
    (source / "empty" / "too").mkdir(parents=True)
    c, r = copy(source, target)
    assert r.done and (target / "empty" / "too").is_dir()
    assert "originals" not in c.tell_of_the_end(r)


def test_a_report_says_not_finished_until_the_run_has_ended(source, target, monkeypatch):
    copy(source, target)
    report = target / hc.FOLDER / "report.txt"
    assert report.read_text(encoding="utf-8").count("DONE") == 1
    (source / "new.epub").write_bytes(b"new")
    c = job(source, target)
    c.check()
    c.prepare()

    def stop(name, a):
        if name == "_write_all":
            raise KeyboardInterrupt
        return None

    Faults(c.target, stop)
    with pytest.raises(KeyboardInterrupt):
        c.run()
    text = report.read_text(encoding="utf-8")
    assert "NOT FINISHED" in text and "DONE" not in text
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 0))
    d = job(source, target)
    d.check()
    d.prepare()
    with pytest.raises(Exit):
        d.run()
    assert "NOT FINISHED" in report.read_text(encoding="utf-8")


def test_a_rerun_with_the_source_unmounted_is_refused(tmp_path, target):
    """Something lying in the folder an unmounted disk leaves: it isn't empty, and nothing in this run has anything to compare it with."""
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    tree(real, {f"f{i}.txt": f"file {i}" for i in range(10)})
    link_to(real, mount)
    copy(mount, target)
    unlink_link(mount)
    tree(mount, {"NOT-MOUNTED.txt": "the disk isn't here"})
    c = job(mount, target)
    c.check()
    c.prepare()
    with pytest.raises(Exit) as e:
        c.run()
    assert e.value.code == exits.NOT_SET_UP and "--new-disk" in e.value.message and not (target / "NOT-MOUNTED.txt").exists()
    assert "NOT FINISHED" in (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    p = job(mount, target)
    p.check()
    p.plan()
    assert any("doesn't look like the source" in line for line in p.lines)
    _, r = copy(mount, target, new_disk=True)
    assert r.done and (target / "NOT-MOUNTED.txt").exists()


def test_a_folder_of_the_one_name_the_source_has_doesn_t_make_it_the_source(tmp_path, target):
    """Its one top-level name, as an empty folder under the mount point: the names say yes, and the numbers and the files don't."""
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    tree(real, {f"books/f{i}.txt": "x" for i in range(10)})
    link_to(real, mount)
    copy(mount, target)
    unlink_link(mount)
    tree(mount, {"books/stray.txt": "not the disk"})
    c = job(mount, target)
    c.check()
    c.prepare()
    with pytest.raises(Exit) as e:
        c.run()
    assert e.value.code == exits.NOT_SET_UP and "--new-disk" in e.value.message and not (target / "books" / "stray.txt").exists()


def test_a_disk_mounted_again_with_new_numbers_is_the_same_source(tmp_path, target):
    real, mount = tmp_path / "disk", tmp_path / "mnt"
    tree(real, {f"f{i}.txt": f"file {i}" for i in range(10)})
    link_to(real, mount)
    copy(mount, target)
    unlink_link(mount)
    moved = tmp_path / "same-disk-other-numbers"
    tree(moved, {f"f{i}.txt": f"file {i}" for i in range(10)})
    for i in range(10):
        os.utime(moved / f"f{i}.txt", ns=(os.stat(real / f"f{i}.txt").st_mtime_ns,) * 2)
    link_to(moved, mount)
    _, r = copy(mount, target)
    assert r.done and r.earlier == 10


def test_a_source_whose_originals_you_removed_is_still_the_same_source(source, target):
    copy(source, target)
    for name in ("a.epub", "z.cbz"):
        (source / name).unlink()
    (source / "kept.txt").write_text("something else")
    _, r = copy(source, target)
    assert r.done and (target / "a.epub").exists()


def test_removing_every_file_on_purpose_can_be_said(tmp_path, target):
    source = tree(tmp_path / "from", {"keep/f.txt": "x", "keep/g.txt": "y"})
    copy(source, target)
    (source / "keep" / "f.txt").unlink()
    (source / "keep" / "g.txt").unlink()
    c, r = copy(source, target)
    assert not r.done and "--new-disk" in r.shrunk
    _, again = copy(source, target, new_disk=True)
    assert again.done and not again.shrunk


def test_a_file_that_failed_and_then_left_stays_failed(tmp_path, target):
    """A source whose root stays while its contents go, such as a pool that lost a disk: exactly half gets past the other guard."""
    source = tree(tmp_path / "from", {"disk1/a.txt": "a", "disk1/b.txt": "b", "disk2/c.txt": "c", "disk2/d.txt": "d"})
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, lambda name, a: eio() if name == "open" and str(a[0]).endswith("c.txt") else None)
    real_copy = c._copy_all

    def then_it_goes(todo, listing, result):
        real_copy(todo, listing, result)
        shutil.rmtree(source / "disk2")

    c._copy_all = then_it_goes
    c.tries["disk2/c.txt"] = hc.TRIES - 1
    r = c.run()
    assert not r.done and "left the source" in r.failed.get("disk2/c.txt", "")


def test_a_file_that_left_without_being_copied_or_failing_is_told_of(tmp_path, target):
    source = tree(tmp_path / "from", {"a.txt": "a", "b.txt": "b", "c.txt": "c", "z.txt": "z"})
    c = job(source, target)
    c.check()
    c.prepare()
    gone = {"n": 0}

    def which(name, a):
        if name == "utime" and not gone["n"]:
            gone["n"] = 1
            (source / "z.txt").unlink()
        return None

    Faults(c.target, which)
    r = c.run()
    assert r.done and r.went == {"z.txt"}


@pytest.mark.skipif(sys.platform == "linux", reason="this disk tells capitals apart")
def test_a_name_respelt_during_the_copy_isn_t_two_files(tmp_path, target):
    source = tree(tmp_path / "from", {"a.txt": "a", "b.txt": "b", "c.txt": "c"})
    c = job(source, target)
    c.check()
    c.prepare()
    done = {"n": 0}

    def which(name, a):
        if name == "utime" and c.done_files == 2 and not done["n"]:
            done["n"] = 1
            (source / "a.txt").rename(source / "A.TXT")
        return None

    Faults(c.target, which)
    r = c.run()
    assert r.done and not r.went and list(r.listing.files).count("a.txt") == 0


def test_a_share_with_no_file_numbers_is_there_if_it_answers(source, target):
    c = job(source, target)
    c.check()
    c.source.numbers = None
    shutil.rmtree(source / "sub")
    (source / "a.epub").unlink()
    (source / "z.cbz").unlink()
    assert c.source.answers() is (os.name == "nt")


def test_a_source_whose_names_have_all_gone_still_answers_if_it_is_the_same_folder(source, target):
    c = job(source, target)
    c.check()
    assert c.source.answers()
    (source / "a.epub").rename(source / "renamed.epub")
    (source / "z.cbz").unlink()
    shutil.rmtree(source / "sub")
    assert c.source.answers()  # moved away by someone: not a disk gone


@pytest.mark.parametrize("left,answers", [(8, True), (5, True), (4, False), (1, False), (0, False)])
def test_a_folder_with_other_numbers_has_to_have_most_of_the_names(tmp_path, target, left, answers):
    source = tree(tmp_path / "from", {f"f{i}.txt": "x" for i in range(8)})
    c = job(source, target)
    c.check()
    c.source.numbers = (1, 1)  # a disk mounted again may get new ones, and an unmount's empty folder has its own
    for i in range(left, 8):
        (source / f"f{i}.txt").unlink()
    assert c.source.answers() is answers


# ---- what went round for ever ---------------------------------------------------------------------------------------------

def bad_spot_files(tmp_path) -> Path:
    data = bytes(range(256)) * ((3 * hc.CHUNK + 123) // 256 + 1)
    return tree(tmp_path / "big", {"a.txt": b"before", "disk.iso": data[:3 * hc.CHUNK + 123], "z.txt": b"after"})


def test_a_read_that_always_fails_past_the_first_chunk_is_the_file_s_problem(tmp_path, target):
    source = bad_spot_files(tmp_path)
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, lambda name, a: eio() if name == "read" and c.current >= hc.CHUNK else None)
    r = c.run()
    assert not r.done and list(r.failed) == ["disk.iso"] and "keeps failing" in r.failed["disk.iso"]
    assert (target / "z.txt").read_bytes() == b"after"  # the next file is reached


def test_a_write_that_always_fails_past_the_first_chunk_is_the_file_s_problem(tmp_path, target):
    source = bad_spot_files(tmp_path)
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, lambda name, a: eio() if name == "_write_all" and c.current >= hc.CHUNK else None)
    r = c.run()
    assert not r.done and list(r.failed) == ["disk.iso"] and (target / "z.txt").exists()


def test_a_flush_that_always_fails_is_the_file_s_problem(tmp_path, target):
    source = tree(tmp_path / "from", {"a.txt": b"x" * 100_000, "z.txt": b"after"})
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, lambda name, a: eio() if name == "_keep_to_disk" and c.current == 100_000 else None)
    r = c.run()
    assert not r.done and list(r.failed) == ["a.txt"] and (target / "z.txt").exists()


def test_a_read_that_makes_the_disk_go_away_every_time_has_a_limit(tmp_path, target):
    """A USB disk that resets at one bad spot: every drop looks like a new start."""
    source = bad_spot_files(tmp_path)
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, lambda name, a: eio() if name == "read" and c.current >= hc.CHUNK else None, away=1)
    r = c.run()
    assert not r.done and "same place" in r.failed["disk.iso"] and (target / "z.txt").exists()


def test_a_full_disk_with_a_file_half_written_holds_and_says_so_once(tmp_path, target):
    source = bad_spot_files(tmp_path)
    clock = [1000.0]
    c = job(source, target, now=lambda: clock[0])
    c.check()
    c.prepare()
    full = {"n": 0}

    def which(name, a):
        if name == "_write_all" and c.current >= hc.CHUNK and full["n"] < 40:
            full["n"] += 1
            clock[0] += 60
            return OSError(errno.ENOSPC, "No space left on device")
        return None

    Faults(c.target, which)
    r = c.run()
    assert r.done and full["n"] == 40 and sum("is full" in line for line in c.lines) == 1


def test_holding_for_room_lets_the_watchdog_say_so(tmp_path, target, monkeypatch):
    source = bad_spot_files(tmp_path)
    clock = [1000.0]
    c = job(source, target, now=lambda: clock[0])
    c.check()
    c.prepare()
    notes = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: notes.append(text) or True)
    full = {"n": 0}

    def which(name, a):
        if name == "_write_all" and c.current >= hc.CHUNK and full["n"] < 20:
            full["n"] += 1
            clock[0] += 60
            if full["n"] == 18:
                c._tick()
            return OSError(errno.ENOSPC, "No space left on device")
        return None

    Faults(c.target, which)
    r = c.run()
    assert r.done and any("the target is full" in text for text in notes)


def test_a_folder_that_makes_the_disk_go_away_every_time_has_a_limit(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.source, lambda name, a: eio() if name == "_entries" and str(a[0]).endswith("deep") else None, away=1)
    r = c.run()
    assert not r.done and "this folder can't be read" in r.failed["sub/deep/"] and "same place" in r.failed["sub/deep/"]


def test_a_hold_isn_t_movement_to_the_watchdog(source, target, notes):
    clock = [1000.0]
    c = job(source, target, now=lambda: clock[0])
    c.check()
    c.last_move = clock[0]
    c.source.answers = lambda: True
    clock[0] += 3000
    c._hold(c.source, None)
    c._tick()
    assert [n[0] for n in notes] == ["HyllyCopy: on hold"]


def small_chunks(monkeypatch, every: int = 8) -> None:
    monkeypatch.setattr(hc, "CHUNK", 1 << 16)
    monkeypatch.setattr(hc, "ON_DISK_EVERY", every << 16)


def many_chunks(tmp_path, n: int) -> Path:
    return tree(tmp_path / "many", {"disk.iso": (bytes(range(256)) * ((n << 8) + 1))[:(n << 16) + 123], "z.txt": b"after"})


def test_a_drop_while_comparing_doesn_t_throw_away_the_rest(tmp_path, target, monkeypatch):
    small_chunks(monkeypatch, 2)
    source = many_chunks(tmp_path, 16)
    data = (source / "disk.iso").read_bytes()
    c = job(source, target)
    c.check()
    c.prepare()
    Path(c._part("disk.iso")).write_bytes(data[:15 << 16])
    Faults(c.source, nth("read", 8, OSError(errno.ECONNRESET, "reset")), away=1)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == data
    assert c.moved_bytes == (1 << 16) + 123 + 5  # the last chunk, and the small file: nothing that was there was written again


@pytest.mark.parametrize("every", [7, 11, 23])
def test_a_share_failing_now_and_then_still_gets_there(tmp_path, target, monkeypatch, every):
    small_chunks(monkeypatch)
    source = many_chunks(tmp_path, 64)
    c = job(source, target)
    c.check()
    c.prepare()
    n = {"n": 0}

    def now_and_then(name, a):
        if name == "_write_all":
            n["n"] += 1
            if n["n"] % every == 0:
                return eio()
        return None

    Faults(c.target, now_and_then)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()


def test_a_share_restarting_during_a_rewrite_doesn_t_add_up_to_a_failure(tmp_path, target, monkeypatch):
    small_chunks(monkeypatch)
    source = many_chunks(tmp_path, 32)
    c = job(source, target)
    c.check()
    c.prepare()
    n = {"n": 0}

    def restarts(name, a):
        if name == "read":
            n["n"] += 1
            if n["n"] in (12, 16, 20, 24):
                return OSError(errno.ECONNRESET, "reset")
        return None

    Faults(c.source, restarts, away=1)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()


def test_the_rest_of_a_file_being_compared_counts_towards_the_limit_only_if_nothing_moves(tmp_path, target, monkeypatch):
    small_chunks(monkeypatch, 2)
    source = many_chunks(tmp_path, 32)
    data = (source / "disk.iso").read_bytes()
    c = job(source, target)
    c.check()
    c.prepare()
    Path(c._part("disk.iso")).write_bytes(data[:30 << 16])
    n = {"n": 0}

    def flaps(name, a):  # more drops than the limit, with a little more compared between each
        if name == "read":
            n["n"] += 1
            if n["n"] % 5 == 0:
                return OSError(errno.ECONNRESET, "reset")
        return None

    Faults(c.source, flaps, away=1)
    r = c.run()
    assert r.done and (target / "disk.iso").read_bytes() == data


def test_a_disk_refusing_times_isn_t_asked_patiently_every_time(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, lambda name, a: eio() if name == "utime" else None)
    r = c.run()
    assert r.done and r.no_times == 4 and len(c.slept) == 3


# ---- what gave up where it should wait ---------------------------------------------------------------------------------------

def test_another_program_holding_the_record_is_waited_for(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    held = {"n": 0}

    def which(name, a):
        if name == "the record" and held["n"] < 6:
            held["n"] += 1
            return PermissionError(errno.EACCES, "Permission denied")
        return None

    Faults(c.target, which)
    r = c.run()
    assert r.done and held["n"] == 6
    assert copied_says(target) == set(contents(source))


def test_a_drop_while_asking_for_the_free_space_is_waited_out(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, nth("disk_usage", 1, OSError(errno.ECONNRESET, "reset")), away=1)
    assert c.run().done


def test_a_moment_s_trouble_with_a_file_s_time_is_tried_again(source, target):
    os.utime(source / "a.epub", (1_600_000_000, 1_600_000_000))
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, nth("utime", 1, OSError(errno.ECONNRESET, "reset")))
    r = c.run()
    assert r.done and not r.no_times and (target / "a.epub").stat().st_mtime == 1_600_000_000


def test_a_disk_that_won_t_keep_times_is_told_and_no_reason_to_fail(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, lambda name, a: PermissionError(errno.EPERM, "Not permitted") if name == "utime" else None)
    r = c.run()
    assert r.done and r.no_times == 4 and "keep the time" in (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")


def test_a_closed_pipe_doesn_t_stop_a_copy(source, target):
    def closed(*a, **k):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    c = hc.Copying(source, target, say=closed, sleep=lambda s: None)
    c.check()
    c.prepare()
    r = c.run()
    assert r.done and c.tell_of_the_end(r)


@pytest.mark.skipif(os.name != "nt", reason="Windows' fsync says 'bad file' whatever went wrong")
def test_a_failed_flush_says_what_really_went_wrong(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"x")
    with open(path, "rb") as fh, pytest.raises(OSError) as e:  # a handle that can't be flushed: access denied
        hc._keep_to_disk(fh)
    assert e.value.winerror == 5


def test_a_disk_that_takes_nothing_is_an_error_not_a_loop():
    class TakesNothing:
        def write(self, data):
            return 0

    with pytest.raises(OSError):
        hc._write_all(TakesNothing(), b"data")


def test_a_report_open_elsewhere_is_written_in_place(tmp_path, monkeypatch):
    path = tmp_path / "report.txt"
    path.write_text("DONE, from before")

    def refused(a, b):
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(os, "replace", refused)
    hc._write_here(str(path), "NOT FINISHED")
    assert path.read_text() == "NOT FINISHED" and os.listdir(tmp_path) == ["report.txt"]


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses a swap for an open file")
def test_a_report_open_elsewhere_really_changes(source, target):
    copy(source, target)
    report = target / hc.FOLDER / "report.txt"
    (source / "new.epub").write_bytes(b"new")
    with open(report, "rb"):
        c, r = copy(source, target)
        assert r.done
    assert "1 file" in report.read_text(encoding="utf-8")


def test_a_report_that_can_t_be_changed_is_said_at_the_start(source, target, monkeypatch):
    copy(source, target)
    real = hc._write_here

    def locked(path, data):
        if str(path).endswith("report.txt"):
            raise PermissionError(errno.EACCES, "Access is denied")
        real(path, data)

    monkeypatch.setattr(hc, "_write_here", locked)
    c = job(source, target)
    c.check()
    c.prepare()
    c.run()
    assert any("may still tell of an earlier run" in line for line in c.lines)
    assert any("report.txt couldn't be written" in line for line in c.lines)
    assert (target / hc.FOLDER / "manifest.sha256").exists()


def test_what_writes_for_itself_makes_no_folder_on_an_unmounted_disk_s_place(tmp_path):
    with pytest.raises(OSError):
        hc._write_here(str(tmp_path / "not-there" / "report.txt"), "x")
    with pytest.raises(OSError):
        hc._make_below(str(tmp_path / "not-there"), "a/b")
    assert not (tmp_path / "not-there").exists()
    hc._make_below(str(tmp_path), "a/b")
    assert (tmp_path / "a" / "b").is_dir()
    (tmp_path / "a" / "file").write_text("x")
    with pytest.raises(OSError):
        hc._make_below(str(tmp_path), "a/file")
    with pytest.raises(OSError):
        hc._make_below(str(tmp_path), "a/file/c")


def test_a_check_leaves_no_folder_behind_if_the_disk_went(source, target):
    copy(source, target)
    v = hc.Verifying(target, say=lambda *a, **k: None, sleep=lambda s: None)
    v.check()
    v.target.mark = None
    shutil.rmtree(target)
    target.mkdir()
    v.run()
    assert not (target / hc.FOLDER).exists()


def test_the_end_of_a_copy_doesn_t_wait_for_a_stuck_disk_elsewhere(monkeypatch):
    import threading

    stuck = threading.Event()
    monkeypatch.setattr(os, "sync", stuck.wait, raising=False)
    monkeypatch.setattr(hc, "SYNC_WAIT", 0.05)
    began = time.monotonic()
    hc._sync_names()
    stuck.set()
    assert time.monotonic() - began < 5


# ---- the target's mark ----------------------------------------------------------------------------------------------------

def test_a_target_whose_mark_has_gone_is_refused_until_told_otherwise(source, target):
    copy(source, target)
    shutil.rmtree(target)
    target.mkdir()  # what an unmounted disk leaves, in a place not under /mnt or /media
    c = job(source, target)
    c.check()
    with pytest.raises(Exit) as e:
        c.prepare()
    assert e.value.code == exits.NOT_SET_UP and "Is the disk mounted" in e.value.message and "--new-disk" in e.value.message
    assert not os.listdir(target)
    p = job(source, target)
    p.check()
    p.plan()
    assert any("mark it left" in line for line in p.lines)
    d, r = copy(source, target, new_disk=True)
    assert r.done and contents(target) == contents(source)
    _, again = copy(source, target)  # and it's a known target again
    assert again.done


def test_the_command_takes_new_disk(source, target):
    cli.main(["copy", str(source), str(target), "--yes"])
    shutil.rmtree(target)
    assert code_of(source, target) == exits.NOT_SET_UP  # a target copied to before isn't asked about: it's refused
    cli.main(["copy", str(source), str(target), "--new-disk", "--yes"])  # a first copy to the new disk: it asks, as any first
    assert contents(target) == contents(source)
    assert code_of("check", target, "--new-disk") == exits.USAGE


def test_a_known_target_with_another_mark_is_refused(source, target):
    """The disk away, and a .hyllycopy in its folder from an earlier mistake."""
    copy(source, target)
    (target / hc.FOLDER / "id").write_text("a-mark-from-some-other-time\n")
    c = job(source, target)
    c.check()
    with pytest.raises(Exit) as e:
        c.prepare()
    assert e.value.code == exits.NOT_SET_UP and "another now" in e.value.message
    assert copy(source, target, new_disk=True)[1].done
    assert copy(source, target)[1].done


def test_a_damaged_byte_in_the_mark_is_read_the_same_everywhere(source, target):
    copy(source, target)
    (target / hc.FOLDER / "id").write_bytes(b"abc\xffdef\n")
    c = job(source, target, new_disk=True)
    c.check()
    c.prepare()
    assert c.target.answers() and c.run().done


def test_a_mark_that_can_t_be_read_is_not_replaced(source, target, monkeypatch):
    copy(source, target)
    mark = target / hc.FOLDER / "id"
    before = mark.read_text()

    def unreadable(path):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(hc, "_read_mark", unreadable)
    c = job(source, target)
    c.check()
    with pytest.raises(Exit) as e:
        c.prepare()
    assert e.value.code == exits.LOCAL and mark.read_text() == before


def test_a_mark_another_run_has_just_made_counts(source, target):
    c = job(source, target)
    target.joinpath(hc.FOLDER).mkdir(parents=True)
    mark = target / hc.FOLDER / "id"
    mark.write_text("theirs\n")
    assert c._make_mark(str(mark)) == "theirs" and mark.read_text() == "theirs\n"
    mark.write_text("")
    made = c._make_mark(str(mark))
    assert made and mark.read_text().strip() == made and len(os.listdir(mark.parent)) == 1


# ---- names as the disk tells them apart ---------------------------------------------------------------------------------------

def test_names_are_told_apart_as_far_as_the_disk_does(tmp_path, target):
    c = job(tree(tmp_path / "from", {"a.txt": "a"}), target)
    c.blind, c.one_form = True, False
    assert c._key("Straße.txt") != c._key("Strasse.txt") and c._key("BOOK.txt") == c._key("book.txt")
    assert c._key("caf\u00e9.txt") != c._key("cafe\u0301.txt")
    c.one_form = True
    assert c._key("caf\u00e9.txt") == c._key("cafe\u0301.txt")
    c.blind, c.one_form = False, False
    assert c._key("BOOK.txt") != c._key("book.txt")


def test_strasse_and_strasse_are_two_files(tmp_path, target):
    source = tree(tmp_path / "from", {"Stra\u00dfe.txt": "with", "Strasse.txt": "without"})
    _, r = copy(source, target)
    assert r.done and contents(target) == contents(source)


def test_your_own_file_of_a_similar_name_doesn_t_stand_in_for_a_copy(tmp_path, target):
    source = tree(tmp_path / "from", {"Strasse.txt": "abc", "z.txt": "z"})
    copy(source, target)
    (target / "Strasse.txt").unlink()
    (target / "Stra\u00dfe.txt").write_text("xyz")
    one_name = (target / "Strasse.txt").exists()  # to a Mac's disk the two are one name: then it is yours, and stays
    _, r = copy(source, target)
    if one_name:
        assert not r.done and (target / "Stra\u00dfe.txt").read_text() == "xyz"
    else:
        assert r.done and (target / "Strasse.txt").read_text() == "abc"


def test_nothing_is_written_through_a_link_on_the_target(tmp_path, source, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    target.mkdir()
    link_to(outside, target / "sub")
    _, r = copy(source, target)
    assert not os.listdir(outside) and not r.done
    assert "through a link" in r.failed["sub/b.epub"] and "through a link" in r.failed["sub/deep/c.pdf"]
    assert (target / "a.epub").exists()
    assert "sub/" in r.failed and "link" in r.failed["sub/"]


# ---- the source and the target, again -------------------------------------------------------------------------------------------

def test_a_target_not_there_yet_is_still_checked_by_the_file_numbers(tmp_path, source, monkeypatch):
    """What a share reached by two names gives: the paths don't tell, and only the numbers do."""
    disguise = tmp_path / "disguise"
    link_to(source, disguise)
    monkeypatch.setattr(os.path, "realpath", lambda p, **k: str(p))
    assert "inside the source" in hc.overlap(str(source), str(disguise / "new" / "folder"))
    assert "inside the target" in hc.overlap(str(disguise / "sub"), str(source))


def test_the_source_inside_the_target_is_seen_by_the_mark_above_it(tmp_path, monkeypatch):
    """A server that gives no file numbers leaves nothing else to go by."""
    monkeypatch.setattr(hc, "overlap", lambda a, b: None)
    target = tree(tmp_path / "to", {"sub/x.txt": "x"})
    c = job(target / "sub", target)
    c.check()
    with pytest.raises(Exit) as e:
        c.prepare()
    assert e.value.code == exits.USAGE and "inside the target" in e.value.message


# ---- small things ---------------------------------------------------------------------------------------------------------------

def test_a_folder_that_failed_once_doesn_t_stick(tmp_path, target):
    source = tree(tmp_path / "from", {"a.txt": "a"})
    (source / "empty").mkdir()
    c = job(source, target)
    c.check()
    c.prepare()
    tried = {"n": 0}

    def once(name, a):
        if name == "_make_below" and a[1] == "empty" and not tried["n"]:
            tried["n"] = 1
            return FileNotFoundError(errno.ENOENT, "Not now")
        return None

    Faults(c.target, once)
    r = c.run()
    assert r.done and tried["n"] == 1 and (target / "empty").is_dir()


def test_folders_are_counted_apart_from_files(tmp_path, target):
    source = tree(tmp_path / "from", {"a.txt": "a"})
    (source / "empty").mkdir()
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, lambda name, a: FileNotFoundError(errno.ENOENT, "No") if name == "_make_below" and a[1] == "empty" else None)
    r = c.run()
    words = c.tell_of_the_end(r)
    assert not r.done and "1 of 1 files are copied and checked, 0 aren't, and 1 folder or file" in words


def test_one_bad_entry_costs_that_entry(tmp_path, target, monkeypatch):
    source = tree(tmp_path / "from", {f"f{i}.txt": "x" for i in range(5)})
    real = os.scandir

    class Entry:
        def __init__(self, entry):
            self.entry, self.name = entry, entry.name

        def stat(self, **k):
            if self.name == "f2.txt":
                raise OSError(errno.EIO, "Input/output error")
            return self.entry.stat(**k)

    class Folder:
        def __init__(self, path):
            self.it = real(path)

        def __enter__(self):
            return map(Entry, self.it.__enter__())

        def __exit__(self, *a):
            return self.it.__exit__(*a)

    monkeypatch.setattr(os, "scandir", Folder)
    c, r = copy(source, target)
    monkeypatch.setattr(os, "scandir", real)
    assert not r.done and set(r.failed) == {"f2.txt"} and "can't be looked at" in r.failed["f2.txt"]
    assert set(contents(target)) == {"f0.txt", "f1.txt", "f3.txt", "f4.txt"}


def test_a_listing_of_the_target_gone_short_forgets_nothing(source, target, monkeypatch):
    """The originals are gone as the report invites, and the checksum in the record is all there is."""
    copy(source, target)
    (source / "z.cbz").unlink()
    real = hc._entries
    root = hc.long_path(target)

    def short(path):
        found, bad = real(path)
        return ([e for e in found if e[0] != "z.cbz"] if path == root else found), bad

    monkeypatch.setattr(hc, "_entries", short)
    copy(source, target)
    monkeypatch.setattr(hc, "_entries", real)
    assert "z.cbz" in (target / hc.FOLDER / "manifest.sha256").read_text(encoding="utf-8") and (target / "z.cbz").exists()


def test_the_record_forgets_what_has_left_both_sides(source, target):
    copy(source, target)
    (source / "z.cbz").unlink()
    (target / "z.cbz").unlink()
    v = verifying(target)
    v.check()
    assert v.run() == 1  # until the next copy says so
    _, r = copy(source, target)
    assert r.done and "z.cbz" not in (target / hc.FOLDER / "manifest.sha256").read_text(encoding="utf-8")
    v = verifying(target)
    v.check()
    assert v.run() == 0


def test_a_copy_whose_original_was_removed_stays_in_the_record(source, target):
    copy(source, target)
    (source / "z.cbz").unlink()
    _, r = copy(source, target)
    assert r.done and "z.cbz" in (target / hc.FOLDER / "manifest.sha256").read_text(encoding="utf-8")
    v = verifying(target)
    v.check()
    assert v.run() == 0


def test_a_target_folder_that_can_t_be_listed_makes_nothing_forgotten(source, target):
    copy(source, target)
    (source / "sub" / "b.epub").unlink()
    c = job(source, target)
    c.check()
    c.prepare()
    Faults(c.target, lambda name, a: PermissionError(errno.EACCES, "Permission denied") if name == "_entries" and str(a[0]).endswith("sub") else None)
    c.run()
    assert "sub/b.epub" in (target / hc.FOLDER / "manifest.sha256").read_text(encoding="utf-8")


# ---- what isn't copied ----------------------------------------------------------------------------------------------

def test_the_system_s_own_files_are_left_out_and_listed(tmp_path, target):
    source = tree(tmp_path / "from", {"a.epub": "a", ".DS_Store": "x", "sub/._a.epub": "x", "sub/Thumbs.db": "x", "sub/desktop.ini": "x",
                                      ".Spotlight-V100/store": "x", "$RECYCLE.BIN/old": "x", "@eaDir/thumb": "x"})
    _, r = copy(source, target)
    assert r.done and set(contents(target)) == {"a.epub"}
    report = (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    assert "Left out (7)" in report and ".DS_Store" in report and "$RECYCLE.BIN" in report


def test_everything_means_those_too(tmp_path, target):
    source = tree(tmp_path / "from", {"a.epub": "a", ".DS_Store": "x", "$RECYCLE.BIN/old": "x"})
    _, r = copy(source, target, everything=True)
    assert r.done and set(contents(target)) == {"a.epub", ".DS_Store", "$RECYCLE.BIN/old"}


def test_a_copy_s_own_folder_isn_t_copied(tmp_path, source, target):
    copy(source, target)
    second = tmp_path / "second"
    _, r = copy(target, second)
    assert r.done and set(contents(second)) == set(contents(source)) and not r.listing.left_out
    assert not (second / hc.FOLDER / "report.txt").read_text(encoding="utf-8").startswith("x")
    assert "4 files" in (second / hc.FOLDER / "report.txt").read_text(encoding="utf-8")


def test_another_copy_s_own_folder_deeper_in_the_source_goes_along_and_can_be_checked_there(tmp_path, source):
    """A backup's disk moved to a bigger one: its books' record and checksums, in library/.hyllycopy, are copied like any
    folder, so that the books can be checked on the new disk as on the old. Only the source's own, at its top, stays
    behind, as the new copy has one of its own."""
    disk, bigger = tmp_path / "disk", tmp_path / "bigger"
    old = disk / "old-backup" / "library"
    copy(source, old)
    copy(source, disk)  # the disk's own, at its top
    _, r = copy(disk, bigger)
    moved = bigger / "old-backup" / "library"
    assert r.done and not r.listing.left_out
    for name in ("record.jsonl", "manifest.sha256", "report.txt"):
        assert (moved / hc.FOLDER / name).read_bytes() == (old / hc.FOLDER / name).read_bytes()
    assert hc._read_mark(str(bigger / hc.FOLDER / "id")) != hc._read_mark(str(disk / hc.FOLDER / "id"))
    assert "old-backup/library/.hyllycopy/record.jsonl" in (bigger / hc.FOLDER / "manifest.sha256").read_text(encoding="utf-8")
    v = verifying(moved)
    v.check()
    assert v.run() == 0 and v.total_files == 4


@pytest.mark.skipif(NO_SYMLINKS, reason="symbolic links take rights this Windows user may not have")
def test_links_are_left_out_and_never_followed(tmp_path, source, target):
    outside = tree(tmp_path / "outside", {"secret.txt": "not to be copied"})
    os.symlink(outside, source / "to-folder")
    os.symlink(outside / "secret.txt", source / "to-file")
    os.symlink(source, source / "sub" / "back-to-the-top")
    _, r = copy(source, target)
    assert r.done and set(contents(target)) == {"a.epub", "sub/b.epub", "sub/deep/c.pdf", "z.cbz"}
    assert {rel for rel, what in r.listing.left_out if what == "a link"} == {"to-folder", "to-file", "sub/back-to-the-top"}


@pytest.mark.skipif(os.name != "nt", reason="Windows' own kind of link")
def test_a_junction_is_left_out_and_never_followed(tmp_path, source, target):
    outside = tree(tmp_path / "outside", {"secret.txt": "not to be copied"})
    link_to(outside, source / "junction")
    _, r = copy(source, target)
    assert r.done and "junction/secret.txt" not in contents(target) and ("junction", "a link") in r.listing.left_out


@pytest.mark.skipif(os.name == "nt", reason="Windows has no such files")
def test_what_isn_t_a_file_is_left_out_rather_than_waited_on(source, target):
    os.mkfifo(source / "a-pipe")
    _, r = copy(source, target)
    assert r.done and ("a-pipe", "not a file") in r.listing.left_out


@pytest.mark.parametrize("name,windows,bad", [
    ("plain.epub", True, False), ("sub/fine (1).epub", True, False), ("a:b.epub", True, True), ("a:b.epub", False, False),
    ("x/what?.epub", True, True), ("dot.", True, True), ("space ", True, True), ("CON", True, True), ("con.txt", True, True),
    ("console.txt", True, False), ("back\\slash", True, True), ("n" * 256, False, True), ("n" * 255, False, False),
    ("COM0.epub", True, True), ("LPT0", True, True), ("CONIN$", True, True), ("conout$.txt", True, True), ("NUL .epub", True, True),
    ("COM10.epub", True, False), ("COM0.epub", False, False)])
def test_names_a_disk_can_t_hold_are_known_beforehand(name, windows, bad):
    assert bool(hc.name_problem(name, windows)) == bad


def test_a_name_windows_would_turn_into_a_hidden_stream_isn_t_tried(tmp_path, target, monkeypatch):
    source = tree(tmp_path / "from", {"a.txt": "a"})
    c = job(source, target)
    c.check()
    c.prepare()
    c.windows_names = True
    monkeypatch.setattr(c, "_probe", lambda: None)
    listing = hc.Listing(files={"a.txt": (1, 0), "b:c.txt": (1, 0)})
    assert list(c._no_home(listing)) == ["b:c.txt"]


def test_two_names_a_disk_can_t_tell_apart(tmp_path, target):
    c = job(tree(tmp_path / "from", {"a.txt": "a"}), target)
    c.windows_names, c.blind = False, True
    no_home = c._no_home(hc.Listing(files={"Book.epub": (1, 0), "book.epub": (1, 0), "other.epub": (1, 0)}))
    assert list(no_home) == ["book.epub"] and "capitals" in no_home["book.epub"]
    c.blind = False
    assert not c._no_home(hc.Listing(files={"Book.epub": (1, 0), "book.epub": (1, 0)}))


@pytest.mark.skipif(os.name == "nt", reason="Windows goes by Windows' rules, whatever the disk")
def test_the_disk_is_asked_about_its_names(source, target):
    c = job(source, target)
    c.check()
    c.prepare()
    assert c.windows_names is False and c.blind == os.path.exists(target / hc.FOLDER / "ID")


def test_long_paths_are_no_trouble(tmp_path, target):
    deep = "/".join(["a folder with a rather long name"] * 12) + "/the book.epub"
    assert len(deep) > 300
    source = tmp_path / "from"
    full = hc.long_path(source) + os.sep + deep.replace("/", os.sep)
    os.makedirs(os.path.dirname(full))
    with open(full, "wb") as fh:
        fh.write(b"deep down")
    c, r = copy(source, target)
    assert r.done and r.copied == 1
    with open(hc.long_path(target) + os.sep + deep.replace("/", os.sep), "rb") as fh:
        assert fh.read() == b"deep down"


@pytest.mark.skipif(sys.platform != "linux", reason="names that aren't UTF-8 are for Linux to have")
def test_a_name_that_isn_t_utf8(tmp_path, target):
    source = tmp_path / "from"
    source.mkdir()
    raw = os.path.join(os.fsencode(source), b"caf\xe9.epub")
    with open(raw, "wb") as fh:
        fh.write(b"bytes")
    c, r = copy(source, target)
    assert r.done
    with open(os.path.join(os.fsencode(target), b"caf\xe9.epub"), "rb") as fh:
        assert fh.read() == b"bytes"
    assert b"caf\xe9.epub\n" in (target / hc.FOLDER / "manifest.sha256").read_bytes()
    assert hc.Verifying(target, say=lambda *a, **k: None) is not None
    _, again = copy(source, target)
    assert again.earlier == 1


def test_a_name_can_t_start_a_line_or_move_the_cursor():
    assert hc.shown("a\nFAKE LINE\x1b[2J ") == "a?FAKE LINE?[2J?"
    assert hc.shown("caf\udce9") == "caf?"


# ---- source and target ----------------------------------------------------------------------------------------------

def refused(source, target) -> Exit:
    c = job(source, target)
    with pytest.raises(Exit) as e:
        c.check()
    return e.value


def test_the_source_can_t_be_the_target_or_overlap_it(source):
    assert refused(source, source).code == exits.USAGE
    assert refused(source, source / "sub").code == exits.USAGE
    assert refused(source, source / "new").code == exits.USAGE
    assert refused(source / "sub", source).code == exits.USAGE
    assert (source / "sub").is_dir() and not (source / "new").exists()


def test_the_target_can_t_be_the_source_in_disguise(tmp_path, source):
    disguise = tmp_path / "disguise"
    link_to(source, disguise)
    assert "same folder" in refused(source, disguise).message
    assert "inside" in refused(source, disguise / "sub").message
    assert "inside" in refused(source, disguise / "new" / "folder").message


def test_a_source_or_a_target_below_a_file_is_refused_at_once_and_never_waited_for(tmp_path, source, monkeypatch, capsys):
    """No folder can be made in a file, and the system says only "not a directory" of a place below one (Windows: "the
    path isn't there"). Taken for a disk gone away, a copy there was waited out, about two minutes with nobody at the
    keyboard, and a note went to the phone. Now it is refused before anything is asked of the disk: the command's code
    2, its words, no note, and nothing written below the file."""
    a_file = tmp_path / "a file"
    a_file.write_text("a file, not a folder", encoding="utf-8")
    for below in (a_file / "below", a_file / "below" / "deeper"):
        e = refused(source, below)
        assert e.code == exits.USAGE and f"The target should be a folder: {below} is below {a_file}, which is a file" in e.message
        e = refused(below, tmp_path / "copy")
        assert e.code == exits.USAGE and f"The source should be a folder: {below} is below {a_file}" in e.message
    assert "isn't one" in refused(source, a_file).message  # the file itself: as it always was
    noted = []
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: noted.append(a) or True)
    monkeypatch.setattr(cli, "at_keyboard", lambda: False)  # as a scheduled run, which waited the longest
    assert code_of(source, a_file / "below", "--yes") == exits.USAGE
    assert "no folder can be made in a file. Nothing was copied." in capsys.readouterr().err and noted == []
    assert a_file.read_text(encoding="utf-8") == "a file, not a folder" and sorted(p.name for p in tmp_path.iterdir()) == ["a file", "from"]


def test_a_disguise_only_a_look_at_the_files_shows(source, monkeypatch):
    """A share reached by two names that nothing tells are the same: the target's mark is seen from the source."""
    monkeypatch.setattr(hc, "overlap", lambda a, b: None)
    c = job(source, source / "sub" / "new")
    c.check()
    c.prepare()
    with pytest.raises(Exit) as e:
        c.run()
    assert e.value.code == exits.USAGE and "inside the source" in e.value.message
    d = job(source, source)
    d.check()
    with pytest.raises(Exit):
        d.prepare()


def test_what_can_t_be_a_source_or_a_target(tmp_path, source):
    file = source / "a.epub"
    assert refused(file, tmp_path / "to").code == exits.USAGE
    assert refused(source, file).code == exits.USAGE
    assert refused(tmp_path / "not-there", tmp_path / "to").code == exits.LOCAL
    (tmp_path / "empty").mkdir()
    empty = refused(tmp_path / "empty", tmp_path / "to")
    assert empty.code == exits.LOCAL and "nothing in the source" in empty.message


@pytest.mark.skipif(os.name == "nt", reason="Windows doesn't mount disks in folders this way")
def test_a_place_where_a_disk_isn_t_mounted_is_refused(tmp_path, monkeypatch):
    place = tmp_path / "mnt" / "bigdisk"
    place.mkdir(parents=True)
    monkeypatch.setattr(hc, "NOT_THERE_YET", (str(tmp_path / "mnt"),))
    assert hc.looks_unmounted(str(place / "books")) and hc.looks_unmounted(str(place))
    (place / "something").write_text("x")
    assert not hc.looks_unmounted(str(place / "books"))
    monkeypatch.setattr(hc, "NOT_THERE_YET", ())
    (place / "something").unlink()
    assert not hc.looks_unmounted(str(place))  # an empty folder of your own, anywhere else
    monkeypatch.setattr(hc, "NOT_THERE_YET", (str(tmp_path / "mnt"),))
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    assert not hc.looks_unmounted(str(place))  # a new, empty disk


def as_fstab_writes_it(path: Path) -> str:
    return str(path).replace("\\", "\\134").replace(" ", "\\040")


@pytest.mark.skipif(os.name == "nt", reason="Windows doesn't mount disks in folders this way")
def test_a_place_fstab_mounts_a_disk_at_is_no_target_while_no_disk_is_mounted_there(tmp_path, monkeypatch, source, capsys):
    """On Linux, a place /etc/fstab lists, wherever it is (/srv/books, say, as examples/hylly.service has it), with no disk
    mounted there: a target at or below it is refused, empty or not, the plan too, and nothing is made there; once a disk
    is mounted there, by the kernel's list (a bind mount, which os.path.ismount can't tell, included), it's taken. A
    made-up fstab and list of mounts, never this computer's."""
    disk = tmp_path / "srv" / "my books"  # a space, as fstab writes it: \040
    disk.mkdir(parents=True)
    fstab, mounts = tmp_path / "fstab", tmp_path / "mountinfo"
    fstab.write_text("# made up\nUUID=0000-1111 / ext4 defaults 0 1\n/swapfile none swap sw 0 0\nproc /proc proc defaults 0 0\n"
                     f"UUID=2222-3333 {as_fstab_writes_it(disk)} ntfs3 defaults,nofail 0 0\n", encoding="utf-8")
    root_only = "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    mounts.write_text(root_only, encoding="utf-8")
    monkeypatch.setattr(hc, "FSTAB", str(fstab))
    monkeypatch.setattr(hc, "MOUNTED_NOW", str(mounts))
    monkeypatch.setattr(hc, "ON_LINUX", True)
    for at_or_below in (disk, disk / "backup", disk / "backup" / "deeper"):
        assert hc.unmounted_place(str(at_or_below)) == str(disk)
    (disk / "left there").write_text("x")  # not empty: still not the disk
    assert hc.unmounted_place(str(disk / "backup")) == str(disk)
    assert hc.unmounted_place(str(tmp_path / "srv")) is None  # above it
    assert hc.unmounted_place(str(tmp_path / "srv" / "my books 2")) is None  # beside it, by a name that starts the same
    e = refused(source, disk / "backup")
    assert e.code == exits.NOT_SET_UP and "/etc/fstab" in e.message and "Mount the disk first" in e.message
    with pytest.raises(SystemExit) as plan:
        cli.main(["copy", str(source), str(disk / "backup"), "--dry-run"])
    assert plan.value.code == exits.NOT_SET_UP and "/etc/fstab" in capsys.readouterr().err
    assert not (disk / "backup").exists()

    mounts.write_text(root_only + f"40 22 8:17 / {as_fstab_writes_it(disk)} rw,relatime - ntfs3 /dev/sdb1 rw\n", encoding="utf-8")
    assert hc.unmounted_place(str(disk / "backup")) is None  # mounted, though os.path.ismount says no
    _, r = copy(source, disk / "backup")
    assert r.done and contents(disk / "backup") == contents(source)

    mounts.unlink()  # no list of mounts to read: os.path.ismount says
    assert hc.unmounted_place(str(disk / "backup")) == str(disk)
    monkeypatch.setattr(os.path, "ismount", lambda p: p in ("/", str(disk)))
    assert hc.unmounted_place(str(disk / "backup")) is None
    monkeypatch.setattr(os.path, "ismount", lambda p: p == "/")
    fstab.unlink()  # no fstab to read: nothing to say
    assert hc.unmounted_place(str(disk / "backup")) is None
    fstab.write_text(f"UUID=2222-3333 {as_fstab_writes_it(disk)} ntfs3 defaults 0 0\n", encoding="utf-8")
    monkeypatch.setattr(hc, "ON_LINUX", False)  # Windows and macOS: as before
    assert hc.unmounted_place(str(disk / "backup")) is None


def test_no_test_goes_by_this_computer_s_fstab():
    """conftest gives every test a made-up empty fstab: on Linux, a test's folder below a place this computer's fstab
    lists, and its list of mounts doesn't show, would have every copy, backup and restore refused."""
    assert hc.FSTAB != "/etc/fstab" and Path(hc.FSTAB).read_text(encoding="utf-8") == "" and hc.unmounted_place("/") is None


@pytest.mark.skipif(os.name == "nt", reason="Windows doesn't mount disks in folders this way")
def test_a_disk_mounted_deeper_on_the_way_than_fstab_s_place_is_a_disk(tmp_path, monkeypatch, source):
    """The deepest place fstab lists on the way to the target counts, and only when nothing is mounted deeper on that
    way: /srv listed with noauto and not mounted says nothing against a target on /srv/books, mounted from fstab, nor
    /mnt listed and not mounted against one on a disk mounted by hand at /mnt/usb; beside those disks, the places are
    still no target. Made-up files, as above."""
    srv, books, mnt, usb = tmp_path / "srv", tmp_path / "srv" / "books", tmp_path / "mnt", tmp_path / "mnt" / "usb"
    for folder in (books, usb):
        folder.mkdir(parents=True)
    fstab, mounts = tmp_path / "fstab", tmp_path / "mountinfo"
    fstab.write_text(f"UUID=0 / ext4 defaults 0 1\nUUID=1 {srv} ext4 noauto,nofail 0 2\nUUID=2 {books} ntfs3 nofail 0 0\n"
                     f"LABEL=data {mnt} ext4 noauto 0 0\n", encoding="utf-8")
    mounts.write_text(f"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 22 8:17 / {books} rw - ntfs3 /dev/sdb1 rw\n"
                      f"41 22 8:33 / {usb} rw - exfat /dev/sdc1 rw\n", encoding="utf-8")
    monkeypatch.setattr(hc, "FSTAB", str(fstab))
    monkeypatch.setattr(hc, "MOUNTED_NOW", str(mounts))
    monkeypatch.setattr(hc, "ON_LINUX", True)
    assert hc.unmounted_place(str(books / "library")) is None and hc.unmounted_place(str(books)) is None
    assert hc.unmounted_place(str(usb / "backup")) is None
    assert hc.unmounted_place(str(srv / "other")) == str(srv) and hc.unmounted_place(str(mnt / "other")) == str(mnt)
    _, r = copy(source, usb / "backup")
    assert r.done and contents(usb / "backup") == contents(source)
    mounts.unlink()  # no list of mounts: each folder on the way is asked
    monkeypatch.setattr(os.path, "ismount", lambda p: p in ("/", str(books), str(usb)))
    assert hc.unmounted_place(str(books / "library")) is None and hc.unmounted_place(str(usb / "backup")) is None
    assert hc.unmounted_place(str(mnt / "other")) == str(mnt)


def copying_in_the_help() -> str:
    text = (Path(__file__).resolve().parents[1] / "src" / "hylly" / "help.html").read_text(encoding="utf-8")
    return text[text.index('<section id="copying">'):text.index("</section>", text.index('<section id="copying">'))]


def test_the_help_names_every_place_watched_for_an_unmounted_disk():
    """Which places are watched, the help says, as the code has them: /etc/fstab's, at every copy, and the four
    where disks are usually mounted, before a target has a mark."""
    section = copying_in_the_help()
    assert [place for place in ("/etc/fstab", *hc.NOT_THERE_YET) if f"<code>{place}</code>" not in section] == []
    assert "On Linux, at every copy, a target at or below a place <code>/etc/fstab</code> mounts a disk at" in section


def test_the_help_says_what_comes_after_moving_a_library():
    """HyllyCopy's section offers a move to a new computer, so it says what comes after the copy: the server and
    its jobs stopped first, and started again after, on Linux, Windows and macOS; on the same computer the library
    setting, and on a new one the records, by a backup and its restore, which makes a fresh copy of them, or by the
    database's own file, with its last changes beside it."""
    from hylly import db

    after = copying_in_the_help().split('id="after-copy"', 1)[1]
    assert all(said in after for said in (
        "hylly server stop", "sudo systemctl stop hylly-jobs.timer", "sudo systemctl start hylly-jobs.timer", "Task Scheduler",
        "macOS", "<code>library</code>", "hylly backup TARGET", "hylly restore BACKUP NEW_FOLDER",
        f"<code>{db.FILE}</code>", f"<code>{db.FILE}-wal</code>"))


def test_a_long_path_a_windows_share_refuses_is_said_to_be_too_long_or_not_allowed_and_isn_t_tried_again(source, target, monkeypatch):
    """Windows says "access denied" for a path some shares find too long, past some 510 letters, and the same for a
    real lack of rights: so it's said as either. A shorter path, or one on a disk of this computer, is waited out as
    any refusal is."""
    share = "\\\\?\\UNC\\made-up-nas\\books\\" + "a" * 490
    assert hc.long_for_a_share(share, windows=True) and not hc.long_for_a_share(share, windows=False)
    assert not hc.long_for_a_share(share[:400], windows=True)
    assert not hc.long_for_a_share("\\\\?\\C:\\" + "a" * 600, windows=True)
    c = job(source, target)
    c.check()
    c.prepare()
    monkeypatch.setattr(hc, "long_for_a_share", lambda path: path.endswith("a.epub"))  # as such a path on such a share
    Faults(c.target, lambda name, a: PermissionError(errno.EACCES, "Access is denied")
           if name == "_no_overwrite_name" and str(a[1]).endswith("a.epub") else None)
    r = c.run()
    assert not r.done and r.failed == {"a.epub": "its path is too long for this share, or not allowed"}
    assert c.tries["a.epub"] == 1 and (target / "sub" / "b.epub").is_file()


def test_a_target_that_won_t_take_writes_is_said_at_once(source, target, monkeypatch):
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "Permission denied")))
    c = job(source, target)
    c.check()
    with pytest.raises(Exit) as e:
        c.prepare()
    assert e.value.code == exits.LOCAL and "read or write" in e.value.message


@pytest.mark.skipif(os.name == "nt", reason="permissions on Linux and macOS")
def test_permissions_are_kept_and_a_read_only_part_is_no_obstacle(tmp_path, target, monkeypatch):
    """By the mode each part is made with, never by a chmod on the target, the library's next disk or a backup's: a
    disk that keeps modes as extended attributes (Linux's ntfs3) would store one for each file, and one that refuses
    chmod would fail it. A read-only original's copy is writable by its owner, which a part has to be to go on after a
    drop; a part an older Hylly left read-only is made anew."""
    source = tree(tmp_path / "from", {"script.sh": "run me", "fixed.txt": "fixed"})
    os.chmod(source / "script.sh", 0o755)
    os.chmod(source / "fixed.txt", 0o444)
    c = job(source, target)
    c.check()
    c.prepare()
    part = Path(c._part("fixed.txt"))
    part.write_bytes(b"fi")
    os.chmod(part, 0o444)  # stopped just before it got its name
    real = os.chmod

    def chmod(path, *a, **k):
        assert not str(path).startswith(str(target)), f"a chmod on the target: {path}"
        return real(path, *a, **k)

    monkeypatch.setattr(os, "chmod", chmod)
    before = os.umask(0o022)  # a usual one: a copy's mode is its original's, as far as the umask allows
    try:
        r = c.run()
    finally:
        os.umask(before)
    assert r.done and (target / "fixed.txt").read_text() == "fixed"
    assert (target / "script.sh").stat().st_mode & 0o777 == 0o755 and (target / "fixed.txt").stat().st_mode & 0o777 == 0o644


# ---- reading past the cache -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, hc.CHUNK - 1, hc.CHUNK, hc.CHUNK + 1, 2 * hc.CHUNK])
def test_reading_past_the_cache_reads_it_all(tmp_path, size):
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    path = tmp_path / "file"
    path.write_bytes(data)
    assert b"".join(hc.past_the_cache(str(path))) == data
    assert b"".join(hc.past_the_cache(hc.long_path(path))) == data


def test_reading_past_the_cache_goes_on_from_where_it_was(tmp_path):
    data = (bytes(range(256)) * (2 * hc.CHUNK // 256 + 1))[:2 * hc.CHUNK + 5]
    path = tmp_path / "file"
    path.write_bytes(data)
    assert b"".join(hc.past_the_cache(str(path), hc.CHUNK)) == data[hc.CHUNK:]
    assert b"".join(hc.past_the_cache(str(path), hc.CHUNK + 7)) == data[hc.CHUNK + 7:]


def test_a_read_back_cut_by_a_drop_goes_on_and_counts_right(tmp_path, target, monkeypatch):
    source = big(tmp_path)
    real = hc.past_the_cache
    failed = {"n": 0}

    def flaky(path, at=0):
        for i, chunk in enumerate(real(path, at)):
            if i == 1 and not failed["n"]:
                failed["n"] = 1
                raise OSError(errno.ECONNRESET, "reset")
            yield chunk

    monkeypatch.setattr(hc, "past_the_cache", flaky)
    _, r = copy(source, target)
    assert r.done and failed["n"] == 1 and (target / "disk.iso").read_bytes() == (source / "disk.iso").read_bytes()


# ---- a check any time later --------------------------------------------------------------------------------------------

def verifying(target, **kw) -> hc.Verifying:
    lines = []
    v = hc.Verifying(target, say=lambda line="", **k: lines.append(line), sleep=lambda s: None, **kw)
    v.lines = lines
    return v


def test_a_check_finds_a_copy_well(source, target):
    copy(source, target)
    v = verifying(target)
    v.check()
    assert v.run() == 0 and "read back the same" in v.lines[-1]
    assert "Problems: 0" in (target / hc.FOLDER / "checked.txt").read_text(encoding="utf-8")


def test_a_check_finds_what_has_changed(source, target):
    copy(source, target)
    data = bytearray((target / "a.epub").read_bytes())
    data[3] ^= 1
    (target / "a.epub").write_bytes(data)  # the same size: only reading it tells
    (target / "sub" / "b.epub").unlink()
    (target / "z.cbz").write_bytes(b"shorter")
    v = verifying(target)
    v.check()
    assert v.run() == 3
    assert v.problems == {"a.epub": "its contents have changed", "sub/b.epub": "it's missing",
                          "z.cbz": "its size has changed (7 bytes, 10 when copied)"}
    assert "sub/b.epub" in (target / hc.FOLDER / "checked.txt").read_text(encoding="utf-8")


def test_a_check_waits_for_a_share_that_goes_away(source, target):
    copy(source, target)
    v = verifying(target)
    v.check()
    Faults(v.target, nth("stat", 2, OSError(errno.ECONNRESET, "reset")), away=2)
    assert v.run() == 0


def test_a_check_needs_something_to_go_by(tmp_path):
    v = verifying(tmp_path)
    with pytest.raises(Exit) as e:
        v.check()
    assert e.value.code == exits.NOT_SET_UP


def test_a_check_changes_nothing_but_its_own_note(source, target):
    copy(source, target)
    before = {k: v for k, v in as_it_is(target).items() if "checked.txt" not in k}
    v = verifying(target)
    v.check()
    v.run()
    assert {k: v for k, v in as_it_is(target).items() if "checked.txt" not in k} == before


# ---- each rule in one place: nothing there, the same source, most of it lost, the copy's own files ------------------

def asked(side: hc.Side) -> list[bool]:
    """Each answer the side gives from now on, in turn: who asks it, and when."""
    said, real = [], side.answers
    side.answers = lambda: said.append(real()) or said[-1]
    return said


def test_nothing_there_is_believed_where_it_is_asked_for_and_only_when_the_disk_answers(source, target):
    """_keep_trying's missing: nothing at a path is believed when the disk answers, asked once, with no wait: a look
    (_look), a part's removal (_drop) and the check (Verifying) go by it. Without missing, nothing there is trouble like
    any other, the file's own while the disk answers."""
    c, _ = copy(source, target)
    c.slept.clear()
    gone = c.target.path("gone.epub")
    said = asked(c.target)
    assert c._keep_trying(c.target, os.stat, gone, missing="none here") == "none here" and said == [True]
    assert c._look(c.target, gone) is None and said == [True, True]
    c._drop(c.target.path(f"{hc.FOLDER}/parts/never-made.part"))  # gone already: nothing to wait for
    assert said == [True, True, True] and not c.slept
    with pytest.raises(hc.FileProblem):
        c._keep_trying(c.target, os.stat, gone)
    v = verifying(target)
    v.check()
    checked = asked(v.target)
    assert v._keep_trying(v.target, os.stat, v.target.path("gone.epub"), missing="none here") == "none here"
    v._check_one("gone.epub", {"h": "0" * 64})
    assert v.problems == {"gone.epub": "it's missing"} and checked == [True, True]


@pytest.mark.parametrize("how", ["look", "drop", "check"])
def test_nothing_there_on_a_disk_away_is_held_for_at_once_by_a_look_and_the_check_and_waited_out_by_a_drop(
        source, target, how):
    """While the disk doesn't answer, "no such file" isn't believed. A look, and the check (Verifying) by one, hold for
    the disk at once; a part's removal waits it out as any trouble, which asks the disk again first: one wait fewer."""
    c, _ = copy(source, target)
    c.slept.clear()
    v = verifying(target)
    v.check()
    v.slept = []
    v.sleep = v.slept.append
    who, side = (v, v.target) if how == "check" else (c, c.target)
    call = {"look": "stat", "drop": "unlink", "check": "stat"}[how]
    Faults(side, nth(call, 1, FileNotFoundError(errno.ENOENT, "No such file or directory")), away=2)
    said = asked(side)
    if how == "look":
        assert c._look(c.target, c.target.path("a.epub")) is not None  # it's there: the "no such file" wasn't
    elif how == "drop":
        c._drop(c.target.path(f"{hc.FOLDER}/parts/never-made.part"))
    else:
        v._check_one("a.epub", c.record.entries["a.epub"])
        assert not v.problems
    assert said[:3] == [False, False, True]
    assert who.slept == ([hc.WAITS[0]] if how == "drop" else [hc.WAITS[0], hc.WAITS[1]])


def test_a_check_waits_for_a_disk_that_comes_back_between_two_asks_and_finds_the_copy_there(source, target):
    """The check looks as a copy does (_look): "no such file" from a disk that doesn't answer when asked is held for,
    and the copy looked at again once the disk is back, not taken for the copy's own trouble at the next ask."""
    copy(source, target)
    v = verifying(target)
    v.check()
    Faults(v.target, nth("stat", 1, FileNotFoundError(errno.ENOENT, "No such file or directory")), away=1)
    said = asked(v.target)
    assert v.run() == 0 and not v.problems and said[:2] == [False, True]
    assert any("isn't answering" in line for line in v.lines) and any("is back" in line for line in v.lines)


@pytest.mark.parametrize("left", range(9))
def test_a_source_answers_and_is_the_one_copied_from_before_by_one_rule(tmp_path, target, left):
    """The same folder by its numbers, else by most of the names seen at its top: as a source answers during a copy
    (Side.answers) and as the source a target was copied from before (_another_source), one rule for both."""
    source = tree(tmp_path / "from", {f"f{i}.txt": "x" for i in range(8)})
    c, _ = copy(source, target)
    c.source.numbers = (1, 1)  # a disk mounted again may get new ones, and an unmount's empty folder has its own
    for i in range(left, 8):
        (source / f"f{i}.txt").unlink()
    assert c.source.answers() is (not c._another_source()) is (2 * left > 8)


@pytest.mark.parametrize("before,now,lost", [(4, 1, True), (4, 2, False), (3, 0, False), (10, 4, True), (10, 5, False),
                                             (0, 0, False)])
def test_most_of_a_source_lost_is_one_rule(before, now, lost):
    """At least SHRUNK_FROM files before, under half of them now: for a source that isn't the one copied from before,
    a copy not done as its source shrank, and a backup whose library looks short (test_backup.py)."""
    assert hc.lost_most(before, now) is lost


def test_a_copy_s_own_files_are_kept_at_while_another_program_holds_the_record_and_the_report_given_up(source, target,
                                                                                                        monkeypatch):
    """_keep_at_it: the record waited for, as the copy can't do without it, with what holds it said (why_held); the
    report, which it can do without, said and left. Refused until a hold is said: Windows first takes a refusal for an
    antivirus scan's, tried again twice after a moment (_strike)."""
    c = job(source, target)
    c.check()
    c.prepare()
    held = []
    real_sleep = c.sleep

    def sleep(seconds) -> None:
        held.append(c.why_held)
        real_sleep(seconds)

    c.sleep = sleep
    refused = PermissionError(errno.EACCES, "Permission denied")
    Faults(c.target, lambda name, a: refused if name == "the record" and not any(held) or name == "_write_here"
           and str(a[0]).endswith("report.txt") else None)
    r = c.run()
    assert r.done and copied_says(target) == set(contents(source))
    assert next(why for why in held if why) == "the record on the target can't be written (Permission denied)"
    assert hc.WAITS[3] in c.slept
    assert r.report == "" and any("report.txt couldn't be written: Permission denied" in line for line in c.lines)
    assert (target / hc.FOLDER / "manifest.sha256").is_file()


# ---- many small files ---------------------------------------------------------------------------------------------------

def test_many_small_files_write_the_record_in_batches(tmp_path, target, monkeypatch):
    """The record written a batch at a time (RECORD_FILES): three batches' worth of files, few enough, as each one is
    made safe on the disk, which takes its time."""
    source = tree(tmp_path / "from", {f"folder{i % 20}/file{i}.txt": f"file {i}" for i in range(120)})
    monkeypatch.setattr(hc, "RECORD_FILES", 40)
    writes = {"n": 0}
    real = hc.Record.write

    def counted(self):
        writes["n"] += 1
        real(self)

    monkeypatch.setattr(hc.Record, "write", counted)
    monkeypatch.setattr(hc, "RECORD_SECONDS", 3600)
    c, r = copy(source, target)
    assert r.done and r.copied == 120 and writes["n"] <= 5
    assert len(contents(target)) == 120
    assert len(copied_says(target)) == 120


def test_a_second_run_over_many_files_asks_about_folders_not_files(tmp_path, target):
    source = tree(tmp_path / "from", {f"folder{i % 5}/file{i}.txt": "x" for i in range(60)})
    copy(source, target)
    c = job(source, target)
    c.check()
    c.prepare()
    faults = Faults(c.target, lambda name, a: None)
    r = c.run()
    assert r.done and r.earlier == 60
    assert faults.calls.count("_entries") == 6 and "stat" not in faults.calls


# ---- a process that really dies -----------------------------------------------------------------------------------------

def test_a_killed_copy_leaves_no_half_file_and_the_next_run_finishes_it(tmp_path, target):
    """The copy in a process of its own, its chunks made small, killed with a part past 1 MB written. Past that, it waits
    for as long as the test's file (hold) is there, so that a fast disk or a test held up for a while doesn't see it
    finish first: the file is removed only once it's killed."""
    source = tree(tmp_path / "from", {"big.iso": os.urandom(2 << 20), **{f"small/{i}.txt": f"small {i}" for i in range(60)}, "z.iso": os.urandom(512 << 10)})
    hold = tmp_path / "hold"
    hold.write_text("the copy goes no further while this is here", encoding="utf-8")
    code = ("import os, sys, time, hylly.hyllycopy as hc, hylly.cli as c\n"
            "hc.ON_DISK_EVERY = hc.CHUNK = 1 << 16\n"
            "hc.FSTAB, hold, write = sys.argv[3], sys.argv[4], hc._write_all\n"  # conftest's made-up fstab
            "def held(fh, data):\n"
            "    write(fh, data)\n"
            "    while fh.tell() > 1 << 20 and os.path.exists(hold):\n"
            "        time.sleep(0.01)\n"
            "hc._write_all = held\n"
            "c.main(['copy', sys.argv[1], sys.argv[2], '--yes'])\n")
    process = subprocess.Popen([sys.executable, "-c", code, str(source), str(target), hc.FSTAB, str(hold)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parts = target / hc.FOLDER / "parts"
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if any(p.stat().st_size > 1 << 20 for p in parts.iterdir()):
                    break
            except OSError:
                pass
            time.sleep(0.01)
        assert process.poll() is None, "it ended before it could be killed"
    finally:
        process.kill()
        process.wait()
        hold.unlink()
    assert not (target / "big.iso").exists()
    for rel, data in contents(target).items():  # whatever has its name is whole
        assert data == (source / rel).read_bytes()
    c, r = copy(source, target)
    assert r.done and contents(target) == contents(source)
    assert any("going on from" in line for line in c.lines)
    v = verifying(target)
    v.check()
    assert v.run() == 0


# ---- the notes ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def notes(monkeypatch) -> list[tuple[str, str, str]]:
    said = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: said.append((title, text, public)) or True)
    return said


def clocked(source, target, **kw) -> tuple[hc.Copying, list[float]]:
    clock = [1000.0]
    c = job(source, target, now=lambda: clock[0], **kw)
    return c, clock


def test_nothing_moving_is_told_once_and_again_after_a_day_and_the_going_on_too(source, target, notes):
    c, clock = clocked(source, target)
    c._tick()
    assert not notes
    clock[0] += hc.TELL_AFTER - 1
    c._tick()
    assert not notes  # the router's stalls and restarts are over long before this
    clock[0] += 2
    c.why_held = "the target isn't answering"
    c._tick()
    assert [n[0] for n in notes] == ["HyllyCopy: on hold"] and "the target isn't answering" in notes[0][1]
    clock[0] += 3600
    c._tick()
    assert len(notes) == 1
    clock[0] += hc.TELL_AGAIN
    c._tick()
    assert len(notes) == 2 and "hylly jobs stop copy" in notes[1][1]
    c._touch()
    clock[0] += 1
    c._tick()
    assert [n[0] for n in notes][-1] == "HyllyCopy: going on"


def test_a_read_that_hangs_is_noticed_without_any_error(source, target, notes):
    c, clock = clocked(source, target)
    clock[0] += hc.TELL_AFTER + 1
    c._tick()
    assert "hanging" in notes[0][1]


def test_a_long_copy_says_it_started_with_an_estimate_and_how_far_when_asked(source, target, notes, monkeypatch):
    c, clock = clocked(source, target, every=3600)
    monkeypatch.setattr(runs, "progress", lambda line: None)
    c.total_files, c.total_bytes, c.done_files, c.done_bytes = 1000, 1_000_000, 100, 100_000
    clock[0] += hc.TELL_STARTED + 1
    c._touch()
    c._tick()
    assert [n[0] for n in notes] == ["HyllyCopy: started"] and "1,000 files" in notes[0][1] and "About" in notes[0][1]
    clock[0] += 3600
    c._touch()
    c._tick()
    assert [n[0] for n in notes][1:] == ["HyllyCopy: how far"] and "of 1,000 files" in notes[1][1]


def test_a_second_hold_within_the_hour_has_words_of_its_own(source, target, notes, monkeypatch):
    """HyllyBell holds back the same words within the hour, and the phone's last word would be "going on"."""
    c, clock = clocked(source, target)
    times = iter(f"10:{m:02d}" for m in range(10, 60))
    monkeypatch.setattr(hc.time, "strftime", lambda *a: next(times))
    clock[0] += hc.TELL_AFTER + 1
    c._tick()
    c._touch()
    clock[0] += 1
    c._tick()
    clock[0] += hc.TELL_AFTER + 1
    c._tick()
    said = [n[:2] for n in notes if "on hold" in n[0]]
    assert len(said) == 2 and said[0] != said[1]


def test_a_short_copy_sends_no_notes(source, target, notes):
    c, r = copy(source, target)
    assert "All 4 files" in c.tell_of_the_end(r) and not notes


def test_a_long_copy_says_when_it_is_done(source, target, notes):
    c, r = copy(source, target)
    c.started_told = True
    words = c.tell_of_the_end(r)
    assert notes == [("HyllyCopy: done", words, words)] and "All 4 files" in words


def test_a_failure_is_told_however_short_and_the_phone_gets_no_names(tmp_path, source, target, notes):
    tree(target, {"a.epub": b"mine"})
    c, r = copy(source, target)
    words = c.tell_of_the_end(r)
    assert not r.done and "aren't" in words and "Don't delete" in words
    title, text, public = notes[0]
    for said in (title, text, public):
        assert "a.epub" not in said and str(tmp_path) not in said


def test_a_note_says_how_many_were_left_out(tmp_path, target, notes):
    source = tree(tmp_path / "from", {"a.txt": "a", ".DS_Store": "x"})
    c, r = copy(source, target)
    assert "Left out: 1" in c.tell_of_the_end(r)


TOPIC = "https://my.example/alarms"


def with_a_phone(tmp_path, monkeypatch, answer):
    from hylly import settings

    path = tmp_path / "with-phone.toml"
    path.write_text('[bell]\ntoast = false\n', encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(path))
    monkeypatch.setenv("HYLLY_NTFY_URL", TOPIC)  # the phone's address: a secret, apart from the settings
    settings.reload()  # these settings, as the copy's command reads them
    posted = []

    def post(url, json=None, **k):
        posted.append(json)
        return answer()

    monkeypatch.setattr(httpx, "post", post)
    return posted


def test_a_note_says_whether_it_reached_the_phone(tmp_path, monkeypatch):
    assert bell.job_note("Done", "No phone set up.") is True
    with_a_phone(tmp_path, monkeypatch, lambda: httpx.Response(200))
    assert bell.job_note("Done", "It went.") is True

    def down():
        raise httpx.ConnectError("no route")

    with_a_phone(tmp_path, monkeypatch, down)
    assert bell.job_note("Done", "It didn't.") is False


def test_a_note_that_couldn_t_reach_the_phone_goes_again_in_its_order(tmp_path, monkeypatch):
    up = {"now": False}

    def answer():
        if not up["now"]:
            raise httpx.ConnectError("the router is restarting")
        return httpx.Response(200)

    posted = with_a_phone(tmp_path, monkeypatch, answer)
    n = hc.Notes()
    n.tell("HyllyCopy: on hold", "Nothing has moved.")
    n.tell("HyllyCopy: going on", "It's moving again.")
    assert len(n.waiting) == 2
    n.again()
    assert len(n.waiting) == 2  # still down
    up["now"] = True
    posted.clear()
    n.again()
    assert not n.waiting and [p["title"] for p in posted] == ["HyllyCopy: on hold", "HyllyCopy: going on"]


def test_a_phone_service_down_for_a_moment_counts_as_not_reached(tmp_path, monkeypatch):
    for status, reached in ((502, False), (429, False), (503, False), (403, True), (200, True)):
        with_a_phone(tmp_path, monkeypatch, lambda s=status: httpx.Response(s))
        assert bell.job_note("Done", f"Answered {status}.") is reached, status


def test_a_note_still_on_its_way_when_the_waiting_ends_counts_as_not_reached(tmp_path, monkeypatch):
    def slow():
        time.sleep(0.4)
        return httpx.Response(200)

    with_a_phone(tmp_path, monkeypatch, slow)
    monkeypatch.setattr(bell, "NOTE_WAIT", 0.02)
    with note_threads_waited_for():
        assert bell.job_note("Done", "Slow phone service.") is False


def test_an_answer_lost_on_its_way_back_isn_t_a_reason_to_send_a_note_again(tmp_path, monkeypatch):
    """The note may well have gone, and going again could send it any number of times."""
    def lost():
        raise httpx.ReadTimeout("no answer")

    with_a_phone(tmp_path, monkeypatch, lost)
    assert bell.job_note("Done", "Perhaps it went.") is True
    assert bell.phone_again("Done", "Perhaps it went.", "Perhaps it went.") is True


def test_two_senders_of_the_waiting_notes_at_once_send_each_one_once_in_its_order(monkeypatch):
    """The watchdog's tick, a moment after its job has ended, and the end's send_the_rest each sent the first note
    waiting, and only then took it away: a slow phone service, and both sent it, one took away the next, never sent,
    or found none left (IndexError) after a copy that went well. One sender at a time now: the second waits its turn."""
    sent, start = [], threading.Barrier(2)

    def slow(title, text, public):
        sent.append(title)
        time.sleep(0.2)  # each send as slow as a phone service far away
        return True

    monkeypatch.setattr(bell, "phone_again", slow)
    n = hc.Notes()
    n.waiting += [("HyllyCopy: on hold", "x", "x"), ("HyllyCopy: going on", "y", "y"), ("HyllyCopy: done", "z", "z")]
    failed = []

    def sender():
        start.wait()
        try:
            n.again()
        except Exception as e:  # noqa: BLE001 - said below
            failed.append(e)

    both = [threading.Thread(target=sender) for _ in range(2)]
    for t in both:
        t.start()
    for t in both:
        t.join(10)
    assert not failed and not n.waiting
    assert sent == ["HyllyCopy: on hold", "HyllyCopy: going on", "HyllyCopy: done"]


def test_notes_waiting_are_tried_every_couple_of_minutes_not_every_look(source, target, monkeypatch):
    tries = []
    monkeypatch.setattr(bell, "phone_again", lambda *a: tries.append(a) or False)
    c, clock = clocked(source, target)
    c.notes.waiting.append(("t", "x", "x"))
    for _ in range(8):
        clock[0] += hc.TICK
        c._touch()
        c._tick()
    assert len(tries) == 1


def test_a_note_said_just_now_counts_as_gone(tmp_path, monkeypatch):
    posted = with_a_phone(tmp_path, monkeypatch, lambda: httpx.Response(200))
    assert bell.job_note("Done", "The same words.") is True
    assert bell.job_note("Done", "The same words.") is True
    assert len(posted) == 1


def test_no_room_gets_one_note_and_it_goes_again_if_it_has_to(tmp_path, source, target, monkeypatch, capsys):
    told, again = [], []
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 0))
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: told.append((title, text, public)) or False)
    monkeypatch.setattr(bell, "phone_again", lambda *a: again.append(a) or True)
    real_tell = hc.Copying.send_the_rest
    monkeypatch.setattr(hc.Copying, "send_the_rest", lambda self: (setattr(self, "sleep", lambda s: None), real_tell(self))[1])
    assert code_of(source, target, "--yes") == exits.LOCAL
    assert [t[0] for t in told] == ["HyllyCopy: stopped"] and len(again) == 1
    assert str(tmp_path) not in told[0][2] and "Not enough room" in capsys.readouterr().out


def test_the_end_waits_a_while_for_the_phone_but_not_for_ever(source, target, monkeypatch):
    clock = [0.0]

    def asleep(seconds):
        clock[0] += seconds

    c = hc.Copying(source, target, say=lambda *a, **k: None, sleep=asleep, now=lambda: clock[0])
    c.notes.waiting.append(("t", "x", "x"))
    monkeypatch.setattr(bell, "phone_again", lambda *a: False)
    c.send_the_rest()
    assert hc.RESEND_FOR <= clock[0] <= hc.RESEND_FOR + 120
    tries = []
    monkeypatch.setattr(bell, "phone_again", lambda *a: tries.append(a) or True)
    c.send_the_rest()
    assert len(tries) == 1 and not c.notes.waiting


# ---- the command --------------------------------------------------------------------------------------------------------

def code_of(*argv) -> int:
    with pytest.raises(SystemExit) as e:
        cli.main(["copy", *map(str, argv)])
    return e.value.code


def test_the_command_shows_a_plan_then_copies_then_checks(source, target, capsys):
    cli.main(["copy", str(source), str(target), "--dry-run"])
    assert "To copy: 4 files" in capsys.readouterr().out and not target.exists()
    cli.main(["copy", str(source), str(target), "--yes"])
    out = capsys.readouterr().out
    assert "All 4 files" in out and "report.txt" in out and contents(target) == contents(source)
    cli.main(["copy", str(source), str(target), "-n"])
    out = capsys.readouterr().out
    assert "Copied and checked already: 4 files" in out and "To copy: 0 files" in out
    cli.main(["copy", "check", str(target), "-n"])
    assert "It would read 4 files" in capsys.readouterr().out and not (target / hc.FOLDER / "checked.txt").exists()
    cli.main(["copy", "check", str(target)])
    assert "read back the same" in capsys.readouterr().out and (target / hc.FOLDER / "checked.txt").is_file()
    last = runs.last_runs()
    assert last and last[0]["job"] == "copy" and last[0]["exit"] == 0


def test_the_command_ends_as_it_should(source, target, capsys):
    assert code_of() == exits.USAGE
    assert code_of(source) == exits.USAGE
    assert code_of("check", target, source) == exits.USAGE
    assert code_of(source, source, "--yes") == exits.USAGE
    assert code_of("check", target) == exits.NOT_SET_UP
    assert code_of(source, target, "--progress", "5m") == exits.USAGE
    tree(target, {"a.epub": b"mine"})
    assert code_of(source, target, "--yes") == exits.PARTLY
    assert "aren't" in capsys.readouterr().out
    (target / "z.cbz").unlink()
    (target / "a.epub").unlink()
    cli.main(["copy", str(source), str(target)])
    (target / "z.cbz").write_bytes(b"not it, now")
    assert code_of("check", target) == exits.PARTLY


def test_one_at_a_time_on_this_computer(source, target, capsys):
    with runs.only_one("copy"):
        assert code_of(source, target, "--yes") == exits.BUSY
    assert "already running" in capsys.readouterr().err
    cli.main(["jobs", "stop", "copy"])
    assert "No HyllyCopy is running" in capsys.readouterr().out


def test_one_at_a_time_on_a_target_shared_with_another_computer(source, target, capsys):
    locks = target / hc.FOLDER / "locks"
    locks.mkdir(parents=True)
    (locks / "copy.json").write_text(json.dumps({"host": "the-other-pc", "computer": "another", "pid": 1, "started": "2026-01-01 10:00", "command": "hylly copy"}))
    from hylly import instance

    with open(locks / "copy.lock", "a+b") as fh:
        assert instance._lock(fh)
        assert code_of(source, target, "--yes") == exits.BUSY
        instance._unlock(fh)
    said = capsys.readouterr().err
    assert "the-other-pc" in said and "the same target" in said


def test_every_command_list_knows_it(capsys):
    cli.main(["commands", "--json"])
    data = json.loads(capsys.readouterr().out)
    copying = next(c for c in data["commands"] if c["command"] == "hylly copy")
    assert copying["job"] == "copy" and not copying["online"] and not copying["account"]
    assert {"-n, --dry-run", "-y, --yes", "--progress", "--everything", "--new-disk"} <= {o["option"] for o in copying["options"]}
    assert copying["person"] and copying["dry_run"] == "offline" and copying["runs"] == "here"
    checking = next(c for c in data["commands"] if c["command"] == "hylly copy check")
    assert {o["option"] for o in checking["options"]} == {"target", "--progress", "-n, --dry-run", "--json"} and checking["job"] == "copy"
    assert 'id="cmd-copy"' in commands.reference_html() and 'id="cmd-copy-check"' in commands.reference_html()


def test_a_first_copy_asks_first_and_with_nobody_there_makes_nothing(source, target, capsys, monkeypatch):
    """hylly copy acts at once, so its first run to a target asks: a mistyped target would get all of it. With nobody at
    the keyboard it ends with exit code 8 and nothing made there; a yes, or --yes, goes ahead, and the next run doesn't
    ask. A no makes nothing either."""
    assert code_of(source, target) == exits.NEEDS_YOU
    assert "Nothing has been copied from" in capsys.readouterr().err and not target.exists()
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr("builtins.input", lambda question: "n")
    assert code_of(source, target) == exits.NEEDS_YOU and not target.exists()
    monkeypatch.setattr("builtins.input", lambda question: "y")
    cli.main(["copy", str(source), str(target)])
    assert contents(target) == contents(source)
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail("not asked again"))
    cli.main(["copy", str(source), str(target)])


def test_a_copy_that_would_write_more_than_all_before_asks_and_the_last_report_stands(tmp_path, source, target, capsys, monkeypatch):
    cli.main(["copy", str(source), str(target), "--yes"])
    report = (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(hc, "FAR_MORE", 0)
    (source / "much more.iso").write_bytes(b"x" * (2 * sum(len(v) for v in contents(source).values())))
    assert code_of(source, target) == exits.NEEDS_YOU
    assert "more than all" in capsys.readouterr().err and not (target / "much more.iso").exists()
    assert (target / hc.FOLDER / "report.txt").read_text(encoding="utf-8") == report  # nothing begun: its DONE still holds
    cli.main(["copy", str(source), str(target), "--yes"])
    assert (target / "much more.iso").is_file()


def test_files_that_keep_failing_end_as_some_failed(tmp_path, target, monkeypatch, capsys):
    source = tree(tmp_path / "from", {f"z{i}.txt": "z" for i in range(6)})
    real = hc.Copying.run

    def run(self):
        Faults(self.source, lambda name, a: eio() if name == "open" else None)
        self.sleep = lambda seconds: None
        return real(self)

    monkeypatch.setattr(hc.Copying, "run", run)
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: True)
    assert code_of(source, target, "--yes") == exits.PARTLY
    assert "Don't delete" in capsys.readouterr().out
    assert runs.last_runs()[0]["exit"] == exits.PARTLY


@pytest.mark.parametrize("text,seconds", [("6h", 21600), ("30m", 1800), (" 1H ", 3600)])
def test_how_often_is_said_in_hours_or_minutes(text, seconds):
    assert runs.seconds_of(text) == seconds


@pytest.mark.parametrize("text", ["5m", "6", "0h", "a day", "-1h"])
def test_how_often_can_t_be_nonsense(text):
    with pytest.raises(ValueError):
        runs.seconds_of(text)


# ---- what a real Samba showed --------------------------------------------------------------------------------

def test_a_share_that_can_t_be_asked_at_the_start_isn_t_a_crash(tmp_path, monkeypatch):
    real = os.path.realpath

    def picky(path, *a, **k):
        if "away" in str(path):
            raise OSError(64, "The specified network name is no longer available")
        return real(path, *a, **k)

    monkeypatch.setattr(hc.os.path, "realpath", picky)
    assert hc.overlap(str(tmp_path / "here"), str(tmp_path / "away")) is None
    assert "same folder" in hc.overlap(str(tmp_path / "away"), str(tmp_path / "away"))


def test_a_file_that_will_be_tried_again_isn_t_called_failed_yet(source, target, monkeypatch):
    goes = [1]
    real = hc.Copying._copy_file

    def once(self, rel, listed, result):
        if goes[0]:
            goes[0] -= 1
            raise hc.FileProblem("the share dropped at that moment")
        return real(self, rel, listed, result)

    monkeypatch.setattr(hc.Copying, "_copy_file", once)
    c, r = copy(source, target)
    said = "\n".join(c.lines)
    assert r.done, said
    assert "Will try again later:" in said and "FAILED" not in said


def test_a_file_that_won_t_be_tried_again_is_called_failed(source, target, monkeypatch):
    real = hc.Copying._copy_file

    def never(self, rel, listed, result):
        if rel == "a.epub":
            raise hc.FileProblem("it can't be read at all", again=False)
        return real(self, rel, listed, result)

    monkeypatch.setattr(hc.Copying, "_copy_file", never)
    c, r = copy(source, target)
    assert not r.done and any("  FAILED: a.epub" in line for line in c.lines), c.lines


def test_a_new_source_to_a_target_copied_to_before_asks_and_writes_nothing(tmp_path, source, target, capsys):
    """A target this computer knows, with nothing on it from this source: a mistyped source would add all of it."""
    cli.main(["copy", str(source), str(target), "--yes"])
    record = (target / hc.FOLDER / "record.jsonl").read_bytes()
    other = tree(tmp_path / "other", {"b.txt": "b", "c.txt": "c"})
    assert code_of(other, target) == exits.NEEDS_YOU
    assert "Nothing has been copied from" in capsys.readouterr().err
    assert not (target / "b.txt").exists() and not (target / "c.txt").exists()
    assert (target / hc.FOLDER / "record.jsonl").read_bytes() == record  # not even the source's note
    cli.main(["copy", str(other), str(target), "--yes"])
    assert (target / "b.txt").is_file()
    capsys.readouterr()
    cli.main(["copy", str(other), str(target)])  # and once it has, the next run doesn't ask
    assert "asks first" not in capsys.readouterr().err


def test_a_run_stopped_by_its_question_sends_a_note_and_is_told_by_hylly_jobs(source, target, monkeypatch):
    """As catalog's question does: a scheduled copy stopped by it doesn't stop in silence."""
    notes = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: notes.append((title, text, public)) or True)
    assert code_of(source, target) == exits.NEEDS_YOU
    assert [n[0] for n in notes] == ["A HyllyCopy stopped"] and "--yes" in notes[0][2] and str(target) not in notes[0][2]
    last = runs.last_runs()
    assert last[0]["job"] == "copy" and last[0]["exit"] == exits.NEEDS_YOU and "nobody is here to answer" in last[0]["said"]
