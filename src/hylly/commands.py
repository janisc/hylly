"""Every command and option of Hylly, as argparse sees them, and the command reference made from them: the help page's,
and hylly commands. hylly takes every command; hyllyd, the library server itself, has a parser of its own here
(daemon_parser) for daemon.py. Only the words: what each command does is cli.py's, which imports everything, while this
may be read by the parts that mustn't, such as the server writing the help page (parts.py)."""

from __future__ import annotations

import argparse
import re
import textwrap

from .exits import TABLE
from .rules import DEVICES, shipped_kinds
from .runs import JOBS, seconds_of
from .shops import SHOPS
from .sites import KEYS, LOOKUPS, SITES

LOGINS = list(SITES)
COOKIE_LOGINS = [site for site in SITES if site not in KEYS]  # a browser's login cookie; the others take a key


def _listed(names) -> str:
    """Names as a sentence lists them: "a, b and c"."""
    names = list(names)
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)



def _and(names) -> str:
    """Names in words: "a, b and c"."""
    names = list(names)
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)


AREAS = {  # hylly --help, hylly alone and the help page's reference list the commands by what they're for
    "To start": ("demo", "init", "doctor", "settings"),
    "Getting books": ("login", "download", "list", *SHOPS, "add", "decide"),
    "The catalog": ("catalog", "lookup", "tag", "set", "rules", "verify"),
    "The library server": ("serve", "server", "user", "people", "reviews", "reports", "theme", "bell"),
    "Copies and backups": ("copy", "backup", "restore"),
    "Looking on": ("jobs", "logs", "commands"),
}
CHECKS = ("copy", "backup")  # hylly copy check TARGET, beside hylly copy SOURCE TARGET: the word check comes first
ALONE = {"jobs": "list", "bell": "show", "reviews": "list", "theme": "list", "settings": "show"}  # the command alone is this action of its own
HIDDEN = frozenset({"copy check", "backup check", "internal"})  # reached by other words, or between Hylly's own processes

START = f"""Hylly: a home library for the e-books and comics you own.

To start:
  hylly demo            a small library of made-up books, to try it on
  hylly init FOLDER     make a folder your library
  hylly login SITE      save a shop's login: {', '.join(SHOPS)}
  hylly download        download what you own, from every shop you have a login for
  hylly catalog         build the catalog
  hylly serve           the library page and the reader, in your browser
  hylly doctor          what shape it is in, with the next step

hylly --help lists every command; hylly COMMAND --help tells more of one."""
ASSISTANTS = ("For an AI assistant: AGENTS.md, in Hylly's source, says where to look first, what is the owner's alone, "
              "and how to try things apart from the real library.")  # hylly --help's last line, and hylly commands'


def version() -> str:
    """Hylly's version: the installed package's, which is pyproject.toml's; __version__ when it runs from its source
    without being installed."""
    from importlib import metadata

    try:
        return metadata.version("hylly")
    except metadata.PackageNotFoundError:
        from . import __version__

        return __version__


