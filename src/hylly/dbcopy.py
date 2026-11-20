"""The nightly copy: the database and the settings, onto the library's disk, once it's past the hour. One of the server's
periodic jobs (periodic.py), which the server runs once an hour, or hylly server jobs with jobs = "external" under
[server]. The copy itself is the database's own (db.nightly) and the settings' (backup.settings_tonight); here is when,
and what's said of it: a failure once, not every hour, and tried again at the next look.

The database's copy is watched (_watch): one that fails, whatever goes wrong, and is tried again every hour, or is made
but isn't good by SQLite's own check of it, and is made again the next night, raises one alarm (bell.job_note, as for
a long job), and is said in the log once a day while it lasts; the
first good one after says so, in the log and to the alarms. Without that a damaged database, a library's disk gone
read-only, or a walled server that can't write the state folder would leave everyone's records without a fresh copy
for weeks, with only a line in the log every hour. The late copy's own alarm (bell.Watch.check_copies) stays, for a
copy that isn't even tried: nothing running the server's jobs.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import db

_said: dict[str, str] = {}  # what the nightly copy last said of a library, by what about: said once, not every hour


def _say_once(root: Path, about: str, words: str | None, log: Callable[[str], None], said: dict[str, str]) -> None:
    """words, unless they were the last said about this; None: nothing to say about it now, so it's said again if it
    comes back. said: where that's kept, by the library and what about (a text, so that hylly server jobs can keep it
    from one run to the next)."""
    key = f"{Path(root).absolute()}|{about}"
    if words is None:
        said.pop(key, None)
    elif said.get(key) != words:
        said[key] = words
        log(words)


def _left_as_they_are(root: Path, others: list[Path]) -> str | None:
    """Of the copies in db-backup that don't count (db.strangers), what each one's database says: only that, as one
    naming no library may be of the library's own database from before it took the library on."""
    if not others:
        return None
    named = [p.name for p in others if db.whose(p) is not None]
    nameless = [p.name for p in others if p.name not in named]
    which = []
    if named:
        which.append(f"{', '.join(named)}, whose database names another library than {db.owner_file(root).name} does")
    if nameless:
        which.append(f"{', '.join(nameless)}, whose database names no library")
    return (f"Left as they are in {db.backups(root)}, never counted nor removed: the copies of {', and of '.join(which)}. "
            f"Remove them when they aren't wanted.")


FAILING = "copy failing"  # said: what the copy's trouble is, while it lasts (_watch)


def _integrity(night: Path) -> str:
    """What SQLite's check of a nightly copy said (its check.json): "ok" for a good one."""
    try:
        check = json.loads((night / "check.json").read_text(encoding="utf-8")).get("integrity")
    except (OSError, ValueError, AttributeError):
        return "check.json can't be read"
    return check if isinstance(check, str) else "check.json says nothing of it"


def _watch(root: Path, trouble: str | None, made: Path | None, now: datetime, log: Callable[[str], None],
           said: dict[str, str], tell: Callable[[str, str, str], object] | None) -> None:
    """The copy's watch: trouble (a copy that failed, or isn't good) raises one alarm, as it starts, and is said in the log
    once a day while it lasts; the first good copy after it (made) is said, there and to the alarms, and the watch starts
    again. Neither: nothing changes (tonight's copy is there already, or no copy could be made here)."""
    if tell is None:
        from .bell import job_note as tell
    key, day = f"{Path(root).absolute()}|{FAILING}", now.strftime("%Y-%m-%d")
    if trouble is not None:
        if key not in said:
            said[key] = trouble
            with contextlib.suppress(Exception):  # the alarm never stands in the copy's way, nor in the settings'
                tell("The nightly copy failed", f"{trouble} The server's log says how it stands once a day, until a copy "
                     f"is good.",
                     "The library's database couldn't be copied tonight, or its copy isn't good. The server's log says why.")
        if said.get(f"{key}|day") != day:
            said[f"{key}|day"] = day
            log(trouble)
    elif made is not None and key in said:
        del said[key]
        said.pop(f"{key}|day", None)
        log(f"The database's nightly copy works again: {made}, and its check is good.")
        with contextlib.suppress(Exception):
            tell("The nightly copy works again", f"The database was copied to {made}, and its check is good.",
                 "The library's database is copied again.")


def copy_tonight(root: Path, log: Callable[[str], None], now: datetime | None = None, said: dict[str, str] | None = None,
                 tell: Callable[[str, str, str], object] | None = None) -> None:
    """What the server does now and then: the copy, once it's past the hour. A failure is said once a day, and tried
    again; it and a copy whose check isn't good raise one alarm, and the first good copy after says so (_watch). A
    database that doesn't keep the library's records isn't copied (db.nightly), and the server says why once. said:
    what was said so far (_say_once), by default kept for as long as this program runs. tell: an alarm's title, text and
    words for the phone (bell.job_note)."""
    now, said = now or datetime.now(), _said if said is None else said
    if now.hour < db.BACKUP_FROM_HOUR:
        return
    trouble, made = None, None
    try:
        made = db.nightly(root, now)
        if made:  # only a copy made says the database keeps the records: once tonight's is there, it isn't asked
            _say_once(root, "not copied", None, log, said)
            check = _integrity(made)
            if check == "ok":
                log(f"The database copied to {made}.")
            else:
                trouble = (f"The database was copied to {made}, but SQLite's check of the copy says: {check}. The database "
                           f"itself may need help; the copies before it are kept until one is good. The next copy is "
                           f"made tomorrow night.")
            _say_once(root, "others", _left_as_they_are(root, db.strangers(root, made.name)), log, said)
    except db.NotHere as e:
        _say_once(root, "not copied", f"The database wasn't copied to {db.backups(root)}: {e}", log, said)
    except Exception as e:  # noqa: BLE001 - whatever it is: watched, so that the copies don't stop with nobody told
        known = isinstance(e, OSError) or type(e).__module__ in ("sqlite3", db.__name__)
        trouble = (f"The database couldn't be copied to {db.backups(root)}: {e if known else f'{type(e).__name__}: {e}'}. "
                   f"It's tried again every hour.")
    _watch(root, trouble, made, now, log, said, tell)
    from . import backup  # the same night, the settings, for a backup made on another computer

    try:
        backup.settings_tonight(root, now)
        _say_once(root, "settings not copied", None, log, said)
    except OSError as e:  # once, not every hour: settings the copy can't take the phone's topic out of stay so till put right
        _say_once(root, "settings not copied", f"The settings couldn't be copied to {backup.settings_backups(root)}: {e}", log, said)
