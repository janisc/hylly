"""One run at a time of each job that talks to a site or writes the library, on this computer and on the library.

Two lookups side by side would ask the sources twice as fast as their limits allow: each run keeps its own
count of requests (lookups.Budget), and both would ask about the same titles. Two downloads from one shop would
fetch the same files into the same places. So each job holds an OS lock on <job>.lock in the state folder
while it runs, as the library server does (instance.py): the operating system drops it when the process ends,
however it ends, so a crashed or killed run never blocks the next one. <job>.json beside it says which process
runs it, since when and how it was started, for the message a second run gets and for hylly jobs stop.

Different jobs don't wait for each other: a lookup and a Humble download ask different sites.

The library may be shared with another computer, and its state folder isn't, so the job also holds a lock beside
the library's records (state\\locks\\). A network share where that can't work is no reason to stop: a job that holds such a
lock writes a note right after it, and a refused lock with no note is taken to be the share's doing. So is one whose
note tells of a run on this computer that has ended: after a power cut the share may hold that lock for a while yet.

A job on the computer that has the disk and one coming to it through Samba don't see each other's locks, as tried on real
Samba: the client keeps its lock to itself, and Samba's locks aren't the ones taken here. Starting the same job on both at
once takes some doing, so that stays as it is, and the help says so.

Which guard decides what between computers, since the database keeps the library's records (tests/test_runs.py runs two
settings folders on one library, as two computers):
- The owner check (db.own): every command that opens the library's records is refused on a computer whose database
  doesn't keep them, whether it got the library's lock or not. So it decides for the downloads and humble seed, hylly
  add, the catalog, and what changes only the database, which takes no job's lock at all: hylly tag, set and decide,
  and the server. (hylly user and people are about this computer's own database, whichever library it keeps.)
- The library's lock, beside its records (above): it decides only for the jobs that open none of them, and so pass the
  owner check: the lookups (hylly lookup, and those of hylly catalog --lookup and of the Inbox's pass, whose answers are
  files in the state folder: their reading of your word catches NotHere), and itch.io's survey and claim, whose files
  are there too.
- A copy, a backup, a restore and their checks hold the target's own lock instead (beside, in the target's .hyllycopy
  folder); the dry runs hold none.
Both guards stay: the lock costs little, and holds where the owner check leaves a job through.
"""

from __future__ import annotations

import re

import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import instance, paths
from .exits import BUSY, LOCAL, Exit
from .shops import SHOPS
from .storage import atomic_write, read_json

JOBS = {  # job: (what it is, how one that has hung is ended)
    "lookup": ("A lookup", "hylly jobs stop lookup"),
    **{shop.id: (shop.job, f"hylly jobs stop {shop.id}") for shop in SHOPS.values()},  # each shop's, by its id
    "catalog": ("A catalog build", "hylly jobs stop catalog"),
    "add": ("A copy into the library", "hylly jobs stop add"),
    "copy": ("A HyllyCopy", "hylly jobs stop copy"),
    "backup": ("A backup", "hylly jobs stop backup"),  # a restore and a backup check too: one at a time on the disks
}
WHY = {"catalog": "two at once would write the same files", "add": "two at once would copy the same books",
       "copy": "one at a time is kinder to the disks, and nothing needs it to be fast",
       "backup": "one at a time is kinder to the disks, and nothing needs it to be fast"}


class Busy(Exit):
    """The job already runs; the message says where. Uncaught, it ends the command with that message."""

    def __init__(self, message: str) -> None:
        super().__init__(BUSY, message)


def lock_file(job: str) -> Path:
    return paths.place("state") / f"{job}.lock"


def note_file(job: str) -> Path:
    return paths.place("state") / f"{job}.json"


