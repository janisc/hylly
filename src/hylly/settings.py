"""Your choices in settings.toml, each with its default: which file of a title to keep, where a title is best read,
the reader's page cache, the lookups, stars and reviews, the themes, 7-Zip, the server computer when it's another one,
the library server and its alarms, the e-reader feed, the backup, the Inbox and your own folders.
What Hylly knows about books is in the rules files instead (rules.py); these sections are what you prefer. See
settings.example.toml.

    library, rules, storybundle_user   above the sections (TOP; paths.py reads them)
    [download]  ereader, tablet, a list for each kind (the rules' kinds), size_tie, language (pick_file), unsure
    [devices]   fixed_layout, image_heavy_mb
    [reader]    cache_gb, cache_folder, book_fonts, word_meanings, memory_mb, pages_ahead, pages_ahead_fast, sharp_ahead,
                tiles_ahead, draw_ahead
    [lookups]   enabled, order, accept, none_days, contact
    [reviews]   who, moderation
    [look]      theme, themes
    [tools]     sevenzip
    [remote]    ssh, programs
    [server]    name, listen, port, access_log, proxy_header, trusted_proxies, allowed_hosts, address, jobs,
                source_url
    [bell]      toast, wrong_passwords, downloads_per_hour, new_devices, library_missing, quiet_minutes,
                alarms_per_hour, remote_detail, daily_summary, command
    [ereader_feed]      kinds, formats, max_mb, skip_tablet, pdf_from
    [backup]    target, keep
    [inbox]     folder, folders, into, lookup, strip_suffix
    [folders.<name>]    label, kind, kinds, genres, named, first, copy_from, list, strip_suffix (FOLDER)

Read once in a command; a running server reads them again when settings.toml changes. A value of the wrong kind stops
with the section and the key: access_log = "false" in quotes is a text, and read as true it would keep the log it was
meant to stop. A key a section doesn't have, a section there is none of, and a name above the sections that isn't read
are told once, and left out; so is one Hylly no longer uses (RETIRED), with what took its place, and a table of rules or
a setting of the top under a section, with where it goes (astray). A number past its limits is told, and the nearest limit goes (LIMITS): an extra zero shouldn't
take the server down, or stop it from starting.

settings.toml holds no secrets, so it can be copied, compared and pasted as it is. The server's own, the alarms' ntfy
address and token, are each in a file of its own in secrets/ in the settings folder, or given by the environment or
systemd (SECRETS, secret_from). One still under [bell], where an older Hylly kept them, is never read: it is told of as
no longer used, with the file it goes in now (RETIRED), and a backup leaves settings.toml out while it's there
(old_secrets).
"""

from __future__ import annotations

import contextlib
import ipaddress
import math
import os
import re
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .exits import NOT_SET_UP, Exit
from . import PROJECT_URL, paths
from .shops import SHOPS
from .sites import LOOKUPS

MAX_DAYS = 999999999  # the most days Python counts
LARGEST = "largest"  # in a list of [download]: the largest file of the formats after it, or of any (pick_file)
DEFAULTS = {
    # The format a title is kept in, best first: the first offered, its largest file (pick_file). A kind's own list,
    # else its device's: tablet for a kind read on a tablet ([kinds.X] device in the rules), ereader for every other.
    # Each kind in the rules has a list here, [] (its device's) unless given below (section_defaults). The e-reader
    # feed offers the e-reader's too (library.Device)
    "download": {
        "ereader": ["epub", "azw3", "mobi", "azw", "prc", "fb2", "pdf", "djvu", "cbz", "cbr", "cb7", "txt"],
        "tablet": ["cbz", "cbr", "cb7", LARGEST, "pdf", "epub"],  # PDF or EPUB, the larger; about as large, PDF
        "rpg": ["pdf", "cbz", "cbr", "cb7", LARGEST, "epub"],  # the PDF keeps the print layout
        "size_tie": 0.95,  # "largest": files this near the largest count as as large
        "language": "",  # "": the file whose label names no language; or one of [languages] in the rules
        # what a download does where its rules are unsure (curation.QUESTIONS): "guess", take the rules' guess as
        # they always have, and put it to you (hylly decide); "ask", hold that file back until you answer
        "unsure": "guess",
    },
    "devices": {  # what makes a title a tablet's whatever its kind; where a kind is read is the rules' ([kinds.X] device)
        "fixed_layout": "tablet",
        "image_heavy_mb": 21,  # a disk's MB (words.py), near the 20 MiB it was
    },
    "reader": {"cache_gb": 10,  # the pages drawn and the comics unpacked whole, on the server's disk: reader.disk_limit
               "cache_folder": "",  # where, by its full path: a disk of its own, say; "": Hylly's cache folder. reader.cache_root
               "book_fonts": True,  # false, and the browser refuses the font files books bring
               "word_meanings": True,  # false: the word box asks Wiktionary and Wikipedia nothing, and the page's policy names neither
               "memory_mb": 128,  # pages the server keeps in its memory. It also keeps to half of what's free: reader.keep_limit
               "pages_ahead": 5, "pages_ahead_fast": 9, "sharp_ahead": 2, "tiles_ahead": 6,  # what the browser fetches ahead; 0 pages_ahead: nothing
               "draw_ahead": 32},  # pages the server draws before they're asked for, at most, for each reader; 0: none
    # enabled: false, and no source is asked at all (lookups.available). order: the sources asked, first one first; []:
    # every one, in the order of sites.LOOKUPS (lookups.in_order)
    "lookups": {"enabled": True, "order": [], "accept": 0.8, "none_days": 180, "contact": ""},
    "reviews": {"who": "off",  # the lowest role that may give titles stars and words, which everyone sees; off: nobody, and none show
                "moderation": "off"},  # whose words wait for an owner: on (all but an owner's), borrowers, or off (nobody's)
    "look": {"theme": "hylly", "themes": []},  # the library's own theme (themes.py), and those that may be picked; []: all
    # 7-Zip by its full path. Elsewhere than Windows also by its name alone, looked for on the PATH; "": 7z, else 7zz
    "tools": {"sevenzip": r"C:\Program Files\7-Zip\7z.exe" if paths.WINDOWS else ""},
    # The server computer, on another computer: commands run there (remote.py). ssh: its name for ssh; "": run them here.
    # programs: the folder of Hylly's programs there, if not on its PATH for ssh (as ~/.local/bin often isn't)
    "remote": {"ssh": "", "programs": ""},
    # The library server (serve.py, access.py), read as it starts: its only options, but for what hylly serve is given for one run
    "server": {"name": "Library",  # what the library calls itself; "": Library
               "listen": "127.0.0.1",  # where it listens: this computer only; "0.0.0.0" every IPv4 address, "::" every one
               "port": 8770,
               "access_log": True,  # one line per request in access.log
               "proxy_header": "X-Forwarded-For",  # the one header a trusted proxy names the sender in
               "trusted_proxies": ["127.0.0.0/8", "::1"],  # whose forwarding headers count: proxies' addresses or networks
               "allowed_hosts": [],  # names it answers to besides its own: a domain of yours behind a proxy
               "address": "",  # the address people use, such as https://books.example.org: shown, and answered to
               "jobs": "internal",  # its periodic jobs (periodic.py): "external" leaves them to hylly server jobs, on a timer
               # where this Hylly's source is, for the Source link on its pages: Hylly's own as it comes; a changed
               # version run for others names its own, as the AGPL asks (section 13)
               "source_url": PROJECT_URL},
    # HyllyBell, the alarms about the server (bell.py), read as the server starts. Where they go on a phone, the ntfy
    # address and its token, is a secret: in secrets/ (SECRETS), never here
    "bell": {
        "toast": True,             # a notification on this computer
        "wrong_passwords": 3,      # wrong passwords for a real user within 15 minutes before an alarm; 0 = never
        "downloads_per_hour": 10,  # different files one login (or address) gets within an hour before an alarm; 0 = never
        "new_devices": True,       # a login from a kind of device not seen before for that user
        "library_missing": 5,      # minutes the library can't be reached before an alarm; 0 = never
        "quiet_minutes": 60,       # the same alarm again at most this often
        "alarms_per_hour": 10,     # all alarms together sent out, ten times that a day; the others stay in bell.log; 0 = only there
        "remote_detail": "counts", # what the phone gets: "counts", or "full" for names, addresses and file names as well
        "daily_summary": 8,        # the hour for a summary of the last 24 hours; -1 = none
        "command": [],             # a program run for each alarm beside ntfy, by its words, no shell (bell.run_command); [] = none
    },
    # The e-reader feed: which titles it lists, and the file each offers (library.Device, which has the same defaults).
    # formats []: those of [download] ereader that are books and flow (formats.toml); a title you put in the feed
    # yourself offers the first of formats, then of ereader, that it has, of any size
    "ereader_feed": {"kinds": ["book", "tech"], "formats": [], "max_mb": 63, "skip_tablet": True,  # near 60 MiB
                     "pdf_from": [shop.id for shop in SHOPS.values() if shop.trusted_pdfs]},  # shops.py
    # hylly backup (backup.py). target: the backup's folder, by its full path, for a plain hylly backup, as a timer runs
    # it; the one typed wins. keep: how many of its zips stay, newest first, as restic's forget counts them, such as
    # { daily = 7, weekly = 4, monthly = 12 } (BACKUP_KEEPS, backup.to_thin); {}: every one
    "backup": {"target": "", "keep": {}},
    # The Inbox (folders.Inbox, copies.py): a folder books are dropped in, from wherever they came, which the server's
    # job empties into the library, each book into the folder of yours its kind belongs in. folder: the Inbox, by its
    # full path; folders: more of them, each read the same way. into: which folder of yours a kind's books go to, by
    # the kind, and "default" for every kind it doesn't name: { default = "collections", comic = "comics" }; {}: as
    # the rules give their kinds folders ([kinds.X] folders: comics, manuals), and every other kind to books
    # (folders.into). lookup: false, and its books aren't looked up online. strip_suffix: what file names drop, as a
    # folder's (folders.py)
    "inbox": {"folder": "", "folders": [], "into": {}, "lookup": True, "strip_suffix": ""},
}
BACKUP_KEEPS = ("daily", "weekly", "monthly")  # what keep under [backup] counts