def how_often(text: str) -> float:
    """--progress, for a copy, a backup and their checks."""
    try:
        return seconds_of(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def count(text: str) -> int:
    """--limit and the like. A negative number would count from the end of the list, which is nearly everything."""
    if not text.isdecimal():
        raise argparse.ArgumentTypeError("a whole number, 0 or more")
    return int(text)


def download_jobs(text: str) -> int:
    """--jobs towards a shop: its servers are shared, whatever this computer could do."""
    if not text.isdecimal() or not 1 <= int(text) <= 6:
        raise argparse.ArgumentTypeError("a number from 1 to 6, as a shop's servers are shared")
    return int(text)


READ_AT_ONCE = 64  # files hylly catalog --jobs reads in parallel at most: more only waits on the disk, a thread each


def read_jobs(text: str) -> int:
    """hylly catalog --jobs: 0 would read nothing, and a run with --rebuild would have set its covers aside first."""
    if not text.isdecimal() or not 1 <= int(text) <= READ_AT_ONCE:
        raise argparse.ArgumentTypeError(f"a number from 1 to {READ_AT_ONCE}")
    return int(text)


def port(text: str) -> int:
    """--port, for a server: a port there is, as port under [server] in the settings is checked (settings.py)."""
    if not text.isdecimal() or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError("a port, a whole number from 1 to 65535")
    return int(text)


def comma_list(allowed, what: str, check: bool = True):
    """The type of an option that takes words comma-separated (--kind, --device, --types, --source): the words given, in
    small letters, each once, in their order. None given, or one that isn't among allowed, is argparse's usage error,
    which names the words there are, so that a word mistyped never quietly chooses nothing. allowed: those words, or a
    function that gives them as the option is read; check False: any word is taken here, to be checked as the command
    runs (--kind: the rules' kinds, yours and --rules' too, are read only then: cli._kinds), and allowed only named."""
    def words(text: str) -> tuple[str, ...]:
        given = tuple(dict.fromkeys(w.strip().lower() for w in text.split(",") if w.strip()))
        there = tuple(allowed() if callable(allowed) else allowed) if check or not given else ()
        wrong = [w for w in given if check and w not in there]
        if not given or wrong:
            said = ", ".join(wrong) or f"no {what}"
            raise argparse.ArgumentTypeError(f"{said}? Use {_listed(there)}, comma-separated")
        return given
    return words


# --kind: kinds by name. Which there are is the rules' (kinds.toml's [kinds.X], and yours, --rules ones too), which are
# read as the command runs, on the computer it runs on: it checks them (cli._kinds); none given names those that come
kinds = comma_list(shipped_kinds, "kind", check=False)


def _kinds_words(what: str = "only these kinds, comma-separated") -> str:
    """--kind's help: the kinds as they come, and those your rules add."""
    return f"{what}: {', '.join(shipped_kinds())}, or one your rules add (hylly rules show kinds)"


# DEVICES (rules.py): where a title is best read (plan.device_for), --device of Humble's download
devices = comma_list(DEVICES, "device")
# itch.io's kinds of item, as its survey groups them by their classification (itchio.GROUPS): --types of its claim, and
# of its download with "reading" (the items of other kinds whose files are all books or comics). Here, as the words may
# not read the shops' code (parts.py): tests/test_itchio.py holds them to itchio.GROUPS
ITCH_TYPES = ("book", "comic", "tabletop", "other", "game")


EXAMPLES: dict[str, tuple[str, ...]] = {  # a command's own --help ends with these: what it's for, by a line or two of use
    "login": ("hylly login humble                    # asks for the cookie",
              "hylly login drivethrurpg --clipboard  # the key from the clipboard",
              "hylly login humble --cookies cookies.txt   # the browser's cookies, exported",
              "hylly login humble --forget           # delete the saved one"),
    "humble": ("hylly humble download --dry-run    # only list what would be downloaded",
               "hylly humble download --limit 5    # the first five titles, to start small",
               "hylly humble list ~/humble-list.csv            # every file, to edit: then",
               "hylly humble download ~/humble-list.csv        # by your changes",
               "hylly humble seed D:/old-books     # copy in the files you have already"),
    "humble list": ("hylly humble list --kind comic    # the comics alone",
                    "hylly humble list -               # to standard output: on the server too"),
    "humble download": ("hylly humble download --limit 5",
                        "hylly humble download --kind comic --dry-run"),
    "humble seed": ("hylly humble seed D:/old-books --dry-run",),
    "storybundle": ("hylly storybundle download --dry-run    # only list what would come",),
    "drivethrurpg": ("hylly drivethrurpg download --dry-run   # only list what would come",),
    "itchio": ("hylly itchio survey                  # what your library and bundles hold",
               "hylly itchio claim --dry-run         # only count what would be claimed",
               "hylly itchio download --types book,comic"),
    "download": ("hylly download --dry-run    # what each shop would download",
                 "hylly download --limit 5    # five titles from each shop, to start small",
                 "hylly download ~/hylly-list.csv --dry-run   # what your changes would bring"),
    "list": ("hylly list ~/hylly-list.csv     # every shop's in one file, to edit",
             "hylly list -                # to standard output: on the server too",
             "hylly list --dry-run        # how many rows each shop's part would have"),
    "add": ("hylly add ebooks --dry-run   # only list what would be copied",
            "hylly add ebooks",
            "hylly add --inbox --dry-run  # where each of the Inbox's books would go"),
    "settings": ("hylly settings show", "hylly settings check    # every mistake in the file", "hylly settings edit",
                 "hylly settings places    # where Hylly keeps each kind of its own files"),
    "copy": ("hylly copy SOURCE TARGET --dry-run        # the plan: nothing is written",
             "hylly copy SOURCE TARGET --progress 6h",
             "hylly copy check TARGET                  # read every copy again"),
    "copy check": ("hylly copy check TARGET --progress 1h",),
    "backup": ("hylly backup TARGET --dry-run             # the plan: nothing is written",
               "hylly backup TARGET",
               "hylly backup                             # to the settings' [backup] target",
               "hylly backup check TARGET                # read every copy again"),
    "backup check": ("hylly backup check TARGET", "hylly backup check TARGET --all          # read back every zip too"),
    "restore": ("hylly restore BACKUP NEW_FOLDER --dry-run  # the plan: nothing is written",
                "hylly restore BACKUP NEW_FOLDER --saved 2026-11-06"),
    "catalog": ("hylly catalog --lookup     # first ask the lookup sources about titles",),
    "lookup": ("hylly lookup --dry-run     # only count what each source would be asked",
               "hylly lookup --source openlibrary --limit 50"),
    "serve": ("hylly serve --background    # hidden, with the settings",
              "hylly serve --lan           # here, for the tablets too, this time",
              "hylly serve --dry-run       # what it would start",
              "hylly serve --service-file  # Linux: print the walled service"),
    "server": ("hylly server status",
               "hylly server restart    # with the settings, as they are now",
               "hylly server autostart on"),
    "user": ("hylly user add ann --role reader",
             "hylly user role ann librarian",
             "hylly user link ann    # a one-time link, for ann's first login",
             "hylly user restore ann --dry-run    # ann, removed: what would come back",
             "hylly user list"),
    "reviews": ("hylly reviews export --year 2026 --out reviews-2026.md",),
    "people": ("hylly people export ann        # all that's kept about ann, as JSON",
               "hylly people erase ann --dry-run   # ann, removed: what would go for good"),
    "bell": ("hylly bell test",),
    "tag": ('hylly tag add to-read --search "lantern wolf" --dry-run',
            "hylly tag list to-read"),
    "decide": ("hylly decide                 # what the downloads and catalog left to you",
               "hylly decide 7 2               # answer question 7 with its second option",
               "hylly decide 7 keep --dry-run  # what keeping the guess would do",
               "hylly decide --answered        # your answers (7 auto takes one back)",
               "hylly decide --kind match      # the lookups' near matches"),
    "set": ('hylly set kind comic --bundle "Moonwake Comics" --dry-run',
            'hylly set authors "Ann Writer" --search "the lantern keeper"',
            'hylly set series auto --search "the lantern keeper"   # as found again',
            'hylly set copies apart --search "the lantern keeper"  # two books, not one',
            "hylly set refused openlibrary --id f:0a1b2c3d4e5f  # a wrong match"),
    "verify": ("hylly verify --hash    # also read each downloaded or copied file again",),
    "jobs": ("hylly jobs --json", "hylly jobs stop humble    # end a Humble download that has hung"),
    "logs": ("hylly logs server --tail 100",),
    "demo": ("hylly demo                 # made, and served here until Ctrl-C",
             "hylly demo --background    # served on its own, for a script",
             "hylly demo --stop          # that server stopped, and no other"),
    "rules": ("hylly rules show genres",
              "hylly rules copy genres                 # into your rules folder, to edit",
              'hylly rules try "Lantern Wolf 003 (2026).cbz"'),
}


class Lines(argparse.HelpFormatter):
    """A command's own --help: each line of its description and examples wrapped to the window on its own, with its
    indent, so that the examples stay one to a line (the usual formatter would run them together). An example too long
    for a narrow window goes on indented further, and is never broken inside an option's name."""

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        out = []
        for line in text.splitlines():
            lead = line[:len(line) - len(line.lstrip())]
            out.append(textwrap.fill(line.strip(), width, initial_indent=indent + lead, subsequent_indent=indent + lead * 3,
                                     break_long_words=False, break_on_hyphens=False) if line.strip() else "")
        return "\n".join(out)


class _Listed(dict):
    """The commands by name, as argparse keeps them: the hidden ones (HIDDEN) are there to be run, and left out of what
    a mistyped command is told to choose from."""

    def __iter__(self):
        return (name for name in super().__iter__() if name not in HIDDEN)


class _Commands(argparse._SubParsersAction):
    """hylly's commands. Two kinds of first word go to a parser of their own: copy check and backup check (beside copy
    SOURCE TARGET and backup TARGET), and a command alone that is one of its actions (ALONE: hylly jobs is jobs list)."""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self._name_parser_map = self.choices = _Listed()

    def __call__(self, parser, namespace, values, option_string=None):
        name, *rest = values
        if name in CHECKS and rest[:1] == ["check"]:
            values = [f"{name} check", *rest[1:]]
        elif name in ALONE and not (rest[:1] and (rest[0] in ("-h", "--help") or rest[0] in actions_of(self.choices[name]))):
            values = [name, ALONE[name], *rest]
        super().__call__(parser, namespace, values, option_string)


def actions_of(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """A command's actions, by name: {} for one without."""
    found = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
    return dict(found.choices.items()) if found else {}


LIBRARY = "the library's folder (default: HYLLY_LIBRARY, else library in settings.toml)"
RULES = "also these rules files, read after your rules folder (a file, or a folder of them; may be given more than once)"
SHORT = {"library": "the library's folder (see Library and rules, below)", "rules": "more rules files (see Library and rules, below)"}


def _library(p) -> None:
    p.add_argument("--library", help=LIBRARY)


def _rules(p, default=None) -> None:
    p.add_argument("--rules", action="append", metavar="FILE_OR_FOLDER", help=RULES, default=default)


def _both(p) -> None:
    """--library and --rules, for a command that reads both."""
    _library(p)
    _rules(p)


def _dry(p, words: str) -> None:
    p.add_argument("-n", "--dry-run", action="store_true", help=words)


def _yes(p, words: str) -> None:
    p.add_argument("-y", "--yes", action="store_true", help=words)


def _json(p, words: str = "as JSON") -> None:
    p.add_argument("--json", action="store_true", help=words)


RESULT = ("its result as one JSON object on standard output, with its exit code and what that means; what it says on the way "
          "goes to standard error")


def _result(p) -> None:
    """--json for a command whose result is one object, however it ends (cli.ONE_OBJECT)."""
    _json(p, RESULT)


def shop_download(a, shop, by: str = "", jobs: int = 2) -> None:
    """The options of hylly SHOP download, the same for every shop, on the shops' core (shopping.py), from its row in
    shops.SHOPS (--update where it says when a file has changed). by: the shop's own word for who made a title, an
    option that takes a part of the name (DriveThruRPG's --publisher); jobs: its parallel downloads unless --jobs says
    (Humble's CDN takes 4)."""
    a.add_argument("--title", help="only titles that contain this text")
    if by:
        a.add_argument(f"--{by}", dest="by", metavar=by.upper(), help=f"only {by}s whose name contains this text")
    a.add_argument("--kind", type=kinds, help=_kinds_words())
    a.add_argument("--limit", type=count, help="at most this many titles")
    a.add_argument("--jobs", type=download_jobs, default=jobs, help=f"parallel downloads, 1 to 6 (default {jobs})")
    if shop.update:
        a.add_argument("--update", action="store_true",
                       help=f"also fetch files {shop.label} has a newer version of (old ones go to superseded/)")
    _dry(a, "only list what would be downloaded: it reads your library on the site, downloads nothing and saves no snapshot")
    _result(a)
    _both(a)


# The options of a shop's download that choose no titles, which a LIST takes beside it (cli._shop_download); every other
# one of them chooses titles (--title, --kind, a shop's own: --bundle, --types), and is its list action's too
RUN_OPTIONS = frozenset({"help", "limit", "max_gb", "jobs", "update", "dry_run", "json", "again", "library", "rules", "list"})
LIST_FILE = ("where to write it (default: {name} in the current folder); - writes it to standard output, as over ssh to "
             "the server computer")


def shop_lists(sub) -> None:
    """The shop list, the same for every shop from the table (shoplist.py): hylly SHOP list [FILE], with the options of
    its download that choose titles (all but RUN_OPTIONS), and a LIST after hylly SHOP download, which it goes by: only
    your changes count (shoplist.py). None by hand: a shop's own options come from its download's parser."""
    for shop in SHOPS.values():
        if shop.id not in sub.choices:  # a shop with no command of its own yet (a test's made-up one): no download to follow
            continue
        acts = next(a for a in sub.choices[shop.id]._actions if isinstance(a, argparse._SubParsersAction))
        download = acts.choices["download"]
        chooses = [a for a in download._actions if a.dest not in RUN_OPTIONS and a.option_strings]
        download.add_argument("list", nargs="?", metavar="LIST", help="by your changes to this list (hylly "
                              f"{shop.id} list's, or hylly list's): what to fetch is the list's, so the options that "
                              "choose titles aren't taken beside it; - reads it from standard input")
        download.set_defaults(choosers=tuple((a.dest, max(a.option_strings, key=len)) for a in chooses))
        a = _maker(acts, shop.id)("list", help=f"everything you own at {shop.label} that a download takes or could take, a "
                                  "row per file, what it would fetch ticked: a CSV to edit in a spreadsheet, then hylly "
                                  f"{shop.id} download FILE goes by your changes")
        a.add_argument("file", nargs="?", metavar="FILE", help=LIST_FILE.format(name=f"{shop.id}-list.csv"))
        for chooser in chooses:  # the same options, its download's own
            a._add_action(chooser)
        a.add_argument("--force", action="store_true", help="write over a list there already (one you may have edited)")
        _dry(a, "only say how many rows it would write, and where: it reads your library on the site, and writes no file")
        _both(a)


def choosers(p) -> None:
    """How hylly tag and hylly set choose titles."""
    p.add_argument("--search", help="titles found by these words, as in the library page's search")
    p.add_argument("--bundle", help="titles from a bundle whose name contains this text")
    p.add_argument("--folder", help="titles with a file in a folder whose path contains this text")
    p.add_argument("--id", nargs="+", help="titles by catalog id")
    _dry(p, "only list the titles that would change")
    _yes(p, "do not ask before changing")


def _server_options(p) -> None:
    """What a server in the foreground may be given for its one run (hylly serve, hyllyd): one in the background, restarted
    or started at logon takes the settings, its only options."""
    p.add_argument("--port", type=port, help="for this run, in the foreground (default: port in [server] of settings.toml, else 8770)")
    p.add_argument("--lan", action=argparse.BooleanOptionalAction,
                   help="listen on every IPv4 address, for other devices at home such as a tablet or an e-reader, this run "
                        "only; --no-lan: on this computer only (default: listen in [server], else this computer only)")
    _library(p)
    _rules(p)


def _browser(p) -> None:
    p.add_argument("--no-browser", action="store_true", help="do not open the browser (one opens only on Windows and macOS, "
                                                             "or with a desktop: DISPLAY or WAYLAND_DISPLAY set)")


def _maker(sub, name_of: str = ""):
    """add(name, help=...): a parser for one command or action, as each of them is made: its help line is the line in
    the list, its own --help says the same as its first words and then the examples, and no option may be shortened."""
    def add(name: str, **kw) -> argparse.ArgumentParser:
        full = f"{name_of} {name}".strip()
        words = kw.pop("about", None) or kw.get("help", "")
        # a plain first word gets its capital; a name keeps its own, as itch.io does
        about = words[:1].upper() + words[1:] if re.match(r"[a-z]+(?![\w.])", words) else words
        kw.update(description=about if about.endswith(".") else about + ".", formatter_class=Lines, allow_abbrev=False,
                  epilog="Examples:\n" + "\n".join(f"  {line}" for line in EXAMPLES[full]) if full in EXAMPLES else None)
        p = sub.add_parser(name, **kw)
        p.set_defaults(leaf=full)
        return p
    return add


def _actions(p, name: str, required: bool = True):
    """The actions of a command, each a parser of its own (hylly humble download --help)."""
    sub = p.add_subparsers(dest="action", required=required, title="actions", parser_class=argparse.ArgumentParser)
    return _maker(sub, name)


def build_parser() -> argparse.ArgumentParser:
    """Every command and option of hylly; also the source of the help page's command reference (reference_html) and of
    hylly commands (commands_data)."""
    from .remote import LOCAL, LOCAL_JOBS  # what always runs here, named as remote.py has it

    parser = argparse.ArgumentParser(prog="hylly", allow_abbrev=False, epilog=epilog(),
                                     description="Hylly: a home library for the e-books and comics you own.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"hylly {version()}", help="which version of Hylly this is")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("-H", "--remote", metavar="HOST",
                       help="run the command on HOST, the server computer, over ssh: its name as ssh knows it (default: "
                            "HYLLY_REMOTE, else [remote] ssh in the settings). Those about this computer and its disks "
                            f"always run here: {', '.join(LOCAL)}, and jobs stop {' or '.join(LOCAL_JOBS)}")
    where.add_argument("--here", action="store_true", help="run it on this computer, whatever HYLLY_REMOTE or [remote] ssh says")
    parser.add_argument("--library", dest="library_first", metavar="FOLDER", help="the library's folder, as --library after "
                        "the command gives it, for a command that uses one: hylly --library FOLDER COMMAND")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", action=_Commands, title="commands")
    add = _maker(sub)

    # ---- getting books
    p = add("login", help="save a shop's login or a lookup source's key, and check it; or forget one saved")
    p.add_argument("site", nargs="?", choices=LOGINS, help=f"{_listed(s for s in COOKIE_LOGINS if s in SHOPS)} take a cookie "
                   f"from the browser; {_listed([*(s for s in SITES if s in KEYS and s in SHOPS), *(sid for sid, look in LOOKUPS.items() if look.key)])} a key; itchio-bundles is itch.io's login "
                   "cookie, for the bundles")
    given = p.add_mutually_exclusive_group()
    given.add_argument("--clipboard", action="store_true", help="take it from the clipboard instead of a prompt")
    given.add_argument("--stdin", action="store_true", help="read it from standard input, a pipe or a file others can't "
                       "read, as docker login --password-stdin does: never shown")
    given.add_argument("--cookies", metavar="FILE", help=f"for {_listed(COOKIE_LOGINS)}: take the login cookie "
                       "from a cookies.txt the browser's cookies were exported to (Netscape's format, as yt-dlp takes), "
                       "which others may not read; - reads it from standard input")
    given.add_argument("--forget", action="store_true", help="delete SITE's saved login or key instead, and say how to end "
                       "it at the site, where it works until it's ended there")
    given.add_argument("--forget-all", action="store_true", help="delete every saved login and key (no SITE), and say how "
                       "to end each at its site")
    _dry(p, "check it with the site, and save nothing; with --forget or --forget-all, only say what it would delete")

    p = add("humble", help="Humble Bundle: download what you own, the best format of each title (resumable)")
    act = _actions(p, "humble")
    a = act("download", help="read your orders and download what's new, the best format of each title")
    a.add_argument("--device", type=devices, help=f"only titles best read on these, comma-separated: {', '.join(DEVICES)}")
    a.add_argument("--bundle", help="only bundles whose name contains this text")  # Title.under, the title's bundle
    a.add_argument("--max-gb", type=float, help="stop adding files after this many GB")
    shop_download(a, SHOPS["humble"], jobs=4)
    a = act("seed", help="copy in matching files you already have, so they aren't downloaded")
    a.add_argument("folders", nargs="+", metavar="FOLDER", help="the folders to look in (only read)")
    a.add_argument("--kind", type=kinds, help=_kinds_words())
    a.add_argument("--snapshot", help="this snapshot file instead of the latest")
    _dry(a, "only report what would be copied: offline, and writes nothing")
    _result(a)
    _both(a)

    p = add("storybundle", help="StoryBundle: download every book into storybundle/ (resumable)")
    a = _actions(p, "storybundle")("download", help="every book not downloaded yet")
    a.add_argument("--bundle", help="only bundles whose name contains this text")  # Title.under, the book's bundle
    a.add_argument("--no-bonus", action="store_true", help="skip the free newsletter bonus bundles")
    shop_download(a, SHOPS["storybundle"])

    p = add("drivethrurpg", help="DriveThruRPG: download every file in your library into drivethrurpg/ (resumable)")
    shop_download(_actions(p, "drivethrurpg")("download", help="every file not downloaded yet"), SHOPS["drivethrurpg"],
                  by="publisher")

    p = add("itchio", help="itch.io: survey counts what your library and bundles hold, by kind, and the space it would take; "
                           "claim claims the bundles' items you want; download fetches the books, comics and tabletop games")
    act = _actions(p, "itchio")
    a = act("survey", help="every item's kind, files and sizes, in state/itchio/survey.txt and .csv (read only at itch.io)")
    a.add_argument("--again", action="store_true", help="read the bundles' pages and ask about every item again, instead of "
                                                        "reusing an earlier survey's answers")
    _dry(a, "only say what a survey would read and write, and when the last one was made: offline")
    _result(a)
    _both(a)
    a = act("claim", help="the unclaimed items the survey found, one at a time")
    a.add_argument("--types", type=comma_list(ITCH_TYPES, "type"),
                   help=f"these types, comma-separated: {', '.join(ITCH_TYPES)} (default: book, comic, tabletop, "
                        "other)")
    a.add_argument("--all", action="store_true", help="every item the survey has classified, games and software too")
    a.add_argument("--every", type=float, default=20.0, help="seconds between claims (default 20, at least 10)")
    a.add_argument("--limit", type=count, help="at most this many in this run")
    _yes(a, "don't ask first. Claiming changes your itch.io account, so use it once you have decided to")
    _dry(a, "only count what would be claimed, and how long it takes: it reads the site, and changes nothing there")
    _both(a)
    a = act("download", help="the files of the kinds on the list not downloaded yet")
    a.add_argument("--types", type=comma_list((*ITCH_TYPES, "reading"), "type"),
                   help=f"{', '.join(ITCH_TYPES)}, and reading: the items of other kinds whose files are all books or "
                        "comics, comma-separated (default: book, comic, tabletop, reading)")
    a.add_argument("--again", action="store_true", help="ask itch.io about every item's files again, instead of reusing "
                                                        "the answers kept (each is asked again once it is two weeks old, "
                                                        "or a few days more)")
    shop_download(a, SHOPS["itchio"], by="creator")  # its creator, who made it as the shops' core takes it

    shop_lists(sub)

    p = add("download", help="every shop you have a login for, one after another: hylly "
                             f"{', '.join(f'{shop} download' for shop in SHOPS)}")
    p.add_argument("list", nargs="?", metavar="LIST", help="by your changes to this list (hylly list's, or a shop's), "
                                                           "at every shop it has rows for; - reads it from standard input")
    _dry(p, "only list what each would download: each reads your library on its site to know, and none downloads anything "
            "or saves a snapshot")
    p.add_argument("--update", action="store_true", help="also fetch the files "
                   f"{_listed(shop.label for shop in SHOPS.values() if shop.update)} have replaced")
    p.add_argument("--limit", type=count, help="at most this many titles from each shop: to start small")
    _result(p)
    _both(p)

    p = add("list", help="everything you own at every shop you have a login for, in one list to edit: hylly "
                         f"{', '.join(f'{shop} list' for shop in SHOPS)}; then hylly download FILE goes by your "
                         "changes")
    p.add_argument("file", nargs="?", metavar="FILE", help=LIST_FILE.format(name="hylly-list.csv"))
    p.add_argument("--force", action="store_true", help="write over a list there already (one you may have edited)")
    _dry(p, "only say how many rows each shop's part would have: each reads your library on its site, and no file is written")
    _both(p)

    p = add("add", help="copy books into one of your own folders in the library, each copy checked (see [folders] in "
                        "settings.example.toml); with --inbox, a pass over the Inbox ([inbox]), which removes its "
                        "books from there once the library has them")
    p.add_argument("folder", nargs="?", help="your folder in the library, such as ebooks: it is made if it isn't there yet")
    p.add_argument("--inbox", action="store_true",
                   help="instead, a pass over the Inbox ([inbox] in the settings), as the server's job makes one a "
                        "minute: each book copied into the folder of yours its kind belongs in, checked, looked up and "
                        "catalogued, and only then removed from there")
    p.add_argument("--from", dest="source", help="the folder the books come from, only read (default: copy_from under "
                                                "[folders.FOLDER] in the settings)")
    p.add_argument("--list", help="a CSV with file,category rows: which book goes into which subfolder (default: list "
                                  "under [folders.FOLDER]; without one, every book is copied with its subfolders)")
    _yes(p, "with --inbox, go ahead without asking with the first pass typed by hand, which removes the Inbox's books "
            "from there once the library has them")
    _dry(p, "only list what would be copied, or with --inbox what a pass would take, set aside and remove: offline, "
            "and writes nothing")
    _result(p)
    _both(p)

    # ---- the catalog
    p = add("catalog", help="build the catalog the library page shows: search, series, genres, covers")
    p.add_argument("--lookup", action="store_true", help="first ask the lookup sources about titles without details "
                                                         "(as hylly lookup does; answers are cached)")
    p.add_argument("--lookup-limit", type=count, help="at most this many new lookups per source in this run")
    p.add_argument("--rebuild", action="store_true", help="read every file again (lookups stay cached)")
    p.add_argument("--retry-covers", action="store_true", help="read again the files recorded without a cover")
    p.add_argument("--jobs", type=read_jobs, default=6, help=f"files read in parallel, 1 to {READ_AT_ONCE} (default 6)")
    p.add_argument("--page", action="store_true", help="only make catalog/data.js again from the database, as the last "
                                                        "build kept it: nothing is read or looked up")
    _yes(p, "keep the build even with far fewer titles than the last one (a source losing more than half of its titles, "
            "or the whole catalog more than a fifth), for titles gone on purpose")
    _dry(p, "only say what a build would read and write, from what the last one found: no file is read, and nothing written")
    _result(p)
    _both(p)

    p = add("lookup", help=f"ask metadata sources ({', '.join(s.name for s in LOOKUPS.values())}) about titles without "
                           "details; resumable, then run hylly catalog")
    p.add_argument("--source", type=comma_list(LOOKUPS, "source"), help=f"only these sources, comma-separated: "
                   f"{', '.join(LOOKUPS)} (default: every one you have a key for)")
    p.add_argument("--kind", type=kinds, help=_kinds_words())
    p.add_argument("--search", help="only the titles a search finds, as in the library page")
    p.add_argument("--limit", type=count, help="at most this many titles per source in this run")
    p.add_argument("--again", action="store_true", help="ask again about titles already answered")
    _dry(p, "only count what would be asked, and show each source's site, its terms and when it was checked against the "
            "site: offline, and writes nothing")
    _json(p, "with --dry-run: as JSON")
    _both(p)

    p = add("tag", help="your own tags: add or remove one on titles you choose, or list them")
    act = _actions(p, "tag")
    for name, words in (("add", "add the tag to the titles chosen"), ("remove", "remove the tag from the titles chosen")):
        a = act(name, help=words)
        a.add_argument("tag", help="the tag, in quotes if it has spaces")
        choosers(a)
        _both(a)
    a = act("list", help="every tag with how many titles have it, or one tag's titles")
    a.add_argument("tag", nargs="?", help="the tag whose titles to show")
    _json(a)
    _both(a)
    a = act("suggest", help="find titles that words in curation/tags.toml point to, for review in the library page")
    _dry(a, "only list the suggestions: the queue for review stays as it is")
    _result(a)
    _both(a)

    p = add("set", help="correct titles' kind, whether the e-reader feed lists them, which format or file of a Humble title "
                        "to keep, or what they show: title, authors, series, number, year, publisher (auto: as found); "
                        "keep a card's copies apart, or refuse a lookup's match")
    p.add_argument("field", choices=["kind", "ereader", "format", "file", "answers", "left-out", "copies", "refused", "title",
                                     "authors", "series", "number", "year", "publisher"])
    p.add_argument("value", help=f"kind: {_listed(shipped_kinds())}, or one your rules add; ereader: always or never; "
                                 "format: epub, pdf, cbz or another marked keep in the rules (hylly rules show "
                                 "formats); file: its name as Humble lists it; answers: auto alone, which takes back "
                                 "your answers to its questions (hylly decide); left-out: auto alone, which takes back "
                                 "the files your shop list left out (a download fetches them again); copies: apart, a "
                                 "card's copies as two "
                                 f"books or more; refused: a lookup source ({', '.join(LOOKUPS)}) whose match is another "
                                 "book; title and the rest: the text (authors separated by ';', \"\" for none); any of "
                                 "them: auto")
    choosers(p)
    _both(p)

    p = add("decide", help="the questions the downloads' and the catalog's rules couldn't settle by themselves (which file to "
                           "keep, whether an item is a title, a file gone from its place, two copies one book, where a "
                           "title's tags went, a folder's files of an unknown kind): list them, or answer one; a lookup's "
                           "near matches with --kind match")
    p.add_argument("number", nargs="?", help="the question's number, as hylly decide lists it: alone, that one")
    p.add_argument("choice", nargs="?", help="the answer: an option's number, a file's name among them, keep for the "
                                              "rules' guess, or auto to take an answer back")
    p.add_argument("--answered", action="store_true", help="the questions answered instead, whose answers stand")
    p.add_argument("--kind", choices=("which-file", "title", "moved", "same", "match", "place", "files"),
                   help="only questions of this kind; match, a lookup's near matches, is listed only so")
    p.add_argument("--offered", metavar="OFFERED", help="what the question offered when the answer was chosen (its offered "
                                                        "in --json): refused, nothing changed, if it offers other options now")
    _json(p)
    _dry(p, "only say what the answer would do: offline, and writes nothing")
    _library(p)

    p = add("rules", help="what Hylly goes by when it guesses from names (titles, series, kinds, genres, games, formats): "
                          "alone, where the rules come from (your settings: hylly settings)")
    _rules(p)
    act = _actions(p, "rules", required=False)
    a = act("show", help="the rules in effect, each marked shipped or yours")
    a.add_argument("what", metavar="FILE_OR_TABLE", help="names, series, kinds, genres, games or formats, or a table, such as filename")
    _result(a)
    _rules(a, argparse.SUPPRESS)
    a = act("copy", help="a shipped file into your rules folder, to edit (never overwrites)")
    a.add_argument("what", metavar="FILE", help="names, series, kinds, genres, games or formats")
    _dry(a, "only say where it would go")
    a = act("try", help="what the rules make of a file's name")
    a.add_argument("what", metavar="NAME", help="a file's name, in quotes")
    a.add_argument("--bundle", help="the bundle it came in")
    a.add_argument("--publisher", help="its publisher")
    a.add_argument("--kind", help=_kinds_words("the kind, instead of the rules' guess"))
    a.add_argument("--mb", type=float, help="the file's size in MB")
    _result(a)
    _rules(a, argparse.SUPPRESS)

    p = add("verify", help="check downloaded and copied files are present with the right size")
    p.add_argument("--hash", action="store_true", help="also re-check every MD5 (reads each downloaded or copied file)")
    _json(p)
    _library(p)

    # ---- the library server
    p = add("serve", help="the library page, the reader and the library feeds over HTTP; one server at a time")
    _server_options(p)
    _browser(p)
    p.add_argument("--background", action="store_true", help="run hidden in the background, without a window, with the "
                                                             "settings as its options")
    p.add_argument("--service-file", action="store_true", help="Linux: print the system-wide service with walls around the "
                   "server (examples/hylly.service) for this computer, filled in from the settings, with the sudo commands "
                   "that install it; nothing is started or installed, and it writes nothing of its own (as every command, it makes "
                   "Hylly's own folders and secrets/ if they aren't there)")
    _dry(p, "only say what it would start, or that it runs already: nothing is started or stopped")

    p = add("server", help="the running library server: whether it runs, stop it, restart it, or start it at every logon; "
                           "and which computer keeps the library's records")
    act = _actions(p, "server")
    a = act("status", help="whether it runs, where and since when; exit code 1 when it doesn't run")
    _json(a)
    a = act("stop", help="stop the running server")
    _dry(a, "only say which server it would stop")
    a = act("restart", help="stop it and start it again in the background, with the settings as they are now")
    _browser(a)
    _dry(a, "only say what it would stop and start")
    a = act("autostart", help="start it at every logon, or no longer: on Windows set here; on Linux and macOS printed, for "
                              "the system to run it")
    turn = a.add_subparsers(dest="turn", required=True, title="on or off", parser_class=argparse.ArgumentParser)
    on = _maker(turn, "server autostart")("on", help="Windows: start it in the background at every logon, with the settings, "
                                                     "and now. Linux and macOS: print the systemd user unit or the launchd "
                                                     "agent that does, and how to install it; nothing is installed")
    _browser(on)
    _dry(on, "only say what would start at logon, and how")
    off = _maker(turn, "server autostart")("off", help="Windows: no longer start it at logon (a running server stays). Linux "
                                                       "and macOS: print how to stop it and remove its start; nothing is changed")
    _dry(off, "only say what would be removed")
    a = act("jobs", help='run what is due of its periodic jobs, once: for a timer to start every minute, when jobs = "external" '
                         "under [server] leaves them to it, and beside a server walled in, which can't see the Inbox: "
                         "then the Inbox alone")
    _dry(a, "only say which are due and would run")
    _result(a)
    _library(a)
    a = act("take-over", help="keep the library's own records (your tags and corrections, the catalog, what was downloaded) "
                              "in this computer's database from now on, as for a library moved here: every other computer's "
                              "database is refused them from then on. It says what it takes over, and asks first")
    _yes(a, "do not ask first: once the library's owner has decided, and stopped the server that kept them")
    _dry(a, "only say what it would take over")
    _library(a)

    p = add("user", help="users, their roles and passwords for the library server's login, which every request needs")
    act = _actions(p, "user")
    a = act("add", help="a new user, or a new password for one, which ends their logins: it says which before it asks for the "
                        "password, twice, unseen")
    a.add_argument("name", help="user name: letters, digits and . _ @ -")
    a.add_argument("--role", choices=["borrower", "reader", "librarian", "owner"], help="the new user's role (unless given, "
                   "the first user is the owner, and the rest readers); with an existing user, changes it too")
    a.add_argument("--password-stdin", action="store_true", help="read the password from standard input, one line from a "
                   "pipe or a file others can't read, as docker login does, instead of asking twice: never shown")
    _dry(a, "only say what it would do: no password is asked")
    a = act("role", help="change a user's role")
    a.add_argument("name", help="user name")
    a.add_argument("new_role", metavar="ROLE", choices=["borrower", "reader", "librarian", "owner"],
                   help="borrower (reads in the page only, gets no files or feeds), reader (reads and downloads), "
                        "librarian (changes one title at a time) or owner (anything, and the library management)")
    _yes(a, "do not ask first: once the library's owner has said yes")
    _dry(a, "only say what it would change")
    a = act("rename", help="a user's new name, with the same role and password, and all that's kept about them")
    a.add_argument("name", help="user name")
    a.add_argument("new", metavar="NEW", help="the new name")
    _dry(a, "only say what it would change")
    a = act("remove", help="remove a user: their logins end at once, and what's kept about them is kept aside")
    a.add_argument("name", help="user name")
    _yes(a, "do not ask first")
    _dry(a, "only say what it would remove")
    a = act("restore", help="a removed user back, with what was kept aside of them, while no user has the name: their "
                            "password and role went with them, so it asks for a new password, twice, unseen")
    a.add_argument("name", help="the removed user's name")
    a.add_argument("--role", choices=["borrower", "reader", "librarian", "owner"], help="their role (unless given, as a new "
                   "user's: a reader, or the owner when there are no users)")
    a.add_argument("--password-stdin", action="store_true", help="read the password from standard input, one line from a "
                   "pipe or a file others can't read, instead of asking twice: never shown")
    _dry(a, "only say what it would bring back: no password is asked")
    a = act("list", help="every user and their role")
    _json(a)
    a = act("link", help="a one-time link that logs a browser in as NAME, within 10 minutes: shown at a terminal")
    a.add_argument("name", help="user name")
    _yes(a, "into a pipe or a file too, which would keep it: once its owner has decided where the link goes")
    _dry(a, "only say what it would make: no link is made")

    p = add("people", help="what the server keeps about its users: where each has got to in their titles")
    act = _actions(p, "people")
    a = act("list", help="whom something is kept about, and how much; then what's kept aside of removed users, apart, with "
                         "when")
    _json(a)
    a = act("export", help="all that's kept about one, as JSON")
    a.add_argument("name", help="user name")
    a = act("forget", help="all their places, from here: they can't be brought back")
    a.add_argument("name", help="user name")
    _yes(a, "do not ask first: only once the person it's about has decided")
    _dry(a, "only count what would be forgotten")
    a = act("erase", help="what's kept aside of a removed user, erased for good: it says what goes, and asks first. Never "
                          "what's kept about a user of that name now")
    a.add_argument("name", help="the removed user's name")
    _yes(a, "do not ask first: only once the person it's about has decided")
    _dry(a, "only say what would go")

    p = add("reviews", help="stars and reviews: who has given how many, what waits, or a year's as Markdown")
    act = _actions(p, "reviews")
    a = act("list", help="who has given how many stars and reviews, and how many wait for an owner (alone: this)")
    _json(a)
    _both(a)
    a = act("export", help="a year's titles, the best first, with the words others may read, as Markdown")
    a.add_argument("--year", help="which year (default: this one)")
    a.add_argument("-o", "--out", help="a file to write, as UTF-8, written over if it's there (default: print it)")
    _dry(a, "only say what it would write, and where")
    _both(a)

    p = add("reports", help="reported titles: what waits for an owner or a librarian, what's marked for a better copy, "
                            "and what the shop's own record has of each")
    _json(p)
    _both(p)

    p = add("theme", help="the library page's themes: list them, or check one of your own before copying it in")
    act = _actions(p, "theme")
    a = act("list", help="every theme there is, which may be picked and which is the library's own (alone: this)")
    _json(a)
    a = act("check", help="whether a theme file can be used, and its contrast")
    a.add_argument("file", help="the theme file")
    _result(a)

    p = add("bell", help="alarms about the library server: where they go, the latest ones, or a test ([bell] in settings)")
    act = _actions(p, "bell")
    a = act("show", help="where the alarms go, and the latest ones (alone: this); of the ntfy topic, only its end")
    _json(a, "as JSON, with the topic masked")
    a = act("test", help="send a test alarm everywhere")
    _dry(a, "only say where it would go")

    # ---- copies and backups
    p = add("copy", help="HyllyCopy: a copy you can leave running, of terabytes or of tens of thousands of small files. "
                         "It only reads the source, overwrites nothing, checks every copy, and waits out disks and shares "
                         "that go away. hylly copy check TARGET reads the copies again")
    p.add_argument("source", help="the folder to copy from: only read")
    p.add_argument("target", help="the folder to copy into: made if it isn't there yet")
    p.add_argument("--everything", action="store_true", help="also the system's own files and folders, which are otherwise "
                   "left out and listed in the report: .DS_Store, ._ files, Thumbs.db, $RECYCLE.BIN and the like")
    p.add_argument("--new-disk", action="store_true", help="the source or the target really is another disk now, or a folder "
                   "you emptied. Without this, a target whose mark isn't the one it had, or a source that doesn't look like "
                   "the one copied from before, is refused, as its disk may just not be mounted")
    p.add_argument("--progress", type=how_often, metavar="EVERY", help="a note on how far it has come this often, such as 6h or 30m")
    _yes(p, "don't ask first about the first copy to a target, the first there from another source, or one that would write "
            "more than all copied there before")
    _dry(p, "only show the plan: what it would copy, the room on the target, and how long it may take")
    _result(p)
    p.set_defaults(action=None)
    p = add("copy check", about="read every copy on a target again, and compare it with the checksum made when it was "
                               "copied: only reads, and can be run any time later")
    p.add_argument("target", help="the folder copied into")
    p.add_argument("--progress", type=how_often, metavar="EVERY", help="a note on how far it has come this often, such as 6h or 30m")
    _dry(p, "only say how much it would read again")
    _result(p)
    p.set_defaults(command="copy", action="check")

    p = add("backup", help="a second copy of the library on another disk, that you can leave running: the books as plain "
                           "files, each one checked, no book deleted, and a zip of your edits and records with each "
                           "backup, the older ones thinned only as keep under [backup] in the settings says. hylly backup "
                           "check TARGET reads it again, and hylly restore gets it back")
    p.add_argument("target", nargs="?", help="the folder on the other disk: made if it isn't there yet. Default: target "
                   "under [backup] in the settings")
    p.add_argument("--new-disk", action="store_true", help="the library or the backup's disk really is another one now. "
                   "Without it, either looking different is refused, as its disk may just not be mounted")
    p.add_argument("--progress", type=how_often, metavar="EVERY", help="a note on how far it has come this often, such as 6h or 30m")
    _yes(p, "don't ask first about the first backup to a target, the first there of another library, or one that would write "
            "more than all backed up there before")
    _dry(p, "only show the plan: what it would copy and zip, the room on the disk, how long it may take, and which older "
            "zips keep would take away")
    _result(p)
    _library(p)
    p.set_defaults(action=None)
    p = add("backup check", about="read every copy in a backup again, and compare it with the checksum made when it was "
                                 "copied, and read back the newest zips: only reads")
    p.add_argument("target", nargs="?", help="the backup's folder. Default: target under [backup] in the settings")
    p.add_argument("--all", action="store_true", help="read back every zip in saved, not only the newest three")
    p.add_argument("--progress", type=how_often, metavar="EVERY", help="a note on how far it has come this often, such as 6h or 30m")
    _dry(p, "only say how much it would read again")
    _result(p)
    p.set_defaults(command="backup", action="check")

    p = add("restore", help="get a backup back: the books copied to a new folder, the newest zip unpacked there, and the "
                            "library's records made this computer's database if it has none. Nothing is written over")
    p.add_argument("target", metavar="BACKUP", help="the backup's folder")
    p.add_argument("to", metavar="NEW_FOLDER", help="the folder for the library: made if it isn't there yet")
    p.add_argument("--saved", metavar="ZIP", help="an older zip, by its name or the start of it, such as 2026-11-06")
    p.add_argument("--new-disk", action="store_true", help="the backup's disk really is another one now")
    p.add_argument("--progress", type=how_often, metavar="EVERY", help="a note on how far it has come this often, such as 6h or 30m")
    _dry(p, "only show the plan: what it would copy and unpack, and where")
    _result(p)

    # ---- looking on, and setting up
    p = add("init", help="make a folder a Hylly library: everything that writes to the library refuses a folder without "
                         "this, as it may be an unmounted disk. An empty or new folder is asked about first")
    p.add_argument("folder", nargs="?", help="default: the library in the settings, or --library")
    _yes(p, "for an empty or new folder: don't ask first. Use it once you have checked that it's the folder for a new "
            "library, not where an unmounted disk should be")
    p.add_argument("--new", action="store_true", help="an empty folder that this computer's database knows as the library, "
                   "with titles, is to be a new library: that one is gone for good, not just unmounted")
    _dry(p, "only say what it would mark")
    _library(p)

    p = add("settings", help="this computer's settings.toml: every setting with its value and where it comes from, the "
                             "file's path, where Hylly keeps its own files, a check of it, or the file in your editor")
    act = _actions(p, "settings")
    a = act("show", help="every setting with its value, and where it comes from: settings.toml, the environment or the "
                         "default, and where the alarms' secrets are; their values, and whatever is left out, not shown")
    _json(a)
    a = act("path", help="the settings file's path, there or not yet")
    _json(a)
    a = act("places", help="where Hylly keeps its own files on this computer, by kind: the settings, the database, the "
                           "logs and locks, the reader's cache and the logins, each folder with what chose it")
    _json(a)
    a = act("check", help="read and check the file: every mistake in it listed, exit code 5 if there's one")
    _json(a)
    a = act("edit", help="the file in your editor: VISUAL, else EDITOR, else the system's own; made empty first if it isn't "
                         "there yet")
    _dry(a, "only say which file it would open, and with what")

    p = add("jobs", help="the jobs running on this computer, how far they have come, and how each one's last run ended; "
                         "or end one that has hung")
    act = _actions(p, "jobs")
    a = act("list", help="the jobs running, and how each one's last run ended (alone: this)")
    _json(a)
    a = act("stop", help="end a job running on this computer, if it has hung: what it has done is kept")
    a.add_argument("job", metavar="NAME", choices=list(JOBS), help="which: " + ", ".join(JOBS))
    _dry(a, "only say what it would end")

    p = add("logs", help="what the logs are and where they are, or the end of one")
    p.add_argument("name", nargs="?", choices=["download", "lookup", "inbox", "server", "bell", "access"],
                   help="the log to show the end of")
    p.add_argument("--tail", type=count, default=40, help="how many of its last lines (default 40)")
    _library(p)

    p = add("doctor", help="what shape this Hylly is in, with the next step for whatever is wrong: it only looks, offline, "
                           "and shows no logins or keys")
    _json(p)
    _both(p)

    p = add("commands", help="every command, what it may do (sites, writes, your account at a shop, what needs a person), "
                             "the exit codes and the environment variables")
    _json(p)
    p.add_argument("--internal", action="store_true", help="also the calls between Hylly's own processes (hylly internal)")

    p = add("demo", help="a small library of made-up books to try things on, apart from your own: its own folder, settings, "
                         "server and owner (demo), and at a terminal it opens this computer's browser logged in as demo; no "
                         "books or shop logins needed")
    p.add_argument("folder", nargs="?", help="an empty or new folder for it (default: hylly-demo in your home folder)")
    p.add_argument("--port", type=port, help="the port for its server (default 8779)")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--no-serve", action="store_true", help="only make it, and say what to set for other commands on it; "
                     "running it again serves it")
    how.add_argument("--background", action="store_true", help="serve it in the background, with no browser, and end: it "
                     "says the address and how to stop it, for a script or an AI assistant that mustn't wait on a server")
    how.add_argument("--stop", action="store_true", help="stop the demo's server running in the background: the one of "
                     "this demo's own folder, and no other")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    _dry(p, "only say where it would make it, and the port it would serve it on; with --stop, which server it would stop")

    # ---- between Hylly's own processes: no help line, left out of the list and of hylly commands (but with --internal)
    p = add("internal", about="calls between Hylly's own processes, not for people: another computer's backup asking "
                              "the server, a long job passed on from another computer, a long one of the server's "
                              "periodic jobs, and the Inbox's pass")
    act = _actions(p, "internal")
    a = act("backup-noted", help="on the server: another computer's note of a backup it made, as JSON on stdin, kept in "
                                 "the library's records")
    a.add_argument("target", help="the backup's folder, as that computer names it")
    _dry(a, "only check the note")
    _library(a)
    a = act("fresh-copy", help="on the server: today's copy of the database made again now, and of the settings, for a "
                               "backup another computer is making")
    _dry(a, "only say what it would make")
    _library(a)
    a = act("job", help="on the server, from another computer: a long job started on its own, so that closing the window "
                        "there doesn't stop it; followed until it ends")
    a.add_argument("words", nargs=argparse.REMAINDER, metavar="COMMAND", help="the command, as hylly takes it")
    _dry(a, "only say what it would start")
    a = act("server-job", help="from hylly server jobs: a long one of the server's periodic jobs (the nightly copy, the "
                               "Inbox), run on its own, so that the next minute's alarms needn't wait for it")
    a.add_argument("name", metavar="NAME", help="the job, by its name: nightly copy, inbox")
    _dry(a, "only say what it would run")
    _library(a)
    a = act("inbox", help="from the server's job: one pass over the Inboxes ([inbox] in the settings), which takes "
                          "their books into the library, checked, looked up and catalogued, and only then removes them "
                          "from there")
    _dry(a, "only say what a pass would take, set aside and remove")
    _library(a)

    order = [name for names in AREAS.values() for name in names]
    sub._choices_actions.sort(key=lambda a: order.index(a.dest))
    return parser


def daemon_parser() -> argparse.ArgumentParser:
    """hyllyd's options: what starting the library server needs, and no more (daemon.py)."""
    p = argparse.ArgumentParser(prog="hyllyd", allow_abbrev=False, formatter_class=Lines,
                                description="hyllyd: Hylly's library server itself, which serves until it's stopped, and "
                                            "never opens a browser. hylly serve starts it for you, and hylly server status, "
                                            "stop and restart look after it; a service starts it as hyllyd --in-background, "
                                            "with the settings as its options.")
    p.add_argument("--version", action="version", version=f"hyllyd {version()}", help="which version of Hylly this is")
    _server_options(p)
    p.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # what services an older Hylly set up pass
    p.add_argument("--in-background", action="store_true", help="as the server nobody watches, for a service or hylly serve "
                   "--background: the settings as its options, one line when it starts, and none started if one runs "
                   "already; its messages in server.log and on standard error, as ever")
    _dry(p, "only say what it would serve, and where: nothing is started")
    return p


def epilog() -> str:
    """hylly --help's end: the commands by what they're for, the rule of the dry run, what asks first, and where to look."""
    areas = "\n".join(f"  {area + ':':<22}{', '.join(names)}" for area, names in AREAS.items())
    yes = [name for name in asks_first() if "--yes" in _person(name)]
    text = (f"The commands, by what they're for:\n{areas}\n\n"
            "To start: hylly demo to try it; then hylly init FOLDER, hylly login SITE, hylly download, hylly catalog, hylly serve.\n\n"
            "Every command that changes something does it at once, and with --dry-run (-n) only says what it would do. These "
            f"ask first, and take --yes (-y) once their owner, or the person it's about, has said yes: {', '.join(yes)}. These "
            f"need a person at the keyboard: {', '.join(name for name in asks_first() if name not in yes)}.\n\n"
            "-H HOST and --here go before the command; --library FOLDER before it or after. With --json, standard output "
            "has only the result, and what a command says on the way goes to standard error. hylly doctor says what shape "
            "it is in; hylly settings show lists the settings it goes by; hylly commands lists every command with what it may do, the "
            f"exit codes and the environment variables.\n\n{ASSISTANTS}")
    return "\n".join(line if line.startswith("  ") else textwrap.fill(line, 120, break_long_words=False, break_on_hyphens=False)
                     for line in text.splitlines())


def leaves(parser: argparse.ArgumentParser | None = None, *, internal: bool = False) -> list[tuple[str, argparse.ArgumentParser, str]]:
    """Every command as it's run, (its name, such as "humble download", its parser, its line), in the order of AREAS: a
    command with actions is each of them, and one that also runs alone (rules) is itself too. internal: hylly internal's
    too, last."""
    top = next(a for a in (parser or build_parser())._actions if isinstance(a, argparse._SubParsersAction))
    lines = {a.dest: a.help or "" for a in top._choices_actions}
    out = []

    def walk(name: str, p: argparse.ArgumentParser, line: str) -> None:
        found = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        if found is None or not found.required:
            out.append((name, p, line))
        if found is not None:
            words = {a.dest: a.help or "" for a in found._choices_actions}
            for word, q in found.choices.items():
                walk(f"{name} {word}", q, words.get(word, ""))

    for name in [n for names in AREAS.values() for n in names] + (["internal"] if internal else []):
        walk(name, top.choices[name], lines.get(name, top.choices[name].description or ""))
        if name in CHECKS:
            walk(f"{name} check", top.choices[f"{name} check"], top.choices[f"{name} check"].description.rstrip("."))
    return out


def options_of(p: argparse.ArgumentParser) -> list[argparse.Action]:
    """A command's own options and words, as its --help shows them: not its actions, nor what's hidden."""
    return [a for a in p._actions if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))
            and a.help != argparse.SUPPRESS]


