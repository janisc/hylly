"""A write cut short costs as little as possible: each way found to lose data, reproduced one by one, for the records,
whole files, snapshots and logs (storage.py and snapshot.py). Those for a run's note, a download, a copy, hylly add
and a title's folder are with the tests of their own modules (test_runs.py, test_download.py, test_copies.py,
test_plan.py)."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest

from hylly import access, catalog, cli, curation, itchio, lookups, manifest, paths, plan, serve, shopping, snapshot, storage
from hylly import download as dl
from hylly.library import Catalog

from helpers import make_job


CUT_INSIDE_A_LETTER = '{"path": "ä"}\n{"path": "cut "'.encode()[:-1] + "ä".encode()[:1]


# ---- records -----------------------------------------------------------------------------------

def test_a_letter_cut_in_two_costs_only_its_own_line(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(CUT_INSIDE_A_LETTER)
    assert list(storage.read_records(path)) == [{"path": "ä"}]
    assert storage.read_lines(tmp_path / "not there") == []


def test_a_line_cut_short_doesnt_swallow_the_next(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(CUT_INSIDE_A_LETTER)
    storage.append_line(path, '{"path": "next"}')
    storage.append_line(path, '{"path": "last"}', durable=True)
    assert [r["path"] for r in storage.read_records(path)] == ["ä", "next", "last"]


def test_lines_from_before_with_windows_endings_still_read(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"a": 1}\r\n{"a": 2}\r\n')
    storage.append_line(path, '{"a": 3}')
    assert [r["a"] for r in storage.read_records(path)] == [1, 2, 3]


def test_every_record_file_survives_a_cut(tmp_path):
    """The record files that used to stop every command until the file was edited by hand, and are files still: the caches."""
    (tmp_path / "cache").mkdir()
    entry = {"path": "a/b.epub", "key": "k", "status": "found", "game": 1}
    for path in (tmp_path / "cache" / "lookups.jsonl", tmp_path / "cache" / "lines.jsonl", tmp_path / "cache" / "files.jsonl"):
        path.write_bytes(json.dumps(entry, ensure_ascii=False).encode() + b"\n" + CUT_INSIDE_A_LETTER)
    assert lookups.Cache(tmp_path / "cache" / "lookups.jsonl").get("k")["status"] == "found"
    assert 1 in itchio.Lines(tmp_path / "cache" / "lines.jsonl", "game").rows
    assert "a/b.epub" in catalog.FileCache(tmp_path / "cache" / "files.jsonl").data


def test_a_cache_keeps_the_last_line_of_each_key_and_passes_over_a_line_without_one(tmp_path):
    path = tmp_path / "cache.jsonl"
    path.write_bytes(b'{"id": 1, "n": "first"}\n{"n": "no key"}\n{"id": [1], "n": "a list"}\n["not a record"]\n'
                     b'{"id": 2, "n": "two"}\n{"id": 1, "n": "again"}\n' + CUT_INSIDE_A_LETTER)
    taken = []
    records = storage.Records(path, "id", each=taken.append)
    assert records.get(1)["n"] == "again" and records.get(2)["n"] == "two" and len(records.rows) == 2
    assert [r["n"] for r in taken] == ["first", "two", "again"]  # each record taken, in order, for an index of one's own
    records.put({"id": 3, "n": "three"})
    assert taken[-1]["n"] == "three" and storage.Records(path, "id").get(3) == {"id": 3, "n": "three"}
    with pytest.raises(KeyError):
        records.put({"n": "no key"})  # a slip of the caller's: nothing is written
    assert storage.Records(path, "id").lines == records.lines
    pairs = storage.Records(path, ("id", "n"))  # a key of several fields: a tuple of their values
    assert {(1, "first"), (1, "again"), (2, "two"), (3, "three")} == set(pairs.rows)


def test_each_cache_keeps_its_own_rule_for_a_write_that_fails(tmp_path, monkeypatch):
    """The catalog's caches let a write that fails go, as the file is simply read again next time; a lookup source's
    answers and itch.io's raise, which ends the run. A dry run writes nothing, so nothing fails."""
    def refused(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "append_line", refused)
    files = catalog.FileCache(tmp_path / "files.jsonl")
    files.put({"path": "a.epub", "size": 1, "mtime": 0, "meta": {}})
    assert files.get("a.epub", 1, 0) and not (tmp_path / "files.jsonl").exists()
    answers = lookups.Cache(tmp_path / "lookup-openlibrary.jsonl")
    with pytest.raises(OSError):
        answers.put("k", ["h:lantern"], "found", {"title": "Lantern Wolf"})
    assert answers.get("k") is None and "h:lantern" not in answers.by_id
    with pytest.raises(OSError):
        itchio.Lines(tmp_path / "itchio-games.jsonl", "id").put({"id": 1, "game": {}})
    dry = itchio.Lines(tmp_path / "itchio-uploads.jsonl", "game", keep=False)
    dry.put({"game": 1, "visible": True, "uploads": []})
    assert dry.get(1)["visible"] and not (tmp_path / "itchio-uploads.jsonl").exists()


