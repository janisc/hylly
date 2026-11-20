"""Hylly's commands run on the server computer, from another one (docs/architecture.md). The server computer
owns the library's data, so the commands run where it is: hylly -H HOST names it as ssh knows it (an alias in
~/.ssh/config, or user@host), else HYLLY_REMOTE, else [remote] ssh in this computer's settings, as docker -H and
podman --remote do; hylly passes each command there, says so on standard error, and shows here what it says.

- Over ssh, with a terminal when there's one here, so a prompt works (hylly login's paste) and Ctrl+C stops the command.
- A long job (one that holds a job's lock, cli.job_of, and those of cli.STARTED_APART: hylly download, each of whose
  shops' runs is one, and hylly server jobs, whose own long jobs write where it does; never a dry run, which holds
  none) starts on the server on its own (hylly internal job, as_job), so closing this window or losing the connection
  doesn't stop it. Its output follows here until it ends; Ctrl+C stops only the following, and
  hylly jobs stop NAME stops the job. hylly jobs shows it from any computer.
- What's about this computer stays here (LOCAL), and so does any command given --here. What's passed on carries --here
  too, so a server whose own settings name another can't pass it further.
- What a job says goes to a file of its own in paths.job_output_dir(), made new (never one that's there already, nor
  through a link) in a folder the walled server can't see: a link left in its way would otherwise write a job's
  output into any file of yours.
- The server computer runs Linux or macOS. The words are quoted for a Unix shell (command_line), and a long job
  is kept going as Unix does it (as_job). Windows' ssh server hands the line to cmd.exe instead, where the quotes are
  letters of the words, and & or | in one would act on the server; a Windows server that can tell refuses it
  (from_another_shell). Quoting for cmd.exe, and a job that outlives ssh there, are left for later.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import paths
from .exits import NOT_SET_UP, USAGE, Exit
from .storage import is_link

LOCAL = ("copy", "backup", "restore", "demo", "commands", "settings")  # about this computer, its disks and its settings file
LOCAL_JOBS = ("copy", "backup")  # their jobs (a restore's is backup's): hylly jobs stop NAME of one stays here too
HERE = "--here"
JOB = ["internal", "job"]  # a long job, started on the server on its own (cli.cmd_internal)
GLOBAL = {"-H": True, "--remote": True, "--here": False}  # hylly's own options, before the command: whether each takes a value
_chosen: dict = {}  # the command's own -H or --here (choose)
NO_HOST = ("{where} names no server computer: give it the server's name as ssh knows it, such as an alias in ~/.ssh/config "
           "or user@host, or {instead}. Nothing was run.")
OUTPUTS_KEPT = 50  # of the jobs started from elsewhere
WINDOWS_SERVER = ("This computer runs Windows, and the command came from another computer's hylly over ssh ([remote] "
                  "ssh, -H or HYLLY_REMOTE there), which needs a Linux or macOS server computer for now: Windows' ssh "
                  "server hands the command to cmd.exe, which takes the quotes around its words for letters of them, and "
                  "a long job would end with the connection. Nothing was run. Run the command on this computer itself, "
                  "or keep the library's server on a Linux or macOS computer.")
OUTPUT_NAME = re.compile(r"\d{4}-\d\d-\d\d_\d{6}-[A-Za-z0-9_-]+(\.err)?\.txt")  # what as_job names them, and all it removes


def choose(host: str | None, here: bool = False) -> None:
    """The command's own choice: -H HOST, or --here for none; neither leaves it to HYLLY_REMOTE and the settings. cli.main
    sets it for each command, and clears it when the command ends. -H with no name is a mistake, not a way to run here:
    that's --here."""
    _chosen.clear()
    if here:
        _chosen["here"] = True
    elif host is not None:
        if not host.strip():
            raise Exit(USAGE, NO_HOST.format(where="-H", instead="--here to run it on this computer"))
        _chosen["host"] = host.strip()


def _section() -> dict:
    """[remote] alone, with its own two checks: a mistake elsewhere in the settings, a wrong [reader] value say, is no
    reason a command can't be passed on, nor hylly doctor run where it can say what the mistake is. A file that can't be
    read as TOML at all still stops it (paths.section)."""
    found, out = paths.section("remote"), {}
    for key in ("ssh", "programs"):
        value = found.get(key, "")
        if not isinstance(value, str):
            raise Exit(NOT_SET_UP, f"{paths.settings_file()}: [remote] {key} = {value!r}: a text in quotes.")
        out[key] = value
    return out


