"""How a command ends, so that a script or an assistant can tell what happened without reading the words. The table is
in the help page and in hylly commands."""

from __future__ import annotations

import contextlib
import re
import sys
import threading
import traceback

from . import PROJECT_URL

OK, PARTLY, USAGE, LOGIN, BUSY, NOT_SET_UP, LOCAL, NEEDS_UPDATE, NEEDS_YOU, INTERRUPTED = 0, 1, 2, 3, 4, 5, 6, 7, 8, 130
BUG = 70  # EX_SOFTWARE: what Hylly didn't foresee, which no retry puts right
ISSUES = f"{PROJECT_URL}/issues"
BUG_WORDS = (f"This is a bug in Hylly, not something you did: the details are just above, on standard error. Please report it "
             f"at {ISSUES}, with those lines (they hold no addresses or keys). What was done is kept.")

TABLE = {
    OK: "It did what was asked.",
    PARTLY: "It didn't all happen: some files failed, a shop or the network stopped it, or the answer is no (hylly verify found "
            "problems; hylly server status: not running). What was done is kept, and running it again goes on.",
    USAGE: "The command was mistyped or asks for something impossible. Nothing happened.",
    LOGIN: "A login is missing, has expired or was refused. Its owner runs hylly login SITE.",
    BUSY: "The same job is already running. Its message says where, and how to end it if it has hung.",
    NOT_SET_UP: "Something has to come first: no library at that place, no catalog or snapshot yet, or a mistake in the settings "
                "or rules. Its message says what.",
    LOCAL: "Trouble on this computer or its disks: not enough space, a share that's gone, a file it can't read or write, a port "
           "that's taken.",
    NEEDS_UPDATE: "A lookup source's answers have changed shape, so its plugin needs an update.",
    NEEDS_YOU: "It needs a person: a password, or a yes, that only someone at the keyboard can give. Nothing changed.",
    BUG: "It stopped by a bug in Hylly: something it didn't foresee went wrong. The details are on standard error, to report "
         f"at {ISSUES}. What was done is kept; running it again is likely to meet the same bug.",
    INTERRUPTED: "Stopped with Ctrl-C. What was done is kept, and running it again goes on.",
}


class Exit(SystemExit):
    """Ends the command with a code of the table and a message, which cli.main shows. str() gives the message, as with a
    SystemExit that has words."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(code)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _scrubbed(kind, value, tb) -> str:
    """A crash's report gets pasted into public places, and order keys travel in addresses: it keeps only the site's name."""
    text = "".join(traceback.format_exception(kind, value, tb))
    return re.sub(r"(https?://[^/\s'\"]+)[^\s'\"]*", r"\1/...", text)


def hide_addresses_in_tracebacks() -> None:
    """Every traceback scrubbed (_scrubbed), for hylly and hyllyd alike."""
    def report(kind, value, tb) -> None:
        sys.stderr.write(_scrubbed(kind, value, tb))

    sys.excepthook = report
    threading.excepthook = lambda a: report(a.exc_type, a.exc_value, a.exc_traceback)


def bug(e: BaseException) -> Exit:
    """What a command ends with when something it didn't foresee went wrong: its traceback, scrubbed, on standard error,
    and Exit(BUG) with one plain line, which cli.main shows after it. Not Partly's 1: a script would retry a bug."""
    with contextlib.suppress(OSError, ValueError):  # a closed standard error is no reason to hide the code
        sys.stderr.write(_scrubbed(type(e), e, e.__traceback__))
        sys.stderr.flush()
    return Exit(BUG, BUG_WORDS)
