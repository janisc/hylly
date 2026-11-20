"""Hylly's database: one SQLite file, hylly.db in the data folder of the server computer, which owns the library's
data (docs/database.md says why). In it: what's kept about people (people.py) and who may log in (access.py); the
library's tags and corrections and the downloads' questions to decide (curation.py), shelves for everyone (shelves.py)
and word on reported titles (reports.py); the catalog (catalog.py); and what came into the library (manifest.py).
migrations/ makes every table.

- Only Hylly's code writes it, and only on the computer it's on. Never over a network share: SQLite's locks don't hold
  there. The data folder is on the computer's own disk.
- WAL: between programs, reading never waits for writing (the server's reads go on through a command's write), and a
  writer waits up to BUSY_SECONDS for another (the server and a command). Within one program a read does wait: a
  program has one connection (Database), which takes one thread's question at a time, so a read waits while another
  thread's write waits for its lock. So the server writes only when something changes (access.Sessions.renew reads
  first).
  Every change is on the disk before it's done (synchronous FULL).
- foreign_keys on, and secure_delete on: what's deleted is overwritten, not left in the file's free pages. The help
  promises that words taken away from a review are gone from the file.
- Migrations: numbered SQL files in migrations/, applied in order when the database is opened, each in one transaction.
  PRAGMA user_version says how far it has come, and PRAGMA application_id that it's Hylly's (APPLICATION_ID). Before
  one is applied to a database already in use, a copy is taken beside it with SQLite's backup API (hylly.before-NNNN.db,
  never written over: a second one is named for when it was taken too), holding the write lock, so two programs
  opening it at once take one copy and one step. A step that leaves much of the file free gives it back (VACUUM).
- A database from a newer version of Hylly is left alone: Newer, and nothing is read from it or written to it. So is
  one from a test version before the first release (Older), which counted its steps another way, unless it came
  through all of them (BEFORE): it then has the first step's very tables, and is taken on as that step (standing).
- Every night the server copies the database to the library's disk (nightly), with each table beside it as JSON lines,
  which need neither Hylly nor SQLite to read: only the database the library's marker names (own), and only its copies
  count (nights), with those from before a database kept the library's records. As each is made, if its check is good,
  the copies before it go once BACKUPS_KEPT newer ones are there, and those more than NIGHTS_DAYS older than this
  computer's day too, so that a server that runs only now and then doesn't keep weeks of them. While no good copy is
  made, none goes, and the newest stays whatever its age. A copy dated after this computer's day (a clock
  that was wrong once) neither counts nor makes others go, and a backup takes none dated more than AHEAD_DAYS ahead
  (latest_day). Another database's are left where they are. With no marker, nothing tells the copies apart, and all
  count. One copy at a time is made of a database (_one_copy_at_a_time): a backup's fresh one and the night's, or two
  backups', share their work folders, so the second waits for the first.
- A nightly copy holds everything the database does: the copy's hylly.db has the users' password hashes and the
  digests of the browsers kept logged in (SECRET), which the JSON lines leave out. So on Linux and macOS, where the
  disk keeps rights, each copy's folder and files are their owner's only (on Windows a mode only sets the read-only
  flag); hylly doctor says when others can read them, and a backup's zip takes the newest copy whole.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path, PurePath

from . import paths
from .storage import atomic_write, is_link, read_json, share_is_gone

FILE = "hylly.db"
BUSY_SECONDS = 15.0
OLDEST_SQLITE = (3, 37, 0)  # STRICT tables
MIGRATIONS = Path(__file__).with_name("migrations")
APPLICATION_ID = 0x48796C6C  # "Hyll", in the header of every database Hylly makes: what tells its steps from the test versions'
BEFORE = 9  # the steps of the test versions before the first release: a database that came through all of them is step 1
BACKUPS_KEPT = 7  # nights
NIGHTS_DAYS = 14  # and none more than these days older than today, once a newer one is made with a good check (nightly)
AHEAD_DAYS = 2  # a copy a backup or a restore may take though dated after this computer's day: time zones differ by at
# most 26 hours, so a server's may be dated tomorrow here; one further ahead was made under a clock that was wrong
ANY_DAY = "9999-12-31"  # as today for nights: every day's copy, the one a server says it has just made among them
COPY_WAIT = 300.0  # seconds a copy of the database waits for another one being made meanwhile: well within the time a
# backup on another computer waits for the server's fresh copy (backup.FRESH_WAIT)
BACKUP_FROM_HOUR = 3  # the nightly copy, at this hour or as soon after it as the library is there
DAY = re.compile(r"\d{4}-\d\d-\d\d")  # a nightly copy's folder; nothing else in db-backup is one
SECRET = frozenset({("account", "hash"), ("browser_login", "digest"), ("browser_login", "stamp")})  # (table, column):
# never shown on the Database page, nor in the plain export beside a nightly copy (the copy's hylly.db has them, to go back to)
COMPUTER = re.compile(r"[A-Za-z0-9._-]{1,64}")  # how a computer's name in a library's marker may be told
TOKEN = "records-token"  # beside the database: this install's own, which the marker of each library it keeps names (own)
TOKEN_TEXT = re.compile(r"[0-9a-f]{32}")
TAKE_OVER = "hylly server take-over"  # the one command that ties a library's records to this install, as its owner says
READ_BACK = 20  # looks at a marker just made elsewhere, a moment apart, while it isn't whole yet (_read_back)
_DRY: contextvars.ContextVar[dict | None] = contextvars.ContextVar("dry_run", default=None)  # a dry run's databases (dry_run)
README = """\
The database of the Hylly server, as it was on the night of {day}.

hylly.db       the database itself, for SQLite and the programs that read it (DB Browser for SQLite, say), with
               everyone's records and the users' password hashes: keep it private
<table>.jsonl  each table, one row on each line as JSON: readable in any text editor, without Hylly or SQLite, and
               without the password hashes
check.json     what was checked when it was made

To go back to this night: stop the server, put the hylly.db in the server's data folder in place of the one there
(hylly settings places, on the server, says where that folder is; keep the one you take away, and delete hylly.db-wal and
hylly.db-shm beside it if they're there), and start the server.
"""


class Newer(Exception):
    """The database is from a newer version of Hylly: left alone. The words say so."""


class Older(Newer):
    """The database is from a test version of Hylly before its first release, at a step this version doesn't take on
    (standing): left alone, as a newer one is, and caught with those, so whatever leaves a newer one alone leaves this
    one too. The words say what to do."""


class Unusable(Exception):
    """This Python's SQLite is too old for the database, or the like: the words say what."""


class NotKept(Exception):
    """What was written didn't come back from the database as it went in, so none of it was kept. The words say what."""


class NotHere(Exception):
    """The library's own records are kept in another computer's database, or can't be from here: the words say why and
    where."""


class Moved(NotHere):
    """NotHere, as another database took the library's records on while this program ran (own): said once already, as
    it was noticed, to Database.on_lost (the server's log, and an alarm)."""


def file() -> Path:
    return paths.place("data") / FILE


def now() -> str:
    """The time a record is stamped with: this computer's local time with its offset, to the second
    (2026-11-19T14:05:00+02:00), the same moment whatever the zone or the clocks' change. The one way every record
    stamps it: those of people, accounts and reports had it already; the others' from before have the local time
    alone, and keep it (moment reads both)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def moment(stamp: str) -> datetime:
    """A record's time, to compare or sort by: one with its offset as it says, one from before records had theirs (the
    local time alone) as this computer's local time then, so that the two side by side compare as the moments they
    are. An old one in the hour the clocks go back, which comes twice, is read as the first of the two (fold 0, as
    datetime reads it): the local time alone can't tell which it was. ValueError when it's no such time; TypeError
    when it isn't text."""
    found = datetime.fromisoformat(stamp)
    return found if found.tzinfo is not None else found.astimezone()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---- records in tables ---------------------------------------------------------------------------

