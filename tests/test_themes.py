"""Themes: the files and their checks, the CSS made of them, each page carrying the default and no colour of its own
outside a theme, and the server's part: /theme.css for anyone, the login page's colours, [look], and each person's pick."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from hylly import bell, paths, people, themes

from helpers import EVERY_ROLE, HOME, in_the_page, logged_in


@pytest.fixture
def users():
    return EVERY_ROLE  # the library fixture's: bea a borrower, rory a reader, lucy a librarian, owen an owner


PAGES = ("catalog_page", "help", "login_page", "server_page", "database_page")
SRC = Path(themes.__file__).parent
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")
SHADE = re.compile(r"rgba\(\s*0,\s*0,\s*0,\s*[.\d]+\)")  # black, see-through: shadows and dimmers, the same in every theme


def shipped_ids() -> list[str]:
    """The ids of the themes in Hylly's own folder, in the order the files sort in: a theme added there is tested as the
    others are, with no list here to add it to."""
    import tomllib

    return [tomllib.loads(p.read_text(encoding="utf-8"))["id"] for p in sorted(themes.SHIPPED.glob("*.toml"))]


def birch_text() -> str:
    return (themes.SHIPPED / "birch.toml").read_text(encoding="utf-8")


def theme_file(tmp_path, text, name="mine.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- the shipped themes, and the pages -----------------------------------------------------------

def test_the_themes_in_hylly_s_folder_come_calm_and_colour_and_all_read_well():
    found = themes.available()
    assert list(found) == shipped_ids() and len(found) >= 2 and found[themes.DEFAULT].group == "calm"
    assert {t.group for t in found.values()} == set(themes.GROUPS)  # some of each
    for theme in found.values():
        assert not theme.told and theme.author and theme.licence and theme.about
        for mode in themes.MODES:
            for fg, bg in themes.TEXT:
                assert themes.contrast(theme.colours[mode][fg], theme.colours[mode][bg]) >= (7 if theme.id == "clear" else 4.5), (theme.id, mode, fg, bg)
    assert found["clear"].font == "atkinson" and "Atkinson Hyperlegible" in (SRC / "fonts" / "faces.css").read_text(encoding="utf-8")


def test_hylly_is_the_library_as_it_was():
    hylly = themes.available()["hylly"].colours
    assert (hylly["light"]["bg"], hylly["light"]["accent"], hylly["light"]["go"]) == ("#f6f4ef", "#2f5d8a", "#2a7d4c")
    assert (hylly["dark"]["r-bar"], hylly["dark"]["r-ink"], hylly["dark"]["r-pick"]) == ("#18181b", "#f4f4f5", "#60a5fa")  # the reader's bars as they were
    book = lambda mode: tuple(hylly[mode][k] for k in themes.BOOK)  # noqa: E731
    assert book("light") == themes.PAPERS["white"] and book("dark") == themes.PAPERS["black"]  # its pages are Auto's, as before


def test_the_pages_papers_are_the_servers():
    """The reader's pages for one book, in the page, are those a theme of your own may keep (themes.PAPERS)."""
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    block = re.search(r"const PAPERS = \{(.*?)\};", page, re.S)[1]
    found = {name: tuple(re.findall(r'"(#[0-9a-f]{6})"', colours)) for name, colours in re.findall(r"(\w+): \[([^\]]*)\]", block)}
    assert found == themes.PAPERS


@pytest.mark.parametrize("page", PAGES)
def test_each_page_carries_the_default_theme_as_its_file_says(page):
    text = (SRC / f"{page}.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    library = page == "catalog_page"  # with the reader's serif and the Look sheet's previews too
    assert themes.page_block(text) == themes.inline_block(library=library), "renew it: themes.inline_block() between its markers"


def test_the_look_sheets_previews_take_every_key_they_use_from_the_block_the_themes_make():
    """The previews of a theme in the Look sheet each show it light (.pv.l) or dark (.pv.d), by --p- names: those two
    lines were typed by hand into the page, with 20 of the keys, and a key added to the themes had to be remembered
    there. inline_block writes them now, every key, as it writes the reader's and the loading screen's; the page uses
    no --p- name the block doesn't give."""
    block = themes.inline_block(library=True)
    for mode in themes.MODES:
        line = next(line for line in block.splitlines() if line.startswith(f".pv.{mode[0]} {{"))
        assert re.findall(r"--p-([\w-]+): var\(--(\w)-([\w-]+)\);", line) == [(k, mode[0], k) for k in themes.KEYS]
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    used = set(re.findall(r"var\(--p-([\w-]+)\)", page))
    assert used and used <= set(themes.KEYS), sorted(used - set(themes.KEYS))
    style = re.sub(r"/\*.*?\*/", "", page[page.index(themes.END):page.index("</style>")], flags=re.S)
    assert ".pv.l" not in style and ".pv.d" not in style and "--p-" not in style.replace("var(--p-", "")  # none by hand
    assert ".pv.l" not in themes.inline_block() and "--serif" not in themes.inline_block()  # the other pages have no previews


def test_the_serif_is_named_once_beside_the_font_and_used_by_its_name():
    """The reader's serif, Literata with Georgia after it, was written out at each of its twelve places, while the
    sans had a name (--font). It has its own now, --serif, beside --font in the theme block, and the page's style and
    markup use it by name."""
    block = themes.inline_block(library=True)
    assert f"--font: {themes.FONTS['']}; --serif: {themes.SERIF};" in block.splitlines()[1]
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    script = page.index('<script type="text/hylly-later"')
    outside_block = page[:script].replace(themes.page_block(page), "")
    assert themes.SERIF not in outside_block and outside_block.count("var(--serif)") >= 11


SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S)
COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*")  # what a script says, not what it does
READ = re.compile(r'\b([A-Za-z_$][\w$]*) = (localStorage\.getItem\("[^"]*"\)(?: \|\| localStorage\.getItem\("[^"]*"\))*)')  # a choice, from where