def taking(option: str, parser: argparse.ArgumentParser | None = None) -> list[str]:
    """The commands that take an option, such as --json, by name: for the help page's lists (helppage.command_lists),
    which are made from this. parser: one already built, when several lists are made at once."""
    return [name for name, p, _ in leaves(parser) if any(option in a.option_strings for a in options_of(p))]


def one_object(parser: argparse.ArgumentParser | None = None) -> list[str]:
    """The commands whose --json is one object however they end (_result, cli.ONE_OBJECT), by name: the help's list of
    them is made from this (helppage.command_lists), and a test holds it to cli.ONE_OBJECT."""
    return [name for name, p, _ in leaves(parser) if any("--json" in a.option_strings and a.help == RESULT for a in options_of(p))]


def asks_first() -> list[str]:
    """The commands that ask a person first, or need one (effects.py's person), by name: the help's lists of them are
    made from this (helppage.command_lists), and a test holds the playbooks' to it. From the table, which has a row for
    every command as leaves names them (a test holds them alike), as hylly --help's own end names them too."""
    from .effects import EFFECTS

    return [name for name, effect in EFFECTS.items() if effect["person"] and not name.startswith("internal ")]


def _option_row(a: argparse.Action) -> dict:
    """An option, or a word a command takes, as its --help names it."""
    return {"option": ", ".join(a.option_strings) or a.metavar or a.dest, "help": a.help or "",
            **({"choices": list(a.choices)} if a.choices else {})}


