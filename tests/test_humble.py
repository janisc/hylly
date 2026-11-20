"""Humble's site as its library page reads it (humble.py's docstring): the pages' rows, the list of your purchases page
by page, the login, the page's actions found by name, a purchase's files, the links, and above all the ids: every title
keeps the id, folder and purchase key it had under the order API (remap), so that nothing is downloaded twice. Against
the made-up site (helpers.HumbleSite) and made-up answers alone: no network."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from hylly import catalog, cli, humble, inventory, plan, snapshot
from hylly import download as dl
from hylly.logins import AuthError, ShopError

from helpers import ORDERS, HumbleSite, order, sub

COOKIE = "c" * 80


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(humble, "PAUSE", 0)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def site_client(site: HumbleSite) -> humble.HumbleClient:
    return site.client(COOKIE)


def products(orders: dict) -> list[tuple]:
    """What the plan and the catalog read of orders, each purchase's items as they come (inventory.parse_orders)."""
    return sorted((p.gamekey, p.machine_name, p.title, p.publisher, p.purchased, p.bundle, p.icon,
                   tuple(sorted((f.filename, f.md5, f.size, f.label, f.role) for f in p.files)))
                  for p in inventory.parse_orders(orders)[0])


# ---- the rows ----------------------------------------------------------------------------------------------------------

def test_the_rows_are_read_with_their_texts_references_and_escapes():
    """Next.js's rows: <id>:<JSON> a line each; a text tagged T with its length in bytes, line ends and all; other tags'
    rows (scripts, hints) and lines that aren't rows passed over; "$<id>" (with a :path) and "$@<id>" the row named,
    "$$" a text that begins with "$", "$undefined" none, "$D" a date."""
    text = "Ann Writer’s\nnotes"  # a quote of three bytes, and a line end
    body = (f'0:{{"a":"$@1"}}\n1:{{"t":"$$5 off","n":"$undefined","d":"$D2026-11-20","x":"$2","y":"$2:k:0"}}\n'
            f'2:{{"k":["Lantern Wolf"]}}\n3:T{len(text.encode()):x},{text}4:I[1,["a.js"],"x"]\nnot a row\n'
            '5:HL["/a.css","style"]\n6:E{"digest":"1"}\n').encode()
    table = humble.rows(body)
    assert table["3"] == text and table["4"] is None and table["5"] is None
    assert humble.answer_of(table, "it") == {"t": "$5 off", "n": None, "d": "2026-11-20", "x": {"k": ["Lantern Wolf"]},
                                             "y": "Lantern Wolf"}
    with pytest.raises(ShopError, match="failed to answer for a test") as e:
        humble.answer_of(humble.rows(b'0:{"a":"$@6"}\n6:E{"digest":"1"}\n'), "a test")
    assert e.value.passing  # Humble's own trouble: worth asking again later
    assert humble.find(table, "k") == ["Lantern Wolf"] and humble.find(table, "nothing") is None
    assert humble.plain("$1", {"1": "$1"}) is None  # a row that names itself ends, rather than going round


# ---- the list and the login ----------------------------------------------------------------------------------------------