def test_a_cache_is_written_anew_once_most_of_it_is_older_lines_and_a_tidy_that_fails_is_let_go(tmp_path, monkeypatch):
    path = tmp_path / "cache.jsonl"
    records = storage.TidiedRecords(path, "key")  # one that raises when a put fails: a tidy that fails is let go all the same
    for n in (1, 2):
        for key in "ab":
            records.put({"key": key, "n": n})

    def refused(*a, **k):
        raise OSError(22, "Invalid argument")

    with monkeypatch.context() as m:
        m.setattr(storage, "atomic_write", refused)
        records.tidy()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4  # as it was, and read the same
    records.tidy()
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [{"key": "a", "n": 2}, {"key": "b", "n": 2}]
    records.put({"key": "a", "n": 3})
    records.tidy()  # one older line of three: it stays as it is
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3 and storage.Records(path, "key").rows == records.rows


def test_each_cache_reads_its_lines_as_written_before_one_class_kept_them_all(tmp_path):
    """The caches on one class (storage.Records), each file in its own place, with lines as each wrote them until now:
    a key's older line, its later one, and another key. The later line wins. Only the catalog's are tidied, at the end
    of a build (storage.TidiedRecords). A lookup source's and itch.io's can't be: another run may append to them
    meanwhile, and a lookup source's id goes by the last line that names it, an older line of another key's too (a title
    edited since keeps its answer)."""
    epub = "humble/Moonwake Press/Lantern Wolf/Lantern Wolf.epub"
    lines = {
        "catalog/cache/files.jsonl": [
            {"path": epub, "size": 90, "mtime": 1763000000.0, "meta": {"title": "Lantern Wolf"}, "cover": False},
            {"path": "own/Kirja.pdf", "size": 7, "mtime": 1763100000.0, "meta": {"title": "Kirja ä"}, "cover": False},
            {"path": epub, "size": 100, "mtime": 1763100000.25, "meta": {"title": "Lantern Wolf", "authors": ["Writer, Ann"]}, "cover": False},
            {"path": epub, "size": 120, "mtime": 1763200000.5, "meta": {"title": "Lantern Wolf", "authors": ["Writer, Ann"]}, "cover": True}],
        "catalog/cache/details.jsonl": [
            {"path": epub, "size": 100, "mtime": 1763100000.25, "meta": {"series": "Lantern"}},
            {"path": epub, "size": 120, "mtime": 1763200000.5, "meta": {"series": "Lantern", "number": "2"}}],
        "state/lookups/lookup-openlibrary.jsonl": [
            {"key": "book|lantern wolf|writer", "ids": ["h:lantern"], "status": "none", "at": "2026-11-01T10:00:00"},
            {"key": "book|lantern wolf|writer", "ids": ["h:wolf"], "status": "found", "at": "2026-11-14T10:00:00",
             "found": {"title": "Lantern Wolf", "score": 0.97}}],
        "state/itchio/uploads.jsonl": [
            {"game": 7, "visible": True, "uploads": [{"id": 70, "filename": "lantern-wolf.pdf", "size": 1000, "md5": "",
                                                       "type": "book", "storage": "hosted", "traits": []}], "at": "2026-11-01T10:00:00"},
            {"game": 8, "visible": False, "uploads": [], "at": "2026-11-01T10:00:01"},
            {"game": 7, "visible": True, "uploads": [], "again": True, "at": "2026-11-14T10:00:00"},
            {"game": 8, "visible": True, "uploads": [], "at": "2026-11-14T10:00:01"}],
        "state/itchio/bundle-pages.jsonl": [
            {"bundle": "Moonwake Bundle", "page": 1, "last": 1, "items": [], "at": "2026-11-01T10:00:00"},
            {"bundle": "Moonwake Bundle", "page": 1, "last": 2, "items": [], "at": "2026-11-14T10:00:00"}],
    }
    for rel, rows in lines.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    last = {rel: rows[-1] for rel, rows in lines.items()}

    def kept(rel: str) -> list[dict]:
        return [json.loads(line) for line in (tmp_path / rel).read_text(encoding="utf-8").splitlines()]

    for rel in ("catalog/cache/files.jsonl", "catalog/cache/details.jsonl"):
        cache = catalog.FileCache(tmp_path / rel)
        assert cache.get(epub, 120, 1763200000.5) == last[rel] and cache.get(epub, 100, 1763100000.25) is None
        cache.tidy()  # most of each is older lines
        assert kept(rel) == list(cache.data.values()) and last[rel] in kept(rel)
        assert len(kept(rel)) == len({r["path"] for r in lines[rel]}) and catalog.FileCache(tmp_path / rel).data == cache.data
    assert '"title": "Kirja ä"' in (tmp_path / "catalog/cache/files.jsonl").read_text(encoding="utf-8")  # its letters as written

    answers = lookups.Cache(tmp_path / "state/lookups/lookup-openlibrary.jsonl")
    assert answers.get("book|lantern wolf|writer") == last["state/lookups/lookup-openlibrary.jsonl"]
    assert answers.by_id["h:wolf"]["status"] == "found" and answers.by_id["h:lantern"]["status"] == "none"

    uploads = itchio.Lines(tmp_path / "state/itchio/uploads.jsonl", "game")
    pages = itchio.Lines(tmp_path / "state/itchio/bundle-pages.jsonl", "bundle,page")
    assert uploads.rows[7]["again"] and set(uploads.rows) == {7, 8} and pages.rows[("Moonwake Bundle", 1)]["last"] == 2
    uploads.put({"game": 9, "visible": True, "uploads": []})
    assert kept("state/itchio/uploads.jsonl") == lines["state/itchio/uploads.jsonl"] + [uploads.rows[9]] and uploads.rows[9]["at"]
    assert not any(hasattr(cache, "tidy") for cache in (uploads, pages, answers, answers.records))


# ---- whole files -------------------------------------------------------------------------------

