"""The library server's periodic jobs, in one place: each one's name, how often it's due and what it does (JOBS), the
server's rounds that run those that are due (start, Runner), and hylly server jobs, which runs them from a timer
instead (once). Not to be mixed up with hylly jobs, which is about the long runs of commands (runs.py).

- The server runs what's due as it starts and then once a minute, in a thread of its own (start). jobs = "external"
  under [server] leaves the jobs that can run elsewhere to hylly server jobs, which runs what's due and ends: for a
  systemd timer (examples/hylly-jobs.timer), cron or Task Scheduler to start every minute. The server reads the setting
  each round, so a change needs no restart. The jobs that keep up what only the running server holds in its memory stay
  with it whatever jobs says (Job.server_only).
- A job is one entry of JOBS: its name, how often it's due, its function, which is given what it needs (Context), a
  few words on what it does, and its kind: the server's own, or a long one.
- One run of each job at a time, and none holds up another. In the server each runs in a thread of its own, and one
  still running when it's due again isn't started twice. Each job that may run elsewhere has a lock of its own
  (server-jobs/<job>.lock in the state folder), which the server takes too, so a switch of jobs mid-round can't run
  one twice. hylly server jobs runs the quick ones (the alarms) itself, and starts a long one (the nightly copy) as a
  process of its own, which it doesn't wait for (hylly internal server-job NAME): the alarms go on every minute while a
  copy takes its time, and a copy isn't cut short when the run that started it ends. One that fails is said in one
  line and tried again when it's next due: it never stops the others, nor the server.
- When each last ran: in the server's memory; for hylly server jobs, each run of which is a process of its own, in
  server-jobs/<job>.json beside its lock, with what the job has said once so far, read after its lock is taken and
  written back under it. What a job keeps of its own stays where it always was: tonight's copy is its day's folder in
  state/db-backup; the alarms' memory is bell.json, and for hylly server jobs bell-jobs.json, as a running server keeps
  bell.json in its memory and would write over what another program put there. Long jobs' notes keep bell-jobs.json
  too, so it's read and written by one program at a time (bell.one_writer). The day's summary goes out once whichever
  sends it (bell.Watch.maybe_summary).
- The Inbox ([inbox] in the settings, folders.py) is a long job of a kind of its own: it needs what a server
  walled in by examples/hylly.service can't see, the Inbox's folders and the lookups' keys (Job.outside_walls). So a
  server that isn't walled starts each pass itself, and a walled one leaves it to hylly server jobs on its timer,
  outside its walls, which then runs that and nothing else while jobs isn't "external". A pass is a process of its own,
  hylly internal inbox, as copying, the lookups and the catalog are no part of the server's; one is started only when
  there's anything for it to do (folders.waiting), and with nothing to run it, the server's log says so once (the job
  "inbox watch"), as hylly doctor does.

"The server is down" is no job: a server can't say that it isn't there. That's for systemd's OnFailure= or a
container's health check of /healthz (docs/ways-to-run-it.md).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import access, bell, db, dbcopy, folders, instance, paths, runs, settings, updates
from .exits import BUG, BUSY, PARTLY, USAGE, Exit, bug
from .storage import atomic_write
from .words import time_words

MINUTE, HOUR = 60.0, 3600.0
ONCE = float("inf")  # as the server starts, and not again while it runs, whatever the clock does
EARLY = 30.0  # seconds early that still count as due: a timer that fires a moment early mustn't make a job skip a round
LOCK_WAIT = 10.0  # seconds a long job, started on its own, waits for the lock hylly server jobs has just let go of
INBOX_LIMIT = HOUR  # an Inbox's pass, a process of its own, is ended after this, and the next goes on from there
NOBODY_RUNS = 10 * MINUTE  # an Inbox left to a timer that hasn't run it for this long: nothing runs it, which is said


@dataclass
class Context:
    """What a job is given: the library; where its words go (the server's log, or what hylly server jobs says on standard
    error); the alarms, for the jobs that raise them (the server's own, or hylly server jobs' own; None in a long job's
    own process); and what the job has said so far, by what about, so that each thing is said once (dbcopy)."""

    root: Path
    log: Callable[[str], None]
    watch: bell.Watch | None = None
    said: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    """One of the server's periodic jobs: its name, how often it's due (in seconds; ONCE: as the server starts), what it
    does (run, and in words), and its kind. server_only: only the server may run it, as it keeps up what the running
    server holds in its memory; while_external too: the server's own part of a job that, with jobs = "external", runs
    elsewhere, and so only then. long: hylly server jobs starts it as a process of its own, and doesn't wait for it;
    limit: the seconds that process gives it, after which it ends itself (a disk that's going can hang it).
    outside_walls: it needs what a server walled in can't see (the Inbox's folders, the lookups' keys), so a walled
    server leaves it to hylly server jobs, which then runs it whatever jobs says. idle: given the library, whether it has
    nothing to do now, looked at cheaply, so that no process is started for it (the Inbox, empty)."""

    name: str
    every: float
    run: Callable[[Context], None]
    what: str
    server_only: bool = False
    while_external: bool = False
    long: bool = False
    limit: float | None = None
    outside_walls: bool = False
    idle: Callable[[Path], bool] | None = None

    @property
    def slug(self) -> str:
        """Its name as a file's: nightly-copy."""
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


def _alarms(ctx: Context) -> None:
    ctx.watch.tick()


def _held_back(ctx: Context) -> None:
    ctx.watch.tell_of_held()


def _home_address(ctx: Context) -> None:
    access.look_at_adapters()


def _nightly_copy(ctx: Context) -> None:
    dbcopy.copy_tonight(ctx.root, ctx.log, said=ctx.said)


def _update_check(ctx: Context) -> None:
    updates.say(ctx.log)


def _no_inbox_pass(root: Path) -> bool:
    """Nothing for an Inbox's pass to do (folders.waiting). What can't be told, a mistake in the settings say, counts
    as something: the pass says what it is."""
    try:
        return not folders.waiting(root)
    except (SystemExit, Exception):  # noqa: BLE001 - the pass, started all the same, says what's wrong
        return False


def _windowless() -> dict:
    """What keeps a process started from here from showing a window, on Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _inbox(ctx: Context) -> None:
    """A pass over the Inboxes, when there's anything for one to do: hylly internal inbox, a process of its own, waited
    for INBOX_LIMIT at most and then ended. What the pass did goes into the log as it says it; a pass that ends badly is
    said once, raised as the job's failure, until one goes well: it would otherwise be said every minute."""
    if _no_inbox_pass(ctx.root):
        return
    try:
        done = subprocess.run([sys.executable, "-P", "-m", "hylly", "--here", "internal", "inbox", "--library", str(ctx.root)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=INBOX_LIMIT, env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
                              **_windowless())
    except subprocess.TimeoutExpired:
        code, words = PARTLY, (f"The Inbox's pass was still running after {INBOX_LIMIT / HOUR:g} hour, and was ended: the next "
                               "one goes on from there.")
    else:
        for line in done.stdout.splitlines():
            if line.strip():
                ctx.log(line)
        if done.returncode == 0:
            ctx.said.pop("inbox", None)
            return
        said = [line for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        code, words = done.returncode, said[-1] if said else f"It ended with exit code {done.returncode}."
    if ctx.said.get("inbox") != words:
        ctx.said["inbox"] = words
        raise Exit(code if code and code != USAGE else PARTLY, f"The Inbox's pass: {words}")


def inbox_last() -> float | None:
    """When hylly server jobs last ran the Inbox's job on its timer (server-jobs/inbox.json); None if it never has."""
    return _kept(next(job for job in JOBS if job.name == "inbox"))["last"]


def nobody_runs_the_inbox(walled: bool, now: float | None = None) -> tuple[str, str] | None:
    """Why nothing runs the Inbox, and what to install, in words; or None when something does, or there's no Inbox: one
    left to a timer (by a walled server, or with jobs = "external") that hasn't run it for NOBODY_RUNS. A server that
    isn't walled, with jobs as it comes, runs it itself."""
    found = [inbox.folder for inbox in folders.inboxes()]
    if not found or not (walled or external()):
        return None
    last, now = inbox_last(), time.time() if now is None else now
    if last is not None and 0 <= now - last < NOBODY_RUNS:
        return None
    why = ("this server is walled in (examples/hylly.service), and can see neither the Inbox's folders nor the lookups' "
           "keys, so hylly server jobs runs it from a timer" if walled else
           'jobs = "external" under [server] leaves it to hylly server jobs')
    since = "it never has" if last is None else f"it last did {time_words(max(now - last, 60))} ago"
    return (f"Nothing runs the Inbox ({', '.join(found)}): {why}, and {since}.",
            "Install examples/hylly-jobs.service and hylly-jobs.timer, filled in for this computer as they say (the help's "
            "The Inbox), then: sudo systemctl enable --now hylly-jobs.timer.")


def _inbox_watch(ctx: Context) -> None:
    """The server's own: with an Inbox left to a timer, said once in its log when nothing runs it (nobody_runs_the_inbox),
    and once again if it runs and then stops. Its first look, as the server starts, only notes that it has begun: a
    timer started with the computer may not have run yet."""
    if "inbox|watching" not in ctx.said:
        ctx.said["inbox|watching"] = "since the server started"
        return
    nobody = nobody_runs_the_inbox(instance.in_system_service())
    if nobody is None:
        ctx.said.pop("inbox|nobody", None)
    elif "inbox|nobody" not in ctx.said:
        ctx.said["inbox|nobody"] = " ".join(nobody)
        ctx.log(" ".join(nobody))


JOBS: tuple[Job, ...] = (
    Job("alarms", MINUTE, _alarms, "users changed, the library out of reach, alarms held back, the daily summary (bell.py)"),
    Job("nightly copy", HOUR, _nightly_copy,
        f"the database and the settings copied to the library's disk, once it's past {db.BACKUP_FROM_HOUR}:00 (dbcopy.py)",
        long=True, limit=2 * HOUR),
    Job("inbox", MINUTE, _inbox, "the books in the Inbox ([inbox] in the settings) taken into the library, each into "
        "the folder of yours its kind belongs in, checked, looked up and catalogued, then removed from there (hylly "
        "internal inbox)", long=True,
        limit=INBOX_LIMIT + 5 * MINUTE, outside_walls=True, idle=_no_inbox_pass),
    Job("alarms held back", MINUTE, _held_back, "the server's own alarms held back over alarms_per_hour, told when there's "
        "room again, while the alarms job runs elsewhere (where it runs, it tells of them itself)", server_only=True,
        while_external=True),
    Job("home address", 5 * MINUTE, _home_address, "this computer's address on the home network, for the addresses the "
        "pages show (access.lan_address)", server_only=True),
    Job("update check", ONCE, _update_check, "what the server runs on that's older than it should be, in its log "
        "(updates.py)", server_only=True),
    Job("inbox watch", NOBODY_RUNS, _inbox_watch, "whether anything runs the Inbox while the server leaves it to a timer: "
        "said once in its log when nothing has for ten minutes, with what to install", server_only=True),
)


def is_due(job: Job, last: float | None, now: float) -> bool:
    """Never run yet; or it ran every seconds ago, less EARLY; or the clock has been put back since. A ONCE job never
    again, a clock put back or not."""
    if last is None:
        return True
    return job.every != ONCE and (now < last or now - last >= job.every - EARLY)


def external() -> bool:
    """jobs = "external" under [server]: the server leaves to hylly server jobs what may run elsewhere."""
    return settings.get("server")["jobs"] == "external"


def mine(server: bool, outside: bool = False, jobs: tuple[Job, ...] | None = None, walled: bool = False) -> list[Job]:
    """The jobs that one runs, of JOBS unless others are given. The server: all of them as it comes, but those that
    stand in for a job run elsewhere (while_external), and for a server walled in (walled) those that need what's
    outside its walls (outside_walls); with jobs = "external" (outside), only its own. hylly server jobs: those that may
    run elsewhere."""
    jobs = JOBS if jobs is None else jobs
    if not server:
        return [job for job in jobs if not job.server_only]
    if outside:
        return [job for job in jobs if job.server_only]
    return [job for job in jobs if not job.while_external and not (walled and job.outside_walls)]


def folder() -> Path:
    """Where each job that may run elsewhere keeps its lock, and for hylly server jobs when it last ran."""
    return paths.place("state") / "server-jobs"


@contextlib.contextmanager
def held(job: Job, wait: float = 0.0) -> Iterator[bool]:
    """The job's own lock, an operating system's, which goes with a program that ends however it ends: whether it was
    got, within wait seconds."""
    path = folder() / f"{job.slug}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        until = time.monotonic() + wait
        got = instance._lock(fh)
        while not got and time.monotonic() < until:
            threading.Event().wait(0.1)
            got = instance._lock(fh)
        try:
            yield got
        finally:
            if got:
                instance._unlock(fh)


class Runner:
    """The server's: runs what's due, each job in a thread of its own, and one that may run elsewhere under its lock.
    last: when each job last started, by its name. ended: set, and the rounds end (start)."""

    def __init__(self, ctx: Context, server: bool = True, jobs: tuple[Job, ...] | None = None,
                 last: dict[str, float] | None = None, walled: bool | None = None) -> None:
        self.ctx, self.server, self.jobs = ctx, server, JOBS if jobs is None else jobs
        self.walled = instance.in_system_service() if walled is None else walled  # the server inside the walls
        self.last: dict[str, float] = dict(last or {})
        self.failed: list[str] = []
        self._running: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self.ended = threading.Event()
        self.rounds: threading.Thread | None = None  # the server's rounds, once started

    def due(self, now: float, outside: bool = False) -> list[Job]:
        return [job for job in mine(self.server, outside, self.jobs, self.walled) if is_due(job, self.last.get(job.name), now)]

    def run_due(self, now: float | None = None, outside: bool = False, wait: float | None = None) -> list[str]:
        """Starts each job that's due and isn't still running from before, each in a thread of its own, and notes when
        it started; with wait, waits that many seconds at most for them all to end. The names of those started."""
        now = time.time() if now is None else now
        started = []
        for job in self.due(now, outside):
            with self._lock:
                if job.name in self._running and self._running[job.name].is_alive():
                    continue  # still at it from before: never two of one job at once
                self.last[job.name] = now
                thread = threading.Thread(target=db.carried(self._one), args=(job,), daemon=True, name=f"job {job.name}")
                self._running[job.name] = thread
            thread.start()
            started.append(job.name)
        if wait is not None:
            until = time.monotonic() + wait
            for name in started:
                self._running[name].join(max(0.0, until - time.monotonic()))
        return started

    def still_running(self) -> list[str]:
        return [name for name, thread in self._running.items() if thread.is_alive()]

    def _one(self, job: Job) -> None:
        with contextlib.nullcontext(True) if job.server_only else held(job) as got:
            if not got:
                return  # hylly server jobs' run of it, still at it after a switch of jobs: tried again when next due
            try:
                job.run(self.ctx)
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a thread ends without a word on SystemExit, Hylly's Exit too
                with self._lock:
                    self.failed.append(job.name)
                self.ctx.log(failed(job, e))


def failed(job: Job, e: BaseException) -> str:
    return f"The server's job {job.name!r} failed this time ({type(e).__name__}: {e}); it's tried again when it's next due."


def start(ctx: Context, every: float = MINUTE) -> Runner:
    """The server's own rounds: what's due now, and again every minute, until runner.ended is set (the server does as it
    stops, and a test when it's done). The wait between rounds is on that Event, never time.sleep: a test that stubs
    time.sleep can't make a loop left running spin. Whether jobs is "external" is read each round, as a running server
    reads its settings again when they change; a mistake made in them keeps what was read before (settings.get). Said in
    the log when it's "external", and when it changes."""
    runner, was = Runner(ctx), [False]

    def rounds() -> None:
        while not runner.ended.is_set():
            try:
                outside = external()
                if outside != was[0]:
                    was[0] = outside
                    ctx.log(LEFT_TO_A_TIMER if outside else "The server runs all its periodic jobs itself again, as jobs "
                                                            "under [server] is no longer \"external\".")
                runner.run_due(outside=outside)
            except (SystemExit, Exception) as e:  # noqa: BLE001 - each job's own trouble is caught in it: this is the rest
                ctx.log(f"The server's periodic jobs: {type(e).__name__}: {e}")
            runner.ended.wait(every)

    runner.rounds = threading.Thread(target=rounds, daemon=True, name="jobs")
    runner.rounds.start()
    return runner


LEFT_TO_A_TIMER = ("The server leaves its periodic jobs to hylly server jobs, as jobs = \"external\" under [server]: "
                   f"{', '.join(job.name for job in mine(server=False))}. Something must run it every minute, such as "
                   "examples/hylly-jobs.timer. The server's own it runs itself: "
                   f"{', '.join(job.name for job in mine(server=True, outside=True))}.")
INTERNAL = ("The server runs these jobs itself, as jobs under [server] isn't \"external\". hylly server jobs is for when a "
            "timer runs them instead: jobs = \"external\" under [server] in {settings}, then a timer that runs hylly server "
            "jobs every minute (examples/hylly-jobs.timer).")


# ---- hylly server jobs, and the long jobs it starts -------------------------------------------------------------------

def _kept(job: Job) -> dict:
    """What hylly server jobs keeps of a job from one run to the next, beside its lock: when it last ran, and what it has
    said once. What isn't right in the file is forgotten: the job is then due, as on the first run."""
    try:
        saved = json.loads((folder() / f"{job.slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    last, said, ended = saved.get("last"), saved.get("said"), saved.get("ended")
    kept = {"last": float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else None,
            "said": {k: w for k, w in said.items() if isinstance(k, str) and isinstance(w, str)} if isinstance(said, dict) else {}}
    if isinstance(ended, dict):  # how a long one's own process last ended (run_apart)
        kept["ended"] = ended
    return kept


def _keep(job: Job, kept: dict) -> None:
    atomic_write(folder() / f"{job.slug}.json", json.dumps(kept, indent=1), durable=False)


def _left(job: Job, last: float, now: float) -> int | None:
    """Seconds until it's due again; None for a ONCE job, which isn't."""
    return None if job.every == ONCE else round(last + job.every - EARLY - now)


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)  # what the jobs say on the way: standard output has the result


def _run_here(job: Job, root: Path, log: Callable[[str], None], said: dict[str, str]) -> bool:
    """A quick job, run by hylly server jobs itself, with alarms of its own: bell-jobs.json is read, changed and written
    while no other program does (bell.one_writer), and what the job sends goes out after, so a slow phone service holds
    up nobody. False if it failed, which is said."""
    going: list[tuple] = []
    watch = None
    try:
        with bell.one_writer():
            watch = bell.Watch(log=log, state=bell.job_state_file(), library=root)
            watch._send = lambda *note: going.append(note)
            job.run(Context(root, log, watch, said))
        ok = True
    except (SystemExit, Exception) as e:  # noqa: BLE001 - one job's trouble, said: the next goes on
        log(failed(job, e))
        ok = False
    for note in going:
        try:
            watch.push(*note)
        except Exception as e:  # noqa: BLE001 - a note that couldn't go is said; it never ends the run as a bug
            log(f"Alarms: {note[0]!r} couldn't be sent ({type(e).__name__}: {e}); it is in bell.log.")
    return ok


def _handle(stream) -> int:
    """A stream's file, for a process started from here to write to (given by hand: on Windows a process of its own gets
    none otherwise); nowhere, for a stream with no file under it."""
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def scratch_folder() -> Path:
    """Where a job started on its own (_start_apart) keeps its temporary files: tmp in Hylly's cache folder, which its
    walls let it write, and which is there as long as the job runs."""
    return paths.place("cache") / "tmp"


def _start_apart(job: Job, root: Path, log: Callable[[str], None]) -> bool:
    """A long job, started as a process of its own, apart from this one (a session or process group of its own), which
    doesn't wait for it: hylly internal server-job NAME, with --here, as it runs on this computer whatever [remote] says.
    What it says goes where this one's does: the journal of a timer's service, cron's mail. Its temporary files, Python's
    and SQLite's (a statement's journal, a sort too big for memory), go into Hylly's cache (scratch_folder), not /tmp:
    a timer's service with PrivateTmp= (examples/hylly-jobs.service) has its /tmp taken away as it ends, which is
    before the job started from it does, and the job's database would then be refused ("unable to open database
    file"). False if it couldn't be started, which is said."""
    return _apart(["internal", "server-job", job.name, "--library", str(root)], log, f"The server's job {job.name!r}",
                  "it's tried again when it's next due") is not False


def start_catalog(root: Path, log: Callable[[str], None]):
    """A catalog build, hylly catalog with --here, started as a long job is (_start_apart), for Builds: after the Server
    page has removed files from the library or put one back, so that a card leaves the library page, or comes back,
    within the build's time. With no --yes: a build that would keep far fewer titles keeps none, as hylly catalog asks
    first. Its process (subprocess.Popen); None if it couldn't be started, which is said."""
    found = _apart(["catalog", "--library", str(root)], log, "A catalog build", "the next hylly catalog takes the change in")
    return None if found is False else found


BUILD_LOOK = 5.0  # seconds between the server's looks at a catalog build running, to start one of its own after it
BUILD_WAIT = 2 * HOUR  # how long the server waits on the builds for a change of its own at most: then the next build
BUILD_TRIES = 3  # builds ended at once as busy in a row, after which the server leaves its change to the next build


class Builds:
    """The catalog builds the server wants after the Server page removed files or put one back (start_catalog), so that
    no such change is left without one: one at a time, as the build's own lock has it (runs.current, this computer's).
    A change made while a build runs gets a build after that one ends, as that one may have read the library before the
    change, and changes made while one waits share it. A build that ended at once as busy (another took the lock just
    then, or the lock beside the library, which no look here sees: another computer building, or a run ended elsewhere
    without letting go) is tried again BUILD_LOOK seconds later, BUILD_TRIES times at most, after which the server says
    so and leaves the change to the next build. A thread of the server's own waits, looking every BUILD_LOOK seconds and
    waiting on the build it started, BUILD_WAIT at most from the latest change, after which it says so too; one that
    fails is said, and the next change starts another. A server stopped or started again before then, which ends that
    thread, leaves the change to the next build as well: hylly catalog, or the next removal's or put back's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wanted: Path | None = None
        self._thread: threading.Thread | None = None
        self._until = 0.0
        self.clock = time.monotonic  # what the waiting goes by (a test's own clock)

    def want(self, root: Path, log: Callable[[str], None]) -> bool:
        """A build of root, as soon as none runs, waited for BUILD_WAIT from now at most: True when none runs now, so it
        starts at once; False when it waits for the one running."""
        free = runs.current("catalog") is None
        with self._lock:
            self._wanted, self._until = root, self.clock() + BUILD_WAIT
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, args=(log,), name="catalog builds", daemon=True)
                self._thread.start()
        return free

    def wait(self, timeout: float | None = None) -> None:
        """Until the thread that waits on the builds has nothing left to do, or timeout seconds."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _done(self) -> None:
        """This thread's end, the change it had (if any) left to the next build; called holding the lock, so that a
        change wanted at that very moment starts a thread of its own (want)."""
        self._wanted = None
        if self._thread is threading.current_thread():
            self._thread = None

    def _run(self, log: Callable[[str], None]) -> None:
        try:
            self._waiting(log)
        except Exception as e:  # noqa: BLE001 - said, never raised: the next change starts another thread
            log(f"Waiting on a catalog build for the Server page's removals failed ({type(e).__name__}: {e}): the next "
                "hylly catalog, or the next removal's build, takes them in.")
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _waiting(self, log: Callable[[str], None]) -> None:
        busy = 0
        while True:
            with self._lock:
                root = self._wanted
                if root is None:
                    return self._done()
                if self.clock() > self._until:
                    self._done()
                    return log(f"A catalog build for the Server page's removals waited {time_words(BUILD_WAIT)} for "
                               "another to end, and wasn't started: the next hylly catalog takes them in.")
            if runs.current("catalog") is not None:  # one runs: this one after it
                time.sleep(BUILD_LOOK)
                continue
            with self._lock:
                self._wanted = None
            process = start_catalog(root, log)
            if process is None:  # said: the next hylly catalog takes it in
                with self._lock:
                    return self._done()
            try:
                code = process.wait(timeout=max(1.0, self._until - self.clock()))
            except subprocess.TimeoutExpired:  # it goes on by itself
                code = None
            if code != BUSY:
                busy = 0
                continue
            busy += 1
            if busy >= BUILD_TRIES:
                with self._lock:
                    self._done()
                return log(f"A catalog build for the Server page's removals was refused {busy} times in a row, as "
                           "another was running (on another computer, perhaps, or one that ended there without letting "
                           "go of the library): the next hylly catalog takes them in, and says why it can't run.")
            with self._lock:  # again, a little later
                self._wanted = self._wanted or root
            time.sleep(BUILD_LOOK)


def _apart(args: list[str], log: Callable[[str], None], who: str, then: str):
    """hylly --here ARGS as a process of its own, apart from this one (a session or process group of its own), not waited
    for, its temporary files in Hylly's cache (scratch_folder); who: what it is, and then: what follows if it couldn't
    be started, in the words said of it. Its process; False if it couldn't be started."""
    detach = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if os.name != "nt":
        try:
            scratch = scratch_folder()
            scratch.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = env["SQLITE_TMPDIR"] = str(scratch)
        except OSError as e:  # the system's own, then, as before
            log(f"{who} keeps its temporary files where the system does: {scratch} couldn't be made ({e}).")
    try:
        process = subprocess.Popen([sys.executable, "-P", "-m", "hylly", "--here", *args],
                                   stdin=subprocess.DEVNULL, stdout=_handle(sys.stdout), stderr=_handle(sys.stderr),
                                   env=env, **detach)
    except OSError as e:
        log(f"{who} couldn't be started on its own ({e}); {then}.")
        return False
    return process


def once(root: Path, dry_run: bool = False, log: Callable[[str], None] = _say, now: float | None = None) -> dict:
    """hylly server jobs: the jobs the server leaves to it, with jobs = "external" under [server], each run if it's due;
    with dry_run, only which are due and would run (no lock taken, nothing written). With any other jobs setting it runs
    nothing, as the server runs them itself, and both at once would raise the same alarms twice; but for those a server
    walled in can't run (outside_walls), which it runs then on a computer whose server is walled in. Each job under its
    own lock: one still running from before is left to it. A quick one runs here; a long one is started on its own
    (_start_apart), unless it has nothing to do (idle), which counts as run, when it was due. What it found: the
    setting, whether this computer's server is walled in, which jobs were due, ran here, had nothing to do, were started
    on their own, failed, or were still running; when the others are due (in seconds from now; None: not again); and
    those the server runs itself."""
    now = time.time() if now is None else now
    mode, walled = settings.get("server")["jobs"], instance.system_service()
    found: dict = {"jobs": mode, "walled": walled, "due": [], "ran": [], "idle": [], "started": [], "failed": [], "still_running": [],
                   "not_due": {}, "server_only": [job.name for job in mine(server=True, outside=mode == "external", walled=walled)]}
    if mode == "external":
        todo = mine(server=False)
    elif walled:  # what the walls keep from the server, and nothing else
        todo = [job for job in mine(server=False) if job.outside_walls]
    else:  # the server's: nothing runs here
        return found
    for job in todo:
        if dry_run:
            kept = _kept(job)
            if is_due(job, kept["last"], now):
                found["due"].append(job.name)
            else:
                found["not_due"][job.name] = _left(job, kept["last"], now)
            continue
        with held(job) as got:
            if not got:
                found["still_running"].append(job.name)
                continue
            kept = _kept(job)  # once the lock is ours: what the last run of it wrote
            if not is_due(job, kept["last"], now):
                found["not_due"][job.name] = _left(job, kept["last"], now)
                continue
            found["due"].append(job.name)
            kept["last"] = now
            _keep(job, kept)  # before it runs: one that ends badly, or a long one on its way, isn't started again till due
            if not job.long:
                found["ran" if _run_here(job, root, log, kept["said"]) else "failed"].append(job.name)
                _keep(job, kept)  # what it said once, under the same lock
                continue
            if job.idle is not None and job.idle(root):  # nothing for it to do: no process started for it
                found["idle"].append(job.name)
                continue
        found["started" if _start_apart(job, root, log) else "failed"].append(job.name)  # its lock let go: it takes it
    return found


def _end(code: int) -> None:
    """This process, ended now, its lock with it, whatever a job hung in a thread of it is doing (run_apart)."""
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)


def run_apart(name: str, root: Path, dry_run: bool = False, log: Callable[[str], None] = _say) -> str:
    """hylly internal server-job NAME: a long job of the server's, which hylly server jobs started on its own. It takes
    the job's lock (waiting LOCK_WAIT seconds for the one that started it to let go), reads what the job said before,
    runs it, and writes that back, under the lock; a second one at once ends at once (BUSY).

    The job runs in a thread, given its limit: one still running then (a disk that's going) is said, and this process
    ends itself, its lock with it, so the next round can try again. How it ended (when, its exit code, in words) is kept
    in its state file beside its lock; a failure, a bug or the limit is also said in one line in bell.log and as a long
    job's note (bell.job_note), as a failing copy mustn't stay silent. What it says on the way, said."""
    job = next((j for j in mine(server=False) if j.name == name and j.long), None)
    if job is None:
        raise Exit(USAGE, f"{name!r} isn't one of the server's long jobs, which hylly server jobs starts on their own: "
                          f"{', '.join(j.name for j in mine(server=False) if j.long)}. Nothing was run.")
    if dry_run:
        return f"It would run the server's job {name!r} now, as a process of its own. Nothing was run."
    with held(job, wait=LOCK_WAIT) as got:
        if not got:
            raise Exit(BUSY, f"The server's job {name!r} is running already: this one leaves it to that, and ends.")
        kept, trouble, began = _kept(job), [], time.monotonic()

        def run() -> None:
            try:
                job.run(Context(root, log, None, kept["said"]))
            except (SystemExit, Exception) as e:  # noqa: BLE001 - told below, by its kind
                trouble.append(e)

        thread = threading.Thread(target=db.carried(run), daemon=True, name=f"job {job.name}")
        thread.start()
        thread.join(job.limit)
        hung = thread.is_alive()
        e = trouble[0] if trouble else None
        if hung:
            code, words = PARTLY, (f"The server's job {name!r} was still running after {job.limit / HOUR:g} hours, and was "
                                   "ended: a disk that's going can hang it. It's tried again when it's next due.")
        elif isinstance(e, SystemExit):
            code, words = e.code if isinstance(e.code, int) and e.code else PARTLY, failed(job, e)
        elif e is not None:
            code, words = BUG, f"The server's job {name!r} stopped by a bug in Hylly ({type(e).__name__}): the details are above."
        else:
            code, words = 0, f"The server's job {name!r} ran."
        kept["ended"] = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                         "seconds": round(time.monotonic() - began), "code": code, "said": words}
        _keep(job, kept)
    if code:
        if e is not None and not isinstance(e, SystemExit):
            bug(e)  # its traceback, scrubbed, where this process's words go
        log(words)
        with contextlib.suppress(Exception):
            bell.job_note(f"The {name} failed", words, f"The server's {name} didn't run to its end. Its log says more.")
    if hung:
        _end(code)  # the hung thread can't be stopped: the process goes, and its lock with it
    if code:
        raise Exit(code)  # its words said above
    return words