def _open(job: str):
    path = lock_file(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a+b")


def current(job: str) -> dict | None:
    """The running job's note; {} if one runs but has not written it yet; None if none runs (and a note left by one that
    was killed goes: nobody can be writing one while this holds the lock)."""
    if not lock_file(job).exists():
        return None
    with _open(job) as fh:
        if instance._lock(fh):
            with contextlib.suppress(OSError):
                note_file(job).unlink(missing_ok=True)
            instance._unlock(fh)
            return None
    for _ in range(20):  # a run writes its note right after taking the lock
        found = read_json(note_file(job))
        if found:
            return found
        time.sleep(0.1)
    return {}


def describe(note: dict) -> str:
    """Who runs it: "process 1234, since 12:40: hylly lookup --source metron" (with the date if it began another day)."""
    if not note.get("pid"):
        return "it has just started"
    started = note.get("started", "")
    since = started[11:] if started[:10] == time.strftime("%Y-%m-%d") else started
    return f"process {note['pid']}, since {since}" + (f": {note['command']}" if note.get("command") else "")


def busy_message(job: str, note: dict) -> str:
    what, stop = JOBS[job]
    why = WHY.get(job, "two at once would ask the same site twice as fast, a sure way to get blocked")
    return (f"{what} is already running on this computer ({describe(note)}).\n"
            f"Not starting another: {why}.\n"
            f"If it has hung, {stop} ends it.")


def _this_computer() -> str:
    """Tells this computer from another with the same name, as new Raspberry Pis and Steam Decks all have. The network
    card's number it comes from stays out of the library."""
    return hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()[:16]


def _has_ended(who: dict) -> bool:
    """Whether the run a library's note tells of was on this computer and is over: its process is gone, or that number
    belongs to one started after it."""
    import psutil  # here, as every command passes this module

    if who.get("computer") != _this_computer():
        return False
    since = instance.started_at(who)  # time.time() of its start, or a note from before with its local time
    if since is None:
        return False
    try:
        return psutil.Process(who["pid"]).create_time() > since[0] + since[1]
    except psutil.NoSuchProcess:
        return True
    except (KeyError, TypeError, ValueError, OverflowError, psutil.Error):
        return False


def _on_library(job: str, root: Path):
    """The job's lock beside the library's records, for a run on another computer."""
    return beside(job, paths.state_dir(root) / "locks", "the same library")


STALE = 3600  # seconds a note beside the library has been left as it was, after which a refusal says how long


def _ago(seconds: float) -> str:
    hours = int(seconds // 3600)
    return "an hour" if hours < 2 else f"{hours} hours" if hours < 48 else f"{hours // 24} days"


def _elsewhere(job: str, who: dict, note: Path, why: str) -> str:
    """Why the job is refused by its lock beside the library (or a target), as the note there tells of the run that holds
    it: on which computer, or on this one with other settings, and the way out, should that run be long over (on a share
    that keeps no locks of its own, its computer switched off mid-run, say): the note, to be deleted. Still refused, as
    the lock may be held: only a person can tell. A note left as it was for an hour or more says how long: a run that
    tells how far it has come keeps its time new (progress), and one from before that only has its start."""
    here = who.get("computer") == _this_computer()
    where = "this computer, with other settings" if here else str(who.get("host") or "another computer")
    times = [t for t in (who.get("began"), who.get("seen")) if isinstance(t, (int, float)) and not isinstance(t, bool)]
    if not times:
        with contextlib.suppress(OSError):
            times = [note.stat().st_mtime]
    idle = time.time() - max(times) if times else 0
    old = f" Its note hasn't changed for {_ago(idle)}." if idle >= STALE else ""
    whose = "no Hylly with other settings is running it here" if here else "that computer isn't running it"
    return (f"{JOBS[job][0]} is already running on {where} ({describe(who)}). Not starting another{why}.{old} If {whose}, "
            f"delete its note, {note}, and start this again.")


def not_running(job: str, root: Path | None = None) -> None:
    """For a dry run that asks a shop's site: Busy when a run of that job goes, on this computer or, with root, on another
    one working on the same library, as only_one and beside would say it; else nothing. Only looked at: a dry run takes no
    lock and writes no note."""
    note = current(job)
    if note is not None:
        raise Busy(busy_message(job, note))
    if root is None:
        return
    folder = paths.state_dir(root) / "locks"
    who = read_json(folder / f"{job}.json")
    if not who or _has_ended(who) or not (folder / f"{job}.lock").is_file():
        return
    with contextlib.suppress(OSError), open(folder / f"{job}.lock", "rb") as fh:
        if instance._lock(fh):  # nobody holds it: a note left by a run that was killed
            instance._unlock(fh)
            return
        raise Busy(_elsewhere(job, who, folder / f"{job}.json",
                              ", not even one that only looks: two at once would ask the same site twice as fast"))


@contextlib.contextmanager
def beside(job: str, folder: Path, what: str = "the same folder"):
    """The job's lock in this folder, for a run on another computer working on the same thing."""
    note = folder / f"{job}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fh = open(folder / f"{job}.lock", "a+b")
    except OSError:  # a share that won't take it says so soon enough, in the job itself
        yield
        return
    locked = instance._lock(fh)
    noted = locked  # another computer goes by the note, so a run past an ended one's lock leaves one too
    if not locked:
        for _ in range(20):
            who = read_json(note)
            if who and _has_ended(who):
                noted = True
                break
            if who:
                fh.close()
                raise Busy(_elsewhere(job, who, note, f": it works on {what}"))
            time.sleep(0.1)
    try:
        if noted:
            said = {"host": socket.gethostname(), "computer": _this_computer(), "pid": os.getpid(),
                    "started": time.strftime("%Y-%m-%d %H:%M"), "began": time.time(),
                    "command": subprocess.list2cmdline(["hylly", *sys.argv[1:]])}
            with contextlib.suppress(OSError):  # a share that won't take it is no reason to stop: another computer's run just isn't told
                atomic_write(note, json.dumps(said), durable=False)
                _beside[note] = said
        yield
    finally:
        _beside.pop(note, None)
        with contextlib.suppress(OSError):  # the share may be gone by now: the system lets the lock go with the process
            if noted:
                note.unlink(missing_ok=True)
            if locked:
                instance._unlock(fh)
            fh.close()


@contextlib.contextmanager
def only_one(job: str, root: Path | None = None):
    """Runs the block as the job's only run on this computer, and with root on that library; raises Busy at once if
    another one runs."""
    fh = _open(job)
    for _ in range(5):  # someone checking the lock holds it for a moment
        if instance._lock(fh):
            break
        time.sleep(0.1)
    else:
        fh.close()
        raise Busy(busy_message(job, current(job) or {}))
    note = note_file(job)
    _holding.append(job)
    try:
        # over the note of a run that was killed. began: the start as time.time() has it, which a stop goes by (instance.wrote)
        atomic_write(note, json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M"), "began": time.time(),
                                       "command": subprocess.list2cmdline(["hylly", *sys.argv[1:]])}), durable=False)
        if root is None:
            _begun(job)
            yield
        else:
            with _on_library(job, root):
                _begun(job)
                yield
    finally:
        _holding.remove(job)
        note.unlink(missing_ok=True)
        instance._unlock(fh)
        fh.close()


