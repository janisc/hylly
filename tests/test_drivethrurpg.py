"""DriveThruRPG source against a fake API shaped like the real one (2026-10-28), on the shops' core (shopping.py)."""

from __future__ import annotations

import functools
import json
import time

import httpx
import pytest

from hylly import catalog, shopsession
from hylly import download as dl
from hylly import drivethrurpg as dtm
from hylly import shopping, words
from hylly.logins import AuthError, ShopError, load_session, save_session

from helpers import BODIES, DRIVETHRURPG_KEY as KEY, DriveThruRPGAPI as FakeAPI, md5, page_data, product


@pytest.fixture
def api():
    fake = FakeAPI()
    transport = httpx.MockTransport(fake.handler)
    client = dtm.DriveThruRPG(KEY, transport=transport, delay=0, wait=0, backoff=0)
    client.fake, client.transport = fake, transport
    yield client
    client.http.close()


def test_reads_the_library_page_by_page(api, monkeypatch):
    monkeypatch.setattr(dtm, "PAGE_SIZE", 1)
    api.login()
    products = api.products()
    assert [p.name for p in products] == ["Far Stars: Referee Rulebook", "Referee Screen Pack"]
    core = products[0]
    assert (core.id, core.order_id, core.publisher) == ("1001", 1, "Example Games Inc.")
    assert [f.filename for f in core.files] == ["Referee Rulebook.pdf", "Crew Sheet.pdf"]
    assert core.files[0].md5 == md5(BODIES[(1, 0)])  # the newest checksum, not the first
    assert core.files[0].size == len(BODIES[(1, 0)])
    assert (core.games, core.types, core.genres) == (["Traveller"], ["Game Master Guides"], ["Space Opera"])
    assert (core.purchased, core.isbn, core.description) == ("2020-01-02", "9780000000002", "Science fiction roleplaying...")
    assert core.cover == "https://www.drivethrurpg.com/images/2/1001.jpg"


def test_a_product_that_cant_be_read_is_left_out_and_named(api, tmp_path, monkeypatch, capsys):
    from hylly import cli

    api.fake.products += [{**product(3, "Odd Kit", "Example Games Inc.", []), "orderProductId": "three"},  # not a number
                          {**product(4, "Odd Map", "Example Games Inc.", []), "files": [{"filename": "map.pdf"}]},  # no index
                          "not a product at all"]
    api.login()
    assert [p.name for p in api.products()] == ["Far Stars: Referee Rulebook", "Referee Screen Pack"]  # the rest as ever
    assert api.unreadable == [("1003", "Odd Kit"), ("1004", "Odd Map"), ("", "a product without a name")]

    real = dtm.DriveThruRPG
    monkeypatch.setattr(dtm, "DriveThruRPG", lambda key: real(key, transport=api.transport, delay=0, wait=0, backoff=0))
    save_session(KEY, "drivethrurpg")
    cli.main(["drivethrurpg", "download", "--dry-run", "--library", str(tmp_path)])
    said = capsys.readouterr()
    out = said.err + said.out  # the warning on standard error, the count with the result
    assert ("Left out of this run's downloads, as DriveThruRPG lists them in a shape Hylly can't read (3): Odd Kit, Odd Map, "
            "a product without a name. What the library page had of them stays there; if this stays, Hylly may need an update."
            ) in out and "2 titles, 3 files" in out

    rows = [{**product(5, "Odd Atlas", "Example Games Inc.", []), "files": [{"index": 0, "filename": "a.pdf", "size": float("inf")}]},
            {**product(6, "Odd Deck", "Example Games Inc.", []), "productId": True, "bundleId": "ten", "name": 6}, *api.fake.products[:2]]
    monkeypatch.setattr(api, "raw_products", lambda progress=None: rows)  # Infinity, as JSON can say it: int() overflows
    assert len(api.products()) == 2 and api.unreadable == [("1005", "Odd Atlas"), ("", "a product without a name")]

    rows[:] = [{**p, "orderProductId": None} for p in api.fake.products[:2]]
    with pytest.raises(ShopError, match="None of the 2 products in DriveThruRPG's listing could be read"):
        api.products()  # a listing that can't be read at all stops, so no empty list stands for the library