def named() -> bool:
    """Whether a server computer is named at all, by -H, HYLLY_REMOTE or [remote] ssh, without asking if the name will do:
    for what runs here whatever is named, and only says so. Never raises."""
    if _chosen.get("here"):
        return False
    if "host" in _chosen or os.environ.get("HYLLY_REMOTE", "").strip():
        return True
    try:
        return bool(_section()["ssh"].strip())
    except Exit:
        return False


def server() -> tuple[str, str] | None:
    """(the ssh name of the server computer, the folder of Hylly's programs there, or "" for its PATH), if one is named:
    by -H (choose), else HYLLY_REMOTE, else [remote] ssh in the settings; None with --here. A name that ssh would take
    for an option, or with spaces or the like in it, is refused: whatever can write the settings or the environment
    mustn't choose ssh's options."""
    if _chosen.get("here"):
        return None
    found = _section()
    host, where = _named_by(found)
    if where == "HYLLY_REMOTE" and not host:
        raise Exit(NOT_SET_UP, NO_HOST.format(where="HYLLY_REMOTE, set and empty,", instead="unset it"))
    folder = str(found.get("programs") or "").strip()
    if not host:
        return None
    if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%\[\]-]+", host):
        raise Exit(NOT_SET_UP, f"{where} should be the server computer's name as ssh knows it, such as an alias in "
                               "~/.ssh/config or user@host, with no spaces, and not starting with -.")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in folder):
        raise Exit(NOT_SET_UP, f"[remote] programs in {paths.settings_file()} should be a folder on the server, such as ~/.local/bin.")
    return host, folder


def _named_by(found: dict) -> tuple[str, str]:
    """(the server computer's name, what names it): -H (choose), else HYLLY_REMOTE, else [remote] ssh in the settings
    (found: [remote] as _section reads it). The name as given, maybe empty: server() checks it."""
    if "host" in _chosen:
        return _chosen["host"], "-H"
    if "HYLLY_REMOTE" in os.environ:
        return os.environ["HYLLY_REMOTE"].strip(), "HYLLY_REMOTE"
    return str(found.get("ssh") or "").strip(), f"[remote] ssh in {paths.settings_file()}"


def named_by() -> str:
    """What names the server computer a command was passed on to, for the words when ssh couldn't reach it: -H,
    HYLLY_REMOTE, or [remote] ssh in this computer's settings, with the file."""
    return _named_by(_section())[1]


def wanted(command: str | None, job: str | None = None) -> bool:
    """Whether a command runs on the server computer: not one about this computer (LOCAL), nor the stop of a job of one
    (job: what hylly jobs stop names), as those run where they're typed."""
    return bool(command) and command not in LOCAL and not (command == "jobs" and job in LOCAL_JOBS) and server() is not None


def words(argv: list[str]) -> list[str]:
    """The command as typed, without hylly's own options before it (-H HOST, --remote=HOST, --here): what's passed on. A
    --library given there goes after the command's own words instead, where every hylly takes it, and so does hylly
    internal job's command, which takes nothing before its own word."""
    kept, at = [], 0
    while at < len(argv):
        word = argv[at]
        name = word.split("=", 1)[0]
        if name in GLOBAL:
            at += 2 if GLOBAL[name] and "=" not in word else 1
        elif word.startswith("-H") and len(word) > 2:  # -HHOST
            at += 1
        elif name == "--library":  # hylly --library FOLDER COMMAND: taken as hylly COMMAND --library FOLDER
            took = 1 if "=" in word else 2
            kept += argv[at:at + took]
            at += took
        else:
            break
    rest = argv[at:]
    end = rest.index("--") if "--" in rest else len(rest)  # before a -- of the command's, after which all are its words
    return rest[:end] + kept + rest[end:]


def command_line(argv: list[str], job: bool, folder: str) -> str:
    """What ssh runs there: hylly, from its folder (a ~ at its start is the server's home), with --here, and the words as
    they were, each quoted."""
    exe = f"{folder.rstrip('/')}/hylly" if folder else "hylly"
    exe = "~/" + shlex.quote(exe[2:]) if exe.startswith("~/") else shlex.quote(exe)
    return " ".join([exe, *(shlex.quote(w) for w in [HERE, *(JOB if job else []), *argv])])


def from_another_shell(argv: list[str]) -> bool:
    """A command passed on to this computer that it can't run as it was meant: this is Windows, the command came over
    ssh from another computer's hylly (--here first, in an ssh session), and either it's a long job, which nothing here
    would keep going once ssh ends, or a word of it has a single quote at its start or end, as quoting for a Unix shell
    leaves it when cmd.exe reads the line (command_line). A command with nothing to quote arrives as it was
    typed, and runs."""
    if not paths.WINDOWS or argv[:1] != [HERE] or not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT")):
        return False
    return argv[1:3] == JOB or any(word[:1] == "'" or word[-1:] == "'" for word in argv[1:])


