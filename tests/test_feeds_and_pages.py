"""The feeds and the pages, as far as Python can see them, each defect found there kept from coming back. How the page
behaves in a browser needs a browser to check."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from hylly import catalog, humble, opds, paths, serve, settings, shops
from hylly import download as dl
from hylly.library import Catalog, Device, ereader_feed

from helpers import build_library, damage_a_mobi, in_the_page, locked_epub


SOURCES = Path(serve.__file__).parent


@pytest.fixture
def running(library, serving):
    _, client = serving(library)
    return client, library


def test_a_feed_s_start_and_search_lead_there_from_any_of_its_pages(tmp_path):
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    shelf = Catalog(tmp_path)
    device = ereader_feed()
    for url, base in (("/opds/src/humble?page=0", "/opds"), ("/opds/new", "/opds"), ("/opds/added", "/opds"), ("/opds/search?q=a", "/opds"),
                      ("/ereader/opds/new", "/ereader/opds")):
        status, _, body = opds.answer(shelf, url, device if base != "/opds" else None)
        assert status == 200
        assert f'<link rel="start" href="{base}" ' in body and f'<link rel="search" href="{base}/opensearch.xml" ' in body


def test_recently_added_holds_a_week_of_what_came_the_newest_first():
    now = datetime(2026, 11, 6, 12, 0, 0)
    ago = lambda days, hours=0: (now - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")  # noqa: E731
    items = [{"id": str(n), "t": t, **({"nw": nw} if nw else {})}
             for n, (t, nw) in enumerate((("Beta", ago(1)), ("alpha", ago(1)), ("Newest", ago(0, 1)), ("Six days", ago(6, 23)),
                                          ("Too old", ago(7, 1)), ("Always here", None), ("Odd", 5)))]
    assert [i["t"] for i in opds.recently_added(items, now)] == ["Newest", "alpha", "Beta", "Six days"]
    assert opds.recently_added([], now) == []


class Shelf:
    """A catalog's stand-in, with what the feeds take of one: its titles, and their sources' names (the shops')."""

    def __init__(self, items):
        self.items = items
        self.labels = shops.labels()

    def get(self):
        return self.items


def came_today(title, **more):
    return {"id": title, "t": title, "k": "book", "f": [], "nw": datetime.now().isoformat(timespec="seconds"), **more}


def test_the_feeds_start_with_recently_added_and_say_how_many():
    shelf = Shelf([came_today("One"), came_today("Two"), {"id": "old", "t": "Old", "k": "book", "f": []}])
    status, kind, body = opds.answer(shelf, "/opds")
    assert status == 200 and "kind=navigation" in kind
    assert body.index("Recently added") < body.index("Recently bought") and "2 titles from the last 7 days" in body
    assert 'href="/opds/added"' in body
    status, kind, body = opds.answer(shelf, "/opds/added")
    assert status == 200 and "kind=acquisition" in kind and "Recently added (2)" in body
    assert "<title>One</title>" in body and "<title>Two</title>" in body and "<title>Old</title>" not in body
    assert "1 title from" in opds.answer(Shelf([came_today("One")]), "/opds")[2]
    assert "0 titles from" in opds.answer(Shelf([]), "/opds")[2]  # the group stays where it is, also on a quiet week


def test_one_title_is_one_title_in_every_count_of_the_feeds():
    """A source, kind, game, genre or tag with one title says "1 title", as the lists and shelves always did."""
    shelf = Shelf([came_today("Lantern Wolf", src="humble", g=["Tides"], gm=["Moonwake"], tg=["Knitting"])])
    for url in ("/opds", "/opds/kind", "/opds/game", "/opds/genre", "/opds/tag"):
        body = opds.answer(shelf, url)[2]
        assert "1 titles" not in body and re.search(r"<content type=\"text\">1 title</content>", body), url
    two = Shelf([came_today("Lantern Wolf", g=["Tides"]), came_today("Moonwake", g=["Tides"])])
    assert "<content type=\"text\">2 titles</content>" in opds.answer(two, "/opds/genre")[2]


def test_an_e_reader_s_recently_added_holds_only_what_suits_it():
    device = ereader_feed()
    shelf = Shelf([came_today("For it", f=[["humble/for-it.epub", "epub", 1000]]), came_today("Not for it", k="comic", f=[["humble/not.cbz", "cbz", 1000]])])
    root = opds.answer(shelf, "/ereader/opds", device)[2]
    assert 'href="/ereader/opds/added"' in root and "1 title from" in root
    assert "Recently added (1)" in opds.answer(shelf, "/ereader/opds/added", device)[2]


def test_a_title_that_came_after_the_first_catalog_is_in_recently_added(tmp_path):
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    assert "Recently added (0)" in opds.answer(Catalog(tmp_path), "/opds/added")[2]  # the first catalog: nothing is news
    (tmp_path / "comics").mkdir(exist_ok=True)
    (tmp_path / "comics" / "Fresh Comic 1.cbz").write_bytes(b"not much of a comic")
    catalog.build(tmp_path, log=lambda s: None)
    body = opds.answer(Catalog(tmp_path), "/opds/added")[2]
    assert "Recently added (1)" in body and "Fresh Comic" in body


def test_a_series_numbered_by_more_than_a_number_goes_by_its_titles():
    issue = lambda t, n: {"id": t, "t": t, "k": "comic", "s": "A Magazine", "n": n, "f": []}  # noqa: E731
    shelf = Shelf([issue("A Magazine (1978-02)", "1978-02"), issue("A Magazine (1977-09)", "1977-09"), issue("A Magazine (1978-01)", "1978-01")])
    body = opds.answer(shelf, "/opds/kind/comic")[2]
    assert body.index("(1977-09)") < body.index("(1978-01)") < body.index("(1978-02)")
    shelf = Shelf([issue("Issue 94", "94"), issue("Issue 93b", "93b"), issue("Issue 93", "93"), issue("Issue 9", "9"), issue("The yearbook", "")])
    body = opds.answer(shelf, "/opds/kind/comic")[2]
    assert body.index("The yearbook") < body.index("Issue 9<") < body.index("Issue 93<") < body.index("Issue 93b") < body.index("Issue 94")


def test_a_feed_s_next_page_stays_in_the_feed(monkeypatch):
    monkeypatch.setattr(opds, "PAGE", 1)
    items = [{"id": str(n), "t": f"Title {n}", "k": "book", "f": []} for n in range(3)]
    body = opds.paged("/opds/src/humble?page=1", "All", items, 1)
    assert 'rel="next" href="/opds/src/humble?page=2"' in body and 'rel="previous" href="/opds/src/humble?page=0"' in body
    assert '<link rel="start" href="/opds" ' in body


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "", "²", "9" * 5000])
def test_a_query_string_can_hold_anything(running, value):
    client, _ = running
    assert client.get("/opds/new", params={"page": value}).status_code == 200
    assert client.get("/api/edits", params={"n": value}).status_code == 200  # this computer, as the owner
    assert client.get("/ereader/opds/new", params={"page": value}).status_code == 200
    assert client.get("/opds/added", params={"page": value}).status_code == 200


def test_a_next_page_leaves_a_bad_page_number_behind():
    items = [{"id": str(n), "t": f"Title {n}", "k": "book", "f": []} for n in range(opds.PAGE + 1)]
    body = opds.paged("/opds/new?page=abc&x=1", "All", items, 0)
    assert 'rel="next" href="/opds/new?x=1&amp;page=1"' in body


def test_a_server_serves_the_page_and_help_of_its_own_version(running):
    client, root = running
    for name in ("index.html", "help.html"):
        (root / "catalog" / name).write_text("<!doctype html><p>From an older version</p>", encoding="utf-8")
    page = client.get("/catalog/index.html")
    assert page.status_code == 200 and page.content == (SOURCES / "catalog_page.html").read_bytes()
    scripts = serve.LATER.findall(page.text)  # the page's own script too, which its first one runs
    assert len(re.findall(r"'sha256-", page.headers["content-security-policy"])) == len(scripts) >= 2  # the old-browser check too
    helps = client.get("/catalog/help.html")
    assert helps.status_code == 200 and "From an older version" not in helps.text and "How to run the commands" in helps.text
    assert "{{" not in helps.text


def test_the_page_the_server_shows_only_exists_after_a_catalog_build(tmp_path, log_in):
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            assert client.get("/catalog/index.html").status_code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_humble_s_summary_counts_the_partial_files_where_they_are(tmp_path):
    (tmp_path / "humble" / "A Bundle").mkdir(parents=True)
    (tmp_path / "humble" / "A Bundle" / "a.epub.part").write_bytes(b"x")
    lines, said = humble.summary([], dl.Manifest(tmp_path, "humble"), tmp_path)
    assert re.search(r"[Pp]artial files?:? *1", "\n".join(lines)), lines
    assert said["partial_files"] == 1


def test_the_page_asks_for_the_foliate_it_comes_with():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    asked = re.search(r'const FOLIATE = "/foliate/([0-9a-f]+)"', page)[1]
    noted = re.search(r"\b([0-9a-f]{40})\b", (SOURCES / "foliate" / "README.md").read_text(encoding="utf-8"))[1]
    assert noted.startswith(asked)  # or browsers would keep the old files, cached for a year


def test_an_old_browser_is_told_before_the_page_could_fail():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert page.index("needs a newer browser") < page.index("<style>") < page.index("const RD = ")
    assert "=>" not in page[page.index("<title>"):page.index("<style>")].split("</script>")[0]  # nothing an old browser can't parse


def test_opened_from_the_folder_the_page_says_where_it_comes_from_and_reads_no_further():
    """The page comes from the library's server. Opened from the folder (file:), it shows one line saying what to
    run, before anything else of it is read: the rest of the page and its scripts never come (plaintext), nor its
    data.js, which the head's script asks for at once, before the style sheets, only from a server."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    body = page[page.index("<body>"):]
    first = re.match(r"<body>\n<script>(.*?)</script>", body, re.S)
    assert first and "if (!/^https?:$/.test(location.protocol)) document.write(" in first[1] and "<plaintext hidden>" in first[1]
    said = re.sub(r'"\s*\+\s*"', "", first[1])  # the line as written, its two halves joined
    assert "This page comes from the library's server: run <code>hylly serve</code> and open the address it shows." in said
    head = page[:page.index("</head>")]
    asks = [m for m in re.finditer(r"<script>(.*?)</script>", head, re.S) if 'data.src = "data.js"' in m[1]]
    assert len(asks) == 1 and asks[0][1].count("\nif (/^https?:$/.test(location.protocol)) (() => {\n") == 1
    assert head.index("needs a newer browser") < asks[0].start() < head.index('<link rel="stylesheet"')
    assert "tags.js" not in page and page.count("location.protocol") == 2  # nothing else in it is for a page opened from disk


def test_a_title_s_files_go_into_its_details_escaped():
    """A file's path and size are the catalog's data like any other: escaped where they go into the details' markup."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    files = page[page.index("  const files = (it.f || []).map(([p, e, s]) => {"):page.index("  const shop = SHOPS[it.src]")]
    assert 'href="../${esc(p)}${DL}" data-file data-size="${esc(s)}">${esc(parts[parts.length - 1])}</a>' in files
    assert not re.findall(r"\$\{[pse]\}", files)  # none of the three goes in as it is


def test_numbers_from_the_server_or_this_browser_s_storage_go_into_the_markup_through_esc_or_number():
    """Everything the page puts into its markup goes through esc(), or Number() for a number, with no exceptions to
    remember: the stars (the outbox's, which anything in this browser could have written, count only as a whole 1 to
    5 or 0, wherever the outbox is read for them), the changes' count, a question's id, a book's contents and a PDF's
    outline."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")

    def part(start: str) -> str:
        at = page.index(start)
        return page[at:page.index("\n}", at)]

    assert "const wholeStars = n => Number.isInteger(n) && n >= 0 && n <= 5 ? n : null;" in page
    assert "const n = wholeStars((p || {}).n);" in part("function outboxStars(")
    assert "outboxStars(found);" in part("async function syncStars(")
    assert "outboxStars(fresh, it.id);" in part("async function loadReviews(")
    assert set(re.findall(r"setMine\(\w+, [^,]+, ([^)]+)\)", page)) == {"n", "RF.stars", "0"}  # never an outbox entry's own n
    stars = part("function starsHtml(")
    assert "${esc(name)} ${Number(all[name])}" in stars and "you ${Number(mine)}" in stars
    assert not re.findall(r"\$\{(?:all\[name\]|mine)\}", stars)
    line = part("function starLine(")
    assert "Math.round(Number(stars))" in line and "repeat(stars)" not in line
    changes = part("async function openChanges(")
    assert "${titles(e.count, Number(e.count))}" in changes and "${e.count}" not in changes
    assert 'data-use="${esc(q.id)}"' in part("async function titleQuestions(")
    toc = part("function tocRows(")
    assert 'data-page="${Number(page)}"' in toc and "<span>${Number(page) + 1}</span>" in toc
    assert "<span>${esc(loc)}</span>" in toc
    assert not re.findall(r"\$\{(?:page|page \+ 1|loc|level)\}", toc)


def test_reviews_reports_and_both_queues_renew_the_page_s_key_after_a_restart():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    for path, personal in (("/api/me/review", True), ("/api/me/report", True), ("/api/reviews/check", False), ("/api/reports/decide", False)):
        call = re.search(rf'sendChange\("{path}", \{{[^{{}}]*\}}, (true|false), "Not \w+"\);', page)
        assert call and (call[1] == "true") == personal, path  # the personal key for one's own, the editors' for the queues
    assert re.findall(r'"X-(?:Server|Personal)-Key": ', page) == ['"X-Personal-Key": ']  # the outbox's, which renews its own
    sending = page[page.index("async function sendKeyed("):page.index("async function keyRenewed(")]
    assert "r.status === 403 && again && await keyRenewed(personal)" in sending
    change = page[page.index("async function sendChange("):page.index("async function postJson(")]
    assert "await sendKeyed(path, body, personal)" in change  # the one helper all four go through


def test_the_list_asks_afresh_after_each_batch_whether_the_marker_is_still_in_reach():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    more = page[page.index("function more() {"):]
    more = more[:more.index("\n}")]
    assert more.index("state.shown += next.length;") < more.index("moreSeen.unobserve(mark);") < more.index("moreSeen.observe(mark);")


def test_a_browser_without_color_mix_is_told_as_the_page_s_colours_need_it():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    check = page[page.index("<title>"):page.index("<style>")].split("</script>")[0]
    assert "color-mix(" in page[page.index("<style>"):]  # the bars' and the chosen rows' backgrounds, with nothing to fall back on
    assert 'CSS.supports("color", "color-mix(in srgb, red, blue)")' in check
    oldest = "Safari on iOS or iPadOS 16.2 or later"  # where color-mix() came; in Chrome, 111: any current one
    assert oldest in check
    for said in (SOURCES.parents[1] / "README.md", SOURCES / "help.html"):
        assert oldest in said.read_text(encoding="utf-8"), said.name


def test_touch_screens_get_text_fields_ios_won_t_zoom_into():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert "input, select, textarea { font-size: max(16px, 1em) !important; }" in page  # !important: others are more specific


def test_the_filters_button_has_a_name_on_a_phone_too():
    """Up to 699 px the word Filters is hidden, so the button says it to a screen reader itself, with the count of
    filters ticked when there are some, as the Manage button says its own."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert 'aria-label="Filters"' in re.search(r'<button id="filtersbtn"[^>]*>', page)[0]
    facets = page[page.index("function renderFacets() {"):page.index("function renderDrill() {")]
    assert 'setAttribute("aria-label", chosen.length ? `Filters: ${chosen.length} ticked` : "Filters")' in facets