def keep_written(value) -> str:
    """keep under [backup] as settings.toml has it, { daily = 7, weekly = 4 }, for hylly settings show and check."""
    from .rules import toml_value

    if not isinstance(value, dict):
        return toml_value(value)
    return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }" if value else "{}"
DEVICE_LISTS = ("ereader", "tablet")  # [download]'s lists for a device, which a kind's own list is [] for
# [download]'s own keys, which no kind may be named (rules.py): its devices' lists and what isn't a list of formats
DOWNLOAD_OWN = (*DEVICE_LISTS, *(key for key, v in DEFAULTS["download"].items() if not isinstance(v, list)))
# A section for each of your own folders, [folders.<name>], every key optional (folders.py, whose Folder they make).
# Only the keys you set are kept: comics and manuals have a kind of their own unless you say otherwise.
FOLDER = {"label": "", "kind": "book", "kinds": {}, "genres": False, "named": False, "first": False,
          "copy_from": "", "list": "", "strip_suffix": ""}
HYLLYS = frozenset({"catalog", "state", "curation", "superseded"})  # Hylly's own folders at the top of the library
INTO_DEFAULT = "default"  # in into under [inbox]: the folder for every kind it doesn't name
# Above the sections: the library, your rules, and the StoryBundle account (paths.setting reads them, with the
# environment's HYLLY_LIBRARY, HYLLY_RULES and HYLLY_STORYBUNDLE_USER first)
TOP = {"library": "", "rules": "", "storybundle_user": ""}
CHOICES = {("devices", "fixed_layout"): ("tablet", "either", "ereader"), ("reviews", "who"): ("off", "borrower", "reader", "librarian", "owner"),
           ("reviews", "moderation"): ("off", "on", "borrowers"),
           ("server", "proxy_header"): ("X-Forwarded-For", "Forwarded", "CF-Connecting-IP", "X-Real-IP"),
           ("server", "jobs"): ("internal", "external"), ("download", "unsure"): ("guess", "ask"),
           ("bell", "remote_detail"): ("counts", "full")}  # and [folders] kind: the rules' kinds (kinds)
ANY_CASE = {("server", "proxy_header")}  # as the server has always read it; the choice goes as listed
LIMITS = {("reader", "memory_mb"): (16, 16384), ("reader", "pages_ahead"): (0, 30), ("reader", "pages_ahead_fast"): (0, 60),
          ("reader", "sharp_ahead"): (0, 5), ("reader", "tiles_ahead"): (0, 30), ("reader", "draw_ahead"): (0, 200)}  # whole numbers, from and to
# What a key takes where its default can't say. "whole": a whole number, below 0 too, as the alarms have always taken
# them (-1: no daily summary). "port": a whole number from 1 to 65535. "list": a list of texts. "text or whole": a text,
# or a whole number without quotes. "listen": an address of this computer's, or every one ("0.0.0.0", "::"). "address":
# a whole http or https address, with no path, or "". "networks": a list of addresses or networks (192.0.2.0/24).
# "source": a whole http or https address, a path too, never "".
# "formats": a list of formats, by their extensions, a dot before one left out; "largest" too in [download]'s. Every
# number may be in quotes too (_number); true and false not.
KIND = {**{("bell", key): "whole" for key in ("wrong_passwords", "downloads_per_hour", "library_missing", "quiet_minutes",
                                              "alarms_per_hour", "daily_summary")},
        ("server", "port"): "port", ("server", "listen"): "listen", ("server", "address"): "address",
        ("server", "source_url"): "source",
        ("server", "trusted_proxies"): "networks", ("", "storybundle_user"): "text or whole", ("backup", "keep"): "keep",
        ("bell", "command"): "command", ("inbox", "folder"): "path", ("inbox", "folders"): "paths",
        ("inbox", "into"): "into",
        **{("download", key): "formats" for key in DEVICE_LISTS},  # and each kind's list (_check)
        ("ereader_feed", "formats"): "formats"}
EXAMPLE = {("server", "allowed_hosts"): ["books.example.org"],  # for a list whose default is empty
           ("lookups", "order"): ["openlibrary", "googlebooks"]}
MAPPED = ipaddress.ip_network("::ffff:0:0/96")  # every IPv4 address, as an IPv6 one: no trusted_proxies network may hold it
UNSET = {("folders", "label"): "the folder's name"}  # what a key not set means where no value says it (hylly settings show)
EMPTY = {("lookups", "order"): f"every source, in this order: {', '.join(LOOKUPS)}",  # and what an empty one means
         ("backup", "target"): "none: hylly backup TARGET names it",
         ("bell", "command"): "none: no program of yours runs for an alarm",
         ("ereader_feed", "formats"): "those of ereader under [download] that are books and flow, in its order",
         ("inbox", "folder"): "none, unless folders lists some",
         ("inbox", "folders"): "none besides folder",
         ("inbox", "into"): "comics to comics, manuals to manuals, as the rules give them folders, every other kind to "
                            "books"}
