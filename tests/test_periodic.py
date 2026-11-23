"""The library server's periodic jobs (periodic.py): each one an entry of its own, run when it's due and not before, a
failing one said in one line without stopping the others or the server, the server leaving what may run elsewhere to
hylly server jobs when [server] jobs = "external", and hylly server jobs running what's due for a timer: the quick ones
itself, a long one as a process of its own, each under a lock of its own."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hylly import access, bell, cli, dbcopy, exits, paths, periodic, settings, updates

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def external(on: bool = True) -> None:
    paths.settings_file().write_text(f'[server]\njobs = "{"external" if on else "internal"}"\n', encoding="utf-8")
    settings.reload()


def made_up(ran: list[str], *names: str, server_only: tuple[str, ...] = (), fails: tuple[str, ...] = (),
            long: tuple[str, ...] = (), every: float = periodic.MINUTE) -> tuple:
    """Jobs that only say they ran: those in fails end as Hylly's own Exit does."""
    def job(name: str) -> periodic.Job:
        def run(ctx) -> None:
            ran.append(name)
            if name in fails:
                raise exits.Exit(exits.NOT_SET_UP, "a mistake in the settings")
        return periodic.Job(name, every, run, f"{name}, made up", server_only=name in server_only, long=name in long)
    return tuple(job(name) for name in names)


def context(root: Path, said: list[str] | None = None) -> periodic.Context:
    return periodic.Context(root, (said if said is not None else []).append, bell.Watch(log=lambda m: None, send=lambda *a: None))


def kept(name: str) -> dict:
    return json.loads((periodic.folder() / f"{name.replace(' ', '-')}.json").read_text(encoding="utf-8"))


def test_each_job_is_one_entry_with_its_name_interval_function_and_kind():
    names = [job.name for job in periodic.JOBS]
    assert len(names) == len(set(names)) and len({job.slug for job in periodic.JOBS}) == len(names)
    assert all(callable(job.run) and job.every > 0 and job.what for job in periodic.JOBS)
    assert names[:3] == ["alarms", "nightly copy", "inbox"]  # what a timer may run, the rest the server's own
    assert {job.name for job in periodic.JOBS if job.server_only} == {"alarms held back", "home address", "update check",
                                                                       "inbox watch"}
    assert [job.name for job in periodic.JOBS if job.long] == ["nightly copy", "inbox"]
    assert [job.name for job in periodic.JOBS if job.outside_walls] == ["inbox"]
    every = {job.name: job.every for job in periodic.JOBS}
    assert every["alarms"] == 60 and every["nightly copy"] == 3600 and every["update check"] == periodic.ONCE
    assert every["inbox"] == 60


def test_the_server_tells_of_its_held_back_alarms_in_one_place(monkeypatch):
    """As it comes the alarms job does (bell.Watch.tick), so the job of their own runs only while the alarms run
    elsewhere (jobs = "external"): never twice a minute."""
    inside = [job.name for job in periodic.mine(server=True)]
    outside = [job.name for job in periodic.mine(server=True, outside=True)]
    assert inside == ["alarms", "nightly copy", "inbox", "home address", "update check", "inbox watch"]
    assert outside == ["alarms held back", "home address", "update check", "inbox watch"]
    assert [job.name for job in periodic.mine(server=False)] == ["alarms", "nightly copy", "inbox"]


def test_a_job_runs_when_it_is_due_and_not_before(tmp_path):
    ran: list[str] = []
    runner = periodic.Runner(context(tmp_path), jobs=made_up(ran, "a", "b"))
    assert runner.run_due(now=1000.0, wait=5) == ["a", "b"]  # never run yet: due
    assert runner.run_due(now=1010.0, wait=5) == []  # ten seconds on: not yet
    assert runner.run_due(now=1000.0 + 60 - periodic.EARLY, wait=5) == ["a", "b"]  # a timer a moment early still counts
    assert runner.run_due(now=500.0, wait=5) == ["a", "b"]  # the clock put back: due at once
    once = periodic.Job("first", periodic.ONCE, lambda ctx: ran.append("first"), "as it starts", server_only=True)
    runner = periodic.Runner(context(tmp_path), jobs=(once,))
    assert runner.run_due(now=1000.0, wait=5) == ["first"] and runner.run_due(now=10.0 ** 9, wait=5) == []
    assert runner.run_due(now=0.0, wait=5) == []  # nor with the clock put back: the update check is once
    assert ran.count("a") == ran.count("b") == 3 and ran[-1] == "first" and len(ran) == 7  # each in a thread of its own


def test_a_job_that_fails_is_said_in_one_line_and_stops_neither_the_others_nor_the_next_round(tmp_path):
    """A SystemExit, such as Hylly's own Exit, ends a thread without a word: the job's is caught, said, and the job is
    tried again when it's next due (as the nightly copy's and the alarms' own loops once did)."""
    ran, said = [], []
    runner = periodic.Runner(context(tmp_path, said), jobs=made_up(ran, "nightly copy", "alarms", fails=("nightly copy",)))
    assert runner.run_due(now=0.0, wait=5) == ["nightly copy", "alarms"]
    assert sorted(ran) == ["alarms", "nightly copy"] and runner.failed == ["nightly copy"]
    assert said == ["The server's job 'nightly copy' failed this time (Exit: a mistake in the settings); it's tried again "
                    "when it's next due."]
    assert runner.run_due(now=60.0, wait=5) == ["nightly copy", "alarms"] and len(ran) == 4  # the next round, both again