def test_a_row_s_spacer_is_its_own_class_not_the_guide_s_tall_row():
    """.grow is the reader guide's bottom row, 68 px tall; your own review's top row used it as a spacer too."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert ".spacer { flex: 1; }" in page and 'style="flex:1"' not in page
    assert re.findall(r'class="grow"', page) == ['class="grow"'] and '<div class="grow"><span class="gsb">' in page  # the guide's alone
    assert '${badge}<span class="spacer"></span><button class="btn" data-review-edit>Edit</button>' in page


def page_style() -> str:
    """The library page's own style sheet, without its comments."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", re.search(r"<style>(.*?)</style>", page, re.S)[1], flags=re.S)


def css_rules(style: str, media: str = "") -> list[dict]:
    """The rules of a style sheet as a browser's CSSOM has them, for the page's script under node: each with its
    selector and its position, and a media block with its rules inside."""
    rules, at = [], 0
    while (opening := style.find("{", at)) >= 0:
        head = style[at:opening].strip()
        if head.startswith("@media"):
            depth, end = 1, opening + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(style[end], 0)
                end += 1
            rules.append({"media": {"mediaText": head[len("@media"):].strip()}, "cssRules": css_rules(style[opening + 1:end - 1])})
        else:
            end = style.index("}", opening) + 1
            body = style[opening + 1:end - 1]
            position = re.search(r"(?:^|;)\s*position:\s*([\w-]+)", body)
            rules.append({"selectorText": head, "style": {"position": position[1] if position else ""}})
        at = end
    return rules


def test_the_page_s_layers_are_named_once_in_their_order_and_a_dialog_goes_by_them():
    """Thirteen layers of the page were bare numbers across its style sheet, and topDialog() and overBook() read them
    to choose which dialog is on top and so what goes inert behind it: a wrong one changes what the keyboard reaches,
    not only the look. They're named once on :root, lowest first, and every part of the page takes its own by name;
    a bare number is left only for what lies inside one part (the guide, the word box, the Look sheet's head)."""
    style = page_style()
    named = dict(re.findall(r"--z-([\w-]+): (\d+);", style))
    order = ["head", "details", "shade", "filters", "tip", "menu", "modal", "tagger", "offline", "look", "reader", "over-reader", "toast", "guide"]
    assert list(named) == order and [int(named[n]) for n in order] == sorted(int(n) for n in named.values())
    for selector, z in re.findall(r"([^{}]+)\{[^{}]*?\bz-index: ([^;]+);", style):
        inside = re.match(r"\s*(?:\.rguide[ .]\S|\.rlook\s|\.lshead\s|::view-transition)", selector)
        assert z.isdigit() if inside else z[:8] == "var(--z-" and z[8:-1] in named, (selector.strip(), z)
    # a slow answer's dialog opens behind the reader; the lists, opened from it, over it (overBook)
    assert int(named["modal"]) < int(named["tagger"]) < int(named["reader"]) < int(named["over-reader"]) < int(named["toast"])
    assert "#listpick { z-index: var(--z-over-reader); }" in style and "#reader { position: fixed; inset: 0; z-index: var(--z-reader);" in style


def test_a_phone_s_limits_are_the_page_s_own_and_the_script_reads_them_from_there():
    """A phone held upright is up to 699 px wide and one on its side up to 499 px high: the style sheet said so, the
    script again in numbers of its own (699 px, and 500 px for the docked filters), and the sideways tiles went by
    500 px, so a window exactly 500 px high was a phone on its side to them and not to the rest. The script reads both
    limits now from the page's own style sheet, where the header stops staying in view, and every rule that goes by a
    window's height says the same. Another sheet is read only when the page's can't be found, so an extension's can't
    decide them; a sheet it can't read, or a rule with a media list and no rules of its own, is passed over; and with
    none found the limits it knows apply (PHONE_LIMITS), which say what the style sheet says."""
    style = page_style()
    assert re.findall(r"@media \(max-(width|height): (\d+)px\) \{\s*header \{ position: static; \}", style) == [("width", "699"), ("height", "499")]
    assert set(re.findall(r"@media[^{]*-height: (\d+)px", style)) == {"499"}
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    script_part = page[page.index('<script type="text/hylly-later"'):]
    assert not re.search(r"\b(?:699|500)px", script_part)
    assert re.search(r'^const PHONE_LIMITS = \{ "max-width": (\d+), "max-height": (\d+) \};', script_part, re.M).groups() == ("699", "499")
    script = """
const asked = [], matchMedia = q => (asked.push(q), { media: q });
const header = { selectorText: "header", style: { position: "static" } };
const THEIRS = { cssRules: [{ media: { mediaText: "(max-width: 999px)" }, cssRules: [header] }, { media: { mediaText: "(max-height: 999px)" }, cssRules: [header] }] };
const UNREAD = { get cssRules() { throw new Error("SecurityError"); } };
const ODD = { cssRules: [{ media: { mediaText: "(max-width: 699px)" } },  // an @import's media list first, with no rules of its own
  { media: { mediaText: "(max-width: 650px)" }, cssRules: [header] }, { media: { mediaText: "(max-height: 450px)" }, cssRules: [header] }] };
const out = {};
for (const [name, own, sheets] of [["the page's", OWN, [THEIRS, OWN]], ["the page's not found", null, [UNREAD, ODD]],
                                   ["none in the page's", { cssRules: [] }, [THEIRS]], ["none at all", null, []], ["no styles", undefined, null]]) {
  globalThis.document = own === undefined ? {} : { querySelector: s => s === "head > style" && own ? { sheet: own } : null, styleSheets: sheets };
  asked.length = 0;
  /* part 1 */
  out[name] = [...asked, pageLimit("max-width"), pageLimit("max-height")];
}
process.stdout.write(JSON.stringify(out));
""".replace("const OWN", "").replace("OWN", "{ cssRules: " + json.dumps(css_rules(style)) + " }")
    out = in_the_page(script, (r"const PHONE_LIMITS = ", r"const SIDEWAYS = "))
    before = ["(min-width: 800px) and (min-height: 500px)", "(max-width: 699px)", 699, 499]  # as the script had them
    assert out == {"the page's": before, "the page's not found": ["(min-width: 800px) and (min-height: 451px)", "(max-width: 650px)", 650, 450],
                   **{name: before for name in ("none in the page's", "none at all", "no styles")}}


def test_a_field_s_look_and_the_count_badges_are_each_written_once():
    """The look of a text field (the page's font, a thin line, rounded corners, the page's background) was spelled out
    in ten rules, and the three count badges in three that differed in their colour alone: a change to either had to
    be made in up to ten places. Each is one rule now, and the rest say only what differs."""
    style = page_style()
    line = "border: 1px solid var(--line)"
    fields = [s.strip() for s, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style) if line in body and re.search(r"\b(input|textarea|select)\b|#fdq|#q\b|\.toggle", s)]
    assert fields == ["select, .toggle, #q, :where(.box, .sheet, #fpanel) :where(input[type=text], input[type=search], textarea)"]
    assert re.findall(r"(?m)^(:where\(\.box, \.sheet, #fpanel\)[^{]*)\{ background: var\(--bg\); \}", style)
    badges = re.findall(r"(?m)^(\.(?:n|count|g)badge[^{]*)\{([^}]*)\}", style)
    assert [s.strip() for s, _ in badges] == [".nbadge, .countbadge, .gbadge", ".countbadge", ".gbadge"]
    assert [b.strip() for _, b in badges[1:]] == ["margin-left: 8px; vertical-align: 1px;", "background: var(--chip); color: var(--accent);"]


def test_each_icon_is_drawn_once_and_shown_by_its_name():
    """The page's icons were pasted inline 57 times, the magnifier four times and the close cross and the chevron three,
    each with the same six attributes, and the script held the reader's once more for the guide. Each is a <symbol>
    now, drawn once in the hidden <svg> at the top of the body and shown by a <use> of it in an <svg class="i">, whose
    look is one rule; every icon shown is there, and no drawing of the page's repeats one."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    sprite = re.search(r'<svg hidden aria-hidden="true">.*?</svg>\n', page, re.S)[0]
    symbols = dict(re.findall(r'<symbol id="i-([\w-]+)" viewBox="0 0 24 24">(.*?)</symbol>', sprite))
    assert len(symbols) == sprite.count("<symbol ") and len(set(symbols.values())) == len(symbols)
    shown = set(re.findall(r'<use href="#i-([\w-]+)"', page)) | set(re.findall(r'\bicon\("([\w-]+)"', page))
    assert shown == set(symbols), shown ^ set(symbols)
    markup = page[page.index("<body>"):page.index('<script type="text/hylly-later"')].replace(sprite, "")
    icons = re.findall(r"<svg\b([^>]*)>(.*?)</svg>", markup, re.S)
    assert len(icons) > 50 and all(a.startswith(' class="i" width="') and re.fullmatch(r'<use href="#i-[\w-]+"( stroke-width="[\d.]+")?/>', d) for a, d in icons)
    drawn = [d for _, d in re.findall(r"<svg\b([^>]*)>(.*?)</svg>", page.replace(sprite, ""), re.S)]
    assert not [d for d in drawn if d in symbols.values() or any(shape in symbols.values() for shape in re.findall(r"<path [^>]*/>", d))]
    assert ".i { fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }" in page


def test_coming_back_asks_again_in_one_place_and_each_kind_waiting_is_read_by_one_helper():
    """Coming back on screen and coming back to the window from another each had a list of what to ask the server
    again, not the same two lists, and 'not more often than every 20 seconds' was written five times, three of them as
    a bare 20000. backOnScreen() names it all, for both; SYNC_EVERY is how often; the outbox's changes of one
    kind come from waiting(), and the outbox is written in one place, so a change can't be kept one way here and
    another there."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert 'document.addEventListener("visibilitychange", () => document.visibilityState === "visible" ? backOnScreen() : sendPlaces());' in page
    assert page.count('addEventListener("focus", backOnScreen);') == 1 and page.count('addEventListener("focus"') == 1
    assert "const SYNC_EVERY = 20000;" in page and page.count("20000") == 1 and page.count("< SYNC_EVERY") == 6  # and backOnScreen
    assert "PLACE_SYNC" not in page and "STARS_SYNC" not in page
    for sync, kind in (("async function syncLists(", "list:"), ("async function syncBookmarks(", "bm:"), ("async function syncShelves(", "shelf:")):
        part = page[page.index(sync):page.index("\n}", page.index(sync))]
        assert f'waiting("{kind}")' in part and "outbox()" not in part, sync
    assert page.count("localStorage.setItem(`hylly.outbox.") == 1 and "store(`hylly.outbox." not in page  # keepOutbox alone


def test_the_session_s_start_catches_only_an_answer_that_didn_t_come():
    """No server, or an answer it can't read, leaves the page read only; an error in any step after that shows in the
    console instead of quietly skipping the rest. The reader's wait for the fonts' say ends either way."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    start = page[page.index("async function startSession() {"):]
    lines = start[:start.index("\n}\n")].split("\n")
    assert [line for line in lines if re.match(r"  (try|\} catch|\} finally)\b", line)] == ["  try {", "  } catch {", "  } finally {"]
    assert lines[lines.index("  try {") + 1] == '    s = await sessionAnswer();'  # asked for beside the titles
    assert lines[lines.index("  } finally {") + 1].startswith("    fontsAreKnown();")
    assert lines[lines.index("  } catch {") + 1].startswith("    return drawStart();")  # drawn as they are, read only
    assert page.count("startSession();") == 1 and "startEditing" not in page


def test_the_editing_names_say_what_they_do_now_and_the_markup_ids_stay():
    """Names newcomers search by. The dialog's ids and the kf key stay, as the markup, links and saved shelves have them."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    for gone in ("backToSeries", "wireTagEditing", "openTagger", "refreshTagger", "function report(", "reportFix", "let tagger"):
        assert gone not in page, gone
    for name in ("function backFromTitle()", "function wireEditing(", "function openBulkEdit(", "let BULK = ", "function sayTagChange(", "function sayFix("):
        assert name in page, name
    assert all(f'id="{i}"' in page for i in ("tagger", "tagok", "tagfield")) and '  kf: { name: "E-reader feed",' in page
    top = page.index("// ---- text normalisation")  # used all through the page, so at its top, not under editing
    assert page.index("const $ = id =>") < top and page.index("const titleById = ") < top


