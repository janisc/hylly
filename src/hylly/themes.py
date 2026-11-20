"""Themes: how the library page, the reader's bars and panels, the loading screen and the page of a book being read
look. A book's page can still be another, for that book only, chosen in the reader's Aa (the page's PAPERS).

A theme is a small TOML file of data, never code: a name, an author, a licence, an optional font from those Hylly
ships, a group (calm or colour), and a light and a dark set of colours, each with every key in KEYS, as #rrggbb. Its artist sets both: nothing
here makes a dark set from a light one. The checks (check) refuse a file that lacks a key, has a key that isn't one or
a colour that isn't one, or puts text on its background at under 3:1; under 4.5:1 is told. A key that came after themes
did (ADDED) is the exception: a file made before it lacks it, so it takes a colour worked out from the file's own
(DERIVE) or else the default theme's, and that is told, so that a new key never drops the themes people made. As only
checked colours reach the CSS, a theme can't bring anything else into a page.

Someone's own theme (own_theme) is one of these with a few colours picked: from PALETTE in the Look sheet, though any
colour of the right side will do (SIDES: in light mode a page or a background is light and what's on it dark, and the
other way round in dark mode, so that every pair reads at 4.5:1 or better). The colours that go with them are worked out
by DERIVE, the same table the page follows, and the whole is checked as a file is. It is kept for each person, never
in a file: its id, OWN, no file may take.

    themes/*.toml            shipped with Hylly; their ids are reserved
    <settings folder>/themes/*.toml    the owner's own (paths.config_dir)

The pages carry the default theme, Hylly, inline (inline_block), so they have colours before /theme.css comes; /theme.css
has every theme that may be picked ([look] themes), so switching is instant. Each colour reaches the pages twice, as
--l-<key> and --d-<key>, so that a part can follow something other than Auto/Light/Dark: the reader's bars follow the
page being read (data-chrome), the loading screen its own .dark, and the Look sheet's previews show both.
"""

from __future__ import annotations

import re
import sys
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

LIBRARY = ("bg", "panel", "ink", "muted", "line", "accent", "accent-ink", "chip", "go", "go-ink", "go-soft", "go-soft-ink",
           "star", "star-off", "ok", "warn", "bad")
READER = ("r-bar", "r-pop", "r-field", "r-rule", "r-edge", "r-dim", "r-ring", "r-ink", "r-muted", "r-soft", "r-link", "r-pick",
          "r-star", "r-mark", "r-mark-ink")
LOADER = ("load-page", "load-ink", "load-leaf", "load-fill", "load-fruit", "load-muted")  # the vines: stems, leaves' edges and fill, berries
BOOK = ("page", "page-ink", "page-muted", "page-link")  # a book's page, as the theme has it: the paper, its text, quieter text, links
KEYS = LIBRARY + READER + LOADER + BOOK
ADDED = (  # the keys that came after the first themes, with when: one a file lacks is filled in (check). Each
    # new key goes here, so that the themes people made before it keep working; any other key a file lacks is refused.
    ("page", "2026-11-11"), ("page-ink", "2026-11-11"), ("page-muted", "2026-11-11"), ("page-link", "2026-11-11"),
)
GROUPS = ("calm", "colour")  # how the Look sheet sorts them: neutral ones for long reading, and those with a feeling of their own
OWN = "yours"  # someone's own theme, made from one of these
MODES = ("light", "dark")
DEFAULT = "hylly"
FONTS = {"": 'system-ui, -apple-system, "Segoe UI", sans-serif',
         "atkinson": '"Atkinson Hyperlegible", system-ui, -apple-system, "Segoe UI", sans-serif'}  # fonts/faces.css