def test_a_whole_file_is_swapped_in_or_not_at_all(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    storage.atomic_write(path, "first")
    assert path.read_text() == "first"

    def broken(*a):
        raise OSError("the share has gone")

    monkeypatch.setattr(os, "replace", broken)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(OSError):
        storage.atomic_write(path, "second")
    assert path.read_text() == "first" and [p.name for p in tmp_path.iterdir()] == ["file.txt"]  # nothing left behind


def test_writers_side_by_side_dont_share_a_temporary_name(tmp_path):
    path = tmp_path / "file.txt"
    problems = []

    def write(text: str) -> None:
        try:
            for _ in range(20):
                storage.atomic_write(path, text * 1000)
        except OSError as e:
            problems.append(e)

    threads = [threading.Thread(target=write, args=(c,)) for c in "ab"]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not problems and path.read_text() in ("a" * 1000, "b" * 1000)
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"]


def test_a_snapshot_cut_short_is_passed_over(tmp_path):
    (tmp_path / "humble").mkdir()
    (tmp_path / "humble" / "2026-01-01_0000.json").write_text('{"good": 1}', encoding="utf-8")
    (tmp_path / "humble" / "2026-01-02_0000.json").write_text('{"cut', encoding="utf-8")
    assert snapshot.latest(tmp_path, "humble") == (tmp_path / "humble" / "2026-01-01_0000.json", {"good": 1})  # as read
    assert snapshot.latest(tmp_path, "storybundle") is None


def test_a_state_folder_that_isn_t_there_has_no_snapshot_but_one_that_can_t_be_listed_says_so(tmp_path, monkeypatch):
    assert snapshot.latest(tmp_path / "not-there", "humble") is None  # a library with no shop yet
    (tmp_path / "humble").mkdir()
    (tmp_path / "humble" / "2026-01-01_0000.json").write_text('{"good": 1}', encoding="utf-8")

    def denied(path="."):
        raise PermissionError(13, "Access is denied", str(path))
    monkeypatch.setattr(os, "listdir", denied)  # a share gone away: not "no orders", which would drop the shop's titles
    with pytest.raises(PermissionError):
        snapshot.latest(tmp_path, "humble")


def test_a_shop_keeps_its_newest_five_snapshots_and_the_newest_of_each_month_before_them(tmp_path, monkeypatch):
    """One snapshot a run would be thousands in a few years. After a new one is saved, a shop keeps its newest
    five and the newest of each month before them; the rest go. Never the newest, nor anything of its folder that isn't
    named as a snapshot is, nor another shop's."""
    from datetime import datetime

    humble = tmp_path / "humble"
    humble.mkdir()
    names = ["2025-12-03_0900", "2025-12-30_2200", "2026-01-15_1000", "2026-02-01_0800", "2026-02-20_0800", "2026-02-27_0800",
             "2026-03-01_1000", "2026-03-02_1000", "2026-03-04_1000", "2026-03-05_1000", "2026-03-06_1000"]
    for name in names:
        (humble / f"{name}.json").write_text("{}", encoding="utf-8")
    (humble / "plan.csv").write_text("kept", encoding="utf-8")
    (humble / "2026-01-01_0000 (copy).json").write_text("{}", encoding="utf-8")  # not a snapshot's name: not one
    (tmp_path / "storybundle").mkdir()
    (tmp_path / "storybundle" / "2025-01-01_0000.json").write_text("[]", encoding="utf-8")

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 9, 12, 30)

    monkeypatch.setattr(snapshot, "datetime", Now)
    saved = snapshot.save({"K1": {}}, tmp_path, "humble")
    assert saved == humble / "2026-03-09_1230.json"
    assert sorted(p.name for p in humble.iterdir()) == [
        "2025-12-30_2200.json", "2026-01-01_0000 (copy).json", "2026-01-15_1000.json", "2026-02-27_0800.json",
        "2026-03-02_1000.json", "2026-03-04_1000.json", "2026-03-05_1000.json", "2026-03-06_1000.json",
        "2026-03-09_1230.json", "plan.csv"]  # the newest five, and the newest of December, January and February
    assert (tmp_path / "storybundle" / "2025-01-01_0000.json").is_file()
    assert snapshot.kept(["2026-03-01_1000.json"], keep=0) == {"2026-03-01_1000.json"}  # never the newest


def test_thinning_never_removes_the_snapshot_just_saved_and_says_what_it_removed(tmp_path, monkeypatch):
    """With five snapshots or more named later in the same month already there (a clock that was ahead, set right since),
    the one just saved would be among those thinning removes: it never is, and the others are told, by name."""
    from datetime import datetime

    humble = tmp_path / "humble"
    humble.mkdir()
    later = [f"2026-03-{day}_1000.json" for day in (20, 21, 22, 23, 24, 25)]
    for name in later:
        (humble / name).write_text("{}", encoding="utf-8")

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 9, 12, 30)

    monkeypatch.setattr(snapshot, "datetime", Now)
    thinned = snapshot.Thinned()
    saved = snapshot.save({"K1": {}}, tmp_path, "humble", thinned)
    assert saved.is_file() and snapshot.load(saved) == {"K1": {}}
    assert sorted(p.name for p in humble.iterdir()) == [saved.name, *later[1:]] and thinned.removed == [later[0]]
    assert thinned.lines("humble") == ["Older snapshots thinned: 1 removed from state/humble/ (the newest 5 and the newest of "
                                       "each month before them stay): 2026-03-20_1000.json."]
    assert snapshot.latest(tmp_path, "humble")[0] == humble / later[-1]  # the newest by name, as ever