def test_the_page_s_small_helpers_are_written_once_storage_a_title_s_ids_and_the_plural():
    """Two pairs of storage helpers did the same, two maps found a title by any of its ids, and "title(s)" was spelled out
    seventeen times. Once each now: what's kept here is JSON, a default when there's nothing, nothing readable or no
    storage, and nothing written while the page is leaving; a title's ids are its own and then its copies'; and a count
    of titles is said one way, as the place it's written had it."""
    script = """
const STORE = new Map(), closed = [];
let OFF = false, FULL = false, leaving = false;
const localStorage = {  // OFF: storage turned off; FULL: it refuses a write
  getItem(k) { if (OFF) throw new Error("SecurityError"); return STORE.has(k) ? STORE.get(k) : null; },
  setItem(k, v) { if (OFF || FULL) throw new Error("QuotaExceededError"); STORE.set(k, String(v)); } };
const document = { getElementById: id => ({ classList: { remove: c => closed.push([id, c]) } }) };
const ITEMS = [{ id: "h:novel", ids: ["h:old", "s:7"] }, { id: "s:9" }];
/* part 1 */
/* part 2 */
/* part 3 */
const out = {};
out.none = [stored("hylly.folded", null), stored("hylly.continue.tab", ""), stored("hylly.filters.upright", true)];
store("hylly.folded", ["g", "tg"]); store("hylly.filters.upright", false); store("hylly.name", "Moonwake Press Library");
out.kept = [STORE.get("hylly.folded"), stored("hylly.folded", null), stored("hylly.filters.upright", true), stored("hylly.name", "")];
STORE.set("hylly.name", "Lantern Library");  // as an older page kept it, as it was: not JSON, so the default until the next session
STORE.set("hylly.papers", "null");
out.unreadable = [stored("hylly.name", ""), stored("hylly.papers", {})];
FULL = true; store("hylly.folded", ["k"]); FULL = false;
leaving = true; store("hylly.folded", ["rd"]); leaving = false;
out.notWritten = STORE.get("hylly.folded");
OFF = true; store("hylly.folded", ["k"]);
out.off = [stored("hylly.folded", "none"), stored("hylly.filters.upright", true)];
out.ids = [idsOf(ITEMS[0]), idsOf(ITEMS[1])];
out.found = [titleById("h:novel") === ITEMS[0], titleById("s:7") === ITEMS[0], titleById("s:9") === ITEMS[1], titleById("s:8")];
out.titles = [titles(0), titles(1), titles(2), titles(1234, "1,234"), titles(1, "1")];
closeDialog("tagger");
out.closed = closed;
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const \$ = id =>", r"// Served by"), (r"// This browser's storage, as JSON", r"const ITEMS = "),
                      (r"const idsOf = ", r"const SHOPS = "))
    assert out["none"] == [None, "", True]
    assert out["kept"] == ['["g","tg"]', ["g", "tg"], False, "Moonwake Press Library"]
    assert out["unreadable"] == ["", {}]
    assert out["notWritten"] == '["g","tg"]'  # a full storage, or the page on its way out: what was there stays
    assert out["off"] == ["none", True]  # turned off: nothing read, and nothing thrown
    assert out["ids"] == [["h:novel", "h:old", "s:7"], ["s:9"]]
    assert out["found"] == [True, True, True, None]
    assert out["titles"] == ["0 titles", "1 title", "2 titles", "1,234 titles", "1 title"]
    assert out["closed"] == [["tagger", "open"]]
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    for gone in ("const remember", "const recall", "BYID", "endWayBack"):
        assert gone not in page, gone
    script = page[page.index('id="pagescript"'):]
    assert script.count("document.getElementById(") == 1 and script.count('title${n === 1 ? "" : "s"}') == 1  # $'s own, titles' own


def test_the_many_titles_dialog_ticks_every_title_in_one_loop_whatever_the_change():
    """Which titles a change of many would touch was worked out by two loops, one for a tag and one for a kind or the
    e-reader feed, the same but for how a title already has it and the word saying so. One loop now, given both: a
    title the change is moot for is greyed and unticked, with its word; one unticked by hand stays so; and the button
    says how many, as a review of a group's suggestion says it too."""
    script = """
const els = {}, $ = id => els[id] || (els[id] = { value: "", disabled: false, textContent: "" });
let op = "add";
const tagOp = () => op, KIND_NAME = { book: "Book", comic: "Comic" };
const FIX_TEXT = { kind: v => v === "auto" ? "automatic" : KIND_NAME[v], ereader: v => ({ auto: "automatic", always: "always listed" })[v] };
const row = i => {
  const box = { checked: true, disabled: false }, note = { textContent: "" }, on = new Set();
  return { dataset: { i: String(i) }, box, on, classList: { toggle: (c, yes) => yes ? on.add(c) : on.delete(c) }, querySelector: sel => sel === "input" ? box : note, note };
};
const rows = [0, 1, 2].map(row);
els.picks = { querySelectorAll: () => rows };
const BULK = { list: [{ tg: ["Lantern"], kfix: "", k0: "book", ereader: "" }, { tg: [], kfix: "comic", k0: "book", ereader: "always" },
  { tg: ["lantern"], kfix: "", k0: "comic", ereader: "" }], off: new Set([1]), fixed: null };
/* part 1 */
/* part 2 */
const seen = () => [rows.map(r => [r.on.has("off"), r.box.disabled, r.box.checked, r.note.textContent].join()), $("tagok").textContent, $("tagok").disabled];
const out = {};
$("tagfield").value = "tag"; $("tagname").value = "Lantern";
refreshBulkEdit(); out.add = seen();
op = "remove"; refreshBulkEdit(); out.remove = seen();
$("tagname").value = ""; refreshBulkEdit(); out.noTag = seen();
op = "add"; $("tagname").value = "Lantern"; BULK.fixed = { tag: "Lantern" }; BULK.off = new Set([0, 1, 2]);
refreshBulkEdit(); out.review = seen();
BULK.fixed = null; BULK.off = new Set([1]);
$("tagfield").value = "kind"; $("fixvalue").value = "comic"; refreshBulkEdit(); out.kind = seen();
$("tagfield").value = "ereader"; $("fixvalue").value = "always"; refreshBulkEdit(); out.feed = seen();
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const titles = ", r"// Served by"), (r"function fixMoot\(", r'\$\("tagname"\)\.oninput = refreshBulkEdit;'))
    has, hasnt, unticked, ticked = "true,true,false,has it already", "true,true,false,doesn't have it", "false,false,false,", "false,false,true,"
    assert out["add"] == [[has, unticked, has], "Add “Lantern” to 0 titles", True]  # either case: the same tag
    assert out["remove"] == [[ticked, hasnt, ticked], "Remove “Lantern” from 2 titles", False]
    assert out["noTag"] == [[ticked, unticked, ticked], "Remove", True]
    assert out["review"] == [[has, unticked, has], "Add “Lantern” to the 0 ticked, no to the rest", False]  # none ticked: no to all
    already = "true,true,false,already"
    assert out["kind"] == [[ticked, already, already], "Kind: Comic, for 1 title", False]  # a comic by your say, or as found
    assert out["feed"] == [[ticked, already, ticked], "E-reader feed: always listed, for 2 titles", False]
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert "Object.entries(KIND_NAME).map(([v, n]) => [v, n])" not in page and '!== "tag" && !BULK.fixed' not in page
    opened = page[page.index("function openBulkEdit("):page.index("function showField(")]
    assert 'r.closest("label").hidden' not in opened and "showField();" in opened  # Add and Remove: showField's alone


def test_the_page_s_script_starts_with_its_contents_each_part_in_order_and_the_state_it_keeps():
    """The page's script is one file of some 7,500 lines, with headings but no list of them, and top-level names that
    one part keeps and others change: nothing said where to look. Its contents now come first, each part by its
    heading's first words and in its order, and in brackets the state it keeps: each name declared at the top level in
    that part, and every top-level let somewhere in the list, so that a part added or a name moved shows here."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    script = page[page.index('<script type="text/hylly-later" id="pagescript">'):page.index("</script>", page.index('id="pagescript"'))]
    lines = script.split("\n")[2:]  # after "use strict"
    assert lines[0].startswith("// Contents.")
    entries: list[str] = []
    for line in lines[:next(i for i, line in enumerate(lines) if not line.startswith("//"))]:
        if line.startswith("//       "):
            entries[-1] += " " + line[2:].strip()
        elif line.startswith("//   "):
            entries.append(line[2:].strip())
    headings = [m.start() for m in re.finditer(r"^// ---- ", script, re.M)]
    assert len(entries) == len(headings) + 1 and entries[0].startswith("(before the first heading): ")
    spans = [(0, headings[0]), *zip(headings, [*headings[1:], len(script)])]
    listed = set()
    for entry, (start, end) in zip(entries, spans):
        label, what = entry.split(": ", 1)
        part = script[start:end]
        assert start == 0 or part.startswith(f"// ---- {label}"), (label, part[:80])
        kept = re.fullmatch(r".*?(?: \[([^]]*)\])?", what)[1]
        for name in kept.split(", ") if kept else []:
            base, _, field = name.partition(".")
            declared = re.search(rf"^(?:let|const) (?:[^\n]*[ ,])?{re.escape(base)}(?: =|,|;)", part, re.M)
            assert declared or field and re.search(rf"\b{re.escape(name)}\b", part), (label, name)  # RD.liftPlace: RD's, set there
            listed.add(base)
    declarations = re.findall(r"^let ([^\n]*?)(?:  //[^\n]*)?$", script, re.M)  # without the comments after them
    lets = {n for line in declarations for n in re.findall(r"(?:^|, )([A-Za-z_$][\w$]*)(?= =|,|;)", line)}
    assert lets - listed == set()


def test_a_feed_stays_xml_whatever_a_title_or_a_search_holds():
    """The second wall, after the catalog's own cleaning: what XML can't hold (control characters, a lone half of a
    surrogate pair, U+FFFE and U+FFFF), from a book file's title or a search, is left out where the feed is written, so
    a strict XML reader takes the page."""
    from xml.etree import ElementTree as ET

    assert opds.escape("Lantern\x01 Wolf\x0b\x7f\ud800\ufffe & <Tides>\t\n") == "Lantern Wolf\x7f &amp; &lt;Tides&gt;\t\n"
    odd = came_today("Lantern\x01 Wolf\x1f", a=["Ann\x0cWriter"], g=["Tides\uffff"], tg=["Knit\x00ting"], d="A tale\ud800 of tides.",
                     f=[["humble/lantern.epub", "epub", 1000]])
    shelf = SeriesShelf([odd])
    for url, device in (("/opds/added", None), ("/opds/genre", None), ("/opds/search?q=lantern%01", None), ("/ereader/opds/added", Device())):
        body = opds.answer(shelf, url, device)[2]
        tree = ET.fromstring(body.encode("utf-8"))  # whole and well formed, as the server sends it
        assert tree is not None and "\x01" not in body and "\ud800" not in body, url
    entry = ET.fromstring(opds.answer(shelf, "/opds/added")[2]).find("{http://www.w3.org/2005/Atom}entry")
    assert entry.find("{http://www.w3.org/2005/Atom}title").text == "Lantern Wolf"


def test_a_title_s_looked_up_details_are_credited_in_the_feeds_with_a_link_to_each_source():
    """As the library page's Details from: the summary ends with the sources' names in both feeds, and in the
    tablets' feed each source's page for the title is a related link (only an https address, as the page links it),
    with no type: an app that offers every link of a type it can open (KOReader opens HTML) would take it for a download.
    The e-reader's feed has none: its summary credits them. A title nobody looked up has neither."""
    from xml.etree import ElementTree as ET

    atom = "{http://www.w3.org/2005/Atom}"
    found = came_today("Lantern Wolf", d="A tale of tides.", f=[["humble/lantern.epub", "epub", 1000]],
                       lk=[["Metron", "https://metron.cloud/issue/lantern-wolf-1/"], ["Comic Vine", "https://comicvine.gamespot.com/lantern-wolf/"],
                           ["Open Library", "http://openlibrary.example/works/x"], ["Google Books", ""]])
    plain = came_today("Moonwake", d="Nobody looked this one up.", f=[["humble/moonwake.epub", "epub", 1000]])
    for device in (None, Device()):  # the tablets' feed and the e-reader's, whose summary is the details text
        tree = ET.fromstring(opds.answer(Shelf([found, plain]), "/ereader/opds/added" if device else "/opds/added", device)[2])
        entry, other = sorted(tree.findall(f"{atom}entry"), key=lambda e: e.find(f"{atom}title").text)
        summary = entry.find(f"{atom}summary").text
        assert summary.endswith("A tale of tides.\n\nDetails from Metron, Comic Vine, Open Library, Google Books"), summary
        related = [(link.get("href"), link.get("title"), link.get("type")) for link in entry.findall(f"{atom}link") if link.get("rel") == "related"]
        assert related == ([] if device else [("https://metron.cloud/issue/lantern-wolf-1/", "Metron", None),
                                              ("https://comicvine.gamespot.com/lantern-wolf/", "Comic Vine", None)])
        offered = [link.get("href") for link in entry.findall(f"{atom}link") if link.get("type") and link.get("rel") != "related"]
        assert not [href for href in offered if "metron" in href or "comicvine" in href]  # nothing of theirs offered as a file
        assert "Details from" not in other.find(f"{atom}summary").text
        assert not [link for link in other.findall(f"{atom}link") if link.get("rel") == "related"]
    assert opds.looked_up({"lk": [["Metron", "https://metron.cloud/x/"], "odd", [], [""], 7, None, ["Comic Vine", 5]]}) == [
        ("Metron", "https://metron.cloud/x/"), ("Comic Vine", "")]  # what an older catalog may hold, as it is
    for odd in (5, True, "Metron", {"Metron": "https://metron.cloud/x/"}, None):  # not a list at all: nothing, and no error
        assert opds.looked_up({"lk": odd}) == [], odd
        assert "Details from" not in opds.book_entry(came_today("Odd", d="A tale.", f=[["humble/odd.epub", "epub", 1]], lk=odd))
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")  # the page reads lk the same way
    assert "const lookedUp = it => (Array.isArray(it.lk) ? it.lk : []).filter(x => Array.isArray(x) && x[0]);" in page
    assert "${lookedUp(it).length ? `<div class=\"kv\">Details from: ${lookedUp(it).map(([name, url]) =>" in page
    assert "it.lk" not in page.replace("Array.isArray(it.lk) ? it.lk :", "")


def test_the_word_box_names_its_source_s_licence_with_a_link_to_it():
    """Wiktionary's and Wikipedia's text is CC BY-SA, which asks for credit: the box names it after the source, and the
    name links to the licence itself, in a tab of its own that has no hold on the reader's. The questions go without an
    Api-User-Agent of the page's own, and the page says why."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert '`${src === "wiki" ? "Wikipedia" : "Wiktionary"}, ${LOOK_LICENCE}`' in page
    licence = re.search(r"^const LOOK_LICENCE = `(.*?)`;", page, re.S | re.M)[1]
    assert re.fullmatch(r'<a href="https://creativecommons\.org/licenses/by-sa/4\.0/" target="_blank" rel="noopener noreferrer"'
                        r'\s+title="Creative Commons Attribution-ShareAlike 4\.0[^"<>]*">CC BY-SA</a>', licence)
    asking = page[page.index("async function wikimedia("):page.index("function bookLang(")]
    assert "headers" not in asking and "Api-User-Agent" in page[page.index("const LOOK_LICENCE"):page.index("async function wikimedia(")]


def test_an_empty_library_says_so_and_tells_its_owner_how_books_come_in():
    """A library with no titles isn't a search that found none: the page says it has none yet, and to the owner,
    as hylly init and hylly catalog do, how books come in, with the help's sections on it."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    compute = page[page.index("function compute("):page.index("\n}\n", page.index("function compute("))]
    assert 'innerHTML = ITEMS.length ? `<div class="empty">Nothing found.</div>` : emptyLibrary();' in compute
    empty = page[page.index("function emptyLibrary() {"):page.index("function compute(")]
    assert "The library has no titles yet." in empty and "EDIT.manage ?" in empty  # the steps are the owner's
    for said in ("<code>hylly login SHOP</code>", "<code>hylly download</code>", "<code>hylly catalog</code>",
                 'href="help.html#buying"', 'href="help.html#own-folders"'):
        assert said in empty, said
    help_page = (SOURCES / "help.html").read_text(encoding="utf-8")
    assert '<section id="buying">' in help_page and '<section id="own-folders">' in help_page


