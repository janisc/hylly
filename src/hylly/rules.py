"""What Hylly knows about books and their files: the rules files (docs/rules.md).

The shipped ones are in data/ beside this file: names, series, kinds, genres, games and formats. Your own .toml
files change them: those in your rules folder (rules\\ next to settings.toml, or rules = in settings.toml, or
HYLLY_RULES), in name order, then each --rules file or folder. Any file may hold any table, and four rules say how
it changes what came before:
  1. tables merge by name: a genre, game, kind or format you name changes that one (a genre's or a game's name with
     case not counting, its first spelling kept), the others stay;
  2. a value replaces the one before, lists included;
  3. <key>_add adds to a list and <key>_drop takes out of it;
  4. off = true takes an entry away, and inherit = false at the top of a table starts it empty.

current() is the result, made into patterns once in a command; a running server reads it again when a file changes.
A mistake stops with the file, the table and the entry it is in. Your choices (which format to keep, what makes a
title a tablet's) are settings instead: settings.py. The kinds a title may be are the rules' too, kinds.toml's [kinds.X]
(Kind): what each is called, where it is best read, and the words that find it (Rules.kind_of).
"""

from __future__ import annotations

import functools
import math
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .exits import NOT_SET_UP, Exit
from . import paths
from .words import MB, many

DATA = Path(__file__).with_name("data")
SHIPPED = ("names", "series", "kinds", "genres", "games", "formats")
LAYOUTS = ("reflowable", "fixed", "comic")
DEVICES = ("ereader", "tablet", "either")  # where a kind is best read ([kinds.X] device)
DEFAULT_KIND = "book"  # what a title is when nothing says otherwise
# A kind's words, what inherit = false and off = true act on in [kinds] and [kinds.X], as they did before the kinds
# were a table: a kind itself, and what else its entry says, can be added and changed but never taken away (merge)
KIND_WORDS = ("bundle_words", "publishers")
EXTRA: list[str] = []  # --rules on the command line: files or folders read last, in this order
WATCH = False  # set by serve.run: a running server looks at the files again; a command keeps the rules it read first
CHECK_EVERY = 2.0  # seconds between a server's looks at whether a rules file changed


class RulesError(Exit):
    """A mistake in a rules file: the message names the file, the table and the entry."""

    def __init__(self, message: str) -> None:
        super().__init__(NOT_SET_UP, message)


class Missing(RulesError):
    """Your rules folder or file, or a --rules one, isn't there: written wrong, or on a share that isn't up yet."""


# ---- what each table may hold ---------------------------------------------------------------

WORDS, PATTERNS, PATTERN, LIST, BOOL, NUMBER, TEXT = "words", "patterns", "pattern", "list", "true or false", "number", "text"
LISTS = (WORDS, PATTERNS, LIST)


@dataclass(frozen=True)
class Spec:
    keys: dict = field(default_factory=dict)  # the table's own keys and the kind of value each holds
    entry: dict | None = None  # a table of entries: the keys each entry may have
    names: tuple = ()  # the entries it may have; () for any
    other: str | None = None  # any other key holds this kind of value (the languages)


SPECS = {
    "filename": Spec({"strip": LIST, "strip_patterns": PATTERNS, "number_pattern": PATTERN, "dots_as_spaces": BOOL}),
    "junk": Spec({"titles": WORDS, "title_patterns": PATTERNS, "title_extensions": LIST, "authors": WORDS,
                  "author_patterns": PATTERNS}),
    "names": Spec({"name_particles": LIST, "particles": LIST, "not_name_words": LIST, "suffixes": LIST,
                   "company_words": LIST, "author_separators": LIST}),
    "series": Spec({"words": WORDS, "bracket_words": WORDS, "part_words": WORDS, "fold_words": WORDS,
                    "collected_words": WORDS, "comic_notes": WORDS, "comic_noise": WORDS}),
    "kinds": Spec({"prose_max_mb": NUMBER}, entry={"label": TEXT, "plural": TEXT, "device": TEXT, "why_device": TEXT,
                                                    "bundle_words": WORDS, "publishers": LIST, "prose": BOOL,
                                                    "layout": TEXT, "folders": LIST, "itchio": LIST,
                                                    "joins_books": BOOL, "text_first": BOOL, "folder_named": BOOL}),
    "labels": Spec({"extra": WORDS, "audio": WORDS, "video": WORDS}),
    "languages": Spec(other=WORDS),
    "genres": Spec({"titles_unread": LIST}, entry={"words": WORDS, "patterns": PATTERNS}),
    "kind_genres": Spec(entry={"genre": TEXT, "unless": LIST, "only": BOOL}),  # each of a kind there is: Rules checks
    "games": Spec(entry={"words": WORDS, "patterns": PATTERNS, "ordinary": BOOL}),
    "formats": Spec(entry={"layout": TEXT, "shop": BOOL, "book": BOOL, "keep": BOOL, "convert": TEXT}),
    "packages": Spec({"books": LIST, "ignore": LIST}),
    "media": Spec({"audio": LIST, "video": LIST, "programs": LIST}),
    "itchio": Spec({"fetch": LIST}),
}
FILE_OF = {"filename": "names", "junk": "names", "names": "names", "series": "series", "kinds": "kinds",
           "labels": "kinds", "languages": "kinds", "genres": "genres", "kind_genres": "genres", "games": "games",
           "formats": "formats", "packages": "formats", "media": "formats", "itchio": "formats"}
# Tables no longer read, with what took their place: one that holds nothing is let by, as a copy of an older shipped file
# has it (hylly rules copy), its rules commented out
RETIRED = {"main_file": "a title's main file is the first of its kind's formats under [download] in settings.toml "
                        "(ereader, tablet, or the kind's own list), as its download is"}


def bare(name: str) -> str:
    """A table's or entry's name as TOML writes it: quoted when it isn't a plain word."""
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def place(where: tuple) -> str:
    """A table, or an entry of one, as TOML writes it: [genres."Railway Stories"]."""
    return "[" + ".".join((where[0], *map(bare, where[1:]))) + "]"


def _check(path: Path, data: dict) -> None:
    for table, value in data.items():
        spec = SPECS.get(table)
        if spec is None and table in RETIRED and isinstance(value, dict) and _hollow(value):
            continue
        if spec is None and table in RETIRED:
            raise RulesError(f"{path}: [{table}] is no longer read: {RETIRED[table]}. Take it out of this file.")
        if spec is None:
            raise RulesError(f"{path}: [{table}] isn't a table of rules. They are: {', '.join(SPECS)}.")
        if not isinstance(value, dict):
            raise RulesError(f"{path}: {table} is a table: write it as [{table}].")
        _check_table(path, f"[{table}]", table, value, spec.keys, spec.other, spec)