def test_a_product_that_cant_be_read_stays_in_the_catalog_as_it_was(api, tmp_path):
    api.login()
    state = tmp_path / "state"
    manifest = dl.Manifest(tmp_path, "drivethrurpg")
    products = api.products()
    sel = shopping.select("drivethrurpg", products, manifest, tmp_path)
    files = {path: (p, f) for p, f, path, _ in sel.pending}
    dl.Downloader(tmp_path, manifest, refresh=lambda job: api.link(*files[job.path]), transport=api.transport,
                  log=lambda _: None, backoff=0).run([shopping.make_job("drivethrurpg", p, f, path) for p, f, path, _ in sel.pending])
    shopping.snapshot("drivethrurpg", products, state)
    titles = lambda: {i.title: i for i in catalog.reader("drivethrurpg")(tmp_path)}  # noqa: E731 - what the catalog builds from
    assert set(titles()) == {"Far Stars: Referee Rulebook", "Referee Screen Pack"}

    api.fake.products[1] = {**api.fake.products[1], "orderProductId": "two"}  # read fine before, not now
    shopping.snapshot("drivethrurpg", api.products(), state, api.unreadable)
    screen = titles()["Referee Screen Pack"]
    assert (screen.id, screen.publisher, screen.games, screen.description) == ("d:1002", "Example Games Inc.", ["Traveller"],
                                                                              "Science fiction roleplaying...")  # as it was

    api.fake.products[1] = {k: v for k, v in api.fake.products[1].items() if k != "productId"}  # nor its id
    shopping.snapshot("drivethrurpg", api.products(), state, api.unreadable)
    assert api.unreadable == [("", "Referee Screen Pack")] and set(titles()) == {"Far Stars: Referee Rulebook", "Referee Screen Pack"}

    box = tmp_path / "drivethrurpg" / "Other Press" / "Derelict Kit" / "box.pdf"  # downloaded, and in no snapshot
    box.parent.mkdir(parents=True)
    box.write_bytes(b"%PDF-broken")
    manifest.add({"path": box.relative_to(tmp_path).as_posix(), "ext": "pdf", "size": box.stat().st_size, "item": "1009",
                  "publisher": "Other Press"})
    api.fake.products[1] = {**product(9, "Derelict Kit", "Other Press", []), "orderProductId": "nine"}
    shopping.snapshot("drivethrurpg", api.products(), state, api.unreadable, manifest)
    kit = titles()["Derelict Kit"]  # a bare entry: its name, and its file and publisher from the downloads' record
    assert (kit.id, kit.publisher, kit.primary[0]) == ("d:1009", "Other Press", "drivethrurpg/Other Press/Derelict Kit/box.pdf")
    assert "Referee Screen Pack" not in titles()  # gone from the listing, and every id read: gone from the shop, as ever


def test_a_refused_key_is_an_auth_error(api):
    api.key = "wrong" * 5
    with pytest.raises(AuthError, match="did not accept the application key") as e:
        api.login()
    assert "wrong" not in str(e.value)


def test_an_expired_token_is_renewed(api):
    api.login()
    api.fake.expire_token_once = True
    assert len(api.products()) == 2
    assert api.fake.tokens == 2


def answering_first(fake: FakeAPI, answers: list) -> dtm.DriveThruRPG:
    """A client whose library pages DriveThruRPG answers with these first, one each, and then as the fake does."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/order_products") and answers:
            return answers.pop(0)
        return fake.handler(request)

    return dtm.DriveThruRPG(KEY, transport=httpx.MockTransport(handler), delay=0, wait=0, backoff=0)


def test_a_new_token_is_fetched_once_a_request_and_one_refused_too_is_the_key_s_trouble():
    """DriveThruRPG's own way, kept: a 401 mid-run logs in again (the token ran out), once; refused again, the key is."""
    fake, answers = FakeAPI(), [httpx.Response(401)] * 2
    with answering_first(fake, answers) as api:
        api.login()
        with pytest.raises(AuthError, match="did not accept the application key"):
            api.products()
    assert fake.tokens == 2 and not answers  # the first login and one more, and the page asked twice


