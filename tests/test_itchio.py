"""itch.io: the survey (the library and the bundles counted by kind, with their files and sizes), claiming
(itchio_survey.py) and downloading on the shops' core (itchio.Library), against a fake API and fake pages shaped like
itch.io's (seen 2026-11-02)."""

from __future__ import annotations

import csv
import functools
import json
from datetime import datetime, timedelta
from pathlib import PurePosixPath

import httpx
import pytest

from hylly import catalog, cli, commands, curation, exits, itchio, itchio_survey, lookups, paths, rules, shopping, snapshot
from hylly import download as dl
from hylly.library import Catalog
from hylly.logins import AuthError, ShopError, load_session, save_session
from hylly.lookups import openlibrary

from helpers import COOKIE, FakeItch, GAMES, ITCHIO_KEY as KEY, ITCH_FILES as FILES_HOST, ItchSite as FakeSite, MB, body
from helpers import CLOCK_TIMES, game, itch, survey, web


def titles_of(items: list[itchio.Item]) -> list[shopping.Title]:
    """The items as the shops' core takes them, those whose files are known (itchio.Library.titles)."""
    return [itchio.title_of(it) for it in items if it.uploads is not None]


def selection(items: list[itchio.Item], manifest, root, *, kinds=itchio.DOWNLOADS, **options) -> shopping.Selection:
    """What a run chooses of these items, as hylly itchio download has the shops' core choose it (cli._shop_download):
    their titles, by the core's options (title, by, limit) and the Library's own choice of the items --types names."""
    with itchio.Library(KEY, state=root / "state", types=kinds) as lib:
        lib.items = {it.game_id: it for it in items}
        return shopping.select("itchio", titles_of(items), manifest, root, chosen=lib.chosen, **options)


def paths_of(sel: shopping.Selection) -> list[str]:
    return [path for _, _, path, _ in sel.pending]


def test_the_library_is_counted_by_kind(tmp_path):
    with itch(FakeItch()) as api:
        items, bundles = itchio_survey.survey(api, tmp_path, log=lambda line: None)
    by_title = {it.title: it for it in items}
    assert len(items) == 7 and bundles == [] and by_title["Sprite Pack"].uploads is None  # software isn't looked into
    assert by_title["Hexcrawl Kit"].group == "tabletop" and by_title["Hexcrawl Kit"].size() == 210 * MB
    assert by_title["Space Blaster"].files() == []  # programs aren't kept
    assert by_title["Pocket Rules"].only_reading() and not by_title["Space Blaster"].only_reading()
    assert by_title["A Field Guide to Moths"].size() == 32 * MB  # traits as an empty dict, as itch.io sends them


def test_the_bundles_are_counted_once_and_the_rest_estimated(tmp_path):
    fake, site = FakeItch(), FakeSite()
    items, bundles = survey(tmp_path, fake, site)
    by_title = {it.title: it for it in items}
    assert [(b.name, b.items, b.claimed, b.gone) for b in bundles] == [("Bundle for Testing", 6, 1, 1), ("Second & Last", 2, 0, 0)]
    assert len(items) == 12  # 7 in the library, and 5 to claim: Ironwood RPG is in both bundles, and counted once
    assert by_title["Hidden Thing"].group == "other" and by_title["Hidden Thing"].uploads is None  # itch.io said no: unknown
    assert by_title["Ironwood RPG"].bundles == ["Bundle for Testing", "Second & Last"]
    assert by_title["Hexcrawl Kit"].bundles == ["Bundle for Testing"] and by_title["Hexcrawl Kit"].claimed
    assert by_title["Tide Pool Tales"].size() == 3 * MB  # free: its files show before claiming
    assert by_title["Ironwood RPG"].uploads is None and by_title["Ironwood RPG"].group == "tabletop"  # paid: they don't
    assert by_title["Neon Drift"].group == "program" and "/games/13" not in fake.asked  # programs aren't asked about
    typical = itchio_survey.estimates(items)
    assert typical["tabletop"] == 105 * MB and itchio_survey.guess(by_title["Ironwood RPG"], typical) == 420 * MB  # 4 files

    text = itchio_survey.report(items, free=157 * 1024 ** 3, bundles=bundles)
    assert "Your bundles: 2" in text and "Every item once: 12 (7 in your library, 5 still to claim in bundles)" in text and "wouldn't describe" in text
    assert "Books, comics and tabletop together: 6 items, about 854.6 MB." in text  # 35 + 50 + 100 + 210 + 420 MiB
    assert "Games and software with programs" in text and "a typical file of its kind" in text
    for f in [p for p in tmp_path.rglob("*") if p.is_file()]:  # neither a bundle's key nor a download page's is kept
        assert "AAAkey111" not in f.read_text(encoding="utf-8") and "SECRETKEY" not in f.read_text(encoding="utf-8")

    fake.asked.clear(), site.asked.clear()
    again, _ = survey(tmp_path, fake, site)
    assert site.asked == ["/my-purchases/bundles"] and not [p for p in fake.asked if p.startswith("/games/")]
    assert {it.title: itchio_survey.guess(it, typical) for it in again} == {it.title: itchio_survey.guess(it, typical) for it in items}


