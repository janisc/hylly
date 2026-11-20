"""Who may use the library server, and the access log of who did.

Users and passwords: in the database (db.py, the table account), kept with `hylly user add|remove|restore|list`. Only
salted scrypt hashes are stored, never a password; a hash says what it was made at, and one made at a lower cost than
today's is made again at its user's next login (made_again). The first user added is the library's owner.

Every request logs in, wherever it comes from, and each user's role is the same everywhere: the server tells nothing
by where a request comes from. Only the login page and what it needs, and /healthz, answer without a login
(serve.Handler). Reading apps (KOReader, Panels, Yomu) log in with HTTP Basic. Browsers get a login page instead, which
password managers fill in (iCloud Keychain, Google Password Manager), and keep the login in a cookie: a
random token, of which only the SHA-256 is stored (browser_login), so what's kept can't be used to log in.
A session ends after 30 days without use (using the library moves it on, once a day at
most), a year after logging in at the latest, at Log out, or when its user's password changes or the user
is removed. Both ways only encode the password, so they belong behind HTTPS (Tailscale Serve, a tunnel) wherever
the network isn't your own.
A one-time sign-in link (sign_in_link: hylly user link, and what hylly serve and hylly demo open) logs a browser in as
one user: once, within LINK_MINUTES, and only while that user's password is the one it was made with. Only its
SHA-256 is kept, beside the sessions, in a form no cookie can match.
A verified login is remembered, so a page full of covers costs one password check, and an
address with 10 wrong passwords within 15 minutes is refused until the 15 minutes have passed: an IPv6 one from
outside by its /64, which one home or one machine is given whole. Past SLOW_NAME wrong passwords for one name within
those 15 minutes, from anywhere, or SLOW_ALL from all addresses together, each try from outside waits its turn
(Gate._turn): a slowdown against a stranger guessing from many addresses, which never holds back a sender inside, at
home or on the tailnet (inside).

The access log: access.log, one line per request in the Combined Log Format that log tools
(GoAccess, fail2ban) read, for 30 days (AccessLog). [server] access_log = false turns it off. A name tried at a
login goes into it, and into the server's log at a lockout, only when it is a user's (Gate.tried): people do type
their password in the name field. Every field is cleaned (storage.shown), and what a sender chose is cut (FIELD_MOST),
so a request can't write a line of its own nor push the log's history out with a few huge ones.

The library's addresses are here too, beside who is inside: on this computer (here), for other devices (other_devices),
and the plain one at home as well (home_too), with this computer's own on the home network (lan_address), as the
server's pages, its start and the commands show them.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from types import EllipsisType

from .exits import USAGE, Exit
from .settings import CHOICES, get as settings_section
from .storage import append_log, aside, atomic_write, shown
from . import db, paths
from .words import many

# What a user may do (docs/architecture.md, The roles): a reader only reads; a librarian changes one title at a time;
# the owner changes anything, many titles at once too, and has the library management.
ROLES = ("borrower", "reader", "librarian", "owner")
NAME = re.compile(r"[\w.@-]{1,64}")
# A password's hashing cost, today's guidance for scrypt: a quarter of a second and 128 MB a hash on a recent laptop, so
# that a copy of the database (the nightly ones on the library's disk among them) costs a stranger as much a guess
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1
# The most memory one hash or check may take: today's cost takes 128 MB and a little (128 * r * N), past OpenSSL's own
# limit of 32 MB, so it is given; a stored hash that would take more (a database written by hand) is no password, rather
# than GBs taken at each login
SCRYPT_MAXMEM = 256 << 20
CHECKS = threading.BoundedSemaphore(2)  # hashes and checks at a time: a flood of logins waits, it can't take GBs of memory
# The headers a trusted proxy may name the sender in ([server] proxy_header, as settings.py lists them), and all those
# set by proxies and tunnels, believed only from [server] trusted_proxies (client_address, serve.Handler._trusted)
PROXY_HEADERS = CHOICES["server", "proxy_header"]
FORWARDED = (*PROXY_HEADERS, "X-Forwarded-Proto", "X-Forwarded-Host")
LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))  # trusted_proxies as they come
FAILS, WINDOW = 10, 15 * 60  # wrong logins from one address (an outside IPv6 one's /64) within WINDOW seconds lock it out that long
# A slowdown (Gate._turn): past SLOW_NAME wrong logins for one name within WINDOW, from anywhere, or past SLOW_ALL from
# all addresses together, a password from outside is checked only at its turn, SLOW_TURN seconds apart, so a stranger
# guessing from many addresses gets one try in two seconds, not four a second. Far above a few mistakes: you mistyping
# yours, or a family together, never come near it. A try waits for its turn in its own request's thread, at most
# SLOW_WAIT seconds; one whose turn is further off is answered at once, try again in a moment. So a few threads wait at
# most, each a few seconds, and the server's 128 places to answer can't all be held waiting. A sender inside (at home,
# on the tailnet, this computer: inside) never waits a turn, so a stranger can't keep you out there; its wrong logins
# still count, towards its own lockout and the ceilings.
SLOW_NAME, SLOW_ALL = 10, 30
SLOW_TURN, SLOW_WAIT = 2.0, 4.0
SWEEP = 1.0  # seconds between the looks over every address counted, for those whose wrong logins are all old (Gate._forget)
# Inside (inside): this computer, a home network's own addresses (the private IPv4 ranges, fc00::/7 and the
# link-local ones) and a tailnet's (100.64.0.0/10; fd7a:115c:a1e0::/48, within fc00::/7)
INSIDE = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                  "169.254.0.0/16", "fe80::/10", "fc00::/7", "100.64.0.0/10"))
LINK_MINUTES = 10  # a one-time sign-in link works this long after it's made, and once (sign_in_link)
LINK_TOKEN = re.compile(r"[A-Za-z0-9_-]{40,64}")  # what one looks like: anything else is no link, and isn't looked up
LOG_LIMIT = 5 << 20  # access.log moves to access.log.1 beyond this: 5.2 MB, as the help says it
# A field of an access-log line that the sender chose is cut beyond this many characters, its middle making way for a
# mark (…), so the start and the end stay (a book's name and its kind, a browser's system and its name). Python's
# server takes 64 KB for each: sixty such requests used to push the last 30 days out of both files. A book's path in
# another alphabet takes nine characters a letter in the address, so the request line keeps 2,000.
FIELD_MOST = {"request": 2000, "referer": 200, "agent": 200}
LOG_DAYS = 30  # and in both, only the lines of the last 30 days are kept (AccessLog)
SESSION_DAYS = 30  # a browser's login ends after this many days without use ...
SESSION_LIMIT_DAYS = 365  # ... and at the latest this long after logging in
SESSIONS_KEPT = 500  # beyond this the oldest go: logging in again and again can't grow the file without end
SESSIONS_EACH = 20  # one user's browsers kept logged in at most: logging in again and again ends only one's own oldest
VERIFIED_KEPT = 1000  # reading apps' logins remembered, so the password isn't checked at every request
COOKIE = "hylly"  # the browser's login; over HTTPS it is "__Host-hylly", which only this site itself can set
COOKIE_HTTPS = "__Host-hylly"


def store() -> db.Database:
    """The database, which keeps the users and the browsers' logins."""
    return db.database()


def _accounts(c) -> dict[str, tuple[str, str]]:
    return {row["name"]: (row["role"], row["hash"]) for row in c.execute("SELECT name, role, hash FROM account ORDER BY name")}


def access_log_file() -> Path:
    return paths.place("state") / "access.log"


def settings() -> bool:
    """[server] access_log from settings.toml, checked with the rest of it (settings.py): access_log = "no" in quotes
    stops the server, rather than leave the log on."""
    return settings_section("server")["access_log"]