def test_the_server_s_rounds_go_on_after_a_failing_job_and_end_with_their_event(tmp_path, monkeypatch):
    """The real rounds (start), one after another: a job ending as Hylly's own Exit does is said each round, and the
    next round comes all the same; the rounds end when runner.ended is set, as the server does when it stops."""
    ran, said = [], []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "nightly copy", "alarms", fails=("nightly copy",), every=0.01))
    runner = periodic.start(context(tmp_path, said), every=0.01)
    try:
        deadline = time.monotonic() + 10
        failed = periodic.failed(periodic.JOBS[0], exits.Exit(exits.NOT_SET_UP, "a mistake in the settings"))
        while said.count(failed) < 2 or ran.count("alarms") < 2:  # each job in a thread of its own: wait for both
            assert time.monotonic() < deadline, said
            time.sleep(0.01)
    finally:
        runner.ended.set()
    runner.rounds.join(5)
    for thread in list(runner._running.values()):  # each job's own thread, the last round's alarms among them
        thread.join(5)
    assert not runner.rounds.is_alive() and ran.count("alarms") >= 2


def test_a_slow_job_holds_up_no_other_and_isnt_started_twice(tmp_path):
    gate, ran = threading.Event(), []
    slow = periodic.Job("slow", periodic.MINUTE, lambda ctx: (ran.append("slow"), gate.wait(10)), "a disk that's going")
    quick = periodic.Job("quick", periodic.MINUTE, lambda ctx: ran.append("quick"), "the alarms, say")
    runner = periodic.Runner(context(tmp_path), jobs=(slow, quick))
    try:
        runner.run_due(now=0.0, wait=1)
        assert "quick" in ran and runner.still_running() == ["slow"]
        assert runner.run_due(now=60.0, wait=1) == ["quick"] and ran.count("slow") == 1
    finally:
        gate.set()


def test_the_server_leaves_a_job_to_hylly_server_jobs_while_its_run_of_it_holds_the_lock(tmp_path):
    """A switch of [server] jobs mid-round can't run one job twice: the server takes each one's lock too."""
    ran: list[str] = []
    jobs = made_up(ran, "nightly copy")
    runner = periodic.Runner(context(tmp_path), jobs=jobs)
    with periodic.held(jobs[0]) as got:
        assert got
        runner.run_due(now=0.0, wait=5)
    assert ran == [] and not runner.failed
    runner.run_due(now=3600.0, wait=5)
    assert ran == ["nightly copy"]


def test_each_of_the_real_jobs_runs_what_it_moved_from(tmp_path, monkeypatch):
    """The move kept each job's own code where it was: the alarms' look and their held ones (bell.Watch), the nightly
    copy (dbcopy, which people.py had), the address at home (access) and the update check (updates)."""
    ran, said = [], []

    class Watch:
        def tick(self):
            ran.append("tick")

        def tell_of_held(self):
            ran.append("held")

    monkeypatch.setattr(dbcopy, "copy_tonight", lambda root, log, now=None, said=None: ran.append(("copy", root, said)))
    monkeypatch.setattr(access, "adapter_address", lambda: (ran.append("adapters"), "192.0.2.20")[1])
    monkeypatch.setattr(updates, "behind_once", lambda: ({"what": "7-Zip", "have": "16.02", "want": "26.02", "why": "Old.",
                                                          "next": "Get the newest."},))
    ctx = periodic.Context(tmp_path, said.append, Watch())
    runner = periodic.Runner(ctx)
    assert runner.run_due(now=0.0, wait=10) == ["alarms", "nightly copy", "inbox", "home address", "update check", "inbox watch"]
    assert sorted(map(str, ran)) == sorted(map(str, ["tick", ("copy", tmp_path, ctx.said), "adapters"])) and not runner.failed
    assert said == ["Worth updating: 7-Zip 16.02, older than 26.02. Old. Get the newest."]
    assert access.lan_address() == "192.0.2.20"
    ran.clear()
    assert runner.run_due(now=60.0, outside=True, wait=10) == ["alarms held back"]  # jobs = "external": the server's own
    assert ran == ["held"]


def test_with_jobs_external_the_server_runs_only_its_own(tmp_path, monkeypatch):
    """The server leaves to hylly server jobs what may run elsewhere: none of those run in it, and it says so once. Its
    own (server_only) it runs either way. The setting is read each round."""
    ran, said = [], []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "alarms", "nightly copy", "home address", server_only=("home address",)))
    external()
    rounds = []
    real = periodic.Runner.run_due
    monkeypatch.setattr(periodic.Runner, "run_due", lambda self, **k: rounds.append(real(self, wait=5, **k)))
    runner = periodic.start(context(tmp_path, said), every=3600)
    try:
        deadline = time.monotonic() + 10
        while not rounds and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        runner.ended.set()
    assert rounds == [["home address"]] and ran == ["home address"]
    assert said == [periodic.LEFT_TO_A_TIMER] and "hylly server jobs" in said[0] and "hylly-jobs.timer" in said[0]


