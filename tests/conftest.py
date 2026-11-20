"""Tests never read your own settings or touch your running server: they get an empty settings file,
their own settings folder (for the server's lock and note, the database, and an empty rules folder; HYLLY_HOME leads there,
and so does LOCALAPPDATA on Windows), their own folder for what jobs started from elsewhere say, and a made-up StoryBundle
account. The rules and settings are read once, as a command reads them: a helper that writes them in the middle of a
test reads them afresh (settings.reload, rules.reset), and the tests of a running server's look at them again, at every
use, take watching. The test PC's own name is made
up too, so the tests answer the same on every PC: it is "testpc", at 192.168.1.5 on the home network.

Every request to the library server logs in: log_in gives a test's client the login of a made-up user, an owner unless
asked for another role. What only costs time is cut short: a password is hashed at a cost of 16 (SCRYPT_N; a stored
hash carries its own cost, so it's checked as well; test_access.py holds Hylly's own to the strong one), and a server a
test starts with serve_forever() looks whether it should stop every STOP_POLL seconds, so that shutdown() doesn't wait
out Python's half second each time.

Whoever runs the tests, no test is root or an administrator, as the suite may run as root in a container (where
HYLLY_ALLOW_ROOT is set instead of taken away, for the processes a test starts); and nothing is asked of this computer's
Task Scheduler, systemd, launchd or 7-Zip, which answer as if there were none. A test's
settings folder is never the usual one (paths.usual_settings_folder, as it is), so nothing a test does can write or take
away the start at logon, which is the whole user's: the few tests of that take usual_folder, which has nothing of this
computer's beneath it.

Nor do tests use your shop logins: the logins and keys you may have in the environment (every variable in sites.SITES),
the alarms' ntfy address and token (settings.SECRETS, with their _FILE and systemd's CREDENTIALS_DIRECTORY),
HYLLY_ALLOW_ROOT, and HYLLY_REMOTE, which would pass the commands on to a server, are taken away for each test. And no
test reaches past this computer over httpx, which is how
Hylly reaches the network: in the test's own process, a request to any other host fails the test (no_network). Made-up
shops answer through httpx.MockTransport, which never connects.

Under all of that, for the whole session, whatever a test does with monkeypatch (an undo() takes away the fixtures'
changes too): as this file is imported, before anything is collected, the environment gets safe values of its own (SAFE:
a settings folder, a home, LOCALAPPDATA, APPDATA and the XDG folders in a folder made for the session, no notifications,
no logins), httpx refuses every host but this computer, and no name is looked up before a request (fetching.resolve).
So a monkeypatch.undo() falls back to those, never to the owner's own. And after every test, never_the_owners_own
checks that every kind's folder of Hylly's own (paths.PLACES), the settings file, the bell's log, HyllyCopy's record of
targets and the jobs' output aren't in the owner's own Hylly folders (REAL, as the environment named them when the
tests began), and that no request left for another host.

Each test's own folders are in one folder of its own, numbered in turn under own_folders: pytest's numbered folders
(tmp_path_factory.mktemp) look through all made before for the next number, which made each test's start slower as a
run went on.

What tests share is here as fixtures, and in helpers.py otherwise, never in one test file for another to import: the
made-up library with its catalog (library, library_at), built once for the run under an isolation of its own
(made_library) and copied for each test into its own folders, its database tied to the copy as a restore ties one; the
library server, every one shut down and closed at the end of its test (serving, server); the demo's library, drawn once
(made_demo, demo_made_once); and a few more that more than one file uses (users, told, person, toml)."""

from __future__ import annotations

import atexit
import contextlib
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import pytest

LOCAL = {"127.0.0.1", "::1", "localhost"}  # this computer: the only place a test's real connection may go
PASSWORD = "correct horse"  # every made-up user's that log_in makes
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0  # the suite itself run as root, as in a container
SCRYPT_N = 16  # a password's hashing cost in the tests: Hylly's own (2**17) takes a quarter of a second and 128 MB a hash,
# and the tests make hundreds
STOP_POLL = 0.02  # seconds between a test's server's looks whether it should stop (Python's own: 0.5, waited at each stop)