SERIF = "Literata, Georgia, serif"  # --serif beside --font: the reader's own serif (fonts/faces.css), whatever the theme
TEXT = (  # text on what it sits on: refused under 3:1, told under 4.5:1
    ("ink", "bg"), ("ink", "panel"), ("ink", "chip"), ("muted", "bg"), ("muted", "panel"), ("accent", "bg"), ("accent", "panel"),
    ("accent-ink", "accent"), ("go-ink", "go"), ("go-soft-ink", "go-soft"), ("ok", "panel"), ("warn", "panel"), ("bad", "panel"),
    ("r-ink", "r-bar"), ("r-ink", "r-pop"), ("r-ink", "r-field"), ("r-bar", "r-ink"), ("r-muted", "r-bar"), ("r-muted", "r-pop"),
    ("r-muted", "r-field"),  # the hint in the reader's search field (::placeholder), as muted is the library's on bg and panel
    ("r-link", "r-bar"), ("r-link", "r-pop"), ("r-mark-ink", "r-mark"), ("load-ink", "load-page"), ("load-muted", "load-page"),
    ("page-ink", "page"), ("page-muted", "page"), ("page-link", "page"))
# How much of the accent the page's tint for what's chosen has (--picked), and of the reader's pick its tint behind a
# panel's chosen row, in %: no more, so that text on them reads at 4.5:1 in any theme of one's own of named colours too
PICKED = {"light": 12, "dark": 14}
READER_PICKED = 14
TINTED = (  # text on a tint the page lays over a background, as color-mix with transparent does it (in sRGB): the text,
    # the tint's colour, how much of it in light and in dark (%), what lies under. Told under 4.5:1, never refused, as a
    # theme made before a pair came here mustn't be left out for it
    ("ink", "accent", PICKED["light"], PICKED["dark"], "panel"),  # a ticked filter and its count, the shelf you're on, a review's box
    ("r-ink", "r-pick", READER_PICKED, READER_PICKED, "r-bar"),  # the Contents row you're at, its page number too
    ("r-ink", "r-pick", READER_PICKED, READER_PICKED, "r-pop"),  # what's picked in Aa, and among a bookmark's reasons
)
SHAPES = (("star", "panel"), ("star-off", "panel"), ("star-off", "bg"),  # not text: told under 3:1. A star not given is
          ("r-star", "r-bar"), ("r-pick", "r-bar"), ("r-pick", "r-pop"))  # one too, in a review on panel and yours on bg
HEX = re.compile(r"#[0-9a-f]{6}")
ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
SHIPPED = Path(__file__).with_name("themes")
BEGIN, END = "/* theme: the default, made by themes.inline_block */", "/* theme: end */"