def head_scripts(page: str) -> list[str]:
    """The scripts in a page's head, in their order, as the page has them."""
    text = (SRC / f"{page}.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    return SCRIPT.findall(text[text.index("<head>"):text.index("</head>")])


def does_all_of(script: str, other: str) -> bool:
    """Whether other does all script does, in the same words and order, comments aside: each line of script that
    reads the browser's storage has its reads among other's, and every other line is one of other's, in order."""
    lines = [line.rstrip() for line in COMMENT.sub("", other).split("\n")]
    reads, at = set(READ.findall(COMMENT.sub("", other))), 0
    for line in (line.rstrip() for line in COMMENT.sub("", script).split("\n")):
        if not line:
            continue
        if READ.search(line):
            if not set(READ.findall(line)) <= reads:
                return False
            continue
        if line not in lines[at:]:
            return False
        at = lines.index(line, at) + 1
    return True


def test_each_page_puts_on_the_browser_s_theme_as_the_library_page_does():
    """The script that puts on the theme picked in this browser, before anything is drawn, and checks what it reads
    from the browser's storage, is pasted into each page by hand, as the theme block is: the first script in the head of
    the login, server, database and help pages is the same, byte for byte, and one in the library page's head does all
    it does, and more (its view). A copy fallen behind would go unseen in the one page whose colours then come out
    wrong."""
    shared = {page: head_scripts(page)[0] for page in PAGES[1:]}
    assert len(set(shared.values())) == 1, ("renew them: the same script first in every page but the library page", shared)
    assert any(does_all_of(shared["help"], other) for other in head_scripts("catalog_page"))


@pytest.mark.parametrize("page", PAGES)
def test_no_page_has_a_colour_of_its_own_outside_its_tokens(page):
    """Presentation apart from logic: a colour is named once, as a custom property, and used by name."""
    text = (SRC / f"{page}.html").read_text(encoding="utf-8")
    stray = []
    for style in re.findall(r"<style>(.*?)</style>", text, re.S):
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        for declaration in re.split(r"[;{}]", style):
            declaration = declaration.strip()
            if declaration.startswith("--"):
                continue
            stray += [c for c in COLOUR.findall(declaration) if not SHADE.fullmatch(c)]
    assert not stray, stray


def test_a_fields_hint_is_in_the_themes_quieter_text_which_the_check_holds_on_every_field():
    """The search box's hint is its only visible label. The browsers' own grey reads under 4.5:1 in most themes, so
    hints take muted, and in the reader r-muted; the theme check covers what each sits on, and so every theme does."""
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    assert "::placeholder { color: var(--muted); opacity: 1; }" in page and "#reader ::placeholder { color: var(--r-muted); }" in page
    assert {("muted", "bg"), ("muted", "panel"), ("r-muted", "r-field")} <= set(themes.TEXT)  # the page's fields, and the reader's search
    assert next(row for row in themes.DERIVE if row[0] == "r-muted")[4] == ["r-bar", "r-pop", "r-field"]  # one's own theme too
    for page in PAGES[1:]:  # the others have no hints: one there would want the same rule
        assert "placeholder=" not in (SRC / f"{page}.html").read_text(encoding="utf-8"), page


def test_the_pages_load_every_theme_and_the_login_page_gets_them_inline():
    for page in ("catalog_page", "help", "server_page"):
        assert '<link rel="stylesheet" href="/theme.css">' in (SRC / f"{page}.html").read_text(encoding="utf-8")
    login = (SRC / "login_page.html").read_text(encoding="utf-8")
    assert "/theme.css" not in login and "/* every theme, from the server */" in login


# ---- a theme file's checks ----------------------------------------------------------------------

@pytest.mark.parametrize("change, said", [
    (lambda t: t.replace('id = "birch"', 'id = "Birch!"'), "id is"),
    (lambda t: t.replace('name = "Birch"', 'name = "<b>Birch</b>"'), "line of text"),
    (lambda t: t.replace('name = "Birch"', 'name = ""'), "needs a name"),
    (lambda t: t.replace('font = ""', 'font = "comic-sans"'), "font is one of"),
    (lambda t: 'css = "body { color: red }"\n' + t, "isn't part of a theme"),
    (lambda t: t[:t.index("[dark]")], "[dark] is missing"),
    (lambda t: t.replace('bg = "#f5f7f8"', 'bg = "white"'), "a colour is #rrggbb"),
    (lambda t: t.replace('bg = "#f5f7f8"', 'bg = "#f5f7f8; background: url(x)"'), "a colour is #rrggbb"),
    (lambda t: re.sub(r'\nmuted = "#536270"[^\n]*', "", t, count=1), "[light] lacks muted."),  # a key themes had from the start
    (lambda t: t[:t.index("[dark]")] + "[dark]\n", "[dark] lacks bg, panel, ink, muted,"),
    (lambda t: t.replace('bg = "#f5f7f8"', 'bg = "#f5f7f8"\nsparkle = "#ffffff"'), "sparkle, which aren't keys"),
    (lambda t: t.replace('muted = "#536270"', 'muted = "#c8d0d6"'), "too faint to read"),
    (lambda t: t.replace("[light]", "[light"), "mine.toml"),
])
def test_a_theme_that_cant_be_used_says_why(tmp_path, change, said):
    with pytest.raises(themes.NotATheme, match=re.escape(said)):
        themes.check(theme_file(tmp_path, change(birch_text())))


def lacking(text, keys, modes=themes.MODES) -> str:
    """A theme file's text without these keys in these modes: as one made before a later Hylly added them."""
    light, dark = text.split("[dark]")
    parts = {"light": light, "dark": dark}
    for mode in modes:
        parts[mode] = re.sub(rf"^(?:{'|'.join(map(re.escape, keys))}) = [^\n]*\n", "", parts[mode], flags=re.M)
    return parts["light"] + "[dark]" + parts["dark"]


def test_a_theme_made_before_a_key_came_is_still_used_with_the_default_theme_s_and_says_so(tmp_path):
    """A new colour key mustn't drop the themes people made. One their file lacks, here the book's page, which came
    after themes did, takes the default theme's where nothing of the file's own works it out, and is told once.
    Only such keys (ADDED): one themes always had is still refused, above."""
    assert set(themes.BOOK) <= {k for k, _ in themes.ADDED}
    assert all(k in themes.KEYS and re.fullmatch(r"2\d{3}-\d\d-\d\d", when) for k, when in themes.ADDED)
    theme = themes.check(theme_file(tmp_path, lacking(birch_text(), themes.BOOK)))
    birch, hylly = themes.available()["birch"].colours, themes.available()["hylly"].colours
    assert all(theme.colours[m][k] == hylly[m][k] for m in themes.MODES for k in themes.BOOK)
    assert all(theme.colours[m][k] == birch[m][k] for m in themes.MODES for k in themes.KEYS if k not in themes.BOOK)  # its own as they were
    assert theme.told == ["[light] and [dark] have no page, page-ink, page-muted, page-link: worked out from its other colours, or else "
                          "the default theme's"]
    owners(lacking(birch_text().replace('id = "birch"', 'id = "pulp"').replace('name = "Birch"', 'name = "Pulp"'), themes.BOOK))
    said = []
    assert "pulp" in themes.available(told=said.append) and not said  # in the picker, and nothing said on each request


def test_a_key_a_theme_lacks_is_worked_out_from_its_own_colours_where_the_table_says_how(tmp_path):
    theme = themes.check(theme_file(tmp_path, lacking(birch_text(), ["page-muted", "page-link"], modes=["light"])))
    light, hylly = theme.colours["light"], themes.available()["hylly"].colours["light"]
    assert light["page-muted"] not in (hylly["page-muted"], light["page-ink"])  # from its own page and text
    assert all(themes.contrast(light[k], light["page"]) >= 4.5 for k in ("page-muted", "page-link"))
    assert theme.colours["dark"] == themes.available()["birch"].colours["dark"]  # the other mode as it was
    assert theme.told == ["[light] has no page-muted, page-link: worked out from its other colours, or else the default theme's"]


def test_a_colour_taken_for_a_lacking_key_must_still_read_and_the_refusal_says_what_s_lacking(tmp_path):
    text = lacking(birch_text().replace('page = "#f9fafb"', 'page = "#3a3a3e"'), ["page-ink"], modes=["light"])  # the default's dark text, on grey
    with pytest.raises(themes.NotATheme, match=r"\[light\] page-ink on page is .* too faint to read.* It lacks page-ink: set it in the file\.$"):
        themes.check(theme_file(tmp_path, text))


def test_a_refusal_names_what_the_file_lacks_only_when_that_is_why(tmp_path):
    text = lacking(birch_text().replace('muted = "#536270"', 'muted = "#c8d0d6"'), themes.BOOK)  # faint quieter text of its own
    with pytest.raises(themes.NotATheme) as refused:
        themes.check(theme_file(tmp_path, text))
    assert "muted on bg" in str(refused.value) and str(refused.value).endswith("4.5:1 at least.") and "lacks" not in str(refused.value)


def test_faint_but_readable_is_told_not_refused(tmp_path):
    theme = themes.check(theme_file(tmp_path, birch_text().replace('muted = "#536270"', 'muted = "#7b8792"')))
    assert any("muted on bg" in line and "under 4.5:1" in line for line in theme.told)


def test_text_on_the_tints_the_page_lays_over_a_theme_is_checked_and_reads_in_every_shipped_theme():
    """The check held text on a theme's own colours only, so the page number of the Contents row you're at (on the
    reader's pick, 18% over its bar), a ticked filter's count (on the accent's tint), the ✕ of a picked filter (at 70%)
    and a star not given fell under what the help promises in six of nine themes, and an owner's theme passed with the
    same gaps. Text on a tint is in the check now (TINTED, told under 4.5:1), and a star not given is a shape there; the
    page draws those rows' text in the theme's ink, with the tints the check has, and every shipped theme passes it all.
    The tints are no stronger than lets text on them read in any theme of one's own of named colours too."""
    for theme in themes.available().values():
        for mode in themes.MODES:
            for fg, colour, light, dark, under in themes.TINTED:
                tinted = themes.tint(theme.colours[mode], colour, light if mode == "light" else dark, under)
                assert themes.contrast(theme.colours[mode][fg], tinted) >= 4.5, (theme.id, mode, fg, colour, under)
            for fg, bg in (("star-off", "panel"), ("star-off", "bg")):
                assert themes.contrast(theme.colours[mode][fg], theme.colours[mode][bg]) >= 3, (theme.id, mode, fg, bg)
    assert {("star-off", "panel"), ("star-off", "bg")} <= set(themes.SHAPES)
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    assert ".frow.on .fn { color: var(--ink); }" in page and ".pop a.on small { color: var(--ink); }" in page
    share = f"color-mix(in srgb, var(--r-pick) {themes.READER_PICKED}%, transparent)"
    assert f".rtoclist button[aria-current] {{ background: {share};" in page
    assert ".rtoclist button[aria-current] span { color: inherit; }" in page
    assert f'.raap button[aria-pressed="true"] {{ background: {share};' in page and f'.rbmp .rbmpick button[aria-pressed="true"] {{ background: {share};' in page
    assert page.count("var(--r-pick) ") == page.count(share) + 4  # the guide's pictures aside, every tint of the pick is that one
    block = themes.inline_block()
    assert f"--picked: color-mix(in srgb, var(--l-accent) {themes.PICKED['light']}%, transparent)" in block
    assert f"--picked: color-mix(in srgb, var(--d-accent) {themes.PICKED['dark']}%, transparent)" in block
    assert ".chip small { color: inherit; margin-left: 4px; }" in page  # the ✕ of a picked filter in its chip's own text, not fainter


def test_text_too_faint_on_a_tint_or_a_star_too_faint_is_told_never_refused(tmp_path):
    """An owner's theme made before these pairs were checked is told, not left out."""
    text = birch_text().replace('ink = "#15202b"         # text', 'ink = "#66727e"         # text', 1).replace('star-off = "#818e9c"', 'star-off = "#c3ccd4"', 1)
    theme = themes.check(theme_file(tmp_path, text))
    assert any(line.startswith("[light] ink on accent at 12% over panel is") and "under 4.5:1" in line for line in theme.told), theme.told
    assert any(line.startswith("[light] star-off on bg is") and "under 3:1" in line for line in theme.told), theme.told


def test_no_colour_of_the_page_bypasses_the_themes_in_its_markup_or_its_one_font():
    """The book search's magnifier had a grey of its own (the dark theme's, about 2:1 on the light reader's field), the
    four paper swatches in Aa were a third copy of PAPERS, and the Read button set its own font, so the Clear theme's
    Atkinson Hyperlegible reached all but it. None of the page's markup names a colour now: the magnifier is in the
    reader's quieter text, the swatches are painted from PAPERS, and Read takes the theme's font."""
    page = (SRC / "catalog_page.html").read_text(encoding="utf-8")
    markup = page[page.index("</head>"):page.index('<script type="text/hylly-later"')]
    markup = re.sub(r"<script>.*?</script>", "", markup, flags=re.S)
    assert not COLOUR.findall(markup), COLOUR.findall(markup)
    assert ".rfindbox > svg { flex: none; color: var(--r-muted); }" in page and '<label class="rfindbox"><svg class="i" width="20" height="20"' in page
    assert 'for (const b of document.querySelectorAll("#raap [data-paper]")) if (PAPERS[b.dataset.paper]) b.firstElementChild.style.background = PAPERS[b.dataset.paper][0];' in page
    readbtn = page[page.index(".readbtn { position: relative;"):]
    assert readbtn[:readbtn.index("}")].count("font: 600 16px var(--font);") == 1


def test_contrast_is_wcags():
    assert round(themes.contrast("#000000", "#ffffff"), 1) == 21.0 and themes.contrast("#777777", "#777777") == 1.0
    assert round(themes.contrast("#767676", "#ffffff"), 2) == 4.54


# ---- the owner's own, [look], and the CSS -----------------------------------------------------------

def owners(text, name="pulp.toml"):
    themes.owners_dir().mkdir(parents=True, exist_ok=True)
    (themes.owners_dir() / name).write_text(text, encoding="utf-8")


def test_the_owners_own_come_after_the_shipped_and_cant_take_their_ids():
    owners(birch_text().replace('id = "birch"', 'id = "pulp"').replace('name = "Birch"', 'name = "Pulp"'))
    owners(birch_text().replace('name = "Birch"', 'name = "Not Birch"'), "fake-birch.toml")
    owners('id = "bad"\n', "bad.toml")
    said = []
    found = themes.available(told=said.append)
    assert "pulp" in found and found["birch"].name == "Birch" and "bad" not in found
    assert any("fake-birch.toml" in s and "id of its own" in s for s in said) and any("bad.toml" in s for s in said)


def test_theme_check_refuses_a_copy_that_still_has_a_shipped_theme_s_id_in_the_words_the_server_would_use(tmp_path, capsys):
    """A copy of a shipped theme with a new name and colours but its id still Hylly's was said to be usable, and then,
    copied, left out of Look with the reason only in a command's output or the server's log: the check says it now,
    as the server would. Another of the owner's with the id is said so too, unless the file would take its place."""
    from hylly import cli

    harbour = (themes.SHIPPED / "hylly.toml").read_text(encoding="utf-8").replace('name = "Hylly"', 'name = "Harbour"', 1)
    path = theme_file(tmp_path, harbour, "harbour.toml")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["theme", "check", str(path)])
    said = themes.taken(path, "hylly", True)
    assert stopped.value.code == 2 and capsys.readouterr().err.strip().endswith(f"It can't be used: {said}")
    assert said == f"The theme {path} has the id 'hylly', which Hylly theme has. It's left out: give it an id of its own."
    owners(harbour, "harbour.toml")  # copied all the same: available() leaves it out with those very words
    assert themes.available(told=(left := []).append)["hylly"].path.parent == themes.SHIPPED
    assert left == [themes.taken(themes.owners_dir() / "harbour.toml", "hylly", True)]
    path.write_text(harbour.replace('id = "hylly"', 'id = "harbour"'), encoding="utf-8")
    cli.main(["theme", "check", str(path)])  # its own id: fine, and so is a new version of it to copy over the old
    assert "Harbour (harbour) can be used" in capsys.readouterr().out
    owners(harbour.replace('id = "hylly"', 'id = "harbour"'), "harbour.toml")
    cli.main(["theme", "check", str(path)])
    assert "Harbour (harbour) can be used" in capsys.readouterr().out
    cli.main(["theme", "check", str(themes.owners_dir() / "harbour.toml")])  # the very file the server uses
    assert "Harbour (harbour) can be used" in capsys.readouterr().out
    other = theme_file(tmp_path, harbour.replace('id = "hylly"', 'id = "harbour"'), "harbour-2.toml")
    with pytest.raises(SystemExit):  # beside it under another name, one of the two would be left out
        cli.main(["theme", "check", str(other)])
    assert capsys.readouterr().err.strip().endswith(themes.taken(other, "harbour", False))
    cli.main(["theme", "check", str(themes.SHIPPED / "birch.toml")])  # a shipped one itself, as it is
    assert "Birch (birch) can be used" in capsys.readouterr().out