def commands_data(internal: bool = False) -> dict:
    """Every command, what it may do, where it runs, how they end and what they read from the environment: hylly commands.
    internal: also hylly internal's calls between Hylly's own processes."""
    from .effects import ALSO, EFFECTS, ENVIRONMENT, REMOTE, SECRET
    from .remote import LOCAL, LOCAL_JOBS

    parser = build_parser()
    out = []
    there = "on the server computer when -H, HYLLY_REMOTE or [remote] ssh names one"
    for name, p, line in leaves(parser, internal=internal):
        runs = ("here" if name.split(" ")[0] in LOCAL else
                f"{there}; the stop of {' or '.join(LOCAL_JOBS)} here, where they run" if name == "jobs stop" else there)
        out.append({"command": f"hylly {name}", "about": line, **EFFECTS[name], "runs": runs,
                    "options": [_option_row(a) for a in options_of(p)]})
    top = [_option_row(a) for a in options_of(parser) if a.dest != "command"]
    daemon = daemon_parser()
    return {"version": version(), "options": top, "remote": REMOTE, "exit_codes": {str(k): v for k, v in TABLE.items()},
            "environment": ENVIRONMENT, "secret": SECRET, "also": ALSO, "assistants": ASSISTANTS, "commands": out,
            "hyllyd": {"about": daemon.description, "options": [_option_row(a) for a in options_of(daemon)]}}