# ---- passwords and users ---------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    with CHECKS:
        key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                             maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, key = stored.split("$")
        if kind != "scrypt":
            return False
        want = base64.b64decode(key)
        with CHECKS:
            got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                 dklen=len(want), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def below_today(stored: str) -> bool:
    """Whether a stored hash was made at a lower cost than today's (SCRYPT_N, SCRYPT_R, SCRYPT_P): one from before the
    cost was raised, made again at its user's next login (made_again). Not one at a higher cost, nor one that can't be
    read, which is no password at all."""
    try:
        kind, n, r, p, _, _ = stored.split("$")
        return kind == "scrypt" and (int(n) < SCRYPT_N or int(r) < SCRYPT_R or int(p) < SCRYPT_P)
    except ValueError:
        return False


def again_if_below(password: str, stored: str) -> str | None:
    """A new hash of a password just checked against stored, at today's cost, when stored is below it (below_today);
    else None. Made before the database is written to, as it takes its moment."""
    return hash_password(password) if below_today(stored) else None


def made_again(c, name: str, stored: str, again: str) -> str:
    """The user's password hash made again at today's cost (again, of the password just checked against stored), in
    the change c is making: only while stored is still theirs, so a new password set meanwhile stays. Their browsers'
    logins and sign-in links made with stored go on to the new hash's stamp: it is the same password, and a new
    password's stamp ends them (Sessions.user). The hash this login goes on with: again, or stored when that wasn't
    theirs any more, whose login then ends at once, as with any password changed while it was checked."""
    if c.execute("UPDATE account SET hash = ? WHERE name = ? AND hash = ?", (again, name, stored)).rowcount == 0:
        return stored
    c.execute("UPDATE browser_login SET stamp = ? WHERE name = ? AND stamp = ?",
              (_password_stamp(again), name, _password_stamp(stored)))
    return again


@functools.cache
def _decoy() -> str:
    return hash_password(secrets.token_hex(8))


def _checked(password: str, stored: str | None) -> bool:
    """Whether password is the one stored is a hash of (None: no such user), in about the time of a check at today's
    cost whatever is stored: an unknown name is checked against a decoy made at today's cost, and so, after it, is a
    wrong password for a hash made below it (below_today), which alone would answer sooner. Else a stranger could tell
    the users' names from the others, and those who haven't logged in since the cost was raised, by the time a wrong
    password takes. An old hash's own check comes on top of the decoy's, an eighth of it at 2**14 (about 280 ms against
    250 ms on a recent laptop): hard to tell over a network in the few tries the lockout and the slowdown leave. A right
    one for such a hash takes its time making the new one (again_if_below)."""
    if check_password(password, stored or _decoy()) and stored:
        return True
    if stored and below_today(stored):
        check_password(password, _decoy())  # for its time only
    return False


def load_accounts() -> dict[str, tuple[str, str]]:
    """name -> (role, password hash), by name. Raises db.Newer for a database of a newer version of Hylly."""
    with store().read() as c:
        return _accounts(c)


def peek_accounts() -> dict[str, tuple[str, str]]:
    """For a look that mustn't change anything (hylly doctor): the users as the database has them, opened read-only.
    Nobody while there's no database yet, or one from before it kept the users."""
    if not db.file().is_file():
        return {}
    c = db.connect(db.file(), readonly=True)
    try:
        if c.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'account'").fetchone() is None:
            return {}
        return _accounts(c)
    finally:
        c.close()


def account_marks() -> dict[str, tuple[str, str]]:
    """name -> (a mark of the user's password hash, when they were added), for what follows a user through a rename
    (bell.Watch's devices): a rename keeps both, a new password the second only, and a new user of the name, or one
    brought back, neither, as every hash has a salt of its own. The mark is the one the alarms already keep of each
    user (bell.Watch.check_users), of no use for guessing the password. Raises db.Newer as load_accounts does."""
    with store().read() as c:
        return {row["name"]: (hashlib.sha256(row["hash"].encode()).hexdigest()[:16], row["since"])
                for row in c.execute("SELECT name, hash, since FROM account")}


def _role_ok(role: str) -> None:
    if role not in ROLES:
        raise Exit(USAGE, f"A role is one of: {', '.join(ROLES)}.")


def kept_aside(c, name: str) -> bool:
    """Whether what was kept of a removed user is kept aside under name (people.aside lists it). The library's records
    still say what they did under that name, so no other user may have it (add_user, rename_user) until they come back
    (restore_user) or what's kept aside is erased for good (people.erase), which takes the name out of those records."""
    return c.execute("SELECT 1 FROM person WHERE name = ? AND removed IS NOT NULL", (name,)).fetchone() is not None


def _not_kept_aside(c, name: str, old: str | None = None) -> None:
    """Exit, with what to do, when name's earlier user is kept aside (kept_aside): for a new user of it (add_user), or
    one renamed to it from old (rename_user)."""
    if not kept_aside(c, name):
        return
    if old is not None:
        raise Exit(USAGE, f"{name} was a user before, and what was kept about them is kept aside under that name: "
                          f"renamed to it, {old} would take over what the library's records say {name} did (shelves for "
                          f"everyone, changes, the word on reported titles), and hylly user restore {name} couldn't "
                          f"bring that back. Choose another name, or erase what's kept aside of {name} for good first "
                          f"(hylly people erase {name}). Nothing changed.")
    raise Exit(USAGE, f"{name} was a user before, and what was kept about them is kept aside under that name: a new "
                      f"user of that name would take over what the library's records say {name} did (shelves for "
                      f"everyone, changes, the word on reported titles). Bring them back with hylly user restore "
                      f"{name}, or erase what's kept aside of {name} for good first (hylly people erase {name}), which "
                      f"takes the name out of those records too; or choose another name. Nothing changed.")


def name_free(name: str, old: str | None = None) -> None:
    """Exit as add_user, or rename_user from old, would for a name whose earlier user is kept aside: asked before a
    password is (hylly user add), and by the dry runs. A user's own name passes add_user: that's a new password."""
    with store().read() as c:
        if old is not None or c.execute("SELECT 1 FROM account WHERE name = ?", (name,)).fetchone() is None:
            _not_kept_aside(c, name, old)


def add_user(name: str, password: str, role: str | None = None) -> bool:
    """Adds the user, or sets a new password, keeping the role unless told; True if new. A new user is a reader unless
    told, but the very first one, with nobody else there, is the owner: there is no other way to the library's
    management, and the one who sets up the library adds themselves first. Never a new user of a name whose earlier
    user is kept aside (_not_kept_aside): they would take over what the library's records say that user did."""
    if not NAME.fullmatch(name):
        raise Exit(USAGE, "A user name is 1 to 64 letters, digits and . _ @ - (no spaces).")
    if role is not None:
        _role_ok(role)
    hashed = hash_password(password)
    with store().write() as c:
        was = c.execute("SELECT role FROM account WHERE name = ?", (name,)).fetchone()
        if was is None:  # a new user: what was kept of someone of that name before isn't theirs
            _not_kept_aside(c, name)
            _set_aside(c, name)
        first = was is None and c.execute("SELECT 1 FROM account LIMIT 1").fetchone() is None
        c.execute("INSERT INTO account (name, role, hash, since) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT (name) DO UPDATE SET role = excluded.role, hash = excluded.hash",
                  (name, role or (was["role"] if was else "owner" if first else "reader"), hashed, db.now()))
    return was is None


def owners(accounts: dict[str, tuple[str, str]]) -> list[str]:
    """The owners among the users (load_accounts, peek_accounts), by name."""
    return [name for name, (role, _) in accounts.items() if role == "owner"]


def removed_stamp(now: datetime | None = None) -> str:
    """When a person's row was set aside, to the microsecond: two removals of one name within a second still come in
    the order they happened (latest_removed_first)."""
    return (now or datetime.now()).astimezone().isoformat(timespec="microseconds")