def _check_table(path: Path, where: str, table: str, value: dict, keys: dict, other: str | None, spec: Spec | None) -> None:
    for key, v in value.items():
        if key in ("off", "inherit"):
            if not isinstance(v, bool):
                raise RulesError(f"{path}, {where} {key}: true or false.")
            if key == "off" and v and spec is not None:  # a whole table: off is for one entry
                raise RulesError(f"{path}, {where} off = true: use inherit = false to start [{table}] empty.")
            continue
        if spec is not None and spec.entry is not None and isinstance(v, dict):
            if spec.names and key not in spec.names:
                raise RulesError(f"{path}, [{table}.{bare(key)}]: {table} can only be {', '.join(spec.names)}.")
            if table == "formats" and not re.fullmatch(r"[a-z0-9]+", key):
                raise RulesError(f"{path}, [formats.{bare(key)}]: a format is its extension, in small letters: "
                                 "[formats.epub].")
            if table in ("kinds", "kind_genres") and not re.fullmatch(r"[a-z][a-z0-9_]*", key):  # a word in commands,
                raise RulesError(f"{path}, [{table}.{bare(key)}]: a kind is one word in small letters, "  # the settings
                                 "such as [kinds.zine].")  # and addresses (/opds/kind/zine)
            if table == "kinds":  # a kind has a list under [download] by its name: never one of that section's own
                from .settings import DOWNLOAD_OWN

                if key in DOWNLOAD_OWN:
                    raise RulesError(f"{path}, [kinds.{key}]: {key} is one of [download]'s own settings in "
                                     f"settings.toml ({', '.join(DOWNLOAD_OWN)}), so no kind may have that name.")
            _check_table(path, f"[{table}.{bare(key)}]", table, v, spec.entry, None, None)
            continue
        base = key if key in keys else re.sub(r"_(add|drop)$", "", key)
        kind = keys.get(base) or other
        if kind is None or (base != key and kind not in LISTS):
            raise RulesError(f"{path}, {where}: {key} isn't one of its keys"
                             + (f" ({', '.join(keys)})." if keys else "."))
        _check_value(path, where, key, kind, v)
        if table == "formats" and base == "layout" and v not in (*LAYOUTS, ""):
            raise RulesError(f"{path}, {where} layout = {v!r}: use {', '.join(LAYOUTS)}.")
        if table == "kinds" and base == "device" and v not in DEVICES:
            raise RulesError(f"{path}, {where} device = {v!r}: use {', '.join(DEVICES)}.")
        if table == "kinds" and base == "layout" and v not in (*LAYOUTS, ""):
            raise RulesError(f"{path}, {where} layout = {v!r}: use {', '.join(LAYOUTS)}.")
        if table == "formats" and base == "convert" and v and not re.fullmatch(r"[a-z0-9]+", v):
            raise RulesError(f"{path}, {where} convert = {v!r}: a format's extension, such as \"epub\".")
        if table == "itchio" and base == "fetch":  # a kind is an extension: "*" or "mp3 files" would take nothing
            for x in v:
                if not re.fullmatch(r"\.?[A-Za-z0-9_]+", x.strip()):
                    raise RulesError(f"{path}, {where} {key}: {x!r} isn't a kind of file. A kind is its extension, "
                                     "such as \"mp3\".")


def _check_value(path: Path, where: str, key: str, kind: str, v) -> None:
    say = f"{path}, {where} {key}"
    if kind in LISTS:
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise RulesError(f"{say}: a list of words in quotes, such as [\"one\", \"two words\"].")
        if kind == WORDS:
            for x in v:
                if not _folded(x).replace("*", "").split():  # "*" alone would find every title; "´" folds to nothing
                    raise RulesError(f"{say}: {x!r} has no word in it. A * goes at the end of a word: \"knit*\".")
        if kind == PATTERNS:
            for p in v:
                _compiles(say, p, joined([p]))
            try:
                re.compile(joined(v), re.I)
            except re.error as e:
                raise RulesError(f"{say}: {_together(e)}") from None
    elif kind == PATTERN:
        if not isinstance(v, str):
            raise RulesError(f"{say}: a pattern in single quotes, such as '^\\d+ '.")
        if v:
            _compiles(say, v, v, flags=0)  # as it is used: alone, and case counting
    elif kind == BOOL and not isinstance(v, bool):
        raise RulesError(f"{say}: true or false.")
    elif kind == NUMBER and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0):
        raise RulesError(f"{say}: a number, such as 8.")
    elif kind == TEXT and not isinstance(v, str):
        raise RulesError(f"{say}: a text in quotes.")


def joined(patterns: list[str]) -> str:
    """A list of patterns as one, the way Hylly uses them: case never counts there, so a (?i) in front is left out."""
    return "|".join(f"(?:{p.removeprefix('(?i)')})" for p in patterns)


def _together(e: re.error) -> str:
    return (f"each pattern can be read alone, but not all of them joined into one, as Hylly uses them ({e}). A group's "
            "name, (?P<name>...), can be used once.")


def _compiles(say: str, pattern: str, used: str, flags: int = re.I) -> None:
    try:
        re.compile(used, flags)
    except re.error as e:
        raise RulesError(f"{say}: {pattern!r} isn't a pattern Hylly can read ({e}). Patterns are regular "
                         "expressions, written in single quotes: '...'.") from None


# ---- reading and merging the files -----------------------------------------------------------

def folder() -> Path:
    """Your rules folder: rules = in settings.toml or HYLLY_RULES (a folder, or one file), else rules\\ next to
    settings.toml."""
    given = paths.setting("rules", "HYLLY_RULES")
    return Path(given) if given else paths.settings_file().parent / "rules"


def _tomls(where: Path) -> list[Path]:
    """A folder's .toml files in name order."""
    return sorted((p for p in where.glob("*.toml") if p.is_file()), key=lambda p: p.name.lower())


def sources() -> list[Path]:
    """Every rules file, in the order read: the shipped ones, your folder's, then --rules."""
    files = [DATA / f"{name}.toml" for name in SHIPPED]
    mine = folder()
    if mine.is_dir():
        files += _tomls(mine)
    elif paths.setting("rules", "HYLLY_RULES"):
        if not mine.is_file():
            raise Missing(f"rules = {mine} (in {paths.settings_file()}, or HYLLY_RULES): there is no such file or "
                          "folder.")
        files.append(mine)  # one file, as --rules may be
    for given in EXTRA:
        p = Path(given)
        if p.is_dir():
            files += _tomls(p)
        elif p.is_file():
            files.append(p)
        else:
            raise Missing(f"--rules {given}: there is no such file or folder.")
    return files


# What tomllib says of a backslash in double quotes, where a pattern's \d is read as an escape: one it doesn't know,
# or a \u or \U without its digits. Only then is the hint about patterns any help.
BACKSLASH = ("'\\'", "hex value", "Unicode scalar value")


def read_file(path: Path) -> dict:
    try:
        data = paths.read_toml(path)  # a BOM at its start, as Notepad may write, is fine
    except paths.NotToml as e:
        hint = e.kind == "syntax" and any(said in e.detail for said in BACKSLASH)
        raise RulesError(e.said() + (" Patterns go in single quotes: '...'." if hint else "")) from None
    except OSError as e:
        raise RulesError(f"{path}: can't be read ({e.strerror or e}).") from None
    _check(path, data)
    return data


def _fold_key(x):
    return x.casefold() if isinstance(x, str) else x


