"""HyllyBell, alarms about the library server: the rare things worth a look, sent at once; everything else in a daily
summary.

Bots try every address on the internet (/.env, /wp-login.php, /admin): that is expected, and only counted. An alarm:
  wrong passwords  a real user's name with wrong passwords (wrong_passwords of them within 15 minutes), or any address
                   locked out after 10 wrong ones; bots guessing names like "admin" don't count
  new device       a login from a kind of device (iPad · Safari, Windows · Firefox, Yomu) not seen before for that user;
                   a user renamed keeps theirs, and a later user of the old name has none (Watch._follow_accounts)
  downloads        one login, or one address, getting downloads_per_hour different files within an hour
  users            a user added or removed, or a role or password changed
  a person's data  what's kept about a user couldn't all be read, or is from a newer version of Hylly
  the library      it hasn't been there for library_missing minutes: a disk that didn't come back after a restart, say.
                   The server runs from elsewhere, so it can say so. Said once, again each day, and when it's back
  a late copy      the database's nightly copy not made for a day and a half: one that hangs or fails each night, or
                   nothing running the server's jobs. Said once for each copy it's late after
  a review         words someone wrote wait for an owner to look at them ([reviews] moderation): how many, never whose
  a report         a title newly reported as a bad file: how many wait, never which or whose
The same alarm comes at most every quiet_minutes, and all of them together at most alarms_per_hour, so that a flood can't
use up what the phone service allows and lose a real alarm later; the ones held back are in bell.log, and one message
says how many. Each goes to bell.log (in the state folder; the server page
shows the latest), to a notification on this computer as set under [bell] in settings.toml, and to ntfy, a free push app
for phones (https://ntfy.sh, or your own ntfy server), at the address in secrets/ntfy-url in the settings folder, with
the token in ntfy-token beside it if the server wants one (or the environment's HYLLY_NTFY_URL: settings.secret_from).
Beside ntfy, a program of yours if command under [bell] names one, for e-mail or a webhook, say (run_command).
The daily summary says what was refused, who logged in from what, and what was downloaded, read from the access log.

Your phone's messages pass through someone else's service, which keeps them a while, so by default they say what happened
in counts and kinds: no names, addresses or file names, and nothing a stranger typed. Those go to bell.log and this
computer's notification (remote_detail under [bell] turns them on for the phone too). Long jobs send their notes the same
way (job_note).

Nothing here knows particular users, addresses or files: the server reports events (access.Gate's logins, each
request), and the settings say what counts.
"""

from __future__ import annotations

import hashlib
import contextlib
import atexit
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import USER_AGENT, access, instance, notify, paths
from .settings import DEFAULTS as SETTINGS, SECRETS, get as settings_section, secret
from .sites import SITES
from .storage import append_log, aside, atomic_write, read_json, shown
from .words import many

# [bell]'s keys and their defaults, in the table every section has (settings.py), and the two secrets, kept apart
DEFAULTS = {"ntfy": "", "ntfy_token": "", **SETTINGS["bell"]}
LOG_LIMIT = 1 << 20  # bell.log moves to bell.log.1 beyond this
URGENT = {"users": 3, "held": 1, "library": 3, "copies": 1}  # places in the hour kept for these, on top of the others: they must get through
LIBRARY_LOOK = 30.0  # seconds a look at the library may take: a dying disk can hang whatever asks it, and that means it isn't there
LIBRARY_AGAIN = 86400.0  # said again this often while it's gone, in case the first went unseen
COPY_LATE = 36 * 3600.0  # seconds without a nightly copy of the database before that is an alarm: a day and a half
COPY_LOOK = 3600.0  # how often the alarms look at the copies, which are on the library's disk
STRONG_TOPIC = 20  # letters, on ntfy.sh, where the topic is all that keeps the alarms private
SOMETHING = ("Library alarm", "There is something to look at: it's in bell.log and on the server page.")
NOTE_WAIT = 20.0  # seconds a command waits at its end for a job's note: a slow phone service must not hold up the job
WRITER_WAIT = 60.0  # seconds one waits for another program to be done with bell-jobs.json
NOT_REACHED = "ntfy not reached"
# [bell] command: the seconds it has, as ntfy has, as a program that only hands an alarm on is stuck if it takes longer,
# and a job's note waits at most NOTE_WAIT for both; and its words in bell.log, the latest of which says how it stands
# (command_trouble)
COMMAND_LIMIT = 15.0
COMMAND_FAILED, COMMAND_WORKS = "The alarm command failed", "The alarm command works again"
# Hylly's own secrets, which a command never gets from its environment: a long job's note comes from a download that
# may have a shop's cookie there, and the server may have the ntfy address and token
HIDDEN = {variable for _, variable in SECRETS.values()} | {variable for _, _, variable in SITES.values()}
NOT_FILES = ("/catalog/", "/opds", "/ereader/opds", "/api/", "/login", "/logout", "/server", "/ping",
             "/foliate/", "/fonts/",  # the reader's own program files and fonts, fetched whenever a book opens
             "/licences/",  # the licences of the work by others Hylly includes, which the help links to
             "/favicon", "/apple-touch-icon", "/icon-", "/manifest.webmanifest",  # the page's icons, for the home screen
             "/theme.css")  # the pages' colours
LINE = re.compile(r'^(\S+) - (\S+) \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')


def settings() -> dict:
    """[bell] in settings.toml over the defaults: each key checked, and one it doesn't have told, as in every section
    (settings.py); with the ntfy address and token from where the secrets are kept."""
    return {**{key: value for key, value in settings_section("bell").items() if key in DEFAULTS},
            "ntfy": secret("ntfy-url"), "ntfy_token": secret("ntfy-token")}


def log_file() -> Path:
    return paths.place("state") / "bell.log"


def state_file() -> Path:
    return paths.place("state") / "bell.json"


def job_state_file() -> Path:
    """Long jobs' own, and hylly server jobs' (periodic.py): a running server keeps bell.json in its memory, and would
    write over what they remember. As two kinds of program keep it, each reads, changes and writes it while no other
    does (one_writer)."""
    return paths.place("state") / "bell-jobs.json"


@contextlib.contextmanager
def one_writer(wait: float = WRITER_WAIT):
    """bell-jobs.json for this program alone while the block runs: a Watch on it made, used and written inside, so that
    one program's count of what went out (alarms_per_hour, quiet_minutes) isn't written over by another's. An operating
    system's lock beside it, which goes with a program that ends however it ends. Send what's to go after the block: a
    slow phone service must not hold up the other."""
    path = job_state_file().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        until = time.monotonic() + wait
        while not instance._lock(fh):
            if time.monotonic() > until:
                raise OSError(f"{path} is held by another program")
            threading.Event().wait(0.1)
        try:
            yield
        finally:
            instance._unlock(fh)