def test_look_picks_the_librarys_own_and_which_may_be_picked():
    found = themes.available()
    default, picked = themes.allowed(found, {"theme": "clear", "themes": ["hylly", "clear", "nothing"]}, told=(said := []).append)
    assert default == "clear" and [t.id for t in picked] == ["clear", "hylly"] and any("'nothing'" in s for s in said)
    default, picked = themes.allowed(found, {"theme": "gone", "themes": ["birch"]}, told=(said := []).append)
    assert default == "hylly" and [t.id for t in picked] == ["hylly", "birch"] and any("'gone'" in s for s in said)  # the default is always there
    every = themes.allowed(found, {"theme": themes.DEFAULT, "themes": []})[1]  # none named: all, the default first
    assert [t.id for t in every] == [themes.DEFAULT, *(i for i in shipped_ids() if i != themes.DEFAULT)]


def test_the_css_holds_each_theme_and_checked_colours_only():
    css = themes.theme_css(list(themes.available().values()))
    for theme_id in shipped_ids():
        assert f':root[data-skin="{theme_id}"], [data-pv="{theme_id}"]' in css  # and a preview of it in the Look sheet
    assert "--l-r-bar:" in css and "--d-load-fruit:" in css and "Atkinson Hyperlegible" in css
    values = re.findall(r"--[a-z-]+: ([^;]+);", css)
    assert all(re.fullmatch(r"#[0-9a-f]{6}", v) or v in themes.FONTS.values() for v in values)
    assert "<" not in css and "@import" not in css and "url(" not in css


