"""Archives, as the catalog and the reader both open them: one file read from a ZIP or a RAR within a limit, whatever
its header claims; a comic's pages in reading order; and 7-Zip, which unpacks what RAR packed. Below meta (a comic's
details and cover), the reader (its pages) and the PDF worker (an EPUB's styles), which all stand on it, so that none
of them reaches into another for these, at the top or inside a function. The error they raise is here too: Unreadable,
which the reader's and the worker's errors are made of.

Only the standard library and the core at the top: the PDF worker, which every command imports, imports this.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

from . import paths, settings

IMAGES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
          ".bmp": "image/bmp"}
MAX_RATIO = 100  # unpacked to packed, above which a big member is a bomb, not a book


class Unreadable(Exception):
    """A file the reader can't open: broken, locked with a password, or not what its name says."""


# ---- ZIP and RAR -----------------------------------------------------------------------------

def not_a_bomb(info, limit: int) -> None:
    """A ZIP member that would unpack beyond limit, or to many times its packed size, is refused before unpacking."""
    if info.file_size > limit or (info.file_size > (32 << 20) and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
        raise Unreadable("A file inside the archive is far bigger than a book or a page can be.")


def read_member(z: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """One file from a ZIP, read no further than limit, whatever its header claims: stored or deflated only."""
    info = z.getinfo(name)
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise Unreadable("A file inside the archive is packed in a way the reader doesn't open.")
    not_a_bomb(info, limit)
    with z.open(info) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise Unreadable("A file inside the archive is far bigger than it says.")
    return data


def rar_member(r, name: str, limit: int) -> bytes:
    """One file from a RAR, no bigger than limit, and not a copy of another file (which RAR can store as a
    reference: a small archive whose files all unpack to the same big one)."""
    info = r.getinfo(name)
    if getattr(info, "file_redir", None) or info.is_symlink() or info.file_size > limit:
        raise Unreadable("A file inside the archive is a link, or far bigger than a page can be.")
    return r.read(info)


_rar_tool: list[str | None] = [None]  # the 7-Zip rarfile was set up with
_rar_lock = threading.Lock()
NO_TOOL = "hylly-reads-rar-with-7-zip-alone"  # a file beside 7-Zip that isn't there: where rarfile's other tools point


def open_rar(path: str):
    """A RAR archive, opened by rarfile with 7-Zip (sevenzip) as the one tool that unpacks it; Unreadable without one."""
    import rarfile

    tool = sevenzip()
    if not tool:  # rarfile would only say it found no tool
        raise Unreadable(no_sevenzip())
    with _rar_lock:  # set up once (rarfile runs 7-Zip to check it), and again only for another 7-Zip
        if _rar_tool[0] != tool:
            # Only that 7-Zip, by its full path, unpacks what's packed (pages stored as they are need no tool). A request
            # reading a page while the choice is made finds none chosen, and rarfile then tries its other tools: each a
            # full path that isn't there, never an unrar found on the PATH or in the folder the server was started from.
            nowhere = str(Path(tool).with_name(NO_TOOL))
            rarfile.UNRAR_TOOL = rarfile.UNAR_TOOL = rarfile.BSDTAR_TOOL = rarfile.SEVENZIP2_TOOL = nowhere
            rarfile.SEVENZIP_TOOL = tool
            rarfile.tool_setup(unrar=False, unar=False, bsdtar=False, sevenzip=True, sevenzip2=False, force=True)
            _rar_tool[0] = tool
    return rarfile.RarFile(path)


def _natural(name: str) -> list:
    """page2 before page10. The numbers are the runs of digits \\d finds, of any script (a full-width ２ is 2); a
    superscript or a circled digit is none to it, and stays a letter (isdecimal: isdigit would make the ² of page1²2 a
    number int() can't read, and stop the sort)."""
    return [int(part) if part.isdecimal() else part.lower() for part in re.split(r"(\d+)", name)]


def comic_pages(names) -> list[str]:
    """A comic's pages in reading order, from the names of what its archive holds: its pictures (IMAGES), page2 before
    page10, and nothing hidden (in a name or a folder starting with a dot), unless that's all there is, as in a comic
    whose pages are all in a folder such as ".Lantern Wolf v01". Never what a Mac adds (__MACOSX, "._" files), nor
    what leads out of the archive (".."). The one list the catalog's page count and cover and the reader's pages go by."""
    def picture(name: str) -> bool:
        name = name.replace("\\", "/")  # as a RAR made on Windows may part its folders
        path = PurePosixPath(name)  # a folder's name ends in /, which PurePosixPath leaves out
        return (not name.endswith("/") and path.suffix.lower() in IMAGES and ".." not in path.parts
                and not path.name.startswith("._") and not any(part.startswith("__MACOSX") for part in path.parts))

    def hidden(name: str) -> bool:
        return any(part.startswith(".") for part in PurePosixPath(name.replace("\\", "/")).parts)

    pictures = [n for n in names if picture(n)]
    return sorted([n for n in pictures if not hidden(n)] or pictures, key=lambda n: _natural(n.replace("\\", "/")))


# ---- 7-Zip -----------------------------------------------------------------------------------

def sevenzip() -> str:
    """7-Zip, which reads CBR pages, by its full path: [tools] sevenzip in the settings. Elsewhere than Windows a name
    alone there is looked for on the PATH, and none means 7z, else 7zz. "" when it isn't there."""
    given = settings.get("tools")["sevenzip"]
    if paths.WINDOWS:
        return given if Path(given).is_file() else ""
    if given.startswith("/"):
        found = given if Path(given).is_file() else ""
    else:
        found = next((found for name in ([given] if given else ["7z", "7zz"]) if (found := shutil.which(name))), "")
    return "" if found and _server_can_write(found) else found


def _server_can_write(path: str) -> bool:
    """In a place that a server with walls around it can change (examples/hylly.service): Hylly's own folders or the library.
    Your own commands run this tool too, outside those walls, so a flaw in the server mustn't get to choose it. The one
    rule, [bell] command's too (bell.server_can_write); the library looked at now, as a comic is read from it."""
    # Loaded only when a 7-Zip is found: the PDF worker, which imports this module, needs neither the alarms nor the
    # users' database that bell brings along. No cycle: bell is the core's, which imports nothing of the books'.
    from .bell import server_can_write

    return server_can_write(path, now=True)


# Which 7-Zip has the RAR part, under the unRAR licence's restriction (THIRD-PARTY-NOTICES.md), which some systems'
# packages leave out: without it only a CBR comic whose pages are stored uncompressed opens (doctor._tools).
_WITH_RAR = ("7-Zip from 7-zip.org has it on every system; on Debian and Ubuntu, the package 7zip-rar adds it to 7zip "
             "(p7zip-rar to the older p7zip-full; on Debian from non-free); Homebrew's sevenzip is built without it.")


CODECS = re.compile(r"^Codecs:[ \t\r]*$(.*?)(?=^\S[^\n]*:[ \t\r]*$|\Z)", re.M | re.S)  # in 7z i, up to the next section
RAR_CODEC = re.compile(r"\bRar[1-5]\b", re.I)  # Rar1 to Rar5 in it; the Formats section names Rar5 too, a format only
BANNER = re.compile(r"7-Zip[^\r\n]*?(\d{1,4})\.(\d{1,4})")  # its version, as updates.seven_zip_version reads it


def sevenzip_info(tool: str) -> tuple[tuple[int, int] | None, bool | None]:
    """Which 7-Zip this is, by its banner, and whether it has the RAR part that unpacks a compressed RAR, by its own
    list of what it unpacks with: one run of 7z i, which prints both. A package without the part, such as Debian's and
    Ubuntu's 7zip without 7zip-rar, lists no Rar1 to Rar5 among its Codecs, though its Formats name Rar and Rar5. None
    for what it doesn't say, both when it can't be run. Cheaper and as sure as unpacking a RAR of our own, which only
    RAR's own program can make. hylly doctor says it (doctor._tools), and the reader, for a comic it can't unpack
    (reader.count)."""
    try:
        with tempfile.TemporaryFile() as printed:  # not a pipe, which a program it starts could hold open beyond the time given
            subprocess.run([tool, "i"], stdout=printed, stderr=subprocess.DEVNULL, timeout=10, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            printed.seek(0)
            text = printed.read(1 << 18).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None, None
    version, listed = BANNER.search(text), CODECS.search(text)
    return ((int(version[1]), int(version[2])) if version else None,
            None if listed is None else bool(RAR_CODEC.search(listed[1])))


def no_sevenzip() -> str:
    """What to install when sevenzip() finds none."""
    if paths.WINDOWS:
        return ("Reading CBR comics needs 7-Zip on the computer the server runs on: install it from 7-zip.org, or set "
                "[tools] sevenzip in settings.toml to where its 7z.exe is.")
    return ("Reading CBR comics needs 7-Zip on the computer the server runs on: install it, so that 7z or 7zz is on "
            "the PATH, or set [tools] sevenzip in settings.toml. One in Hylly's own folders or the library doesn't count: "
            f"the server can write there, and your commands run it too. Most CBR comics also need its RAR part: {_WITH_RAR}")


def no_rar_part() -> str:
    """What to install when the 7-Zip sevenzip() finds has no RAR part (doctor._tools)."""
    if paths.WINDOWS:
        return "Install 7-Zip from 7-zip.org, which has it, or set [tools] sevenzip in settings.toml to its 7z.exe."
    return (f"Get a 7-Zip with its RAR part: {_WITH_RAR} Then run hylly doctor again. It asks 7z, else 7zz, on the "
            "PATH; to use another, such as 7-zip.org's 7zz beside a 7z without it, set [tools] sevenzip in settings.toml "
            "to it.")