def _kind(where: tuple, key: str) -> str | None:
    """What a key holds in the table or entry at where (SPECS)."""
    spec = SPECS.get(where[0]) if where else None
    if spec is None:
        return None
    return (spec.keys.get(key) or spec.other) if len(where) == 1 else (spec.entry or {}).get(key)


def _order(item: tuple) -> int:
    """In one table a value comes first, then its _drop, then its _add, whatever their order in the file."""
    key, value = item
    m = re.fullmatch(r"(.+)_(add|drop)", key) if isinstance(value, list) else None
    return 0 if m is None else 1 if m[2] == "drop" else 2


def _hollow(table: dict) -> bool:
    """Nothing in it, or only tables with nothing in them."""
    return all(isinstance(v, dict) and _hollow(v) for v in table.values())


NOTED = ("nothing", "new:")  # what merge notes beside the changes, for hylly rules: what isn't there to take away, and
# an entry an _add or _drop made, as none had its name


def merge(base: dict, mine: dict, name: str, where: tuple = (), origin: dict | None = None) -> dict:
    """mine over base by the four rules; origin notes which entries each file added, changed or took away, and what
    it would take away that isn't there."""
    origin = {} if origin is None else origin

    def note(at: tuple, how: str) -> None:
        notes = origin.setdefault(at, [])
        if (name, how) not in notes:
            notes.append((name, how))

    if mine.get("inherit") is False:
        out: dict = _words_away(base, where)
        note(where, "emptied")
    else:
        out = dict(base)
    for key, value in sorted(mine.items(), key=_order):
        if key in ("inherit", "off"):
            continue
        m = re.fullmatch(r"(.+)_(add|drop)", key)
        if m and isinstance(value, list):
            same = (lambda x: x) if _kind(where, m[1]) == PATTERNS else _fold_key  # in a pattern, case can count
            have = list(out.get(m[1]) or [])
            seen = {same(x) for x in have}
            if m[2] == "add":
                for x in value:
                    if same(x) not in seen:
                        have.append(x)
                        seen.add(same(x))
            else:
                for x in value:
                    if same(x) not in seen:
                        note(where, f"nothing: {key} = [{toml_value(x)}]")
                gone = {same(x) for x in value}
                have = [x for x in have if same(x) not in gone]
            out[m[1]] = have
        elif isinstance(value, dict):
            if len(where) == 1:  # an entry: by its name as the rules before had it, case not counting
                key = _named(out, key)
            had = isinstance(out.get(key), dict)
            if value.get("off") is True:
                if had and where == ("kinds",):  # a kind stays: off takes its words away, all an entry held before
                    out[key] = _words_away(out[key], where + (key,))
                    note(where + (key,), "off")
                elif had:
                    del out[key]
                    note(where + (key,), "off")
                else:  # a name written wrong, or an entry a later version no longer ships
                    note(where + (key,), "nothing: off = true")
                continue
            if _hollow(value):  # a table with nothing in it changes nothing: a copied file's, its rules commented out
                continue
            out[key] = merge(out[key] if had else {}, value, name, where + (key,), origin)
            note(where + (key,), "changed" if had else "added")  # a table, or an entry of one
            changes = [k for k, v in value.items() if isinstance(v, list) and re.fullmatch(r".+_(add|drop)", k)]
            if not had and changes:  # meant to change one, and none had that name: a new one, which hylly rules says
                note(where + (key,), f"new: {', '.join(changes)}")
        else:
            out[key] = value
    return out


def _named(table: dict, name: str) -> str:
    """An entry's name as the table has it already, case not counting: [genres."travel"] changes the shipped Travel,
    which keeps its spelling; name itself for one the table hasn't."""
    if name in table:
        return name
    folded = name.casefold()
    return next((k for k, v in table.items() if isinstance(v, dict) and k.casefold() == folded), name)


def _words_away(table: dict, where: tuple) -> dict:
    """What inherit = false leaves of a table to start from: nothing, but in the kinds. There, as before they were a
    table, it starts only the words empty (KIND_WORDS) and [kinds]' own prose_max_mb: the kinds themselves, and what
    else their entries say, stay as they came."""
    if where == ("kinds",):
        return {name: _words_away(entry, where + (name,)) for name, entry in table.items() if isinstance(entry, dict)}
    if where[:1] == ("kinds",) and len(where) == 2:
        return {k: v for k, v in table.items() if k not in KIND_WORDS}
    return {}


def load(files: list[Path]) -> Rules:
    data: dict = {}
    origin: dict = {}
    for i, path in enumerate(files):
        name = "shipped" if i < len(SHIPPED) and path.parent == DATA else str(path)
        data = merge(data, read_file(path), name, (), origin if name != "shipped" else {})
    return Rules(data, files, origin)


def commented(text: str) -> str:
    """A shipped file as a start for your own (hylly rules copy): its rules commented out, its tables' names and its
    notes as they are, so it changes nothing until you take the # off a line. A list that runs over several lines is
    written on one, so that taking the # off that line is always whole TOML: off the first of several, the next
    table's name would be read as an item of the list, and the mistake said of a line never touched."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        x = lines[i]
        if not x.strip() or x.lstrip().startswith(("#", "[")):
            out.append(x)
            i += 1
            continue
        parts = _value_lines(lines, i)
        if len(parts) == 1:
            out.append("# " + x)
            i += 1
            continue
        notes = [note for _, note in parts[1:] if note]  # a note beside an item of the list: on a line of its own
        out += [" " * (len(x) - len(x.lstrip())) + note for note in notes]
        one = parts[0][0].rstrip()
        for code, _ in parts[1:]:
            code = code.strip()
            one += code if one.endswith(("[", "{")) or code.startswith(("]", "}")) or not code else " " + code
        out.append("# " + one + (f"  {parts[0][1]}" if parts[0][1] else ""))
        i += len(parts)
    return "\n".join(out) + "\n"


def _value_lines(lines: list[str], i: int) -> list[tuple[str, str]]:
    """The lines a key's value takes from lines[i], each as (its TOML, its note after a #): one, or as many as a list
    running over several takes. A text over several lines (in triple quotes), or a list never closed, is one line each,
    as it was."""
    depth, taken = 0, []
    for line in lines[i:]:
        at, note = 0, ""
        while at < len(line):
            c = line[at]
            if line.startswith(('"""', "'''"), at):
                return [(lines[i], "")]
            if c in "\"'":  # a text: what is inside it is no bracket and no note
                at += 1
                while at < len(line) and line[at] != c:
                    at += 2 if c == '"' and line[at] == "\\" else 1
            elif c == "#":
                line, note = line[:at], line[at:]
                break
            else:
                depth += (c in "[{") - (c in "]}")
            at += 1
        taken.append((line, note))
        if depth <= 0:
            return taken
    return [(lines[i], "")]


# ---- words and patterns ------------------------------------------------------------------------

def _fold_char(c: str) -> str:
    d = unicodedata.normalize("NFKD", c)
    if len(d) > 1 and all(unicodedata.combining(x) for x in d[1:]):
        c = d[0]
    low = c.lower()
    low = low if len(low) == 1 else c
    return "σ" if low == "ς" else low  # a Greek word's last sigma is the same letter: ΟΔΟΣ is οδος