def counting(what: str):
    """Progress as things are counted: one line rewritten on a terminal, a line now and then elsewhere, as a log would get one
    endless line. On standard error, as all progress is: standard output is for the command's result."""
    def show(n: int) -> None:
        if getattr(sys.stderr, "isatty", lambda: False)():
            print(f"\r  {n} {what}", end="", file=sys.stderr, flush=True)
        elif n % 100 == 0:
            print(f"  {n} {what}", file=sys.stderr, flush=True)

    return show


def running() -> list[dict]:
    """The jobs running on this computer, each with its note."""
    out = []
    for job, (what, stop_it) in JOBS.items():
        note = current(job)
        if note is not None:
            out.append({"job": job, "what": what, "stop": stop_it, **note})
    return out


_holding: list[str] = []  # the jobs this process runs
_beside: dict[Path, dict] = {}  # the notes this process holds beside a library or a target (beside), and what each says
_said = 0.0
SAY_EVERY = 30  # seconds between notes of how far a job has come


def progress(line: str) -> None:
    """How far the job has come, in its note, for hylly jobs; and the time in its note beside the library or the target,
    for another computer refused by it (_elsewhere): its run goes on. That one gets no line, which may name a title."""
    global _said
    if not _holding or time.monotonic() - _said < SAY_EVERY:
        return
    _said = time.monotonic()
    note, at = note_file(_holding[-1]), time.strftime("%Y-%m-%d %H:%M")
    with contextlib.suppress(OSError):
        atomic_write(note, json.dumps({**read_json(note), "progress": line, "at": at}), durable=False)
    if _beside and _touching.acquire(blocking=False):
        try:
            threading.Thread(target=_touch, args=(at,), daemon=True, name="notes beside").start()
        except RuntimeError:  # no thread to be had just now: the next line tries again
            _touching.release()


_touching = threading.Lock()  # one _touch at a time