PALETTE = {  # the named colours of the Look sheet: every light one reads on every dark one at 6.6:1 or better
    "light": (("White", "#fbfaf6"), ("Paper", "#f4eee2"), ("Sepia", "#f3ead6"), ("Mist", "#e4eaee"), ("Sky", "#d6e7f7"),
              ("Mint", "#d8eedd"), ("Blush", "#f6dfdb"), ("Cloudberry", "#fbd79a"), ("Lilac", "#e4dcf5")),
    "dark": (("Black", "#0c0c0d"), ("Ink", "#1b1916"), ("Grey", "#3a3a3e"), ("Night", "#0f141b"), ("Bark", "#2d1f14"),
             ("Spruce", "#14301f"), ("Fjord", "#123848"), ("Lake", "#1c4a7a"), ("Plum", "#3a1d49"), ("Lingonberry", "#861b2b")),
}
PAPERS = {  # the reader's pages for one book, as the page has them (PAPERS there): page, text, quieter text, links
    "white": ("#fbfaf6", "#1c1917", "#6b645c", "#2f5d8a"), "sepia": ("#f3ead6", "#3b2f22", "#6e5e48", "#7c4a14"),
    "grey": ("#3a3a3e", "#e4e4e7", "#b4b4bb", "#93c5fd"), "black": ("#0c0c0d", "#d6d3d1", "#9c968f", "#93c5fd"),
}
CHOOSE = {  # what someone may pick for their own theme, and which side it is: a page or a background, or what's on it
    "bg": "back", "panel": "back", "ink": "front", "accent": "front",
    "page": "back", "page-ink": "front", "page-link": "front",
    "load-page": "back", "load-ink": "front", "load-leaf": "front",
}
SIDES = {  # (mode, side): the luminance a pick must have. Light at 0.70 or more on dark at 0.11 or less, and dark at 0.07
    ("light", "back"): (0.70, 1.0), ("light", "front"): (0.0, 0.11),  # or less under light at 0.50 or more: 4.5:1 at least
    ("dark", "back"): (0.0, 0.07), ("dark", "front"): (0.50, 1.0),
}
DERIVE = (  # someone's own theme: [key, from, towards, how far, what it must read on], worked out in order once anything
    # it comes from or sits on was picked or worked out, but never one they picked. With something to read on, it goes a
    # twentieth at a time until it reads there at 4.5:1: back from how far (quieter text), or out from where it was when
    # that's below 0 (a colour of the theme they didn't pick, on a background they did). "on": white or near black,
    # whichever reads better on it. "": the same.
    ["accent", "accent", "ink", -1, ["bg", "panel"]], ["ok", "ok", "ink", -1, ["panel"]], ["warn", "warn", "ink", -1, ["panel"]],
    ["bad", "bad", "ink", -1, ["panel"]], ["page-link", "page-link", "page-ink", -1, ["page"]], ["load-ink", "load-ink", "ink", -1, ["load-page"]],
    ["muted", "ink", "bg", 0.3, ["bg", "panel"]], ["star-off", "ink", "bg", 0.5, ["bg", "panel"]],  # a star not given, a shape: grey
    ["line", "bg", "ink", 0.14, []], ["chip", "bg", "ink", 0.07, []],
    ["accent-ink", "accent", "on", 0, []],
    ["r-bar", "bg", "", 0, []], ["r-pop", "panel", "", 0, []], ["r-field", "chip", "", 0, []], ["r-rule", "line", "", 0, []],
    ["r-edge", "bg", "ink", 0.24, []], ["r-dim", "bg", "ink", 0.4, []], ["r-ring", "bg", "ink", 0.5, []], ["r-ink", "ink", "", 0, []],
    ["r-muted", "ink", "bg", 0.3, ["r-bar", "r-pop", "r-field"]], ["r-soft", "ink", "bg", 0.2, []], ["r-link", "accent", "", 0, []], ["r-pick", "accent", "", 0, []],
    ["page-muted", "page-ink", "page", 0.28, ["page"]], ["load-fill", "load-page", "load-leaf", 0.28, []],
    ["load-muted", "load-ink", "load-page", 0.18, ["load-page"]],
)
ON_LIGHT, ON_DARK = "#ffffff", "#0f141b"


class NotATheme(ValueError):
    """A theme file that can't be used, and why."""


@dataclass
class Theme:
    id: str
    name: str
    author: str
    licence: str
    about: str
    font: str
    colours: dict[str, dict[str, str]]  # mode -> key -> #rrggbb
    path: Path | None
    told: list[str] = field(default_factory=list)  # what the checks say, short of refusing
    group: str = "calm"
    base: str = ""  # someone's own: the theme it was made from