EMPTY_KIND = "its device's list: tablet for a kind read on a tablet ([kinds.X] device in the rules), else ereader"
# Keys Hylly once read and reads no more, with what took their place: told once, by name, and left out, as any key a
# section hasn't is. Who may come in no longer hangs on where a request comes from: every request logs in. Where the
# server listens, and the address people use, are said in the settings, not worked out; there is one port; the
# server runs until it is stopped; and the format a title is kept in is one list for each device and kind (pick_file).
LOGIN_ALWAYS = "Hylly always asks for a login now: hylly user add NAME"
TABLET_LIST = ("Comics and RPG books go by a list now, tablet under [download] (rpg for RPG books): the first format "
               "on it that is offered wins, and \"largest\" there takes the largest file of the formats after it")
TABLET_AS_IT_COMES = '["cbz", "cbr", "cb7", "largest", "pdf", "epub"]'
KIND_DEVICE = ('Where a kind is best read is its own now, device = "tablet", "either" or "ereader" under [kinds.X] in '
               "a rules file of yours (hylly rules copy kinds makes one): comics and RPG books on a tablet, tech books "
               "either, the rest on an e-reader, as they come")
# The server's own secrets, each in a file of its own in secrets/ in the settings folder (secrets_dir), by its name there:
# the key under [bell] an older Hylly kept it in, in settings.toml (no longer read: RETIRED, old_secrets), and the
# environment variable that gives it instead
SECRETS = {"ntfy-url": ("ntfy", "HYLLY_NTFY_URL"), "ntfy-token": ("ntfy_token", "HYLLY_NTFY_TOKEN")}
RETIRED = {("server", "login"): LOGIN_ALWAYS, ("server", "home_networks"): LOGIN_ALWAYS, ("bell", "open_door"): LOGIN_ALWAYS,
           ("server", "lan"): 'Write listen = "0.0.0.0" instead, to listen on the home network',
           ("server", "public_address"): 'It is renamed: address = "..." with the same value',
           ("server", "tunnel_port"): "There is one port now: point the tunnel or proxy at the server's port (port under [server])",
           ("server", "idle_minutes"): "The server no longer stops by itself: hylly server stop stops it",
           ("download", "text_order"): "It is renamed: ereader = [...] under [download], with the same value",
           ("download", "comic_format"): f'{TABLET_LIST}. comic_format = "cbz-first" is tablet = {TABLET_AS_IT_COMES}, '
                                         'as it comes; "largest" is tablet = ["largest", "cbz", "cbr", "cb7", "pdf", '
                                         '"epub"]',
           ("download", "layout_order"): f"{TABLET_LIST}. Its comic formats go before \"largest\" there and the others "
                                         "after it, in its order: layout_order = [\"cbz\", \"cbr\", \"cb7\", \"pdf\", "
                                         f"\"epub\"] is tablet = {TABLET_AS_IT_COMES}, as it comes",
           ("download", "rpg_format"): "RPG books have a list of their own now, rpg under [download]: rpg_format = "
                                       '"pdf" is rpg = ["pdf", "cbz", "cbr", "cb7", "largest", "epub"], as it comes; '
                                       '"like-comics" is rpg = [], which reads them by the tablet list while RPG books '
                                       'are read on a tablet ([kinds.rpg] device = "tablet" in the rules, as it comes)',
           **{("devices", key): KIND_DEVICE for key in ("tablet_kinds", "either_kinds")},
           ("folders", "devices"): "Where a title is best read is worked out for every title now, in each of your "
                                   "folders as at the shops, by its kind and its main file",
           ("folders", "inbox"): "It moved to [inbox], a section of its own: folder = '...' there, with the path "
                                 "copy_from has, makes that folder the Inbox, and into = { default = \"<folder>\" } "
                                 "there sends its books into <folder> as before (comics and manuals into their own "
                                 "folders, unless into names <folder> for them too). copy_from stays, for hylly add",
           ("folders", "lookup"): "It was the Inbox's, and moved with it: lookup = false under [inbox]",
           ("ereader_feed", "always_formats"): "A title you put in the feed yourself offers the first of formats "
                                               "under [ereader_feed], then of ereader under [download], that it has, "
                                               "of any size",
           **{("bell", key): f"It is a secret, kept in a file of its own now, secrets/{name} in the settings folder: put "
                             "its value there, and take the line out of settings.toml. hylly bell says where that file is"
              for name, (key, _) in SECRETS.items()}}
# What the environment sets instead of the file (paths.setting, remote.server): hylly settings show says which comes from where
FROM_ENVIRONMENT = {("", "library"): "HYLLY_LIBRARY", ("", "rules"): "HYLLY_RULES", ("", "storybundle_user"): "HYLLY_STORYBUNDLE_USER",
                    ("remote", "ssh"): "HYLLY_REMOTE"}
WATCH = False  # set by serve.run: a running server looks at settings.toml again; a command keeps what it read first
CHECK_EVERY = 2.0  # seconds between a server's looks at whether settings.toml changed

_lock = threading.Lock()
_state: dict = {"stamp": None, "values": None, "checked": -1e9}
_warned: set = set()  # what was told once: (section, key) of a key a section has not, ("", name) of one above them


def kinds() -> tuple[str, ...]:
    """The kinds a title may be, in their order: the rules' ([kinds.X] in kinds.toml, and in yours). While the rules
    can't be read (a rules folder on a share not up yet: said where they are read), the shipped ones."""
    from . import rules

    try:
        return tuple(rules.current().kinds)
    except (SystemExit, Exception):  # noqa: BLE001 - a kind only your rules know must not stop the settings meanwhile
        return rules.shipped_kinds()


def section_defaults(section: str) -> dict:
    """A section's keys with their defaults, as DEFAULTS has them; under [download] a list for each kind too, in the
    rules' order after the devices' own, [] (its device's) where DEFAULTS gives none."""
    base = DEFAULTS[section]
    if section != "download":
        return base
    lists, named = {key: base[key] for key in DEVICE_LISTS}, kinds()
    return {**lists, **{kind: base.get(kind, []) for kind in named},
            **{key: value for key, value in base.items() if key not in lists and key not in named}}


def retired(section: str, key: str) -> str:
    """What took the place of a key Hylly no longer reads (RETIRED), for a section or a folder's (folders.<name>, whose
    name stands for <folder> in the words of one of its own: the Inbox's); "" for one it never read."""
    table, _, name = section.partition(".")
    found = RETIRED.get((section, key)) or RETIRED.get((table, key), "")
    return found.replace("<folder>", name) if table == "folders" and name else found


