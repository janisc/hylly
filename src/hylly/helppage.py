"""The help page: help.html, with this computer's folders filled in for the owner, or in words for everyone, and the
command reference and every list of commands in it made from the commands themselves (commands.py), and its list of
the lookup sources' keys from their rows (sites.py). Only the server sends it, at /catalog/help.html: nothing writes it
into the library. The server needn't import the catalog's builder for that, nor the code that talks to the shops, nor
the lookups' (parts.py)."""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from . import INSTALL, PROJECT_URL, access, bell, db, folders, instance, paths, remote, rules, settings
from .commands import asks_first, build_parser, leaves, one_object, options_of, reference_html, taking
from .sites import LOOKUPS
from .words import fill

LIST = re.compile(r"<!--commands:([a-z-]+)-->")  # in help.html: a list of commands, by its name in command_lists
KEYS = "<!--lookups:keys-->"  # in help.html: where to get each lookup source's key (lookup_keys)
PLACES = "<!--places-->"  # in help.html: Hylly's own folders on this computer, by kind (places)


def command_lists() -> dict[str, str]:
    """The help's lists of commands, as HTML, by the name its placeholder gives (<!--commands:json-->): what takes
    --json, and of those what gives one object however it ends, --library and --rules, what asks first or needs a person
    at the keyboard, and what always runs on this computer. Made from the parser, as the reference is, since lists written by hand fell behind it."""
    parser = build_parser()
    every = {name: p for name, p, _ in leaves(parser)}
    asking, yes = asks_first(), taking("--yes", parser)
    stops = [(f"jobs stop {job}", "jobs stop") for job in remote.LOCAL_JOBS]  # forms, not commands of their own
    return {"json": _listed(taking("--json", parser), every),
            "one-object": _listed(one_object(parser), every),
            "library": _listed(taking("--library", parser), every),
            "rules": _listed(taking("--rules", parser), every),
            "asks": _listed([name for name in asking if name in yes], every),
            "needs-a-person": _listed([name for name in asking if name not in yes], every),
            "here": _listed([name for name in every if name.split()[0] in remote.LOCAL], every, also=stops)}


def _listed(names: list[str], every: dict, also: list[tuple[str, str]] | None = None) -> str:
    """Commands as the help names them, each a link to its entry in the reference. A command of several actions that all
    are named goes by its own name (humble, for each of its actions); one that also runs as itself, when only that is
    meant, goes by the words it takes (backup TARGET, not backup check), or with "alone" after it (rules alone, not rules
    copy). every: each command as it's run, and its parser (commands.leaves). also: (words, the command they're a form
    of)."""
    shown, done = [], set()
    for name in names:
        top = name.split()[0]
        if top in done:
            continue
        its = [n for n in every if n.split()[0] == top]
        if len(its) > 1 and set(its) <= set(names):
            shown.append((top, top, ""))
            done.add(top)
        elif name == top and len(its) > 1:  # the command itself, not each of its actions
            said = [(a.metavar or a.dest).upper() for a in options_of(every[name]) if not a.option_strings]
            shown.append((" ".join([name, *said]), name, "" if said else " alone"))
        else:
            shown.append((name, name, ""))
    shown += [(words, of, "") for words, of in also or []]
    links = [f'<a href="#cmd-{of.replace(" ", "-")}"><code>{html.escape(words)}</code></a>{after}' for words, of, after in shown]
    return ", ".join(links[:-1]) + " and " + links[-1] if len(links) > 1 else "".join(links)


def lookup_keys() -> str:
    """The help's list of the lookup sources that need a key, as list items: where to get each one and how to save it,
    in the words hylly login says them, from the sources' rows (sites.LOOKUPS)."""
    return "\n".join(f"<li><b>{html.escape(s.name)}:</b> {html.escape(s.get_key, quote=False).replace(' &gt; ', ' → ')} "
                     f"Then: <code>hylly login {sid} --clipboard</code></li>" for sid, s in LOOKUPS.items() if s.key)


