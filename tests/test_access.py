"""Logins and the access log: password hashes, a login for every request wherever it comes from, the one-time sign-in
link, wrong passwords, the log lines, the settings no longer used, and who can reach the server."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hylly import access, serve

from helpers import start


def test_passwords_are_stored_only_as_hashes():
    assert access.add_user("reader", "correct horse") is True
    assert access.add_user("reader", "battery staple") is False  # a new password, the same user
    from hylly import db

    on_disk = b"".join(p.read_bytes() for p in (db.file(), db.file().with_name("hylly.db-wal")) if p.exists())
    assert b"battery" not in on_disk and access.load_accounts()["reader"][1].startswith("scrypt$")
    assert access.check_password("battery staple", access.load_accounts()["reader"][1])
    assert not access.check_password("correct horse", access.load_accounts()["reader"][1])
    assert access.remove_user("reader") and not access.remove_user("reader") and access.load_accounts() == {}
    with pytest.raises(SystemExit):
        access.add_user("two words", "whatever1")


def test_a_hash_carries_its_own_cost_so_the_tests_cheap_one_and_hylly_s_strong_one_are_both_checked_right(monkeypatch):
    """The tests hash at a cost of 16 (conftest.SCRYPT_N), which only saves time: a hash says what it was made at, and
    is checked at that, whatever the cost is now."""
    import conftest

    cheap = access.hash_password("correct horse")
    assert cheap.split("$")[1:4] == [str(conftest.SCRYPT_N), "8", "1"] and access.check_password("correct horse", cheap)
    with monkeypatch.context() as strong:
        strong.setattr(access, "SCRYPT_N", 2 ** 17)  # 128 MB a hash, past OpenSSL's own limit: SCRYPT_MAXMEM gives it
        made_strong = access.hash_password("battery staple")
        assert access.check_password("correct horse", cheap) and not access.check_password("battery staple", cheap)
    assert made_strong.split("$")[1] == "131072" and access.check_password("battery staple", made_strong)
    assert not access.check_password("correct horse", made_strong)
    too_much = made_strong.replace("$131072$", f"${2 ** 20}$", 1)  # a GB a check, as a database written by hand may ask
    assert not access.check_password("battery staple", too_much)  # no password, rather than the memory taken


def test_hylly_itself_hashes_a_password_at_full_strength():
    """Outside the tests, in a Python of its own as a command or the server is: a cost of 2**17 or more (with r = 8 and
    p = 1), today's guidance for scrypt, about a quarter of a second and 128 MB a check, as a guessed password must
    cost something."""
    import subprocess

    code = "from hylly import access; print(access.SCRYPT_N, access.hash_password('correct horse').split('$')[1])"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(Path(access.__file__).parents[1])})
    assert done.returncode == 0, done.stderr
    module, stored = map(int, done.stdout.split())
    assert module == stored >= 2 ** 17


def test_a_hash_below_today_s_cost_is_made_again_at_the_next_login_and_the_logins_go_on(monkeypatch):
    """A hash from before the cost was raised still checks, and at its user's next login, on the login page or from a
    reading app, it is made again at today's cost: on the page in the same change as the login's own. The browsers
    already logged in with that password stay so, and a reading app's login is remembered as before. A wrong password,
    a hash at today's cost or above it, and a password set meanwhile are left as they are. A wrong password for a hash
    below today's cost is checked against the decoy too, as an unknown name is, so that neither answers sooner than a
    user at today's cost: the old check alone took an eighth of the time, and gave away who is a user."""
    import base64

    import conftest

    access.add_user("ann", "correct horse")
    access.add_user("rory", "battery staple")
    old = {name: hashed for name, (_, hashed) in access.load_accounts().items()}  # at the tests' cost
    ip, today = "203.0.113.9", conftest.SCRYPT_N * 2
    gate = access.Gate(log=lambda m: None)
    before = gate.log_in(ip, "ann", "correct horse")[1]  # a browser logged in while that was today's cost
    monkeypatch.setattr(access, "SCRYPT_N", today)  # the cost raised since
    assert access.below_today(old["ann"]) and not access.below_today("not a hash")
    checks = []
    real = access.check_password
    monkeypatch.setattr(access, "check_password", lambda p, s: checks.append(s) or real(p, s))
    basic = lambda login: {"Authorization": "Basic " + base64.b64encode(login).decode()}  # noqa: E731
    assert gate.log_in(ip, "ann", "wrong horse")[0] == "wrong" and access.load_accounts()["ann"][1] == old["ann"]
    assert gate.check(ip, basic(b"rory:wrong horse")) == ("login", "rory")
    assert gate.check(ip, basic(b"nobody:wrong horse"))[0] == "login"
    decoy = access._decoy()
    assert checks == [old["ann"], decoy, old["rory"], decoy, decoy]  # each wrong one as long as the unknown name's
    checks.clear()
    after = gate.log_in(ip, "ann", "correct horse")[1]
    assert checks == [old["ann"]]  # a right one: no decoy, as making the new hash takes that time
    stored = access.load_accounts()["ann"][1]
    assert stored.split("$")[1:4] == [str(today), "8", "1"] and access.check_password("correct horse", stored)
    assert all(gate.check(ip, {"Cookie": f"hylly={token}"}) == ("ok", "ann") for token in (before, after))
    checks.clear()
    app = basic(b"rory:battery staple")
    assert gate.check(ip, app) == ("ok", "rory") == gate.check(ip, app)  # the second remembered: one check
    assert checks == [old["rory"]] and access.load_accounts()["rory"][1].split("$")[1] == str(today)
    checks.clear()
    assert gate.log_in(ip, "ann", "wrong horse")[0] == "wrong" and checks == [stored]  # at today's cost: no decoy
    made = access.load_accounts()
    assert gate.log_in(ip, "ann", "correct horse")[0] == "ok" and access.load_accounts() == made  # today's cost: kept
    monkeypatch.setattr(access, "SCRYPT_N", conftest.SCRYPT_N)
    assert gate.log_in(ip, "ann", "correct horse")[0] == "ok" and access.load_accounts() == made  # above it: kept too
    with access.store().write() as c:  # the old hash checked, and a new password set while it was
        assert access.made_again(c, "ann", old["ann"], access.hash_password("correct horse")) == old["ann"]
    assert access.load_accounts() == made


def test_a_reading_app_s_login_holds_when_its_stronger_hash_can_t_be_written_and_the_log_says_so(monkeypatch):
    """The database busy, or its disk full, as a reading app logs in with a hash below today's cost: the login is let in
    all the same and remembered, the server's log says why the hash stays, and the next login tries again."""
    import base64
    import sqlite3

    import conftest

    access.add_user("rory", "battery staple")
    old = access.load_accounts()["rory"][1]
    monkeypatch.setattr(access, "SCRYPT_N", conftest.SCRYPT_N * 2)
    said, checks = [], []
    real = access.check_password
    monkeypatch.setattr(access, "check_password", lambda p, s: checks.append(s) or real(p, s))
    gate = access.Gate(log=said.append)

    def busy(c, name, stored, again):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as locked:
        locked.setattr(access, "made_again", busy)
        app = {"Authorization": "Basic " + base64.b64encode(b"rory:battery staple").decode()}
        assert gate.check("203.0.113.9", app) == ("ok", "rory") == gate.check("203.0.113.9", app)
    assert checks == [old] and access.load_accounts()["rory"][1] == old  # remembered, and the old hash stays
    assert said == ["rory's password couldn't be hashed again at today's cost (database is locked); at the next login, then."]
    assert access.Gate(log=said.append).check("203.0.113.9", app) == ("ok", "rory")  # a new server: tried again, and taken
    assert access.load_accounts()["rory"][1].split("$")[1] == str(conftest.SCRYPT_N * 2)


def test_a_look_at_the_users_only_reads_the_database(monkeypatch, tmp_path_factory):
    """hylly doctor's look (peek_accounts): the users as the database has them, opened read-only. Nobody where there's no
    database yet, or one from before it kept the users, and looking neither makes one nor brings it up to now."""
    import sqlite3

    from hylly import db

    assert access.peek_accounts() == {} and not db.file().exists()  # no database: nobody, and none made
    access.add_user("reader", "correct horse", role="librarian")
    assert access.peek_accounts() == access.load_accounts() and access.peek_accounts()["reader"][0] == "librarian"
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path_factory.mktemp("another-settings-folder")))
    db.close_all()
    db.file().parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(db.file(), isolation_level=None)  # one at the first of the test versions' steps: no users' table yet
    old.execute("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT NOT NULL) STRICT")
    old.execute("PRAGMA user_version = 1")
    old.close()
    assert access.peek_accounts() == {}
    old = sqlite3.connect(db.file())
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1  # only looked at
    old.close()


def test_everyone_must_log_in_wherever_they_come_from():
    """Nothing is let in by where it comes from: home, away, this computer, a proxy, a VPN's range."""
    gate = access.Gate(log=lambda m: None)
    for ip in ("192.168.1.45", "203.0.113.9", "127.0.0.1", "::1", "100.101.102.103"):
        for headers in ({}, {"Host": "localhost"}, {"X-Forwarded-For": "203.0.113.9"}):
            assert gate.check(ip, headers) == ("login", None), (ip, headers)
    assert gate.role(None) == "borrower"  # no user: the least of the roles, never a reader's or more


APP = {"User-Agent": "Yomu/100 CFNetwork/1500", "Accept": "*/*"}  # a reading app
NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "User-Agent": "Mozilla/5.0 (iPhone)", "Accept": "text/html"}
OPENED = ["/", "/catalog/index.html", "/catalog/help.html", "/catalog/data.js", "/opds", "/ereader/opds", "/me/opds",
          "/opds/search?q=novel", "/api/session", "/api/me/places", "/ping", "/server", "/server/status", "/database",
          "/foliate/view.js", "/fonts/", "/catalog/covers/x.jpg", "/humble/b/novel.epub"]


def test_an_anonymous_request_is_refused_everywhere_from_this_computer_too(library):
    """Every request needs a login, from anywhere: from this computer by localhost or 127.0.0.1, from another device at
    home, through a proxy or a tunnel (on the one port, with forwarding headers or none). A browser opening a page goes
    to the login page and back after; a page's own request gets 401 with no password dialog; a reading app gets 401
    asking for its Basic login. Nothing is answered, changed or written without one."""
    access.add_user("owen", "correct horse", role="owner")
    srv, client = start(library, access_log=True)
    port = srv.server_port
    try:
        wheres = {"localhost": {"Host": f"localhost:{port}"}, "127.0.0.1": {}, "home": {"Host": "192.168.1.5:8770"},
                  "proxy": {"X-Forwarded-For": "203.0.113.9"},
                  "tunnel": {"Host": "testpc.tail1234.ts.net", "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}}
        for where, headers in wheres.items():
            for path in OPENED:
                app = client.get(path, headers={**headers, **APP})
                if path.startswith(("/foliate/", "/fonts/")):  # the reader's own: only a browser asks, and gets no dialog
                    assert app.status_code == 401 and "www-authenticate" not in app.headers, (where, path)
                else:
                    assert app.status_code == 401 and app.headers["www-authenticate"].startswith("Basic "), (where, path)
                own = client.get(path, headers={**headers, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})
                assert own.status_code == 401 and "www-authenticate" not in own.headers, (where, path)
            opened = client.get("/catalog/index.html?x=1", headers={**headers, **NAVIGATE})
            assert opened.status_code == 302 and opened.headers["location"] == "/login?next=%2Fcatalog%2Findex.html%3Fx%3D1"
            for path, body in (("/api/tags", {"op": "add", "tag": "Read", "ids": ["h:novel"]}), ("/api/me/place", {"id": "h:novel"}),
                               ("/server/stop", {}), ("/database/query", {"sql": "SELECT 1"})):
                assert client.post(path, json=body, headers={**headers, **APP}).status_code == 401, (where, path)
        from hylly import curation, people

        assert people.everyone() == [] and curation.history(library, 10) == []
    finally:
        srv.shutdown()
    assert {line.split(" ")[2] for line in access.access_log_file().read_text(encoding="utf-8").splitlines()} == {"-"}


def test_a_health_check_is_answered_without_a_login_and_says_nothing_else(library, log_in):
    """/healthz, for a container's or a service's health check: ok, whoever asks, and nothing about the library."""
    srv, client = start(library)
    try:
        for headers in ({}, APP, NAVIGATE, {"Host": "192.168.1.5:8770", "X-Forwarded-For": "203.0.113.9"}):
            ok = client.get("/healthz", headers=headers)
            assert ok.status_code == 200 and ok.content == b"ok" and ok.headers["content-type"] == "text/plain"
            assert ok.headers["cache-control"] == "no-store" and "set-cookie" not in ok.headers
        assert client.head("/healthz").status_code == 200 and client.head("/healthz").content == b""
        assert client.get("/healthz/x", headers=APP).status_code == 401 and client.get("/healthz?x", headers=APP).status_code == 200
        assert log_in(client).get("/healthz").content == b"ok"  # the same for a login
    finally:
        srv.shutdown()


def test_login(library, monkeypatch):
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    try:
        first = client.get("/opds")
        assert first.status_code == 401 and first.headers["www-authenticate"].startswith("Basic ")
        assert client.get("/opds", auth=("reader", "wrong")).status_code == 401
        assert client.get("/opds", auth=("nobody", "correct horse")).status_code == 401
        checks = []
        real = access.check_password
        monkeypatch.setattr(access, "check_password", lambda p, s: checks.append(1) or real(p, s))
        for _ in range(3):
            assert client.get("/opds", auth=("reader", "correct horse")).status_code == 200
        assert len(checks) == 1  # remembered after the first check
        access.add_user("reader", "a new password")  # a changed password ends the remembered login
        assert client.get("/opds", auth=("reader", "correct horse")).status_code == 401
    finally:
        srv.shutdown()


BROWSER = {"Sec-Fetch-Mode": "navigate", "User-Agent": "Mozilla/5.0 (iPhone)"}
TUNNEL = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "library.example"}


def log_in(client, name, password, next_="/catalog/index.html", headers=None):
    return client.post("/login", data={"username": name, "password": password, "next": next_}, headers=headers or {})


def test_browsers_get_a_login_page(library):
    """Password managers fill in a page's form, not the browser's own password dialog."""
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    try:
        first = client.get("/catalog/index.html?x=1", headers=BROWSER)
        assert first.status_code == 302 and first.headers["location"] == "/login?next=%2Fcatalog%2Findex.html%3Fx%3D1"
        page = client.get(first.headers["location"], headers=BROWSER)
        assert page.status_code == 200 and 'autocomplete="username"' in page.text and 'autocomplete="current-password"' in page.text
        assert 'name="next" value="/catalog/index.html?x=1"' in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"] and page.headers["cache-control"] == "no-store"
        old = client.get("/catalog/index.html", headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})  # no Sec-Fetch
        assert old.status_code == 302 and old.headers["location"].startswith("/login?")
        own = client.get("/api/session", headers={"Sec-Fetch-Mode": "cors"})  # a page's own request: no password dialog
        assert own.status_code == 401 and "www-authenticate" not in own.headers
        app = client.get("/opds", headers={"User-Agent": "Yomu/100 CFNetwork/1500", "Accept": "*/*"})  # a reading app
        assert app.status_code == 401 and app.headers["www-authenticate"].startswith("Basic ")
        assert client.get("/opds", auth=("reader", "correct horse")).status_code == 200
        # a browser's remembered Basic password no longer gets past the login page, so Log out is really out
        remembered = client.get("/catalog/index.html", headers=BROWSER, auth=("reader", "correct horse"))
        assert remembered.status_code == 302 and remembered.headers["location"].startswith("/login?")
        own = client.get("/api/session", headers={"Sec-Fetch-Mode": "cors"}, auth=("reader", "correct horse"))
        assert own.status_code == 401 and "www-authenticate" not in own.headers
    finally:
        srv.shutdown()


