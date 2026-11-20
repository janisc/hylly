"""Saving and loading the shops' session cookies, and the keys of DriveThruRPG and the lookup sources.

Humble: ory_session_stoiccohens510ht9xek (its sign-in's session). StoryBundle: _storybundle31_session. DriveThruRPG: an application key from
the account settings. Metron, Comic Vine and Google Books (metadata lookups): an API token and API keys. itch.io: an
API key for the library, and the browser's login cookie (itchio) for the bundles' pages. All live next to each other
in the logins folder (paths.logins_dir: in the data folder as it comes), outside the library and the program, each in the file
sites.SITES names for it, and are never printed. Every function here is told which site: none is the default. Only the parts that talk to the shops and the lookup sources import this (parts.py); the library server's
walls hide the folder from it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths
from .paths import windows_tool
from .sites import KEYS, SITES  # noqa: F401  (the names: sites.py, for those who may not import this)
from .storage import atomic_write

MIN_LENGTH = {**{site: 16 for site in KEYS}, "itchio-bundles": 20}  # keys, and itch.io's cookie, are shorter than others


class AuthError(Exception):
    """The session cookie is missing, malformed or no longer accepted."""


class ShopError(RuntimeError):
    """A shop's answer that stops the work. It says in words what was asked for, never the address, because order keys
    travel in addresses and these messages get pasted into public places. A RuntimeError, as such stops always were.
    status: the shop's HTTP answer, as 403 and 429 say it's turning us away; 0 when there's none to tell. no_answer: the
    shop didn't answer at all, after every try (shopsession.ShopSession)."""

    def __init__(self, message: str, status: int = 0, *, no_answer: bool = False) -> None:
        super().__init__(message)
        self.status, self.no_answer = status, no_answer

    @property
    def passing(self) -> bool:
        """No answer at all, or the shop busy (429, 5xx): worth asking again later. Anything else, an error without a
        status too, is about the thing asked for (an item hidden, a listing that can't be read)."""
        return self.no_answer or self.status == 429 or self.status >= 500


LONGEST_WAIT = 120.0  # seconds of a shop's Retry-After waited out on the spot; longer, and that request ends


def retry_after(r, now: float | None = None) -> float | None:
    """A server's Retry-After in seconds, the one reading of it for the shops, the lookups and the downloads: a number
    of seconds ("120", "1.5") or a date, never less than 0. None when there is none, or it can't be read; a caller that
    wants 0 then writes `or 0.0`. now: the time (time.time()) a date is counted from, for a test's clock."""
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    value = (r.headers.get("retry-after") or "").strip()
    if not value:
        return None
    if value.isascii() and value.replace(".", "", 1).isdigit():  # "²" passes isdigit, not float()
        return float(value)  # hundreds of digits make it infinite: longer than anything is waited out
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:  # "-0000": the time is UTC, the sender's own zone unknown
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def session_path(site: str) -> Path:
    """Where the site's login is kept: in the logins folder (paths.logins_dir), as <site>-session.txt for a cookie
    and <site>-key.txt or -token.txt for a key."""
    return paths.logins_dir() / SITES[site][1]


def normalize(value: str, site: str) -> str:
    """Accept the bare value or a pasted 'name=value;' fragment. Quotes are part of the value."""
    name = SITES[site][0]
    value = value.strip().rstrip(";").strip()
    if value.startswith(name + "="):
        value = value[len(name) + 1 :]
    return value


def looks_like_cookie(value: str, site: str) -> bool:
    """One long run of printable characters without spaces, so random clipboard text is never sent."""
    return MIN_LENGTH.get(site, 60) <= len(value) <= 4000 and all(33 <= ord(c) < 127 for c in value)


CLIPBOARDS = {"darwin": [["pbpaste"]],
              "other": [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]}


def clipboards(platform: str = sys.platform) -> list[list[str]]:
    """What reads the clipboard here, the first that works. On Windows, PowerShell by its full path: by its name, Windows
    would look in the current folder first."""
    if platform == "win32":
        return [[windows_tool("powershell"), "-NoProfile", "-Command", "Get-Clipboard -Raw"]]
    return CLIPBOARDS.get(platform, CLIPBOARDS["other"])


def read_clipboard(platform: str = sys.platform) -> str:
    for tool in clipboards(platform):
        try:
            out = subprocess.run(tool, capture_output=True, text=True, errors="replace", timeout=30)  # a picture, say: refused later
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            return out.stdout or ""
    raise AuthError("Nothing here can read the clipboard (on Linux, wl-paste, xclip or xsel can); run 'hylly login SITE' and paste instead.")


def usable(value: str, site: str) -> str:
    """The value to save and send, or AuthError if it isn't one: random clipboard text goes nowhere."""
    value = normalize(value, site)
    if not looks_like_cookie(value, site):
        what = f"an {SITES[site][0]}" if site in KEYS else "a session cookie"
        raise AuthError(
            f"That does not look like {what} (expected one long value without spaces). "
            f"Copy only the value of {SITES[site][0]} and try again."
        )
    return value


def save_session(value: str, site: str) -> Path:
    path = session_path(site)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, usable(value, site), mode=0o600)
    return path


def forget_session(site: str) -> bool:
    """The site's saved login or key deleted (hylly login SITE --forget): True if there was one. The site still takes it
    until it's ended there, and an environment variable set for it still stands in for it."""
    try:
        session_path(site).unlink()
    except FileNotFoundError:
        return False
    return True


def _what(site: str) -> str:
    return SITES[site][0] if site in KEYS else "session cookie"


def load_session(site: str) -> str:
    """The site's environment variable if set, otherwise the saved file."""
    value = os.environ.get(SITES[site][2])
    if not value:
        path = session_path(site)
        if not path.is_file():
            raise AuthError(f"No {site} {_what(site)} saved. Run 'hylly login {site}' first (it is stored in {path}).")
        value = path.read_text(encoding="utf-8")
    value = normalize(value, site)
    if not value:
        raise AuthError(f"The saved {site} {_what(site)} is empty. Run 'hylly login {site}' again.")
    return value