def _luminance(colour: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG's contrast ratio of two #rrggbb colours, 1 to 21."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check(path: Path) -> Theme:
    """A theme file read and checked. Raises NotATheme with what's wrong."""
    try:
        data = paths.read_toml(path)  # a BOM at its start, as Notepad may write, is fine
    except paths.NotToml as e:
        raise NotATheme(e.said(path.name)) from None
    except OSError as e:
        raise NotATheme(f"{path.name}: {e}") from None
    theme_id = data.get("id")
    if not isinstance(theme_id, str) or not ID.fullmatch(theme_id):
        raise NotATheme(f"{path.name}: id is a short name in small letters, digits and -, such as \"birch\".")
    if theme_id == OWN:
        raise NotATheme(f"{path.name}: the id {OWN!r} is each person's own theme's: give it another.")
    words = {}
    for key in ("name", "author", "licence", "about", "font", "group"):
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > 200 or re.search(r"[\x00-\x1f\x7f<>{};]", value):
            raise NotATheme(f"{path.name}: {key} is a line of text, without < > {{ }} or ;.")
        words[key] = value.strip()
    if not words["name"]:
        raise NotATheme(f"{path.name}: it needs a name.")
    if words["font"] not in FONTS:
        raise NotATheme(f"{path.name}: font is one of {', '.join(repr(f) for f in FONTS)}.")
    words["group"] = words["group"] or "calm"
    if words["group"] not in GROUPS:
        raise NotATheme(f"{path.name}: group is one of {', '.join(repr(g) for g in GROUPS)}.")
    unknown = set(data) - {"id", "name", "author", "licence", "about", "font", "group", *MODES}
    if unknown:
        raise NotATheme(f"{path.name}: {', '.join(sorted(unknown))} isn't part of a theme.")
    colours, lacking = {}, {}
    for mode in MODES:
        given = data.get(mode)
        if not isinstance(given, dict):
            raise NotATheme(f"{path.name}: [{mode}] is missing: a theme sets both modes.")
        extra = sorted(set(given) - set(KEYS))
        if extra:
            raise NotATheme(f"{path.name}: [{mode}] has {', '.join(extra)}, which aren't keys.")
        bad = [k for k in KEYS if k in given and (not isinstance(given[k], str) or not HEX.fullmatch(given[k].lower()))]
        if bad:
            raise NotATheme(f"{path.name}: [{mode}] {', '.join(bad)}: a colour is #rrggbb, such as \"#2f5d8a\".")
        mine = {k: given[k].lower() for k in KEYS if k in given}
        missing = [k for k in KEYS if k not in mine]
        if missing:  # made before a key came (ADDED): that one from its own colours, as for one's own, or the default's
            base, later = _default_colours(mode), {k for k, _ in ADDED}
            refused = [k for k in missing if k not in later or k not in base]
            if refused:
                raise NotATheme(f"{path.name}: [{mode}] lacks {', '.join(refused)}.")
            lacking[mode] = missing
            out = worked_out({**base, **mine}, set(mine))
            mine = {k: out[k] for k in KEYS}
        colours[mode] = mine
    try:
        told = _read_well(colours, path.name)
    except NotATheme as e:  # the pair too faint to read, as _read_well found it first: a key filled in may be why
        faint = next(((m, fg, bg) for m in MODES for fg, bg in TEXT if contrast(colours[m][fg], colours[m][bg]) < 3), None)
        took = [k for k in faint[1:] if k in lacking.get(faint[0], [])] if faint else []
        if not took:
            raise
        raise NotATheme(f"{e} It lacks {' and '.join(took)}: set {'it' if len(took) == 1 else 'them'} in the file.") from None
    both = lacking.get("light") == lacking.get("dark")  # said once for the two, as a key that came is missing from both
    said = [("[light] and [dark] have" if both else f"[{mode}] has") + f" no {', '.join(keys)}: worked out from its other colours, "
            "or else the default theme's" for mode, keys in lacking.items() if not (both and mode == "dark")]
    return Theme(theme_id, words["name"], words["author"], words["licence"], words["about"], words["font"], colours, path, said + told,
                 words["group"])


def _default_colours(mode: str) -> dict[str, str]:
    """The default theme's colours in a mode, as its file has them: where a key another theme's file lacks starts from."""
    try:
        given = tomllib.loads((SHIPPED / f"{DEFAULT}.toml").read_text(encoding="utf-8")).get(mode)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(given, dict):
        return {}
    return {k: v.lower() for k, v in given.items() if k in KEYS and isinstance(v, str) and HEX.fullmatch(v.lower())}


def tint(colours: dict[str, str], colour: str, share: int, under: str) -> str:
    """What the page shows for colour laid at share % over under (color-mix(in srgb, colour share%, transparent))."""
    return mix(colours[under], colours[colour], share / 100)


def lowest_text(colours: dict[str, dict[str, str]]) -> float:
    """The faintest of a theme's text, in either mode: on its own colours (TEXT) and on the page's tints (TINTED)."""
    return min([contrast(colours[m][fg], colours[m][bg]) for m in MODES for fg, bg in TEXT]
               + [contrast(colours[m][fg], tint(colours[m], colour, light if m == "light" else dark, under))
                  for m in MODES for fg, colour, light, dark, under in TINTED])


def _read_well(colours: dict[str, dict[str, str]], name: str) -> list[str]:
    """The contrast checks: text under 3:1 on what it sits on is refused (NotATheme), and under 4.5:1 told, as are text
    on a tint under 4.5:1 and shapes under 3:1. Returns what's told."""
    told = []
    for mode in MODES:
        for fg, bg in TEXT:
            ratio = contrast(colours[mode][fg], colours[mode][bg])
            if ratio < 3:
                raise NotATheme(f"{name}: [{mode}] {fg} on {bg} is {ratio:.2f}:1, too faint to read; 4.5:1 at least.")
            if ratio < 4.5:
                told.append(f"[{mode}] {fg} on {bg} is {ratio:.2f}:1, under 4.5:1")
        for fg, colour, light, dark, under in TINTED:
            share = light if mode == "light" else dark
            ratio = contrast(colours[mode][fg], tint(colours[mode], colour, share, under))
            if ratio < 4.5:
                told.append(f"[{mode}] {fg} on {colour} at {share}% over {under} is {ratio:.2f}:1, under 4.5:1")
        for fg, bg in SHAPES:
            ratio = contrast(colours[mode][fg], colours[mode][bg])
            if ratio < 3:
                told.append(f"[{mode}] {fg} on {bg} is {ratio:.2f}:1, under 3:1")
    return told


def mix(a: str, b: str, t: float) -> str:
    """a, moved towards b by t, in sRGB as the page does it (rounded the same)."""
    x = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    y = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(v + (w - v) * t + 0.5):02x}" for v, w in zip(x, y))


