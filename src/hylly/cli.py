"""Command line: what each command does (cmd_*), and main, which runs one. The commands, their options and their words
are in commands.py; hylly --help lists them all. hyllyd, the library server itself, is daemon.py's.

Every shop has the same shape, hylly <shop> <action>, download among its actions, each shop and its actions named once
(shops.py). hylly download runs every shop you have a login for. Every command that changes something does it, and with
--dry-run only says what it would do."""

from __future__ import annotations

import contextlib
import errno
import getpass
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from . import paths, runs, shops, snapshot
from .commands import LOGINS, START, build_parser, commands_data  # the words: commands.py
from .exits import BUG, BUSY, INTERRUPTED, ISSUES, LOCAL, LOGIN, NEEDS_UPDATE, NEEDS_YOU, NOT_SET_UP, PARTLY, TABLE, USAGE, Exit
from .exits import bug, hide_addresses_in_tracebacks  # hylly's and hyllyd's alike
from .logins import SITES, AuthError, ShopError, load_session, read_clipboard, save_session, session_path, usable
from .sites import COOKIE_PLACES, ENDED_THERE, KEYS, LOOKUPS
from .storage import RUN_LOG_LIMIT, append_log, atomic_write, share_is_gone
from .words import MB, many, size_words, time_words


LONG_JOB = 1800  # seconds: a job that took this long says when it ends, as you may have stopped waiting for it
ON_THE_SPOT = 60  # seconds: one that fails this soon with someone at the keyboard only says it there


@dataclass
class Ended:
    """How a long job's run ended, beside its exit code, for its watch (_watched): its phone note, and what hylly jobs
    records. The job's handler returns it, and an Exit that ends the run carries it (carried), so that a run that ends
    partly done says what it did too. A handler that says nothing of its run (hylly lookup's) returns None: an Ended
    with nothing in it."""

    text: str = ""  # what it did, in counts ("120 files done, 3 failed."): after how long it took in its note that it
    # finished, and what hylly jobs says of it when its end has no words of its own
    failed: int = 0  # the files or problems that failed: a run that ends partly done with some has finished, not stopped
    stopped: int = 0  # the files it didn't get to finish, or 1 when it was stopped as a whole: no note that it finished
    told: bool = False  # it has sent its own note (a HyllyCopy, a backup, a question with nobody there): no other goes
    ran: bool = False  # it did its work though it ends needing a person (exit code 8): hylly jobs tells of it all the same

    def carried(self, e: BaseException) -> BaseException:
        """e, the Exit that ends the run (or what _in_words makes one of), with this to tell how it ended."""
        e.ended = self
        return e

    @contextlib.contextmanager
    def going(self):
        """A run whose facts are gathered as it goes (a catalog build's question): whatever ends it carries them, unless
        it carries its own, and its watch finds them under the Exit _in_words makes of it (_ended_of)."""
        try:
            yield self
        except BaseException as e:
            if getattr(e, "ended", None) is None:
                with contextlib.suppress(AttributeError):  # one that takes no attributes of its own: a bug, said as one
                    e.ended = self
            raise


LOGIN_AGAIN = "The login is missing, has expired or was refused. Log in again, and it goes on where it stopped."
KEPT = "What was done is kept. bell.log says more."


def _network_trouble() -> type[BaseException] | tuple:
    """httpx's errors, the network's trouble as the shops and the lookup sources meet it, to catch: once httpx is
    loaded, as only a command that goes online loads it (and so only it can meet one), and nothing before. So a command
    that stays on this computer, and --version or --help, starts without loading it."""
    httpx = sys.modules.get("httpx")
    return httpx.HTTPError if httpx is not None else ()


def _keep_awake(on: bool) -> None:
    from . import download as dl

    dl.keep_awake(on)


def at_keyboard() -> bool:
    """Whether someone here can answer. On Windows, nul passes for a keyboard, as a scheduled task's input may be."""
    try:
        if not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            handle = wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno()))
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD())))
    except (AttributeError, OSError, ValueError):  # no stdin at all, as under pythonw; or one closed
        return False
    return True


NOBODY = "It asks first, and nobody is here to answer: show the list to its owner, and pass --yes once they have said yes. Nothing changed."


def _confirm(question: str, nobody: str = NOBODY) -> bool:
    """A yes from someone at the keyboard. With nobody there it ends here: a program has no answer to give."""
    if not at_keyboard():
        raise Exit(NEEDS_YOU, nobody)
    _note(f"{question} [y/N] ", end="")  # a question, on standard error: the output stays the command's result
    try:
        return input("").strip().lower() in ("y", "yes")
    except EOFError:
        raise Exit(NEEDS_YOU, nobody) from None


def _note(*words, **kw) -> None:
    """Progress, a note, a warning or a question: on standard error, so that standard output, piped or in a file, has the
    command's result only."""
    print(*words, file=sys.stderr, flush=True, **kw)


def _hidden(prompt: str, command: str) -> str:
    """A password or key typed at the keyboard. Without one it ends here: on Windows getpass reads the console itself, and
    would wait for ever."""
    nobody = f"This needs a person: its owner runs {command} in a terminal. Nothing changed."
    if not at_keyboard():
        raise Exit(NEEDS_YOU, nobody)
    try:
        return getpass.getpass(prompt)
    except EOFError:  # Ctrl-D, or input that has ended
        raise Exit(NEEDS_YOU, nobody) from None


@contextlib.contextmanager
def _watched(job: str, args):
    """A long job: the computer stays awake while it runs, and you are told if it fails, or when it ends after a long
    while or with failed files. Not for Ctrl-C, a stop you asked for or a dry run, nor when it fails at once in front of
    you: a mistyped option is no news for your phone. What the exit code doesn't say is the run's Ended: the one the Exit
    it ends with carries (_ended_of), else its handler's, which _run hands back as the run's ended (it yields the run).
    Each run has its own, as each shop's of hylly download has, which runs it as a command of its own."""
    from . import bell

    what, began = runs.JOBS[job][0], time.monotonic()
    quiet = getattr(args, "dry_run", False)
    in_front_of_you = lambda: at_keyboard() and time.monotonic() - began < ON_THE_SPOT  # noqa: E731
    started, ended = time.strftime("%Y-%m-%d %H:%M"), {"code": 0, "said": ""}
    run, facts = SimpleNamespace(ended=None), Ended()  # run.ended: what its handler returned
    before = sys.exc_info()[1]  # what was being handled as it began, should it begin in an except: not this run's

    def finished(facts: Ended) -> None:
        took = time.monotonic() - began
        if not quiet and not facts.stopped and not facts.told and (took >= LONG_JOB or facts.failed):
            text = f"It took {time_words(took)}." + (f" {facts.text}" if facts.text else "")
            bell.job_note(f"{what} finished", text, text)

    outer = dict(runs.RECORD)  # a watched run inside another's, should one ever start one: the outer one's comes back after
    runs.RECORD.clear()
    if not quiet:  # its record begins as it takes its locks, as one that hasn't ended (runs._begun)
        runs.RECORD.update(job=job, started=started)
    _keep_awake(True)
    try:
        yield run
    except SystemExit as e:  # the run's own end, or what _in_words made of a shop's, the network's or a disk's
        words = e.message if isinstance(e, Exit) else e.code if isinstance(e.code, str) else ""
        ended.update(code=e.code if isinstance(e.code, int) else PARTLY if e.code else 0, said=words)
        facts = _ended_of(e, before) or run.ended or Ended()  # its handler's, had it returned before a lock's end refused it
        if e.code in (None, 0) or (e.code == PARTLY and facts.failed):  # it ended, with some files failed
            finished(facts)
        elif e.code not in (USAGE, INTERRUPTED, NEEDS_YOU) and not quiet and not isinstance(e, runs.Busy) and not in_front_of_you() and not facts.told:
            bell.job_note(f"{what} stopped", words or "See the job's own output.", getattr(e, "public", ""))
        raise
    except KeyboardInterrupt:
        ended.update(code=INTERRUPTED, said="Stopped.")
        raise
    except Exception as e:  # noqa: BLE001 - what _in_words has no words for is a bug: main says so, with its traceback
        if not quiet and not in_front_of_you():  # by its kind alone: its message may have an address in it
            bell.job_note(f"{what} stopped by a bug", f"{type(e).__name__}. This is a bug in Hylly: the job's output has the "
                          f"details, to report at {ISSUES}.", "It stopped by a bug in Hylly. What was done is kept; the job's "
                          "output says more.")
        ended.update(code=BUG, said=f"Stopped by a bug in Hylly ({type(e).__name__}): its output has the details, to report.")
        raise
    else:
        facts = run.ended or Ended()
        finished(facts)
    finally:
        _keep_awake(False)
        if not quiet and (ended["code"] not in (USAGE, BUSY, NOT_SET_UP, NEEDS_YOU) or facts.ran):  # what hylly jobs tells of. A run that didn't start isn't one
            runs.remember(job, started, time.monotonic() - began, ended["code"], ended["said"] or facts.text)
        else:
            runs.unbegun(job)
        runs.RECORD.clear()
        runs.RECORD.update(outer)


def _ended_of(e: BaseException | None, before: BaseException | None = None) -> Ended | None:
    """How a run ended, as what it ended with carries it (Ended.carried), or what that was raised in place of: an error
    in an except or a finally (the words _in_words makes of one, a note that couldn't go), whose run had said already.
    The first Ended on the way back, which stops at before: what was being handled as the run began, and what it was
    raised in place of, are another's."""
    seen: set[int] = set()
    while e is not None and e is not before and id(e) not in seen:
        seen.add(id(e))
        if isinstance(getattr(e, "ended", None), Ended):
            return e.ended
        e = e.__cause__ or e.__context__  # the one it was raised from, or while: "from None" keeps it too
    return None


def _titles_kept_for(root) -> int:
    """How many titles this computer's database has in the catalog of the library at root, if it knows that folder as its
    library: 0 if it doesn't, or there's no database, or it can't be read. Opened read-only: init only looks."""
    import sqlite3

    from . import db

    if not db.file().is_file():
        return 0
    try:
        c = db.snapshot(db.file())
    except (sqlite3.Error, db.Unusable):
        return 0
    try:
        if not db.standing(c, db.file())[0]:  # no tables yet: only those of a version that knows them are read
            return 0
        mine = c.execute("SELECT root FROM library").fetchone()
        same = mine is not None and os.path.normcase(os.path.abspath(mine[0])) == os.path.normcase(os.path.abspath(root))
        return c.execute("SELECT count(*) FROM title").fetchone()[0] if same else 0
    except (sqlite3.Error, db.Newer, db.Unusable):
        return 0
    finally:
        c.close()


def _bring_books_in(root, then: str) -> str:
    """An empty library's next step, at the end of hylly init and of a hylly catalog that found nothing: how books
    get in, as the README's quick start has it, for someone with no shop's login too."""
    return (f"Next: hylly login SHOP and hylly download for your books at a shop, or put a folder of your own books in "
            f"{root}; then {then}.")


def _library_line(root) -> str:
    """settings.toml's line for the library: the path in single quotes, which take a Windows path as it is, unless it
    holds one itself or a character only an escape can write."""
    plain = str(root)
    if "'" in plain or any(ord(ch) < 32 or ch == "\x7f" for ch in plain):  # TOML's basic strings take JSON's escapes
        return "library = " + json.dumps(plain, ensure_ascii=False).replace("\x7f", "\\u007f")
    return f"library = '{plain}'"


def _settings_writable() -> bool:
    """Whether hylly init may write a settings.toml of its own: in this user's usual settings folder, or the one
    HYLLY_HOME or HYLLY_CONFIG_DIR names, as a Hylly tried apart from the usual one has; never a file HYLLY_SETTINGS names,
    which is someone's own, nor, on Windows, a folder another LOCALAPPDATA or APPDATA leads to
    (paths.usual_settings_folder)."""
    named = os.environ.get("HYLLY_HOME") or os.environ.get("HYLLY_CONFIG_DIR")
    return not os.environ.get("HYLLY_SETTINGS") and (bool(named) or paths.usual_settings_folder())


def _library_in_settings(root, dry_run: bool) -> None:
    """hylly init FOLDER's word on the settings, so that the commands find the library without --library: with no
    settings.toml yet, one is written with the library's line alone; one there already is never changed, and gets the
    line to add."""
    file, line = paths.settings_file(), _library_line(root)
    try:  # a name that isn't text, as Linux may have: settings.toml, UTF-8, can't hold it
        written = f"{line}\n".encode("utf-8")
    except UnicodeEncodeError:
        shown = str(root).encode("utf-8", "backslashreplace").decode("utf-8")
        return print(f"{shown}'s name isn't UTF-8 text, which {file} can't hold: give the library with --library or "
                     "HYLLY_LIBRARY, or give the folder a name that is.")
    if file.exists() or not _settings_writable():
        return print(f"To use it without --library, put this in {file}, at the top, above the first [section]:  {line}")
    if dry_run:
        return print(f"It would also write {file}, as there is none yet, with the library's line: {line}")
    file.parent.mkdir(parents=True, exist_ok=True)
    try:  # made new, never over one that came meanwhile; on Linux and macOS its user's alone, as it may hold secrets later
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return print(f"To use it without --library, put this in {file}, at the top, above the first [section]:  {line}")
    with os.fdopen(fd, "wb") as fh:
        fh.write(written)
    print(f"Wrote {file}, with the library's line, so that commands find it without --library: {line}")
    _mention_app_copies()  # written from inside a packaged app, it is that app's copy


def cmd_init(args) -> None:  # noqa: D103
    """Makes a folder a Hylly library, or says that it is one. An empty or new folder is asked about first: an unmounted
    disk leaves an empty folder where the library was, and marked, it would take every download onto the wrong disk.
    With FOLDER, the settings get the library's line, if they have no file yet (_library_in_settings)."""
    from . import db

    root = paths.Path(args.folder).absolute() if args.folder else paths.library_root(args.library)
    marker = paths.marker_file(root)
    if marker.is_file():
        print(f"{root} is a Hylly library already.")
        if args.folder:
            _library_in_settings(root, args.dry_run)
        return
    new = not root.is_dir()
    empty = new or not any(root.iterdir())
    titles = _titles_kept_for(root) if empty and not args.new else 0
    if titles:
        raise Exit(NOT_SET_UP, f"{root} is {'not there' if new else 'empty'}, but this computer's database knows it as the "
                               f"library, with {many(titles, 'title')} in its catalog. Is its disk or share there? An "
                               f"unmounted disk leaves just such an empty folder, and marked, it would take every download onto "
                               f"the wrong disk. Mount the disk or connect the share, and the library is found again. Only if "
                               f"that library is gone for good, and this is to be a new one in its place: hylly init {paths.typed(root)} --new. "
                               f"Nothing changed.")
    print(f"This marks {root} as a Hylly library, by writing {marker}{', and making the folder' if new else ''}.")
    if args.dry_run:
        if args.folder:
            _library_in_settings(root, dry_run=True)
        return print((f"{root} is {'not there yet' if new else 'empty'}, so it would ask first. " if empty else "") + "Nothing changed.")
    if empty:
        _note(f"{root} is {'not there yet' if new else 'empty'}. An unmounted disk leaves an empty folder where a library was: "
              f"if the library's disk or share should be there, it isn't, and this is the wrong folder to mark.")
        nobody = (f"{root} is {'new' if new else 'empty'}, so init asks first, and nobody is here to answer. Once its owner has "
                  f"checked that it's the folder for a new library, not where an unmounted disk should be, hylly init {paths.typed(root)} "
                  f"--yes marks it. Nothing changed.")
        if not args.yes and not _confirm("Make it a new Hylly library?", nobody):
            return print("Nothing changed.")
    atomic_write(marker, json.dumps({"id": secrets.token_hex(16), "created": db.now()}, indent=1))
    print(f"{root} is now a Hylly library. To undo it, delete {marker}.")
    if args.folder:
        _library_in_settings(root, dry_run=False)
    if empty:  # nothing in it yet: how books come in
        print(_bring_books_in(root, "hylly catalog"))


COOKIES = {"humble": (SITES["humble"][0], "www.humblebundle.com"), "storybundle": ("_storybundle31_session", "storybundle.com"),
           "itchio-bundles": ("itchio", "itch.io")}  # the logins that are a browser's cookie: its name, and the site's own name


def cmd_login(args) -> None:
    site = args.site
    if getattr(args, "forget", False) or getattr(args, "forget_all", False):
        return _forget(args)
    if not site:
        raise Exit(USAGE, f"Which login? hylly login SITE, one of: {', '.join(LOGINS)}.")
    if getattr(args, "cookies", None) is not None and site not in COOKIES:
        raise Exit(USAGE, f"--cookies takes a browser's login cookie, and {site} takes {'an' if SITES[site][0][0].lower() in 'aeiou' else 'a'} "
                          f"{SITES[site][0]}: give it with --stdin or --clipboard, or at the prompt. Nothing was saved.")
    if site == "drivethrurpg":
        _login_drivethrurpg(args)
        return
    if site in LOOKUPS and LOOKUPS[site].key:  # a lookup source's key
        _login_lookup(args, site)
        return
    if site == "itchio":
        _login_itchio(args)
        return
    if site not in COOKIES:  # a new shop's key login with no branch above: a bug, said plainly, before it's asked for
        raise RuntimeError(f"Hylly has no way to take a {site} {SITES[site][0]}: its branch is missing from cmd_login.")
    cookie, host = COOKIES[site]
    value = _given(args, site, "Cookie: ", f"Paste the value of the {cookie} cookie from {host} and press Enter.",
                   f"(Where each browser keeps it, under {host}: {COOKIE_PLACES}.)")

    def check(given: str) -> str:
        if site == "storybundle":
            from . import storybundle

            return f"StoryBundle accepted it: your 'My Book Bundles' page opened ({size_words(len(storybundle.library_page(given)))})."
        if site == "itchio-bundles":
            from . import itchio_survey

            size, bundles = itchio_survey.check_cookie(given)
            return f"itch.io accepted it: your bundles page opened ({size_words(size)}, {many(bundles, 'bundle link')})."
        if site == "humble":
            from .humble import HumbleClient

            with HumbleClient(given, tries=1) as client:  # asked once: no answer is said at once
                client.check()
                return "Humble accepted it: it says you're signed in."
        # a cookie login with no check of its own, a new shop's say: a bug, and never sent to another shop to try
        raise RuntimeError(f"Hylly has no way to check a {site} cookie: its check is missing from cmd_login.")

    _save_if_accepted(site, value, check, args.dry_run)


def _given(args, site: str, prompt: str, *how: str) -> str:
    """The login as its owner gives it: from a cookies.txt, standard input or the clipboard, or typed unseen. Only its
    length is ever said."""
    if getattr(args, "cookies", None) is not None:
        value = usable(_cookie_from(args.cookies, site), site)
        _note(f"Read the {COOKIES[site][0]} cookie, {many(len(value), 'character')}, from "
              + ("standard input." if args.cookies == "-" else f"{args.cookies}. It holds your logins to every site it has "
                 "cookies of: delete it once this one is saved."))
        return value
    if getattr(args, "stdin", False):
        value = usable(_piped_in("the login", "--stdin"), site)
        _note(f"Read {many(len(value), 'character')} from standard input.")
        return value
    if args.clipboard:
        value = usable(read_clipboard(), site)
        _note(f"Read {many(len(value), 'character')} from the clipboard. They stay there until you copy something else.")
        return value
    command = f"hylly login {site}, or with --clipboard,"
    if at_keyboard():
        for line in how:
            _note(line)
        _note("Input is hidden, so nothing appears while you paste.")
    return usable(_hidden(prompt, command), site)


SECRET_MOST = 16384  # bytes on standard input for a password or a login: no more than any is
COOKIES_MOST = 10 << 20  # bytes of a cookies.txt: a browser's whole jar, and nothing a mistake could fill memory with


def _not_for_others(st: os.stat_result, what: str) -> None:
    """A file with a secret in it, refused where others than its owner may read or change it: on Linux and macOS, by
    its rights, as ssh refuses a key. On Windows a file's rights are its folder's, which this doesn't read."""
    if os.name != "nt" and stat.S_ISREG(st.st_mode) and st.st_mode & 0o077:
        raise Exit(USAGE, f"{what} may be read by others on this computer (its rights are {stat.S_IMODE(st.st_mode):o}), and it "
                          "holds a secret: chmod 600 it, and run the command again. Nothing was read.")