def test_trouble_thinning_is_said_and_the_snapshot_is_saved_all_the_same(tmp_path, monkeypatch):
    """Thinning is housekeeping: a snapshot it can't remove just now, or a folder it can't list, is said, and the run goes
    on with the snapshot saved."""
    humble = tmp_path / "humble"
    humble.mkdir()
    for month in range(1, 13):
        for day in (1, 2):
            (humble / f"2025-{month:02}-{day:02}_1000.json").write_text("{}", encoding="utf-8")
    real = os.remove

    def held(path, *args, **kw):
        if Path(path).name == "2025-06-01_1000.json":  # held open on a share, say
            raise PermissionError(13, "The process cannot access the file", str(path))
        return real(path, *args, **kw)

    monkeypatch.setattr(os, "remove", held)
    thinned = snapshot.Thinned()
    saved = snapshot.save({"K1": {}}, tmp_path, "humble", thinned)  # not the newest's text: that isn't saved again
    assert saved.is_file() and (humble / "2025-06-01_1000.json").is_file() and len(thinned.removed) == 9  # the 1st of 10 months
    assert thinned.trouble == ["2025-06-01_1000.json (The process cannot access the file)"]
    assert thinned.lines("humble")[1] == ("Older snapshots not thinned in state/humble/: 2025-06-01_1000.json (The process "
                                          "cannot access the file). They stay, and the next run thins them.")
    real_list = os.listdir

    def denied(path="."):
        if Path(path) == humble:
            raise PermissionError(13, "Access is denied", str(path))
        return real_list(path)

    monkeypatch.setattr(os, "listdir", denied)
    thinned = snapshot.Thinned()
    saved = snapshot.save({"K2": {}}, tmp_path, "humble", thinned)  # no exception: saved, and the trouble said
    assert snapshot.load(saved) == {"K2": {}} and thinned.removed == []
    assert thinned.trouble == ["its folder can't be listed just now (Access is denied)"]


def test_a_run_saves_a_snapshot_only_when_the_shop_s_listing_has_changed(tmp_path, monkeypatch):
    """A run with no change at the shop adds no copy of the newest snapshot, every night for ever: that one holds what
    the shop listed, and stands for the run, and nothing is thinned. A change is saved as ever, and so is a listing
    whose newest snapshot can't be read just now."""
    from datetime import datetime

    clock = [datetime(2026, 3, 9, 12, 30)]

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(snapshot, "datetime", Now)
    humble = tmp_path / "humble"
    first = snapshot.save({"K1": {"gamekey": "K1"}}, tmp_path, "humble")
    clock[0] = datetime(2026, 3, 10, 3, 0)
    thinned = snapshot.Thinned()
    assert snapshot.save({"K1": {"gamekey": "K1"}}, tmp_path, "humble", thinned) == first
    assert thinned == snapshot.Thinned() and [p.name for p in humble.iterdir()] == [first.name]
    both = {"K1": {"gamekey": "K1"}, "K2": {"gamekey": "K2"}}  # bought since
    changed = snapshot.save(both, tmp_path, "humble")
    assert changed.name == "2026-03-10_0300.json" and sorted(p.name for p in humble.iterdir()) == [first.name, changed.name]
    clock[0] = datetime(2026, 3, 11, 3, 0)
    changed.write_bytes(b"\xff cut short")  # can't be read: a new one is saved
    assert snapshot.save(both, tmp_path, "humble").name == "2026-03-11_0300.json"
    state = tmp_path / "state"  # the core's, every shop's but Humble's, alike
    guide = shopping.File(0, "guide.pdf", 10, "a" * 32)
    t = [shopping.Title("101", "Lantern Wolf Field Guide", "Moonwake Press", [guide])]
    made = shopping.snapshot("drivethrurpg", t, state)
    clock[0] = datetime(2026, 3, 12, 3, 0)
    assert shopping.snapshot("drivethrurpg", t, state) == made and len(list((state / "drivethrurpg").iterdir())) == 1


def test_a_failures_file_cut_short_is_started_anew(tmp_path):
    a = make_job("a.epub", b"1")
    path = tmp_path / "failures.csv"
    path.write_bytes(b"pa")  # the header cut off: this ended every later run
    assert dl.write_failures(path, {"ok": [], "failed": [(a, "boom")], "stopped": []}) == 1
    with path.open(encoding="utf-8-sig", newline="") as fh:
        assert [row["path"] for row in csv.DictReader(fh)] == [a.path]