class _Folded(dict):
    """Each character's fold, by its code, worked out the first time it is asked for (str.translate's table)."""

    def __missing__(self, code: int) -> str:
        self[code] = _fold_char(chr(code))
        return self[code]


_FOLDED = _Folded()


def fold(text: str) -> str:
    """Small letters without accents, one for one: what is found in it is at the same place in the text. An accent
    written as a mark of its own stays: nfc() first joins it to its letter. The page's fold (catalog_page.html) gives
    the same, with nfc() first."""
    return text.lower() if text.isascii() else text.translate(_FOLDED)


def nfc(text: str) -> str:
    """An accent written as a mark of its own, as some file names have it, joined to its letter."""
    return unicodedata.normalize("NFC", text)


class InWords(dict):
    """Each character of a folded text as words read it (str.translate's table, worked out as asked for): a letter or a
    digit of any script as it is, and so a mark that is part of one, such as a vowel sign in Hindi, Tamil or Thai; an
    accent, a mark that goes on a letter (its combining class isn't 0), left out wherever it is, as fold() leaves out
    those of a letter written with them, so Hebrew or Arabic with its vowel points is the same word without; anything
    else a space. keep: characters that stay as they are."""

    def __init__(self, keep: str = "") -> None:
        super().__init__({ord(c): c for c in keep})

    def __missing__(self, code: int) -> str:
        c = chr(code)
        if unicodedata.category(c).startswith("M"):
            self[code] = "" if unicodedata.combining(c) else c
        else:
            self[code] = c if c.isalnum() else " "  # isalnum: what re's [^\W_] is
        return self[code]


_IN_WORDS = InWords()


def words_of(text: str) -> list[str]:
    """A text's words as searches, series and tag suggestions compare them: in one Unicode form, small letters without
    accents (fold), of letters and digits of any script (InWords), so that a name in Cyrillic, Greek, Hindi or Japanese
    has words too. The page's words() gives the same (catalog_page.html; test_rules.py runs both)."""
    return fold(nfc(text or "")).translate(_IN_WORDS).split()


def _folded(word: str) -> str:
    """A word or phrase of the rules in fold()'s small letters."""
    return fold(nfc(word.strip()))


START, END = r"(?<![^\W_])", r"(?![^\W_])"  # where a word starts and ends: not next to a letter or a digit


def _phrase(word: str) -> tuple[str, bool]:
    """One word or phrase as a pattern, in fold()'s small letters, and whether a * ends it ("starts with"). The
    spaces of a phrase are any spaces, punctuation or _, and next to a mark of its own, such as the & of "d & d",
    none too ("D&D")."""
    w = _folded(word)
    parts = w.rstrip("*").split()
    out = re.escape(parts[0])
    for before, part in zip(parts, parts[1:]):
        # between the words of a phrase: spaces or punctuation, but not the | between two texts (genres_of)
        mark = not re.search(r"[^\W_]", before) or not re.search(r"[^\W_]", part)
        out += r"(?:[^\w|]|_)" + ("*" if mark else "+") + re.escape(part)
    return out, w.endswith("*")


def _word(word: str) -> str:
    """One word or phrase as Words find it: whole words, which spaces, punctuation or _ end ("PDF_Bonus"); a * at the
    end means "starts with"."""
    out, starts = _phrase(word)
    return out + ("" if starts else END)  # where it starts: Words


def phrases(words: list[str]) -> str:
    """Words for a pattern of Hylly's own ([junk], [series]), which says what goes around them, looked for in
    fold(text) as Words are: longest first; a * at the end takes in the rest of the word. The page's reWords
    (catalog_page.html) reads [series] fold_words the same way."""
    out = []
    for w in sorted(words, key=lambda w: len(w.strip()), reverse=True):
        p, starts = _phrase(w)
        out.append(p + (r"[^\W_]*" if starts else ""))
    return "(?:" + "|".join(out) + ")" if out else "(?!)"


def alt(words: list[str]) -> str:
    """A list's items, not words ([names] suffixes, [junk] title_extensions), for a pattern of Hylly's own, which adds
    what goes around them: longest first, as written (in small letters); a * at the end means "starts with"."""
    out = [re.escape(w.lower().rstrip("*")) + (r"\w*" if w.endswith("*") else "") for w in words]
    return "(?:" + "|".join(sorted(out, key=len, reverse=True)) + ")" if out else "(?!)"


class Words:
    """Words and patterns that find a genre, a game, a kind's bundle, a label; start: where the words may be, at a
    word's start ([junk]: at the text's start)."""

    def __init__(self, words: list[str] = (), patterns: list[str] = (), start: str = START):
        found = sorted((_word(w) for w in words), key=len, reverse=True)
        self.words = re.compile(start + "(?:" + "|".join(found) + ")") if found else None
        self.patterns = re.compile(joined(patterns), re.I) if patterns else None

    def find(self, text: str, folded: str | None = None) -> str | None:
        """What it finds in the text, as written there; None when nothing. folded: fold(text), when one text is
        looked through by many (genres_of); the text is then nfc() already."""
        if not text:
            return None
        if folded is None:
            text = nfc(text)
        if self.words is not None:
            m = self.words.search(fold(text) if folded is None else folded)
            if m:
                return text[m.start():m.end()]
        if self.patterns is not None:
            m = self.patterns.search(text)
            if m:
                return m[0]
        return None


class Folded:
    """A pattern of Hylly's own with words in it (phrases), looked for in fold(text) as Words are; what it finds is
    the text as written there, since fold() keeps each letter in its place."""

    def __init__(self, pattern: re.Pattern):
        self.pattern = pattern

    def search(self, text: str) -> Hit | None:
        text = nfc(text)
        m = self.pattern.search(fold(text))
        return Hit(text, m) if m else None

    def match(self, text: str) -> Hit | None:
        text = nfc(text)
        m = self.pattern.match(fold(text))
        return Hit(text, m) if m else None

    def sub(self, repl: str, text: str) -> str:
        text = nfc(text)
        out, at = [], 0
        for m in self.pattern.finditer(fold(text)):
            out += [text[at:m.start()], repl]
            at = m.end()
        return "".join(out) + text[at:]


class Hit:
    """What a Folded pattern found: hit["series"] is that group as the text wrote it."""

    def __init__(self, text: str, m: re.Match):
        self.text, self.m = text, m

    def __getitem__(self, group) -> str | None:
        start, end = self.m.span(group)
        return self.text[start:end] if start >= 0 else None


# ---- the rules in effect ---------------------------------------------------------------------

@dataclass(frozen=True)
class Format:
    name: str
    layout: str = ""
    shop: bool = False
    book: bool = False
    keep: bool = False
    convert: str = ""  # a format worth converting it to, from a rules file of yours: nothing converts, rules try says it