def test_the_server_sees_a_new_theme_and_a_new_look_within_seconds(watching):
    assert [t.id for t in themes.current()[1]][0] == "hylly"
    owners(birch_text().replace('id = "birch"', 'id = "pulp"').replace('name = "Birch"', 'name = "Pulp"'))
    assert "pulp" in [t.id for t in themes.current()[1]]
    paths.settings_file().write_text('[look]\ntheme = "clear"\n', encoding="utf-8")
    assert themes.current()[0] == "clear"


# ---- the server -----------------------------------------------------------------------------------

def test_theme_css_is_for_anyone_even_from_outside(server):
    through = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}  # a tunnel on this computer, on the one port
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=through) as outside:
        answer = outside.get("/theme.css")
        assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/css") and 'data-skin="clear"' in answer.text
        assert outside.get("/api/session").status_code == 401  # and nothing else of the library's
        login = outside.get("/login")
        assert 'data-skin="birch"' in login.text and "style-src 'unsafe-inline'" in login.headers["content-security-policy"]
    assert not bell.is_file("/theme.css")


def test_the_login_page_wears_the_library_s_own_theme_on_a_browser_s_first_visit(server, watching):
    """The login page learned the library's own theme only from what the library page had kept in that browser, so a
    new device met the Hylly theme whatever [look] theme said. The server now names it on the page's root, the
    one-time link's page's too, and a theme this browser picked, or kept from the library page, still wins."""
    import json
    import shutil
    import subprocess

    from hylly import access

    paths.settings_file().write_text('[look]\ntheme = "lake"\n', encoding="utf-8")
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as browser:
        pages = [browser.get("/login").text, browser.get("/login", params={"once": access.sign_in_link("rory")}).text]
    assert all('<html lang="en" data-skin="lake">' in page for page in pages) and 'name="once"' in pages[1]
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's script runs only in a browser here")
    boot = re.findall(r"<script>(.*?)</script>", pages[0], re.S)[0]
    worn = {}
    for kept in ({}, {"hylly.look": "birch"}, {"hylly.lookhere": "clear"}, {"hylly.look": "<b>"}):
        script = (f"const kept = {json.dumps(kept)}, root = {{dataset: {{skin: 'lake'}}, style: {{setProperty() {{}}}}}};\n"
                  "const localStorage = {getItem: k => kept[k] ?? null}, document = {documentElement: root};\n"
                  f"{boot}\nprocess.stdout.write(root.dataset.skin);\n")
        worn[json.dumps(kept)] = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert list(worn.values()) == ["lake", "birch", "clear", "lake"]  # nothing kept here: the library's; else this browser's


