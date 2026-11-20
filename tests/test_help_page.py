"""The help page as a whole: its contents follow the page, part by part, every section leads back to them, and what it
says once, a limit or a name, agrees with the code and the library page that it describes."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from hylly import access, helppage, hyllycopy, instance, reader, rules, serve, settings, words

SOURCES = Path(serve.__file__).parent
TEMPLATE = (SOURCES / "help.html").read_text(encoding="utf-8")
PAGE = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
README = (SOURCES.parents[1] / "README.md").read_text(encoding="utf-8")
MADE = ("cmd-", "area-")  # the reference's own entries, one for each command and area: listed in the reference itself


def section(sid: str, page: str = TEMPLATE) -> str:
    return re.search(rf'<section id="{sid}">(.*?)</section>', page, re.S)[1]


def sections(page: str) -> list[tuple[str, str]]:
    return re.findall(r'<section id="([^"]+)">(.*?)</section>', page, re.S)


class Contents(HTMLParser):
    """The contents' links as [(a section, [its parts])]: a section's link in a part's list, its parts' in the list
    inside that one."""

    def __init__(self) -> None:
        super().__init__()
        self.depth, self.found = 0, []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "ul":
            self.depth += 1
        elif tag == "a":
            href = dict(attrs)["href"]
            assert href.startswith("#"), href
            if self.depth == 1:
                self.found.append((href[1:], []))
            else:
                self.found[-1][1].append(href[1:])

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul":
            self.depth -= 1


def test_the_contents_are_the_sections_and_their_parts_in_the_order_of_the_page(tmp_path):
    """The contents, kept by hand, once ran in another order than the page and named none of a section's own parts. Now
    each section is there in the page's order, with every id inside it, so a link anywhere to one has its line too."""
    page = helppage.help_page(tmp_path, generic=True)  # as the server sends it: the reference made from the commands
    contents = Contents()
    contents.feed(section("contents", page))
    inside = [(sid, [i for i in re.findall(r'(?<![\w-])id="([^"]+)"', body) if not i.startswith(MADE)])
              for sid, body in sections(page) if sid != "contents"]
    assert contents.found == inside
    assert {"guide", "bookmarks", "pageturn", "cantopen", "myfeed", "browsers"} <= {i for _, parts in inside for i in parts}


def test_every_section_leads_back_to_the_contents():
    assert [sid for sid, body in sections(TEMPLATE)
            if sid != "contents" and not body.startswith('\n<a class="up" href="#contents">')] == []


def test_the_ways_away_from_home_go_step_by_step_with_tailscale_s_command_for_each_system():
    """Serve and Funnel once came as a finished setup, the Windows path to Tailscale's command at the section's end, and
    uv's folder for commands only as Windows has it."""
    ways = section("ways")
    for part, command in (("home-network", "serve"), ("outside", "funnel")):
        text = ways[ways.index(f'<h3 id="{part}">'):]
        steps = re.search(r"<ol>(.*?)</ol>", text[:text.index("<h3", 4)], re.S)[1]
        assert f"<code>tailscale {command} --bg {{{{port}}}}</code>" in steps and 'href="#tailscale"' in steps, part
        assert "<code>hylly server restart</code>" in steps and "<code>address</code>" in steps, part
    note = re.search(r'<p class="note" id="tailscale">(.*?)</p>', ways, re.S)[1]
    assert all(system in note for system in ("Windows", "Linux", "macOS")) and ways.index(note) < ways.index('<h3 id="outside">')
    assert "tailscale.exe" not in ways.replace(note, "")  # the Windows path, said once
    assert ("<code>%USERPROFILE%\\.local\\bin</code> on Windows, <code>~/.local/bin</code> on Linux and macOS"
            in section("running"))


