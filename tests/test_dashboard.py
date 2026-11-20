"""The server page (/server): only for an owner's login, from anywhere; buttons only with the page's key, and what it
reports."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading

import httpx
import pytest

from hylly import catalog, dashboard, instance, serve

from helpers import build_library


@pytest.fixture
def running(library, serving):
    srv, client = serving(library)
    return client, srv, srv.serving_thread


def test_page_and_status(running):
    client, srv, _ = running
    client.get("/opds")
    client.get("/ping")  # the catalog page's keep-alive is not a use worth counting
    page = client.get("/server")
    assert page.status_code == 200 and srv.RequestHandlerClass.server_key in page.text and page.headers["cache-control"] == "no-store"
    s = client.get("/server/status").json()
    assert s["requests"] == 1 and s["clients"][0]["you"] and s["clients"][0]["n"] == 1
    assert s["titles"] > 0 and s["memory_mb"] > 0 and "idle_minutes" not in s  # it never stops by itself
    assert s["listen"] == "127.0.0.1" and s["browser"] is s["opds"] is s["ereader"] is None  # this computer only
    assert client.get("/server", headers={"Host": "rebound.example"}).status_code == 421  # not one of its names
    assert client.get("/server/status", headers={"Host": "rebound.example"}).status_code == 421
    home = {"Host": "192.168.1.5:8770", "X-Forwarded-For": "203.0.113.9"}  # the owner at another device, or through a tunnel
    assert client.get("/server", headers=home).status_code == 200 and client.get("/server/status", headers=home).status_code == 200


def test_the_page_names_no_networks_and_says_every_request_logs_in(running, log_in):
    """Who can come in no longer hangs on where a request comes from: the page names no home networks, and no login
    setting, only the users. Anyone but the owner is told it's the owner's (403)."""
    client, srv, _ = running
    s = client.get("/server/status").json()
    assert "home" not in s and "login" not in s and s["users"] == ["owner (owner)"]
    page = client.get("/server").text
    assert 'id="home"' not in page and "home_networks" not in page and "door left open" not in page
    assert "Always needed · users:" in page and "Only the library's owners see this page." in page
    assert 'location.assign("/login?" + new URLSearchParams({next: location.pathname}))' in page  # a login ended: to the login page
    with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}"), "librarian") as librarian:
        assert librarian.get("/server").status_code == 403 and librarian.get("/server/status").status_code == 403
        assert librarian.get("/server").text == "The Server page is for the library's owner."


def test_the_page_shows_a_browsers_address_first_then_the_reading_apps(tmp_path, log_in):
    """For other devices, the library page's address for a browser, then the feeds for the reading apps: at the
    address people use ([server] address), else at this computer's own on the home network while it listens there."""
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    serve_and_home = ("0.0.0.0", "https://testpc.tail1234.ts.net", None)  # an e-reader without Tailscale too: the plain one
    for listen, address, base in (("0.0.0.0", "", "http://192.168.1.5"), ("127.0.0.1", "https://testpc.tail1234.ts.net", None),
                                  serve_and_home):
        srv, _ = serve.make_server(tmp_path, listen, 0, address=address)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
                s = client.get("/server/status").json()
                shown = f"{base}:{srv.server_port}" if base else address
                assert s["listen"] == listen and s["pc"] == f"http://127.0.0.1:{srv.server_port}/"
                assert (s["browser"], s["opds"], s["ereader"]) == (f"{shown}/", f"{shown}/opds", f"{shown}/ereader/opds")
                at_home = f"http://192.168.1.5:{srv.server_port}/" if (listen, address, base) == serve_and_home else None
                assert s["at_home"] == at_home  # only with address set, while it listens beyond this computer
                page = client.get("/server").text
                assert page.index("<td>In a browser</td>") < page.index("<td>Reading apps (OPDS)</td>") < page.index("<td>E-readers (OPDS)</td>")
                assert page.index("<td>E-readers (OPDS)</td>") < page.index('<tr id="at_home_row" hidden><td>Home network</td>')
                assert 'id="lan"' not in page and "lan = true" not in page and "<code>listen</code>, <code>address</code>" in page
        finally:
            srv.shutdown()
            srv.server_close()


def test_the_page_tells_of_what_is_too_old(running, monkeypatch):
    from hylly import updates

    client, _, _ = running
    updates.behind_once.cache_clear()
    monkeypatch.setattr(updates, "behind", lambda: [{"what": "7-Zip", "have": "16.02", "want": "26.02", "why": "Flaws.", "next": "Get it."}])
    assert client.get("/server/status").json()["behind"][0]["what"] == "7-Zip"
    page = client.get("/server").text
    assert 'id="behind"' in page and 'put("behind"' in page
    updates.behind_once.cache_clear()