def test_the_session_says_which_themes_and_whose(server):
    with logged_in(server, "rory") as browser:
        session = browser.get("/api/session").json()
        assert session["look"] == "hylly" and [t["id"] for t in session["looks"]][:3] == ["hylly", "birch", "blue-hour"]
        clear = next(t for t in session["looks"] if t["id"] == "clear")
        assert clear["name"] == "Clear" and clear["group"] == "calm" and clear["light"]["bg"] == "#ffffff" and len(clear["dark"]) == len(themes.KEYS)
        assert session["palette"]["light"][0] == ["White", "#fbfaf6"] and session["choose"]["page"] == "back" and session["derive"][0][0] == "accent"
        assert session["me"]["look"] == "" and session["me"]["own"] is None


def picking(browser, look, key=None):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/look", json={"look": look}, headers={"X-Personal-Key": key})


@pytest.mark.parametrize("name", ["bea", "rory", "lucy", "owen"])
def test_every_role_picks_a_theme_of_its_own(server, name):
    with logged_in(server, name) as browser:
        assert picking(browser, "birch").json()["ok"]
        assert browser.get("/api/session").json()["me"]["look"] == "birch"
    with logged_in(server, "rory" if name != "rory" else "lucy") as other:
        assert other.get("/api/session").json()["me"]["look"] == ""
    assert people.Person(name).look() == "birch" and people.Person(name).export()["look"] == "birch"