def read_state(path: Path) -> dict:
    """What's right in a state file. A damaged one must not stop the server or lose an alarm: what's wrong is forgotten."""
    saved = read_json(path)
    when = lambda t: isinstance(t, (int, float)) and not isinstance(t, bool)  # noqa: E731
    state = {}
    devices, last, sent, held, users, summary_day = (saved.get(k) for k in ("devices", "last", "sent", "held", "users", "summary"))
    state["devices"] = {u: [k for k in kinds if isinstance(k, str)] for u, kinds in devices.items() if isinstance(kinds, list)} if isinstance(devices, dict) else {}
    whose = saved.get("whose")  # each device list's account, by its marks (Watch._follow_accounts)
    state["whose"] = {u: list(m) for u, m in whose.items() if isinstance(m, list) and len(m) == 2
                      and all(isinstance(x, str) for x in m)} if isinstance(whose, dict) else {}
    state["last"] = {k: t for k, t in last.items() if when(t)} if isinstance(last, dict) else {}  # "kind|key" -> when it last went out,
    state["sent"] = [t for t in sent if when(t)] if isinstance(sent, list) else []  # so a restart doesn't say it all again
    state["held"] = held if isinstance(held, int) and not isinstance(held, bool) and held > 0 else 0
    if isinstance(users, dict) and all(isinstance(u, list) and len(u) == 2 for u in users.values()):
        state["users"] = users
    if isinstance(summary_day, str):
        state["summary"] = summary_day
    if isinstance(saved.get("copies"), str):  # the last nightly copy a late one was said after
        state["copies"] = saved["copies"]
    library = saved.get("library")  # the library last seen, and if it's gone, since when, and when that was said
    if isinstance(library, dict) and isinstance(library.get("root"), str):
        state["library"] = {"root": library["root"], **{k: library[k] for k in ("gone", "told") if when(library.get(k))}}
    return state


def channels(conf: dict) -> list[str]:
    """Where alarms go, in words (never the ntfy topic, which is as good as a password to read them)."""
    out = ["bell.log"]
    if conf["toast"]:
        out.append("notifications on this computer")
    if conf["ntfy"]:
        try:
            out.append(f"ntfy ({_server(urlsplit(conf['ntfy'])) or '?'})")
        except ValueError:
            out.append("ntfy (its address looks mistyped)")
    if conf.get("command"):
        out.append(f"the command {program(conf['command'])}")
    return out


def program(words: list[str]) -> str:
    """A command's program by its name alone, as it's shown: its arguments may hold an address or a key."""
    return re.split(r"[\\/]", words[0])[-1] or words[0]


_seen: dict[str, Path] = {}  # a library's path as written -> as its links resolved when it was last there


def remember_library(root) -> None:
    """The library as its links resolve, taken where it is known to be there: the alarms' look at it, aside
    (Watch._library_there), and 7-Zip's check as a comic is read (archives.sevenzip). server_can_write compares with it,
    never resolving the library itself at an alarm: "the library can't be reached" is one, when a share may hang
    whatever asks it."""
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        _seen[os.path.abspath(root)] = Path(root).resolve()


def server_can_write(path: str, now: bool = False) -> bool:
    """In a place that a server with walls around it can change (examples/hylly.service): any of Hylly's own folders on
    this computer (paths.PLACES), the secrets in the settings folder too, or the library. Your own commands run what's checked so too, 7-Zip (archives.sevenzip) and [bell] command,
    a download with the shops' logins within reach among them, so a flaw in the server mustn't get to choose it. The
    library by its path as written and as it resolved when it was last there (remember_library), so that an alarm
    never touches its disk; now: resolved now too, where it's in use anyway (7-Zip, as a comic is read). A path
    written inside a place counts before anything is asked of the disk about it."""
    places = _places(now)
    return _written_inside(path, places) or any(paths._resolved(Path(path)).is_relative_to(place) for place in places)


def _places(now: bool = False) -> list[Path]:
    places = list(dict.fromkeys(paths._resolved(paths.place(kind)) for kind in paths.PLACES))
    with contextlib.suppress(SystemExit, OSError):  # no library set: only Hylly's own folders, then
        library = paths.library_root(None)
        if now:
            remember_library(library)
        written = os.path.abspath(library)
        places += [Path(written), *([_seen[written]] if written in _seen else [])]
    return places


def _written_inside(path: str, places: list[Path]) -> bool:
    """A full path, as it's written, in one of the places: no disk asked."""
    return os.path.isabs(path) and any(Path(os.path.abspath(path)).is_relative_to(place) for place in places)


def _named(word: str) -> set[str]:
    """The paths a word of a command may name: itself, after an = (--config=/path), after a short option (-K/path),
    each also after curl's @ (@/path, -d@/path, --data=@/path)."""
    bases = {word, word.partition("=")[2], word[2:] if re.match(r"-[^-]", word) else ""}
    return {named for base in bases for named in (base, base[1:] if base.startswith("@") else "")} - {""}


def command_refused(words: list[str]) -> str:
    """Why [bell] command isn't run, or "": its program, or a file one of its words names (_named), where the server
    can write (server_can_write). Looked at before every run, as a file can be put there, or a link pointed there, at
    any time; a full path written inside the library is refused before anything is asked of its disk."""
    import shutil

    places = _places()
    program_file = shutil.which(words[0]) if not re.search(r"[\\/]", words[0]) else words[0]
    named = [program_file] if program_file else []
    for word in words[1:]:
        for part in _named(word):
            with contextlib.suppress(OSError, ValueError):
                if _written_inside(part, places) or Path(part).is_file():
                    named.append(part)
    inside = next((path for path in named if server_can_write(path)), None)
    return (f"{inside} is in one of Hylly's own folders or the library, so it isn't run: the server can write there, and your "
            "own commands run the alarm command too" if inside else "")