def test_the_server_runs_them_all_as_it_comes(tmp_path, monkeypatch):
    ran, said = [], []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "alarms", "home address", server_only=("home address",)))
    assert settings.get("server")["jobs"] == "internal" and not periodic.external()
    runner = periodic.start(context(tmp_path, said), every=3600)
    try:
        for _ in range(500):  # each job in a thread of its own
            if len(ran) == 2:
                break
            time.sleep(0.02)
    finally:
        runner.ended.set()
    assert sorted(ran) == ["alarms", "home address"] and said == []  # nothing to say as it comes


def test_hylly_server_jobs_runs_what_is_due_and_keeps_when_each_ran(tmp_path, monkeypatch, capsys):
    ran: list[str] = []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "alarms", "reports", "home address", server_only=("home address",)))
    external()
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out == "Ran what was due: alarms, reports.\n"
    assert ran == ["alarms", "reports"]  # never the server's own
    assert kept("alarms")["said"] == {} and kept("reports")["last"] == kept("alarms")["last"]  # each its own, beside its lock
    assert sorted(p.name for p in periodic.folder().iterdir()) == ["alarms.json", "alarms.lock", "reports.json", "reports.lock"]
    cli.main(["server", "jobs", "--library", str(tmp_path)])  # a moment later: not due yet
    assert capsys.readouterr().out == "Nothing was due here.\nNext: alarms in 1 minute, reports in 1 minute.\n"
    for name in ("alarms", "reports"):
        (periodic.folder() / f"{name}.json").write_text(json.dumps({**kept(name), "last": kept(name)["last"] - 120}),
                                                        encoding="utf-8")
    cli.main(["server", "jobs", "--json", "--library", str(tmp_path)])
    found = json.loads(capsys.readouterr().out)
    assert found["exit_code"] == 0 and found["ran"] == ["alarms", "reports"] and found["jobs"] == "external"
    assert found["server_only"] == ["home address"] and len(ran) == 4
    (periodic.folder() / "alarms.json").write_text("not json", encoding="utf-8")  # damaged: forgotten, and it's due again
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out.startswith("Ran what was due: alarms.\n")


def test_a_job_still_running_from_before_is_left_to_it_and_the_others_run(tmp_path, monkeypatch, capsys):
    """One lock for each job: a nightly copy that takes its time holds up neither the next minute's alarms nor the run."""
    ran: list[str] = []
    jobs = made_up(ran, "alarms", "nightly copy")
    monkeypatch.setattr(periodic, "JOBS", jobs)
    external()
    with periodic.held(jobs[1]) as got:  # a copy still at it
        assert got
        cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out == ("Ran what was due: alarms.\n"
                                       "Still running from before, and left to it: nightly copy.\n")
    assert ran == ["alarms"]


def test_a_long_job_is_started_on_its_own_and_not_waited_for(tmp_path, monkeypatch, capsys):
    """hylly server jobs starts it as hylly internal server-job NAME, a process apart (a session or process group of its
    own), with --here, as it runs on this computer whatever [remote] says; it notes when, so the next minute doesn't
    start it again."""
    started: list[dict] = []
    monkeypatch.setattr(periodic.subprocess, "Popen", lambda args, **kw: started.append({"args": args, **kw}))
    monkeypatch.setattr(periodic, "JOBS", made_up([], "alarms", "nightly copy", long=("nightly copy",)))
    external()
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out == "Ran what was due: alarms.\nStarted on its own, and not waited for: nightly copy.\n"
    (one,) = started
    assert one["args"][1:] == ["-P", "-m", "hylly", "--here", "internal", "server-job", "nightly copy", "--library", str(tmp_path)]
    assert (one.get("start_new_session") or one.get("creationflags")) and one["stdin"] == subprocess.DEVNULL
    if os.name != "nt":  # its temporary files in Hylly's cache: a timer's service takes its /tmp away as it ends
        assert one["env"]["TMPDIR"] == one["env"]["SQLITE_TMPDIR"] == str(periodic.scratch_folder())
        assert periodic.scratch_folder().is_dir() and periodic.scratch_folder().is_relative_to(paths.place("cache"))
    assert kept("nightly copy")["last"] is not None
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert len(started) == 1 and "nightly copy in 1 minute" in capsys.readouterr().out  # noted: not started again


def test_a_catalog_build_after_a_removal_is_started_on_its_own_with_here(tmp_path, monkeypatch):
    """The Server page's removal and Put back start hylly catalog as a long job is started (a process apart, --here, its
    temporary files in Hylly's cache), with no --yes, and get its process, to wait on; one that can't be started is
    said, and the next build takes the change in."""
    started: list[dict] = []
    said: list[str] = []
    monkeypatch.setattr(periodic.subprocess, "Popen", lambda args, **kw: started.append({"args": args, **kw}) or "its process")
    assert periodic.start_catalog(tmp_path, said.append) == "its process"
    (one,) = started
    assert one["args"][1:] == ["-P", "-m", "hylly", "--here", "catalog", "--library", str(tmp_path)]
    assert (one.get("start_new_session") or one.get("creationflags")) and one["stdin"] == subprocess.DEVNULL and said == []
    if os.name != "nt":
        assert one["env"]["TMPDIR"] == str(periodic.scratch_folder())

    def refused(args, **kw):
        raise OSError("no such program")

    monkeypatch.setattr(periodic.subprocess, "Popen", refused)
    assert periodic.start_catalog(tmp_path, said.append) is None
    assert said == ["A catalog build couldn't be started on its own (no such program); the next hylly catalog takes the change in."]