def _fits(value, kind: str) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value < 2**63
    if kind == "real":
        return isinstance(value, float)
    return isinstance(value, str)  # text


class Shape:
    """How a kind of record is kept in its table: each field in a column of its own, of the type it should be, and
    anything else, a field of another type among them, in the column more, as JSON. So a record comes back exactly as
    it went in, and the columns can be asked about in SQL.

    fields: (the record's key, its column, its kind: text, real, int, bool, or json for any value, kept as JSON text).
    op: the op every record of this kind has, which needn't be kept."""

    def __init__(self, table: str, fields: tuple[tuple[str, str, str], ...], op: str | None = None) -> None:
        self.table, self.fields, self.op = table, fields, op
        self._keys = {key for key, _, _ in fields}

    def row(self, record: dict) -> dict:
        out, more = {}, {}
        for key, column, kind in self.fields:
            value = record.get(key)
            if key not in record:
                out[column] = None
            elif kind == "json":
                out[column] = dump(value)
            elif _fits(value, kind):
                out[column] = int(value) if kind == "bool" else value
            else:
                out[column], more[key] = None, value
        for key, value in record.items():
            if key not in self._keys and not (key == "op" and self.op is not None and value == self.op):
                more[key] = value
        out["more"] = dump(more) if more else None
        return out

    def record(self, row: sqlite3.Row) -> dict:
        out = {"op": self.op} if self.op is not None else {}
        for key, column, kind in self.fields:
            value = row[column]
            if value is not None:
                out[key] = json.loads(value) if kind == "json" else bool(value) if kind == "bool" else value
        if row["more"]:
            out.update(json.loads(row["more"]))
        return out

    def insert(self, c: sqlite3.Connection, record: dict, **also) -> int:
        values = {**also, **self.row(record)}
        names, marks = ", ".join(values), ", ".join("?" for _ in values)
        return c.execute(f"INSERT INTO {self.table} ({names}) VALUES ({marks})", tuple(values.values())).lastrowid

    def update(self, c: sqlite3.Connection, record: dict, where: str, *args) -> None:
        values = self.row(record)
        sets = ", ".join(f"{name} = ?" for name in values)
        c.execute(f"UPDATE {self.table} SET {sets} WHERE {where}", (*values.values(), *args))


# ---- opening, and the migrations -----------------------------------------------------------------

def migrations() -> list[tuple[int, str, str]]:
    """(number, name, SQL) of each, in order."""
    found = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        number, _, name = path.stem.partition("_")
        found.append((int(number), name, path.read_text(encoding="utf-8")))
    if [n for n, _, _ in found] != list(range(1, len(found) + 1)):
        raise Unusable(f"The migrations in {MIGRATIONS} must be numbered from 1 on, with none missing.")
    return found


def statements(sql: str) -> list[str]:
    """A script's statements, one by one: sqlite3's executescript would commit the transaction it's in."""
    out, part = [], ""
    for line in sql.splitlines(keepends=True):
        part += line
        if sqlite3.complete_statement(part):
            out.append(part.strip())
            part = ""
    if any(line.strip() and not line.strip().startswith("--") for line in part.splitlines()):
        raise Unusable(f"An unfinished statement at the end of a migration: {part.strip()[:80]}")
    return out


def _private(path: Path) -> None:
    """An empty file only its owner may read, for SQLite to make a database in: it gives its WAL and the rest the same
    mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))


def _address(path: PurePath) -> str:
    """The file: address SQLite opens a database by. A Windows share's \\\\server\\share\\... is file://server/share/...
    to as_uri, which SQLite refuses (an address's host may only be localhost): after an empty host, //server/share/...
    is a path, which it opens."""
    uri = path.as_uri()
    return uri if uri.startswith("file:///") else "file://" + uri.removeprefix("file:")


def connect(where: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < OLDEST_SQLITE:
        raise Unusable(f"This Python's SQLite is {sqlite3.sqlite_version}; Hylly's database needs "
                       f"{'.'.join(map(str, OLDEST_SQLITE))} or newer. A newer Python has it.")
    memory = str(where) == ":memory:"
    if readonly:
        c = sqlite3.connect(f"{_address(Path(where).resolve())}?mode=ro", uri=True, timeout=BUSY_SECONDS, isolation_level=None,
                            check_same_thread=False)
    else:
        if not memory:
            _private(Path(where))
        c = sqlite3.connect(str(where), timeout=BUSY_SECONDS, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    for pragma in ("foreign_keys = ON", "secure_delete = ON", f"busy_timeout = {int(BUSY_SECONDS * 1000)}"):
        c.execute(f"PRAGMA {pragma}")
    if not readonly and not memory:
        c.execute("PRAGMA synchronous = FULL")  # this connection's alone: WAL, which the file keeps, once it's known (Database)
    return c


def _shape(c: sqlite3.Connection) -> list[tuple]:
    """A database's tables, views and indexes by name, with each table's and view's columns as SQLite has them (name,
    type, not null, default, key, generated or not): what tells the first step's tables from any others."""
    found = c.execute("SELECT type, name, tbl_name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    return [(kind, name, table, [tuple(row)[1:] for row in c.execute("SELECT * FROM pragma_table_xinfo(?)", (name,))]
             if kind in ("table", "view") else []) for kind, name, table in found]