def test_the_page_names_a_folder_of_the_library_in_words_and_not_by_a_windows_path():
    """Everyone sees the page's texts, on Linux and macOS too, where curation\\tags.toml is no path at all: the
    tag suggestions say "tags.toml, in the library's curation folder", and no text of the page writes a folder
    Windows' way, with a backslash between names (as a script string writes it, \\\\)."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert [code for code in re.findall(r"<code>([^<]*)</code>", page) if "\\" in code] == []  # what it shows as a path
    assert re.findall(r"[\w-]+\\\\[\w-]+\.[A-Za-z]{2,5}\b", page) == []  # folder\\file.ext, anywhere: not a\\b, \\d+
    assert "<code>tags.toml</code>, in the library's curation folder" in page


# ---- e-books that can't be opened ----------------------------------------------------------------------------

def test_a_feed_offers_only_what_a_device_could_open():
    epub, mobi = ["a/tale.epub", "epub", 2 ** 20], ["a/tale.mobi", "mobi", 2 ** 20]
    only = {"id": "x", "t": "Only Locked", "k": "book", "f": [epub], "bad": {epub[0]: "locked"}}
    both = {"id": "y", "t": "Two Files", "k": "book", "f": [epub, mobi], "bad": {epub[0]: "damaged"}}
    fine = {"id": "z", "t": "All Fine", "k": "book", "f": [epub, mobi]}
    device = Device()
    assert not device.suits(only) and not device.suits({**only, "ereader": "always"})  # saying always can't make it readable
    assert device.suits(both) and device.best_file(both) == mobi and device.best_file(fine) == epub
    assert not opds.something_to_offer(only) and opds.something_to_offer(both) and opds.something_to_offer(fine)
    assert opds.something_to_offer({**only, "z": [["a/p.zip/tale.mobi", "mobi", 1]]})
    entry = opds.book_entry(both, None)
    assert "a/tale.mobi" in entry and "a/tale.epub" not in entry
    assert "a/tale.epub" in opds.book_entry(fine, None)


def test_a_title_that_can_t_be_opened_stays_out_of_both_feeds(tmp_path):
    build_library(tmp_path)
    damage_a_mobi(tmp_path)
    locked_epub(tmp_path / "collections" / "Poetry" / "Ann Writer - The Locked Tale.epub", title="The Locked Tale")
    catalog.build(tmp_path, log=lambda s: None)
    shelf, device = Catalog(tmp_path), ereader_feed()
    for url, into in (("/opds/search?q=locked", None), ("/opds/src/collections", None), ("/opds/kind/book", None), ("/ereader/opds/search?q=locked", device)):
        status, _, body = opds.answer(shelf, url, into)
        assert status == 200 and "Locked Tale" not in body, url
    body = opds.answer(shelf, "/opds/search?q=dust")[2]
    assert "Bartleby%20the%20Scrivener.epub" in body and "Bartleby%20the%20Scrivener.mobi" not in body  # that one is damaged, its EPUB fine


def test_a_locked_book_s_text_stops_at_the_fact_and_only_a_damaged_one_asks_for_another_copy():
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    said = page[page.index("function cantOpen(it)"):page.index("function listRow(it)")]
    locked, damaged = re.search(r'includes\("locked"\)\s*\?\s*"([^"]*)"\s*:\s*"([^"]*)"', said).groups()
    assert locked == "The shop that sold it locked the file to its own app, so it can only be read there."
    assert "another copy" in damaged.lower()
    item = re.search(r'<li id="cantopen">.*?</li>', (SOURCES / "help.html").read_text(encoding="utf-8"), re.S).group(0)
    assert "locked by the shop that sold it, so that only that shop's app can read it" in item
    for sentence in re.split(r"(?<=\.) ", item.lower()):
        assert "another copy" not in sentence or ("damaged" in sentence and "locked" not in sentence), sentence


def asking(lists=(), me=None, shelves_shown=(), ranks=dict) -> opds.Asking:
    """Who is asking, as these tests give it: the lists for everyone, what's yours (with by, whose feed it is), the
    shelves and the ranks, each as it is but the ranks (opds.Asking takes a function for each)."""
    return opds.Asking(me["by"] if me else None, lambda: list(lists), lambda: me, lambda: list(shelves_shown), ranks)


def test_ticked_lists_open_the_feeds_featured_first_each_in_its_own_order():
    shelf = Shelf([came_today("Alpha", f=[["humble/a.epub", "epub", 1000]]), came_today("Beta", k="comic", f=[["humble/b.cbz", "cbz", 1000]]),
                   came_today("Gamma", f=[["humble/g.epub", "epub", 1000]])])
    lists = [{"key": "3-best", "name": "Best of", "featured": True, "ids": ["Gamma", "Beta", "Alpha", "gone"]},
             {"key": "3-comics", "name": "Comics night", "featured": False, "ids": ["Beta"]}]
    start = opds.answer(shelf, "/opds", asking=asking(lists=lists))[2]
    assert start.index("<title>Featured</title>") < start.index("<title>Comics night</title>") < start.index("<title>Recently added</title>")
    assert "Best of: 3 titles" in start and '<link rel="http://opds-spec.org/featured" href="/opds/list/3-best"' in start
    status, kind, body = opds.answer(shelf, "/opds/list/3-best", asking=asking(lists=lists))
    assert status == 200 and "kind=acquisition" in kind and "Best of (3)" in body
    assert body.index("<title>Gamma</title>") < body.index("<title>Beta</title>") < body.index("<title>Alpha</title>")  # its own order, not by title
    assert opds.answer(shelf, "/opds/list/3-other", asking=asking(lists=lists))[0] == 404
    device = ereader_feed()
    root = opds.answer(shelf, "/ereader/opds", device, asking=asking(lists=lists))[2]
    assert "Best of: 2 titles" in root and "Comics night" not in root  # only what suits it; a list with none of that isn't offered
    assert opds.answer(shelf, "/ereader/opds/list/3-comics", device, asking=asking(lists=lists))[0] == 404
    plain = opds.answer(shelf, "/opds")[2]
    assert "<title>Featured</title>" not in plain and "opds-spec.org/featured" not in plain


def test_a_feed_of_your_own_starts_with_what_you_re_reading_then_your_lists_then_the_library():
    shelf = Shelf([came_today("Alpha", f=[["humble/a.epub", "epub", 1000]]), came_today("Beta", k="comic", f=[["humble/b.cbz", "cbz", 1000]]),
                   came_today("Gamma", f=[["humble/g.epub", "epub", 1000]])])
    lists = [{"key": "3-best", "name": "Best of", "featured": True, "ids": ["Gamma"]}]
    me = {"by": "rory", "continue": ["Beta", "Alpha"], "reading": ["Gamma", "Alpha"], "lists": [{"id": "r1", "name": "Rainy days", "ids": ["Alpha", "gone"]}]}
    status, kind, start = opds.answer(shelf, "/me/opds", asking=asking(lists=lists, me=me))
    assert status == 200 and "kind=navigation" in kind and "<title>Library · rory</title>" in start
    order = [start.index(f"<title>{t}</title>") for t in ("Continue reading", "Reading list", "Rainy days", "Featured", "Recently added")]
    assert order == sorted(order) and "2 titles you're in the middle of" in start
    assert 'href="/me/opds/continue"' in start and 'href="/me/opds/mine/r1"' in start and 'href="/me/opds/list/3-best"' in start
    assert '<link rel="start" href="/me/opds" ' in start and 'href="/me/opds/opensearch.xml"' in start  # it stays your own
    going = opds.answer(shelf, "/me/opds/continue", asking=asking(lists=lists, me=me))[2]
    assert going.index("<title>Beta</title>") < going.index("<title>Alpha</title>")  # the latest first, as given
    reading = opds.answer(shelf, "/me/opds/reading", asking=asking(lists=lists, me=me))[2]
    assert reading.index("<title>Gamma</title>") < reading.index("<title>Alpha</title>") and "Reading list (2)" in reading
    assert "Rainy days (1)" in opds.answer(shelf, "/me/opds/mine/r1", asking=asking(lists=lists, me=me))[2]
    assert opds.answer(shelf, "/me/opds/mine/nope", asking=asking(lists=lists, me=me))[0] == 404
    assert opds.answer(shelf, "/opds/continue", asking=asking(lists=lists))[0] == 404 and "Continue reading" not in opds.answer(shelf, "/opds", asking=asking(lists=lists))[2]
    device = ereader_feed()
    root = opds.answer(shelf, "/me/ereader/opds", device, asking=asking(lists=lists, me=me))[2]
    assert 'href="/me/ereader/opds/continue"' in root and "1 title you're in the middle of" in root  # only what suits it


def test_a_shelf_ticked_for_the_apps_is_a_feed_of_its_own_with_only_what_s_on_it():
    from hylly import shelves
    shelf = Shelf([came_today("Alpha", f=[["humble/a.epub", "epub", 1000]], g=["Fantasy"]), came_today("Beta", k="comic", f=[["humble/b.cbz", "cbz", 1000]]),
                   came_today("Gamma", f=[["humble/g.epub", "epub", 1000]], g=["Fantasy"], s="Saga", n="1"), came_today("Delta", f=[["humble/d.epub", "epub", 1000]], s="Saga", n="2")])
    on = [{"id": "s1", "name": "Books only", "rule": shelves.rule({"show": {"k": ["book"]}})},
          {"id": "s2", "name": "Nothing here", "rule": shelves.rule({"show": {"k": ["manual"]}})}]
    start = opds.answer(shelf, "/opds", asking=asking(shelves_shown=on))[2]
    assert "<title>Books only</title>" in start and "A shelf: 3 titles" in start and 'href="/opds/shelf/s1"' in start
    assert "Nothing here" not in start  # a shelf with nothing for this feed isn't offered
    root = opds.answer(shelf, "/opds/shelf/s1", asking=asking(shelves_shown=on))[2]
    assert "<title>Library: Books only (3)</title>" in root and '<link rel="start" href="/opds/shelf/s1" ' in root
    assert 'href="/opds/shelf/s1/genre"' in root and 'href="/opds/shelf/s1/opensearch.xml"' in root and "Books only</title><id>" not in root  # no shelves in it
    genres = opds.answer(shelf, "/opds/shelf/s1/genre", asking=asking(shelves_shown=on))[2]
    assert "Fantasy" in genres and "2 titles" in genres
    assert "<title>Beta</title>" not in opds.answer(shelf, "/opds/shelf/s1/added", asking=asking(shelves_shown=on))[2]
    assert opds.answer(shelf, "/opds/shelf/nope", asking=asking(shelves_shown=on))[0] == 404 and opds.answer(shelf, "/opds/shelf/s1")[0] == 404  # not ticked
    device = ereader_feed()
    assert 'href="/ereader/opds/shelf/s1"' in opds.answer(shelf, "/ereader/opds", device, asking=asking(shelves_shown=on))[2]


class SeriesShelf(Shelf):  # the series feed groups by each title's series key (library.series_key)
    def search(self, q):
        return self.items


def many(n, **more):
    """n made-up titles, named for the A to Z: Aa1, Ab2, Ba3, ... and a few by number."""
    names = [f"{chr(65 + i % 26)}name {i}" for i in range(n - 3)] + ["1st thing", "2nd thing", "…"]
    return [came_today(t, **more) for t in names]


def test_a_long_listing_starts_with_an_a_to_z_index():
    shelf = Shelf(many(120))
    status, kind, body = opds.answer(shelf, "/opds/kind/book")
    assert status == 200 and "kind=navigation" in kind and "<title>Books (120)</title>" in body
    assert body.index("<title>All 120 titles</title>") < body.index("<title>A</title>") < body.index("<title>Z</title>") < body.index("<title>0–9</title>") < body.index("<title>#</title>")
    assert 'href="/opds/kind/book?letter=A"' in body and "5 titles" in body and 'href="/opds/kind/book?all=1"' in body
    a = opds.answer(shelf, "/opds/kind/book?letter=A")
    assert "kind=acquisition" in a[1] and "Books: A (5)" in a[2] and "<title>Bname 1</title>" not in a[2]
    assert "Books: 0–9 (2)" in opds.answer(shelf, "/opds/kind/book?letter=" + "0%E2%80%939")[2]
    everything = opds.answer(shelf, "/opds/kind/book?all=1")[2]
    assert "Books (120)" in everything and "all=1&amp;page=1" in everything  # the next page stays in All
    assert "kind=acquisition" in opds.answer(Shelf(many(90)), "/opds/kind/book")[1]  # a short one as before
    assert "kind=acquisition" in opds.answer(SeriesShelf(many(120)), "/opds/search?q=name")[1]  # and a search's answer is a list


def test_many_series_start_with_an_a_to_z_index_too():
    titles = [came_today(f"{chr(65 + i % 26)}saga {i} vol {v}", s=f"{chr(65 + i % 26)}saga {i}", n=str(v)) for i in range(110) for v in (1, 2)]
    shelf = SeriesShelf(titles)
    body = opds.answer(shelf, "/opds/series")[2]
    assert "<title>All series</title>" in body and 'href="/opds/series?letter=B"' in body
    b = opds.answer(shelf, "/opds/series?letter=B")[2]
    assert "<title>Series: B</title>" in b and "Bsaga 1</title>" in b and "Asaga 0</title>" not in b


def test_best_rated_and_most_read_open_the_feed_when_there_are_any():
    shelf = Shelf([came_today("Alpha"), came_today("Beta"), came_today("Gamma")])
    asked = []
    ranks = lambda: asked.append(1) or {"best": ["Gamma", "Alpha", "gone"], "most": ["Beta"]}  # noqa: E731
    start = opds.answer(shelf, "/opds", asking=asking(ranks=ranks))[2]
    assert start.index("<title>Recently bought</title>") < start.index("<title>Best rated</title>") < start.index("<title>Most read</title>")
    assert "2 titles with stars, the best first" in start and "1 title that most of you have read in" in start
    assert 'rel="http://opds-spec.org/sort/popular" href="/opds/popular"' in start and 'rel="http://opds-spec.org/sort/new" href="/opds/added"' in start
    best = opds.answer(shelf, "/opds/best", asking=asking(ranks=ranks))[2]
    assert best.index("<title>Gamma</title>") < best.index("<title>Alpha</title>") and "Best rated (2)" in best
    assert "<title>Beta</title>" in opds.answer(shelf, "/opds/popular", asking=asking(ranks=ranks))[2]
    asked.clear()
    opds.answer(shelf, "/opds/kind/book", asking=asking(ranks=ranks))
    assert asked == []  # asked only where they're shown
    none = opds.answer(shelf, "/opds", asking=asking(ranks=lambda: {"best": [], "most": []}))[2]
    assert "Best rated" not in none and "Most read" not in none and "sort/popular" not in none


def test_a_feed_page_works_out_only_what_of_yours_it_shows_and_each_part_once():
    """The start page shows all of it, each part read once however often the page looks; a shelf's address its
    shelves; a list for everyone the lists; Continue reading, the reading list and your own lists what's yours; Best
    rated and Most read the ranks; and the search, its description, the library's own listings and an address that is
    no page none of it."""
    shelf = SeriesShelf([came_today("Alpha", f=[["humble/a.epub", "epub", 1000]])])  # one that searches too
    asked: list[str] = []
    mine = {"continue": ["Alpha"], "reading": ["Alpha"], "lists": [{"id": "r1", "name": "Rainy days", "ids": ["Alpha"]}]}
    on = [{"id": "s1", "name": "Books only", "rule": lambda it: True}]

    def read(url: str) -> list[str]:
        asked.clear()
        parts = {"lists": [{"key": "3-best", "name": "Best of", "featured": True, "ids": ["Alpha"]}], "mine": mine, "shelves": on,
                 "ranks": {"best": ["Alpha"], "most": []}}
        part = {name: (lambda name=name: asked.append(name) or parts[name]) for name in parts}
        assert opds.answer(shelf, url, asking=opds.Asking("rory", **part))[0] in (200, 404)
        return sorted(asked)

    assert read("/opds") == ["lists", "mine", "ranks", "shelves"]
    for url in ("/opds/opensearch.xml", "/opds/nonesuch", "/opds/search?q=alpha", "/opds/added", "/opds/new", "/opds/genre",
                "/opds/kind/book", "/opds/series", "/opds/series/x", "/me/opds/opensearch.xml"):
        assert read(url) == [], url
    assert read("/opds/shelf/s1/added") == ["shelves"] and read("/opds/shelf/s1") == ["ranks", "shelves"]
    assert read("/opds/list/3-best") == ["lists"] and read("/opds/best") == ["ranks"] and read("/opds/popular") == ["ranks"]
    assert read("/opds/continue") == read("/opds/reading") == read("/opds/mine/r1") == ["mine"]


def test_the_server_reads_a_person_s_records_for_a_feed_page_only_where_it_shows_them(running, monkeypatch):
    """A reading app asking for the search's description, a listing or an address that is no page: nothing of the
    person's records is read for it; the start page reads them all, and Continue reading the person's own (with the
    lists for everyone, which it leaves out of theirs)."""
    client, _ = running
    asked: list[str] = []
    for name in ("_feed_lists", "_feed_ranks"):
        real = getattr(serve.Handler, name)
        monkeypatch.setattr(serve.Handler, name, lambda self, real=real, name=name: asked.append(name) or real(self))
    for name in ("_my_feed", "_feed_shelves"):
        real = getattr(serve.Handler, name)
        monkeypatch.setattr(serve.Handler, name, lambda self, *a, real=real, name=name: asked.append(name) or real(self, *a))
    for url, status, read in (("/opds/opensearch.xml", 200, []), ("/opds/nonesuch", 404, []), ("/opds/added", 200, []),
                              ("/ereader/opds/genre", 200, []), ("/opds/search?q=lantern", 200, []),
                              ("/opds", 200, ["_feed_lists", "_feed_ranks", "_feed_shelves", "_my_feed"]),
                              ("/opds/continue", 200, ["_feed_lists", "_my_feed"])):
        asked.clear()
        assert client.get(url).status_code == status, url
        assert sorted(asked) == read, url


# ---- the page and the keys: what an edit draws anew, and the menus' rows ----

def test_the_menus_rows_that_open_something_here_are_buttons_and_only_those_that_go_somewhere_are_links():
    """Twelve rows of the Help, Manage and account menus were links to "#" that opened a dialog: a screen reader said
    link, Space didn't press them, and a middle click opened a second library. They're buttons now, styled as the rows
    are; the links left (Help, the Server page, To decide, Log in, Source, Licences, a shelf) go somewhere, and the
    menus' own code finds rows of either kind."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    header = page[page.index("<header>"):page.index("</header>")]
    for name in ("guidelink", "reloadlink", "repqlink", "revqlink", "suggestlink", "tagshown", "changeslink", "mychanges",
                 "listlink", "ownlink", "bmlink", "myrevlink"):
        assert re.search(rf'<button type="button" id="{name}" class="(ir|tile)"', header), name
    assert 'href="#"' not in header
    assert ".pop a, .pop .ir, .pop .tile, .pop form button { display: flex;" in page and ".pop .ir { flex-direction: row;" in page
    assert '[...pop.querySelectorAll(".ir")]' in page and 'n.matches(".pop a, .pop .ir, .pop .tile")' in page
    assert "e.preventDefault(); open" not in page  # no link's default left to stop