def _hylly_places() -> list[Path]:
    """The owner's own Hylly folders, as Hylly would find them from the environment the tests were started in, and the
    places it writes to beside them: the systemd user unit, the launchd agent. Every kind's (paths.PLACES): its own
    variable's, the one folder of all in one, and each system's default. Not the whole home or LOCALAPPDATA, which hold
    the system's temp folder, where pytest's own folders are."""
    home, env = Path.home(), os.environ.get
    found = [env("HYLLY_HOME"), env("LOCALAPPDATA") and Path(env("LOCALAPPDATA")) / "Hylly",
             env("APPDATA") and Path(env("APPDATA")) / "Hylly", home / "AppData" / "Local" / "Hylly",
             home / "AppData" / "Roaming" / "Hylly", home / ".local" / "share" / "Hylly", home / ".local" / "state" / "hylly",
             *(env(f"HYLLY_{kind}_DIR") for kind in ("CONFIG", "DATA", "STATE", "CACHE", "LOGINS")),
             *(Path(env(variable) or home / default) / "hylly" for variable, default in (
                 ("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state"),
                 ("XDG_CACHE_HOME", ".cache"))),
             home / "Library" / "Application Support" / "Hylly", home / "Library" / "Caches" / "Hylly",
             Path(env("XDG_CONFIG_HOME") or home / ".config") / "systemd" / "user", home / "Library" / "LaunchAgents"]
    return [Path(p) for p in found if p]


def _same(path) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


REAL = [_same(p) for p in _hylly_places()]  # taken now, before anything below changes the environment
REAL_SETTINGS = [_same(os.environ["HYLLY_SETTINGS"])] if os.environ.get("HYLLY_SETTINGS") else []


def in_the_owners(path) -> bool:
    """Whether a path is in one of the owner's own Hylly folders (REAL), or is the settings file they named."""
    where = _same(path)
    return where in REAL_SETTINGS or any(where == real or where.startswith(real.rstrip(os.sep) + os.sep) for real in REAL)


for _old in Path(tempfile.gettempdir()).glob("hylly-tests-*"):  # a session's left over (killed, or a file held open)
    with contextlib.suppress(OSError):
        if _old.is_dir() and time.time() - _old.stat().st_mtime > 86400:
            shutil.rmtree(_old, ignore_errors=True)
SESSION = Path(tempfile.mkdtemp(prefix="hylly-tests-"))  # the session's own, gone at its end
SAFE = {"HYLLY_HOME": SESSION / "settings" / "Hylly", "LOCALAPPDATA": SESSION / "settings", "APPDATA": SESSION / "roaming",
        "USERPROFILE": SESSION / "home", "HOME": SESSION / "home", "XDG_CONFIG_HOME": SESSION / "home" / ".config",
        "XDG_DATA_HOME": SESSION / "home" / ".local" / "share", "XDG_STATE_HOME": SESSION / "home" / ".local" / "state",
        "XDG_CACHE_HOME": SESSION / "home" / ".cache",
        "HYLLY_SETTINGS": SESSION / "settings" / "settings.toml", "HYLLY_NO_NOTIFY": "1", "HYLLY_STORYBUNDLE_USER": "12345",
        # as root, so that what runs before the tests' own stand-ins (a fixture of a module's, say) and a hylly a test
        # starts as a process of its own aren't refused, as paths.is_root's stand-in can't reach them
        **({"HYLLY_ALLOW_ROOT": "1"} if AS_ROOT else {})}
os.environ.update({name: str(value) for name, value in SAFE.items()})
PLACE_VARIABLES = tuple(f"HYLLY_{kind}_DIR" for kind in ("CONFIG", "DATA", "STATE", "CACHE", "LOGINS"))  # HYLLY_HOME wins
for _name in ("HYLLY_LIBRARY", "HYLLY_RULES", "HYLLY_REMOTE", *PLACE_VARIABLES, *(() if AS_ROOT else ("HYLLY_ALLOW_ROOT",))):
    os.environ.pop(_name, None)
def _tidy() -> None:
    """The session's folder, at its end: its databases closed first, as Windows won't remove a file held open; what
    still can't go is left to the next session's tidying, above."""
    with contextlib.suppress(Exception):
        from hylly import db

        db.close_all()
    shutil.rmtree(SESSION, ignore_errors=True)