def _ssh() -> str:
    """ssh, as a terminal finds it on the PATH, but never in the current folder: on Windows a program there is found
    first, and a folder that others can write (the Inbox, say) could hold one. Raises FileNotFoundError without one."""
    if os.name != "nt":
        return "ssh"
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH")
    for folder in [*os.environ.get("PATH", "").split(os.pathsep), system]:
        folder = folder.strip().strip('"')
        if folder and os.path.isabs(folder) and os.path.isfile(os.path.join(folder, "ssh.exe")):
            return os.path.join(folder, "ssh.exe")
    raise FileNotFoundError("ssh")


def forward(argv: list[str], job: bool) -> int:
    """The command, run on the server computer; its exit code. One line on standard error says where, as the output
    that follows is the server's."""
    host, folder = server()
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    print(f"Running on {host}.", file=sys.stderr, flush=True)
    try:
        ssh = [_ssh(), *(["-t"] if terminal else ["-T"]), "-o", "ServerAliveInterval=30", "--", host, command_line(argv, job, folder)]
        return subprocess.run(ssh).returncode
    except FileNotFoundError:
        raise Exit(NOT_SET_UP, f"The server computer is {host}, but there's no ssh on this computer to reach it. "
                               "Install OpenSSH's client, or run the command with hylly --here.") from None
    except KeyboardInterrupt:
        return 130


def ask(argv: list[str], text: str = "", timeout: float = 120) -> str:
    """A command the server runs at once, with text on its stdin: a note of a backup made here to keep, or a fresh copy
    of the database to make for one. Returns what it said; raises OSError if it can't be reached, or says no."""
    host, folder = server()
    try:
        done = subprocess.run([_ssh(), "-T", "-o", "ServerAliveInterval=30", "--", host, command_line(argv, False, folder)],
                              input=text.encode("utf-8"), capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"{host} couldn't be asked: {e}") from None
    if done.returncode:
        raise OSError(f"{host} said no: {done.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return done.stdout.decode("utf-8", "replace")


def _outputs() -> Path:
    folder = paths.job_output_dir()
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if is_link(folder) or not folder.is_dir():
        raise Exit(NOT_SET_UP, f"{folder}, where long jobs keep what they say, should be a folder of its own, not a link: "
                               "move it aside, and Hylly makes it again.")
    return folder


def _new_file(path: Path) -> int:
    """A file made now for writing, readable by its owner only: never one that's there already, nor through a link."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)


def as_job(argv: list[str]) -> int:
    """On the server: the command started on its own, apart from this ssh session, with its result and what it says on
    the way each in a file of its own (.txt and .err.txt), so that the two stay apart as they are here; followed here,
    each to its own stream, until it ends. Its exit code, or 0 when only the following was stopped."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", "-".join(w for w in argv[:2] if not w.startswith("-")))[:40] or "job"
    folder = _outputs()
    out = folder / f"{datetime.now():%Y-%m-%d_%H%M%S}-{name}-{secrets.token_hex(3)}.txt"
    err = out.with_suffix(".err.txt")
    with os.fdopen(_new_file(out), "wb") as fh, os.fdopen(_new_file(err), "wb") as said:  # -P: not the current folder
        job = subprocess.Popen([sys.executable, "-P", "-m", "hylly", HERE, *argv], stdin=subprocess.DEVNULL, stdout=fh,  # first
                               stderr=said, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    kept = sorted(p for p in folder.iterdir() if OUTPUT_NAME.fullmatch(p.name) and not is_link(p) and p.is_file())
    for old in kept[:-2 * OUTPUTS_KEPT]:  # two for each job
        old.unlink(missing_ok=True)
    from .runs import JOBS

    stop = f"hylly jobs stop {argv[0]}" if argv and argv[0] in JOBS else "hylly jobs stop NAME"
    print(f"Started on {socket.gethostname()} as a job of its own: closing this window doesn't stop it. Ctrl+C stops following "
          f"it here; {stop} stops the job. What it says is also in {out}, and on the way in {err.name}.", file=sys.stderr, flush=True)
    try:
        with open(out, "rb") as result, open(err, "rb") as notes:
            while True:
                ended = job.poll() is not None
                got = False
                for fh, stream in ((notes, sys.stderr), (result, sys.stdout)):
                    chunk = fh.read()
                    if chunk:
                        got = True
                        stream.buffer.write(chunk)
                        stream.flush()
                if ended:
                    break
                if not got:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStill running on the server: hylly jobs shows it.", file=sys.stderr, flush=True)
        return 0
    return job.returncode