def test_the_list_to_sort(tmp_path):
    items, _ = survey(tmp_path)
    with itchio_survey.write_csv(items, tmp_path / "survey.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["group"] for r in rows] == ["book", "book", "comic", "comic", "tabletop", "tabletop", "other", "other", "game", "game"]
    ironwood = next(r for r in rows if r["title"] == "Ironwood RPG")
    assert (ironwood["claimed"], ironwood["files"], ironwood["MB"], ironwood["MB is"]) == ("", "4", "440.4", "estimate")  # 420 MiB
    hex_kit = next(r for r in rows if r["title"] == "Hexcrawl Kit")
    assert (hex_kit["claimed"], hex_kit["file types"], hex_kit["bundles"]) == ("yes", "PDF ZIP", "Bundle for Testing")
    assert next(r for r in rows if r["title"] == "Drive Zine")["linked elsewhere"] == "1"


def test_the_survey_s_cells_that_would_start_a_formula_get_a_quote_first(tmp_path):
    """Every word in the survey is a stranger's (a creator names the item, the creator and the short text): a cell a
    spreadsheet would take for a formula is written with a ' before it; the numbers stay numbers."""
    odd = itchio.Item(game_id=1, title='=HYPERLINK("http://example.invalid/","Velvet Jinx")', creator="@Ann Writer",
                      classification="book", url="https://lanternwolf.example.invalid/velvet", short_text="-a zine",
                      bundles=["+Moonwake Press Bundle"], uploads=[])
    plain = itchio.Item(game_id=2, title="Velvet Jinx", creator="Ann Writer", classification="book",
                        short_text="\tsoft", uploads=[])
    with itchio_survey.write_csv([odd, plain], tmp_path / "survey.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = {r["url"]: r for r in csv.DictReader(fh)}
    odd_row, plain_row = rows["https://lanternwolf.example.invalid/velvet"], rows[""]
    assert (odd_row["title"], odd_row["creator"], odd_row["description"], odd_row["bundles"]) == (
        "'" + odd.title, "'@Ann Writer", "'-a zine", "'+Moonwake Press Bundle")
    assert plain_row["title"] == "Velvet Jinx" and plain_row["description"] == "'\tsoft" and plain_row["files"] == "0"


def test_the_key_and_the_cookie_are_checked_and_never_shown():
    fake = FakeItch()
    assert itchio.check_key(KEY, transport=httpx.MockTransport(fake.handler)) == "A Reader"
    with pytest.raises(AuthError) as refused:
        itchio.check_key("w" * 40, transport=httpx.MockTransport(fake.handler))
    assert "w" * 40 not in str(refused.value) and "hylly login itchio" in str(refused.value)
    assert save_session(KEY, "itchio").name == "itchio-key.txt" and load_session("itchio") == KEY

    site = FakeSite()
    assert itchio_survey.check_cookie(COOKIE, transport=httpx.MockTransport(site.handler))[1] == 2
    with pytest.raises(AuthError):
        itchio_survey.check_cookie("d" * 30, transport=httpx.MockTransport(site.handler))
    with pytest.raises(AuthError), web(site, "d" * 30) as pages:
        pages.bundles()
    with pytest.raises(ShopError) as down:  # itch.io's own trouble says nothing about the cookie: not a login to give again
        itchio_survey.check_cookie(COOKIE, transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert down.value.status == 503 and not isinstance(down.value, AuthError)
    assert save_session(f"itchio={COOKIE};", "itchio-bundles").name == "itchio-session.txt"
    assert load_session("itchio-bundles") == COOKIE  # pasted as name=value: the value is kept


def test_only_the_survey_so_far(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["itchio"])
    assert e.value.code == 2 and "{survey,claim,download,list}" in capsys.readouterr().err  # it says what it takes


def test_claiming_takes_the_kinds_asked_for_at_a_person_s_pace(tmp_path):
    fake, site = FakeItch(), FakeSite()
    survey(tmp_path, fake, site)
    logged, slept, clock = [], [], [1000.0]

    def sleep(s):
        slept.append(s)
        clock[0] += s

    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, dry_run=True, log=logged.append) == 0
        assert "To claim: 3 items, one every 20 seconds" in logged[-1] and site.claimed == {3}  # a dry run claims nothing
        assert "Not claimed: 1 item whose kind itch.io wouldn't tell" in logged[0]  # Hidden Thing
        done = itchio_survey.claim(api, pages, tmp_path, log=logged.append, sleep=sleep, now=lambda: clock[0])
    assert done == 3 and site.claimed == {3, 11, 12, 14}  # books, comics, tabletop; not the program (13), nor 15
    assert slept == [20, 20]  # never two claims closer than that
    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, log=logged.append, sleep=sleep, now=lambda: clock[0]) == 0  # all done
        itchio_survey.claim(api, pages, tmp_path, groups=None, dry_run=True, log=logged.append)  # --all: the program too
    assert logged[-1].startswith("To claim: 1 item, ")  # still not the item itch.io wouldn't describe
    for f in [p for p in tmp_path.rglob("*") if p.is_file()]:  # neither a bundle's key nor a download page's is kept
        text = f.read_text(encoding="utf-8")
        assert "AAAkey111" not in text and "NEWKEY" not in text


def test_claiming_takes_only_what_the_survey_classified_and_waits_for_the_rest(tmp_path):
    fake, site = FakeItch(), FakeSite()
    fake.hidden.clear()
    real = fake.handler

    def busy(request):  # itch.io describes Tide Pool Tales, then is busy: three in a row, and the survey stops
        game = request.url.path.removeprefix("/games/")
        return httpx.Response(503) if game.isdigit() and game != "11" else real(request)

    fake.handler = busy
    with pytest.raises(ShopError, match="in a row"):
        survey(tmp_path, fake, site)  # 12 and 14 noted as asked again, 15 never asked

    def to_claim(**kw) -> str:
        logged = []
        with itch(fake) as api, web(site) as pages:
            assert itchio_survey.claim(api, pages, tmp_path, dry_run=True, log=logged.append, **kw) == 0
        return "\n".join(logged)

    said = to_claim()
    assert "To claim: 1 item, " in said  # Tide Pool Tales; not the three of unknown kind, as 'other'
    assert "Not claimed until a survey has finished (hylly itchio survey): 3 items whose kind" in said
    assert "To claim: 2 items, " in to_claim(groups=None)  # --all: and the program, by its page; still not those three
    assert "To claim: 0 items" in to_claim(groups=("other",))

    fake.handler, fake.hidden = real, {15}  # the survey finishes; itch.io won't describe Hidden Thing
    survey(tmp_path, fake, site)
    said = to_claim()
    assert "To claim: 3 items, " in said and "until a survey has finished" not in said
    assert "Not claimed: 1 item whose kind itch.io wouldn't tell (hidden or taken down, say)" in said
    assert site.claimed == {3}  # dry runs, all of them


def test_a_claim_itch_io_does_not_take_is_left_for_the_next_run(tmp_path):
    fake, site = FakeItch(), FakeSite()
    fake.hidden.clear()  # every item described, so every one can be claimed
    survey(tmp_path, fake, site)
    site.refuse = {14}
    logged, slept = [], []
    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, log=logged.append, sleep=slept.append) == 3
    assert site.claimed == {3, 11, 12, 15}  # the one after it was claimed all the same
    assert 100 in slept and any("didn't take the claim of 'Paper Moons'" in line for line in logged)  # a longer pause
    assert "didn't take 1: 'Paper Moons'" in logged[-1]
    site.refuse, site.asked = set(), []
    with itch(fake) as api, web(site) as pages:  # the next run tries it once more
        assert itchio_survey.claim(api, pages, tmp_path, log=lambda line: None, sleep=lambda s: None) == 1
    assert site.claimed == {3, 11, 12, 14, 15}


def test_claiming_stops_when_itch_io_takes_none_of_several_in_a_row(tmp_path):
    fake, site = FakeItch(), FakeSite()
    fake.hidden.clear()  # every item described, so every one can be claimed
    survey(tmp_path, fake, site)
    site.refuse = {11, 12, 14}
    with itch(fake) as api, web(site) as pages, pytest.raises(RuntimeError) as stopped:
        itchio_survey.claim(api, pages, tmp_path, log=lambda line: None, sleep=lambda s: None)
    assert "3 claims in a row" in str(stopped.value) and "Paper Moons" in str(stopped.value)
    assert "0 were claimed" in str(stopped.value) and site.claimed == {3}
    site.refuse = {11}
    with itch(fake) as api, web(site) as pages:  # those not taken before go last: the others first
        assert itchio_survey.claim(api, pages, tmp_path, log=lambda line: None, sleep=lambda s: None) == 3
    assert site.claimed == {3, 12, 14, 15}