def fits(mode: str, key: str, colour: str) -> bool:
    """Whether a colour may be picked for this key in this mode (CHOOSE, SIDES)."""
    if key not in CHOOSE or mode not in MODES or not isinstance(colour, str) or not HEX.fullmatch(colour):
        return False
    low, high = SIDES[(mode, CHOOSE[key])]
    return low <= _luminance(colour) <= high


def worked_out(colours: dict[str, str], picked: set[str]) -> dict[str, str]:
    """One mode's colours with DERIVE applied: each key worked out once one it comes from was picked or worked out."""
    out, changed = dict(colours), set(picked)
    for key, a, b, t, under in DERIVE:
        if key in picked or not changed & {a, b, *under}:
            continue
        if b == "on":
            out[key] = ON_LIGHT if contrast(ON_LIGHT, out[a]) >= contrast(ON_DARK, out[a]) else ON_DARK
        elif not b:
            out[key] = out[a]
        else:
            start, end, n = out[a], out[b], round(abs(t) * 20)
            for steps in (range(n, -1, -1) if t >= 0 else range(n + 1)):
                out[key] = mix(start, end, steps / 20)
                if all(contrast(out[key], out[u]) >= 4.5 for u in under):
                    break
        changed.add(key)
    return out


def own_theme(base: Theme, picks) -> Theme:
    """Someone's own theme: base with their picks ({"light": {key: colour}, "dark": {...}}), and what goes with them.
    Raises NotATheme with what's wrong: a key that can't be picked, a colour of the wrong side, or a whole that reads
    under 3:1 somewhere."""
    if not isinstance(picks, dict) or set(picks) - set(MODES):
        raise NotATheme("Your own theme is a light and a dark set of colours.")
    colours = {}
    for mode in MODES:
        mine = picks.get(mode) or {}
        if not isinstance(mine, dict) or len(mine) > len(CHOOSE):
            raise NotATheme("Your own theme is a light and a dark set of colours.")
        for key, colour in mine.items():
            if key not in CHOOSE:
                raise NotATheme(f"{key} isn't one of the colours to pick.")
            if not fits(mode, key, colour):
                side = "light" if (mode == "light") == (CHOOSE[key] == "back") else "dark"
                raise NotATheme(f"In {mode} mode, {key} takes a {side} colour, so that what's on it reads well.")
        colours[mode] = worked_out({**base.colours[mode], **mine}, set(mine))
    told = _read_well(colours, f"{base.name}, yours")
    return Theme(OWN, f"{base.name}, yours", "", "", f"Your own colours, made from {base.name}.", base.font, colours, None, told,
                 base.group, base.id)