def test_the_library_page_is_told_of_books_fonts(running, monkeypatch):
    """The cleaning it does then is the browser's, and tried in a browser; this is what it goes by."""
    from hylly import settings

    client, _, _ = running
    assert client.get("/api/session").json()["book_fonts"] is True
    assert "font-src 'self' data: blob:" in client.get("/catalog/index.html").headers["content-security-policy"]
    with monkeypatch.context() as only_this:  # its own undo, at the end of the block: not the test's settings folder's
        only_this.setitem(settings.DEFAULTS["reader"], "book_fonts", False)
        settings.reload()
        assert client.get("/api/session").json()["book_fonts"] is False
        assert "font-src 'self';" in client.get("/catalog/index.html").headers["content-security-policy"]
    settings.reload()


def test_buttons_need_the_page_key(running, monkeypatch):
    client, srv, _ = running
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    calls = []
    monkeypatch.setattr(dashboard, "restart_later", lambda: calls.append("restart"))
    assert client.post("/server/restart").status_code == 403
    assert client.post("/server/restart", headers={**key, "Origin": "https://elsewhere.example"}).status_code == 403
    assert client.post("/server/restart", headers={"X-Server-Key": "guess"}).status_code == 403
    assert calls == []
    assert client.post("/server/restart", headers={**key, "Origin": f"http://127.0.0.1:{srv.server_port}"}).json() == {"ok": True}
    assert calls == ["restart"]


def test_the_page_says_how_the_start_at_logon_goes_and_writes_none(running, monkeypatch):
    """A web page shouldn't write the system's start-up entries, and the page's switch once wrote one without
    the server's options. It only says whether there is one, and the command that turns it on or off."""
    client, srv, _ = running
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    monkeypatch.setattr(instance, "autostart_on", lambda *a, **k: pytest.fail("the page writes no start at logon"))
    monkeypatch.setattr(instance, "autostart_off", lambda *a, **k: pytest.fail("nor takes one away"))
    for turn in ("on", "off"):
        assert client.post(f"/server/autostart?to={turn}", headers=key).status_code == 404
    page = client.get("/server").text
    assert "logonbox" not in page and "/server/autostart" not in page and "hylly server autostart ${turn}" in page
    s = client.get("/server/status").json()
    assert s["system"] == instance.SYSTEM and "logon" in s


def test_the_page_says_where_a_restart_brings_a_server_started_with_options_of_its_own(running, monkeypatch):
    """Typed for one run in a terminal (--port, --lan): a Restart brings it back with the settings, which may be another
    address, so the page says so, and where."""
    client, srv, _ = running
    assert client.get("/server/status").json()["typed"] == []  # as the settings have it: nothing to say
    monkeypatch.setattr(srv.RequestHandlerClass, "info", {"given": {"port": 8790, "lan": True, "library": "x"}})
    s = client.get("/server/status").json()
    assert s["typed"] == ["--port", "--lan", "--library"] and s["restarted"] == "http://127.0.0.1:8770/"
    page = client.get("/server").text
    assert 'id="typed"' in page and "brings it back in the background with the settings" in page


def test_the_page_shows_the_server_s_messages_however_it_runs(running):
    """The server writes server.log itself, in a terminal too, so the page's Messages show them."""
    client, _, _ = running
    instance.write_log("Library server started (Ctrl-C stops it).")
    s = client.get("/server/status").json()
    assert not s["background"] and s["log"][-1].endswith(" Library server started (Ctrl-C stops it).")
    assert "It runs in a terminal window, so its messages are there." not in client.get("/server").text


PAGE_RUN = """
const T = {reloads: 0, posts: [], answers: {}, said: [], sent: []};
const T_els = {};
const T_el = id => T_els[id] || (T_els[id] = {id, innerHTML: "", textContent: "", hidden: false, disabled: false, className: "",
                                              dataset: {}, style: {}, onclick: null, classList: {add() {}, remove() {}, toggle() {}},
                                              addEventListener() {}, setAttribute(name, value) { this[name] = String(value); }});
const T_on = {};
const document = {hidden: false, getElementById: T_el, addEventListener: (type, f) => (T_on[type] = T_on[type] || []).push(f)};
const window = {devicePixelRatio: 1, addEventListener: (type, f) => (T_on[type] = T_on[type] || []).push(f)};
const location = {pathname: "/server", hash: "", reload: () => { T.reloads += 1; }, assign: () => {}};
const history = {replaceState: (state, title, url) => { location.hash = url; T.hashes = [...(T.hashes || []), url]; }};
const fetch = async (path, init) => {
  const json = body => async () => { if (body === undefined) throw new Error("not JSON"); return body; };
  if (path === "/server/status") return {status: 200, ok: true, json: json(T.status)};
  if (path === "/server/questions") return {status: 200, ok: true, json: json(T.questions || {ok: true, items: [], answered: []})};
  if (path === "/server/removed") return {status: 200, ok: true, json: json(T.removed || {ok: true, files: [], count: 0, bytes: 0})};
  T.posts.push(path);
  if (init && init.body) T.sent.push(JSON.parse(init.body));
  const a = T.answers[path] || {status: 200, body: {ok: true}};
  return {status: a.status, ok: a.status < 300, statusText: a.statusText || "", json: json(a.body)};
};
const setTimeout = () => 0, setInterval = () => 0, clearInterval = () => {}, confirm = () => true;
const navigator = {clipboard: {writeText: async () => {}}};
const T_tick = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
const T_click = selector => T_on.click.forEach(f => f({target: {closest: s => s === selector ? {dataset: {}} : null}}));
const T_banner = () => [T_el("banner").className, T_el("banner").innerHTML.replace(/ ?<button[^>]*>[^<]*<\\/button>/g, "")];
"""