def _check(section: str, key: str, value, default):
    """One value, of the kind its default is or KIND says, as it is used. section: "" above the sections, and
    folders.<name> for a folder of yours, which goes by the table of [folders]."""
    table = section.partition(".")[0]
    written = keep_written(value) if (table, key) == ("backup", "keep") else repr(value)
    say = f"{paths.settings_file()}: {f'[{section}] ' if section else ''}{key} = {written}"
    kind = KIND.get((table, key)) or ("formats" if table == "download" and isinstance(default, list) else None)
    if kind in ("listen", "address", "networks"):
        return _where(say, kind, value)
    if kind == "source":
        return _source(say, value)
    if kind == "whole":
        number = _number(value)
        if not isinstance(number, int):
            raise Exit(NOT_SET_UP, f"{say}: a whole number, such as {default}.")
        return number
    if kind == "port":
        number = _number(value)
        if number is None or not math.isfinite(number) or number != int(number) or not 1 <= number <= 65535:
            raise Exit(NOT_SET_UP, f"{say}: a port, a whole number from 1 to 65535, such as {default}.")
        return int(number)
    if kind == "keep":  # [backup] keep: counts of the zips to keep, as restic's forget has them
        counts = {k: _number(v) for k, v in value.items()} if isinstance(value, dict) else None
        if (counts is None or set(counts) - set(BACKUP_KEEPS)
                or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts.values())):
            raise Exit(NOT_SET_UP, f"{say}: how many zips to keep, as {{ daily = 7, weekly = 4, monthly = 12 }}: whole "
                                   f"numbers, of {', '.join(BACKUP_KEEPS)}; {{}} keeps every zip.")
        if counts and not any(counts.values()):
            raise Exit(NOT_SET_UP, f"{say}: that would keep only the newest zip. Give a count above 0, or {{}} to keep "
                                   "every zip.")
        return counts
    if kind == "command":  # [bell] command: run by its words, never by a shell (bell.run_command)
        return _command(say, value)
    if kind in ("path", "paths"):  # [inbox] folder and folders: never one the current folder would decide
        return _inbox_folders(say, kind, value)
    if kind == "into":
        return _into(say, value)
    if kind == "text or whole":
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise Exit(NOT_SET_UP, f"{say}: a number in quotes, such as '12345'.")
        return str(value)
    if kind == "formats":  # as a file's extension may be written: ".EPUB" is epub, which a title's files are known by
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip().lstrip(".") for x in value):
            raise Exit(NOT_SET_UP, f'{say}: a list of formats in quotes, such as ["epub", "pdf"].')
        value = [x.strip().lstrip(".").lower() for x in value]
        if table != "download" and LARGEST in value:
            raise Exit(NOT_SET_UP, f'{say}: "{LARGEST}" is for the lists under [download]; here the first of the '
                                   "formats a title has is offered.")
        from . import rules

        try:
            known = list(rules.current().formats)  # formats.toml's, and your rules files'
        except (SystemExit, Exception):  # noqa: BLE001 - a rules folder on a share not up yet: said where it's read. A
            known = None  # format only your rules know must not stop a command or the server's start meanwhile
        unknown = next((x for x in value if known is not None and x not in known
                        and not (x == LARGEST and table == "download")), None)
        if unknown is not None:
            raise Exit(NOT_SET_UP, f"{say}: {unknown!r} isn't a format Hylly knows. They are {', '.join(known)}"
                                   + (f', and "{LARGEST}"' if table == "download" else "") + " (formats.toml).")
        if not value and key in DEVICE_LISTS:  # a kind's own [] is its device's list; a device's has none
            raise Exit(NOT_SET_UP, f"{say}: a device's list of formats, best first, such as {default[:3]!r}: an empty "
                                   "one would keep only the largest file of every title. Take the line out for the "
                                   "list as it comes.")
        return value
    if isinstance(default, list) or kind == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            raise Exit(NOT_SET_UP, f"{say}: a list in quotes, such as {(default or EXAMPLE.get((table, key), []))[:2]!r}.")
        value = [x.strip().lower() for x in value]
        if key == "kinds" and any(x not in kinds() for x in value):  # [ereader_feed] kinds
            raise Exit(NOT_SET_UP, f"{say}: kinds are {', '.join(kinds())}.")
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict) or not all(isinstance(x, str) for x in value.values()):
            raise Exit(NOT_SET_UP, f'{say}: a table in braces, such as {{ "Graphic Novels" = "comic" }}.')
        if key == "kinds" and any(x not in kinds() for x in value.values()):
            raise Exit(NOT_SET_UP, f"{say}: kinds are {', '.join(kinds())}.")
        return dict(value)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            quoted = isinstance(value, str) and value.strip().lower() in ("true", "false")
            raise Exit(NOT_SET_UP, f"{say}: true or false{', without quotes' if quoted else ''}.")
        return value
    if isinstance(default, (int, float)):
        value = _number(value)
        if value is None or not math.isfinite(value) or value < 0:
            raise Exit(NOT_SET_UP, f"{say}: a number, such as {default}.")
        if key in ("size_tie", "accept") and value > 1:
            raise Exit(NOT_SET_UP, f"{say}: a number from 0 to 1, such as {default}.")
        if key == "none_days" and value > MAX_DAYS:
            raise Exit(NOT_SET_UP, f"{say}: a number of days, at most {MAX_DAYS}.")
        limits = LIMITS.get((table, key))
        if limits:
            if value != int(value):
                raise Exit(NOT_SET_UP, f"{say}: a whole number, such as {default}.")
            near = max(limits[0], min(limits[1], int(value)))
            if near != value and (section, key, value) not in _warned:
                _warned.add((section, key, value))
                print(f"{say} is past its limits, {limits[0]} to {limits[1]}: {near} goes instead.", file=sys.stderr)
            return near
        return value
    if not isinstance(value, str):
        raise Exit(NOT_SET_UP, f"{say}: a text in quotes.")
    choices = CHOICES.get((table, key)) or (kinds() if (table, key) == ("folders", "kind") else None)
    if choices:
        fold = str.lower if (table, key) in ANY_CASE else str
        found = next((x for x in choices if fold(x) == fold(value)), None)
        if found is None:
            raise Exit(NOT_SET_UP, f"{say}: use {' or '.join(choices)}.")
        value = found
    if (section, key) == ("tools", "sevenzip"):
        _tool(say, value)
    if (section, key) == ("reader", "cache_folder") and value and not _full_path(value):
        example = r"D:\Hylly cache" if paths.WINDOWS else "/mnt/fast/hylly-cache"
        raise Exit(NOT_SET_UP, f'{say}: a folder by its full path, such as {example}, or "" for Hylly\'s own cache folder.')
    if (section, key) == ("backup", "target") and value and not _full_path(value):
        example = r"E:\Hylly backup" if paths.WINDOWS else "/mnt/backup/hylly"
        raise Exit(NOT_SET_UP, f'{say}: the backup\'s folder by its full path, such as {example}, or "" for none.')
    return value


def _where(say: str, kind: str, value):
    """[server]'s addresses, as the server uses them. listen: an address of this computer's, as it is written plainly
    ("::" for "::0"). address: the whole address people use, http or https, without the / at its end; "" for none.
    networks: proxies' addresses or networks, each as written."""
    if kind == "listen":
        try:
            return str(ipaddress.ip_address(value.strip() if isinstance(value, str) else ""))
        except ValueError:
            raise Exit(NOT_SET_UP, f'{say}: an address of this computer\'s in quotes: "127.0.0.1" for this computer only, '
                                   '"0.0.0.0" for every IPv4 address, "::" for every address.') from None
    if kind == "address":
        if not isinstance(value, str):
            raise Exit(NOT_SET_UP, f"{say}: a text in quotes.")
        value = value.strip()
        try:
            parts = urlsplit(value)
            fine = (parts.scheme in ("http", "https") and parts.hostname and not parts.username and parts.path in ("", "/")
                    and not parts.query and not parts.fragment and (parts.port or 1))
        except ValueError:  # a port that is no number, a broken [address]
            fine = False
        if value and not fine:
            raise Exit(NOT_SET_UP, f'{say}: the address people use, in full and with nothing after its name and port, such '
                                   'as "https://books.example.org" or "http://192.0.2.10:8770"; "" for none.')
        return value.rstrip("/")
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise Exit(NOT_SET_UP, f'{say}: a list in quotes, such as ["127.0.0.1", "::1"].')
    try:
        networks = [ipaddress.ip_network(x.strip(), strict=False) for x in value]
    except ValueError:
        raise Exit(NOT_SET_UP, f'{say}: addresses or networks in quotes, such as ["127.0.0.1", "172.18.0.0/16"]; [] for '
                               'none.') from None
    for written, n in zip(value, networks):  # every sender could name another in its headers, dodge the lockout, and the
        if n.prefixlen == 0 or (n.version == 6 and MAPPED.subnet_of(n)):  # cap spare it
            what = "every address there is" if n.prefixlen == 0 else "every IPv4 address, written as IPv6"
            raise Exit(NOT_SET_UP, f"{say}: {written.strip()} holds {what}, so anyone's forwarding headers would count: name "
                                   'your proxies\' own, such as ["127.0.0.1", "172.18.0.0/16"].')
    return [x.strip() for x in value]