class Build:
    """A catalog build's process as the server waits on it: ends with code, at once."""

    def __init__(self, code: int = 0) -> None:
        self.code = code

    def wait(self, timeout=None) -> int:
        return self.code


def test_the_server_never_leaves_a_removal_without_a_build_one_at_a_time(tmp_path, monkeypatch):
    """A change wanted while a build runs gets a build once that one has ended, never beside it; changes wanted while it
    waits share one; one whose build ended at once as busy (another took the lock just then) is started again; and with
    none running, it starts at once. Nothing waits past BUILD_WAIT: said, and left to the next build."""
    looks, started, said, running = [], [], [], [True]  # running: whether a build holds its lock just now
    monkeypatch.setattr(periodic, "BUILD_LOOK", 0.01)
    monkeypatch.setattr(periodic.runs, "current", lambda job: looks.append(job) or ({"pid": 1} if running[0] else None))
    codes = iter([exits.BUSY, 0])
    monkeypatch.setattr(periodic, "start_catalog", lambda root, log: started.append(root) or Build(next(codes, 0)))
    builds = periodic.Builds()
    assert builds.want(tmp_path, said.append) is False  # one runs: this one waits for it
    assert builds.want(tmp_path, said.append) is False  # and the next change with it
    time.sleep(0.05)
    assert started == []  # never beside the one running
    running[0] = False
    builds.wait(10)
    assert started == [tmp_path, tmp_path] and set(looks) == {"catalog"} and said == []  # the busy one, started again
    assert builds.want(tmp_path, said.append) is True  # none runs: at once
    builds.wait(10)
    assert started == [tmp_path] * 3
    monkeypatch.setattr(periodic, "BUILD_WAIT", 0.05)
    monkeypatch.setattr(periodic.runs, "current", lambda job: {"pid": 1})  # one that never ends
    assert builds.want(tmp_path, said.append) is False
    builds.wait(10)
    assert started == [tmp_path] * 3 and len(said) == 1 and said[0].startswith("A catalog build for the Server page's removals waited")


def test_a_build_refused_again_and_again_is_tried_a_few_times_apart_and_then_said(tmp_path, monkeypatch):
    """A build refused by a lock no look here sees (another computer's, beside the library): tried again BUILD_LOOK
    seconds apart, BUILD_TRIES times in all, then said in words and left to the next build, never in a loop."""
    started, said, sleeps = [], [], []
    monkeypatch.setattr(periodic, "BUILD_LOOK", 0.01)
    monkeypatch.setattr(periodic.runs, "current", lambda job: None)
    monkeypatch.setattr(periodic, "start_catalog", lambda root, log: started.append(root) or Build(exits.BUSY))
    real_sleep = time.sleep
    monkeypatch.setattr(periodic.time, "sleep", lambda s: sleeps.append(s) or real_sleep(s))
    builds = periodic.Builds()
    builds.want(tmp_path, said.append)
    builds.wait(10)
    assert len(started) == periodic.BUILD_TRIES == 3 and sleeps == [0.01] * 2
    assert said == ["A catalog build for the Server page's removals was refused 3 times in a row, as another was running "
                    "(on another computer, perhaps, or one that ended there without letting go of the library): the next "
                    "hylly catalog takes them in, and says why it can't run."]


def test_a_waiter_that_failed_is_replaced_and_each_change_waits_its_own_time(tmp_path, monkeypatch):
    """The thread that waits on the builds, failing, says so and lets the next change start another; and the time a
    change waits is counted from that change, not from when the thread began."""
    started, said, running = [], [], [True]
    monkeypatch.setattr(periodic, "BUILD_LOOK", 0.01)
    monkeypatch.setattr(periodic.runs, "current", lambda job: None)

    def failing(root, log):
        raise RuntimeError("made-up trouble")

    monkeypatch.setattr(periodic, "start_catalog", failing)
    builds = periodic.Builds()
    builds.want(tmp_path, said.append)
    builds.wait(10)
    assert said == ["Waiting on a catalog build for the Server page's removals failed (RuntimeError: made-up trouble): the "
                    "next hylly catalog, or the next removal's build, takes them in."]
    monkeypatch.setattr(periodic, "start_catalog", lambda root, log: started.append(root) or Build(0))
    builds.want(tmp_path, said.append)
    builds.wait(10)
    assert started == [tmp_path] and len(said) == 1  # a new thread for it
    clock = [0.0]
    builds.clock = lambda: clock[0]
    monkeypatch.setattr(periodic.runs, "current", lambda job: {"pid": 1} if running[0] else None)
    builds.want(tmp_path, said.append)  # waits until BUILD_WAIT on this clock
    clock[0] = periodic.BUILD_WAIT - 1
    builds.want(tmp_path, said.append)  # a change later: until twice that, nearly
    clock[0] = periodic.BUILD_WAIT + 1  # past the first one's time
    time.sleep(0.05)
    running[0] = False
    builds.wait(10)
    assert started == [tmp_path] * 2 and len(said) == 1  # built, not given up