def for_page(picked: list[Theme]) -> dict:
    """What the Look sheet needs: each theme that may be picked with all its colours, and the rules for one's own."""
    return {"looks": [{"id": t.id, "name": t.name, "about": t.about, "group": t.group, "font": t.font, **t.colours} for t in picked],
            "palette": {m: [list(c) for c in PALETTE[m]] for m in MODES}, "choose": CHOOSE,
            "sides": {f"{m} {s}": v for (m, s), v in SIDES.items()}, "derive": [list(r) for r in DERIVE], "on": [ON_LIGHT, ON_DARK]}


def owners_dir() -> Path:
    return paths.config_dir() / "themes"


def available(told=None) -> dict[str, Theme]:
    """Every theme that can be used, shipped first, then the owner's. One that can't, or that takes a shipped one's
    id, is left out and said (told: where to, else standard error)."""
    say = told or (lambda line: print(line, file=sys.stderr))
    found: dict[str, Theme] = {}
    for folder, own in ((SHIPPED, False), (owners_dir(), True)):
        for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
            try:
                theme = check(path)
            except NotATheme as e:
                say(f"The theme {e} It's left out.")
                continue
            if theme.id in found:
                say(taken(path, theme.id, not own or found[theme.id].path.parent == SHIPPED))
                continue
            found[theme.id] = theme
    return found


def taken(path: Path, theme_id: str, hyllys: bool) -> str:
    """What is said of a theme whose id another one has already: Hylly's own (hyllys), or another of the owner's. By
    available(), which leaves it out, and by hylly theme check, which refuses it with the same words."""
    return (f"The theme {path} has the id {theme_id!r}, which {'Hylly' if hyllys else 'another'} theme has. It's left "
            "out: give it an id of its own.")


def allowed(themes: dict[str, Theme], look: dict, told=None) -> tuple[str, list[Theme]]:
    """[look] applied: the library's default theme and those that may be picked, the default first. One named that
    isn't there is said and passed over, never stopping anything."""
    say = told or (lambda line: print(line, file=sys.stderr))
    wanted = look.get("themes") or list(themes)
    picked = [themes[t] for t in dict.fromkeys(wanted) if t in themes]
    for t in wanted:
        if t not in themes:
            say(f"[look] themes names {t!r}, which isn't a theme here (hylly theme list): it's left out.")
    default = look.get("theme") or DEFAULT
    if default not in themes:
        say(f"[look] theme = {default!r} isn't a theme here (hylly theme list): {DEFAULT} instead.")
        default = DEFAULT
    if themes.get(default) and themes[default] not in picked:
        picked.insert(0, themes[default])
    picked.sort(key=lambda t: (t.id != default, t.name.lower()))
    return default, picked