def latest_removed_first(rows: list) -> list:
    """Rows of person set aside (with id and removed), the latest removed first: by when, to the microsecond, then by
    the row's id, for stamps from before they were kept so finely. A stamp that can't be read comes last."""
    def when(row) -> float:
        try:
            return datetime.fromisoformat(row["removed"]).timestamp()
        except (TypeError, ValueError):
            return float("-inf")

    return sorted(rows, key=lambda row: (when(row), row["id"]), reverse=True)


def _set_aside(c, name: str) -> bool:
    """What's kept about the person of that name, no longer theirs (people.aside lists it): in the same change as the
    user's."""
    return c.execute("UPDATE person SET removed = ?, rev = rev + 1 WHERE name = ? AND removed IS NULL",
                     (removed_stamp(), name)).rowcount > 0


def set_role(name: str, role: str) -> bool:
    """A user's new role, in force from their next request; False if there is no such user."""
    _role_ok(role)
    with store().write() as c:
        return c.execute("UPDATE account SET role = ? WHERE name = ?", (role, name)).rowcount > 0


# What the library's records name a person by, by their name: the shelves for everyone they made, their changes to the
# catalog (tags, corrections, answers) and their word on reported titles. The server tells whose they are by the name
# someone logs in with, so a rename changes these too (rename_user); a question's answer names them in its JSON.
NAMED_BY = (("shared_shelf", "made_by"), ("edit", "who"), ("report_decision", "decided_by"))
# What those records name a user by once what was kept aside of them is erased for good (people.erase), so that the name
# is free and a later user given it takes over nothing. No user can have it, as a name has no spaces (NAME): a shelf for
# everyone they made stays, and only an owner may change it.
FORMER = "a former user"


def _named(c, name: str) -> int:
    """How many of the library's records name name (NAMED_BY, and the questions' answers)."""
    found = sum(c.execute(f"SELECT count(*) FROM {table} WHERE {column} = ?", (name,)).fetchone()[0]
                for table, column in NAMED_BY)
    return found + c.execute("SELECT count(*) FROM question WHERE json_extract(answer, '$.by') = ?", (name,)).fetchone()[0]


def named(name: str) -> int:
    """How many of the library's records name name: what hylly people erase says would name FORMER instead."""
    with store().read() as c:
        return _named(c, name)


def name_in_records(c, old: str, new: str) -> int:
    """What the library's records name old by, named new, in the change c is making: for a rename (rename_user), or
    FORMER for an erase (people.erase). How many records changed."""
    changed = sum(c.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old)).rowcount
                  for table, column in NAMED_BY)
    return changed + c.execute("UPDATE question SET answer = json_set(answer, '$.by', ?) "
                               "WHERE json_extract(answer, '$.by') = ?", (new, old)).rowcount


def rename_user(old: str, new: str) -> bool:
    """The same role and password under a new name, and all that's kept about them (their places, lists, reviews and the
    rest), and what the library's records name them by (NAMED_BY, and their answers to the questions), all in one
    change: an interruption leaves all of it as it was or all of it renamed, and a later user given the old name has
    none of it. Never to a name what was kept of a removed user is set aside under: the records that name them would be
    the renamed user's, and go along with a later rename, and hylly user restore couldn't bring them back. False if
    there's no such user. Their logins end: those are by name."""
    if not NAME.fullmatch(new):
        raise Exit(USAGE, "A user name is 1 to 64 letters, digits and . _ @ - (no spaces).")
    with store().write() as c:
        if c.execute("SELECT 1 FROM account WHERE name = ?", (old,)).fetchone() is None:
            return False
        if c.execute("SELECT 1 FROM account WHERE name = ?", (new,)).fetchone() is not None:
            raise Exit(USAGE, f"There is already a user {new}.")
        _not_kept_aside(c, new, old)
        c.execute("UPDATE account SET name = ? WHERE name = ?", (new, old))
        c.execute("DELETE FROM browser_login WHERE name = ?", (old,))  # renaming back mustn't bring them back
        _set_aside(c, new)  # someone's who had the new name before: not the renamed user's
        c.execute("UPDATE person SET name = ?, rev = rev + 1 WHERE name = ? AND removed IS NULL", (new, old))
        name_in_records(c, old, new)
    return True


def remove_user(name: str) -> bool | str:
    """False if there's no such user; else True, or "set aside" when what was kept about them was set aside too. Their
    logins end at once, in the same change. One the library's records name (_named), with nothing else kept about them,
    has a row of their own kept aside for that, since when they were a user: so their name is no one else's until they
    come back or it's erased (kept_aside), as anyone's is with more kept."""
    with store().write() as c:
        account = c.execute("SELECT since FROM account WHERE name = ?", (name,)).fetchone()
        if account is None:
            return False
        c.execute("DELETE FROM account WHERE name = ?", (name,))
        c.execute("DELETE FROM browser_login WHERE name = ?", (name,))
        if _set_aside(c, name):
            return "set aside"
        if not _named(c, name):
            return True
        c.execute("INSERT INTO person (name, since, removed) VALUES (?, ?, ?)", (name, account["since"], removed_stamp()))
        return "set aside"


def restore_user(name: str, password: str, role: str | None = None) -> str:
    """A removed user back, with what was kept aside of them (the latest, if that name was removed more than once), in
    one change: their places, lists, bookmarks and the rest are theirs again. Only while no user has the name: else
    it would become that user's. Their password and role went with them, so a new password, and the role given, else
    as a new user's. When what came back had been set aside. Exit when the name is a user's, or nothing is kept aside
    of it."""
    if role is not None:
        _role_ok(role)
    hashed = hash_password(password)
    with store().write() as c:
        found = _restorable(c, name)
        _set_aside(c, name)  # someone's of that name with no account, if any: not the one coming back
        c.execute("UPDATE person SET removed = NULL, rev = rev + 1 WHERE id = ?", (found["id"],))
        first = c.execute("SELECT 1 FROM account LIMIT 1").fetchone() is None
        c.execute("INSERT INTO account (name, role, hash, since) VALUES (?, ?, ?, ?)",
                  (name, role or ("owner" if first else "reader"), hashed, db.now()))
    return found["removed"]


def restorable(name: str) -> str:
    """When what restore_user would bring back was set aside, asked before a password is (hylly user restore, and its
    dry run). Exit as restore_user does."""
    with store().read() as c:
        return _restorable(c, name)["removed"]


def _restorable(c, name: str):
    """The row kept aside of name that restore_user brings back: the latest removed, as people.aside lists them first.
    Exit when the name is a user's, or nothing is kept aside of it."""
    taken = c.execute("SELECT 1 FROM account WHERE name = ?", (name,)).fetchone() is not None
    found = next(iter(latest_removed_first(c.execute("SELECT id, removed FROM person WHERE name = ? AND removed IS NOT NULL",
                                                     (name,)).fetchall())), None)
    if found is None:
        raise Exit(USAGE, f"Nothing is kept aside of {name}: hylly people list shows whom something is"
                          + ("" if taken else f", and hylly user add {name} makes a new user") + ". Nothing changed.")
    if taken:
        raise Exit(USAGE, f"{name} is a user now, so what was kept aside of an earlier {name} can't come back: it would "
                          f"be theirs. Give that user another name first (hylly user rename {name} NEW), or erase what's "
                          f"kept aside for good (hylly people erase {name}). Nothing changed.")
    return found