atexit.register(_tidy)


def _secret_variables() -> list[str]:
    """Every variable that may carry a login or the alarms' secrets: a login of the owner's would reach their account, and
    their ntfy address their phone. The alarms' by name (settings.SECRETS has them), so that the tests can be run against
    a Hylly from before they had files of their own, to see the new ones fail there."""
    from hylly.sites import SITES

    ntfy = [name for variable in ("HYLLY_NTFY_URL", "HYLLY_NTFY_TOKEN") for name in (variable, f"{variable}_FILE")]
    return [*(variable for _, _, variable in SITES.values()), *ntfy, "CREDENTIALS_DIRECTORY"]


def _no_logins() -> None:
    for variable in _secret_variables():
        os.environ.pop(variable, None)


_no_logins()


def _guard_the_network() -> list[str]:
    """httpx, under everything for the session: a request to any host but this computer is refused, and noted, so that
    never_the_owners_own fails its test, also when the test has undone no_network. Nor is a name looked up before a
    request (fetching.resolve): as one that can't be, it is left to the test's MockTransport; a test of the lookup's
    refusals makes up its own answers."""
    import httpx

    from hylly import fetching

    real, fetching.resolve = fetching.resolve, lambda host, port: []
    fetching.resolve.real = real  # for the test of the lookup itself, of this computer's own name only

    asked: list[str] = []
    real_sync, real_async = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request

    def refused(request) -> None:
        if request.url.host not in LOCAL:
            asked.append(f"{request.method} {request.url.scheme}://{request.url.host}{request.url.path}")
            raise httpx.ConnectError(f"A test asked the network: {asked[-1]}", request=request)

    def handle_request(self, request):
        refused(request)
        return real_sync(self, request)

    async def handle_async_request(self, request):
        refused(request)
        return await real_async(self, request)

    httpx.HTTPTransport.handle_request = handle_request
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request
    return asked


ASKED_ANYWHERE = _guard_the_network()


def _quick_stops() -> None:
    """Every server a test starts with serve_forever() looks whether it should stop every STOP_POLL seconds, for the
    session, wherever the test starts it: a test's shutdown() then takes a moment, not up to half a second. One given
    its own poll_interval (serve.run's) keeps it."""
    import socketserver

    real = socketserver.BaseServer.serve_forever

    def serve_forever(self, poll_interval: float = STOP_POLL) -> None:
        real(self, poll_interval)

    socketserver.BaseServer.serve_forever = serve_forever


_quick_stops()


SYSTEM_TOOLS = ("_schtasks", "_systemctl", "_systemctl_wide", "_launchctl", "_lingering")  # instance's ways of asking the system


def _nothing_asked(real):
    """A stand-in for one of instance's ways of asking this computer's Task Scheduler, systemd, launchd or loginctl: no
    such task, unit or agent, no lingering, and nothing asked. real: the way itself, for usual_folder."""
    def asked(*args: str, **options) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 1, "", "not asked: a test's stand-in")

    def not_lingering() -> bool:  # this user's services don't start with the computer
        return False

    found = not_lingering if real.__name__ == "_lingering" else asked
    found.real = real
    return found


def pytest_addoption(parser):
    parser.addoption("--new-snapshot", action="store_true",
                     help="write tests/shipped_rules.json and tests/shipped_word_answers.json again from src/hylly/data, "
                          "and tests/routes.json from the server's answers (tests/test_routes.py), after a change that "
                          "is meant")


@pytest.fixture(scope="session")
def own_folders(tmp_path_factory) -> Path:
    """The folder of every test's own folder (no_personal_settings), numbered in turn: made once, as pytest's numbered
    folders (mktemp) look through every folder made before for the next number, which cost each test more as the run
    went on."""
    return tmp_path_factory.mktemp("own")


OWN_NUMBERS = itertools.count()  # the next test's own folder


@pytest.fixture(autouse=True)
def no_personal_settings(own_folders, monkeypatch):
    own = own_folders / str(next(OWN_NUMBERS))
    isolated(monkeypatch, own)
    yield
    from hylly import db

    db.close_all()  # each test's own database, closed, so its folder can go