def _source(say: str, value) -> str:
    """[server] source_url: where this Hylly's source is, as the Source links on its pages lead there. A whole http or
    https address, with a path as a project's page has one, in the characters a web address is written in (RFC 3986:
    no space, quote or angle bracket, which only a mistake would bring into a page's link), and no name or password
    before an @. Its scheme and name in small letters, as a page's own check reads them; the rest as written. Never "":
    a server run for others offers them its source, so the line is taken out for Hylly's own rather than emptied."""
    fine = isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", value.strip()) is not None
    if fine:
        value = value.strip()
        try:
            parts = urlsplit(value)
            fine = bool(parts.scheme in ("http", "https") and parts.hostname and "@" not in parts.netloc and (parts.port or 1))
        except ValueError:  # a port that is no number, a broken [IPv6] name
            fine = False
    if not fine:
        raise Exit(NOT_SET_UP, f'{say}: the whole address of this Hylly\'s source, such as "{PROJECT_URL}" (Hylly\'s own). '
                               "A changed version run for others names where its own source is; for Hylly's own, take the "
                               "line out.")
    start = f"{parts.scheme}://{parts.netloc.lower()}"  # HTTPS://Code.Example.ORG/Hylly is https://code.example.org/Hylly
    return start + value[len(start):]


def _full_path(value: str) -> bool:
    return PureWindowsPath(value).is_absolute() if paths.WINDOWS else value.startswith("/")


def _inbox_folders(say: str, kind: str, value) -> str | list[str]:
    """[inbox] folder, "" for none, and folders, a list: each Inbox by its full path, as written but for the spaces
    around it, as the server's job that empties it may be started from any folder."""
    example = r"D:\Inbox" if paths.WINDOWS else "/srv/inbox"
    several = kind == "paths"
    given = value if several else [value]
    if (several and not isinstance(value, list)) or not all(isinstance(x, str) for x in given):
        raise Exit(NOT_SET_UP, f"{say}: " + (f"the Inboxes' folders, each by its full path in quotes, such as "
                                             f"['{example}']." if several else
                                             f"the Inbox's folder by its full path in quotes, such as '{example}', or "
                                             '"" for none.'))
    found = [x.strip() for x in given]
    wrong = next((x for x in found if (x or several) and not _full_path(x)), None)
    if wrong is not None:
        raise Exit(NOT_SET_UP, f"{say}: {wrong!r} isn't a folder by its full path: the Inbox is named so, such as "
                               f"'{example}'" + ("" if several else ', or "" for none') + ".")
    return found if several else found[0]


def own_name(name: str) -> bool:
    """Whether a name can be one of your own folders in the library (folders.py): one plain name, not a path, nor a
    shop's folder (shops.py) or Hylly's own (HYLLYS), nor one a build passes over (starting with _ or .)."""
    shops = {shop.folder for shop in SHOPS.values()}
    return (bool(name) and name.lower() not in shops and name.lower() not in HYLLYS and not name.startswith(("_", "."))
            and not set(name) & set("/\\:"))


def _into(say: str, value) -> dict[str, str]:
    """[inbox] into: which of your folders each kind's books go to, by the kind's name in the rules (kinds), and
    INTO_DEFAULT for every kind it doesn't name: each a folder of your own by its plain name (own_name), as written."""
    example = '{ default = "collections", comic = "comics" }'
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise Exit(NOT_SET_UP, f"{say}: a table in braces, each kind with the folder of yours its books go to, such as "
                               f"{example}; {{}} as the rules give them.")
    known = kinds()
    out: dict[str, str] = {}
    for key, folder in value.items():
        kind, folder = key.strip().lower(), folder.strip()
        if kind != INTO_DEFAULT and kind not in known:
            raise Exit(NOT_SET_UP, f"{say}: {key!r} isn't a kind: they are {', '.join(known)}, and {INTO_DEFAULT} for "
                                   "every kind not named.")
        if not own_name(folder):
            raise Exit(NOT_SET_UP, f"{say}: {folder!r} isn't a name for a folder of your own: one plain name, not a "
                                   "shop's folder or Hylly's own, not starting with _ or ., and not a path, such as "
                                   f"{example}.")
        out[kind] = folder
    return out


def _tool(say: str, value: str) -> None:
    """A program by its full path: on Windows, one by its name alone is looked for first in the folder the server was
    started from. Elsewhere a name alone is looked for on the PATH only."""
    if paths.WINDOWS and not PureWindowsPath(value).is_absolute():
        raise Exit(NOT_SET_UP, f"{say}: 7-Zip by its full path, such as C:\\Program Files\\7-Zip\\7z.exe.")
    if not paths.WINDOWS and "/" in value and not value.startswith("/"):
        raise Exit(NOT_SET_UP, f"{say}: 7-Zip by its full path, such as /usr/bin/7z, or by its name alone, 7z, to look for "
                         "it on the PATH.")


def _command(say: str, value) -> list[str]:
    """[bell] command: a program and its arguments, each a word of its own, as the program gets them: no shell reads
    them, so a line written for one (a single word with spaces, say) is refused here rather than failing at every alarm.
    The program as _tool takes 7-Zip: on Windows by its full path, as by its name alone it would be looked for first in
    the folder the server was started from. [] for none."""
    example = '["mail", "-s", "Library alarm", "ann@example.org"]'
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise Exit(NOT_SET_UP, f"{say}: the program and its arguments as a list of words in quotes, such as {example}; [] "
                               "for none.")
    if any("\0" in x for x in value):
        raise Exit(NOT_SET_UP, f"{say}: a word with a NUL character in it (\\u0000), which no program can be given.")
    if not value:
        return []
    program = value[0].strip()
    if not program:
        raise Exit(NOT_SET_UP, f"{say}: the first word is the program, such as {example}; [] for none.")
    named = not re.search(r"[\\/]", program)
    if named and re.search(r"\s", program):
        raise Exit(NOT_SET_UP, f"{say}: each word in quotes of its own, such as {example}: no shell reads the line, so "
                               f"{program.split()[0]!r} with the rest after it is no program's name.")
    if paths.WINDOWS and not PureWindowsPath(program).is_absolute():
        raise Exit(NOT_SET_UP, f"{say}: the program by its full path, such as C:\\Windows\\System32\\curl.exe: by its "
                               "name alone, Windows would look for it first in the folder the server was started from.")
    if not paths.WINDOWS and not named and not program.startswith("/"):
        raise Exit(NOT_SET_UP, f"{say}: the program by its full path, such as /usr/bin/mail, or by its name alone, mail, "
                               "to look for it on the PATH.")
    return [program, *value[1:]]


def _number(value) -> int | float | None:
    """A number, or one in quotes, as Hylly has long read some of them: "30" can only mean 30, where "false" in quotes
    would read as true, so true and false stay without quotes. A whole one in quotes comes as an int. None for anything
    else: true, "abc", [30]."""
    number = None
    if isinstance(value, str):
        for kind in (int, float):
            try:
                number = kind(value.strip())
                break
            except ValueError:
                pass
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = value
    try:
        float(number) if number is not None else None
    except OverflowError:  # a whole number too long to be any setting's
        return None
    return number


