"""One run at a time of each job that talks to a site: a second is refused and told where the first runs; and the note a
run keeps beside the library, which another computer goes by, whatever cut the run short (once among the tests of
writes cut short, test_storage.py)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import pytest

from hylly import catalog, cli, instance, runs, storage

from helpers import build_library


def test_a_second_run_is_refused_and_told_where_the_first_runs():
    assert runs.current("lookup") is None
    with runs.only_one("lookup"):
        assert runs.current("lookup")["pid"] == os.getpid()
        with pytest.raises(runs.Busy) as busy:
            with runs.only_one("lookup"):
                pass
        assert f"process {os.getpid()}, since " in str(busy.value) and "hylly jobs stop lookup" in str(busy.value)
        with runs.only_one("humble"):  # another site doesn't wait for it
            pass
    assert runs.current("lookup") is None and not runs.note_file("lookup").exists()
    with runs.only_one("lookup"):  # free again
        pass


def test_the_site_commands_wait_their_turn(monkeypatch, tmp_path, capsys):
    with runs.only_one("humble"):
        for command in (["humble", "download"], ["humble", "seed", str(tmp_path)]):  # before Humble is asked
            with pytest.raises(runs.Busy):
                cli.main([*command, "--library", str(tmp_path)])
        with pytest.raises(runs.Busy):  # a dry run reads Humble's site too, as the run does: it waits its turn
            cli.main(["humble", "download", "--dry-run", "--library", str(tmp_path)])
        with pytest.raises(SystemExit) as offline:  # a seed's dry run reads the last snapshot only, so it needn't wait
            cli.main(["humble", "seed", str(tmp_path), "--dry-run", "--library", str(tmp_path)])
        assert not isinstance(offline.value, runs.Busy)
    with runs.only_one("lookup"):
        with pytest.raises(runs.Busy):
            cli.main(["lookup", "--library", str(tmp_path)])
        seen = {}
        monkeypatch.setattr(catalog, "build", lambda root, **kw: seen.update(kw))
        cli.main(["catalog", "--lookup", "--library", str(tmp_path)])  # it builds, from what was found so far
        assert seen["lookup"] is False and "without new lookups" in capsys.readouterr().err  # a note


def test_two_settings_folders_on_one_library_each_guard_decides_its_part(monkeypatch, tmp_path):
    """As two computers on one library (runs.py's opening text): the second one's change to the library's records is
    refused by the owner check, whatever lock it got; its lookup, which opens none of the records, by the library's
    lock while the first one's runs. Its own lock, in its own settings folder, holds nothing back."""
    from hylly import curation, db
    from hylly.exits import NOT_SET_UP

    root, first = tmp_path / "library", os.environ["HYLLY_HOME"]
    build_library(root)  # the first one keeps the library's records
    with runs.only_one("lookup", root):  # the first one's lookup, running
        monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "second"))
        db.close_all()
        with pytest.raises(runs.Busy, match="already running on .*it works on the same library"):
            with runs.only_one("lookup", root):
                pass
        with runs.only_one("catalog", root):  # a job the first isn't running: the lock is the second's
            with pytest.raises(db.NotHere, match="run this there"):
                curation.change(root, "add", "Knitting", [{"id": "h:novel", "t": "The Novel", "f": [["humble/b/novel.epub"]]}], "owen")
        with pytest.raises(SystemExit) as refused:  # and a command that changes them, with no job's lock at all
            cli.main(["tag", "add", "Knitting", "--id", "h:novel", "--yes", "--library", str(root)])
        assert refused.value.code == NOT_SET_UP
        monkeypatch.setenv("HYLLY_HOME", first)
        db.close_all()
    with runs.only_one("lookup", root):  # free again, once the first one's is done
        assert curation.load(root).of(["h:novel"]) == []


