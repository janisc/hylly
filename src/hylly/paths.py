"""Where things live: the library root, its subfolders, the shops' logins and your own settings.

Everything the tool records is relative to the library root, so the library can move
(for example to a network share under another drive letter) by pointing --library or
HYLLY_LIBRARY at the new place.

What Hylly keeps of its own on this computer, outside the library, goes by kind, each kind in a folder of its own
(PLACES, place): settings.toml with the rules, the themes and the alarms' secrets (config), the database (data), the
logs, locks and notes (state), the reader's cache (cache) and the shops' logins (logins). See settings.example.toml.
Environment variables override the file.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .exits import NOT_SET_UP, Exit
from .storage import share_is_gone

WINDOWS = os.name == "nt"  # whose rules apply, for names and settings alike: the one switch, which the tests turn to try the others'
SYSTEM = "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"  # whose folders (PLACES): tests turn it
WINDOWS_TOOLS = {"powershell": ("WindowsPowerShell", "v1.0", "powershell.exe"), "schtasks": ("schtasks.exe",)}  # in System32
CLUTTER_FOLDERS = frozenset({  # the systems' own folders, in lower case: never copied (hyllycopy), never read for books (catalog)
    ".spotlight-v100", ".trashes", ".fseventsd", ".temporaryitems", ".documentrevisions-v100", ".appledouble",
    "$recycle.bin", "system volume information", "@eadir", "#recycle", "lost+found"})
TOOLS_FOLDERS = frozenset({  # tools' folders beside your books, in lower case: never read for books (catalog), but copied
    ".git", ".svn", ".hg",  # version control's
    ".trash", ".caltrash",  # trash: a desktop's in your home folder, Calibre's (a disk's of each user, .Trash-1000: catalog)
    ".stversions", ".stfolder", ".sync", ".dropbox.cache"})  # sync tools': Syncthing's old copies and marker, Resilio's, Dropbox's


def windows_tool(name: str) -> str:
    """One of Windows' own programs, by its full path in System32. By its name alone, Windows looks in the current folder
    first, where whoever can write that folder (a share, Downloads) could have put a program of the same name. Not looked
    for here: one that isn't there fails to start just as a name that isn't found does. The same answer on every system,
    so that the tests can see it."""
    root = os.environ.get("SystemRoot") or ""
    return ntpath.join(root if ntpath.isabs(root) else r"C:\Windows", "System32", *WINDOWS_TOOLS[name])


# Where Hylly keeps its own files on this computer, by kind: what each holds, its own variable, its folder inside the one
# folder of all in one, and each system's default. Each kind's folder is (place):
#   1. HYLLY_HOME, all in one: every kind in that one folder, the logins and the cache in folders of their own inside it.
#      It wins over the kinds' own variables, so that one variable keeps a Hylly tried apart (a test's, the demo's, an
#      assistant's) whole;
#   2. the kind's own variable, HYLLY_CONFIG_DIR and the rest, a folder by its full path;
#   3. the one folder an install made before Hylly kept its files apart (_old_place, holding any of OLD_SIGNS), used
#      all in one as HYLLY_HOME naming it would be: nothing of an install moves. Decided once for each process
#      (one_folder), so that a moment the folder can't be seen never sends a write elsewhere;
#   4. the system's default: Linux's XDG folders (each $XDG_..._HOME as set, if it's a full path), macOS's Library,
#      Windows' roaming AppData for the settings and its local one for the rest. On Windows the rest go in folders inside
#      %LOCALAPPDATA%\Hylly, never in it, as files of Hylly's there would make it the folder of before.
# The logins are in the data folder unless they're given a place of their own.
PLACES = {
    "config": ("settings.toml, the rules, your themes and the alarms' secrets", "HYLLY_CONFIG_DIR", "",
               {"linux": "$XDG_CONFIG_HOME/hylly", "mac": "~/Library/Application Support/Hylly", "windows": "%APPDATA%/Hylly"}),
    "data": ("the database, hylly.db, with its copies from before an update and the token that ties a library's records to "
             "this install (records-token), and HyllyCopy's record of where it copied",
             "HYLLY_DATA_DIR", "",
             {"linux": "$XDG_DATA_HOME/hylly", "mac": "~/Library/Application Support/Hylly", "windows": "%LOCALAPPDATA%/Hylly/data"}),
    "state": ("the logs, the locks and notes of the server and the jobs, the alarms' memory, and what long jobs said",
              "HYLLY_STATE_DIR", "",
              {"linux": "$XDG_STATE_HOME/hylly", "mac": "~/Library/Application Support/Hylly/state",
               "windows": "%LOCALAPPDATA%/Hylly/state"}),
    "cache": ("the reader's cache: the pages drawn and the comics unpacked", "HYLLY_CACHE_DIR", "cache",
              {"linux": "$XDG_CACHE_HOME/hylly", "mac": "~/Library/Caches/Hylly", "windows": "%LOCALAPPDATA%/Hylly/cache"}),
    "logins": ("the shops' logins and the lookup sources' keys", "HYLLY_LOGINS_DIR", "logins",
               {"linux": "<data>/logins", "mac": "<data>/logins", "windows": "<data>/logins"}),
}
XDG = {"XDG_CONFIG_HOME": ".config", "XDG_DATA_HOME": ".local/share", "XDG_STATE_HOME": ".local/state", "XDG_CACHE_HOME": ".cache"}
APPDATA = {"APPDATA": "AppData/Roaming", "LOCALAPPDATA": "AppData/Local"}  # Windows' own, where the variable isn't set
OLD_SIGNS = ("settings.toml", "hylly.db", "logins", "secrets", "rules", "themes")  # what makes the folder of before Hylly's
SETTINGS_SIGNS = ("settings.toml", "secrets", "rules", "themes")  # of those, what a data folder never holds
NAMES = {"config": "the settings folder", "data": "the data folder", "state": "the state folder",
         "cache": "the cache folder", "logins": "the logins folder"}  # each kind as people read it: the help's words
ONE_FOLDER = "one-folder"  # where a place came from: an install's one folder, all in one
FROM = {"HYLLY_HOME": "HYLLY_HOME, all in one", ONE_FOLDER: "the one folder this install has, all in one",
        "default": "this system's default"}  # where a place came from, in words (places); a kind's own variable by its name
_one_folder: dict[tuple, bool] = {}  # one_folder's answers for this process, by what they were worked out from


def _old_place(usual: bool = False) -> Path:
    """Where every install kept everything, all in one, before the kinds had folders of their own: %LOCALAPPDATA%\\Hylly on
    Windows, ~/.local/share/Hylly on Linux and macOS, whatever XDG_DATA_HOME may say there. usual: on Windows by the
    profile's own AppData, whatever LOCALAPPDATA says (usual_settings_folder)."""
    if SYSTEM != "windows":
        return Path.home() / ".local" / "share" / "Hylly"
    if usual:
        return Path.home() / "AppData" / "Local" / "Hylly"
    return Path(_env("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Hylly"  # as it always was, without the variable


def holds_hylly(folder: Path, signs: tuple[str, ...] = OLD_SIGNS) -> bool:
    """Whether a folder holds Hylly's own files, as an install's one folder of all in one does: any of OLD_SIGNS. A folder
    that is there but empty, as a first command of an older Hylly left it, holds nothing to keep."""
    try:
        return folder.is_dir() and any(os.path.lexists(folder / sign) for sign in signs)
    except OSError:  # one that can't be looked at: not taken for Hylly's
        return False


def _same_folder(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except (OSError, ValueError):  # either not there: not the same
        return False


def one_folder(usual: bool = False) -> Path | None:
    """An install's one folder (_old_place) when it holds Hylly's files, to be used all in one; else None. Decided once
    for each process, by what it is worked out from (the system, the home folder, LOCALAPPDATA and XDG_DATA_HOME), so
    that a moment the folder can't be seen, a share's or a disk's hiccup, never sends one write to the folders by kind;
    a test's other home or variables still count. On a disk that doesn't tell capitals from small letters, Linux's data
    folder, ~/.local/share/hylly, is the same as ~/.local/share/Hylly: it counts as an install's one folder then only
    while it holds what a data folder never does, the settings (SETTINGS_SIGNS). A process started before the folder can
    be seen (a home folder on the network, mounted late) keeps the folders by kind for as long as it runs: the folder
    must be there as Hylly starts. usual: by the profile's own AppData on Windows (usual_settings_folder)."""
    key = (SYSTEM, str(Path.home()), _env("LOCALAPPDATA"), _env("XDG_DATA_HOME"), usual)
    if key not in _one_folder:
        folder = _old_place(usual)
        data = _default(PLACES["data"][3][SYSTEM], usual)
        _one_folder[key] = holds_hylly(folder, SETTINGS_SIGNS if _same_folder(folder, data) else OLD_SIGNS)
    return _old_place(usual) if _one_folder[key] else None


def forget_one_folder() -> None:
    """one_folder looks again: for the tests, which make and take away such folders as they go."""
    _one_folder.clear()


def _default(template: str, usual: bool) -> Path:
    """A default of PLACES as a path: ~ the home folder, $XDG_..._HOME as set if it's a full path (else its default under
    the home folder), %APPDATA% and %LOCALAPPDATA% as set (else the profile's own; usual: always the profile's own), and
    <data> the data folder."""
    head, _, rest = template.partition("/")
    if head == "~":
        base = Path.home()
    elif head.startswith("$"):
        given = _env(head[1:])
        base = Path(given) if given and os.path.isabs(given) else Path.home().joinpath(*XDG[head[1:]].split("/"))
    elif head.startswith("%"):
        given = "" if usual else _env(head.strip("%"))
        base = Path(given) if given else Path.home().joinpath(*APPDATA[head.strip("%")].split("/"))
    else:
        base = _place("data", usual)[0]
    return base.joinpath(*rest.split("/")) if rest else base


def _place(kind: str, usual: bool = False) -> tuple[Path, str]:
    """A kind's folder and where it came from: HYLLY_HOME, the kind's own variable, ONE_FOLDER or "default". usual: as it
    would be with none of Hylly's variables set."""
    _, variable, inside, defaults = PLACES[kind]
    home = "" if usual else _env("HYLLY_HOME")
    if home:
        return Path(home, inside) if inside else Path(home), "HYLLY_HOME"
    own = "" if usual else _env(variable)
    if own:
        return Path(own), variable
    old = one_folder(usual)
    if old is not None:
        return (old / inside if inside else old), ONE_FOLDER
    return _default(defaults[SYSTEM], usual), "default"


def place(kind: str) -> Path:
    """The folder of one kind of Hylly's own files on this computer (PLACES): "config", "data", "state", "cache" or
    "logins"."""
    return _place(kind)[0]


def places() -> list[dict]:
    """Every kind's folder, its name as people read it (NAMES), what it holds and where it came from: HYLLY_HOME, the
    kind's own variable by its name, ONE_FOLDER or "default", and that in words (said). For hylly settings places and
    the doctor."""
    found = []
    for kind, (holds, variable, _, _) in PLACES.items():
        folder, why = _place(kind)
        found.append({"kind": kind, "name": NAMES[kind], "folder": str(folder), "holds": holds, "from": why,
                      "said": FROM.get(why, why), "variable": variable})
    return found


def all_in_one() -> Path | None:
    """The one folder that holds the kinds of Hylly's files that no variable of their own takes elsewhere: HYLLY_HOME's,
    which holds every kind, or an install's one folder (one_folder). None when the kinds have folders of their own."""
    home = _env("HYLLY_HOME")
    if home:
        return Path(home)
    return one_folder() if any(_place(kind)[1] == ONE_FOLDER for kind in PLACES) else None


def config_dir() -> Path:
    """The settings folder: settings.toml, the rules, your themes and the alarms' secrets (place)."""
    return place("config")


# The names Windows keeps for its devices, in capitals (windows_cant_hold): COM and LPT with a digit, 0 as Microsoft
# lists it, and with a superscript 1, 2 or 3, as Windows before 11 and Python's ntpath.isreserved take them
WINDOWS_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                             *(f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789\u00b9\u00b2\u00b3")})


def windows_cant_hold(part: str) -> bool:
    """Whether Windows takes a name, one part of a path, for one of its devices (WINDOWS_DEVICES), as it reads one: by
    what comes before its first dot, with the spaces at the end of that left out, in capitals (CON, con.epub, NUL .pdf,
    Nul. Points, COM0.cbz). So a name that ends in dots or spaces counts as without them. Windows 11 holds some of these
    (con.epub), earlier Windows none; on Linux and macOS they are ordinary names. The one test of it: every shop's
    names (clean, safe_filename), the server's (renamed_on_windows) and a copy's (hyllycopy.name_problem)."""
    return part.split(".", 1)[0].rstrip(" ").upper() in WINDOWS_DEVICES


def not_a_device(name: str) -> str:
    """A name Windows would take for a device (windows_cant_hold) with a _ after that part, which makes it an ordinary
    name everywhere: CON is CON_, con.epub con_.epub, Nul. Points Nul_. Points; any other name as it is."""
    if not windows_cant_hold(name):
        return name
    stem, dot, rest = name.partition(".")
    return f"{stem}_{dot}{rest}"


def elsewhere_on_windows(rel: str) -> bool:
    """Whether Windows would take a path in the library, by its words, for a place elsewhere: a backslash makes
    \\\\other-pc\\share of it, which Windows would log on to with this computer's credentials, and a colon a drive or a
    hidden part of a file. On Linux and macOS both are ordinary letters in a name ('Moonwake: Second Tide.epub')."""
    return "\\" in rel or ":" in rel


def renamed_on_windows(rel: str) -> bool:
    """Whether Windows would quietly take a part of a path in the library for another name: one ending in a dot or a
    space, which it drops, or one named as a device (windows_cant_hold: CON, NUL, COM1, whatever follows a dot). On
    Linux and macOS these are ordinary names (a folder S.H.E.L.F.). Only the server refuses these (serve.library_path),
    not a record's path (inside_library): Windows 11 holds such names, and a record left out there would take its title
    out of a build."""
    return any(part != part.rstrip(". ") or windows_cant_hold(part) for part in rel.split("/"))


def inside_library(rel) -> bool:
    """Whether a record's path names a place inside the library, by its words alone: relative, with forward slashes,
    no . or .. in it, and on Windows nothing that leads elsewhere (elsewhere_on_windows). What the records say is
    only Hylly's own writing, but the walled server may write them too, and nothing it puts there may lead a command
    out of the library."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or any(ord(ch) < 32 or ch == "\x7f" for ch in rel):
        return False
    if WINDOWS and elsewhere_on_windows(rel):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def library_file(root: Path, rel, resolved: bool = False) -> Path | None:
    """A record's file, as a path to open, only if it's inside the library: by its words (inside_library), and where
    any link on the way leads. None otherwise. As it is named in the library, which its name and its title go by; or
    resolved, its links followed, for the server, which then looks at what it really is (serve.library_path)."""
    if not inside_library(rel):
        return None
    f = Path(root) / rel
    try:
        target = f.resolve()
        if not target.is_relative_to(Path(root).resolve()):
            return None
    except (OSError, RuntimeError):
        return None
    return target if resolved else f


class LibraryFiles:
    """A record's files as one catalog build looks at them: library_file's answer, each with what the file is (its
    os.stat, links followed). On a share every look at a path is a round trip, and library_file resolves the library's
    root and every folder on the way at each call; here the root is resolved once, each folder once, and the file itself
    only when it is a link (or on Windows another reparse point, such as a junction), which can lead anywhere: a file
    that is no link, in a folder whose place is inside the library, is inside it, so a link out is refused as ever. A
    file is looked at once for the build, as a title's main file is read for its details and then checked to open."""

    def __init__(self, root: Path):
        self.root = Path(root)
        try:
            self._root: Path | None = self.root.resolve()
        except (OSError, RuntimeError):  # library_file then finds no file inside it
            self._root = None
        self._inside: dict[str, bool] = {}  # each folder in the library, by its path there: whether it leads inside
        self._seen: dict[str, tuple[Path, os.stat_result]] = {}

    def _folder_inside(self, rel: str) -> bool:
        known = self._inside.get(rel)
        if known is None:
            try:
                known = self._root is not None and (self.root / rel).resolve().is_relative_to(self._root)
            except (OSError, RuntimeError):  # a loop of links, on Python 3.11 and 3.12
                known = False
            self._inside[rel] = known
        return known

    def file(self, rel) -> tuple[Path, os.stat_result] | None:
        """The file a record's path names, as library_file names it, with its os.stat; None when library_file would
        give none, or the file can't be looked at just now (OSError), which isn't kept: the next look tries again."""
        seen = self._seen.get(rel) if isinstance(rel, str) else None
        if seen is not None:
            return seen
        if not inside_library(rel) or self._root is None or not self._folder_inside(rel.rpartition("/")[0]):
            return None
        f = self.root / rel
        try:
            st = os.lstat(f)
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0):  # where it leads, as library_file looks
                if not f.resolve().is_relative_to(self._root):
                    return None
                st = os.stat(f)
        except (OSError, RuntimeError):
            return None
        self._seen[rel] = (f, st)
        return f, st


def job_output_dir() -> Path:
    """Where the jobs started from another computer keep what they say (remote.as_job): a folder the walled server can't
    see (docs/architecture.md), so that nothing put there can lead a job's output into a file of yours. In the state
    folder when the kinds have folders of their own, where the walls hide it as they hide the logins
    (examples/hylly.service). All in one (HYLLY_HOME, or the folder of an earlier install), outside that folder, which the
    walled server may write: ~/.local/state/hylly/job-output on Linux and macOS; on Windows beside it and named after it,
    Hylly-jobs for the usual one, so that two such folders side by side (HYLLY_HOME naming each) don't tidy away each
    other's. Each test has one of its own (tests/conftest.py)."""
    folder, why = _place("state")
    if why not in ("HYLLY_HOME", ONE_FOLDER):
        return folder / "job-output"
    if SYSTEM == "windows":
        folder = Path(os.path.abspath(folder))  # with no .. left in it, which would give the name
        return folder.parent / f"{folder.name or 'Hylly'}-jobs"
    return Path.home() / ".local" / "state" / "hylly" / "job-output"


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # a link that leads round in a circle, say: then as it's written
        return path.absolute()


def usual_settings_folder(settings_file_counts: bool = True) -> bool:
    """Whether this is this user's usual Hylly, and not one set up to try things on, by a test or an assistant: what
    belongs to the whole user (the start at logon, a system-wide service's say) isn't for those to change. Told by
    HYLLY_HOME and the settings folder alone: a Hylly is another when HYLLY_HOME, HYLLY_CONFIG_DIR or on Windows
    LOCALAPPDATA or APPDATA names another place. A variable of the data, the state, the cache or the logins doesn't make
    it another: it's the user's own way of keeping that kind, which the start at logon carries (instance.py). The usual
    places are those with none of Hylly's variables set: an install's one folder, there yet or not, or the settings
    folder where this system keeps it (on Windows by the profile's own AppData). Links and .. are followed, so the usual
    one written another way, HYLLY_HOME naming the one folder, say, is still the usual one.
    settings_file_counts: whether a settings.toml of its own (HYLLY_SETTINGS) makes it another too. It does for the start
    at logon, which would start a server without that file. It doesn't for a system-wide service with walls: the lock
    and the server's note are in the state folder whatever the file, so a server started that way would replace the one
    in the walls."""
    if settings_file_counts and _env("HYLLY_SETTINGS"):
        return False
    together = _resolved(_old_place(usual=True))
    home = _env("HYLLY_HOME")
    if home:
        return _resolved(Path(home)) == together
    return _resolved(config_dir()) in (together, _resolved(_default(PLACES["config"][3][SYSTEM], usual=True)))


def keep_private() -> None:
    """On Linux and macOS, Hylly's own folders are yours alone: they hold logins, keys, password hashes, what people read
    and the pages of their books. Each is made if it isn't there, so that a walled service finds every folder its walls
    name, without a - that would let it start without them: the cache, the logins and, with the kinds apart, the folder
    for what long jobs said, which the walls hide from the server, too. What in them others could read is put right.
    One that can't be made here, as inside walls that leave it out, is no reason to stop: what needs it says so. On
    Windows their place in your profile already sees to that."""
    if os.name == "nt":
        return
    folders = list(dict.fromkeys(place(kind) for kind in ("config", "data", "state", "cache", "logins")))
    if _place("state")[1] not in ("HYLLY_HOME", ONE_FOLDER):  # all in one, it's outside the one folder, out of the walls
        folders.append(job_output_dir())
    for folder in folders:
        with contextlib.suppress(OSError):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for folder in folders:
        with contextlib.suppress(OSError):  # one walled in read-only or hidden, say, or a disk without permissions:
            # the others still. One inside another (the logins, what long jobs said), where a walled server may have
            # left a link in its place, is never followed: a link may lead anywhere
            inside = any(other != folder and folder.is_relative_to(other) for other in folders)
            if not folder.is_dir() or (inside and folder.is_symlink()):
                continue
            with contextlib.suppress(OSError):
                os.chmod(folder, 0o700)
            for path in folder.iterdir():
                with contextlib.suppress(OSError):  # and one that can't be changed is no reason to leave the others
                    if path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077:
                        os.chmod(path, path.stat().st_mode & 0o700)


def _env(name: str) -> str:
    return os.environ.get(name) or ""


AS_ROOT = ("Hylly doesn't run as root. What it writes would then be root's, out of your own reach, and a flaw in it or in a book it "
           "opens could do anything on this computer. Run it as your own user, without sudo. Where that truly can't be, "
           "HYLLY_ALLOW_ROOT=1 lets it.")


def is_root() -> bool:
    """On Linux or macOS, as root, by sudo or otherwise."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def running_as_root() -> bool:
    """As root, and not told that it has to be. Checked as every command starts, however it was started: an installer
    would only guard its own way in."""
    return is_root() and not _env("HYLLY_ALLOW_ROOT")


def settings_file() -> Path:
    return Path(_env("HYLLY_SETTINGS") or config_dir() / "settings.toml")


class NotToml(ValueError):
    """A TOML file people edit that can't be read (read_toml): not saved as UTF-8 (kind "encoding"), not TOML
    ("syntax"), or lists or tables inside each other too deeply ("depth"). Its words name the file and the line, where
    one can be told; each reader turns it into its own message (said, with a hint of its own after it)."""

    def __init__(self, path: Path, kind: str, line: int | None = None, detail: str = "") -> None:
        self.path, self.kind, self.line, self.detail = path, kind, line, detail
        super().__init__(self.said())

    def said(self, name: str | Path | None = None) -> str:
        """What is wrong, in one sentence, with the file by its path or by the name given (a theme's by its name)."""
        name = self.path if name is None else name
        if self.kind == "encoding":
            return f"{name} isn't saved as UTF-8 (line {self.line}): save it as UTF-8."
        if self.kind == "depth":
            return f"{name}: not readable as TOML (lists or tables inside each other too deeply)."
        return f"{name}: not readable as TOML ({self.detail})."


TOML_LINE = re.compile(r"\bat line (\d+)")  # where tomllib says a mistake is, in its message: "(at line 3, column 5)"
LINE_ENDS = re.compile(rb"\r\n|\r|\n")  # Windows', old Macs' (a CR alone) and the others'


def read_toml(path: Path) -> dict:
    """A TOML file people edit, as its tables: settings.toml, a rules file, tags.toml, a theme. A BOM at its start, as
    Notepad may write, is left out, and a CR alone, as an old Mac's editor ends a line, ends one here too, as reading
    tags.toml and themes as text always took it (TOML itself takes only LF and CRLF). NotToml when it isn't UTF-8 or
    TOML, naming the file and the line; OSError when it can't be read (FileNotFoundError when it isn't there), for each
    reader to say as it says it."""
    data = Path(path).read_bytes().removeprefix(b"\xef\xbb\xbf")
    try:
        return tomllib.loads(re.sub(r"\r(?!\n)", "\n", data.decode("utf-8")))
    except UnicodeDecodeError as e:
        raise NotToml(Path(path), "encoding", len(LINE_ENDS.findall(data[:e.start])) + 1) from None
    except tomllib.TOMLDecodeError as e:
        found = TOML_LINE.search(str(e))
        raise NotToml(Path(path), "syntax", int(found[1]) if found else None, str(e)) from None
    except RecursionError:
        raise NotToml(Path(path), "depth") from None


def _settings() -> dict:
    path = settings_file()
    if not path.is_file():
        return {}
    try:
        return read_toml(path)
    except NotToml as e:
        hint = " Paths go in single quotes: 'D:\\Books'." if e.kind == "syntax" else ""
        raise Exit(NOT_SET_UP, e.said() + hint) from None


def setting(name: str, env: str | None = None) -> str:
    """One personal setting: its environment variable if set, else settings.toml, else ''."""
    return (_env(env) if env else "") or str(_settings().get(name, ""))


def section(name: str) -> dict:
    """A [section] of settings.toml, such as [server]; empty if it is not there."""
    value = _settings().get(name)
    return value if isinstance(value, dict) else {}


def library_root(override: str | None = None) -> Path:
    """The library root: --library, then HYLLY_LIBRARY, then 'library' in settings.toml."""
    root = override or setting("library", "HYLLY_LIBRARY")
    if not root:
        raise Exit(NOT_SET_UP, f"No library set: add library = '<folder>' to {settings_file()} (see settings.example.toml), "
                         "set HYLLY_LIBRARY, or pass --library. For a new library: hylly init FOLDER; to try Hylly first: hylly demo.")
    return Path(root)


def state_dir(root: Path) -> Path:
    """What Hylly keeps beside the books. Each shop's own files in a folder of its own, named by its id (shops.Shop.state):
    its snapshots, its failures file and its working files (StoryBundle's left-out names, itch.io's answers and survey). The rest is
    every shop's or the library's, here: the downloads' log (download-log.txt), the library's markers (marker_file,
    db.owner_file), the database's and the settings' nightly copies (db-backup, settings-backup), the last backup's note
    (backup.json), the jobs' locks (locks), the lookups' answers and log (lookups, lookup-log.txt) and the Inbox's
    (inbox.json, inbox-log.txt). Keep private: snapshots contain order keys, and the copies everyone's records."""
    return root / "state"


def marker_file(root: Path) -> Path:
    return state_dir(root) / "library.json"


REQUIRE_MARKER = True  # tests of other things turn it off, for their small made-up libraries
SHARE_WAIT = 240  # seconds a long run waits for the library's network share to come back (library_missing's wait)
SHARE_ASK_EVERY = 15  # seconds between its looks for the share while it waits


def library_missing(root: Path, wait: Callable[[float], object] | None = None) -> bool:
    """Whether this folder isn't the library, or the library isn't there. On Linux and macOS an unmounted disk leaves an
    empty folder, so it looks like a library with nothing in it, and a mistyped --library anywhere looks the same:
    everything would be downloaded again, onto the wrong disk. The marker tells them apart. Trouble that passes doesn't
    count: this is asked during long runs.

    wait: for a long run (a download, hylly add), how to wait while the library's network share is gone (Windows says
    "not found" of a share that went away too: storage.share_is_gone), looking again every SHARE_ASK_EVERY seconds for
    up to SHARE_WAIT, before the library counts as missing: a NAS restarting its file sharing is gone for a moment.
    time.sleep, or a stop event's wait, whose True (the run stopping) ends the waiting, the share still gone."""
    if not REQUIRE_MARKER:
        return False
    looks = SHARE_WAIT // SHARE_ASK_EVERY if wait is not None else 0
    while True:
        try:
            marker_file(root).stat()
        except FileNotFoundError as e:
            if looks <= 0 or not share_is_gone(e) or wait(SHARE_ASK_EVERY):
                return True
            looks -= 1
            continue
        except OSError:
            pass
        return False


def typed(path) -> str:
    """A path as it's typed in a command a message suggests: in quotes when the shell needs them, as for a space
    ("C:\\My Books" on Windows; 'My Books' on Linux and macOS, as shlex.quote writes it)."""
    return subprocess.list2cmdline([str(path)]) if WINDOWS else shlex.quote(str(path))


def check_library(root: Path) -> None:
    """For what writes to the library or reads it for an answer: there or not, before anything starts."""
    if library_missing(root):
        raise Exit(NOT_SET_UP, f"No Hylly library at {root}. Is the disk or the share there, and is the path right? A "
                         f"library that was there is found again once they are: don't mark the folder for it. Only for a "
                         f"new library: hylly init {typed(root)}")


def catalog_dir(root: Path) -> Path:
    """The catalog as the last build wrote it for the library server (data.js), and its covers and cache."""
    return root / "catalog"


def logins_dir() -> Path:
    """The shops' logins and the lookup sources' keys: a folder of their own (place), so that the library server, which
    needs none of them, can be walled off from them alone (examples/hylly.service)."""
    return place("logins")


# ---- names in the library: the folders and files every shop's downloads go to ----------------------------------------

MAX_NAME = 50  # folder name length cap, keeps full paths under Windows' 260
MAX_FILE_NAME = 150  # file name length cap, with its extension


def clean(name: str) -> str:
    """A Windows- and NAS-safe folder name, for every shop's bundles, publishers, creators and titles: none Windows
    takes for a device (not_a_device), so a copy to a Windows disk takes it too (hyllycopy.name_problem)."""
    name = name.replace(":", " -")
    name = re.sub(r'[<>"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = not_a_device(name[:MAX_NAME].rstrip(". "))
    return name or "Untitled"


# The endings Hylly gives a file of its own while it is being written beside its place: a download's part (.part), a
# copy's (.copying: hylly add, seed; .seed before it), a small write's (.tmp). No shop's file is saved under one
# (safe_filename), so none is taken for another file's part, and a backup leaves them out (backup.BackingUp).
TEMPORARY = (".part", ".copying", ".seed", ".tmp")


def safe_filename(name: str) -> str:
    """A shop's own name for a file, with only the characters Windows cannot store replaced: every shop's files are
    saved under it. One ending in Hylly's own temporary endings (TEMPORARY) gets a _ after it: "x.pdf.part" is saved
    as "x.pdf.part_", or a shop's file could be taken for another's partial download (x.pdf's), and go on from it. None
    Windows takes for a device (not_a_device: CON.pdf is saved as CON_.pdf)."""
    name = not_a_device(re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". "))
    if name.lower().endswith(TEMPORARY):
        name += "_"
    suffix = PurePosixPath(name).suffix if len(PurePosixPath(name).suffix) <= 12 else ""
    keep = MAX_FILE_NAME - len(suffix)
    while len(name) > MAX_FILE_NAME or len(name.encode("utf-8")) > 240:  # some disks count bytes
        name = name[:len(name) - len(suffix)][:keep].rstrip(". ") + suffix
        keep -= 10
    name = not_a_device(name)  # cut short, it may be one again: CON, a long run of spaces and x.pdf is CON.pdf
    if name.lower().endswith(TEMPORARY):  # cut short to one, its own ending too long to keep
        name = name[:-1] + "_"
    return name or "file"