def run_command(words: list[str], title: str, text: str, priority: str, limit: float | None = None) -> str:
    """[bell] command, for one alarm: the program run by its words, never by a shell, with the alarm in its environment,
    HYLLY_ALARM_TITLE, HYLLY_ALARM_TEXT, HYLLY_ALARM_PRIORITY and HYLLY_ALARM_TIME, and on its standard input, the
    title, a blank line and the text, as mail and curl take it; cut as ntfy cuts them. Never on its command line, where
    anyone on this computer may read it, and a name someone typed may be in a text. Hylly's secrets aren't in its
    environment (HIDDEN). What it says on standard output goes nowhere, and of its standard error only the end is kept
    (TAIL), so neither can hold it up or fill the memory. It has limit seconds (COMMAND_LIMIT): one past them is
    stopped, with what it started (_stop), and this waits a moment more at most. "" when it ended well, else what went
    wrong, in words for bell.log; it never raises."""
    name, limit = program(words), COMMAND_LIMIT if limit is None else limit
    refused = command_refused(words)
    if refused:
        return refused
    title, text = cut(title, 200), cut(text, 3500)
    alarm = {"HYLLY_ALARM_TITLE": title, "HYLLY_ALARM_TEXT": text, "HYLLY_ALARM_PRIORITY": priority,
             "HYLLY_ALARM_TIME": datetime.now().astimezone().isoformat(timespec="seconds")}
    env = {**{k: v for k, v in os.environ.items() if k.upper() not in HIDDEN},
           **{k: v.replace("\0", "") for k, v in alarm.items()}}  # a NUL can't be in the environment
    try:
        process = subprocess.Popen(words, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                                   start_new_session=not paths.WINDOWS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return f"{words[0]} wasn't found"
    except (OSError, ValueError) as e:
        return f"{name} couldn't start ({getattr(e, 'strerror', None) or type(e).__name__})"
    job = _windows_job(process) if paths.WINDOWS else None
    with _running_lock:
        _running[process.pid] = running = [process, job, False]  # False: not stopped by stop_commands
    said = bytearray()
    threading.Thread(target=_feed, args=(process.stdin, f"{title}\n\n{text}\n".encode("utf-8")), daemon=True).start()
    reading = threading.Thread(target=_tail, args=(process.stderr, said), daemon=True)
    reading.start()
    try:
        process.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        stopped = _stop(process, job)
        return f"{name} hadn't ended after {limit:g} seconds, and " + ("was stopped" if stopped else "couldn't be stopped")
    finally:
        with _running_lock:
            _running.pop(process.pid, None)
        _close_job(job)
    if running[2]:
        return f"{name} was stopped by a Ctrl-C, or as what ran it ended"
    if process.returncode:
        reading.join(1)  # its last words; a moment only, as something it left running may keep its standard error open
        last = bytes(said).decode("utf-8", "replace").strip().splitlines()[-1:]
        words_said = shown(last[0])[:200] if last else ""  # the one cleaner every log line goes through
        return f"{name} ended with code {process.returncode}" + (f": {words_said}" if words_said else "")
    return ""


TAIL = 4096  # bytes of a command's standard error kept, its end: where a program says why it failed
_running: dict[int, list] = {}  # the commands running now, by process id: the process, its job object on Windows, stopped
_running_lock = threading.Lock()


def _feed(stream, data: bytes) -> None:
    """The alarm on the command's standard input, aside: a program that doesn't read it mustn't hold anything up."""
    with contextlib.suppress(OSError, ValueError):
        stream.write(data)
        stream.close()


def _tail(stream, said: bytearray) -> None:
    """The command's standard error as it comes, keeping only its end: a program that says a lot fills no memory."""
    with contextlib.suppress(OSError, ValueError):
        while chunk := stream.read1(1 << 16):
            said.extend(chunk)
            del said[:-TAIL]


def _kernel():
    """Windows' kernel32, with the types of what a job object takes, on an instance of its own (pdfworker.py does so)."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def _windows_job(process: subprocess.Popen):
    """On Windows, a job object holding the command, so that a stop ends what it started too (a .cmd's curl, a
    PowerShell's child), as pdfworker.py's worker holds itself in one. None where it can't be made: then only the
    program itself is stopped. Its closing kills nothing: what a command leaves running when it ends well is its own."""
    try:
        kernel = _kernel()
        job = kernel.CreateJobObjectW(None, None)
        if job and kernel.AssignProcessToJobObject(job, int(process._handle)):
            return job
        if job:
            kernel.CloseHandle(job)
    except (OSError, AttributeError, ValueError):
        pass
    return None


def _close_job(job) -> None:
    if job:
        with contextlib.suppress(OSError, AttributeError):
            _kernel().CloseHandle(job)


def _stop(process: subprocess.Popen, job=None) -> bool:
    """A command past its time, with what it started: on Linux and macOS its process group (start_new_session makes
    one of it), on Windows its job object. What it started in a group or session of its own, as a program that detaches
    itself does, can't be told from any other program, and runs on. True when the program itself is known to have
    ended, after a moment at most."""
    with contextlib.suppress(OSError, AttributeError):
        if not paths.WINDOWS:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            if job:
                _kernel().TerminateJobObject(job, 1)
            process.kill()
    try:
        process.wait(timeout=2)
        return True
    except subprocess.TimeoutExpired:
        return False


def stop_commands() -> None:
    """Every command still running, stopped as one past its time is: at a Ctrl-C during a job's note or hylly bell
    test, and as this program ends, since in a session of its own it hears no Ctrl-C, and nothing else would end it."""
    with _running_lock:
        running = list(_running.values())
    for each in running:
        if each[0].poll() is None:
            each[2] = True
            _stop(*each[:2])


atexit.register(stop_commands)


def command_trouble(lines: int = 400) -> tuple[str, str] | None:
    """When the command last failed and why, while that is bell.log's latest word on it, for hylly doctor; None once a
    run has worked since (Watch._ran says so there), or when the log's end, and the one before it moved aside
    (bell.log.1), have nothing of it. Only by a line's start, its time and "command: "."""
    log = log_file()
    for line in reversed(access.tail(log.with_suffix(".log.1"), lines) + access.tail(log, lines)):
        found = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) command: (.*)", line)
        if found:
            if not found[2].startswith(COMMAND_FAILED):
                return None
            return found[1], re.sub(rf"^{COMMAND_FAILED}\W*", "", found[2])
    return None


def weak_topic(conf: dict) -> bool:
    """A topic on the public ntfy.sh that someone could guess, and then read the alarms; a token or your own server is another matter."""
    try:
        url = urlsplit(conf["ntfy"])
    except ValueError:  # a mistyped address: said when an alarm goes out, and no reason to stop the server
        return False
    return url.netloc.lower() == "ntfy.sh" and not conf["ntfy_token"] and len(url.path.rstrip("/").rpartition("/")[2]) < STRONG_TOPIC


def _server(url) -> str:
    """The server's name and port, without a user:password@ someone wrote there."""
    return (url.hostname or "") + (f":{url.port}" if url.port else "")


def masked(target: str) -> str:
    """An ntfy address fit to show: the topic is as good as a password for reading the alarms, so a short one shows nothing
    of itself and a long one only its end, enough to tell it from another."""
    try:
        url = urlsplit(target)
        topic = url.path.rstrip("/").rpartition("/")[2]
        return f"{url.scheme}://{_server(url)}/...{topic[-4:] if len(topic) >= 12 else ''}"
    except ValueError:
        return "(an address that looks mistyped)"


def cut(text: str, limit: int) -> str:
    """At most limit bytes, which is how ntfy counts, without leaving half a letter."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def device(agent: str) -> str:
    """A kind of device from a User-Agent: "iPhone · Safari", "Windows · Firefox", "Yomu (app)". An iPad's Safari
    says it is a Mac, so that is "Mac or iPad"."""
    a = agent or ""
    if "Mozilla/" not in a:
        name = re.match(r"[A-Za-z][\w.-]*", a)
        return f"{name[0][:40]} (app)" if name else "unknown app"  # what any client calls itself
    system = next((s for key, s in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac or iPad"),
                                    ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Linux", "Linux")) if key in a), "other")
    browser = next((b for pattern, b in ((r"Edg[A-Za-z]*/", "Edge"), (r"Firefox/|FxiOS/", "Firefox"), (r"OPR/", "Opera"),
                                         (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari")) if re.search(pattern, a)), "browser")
    return f"{system} · {browser}"


def typed(name: str) -> str:
    """A name someone typed at the login page, as an alarm quotes it, as the server's log does (access.typed): short,
    and nothing that could start a line, move the cursor or turn the text round. So too a path a stranger asked for, in
    the daily summary."""
    return access.typed(name)


NAME_MOST = 40  # characters of a name an alarm quotes, as typed() cuts one


def file_named(name: str) -> str:
    """A file's name as the downloads alarm quotes it: cleaned and quoted as typed() quotes a name, and at most as long,
    but a longer one cut in its middle, with …, so that its end, the kind of file, stays."""
    name = shown(name)
    if len(name) > NAME_MOST:
        name = f"{name[:NAME_MOST - 13]}…{name[-12:]}"
    return repr(name)


def is_file(path: str) -> bool:
    """A book, comic or other file of the library, not the page, its covers, feeds or the server's own addresses."""
    return path != "/" and not path.startswith(NOT_FILES) and "." in path.rsplit("/", 1)[-1]


def server_page() -> str:
    """The Server page's address, for an alarm's notification on this computer to open when clicked (notify.toast, where
    the system allows it): [server] address, the one people use, else this computer's own; "" where it can't be told."""
    import ipaddress

    try:
        conf = settings_section("server")
        base = str(conf.get("address") or "").rstrip("/")
        if not base:
            listen = ipaddress.ip_address(str(conf.get("listen") or "127.0.0.1"))
            host = "localhost" if listen.is_loopback or listen.is_unspecified else str(listen)
            base = f"http://{f'[{host}]' if ':' in host else host}:{int(conf['port'])}"
        return base + "/server"
    except (KeyError, ValueError, TypeError):
        return ""

def ntfy_message(target: str, title: str, text: str, priority: str) -> tuple[str, dict]:
    """ntfy's JSON publishing (UTF-8 safe, unlike its headers): the server's address and the message."""
    url = urlsplit(target)
    base, _, topic = url.path.rstrip("/").rpartition("/")
    if url.scheme not in ("https", "http") or not url.netloc or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        raise ValueError("the ntfy address (secrets/ntfy-url in the settings folder) should be like https://ntfy.sh/your-topic")
    return f"{url.scheme}://{url.netloc}{base}", {
        "topic": topic, "title": cut(title, 200), "message": cut(text, 3500),
        "priority": {"high": 4, "default": 3}.get(priority, 2),
        "tags": [{"high": "warning", "default": "books"}.get(priority, "bar_chart")]}


def job_note(title: str, text: str, public: str = "", wait: float | None = None) -> bool:
    """From a long job: to the same places as an alarm, less loud, with a count of its own by the same limits. public: its
    text for the phone, without names or paths. It never gets in the way of the job, or changes how it ends: a slow phone
    service gets a moment, and Ctrl-C here ends only the waiting, and a command of yours ([bell] command) running for it.
    False if it isn't known to have reached the phone, for phone_again: the phone service couldn't be reached, or hadn't
    answered when the waiting ended. wait: seconds, instead of NOTE_WAIT."""
    reached: list[bool | None] = [None]

    def send() -> None:
        try:
            going: list[tuple] = []
            with one_writer():  # hylly server jobs keeps bell-jobs.json too
                watch = Watch(log=lambda message: None, state=job_state_file())
                watch._send = lambda *note: going.append(note)
                watch.tell_of_held()
                watch.alarm("job", hashlib.sha256(f"{title}|{text}".encode()).hexdigest()[:16], title, text,
                            public=(title, public or "bell.log says more."), priority="default")
            for note in going:  # here, as this thread is the background
                reached[0] = reached[0] is not False and not any(_worth_again(p) for p in watch.push(*note))
        except (SystemExit, Exception):  # noqa: BLE001
            reached[0] = True  # no settings to go by, or the like: a second try would fare no better
        if reached[0] is None:
            reached[0] = True  # said just now, or over the limit: nothing was to go

    from . import db

    sending = threading.Thread(target=db.carried(send), daemon=True)  # a dry run's, with its databases
    sending.start()
    try:
        sending.join(NOTE_WAIT if wait is None else wait)
    except KeyboardInterrupt:
        stop_commands()  # in a session of its own, a command of yours hears no Ctrl-C: it ends with the waiting
        with contextlib.suppress(KeyboardInterrupt):
            sending.join(5)  # a moment, for bell.log to have the stop
    return reached[0] is True


def _worth_again(problem: str) -> bool:
    """Trouble that passes, with the note surely not delivered: the service not reached, down for a moment, or asking to wait.
    An answer lost on its way back may mean the note went, and going again could send it any number of times."""
    return bool(re.match(rf"{NOT_REACHED} \((ConnectError|ConnectTimeout|PoolTimeout)\)|ntfy said (429|5\d\d)", problem))


def phone_again(title: str, text: str, public: str = "") -> bool:
    """A job's note that couldn't reach the phone, to the phone alone: everywhere else it has been, a command of yours
    too. True when it's off your hands, also if that's because the settings can't be read."""
    try:
        watch = Watch({**settings(), "toast": False, "command": []}, log=lambda message: None, state=job_state_file())
        return not any(_worth_again(p) for p in watch.push(title, text, "default", (title, public or "bell.log says more.")))
    except (SystemExit, Exception):  # noqa: BLE001
        return True


INBOX_DONE = "inbox.json"  # in the library's state folder: what the Inbox's passes did (folders.state_file names it)


def inbox_said(added: int, found: int | None, aside: int, already: int) -> str:
    """What the Inbox's passes did, in counts, as the phone has it: "3 books added from the Inbox, details found for 2,
    and 1 set aside in Not added." found: for how many of the books added details were looked up; None when none of
    them was to be looked up (lookup = false). "" for nothing."""
    parts = []
    if added:
        parts.append(f"{many(added, 'book')} added from the Inbox" + (f", details found for {found}" if found is not None else ""))
    if already:
        parts.append(f"{already:,} already in the library removed from it" if parts else
                     f"{many(already, 'book')} already in the library removed from the Inbox")
    if aside:
        parts.append(f"{aside:,} set aside in Not added" if parts else f"{many(aside, 'book')} from the Inbox set aside in Not added")
    if not parts:
        return ""
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]
    return text[:1].upper() + text[1:] + "."