def test_a_theme_that_may_not_be_picked_is_refused_and_none_is_the_librarys_own(server, watching):
    with logged_in(server, "rory") as browser:
        assert picking(browser, "nothing").status_code == 409
        assert picking(browser, "<b>").status_code == 409
        assert picking(browser, "clear").json()["ok"]
        paths.settings_file().write_text('[look]\nthemes = ["hylly", "birch"]\n', encoding="utf-8")
        assert picking(browser, "clear").status_code == 409
        assert picking(browser, "").json()["ok"] and people.Person("rory").look() == ""
        assert picking(browser, "birch", key="wrong").status_code == 403
        assert browser.post("/api/me/look", content=b'{"look":"birch"}', headers={"X-Personal-Key": browser.get("/api/session").json()["me"]["key"]}).status_code == 415


# ---- a theme of your own ---------------------------------------------------------------------------------------------

def sides(mode, side):
    return [c for _, c in themes.PALETTE["light" if (mode == "light") == (side == "back") else "dark"]]


def test_every_named_colour_reads_on_every_one_of_the_other_side():
    for mode in themes.MODES:
        light = mode == "light"
        papers = [v for v in themes.PAPERS.values() if (themes._luminance(v[0]) > 0.5) == light]
        backs = sides(mode, "back") + [v[0] for v in papers]
        fronts = sides(mode, "front") + [c for v in papers for c in (v[1], v[3])]  # the reader's pages may be kept too
        assert all(themes.fits(mode, "page", c) for c in backs) and all(themes.fits(mode, "page-ink", c) for c in fronts)
        assert min(themes.contrast(b, f) for b in backs for f in fronts) >= 4.5
    assert min(themes.contrast(a, b) for _, a in themes.PALETTE["light"] for _, b in themes.PALETTE["dark"]) >= 6.6