def test_stop_ends_a_run_that_has_hung(capsys):
    script = "import time\nfrom hylly import runs\nwith runs.only_one('storybundle'):\n    time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", script])
    deadline = time.monotonic() + 20
    while not runs.current("storybundle"):
        assert child.poll() is None and time.monotonic() < deadline
        time.sleep(0.1)
    cli.main(["jobs", "stop", "storybundle"])
    assert "Stopped (process " in capsys.readouterr().out
    assert child.wait(timeout=10) is not None and runs.current("storybundle") is None
    cli.main(["jobs", "stop", "storybundle"])
    assert "No StoryBundle download is running" in capsys.readouterr().out


def test_a_stopped_or_killed_run_is_said_not_to_have_ended_not_taken_for_the_run_before(capsys):
    """A stop ends a job at once, as closing its window or a power cut does: its watch never got to write how it ended,
    and hylly jobs showed the run before as the last, exit code 0 and all. Now the record begins as the run takes its
    lock, as one that hasn't ended, and only its end replaces it: while it runs it is shown as running, and once
    stopped it is said not to have ended."""
    runs.remember("storybundle", "2026-11-01 10:00", 60, 0, "All 12 files done.")  # the run before, which went well
    script = ("import argparse, time\nfrom hylly import cli, runs\ncli._keep_awake = lambda on: None\n"
              "with cli._watched('storybundle', argparse.Namespace()):\n    with runs.only_one('storybundle'):\n"
              "        time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", script])
    deadline = time.monotonic() + 20
    while not runs.current("storybundle"):
        assert child.poll() is None and time.monotonic() < deadline
        time.sleep(0.1)
    cli.main(["jobs"])
    out = capsys.readouterr().out
    assert "Running: A StoryBundle download" in out and "didn't end" not in out and "All 12 files done" not in out
    cli.main(["jobs", "stop", "storybundle"])
    assert child.wait(timeout=10) is not None and "Stopped (process " in capsys.readouterr().out
    cli.main(["jobs"])
    out = capsys.readouterr().out
    assert "No job is running." in out and "Last: A StoryBundle download, " in out
    assert "didn't end (stopped or killed)." in out and "All 12 files done" not in out and "exit code 0" not in out
    cli.main(["jobs", "--json"])
    (last,) = [r for r in json.loads(capsys.readouterr().out)["last"] if r["job"] == "storybundle"]
    assert last["ended"] is False and last["exit"] is None and last["pid"] == child.pid


def test_a_watched_run_refused_after_taking_its_lock_leaves_the_record_before_it(capsys):
    """A run that doesn't count, refused once its lock is held (no login saved, say): the record it began goes, and
    the one before is there again, as hylly jobs told of it before. One that ends writes its end over the begun one."""
    from hylly.exits import NOT_SET_UP, Exit

    runs.remember("lookup", "2026-11-01 10:00", 60, 0, "Looked up 3.")
    before = runs.last_file("lookup").read_bytes()
    with pytest.raises(Exit):
        with cli._watched("lookup", argparse.Namespace()), runs.only_one("lookup"):
            assert json.loads(runs.last_file("lookup").read_text(encoding="utf-8"))["ended"] is False
            raise Exit(NOT_SET_UP, "No source has a key.")
    assert runs.last_file("lookup").read_bytes() == before and runs.RECORD == {}
    with cli._watched("lookup", argparse.Namespace()), runs.only_one("lookup"):
        pass
    (last,) = [r for r in runs.last_runs() if r["job"] == "lookup"]
    assert last["ended"] is True and last["exit"] == 0 and last["started"] != "2026-11-01 10:00"