@dataclass(frozen=True)
class Kind:
    """A kind a title may be ([kinds.X] in kinds.toml): what it is called, where it is best read, and what finds it."""
    name: str
    label: str  # one of them, as the library page names it: "Comic"
    plural: str  # many of them, as the page's Kind filter and the feeds name them: "Comics"
    device: str = "ereader"  # where it is best read (DEVICES), unless its file says otherwise (plan.device_for)
    words: Words | None = None  # bundle_words: found in the name of a bundle a title came in
    publishers: tuple[str, ...] = ()  # whole words in the name of who made a title, casefolded (makers)
    prose: bool = False  # by its words, but with a text file under prose_max_mb: a novel, a book
    layout: str = ""  # a title offered in a format of this layout is this kind, whatever its words
    folders: tuple[str, ...] = ()  # your own folders of this name are of this kind (folders.py), in small letters
    itchio: tuple[str, ...] = ()  # itch.io's classifications of this kind, as its survey groups them
    why_device: str = ""  # why it is read where it is, in a few words ("" for the device's own: plan.device_for)
    joins_books: bool = False  # a copy of it in another place is the same title as a book's there (merge_copies)
    text_first: bool = False  # its e-book is opened before its pages (PDF, comic): the reader and the library page
    makers: re.Pattern | None = None  # publishers, as a pattern of whole words (why)
    folder_named: bool = False  # a file named by its folder goes by its whole name (catalog.folder_named)

    @property
    def word(self) -> str:
        """Its label within a sentence, as a reason gives it: its first letter small, but where the label starts with a
        word in capitals ("RPG book")."""
        first = self.label.split(" ", 1)[0]
        return self.label if len(first) > 1 and first.isupper() else self.label[:1].lower() + self.label[1:]

    def why(self, bundles: list[str], maker: str) -> str:
        """Why a title is this kind by its words: a word in the name of a bundle it came in, or who made it; "" when
        neither."""
        for bundle in bundles:
            found = self.words.find(bundle) if self.words else None
            if found is not None:
                return f'bundle "{found}"'
        return f"publisher {maker}" if maker and self.makers and self.makers.search(maker.casefold()) else ""


# One word of a person's name in the letters A to Z alone, as names were told before any script counted (Rules.is_name_word)
A_TO_Z_NAME = re.compile(r"[A-Z](?:[a-zA-Z'’\-]+|\.?)")

# A title in a file's details that is a web address, a drive path, a code like "00 1234_FM_Intro", or starts like a
# path or a placeholder: these are Hylly's own, not words to change.
CODE_JUNK = r"^[.*<\\/]|^(?:https?|file|ftp):|^www\.|^[a-z]:\\|[^\W_]_[^\W_]"


