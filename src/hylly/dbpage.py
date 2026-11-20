"""The Database page (/database): the owner's look at the tables of the database, live, for an owner logged in on any
device, as the Server page (serve.Handler._database_page; docs/database.md, Looking inside).

It can only read, three times over: the connection is opened read-only, PRAGMA query_only is on, and SQLite's
authorizer refuses everything but reading: no ATTACH, no PRAGMA, nothing that writes. The password hashes and the
browser logins' digests read as nothing, so they never leave the database through here. Every question has a time limit
(SECONDS) and a row limit (ROWS): a question that would run for ever, or bring the whole catalog, is cut short and says so.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from . import db
from .words import many

HIDDEN = db.SECRET  # secrets: read as NULL
REFUSED_FUNCTIONS = frozenset({"fts3_tokenizer", "load_extension"})  # say where memory is, or load code: never asked
AS_TABLES = frozenset({"sqlite_master", "sqlite_schema", "json_each", "json_tree"})  # read as tables too: the schema, and JSON
LONGEST = 10_000_000  # bytes of one value SQLite makes for a question: a question can't fill the server's memory
ROWS = 500  # at most, for a question
PAGE = 50  # rows of a table at a time
SECONDS = 3.0
SQL_MOST = 10_000  # characters
TEXT_MOST = 2_000  # characters of one value shown; the rest is said to be there
EXAMPLES = [  # (what, the SQL): questions across everything, to learn SQL on
    ("Titles by kind", "SELECT kind, count(*) AS titles FROM title GROUP BY kind ORDER BY titles DESC;"),
    ("What people finished, by month", "SELECT strftime('%Y-%m', time, 'unixepoch') AS month, count(*) AS finished\n"
                                       "FROM finished WHERE done GROUP BY month ORDER BY month;"),
    ("Titles on most lists", "SELECT title_t, count(*) AS lists FROM list_entry GROUP BY title ORDER BY lists DESC LIMIT 10;"),
    ("Bundles with titles nobody has opened", "SELECT bundle, count(*) AS unopened FROM title t\n"
                                              "WHERE bundle IS NOT NULL AND NOT EXISTS (SELECT 1 FROM place p WHERE p.title = t.id)\n"
                                              "GROUP BY bundle ORDER BY unopened DESC LIMIT 20;"),
    ("Your latest changes", "SELECT e.at, e.who, e.field, e.op, e.value, count(t.title) AS titles\n"
                            "FROM edit e LEFT JOIN edit_title t ON t.edit = e.seq GROUP BY e.seq ORDER BY e.seq DESC LIMIT 20;"),
    ("Each build of the catalog", "SELECT generated, titles FROM catalog_build ORDER BY seq DESC;"),
    ("What was downloaded, by month", "SELECT substr(downloaded_at, 1, 7) AS month, shop, count(*) AS files,\n"
                                      "round(sum(size) / 1e9, 1) AS gb FROM downloaded GROUP BY month, shop ORDER BY month DESC, shop;"),
    ("The biggest downloads", "SELECT shop, title, ext, round(size / 1e6) AS mb FROM downloaded ORDER BY size DESC LIMIT 20;"),
]


class Refused(ValueError):
    """A question this page doesn't answer: the words say why."""


def _authorizer(known: set[str]):
    """SQLite's word on each step of a question: reading the database's own tables and views (known), a WITH's own
    rows (no database's), JSON taken apart (AS_TABLES), and the secrets as nothing; SQL's functions, but not those of
    REFUSED_FUNCTIONS; nothing else, not the rest of SQLite's built-in virtual tables, such as dbstat."""
    def authorize(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ:
            if dbname is not None and arg1 not in known:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_IGNORE if (arg1, arg2) in HIDDEN else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if str(arg2 or "").lower() in REFUSED_FUNCTIONS else sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def connect(where: Path | None = None) -> tuple[sqlite3.Connection, int]:
    """A connection that can only read, with the authorizer in place, and the database's schema version."""
    c = db.connect(where or db.file(), readonly=True)
    try:
        schema = c.execute("PRAGMA user_version").fetchone()[0]
        c.execute("PRAGMA query_only = ON")
        known = {row[0] for row in c.execute("SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')")}
        if hasattr(c, "setlimit"):
            c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, LONGEST)
        c.set_authorizer(_authorizer(known | AS_TABLES))
    except BaseException:
        c.close()
        raise
    return c, schema


def _ask(c: sqlite3.Connection, sql: str, args=(), most: int = ROWS) -> dict:
    """Up to most rows of a question, within SECONDS. Values too long are cut, and say so."""
    deadline = time.monotonic() + SECONDS
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 20_000)
    started = time.monotonic()
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchmany(most + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise Refused(f"It took more than {SECONDS:g} seconds, so it was stopped.") from None
        raise Refused(str(e)) from None
    except sqlite3.DatabaseError as e:  # not authorized, and the like
        if "not authorized" in str(e) or "readonly" in str(e).replace("-", ""):
            raise Refused("Only reading here: this page can't change the database, and asks it nothing but to read.") from None
        raise Refused(str(e)) from None
    except (sqlite3.Error, sqlite3.Warning) as e:  # whatever else sqlite3 says of a question: two statements at once are
        raise Refused(str(e)) from None  # a Warning before Python 3.12, and a value it can't take an InterfaceError
    finally:
        c.set_progress_handler(None, 0)
    columns = [d[0] for d in cur.description or []]
    return {"columns": columns, "rows": [[_shown(v) for v in row] for row in rows[:most]], "more": len(rows) > most,
            "ms": round((time.monotonic() - started) * 1000)}


def _shown(value):
    if isinstance(value, bytes):
        return f"({many(len(value), 'byte')})"
    if isinstance(value, str):
        value = re.sub(r"scrypt\$[^\s:\"']+", "scrypt$…", value)  # a password's hash outside HIDDEN, should one be there
        if len(value) > TEXT_MOST:
            return value[:TEXT_MOST] + f"… ({many(len(value) - TEXT_MOST, 'more character')})"
    return value


def tables(c: sqlite3.Connection) -> list[dict]:
    """Every table, with its rows."""
    names = [row[0] for row in _ask(c, "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")["rows"]]
    return [{"name": n, "rows": _ask(c, f'SELECT count(*) FROM "{n}"')["rows"][0][0]} for n in names]


def table(c: sqlite3.Connection, name: str, page: int = 0) -> dict:
    """One table: how it's made, and its rows a page at a time, in the order they went in."""
    found = _ask(c, "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,))["rows"]
    if not found or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise Refused("No such table.")
    page = max(0, int(page))
    rows = _ask(c, f'SELECT * FROM "{name}" ORDER BY rowid LIMIT ? OFFSET ?', (PAGE + 1, page * PAGE), most=PAGE)
    hidden = sorted(col for tab, col in HIDDEN if tab == name)
    return {"name": name, "sql": found[0][0], "page": page, "size": PAGE, "hidden": hidden, **rows}  # size: its pages' rows


def question(c: sqlite3.Connection, sql) -> dict:
    """Your own SQL: one statement, that only reads."""
    if not isinstance(sql, str) or not sql.strip():
        raise Refused("Write a question in SQL, such as SELECT * FROM title LIMIT 10.")
    if len(sql) > SQL_MOST:
        raise Refused(f"At most {SQL_MOST:,} characters.")
    return _ask(c, sql.strip())


def limits() -> dict[str, int]:
    """What a question and a table's pages are held to, which the page says and counts its rows by, sent with the
    tables (serve.Handler._database_tables): it keeps no copy of these numbers."""
    return {"rows": ROWS, "page": PAGE}