def logins(c, name: str, now: Callable[[], float] = time.time) -> list[dict]:
    """Every browser login and one-time sign-in link of name's the database c keeps, as people.Person.export gives
    them: its kind, when it began and ends, and whether it still lets someone in (works), else why not (ended). An ended
    one is kept until the next login clears it. Never the digest or the password's stamp. A link lasts LINK_MINUTES, and
    a browser's login at least SESSION_DAYS: so each is told from the other."""
    def when(seconds: int) -> str:
        return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")

    found = c.execute("SELECT hash FROM account WHERE name = ?", (name,)).fetchone()
    stamp, at, out = _password_stamp(found["hash"]) if found else None, now(), []
    for row in c.execute("SELECT stamp, since, until FROM browser_login WHERE name = ? ORDER BY since, rowid", (name,)):
        ended = ("its time is up" if row["until"] <= at else "no such user now" if stamp is None else
                 "a new password since" if not hmac.compare_digest(stamp, str(row["stamp"])) else "")
        out.append({"kind": "sign-in link" if row["until"] - row["since"] <= LINK_MINUTES * 60 else "browser",
                    "since": when(row["since"]), "until": when(row["until"]), "works": not ended, **({"ended": ended} if ended else {})})
    return out


# ---- browser sessions ------------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _password_stamp(stored: str) -> str:
    """Changes with the password (the stored hash has a new salt every time): older sessions end with it."""
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:24]


def _link_digest(token: str) -> str:
    """A sign-in link's, kept in browser_login beside the browsers' sessions, but made apart from theirs: "link:" goes
    in first, which no cookie's token has (cookie_token), so a link can't be sent as a cookie, nor a cookie as a link."""
    return hashlib.sha256(f"link:{token}".encode("ascii")).hexdigest()


def sign_in_link(name: str, now: Callable[[], float] = time.time) -> str:
    """A one-time sign-in link's token for the user: /login?once=TOKEN logs a browser in as them (Sessions.use_link),
    once, within LINK_MINUTES, and only while their password is the one it was made with. Only its digest is kept
    (_link_digest), and it goes when the user does. Exit when there's no such user."""
    token, at = secrets.token_urlsafe(32), now()
    with store().write() as c:
        found = c.execute("SELECT hash FROM account WHERE name = ?", (name,)).fetchone()
        if found is None:
            raise Exit(USAGE, f"There is no user {name}. hylly user list shows who there is.")
        c.execute("DELETE FROM browser_login WHERE until <= ?", (at,))
        c.execute("INSERT INTO browser_login (digest, name, stamp, since, until) VALUES (?, ?, ?, ?, ?)",
                  (_link_digest(token), name, _password_stamp(found["hash"]), int(at), int(at + LINK_MINUTES * 60)))
    return token


def cookie_token(header: str | None) -> str | None:
    """The session token in a Cookie header, the HTTPS-only name first; anything not shaped like one is ignored."""
    for name in (COOKIE_HTTPS, COOKIE):
        found = re.search(rf"(?:^|;)\s*{re.escape(name)}=([A-Za-z0-9_-]{{40,64}})\s*(?:;|$)", header or "")
        if found:
            return found[1]
    return None


class Sessions:
    """Browser logins, in the database (browser_login): token digest -> user, the password's stamp, and when it ends,
    so a restarted server still knows them."""

    def __init__(self, now=time.time) -> None:
        self.now = now

    def start(self, user: str, stored: str, again: str | None = None) -> str:
        """A new session's token. again: the user's password hash made again at today's cost (again_if_below), taken in
        the same change (made_again)."""
        with store().write() as c:
            if again:
                stored = made_again(c, user, stored, again)
            return self._start(c, user, stored)

    def _start(self, c, user: str, stored: str) -> str:
        """A new session's token, in the change c is making."""
        token = secrets.token_urlsafe(32)
        now = self.now()
        c.execute("DELETE FROM browser_login WHERE until <= ?", (now,))
        c.execute("INSERT INTO browser_login (digest, name, stamp, since, until) VALUES (?, ?, ?, ?, ?)",
                  (_digest(token), user, _password_stamp(stored), int(now), int(now + SESSION_DAYS * 86400)))
        c.execute("DELETE FROM browser_login WHERE name = ? AND digest NOT IN (SELECT digest FROM browser_login WHERE name = ? "
                  "ORDER BY since DESC, rowid DESC LIMIT ?)", (user, user, SESSIONS_EACH))
        c.execute("DELETE FROM browser_login WHERE digest NOT IN (SELECT digest FROM browser_login ORDER BY since DESC, rowid DESC LIMIT ?)",
                  (SESSIONS_KEPT,))
        return token

    def _link(self, c, token: str | None):
        """A sign-in link's row and its user's password hash, while the link lasts and that password is the one it was
        made with; None otherwise."""
        if not token or not LINK_TOKEN.fullmatch(token):
            return None
        found = c.execute("SELECT b.name, b.stamp, b.until, a.hash FROM browser_login b JOIN account a ON a.name = b.name "
                          "WHERE b.digest = ?", (_link_digest(token),)).fetchone()
        if not found or found["until"] <= self.now() or not hmac.compare_digest(_password_stamp(found["hash"]), str(found["stamp"])):
            return None
        return found

    def link_user(self, token: str | None) -> str | None:
        """Whose this sign-in link is, if it still works: only looked at, so the page that asks to use it can say whose."""
        try:
            with store().read() as c:
                found = self._link(c, token)
        except (db.Newer, db.Unusable):
            return None
        return found["name"] if found else None

    def use_link(self, token: str | None) -> tuple[str, str] | None:
        """A sign-in link used: (its user, a new session's token), and the link gone, in the same change, so a second use
        finds nothing; None when it doesn't work (used, too old, a new password since, the user gone, or never a link).
        Looked for first by reading only: anyone may send a value shaped like one, and only a link that is there takes
        the database's write lock."""
        if not token or not LINK_TOKEN.fullmatch(token):
            return None
        try:
            with store().read() as c:
                if c.execute("SELECT 1 FROM browser_login WHERE digest = ?", (_link_digest(token),)).fetchone() is None:
                    return None
        except (db.Newer, db.Unusable):
            return None
        with store().write() as c:
            found = self._link(c, token)
            c.execute("DELETE FROM browser_login WHERE digest = ?", (_link_digest(token),))  # used, whatever came of it
            return (found["name"], self._start(c, found["name"], found["hash"])) if found else None

    def _found(self, c, token: str | None):
        return c.execute("SELECT name, stamp, since, until FROM browser_login WHERE digest = ?", (_digest(token),)).fetchone() if token else None

    def user(self, token: str | None, users: dict[str, str]) -> str | None:
        """Whose session this is, while it lasts and the password is the one it began with."""
        try:
            with store().read() as c:
                found = self._found(c, token)
        except (db.Newer, db.Unusable):
            return None
        if not found or found["until"] <= self.now():
            return None
        stored = users.get(found["name"])
        return found["name"] if stored and hmac.compare_digest(_password_stamp(stored), str(found["stamp"])) else None

    def renew(self, token: str | None) -> int | None:
        """Using the library keeps a login going: once a day at most, its end moves on to SESSION_DAYS from now,
        but never past SESSION_LIMIT_DAYS after the login. The seconds it now has left when it moved (for the
        browser's cookie), else None. Only for a session user() accepts: this doesn't check the password. Read first:
        only a login whose end really moves takes the database's write lock (and is looked at again under it, as it
        stands then), so that the requests of a day it moved already never wait on another program's write, such as a
        catalog build's."""
        if not token:
            return None
        with store().read() as c:
            if self._moved(self._found(c, token)) is None:
                return None
        with store().write() as c:
            moved = self._moved(self._found(c, token))
            if moved is None:  # ended, or moved by another request, meanwhile
                return None
            until, now = moved
            c.execute("UPDATE browser_login SET until = ? WHERE digest = ?", (until, _digest(token)))
            return int(until - now)

    def _moved(self, found) -> tuple[int, float] | None:
        """Where a login's end moves to now that it's used (renew), and now; None while it doesn't move: it has ended,
        or it moved less than a day ago."""
        now = self.now()
        if not found or found["until"] <= now:
            return None
        until = int(min(now + SESSION_DAYS * 86400, found["since"] + SESSION_LIMIT_DAYS * 86400))
        return (until, now) if until - found["until"] >= 86400 else None

    def end(self, token: str | None) -> None:
        if token:
            with store().write() as c:
                c.execute("DELETE FROM browser_login WHERE digest = ?", (_digest(token),))