def isolated(monkeypatch, own: Path) -> None:
    """A test's isolation, in a folder of its own (own): its settings file (settings/settings.toml), its settings folder
    (appdata/Hylly) and its fstab, and all that the module's docstring tells, set by monkeypatch, which takes it away
    again. no_personal_settings gives each test its own; made_library builds the library it copies under one too."""
    (own / "settings").mkdir(parents=True)
    monkeypatch.setenv("HYLLY_SETTINGS", str(own / "settings" / "settings.toml"))
    appdata = own / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("HYLLY_HOME", str(appdata / "Hylly"))  # the settings folder: it wins over LOCALAPPDATA,
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))  # on Windows the same one, should a test take HYLLY_HOME away
    for name in ("LIBRARY", "RULES", "ALLOW_ROOT", "REMOTE"):
        monkeypatch.delenv(f"HYLLY_{name}", raising=False)
    for name in PLACE_VARIABLES:  # over them anyway, but a test that takes HYLLY_HOME away finds none of the owner's
        monkeypatch.delenv(name, raising=False)
    if AS_ROOT:  # so that a hylly a test starts as a process of its own runs too, where paths.is_root, below, can't reach
        monkeypatch.setenv("HYLLY_ALLOW_ROOT", "1")
    monkeypatch.setenv("HYLLY_STORYBUNDLE_USER", "12345")
    monkeypatch.setenv("HYLLY_NO_NOTIFY", "1")  # no notifications from tests
    from hylly import access, archives, cli, dashboard, doctor, hyllycopy, instance, paths, rules, serve, settings, updates

    # On Linux every copy, backup and restore reads /etc/fstab (hyllycopy.unmounted_place): a made-up empty one, so that
    # no test goes by this computer's disks; test_hyllycopy.py makes up its own to try the guard.
    fstab = own / "fstab"
    fstab.write_text("", encoding="utf-8")
    monkeypatch.setattr(hyllycopy, "FSTAB", str(fstab))

    for variable in _secret_variables():  # a login of yours would win over the test's own, and reach your account
        monkeypatch.delenv(variable, raising=False)

    monkeypatch.setattr(rules, "CHECK_EVERY", 0)
    monkeypatch.setattr(cli, "_keep_awake", lambda on: None)  # no tests keeping this computer awake
    # paths.usual_settings_folder stays real: a test's settings folder is never the usual one, so nothing a test does can
    # change the start at logon, which is the whole user's. A test of that takes usual_folder, below. Nor is anything
    # asked of this computer's own Task Scheduler, systemd, launchd or loginctl, its 7-Zip, or who runs the tests: each
    # answers as nothing would, whatever this computer has. Their real ones are tried, with made-up answers, where
    # they're tested (test_other_systems.py, test_updates.py, test_doctor.py, test_assistants.py, test_reader.py,
    # test_rules.py).
    for tool in SYSTEM_TOOLS:
        monkeypatch.setattr(instance, tool, _nothing_asked(getattr(instance, tool)))
    monkeypatch.setattr(archives, "sevenzip", lambda: "")  # none found: a test that needs one makes one up
    monkeypatch.setattr(updates, "seven_zip_version", lambda tool: None)
    monkeypatch.setattr(archives, "sevenzip_info", lambda tool: (None, None))  # the doctor's and the reader's way to it
    monkeypatch.setattr(dashboard, "logon_start", lambda: None)
    monkeypatch.setattr(paths, "is_root", lambda: False)  # the tests of root, and of an administrator's terminal, say so
    monkeypatch.setattr(doctor, "_elevated", lambda: False)  # themselves (test_assistants.py)
    # Nor does the doctor look at this computer's own start at logon, whose program may be any; test_other_systems.py
    # looks at made-up ones (instance.logon_found). Nor at its walled service's files, which a test that takes a walled
    # one for granted would read: test_other_systems.py makes up its own (instance.service_differences).
    monkeypatch.setattr(instance, "logon_found", lambda: None)
    monkeypatch.setattr(instance, "service_differences", lambda: None)
    monkeypatch.setattr(paths, "job_output_dir", lambda: appdata / "Hylly-jobs")  # as on Windows: not the real one on Linux and macOS
    monkeypatch.setattr(paths, "REQUIRE_MARKER", False)  # small made-up libraries; test_library_marker.py turns it on
    paths.forget_one_folder()  # decided once for each process (paths.one_folder): looked at afresh for each test
    monkeypatch.setattr(settings, "CHECK_EVERY", 0)
    # Read once, as a command reads them: a look at the files at every use cost the slowest files a good part of their
    # time. A test that writes them reads them afresh; watching, below, is for the tests of a running server's look,
    # which serve.run turns on too; and once on, every use looks (CHECK_EVERY 0).
    monkeypatch.setattr(rules, "WATCH", False)
    monkeypatch.setattr(settings, "WATCH", False)
    monkeypatch.setattr(rules, "EXTRA", [])
    rules.reset()
    settings.reload()
    monkeypatch.setattr(access, "SCRYPT_N", SCRYPT_N)

    made_up = lambda: "testpc"  # noqa: E731
    made_up.real = serve.own_name  # the system's, for the test of what it asks (test_security.py)
    monkeypatch.setattr(serve, "own_name", made_up)
    # and its address at home, as if looked up already (access.lan_address): only the server's job "home address" looks
    # again, and no test runs it but on purpose; the adapters themselves: test_access.py
    monkeypatch.setattr(access, "_adapter", {"address": "192.168.1.5"})
    # the server's failures said once each are the process's: each test starts with none said yet
    monkeypatch.setattr(serve.Handler, "FAILED", set())
    monkeypatch.setattr(serve.Handler, "FAILED_SAID", {})