def test_a_long_job_s_own_process_runs_it_under_its_lock_and_keeps_what_it_said(tmp_path, monkeypatch, capsys):
    said_by_copy = []

    def copy_tonight(root, log, now=None, said=None):
        said_by_copy.append(dict(said))
        said[f"{root}|not copied"] = "The database wasn't copied: made-up words."
        log("The database wasn't copied: made-up words.")

    monkeypatch.setattr(dbcopy, "copy_tonight", copy_tonight)
    cli.main(["internal", "server-job", "nightly copy", "--library", str(tmp_path)])
    out = capsys.readouterr()
    assert out.out == "The server's job 'nightly copy' ran.\n" and "made-up words" in out.err
    assert kept("nightly copy")["said"] == {f"{tmp_path}|not copied": "The database wasn't copied: made-up words."}
    cli.main(["internal", "server-job", "nightly copy", "--library", str(tmp_path)])
    assert said_by_copy[1] == {f"{tmp_path}|not copied": "The database wasn't copied: made-up words."}  # read back: said once
    monkeypatch.setattr(periodic, "LOCK_WAIT", 0.2)
    with periodic.held(periodic.JOBS[1]) as got:
        assert got
        with pytest.raises(SystemExit) as e:
            cli.main(["internal", "server-job", "nightly copy", "--library", str(tmp_path)])
    assert e.value.code == exits.BUSY and "running already" in capsys.readouterr().err
    for name in ("alarms", "home address", "something else"):  # a quick one, the server's own, or none at all
        with pytest.raises(SystemExit) as e:
            cli.main(["internal", "server-job", name, "--library", str(tmp_path)])
        assert e.value.code == exits.USAGE and "isn't one of the server's long jobs" in capsys.readouterr().err
    assert kept("nightly copy")["ended"]["code"] == 0 and kept("nightly copy")["ended"]["said"].endswith("ran.")
    assert [job.limit for job in periodic.JOBS if job.long] == [2 * periodic.HOUR, periodic.INBOX_LIMIT + 5 * periodic.MINUTE]


def long_one(monkeypatch, run, limit: float | None = None) -> list[tuple]:
    """A made-up long job doing run, and the notes it sends (bell.job_note), as the nightly copy does."""
    monkeypatch.setattr(periodic, "JOBS", (periodic.Job("nightly copy", periodic.HOUR, run, "made up", long=True, limit=limit),))
    notes: list[tuple] = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="", wait=None: notes.append((title, text)) or True)
    return notes