def _tell(said: tuple, text: str) -> None:
    """What is left out, told once (_warned): a running server reads the file again and again."""
    if said not in _warned:
        _warned.add(said)
        print(f"{paths.settings_file()}: {text}", file=sys.stderr)


def astray(section: str, key: str) -> str:
    """What to do with what is written in the wrong place, in a sentence: a table of rules at the top (rules.SPECS),
    which settings.toml doesn't read, or one of the top's own settings (TOP) under a section, where a line added at the
    end of the file falls; "" for anything else. section is "" for the top."""
    from . import rules

    if not section and key in rules.SPECS:
        at = rules.folder()
        where = (f"{at}, the rules file rules = names" if at.is_file() else
                 f"a .toml file in your rules folder, {at} (hylly rules copy {rules.FILE_OF[key]} starts one there)")
        return f"[{key}] is a table of rules, not of settings, so it is left out here: it goes in {where}."
    if section and key in TOP:
        return (f"[{section}] {key} is one of the settings at the top, above the first [section], so it is left out "
                "here: move it up there.")
    return ""


def misplaced(found: dict, rules_only: bool = False) -> list[str]:
    """Everything settings.toml has in the wrong place (astray), as found there (paths._settings): a table of rules at
    the top, and the top's settings under a section or a folder's. rules_only: only what is about the rules, for
    hylly rules, which reads no section."""
    keys = [("", name) for name, value in found.items() if isinstance(value, dict)]
    for name, value in found.items():
        if isinstance(value, dict) and name != "folders":
            keys += [(name, key) for key in value if not isinstance(value[key], dict)]
    for name, value in (found.get("folders") if isinstance(found.get("folders"), dict) else {}).items():
        keys += [(f"folders.{name}", key) for key in value] if isinstance(value, dict) else [("folders", name)]
    return [said for section, key in keys if (said := astray(section, key))
            and (not rules_only or key == "rules" or not section)]


def _section(section: str, defaults: dict, saved: dict, fill: bool = True) -> dict:
    """A section's values, each checked: every key, the defaults filling in; with fill=False only those set."""
    values = {key: _check(section, key, saved[key], default) if key in saved else default
              for key, default in defaults.items() if fill or key in saved}
    values["_set"] = sorted(k for k in saved if k in defaults)  # those your settings.toml sets
    values["_unknown"] = {k: v for k, v in saved.items() if k not in defaults}  # and those the section hasn't
    for key in values["_unknown"]:
        if astray(section, key):
            _tell((section, key), astray(section, key))
        elif retired(section, key):
            _tell((section, key), f"[{section}] {key} is no longer used, so it is left out. {retired(section, key)}.")
        else:
            _tell((section, key), f"[{section}] {key} isn't one of its settings ({', '.join(defaults)}), so it is left out.")
    return values


def _load() -> dict:
    """Every section of DEFAULTS; "folders", your folders' sections by name in lower case, each with the keys it sets;
    and "", what is at the top, before any [section]."""
    found = paths._settings()
    top = {name: value for name, value in found.items() if not isinstance(value, dict)}
    for name in (*DEFAULTS, "folders"):
        if name in top:
            raise Exit(NOT_SET_UP, f"{paths.settings_file()}: {name} = ...: a section, [{name}], with its settings on the "
                                   "lines under it.")
    out = {"": {name: _check("", name, found[name], default) for name, default in TOP.items() if name in found}}
    out[""]["_unknown"] = {name: value for name, value in found.items()
                           if name not in (*TOP, *DEFAULTS, "folders")}  # told and listed by name: one may be a secret
    for name, value in out[""]["_unknown"].items():
        if isinstance(value, dict) and astray("", name):
            _tell(("", name), astray("", name))
        elif isinstance(value, dict):
            _tell(("", name), f"[{name}] isn't one of its sections ({', '.join(DEFAULTS)}, folders.<name>), so it is "
                              "left out.")
        else:
            under = next((f"; it belongs under [{section}]" for section, keys in DEFAULTS.items() if name in keys), "")
            _tell(("", name), f"{name}, at the top before any [section], isn't one of the settings there "
                              f"({', '.join(TOP)}{under}), so it is left out.")
    for section in DEFAULTS:
        out[section] = _section(section, section_defaults(section), paths.section(section))
    out["folders"] = {}
    for name, saved in paths.section("folders").items():
        if isinstance(saved, dict):
            out["folders"][name.lower()] = _section(f"folders.{name}", FOLDER, saved, fill=False)
        elif astray("folders", name):
            _tell(("folders", name), astray("folders", name))
        else:
            _tell(("folders", name), f"[folders] {name} isn't a folder's section, [folders.{name}] with its settings on "
                                     "the lines under it, so it is left out.")
    return out


def _stamp() -> tuple:
    try:
        st = paths.settings_file().stat()
        return str(paths.settings_file()), st.st_mtime_ns, st.st_size
    except OSError:
        return str(paths.settings_file()), 0, 0


def get(section: str) -> dict:
    """One section with every key: your value, else the default. "folders": your folders' sections, by name in lower
    case, each with the keys it sets. Read once in a command: a mistake anywhere in the file stops it. A running
    server (WATCH) looks again every CHECK_EVERY seconds; a mistake made then keeps the values from before, and is
    told once."""
    with _lock:
        now = time.monotonic()
        if _state["values"] is None or (WATCH and now - _state["checked"] >= CHECK_EVERY):
            _state["checked"] = now
            stamp = _stamp()
            if stamp == _state["stamp"] and _state["values"] is not None:
                _state["told"] = None  # the file as read: put right, so a mistake made again is told again
            else:
                try:
                    _state.update(values=_load(), stamp=stamp, told=None)
                except (SystemExit, Exception) as e:  # noqa: BLE001 - the values from before stay
                    if _state["values"] is None:
                        raise
                    said = str(e) if isinstance(e, SystemExit) else f"{paths.settings_file()}: {type(e).__name__}: {e}"
                    if _state.get("told") != said:
                        _state["told"] = said
                        print(f"{said}\nThe settings from before stay in effect until it is put right.", file=sys.stderr)
        return _state["values"][section]


def reload() -> None:
    """Forget the values read so far, as a new command would (tests)."""
    with _lock:
        _state.update(stamp=None, values=None, checked=-1e9, told=None)
        _warned.clear()


# ---- the format a title is kept in: one order, said once ------------------------------------------------------------

def formats_for(kind: str) -> tuple[list[str], bool]:
    """A kind's formats, best first ([download]): its own list if it has one, else its device's, the tablet's for a
    kind read on a tablet ([kinds.X] device in the rules) and the e-reader's for every other; and whether that device
    is the e-reader."""
    from . import rules

    found = rules.current().kinds.get(kind)
    ereader = not (found and found.device == "tablet")
    d = get("download")
    return (d.get(kind) if found else None) or d["ereader" if ereader else "tablet"], ereader