@pytest.fixture(autouse=True)
def never_the_owners_own(no_personal_settings, monkeypatch):
    """After each test, with what it set still in effect: none of the places Hylly writes is in the owner's own folders,
    and no request left for another host, however the test went about it."""
    before = len(ASKED_ANYWHERE)
    yield
    from hylly import bell, hyllycopy, paths

    places = {"the settings file": paths.settings_file(), "the bell's log": bell.log_file(),
              "HyllyCopy's targets": hyllycopy._targets_file(), "the jobs' output": paths.job_output_dir(),
              **{f"the {kind} folder": paths.place(kind) for kind in paths.PLACES}}
    inside = [f"{what}: {where}" for what, where in places.items() if in_the_owners(where)]
    asked = ASKED_ANYWHERE[before:]
    assert not inside, f"A test reached the owner's own folders: {'; '.join(inside)}"
    assert not asked, f"A test asked the network: {', '.join(asked)}"


@pytest.fixture
def watching(monkeypatch):
    """For the tests of what a running server does when the settings or the rules change under it, and of how they are
    read as they change: they are looked at again (WATCH), as serve.run turns on, at every use in the tests (CHECK_EVERY
    0, above). The other tests read them once, as a command does."""
    from hylly import rules, settings

    monkeypatch.setattr(rules, "WATCH", True)
    monkeypatch.setattr(settings, "WATCH", True)


@pytest.fixture
def usual_folder(monkeypatch):
    """For the few tests of what only the user's usual settings folder may do, the start at logon above all: the test's
    own taken for the usual one (paths.usual_settings_folder; never for a walled service's, which a test of one makes
    up itself), with nothing of this computer's beneath it. subprocess.run runs nothing, and answers as a program that
    failed, and instance's ways of asking the system are their real selves again, as all they do is call it: a test
    gives subprocess.run the answers it wants (test_other_systems.fake_systemd), or fakes one of those ways."""
    from hylly import instance, paths

    monkeypatch.setattr(paths, "usual_settings_folder", lambda settings_file_counts=True: settings_file_counts)
    monkeypatch.setattr(subprocess, "run", lambda command, *a, **k: subprocess.CompletedProcess(command, 1, "", "not run: a test's stand-in"))
    for tool in SYSTEM_TOOLS:
        monkeypatch.setattr(instance, tool, getattr(instance, tool).real)