@pytest.mark.slow
def test_a_note_left_behind_names_no_process_to_end(capsys):
    """A killed run leaves its note, and its number goes to another program in time. While the lock is held for a
    moment (a run starting, a check), a stop ends nothing; with the lock free, the note is a leftover, and goes."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # some program, begun after the note
    try:
        old = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 600))
        runs.note_file("lookup").parent.mkdir(parents=True, exist_ok=True)
        runs.note_file("lookup").write_text(json.dumps({"pid": other.pid, "started": old}), encoding="utf-8")
        with runs._open("lookup") as fh:
            assert instance._lock(fh)  # held a moment by someone else
            with pytest.raises(SystemExit, match=f"tells of process {other.pid}, which has ended or is another program: nothing was stopped"):
                runs.stop("lookup")
            instance._unlock(fh)
        assert other.poll() is None  # it runs on
        cli.main(["jobs", "stop", "lookup"])
        assert "No lookup is running" in capsys.readouterr().out and not runs.note_file("lookup").exists()
    finally:
        other.kill()
        other.wait()


# ---- who runs what, on the library and across computers: each way found to lose a run (once in test_storage.py) ------

def refused_by_the_share(monkeypatch, root):
    real = instance._lock
    monkeypatch.setattr(runs.instance, "_lock", lambda fh: False if str(fh.name).startswith(str(root)) else real(fh))


def noted_run(note, computer, pid, started):
    note.write_text(json.dumps({"host": "the-other-computer", "computer": computer, "pid": pid, "started": started}), encoding="utf-8")


def test_a_run_ended_by_a_power_cut_doesnt_block_the_next(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    root = tmp_path / "library"
    (root / "state" / "locks").mkdir(parents=True)
    note = root / "state" / "locks" / "catalog.json"
    refused_by_the_share(monkeypatch, root)
    ended = int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True).stdout)
    now = time.strftime("%Y-%m-%d %H:%M")
    noted_run(note, runs._this_computer(), ended, now)
    with runs.only_one("catalog", root):
        # with a note of its own, or another computer would take the lock for the share's doing and go ahead too
        assert json.loads(note.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not note.exists()
    noted_run(note, runs._this_computer(), os.getpid(), "2020-01-01 10:00")  # its number given to a process started after it
    with runs.only_one("catalog", root):
        pass
    assert not note.exists()
    # still running: on another computer, also with this one's name, or under another of this computer's users, as
    # another settings folder: said as such, not by the name in the note
    for computer, pid, said in (("another-computer", ended, "the-other-computer"),
                                (runs._this_computer(), os.getpid(), "this computer, with other settings")):
        noted_run(note, computer, pid, now)
        with pytest.raises(runs.Busy, match=said):
            with runs.only_one("catalog", root):
                pass


def test_a_share_that_wont_take_the_note_doesnt_stop_the_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    root = tmp_path / "library"
    real = storage.atomic_write

    def refused(path, *a, **k):
        if str(path).startswith(str(root)):
            raise PermissionError(13, "Permission denied")
        return real(path, *a, **k)

    monkeypatch.setattr(runs, "atomic_write", refused)
    with runs.only_one("catalog", root):
        pass


def test_a_refusal_by_the_library_s_note_names_the_note_says_how_old_it_is_and_still_refuses(tmp_path, monkeypatch):
    """A share that refuses this computer's locks, and a note left by another computer's run that was killed: the job
    was refused for as long as that lasted, with neither the note's file nor what to do. Now the refusal names the
    note, to be deleted if that computer isn't running the job, and a note left as it was for an hour or more says how
    long, by the time in it or, for one from before, by its file's; the job is still refused while the note is there,
    as the lock may well be held. The dry run's look says the same."""
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    root = tmp_path / "library"
    (root / "state" / "locks").mkdir(parents=True)
    note = root / "state" / "locks" / "catalog.json"
    (root / "state" / "locks" / "catalog.lock").touch()
    refused_by_the_share(monkeypatch, root)
    three_days = time.time() - 3 * 86400
    note.write_text(json.dumps({"host": "the-other-computer", "computer": "another-computer", "pid": 4242,
                                "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(three_days)), "began": three_days}),
                    encoding="utf-8")
    for _ in range(2):  # refused each time: nothing passes a lock it can't take
        with pytest.raises(runs.Busy) as refused:
            with runs.only_one("catalog", root):
                pass
        said = str(refused.value)
        assert "already running on the-other-computer" in said and "Its note hasn't changed for 3 days." in said
        assert f"If that computer isn't running it, delete its note, {note}, and start this again." in said
    with pytest.raises(runs.Busy, match="hasn't changed for 3 days. If that computer isn't running it, delete its note"):
        runs.not_running("catalog", root)
    noted_run(note, "another-computer", 4242, time.strftime("%Y-%m-%d %H:%M"))  # from before: no time in it, its file's
    os.utime(note, (time.time() - 5 * 3600,) * 2)
    with pytest.raises(runs.Busy, match="Its note hasn't changed for 5 hours."):
        with runs.only_one("catalog", root):
            pass
    os.utime(note)  # a run that has just begun, or gone on: no age said
    with pytest.raises(runs.Busy) as refused:
        with runs.only_one("catalog", root):
            pass
    assert "hasn't changed" not in str(refused.value) and str(note) in str(refused.value)
    note.unlink()  # deleted, as it says: a lock refused with no note is the share's doing, and the job goes
    with runs.only_one("catalog", root):
        pass


def test_a_run_keeps_its_note_beside_the_library_new_as_it_goes_without_its_lines(tmp_path, monkeypatch):
    """runs.progress wrote only this computer's note, so another computer's refusal could never tell a run going on
    from one long over. Now the note beside the library gets the time too, as each line of progress comes, in a
    thread of its own, as a share that stalls must hold up no job; never the line itself, which may name a title."""
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(runs, "_said", 0.0)
    root = tmp_path / "library"
    note = root / "state" / "locks" / "catalog.json"
    with runs.only_one("catalog", root):
        began = json.loads(note.read_text(encoding="utf-8"))
        assert "seen" not in began
        runs.progress("12 of 40 books: The Lantern Wolf")
        deadline = time.monotonic() + 10
        while "seen" not in json.loads(note.read_text(encoding="utf-8")):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        while runs._touching.locked():
            time.sleep(0.01)
        now = json.loads(note.read_text(encoding="utf-8"))
        assert {k: now[k] for k in began} == began and now["seen"] >= began["began"] and "Lantern Wolf" not in json.dumps(now)
        assert "Lantern Wolf" in runs.note_file("catalog").read_text(encoding="utf-8")  # hylly jobs has the line
        old = {**began, "computer": "another-computer", "began": time.time() - 3 * 86400}
        assert "hasn't changed" in runs._elsewhere("catalog", old, note, "")
        assert "hasn't changed" not in runs._elsewhere("catalog", {**old, "seen": now["seen"]}, note, "")
    assert not note.exists()


def test_a_job_on_another_computer_is_named(tmp_path):
    folder = tmp_path / "state" / "locks"
    folder.mkdir(parents=True)
    (folder / "catalog.json").write_text(json.dumps({"host": "other-computer", "pid": 7, "started": "2026-11-04 10:00"}), encoding="utf-8")
    from hylly import instance

    with open(folder / "catalog.lock", "a+b") as fh:
        assert instance._lock(fh)  # held, as that computer would hold it
        with pytest.raises(runs.Busy, match="other-computer"):
            with runs.only_one("catalog", tmp_path):
                pass
        instance._unlock(fh)
    with runs.only_one("catalog", tmp_path):
        assert json.loads((folder / "catalog.json").read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not (folder / "catalog.json").exists()


def test_a_share_that_cant_lock_stops_nothing(tmp_path, monkeypatch):
    from hylly import instance

    real = instance._lock
    monkeypatch.setattr(instance, "_lock", lambda fh: real(fh) if "locks" not in fh.name else False)
    ran = []
    with runs.only_one("catalog", tmp_path):  # refused, and no note says anyone holds it
        ran.append(True)
    assert ran


def test_two_catalog_builds_dont_run_at_once(tmp_path):
    with runs.only_one("catalog", tmp_path), pytest.raises(runs.Busy, match="write the same files"):
        cli.main(["catalog", "--library", str(tmp_path)])