class Rules:
    """The rules in effect, made into patterns. data: the merged tables, as the files wrote them."""

    def __init__(self, data: dict, files: list[Path] = (), origin: dict | None = None):
        self.data, self.files, self.origin = data, list(files), origin or {}
        t = lambda name: data.get(name) or {}  # noqa: E731
        f = t("filename")
        # an item with a _ in it is looked for as written, in the name before its _ become spaces (meta.from_filename):
        # "_retail" takes out "Title_retail"'s, never the word in "The Retail Giant"; one of nothing but _ found nothing
        # before, and would now run a name's words together, so it still finds nothing
        items = f.get("strip", [])
        written = [re.escape(s) for s in items if "_" in s and s.strip("_ ")]
        self.strip_written = self._compile(("filename",), "|".join(written), re.I) if written else None
        strip = [re.escape(s) for s in items if "_" not in s]
        strip += [joined(f["strip_patterns"])] if f.get("strip_patterns") else []
        self.strip = self._compile(("filename",), "|".join(strip), re.I) if strip else None
        self.number = self._compile(("filename",), f["number_pattern"]) if f.get("number_pattern") else None
        self.dots_as_spaces = bool(f.get("dots_as_spaces"))

        j = t("junk")  # words at the start of a title or a name
        code = [CODE_JUNK] + ([rf"\.{alt(j['title_extensions'])}\b"] if j.get("title_extensions") else [])
        self.junk_title = self._words(("junk",), j.get("titles", []), j.get("title_patterns", []) + code, start="^")
        self.junk_author = self._words(("junk",), j.get("authors", []), j.get("author_patterns", []), start="^")

        n = t("names")
        self.particles = [p.lower() for p in n.get("particles", [])]  # the library page's sort by author
        self.suffixes = [s.lower() for s in n.get("suffixes", [])]
        self.name_particles = {nfc(p.lower()) for p in n.get("name_particles", [])}  # a name in a file's name: is_name_word
        self.not_name_words = {w.lower() for w in n.get("not_name_words", [])}
        keep = n.get("company_words", []) + n.get("suffixes", [])
        self.keep_comma = re.compile(rf"(?i)\b{alt(keep)}\b\.?") if keep else None
        seps = n.get("author_separators", [])
        self.author_split = re.compile("|".join(re.escape(s) for s in seps), re.I) if seps else None  # " AND " too

        s = t("series")  # each looked for in fold(text): Folded
        num = r"(?P<num>\d+(?:\.\d+)?)"
        words = phrases(s.get("words", []))
        self.series = self._folded(rf"^(?P<series>.+?)[\s,:\-]*(?:{START}{words}\.?\s*|#\s*){num}\b")
        self.bracket_series = self._folded(rf"\((?P<series>[^()]+?)\s+{phrases(s.get('bracket_words', []))}\s+"
                                           rf"{num}\)\s*$")
        self.bare_volume = self._folded(rf"^{words}\.?\s*\d+(?:\.\d+)?$|^#\s*\d+$")
        self.part_label = self._folded(rf"{START}{phrases(s.get('part_words', []))}\s*#?\d+")
        self.series_fold = list(s.get("fold_words", []))  # as written: the page reads them as words too
        self.fold_words = self._folded(rf"{START}{phrases(self.series_fold)}{END}").pattern
        self.collected = self._folded(rf"{START}{phrases(s.get('collected_words', []))}{END}")
        self.comic_notes = self._folded(rf"\(\s*(?:{phrases(s.get('comic_notes', []))}|of \d+|\d+ of \d+)\s*\)")
        self.comic_noise = self._folded(rf"{START}{phrases(s.get('comic_noise', []))}{END}")

        k = t("kinds")
        self.prose_max_mb = float(k.get("prose_max_mb", 0))
        self.kinds = {name: self._kind(name, entry) for name, entry in k.items() if isinstance(entry, dict)}
        for what in ("folders", "itchio"):  # one name, one kind: else which would win?
            seen: dict[str, str] = {}
            for kind in self.kinds.values():
                for name in getattr(kind, what):
                    if name in seen:
                        raise self._clash_of(("kinds", kind.name), f"[kinds.{kind.name}] {what}: {name!r} is "
                                             f"[kinds.{seen[name]}]'s already. Take it out of one of them.")
                    seen[name] = kind.name
        lab = t("labels")
        self.labels = {name: Words(lab.get(name, [])) for name in ("extra", "audio", "video")}
        self.languages = {name: Words(words) for name, words in t("languages").items() if isinstance(words, list)}

        self.genres = [(name, self._words(("genres", name), g.get("words", []), g.get("patterns", [])))
                       for name, g in t("genres").items() if isinstance(g, dict)]
        unread = t("genres").get("titles_unread")  # the sources whose titles aren't read for a genre, by name in any case
        self.titles_unread = frozenset(s.strip().casefold() for s in unread) if isinstance(unread, list) else frozenset()
        self.kind_genres = {name: g for name, g in t("kind_genres").items() if isinstance(g, dict) and g.get("genre")}
        for name, g in t("kind_genres").items():
            if isinstance(g, dict) and name not in self.kinds:
                raise self._clash_of(("kind_genres", name), f"[kind_genres.{bare(name)}]: {name} isn't a kind. They "
                                     f"are {', '.join(self.kinds)} ([kinds.X] in kinds.toml).")
        self.games = [(name, self._words(("games", name), g.get("words", []), g.get("patterns", [])),
                       bool(g.get("ordinary"))) for name, g in t("games").items() if isinstance(g, dict)]

        self.formats = {name: Format(name, **{k: v for k, v in g.items() if k in Format.__dataclass_fields__})
                        for name, g in t("formats").items() if isinstance(g, dict)}
        fm = self.formats.values()
        self.shop_formats = {x.name for x in fm if x.shop}
        self.book_formats = {x.name for x in fm if x.book}
        self.book_suffixes = {"." + x for x in self.book_formats}
        self.keep_formats = [x.name for x in fm if x.keep]
        self.comic_formats = {x.name for x in fm if x.layout == "comic"}
        self.fixed_formats = {x.name for x in fm if x.layout in ("fixed", "comic")}
        self.reflowable = {x.name for x in fm if x.layout == "reflowable"}
        self.readable = {x.name for x in fm if x.book and x.layout}  # a book's own file, not a package
        pk = t("packages")
        self.package_books = tuple("." + x.lower().lstrip(".") for x in pk.get("books", []))
        self.package_ignore = tuple("." + x.lower().lstrip(".") for x in pk.get("ignore", []))
        md = t("media")
        self.audio_formats, self.video_formats, self.programs = ({x.lower() for x in md.get(name, [])}
                                                                 for name in ("audio", "video", "programs"))
        self.itchio_fetch = {x.strip().lower().lstrip(".") for x in t("itchio").get("fetch", [])}

    # -- patterns from several files, which only together may not go -----------------------------
    def _compile(self, where: tuple, pattern: str, flags: int = 0) -> re.Pattern:
        try:
            return re.compile(pattern, flags)
        except re.error as e:
            raise self._clash(where, e) from None

    def _words(self, where: tuple, words: list[str], patterns: list[str], start: str = START) -> Words:
        try:
            return Words(words, patterns, start)
        except re.error as e:
            raise self._clash(where, e) from None

    def _folded(self, pattern: str) -> Folded:
        return Folded(self._compile(("series",), pattern))

    def _kind(self, name: str, entry: dict) -> Kind:
        """A kind's entry as it is used. Of one a rules file adds, only what it says: its label is its name, its plural
        its label with an s, and it is read on an e-reader, unless it says otherwise."""
        label = str(entry.get("label") or name[:1].upper() + name[1:])
        words = self._words(("kinds", name), entry["bundle_words"], []) if entry.get("bundle_words") else None
        publishers = tuple(dict.fromkeys(p.strip().casefold() for p in entry.get("publishers", []) if p.strip()))
        # a publisher is found as whole words: what stands next to it is no letter or digit, or the name's start or end
        longest = sorted(publishers, key=len, reverse=True)
        makers = re.compile(r"(?<![^\W_])(?:" + "|".join(map(re.escape, longest)) + r")(?![^\W_])") if publishers else None
        return Kind(name, label, str(entry.get("plural") or label + "s"), str(entry.get("device") or "ereader"), words,
                    publishers, bool(entry.get("prose")), str(entry.get("layout") or ""),
                    tuple(f.lower() for f in entry.get("folders", [])),
                    tuple(g.lower() for g in entry.get("itchio", [])), str(entry.get("why_device") or ""),
                    bool(entry.get("joins_books")), bool(entry.get("text_first")), makers, bool(entry.get("folder_named")))

    def _clash_of(self, where: tuple, said: str) -> RulesError:
        """A mistake only the rules merged show: the files that wrote to where (the table, if not the entry)."""
        notes = self.origin.get(where, []) or self.origin.get(where[:1], [])
        files = list(dict.fromkeys(n for n, how in notes if not how.startswith(NOTED)))  # by path, as a file's own
        return RulesError(f"{', '.join(files) or 'The shipped rules'}: {said}")

    def _clash(self, where: tuple, e: re.error) -> RulesError:
        """Patterns each file's check let through, which fail joined: the table, and the files that wrote to it."""
        notes = self.origin.get(where, [])
        files = list(dict.fromkeys(self.shown(n) for n, how in notes if not how.startswith(NOTED)))
        return RulesError(f"{', '.join(files) or 'The shipped rules'}, {place(where)}: {_together(e)}")

    def shown(self, name: str) -> str:
        """One of your files by its name, or by its whole path when another of them has the same name."""
        base = Path(name).name.lower()
        return name if sum(p.name.lower() == base for p in self.files if p.parent != DATA) > 1 else Path(name).name

    # -- names ---------------------------------------------------------------------------------
    def is_junk_title(self, title: str) -> bool:
        return self.junk_title.find(title) is not None

    def is_junk_author(self, name: str) -> bool:
        return self.junk_author.find(name) is not None

    def is_name_word(self, word: str, a_to_z: bool = False) -> bool:
        """One word of a person's name in a file's name: a particle ([names] name_particles: van, de) as written; else a
        capital letter of any script that has them, alone or with a dot (an initial), or followed by letters, ' or -
        (Ann, Sävel, Éloïse, Zdzisław, O’Brien, Jean-Luc, McKay). An accent written as a mark of its own, as some file
        names have it, counts with its letter. A script without capitals has no word Hylly can tell from a title's.
        a_to_z: in the letters A to Z alone, as before any script counted (meta.from_filename's tie-break)."""
        if nfc(word) in self.name_particles:
            return True
        if a_to_z:
            return A_TO_Z_NAME.fullmatch(word) is not None
        first, rest = word[:1], word[1:]
        if not (first.isalpha() and (first.isupper() or first.istitle())):
            return False
        return rest in ("", ".") or all(c.isalpha() or c in "'’-" or unicodedata.category(c).startswith("M") for c in rest)

    def strip_number(self, text: str) -> str:
        return self.number.sub("", text, count=1) if self.number else text

    def series_key(self, series: str) -> str:
        """Spellings of one series fold together: without accents, the fold words, spaces and punctuation; its letters
        and digits, of any script, run together. The page's seriesKey (catalog_page.html) gives the same."""
        text = fold(nfc(series))  # an accent written as a mark of its own, too
        return "".join(self.fold_words.sub(" ", text).translate(_IN_WORDS).split())

    def author_key(self, name: str) -> str:
        """A name as the library page sorts authors, surname first: "Ann K. Le Moonwake" as "le moonwake ann k". Its words
        as words_of has them; a suffix at the end ([names] suffixes: Jr) left out, and the particles before the surname
        ([names] particles: van, de) kept with it."""
        found = words_of(name)
        suffixes = {" ".join(words_of(s)) for s in self.suffixes}
        particles = {" ".join(words_of(p)) for p in self.particles}
        while len(found) > 1 and found[-1] in suffixes:
            found.pop()
        start = len(found) - 1
        while start > 0 and found[start - 1] in particles:
            start -= 1
        return " ".join(found[start:] + found[:start])

    # -- kinds, labels and languages ------------------------------------------------------------
    def kind_of(self, default: str, bundles: list[str], maker: str, files: list[tuple[str, int]]) -> tuple[str, str]:
        """What kind a shop's title is, and why in a few words, the same at every shop: offered in a format of a kind's
        layout (a comic's CBZ), that kind; else the words of a kind in the name of a bundle it came in, or in the name
        of who made it (maker: its publisher, itch.io's creator, as whole words), the most particular kind first (the
        last in the table), and of one with prose, only without a text file smaller than prose_max_mb (with one, a
        novel: a book); without any, default: its shop's own word for it (itch.io's classification), else its shop's
        kind. The words of default itself change nothing (an RPG publisher's at DriveThruRPG, whose titles are RPG
        books): the prose rule is only for words that would make a title another kind. files: (format, size in
        bytes), as far as they are known: a file the shop names only in its link says nothing."""
        exts = {ext for ext, _ in files}
        particular = list(reversed(self.kinds.values()))
        for kind in particular:
            offered = exts & {f.name for f in self.formats.values() if kind.layout and f.layout == kind.layout}
            if offered:
                return kind.name, f"{'/'.join(sorted(offered)).upper()} offered"
        reflow = [size for ext, size in files if ext in self.reflowable]
        prose = bool(reflow) and max(reflow) < self.prose_max_mb * MB  # a disk's MB, as it is named: words.py
        despite = ""
        for kind in particular:
            why = kind.why(bundles, maker) if kind.name != default else ""
            if why and kind.prose and prose:
                despite = despite or why
            elif why:
                return kind.name, why
        if despite:
            return DEFAULT_KIND, f"prose despite {despite}"
        return default if default in self.kinds else DEFAULT_KIND, ""

    def itchio_kind(self, group: str | None) -> str:
        """The kind of an itch.io item by its classification, as its survey groups them ([kinds.X] itchio); "" for one
        no kind lists: its shop's kind then."""
        return next((kind.name for kind in self.kinds.values() if (group or "") in kind.itchio), "")

    def other_language(self, label: str, keep: str = "") -> bool:
        """The label names a language other than keep ("": the file whose label names none)."""
        return any(w.find(label) is not None for name, w in self.languages.items() if name != keep)

    # -- genres and games -----------------------------------------------------------------------
    def genres_of(self, *texts: str) -> list[str]:
        blob = nfc(" | ".join(t for t in texts if t))
        folded = fold(blob)  # once, for every genre
        return [name for name, w in self.genres if w.find(blob, folded) is not None]

    def game_name(self, name: str) -> str:
        text = nfc(name)
        folded = fold(text)
        return next((known for known, w, _ in self.games if w.find(text, folded) is not None), name)

    def game_names(self, *texts: str, prose: bool = False) -> list[str]:
        blob = nfc(" | ".join(t for t in texts if t))
        folded = fold(blob)
        return [name for name, w, ordinary in self.games
                if not (prose and ordinary) and w.find(blob, folded) is not None]

    # -- what came from where, for hylly rules ----------------------------------------------------
    def mark(self, *where: str) -> str:
        """shipped, from FILE, or changed in FILE."""
        notes = [(self.shown(n), how) for n, how in self.origin.get(tuple(where), []) if not how.startswith(NOTED)]
        if not notes:
            return "shipped"
        added = [n for n, how in notes if how == "added"]
        files = list(dict.fromkeys(n for n, _ in notes))
        if added:
            later = [n for n in files if n != added[0]]
            return f"from {added[0]}" + (f", changed in {', '.join(later)}" if later else "")
        return f"changed in {', '.join(files)}"