def run_server_page(client, steps: str, before: str = ""):
    """The server page's own script, run by node as a browser runs it, with the status the server gives and buttons the
    server answers as T.answers says; steps, in JavaScript, push what the page shows into T.said, which comes back.
    before: JavaScript run before the page's own, as a status changed before its first ask."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's script runs only in a browser here")
    code = re.findall(r"<script>(.*?)</script>", client.get("/server").text, re.S)[-1]
    status = client.get("/server/status").json()
    script = f"{PAGE_RUN}\nT.status = {json.dumps(status)};\n{before}\n{code}\n(async () => {{ await T_tick();\n{steps}\n" \
             "process.stdout.write(JSON.stringify(T)); })();\n"
    out = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def test_the_page_keeps_what_a_button_said_until_the_next_press_or_a_close(running):
    """Every refresh, each five seconds, cleared the message line, so a refused Stop or Restart, said in it, was gone
    before it could be read. What a button leaves to say now stays, through the refreshes, until the next button
    press or its own Close; then the line says again what the refresh has to say."""
    client, _, _ = running
    found = run_server_page(client, """
      T.status.updated = true;
      await refresh();
      T.said.push(T_banner());
      T.answers["/server/stop"] = {status: 500, body: {ok: false, error: "it couldn't stop"}};
      await $("stop").onclick();
      T.said.push(T_banner());
      await refresh(); await refresh();
      T.said.push(T_banner());
      T.answers["/server/restart"] = {status: 500, body: {ok: false, error: "it couldn't restart"}};
      await $("restart").onclick();
      await refresh();
      T.said.push(T_banner());
      T.said.push(/data-unsay/.test(T_el("banner").innerHTML));
      T_click("[data-unsay]");
      await refresh();
      T.said.push(T_banner());
    """)
    changed = ["show warn", "The program has changed since this server started. <b>Restart</b> to use the new version."]
    stop = ["show bad", "Stop failed: it couldn't stop"]
    assert found["said"] == [changed, stop, stop, ["show bad", "Restart failed: it couldn't restart"], True, changed]


def test_the_page_reloads_itself_when_the_server_has_started_again_since(running):
    """The buttons' key is made anew at each start, and the status asks for none: a page left open across a restart
    done elsewhere showed Running, and its buttons failed with a bare 403. It now reloads itself once the server's
    process is another, at the next refresh or when a button's key is refused; never for the one it began with. The
    one it began with is the one that wrote the page, not the first to answer it: a restart before that counts too."""
    client, srv, _ = running
    same = run_server_page(client, """
      await refresh(); await refresh();
      T.answers["/server/restart"] = {status: 403, statusText: "Forbidden"};
      await $("restart").onclick();
      T.said.push(T_banner());
    """)
    assert same["reloads"] == 0 and same["said"] == [["show bad", "Restart failed: 403 Forbidden"]]
    again = run_server_page(client, "T.status.pid += 1; await refresh();")
    assert again["reloads"] == 1
    stopped = run_server_page(client, """
      await $("stop").onclick();
      T.status.pid += 1; T.status.started = "a while later";
      T_on.visibilitychange.forEach(f => f()); await T_tick();
    """)
    assert stopped["posts"] == ["/server/stop"] and stopped["reloads"] == 1  # started again after a Stop from here
    pressed = run_server_page(client, """
      T.answers["/server/restart"] = {status: 403, statusText: "Forbidden"};
      T.status.pid += 1;
      await $("restart").onclick();
    """)
    assert pressed["reloads"] == 1
    early = run_server_page(client, "", before="T.status.pid += 1;")  # restarted before the page's first status came
    assert early["reloads"] == 1
    started = srv.RequestHandlerClass.info.get("started", "")
    assert f'? "{dashboard.PROC.pid} {started}" : null;' in client.get("/server").text  # what the server wrote it with


def test_stop_from_the_page(running):
    client, srv, thread = running
    assert client.post("/server/stop", headers={"X-Server-Key": srv.RequestHandlerClass.server_key}).json() == {"ok": True}
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_app_names():
    assert dashboard.app_name("Yomu/2.1 CFNetwork/1568 Darwin/25.0") == "Yomu 2.1"
    assert dashboard.app_name("KOReader/2026.07.1 (https://koreader.rocks/) Linux") == "KOReader 2026.07.1"
    assert dashboard.app_name("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/140.0 Safari/537.36 Edg/140.0") == "Edge (browser)"
    assert dashboard.app_name("Mozilla/5.0 (iPad; CPU OS 18_0) AppleWebKit/605.1.15 Version/18.0 Safari/604.1") == "Safari (browser)"
    assert dashboard.app_name("") == "unknown"


def test_a_question_whose_files_can_be_compared_offers_it_and_only_such_one(running):
    """Are these two copies one book, and which file to keep: a Compare button, which opens a dialog; is it a title (a
    file not downloaded, nothing to show): none. Nothing is fetched for it until it's pressed."""
    client, _, _ = running
    q = lambda n, kind: {"id": n, "kind": kind, "title": "Lantern Wolf", "ask": "?", "options": [{"word": "one book"}], "guess": 0, "offered": "x"}  # noqa: E731
    before = f"T.questions = {json.dumps({'ok': True, 'items': [q(7, 'same'), q(8, 'which-file'), q(9, 'title')], 'answered': []})};"
    t = run_server_page(client, 'T.said.push(T_el("questions").innerHTML);', before)
    shown = t["said"][0]
    assert 'data-compare="7"' in shown and 'data-compare="8"' in shown and 'data-compare="9"' not in shown
    assert 'id="cmpbtn7" data-compare="7" aria-haspopup="dialog">Compare the files</button>' in shown and "cmp-sides" not in shown
    assert not [p for p in t["posts"] if "compare" in p or "/api/page" in p]