@pytest.mark.skipif(os.name == "nt", reason="Windows goes by the folder's permissions")
def test_what_only_you_may_read_stays_yours(tmp_path, monkeypatch):
    from hylly import db

    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    access.add_user("reader", "correct horse")  # into the database, which holds the password hashes
    assert stat.S_IMODE(db.file().stat().st_mode) == 0o600
    before = os.umask(0o077)
    try:
        other = tmp_path / "orders.json"
        storage.atomic_write(other, "x")  # a new file gets what your umask says
        assert stat.S_IMODE(other.stat().st_mode) == 0o600
    finally:
        os.umask(before)
    os.chmod(other, 0o640)
    storage.atomic_write(other, "y")  # and one you have keeps what you gave it
    assert stat.S_IMODE(other.stat().st_mode) == 0o640
    os.chmod(db.file(), 0o644)
    paths.keep_private()
    assert stat.S_IMODE(paths.config_dir().stat().st_mode) == 0o700 and stat.S_IMODE(db.file().stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows goes by the folder's permissions")
def test_a_disk_without_permissions_is_no_reason_to_stop(tmp_path, monkeypatch):
    """A file's permissions are given as it's made, never by a chmod after, which a disk that keeps them as extended
    attributes (Linux's ntfs3) would store, and one that refuses chmod would fail: a chmod refused stops nothing, and a
    secret is its owner's only all the same. A disk that shows every file as others' (a share without permissions)
    isn't given a secret, which would be left readable: at once, as no wait mends that. One that shows every file as its
    owner's alone, the x bit with it (0700, as macOS's SMB mounts and some others do), is no such disk."""
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    path = tmp_path / "data.js"
    path.write_text("old")

    def refused(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    slept = []
    monkeypatch.setattr(storage.os, "chmod", refused)
    monkeypatch.setattr(storage.time, "sleep", slept.append)
    storage.atomic_write(path, "new")
    assert path.read_text() == "new"
    paths.keep_private()
    storage.atomic_write(path, "secret", mode=0o600)
    assert path.read_text() == "secret" and stat.S_IMODE(path.stat().st_mode) == 0o600
    real = os.fstat
    for shown, kept in ((0o100700, True), (0o100755, False), (0o100640, False)):  # as such a share shows each file
        with monkeypatch.context() as m:
            m.setattr(storage.os, "fstat", lambda fd: os.stat_result((shown, *real(fd)[1:])))
            if kept:
                storage.atomic_write(path, f"secret {shown:o}", mode=0o600)
            else:
                with pytest.raises(PermissionError, match="shows it to others"):
                    storage.atomic_write(path, "another secret", mode=0o600)
    assert path.read_text() == "secret 100700" and not list(tmp_path.glob("data.js.*.tmp")) and not slept


def test_a_disk_that_refuses_files_ends_the_write_instead_of_hanging_it(tmp_path, monkeypatch):
    tried = []

    def refused(path, *a, **k):
        tried.append(path)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage.os, "open", refused)
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        storage.atomic_write(tmp_path / "x.txt", "x")
    assert len(tried) == 6 and len(set(tried)) == 6


GONE_SHARE = "//127.0.0.1/hylly_no_such_share/library"  # really asked: faking the error once hid that open() doesn't give it
on_windows = pytest.mark.skipif(os.name != "nt", reason="Windows' own error numbers")


@pytest.fixture
def gone_share() -> str:
    """GONE_SHARE, asked of this computer once: Windows says 53 or 67 of a share that isn't there (share_is_gone). One
    that answers otherwise (without file sharing, say) has nothing for these tests to go by, which is no fault of the
    code: the test is skipped, with what it said."""
    try:
        os.stat(GONE_SHARE)
    except OSError as e:
        if not storage.share_is_gone(e):
            pytest.skip(f"this computer answers {GONE_SHARE} with Windows' error {getattr(e, 'winerror', None)} "
                        f"({e.strerror}), not 53 or 67, the numbers of a share that is gone")
        return GONE_SHARE
    pytest.skip(f"{GONE_SHARE} is there on this computer")


@on_windows
def test_a_share_gone_isnt_taken_for_having_no_records(tmp_path, gone_share):
    assert storage.read_lines(tmp_path / "not there") == []
    assert storage.read_lines(tmp_path / "no such folder" / "not there") == []
    for read in (lambda: plan.recorded_folders(Path(GONE_SHARE)), lambda: dl.Manifest(Path(GONE_SHARE), "humble"),
                 lambda: manifest.copies(Path(GONE_SHARE)), lambda: curation.load(Path(GONE_SHARE))):
        with pytest.raises(OSError) as e:
            read()
        assert storage.share_is_gone(e.value)


@on_windows
def test_a_command_says_so_when_the_share_is_gone(gone_share):
    with pytest.raises(SystemExit, match="can't be reached"):
        cli.main(["verify", "--library", GONE_SHARE])


@on_windows
def test_the_server_page_tells_a_gone_share_from_a_server_that_is_down(tmp_path, log_in, gone_share):
    (tmp_path / "catalog").mkdir()
    item = {"id": "x", "t": "A Title", "k": "book", "f": [["x.epub", "epub", 10]]}
    (tmp_path / "catalog" / "data.js").write_text("window.CATALOG = " + json.dumps({"items": [item]}) + ";", encoding="utf-8")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shelf = srv.RequestHandlerClass.catalog
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", timeout=60)) as client:
            s = client.get("/server/status").json()
            assert s["titles"] == 1 and not s["library_gone"]
            shelf.root, shelf.path = Path(GONE_SHARE), Path(GONE_SHARE) / "catalog" / "data.js"
            s = client.get("/server/status").json()
            assert s["titles"] == 0 and s["library_gone"]
            assert client.get("/opds").status_code == 500  # not an empty list, which a reading app would believe
            shelf.root, shelf.path = tmp_path, tmp_path / "catalog" / "data.js"
            s = client.get("/server/status").json()
            assert s["titles"] == 1 and not s["library_gone"]
    finally:
        srv.shutdown()
        srv.server_close()


@on_windows
def test_a_server_started_while_the_share_is_gone_knows_it_from_no_catalog_yet(tmp_path, gone_share):
    assert Catalog(tmp_path).get() == []
    with pytest.raises(OSError) as e:
        Catalog(Path(GONE_SHARE)).get()
    assert storage.share_is_gone(e.value)


@on_windows
def test_a_name_windows_refuses_isnt_retried(tmp_path):
    for name in ("a?b.epub.part", "x" * 300 + ".part"):
        with pytest.raises(OSError) as e:
            (tmp_path / name).open("wb")
        assert dl.no_use_retrying(e.value), name
    with pytest.raises(OSError) as e:
        (tmp_path / "no such folder" / "a.part").open("rb")
    assert not dl.no_use_retrying(e.value)


def test_the_server_starts_over_when_the_share_drops_out_between_two_reads(tmp_path, monkeypatch):
    data = tmp_path / "catalog" / "data.js"
    data.parent.mkdir()
    data.write_text(json.dumps({"items": [{"id": "x", "t": "A Title"}]}), encoding="utf-8")
    shelf = Catalog(tmp_path)
    real = curation.load

    def gone(root):
        raise OSError(5, "the share dropped out")

    monkeypatch.setattr(serve.curation, "load", gone)
    with pytest.raises(OSError):
        shelf.get()
    monkeypatch.setattr(serve.curation, "load", real)
    assert shelf.search("title")


# ---- logs ------------------------------------------------------------------------------------------------------------

def test_every_log_is_written_one_way_and_moved_aside_beyond_its_limit(tmp_path):
    """download-log.txt and lookup-log.txt once only grew, and the three logs each moved themselves aside their
    own way. One way now (storage.append_log): beyond the limit a log moves aside, NAME.1.txt for a run's log and
    NAME.log.1 for a .log, and the one before that goes; a line left torn is ended first; nothing is left aside."""
    log = tmp_path / "state" / "lookup-log.txt"
    assert storage.append_log(log, "the first", limit=100) is False  # none yet: made, folder and all
    assert log.read_text(encoding="utf-8") == "the first\n"
    (tmp_path / "state" / "lookup-log.1.txt").write_text("the oldest", encoding="utf-8")
    log.write_text("y" * 150, encoding="utf-8")  # over the limit, and its last line torn
    assert storage.append_log(log, ["one", "two"], limit=100) is True
    assert (tmp_path / "state" / "lookup-log.1.txt").read_text(encoding="utf-8") == "y" * 150
    assert log.read_text(encoding="utf-8") == "one\ntwo\n"
    log.write_text("z" * 150, encoding="utf-8")
    assert storage.append_log(log, "after a torn line") is False and log.read_text(encoding="utf-8") == "z" * 150 + "\nafter a torn line\n"
    access = tmp_path / "access.log"
    access.write_text("a" * 150 + "\n", encoding="utf-8")
    assert storage.append_log(access, "new", 100) and (tmp_path / "access.log.1").read_text(encoding="utf-8") == "a" * 150 + "\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["access.log", "access.log.1", "state"]


def test_one_cleaner_for_every_log_line_and_every_name_shown():
    """There were once five cleaners, two of them letting C1 controls and U+2028 through, none the marks that turn text
    round. One now (storage.shown): whatever could start a line or move the cursor is ?, every invisible format
    character (Unicode's Cf) is dropped, the tags that hide words from a reader among them, but the two joiners that
    Persian, the Indic scripts and emoji need; a half of a letter is ?, and other alphabets and spaces of every kind
    stay."""
    assert storage.shown("a\nb\rc\td\x1b[2Je\x7ff\u0085g\x9bh\u2028i\u2029j") == "a?b?c?d?[2Je?f?g?h?i?j"
    assert storage.shown("\u202eevil\u200b \u2066x\u2069\ufeff\u200f") == "evil x"
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "say all is well")  # tag characters: words a screen doesn't show
    assert storage.shown(f"Bot\U000e0001{hidden}\U000e007f/1.0") == "Bot/1.0"
    assert storage.shown("a\u061cb\u00adc\ufff9d\ufffae\ufffbf\u180eg\u2064h") == "abcdefgh"
    kept = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0645 \u0915\u094d\u200d\u0937 \U0001f469\u200d\U0001f4bb"  # joiners
    assert storage.shown(kept) == kept
    assert storage.shown("Ann\u00a0Writer\u3000Lumi\u00e4\u2009\u0416") == "Ann\u00a0Writer\u3000Lumi\u00e4\u2009\u0416"
    assert storage.shown("caf\udce9") == "caf?" and storage.shown(Path("x")) == "x"
    from hylly import hyllycopy

    assert catalog.shown is hyllycopy.shown is storage.shown  # the one, wherever it's named