# ---- showing them: hylly rules ---------------------------------------------------------------------

def toml_value(v) -> str:
    """A value as TOML writes it; a pattern with backslashes in single quotes, as the files have them."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        if ("\\" in v or '"' in v) and "'" not in v and "\n" not in v:
            return f"'{v}'"
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return repr(v)


def _lines(key: str, v) -> list[str]:
    """key = value, a long list wrapped over lines."""
    one = f"{key} = {toml_value(v)}"
    if len(one) <= 116 or not isinstance(v, list):
        return [one]
    out, line = [], f"{key} = ["
    indent = " " * len(line)
    for i, x in enumerate(v):
        part = toml_value(x) + ("," if i < len(v) - 1 else "]")
        if len(line) + len(part) + 1 > 116 and line.strip() not in (f"{key} = [", ""):
            out.append(line.rstrip())
            line = indent
        line += part + " "
    out.append(line.rstrip())
    return out


def as_toml(r: Rules, tables: list[str]) -> str:
    """The tables in effect as TOML, each entry marked shipped, from your file, or changed in it."""
    out = [f"# In effect, from {many(len(r.files), 'rules file')} (hylly rules lists them)."]
    for table in tables:
        value = r.data.get(table) or {}
        spec = SPECS[table]
        plain = {k: v for k, v in value.items() if not isinstance(v, dict)}
        entries = {k: v for k, v in value.items() if isinstance(v, dict)}
        if plain or not entries:
            out += ["", f"[{table}]" + ("" if spec.entry else f"  # {r.mark(table)}")]
            for k, v in plain.items():
                out += _lines(k, v)
        for name, entry in entries.items():
            out += ["", f"[{table}.{bare(name)}]  # {r.mark(table, name)}"]
            for k, v in entry.items():
                out += _lines(k, v)
        gone = [w[1] for w, notes in r.origin.items() if len(w) == 2 and w[0] == table and w[1] not in entries
                and any(how == "off" for _, how in notes)]
        for name in gone:
            out += ["", f"# [{table}.{bare(name)}] is taken away (off = true in "
                        f"{', '.join(r.shown(n) for n, how in r.origin[(table, name)] if how == 'off')})"]
        out += [f"# nothing to take away: {x}" for x in _noted(r, "nothing", table)]
        out += [f"# {NEW}: {x}" for x in _noted(r, "new", table)]
    return "\n".join(out)


NEW = "a new entry, as there was none of that name"  # made by an _add or _drop (merge): [genres.Travle] words_add


def _noted(r: Rules, what: str, table: str | None = None, file: str | None = None) -> list[str]:
    """What merge noted of your files (NOTED), by what: "nothing", what they would take away that isn't there; "new",
    the entries an _add or _drop made, meant to change one that has no such name. A name written wrong, say; noted, not
    a mistake, as a later version may no longer ship what a file changes."""
    return [f"{place(where)} {how.split(': ', 1)[1]}" + ("" if file else f" (in {r.shown(n)})")
            for where, said in r.origin.items() if where and (table is None or where[0] == table)
            for n, how in said if how.startswith(what) and (file is None or n == file)]


SYSTEMS_LEAVE = frozenset({"thumbs.db", "desktop.ini"})  # what Windows leaves in a folder it has shown; else hidden


def _unread(where: Path) -> list[str]:
    """What is in a folder of rules and isn't read, and why: a file whose name doesn't end in .toml, as Notepad may save
    mine.toml.txt, and a folder, which isn't looked in. What the systems leave in a folder (a hidden file, Thumbs.db)
    isn't said."""
    read = set(_tomls(where))
    try:
        found = sorted(where.iterdir(), key=lambda p: p.name.lower())
    except OSError:  # gone or not to be read: hylly rules says what it can of the rest
        return []
    out = []
    for p in found:
        if p in read or p.name.startswith(".") or p.name.lower() in SYSTEMS_LEAVE:
            continue
        if p.is_dir():
            out.append(f"{p.name}: a folder, which isn't looked in")
        else:
            ends = f"ends in {p.suffix}, not .toml" if p.suffix else "doesn't end in .toml"
            out.append(f"{p.name}: its name {ends}, so it isn't read")
    return out