# ---- the login check -------------------------------------------------------------------------

def _address_in(text: str) -> str | None:
    """The address one entry of a forwarding header names, in ipaddress's own words, or None for anything else: what is
    written there came over the network, so none of it is ever kept or logged as received. The forms proxies write
    (RFC 7239 among them): 192.0.2.7, 192.0.2.7:4711, 2001:db8::7, [2001:db8::7] and [2001:db8::7]:4711, in quotes
    or not. An address with a scope id (fe80::1%eth0) is none: the part after % may hold any text at all."""
    text = text.strip().strip('"').strip()
    if text.startswith("["):
        text = text[1:].partition("]")[0]
    elif text.count(":") == 1:
        text = text.partition(":")[0]
    if "%" in text:
        return None
    try:
        return unmapped(str(ipaddress.ip_address(text)))
    except ValueError:
        return None


def _entries(headers, name: str) -> list[str]:
    """Every line of a header, as one list of its entries: a proxy may add a line of its own instead of adding to the
    one there, so the sender's own line can come first."""
    return [entry for line in _lines(headers, name) for entry in _split(line, ",") if entry.strip()]


def _split(text: str, mark: str) -> list[str]:
    """text cut at each mark outside a quoted string (RFC 7230 and 7239: "a,b" is one value, and \\" inside it
    is a quote), so a quoted value a proxy wrote can never start an entry or a for= of its own."""
    parts, part, quoted, escaped = [], [], False, False
    for char in text:
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == mark and not quoted:
            parts.append("".join(part))
            part = []
            continue
        part.append(char)
    parts.append("".join(part))
    return parts


def _lines(headers, name: str) -> list[str]:
    if hasattr(headers, "get_all"):  # a request's own headers (HTTPMessage)
        return [str(line) for line in headers.get_all(name) or []]
    return [str(headers[name])] if headers.get(name) else []  # a test's plain dict


def _forwarded_for(element: str) -> str:
    """What for= says in one element of a Forwarded header (for=192.0.2.7;proto=https), or "" when it says nothing."""
    for pair in _split(element, ";"):
        key, _, value = pair.partition("=")
        if key.strip().lower() == "for":
            return value
    return ""


def proxy_header() -> str:
    """[server] proxy_header: the one header a trusted proxy (trusted_proxies) puts the sender's address in. Tailscale
    Serve and Funnel and most proxies add it at the end of X-Forwarded-For, the default; cloudflared sets
    CF-Connecting-IP. Written in any case, it comes as PROXY_HEADERS has it (settings.py)."""
    return settings_section("server")["proxy_header"]


def trusted_proxies() -> tuple:
    """[server] trusted_proxies, as networks: the proxies and tunnels whose forwarding headers count (client_address,
    serve.Handler._trusted). As they come, this computer's own: Tailscale Serve and Funnel, cloudflared or a proxy
    running here."""
    return tuple(ipaddress.ip_network(n, strict=False) for n in settings_section("server")["trusted_proxies"])


def unmapped(peer: str) -> str:
    """An IPv4 sender as itself: a server listening on "::" sees 192.0.2.7 as ::ffff:192.0.2.7. Anything else as it is."""
    try:
        found = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    mapped = getattr(found, "ipv4_mapped", None)
    return str(mapped) if mapped else peer


def is_trusted(peer: str, proxies=LOOPBACK) -> bool:
    """Whether peer is one of the trusted proxies (trusted_proxies), whose forwarding headers count."""
    try:
        found = ipaddress.ip_address(unmapped(peer))
    except ValueError:
        return False
    return any(found in network for network in proxies)


def client_address(peer: str, headers, trust: str = "X-Forwarded-For", proxies=LOOPBACK) -> str:
    """Who sent the request, for the lockout, the access log and the devices list. Through a proxy or tunnel, every
    request comes from the proxy's address, so a header names the sender: believed only when the request comes from one
    of the trusted proxies ([server] trusted_proxies, this computer's own as they come), and only the one header they
    set (trust, from [server] proxy_header). Of X-Forwarded-For and Forwarded, all their lines as one list, the
    nearest entry that isn't a trusted proxy itself: those to the right were added by the proxies along the way, those
    further left are whatever the sender typed. An entry that names no address (unknown, _hidden, anything else) ends
    the walk at the nearest one known, the last trusted proxy's or the peer's: past it, nothing says who added what.
    A header of one address (CF-Connecting-IP, X-Real-IP) counts only as one line. With the rest believed, a stranger
    could pose as a new address at every try and never be locked out; from anyone else, a device at home among them,
    none is believed. The sender is always in ipaddress's own words (_address_in), never as a header wrote it: it goes
    into the access log, the server's log and the alarms."""
    peer = unmapped(peer)
    if not is_trusted(peer, proxies):
        return peer
    if trust in ("X-Forwarded-For", "Forwarded"):
        entries = _entries(headers, trust)
        if trust == "Forwarded":
            entries = [_forwarded_for(element) for element in entries]
    else:  # set by the proxy: two lines of it can't say which one the proxy wrote
        entries = _lines(headers, trust)
        if len(entries) > 1:
            return peer
    nearest = peer
    for entry in reversed(entries):  # from the nearest proxy back
        found = _address_in(entry)
        if found is None:
            return nearest
        if not is_trusted(found, proxies):
            return found
        nearest = found
    return nearest


def inside(sender: str) -> bool:
    """Whether a sender is inside, where a stranger on the internet can't send from: this computer, the home network,
    the tailnet (INSIDE). By the sender as client_address works it out, never as a header says: so through Tailscale
    Serve a device of the tailnet is inside, by its own 100.x or fd7a: address; through Funnel or cloudflared a
    visitor is the public address the tunnel names, outside; and so is any public address a request from this computer
    names through a trusted proxy. A home device that reaches the server over the home's public IPv6 (with listen =
    "::") is outside."""
    try:
        found = ipaddress.ip_address(unmapped(sender.partition("%")[0]))
    except ValueError:
        return False
    return any(found in network for network in INSIDE)


def sender_key(ip: str) -> str:
    """Whom wrong logins count against, for the lockout: an IPv4 sender by its address, an IPv6 one from outside by its
    /64, which one home or one machine is given whole, so a stranger can't take a fresh address of it at every try. One
    inside (inside: this computer, the home network, the tailnet) by its own address: each device there has one, and a
    stranger can't pick it."""
    try:
        found = ipaddress.ip_address(unmapped(ip.partition("%")[0]))
    except ValueError:
        return ip
    if found.version == 4 or inside(str(found)):
        return str(found)
    return str(ipaddress.ip_network(f"{found}/64", strict=False))