DIALOGS = """
const T_dialog = id => {  // a dialog as the page uses it: its question's words, and closed at once with T.answer's button
  const d = T_el(id), inner = T_el(id + "-asked");
  let closed = null;
  d.querySelector = () => inner;
  d.addEventListener = (type, f) => { closed = f; };
  d.showModal = () => { T.said.push(`${id}: ${inner.innerHTML}`); d.returnValue = T.answer; closed(); };
};
const T_press = (selector, dataset) => T_on.click.forEach(f => f({target: {closest: s => s === selector ? {dataset, disabled: false} : null}}));
T_dialog("removeboth"); T_dialog("emptyremoved");
T.focused = [];  // where the keyboard's place went, once the lists were drawn again
for (const id of ["decided", "removedsaid"]) T_el(id).focus = () => T.focused.push(`${id}: ${T_el(id).textContent}`);
"""


def test_two_copies_get_keep_both_in_the_list_and_what_removes_for_good_or_both_asks_first(running):
    """Two copies that may be one book: in the list, Keep both (the answer two books, through /server/decide as any
    answer, the guess marked as the other answers' are) and Compare the files in place of their two answers, the copies
    named by their first files; the removals are Compare's (below). A question of another kind keeps its own. Keep only
    this one (keep left) sends what it's about at once; Remove both… only once its dialog's own button is pressed, never
    after Cancel. The Removed part is hidden while _Removed is empty; holding
    files, it lists each with Put back, which sends its place there, and Empty _Removed…, which asks first in a dialog
    of its own, saying how many and how much and how many were put there by hand, and sends what it showed. Both dialogs
    are in the page, each with Cancel. Once the lists are drawn again, the keyboard's place is what the answer said."""
    client, _, _ = running
    page = client.get("/server").text
    for dialog, button in (("removeboth", "Remove both"), ("emptyremoved", "Delete them")):
        assert re.search(rf'<dialog id="{dialog}"[^>]*>\s*<form method="dialog">.*?<button value="cancel"[^>]*>Cancel</button>'
                         rf'<button value="ok" class="danger">{button}</button>', page, re.S)
    two = [{"word": "The same book: one card", "answer": "same"}, {"word": "Two books: keep them apart", "answer": "apart"}]
    same = {"id": 7, "kind": "same", "title": "Moonwake Saga", "ask": "?", "options": two, "guess": 1, "offered": "abc",
            "subjects": [{"id": "f:a", "f": "comics/Sagas/Moonwake%20Saga.epub"}, {"id": "f:b", "f": "shelf/Moonwake%20Saga.epub"}]}
    other = {"id": 8, "kind": "which-file", "title": "Lantern Wolf", "ask": "?", "guess": 0, "offered": "x",
             "options": [{"file": "lw2.cbz", "format": "cbz", "size": 100}, {"file": "lw2.pdf", "format": "pdf", "size": 300}]}
    removed = {"ok": True, "folder": "_Removed", "count": 2, "bytes": 3000, "more": False, "by_hand": 1, "digest": "d" * 64, "files": [
        {"held": "shelf/Moonwake Saga.epub", "path": "shelf/Moonwake Saga.epub", "size": 2000, "at": "2026-11-20T10:00:00+02:00",
         "t": "Moonwake Saga"},
        {"held": "ebooks/Hedge Lore (2).pdf", "path": "ebooks/Hedge Lore.pdf", "size": 1000, "at": "", "t": ""}]}
    before = f"T.questions = {json.dumps({'ok': True, 'items': [same, other], 'answered': []})};" + DIALOGS
    t = run_server_page(client, """
      T.said.push(T_el("questions").innerHTML, T_el("tab-removed").hidden);
      for (const [path, did] of [["/server/remove", "Moved."], ["/server/put-back", "Put back."], ["/server/empty-removed", "Deleted."]])
        T.answers[path] = {status: 200, body: {ok: true, did}};
      T.answer = "cancel"; T_press("[data-remove]", {q: "7", remove: "none", offered: "abc"}); await T_tick();
      T.answer = "ok"; T_press("[data-remove]", {q: "7", remove: "none", offered: "abc"}); await T_tick();
      T.said.push(T_el("questions").innerHTML);  // settled: not shown again, though the server lists it till its build
      T.answers["/server/remove"] = {status: 404, body: {ok: false, error: "No question 7 about two copies is open."}};
      T.answer = "ok"; T_press("[data-remove]", {q: "7", remove: "left", offered: "abc"}); await T_tick();
      T.said.push(T_el("decided").textContent);
      T.removed = %s;
      await removedList();
      T.said.push(T_el("removedlist").innerHTML, T_el("tab-removed").hidden);
      T_press("[data-putback]", {putback: "ebooks/Hedge Lore (2).pdf"}); await T_tick();
      T.answer = "cancel"; T_press("[data-empty]", {}); await T_tick();
      T.answer = "ok"; T_press("[data-empty]", {}); await T_tick();
    """ % json.dumps(removed), before)
    shown, hidden, asked_once, asked_twice, after, settled_said, listed, hidden_now, asked_empty, asked_empty_again = t["said"]
    assert 'data-q="7"' not in after and 'data-q="8"' in after and settled_said == "Question 7 is settled already."
    seven, eight = shown.split('<div class="question">')[1:]
    assert '<div class="options"><button data-q="7" data-choice="2" data-offered="abc" class="guess">Keep both · the guess</button>' \
           '<button type="button" id="cmpbtn7" data-compare="7" aria-haspopup="dialog">Compare the files</button></div>' in seven
    assert "The copies: <code>comics/Sagas/Moonwake Saga.epub</code> and <code>shelf/Moonwake Saga.epub</code>" in seven
    assert "The same book: one card" not in seven and "data-remove" not in shown and "lw2.pdf (PDF" in eight
    assert hidden is True and hidden_now is False
    assert asked_once == asked_twice and asked_once.startswith("removeboth: <p>Every file of both cards goes: the left one's, "
                                                               "<code>comics/Sagas/Moonwake Saga.epub</code>")
    assert 'data-putback="ebooks/Hedge Lore (2).pdf">Put back</button>' in listed and "<code>ebooks/Hedge Lore.pdf</code>" in listed
    assert "put there by hand" in listed and "Empty _Removed…</button>" in listed and "2 files, 3.0 KB." in listed
    assert asked_empty == asked_empty_again == ("emptyremoved: <p>2 files, 3.0 KB, are deleted for good, and that space is free "
                                                "again. Of them, 1 file was put there by hand, not removed on this page: it goes too.</p>")
    assert t["posts"] == ["/server/remove", "/server/remove", "/server/put-back", "/server/empty-removed"]
    assert t["sent"] == [{"id": 7, "keep": "none", "offered": "abc"}, {"id": 7, "keep": "left", "offered": "abc"},
                         {"held": "ebooks/Hedge Lore (2).pdf"}, {"digest": "d" * 64}]  # what the list showed, to delete just that
    assert t["focused"] == ["decided: Question 7: Moved.", "decided: Question 7 is settled already.", "removedsaid: Put back.",
                            "removedsaid: Deleted."]