def _piped_in(what: str, option: str, most: int = SECRET_MOST) -> str:
    """A secret on standard input, as docker login --password-stdin and gh auth login --with-token take theirs: from a
    pipe or a file, never the keyboard, where it would show as it's typed, nor a file others may read. As it came, but
    for the UTF-8 mark Windows may put first; it is never shown."""
    stream = sys.stdin
    if stream is None or stream.isatty():
        raise Exit(USAGE, f"{option} reads {what} from a pipe or a file, not the keyboard, where it would show as it's typed: "
                          f"leave {option} out, and it's asked for unseen. Nothing changed.")
    try:
        _not_for_others(os.fstat(stream.fileno()), "The file on standard input")
    except (AttributeError, OSError, ValueError):  # no file under it, as in a test: nothing to look at
        pass
    raw = stream.buffer.read(most + 1) if hasattr(stream, "buffer") else stream.read(most + 1).encode("utf-8")
    if len(raw) > most:
        raise Exit(USAGE, f"More than {most:,} bytes came on standard input for {what}: give {what} alone. Nothing changed.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Exit(USAGE, f"What came on standard input for {what} isn't UTF-8 text. Nothing changed.") from None


def _cookie_from(given: str, site: str) -> str:
    """The site's login cookie from a cookies.txt, in the Netscape format that browsers' add-ons export and yt-dlp's
    --cookies takes: a line for each cookie, its fields apart by tabs (the site, whether for its subdomains too, the
    path, HTTPS only, until when, the name, the value), # before a note, and #HttpOnly_ before the site of one that
    scripts can't read. "-": from standard input. A file others may read is refused, where that can be checked, as it
    holds the logins to every site in it. Of several, the one that lasts longest; one for the browser's session alone
    counts as the newest."""
    name, host = COOKIES[site]
    if given == "-":
        text, where = _piped_in("the cookies", "--cookies -", COOKIES_MOST), "standard input"
    else:
        try:
            with open(given, "rb") as fh:
                _not_for_others(os.fstat(fh.fileno()), given)
                raw = fh.read(COOKIES_MOST + 1)
        except FileNotFoundError:
            raise Exit(USAGE, f"--cookies {given}: there's no such file here. Nothing was saved.") from None
        except OSError as e:
            raise Exit(LOCAL, f"--cookies {given} can't be read: {e.strerror or e}. Nothing was saved.") from None
        if len(raw) > COOKIES_MOST:
            raise Exit(USAGE, f"--cookies {given} is over {size_words(COOKIES_MOST)}: that's no cookies.txt. Nothing was saved.")
        text, where = raw.decode("utf-8", "replace"), given
    site_name = host.removeprefix("www.")
    found = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7 or fields[5] != name or not fields[6].strip() or not re.fullmatch(r"[0-9]{1,19}", fields[4].strip()):
            continue  # not a cookie's line, not this one, or "until" no time (a superscript digit, or 5,000 digits of them)
        domain = fields[0].strip().lstrip(".").lower()
        if domain == site_name or domain.endswith("." + site_name):
            found.append((int(fields[4]) or float("inf"), fields[6].strip()))
    if not found:
        raise AuthError(f"No {name} cookie of {site_name} in {where}. Log in at {host} in the browser, export its cookies "
                        "again (in the cookies.txt format, as a browser add-on saves them), and try again.")
    return max(found)[1]


def _save_if_accepted(site: str, value: str, check, dry_run: bool = False) -> None:
    """Asks the site first: a wrong one never replaces one that works. dry_run: only asked, and not saved."""
    kept = " Not saved: the login you had stays as it was." if session_path(site).is_file() else " Not saved."
    try:
        said = check(value)
    except AuthError as e:
        raise AuthError(f"{e}{kept}") from None
    except ShopError as e:  # the site's trouble, which says nothing about the login
        raise Exit(PARTLY, f"{e} So it couldn't be checked.{kept}") from None
    except _network_trouble() as e:
        raise Exit(PARTLY, f"Network trouble ({type(e).__name__}), so it couldn't be checked.{kept}") from None
    if dry_run:
        return print(f"{said} It would be saved to {session_path(site)}; nothing was saved.")
    print(f"Saved to {save_session(value, site)}. {said}")


def _login_drivethrurpg(args) -> None:
    from . import drivethrurpg

    value = _given(args, "drivethrurpg", "Key: ", "Paste your DriveThruRPG application key and press Enter.",
                   "(drivethrurpg.com > Account > Settings > Library App Keys: make a new key and copy it.)")
    _save_if_accepted("drivethrurpg", value, lambda given: f"DriveThruRPG accepted it: {drivethrurpg.check_key(given)} titles in your library.", args.dry_run)


def _login_itchio(args) -> None:
    from . import itchio

    value = _given(args, "itchio", "Key: ", "Paste your itch.io API key and press Enter.",
                   "(itch.io > your name, top right > Settings > API keys > Generate new API key; copy it.)")
    _save_if_accepted("itchio", value, lambda given: f"itch.io accepted it: logged in as {itchio.check_key(given)}.", args.dry_run)


# The commands that read the library's catalog or records, or write to it: a folder that isn't a Hylly library is said
# as such before they start (paths.check_library), not as trouble with the disk. tests/test_library_marker.py holds
# every command with --library to this, or to a reason of its own
NEEDS_THE_LIBRARY = {*shops.SHOPS, "list", "add", "catalog", "lookup", "verify", "tag", "set", "decide", "reviews",
                     "reports"}


def asks_a_shop(args) -> str | None:
    """The shop whose site a dry run reads, as its action's row says (effects.py), or a shop list (hylly SHOP list),
    which reads it as a dry run does: it waits its turn as a real run does, refused while one goes (runs.not_running),
    and takes no lock of its own."""
    from .effects import EFFECTS, SITE

    if args.command not in shops.SHOPS:
        return None
    if getattr(args, "action", None) == "list":
        return args.command
    if not getattr(args, "dry_run", False):
        return None
    return args.command if EFFECTS[args.leaf]["dry_run"] == SITE else None


def job_of(args) -> str | None:
    """The site a command talks to, or the library's files it writes, one run of each at a time (runs.py); None for
    offline work, and for every dry run, which writes no lock nor a record of how it ended: it changes nothing. A shop's
    action holds the job its row names (effects.py): the shop's own, or none for what's offline. A command with one is
    watched as it runs (_watched), and passed on to the server computer it starts there on its own (_here_or_there).
    tests/test_programs.py holds this to the job each row of effects.py names, and to runs.JOBS."""
    from .effects import EFFECTS

    if getattr(args, "dry_run", False):
        return None
    if args.command in shops.SHOPS:
        return EFFECTS[args.leaf]["job"]
    if args.command in ("lookup", "add", "catalog", "copy"):
        return args.command
    return "backup" if args.command in ("backup", "restore") else None


def _login_lookup(args, site: str) -> None:
    """A lookup source's key, with its row's words (sites.LOOKUPS): what the key is called, and where to get one."""
    from . import lookups

    row = LOOKUPS[site]
    value = _given(args, site, "Key: ", f"Paste your {row.name} {row.key} and press Enter.", f"({row.get_key})")

    def check(given: str) -> str:
        source = lookups.sources()[site](given)
        with tempfile.TemporaryDirectory() as folder:  # a budget of its own: one question needs no library, and a new key
            budget = lookups.Budget(paths.Path(folder) / "budget.txt", source.per_hour, source.per_day)  # no room in a day's budget
            session = lookups.Session(source, budget)
            try:
                return f"{source.check(session)}."
            except lookups.Refused as e:
                raise AuthError(str(e)) from None
            except lookups.LookupProblem as e:  # the source's trouble, which says nothing about the key
                raise ShopError(str(e)) from None
            finally:
                session.close()

    _save_if_accepted(site, value, check, args.dry_run)


def _ended_there(site: str) -> tuple[str, str]:
    """What a saved login is, and how it ends at its site (sites.ENDED_THERE); a lookup source's key from its row:
    where it was made (key_at, else the site it asks)."""
    if site in ENDED_THERE:
        return ENDED_THERE[site]
    row = LOOKUPS[site]
    return f"your {row.name} {row.key}", f"delete it where you made it, at {row.key_at or row.host}, if the site offers that"


def _forget(args) -> None:
    """hylly login SITE --forget, or --forget-all: the saved login or key deleted from the logins folder, and how to end
    it at the site, where it works until it's ended there. Said too: an environment variable set for it, which stands in
    for the file; and on Windows an app's own copy of Hylly's folders, where a login saved from inside that app is."""
    from . import doctor
    from .logins import forget_session

    if args.forget_all and args.site:
        raise Exit(USAGE, f"--forget-all deletes every saved login and key: leave out {args.site}, or for {args.site} alone, "
                          f"hylly login {args.site} --forget. Nothing was deleted.")
    if not args.site and not args.forget_all:
        raise Exit(USAGE, f"Which login? hylly login SITE --forget, one of: {', '.join(LOGINS)}; hylly login --forget-all "
                          "forgets every one. Nothing was deleted.")

    def a_link(site: str) -> str:  # a login's file that is a link: deleting it leaves the file it leads to, the login in it
        path = session_path(site)
        if not path.is_symlink():
            return ""
        return f", a link to {os.path.realpath(path)}" + (", which holds it and stays: delete that file too, if it should go"
                                                          if path.exists() else ", which isn't there")

    sites = [args.site] if args.site else list(SITES)
    saved = [site for site in sites if session_path(site).is_file() or session_path(site).is_symlink()]
    if not saved:
        print(f"No {args.site} {SITES[args.site][0] if args.site in KEYS else 'login'} is saved here, in "
              f"{session_path(args.site)}: nothing was deleted." if args.site else
              f"No login or key is saved here, in {paths.logins_dir()}: nothing was deleted.")
    elif args.dry_run:
        for site in saved:
            print(f"It would delete {session_path(site)}, {_ended_there(site)[0]}{a_link(site)}.")  # the link alone
        print("Nothing was deleted.")
    else:
        failed = []
        for site in saved:
            what, there = _ended_there(site)
            link = a_link(site)  # said before the link goes: then it leads nowhere to tell
            try:
                if forget_session(site):
                    print(f"Deleted {session_path(site)}, {what}{link}. It works at the site until it's ended there: {there}.")
            except OSError as e:
                failed.append(f"{session_path(site)} ({e.strerror or e})")
        if failed:
            raise Exit(LOCAL, f"Couldn't delete {', '.join(failed)}: it stays saved. Delete it by hand, or put right "
                              "what keeps it there, and run this again.")
    for site in sites:  # what stands in for a saved one, saved or not
        if os.environ.get(variable := SITES[site][2]):
            _note(f"{variable} is set here, and Hylly uses it in place of the file: unset it too, and wherever it's set "
                  "(a shell's profile, a service's file).")
    for copy in doctor.app_copies():
        _note(f"Note: an app keeps its own copy of Hylly's folders, {copy}: a login saved from inside that app, by an AI "
              "assistant say, is there and not here. Forget it from inside that app too.")


def _to_log(path, label: str, line: str) -> None:
    """One line at the end of a log of the state folder's, download-log.txt, with when and the shop's tag (label), as
    every log is written (storage.append_log), moved aside beyond its limit. Opened per line: a handle kept open on a
    network share goes bad when the share drops out for a moment (and would end the run), so it is tried three times.
    Trouble writing it is no reason to stop: the terminal has the line."""
    with contextlib.suppress(OSError):
        append_log(path, f"{datetime.now():%Y-%m-%d %H:%M:%S} {label}{line}", RUN_LOG_LIMIT, attempts=3, wait=2)


def _thinned(state, shop_id: str, thinned) -> None:
    """What a run says of the older snapshots thinned as the shop's new one was saved (snapshot.put): which went, and
    what couldn't be done, a line each, on standard output, in state/download-log.txt and in --json."""
    lines = thinned.lines(shop_id)
    if not lines:
        return
    for line in lines:
        print(line)
        _to_log(state / "download-log.txt", shops.SHOPS[shop_id].tag, line)
    _report(snapshots_removed=thinned.removed, **({"snapshots_not_removed": thinned.trouble} if thinned.trouble else {}))


def cmd_humble_download(args) -> Ended | None:
    """hylly humble download, on the shops' core as every shop's (_shop_download): Humble's Library reads your orders,
    with the formats you chose and those downloaded already (humble.Library), a dry run too."""
    from . import humble

    root = paths.library_root(args.library)
    return _shop_download(args, "humble", lambda session: humble.Library(session, root, note=_note, progress=_progress))


def _check_space(root, need: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if need + (1 << 30) > free:  # the files, and 1.1 GB besides for the disk's own needs
        raise Exit(LOCAL, f"Not enough space: {size_words(need)} needed, {size_words(free)} free on {root.anchor or root}.")


def _run_jobs(root, manifest, jobs, *, refresh, workers: int, failures: str, link_refreshes: int = 2, label: str = "",
              notes: list[str] | None = None, left: list | None = None, name=None, refused: str | None = None,
              gone=None, yours: int = 0) -> Ended:
    """Download jobs with logging to state\\download-log.txt, every shop's, each line with the shop's tag (label).
    notes: lines the terminal has had already, for the log too (what an itch.io run leaves out). left: the files the
    selection left out (download.Left), said already, which its --json names with those a file's link names so only as
    its download starts (name: what names a file the shop names only in its link, StoryBundle's). refused: the answers
    the shop's run counts as turned away, in its words (download.REFUSED); gone: what the shop does about a file it no
    longer has (download.Downloader). yours: the files you left out (a shop list's unticked rows), counted apart in its
    end line, as hylly jobs and the phone note have it: no failure, nothing pending. How the run ended (Ended) is
    returned, or carried by what it ends with."""
    from . import download as dl

    state = paths.state_dir(root)
    state.mkdir(parents=True, exist_ok=True)
    need = sum(j.size for j in jobs)
    log_path = state / "download-log.txt"

    def log(line: str, terminal: bool = True, result: bool = False) -> None:
        # The terminal always gets the line, as progress on standard error but for the run's end, its result; the log
        # file only if it can (_to_log).
        if terminal:
            print(line, flush=True, **({} if result else {"file": sys.stderr}))
            runs.progress(line)
        _to_log(log_path, label, line)

    log(f"Run started: {many(len(jobs), 'file')}, {size_words(need)}, {workers} at a time. Ctrl-C stops safely.")
    for line in notes or []:
        log(line, terminal=False)
    downloader = dl.Downloader(root, manifest, refresh=refresh, workers=workers, log=log, link_refreshes=link_refreshes,
                               gone=gone, name=name, refused=refused or dl.REFUSED)
    results = downloader.run(jobs)
    for line in dl.left_out(results.get("left", [])):  # a kind no download takes, known only now (StoryBundle's)
        log(line, result=True)
    blocked = downloader.blocked
    kept = len(results.get("kept", ()))  # chosen files that didn't come, with another format standing in: not done
    instead = f"{many(kept, 'chosen file')} didn't come, another format standing in" if kept else ""
    mine = f"{yours} left out by you" if yours else ""
    ended = Ended(text=f"{many(len(results['ok']), 'file')} done, {len(results['failed'])} failed." + (f" {instead}." if kept else "")
                  + (f" {mine}." if yours else ""), failed=len(results["failed"]),
                  stopped=len(results["stopped"]) or int(downloader.stop.is_set() or bool(blocked)))
    try:
        failed_total = dl.write_failures(state / failures, results, manifest)
    except OSError as e:
        failed_total = len(results["failed"])
        log(f"Could not write state/{failures} ({e}); the failures are listed above.")
    ok_bytes = sum(j.size for j, _ in results["ok"])
    _report(done=len(results["ok"]), bytes_done=ok_bytes, failed=len(results["failed"]), not_finished=len(results["stopped"]),
            kept_instead=kept, left_out_by_you=yours, failures=str(state / failures), failures_listed=failed_total,
            **({"left_out": dl.left_out([*(left or ()), *results["left"]])} if results.get("left") else {}))
    log(
        f"Run ended: {many(len(results['ok']), 'file')} done ({size_words(ok_bytes)}), {len(results['failed'])} failed, "
        f"{len(results['stopped'])} not finished{f', {instead}' if kept else ''}{f', {mine}' if yours else ''}. Failures "
        f"listed in state/{failures}: "
        f"{failed_total}.", result=True
    )
    if downloader.auth_failed:
        raise ended.carried(AuthError(downloader.auth_failed))
    if blocked:
        log(blocked)
        raise ended.carried(Exit(LOCAL if paths.library_missing(root) or downloader.disk_full else PARTLY, blocked))
    if downloader.stop.is_set():
        raise ended.carried(Exit(INTERRUPTED, "Stopped. What was downloaded is kept; running it again goes on from there."))
    if results["failed"]:
        raise ended.carried(Exit(PARTLY, f"{many(len(results['failed']), 'file')} failed: see state/{failures}. Running it "
                                         "again tries them again."))
    return ended


def cmd_storybundle(args) -> Ended | None:
    from . import storybundle

    return _shop_download(args, "storybundle", storybundle.Library,
                          skip=(storybundle.bonus, "Bonus books skipped") if args.no_bonus else None)


def cmd_drivethrurpg(args) -> Ended | None:
    from . import drivethrurpg

    return _shop_download(args, "drivethrurpg", drivethrurpg.Library)


def _kinds(given) -> frozenset[str] | None:
    """--kind's kinds, checked against the kinds in the rules (kinds.toml's [kinds.X], yours and --rules' too), as the
    command runs: None for none given."""
    from . import rules

    if not given:
        return None
    given = frozenset([given] if isinstance(given, str) else given)
    known = rules.current().kinds
    unknown = sorted(given - set(known))
    if unknown:
        raise Exit(USAGE, f"--kind: {', '.join(unknown)}? The kinds are {', '.join(known)} (hylly rules show kinds).")
    return given


NO_CATALOG = "No catalog yet: run hylly catalog first."


def _catalog_items(cat) -> list[dict]:
    """The catalog's titles, your corrections laid over them (cat: library.Catalog), or Exit (NOT_SET_UP) when the
    library has none yet."""
    items = cat.get()
    if not items:
        raise Exit(NOT_SET_UP, NO_CATALOG)
    return items


def _latest_snapshot(state, also: str = ""):
    """The newest snapshot of your Humble orders in the library's state folder, with the orders it holds
    (snapshot.latest), or Exit (NOT_SET_UP) saying how to make one; also: what else would do, said after that."""
    found = snapshot.latest(state, "humble")
    if found is None:
        raise Exit(NOT_SET_UP, "No snapshot of your Humble orders yet: run hylly humble download first (with --limit 0 it "
                               f"reads and saves your orders without downloading){also}.")
    return found


def _list_some(lines: list[str], most: int = 40) -> None:
    """The first most of these lines, each as it is, then how many more there are."""
    for line in lines[:most]:
        print(line)
    if len(lines) > most:
        print(f"  ... and {len(lines) - most} more")


def _list_bytes(where: str, writes: str) -> bytes:
    """A shop list's bytes: from standard input for - (a pipe or a file, never the keyboard, where it would wait for one
    typed), else from the file; never more than any list (shoplist.MOST), so a file that is no list isn't read whole."""
    from .shoplist import MOST

    if where == "-":
        stream = sys.stdin
        if stream is None or stream.isatty():
            raise Exit(USAGE, f"- reads the list from standard input, a pipe or a file (as - < humble-list.csv), and nothing "
                              f"is piped in: give the list's file, or pipe it in. {writes} writes one. Nothing was downloaded.")
        raw = stream.buffer.read(MOST + 1) if hasattr(stream, "buffer") else stream.read(MOST + 1).encode("utf-8")
        return raw
    try:
        with paths.Path(where).open("rb") as fh:
            return fh.read(MOST + 1)
    except FileNotFoundError:
        raise Exit(USAGE, f"There is no list at {where}: {writes} writes one. Nothing was downloaded.") from None
    except OSError as e:
        raise Exit(LOCAL, f"{where} can't be read ({e.strerror or type(e).__name__}). Nothing was downloaded.") from None


def _given_list(args, writes: str = "hylly list"):
    """A download's LIST, read once (shoplist.read: a file that isn't one stops here, before the shop is asked); None
    without one. writes: the command that writes such a list, for the words."""
    from . import shoplist

    where = getattr(args, "list", None)
    if not where:
        return None
    if getattr(args, "given_list", None) is None:
        args.given_list = shoplist.read(_list_bytes(where, writes), "Standard input" if where == "-" else where, writes)
    return args.given_list


def _no_choosers(args) -> None:
    """A LIST chooses the titles itself: the options that choose titles (commands.shop_lists) are refused beside it."""
    given = [option for dest, option in getattr(args, "choosers", ()) if getattr(args, dest, None) not in (None, False)]
    if given:
        raise Exit(USAGE, f"{', '.join(given)} {'chooses' if len(given) == 1 else 'choose'} titles, and a list chooses them "
                          "itself: give the list alone, or the options without it. Nothing was downloaded.")


def _list_out(args, shop_id: str | None) -> str:
    """Where hylly SHOP list (or hylly list) writes: FILE, else <shop>-list.csv (hylly-list.csv) in the current folder.
    One there already is never written over without --force: a list you edited, lost by a second run, is the mistake
    this keeps from you. Checked before any shop is asked."""
    from . import shoplist

    out = getattr(args, "file", None) or shoplist.name(shop_id)
    if out != "-" and paths.Path(out).exists() and not getattr(args, "force", False) and not args.dry_run:
        raise Exit(USAGE, f"{out} is there already, and may be a list you have edited: give another name, or add --force to "
                          "write over it. Nothing was written.")
    return out


_PARTS_OF_A_LIST: set[str] = set()  # the files hylly list has each shop's list write, to make one of: said nothing of


def _write_list(out: str, data: bytes, dry_run: bool, what: str, then: str = "hylly download", force: bool = False) -> None:
    """The list written whole (or nothing), or to standard output for -; a dry run says where it would go (and that it
    would write over one there, with --force). what: its rows in words; then: the command that takes it back."""
    if dry_run and out not in _PARTS_OF_A_LIST:  # a shop's part of hylly list's is no file of yours: written all the same
        there = out != "-" and paths.Path(out).exists()
        return _note(f"It would write {what} to {'standard output' if out == '-' else out}"
                     + ((" (there already: it would write over it, as --force says)." if force else
                         " (there already: only with --force).") if there else "."))
    if out == "-":
        sys.stdout.flush()
        stream = getattr(sys.stdout, "buffer", None)
        try:
            if stream is not None:
                stream.write(data)
                stream.flush()
            else:  # a stream of text alone (a test's)
                sys.stdout.write(data.decode("utf-8"))
        except OSError as e:  # a reader that has read enough (hylly SHOP list - | head): it ends quietly, as ever
            if not isinstance(sys.stdout, _Reader):
                raise
            sys.stdout._lost(e)
        return
    try:
        atomic_write(paths.Path(out), data)
    except OSError as e:
        raise Exit(LOCAL, f"{out} couldn't be written ({e.strerror or type(e).__name__}). Nothing was written.") from None
    if out in _PARTS_OF_A_LIST:  # a shop's part of hylly list's: that one says where the whole list went
        return
    _note(f"Wrote {what} to {out}: edit its download column in a spreadsheet or an editor, then {then} {paths.typed(out)} "
          "goes by your changes.")


def _list_rows(n: int, ticked: int) -> str:
    return f"{many(n, 'row')}, {ticked:,} ticked"


def _keep_choices(root, shop, sel, yours: dict) -> int:
    """What a list changed of your choices that last, kept as edits (curation.leave_out): the rows the rules had ticked
    that it unticked, left out from now on (under the title's catalog id), and those you left out that it ticked again,
    taken back under every id of the title's they are kept under (its other purchases' too: Title.also, as a title
    bought again may lead under another). yours: the files left out, as select read them (curation.left_out). A title
    the shop gives no key of its own (a Humble item known by its folder) has no catalog id to keep it by: its rows count
    for this run alone. How many files couldn't be kept so."""
    from . import curation

    changes: dict[str, tuple[str, dict]] = {}
    alone = 0
    for t, f, _ in sel.unticked:
        if t.key in ("", None):
            alone += 1
            continue
        changes.setdefault(curation.catalog_id(shop.id, t.key), (t.name, {}))[1][str(f.key)] = f.name
    for t, f, _ in sel.ticked_back:
        for key in [t.key, *getattr(t, "also", ())]:
            cid = curation.catalog_id(shop.id, key) if key not in ("", None) else ""
            if cid and str(f.key) in yours.get(cid, ()):
                changes.setdefault(cid, (t.name, {}))[1][str(f.key)] = None
    if changes:
        curation.leave_out(root, changes, by=curation.COMMAND_LINE)
    return alone


def _list_problems(shop, given, sel, titles) -> list[tuple[str, str, str]]:
    """The rows of a list a run couldn't take, each (its title, its id, why), the rest going on: what couldn't be read of
    it (shoplist.Given.problems), an id of another shop, or none, and one this shop's run matched no file by (its title
    or its file not listed any more, or a file no download takes)."""
    from . import shoplist

    from .shopping import row_keys

    found = list(given.problems)
    keys = {str(k): t for t in titles for k in row_keys(t)}
    for rid, _ in given.marks.items():
        if rid in sel.matched:
            continue
        title = given.named.get(rid, {}).get("title", "")
        split = shoplist.parts(rid)
        if not split or split[0] not in shops.SHOPS:
            found.append((title, rid, "matching nothing: an id is the shop's name, the title's key and the file's, as "
                                      "a list gives it"))
        elif split[0] != shop.id:
            found.append((title, rid, f"a row of {shops.SHOPS[split[0]].label}'s, not {shop.label}'s: hylly download "
                                      "takes every shop's"))
        elif split[1] not in keys or split[2] not in {str(f.key) for f in keys[split[1]].files}:
            found.append((title, rid, "not at the shop any more"))
        else:
            found.append((title, rid, "a file no download takes (hylly rules show formats)"))
    return found


def _shop_download(args, shop_id: str, library, skip=None) -> Ended | None:
    """hylly SHOP download, the same for every shop, on the shops' core (shopping.py). library: the shop's own Library
    (its module's), opened with its login. Its library is read and, but by a dry run, which leaves nothing behind, kept
    as a snapshot; then what is still to download is chosen, its totals and the first 40 said, and what the never-list
    leaves out (a dry run's too); the space is checked, and the files are fetched, each with a fresh link right before
    its download, unless the listing gave one (Humble's), and the formats behind a file with theirs; a file the shop
    names only in its link (StoryBundle's) is named by that link as its download starts, which then fetches it
    (shopping.Naming). A Library with ways of its own has them asked (shopping.py): the titles it takes (chosen), its
    words for what is left out (left_out), what it does about a file it no longer has (gone), the answers a run of its
    counts as turned away (refused), its own snapshot (snapshot), what its listing lacked this time (unanswered,
    no_data: said, and the run then ends partly done, however its downloads go), and its own words on the whole library
    at the end (summary: Humble's, per kind). skip: (a test, its words) for the titles a shop's own option leaves out of
    the run, which the totals line counts in those words (StoryBundle's --no-bonus). Each title is of the kind its shop
    found it to be (shopping.kind_found) or, laid over that after the snapshot is kept, the one you chose for it
    (shopping.corrected, by its catalog id): what --kind chooses by, and a title not downloaded yet is downloaded as.

    What the rules couldn't settle the run keeps as questions (curation.keep_questions; a dry run's only shown):
    those select found, and a shop's own (Library.questions, with the kinds it finds wholly: Library.covers); a run that
    read the shop's whole listing lets those of a title it no longer lists go (Library.incomplete, Library.listed). Each is
    the guess as before, or with [download] unsure = "ask" its file is held back; the totals say how many were, and the
    run ends by saying how many questions are open, the new ones apart (hylly decide), and in --json each of them.

    The shop list (shoplist.py, the same code path): hylly SHOP list (args.action list) reads the shop as a dry run does,
    keeps nothing, and writes the rows of the titles its options choose, the rules' picks ticked, to its FILE; a run
    with a LIST (args.list) goes by it (select's marks: only your changes count, a row as written going by the rules),
    the options that choose titles refused beside it, and keeps what it changed of your choices that last (the files it
    unticks where the rules had ticked them, left out from now on; those left out it ticks, taken back: _keep_choices)
    and the which-file questions whose mark it moved to one other file, answered with it (_answer_moved). Its rows it
    couldn't take are said with why, and the run ends partly done (exit code 1); a file that isn't a list stops it
    before the shop is asked. The files you left out are never
    fetched by a plain run, which counts them apart (Selection.yours: left out by you). How a run that downloaded
    ended (Ended) is returned, for its watch: None for a list, a dry run, or one with nothing to download."""
    from . import curation, settings, shoplist
    from . import download as dl
    from . import shopping

    shop = shops.SHOPS[shop_id]
    root = paths.library_root(args.library)
    state = paths.state_dir(root)
    listing = getattr(args, "action", None) == "list"  # hylly SHOP list: its rows written, nothing downloaded or kept
    dry = bool(args.dry_run) or listing
    given = None if listing else _given_list(args, f"hylly {shop.id} list")  # a LIST: what to fetch is its rows' say
    if given is not None:
        _no_choosers(args)
    out = _list_out(args, shop.id) if listing else None  # checked before the shop is asked
    kinds = _kinds(getattr(args, "kind", None))  # before the shop is asked anything
    manifest = dl.Manifest(root, shop.id)
    update = bool(getattr(args, "update", False))  # a shop without --update has no way to say when a file changed
    ended = None
    with library(load_session(shop.logins[0])) as lib:
        _note(f"Reading your {shop.label} library...")
        lib.check()
        titles = lib.titles(progress=runs.counting("titles"))
        _end_progress()
        unreadable = list(lib.unreadable)
        if unreadable:
            _note(shopping.unread(shop.id, unreadable))
        partly = getattr(lib, "unanswered", "")  # Humble's purchases it gave no data for this time
        if partly:
            _note(partly)
            _report(no_data=getattr(lib, "no_data", 0))
        thinned, own = snapshot.Thinned(), getattr(lib, "snapshot", None)  # a shop's own snapshot: Humble's orders
        snap = None if dry else own(state, thinned) if own else shopping.snapshot(  # a dry run, a list: none
            shop.id, titles, state, unreadable, manifest, thinned)
        _thinned(state, shop.id, thinned)
        shopping.corrected(shop.id, titles, curation.kinds(root))  # yours, after the snapshot: that keeps the shop's words
        answers = curation.answers(root)  # yours to the questions, which settle them
        yours = curation.left_out(root)  # the files you left out with a list
        sel = shopping.select(shop.id, titles, manifest, root, title=args.title, by=getattr(args, "by", None),
                              kinds=kinds, devices=getattr(args, "device", None), limit=None if listing else args.limit,
                              max_gb=None if listing else getattr(args, "max_gb", None), update=update,  # Humble's options
                              chosen=getattr(lib, "chosen", None),  # a shop's own choice too (itch.io's --types)
                              bundle=getattr(args, "bundle", None), skip=skip and skip[0],  # and StoryBundle's options
                              answers=answers, ask=settings.get("download")["unsure"] == "ask",
                              asked=curation.asked(root, shop.id), yours=yours,  # the files you left out
                              marks=given.of(shop.id) if given is not None else None)  # and a list's marks
        if listing:  # the rows of the titles chosen, the rules' picks ticked: written, and nothing else done
            found = shoplist.rows(shop.id, sel, datetime.now().strftime("%Y-%m-%d"))
            ticks = sum(r[-1] == shoplist.ON for r in found)
            _note(f"{many(len(titles), 'title')} at {shop.label}: {_list_rows(len(found), ticks)} (what a download would fetch, "
                  "done or not).")
            _write_list(out, shoplist.data(found), args.dry_run, _list_rows(len(found), ticks), f"hylly {shop.id} download",
                        getattr(args, "force", False))
            if partly:
                raise Exit(PARTLY, partly)
            return
        own_questions, covers = getattr(lib, "questions", None), getattr(lib, "covers", ())
        found = sel.questions + (own_questions(answers) if own_questions else [])
        # the shop's whole listing read (nothing unreadable, no purchase without data: Library.incomplete): a title it no
        # longer lists has no question either, whatever the run's options. Library.listed: every title the shop lists,
        # where its titles are only those it knows the files of (itch.io's)
        whole = not unreadable and not getattr(lib, "incomplete", False)
        listed = getattr(lib, "listed", None) or {str(t.key) for t in titles}
        open_now, new = curation.keep_questions(
            root, shop.id, found, lambda q: q["kind"] in covers or q["item"] in sel.looked or (whole and q["item"] not in listed),
            keep=not args.dry_run)
        moved = _answer_moved(root, shop, sel, open_now, args.dry_run)  # a which-file question's mark moved: answered
        if moved:  # answered (a dry run's would be): no question to decide any more
            new -= sum(q["first_found"] == q["last_found"] for q, _ in moved)
            open_now = [q for q in open_now if all(q is not a for a, _ in moved)]
            replan = getattr(lib, "replan", None)  # a shop's own plan (Humble's), for its summary: by the answers kept
            if replan and not args.dry_run:
                replan()
        words = getattr(lib, "left_out", None) or dl.left_out  # a shop's own words for it (itch.io's)
        files, size, left = sum(len(t.files) for t in titles), sum(f.size for _, f, _, _ in sel.pending), words(sel.left)
        beside = shopping.noted(shop.id, sel)  # every run, a dry run too: what the shop changed, coming beside the old
        about = "about " if any(getattr(f, "rough", False) for _, f, _, _ in sel.pending) else ""  # rounded sizes
        problems = _list_problems(shop, given, sel, titles) if given is not None else []
        alone = _keep_choices(root, shop, sel, yours) if given is not None and not args.dry_run else 0
        _report(dry_run=bool(args.dry_run), titles=len(titles), files=files, snapshot=snap.name if snap else None,
                unreadable=len(unreadable), to_download={"titles": sel.titles, "files": len(sel.pending), "bytes": size},
                already_done=sel.done, changed=sel.changed, substituted=sel.substituted, left_out=left,
                left_out_by_you=sel.yours, **({"unticked_this_run": sel.only_now} if sel.only_now else {}),
                **({"beside": sel.noted} if sel.noted else {}), **({"listed_again": sel.again} if sel.again else {}),
                **({"skipped": sel.skipped} if skip else {}),
                held_back=sel.held, **({"moved_by_hand": sel.forgotten} if sel.forgotten else {}),
                questions=open_now, questions_new=new,
                **({"pending": [{"path": str(path), "newer": bool(replace), "kind": shopping.kind_found(shop.id, t),
                                 "bytes": f.size,
                                 **({"why": t.why} if getattr(t, "why", "") else {})} for t, f, path, replace in sel.pending]}
                   if args.dry_run else {}))
        print(
            f"{many(len(titles), 'title')}, {many(files, 'file')}" + (f" (snapshot {snap.name})" if snap else "") + ". To download: "
            f"{many(sel.titles, 'title')}, {many(len(sel.pending), 'file')}, {about}{size_words(size)}. Already done: "
            f"{many(sel.done, 'file')}."
            + (f" Tried again among them: {many(sel.substituted, 'file')} with another format in "
               f"{'its' if sel.substituted == 1 else 'their'} place." if sel.substituted else "")
            + (f" Changed on {shop.label}: {many(sel.changed, 'file')}" + ("." if update or not shop.update else
                                                                  " (run with --update to fetch them).") if sel.changed else "")
            + (f" Listed more than once: {sel.again}, each taken once." if sel.again else "")
            + (f" {skip[1]}: {sel.skipped}." if skip and sel.skipped else "")
            + (f" Held back for your answer: {many(sel.held, 'file')} (unsure = \"ask\" under [download])." if sel.held else "")
            + (f" Moved by hand, as you said, and not fetched again: {many(sel.forgotten, 'file')}." if sel.forgotten else "")
            + (f" Left out by you: {many(sel.yours, 'file')}, not fetched (hylly {shop.id} list shows "
               f"{'it' if sel.yours == 1 else 'them'})." if sel.yours else "")
            + (f" Unticked in your list, for this run alone: {many(sel.only_now, 'file')}." if sel.only_now else "")
        )
        summary = getattr(lib, "summary", None)  # what the whole library has and needs (Humble's, per kind)

        def from_the_list() -> None:
            """What the list says (docs/downloads.md), a dry run's with or without one: before the questions."""
            if given is not None or args.dry_run:
                _from_the_list(shop, given, sel, size, about)

        def to_decide() -> None:
            """The run's last words, a dry run's too: how many questions are open at the shop, the new ones apart."""
            n = len(open_now)
            if not n:
                return
            said = f"{many(n, 'question')} to decide" + (
                "" if not new else (" (new)" if n == 1 else ", all new") if new == n else f", {new} of them new")
            if args.dry_run and new:  # kept by a run only
                print(f"{said}; a dry run keeps no new one, so hylly decide lists "
                      + ("them" if new == n else f"the other {n - new} now, and the new") + " once a run has found them.")
            else:
                print(f"{said}: hylly decide lists them and takes your answers.")

        def summed() -> None:
            """The summary last, the library as the run leaves it (its downloads recorded in manifest), a dry run's
            too, after its list of files: in words and in --json. Only words on top of the run: a library gone away
            gets none (the run says so itself), and one that can't be worked out now (a share dropping out, say) is
            said in a line, the run's own end kept as it is."""
            if paths.library_missing(root):
                return
            try:
                lines, found = summary(manifest, root)
            except Exception as e:  # noqa: BLE001 - its words are no part of what the run did
                print(f"The summary of the {shop.label} library couldn't be made this time ({type(e).__name__}: {e}); "
                      "the run went as said above.")
                return
            _report(**found)
            for line in lines:
                print(line)

        try:
            if args.dry_run:
                _list_some([f"  {'(newer) ' if replace else ''}{path}" for _, _, path, replace in sel.pending])
            for line in beside + left:  # part of what the run did, a dry run's too: with its result
                print(line)
            if not summary:  # every shop's partial files counted, those no download goes on from named; Humble's
                parts, leftovers = dl.left_behind(shop.id, titles, manifest, root, answers=answers, yours=yours)
                _report(**parts)  # summary has them at its end, its folder walked once
                for line in leftovers:
                    print(line)
            if given is not None:  # after the files listed, so that none reads as one of these
                _list_notes(shop, given, sel, alone, problems, state, args.dry_run, moved)
            if sel.pending and not args.dry_run:
                _check_space(root, size)
                jobs, chosen = [], {}  # each job's title and file, by the job's id: one named by its link changes place
                for t, f, path, replace in sel.pending:
                    job = shopping.make_job(shop.id, t, f, path, replace)
                    jobs.append(job)
                    chosen[id(job)] = (t, f)
                    for behind, other in zip(job.fallbacks, getattr(f, "fallbacks", ())):  # the formats behind it (Humble's)
                        chosen[id(behind)] = (t, other)
                fetched = {job.path for job in jobs}  # a format behind a chosen file that a list ticked comes as a job of
                for job in jobs:  # its own: never also as that file's fallback, to the same place in the same run
                    job.fallbacks = [behind for behind in job.fallbacks if behind.path not in fetched]
                # the files the shop names only in their links (StoryBundle's), each named by its first link as its
                # download starts, which that link then fetches
                naming = (shopping.Naming(shop.id, root, manifest, lib.link, chosen)
                          if any(not f.name for _, f, _, _ in sel.pending) else None)
                gone = getattr(lib, "gone", None)  # what the shop does about a file it no longer has (itch.io asks again)
                try:
                    ended = _run_jobs(root, manifest, jobs, refresh=lambda job: lib.link(*chosen[id(job)]),
                                      workers=args.jobs, failures=shop.failures, link_refreshes=shop.link_refreshes,
                                      label=shop.tag, notes=beside + words(sel.left), left=sel.left, name=naming,
                                      refused=getattr(lib, "refused", dl.REFUSED), yours=sel.yours,
                                      gone=(lambda job: gone(*chosen[id(job)], job, manifest)) if gone else None)
                finally:
                    if naming is not None:  # what its links named as a kind no download takes, for the runs after it;
                        with contextlib.suppress(OSError):  # not written, it is asked about again: never the run's end
                            naming.keep()
        except BaseException:  # a run that failed or stopped says it too, never in place of how it ended
            if summary:
                with contextlib.suppress(Exception):
                    summed()
            from_the_list()
            to_decide()
            raise
        if summary:
            summed()
        from_the_list()
        to_decide()
    if partly:  # said at the start; the run ends with it, partly done, however its downloads went
        raise Exit(PARTLY, partly)
    if problems:  # said above, with why: the rest went on
        raise Exit(PARTLY, f"{many(len(problems), 'row')} of your list couldn't be taken: said above, "
                           "each with why. The rest went as said.")
    return ended


def _from_the_list(shop, given, sel, size: int, about: str) -> None:
    """What a list says, in one sentence (docs/downloads.md): its rows of this shop, how many ticked, how many to
    download and how much, and how many of the ticked are in the library already. Without a list (a dry run), the list
    the rules make, never written out."""
    if given is not None:
        marks = given.of(shop.id)
        rows, ticks = given.rows.get(shop.id, 0), sum(on for on, _ in marks.values())
        here = sum(1 for r in sel.rows if r[6] and marks.get(r[5], (False,))[0])
        head = "From your list"
    else:
        from .shoplist import ticked

        rows, ticks = len(sel.rows), sum(ticked(r[3], r[4]) for r in sel.rows)
        here = sum(1 for r in sel.rows if r[6] and ticked(r[3], r[4]))
        head = "From the list the rules make"
    print(f"{head}: {_list_rows(rows, ticks)}: {len(sel.pending):,} to download ({about}{size_words(size)}), {here:,} in "
          "the library already.")


def _answer_moved(root, shop, sel, open_now: list[dict], dry_run: bool) -> list[tuple[dict, str]]:
    """The which-file questions whose mark a list moved to exactly one other of the title's files, the guess unticked
    (Selection.answers): each answered with that file by its option's number (two files may share a name), as hylly
    decide NUMBER N answers it (curation.answer: a file correction, the same edit), with what the question offered, so
    that one offering other files by now is left as it is; but in a dry run, which keeps nothing. So the question
    closes, and the guess is never fetched later. What was answered (or a dry run would answer): [(the question, the
    file's name)]."""
    from . import curation

    done = []
    for t, n, options in sel.answers:
        cid = curation.catalog_id(shop.id, t.key)
        q = next((q for q in open_now if q["kind"] == "which-file" and q["catalog_id"] == cid and not q["file"]), None)
        name = str(options[n].get("file") or "")
        if q is None:
            continue
        if not dry_run:
            try:
                curation.answer(root, q["id"], str(n + 1), by=curation.COMMAND_LINE, offered=curation.fingerprint(options))
            except Exit as e:  # the question changed under it, say: it stays open, and is said
                _note(f"Your list's choice of {name} for {t.name} couldn't answer its question: {e.message}")
                continue
        done.append((q, name))
    return done


def _list_notes(shop, given, sel, alone: int, problems: list, state, dry_run: bool = False,
                moved: list | None = None) -> None:
    """What a run with a list says besides its totals, after its files: the rows it couldn't take, each with why (also
    in --json, and the log but in a dry run, which writes nothing); the files the shop changed since the list (another
    size than its row says, within the rounding it was shown with: the current file comes under the same id); the
    questions it answered by a mark moved; what of your choices it changed for good, and that a list without picked
    counts for that run alone; and how old the list is, where titles bought since aren't in it (by their purchase dates:
    a shop that gives none has no such note)."""
    from .shoplist import line, same_size

    if problems:
        print(f"Rows of your list not taken ({len(problems)}), the rest going on:")
        for title, rid, why in problems:
            print(line(title, rid, why))
            if not dry_run:
                _to_log(state / "download-log.txt", shop.tag, f"Not taken from the list: {line(title, rid, why).strip()}")
    _report(list_problems=[{"title": t, "id": rid, "why": why} for t, rid, why in problems])
    taken = {id(f) for _, f, _, _ in sel.pending}
    changed = [(t, rid) for t, f, _, _, _, rid, _ in sel.rows if id(f) in taken and given.named.get(rid, {}).get("mb")
               and not same_size(given.named[rid]["mb"], f.size)]
    if changed:
        print(f"Changed at {shop.label} since your list ({len(changed)}): the file it lists now comes, under the same id:")
        for t, rid in changed[:10]:
            print(line(t.name, rid, "another size than the list says"))
    for q, name in moved or []:
        number = next((n for n, o in enumerate(q["options"], 1) if o.get("file") == name), "N")
        print(f"Your list {'would answer' if dry_run else 'answered'} the question of which file to keep for {q['title']} "
              f"({q['catalog_id']}): {name}, as hylly decide {q['id'] or 'NUMBER'} {number} would"
              + ("." if dry_run else "; hylly decide --answered lists it, and auto takes it back."))
    unpicked = given.unpicked.get(shop.id, 0)
    if unpicked:
        whole = unpicked >= given.rows.get(shop.id, 0)
        print(("Your list has no picked column" if whole else f"{many(unpicked, 'row')} of your list "
               "can't say what the rules ticked (picked)") + ": its marks there count for this run alone, and nothing is "
              f"kept of them. hylly {shop.id} list writes a list with it.")
    it = lambda n: "it" if n == 1 else "them"  # noqa: E731
    new = sum(not there for _, _, there in sel.unticked)  # of the files the rules take, not those in the library
    if new and dry_run:
        print(f"Your list would leave out {many(new, 'file')} that a download would take, from then on: a dry run keeps nothing.")
    elif new:
        print(f"Your list left out {many(new, 'file')} that a download would take: no plain download fetches {it(new)} from now "
              f"on, and a new list shows {it(new)} unticked, \"left out\" (tick {it(new)} again, or give hylly set left-out "
              "auto --id the row's id, to take one back).")
    if sel.ticked_back:
        print(f"Your list {'would take' if dry_run else 'took'} back {many(len(sel.ticked_back), 'file')} you had left out: a "
              f"download takes {it(len(sel.ticked_back))} again.")
    if alone:
        print(f"{many(alone, 'file')} of titles {shop.label} gives no id for are left out of this run alone: no download can "
              "keep that choice.")
    since = [t for t in sel.unlisted if given.written and str(getattr(t, "purchased", "") or "")[:10] > given.written]
    if since:
        days = (datetime.now() - datetime.fromisoformat(given.written)).days
        print(f"This list was written {many(days, 'day')} ago; {many(len(since), 'item')} "
              f"bought since {'is' if len(since) == 1 else 'are'}n't in it: run hylly {shop.id} list again to see them.")


def _may_claim(how_many: int) -> bool:
    """Claiming changes the account, so it goes ahead only on a yes from someone at the keyboard, or --yes."""
    nobody = ("Claiming changes your itch.io account, so it asks first, and nobody is here to answer. "
              "hylly itchio claim --yes claims without asking, once you have decided to.")
    if _confirm(f"Claim these {how_many}? They are added to your itch.io account, under itch.io's terms: yours to judge.", nobody):
        return True
    print("Nothing was claimed.")
    return False


def cmd_itchio(args) -> Ended | None:
    """itch.io: survey counts what there is and the space it would take; claim claims the bundles' items you want;
    download, on the shops' core, fetches the books, comics and tabletop games, and the other items whose files are all
    books, with the run's options told to its Library (a dry run keeps none of itch.io's answers)."""
    from . import itchio, itchio_survey

    root = paths.library_root(args.library)
    state = paths.state_dir(root)
    if args.action in ("download", "list"):  # the list is the download's code path (_shop_download), keeping nothing
        from . import curation, shoplist

        given = _given_list(args, "hylly itchio list") if args.action == "download" else None  # a LIST: its items alone,
        if given is not None:  # of every type, are asked about, as the list chooses
            _no_choosers(args)
        every = (*itchio.WANTED, "other", "game", "reading")
        kinds = every if given is not None else (args.types or itchio.DOWNLOADS)  # --types checked as it's read
        keys = {split[1] for split in map(shoplist.parts, given.of("itchio")) if split} if given is not None else None  # one
        # that can't be read is said by the run, as matching nothing
        wanted = _kinds(args.kind)  # --kind: of the answers aged, those of these kinds are asked again first (--limit)
        return _shop_download(args, "itchio", lambda key: itchio.Library(
            key, state=state, again=getattr(args, "again", False), dry_run=args.dry_run or args.action == "list",
            types=kinds, title=args.title, creator=args.by, limit=getattr(args, "limit", None), log=_note, kinds=wanted,
            yours=curation.kinds(root) if wanted else {}, keys=keys))  # its progress
    if args.action == "claim":
        groups = None if args.all else tuple(args.types or itchio_survey.CLAIMABLE)  # --types checked as it's read
        if args.every < 10:
            raise Exit(USAGE, "At least 10 seconds between claims, so claiming asks no more of itch.io's servers than a "
                             "person pressing the buttons would.")
        with itchio.Itch(load_session("itchio")) as itch, itchio_survey.Web(load_session("itchio-bundles")) as web:
            with itchio.stops_saying("What was claimed is kept"):
                done = itchio_survey.claim(itch, web, state, groups=groups, every=args.every, limit=args.limit,
                                           dry_run=args.dry_run, ask=None if args.yes else _may_claim)
        if done:
            print(f"Claimed {done}. hylly itchio survey now counts them with their exact sizes.")
        return
    if args.dry_run:  # offline: what a survey asks depends on what itch.io answers, so this says only what it would do
        last = state / itchio_survey.SURVEY
        made = datetime.fromtimestamp(last.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if last.is_file() else ""
        _report(dry_run=True, last_survey=made or None)
        return print("A survey reads your itch.io library and, with the login cookie, your bundles, and asks about "
                     + ("every item again" if args.again else "the items an earlier survey hasn't") + f"; it writes {last} "
                     f"and {state / itchio_survey.SURVEY_CSV}. " + (f"The last survey was made {made}." if made else "No survey "
                     "has been made yet.") + " Nothing was asked or written.")
    try:
        cookie = load_session("itchio-bundles")
    except AuthError:
        cookie = None
        _note("No itch.io login cookie saved, so the bundles are left out (hylly login itchio-bundles).")
    with itchio.Itch(load_session("itchio")) as itch, \
            (itchio_survey.Web(cookie) if cookie else contextlib.nullcontext()) as web:
        with itchio.stops_saying():
            items, bundles = itchio_survey.survey(itch, state, web, again=args.again, log=_note)  # its progress
    text = itchio_survey.report(items, shutil.disk_usage(root).free, bundles)
    atomic_write(state / itchio_survey.SURVEY, text)
    listing = itchio_survey.write_csv(items, state / itchio_survey.SURVEY_CSV)
    _report(items=len(items), bundles=len(bundles), report=text, saved=[str(state / itchio_survey.SURVEY), str(listing)])
    print(f"\n{text}\nSaved: {state / itchio_survey.SURVEY}\n       {listing} (every item but the software and the programs, to sort)")


def cmd_seed(args) -> Ended:
    """hylly humble seed: copies in the files of Humble's titles you have already, offline, from the latest snapshot of
    your orders: every file a download takes, at the place the shops' core gives it (shopping.select, by the plan:
    humble.titles_of), whether downloaded already, still to download or changed on Humble. What the never-list leaves out
    is named and never copied in, as the downloads leave it out (download.never), and so is a file you said you moved by
    hand (hylly decide: curation.answers), as the downloads never fetch it again, and one a shop list left out
    (curation.left_out), which no download takes."""
    from . import curation, humble, plan, shopping
    from . import download as dl

    root = paths.library_root(args.library)
    state = paths.state_dir(root)
    newest = None if args.snapshot else _latest_snapshot(state, ", or pass --snapshot")
    kinds = _kinds(args.kind)
    manifest = dl.Manifest(root, "humble")
    orders = snapshot.load(paths.Path(args.snapshot)) if args.snapshot else newest[1]
    sel = shopping.select("humble", humble.titles_of(plan.for_library(orders, root)), manifest, root,
                          kinds=kinds, answers=curation.answers(root), yours=curation.left_out(root))
    jobs = [shopping.make_job("humble", t, f, path) for t, f, path, _ in sel.every]  # no download takes the others, nor a seed
    if not args.folders:
        raise Exit(USAGE, "Which folders? hylly humble seed FOLDER ... (they are only read).")
    report = dl.seed([paths.Path(s) for s in args.folders], jobs, root, manifest, dry_run=args.dry_run)
    _report(dry_run=bool(args.dry_run), copied=len(report["copied"]), copied_bytes=sum(f.stat().st_size for f, _ in report["copied"]),
            already=len(report["already"]), other_version=[str(f) for f, _ in report["other_version"]],
            not_chosen=len(report["unknown"]), failed=[{"path": str(f), "error": str(error)} for f, error in report["failed"]],
            left_out=dl.left_out(sel.left))
    verb = "would copy" if args.dry_run else "copied"
    print(f"\n{verb}: {many(len(report['copied']), 'file')} ({size_words(sum(f.stat().st_size for f, _ in report['copied']))})   "
          f"already in the library: {len(report['already'])}")
    print(f"same name but another version: {len(report['other_version'])}   not chosen: {len(report['unknown'])}   failed: {len(report['failed'])}")
    for f, job in report["other_version"][:20]:
        print(f"  other version: {f}")
    for f, error in report["failed"]:
        print(f"  failed: {f}: {error}")
    for line in dl.left_out(sel.left):  # as the downloads name them
        print(line)
    ended = Ended(text=f"{many(len(report['copied']), 'file')} copied, {len(report['failed'])} failed.", failed=len(report["failed"]))
    if report["failed"]:
        raise ended.carried(Exit(PARTLY, f"{many(len(report['failed']), 'file')} couldn't be copied: see above."))
    return ended


def cmd_catalog(args) -> Ended | None:
    from . import catalog

    root = paths.library_root(args.library)
    cat = paths.catalog_dir(root)
    if args.dry_run:  # what a build reads depends on the files, which only the build itself reads
        said = _catalog_plan(root, args)
        _report(dry_run=True, plan=said, titles_last_build=_titles_kept_for(root))
        return print(said)
    if args.page:  # only the catalog's data.js for the server, from what the database has: nothing read or looked up
        paths.check_library(root)
        if not catalog.page(root):
            raise Exit(NOT_SET_UP, "The database has no catalog yet: hylly catalog builds one.")
        address, said = _page_shown(root)
        _report(made=str(cat / "data.js"), **({"page": address} if address else {}))
        print(f"Made again from the database: {cat / 'data.js'}.")
        return print(said)
    ended = Ended()  # what its question of a build with far fewer titles says of the run (_keep_fewer)
    with ended.going():
        with contextlib.ExitStack() as turn:
            if args.rebuild:  # read every file again, what they're read by set aside till the build is kept; lookups
                turn.enter_context(catalog.rebuilding(root))  # stay cached
            if args.lookup:  # it asks the sources first, so it waits its turn as hylly lookup does
                try:
                    turn.enter_context(runs.only_one("lookup", root))
                except runs.Busy as e:
                    _note(f"{e}\nBuilding the catalog without new lookups: it shows what has been found so far.")
                    args.lookup = False
            stats = catalog.build(root, lookup=args.lookup, lookup_limit=args.lookup_limit, workers=args.jobs,
                                  retry_covers=args.retry_covers, log=_built, fewer=_keep_fewer(args.yes, ended))
        if not args.rebuild:  # kept: what a rebuild that never ended set aside isn't needed now
            catalog.leftovers(root, _built)
        address, said = _page_shown(root)
        _report(**({"page": address} if address else {}), **(stats or {}))
        print(said)
        if stats and not stats.get("titles"):  # an empty library: how books come in
            print(_bring_books_in(root, "hylly catalog again"))
    return ended


def _page_shown(root) -> tuple[str, str]:
    """Where the library page shows the catalog, and hylly catalog's last line saying so. The page comes from the library
    server, which reads the catalog again as it changes: the address of the one running for this library, as other
    devices reach it, or this computer's; else "" and how to start one."""
    from . import instance, serve

    info = instance.running() or {}
    try:
        ours = bool(info.get("library")) and os.path.samefile(info["library"], root)
    except OSError:  # its library isn't there just now, or this one isn't
        ours = False
    if not ours:
        return "", "The library page comes from the library's server: run hylly serve and open the address it shows."
    listen, port = serve.listens(info) or "127.0.0.1", info.get("port", serve.PORT)
    address = (serve.other_devices(listen, port, info.get("address") or "") or serve.here(listen, port)).rstrip("/") + "/"
    return address, f"The library page shows it at {address}"


SUMMARY = re.compile(r"\s*[^\s].*: \d+ looked up, \d+ found|Found details for \d+ titles|\s*titles: \d+, |\s*\d+ new titles? in the library")


def _built(line: str) -> None:
    """A catalog build's lines: its counts and what came new are its result, on standard output; the rest is progress."""
    if SUMMARY.match(line):
        print(line, flush=True)
    else:
        _note(line)


def _catalog_plan(root, args) -> str:
    """hylly catalog --dry-run: what a build would do, from what the last one kept. Which files are new or changed only
    the build itself finds out, by reading the folders; this reads the database only, read-only, and claims nothing."""
    cat = paths.catalog_dir(root)
    if args.page:
        return f"It would make {cat / 'data.js'} again from the database, as the last build kept it. Nothing was written."
    paths.check_library(root)
    titles = _titles_kept_for(root)
    reads = ("every file again, with catalog/covers and the caches of the files' details set aside first, until the new "
             "build is kept" if args.rebuild else
             "every file the first time" if not titles else "the files new or changed since the last build"
             + (", and those recorded without a cover" if args.retry_covers else ""))
    return (f"A build would read {reads}, in {root}"
            + (", after asking the lookup sources about titles without details (hylly lookup --dry-run counts them)" if args.lookup else "")
            + f", and write the catalog in {cat}. " + (f"The last build has {many(titles, 'title')}." if titles else "There's no catalog yet.")
            + " Nothing was read or written.")


FEWER_NOTE = ("It had far fewer titles than the last one, so nothing was kept. If a disk or share was away, run hylly catalog "
              "again once it's back; if the titles are gone on purpose, hylly catalog --yes keeps the build.")


def _keep_fewer(yes: bool, ended: Ended):
    """hylly catalog's answer when a build has far fewer titles than the last one (catalog.shrunk): --yes keeps it, at the
    keyboard the person says, and with nobody there it isn't kept: the build says what to run, and a note says so, as a
    scheduled build's output is read by nobody. Either way hylly jobs shows the run that wasn't kept. ended: the run's,
    which says so."""
    def answer(said: str) -> bool:
        from . import bell

        if yes:
            _note(f"{said}\nKept all the same, as --yes says.")
            return True
        ended.ran = True  # a run that did its work, needing a yes: hylly jobs tells of it (_watched)
        if not at_keyboard():
            bell.job_note(f"{runs.JOBS['catalog'][0]} stopped", f"{said}\n{FEWER_NOTE}", FEWER_NOTE)
            ended.told = True
            return False
        _note(said)
        if _confirm("Keep this build all the same, without those titles?"):
            return True
        raise Exit(NEEDS_YOU, "Nothing was kept, and the catalog is as it was. If those titles are gone on purpose, "
                              "hylly catalog --yes keeps the build.")
    return answer


def cmd_lookup(args) -> None:
    """Asks the metadata sources about the catalog's titles that lack details; then hylly catalog shows them."""
    from . import bell, library, lookups

    if args.json and not args.dry_run:
        raise Exit(USAGE, "--json goes with --dry-run here: a run's own output is its lines, and hylly jobs --json tells how it ended.")
    kinds = _kinds(args.kind)  # before anything is said
    root = paths.library_root(args.library)
    chosen_ids = list(args.source) if args.source else None  # each a source there is: checked as it's read (commands)
    cat = library.Catalog(root)  # read once: the search below goes by the titles read here
    items = _catalog_items(cat)  # with your corrections: a title you fixed is asked about as you wrote it
    queries = [lookups.Query.from_record(it) for it in items]
    if args.search:
        wanted = {it["id"] for it in cat.search(args.search)}
        queries = [q for q in queries if q.id in wanted]
    chosen, skipped = lookups.available(chosen_ids)
    left_out = [why for why in skipped if why != lookups.OFF]  # lookups off is the settings' choice: said, and no failure
    if not chosen and left_out and not args.json:  # a dry run's JSON has them in left_out
        raise Exit(LOGIN if all(lookups.key_missing(why) for why in left_out) else NOT_SET_UP,  # keys only: the owner's login
                   "No source is left to ask, so nothing was looked up. "
                   + " ".join(why if why.endswith(".") else f"{why}." for why in left_out))
    for why in skipped:
        if not args.json:
            _note(why)
    if args.dry_run:  # a run's plan alone: it counts as a real run would ask, with no request and nothing written
        stats = lookups.dry_run(root, queries, chosen, again=args.again, kinds=kinds)
        if args.json:
            return _as_json({"sources": [{"id": src.id, "name": src.name, "left": stats[src.id]["left"], "waiting": stats[src.id]["waiting"],
                                          "home": src.home, "terms": src.terms, "verified": src.verified} for src in chosen],
                             "left_out": skipped})
        for src in chosen:
            st = stats[src.id]
            print(f"{src.name}: {st['left']} titles to look up"
                  + (f", {st['waiting']} more once the sources before it have answered." if st["waiting"] else "."))
            print(f"  {src.about()}")
        return
    state = paths.state_dir(root)
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / "lookup-log.txt"

    def log(line: str) -> None:
        if SUMMARY.match(line):  # how each source's turn ended, and what was found: the result
            print(line, flush=True)
        else:  # the rest, as progress: hylly logs lookup has them all
            _note(line)
        try:
            append_log(log_path, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", RUN_LOG_LIMIT, attempts=6)
        except OSError:
            pass  # the terminal has it
        runs.progress(line)

    try:
        stats = lookups.run(root, queries, chosen, limit=args.limit, again=args.again,
                            kinds=kinds, log=log, notify=bell.job_note)
    except lookups.LookupProblem as e:  # the one that ends a run: its answers can't be written
        raise Exit(LOCAL, str(e)) from None
    except KeyboardInterrupt:
        raise Exit(INTERRUPTED, "Stopped. Everything looked up so far is kept; running it again continues.") from None
    found = sum(st["found"] for st in stats.values())
    if found:
        log(f"Found details for {found} titles. Run hylly catalog to show them in the page and the feeds.")
    if any(st.get("changed") for st in stats.values()):
        raise Exit(NEEDS_UPDATE, "A source's answers have changed shape: see above.")
    if any(st.get("refused") for st in stats.values()):
        raise Exit(LOGIN, "A source refused its key or token: see above. Its owner runs hylly login with the source's name.")
    if any(st["stopped"] and not st.get("quota") for st in stats.values()):
        raise Exit(PARTLY, "A source stopped before it was done: see above. The next run goes on.")


def cmd_serve(args) -> None:
    """hylly serve: the library server, for a person: started here, with what was typed for this one run, or hidden with
    --background, with the settings; or the one running already said, and left as it is. With --service-file, nothing
    started: the walled service for this computer printed (_service_file)."""
    if getattr(args, "service_file", False):
        return _service_file(args)
    _start_server(args)


def _service_file(args) -> None:
    """hylly serve --service-file: the system-wide service with walls around the server, for this computer, from the
    settings (instance.service_file), on standard output as it is, so that it can go to a file; the commands that
    install it, with sudo, which are the owner's, on standard error. It takes the settings alone, as the server it
    starts does: what's typed for one run can't be given with it. Whether a server runs, or a walled one is installed
    already, changes nothing: it only prints."""
    from . import instance

    typed = [f"--{name}" for name in _typed(args)] + (["--background"] if args.background else [])
    if typed:
        raise Exit(USAGE, f"--service-file prints the service as the settings have it, which are the only options of the "
                          f"server it starts: {', '.join(typed)} can't be given with it. Set them in {paths.settings_file()} "
                          "(hylly settings edit) first. Nothing was printed.")
    text, steps, also = instance.service_file()
    if args.dry_run:
        return print(f"It would print the system-wide service with walls around the server, for this computer, with the "
                     f"commands that install it ({instance.WALLED_FILE}). Nothing is installed either way.")
    sys.stdout.write(text)
    sys.stdout.flush()
    _note("Nothing was installed, and nothing written but what every command makes, Hylly's own folders and secrets/, if "
          "they weren't there. The system runs the server from the file above, once it's in place. To put it there:\n" + "\n".join(f"  {step}" for step in steps) + f"\n{also}")


def cmd_server(args) -> None:
    """hylly server status, stop, restart and autostart on|off: the running server, and its start at logon; hylly server
    jobs, its periodic jobs run by a timer instead; and hylly server take-over, which keeps the library's records here."""
    from . import instance, serve

    if args.action == "jobs":
        return _server_jobs(args)
    if args.action == "take-over":
        return _take_over(args)
    if args.action == "status":
        return _server_status(args)
    if args.action == "stop":
        if args.dry_run:
            info = instance.running()
            return print("No library server is running." if info is None else
                         "It would stop this library server; nothing was stopped.\n" + serve.describe(info))
        return print("Stopped the library server." if instance.stop() is not None else "No library server is running.")
    if args.action == "autostart":
        return _autostart(args)
    _start_server(args, restart=True)


def _server_status(args) -> None:
    """hylly server status: the running server, from its note read once, as JSON or in words (serve.status), so that
    what it says and how it ends are the same look; PARTLY when none runs."""
    from . import instance, serve

    info = instance.running()
    if args.json:  # what keeps the library page from showing: "library", "catalog" or null (serve.missing)
        gap = {"missing": serve.missing(paths.Path(info["library"]))} if info and info.get("library") else {}
        _as_json({"running": info is not None, **(info or {}), **gap, "starts_at_logon": instance.autostart_state()})
    else:
        print(serve.status(info))
    if info is None:
        raise Exit(PARTLY)


def _take_over(args) -> None:
    """hylly server take-over: the library's own records kept in this computer's database from now on (db.take_over),
    once it has said what it takes over and from whom, and its owner has said yes."""
    from . import db

    root = paths.library_root(args.library)
    paths.check_library(root)
    plan, here, marker = db.taking_over(root), db.file(), db.owner_file(root)
    if plan["kept"]:
        token, had = db.token_file(), db._read_token(db.token_file())
        if had == "" and not args.dry_run:  # left so by a crash: made again by the owner's command, turn or none (db._token)
            with contextlib.suppress(OSError):
                db._token(db.database(), make=True, anew=True)
        db.own(db.database(), root)  # a marker from before tokens: tied now, as any command would, its token made if it must be
        kept = f"This computer's database, {here}, keeps the records of the library at {root} already."
        tied = None
        with contextlib.suppress(OSError, ValueError):
            tied = db.owner(root).get("token")
        if (plan["marker"] or {}).get("token") is not None or tied is None:  # tied before, or it can't be just now
            return print(f"{kept} Nothing changed.")
        return print(f"{kept} {marker} now names this install too, by the token in {token}"
                     + (", made again, as it held none." if had == "" else "."))
    mine, counts = plan["mine"], plan["counts"]
    print(f"From now on, the records of the library at {root} (your tags and corrections, the catalog, what was downloaded) "
          f"would be kept in this computer's database, {here}, and every other computer's would be refused them.")
    if mine is None:
        print("It has none of them yet, so they would start afresh here: hylly catalog builds the catalog again.")
    else:
        away = os.path.abspath(mine["root"]) != os.path.abspath(root)
        print(f"It has {many(counts['title'], 'title')} in its catalog, {many(counts['edit'], 'tag or correction', 'tags and corrections')}, "
              f"and {many(counts['download'], 'file')} downloaded on record" + (f", as the records of the library at {mine['root']}: "
              f"this says that {root} is that library, moved" if away else "") + ".")
    if plan["marker"] is None:
        print(f"Until now no database keeps them: {marker} isn't there, or can't be read.")
    else:
        whose = f"{'another copy of this same database' if plan['copy'] else 'the database'} on {plan['computer']}"
        print(f"Until now they are kept by {whose}. Stop the server there first (hylly server stop, there), so that nothing "
              f"changed there from now on is left behind. What it has and this one hasn't stays there: to bring it along, "
              f"put its latest nightly copy, in {db.backups(root)}, in place of {here} before this (the help's After the copy "
              f"says how).")
    if args.dry_run:
        return print("Nothing changed.")
    nobody = (f"Taking the library's records over asks first, and nobody is here to answer. Once its owner has decided, and "
              f"stopped the server that kept them: {db.take_over_command(root)} --yes. Nothing changed.")
    if not args.yes and not _confirm("Keep the library's records in this computer's database from now on?", nobody):
        return print("Nothing changed.")
    db.take_over(root)
    print(f"The library's records are kept in this computer's database from now on: {marker} names it. If the server runs "
          f"here, restart it: hylly server restart.")


def _typed(args) -> dict:
    """The server's options typed now, for this one run in the foreground; --library and --rules by their full paths."""
    rules = [str(paths.Path(p).absolute()) for p in args.rules] if getattr(args, "rules", None) else None
    library = str(paths.Path(args.library).absolute()) if getattr(args, "library", None) else None
    found = {"port": getattr(args, "port", None), "lan": getattr(args, "lan", None), "library": library, "rules": rules}
    return {k: v for k, v in found.items() if v is not None}


def _server_jobs(args) -> None:
    """hylly server jobs: the periodic jobs the server leaves to a timer with jobs = "external" under [server], each run
    if it's due (periodic.once); with --dry-run only which would. A job that failed makes it end with PARTLY, after what
    it said. With jobs as it comes, the server runs them itself: it runs nothing and ends well, so that a timer left on
    doesn't show as failed every minute, saying why only to a dry run and to someone at the keyboard; beside a server
    walled in, it runs the Inbox alone, and says so only when a pass started (periodic.once)."""
    from . import periodic

    found = periodic.once(paths.library_root(args.library), dry_run=args.dry_run)
    _report(**found)
    if found["jobs"] != "external" and not found["walled"]:
        if args.dry_run or at_keyboard():
            print(periodic.INTERNAL.format(settings=paths.settings_file()) + " Nothing was run.", file=sys.stderr)
        return
    named = ", ".join
    later = named(f"{name} in {time_words(max(seconds, 60))}" for name, seconds in
                  sorted(((n, s) for n, s in found["not_due"].items() if s is not None), key=lambda n: n[1]))
    if args.dry_run:
        print(f"Due, and would run now: {named(found['due'])}." if found["due"] else "Nothing is due now.")
        if later:
            print(f"Not due yet: {later}.")
        if found["jobs"] == "external":
            print(f"The server's own, which it runs itself either way: {named(found['server_only'])}.")
        else:  # a walled server's computer: only what its walls keep from it is this one's
            print(f"The server, walled in, runs the others itself, as jobs under [server] isn't \"external\": "
                  f"{named(found['server_only'])}.")
        return print("Nothing was run.")
    beside = found["jobs"] != "external"  # a walled server's computer: the Inbox alone, said only when it did something
    if not beside:
        print(f"Ran what was due: {named(found['ran'])}." if found["ran"] else "Nothing was due here.")
    if found["started"]:
        print(f"Started on its own, and not waited for: {named(found['started'])}.")
    if found["still_running"]:
        print(f"Still running from before, and left to it: {named(found['still_running'])}.")
    if later and not beside:
        print(f"Next: {later}.")
    if found["failed"]:
        raise Exit(PARTLY, f"Not all of them went well: {named(found['failed'])} failed, as said above. Each is tried again "
                           "when it's next due.")


def _start_server(args, restart: bool = False, autostart: bool = False) -> None:
    """hylly serve, server restart and, on Windows, server autostart on: the server to start, unless one runs already.
    Its settings are the server's only options: what's typed on hylly serve is for that one run, here in the
    foreground; nothing remembers it, and a server in the background, restarted or started at logon always takes the
    settings. A server running already is said, and nothing is restarted to match what was typed (hylly server restart
    does that, with the settings). hyllyd does the serving."""
    import webbrowser

    from . import access, instance, rules, serve

    typed = _typed(args)
    hidden = getattr(args, "background", False) or restart or autostart
    current = instance.running()
    if restart and current and instance.system_owns(current.get("pid")):
        if args.dry_run:
            return print("It would ask the system to restart this library server; nothing was stopped.\n" + serve.describe(current))
        instance.restart_through_system()
        for _ in range(150):
            time.sleep(0.2)
            now = instance.running()
            if now and now.get("pid") not in (None, current.get("pid")):
                return print("Restarted the library server.\n" + serve.describe(now))
        raise Exit(LOCAL, "The system was asked to restart the library server, and it hasn't come back after 30 seconds: "
                          "hylly server status says whether it runs, and hylly logs server what it said.")
    if instance.system_service() and not instance.in_system_service():  # walled: none may start outside the walls
        if current and not typed and not restart and not autostart:
            return print("The library server is already running.\n" + serve.describe(current))
        raise Exit(NOT_SET_UP, instance.WALLED)
    if hidden and typed:
        refused = (f"A server in the background takes its options from the settings, so that a restart or the next start at "
                   f"logon brings the same one back: --{', --'.join(typed)} can't be given with it. Set them under [server] "
                   f"in {paths.settings_file()} (hylly settings edit; library and rules at the top), or leave out "
                   "--background to run one here, in this window, with them. Nothing was started.")
        if not args.dry_run:  # which changes nothing, the log neither
            instance.write_log(refused)  # a start at logon from before has no window to say it in: hylly server status shows it
        raise Exit(USAGE, refused)
    port, listen = serve.options(typed)
    root = paths.library_root(typed.get("library")) if current is None or restart else None  # none to find for one running
    if args.dry_run:
        print(_server_plan(current, restart, hidden, root, port, listen,
                           instance.autostart_on(dry_run=True) if autostart else ""))
        if current is not None and typed:  # as the run would end: what was typed isn't what runs
            raise Exit(BUSY, _not_as_typed(current, typed))
        return
    if autostart:
        instance.autostart_on()
        print("The library server now starts in the background at every logon, with the settings.")
    if restart and current is not None:
        instance.stop()
        print("Stopped the running library server.", flush=True)
        current = None
    if current is not None:
        print("The library server is already running.\n" + serve.describe(current))
        if typed:  # the one running is left as it is: what was typed would need another one
            raise Exit(BUSY, _not_as_typed(current, typed))
        port, listen = current.get("port", port), serve.listens(current) or listen
    elif hidden:
        try:
            rules.sources()
        except rules.Missing as e:  # the hidden server starts all the same, and its log says it: here too
            print(f"{e} {serve.NOT_THERE_YET}", flush=True)
        print("Started the library server in the background.\n" + serve.describe(instance.start_background()))
        if note := serve.missing_line(root, False):  # it starts all the same: a share may come up later
            print(note)
    else:
        rules.EXTRA[:] = typed.get("rules") or []  # this run's own, read after the rules folder
        return serve.run(root, port=port, listen=listen, open_browser=not args.no_browser, given=typed)
    if not args.no_browser:  # the owner's way in, when there's one owner: at a terminal only, never into a file
        link, words = serve.owners_link(port, listen)
        if link:
            print(f"Logging you in as the owner, once, within {access.LINK_MINUTES} minutes: {link}")
        elif words:
            print(f"To log in: {words}.")
        if serve.a_desktop():  # a browser only where there is one to open, as the server's own start has it
            webbrowser.open(link or f"{serve.here(listen, port)}/")


def _not_as_typed(current: dict, typed: dict) -> str:
    """Why hylly serve with options ends with BUSY while a server runs: the one running is left as it is, and nothing is
    restarted for what was typed. A port, a library, where it listens (--lan) or rules files (--rules) it doesn't have
    are named, as a script that asked for one would otherwise take the server running for the one it asked for. Its
    rules files are those its own start was given (its note's given); one started with the settings alone has none."""
    from . import serve

    differs = []
    if "port" in typed and current.get("port") not in (None, typed["port"]):
        differs.append(f"on port {current['port']}, not {typed['port']}")
    if "library" in typed and current.get("library") and _same_folder(current["library"]) != _same_folder(typed["library"]):
        differs.append(f"for the library in {current['library']}, not {typed['library']}")
    listens, wanted = serve.listens(current), "0.0.0.0" if typed.get("lan") else "127.0.0.1"
    if "lan" in typed and listens and listens != wanted:
        differs.append(f"listening on {listens}, not {wanted}")
    theirs = [str(p) for p in (current.get("given") or {}).get("rules") or []]
    if "rules" in typed and [_same_folder(p) for p in theirs] != [_same_folder(p) for p in typed["rules"]]:
        differs.append(f"with {'the rules files ' + ', '.join(theirs) if theirs else 'no rules files of --rules'}, not "
                       f"{', '.join(typed['rules'])}")
    return (f"It runs {' and '.join(differs) or 'as it was started'}, and nothing was restarted for what you typed. To run "
            "one with that here: hylly server stop, then this again. To keep it: put it in the settings (hylly settings "
            "edit), then hylly server restart.")


def _server_plan(current: dict | None, restart: bool, hidden: bool, root, port: int, listen: str, logon: str) -> str:
    """What a dry run of hylly serve, server restart and server autostart on says it would do."""
    from . import serve

    lines = [logon] if logon else []
    with_what = f"on port {port}, listening on {listen}, for the library in {root}"
    if restart and current is not None:
        lines.append(f"It would stop the running library server, and start it again in the background with the settings, {with_what}.")
    elif current is not None:
        lines.append("The library server is already running, and it would be left as it is.\n" + serve.describe(current))
    else:
        lines.append(f"It would start the library server {'in the background' if hidden else 'here'}, {with_what}.")
    return "\n".join([*lines, "Nothing was started or stopped."])


def _autostart(args) -> None:
    """hylly server autostart on|off. On Windows the Task Scheduler task, set (and the server started now) or removed.
    On Linux and macOS the system runs the server itself: the systemd user unit or the launchd agent is printed,
    with this computer's paths, and the commands that install it or take it away; nothing is changed here."""
    from . import instance

    if instance.SYSTEM == "windows":
        if args.turn == "on":
            return _start_server(args, autostart=True)
        if args.dry_run:
            return print("It would remove the start at logon; nothing was changed." if instance.autostart_off(dry_run=True)
                         else "It is not set to start at logon.")
        if not instance.autostart_off():
            return print("It was not set to start at logon.")
        return print("Removed the start at logon. A server running now runs on: hylly server stop stops it.")
    if args.turn == "on":
        text, steps, also = instance.logon_start()
        if args.dry_run:
            return print(f"It would print the {instance.logon_kind()} for this computer, with the commands that install it. "
                         "Nothing is installed either way.")
        sys.stdout.write(text)
        sys.stdout.flush()
        return _note("Nothing was installed: the system runs the server, from the file above. To have it start at every "
                     "logon:\n" + "\n".join(f"  {step}" for step in steps) + (f"\n{also}" if also else ""))
    steps, also = instance.logon_stop()
    if args.dry_run:
        return print(f"It would print the commands that stop and remove the {instance.logon_kind()}. Nothing is changed either way.")
    print(f"Nothing was changed here. The {instance.logon_kind()} goes with these commands, which also stop the server it runs:\n"
          + "\n".join(f"  {step}" for step in steps) + (f"\n{also}" if also else ""))


NOT_A_NAME = "A user name is 1 to 64 letters, digits and . _ @ - (no spaces). Nothing changed."


def cmd_people(args) -> None:
    from . import access, db, people

    if args.action != "list" and not access.NAME.fullmatch(args.name):  # "" among them, which no one is
        raise Exit(USAGE, NOT_A_NAME)

    def line(p: dict) -> str:
        return (f"{p['name']:<24} {many(p['titles'], 'title')}, {p['listed']} on the reading list, "
                f"{many(p['bookmarks'], 'bookmark')}, since {p['since'][:10]}")

    try:
        aside = people.aside(None if args.action == "list" else args.name)
    except db.Newer as e:
        raise Exit(LOCAL, str(e)) from None
    if args.action == "list":
        try:
            found = people.everyone()
        except db.Newer as e:
            raise Exit(LOCAL, str(e)) from None
        if args.json:
            return _as_json({"people": found, "kept_aside": [{k: v for k, v in a.items() if k != "id"} for a in aside]})
        if not found and not aside:
            return print("Nothing is kept about anyone yet. It starts when a logged-in user reads in the library page.")
        for p in found:
            print(line(p))
        if aside:  # a removed user's, no longer theirs: apart, with when
            print(("\n" if found else "") + "Kept aside, from users removed (hylly user restore NAME brings one back with it, "
                  "hylly people erase NAME erases it for good):")
            for a in aside:
                print(f"{line(a)}, removed {a['removed'][:10]}")
        return print(f"In {db.file()}")
    person = people.Person(args.name)
    if args.action == "erase":
        return _erase_aside(args, aside, person.kept())
    if not person.kept():
        raise Exit(USAGE, f"Nothing is kept about {args.name}: hylly people list shows whom something is kept about."
                   + (f" What's kept aside of an earlier {args.name}, removed {aside[0]['removed'][:10]}, comes back with "
                      f"them by hylly user restore {args.name}, or goes for good by hylly people erase {args.name}." if aside else ""))
    if args.action == "export":
        return _as_json(person.export())
    if args.dry_run:
        kept = person.export()
        places, marked = len(kept["places"]), len({mark["id"] for mark in kept["finished"]})  # by title: the export has every change
        return print(f"It would forget all the places of {args.name}: {many(places, 'place')} and "
                     f"{many(marked, 'finished mark')}, which can't be brought back from here. Nothing changed.")
    if not args.yes and not _confirm(f"Forget all the places of {args.name}? They can't be brought back from here."):
        return print("Nothing changed.")
    count = person.forget_places()
    print(f"Forgot {args.name}'s places in {many(count, 'title')}. The nightly copies made before still have them: "
          f"each goes once {db.BACKUPS_KEPT} newer ones are there, or with the first good one made after it's {db.NIGHTS_DAYS} days old.")


def _erase_aside(args, aside: list[dict], theirs: bool) -> None:
    """hylly people erase NAME: what's kept aside of a removed user of that name, every row of it there is, erased for
    good, after saying what goes. Never what's kept about a user of that name now: that isn't kept aside. With no user
    of that name, the library's records that name them say access.FORMER after it, and the name is free (people.erase)."""
    from . import access, people

    if not aside:
        raise Exit(USAGE, f"Nothing is kept aside of {args.name}: hylly people list shows whom something is."
                   + (f" What's kept about the user {args.name} is theirs, and is kept aside once they're removed (hylly "
                      f"user remove {args.name}); hylly people forget {args.name} forgets their places now." if theirs else ""))

    def what(sets: list[dict]) -> str:
        return (f"what's kept aside of {args.name}: "
                + "; ".join(f"{people.kept_in_words(a['kept'])}, removed {a['removed'][:10]}" for a in sets))

    user = args.name in access.load_accounts()  # then the records that name them may be that user's, and stay
    named = 0 if user else access.named(args.name)
    records = (f" The library's records that name {args.name}, {named} of them (shelves for everyone, changes, the word on "
               f"reported titles, answers to questions), would say \"{access.FORMER}\" instead." if named else "")
    if args.dry_run:
        return print(f"It would erase for good {what(aside)}.{records}" + ("" if user else f" Then {args.name} is free for "
                     "a new user.") + " Nothing changed.")
    if not args.yes and not _confirm(f"Erase for good {what(aside)}?{records} It can't be brought back from here, by hylly "
                                     "user restore either."):
        return print("Nothing changed.")
    gone, unnamed = people.erase(args.name, [a["id"] for a in aside])
    went = [a for a in aside if a["id"] in gone]
    stayed = len(aside) - len(went)
    now = "hylly people list shows what's kept aside now."
    if not went:
        raise Exit(PARTLY, f"Nothing was erased: what was kept aside of {args.name} had changed meanwhile, brought back or "
                           f"erased already. {now}")
    print(f"Erased for good {what(went)}."
          + (f" The library's records that named {args.name}, {unnamed} of them, say \"{access.FORMER}\" now." if unnamed else "")
          + ("" if stayed or user else f" {args.name} is free for a new user.")
          + " The nightly copies made before still have it until they go, and so do the backup disk's zips.")
    if stayed:
        raise Exit(PARTLY, f"Not all of it: {stayed} of the {len(aside)} kept aside of {args.name} had changed meanwhile, "
                           f"brought back or erased already, and stayed as they were. {now}")


def cmd_reviews(args) -> None:
    """Stars and reviews: how many each person has given and how many wait for an owner, or a year's as Markdown."""
    import datetime as dt
    import pathlib

    from . import access, curation, library, people, reviews

    root = paths.library_root(args.library)
    items = _catalog_items(library.Catalog(root))
    titles = {i: it for it in items for i in curation.ids_of(it)}
    found = people.People().everyones_reviews(set(access.load_accounts()))
    if args.action == "export":
        year = str(args.year or dt.date.today().year)
        text = reviews.best_of(found, titles, year)
        if args.dry_run:
            where = f"write it to {args.out}" + (", over the file there" if pathlib.Path(args.out).exists() else "") if args.out else "print it"
            return print(f"The best of {year} would be {many(len(text.splitlines()), 'line')} of Markdown: it would {where}. Nothing was written.")
        if not args.out:
            return print(text, end="")
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
        return print(f"The best of {year} is in {args.out}.")
    rows = {name: (len(said), sum(r.get("state") in reviews.WORDS for r in said), sum(r.get("state") in (reviews.WAITING, reviews.EDIT_WAITING) for r in said))
            for name, said in found.items()}
    if args.json:
        return _as_json({"people": [{"name": n, "rated": a, "with_words": w, "waiting": q} for n, (a, w, q) in rows.items()]})
    if not rows:
        return print("Nobody has given stars yet. [reviews] who in settings.toml says who may (hylly settings show).")
    for name, (rated, words, waiting) in rows.items():
        print(f"{name:<24} {rated} rated, {words} with words" + (f", {waiting} waiting for an owner" if waiting else ""))
    total = sum(q for _, _, q in rows.values())
    print(f"{total} waiting: Reviews to check, in the library page's Manage menu." if total else "Nothing waits for an owner.")


def cmd_reports(args) -> None:
    """Reported titles: those waiting for an owner or a librarian, and those marked for a better copy, each with its files and
    what the shop's own record has of it: for a Humble title, every format and size in the orders saved at the last
    download. Nothing is asked of any shop."""
    from urllib.parse import unquote

    from . import access, curation, inventory, library, matching, people, reports
    from .storage import shown

    def one(text) -> str:
        """Outside text (a reader's comment, a title, a file's path, a shop's label) on one line of the listing: its
        line ends as " / ", as bell.log has an alarm's lines, and nothing else that could start a line (storage.shown),
        so no comment can make the listing show a report or a title that isn't there."""
        return shown(" / ".join(str(text).splitlines()))

    root = paths.library_root(args.library)
    items = _catalog_items(library.Catalog(root))
    index = {i: it for it in items for i in curation.ids_of(it)}
    titles = matching.Titles(items)  # the reports and the decisions on them by the id their title has now, as the page has them
    states = reports.states(people.People().everyones_reports(set(access.load_accounts()), titles),
                            reports.following(reports.Decisions().latest(), titles))
    orders = snapshot.latest(paths.state_dir(root), "humble")
    offered, saved = {}, ""
    if orders:
        products, _ = inventory.parse_orders(orders[1])
        for product in products:
            offered.setdefault(product.machine_name, product.books)
        saved = orders[0].stem[:10]  # its day, from its name
    found = []
    for title_id, s in states.items():
        it = index.get(title_id)
        if not it:
            continue
        have = [{"path": unquote(p), "format": ext.upper(), "size": size} for p, ext, size in it.get("f", [])]
        shop = next((offered[i[2:]] for i in curation.ids_of(it) if i.startswith("h:") and i[2:] in offered), None)
        found.append({"id": it["id"], "title": it.get("t", ""), "state": s["state"],
                      "decided": {k: s["decided"][k] for k in ("decision", "by", "when")} if s["decided"] else None,
                      "reports": [{"by": r["by"], "when": r.get("when", ""), "reasons": [reports.REASONS.get(k, k) for k in r["reasons"]], "comment": r.get("comment", "")}
                                  for r in s["reports"]],
                      "files": have,
                      "shop": None if shop is None else [{"label": f.label, "format": f.ext.upper(), "size": f.size,
                                                          "here": any(h["format"] == f.ext.upper() and h["size"] == f.size for h in have)} for f in shop]})
    found.sort(key=lambda e: (e["state"] != reports.WAITING, e["title"].lower()))
    if args.json:
        return _as_json({"orders_saved": saved, "titles": found})
    if not found:
        return print("No title is reported, and none is marked for a better copy.")
    waiting = [e for e in found if e["state"] == reports.WAITING]
    print(f"{len(waiting)} waiting for an owner or a librarian, {len(found) - len(waiting)} marked for a better copy.")
    for e in found:  # --json keeps each text whole, in a field of its own; here, each on one line (one)
        marked = e["decided"] and f"{one(e['decided']['by'])}, {one(e['decided']['when'][:10])}"
        state = "waiting" if e["state"] == reports.WAITING else f"better copy wanted (marked by {marked})"
        print(f"\n{one(e['title'])}: {state}")
        for f in e["files"]:
            print(f"  in the library: {one(f['path'])} ({one(f['format'])}, {size_words(f['size'])})")
        for r in e["reports"]:
            said = f'. "{one(r["comment"])}"' if r["comment"] else ""
            print(f"  {one(r['by'])}, {one(r['when'][:10])}: {one(', '.join(r['reasons']))}{said}")
        if e["shop"] is not None:
            listed = ", ".join(f"{one(f['label'])} {size_words(f['size'])}" + (" (this one)" if f["here"] else "")
                               for f in e["shop"])
            print(f"  Humble has, in the orders saved {saved}: {listed or 'no book files'}")
    if waiting:
        print("\nGo through them in the library page: Manage, Reported titles.")


def cmd_theme(args) -> None:
    """The library page's themes: those there are, which may be picked and which is the library's own; or one file checked."""
    import pathlib

    from . import settings, themes

    if args.action == "check":
        if not args.file:
            raise Exit(USAGE, "Which file? For example: hylly theme check my-theme.toml")
        try:
            theme = themes.check(pathlib.Path(args.file))
        except themes.NotATheme as e:
            raise Exit(USAGE, f"It can't be used: {e}") from None
        # an id another theme has already, as a copy of Hylly's has until it's given its own: where it goes, it would be
        # left out (available), unless it takes the place of that same file, or of one of yours of its name
        here = pathlib.Path(args.file)
        there = themes.available(told=lambda line: None).get(theme.id)
        hyllys = there is not None and there.path.parent == themes.SHIPPED
        if there is not None and there.path.resolve() != here.resolve() and (hyllys or there.path.name != here.name):
            raise Exit(USAGE, f"It can't be used: {themes.taken(here, theme.id, hyllys)}")
        lowest = int(themes.lowest_text(theme.colours) * 100) / 100  # on the tints too; cut down, never rounded up
        _report(name=theme.name, id=theme.id, usable=True, lowest_contrast=lowest, worth_a_look=list(theme.told),
                copy_to=str(themes.owners_dir()))
        for line in theme.told:
            print(f"  worth a look: {line}")
        return print(f"{theme.name} ({theme.id}) can be used: its text is at least {lowest:.2f}:1 on what it sits on, tints included, in both modes. "
                     f"Copy it to {themes.owners_dir()}, and the server has it within seconds.")
    said: list[str] = []
    found = themes.available(told=said.append)
    default, picked = themes.allowed(found, settings.get("look"), told=said.append)
    if args.json:
        return _as_json({"folder": str(themes.owners_dir()), "default": default, "said": said,
                         "themes": [{"id": t.id, "name": t.name, "author": t.author, "licence": t.licence, "about": t.about,
                                     "file": str(t.path), "shipped": t.path.parent == themes.SHIPPED, "picked": t in picked, "notes": t.told}
                                    for t in found.values()]})
    for t in found.values():
        marks = [m for m in ("the library's own" if t.id == default else "", "" if t in picked else "not to be picked ([look] themes)",
                             "" if t.path.parent == themes.SHIPPED else f"yours, {t.path.name}") if m]
        print(f"{t.id:<16} {t.name:<16} {t.about}" + (f"  ({'; '.join(marks)})" if marks else ""))
    for line in said:
        print(line)
    print(f"Your own go in {themes.owners_dir()}: hylly theme check FILE first.")


def _no_owner(accounts: dict) -> str:
    """What to say when there are users but none is an owner: then nobody has the library's management."""
    from . import access

    return ("" if not accounts or access.owners(accounts) else
            "No user is an owner, so nobody has the library's management: hylly user role NAME owner")


def _mention_app_copies() -> None:
    """On Windows, a packaged app's own copy of the settings folder (doctor.app_copies), said on standard error: what a
    program started from such an app writes, an AI assistant's hylly user add say, lands in that copy, and the library
    server outside it never sees it."""
    from . import doctor

    for copy in doctor.app_copies():
        _note(f"Note: an app keeps its own copy of this settings folder, {copy}. Users added from inside that app, by an AI "
              "assistant say, are there and not here, and the library server never sees them: add them in your own terminal.")


def cmd_user(args) -> None:
    from . import access, instance, people, serve

    if not getattr(args, "json", False):  # with --json, standard output has the result alone
        _mention_app_copies()
    if args.action == "list":
        return _user_list(args)
    if args.action == "restore" and not access.NAME.fullmatch(args.name):  # "" among them, which no one is
        raise Exit(USAGE, NOT_A_NAME)
    if args.dry_run:
        return print(_user_plan(args))
    if args.action == "restore":
        removed = access.restorable(args.name)  # before the first prompt: whether it can, and what comes back
        kept = people.kept_in_words(people.aside(args.name)[0]["kept"])
        role = args.role or ("reader" if access.load_accounts() else "owner")
        _note(f"Bringing back {args.name} ({role}), with what was kept aside of them, removed {removed[:10]}: {kept}. Their "
              "password went with them: this sets a new one.")
        access.restore_user(args.name, _password(args), args.role)
        role = access.load_accounts()[args.name][0]
        return print(f"Brought back {args.name} as {role}, with what was kept aside of them: {kept}. Give them the new "
                     f"password, or a one-time link: hylly user link {args.name}")
    if args.action == "link":
        if args.name not in access.load_accounts():
            raise Exit(USAGE, f"There is no user {args.name}. hylly user list shows who there is.")
        if not args.yes and not serve.in_a_terminal():  # into a pipe or a file, a log or an assistant's reading: kept
            raise Exit(NEEDS_YOU, f"A one-time link logs a browser in as {args.name}, so it is shown only at a terminal, and "
                                  "standard output here is a pipe or a file, which would keep it. Its owner runs hylly user "
                                  f"link {args.name} in a terminal, or with --yes once they have decided where the link goes. "
                                  "Nothing was made.")
        token = access.sign_in_link(args.name)
        port = (instance.running() or {}).get("port") or serve.options()[0]
        print(f"A one-time link that logs a browser in as {args.name}: it works once, within {access.LINK_MINUTES} minutes, "
              "and only while their password stays the same. At the address the browser reaches the library by:")
        return print("\n".join(f"  {link}" for link in serve.sign_in_links(token, port)))
    if args.action == "remove":
        if args.name not in access.load_accounts():
            raise Exit(USAGE, f"There is no user {args.name}. hylly user list shows who there is.")
        if not args.yes and not _confirm(f"Remove the user {args.name}? Their logins end at once."):
            return print("Nothing changed.")
        aside = access.remove_user(args.name) == "set aside"
        return print(f"Removed {args.name}." + (f" What was kept about them is kept aside in the database, no longer theirs: "
                                                f"hylly user restore {args.name} brings them back with it while nobody else "
                                                f"has the name, and hylly people erase {args.name} erases it for good."
                                                if aside else ""))
    if args.action == "rename":
        if not access.rename_user(args.name, args.new):  # all that's kept about them too, in the same change
            raise Exit(USAGE, f"There is no user {args.name}. hylly user list shows who there is.")
        return print(f"{args.name} is now {args.new}, with the same role and password, and all that's kept about them. "
                     "Their logins have ended, as those go by name: they log in again as the new name.")
    if args.action == "role":  # what a user may do, an owner's all of it: asked first, as only the library's owner decides
        accounts = access.load_accounts()
        if args.name not in accounts:
            raise Exit(USAGE, f"There is no user {args.name}. hylly user list shows who there is.")
        was = accounts[args.name][0]
        if was == args.new_role:
            return print(f"{args.name} is {was} already. Nothing changed.")
        nobody = (f"A user's role says what they may do, so changing it asks first, and nobody is here to answer: once the "
                  f"library's owner has said yes, hylly user role {args.name} {args.new_role} --yes. Nothing changed.")
        owner = " An owner may do anything, the library's management too." if args.new_role == "owner" else ""
        if not args.yes and not _confirm(f"Make {args.name} {args.new_role}, from {was}?{owner}", nobody):
            return print("Nothing changed.")
        access.set_role(args.name, args.new_role)
        return print(f"{args.name} is now {args.new_role}, from their next click.")
    accounts = access.load_accounts()  # before the first prompt: the name, and whether it's a new user or a new password
    if not access.NAME.fullmatch(args.name):
        raise Exit(USAGE, "A user name is 1 to 64 letters, digits and . _ @ - (no spaces). Nothing changed.")
    if args.name in accounts:
        was = accounts[args.name][0]
        print(f"{args.name} exists ({was}): this sets a new password for them, and ends their logins elsewhere"
              + (f", and makes them {args.role}" if args.role and args.role != was else "") + ".", file=sys.stderr)
    else:
        access.name_free(args.name)  # an earlier user of that name kept aside: refused, before a password is asked
        print(f"New user {args.name} ({args.role or ('reader' if accounts else 'owner')})" + ("" if accounts else ": the first "
              "user is the library's owner, unless --role says otherwise") + ".", file=sys.stderr)
    new = access.add_user(args.name, _password(args), args.role)
    accounts = access.load_accounts()
    role = accounts[args.name][0]
    first = new and len(accounts) == 1
    print(f"Added {args.name} as {role}" + (": the first user is the library's owner." if first and role == "owner" else ".")
          if new else f"Changed the password of {args.name} ({role}); their logins elsewhere have ended.")
    if note := _no_owner(accounts):
        print(note)


def _user_list(args) -> None:
    """hylly user list: the users and their roles, made once, then printed as JSON or as lines; the lines also say when
    the library has no owner, and who was removed with what was kept about them set aside."""
    from . import access, people

    accounts = access.load_accounts()
    users = [{"name": name, "role": role} for name, (role, _) in sorted(accounts.items())]
    if args.json:
        return _as_json({"users": users})
    print("\n".join(f"{u['name']:<24} {u['role']}" for u in users) if users
          else "No users yet, so nobody can use the library. Add one with: hylly user add NAME (the first is the owner)")
    if note := _no_owner(accounts):
        print(note)
    if aside := people.aside():  # a user removed by mistake: where to look first
        removed = ", ".join(f"{a['name']} ({a['removed'][:10]})" for a in aside)
        print(f"Removed, with what was kept about them kept aside: {removed}. hylly user restore NAME brings one back "
              "with it, and hylly people list says what's kept.")


def _password(args) -> str:
    """A new password for hylly user add or restore: once from standard input with --password-stdin, else typed twice,
    unseen."""
    if getattr(args, "password_stdin", False):  # once, from a pipe or a file: a script's, or a container's first user
        password = _piped_in("the password", "--password-stdin").removesuffix("\n").removesuffix("\r")
        if "\n" in password or "\r" in password:
            raise Exit(USAGE, "--password-stdin takes one line: the password, and nothing after it. Nothing changed.")
        if len(password) < 8:
            raise Exit(USAGE, "Use at least 8 characters. Nothing changed.")
        return password
    again = f"hylly user {args.action} {args.name}" + (f" --role {args.role}" if args.role else "")
    password = _hidden(f"Password for {args.name} (hidden as you type): ", again)
    if len(password) < 8:
        raise Exit(USAGE, "Use at least 8 characters.")
    if _hidden("The same again: ", again) != password:
        raise Exit(USAGE, "The two did not match; nothing changed.")
    return password


def _user_plan(args) -> str:
    """What hylly user add, restore, role, rename, remove or link would do: the dry run, which asks for no password."""
    from . import access, people

    accounts = access.load_accounts()
    name, there = args.name, args.name in accounts
    if args.action == "restore":
        removed = access.restorable(name)
        kept = people.kept_in_words(people.aside(name)[0]["kept"])
        ask = "read" if getattr(args, "password_stdin", False) else "ask for"  # standard input stays unread
        return (f"It would {ask} a new password for {name}, and bring them back as {args.role or ('reader' if accounts else 'owner')}, "
                f"with what was kept aside of them, removed {removed[:10]}: {kept}. Nothing changed.")
    if args.action != "add" and not there:
        raise Exit(USAGE, f"There is no user {name}. hylly user list shows who there is.")
    if args.action == "add":
        if not access.NAME.fullmatch(name):
            raise Exit(USAGE, "A user name is 1 to 64 letters, digits and . _ @ - (no spaces).")
        access.name_free(name)
        role = args.role or (accounts[name][0] if there else "owner" if not accounts else "reader")
        ask = "read" if getattr(args, "password_stdin", False) else "ask for"  # standard input stays unread
        said = (f"It would {ask} a new password for {name}, and end their logins elsewhere" if there else
                f"It would {ask} a password for {name}, and add them as {role}") + (f", as {role}" if there and args.role else "")
    elif args.action == "role":
        said = f"It would make {name} {args.new_role}, from {accounts[name][0]}"
    elif args.action == "rename":
        if args.new in accounts:
            raise Exit(USAGE, f"There is a user {args.new} already: give another name.")
        access.name_free(args.new, name)
        said = f"It would rename {name} to {args.new}, ending their logins"
    elif args.action == "remove":
        said = f"It would remove {name} ({accounts[name][0]}), and their logins would end at once"
    else:
        said = f"It would make a one-time link that logs a browser in as {name}, within {access.LINK_MINUTES} minutes"
    return f"{said}. Nothing changed."


def cmd_bell(args) -> None:
    from . import access, bell, settings

    conf = bell.settings()
    as_json = getattr(args, "json", False)  # bell show's
    where = settings.secret_from("ntfy-url")[1] or str(settings.secrets_dir() / "ntfy-url")  # never the topic itself
    if bell.weak_topic(conf) and not as_json:
        weak = "Your ntfy topic is short enough to guess, and whoever does can read your alarms."
        if sys.stdout.isatty() and sys.stderr.isatty():
            # a new one on a screen only: in a pipe, a log or an assistant's transcript it is known before it's set
            _note(f"{weak} A stronger one, to put in {where} in its place and subscribe to on your phone: "
                  f"https://ntfy.sh/{secrets.token_urlsafe(18)}")
        else:
            _note(f"{weak} Run hylly bell in a terminal, and it suggests a stronger one.")
    if args.action == "test" and args.dry_run:
        return print(f"It would send a test alarm to: {', '.join(bell.channels(conf))}. Nothing was sent.")
    if args.action == "test":
        watch = bell.Watch(conf)
        said = ("A test alarm", "From hylly bell test: if this reached you, alarms work.")
        watch._record("test", *said)  # every time: a test isn't held back as a second alarm would be
        problems = watch.push(*said, "high", public=said)
        print(f"Sent to: {', '.join(bell.channels(conf))}.")
        for p in problems:
            print(f"  But: {p}.")
        if any("notification on this computer" not in p for p in problems):  # a server has no screen, which is no failure
            raise Exit(PARTLY)
        return
    # what bell show shows, made once, then printed as JSON or as lines
    shown = {"channels": bell.channels(conf), "ntfy": bell.masked(conf["ntfy"]) if conf["ntfy"] else "",
             "ntfy_from": where if conf["ntfy"] else "", "weak_topic": bell.weak_topic(conf),
             **{k: v for k, v in conf.items() if k not in ("ntfy", "ntfy_token")}, "latest": access.tail(bell.log_file(), 10)}
    if as_json:
        return _as_json(shown)
    print(f"Alarms go to: {', '.join(shown['channels'])}  (settings: [bell] in {paths.settings_file()})")
    if shown["ntfy"]:
        print(f"  Phone: in the ntfy app, subscribe to the address in {where}, {shown['ntfy']}  (keep it to "
              "yourself: whoever knows it can read them)")
        print(f"  The phone gets: {'everything' if shown['remote_detail'] == 'full' else 'counts and kinds, no names, addresses or file names'}")
    else:
        print(f"  Phone: none. For alarms on your phone, put an ntfy address, https://ntfy.sh/ and a long random topic, in {where}")
    summary = f"at {shown['daily_summary']}:00" if shown["daily_summary"] >= 0 else "off"
    print(f"  Wrong passwords for a real user: {shown['wrong_passwords'] or 'off'} within 15 minutes; "
          f"downloads: {shown['downloads_per_hour'] or 'off'} files an hour; new devices: {'on' if shown['new_devices'] else 'off'}; "
          f"library out of reach: {str(shown['library_missing']) + ' minutes' if shown['library_missing'] else 'off'}; "
          f"the same again after {shown['quiet_minutes']} minutes; "
          f"at most {shown['alarms_per_hour']} sent out an hour; "
          f"daily summary: {summary}")
    print("Latest:\n  " + "\n  ".join(shown["latest"]) if shown["latest"] else "None yet.")


def cmd_tag(args) -> None:
    """hylly tag: the tags' words' suggestions (suggest), the tags or one tag's titles (list), or a tag added to the
    titles chosen, or removed (add, remove): the catalog's titles, with your corrections laid over them."""
    from . import library

    root = paths.library_root(args.library)
    cat = library.Catalog(root)
    items = _catalog_items(cat)
    if args.action == "suggest":
        return _tag_suggest(args, root, items)
    if args.action == "list":
        return _tag_list(args, items)
    _tag_change(args, root, cat, items)


def _title_line(item: dict) -> str:
    """A title in a list of them: its name, and its first two authors."""
    return f"    {item['t']}" + (f" - {', '.join(item['a'][:2])}" if item.get("a") else "")


def _tag_suggest(args, root, items: list[dict]) -> None:
    """hylly tag suggest: the titles the words of each tag match (curation/tags.toml in the library), kept for review on
    the library page; a dry run only counts them. What it found is made once, for its --json (_report) and its lines."""
    from collections import Counter

    from . import curation

    words = curation.rules_file(root)
    starts = not words.exists()  # a run starts it with examples, which a dry run leaves unwritten
    if args.dry_run:
        found = [] if starts else curation.suggest(root, items)
    else:
        found = curation.find_suggestions(root, items)
    by_tag = Counter(t["tag"] for s in found for t in s["tags"]).most_common()
    total = sum(n for _, n in by_tag)
    _report(dry_run=bool(args.dry_run), words_in=str(words), titles=len(found), suggestions=total, by_tag=dict(by_tag),
            found=[{"title": s["t"], "tags": [t["tag"] for t in s["tags"]]} for s in found])
    counts = f"{many(total, 'suggestion')} for {many(len(found), 'title')}"
    tags = ", ".join(f"{t} {n}" for t, n in by_tag)
    print(f"Words that suggest each tag: {words}" + (" (not there yet: a run starts it with examples)" if args.dry_run and starts else ""))
    if args.dry_run:
        return print(counts + (f": {tags}" if tags else "") + ". As a dry run, the ones waiting for review in the library "
                     "page stay as they are.")
    if not found:
        return print("No suggestions: every match is decided already, or no tag has words that match.")
    print(f"{counts}: {tags}")
    _list_some([f"    {s['t']}: " + "; ".join(f"{t['tag']} ({t['why']})" for t in s["tags"]) for s in found], most=15)
    print("Review them on the library page: Suggestions (on this computer, with the server running).")


def _tag_list(args, items: list[dict]) -> None:
    """hylly tag list [TAG]: every tag with how many titles have it, or the titles with TAG, however it's written; made
    once, then printed as JSON or as lines."""
    from collections import Counter

    if args.tag:
        key = args.tag.casefold()
        tagged = [i for i in items if key in (t.casefold() for t in i.get("tg", []))]
        if args.json:
            return _as_json({"tag": args.tag, "titles": [{"id": i["id"], "title": i["t"], "authors": i.get("a", [])}
                                                         for i in tagged]})
        return print(f"{many(len(tagged), 'title')} tagged {args.tag}:\n" + "\n".join(_title_line(i) for i in tagged))
    counts = Counter(t for i in items for t in i.get("tg", [])).most_common()
    if args.json:
        return _as_json({"tags": [{"tag": t, "titles": n} for t, n in counts]})
    print("\n".join(f"{n:5}  {t}" for t, n in counts) or "No tags yet. Add one with: hylly tag add TAG --search WORDS")


def _tag_change(args, root, cat, items: list[dict]) -> None:
    """hylly tag add or remove TAG: on the titles chosen (_choose) that haven't it, or have it, shown first, and asked
    unless --yes; a dry run only shows them."""
    from . import curation

    adding = args.action == "add"
    tag = curation.tag_name(args.tag or "")
    picked = _choose(cat, items, args, needed=adding)
    has = lambda i: tag.casefold() in (t.casefold() for t in i.get("tg", []))  # noqa: E731
    todo = [i for i in picked if has(i) != adding]
    if not todo:
        return print(f"Nothing to do: {many(len(picked), 'title')} chosen, and " +
                     ("all of them have the tag already." if adding else "none of them has the tag."))
    already = len(picked) - len(todo)
    _preview(todo, f" ({already} chosen {'has' if already == 1 else 'have'} it already)" if adding and already else "")
    if args.dry_run:
        return
    these = "this title" if len(todo) == 1 else f"these {len(todo)} titles"
    if not args.yes and not _confirm(f"{'Add' if adding else 'Remove'} the tag '{tag}' {'to' if adding else 'from'} {these}?"):
        return print("Nothing changed.")
    done = curation.change(root, args.action, tag, todo, by=curation.COMMAND_LINE)
    print(f"{'Added' if adding else 'Removed'} '{tag}' {'to' if adding else 'from'} {many(len(done), 'title')}. "
          "The library page and the reading apps have it at once; reload the page if it's open.")


def _choose(cat, items: list[dict], args, needed: bool = True) -> list[dict]:
    """The titles --search, --bundle, --folder and --id choose (together: all of them must hold)."""
    from urllib.parse import unquote

    from . import curation

    if needed and not (args.search or args.bundle or args.folder or args.id):
        raise Exit(USAGE, "Choose the titles with --search, --bundle, --folder or --id (several narrow it down).")
    picked = cat.search(args.search) if args.search else list(items)
    if args.bundle:
        picked = [i for i in picked if args.bundle.casefold() in (i.get("b") or "").casefold()]
    if args.folder:
        part = args.folder.replace("\\", "/").casefold()
        picked = [i for i in picked if any(part in unquote(f[0]).casefold() for f in i.get("f", []))]
    if args.id:
        picked = [i for i in picked if set(curation.ids_of(i)) & set(args.id)]
    if not (args.search or args.bundle or args.folder or args.id):
        return picked
    known = {i for it in items for i in curation.ids_of(it)}
    unknown = [i for i in args.id or [] if i not in known]
    if not picked:  # a mistyped id or an empty search: not "all of them have it already", and no success
        given = [f'--{name} "{value}"' for name, value in (("search", args.search), ("bundle", args.bundle), ("folder", args.folder))
                 if value] + ([f"--id {' '.join(args.id)}"] if args.id else [])
        raise Exit(USAGE, f"No title matches {' and '.join(given)}"
                          + (f": the catalog has no title with the id {', '.join(unknown)}" if unknown else "")
                          + ". Nothing changed. hylly tag list, or a search in the library page, shows what there is.")
    if unknown:
        print(f"Note: the catalog has no title with the id {', '.join(unknown)}: left out.", file=sys.stderr)
    return picked


def _preview(todo: list[dict], note: str = "") -> None:
    from collections import Counter
    from urllib.parse import unquote

    groups = Counter(i.get("b") or unquote(i["f"][0][0]).rsplit("/", 1)[0] for i in todo)
    print(f"{many(len(todo), 'title')}{note}, from " + "; ".join(f"{g} ({n})" for g, n in groups.most_common(5)) + (" ..." if len(groups) > 5 else ""))
    print("\n".join(_title_line(i) for i in todo))


def cmd_set(args) -> None:
    from . import curation, library

    root = paths.library_root(args.library)
    cat = library.Catalog(root)
    items = cat.get() if args.id else _catalog_items(cat)  # by its id, a shop's title not downloaded yet: no catalog needed
    if args.field in ("copies", "refused"):
        return _set_answers(root, cat, items, args)
    if args.field == curation.LEFT:
        return _set_left_out(root, cat, items, args)
    text, answered = args.field in curation.TEXT, args.field in curation.ANSWERED
    value = args.value if (text or args.field == "file") and args.value.lower() != "auto" else args.value.lower()
    if text and value != "auto":
        value = curation.text_value(args.field, value)  # checked before anything is chosen
    elif args.field == "file" and value != "auto":
        value = curation.answer_value("file", value)  # a file's name, as its shop lists it
    elif args.field == "answers" and value != "auto":
        raise Exit(USAGE, "answers takes auto alone, which takes back your answers to the title's questions: hylly "
                          "decide gives them.")
    elif not text and not answered and value not in (*curation.single(args.field), "auto"):
        raise Exit(USAGE, f"{args.field} is one of: {', '.join((*curation.single(args.field), 'auto'))}")
    picked = _choose(cat, items + _uncatalogued(root, items, args.id), args)  # a title not downloaded yet, by its id
    if args.field == "format" and value != "auto":
        picked = _offering(root, picked, value)
    if args.field == "file" and value != "auto":
        picked = _offering(root, picked, value, by_name=True)
    edits = curation.load(root)
    want = None if value == "auto" else value
    todo = [i for i in picked if (edits.in_effect(curation.ids_of(i), args.field) if want is not None  # cleared: by
                                  else edits.fixes_of(curation.ids_of(i)).get(args.field)) != want]  # the record itself
    what = {"kind": "the kind", "ereader": "the e-reader feed", "format": "the format to keep", "file": "the file to keep",
            "answers": "your answers to its questions"}.get(args.field, f"the {args.field}")
    to = "automatic" if want is None else (", ".join(want) if isinstance(want, list) else want) or "none"
    if not todo:
        return print(f"Nothing to do: {many(len(picked), 'title')} chosen, and {what} is {to} for all of them already.")
    _preview(todo, f" ({len(picked) - len(todo)} chosen are {to} already)" if len(picked) > len(todo) else "")
    if args.dry_run:
        return
    if not args.yes and not _confirm(f"Set {what} to {to} for {'this title' if len(todo) == 1 else f'these {len(todo):,} titles'}?"):
        return print("Nothing changed.")
    done, _ = curation.change_single(root, args.field, value, todo, by=curation.COMMAND_LINE)
    if args.field in ("format", "file") and want is None:  # the order under [download] decides again, but for what is there
        return print(f"Set {what} to {to} for {many(len(done), 'title')}: one not downloaded yet goes by the order under [download] "
                     "in the settings, and one downloaded keeps the format it has. hylly humble download fetches what's new.")
    if args.field in ("format", "file"):
        return print(f"Set {what} to {to} for {many(len(done), 'title')}. Now run hylly humble download to fetch it, then hylly catalog; "
                     "the files already there stay in their folders.")
    if args.field == "answers":  # taken back: the next run asks again, if the rules are still unsure
        return print(f"Took back {what} for {many(len(done), 'title')}: the next download asks again where its rules are still "
                     "unsure (hylly decide).")
    print(f"Set {what} to {to} for {many(len(done), 'title')}. The library page and the reading apps have it at once; "
          "reload the page if it's open.")
    if args.field == "kind":  # the downloads go by it too, and where a title is read follows at the next build
        print("Where each is best read follows at the next hylly catalog. A shop's title not downloaded yet is "
              "downloaded as that kind; one downloaded keeps the files it has (hylly set format moves one).")
    if args.field == "ereader" and want == "always":
        from .library import ereader_feed

        feed = ereader_feed()
        unlisted = sum(not feed.suits({**i, "ereader": "always"}) for i in todo)
        if unlisted:
            print(f"{many(unlisted, 'title')} still won't be listed, having no file the feed can offer. A PDF counts only from "
                  "the sources under pdf_from in [ereader_feed] in the settings.")


def _purchase_groups(root) -> dict[str, list[str]]:
    """Each Humble title's keys, by every one of them: its own and its other purchases' machine names (a title bought
    again, which may lead under another since), as the plan groups them from the newest snapshot: built once."""
    from . import humble, plan

    source = snapshot.latest(paths.state_dir(root), "humble")
    try:
        found = humble.titles_of(plan.for_library(source[1], root)) if source else []
    except (OSError, ValueError):
        found = []
    groups = {}
    for t in found:
        every = [k for k in [t.key, *t.also] if k not in ("", None)]
        groups.update({k: every for k in every})
    return groups


def _set_left_out(root, cat, items: list[dict], args) -> None:
    """hylly set left-out auto: the files a shop list left out taken back, so a download fetches them again (and a new
    list shows them ticked). --id takes a list row's id (humble:lanternwolf_book:Lantern Wolf.epub: that file alone) or
    a title's catalog id (h:lanternwolf_book: all its files left out), a title never downloaded too; --search, --bundle
    and --folder take the catalog's titles, all their files. Each under every key its title is known by (its other
    purchases' too), as the downloads read them under any of them (curation.leave_out: one edit, undone exactly)."""
    from argparse import Namespace

    from . import curation, shoplist

    if args.value.strip().lower() != "auto":
        raise Exit(USAGE, "left-out takes auto alone, which takes back the files a shop list left out: a list's unticked "
                          "rows give them (hylly SHOP download LIST).")
    by_letter = {shop.letter: shop for shop in shops.SHOPS.values()}
    wanted: dict[str, set[str] | None] = {}  # catalog id: the file keys to take back, None for all of them
    groups: dict[str, list[str]] | None = None  # Humble's titles' keys, by each of them: the plan built once

    def title(letter: str, key: str, files: set[str] | None) -> None:
        nonlocal groups
        shop = by_letter.get(letter)
        if shop is not None and shop.id == "humble" and groups is None:
            groups = _purchase_groups(root)
        for k in (groups or {}).get(key, [key]) if shop is not None and shop.id == "humble" else [key]:
            cid = f"{letter}:{k}"
            if files is None or wanted.get(cid, set()) is None:
                wanted[cid] = None
            else:
                wanted.setdefault(cid, set()).update(files)

    rows = [i for i in args.id or [] if (shoplist.parts(i) or ("",))[0] in shops.SHOPS]
    for given in args.id or []:
        split = shoplist.parts(given) if given in rows else None
        if split:  # a list row's id: that file
            title(shops.SHOPS[split[0]].letter, split[1], {split[2]})
        elif ":" in given:
            letter, _, key = given.partition(":")
            title(letter, key, None)
    if args.search or args.bundle or args.folder:
        for rec in _choose(cat, items, Namespace(search=args.search, bundle=args.bundle, folder=args.folder, id=None)):
            for i in curation.ids_of(rec):
                if ":" in i:
                    title(*i.split(":", 1), None)
    if not wanted:
        raise Exit(USAGE, "Choose the files with --id (a list row's id, or a title's catalog id), or the titles with "
                          "--search, --bundle or --folder. Nothing changed.")
    kept = curation.left_out(root)
    changes = {cid: (cid, {key: None for key in sorted(kept[cid]) if files is None or key in files})
               for cid, files in wanted.items() if kept.get(cid)}
    changes = {cid: change for cid, change in changes.items() if change[1]}
    if not changes:
        return print("Nothing to do: none of the files chosen is left out by a shop list.")
    n = sum(len(files) for _, files in changes.values())
    print(f"{many(n, 'file')} left out with a shop list, of {many(len(changes), 'title')}: " + ", ".join(
        f"{cid} ({', '.join(files)})" for cid, (_, files) in changes.items()))
    if args.dry_run:
        return print(f"It would take {'it' if n == 1 else 'them'} back, so that a download fetches "
                     f"{'it' if n == 1 else 'them'} again. Nothing changed.")
    if not args.yes and not _confirm(f"Take back {'this file' if n == 1 else f'these {n:,} files'} left out, so a download fetches "
                                     f"{'it' if n == 1 else 'them'} again?"):
        return print("Nothing changed.")
    curation.leave_out(root, changes, by=curation.COMMAND_LINE)
    print(f"Took back the files left out with a shop list for {many(len(changes), 'title')}: the next download fetches them, and "
          "a new list shows them ticked.")


def _set_answers(root, cat, items: list[dict], args) -> None:
    """hylly set copies (apart, or auto) and hylly set refused (a lookup source, or auto): your answers about the chosen
    cards, without a question (curation.set_copies, curation.set_refused), which the next hylly catalog builds by."""
    from . import curation

    value = args.value.strip().lower()
    if args.field == "copies" and value not in ("apart", "auto"):
        raise Exit(USAGE, "copies is apart (a card's copies are two books or more) or auto (as the rules have them).")
    if args.field == "refused":
        curation.set_refused(root, [], value, curation.COMMAND_LINE)  # the source checked before anything is chosen
    picked = _choose(cat, items, args)
    todo = [i for i in picked if len(curation.ids_of(i)) > 1] if (args.field, value) == ("copies", "apart") else picked
    if not todo:
        return print(f"Nothing to do: {many(len(picked), 'title')} chosen, and none of them is a card of copies in several places.")
    what = ("Keep the copies apart, as books of their own," if value == "apart" else
            "Let the rules decide again which copies are one book," if args.field == "copies" else
            "Take back your refusals of lookups' matches" if value == "auto" else
            f"Refuse {curation.source_name(curation.lookup_source(value) or value)}'s match")
    _preview(todo)
    if args.dry_run:
        return
    if not args.yes and not _confirm(f"{what} for {'this title' if len(todo) == 1 else f'these {len(todo):,} titles'}?"):
        return print("Nothing changed.")
    change = curation.set_copies if args.field == "copies" else curation.set_refused
    done, _ = change(root, todo, value, curation.COMMAND_LINE)
    if not done:
        return print(f"Nothing to do: it is so for all {len(todo)} already.")
    print(f"Done for {many(len(done), 'title')}: the next hylly catalog builds the catalog so"
          + (", and the sources refused aren't asked about them again." if args.field == "refused" and value != "auto" else "."))


def _question_lines(q: dict) -> list[str]:
    """A question as hylly decide says it: its number, where and what, why, and its options by number, the guess
    marked; an answered one's answer marked instead."""
    from . import curation

    shop = (shops.SHOPS[q["shop"]].label if q["shop"] in shops.SHOPS else "The catalog" if q["shop"] == curation.CATALOG
            else q["shop"])
    about = ", ".join(x for x in (q["catalog_id"], q["file"]) if x)
    lines = [f"{q['id']:>4}  {shop}: {q['title']}" + (f" ({about})" if about else "") + f". {q['ask']}"
             + (f" ({q['why']})" if q["why"] else "")]
    given = (q.get("answer") or {}).get("option")
    for n, o in enumerate(q["options"], 1):
        said = (f"{o['file']} ({str(o.get('format') or '?').upper()}, {size_words(o.get('size', 0))}"
                + (f", {o['label']}" if o.get("label") and o["label"].lower() != str(o.get("format")).lower() else "")
                + ")") if "file" in o else o.get("word", "")
        if given:
            mark = f"  <- your answer ({q['answer'].get('by', '')}, {str(q['answer'].get('at', ''))[:10]})" if n == given else ""
        else:
            mark = "" if n - 1 != q["guess"] else "  <- the guess, held back for your answer" if q["held"] else "  <- the guess, taken"
        lines.append(f"        {n}  {said}{mark}")
    return lines


def _from_where(found: list[dict]) -> str:
    """Where questions came from, in words: the downloads, the catalog, or both."""
    from . import curation

    places = [w for w, there in (("the downloads", any(q["shop"] != curation.CATALOG for q in found)),
                                 ("the catalog", any(q["shop"] == curation.CATALOG for q in found))) if there]
    return " and ".join(places)


def cmd_decide(args) -> None:
    """hylly decide: the questions the downloads' and the catalog's rules couldn't settle by themselves
    (curation.questions), and the answer to one by its number, as the server page gives it (curation.answer): an ordinary
    edit, which settles it. auto takes one answer back (curation.take_back), and --answered lists those answered, whose
    answers stand. The quiet kind (a lookup's near match) is left out of the list but for a line counting it, and listed
    by --kind match: --kind lists any one kind. --offered: what the question offered as the answer was made (its offered
    in --json), refused when it offers others now; without it, the answer goes by what the question offers now, and says
    what it did in so many words."""
    from . import curation

    root = paths.library_root(args.library)
    answered = bool(getattr(args, "answered", False))
    kind = getattr(args, "kind", None)
    if args.number is not None:
        said = str(args.number)
        if not said.isdecimal() or len(said) > 19 or not 0 < int(said) < curation.NUMBER_MOST:
            raise Exit(USAGE, f"{said[:20]}{'...' if len(said) > 20 else ''}? A question's number, as hylly decide lists them.")
        args.number = int(said)
    if args.number is None or args.choice is None:
        kinds = {kind} if kind else set(curation.QUESTIONS) if args.number is not None else None  # by number: any kind
        found = [q for q in curation.questions(root, answered=answered, kinds=kinds)
                 if args.number is None or q["id"] == args.number]
        quiet = 0 if kind or answered or args.number is not None else curation.quiet_count(root)
        if args.number is not None and not found:
            raise Exit(USAGE, f"No question {args.number} is {'answered' if answered else 'open'}: hylly decide"
                              f"{' --answered' if answered else ''} lists those that are.")
        if args.json:
            return _as_json({"questions": found, **({"answered": True} if answered else {}),
                             **({"near_matches": quiet} if quiet else {})})
        if not found:
            print("No answered questions: hylly decide lists those open." if answered else
                  "No questions to decide: the rules were sure of everything, or you have answered.")
            if quiet:
                print(curation.near_line(quiet, after=False) + ".")
            return
        if args.number is None:
            print(f"{many(len(found), 'question')} " + ("answered" if answered else "to decide")
                  + f", from {_from_where(found)}:")
        for q in found:
            for line in _question_lines(q):
                print(line)
        if answered:
            return print("hylly decide NUMBER auto takes one answer back: the next run or hylly catalog asks again where "
                         "the rules are still unsure.")
        print("hylly decide NUMBER CHOICE answers one: CHOICE is an option's number, a file's name among them, or keep for "
              "the guess. hylly decide --answered lists those answered.")
        if quiet:
            print(curation.near_line(quiet, after=True) + ".")
        return
    done = curation.answer(root, args.number, args.choice, curation.COMMAND_LINE, dry_run=args.dry_run, offered=args.offered)
    q, o = done["question"], done["option"]
    if args.json:
        return _as_json({"dry_run": bool(args.dry_run), "question": q, "answer": o, "field": done["field"], "id": done["id"],
                         "did": done["did"]})
    about = f" ({done['id']})" if done["id"] else ""
    if o.get("answer") == "auto" and "file" not in o:  # taken back
        return print(f"{'It would take back' if args.dry_run else 'Took back'} your answer to question {q['id']}, "
                     f"{q['title']}{about}: the next run or hylly catalog asks again where the rules are still unsure"
                     + (". Nothing changed: a dry run." if args.dry_run else "."))
    said = o.get("file") or o.get("word", "")
    print(f"{'It would answer' if args.dry_run else 'Answered'} question {q['id']}, {q['title']}{about}, with {said}, "
          f"which {done['did']}" + (". Nothing changed: a dry run." if args.dry_run else f" (hylly decide {q['id']} auto "
                                                                                       "takes it back)."))


def _offering(root, picked: list[dict], ext: str, by_name: bool = False) -> list[dict]:
    """The chosen titles Humble offers in this format, or with by_name a file of this name (hylly set file: any of an
    item's files, an extra too, which makes an item with no book of its own a title); the others are named and left
    out. The other shops download every file a title has, so there is none to choose."""
    from . import plan
    from .inventory import parse_orders

    _, orders = _latest_snapshot(paths.state_dir(root))
    offered: dict[str, set[str]] = {}
    for p in (parse_orders(orders)[0] if by_name else []):
        offered.setdefault(f"h:{p.machine_name}", set()).update(f.filename for f in p.files)
    for t in ([] if by_name else plan.build(orders).titles):
        offered[f"h:{t.product.machine_name}"] = {f.ext for f in t.product.books}
    ok = [r for r in picked if ext in offered.get(r["id"], set())]
    left = [r.get("t", "") for r in picked if r not in ok]
    if left:
        what = f"a file named {ext!r}" if by_name else f"as {ext.upper()}"
        _note(f"Left out: {many(len(left), 'title')} Humble doesn't offer {what} (or that aren't from Humble): "
              + "; ".join(left[:8]) + ("; …" if len(left) > 8 else ""))
    return ok


def _uncatalogued(root, items: list[dict], ids: list[str] | None) -> list[dict]:
    """The titles --id names that the catalog hasn't got (none of their files downloaded yet) but a question names
    (hylly decide), or a shop's newest snapshot lists, as bare records: a correction may name a shop's title by its
    catalog id before its download, as an answer to a question does."""
    from . import curation
    from .inventory import parse_orders

    known = {i for it in items for i in curation.ids_of(it)}
    wanted = [i for i in dict.fromkeys(ids or []) if i not in known]
    if not wanted:
        return []
    named = {q["catalog_id"]: q["title"] for q in curation.questions(root)}
    for shop in shops.SHOPS.values():
        if not any(i.startswith(f"{shop.letter}:") and i not in named for i in wanted):
            continue
        source = snapshot.latest(paths.state_dir(root), shop.id)
        data = source[1] if source else []
        listed = ({p.machine_name: p.title for p in parse_orders(data)[0]} if isinstance(data, dict)  # orders (Humble's)
                  else {str(e.get("id")): str(e.get("name") or "") for e in data if isinstance(e, dict)})
        named.update({f"{shop.letter}:{key}": name for key, name in listed.items() if f"{shop.letter}:{key}" not in named})
    shop_of = {shop.letter: shop.name for shop in shops.SHOPS.values()}  # what the list of titles chosen says they're from
    return [{"id": i, "t": named[i], "f": [[""]], "b": f"{shop_of.get(i.split(':', 1)[0], '')} (not in the catalog yet)"}
            for i in wanted if i in named]


def _without_records(root) -> int | None:
    """How many files in your own folders the last catalog build listed that verify can't check, having no record of
    them: the ones you put there yourself, as only hylly add's copies are recorded with their size and MD5. None before
    the first build."""
    import unicodedata

    from . import folders, manifest

    # in any capitals, as the copies' records are told apart, and either Unicode form: macOS may spell a name decomposed
    same = lambda path: unicodedata.normalize("NFC", path).lower()  # noqa: E731
    with manifest.store(root).read() as c:
        if c.execute("SELECT count(*) FROM catalog_build").fetchone()[0] == 0:
            return None
        listed = {row[0] for row in c.execute("SELECT path FROM title_file")}
        copied = {same(e["dest"]) for e in manifest._copies(c) if isinstance(e.get("dest"), str)}
    return sum(1 for path in listed if "/" in path and folders.is_own(path.split("/", 1)[0]) and same(path) not in copied)


def _own_books(root) -> bool:
    """Whether a folder of your own has a book file, as a build reads them (catalog.own_folders, book_files): past the
    systems' and the tools' folders, such as $RECYCLE.BIN at a disk's root, and an empty folder has none. Walked only
    before the first build, which counts them from then on. What can't be read, folders or rules, can't tell: True, as
    the line this decides on is true anyway."""
    from . import catalog, rules

    try:
        books = rules.current().book_suffixes
        return any(catalog.book_files(root / own.name, books, lambda folder: False, []) for own in catalog.own_folders(root))
    except (OSError, SystemExit):  # a folder that can't be listed, a mistake in the rules or the settings
        return True


def cmd_verify(args) -> None:
    from . import copies, manifest, removed
    from . import download as dl

    root = paths.library_root(args.library)
    problems, checked = [], 0
    missing: dict[str, tuple[str, dict]] = {}  # the files missing, each with its shop ("" for a copy) and its record
    progress = _progress if args.hash and not args.json else None
    for shop in shops.SHOPS:  # the one list of the shops, so a shop added there is checked too
        downloaded = dl.Manifest(root, shop)
        if not downloaded.entries:
            continue
        found = dl.verify(downloaded, root, check_hash=args.hash, progress=progress)
        missing.update({path: (shop, downloaded.entries[path]) for path, problem in found
                        if problem == "missing" and path in downloaded.entries})
        problems += found
        checked += sum(1 for e in downloaded.entries.values() if not e.get("gone"))  # as dl.verify checks them
        if progress:
            _end_progress()
    n, bad = copies.verify(root, check_hash=args.hash, progress=progress)  # the copies in your own folders
    copied = manifest.copies(root) if any(problem == "missing" for _, problem in bad) else {}
    missing.update({path: ("", copied.get(path.lower(), {})) for path, problem in bad if problem == "missing"})
    checked, problems = checked + n, problems + bad
    if progress and n:
        _end_progress()
    yours = removed.by_you(root, missing, check_hash=args.hash)  # removed on the Server page, or by hand: not missing
    problems = [(path, problem) for path, problem in problems if not (problem == "missing" and path in yours)]
    unchecked = _without_records(root)
    if args.json:
        _as_json({"checked": checked, "with_md5": args.hash, "problems": [{"path": str(path), "problem": problem} for path, problem in problems],
                  "removed": len(yours), "no_record": unchecked})
        if problems:
            raise Exit(PARTLY)
        return
    print(f"Checked {many(checked, 'file')}{' with MD5' if args.hash else ''}: {many(len(problems), 'problem')}.")
    if yours:
        print(f"{many(len(yours), 'file')} you removed (in {removed.FOLDER}, emptied from it, or moved by hand as you "
              f"answered) {'is' if len(yours) == 1 else 'are'} no problem.")
    if unchecked:  # so that 0 problems isn't taken for a clean bill of health of files never read
        print(f"{many(unchecked, 'file')} in your own folders {'has' if unchecked == 1 else 'have'} no record to "
              "check against: verify checks downloaded and copied files.")
    elif unchecked is None and _own_books(root):
        print("Files you put in your own folders yourself have no record to check against: verify checks downloaded and "
              "copied files (after hylly catalog, it says how many).")
    for path, problem in problems[:50]:
        print(f"  {problem}: {path}")
    if problems:
        raise Exit(PARTLY)


def cmd_add(args) -> None:
    """Books into one of your own folders, from the folder [folders.<name>] copy_from names, by its list or all of them.
    With --inbox, a pass over the Inboxes ([inbox]) instead, as the server's job makes them (_inbox_pass): a person
    typing it is told so first, and asked before the first such pass removes anything."""
    from . import copies, folders

    root = paths.library_root(args.library)
    if args.inbox:
        if args.folder or args.source or args.list:
            raise Exit(USAGE, "hylly add --inbox takes no folder, --from or --list: each of the Inbox's books goes to "
                              "the folder of yours its kind belongs in, as into under [inbox] says.")
        return _add_from_the_inbox(args, root)
    if not args.folder:
        raise Exit(USAGE, "Into which folder? hylly add FOLDER: your folder in the library, such as ebooks. hylly add "
                          "--inbox makes a pass over the Inbox.")
    if not folders.is_own(args.folder):
        raise Exit(USAGE, f"{args.folder} isn't a name for a folder of your own: one plain name, not a shop's folder or "
                         "Hylly's own, and not a path.")
    own = folders.folder(args.folder)
    source, list_file = args.source or own.copy_from, args.list or own.list
    if not source:
        raise Exit(NOT_SET_UP, f"Where from? Set copy_from under [folders.{args.folder}] in {paths.settings_file()} "
                         "(see settings.example.toml), or pass --from. For the Inbox: hylly add --inbox.")
    passed: list[str] = []
    gone: list[copies.Op] = []  # listed books whose copy and original are both gone: said, and no failure
    copied = copies.add_books(root, own.name, paths.Path(source), paths.Path(list_file) if list_file else None,
                              own.strip_suffix, dry_run=args.dry_run, passed=passed, gone=gone)
    _report(dry_run=bool(args.dry_run), into=str(root / own.name), source=str(source), passed_over=passed,
            gone=[op.dest for op in gone], **{"would_copy" if args.dry_run else "copied": copied})
    if copied and not args.dry_run:  # a book whose name another book's copy has ends the run in NAME TAKEN instead, every time
        print("Done. Run 'hylly catalog --lookup' to add them to the catalog. Before removing any originals, run "
              f"'hylly add {own.name}' again: once it has nothing left to copy, run 'hylly verify --hash', which reads every copy again.")


def _add_from_the_inbox(args, root) -> None:
    """hylly add --inbox: said first, then a pass over the Inboxes, by hand (_inbox_pass). An Inbox's folder gone is
    LOCAL, as a source that isn't there is for hylly add; other trouble that left books in the Inbox, PARTLY."""
    from . import folders

    found = folders.inboxes(root=root)
    if not found:
        raise Exit(NOT_SET_UP, f"No Inbox is set: folder = '...' under [inbox] in {paths.settings_file()} names one "
                               "(see settings.example.toml).")
    names, lookup = ", ".join(inbox.folder for inbox in found), found[0].lookup
    _note(f"The Inbox's books ({names}) are copied into the library, each into the folder of yours its kind belongs in "
          f"({found[0].where()}), each copy checked, {'looked up, ' if lookup else ''}catalogued, and then removed from "
          f"there; what can't be taken goes into its {folders.NOT_ADDED} folder, with a note why. "
          + ("This only says what it would do." if args.dry_run else "This is a pass, as the server's job makes one a minute."))
    done = _inbox_pass(root, found, dry_run=args.dry_run, by_hand=True, yes=args.yes)
    _report(dry_run=bool(args.dry_run), inboxes=[inbox.folder for inbox in found], inbox=True,
            **{k: v for k, v in done.items() if k != "trouble"})
    if done["missing"]:
        raise Exit(LOCAL, f"Can't read the Inbox, {', '.join(done['missing'])}: it isn't there. Is the disk there, and "
                          "is the path right? Nothing was copied from it.")
    if done["trouble"] and not args.dry_run:
        ended = Exit(PARTLY, " ".join(done["trouble"]))
        ended.shown = True  # said already, as the pass went: main doesn't say it again, and its --json object has it
        raise ended


INBOX_NOTE = "Books from the Inbox"  # the title of the phone's note after a pass
PATIENCE = 60.0  # seconds an Inbox's lookup waits for a source's budget at most; longer, and it asks at a later pass
LOOKUP_DAYS = 7  # days a book's lookup that had to wait is asked about again by the passes; hylly lookup asks it after that
SHOWN = 10  # books named in a phone note's full text, and then how many more
DONE_KEPT = 2 * 86400.0  # seconds what each pass did is kept, for the daily summary


def _patient(seconds: float) -> None:
    """The Inbox's lookups' wait between requests: one longer than PATIENCE (an hour's budget spent, a source asking to
    slow down) ends that source's turn as a spent budget does, and its books are asked about at a later pass, so a pass
    never holds the Inbox for long."""
    from . import lookups

    if seconds > PATIENCE:
        raise lookups.Quota(f"its budget has no room for {time_words(seconds)} now; the Inbox asks again at a later pass")
    time.sleep(seconds)


def _listed_by_size(root):
    """The places in the library of the files the catalog lists with a size (copies.look): for a book in the Inbox that
    no recorded copy has the size and MD5 of, the files in your own folders are looked at too. As the last build kept
    it in the database; one that can't be read lists nothing."""
    import sqlite3

    from . import db, manifest

    def listed(size: int) -> list[str]:
        try:
            with manifest.store(root).read() as c:
                return [row[0] for row in c.execute("SELECT path FROM title_file WHERE size = ? ORDER BY path", (size,))]
        except (OSError, SystemExit, sqlite3.Error, db.Newer, db.Unusable):
            return []

    return listed


def _catalogued(root, dests) -> dict[str, dict | None]:
    """The catalog's record of the title with a file at each of these places in the library, by place, in any capitals;
    None where it lists none. As the last build kept it in the database; one that can't be read lists nothing, and a
    build is then tried, which says why."""
    from . import manifest

    found: dict[str, dict | None] = {dest: None for dest in dests}
    if not found:
        return found
    try:
        with manifest.store(root).read() as c:
            for dest in found:
                row = c.execute("SELECT t.body FROM title_file f JOIN title t ON t.id = f.title WHERE f.path = ? COLLATE "
                                "NOCASE LIMIT 1", (dest,)).fetchone()
                found[dest] = json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001 - any trouble with the database: the build that's tried then says what it is
        return {dest: None for dest in found}
    return found


def _unopened(record: dict, dest: str) -> str:
    """Why no reader can open a book's copy in the library, as the catalog's build found it (meta.cant_open), or "".
    One locked by the shop that sold it is a whole book, which goes as any other."""
    from urllib.parse import unquote

    from . import meta

    for path, why in (record.get("bad") or {}).items():
        if isinstance(path, str) and unquote(path).lower() == dest.lower() and why == meta.DAMAGED:
            return "what every reader needs is missing from it"
    return ""


def _waiting_questions(root, due: set[str], pending: dict) -> tuple[list, dict[str, list[str]]]:
    """The questions about the books whose lookups are due, as a build would ask them, from the catalog's records of
    them (lookups.Query.from_record), and their places by title. A place the catalog no longer lists, and the library no
    longer has, is forgotten."""
    from . import lookups

    questions: dict[str, object] = {}
    ids: dict[str, list[str]] = {}
    for dest, record in _catalogued(root, sorted(due)).items():
        if record is None:
            if not (root / dest).exists():
                pending.pop(dest, None)
            continue
        ids.setdefault(record["id"], []).append(dest)
        questions.setdefault(record["id"], lookups.Query.from_record(record))
    return list(questions.values()), ids


# what a pass says once of a book in an Inbox, or of a folder there, by the Inbox's key and its path in it
BOOK_SAID = ("aside", "remove", "unfinished", "unread", "unopened", "library", "differs", "no tag")


def _given_words(book, kind: bool = True) -> str:
    """What a book is given besides its place, for a dry run's line: " (kind RPG book, tagged To read)"; "" for
    nothing. kind: False for a book the library has already, whose card gets its tags alone."""
    said = [f"kind {_kind_word(book.fix)}"] if book.fix and kind else []
    said += [f"tagged {', '.join(book.tags)}"] if book.tags else []
    return f" ({', '.join(said)})" if said else ""


def _just_copied(root, book) -> dict:
    """A book a pass has just copied in, as an edit names its title before the build that catalogues it: by the id that
    build gives the title (catalog.folder_title_id), its file's name for its title, and its place."""
    from urllib.parse import quote

    from . import catalog

    return {"id": catalog.folder_title_id(root, book.dest), "t": paths.Path(book.dest).stem, "f": [[quote(book.dest)]]}


def _give(root, given: list[tuple[dict, list[str], str]], note) -> str:
    """Titles given what their books' folders in the Inbox say (folders.placed), each as (the title, as an edit names
    it, its tags, the kind to keep or ""): the tags its # folders name, and the kind a kind's folder named, where the
    folder of yours it went to would give it another, as your own edits give them (curation.change,
    curation.change_single), by the Inbox (curation.INBOX): the library page lists them in Recent changes, where each
    can be undone, and a tag goes as any other. A book just copied in by the id the build that catalogues it gives its
    title (_just_copied), so that the build goes by the kind already; one the library had, by the card it is in. One
    edit for each tag, and for each kind, and a card once in each, whatever number of its files came. Said as it goes
    (note); what kept them from being given, in words, or "": the books are in the library all the same."""
    from . import curation, db

    tags: dict[str, tuple[str, dict[str, dict]]] = {}
    kinds: dict[str, dict[str, dict]] = {}
    for title, named, fix in given:
        for tag in named:
            tags.setdefault(tag.casefold(), (tag, {}))[1].setdefault(title["id"], title)
        if fix:
            kinds.setdefault(fix, {}).setdefault(title["id"], title)
    try:
        for tag, titles in tags.values():
            done = curation.change(root, "add", tag, list(titles.values()), by=curation.INBOX)
            if done:
                note(f"  tagged {tag}: {many(len(done), 'title')}")
        for kind, titles in kinds.items():
            done, _ = curation.change_single(root, "kind", kind, list(titles.values()), by=curation.INBOX)
            if done:
                note(f"  kind {_kind_word(kind)}, as its folder in the Inbox says: {many(len(done), 'title')}")
    except (OSError, SystemExit, db.NotHere, db.Newer, db.Unusable) as e:
        why = getattr(e, "message", None) or getattr(e, "strerror", None) or e
        return (f"The Inbox's books are in the library, but the tags and kinds their folders there name couldn't be "
                f"kept ({why}): give them by hand, with hylly tag and hylly set kind, or in the library page.")
    return ""


def _kind_word(kind: str) -> str:
    """A kind as a reason says it, its label within a sentence (rules.Kind.word): "RPG book", "comic"."""
    from . import rules

    found = rules.current().kinds.get(kind)
    return found.word if found else kind


def _inbox_pass(root, inboxes: list, *, dry_run: bool = False, by_hand: bool = False, yes: bool = False) -> dict:
    """One pass over the Inboxes ([inbox], folders.inboxes): what each holds is looked at (copies.look), each book
    placed in the folder of yours its kind belongs in, as its folders in the Inbox say (folders.placed); and then, in
    this order, what can't be taken is set aside in the Inbox's Not added folder with a note why; each book taken is
    copied in as hylly add copies, checked and recorded; it is given the tags its # folders name, and the kind its
    folder there gives it where the folder of yours it went to would give it another, as your own edits are (_give);
    the lookup sources are asked about the new books only, unless lookup = false under [inbox], within each source's
    budget (one out of budget or offline asks again at a later pass, and the book shows meanwhile with what its file
    says); the catalog is built; and only then are the originals removed from the Inbox, each once it and its copy, both
    read again, are the same, with the folders that leaves empty. A book the library has already goes from the Inbox at
    once, once the catalog lists it; one whose copy the catalog can't open stays, and is set aside at the next pass if
    it's still so.
    While lookups only wait, the catalog is built again only once a source has found something. The library, by any
    spelling of it (copies.Guard), and anything outside the Inbox, through a link or a junction, are never touched; a
    full disk makes the books wait.

    Said: each line in the Inbox's log (hylly logs inbox); what the pass did on standard output, which the server's job
    puts in the server's log; and a phone note for a pass that took books, as a long job's note, in counts. Trouble
    with an Inbox's folder or the library is said once, until it's put right (the state's said), but by hand, where a
    person reads it. dry_run: what a pass would do, and nothing is written. by_hand: hylly add typed by a person, which
    asks before the first pass that removes anything from an Inbox (yes says yes for them), and takes a book whose time
    is two minutes old without a second look. What it found and did, for the result: the places of the books added, and
    for how many details were found; those already in the library; those set aside, with why; those left for later; the
    Inboxes that weren't there; and the trouble said."""
    from . import bell, catalog, copies, curation, folders, lookups

    now, state = time.time(), folders.read_state(root)
    logged = folders.log_file(root)
    result: dict = {"added": [], "details_found": None, "already": [], "set_aside": [], "later": [], "missing": [], "trouble": []}

    def write(line: str) -> None:
        if not dry_run:
            with contextlib.suppress(OSError):  # the output has it
                append_log(logged, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", RUN_LOG_LIMIT, attempts=6)

    def note(line: str) -> None:  # on the way: progress
        _note(line)
        write(line)

    def tell(line: str) -> None:  # what the pass did: its result
        print(line, flush=True)
        write(line)

    def once(key: str, words: str, trouble: bool = True) -> None:
        if trouble:
            result["trouble"].append(words)
        if by_hand or dry_run or state["said"].get(key) != words:
            tell(words)
        if not dry_run:
            state["said"][key] = words

    guard = copies.Guard(root)  # the library, by any spelling of it: never touched
    found = copies.look(root, inboxes, state["seen"], now, by_hand, guard, _listed_by_size(root))
    for inbox in inboxes:
        if inbox in found.missing:
            result["missing"].append(inbox.folder)
            once(folders.missing_said(inbox.key), f"The Inbox, {inbox.folder}, isn't there. Make the folder, or connect "
                 "the disk or share it is on: Hylly makes nothing there itself, as an unmounted disk's place must stay "
                 "empty. Each pass looks again.")
        else:
            state["said"].pop(folders.missing_said(inbox.key), None)
        if inbox in found.overlap:
            once(f"overlap|{inbox.key}", f"The Inbox, {inbox.folder}, and the library, {root}, overlap: one is the other "
                 "or inside it. Told by their paths with links followed, by the folders' identities on the disk (\\\\?\\ "
                 "before a path, a link or a junction, a bind mount of a library's folder), or by a file Hylly wrote for "
                 "a moment in one and found in the other (a second share name or mount of the same folder). Nothing is "
                 "taken from it, nor removed, until [inbox] names a folder outside the library.")
        else:
            state["said"].pop(f"overlap|{inbox.key}", None)
        unprobed = next((why for other, why in found.unprobed if other is inbox), "")
        if unprobed:
            once(f"unprobed|{inbox.key}", f"Nothing is taken from the Inbox, {inbox.folder}, this time: {unprobed}. "
                 "Each pass tries again.")
        else:
            state["said"].pop(f"unprobed|{inbox.key}", None)
        if inbox in found.nested:
            once(f"nested|{inbox.key}", f"The Inbox, {inbox.folder}, and another Inbox overlap: one is inside the "
                 "other, so a book in it would be taken twice. Neither is taken from until [inbox] names folders apart.")
        else:
            state["said"].pop(f"nested|{inbox.key}", None)
    if found.unreadable:
        n = len(found.unreadable)
        once("unreadable", f"Passed over {many(n, 'folder')} in the Inbox it isn't allowed to read: "
             + ", ".join(found.unreadable[:5]) + (f" and {n - 5} more" if n > 5 else "") + ".", trouble=False)
    else:
        state["said"].pop("unreadable", None)
    for key, folder in found.no_tags:
        once(f"no tag|{key}|{folder}", f"{paths.Path(key) / folder} names no tag a book can have (one is 1 to "
             f"{curation.TAG_MAX} letters after the {folders.TAG_MARK}): its books are taken in without it.",
             trouble=False)
    result["later"] = [book.shown for book in found.later + found.unfinished]
    for book in found.unfinished:
        once(f"unfinished|{book.inbox}|{book.rel}", f"{book.shown} ends in zeros, as a copy that stopped half way does: "
             "it stays in the Inbox until it is whole, and is taken then.", trouble=False)
    unread = {id(book): why for book, why in found.unread}
    for book, why in found.unread:
        once(f"unread|{book.inbox}|{book.rel}", f"{book.shown} can't be read ({why}): it stays in the Inbox, and each "
             "pass looks again. The user Hylly runs as must be able to read and change the files in the Inbox.",
             trouble=False)
    if dry_run:
        for book in found.take:
            tell(f"  would copy into the library as {book.dest}{_given_words(book)}, then remove from the Inbox: "
                 f"{book.shown}")
        for book, dest in found.already:
            tell(f"  would remove from the Inbox, as the library has it already as {dest}"
                 f"{_given_words(book, kind=False)}: {book.shown}")
        for book, kind, details in found.aside:
            tell(f"  would set aside in {folders.NOT_ADDED}: {book.shown}: {copies.aside_why(kind, details)}")
        for book in found.later:
            why = (f"it can't be read ({unread[id(book)]})" if id(book) in unread else
                   "it is still coming in or hasn't stayed the same for two minutes yet")
            tell(f"  left for a later pass, as {why}: {book.shown}")
        for book in found.unfinished:
            tell(f"  left for a later pass, as its end is all zeros, as a copy that stopped half way has it: "
                 f"{book.shown}")
        tell(f"A pass would take {many(len(found.take), 'book')} into the library, remove "
             f"{len(found.already)} the library has already, set aside {len(found.aside)} and leave "
             f"{len(found.later) + len(found.unfinished)} for later"
             + (", looking the new ones up and building the catalog before any original goes" if found.take else "")
             + ". Nothing was changed.")
        result.update(added=[book.dest for book in found.take], already=[dest for _, dest in found.already],
                      set_aside=[{"file": book.shown, "why": copies.aside_why(kind, details)}
                                 for book, kind, details in found.aside])
        return result

    if by_hand:  # the first pass by hand that would remove or move anything from an Inbox asks first
        new = [inbox for inbox in inboxes if inbox not in found.missing and inbox not in found.overlap
               and inbox not in found.nested and all(other is not inbox for other, _ in found.unprobed)
               and state["agreed"].get(inbox.key) != inbox.folder]
        if new and (found.take or found.already or found.aside):
            words = (f"This removes the books from {', '.join(inbox.folder for inbox in new)} once the library has "
                     f"them, and moves what can't be taken into its {folders.NOT_ADDED} folder.")
            if yes:
                _note(f"{words} Going on, as --yes says.")
            else:
                nobody = (f"{words} So the first time it asks, and nobody is here to answer: once its owner has said yes, the "
                          "same command with --yes goes ahead. Nothing was changed.")
                if not _confirm(f"{words} Go ahead?", nobody):
                    raise Exit(NEEDS_YOU, "Nothing was changed. hylly add with --yes goes ahead without asking.")
            for inbox in new:
                state["agreed"][inbox.key] = inbox.folder

    taken, gone_books = state["taken"], set()  # taken: the books copied in whose originals are still to go
    tagged = state["tagged"]  # the originals whose folders' tags and kind were given, by Inbox and path: size and MD5

    def given_before(book) -> bool:
        """Whether this very original, by its path, size and MD5, has had its tags and kind given by a pass before: one
        that stays in the Inbox (its removal failing) never gives back a tag its owner has taken off since."""
        was = tagged.get(book.inbox, {}).get(book.rel)
        return isinstance(was, dict) and bool(book.md5) and (was.get("size"), was.get("md5")) == (book.size, book.md5)

    def remember(books) -> None:
        for book in books:
            if book.md5:
                tagged.setdefault(book.inbox, {})[book.rel] = {"size": book.size, "md5": book.md5}

    def aside(book, kind: str, details: str) -> None:
        why = copies.aside_why(kind, details)
        try:
            copies.set_aside(book, kind, details)
        except OSError as e:
            once(f"aside|{book.inbox}|{book.rel}", f"{book.shown} can't be taken ({why.rstrip('.')}), and couldn't be "
                 f"moved into {folders.NOT_ADDED} either ({e.strerror or e}): it stays where it is.")
            return
        gone_books.add((book.inbox, book.rel))
        result["set_aside"].append({"file": book.shown, "why": why})
        tell(f"{book.shown} set aside in {folders.NOT_ADDED}: {why}")

    for book, kind, details in found.aside:
        aside(book, kind, details)
    copied, failed, trouble = copies.take_in(root, found.take, log=note) if found.take else ([], [], "")
    for book, kind, details in failed:
        aside(book, kind, details)
    if trouble:
        once("trouble|copy", trouble)
    else:
        state["said"].pop("trouble|copy", None)
    records = copies.recorded(root) if copied else {}
    for book, _ in copied:  # the MD5 its copy was recorded with: what it was, as it was read
        book.md5 = book.md5 or str((records.get(book.dest.lower()) or {}).get("md5") or "")
    fresh = [book for book, how in copied if how == "copied" and (book.tags or book.fix) and not given_before(book)]
    not_given = _give(root, [(_just_copied(root, book), book.tags, book.fix) for book in fresh], note)
    if not not_given:
        remember(fresh)
    for book, how in copied:
        if how == "copied":  # one already there, the same, was the library's before: no new book
            taken[book.dest] = {"since": now, "found": False, "lookup": book.lookup}
            if book.lookup:
                state["pending"][book.dest] = {"since": now, "asked": 0}

    pending = state["pending"]
    for dest in [d for d, p in pending.items() if not isinstance(p, dict) or now - (p.get("since") or 0) > LOOKUP_DAYS * 86400]:
        del pending[dest]  # asked often enough: hylly lookup asks about it from now on
    due = {d for d, p in pending.items() if now - (p.get("asked") or 0) >= folders.LOOKUP_AGAIN}
    to_go = [(book, book.dest) for book, _ in copied] + list(found.already)
    answered = [False]  # whether a source found details for one of them in this pass

    def ask_about(questions: list, ids: dict[str, list[str]]) -> int:
        """The lookup sources, about the Inbox's books whose lookups are due: within each source's budget, waiting for
        none (_patient). One out of budget, offline or with an answer it couldn't read asks again at a later pass."""
        if not questions:
            return 0
        try:
            with runs.only_one("lookup", root):  # as hylly lookup and hylly catalog --lookup take turns
                chosen, skipped = lookups.available()
                for why in skipped:
                    note(f"  {why}")
                if not chosen:  # no source to ask (lookups off, [lookups] order, or no keys): no details to be found either
                    for dest in (d for dests in ids.values() for d in dests):
                        pending.pop(dest, None)
                        if dest in taken:
                            taken[dest]["lookup"] = False
                    return 0
                stats = lookups.run(root, questions, chosen, log=lambda line: note(f"  {line}"), sleep=_patient)
        except runs.Busy:
            note("A lookup is running already: the Inbox's new books are looked up at a later pass.")
            for dests in ids.values():
                for dest in dests:
                    pending[dest]["asked"] = now
            return 0
        waits = any(st.get("quota") or st["left"] or st["waiting"] or st["errors"]
                    or (st["stopped"] and not st.get("refused") and not st.get("changed")) for st in stats.values())
        answers = lookups.Answers(root)
        with_details = {q.id for q in questions if answers.for_query(q)}
        answered[0] = answered[0] or any(st["found"] for st in stats.values())
        for title, dests in ids.items():
            for dest in dests:
                if title in with_details and dest in taken:
                    taken[dest]["found"] = True
                if waits:
                    pending[dest]["asked"] = now
                else:
                    pending.pop(dest, None)
        return sum(st["asked"] for st in stats.values())

    def ask(queries: list, files: dict[str, list[str]]) -> int:  # the build's own questions, of the books due
        by_file = {rel: title for title, rels in files.items() for rel in rels}
        ids: dict[str, list[str]] = {}
        for dest in due:
            if dest in by_file:
                ids.setdefault(by_file[dest], []).append(dest)
            elif not (root / dest).exists():
                pending.pop(dest, None)  # gone from the library meanwhile
        return ask_about([q for q in queries if q.id in ids], ids)

    listed = _catalogued(root, [dest for _, dest in found.already])
    build, asked_already = bool(copied) or any(listed.get(dest) is None for _, dest in found.already), False
    if not build and due:  # only lookups that waited: the books are in the catalog already, which is built again only
        questions, ids = _waiting_questions(root, due, pending)  # if a source has found something for them now
        ask_about(questions, ids)
        build, asked_already = answered[0], True
    built = False
    if build:
        try:
            with runs.only_one("catalog", root), _in_words():
                catalog.build(root, log=note, ask=ask if due and not asked_already else None)
            built = True
            state["said"].pop("trouble|build", None)
        except runs.Busy:
            once("trouble|build", "A catalog build is running already: the Inbox's books are in the library, and go from the "
                 "Inbox once a build of its own goes through, at a later pass.")
        except catalog.PageNotWritten as e:  # the build is kept, its books catalogued: only the server's copy of it lags
            built = True
            once("trouble|build", e.message)
        except Exit as e:
            if e.code == INTERRUPTED:
                raise
            once("trouble|build", "The catalog wasn't built, so the Inbox's books are in the library but stay in the Inbox "
                 f"until a build goes through: {e.message}")
    if built or not build:
        listed = _catalogued(root, [dest for _, dest in to_go])
    # a book the library has already, dropped under a # folder: the card it is in gets the tag, once for that original,
    # however many passes it stays in the Inbox; which can be undone, and then stays off
    had = [(book, dest) for book, dest in [*found.already, *((book, book.dest) for book, how in copied
                                                             if how == "already there")]
           if book.tags and listed.get(dest) and not _unopened(listed[dest], dest) and not given_before(book)]
    given = _give(root, [(listed[dest], book.tags, "") for book, dest in had], note)
    if not given:
        remember(book for book, _ in had)
    not_given = given or not_given
    if not_given:
        once("trouble|edits", not_given)
    else:
        state["said"].pop("trouble|edits", None)
    for book, dest in to_go:
        record = listed.get(dest)
        if record is None:
            continue  # not in the catalog yet: it stays, and a later pass tries again
        bad = _unopened(record, dest)
        key = f"unopened|{book.inbox}|{book.rel}"
        if bad:  # the catalog can't open its copy: kept, and set aside if it's still so at a later pass
            if key in state["said"]:
                state["said"].pop(key)
                taken.pop(dest, None)
                aside(book, "unopened", bad)
            else:
                once(key, f"{book.shown} is in the library as {dest}, but no reader can open that copy ({bad}): it "
                     f"stays in the Inbox, and goes into {folders.NOT_ADDED} at the next pass if it's still so.",
                     trouble=False)
            continue
        state["said"].pop(key, None)
        try:
            how = copies.remove(book, paths.library_file(root, dest) or root / dest, guard)
        except OSError as e:
            once(f"remove|{book.inbox}|{book.rel}", f"{book.shown} is in the library as {dest}, but couldn't be removed "
                 f"from the Inbox ({e.strerror or e}): the user Hylly runs as must be able to change the Inbox's folders.")
            continue
        if how == "changed":
            note(f"{book.shown} has changed since it was taken: it stays in the Inbox, and is looked at again.")
            continue
        if how == "library":
            once(f"library|{book.inbox}|{book.rel}", f"{book.shown} is the library's own file, by another spelling of "
                 "its folder, so it stays: [inbox] must name a folder outside the library.")
            continue
        if how == "differs":
            once(f"differs|{book.inbox}|{book.rel}", f"{book.shown} and its copy in the library, {dest}, don't read "
                 "the same now, so it stays in the Inbox, and is looked at again.", trouble=False)
            continue
        gone_books.add((book.inbox, book.rel))
        write(f"Removed from the Inbox, as the library has it as {dest}: {book.shown}")
        was = taken.pop(dest, None)
        if was is not None:  # added by this pass, or by one whose build didn't go through
            result["added"].append(dest)
            if was.get("lookup"):
                result["details_found"] = (result["details_found"] or 0) + bool(was.get("found"))
        else:
            result["already"].append(dest)
    for inbox in inboxes:
        if (inbox not in found.missing and inbox not in found.overlap and inbox not in found.nested
                and all(other is not inbox for other, _ in found.unprobed)):
            copies.tidy(paths.Path(inbox.folder))

    for name, files in found.seen.items():  # what the next look compares with, but the books that are gone
        state["seen"][name] = {rel: seen for rel, seen in files.items() if (name, rel) not in gone_books}
    for inbox in found.missing:
        state["seen"].pop(inbox.key, None)
    looked = {inbox.key for inbox in inboxes if inbox.key in found.seen}
    for key in [k for k in state["said"] if k.split("|")[0] in BOOK_SAID]:
        # said of a book no longer in its Inbox, or of a folder no books are in any more: forgotten, to be said again if
        # it comes back; and of an Inbox [inbox] no longer names. By the Inbox's key, as written: a path may have a |
        kind = key.split("|")[0]
        name = next((inbox.key for inbox in inboxes if key.startswith(f"{kind}|{inbox.key}|")), None)
        if name is None:
            del state["said"][key]
            continue
        if name not in looked:
            continue
        rel, there = key[len(f"{kind}|{name}|"):], state["seen"].get(name, {})
        if not (any(f.startswith(f"{rel}/") for f in there) if kind == "no tag" else rel in there):
            del state["said"][key]
    for name in list(tagged):  # an original gone from its Inbox, or an Inbox no longer named: given again if it's back
        if not any(inbox.key == name for inbox in inboxes) or not isinstance(tagged[name], dict):
            del tagged[name]
        elif name in looked:
            tagged[name] = {rel: was for rel, was in tagged[name].items() if rel in state["seen"].get(name, {})}
    for dest in [d for d, was in taken.items() if not isinstance(was, dict) or now - (was.get("since") or 0) > LOOKUP_DAYS * 86400]:
        del taken[dest]
    added, aside_n, already = len(result["added"]), len(result["set_aside"]), len(result["already"])
    if added or aside_n or already:
        found_n = result["details_found"]
        state["done"] = [row for row in state["done"] if now - row[0] < DONE_KEPT] + [[now, added, -1 if found_n is None else found_n,
                                                                                       aside_n, already]]
        counts = bell.inbox_said(added, found_n, aside_n, already)
        tell(f"Inbox: {counts}")
        if not (by_hand and at_keyboard()):  # a person at the keyboard has just read it
            names = [f"Added: {dest}" for dest in result["added"]] + [f"Already in the library: {dest}" for dest in result["already"]]
            names += [f"Set aside: {s['file']}: {s['why']}" for s in result["set_aside"]]
            more = f"\n… and {len(names) - SHOWN} more: hylly logs inbox." if len(names) > SHOWN else ""
            bell.job_note(INBOX_NOTE, counts + "\n" + "\n".join(names[:SHOWN]) + more, counts)
    try:
        atomic_write(folders.state_file(root), json.dumps(state, indent=1, ensure_ascii=False), durable=False)
    except OSError as e:
        note(f"The Inbox's state couldn't be kept ({e.strerror or e}): the next pass looks at its books afresh.")
    return result


def _copying(copying, job: str, began: float, held: bool = False):
    """A HyllyCopy's run, one at a time here and on its target. held: the job's lock here is held already, by a backup
    or a restore for the whole of its run (cmd_backup), and this adds only the target's."""
    try:
        with contextlib.nullcontext() if held else runs.only_one(job):
            copying.prepare()
            with runs.beside(job, paths.Path(copying.folder) / "locks", "the same target"):
                return copying.run()
    except Exit as e:  # no room, a disk that isn't there: the phone may be away with the share, so this one goes again too
        if e.code in (LOCAL, NOT_SET_UP) and not (at_keyboard() and time.monotonic() - began < ON_THE_SPOT):
            words = e.message
            copying.out(words)
            e.shown = True  # said: main doesn't say it again, and its --json object still has it
            Ended(text=words, told=True).carried(e)
            copying.notes.tell(f"{copying.NAME}: stopped", words, "It stopped before it was done. What was copied is kept. bell.log says more.")
            copying.send_the_rest()
        raise


ASKED = ("It asks before a first copy to a target, the first there from another source, or one far bigger than before, and "
         "nobody was there to answer: nothing was copied. Run it at a terminal, or with --yes once you're sure of where it copies "
         "from and to.")


def _go_ahead(args, job: str):
    """The question a copy or a backup asks its owner before its first run to a target, or one that would write more than
    all copied there before (hyllycopy's ask_before and _far_more): --yes answers it, someone at the keyboard does, and
    with nobody there it ends with nothing copied, exit code 8. As catalog's question does (_keep_fewer), hylly jobs
    tells of the run either way, and with nobody there a note says so: a scheduled one doesn't stop in silence."""
    from . import bell

    def ask(why: str) -> None:
        if args.yes:
            return print(f"{why} Going on, as --yes says.", flush=True)
        nobody = (f"{why} So it asks first, and nobody is here to answer: once its owner has checked where it copies from and "
                  "to, the same command with --yes goes ahead. Nothing was copied.")
        if not at_keyboard():  # a run that stopped at the question: hylly jobs tells of it, and this note
            bell.job_note(f"{runs.JOBS[job][0]} stopped", nobody, ASKED)
            raise Ended(text=ASKED, told=True, ran=True).carried(Exit(NEEDS_YOU, nobody))
        if not _confirm(f"{why} Go ahead?", nobody):  # answered no: hylly jobs tells of that run too
            raise Ended(ran=True).carried(Exit(NEEDS_YOU, "Nothing was copied. Once you're sure of where it copies "
                                                          "from and to, the same command goes ahead, and with --yes it "
                                                          "doesn't ask."))
    return ask


def cmd_copy(args) -> Ended | None:
    """HyllyCopy: a copy, its plan with --dry-run, and hylly copy check. No library in it, so it holds the disks itself."""
    from . import hyllycopy as hc

    every = args.progress or 0
    if args.action == "check":
        checking = hc.Verifying(args.target, every=every)
        checking.check()
        if args.dry_run:
            checking.plan()
            return _report(dry_run=True, plan=checking.planned)
        with runs.only_one("copy"):
            problems = checking.run()
        _report(**checking.checked)
        ended = Ended(text=f"{many(problems, 'problem')} found.", failed=problems, told=True)
        if problems:
            raise ended.carried(Exit(PARTLY))
        return ended
    copying = hc.Copying(args.source, args.target, everything=args.everything, new_disk=args.new_disk, every=every)
    copying.check()
    if args.dry_run:
        copying.plan()
        return _report(dry_run=True, plan=copying.planned)
    copying.ask_before(_go_ahead(args, "copy"))
    result = _copying(copying, "copy", time.monotonic())
    words = copying.tell_of_the_end(result)
    _report(**hc.result_data(result), words=words)
    ended = Ended(text=words, failed=len(result.failed) + len(result.disagree) + len(result.went), told=True)
    if not result.done or result.disagree or result.went:
        raise ended.carried(Exit(PARTLY))
    return ended


def cmd_backup(args) -> Ended | None:
    """A backup, its plan with --dry-run, and hylly backup check: the order of the work is backup.py's. Without TARGET,
    to target under [backup] in the settings. A backup holds its lock to the end, the zip and the records too, so that
    hylly jobs tells of it and a second one waits its turn till then."""
    from . import backup

    every, target = args.progress or 0, backup.target_of(args.target)
    if args.action == "check":
        if args.dry_run:
            return _report(dry_run=True, plan=backup.check_plan(target, every=every, every_zip=args.all))
        ended = backup.check(target, every=every, every_zip=args.all)
    else:
        root = paths.library_root(args.library)
        paths.check_library(root)
        if args.dry_run:
            return _report(dry_run=True, plan=backup.plan(root, target, new_disk=args.new_disk, every=every))
        began = time.monotonic()
        with runs.only_one("backup"):
            ended = backup.run(root, target, new_disk=args.new_disk, every=every, ask=_go_ahead(args, "backup"),
                               copy=lambda job: _copying(job, "backup", began, held=True))
    _report(**{**ended.data, "whole": ended.whole, "words": ended.words})
    told = Ended(text=ended.words, failed=ended.failed, told=True)  # its notes are its own (backup.py)
    if not ended.whole:
        raise told.carried(Exit(PARTLY))
    return told


def cmd_restore(args) -> Ended | None:
    """The books copied to a new folder, the zip unpacked in it, and the library's records made this computer's
    database if it has none (backup.restore). Nothing there is written over. The backup's lock is held to the end, the
    unpacking and the records too."""
    from . import backup

    to, every = paths.Path(args.to), args.progress or 0
    if args.dry_run:
        return _report(dry_run=True, plan=backup.restore_plan(args.target, to, saved=args.saved, new_disk=args.new_disk, every=every))
    began = time.monotonic()
    with runs.only_one("backup"):
        ended = backup.restore(args.target, to, saved=args.saved, new_disk=args.new_disk, every=every,
                               copy=lambda copying: _copying(copying, "backup", began, held=True))
    _report(**{**ended.data, "whole": ended.whole, "words": ended.words})
    told = Ended(text=ended.words, failed=ended.failed, told=True)  # its notes are its own (backup.py)
    if not ended.whole:
        raise told.carried(Exit(PARTLY))
    return told


def cmd_rules(args) -> None:
    """The rules in effect and where they come from, one table of them, a shipped file to start your own from, or
    what the rules make of a name."""
    from . import rules

    if args.action == "try":
        return _try_name(args)
    if args.action in ("show", "copy") and args.what not in (*rules.SHIPPED, *rules.SPECS):
        what = "file" if args.action == "copy" else "file or table"
        raise Exit(USAGE, f"Which {what}? hylly rules {args.action} " + "|".join(rules.SHIPPED)
                         + ("" if args.action == "copy" else " (or a table: " + ", ".join(rules.SPECS) + ")"))
    if args.action == "copy":
        name = rules.FILE_OF.get(args.what, args.what)
        if rules.folder().is_file():
            raise Exit(USAGE, f"Your rules are one file, {rules.folder()} (rules in {paths.settings_file()}, or "
                              "HYLLY_RULES): write what you change there, or set rules to a folder. Nothing was written.")
        dest = rules.folder() / f"{name}.toml"
        if dest.exists():
            raise Exit(USAGE, f"{dest} exists already: edit it, or move it away first. Nothing was written.")
        if args.dry_run:
            return print(f"It would copy the shipped {name}.toml to {dest}, with every rule in it commented out. Nothing was written.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rules.commented((rules.DATA / f"{name}.toml").read_text(encoding="utf-8")), encoding="utf-8")
        return print(f"Copied the shipped {name}.toml to {dest}, with every rule in it commented out: it changes "
                     "nothing until you take the # off a line.\nTake it off only what you change: what stays commented "
                     "out stays as shipped, and gets the program's own updates. hylly rules shows what is in effect.")
    r = rules.current()
    if args.action == "show":
        tables = [t for t, f in rules.FILE_OF.items() if f == args.what] if args.what in rules.SHIPPED else [args.what]
        _report(what=args.what, files=[str(p) for p in r.files], tables={t: r.data.get(t) or {} for t in tables})
        return print(rules.as_toml(r, tables))
    print(rules.describe(r))
    print("\nYour settings, with where each comes from: hylly settings show.")


def _try_name(args) -> None:
    """hylly rules try NAME: the title, authors, series, kind, genres, games and device the rules give a file's
    name, without a catalog run: what catalog.from_name makes of it, by the catalog's own record(), as a build makes
    every title, so what try says and what the build does can't differ. Here it is only said."""
    from . import catalog

    if not args.what:
        raise Exit(USAGE, 'Which name? hylly rules try "Neon Rangers 002 (2009).cbz" [--bundle NAME] [--publisher NAME]')
    kind = next(iter(_kinds(args.kind.strip().lower()))) if args.kind else ""
    size = int((args.mb or 0) * MB)  # a disk's MB, as --mb is named: words.py
    found = catalog.from_name(args.what, args.bundle or "", args.publisher or "", kind, size)
    ext, fmt, labelled = found["format"], found["known"], found["labelled"]
    what = ", ".join(x for x in (fmt.layout if fmt else "", "sold by shops as a book" if fmt and fmt.shop else "",
                                 "a book in your folders" if fmt and fmt.book else "",
                                 "can be kept (hylly set format)" if fmt and fmt.keep else "",
                                 f"worth converting to {fmt.convert.upper()}" if fmt and fmt.convert else "") if x)
    roles = {"book": "the book", "extra": "an extra", "audio": "audio", "video": "video"}
    in_label = (f'"{labelled[1]}" would make a shop\'s file {roles[labelled[0]]} ([labels] {labelled[0]})' if labelled
                else "no word that would make a shop's file an extra")
    why = "--kind" if kind else found["why_kind"]
    series, number = found["series"], found["number"]
    rows = [("Name", found["name"]), ("Title", found["title"]),
            ("Authors", "; ".join(found["authors"]) or "(none in the name)"),
            ("Series", (f"{series}, number {number}" if number else series) if series else "(none)"),
            ("Kind", found["kind"] + (f" ({why})" if why else "")),
            ("Format", f"{ext or '(none)'}: {what or 'not a format the rules know'}"),
            ("A shop's file", roles.get(found["role"], found["role"])
             + (f", labelled {found['label']}" if found["label"] else "")),
            ("In a label", in_label + (f"; names a language: {found['language']}" if found["language"] else "")),
            ("Genres", ", ".join(found["genres"]) or "(none)"),
            ("Games", ", ".join(found["games"])
             or ("(none)" if found["kind"] == "rpg" else "(only RPG titles have one)")),
            ("Device", found["device"] + (f" ({found['why_device']})" if found["why_device"] else "")
             + (f", at {args.mb:g} MB" if args.mb and ext != "zip" else ""))]  # a ZIP's own size never counts
    _report(**{label.lower().replace(" ", "_").replace("'", ""): value for label, value in rows})
    print("\n".join(f"{label + ':':<15}{value}" for label, value in rows))
    if args.bundle is None and args.publisher is None:
        _note("A shop's title gets its kind and genres from its bundle and publisher too: add --bundle and --publisher to "
              "try them.")


def cmd_humble(args) -> Ended | None:
    actions = {"download": cmd_humble_download, "list": cmd_humble_download, "seed": cmd_seed}  # by name as it runs: a
    # test may stand one in; the list is the download's code path (_shop_download)
    return actions[args.action](args)


def _logged_in(shop) -> bool:
    login = shop.logins[0]  # the one its download needs
    return bool(os.environ.get(SITES[login][2]) or session_path(login).is_file())


def cmd_download_all(args) -> None:
    """Every shop you have a login for, one after another; a problem with one doesn't stop the others. With a LIST
    (hylly list's, or a shop's), every shop it has rows for, each with its own rows as the list has them (in a file of
    its own for its run, as standard input is read once), whose run says what it couldn't take of them; the rows of a
    shop you aren't logged in to are said in one line for the shop, with how many (in the output, the log but in a dry
    run, and --json's list_problems, an entry for the shop), and those naming no shop each; then the run ends partly
    done."""
    from . import shoplist

    given = _given_list(args)
    unknown, results, codes, asked, each = [], [], [], 0, []
    if given is not None:
        unknown = [(title, rid, "matching nothing: an id is the shop's name, the title's key and the file's, as a list "
                                "gives it" if rid else "no id")
                   for shop, title, _, _, rid, _, _ in given.entries if shop not in shops.SHOPS]
    state = None
    with contextlib.suppress(SystemExit):  # no library: each shop's run says so itself
        state = paths.state_dir(paths.library_root(args.library))
    with tempfile.TemporaryDirectory() as folder:
        for shop, it in shops.SHOPS.items():  # the one list of the shops, which hylly verify checks too
            login = it.logins[0]  # the one its download needs
            mine = None
            if given is not None:
                n = given.rows.get(shop, 0)
                if not n:
                    each.append({"shop": shop, "ended": "no rows in the list", "exit_code": None})
                    continue
                if not _logged_in(it):
                    said = (f"{many(n, 'row')} of {it.label}'s, a shop you aren't logged in to (hylly login "
                            f"{login}): not taken")
                    results.append(f"{it.label}: {said}.")
                    unknown.append(("", shop, said))
                    if state is not None and not args.dry_run:
                        _to_log(state / "download-log.txt", it.tag, f"Not taken from the list: {said}.")
                    each.append({"shop": shop, "ended": "no login saved", "exit_code": None})
                    codes.append(PARTLY)
                    continue
                mine = paths.Path(folder) / f"{shop}.csv"  # its rows alone, as the list has them, marks and all
                mine.write_bytes(shoplist.data(given.entries_of(shop)))
            elif not _logged_in(it):
                results.append(f"{it.label}: no login saved (hylly login {login}), so it was left out.")
                each.append({"shop": shop, "ended": "no login saved", "exit_code": None})
                continue
            asked += 1
            _note(f"\n== {it.label} ==")
            flags = (["--dry-run"] if args.dry_run else []) + (["--update"] if args.update and it.update else [])
            flags += ["--limit", str(args.limit)] if args.limit is not None else []
            flags += [str(mine)] if mine else []
            try:
                main(["--here", shop, "download", *flags, *(["--library", args.library] if args.library else []),  # where this runs
                      *(x for given_ in args.rules or [] for x in ("--rules", given_))])
                results.append(f"{it.label}: done.")
            except SystemExit as e:  # a bug in one too (main: exit code 70) is no reason to leave out the others
                each.append({"shop": shop, **LAST})
                if e.code == INTERRUPTED:  # you stopped it, not one shop. Its message has been said
                    _report(shops=each)
                    raise Exit(INTERRUPTED) from None
                if e.code not in (None, 0):
                    codes.append(e.code if isinstance(e.code, int) else PARTLY)
                    if isinstance(e.code, str):  # one main didn't say: its own words
                        print(e.code, file=sys.stderr)
                    why = str(e).removeprefix("Error: ") if isinstance(e, Exit) or isinstance(e.code, str) else ""
                    how = "partly done" if e.code == PARTLY else "stopped"  # partly: it went on, as said above
                    results.append(f"{it.label}: {how}" + (f": {why.splitlines()[0]}" if why else "."))
                else:
                    results.append(f"{it.label}: done.")
            else:
                each.append({"shop": shop, **LAST})
    named = [(title, rid, why) for title, rid, why in unknown if rid not in shops.SHOPS]
    if named:
        print("\nRows of your list not taken, as they name no shop, the rest going on:")
        for title, rid, why in named:
            print(shoplist.line(title, rid, why))
        codes.append(PARTLY)
    if unknown:
        _report(list_problems=[{"title": t, "id": rid, "why": why} for t, rid, why in unknown])
    _report(shops=each)
    print("\n" + "\n".join(results))
    if not asked and given is None:
        raise Exit(LOGIN, "No shop has a login saved, so nothing was done.")
    if not asked and not unknown and not codes:
        raise Exit(USAGE, "Your list has no row of any shop: hylly list writes one. Nothing was downloaded.")
    if codes:  # a bug in any of them; else what they have in common, say every login has expired; else partly done
        raise Exit(BUG if BUG in codes else codes[0] if len(set(codes)) == 1 else PARTLY)


def cmd_list_all(args) -> None:
    """hylly list [FILE]: hylly SHOP list for every shop you have a login for, in one file (shoplist.merged); a problem
    with one shop doesn't stop the others, and is said."""
    from . import shoplist

    out = _list_out(args, None)
    results, codes, texts, asked = [], [], [], 0
    with tempfile.TemporaryDirectory() as folder:
        for shop, it in shops.SHOPS.items():
            if not _logged_in(it):
                results.append(f"{it.label}: no login saved (hylly login {it.logins[0]}), so it isn't in the list.")
                continue
            asked += 1
            _note(f"\n== {it.label} ==")
            mine = paths.Path(folder) / f"{shop}.csv"  # written by a dry run too: it is no file of yours, and goes
            _PARTS_OF_A_LIST.add(str(mine))
            try:
                main(["--here", shop, "list", str(mine), "--force", *(["--dry-run"] if args.dry_run else []),
                      *(["--library", args.library] if args.library else []),
                      *(x for given_ in args.rules or [] for x in ("--rules", given_))])
                results.append(f"{it.label}: in the list.")
            except SystemExit as e:
                if e.code == INTERRUPTED:
                    raise Exit(INTERRUPTED) from None
                if e.code not in (None, 0):
                    codes.append(e.code if isinstance(e.code, int) else PARTLY)
                    why = str(e).removeprefix("Error: ") if isinstance(e, Exit) or isinstance(e.code, str) else ""
                    gone = not mine.is_file()
                    results.append(f"{it.label}: stopped" + (f": {why.splitlines()[0]}" if why else ".")
                                   + (f" Its rows aren't in the list, so a download of it leaves {it.label} out: hylly "
                                      f"{shop} list writes them once it can." if gone else ""))
            finally:
                _PARTS_OF_A_LIST.discard(str(mine))
            if mine.is_file():
                texts.append(mine.read_bytes())
        if not asked:
            raise Exit(LOGIN, "No shop has a login saved, so no list was written.")
        data = shoplist.merged(texts)
    _note("\n" + "\n".join(results))
    n = data.count(b"\r\n") - 1
    _write_list(out, data, args.dry_run, f"{many(n, 'row')} of {many(len(texts), 'shop')}",
                force=args.force)
    if codes:
        raise Exit(BUG if BUG in codes else codes[0] if len(set(codes)) == 1 else PARTLY)


def _as_json(data) -> None:
    print(json.dumps(data, indent=1))  # all in plain letters, or a pipe on Windows would get an old code page's


def cmd_jobs(args) -> None:
    """The jobs running on this computer, and how each one's last run ended; or one that has hung, ended."""
    if args.action == "stop":
        return _stop_run(args.job, args.dry_run)
    running = runs.running()
    going = {j["job"] for j in running}  # a run going now has begun its record (ended: false), and is shown as running
    last = [r for r in runs.last_runs() if r.get("ended") is not False or r.get("job") not in going]
    if args.json:
        return _as_json({"running": running, "last": last})
    print("\n".join(f"Running: {j['what']} ({runs.describe(j)}). If it has hung, {j['stop']} ends it."
                    + (f"\n  At {j['at']}: {j['progress']}" if j.get("progress") else "") for j in running) or "No job is running.")
    for r in last:
        if r.get("ended") is False:  # it never got to its end: stopped, killed, or the power cut
            print(f"Last: {r['what']}, {r['started']}, didn't end (stopped or killed).")
            continue
        print(f"Last: {r['what']}, {r['started']}, {time_words(r.get('seconds', 0))}, exit code {r.get('exit')}"
              + (f": {str(r['said']).splitlines()[0]}" if r.get("said") else "."))


def _log_files(library: str | None) -> dict:
    from . import access, bell, instance

    places = {"server": instance.log_file(), "bell": bell.log_file(), "access": access.access_log_file()}
    with contextlib.suppress(SystemExit):  # those of the state folder are there without a library
        state = paths.state_dir(paths.library_root(library))
        places = {"download": state / "download-log.txt", "lookup": state / "lookup-log.txt", "inbox": state / "inbox-log.txt", **places}
    return places


def cmd_logs(args) -> None:
    """What the logs are and where, or the end of one."""
    from . import access

    places = _log_files(args.library)
    if not args.name:
        for name, path in places.items():
            print(f"{name:9} {path}" + ("" if path.is_file() else "  (none yet)"))
        return print("hylly logs NAME shows the end of one.")
    if args.name not in places:
        raise Exit(NOT_SET_UP, "That log is in the library, and no library is set: pass --library.")
    print("\n".join(access.tail(places[args.name], args.tail or 1)) or f"Nothing in {places[args.name]} yet.")


def cmd_commands(args) -> None:
    data = commands_data(internal=args.internal)
    if args.json:
        return _as_json(data)
    for c in data["commands"]:
        print(f"{c['command']}: {c['about']}")
        print("    online: " + (", ".join(c["online"]) or "no") + "; writes: " + ", ".join(c["writes"])
              + (f"; job: {c['job']}" if c["job"] else "") + ("; CHANGES YOUR ACCOUNT AT A SHOP" if c["account"] else "")
              + (f"; needs a person: {c['person']}" if c["person"] else "") + (f"; --dry-run: {c['dry_run']}" if c["dry_run"] else "")
              + ("; runs here" if c["runs"] == "here" else ""))
    print("\nBefore the command: " + "; ".join(f"{o['option']}: {o['help']}" for o in data["options"]))
    print(f"\n{data['remote']}")
    print(f"\n{data['also']}")
    print("\nExit codes:")
    for code, words in data["exit_codes"].items():
        print(f"  {code:>3}  {words}")
    print("\nEnvironment:")
    for name, words in data["environment"].items():
        print(f"  {name}: {words}")
    print(f"\n{data['assistants']}")


def cmd_settings(args) -> None:
    """hylly settings: this computer's settings.toml, every setting with where its value comes from (show), its path,
    where Hylly keeps each kind of its own files (places), a check of all of it, or the file in an editor. As git config
    shows, edits and checks its own."""
    from . import settings

    file = paths.settings_file()
    named = (" (HYLLY_SETTINGS)" if paths._env("HYLLY_SETTINGS") else " (in HYLLY_HOME)" if paths._env("HYLLY_HOME") else
             " (in HYLLY_CONFIG_DIR)" if paths._env("HYLLY_CONFIG_DIR") else "")
    if args.action == "path":
        if args.json:
            return _as_json({"file": str(file), "there": file.is_file(), "named_by": named.strip(" ()") or None})
        return print(file)
    if args.action == "places":
        found = paths.places()
        if args.json:
            return _as_json({"places": found, "all_in_one": str(paths.all_in_one() or "") or None, "settings_file": str(file),
                             "job_output": str(paths.job_output_dir())})
        width = max(len(p["name"]) for p in found) + 2
        for p in found:
            print(f"{p['name'].capitalize()}:".ljust(width) + f"{p['folder']}  ({p['said']})")
        print(f"The settings file is {file}{named}, and what long jobs said is in {paths.job_output_dir()}.")
        one = paths.all_in_one()
        variables = ", ".join(f"{p['variable']} ({p['name']})" for p in found)
        return print((f"All in one folder, {one}. " if one is not None else "")
                     + f"A kind's own variable takes it elsewhere, a folder by its full path: {variables}"
                     + ("." if one is not None else "; and HYLLY_HOME keeps them all in one folder."))
    if args.action == "edit":
        return _edit_settings(args, file)
    if args.action == "check":
        mistakes, left = settings.problems()
        if args.json:
            _as_json({"file": str(file), "there": file.is_file(), "mistakes": mistakes, "left_out": left})
        else:
            print(f"{file}{named}: " + ("not there, so every setting is its default." if not file.is_file() else
                                        f"{many(len(mistakes), 'mistake')} in it." if mistakes else "it reads fine."))
            for line in [*mistakes, *left]:
                print(f"  {line}")
        if mistakes:
            raise Exit(NOT_SET_UP)
        return
    from .folders import defaults  # the folders Hylly knows by name, with the values they have unless set

    try:
        built_in = defaults()
        found = settings.rows(built_in)
    except Exit as e:
        raise Exit(e.code, f"{e.message}\nhylly settings check lists every mistake in it.") from None
    kept = {name: settings.secret_from(name)[1] for name in settings.SECRETS}  # where each is, never what
    if args.json:
        return _as_json({"file": str(file), "there": file.is_file(), "settings": found, "secrets": kept})
    print(f"Settings file: {file}{named}" + ("" if file.is_file() else ", not there yet: every setting is its default")
          + ". settings.example.toml says what each one does.")
    print("\n".join(settings.listing(built_in)))
    print(f"  (the alarms' secrets, never shown: in {settings.secrets_dir()})")
    for name, where in kept.items():
        print(f"    {name}".ljust(60) + f"  # {'set: ' + where if where else 'not set'}")


def _edit_settings(args, file) -> None:
    """hylly settings edit: VISUAL, else EDITOR, else the system's own; then a check of what was written."""
    import shlex
    import subprocess

    from . import settings

    given = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
    named = "VISUAL" if os.environ.get("VISUAL") else "EDITOR"
    if given:
        try:
            editor = [w.strip('"') for w in shlex.split(given, posix=os.name != "nt")]
        except ValueError as e:  # an unclosed quote, say
            raise Exit(USAGE, f"{named} can't be read as a command ({e}): {given}. Put it right, or edit {file} yourself. "
                              "Nothing was opened.") from None
        if not editor:
            raise Exit(USAGE, f"{named} names no editor. Put it right, or edit {file} yourself. Nothing was opened.")
        if os.name == "nt" and not os.path.dirname(editor[0]):  # a bare name: from the PATH, never the current folder
            found = _on_the_path(editor[0])
            if not found:
                raise Exit(NOT_SET_UP, f"{editor[0]}, the editor {named} names, isn't on the PATH: give it by its full path, "
                                       f"or edit {file} yourself. Nothing was opened.")
            editor[0] = found
    elif os.name == "nt":  # by its full path: never one in the current folder
        editor = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")]
    elif sys.platform == "darwin":
        editor = ["open", "-W", "-t"]  # TextEdit, waited for
    else:
        found = next((shutil.which(name) for name in ("editor", "nano", "vi") if shutil.which(name)), None)
        if not found:
            raise Exit(NOT_SET_UP, f"No editor found here: set VISUAL or EDITOR to one, or edit {file} yourself.")
        editor = [found]
    if args.dry_run:
        return print(f"It would open {file} in {' '.join(editor)}" + ("" if file.is_file() else ", after making it, empty")
                     + ". Nothing was opened or made.")
    if not at_keyboard():
        raise Exit(NEEDS_YOU, f"An editor is for a person: its owner runs hylly settings edit in a terminal, or edits {file}. "
                              "Nothing changed.")
    if not file.is_file():
        file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(file, "# Hylly's settings: settings.example.toml says what each one does.\n")
        _note(f"Made {file}, empty.")
    try:
        ended = subprocess.run([*editor, str(file)]).returncode
    except OSError as e:
        raise Exit(LOCAL, f"The editor couldn't be started ({' '.join(editor)}: {e.strerror or e}). Set VISUAL or EDITOR to "
                          "one that's there.") from None
    if ended:
        raise Exit(LOCAL, f"The editor ended with exit code {ended}.")
    settings.reload()
    mistakes, _ = settings.problems()
    print(f"{file}: " + (f"{many(len(mistakes), 'mistake')} in it now:" if mistakes else "it reads fine."))
    for line in mistakes:
        print(f"  {line}")
    if mistakes:
        raise Exit(NOT_SET_UP)


def _on_the_path(name: str) -> str | None:
    """A program by its bare name, as a terminal finds it on the PATH, with PATHEXT's endings (VS Code's code is code.cmd),
    but never in the current folder, which Windows would look in first: a folder others can write could hold one."""
    # the endings first: VS Code's folder has code.cmd, and beside it code, a script for other shells Windows can't run
    endings = [*(e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e), ""]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not os.path.isabs(folder):
            continue
        for ending in endings:
            path = os.path.join(folder, name + ending)
            if os.path.isfile(path):
                return path
    return None


def cmd_doctor(args) -> None:
    from . import doctor

    result = doctor.report(args.library)
    if args.json:
        _as_json(result)
    else:
        print(doctor.text(result))
    if result["problems"]:
        raise Exit(PARTLY)


def cmd_demo(args) -> None:
    from . import demo

    if args.stop:
        if args.port is not None or args.no_browser:
            raise Exit(USAGE, "hylly demo --stop stops the demo's server on whatever port it runs, and opens nothing: it takes "
                              "no --port or --no-browser. Nothing was stopped.")
        demo.stop(args.folder, dry_run=args.dry_run)
        return
    port = demo.PORT if args.port is None else args.port
    if args.dry_run:
        place = demo.place_of(args.folder)
        there = demo.is_demo(place)
        serving = ("." if args.no_serve else f"; then serve it in the background at http://127.0.0.1:{port}/, and end."
                   if args.background else f"; then serve it at http://127.0.0.1:{port}/.")
        return print(f"It would {'use the demo library in' if there else 'make a demo library of made-up books in'} {place}, "
                     "with settings and an owner of its own, and build its catalog" + serving + " Nothing was made.")
    demo.run(args.folder, port, serve_it=not args.no_serve, open_browser=not args.no_browser, background=args.background)


def cmd_internal(args) -> None:
    """Between Hylly's own processes: another computer's backup asking the server, a long job passed on from another
    computer (remote.py), and a long one of the server's periodic jobs (periodic.run_apart). Not for people, and left
    out of hylly --help and hylly commands."""
    from . import backup, remote

    if args.action == "job":  # on the server, from another computer: a long job, on its own
        if args.dry_run:
            return print(f"It would start hylly {' '.join(args.words)} on its own, its output in {paths.job_output_dir()}.")
        raise Exit(remote.as_job(args.words))  # the job's own code, as it ended there
    if args.action == "server-job":  # a long one of the server's periodic jobs, started on its own by hylly server jobs
        from . import periodic

        return print(periodic.run_apart(args.name, paths.library_root(args.library), dry_run=args.dry_run))
    root = paths.library_root(args.library)
    paths.check_library(root)
    if args.action == "inbox":  # a pass over the Inboxes, which the server's job starts (periodic._inbox)
        return _inbox_job(root, args.dry_run)
    if args.action == "backup-noted":  # another computer's note of its backup
        try:
            note = json.loads(sys.stdin.read())
            (backup.check_note if args.dry_run else lambda n: backup.keep_note(root, n))(note)
        except ValueError as e:
            raise Exit(USAGE, f"internal backup-noted wants a backup's note as JSON on stdin: {e}") from None
        return print("It's a backup's note, and would be kept." if args.dry_run else "Noted.")
    from . import db  # the database's copy made now, for a backup another computer is making

    if args.dry_run:
        return print(f"It would make today's copy of the database again in {db.backups(root)}, and of the settings if not made yet.")
    db.own(db.database(), root)  # only of the records it keeps: else db.NotHere
    made = db.nightly(root, fresh=True)
    with contextlib.suppress(OSError):  # the settings' copy of tonight, if not made yet: the night's own job tries again
        backup.settings_tonight(root)
    print(f"Copied: {made}.")


def _inbox_job(root, dry_run: bool) -> None:
    """hylly internal inbox: a pass over every Inbox (_inbox_pass), one at a time with hylly add, whose lock it holds:
    while one typed by a person runs, the pass leaves the Inboxes to it and ends well, as the next comes in a minute.
    Trouble the pass has said is no failure of the job's, which would be said again every minute: it ends well too."""
    from . import folders

    found = folders.inboxes(root=root)
    if not found:
        words = "No Inbox is set: folder = '...' under [inbox] names one."
        return print(f"{words} Nothing was changed.") if dry_run else _note(words)
    if dry_run:
        _report(dry_run=True, **_inbox_pass(root, found, dry_run=True))
        return
    try:
        with runs.only_one("add", root):
            _report(**_inbox_pass(root, found))
    except runs.Busy as e:
        _note(f"{e}\nThe Inboxes wait for it: the next pass goes on.")


def _stop_run(job: str, dry_run: bool = False) -> None:
    """hylly jobs stop NAME."""
    if dry_run:
        note = runs.current(job)
        return print(re.sub(r"^An? ", "No ", runs.JOBS[job][0]) + " is running on this computer." if note is None else
                     f"It would stop {runs.JOBS[job][0][:1].lower()}{runs.JOBS[job][0][1:]} ({runs.describe(note)}). Nothing was stopped.")
    note = runs.stop(job)
    if note is None:
        return print(re.sub(r"^An? ", "No ", runs.JOBS[job][0]) + " is running on this computer.")
    print(f"Stopped ({runs.describe(note)}). What it had finished is kept; running it again goes on from there.")


def _progress(done: int, total: int) -> None:
    """On a terminal, one line rewritten. Elsewhere that makes one endless line in a log: a line each tenth of the way. On
    standard error, as all progress is."""
    if getattr(sys.stderr, "isatty", lambda: False)():
        _note(f"\r  {done}/{total}", end="")
    elif done == total or done % max(total // 10, 1) == 0:
        _note(f"  {done}/{total}")


def _end_progress() -> None:
    """The end of a line _progress rewrote: a terminal's next line starts on a line of its own."""
    if getattr(sys.stderr, "isatty", lambda: False)():
        _note()


def main(argv: list[str] | None = None) -> None:
    """The command, and how it ends: exits.TABLE. Anything it has no words for is a bug, exit code 70, with its traceback
    (exits.bug): never 1, which says run it again."""
    from . import db, remote

    dry_before = db.in_dry_run()  # a dry run's, as hylly download --dry-run runs each shop's: the outer one ends it
    run: dict = {"found": {}, "json": None}  # what its handler finds (_report), and where its JSON goes, if asked for
    watched = {}  # the outer run's streams on a real pipe, standard error's too (2>&1 | head): for a reader that has
    if not _RUNS:  # read enough (_Reader)
        for name in ("stdout", "stderr"):
            if _piped(getattr(sys, name)):
                watched[name] = _Reader(getattr(sys, name))
                setattr(sys, name, watched[name])
    _RUNS.append(run)
    code, said = 0, ""
    try:
        try:
            with _in_words():
                _main(argv)
        except Exit:
            raise
        except Exception as e:  # noqa: BLE001 - what nothing foresaw
            raise bug(e) from None
    except SystemExit as e:  # Hylly's own Exit, or argparse's for a mistyped command
        code = e.code if isinstance(e.code, int) else 0 if e.code is None else USAGE
        said = e.message if isinstance(e, Exit) else ""
        run["meaning"] = getattr(e, "meaning", "")  # an end whose code's own words don't fit it, a busy database's
        if said and not getattr(e, "shown", False):  # one a copy has said already, as it went
            print(said, file=sys.stderr)
        raise
    finally:
        _RUNS[:] = [r for r in _RUNS if r is not run]  # by itself: another run's may look the same
        LAST.clear()  # how it went, for hylly download's own result: each shop's run is one of these
        LAST.update(exit_code=code, meaning=run.get("meaning") or TABLE.get(code, ""), message=said)
        LAST.update({k: v for k, v in run["found"].items() if k not in LAST})
        if run["json"] is not None:  # one object, however it ended: a bug's too
            with contextlib.suppress(OSError, ValueError):
                print(json.dumps({"command": run["command"], **LAST}, indent=1, default=str), file=run["json"], flush=True)
        remote.choose(None)  # a command's -H or --here is its own
        if db.in_dry_run() and not dry_before:  # a dry run's copies in memory go, and the next command has the real ones
            db.end_dry_run()
        for name, stream in watched.items():
            with contextlib.suppress(OSError, ValueError):  # a short output is written only now: a reader gone
                stream.flush()  # before it is told here, quietly
            if getattr(sys, name) is stream:
                setattr(sys, name, stream.stream)


def _piped(stream) -> bool:
    """Whether standard output or error is a real pipe (hylly X | head, hylly X 2>&1 | head), or a socket, as a
    service's log may be: not a terminal, a file or a file on a share, whose trouble in writing is said as ever."""
    try:
        mode = os.fstat(stream.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
    except (AttributeError, OSError, ValueError):  # no file under it, as in a test
        return False


class _Reader:
    """Standard output or error on a pipe (_piped), as main hands it to a command. A reader that has gone (hylly X |
    head, which has read all it wanted) shows as EPIPE, or on Windows EINVAL, which only on a pipe means that: from then
    on what's written goes to the null device, and the command goes on with its work and ends with its own code, without
    a word of the pipe. Any other trouble in writing is raised as ever."""

    def __init__(self, stream) -> None:
        self.stream, self.gone = stream, False

    def _lost(self, e: OSError) -> None:
        if not (isinstance(e, BrokenPipeError) or e.errno in (errno.EPIPE, errno.EINVAL)):
            raise e
        self.gone = True
        with contextlib.suppress(OSError, ValueError, AttributeError):  # what's left in the buffer goes there too, quietly
            nowhere = os.open(os.devnull, os.O_WRONLY)
            os.dup2(nowhere, self.stream.fileno())
            os.close(nowhere)

    def write(self, text: str) -> int:
        if not self.gone:
            try:
                return self.stream.write(text)
            except OSError as e:
                self._lost(e)
        return len(text)

    def flush(self) -> None:
        if self.gone:
            with contextlib.suppress(OSError, ValueError):
                self.stream.flush()
            return
        try:
            self.stream.flush()
        except OSError as e:
            self._lost(e)
            with contextlib.suppress(OSError, ValueError):
                self.stream.flush()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


ONE_OBJECT = frozenset({  # with --json, these print one object, however they end (main), and their output goes to standard error
    "humble download", "humble seed", "storybundle download",
    "drivethrurpg download", "itchio survey", "itchio download", "download", "add", "catalog", "tag suggest", "rules show",
    "rules try", "theme check", "copy", "copy check", "backup", "backup check", "restore", "server jobs"})
_RUNS: list[dict] = []  # each main's run, the innermost last
LAST: dict = {}  # how the run that ended last went (main): its exit code, what that means, its message and what it found


def _report(**found) -> None:
    """What a command found, for its result as one JSON object (ONE_OBJECT): kept whether or not --json asks for it, as
    hylly download keeps each shop's (LAST)."""
    if _RUNS:
        _RUNS[-1]["found"].update(found)


@contextlib.contextmanager
def _in_words():
    """What a run may end in besides Hylly's own Exit, turned into one: the code of the table, and words for people. Around
    the handler, inside a job's watch (_watched), so that its record and its phone note say what the exit code says; and
    around the whole command in main. What isn't here is a bug (main). How the run ended (Ended), if what it is made of
    carried that, its watch finds there (_ended_of)."""
    import sqlite3

    from . import db

    def said(code: int, words: str, public: str = KEPT) -> Exit:  # public: the phone's text, without names or paths
        ended = Exit(code, words)
        ended.public = public
        return ended

    try:
        yield
    except AuthError as e:
        raise said(LOGIN, f"Error: {e}", LOGIN_AGAIN) from None
    except ShopError as e:  # no answer at all, after every try: an outage, so what to do about it, as for the network's
        raise said(PARTLY, f"Error: {e}" + (" What was done is kept; try again later." if e.no_answer else "")) from None
    except _network_trouble() as e:  # by its kind alone: its message may have the address
        raise said(PARTLY, f"Network trouble ({type(e).__name__}), so this stopped. What was done is kept; try again later.") from None
    except (db.NotHere, db.Newer, db.Unusable, db.NotKept) as e:  # the database: what's wrong, and where to go
        raise said(NOT_SET_UP, str(e)) from None
    except sqlite3.Error as e:  # by what SQLite says: a damaged file or a disk's trouble, or busy. Anything else is a bug
        kind = _database_kind(e)
        if kind == "busy":
            busy = said(BUSY, f"The database, {db.file()}, is busy: another program has held it for {db.BUSY_SECONDS:g} seconds "
                              f"({e}). Try again in a moment. Nothing more was done.")
            busy.meaning = "The database is in use by another program: try again in a moment. Nothing changed."
            raise busy from None
        if kind:
            raise said(LOCAL, _database_trouble(e)) from None
        raise
    except OSError as e:  # a broken pipe here is another program's: standard output's and error's readers are main's
        if share_is_gone(e):
            raise said(LOCAL, f"The library's network share can't be reached right now: {e}") from None
        raise said(LOCAL, f"Can't go on: {e}") from None
    except KeyboardInterrupt:
        raise Exit(INTERRUPTED, "Stopped.") from None
    except Exception as e:
        from .lookups import LookupProblem

        if isinstance(e, LookupProblem):  # the one that ends a run: its answers can't be written (lookups.run)
            raise said(LOCAL, str(e)) from None
        raise


DAMAGED = ("SQLITE_CORRUPT", "SQLITE_NOTADB", "SQLITE_IOERR", "SQLITE_CANTOPEN", "SQLITE_FULL", "SQLITE_READONLY")
BUSY_NAMES = ("SQLITE_BUSY", "SQLITE_LOCKED")


def _database_kind(e) -> str:
    """"busy", "damaged" (a damaged file, or its disk's trouble: full, read-only, can't be opened or read), or "" for
    anything else SQLite says, a mistake in Hylly's own SQL or a rule broken, which is a bug. By SQLite's own name for
    it, its extended ones too (SQLITE_IOERR_READ)."""
    name = getattr(e, "sqlite_errorname", None) or ""
    if name.startswith(BUSY_NAMES):
        return "busy"
    return "damaged" if name.startswith(DAMAGED) else ""


def _database_trouble(e) -> str:
    """The database couldn't be used: which file, and the way back if it's damaged, the nightly copies with their README."""
    from . import db

    nights = "the library's state/db-backup folder"
    with contextlib.suppress(SystemExit, OSError):
        nights = str(db.backups(paths.library_root()))
    return (f"The database, {db.file()}, can't be used: {e}. If another program has it busy, try again in a moment; if its disk "
            f"is full, make room. If it's damaged (\"malformed\", \"not a database\"), its nightly copies are in {nights}, a "
            f"folder for each night, and the README.txt in each says how to put one back. Nothing more was done.")


HANDLERS = {  # each command's own: by name, looked up as it runs
    "login": "cmd_login", **{shop: f"cmd_{shop}" for shop in shops.SHOPS},  # each shop's, cmd_<its id>
    "download": "cmd_download_all", "list": "cmd_list_all", "add": "cmd_add", "copy": "cmd_copy", "backup": "cmd_backup",
    "restore": "cmd_restore", "verify": "cmd_verify", "init": "cmd_init", "lookup": "cmd_lookup", "jobs": "cmd_jobs",
    "logs": "cmd_logs", "doctor": "cmd_doctor", "commands": "cmd_commands", "demo": "cmd_demo", "catalog": "cmd_catalog",
    "serve": "cmd_serve", "server": "cmd_server", "user": "cmd_user", "people": "cmd_people", "bell": "cmd_bell",
    "tag": "cmd_tag", "set": "cmd_set", "decide": "cmd_decide", "rules": "cmd_rules", "theme": "cmd_theme", "reviews": "cmd_reviews",
    "reports": "cmd_reports", "settings": "cmd_settings", "internal": "cmd_internal",
}


def _main(argv: list[str] | None = None) -> None:
    from . import remote

    hide_addresses_in_tracebacks()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if remote.from_another_shell(argv):  # passed on from a computer that took this one for Linux or macOS
        raise Exit(NOT_SET_UP, remote.WINDOWS_SERVER)
    args = build_parser().parse_args(argv)
    if args.command is None:  # hylly alone: where to start, which isn't a command
        raise Exit(USAGE, START)
    if getattr(args, "json", False) and args.leaf in ONE_OBJECT and _RUNS:  # stdout for its object alone (main), from here,
        _RUNS[-1].update(json=sys.stdout, command=f"hylly {args.leaf}")  # so that an end this early gets one too
        with contextlib.redirect_stdout(sys.stderr):
            return _here_or_there(args, argv)
    _here_or_there(args, argv)


# Commands that hold no job themselves (job_of) and, passed on to the server computer, start there on their own all
# the same, but for a dry run: each with why
STARTED_APART = {
    "download": "each of its shops' runs is a job of its own, a command of its own",
    "server jobs": "the long jobs it starts on their own (periodic._start_apart) write where it writes: attached to ssh, "
                   "they would have its terminal, which ends with it, or its pipes, which ssh waits on",
}


def _here_or_there(args, argv: list[str]) -> None:
    """The command here, or passed on to the server computer."""
    from . import remote

    _library_first(args)
    remote.choose(None if args.here else args.remote, here=args.here)
    if args.command == "jobs" and getattr(args, "job", None) in remote.LOCAL_JOBS and remote.named():  # whatever is named
        print(f"A {args.job} runs on the computer it's typed on, so its stop is here, on this computer.", file=sys.stderr)
    try:
        there = remote.wanted(args.command, getattr(args, "job", None))
    except Exit as e:  # the settings can't say which computer is the server: the doctor is the one to say what's wrong
        if args.command != "doctor" or e.code != NOT_SET_UP or args.remote:
            raise
        print(f"{e.message}\nSo the doctor looks here, on this computer.", file=sys.stderr)
        there = False
    if there and args.command == "login" and getattr(args, "cookies", None) not in (None, "-"):
        file, send = paths.typed(args.cookies), f"hylly login {args.site} --cookies -"
        raise Exit(USAGE, f"hylly login runs on the server computer, {remote.server()[0]}, which can't see {args.cookies} on this "
                          f"one: {f'Get-Content {file} | {send}' if paths.WINDOWS else f'{send} < {file}'} sends it there. "
                          "Nothing was run.")
    here = _list_here(args) if there else None
    if here:  # a list's file on this computer, which the server computer can't see: through ssh's own streams
        host, (read, file) = remote.server()[0], here
        said = [w if w != file else "-" for w in remote.words(argv)] + ([] if file in argv else ["-"])
        words = " ".join(["hylly", *(paths.typed(w) for w in said)])  # quoted as it is to be typed: --title "Lantern Wolf"
        sent = (f"Get-Content {paths.typed(file)} | {words}" if paths.WINDOWS else f"{words} < {paths.typed(file)}") if read \
            else f"{words} > {paths.typed(file)}"
        raise Exit(USAGE, f"hylly {args.leaf} runs on the server computer, {host}, which "
                          + (f"can't see {file} on this one: {sent} sends it there." if read else
                             f"would write {file} there: {sent} writes it here.") + " Nothing was run.")
    if there:  # the server computer owns the library's data: run it there. A long job starts there on its own: one that
        # holds a job (job_of), and those of STARTED_APART. Not with its LIST on standard input, which a job started on
        # its own wouldn't have, nor with --json, whose one object a script waits for: those run while ssh does, as a
        # dry run does, which holds no job and changes nothing
        apart = (bool(job_of(args)) or (args.leaf in STARTED_APART and not args.dry_run)) \
            and not getattr(args, "json", False) and getattr(args, "list", None) != "-"
        code = remote.forward(remote.words(argv), apart)
        if code == SSH_FAILED:  # ssh's own: it didn't reach the server, or was refused there. The run ended here
            raise Exit(LOCAL, f"ssh couldn't reach the server computer, {remote.server()[0]}, that {remote.named_by()} names, "
                              "or was refused there (ssh's exit code 255; what it said is above). Nothing was run there. "
                              "hylly --here runs a command on this computer instead.")
        if _RUNS:
            _RUNS[-1]["json"] = None  # the server's hylly has given its own object, as its output came
        raise Exit(code)  # the server's own code, as it ended there
    _run(args)


def _list_here(args) -> tuple[bool, str] | None:
    """A shop list's file on this computer (not -) that a command run on the server computer would need: (True, a
    download's LIST it reads; or False, a list's FILE it writes), else None."""
    from . import shoplist

    if (args.command in shops.SHOPS and getattr(args, "action", None) == "list") or args.command == "list":
        out = getattr(args, "file", None) or shoplist.name(args.command if args.command in shops.SHOPS else None)
        return None if out == "-" or args.dry_run else (False, out)
    given = getattr(args, "list", None)
    return (True, given) if given and given != "-" else None


def _library_first(args) -> None:
    """hylly --library FOLDER COMMAND, as git -C: the same as --library after the command, for one that uses a library."""
    if args.library_first is None:
        return
    if not hasattr(args, "library"):
        raise Exit(USAGE, f"hylly {args.leaf} doesn't use a library, so --library before it has nothing to do: leave it out. "
                          "Nothing was done.")
    if args.library is not None and _same_folder(args.library) != _same_folder(args.library_first):
        raise Exit(USAGE, f"Two libraries, {args.library_first} before the command and {args.library} after it: give one. "
                          "Nothing was done.")
    args.library = args.library_first


SSH_FAILED = 255  # ssh's exit code for its own trouble; Hylly never ends with it


def _same_folder(path: str) -> str:
    """A folder as this computer tells folders apart: its full path, links followed, capitals as the system takes them."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def _run(args) -> None:
    """The command, here: with its dry run, its job's lock and watch, and the words of how it ended (_in_words)."""
    from . import db, rules, settings

    dry = bool(getattr(args, "dry_run", False))
    if dry and not db.in_dry_run():  # its databases read into memory, from now: what it would write goes nowhere (main ends it)
        db.start_dry_run()
    if paths.running_as_root():
        if args.command not in ("doctor", "commands"):  # which only look: the doctor says what's wrong with this too
            raise Exit(NOT_SET_UP, paths.AS_ROOT)
    elif args.command != "demo" and not dry:  # which keeps its own, and needn't make yours; a dry run changes nothing
        paths.keep_private()
        if args.command not in ("doctor", "commands"):  # which only look
            settings.secrets_at_start()
    rules.EXTRA[:] = getattr(args, "rules", None) or []  # --rules: read after your rules folder
    handler = globals()[HANDLERS[args.command]]
    job = job_of(args)
    if shop := asks_a_shop(args):
        root = None
        with contextlib.suppress(SystemExit):  # no library set: the command says so itself
            root = paths.library_root(args.library)
        runs.not_running(shop, root)
    if job in ("copy", "backup"):  # no library in it, or it holds the disks itself
        with _watched(job, args) as run, _in_words():
            run.ended = handler(args)  # how it ended, for its note and hylly jobs (Ended)
    elif job:
        with _watched(job, args) as run, _in_words():  # also a library that isn't there: a scheduled run wants to say so
            root = paths.library_root(args.library)
            paths.check_library(root)
            with runs.only_one(job, root):
                run.ended = handler(args)
    else:
        with _in_words():
            if args.command in NEEDS_THE_LIBRARY:
                paths.check_library(paths.library_root(args.library))
            handler(args)


if __name__ == "__main__":
    main()