def reference_html() -> str:
    """Every command with its options, as HTML for the help page; made from the parser, so it can't fall behind."""
    import html

    def usage(a: argparse.Action) -> str:
        if not a.option_strings:  # a positional: its choices, or its name
            return " | ".join(a.choices) if a.choices else (a.metavar or a.dest).upper()
        if a.nargs == 0:
            return ", ".join(a.option_strings)
        value = a.metavar or ("{" + ",".join(a.choices) + "}" if a.choices else a.dest.upper())
        return f"{', '.join(a.option_strings)} {value}" + (" …" if a.nargs in ("+", "*") else "")

    def table(rows: list[tuple[str, str]]) -> str:
        return ('<table class="opts">' + "".join(f"<tr><td><code>{html.escape(u)}</code></td><td>{html.escape(h)}</td></tr>"
                                                 for u, h in rows) + "</table>") if rows else ""

    parser = build_parser()
    found = {name: (p, line) for name, p, line in leaves(parser)}
    lines = {a.dest: a.help or "" for a in next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))._choices_actions}
    out = ['<div class="cmd" id="cmd-hylly"><h3><code>hylly</code></h3><p>Options that go before the command.</p>'
           + table([(usage(a), a.help or "") for a in options_of(parser) if a.dest != "command"]) + "</div>"]
    for area, names in AREAS.items():
        out.append(f'<div class="prog" id="area-{re.sub(r"[^a-z]+", "-", area.lower()).strip("-")}"><h3>{html.escape(area)}</h3></div>')
        for top in names:
            if top not in found:  # a command of actions only: its own line first, then each action's
                out.append(f'<div class="cmd" id="cmd-{top}"><h3><code>hylly {top}</code></h3><p>{html.escape(lines[top])}</p></div>')
            for name, (p, line) in found.items():
                if name.split(" ")[0] != top:
                    continue
                rows = [(usage(a), SHORT.get(a.dest, a.help or "")) for a in options_of(p)]
                out.append(f'<div class="cmd" id="cmd-{name.replace(" ", "-")}"><h3><code>hylly {html.escape(name)}</code></h3>'
                           f"<p>{html.escape(line)}</p>{table(rows)}</div>")
    daemon = daemon_parser()
    out.append(f'<div class="cmd" id="cmd-hyllyd"><h3><code>hyllyd</code></h3><p>{html.escape(daemon.description)}</p>'
               + table([(usage(a), a.help or "") for a in options_of(daemon)]) + "</div>")
    from .effects import ENVIRONMENT

    def listing(heading: str, anchor: str, rows: dict) -> str:
        return (f'<div class="cmd" id="{anchor}"><h3>{heading}</h3><table class="opts">'
                + "".join(f"<tr><td><code>{html.escape(str(k))}</code></td><td>{html.escape(v)}</td></tr>" for k, v in rows.items()) + "</table></div>")

    out += [listing("Library and rules", "common-options", {"--library FOLDER": LIBRARY, "--rules FILE_OR_FOLDER": RULES}),
            listing("Commands that ask first", "asks-first", {f"hylly {name}": _person(name) for name in asks_first()}),
            listing("How a command ends: its exit code", "exit-codes", TABLE), listing("Environment variables", "environment", ENVIRONMENT)]
    return "\n".join(out)


def _person(name: str) -> str:
    from .effects import EFFECTS

    return EFFECTS[name]["person"]