def test_a_line_of_any_log_is_cleaned_as_it_is_written(tmp_path):
    """Whoever writes a log line, nothing in it can start another line or turn the text round."""
    log = tmp_path / "bell.log"
    storage.append_log(log, ["a\u0085b\u2028c\u202ed", "e\nf"], 1 << 20)
    assert log.read_text(encoding="utf-8") == "a?b?cd\ne?f\n"


def test_a_log_is_open_to_append_only_for_the_write_itself(tmp_path, monkeypatch):
    """A line written while another process moves the log aside goes into the file it opened: the longer the log is
    open before the write, the wider that window. The look at whether the last line was left torn comes before the
    log is opened to append, so the window is the write alone, as the server's own log had it before; a torn line
    is still ended first."""
    log = tmp_path / "server.log"
    log.write_text("a line cut sh", encoding="utf-8")
    opened, real = [], Path.open
    monkeypatch.setattr(Path, "open", lambda self, mode="r", *a, **k: (opened.append(mode) if self == log else None)
                        or real(self, mode, *a, **k))
    storage.append_log(log, "the next", 1 << 20)
    assert opened == ["rb", "ab"] and log.read_text(encoding="utf-8") == "a line cut sh\nthe next\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_log_that_is_a_pipe_still_gets_its_lines(tmp_path):
    """A log linked to a pipe (a service reading it as it comes) got no line: an append that first read the file's
    last letter can't seek in a pipe. A pipe's end is never looked at, and its lines go as they come."""
    pipe = tmp_path / "server.log"
    os.mkfifo(pipe)
    got = []
    reader = threading.Thread(target=lambda: got.append(pipe.read_bytes()), daemon=True)
    reader.start()
    assert storage.append_log(pipe, ["the first", "the second"], 100) is False
    reader.join(10)
    assert got == [b"the first\nthe second\n"]