COMPARE = """
const T_cmp = T_el("compare");  // the comparison's dialog: a browser tells of its close only at its next frame, so not here
T_cmp.showModal = () => { T_cmp.open = true; };
T_cmp.close = () => { T_cmp.open = false; };
for (const id of ["cmpbtn7", "cmpbtn8"]) T_el(id).focus = () => T.focused.push(id);
const T_key = (key, more = {}) => T_on.keydown.forEach(f => f({key, target: {type: "submit"}, ...more,
                                                                preventDefault() { T.prevented = (T.prevented || 0) + 1; }}));
const T_open = async n => { T_press("[data-compare]", {compare: String(n)}); for (let i = 0; i < 4; i++) await T_tick(); };
T.answers["/api/session"] = {status: 200, body: {widths: [240, 600, 900, 1200]}};
"""


def test_compare_opens_one_dialog_with_each_copy_s_own_keep_under_its_page_and_both_at_the_bottom(running):
    """Compare the files opens a dialog the size of the window, one at a time: the question at its top with a Close (×);
    a column for each copy, what it is (escaped), its page, and right under it Keep only this one (keep left, keep
    right: the copies as the columns show them, left to right); at the bottom ◀, the slider, ▶ and Keep both, marked as
    the guess, and Remove both…. ← and → turn both, the keyboard's place left where it is; not with a modifier, nor on
    the slider, which steps by itself. Close and Esc give the keyboard's place back to its Compare the files, and it
    opens again as it was left; an answer from it closes it, the place going to what the answer said. With one copy not
    shown, no removal is offered, only Keep both."""
    client, _, _ = running
    page = client.get("/server").text
    assert re.search(r'<dialog id="compare" class="cmp" aria-labelledby="cmpasked">\s*<div class="cmp-top"><div id="cmpasked"'
                     r'[^>]*></div><button type="button" class="cmp-close" data-cmpclose aria-label="Close the comparison">×</button>', page)
    two = [{"word": "The same book: one card", "answer": "same"}, {"word": "Two books: keep them apart", "answer": "apart"}]
    same = {"id": 7, "kind": "same", "title": "Moonwake Saga", "ask": "Are these one book?", "options": two, "guess": 1,
            "offered": "abc", "why": "the same name", "subjects": [{"id": "s:1", "f": "a.cbz"}, {"id": "s:2", "f": "b.cbz"}]}
    side = lambda n, name, size: {"id": f"s:{n}", "name": name, "format": "cbz", "size": size, "date": "2026-11-01",  # noqa: E731
                                  "reads": "page", "cover": "", "language": "", "files": 1, "t": "Moonwake Saga"}
    sides = [side(1, "Moonwake <i>Saga</i>.cbz", 3000), side(2, "Moonwake Saga (scan).cbz", 1000)]

    def before(shown, unshown):
        return f"T.questions = {json.dumps({'ok': True, 'items': [same], 'answered': []})};" + DIALOGS + COMPARE + f"""
          T.answers["/server/compare?q=7"] = {{status: 200, body: {json.dumps({'ok': True, 'sides': shown, 'listed': [], 'unshown': unshown})}}};
          T.answers["/api/pages?id=s%3A1"] = T.answers["/api/pages?id=s%3A2"] = {{status: 200, body: {{ok: true, pages: 24, v: "9"}}}};
          T.answers["/server/remove"] = {{status: 200, body: {{ok: true, did: "Moved."}}}};
        """
    t = run_server_page(client, """
      const seen = () => ({asked: T_el("cmpasked").innerHTML, sides: T_el("cmpsides").innerHTML, bottom: T_el("cmpbottom").innerHTML,
                           open: !!T_cmp.open, at: [T_el("cmpat0").textContent, T_el("cmpat1").textContent], src: T_el("cmppage1").src});
      await T_open(7);
      T.said.push(seen());
      T_key("ArrowRight"); T_key("ArrowRight"); T.said.push(seen());
      T_key("ArrowLeft"); T_key("ArrowRight", {altKey: true}); T_key("ArrowRight", {target: {type: "range"}}); T.said.push(seen());
      T_press("[data-cmpclose]", {}); T.said.push(seen());
      T_key("ArrowRight"); T.said.push(seen());  // closed: no turning
      await T_open(7); T.said.push(seen());  // as it was left, nothing fetched again
      T_cmp.oncancel({preventDefault() {}}); T.said.push(seen());  // Esc
      await T_open(7);
      T_press("[data-remove]", {q: "7", remove: "right", offered: "abc"}); await T_tick(); await T_tick();
      T.said.push(seen());
    """, before(sides, 0))
    opened, turned, back, closed, still, again, escaped, answered = t["said"]
    assert opened["open"] and "<b>7.</b> <b>Moonwake Saga</b> · Are these one book?" in opened["asked"]
    assert "(the same name) Click a page to magnify that spot on each; ← and → turn the pages." in opened["asked"]
    assert "Moonwake &lt;i&gt;Saga&lt;/i&gt;.cbz" in opened["sides"] and "<i>Saga" not in opened["sides"]
    left, right = opened["sides"].split('<div class="cmp-facts">')[1:]
    assert left.index('id="cmppage0"') < left.index('<button class="keep" data-q="7" data-remove="left" data-offered="abc" '
                                                    'aria-describedby="cmpname0">Keep only this one</button>')
    assert right.index('id="cmppage1"') < right.index('data-remove="right" data-offered="abc" aria-describedby="cmpname1">Keep only this one')
    assert "3.0 times the other's size" in left and "Keep this file" not in opened["sides"] and "Keep both" not in opened["sides"]
    bottom = opened["bottom"]
    assert bottom.index('aria-label="An earlier page">◀') < bottom.index('id="cmpslide" min="0" max="23" value="2"') \
        < bottom.index('aria-label="A later page">▶') < bottom.index(
            '<button data-q="7" data-choice="2" data-offered="abc" class="keep guess">Keep both · the guess</button>'
            '<button data-q="7" data-remove="none" data-offered="abc">Remove both…</button>')
    assert opened["at"] == ["Page 3 of 24", "Page 3 of 24"] and opened["src"] == "/api/page?id=s%3A2&n=2&w=600&v=9"
    assert turned["at"] == ["Page 5 of 24"] * 2 and turned["src"] == "/api/page?id=s%3A2&n=4&w=600&v=9"
    assert back["at"] == ["Page 4 of 24"] * 2 and t["prevented"] == 3  # not with Alt, nor on the slider
    assert not closed["open"] and still["at"] == ["Page 4 of 24"] * 2
    assert again["open"] and again["at"] == ["Page 4 of 24"] * 2 and not escaped["open"] and not answered["open"]
    assert t["posts"].count("/server/compare?q=7") == 1 and t["sent"] == [{"id": 7, "keep": "right", "offered": "abc"}]
    assert t["focused"] == ["cmpbtn7", "cmpbtn7", "decided: Question 7: Moved."]
    one = run_server_page(client, "await T_open(7); T.said.push(T_el('cmpsides').innerHTML, T_el('cmpbottom').innerHTML, "
                                  "T_el('cmpasked').innerHTML);", before(sides[:1], 1))
    sides_one, bottom_one, asked_one = one["said"]
    assert "data-remove" not in sides_one + bottom_one and "Keep both · the guess</button>" in bottom_one
    assert "A copy isn't a card of its own in the catalog just now" in asked_one