def test_asking_itch_io_ends_one_way_on_ctrl_c_or_on_trouble_that_stops_it_saying_what_is_kept():
    """A download's listing, a survey and claiming (itchio.stops_saying): Ctrl-C ends it as stopped (130), the trouble
    that stops it (claims itch.io didn't take, or itch.io's own: a ShopError is one) as partly done (1), each saying
    what is kept and that running it again goes on from there; anything else, a disk's say, goes on up as it is."""
    again = "running it again goes on from there."
    with pytest.raises(exits.Exit) as e, itchio.stops_saying():
        raise KeyboardInterrupt
    assert (e.value.code, e.value.message) == (exits.INTERRUPTED, f"Stopped. What was asked so far is kept; {again}")
    with pytest.raises(exits.Exit) as e, itchio.stops_saying("What was claimed is kept"):
        raise RuntimeError("itch.io didn't take 3 claims in a row, so claiming stopped. 2 were claimed in this run.")
    assert (e.value.code, e.value.message) == (exits.PARTLY, "itch.io didn't take 3 claims in a row, so claiming stopped. 2 were "
                                                              f"claimed in this run. What was claimed is kept; {again}")
    with pytest.raises(exits.Exit) as e, itchio.stops_saying():
        raise ShopError("itch.io answered HTTP 503 for your library.", 503)
    assert (e.value.code, e.value.message) == (exits.PARTLY, f"itch.io answered HTTP 503 for your library. What was asked so far is kept; {again}")
    with pytest.raises(OSError), itchio.stops_saying():
        raise OSError("the state folder can't be written")


def download_all(api, fake, root, sel, manifest, items):
    """Every file chosen, downloaded as the core's run downloads it: its job (shopping.make_job), its link asked for
    right before."""
    by_id, files = {it.game_id: it for it in items}, {path: (t, f) for t, f, path, _ in sel.pending}
    downloader = dl.Downloader(root, manifest, refresh=lambda job: api.link(by_id[files[job.path][0].key], files[job.path][1].upload),
                               transport=httpx.MockTransport(fake.handler), log=lambda _: None, backoff=0)
    return downloader.run([shopping.make_job("itchio", *chosen) for chosen in sel.pending])


def test_downloads_the_books_comics_and_tabletop_once(tmp_path):
    fake = FakeItch()
    state = tmp_path / "state"
    with itch(fake) as api:
        items = itchio.library(api, state, log=lambda line: None)
        manifest = dl.Manifest(state.parent, "itchio")
        sel = selection(items, manifest, tmp_path)
        assert paths_of(sel) == [
            "itchio/Ann Writer/A Field Guide to Moths/guide.pdf", "itchio/Ann Writer/A Field Guide to Moths/guide.epub",
            "itchio/Bea Artist/Lantern Comics #1/issue1.cbz",
            "itchio/Cy Maker/Hexcrawl Kit/hexcrawl.pdf", "itchio/Cy Maker/Hexcrawl Kit/maps.zip",
            "itchio/Eve Designer/Pocket Rules/rules.pdf",  # a game whose files are all books: a rulebook
        ]  # not the programs (Space Blaster), the art assets (Sprite Pack) or the zine on Google Drive
        assert sel.titles == 4
        results = download_all(api, fake, tmp_path, sel, manifest, items)
        assert len(results["ok"]) == 6 and not results["failed"]
        assert (tmp_path / "itchio/Eve Designer/Pocket Rules/rules.pdf").read_bytes() == body(500)
        entry = manifest.get("itchio/Bea Artist/Lantern Comics #1/issue1.cbz")
        assert (entry["item"], entry["file"], entry["kind"], entry["device"]) == (2, 200, "comic", "tablet")  # numbers, as ever
        assert len(entry["listed_md5"]) == 32 and entry["listed_size"] == 50 * MB  # as listed: both (File.keep_size)
        assert (entry["publisher"], entry["bundle"]) == ("Bea Artist", "Bea Artist")  # its creator, as every shop's maker
        assert not [k for k in entry if k.startswith("itchio")] and "creator" not in entry
        assert (entry["shop_id"], entry["md5_source"]) == ("itchio:2:200", "shop")  # itch.io's listed MD5 vouched for it
        assert shopping.make_job("itchio", *sel.pending[1]).device == "ereader"  # an EPUB book
        again = selection(itchio.library(api, state, log=lambda line: None), dl.Manifest(manifest.root, manifest.shop), tmp_path)
    assert (again.pending, again.done) == ([], 6)
    for f in [p for p in state.rglob("*") if p.is_file()]:  # neither the key nor a signed link is kept
        text = f.read_text(encoding="utf-8")
        assert "SIGNED" not in text and KEY not in text


def test_a_job_s_device_by_the_shops_rule():
    def device(name: str, mb: float, classification: str = "book") -> str:
        u = itchio.Upload(id=1, filename=name, size=int(mb * MB))
        it = itchio.Item(game_id=1, title="The Lantern Keeper", creator="Ann Writer", classification=classification,
                         uploads=[u])
        return shopping.make_job("itchio", itchio.title_of(it), itchio.File.of(u), f"itchio/Ann Writer/The Lantern Keeper/{name}").device

    assert device("keeper.epub", 2) == "ereader" and device("keeper.epub", 30) == "tablet"
    assert device("keeper.zip", 40) == "ereader"  # a ZIP package is big for its formats, not for its pages
    assert device("keeper.djvu", 2) == "tablet" and device("keeper.zip", 1, "comic") == "tablet"


def test_reading_items_are_those_whose_files_are_all_books():
    def item(*names: str) -> itchio.Item:
        return itchio.Item(game_id=1, title="The Lantern Keeper", creator="Ann Writer", classification="game",
                           uploads=[itchio.Upload(id=n, filename=name) for n, name in enumerate(names)])

    for ext in ("pdf", "epub", "cbz", "cbr", "cb7", "mobi", "azw3", "djvu", "azw", "prc", "fb2"):  # a book's formats
        assert item(f"keeper.{ext}").only_reading(), ext
    assert not item("keeper.zip").only_reading() and not item("keeper.pdf", "keeper.exe").only_reading()


def test_kinds_title_creator_and_limit_choose_items(tmp_path):
    with itch(FakeItch()) as api:
        items = itchio.library(api, tmp_path, log=lambda line: None)
    manifest = dl.Manifest(tmp_path, "itchio")

    def chosen(**kw):
        return sorted({t.name for t, _, _, _ in selection(items, manifest, tmp_path, **kw).pending})

    assert chosen(kinds=("book",)) == ["A Field Guide to Moths"]
    assert chosen(kinds=("game",)) == ["Pocket Rules"]  # games: their files that aren't programs
    assert chosen(title="hex") == ["Hexcrawl Kit"] and chosen(by="eve") == ["Pocket Rules"]  # --creator, as the core has it
    assert len(selection(items, manifest, tmp_path, limit=1).pending) == 2  # one item, both its files