def test_the_reader_s_cache_is_sized_and_placed_once_and_a_cbr_comic_points_there():
    """The CBR item once said 3 GB in cache\\pages next to the settings, while the cache is one, of cache_gb, wherever
    cache_folder puts it, and an unpacked comic is held to its own limit."""
    reading = section("reading")
    cbr = re.search(r'<li id="cbr">.*?</li>', reading, re.S)[0]
    cache = re.search(r'<li id="cache">.*?</li>', reading, re.S)[0]
    assert 'href="#cache"' in cbr and "cache\\pages" not in cbr and "3 GB" not in cbr
    assert f"more than {words.size_words(reader.MAX_UNPACKED)} unpacked" in cbr
    assert f"up to <code>cache_gb</code> ({settings.DEFAULTS['reader']['cache_gb']:g} GB)" in cache and "<code>cache_folder</code>" in cache


def test_the_library_page_says_when_a_rebuild_is_needed_as_the_rules_say_it():
    """--rebuild was once only for a program change, while how authors are split and what a ZIP holds are read with
    each file, so a change to those rules needs it too."""
    rebuild = re.search(r"<li><code>--rebuild</code>.*?</li>", section("catalog"))[0]
    rules_files = section("rules-files")
    for said in ("authors are split", "what a ZIP holds (<code>[packages]</code>)"):
        assert said in rebuild and said in rules_files, said
    assert 'href="#rules-files"' in rebuild and "only need" not in rebuild


def test_the_help_names_the_filters_and_a_shelf_s_rows_as_the_page_does():
    """The help once had a Reading list filter with On my reading list, the page a group Lists with My reading list;
    and a shelf's rows went by fewer fields than the page offers."""
    table = PAGE[PAGE.index("const FACET = {"):PAGE.index("\n};", PAGE.index("const FACET = {"))]  # the filter groups, in their order
    groups = re.findall(r'^  (\w+): \{ name: "([^"]+)", plural: "[^"]+", cluster: "([^"]+)"', table, re.M)
    facets = {key: name for key, name, _ in groups}
    headings = re.findall(r'"([^"]+)"', re.search(r"^const CLUSTERS = \[(.*?)\]\.map", PAGE, re.M)[1])
    assert headings and {cluster for *_, cluster in groups} == set(headings)
    page = section("catalog")
    filters = page[page.index("<li><b>The filters</b>"):]
    filters = filters[:filters.index("<ul>")]
    for title in headings:
        group = re.search(rf"<i>{title}</i> \((.*?)\)", filters)[1]
        named = re.findall(r"<i>([^<]+)</i>", group)
        assert named == [name for _, name, cluster in groups if cluster == title], title
    mine = re.search(r'^  ls: \{.*\n    labels: \{ on: "([^"]+)" \}', table, re.M)[1]
    assert f"<i>{facets['ls']}</i> has <i>{mine}</i>" in page and "On my reading list" not in TEMPLATE.split('<section id="running">')[1]
    fields = dict(re.findall(r'(\w+): "([^"]+)"', PAGE[PAGE.index("const SHELF_FIELDS = "):PAGE.index("};", PAGE.index("const SHELF_FIELDS = "))]))
    rows = re.split(r", | or ", re.search(r"Rows go by ([^.]*)\.", section("shelves"))[1])
    assert rows == [name.lower() + (" details" if key == "miss" else "") for key, name in fields.items()]


def test_a_shop_s_cookie_is_found_in_each_browser_and_said_once():
    """F12, Application, Cookies, three times over, is Chrome's and Edge's alone; Firefox keeps cookies under
    Storage, and Safari, a browser the library is made for, in its Web Inspector."""
    body = TEMPLATE.split('<section id="running">')[1]  # the sections, past the contents
    step = re.search(r'<li id="not-logged-in">.*?</ol>', body, re.S)[0]
    for browser in ("in Chrome and Edge, F12", "in Firefox, F12", "in Safari on a Mac"):
        assert browser in step, browser
    assert body.count("F12") == 2  # in that one step
    for sid in ("storybundle", "itchio"):
        assert 'href="#not-logged-in"' in section(sid), sid


def test_what_goes_wrong_has_its_steps_for_every_system():
    """What goes wrong once had a disk check that is a Windows program only, a workaround for one kind of share, and a
    script to double-click, with nothing for Linux and macOS."""
    problems = section("problems")
    assert "CrystalDiskInfo on Windows" in problems and "<code>smartctl -a</code>" in problems and "on a NAS" in problems
    body = TEMPLATE.split('<section id="running">')[1]
    assert [line[:80] for line in body.splitlines() if re.search(r"\.cmd\b", line) and "Windows" not in line] == []