def test_your_stars_stay_together_on_one_row_beside_or_under_their_label():
    """At phone width the five 44 px star buttons wrapped beside the cover, two on the label's row and three below,
    so the fourth star was the second of the second row. They're one group that doesn't wrap now: beside the label
    where there's room, else on a row of their own under it, in the details and in the review form alike."""
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert ".sfive { display: inline-flex; flex: none; }" in page and ".smine { display: flex; flex-wrap: wrap;" in page
    assert '<span>Your stars</span><span class="sfive">${buttons}</span><span class="ssaid" role="status"></span>' in page
    form = page[page.index("function revStarsHtml() {"):page.index("function openReviewForm(")]
    assert '`<span>Your stars</span><span class="sfive">${starButtons(RF.stars, "data-rf-stars")}</span>`' in form


# The grid as far as drawing the cards goes: cards in one column, each 100 px tall, the window scrolled by scrollY
GRID_PAGE = """
let scrollY = 0, made = 0;
const scrolled = [];
const window = { scrollTo: (x, y) => { scrolled.push(y); scrollY = y; } };
class Card {
  constructor(entry) { this.dataset = { k: cardKey(entry) }; this.made = cardHtml(entry); this.n = ++made; }
  getBoundingClientRect() { return { top: above + grid.children.indexOf(this) * 100 - scrollY }; }
  replaceWith(other) { grid.children[grid.children.indexOf(this)] = other; }
}
let above = 0;  // what lies above the list, such as the Continue row
const grid = { children: [], set innerHTML(v) { this.children = []; }, appendChild(c) { this.children.push(c); }, getBoundingClientRect: () => ({ top: above - scrollY }) };
const headerBottom = () => 0;
const $ = id => id === "grid" ? grid : {};
const cardHtml = entry => entry.it.t, card = entry => new Card(entry), cardOpens = (el, entry) => { el.entry = entry; };
const state = { shown: 0, list: [] };
const more = () => { for (const e of state.list.slice(state.shown, state.shown + 120)) grid.appendChild(card(e)); state.shown = grid.children.length; };
const firstVisible = () => grid.children.findIndex(c => c.getBoundingClientRect().top + 100 > 0);
const titles = n => Array.from({ length: n }, (_, i) => ({ it: { id: `t${i}`, t: `Title ${i}` } }));
"""


def test_an_edit_or_a_sync_keeps_your_place_in_the_list():
    """Every change redrew the list from its first 120 cards: an owner fixing "Missing: author" far down the list
    landed near the top after each fix. Drawn again after an edit or a sync now, the same titles in the same order have
    only the card that changed drawn anew; otherwise as many cards as were shown come back, and the title at the top of
    the window stays where it was on the screen, or, gone from the list, the one now in its place. At the list's top it
    stays at the top: an order by what comes after the page opened (Best rated, Recently read) shows its new first titles
    as they come, not the old first one kept in view. A new search, filter, order or shelf starts from the top, as ever."""
    script = GRID_PAGE + """
/* part 1 */
/* part 2 */
const out = {};
state.list = titles(300); drawList(); more();  // two batches drawn, scrolled down to the 151st
scrollY = 15000;
const before = [...grid.children];
state.list[150].it.t = "Title 150, corrected";
drawList(true);
out.same = { shown: state.shown, cards: grid.children.length, replaced: grid.children.filter((c, i) => c !== before[i]).map(c => c.dataset.k), scrolled: scrolled.splice(0) };
const moved = state.list.splice(150, 1)[0]; state.list.splice(10, 0, moved);  // its new title sorts it higher up
drawList(true);
out.order = { cards: grid.children.length, scrolled: scrolled.splice(0), top: grid.children[firstVisible()].dataset.k };
scrollY = 15000;
const atTop = grid.children[firstVisible()].dataset.k, next = cardKey(state.list[firstVisible() + 1]);
state.list = state.list.filter(e => cardKey(e) !== atTop);  // the title at the top fixed out of the list
drawList(true);
out.gone = { was: atTop, next, scrolled: scrolled.splice(0), top: grid.children[firstVisible()].dataset.k };
drawList();  // a new search
out.fresh = { cards: grid.children.length, scrolled: scrolled.splice(0) };
scrollY = 0;
state.list.reverse();  // the stars came: Best rated in its real order now
drawList(true);
out.top = { scrolled: scrolled.splice(0), first: grid.children[0].dataset.k === cardKey(state.list[0]) };
above = 300; scrollY = 120;  // the Continue row above the list, scrolled partly past it: the list's top still in view
state.list.reverse();
drawList(true);
out.listTop = { scrolled: scrolled.splice(0), at: scrollY };
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const cardKey = ", r"function card\("), (r"function drawList\(", r"// ---- the filters: groups"))
    assert out["same"] == {"shown": 240, "cards": 240, "replaced": ["t:t150"], "scrolled": []}  # the one card, nothing moved
    assert out["order"] == {"cards": 240, "scrolled": [1000], "top": "t:t150"}  # as many as were shown, the title at the top kept
    assert out["gone"] == {"was": "t:t149", "next": "t:t151", "scrolled": [15000], "top": "t:t151"}  # the next one in its place
    assert out["fresh"] == {"cards": 120, "scrolled": []}
    assert out["top"] == {"scrolled": [], "first": True} and out["listTop"] == {"scrolled": [], "at": 120}
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert "function redraw() {" in page and "  compute(true);\n  if (SHEET.it) openItem(SHEET.it, true);\n}" in page
    for edit in ("if (!quiet) redraw();", "state.f.tg.size ? compute(true) : renderFacets();",
                 "if (ratingsUsed()) compute(true);", 'state.sort === "list") compute(true);'):
        assert edit in page, edit  # a correction, a tag, stars come, places come: the same view drawn again
    assert '$("sort").onchange = e => { state.sort = e.target.value; compute(); };' in page  # a new order: from the top


# The library page as far as a mouse dragging it goes: the page, which scrolls down, a row that scrolls sideways and the
# grid in the list (main), a field in the header and one in the list, an empty library's words; and the window's events
DRAG_PAGE = """
let NOW = 1000, frameIds = 0;
const performance = { now: () => NOW }, frames = new Map(), timers = [];
const requestAnimationFrame = f => (frames.set(++frameIds, f), frameIds), cancelAnimationFrame = id => frames.delete(id);
const setTimeout = f => timers.push(f);
const glideOut = (most = 1000) => { for (let n = 0; n < most && frames.size; n++) { NOW += 16; const [[id, f]] = frames; frames.delete(id); f(NOW); } };
const LESS_MOTION = { matches: false };
let DIALOGS = [];
const ON = {};
function addEventListener(kind, f, how) { (ON[kind] ||= []).push({ f, once: !!(how && how.once) }); }
function removeEventListener(kind, f) { ON[kind] = (ON[kind] || []).filter(l => l.f !== f); }
function fire(kind, more = {}) {
  const e = Object.assign({ type: kind, pointerId: 1, pointerType: "mouse", button: 0, buttons: 1, offsetX: 5, offsetY: 5, prevented: false,
    stopped: false, preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } }, more);
  for (const l of [...(ON[kind] || [])]) {
    if (l.once) removeEventListener(kind, l.f);
    l.f(e);
    if (e.stopped) break;
  }
  return e;
}
class El {  // is: the selectors it matches; w and h: how far it scrolls sideways and down
  constructor(is, parent, more = {}) {
    Object.assign(this, { is, parentElement: parent, w: 0, h: 0, x: 0, y: 0, ox: "visible", oy: "visible", clientWidth: 0, clientHeight: 0 }, more);
  }
  get scrollWidth() { return this.clientWidth + this.w; }
  get scrollHeight() { return this.clientHeight + this.h; }
  get scrollLeft() { return this.x; }
  set scrollLeft(v) { this.x = Math.max(0, Math.min(this.w, v)); }
  get scrollTop() { return this.y; }
  set scrollTop(v) { this.y = Math.max(0, Math.min(this.h, v)); }
  matches(sel) { return sel.split(",").some(s => this.is.includes(s.trim())); }
  closest(sel) { for (let e = this; e; e = e.parentElement) if (e.matches(sel)) return e; return null; }
}
const getComputedStyle = el => ({ overflowX: el.ox, overflowY: el.oy });
const html = new El(["html"], null, { clientWidth: 1000, clientHeight: 700, h: 5000 }), body = new El(["body"], html);
const classes = new Set(), captured = [];
Object.assign(html, { classList: { add: c => classes.add(c), remove: c => classes.delete(c) }, setPointerCapture: id => captured.push(id) });
const search = new El(["input"], new El(["header"], body)), main = new El(["main"], body);
const row = new El([".ctiles"], main, { ox: "auto", oy: "auto", clientWidth: 800, clientHeight: 180, w: 2000 });
const tile = new El(["button", ".ctile"], row, { clientWidth: 104, clientHeight: 170 }), grid = new El(["#grid"], main);
const card = new El(["button", ".card"], grid, { clientWidth: 150, clientHeight: 260 }), cover = new El(["img"], card, { clientWidth: 150, clientHeight: 225 });
const field = new El(["input"], main), command = new El(["code"], new El([".empty"], grid));
const picked = { isCollapsed: true, removeAllRanges() { this.isCollapsed = true; } };
const document = { body, documentElement: html, scrollingElement: html, getSelection: () => picked };
/* part 1 */
/* part 2 */
// pressed on target and moved by each step, 10 ms apart, then let go after wait ms and clicked, as a browser does
function drag(target, steps, { wait = 10, click = true, ...more } = {}) {
  let x = 500, y = 400;
  fire("pointerdown", { target, clientX: x, clientY: y, ...more });
  for (const [dx, dy] of steps) {
    NOW += 10;
    x += dx;
    y += dy;
    fire("pointermove", { target, clientX: x, clientY: y, ...more });
  }
  const during = classes.has("panning");
  NOW += wait;
  fire("pointerup", { target, clientX: x, clientY: y, ...more, buttons: 0 });
  const swallowed = click ? fire("click", { target }).prevented : null;
  return { page: html.y, row: row.x, during, panning: classes.has("panning"), swallowed, gliding: frames.size > 0 };
}
"""


def test_a_mouse_drags_the_titles_as_a_finger_does_and_a_press_that_hardly_moves_is_still_a_click():
    """On a PC the list moved only by the wheel and the scroll bars: a press of the mouse's button and a drag, as a
    finger moves it on a phone, picked words or did nothing. Now such a drag scrolls the page, or with a drag mostly
    sideways a row that scrolls sideways, from the press on, and glides on as it's let go, slowing to the page's end at
    most, unless less motion is asked for; a new press, the wheel, a key or a finger stops that. A press that moves
    less than 6 px is a click, as before; one that moved opens nothing, and only that one click is taken. A finger, a
    pen, the other buttons, a key held, a field, an empty library's words, a row's own scroll bar and an open dialog
    keep what they did."""
    script = DRAG_PAGE + """