def test_a_link_itch_io_will_not_give(tmp_path):
    fake = FakeItch()
    with itch(fake) as api:
        items = {it.game_id: it for it in itchio.library(api, tmp_path, log=lambda line: None)}
        moths = items[1]
        fake.gone = {100}
        assert api.link(moths, moths.uploads[0]) == ""  # gone from itch.io
        assert api.link(moths, moths.uploads[1]).startswith(f"https://{FILES_HOST}/101?")
        stranger = itchio.Item(game_id=1, title="Not mine", creator="x", key_id=5)
        with pytest.raises(dl.DownloadError) as refused:
            api.link(stranger, moths.uploads[1])
        assert "HTTP 400" in str(refused.value)
    with itch(fake, key="w" * 40) as api, pytest.raises(AuthError):
        api.link(moths, moths.uploads[1])


def downloaded(tmp_path) -> list[itchio.Item]:
    """The fake's library, its books, comics and tabletop games downloaded, and its snapshot saved as a run saves it."""
    state = tmp_path / "state"
    fake = FakeItch()
    with itch(fake) as api:
        items = itchio.library(api, state, log=lambda line: None)
        manifest = dl.Manifest(state.parent, "itchio")
        download_all(api, fake, tmp_path, selection(items, manifest, tmp_path), manifest, items)
    return items


def test_itch_io_s_classifications_are_mapped_to_kinds_once_as_data():
    """An item's own word for its kind is its classification's, as the rules map them beside the kinds
    ([kinds.X] itchio in kinds.toml), where KIND_OF was; the words of its bundles and its creator go over it, as at every
    shop. The download and the catalog read the one mapping."""
    expected = {"book": "book", "comic": "comic", "physical_game": "rpg", "other": "book", "game": "rpg"}
    for classification, kind in expected.items():
        it = itchio.Item(game_id=1, title="Moonwake", creator="Ann Writer", classification=classification, key_id=1,
                         uploads=[itchio.Upload(10, "moonwake.pdf", 30 << 20, "md5-1")])
        t = itchio.title_of(it)
        assert (t.kind, shopping.kind_found("itchio", t)) == (kind, kind), classification
    program = itchio.title_of(itchio.Item(game_id=2, title="Lantern Tool", creator="Ann Writer", classification="tool",
                                          key_id=1, uploads=[]))
    assert (program.kind, shopping.kind_found("itchio", program)) == ("", "book")  # no kind's: its shop's
    assert not hasattr(itchio, "KIND_OF")


def test_the_snapshot_and_the_catalog(tmp_path):
    state = tmp_path / "state"
    items = downloaded(tmp_path)
    snap = shopping.snapshot("itchio", titles_of(items), state)
    assert KEY not in snap.read_text(encoding="utf-8") and "SIGNED" not in snap.read_text(encoding="utf-8")
    found = {i.title: i for i in catalog.itchio_items(tmp_path)}
    assert sorted(found) == ["A Field Guide to Moths", "Hexcrawl Kit", "Lantern Comics #1", "Pocket Rules"]
    moths, kit = found["A Field Guide to Moths"], found["Hexcrawl Kit"]
    assert (moths.id, moths.source, moths.kind, moths.publisher) == ("i:1", "itchio", "book", "Ann Writer")
    assert moths.primary[0].endswith("guide.epub") and moths.description == "About A Field Guide to Moths."
    assert (kit.kind, catalog.device_of(kit), kit.cover_url) == ("rpg", "tablet", "https://img.itch.zone/3.png")
    assert json.loads(snap.read_text(encoding="utf-8"))[0]["kind"] in ("book", "comic", "rpg")


def test_the_catalog_reads_a_snapshot_from_before_itch_io_was_on_the_core_as_it_did(tmp_path):
    """A snapshot itch.io's own code saved (title, creator, group, kind, bundles) gives the catalog the same titles as
    the core's (name, publisher, details and files): the newest is read, whichever made it."""
    state = tmp_path / "state"
    items = downloaded(tmp_path)
    shopping.snapshot("itchio", titles_of(items), state)
    new = catalog.itchio_items(tmp_path)
    for it in items:
        it.bundles = ["Bundle for Testing"]
    old = [{"id": it.game_id, "title": it.title, "creator": it.creator, "group": it.group, "kind": rules.current().itchio_kind(it.group),
            "url": it.url, "cover": it.cover, "description": it.short_text, "published": it.published, "bundles": it.bundles}
           for it in items if it.uploads is not None]  # as itch.io's own snapshot wrote it
    (state / "itchio" / "2099-01-01_0000.json").write_text(json.dumps(old, indent=1), encoding="utf-8")
    assert snapshot.latest(state, "itchio")[0].name == "2099-01-01_0000.json"
    before = catalog.itchio_items(tmp_path)
    shape = lambda i: (i.id, i.title, i.publisher, i.kind, catalog.device_of(i), i.files, i.cover_url, i.description)  # noqa: E731
    assert [shape(i) for i in before] == [shape(i) for i in new] and len(new) == 4
    assert {i.bundle for i in before} == {"Bundle for Testing"} and {i.bundle for i in new} == {""}


def test_the_creator_stands_as_the_author_when_nothing_else_names_one(tmp_path):
    state = tmp_path / "state"
    items = downloaded(tmp_path)
    shopping.snapshot("itchio", titles_of(items), state)
    catalog.build(tmp_path, log=lambda line: None)
    titles = {it["t"]: it for it in Catalog(tmp_path).get()}
    kit = titles["Hexcrawl Kit"]
    assert not catalog.itchio_items(tmp_path)[0].meta.get("authors")  # the fake's files name nobody
    assert kit["a"] == [kit["p"]] and "a" in kit["lf"]
    question = lookups.Query.from_record(kit)
    assert question.authors == [kit["p"]] and "authors" not in question.has  # still asked about, and an answer can fill it in

    source = openlibrary.OpenLibrary()
    cache = tmp_path / "state" / "lookups" / "lookup-openlibrary.jsonl"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": source.key(question), "ids": [kit["id"]], "status": "found", "at": "2026-11-04T10:00:00",
                                 "found": {"authors": ["Bo Writer"], "score": 1.0, "url": "https://openlibrary.org/works/OL1W"}}) + "\n",
                     encoding="utf-8")
    catalog.build(tmp_path, log=lambda line: None)
    kit = {it["t"]: it for it in Catalog(tmp_path).get()}["Hexcrawl Kit"]
    assert kit["a"] == ["Bo Writer"]  # a real answer comes first
    assert source.key(lookups.Query.from_record(kit)) == source.key(question)  # and the question stays the same

    curation.change_single(tmp_path, "authors", "Cy Editor", [kit], by="this PC")
    kit = {it["t"]: it for it in Catalog(tmp_path).get()}["Hexcrawl Kit"]
    assert kit["a"] == ["Cy Editor"] and kit["p"]  # and yours before all


def test_a_creators_name_is_kept_as_it_is_written(tmp_path):
    state = tmp_path / "state"
    items = downloaded(tmp_path)
    for it in items:
        it.creator = "Ann Writer, Moss Press"
    shopping.snapshot("itchio", titles_of(items), state)
    catalog.build(tmp_path, log=lambda line: None)
    kit = {it["t"]: it for it in Catalog(tmp_path).get()}["Hexcrawl Kit"]
    assert kit["a"] == ["Ann Writer, Moss Press"]  # not "Moss Press Ann Writer"