def _touch(at: str) -> None:
    """The time made new in the notes this process holds beside a library or a target (progress): those of a lookup
    inside a catalog build too, as both go on. In a thread of its own, one at a time, as a share that stalls must hold
    up no job, and a copy's watchdog least of all, which tells of the stall. Only a note this run still holds is
    written; one let go while its write was on its way (beside) goes again, if it is still this run's: another
    computer's, written since, stays."""
    try:
        for there, said in list(_beside.items()):
            if _beside.get(there) is not said:  # let go meanwhile, and perhaps taken by another computer's run since
                continue
            with contextlib.suppress(OSError):  # a share gone for a while: its time is new again at a later line
                atomic_write(there, json.dumps({**said, "at": at, "seen": time.time()}), durable=False)
                if _beside.get(there) is not said and read_json(there).get("began") == said["began"]:
                    there.unlink(missing_ok=True)
    finally:
        _touching.release()


def last_file(job: str) -> Path:
    return paths.place("state") / f"{job}-last.json"


RECORD: dict = {}  # the job whose run a watch records (cli._watched), its "job" and when it "started"; "before" once begun


def _record(job: str, started: str) -> dict:
    return {"job": job, "what": JOBS[job][0], "command": subprocess.list2cmdline(["hylly", *sys.argv[1:]]), "started": started}


def _begun(job: str) -> None:
    """The record of a watched job's run as its locks are taken, as one that hasn't ended: remember replaces it as the
    run ends. One stopped (hylly jobs stop, which ends it at once), killed, or cut off with the power never gets that
    far, and hylly jobs says so, where it showed the run before as the last. What it said until now is kept, for a run
    that doesn't count after all (unbegun)."""
    if RECORD.get("job") != job or "before" in RECORD:
        return
    path = last_file(job)
    try:
        RECORD["before"] = path.read_bytes()
    except FileNotFoundError:
        RECORD["before"] = None
    except OSError:
        return
    with contextlib.suppress(OSError):
        atomic_write(path, json.dumps({**_record(job, RECORD["started"]), "exit": None, "said": "", "ended": False,
                                       "pid": os.getpid()}), durable=False)


def unbegun(job: str) -> None:
    """A watched run that didn't count after all, refused once it held its locks (no login, a question with nobody
    there): the record it began goes, and what was there before is there again, unless another run has begun since."""
    before = RECORD.pop("before", False)
    if before is False or RECORD.get("job") != job:
        return
    path = last_file(job)
    now = read_json(path)
    if now.get("ended") is not False or now.get("pid") != os.getpid() or now.get("started") != RECORD.get("started"):
        return
    with contextlib.suppress(OSError):
        if before is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, before, durable=False)


def remember(job: str, started: str, seconds: float, code: int, said: str) -> None:
    """How the job's last run ended, for hylly jobs: in place of the record it began with (_begun)."""
    with contextlib.suppress(OSError):
        atomic_write(last_file(job), json.dumps({**_record(job, started), "seconds": round(seconds), "exit": code, "said": said,
                                                 "ended": True}), durable=False)


def last_runs() -> list[dict]:
    found = [read_json(last_file(job)) for job in JOBS]
    return sorted((n for n in found if n.get("started")), key=lambda n: str(n["started"]), reverse=True)


def stop(job: str, timeout: float = 15) -> dict | None:
    """Ends the job's running process and waits until it is gone. Returns its note, or None if none was running."""
    note = instance.holder(lambda: current(job), JOBS[job][0], note_file(job), job=True)
    if note is None:
        return None
    pid = note["pid"]
    try:
        os.kill(pid, signal.SIGTERM)  # ends it at once, as closing its window would; what it saved is kept
    except OSError:
        pass  # already gone
    deadline = time.monotonic() + timeout
    while current(job) is not None:
        if time.monotonic() > deadline:
            raise Exit(LOCAL, f"{JOBS[job][0]} (process {pid}) did not stop within {timeout:g} seconds.")
        time.sleep(0.2)
    return note


def seconds_of(text: str) -> float:
    """--progress: 6h or 30m."""
    m = re.fullmatch(r"(\d+)\s*([mh])", text.strip().lower())
    if not m or int(m[1]) * (3600 if m[2] == "h" else 60) < 600:
        raise ValueError("like 6h or 30m, and at least 10 minutes")
    return int(m[1]) * (3600 if m[2] == "h" else 60)