def pick_file(kind: str, offered: list, prefer: str = "", had=()) -> tuple:
    """The one of a title's files it is kept in, and why in a few words: of the first format of its kind's list that
    is offered (formats_for), the largest file. "largest" on the list: the largest file of the formats after it, or of
    any when none of those is; files this near it count as as large ([download] size_tie), and the first of them
    listed wins. Nothing on the list offered: the largest file. prefer: the format you chose for the title (hylly set
    format), when it is offered. had: the formats it is downloaded in already, as its own choice: one of them stays
    while it is offered, so a change of the order downloads nothing again. Only files in the language to keep count
    ([download] language). Humble's plan picks its download so, the catalog a title's main file, and the book inside a
    ZIP package that is read. offered: a shop's files (with ext, size and label), or [path, ext, size] as the catalog
    keeps them."""
    if prefer:
        mine = [f for f in offered if _about(f)[0] == prefer]
        if mine:
            return pick_file(kind, mine)[0], f"{prefer.upper()}: your choice"
        best, why = pick_file(kind, offered, had=had)
        return best, f"{why} ({prefer.upper()}, your choice, is not offered)"
    files = in_language(offered)
    order, ereader = formats_for(kind)
    best, why = _first(order, files, ereader)
    kept = [f for f in files if _about(f)[0] in had]
    if kept and _about(best)[0] not in had:
        best = _first(order, kept, ereader)[0]
        return best, f"{_about(best)[0].upper()}: as downloaded before"
    return best, why


def in_language(files: list) -> list:
    """The files in the language to keep ([download] language): those whose label names it, else those whose label
    names no other language; all of them when every one does. Files without labels (the catalog's) as they are."""
    if not any(_about(f)[2] for f in files):
        return files
    from .rules import current

    r, keep = current(), get("download")["language"]
    if keep:
        if keep not in r.languages:
            raise Exit(NOT_SET_UP, f"{paths.settings_file()}: [download] language = {keep!r}: use one of the "
                                   f"[languages] in the rules ({', '.join(r.languages)}), or \"\" for the file whose "
                                   "label names none.")
        mine = [f for f in files if r.languages[keep].find(_about(f)[2]) is not None]
        if mine:
            return mine
    return [f for f in files if not r.other_language(_about(f)[2], keep)] or files


def _about(f) -> tuple[str, float, str]:
    """A file as the choice sees it: its format, its size and its label (a shop's own name for it, "PDF (HQ)")."""
    if isinstance(f, (list, tuple)):
        return f[1], f[2] if len(f) > 2 and isinstance(f[2], (int, float)) else 0, ""
    return f.ext, f.size or 0, getattr(f, "label", "") or ""


def _first(order: list[str], files: list, ereader: bool) -> tuple:
    """The file a list picks of these, and why, in the words Humble's plan has always used."""
    at = order.index(LARGEST) if LARGEST in order else len(order)
    for fmt in order[:at]:
        mine = [f for f in files if _about(f)[0] == fmt]
        if mine:
            return max(mine, key=lambda f: _about(f)[1]), _why(fmt, files, ereader)
    after = order[at + 1:]
    candidates = [f for f in files if _about(f)[0] in after] or files
    top, tie = max(_about(f)[1] for f in candidates), get("download")["size_tie"]
    near = [f for f in candidates if _about(f)[1] >= top * tie]
    best = min(near, key=lambda f: (after.index(_about(f)[0]) if _about(f)[0] in after else len(after), -_about(f)[1]))
    if len(candidates) == 1:
        return best, "only format offered"
    if _about(best)[1] == top:
        return best, "largest file"
    return best, f"within {100 - tie * 100:.0f}% of the largest; {_about(best)[0].upper()} preferred"


def _why(fmt: str, files: list, ereader: bool) -> str:
    """Why a format on the list was taken: for an e-reader, as text; for a tablet, by the format's layout."""
    from .rules import current

    layout = getattr(current().formats.get(fmt), "layout", "")
    if not ereader and layout == "comic":
        return f"{fmt.upper()} preferred when offered"
    if not ereader and layout == "fixed":
        return f"{fmt.upper()} keeps the print layout" if len(files) > 1 else "only format offered"
    return "only format offered" if len({_about(f)[0] for f in files}) == 1 else f"{fmt.upper()} preferred for text"


def rows(built_in: dict | None = None) -> list[dict]:
    """Every setting with its value as it's used, and where that comes from: "environment" (with the variable's name),
    "settings.toml" or "default"; then what is left out. The value of anything left out is never shown, in any section or
    at the top: a topic or a shop's cookie written in the wrong place, or still where the alarms' secrets were kept
    before, is a secret all the same. built_in: the folders Hylly knows by name (folders.defaults), given by the caller, as the
    settings, part of the core, may not reach for them. For hylly settings show, and its --json."""
    BUILT_IN = built_in or {}
    from .rules import toml_value

    def row(section: str, key: str, value, given: bool, table: str = "") -> dict:
        table = table or section
        variable = FROM_ENVIRONMENT.get((table, key))
        if variable and paths._env(variable):  # the environment's, instead of the file's
            value, came = paths._env(variable), f"environment: {variable}"
        elif variable == "HYLLY_REMOTE" and variable in os.environ:  # set and empty: refused, not taken for none (remote.server)
            value, came = "", "environment: HYLLY_REMOTE, set and empty: a mistake that stops every command passed on; unset it"
        else:
            came = "settings.toml" if given else "default"
        if not given and (table, key) in UNSET and not variable:
            shown = f"({UNSET[table, key]})"
        elif (table, key) == ("backup", "keep"):
            shown = keep_written(value) + ("" if value else " (every zip stays)")
        elif isinstance(value, dict):
            shown = "{ " + ", ".join(f"{toml_value(k)} = {toml_value(v)}" for k, v in value.items()) + " }" if value else "{}"
            shown += f" ({EMPTY[table, key]})" if not value and (table, key) in EMPTY else ""
        else:
            empty = EMPTY.get((table, key)) or (EMPTY_KIND if table == "download" and isinstance(value, list) else "")
            shown = toml_value(value) + (f" ({empty})" if not value and empty else "")
        return {"section": section, "key": key, "value": shown, "from": came}

    def left(section: str, key: str, why: str) -> dict:
        return {"section": section, "key": key, "value": "(not shown)", "from": why}

    top = get("")
    out = [row("", key, top.get(key, default), key in top, "") for key, default in TOP.items()]
    for section in DEFAULTS:
        values = get(section)
        out += [row(section, key, values[key], key in values["_set"])
                for key in section_defaults(section) if key in values]
        out += [left(section, key, "no longer used: left out" if retired(section, key) else
                     f"not one of [{section}]'s settings: left out") for key in values["_unknown"]]
    for name, values in get("folders").items():
        out += [row(f"folders.{name}", key, values[key] if key in values["_set"] else BUILT_IN.get(name, {}).get(key, default),
                    key in values["_set"], "folders") for key, default in FOLDER.items()]
        out += [left(f"folders.{name}", key, "no longer used: left out" if retired("folders", key) else
                     f"not one of [folders.{name}]'s settings: left out") for key in values["_unknown"]]
    for name, value in top["_unknown"].items():
        out.append(left(name, "", "not one of the sections: left out") if isinstance(value, dict) else
                   left("", name, "at the top, not one of the settings there: left out"))
    return out


def listing(built_in: dict | None = None) -> list[str]:
    """rows, as hylly settings show prints them: each section's settings under its name, the top's first, each marked
    with where it comes from."""
    lines, section = [], None
    for r in rows(built_in):
        if r["section"] != section:
            section = r["section"]
            if section and r["key"]:
                lines.append(f"  [{section}]")
            elif not section:
                lines.append("  (at the top, before any [section])")
        if not r["key"]:  # a section there is none of
            lines.append(f"  [{r['section']}]".ljust(60) + f"  # {r['from']}")
        elif not r["section"] and r["value"] == "(not shown)":  # a name at the top that isn't a setting: by name only
            lines.append(f"  {r['key']}".ljust(60) + f"  # {r['from']}")
        else:
            lines.append(f"    {r['key']} = {r['value']}".ljust(60) + f"  # {r['from']}")
    return lines