# never taken: scripts, a shortcut, a web page, and a program whose name ends in a dot or a space (Windows drops those,
# which leaves .exe)
LEFT_OUT = {"run.bat": "BAT", "start.lnk": "LNK", "setup.ps1": "PS1", "menu.js": "JS", "index.html": "HTML",
            "Setup.exe.": "EXE", "Setup.exe ": "EXE"}
TAKEN = ["rules.pdf", "issue1.cbz", "maps.zip", "cover.png"]


def item_of(*names: str) -> itchio.Item:
    """A claimed tabletop game with these files."""
    return itchio.Item(game_id=21, key_id=1021, title="Lantern Wolf", creator="Moonwake Press", classification="physical_game",
                       uploads=[itchio.Upload(id=2100 + n, filename=name, size=1000) for n, name in enumerate(names)])


def my_rules(text: str) -> None:
    """A rules file of your own, in the rules folder next to the settings, read as the next command would read it."""
    folder = paths.settings_file().parent / "rules"
    folder.mkdir(exist_ok=True)
    (folder / "mine.toml").write_text(text, encoding="utf-8")
    rules.reset()


def test_only_the_kinds_on_the_list_are_taken_and_the_rest_is_listed(tmp_path):
    sel = selection([item_of(*LEFT_OUT, *TAKEN)], dl.Manifest(tmp_path, "itchio"), tmp_path)
    assert paths_of(sel) == [f"itchio/Moonwake Press/Lantern Wolf/{name}" for name in TAKEN]
    assert {x.name: x.kind.upper() for x in sel.left} == LEFT_OUT and sel.titles == 1
    lines = itchio.left_out(sel.left)  # in itch.io's words: the core asks its Library for them (cli._shop_download)
    assert lines[0] == "Left out, not failures: 7 files (hylly rules show itchio lists the kinds a download takes):"
    assert "  'Lantern Wolf': 'Setup.exe ' (EXE, a program)" in lines  # saved, its space would go: an EXE
    assert "  'Lantern Wolf': 'Setup.exe.' (EXE, a program)" in lines
    assert "  'Lantern Wolf': 'index.html' (HTML, not on the list to fetch)" in lines
    assert [line for line in lines[1:-1] if not any(repr(name) in line for name in LEFT_OUT)] == []  # each named
    assert lines[-1].startswith('To take a kind too, write fetch_add = ["mp3"] under [itchio] in a rules file of your own')
    assert itchio.left_out(selection([item_of(*TAKEN)], dl.Manifest(tmp_path, "itchio"), tmp_path).left) == []


def test_a_program_stays_out_whatever_the_rules_say(tmp_path):
    for media in ("", '[media]\nprograms_drop = ["exe", "bat"]\n', "[media]\ninherit = false\n"):
        my_rules('[itchio]\nfetch_add = ["exe", "bat"]\n' + media)  # on the list, and the rules' programs emptied
        sel = selection([item_of("Setup.exe.", "run.bat")], dl.Manifest(tmp_path, "itchio"), tmp_path)
        assert sel.pending == [] and [x.why for x in sel.left] == [itchio.PROGRAM] * 2, media
    never = sorted(itchio.NEVER)
    my_rules(f'[itchio]\nfetch_add = {json.dumps([k for k in never if "-" not in k])}\n[media]\ninherit = false\n')
    sel = selection([item_of(*(f"file.{k}" for k in never))], dl.Manifest(tmp_path, "itchio"), tmp_path)
    assert sel.pending == [] and [(x.kind, x.why) for x in sel.left] == [(k, itchio.PROGRAM) for k in never]
    my_rules('[media]\nprograms_drop = ["love"]\n')  # a program only by the rules: no longer one, the list keeps it out
    sel = selection([item_of("moonwake.love")], dl.Manifest(tmp_path, "itchio"), tmp_path)
    assert sel.pending == [] and [x.why for x in sel.left] == [itchio.NOT_LISTED]


def test_a_kind_is_judged_by_the_name_it_is_saved_under():
    for name, kind in (("Setup.exe.", "exe"), ("Setup.exe ", "exe"), ("Setup.exe . .", "exe"), ("Notes.PDF", "pdf"),
                       ("notes.pdf:hidden.bat", "bat"), ("README", ""), (".bat", "")):
        assert itchio.kind_of(name) == kind == itchio.Upload(id=1, filename=name).ext, name
        assert paths.safe_filename(name).lower().endswith(f".{kind}") or not kind


def test_a_run_and_a_dry_run_list_what_they_leave_out_and_a_kind_added_is_taken(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(GAMES, 3, ("Hexcrawl Kit", "physical_game", "Cy Maker", [
        ("hexcrawl.pdf", 10 * MB, "hosted", []), ("theme.mp3", 4 * MB, "hosted", []), ("Setup.exe.", 1 * MB, "hosted", [])]))
    fake = FakeItch()
    real = itchio.Itch
    monkeypatch.setattr(itchio, "Itch", lambda key: real(key, transport=httpx.MockTransport(fake.handler), delay=0,
                                                         sleep=lambda s: None))
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=httpx.MockTransport(fake.handler),
                                                            backoff=0))
    monkeypatch.setenv("HYLLY_ITCHIO_KEY", KEY)

    def run_download(*flags: str) -> None:
        cli.main(["itchio", "download", "--types", "tabletop", *flags, "--library", str(tmp_path)])

    run_download("--dry-run")
    out = capsys.readouterr().out
    assert not (tmp_path / "state" / itchio.UPLOADS).exists() and not snapshot.latest(tmp_path / "state", "itchio")  # kept none
    assert "  itchio/Cy Maker/Hexcrawl Kit/hexcrawl.pdf\nLeft out, not failures: 2 files" in out  # after what it takes
    assert "  'Hexcrawl Kit': 'theme.mp3' (MP3, not on the list to fetch)\n" in out
    assert "  'Hexcrawl Kit': 'Setup.exe.' (EXE, a program)\n" in out and "fetch_add" in out

    cli.main(["rules", "copy", "formats"])  # the owner's way: a copy of formats.toml, and the # off one line in it
    copied = paths.settings_file().parent / "rules" / "formats.toml"
    text = copied.read_text(encoding="utf-8")
    assert '[itchio]\n' in text and text.count('\n# fetch_add = ["mp3"]\n') == 1
    copied.write_text(text.replace('\n# fetch_add = ["mp3"]\n', '\nfetch_add = ["mp3"]\n'), encoding="utf-8")
    rules.reset()  # as the next command reads them
    capsys.readouterr()
    run_download()
    out = capsys.readouterr().out
    kit = tmp_path / "itchio" / "Cy Maker" / "Hexcrawl Kit"
    assert sorted(p.name for p in kit.iterdir()) == ["hexcrawl.pdf", "theme.mp3"] and (kit / "theme.mp3").read_bytes() == body(301)
    assert "  'Hexcrawl Kit': 'Setup.exe.' (EXE, a program)\n" in out and "fetch_add" not in out  # a program: no kind to add
    log = (tmp_path / "state" / "download-log.txt").read_text(encoding="utf-8")
    assert "[itchio] Left out, not failures: 1 file " in log and "[itchio]   'Hexcrawl Kit': 'Setup.exe.' (EXE, a program)" in log
    assert "2 files done (" in log and ", 0 failed," in log  # what was left out isn't a failure

    copied.unlink()  # the list as shipped again: the MP3 is left out, and the copy downloaded before stays
    rules.reset()
    run_download("--dry-run")
    out = capsys.readouterr().out
    assert "  'Hexcrawl Kit': 'theme.mp3' (MP3, not on the list to fetch, downloaded before, and still there)\n" in out
    assert (kit / "theme.mp3").read_bytes() == body(301) and "To download: 0 titles, 0 files" in out