def test_a_log_whose_older_file_cant_be_replaced_is_moved_again_only_an_hour_later(tmp_path, monkeypatch):
    """The older file held open by another program (Windows): each line renamed the log, failed, copied the whole of it
    back and removed the copy. Now a failed move is tried again only after MOVE_AGAIN, and the log takes its lines."""
    log = tmp_path / "access.log"
    log.write_text("x" * 150 + "\n", encoding="utf-8")
    (tmp_path / "access.log.1").mkdir()  # can't be replaced, as one held open can't
    (tmp_path / "access.log.1" / "inside").write_text("held", encoding="utf-8")
    tries, real = [], storage._take_aside
    monkeypatch.setattr(storage, "_take_aside", lambda *a: tries.append(1) or real(*a))
    for line in ("one", "two", "three"):
        assert storage.append_log(log, line, 100) is False
    assert tries == [1] and log.read_text(encoding="utf-8") == "x" * 150 + "\none\ntwo\nthree\n"  # every line, once tried
    assert sorted(p.name for p in tmp_path.iterdir()) == ["access.log", "access.log.1"]  # nothing left aside
    shutil.rmtree(tmp_path / "access.log.1")
    monkeypatch.setitem(storage._not_moved, str(log), 0.0)  # an hour later
    assert storage.append_log(log, "four", 100) is True and tries == [1, 1]
    assert (tmp_path / "access.log.1").read_text(encoding="utf-8").endswith("three\n") and log.read_text(encoding="utf-8") == "four\n"


def test_what_a_move_cut_short_left_beside_a_log_is_taken_back_in_at_the_next(tmp_path, monkeypatch):
    """A copy back cut short (a share gone, a full disk) left NAME.<hex> beside the log for good. At the log's next move
    aside its lines go into the older file, so none is lost (some may come twice), and it goes; one a moment old may be
    another process's move under way, and is left alone."""
    log = tmp_path / "download-log.txt"
    left = tmp_path / "download-log.txt.0a1b2c3d"
    left.write_text("a line a cut copy back left\n", encoding="utf-8")
    young = tmp_path / "download-log.txt.99999999"
    young.write_text("another move under way\n", encoding="utf-8")
    (tmp_path / "download-log.txt.notahex1").write_text("someone else's\n", encoding="utf-8")
    log.write_text("y" * 150 + "\n", encoding="utf-8")
    monkeypatch.setattr(storage, "LEFT_AGE", 0)  # as if the first were a minute old ...
    os.utime(young, (time.time() + 3600, time.time() + 3600))  # ... and the other only just written
    assert storage.append_log(log, "new", 100) is True
    assert (tmp_path / "download-log.1.txt").read_text(encoding="utf-8") == "y" * 150 + "\na line a cut copy back left\n"
    assert not left.exists() and young.exists() and (tmp_path / "download-log.txt.notahex1").exists()


def test_a_log_another_process_moved_first_is_no_failure_and_waits_no_hour(tmp_path, monkeypatch):
    """Two processes found the log over its limit at once (the server and a second start): the one that lost the race
    found it gone, took that for a failure and waited an hour before moving server.log or bell.log again. A log
    already moved is done with: the line begins a fresh one, and the next time it's over its limit, it moves."""
    log = tmp_path / "bell.log"
    log.write_text("z" * 150 + "\n", encoding="utf-8")
    real, raced = Path.replace, []

    def first_another_moves_it(self, target):
        if self == log and not raced:
            raced.append(1)
            real(log, tmp_path / "bell.log.1")  # the other process, a moment before
        return real(self, target)

    monkeypatch.setattr(Path, "replace", first_another_moves_it)
    assert storage.append_log(log, "after the race", 100) is False
    assert str(log) not in storage._not_moved and log.read_text(encoding="utf-8") == "after the race\n"
    log.write_text("y" * 150 + "\n", encoding="utf-8")
    assert storage.append_log(log, "and the next move", 100) is True  # not an hour later: now
    assert (tmp_path / "bell.log.1").read_text(encoding="utf-8") == "y" * 150 + "\n"