const out = {};
html.y = 1000;
out.click = drag(card, [[0, -3], [2, -2]]);
out.flick = drag(cover, [[0, -4], [0, -4], [0, -20], [0, -20]]);
glideOut();
out.glided = html.y;
timers.splice(0).forEach(f => f());
out.slow = drag(card, [[0, 10], [0, 10]], { wait: 300, click: false });  // and a browser that sends no click after it
out.noClick = { waiting: (ON.click || []).length };
timers.splice(0).forEach(f => f());
out.noClick.after = (ON.click || []).length;
out.noClick.next = fire("click", { target: card }).prevented;
out.sideways = drag(tile, [[-10, 2], [-30, 0]]);
glideOut();
out.sidewaysGlided = { row: row.x, page: html.y };
out.down = drag(tile, [[0, -10], [0, -30]]);
const pressed = fire("pointerdown", { target: card, clientX: 0, clientY: 0 });
out.stopped = { byPress: frames.size };
fire("pointerup", { target: card, clientX: 0, clientY: 0, buttons: 0 });
drag(card, [[0, -20], [0, -20]]);
out.stopped.restingFinger = [frames.size, (fire("wheel", { deltaX: 0, deltaY: 1 }), frames.size)];  // a trackpad's next to nothing
for (const kind of ["wheel", "keydown", "touchstart"]) {
  drag(card, [[0, -20], [0, -20]]);
  out.stopped[kind] = [frames.size, (fire(kind, kind === "wheel" ? { deltaY: 40 } : {}), frames.size)];
}
const page = html.y;
out.kept = {};
for (const [name, target, more] of [["touch", card, { pointerType: "touch" }], ["pen", card, { pointerType: "pen" }], ["right button", card, { button: 2 }],
                                    ["shift", card, { shiftKey: true }], ["ctrl", card, { ctrlKey: true }], ["alt", card, { altKey: true }],
                                    ["cmd", card, { metaKey: true }], ["field in the list", field, {}], ["search", search, {}],
                                    ["an empty library's words", command, {}], ["the row's scroll bar", row, { offsetY: 185 }]]) {
  const r = drag(target, [[0, -20], [0, -20]], more);
  out.kept[name] = r.page === page && r.row === row.x && !r.swallowed && !r.gliding && !r.during;
}
DIALOGS = [{}];
const behind = drag(card, [[0, -20], [0, -20]]);
out.kept["a dialog open"] = behind.page === page && !behind.swallowed;
DIALOGS = [];
LESS_MOTION.matches = true;
out.lessMotion = drag(card, [[0, -20], [0, -20]]);
LESS_MOTION.matches = false;
html.y = 4990;
out.atTheEnd = drag(card, [[0, -20], [0, -20]]);
const at = frames.size;
glideOut(3);
out.atTheEnd.frames = [at, frames.size];
fire("pointerdown", { target: card, clientX: 500, clientY: 400 });
fire("pointermove", { target: card, clientX: 500, clientY: 450 });
picked.isCollapsed = false;  // words picked on the way
fire("pointermove", { target: card, clientX: 500, clientY: 460 });
const drop = fire("dragstart", { target: cover });
fire("pointermove", { target: card, clientX: 500, clientY: 470, buttons: 0 });  // let go outside the window
out.outside = { picked: !picked.isCollapsed, dropped: drop.prevented, panning: classes.has("panning"), gliding: frames.size > 0,
  click: fire("click", { target: card }).prevented, dragstart: fire("dragstart", { target: cover }).prevented, captured: [...new Set(captured)] };
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const GLIDE_MS = ", r"function stopGlide\(\)"), (r"// ---- a mouse drags the titles", r"// ---- detail ----"))
    assert out["click"] == {"page": 1000, "row": 0, "during": False, "panning": False, "swallowed": False, "gliding": False}
    assert out["flick"] == {"page": 1048, "row": 0, "during": True, "panning": False, "swallowed": True, "gliding": True}
    assert 1048 + 100 < out["glided"] < 1048 + 3 * 420  # on a little, slowing, at the most as fast as a zoomed page
    assert out["slow"]["page"] == out["glided"] - 20 and not out["slow"]["gliding"]  # stopped before it was let go: no glide
    assert out["noClick"] == {"waiting": 1, "after": 0, "next": False}  # with no click after a drag, the next one opens
    assert out["sideways"]["row"] == 40 and out["sideways"]["page"] == out["slow"]["page"]
    assert out["sidewaysGlided"]["row"] > 40 and out["sidewaysGlided"]["page"] == out["slow"]["page"]
    assert out["down"]["page"] == out["slow"]["page"] + 40 and out["down"]["row"] == out["sidewaysGlided"]["row"]  # the page, not the row
    assert out["stopped"] == {"byPress": 0, "restingFinger": [1, 1], "wheel": [1, 0], "keydown": [1, 0], "touchstart": [1, 0]}
    assert out["kept"] == dict.fromkeys(out["kept"], True) and len(out["kept"]) == 12
    assert out["lessMotion"]["swallowed"] and not out["lessMotion"]["gliding"]  # moved with the mouse, and no further
    assert out["atTheEnd"]["page"] == 5000 and out["atTheEnd"]["frames"] == [1, 0]  # at the end at once: the glide ends
    assert out["outside"] == {"picked": False, "dropped": True, "panning": False, "gliding": False, "click": False, "dragstart": False,
                              "captured": [1]}  # every drag followed to its end, outside the window too
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert "html.panning, html.panning * { cursor: grabbing !important; -webkit-user-select: none; user-select: none; }" in page


# A made-up library of 10,000 titles for the search, the same at every run: made-up words, a few with accents, and the
# words people search for in many of them
MADE_UP_TITLES = """
let seed = 55;
const rand = () => { seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t ^= t + Math.imul(t ^ (t >>> 7), 61 | t); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
const pick = list => list[Math.floor(rand() * list.length)];
const SYL = ["lan", "tern", "wolf", "moon", "wake", "ash", "down", "fel", "ix", "ber", "ry", "mar", "lo", "quin", "tor", "vel",
             "sar", "kin", "dal", "rin", "oth", "eb", "un", "cal", "dor", "mir", "an", "és", "tal", "brî", "go", "nør"];
const COMMON = ["the", "of", "and", "dragon", "shadow", "night", "river", "harbor", "glass", "iron", "silver", "crown", "lantern"];
const made = () => Array.from({ length: 2 + Math.floor(rand() * 2) }, () => pick(SYL)).join("");
const phrase = (lo, hi) => Array.from({ length: lo + Math.floor(rand() * (hi - lo + 1)) }, () => rand() < 0.3 ? pick(COMMON) : made())
  .map(w => w[0].toUpperCase() + w.slice(1)).join(" ");
const ITEMS = Array.from({ length: 10000 }, (_, i) => {
  const t = phrase(3, 6), it = { id: `h:${i}`, t, a: [`${phrase(1, 1)} ${phrase(1, 1)}`], p: phrase(1, 2) + " Press",
    g: [pick(["Science Fiction", "Fantasy", "Mystery & Thriller", "Cooking", "History"])], sub: [phrase(2, 2), phrase(2, 2)],
    f: [[`humble/${encodeURIComponent(phrase(1, 2))}/${encodeURIComponent(t)}.epub`, "epub", 1000]] };
  if (rand() < 0.5) it.s = phrase(2, 3);
  if (rand() < 0.5) it.b = phrase(2, 3) + " Bundle"; else it.col = phrase(1, 2);
  if (rand() < 0.2) it.tg = [made()];
  if (rand() < 0.1) it.cp = [["storybundle", phrase(2, 3)]];
  return it;
});
"""
# The search as it was before its words were made when a search needs them and its typo pass compared each word once
# a search: kept here, so that the page's is held to the very same titles and scores
SEARCH_BEFORE = """
const indexBefore = it => [
  [words(it.tg.join(" ")), 2.5], [words((it.gm || []).join(" ")), 2.5],
  [words(it.t), 3], [words(it.s), 3], [words((it.a || []).join(" ")), 2.5],
  [words([it.p, it.b, it.col, ...(it.cp || []).map(c => c[1])].join(" ")), 1], [words([(it.g || []).join(" "), (it.sub || []).join(" ")].join(" ")), 1],
  [words((it.f || []).map(([p]) => decodeURIComponent(p.split("/").pop()).replace(/\\.[^.]*$/, "")).join(" ")), 0.5],
];
function levBefore(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i]; let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      best = Math.min(best, cur[j]);
    }
    if (best > max) return max + 1;
    prev = cur;
  }
  return prev[b.length];
}
function scoreBefore(idx, qw) {
  let total = 0;
  for (const w of qw) {
    let best = 0;
    for (const [toks, weight] of idx) for (const t of toks) {
      if (t === w) best = Math.max(best, weight * 2);
      else if (t.startsWith(w)) best = Math.max(best, weight * 1.4);
    }
    if (!best && w.length >= 4) {
      const max = w.length >= 7 ? 2 : 1;
      for (const [toks, weight] of idx) for (const t of toks) {
        if (levBefore(w, t.slice(0, w.length + max), max) <= max) { best = Math.max(best, weight * 0.8); break; }
      }
    }
    if (!best) return 0;
    total += best;
  }
  return total;
}
"""


def test_a_search_finds_the_same_titles_in_the_same_order_with_its_words_made_when_it_needs_them():
    """Every title's words for the search were made as the page laid the titles, before their first draw: a quarter
    of a second on 10,000 titles that only a search uses. They're made when a search first needs them now (the one given
    in the address, too), or while the page is idle after the first draw. The typo pass compared each word of every
    title with the query word, though most words are in many titles: each word is compared once a search now, and the
    edit distance keeps its two rows. Over 10,000 made-up titles, every search finds the same titles with the same
    scores, so in the same order, as before; and a title tagged anew is found by its new tag and not its old one."""
    script = MADE_UP_TITLES + SEARCH_BEFORE + """
const window = {};
/* part 1 */
/* part 2 */
const out = { unmade: ITEMS.every(it => it.idx === null), same: {}, found: {}, compared: {}, again: {} };
const levOwn = lev;
let compared = 0;
lev = (a, b, max) => (compared++, levOwn(a, b, max));  // each comparison of a word with the query word's
const QUERIES = ["lan", "lantern", "dragon", "dragn", "lantren", "harbr", "silvr crwn", "moon harbor lantern", "the of",
                 "brîgo", "brigo", "nor", "qqqzzxx", "ashdown wolf"];
for (const q of QUERIES) {
  const qw = words(q), near = qw.map(() => new Map());
  compared = 0;
  const now = ITEMS.map(it => score(it, qw, near));
  out.compared[q] = compared;
  compared = 0;
  ITEMS.forEach(it => score(it, qw, near));  // the same search again, the words compared already
  out.again[q] = compared;
  const before = ITEMS.map(it => scoreBefore(indexBefore(it), qw));
  const order = scores => ITEMS.map((it, i) => [it, scores[i]]).filter(([, s]) => s)
    .sort((x, y) => y[1] - x[1] || x[0].t.localeCompare(y[0].t)).map(([it, s]) => `${it.id} ${s}`);  // compute's own order
  out.same[q] = JSON.stringify(order(now)) === JSON.stringify(order(before)) && now.every((s, i) => s === before[i]);
  out.found[q] = now.filter(Boolean).length;
}
out.distinct = new Set(ITEMS.flatMap(it => searchIndex(it).flatMap(([toks]) => toks))).size;
const tagged = ITEMS[7];
retag(tagged, ["Zebrawood"]);
out.retag = [score(tagged, words("zebrawood")) > 0, tagged.idx !== null, ITEMS.filter(it => score(it, words("zebrawood"))).map(it => it.id)];
takeTitle(tagged, { t: "Quillwort", tg: [] });  // the server's answer to a correction: laid anew, its words made again
out.taken = [score(tagged, words("quillwort")) > 0, score(tagged, words("zebrawood"))];
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"// ---- text normalisation", r"// ---- filtering, grouping, sorting"),
                      (r"function retag\(", r"function fillTagList\("))
    assert out["unmade"]  # laid, and no title's words made: nothing a first draw waits for
    assert out["same"] == {q: True for q in out["same"]}, out["found"]
    assert out["found"]["dragon"] > 1000 and out["found"]["lantren"] > 0 and out["found"]["qqqzzxx"] == 0  # typos found, nonsense not
    assert out["found"]["brigo"] == out["found"]["brîgo"] > 0  # accents folded, as before
    for q, n in out["compared"].items():
        assert n <= len(q.split()) * out["distinct"], (q, n)  # each word at most once a search, whatever titles have it
    assert out["compared"]["qqqzzxx"] > 1000 and set(out["again"].values()) == {0}
    assert out["retag"] == [True, True, ["h:7"]] and out["taken"] == [True, 0]
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    lev = page[page.index("function lev("):page.index("// every query word must match somewhere")]
    assert "Array.from" not in lev and "[i]" not in lev and "LEV_ROWS" in lev  # its two rows, kept, not made for each row
    assert "it.idx = null;" in page[page.index("function lay(it) {"):page.index("function searchIndex(it) {")]
    assert page.count("searchIndex(") == 3  # made, read by score, and made while the page is idle


def test_the_titles_are_drawn_at_once_or_with_the_session_s_say_and_their_words_made_after():
    """The list was drawn three times at start-up: as the page loaded, again when the session's answer came (the e-reader
    feed's titles), and a third time for an editor. The session is asked for beside the titles now: when its answer is
    in as they are, it's laid over them and they're drawn once; when it isn't, they're drawn at once, never waiting
    for it, and again when it comes; with none (no server) they're drawn as they are, and if one of the session's steps
    fails, they're drawn all the same. Their words for the search are made after the first draw, a few at a time while
    the page is idle, in Safari too, which has no idle callback."""
    script = """