def test_any_own_theme_of_named_colours_reads_well_and_keeps_what_was_picked():
    import random

    found, rnd = themes.available(), random.Random(11)
    for _ in range(400):
        base = rnd.choice(list(found.values()))
        picks = {m: {k: rnd.choice(sides(m, s)) for k, s in themes.CHOOSE.items() if rnd.random() < 0.5} for m in themes.MODES}
        own = themes.own_theme(base, picks)
        assert not own.told and own.id == themes.OWN and own.base == base.id and own.name == f"{base.name}, yours"
        assert all(own.colours[m][k] == c for m in themes.MODES for k, c in picks[m].items())
    assert themes.own_theme(found["lake"], {"light": {}, "dark": {}}).colours == found["lake"].colours  # nothing picked: the theme itself


def test_the_look_sheet_works_out_an_own_theme_as_the_server_does():
    """The page's workedOut (its Look sheet's preview) against themes.worked_out (what the server keeps and wears), under
    node: the same colours for the same picks, on every theme and in both modes, with the server's own table."""
    import json
    import random

    found, rnd, cases = themes.available(), random.Random(46), []
    for _ in range(60):
        base, mode = rnd.choice(list(found.values())), rnd.choice(themes.MODES)
        picks = {k: rnd.choice(sides(mode, s)) for k, s in themes.CHOOSE.items() if rnd.random() < 0.4}
        cases.append(({**base.colours[mode], **picks}, sorted(picks)))
    script = (f"const LOOKRULES = {json.dumps(themes.for_page(list(found.values())))};\n/* part 1 */\n"
              f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(([colours, picked]) => workedOut(colours, picked))));")
    on_the_page = in_the_page(script, (r"const hexRGB = ", r"function fitsSide\("))
    assert on_the_page == [themes.worked_out(colours, set(picked)) for colours, picked in cases]
    assert sum(page != colours for page, (colours, _) in zip(on_the_page, cases)) >= 40  # most picks change something


def test_an_own_theme_works_out_what_goes_with_its_picks():
    lake = themes.available()["lake"]
    own = themes.own_theme(lake, {"light": {"bg": "#fbd79a", "ink": "#3a1d49"}, "dark": {}})
    light = own.colours["light"]
    assert light["r-bar"] == "#fbd79a" and light["r-ink"] == "#3a1d49" and light["muted"] not in (lake.colours["light"]["muted"], light["ink"])
    assert themes.contrast(light["muted"], light["bg"]) >= 4.5 and themes.contrast(light["accent"], light["bg"]) >= 4.5
    assert own.colours["dark"] == lake.colours["dark"]  # the other mode as it was


@pytest.mark.parametrize("picks, said", [
    ({"light": {"bg": "#123848"}}, "takes a light colour"),  # a dark background in light mode
    ({"dark": {"page-ink": "#1b1916"}}, "takes a light colour"),  # dark text in dark mode
    ({"light": {"r-bar": "#ffffff"}}, "isn't one of the colours to pick"),
    ({"light": {"bg": "white"}}, "takes a light colour"),
    ({"sepia": {}}, "a light and a dark set"),
    ("lake", "a light and a dark set"),
])
def test_an_own_theme_of_the_wrong_colours_is_refused(picks, said):
    with pytest.raises(themes.NotATheme, match=re.escape(said)):
        themes.own_theme(themes.available()["lake"], picks)