def test_a_file_saved_before_under_a_name_not_on_the_list_is_not_fetched_into_it_again(tmp_path):
    manifest = dl.Manifest(tmp_path, "itchio")  # a version before the list fetched it
    manifest.add({"path": "itchio/Moonwake Press/Lantern Wolf/notes.bat", "size": 1000, "item": 21, "file": 2100})
    renamed = item_of("notes.pdf")  # since renamed on itch.io, and the file on the disk gone
    sel = selection([renamed], manifest, tmp_path)
    assert sel.pending == [] and [(x.kind, x.why, x.there) for x in sel.left] == [("bat", itchio.PROGRAM, False)]


def test_a_record_from_before_the_listed_md5_was_kept_comes_again_only_where_itch_io_vouched_for_its_md5(tmp_path):
    """The core asks itch.io's own rule for a file still listed as it was downloaded (File.current, itchio.replaced): a
    record without a listed MD5 goes by its file's MD5 only where itch.io's vouched for it then (md5_source shop), and
    else counts as the same file, so a listing that never matched its file isn't fetched again and again."""
    wolf = item_of("rules.pdf")
    wolf.uploads[0].md5 = "a" * 32  # what itch.io lists now
    path = "itchio/Moonwake Press/Lantern Wolf/rules.pdf"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_bytes(b"x" * 1000)
    for source, fetched in (("transfer", []), ("shop", ["itchio/Moonwake Press/Lantern Wolf/rules (2).pdf"])):
        manifest = dl.Manifest(tmp_path, "itchio")
        manifest.add({"path": path, "size": 1000, "md5": "b" * 32, "md5_source": source, "item": 21, "file": 2100})
        sel = selection([wolf], manifest, tmp_path)
        assert (paths_of(sel), sel.done) == (fetched, 0 if fetched else 1), source


def test_a_file_whose_md5_itch_io_stops_listing_is_still_told_changed_by_its_size(tmp_path):
    """itch.io's records keep the size it listed, an MD5 listed or not (File.keep_size), as they always have: should
    itch.io stop listing a file's MD5, a new size still tells a replaced file, which comes beside the old one. A shop
    whose File doesn't keep it records the size only where no MD5 is listed (DriveThruRPG's, test_shops)."""
    fake = FakeItch()
    with itch(fake) as api:
        items = itchio.library(api, tmp_path / "state", log=lambda line: None)
        manifest = dl.Manifest(tmp_path, "itchio")
        download_all(api, fake, tmp_path, selection(items, manifest, tmp_path, kinds=("comic",)), manifest, items)
    path = "itchio/Bea Artist/Lantern Comics #1/issue1.cbz"
    assert dl.Manifest(tmp_path, "itchio").get(path)["listed_size"] == 50 * MB  # with its MD5 listed too
    comic = next(it.uploads[0] for it in items if it.game_id == 2)
    comic.md5 = ""  # itch.io lists no MD5 for it now
    assert selection(items, dl.Manifest(tmp_path, "itchio"), tmp_path, kinds=("comic",)).done == 1  # the size as it was
    comic.size = 51 * MB  # and then another size
    assert paths_of(selection(items, dl.Manifest(tmp_path, "itchio"), tmp_path, kinds=("comic",))) == [
        "itchio/Bea Artist/Lantern Comics #1/issue1 (2).cbz"]


def test_the_types_of_a_download_and_a_claim_are_checked_and_named_as_itch_io_groups_its_items(tmp_path, capsys):
    """A mistyped --types is a usage error that names the types there are, for a claim too, which took it before as a
    type that chose nothing (commands.comma_list); and those words are itch.io's groups (itchio.GROUPS), as the parser
    may not read itchio.py."""
    for action, also in (("download", "other, game and reading"), ("claim", "other and game")):
        with pytest.raises(SystemExit) as e:
            cli.main(["itchio", action, "--types", "book,novels", "--library", str(tmp_path)])
        said = capsys.readouterr().err
        assert e.value.code == exits.USAGE and f"--types: novels? Use book, comic, tabletop, {also}" in said
    assert set(commands.ITCH_TYPES) == {g for g in itchio.GROUPS.values() if g}
    assert set(itchio.DOWNLOADS) <= {*commands.ITCH_TYPES, "reading"}
    assert set(itchio_survey.CLAIMABLE) <= set(commands.ITCH_TYPES)
    assert cli.build_parser().parse_args(["itchio", "claim", "--types", "Comic, book,comic"]).types == ("comic", "book")


def test_a_library_page_that_never_ends_stops():
    fake = FakeItch()
    real = fake.handler

    def same_page(request):  # itch.io ignoring the page asked for: the same full page every time
        if request.url.path == "/profile/owned-keys":
            return httpx.Response(200, json={"per_page": 2, "owned_keys": [
                {"id": 1000 + i, "game_id": i, "game": game(i)} for i in (1, 2)]})
        return real(request)

    fake.handler = same_page
    with itch(fake) as api, pytest.raises(RuntimeError) as stopped:
        api.owned()
    assert "the same as page 1" in str(stopped.value)
    fake.handler = real
    with itch(fake) as api:
        assert len(api.owned()) == 7 and fake.asked.count("/profile/owned-keys") == 2  # page 2 isn't full: the last


def test_the_survey_stops_when_itch_io_is_busy_and_asks_again_later(tmp_path):
    fake = FakeItch()
    real = fake.handler
    fake.handler = lambda request: (httpx.Response(503) if request.url.path.endswith("/uploads") else real(request))
    with itch(fake) as api, pytest.raises(RuntimeError) as stopped:
        itchio_survey.survey(api, tmp_path, log=lambda line: None)
    assert f"{itchio.FAILS_IN_A_ROW} items in a row" in str(stopped.value)
    fake.handler = real
    with itch(fake) as api:  # the busy ones weren't kept as answers: asked again
        items, _ = itchio_survey.survey(api, tmp_path, log=lambda line: None)
    assert {it.title: it for it in items}["A Field Guide to Moths"].size() == 32 * MB


