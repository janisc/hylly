"""One library server at a time, on any platform: a lock held while it runs, and a note of where it is.

The server keeps an OS lock on server.lock (in the state folder) for as long as it runs. The
operating system drops the lock when the process ends, however it ends, so a crashed or killed
server never looks alive. server.json beside it says where the server listens and on which
port; hylly server status, stop and restart, and the launchers, read it.

Also here: the server's own log, server.log, which it writes itself however it runs, with a size limit (write_log,
logged); starting the server, hyllyd, hidden in the background; and the optional start at every logon. On Windows
that is a Task Scheduler task, set up here. On Linux and macOS the system runs the server itself, in the foreground,
from a systemd user unit or a launchd agent: hylly server autostart on prints the one for this computer, with the
commands that install it, and installs nothing (logon_start). On a Linux computer that serves the internet, the server
can be a system-wide service with walls around it instead (examples/hylly.service): hylly serve --service-file prints
that one for this computer, from the settings, and installs nothing either (service_file); the doctor compares the one
installed with the settings (service_differences).
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths, rules, settings
from .exits import LOCAL, NOT_SET_UP, PARTLY, TABLE, USAGE, Exit
from .sites import SITES
from .storage import append_log, atomic_write, read_json

if os.name == "nt":
    import msvcrt
else:
    import fcntl

TASK = "Hylly library server"
SYSTEM = "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
UNIT = "hylly.service"  # a systemd user service, on Linux
AGENT = "io.github.janisc.hylly"  # a launchd agent, on macOS
SERVICE_FLAG = "HYLLY_SYSTEM_SERVICE"  # in the environment of a server in a system-wide service (examples/hylly.service)
RESTART_CODE = 75  # how such a server asks to be started again: inside its walls it can't ask the system anything
UNIT_FOLDERS = ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")
WALLED = ("The library server here is a system-wide service, with walls around it, and nothing else may start one outside "
          "them. To start it: sudo systemctl start hylly (if that says it started too often: sudo systemctl reset-failed "
          "hylly first). Its options are in the settings, and hylly server restart restarts it.")
# What was set when it was asked for: where Hylly keeps its files (paths.PLACES, and the XDG folders on Linux, which a
# shell's profile may set where the system's service manager doesn't see them), the library, the rules, and PATH: 7-Zip
# from Homebrew, say, which a server started by launchd wouldn't find
KEPT_FOR_THE_SERVER = ("HYLLY_HOME", *(variable for _, variable, _, _ in paths.PLACES.values()), *paths.XDG, "HYLLY_SETTINGS",
                       "HYLLY_LIBRARY", "HYLLY_RULES", "PATH")
# The shops' logins and the lookup sources' keys, as the environment may give them instead of hylly login (sites.SITES):
# for the commands that download and look up, never for the server, which needs none of them, nor for what it starts.
# Taken out of a server's environment however it starts (spawn, without_logins), as the units above never pass them.
NOT_FOR_THE_SERVER = tuple(variable for _, _, variable in SITES.values())
LOG_LIMIT = 1 << 20  # server.log moves to server.log.1 beyond this (1.0 MB), and the one before that goes
STAMPED = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")  # the time at the start of a line in server.log


def lock_file() -> Path:
    return paths.place("state") / "server.lock"


def info_file() -> Path:
    return paths.place("state") / "server.json"


def log_file() -> Path:
    return paths.place("state") / "server.log"


_log_lock = threading.Lock()


def write_log(text: str) -> None:
    """Lines into server.log, each with the time. Beyond LOG_LIMIT it moves to server.log.1 first, and the one before that
    goes, as access.log does: a server runs for months. The server opens it itself, as its own user, so a service with
    walls needs only its state folder writable, and nobody outside them opens it for it. Trouble writing it is no
    reason to stop: the same lines are on standard error, under a service in the system's journal."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    # A line ends at a line break only, and each is cleaned as it is written (append_log): what a request sent can't
    # begin one that looks like the server's
    lines = [f"{stamp} {line}" if line else "" for line in (part.rstrip("\r") for part in text.split("\n"))]
    with _log_lock, contextlib.suppress(OSError):
        # Another process appending at the same moment could, on Windows, cost a line, as it always could there:
        # left as it is, as the other writers (a stop's line, the restart's helper) write while the server is ending
        append_log(log_file(), lines, LOG_LIMIT)


class Logged:
    """Standard error while the server runs (logged): what is written there goes on to where it went (a terminal, the
    system's journal, or nowhere for a hidden server), and into server.log, a whole line at a time (write_log). So the
    log has all the server says, its failures' tracebacks too, however it was started."""

    def __init__(self, stream) -> None:
        self.stream, self._part = stream, ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            if self.stream is not None:  # none at all in a pythonw
                with contextlib.suppress(OSError, ValueError):
                    self.stream.write(text)
            done, _, self._part = (self._part + text).rpartition("\n")
            if done:
                write_log(done)
        return len(text)

    def flush(self) -> None:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.stream.flush()

    def __getattr__(self, name: str):  # isatty, encoding and the rest, as the stream has them
        return getattr(self.stream, name)


def _into_the_log(stream) -> bool:
    """Whether standard error is server.log itself, as a service an older Hylly set up has it (a shell's >>, or
    StandardOutput=append): then each line goes into it once, with its time, and not again as it is."""
    try:
        return os.path.samestat(os.fstat(stream.fileno()), os.stat(log_file()))
    except (OSError, ValueError, AttributeError):  # no file there, or no stream at all (a pythonw)
        return False


@contextlib.contextmanager
def logged():
    """While the block runs, standard error also goes into server.log (Logged): the server's start, hylly serve's and
    hyllyd's alike. Once, however often it's asked."""
    if isinstance(sys.stderr, Logged):
        yield
        return
    before = sys.stderr
    sys.stderr = mine = Logged(None if _into_the_log(before) else before)
    try:
        yield
    finally:
        sys.stderr = before
        if mine._part:  # a last line without its line break
            write_log(mine._part)


class AlreadyRunning(Exception):
    pass


def _lock(fh) -> bool:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    if os.name == "nt":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _open_lock():
    path = lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a+b")


class Claim:
    """Being the library server: holds the lock from creation until release() or the end of the process."""

    def __init__(self) -> None:
        self._fh = _open_lock()
        for _ in range(5):  # someone checking the lock holds it for a moment
            if _lock(self._fh):
                break
            time.sleep(0.1)
        else:
            self._fh.close()
            raise AlreadyRunning("A library server is already running (hylly server status shows where).")
        info_file().unlink(missing_ok=True)  # the note of a server that was killed
        self._mine: dict | None = None

    def announce(self, **info) -> None:
        atomic_write(info_file(), json.dumps(info, indent=1))
        self._mine = info

    def release(self) -> None:
        if self._fh.closed:
            return
        if self._mine is not None:
            info_file().unlink(missing_ok=True)
        _unlock(self._fh)
        self._fh.close()


def running() -> dict | None:
    """The running server's note; {} if one runs but has not written it yet; None if none runs (and a note left by one
    that was killed goes: nobody can be writing one while this holds the lock)."""
    if not lock_file().exists():
        return None
    with _open_lock() as fh:
        if _lock(fh):
            with contextlib.suppress(OSError):
                info_file().unlink(missing_ok=True)
            _unlock(fh)
            return None
    for _ in range(20):  # a server writes its note as soon as it listens
        found = read_json(info_file())
        if found:
            return found
        time.sleep(0.1)
    return {}


def started_at(note: dict) -> tuple[float, float] | None:
    """When a note's process took its lock, in seconds since the epoch, and the leeway that time takes: "began" is
    time.time() itself, whatever the time zone or the clock says later; a note an older Hylly wrote has only "started",
    the local time, to the second (the server's) or the minute (a job's), which a change of time zone or the hour the
    clocks go back can put off by hours. None when it says nothing that can be read."""
    if isinstance(note.get("began"), (int, float)) and not isinstance(note.get("began"), bool):
        return float(note["began"]), 5  # the system's own count of a process's start can be a second off
    for form, leeway in (("%Y-%m-%dT%H:%M:%S", 5), ("%Y-%m-%d %H:%M", 60)):
        try:
            return time.mktime(time.strptime(str(note.get("started") or ""), form)), leeway
        except (ValueError, OverflowError):
            pass
    return None