const state = {}, drawn = [], frames = [], idle = [], timers = [];
const compute = (again = false) => { drawn.push(again ? "again" : "fresh"); state.hits = []; };
const requestAnimationFrame = f => frames.push(f);
const setTimeout = (f, ms) => timers.push(f);
let window = { requestIdleCallback: f => idle.push(f) }, session = () => {};
const setMode = () => {}, loadHash = () => {}, startSession = () => Promise.resolve().then(() => session());
const ITEMS = Array.from({ length: 50 }, (_, i) => ({ id: `t${i}`, t: `Lantern Wolf ${i}`, a: ["Ann Writer"] }));
/* part 1 */
/* part 2 */
const start = () => {  // the page's last lines
/* part 3 */
};
const tick = () => new Promise(done => setImmediate(done));
const fresh = () => { state.hits = undefined; drawn.length = 0; drawStart.drawn = false; frames.length = 0; idle.length = 0; timers.length = 0; };
const out = {};
(async () => {
  window.SESSION_IN = true;  // the answer came beside the titles: its steps run at once, then they're drawn with it
  session = () => drawStart(true);
  start();
  const before = [...drawn];
  await tick();
  timers.splice(0).forEach(f => f());
  out.inTime = { before, drawn: [...drawn], made: ITEMS.filter(it => it.idx).length, frames: frames.length };
  frames.shift()();  // the first draw is on the screen
  let left = 3, slices = 0;
  while (idle.length) { slices++; idle.shift()({ timeRemaining: () => (left = left > 0 ? left - 1 : 3) ? 5 : 0 }); }
  out.idle = { made: ITEMS.filter(it => it.idx).length, slices };
  fresh(); window.SESSION_IN = false;  // not in yet: drawn at once, and again when it comes
  session = () => {};
  start();
  out.late = { atOnce: [...drawn] };
  await tick(); timers.splice(0).forEach(f => f());
  drawStart(true);
  out.late.drawn = [...drawn];
  fresh();
  session = () => drawStart();  // no answer at all (no server): drawn as they are
  start(); await tick(); timers.splice(0).forEach(f => f());
  out.none = [...drawn];
  fresh(); window.SESSION_IN = true;
  session = () => { throw new Error("a step that failed"); };  // in, but a step after it failed
  start(); await tick().catch(() => {});
  const beforeTimer = [...drawn];
  timers.splice(0).forEach(f => f());
  out.failed = { beforeTimer, drawn: [...drawn] };
  fresh(); for (const it of ITEMS) lay(it);
  window = { SESSION_IN: true };  // Safari: no idle callback, a pause instead
  session = () => drawStart(true);
  start(); await tick(); frames.shift()();
  while (timers.length) timers.shift()();
  out.safari = ITEMS.filter(it => it.idx).length;
  process.stdout.write(JSON.stringify(out));
})();
process.on("unhandledRejection", () => {});
"""
    out = in_the_page(script, (r"// ---- text normalisation", r"// ---- filtering, grouping, sorting"),
                      (r"// The titles at the start, never waiting", r"async function startSession\(\)"),
                      (r"setMode\(\); loadHash\(true\);", r'window\.addEventListener\("hashchange"'))
    assert out["inTime"] == {"before": [], "drawn": ["fresh"], "made": 0, "frames": 1}  # once, with it, nothing made before it
    assert out["idle"]["made"] == 50 and out["idle"]["slices"] > 1  # a few at a time, while the page is idle
    assert out["late"] == {"atOnce": ["fresh"], "drawn": ["fresh", "again"]}  # at once, never waiting, and once more
    assert out["none"] == ["fresh"]
    assert out["failed"] == {"beforeTimer": [], "drawn": ["fresh"]}
    assert out["safari"] == 50
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    assert "SESSION_WAIT" not in page
    start = page[page.index("async function startSession() {"):]
    start = start[:start.index("\n}\n")]
    assert "compute(" not in start and start.count("drawStart(true)") == 2  # a reader's and an editor's, at the end of each


def test_the_session_is_asked_for_and_read_beside_the_titles_and_taken_as_any_answer_is():
    """The page's first script asks for the session beside the titles and reads it, saying so (SESSION_IN) once it's
    in; the page's script takes it as every answer is taken: a login that has ended goes to the login page, an answer
    refused or unreadable is an error, and one that didn't come is asked for again."""
    head = in_the_page("""
const fetch = async (url, init) => {
  if (MODE === "away") throw new TypeError("Failed to fetch");
  return { status: 200, ok: true, url: "http://b55a.localhost" + url, json: async () => { if (MODE === "odd") throw new SyntaxError("Unexpected token"); return { by: "ann" }; } };
};
const out = {};
(async () => {
  for (const MODE_ of ["in", "odd", "away"]) {
    globalThis.MODE = MODE_;
    const window = {};
/* part 1 */
    const a = await window.SESSION_ASKED;
    out[MODE_] = [a && a.r.status, a && a.s, !!window.SESSION_IN];
  }
  process.stdout.write(JSON.stringify(out));
})();
""", (r"  window\.SESSION_ASKED = ", r"  Promise\.all\(\[titles, read\]\)"))
    assert head == {"in": [200, {"by": "ann"}, True], "odd": [200, None, True], "away": [None, None, False]}

    def run(asked: str) -> object:
        script = """
const asked = [], went = [];
const fetchAsIs = async () => { throw new Error("no fetch here"); }, netCheck = () => {};
const getJson = async path => (asked.push(path), { by: "asked again" });
const location = { href: "http://b55a.localhost/catalog/index.html", origin: "http://b55a.localhost", pathname: "/catalog/index.html",
  search: "", hash: "#q=lantern", assign: url => went.push(url) };
const window = globalThis;
window.SESSION_ASKED = %s;
/* part 1 */
sessionAnswer().then(s => s, e => `error: ${e.message}`).then(s => process.stdout.write(JSON.stringify({ s, asked, went, leaving })));
""" % asked
        return in_the_page(script, (r"let leaving = false;", r"// Back on screen \(another app"))

    r = '{ status: %d, ok: %s, url: "http://b55a.localhost/api/session" }'
    came = run('Promise.resolve({ r: %s, s: { by: "ann" } })' % (r % (200, "true")))
    assert came == {"s": {"by": "ann"}, "asked": [], "went": [], "leaving": False}  # nothing asked again
    ended = run('Promise.resolve({ r: %s, s: null })' % (r % (401, "false")))
    assert ended["s"] == "error: the server answered 401" and ended["went"] == ["/login?next=%2Fcatalog%2Findex.html%23q%3Dlantern"]
    odd = run('Promise.resolve({ r: %s, s: null })' % (r % (200, "true")))
    assert odd["s"] == "error: the server's answer couldn't be read" and odd["went"] == []
    lost = run("Promise.resolve(null)")  # the first script's request failed
    assert lost == {"s": {"by": "asked again"}, "asked": ["/api/session"], "went": [], "leaving": False}
    assert run("undefined")["asked"] == ["/api/session"]  # a first script without it


# The filter panel's parts, with the page around them as far as they go
FILTERS_PAGE = """
const els = new Map(), $ = id => els.get(id) || els.set(id, { id, innerHTML: "", textContent: "", hidden: false, scrollTop: 0,
  attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); }, querySelectorAll: () => [], querySelector: () => null, contains: () => false }).get(id);
const open = new Set();
const document = { activeElement: null, body: { classList: { contains: c => open.has(c), toggle: (c, on) => on ? open.add(c) : open.delete(c) } } };
const window = { CATALOG: { sources: { humble: "Humble Bundle", ebooks: "Ebooks" } } };
const KINDS = { book: { plural: "Books" }, comic: { plural: "Comics" } };
const ME = null, ONLIST = new Map(), TITLE_ON = new Map(), REPORTED = new Map(), ratingOf = () => [], missing = it => it.c ? [] : ["c"];
const stored = (name, none) => none, store = () => {}, CSS = { escape: s => s }, shownControl = () => true;
const pickedRow = () => {}, closeDrill = () => {};
let mode = "sheet";
const ITEMS = Array.from({ length: 40 }, (_, i) => ({ id: `t${i}`, t: `Lantern Wolf ${i}`, k: i % 2 ? "book" : "comic",
  src: i % 4 ? "humble" : "ebooks", g: [["Fantasy", "Mystery & Thriller", "Cooking"][i % 3]], tg: i % 5 ? [] : ["Knitting"],
  dev: "either", c: i % 7 ? "covers/0123456789abcdef.webp" : "" }));
for (const it of ITEMS) { it.srcs = [it.src]; it._s = 1; }  // as compute leaves them, with no search
"""