def inbox_day(library: Path | None, now: float) -> str:
    """The daily summary's line of the Inbox: what its passes did in the last 24 hours, read from the library's state
    folder; "" for nothing, or what can't be read, which is no reason to hold up the summary."""
    if library is None:
        return ""
    done = read_json(paths.state_dir(library) / INBOX_DONE).get("done")
    rows = [row for row in (done if isinstance(done, list) else []) if isinstance(row, list) and len(row) == 5
            and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in row) and 0 <= now - row[0] < 86400]
    looked = [row[2] for row in rows if row[2] >= 0]
    said = inbox_said(sum(int(row[1]) for row in rows), int(sum(looked)) if looked else None,
                      sum(int(row[3]) for row in rows), sum(int(row[4]) for row in rows))
    return f"Inbox: {said}" if said else ""


def request_path(target: str) -> str:
    """The path of a request line's target. A scanner's target can be one urlsplit refuses (http://[bad): then the target
    up to its query, so that one odd line never stops the daily summary."""
    try:
        return urlsplit(target).path
    except ValueError:
        return target.split("?", 1)[0]


def summary(lines: list[str], now: datetime, counts_only: bool = False) -> str:
    """The last 24 hours from access-log lines: what was refused without a login, who logged in from what, the downloads.
    counts_only: how many of each, and no names, addresses or paths, for what passes through someone else's service."""
    since = now - timedelta(hours=24)
    refused, addresses, devices, files, errors = Counter(), set(), defaultdict(set), defaultdict(set), 0
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        ip, user, when, target, status, agent = m[1], m[2], m[3], m[5], int(m[6]), m[7]
        try:
            if datetime.strptime(when, "%d/%b/%Y:%H:%M:%S %z") < since:
                continue
        except ValueError:
            continue
        path = request_path(target)
        if user == "-" and status in (401, 403, 404, 405, 429, 501):
            refused["/".join(path.split("/")[:3])[:40] or "/"] += 1
            addresses.add(ip)
        if user != "-" and status < 400:
            devices[user].add(device(agent))
        if status in (200, 206) and is_file(path):
            files[user if user != "-" else ip].add(path)
        errors += status >= 500 and status != 501  # 501: a bot's OPTIONS or PROPFIND, which this server doesn't do
    parts = []
    if counts_only:
        if refused:
            parts.append(f"Refused: {many(sum(refused.values()), 'request')} without a login, from {many(len(addresses), 'address', 'addresses')}.")
        if devices:
            kinds = len({d for seen in devices.values() for d in seen})
            parts.append(f"Logins: {many(len(devices), 'user')}, {many(kinds, 'kind')} of device.")
        if files:
            parts.append(f"Downloads: {many(sum(len(p) for p in files.values()), 'file')}.")
        if errors:
            parts.append(f"Server errors: {errors}.")
        return "\n".join(parts) or "Quiet: nothing refused, no logins, no downloads."
    if refused:  # paths strangers chose, which bell.log, hylly bell and a phone told in full repeat: typed
        top = ", ".join(f"{typed(p)} ({n})" for p, n in refused.most_common(4))
        parts.append(f"Refused: {many(sum(refused.values()), 'request')} without a login, from {many(len(addresses), 'address', 'addresses')}, "
                     f"most often {top}.")
    if devices:
        parts.append("Logins: " + "; ".join(f"{u} ({', '.join(sorted(d))})" for u, d in sorted(devices.items())) + ".")
    if files:
        parts.append("Downloads: " + ", ".join(f"{w} {len(p)}" for w, p in sorted(files.items())) + ".")
    if errors:
        parts.append(f"Server errors: {errors}.")
    return "\n".join(parts) or "Quiet: nothing refused, no logins, no downloads."