def test_reading_names_each_kind_that_opens_its_pages_first_as_the_rules_have_them():
    """A tech book, with an EPUB and a PDF, opens its PDF: meant, as its kind is better on a tablet for code and
    diagrams, but the help's list of what opens its pages first left tech books out."""
    reading = section("reading")
    others = re.search(r"anything else \(([^)]*)\) opens its CBZ, CBR or PDF first", reading)[1]
    kinds = rules.current().kinds.values()
    assert {re.sub(r"^an? ", "", w) for w in others.split(", ")} == {k.word for k in kinds if not k.text_first}
    assert [k.word for k in kinds if k.text_first] == ["book"] and "A book opens its EPUB first" in reading
    assert "<code>text_first</code>" in reading


def test_which_browser_is_said_at_the_top_of_reading_and_alike_in_the_page_and_the_readme():
    """The browsers were once said only in a dated note of the help's, and all three texts called Firefox one; Safari,
    Chrome and Edge are the ones Hylly is made for, and the others are expected to work."""
    reading = section("reading")
    line = re.search(r'<p id="browsers">(.*?)</p>', reading, re.S)[1]
    assert reading.index('id="browsers"') < reading.index("<h3")
    readme = re.search(r"\*\*Browsers:\*\* (.*)", README)[1]
    for said in (line, readme):
        assert "Hylly is made for Safari, Chrome and Edge" in said and "Safari 16.4" in said
        assert "Safari on iOS or iPadOS 16.2 or later, or a current Chrome or Edge" in said
        assert "others, such as a current firefox, should work too" in said.lower()
    joined = re.sub(r'"\s*\+\s*"', "", PAGE)  # the page's lines as written, their halves joined
    for version in ("16.2", "16.4"):
        assert (f"needs a newer browser: Safari on iOS or iPadOS {version} or later, or a current Chrome or Edge; others, "
                "such as a current Firefox, should work too.") in joined, version
    for text in (joined, README, TEMPLATE.split('<section id="running">')[1]):
        assert "Chrome, Edge or Firefox" not in text


def test_the_zoom_s_limit_is_said_as_the_reader_keeps_it():
    """The help and the guide once said the fingers and the mouse wheel zoom as far as you like; both stop at six times,
    the pinch by a 6 of its own."""
    assert re.search(r"const ZOOM_MOST = (\d+);", PAGE)[1] == "6"  # "six", below
    assert "s = Math.min(ZOOM_MOST, Math.max(1, g.s0 * apart(a, b) / g.d0));" in PAGE and "Math.min(6," not in PAGE
    guide = PAGE[PAGE.index("function guideZoom("):PAGE.index("function guideMarks(")]
    zooming = re.search(r"<li><b>Zooming a comic or a PDF:</b>.*?</li>", section("reading"))[0]
    assert guide.count("up to six times") == 2 and zooming.count("up to six times") == 2
    assert "as far as you like" not in PAGE and "as far as you like" not in TEMPLATE


def test_what_s_kept_says_the_read_ahead_hints_are_in_the_access_log_as_any_request():
    """The hints once went to the server under a number, not a name, and were kept in its memory only; each is a request
    the access log has, with the login's name, as every request but the pages' own checks."""
    item = re.search(r"<li><b>The pages drawn are not about you:</b>.*?</li>", section("kept"))[0]
    assert "drawing queue" in item and 'href="#access-log"' in item and "login's name" in item
    assert "/api/ahead" in PAGE and "/api/ahead" not in serve.Handler.UNLOGGED
    assert '<li id="access-log"><b>The access log</b>' in section("logins")


