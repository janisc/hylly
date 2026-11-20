"""User roles: a reader only reads, a librarian changes one title at a time, the owner anything. The server decides."""

from __future__ import annotations

import json
import re
import threading

import httpx
import pytest

from hylly import access, catalog, cli, serve

from helpers import HOME, build_library, change, fix

ONE, TWO = ["h:novel"], ["h:novel", "s:7"]


@pytest.fixture
def users():
    return {"rory": "reader", "lucy": "librarian", "owen": "owner"}  # the library fixture's, served by server's


def client_for(srv, name):
    return httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", auth=(name, "correct horse"), headers=HOME)


def test_the_first_user_is_the_owner_and_a_new_one_after_gets_no_rights_and_keeps_the_role_given():
    assert access.add_user("first", "correct horse") and access.load_accounts()["first"][0] == "owner"  # nobody else there
    assert access.add_user("new", "correct horse") and access.load_accounts()["new"][0] == "reader"  # gets no rights by itself
    access.remove_user("first")
    assert access.add_user("third", "correct horse") and access.load_accounts()["third"][0] == "reader"  # only the very first
    access.remove_user("third")
    stored = access.load_accounts()["new"][1]
    assert access.set_role("new", "owner") and access.load_accounts()["new"] == ("owner", stored)
    access.add_user("new", "a new password")  # a new password keeps the role
    assert access.load_accounts()["new"][0] == "owner" and access.check_password("a new password", access.load_accounts()["new"][1])
    assert not access.set_role("nobody", "owner")
    with pytest.raises(SystemExit):
        access.set_role("new", "admin")
    with pytest.raises(SystemExit):
        access.add_user("x", "correct horse", role="god")
    assert all(role in access.ROLES and stored.startswith("scrypt$") for role, stored in access.load_accounts().values())


def test_the_user_commands(capsys, monkeypatch):
    cli.main(["user", "list"])
    assert "No users yet" in capsys.readouterr().out
    typed = iter(["correct horse"] * 4)
    monkeypatch.setattr(cli, "_hidden", lambda prompt, command: next(typed))
    cli.main(["user", "add", "owen"])
    assert capsys.readouterr().out == "Added owen as owner: the first user is the library's owner.\n"  # and says so
    cli.main(["user", "add", "lucy"])
    assert capsys.readouterr().out == "Added lucy as reader.\n"
    cli.main(["user", "role", "lucy", "librarian", "--yes"])
    cli.main(["user", "list"])
    out = capsys.readouterr().out
    assert "lucy is now librarian" in out and re.search(r"^lucy\s+librarian$", out, re.M) and "scrypt" not in out
    assert "No user is an owner" not in out
    with pytest.raises(SystemExit):
        cli.main(["user", "role", "lucy", "boss"])
    cli.main(["user", "role", "owen", "reader", "--yes"])  # no owner left: the list says what that means, and what to do
    cli.main(["user", "list"])
    assert "No user is an owner, so nobody has the library's management: hylly user role NAME owner" in capsys.readouterr().out
    cli.main(["user", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == {"users": [{"name": "lucy", "role": "librarian"}, {"name": "owen", "role": "reader"}]}


def test_a_new_role_is_asked_about_first_as_it_changes_what_the_user_may_do(capsys, monkeypatch):
    """hylly user role, an owner's above all, asks first, as user remove does: with nobody at the keyboard, a script's
    or an assistant's, it ends with code 8 and changes nothing, unless --yes says the library's owner has said yes."""
    from hylly import exits

    access.add_user("owen", "correct horse", role="owner")
    access.add_user("rory", "correct horse", role="reader")
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "role", "rory", "owner"])
    said = capsys.readouterr().err
    assert e.value.code == exits.NEEDS_YOU and "hylly user role rory owner --yes" in said and "Nothing changed" in said
    assert access.load_accounts()["rory"][0] == "reader"
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(capsys.readouterr().err) or "n")
    cli.main(["user", "role", "rory", "owner"])
    assert "Make rory owner, from reader? An owner may do anything" in asked[0] and access.load_accounts()["rory"][0] == "reader"
    assert "Nothing changed" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cli.main(["user", "role", "rory", "owner"])
    assert access.load_accounts()["rory"][0] == "owner" and "rory is now owner" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("nothing to ask: the role is that already"))
    cli.main(["user", "role", "rory", "owner"])
    assert "rory is owner already" in capsys.readouterr().out
    monkeypatch.setattr(cli, "at_keyboard", lambda: False)
    cli.main(["user", "role", "rory", "reader", "--yes"])  # once the owner has said yes
    assert access.load_accounts()["rory"][0] == "reader"


def test_user_add_says_first_whether_it_makes_a_user_or_a_new_password(capsys, monkeypatch):
    """Before the first prompt: the name checked, and whether it's a new user (with the role it gets) or a new password for
    one there, which ends their logins. With nobody at the keyboard, the command it says to run is one that works."""
    asked = []

    def typed(prompt, command):
        asked.append((capsys.readouterr().err, command))  # what was said before this prompt
        return "correct horse"

    monkeypatch.setattr(cli, "_hidden", typed)
    cli.main(["user", "add", "owen"])
    assert "New user owen (owner)" in asked[0][0] and "first user" in asked[0][0]
    cli.main(["user", "add", "lucy", "--role", "librarian"])
    assert "New user lucy (librarian)." in asked[2][0] and asked[2][1] == "hylly user add lucy --role librarian"
    cli.main(["user", "add", "lucy"])
    assert "lucy exists (librarian): this sets a new password for them, and ends their logins elsewhere." in asked[4][0]
    capsys.readouterr()
    asked.clear()
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "add", "bad name"])
    assert e.value.code == 2 and "no spaces" in capsys.readouterr().err and asked == []  # refused before any prompt