def test_a_browser_at_a_plain_http_address_is_told_from_a_reading_app_without_sec_fetch_headers(library):
    """At http://192.168.x.x or a Tailscale address, browsers send no Sec-Fetch headers. What the library's own pages ask
    comes with the page's address as its Referer: such a request gets no password dialog, and a password the browser
    remembers from one doesn't count, so Log out means logged out. A reading app keeps its Basic login and its dialog,
    a feed it came from in its Referer too."""
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    home = "192.168.1.5:8770"
    page = {"Host": home, "Referer": f"http://{home}/catalog/index.html", "User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15"}
    try:
        for path, accept in (("/api/session", "*/*"), ("/catalog/covers/x.jpg", "image/webp,*/*"), ("/foliate/view.js", "*/*"),
                             ("/catalog/data.js", "*/*")):
            own = client.get(path, headers={**page, "Accept": accept})
            assert own.status_code == 401 and "www-authenticate" not in own.headers, path  # no password dialog
            remembered = client.get(path, headers={**page, "Accept": accept}, auth=("reader", "correct horse"))
            assert remembered.status_code == 401 and "www-authenticate" not in remembered.headers, path  # a dialog's: not counted
        book_frame = {"Host": home, "User-Agent": page["User-Agent"], "Accept": "*/*"}  # a book's blob: frame: no Referer
        for path in ("/fonts/atkinson-italic-700-latin.woff2", "/foliate/view.js"):  # only a browser asks for these
            own = client.get(path, headers=book_frame, auth=("reader", "correct horse"))
            assert own.status_code == 401 and "www-authenticate" not in own.headers, path
        followed = client.get("/catalog/help.html", headers={**page, "Accept": "text/html"})  # a link on the page, followed
        assert followed.status_code == 302 and followed.headers["location"].startswith("/login?next=")
        for referer in ("", f"http://{home}/opds", f"http://{home}/me/opds/continue", "http://elsewhere.example/catalog/index.html"):
            app = client.get("/opds", headers={"Host": home, "Referer": referer, **APP})
            assert app.status_code == 401 and app.headers["www-authenticate"].startswith("Basic "), referer
            assert client.get("/opds", headers={"Host": home, "Referer": referer, **APP}, auth=("reader", "correct horse")).status_code == 200
    finally:
        srv.shutdown()