def test_a_leftover_is_taken_back_by_one_take_back_only(tmp_path, monkeypatch):
    """Two take-backs of one leftover at once both copied it into the older file, and tore lines between them. Each
    claims it first by renaming it; the other finds it gone, so its lines come once and whole."""
    log = tmp_path / "server.log"
    left = tmp_path / "server.log.0a1b2c3d"
    left.write_text("".join(f"left line {i}\n" for i in range(100)), encoding="utf-8")
    monkeypatch.setattr(storage, "LEFT_AGE", 0)
    real, inner = shutil.copyfileobj, []

    def another_take_back_meanwhile(source, target, *a):
        if not inner:  # while the first copies, a second one looks
            inner.append(1)
            storage._take_back(log)
        return real(source, target, *a)

    monkeypatch.setattr(storage.shutil, "copyfileobj", another_take_back_meanwhile)
    storage._take_back(log)
    assert inner and (tmp_path / "server.log.1").read_text(encoding="utf-8") == "".join(f"left line {i}\n" for i in range(100))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["server.log.1"]
    left.write_text("held\n", encoding="utf-8")  # a copy that fails: it keeps its name, for the next move
    monkeypatch.setattr(storage.shutil, "copyfileobj", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    storage._take_back(log)
    assert left.read_text(encoding="utf-8") == "held\n" and not left.with_name(left.name + ".taking").exists()


def test_no_log_is_appended_to_but_one_way():
    """The three logs and the runs' logs each once had their own way of appending and of moving aside. Every file
    opened to append to, in any mode (a, ab, a+, a+b, os.open with O_APPEND), is storage's now, but what is no log
    written line by line: the hidden server's errors, which it hands to the program it starts as its standard error
    (instance.spawn); what a copy knows, written in batches on the target disk with a care of its own
    (hyllycopy.Record), and the checksums of a backup's earlier copies, beside them on its disk in a folder that has to
    be there (backup.BackingUp._noted); and the locks, opened so that they are made when missing and never emptied."""
    import ast

    src = Path(storage.__file__).parent
    found = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "open":
                modes = [a.value for a in [*node.args, *(k.value for k in node.keywords if k.arg == "mode")]
                         if isinstance(a, ast.Constant) and isinstance(a.value, str) and re.fullmatch(r"[rwxa]\+?[bt]?\+?", a.value)]
                found += [(path.name, mode) for mode in modes if "a" in mode]
            if any(isinstance(n, ast.Attribute) and n.attr == "O_APPEND" for n in ast.walk(node)):
                found.append((path.name, "O_APPEND"))
    locks = [("bell.py", "a+b"), ("db.py", "a+b"), ("db.py", "a+b"), ("instance.py", "a+b"), ("periodic.py", "a+b"),
             ("runs.py", "a+b"), ("runs.py", "a+b")]  # db's second: the turn to make the token (db._making)
    assert sorted(found) == sorted([*locks, ("backup.py", "a+b"), ("hyllycopy.py", "ab"), ("instance.py", "ab"), ("storage.py", "a+b"),
                                    ("storage.py", "ab"), ("storage.py", "ab"), ("storage.py", "ab")])


# ---- a write tried again ---------------------------------------------------------------------------------------------

def test_a_write_tries_again_waiting_longer_where_asked_and_telling_of_each_try(monkeypatch):
    slept, told, tries = [], [], []
    monkeypatch.setattr(time, "sleep", slept.append)

    def flaky():
        tries.append(1)
        if len(tries) < 3:
            raise OSError(22, "Invalid argument")
        return "written"

    def never():
        raise OSError(22, "Invalid argument")

    assert storage.retrying(flaky) == "written" and slept == [0.5, 1.0]  # Hylly's own small writes
    slept.clear()
    with pytest.raises(OSError):
        storage.retrying(never, 7, 5, longest=60, told=told.append)
    assert slept == [5, 10, 20, 40, 60, 60] and len(told) == 6 and all(e.errno == 22 for e in told)
    slept.clear(), told.clear()
    with pytest.raises(OSError):  # one no wait will mend: at once
        storage.retrying(never, 7, 5, longest=60, told=told.append, hopeless=lambda e: e.errno == 22)
    assert slept == [] and told == []


def test_a_path_below_a_file_is_answered_at_once_and_a_share_away_still_gets_its_tries(tmp_path, monkeypatch):
    (tmp_path / "a file").write_text("not a folder", encoding="utf-8")
    below = tmp_path / "a file" / "state" / "records.jsonl"  # --library given below a file, say: no wait mends that
    began = time.monotonic()
    if os.name == "nt":  # Windows says no such file there, as for a folder that isn't there: no records, as above
        assert storage.read_lines(below) == []
    else:
        with pytest.raises(NotADirectoryError):
            storage.read_lines(below)
    assert time.monotonic() - began < 2  # it took about 15 seconds of tries
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)

    def below_a_file():
        raise NotADirectoryError(20, "Not a directory")

    with pytest.raises(NotADirectoryError):
        storage.retrying(below_a_file)
    assert slept == []

    def away():
        raise OSError(22, "Invalid argument")

    with pytest.raises(OSError):
        storage.retrying(away)
    assert slept == [0.5, 1.0, 2.0, 4.0, 8.0]


# ---- a small JSON file -----------------------------------------------------------------------------------------------

def test_a_small_json_file_is_a_table_or_nothing(tmp_path):
    path = tmp_path / "note.json"
    assert storage.read_json(path) == {}  # not there
    for damaged in ('{"pid": 12', "[1, 2]", "12", "null", '"text"'):  # cut short, or not a table
        path.write_text(damaged, encoding="utf-8")
        assert storage.read_json(path) == {}, damaged
    path.write_bytes(b'{"name": "\xff"}')  # not UTF-8
    assert storage.read_json(path) == {}
    path.write_text('{"pid": 12, "name": "Ann Writer"}', encoding="utf-8")
    assert storage.read_json(path) == {"pid": 12, "name": "Ann Writer"}
    assert storage.read_json(tmp_path) == {}  # a folder
    (tmp_path / "a file").write_text("{}", encoding="utf-8")
    assert storage.read_json(tmp_path / "a file" / "note.json") == {}  # below a file
