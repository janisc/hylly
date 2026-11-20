"""The Database page (dbpage.py): the owner's look at the tables, with an owner's login from anywhere, and only ever reading."""

from __future__ import annotations

import re
import threading

import httpx
import pytest

from hylly import access, db, dbpage, people, serve

from helpers import build_library


@pytest.fixture
def users():
    return {"rory": "owner"}  # the library fixture's


@pytest.fixture
def served(library, serving):
    people.Person("rory").place({"id": "h:novel", "t": "The Novel", "f": "humble/b/novel.epub"}, 12, 0.25)
    srv, client = serving(library, name="rory")
    return client, {"X-Server-Key": srv.RequestHandlerClass.server_key}


def test_an_owner_sees_both_pages_from_anywhere_and_a_reader_neither(tmp_path):
    """The Server and Database pages are an owner's, wherever they log in from: at this computer, at another device at
    home, through a tunnel. A reader or a borrower sees neither, here either: this computer makes nobody the owner, and
    anyone else is told they're the owner's (403). Their posts come with the page's key, and from this site's own
    pages."""
    build_library(tmp_path)
    access.add_user("rory", "correct horse", role="owner")
    access.add_user("bob", "battery staple", role="reader")
    access.add_user("bea", "battery staple", role="borrower")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    home, away = "192.168.1.5:8770", {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "http"}  # another device; a proxy's visitor
    try:
        for where, headers in (("here", {}), ("home", {"Host": home}), ("away", {"Host": home, **away})):
            site = f"http://{headers.get('Host', base.removeprefix('http://'))}"
            for name, password, seen in (("bob", "battery staple", 403), ("bea", "battery staple", 403), ("rory", "correct horse", 200)):
                with httpx.Client(base_url=base, headers=headers) as client:
                    client.post("/login", data={"username": name, "password": password, "next": "/"}, headers={"Origin": site})
                    assert client.get("/database").status_code == seen, (where, name)
                    assert client.get("/server").status_code == seen, (where, name)
                    key = {"X-Server-Key": srv.RequestHandlerClass.server_key, "Origin": site}
                    assert client.post("/database/query", json={"sql": "SELECT 1"}, headers=key).status_code == seen, (where, name)
                    elsewhere = {**key, "Origin": "https://elsewhere.example"}
                    assert client.post("/database/query", json={"sql": "SELECT 1"}, headers=elsewhere).status_code == 403
    finally:
        srv.shutdown()


def asking(client, key, sql):
    return client.post("/database/query", json={"sql": sql}, headers=key)


def test_the_page_and_its_tables_are_for_an_owner_with_the_pages_key(served):
    client, key = served
    page = client.get("/database")
    assert page.status_code == 200 and "Library database" in page.text and key["X-Server-Key"] in page.text
    assert "if (r.status === 401) {" in page.text and "to this computer only" not in page.text  # a login ended: to the login page
    assert "Content-Security-Policy" in page.headers
    for elsewhere in ({"Host": "192.168.1.5:8770"}, {"X-Forwarded-For": "203.0.113.9"}):  # at home; through a tunnel
        assert client.get("/database", headers=elsewhere).status_code == 200
        assert client.get("/database/tables", headers={**key, **elsewhere}).status_code == 200
    assert client.get("/database/tables").status_code == 403  # without the page's key: no other site can ask
    assert client.get("/database/tables", headers={**key, "Origin": "https://elsewhere.example"}).status_code == 403
    found = client.get("/database/tables", headers=key).json()
    rows = {t["name"]: t["rows"] for t in found["tables"]}
    assert found["schema"] == len(db.migrations()) and rows["place"] == 1 and rows["account"] == 1 and rows["title"] > 0


def test_a_table_comes_a_page_at_a_time_with_its_secrets_out_of_sight(served, monkeypatch):
    client, key = served
    one = client.get("/database/table", params={"name": "account"}, headers=key).json()
    assert one["columns"][:3] == ["name", "role", "hash"] and one["rows"][0][:3] == ["rory", "owner", None] and one["hidden"] == ["hash"]
    assert "CREATE TABLE account" in one["sql"]
    monkeypatch.setattr(dbpage, "PAGE", 2)
    first = client.get("/database/table", params={"name": "title"}, headers=key).json()
    second = client.get("/database/table", params={"name": "title", "page": "1"}, headers=key).json()
    assert len(first["rows"]) == 2 and first["more"] and first["rows"] != second["rows"]
    for name in ("no_such", "account; DROP TABLE account", "sqlite_schema"):
        assert client.get("/database/table", params={"name": name}, headers=key).status_code == 400


def test_the_page_counts_its_rows_and_says_its_limit_as_the_server_has_them(served, monkeypatch):
    """The page worked out "Rows 51 to 100" with a page of 50 of its own, and said "at most 500 rows" in words of its
    own, beside dbpage.PAGE and dbpage.ROWS: a change to either would have left the page saying the old, its rows
    numbered wrong. The tables come with the limits now (dbpage.limits), a table's page with its size, and the page
    keeps no number of its own."""
    client, key = served
    monkeypatch.setattr(dbpage, "PAGE", 2)
    monkeypatch.setattr(dbpage, "ROWS", 7)
    assert client.get("/database/tables", headers=key).json()["limits"] == dbpage.limits() == {"rows": 7, "page": 2}
    second = client.get("/database/table", params={"name": "title", "page": "1"}, headers=key).json()
    assert (second["page"], second["size"], len(second["rows"])) == (1, 2, 2)
    page = client.get("/database").text
    assert "const first = page * data.size + 1;" in page and "data.limits.rows" in page
    assert not re.search(r"\b(50|500)\b", page)