def _astray_files(where: Path, read: list[Path]) -> list[Path]:
    """The .toml files in the settings folder that hold rules no one reads: a file there with a table of rules in it
    (SPECS), which no settings file has, and which isn't read already (by rules = or --rules, under any spelling of its
    path). settings.toml itself, a copy of it or of settings.example.toml (settings*.toml) is never one."""
    if not where.is_dir():
        return []
    taken = {p.resolve() for p in read}
    out = []
    for p in _tomls(where):
        if p.name.lower().startswith("settings") or p.resolve() in taken:
            continue
        try:
            data = paths.read_toml(p)
        except (OSError, paths.NotToml):  # not to be read: nothing says it holds rules
            continue
        if any(name in SPECS and isinstance(value, dict) for name, value in data.items()):
            out.append(p)
    return out


def describe(r: Rules) -> str:
    """Where the rules come from, what each of your files changes, and how many of each there are."""
    lines = [f"Shipped rules: {', '.join(f'{n}.toml' for n in SHIPPED)}, in {DATA}"]
    mine = folder()
    how = ("rules in settings.toml" if paths.setting("rules") and not paths._env("HYLLY_RULES")
           else "HYLLY_RULES" if paths._env("HYLLY_RULES") else "next to settings.toml")
    lines.append(f"Your rules {'file' if mine.is_file() else 'folder'} ({how}): {mine}"
                 + ("" if mine.exists() else " (not there yet: hylly rules copy FILE starts one)"))
    lines += [f"  Not read: {x}" for x in (_unread(mine) if mine.is_dir() else [])]
    beside = paths.settings_file()  # a rules file saved beside settings.toml, not in the rules folder next to it
    lines += [f"  Not read: {p.name}, beside {beside.name}: rules files go in the rules folder"
              for p in _astray_files(beside.parent, r.files)]
    from .settings import misplaced  # rules written into settings.toml, which reads none, or rules = under a section

    lines += [f"  Not read: {beside.name}: {said}" for said in misplaced(paths._settings(), rules_only=True)]
    yours = [p for p in r.files if p.parent != DATA]
    for p in yours:
        notes: dict[str, list[str]] = {}
        for where, said in r.origin.items():
            for name, how in said:
                if name == str(p) and where and not how.startswith(NOTED):
                    notes.setdefault(where[0], []).append(how if len(where) == 1 else f"{how}:{where[1]}")
        parts = []
        for table, said in notes.items():
            counts = {k: sum(1 for s in said if s.split(":")[0] == k and ":" in s) for k in ("added", "changed", "off")}
            words = [f"{n} {w}" for w, n in (("added", counts["added"]), ("changed", counts["changed"]),
                                             ("taken away", counts["off"])) if n]
            if "emptied" in said:
                words.insert(0, "started empty")
            parts.append(f"[{table}] " + (", ".join(words) if words else "changed"))
        lines.append(f"  {p}" + (f": {'; '.join(parts)}" if parts else ": changes nothing"))
        lines += [f"    nothing to take away: {x}" for x in _noted(r, "nothing", file=str(p))]
        lines += [f"    {NEW}: {x}" for x in _noted(r, "new", file=str(p))]
    if not yours:
        lines.append("  No rules files of your own: the shipped ones are in effect as they are.")
    lines.append(f"In effect: {many(len(r.genres), 'genre')}, {many(len(r.games), 'game')}, {many(len(r.formats), 'format')}, "
                 f"{many(len(r.languages), 'language')}. hylly rules show FILE shows one in full.")
    return "\n".join(lines)


# ---- the rules in effect, read again when a file changes ------------------------------------------

_lock = threading.Lock()
_state: dict = {"key": None, "rules": None, "checked": -1e9, "read": 0}


def _stamp(p: Path) -> tuple:
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


def current() -> Rules:
    """The rules in effect, read once in a command: a mistake stops it. A running server (WATCH) looks at the files
    again every CHECK_EVERY seconds; a mistake made then keeps the rules from before, and is told once."""
    with _lock:
        now = time.monotonic()
        if _state["rules"] is not None and (not WATCH or now - _state["checked"] < CHECK_EVERY):
            return _state["rules"]
        _state["checked"] = now
        try:
            files = sources()
            key = tuple((str(p), *_stamp(p)) for p in files) + (tuple(EXTRA),)
            if key != _state["key"] or _state["rules"] is None:
                _state.update(rules=load(files), key=key, read=_state["read"] + 1)
            _state["told"] = None  # the files as read: put right, so a mistake made again is told again
        except (SystemExit, Exception) as e:  # noqa: BLE001 - a --rules folder gone a while, a file not UTF-8
            if _state["rules"] is None:
                raise
            said = str(e) if isinstance(e, SystemExit) else f"The rules can't be read again ({type(e).__name__}: {e})."
            if _state.get("told") != said:
                _state["told"] = said
                print(f"{said}\nThe rules from before stay in effect until it is put right.", file=sys.stderr)
        return _state["rules"]


def generation() -> int:
    """How many times the rules have been read in this program: one more whenever what is in effect may have changed,
    for what is made from them and kept (the server's page data: library.Catalog.page_script). Never the same number for
    two readings, as an object's id may be."""
    current()
    return _state["read"]


def reset() -> None:
    """Forget the rules read so far: the next use reads them afresh, and a mistake then stops it (as tests want)."""
    with _lock:
        _state.update(key=None, rules=None, checked=-1e9, told=None)


def shipped() -> Rules:
    """The shipped rules alone, without your files."""
    return load([DATA / f"{name}.toml" for name in SHIPPED])


@functools.cache
def shipped_kinds() -> tuple[str, ...]:
    """The kinds as they come, by name (kinds.toml's [kinds.X]), for words written before any rules are read: a
    command's --help. Your rules may add to them; current().kinds are the ones in effect."""
    table = read_file(DATA / "kinds.toml").get("kinds", {})
    return tuple(name for name, entry in table.items() if isinstance(entry, dict))