def test_a_login_s_check_asks_a_400_again_but_says_no_answer_at_once(monkeypatch):
    """hylly login drivethrurpg: one 400 now and then, as its API answers, doesn't refuse a good key (asked again, as in
    a run); no answer at all is said at once, asked once."""
    fake, asked, real = FakeAPI(), [], dtm.DriveThruRPG

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path.endswith("/auth_key") and asked.count(request.url.path) == 1:
            return httpx.Response(400)
        return fake.handler(request)

    monkeypatch.setattr(dtm, "DriveThruRPG", lambda key, **options: real(key, transport=httpx.MockTransport(handler),
                                                                       backoff=0, **options))
    assert dtm.check_key(KEY) == 2 and asked.count("/api/vBeta/auth_key") == 2
    asked.clear()

    def down(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        raise httpx.ConnectError("no network", request=request)

    monkeypatch.setattr(dtm, "DriveThruRPG", lambda key, **options: real(key, transport=httpx.MockTransport(down), **options))
    with pytest.raises(ShopError) as e:
        dtm.check_key(KEY)
    assert e.value.no_answer and len(asked) == 1


def test_drivethrurpg_s_400_now_and_then_is_asked_again_and_its_wait_waited_out(monkeypatch):
    """DriveThruRPG's own way, kept: its API answers 400 now and then and the answer next time, so a 400 is asked
    again, as 429 and 5xx are (only there: the other shops' 400 is an answer). A Retry-After of 1.5 seconds is waited
    out: it used to be read as none."""
    slept, answers = [], [httpx.Response(400, headers={"Retry-After": "1.5"}), httpx.Response(400)]
    monkeypatch.setattr(time, "sleep", slept.append)
    with answering_first(FakeAPI(), answers) as api:
        api.login()
        assert len(api.products()) == 2 and not answers and any(1.4 < s <= 1.5 for s in slept)
        answers += [httpx.Response(400)] * shopsession.TRIES
        with pytest.raises(ShopError) as e:
            api.products()
    assert e.value.status == 400 and not answers


def test_waits_while_a_file_is_prepared(api):
    api.login()
    core = api.products()[0]
    api.fake.preparing[(1, 0)] = 2
    assert api.link(core, core.files[0]).startswith("https://files.example.com/1/0")


def test_downloads_into_publisher_and_product_folders_once(api, tmp_path):
    api.login()
    products = api.products()
    manifest = dl.Manifest(tmp_path, "drivethrurpg")
    sel = shopping.select("drivethrurpg", products, manifest, tmp_path)
    paths = [path for _, _, path, _ in sel.pending]
    assert paths == [
        "drivethrurpg/Example Games Inc/Far Stars - Referee Rulebook/Referee Rulebook.pdf",
        "drivethrurpg/Example Games Inc/Far Stars - Referee Rulebook/Crew Sheet.pdf",
        "drivethrurpg/Example Games Inc/Referee Screen Pack/Referee Screen.pdf",
    ]
    files = {}
    jobs = []
    for p, f, path, replace in sel.pending:
        jobs.append(shopping.make_job("drivethrurpg", p, f, path, replace))
        files[path] = (p, f)
    logged = []
    downloader = dl.Downloader(tmp_path, manifest, refresh=lambda job: api.link(*files[job.path]), transport=api.transport,
                               log=logged.append, backoff=0)
    results = downloader.run(jobs)
    assert len(results["ok"]) == 3 and not results["failed"]
    assert (tmp_path / paths[0]).read_bytes() == BODIES[(1, 0)]
    entry = manifest.get(paths[0])
    assert (entry["item"], entry["file"], entry["md5_source"]) == ("1001", 0, "shop")  # the listed MD5
    assert entry["shop_id"] == "drivethrurpg:1001:0" and entry["listed_md5"] == md5(BODIES[(1, 0)])
    # the record DriveThruRPG's always was, but for the fields every shop names alike now (item, file, listed_md5)
    assert set(entry) == {"path", "shop_id", "title", "bundle", "kind", "device", "role", "label", "ext", "size", "md5", "md5_ok",
                          "md5_source", "last_modified", "downloaded_at", "source", "item", "file", "listed_md5", "publisher"}
    assert (entry["bundle"], entry["publisher"], entry["source"]) == ("Example Games Inc.", "Example Games Inc.", "drivethrurpg")
    assert f"{words.mb(len(BODIES[(1, 0)])):>11}" in "\n".join(logged)  # the log's column of sizes

    again = shopping.select("drivethrurpg", api.products(), dl.Manifest(manifest.root, manifest.shop), tmp_path)
    assert (again.pending, again.done, again.changed) == ([], 3, 0)

    # DriveThruRPG corrects a stale checksum to the one the file already has: still done
    api.fake.products[0]["files"][0]["checksums"].append({"checksum": md5(BODIES[(1, 0)]), "checksumDate": "2026-10-28T00:00:00-05:00"})
    corrected = dl.Manifest(manifest.root, manifest.shop)
    e = corrected.get(paths[0])
    corrected.entries[paths[0]] = {**e, "listed_md5": "f" * 32}  # as if the listing was stale when downloaded
    assert shopping.select("drivethrurpg", api.products(), corrected, tmp_path).changed == 0


def test_a_newer_version_waits_for_update_and_keeps_its_place(api, tmp_path):
    api.login()
    manifest = dl.Manifest(tmp_path, "drivethrurpg")
    sel = shopping.select("drivethrurpg", api.products(), manifest, tmp_path)
    files = {path: (p, f) for p, f, path, _ in sel.pending}
    dl.Downloader(tmp_path, manifest, refresh=lambda job: api.link(*files[job.path]), transport=api.transport,
                  log=lambda _: None, backoff=0).run([shopping.make_job("drivethrurpg", p, f, path) for p, f, path, _ in sel.pending])

    BODIES[(2, 0)] = b"referee screen, second printing " * 80
    try:
        api.fake.products[1] = product(2, "Referee Screen Pack (Revised)", "Example Games Inc.", [(0, "Referee Screen v2.pdf")])
        later = shopping.select("drivethrurpg", api.products(), dl.Manifest(manifest.root, manifest.shop), tmp_path)
        assert (later.pending, later.changed) == ([], 1)
        update = shopping.select("drivethrurpg", api.products(), dl.Manifest(manifest.root, manifest.shop), tmp_path, update=True)
        assert [(path, replace) for _, _, path, replace in update.pending] == [
            ("drivethrurpg/Example Games Inc/Referee Screen Pack/Referee Screen.pdf", True)]  # same folder and name as before
    finally:
        BODIES[(2, 0)] = b"referee screen " * 80


def test_a_program_among_a_product_s_files_is_left_out_and_named_and_one_downloaded_before_stays(api, tmp_path, monkeypatch,
                                                                                                   capsys):
    """The never-list on DriveThruRPG's files too (the shops' core): a character generator as a macro workbook is
    neither asked for nor downloaded, and every run, a dry run too, names it; one an earlier run downloaded stays."""
    from hylly import cli

    monkeypatch.setitem(BODIES, (3, 0), b"%PDF-1.4 starship kit " * 40)
    monkeypatch.setitem(BODIES, (3, 1), b"PK\x03\x04 macros " * 9)
    api.fake.products.append(product(3, "Starship Kit", "Example Games Inc.", [(0, "Kit.pdf"), (1, "Crew Generator.xlsm")]))
    real = dtm.DriveThruRPG
    monkeypatch.setattr(dtm, "DriveThruRPG", lambda key: real(key, transport=api.transport, delay=0, wait=0, backoff=0))
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=api.transport, backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)  # the room on this computer's disk is no part of it
    save_session(KEY, "drivethrurpg")
    named = "  'Starship Kit': 'Crew Generator.xlsm' (XLSM, a program, script or shortcut"
    cli.main(["drivethrurpg", "download", "--dry-run", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert "3 titles, 5 files. To download: 3 titles, 4 files" in out and named + ")\n" in out
    links = api.fake.links
    cli.main(["drivethrurpg", "download", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    kit = tmp_path / "drivethrurpg" / "Example Games Inc" / "Starship Kit"
    assert sorted(p.name for p in kit.iterdir()) == ["Kit.pdf"] and api.fake.links == links + 4 and named + ")\n" in out
    assert named in (tmp_path / "state" / "download-log.txt").read_text(encoding="utf-8")

    (kit / "Crew Generator.xlsm").write_bytes(BODIES[(3, 1)])  # as an earlier version downloaded it
    dl.Manifest(tmp_path, "drivethrurpg").add({"path": "drivethrurpg/Example Games Inc/Starship Kit/Crew Generator.xlsm",
                                               "ext": "xlsm", "size": len(BODIES[(3, 1)]), "item": "1003",
                                               "file": 1})
    cli.main(["drivethrurpg", "download", "--dry-run", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Already done: 4 files." in out and named + ", downloaded before, and still there)\n" in out
    assert (kit / "Crew Generator.xlsm").is_file()  # nothing is deleted


def test_a_run_says_which_older_snapshots_it_removed_in_its_output_its_log_and_its_json(api, tmp_path, monkeypatch, capsys):
    """Thinning, as a run saves its snapshot, is said: which went, on standard output (with --json, standard
    error), in state/download-log.txt with the shop's tag, and in --json."""
    from hylly import cli

    real = dtm.DriveThruRPG
    monkeypatch.setattr(dtm, "DriveThruRPG", lambda key: real(key, transport=api.transport, delay=0, wait=0, backoff=0))
    monkeypatch.setattr(dl, "Downloader", functools.partial(dl.Downloader, transport=api.transport, backoff=0))
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    save_session(KEY, "drivethrurpg")
    folder = tmp_path / "state" / "drivethrurpg"
    folder.mkdir(parents=True)
    old = [f"2025-01-{day:02}_1200.json" for day in range(1, 8)]
    for name in old:
        (folder / name).write_text("[]", encoding="utf-8")
    cli.main(["drivethrurpg", "download", "--library", str(tmp_path), "--json"])
    out = capsys.readouterr()
    said = "Older snapshots thinned: 3 removed from state/drivethrurpg/ (the newest 5 and the newest of each month before " \
           "them stay): 2025-01-01_1200.json, 2025-01-02_1200.json, 2025-01-03_1200.json."
    assert json.loads(out.out)["snapshots_removed"] == old[:3] and said in out.err
    assert f"[drivethrurpg] {said}" in (tmp_path / "state" / "download-log.txt").read_text(encoding="utf-8")
    assert sorted(p.name for p in folder.iterdir())[:4] == old[3:]


def test_title_publisher_and_limit_choose_products(api, tmp_path):
    api.login()
    products = api.products()
    manifest = dl.Manifest(tmp_path, "drivethrurpg")
    assert {p.name for p, *_ in shopping.select("drivethrurpg", products, manifest, tmp_path, title="screen").pending} == {"Referee Screen Pack"}
    assert shopping.select("drivethrurpg", products, manifest, tmp_path, by="nobody").pending == []
    assert len(shopping.select("drivethrurpg", products, manifest, tmp_path, limit=1).pending) == 2  # one product, both its files


def test_snapshot_and_catalog_items(api, tmp_path):
    api.login()
    products = api.products()
    state = tmp_path / "state"
    manifest = dl.Manifest(state.parent, "drivethrurpg")
    sel = shopping.select("drivethrurpg", products, manifest, tmp_path)
    files = {path: (p, f) for p, f, path, _ in sel.pending}
    dl.Downloader(tmp_path, manifest, refresh=lambda job: api.link(*files[job.path]), transport=api.transport,
                  log=lambda _: None, backoff=0).run([shopping.make_job("drivethrurpg", p, f, path) for p, f, path, _ in sel.pending])
    snap = shopping.snapshot("drivethrurpg", products, state)
    assert "files.example.com" not in snap.read_text(encoding="utf-8") and KEY not in snap.read_text(encoding="utf-8")
    items = {i.title: i for i in catalog.reader("drivethrurpg")(tmp_path)}
    core = items["Far Stars: Referee Rulebook"]
    assert (core.id, core.source, core.kind, catalog.device_of(core), core.publisher) == ("d:1001", "drivethrurpg", "rpg", "tablet",
                                                                                         "Example Games Inc.")
    assert core.primary[0].endswith("Referee Rulebook.pdf")  # the biggest file leads
    assert json.loads(snap.read_text(encoding="utf-8"))[0]["files"][0]["md5"] == md5(BODIES[(1, 0)])


def test_the_core_s_snapshot_is_drivethrurpg_s_as_it_was_so_an_old_one_reads_as_before(api, tmp_path):
    """DriveThruRPG's snapshots, made by the shops' core now, hold what they held, field by field, and no order
    number; one written before reads as before in the catalog, the same titles with the same details."""
    api.login()
    products = api.products()
    state = tmp_path / "state"
    old = [{"id": p.id, "name": p.name, "publisher": p.publisher, "modified": p.modified, "cover": p.cover,
            "purchased": p.purchased, "isbn": p.isbn, "description": p.description, "games": p.games, "types": p.types,
            "genres": p.genres, "bundle": p.bundle,
            "files": [{"index": f.index, "filename": f.filename, "md5": f.md5, "size": f.size} for f in p.files]}
           for p in products]  # as drivethrurpg.snapshot wrote them before the core
    snap = shopping.snapshot("drivethrurpg", products, state)
    assert json.loads(snap.read_text(encoding="utf-8")) == old and '"order_id"' not in snap.read_text(encoding="utf-8")
    manifest = dl.Manifest(tmp_path, "drivethrurpg")
    sel = shopping.select("drivethrurpg", products, manifest, tmp_path)
    files = {path: (p, f) for p, f, path, _ in sel.pending}
    dl.Downloader(tmp_path, manifest, refresh=lambda job: api.link(*files[job.path]), transport=api.transport,
                  log=lambda _: None, backoff=0).run([shopping.make_job("drivethrurpg", p, f, path) for p, f, path, _ in sel.pending])
    now = catalog.reader("drivethrurpg")(tmp_path)
    snap.unlink()
    (state / "drivethrurpg" / "2026-11-01_1200.json").write_text(json.dumps(old, indent=1), encoding="utf-8")  # one from before
    assert catalog.reader("drivethrurpg")(tmp_path) == now and len(now) == 2


def test_the_catalog_and_the_feed_know_the_game(api, tmp_path):
    from hylly import opds
    from hylly.library import Catalog

    api.login()
    products = api.products()
    state = tmp_path / "state"
    manifest = dl.Manifest(state.parent, "drivethrurpg")
    sel = shopping.select("drivethrurpg", products, manifest, tmp_path)
    files = {path: (p, f) for p, f, path, _ in sel.pending}
    dl.Downloader(tmp_path, manifest, refresh=lambda job: api.link(*files[job.path]), transport=api.transport,
                  log=lambda _: None, backoff=0).run([shopping.make_job("drivethrurpg", p, f, path) for p, f, path, _ in sel.pending])
    snap = shopping.snapshot("drivethrurpg", products, state)
    data = json.loads(snap.read_text(encoding="utf-8"))  # and a Mothership product, as if downloaded too
    data.append({**data[0], "id": "1003", "name": "Mothership Derelict Kit", "publisher": "Other Press", "games": ["Mothership 1e"]})
    snap.write_text(json.dumps(data), encoding="utf-8")
    box = tmp_path / "drivethrurpg" / "Other Press" / "Mothership Derelict Kit" / "box.pdf"
    box.parent.mkdir(parents=True)
    box.write_bytes(b"%PDF-broken")
    manifest.add({"path": box.relative_to(tmp_path).as_posix(), "ext": "pdf", "size": box.stat().st_size, "item": "1003"})

    catalog.build(tmp_path, log=lambda s: None)
    recs = {r["t"]: r for r in page_data(tmp_path)["items"]}
    core = recs["Far Stars: Referee Rulebook"]
    assert (core["src"], core["k"], core["gm"], core["dev"]) == ("drivethrurpg", "rpg", ["Traveller"], "tablet")
    assert "Game Master Guides" in core["sub"] and core["d"].startswith("Science fiction")
    assert recs["Mothership Derelict Kit"]["gm"] == ["Mothership"]  # the shop's "Mothership 1e", as the filter names it

    cat = Catalog(tmp_path)
    assert "By game" in opds.answer(cat, "/opds")[2]
    games = opds.answer(cat, "/opds/game")[2]
    assert "Traveller" in games and "2 titles" in games
    listing = opds.answer(cat, "/opds/game/Traveller")[2]
    assert "Referee Rulebook" in listing and "Referee Screen Pack" in listing and "Derelict Kit" not in listing
    assert [i["t"] for i in cat.search("traveller screen")] == ["Referee Screen Pack"]


def test_the_key_is_saved_like_a_cookie():
    path = save_session(KEY, "drivethrurpg")
    assert path.name == "drivethrurpg-key.txt"
    assert load_session("drivethrurpg") == KEY
    with pytest.raises(AuthError, match="application key"):
        save_session("too short", "drivethrurpg")
