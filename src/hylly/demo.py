"""hylly demo: a small library of made-up books to try things on, apart from your own: its own folder, settings, users and
server. Nothing in it comes from anywhere, so nothing is needed but this program: no logins, no books, no network. Its
owner, demo, is made with a password nobody is told: at a terminal, the server shows and opens a one-time link that logs
you in as demo, and hylly user link demo makes one at a terminal (into a pipe or a file only with --yes). Its server runs
here until Ctrl-C, or with --background on its own, for a script or an AI assistant, until hylly demo --stop, which stops
that one and no other."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import struct
import tomllib
import zipfile
import zlib
from pathlib import Path

from . import access, db, paths
from .exits import LOCAL, USAGE, Exit
from .storage import atomic_write, read_json

BOOKS = [  # folder, file's name, title, author, series, its number, description
    ("ebooks/Mysteries", "The Lantern Keepers", "The Lantern Keepers", "Ada Winterbourne", "The Lantern Keepers", "1",
     "A harbour town's lamps go out one by one, and the last keeper starts to count."),
    ("ebooks/Mysteries", "The Salt Ledger", "The Salt Ledger", "Ada Winterbourne", "The Lantern Keepers", "2",
     "A ledger washes ashore, in a hand everyone in town knows and nobody will name."),
    ("ebooks/Mysteries", "A Tide of Small Lies", "A Tide of Small Lies", "Ada Winterbourne", "The Lantern Keepers", "3",
     "The keepers are down to two, and only one of them is telling the truth."),
    ("ebooks/Science fiction", "Orbit of the Paper Moon", "Orbit of the Paper Moon", "Felix Ashdown", "", "",
     "A generation ship's archivist finds a moon on the charts that nobody launched."),
    ("ebooks/Science fiction", "The Quiet Engineers", "The Quiet Engineers", "Della Fairweather-Cole", "", "",
     "They keep the station running, and they have stopped answering questions."),
    ("ebooks/Handbooks", "Bread for Beginners", "Bread for Beginners", "Penny Baker", "", "",
     "Twelve loaves, in order of how forgiving they are."),
    ("ebooks/Handbooks", "A Field Guide to Imaginary Birds", "A Field Guide to Imaginary Birds", "Felix Ashdown", "", "",
     "Where to find them, what they eat, and why you should not feed them."),
]
COMICS = [("comics", f"Neon Rangers {n:03}", 8) for n in (1, 2, 3)] + [("comics", "The Clockwork Cat", 6)]
LINES = ("The lamp went out over the water, and nobody on the quay said a word about it until morning.",
         "She counted the steps twice, because the first count had come out wrong.",
         "By the time the tide turned, the ledger had been read by more people than would ever say so.",
         "He had kept the same notebook for eleven years, and it had only now started to disagree with him.",
         "Nothing in the room had moved, and that was what worried her.",
         "They were told to wait, so they waited, and after a while it stopped feeling like a choice.",
         "The map was right about the river and wrong about nearly everything else.",
         "Nobody remembered ordering the crates, but they were signed for all the same.",
         "It rained on the third day, which the almanac had said it wouldn't.",
         "The letter was short, polite and, as far as anyone could tell, from nobody at all.",
         "Whoever had built the stairs had not meant them to be climbed in the dark.",
         "In the end it was the small things that gave it away: the salt, the second cup, the door left on the latch.")
TINTS = ((1.0, .55, .35), (.4, .7, 1.0), (.5, .9, .6), (1.0, .8, .35), (.8, .5, 1.0), (.95, .45, .6), (.45, .9, .9))


def _png(width: int, height: int, shade, tint: tuple[float, float, float] | None = None) -> bytes:
    """A picture, shade(x, y) -> 0 to 255: grey, or tinted. With nothing but the standard library."""
    if tint:
        rows = b"".join(b"\0" + bytes(int(shade(x, y) * c) for x in range(width) for c in tint) for y in range(height))
    else:
        rows = b"".join(b"\0" + bytes(shade(x, y) for x in range(width)) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2 if tint else 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def _picture(seed: int, width: int = 300, height: int = 450, tint: tuple[float, float, float] | None = None) -> bytes:
    """Something with shapes that differs from book to book, for a cover or a comic's page."""
    a, b = 3 + seed % 7, 5 + seed % 11

    def shade(x: int, y: int) -> int:
        ring = ((x - width // 2) ** 2 + (y - height // 3) ** 2) // (900 + 40 * a)
        return 60 + ((ring * 37 + x // a + y // b) % 160)

    return _png(width, height, shade, tint)


def _text(seed: int) -> str:
    return " ".join(LINES[(seed * 5 + i * 7) % len(LINES)] for i in range(6))


def _epub(path: Path, seed: int, title: str, author: str, series: str, number: str, about: str) -> None:
    chapters = 3
    meta = f'<meta name="calibre:series" content="{series}"/><meta name="calibre:series_index" content="{number}"/>' if series else ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:hylly-demo:{seed}</dc:identifier><dc:title>{title}</dc:title>
<dc:creator>{author}</dc:creator><dc:language>en</dc:language><dc:description>{about}</dc:description><dc:publisher>Made up for this demo</dc:publisher>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta><meta name="cover" content="cover"/>{meta}</metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>
{"".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(1, chapters + 1))}</manifest>
<spine>{"".join(f'<itemref idref="c{i}"/>' for i in range(1, chapters + 1))}</spine></package>"""
    page = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{0}</title></head><body>{1}</body></html>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                                             '<rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/book.opf", opf)
        z.writestr("OEBPS/cover.png", _picture(seed, tint=TINTS[seed % len(TINTS)]))
        z.writestr("OEBPS/nav.xhtml", page.format("Contents", '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>'
                                                  + "".join(f'<li><a href="c{i}.xhtml">Chapter {i}</a></li>' for i in range(1, chapters + 1)) + "</ol></nav>"))
        for i in range(1, chapters + 1):
            z.writestr(f"OEBPS/c{i}.xhtml", page.format(f"Chapter {i}", f"<h1>Chapter {i}</h1>"
                                                        + "".join(f"<p>{_text(seed + i * n)}</p>" for n in range(1, 13))))


def _cbz(path: Path, seed: int, pages: int) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for n in range(1, pages + 1):
            z.writestr(f"page{n:03}.png", _picture(seed * 13 + n, 400, 600))


def is_demo(folder: Path) -> bool:
    return read_json(paths.marker_file(folder / "library")).get("demo") is True


def make(folder: Path) -> Path:
    """The library, in library\\ of the folder, once: running it again keeps it, with whatever you changed in it. Its settings
    go beside it, never in it, as the server serves the library's files."""
    root = folder / "library"
    if is_demo(folder):
        return root
    if folder.exists() and any(folder.iterdir()):
        raise Exit(USAGE, f"{folder} has other things in it, and a demo keeps to a folder of its own. Name an empty one, or one that doesn't exist yet.")
    for n, (folder, name, title, author, series, number, about) in enumerate(BOOKS, 1):
        (root / folder).mkdir(parents=True, exist_ok=True)
        _epub(root / folder / f"{name}.epub", n, title, author, series, number, about)
    for n, (folder, name, pages) in enumerate(COMICS, 1):
        (root / folder).mkdir(parents=True, exist_ok=True)
        _cbz(root / folder / f"{name}.cbz", n, pages)
    atomic_write(paths.marker_file(root), json.dumps({"demo": True, "created": db.now()}, indent=1))
    return root


LEADS_AWAY = ("HYLLY_SETTINGS", "HYLLY_RULES", "HYLLY_REMOTE")  # your settings file, your rules, your server computer


def yours() -> tuple[str, ...]:
    """What in the environment would lead a command on the demo to what is yours, past the demo's own settings folder:
    another settings.toml, your rules, and your server computer, to which the command would be passed on over ssh,
    whatever the demo's settings say (LEADS_AWAY); and the secrets, your shops' logins and your phone's alarms (what hylly
    commands lists under secret, the alarms' _FILE beside each, and systemd's credentials), which nothing of the demo's
    needs, and with which its alarms would ring your phone."""
    from .effects import SECRET
    from .settings import SECRETS

    alarms = [f"{variable}_FILE" for _, variable in SECRETS.values()]
    return (*LEADS_AWAY, *SECRET, *alarms, "CREDENTIALS_DIRECTORY")


def _apart(folder: Path) -> None:
    os.environ["HYLLY_HOME"] = str(folder / "settings")
    os.environ["HYLLY_LIBRARY"] = str(folder / "library")
    for name in yours():
        os.environ.pop(name, None)


def apart_from_yours(folder: Path) -> None:
    """From here on this process, and a server it starts or restarts, has the demo's settings, users and server, not
    yours: its settings folder is settings\\ in the demo's folder, beside the library, never in it; and nothing of
    yours that the environment holds (yours): no command of it is passed on to your server computer."""
    _apart(folder)
    paths.keep_private()


def variables(folder: Path) -> str:
    """What to set for other commands on the demo, as hylly demo says it: each on a line of its own, nothing after it, as
    a path is copied whole, spaces and all. HYLLY_SETTINGS, HYLLY_RULES or HYLLY_REMOTE, set, would lead them to your own
    settings, rules or server computer (yours), and the secrets to your logins and your phone; HYLLY_NO_NOTIFY keeps a
    try that fails from ringing this computer's bell."""
    return ("For other commands on it, set these, and leave HYLLY_SETTINGS, HYLLY_RULES and HYLLY_REMOTE unset, as they "
            "would lead them to your own settings, rules or server computer (hylly --here before a command keeps it on "
            "this computer), and the logins and alarms' secrets that hylly commands --json lists under secret:\n"
            f"  HYLLY_HOME={folder / 'settings'}\n  HYLLY_LIBRARY={folder / 'library'}\n  HYLLY_NO_NOTIFY=1")


DEMO_SETTINGS = "# The demo's own settings (hylly demo)."
SERVER_TABLE = re.compile(r"""^\s*\[\s*(?:server|"server"|'server')\s*\](?:\s*#.*)?\s*$""")  # however its header is written
A_TABLE = re.compile(r"^\s*\[")  # where the next table begins, [[...]] too


def _toml(value) -> str | None:
    """A plain value as TOML writes it; None for one the demo doesn't write again (a table, a date, nan)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # TOML's basic strings take JSON's escapes
    if isinstance(value, list) and all(_toml(v) is not None for v in value):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return None


def keep_port(port: int) -> None:
    """The demo's own settings.toml (in its settings folder, apart_from_yours) says its port, and that it listens on this
    computer only: a server's settings are its only options, so a Restart from its server page, or hylly server restart
    with the demo's two set, brings it back there, not on 8770, your own server's. The demo made the file, so its
    [server] table is the demo's to write, whole, with what else it held (a name, say); the rest of the file stays as it
    is. Read as the settings read it (tomllib), so no way of writing the header or a key can leave a second [server] or
    a second port; a file it can't read that way begins again."""
    file = paths.settings_file()
    text = file.read_text(encoding="utf-8") if file.is_file() else DEMO_SETTINGS
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        before, text = {}, DEMO_SETTINGS
    server = before.get("server") if isinstance(before.get("server"), dict) else {}
    kept, inside = [], False
    for line in text.splitlines():  # every line but the [server] table's
        if SERVER_TABLE.match(line):
            inside = True
        elif A_TABLE.match(line):
            inside = False
        if not inside:
            kept.append(line)
    table = ["[server]", f"port = {int(port)}", 'listen = "127.0.0.1"']
    for key, value in server.items():
        said = _toml(value)
        if key not in ("port", "listen") and said is not None:
            table.append(f"{key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else json.dumps(key, ensure_ascii=False)} = {said}")
    rest = "\n".join(kept).rstrip()
    written = (f"{rest}\n\n" if rest else "") + "\n".join(table) + "\n"
    try:
        if tomllib.loads(written)["server"]["port"] != int(port):
            raise KeyError("port")
    except (tomllib.TOMLDecodeError, KeyError, TypeError):  # [server] some other way still (server.port = ...): the demo's alone
        written = f"{DEMO_SETTINGS}\n\n" + "\n".join(table) + "\n"
    atomic_write(file, written)
    from . import settings

    settings.reload()  # this process reads it afresh too


OWNER = "demo"  # the demo's owner, made when it has no users yet


def owner() -> bool:
    """The demo's owner, made if the demo has no users yet, with a password nobody is told: the server's one-time link
    logs its browser in (serve.owners_link), and hylly user add demo sets one of your own. True when made now."""
    if access.load_accounts():
        return False
    access.add_user(OWNER, secrets.token_urlsafe(24), role="owner")
    return True


PORT = 8779  # the demo's server's, unless --port says another: not 8770, your own server's


def place_of(folder: str | None) -> Path:
    """The demo's folder: the one named, or hylly-demo in your home folder."""
    return Path(folder).absolute() if folder else Path.home() / "hylly-demo"


def stop_command(folder: str | None) -> str:
    """hylly demo --stop, for this demo's folder as it was named."""
    return "hylly demo --stop" + (f" {paths.typed(place_of(folder))}" if folder else "")


IN_THIS_WINDOW = "Ctrl-C in this window"  # the Stops line of a demo served here, in the foreground (run)


def _described(info: dict, folder: str | None) -> str:
    """The demo's server in the background, as serve.describe says a server, with the demo's own way to stop it: hylly
    server stop, without the demo's settings folder set, would stop your own."""
    from . import serve

    return serve.describe(info, f"never by itself; {stop_command(folder)} stops it")


def run(folder: str | None, port: int, serve_it: bool = True, open_browser: bool = True, log=print,
        background: bool = False) -> Path:
    """The demo made, or kept as it is, with its catalog built, and served: here until Ctrl-C, or with background on
    its own, as its settings say (keep_port), and this ends once it listens. A demo server running already is left as it
    is, and said, and keeps its port: the settings are its options, for a restart too."""
    from . import catalog, instance, serve

    place = place_of(folder)
    root = make(place)
    apart_from_yours(place)
    running = instance.running()
    if running is None:
        keep_port(port)
    catalog.build(root, log=log)
    owner()
    log(f"\nA demo library of made-up books, apart from your own, is in {place}. Delete that folder when you're done with it.")
    log(variables(place))
    how = ("at a terminal, the server below shows a one-time link that logs you in, and with these set, hylly user link "
           f"{OWNER} makes another" if serve_it and not background else
           f"with these set, hylly user link {OWNER}, at a terminal, makes a one-time link that logs a browser in")
    log(f"Every request to its server logs in. Its owner is {OWNER}: {how}, and hylly user add {OWNER} gives {OWNER} a "
        "password, for another browser or a reading app.")
    if background:
        if running is not None:
            log(f"Its server is running already, and is left as it is:\n{_described(running, folder)}")
        else:
            running = instance.start_background()
            log(f"Serving it in the background, on its own until it's stopped:\n{_described(running, folder)}")
        link, _ = serve.owners_link(running.get("port", port), serve.listens(running) or "127.0.0.1")
        if link:  # at a terminal only, as the server's own start has it: never into a file, which would keep it
            log(f"Logging you in as {OWNER}, once, within {access.LINK_MINUTES} minutes: {link}")
        log(f"To stop it, and no other server: {stop_command(folder)}")
    elif serve_it:
        log(f"Serving it at http://127.0.0.1:{port}/ until Ctrl-C.")
        # a restart from its server page stays on this port (keep_port) and in this library (HYLLY_LIBRARY)
        serve.run(root, port=port, listen="127.0.0.1", open_browser=open_browser, given={"port": port, "library": str(root)},
                  stops=IN_THIS_WINDOW)  # hylly server stop, typed in another, would reach your own server
    else:
        log("hylly demo again, without --no-serve, serves it here; with --background, on its own.")
    return root


def _same(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


def _holds(pid, lock: Path) -> bool | None:
    """Whether the process a server's note names has that server's lock open, as the server holds it for as long as it
    runs: a note naming any other process names no demo server. None when that can't be told: the process's files
    not to be looked at, or the lock not among them on Windows, which doesn't list every file a process has open. A
    process gone, or no number, is a no."""
    import psutil

    try:
        files = psutil.Process(int(pid)).open_files()
    except (psutil.NoSuchProcess, TypeError, ValueError):
        return False
    except (psutil.Error, OSError):
        return None
    if any(_same(Path(found.path), lock) for found in files):
        return True
    return None if os.name == "nt" else False


def _seems_the_server(note: dict) -> bool:
    """For a process whose files can't be looked at (_holds): whether it began before its note did, as the note's own
    process has (instance.started_at), and runs a Hylly server (hyllyd, python -m hylly.daemon), as the server's own
    command line is. A number given meanwhile to another program fails the one, any other program the other."""
    import psutil

    from . import instance

    since = instance.started_at(note)
    try:
        process = psutil.Process(int(note["pid"]))
        return (since is not None and process.create_time() <= since[0] + since[1]
                and instance._its_command(note, process.cmdline(), job=False))
    except (psutil.Error, OSError, KeyError, TypeError, ValueError):
        return False


def stop(folder: str | None, dry_run: bool = False, log=print) -> dict | None:
    """hylly demo --stop: the demo's own server, and only that one, found as every server is, by the lock and the note
    in its settings folder: here the demo's own, settings\\ beside its library, never yours. Refused, with nothing
    stopped: a folder that isn't a demo's; one whose settings folder leads to your usual one, by a link say; a server
    there that serves another library than the demo's (another Hylly's, through a link); a note naming a process that
    doesn't hold the demo's lock, or, where that can't be told, one that doesn't seem the note's own server
    (_seems_the_server); and a server just starting, with no note yet. Returns the stopped server's note, or None."""
    from . import instance

    place = place_of(folder)
    if not is_demo(place):
        raise Exit(USAGE, f"{place} isn't a demo's folder, so no demo server of it can be running: hylly demo makes one. "
                          "Nothing was stopped.")
    _apart(place)
    if paths.usual_settings_folder(settings_file_counts=False):
        raise Exit(USAGE, f"The demo's settings folder, {paths.config_dir()}, leads to your own: a demo has one of its own. "
                          "Nothing was stopped.")
    found = instance.running()
    if found is None:
        log("No demo server is running.")
        return None
    if not found:  # its lock taken, its note not written yet
        raise Exit(LOCAL, "The demo's server is starting, and hasn't said yet where it listens: try again in a moment. "
                          "Nothing was stopped.")
    library = found.get("library")
    if not library or not _same(Path(library), place / "library"):
        named = library or "a library it doesn't name"
        raise Exit(USAGE, f"The server whose lock is in the demo's settings folder, {paths.config_dir()}, serves {named}, "
                          "not the demo's: another Hylly's, through a link say. Nothing was stopped.")
    holds = _holds(found.get("pid"), instance.lock_file())
    if holds is False:
        raise Exit(LOCAL, f"The note of the demo's server names process {found.get('pid', '?')}, which doesn't hold the demo's "
                          "lock, so it isn't the demo's server. Nothing was stopped.")
    if holds is None and not _seems_the_server(found):
        raise Exit(LOCAL, f"The note of the demo's server names process {found.get('pid', '?')}, and whether that is the demo's "
                          "server can't be told here: its files can't be looked at, and it didn't begin before the note or "
                          "isn't a Hylly server. Nothing was stopped. With the demo's HYLLY_HOME set, as hylly demo printed "
                          "it, hylly server stop stops the server of that settings folder.")
    if dry_run:
        log(f"It would stop the demo's server, and no other; nothing was stopped.\n{_described(found, folder)}")
        return None
    found = instance.stop()
    log("Stopped the demo's server." if found is not None else "No demo server is running.")
    return found