def _runs(command: list[str]) -> tuple[str, list[str]]:
    """What a command line runs, and what it is given: only its program word counts, or for a Python the module after -m
    (hylly.daemon) or the script it was handed (a uv tool's bin/hyllyd is one), by name, never a word further on: an
    editor with hylly's file open runs the editor. ("", []) for anything else, python -c too."""
    def name(word: str) -> str:
        return os.path.basename(word).lower().removesuffix(".exe")

    if not command:
        return "", []
    if not name(command[0]).startswith("python"):
        return name(command[0]), command[1:]
    words = iter(enumerate(command[1:], 1))
    for i, word in words:
        if word == "-m":
            return (command[i + 1], command[i + 2:]) if i + 1 < len(command) else ("", [])
        if word in ("-W", "-X"):  # a Python option with a value of its own after it
            next(words, None)
        elif word in ("-c", "-") or not word.startswith("-"):  # code given in place, or the script itself
            return ("", []) if word in ("-c", "-") else (name(word), command[i + 1:])
    return "", []


def _its_command(note: dict, command: list[str], job: bool) -> bool:
    """Whether a command line is the one a note's process had: the server's is hyllyd itself (hylly.daemon), whatever it
    is given; a job's is hylly with the very words its note keeps (runs.py), so another job's hylly never passes."""
    runs, given = _runs(command)
    if not job:
        return runs in ("hyllyd", "hylly.daemon")
    try:
        said = shlex.split(str(note.get("command") or ""))
    except ValueError:
        return False
    return runs == "hylly" and said[:1] == ["hylly"] and given == said[1:]


def wrote(note: dict, job: bool = False) -> bool:
    """Whether the process a note names is the one that wrote it: the server's note, or with job a job's (runs.py). A
    killed or stopped one leaves its note behind, and its number goes to another program in time, which began after the
    note did; the note's own process began before it. On Linux and macOS the system counts a process's start from the
    clock as it is now, so a clock set forward makes even the note's own process seem to have begun after it: there, one
    whose command line is the note's (_its_command) is taken for it. Gone, out of reach, or a note without a time: not it."""
    import psutil  # here, as every command passes this module (runs.py)

    since = started_at(note)
    if since is None or not note.get("pid"):
        return False
    try:
        process = psutil.Process(note["pid"])
        if process.create_time() <= since[0] + since[1]:
            return True
        return SYSTEM != "windows" and _its_command(note, process.cmdline(), job)
    except (psutil.Error, TypeError, ValueError, OverflowError):
        return False


def holder(look: Callable[[], dict | None], what: str, note_file: Path, job: bool = False) -> dict | None:
    """For a stop: the note of the process that holds a lock (look: running, or with job a job's runs.current), or None
    when none does. A note its process didn't write (wrote) is one left behind while a start or a check holds the lock
    for a moment: looked at again for a few seconds, and then nothing is stopped, rather than whatever has that number."""
    deadline = time.monotonic() + 3
    while True:
        note = look()
        if note is None or wrote(note, job):
            return note
        if time.monotonic() > deadline:
            break
        time.sleep(0.2)
    raise Exit(LOCAL, f"{what} holds its lock, but {note_file} " + (
        f"tells of process {note['pid']}, which has ended or is another program" if note.get("pid") else
        "does not say which process it is") + ": nothing was stopped. Try again in a moment.")


def stop(timeout: float = 15) -> dict | None:
    """Stops the running server and waits until it is gone. Returns its note, or None if none was running."""
    info = holder(running, "A library server", info_file())
    if info is None:
        return None
    pid = info["pid"]
    try:
        os.kill(pid, signal.SIGTERM)  # Windows: ends the process at once; elsewhere it shuts down cleanly
    except OSError:
        pass  # already gone
    deadline = time.monotonic() + timeout
    while running() is not None:
        if time.monotonic() > deadline:
            raise Exit(LOCAL, f"The library server (process {pid}) did not stop within {timeout:g} seconds.")
        time.sleep(0.2)
    if os.name == "nt":  # ended at once: it had no chance to say so itself
        write_log("Stopped (hylly server stop or restart).")
    return info


def _python() -> str:
    """This Python, but the console one: it gets no window, and the firewall permission given to it applies."""
    exe = Path(sys.executable)
    return str(exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe)


# -P: not the current folder first on the import path, where a hylly of anyone's could be
DAEMON = ["-P", "-m", "hylly.daemon"]  # hyllyd, the server itself
HYLLY = ["-P", "-m", "hylly", "--here"]  # a command of hylly's, for this computer's own server: never passed on (remote.py)
RESTART = [*HYLLY, "server", "restart", "--no-browser"]  # the server page's Restart, apart from the server it stops