@pytest.fixture
def toml(monkeypatch, tmp_path):
    """toml(text): the settings' [reader] table, as text, written and read afresh, in a settings folder of its own
    (tmp_path/settings) with no settings file named: the reader's settings as the server reads them."""
    from hylly import paths, settings

    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    monkeypatch.delenv("HYLLY_SETTINGS", raising=False)
    paths.config_dir().mkdir(parents=True, exist_ok=True)

    def write(text: str) -> None:
        paths.settings_file().write_text("[reader]\n" + text, encoding="utf-8")
        settings.reload()

    yield write
    settings.reload()


@pytest.fixture
def log_in():
    """Logs a test's client in to the library server, as every request there needs: log_in(client) as an owner,
    log_in(client, "reader") with another role, log_in(client, name="ann") as that user. A user of that name is made if
    there's none yet (by its role's name unless named, with PASSWORD), and the client gets a browser's session in its
    cookies, as the login page gives one, so its requests may look like a browser's or a reading app's. Returns the
    client."""
    def logged_in(client, role: str = "owner", name: str | None = None):
        from hylly import access

        name = name or role
        if name not in access.load_accounts():
            access.add_user(name, PASSWORD, role=role)
        client.cookies.set(access.COOKIE, access.Sessions().start(name, access.load_accounts()[name][1]))
        return client

    return logged_in


# ---- the made-up library, built once, and its server -----------------------------------------------------------------

class Made(NamedTuple):
    """The made-up library as made_library built it: the library, the database and records-token it was built with
    (closed), and what build_library added to the settings."""
    library: Path
    data: Path
    settings: str


@pytest.fixture(scope="session")
def made_library(tmp_path_factory) -> Made:
    """helpers.build_library, and the catalog built over it, once for the whole run, in a test's isolation of its own
    (isolated): what library_at copies for each test, in a moment, where building it took a test a good part of its
    time."""
    from helpers import build_library
    from hylly import catalog, db, paths

    made = tmp_path_factory.mktemp("made-library")
    with pytest.MonkeyPatch.context() as monkeypatch:
        isolated(monkeypatch, made / "own")
        build_library(made / "library")
        catalog.build(made / "library", log=lambda s: None)
        db.close_all()  # its writes all in the file, for the copies
        found = Made(made / "library", paths.place("data"), paths.settings_file().read_text(encoding="utf-8"))
    from hylly import rules, settings

    rules.reset()  # nothing of its settings left for the test that asked for it first, which reads its own
    settings.reload()
    return found