def test_compare_of_a_title_s_files_puts_keep_this_file_under_each_in_the_options_order_the_guess_marked(running):
    """A title offered in several files: a column for each, in its options' order, the files the library has with their
    pages and those it hasn't with what the shop lists; under each Keep this file, that option's answer through
    /server/decide (as its button in the list), the rules' guess marked. Nothing to remove, no Keep both; an answer
    closes it."""
    client, _, _ = running
    options = [{"file": "lw.pdf", "format": "pdf", "size": 300}, {"file": "lw.cbz", "format": "cbz", "size": 100},
               {"file": "lw.epub", "format": "epub", "size": 50, "label": "EPUB (fixed)"}]
    q = {"id": 8, "kind": "which-file", "title": "Lantern Wolf", "ask": "Which file to keep?", "options": options, "guess": 1,
         "offered": "x<y"}
    cbz = {"id": "s:3", "name": "lw.cbz", "format": "cbz", "size": 100, "date": "", "reads": "page", "cover": "", "language": "", "files": 3}
    listed = [{"file": "lw.pdf", "format": "pdf", "size": 300, "label": None},
              {"file": "lw.epub", "format": "epub", "size": 50, "label": "EPUB (fixed)"}]
    before = f"T.questions = {json.dumps({'ok': True, 'items': [q], 'answered': []})};" + DIALOGS + COMPARE + f"""
      T.answers["/server/compare?q=8"] = {{status: 200, body: {json.dumps({'ok': True, 'sides': [cbz], 'listed': listed, 'unshown': 0})}}};
      T.answers["/api/pages?id=s%3A3"] = {{status: 200, body: {{ok: true, pages: 1, v: "1"}}}};
      T.answers["/server/decide"] = {{status: 200, body: {{ok: true, did: "keeps lw.pdf"}}}};
    """
    t = run_server_page(client, """
      await T_open(8);
      T.said.push(T_el("cmpsides").innerHTML, T_el("cmpbottom").innerHTML, T_el("questions").innerHTML, T_el("cmpasked").innerHTML);
      T_press("[data-choice]", {q: "8", choice: "1", offered: "x<y"}); await T_tick(); await T_tick();
      T.said.push(!!T_cmp.open);
    """, before)
    sides, bottom, row, asked, still_open = t["said"]
    pdf, comic, epub = sides.split('<div class="cmp-facts">')[1:]
    assert "Not downloaded: only what the shop lists of it." in pdf
    assert 'data-choice="1" data-offered="x&lt;y" aria-describedby="cmpname0">Keep this file</button>' in pdf
    assert 'id="cmppage1"' in comic and 'data-choice="2" data-offered="x&lt;y" aria-describedby="cmpname1">Keep this file · the guess</button>' in comic
    assert "EPUB · 50 bytes · EPUB (fixed)" in epub and 'data-choice="3"' in epub and "Its card has" not in comic
    assert "data-remove" not in sides + bottom and "Keep both" not in bottom and "cmpslide" not in bottom  # one page: nothing to turn
    assert asked.endswith('<div class="why">Click a page to magnify that spot on each.</div>')
    assert '<div class="options"><button type="button" id="cmpbtn8" data-compare="8"' in row
    assert t["sent"] == [{"id": 8, "choice": "1", "offered": "x<y"}] and not still_open
    assert t["focused"] == ["decided: Answered question 8: this keeps lw.pdf."]