class Watch:
    """Turns the server's events into alarms. Thread-safe; an alarm never holds up or breaks a request."""

    def __init__(self, conf: dict | None = None, *, send=None, now=time.time, log=print, state: Path | None = None, library: Path | None = None) -> None:
        self.conf = conf if conf is not None else settings()
        self.library, self._looking = library, None  # the folder to keep an eye on, and the look at it under way
        self._copies_looked = -COPY_LOOK  # when the copies were last looked at
        self.now, self._log = now, log
        self._state_file = state or state_file()
        self._send = send or self._deliver
        self._lock = threading.Lock()
        self._wrong: dict[str, list[tuple[float, str]]] = defaultdict(list)  # real user -> (when, address)
        self._got: dict[str, dict[str, float]] = defaultdict(dict)  # login or address -> file -> when
        self._state = read_state(self._state_file)

    # ---- events ----
    def event(self, kind: str, **data) -> None:
        """An event, handled by _on_<kind>: "login", "wrong" and "locked" from the login check (access.Gate.on_event);
        "people", "review" and "report" from the server (serve.py)."""
        try:
            getattr(self, f"_on_{kind}")(**data)
        except Exception as e:  # noqa: BLE001 - an alarm must never break a login
            self._log(f"Alarms: {kind} not handled ({type(e).__name__}).")

    def request(self, ip: str, user: str | None, method: str, path: str, status: int) -> None:
        """Every answered request: the downloads."""
        try:
            limit = self.conf["downloads_per_hour"]
            if limit > 0 and method == "GET" and status in (200, 206) and is_file(path):
                who = user or ip
                with self._lock:
                    got = {p: t for p, t in self._got[who].items() if self.now() - t < 3600}
                    got[path] = self.now()
                    self._got[who] = got
                if len(got) >= limit:
                    latest = file_named(path.rsplit("/", 1)[-1])
                    self.alarm("downloads", who, f"Many downloads: {who}",
                               f"{who} got {many(len(got), 'file')} within an hour, the latest {latest}. "
                               f"Expected? (downloads_per_hour = {limit} under [bell])",
                               public=("Many downloads", f"One login or address got {many(len(got), 'file')} within an hour. Expected?"))
        except Exception as e:  # noqa: BLE001
            self._log(f"Alarms: a request not handled ({type(e).__name__}).")

    def _on_login(self, user: str, ip: str, agent: str = "", via: str = "") -> None:
        if not self.conf["new_devices"]:
            return
        marks = self._follow_accounts() or {}  # renamed since the last look: their devices are theirs under the new name
        kind = device(agent)
        with self._lock:
            known = self._state["devices"].setdefault(user, [])
            if user in marks:
                self._state["whose"][user] = list(marks[user])
            if kind in known:
                return
            known.append(kind)
            self._keep_state()
        self.alarm("device", f"{user}|{kind}", f"New device: {user}",
                   f"{user} logged in from {kind}{f' ({via})' if via else ''}, address {ip}. A device not seen before for "
                   f"{user}: fine if it was them.",
                   public=("New device", "A login from a kind of device not seen before for that user: fine if it was them."))

    def _follow_accounts(self) -> dict[str, tuple[str, str]] | None:
        """Each device list where its user is now: a user renamed keeps their devices, with no alarm for them under the
        new name, and a later user of the old name has none. The lists go by name, as logins do, and a rename is a
        command's, in the database, while the server holds bell.json in its memory: so each list is kept with the marks
        of its user's account (whose: access.account_marks), which a rename keeps, and at each login and each minute's
        look at the users (check_users) a list goes to the user with its password's mark, or stays with its name while
        that's a user added when it was (a new password); else its user is gone, and so is the list. A list without
        marks, kept before lists had them, is its name's while that's a user's, as it always was. A rename made while the
        server runs is followed at its next login or look, not at once: a save before that still has the list under the
        old name, with its marks beside it (whose), so that login or look moves it all the same; one made while the
        server was stopped, at its first. The users as read, or None when they couldn't be: then, or with no users at
        all, nothing moves or goes, as there's no one to follow (a database put back without its users, say)."""
        import sqlite3

        from . import db

        try:
            marks = access.account_marks()
        except (db.Newer, db.Unusable, sqlite3.Error, OSError):
            return None
        if not marks:
            return marks
        counted = Counter(mark for mark, _ in marks.values())
        by_mark = {mark: name for name, (mark, _) in marks.items() if counted[mark] == 1}  # a copied hash names no one
        with self._lock:
            devices, whose = self._state["devices"], self._state["whose"]
            kept: dict[str, list[str]] = {}
            held: dict[str, list[str]] = {}
            for name, kinds in devices.items():
                was = whose.get(name)
                if was is None:
                    owner = name if name in marks else None
                else:
                    owner = by_mark.get(was[0]) or (name if name in marks and marks[name][1] == was[1] else None)
                if owner is None:
                    continue
                into = kept.setdefault(owner, [])
                into += [kind for kind in kinds if kind not in into]
                held[owner] = list(marks[owner])
            if kept != devices or held != whose:
                self._state["devices"], self._state["whose"] = kept, held
                self._keep_state()
        return marks

    def _on_people(self, name: str, what: str) -> None:
        self.alarm("people", name, f"What's kept for {name}", what,
                   public=("What's kept for a user", "What's kept about one user couldn't all be read, or is from a newer version of Hylly. "
                                                     "Nothing was thrown away. The details are in bell.log on the server."))

    def _on_review(self, waiting: int) -> None:
        """Words someone wrote wait for an owner to look at them: how many, never what they say or whose."""
        wait = "waits" if waiting == 1 else "wait"
        self.alarm("review", "waiting", "A review waits for you", f"{many(waiting, 'review')} {wait} for you to look at "
                   f"{'it' if waiting == 1 else 'them'}: the library page, Manage, Reviews to check.",
                   public=("A review waits for you", f"{many(waiting, 'review')} {wait} for you in the library page: Manage, Reviews to check."),
                   priority="default")

    def _on_report(self, waiting: int) -> None:
        """A title was reported: how many wait, never which or whose."""
        wait = "waits" if waiting == 1 else "wait"
        self.alarm("report", "waiting", "A title was reported", f"{many(waiting, 'reported title')} {wait} for you to look at "
                   f"{'it' if waiting == 1 else 'them'}: the library page, Manage, Reported titles.",
                   public=("A title was reported", f"{many(waiting, 'reported title')} {wait} for you in the library page: Manage, Reported titles."),
                   priority="default")

    def _on_wrong(self, name: str, ip: str, known: bool) -> None:
        limit = self.conf["wrong_passwords"]
        if not known or limit <= 0:  # names that aren't users are bots guessing: only the lockout counts them
            return
        with self._lock:
            recent = [(t, a) for t, a in self._wrong[name] if self.now() - t < access.WINDOW] + [(self.now(), ip)]
            self._wrong[name] = recent
        if len(recent) >= limit:
            others = len({a for _, a in recent} - {ip})
            self.alarm("wrong", name, f"Wrong passwords for {name}",
                       f"{many(len(recent), 'wrong password')} for {name} within {access.WINDOW // 60} minutes, from {ip}"
                       + (f" and {many(others, 'other address', 'other addresses')}" if others else "")
                       + f". If it wasn't {name}, someone is guessing; after {access.FAILS} wrong ones an address waits.",
                       public=("Wrong passwords", f"{many(len(recent), 'wrong password')} for a real user within {access.WINDOW // 60} minutes. "
                                                  f"After {access.FAILS} wrong ones an address waits."))

    def _on_locked(self, ip: str, name: str, known: bool = True) -> None:
        # one alarm for all addresses within quiet_minutes: guessing from many addresses is one guesser, not many alarms.
        # A name that isn't a user's comes as the login check's words for it, which say only its length (access.unknown_name)
        self.alarm("locked", "", "An address locked out",
                   f"{ip} is refused for {access.WINDOW // 60} minutes after {access.FAILS} wrong passwords "
                   f"(last tried as {typed(name) if known else name}). Others in the next {many(self.conf['quiet_minutes'], 'minute')} aren't announced.",
                   public=("An address locked out", f"An address is refused for {access.WINDOW // 60} minutes after {access.FAILS} wrong "
                                                    f"passwords. Others in the next {many(self.conf['quiet_minutes'], 'minute')} aren't announced."))

    # ---- once a minute: the users, the library, and the daily summary ----
    def start(self) -> None:
        """As the server starts: a word on a topic short enough to guess. The look once a minute (tick) is one of the
        server's periodic jobs (periodic.py), which a timer may run instead."""
        if weak_topic(self.conf):
            self._log("Alarms: the ntfy topic is short enough to guess, and whoever does can read the alarms. hylly bell, in a "
                      "terminal, suggests a stronger one.")

    def tick(self) -> None:
        self.tell_of_held()
        self.check_users()
        self.check_library()
        self.check_copies()
        self.maybe_summary()

    def check_users(self) -> None:
        from . import db

        try:
            accounts = access.load_accounts()
        except (db.Newer, db.Unusable):  # a database this version can't use: told of where it's opened
            return
        self._follow_accounts()  # the devices of a user renamed, removed or added since the last look
        now = {n: [r, hashlib.sha256(h.encode()).hexdigest()[:16]] for n, (r, h) in accounts.items()}
        before = self._state.get("users")
        with self._lock:
            self._state["users"] = now
            self._keep_state()
        if before is None:  # the first look: nothing to compare with
            return
        changes = [f"{n} added ({now[n][0]})" for n in sorted(now.keys() - before.keys())]
        changes += [f"{n} removed" for n in sorted(before.keys() - now.keys())]
        for n in sorted(now.keys() & before.keys()):
            if now[n][0] != before[n][0]:
                changes.append(f"{n}: {before[n][0]} → {now[n][0]}")
            if now[n][1] != before[n][1]:
                changes.append(f"{n}: a new password")
        if changes:
            text = "; ".join(changes) + ". Normally that was you, with hylly user."
            self.alarm("users", hashlib.sha256(text.encode()).hexdigest(), "Users changed", text,
                       public=("Users changed", f"{many(len(changes), 'change')} to users or their roles and passwords. "
                                                "Normally that was you, with hylly user."))

    def _library_there(self) -> bool:
        """Its marker can be read. Anything else counts here, a dead disk's errors too, as it's only said after minutes of it;
        and asked aside, as a disk that's going can hang whoever asks, and the other alarms must go on."""
        if self._looking is not None and self._looking.is_alive():  # the last look hasn't come back: no more of them to hang
            return False
        answer = []

        def look() -> None:
            try:
                paths.marker_file(self.library).stat()
                answer.append(True)
            except OSError:
                answer.append(False)
                return
            remember_library(self.library)  # there: as its links resolve, for server_can_write at an alarm

        self._looking = threading.Thread(target=look, daemon=True, name="library-look")
        self._looking.start()
        self._looking.join(LIBRARY_LOOK)
        return bool(answer and answer[0])

    def check_library(self) -> None:
        minutes = self.conf["library_missing"]
        if self.library is None or minutes <= 0:
            return
        there, root, now = self._library_there(), str(self.library), self.now()
        back = due = False
        with self._lock:
            seen = self._state.get("library", {})
            if there:
                back = "told" in seen
                if seen != {"root": root}:
                    self._state["library"] = {"root": root}
                    self._keep_state()
            elif seen.get("root") == root:  # one never seen here, as a folder not made a library yet, hasn't gone anywhere
                if "gone" not in seen:
                    seen["gone"] = now
                    self._keep_state()
                due = now - seen["gone"] >= minutes * 60 and now - seen.get("told", now - LIBRARY_AGAIN) >= LIBRARY_AGAIN
        # Only what went out counts as said: a disk that came and went a moment ago may be kept quiet by quiet_minutes, and is tried again.
        if due and self.alarm("library", "", "The library can't be reached",
                              f"{root} hasn't been reachable for {many(minutes, 'minute')} or more. The server is up, but has no books to offer. Is the disk plugged in and "
                              "mounted, or the share up?",
                              public=("The library can't be reached", f"It hasn't been reachable for {many(minutes, 'minute')} or more. The server is up, but has no books to offer. "
                                                                       "Is the disk plugged in and mounted, or the share up?")):
            with self._lock:
                seen["told"] = now
                self._keep_state()
        if back:
            self.alarm("library", "back", "The library is back", f"{root} can be reached again.",
                       public=("The library is back", "It can be reached again."), priority="default")

    def _summary_sent_elsewhere(self, day: str) -> bool:
        """Whether the day's summary went out from the other of the two that send it, the server (bell.json) and hylly
        server jobs (bell-jobs.json): a switch between them ([server] jobs) mustn't send it twice. Only read."""
        other = job_state_file() if self._state_file == state_file() else state_file()
        return read_state(other).get("summary") == day

    def check_copies(self) -> None:
        """The database's nightly copy not made for COPY_LATE: a copy that hangs or fails every night, or nothing running
        the server's jobs (jobs = "external" and no timer). Only where this computer's database keeps the library's
        records, as only that one is copied (db.nightly), and while the library is there, as its own alarm says when
        it isn't. Said once for each copy it's late after, as any alarm is (quiet_minutes, alarms_per_hour); looked at
        every COPY_LOOK, as the copies are on the library's disk."""
        if self.library is None or self.now() - self._copies_looked < COPY_LOOK:
            return
        self._copies_looked = self.now()
        import sqlite3

        from . import db

        try:
            if paths.library_missing(self.library) or not db.file().is_file():
                return
            db._copied_here(self.library, db.file())  # NotHere: the records are another database's, copied there
            last = db.last_backup(self.library)
            made = last[1].get("made") if last else None
            since = (datetime.fromisoformat(made).timestamp() if isinstance(made, str) else
                     datetime.strptime(last[0], "%Y-%m-%d").timestamp() if last else db.owner_file(self.library).stat().st_mtime)
        except (db.NotHere, db.Newer, db.Unusable, sqlite3.Error, OSError, ValueError):
            return
        after = last[0] if last else "none"
        if self.now() - since <= COPY_LATE or self._state.get("copies") == after:
            return
        when = f"since {last[0]}" if last else "yet"
        if self.alarm("copies", after, "The nightly copy is late",
                      f"The database hasn't been copied to {db.backups(self.library)} {when}: more than a day and a half. Is "
                      "the server running its jobs, or hylly server jobs on its timer with jobs = \"external\"? Their log "
                      "says why a copy failed. hylly backup makes a fresh one meanwhile.",
                      public=("The nightly copy is late", "The library's database hasn't been copied for more than a day and a "
                                                          "half. The server's log says why.")):
            with self._lock:
                self._state["copies"] = after
                self._keep_state()

    def maybe_summary(self) -> None:
        hour, now = self.conf["daily_summary"], datetime.fromtimestamp(self.now()).astimezone()
        day = now.date().isoformat()
        if hour < 0 or now.hour < hour or self._state.get("summary") == day or self._summary_sent_elsewhere(day):
            return
        with self._lock:
            self._state["summary"] = day
            self._keep_state()
        lines = []
        for path in (aside(access.access_log_file()), access.access_log_file()):
            try:  # a line ends at a line break only: U+0085 and the like are a sender's, inside one (access.tail)
                lines += path.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                pass
        inbox = inbox_day(self.library, self.now())  # in counts, as the phone may have it too
        text, counts = (f"{words}\n{inbox}" if inbox else words for words in (summary(lines, now), summary(lines, now, counts_only=True)))
        self._record("summary", "The last 24 hours", text)
        self._send("Library: the last 24 hours", text, "low", ("Library: the last 24 hours", counts))

    # ---- sending ----
    def _room(self, kind: str) -> bool:
        """Whether one more may go out within alarms_per_hour. Remembered over a restart, as is the count the phone service keeps."""
        now, cap = self.now(), self.conf["alarms_per_hour"]
        sent = self._state["sent"] = [t for t in self._state.get("sent", []) if 0 <= now - t < 86400]  # 0: a clock put back
        limit = cap + URGENT.get(kind, 0) if cap > 0 else 0
        return sum(1 for t in sent if now - t < 3600) < limit and len(sent) < limit * 10

    def _keep_state(self) -> None:
        with contextlib.suppress(OSError):  # the alarm matters more than remembering it
            self._save()

    def alarm(self, kind: str, key: str, title: str, text: str, public: tuple[str, str] | None = None, priority: str = "high") -> bool:
        """Sends an alarm, unless the same one went out within quiet_minutes; if there have been too many lately it only
        goes to bell.log, and tell_of_held mentions it. public: its title and text for what passes through someone else's
        service. True if it went."""
        with self._lock:
            quiet, now, name = self.conf["quiet_minutes"] * 60, self.now(), f"{kind}|{key}"
            last = self._state["last"] = {k: t for k, t in self._state["last"].items() if 0 <= now - t < quiet}
            if name in last:
                return False
            last[name] = now
            going = self._room(kind)
            if going:
                self._state["sent"].append(self.now())
            else:
                self._state["held"] = self._state.get("held", 0) + 1
            self._keep_state()
        self._record(kind, title, text)
        if going:
            self._send(title, text, priority, public)
        return going

    def tell_of_held(self) -> None:
        """One message for the alarms held back, as soon as there is room for it."""
        with self._lock:
            held = self._state.get("held", 0)
            if not held or not self._room("held"):
                return
            self._state["held"] = 0
            self._state["sent"].append(self.now())
            self._keep_state()
        text = f"{many(held, 'more alarm')} held back, over the limit of alarms_per_hour under [bell]. They are in bell.log."
        self._send("More alarms", text, "high", ("More alarms", text))

    def _record(self, kind: str, title: str, text: str) -> None:
        """A line in bell.log, its lines joined by /, and cleaned as every log line is (storage.append_log)."""
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {kind}: {title}. {' / '.join(text.splitlines())}"
        with self._lock:
            try:
                append_log(log_file(), line, LOG_LIMIT)
            except OSError as e:
                self._log(f"Alarms: bell.log not written ({e.strerror}).")

    def _deliver(self, title: str, text: str, priority: str, public: tuple[str, str] | None = None) -> None:
        """In the background: a slow notification or phone service must not hold up a request."""
        from . import db

        threading.Thread(target=db.carried(self.push), args=(title, text, priority, public), daemon=True).start()

    def push(self, title: str, text: str, priority: str, public: tuple[str, str] | None = None) -> list[str]:
        """To each channel set; what didn't work, in words that never show the ntfy topic. This computer gets it all; the
        phone, through someone else's service, what public says, unless you asked for it all (remote_detail). A program
        of yours ([bell] command) gets what the phone gets, as it hands it on to some service too, and runs at the same
        time as ntfy, with or without it: neither waits for the other, and this returns once both are done."""
        problems = []
        far = (title, text) if self.conf.get("remote_detail") == "full" else public or SOMETHING
        command, ran = self.conf.get("command") or [], []

        def run() -> None:
            try:
                ran.append(run_command(command, *far, priority))
            except Exception as e:  # noqa: BLE001 - a program of yours must never stop the alarms
                ran.append(f"{program(command)} couldn't be run ({type(e).__name__})")

        running = threading.Thread(target=run, daemon=True, name="alarm-command") if command else None
        if running is not None:
            running.start()
        if self.conf["toast"] and priority != "low" and not notify.toast(title, text, server_page()):
            problems.append("no notification on this computer (turned off, or this computer has no way to show one)")
        if self.conf["ntfy"]:
            import httpx

            try:
                url, body = ntfy_message(self.conf["ntfy"], *far, priority)
                token = self.conf["ntfy_token"]
                answer = httpx.post(url, json=body, timeout=15, headers={"User-Agent": USER_AGENT, **({"Authorization": f"Bearer {token}"} if token else {})})
                if answer.status_code >= 300:
                    problems.append(f"ntfy said {answer.status_code}")
            except ValueError as e:
                problems.append(str(e))
            except httpx.HTTPError as e:
                problems.append(f"ntfy not reached ({type(e).__name__})")
        if running is not None:
            try:
                running.join(COMMAND_LIMIT + 5)  # it ends within its limit and a moment; this is for a stop that hangs
            except KeyboardInterrupt:  # hylly bell test's Ctrl-C (before this, the end of the program: stop_commands)
                stop_commands()
                self._ran(f"{program(command)} was stopped by a Ctrl-C")
                raise
            problem = ran[0] if ran else f"{program(command)} hadn't been stopped after {COMMAND_LIMIT + 5:g} seconds"
            self._ran(problem)
            if problem:
                problems.append(f"the alarm command failed: {problem}")
        for p in problems:
            if "notification on this computer" not in p:  # a server with no screen is nothing to report
                self._log(f"Alarms: {p}.")
        return problems

    def _ran(self, problem: str) -> None:
        """The command's run, in bell.log: each that failed, and the first to work after one, so that the latest word on
        it there is how it stands (command_trouble, for hylly doctor). It never holds up an alarm or the server."""
        if problem:
            self._record("command", COMMAND_FAILED, problem)
        elif command_trouble():
            self._record("command", COMMAND_WORKS, "It ran and ended well.")

    def _save(self) -> None:
        atomic_write(self._state_file, json.dumps(self._state, indent=1), attempts=1)  # under the lock: requests wait for it
