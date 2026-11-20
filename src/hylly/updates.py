"""What Hylly stands on, and whether it is new enough. How it's kept up to date, in one place:

- pyproject.toml has a floor for each requirement (constraint-dependencies for those below them). A floor goes up when
  a release fixes a flaw Hylly can reach, with the reason beside it.
- uv.lock says what an install gets. .github/workflows/audit.yml checks it against the published flaws every week and
  on every change to it, and a finding fails the run.
- Here: what is actually in use, as a program installed earlier stays on what it had until it's synced, and Python
  and 7-Zip come from elsewhere. hylly doctor, the server's log and its page say so, with the next step. Nothing is
  refused for it, and nothing here goes online: the versions wanted move with Hylly's own updates.

The numbers below are those of PACKAGES in pyproject.toml (a test keeps them the same).
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from datetime import date
from importlib import metadata
from pathlib import Path

from . import INSTALL

# The file-reading ones, as those are what a book from anywhere reaches: name, the oldest accepted, why.
PACKAGES = {
    "pymupdf": ("PyMuPDF", "2.1", "Older ones draw PDFs with a MuPDF that a crafted PDF can make write past its memory (CVE-2026-3308)."),
    "pillow": ("Pillow", "13.0", "Older ones have flaws in reading pictures, which come out of comics and books."),
}
# The oldest release of each Python with the fixes in reading ZIP files (CVE-2025-8291) and XML: what books are made of.
PYTHONS = {(3, 11): (3, 11, 14), (3, 12): (3, 12, 12), (3, 13): (3, 13, 10), (3, 14): (3, 14, 1)}
PYTHON_ENDS = {(3, 11): date(2027, 10, 31), (3, 12): date(2028, 10, 31), (3, 13): date(2029, 10, 31),  # no fixes after these
               (3, 14): date(2030, 10, 31), (3, 15): date(2031, 10, 31)}
SEVEN_ZIP = (26, 2)  # fixes a flaw where a crafted archive could overrun its memory, and so run code (CVE-2026-14266)


def numbers(version: str) -> tuple[int, ...]:
    """A version to compare: 2.1 as (2, 1, 0, 0), and a test release of it, 2.1rc1, just before that. What isn't a version
    reads as very old."""
    found = re.match(r"\s*(\d{1,9})(?:\.(\d{1,9}))?(?:\.(\d{1,9}))?(.*)", version, re.S)
    if not found:
        return (0, 0, 0, 0)
    test = re.match(r"\.?(a|b|c|rc|alpha|beta|pre|dev)", found[4], re.I)
    return (int(found[1]), int(found[2] or 0), int(found[3] or 0), -1 if test else 0)


HERE = Path(__file__).resolve().parent  # this program's own folder: src/hylly in a checkout of the source


def _is_a_tool() -> bool:
    """Installed with uv tool install, whose libraries are its own, and not run from a checkout of the source by uv run."""
    return (Path(sys.prefix) / "uv-receipt.toml").is_file()


def _from_a_checkout() -> bool:
    """Run from a checkout of the source, as uv run and an editable install run it: its pyproject.toml beside src. Not an
    installed package, whose folder is in a site-packages (a container's, say), with no checkout to go to."""
    return HERE.parent.name == "src" and (HERE.parent.parent / "pyproject.toml").is_file()


def install_command() -> str:
    """Hylly installed, or installed again over itself, from the project's own address, with the versions of its libraries
    that it was tested with and without what it never uses (hylly.INSTALL): the help and the README give the same."""
    return INSTALL


def _checkout() -> str | None:
    """The checkout of the source a uv tool install --editable runs from, as its receipt says; None for any other."""
    try:
        receipt = tomllib.loads((Path(sys.prefix) / "uv-receipt.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = [r.get("editable") for r in receipt.get("tool", {}).get("requirements", []) if isinstance(r, dict) and r.get("name") == "hylly"]
    return str(found[0]) if found and found[0] else None


def _sync(python: str = "") -> str:
    """How this program gets newer packages, or with python another Python (uv fetches it): the install again, as uv tool
    upgrade keeps the versions it was given (tool-constraints.txt), and from a git address would take the newest Hylly
    under them, untried; for one installed from a checkout, in that checkout. An installed package, from neither, is
    updated as it was installed, which only its owner knows."""
    with_python = f" --python {python}" if python else ""
    again = re.sub(r" --python \S+", with_python, install_command(), count=1) if python else install_command()
    if not _is_a_tool():
        if _from_a_checkout():
            return f"In Hylly's checkout: uv sync{with_python}"
        return (f"Hylly here is an installed package, not uv's tool nor a checkout of its source: update it as it was installed "
                f"(a newer container image, say), with newer libraries{f' and Python {python}' if python else ''}; or install "
                f"it as the help says: {again}")
    checkout = _checkout()
    if checkout:
        return (f"In {checkout}: uv tool install --force{with_python} --editable . --overrides tool-overrides.txt "
                "--constraints tool-constraints.txt")
    return again


def _finding(what: str, have: str, want: str, why: str, then: str) -> dict:
    return {"what": what, "have": have, "want": want, "why": why, "next": then}


def _packages(installed=metadata.version) -> list[dict]:
    out = []
    for name, (label, floor, why) in PACKAGES.items():
        try:
            have = installed(name)
        except metadata.PackageNotFoundError:
            continue  # what is missing altogether shows as soon as it's wanted
        if not isinstance(have, str):  # an installation in pieces says None
            continue
        if numbers(have) < numbers(floor):
            out.append(_finding(label, have, floor, why, _sync()))
    return out


def _python(have: tuple[int, ...] | None = None, today: date | None = None) -> list[dict]:
    have, today = have or tuple(sys.version_info[:3]), today or date.today()
    minor, shown = have[:2], ".".join(map(str, have))
    newest = ".".join(map(str, max(PYTHONS)))  # the newest Python whose fixes the table knows: it moves on with the table
    then = _sync(newest) + " (uv fetches that Python)."
    if minor in PYTHON_ENDS and today > PYTHON_ENDS[minor]:
        return [_finding("Python", shown, "a Python that still gets fixes", f"Python {minor[0]}.{minor[1]} gets no more security fixes.", then)]
    floor = PYTHONS.get(minor)
    if floor and have < floor:
        if minor == max(PYTHONS):  # the install's own --python: alone, it would take this same one again, as uv has it
            then = (f"uv python install --upgrade {newest} first, which brings uv's own newest {newest}, as the install again "
                    f"alone would take this {shown} again; then: {_sync(newest)}.")
        return [_finding("Python", shown, ".".join(map(str, floor)), "Older ones lack fixes in reading ZIP files and XML, which is what books are made of. "
                         "A Python from your system's own packages may have them under the older number.", then)]
    return []


def seven_zip_version(tool: str) -> tuple[int, int] | None:
    """Which 7-Zip this is, from what it prints with nothing to do: '7-Zip (z) 26.02 (x64)', '7-Zip [64] 16.02'. None if
    it doesn't say."""
    try:
        with tempfile.TemporaryFile() as printed:  # not a pipe, which a program it starts could hold open beyond the time given
            subprocess.run([tool], stdout=printed, stderr=subprocess.DEVNULL, timeout=10, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            printed.seek(0)
            text = printed.read(4096)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(rb"7-Zip[^\r\n]*?(\d{1,4})\.(\d{1,4})", text)
    return (int(found[1]), int(found[2])) if found else None


def _seven_zip() -> list[dict]:
    from . import archives

    try:
        tool = archives.sevenzip()
    except SystemExit:  # a mistake in the settings, which is told about in its own place
        return []
    have = seven_zip_version(tool) if tool else None
    if not have or have >= SEVEN_ZIP:
        return []
    then = "Get the newest from 7-zip.org."
    if sys.platform != "win32":
        then += " A distribution's own package may have the fix under the older number."
    return [_finding("7-Zip", f"{have[0]}.{have[1]:02d}", f"{SEVEN_ZIP[0]}.{SEVEN_ZIP[1]:02d}",
                     "Older ones have a flaw where a crafted archive could run code on this computer (CVE-2026-14266).", then)]


def behind() -> list[dict]:
    """What is older than it should be: {what, have, want, why, next} for each."""
    return [*_packages(), *_python(), *_seven_zip()]


@functools.lru_cache(maxsize=1)
def behind_once() -> tuple[dict, ...]:
    """The same for a running server, which asks often: none of these change while it runs. (A package may, but it
    only comes into use with a restart.) Whatever trips it up, the server's page goes on."""
    try:
        return tuple(behind())
    except Exception:  # noqa: BLE001
        return ()


def line(found: dict) -> str:
    return f"Worth updating: {found['what']} {found['have']}, older than {found['want']}. {found['why']} {found['next']}"


def say(log: Callable[[str], None]) -> None:
    """The server's job "update check", as it starts (periodic.py): a line in its log for each that's behind."""
    for found in behind_once():
        log(line(found))