def test_what_each_may_do(server):
    with client_for(server, "rory") as reader, client_for(server, "lucy") as librarian, client_for(server, "owen") as owner:
        s = reader.get("/api/session").json()
        assert s["role"] == "reader" and not s["edit"] and s["key"] is None and s["can"] == {"one": False, "many": False, "manage": False}
        assert change(reader, ONE).status_code == 403 and fix(reader, ONE).status_code == 403
        assert reader.get("/api/edits").status_code == 403 and reader.get("/api/suggestions").status_code == 403

        s = librarian.get("/api/session").json()
        assert s["role"] == "librarian" and s["can"] == {"one": True, "many": False, "manage": False}
        assert change(librarian, ONE).json()["changed"] == ONE and fix(librarian, ONE).json()["ok"]
        refused = change(librarian, TWO)
        assert refused.status_code == 403 and "owner" in refused.json()["error"]
        assert fix(librarian, TWO).status_code == 403 and change(librarian, ["h:novel", "h:novel"]).status_code == 200  # one title twice is one
        assert librarian.get("/api/suggestions").status_code == 403 and librarian.get("/api/declined").status_code == 403
        key = librarian.get("/api/session").json()["key"]
        assert librarian.post("/api/suggest", headers={"X-Server-Key": key}).status_code == 403

        s = owner.get("/api/session").json()
        assert s["role"] == "owner" and s["can"] == {"one": True, "many": True, "manage": True}
        assert sorted(change(owner, TWO, tag="Shelf").json()["changed"]) == sorted(TWO) and fix(owner, TWO).json()["ok"]
        assert owner.get("/api/suggestions").status_code == 200
        everyone = {e["by"] for e in owner.get("/api/edits").json()["edits"]}
        assert {"lucy", "owen"} <= everyone  # the owner sees everyone's changes ...
        assert {e["by"] for e in librarian.get("/api/edits").json()["edits"]} == {"lucy"}  # ... a librarian their own


def test_this_computer_is_no_owner_without_a_login(tmp_path):
    """Nothing is given by where a request comes from: on the server's own computer, by localhost, a request without a
    login gets nothing, and an owner's login has the owner's role, the same as anywhere."""
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    access.add_user("owen", "correct horse", role="owner")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", headers={"Host": "localhost"}) as pc:
            assert pc.get("/api/session").status_code == 401 and pc.get("/server").status_code == 401
            assert pc.post("/api/tags", json={"op": "add", "tag": "Read", "ids": TWO}).status_code == 401
            s = pc.get("/api/session", auth=("owen", "correct horse")).json()
            assert s["role"] == "owner" and s["by"] == "owen" and s["can"]["many"]
    finally:
        srv.shutdown()


def test_the_owner_at_another_computer_has_it_all_once_logged_in(tmp_path):
    """The server may be a computer nobody sits at. Without a login nothing is let in; logged in, at home or through a
    tunnel (on the one port, over HTTPS), you have all an owner has, the server page too."""
    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    access.add_user("owen", "correct horse", role="owner")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    form = {"username": "owen", "password": "correct horse", "next": "/"}
    through = {"Host": "testpc.tail1234.ts.net", "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}

    def check(browser, tag):
        s = browser.get("/api/session").json()
        assert s["session"] and s["role"] == "owner" and s["can"] == {"one": True, "many": True, "manage": True}
        assert change(browser, TWO, tag=tag).json()["changed"] and fix(browser, TWO).json()["ok"]
        assert browser.get("/api/suggestions").status_code == 200 and browser.get("/api/edits").json()["edits"][0]["by"] == "owen"
        assert browser.get("/server").status_code == 200 and browser.get("/server/status").json()["users"] == ["owen (owner)"]
        return browser.get("/catalog/help.html").text

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", headers=HOME) as home:
            assert home.get("/api/session").status_code == 401  # at home with no login: nothing
            assert home.post("/login", data=form).status_code == 303
            assert "{{" not in check(home, "Shelf")  # the help for the owner, its folders filled in
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", headers=through) as outside:
            answer = outside.post("/login", data=form)
            assert answer.status_code == 303 and "Secure" in answer.headers["set-cookie"]
            cookie = answer.headers["set-cookie"].split(";")[0]  # sent by hand: a Secure one isn't over plain http
            outside.cookies.clear()
            outside.headers["Cookie"] = cookie
            check(outside, "Away")
    finally:
        srv.shutdown()


def test_a_lower_role_counts_at_once(server):
    with client_for(server, "owen") as owner:
        assert change(owner, TWO).status_code == 200
        access.set_role("owen", "librarian")
        assert change(owner, TWO).status_code == 403 and change(owner, ONE).status_code == 200
        access.set_role("owen", "reader")
        assert change(owner, ONE).status_code == 403 and not owner.get("/api/session").json()["edit"]


def test_a_session_has_the_role_too(server):
    """The role isn't kept in the session: lowering it ends what a logged-in browser may do at its next click."""
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers=HOME) as browser:
        assert browser.post("/login", data={"username": "lucy", "password": "correct horse", "next": "/"}).status_code == 303
        s = browser.get("/api/session").json()
        assert s["session"] and s["role"] == "librarian" and s["by"] == "lucy"
        assert change(browser, ONE).status_code == 200 and change(browser, TWO).status_code == 403
        access.set_role("lucy", "reader")
        s = browser.get("/api/session").json()
        assert s["session"] and s["role"] == "reader" and change(browser, ONE).status_code == 403


def test_the_server_page_lists_roles(server):
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", auth=("owen", "correct horse")) as pc:
        users = pc.get("/server/status").json()["users"]
    assert "lucy (librarian)" in users and "owen (owner)" in users and "rory (reader)" in users