def test_the_filters_are_counted_only_while_they_show_and_as_they_open():
    """Every draw of the list counted each filter group over every title, twelve passes in all, while the panel was
    closed too: most of the time a draw took. The groups are counted only while the panel shows now (docked beside the
    titles, or open over them), and as it opens, with what changed meanwhile; the chips above the titles, the Filters
    button's count and the rest are kept up to date all the same. Opened, it shows the same counts as when it showed all
    along."""
    script = FILTERS_PAGE + """
/* part 1 */
/* part 2 */
/* part 3 */
/* part 4 */
/* part 5 */
const countedBy = facetCounts;
let counted = 0;
facetCounts = key => (counted++, countedBy(key));
const fn = (key, v) => (new RegExp(`data-fk="${key}" data-fv="${v}">.*?<span class="fn">([\\\\d,]+)</span>`).exec($("fgroups").innerHTML) || [])[1];
const out = {};
state.f.g.add("Fantasy");
renderFacets();  // a draw with the panel closed, as on a phone
out.closed = { counted, groups: $("fgroups").innerHTML, chips: $("pickedchips").innerHTML.includes('data-fv="Fantasy"'),
               badge: $("fbadge").textContent, label: $("filtersbtn").attrs["aria-label"] };
setOpen(true);  // the Filters button
out.opened = { counted, fantasy: fn("g", "Fantasy"), comics: fn("k", "comic"), html: $("fgroups").innerHTML };
counted = 0;
setOpen(false); setOpen(true);  // nothing changed meanwhile: not counted again
out.again = counted;
renderFacets();  // a draw with it open, as before: counted as it always was
out.always = { counted, same: $("fgroups").innerHTML === out.opened.html };
setOpen(false);
state.f.g.clear(); state.f.k.add("comic");  // ticked above the titles meanwhile
counted = 0;
renderFacets();
out.meanwhile = counted;
setOpen(true);
out.after = { counted, fantasy: fn("g", "Fantasy"), cooking: fn("g", "Cooking"), comics: fn("k", "comic") };
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const FACET = \{", r"// ---- text normalisation"), (r"function passes\(", r"const SHELF_FEW"),
                      (r"const esc = ", r"// where a title's looked-up details came from"),
                      (r"// ---- the filters: groups", r"function renderDrill\(\)"), (r"function setOpen\(on\)", r"function toggleOpen\("))
    assert out["closed"] == {"counted": 0, "groups": "", "chips": True, "badge": 1, "label": "Filters: 1 ticked"}
    assert out["opened"]["counted"] == 12 and out["opened"]["fantasy"] == "14" and out["opened"]["comics"] == "7"  # 14 fantasy, 7 of them comics
    assert out["again"] == 0 and out["always"] == {"counted": 12, "same": True}
    assert out["meanwhile"] == 0
    assert out["after"] == {"counted": 12, "fantasy": "7", "cooking": "7", "comics": "20"}  # the 20 comics' genres now


# A part of the page as far as finding a control again goes: elements with an id, a tag, classes and data- attributes
FOCUS_PAGE = """
const CSS = { escape: s => s };
const document = { activeElement: null };
class E {
  constructor(tag, { id = "", cls = [], data = {} } = {}) {
    Object.assign(this, { tagName: tag.toUpperCase(), id, kids: [], parent: null });
    this.classList = Object.assign([...cls], { contains: c => cls.includes(c) });
    this.attributes = Object.entries(data).map(([k, v]) => ({ name: `data-${k}`, value: v }));
  }
  get isConnected() { let e = this; while (e.parent) e = e.parent; return e === box; }
  add(...kids) { for (const k of kids) { k.parent = this; this.kids.push(k); } return this; }
  all() { return this.kids.flatMap(k => [k, ...k.all()]); }
  contains(x) { return x === this || this.all().includes(x); }
  focus() { document.activeElement = this; }
  matches(sel) {
    const m = sel.match(/^(?:#(.+)|([a-z]*)(?:\\.([\\w-]+))?((?:\\[[^\\]]+\\])*))$/);
    if (m[1]) return this.id === m[1];
    const attrs = [...m[4].matchAll(/\\[([\\w-]+)="([^"]*)"\\]/g)];
    return (!m[2] || this.tagName === m[2].toUpperCase()) && (!m[3] || this.classList.contains(m[3])) && attrs.every(([, k, v]) => this.attributes.some(a => a.name === k && a.value === v));
  }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const box = new E("div", { id: "sheet" });
const tagLine = tags => new E("div", { cls: ["tagline"] }).add(...tags.map(t => new E("button", { cls: ["x"], data: { untag: t } })), new E("input", { cls: ["tagadd"] }));
const stars = () => new E("div", { cls: ["stars"] }).add(...[1, 2, 3, 4, 5].map(n => new E("button", { cls: ["sbtn"], data: { stars: String(n) } })));
const draw = (...kids) => () => { box.kids = []; box.add(...kids); };
"""


def test_a_part_drawn_anew_keeps_the_focus_on_the_same_control():
    """Adding a tag redrew the whole details sheet and the box typed in was gone, so a third tag meant clicking it again
    (and the title's reviews and place were asked for again); giving stars, the heart, saving a review, a pick in Look
    or a filter ticked with the keys dropped the focus too. A part drawn anew keeps it on the same control now, found by
    its id, else its kind, its data- attributes and its place among those like it; gone, on the part's own first, such as
    the tag box. A tag's change draws only the Tags line, and a change of a title's own draws its details without asking
    for its reviews or its place again."""
    script = FOCUS_PAGE + """
/* part 1 */
const out = {};
draw(tagLine(["Knitting"]), stars())();
box.querySelector("input").focus();
keepFocus(box, draw(tagLine(["Knitting", "Lantern night"]), stars()), ".tagadd");
out.typed = [document.activeElement.classList[0], document.activeElement.isConnected];
box.querySelector('button.x[data-untag="Knitting"]').focus();
keepFocus(box, draw(tagLine(["Lantern night"]), stars()), ".tagadd");
out.untagged = [document.activeElement.classList[0], document.activeElement.isConnected];
box.querySelector('button.sbtn[data-stars="4"]').focus();
keepFocus(box, draw(tagLine(["Lantern night"]), stars()));
out.star = [document.activeElement.attributes[0].value, document.activeElement.isConnected];
const outside = new E("button", { id: "elsewhere" });
outside.focus();
keepFocus(box, draw(stars()));
out.outside = document.activeElement.id;
draw(new E("button", { cls: ["chip"], data: { fk: "g", fv: "Fantasy" } }))();
box.querySelector("button").focus();
keepFocus(box, draw(), had => had.attributes[0].value === "g" ? outside : null);  // the last chip taken off: on what the part names
out.lastChip = document.activeElement.id;
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"// A part of the page drawn anew \(draw\)", r"// where a title's looked-up details came from"))
    assert out == {"typed": ["tagadd", True], "untagged": ["tagadd", True], "star": ["4", True], "outside": "elsewhere", "lastChip": "elsewhere"}
    page = (SOURCES / "catalog_page.html").read_text(encoding="utf-8")
    change = page[page.index("async function changeTag("):page.index("function redraw() {")]
    assert "if (SHEET.it) tagsAgain(SHEET.it);" in change and "openItem(" not in change
    again = page[page.index("function tagsAgain(it) {"):page.index("// The owner's alone: a title's open questions")]
    assert "line.outerHTML = tagsHtml(it);" in again and '}, ".tagadd");' in again
    drawn = page[page.index("function itemDrawn(it, again) {"):page.index("function tagsHtml(it) {")]
    assert "if (again) showReviews(it);\n  else loadReviews(it);" in drawn and "if (canRead(it) && !again) askPlace(it);" in drawn
    facets = page[page.index("function renderFacets() {"):page.index("function facetsDrawn() {")]
    assert 'chip($("pickedchips")) || $("filtersbtn")' in facets and 'chip($("fgroups")) || sameRow(had) || $("fpanel")' in facets
    for kept in ("keepFocus($(\"looksheet\"), lookDrawn);",
                 'if (box && SHEET.it) keepFocus($("sheet"), () => {', 'if (row && SHEET.it && $("reader").hidden) keepFocus($("sheet"), () => {'):
        assert kept in page, kept


def test_what_the_sheet_shows_is_one_record_and_back_goes_where_it_says():
    """Which of its views the details sheet showed (a title, a series, a list, your lists, bookmarks, your reviews) was
    guessed from its markup and four variables: 'back' looked for .series-list or ol.lrows, and a redraw for .bmrows,
    which your reviews' list has too, so bookmarks coming drew themselves over it. One record (SHEET) says it now, set
    as each opens: a title opened from a series or a list goes back to it, as far down as it was, and anything else
    closes; the redraws go by its view."""
    script = """
const sheet = { scrollTop: 0 }, detail = { open: false, classList: { add() { detail.open = true; }, remove() { detail.open = false; } } };
const $ = id => id === "sheet" ? sheet : id === "detail" ? detail : { textContent: "", hidden: false, classList: { contains: () => false } };
const closeDialog = id => $(id).classList.remove("open"), document = { querySelector: () => null };
const went = [], drawn = [];
function openSeries(list) { sheetShows("series", { list }); went.push(["series", list.length]); show(); }
function openAList(id) { sheetShows("list", { list: id }); went.push(["list", id]); sheet.scrollTop = 0; show(); }
const BOOKMARKS = [], RD = { it: null }, renderBookmarks = () => drawn.push("bookmarks"), renderList = () => drawn.push("list");
const renderOwnLists = () => drawn.push("lists"), placesKnown = () => {}, readRowAgain = () => {};
/* part 1 */
/* part 2 */
/* part 3 */
const A = { id: "a" }, B = { id: "b" }, out = {};
const step = () => [SHEET.view, SHEET.it && SHEET.it.id, SHEET.back && [SHEET.back.view, SHEET.back.top], detail.open, sheet.scrollTop];
openSeries([A, B]);
went.length = 0;
sheet.scrollTop = 40;
sheetShows("title", { it: A }); out.fromSeries = step();
sheetShows("title", { it: A }); out.again = step();  // drawn anew, or back from reading it
closeSheet(); out.back = [step(), went.splice(0)];
openAList("l1"); went.length = 0; sheet.scrollTop = 25;
sheetShows("title", { it: B }); out.fromList = step();
sheetShows("title", { it: A }); out.another = step();  // from one title's details to another's: no way back
sheetShows("list", { list: "l1" }); sheet.scrollTop = 25; sheetShows("title", { it: B });
closeSheet(); out.backToList = [step(), went.splice(0)];
sheetShows("reviews"); show();
bookmarksShown(); readingShown(); out.reviews = drawn.splice(0);  // your reviews' list: neither redrawn as another view
sheetShows("bookmarks"); bookmarksShown(); sheetShows("lists"); readingShown(); sheetShows("list", { list: "reading" }); readingShown();
out.redrawn = drawn.splice(0);
sheetShows("bookmarks"); sheetShows("title", { it: A }); closeSheet(); out.closed = [step(), went.splice(0)];  // from bookmarks: nothing to go back to
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const SHEET = ", r"// A title's details\. again:"), (r"function bookmarksShown\(", r"function dropBookmark\("),
                      (r"function readingShown\(", r"function listChange\("))
    assert out["fromSeries"] == ["title", "a", ["series", 40], True, 40]
    assert out["again"] == ["title", "a", ["series", 40], True, 40]
    assert out["back"] == [["series", None, None, True, 40], [["series", 2]]]  # the series again, as far down as it was
    assert out["fromList"] == ["title", "b", ["list", 25], True, 25] and out["another"] == ["title", "a", None, True, 25]
    assert out["backToList"] == [["list", None, None, True, 25], [["list", "l1"]]]
    assert out["reviews"] == [] and out["redrawn"] == ["bookmarks", "lists", "list"]
    assert out["closed"] == [["", None, None, False, 25], []]


def test_text_typed_in_a_shelf_row_stays_when_another_row_changes():
    """Each change in the shelf sheet draws every row again from what's kept, and what was typed in a row's box but not
    yet entered was kept nowhere: Add a row, another field, a suggestion or an × in any row emptied every box. What's
    typed is kept with its row now, and drawn back into its box; entered as a value, the box empties, as before."""
    script = """
const els = {}, handlers = {};
const $ = id => els[id] || (els[id] = { id, hidden: false, innerHTML: "", querySelectorAll: () => [], querySelector: () => ({ focus() {} }),
  addEventListener: (kind, f) => { handlers[kind] = f; } });
const SH = { show: [{ key: "a", vals: [] }], hide: [{ key: "a", vals: ["Ann Writer"] }], choices: {} };
const SHELF_FIELDS = { a: "Author" }, SHELF_TEXT = { a: () => [] }, SHELF_TYPED = new Set(["a"]), SHELVES = {};
const CLOSE_X = "×", esc = s => String(s).replace(/"/g, "&quot;"), label = (k, v) => v, norm = s => s.toLowerCase(), shelfChoices = () => [];
const shelfCounted = () => {};
/* part 1 */
const row = (side, i) => ({ dataset: { side, i: String(i) }, querySelector: sel => sel === ".sval" ? box(side, i) : { set innerHTML(v) {} } });
const boxes = {}, box = (side, i) => boxes[`${side}${i}`] || (boxes[`${side}${i}`] = { value: "", classList: { contains: c => c === "sval" }, closest: () => row(side, i) });
const type = (side, i, text) => { box(side, i).value = text; handlers.input({ target: box(side, i) }); };
const press = (side, i, data) => handlers.click({ target: { closest: sel => sel === "button" ? { dataset: data, classList: { contains: () => false }, closest: () => row(side, i) } : null } });
const values = side => [...$(`shelf${side}`).innerHTML.matchAll(/class="sval"[^>]* value="([^"]*)"/g)].map(m => m[1]);
renderShelfRows();
const out = {};
type("show", 0, "Lantern");
press("hide", 0, { val: "0" });  // Ann Writer taken off the other row: every row drawn again
out.kept = { show: values("show"), hide: values("hide") };
addValue(row("show", 0), "Lantern Wolf");  // entered: a value, and the box empty
out.entered = { show: values("show"), vals: SH.show[0].vals };
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function renderShelfRows\(\)", r"// What the server would refuse of a shelf"))
    assert out["kept"] == {"show": ["Lantern"], "hide": [""]}
    assert out["entered"] == {"show": [""], "vals": ["Lantern Wolf"]}


def test_each_filter_group_is_written_once_and_the_rest_of_the_page_takes_it_from_there():
    """A filter group was written out in seven places that had to agree: its name, the ticked values (state.f, where a
    missing one threw), the panel's headings, its plural, the names of its values, the one long line of values() and a
    rating's own order. One table (FACET) has each now, and the rest is made from it as the seven had it; values()
    gives a title's values as before, the fallback for a key of none of them too."""
    script = """
const window = { CATALOG: { sources: { humble: "Humble Bundle", Comics: "Comics" } } };
const KINDS = { book: { label: "Book", plural: "Books" }, comic: { label: "Comic", plural: "Comics" } };
let ME = { by: "rory" };
const readingNow = it => "started", ratingOf = it => ["4", "mine"], missing = it => ["c", "y"];
const ONLIST = new Map([["h:novel", 0]]), TITLE_ON = new Map([["h:novel", ["l1"]]]), REPORTED = new Map([["h:novel", "waiting"]]);
/* part 1 */
/* part 2 */
const it = { id: "h:novel", k: "book", dev: "ereader", srcs: ["humble", "Comics"], g: ["Poetry"], tg: ["Lantern"], kf: "yes", t: "Lantern Wolf" };
const keys = Object.keys(FACET);
const out = { keys, f: Object.entries(state.f).map(([k, v]) => [k, v instanceof Set && !v.size]), clusters: CLUSTERS,
  names: keys.map(k => FACET[k].name), plurals: keys.map(k => FACET[k].plural), order: keys.filter(k => FACET[k].order).map(k => [k, FACET[k].order]),
  values: Object.fromEntries(keys.map(k => [k, values(it, k)])), other: values(it, "t"), bare: [values({ id: "s:7" }, "g"), values({ id: "s:7" }, "kf")],
  labels: [FACET.src.labels.humble, FACET.k.labels.comic, FACET.rt.labels[4], FACET.miss.labels.all, FACET.ls.labels.on] };
ME = null;
out.nobody = values(it, "rd");
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"const FACET = \{", r"// ---- text normalisation"), (r"const CLUSTERS = ", r"const TOP = "))
    keys = ["src", "k", "rd", "rt", "ls", "gm", "g", "tg", "dev", "kf", "miss", "rp"]  # the chips' and the address's order
    assert out["keys"] == keys and out["f"] == [[k, True] for k in keys]  # a set for each, and no more
    assert out["clusters"] == [["Browse", ["k", "rd", "rt", "ls", "gm", "g", "tg"]], ["Source and device", ["src", "dev", "kf"]],
                               ["Housekeeping", ["miss", "rp"]]]
    assert out["names"] == ["Source", "Kind", "Reading", "Rating", "Lists", "Game", "Genre", "Tags", "Device", "E-reader feed",
                            "Missing", "Reported"]
    assert out["plurals"] == ["sources", "kinds", "states", "ratings", "lists", "games", "genres", "tags", "devices", "choices",
                              "details", "states"]
    assert out["order"] == [["rt", ["4", "3", "mine", "unrated", "words"]]]  # in their own order, not by how many
    assert out["values"] == {"src": ["humble", "Comics"], "k": ["book"], "rd": ["started"], "rt": ["4", "mine"], "ls": ["on", "l1"],
                             "gm": [], "g": ["Poetry"], "tg": ["Lantern"], "dev": ["ereader"], "kf": ["yes"], "miss": ["c", "y"],
                             "rp": ["waiting"]}
    assert out["other"] == ["Lantern Wolf"] and out["bare"] == [[], []] and out["nobody"] == []  # none without a login
    assert out["labels"] == ["Humble Bundle", "Comics", "★ 4 and up", "Everything", "My reading list"]


def test_a_tag_with_a_bar_in_it_stays_one_filter_in_the_address_and_an_old_link_s_bars_still_part_its_values():
    """The address bar had a group's chosen values joined with |, so a tag such as "sci-fi|fantasy", which the server
    allows (and must, for it to be removed again), came back from a reload, Back or a shared link as two tags nobody
    chose. Each value is an entry of its own now; an older link's | still parts its values, but for a value a title has."""
    script = """
const FACET = { tg: { name: "Tags" }, g: { name: "Genre" } };
const state = { q: "", sort: "rel", group: true, f: { tg: new Set(), g: new Set() } };
const ITEMS = [{ tg: ["sci-fi|fantasy", "Moonwake"], g: ["Poetry"] }, { tg: ["sci-fi", "fantasy"], g: [] }];
const values = (it, key) => it[key];
let shelfId = "", wantedShelf = "", guideWanted = false, openPending = false;
const location = { hash: "" }, history = { state: null, replaceState(s, t, url) { location.hash = url; } };
const box = {}, $ = id => box[id] || (box[id] = {});
const useShelf = () => {}, openingShelf = () => "", shelfById = () => null;
/* part 1 */
const out = {};
state.f.tg = new Set(["sci-fi|fantasy", "Moonwake"]);
saveHash();
out.hash = location.hash;
state.f.tg = new Set();
loadHash();
out.back = [...state.f.tg];
location.hash = "#tg=Moonwake%7Csci-fi";  // an older link: two tags in one
loadHash();
out.old = [...state.f.tg];
location.hash = "#tg=sci-fi%7Cfantasy";  // a value a title has, whole: as it was chosen
loadHash();
out.whole = [...state.f.tg];
process.stdout.write(JSON.stringify(out));
"""
    out = in_the_page(script, (r"function saveHash\(\)", r"let timer;"))
    assert out["hash"] == "#tg=sci-fi%7Cfantasy&tg=Moonwake"
    assert out["back"] == ["sci-fi|fantasy", "Moonwake"]
    assert out["old"] == ["Moonwake", "sci-fi"]
    assert out["whole"] == ["sci-fi|fantasy"]


# ---- the e-reader feed: PDFs only from known publishers (once among the PDF worker's tests) --------------------------

def title(path, **more):
    return {"id": "t", "k": "book", "src": path.split("/")[0], "f": [[path, path.rsplit(".", 1)[-1], 1000]], **more}


def test_a_pdf_goes_into_the_e_reader_feed_only_from_the_shops():
    with_pdfs = Device(formats=("epub", "pdf"))
    assert with_pdfs.best_file(title("humble/Bundle/a.pdf")) and with_pdfs.best_file(title("storybundle/Bundle/a.pdf"))
    assert with_pdfs.best_file(title("drivethrurpg/Maker/a.pdf"))
    assert with_pdfs.best_file(title("itchio/maker/a.pdf")) is None and with_pdfs.best_file(title("Comics/a.pdf")) is None
    assert with_pdfs.best_file(title("itchio/maker/a.epub"))  # the rule is about PDFs


def test_always_listed_doesnt_bring_in_a_pdf_from_anywhere():
    feed = Device()
    assert not feed.suits(title("itchio/maker/a.pdf", ereader="always"))
    assert feed.suits(title("humble/Bundle/a.pdf", ereader="always"))
    both = title("itchio/maker/a.pdf", ereader="always")
    both["f"].append(["itchio/maker/a.txt", "txt", 10])
    assert feed.best_file(both)[1] == "txt"  # its next best


def test_it_goes_by_where_the_file_came_from_not_the_title():
    merged = title("itchio/maker/a.pdf", ereader="always")
    merged["src"] = "humble"  # a copy from elsewhere, among a Humble title's files
    assert not Device().suits(merged)
    packed = {"id": "t", "k": "book", "src": "itchio", "ereader": "always", "f": [], "z": [["storybundle/b/a.zip/a.pdf", "pdf", 10]]}
    assert Device().suits(packed)


def test_your_settings_can_widen_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text('[ereader_feed]\npdf_from = ["Comics", "humble"]\n', encoding="utf-8")
    feed = ereader_feed()
    assert feed.pdf_from == ("comics", "humble") and feed.suits(title("Comics/a.pdf", ereader="always"))
    assert not feed.suits(title("itchio/a.pdf", ereader="always"))
    paths.settings_file().write_text('[ereader_feed]\npdf_from = ["any"]\n', encoding="utf-8")
    settings.reload()  # as the next command reads them
    assert ereader_feed().suits(title("itchio/a.pdf", ereader="always"))
    settings.reload()  # a command that starts with it: a running server would keep the values from before
    paths.settings_file().write_text('[ereader_feed]\npdf_from = "any"\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="pdf_from"):
        ereader_feed()


def test_pdf_from_goes_by_the_folders_real_name():
    """The catalog's paths are as in an address."""
    feed = Device(pdf_from=("my books", "café"))
    assert feed.suits(title("My%20Books/a.pdf", ereader="always"))
    assert feed.suits(title("CAF%C3%89/a.pdf", ereader="always"))
    assert not feed.suits(title("My%20Other/a.pdf", ereader="always"))


def test_the_e_reader_feed_s_size_limit_counts_a_disk_s_mb():
    """[ereader_feed] max_mb counts a disk's MB (words.py), and its default, 63, keeps the 60 MiB it was: a file of 60
    MiB is still offered, one of 63,000,001 bytes isn't."""
    from hylly import library

    def offered(size: int) -> list | None:
        return library._pick({"f": [["shelf/Moonwake.epub", "epub", size]]}, ("epub",), Device().max_mb)

    assert Device().max_mb == settings.DEFAULTS["ereader_feed"]["max_mb"] == 63
    assert offered(60 * 2**20) and offered(63_000_000) and not offered(63_000_001)
    assert "EPUB, 63.0 MB" in opds.details({"t": "Moonwake"}, ["shelf/Moonwake.epub", "epub", 63_000_000])  # as the feed says it


def test_the_e_reader_feed_s_defaults_are_the_settings_own():
    """The feed's Device takes each default from [ereader_feed] in settings.DEFAULTS, where it is written once."""
    from hylly import library, settings

    feed, said = library.Device(), settings.DEFAULTS["ereader_feed"]
    assert (list(feed.kinds), list(feed.formats), feed.max_mb, feed.skip_tablet, list(feed.pdf_from)) == (
        said["kinds"], said["formats"], said["max_mb"], said["skip_tablet"], said["pdf_from"])