def test_licences_and_source_says_the_licence_links_the_source_it_is_given_and_each_part_s_licence(tmp_path):
    """The help once credited nobody, and named neither Hylly's licence nor where its source is. Its last section,
    Licences and source, says the licence and who holds the rights, links the source the server gives it ([server]
    source_url, Hylly's own as it comes), and each part Hylly includes with a link to its licence's text as the server
    serves it (serve.LICENCES): each once, and nothing else there; and the online sources' credits. The account menu's
    line says where its Source and Licences are."""
    from hylly import PROJECT_URL

    changed = "https://code.example.org/lantern-wolf/hylly"
    page = helppage.help_page(tmp_path, generic=True, source=changed)
    assert sections(page)[-1][0] == "licences" and "{{" not in page
    licences = section("licences", page)
    assert "<h2>Licences and source</h2>" in licences and "Copyright (C) 2026 janisc and the Hylly contributors" in licences
    assert "GNU Affero General Public License</a>, version 3 or later" in licences
    assert f'<a href="{changed}" rel="noopener noreferrer">' in licences and PROJECT_URL not in licences
    assert "<code>source_url</code> under <code>[server]</code>" in licences and "source_url" in section("server", page)
    linked = re.findall(r'href="/licences/([^"]+)"', page)
    assert sorted(linked) == sorted(serve.LICENCES) and linked == re.findall(r'href="/licences/([^"]+)"', licences)
    assert all(name in licences for name in ("Metron", "Comic Vine", "Open Library", "Google Books", "Wiktionary",
                                             "Wikipedia", "CC BY-SA", "THIRD-PARTY-NOTICES.md"))
    assert f'<a href="{PROJECT_URL}" rel="noopener noreferrer">' in section("licences", helppage.help_page(tmp_path))
    notices = (SOURCES.parents[1] / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    included = notices[notices.index("## Included"):notices.index("## Python packages")]
    holders = [h.strip().removeprefix("The ") for row in included.splitlines() if row.startswith("| [")
               for h in row.rstrip(" |").rsplit("|", 1)[1].split(";")]
    assert len(holders) >= 8 and [h for h in holders if h not in licences] == []  # each part's holders, as the notices name them
    assert "At its foot, <i>Source</i> and <i>Licences</i> (<a href=\"#licences\">" in section("catalog")
    assert 'id="sourcelink"' in PAGE and 'href="help.html#licences"' in PAGE


def test_the_help_installs_hylly_as_any_command_line_tool_with_the_versions_it_was_tested_with(tmp_path):
    """One command, from Hylly's own address, the same in the help's install, its update and the README: uv's install
    with the constraints and overrides beside it, as a tool's install takes no lock. The help is given it ({{install}}),
    so its template names no address by hand. No program folder to open a terminal in, no uv run before the commands,
    and on Windows the server stopped before the update, as it holds its libraries' files open, and started again
    after."""
    from hylly import BRANCH, INSTALL, PROJECT_URL, PYTHON, updates

    install = updates.install_command()  # the program and the two files beside it from one branch, on a Python of now
    assert install == INSTALL and install.startswith(f"uv tool install --force --python {PYTHON} git+{PROJECT_URL}@{BRANCH} --overrides https://")
    assert install.count(f"/{BRANCH}/tool-") == 2
    assert install.count("https://raw.githubusercontent.com/") == 2 and "<pre>{{install}}</pre>" in TEMPLATE
    page = helppage.help_page(tmp_path, generic=True)
    running, updating = section("running", page), section("updates", page)
    assert f"<pre>{install}</pre>" in running and f"<pre>{install}\nhylly server restart\nhylly doctor</pre>" in updating
    assert f"<pre>hylly server stop\n{install}\nhylly serve --background\nhylly doctor</pre>" in updating
    assert "os error 32" in updating and "uv tool upgrade hylly" in updating and f"   {install}\n" in README
    assert all(word not in TEMPLATE for word in ("uv run", "program folder", "{{program}}", "uv sync"))
    assert "uv run hylly" not in README.split("## For developers")[0]


def test_the_help_names_each_kind_s_folder_and_what_lives_in_it_never_a_file_beside_the_settings_file(tmp_path, monkeypatch):
    """settings.toml is the settings file itself ({{settings}}), so nothing is "beside" it: the settings go in it, the
    themes in the settings folder ({{settings_folder}}), the database in the data folder ({{data_folder}}), the pages
    drawn in the cache folder ({{cache_folder}}), the logins in theirs. The owner's help names this computer's folders,
    wherever HYLLY_SETTINGS puts the file; everyone else's names them in words. A theme of Hylly's own is found by its
    file, as hylly theme --json lists it, not in a program's folder."""
    from hylly import db, paths

    assert "beside <code>{{settings}}</code>" not in TEMPLATE and "settings.toml</code> beside" not in TEMPLATE
    # the shops' logins and the lookups' keys are in the logins folder, wherever that is; what long jobs said, as it is
    # for both ways of keeping the folders; the launchers are only in a checkout of the source
    assert TEMPLATE.count("stored only in <code>{{logins_folder}}</code>") == 4 and "stored only in <a" not in TEMPLATE
    assert "in <code>job-output</code> in the state folder, which the server's walls hide" in section("remote")
    assert "outside that folder, which the walls let the server write" in section("remote") and "scripts\\" not in TEMPLATE
    for value in ("settings_folder", "data_folder", "cache_folder", "logins_folder"):
        assert "{{" + value + "}}" in TEMPLATE, value
    assert "<code>[reviews]</code>" in section("stars") and "in <code>{{settings}}</code>: <code>[reviews]</code>" in section("stars")
    assert "<code>[look]</code> in <code>{{settings}}</code>" in section("themes") and "hylly theme --json" in section("themes")
    assert "the <code>themes</code> folder in <code>{{settings_folder}}</code>" in section("themes")
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "elsewhere" / "settings.toml"))  # the file apart from its folder
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "home"))
    owners = helppage.help_page(tmp_path / "library")
    assert f"<code>hylly.db</code> in <code>{db.file().parent}</code>" in owners and db.file().parent != paths.settings_file().parent
    assert f"in <code>{paths.place('cache')}</code>, up to <code>cache_gb</code>" in owners
    assert f"<code>themes</code> folder in <code>{paths.config_dir()}</code>" in owners
    places = ", ".join(f"{p['name']} <code>{p['folder']}</code>" for p in paths.places())  # each by its name, once
    assert f"<td>{places}</td>" in owners
    words = helppage.help_page(tmp_path / "library", generic=True)
    assert "<code>hylly.db</code> in <code>the data folder</code>" in words and str(tmp_path) not in words
    assert "<td>on the server computer, each as <code>hylly settings places</code> lists it there</td>" in words
    assert not re.search(r"the (settings|data|state|cache|logins) folder <code>the \1 folder", words)  # never said twice