def test_a_browser_opening_a_feed_goes_to_the_library_page_and_reading_apps_get_the_feed(library, log_in):
    """A feed's address typed into a browser, logged in, goes to the library page instead of showing its XML;
    reading apps are never sent on, whatever they say they take: the feed's type in their Accept settles it."""
    srv, client = start(library)
    log_in(client)
    plain = {"User-Agent": "Mozilla/5.0 (iPad) Safari/605.1.15", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    apps = {"KOReader": {"User-Agent": "KOReader/2026.10 (https://koreader.example/) Linux", "Accept": "*/*"},
            "Panels": {"User-Agent": "Panels/3.2 CFNetwork/1500 Darwin/25.0", "Accept": "application/atom+xml,*/*"},
            "an Atom client": {"User-Agent": "Lantern Wolf Feeds/1.0", "Accept": "application/atom+xml,application/xml;q=0.9"},
            "a Mozilla one": {"User-Agent": "Mozilla/5.0 (Linux; Android 16) Moonwake Reader/4.0",
                              "Accept": "text/html,application/atom+xml;profile=opds-catalog,*/*"}}
    try:
        for path in ("/opds", "/ereader/opds", "/me/opds", "/me/ereader/opds", "/opds/search?q=novel"):
            for browser in (NAVIGATE, plain):
                sent = client.get(path, headers=browser)
                assert sent.status_code == 302 and sent.headers["location"] == "/", (path, browser)
            for name, headers in apps.items():
                fed = client.get(path, headers=headers)
                assert fed.status_code == 200 and fed.headers["content-type"].startswith("application/atom+xml"), (path, name)
            own = client.get(path, headers={"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})  # a page's own fetch
            assert own.status_code == 200, path
        assert client.get("/", headers=NAVIGATE).status_code == 302  # and on to the library page itself
        access.add_user("rory", "correct horse")
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}") as app:  # Basic, the way reading apps log in
            for headers in (plain, {**plain, "User-Agent": "Mozilla/5.0 (Linux; Android 16) Lantern Wolf Reader/2.0"}):
                fed = app.get("/opds", headers=headers, auth=("rory", "correct horse"))  # looks like a browser, logs in as an app
                assert fed.status_code == 200 and fed.headers["content-type"].startswith("application/atom+xml")
    finally:
        srv.shutdown()


def test_the_status_shows_a_browsers_address_first_then_the_reading_apps(monkeypatch):
    """The address people use (address), else this computer's own on the home network while it listens
    there, else the one address it listens on; a browser's address first, then the feeds, labelled. On this computer
    only, it says so."""
    base = {"pid": 1, "port": 8770, "library": "x"}
    loopback = serve.describe({**base, "listen": "127.0.0.1"})
    assert "  On this computer:  http://127.0.0.1:8770/\n" in loopback and "In a browser" not in loopback
    assert "  Other devices:     none: it listens on this computer only (127.0.0.1; listen under [server], or --lan)\n" in loopback
    behind = serve.describe({**base, "listen": "127.0.0.1", "address": "https://testpc.tail1234.ts.net"})  # a proxy's address
    assert ("  In a browser:      https://testpc.tail1234.ts.net/  (the library page, on another device)\n  Reading apps (OPDS):\n"
            "    For tablets:     https://testpc.tail1234.ts.net/opds  (your feed; in full, with https://)\n") in behind
    one = serve.describe({**base, "listen": "192.0.2.10"})
    assert "  On this computer:  http://192.0.2.10:8770/\n  In a browser:      http://192.0.2.10:8770/  (" in one
    six = serve.describe({**base, "listen": "2001:db8::10"})
    assert "  In a browser:      http://[2001:db8::10]:8770/  (" in six and "http://[2001:db8::10]:8770/ereader/opds" in six
    everywhere = serve.describe({**base, "listen": "::"})
    assert "  On this computer:  http://[::1]:8770/\n  In a browser:      http://192.168.1.5:8770/  (" in everywhere
    assert "Home network" not in everywhere + behind + one  # no address set, or listening here only: nothing more
    both = serve.describe({**base, "listen": "0.0.0.0", "address": "https://testpc.tail1234.ts.net"})  # Serve, and an e-reader
    assert "  In a browser:      https://testpc.tail1234.ts.net/  (" in both
    assert ("  Home network:      http://192.168.1.5:8770/  (plain http, for a device that can't use the address above, "
            "such as an e-reader without Tailscale; its feed: /ereader/opds)\n") in both  # in line with the others
    assert "Home network" not in serve.describe({**base, "listen": "0.0.0.0", "address": "http://192.168.1.5:8770"})
    monkeypatch.setattr(access, "lan_address", lambda: "127.0.0.1")  # on no network at all
    assert "  Other devices:     none just now: this computer is on no network\n" in serve.describe({**base, "listen": "0.0.0.0"})


def test_the_home_screen_app_needs_no_login_for_its_icons(library):
    """A phone fetches the icon and the app description when adding the library to its home screen, logged in or not."""
    srv, client = start(library)
    try:
        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == 200 and manifest.json()["display"] == "standalone"
        assert manifest.json()["start_url"] == "/catalog/index.html"
        for path in ("/icon-180.png", "/apple-touch-icon.png", "/icon-512.png", "/favicon.ico"):
            icon = client.get(path)
            assert icon.status_code == 200 and icon.content.startswith(b"\x89PNG")
        assert client.get("/catalog/index.html", headers=BROWSER).status_code == 302  # the library itself still asks
        login = client.get("/login", headers=BROWSER)
        assert 'rel="manifest"' in login.text and 'apple-mobile-web-app-capable' in login.text
        # and its policy lets the browser fetch them: under default-src 'none' alone, Chrome and Edge refused the manifest
        policy = login.headers["content-security-policy"]
        assert "manifest-src 'self'" in policy and "img-src 'self' data:" in policy and "default-src 'none'" in policy
    finally:
        srv.shutdown()


def test_logging_in_and_out(library):
    access.add_user("owen", "correct horse")  # the first user, the owner
    access.add_user("reader", "correct horse")
    srv, client = start(library, access_log=True)
    try:
        wrong = log_in(client, "reader", "battery staple")
        assert wrong.status_code == 401 and "Wrong name or password" in wrong.text and "set-cookie" not in wrong.headers
        assert 'value="reader"' in wrong.text  # the name stays; only the password is asked again
        ok = log_in(client, "reader", "correct horse", next_="/catalog/index.html?x=1#k=comic")
        assert ok.status_code == 303 and ok.headers["location"] == "/catalog/index.html?x=1#k=comic"
        cookie = ok.headers["set-cookie"]
        assert cookie.startswith("hylly=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Max-Age=2592000" in cookie
        assert "Secure" not in cookie  # plain HTTP at home: a Secure cookie would never come back
        token = re.search(r"hylly=([^;]+)", cookie)[1]
        assert client.get("/catalog/index.html", headers=BROWSER).status_code == 200  # the client sends the cookie back
        s = client.get("/api/session").json()
        assert s["session"] and s["by"] == "reader" and s["role"] == "reader" and not s["key"]  # a new user only reads
        assert "idle" not in s  # a server never stops by itself: the page sends it no "still open" every minute
        assert client.get("/login", params={"next": "/opds"}, headers=BROWSER).headers["location"] == "/opds"  # in already
        out = client.post("/logout")
        assert out.status_code == 303 and out.headers["location"] == "/login" and "Max-Age=0" in out.headers["set-cookie"]
        assert client.get("/opds", headers={"Cookie": f"hylly={token}"}).status_code == 401  # the token itself is over too
    finally:
        srv.shutdown()
    log = access.access_log_file().read_text(encoding="utf-8")
    assert "correct horse" not in log and "battery" not in log and token not in log  # never logged


def test_through_a_tunnel(library):
    """Over HTTPS the cookie is Secure and named __Host-, which no other site, not even a neighbouring one, can set;
    another site's form can neither log you in nor out."""
    access.add_user("reader", "correct horse")
    srv, client = start(library, hosts=serve.Hosts(["library.example"]))  # the name the tunnel hands on: one of its own
    try:
        ok = log_in(client, "reader", "correct horse", headers=TUNNEL)
        cookie = ok.headers["set-cookie"]
        assert cookie.startswith("__Host-hylly=") and "; Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie
        token = re.search(r"__Host-hylly=([^;]+)", cookie)[1]
        mine = {**TUNNEL, "Cookie": f"__Host-hylly={token}"}
        assert client.get("/opds", headers=mine).status_code == 200
        assert client.get("/opds", headers={**TUNNEL, "Cookie": f"__Host-hylly={token[:-2]}xx"}).status_code == 401
        for other in ("https://evil.example", "null", "https://library.example.evil.example"):
            assert log_in(client, "reader", "correct horse", headers={**TUNNEL, "Origin": other}).status_code == 403
            assert client.post("/logout", headers={**mine, "Origin": other}).status_code == 403
        assert client.get("/opds", headers=mine).status_code == 200  # still logged in
        assert log_in(client, "reader", "correct horse", headers={**TUNNEL, "Origin": "https://library.example"}).status_code == 303
        out = client.post("/logout", headers={**mine, "Origin": "https://library.example"})
        assert sorted(c.split("=")[0] for c in out.headers.get_list("set-cookie")) == ["__Host-hylly", "hylly"]
        assert client.get("/opds", headers=mine).status_code == 401
    finally:
        srv.shutdown()


def test_sessions_end():
    access.add_user("reader", "correct horse")
    access.add_user("other", "another horse")
    clock, ip = [1_800_000_000.0], "203.0.113.9"
    gate = access.Gate(log=lambda m: None, sessions=access.Sessions(now=lambda: clock[0]))
    sent = lambda token: {"Cookie": f"hylly={token}"}  # noqa: E731
    first = gate.log_in(ip, "reader", "correct horse")[1]
    assert gate.check(ip, sent(first)) == ("ok", "reader")
    assert gate.check("192.168.1.45", sent(first)) == ("ok", "reader")  # the same at home
    restarted = access.Gate(log=lambda m: None, sessions=access.Sessions(now=lambda: clock[0]))
    assert restarted.check(ip, sent(first)) == ("ok", "reader")  # kept across a restart ...
    from hylly import db

    on_disk = b"".join(p.read_bytes() for p in (db.file(), db.file().with_name("hylly.db-wal")) if p.exists())
    assert first.encode() not in on_disk  # ... as a digest only
    clock[0] += access.SESSION_DAYS * 86400 + 1
    assert gate.check(ip, sent(first))[0] == "login"  # after 30 days
    clock[0] -= access.SESSION_DAYS * 86400 + 1
    second = gate.log_in(ip, "other", "another horse")[1]
    access.add_user("other", "a third horse")
    assert gate.check(ip, sent(second))[0] == "login"  # a new password ends it
    third = gate.log_in(ip, "reader", "correct horse")[1]
    access.remove_user("reader")
    assert gate.check(ip, sent(third))[0] == "login" and gate.check(ip, sent(first))[0] == "login"  # so does removing the user
    for junk in ("hylly=", f"hylly={'A' * 43}", "hylly=../../users.txt", f'hylly="{second}"', "hyllyx=" + second):
        assert gate.check(ip, {"Cookie": junk})[0] == "login"


def test_using_the_library_keeps_the_login_going():
    """30 days without use end a login; using it moves the end on, once a day at most, for a year at the most."""
    access.add_user("reader", "correct horse")
    clock, ip, day = [1_800_000_000.0], "203.0.113.9", 86400
    sessions = access.Sessions(now=lambda: clock[0])
    gate = access.Gate(log=lambda m: None, sessions=sessions)
    token = gate.log_in(ip, "reader", "correct horse")[1]
    assert sessions.renew(token) is None  # just logged in: nothing to move
    clock[0] += 20 * day
    assert sessions.renew(token) == access.SESSION_DAYS * day  # used on day 20: 30 days from now
    clock[0] += 3600
    assert sessions.renew(token) is None  # the same day again: the file isn't written on every page
    clock[0] += 25 * day
    assert gate.check(ip, {"Cookie": f"hylly={token}"}) == ("ok", "reader")  # day 45: over without the use on day 20
    for _ in range(access.SESSION_LIMIT_DAYS):  # read every day ...
        clock[0] += day
        sessions.renew(token)
    assert gate.check(ip, {"Cookie": f"hylly={token}"})[0] == "login"  # ... and still, a year after logging in it ends
    later = gate.log_in(ip, "reader", "correct horse")[1]
    clock[0] += access.SESSION_DAYS * day + 1
    assert sessions.renew(later) is None and gate.check(ip, {"Cookie": f"hylly={later}"})[0] == "login"  # 30 days unused
    assert sessions.renew(None) is None and sessions.renew("A" * 43) is None


def test_a_login_used_on_a_day_its_end_moved_already_takes_no_write_lock(monkeypatch):
    """Every page opened, and every question of the page's, renewed the login in a write of the database, though its end
    moves once a day: while another program wrote (a catalog build), each logged-in request waited for it, and past
    the database's wait it failed. The login is read first now, and only an end that moves takes the write lock: on a
    day it moved already, nothing is written, and no other program's write is waited for."""
    import sqlite3
    import time

    from hylly import db

    access.add_user("reader", "correct horse")
    clock, day = [1_800_000_000.0], 86400
    sessions = access.Sessions(now=lambda: clock[0])
    token = access.Gate(log=lambda m: None, sessions=sessions).log_in("203.0.113.9", "reader", "correct horse")[1]
    database, writes = db.database(), []
    begin = database.write
    monkeypatch.setattr(database, "write", lambda: writes.append(clock[0]) or begin())
    assert sessions.renew(token) is None and writes == []  # just logged in: read, and nothing to move
    clock[0] += 2 * day
    assert sessions.renew(token) == access.SESSION_DAYS * day and len(writes) == 1  # its end moves: written
    clock[0] += 3600
    other = sqlite3.connect(db.file(), timeout=0, isolation_level=None)  # another program, in the middle of a write
    try:
        other.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        assert sessions.renew(token) is None and len(writes) == 1  # the same day: read, past the other's write
        assert time.monotonic() - started < 1
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert sessions.renew(None) is None and len(writes) == 1  # no login: nothing asked at all


def test_a_request_reads_the_accounts_and_looks_up_its_login_once(library, monkeypatch):
    """Who is asking is worked out once a request now (serve.Caller): the accounts read once, and the browser's login
    looked up once, whatever the request asks of them after (the library page's session asks the role, the keys and
    the login again and again), where each rule used to read them anew: a logged-in request read every account several
    times, and its login three. A reading app's Basic login reads them once too, and looks up no browser login."""
    access.add_user("owen", "correct horse", role="owner")
    access.add_user("rory", "correct horse", role="reader")
    srv, client = start(library)
    read, looked = [], []
    accounts, user = access.load_accounts, access.Sessions.user
    monkeypatch.setattr(access, "load_accounts", lambda: read.append(1) or accounts())
    monkeypatch.setattr(access.Sessions, "user", lambda self, *a: looked.append(1) or user(self, *a))
    try:
        assert log_in(client, "owen", "correct horse").status_code == 303
        read.clear()
        session = client.get("/api/session").json()
        assert session["by"] == "owen" and session["role"] == "owner" and session["key"] and session["session"]
        assert (len(read), len(looked)) == (1, 1)
        client.cookies.clear()
        read.clear(), looked.clear()
        assert client.get("/opds", auth=("rory", "correct horse"), headers={"User-Agent": "ReadingApp/1.0"}).status_code == 200
        assert (len(read), len(looked)) == (1, 0)
    finally:
        srv.shutdown()


def test_the_login_page_says_how_long_a_login_lasts_as_the_server_keeps_it(library, monkeypatch):
    """The login page, and the page a sign-in link opens, said "30 days" in words of their own, beside
    access.SESSION_DAYS: a change to the one would have left them saying the other. Both are filled in from it now."""
    access.add_user("reader", "correct horse")
    monkeypatch.setattr(access, "SESSION_DAYS", 7)
    srv, client = start(library)
    try:
        page, link = client.get("/login"), client.get("/login", params={"once": access.sign_in_link("reader")})
    finally:
        srv.shutdown()
    for answer in (page, link):
        assert answer.status_code == 200 and "stays logged in for 7 days" in answer.text and "30 days" not in answer.text
    assert "{{days}}" in Path(serve.__file__).with_name("login_page.html").read_text(encoding="utf-8")


def test_the_renewed_cookie_goes_back_to_the_browser(library):
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    try:
        cookie = log_in(client, "reader", "correct horse", headers=TUNNEL).headers["set-cookie"]
        token = re.search(r"__Host-hylly=([^;]+)", cookie)[1]
        mine = {**TUNNEL, "Cookie": f"__Host-hylly={token}"}
        from hylly import db

        def a_day_later():  # as if the last renewal were two days ago
            with db.database().write() as c:
                c.execute("UPDATE browser_login SET until = until - ? WHERE digest = ?", (2 * 86400, access._digest(token)))

        assert "set-cookie" not in client.get("/api/session", headers=mine).headers  # renewed today already
        a_day_later()
        renewed = client.get("/api/session", headers={**mine, "Sec-Fetch-Mode": "cors"}).headers.get("set-cookie", "")
        assert renewed.startswith(f"__Host-hylly={token};") and "; Secure" in renewed and "HttpOnly" in renewed
        assert 2591990 <= int(re.search(r"Max-Age=(\d+)", renewed)[1]) <= 2592000
        assert "set-cookie" not in client.get("/api/session", headers=mine).headers  # once a day
        a_day_later()
        assert "set-cookie" not in client.get("/foliate/view.js", headers=mine).headers  # the reader's files: never
        page = client.get("/catalog/index.html", headers={**mine, "Sec-Fetch-Mode": "navigate", "User-Agent": "Mozilla/5.0"})
        assert page.status_code == 200 and page.headers.get("set-cookie", "").startswith("__Host-hylly=")  # opening it: yes
    finally:
        srv.shutdown()


def test_the_login_page_shares_the_lockout(library):
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    try:
        for _ in range(access.FAILS - 1):
            assert log_in(client, "reader", "guess").status_code == 401
        tenth = log_in(client, "nobody", "guess")
        assert tenth.status_code == 429 and "Too many wrong passwords" in tenth.text
        assert log_in(client, "reader", "correct horse").status_code == 429  # even the right one waits now
        assert client.get("/opds", auth=("reader", "correct horse")).status_code == 429  # and the apps' login too
    finally:
        srv.shutdown()


def test_the_login_page_escapes_and_stays_on_this_site(library):
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    nasty = '"><script>alert(1)</script>'
    try:
        page = log_in(client, nasty, "x", next_=nasty)
        assert "<script>alert" not in page.text and "&quot;&gt;&lt;script&gt;" in page.text
        for elsewhere in ("//evil.example/", "/\\evil.example", "https://evil.example/", "javascript:alert(1)",
                          "/x\r\nSet-Cookie: a=1", "/", ""):
            assert log_in(client, "reader", "correct horse", next_=elsewhere).headers["location"] == "/catalog/index.html"
            client.cookies.clear()
            assert 'name="next" value="/catalog/index.html"' in client.get("/login", params={"next": elsewhere}).text
        for odd in ("/é", "/x\x00y", "/x\ty"):  # nothing a header can't carry
            assert log_in(client, "reader", "correct horse", next_=odd).headers["location"] == "/catalog/index.html"
            client.cookies.clear()
        try:
            huge = log_in(client, "reader", "x" * 500_000)  # a giant form isn't even read, let alone hashed
            assert huge.status_code == 413
        except (httpx.ReadError, httpx.WriteError):
            pass  # Windows may drop the connection while the form is still on its way: turned away unread all the same
        assert log_in(client, "reader", "x" * 3000).status_code == 401  # a long one that fits is cut, not hashed whole
        shelf = "/catalog/index.html#shelf=comics&q=lantern"  # the library page's address, its part after # put in by the page
        assert log_in(client, "reader", "correct horse", next_=shelf).headers["location"] == shelf
        client.cookies.clear()
        assert f'name="next" value="{shelf.replace("&", "&amp;")}"' in log_in(client, "reader", "guess", next_=shelf).text
        for elsewhere in ("//evil.example/#q=x", "https://evil.example/#q=x", "#q=x", "/x#\r\nSet-Cookie: a=1"):
            assert log_in(client, "reader", "correct horse", next_=elsewhere).headers["location"] == "/catalog/index.html"
            client.cookies.clear()
        crowded = client.post("/login", content="&".join(f"f{i}=1" for i in range(50)),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert crowded.status_code == 400  # more fields than the form has: refused, not a crash
    finally:
        srv.shutdown()


def test_the_login_page_carries_the_part_after_hash_on_to_the_page_after(library):
    """The library page's address with a shelf or a search after # (#shelf=..., #q=...) opened on a browser not logged in
    yet: the redirect to /login keeps the address's part after #, which no request carries, and the login page and the
    one-time link's page put it into next, so the login goes on to that shelf. Only onto a path of this server's own (the
    server checks next as ever); never twice."""
    import shutil
    import subprocess

    access.add_user("owen", "correct horse")
    srv, client = start(library)
    try:
        pages = [client.get("/login", params={"next": "/catalog/index.html"}, headers=NAVIGATE).text,
                 client.get("/login", params={"once": access.sign_in_link("owen"), "next": "/catalog/index.html"}, headers=NAVIGATE).text]
    finally:
        srv.shutdown()
    assert 'name="once"' in pages[1]
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's script runs only in a browser here")
    for page in pages:
        code = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
        assert "location.hash" in code
        for hash_, value, wanted in (("#shelf=comics&q=lantern", "/catalog/index.html", "/catalog/index.html#shelf=comics&q=lantern"),
                                     ("", "/catalog/index.html", "/catalog/index.html"), ("#", "/opds", "/opds"),
                                     ("#q=x", "/catalog/index.html#q=y", "/catalog/index.html#q=y"),
                                     ("#q=x", "https://evil.example/", "https://evil.example/")):
            script = (f"const next = {{value: {json.dumps(value)}}}, location = {{hash: {json.dumps(hash_)}}};\n"
                      "const document = {querySelector: () => next};\n"
                      f"{code}\nprocess.stdout.write(JSON.stringify(next.value));\n")
            out = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
            assert json.loads(out.stdout) == wanted, (hash_, value)


def test_too_many_wrong_passwords(library):
    access.add_user("reader", "correct horse")
    srv, client = start(library)
    try:
        for _ in range(access.FAILS):
            assert client.get("/opds", auth=("reader", "guess")).status_code == 401
        locked = client.get("/opds", auth=("reader", "correct horse"))
        assert locked.status_code == 429 and locked.headers["retry-after"]
    finally:
        srv.shutdown()


class Clock:
    """A made-up clock for the gate: it moves only when told to, or when a try waits for its turn (sleep), unless the
    waits are those of tries side by side, each in its own thread, which this clock doesn't move for."""

    def __init__(self, moves: bool = True) -> None:
        self.at, self.moves, self.waits = 1000.0, moves, []

    def __call__(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self.moves:
            self.at += seconds


def test_the_lockout_forgets_an_address_with_nothing_against_it():
    """An address asked to log in once kept a row, an empty one too, for as long as the server ran; now only the
    wrong logins of the last 15 minutes are kept, and an address whose last one is older goes."""
    clock = Clock()
    gate = access.Gate(log=lambda m: None, clock=clock, sleep=clock.sleep)
    for i in range(50):
        assert gate.check(f"198.51.100.{i}", {"Authorization": "Basic " + "bm9ib2R5Omd1ZXNz"})[0] == "login"  # nobody:guess
    assert len(gate._fails) == 50
    for i in range(50, 100):
        assert gate.check(f"198.51.100.{i}", {})[0] == "login"  # only asked: nothing kept of it
    assert len(gate._fails) == 50
    clock.at += access.WINDOW
    assert gate.check("198.51.100.200", {"Authorization": "Basic " + "bm9ib2R5Omd1ZXNz"})[0] == "login"
    assert list(gate._fails) == ["198.51.100.200"] and not gate._turns  # the old ones went with the next wrong login
    clock.at += access.WINDOW
    assert gate.check("198.51.100.200", {})[0] == "login" and not gate._fails


def test_the_look_over_every_address_counted_comes_at_most_once_a_second():
    """Every wrong login and every turn once looked over all the addresses counted, under the lock
    every request's check takes, and logins that can't be read come unpaced: with 50,000 senders, 2 ms a call. The
    old ones go at most once a second (SWEEP); until then they only wait, and count for nothing (_locked)."""
    clock = Clock()
    gate = access.Gate(log=lambda m: None, clock=clock, sleep=clock.sleep)
    garbled = {"Authorization": "Basic !!!"}  # a login that can't be read: a wrong one, with no turn

    def at(seconds: float, sender: str) -> set[str]:
        clock.at = 1000.0 + seconds
        gate.check(sender, garbled)
        return set(gate._fails)

    assert at(0, "198.51.100.1") == {"198.51.100.1"}
    assert at(0.5, "198.51.100.2") == {"198.51.100.1", "198.51.100.2"}
    assert at(access.WINDOW + 0.2, "198.51.100.3") == {"198.51.100.2", "198.51.100.3"}  # the first old: gone
    assert at(access.WINDOW + 0.6, "198.51.100.4") == {"198.51.100.2", "198.51.100.3", "198.51.100.4"}  # within the second
    assert at(access.WINDOW + 1.3, "198.51.100.5") == {"198.51.100.3", "198.51.100.4", "198.51.100.5"}


def test_an_ipv6_sender_counts_by_its_64_and_a_device_at_home_by_its_own_address():
    """Forty wrong logins from forty addresses of one IPv6 /64 were once all answered. Now a /64 from the internet is
    one sender, which one home or one machine is given whole; a home network's or a tailnet's own addresses
    (fc00::/7, fe80::/10) are each one, as an IPv4 address is."""
    clock = Clock()
    gate = access.Gate(log=lambda m: None, clock=clock, sleep=clock.sleep)
    for i in range(access.FAILS):  # the tenth wrong one locks the sender out
        assert gate.log_in(f"2001:db8:7:1::{i + 1:x}", "nobody", "guess")[0] == ("locked" if i == access.FAILS - 1 else "wrong")
    assert gate.log_in("2001:db8:7:1:ffff::9", "nobody", "guess")[0] == "locked"  # a fresh address of that /64
    assert gate.log_in("2001:db8:7:2::1", "nobody", "guess")[0] == "wrong"  # the next /64 is someone else
    assert access.sender_key("2001:db8:7:1:abcd::1") == "2001:db8:7:1::/64" and access.sender_key("192.0.2.7") == "192.0.2.7"
    for home in ("fd00:7:7:7::1", "fe80::1%en0", "::1"):
        assert access.sender_key(home) == home.partition("%")[0]
    for i in range(access.FAILS - 1):
        assert gate.log_in(f"fd00:7:7:7::{i + 1:x}", "nobody", "guess")[0] == "wrong"
    assert gate.log_in("fd00:7:7:7::1", "nobody", "guess")[0] == "wrong"  # the first one's second: not locked


def test_past_a_ceiling_wrong_logins_slow_down_and_never_lock():
    """Once nothing counted wrong logins for one name, nor all of them together, so a stranger changing addresses
    could guess at four a second without end. Past SLOW_NAME for one name, or SLOW_ALL from everyone, each password
    is checked at its turn, SLOW_TURN seconds apart, waited for in the request's own thread for at most SLOW_WAIT; a
    turn further off is answered at once. A few wrong ones never slow anyone; the right password always gets in at
    its turn; the window passing ends it."""
    access.add_user("owen", "correct horse")
    access.add_user("ann", "correct horse")
    said, clock = [], Clock(moves=False)  # tries side by side: none moves the clock for another
    gate = access.Gate(log=said.append, clock=clock, sleep=clock.sleep)
    address = iter(f"198.51.100.{i}" for i in range(1, 250))  # a fresh address at every try: no lockout helps
    for _ in range(3):
        assert gate.log_in(next(address), "owen", "a typo")[0] == "wrong"
    assert gate.log_in(next(address), "owen", "correct horse")[0] == "ok" and not clock.waits  # a few typos: no wait at all
    for _ in range(access.SLOW_NAME - 3):
        assert gate.log_in(next(address), "owen", "guess")[0] == "wrong"
    assert not clock.waits and said == [f"{access.SLOW_NAME} wrong logins for 'owen' within 15 minutes: each try now "
                                        f"waits its turn, {access.SLOW_TURN:g} seconds apart, until there are fewer."]
    turns = [gate.log_in(next(address), "owen", "guess")[0] for _ in range(5)]  # five at once
    assert turns == ["wrong", "wrong", "wrong", "slow", "slow"] and clock.waits == [2.0, 4.0]  # the first's turn is now
    assert gate.log_in(next(address), "ann", "a typo")[0] == "wrong"  # another name isn't slowed
    assert gate.check(next(address), {"Authorization": "Basic b3dlbjpndWVzcw=="})[0] == "slow"  # owen:guess in an app
    clock.at += access.SLOW_WAIT  # the turns taken have passed
    clock.moves = True
    assert gate.log_in(next(address), "owen", "correct horse")[0] == "ok"  # the right one, at its turn: no lock
    assert len(said) == 1  # the slowdown said once
    clock.at += access.WINDOW
    clock.waits.clear()
    assert gate.log_in(next(address), "owen", "a typo")[0] == "wrong" and not clock.waits  # the window has passed


def test_past_a_ceiling_from_all_addresses_together_every_name_waits_its_turn():
    """Many names, each wrong a few times, from many addresses: past SLOW_ALL in 15 minutes, every check takes a
    turn, whoever's name, and the server's log says so once."""
    access.add_user("owen", "correct horse")
    said, clock = [], Clock()
    gate = access.Gate(log=said.append, clock=clock, sleep=clock.sleep)
    for i in range(access.SLOW_ALL):
        assert gate.log_in(f"203.0.113.{i}", f"guess{i % 7}", "guess")[0] == "wrong"
    assert not clock.waits and said[-1].startswith(f"{access.SLOW_ALL} wrong logins from all addresses together within 15")
    assert gate.log_in("192.0.2.1", "owen", "correct horse")[0] == "ok" and clock.waits == []  # the first turn is now
    assert gate.log_in("192.0.2.2", "owen", "correct horse")[0] == "ok" and clock.waits == [access.SLOW_TURN]
    assert sum("all addresses together" in line for line in said) == 1


def test_the_server_says_try_again_in_a_few_seconds_when_a_try_has_no_turn(library, monkeypatch):
    access.add_user("reader", "correct horse")
    monkeypatch.setattr(access, "SLOW_NAME", 2)
    monkeypatch.setattr(access, "SLOW_TURN", 60.0)  # a turn far off: none within SLOW_WAIT after the first
    srv, client = start(library)
    try:
        for i in range(2):
            assert client.get("/opds", auth=("reader", "guess"), headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code == 401
        assert client.get("/opds", auth=("reader", "guess"), headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 401
        slow = client.get("/opds", auth=("reader", "correct horse"), headers={"X-Forwarded-For": "203.0.113.6"})
        assert slow.status_code == 429 and slow.headers["retry-after"] == f"{access.SLOW_WAIT:.0f}" and b"few seconds" in slow.content
        page = log_in(client, "reader", "guess", headers={"X-Forwarded-For": "203.0.113.7"})
        assert page.status_code == 429 and "try again in a few seconds" in page.text
    finally:
        srv.shutdown()


def test_a_sender_inside_never_waits_a_turn_while_a_stranger_guesses_past_the_ceiling():
    """A turn goes to whoever asks first, so a stranger guessing from enough addresses once kept
    every fresh login waiting or refused, the owner's on the tailnet and every reading app's after a restart too. A
    sender inside (this computer, the home network, the tailnet: access.inside) never waits a turn and is never told
    "slow"; its wrong logins still count, towards its own lockout and the ceilings. From outside, a fresh login may be
    told to try again while the stranger goes on."""
    access.add_user("owen", "correct horse")
    clock = Clock(moves=False)  # tries side by side
    gate = access.Gate(log=lambda m: None, clock=clock, sleep=clock.sleep)
    stranger = iter(f"198.51.100.{i}" for i in range(1, 250))
    while gate.log_in(next(stranger), "owen", "guess")[0] != "slow":  # past the ceiling, and every turn taken
        pass
    assert gate.log_in("203.0.113.40", "owen", "correct horse")[0] == "slow"  # outside: no turn now
    waited = list(clock.waits)
    for home in ("127.0.0.1", "::1", "192.168.1.20", "10.0.0.7", "100.101.102.103", "fd00:7:7:7::5", "fe80::5%en0"):
        assert access.inside(home), home
        assert gate.log_in(home, "owen", "correct horse")[0] == "ok", home
    assert clock.waits == waited  # none of them waited
    wrong, per_name = len(gate._wrong), sum(gate._per_name.values())
    for i in range(access.FAILS):  # an inside sender's wrong ones still count: towards its lockout, and the ceilings
        assert gate.log_in("192.168.1.30", "owen", "guess")[0] == ("locked" if i == access.FAILS - 1 else "wrong")
    assert len(gate._wrong) == wrong + access.FAILS and sum(gate._per_name.values()) == per_name + access.FAILS
    assert gate.log_in("192.168.1.31", "owen", "correct horse")[0] == "ok" and clock.waits == waited  # its neighbour isn't
    assert gate.check("100.101.102.103", {"Authorization": "Basic b3dlbjpndWVzcw=="})[0] == "login"  # an app: no "slow"
    for outside in ("203.0.113.9", "2001:db8::1", "8.8.8.8", "::ffff:203.0.113.9"):
        assert not access.inside(outside), outside


def test_through_tailscale_serve_a_tailnet_device_is_inside_and_through_funnel_a_visitor_is_outside(library, monkeypatch):
    """Who is inside goes by the sender client_address works out, never by what a header says: Tailscale Serve and
    Funnel both come from this computer and add the visitor's address at the end of X-Forwarded-For; a tailnet
    device's is its 100.x (inside), a visitor's through Funnel a public one (outside), and so is any public address a
    request from this computer names. A made-up inside address before it changes nothing."""
    for header, sender, home in (("100.101.102.103", "100.101.102.103", True), ("203.0.113.9", "203.0.113.9", False),
                                 ("100.101.102.103, 203.0.113.9", "203.0.113.9", False), (None, "127.0.0.1", True)):
        found = access.client_address("127.0.0.1", {"X-Forwarded-For": header} if header else {})
        assert found == sender and access.inside(found) is home, header
    assert access.client_address("192.168.1.5", {"X-Forwarded-For": "203.0.113.9"}) == "192.168.1.5"  # no trusted proxy
    access.add_user("reader", "correct horse")
    monkeypatch.setattr(access, "SLOW_NAME", 2)
    monkeypatch.setattr(access, "SLOW_TURN", 60.0)  # a turn far off: none within SLOW_WAIT after the first
    srv, client = start(library)
    try:
        for i in range(3):  # a stranger through Funnel, from three addresses: past the ceiling, and the one turn taken
            client.get("/opds", auth=("reader", "guess"), headers={"X-Forwarded-For": f"203.0.113.{i}"})
        funnel = client.get("/opds", auth=("reader", "correct horse"), headers={"X-Forwarded-For": "198.51.100.7"})
        assert funnel.status_code == 429 and b"few seconds" in funnel.content
        serve_ = client.get("/opds", auth=("reader", "correct horse"), headers={"X-Forwarded-For": "100.101.102.103"})
        assert serve_.status_code == 200  # a device of the tailnet through Serve, at once
        assert client.get("/opds", auth=("reader", "correct horse")).status_code == 200  # this computer itself
        page = log_in(client, "reader", "correct horse", headers={"X-Forwarded-For": "100.101.102.104"})
        assert page.status_code == 303  # the login page too
    finally:
        srv.shutdown()


def test_access_log(library):
    access.add_user("reader", "correct horse")
    srv, client = start(library, access_log=True)
    try:
        client.get("/opds/search", params={"q": "novel"}, auth=("reader", "correct horse"), headers={"User-Agent": "KOReader/2026.07"})
        client.get("/ping", auth=("reader", "correct horse"))  # the page's keep-alive is left out
        client.get("/opds")  # refused: no login
        client.get("/opds", auth=("admin", "guess"))  # refused: a name that isn't a user's is logged by its length only
        client.get("/opds", auth=("reader", "guess"))  # a user's name, with a wrong password: logged as it is
    finally:
        srv.shutdown()
    lines = access.access_log_file().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    combined = re.compile(r'^127\.0\.0\.1 - (\S+) \[\d\d/\w{3}/\d{4}:\d\d:\d\d:\d\d [+-]\d{4}\] "([^"]*)" (\d{3}) (\S+) "[^"]*" "([^"]*)"$')
    parsed = [combined.match(line) for line in lines]
    assert all(parsed), lines
    # each line is written as its answer goes, by the thread that gave it: in the order they ended, not always as asked
    (found,) = [p for p in parsed if p[3] == "200"]
    assert found.groups()[:3] == ("reader", "GET /opds/search?q=novel HTTP/1.1", "200") and found[5] == "KOReader/2026.07"
    assert sorted(p.group(1, 3) for p in parsed if p[3] != "200") == [
        ("(an_unknown_name_of_5_characters)", "401"), ("-", "401"), ("reader", "401")]


def test_a_name_that_isnt_a_users_never_reaches_a_log(library, capsys):
    """People type their password in the name field. A name tried at a login reaches the access log, the server's log
    and the alarms (bell.log) only when it is a user's; any other only by its length."""
    from hylly import bell

    access.add_user("reader", "correct horse")
    typed = "Tulip Kettle 42"  # a password, typed where the name goes
    srv, client = start(library, access_log=True)
    try:
        assert log_in(client, "reader", "guess").status_code == 401  # a user's name, a wrong password
        for _ in range(access.FAILS - 3):
            assert client.get("/opds", auth=(typed, "guess")).status_code == 401  # in a reading app
        assert log_in(client, "", "guess").status_code == 401  # no name at all
        assert log_in(client, typed, "guess").status_code == 429  # the tenth wrong one: the address is locked out
    finally:
        srv.shutdown()
    logs = {"access.log": access.access_log_file().read_text(encoding="utf-8"), "server.log": capsys.readouterr().err,
            "bell.log": bell.log_file().read_text(encoding="utf-8")}
    for place, text in logs.items():
        assert "Tulip" not in text and "Kettle" not in text, place
    assert logs["access.log"].count(" - (an_unknown_name_of_15_characters) [") == access.FAILS - 2
    assert " - reader [" in logs["access.log"]
    assert "10 wrong logins from 127.0.0.1 (last tried as an unknown name of 15 characters)" in logs["server.log"]
    assert "(last tried as an unknown name of 15 characters)" in logs["bell.log"]


def test_the_access_log_keeps_30_days(monkeypatch):
    """Lines older than 30 days go at the server's start, the log on or off, and at every rotation: by size, and at the
    first request of a new day, as a server may run for months without filling 5 MB. Each line goes by its own day:
    one dated far ahead stays (a clock behind mustn't cost real lines) and doesn't keep the old ones after it."""
    path, older = access.access_log_file(), access.access_log_file().with_suffix(".log.1")
    path.parent.mkdir(parents=True, exist_ok=True)

    def line(days_ago: int, book: str) -> str:
        when = (datetime.now().astimezone() - timedelta(days=days_ago)).strftime("%d/%b/%Y:%H:%M:%S %z")
        return f'203.0.113.9 - reader [{when}] "GET /books/{book}.epub HTTP/1.1" 200 5 "-" "KOReader/2026.07"\n'

    def books(file: Path) -> list[str]:
        return re.findall(r"/books/(\w)\.epub", file.read_text(encoding="utf-8"))

    older.write_text(line(45, "a"), encoding="utf-8")
    path.write_text(line(-400, "b") + line(31, "c") + "a torn line\n" + line(30, "d") + line(-1, "e") + line(1, "f"),
                    encoding="utf-8")
    access.AccessLog(False)  # a server starts, with the log turned off
    assert not older.exists()  # a file of old lines only goes whole
    assert books(path) == ["b", "d", "e", "f"] and "torn" not in path.read_text(encoding="utf-8")  # b and e are ahead: kept
    day = [date.today()]
    log = access.AccessLog(True, today=lambda: day[0])
    day[0] += timedelta(days=1)  # the next day, d is 31 days old: it goes at the first request
    log.write("203.0.113.9", "reader", "GET /books/g.epub HTTP/1.1", 200, "5", None, "KOReader/2026.07")
    assert books(path) == ["b", "e", "f", "g"]  # b, dated ahead, stays until it is old
    path.write_text(line(40, "h") + line(2, "i"), encoding="utf-8")
    monkeypatch.setattr(access, "LOG_LIMIT", 10)  # beyond 5 MB, as it were: a rotation the same day
    log.write("203.0.113.9", "reader", "GET /books/j.epub HTTP/1.1", 200, "5", None, "KOReader/2026.07")
    assert books(older) == ["i"] and books(path) == ["j"]


def test_a_log_file_that_cant_be_changed_is_tried_again_within_the_hour(monkeypatch):
    """Held open by another program (on Windows), a file keeps its old lines until a later try: not the next day, and
    not at every request, each of which would wait for it."""
    path = access.access_log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now().astimezone() - timedelta(days=40)).strftime("%d/%b/%Y:%H:%M:%S %z")
    path.write_text(f'203.0.113.9 - reader [{old}] "GET /books/k.epub HTTP/1.1" 200 5 "-" "-"\n', encoding="utf-8")
    tries, real = [], access._drop_old

    def held(file, since, until):
        tries.append(file.name)
        raise PermissionError("in use")

    monkeypatch.setattr(access, "_drop_old", held)
    log = access.AccessLog(False)
    log.write("203.0.113.9", None, "GET /opds HTTP/1.1", 200, "5", None, None)
    assert tries == ["access.log.1", "access.log"] and path.exists()  # at the start, and not again at the next request
    monkeypatch.setattr(access, "_drop_old", real)
    log._again -= access.AccessLog.AGAIN  # an hour later
    log.write("203.0.113.9", None, "GET /opds HTTP/1.1", 200, "5", None, None)
    assert not path.exists()  # its only line was old


def test_huge_requests_are_cut_in_the_access_log_and_cant_push_its_history_out():
    """The request line, Referer and User-Agent once went in whole, up to 64 KB each, so sixty requests without a login
    rolled the 5 MB log twice and its 30 days were gone. Each is cut now, its start and end kept with a mark between."""
    log = access.AccessLog(True)
    huge = "/humble/" + "x" * 60_000 + ".epub"
    for _ in range(60):
        log.write("203.0.113.9", None, f"GET {huge} HTTP/1.1", 401, "12", "https://elsewhere.example/" + "r" * 60_000,
                  "Mozilla/5.0 (iPhone; " + "u" * 60_000 + ") Safari/605.1.15")
    path = access.access_log_file()
    lines = path.read_text(encoding="utf-8").split("\n")[:-1]
    assert len(lines) == 60 and path.stat().st_size < 60 * 3000 and not path.with_suffix(".log.1").exists()
    found = re.match(r'^\S+ - - \[[^\]]+\] "([^"]*)" 401 12 "([^"]*)" "([^"]*)"$', lines[0])
    request, referer, agent = found.groups()
    assert len(request) == access.FIELD_MOST["request"] and request.startswith("GET /humble/xxx") and request.endswith(".epub HTTP/1.1")
    assert "…" in request and len(referer) == len(agent) == 200 and agent.endswith(") Safari/605.1.15")
    from hylly import bell

    assert bell.LINE.match(lines[0]) and bell.is_file(bell.request_path(bell.LINE.match(lines[0])[5]))  # still a book's download
    log.write("203.0.113.9", None, 'GET /a\\" HTTP/1.1' + "\\" * 400, 401, None, None, None)  # a cut can't leave half an escape
    assert re.fullmatch(r'\S+ - - \[[^\]]+\] "(?:[^"\\]|\\.)*" 401 - "-" "-"', path.read_text(encoding="utf-8").split("\n")[-2])


def test_the_tail_of_a_log_starts_at_a_whole_line_and_ends_lines_at_line_breaks_only(tmp_path):
    """A read begun mid-file once showed the middle of a line as if it began one; and U+0085, which a stranger's
    User-Agent can carry, made two lines of one, the second looking like a line of its own."""
    path = tmp_path / "server.log"
    path.write_text("x" * 9000 + " the end of a long line\n" + "".join(f"line {i}\n" for i in range(3)), encoding="utf-8")
    assert access.tail(path, 2) == ["line 1", "line 2"]
    assert access.tail(path, 10) == ["line 0", "line 1", "line 2"]  # not the long one's end, as if it were a line
    path.write_text('203.0.113.9 - - [x] "GET / HTTP/1.1" 401 - "-" "Bot\u0085203.0.113.1 - owen [x] \u202e"\r\nlast\n',
                    encoding="utf-8")
    assert access.tail(path, 5) == ['203.0.113.9 - - [x] "GET / HTTP/1.1" 401 - "-" "Bot?203.0.113.1 - owen [x] "', "last"]
    assert access.tail(tmp_path / "none.log", 5) == [] and access.tail(tmp_path, 5) == []
    # a read that begins exactly at a line's start keeps that line: twelve of 541 letters or so, 6,496 bytes in all
    twelve = [f"L{i:02d} " + "x" * (536 if i else 540) for i in range(12)]
    path.write_bytes(("an older line\n" + "".join(line + "\n" for line in twelve)).encode())  # \n as the log has it, on Windows too
    assert path.stat().st_size - (200 * 12 + 4096) == len("an older line\n")  # where the read begins
    assert access.tail(path, 12) == twelve


def test_a_strangers_user_agent_with_a_line_end_of_its_own_stays_one_line_and_in_the_summary(monkeypatch):
    """Byte 0x85 once split an access-log line in two for every reader that used splitlines: the server page's tail,
    hylly logs, and the daily summary, which then lost that request."""
    from hylly import bell

    access.AccessLog(True).write("203.0.113.9", "owen", "GET /books/a.epub HTTP/1.1", 200, "5", None,
                                 "Lantern\u0085203.0.113.1 - - [01/Jan/2026:00:00:00 +0000] \u2028\u202eforged")
    text = access.access_log_file().read_text(encoding="utf-8")
    assert text.count("\n") == 1 and "\u0085" not in text and "\u2028" not in text and "\u202e" not in text
    assert '"Lantern?203.0.113.1 - - [01/Jan/2026:00:00:00 +0000] ?forged"' in text
    assert len(access.tail(access.access_log_file(), 5)) == 1
    now = datetime.now().astimezone()
    assert "Downloads: owen 1." in bell.summary(text.split("\n"), now)
    sent = []
    watch = bell.Watch(log=lambda m: None)
    monkeypatch.setattr(watch, "_send", lambda *a, **k: sent.append(a[1]))
    monkeypatch.setattr(watch, "_record", lambda *a: None)
    watch.conf["daily_summary"] = 0
    watch.maybe_summary()
    assert sent and "Downloads: owen 1." in sent[0]


def test_an_address_with_a_zone_is_no_sender_and_the_address_field_is_cleaned():
    """An IPv6 address with a zone (::1%any words) once passed as an address, and the address was the one field of an
    access-log line written as it came. A forwarded address has no zone: it names no sender. And whatever the address
    field holds, it can't end the field or the line."""
    for forwarded in ("::1%any words", "fe80::1%eth0", '[fe80::1%x" 401]'):
        assert access.client_address("127.0.0.1", {"X-Forwarded-For": forwarded}) == "127.0.0.1", forwarded
    access.AccessLog(True).write('::1%a b"\n203.0.113.99 - - [01/Jan/2026:00:00:00 +0000] "GET /forged', None,
                                 "GET /opds HTTP/1.1", 401, None, None, None)
    line = access.access_log_file().read_text(encoding="utf-8")
    assert line.count("\n") == 1 and line.startswith('::1%a_b\\"?203.0.113.99_-_-_[01/Jan/2026:00:00:00_+0000]_\\"GET_/forged - - [')
    assert set(access.PROXY_HEADERS) < set(access.FORWARDED)  # one list of the headers that name a sender


def test_an_access_log_that_cant_be_written_is_said_once_and_the_alarms_still_see_each_request(library, monkeypatch, capsys):
    """A full disk, or the file held by another program on Windows: the write once raised after the answer was sent,
    so the watcher behind it, which counts downloads and the open door for the alarms, never saw the request, and the
    server's log got a traceback for each one. Now the alarms see it first, and the log says so once."""
    from hylly import bell

    access.add_user("reader", "correct horse")
    access.access_log_file().mkdir(parents=True)  # a folder where the file goes: no line can be written to it
    seen = []
    real = bell.Watch.request
    monkeypatch.setattr(bell.Watch, "request", lambda self, *a: (seen.append(a[3]), real(self, *a)))
    srv, client = start(library, access_log=True)
    try:
        for _ in range(3):
            assert client.get("/opds", auth=("reader", "correct horse")).status_code == 200
    finally:
        srv.shutdown()
    assert seen == ["/opds"] * 3
    err = capsys.readouterr().err
    assert err.count("can't be written") == 1 and "requests aren't logged until it can be" in err and "Traceback" not in err
    told = []
    log = access.AccessLog(True, log=told.append)
    for _ in range(2):
        log.write("203.0.113.9", None, "GET / HTTP/1.1", 401, None, None, None)
    access.access_log_file().rmdir()
    log.write("203.0.113.9", None, "GET / HTTP/1.1", 401, None, None, None)  # it works again ...
    assert access.access_log_file().read_text(encoding="utf-8").count("\n") == 1 and len(told) == 1
    access.access_log_file().unlink()
    access.access_log_file().mkdir()
    log.write("203.0.113.9", None, "GET / HTTP/1.1", 401, None, None, None)  # ... so a later failure is said again
    assert len(told) == 2


def test_no_log_unless_asked(library, log_in):
    srv, client = start(library)
    try:
        assert log_in(client).get("/opds").status_code == 200
    finally:
        srv.shutdown()
    assert not access.access_log_file().exists()


def test_the_sender_behind_a_trusted_proxy():
    """trusted_proxies, this computer's own as they come: their forwarding headers name the sender, anyone else's are
    ignored. Of a chain, the nearest entry that isn't a trusted proxy: what's further left is whatever the sender typed."""
    headers = {"X-Forwarded-For": "198.51.100.7, 203.0.113.9"}  # the first entry is whatever the sender claimed
    assert access.client_address("127.0.0.1", headers) == "203.0.113.9"  # the one the tunnel added
    typed = {"CF-Connecting-IP": "203.0.113.5", "X-Real-IP": "203.0.113.6", **headers}  # what a sender can type itself
    assert access.client_address("127.0.0.1", typed) == "203.0.113.9"  # ignored: only the tunnel's own header counts
    assert access.client_address("127.0.0.1", typed, "CF-Connecting-IP") == "203.0.113.5"  # behind cloudflared
    assert access.client_address("::1", {"Forwarded": 'for=198.51.100.1, for="[2001:db8::1]";proto=https'}, "Forwarded") == "2001:db8::1"
    assert access.client_address("192.168.1.45", headers) == "192.168.1.45"  # a device at home can't claim another address
    assert access.client_address("127.0.0.1", {"X-Forwarded-For": "not an address"}) == "127.0.0.1"
    assert access.client_address("::ffff:127.0.0.1", headers) == "203.0.113.9"  # this computer, as a server on "::" sees it
    assert access.client_address("::ffff:192.0.2.45", headers) == "192.0.2.45"  # and anyone else, plainly
    containers = access.LOOPBACK + (ipaddress.ip_network("172.18.0.0/16"),)  # a proxy in another container, listed
    assert access.client_address("172.18.0.3", headers, proxies=containers) == "203.0.113.9"
    assert access.client_address("172.18.0.3", headers) == "172.18.0.3"  # not listed: its headers count for nothing
    assert access.client_address("127.0.0.1", headers, proxies=(ipaddress.ip_network("172.18.0.0/16"),)) == "127.0.0.1"
    chained = {"X-Forwarded-For": "192.0.2.66, 198.51.100.7, 172.18.0.9"}  # through two proxies, both listed
    assert access.client_address("172.18.0.3", chained, proxies=containers) == "198.51.100.7"
    assert access.client_address("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}) == "127.0.0.1"  # all of them proxies: the farthest
    assert not access.is_trusted("not an address") and access.is_trusted("::ffff:127.0.0.2")
    assert access.trusted_proxies() == access.LOOPBACK  # as they come
    settings_say('[server]\ntrusted_proxies = ["192.0.2.10", "2001:db8::/32"]\n')
    assert access.trusted_proxies() == (ipaddress.ip_network("192.0.2.10/32"), ipaddress.ip_network("2001:db8::/32"))
    settings_say("[server]\ntrusted_proxies = []\n")
    assert access.trusted_proxies() == () and access.client_address("127.0.0.1", headers, proxies=()) == "127.0.0.1"
    settings_say("")


def test_every_line_of_a_forwarding_header_counts_and_only_an_address_in_its_own_words_is_a_sender():
    """A proxy may add a line of its own instead of adding to the one there (HAProxy does): the sender's own line then
    comes first, and reading only that would let the sender pick its address. An entry that names no address ends the
    walk at the nearest one known. Nothing a header wrote is ever kept as written: a scope id may hold any text, quotes
    and line ends too, which would go into the access log and the alarms as they are."""
    from http.client import HTTPMessage

    def lines(name: str, *values: str) -> HTTPMessage:
        found = HTTPMessage()
        for value in values:
            found[name] = value  # a line of its own each
        return found

    assert access.client_address("127.0.0.1", lines("X-Forwarded-For", "203.0.113.77", "198.51.100.25")) == "198.51.100.25"
    assert access.client_address("127.0.0.1", lines("Forwarded", "for=203.0.113.77", "for=198.51.100.25"), "Forwarded") == "198.51.100.25"
    two = lines("CF-Connecting-IP", "203.0.113.77", "198.51.100.25")  # one address, set by the proxy: two can't say which
    assert access.client_address("127.0.0.1", two, "CF-Connecting-IP") == "127.0.0.1"
    assert access.client_address("127.0.0.1", lines("CF-Connecting-IP", "198.51.100.25"), "CF-Connecting-IP") == "198.51.100.25"
    for forged in ('fe80::1%x" 299 0 "-" "-', "fe80::1%x\r\n198.51.100.9 - - [01/Jan/2026:00:00:00 +0000]", "fe80::1%eth0",
                   '198.51.100.9" 200', "198.51.100.9\r\n203.0.113.5"):
        for header, trust in (("X-Forwarded-For", "X-Forwarded-For"), ("X-Real-IP", "X-Real-IP")):
            assert access.client_address("127.0.0.1", {header: forged}, trust) == "127.0.0.1", (forged, header)
        assert access.client_address("127.0.0.1", {"Forwarded": f'for="{forged}"'}, "Forwarded") == "127.0.0.1", forged
    for written, sender in (("192.0.2.7:4711", "192.0.2.7"), ("[2001:DB8::7]:4711", "2001:db8::7"), ("[2001:db8::7]", "2001:db8::7"),
                            ("2001:0db8:0000::0007", "2001:db8::7"), ('"198.51.100.7"', "198.51.100.7"), ("::ffff:192.0.2.7", "192.0.2.7")):
        assert access.client_address("127.0.0.1", {"X-Forwarded-For": f"203.0.113.66, {written}"}) == sender, written
    assert access.client_address("::1", {"Forwarded": 'for="[2001:db8:cafe::17]:4711";proto=https'}, "Forwarded") == "2001:db8:cafe::17"
    assert access.client_address("::1", {"Forwarded": "for=192.0.2.60;proto=http;by=203.0.113.43"}, "Forwarded") == "192.0.2.60"
    containers = access.LOOPBACK + (ipaddress.ip_network("172.18.0.0/16"),)
    for written in ("for=203.0.113.66, for=_hidden, for=172.18.0.9", "for=203.0.113.66, for=unknown, for=172.18.0.9",
                    "for=203.0.113.66, proto=https, for=172.18.0.9", "for=203.0.113.66, for=garbage, for=172.18.0.9"):
        assert access.client_address("172.18.0.3", {"Forwarded": written}, "Forwarded", containers) == "172.18.0.9", written
    assert access.client_address("172.18.0.3", {"Forwarded": "for=203.0.113.66, for=unknown"}, "Forwarded", containers) == "172.18.0.3"
    assert access.client_address("172.18.0.3", {"X-Forwarded-For": "203.0.113.66, garbage, 172.18.0.9"}, proxies=containers) == "172.18.0.9"
    assert access.client_address("127.0.0.1", {"X-Forwarded-For": "203.0.113.66, , 198.51.100.7"}) == "198.51.100.7"  # a blank: nothing


def test_someone_guessing_through_a_trusted_proxy_is_locked_out_and_you_are_not(library):
    access.add_user("reader", "correct horse")
    srv, client = start(library, access_log=True)
    try:
        for _ in range(access.FAILS):  # someone guessing through the tunnel is locked out ...
            client.get("/opds", auth=("reader", "guess"), headers={"X-Forwarded-For": "203.0.113.9"})
        assert client.get("/opds", auth=("reader", "correct horse"), headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
        ok = client.get("/opds", auth=("reader", "correct horse"), headers={"X-Forwarded-For": "198.51.100.20"})
        assert ok.status_code == 200  # ... and you, coming through the same tunnel from elsewhere, are not
        for i in range(3):  # a new made-up address at every try doesn't get the guesser out of the lockout
            fake = {"X-Forwarded-For": "203.0.113.9", "CF-Connecting-IP": f"192.0.2.{i}", "X-Real-IP": f"192.0.2.{i + 50}"}
            assert client.get("/opds", auth=("reader", "guess"), headers=fake).status_code == 429
    finally:
        srv.shutdown()
    lines = access.access_log_file().read_text(encoding="utf-8").splitlines()
    assert lines[-1].startswith("203.0.113.9 ") and not any(line.startswith("192.0.2.") for line in lines)


def test_a_reading_apps_login_ends_with_a_change_made_while_it_was_checked(monkeypatch):
    """The password takes a moment to check. A removal, or a new password, landing in that moment used to leave the
    app's login remembered afterwards, so it went on working; a removed borrower even came back as a reader."""
    import base64

    for change in ("remove", "new password"):
        access.add_user("rory", "first password", role="borrower")
        gate = access.Gate(log=lambda m: None)
        real = access.check_password

        def checked_while_changed(password, stored, real=real, change=change):
            ok = real(password, stored)
            if change == "remove":
                access.remove_user("rory")
            else:
                access.add_user("rory", "second password")
            gate.users()  # another request, meanwhile: the change is seen
            return ok

        login = {"Authorization": "Basic " + base64.b64encode(b"rory:first password").decode()}
        monkeypatch.setattr(access, "check_password", checked_while_changed)
        assert gate.check("203.0.113.5", login) == ("ok", "rory")  # the request in flight was let in
        monkeypatch.setattr(access, "check_password", real)
        assert gate.check("203.0.113.5", login)[0] == "login", change  # and nothing after it
        spelled_apart = {"Authorization": "basic  " + login["Authorization"][6:]}
        assert gate.check("203.0.113.6", spelled_apart)[0] == "login", change
        assert gate.role("rory") == ("borrower" if change != "remove" else access.ROLES[0])
        access.remove_user("rory")
    assert gate.role("nobody by that name") == access.ROLES[0] == "borrower"  # not a reader


# ---- the one-time sign-in link -----------------------------------------------------------------------------------

def test_a_one_time_link_logs_a_browser_in_once_and_is_never_written_down(library):
    """Opening the link shows a page that asks to use it, with one button: a chat app's preview of the link uses nothing
    up and logs nothing in, and another site's form can't use it. Used, it logs the browser in as its user and is gone.
    The access log keeps no token, used or not."""
    access.add_user("owen", "correct horse")  # the first user: the owner
    access.add_user("rory", "correct horse")
    srv, client = start(library, access_log=True)
    here = f"http://127.0.0.1:{srv.server_port}"
    try:
        token = access.sign_in_link("rory")
        page = client.get("/login", params={"once": token, "next": "/opds"}, headers=NAVIGATE)
        assert page.status_code == 200 and "Log in as rory" in page.text and f'name="once" value="{token}"' in page.text
        assert 'name="next" value="/opds"' in page.text and 'name="password"' not in page.text
        assert page.headers["referrer-policy"] == "origin"  # its address has the link in it: never sent on (and null no Origin)
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"] and page.headers["cache-control"] == "no-store"
        assert "set-cookie" not in page.headers and client.get("/login", params={"once": token}).status_code == 200  # nothing used up
        elsewhere = client.post("/login", data={"once": token, "next": "/opds"}, headers={"Origin": "https://elsewhere.example"})
        assert elsewhere.status_code == 403
        used = client.post("/login", data={"once": token, "next": "/opds"}, headers={"Origin": here})
        assert used.status_code == 303 and used.headers["location"] == "/opds" and used.headers["set-cookie"].startswith("hylly=")
        s = client.get("/api/session").json()
        assert s["by"] == "rory" and s["role"] == "reader" and s["session"]
        client.cookies.clear()
        again = client.post("/login", data={"once": token, "next": "/opds"})
        assert again.status_code == 410 and "This sign-in link has been used, or is too old" in again.text
        assert "set-cookie" not in again.headers and client.get("/api/session").status_code == 401
        assert "has been used, or is too old" in client.get("/login", params={"once": token}).text  # the page says so too
        assert client.post("/login", data={"once": "not a link", "next": "/"}).status_code == 410
    finally:
        srv.shutdown()
    log = access.access_log_file().read_text(encoding="utf-8")
    assert token not in log and "/login?once=-&next=%2Fopds" in log and " - rory [" in log
    from hylly import db

    on_disk = b"".join(p.read_bytes() for p in (db.file(), db.file().with_name("hylly.db-wal")) if p.exists())
    assert token.encode() not in on_disk  # only its digest was ever kept


def test_a_link_ends_after_ten_minutes_with_a_new_password_or_with_its_user_and_is_no_cookie(monkeypatch):
    clock, ip = [1_800_000_000.0], "203.0.113.9"
    now = lambda: clock[0]  # noqa: E731
    sessions = access.Sessions(now=now)
    gate = access.Gate(log=lambda m: None, sessions=sessions)
    access.add_user("owen", "correct horse")
    writes, real = [], type(access.store()).write
    monkeypatch.setattr(type(access.store()), "write", lambda self: writes.append(1) or real(self))
    for never in ("A" * 43, "not a link", ""):  # anyone may send these, with no login: only read, no write lock taken
        assert gate.use_link(ip, never) == ("gone", None)
    assert writes == []
    monkeypatch.setattr(type(access.store()), "write", real)
    token = access.sign_in_link("owen", now=now)
    clock[0] += access.LINK_MINUTES * 60 - 1
    assert sessions.link_user(token) == "owen"
    clock[0] += 2
    assert sessions.link_user(token) is None and gate.use_link(ip, token) == ("gone", None)  # too old
    token = access.sign_in_link("owen", now=now)
    access.add_user("owen", "a new password")
    assert gate.use_link(ip, token) == ("gone", None)  # made before the new password
    token = access.sign_in_link("owen", now=now)
    assert gate.check(ip, {"Cookie": f"hylly={token}"})[0] == "login"  # a link is no cookie ...
    cookie = gate.log_in(ip, "owen", "a new password")[1]
    assert sessions.link_user(cookie) is None and gate.use_link(ip, cookie) == ("gone", None)  # ... nor a cookie a link
    assert gate.check(ip, {"Cookie": f"hylly={cookie}"}) == ("ok", "owen")
    verdict, (user, session) = gate.use_link(ip, token)
    assert (verdict, user) == ("ok", "owen") and gate.check(ip, {"Cookie": f"hylly={session}"}) == ("ok", "owen")
    assert gate.use_link(ip, token) == ("gone", None)  # once
    token = access.sign_in_link("owen", now=now)
    access.remove_user("owen")
    assert gate.use_link(ip, token) == ("gone", None)
    with pytest.raises(SystemExit, match="There is no user nobody"):
        access.sign_in_link("nobody")
    access.add_user("owen", "correct horse")
    token = access.sign_in_link("owen", now=now)
    for _ in range(access.FAILS):
        gate.log_in(ip, "owen", "guess")
    assert gate.use_link(ip, token) == ("locked", None) and sessions.link_user(token) == "owen"  # an address locked out waits


def test_hylly_user_link_prints_a_link_at_each_address_the_library_has(monkeypatch, capsys):
    from hylly import cli

    access.add_user("owen", "correct horse")
    access.add_user("ann", "correct horse")
    for said, wanted in (('[server]\nlisten = "0.0.0.0"\naddress = "https://books.example.org/"\n',
                          ["https://books.example.org", "http://192.168.1.5:8770"]),  # and the plain one at home, for an e-reader
                         ('[server]\nlisten = "0.0.0.0"\n', ["http://192.168.1.5:8770"]),  # this computer's own at home
                         ('[server]\naddress = "https://books.example.org"\n', ["https://books.example.org"]),  # a proxy's
                         ("", []), ('[server]\nlisten = "::"\nport = 8790\n', ["http://192.168.1.5:8790"])):
        settings_say(said)
        cli.main(["user", "link", "ann", "--yes"])  # into what the test reads, a pipe: once its owner has said so
        out = capsys.readouterr().out
        assert out.startswith(f"A one-time link that logs a browser in as ann: it works once, within {access.LINK_MINUTES} minutes")
        links = re.findall(r"^  (\S+)$", out, re.M)
        here = "http://[::1]:8790" if "::" in said else "http://127.0.0.1:8770"
        assert [link.partition("/login?once=")[0] for link in links] == [*wanted, here], said
        token = links[0].partition("once=")[2]
        assert all(link.endswith(f"/login?once={token}") for link in links) and access.Sessions().link_user(token) == "ann"
    with pytest.raises(SystemExit):
        cli.main(["user", "link", "nobody"])


def test_a_one_time_link_goes_into_a_pipe_or_a_file_only_with_a_yes(monkeypatch, capsys):
    """A link logs a browser in as its user, the owner too, so hylly user link shows it at a terminal; into a pipe or a
    file, a log's or an assistant's, which would keep it, only with --yes: without, it ends with code 8, says why, and
    makes none. A name that isn't there is still a mistake first."""
    from hylly import cli, exits

    access.add_user("owen", "correct horse")
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "link", "owen"])  # standard output here is the test's own, no terminal
    said = capsys.readouterr()
    assert e.value.code == exits.NEEDS_YOU and "login?once=" not in said.out + said.err
    assert "only at a terminal" in said.err and "--yes" in said.err and "Nothing was made" in said.err
    with access.store().read() as c:
        assert c.execute("SELECT count(*) FROM browser_login").fetchone()[0] == 0  # no link was made
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "link", "nobody"])
    assert e.value.code == exits.USAGE and "hylly user list" in capsys.readouterr().err
    monkeypatch.setattr(serve, "in_a_terminal", lambda: True)  # a person at a terminal: no yes needed
    cli.main(["user", "link", "owen"])
    assert "login?once=" in capsys.readouterr().out


# ---- what the server says at its start, its status and the doctor -------------------------------------------------

def settings_say(text: str) -> None:
    """The settings say this, read afresh with the rules, as the next command would read them."""
    from hylly import rules, settings

    Path(os.environ["HYLLY_SETTINGS"]).write_text(text, encoding="utf-8")
    settings.reload()
    rules.reset()


def started(monkeypatch, capsys, background: bool = False, open_browser: bool = True, tty: bool = True, made: dict | None = None,
            **options) -> tuple[str, list[str]]:
    """What serve.run says and opens as it starts, with a server that only pretends to serve: at a terminal, or with
    what it prints going into a file (tty False), on a computer with a desktop. options: listen, as hylly serve gives it.
    made: gets where the server was made to listen ("host") and what else it was given. What it says: its lines on
    standard output, then those on standard error, its messages."""
    from hylly import periodic

    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
    opened: list[str] = []
    handler = SimpleNamespace(root=Path("."), watch=SimpleNamespace(start=lambda: None))
    fake = SimpleNamespace(RequestHandlerClass=handler, server_close=lambda: None,
                           serve_forever=lambda poll_interval: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(serve, "make_server", lambda *a, **k: ((made if made is not None else {}).update(host=a[1], port=a[2], **k),
                                                               (fake, [0.0]))[1])
    monkeypatch.setattr(serve.webbrowser, "open", opened.append)
    monkeypatch.setattr(serve, "a_desktop", lambda: True)
    monkeypatch.setattr(serve.signal, "signal", lambda *a: None)
    jobs = SimpleNamespace(ended=threading.Event())
    monkeypatch.setattr(periodic, "start", lambda *a, **k: jobs)  # no jobs: its library is made up
    serve.run(Path("."), port=8790, background=background, open_browser=open_browser, **options)
    assert jobs.ended.is_set()  # its periodic jobs' rounds end with it
    said = capsys.readouterr()
    return said.out + said.err, opened


def test_hylly_serve_in_a_terminal_shows_and_opens_a_one_time_link_for_the_one_owner(monkeypatch, capsys):
    said, opened = started(monkeypatch, capsys)  # nobody yet: the login page, which says what to do
    assert "Log in:" not in said and opened == ["http://127.0.0.1:8790/"]
    assert ("  Login:             always: no users yet, so nobody can log in\n"
            "                     hylly user add NAME, on the server computer: the first user is the owner\n") in said
    access.add_user("owen", "correct horse")
    said, opened = started(monkeypatch, capsys)
    link = re.search(r"  Log in:            (http://127\.0\.0\.1:8790/login\?once=\S+)\n", said)[1]
    assert f"(as the owner, once, within {access.LINK_MINUTES} minutes)" in said and opened == [link]
    assert access.Sessions().link_user(link.partition("once=")[2]) == "owen"
    said, opened = started(monkeypatch, capsys, open_browser=False)  # --no-browser: shown all the same
    assert "  Log in:            http://127.0.0.1:8790/login?once=" in said and opened == []
    said, opened = started(monkeypatch, capsys, background=True)  # in the background: never, its log keeps no link
    assert "once=" not in said
    assert "Library server started: port 8790, listening on 127.0.0.1 (this computer only), every request logs in" in said
    said, opened = started(monkeypatch, capsys, listen="::")  # on every address: the link, and the browser, at [::1]
    assert re.search(r"  Log in:            http://\[::1\]:8790/login\?once=\S+\n", said) and opened[0].startswith("http://[::1]:8790/")
    access.add_user("ann", "correct horse", role="owner")
    said, opened = started(monkeypatch, capsys)  # two owners: whose? hylly user link NAME says
    assert "once=" not in said and opened == ["http://127.0.0.1:8790/"]
    assert "  Log in:            hylly user link NAME, at a terminal, makes a one-time link that logs a browser in as NAME\n" in said


def links_kept() -> int:
    from hylly import db

    with db.database().read() as c:
        return c.execute("SELECT count(*) FROM browser_login").fetchone()[0]


def test_a_start_whose_lines_go_into_a_file_shows_no_link_and_makes_none(monkeypatch, capsys, tmp_path):
    """nohup, a service's or a container's log, a supervisor's, hylly serve > log: a file would keep a live owner's
    link. So only at a terminal is one made, shown or opened; otherwise the line says how to make one, at a terminal.
    The same for hylly serve when it finds a server running, or starts one in the background, and opens the browser."""
    from hylly import cli, instance

    access.add_user("owen", "correct horse")
    said, opened = started(monkeypatch, capsys, tty=False)
    assert "once=" not in said and opened == ["http://127.0.0.1:8790/"] and links_kept() == 0
    assert "  Log in:            hylly user link NAME, at a terminal, makes a one-time link that logs a browser in as NAME\n" in said
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr(instance, "running", lambda: {"pid": 1, "port": 8790, "listen": "127.0.0.1", "library": "x", "given": {}})
    monkeypatch.setattr(instance, "system_service", lambda: False)  # not a walled one, whatever computer this is
    for tty in (False, True):  # a server running already: the browser opened on it
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
        cli.main(["serve"])
        said = capsys.readouterr().out
        if tty:
            link = re.search(r"Logging you in as the owner, once, within 10 minutes: (\S+)\n", said)[1]
            assert opened[-1] == link and access.Sessions().link_user(link.partition("once=")[2]) == "owen"
        else:
            assert "once=" not in said and opened[-1] == "http://127.0.0.1:8790/" and links_kept() == 0
            assert "To log in: hylly user link NAME, at a terminal, makes a one-time link" in said


def test_the_status_and_the_start_say_who_can_log_in(monkeypatch, capsys):
    """No home networks, no "outside", no login setting: who can log in, and what to do when nobody can, or nobody is
    an owner. A server started at logon or by the system says that in its log."""
    running = {"pid": 1, "port": 8770, "lan": True, "library": "x", "login": "outside"}  # a server from before: login ignored
    text = serve.describe(running)
    assert ("  On this computer:  http://127.0.0.1:8770/\n"
            "  In a browser:      http://192.168.1.5:8770/  (the library page, on another device)\n"
            "  Reading apps (OPDS):\n"
            "    For tablets:     http://192.168.1.5:8770/opds  (your feed; in full, with http://)\n"
            "    For e-readers:   http://192.168.1.5:8770/ereader/opds  (only books that suit an e-ink reader)\n") in text
    assert "  Login:             always: no users yet, so nobody can log in\n" in text
    assert not any(word in text for word in ("Counts as home", "outside", "/me/opds", "home_networks", "Tunnel", "Public"))
    access.add_user("owen", "correct horse")
    access.add_user("ann", "correct horse")
    assert "  Login:             always: 2 users, 1 of them an owner\n  Library:" in serve.describe(running)
    assert "Logins:" not in started(monkeypatch, capsys, background=True)[0]
    access.set_role("owen", "reader")
    said = started(monkeypatch, capsys, background=True)[0]
    assert "Logins: 2 users, none of them an owner. Next: hylly user role NAME owner, for the library's management." in said


def test_the_doctor_says_who_can_reach_the_server_and_flags_no_users_or_no_owner(monkeypatch):
    from hylly import doctor, instance

    monkeypatch.setattr(instance, "running", lambda: None)
    nobody = doctor._access()
    assert nobody == [{"id": "access", "what": "Access", "state": "problem",
                       "says": "It listens on this computer only (127.0.0.1), with no address set. Every request logs in: no "
                               "users yet, so nobody can log in.",
                       "next": "hylly user add NAME, on the server computer: the first user is the owner", "owner": True,
                       "access": {"listens": "127.0.0.1", "address": None, "users": 0, "owners": 0}}]
    access.add_user("owen", "correct horse")
    settings_say('[server]\nlisten = "0.0.0.0"\naddress = "https://books.example.org/"\n')
    found = doctor._access()[0]
    assert found["state"] == "ok" and "next" not in found and "owner" not in found
    assert found["says"] == ("It listens on every IPv4 address of this computer's (0.0.0.0), and the address people use is "
                             "https://books.example.org. Every request logs in: 1 user, 1 of them an owner.")
    assert found["access"] == {"listens": "0.0.0.0", "address": "https://books.example.org", "users": 1, "owners": 1}
    settings_say('[server]\nlisten = "::"\n')
    assert doctor._access()[0]["says"].startswith("It listens on every address of this computer's (::), with no address set.")
    settings_say('[server]\nlisten = "192.0.2.10"\n')
    assert doctor._access()[0]["says"].startswith("It listens on one address, 192.0.2.10, with no address set.")
    access.set_role("owen", "reader")
    found = doctor._access()[0]
    assert found["state"] == "problem" and found["next"] == "hylly user role NAME owner, for the library's management"
    assert found["says"].endswith("Every request logs in: 1 user, none of them an owner.")
    monkeypatch.setattr(instance, "running", lambda: {"pid": 8, "lan": False})  # the running server's own, over the settings
    assert doctor._access()[0]["says"].startswith("It listens on this computer only (127.0.0.1),")
    report = doctor.report()
    assert [c for c in report["checks"] if c["what"] == "Access"][0]["access"]["owners"] == 0
    assert not [c for c in report["checks"] if c["what"] in ("Home network", "Public address", "Address") and "login" in c["says"]]
    json.dumps(report)  # for --json, as it is
    from hylly import db

    monkeypatch.setattr(access, "peek_accounts", lambda: (_ for _ in ()).throw(db.Unusable("made-up trouble with the file")))
    found = doctor._access()[0]  # a database that can't be used: said as such, not as a bare error
    assert found["state"] == "problem" and found["says"].endswith(
        "Every request logs in: nobody can, as the users can't be read from the database (made-up trouble with the file).")
    assert found["next"].startswith("Look at the Database line") and found["access"]["users"] is None


# ---- the library's addresses: this computer's own on the home network --------------------------------------------

def test_the_address_for_other_devices_is_a_real_adapters_never_a_vpns(monkeypatch):
    """This computer's address on the home network is a private IPv4 address of an adapter that is up and isn't a
    VPN's or a virtual network's; of several, the one the default route goes by; else the first. Only with no adapter
    to list, the route alone (towards an address no home network has); without one, 127.0.0.1."""
    import socket
    from types import SimpleNamespace

    import psutil

    def adapters(found: dict, down=()):
        monkeypatch.setattr(psutil, "net_if_addrs", lambda: {name: [SimpleNamespace(family=socket.AF_INET, address=a)]
                                                             for name, a in found.items()})
        monkeypatch.setattr(psutil, "net_if_stats", lambda: {name: SimpleNamespace(isup=name not in down) for name in found})

    real_probe = access._route_probe
    home = {"Ethernet 9": "10.99.0.1", "Ethernet 7": "198.51.100.56", "Lantern Link": "169.254.10.20", "WLAN 3": "192.0.2.20",
            "vEthernet (Lantern Switch)": "172.31.200.1", "docker-lantern": "172.29.9.1", "tailscale-lantern": "100.70.80.90",
            "wg7": "10.66.0.2", "Loopback Lantern": "127.0.0.1"}  # all made up; 198.51.100.x and 192.0.2.x count as private
    adapters(home, down=("Ethernet 9",))
    monkeypatch.setattr(access, "_route_probe", lambda: "192.0.2.20")
    assert access.adapter_address() == "192.0.2.20"  # a virtual machine's host-only adapter, plainly named, is listed first
    monkeypatch.setattr(access, "_route_probe", lambda: "10.66.0.2")  # a VPN has the default route: never its address
    assert access.adapter_address() == "198.51.100.56"  # the first one left, then: by its name alone, it can't be told
    monkeypatch.setattr(access, "_route_probe", lambda: None)  # no default route at all
    assert access.adapter_address() == "198.51.100.56"
    adapters({"eth-lantern": "100.64.0.20", "tun7": "10.66.0.2"})  # a server with a shared (not private) address only
    monkeypatch.setattr(access, "_route_probe", lambda: "100.64.0.20")
    assert access.adapter_address() == "100.64.0.20"  # no private one listed: the route's own
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: (_ for _ in ()).throw(OSError("no list")))
    assert access.adapter_address() == "100.64.0.20"
    monkeypatch.setattr(access, "_route_probe", lambda: None)
    assert access.adapter_address() == "127.0.0.1"
    aimed = []

    class Probe:  # a socket that only says where it was aimed
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def connect(self, where):
            aimed.append(where)

        def getsockname(self):
            return ("192.0.2.20", 50000)

    monkeypatch.setattr(access, "_route_probe", real_probe)
    monkeypatch.setattr(socket, "socket", Probe)
    assert access._route_probe() == "192.0.2.20" and aimed == [("192.0.2.1", 9)]  # an address no home network has


def test_the_adapters_are_looked_at_again_apart_from_any_request(monkeypatch, tmp_path):
    """The address for other devices is kept, and looked at again every five minutes by the server's job "home
    address", in a thread of its own: while it looks, a request gets the one from before at once, and the job isn't
    started twice."""
    import time as clock

    from hylly import bell, periodic

    asked, gate = [], threading.Event()

    def slow() -> str:
        asked.append(1)
        gate.wait(5)
        return f"192.0.2.{20 + len(asked)}"

    monkeypatch.setattr(access, "_adapter", {"address": None})
    monkeypatch.setattr(access, "adapter_address", lambda: (asked.append(1), "192.0.2.20")[1])
    assert access.lan_address() == "192.0.2.20" and access.lan_address() == "192.0.2.20" and asked == [1]  # kept
    monkeypatch.setattr(access, "adapter_address", slow)
    home = tuple(job for job in periodic.JOBS if job.name == "home address")
    assert home[0].server_only and home[0].every == 300  # the server's own, whatever [server] jobs says
    runner = periodic.Runner(periodic.Context(tmp_path, print, bell.Watch(log=print)), server=True, jobs=home)
    t0 = clock.monotonic()
    assert runner.run_due(now=1000.0) == ["home address"]
    assert access.lan_address() == "192.0.2.20" and clock.monotonic() - t0 < 1  # the one from before, without waiting
    assert runner.run_due(now=1300.0) == [] and runner.still_running() == ["home address"]  # one look at a time
    gate.set()
    for _ in range(100):
        if not runner.still_running():
            break
        clock.sleep(0.05)
    assert access.lan_address() == "192.0.2.22" and len(asked) == 2


# ---- the settings that are no longer used ------------------------------------------------------------------------

def test_the_old_settings_are_named_once_as_no_longer_used_and_a_server_starts_with_them(monkeypatch, capsys, watching):
    """login, home_networks and [bell] open_door: Hylly always asks for a login now. lan, public_address and tunnel_port:
    where it listens and the address people use are said (listen, address), and there is one port. Written in as the
    live server's settings have them (lan, public_address, no tunnel_port), each is told once, by name, with what to write
    instead, and left out; nothing else stops on them, and the server starts on this computer only, until listen says
    otherwise."""
    from hylly import bell, settings

    settings_say('[server]\nlogin = "outside"\nlan = true\nidle_minutes = 30\npublic_address = "https://books.example.org/"\n'
                 'home_networks = ["192.168.1.0/24"]\n[bell]\nopen_door = true\n')
    settings.reload()
    assert serve.options() == (8770, "127.0.0.1") and access.settings() is True and "open_door" not in bell.settings()
    for section in settings.DEFAULTS:
        settings.get(section)
    told = capsys.readouterr().err.splitlines()
    file = str(Path(os.environ["HYLLY_SETTINGS"]))
    assert told == [f"{file}: [server] login is no longer used, so it is left out. Hylly always asks for a login now: hylly user add NAME.",
                    f'{file}: [server] lan is no longer used, so it is left out. Write listen = "0.0.0.0" instead, to listen on the home network.',
                    f"{file}: [server] idle_minutes is no longer used, so it is left out. The server no longer stops by itself: hylly server stop stops it.",
                    f'{file}: [server] public_address is no longer used, so it is left out. It is renamed: address = "..." with the same value.',
                    f"{file}: [server] home_networks is no longer used, so it is left out. Hylly always asks for a login now: hylly user add NAME.",
                    f"{file}: [bell] open_door is no longer used, so it is left out. Hylly always asks for a login now: hylly user add NAME."]
    with open(file, "a", encoding="utf-8") as fh:  # read again, as a running server does when the file changes: told once
        fh.write("quiet_minutes = 30\n")
    assert settings.get("bell")["toast"] and capsys.readouterr().err == ""
    listed = "\n".join(settings.listing())
    for key in ("login", "lan", "idle_minutes", "public_address", "open_door"):
        assert re.search(rf"^    {key} = \(not shown\) +# no longer used: left out$", listed, re.M), key
    assert serve.Hosts.from_settings().extra == set()  # public_address's name is no name of the server's now
    port, listen = serve.options()
    made: dict = {}
    said = started(monkeypatch, capsys, background=True, made=made, listen=listen)[0]
    assert ("Library server started: port 8790, listening on 127.0.0.1 (this computer only), every request logs in"
            ) in said and made["host"] == "127.0.0.1" and made["address"] == "" and "idle_minutes" not in made
    settings.reload()
    settings_say("[server]\ntunnel_port = 8771\n")
    assert serve.options()[1] == "127.0.0.1" and capsys.readouterr().err == (
        f"{file}: [server] tunnel_port is no longer used, so it is left out. There is one port now: point the tunnel or "
        "proxy at the server's port (port under [server]).\n")


def test_a_comma_or_semicolon_inside_a_quoted_forwarded_value_is_part_of_the_value():
    """A proxy that writes the visitor's own text quoted (host="...") after its for= can't be made to name another
    sender by a comma or a semicolon inside it."""
    for header in ('for=203.0.113.44;host="a,for=198.51.100.99"', 'for=203.0.113.44;host="a;for=198.51.100.99"',
                   'for=203.0.113.44;host="a\\",for=198.51.100.99"'):
        assert access.client_address("127.0.0.1", {"Forwarded": header}, "Forwarded") == "203.0.113.44", header