def test_the_survey_stopping_for_no_answer_still_says_it_was_no_answer(tmp_path):
    """itch.io not answering at all about item after item: the survey's stop keeps that it was no answer (no_answer),
    as the error it stopped for had it."""
    fake = FakeItch()
    real = fake.handler

    def down(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            raise httpx.ConnectError("no network", request=request)
        return real(request)

    fake.handler = down
    with itch(fake) as api, pytest.raises(ShopError) as stopped:
        itchio_survey.survey(api, tmp_path, log=lambda line: None)
    assert "in a row" in str(stopped.value) and stopped.value.no_answer and stopped.value.passing


def test_items_refused_before_dont_stop_a_run_and_are_left_after_three(tmp_path):
    fake, site = FakeItch(), FakeSite()
    fake.hidden.clear()  # every item described, so every one can be claimed
    survey(tmp_path, fake, site)
    site.refuse = {11, 14, 15}
    quiet = {"log": lambda line: None, "sleep": lambda s: None}
    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, **quiet) == 1  # 12; 11, 14 and 15 not taken, never three in a row
    for _ in range(2):  # the same three again, back to back: their trouble, not the site's
        with itch(fake) as api, web(site) as pages:
            assert itchio_survey.claim(api, pages, tmp_path, **quiet) == 0
    logged = []
    with itch(fake) as api, web(site) as pages:  # refused in three runs: left for good, and said so
        assert itchio_survey.claim(api, pages, tmp_path, log=logged.append, sleep=lambda s: None) == 0
    assert any("Left for good: 3 items" in line for line in logged) and "To claim: 0 items" in "\n".join(logged)


def test_the_limit_takes_new_items_before_earlier_refusals(tmp_path):
    fake, site = FakeItch(), FakeSite()
    survey(tmp_path, fake, site)
    site.refuse = {11}
    quiet = {"log": lambda line: None, "sleep": lambda s: None}
    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, limit=1, **quiet) == 0  # 11 first, not taken
    with itch(fake) as api, web(site) as pages:
        assert itchio_survey.claim(api, pages, tmp_path, limit=1, **quiet) == 1  # then a new one, not 11 again
    assert 12 in site.claimed and 11 not in site.claimed


def test_a_download_asks_again_about_an_item_s_files_once_its_answer_is_old(tmp_path, monkeypatch):
    """Creators revise their files. A download takes an item's files from the answers kept while they are fresh,
    asks itch.io again about an item whose answer is KEPT_DAYS old, and about every item with again."""
    fake = FakeItch()
    state = tmp_path / "state"
    uploads = lambda: [p for p in fake.asked if p.endswith("/uploads")]  # noqa: E731
    with itch(fake) as api:
        itchio.library(api, state, log=lambda line: None)  # asks about each item once, and keeps the answers
        first = uploads()
        monkeypatch.setitem(GAMES, 2, ("Lantern Comics #1", "comic", "Bea Artist", [
            ("issue1.cbz", 50 * MB, "hosted", []), ("issue1-errata.pdf", 1 * MB, "hosted", [])]))  # an errata added since
        fake.asked.clear()
        items = {it.game_id: it for it in itchio.library(api, state, log=lambda line: None)}
        assert not uploads() and len(items[2].uploads) == 1  # fresh answers: as kept
        kept = {json.loads(line)["game"]: json.loads(line) for line in (state / itchio.UPLOADS).read_text(encoding="utf-8").splitlines()}
        old = (datetime.now() - timedelta(days=itchio.KEPT_DAYS + itchio.SPREAD_DAYS)).isoformat(timespec="seconds")
        with (state / itchio.UPLOADS).open("a", encoding="utf-8") as fh:  # item 2's answer, as if had three weeks ago
            fh.write(json.dumps({**kept[2], "at": old}) + "\n")
        items = {it.game_id: it for it in itchio.library(api, state, log=lambda line: None)}
        assert uploads() == ["/games/2/uploads"] and [u.filename for u in items[2].uploads] == ["issue1.cbz", "issue1-errata.pdf"]
        fake.asked.clear()
        itchio.library(api, state, log=lambda line: None)
        assert not uploads()  # asked just now: fresh again
        itchio.library(api, state, again=True, log=lambda line: None)
        assert uploads() == first and len(first) > 1  # again: every item, as the first time
    top = next(a for a in commands.build_parser()._actions if a.__class__.__name__ == "_Commands")
    again = next(a for a in commands.actions_of(top.choices["itchio"])["download"]._actions if "--again" in a.option_strings)
    assert itchio.KEPT_DAYS == 14 and "two weeks" in again.help  # its help says as long


def test_a_file_itch_io_no_longer_has_is_asked_about_again_at_once_and_the_failure_says_so(tmp_path, monkeypatch, capsys):
    """A creator replaced a PDF. Its link answers 404, so the run fails it saying what comes next, and the next run
    asks itch.io about the item's files at once, whatever the kept answer's age, and fetches the new file."""
    fake = FakeItch()
    real = itchio.Itch
    monkeypatch.setattr(itchio, "Itch", lambda key: real(key, transport=httpx.MockTransport(fake.handler), delay=0,
                                                         sleep=lambda s: None))
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=httpx.MockTransport(fake.handler),
                                                            backoff=0))
    monkeypatch.setenv("HYLLY_ITCHIO_KEY", KEY)
    run = lambda: cli.main(["itchio", "download", "--types", "book", "--library", str(tmp_path)])  # noqa: E731
    run()
    moths = tmp_path / "itchio" / "Ann Writer" / "A Field Guide to Moths"
    assert sorted(p.name for p in moths.iterdir()) == ["guide.epub", "guide.pdf"]
    monkeypatch.setitem(GAMES, 1, ("A Field Guide to Moths", "book", "Ann Writer", [
        ("the old guide, taken down", 0, "external", []), ("guide.epub", 2 * MB, "hosted", {}),
        ("guide.pdf", 31 * MB, "hosted", [])]))  # the PDF replaced: a new upload, and the old one gone
    fake.gone = {100}
    (moths / "guide.pdf").unlink()  # so the run wants the old one again
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        run()
    assert e.value.code == exits.PARTLY
    with (tmp_path / "state" / "itchio" / "failures.csv").open(encoding="utf-8-sig", newline="") as fh:
        (failed,) = list(csv.DictReader(fh))
    assert failed["path"].endswith("guide.pdf") and itchio.REPLACED in failed["error"]
    assert "next hylly itchio download asks about the item's files again" in capsys.readouterr().err
    old = "itchio/Ann Writer/A Field Guide to Moths/guide.pdf"
    assert dl.Manifest(tmp_path, "itchio").get(old)["gone"]  # its record: a file that won't come
    cli.main(["verify", "--json", "--library", str(tmp_path)])  # ends well: the gone one isn't counted missing
    assert json.loads(capsys.readouterr().out)["problems"] == []
    fake.asked.clear()
    run()  # asked again at once: the new PDF comes, into the old one's place by name, as a new file
    assert "/games/1/uploads" in fake.asked and (moths / "guide (2).pdf").read_bytes() == body(102)
    assert not list(csv.DictReader((tmp_path / "state" / "itchio" / "failures.csv").open(encoding="utf-8-sig", newline="")))
    (title,) = [i for i in catalog.itchio_items(tmp_path) if i.title == "A Field Guide to Moths"]
    assert sorted(PurePosixPath(f[0]).name for f in title.files) == ["guide (2).pdf", "guide.epub"]  # not the gone one