def test_a_long_job_s_failure_or_bug_is_kept_with_its_code_and_said_in_one_line(tmp_path, monkeypatch, capsys):
    """Its own process ends with the job's code, keeps how it ended beside its lock, and says it in one line, as a
    long job's note too (bell.log, and the phone), so a copy that fails doesn't stay silent."""
    def stops(ctx):
        raise exits.Exit(exits.NOT_SET_UP, "a mistake in the settings")

    notes = long_one(monkeypatch, stops)
    with pytest.raises(SystemExit) as e:
        cli.main(["internal", "server-job", "nightly copy", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert e.value.code == exits.NOT_SET_UP and kept("nightly copy")["ended"]["code"] == exits.NOT_SET_UP
    assert said.err.count("a mistake in the settings") == 1 and notes == [("The nightly copy failed", kept("nightly copy")["ended"]["said"])]
    notes = long_one(monkeypatch, lambda ctx: {}["no such key"])
    with pytest.raises(SystemExit) as e:
        cli.main(["internal", "server-job", "nightly copy", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert e.value.code == exits.BUG and kept("nightly copy")["ended"]["code"] == exits.BUG and "KeyError" in said.err
    assert len(notes) == 1 and "a bug in Hylly" in notes[0][1]


def test_a_long_job_that_hangs_past_its_limit_ends_its_own_process_and_lets_go_of_its_lock(tmp_path, monkeypatch, capsys):
    gate = threading.Event()
    notes = long_one(monkeypatch, lambda ctx: gate.wait(30), limit=0.2)
    ended = []
    monkeypatch.setattr(periodic, "_end", lambda code: (ended.append(code), (_ for _ in ()).throw(SystemExit(code)))[1])
    try:
        with pytest.raises(SystemExit):
            cli.main(["internal", "server-job", "nightly copy", "--library", str(tmp_path)])
        with periodic.held(periodic.JOBS[0]) as got:
            assert got  # let go of: the next round can try again
    finally:
        gate.set()
    assert ended == [exits.PARTLY] and kept("nightly copy")["ended"]["code"] == exits.PARTLY
    assert "was still running after" in capsys.readouterr().err and "still running after" in notes[0][1]


def test_a_send_that_fails_after_the_alarms_look_is_said_and_ends_nothing(tmp_path, monkeypatch, capsys):
    def alarms(ctx):
        ctx.watch._send("Users changed", "ann added (owner)", "high", ("Users changed", "1 change"))

    monkeypatch.setattr(periodic, "JOBS", (periodic.Job("alarms", periodic.MINUTE, alarms, "made up"),))
    monkeypatch.setattr(bell.Watch, "push", lambda self, *a: {}["broken"])
    external()
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert said.out == "Ran what was due: alarms.\n" and "Alarms: 'Users changed' couldn't be sent (KeyError" in said.err


def test_a_long_job_started_on_its_own_really_runs_apart(tmp_path, monkeypatch):
    """The process itself, as the timer starts it: it takes the job's lock once hylly server jobs has let go of it, runs
    the job, and says so where hylly server jobs' own words go. Those go to a file here: pytest's capture empties its
    own as it's read, and lost the first letters of the line, so the test never saw it and waited out its minute."""
    monkeypatch.setattr(periodic, "JOBS", tuple(job for job in periodic.JOBS if job.name == "nightly copy"))
    external()
    said = tmp_path / "said.txt"
    with said.open("w", encoding="utf-8") as own:
        monkeypatch.setattr(sys, "stdout", own)
        found = periodic.once(tmp_path, log=lambda line: None)
    assert found["started"] == ["nightly copy"]
    deadline = time.monotonic() + 60
    while "The server's job 'nightly copy' ran." not in said.read_text(encoding="utf-8", errors="replace"):
        assert time.monotonic() < deadline, said.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.2)
    with periodic.held(periodic.JOBS[0], wait=5) as got:
        assert got  # let go of when it ended


def test_hylly_server_jobs_dry_run_says_what_is_due_and_changes_nothing(tmp_path, monkeypatch, capsys):
    ran: list[str] = []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "alarms", "reports", "update check", server_only=("update check",)))
    external()
    before = sorted(p.name for p in paths.config_dir().rglob("*")) if paths.config_dir().exists() else []
    cli.main(["server", "jobs", "--dry-run", "--library", str(tmp_path)])
    assert capsys.readouterr().out == ("Due, and would run now: alarms, reports.\n"
                                       "The server's own, which it runs itself either way: update check.\nNothing was run.\n")
    after = sorted(p.name for p in paths.config_dir().rglob("*")) if paths.config_dir().exists() else []
    assert ran == [] and after == before and not periodic.folder().exists()
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    capsys.readouterr()
    cli.main(["server", "jobs", "-n", "--library", str(tmp_path)])
    said = capsys.readouterr().out
    assert said.startswith("Nothing is due now.\nNot due yet: alarms in 1 minute, reports in 1 minute.\n") and len(ran) == 2


def test_a_once_only_job_a_timer_runs_is_once_and_breaks_nothing(tmp_path, monkeypatch, capsys):
    ran: list[str] = []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "first", every=periodic.ONCE))
    external()
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    cli.main(["server", "jobs", "--json", "--library", str(tmp_path)])
    found = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert ran == ["first"] and found["not_due"] == {"first": None} and found["exit_code"] == 0