def test_the_list_is_read_page_by_page_and_ends_at_its_last_an_empty_one_or_one_seen_before(monkeypatch):
    site = HumbleSite(ORDERS)
    monkeypatch.setattr(humble, "LIMIT", 4)
    with site_client(site) as client:
        keys = client.order_keys()
    every = site.items()
    assert keys == list(dict.fromkeys(i["productEntitlementId"] for i in every))  # newest first, each once
    assert sum(len(v) for v in client.listed.values()) == len(every)
    pages = -(-len(every) // 4)
    assert site.asked.count(("action", "filterUserLibrary")) == pages
    assert site.asked[0] == ("GET", "/api/auth/session-status")  # the login first

    class Again(HumbleSite):  # a list that names more pages than it has, and gives its last one again
        def handler(self, request):
            if request.headers.get("next-action") == self.LIST_ID:
                request = httpx.Request("POST", request.url, headers=request.headers,
                                        content=request.content.replace(b'name="_1_page"\r\n\r\n3', b'name="_1_page"\r\n\r\n2'))
            return super().handler(request)

    with Again(ORDERS).client(COOKIE) as client, pytest.raises(ShopError, match="ended early") as e:
        client.order_keys()  # page 3 answered as page 2 again: the list ends there, short of its items
    assert "(8 of 23 items)" in str(e.value)
    empty = HumbleSite({})
    with site_client(empty) as client:
        assert client.order_keys() == [] and empty.asked.count(("action", "filterUserLibrary")) == 1


def test_a_login_humble_doesn_t_take_is_said_so_and_never_shown(capsys, monkeypatch):
    site = HumbleSite(ORDERS, signed_in=False)
    with site_client(site) as client, pytest.raises(AuthError) as e:
        client.order_keys()
    assert "hylly login humble" in str(e.value) and COOKIE not in str(e.value)
    assert "(Humble's answer: anonymous, not signed in.)" in str(e.value)  # how it was turned away, for the owner
    assert site.asked == [("GET", "/api/auth/session-status")]  # nothing else asked with a login Humble doesn't take
    for answer, how in ((httpx.Response(403), "it answered HTTP 403"), (httpx.Response(200, text="<html>"), "a page"),
                        (httpx.Response(302, headers={"location": "https://auth.humblebundle.com/login"}), "sign-in page")):
        with humble.HumbleClient(COOKIE, transport=httpx.MockTransport(lambda request: answer)) as client, \
                pytest.raises(AuthError, match="expired") as e:
            client.check()
        assert how in str(e.value) and COOKIE not in str(e.value)
    monkeypatch.setattr(humble, "HumbleClient", site.client)
    monkeypatch.setattr(cli, "read_clipboard", lambda: COOKIE)
    with pytest.raises(SystemExit):
        cli.main(["login", "humble", "--clipboard"])
    said = capsys.readouterr()
    assert COOKIE not in said.out + said.err and "ory_session_stoiccohens510ht9xek" in said.out + said.err


def test_a_login_humble_takes_is_checked_with_one_question(tmp_path, monkeypatch, capsys):
    site = HumbleSite(ORDERS)
    monkeypatch.setattr(humble, "HumbleClient", site.client)
    monkeypatch.setattr(cli, "read_clipboard", lambda: COOKIE)
    monkeypatch.delenv("HYLLY_HUMBLE_SESSION", raising=False)
    cli.main(["login", "humble", "--clipboard"])
    assert "Humble accepted it: it says you're signed in." in capsys.readouterr().out
    assert site.asked == [("GET", "/api/auth/session-status")]


# ---- the page's actions --------------------------------------------------------------------------------------------------

def test_the_actions_are_found_by_name_in_the_pages_scripts_and_found_again_when_the_site_changes():
    site = HumbleSite(ORDERS)
    with site_client(site) as client:
        client.order_keys()
        assert client.actions.ids["filterUserLibrary"] == HumbleSite.LIST_ID
        read = [p for m, p in site.asked if p.startswith("/_next/")]
        assert read == ["/_next/static/chunks/list.js"]  # the last script the library page lists first: found there
        client.orders(["K1"])  # its page lists the link's script
        client.link_set("K1~novel")
        assert client.actions.ids["getSignedUrlsForEntitlements"] == HumbleSite.LINK_ID
        assert "/home/library/content/K1~novel" not in [p for m, p in site.asked if m == "GET"]  # its page wasn't needed
        before = len(site.asked)
        site.LIST_ID = "60" + "c" * 40  # a new version of Humble's site: the old id answers 404, and is looked for again
        client.order_keys()
        assert client.actions.ids["filterUserLibrary"] == site.LIST_ID
        assert ("GET", "/home/library") in site.asked[before:]

    class Bare(HumbleSite):  # a site whose scripts name no actions
        def handler(self, request):
            if request.url.path.startswith("/_next/"):
                return httpx.Response(200, text="let b=1;")
            return super().handler(request)

    with Bare(ORDERS).client(COOKIE) as client, pytest.raises(ShopError, match="Hylly may need an update"):
        client.order_keys()


def test_an_answer_of_a_shape_it_can_t_read_says_hylly_may_need_an_update():
    """The list, a purchase's page or a link of another shape than the site's: a ShopError that says so, never a
    KeyError or a TypeError; odd fields inside a purchase are taken as none."""
    class Odd(HumbleSite):
        def __init__(self, orders, what):
            super().__init__(orders)
            self.what = what

        def handler(self, request):
            r = super().handler(request)
            path, action = request.url.path, request.headers.get("next-action")
            if self.what == "list" and action == self.LIST_ID:
                return httpx.Response(200, content=b'0:{"a":"$@1"}\n1:{"results":"none"}\n')
            if self.what == "page" and path == "/home/library/K1":
                return httpx.Response(200, content=b'0:{}\n1:{"nothing":"here"}\n')
            if self.what == "fields" and path == "/home/library/K1":
                item = {"title": 5, "entitlements": {"downloads": [{"id": "K1~novel", "downloads": [
                    {"key": 3, "platform": None, "downloadStruct": [{"name": ["EPUB"], "url": "novel.epub", "fileSize": "big"}, 7]},
                    "not a download"]}, 4]}}
                return httpx.Response(200, content=b'0:{}\n1:' + json.dumps({"userLibraryItem": item}).encode() + b"\n")
            if self.what == "link" and action == self.LINK_ID:
                return httpx.Response(200, content=b'0:{"a":"$@1"}\n1:{"ok":true,"data":{"signedUrls":[]}}\n')
            return r

    for what in ("list", "page", "link"):
        with Odd(ORDERS, what).client(COOKIE) as client, pytest.raises(ShopError, match="Hylly may need an update"):
            client.order_keys()
            client.orders(["K1"])
            client.link_set("K1~novel")
    with Odd(ORDERS, "fields").client(COOKIE) as client:
        client.order_keys()
        (got,), _ = client.orders(["K1"])[0].values(), None
        (novel,) = [s for s in got["subproducts"] if s["entitlement"] == "K1~novel"]
        assert novel["downloads"] == [{"machine_name": "", "platform": "", "download_struct": [
            {"name": "", "url": {"web": ""}, "md5": None, "sha1": None, "file_size": 0}]}]
        assert not [p for p in inventory.parse_orders({"K1": got})[0] if p.machine_name == "novel"][0].files  # no file


def test_a_purchase_whose_page_is_gone_is_one_humble_gave_no_data_for_and_one_of_keys_alone_isn_t_asked_for():
    orders = {**ORDERS, "K9": order("K9", "2025-02-01T10:00:00", "Humble Bundle: Keys", [
        {"machine_name": "somekey", "human_name": "A Game's Key", "downloads": []}])}
    site = HumbleSite(orders, answered=set(ORDERS) - {"K3"})
    with site_client(site) as client:
        keys = client.order_keys()
        got, missing = client.orders(keys)
    assert missing == ["K3"] and "K9" in got and ("GET", "/home/library/K9") not in site.asked
    assert [s["machine_name"] for s in got["K9"]["subproducts"]] == ["somekey"]


# ---- the links -----------------------------------------------------------------------------------------------------------

def test_a_link_set_is_asked_for_as_the_title_s_page_asks_and_a_refusal_says_whose_it_is(monkeypatch):
    site = HumbleSite(ORDERS)
    with site_client(site) as client:
        client.order_keys()
        client.orders(["K1"])
        links, lasts = client.link_set("K1~novel")
    assert links == {"novel": {"novel_ebook": {"novel.epub": "https://dl.humble.com/novel.epub?t=fresh1",
                                               "novel.mobi": "https://dl.humble.com/novel.mobi?t=fresh1",
                                               "novel.pdf": "https://dl.humble.com/novel.pdf?t=fresh1"}}}
    assert lasts > 3600
    asked = []
    real = site.handler

    def watch(request):
        if request.headers.get("next-action") == HumbleSite.LINK_ID:
            asked.append((request.url.path, request.headers.get("content-type"),
                          httpx.QueryParams("t=" + request.headers.get("next-router-state-tree", "")).get("t")))
        return real(request)

    site.handler = watch
    with site_client(site) as client:
        client.order_keys()
        client.link_set("K1~novel")
    (path, kind, tree), = asked
    assert path == "/home/library/content/K1~novel" and kind == "text/plain;charset=UTF-8"
    assert json.loads(tree)[1]["children"][0] == "(default)" and '["id","K1~novel","d",null]' in tree  # its page's state

    for answer, signed_in, error, said in (
            ({"ok": False, "code": "not_authenticated"}, False, AuthError, "hylly login humble"),
            ({"ok": False, "code": "not_authenticated"}, True, ShopError, "Hylly may need an update"),
            ({"ok": False, "code": "busy", "retryable": True}, True, ShopError, "this time. Running it again asks again.")):
        class Refusing(HumbleSite):
            def handler(self, request):
                if request.headers.get("next-action") == self.LINK_ID:
                    return httpx.Response(200, content=b'0:{"a":"$@1"}\n1:' + json.dumps(answer).encode() + b"\n")
                return super().handler(request)

        site = Refusing(ORDERS)
        with site_client(site) as client:
            client.order_keys()
            site.signed_in = signed_in
            with pytest.raises(error, match=said) as e:
                client.link_set("K1~novel")
            assert COOKIE not in str(e.value)


def test_a_library_asks_for_an_item_s_links_once_and_again_when_one_was_refused_or_they_run_out(tmp_path, monkeypatch):
    site = HumbleSite(ORDERS)
    monkeypatch.setattr(humble, "HumbleClient", site.client)
    with humble.Library(COOKIE, tmp_path) as lib:
        lib.check()
        titles = {t.key: t for t in lib.titles()}
        novel = titles["novel"]
        epub, others = novel.files[0], novel.files[0].fallbacks
        assert epub.url == "" and lib.link(novel, epub).endswith("novel.epub?t=fresh1")
        assert all(lib.link(novel, f).endswith("?t=fresh1") for f in others) and site.links == 1  # one set for all
        assert lib.link(novel, epub).endswith("novel.epub?t=fresh2") and site.links == 2  # asked again: refused
        assert lib.link(novel, humble.File(key="x", name="gone.epub")) == ""  # one Humble no longer lists
        lib._cache = {k: (time.monotonic() - 1, v) for k, (_, v) in lib._cache.items()}  # run out
        assert lib.link(novel, others[0]).endswith("?t=fresh3")


# ---- the ids: every title as it was ----------------------------------------------------------------------------------------

OLD = {  # orders as the order API gave them, and a snapshot kept them: some machine names with a "-" or capitals
    "AbCdEf1234567890": order("AbCdEf1234567890", "2019-03-01T10:00:00", "Humble Book Bundle: Moonwake Press", [
        sub("lantern-wolf", "Lantern Wolf", "Moonwake Press", [("EPUB", "lantern.epub", 1, "m-lantern")]),
        sub("NightHarbor", "Night Harbor", "Moonwake Press", [("PDF", "harbor.pdf", 2, "m-harbor")]),
        sub("tide_book", "The Tide Book", "Ann Writer", [("EPUB", "tide.epub", 1, "m-tide")])]),
    "ZyXwVu0987654321": order("ZyXwVu0987654321", "2021-05-01T10:00:00", "Humble Book Bundle: Moonwake Press Again", [
        sub("tide_book", "The Tide Book", "Ann Writer", [("EPUB", "tide.epub", 1, "m-tide")]),
        sub("foo_bar_2", "Foo Bar 2", "Ann Writer", [("EPUB", "fb2.epub", 1, "m-fb2")]),
        sub("foobar2", "Foobar Two", "Ann Writer", [("EPUB", "fbtwo.epub", 1, "m-fbtwo")])]),
}


def as_the_site_gives(orders: dict, renamed: dict[str, str], ids: dict[str, str]) -> dict:
    """The orders as Humble's site gives them now (HumbleSite), each purchase under a new id, machine names changed as
    the site changes them (renamed: "-" as "___", capitals small)."""
    site = {}
    for key, o in orders.items():
        new = json.loads(json.dumps(o))
        new["gamekey"] = ids[key]
        for s in new["subproducts"]:
            s["machine_name"] = renamed.get(s["machine_name"], s["machine_name"])
        site[ids[key]] = new
    return site


def test_every_title_keeps_its_id_its_purchase_s_key_and_what_the_site_no_longer_gives():
    """remap, on the first run after the site changed: a machine name the site changed ("-", capitals) back to the one
    the snapshot has, by a file's MD5; each purchase back under its order key, by the items it holds; its date, and its
    items' publisher and cover, from the snapshot. What the plan and the catalog read is then just as before."""
    ids = {"AbCdEf1234567890": "8f0e0000-0000-4000-8000-000000000001", "ZyXwVu0987654321": "8f0e0000-0000-4000-8000-000000000002"}
    site = HumbleSite(as_the_site_gives(OLD, {"lantern-wolf": "lantern___wolf", "NightHarbor": "nightharbor"}, ids))
    with site_client(site) as client:
        got, missing = client.orders(client.order_keys())
    assert not missing and {s["machine_name"] for o in got.values() for s in o["subproducts"]} >= {"lantern___wolf", "nightharbor"}
    assert all(not s["payee"] and not s["icon"] and not o["created"] for o in got.values() for s in o["subproducts"])
    mapped, keys = humble.remap(got, snapshot.sanitize(OLD), "2026-11-20T10:00:00")
    assert keys == {v: k for k, v in ids.items()} and products(mapped) == products(snapshot.sanitize(OLD))
    assert {o["entitlement"] for o in mapped.values()} == set(ids.values())  # kept for the next run
    again, keys = humble.remap(json.loads(json.dumps(got)), mapped, "2026-12-01T10:00:00")  # the next run: by the ids kept
    assert keys == {v: k for k, v in ids.items()} and products(again) == products(mapped)


def test_a_changed_name_is_taken_back_only_by_a_file_of_its_own_or_where_there_is_one_name_alone():
    """Two machine names whose letters and digits are the same (foo_bar_2 and foobar2): a changed one goes to the one
    sharing a file's MD5 with it; one sharing none is a title of its own, as is a new title whose name only looks like
    an old one's; an item with no files at all takes the one name there is."""
    earlier = snapshot.sanitize(OLD)
    renamed = {"foo_bar_2": "Foo_Bar_2", "foobar2": "FooBar2", "lantern-wolf": "lantern___wolf"}
    got = as_the_site_gives(OLD, renamed, {k: k.lower() for k in OLD})
    for o in got.values():
        o["entitlement"] = o["gamekey"]
    new_one = sub("lanternwolf", "Lantern Wolf, the Sequel", "Moonwake Press", [("EPUB", "sequel.epub", 1, "m-sequel")])
    got["zyxwvu0987654321"]["subproducts"].append(new_one)
    mapped, _ = humble.remap(got, earlier, "2026-11-20T10:00:00")
    names = {s["human_name"]: s["machine_name"] for o in mapped.values() for s in o["subproducts"]}
    assert names["Foo Bar 2"] == "foo_bar_2" and names["Foobar Two"] == "foobar2" and names["Lantern Wolf"] == "lantern-wolf"
    assert names["Lantern Wolf, the Sequel"] == "lanternwolf"  # an exact name of its own: never taken for another's
    stranger = {"p1": {"gamekey": "p1", "entitlement": "p1", "created": "", "product": {"human_name": "Other"},
                       "subproducts": [{**sub("lanternwolf2", "Other", "", [("EPUB", "o.epub", 1, "m-other")]),
                                        "machine_name": "Lantern_Wolf", "entitlement": "e1"}]}}
    keys_only = {"p2": {"gamekey": "p2", "entitlement": "p2", "created": "", "product": {"human_name": "Keys"},
                        "subproducts": [{"machine_name": "NIGHTHARBOR", "entitlement": "e2", "downloads": []}]}}
    mapped, keys = humble.remap({**stranger, **keys_only}, earlier, "2026-11-20T10:00:00")
    assert mapped["p1"]["subproducts"][0]["machine_name"] == "Lantern_Wolf"  # no file of lantern-wolf's: its own
    assert mapped["AbCdEf1234567890"]["subproducts"][0]["machine_name"] == "NightHarbor"  # no files: the one name there is
    assert keys == {"p2": "AbCdEf1234567890"}  # p1 holds nothing of an order's: its own id


def test_a_new_purchase_keeps_the_day_it_was_first_seen_and_each_order_key_is_given_once():
    earlier = snapshot.sanitize(OLD)
    both = as_the_site_gives({"AbCdEf1234567890": OLD["AbCdEf1234567890"]}, {}, {"AbCdEf1234567890": "pe-a"})
    split = as_the_site_gives({"AbCdEf1234567890": OLD["AbCdEf1234567890"]}, {}, {"AbCdEf1234567890": "pe-b"})
    split["pe-b"]["subproducts"] = split["pe-b"]["subproducts"][:1]  # the same order's item again, in a second purchase
    fresh = {"pe-new": order("pe-new", "", "Humble Book Bundle: New", [sub("brand_new", "Brand New", "", [("EPUB", "n.epub", 1)])])}
    got = {**both, **split, **fresh}
    for k, o in got.items():
        o["entitlement"], o["created"] = k, ""
        for s in o["subproducts"]:
            s["entitlement"] = f"{k}-{s['machine_name']}"
    mapped, keys = humble.remap(got, earlier, "2026-11-20T10:00:00")
    assert keys == {"pe-a": "AbCdEf1234567890"}  # the one that holds the most of its items; the other keeps its own id
    assert mapped["pe-b"]["created"] == "2026-11-20T10:00:00" and mapped["pe-new"]["created"] == "2026-11-20T10:00:00"
    assert mapped["AbCdEf1234567890"]["created"] == "2019-03-01T10:00:00"
    later, _ = humble.remap(json.loads(json.dumps(got)), mapped, "2026-12-24T10:00:00")
    assert later["pe-new"]["created"] == "2026-11-20T10:00:00"  # the day it was first seen, kept


def test_a_library_downloaded_before_the_site_changed_has_nothing_to_download_and_every_title_as_it_was(tmp_path, monkeypatch, capsys):
    """The whole of it, as the owner's library meets it: titles downloaded under the order API (their records by
    machine name, the snapshot by order key), then Humble's new site, with new ids and some names changed: a dry run
    finds nothing to download, and once a run has saved what it read, the catalog has every title under its old id,
    in its old folder, with its date, publisher and cover; a second run fetches nothing either."""
    root, state = tmp_path, tmp_path / "state"
    snapshot.save(OLD, state, "humble")
    for t in plan.build(OLD).titles:  # every title downloaded as the order API had it
        (root / t.path).parent.mkdir(parents=True, exist_ok=True)
        (root / t.path).write_bytes(b"a book of its own")
        dl.Manifest(root, "humble").add({"path": t.path, "shop_id": t.product.machine_name, "size": 17, "role": "book",
                                         "ext": t.choice.ext, "md5": t.choice.md5})
    before = {i.id: (i.title, i.files, i.publisher, i.bundle, i.purchased, i.cover_url) for i in catalog.humble_items(root)}
    ids = {"AbCdEf1234567890": "8f0e0000-0000-4000-8000-000000000001", "ZyXwVu0987654321": "8f0e0000-0000-4000-8000-000000000002"}
    site = HumbleSite(as_the_site_gives(OLD, {"lantern-wolf": "lantern___wolf", "NightHarbor": "nightharbor",
                                             "foo_bar_2": "foo_bar_2", "foobar2": "foobar2"}, ids),
                      answer=lambda request: pytest.fail("nothing is downloaded"))
    monkeypatch.setattr(humble, "HumbleClient", site.client)
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", COOKIE)
    monkeypatch.setattr(cli, "_check_space", lambda root, need: None)
    cli.main(["humble", "download", "--dry-run", "--json", "--library", str(root)])
    said = json.loads(capsys.readouterr().out)
    assert said["to_download"]["files"] == 0 and said["already_done"] == len(before)
    for _ in range(2):
        cli.main(["humble", "download", "--library", str(root)])
        capsys.readouterr()
        after = {i.id: (i.title, i.files, i.publisher, i.bundle, i.purchased, i.cover_url) for i in catalog.humble_items(root)}
        assert after == before
    assert sorted(snapshot.latest(state, "humble")[1]) == sorted(OLD)  # under their order keys


def site_shape(orders: dict, ids: dict[str, str], renamed: dict[str, str] | None = None) -> dict:
    """The orders as the client reads them from the site (order_of's shape): under the site's purchase ids, each item
    with its entitlement id (purchase~old machine name), machine names changed as the site changes them, no date."""
    out = as_the_site_gives(orders, renamed or {}, ids)
    for key, o in out.items():
        o["entitlement"], o["created"] = key, ""
        for s, before in zip(o["subproducts"], next(v for k, v in orders.items() if ids[k] == key)["subproducts"]):
            s["entitlement"] = f"{key}~{before['machine_name']}"
            s["payee"], s["icon"] = {}, None
    return out


def test_the_next_run_goes_by_the_ids_the_site_gave_and_remap_leaves_what_it_was_given_as_it_was():
    """Once a run has saved the site's ids beside the old ones (entitlement), the next goes by them alone: an item
    whose name the site changed again, and whose file was replaced, keeps its id; a purchase keeps its key and date."""
    ids = {"AbCdEf1234567890": "pe-1", "ZyXwVu0987654321": "pe-2"}
    first = site_shape(OLD, ids, {"lantern-wolf": "lantern___wolf"})
    given = json.loads(json.dumps(first))
    saved, _ = humble.remap(first, snapshot.sanitize(OLD), "2026-11-20T10:00:00")
    assert first == given  # what it was given, as it was
    later = site_shape(OLD, ids, {"lantern-wolf": "lantern_wolf_v2", "NightHarbor": "harbor"})
    for o in later.values():
        for s in o["subproducts"]:
            for d in s["downloads"]:
                for x in d["download_struct"]:
                    x["md5"] = "replaced-" + str(x["md5"])  # every file replaced since: no MD5 to go by
    again, keys = humble.remap(later, snapshot.sanitize(saved), "2026-12-24T10:00:00")
    assert keys == {"pe-1": "AbCdEf1234567890", "pe-2": "ZyXwVu0987654321"}
    names = sorted(s["machine_name"] for o in again.values() for s in o["subproducts"])
    assert names == sorted(s["machine_name"] for o in OLD.values() for s in o["subproducts"])
    assert again["AbCdEf1234567890"]["created"] == "2019-03-01T10:00:00"


def test_a_renamed_item_whose_file_was_replaced_keeps_its_id_by_its_title():
    renamed = site_shape({"AbCdEf1234567890": OLD["AbCdEf1234567890"]}, {"AbCdEf1234567890": "pe-1"},
                         {"lantern-wolf": "lantern___wolf"})
    for s in renamed["pe-1"]["subproducts"]:
        s["entitlement"] = ""  # a first run: no ids saved yet
        for d in s["downloads"]:
            for x in d["download_struct"]:
                x["md5"] = "a newer upload"
    mapped, _ = humble.remap(renamed, snapshot.sanitize(OLD), "2026-11-20T10:00:00")
    assert "lantern-wolf" in {s["machine_name"] for s in mapped["AbCdEf1234567890"]["subproducts"]}
    other = json.loads(json.dumps(renamed))
    other["pe-1"]["subproducts"][0]["human_name"] = "Lantern Wolf Returns"  # the same letters, another title: its own
    mapped, _ = humble.remap(other, snapshot.sanitize(OLD), "2026-11-20T10:00:00")
    assert "lantern___wolf" in {s["machine_name"] for s in mapped["AbCdEf1234567890"]["subproducts"]}


def test_the_bundle_s_name_and_date_are_the_snapshot_s_and_an_older_snapshot_s_ids_count_too(tmp_path):
    """A purchase keeps its bundle's name as the snapshot has it, whatever the site calls it now; and a purchase the
    newest snapshot lacks (a run the site skipped it in) is found in an older one (history), its ids with it."""
    state = tmp_path / "state"
    older = snapshot.save(OLD, state, "humble")
    older.rename(older.with_name("2026-01-01_0000.json"))  # older than the next, saved in the same minute
    snapshot.save({"ZyXwVu0987654321": OLD["ZyXwVu0987654321"]}, state, "humble")
    assert sorted(snapshot.latest(state, "humble")[1]) == ["ZyXwVu0987654321"]
    site = site_shape(OLD, {"AbCdEf1234567890": "pe-1", "ZyXwVu0987654321": "pe-2"}, {"lantern-wolf": "lantern___wolf"})
    site["pe-1"]["product"]["human_name"] = "Humble Book Bundle: Moonwake Press (Renamed)"
    for o in site.values():
        for s in o["subproducts"]:
            s["entitlement"] = ""
    mapped, keys = humble.remap(site, humble.history(state), "2026-11-20T10:00:00")
    assert keys["pe-1"] == "AbCdEf1234567890" and mapped["AbCdEf1234567890"]["created"] == "2019-03-01T10:00:00"
    assert mapped["AbCdEf1234567890"]["product"]["human_name"] == "Humble Book Bundle: Moonwake Press"
    assert "lantern-wolf" in {s["machine_name"] for s in mapped["AbCdEf1234567890"]["subproducts"]}


def test_an_item_its_purchase_s_page_lists_no_files_for_keeps_the_snapshot_s_and_is_said(tmp_path, monkeypatch):
    class Missing(HumbleSite):  # K1's page without its novel
        def handler(self, request):
            r = super().handler(request)
            if request.url.path == "/home/library/K1":
                table = humble.rows(r.content)
                ents = table["5"]
                body = r.content.replace(json.dumps(ents).encode(), json.dumps([e for e in ents if e["id"] != "K1~novel"]).encode())
                return httpx.Response(200, content=body, headers=r.headers)
            return r

    snapshot.save(ORDERS, tmp_path / "state", "humble")
    monkeypatch.setattr(humble, "HumbleClient", Missing(ORDERS).client)
    with humble.Library(COOKIE, tmp_path) as lib:
        lib.check()
        titles = {t.key: t for t in lib.titles()}
    assert lib.unreadable == [("novel", "The Novel")] and "novel" in titles  # said, and kept with its files
    from hylly import shopping

    assert "The Novel" in shopping.unread("humble", lib.unreadable)



def test_an_item_listed_as_a_download_that_never_had_files_is_said_quietly_not_as_unreadable(tmp_path, monkeypatch):
    """The list says it has files, its purchase's page lists none, and no snapshot had any (an audio collection,
    say): nothing to keep or download, and no sign that Humble's site changed, so a line of its own, not the alarm."""
    class Missing(HumbleSite):  # K1's page without its novel, as for an item whose files the page never gives
        def handler(self, request):
            r = super().handler(request)
            if request.url.path == "/home/library/K1":
                table = humble.rows(r.content)
                ents = table["5"]
                body = r.content.replace(json.dumps(ents).encode(), json.dumps([e for e in ents if e["id"] != "K1~novel"]).encode())
                return httpx.Response(200, content=body, headers=r.headers)
            return r

    before = json.loads(json.dumps(ORDERS))
    for s in before["K1"]["subproducts"]:
        if s["machine_name"] == "novel":
            s["downloads"] = []
    snapshot.save(before, tmp_path / "state", "humble")
    said: list[str] = []
    monkeypatch.setattr(humble, "HumbleClient", Missing(ORDERS).client)
    with humble.Library(COOKIE, tmp_path, note=said.append) as lib:
        lib.check()
        lib.titles()
    assert lib.unreadable == []
    assert ("1 item Humble lists as downloads has no files on its purchase's page, nor in any earlier snapshot, so there "
            "is nothing of it to download: The Novel.") in said

def test_a_list_short_of_its_counts_or_ids_or_files_stops_the_run_rather_than_give_a_poorer_library():
    class Short(HumbleSite):
        def __init__(self, orders, change):
            super().__init__(orders)
            self.change = change

        def handler(self, request):
            r = super().handler(request)
            if request.headers.get("next-action") == self.LIST_ID:
                answer = humble.answer_of(humble.rows(r.content), "x")
                self.change(answer)
                return httpx.Response(200, content=b'0:{"a":"$@1"}\n1:' + json.dumps(answer).encode() + b"\n")
            return r

    def no_pages(a):
        del a["pagination"]["totalPages"]

    def no_content_ids(a):
        for i in a["results"]:
            i.pop("contentId")

    def no_files(a):
        for i in a["results"]:
            i["type"], i["deliveryMethods"] = "something_else", ["somehow"]

    def fewer(a):
        a["pagination"]["totalDocs"] += 5

    for change, said in ((no_pages, "Hylly may need an update"), (no_content_ids, "no contentId"),
                         (no_files, "has files to download"), (fewer, "ended early")):
        with Short(ORDERS, change).client(COOKIE) as client, pytest.raises(ShopError, match=said):
            client.order_keys()


def test_a_run_that_finds_most_of_the_titles_gone_stops_before_it_saves_or_downloads_anything(tmp_path, monkeypatch, capsys):
    many_titles = {"K1": order("K1", "2020-01-01T10:00:00", "Humble Book Bundle: Many", [
        sub(f"title_{n}", f"Title {n}", "Moonwake Press", [("EPUB", f"t{n}.epub", 1)]) for n in range(30)])}
    snapshot.save(many_titles, tmp_path / "state", "humble")
    (tmp_path / "state" / "library.json").write_text('{"id": "x"}', encoding="utf-8")
    few = {"K1": order("K1", "2020-01-01T10:00:00", "Humble Book Bundle: Many", [
        sub(f"other_{n}", f"Other {n}", "Moonwake Press", [("EPUB", f"o{n}.epub", 1)]) for n in range(30)])}
    monkeypatch.setattr(humble, "HumbleClient", HumbleSite(few, answer=lambda r: pytest.fail("nothing is fetched")).client)
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", COOKIE)
    before = sorted(p.name for p in (tmp_path / "state" / "humble").iterdir())
    with pytest.raises(SystemExit):
        cli.main(["humble", "download", "--library", str(tmp_path)])
    assert "30 of the 30 titles with files in your newest snapshot aren't in what its site listed" in capsys.readouterr().err
    assert sorted(p.name for p in (tmp_path / "state" / "humble").iterdir()) == before  # no snapshot saved
    assert humble.gone(many_titles, many_titles) is None and humble.gone(many_titles, few) == (30, 30)
    assert humble.gone(many_titles, {}) is None  # only gone, none new: purchases Humble no longer lists, kept back


def test_a_page_asked_for_as_the_router_asks_takes_next_s_check_value_from_the_first_redirect_and_keeps_it():
    """Next.js sends a router's request without its _rsc check value on to the same page with it (307): no refusal of
    the login; the value is kept, so only the first page of a run is asked for twice. A redirect anywhere else is."""
    for value in (HumbleSite.RSC, ""):  # "" for requests with none of the router's headers it goes by, as Humble's is
        site = HumbleSite(ORDERS)
        site.RSC = value
        with site_client(site) as client:
            got, _ = client.orders(client.order_keys())
        assert [x for x in site.asked if x[0] == "redirect"] == [("redirect", "/home/library")] and len(got) == len(ORDERS)
        assert client.rsc == value

    class Elsewhere(HumbleSite):  # its router's requests sent on to the sign-in page: a login it doesn't take
        def handler(self, request):
            if request.headers.get("rsc"):
                return httpx.Response(307, headers={"location": "https://auth.humblebundle.com/login"})
            return super().handler(request)

    with Elsewhere(ORDERS).client(COOKIE) as client, pytest.raises(AuthError, match="expired"):
        client.order_keys()

    class Round(HumbleSite):  # the same page again and again, whatever is asked: Humble's site changed
        def handler(self, request):
            if request.headers.get("rsc"):
                return httpx.Response(307, headers={"location": f"{request.url.path}?_rsc=x{len(self.asked)}"})
            return super().handler(request)

    with Round(ORDERS).client(COOKIE) as client, pytest.raises(ShopError, match="on to itself"):
        client.order_keys()


def test_a_title_renamed_whole_keeps_its_id_by_its_very_file_and_one_the_site_no_longer_lists_keeps_its_place():
    """An item the site names anew (no letters in common to go by), whose file is the very file (its MD5) of one machine
    name the site no longer lists, takes that name back; and the newest snapshot's titles with files that the site
    doesn't list at all stay where they were, in their own purchase or with it whole (keep_back)."""
    site = site_shape(OLD, {"AbCdEf1234567890": "pe-1", "ZyXwVu0987654321": "pe-2"}, {"NightHarbor": "harbour_of_night"})
    for o in site.values():
        for s in o["subproducts"]:
            s["entitlement"] = ""
    site["pe-2"]["subproducts"] = [s for s in site["pe-2"]["subproducts"] if s["machine_name"] != "foobar2"]
    mapped, _ = humble.remap(site, snapshot.sanitize(OLD), "2026-11-20T10:00:00")
    assert "NightHarbor" in {s["machine_name"] for s in mapped["AbCdEf1234567890"]["subproducts"]}
    assert humble.keep_back(mapped, snapshot.sanitize(OLD)) == (1, 0)
    assert "foobar2" in {s["machine_name"] for s in mapped["ZyXwVu0987654321"]["subproducts"]}
    only_one = {"AbCdEf1234567890": mapped["AbCdEf1234567890"]}
    assert humble.keep_back(only_one, snapshot.sanitize(OLD)) == (2, 1) and "ZyXwVu0987654321" in only_one
    titles = lambda orders: {p.machine_name for p in inventory.parse_orders(orders)[0]}  # noqa: E731
    assert titles(only_one) == titles(OLD)  # every title (one bought twice is there by its other purchase)


def test_a_kept_back_purchase_s_key_is_never_another_s_and_its_titles_say_why_they_get_no_link(tmp_path, monkeypatch):
    """A purchase the site no longer lists, put back whole (keep_back), keeps its order key for itself: a later new
    purchase holding one of its titles again gets its own id, date and bundle. A kept-back title not in the library
    yet takes another purchase's link for its very file, else says plainly why it gets none."""
    earlier = snapshot.sanitize(OLD)
    first = site_shape({"ZyXwVu0987654321": OLD["ZyXwVu0987654321"]}, {"ZyXwVu0987654321": "pe-2"})
    saved, _ = humble.remap(first, earlier, "2026-11-20T10:00:00")
    assert humble.keep_back(saved, earlier) == (2, 1) and saved["AbCdEf1234567890"]["kept_back"]
    later = {"pe-9": order("pe-9", "", "Humble Book Bundle: Later", [
        sub("NightHarbor", "Night Harbor", "", [("PDF", "harbor.pdf", 2, "m-harbor")]),
        sub("newbook", "A New Book", "", [("EPUB", "new.epub", 1, "m-new")])])}
    later["pe-9"]["entitlement"] = "pe-9"
    for s in later["pe-9"]["subproducts"]:
        s["entitlement"] = "pe-9~" + s["machine_name"]
    mapped, keys = humble.remap(later, json.loads(json.dumps(saved)), "2027-02-01T10:00:00")
    assert keys == {} and mapped["pe-9"]["created"] == "2027-02-01T10:00:00"  # its own key and date
    assert mapped["pe-9"]["product"]["human_name"] == "Humble Book Bundle: Later"
    snapshot.save(saved, tmp_path / "state", "humble")
    site = HumbleSite({"ZyXwVu0987654321": OLD["ZyXwVu0987654321"]})
    site.orders = {"pe-2": {**OLD["ZyXwVu0987654321"], "gamekey": "pe-2"}}
    monkeypatch.setattr(humble, "HumbleClient", site.client)
    with humble.Library(COOKIE, tmp_path) as lib:
        lib.check()
        titles = {t.key: t for t in lib.titles()}
        assert lib.kept_back == (2, 1) and humble.kept(lib.saved["AbCdEf1234567890"], "NightHarbor")
        with pytest.raises(ShopError, match="no longer lists this title"):
            lib.link(titles["NightHarbor"], titles["NightHarbor"].files[0])


def test_a_purchase_s_page_with_no_list_of_its_files_among_pages_read_is_one_humble_gave_no_data_for():
    """Humble's own error page for one purchase, while the others' pages are read: that purchase is carried over, as a
    page refused is, and counted; but pages so when none could be read, or more than a few, still say its site changed."""
    class Erring(HumbleSite):
        def __init__(self, orders, odd):
            super().__init__(orders)
            self.odd = odd

        def handler(self, request):
            if request.url.path.removeprefix("/home/library/") in self.odd and not request.headers.get("next-action"):
                return httpx.Response(200, content=b'0:{}\n1:{"digest":"an error of its own"}\n')
            return super().handler(request)

    keys_with_files = [k for k in ORDERS if any(s.get("downloads") for s in ORDERS[k]["subproducts"])]
    assert len(keys_with_files) >= 2
    with site_client(Erring(ORDERS, {keys_with_files[-1]})) as client:
        got, missing = client.orders(client.order_keys())
    assert missing == [keys_with_files[-1]] and client.unread_pages == 1 and keys_with_files[0] in got
    with site_client(Erring(ORDERS, set(keys_with_files))) as client, pytest.raises(humble.PageUnread, match="Hylly may need an update"):
        client.orders(client.order_keys())  # none read
    many = {f"K{n}": order(f"K{n}", f"2025-01-{n:02}T10:00:00", f"Bundle {n}",
                           [sub(f"book{n}", f"Book {n}", "Lantern Press", [("EPUB", f"book{n}.epub", 1)])]) for n in range(1, 11)}
    with site_client(Erring(many, {"K2", "K4", "K6", "K8"})) as client, pytest.raises(humble.PageUnread):
        client.orders(client.order_keys())  # four of ten: more than a few