def problems() -> tuple[list[str], list[str]]:
    """Every mistake in the settings file, not only the first, which is all a command is told (get); and what is left out,
    which isn't one: for hylly settings check. Read afresh, as written now."""
    try:
        found = paths._settings()
    except Exit as e:  # not TOML at all, or not UTF-8: nothing more can be read
        return [e.message], []
    mistakes, left = [], []
    file = paths.settings_file()
    from . import rules

    try:  # the kinds a setting may name are the rules': rules that can't be read are said first, as the cause
        rules.load(rules.sources())  # read afresh, as written now, and kept nowhere
    except (SystemExit, Exception) as e:  # noqa: BLE001 - said, and the kinds checked against the shipped ones
        mistakes.append(f"The rules can't be read, so the kinds below are checked against the shipped ones: "
                        f"{e if isinstance(e, SystemExit) else f'{type(e).__name__}: {e}'}")

    def checked(section: str, key: str, value, default) -> None:
        try:
            _check(section, key, value, default)
        except Exit as e:
            mistakes.append(e.message)

    for name in (*DEFAULTS, "folders"):
        if name in found and not isinstance(found[name], dict):
            mistakes.append(f"{file}: {name} = ...: a section, [{name}], with its settings on the lines under it.")
    for name, value in found.items():
        if name in TOP:
            checked("", name, value, TOP[name])
        elif isinstance(value, dict) and astray("", name):
            left.append(f"{file}: {astray('', name)}")
        elif name not in (*DEFAULTS, "folders"):
            what = f"[{name}] isn't one of its sections" if isinstance(value, dict) else f"{name}, at the top, isn't one of the settings there"
            left.append(f"{file}: {what}, so it is left out.")
    for section in DEFAULTS:
        defaults = section_defaults(section)
        saved = found.get(section) if isinstance(found.get(section), dict) else {}
        for key, value in saved.items():
            if key in defaults:
                checked(section, key, value, defaults[key])
            elif astray(section, key):
                left.append(f"{file}: {astray(section, key)}")
            else:
                why = (f"is no longer used, so it is left out. {retired(section, key)}." if retired(section, key)
                       else "isn't one of its settings, so it is left out.")
                left.append(f"{file}: [{section}] {key} {why}")
    folders = found.get("folders") if isinstance(found.get("folders"), dict) else {}
    for name, saved in folders.items():
        if not isinstance(saved, dict):
            left.append(f"{file}: " + (astray("folders", name) or f"[folders] {name} isn't a folder's section, "
                                                                   f"[folders.{name}], so it is left out."))
            continue
        for key, value in saved.items():
            if key in FOLDER:
                checked(f"folders.{name}", key, value, FOLDER[key])
            elif astray(f"folders.{name}", key):
                left.append(f"{file}: {astray(f'folders.{name}', key)}")
            else:
                instead = retired(f"folders.{name}", key)
                why = (f"is no longer used, so it is left out. {instead}." if instead
                       else "isn't one of its settings, so it is left out.")
                left.append(f"{file}: [folders.{name}] {key} {why}")
    return mistakes, left


# ---- the server's own secrets: the alarms' ntfy address and token ----------------------------------------------------------

def secrets_dir() -> Path:
    """The server's own secrets, in the settings folder: the ntfy address, whose topic is as good as a password for reading
    the alarms, and its token, each in a file of its own (SECRETS). A folder apart from the logins, so that a walled
    server reads these and never those (examples/hylly.service)."""
    return paths.config_dir() / "secrets"


PLACES = ("environment", "a file", "systemd", "secrets")  # where a secret may come from: the first that has it counts


def _read(path: Path) -> str:
    """A secret's file, without its line's end: UTF-8, with Notepad's mark or without, or UTF-16 with its mark, as
    PowerShell 5's echo > writes one. ValueError for anything else."""
    raw = path.read_bytes()
    return raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig").strip()


def _unreadable(path: Path, which: str, name: str, e: Exception) -> Exit:
    """A secret's file that can't be read, as the Exit that says which, never what's in it; with the file (path) for
    hylly doctor's next step."""
    why = getattr(e, "strerror", None) or ("it isn't text" if isinstance(e, ValueError) else str(e))
    stopped = Exit(NOT_SET_UP, f"{path}{which}, which holds the alarms' {SECRETS[name][0]}, can't be read ({why}): save "
                               "it again as text, or take it away.")
    stopped.path = path
    return stopped


def _from(place: str, name: str) -> tuple[str, str]:
    """One secret from one of the PLACES: its value, and where it came from in words; ("", "") when that place hasn't
    it. The file a _FILE variable names must be there."""
    variable = SECRETS[name][1]
    if place == "environment":
        value = paths._env(variable).strip()
        return (value, f"{variable} in the environment") if value else ("", "")
    path, which = None, ""
    if place == "a file" and paths._env(f"{variable}_FILE"):
        path, which = Path(paths._env(f"{variable}_FILE")), f" ({variable}_FILE)"
    elif place == "systemd" and paths._env("CREDENTIALS_DIRECTORY"):
        path, which = Path(paths._env("CREDENTIALS_DIRECTORY")) / name, f" (systemd's credential {name})"
    elif place == "secrets":
        path = secrets_dir() / name
    if path is None:
        return "", ""
    try:
        value = _read(path)
    except FileNotFoundError:
        if place != "a file":
            return "", ""
        stopped = Exit(NOT_SET_UP, f"{variable}_FILE names {path}, which isn't there: put the file there, or unset "
                                   f"{variable}_FILE.")
        stopped.path = path
        raise stopped from None
    except (OSError, ValueError) as e:  # a folder, a file it may not read, or one that isn't text
        raise _unreadable(path, which, name, e) from None
    return (value, f"{path}{which}") if value else ("", "")


def secret_from(name: str) -> tuple[str, str]:
    """One of the server's secrets, and where it came from (a file's path, or a variable's name); ("", "") when none has
    it. The address from the first place that has it: its variable (HYLLY_NTFY_URL); the file its variable with _FILE
    after it names, as Docker gives its secrets; systemd's credential of its name, which LoadCredential=ntfy-url:... in
    a unit puts in $CREDENTIALS_DIRECTORY; its file in secrets/. The token from where the address came, and nowhere
    else, so that a token kept for one ntfy server never goes to an address given elsewhere; with no address, from the
    first that has it, to be shown. A file named but missing, or there and unreadable, stops with its name, as a mistake
    in the settings does: the alarms would otherwise go quiet without a word."""
    places = PLACES
    if name != "ntfy-url":
        address = next((place for place in PLACES if _from(place, "ntfy-url")[0]), None)
        places = (address,) if address else PLACES
    for place in places:
        value, where = _from(place, name)
        if value:
            return value, where
    return "", ""


def secret(name: str) -> str:
    return secret_from(name)[0]


def old_secrets(bell: dict) -> list[str]:
    """The keys an older Hylly kept the alarms' secrets under that [bell], as written (paths.section("bell")), still
    sets: never read, and told of as no longer used (RETIRED), but a secret all the same, so a backup leaves
    settings.toml out while one is there, and hylly doctor says so."""
    return [key for key, _ in SECRETS.values() if bell.get(key) not in (None, "")]


def make_secrets_dir() -> None:
    """secrets/, its owner's only: there for the secrets to be put in, and before a walled server's unit starts, which
    names it (examples/hylly.service)."""
    folder = secrets_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # a disk that doesn't do permissions, or the walls' read-only one
        os.chmod(folder, 0o700)


def secrets_at_start() -> None:
    """As a command or the server starts, but for a dry run: secrets/ made if it isn't there (make_secrets_dir). Where
    it can't be, as inside a walled server's unit, nothing is said: the folder is the owner's to make then."""
    with contextlib.suppress(OSError):
        make_secrets_dir()