@pytest.fixture
def library_at(made_library):
    """library_at(root): the made-up library at root, its catalog built, as helpers.build_library and catalog.build
    leave it: a copy of the one made_library built, its database put in this test's settings folder and made the
    library's own, as a restore makes it (tied to the copy's root by the records-token beside it, which comes along), and
    build_library's settings added to the test's. Where the test has a database or settings or rules of its own already,
    which the copy would pass over, it is built afresh instead. Returns root."""
    from helpers import build_library, with_settings
    from hylly import catalog, db, paths, rules

    def copy(root: Path) -> Path:
        data, settings_file, own_rules = paths.place("data"), paths.settings_file(), rules.folder()
        if ((data / db.FILE).exists() or (settings_file.is_file() and settings_file.read_text(encoding="utf-8").strip())
                or own_rules.exists() or (root.exists() and any(root.iterdir()))):
            build_library(root)
            catalog.build(root, log=lambda s: None)
            return root
        shutil.copytree(made_library.library, root, dirs_exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
        for name in (db.FILE, db.TOKEN):
            shutil.copy2(made_library.data / name, data / name)
        with contextlib.closing(sqlite3.connect(data / db.FILE)) as c, c:
            c.execute("UPDATE library SET root = ?", (os.path.abspath(root),))
        with_settings(made_library.settings)
        return root

    return copy


@pytest.fixture
def users() -> dict[str, str]:
    """The made-up users the library fixture adds, by name and their roles: none, unless a file of tests overrides this
    (with helpers.EVERY_ROLE, say)."""
    return {}


@pytest.fixture
def library(tmp_path, library_at, users):
    """The made-up library at tmp_path, its catalog built (library_at), with the users users names, each with
    PASSWORD."""
    from hylly import access

    library_at(tmp_path)
    for name, role in users.items():
        access.add_user(name, PASSWORD, role=role)
    return tmp_path


@pytest.fixture
def serving(log_in):
    """serving(root, login="owner", name=None, **options): the library at root served on a port of its own
    (serve.make_server, with its options), and a client for it, logged in as log_in logs in: as an owner, as a user of
    another role, or as name; login None, as nobody. Returns the server and the client; the server's serving_thread is
    the thread that serves it. As the test ends, every client it gave is closed, and every server shut down and closed
    (server_close), its socket with it."""
    import httpx

    from hylly import serve

    started = []

    def serve_it(root, login: str | None = "owner", name: str | None = None, **options):
        srv, _ = serve.make_server(root, "127.0.0.1", 0, **options)
        srv.serving_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        srv.serving_thread.start()
        client = httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
        started.append((srv, client))
        if login is not None:
            log_in(client, login, name=name)
        return srv, client

    yield serve_it
    for srv, client in reversed(started):
        client.close()
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def server(library, serving):
    """The library fixture's library served on a port of its own, nobody logged in: the server (serving)."""
    srv, _ = serving(library, login=None)
    return srv


class MadeDemo(NamedTuple):
    """The demo's library as demo.make made it once (made_demo): make(folder) copies it there, as demo.make would have
    drawn it, its marker made now."""
    library: Path

    def make(self, folder: Path) -> Path:
        from hylly import demo, paths, storage

        root = folder / "library"
        shutil.copytree(self.library, root)
        marker = json.loads(paths.marker_file(root).read_text(encoding="utf-8"))
        marker["created"] = datetime.now().isoformat(timespec="seconds")
        storage.atomic_write(paths.marker_file(root), json.dumps(marker, indent=1))
        assert demo.is_demo(folder)
        return root


@pytest.fixture(scope="session")
def made_demo(tmp_path_factory) -> MadeDemo:
    """hylly demo's library, its books and comics drawn by demo.make once for the whole run, in a test's isolation of
    its own: drawing them takes seconds, and every test of the demo made one."""
    from hylly import demo

    made = tmp_path_factory.mktemp("made-demo")
    with pytest.MonkeyPatch.context() as monkeypatch:
        isolated(monkeypatch, made / "own")
        return MadeDemo(demo.make(made / "demo"))


@pytest.fixture
def demo_made_once(made_demo, monkeypatch):
    """demo.make, for a folder that is empty or not there yet, copies made_demo's library there instead of drawing it
    again; a folder with a demo in it already, or other things, goes to demo.make itself, which keeps or refuses it. What
    hylly demo does with the library from there (its catalog, its settings, its owner) is as it would be."""
    from hylly import demo

    real = demo.make

    def make(folder: Path) -> Path:
        if folder.exists() and any(folder.iterdir()):
            return real(folder)
        return made_demo.make(folder)

    monkeypatch.setattr(demo, "make", make)


@pytest.fixture
def told() -> list:
    """What a person's records told, as people.Person's own telling: (name, what) each."""
    return []


@pytest.fixture
def person(told):
    """rory, as the server knows a person: what is kept of them, told to told."""
    from hylly import people

    return people.Person("rory", lambda name, what: told.append((name, what)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """httpx's real transports, under every client the code makes, refuse any host but this computer: the test fails at
    once and, should the code have caught that or asked from a thread (where it is only a warning), again when it ends.
    A MockTransport is no real transport, so the made-up shops answer as before. It gives what was asked: a test of the
    guard itself empties it."""
    import httpx

    asked: list[str] = []

    def refused(request: httpx.Request) -> None:
        if request.url.host not in LOCAL:
            asked.append(f"{request.method} {request.url.scheme}://{request.url.host}{request.url.path}")
            pytest.fail(f"A test asked the network: {asked[-1]}. Give its client an httpx.MockTransport.", pytrace=False)

    real_sync, real_async = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request

    def handle_request(self, request):
        refused(request)
        return real_sync(self, request)

    async def handle_async_request(self, request):
        refused(request)
        return await real_async(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    yield asked
    if asked:
        pytest.fail(f"A test asked the network: {', '.join(asked)}.", pytrace=False)
