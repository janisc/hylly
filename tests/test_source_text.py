"""Hylly's own text files have no stray control characters: a shell's escaping once turned a regex's \\b into a
backspace, and the regex quietly stopped matching."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = {".py", ".html", ".js", ".mjs", ".css", ".md", ".toml", ".txt", ".cmd", ".ps1", ".json", ".csv"}
CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def test_no_control_characters_in_text_files():
    bad = [str(p.relative_to(ROOT)) for folder in ("src", "tests", "tools", "docs", "scripts")
           for p in (ROOT / folder).rglob("*")
           if p.suffix.lower() in TEXT and "__pycache__" not in p.parts and p.is_file() and CONTROL.search(p.read_bytes())]
    assert not bad


def contrast(a: str, b: str) -> float:
    def light(colour: str) -> float:
        values = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4 for v in values]
        return .2126 * r + .7152 * g + .0722 * b
    high, low = sorted((light(a), light(b)), reverse=True)
    return (high + .05) / (low + .05)


def test_the_loading_screens_word_can_be_read():
    """Its colours come from the themes now (themes.py), each with a light screen and a dark."""
    from hylly import themes

    assert ("load-muted", "load-page") in themes.TEXT
    pairs = [(t.colours[m]["load-page"], t.colours[m]["load-muted"]) for t in themes.available().values() for m in themes.MODES]
    assert len(pairs) == 2 * len(themes.available())
    assert all(contrast(word, paper) >= 4.5 for paper, word in pairs), pairs


def test_every_licence_the_notices_name_is_there_and_goes_into_the_package():
    import tomllib

    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    included = notices[notices.index("## Included"):notices.index("## Python packages")]
    named = re.findall(r"\(`(src/hylly/[^`]*(?:LICENSE[^`]*|OFL-[^`]*))`\)", included)
    assert len(named) >= 7
    # the icons' licences, which live beside Hylly's own files rather than with a vendored folder
    written_in = {f"src/hylly/LICENSE-{name}.txt" for name in ("lucide", "feather")}
    assert written_in <= set(named)
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["license-files"]
    packaged = {p.relative_to(ROOT).as_posix() for pattern in globs for p in ROOT.glob(pattern)}
    assert written_in <= packaged
    assert [n for n in named if not (ROOT / n).is_file() or n not in packaged] == []


def test_the_library_pages_icons_credit_lucide_and_feather():
    """Some of the page's icons are Lucide's or Feather's, a few changed: both licences ask for their notice in copies."""
    page = (ROOT / "src" / "hylly" / "catalog_page.html").read_text(encoding="utf-8")
    first_icon = page.index("<svg", page.index("<body>"))
    assert re.search(r"<!-- Icons in this page: .*Lucide \(ISC\) and Feather \(MIT\).*THIRD-PARTY-NOTICES\.md -->",
                     page[:first_icon])
    lucide = (ROOT / "src" / "hylly" / "LICENSE-lucide.txt").read_text(encoding="utf-8")
    assert lucide.startswith("ISC License") and "Lucide Icons and Contributors" in lucide
    assert "derived from the Feather project" in lucide and "Copyright (c) 2013-present Cole Bemis" in lucide
    feather = (ROOT / "src" / "hylly" / "LICENSE-feather.txt").read_text(encoding="utf-8")
    assert feather.startswith("The MIT License (MIT)") and "Cole Bemis" in feather
    rows = [line for line in (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8").splitlines()
            if "`src/hylly/catalog_page.html`" in line]
    assert any("[Lucide]" in r and "`src/hylly/LICENSE-lucide.txt`" in r for r in rows)
    assert any("[Feather]" in r and "`src/hylly/LICENSE-feather.txt`" in r for r in rows)


def test_the_opening_s_vines_say_how_they_were_tuned_but_not_where():
    """The vines of a book's opening were tuned by eye on a page of their own, which stays out of the public repository:
    the library page's comments say what the vines are and how they were tuned, and name no mock-up."""
    page = (ROOT / "src" / "hylly" / "catalog_page.html").read_text(encoding="utf-8")
    assert not re.search(r"mock-?ups?\b", page, re.I)
    vines = page[page.index("// ---- opening: the progress in the middle"):page.index("  function start(el, prog) {")]
    assert "Tuned by eye" in vines and "K = { speed:" in vines