def test_no_theme_file_takes_the_id_of_ones_own(tmp_path):
    with pytest.raises(themes.NotATheme, match="each person's own"):
        themes.check(theme_file(tmp_path, birch_text().replace('id = "birch"', 'id = "yours"')))
    with pytest.raises(themes.NotATheme, match="group is one of"):
        themes.check(theme_file(tmp_path, birch_text().replace('group = "calm"', 'group = "loud"')))


def owning(browser, look, own=..., key=None):
    key = key if key is not None else browser.get("/api/session").json()["me"]["key"]
    return browser.post("/api/me/look", json={"look": look, **({} if own is ... else {"own": own})}, headers={"X-Personal-Key": key})


def test_your_own_theme_is_kept_worn_and_follows_you(server):
    mine = {"base": "lake", "light": {"page": "#f3ead6", "page-ink": "#3b2f22", "page-link": "#7c4a14"}, "dark": {"bg": "#3a1d49"}}
    with logged_in(server, "rory") as browser:
        assert owning(browser, "yours").status_code == 409  # none yet
        assert owning(browser, "yours", mine).json()["ok"]
        me = browser.get("/api/session").json()["me"]
        assert me["look"] == "yours" and me["own"]["light"] == mine["light"] and me["own"]["colours"]["light"]["page"] == "#f3ead6"
        assert me["own"]["colours"]["dark"]["r-bar"] == "#3a1d49"  # what goes with it, worked out
        assert owning(browser, "birch").json()["ok"]  # another worn: yours is kept, to go back to
        assert browser.get("/api/session").json()["me"]["own"]["base"] == "lake"
        assert owning(browser, "yours").json()["ok"] and people.Person("rory").look() == "yours"
        assert owning(browser, "yours", {**mine, "light": {"bg": "#123848"}}).status_code == 409  # a dark background by day
        assert owning(browser, "yours", {**mine, "base": "nothing"}).status_code == 409
        assert owning(browser, "yours", {**mine, "css": "x"}).status_code == 409
        assert owning(browser, "", None).json()["ok"] and browser.get("/api/session").json()["me"]["own"] is None  # forgotten
    assert people.Person("rory").export()["own_look"] is None


def test_an_own_theme_whose_theme_goes_is_left_out(server, watching):
    with logged_in(server, "rory") as browser:
        assert owning(browser, "yours", {"base": "moss", "light": {"bg": "#fbd79a"}, "dark": {}}).json()["ok"]
        paths.settings_file().write_text('[look]\nthemes = ["hylly", "birch"]\n', encoding="utf-8")
        assert browser.get("/api/session").json()["me"]["own"] is None


def test_without_a_login_there_is_nowhere_to_keep_a_pick(server):
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as home:
        assert home.get("/api/session").status_code == 401  # at home too: the login page has the colours (theme.css)
        assert home.post("/api/me/look", json={"look": "birch"}).status_code == 401


def test_forgetting_someones_places_forgets_their_own_theme_too():
    rory = people.Person("rory")
    rory.pick_look("yours", {"base": "lake", "light": {"bg": "#fbd79a"}, "dark": {}})
    assert people.Person("rory").own_look()["light"] == {"bg": "#fbd79a"}
    rory.forget_places()
    assert people.Person("rory").own_look() is None and people.Person("rory").look() == ""


def test_forgetting_someones_places_forgets_their_theme_too():
    person = people.Person("rory")
    assert person.pick_look("clear") and not person.pick_look("clear")
    assert people.Person("rory").look() == "clear"
    person.forget_places()
    assert people.Person("rory").look() == ""


def test_theme_check_says_the_faintest_text_there_is_tints_included(tmp_path, capsys):
    """hylly theme check said "its text is at least 4.2:1" of a theme whose text on a tint was told at 4.12:1: the
    lowest it named went by the theme's own colours alone, rounded up. It counts the tints too now, never rounded up."""
    from hylly import cli
    text = birch_text().replace('ink = "#15202b"         # text', 'ink = "#66727e"         # text', 1)
    path = theme_file(tmp_path, text.replace('id = "birch"', 'id = "faint"'))  # its own id, or the check refuses it
    lowest = themes.lowest_text(themes.check(path).colours)
    assert lowest < min(themes.contrast(c[a], c[b]) for c in themes.check(path).colours.values() for a, b in themes.TEXT)  # a tint's
    cli.main(["theme", "check", str(path)])
    out = capsys.readouterr().out
    assert f"its text is at least {int(lowest * 100) / 100:.2f}:1 on what it sits on, tints included" in out and "4.2:1" not in out, out