class Gate:
    """The login check of one server: the users in the database (read again at every ask), logins already verified,
    the addresses that are refused for a while after too many wrong passwords, and the slowdown (_turn) past too many
    for one name or from everyone together. clock and sleep: the time and the wait, which a test makes up."""

    def __init__(self, log=print, sessions: Sessions | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._log = log
        self.sessions = sessions or Sessions()
        self._clock, self._sleep = clock, sleep
        self._secret = secrets.token_bytes(16)
        self._users: dict[str, str] = {}
        self._roles: dict[str, str] = {}
        self._unusable = False  # the database couldn't be used at the last ask: said once, until it can
        self._verified: dict[str, tuple[str, str]] = {}  # a name and password checked -> (the name, its password's stamp)
        self._fails: dict[str, list[float]] = {}  # by sender_key, the wrong logins within WINDOW; none kept empty
        # Every wrong login within WINDOW, as (when, its name's key), and how many each name has: a name only by a digest
        # of it, as a password typed in the name field mustn't be kept. None: no name. At one turn in SLOW_TURN seconds
        # past SLOW_ALL, a window holds a few hundred at most.
        self._wrong: deque[tuple[float, bytes | None]] = deque()
        self._per_name: Counter[bytes] = Counter()
        self._turns: dict[bytes | None, float] = {}  # the next free turn, for one name, or everyone together (None)
        self._slowed: dict[bytes | None, float] = {}  # when the server's log last said a slowdown began, for each
        self._swept = float("-inf")  # when _forget last looked over every address
        self._lock = threading.Lock()
        self.on_event = lambda kind, **data: None  # logins and wrong passwords, for the alarms (bell.Watch.event)

    def users(self) -> dict[str, str]:
        """Each user's password hash, by name (accounts)."""
        return self.accounts()[0]

    def accounts(self) -> tuple[dict[str, str], dict[str, str]]:
        """The users, each one's password hash and role, by name: read again at every ask, a handful of rows, so that a
        change made by a command counts at once, and given as one, so that the two always go together. The server
        reads them once a request (serve.Caller), and asks with them what it asks after (session_user, check, role).
        Neither is ever changed once given: a change makes new ones. A database of a newer version of Hylly has nobody
        in it that this one may let in."""
        with self._lock:
            try:
                accounts = load_accounts()
            except (db.Newer, db.Unusable) as e:
                if not self._unusable:
                    self._log(f"Nobody can log in: {e}")
                accounts, self._unusable = {}, True
            else:
                self._unusable = False
            if accounts != {n: (self._roles.get(n), h) for n, h in self._users.items()}:
                self._users = {name: stored for name, (_, stored) in accounts.items()}
                self._roles = {name: role for name, (role, _) in accounts.items()}
                self._verified.clear()  # a changed or removed password ends the remembered logins
            return self._users, self._roles

    @property
    def unusable(self) -> bool:
        """The database couldn't be used at the last ask (users): nobody can log in until it can."""
        return self._unusable

    def role(self, user: str | None, roles: dict[str, str] | None = None) -> str:
        """A user's role as the database says now, or as roles has them (accounts, as a request read them): a role
        lowered takes effect at the next request. A name that isn't a user (any more), or none, gets the least of
        them."""
        roles = self.accounts()[1] if roles is None else roles
        return roles.get(user, ROLES[0]) if user else ROLES[0]

    def check(self, client_ip: str, headers, session: str | None | EllipsisType = ...,
              users: dict[str, str] | None = None) -> tuple[str, str | None]:
        """("ok", the user); ("login", None) when there's no login, or ("login", the name tried, as the access log may keep
        it: tried) when it was wrong; ("locked", None); or ("slow", None) when its turn is too far off (_turn). Every
        request needs one, wherever it comes from: a browser's session, or a reading app's Basic login. session: the
        browser login's user as the request asked already (session_user), so it's asked once; users: the users as it
        read them (accounts). Either is read here when not given."""
        user = self.session_user(headers, users) if session is ... else session
        if user:
            return "ok", user
        if self._locked(client_ip):
            return "locked", None
        auth = headers.get("Authorization") or ""
        if not auth.lower().startswith("basic "):
            return "login", None  # the first request: the app is asked for the password
        try:
            name, _, password = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return self._fail(client_ip, None)
        users = self.users() if users is None else users
        token = hmac.new(self._secret, f"{name}\0{password}".encode("utf-8"), "sha256").hexdigest()  # however it's spelled
        stored = users.get(name)
        if stored and self._verified.get(token) == (name, _password_stamp(stored)):  # the same user, with the same password
            return "ok", name
        if not self._turn(name, client_ip):  # a password is checked only at its turn, while there's a slowdown
            return "slow", None
        if _checked(password, stored):  # unknown names, and old hashes, take about as long as known ones (_checked)
            again = again_if_below(password, stored)
            if again:  # the reading app's login has no change of its own to make: this one alone
                try:
                    with store().write() as c:
                        stored = made_again(c, name, stored, again)
                except (sqlite3.Error, OSError, db.Unusable) as e:  # the login holds: the next one tries again
                    self._log(f"{name}'s password couldn't be hashed again at today's cost ({e}); at the next login, then.")
                self.users()  # read again now, so the login remembered below isn't forgotten at the next request
            with self._lock:
                if len(self._verified) >= VERIFIED_KEPT:
                    self._verified.clear()
                self._verified[token] = (name, _password_stamp(stored))  # checked again at each use: a change meanwhile counts
            self.on_event("login", user=name, ip=client_ip, agent=headers.get("User-Agent") or "", via="app login")
            return "ok", name
        return self._fail(client_ip, name)

    def session_user(self, headers, users: dict[str, str] | None = None) -> str | None:
        """The user of the browser login the request's cookie names, while it lasts (Sessions.user), by the users as
        given (accounts) or read now; None, reading nothing, when it names none (a reading app's Basic login)."""
        token = cookie_token(headers.get("Cookie"))
        if not token:
            return None
        return self.sessions.user(token, self.users() if users is None else users)

    def log_in(self, client_ip: str, name: str, password: str, agent: str = "") -> tuple[str, str | None]:
        """The login page's form: ("ok", session token), ("wrong", None), ("locked", None) or ("slow", None). Wrong
        ones count towards the same lockout and slowdown as wrong Basic logins, and unknown names, and users whose hash is
        still below today's cost, take about as long as known ones at today's cost (_checked)."""
        if self._locked(client_ip):
            return "locked", None
        stored = self.users().get(name)
        if not self._turn(name, client_ip):
            return "slow", None
        if _checked(password, stored):
            self.on_event("login", user=name, ip=client_ip, agent=agent, via="login page")
            return "ok", self.sessions.start(name, stored, again=again_if_below(password, stored))
        self._fail(client_ip, name)
        return ("locked" if self._locked(client_ip) else "wrong"), None

    def use_link(self, client_ip: str, token: str, agent: str = "") -> tuple[str, tuple[str, str] | None]:
        """A one-time sign-in link, sent from the page that asks to use it: ("ok", (its user, a session's token)),
        ("gone", None) when it doesn't work, or ("locked", None) for an address locked out after wrong passwords. A
        link is far too long to guess, so one that doesn't work counts towards no lockout: it was used, or is too old."""
        if self._locked(client_ip):
            return "locked", None
        found = self.sessions.use_link(token)
        if not found:
            return "gone", None
        self.on_event("login", user=found[0], ip=client_ip, agent=agent, via="sign-in link")
        return "ok", found

    def tried(self, name: str | None) -> str | None:
        """A name tried at a login, as the access log keeps it: a user's as it is; any other only by its length
        (unknown_name), in brackets, which no user's name has; None for no name, and ? for a login that couldn't be
        read. By the users as the last check read them."""
        if name is None:
            return "?"
        if not name:
            return None
        return name if name in self._users else f"({unknown_name(name)})"

    def _locked(self, ip: str) -> bool:
        key = sender_key(ip)
        with self._lock:
            now = self._clock()
            recent = [t for t in self._fails.get(key, []) if now - t < WINDOW]
            if recent:
                self._fails[key] = recent
            else:  # an address asked to log in is no reason to keep a row for it
                self._fails.pop(key, None)
            return len(recent) >= FAILS

    def _name_key(self, name: str | None) -> bytes | None:
        """A name tried, as the slowdown counts it: by a digest only this server can make. None for no name."""
        return hmac.new(self._secret, name.encode("utf-8", "replace"), "sha256").digest()[:16] if name else None

    def _counts(self, key: bytes | None) -> list[tuple[bytes | None, int, int]]:
        """(what, how many wrong logins within WINDOW, the most before a slowdown): everyone together, and the name."""
        return [(None, len(self._wrong), SLOW_ALL), *([(key, self._per_name[key], SLOW_NAME)] if key is not None else [])]

    def _forget(self, now: float) -> None:
        """What is older than WINDOW goes, under the lock: wrong logins, at once; addresses whose last one is old, the
        slowdowns said and the turns that have come, which hold nobody back any more, at most once in SWEEP seconds, as
        that looks over every one of them while every request's check waits for the lock."""
        while self._wrong and now - self._wrong[0][0] >= WINDOW:
            _, key = self._wrong.popleft()
            if key is not None:
                self._per_name[key] -= 1
                if self._per_name[key] <= 0:
                    del self._per_name[key]
        if now - self._swept < SWEEP:
            return
        self._swept = now
        for key in [k for k, times in self._fails.items() if now - times[-1] >= WINDOW]:
            del self._fails[key]
        for key in [k for k, at in self._slowed.items() if now - at >= WINDOW]:
            del self._slowed[key]
        for key in [k for k, at in self._turns.items() if at <= now]:
            del self._turns[key]

    def _turn(self, name: str | None, client_ip: str) -> bool:
        """Whether a password may be checked now. Past SLOW_NAME wrong logins for this name within WINDOW, from
        anywhere, or past SLOW_ALL from all addresses together, a check from outside takes a turn, SLOW_TURN seconds
        after the one before, and waits for it here, in its own request's thread, outside the lock. A turn more than
        SLOW_WAIT away is no turn: False, and the try is answered at once, to come again in a moment. A turn goes to
        whoever asks first, so while a stranger guesses from enough addresses, a fresh login from outside may be told
        to try again; a sender inside (inside) never waits, so at home and on the tailnet nobody can be kept out so.
        You mistyping a few times never meet it. Any name counts, a user's or not, so whether a name slows down says
        nothing about whether it is a user's."""
        if inside(client_ip):
            return True
        key = self._name_key(name)
        with self._lock:
            now = self._clock()
            self._forget(now)
            paced = [k for k, many, most in self._counts(key) if many >= most]
            if not paced:
                return True
            at = max(now, *(self._turns.get(k, now) for k in paced))
            if at - now > SLOW_WAIT:
                return False
            for k in paced:
                self._turns[k] = at + SLOW_TURN
        if at > now:
            self._sleep(at - now)
        return True

    def _fail(self, ip: str, name: str | None) -> tuple[str, str | None]:
        """A wrong login, towards the lockout and the slowdown. name: the name tried, "" for none, None for a login that
        couldn't be read. The server's log and the alarms get it only when it's a user's, and the access log too
        (tried, which is returned for it): other names only by their length."""
        key, sender = self._name_key(name), sender_key(ip)
        with self._lock:
            now = self._clock()
            self._forget(now)
            self._fails.setdefault(sender, []).append(now)
            locked = len(self._fails[sender]) == FAILS
            self._wrong.append((now, key))
            if key is not None:
                self._per_name[key] += 1
            slowed = [k for k, many, most in self._counts(key) if many == most and k not in self._slowed]
            for k in slowed:
                self._slowed[k] = now
            known = bool(name) and name in self._users
        said = name if known else unknown_name(name)
        last = typed(name) if known else said
        if locked:
            self._log(f"{FAILS} wrong logins from {shown(ip)} (last tried as {last}): refused for {WINDOW // 60} minutes.")
        for k in slowed:  # said once in WINDOW for each
            whose = f"for {last}" if k is not None else "from all addresses together"
            self._log(f"{SLOW_NAME if k is not None else SLOW_ALL} wrong logins {whose} within {WINDOW // 60} minutes: "
                      f"each try now waits its turn, {SLOW_TURN:g} seconds apart, until there are fewer.")
        self.on_event("wrong", name=said, ip=ip, known=known)
        if locked:
            self.on_event("locked", ip=ip, name=said, known=known)
        return "login", self.tried(name)


def typed(name: str) -> str:
    """A name someone typed at a login, as the server's log and an alarm quote it: cleaned (storage.shown), at most 40
    characters, in quotes it can't close."""
    return repr(shown(name)[:40])


def unknown_name(name: str | None) -> str:
    """What the logs say of a name tried at a login that isn't a user's: only how long it was. People do type their
    password in the name field, and a password must never be written down: not in the access log, the server's log
    or the alarms. None: a login that couldn't be read."""
    if name is None:
        return "a login that couldn't be read"
    if not name:
        return "an empty name"
    return f"an unknown name of {many(len(name), 'character')}"


# ---- the library's addresses -----------------------------------------------------------------

# Adapters whose addresses look private but aren't the home network's: VPNs and tunnels, and the virtual networks of
# WSL, Hyper-V, containers and virtual machines, by the names they come with. By the name only: a VPN's or a virtual
# machine's adapter with a plain one (Windows numbers many "Ethernet N") is kept, and only the default route
# (_route_probe) may pass it over. Which address is shown, nothing else; address under [server] says it outright.
VIRTUAL = re.compile(r"vethernet|wsl|hyper-v|docker|podman|virtualbox|vbox|vmware|vmnet|tailscale|zerotier|wireguard|"
                     r"openvpn|vpn|loopback|bluetooth|^tap|^tun|^utun|^wg|^zt|^veth|^br-|^virbr|^vnet\d|^lxcbr|^lxdbr|"
                     r"^incusbr|^cni|^flannel|^cali|^cilium|^vxlan", re.I)
_adapter: dict = {"address": None}  # this computer's address at home, as last looked up (lan_address)


def _route_probe() -> str | None:
    """The address this computer would send from towards an address no home network has (TEST-NET-1, 192.0.2.1): that
    of the adapter with the default route. A UDP socket only picks it; nothing is sent. None without a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def adapter_address() -> str:
    """This computer's address on the home network: the private IPv4 address of an adapter that is up and isn't a
    VPN's or a virtual network's by its name (VIRTUAL). Of several, the one the default route goes by (_route_probe),
    which is the home network's unless a VPN has it; else the first. Only if no adapter can be listed, the default route's own
    address, whatever it is; 127.0.0.1 without one."""
    found = []
    try:
        import psutil

        up = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in up and up[name].isup and not VIRTUAL.search(name):
                for a in addrs:
                    if a.family == socket.AF_INET:
                        ip = ipaddress.ip_address(a.address)
                        if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                            found.append(a.address)
    except Exception:  # noqa: BLE001 - no list of adapters: the route alone
        found = []
    routed = _route_probe()
    if found:
        return routed if routed in found else found[0]
    return routed or "127.0.0.1"


def lan_address() -> str:
    """adapter_address, kept, so that no request waits on the adapters: looked up the first time it's asked, which
    the server's start does (run), and again every five minutes by the server's job "home address" (periodic.py), in a
    thread of its own, while the address from before is answered."""
    if _adapter["address"] is None:
        look_at_adapters()
    return _adapter["address"]


def look_at_adapters() -> None:
    """The adapters looked at again: the server's job "home address"."""
    _adapter["address"] = adapter_address()


def _bracketed(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def here(listen: str, port: int) -> str:
    """The library's address on this computer, for its own browser and the owner's link: 127.0.0.1 when the server
    listens there or on every IPv4 address, [::1] on every address ("::"), else the one address it listens on."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(listen, listen)
    return f"http://{_bracketed(host)}:{port}"


def other_devices(listen: str, port: int, address: str = "") -> str | None:
    """The address other devices use, without the / at its end: [server] address when set, the one people use; else,
    while the server listens beyond this computer, this computer's own on the home network (lan_address), or the one
    address it listens on. None while it listens on this computer only, or no network is there at all."""
    if address:
        return address
    ip = ipaddress.ip_address(listen)
    if ip.is_loopback:
        return None
    host = lan_address() if ip.is_unspecified else listen
    return None if ipaddress.ip_address(host).is_loopback else f"http://{_bracketed(host)}:{port}"


def home_too(listen: str, port: int, address: str = "") -> str | None:
    """The plain address at home as well, for a device that can't use [server] address (an e-reader without Tailscale,
    say): what other_devices would give without address, while address is set and the server listens beyond this
    computer. None otherwise, or when it is address itself."""
    home = other_devices(listen, port) if address else None
    return home if home and home != address else None


# ---- the access log --------------------------------------------------------------------------

def _quoted(text: str) -> str:
    """A field of an access-log line, inside its quotes: those and backslashes escaped, as log tools read them, and
    cleaned (storage.shown), so it can't end the field or the line, however the line is written."""
    return shown(text.replace("\\", "\\\\").replace('"', '\\"'))


def _field(text: str) -> str:
    """A field of an access-log line without quotes (the address, the user): _quoted, and no spaces, which would end it."""
    return re.sub(r"\s", "_", _quoted(text)) or "-"


def _cut(text: str, most: int) -> str:
    """What a sender chose, in at most most characters: its start and its end stay, and a mark (…) where the middle went."""
    if len(text) <= most:
        return text
    end = most // 3
    return f"{text[:most - end - 1]}…{text[-end:]}"


ONCE = re.compile(r"([?&]once=)[^&#\s]*")  # a sign-in link's token in an address, as the server takes it (serve.Handler)


def masked(text: str) -> str:
    """An address, a request line or a Referer, without a sign-in link's token: never written down, used or not."""
    return ONCE.sub(r"\1-", text)


LOGGED_ON = re.compile(rb"\S+ - \S+ \[(\d\d)/([A-Z][a-z]{2})/(\d{4}):")  # a line's day, as AccessLog.write puts it
MONTHS = {m.encode(): n for n, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _logged_on(line: bytes) -> date | None:
    """The day an access-log line was written; None if that can't be read."""
    found = LOGGED_ON.match(line)
    try:
        return date(int(found[3]), MONTHS[found[2]], int(found[1])) if found else None
    except (KeyError, ValueError):
        return None


def _drop_old(path: Path, since: date, until: date) -> None:
    """The lines of one log file dated before the day since go; the file goes if that is all of it. Each line is judged
    by its own day, so a line dated ahead (a clock set ahead wrote it, or this computer's clock is behind) stays and
    holds nothing else back; one whose day can't be read goes, or stays, with the line before it. A file whose first
    line is from since to until is left as it is: the lines are written in time order, so it has nothing to go yet,
    and anything old further on goes within LOG_DAYS, once that first line is old."""
    def fresh(day: date | None) -> bool:
        return day is not None and since <= day <= until

    try:
        with path.open("rb") as fh:
            first = fh.readline()
            if not first or fresh(_logged_on(first)):
                return
            lines = [first, *fh]
    except FileNotFoundError:
        return
    kept, keep = [], False
    for line in lines:
        day = _logged_on(line)
        keep = keep if day is None else day >= since  # a day ahead stays: a clock behind mustn't cost real lines
        if keep:
            kept.append(line)
    if not kept:
        path.unlink()
    elif len(kept) < len(lines):
        atomic_write(path, b"".join(kept), attempts=2)  # a request waits for it: not long


class AccessLog:
    """access.log, a line per request. Beyond LOG_LIMIT it moves to access.log.1 (and the one before that goes), and in
    both only the lines of the last LOG_DAYS days are kept: the older ones go at
    the server's start and at every rotation, by size or at the first request of a new day, as a server may run for
    months without filling 5 MB. With the log turned off too, so that what an earlier server wrote goes in time."""

    AGAIN = 3600  # seconds before the old lines are tried again, when a file couldn't be changed

    def __init__(self, enabled: bool = False, today: Callable[[], date] = date.today, log: Callable[[str], None] = print) -> None:
        self.enabled = enabled
        self._today = today
        self._log = log  # the server's log, for a line that couldn't be written
        self._lock = threading.Lock()
        self._done: date | None = None  # the day the old lines last went from both files
        self._again = 0.0  # after a file that couldn't be changed, not tried before this (time.monotonic())
        self._unwritten = False  # a line couldn't be written: said once in the server's log, until one can
        self._old_lines_go()  # the server's start: nobody else has this yet

    def _old_lines_go(self) -> None:
        """The lines older than LOG_DAYS days go from both files, once a day; under the lock. Lines dated more than a
        day ahead go too. A file that can't be changed now, as one held open by another program on Windows, is tried
        again in an hour, not at every request, as each try makes that request wait: the old lines never fail one."""
        today = self._today()
        if today == self._done or time.monotonic() < self._again:
            return
        path, failed = access_log_file(), False
        for each in (aside(path), path):
            try:
                _drop_old(each, today - timedelta(days=LOG_DAYS), today + timedelta(days=1))
            except OSError:
                failed = True
        if failed:
            self._again = time.monotonic() + self.AGAIN
        else:
            self._done = today

    def write(self, ip: str, user: str | None, request_line: str, status: int, size: str | None,
              referer: str | None, agent: str | None) -> None:
        if self._done != self._today():  # the first request of a new day
            with self._lock:
                self._old_lines_go()
        if not self.enabled:
            return
        when = datetime.now().astimezone().strftime("%d/%b/%Y:%H:%M:%S %z")
        who = _field(user) if user else "-"  # what's said of a name that isn't a user's has spaces
        # Each field as the sender wrote it, a sign-in link's token masked first, then cut, and only then escaped: a cut
        # can't leave half an escape to end the quotes
        request, referer, agent = (_quoted(_cut(text, FIELD_MOST[field])) for field, text in
                                   (("request", masked(request_line)), ("referer", masked(referer or "-")), ("agent", agent or "-")))
        line = f'{_field(ip)} - {who} [{when}] "{request}" {status} {_field(str(size or "-"))} "{referer}" "{agent}"'
        with self._lock:
            try:
                if append_log(access_log_file(), line, LOG_LIMIT):
                    self._done = None  # a rotation: the older file keeps only the last LOG_DAYS days too
                    self._old_lines_go()
            except OSError as e:  # a full disk, or the file held by another program on Windows: the request was answered
                if not self._unwritten:  # said once, until a line is written again
                    self._log(f"{access_log_file()} can't be written ({e.strerror or type(e).__name__}): requests aren't "
                              "logged until it can be. The alarms still see them.")
                self._unwritten = True
            else:
                self._unwritten = False


def tail(path: Path, lines: int) -> list[str]:
    """The last lines of a log, reading only its end, for the server page, hylly logs and the alarms' look at bell.log.
    A read begun in the middle of a line drops what it got of that one, only its end; one begun at a line's start
    keeps it. Lines end at a line break only: U+0085 and the others that Python's splitlines ends a line at are what a
    sender put inside one, and each line is shown as storage.shown makes it."""
    try:
        with path.open("rb") as fh:
            start = max(0, fh.seek(0, 2) - 200 * lines - 4096)
            fh.seek(max(0, start - 1))
            data = fh.read()
    except OSError:
        return []
    if start:  # the letter before the read too: a line break there, and the read begins at a line's start
        data = data.partition(b"\n")[2]
    text = data.decode("utf-8", errors="replace").rstrip()
    return [shown(line.rstrip("\r")) for line in text.split("\n")[-lines:]] if text else []