def test_with_jobs_internal_hylly_server_jobs_runs_nothing_ends_well_and_says_why_to_a_person(tmp_path, monkeypatch, capsys):
    """The server runs them itself then, and both at once would raise the same alarms twice. A timer left on mustn't
    show as failed every minute, nor fill its log: the words are for a dry run and for someone at the keyboard."""
    ran: list[str] = []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "alarms"))
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert ran == [] and said.out == "" and said.err == "" and not periodic.folder().exists()
    cli.main(["server", "jobs", "--dry-run", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert said.err.startswith("The server runs these jobs itself") and 'jobs = "external"' in said.err and said.out == ""
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert "Nothing was run." in capsys.readouterr().err and ran == []


def test_hylly_server_jobs_ends_partly_when_a_job_failed(tmp_path, monkeypatch, capsys):
    ran: list[str] = []
    monkeypatch.setattr(periodic, "JOBS", made_up(ran, "alarms", "reports", fails=("alarms",)))
    external()
    with pytest.raises(SystemExit) as e:
        cli.main(["server", "jobs", "--library", str(tmp_path)])
    said = capsys.readouterr()
    assert e.value.code == exits.PARTLY and said.out == "Ran what was due: reports.\n" and ran == ["alarms", "reports"]
    assert "'alarms' failed this time (Exit: a mistake in the settings)" in said.err and "alarms failed" in said.err


def test_the_alarms_run_by_the_timer_keep_their_own_memory_and_send_after_letting_it_go(tmp_path, monkeypatch):
    """bell.json is the running server's, held in its memory: hylly server jobs keeps its alarms' memory in
    bell-jobs.json, as long jobs' notes do, read and written while no other program does, and sends an alarm before it
    ends, once bell-jobs.json is free for the other again."""
    from hylly import access

    monkeypatch.setattr(periodic, "JOBS", tuple(job for job in periodic.JOBS if job.name == "alarms"))
    external()
    access.add_user("ann", "correct horse")
    sent, free = [], []

    def push(self, title, *a):
        with bell.one_writer(wait=0):  # not while the alarms' look holds it
            free.append(title)
        sent.append(title)
        return []

    monkeypatch.setattr(bell.Watch, "push", push)
    periodic.once(tmp_path, log=lambda line: None)
    state = json.loads(bell.job_state_file().read_text(encoding="utf-8"))
    assert set(state["users"]) == {"ann"} and not bell.state_file().exists()
    access.add_user("rory", "correct horse")
    (periodic.folder() / "alarms.json").write_text(json.dumps({**kept("alarms"), "last": kept("alarms")["last"] - 120}),
                                                   encoding="utf-8")
    periodic.once(tmp_path, log=lambda line: None)
    assert "Users changed" in sent and free == sent


def test_the_example_timer_runs_hylly_server_jobs_every_minute_within_the_server_s_walls():
    """hylly-jobs.service has the walls of hylly.service, but for its one port, and keeps the network for the phone: what
    the jobs write, the library's state/ and bell.log, is then out of a walled server's reach too. No RequiresMountsFor,
    nor a library that must be there: with its disk away the alarm that it can't be reached must still go out. The
    nightly copy, a process of its own, isn't ended with the run that started it (KillMode=process)."""
    timer = (EXAMPLES / "hylly-jobs.timer").read_text(encoding="utf-8").splitlines()
    service = (EXAMPLES / "hylly-jobs.service").read_text(encoding="utf-8").splitlines()
    assert "OnCalendar=minutely" in timer and "WantedBy=timers.target" in timer
    assert "Type=oneshot" in service and any(line.startswith("ExecStart=") and line.endswith("/hylly server jobs") for line in service)
    assert any('jobs = "external"' in line for line in service if line.startswith("#")) and "KillMode=process" in service
    server = (EXAMPLES / "hylly.service").read_text(encoding="utf-8").splitlines()
    walls = server[server.index("[Service]"):server.index("[Install]")]
    walls = walls[next(i for i, line in enumerate(walls) if line.startswith("# The disk")):]
    logins = "InaccessiblePaths=/home/someone/.local/share/hylly/logins"  # in its place, the lookups' keys alone (below)
    walls = [line.replace("BindPaths=/srv/books", "BindPaths=-/srv/books") for line in walls
             if line and not line.startswith(("#", "SocketBind")) and line != logins]
    assert walls and [line for line in walls if line not in service] == []
    assert not [line for line in service if line.startswith(("RequiresMountsFor", "SocketBind"))]
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK" in service


def test_the_example_jobs_service_can_run_the_inbox_and_sees_no_shop_s_login():
    """The Inbox's pass, which a walled server leaves to this: it writes the Inbox, which the walls must let it, and asks
    the lookup sources, which needs their keys. Of the logins folder it sees those alone, read only; the shops' logins
    aren't even there (a tmpfs over the folder), as in the server's walls."""
    from hylly import lookups, sites

    service = (EXAMPLES / "hylly-jobs.service").read_text(encoding="utf-8").splitlines()
    folder = "/home/someone/.local/share/hylly/logins"  # in the data folder, as a new install has it on Linux
    assert "BindPaths=-/srv/inbox" in service and f"TemporaryFileSystem={folder}:ro" in service
    keys = {sites.SITES[cls.credential][1] for cls in lookups.sources().values() if cls.credential}
    bound = [part.lstrip("-") for line in service if line.startswith("BindReadOnlyPaths=") for part in line.split("=", 1)[1].split()]
    inside = {Path(part).name for part in bound if part.startswith(folder + "/")}
    assert inside == keys == {"metron-token.txt", "comicvine-key.txt", "googlebooks-key.txt"}
    assert not [line for line in service if folder in line and line.startswith(("BindPaths=", "ReadWritePaths="))]


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd's own check")
def test_systemd_itself_accepts_the_example_timer_and_its_service():
    done = subprocess.run(["systemd-analyze", "verify", str(EXAMPLES / "hylly-jobs.service"), str(EXAMPLES / "hylly-jobs.timer")],
                          capture_output=True, text=True)
    for word in ("Unknown key", "Unknown section", "Invalid", "Missing", "Failed to parse"):
        assert word not in done.stderr, done.stderr


# ---- the Inbox: a long job a walled server leaves to the timer -------------------------------------------------------

def inbox_jobs(ran: list[str], idle: bool = True) -> tuple:
    """The alarms, and an Inbox's job as the real one is (long, outside the walls), which has nothing to do if idle."""
    return (periodic.Job("alarms", periodic.MINUTE, lambda ctx: ran.append("alarms"), "made up"),
            periodic.Job("inbox", periodic.MINUTE, lambda ctx: ran.append("inbox"), "made up", long=True,
                         outside_walls=True, idle=lambda root: idle))


def test_a_walled_server_leaves_the_inbox_to_hylly_server_jobs_which_then_runs_that_alone(tmp_path, monkeypatch, capsys):
    """Inside the walls a server sees neither the Inbox's folders nor the lookups' keys: it runs every job of its own but
    that one, and hylly server jobs, with jobs as it comes, runs the Inbox and nothing else on such a computer. Beside a
    server that isn't walled it runs nothing, as before."""
    ran: list[str] = []
    monkeypatch.setattr(periodic, "JOBS", inbox_jobs(ran))
    assert [job.name for job in periodic.Runner(context(tmp_path), walled=True).due(0.0)] == ["alarms"]
    assert [job.name for job in periodic.Runner(context(tmp_path), walled=False).due(0.0)] == ["alarms", "inbox"]
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out == "" and not periodic.folder().exists()  # not walled: the server runs them all
    monkeypatch.setattr(periodic.instance, "system_service", lambda: True)
    cli.main(["server", "jobs", "-n", "--library", str(tmp_path)])
    assert capsys.readouterr().out == ("Due, and would run now: inbox.\nThe server, walled in, runs the others itself, as jobs "
                                       "under [server] isn't \"external\": alarms.\nNothing was run.\n")
    monkeypatch.setattr(periodic.subprocess, "Popen", lambda *a, **k: pytest.fail("an Inbox with nothing to do needs no process"))
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out == "" and ran == [] and kept("inbox")["last"] is not None  # nothing to say, every minute
    started: list[list[str]] = []
    monkeypatch.setattr(periodic, "JOBS", inbox_jobs(ran, idle=False))
    monkeypatch.setattr(periodic.subprocess, "Popen", lambda args, **kw: started.append(args))
    (periodic.folder() / "inbox.json").unlink()
    cli.main(["server", "jobs", "--library", str(tmp_path)])
    assert capsys.readouterr().out == "Started on its own, and not waited for: inbox.\n"
    assert started[0][-4:] == ["server-job", "inbox", "--library", str(tmp_path)]


def test_the_real_inbox_job_starts_no_pass_while_there_is_nothing_for_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(periodic, "JOBS", tuple(job for job in periodic.JOBS if job.name == "inbox"))
    monkeypatch.setattr(periodic.subprocess, "run", lambda *a, **k: pytest.fail("no Inbox: no pass"))
    monkeypatch.setattr(periodic.subprocess, "Popen", lambda *a, **k: pytest.fail("no Inbox: no process"))
    external()
    found = periodic.once(tmp_path, log=lambda line: None)
    assert found["idle"] == ["inbox"] and found["ran"] == found["started"] == []
    runner = periodic.Runner(context(tmp_path))
    assert runner.run_due(now=0.0, wait=5) == ["inbox"] and not runner.failed


def test_the_inbox_job_runs_a_pass_in_a_process_of_its_own_and_says_one_that_ends_badly_once(tmp_path, monkeypatch):
    """hylly internal inbox, waited for: what it says goes into the server's log; one that fails is the job's failure
    once, not every minute, until a pass goes well."""
    calls: list[list[str]] = []
    answers = [subprocess.CompletedProcess([], 0, "Inbox: 1 book added from the Inbox.\n", ""),
               subprocess.CompletedProcess([], exits.LOCAL, "", "The library's disk isn't taking copies now.\n"),
               subprocess.CompletedProcess([], exits.LOCAL, "", "The library's disk isn't taking copies now.\n"),
               subprocess.CompletedProcess([], 0, "", ""),
               subprocess.CompletedProcess([], exits.LOCAL, "", "The library's disk isn't taking copies now.\n")]
    monkeypatch.setattr(periodic, "_no_inbox_pass", lambda root: False)
    monkeypatch.setattr(periodic.subprocess, "run", lambda args, **kw: (calls.append(args), answers.pop(0))[1])
    said: list[str] = []
    ctx = context(tmp_path, said)
    periodic._inbox(ctx)
    assert calls[0][1:] == ["-P", "-m", "hylly", "--here", "internal", "inbox", "--library", str(tmp_path)]
    assert said == ["Inbox: 1 book added from the Inbox."]
    with pytest.raises(exits.Exit) as e:
        periodic._inbox(ctx)
    assert e.value.code == exits.LOCAL and "isn't taking copies" in e.value.message
    periodic._inbox(ctx)  # the same again: said already
    periodic._inbox(ctx)  # a pass that went well
    with pytest.raises(exits.Exit):
        periodic._inbox(ctx)  # and so said again


def test_nothing_running_the_inbox_is_said_once_in_the_server_s_log(tmp_path, monkeypatch):
    """A walled server leaves the Inbox to a timer: when that hasn't run it for ten minutes, the server's log says so once,
    with what to install, but not at its first look, as a timer started with the computer may not have run yet."""
    paths.settings_file().write_text(f"[inbox]\nfolder = '{tmp_path / 'inbox'}'\n", encoding="utf-8")
    settings.reload()
    monkeypatch.setattr(periodic.instance, "in_system_service", lambda: True)
    said: list[str] = []
    ctx = context(tmp_path, said)
    periodic._inbox_watch(ctx)
    assert said == []
    periodic._inbox_watch(ctx)
    periodic._inbox_watch(ctx)
    assert len(said) == 1 and f"Nothing runs the Inbox ({tmp_path / 'inbox'})" in said[0] and "hylly-jobs.timer" in said[0]
    periodic._keep(next(job for job in periodic.JOBS if job.name == "inbox"), {"last": time.time(), "said": {}})
    periodic._inbox_watch(ctx)
    assert "inbox|nobody" not in ctx.said and len(said) == 1
    monkeypatch.setattr(periodic.instance, "in_system_service", lambda: False)  # one that isn't walled runs it itself
    assert periodic.nobody_runs_the_inbox(False, now=time.time() + 3600) is None