PARTS = ["decide", "removed", "addresses", "running", "devices", "alarms", "access", "messages"]
TABS = """
const T_shown = () => ["decide", "removed", "addresses", "running", "devices", "alarms", "access", "messages"].filter(id => !T_el(id).hidden)
  .map(id => `${id}${T_el("tab-" + id)["aria-selected"] === "true" && T_el("tab-" + id).tabIndex === 0 ? "" : " (its tab not selected)"}`).join(" ");
const T_tabkey = (key, on) => T_on.keydown.forEach(f => f({key, target: {closest: s => s === '[role="tab"]' ? T_el(on) : null},
                                                            preventDefault() {}}));
for (const id of ["decide", "removed", "addresses", "running", "devices", "alarms", "access", "messages"])
  T_el("tab-" + id).focus = () => T.focused.push("tab-" + id);
T.focused = [];
"""


def test_the_page_s_parts_are_tabs_one_shown_the_one_the_address_names(running):
    """The page's parts are tabs in a bar of their own (a tablist, each tab controlling its part, a tabpanel named by
    it), one part shown. The address names the part (#decide, as the library page's links have it): that one, once it
    has a tab; with none, To decide while it has questions, its tab with how many, else How it runs. To decide and
    Removed have a tab only while they hold anything. A tab picked goes into the address, without a step in the
    history; the lists drawn again leave the part shown as it is. ← and → go along the tabs there are, round, Home
    and End to the ends, each shown as the keyboard comes to it."""
    client, _, _ = running
    page = client.get("/server").text
    bar = page[page.index('<div class="tabs" id="tabs" role="tablist"'):]
    bar = bar[:bar.index("</div>")]
    assert [m for m in re.findall(r'id="tab-(\w+)" aria-controls="(\w+)"', bar)] == [(p, p) for p in PARTS]
    for part in PARTS:
        assert re.search(rf'<section id="{part}" role="tabpanel" aria-labelledby="tab-{part}" tabindex="0"', page), part
    assert 'id="tab-decide" aria-controls="decide" aria-selected="false" tabindex="-1" hidden>To decide<span class="count"' in bar
    assert 'id="tab-removed" aria-controls="removed" aria-selected="false" tabindex="-1" hidden>Removed</button>' in bar
    q = {"id": 7, "kind": "title", "title": "Lantern Wolf", "ask": "?", "options": [{"word": "a title"}], "guess": 0, "offered": "x"}
    asked = {"ok": True, "items": [q, {**q, "id": 8}], "answered": []}
    shown = run_server_page(client, 'T.said.push(T_shown(), T_el("decidecount").textContent, T_el("decidecount").hidden);',
                            f"T.questions = {json.dumps(asked)};" + TABS)
    assert shown["said"] == ["decide", 2, False] and "hashes" not in shown  # chosen for it: the address stays as it was
    none = run_server_page(client, 'T.said.push(T_shown(), T_el("tab-decide").hidden, T_el("tab-removed").hidden);', TABS)
    assert none["said"] == ["running", True, True]
    named = run_server_page(client, 'T.said.push(T_shown());', f'location.hash = "#devices"; T.questions = {json.dumps(asked)};' + TABS)
    assert named["said"] == ["devices"]
    t = run_server_page(client, f"""
      T.said.push(T_shown());  // #decide while there's nothing to decide: How it runs
      T.questions = {json.dumps(asked)}; await questions(); T.said.push(T_shown());  // there's something now: that one
      T_on.click.forEach(f => f({{target: {{closest: s => s === '[role="tab"]' ? T_el("tab-alarms") : null}}}}));
      await questions(); await removedList(); T.said.push(T_shown());  // drawn again: still there
      T.removed = {{ok: true, folder: "_Removed", count: 1, bytes: 9, more: false, by_hand: 0, digest: "d", files: []}};
      await removedList(); T.said.push(T_el("tab-removed").hidden);
      T_tabkey("ArrowRight", "tab-alarms"); T_tabkey("ArrowRight", "tab-access"); T_tabkey("ArrowRight", "tab-messages");
      T_tabkey("ArrowRight", "tab-decide"); T_tabkey("ArrowLeft", "tab-removed"); T_tabkey("End", "tab-decide"); T_tabkey("Home", "tab-messages");
      T_tabkey("Enter", "tab-decide"); T.said.push(T_shown());
    """, 'location.hash = "#decide";' + TABS)
    assert t["said"] == ["running", "decide", "alarms", False, "decide"]
    assert t["hashes"] == ["#alarms", "#access", "#messages", "#decide", "#removed", "#decide", "#messages", "#decide"]
    assert t["focused"] == ["tab-access", "tab-messages", "tab-decide", "tab-removed", "tab-decide", "tab-messages", "tab-decide"]