def places(generic: bool) -> str:
    """The help's line of Hylly's own folders, by kind: each by its name and its path, for the owner; for everyone else,
    where to find them, as the folders' names alone would only say them twice."""
    if generic:
        return "on the server computer, each as <code>hylly settings places</code> lists it there"
    return ", ".join(f"{html.escape(p['name'])} <code>{html.escape(p['folder'])}</code>" for p in paths.places())


def help_page(root: Path, generic: bool = False, source: str = PROJECT_URL) -> str:
    """The admin cheat sheet; the template has none of your folders. generic: in words instead of this computer's
    folders, as the server sends it to everyone but the owner, with the port as it comes. Otherwise with your own folders
    filled in from settings.toml, and the port the server listens on: the server sends that one to the owner alone.
    source: where its Source link leads, the server's [server] source_url, the same for everyone."""
    if generic:
        values = {"library": "the library folder", "settings": "settings.toml (in the settings folder)",
                  "settings_folder": "the settings folder", "data_folder": "the data folder",
                  "cache_folder": "the cache folder", "logins_folder": "the logins folder",
                  "server_log": "server.log (in the state folder)", "users_file": "the database, hylly.db (in the data folder)",
                  "access_log": "access.log (in the state folder)", "bell_log": "bell.log (in the state folder)",
                  "address": "shown to owners in this page", "port": str(settings.DEFAULTS["server"]["port"]),
                  "own_folders": "your own folders", "rules_folder": "rules (in the settings folder)"}
    else:
        unset = "(not set: see settings.example.toml)"
        values = {
            "library": str(root).rstrip("\\/"),  # a share's root prints as \\NAS\share\
            "settings": str(paths.settings_file()),
            # Hylly's own folders by kind (paths.PLACES): the settings folder is where the rules, the themes and the
            # secrets are, and settings.toml too unless HYLLY_SETTINGS names another file
            "settings_folder": str(paths.config_dir()), "data_folder": str(paths.place("data")),
            "cache_folder": str(paths.place("cache")),
            "logins_folder": str(paths.logins_dir()),
            "server_log": str(instance.log_file()),
            "users_file": f"the database, {db.file()}",
            "access_log": str(access.access_log_file()),
            "bell_log": str(bell.log_file()),
            "address": str(paths.section("server").get("address") or unset).rstrip("/"),  # [server] address
            "port": port(),
            "own_folders": ", ".join(f"{f.name} ({f.label})" if f.label.lower() != f.name.lower() else f.name
                                     for f in folders.own_folders(root)) or "none yet",
            "rules_folder": str(rules.folder()),
        }
    values["source"], values["install"] = source, INSTALL
    text = Path(__file__).with_name("help.html").read_text(encoding="utf-8")
    text = fill(text, values)
    lists = command_lists()
    text = text.replace("<!--commands-->", reference_html()).replace(KEYS, lookup_keys()).replace(PLACES, places(generic))
    return for_this_system(LIST.sub(lambda m: lists[m[1]], text))


def port() -> str:
    """The port in the owner's help, for the addresses it shows: the running server's own, from its note (instance.py),
    as hylly serve --port wins over the settings; else [server] port; else 8770. The owner's help is made in the
    server itself, so the note it reads is its own."""
    try:
        found = json.loads(instance.info_file().read_text(encoding="utf-8")).get("port")
    except (OSError, ValueError, AttributeError):  # no server running, or a note half there or not a note
        found = None
    return str(found or paths.section("server").get("port") or settings.DEFAULTS["server"]["port"])


def for_this_system(text: str, windows: bool = os.name == "nt") -> str:
    """The help speaks of folders as Windows writes them. Elsewhere, what you would type or look for has the other slash."""
    def typed(m: re.Match) -> str:  # not what is only for Windows: %LOCALAPPDATA%, C:\\..., $env:
        return m[0] if re.search(r"%\w+%|\b[A-Z]:\\|\$env:", m[0]) else m[0].replace("\\", "/")

    return text if windows else re.sub(r"<(code|pre)>.*?</\1>", typed, text, flags=re.S)