def test_a_file_replaced_under_its_own_upload_comes_beside_the_old_one_and_an_identical_one_is_not_fetched(tmp_path, monkeypatch):
    """A creator may replace a file under the same upload: itch.io then lists another MD5 for it, and the new file
    comes beside the old one, which stays, under a name of its own, said in the run, once. A new upload that is a file
    the item has already (the same MD5) isn't fetched again, and is said to be the same file."""
    fake = FakeItch()
    state = tmp_path / "state"
    moths = tmp_path / "itchio" / "Ann Writer" / "A Field Guide to Moths"
    with itch(fake) as api:
        def chosen():
            items = itchio.library(api, state, again=True, log=lambda line: None)
            return items, selection(items, dl.Manifest(tmp_path, "itchio"), tmp_path, kinds=("book",))

        items, first = chosen()
        download_all(api, fake, tmp_path, first, dl.Manifest(tmp_path, "itchio"), items)
        assert sorted(p.name for p in moths.iterdir()) == ["guide.epub", "guide.pdf"] and not shopping.noted("itchio", first)
        fake.bodies[100] = b"the second edition\n" * 64  # guide.pdf replaced, under the same upload
        monkeypatch.setitem(GAMES, 1, (*GAMES[1][:3], [*GAMES[1][3], ("guide again.epub", 2 * MB, "hosted", {})]))
        fake.bodies[102] = body(101)  # guide.epub uploaded again as it was
        items, second = chosen()
        assert paths_of(second) == ["itchio/Ann Writer/A Field Guide to Moths/guide (2).pdf"] and second.done == 2
        said = "\n".join(shopping.noted("itchio", second))
        assert said.startswith("Changed on itch.io:\n")
        assert "'guide.pdf' was replaced on itch.io: the new one comes as 'guide (2).pdf', beside the old one" in said
        assert "'guide again.epub' is the same file as 'guide.epub', downloaded before: not fetched again" in said
        download_all(api, fake, tmp_path, second, dl.Manifest(tmp_path, "itchio"), items)
        assert (moths / "guide (2).pdf").read_bytes() == fake.bodies[100] and (moths / "guide.pdf").read_bytes() == body(100)
        _, third = chosen()  # nothing fetched twice: the new one is the upload's now
        said = "\n".join(shopping.noted("itchio", third))
        assert third.pending == [] and "replaced" not in said and "'guide again.epub' is the same file" in said  # every run


def test_a_kept_answer_ages_by_its_moment_kept_with_its_offset_or_before_answers_had_one(tmp_path):
    """itch.io's answers are kept with their time's offset (db.now); those kept before have the local time alone, read
    as this computer's then: side by side in one file, each ages by the moment it was had."""
    from datetime import timezone

    day = 86400
    for when, base in CLOCK_TIMES.items():  # asked in summer, in winter, and in the hour the clocks go back
        now = base.astimezone()
        for at in ((base - timedelta(days=2)).isoformat(),  # as kept before
                   (now - timedelta(days=2)).isoformat(),
                   (now - timedelta(days=2)).astimezone(timezone(timedelta(hours=-9))).isoformat()):
            assert itchio._older({"at": at}, day, now) and not itchio._older({"at": at}, 3 * day, now), (when, at)
    now = datetime.now().astimezone()
    assert itchio._older({}, 3 * day, now) and itchio._older({"at": "soon"}, 3 * day, now)  # none, or a wrong one: old
    lines = itchio.Lines(tmp_path / "uploads.jsonl", "game")
    lines.put({"game": 1})
    with (tmp_path / "uploads.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"game": 2, "at": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")}) + "\n")
    rows = itchio.Lines(tmp_path / "uploads.jsonl", "game").rows
    assert datetime.fromisoformat(rows[1]["at"]).utcoffset() is not None
    assert [itchio._older(rows[g], day, now) for g in (1, 2)] == [False, True]


def test_only_the_items_a_run_takes_are_asked_about_and_old_answers_age_on_days_of_their_own(tmp_path):
    """An answer ages after KEPT_DAYS and a few days more by the item's id (SPREAD_DAYS), so a survey's answers
    don't all age on one day; a download asks only about the items it may take (--types, --title, --creator), and of
    the old answers only those of the first --limit items; a dry run lets none age, and keeps nothing."""
    fake = FakeItch()
    state = tmp_path / "state"
    uploads = lambda: sorted(p for p in fake.asked if p.endswith("/uploads"))  # noqa: E731
    item = lambda game_id: itchio.Item(game_id=game_id, title="t", creator="c")  # noqa: E731
    assert [itchio.kept_for(item(i)) / 86400 for i in (7, 1, 6)] == [14, 15, 20]
    with itch(fake) as api:
        itchio.library(api, state, log=lambda line: None)
        kept = {json.loads(line)["game"]: json.loads(line) for line in (state / itchio.UPLOADS).read_text(encoding="utf-8").splitlines()}

        def aged(days: float) -> None:  # every answer, as if had that many days ago
            at = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
            with (state / itchio.UPLOADS).open("a", encoding="utf-8") as fh:
                for row in kept.values():
                    fh.write(json.dumps({**row, "at": at}) + "\n")

        def asked(**kw) -> list[str]:
            fake.asked.clear()
            itchio.library(api, state, log=lambda line: None, **kw)
            return uploads()

        aged(14.5)  # old only for an item whose id adds no days: 7 is the one, but it is a zine on Google Drive
        assert asked(keep=False) == []  # a dry run: as kept
        assert asked(kinds=("other",)) == ["/games/7/uploads"]
        aged(30)
        assert asked(keep=False) == []
        assert asked(title="hex") == ["/games/3/uploads"]  # Hexcrawl Kit alone, of those --title takes
        assert asked(creator="eve", kinds=("game", "reading")) == ["/games/5/uploads"]
        assert asked(limit=1) == ["/games/1/uploads"]  # the first item the run would take: Ann Writer's
        assert asked(limit=1, of_kind=lambda it, row: it.group == "other") == ["/games/7/uploads"]  # --kind's, not Bea's
        lib = itchio.Library("k" * 20, state=state, kinds=frozenset({"rpg"}))  # by the kind the core finds, yours too
        hexkit, moths = (next(it for it in itchio.library(api, state, log=lambda line: None) if it.game_id == n) for n in (3, 1))
        assert (lib._of_kind(hexkit, kept[3]), lib._of_kind(moths, kept[1])) == (True, False)
        lib.yours = {"i:1": "rpg"}
        assert lib._of_kind(moths, kept[1])
        assert asked(title="nothing of the kind") == []