def standing(c: sqlite3.Connection, where: Path | str) -> tuple[int, bool]:
    """How far the database has come in this version's steps (0: none yet), and whether it's one from the test versions
    before the first release that came through all their steps (BEFORE): it has the first step's very tables, which
    migrate takes it on as. Only looked at, so a read-only connection may ask. Raises Newer for a newer version's, Older
    for one of those test versions' at another step (they counted their steps another way, so its number says nothing
    here), and Unusable for another program's."""
    mark, have = (c.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version"))
    known = len(migrations())
    if mark == APPLICATION_ID:
        if have > known:
            raise Newer(f"The database {where} is from a newer version of Hylly (its schema is {have}, this version knows "
                        f"{known}), so it's left alone, and nothing is kept in it until Hylly is updated.")
        return have, False
    if mark:
        raise Unusable(f"{where} isn't a database of Hylly's but another program's (its application id is {mark}), so it's "
                       f"left as it is. Move it aside, and Hylly makes a new one there.")
    if not have:
        return 0, False
    said = (f"The database {where} is from a test version of Hylly from before its first release, which counted its steps "
            f"another way: it came through {have} of them")
    if have > BEFORE:
        raise Older(f"{said}, more than the {BEFORE} this version knows of. So it's left as it is, and nothing is kept in it.")
    if have < BEFORE:
        raise Older(f"{said}, and this version takes on only one that came through all {BEFORE} (user_version {BEFORE}, "
                    f"with no application id yet). So it's left as it is, and nothing is kept in it. To use it, open it once "
                    f"with a test version that knows all {BEFORE} of those steps (any command that reads the records will "
                    f"do, such as hylly people list), which brings it through the rest, and then with this version.")
    first = connect(":memory:")
    try:
        for statement in statements(migrations()[0][2]):
            first.execute(statement)
        mine, theirs = _shape(c), _shape(first)
    finally:
        first.close()
    if mine != theirs:
        names = sorted({part[1] for part in mine} ^ {part[1] for part in theirs}) or [a[1] for a, b in zip(mine, theirs) if a != b]
        raise Older(f"{said}, all {BEFORE}, but its tables aren't what they made ({', '.join(names[:3])}): changed since by "
                    f"something else than Hylly, perhaps. So it's left as it is, and nothing is kept in it. A nightly copy "
                    f"of it, in state/db-backup in the library, may be put in its place.")
    return 1, True


def migrate(c: sqlite3.Connection, where: Path | str) -> None:
    """The database brought up to this version's steps (standing raises for one left alone). One already in use has a
    copy taken beside it first (copy_before), before the first step this program takes, or before one from the test
    versions is taken on as the first. Each step is taken holding the database's write lock, and only if no other
    program has taken it meanwhile: so is the copy, so two programs opening the database at once take one copy, whole,
    and the step once. A step that leaves much of the file free gives it back (_give_back)."""
    known = migrations()
    have, before = standing(c, where)
    if not before and have == len(known):
        return
    copied = not (have or before) or str(where) == ":memory:"  # a new database, or a dry run's copy: nothing to keep

    def step(due, take, number: int) -> bool:
        """One step in one transaction: if it's still due (another program may have just taken it), the copy first, as the
        database is before anything is written, and then the step."""
        nonlocal copied
        c.execute("BEGIN IMMEDIATE")
        try:
            done = due()
            if done:
                if not copied:
                    copy_before(where, number)
                    copied = True
                take()
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return done

    def header() -> None:  # the first step's tables already: only its header says so from now on, and nothing else changes
        c.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        c.execute("PRAGMA user_version = 1")

    def take(number: int, sql: str) -> None:
        for statement in statements(sql):
            c.execute(statement)
        c.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        c.execute(f"PRAGMA user_version = {number}")

    if before:
        step(lambda: c.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID, header, 1)
    took = False
    for number, _, sql in known[have:]:
        took = step(lambda: c.execute("PRAGMA user_version").fetchone()[0] < number, lambda: take(number, sql), number) or took
    if took and str(where) != ":memory:":
        _give_back(c)


def copy_before(where: Path | str, number: int) -> Path:
    """The database as it is before its step number is taken, beside it as hylly.before-NNNN.db, by a connection of its
    own that only reads: the caller holds the write lock, so nothing changes it meanwhile. One there already, from an
    earlier step of that number (a database put back from a copy, say), is never written over: this one is named for
    when it was taken too, hylly.before-NNNN-<date>_<time>.db."""
    name = f"{Path(where).stem}.before-{number:04d}"
    target, stamp, n = Path(where).with_name(f"{name}.db"), datetime.now().strftime("%Y-%m-%d_%H%M%S"), 0
    while os.path.lexists(target):
        n += 1
        target = Path(where).with_name(f"{name}-{stamp}{'' if n == 1 else f'-{n}'}.db")
    source = connect(where, readonly=True)
    try:
        copy_to(source, target)
    finally:
        source.close()
    return target


GIVE_BACK = 4  # a step that leaves a quarter of the file's pages free or more gives them back (VACUUM)


def _give_back(c: sqlite3.Connection) -> None:
    """The file made as small as what it holds, by VACUUM, when a step has left much of it free (a table made anew, the
    old one dropped): else it would stay that size, and so would its nightly copies. What's freed holds nothing
    (secure_delete), so this is only for its size. Only if no other program has the database open just now: VACUUM
    waits for none, and the pages left free are used again by the records that come."""
    pages, free = (c.execute(f"PRAGMA {name}").fetchone()[0] for name in ("page_count", "freelist_count"))
    if not free or free * GIVE_BACK < pages:
        return
    c.execute("PRAGMA busy_timeout = 0")
    try:
        with contextlib.suppress(sqlite3.OperationalError):  # busy, or no room on the disk for it: the file stays as it is
            c.execute("VACUUM")
    finally:
        c.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")


def copy_to(c: sqlite3.Connection, target: Path) -> None:
    """A consistent copy of the database, even while it's written to, by SQLite's backup API: never a plain file copy.
    The copy is in the plain journal mode, so it can be opened anywhere, also read-only. It's made under a name of this
    program's own and put in place whole, so that two programs copying at once never write into each other's copy."""
    part = target.with_name(f"{target.name}.{os.getpid()}-{secrets.token_hex(4)}.part")
    _private(part)
    try:
        out = sqlite3.connect(str(part), isolation_level=None)
        try:
            c.backup(out)
            out.execute("PRAGMA journal_mode = DELETE")
        finally:
            out.close()
        os.replace(part, target)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


def _stamp(where: Path | str) -> tuple:
    try:
        st = os.stat(where)
        return st.st_mtime_ns, st.st_size, os.path.exists(f"{where}-wal")
    except FileNotFoundError:
        return ()


def snapshot(where: Path | str) -> sqlite3.Connection:
    """The database as it is at one moment, copied into memory by SQLite's backup API from a read-only connection, and
    making nothing beside it. With a program that has it open (its journal is there), through that journal and its
    locks, which keep the copy whole. With none, a read-only look would leave a journal behind, so it's read as a file
    nothing changes (immutable), and the file's own stamp, the same before and after, says that nothing did: else it's
    read again, as a program that opened it meanwhile writes its journal first, and the file only at its end."""
    c = connect(":memory:")
    try:
        for _ in range(10):
            before = _stamp(where)
            if not before:
                break
            busy = before[2]
            source = sqlite3.connect(f"{_address(Path(where).resolve())}?mode=ro{'' if busy else '&immutable=1'}", uri=True,
                                     timeout=BUSY_SECONDS)
            try:
                source.backup(c)
            except sqlite3.DatabaseError:
                if busy or _stamp(where) == before:  # the file itself is so: nothing to wait for
                    raise
            finally:
                source.close()
            if busy or _stamp(where) == before:
                break
            time.sleep(0.2)
        else:
            raise Unusable(f"{where} kept changing while it was read; try again in a moment.")
    except BaseException:
        c.close()
        raise
    c.row_factory = sqlite3.Row
    return c


def scratch(where: Path | str) -> sqlite3.Connection:
    """For a dry run: the database as it is (snapshot), in memory, and brought up to date there, so that what the
    command would write goes nowhere: not the file, its journal, nor a copy before a migration. One that isn't there
    yet is an empty one, and isn't made."""
    c = snapshot(where)
    try:
        migrate(c, ":memory:")
    except BaseException:
        c.close()
        raise
    return c


def in_dry_run() -> bool:
    """Whether this command is a dry run (cli.main), in this thread and what it runs: its databases are copies in
    memory. Another thread, a server's say, goes on with the real ones."""
    return _DRY.get() is not None


def carried(target: Callable) -> Callable:
    """target, to run in a thread of its own with this thread's context, a dry run's databases with it: a new thread
    starts with an empty one (before Python 3.14), and would write to the real ones. Once for each thread or task, as a
    context runs in one thread at a time: threading.Thread(target=db.carried(fn)), executor.submit(db.carried(fn), x)."""
    run = contextvars.copy_context().run  # copied here, in the thread that starts it
    return lambda *args, **kw: run(target, *args, **kw)


def start_dry_run() -> None:
    _DRY.set({})


def end_dry_run() -> None:
    """The dry run's copies in memory go, and what comes next has the real databases."""
    found = _DRY.get() or {}
    _DRY.set(None)
    for copy in found.values():
        copy.close()


class Database:
    """One database, for this program: one connection, used by one thread at a time."""

    def __init__(self, where: Path | str, copy: bool = False) -> None:
        self.where, self.copy = where, copy  # copy: a dry run's, in memory (scratch)
        self._lock = threading.RLock()
        self._c: sqlite3.Connection | None = None
        self.owned: dict[str, tuple | None] = {}  # the libraries whose records it keeps, as checked (own): their marker's stamp then
        self.lost: dict[str, str] = {}  # those another database took on while this program ran, with why it refuses them since
        self.on_lost: Callable[[str], None] | None = None  # told that, once (the server: its log, and an alarm)
        self.on_note: Callable[[str], None] | None = None  # told what own did on its own (the server: its log)

    def _connection(self) -> sqlite3.Connection:
        if self._c is None and self.copy:
            self._c = scratch(self.where)
        if self._c is None:
            c = connect(self.where)
            try:
                migrate(c, self.where)
                if str(self.where) != ":memory:":  # only now: a database left alone (standing) keeps its header as it was
                    c.execute("PRAGMA journal_mode = WAL")
            except BaseException:
                c.close()
                raise
            self._c = c
        return self._c

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One transaction, whole or not at all, holding the database's write lock: another program's waits. Inside
        another, it's part of that one."""
        with self._lock:
            c = self._connection()
            if c.in_transaction:
                yield c
                return
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """The database as it is at one moment, however many questions are asked of it."""
        with self._lock:
            c = self._connection()
            if c.in_transaction:
                yield c
                return
            c.execute("BEGIN")
            try:
                yield c
            finally:
                c.execute("COMMIT")

    def copy_to(self, target: Path) -> None:
        with self._lock:
            copy_to(self._connection(), target)

    def checkpoint(self) -> None:
        """The WAL written into the database and emptied: after words were taken away, they're gone from it too."""
        with self._lock:
            self._connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self._lock:
            if self._c is not None:
                self._c.close()
                self._c = None


_open: dict[str, Database] = {}
_open_lock = threading.Lock()


def database(where: Path | str | None = None) -> Database:
    """The data folder's database, or the one named: the same object for the whole program."""
    where = where if str(where) == ":memory:" else Path(where or file())
    key = str(where) if str(where) == ":memory:" else os.path.abspath(where)
    dry = _DRY.get()
    if dry is not None and str(where) != ":memory:":  # a dry run's own, apart from what other threads use
        if key not in dry:
            dry[key] = Database(where, copy=True)
        return dry[key]
    with _open_lock:
        if key not in _open or str(where) == ":memory:":
            found = Database(where)
            if str(where) == ":memory:":
                return found
            _open[key] = found
        return _open[key]


def close_all() -> None:
    """Every database this program has open, closed (the tests, between one and the next)."""
    with _open_lock:
        for found in _open.values():
            found.close()
        _open.clear()


# ---- the library whose records it keeps -----------------------------------------------------------

def owner_file(root: Path) -> Path:
    """In the library: which database keeps its own records (the tags and corrections, the catalog, what was
    downloaded)."""
    return paths.state_dir(root) / "database.json"


def _marker_stamp(root: Path) -> tuple | None:
    """When the library's owner_file last changed, its size, and which file it is (a marker written again is a new file):
    what own reads it again by. None when it isn't there."""
    try:
        st = owner_file(root).stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def _computer(marker: dict) -> str:
    """The computer a library's marker names, as it may be shown: a name that doesn't look like one isn't."""
    computer = marker.get("computer")
    if computer == socket.gethostname():
        return "this computer, in another data folder"
    return computer if isinstance(computer, str) and COMPUTER.fullmatch(computer) else "another computer"


def own(found: Database, root: Path) -> None:
    """That this database keeps the library's own records, or raises NotHere. It does if the library's owner_file names
    the id it has and this install's token (token_file). A copy of the database, put in another settings folder or on
    another computer, has the id but not the token: it's refused as another database is, with the one command that
    takes the records over (take_over). A marker from before markers named a token, naming this database, is tied to
    this install as it's first checked (_tie): quietly, once, and said to found.on_note.

    If neither the marker nor the database has a library yet, it starts to keep its records, wherever the library is:
    on this computer's own disk or on a share. The marker is made by an exclusive create and read back, so that of two
    computers starting on a new library at once only the one it names goes on. It keeps every other computer from
    taking them on too: from there, the commands run here (remote.py). A folder without a marker is this database's
    library, moved or restored, only where the library it keeps can be read and has no marker now (_old_place): where
    it can't be reached, a disk unplugged say, the folder may be another library, and only take_over says it's that
    one, moved. A folder that isn't a library now (an unmounted disk's empty folder, paths.library_missing) is taken
    on by nothing: OSError, as for a change to its records (curation._append).

    Once checked, a check is one stat of the marker, as it's asked at every request of the server: the marker is read
    again only when it has changed. If it then names another database, or another install's, the records were moved
    while this program ran, and from then on it refuses them, whatever the marker says later, and tells found.on_lost
    once: a server here would otherwise go on keeping changes in a database nothing reads any more. A marker that has
    gone (with its disk, say) changes nothing: the records aren't taken on afresh meanwhile."""
    key = os.path.abspath(root)
    if key in found.lost:
        raise Moved(found.lost[key])
    had = key in found.owned
    try:
        stamp = _marker_stamp(root)  # before it's read: a change after this is read again at the next check
    except OSError:  # a share that has gone, say: said below, as before, or for one taken on, left as it was
        if had:
            return
        stamp = None
    if had and (stamp is None or stamp == found.owned.get(key)):  # get: another thread may have just let it go
        return
    try:
        marker = owner(root)
    except FileNotFoundError:
        if had:
            return
        marker = None
        try:
            os.stat(root)
        except OSError as e:  # a share that has gone isn't a library without records: said as such
            if share_is_gone(e):
                raise
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        raise NotHere(f"{owner_file(root)}, which says which computer keeps the library's own records, can't be read ({e}).") from None
    if marker is None and paths.library_missing(root):  # taken on, its marker and records would be on the disk under it
        raise OSError(f"The library at {root} isn't there")
    refused, tie = None, False
    with (found.read() if had else found.write()) as c:  # looked at again, it's only compared: no wait for a writer
        mine = c.execute("SELECT id, root FROM library").fetchone()
        if marker is None:
            stamp = _take_on(found, c, root, mine)
        elif mine is None or mine["id"] != marker["id"]:
            refused = marker
        elif marker.get("token") is not None and marker["token"] != _token(found):
            refused = marker  # a copy of this database: the marker names the install it was copied from, or another's
        else:
            tie = marker.get("token") is None  # as Hylly wrote it before markers named an install
            if not had:
                c.execute("UPDATE library SET root = ?", (key,))
    if refused is not None and not had:
        raise NotHere(_kept_elsewhere(root, refused, mine["id"] if mine is not None else None, found.where))
    if refused is not None:  # moved while this program ran
        elsewhere = _computer(refused)
        words = (f"The library's own records (your tags and corrections, the catalog, what was downloaded) are now kept in the "
                 f"database on {elsewhere}: {owner_file(root)} was changed to name it while this program ran. So this program "
                 f"writes them no more, and a change made here isn't kept. Stop the server here (hylly server stop), and use "
                 f"the one on {elsewhere}.")
        with found._lock:
            first = key not in found.lost
            found.lost[key] = words
            found.owned.pop(key, None)
        if first and found.on_lost is not None:
            with contextlib.suppress(Exception):  # the telling never stands in the refusal's way
                found.on_lost(words)
        raise Moved(words)
    if tie:
        stamp = _tie(found, root, marker, stamp)
    found.owned[key] = stamp


def token_file(where: Path | str | None = None) -> Path:
    """This install's own token, beside its database in the data folder: a random one, which the marker of the library
    whose records it keeps names too (own). Not in the database, so that a copy of that, put in another settings folder
    or on another computer, doesn't carry it along, and is told from the database it was copied from."""
    return Path(where or file()).with_name(TOKEN)


def _read_token(path: Path) -> str | None:
    """The token in token_file: None when there's no file, "" when it holds none."""
    try:
        said = path.read_bytes().decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    return said if TOKEN_TEXT.fullmatch(said) else ""


def _token(found: Database | Path, make: bool = False, anew: bool = False) -> str | None:
    """This install's token (token_file beside found, a database or its file), made the first time when make, readable
    by its owner only; None while there's none and none is made, and for a database in memory. What's returned is what
    the file holds, read back: of two programs of this install making it at once, both go on with the one in place
    (_place_token). A file there that holds no token, empty or cut short, counts as none once it stays so for a moment
    (another program may be writing it just now, on a disk without links): a crash while it was made there leaves one,
    and make puts a whole one in its place. Only these make one: taking a library on, tying a marker that names no
    token yet, a restore's tie, and take_over. A marker that names the token that went still refuses the records until
    take_over (own), so a new token never takes them from the install the old one tied them to. One program of this
    install makes it at a time (_making), each reading it again once its turn comes: of several meeting a spoiled one at
    once (the server and a timer at a start), the first makes a new one, and the rest go on with it. Without a turn (a
    disk that keeps no locks), a spoiled one is OSError, as several could each put their own in its place: a marker
    from before tokens then counts as it did, and the doctor names the file; anew (take_over, the owner's command)
    replaces it all the same."""
    where = found.where if isinstance(found, Database) else found
    if str(where) == ":memory:":
        return None
    path = token_file(where)
    if not (in_dry_run() or isinstance(found, Database) and found.copy):  # a dry run changes nothing
        _tidy(path)
    said = _read_token(path)
    if said or not make:
        return said or None
    with _making(path) as turn:
        said = _read_token(path)  # made meanwhile, by the program whose turn it was
        if said:
            return said
        spoiled = said == ""
        if spoiled:
            said = _token_written(path)  # being written by one that takes no turn (a version from before), on a disk without links
            if said:
                return said
            if not (turn or anew):
                raise OSError(f"{path}, this install's token, holds none, and {path.name}.lock beside it can't be locked to make "
                              f"it again: {TAKE_OVER} makes a new one")
        return _place_token(path, instead=spoiled)


MAKING_WAIT = 3.0  # seconds a program waits its turn to make the token: one making it holds it a second at most


@contextlib.contextmanager
def _making(path: Path):
    """This program's turn to make the token at path: an OS lock on records-token.lock beside it, which the system lets
    go with the program, however it ends; whether it was had. One that can't be had in MAKING_WAIT (a disk that keeps no
    locks) goes on without, as before turns were taken, but for a spoiled token's replacing (_token)."""
    from . import instance

    with open(path.with_name(f"{path.name}.lock"), "a+b") as fh:
        deadline, held = time.monotonic() + MAKING_WAIT, False
        while not (held := instance._lock(fh)) and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            yield held
        finally:
            if held:
                with contextlib.suppress(OSError):
                    instance._unlock(fh)


LEFT_FOR = 60  # seconds after which a records-token.*.tmp is a crash's leftover, not one being written


def _tidy(path: Path) -> None:
    """The files a token was written to first (_place_token), left by a crash on their way into place: gone after a
    minute. One younger may be another program's, on its way."""
    try:
        left = [p for p in path.parent.glob(f"{path.name}.*.tmp") if time.time() - p.stat().st_mtime > LEFT_FOR]
    except OSError:
        return
    for part in left:
        with contextlib.suppress(OSError):
            part.unlink()


def _token_written(path: Path) -> str | None:
    """The token in path, looked at again a moment apart while it holds none: another program of this install may be
    writing it just now, on a disk without links (_place_token)."""
    for _ in range(READ_BACK):
        said = _read_token(path)
        if said:
            return said
        time.sleep(0.05)
    return None


def _place_token(path: Path, instead: bool = False) -> str:
    """A new token, written whole to a file of its own beside path, then linked into place: only if there's none there,
    and never in a moment when it's there but empty. Never taking away one another program of this install has put
    there meanwhile: that one stays, and is the token. A disk without hard links makes it in place by an exclusive
    create instead, which a look at it meanwhile waits out (_token_written). instead: in place of one that holds no
    token. What path holds then, read back."""
    made = secrets.token_hex(16)
    part = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(made + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if instead:
            os.replace(part, path)
        else:
            try:
                os.link(part, path)
            except FileExistsError:
                pass  # made meanwhile, by another program of this install's: that one
            except OSError:  # a disk that has no hard links
                with contextlib.suppress(FileExistsError), \
                        os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(made + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
    finally:
        with contextlib.suppress(OSError):
            part.unlink()
    said = _token_written(path)
    if not said:
        raise OSError(f"{path}, this install's token, was made but holds none")
    return said


def owner(root: Path) -> dict:
    """The library's owner_file, read and judged in this one place: the marker as it says, its id there.
    FileNotFoundError when there's none; ValueError when it doesn't name one (damaged, half written, a list); any other
    OSError when it can't be read, a share that has gone among them (share_is_gone), which each caller raises or not as
    it must."""
    marker = json.loads(owner_file(root).read_text(encoding="utf-8"))
    if not isinstance(marker, dict) or not isinstance(marker.get("id"), str):
        raise ValueError("no id in it")
    return marker


def _read_back(root: Path) -> dict:
    """The marker, just made or written here, as it now says: it may be another's, made at the same moment, and still
    being written. NotHere when it can't be read."""
    for n in range(READ_BACK):
        try:
            return owner(root)
        except ValueError as e:  # half written, by another computer just now: looked at again in a moment
            if n < READ_BACK - 1:
                time.sleep(0.05)
                continue
            problem = e
        except OSError as e:
            if share_is_gone(e):
                raise
            problem = e
        break
    raise NotHere(f"{owner_file(root)}, which says which computer keeps the library's own records, can't be read ({problem}).")


def _made(root: Path, marker: dict) -> bool:
    """The library's owner_file made with marker in it, if there is none: an exclusive create, so that of two computers
    taking a new library on at once, one makes it. False when one was there already."""
    path = owner_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(marker, indent=1).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()  # its own, half written: none is better
        raise
    return True


def _take_on(found: Database, c: sqlite3.Connection, root: Path, mine) -> tuple | None:
    """own, for a library without a marker: taken on, in the transaction c, as this database's own library moved or
    restored (mine, its row), or as a new one if it has none; NotHere where it mustn't be, and where another computer
    made the marker at the same moment. The marker's stamp, as made."""
    key = os.path.abspath(root)
    if mine is not None and os.path.abspath(mine["root"]) != key:
        where = _old_place(mine, _token(found))
        if where in (THERE, COPIED):  # still there, kept by this database or a copy of it: one database, one library
            raise NotHere(f"This computer's database keeps the records of the library at {mine['root']}, which is still there, "
                          f"so it doesn't take on {root} as well: one database, one library. To keep that library's records "
                          f"elsewhere, give {root} a data folder of its own, on this computer's own disk: HYLLY_HOME set "
                          f"to a folder that holds all of that library's Hylly files, for its commands and its server.")
        if where == AWAY:
            raise NotHere(f"This computer's database keeps the records of the library at {mine['root']}, which can't be reached "
                          f"now (is its disk or share there?), so it doesn't take on {root} in its place: one database, one "
                          f"library, and this may be another one. If {root} is that library, moved: {take_over_command(root)}, "
                          f"which asks first. If it's another library, give it a data folder of its own, on this computer's "
                          f"own disk: HYLLY_HOME set to a folder that holds all of that library's Hylly files, for its "
                          f"commands and its server.")
    library_id = mine["id"] if mine is not None else secrets.token_hex(16)  # a library moved or restored is taken on
    if mine is None:
        c.execute("INSERT INTO library (id, root, since) VALUES (?, ?, ?)", (library_id, key, now()))
    else:
        c.execute("UPDATE library SET root = ?", (key,))
    if found.copy:  # a dry run's database takes the library on in memory only, in whichever thread it's asked
        return None
    token = _token(found, make=True)
    _made(root, {"id": library_id, "computer": socket.gethostname(), "since": now(), "token": token})
    stamp = _marker_stamp(root)  # before the read back, as for any look
    back = _read_back(root)
    if back["id"] != library_id or back.get("token") != token:  # made at the same moment elsewhere: the records are its
        raise NotHere(_kept_elsewhere(root, back, library_id, found.where))  # ... its library row goes back as it was
    return stamp


THERE, COPIED, GONE, AWAY = "there", "copied", "gone", "away"  # _old_place


def _old_place(mine, ours: str | None) -> str:
    """Where the library this database keeps the records of now stands, seen from another folder: THERE, its marker
    naming this database (and this install, ours, or none), so that the folder is another library; COPIED, its marker
    naming this database's id with another install's token, a copy of it elsewhere keeping them; GONE, its folder
    readable and a library, with no marker of this database's (moved, or restored, with its records here); or AWAY, as
    it can't be read or is no library now (a disk unplugged, or an unmounted one's empty folder): nothing tells the
    folder from another library then."""
    old = Path(mine["root"])
    try:
        there = owner(old)
    except FileNotFoundError:
        try:
            readable = old.is_dir() and not paths.library_missing(old)
        except OSError:
            readable = False
        return GONE if readable else AWAY
    except (OSError, ValueError):
        return AWAY
    if there["id"] != mine["id"]:
        return GONE
    return THERE if there.get("token") is None or there.get("token") == ours else COPIED


def _tie(found: Database, root: Path, marker: dict, stamp: tuple | None) -> tuple | None:
    """A marker as Hylly wrote it before markers named an install (no token), naming this database: this install's token,
    as its file holds it (_token reads it back, so two programs of this install tying it at once write the same), written
    into it with this computer's name, once and quietly, a line to found.on_note. So the library whose records
    a database kept is kept by it on, and a copy of it elsewhere is refused from now. Where it can't be written just now
    (a disk that's read-only, say), the marker counts as before, and is tied at a later start. The stamp to keep."""
    if found.copy:
        return stamp
    try:
        token = _token(found, make=True)
        tied = {**marker, "computer": socket.gethostname(), "token": token}
        atomic_write(owner_file(root), json.dumps(tied, indent=1), attempts=2)
        after = _marker_stamp(root)
    except OSError:
        return stamp
    back = _read_back(root)
    if back["id"] != marker["id"] or back.get("token") != token:  # tied at the same moment by a copy of it elsewhere
        raise NotHere(_kept_elsewhere(root, back, marker["id"], found.where))
    if found.on_note is not None:
        with contextlib.suppress(Exception):
            found.on_note(f"{owner_file(root)} now names this install too, by the token in {token_file(found.where)}: the "
                          f"library's records are kept in this database, and a copy of it elsewhere is refused them.")
    return after


def tie(root: Path, where: Path | None = None) -> None:
    """The library's marker made to name this install's token, the database it names kept: for a restore that has just
    put a backup's copy of the records in place as this computer's database (at where), the marker having come along
    from the install the backup was made on. The database itself isn't opened: it's brought up to this version, if it
    needs that, as it's first opened, and own then takes the library on at its new place."""
    marker = owner(root)
    token = _token(Path(where or file()), make=True)
    atomic_write(owner_file(root), json.dumps({**marker, "computer": socket.gethostname(), "token": token}, indent=1))


def take_over_command(root: Path) -> str:
    return f"{TAKE_OVER} --library {paths.typed(root)}"


def _kept_elsewhere(root: Path, marker: dict, mine: str | None, where: Path | str | None = None) -> str:
    """Why this database (at where) isn't given the library's records: the marker names another database (by the
    computer it names), or this one's id with another install's token: a copy of it elsewhere, or this install's own
    token gone from its data folder (token_file missing, or holding none); and how to keep them here instead."""
    here = Path(file() if str(where or ":memory:") == ":memory:" else where)
    elsewhere, take = _computer(marker), take_over_command(root)
    if marker["id"] == mine and not _read_token(token_file(here)):
        why = "is missing" if _read_token(token_file(here)) is None else "holds no token"
        return (f"This computer's database, {here}, has the library's own records (your tags and corrections, the catalog, "
                f"what was downloaded), and {owner_file(root)} names it, but by this install's token, and {token_file(here)}, "
                f"which holds it beside {here.name}, {why}. If this install has kept them all along, and that file went "
                f"astray (left behind when the data folder moved, say), put it back, or run {take}, which asks first and "
                f"ties them to this install anew. If this database is a copy of the one that keeps them, on {elsewhere}, "
                f"run this there instead (with [remote] ssh in this computer's settings, hylly does that for you), or stop "
                f"the server there (hylly server stop, there) before taking them over here.")
    if marker["id"] == mine:
        return (f"This computer's database, {here}, has the library's own records (your tags and corrections, the catalog, "
                f"what was downloaded), but as a copy: {owner_file(root)} names the database on {elsewhere} as the one that "
                f"keeps them, so a change made here would be missing there, and one made there missing here. Run this there: "
                f"with [remote] ssh in this computer's settings, hylly does that for you. To keep them here from now on "
                f"instead, as for a library moved to this computer, first stop the server on {elsewhere} (hylly server stop, "
                f"there), so that nothing changed there meanwhile is left behind; then {take}, which asks first.")
    return (f"The library's own records (your tags and corrections, the catalog, what was downloaded) are kept in the database on "
            f"{elsewhere}: run this there. With [remote] ssh in this computer's settings, hylly does that for you. To keep them "
            f"here instead, as for a library moved to this computer, first stop the server on {elsewhere} (hylly server stop, "
            f"there), so that nothing changed there meanwhile is left behind. Then bring them here: hylly restore does it all, "
            f"from a backup made there; or put the latest nightly copy, in {backups(root)}, in place of this computer's "
            f"database, {here} (the help's After the copy says how), and run {take}, which asks first. With none of the "
            f"library's records in this computer's database, {take} starts them afresh here.")


# ---- taking the records over: hylly server take-over ---------------------------------------------------------------

def taking_over(root: Path, found: Database | None = None) -> dict:
    """What take_over would do for the library at root, for its words: kept (this install keeps the records already,
    and there's nothing to do), the marker as it is (None when there's none, or it can't be read), the computer it names,
    whether it names a copy of this database, and what this database has of the records: its library's id and folder,
    and how many titles, tags and corrections, and downloads. NotHere where it can't be done: this database keeps another
    library's records, which is still there, and kept by this install (one kept by a copy of it elsewhere isn't)."""
    found = found or database()
    try:
        marker = owner(root)
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        marker = None
    ours = _token(found)
    with found.read() as c:
        mine = c.execute("SELECT id, root FROM library").fetchone()
        counts = {table: c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("title", "edit", "download")}
    same = marker is not None and mine is not None and marker["id"] == mine["id"]
    kept = same and marker.get("token") in (None, ours)  # one from before tokens is tied as own reads it
    elsewhere = mine is not None and os.path.abspath(mine["root"]) != os.path.abspath(root)
    if not kept and elsewhere and _old_place(mine, ours) == THERE:
        raise NotHere(f"This computer's database keeps the records of the library at {mine['root']}, which is still there, so "
                      f"it doesn't take over those of {root} as well: one database, one library. To keep that library's "
                      f"records on this computer too, give {root} a data folder of its own, on this computer's own disk: "
                      f"HYLLY_HOME set to a folder that holds all of that library's Hylly files, for its commands and its server.")
    return {"kept": kept,
            "marker": marker, "computer": _computer(marker) if marker is not None else None, "copy": same,
            "mine": dict(mine) if mine is not None else None, "counts": counts}


def take_over(root: Path, found: Database | None = None) -> str:
    """The library's marker written anew, naming this database and this install's token: from now on its records are
    kept here, and every other database is refused them, the one that kept them until now from its next look (own). The
    records are what this database has: of its library moved here, or a backup's or a night's copy put in its place;
    with none, they start afresh, under a new id. hylly server take-over, once its owner has said yes (a restore ties the
    marker it brought itself: tie). NotHere as taking_over says. The library's id."""
    found = found or database()
    key = os.path.abspath(root)
    try:
        before = owner(root)
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        before = None
    taking_over(root, found)  # NotHere where it can't be done
    token = _token(found, make=True, anew=True)
    with found.write() as c:
        mine = c.execute("SELECT id, root FROM library").fetchone()
        library_id = mine["id"] if mine is not None else secrets.token_hex(16)
        if mine is None:
            c.execute("INSERT INTO library (id, root, since) VALUES (?, ?, ?)", (library_id, key, now()))
        else:
            c.execute("UPDATE library SET root = ?", (key,))
        since = before.get("since") if before and before["id"] == library_id else None
        since = since if isinstance(since, str) else now()  # when the records were first kept, for a copy of them
        atomic_write(owner_file(root), json.dumps({"id": library_id, "computer": socket.gethostname(), "since": since,
                                                   "token": token}, indent=1))
    with found._lock:
        found.lost.pop(key, None)
        found.owned.pop(key, None)
    own(found, root)
    return library_id


def _private_text(path: Path):
    """A file for writing text, readable by its owner only."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", newline="\n")


# ---- reading one, and the nightly copy -----------------------------------------------------------

def tables(c: sqlite3.Connection) -> list[str]:
    return [row[0] for row in c.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def export(source: Path, folder: Path) -> dict:
    """Each table of a database, a copy not in use, as <table>.jsonl in folder, and check.json: what was checked. The
    SECRET columns are left out: the copy itself has them."""
    c = connect(source, readonly=True)
    try:
        integrity = [row[0] for row in c.execute("PRAGMA integrity_check")]
        rows = {}
        for table in tables(c):
            hidden = {column for name, column in SECRET if name == table}
            with _private_text(folder / f"{table}.jsonl") as fh:
                count = 0
                for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                    fh.write(dump({k: v for k, v in dict(row).items() if k not in hidden}) + "\n")
                    count += 1
            rows[table] = count
        schema = c.execute("PRAGMA user_version").fetchone()[0]
        mine = c.execute("SELECT id FROM library").fetchone() if "library" in rows else None
    finally:
        c.close()
    said = {"made": now(), "library": mine[0] if mine else None,  # whose copy it is (whose), for nights
            "integrity": "ok" if integrity == ["ok"] else "; ".join(integrity), "schema": schema, "rows": rows}
    with _private_text(folder / "check.json") as fh:
        fh.write(json.dumps(said, indent=1))
    return said


def backups(root: Path) -> Path:
    return paths.state_dir(root) / "db-backup"


def marked(root: Path) -> str | None:
    """The id the library's owner_file names: None when there's none, or it can't be read. A share that has gone is
    said as such."""
    try:
        return owner(root)["id"]
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        return None


EARLIER = "earlier"  # whose: a copy made before the library's records were kept in the database, which has no library table:
# one of the test versions' first step, before the first release. It counts as the library's, so it goes in its turn (nights);
# this version never opens one (Older), and a restore never puts one in place


def whose(night: Path) -> str | None:
    """The id of the library whose records a nightly copy keeps: as its check.json says, or, for a copy made before
    check.json said it, as the copy's own database does. EARLIER for a copy from before a database kept a library's
    records; None for a copy of a database that kept none though it could, or one that can't be read."""
    said = read_json(night / "check.json")
    if "library" in said:
        return said["library"] if isinstance(said["library"], str) else None
    try:
        c = connect(night / FILE, readonly=True)
    except (sqlite3.Error, Unusable):
        return None
    try:
        if "library" not in tables(c):
            return EARLIER
        row = c.execute("SELECT id FROM library").fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    return row[0] if row and isinstance(row[0], str) else None


def _nights(root: Path, today: str | None) -> tuple[list[Path], list[Path]]:
    """The folders of db-backup named for a day that has come (DAY), each holding a database, oldest first: the
    library's (nights), and the rest (strangers)."""
    where, today = backups(root), today or datetime.now().strftime("%Y-%m-%d")
    if not where.is_dir():
        return [], []
    days = sorted(p for p in where.iterdir()
                  if DAY.fullmatch(p.name) and p.name <= today and not is_link(p) and p.is_dir() and (p / FILE).is_file())
    mark = marked(root) if days else None
    if mark is None:  # no database has taken the library on, so nothing tells its copies from another's: all count
        return days, []
    ours = [p for p in days if whose(p) in (mark, EARLIER)]
    return ours, [p for p in days if p not in ours]


def nights(root: Path, today: str | None = None) -> list[Path]:
    """The nightly copies of the library's records, oldest first: of the database the library's marker names (whose),
    and those made before a database kept the library's records (EARLIER), which were its own then. Nothing else in
    db-backup counts, nor is ever removed: not even another database's copies (strangers). A library with no marker
    has had no database take it on, so every copy there counts, as all did before copies were told apart."""
    return _nights(root, today)[0]


def strangers(root: Path, today: str | None = None) -> list[Path]:
    """The copies in db-backup of another database than the one the library's marker names, or of one that kept no
    library's records though it could have: never counted, never removed, only said. None when there's no marker."""
    return _nights(root, today)[1]


def _copied_here(root: Path, where: Path) -> None:
    """That the database at where keeps the library's records, or NotHere: the library's marker names the id of its
    library row, and the token beside it where it names one (own). Only looked at, never taken on (own does that): a
    computer that doesn't keep them, nor a copy of the database elsewhere, never copies its database over the copies of
    the one that does."""
    try:
        marker = owner(root)
    except FileNotFoundError:
        raise NotHere(f"{owner_file(root)}, which names the database that keeps the library's records, isn't there: no "
                      f"database keeps them yet, so none is copied. This computer's database is copied once it keeps the "
                      f"records, which Hylly's server takes on when it starts with the library there.") from None
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and share_is_gone(e):
            raise
        raise NotHere(f"{owner_file(root)}, which names the database that keeps the library's records, can't be read "
                      f"({e}), so none is copied.") from None
    with database(where).read() as c:
        mine = c.execute("SELECT id FROM library").fetchone()
    token = marker.get("token")
    if mine is None or mine["id"] != marker["id"] or (token is not None and token != _read_token(token_file(where))):
        computer = marker.get("computer")
        computer = ("this computer, in another data folder" if computer == socket.gethostname()
                    else computer if isinstance(computer, str) and COMPUTER.fullmatch(computer) else "another computer")
        raise NotHere(f"The library's records are kept in the database on {computer}, not in this one ({where}): only "
                      f"that one is copied, and the copies already there are left as they are.")


def nightly(root: Path, now: datetime | None = None, where: Path | None = None, fresh: bool = False) -> Path | None:
    """Tonight's copy on the library's disk, made first beside the database and moved there whole, if there's a
    database, it keeps the library's records (NotHere if not), and the copy isn't made yet. The copy is the database at
    one moment, with every table beside it as JSON lines. Then, when this one's check is good, those before it go once
    BACKUPS_KEPT newer ones are there, and those more than NIGHTS_DAYS older than today too: this one always stays.
    fresh: made now even if today's is there (for a backup), which it takes the place of only once it's whole. Another
    copy of the database being made meanwhile is waited for (_one_copy_at_a_time), and TimeoutError says if it takes
    too long."""
    where = Path(where or file())
    if not where.is_file() or paths.library_missing(root):
        return None
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    if not fresh and backups(root) / day in nights(root, day):
        return None  # tonight's is made: the live database isn't opened every hour only to be asked whose it is
    with _one_copy_at_a_time(where):
        return _nightly(root, where, day, fresh)


@contextlib.contextmanager
def _one_copy_at_a_time(where: Path):
    """A copy of the database at where, as the only one being made of it: two at once (two backups' fresh copies, or a
    backup's and the night's) would share hylly.db.now or <day>.fresh, and one could take the other's away mid-copy.
    A second one waits for the first, up to COPY_WAIT, and then says so (TimeoutError, which an OSError is). By an OS
    lock on db-copy.lock beside the database, as runs.py does a job's: it goes with the process however that ends."""
    from . import instance

    with open(where.with_name("db-copy.lock"), "a+b") as fh:
        began = time.monotonic()
        while not instance._lock(fh):
            if time.monotonic() - began > COPY_WAIT:
                raise TimeoutError(f"another copy of the database ({where}) has been under way for more than "
                                   f"{COPY_WAIT / 60:.0f} minutes, so this one wasn't made: try again once that one is done")
            time.sleep(0.5)
        try:
            yield
        finally:
            instance._unlock(fh)


def _nightly(root: Path, where: Path, day: str, fresh: bool) -> Path | None:
    """nightly, as the only copy of the database being made."""
    target = backups(root) / day
    had = target in nights(root, day)
    if had and not fresh:
        return None  # made meanwhile, by the copy that was waited for
    _copied_here(root, where)
    if os.path.lexists(target) and is_link(target):
        target.unlink()  # something put in tonight's place: tonight's copy goes there instead
    elif target in strangers(root, day):
        raise NotHere(f"{target} holds a copy of another database, so tonight's isn't made there: that one is left as "
                      f"it is. Move it out of {backups(root)} to have tonight's copy.")
    elif target.exists() and not had:
        shutil.rmtree(target, ignore_errors=True)
    work = where.with_name(f"{FILE}.{'now' if fresh else 'tonight'}")  # a backup's and the server's own apart
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    try:
        database(where).copy_to(work / FILE)
        checked = export(work / FILE, work)
        (work / "README.txt").write_text(README.format(day=day), encoding="utf-8")
        part = target.with_name(f"{day}.{'fresh' if fresh else 'part'}")
        shutil.rmtree(part, ignore_errors=True)
        part.parent.mkdir(parents=True, exist_ok=True)
        _onto_the_library(work, part)
        if had:  # today's earlier copy goes only now that this one is whole
            old = target.with_name(f"{day}.old")
            shutil.rmtree(old, ignore_errors=True)
            target.rename(old)
            try:
                part.rename(target)
            except OSError:
                old.rename(target)
                raise
            shutil.rmtree(old, ignore_errors=True)
        else:
            part.rename(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ours = nights(root, day)  # none dated after today: such a copy never makes others go
    oldest = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=NIGHTS_DAYS)).strftime("%Y-%m-%d")  # kept: this, or later
    good = checked.get("integrity") == "ok"  # none goes after a copy that isn't: a damaged one is never all that's left
    by_age = [p for p in ours[-BACKUPS_KEPT:-1] if p.name < oldest] if good else []
    for old in (ours[:-BACKUPS_KEPT] if good else []) + by_age:  # by their age only after a copy that's good, which then stays
        shutil.rmtree(old, ignore_errors=True)
    return target


def _onto_the_library(work: Path, part: Path) -> None:
    """The copy made beside the database, onto the library's disk: its contents only, the folder and each file their
    owner's only by the mode they're made with (your umask may take more away). Never copytree's copystat: its chmod
    a disk that keeps modes as extended attributes (Linux's ntfs3) would store for each file, and on one that refuses
    chmod it would stop the copy."""
    part.mkdir(mode=0o700)
    for path in sorted(work.iterdir()):
        made = os.open(part / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(made, "wb") as out, open(path, "rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)


def latest_day(now: datetime | None = None) -> str:
    """The latest day a copy a backup or a restore takes may be dated: AHEAD_DAYS after this computer's day."""
    return ((now or datetime.now()) + timedelta(days=AHEAD_DAYS)).strftime("%Y-%m-%d")


def dated_ahead(root: Path, now: datetime | None = None) -> list[Path]:
    """The library's copies dated after latest_day: made under a clock that was wrong, they're never taken, only said."""
    return [p for p in nights(root, ANY_DAY) if p.name > latest_day(now)]


def last_backup(root: Path, now: datetime | None = None, ahead: bool = False) -> tuple[str, dict] | None:
    """The day of the latest copy (nights, as of now), and what was checked of it. ahead: up to latest_day, as a backup
    takes it: a server in another time zone may have dated its copy tomorrow here."""
    days = nights(root, latest_day(now) if ahead else (now or datetime.now()).strftime("%Y-%m-%d"))
    if not days:
        return None
    return days[-1].name, read_json(days[-1] / "check.json") or {"integrity": "check.json can't be read"}