def _sets(theme: Theme) -> str:
    return " ".join(f"--{m[0]}-{k}: {theme.colours[m][k]};" for m in MODES for k in KEYS) + f" --font: {FONTS[theme.font]};"


def theme_css(themes: list[Theme]) -> str:
    """/theme.css: each theme's colours, for data-skin on the page's root, and for data-pv on a preview of it in the
    Look sheet."""
    return "".join(f':root[data-skin="{t.id}"], [data-pv="{t.id}"] {{ {_sets(t)} }}\n' for t in themes)


def _active(mode: str) -> str:
    m = mode[0]
    return (" ".join(f"--{k}: var(--{m}-{k});" for k in LIBRARY) + f" --code: var(--{m}-chip);"
            f" --picked: color-mix(in srgb, var(--{m}-accent) {PICKED[mode]}%, transparent);"
            f" --shadow: 0 1px 3px rgba(0, 0, 0, {'.12' if m == 'l' else '.5'}); color-scheme: {mode};")


def _reader(mode: str) -> str:
    return " ".join(f"--{k}: var(--{mode[0]}-{k});" for k in READER)


def _loader(mode: str) -> str:
    m = mode[0]
    short = {"load-page": "lp", "load-ink": "li", "load-leaf": "ll", "load-fill": "lf", "load-fruit": "lr", "load-muted": "lm"}
    return " ".join(f"--{short[k]}: var(--{m}-{k});" for k in LOADER) + f" --lt: color-mix(in srgb, var(--{m}-load-ink) 16%, transparent);"


def _preview(mode: str) -> str:
    return " ".join(f"--p-{k}: var(--{mode[0]}-{k});" for k in KEYS)


def inline_block(theme: Theme | None = None, library: bool = False) -> str:
    """What each page carries between BEGIN and END: the default theme's colours and font, and which of the colours each
    part uses. The library follows Auto/Light/Dark; the reader's bars its data-chrome, light over white and sepia paper
    and dark otherwise (the guide always dark); the loading screen its .dark. The library page's (library) has its
    reader's serif too, beside the font (--serif), and the Look sheet's previews of a theme, each in its .l or .d, by
    --p-<key>, every key."""
    theme = theme or check(SHIPPED / f"{DEFAULT}.toml")
    return "\n".join([
        BEGIN,
        f":root {{ {_sets(theme)}{f' --serif: {SERIF};' if library else ''} }}",
        f":root {{ {_active('light')} }}",
        f'@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {_active("dark")} }} }}',
        f':root[data-theme="dark"] {{ {_active("dark")} }}',
        f'#reader, #reader[data-chrome="dark"], .rguide {{ {_reader("dark")} }}',
        f'#reader[data-chrome="light"] {{ {_reader("light")} }}',
        f".rload {{ {_loader('light')} }}",
        f".rload.dark {{ {_loader('dark')} }}",
        *([f".pv.l {{ {_preview('light')} }}", f".pv.d {{ {_preview('dark')} }}"] if library else []),
        END,
    ])


def page_block(text: str) -> str | None:
    """The block a page carries, as it is in the page."""
    found = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), text, re.S)
    return found[0] if found else None


_lock = threading.Lock()
_now: dict = {"stamp": None, "value": None}


def current() -> tuple[str, list[Theme], str]:
    """For the server: the library's default theme, those that may be picked, and /theme.css, as the theme files and
    [look] in settings.toml are now. Made again when either changes; what's wrong in them is said then, once."""
    from . import settings

    look = settings.get("look")
    files = tuple((str(p), p.stat().st_mtime_ns) for folder in (SHIPPED, owners_dir()) if folder.is_dir() for p in sorted(folder.glob("*.toml")))
    stamp = (files, look["theme"], tuple(look["themes"]))
    with _lock:
        if _now["stamp"] != stamp:
            default, picked = allowed(available(), look)
            _now.update(stamp=stamp, value=(default, picked, theme_css(picked)))
        return _now["value"]