def spawn(arguments: list[str], errors_to_log: bool = False) -> subprocess.Popen:
    """Runs this Python with these arguments (DAEMON, say) hidden and detached from this process. What it prints goes
    nowhere: the server writes server.log itself (logged), and could move it aside then, as nothing else holds it open.
    errors_to_log: its standard error goes to server.log, for the server page's Restart (RESTART), a hylly command
    that has no log of its own and ends in a moment."""
    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        detach = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        detach = {"start_new_session": True}
    with log.open("ab") if errors_to_log else contextlib.nullcontext(subprocess.DEVNULL) as errors:
        return subprocess.Popen([_python(), *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                env={**without_logins(os.environ), "PYTHONUNBUFFERED": "1"}, **detach)


def without_logins(environment) -> dict[str, str]:
    """An environment without the shops' logins and the lookup sources' keys (NOT_FOR_THE_SERVER): a server's, or that
    of what starts one."""
    return {name: value for name, value in environment.items() if name not in NOT_FOR_THE_SERVER}


def logins_out() -> None:
    """As a server starts, however it was started (hylly serve at a terminal, a service, spawn): the shops' logins and
    the lookup sources' keys out of its own environment (NOT_FOR_THE_SERVER), and so out of every process it starts.
    It needs none of them; the Inbox's lookups it starts take the keys saved with hylly login, as in a walled service."""
    for name in [name for name in os.environ if name in NOT_FOR_THE_SERVER]:
        os.environ.pop(name, None)


def _said_since(log: Path, size: int) -> str:
    """The last line the server wrote into its log after it had this size, without its time; '' if none."""
    try:
        with log.open("rb") as fh:
            end = fh.seek(0, 2)
            fh.seek(size if end >= size else 0)  # smaller now: moved aside meanwhile, and begun again
            lines = fh.read().decode("utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return STAMPED.sub("", lines[-1]) if lines else ""


def start_background(timeout: float = 30) -> dict:
    """Starts hyllyd hidden in the background, with the settings, and waits until it listens. Returns its note."""
    log = log_file()
    try:
        size = log.stat().st_size
    except OSError:
        size = 0
    child = spawn([*DAEMON, "--in-background"])
    deadline = time.monotonic() + timeout
    while True:
        info = running()
        if info:
            return info
        if child.poll() is not None:  # it ended with a code of its own, 6 for a port that's taken, say: that one, as here
            said = _said_since(log, size) or f"it ended at once, without a word (exit code {child.returncode})"
            code = child.returncode if child.returncode in TABLE and child.returncode not in (0, PARTLY) else LOCAL
            raise Exit(code, f"The library server did not start: {said}\n(All its messages: {log})")
        if time.monotonic() > deadline:
            raise Exit(LOCAL, f"The library server has not started after {timeout:g} seconds; see {log}.")
        time.sleep(0.2)


# ---- start at logon: a Task Scheduler task on Windows; on Linux and macOS a systemd unit or a launchd agent, printed ----

# What the systemd user unit and the launchd agent run: the server itself, in the foreground for the system that looks
# after it (in the background it would seem to have ended), with the settings; its messages on standard error and in
# server.log, which it writes itself.
LOGON = [*DAEMON, "--in-background"]
TASK_ARGUMENTS = [*HYLLY, "serve", "--background", "--no-browser"]  # the Windows task's: it starts the server and ends


def task_xml(command: str, arguments: str, workdir: str, user: str) -> str:
    """A Task Scheduler task that runs command at the user's logon, without a window and without a time limit
    beyond the few seconds the start takes."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts the library server (hylly serve) in the background at logon.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT15S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def _tool(*argv: str, **options) -> subprocess.CompletedProcess:
    """A system's own tool run (Task Scheduler's, systemd's, launchd's, loginctl), and what it said, read as text, with
    whatever isn't text in it replaced; its exit code is the caller's to look at. Each tool has its own runner below,
    which the tests put a stand-in for, so that no test asks this computer's system."""
    return subprocess.run(list(argv), capture_output=True, text=True, errors="replace", **options)


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return _tool(paths.windows_tool("schtasks"), *args)


def start_folder() -> str:
    """The folder the start at logon runs the server in: your home folder, which is there whatever installed Hylly, and
    not where its program is, which an update or an uninstall takes away."""
    return str(Path.home())


def _kept_environment() -> dict[str, str]:
    return {name: os.environ[name] for name in KEPT_FOR_THE_SERVER if os.environ.get(name)}


def _unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / UNIT


def unit_text(python: str, arguments: list[str], folder: str, environment: dict[str, str]) -> str:
    """The systemd user unit: on when you log in, or from the computer's start with lingering on, and started again if
    it fails. Stopping it with hylly server stop isn't a failure. Its messages go to the journal, as the server writes
    server.log itself: nothing opens that file for it, and nothing makes it grow for ever."""
    def setting(text: str) -> str:  # systemd expands % everywhere, and $ only on the command line
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    def word(text: str) -> str:
        return setting(text).replace("$", "$$")

    def bare(text: str) -> str:  # for what takes one value: systemd refuses quotes there
        return text.replace("%", "%%")

    lines = ["[Unit]", "Description=Hylly library server",
             "StartLimitIntervalSec=600", "StartLimitBurst=5", "",  # one that can't start (another runs, say) doesn't try for ever
             "[Service]", "ExecStart=" + " ".join(word(a) for a in [python, *arguments]), f"WorkingDirectory={bare(folder)}",
             'Environment="PYTHONUNBUFFERED=1"', *(f"Environment={setting(f'{k}={v}')}" for k, v in environment.items()),
             "Restart=on-failure", "RestartSec=10", "",
             "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _tool("systemctl", "--user", *args)


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT}.plist"


def agent_plist(python: str, arguments: list[str], folder: str, environment: dict[str, str]) -> bytes:
    """The launchd agent: on when you log in, and started again if it fails. No file for its output: the server writes
    server.log itself."""
    return plistlib.dumps({"Label": AGENT, "ProgramArguments": [python, *arguments], "WorkingDirectory": folder,
                           "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 20,
                           "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **environment}})


def _launchctl(*args: str, **options) -> subprocess.CompletedProcess:
    return _tool("launchctl", *args, **options)


def _gui() -> str:
    return f"gui/{getattr(os, 'getuid', lambda: 0)()}"


def _lingering() -> bool:
    """Whether this user's services start with the computer, with nobody logged in. Yours to choose: it goes for all of them."""
    if not shutil.which("loginctl"):
        return False
    return _tool("loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value").stdout.strip() == "yes"


def after_autostart_off() -> str | None:
    """What stays as it was when the start at logon goes."""
    if SYSTEM == "linux" and _lingering():
        return "Lingering stays on for this user, as other services may use it: loginctl disable-linger turns it off."
    return None


def in_system_service() -> bool:
    """This process is the server inside a system-wide service."""
    return bool(os.environ.get(SERVICE_FLAG))


def _systemctl_wide(*args: str) -> subprocess.CompletedProcess:
    return _tool("systemctl", *args)


def _me() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def _system_unit() -> Path | None:
    """The system-wide service's file, where the system would find it first; None when there's none."""
    return next((Path(folder) / UNIT for folder in UNIT_FOLDERS if os.path.lexists(Path(folder) / UNIT)), None)


def system_service() -> bool:
    """Whether the server here is a system-wide service, set up by hand with walls around it (examples/hylly.service),
    rather than the user's own service or a hidden process. Then nothing else may start one, or it would run outside them.
    Told from the service's file, without asking the system, which may not answer: masked, disabled or unreadable, it's still
    the one to go by. Another user's service says nothing about yours."""
    if SYSTEM != "linux":
        return False
    if in_system_service():
        return True
    if not paths.usual_settings_folder(settings_file_counts=False):
        return False
    path = _system_unit()
    if path is None:
        return False
    try:
        users = re.findall(r"(?m)^\s*User\s*=\s*(\S+)", path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return True
    return not users or _is_me(users[-1])  # by name or by number; systemd takes no quotes there


def system_owns(pid: int | None) -> bool:
    """Whether the system's service manager started this server, and so has to be the one to restart it: it would take
    a helper down along with the server, and otherwise wouldn't look after what came next."""
    if not pid:
        return False
    if system_service():
        if in_system_service():
            return pid == os.getpid()
        return _systemctl_wide("show", "-p", "MainPID", "--value", UNIT).stdout.strip() == str(pid)
    if SYSTEM == "linux" and shutil.which("systemctl"):
        return _systemctl("show", "-p", "MainPID", "--value", UNIT).stdout.strip() == str(pid)
    if SYSTEM == "mac":
        return re.search(rf"\bpid = {pid}\b", _launchctl("print", f"{_gui()}/{AGENT}").stdout) is not None
    return False


def restart_through_system() -> None:
    if system_service():  # no asking the system for it, from inside the walls or without sudo: the server ends with RESTART_CODE
        pid = os.getpid() if in_system_service() else (running() or {}).get("pid")
        if pid:
            os.kill(pid, signal.SIGUSR1)
        return
    if SYSTEM == "linux":
        done = _systemctl("--no-block", "restart", UNIT)
    else:
        done = _launchctl("kickstart", "-k", f"{_gui()}/{AGENT}", start_new_session=True)
    if done.returncode:
        raise Exit(LOCAL, f"The system did not restart the library server: {done.stderr.strip() or done.stdout.strip()}")


def _is_the_usual_hylly() -> None:
    if not paths.usual_settings_folder():
        raise Exit(USAGE, f"The start at logon is one for the whole user, and this Hylly has settings of its own ({paths.config_dir()}, "
                          f"{paths.settings_file()}): it would change the real one, which would then not find them. Nothing changed.")


def autostart_state() -> bool | None:
    """Whether the server starts at logon; None where this system has nothing to do that with, or this Hylly isn't the
    user's usual one, which has no say in it."""
    if in_system_service():
        return True
    if not paths.usual_settings_folder():
        return None
    if SYSTEM == "windows":
        return _schtasks("/Query", "/TN", TASK).returncode == 0
    if SYSTEM == "mac":
        return _agent_path().is_file()
    if not shutil.which("systemctl"):
        return None
    if system_service():
        return _systemctl_wide("is-enabled", UNIT).returncode == 0
    return _systemctl("is-enabled", UNIT).returncode == 0


def autostart_on(dry_run: bool = False) -> str | None:
    """Windows: sets the start at logon, a Task Scheduler task that starts the server in the background with the settings
    (TASK_ARGUMENTS). Returns what you may also want to know; with dry_run, only what it would set. On Linux and macOS
    the system runs the server: logon_start prints what it takes."""
    _is_the_usual_hylly()
    if dry_run:
        return f"It would start the library server in the background at every logon, with the settings, by a Task Scheduler task, {TASK}."
    command = str(Path(sys.executable).with_name("pythonw.exe"))  # no window; it starts the server and ends
    arguments = subprocess.list2cmdline(TASK_ARGUMENTS)
    user = f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ['USERNAME']}"
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "task.xml"
        xml.write_text(task_xml(command, arguments, start_folder(), user), encoding="utf-16")
        done = _schtasks("/Create", "/TN", TASK, "/XML", str(xml), "/F")
    if done.returncode:
        raise Exit(LOCAL, f"Task Scheduler did not accept the logon start: {done.stderr.strip() or done.stdout.strip()}")
    return None


def autostart_off(dry_run: bool = False) -> bool:
    """Windows: removes the logon start; False if there was none. With dry_run, only whether there's one to remove. On
    Linux and macOS: logon_stop."""
    _is_the_usual_hylly()
    if not autostart_state() or dry_run:
        return bool(autostart_state())
    done = _schtasks("/Delete", "/TN", TASK, "/F")
    if done.returncode:
        raise Exit(LOCAL, f"Task Scheduler did not remove the logon start: {done.stderr.strip()}")
    return True


def logon_kind() -> str:
    return "launchd agent" if SYSTEM == "mac" else "systemd user unit"


def _may_print() -> None:
    """What keeps a start at logon from being made here at all: another Hylly's settings folder, a system-wide service,
    a Linux without systemd."""
    _is_the_usual_hylly()
    if system_service():
        raise Exit(NOT_SET_UP, WALLED)
    if SYSTEM == "linux" and not shutil.which("systemctl"):
        raise Exit(USAGE, "This system has no systemd to start the server at logon: have what starts its programs run the "
                          f"server itself, in the foreground: {shlex.join([_python(), *LOGON])}. Nothing changed.")


def this_hylly() -> str:
    """The hylly running now, as a command to type again from any folder: its own full path (the installed command, or
    a checkout's .venv's, which uv run starts), else this Python with -P -m hylly."""
    program = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if program and program.stem.lower() == "hylly" and program.is_file():
        return shlex.quote(str(program.absolute()))
    return f"{shlex.quote(_python())} -P -m hylly"


def _written(path: Path) -> str:
    """The step that writes the start at logon: into a new file first, which takes its place only when the command
    went right, so a mistake never leaves an empty one where the system would look."""
    new = shlex.quote(f"{path}.new")
    return f"{this_hylly()} server autostart on > {new} && mv {new} {shlex.quote(str(path))}"


def logon_start() -> tuple[str, list[str], str]:
    """What hylly server autostart on prints on Linux and macOS: the systemd user unit or the launchd agent with
    this computer's paths filled in, which says at its top where it goes and how it is installed; those commands, with
    the hylly that runs now (this_hylly); and what else to know. Nothing is installed: the system then runs the server,
    in the foreground, and starts it again if it fails, as with any service. All of it is made before a word of it is
    printed: when something stops it (a system-wide service, say), nothing goes to standard output."""
    _may_print()
    python, folder, environment = _python(), start_folder(), _kept_environment()
    stop = f"{this_hylly()} server stop    # the server running now, if one does: the system starts its own"
    if SYSTEM == "mac":
        path = _agent_path()
        steps = [stop, f"mkdir -p {shlex.quote(str(path.parent))}", f"launchctl bootout {_gui()}/{AGENT}    # only if an earlier one is loaded",
                 _written(path), f"launchctl bootstrap {_gui()} {shlex.quote(str(path))}"]
        said = "\n".join(["Hylly's start at logon for this computer, as hylly server autostart on printed it. To install it:",
                          *(f"  {step}" for step in steps)]).replace("--", "- -")  # a comment can't hold two dashes
        head, _, rest = agent_plist(python, LOGON, folder, environment).decode("utf-8").partition("<plist")
        return (f"{head}<!-- {said} -->\n<plist{rest}", steps,
                "macOS may ask once whether Python may run in the background: it takes that for the start at logon to work.")
    path = _unit_path()
    steps = [stop, f"mkdir -p {shlex.quote(str(path.parent))}", _written(path),
             "systemctl --user daemon-reload", f"systemctl --user enable --now {UNIT}"]
    head = ["# Hylly's start at logon for this computer, as hylly server autostart on printed it. To install it:",
            *(f"#   {step}" for step in steps), "# Its messages: journalctl --user -u hylly, and server.log in the state folder."]
    also = ("It also starts with the computer, with nobody logged in, as lingering is on for this user." if _lingering() else
            "It starts when you log in. For a computer nobody logs in to, such as a server, it has to start with the "
            f"computer too: loginctl enable-linger {getpass.getuser()} (with sudo if it asks).")
    return "\n".join(head) + "\n" + unit_text(python, LOGON, folder, environment), steps, also


def logon_stop() -> tuple[list[str], str]:
    """What hylly server autostart off prints on Linux and macOS: the commands that stop the server the system runs and
    take its start at logon away, and what stays as it was. Nothing is changed here."""
    _is_the_usual_hylly()
    if system_service():
        raise Exit(NOT_SET_UP, WALLED)
    if SYSTEM == "mac":
        return [f"launchctl bootout {_gui()}/{AGENT}", f"rm {shlex.quote(str(_agent_path()))}"], ""
    return ([f"systemctl --user disable --now {UNIT}", f"rm {shlex.quote(str(_unit_path()))}", "systemctl --user daemon-reload"],
            after_autostart_off() or "")


def _unit_words(text: str) -> list[str]:
    """A unit's ExecStart, as its words (the last one counts), with systemd's own $$ and %% as one; [] when there's none."""
    lines = re.findall(r"(?m)^\s*ExecStart\s*=\s*(.*\S)", text)
    try:
        words = shlex.split(lines[-1]) if lines else []
    except ValueError:
        return []
    words = [w.replace("$$", "$").replace("%%", "%") for w in words]
    return [words[0].lstrip("-@+!:"), *words[1:]] if words else []


UNWATCHED = {"--in-background", "--no-browser"}  # all a server nobody watches is given now; services from before pass both
TASKED = {"-P", "-m", "hylly", "--here", "serve", "--background", "--no-browser"}  # all the Windows task's words may be


def _refused(words: list[str]) -> bool:
    """Whether hyllyd refuses this start of a unit or an agent now (its program and arguments): the server's options
    given to one in the background, or an older Hylly's hylly serve. It then fails at every start."""
    if not words:
        return False
    rest = words[1:]
    if os.path.basename(words[0]).lower().removesuffix(".exe") != "hyllyd":  # a Python, then
        if "hylly.daemon" not in rest:
            return True
        rest = rest[rest.index("hylly.daemon") + 1:]
    return not set(rest) <= UNWATCHED


def logon_found() -> dict | None:
    """The start at logon there is, for hylly doctor: where it is, the program it starts, whether that is gone (with
    an uninstalled Hylly, say), and whether it is old: one an older Hylly made, with that version's options for the
    server, or a system-wide service that isn't the one examples/hylly.service gives today: its log written by a
    shell (shelled), or without RestartForceExitStatus=75 (no_restart). fails: one that passes the server options of
    its own, refused now at every start (_refused). walled: the system-wide service. None when there's none, or this
    Hylly has no say in it."""
    if not paths.usual_settings_folder():
        return None
    if SYSTEM == "windows":
        done = _schtasks("/Query", "/TN", TASK, "/XML")
        if done.returncode:
            return None
        from xml.etree import ElementTree

        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        try:
            task = ElementTree.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", done.stdout))
        except ElementTree.ParseError:
            return None
        program = task.findtext("t:Actions/t:Exec/t:Command", "", ns).strip().strip('"')  # in quotes, if made by hand
        arguments = task.findtext("t:Actions/t:Exec/t:Arguments", "", ns).strip()
        words = {w.strip('"') for w in arguments.split()}
        return {"where": f"the Task Scheduler task {TASK}", "program": program, "gone": not Path(program).is_file(), "walled": False,
                "old": arguments != subprocess.list2cmdline(TASK_ARGUMENTS),
                "fails": not ({"serve", "--background"} <= words <= TASKED)}
    if SYSTEM == "mac":
        path = _agent_path()
        try:
            agent = plistlib.loads(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError, plistlib.InvalidFileException):
            agent = {}
        words = [str(w) for w in agent.get("ProgramArguments") or []]
        return {"where": str(path), "program": words[0] if words else "?", "gone": bool(words) and not Path(words[0]).is_file(),
                "walled": False, "old": words[1:] != LOGON or "StandardOutPath" in agent or "StandardErrorPath" in agent,
                "fails": _refused(words)}
    walled = system_service()
    path = _system_unit() if walled else _unit_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path else None
    except FileNotFoundError:
        text = None
    except OSError:
        text = ""
    if text is None:
        return None
    words = _unit_words(text)
    shelled = walled and words[:2] in (["/bin/sh", "-c"], ["sh", "-c"])  # from before: a shell that wrote its log
    words = words[3:] if shelled else words
    no_restart = bool(text) and walled and re.search(r"(?m)^\s*RestartForceExitStatus\s*=.*\b75\b", text) is None
    if not text:  # masked, or not to be read: nothing to go by
        old = False
    elif walled:
        old = shelled or no_restart
    else:
        old = words[1:] != LOGON or re.search(r"(?m)^\s*Standard(Output|Error)\s*=\s*append", text) is not None
    return {"where": str(path), "program": words[0] if words else "?", "gone": bool(words) and not Path(words[0]).is_file(),
            "walled": walled, "old": old, "fails": _refused(words), "shelled": shelled, "no_restart": no_restart}


# ---- the walled service: the server as a system-wide service with walls around it, on Linux (examples/hylly.service) ----

WALLED_FILE = f"/etc/systemd/system/{UNIT}"  # where the walled service is installed
ABOUT = [  # what the walls are, at the top of examples/hylly.service and of every one hylly serve --service-file prints
    "# The library server as a system-wide service with walls around it, for a Linux computer that serves the internet.",
    "# Of your folders it sees only the library, Hylly's own folders (its settings only to read, and neither the shops'",
    "# logins nor what long jobs said) and the program; it can gain no rights, and it can't reach other programs through",
    "# their sockets. A flaw in it could still change the library and Hylly's records, read what any user may in /etc and",
    "# /usr, and use the network. The help tells more (\"The library server\").",
]
# What all in one folder holds of the settings' own (paths.SETTINGS_SIGNS), each walled read-only in it, the folder
# being the server's to write: settings.toml and secrets/ must be there before the server starts (every command makes
# secrets/, hylly settings edit settings.toml); your rules and themes folders, which nothing makes for you, have a -
SETTINGS_IN_ONE = ("settings.toml", "secrets")
OPTIONAL_IN_ONE = ("rules", "themes")
NOT_HIDDEN_FROM = ("/home", "/root", "/run")  # a library's parent in these isn't put under TemporaryFileSystem: ProtectHome
# and /run:ro hide them already


def _in(path: str, folder: str) -> bool:
    """Whether a path is the folder or inside it, by their words alone, as systemd compares the paths it mounts."""
    path, folder = os.path.normpath(path), os.path.normpath(folder)
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _unit_path_word(path: str, quoted: bool = True) -> str:
    """A path as a word of a systemd setting that lists paths: systemd's own % doubled, and in quotes when it has a space,
    which would end it there (quoted; not for a setting of one path, WorkingDirectory=, which takes quotes as they are).
    One with a quote, a backslash, a colon (which parts a bind's two paths) or a control character in it is refused: no
    folder is worth the doubt of how systemd would read it."""
    if not os.path.isabs(path) or any(c in path for c in "\"'\\:") or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise Exit(USAGE, f"{path} can't go in a systemd unit as it is (it isn't a full path, or it has a quote, a backslash, "
                          "a colon or a control character in it): give that folder another name, or write the service by hand "
                          "from examples/hylly.service. Nothing was printed.")
    word = path.replace("%", "%%")
    return f'"{word}"' if quoted and any(c.isspace() for c in word) else word


def walled_unit(*, user: str, group: str, library: str, places: dict[str, str], program: list[str],
                read_only: list[str], pythons: str | None, port: int, environment: dict[str, str] | None = None,
                cache_folder: str = "", rules_folder: str = "") -> str:
    """The walled service itself, below its header: what examples/hylly.service has, with this computer's values.
    places: Hylly's own folders by kind (paths.PLACES' kinds, and "job-output"); program: what ExecStart starts, given
    --in-background; read_only: the program's folders, which the walls must let it see; pythons: the folder of the
    Pythons uv installed, if the program's Python is one of them. Each of Hylly's folders is bound once, the one inside
    another with it; what the walls hide inside a bound folder (the logins, what long jobs said) must be there, or the
    service doesn't start, rather than start with a wall missing; elsewhere it's hidden where it is there (-). All in
    one folder, which the server writes, the settings' own files in it are walled read-only one by one: settings.toml
    and secrets/ must be there, or it doesn't start (else the server could make one itself), and your rules and themes
    folders, which no command makes, are walled where they are there (-). Made whole before it's returned: a folder
    no unit can hold as it is stops it at once (_unit_path_word), and so does a variable's value with a line break."""
    w = _unit_path_word
    folders = list(dict.fromkeys(places[kind] for kind in ("data", "state", "cache")))
    bound = [f for f in folders if not any(o != f and _in(f, o) for o in folders)]
    config = places["config"]
    together = any(_in(config, f) for f in bound)

    def hidden(path: str) -> str:
        return w(path) if any(_in(path, f) for f in bound) else "-" + w(path)

    parent = os.path.dirname(os.path.normpath(library))
    tmpfs = " ".join([*([] if parent == "/" or any(_in(parent, f) for f in NOT_HIDDEN_FROM) else [f"{w(parent)}:ro"]), "/run:ro"])
    words = " ".join([w(program[0]).replace("$", "$$"), *program[1:], "--in-background"])  # $ too, on a command line
    variables = [f"Environment={_quoted_setting(f'{k}={v}')}" for k, v in (environment or {}).items()]
    if together:
        settings_lines = [
            "# All in one folder, which the server writes, the settings' own files in it are walled one by one, each only to",
            "# read: settings.toml, your rules and themes, and the alarms' ntfy address and token in secrets/, so that nothing",
            "# the server could be made to write steers your own commands, nor where their notes and the token go.",
            "# settings.toml and secrets/ must be there before the server starts, or it doesn't start: else the server could",
            "# make one itself (every command makes secrets/, hylly settings edit settings.toml). Your rules and themes, which",
            "# nothing makes for you, have a -: walled once they are there at a start. An editor that writes a new",
            "# settings.toml in its place is seen by the server at its next start.",
            "ReadOnlyPaths=" + " ".join([*(w(os.path.join(config, name)) for name in SETTINGS_IN_ONE),
                                         *("-" + w(os.path.join(config, name)) for name in OPTIONAL_IN_ONE)])]
    else:
        settings_lines = [
            "# The settings folder: settings.toml, your rules and themes, and the alarms' ntfy address and token in secrets/. The",
            "# server reads them and never writes them, so nothing it could be made to write steers your own commands, nor where",
            "# their notes and the token go. The folder is bound, not each file, so the server sees a change to settings.toml",
            "# whichever editor makes it, one that writes a new file in its place too.",
            f"BindReadOnlyPaths={w(config)}"]
    if rules_folder and not _in(rules_folder, config):
        settings_lines += ["# Your rules, where rules = in the settings puts them: read, never written",
                           f"BindReadOnlyPaths=-{w(rules_folder)}"]
    lines = [
        "[Unit]",
        "Description=Hylly library server, walled in",
        "After=network-online.target",
        "Wants=network-online.target",
        f"RequiresMountsFor={w(library)}",
        "# A crash loop stops here, and a few restarts on purpose don't",
        "StartLimitIntervalSec=60",
        "StartLimitBurst=5",
        "",
        "[Service]",
        f"User={user}",
        f"Group={group}",
        "# What the server makes (covers, caches, the nightly copies in the library) is readable by all and writable by it alone,",
        "# systemd's own 0022. Only if others in its group must change those files too, as on a library shared with Samba to a",
        "# group, does it take this line:",
        "#UMask=0002",
        "# In the data folder, which is there inside the walls: not where the program is",
        f"WorkingDirectory={_unit_path_word(places['data'], quoted=False)}",
        "# hyllyd, as uv tool install put it there (the help's install, with tool-constraints.txt: the versions that were tested).",
        "# It writes server.log itself, in the state folder, which BindPaths below lets it write: it opens the file as its own",
        "# user, inside the walls, and keeps it short. Its messages go to the journal too. Not StandardOutput=append: root opens",
        "# that, outside the walls, and follows a link that the server could put in the log's place.",
        f"ExecStart={words}",
        "# The flag tells the server where it is. It restarts (hylly server restart, or Restart on the server page) by ending",
        "# with code 75: that is no failure, and the system starts it again.",
        f"Environment={SERVICE_FLAG}=1",
        "Environment=PYTHONUNBUFFERED=1",
        *variables,
        "Restart=on-failure",
        "RestartSec=2",
        f"SuccessExitStatus={RESTART_CODE}",
        f"RestartForceExitStatus={RESTART_CODE}",
        "",
        "# The disk: only these exist for it. 7-Zip, for CBR comics, if it's in your home folder, needs a line of its own.",
        f"TemporaryFileSystem={tmpfs}",
        f"BindPaths={w(library)}",
        "ProtectHome=tmpfs",
        "# Hylly's data (the database), its state (logs, locks, notes) and its cache (the reader's pages), which the server writes",
        "BindPaths=" + " ".join(w(f) for f in bound),
        "# The reader's cache in a folder of its own ([reader] cache_folder), on another disk, say: the - lets the server start while",
        "# that disk is away (it then keeps the pages in memory only). Restart the server once the disk is connected again.",
        f"BindPaths=-{w(cache_folder)}" if cache_folder else "#BindPaths=-/mnt/fast/hylly-cache",
        "# The shops' logins and the lookups' keys: hyllyd needs none of them, and doesn't even see the folder",
        f"InaccessiblePaths={hidden(places['logins'])}",
        "# Nor what the jobs started from another computer said, so that no link the server could leave there leads it elsewhere",
        f"InaccessiblePaths={hidden(places['job-output'])}",
        *settings_lines,
        "# The program, as uv tool install put it there. One installed from a checkout of the source (--editable) needs that",
        "# folder too, in a BindReadOnlyPaths= line of its own.",
        *(f"BindReadOnlyPaths={w(folder)}" for folder in read_only),
        *(["# The Python that uv installed, if it did (uv python list says where): the tool's own is a link to it",
           f"BindReadOnlyPaths=-{w(pythons)}"] if pythons else []),
        "# Names on the network, where systemd-resolved does them; the - lets a computer without it start too",
        "BindReadOnlyPaths=-/run/systemd/resolve",
        "",
        "# Other processes of your own: without this, /proc would lead through them to the whole disk",
        "PrivateUsers=yes",
        "",
        "# The rest of the computer",
        "ProtectSystem=strict",
        "InaccessiblePaths=-/etc/ssh -/etc/samba -/var/lib/samba -/var/lib/tailscale -/var/log -/dev/pts",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectProc=invisible",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "SystemCallErrorNumber=EPERM",
        "# No AF_UNIX: that's what keeps it from the sockets of other programs, on the disk or not",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK",
        "# The one port the server may listen on: [server] port in the settings (8770 as it comes). Change it here too, or the",
        "# server can't start (its log says the system refused the port). Tailscale Serve, Funnel or a proxy point at this port.",
        f"SocketBindAllow=tcp:{int(port)}",
        "SocketBindDeny=any",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        ""]
    return "\n".join(lines)


def _quoted_setting(text: str) -> str:
    """One Environment= value, in quotes: systemd expands % in it (doubled here), and takes \\ and " inside quotes. A line
    break or another control character, which no line of a unit holds, is refused."""
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise Exit(USAGE, f"{text.partition('=')[0]} has a line break or a control character in it, which a systemd unit can't "
                          "hold: set it without one, or write the service by hand from examples/hylly.service. Nothing was "
                          "printed.")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _walled_program() -> tuple[list[str], list[str], str | None]:
    """What the walled service starts, and the folders of the program it must see: hyllyd beside this Python, as uv
    tool install puts it there, else this Python with -P -m hylly.daemon; the environment it's installed in, and the
    source's own folder when that is elsewhere (installed from a checkout, --editable); and, when the Python it was made
    with isn't the system's own, its folder: for one uv installed, the folder of all its Pythons, so that the next one
    the tool's link leads to is in the walls too."""
    prefix = Path(sys.prefix).absolute()
    hyllyd = Path(sys.executable).absolute().parent / "hyllyd"  # the environment's own bin, not where its python leads
    program = [str(hyllyd)] if hyllyd.is_file() else [_python(), *DAEMON]
    read_only = [str(prefix)]
    source = Path(__file__).resolve().parent.parent  # the folder hylly is imported from
    if not source.is_relative_to(prefix.resolve()):
        read_only.append(str(source))
    base = Path(sys.base_prefix).resolve()
    pythons = None
    if not base.is_relative_to(prefix.resolve()) and not _in(str(base), "/usr"):
        pythons = str(base.parent if base.parent.name == "python" else base)
    return program, read_only, pythons


def _group() -> str:
    import grp

    return grp.getgrgid(os.getgid()).gr_name


def service_file() -> tuple[str, list[str], str]:
    """What hylly serve --service-file prints: the walled service (walled_unit) filled in for this computer from the
    settings, as this user, for its library, Hylly's own folders, the program and the settings' port, under a header
    that says what it is and how to install it; those commands, the owner's with sudo; and what else to know. Nothing
    is installed or written, and all of it is made before a word is printed. Linux only, and only for the user's usual
    Hylly: one set up apart from it (HYLLY_HOME elsewhere, a settings file of its own) has no say in what the whole
    computer runs."""
    if SYSTEM != "linux":
        raise Exit(USAGE, "The walled service is a systemd one, for Linux: run hylly serve --service-file on the Linux computer "
                          "that serves the library. Nothing was printed.")
    if not paths.usual_settings_folder():
        raise Exit(USAGE, f"The walled service is the whole computer's, and this Hylly has settings of its own ({paths.config_dir()}, "
                          f"{paths.settings_file()}): a service made from it would start a server with the usual ones. Nothing "
                          "was printed.")
    library = str(paths.library_root().absolute())
    port = int(settings.get("server")["port"])
    places = {kind: str(paths.place(kind).absolute()) for kind in paths.PLACES}
    places["job-output"] = str(paths.job_output_dir().absolute())
    program, read_only, pythons = _walled_program()
    environment = {name: os.environ[name] for name in KEPT_FOR_THE_SERVER
                   if os.environ.get(name) and name not in ("PATH", "HYLLY_SETTINGS")}
    rules_folder = str(rules.folder().absolute())
    unit = walled_unit(user=_me(), group=_group(), library=library, places=places, program=program, read_only=read_only,
                       pythons=pythons, port=port, environment=environment,
                       cache_folder=settings.get("reader")["cache_folder"], rules_folder=rules_folder)
    write = f"{this_hylly()} serve --service-file > hylly.service.new && mv hylly.service.new hylly.service"
    if os.path.lexists(WALLED_FILE):
        how = ("To put it in place of the one installed, see first what it would change, as a line added there by hand "
               "stays only if it's added here too:")
        steps = [write, f"diff {shlex.quote(WALLED_FILE)} hylly.service", "sudo cp hylly.service /etc/systemd/system/",
                 "sudo systemctl daemon-reload", "sudo systemctl restart hylly"]
    else:
        how = ("To install it, with the server stopped (hylly server stop) and no start at logon of your own (hylly server "
               "autostart off prints how to take one away):")
        steps = [write, "sudo cp hylly.service /etc/systemd/system/", "sudo systemctl daemon-reload",
                 "sudo systemctl enable --now hylly"]
    said = (f"This one is for this computer, as hylly serve --service-file printed it from the settings: for {_me()}, the "
            f"library in {library}, and port {port}. {how}")
    head = [*ABOUT, "#", *textwrap.wrap(said, 118, initial_indent="# ", subsequent_indent="# ", break_on_hyphens=False,
                                        break_long_words=False), *(f"#   {step}" for step in steps),
            "# Its messages: journalctl -u hylly, and server.log in the state folder (hylly logs server)."]
    named = [library, places["data"], places["state"], places["cache"], places["logins"],
             *([places["job-output"]] if _in(places["job-output"], places["state"]) else [])]
    missing = [folder for folder in dict.fromkeys(named) if not os.path.isdir(folder)]
    if any(_in(places["config"], f) for f in (places["data"], places["state"], places["cache"])):
        missing += [os.path.join(places["config"], name) for name in SETTINGS_IN_ONE
                    if not os.path.lexists(os.path.join(places["config"], name))]
    elif not os.path.isdir(places["config"]):
        missing.append(places["config"])
    also = ("Everything it names is there now." if not missing else
            "Not there yet, and needed before it starts: " + ", ".join(missing) + ". Any hylly command run as you makes Hylly's "
            "own folders and secrets/; hylly settings edit makes settings.toml; the library is there once its disk is.")
    return "\n".join(head) + "\n" + unit, steps, also


# ---- the walled service installed, against the settings: for hylly doctor ----

DROP_INS = (f"{UNIT}.d", "service.d")  # the walled service's own drop-ins, and those of every service
# Where systemctl set-property keeps what it sets, read before UNIT_FOLDERS for drop-ins, as systemd does
CONTROL_FOLDERS = ("/etc/systemd/system.control", "/run/systemd/system.control")


def _unit_lines(text: str) -> list[tuple[str, str, str]]:
    """A unit file's settings as systemd reads them (systemd.syntax): (section, key, value) in order, without the
    comments (# and ;) and the blank lines; a line that ends in a backslash goes on in the next, with a space for it."""
    found, section, held = [], "", None
    for raw in text.splitlines():
        line = raw.strip()
        if held is not None and line[:1] in ("#", ";"):  # a comment inside a line that goes on is left out
            continue
        if held is None and (not line or line[:1] in ("#", ";")):
            continue
        if line.endswith("\\"):
            held = (held or "") + line[:-1] + " "
            continue
        line, held = (held or "") + line, None
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif "=" in line:
            key, _, value = line.partition("=")
            found.append((section, key.strip(), value.strip()))
    if held is not None and "=" in held:
        key, _, value = held.partition("=")
        found.append((section, key.strip(), value.strip()))
    return found


def _listed_words(value: str, colons: bool = False) -> list[list[str]] | None:
    """A setting's list as systemd splits it: words at white space outside quotes, a word in "" or '' or with \\ before
    a character kept whole, and with colons each word cut at its colons too (a bind's source, destination and options),
    those quoted or escaped kept; %% is %. None for one systemd fills in itself (a specifier such as %h), or with a quote
    left open: it can't be told here."""
    if re.search(r"%[^%]|%$", value.replace("%%", "")):
        return None
    words, parts, word, started, quote, i = [], [], "", False, None, 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            word, started, i = word + value[i + 1], True, i + 2
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                word += c
        elif c in "\"'":
            quote, started = c, True
        elif c.isspace():
            if started:
                words.append([*parts, word])
            parts, word, started = [], "", False
        elif colons and c == ":":
            parts, word, started = [*parts, word], "", True
        else:
            word, started = word + c, True
        i += 1
    if quote:
        return None
    if started:
        words.append([*parts, word])
    return [[part.replace("%%", "%") for part in w] for w in words]


def _walled_files() -> tuple[list[Path], list[str]] | None:
    """The walled service's files, in the order systemd reads them: the unit itself where the system finds it first
    (_system_unit), then its drop-ins, hylly.service.d/*.conf and every service's service.d/*.conf, from CONTROL_FOLDERS
    (systemctl set-property's) and then UNIT_FOLDERS, each name taken from the first folder that has it, in the order of
    their names, wherever they are. And the drop-in folders that are there but can't be listed, which may hold anything.
    None without the unit."""
    unit = _system_unit()
    if unit is None:
        return None
    named: dict[str, Path] = {}
    unread = []
    for folder in (*CONTROL_FOLDERS, *UNIT_FOLDERS):
        for drop_in in DROP_INS:
            where = Path(folder, drop_in)
            try:
                names = sorted(os.listdir(where))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:  # there, and not to be listed: not passed over as if it were empty
                if os.path.lexists(where):
                    unread.append(str(where))
                continue
            for name in names:
                if name.endswith(".conf"):
                    named.setdefault(name, where / name)
    return [unit, *(named[name] for name in sorted(named))], unread


def _is_me(user: str) -> bool:
    """Whether a unit's User= is this user: by name, or by number."""
    return user in (_me(), str(getattr(os, "getuid", lambda: -1)()))


def _port_rule(rule: str) -> tuple[str | None, str | None, tuple[int, int] | None] | None:
    """A SocketBindAllow= or SocketBindDeny= rule: [ipv4|ipv6:][tcp|udp:][port or low-high], or any; (family, protocol,
    ports), None for what isn't given. None for one systemd wouldn't take, which it leaves out."""
    if rule == "any":
        return None, None, None
    family = protocol = ports = None
    parts = rule.split(":")
    if parts and parts[0] in ("ipv4", "ipv6"):
        family = parts.pop(0)
    if parts and parts[0] in ("tcp", "udp"):
        protocol = parts.pop(0)
    if len(parts) > 1:
        return None
    if parts and parts[0]:
        found = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", parts[0])
        if not found:
            return None
        low, high = int(found[1]), int(found[2] or found[1])
        if not 1 <= low <= high <= 65535:
            return None
        ports = (low, high)
    return family, protocol, ports


def _port_allowed(allow: list[str], deny: list[str], family: str, port: int) -> bool | None:
    """Whether the walls let the server listen on this TCP port, as systemd decides it: allowed when an allow rule
    matches, else refused when a deny rule does, else allowed. None when a rule can't be read: then it can't be told."""
    def matches(rules: list[str]) -> bool | None:
        read = [_port_rule(r) for r in rules]
        if any(r is None for r in read):
            return None
        return any((f in (None, family)) and (p in (None, "tcp")) and (ports is None or ports[0] <= port <= ports[1])
                   for f, p, ports in read)

    allowed, denied = matches(allow), matches(deny)
    if allowed:
        return True
    if allowed is None or denied is None:
        return None
    return not denied


# How the walls leave a path, by the mounts that name it: hidden, only to read, or to write. The deepest one decides, as
# systemd mounts them shortest path first; at one path the more restrictive wins (RESTRICTS), as systemd keeps it: a
# bind where InaccessiblePaths= or ReadOnlyPaths= names the same folder is hidden, or only to read
HIDES = ("TemporaryFileSystem", "InaccessiblePaths")
SHOWS_READ_ONLY = ("BindReadOnlyPaths", "ReadOnlyPaths")
SHOWS_WRITABLE = ("BindPaths", "ReadWritePaths")
RESTRICTS = {"writable": 1, "read-only": 2, "hidden": 3}
ONE_LIST = {"BindPaths": "BindReadOnlyPaths", "BindReadOnlyPaths": "BindPaths"}  # one list in systemd: either, empty, clears both
LISTED = (*HIDES, *SHOWS_READ_ONLY, *SHOWS_WRITABLE, "SocketBindAllow", "SocketBindDeny", "Environment", "EnvironmentFile")
YES = ("1", "yes", "y", "true", "t", "on")  # the words systemd takes for yes (its parse_boolean)
# Where the settings a server reads are told from: the same in the service's Environment= as here, or its port and its
# library from the settings aren't these settings'
SETTINGS_PLACE = ("HYLLY_SETTINGS", "HYLLY_HOME", "HYLLY_CONFIG_DIR", "XDG_CONFIG_HOME")


def _walled_reading(files: list[Path]) -> tuple[dict, list[str]]:
    """The settings of the walled service that the doctor compares, from its files in order: User=, ProtectHome=,
    ProtectSystem=, RootDirectory= and RootImage= (the last of each), and the lists (LISTED), each an empty assignment
    clearing what came before, as systemd has it, BindPaths= and BindReadOnlyPaths= together (ONE_LIST); each value
    with the file and the line it came from. And the files that couldn't be read."""
    found: dict = {"lists": {key: [] for key in LISTED}}
    unread = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unread.append(str(file))
            continue
        for section, key, value in _unit_lines(text):
            if section != "Service":
                continue
            line = (value, str(file), f"{key}={value}")
            if key in LISTED:
                if value:
                    found["lists"][key] = [*found["lists"][key], line]
                else:
                    found["lists"][key] = []
                    if key in ONE_LIST:
                        found["lists"][ONE_LIST[key]] = []
            elif key in ("User", "ProtectHome", "ProtectSystem", "RootDirectory", "RootImage"):
                found[key] = line
    return found, unread


def _what_shows(path: str, found: dict) -> tuple[str, tuple | None] | None:
    """How the walls leave a path: ("hidden", line), ("read-only", line) or ("writable", line), by the deepest mount that
    names it or a folder it's in (by the path as written, or as both lead through their links), the more restrictive
    at one path (RESTRICTS), and the line that does it; ("writable", None) when no wall names it. None when a line that
    may name it can't be read."""
    mounts = []  # (path, mode, line)
    protect_home = (found.get("ProtectHome") or ("",))[0].lower()
    if protect_home in (*YES, "tmpfs", "read-only"):
        mode = "read-only" if protect_home == "read-only" else "hidden"
        mounts += [(folder, mode, found["ProtectHome"]) for folder in ("/home", "/root", "/run/user")]
    protect_system = (found.get("ProtectSystem") or ("",))[0].lower()
    if protect_system == "strict":
        mounts.append(("/", "read-only", found["ProtectSystem"]))
    elif protect_system in (*YES, "full"):
        mounts += [(f, "read-only", found["ProtectSystem"]) for f in ("/usr", "/boot", "/efi")]
        mounts += [("/etc", "read-only", found["ProtectSystem"])] if protect_system == "full" else []
    for keys, mode in ((HIDES, "hidden"), (SHOWS_READ_ONLY, "read-only"), (SHOWS_WRITABLE, "writable")):
        for key in keys:
            for value, file, line in found["lists"][key]:
                words = _listed_words(value, colons=True)
                if words is None:
                    return None
                for parts in words:
                    if key.startswith("Bind"):  # source[:destination[:options]]: what the server sees is at the destination
                        where = parts[1] if len(parts) > 1 and parts[1] else parts[0]
                    else:
                        where = parts[0]  # a TemporaryFileSystem's :ro, another list's own path
                    mounts.append((where.lstrip("-+"), mode, (value, file, line)))
    written, real = os.path.normpath(path), os.path.realpath(path)
    best = None
    for where, mode, line in mounts:
        if not os.path.isabs(where):
            continue
        for mine, theirs in ((written, os.path.normpath(where)), (real, os.path.realpath(where))):
            if _in(mine, theirs):
                key = (len(theirs.rstrip("/").split("/")), RESTRICTS[mode])
                if best is None or key > best[0]:
                    best = (key, mode, line)
    return ("writable", None) if best is None else (best[1], best[2])


def _unit_environment(found: dict) -> dict[str, tuple[str, str]] | None:
    """The variables the service's Environment= lines give the server, each with the line that gave it (the last one
    counting, an empty Environment= clearing all before it); None when one can't be told here (a specifier)."""
    given: dict[str, tuple[str, str]] = {}
    for value, _, line in found["lists"]["Environment"]:
        words = _listed_words(value)
        if words is None:
            return None
        for parts in words:
            name, sep, said = parts[0].partition("=")
            if sep:
                given[name] = (said, line)
    return given


def _same_place(variable: str, unit: dict[str, tuple[str, str]]) -> bool:
    """Whether a variable that tells where the settings are says the same in the service as here: both unset, or the
    same folder or file, through links too; XDG_CONFIG_HOME unset as its default, ~/.config."""
    theirs, ours = (unit.get(variable) or ("",))[0], os.environ.get(variable, "")
    if variable == "XDG_CONFIG_HOME":
        theirs, ours = theirs or str(Path.home() / ".config"), ours or str(Path.home() / ".config")
    return theirs == ours or bool(theirs and ours and os.path.realpath(theirs) == os.path.realpath(ours))


def service_differences() -> dict | None:
    """For hylly doctor: the walled service installed (examples/hylly.service), as the system's own files say it (the unit
    and its drop-ins, _walled_files), against the settings, on the three things whose difference stops the server or
    loosens its walls: the user it runs the server as (User=), whether the walls let it see the library and write there
    (_what_shows), and whether they let it listen on the settings' port (SocketBindAllow=, SocketBindDeny=). The library
    is the one the server gets: its Environment= HYLLY_LIBRARY, else the settings' library, never this shell's. What the
    unit says another way is no difference: the user by number, a path through a link, a bind of a folder the library
    is in, a port range, a quoted path. Nor is what can't be told here, which is said instead (skipped): a specifier
    systemd fills in (%h), an EnvironmentFile=, settings the service names another way than this shell (SETTINGS_PLACE),
    a root of its own (RootDirectory=); and with a file or a drop-in folder that can't be read, nothing is compared. Each
    difference: the line and its file (or the file it's missing from), what it means, and the line wanted. None when
    this isn't the computer's walled service, or it's masked."""
    if not system_service():
        return None
    walled = _walled_files()
    if not walled:
        return None
    files, folders = walled
    found, unread = _walled_reading(files)
    if unread or folders:  # a drop-in that can't be read may say anything: nothing is called a difference then
        return {"files": [str(f) for f in files], "differences": [], "unread": [*folders, *unread], "skipped": []}
    if not files[0].read_text(encoding="utf-8", errors="replace").strip():
        return None  # masked: the doctor's look at what it starts says so
    differences, skipped, me = [], [], _me()
    user = found.get("User")
    if user is None or not user[0]:
        differences.append({"line": user[2] if user else "", "file": user[1] if user else str(files[0]),
                            "says": "it names no user to run the server as (User=), so systemd would start it as root, "
                                    "which Hylly refuses: it can't start", "want": f"User={me}"})
    elif user[0][:1] in "\"'":  # systemd takes User= as it is
        differences.append({"line": user[2], "file": user[1], "want": f"User={me}",
                            "says": f"{user[2]} is in quotes, which systemd keeps as part of the name: there's no such user, "
                                    "and the service doesn't start"})
    elif _listed_words(user[0]) is None:
        skipped.append(f"the user, as {user[2]} has systemd fill in a part of it")
    elif not _is_me(user[0]):
        differences.append({"line": user[2], "file": user[1],
                            "says": f"it runs the server as {user[0]}, not as you, {me}, whose folders its walls name",
                            "want": f"User={me}"})
    unit_env = _unit_environment(found)
    told = unit_env is not None and not found["lists"]["EnvironmentFile"]
    if unit_env is None:
        skipped.append("the library and the port, as an Environment= line has systemd fill in a part of it")
    elif found["lists"]["EnvironmentFile"]:
        skipped.append(f"the library and the port, as {found['lists']['EnvironmentFile'][-1][2]} may set the variables that "
                       "say where the library and the settings are")
    here = told and all(_same_place(variable, unit_env) for variable in SETTINGS_PLACE)
    if told and not here:
        named = ", ".join(v for v in SETTINGS_PLACE if not _same_place(v, unit_env))
        skipped.append(f"the port and the settings' library, as the service names its settings another way than this "
                       f"shell does ({named})")
    library, whose = None, ""
    if told and (unit_env.get("HYLLY_LIBRARY") or ("",))[0]:
        library, line = unit_env["HYLLY_LIBRARY"]
        whose = f"the library its {line} names"
    elif here:
        library, whose = paths.setting("library"), "the library the settings name"  # settings.toml's, never this shell's
    if library and (found.get("RootDirectory") or found.get("RootImage")):
        skipped.append("the library, as the service has a root of its own (RootDirectory= or RootImage=)")
        library = None
    shows = _what_shows(os.path.abspath(library), found) if library else None
    if library and shows is None:
        skipped.append("the library, as a line that may name it has systemd fill in a part of it")
    if shows and shows[0] != "writable":
        said = ("hides it, and no BindPaths= brings it back" if shows[0] == "hidden" else
                "lets the server see it only to read, so it can't write its covers, caches and nightly copies there")
        try:
            want = f"BindPaths={_unit_path_word(os.path.abspath(library))}"
        except SystemExit:  # a path no printed unit holds: the bind to write by hand
            want = f"a BindPaths= line for {library}"
        differences.append({"line": shows[1][2], "file": shows[1][1], "want": want,
                            "says": f"{shows[1][2]} {said}: {whose}, {library}"})
    if here:
        port, listen = int(settings.get("server")["port"]), str(settings.get("server")["listen"])
        allow, deny = found["lists"]["SocketBindAllow"], found["lists"]["SocketBindDeny"]
        family = "ipv6" if ":" in listen else "ipv4"  # where [server] listen says: "::" is an IPv6 socket, IPv4 too inside it
        if _port_allowed([value for value, _, _ in allow], [value for value, _, _ in deny], family, port) is False:
            allowed = " and ".join(line for _, _, line in allow) or "no SocketBindAllow= line"
            differences.append({"line": allowed if allow else deny[-1][2], "file": (allow or deny)[-1][1],
                                "says": f"{allowed}, with {deny[-1][2]}, doesn't let the server listen on port {port}, the "
                                        "settings' [server] port: the system refuses it, and the server can't start",
                                "want": f"SocketBindAllow=tcp:{port}"})
    return {"files": [str(f) for f in files], "differences": differences, "unread": [], "skipped": skipped}