def test_the_limits_the_help_names_are_said_as_their_true_size_in_thousands():
    """The limits Hylly keeps by itself count in 1,024s, as they always have, so the help says each as words.size_words
    says it (1.1 GB, not "1 GB"); the defaults of the size settings, which count in thousands, as they are."""
    said = words.size_words
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    js = {name: int(n) * 1048576 for name, n in re.findall(r"\b(SHARE_MAX|BOOK_WHOLE) = (\d+) \* 1048576", page)}
    js |= {name: int(n) * 1_000_000 for name, n in re.findall(r"\b(WHOLE_MOST|WHOLE_MOST_TOUCH) = (\d+)e6\b", page)}  # in thousands
    least, most = reader.KEEP_DISK_FREE
    for words_there in (f"beyond {said(instance.LOG_LIMIT)} moves the file", f"under about {said(2 * access.LOG_LIMIT)}: "
                        f"{said(access.LOG_LIMIT)}, then one older file", f"{said(least)} at least, {said(most)} at most",
                        f"at least {said(least)}, and at most {said(most)}", f"A file over {said(js['SHARE_MAX'])}",
                        f"an EPUB over {said(js['BOOK_WHOLE'])} is read where it lies",
                        f"up to {said(js['WHOLE_MOST'])} on a computer and {said(js['WHOLE_MOST_TOUCH'])} on a phone or tablet:",
                        f"more than {said(reader.MAX_UNPACKED)} unpacked", f"at most the last {said(hyllycopy.ON_DISK_EVERY)} of a file",
                        f"text file over {settings.DEFAULTS['devices']['image_heavy_mb']} MB",
                        f"under {settings.DEFAULTS['ereader_feed']['max_mb']} MB by default"):
        assert words_there in TEMPLATE, words_there
    assert TEMPLATE.count(f"and over {said(hyllycopy.FAR_MORE)}, so a") == 2  # a copy's and a backup's asking first