@pytest.mark.parametrize("sql", [
    "DELETE FROM account", "UPDATE account SET role = 'owner'", "INSERT INTO account VALUES ('x', 'owner', 'h', '')",
    "DROP TABLE place", "CREATE TABLE mine (a)", "ATTACH DATABASE 'elsewhere.db' AS other", "PRAGMA writable_schema = 1",
    "PRAGMA query_only = OFF", "SELECT 1; DELETE FROM account", "VACUUM INTO 'copy.db'", "BEGIN IMMEDIATE",
    "WITH x AS (SELECT 1) DELETE FROM place", "SELECT load_extension('evil')",
])
def test_nothing_that_writes_gets_through(served, sql):
    client, key = served
    before = (db.file().read_bytes(), access.load_accounts())
    answer = asking(client, key, sql)
    assert answer.status_code == 400 and not answer.json()["ok"] and answer.json()["error"]
    assert access.load_accounts() == before[1] and people.Person("rory").latest()["h:novel"]["at"] == 12


def test_questions_are_answered_and_secrets_stay_out_of_sight(served):
    client, key = served
    answer = asking(client, key, "SELECT kind, count(*) AS titles FROM title GROUP BY kind ORDER BY titles DESC").json()
    assert answer["ok"] and answer["columns"] == ["kind", "titles"] and answer["rows"] and not answer["more"]
    assert asking(client, key, "SELECT name, hash FROM account").json()["rows"] == [["rory", None]]
    assert asking(client, key, "SELECT count(*) FROM account WHERE hash IS NOT NULL").json()["rows"] == [[0]]  # not even its shape
    access.Gate(log=lambda m: None).log_in("203.0.113.9", "rory", "correct horse")
    assert asking(client, key, "SELECT name, digest, stamp FROM browser_login").json()["rows"] == [["rory", None, None]] * 2  # and the page's own
    for what, sql in dbpage.EXAMPLES:
        assert asking(client, key, sql).json()["ok"], what
    examples = client.get("/database/examples", headers=key).json()["examples"]
    assert [e[0] for e in examples] == [e[0] for e in dbpage.EXAMPLES]


def test_a_question_too_big_or_too_long_is_cut_short(served, monkeypatch):
    client, key = served
    many = asking(client, key, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 600) SELECT i FROM n").json()
    assert len(many["rows"]) == dbpage.ROWS and many["more"]
    monkeypatch.setattr(dbpage, "SECONDS", 0.2)
    forever = asking(client, key, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n")
    assert forever.status_code == 400 and "seconds" in forever.json()["error"]
    long_text = asking(client, key, "SELECT printf('%.5000c', 'x')").json()["rows"][0][0]
    assert len(long_text) < 2100 and "more characters" in long_text
    assert asking(client, key, "").status_code == 400 and asking(client, key, "SELECT 1" + " " * dbpage.SQL_MOST).status_code == 400


def test_two_statements_at_once_are_refused_in_words_on_every_python(served):
    """Python 3.11's sqlite3 says so with a Warning, 3.12's with a ProgrammingError: either way the page's refusal (400),
    not a failure of the server; and so is anything else sqlite3 says of a question, whatever error it is."""
    import sqlite3

    client, key = served
    answer = asking(client, key, "SELECT 1; SELECT 2")
    assert answer.status_code == 400 and "one statement at a time" in answer.json()["error"]

    class Asked:  # a connection as an older Python's sqlite3 answers a question
        def __init__(self, error):
            self.error = error

        def set_progress_handler(self, *args):
            pass

        def execute(self, sql, args=()):
            raise self.error

    for error in (sqlite3.Warning("You can only execute one statement at a time."), sqlite3.InterfaceError("Error binding parameter 1")):
        with pytest.raises(dbpage.Refused, match=str(error)):
            dbpage.question(Asked(error), "SELECT 1; SELECT 2")


def test_a_page_left_open_across_a_restart_says_to_reload_it(served):
    """The page's key is made anew at each server start, so a page open across one had its questions refused with
    a bare "The server said 403.". It now says what to do, and, as a proxy that doesn't hand on the browser's name
    gets a 403 at every ask, where the help says what it must; what else goes wrong is said as before."""
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    client, key = served
    assert asking(client, {"X-Server-Key": "from an earlier start"}, "SELECT 1").status_code == 403
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's script runs only in a browser here")
    code = re.findall(r"<script>(.*?)</script>", client.get("/database").text, re.S)[-1]
    code = code[:code.index("$(\"earlier\").onclick")]  # its functions, not what it does as it opens
    said = []
    for status, body in ((403, "not allowed"), (400, '{"ok": false, "error": "Only reading."}'), (500, "")):
        script = (f"const document = {{getElementById: () => ({{}})}}, location = {{assign() {{}}, pathname: '/database'}};\n"
                  f"const fetch = async () => ({{status: {status}, ok: false, json: async () => JSON.parse({json.dumps(body)})}});\n"
                  f"{code}\nask('/database/query', {{sql: 'SELECT 1'}}).catch(e => process.stdout.write(e.message));\n")
        said.append(subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert said[1:] == ["Only reading.", "The server said 500."]
    assert said[0].startswith("The server restarted, or refused this page's request: reload this page. If it keeps saying so, ")
    assert "Open to the internet" in said[0]  # where the help says what a proxy must hand on
    help_text = (Path(serve.__file__).parent / "help.html").read_text(encoding="utf-8")
    assert '<h3 id="outside">Open to the internet</h3>' in help_text and "isn't one of the server's, and the login is refused (403)" in help_text