def test_the_page_shows_its_address_on_this_computer_only_on_it_and_names_the_devices_by_who_looks(running):
    """On this computer, and its address there, only when the status says the page is looked at on the server's own
    computer (here); the devices list names the device looking and the server's own computer apart, and the same one,
    the server's computer looked at on itself, as this computer."""
    client, _, _ = running
    clients = [{"ip": "127.0.0.1", "app": "Chrome (browser)", "n": 3, "last": 0, "you": True, "server": True},
               {"ip": "192.0.2.44", "app": "Safari (browser)", "n": 2, "last": 0, "you": False, "server": False}]
    t = run_server_page(client, """
      T.said.push(T_el("pc_row").hidden);
      T.status.here = false; T.status.clients[0].you = false; T.status.clients[1].you = true; await refresh();
      T.said.push(T_el("pc_row").hidden, T_el("devicelist").innerHTML);
    """, f"T.status.here = true; T.status.clients = {json.dumps(clients)};")
    on_it, elsewhere, devices = t["said"]
    assert on_it is False and elsewhere is True
    assert "<td>The server's computer, 127.0.0.1</td>" in devices and "<td>This device, 192.0.2.44</td>" in devices
    first = run_server_page(client, 'T.said.push(T_el("devicelist").innerHTML);', f"T.status.here = true; T.status.clients = {json.dumps(clients)};")
    assert "<td>This computer, 127.0.0.1</td>" in first["said"][0] and "<td>192.0.2.44</td>" in first["said"][0]
    page = client.get("/server").text
    assert '<tr id="pc_row" hidden><td>On this computer</td><td id="pc"></td></tr>' in page and "this computer only" not in page
