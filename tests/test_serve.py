"""hylly serve: OPDS feeds, file downloads with ranges, and what must stay private; and a book's own fonts, which the
page's policy lets in or keeps out."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest

from hylly import catalog, opds, paths, serve, settings
from hylly.words import size_words
from hylly.library import SHOP_TRAITS, Catalog, Device, cover_sites, ereader_feed, good_cover, kinds_table, page_item, shown_cover

from helpers import OPF, as_hylly_left_it_before, build_library, make_epub, near_miss, two_moonwakes
from helpers import page_data as read_data  # data.js as the catalog wrote it; page_data, below, is the server's


ATOM = "{http://www.w3.org/2005/Atom}"


@pytest.fixture
def server(library, serving):
    _, client = serving(library)
    return client, library


def entries(xml_text: str) -> list:
    return ET.fromstring(xml_text).findall(f"{ATOM}entry")


def write_data(root, data: dict) -> None:
    """data.js as the catalog writes it, a little newer than before, so a server reads it again."""
    path = root / "catalog" / "data.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.stat().st_mtime if path.exists() else 0
    path.write_text("window.CATALOG = " + json.dumps(data) + ";\n", encoding="utf-8")
    os.utime(path, (before + 5, before + 5) if before else None)


def mistake_in_the_rules():
    from hylly import paths

    folder = paths.settings_file().parent / "rules"
    folder.mkdir(exist_ok=True)
    (folder / "mine.toml").write_text('[genres."Lanterns"]\nwords = "lantern"\n', encoding="utf-8")
    return folder / "mine.toml"


def test_a_server_doesnt_start_with_a_mistake_in_the_rules_or_the_settings(tmp_path, monkeypatch):
    from hylly import paths, rules, settings

    monkeypatch.setattr(serve, "make_server", lambda *a, **k: pytest.fail("it would serve"))
    monkeypatch.setattr(rules, "WATCH", False)
    monkeypatch.setattr(settings, "WATCH", False)
    mistake_in_the_rules()
    with pytest.raises(SystemExit, match=r"mine\.toml, \[genres\.Lanterns\] words: a list"):
        serve.run(tmp_path, port=0, open_browser=False)
    assert rules.WATCH and settings.WATCH  # once running, it looks at the files again
    mistake_in_the_rules().unlink()
    for text, said in (('[download]\ntablet = "cbz"\n', r"\[download\] tablet = 'cbz': a list of formats in quotes"),
                       ('[server]\naccess_log = "no"\n', r"\[server\] access_log = 'no': true or false")):
        settings.reload()  # a start is a process of its own, which has read nothing yet: the first one read [server]
        paths.settings_file().write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit, match=said):
            serve.run(tmp_path, port=0, open_browser=False)


def test_a_server_starts_before_its_rules_folder_is_there(tmp_path, monkeypatch, capsys):
    """At logon a share may come up after the server: it starts, and reads the rules once they are there."""
    from hylly import paths, rules, settings

    class Serving(Exception):
        pass

    monkeypatch.setattr(serve, "make_server", lambda *a, **k: (_ for _ in ()).throw(Serving()))
    monkeypatch.setattr(rules, "WATCH", False)
    monkeypatch.setattr(settings, "WATCH", False)
    share = tmp_path / "share" / "curation" / "rules"
    paths.settings_file().write_text(f"rules = '{share}'\n", encoding="utf-8")
    with pytest.raises(Serving):
        serve.run(tmp_path, port=0, open_browser=False)
    assert "or HYLLY_RULES): there is no such file or folder. The server starts all the same" in capsys.readouterr().err
    with pytest.raises(rules.Missing):
        rules.current()  # until then, a request that needs them gets the error page
    share.mkdir(parents=True)
    (share / "mine.toml").write_text('[genres."Lanterns"]\nwords = ["lantern*"]\n', encoding="utf-8")
    assert rules.current().genres_of("The Lantern Keeper") == ["Lanterns"]


def test_a_server_with_no_library_or_no_catalog_says_so_at_its_start_and_in_its_status(tmp_path, monkeypatch, capsys):
    """It starts all the same, as a share may come up later, but says what's missing and the command to run: at a start
    in front of you, in server.log for a hidden one, after a start in the background, and in hylly server status."""
    from types import SimpleNamespace

    from hylly import cli, instance, paths, periodic

    class Serving(Exception):
        pass

    library = tmp_path / "library"
    handler = SimpleNamespace(root=library, watch=SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(serve, "make_server", lambda *a, **k: (SimpleNamespace(RequestHandlerClass=handler), [0.0]))
    monkeypatch.setattr(periodic, "start", lambda *a, **k: (_ for _ in ()).throw(Serving()))  # just after the start's lines
    for background in (False, True):
        with pytest.raises(Serving):
            serve.run(library, port=8790, open_browser=False, background=background, given={"library": str(library)})
        assert (f"No Hylly library at {library}: the library page says so until there is one. Is the disk or the share "
                f"there, and is the path right?") in capsys.readouterr().err
    (library / "catalog").mkdir(parents=True)
    with pytest.raises(Serving):
        serve.run(library, port=8790, open_browser=False, given={"library": str(library)})
    assert (f"No catalog yet at {library}: the library page says so until there is one. Run hylly catalog --library "
            f"{library}, then reload the page.") in capsys.readouterr().err
    with pytest.raises(Serving):
        serve.run(library, port=8790, open_browser=False)  # its library from the settings: so is the command's
    assert "Run hylly catalog, then reload the page." in capsys.readouterr().err
    (library / "catalog" / "index.html").write_text("left by an earlier version", encoding="utf-8")  # no catalog
    with pytest.raises(Serving):
        serve.run(library, port=8790, open_browser=False)
    assert "No catalog yet" in capsys.readouterr().err
    (library / "catalog" / "data.js").write_text("built", encoding="utf-8")  # what a build writes
    with pytest.raises(Serving):
        serve.run(library, port=8790, open_browser=False)
    assert "No catalog" not in capsys.readouterr().err and serve.missing_line(library, False) == ""

    running = {"pid": 1, "port": 8770, "lan": False, "login": "none", "library": str(library), "given": {}}
    monkeypatch.setattr(instance, "running", lambda: running)
    assert "No " not in serve.status()
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)  # the folder is there, but isn't marked as a library
    cli.main(["server", "status"])
    assert (f"  Library:           {library}\n  Messages:          {instance.log_file()}\nNo Hylly library at {library}: the "
            "library page says so") in capsys.readouterr().out
    cli.main(["server", "status", "--json"])
    assert json.loads(capsys.readouterr().out)["missing"] == "library"

    monkeypatch.setattr(instance, "running", lambda: None)
    monkeypatch.setattr(instance, "start_background", lambda: {**running, "pid": 2})
    monkeypatch.setenv("HYLLY_LIBRARY", str(library))  # in the background, the settings' library
    cli.main(["serve", "--background", "--no-browser"])
    said = capsys.readouterr().out
    assert said.startswith("Started the library server in the background.") and f"\nNo Hylly library at {library}: " in said


def test_a_share_gone_away_is_no_library_whatever_it_was_asked_last(tmp_path, monkeypatch):
    """Windows calls a share gone away "not found" too (53, 67): gone between the look at the folder and the one at the
    page, it is still no library, not a library with no catalog. Other trouble, such as no right to look, can't tell, and
    says nothing."""
    import errno
    from pathlib import Path

    class GoneShare(FileNotFoundError):
        winerror = 67

    library = tmp_path / "library"
    (library / "catalog").mkdir(parents=True)
    real, trouble = Path.stat, []

    def stat(self, *args, **kwargs):
        if self.name == "data.js" and trouble:
            raise trouble[0]
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert serve.missing(library) == "catalog"  # really not built yet
    trouble.append(GoneShare(errno.ENOENT, "The network name cannot be found"))
    assert serve.missing(library) == "library"
    trouble[0] = PermissionError(errno.EACCES, "Access is denied")
    assert serve.missing(library) is None


@pytest.mark.parametrize("windows", [True, False])
def test_a_suggested_command_quotes_a_folder_with_a_space_as_the_shell_needs_it(tmp_path, monkeypatch, windows):
    from hylly import paths

    monkeypatch.setattr(paths, "WINDOWS", windows)
    library = tmp_path / "My Books"
    if windows:
        assert paths.typed(r"C:\My Books") == '"C:\\My Books"' and paths.typed(r"C:\Books") == r"C:\Books"
    else:
        assert paths.typed("/home/ann/My Books") == "'/home/ann/My Books'" and paths.typed("/srv/books") == "/srv/books"
    quoted = paths.typed(library)
    assert quoted != str(library) and serve.missing_line(library, False).endswith(f"only for a new library: hylly init {quoted}")
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    with pytest.raises(SystemExit) as refused:
        paths.check_library(library)
    assert str(refused.value).endswith(f"Only for a new library: hylly init {quoted}")
    monkeypatch.setattr(paths, "REQUIRE_MARKER", False)
    (library / "catalog").mkdir(parents=True)
    assert f"Run hylly catalog --library {quoted}, then reload the page." in serve.missing_line(library, True)


def test_where_the_library_page_would_be_a_short_page_says_what_s_missing(tmp_path, monkeypatch, log_in):
    """A server on a folder that isn't a library (yet), or has no catalog: / and the library page's address answer with a
    short page saying so and what to run, instead of a bare "not found". With the headers of Hylly's own pages, kept by
    no browser, so a reload shows the library once it's there, and naming no folder, as anyone let in sees it."""
    from hylly import cli, paths

    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    paths.settings_file().write_text('[server]\nname = "Ann & the <Lantern> Shelf"\n', encoding="utf-8")
    library = tmp_path / "library"
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            def said(path: str = "/catalog/index.html") -> str:
                answer = client.get(path, follow_redirects=True)
                assert answer.status_code == 404 and answer.headers["content-type"] == "text/html; charset=utf-8"
                assert answer.headers["cache-control"] == "no-store" and answer.headers["x-frame-options"] == "DENY"
                assert answer.headers["cross-origin-opener-policy"] == "same-origin" and answer.headers["x-content-type-options"] == "nosniff"
                assert "frame-ancestors 'none'" in answer.headers["content-security-policy"]
                assert "<title>Ann &amp; the &lt;Lantern&gt; Shelf</title>" in answer.text and "<script" not in answer.text
                assert str(tmp_path) not in answer.text
                return answer.text

            assert "The library isn't there just now" in said("/") and "<code>hylly init</code>" in said("/catalog/")
            library.mkdir()  # there, but not marked as a library: an empty folder under an unmounted disk, say
            assert "The library isn't there just now" in said()
            cli.main(["init", str(library), "--yes"])
            assert "The library has no catalog yet" in said("/") and "<code>hylly catalog</code>" in said()
            assert client.get("/catalog/data.js").text == "not found"  # only the page's own address says why
            (library / "catalog").mkdir()
            (library / "catalog" / "index.html").write_text("left by an earlier build", encoding="utf-8")
            assert "The library has no catalog yet" in said()  # a page left in the folder is no catalog
            write_data(library, {"items": []})  # what a build writes
            page = client.get("/", follow_redirects=True)
            assert page.status_code == 200 and "The library has no catalog" not in page.text and "left by" not in page.text
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_ereader_feeds_settings_written_wrong_name_the_section_and_the_key():
    from hylly import paths

    for text, said in (('max_mb = "lots"', r"\[ereader_feed\] max_mb = 'lots': a number, such as 63"),
                       ("max_mb = nan", r"max_mb = nan: a number"),
                       ('skip_tablet = "no"', r"skip_tablet = 'no': true or false"),
                       ('formats = "epub"', r"formats = 'epub': a list of formats in quotes, such as \[\"epub\", \"pdf\"\]")):
        paths.settings_file().write_text(f"[ereader_feed]\n{text}\n", encoding="utf-8")
        with pytest.raises(SystemExit, match=said):
            ereader_feed()
    paths.settings_file().write_text('[ereader_feed]\nformats = ["EPUB"]\nmax_mb = 80\n', encoding="utf-8")
    feed = ereader_feed()
    assert feed.formats == ("epub",) and feed.max_mb == 80.0 and feed.kinds == ("book", "tech")


def test_a_hidden_server_with_a_mistake_says_so_where_it_was_started(tmp_path):
    """A real start in the background, whose reason comes from the lines it wrote into its own log."""
    from hylly import instance, paths

    mistake_in_the_rules()
    paths.settings_file().write_text(f"library = '{tmp_path.as_posix()}'\n[server]\nport = 8799\n", encoding="utf-8")
    try:
        with pytest.raises(SystemExit, match=r"did not start: .*mine\.toml, \[genres\.Lanterns\] words: a list"):
            instance.start_background()
    finally:  # one that started after all, the mistake missed, is stopped: never left running after a red run
        if instance.running():
            instance.stop()
    assert "mine.toml" in instance.log_file().read_text(encoding="utf-8")


def listening(root, listen: str):
    srv, _ = serve.make_server(root, listen, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_it_listens_where_listen_says_and_on_one_port(tmp_path, log_in):
    """[server] listen: 127.0.0.1 as it comes, every IPv4 address ("0.0.0.0"), or every address ("::"), IPv4
    too where the system lets it. Asked here at 127.0.0.1 and ::1 only: nothing reaches past this computer. An IPv4
    sender on "::" is itself, not ::ffff:127.0.0.1: the trusted proxies, the log and the lockout go by it."""
    import socket

    from hylly import access

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    for listen in ("127.0.0.1", "0.0.0.0"):
        srv = listening(tmp_path, listen)
        try:
            assert srv.socket.family == socket.AF_INET and srv.server_address[0] == listen
            with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
                assert client.get("/ping").status_code == 200
        finally:
            srv.shutdown()
            srv.server_close()
    if not socket.has_ipv6:
        pytest.skip("this system has no IPv6, so nothing listens on '::' (the IPv4 ones above are tried)")
    try:
        srv = listening(tmp_path, "::")
    except OSError as e:
        pytest.skip(f"this system takes no IPv6 socket on '::' ({e.strerror}): the IPv4 ones above are tried")
    seen = []
    srv.RequestHandlerClass.watch.request = lambda sender, *a: seen.append(sender)
    try:
        assert srv.socket.family == socket.AF_INET6
        with log_in(httpx.Client(base_url=f"http://[::1]:{srv.server_port}")) as client:
            assert client.get("/opds", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200
        if socket.has_dualstack_ipv6():
            with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
                assert client.get("/opds", headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200
                assert client.get("/opds").status_code == 200
            for _ in range(100):  # the handler notes a request as it ends, which may be a moment after its answer
                if len(seen) >= 3:
                    break
                time.sleep(0.02)
            assert seen == ["203.0.113.9", "198.51.100.7", "127.0.0.1"]  # this computer, trusted, and plainly IPv4
        else:
            assert seen == ["203.0.113.9"]
    finally:
        srv.shutdown()
        srv.server_close()
    assert access.unmapped("::ffff:192.0.2.7") == "192.0.2.7" and access.unmapped("2001:db8::1") == "2001:db8::1"


def test_where_it_listens_is_said_in_the_settings_and_lan_is_one_runs_every_ipv4_address():
    from hylly import paths, settings

    assert serve.options() == (8770, "127.0.0.1")
    for written, wanted in (('"0.0.0.0"', "0.0.0.0"), ('"::"', "::"), ('"::0"', "::"), ('" 192.0.2.10 "', "192.0.2.10"),
                            ('"::1"', "::1")):
        paths.settings_file().write_text(f"[server]\nlisten = {written}\n", encoding="utf-8")
        settings.reload()  # as a command reads them
        assert serve.options()[1] == wanted, written
        assert serve.options({"lan": True})[1] == "0.0.0.0" and serve.options({"lan": False})[1] == "127.0.0.1"
    for wrong in ('"everywhere"', '"localhost"', "true", '"192.0.2.300"', '""'):
        settings.reload()  # a command, which a mistake stops (a running server keeps what it had)
        paths.settings_file().write_text(f"[server]\nlisten = {wrong}\n", encoding="utf-8")
        with pytest.raises(SystemExit, match=r"\[server\] listen = .*: an address of this computer's in quotes"):
            serve.options()


def test_a_port_the_system_refuses_says_why_not_only_that_it_is_taken(tmp_path, monkeypatch):
    """A port another program has is one reason; the system refusing it (a service's walls, SocketBindAllow) is
    another, and an address this computer doesn't have a third."""
    import errno
    import socket

    from hylly import exits

    said = {code: serve.cant_listen(OSError(code, "made-up words"), "0.0.0.0", 8790)
            for code in (errno.EADDRINUSE, errno.EACCES, errno.EPERM, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EIO)}
    assert said[errno.EADDRINUSE].startswith("Port 8790 is taken by another program (made-up words). Choose another with --port")
    for refused in (said[errno.EACCES], said[errno.EPERM]):
        assert refused.startswith("The system refused to let the server listen at 0.0.0.0 port 8790 (made-up words).")
        assert "SocketBindAllow" in refused and "8790 must be one of them" in refused and "taken" not in refused
    assert said[errno.EADDRNOTAVAIL].startswith('listen = "0.0.0.0" under [server] in ') and "isn't an address of this computer" in said[errno.EADDRNOTAVAIL]
    assert 'listen = "0.0.0.0"' in said[errno.EAFNOSUPPORT] and "no IPv6" in said[errno.EAFNOSUPPORT]
    assert said[errno.EIO] == "The server couldn't listen at 0.0.0.0 port 8790: made-up words."
    with socket.socket() as taken:  # a real one: a port another program has
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(exits.Exit, match=rf"Port {taken.getsockname()[1]} is taken by another program"):
            serve.run(tmp_path, port=taken.getsockname()[1], open_browser=False)


def test_only_the_listening_is_said_as_the_port_s_trouble(tmp_path, monkeypatch, capsys):
    """A settings folder the server may not write, say, isn't a port that's taken: it keeps its own words and its
    file's name, as hyllyd's and hylly's "Can't go on" say them. A port below 1024 needs rights of its own on Linux and
    macOS, which is said as such."""
    import errno

    from hylly import access, daemon, exits

    folder = str(tmp_path / "Hylly" / "hylly.db")
    monkeypatch.setattr(access, "Gate", lambda **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "Permission denied", folder)))
    with pytest.raises(PermissionError) as e:
        serve.make_server(tmp_path, "127.0.0.1", 0)
    assert not isinstance(e.value, serve.CantListen)
    with pytest.raises(SystemExit) as e:
        daemon.main(["--library", str(tmp_path), "--port", "8797"])
    said = capsys.readouterr().err
    assert e.value.code == exits.LOCAL and "Can't go on: [Errno 13] Permission denied: " in said and "hylly.db" in said
    assert "taken by another program" not in said and "listen at" not in said
    if os.name != "nt":
        low = serve.cant_listen(OSError(errno.EACCES, "made-up words"), "0.0.0.0", 80)
        assert "a port below 1024 takes rights of its own" in low and "Choose one above it" in low and "taken" not in low


def test_a_newer_database_at_the_start_is_said_once_and_raises_the_alarm(tmp_path, capsys):
    from hylly import bell, db

    with db.database().write() as c:
        c.execute("PRAGMA user_version = 99")  # as a newer version of Hylly leaves it
    db.close_all()
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    srv.server_close()
    said = [line for line in capsys.readouterr().err.splitlines() if "newer version of Hylly" in line]
    assert len(said) == 1 and "The database: " in said[0]  # in server.log
    told = bell.log_file().read_text(encoding="utf-8")
    assert told.count("What's kept for everyone") == 1 and "newer version of Hylly" in told


def test_the_server_takes_the_library_on_as_it_starts_if_it_is_there(tmp_path, monkeypatch):
    """Before anyone asks, so that its nightly copy is made from the first night: the library's marker names this
    computer's database. A library whose disk isn't there yet is taken on when it is."""
    from hylly import db, paths

    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)  # no marker of a library there: its disk isn't connected
    srv, _ = serve.make_server(away, "127.0.0.1", 0)
    srv.server_close()
    assert not db.owner_file(away).exists()
    monkeypatch.setattr(paths, "REQUIRE_MARKER", False)
    library = tmp_path / "library"
    (library / "state").mkdir(parents=True)
    srv, _ = serve.make_server(library, "127.0.0.1", 0)
    srv.server_close()
    marker = json.loads(db.owner_file(library).read_text(encoding="utf-8"))
    with db.database().read() as c:
        assert c.execute("SELECT id FROM library").fetchone()[0] == marker["id"]


def test_a_server_of_this_version_takes_on_the_marker_the_version_before_left_and_says_so_in_its_log(tmp_path, capsys):
    """The move from before, on a live server: its library's marker, as the version before wrote it, names no install.
    The server starting on it is never refused: it ties the marker to its install, once, and says so in server.log."""
    from hylly import db

    root = tmp_path / "library"
    before = as_hylly_left_it_before(root)
    db.close_all()
    for _ in range(2):  # and its next start
        srv, _ = serve.make_server(root, "127.0.0.1", 0)
        srv.server_close()
        db.close_all()
    said = [line for line in capsys.readouterr().err.splitlines() if "names this install too" in line]
    assert len(said) == 1 and "The database: " in said[0]  # in server.log
    token = db.token_file().read_text(encoding="utf-8").strip()
    assert json.loads(db.owner_file(root).read_text(encoding="utf-8")) == {**before, "token": token}


def test_a_running_server_keeps_the_records_no_more_once_another_database_keeps_them(tmp_path, monkeypatch, capsys, log_in):
    """Two settings folders on one library, as two computers: the records pass to the second while the first's server
    runs. From then on the first refuses changes, shows the catalog as built (the tags are the second's, which it can't
    read), writes no nightly copy, and says so once, in its log and as an alarm."""
    from datetime import datetime

    from hylly import bell, curation, db, dbcopy

    root = tmp_path / "library"
    build_library(root)
    catalog.build(root, log=lambda s: None)
    first, second = os.environ["HYLLY_HOME"], str(tmp_path / "another-computer")
    srv, _ = serve.make_server(root, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            key = {"X-Server-Key": client.get("/api/session").json()["key"]}

            def tag(name):
                return client.post("/api/tags", json={"op": "add", "tag": name, "ids": ["h:novel"]}, headers=key)

            def found(word):
                return [e.find(f"{ATOM}title").text for e in entries(client.get("/opds/search", params={"q": word}).text)]

            assert tag("Knitting").json()["ok"] and found("knitting") == ["The Novel"]
            monkeypatch.setenv("HYLLY_HOME", second)  # the second computer, the first's server still running
            with pytest.raises(db.NotHere, match="first stop the server"):
                curation.load(root)
            db.take_over(root)  # and the records taken over there, afresh, as the refusal says (hylly server take-over)
            novel = next(i for i in Catalog(root).get() if i["id"] == "h:novel")
            for name in ("Weaving", "Dyeing", "Spinning"):
                curation.change(root, "add", name, [novel], by="owen")
            assert not (root / "catalog" / "tags.js").exists()  # nothing written for the page: the server lays them over
            monkeypatch.setenv("HYLLY_HOME", first)
            for _ in range(2):
                assert tag("Crochet").status_code == 409
            assert found("weaving") == [] and found("knitting") == []  # the catalog as built: the tags are the other one's
            shown = page_data(client)["items"]
            assert "The Novel" in [it["t"] for it in shown] and not any(it.get("tg") for it in shown)
            said = []
            dbcopy.copy_tonight(root, said.append, datetime(2026, 11, 20, 4))
            assert "wasn't copied" in said[0] and not db.backups(root).exists()
            db.owner_file(root).unlink()
            assert tag("Crochet").status_code == 409 and not db.owner_file(root).exists()  # refused from then on, never taken back
    finally:
        srv.shutdown()
        srv.server_close()
    with db.database().read() as c:
        assert c.execute("SELECT count(*) FROM edit").fetchone()[0] == 1  # the first's own database: only what it kept before
    told = [line for line in capsys.readouterr().err.splitlines() if "now kept in the database on" in line]  # server.log
    assert len(told) == 1 and "The database: " in told[0] and "on this computer, in another data folder" in told[0]
    assert "hylly server stop" in told[0]
    assert bell.log_file().read_text(encoding="utf-8").count("now kept in the database on") == 1


def test_once_checked_the_marker_costs_one_look_at_its_date_until_it_changes(tmp_path, monkeypatch):
    """The server asks at every request whose the records are: while the marker stays as it was, it isn't read and the
    database isn't asked. Written again naming the same database, it's read once more and compared, and the records
    stay here; gone with its disk, nothing is taken on afresh."""
    from pathlib import Path

    from hylly import db

    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    found = db.database()
    for _ in range(2):  # taken on, and the marker it wrote read once
        db.own(found, root)
    marker = db.owner_file(root)
    stat, read_text = Path.stat, Path.read_text

    def counted(m, looked, read):
        def stat_counted(self, **k):
            if self == marker:
                looked.append(self)
            return stat(self, **k)

        def read_counted(self, *a, **k):
            if self == marker:
                read.append(self)
            return read_text(self, *a, **k)

        m.setattr(Path, "stat", stat_counted)
        m.setattr(Path, "read_text", read_counted)

    looked, read = [], []
    with monkeypatch.context() as m:
        counted(m, looked, read)
        m.setattr(found, "write", lambda: pytest.fail("the database was asked"))
        for _ in range(3):
            db.own(found, root)
    assert looked == [marker] * 3 and read == []
    text, then = marker.read_text(encoding="utf-8"), marker.stat().st_mtime_ns
    marker.unlink()
    marker.write_text(text, encoding="utf-8")  # written again, the same database named: a restore, say
    os.utime(marker, ns=(then + 10**9, then + 10**9))  # a moment later
    looked, read = [], []
    with monkeypatch.context() as m:
        counted(m, looked, read)
        m.setattr(found, "write", lambda: pytest.fail("it waited to write"))  # only compared: nothing to write
        for _ in range(2):
            db.own(found, root)
    assert read == [marker] and found.lost == {}
    marker.unlink()  # gone with its disk
    db.own(found, root)
    assert not marker.exists()


def test_a_request_that_fails_where_nothing_caught_it_still_gets_an_answer(server, monkeypatch, capsys):
    from hylly import rules

    client, root = server
    said, real = r"D:\Books\curation\rules\mine.toml, [genres.Lanterns] words: a list", opds.answer
    monkeypatch.setattr(opds, "answer", lambda *a, **k: (_ for _ in ()).throw(SystemExit(said)))
    for _ in range(2):
        answer = client.get("/opds")
        assert answer.status_code == 500 and answer.text == serve.Handler.FAILED_TEXT.decode()  # no folder of this computer
    assert capsys.readouterr().err.count(f"A request failed: {said}") == 1  # in server.log, once
    monkeypatch.setattr(opds, "answer", lambda *a, **k: 1 / 0)
    assert client.get("/opds").status_code == 500
    err = capsys.readouterr().err
    assert "A request failed: ZeroDivisionError" in err and "Traceback" in err
    monkeypatch.setattr(opds, "answer", real)
    rules.reset()  # a server made without serve.run, its rules never read
    mistake_in_the_rules()
    # a catalog from before the build made the series keys: the series feed reads the rules
    data = read_data(root)
    write_data(root, {**data, "items": [{k: v for k, v in it.items() if k != "sk"} for it in data["items"]]})
    assert client.get("/opds/series").status_code == 500 and client.get("/opds").status_code == 200


def test_a_served_catalog_tells_its_troubles_in_the_server_s_log_and_a_lasting_one_once_an_hour(library, serving,
                                                                                                monkeypatch, capsys):
    """The catalog the server serves (library.Catalog, made by make_server) tells its troubles as the server's own: a
    message in the server's own words (_stamp), and a failure once as it begins and again once an hour while it goes on
    (tell_failure). Here, the ids a rebuild took away can't be followed, and the titles reload all the same."""
    srv, client = serving(library)
    with monkeypatch.context() as m:
        said = []
        m.setattr(serve, "_stamp", said.append)
        srv.RequestHandlerClass.catalog.say("A word of the catalog's.")
        assert said == ["A word of the catalog's."]
    data = read_data(library)
    assert client.get("/api/catalog").status_code == 200
    monkeypatch.setattr("hylly.library.moved_ids", lambda *a: 1 / 0)
    for _ in range(2):
        write_data(library, {**data, "generated": "again"})
        assert client.get("/api/catalog").json() == {"generated": "again"}  # reloaded all the same
    err = capsys.readouterr().err
    assert err.count("Ids that went in a rebuild couldn't be followed") == 1 and "ZeroDivisionError" in err
    monkeypatch.setattr(serve.Handler, "FAILED_AGAIN", 0)  # an hour on
    write_data(library, data)
    assert client.get("/api/catalog").status_code == 200
    assert "until it's reloaded, 2 more times since it was last said: ZeroDivisionError" in capsys.readouterr().err


def test_a_failure_that_goes_on_is_said_again_each_hour_with_how_often_it_came(server, monkeypatch, capsys):
    """server.log says a kind of failure as it begins, with where it came from, and again once an hour has passed since
    it was last said, while it goes on, with how many times it came since: one that lasts shows for as long as it lasts."""
    client, _ = server
    monkeypatch.setattr(serve.Handler, "FAILED", set())  # each kind new again
    monkeypatch.setattr(opds, "answer", lambda *a, **k: 1 / 0)
    for _ in range(2):
        assert client.get("/opds").status_code == 500
    err = capsys.readouterr().err
    assert err.count("A request failed: ZeroDivisionError: division by zero") == 1 and err.count("Traceback") == 1
    monkeypatch.setattr(serve.Handler, "FAILED_AGAIN", 0)  # an hour on
    assert client.get("/opds").status_code == 500
    err = capsys.readouterr().err
    assert "A request failed, 2 more times since it was last said: ZeroDivisionError: division by zero" in err
    assert "Traceback" not in err
    assert client.get("/opds").status_code == 500
    assert "A request failed, 1 more time since it was last said:" in capsys.readouterr().err  # counted afresh


def test_records_that_cant_be_read_or_kept_are_answered_as_the_page_expects(server, monkeypatch, capsys):
    """The database can't be used just then (another command holding it past its wait, a full disk): a change of yours
    is refused in JSON, as the library page expects, and so is a look at what's kept, while the page's session still
    comes, without what couldn't be read. server.log says what it was, once for each place."""
    import sqlite3

    from hylly import people

    client, _ = server
    monkeypatch.setattr(serve.Handler, "FAILED", set())  # each kind new again
    session = client.get("/api/session").json()
    keys = {"X-Personal-Key": session["me"]["key"], "X-Server-Key": session["key"]}

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    for name in ("place", "new_seen", "shelves"):
        monkeypatch.setattr(people.Person, name, locked)
    monkeypatch.setattr(serve.curation, "change", locked)
    for _ in range(2):
        kept = client.post("/api/me/place", json={"id": "h:novel", "at": 3, "pct": 0.1}, headers=keys)
        assert kept.status_code == 500 and kept.json() == {"ok": False, "error": "It couldn't be kept (OperationalError)."}
    read = client.get("/api/shelves")
    assert read.status_code == 500 and read.json() == {"ok": False, "error": "They couldn't be read (OperationalError)."}
    tagged = client.post("/api/tags", json={"op": "add", "tag": "Knitting", "ids": ["h:novel"]}, headers=keys)
    assert tagged.status_code == 500 and tagged.json()["error"] == "It couldn't be kept (OperationalError)."
    for _ in range(2):  # what's yours left out of the session, and said once, as the record handlers say it
        again = client.get("/api/session")
        assert again.status_code == 200 and again.json()["me"]["newSeen"] == "" and again.json()["me"]["key"] == keys["X-Personal-Key"]
    for name in ("_everyones", "_report_states"):  # the owners' queues, and the changes, suggestions and declined tags
        monkeypatch.setattr(serve.Handler, name, locked)
    for name in ("history", "pending", "declined"):
        monkeypatch.setattr(serve.curation, name, locked)
    for path in ("/api/reviews/waiting", "/api/reports", "/api/edits", "/api/suggestions", "/api/declined"):
        read = client.get(path)
        assert read.status_code == 500 and read.json() == {"ok": False, "error": "They couldn't be read (OperationalError)."}, path
    err = capsys.readouterr().err
    assert err.count("What's kept couldn't be read or kept: OperationalError: database is locked") == 9  # each place once
    assert "A request failed" not in err and "Traceback" not in err


def test_a_client_gone_while_its_answer_goes_is_no_failure_of_the_records(server, monkeypatch, capsys):
    """A phone that drops the connection while what's kept is on its way to it: nothing to answer, and nothing in
    server.log, as no record failed."""
    client, _ = server
    monkeypatch.setattr(serve.Handler, "FAILED", set())
    real = serve.Handler._send

    def gone(self, status, *args, **kwargs):
        if status == 200 and self._url.path.startswith(("/api/me/", "/api/edits")):
            self._status = status  # its answer begun, and the client gone
            raise BrokenPipeError(32, "Broken pipe")
        return real(self, status, *args, **kwargs)
    monkeypatch.setattr(serve.Handler, "_send", gone)
    for path in ("/api/me/places", "/api/me/lists", "/api/me/bookmarks", "/api/edits"):
        with pytest.raises(httpx.TransportError):
            client.get(path)
    out, err = capsys.readouterr()
    assert "What's kept" not in err and "A request failed" not in err and "Traceback" not in out + err


def test_root_and_navigation(server):
    """Every feed is the logged-in person's own: it starts with what they're reading and their reading list."""
    client, _ = server
    r = client.get("/opds")
    assert r.status_code == 200 and "kind=navigation" in r.headers["content-type"]
    titles = [e.find(f"{ATOM}title").text for e in entries(r.text)]
    assert titles[:4] == ["Continue reading", "Reading list", "Recently added", "Recently bought"]
    assert "Humble Bundle" in titles and "Series" in titles
    assert client.get("/").headers["location"] == "/catalog/index.html"
    assert client.get("/catalog/index.html").status_code == 200


def test_copies_are_one_entry_listed_under_every_source(tmp_path):
    build_library(tmp_path)
    copy = tmp_path / "collections" / "Ellery" / "The Novel - Marlow Ellery.epub"  # the Humble novel, also in a collection
    copy.parent.mkdir(parents=True)
    make_epub(copy, OPF.replace("The Long Dust", "The Novel"))
    catalog.build(tmp_path, log=lambda s: None)
    cat = Catalog(tmp_path)
    novel = [i for i in cat.get() if i["t"] == "The Novel"]
    assert len(novel) == 1 and novel[0]["id"] == "h:novel" and novel[0]["cp"] == [["collections", "Ellery"]] and len(novel[0]["f"]) == 2
    assert "urn:hylly:h:novel" in opds.answer(cat, "/opds/src/collections")[2]


def test_search_and_download(server):
    client, root = server
    found = entries(client.get("/opds/search", params={"q": "novel elle"}).text)  # every word, by prefix
    assert [e.find(f"{ATOM}title").text for e in found] == ["The Novel"]
    acq = [link for link in found[0].findall(f"{ATOM}link") if link.get("rel") == "http://opds-spec.org/acquisition"][0]
    assert acq.get("type") == "application/epub+zip"
    body = client.get(acq.get("href"))
    assert body.status_code == 200 and body.content == (root / acq.get("href").lstrip("/").replace("%20", " ")).read_bytes()
    part = client.get(acq.get("href"), headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10 and part.headers["content-range"].startswith("bytes 0-9/")
    cover = [link for link in found[0].findall(f"{ATOM}link") if link.get("rel") == "http://opds-spec.org/image"][0]
    assert client.get(cover.get("href")).status_code == 200


def test_rebuilt_catalog_and_a_change_show_on_reload(server):
    client, root = server
    first = client.get("/catalog/data.js")
    assert first.headers["cache-control"] == "no-cache" and first.headers["etag"]  # the browser asks every time
    since = {"If-None-Match": first.headers["etag"]}
    unchanged = client.get("/catalog/data.js", headers=since)  # ...and gets "unchanged" cheaply
    assert unchanged.status_code == 304 and unchanged.content == b"" and unchanged.headers["etag"] == first.headers["etag"]
    assert client.get("/catalog/data.js", headers={"If-None-Match": f'"x", {first.headers["etag"]}'}).status_code == 304
    data = read_data(root)
    data["items"][0]["t"] = "Moonwake Tide"
    write_data(root, data)  # hylly catalog ran again
    again = client.get("/catalog/data.js", headers=since)
    assert again.status_code == 200 and "Moonwake Tide" in again.text and again.headers["etag"] != first.headers["etag"]
    key = {"X-Server-Key": client.get("/api/session").json()["key"]}
    assert client.post("/api/tags", json={"op": "add", "tag": "Knitting", "ids": [data["items"][0]["id"]]}, headers=key).json()["ok"]
    tagged = client.get("/catalog/data.js", headers={"If-None-Match": again.headers["etag"]})  # an edit: made again at once
    assert tagged.status_code == 200 and '"tg":["Knitting"]' in tagged.text


def test_text_is_sent_packed(server):
    client, root = server
    data = client.get("/catalog/data.js")  # httpx asks for gzip and unpacks it
    assert data.headers["content-encoding"] == "gzip" and data.headers["vary"] == "Accept-Encoding"
    assert int(data.headers["content-length"]) < len(data.content)
    plain = client.get("/catalog/data.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.content == data.content
    assert int(plain.headers["content-length"]) == len(plain.content)
    whole = client.get("/catalog/data.js", headers={"Range": "bytes=0-9"})  # made by the server: always whole
    assert whole.status_code == 200 and whole.content == data.content
    head = client.head("/catalog/data.js")
    assert head.status_code == 200 and head.content == b"" and head.headers["etag"] == data.headers["etag"]
    feed = client.get("/opds/series")
    assert feed.headers["content-encoding"] == "gzip" and entries(feed.text)
    assert "content-encoding" not in client.get("/catalog/data.js", headers={"Accept-Encoding": "gzip;q=0"}).headers


def page_data(client) -> dict:
    """The library page's data.js as the server makes it: one JSON value, and nothing else."""
    text = client.get("/catalog/data.js").text
    assert text.startswith("window.CATALOG = ") and text.endswith(";\n")
    return json.loads(text.removeprefix("window.CATALOG = ").removesuffix(";\n"))


def test_the_page_s_data_is_the_server_s_own_with_your_tags_and_corrections_laid_over_it(server):
    """/catalog/data.js is made by the server from the catalog it has read, with the database's tags and
    corrections laid over it, never catalog/data.js as it is: what a file there holds after its data never reaches the
    page, and a tag or a correction is in the next answer, with no file in between to fall behind. A series or an author
    you correct gets its sort key made again; what you corrected and what the catalog found come along for the details."""
    client, root = server
    disk = root / "catalog" / "data.js"
    disk.write_text(disk.read_text(encoding="utf-8") + "window.PUT_THERE = 'by hand';\n", encoding="utf-8")
    os.utime(disk, (disk.stat().st_mtime + 5, disk.stat().st_mtime + 5))
    assert "PUT_THERE" not in client.get("/catalog/data.js").text
    novel = next(it for it in page_data(client)["items"] if it["id"] == "h:novel")
    assert (novel["s"], novel["sk"], novel["ak"]) == ("Dust and Ember", "dustandember", "ellery marlow")
    assert not {"fx", "o", "tg"} & set(novel) and not any(k.startswith("_") for k in novel)
    key = {"X-Server-Key": client.get("/api/session").json()["key"]}
    assert client.post("/api/tags", json={"op": "add", "tag": "Knitting", "ids": ["h:novel"]}, headers=key).json()["ok"]
    for field, value in (("series", "The Yarn Collection"), ("authors", ["Anna van den Berg"]), ("kind", "tech")):
        fixed = client.post("/api/fix", json={"field": field, "value": value, "ids": ["h:novel"]}, headers=key).json()
    novel = next(it for it in page_data(client)["items"] if it["id"] == "h:novel")
    assert fixed["items"]["h:novel"]["title"] == novel  # the answer to a change: the title as the page gets it
    assert (novel["tg"], novel["s"], novel["sk"], novel["a"], novel["ak"], novel["k"]) == \
        (["Knitting"], "The Yarn Collection", "yarn", ["Anna van den Berg"], "van den berg anna", "tech")
    assert novel["fx"] == {"series": "The Yarn Collection", "authors": ["Anna van den Berg"], "kind": "tech"}
    assert novel["o"] == {"s": "Dust and Ember", "a": ["Marlow Ellery"], "k": "book"}  # what the catalog found
    assert not (root / "catalog" / "tags.js").exists()
    for field in ("series", "authors", "kind"):
        client.post("/api/fix", json={"field": field, "value": "auto", "ids": ["h:novel"]}, headers=key)
    novel = next(it for it in page_data(client)["items"] if it["id"] == "h:novel")
    assert (novel["s"], novel["sk"], novel["ak"], novel["k"]) == ("Dust and Ember", "dustandember", "ellery marlow", "book")
    assert "fx" not in novel and "o" not in novel and novel["tg"] == ["Knitting"]


def test_the_page_s_data_is_made_once_for_each_catalog_and_edit(tmp_path):
    from hylly import curation

    write_data(tmp_path, {"generated": "x", "items": [{"id": "f:1", "t": "Moonwake", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    cat = Catalog(tmp_path)
    first = cat.page_script()
    assert cat.page_script() is first  # kept, not made again
    curation.change(tmp_path, "add", "Knitting", cat.get(), by="owen")
    second = cat.page_script()
    assert second is not first and second[0] != first[0] and b'"tg":["Knitting"]' in second[1]
    assert cat.page_script() is second and gzip.decompress(second[2]) == second[1]
    write_data(tmp_path, {"generated": "y", "items": []})
    shops = json.dumps(SHOP_TRAITS, separators=(",", ":"))  # what the page needs to know of each shop, always sent
    kinds = json.dumps(kinds_table(), separators=(",", ":"))  # and the kinds, with their names: the page has none
    assert cat.page_script()[1] == (f'window.CATALOG = {{"generated":"y","sources":{{}},"shops":{shops},"kinds":{kinds},'
                                    '"items":[]};\n').encode()
    assert json.loads(kinds)["comic"] == {"label": "Comic", "plural": "Comics", "device": "tablet", "text_first": False}
    from hylly import paths, rules  # the rules read again with another name for a kind: the page's data made anew

    folder = paths.settings_file().parent / "rules"
    folder.mkdir(exist_ok=True)
    for label in ("Strip", "Comic", "Strip"):  # read again and again, as a running server does: never the data of before
        (folder / "mine.toml").write_text(f'[kinds.comic]\nlabel = "{label}"\n', encoding="utf-8")
        rules.reset()
        assert f'"comic":{{"label":"{label}"'.encode() in cat.page_script()[1]


def test_no_title_author_or_tag_can_run_as_the_page_s_code(tmp_path):
    """Whatever a title, an author or a tag holds, the page's data.js is one JSON value: quotes, a </script>, the two
    characters that end a line in an older browser's script even inside a string (U+2028, U+2029) and half of a pair of
    them all stay in their strings, and the page reads back exactly what the catalog has."""
    from hylly import curation

    odd = 'Moonwake "Tide"\'; </script><b>x</b>   line   part \\" end'
    half = odd + " \ud800"  # half of a pair, as a JSON file may hold it: in the description, which no edit keeps
    item = {"id": "f:1", "t": odd, "a": [odd, "Ann Writer"], "s": odd, "d": half, "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}
    write_data(tmp_path, {"generated": "x", "sources": {"shelf": odd}, "items": [item]})
    cat = Catalog(tmp_path)
    tag = 'Lantern "Wolf" </script>'
    curation.change(tmp_path, "add", tag, cat.get(), by="owen")
    _, body, _ = cat.page_script()
    text = body.decode("utf-8")  # and so whole UTF-8
    assert " " not in text and " " not in text and text.count("\n") == 1 and text.endswith(";\n")
    data = json.loads(text.removeprefix("window.CATALOG = ").removesuffix(";\n"))
    assert (data["items"][0]["t"], data["items"][0]["a"][0], data["items"][0]["tg"], data["sources"]) == (odd, odd, [tag], {"shelf": odd})
    assert data["items"][0]["d"] == half
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the script is read by a browser here")
    run = (f"const window = {{}};\n{text}\nprocess.stdout.write(JSON.stringify([window.CATALOG.items[0].t, "
           "window.CATALOG.items[0].d, window.CATALOG.items[0].tg, Object.keys(window)]));\n")
    out = subprocess.run([node, "-"], input=run, capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(out.stdout) == [odd, half, [tag], ["CATALOG"]]  # as a browser reads it: the data, and nothing ran


COVERS = {  # a title's cover: whether the build would write it (catalog.record), and so whether the server and the page keep it
    "covers/0123456789abcdef.webp": True, "https://img.example.net/a.png?w=1": True, "https://art.example.org:8443/c.png": True,
    "https://img.example.net": True, "covers/own.webp": False, "covers/0123456789abcdef.webp?x": False,
    "covers/0123456789abcdef.webp/../../api/session": False, "covers/../covers/0123456789abcdef.webp": False,
    "http://192.168.1.20/cover.png": False, "http://plain.example/d.png": False, "file:///C:/Users/Public/cover.png": False,
    "//no-scheme.example/f.png": False, "https://who@hidden.example/e.png": False, "https:///cover.png": False,
    "/api/session": False, "../state/x.png": False, "": False,
}


def test_only_a_cover_the_build_would_write_reaches_the_page_the_feeds_or_the_policy(tmp_path):
    """An older catalog, or one changed by hand, may hold any address as a cover. The server keeps a title's own
    thumbnail and a shop's https address with a host (good_cover), and nothing else reaches the page's data, the feeds'
    image links, or the sites the page's policy names."""
    items = [{"id": f"f:{n}", "t": f"Moonwake {n}", "k": "book", "f": [[f"a/Moonwake {n}.epub", "epub", 1]], "c": cover}
             for n, cover in enumerate(COVERS)]
    write_data(tmp_path, {"generated": "x", "items": items})
    cat = Catalog(tmp_path)
    kept = {it["t"]: it.get("c") for it in cat.get()}
    assert {cover: kept[f"Moonwake {n}"] == cover for n, cover in enumerate(COVERS) if cover} == {c: ok for c, ok in COVERS.items() if c}
    assert cat.cover_sites() == ("art.example.org:8443", "img.example.net")
    shown = json.loads(cat.page_script()[1].decode().removeprefix("window.CATALOG = ").removesuffix(";\n"))
    assert sorted(it["c"] for it in shown["items"] if "c" in it) == sorted(c for c, ok in COVERS.items() if ok)
    feed = "".join(opds.book_entry(it) for it in cat.get())  # the reading apps' image links
    images = set(re.findall(r'<link rel="http://opds-spec.org/image" href="([^"]*)"', feed))
    assert images == {c if c.startswith("https://") else f"/catalog/{c}" for c, ok in COVERS.items() if ok}


def test_the_page_keeps_the_covers_the_server_keeps():
    """The page checks a cover too (catalog_page.html, COVER), for the data it has, by the server's rule."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't on this computer: the page's check runs only in a browser here")
    page = (Path(serve.__file__).parent / "catalog_page.html").read_text(encoding="utf-8")
    rule = re.search(r"^const COVER = [^\n]*\n", page, re.M)[0]
    shown = {**COVERS, **VERSIONED_COVERS}  # as the build writes them, and the catalog's own as the server sends them
    run = f"{rule}process.stdout.write(JSON.stringify({json.dumps(list(shown))}.map(c => COVER.test(c))));\n"
    out = subprocess.run([node, "-"], input=run, capture_output=True, text=True, encoding="utf-8", check=True)
    assert dict(zip(shown, json.loads(out.stdout))) == {c: shown_cover(c) for c in shown} == shown
    assert {c: good_cover(c) for c in COVERS} == COVERS and not any(good_cover(c) for c in VERSIONED_COVERS)  # never data.js's


VERSIONED_COVERS = {  # one of the catalog's own with its version, as only the server's page data has it (Catalog._for_page)
    "covers/0123456789abcdef.webp?v=1763560000123456789": True, "covers/0123456789abcdef.webp?v=": False,
    "covers/0123456789abcdef.webp?v=17x": False, "covers/0123456789abcdef.webp?v=1&v=2": False,
    "covers/0123456789abcdef.webp?v=1#x": False, "covers/own.webp?v=1": False,
    "covers/0123456789abcdef.webp?v=\u0661\u0662": False,  # digits of another script: the page's \d takes 0-9 alone
    "covers/0123456789abcdef.webp?v=123456789012345678901": False,  # longer than a time in nanoseconds can be
}


def test_the_page_gets_its_covers_by_their_versions_and_keeps_them_while_the_reading_apps_get_theirs_as_before(server):
    """The library page's data names each of the catalog's own thumbnails with its version (?v=, its file's time in
    nanoseconds), and
    a cover asked for by the version it has is kept by the browser for good (private: never by a proxy on the way),
    answered so again when asked whether it changed, by a range and by a HEAD; one asked for without it, or by another,
    goes as before, asked about each time. The feeds, and catalog/data.js as the build wrote it, name it as they did,
    so a reading app gets what it got. A cover made again gets a new version once the catalog is."""
    client, root = server
    built = {it["id"]: it["c"] for it in read_data(root)["items"] if it.get("c", "").startswith("covers/")}
    shown = {it["id"]: it["c"] for it in page_data(client)["items"] if it["id"] in built}
    assert built and all("?" not in c for c in built.values())  # data.js on the disk: as the build wrote it
    for key, cover in built.items():
        assert shown[key] == f"{cover}?v={(root / 'catalog' / cover).stat().st_mtime_ns}"
    key, cover = next(iter(built.items()))
    feed = client.get("/opds/search", params={"q": next(it["t"] for it in read_data(root)["items"] if it["id"] == key)}).text
    assert f'href="/catalog/{cover}" type="image/webp"' in feed and "?v=" not in feed  # the reading apps' links
    kept = client.get(f"/catalog/{shown[key]}")
    assert kept.status_code == 200 and kept.content == (root / "catalog" / cover).read_bytes()
    assert kept.headers["cache-control"] == serve.KEPT_COVER == "private, max-age=31536000, immutable"
    modified = {"If-Modified-Since": kept.headers["last-modified"]}
    for answer, status in ((client.get(f"/catalog/{shown[key]}", headers=modified), 304),
                           (client.get(f"/catalog/{shown[key]}", headers={"Range": "bytes=0-9"}), 206),
                           (client.head(f"/catalog/{shown[key]}"), 200)):
        assert (answer.status_code, answer.headers["cache-control"]) == (status, serve.KEPT_COVER)
    for asked in (f"/catalog/{cover}", f"/catalog/{cover}?v=1", f"/catalog/{cover}?v=", f"/catalog/{cover}?again"):
        answer = client.get(asked)
        assert (answer.status_code, answer.headers["cache-control"]) == (200, "no-cache"), asked
    assert "cache-control" not in client.get(f"/catalog/{cover}", headers=modified).headers  # a 304 as before
    later = (root / "catalog" / cover).stat().st_mtime_ns + 1000  # a build makes it again, a moment later
    os.utime(root / "catalog" / cover, ns=(later, later))
    assert client.get(f"/catalog/{shown[key]}").headers["cache-control"] == "no-cache"  # not the version it has now
    write_data(root, read_data(root))
    again = next(it["c"] for it in page_data(client)["items"] if it["id"] == key)
    assert again == f"{cover}?v={later}" and client.get(f"/catalog/{again}").headers["cache-control"] == serve.KEPT_COVER


def test_a_cover_written_again_as_it_is_sent_is_never_kept_under_the_version_it_had(server, monkeypatch):
    """A cover asked for by its version is judged by the file as it is opened and read, never by a look at it before:
    one a build writes again in its place just before it's opened, or as it's read, goes as it was read, with its new
    time, and the browser asks about it again; it is never kept under the version it had."""
    client, root = server
    key, cover = next((it["id"], it["c"]) for it in read_data(root)["items"] if it.get("c", "").startswith("covers/"))
    path = root / "catalog" / cover
    asked = next(it["c"] for it in page_data(client)["items"] if it["id"] == key)
    was, new = path.read_bytes(), b"RIFF\0\0\0\0WEBP" + b"written again" * 50

    def write_again():  # in its place, as save_thumbnail writes one, a moment later
        with open(path, "r+b") as fh:
            fh.write(new)
            fh.truncate()
        os.utime(path, ns=(path.stat().st_mtime_ns + 1000,) * 2)

    real_open = Path.open

    def opened(p, *a, **k):  # written again between the server's look at the file and its opening
        if p == path:
            write_again()
        return real_open(p, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", opened)
        answer = client.get(f"/catalog/{asked}")
    assert answer.content == new and answer.headers["cache-control"] == "no-cache"
    path.write_bytes(was)
    asked = f"{cover}?v={path.stat().st_mtime_ns}"
    real_fstat, done = os.fstat, []

    def fstat(fd):  # written again once it's opened, as it's read
        found = real_fstat(fd)
        if found.st_ino == path.stat().st_ino and not done:
            done.append(True)
            write_again()
        return found

    with monkeypatch.context() as m:
        m.setattr(os, "fstat", fstat)
        answer = client.get(f"/catalog/{asked}")
    assert done and answer.content == new and answer.headers["cache-control"] == "no-cache"
    assert client.get(f"/catalog/{cover}?v={path.stat().st_mtime_ns}").headers["cache-control"] == serve.KEPT_COVER


def test_the_covers_are_listed_once_for_each_catalog_read_not_for_each_edit(tmp_path, monkeypatch):
    """The thumbnails' versions come from one listing of catalog/covers for each data.js the server reads: a tag or a
    correction makes the page's data again without looking at the covers again. A cover whose file isn't there keeps
    its address as the build wrote it."""
    from hylly import curation

    write_data(tmp_path, {"generated": "x", "items": [
        {"id": f"f:{n}", "t": f"Moonwake {n}", "k": "book", "f": [[f"a/Moonwake {n}.epub", "epub", 1]],
         "c": f"covers/{n:016x}.webp"} for n in range(3)]})
    (tmp_path / "catalog" / "covers").mkdir()
    for n in range(2):
        (tmp_path / "catalog" / "covers" / f"{n:016x}.webp").write_bytes(b"RIFF")
    listed, real = [], os.scandir
    covers_folder = tmp_path / "catalog" / "covers"  # only its listings count: the database lists its migrations too
    monkeypatch.setattr(os, "scandir", lambda path=".": (Path(path) == covers_folder and listed.append("covers")) or real(path))
    cat = Catalog(tmp_path)

    def covers():
        shown = json.loads(cat.page_script()[1].decode().removeprefix("window.CATALOG = ").removesuffix(";\n"))
        return [it["c"] for it in shown["items"]]

    first = covers()
    assert [c.partition("?v=")[2].isdigit() for c in first] == [True, True, False] and first[2] == f"covers/{2:016x}.webp"
    curation.change(tmp_path, "add", "Knitting", cat.get(), by="owen")
    assert covers() == first and listed == ["covers"]
    assert cat.for_page(cat.get())[0]["c"] == first[0]  # a correction's answer names it as the page's data does
    write_data(tmp_path, read_data(tmp_path))
    covers()
    assert listed == ["covers", "covers"]  # a new catalog: listed again


def test_a_correction_s_answer_is_read_under_the_catalog_s_lock(tmp_path, monkeypatch):
    write_data(tmp_path, {"generated": "x", "items": [{"id": "f:1", "t": "Moonwake", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    cat = Catalog(tmp_path)
    held = []
    real = page_item
    monkeypatch.setattr("hylly.library.page_item", lambda it: held.append(cat._lock.locked()) or real(it))
    assert [t["t"] for t in cat.for_page(cat.get())] == ["Moonwake"] and held == [True]


@pytest.mark.parametrize("failing", ["revision", "load"])
def test_a_database_error_while_the_catalog_is_read_again_leaves_nothing_half_laid(tmp_path, monkeypatch, failing):
    """A catalog read again whose tags and corrections can't be laid over it just then, by any error (an SQLite
    one too, not only a share that drops out), is laid again by the next request: no title is left without its words,
    tags and corrections, and a search finds it. data.js, read whole, isn't read again for each request while the
    database keeps failing."""
    import sqlite3

    from hylly import curation

    write_data(tmp_path, {"generated": "x", "items": [{"id": "f:1", "t": "Moonwake", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    cat = Catalog(tmp_path)
    curation.change(tmp_path, "add", "Knitting", cat.get(), by="owen")
    assert cat.get()[0]["tg"] == ["Knitting"]
    write_data(tmp_path, {"generated": "y", "items": [{"id": "f:1", "t": "Lantern Wolf", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    real, real_read, reads = getattr(curation, failing), Path.read_text, []

    def locked(root):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(serve.curation, failing, locked)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (reads.append(self) if self == cat.path else None) or real_read(self, *a, **k))
    for _ in range(5):
        with pytest.raises(sqlite3.Error):
            cat.get()
    assert reads == [cat.path]  # read once, and kept while the database fails
    monkeypatch.setattr(serve.curation, failing, real)
    assert [it["t"] for it in cat.search("lantern")] == ["Lantern Wolf"]
    assert cat.get()[0]["tg"] == ["Knitting"] and "_w" in cat.get()[0] and reads == [cat.path]


def test_a_request_keeps_the_titles_it_was_given_while_a_new_edit_is_laid_over_them(tmp_path):
    """An edit is laid over copies of the titles, put in place whole: a request still going through the list it was
    given, a feed or the page's data, finds each title as it was, never one changed half way."""
    from hylly import curation

    write_data(tmp_path, {"generated": "x", "items": [{"id": "f:1", "t": "Moonwake", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    cat = Catalog(tmp_path)
    given = cat.get()
    title = given[0]
    curation.change(tmp_path, "add", "Knitting", given, by="owen")
    now = cat.get()
    assert now is not given and now[0]["tg"] == ["Knitting"] and [it["t"] for it in cat.search("knitting")] == ["Moonwake"]
    assert given[0] is title and "tg" not in title and "knitting" not in title["_w"]


def test_each_library_s_feeds_name_its_own_sources_and_a_reload_puts_new_names_in_place_whole(tmp_path):
    """The names of a library's folders come with its catalog and are kept with it, so another library's never stand in
    for them; a reload puts its names in place whole, and a feed being made goes on with those it began with."""
    first, second = tmp_path / "first", tmp_path / "second"
    item = {"id": "f:1", "t": "Moonwake", "k": "book", "src": "mine", "f": [["mine/Moonwake.epub", "epub", 1]]}
    write_data(first, {"generated": "x", "sources": {"mine": "Lantern Folder"}, "items": [item]})
    write_data(second, {"generated": "x", "sources": {"mine": "Moonwake Shelf"}, "items": [item]})
    one, two = Catalog(first), Catalog(second)
    two.get()
    one.get()

    def titles(cat):
        return [e.find(f"{ATOM}title").text for e in entries(opds.answer(cat, "/opds")[2])]
    assert "Moonwake Shelf" in titles(two) and "Lantern Folder" not in titles(two) and "Lantern Folder" in titles(one)
    assert two.labels["humble"] == "Humble Bundle"  # the shops' own names too
    began = two.labels
    write_data(second, {"generated": "y", "sources": {"mine": "Moonwake Shelf", "more": "Ann's Folder"}, "items": [item]})
    two.get()
    assert two.labels["more"] == "Ann's Folder" and "more" not in began and began["mine"] == "Moonwake Shelf"
    assert "From: Moonwake Shelf" in opds.details(two.get()[0], item["f"][0], two.labels)


def test_a_data_js_that_can_t_be_read_whole_is_read_again_by_the_next_request(tmp_path, monkeypatch):
    """What stops data.js half read leaves nothing of it: the next request reads it again, whole."""
    write_data(tmp_path, {"generated": "x", "items": [{"id": "f:1", "t": "Moonwake", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    cat = Catalog(tmp_path)
    assert [it["t"] for it in cat.get()] == ["Moonwake"]
    write_data(tmp_path, {"generated": "y", "items": [{"id": "f:1", "t": "Lantern Wolf", "k": "book", "f": [["a/Moonwake.epub", "epub", 1]]}]})
    real = cover_sites

    def stalled(items):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr("hylly.library.cover_sites", stalled)  # after the titles are taken from it, before they're ready
    with pytest.raises(OSError):
        cat.get()
    monkeypatch.setattr("hylly.library.cover_sites", real)
    assert [it["t"] for it in cat.search("lantern")] == ["Lantern Wolf"] and "_k0" in cat.get()[0]


def test_tags_js_is_gone_and_the_page_and_its_help_come_from_the_program(server):
    """The page's own copy of the tags is no more: nothing writes catalog/tags.js, and a file of that name, or of the
    page's or its help's, left in the library by an earlier version, is never sent."""
    client, root = server
    for name in ("tags.js", "index.html", "help.html"):
        assert not (root / "catalog" / name).exists()
        (root / "catalog" / name).write_text("left by an earlier version", encoding="utf-8")
    assert client.get("/catalog/tags.js").status_code == 404
    for path in ("/catalog/index.html", "/catalog/help.html"):
        assert client.get(path).status_code == 200 and "left by" not in client.get(path).text, path
    assert client.get("/catalog/index.html").content == (Path(serve.__file__).parent / "catalog_page.html").read_bytes()


def test_the_library_page_goes_from_memory_however_it_s_asked_for(server, monkeypatch):
    """Hylly's own files go from memory, packed, plain or a part, so no client, however slow, holds one open while
    it's sent: on Windows a file held open can't be replaced. (data.js is made by the server: test_text_is_sent_packed.)"""
    client, _ = server
    page = Path(serve.__file__).parent / "catalog_page.html"
    whole, opened, held = page.read_bytes(), [], []
    real_open, real_setup = Path.open, serve.Handler.setup

    def open_(self, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)
        if self == page:
            opened.append(fh)
        return fh

    def setup(handler):  # each write to the client notes whether the page's file is open just then
        real_setup(handler)
        write = handler.wfile.write
        handler.wfile.write = lambda data: held.append(any(not fh.closed for fh in opened)) or write(data)
    monkeypatch.setattr(Path, "open", open_)
    monkeypatch.setattr(serve.Handler, "setup", setup)
    plain = client.get("/catalog/index.html", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers and plain.content == whole
    part = client.get("/catalog/index.html", headers={"Range": "bytes=10-99"})
    assert part.status_code == 206 and part.content == whole[10:100] and part.headers["content-range"] == f"bytes 10-99/{len(whole)}"
    packed = client.get("/catalog/index.html")  # httpx asks for gzip and unpacks it
    assert packed.headers["content-encoding"] == "gzip" and packed.content == whole
    assert held and not any(held)
    head = client.head("/catalog/index.html", headers={"Accept-Encoding": "identity"})
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == str(len(whole))


def test_series_in_volume_order(server):
    client, _ = server
    series = entries(client.get("/opds/series").text)
    rangers = [e for e in series if e.find(f"{ATOM}title").text == "Neon Rangers"][0]
    href = rangers.find(f"{ATOM}link").get("href")
    vols = [e.find(f"{ATOM}title").text for e in entries(client.get(href).text)]
    assert vols == ["Neon Rangers 002", "Neon Rangers 008"]


def test_the_series_feed_groups_by_the_keys_the_catalog_was_built_with(tmp_path):
    from hylly import rules

    items = [{"id": f"m{n}", "t": f"{s} {n}", "s": s, "n": str(n), "k": "comic", "src": "humble", "sk": "moonwake",
              "f": [[f"comics/{s} {n}.cbz", "cbz", 1]]} for n, s in enumerate(["Moonwake", "Moonwake", "Moonwake Omnibus"], 1)]
    write_data(tmp_path, {"generated": "x", "items": items})
    cat = Catalog(tmp_path)  # served without the rules the catalog was built with (hylly catalog --rules)
    feed = entries(opds.answer(cat, "/opds/series")[2])
    assert [(e.find(f"{ATOM}title").text, e.find(f"{ATOM}content").text) for e in feed] == [("Moonwake", "3 volumes")]
    assert len(entries(opds.answer(cat, "/opds/series/moonwake")[2])) == 3  # as the page groups them
    assert "omnibus" not in rules.current().series_fold
    write_data(tmp_path, {"generated": "x", "items": [{k: v for k, v in it.items() if k != "sk"} for it in items]})
    feed = entries(opds.answer(cat, "/opds/series")[2])  # a catalog from before the build made the keys: the rules in effect
    assert [(e.find(f"{ATOM}title").text, e.find(f"{ATOM}content").text) for e in feed] == [("Moonwake", "2 volumes")]


def test_names_in_any_script_are_found_and_have_their_own_series_in_the_feeds(tmp_path):
    named = [("Книга", "Тихая гавань"), ("Другая", "Тихая гавань"), ("月の庭 第1巻", "月の庭"), ("月の庭 第2巻", "月の庭"),
             ("Οδός 1", "ΟΔΟΣ"), ("Οδός 2", "Οδός"), ("चाँद 1", "चाँद का बगीचा"), ("चाँद 2", "चाँद का बगीचा")]
    items = [{"id": f"m{n}", "t": t, "s": s, "n": str(n), "k": "book", "src": "humble", "f": [[f"books/{n}.epub", "epub", 1]]}
             for n, (t, s) in enumerate(named, 1)]
    write_data(tmp_path, {"generated": "x", "items": items})
    cat = Catalog(tmp_path)
    assert [i["id"] for i in cat.search("КНИГА")] == ["m1"] and [i["id"] for i in cat.search("гава")] == ["m1", "m2"]
    assert [i["id"] for i in cat.search("月の")] == ["m3", "m4"] and [i["id"] for i in cat.search("οδος")] == ["m5", "m6"]
    feed = entries(opds.answer(cat, "/opds/series")[2])
    assert sorted(e.find(f"{ATOM}content").text for e in feed) == ["2 volumes"] * 4  # ΟΔΟΣ and Οδός are one
    for e in feed:
        href = e.find(f"{ATOM}link").get("href")
        assert href.isascii() and len(entries(opds.answer(cat, href)[2])) == 2  # the address is the key, as a link has it


def test_paging(server, monkeypatch):
    client, _ = server
    monkeypatch.setattr(opds, "PAGE", 2)
    first = ET.fromstring(client.get("/opds/src/humble").text)
    nxt = [link.get("href") for link in first.findall(f"{ATOM}link") if link.get("rel") == "next"]
    assert len(first.findall(f"{ATOM}entry")) == 2 and nxt == ["/opds/src/humble?page=1"]


def test_file_links_save_the_file(server):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, quote

    client, root = server
    book = root / "collections" / "Poetry" / "Ånd – Dikter.pdf"
    book.write_bytes(b"%PDF-1.4 x")
    url = "/" + quote(book.relative_to(root).as_posix())
    assert "content-disposition" not in client.get(url).headers  # shown in the browser, and apps fetch it as before
    saved = client.get(url, params={"dl": "1"})  # the library page's link: saved as a file
    assert saved.content == book.read_bytes() and saved.headers["content-type"] == "application/pdf"
    assert saved.headers["content-disposition"] == ("attachment; filename=\"_nd _ Dikter.pdf\"; "
                                                    "filename*=UTF-8''%C3%85nd%20%E2%80%93%20Dikter.pdf")
    assert client.get(url, params={"dl": "1"}, headers={"Range": "bytes=0-3"}).headers["content-disposition"].startswith("attachment;")
    # a name a NAS allows but Windows doesn't: nothing in it can end the header's quoted name
    odd = serve.Handler._saved(SimpleNamespace(_query=parse_qs("dl=1")), 'say "hi" \\ bye.pdf')["Content-Disposition"]
    assert odd == "attachment; filename=\"say _hi_ _ bye.pdf\"; filename*=UTF-8''say%20%22hi%22%20%5C%20bye.pdf"
    assert serve.Handler._saved(SimpleNamespace(_query=parse_qs("dl=0")), "a.pdf") == {}


def test_the_book_engine_is_served(server):
    client, _ = server
    view = client.get("/foliate/view.js")
    assert view.status_code == 200 and view.headers["content-type"].startswith("text/javascript") and "foliate-view" in view.text
    assert client.get("/foliate/vendor/zip.js").status_code == 200
    kept = client.get("/foliate/78914aef/vendor/zip.js")  # the page names the version: kept for good
    assert kept.status_code == 200 and "immutable" in kept.headers["cache-control"] and "no-cache" in view.headers["cache-control"]
    faces = client.get("/fonts/faces.css")
    assert faces.headers["content-type"].startswith("text/css") and "Literata" in faces.text
    font = client.get("/fonts/literata-normal-200-900-latin.woff2")
    assert font.headers["content-type"] == "font/woff2" and font.content[:4] == b"wOF2"
    for wrong in ("/foliate/LICENSE", "/foliate/nothing.js", "/foliate/../serve.py", "/foliate/..%2Fserve.py", "/foliate/..%5Cserve.py",
                  "/fonts/OFL-Literata.txt", "/fonts/..%2Ffoliate%2Fview.js", "/fonts/..%5Cserve.py"):
        assert client.get(wrong).status_code == 404, wrong


def test_the_licences_of_the_work_hylly_includes_are_served_as_plain_text_to_whoever_may_read_the_help(server, log_in):
    """/foliate/ and /fonts/ serve only scripts, styles and fonts, so the MIT, BSD and OFL notices of the parts Hylly
    brings never reached a browser. Each is served at /licences/ now, by its path beside the program, as plain text, to
    whoever may read the help that links to them, a borrower too: from the one table, and no other file of those
    folders. Like everything but the login page and what it needs, only after a login."""
    client, _ = server
    sources = Path(serve.__file__).parent
    with log_in(httpx.Client(base_url=client.base_url), "borrower") as borrower, \
            httpx.Client(base_url=client.base_url) as stranger:
        for rel in serve.LICENCES:
            for asker in (client, borrower):
                answer = asker.get(serve.LICENCE_PATH + rel)
                assert answer.status_code == 200 and answer.headers["content-type"] == "text/plain; charset=utf-8", rel
                assert answer.content == (sources / rel).read_bytes() and answer.headers["x-content-type-options"] == "nosniff"
            assert stranger.get(serve.LICENCE_PATH + rel).status_code == 401, rel
        head = borrower.head("/licences/fonts/OFL-Literata.txt")
        assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) > 1000
        assert "SIL OPEN FONT LICENSE" in borrower.get("/licences/fonts/OFL-AtkinsonHyperlegible.txt").text.upper()
        for wrong in ("/licences/", "/licences/foliate/README.md", "/licences/foliate/view.js", "/licences/fonts/faces.css",
                      "/licences/fonts/README.md", "/licences/fonts/literata-normal-200-900-latin.woff2", "/licences/LICENSE",
                      "/licences/serve.py", "/licences/Foliate/LICENSE", "/licences/foliate/LICENSE/",
                      "/licences//foliate/LICENSE", "/licences/foliate%5CLICENSE", "/licences/fonts%2F..%2Fserve.py"):
            assert client.get(wrong).status_code == 404, wrong
            assert borrower.get(wrong).status_code == 404, wrong
    assert client.get("/foliate/LICENSE").status_code == 404  # the reader's own route still serves only its scripts


def test_the_licences_served_are_those_the_notices_name_and_the_package_carries():
    """The licences are one table in code (serve.LICENCES). Each file in it is there; each part the first table of
    THIRD-PARTY-NOTICES.md names has its row, by its licence file's name; and every licence file the package takes in
    beside the program (pyproject.toml's license-files) is in it."""
    import tomllib

    repo = Path(__file__).resolve().parents[1]
    sources = repo / "src" / "hylly"
    assert [rel for rel in serve.LICENCES if not (sources / rel).is_file()] == []
    assert len(set(serve.LICENCES)) == len(serve.LICENCES)
    notices = (repo / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    included = notices[notices.index("## Included"):notices.index("## Python packages")]
    rows = [line for line in included.splitlines() if line.startswith("| [")]
    named = [re.findall(r"\(`src/hylly/([^`]+)`\)", row) for row in rows]
    assert len(rows) >= 7 and all(len(files) == 1 for files in named), rows
    assert sorted(files[0] for files in named) == sorted(serve.LICENCES)
    globs = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["license-files"]
    packaged = {p.relative_to(sources).as_posix() for g in globs for p in repo.glob(g) if p.is_relative_to(sources)}
    assert packaged == set(serve.LICENCES)


def test_the_source_link_on_every_page_is_the_one_source_url_hylly_s_own_as_it_comes(tmp_path, log_in):
    """Once no page offered Hylly's source, as the AGPL asks of a server run for others. The login page, the one-time
    link's page, the library page's account menu (from /api/session) and the help, the owner's and everyone else's,
    have [server] source_url, read as the server starts: Hylly's own as it comes, a changed version's own when set."""
    import html

    from hylly import PROJECT_URL, access, paths, settings

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    changed = "https://code.example.org/lantern-wolf/hylly?tab=source&ref=moonwake"
    for written, wanted in (("", PROJECT_URL), (f'[server]\nsource_url = "{changed}"\n', changed)):
        paths.settings_file().write_text(written, encoding="utf-8")
        settings.reload()  # as a server reads them at its start
        srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            with log_in(httpx.Client(base_url=base)) as owner, log_in(httpx.Client(base_url=base), "reader") as reader, \
                    httpx.Client(base_url=base) as stranger:
                session = owner.get("/api/session").json()
                helps = [asker.get("/catalog/help.html").text for asker in (owner, reader)]
                login = stranger.get("/login").text
                link = stranger.get("/login", params={"once": access.sign_in_link("reader")}).text
        finally:
            srv.shutdown()
            srv.server_close()
        link_to = f'<a href="{html.escape(wanted)}" rel="noopener noreferrer">Source</a>'
        assert session["source"] == wanted and link_to in login and link_to in link and "{{" not in login + link
        for text in helps:
            licences = re.search(r'<section id="licences">(.*?)</section>', text, re.S)[1]
            assert f'<a href="{html.escape(wanted)}" rel="noopener noreferrer">this Hylly\'s source</a>' in licences
    templates = [(Path(serve.__file__).parent / name).read_text(encoding="utf-8")
                 for name in ("login_page.html", "catalog_page.html", "help.html")]
    assert [name for name, text in zip(("login", "library", "help"), templates) if PROJECT_URL in text] == []  # none by hand
    assert '$("sourcelink").href = s.source;' in templates[1]
    assert 'if (/^https?:\\/\\//i.test(s.source || "")) {' in templates[1]  # any capitals: the line shows for every web address


def test_private_and_outside_paths_are_refused(server):
    client, _ = server
    assert client.get("/state/manifest.jsonl").status_code == 404
    assert client.get("/%2e%2e/%2e%2e/windows/win.ini").status_code == 404
    assert client.get("/opds/nonsense").status_code == 404


def test_ereader_rule(monkeypatch, tmp_path):
    ereader = Device()
    book = {"k": "book", "f": [["a.pdf", "pdf", 1000], ["a.mobi", "mobi", 900], ["a.epub", "epub", 800]]}
    assert ereader.suits(book) and ereader.best_file(book)[1] == "epub"  # EPUB first, then AZW3, then MOBI
    assert not ereader.suits({"k": "comic", "f": [["a.epub", "epub", 10]]})
    assert not ereader.suits({"k": "book", "f": [["a.pdf", "pdf", 10]]})  # PDF only
    assert not ereader.suits({**book, "dev": "tablet"})  # Humble says: best on a tablet
    assert not ereader.suits({"k": "book", "f": [["big.epub", "epub", 70 * 2 ** 20]]})
    assert not ereader.suits({**book, "ereader": "never"})  # your own say beats the rules
    comic = {"k": "comic", "ereader": "always", "f": [["a.cbz", "cbz", 10]]}
    assert ereader.suits(comic) and ereader.best_file(comic)[1] == "cbz"  # always: any file of the e-reader's formats
    assert not ereader.suits({"k": "book", "ereader": "always", "f": [["a.chm", "chm", 10]]})  # ...if there is one
    settings = tmp_path / "settings.toml"
    settings.write_text("[ereader_feed]\nformats = ['MOBI']\nmax_mb = 1\nkinds = ['book', 'rpg']\n", encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(settings))
    mine = ereader_feed()
    assert mine.best_file(book)[1] == "mobi" and mine.kinds == ("book", "rpg") and mine.max_mb == 1 and mine.skip_tablet


def test_the_e_reader_feed_offers_the_e_reader_s_formats_that_flow():
    """[ereader_feed] formats is the e-reader's list under [download] unless you give it one: its formats that flow to the
    screen, in its order, so a PDF, a DjVu or a comic file stays out as before, and a Kindle's order is said once. A
    title you put in the feed yourself offers any of the e-reader's, of any size, as always_formats did."""
    feed = Device()
    assert feed.offers() == ("epub", "azw3", "mobi", "azw", "prc", "fb2")  # books that flow: no plain text
    for ext in ("pdf", "djvu", "cbz", "cbr"):
        assert not feed.suits({"k": "book", "src": "humble", "f": [[f"humble/b/a.{ext}", ext, 10]]}), ext
    shelf = {"k": "book", "f": [["shelf/a.fb2", "fb2", 10], ["shelf/a.djvu", "djvu", 10]]}
    assert feed.best_file(shelf)[1] == "fb2" and feed.best_file({**shelf, "ereader": "always"})[1] == "fb2"
    big = {"k": "book", "ereader": "always", "f": [["humble/b/a.pdf", "pdf", 90 * 2 ** 20], ["humble/b/a.cbz", "cbz", 10]]}
    assert feed.best_file(big)[1] == "pdf"  # of any size, in the e-reader's order: the PDF before the CBZ
    both = {"k": "book", "f": [["a/b.epub", "epub", 10], ["a/b.azw3", "azw3", 10]]}
    kindle = Device(ereader_order=("azw3", "mobi", "epub"))
    assert feed.best_file(both)[1] == "epub" and kindle.best_file(both)[1] == "azw3"
    assert Device(formats=("epub",), ereader_order=("azw3",)).best_file(both)[1] == "epub"  # yours win


def test_ereader_feed(server):
    client, _ = server
    root = ET.fromstring(client.get("/ereader/opds").text)
    assert root.find(f"{ATOM}title").text.startswith("Library for e-readers")
    links = [link.get("href") for link in root.iter(f"{ATOM}link")]
    assert links and all(h.startswith("/ereader/opds") for h in links)  # every step stays in the e-reader feed
    acquisitions = lambda e: [link for link in e.findall(f"{ATOM}link") if link.get("rel") == "http://opds-spec.org/acquisition"]  # noqa: E731
    both = [e for e in entries(client.get("/opds/src/collections").text) if len(acquisitions(e)) == 2]  # EPUB and MOBI
    books = {e.find(f"{ATOM}id").text: e for e in entries(client.get("/ereader/opds/kind/book").text)}
    assert both and both[0].find(f"{ATOM}id").text in books and not entries(client.get("/ereader/opds/kind/comic").text)
    for e in books.values():
        assert [link.get("type") for link in acquisitions(e)] == ["application/epub+zip"]  # the one best file, not the MOBI too
    info = books[both[0].find(f"{ATOM}id").text].find(f"{ATOM}summary").text
    assert info.startswith("EPUB, ") and "From: Collections, Poetry" in info  # what KOReader shows as Book information
    assert not entries(client.get("/ereader/opds/search", params={"q": "ranger"}).text)
    assert entries(client.get("/opds/search", params={"q": "ranger"}).text)  # the whole library still has it


def test_ereader_feed_hands_out_the_book_inside_a_zip(tmp_path, log_in):
    import zipfile

    build_library(tmp_path)
    inner = tmp_path / "inner.epub"
    make_epub(inner, OPF.replace("The Long Dust", "Zipped Worlds"))
    pack = tmp_path / "collections" / "Packs" / "Ann Writer - Zipped Worlds.zip"  # how StoryBundle ships its books
    pack.parent.mkdir(parents=True)
    with zipfile.ZipFile(pack, "w") as z:
        z.write(inner, "Zipped Worlds/Zipped Worlds.epub")
        z.writestr("Zipped Worlds/Zipped Worlds.pdf", b"%PDF-1.4 not really")
    epub = inner.read_bytes()
    inner.unlink()
    catalog.build(tmp_path, log=lambda s: None)
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")) as client:
            found = entries(client.get("/ereader/opds/search", params={"q": "zipped worlds"}).text)
            acq = [link for link in found[0].findall(f"{ATOM}link") if link.get("rel") == "http://opds-spec.org/acquisition"]
            assert len(found) == 1 and len(acq) == 1 and acq[0].get("href").endswith(".zip/Zipped%20Worlds/Zipped%20Worlds.epub")
            got = client.get(acq[0].get("href"))
            assert got.status_code == 200 and got.content == epub and got.headers["content-type"] == "application/epub+zip"
            assert int(acq[0].get("length")) == len(epub)
            whole = entries(client.get("/opds/search", params={"q": "zipped worlds"}).text)[0]
            assert [link.get("type") for link in whole.findall(f"{ATOM}link") if "acquisition" in link.get("rel")] == ["application/zip"]
            base = acq[0].get("href").rsplit("/", 2)[0]
            assert client.get(base + "/nothing.epub").status_code == 404
            assert client.get("/state/x.zip/a.epub").status_code == 404
    finally:
        srv.shutdown()


def test_the_server_page_lists_the_questions_and_takes_the_owner_s_answer_with_its_key(tmp_path, log_in, monkeypatch):
    """The Server page, an owner's, shows the questions the downloads left, each with its number, its options and
    the rules' guess, and takes an answer as hylly decide does, by the owner's name, with the page's key, from this
    site's own pages only, and with what the question offered as the page showed it: one that offers other options by
    then is refused (409), with words the page shows; a reader, a borrower and a visitor who isn't logged in see none
    of it (403, it's the owner's; 401 without a login); anything that is no question's number or answer, huge,
    negative or not among the options, is refused (400,
    never 500), nothing changed. On a server whose records another computer keeps, the page shows no list."""
    from hylly import access, curation, db

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    moved = {"kind": "moved", "item": "71", "file": "0", "title": "Lantern Wolf", "options": list(curation.MOVED),
             "guess": 0, "held": True, "why": "lantern.pdf is gone from its folder"}
    files = [curation.file_option("art.pdf", "pdf", 50)]
    item = {"kind": "title", "item": "prints", "file": "", "title": "Moonwake Art Prints", "options": [*files, curation.LEAVE_OUT],
            "guess": 1, "held": False, "why": "its only file is no book by the rules, so it was left out"}
    curation.keep_questions(tmp_path, "drivethrurpg", [moved], lambda q: True)
    curation.keep_questions(tmp_path, "humble", [item], lambda q: True)
    q, art = curation.questions(tmp_path)
    access.add_user("bob", "battery staple", role="reader")
    access.add_user("bea", "battery staple", role="borrower")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    try:
        with log_in(httpx.Client(base_url=base), name="rory") as owner, log_in(httpx.Client(base_url=base), "reader") as reader, \
                log_in(httpx.Client(base_url=base), "borrower") as borrower, httpx.Client(base_url=base) as visitor:
            page = owner.get("/server")
            assert page.status_code == 200 and 'id="decide"' in page.text and "/server/questions" in page.text
            assert "Make it a title: " in page.text and 'id="decided"' in page.text and "data-offered" in page.text
            assert owner.get("/server/questions").json() == {"ok": True, "items": [q, art], "answered": []}
            answer = {"id": q["id"], "choice": 2, "offered": q["offered"]}
            for other, refused in ((reader, 403), (borrower, 403), (visitor, 401)):  # the owner's, not theirs
                assert other.get("/server/questions").status_code == refused
                assert other.post("/server/decide", json=answer, headers=key).status_code == refused
            assert owner.post("/server/decide", json=answer).status_code == 403  # without the page's key
            elsewhere = {**key, "Origin": "https://elsewhere.example"}
            assert owner.post("/server/decide", json=answer, headers=elsewhere).status_code == 403  # another site's page
            for wrong in ({"id": q["id"], "choice": 3}, {"id": 99, "choice": 1}, {"id": "x", "choice": 1}, {"id": True, "choice": 1},
                          {"id": 10**30, "choice": 1}, {"id": -1, "choice": 1}, {"id": 2**63, "choice": 1},
                          {"id": q["id"], "choice": "9" * 5000}, {"id": q["id"], "choice": "x" * 301}, {"id": q["id"], "choice": [2]},
                          {"id": q["id"], "choice": "nothing.pdf"}, {"id": q["id"], "choice": 2, "offered": "x" * 65}):
                refused = owner.post("/server/decide", json=wrong, headers=key)
                assert refused.status_code == 400 and not refused.json()["ok"] and refused.json()["error"], str(wrong)[:60]
            curation.keep_questions(tmp_path, "humble", [{**item, "options": [*files, curation.file_option("art_hq.pdf", "pdf", 120),
                                                                               curation.LEAVE_OUT], "guess": 2}], lambda x: True)
            changed = owner.post("/server/decide", json={"id": art["id"], "choice": 2, "offered": art["offered"]}, headers=key)
            assert changed.status_code == 409 and "offers other options now" in changed.json()["error"]  # was "leave it out"
            assert curation.load_lines(tmp_path) == []  # nothing changed
            done = owner.post("/server/decide", json=answer, headers={**key, "Origin": base})
            assert done.json() == {"ok": True, "field": "answers", "id": "d:71",
                                   "did": "never fetches it again: it stays where you moved it"}
            assert [x["id"] for x in owner.get("/server/questions").json()["items"]] == [art["id"]]
            with monkeypatch.context() as m:  # the records kept on another computer: no list here, they're its to answer
                m.setattr(curation, "questions", lambda root: (_ for _ in ()).throw(db.NotHere("kept on another computer")))
                assert owner.get("/server/questions").json() == {"ok": True, "items": [], "answered": [], "elsewhere": True}
    finally:
        srv.shutdown()
    last = curation.load_lines(tmp_path)[-1]
    assert (last["field"], last["by"], last["titles"][0]["id"]) == ("answers", "rory", "d:71")
    assert curation.answers(tmp_path)["d:71"]["moved:0"]["a"] == "moved"


def test_the_server_page_lists_the_answered_apart_and_takes_one_back_and_the_library_page_shows_the_owner_a_count(
        tmp_path, log_in):
    """The Server page's To decide shows the questions answered apart, each with a Take
    back (the same as hylly decide NUMBER auto: the owner's, with the page's key, from this site only); the library page
    gives the owner alone how many questions are open (a link to To decide) and each title's own, for its details; a
    librarian, a reader and a borrower get neither. Recent changes, the owner's, says an answer's edit in words."""
    from hylly import access, curation

    build_library(tmp_path)
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    (shelf / "a.xyz").write_bytes(b"x")
    moved = {"kind": "moved", "item": "7", "file": "0", "title": "Arcade Worlds", "options": list(curation.MOVED),
             "guess": 0, "held": False, "why": "its file is gone from its folder"}
    catalog.build(tmp_path, log=lambda s: None)
    curation.keep_questions(tmp_path, "storybundle", [moved], lambda q: True)
    files, arcade = curation.questions(tmp_path)
    curation.answer(tmp_path, files["id"], "keep", by="rory")
    access.add_user("lin", "battery staple", role="librarian")
    access.add_user("bob", "battery staple", role="reader")
    access.add_user("bea", "battery staple", role="borrower")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    try:
        with log_in(httpx.Client(base_url=base), name="rory") as owner, log_in(httpx.Client(base_url=base), "librarian") as lin, \
                log_in(httpx.Client(base_url=base), "reader") as reader, log_in(httpx.Client(base_url=base), "borrower") as bea:
            page = owner.get("/server").text
            assert 'id="answered"' in page and "Take back" in page
            said = owner.get("/server/questions").json()
            assert [q["id"] for q in said["items"]] == [arcade["id"]] and [q["id"] for q in said["answered"]] == [files["id"]]
            assert said["answered"][0]["answer"]["by"] == "rory" and said["answered"][0]["answer"]["option"] == 1
            session = owner.get("/api/session").json()
            assert session["toDecide"] == 1  # a count only; a title's own come with its details
            assert [x["id"] for x in owner.get("/api/questions?id=s:7").json()["items"]] == [arcade["id"]]
            for other in (lin, reader, bea):  # nobody but the owner sees the count, or a title's question
                assert "toDecide" not in other.get("/api/session").json()
            take = {"id": files["id"], "choice": "auto"}
            for other in (lin, reader, bea):
                assert other.post("/server/decide", json=take, headers=key).status_code == 403
            assert owner.post("/server/decide", json=take).status_code == 403  # without the page's key
            assert owner.post("/server/decide", json=take, headers={**key, "Origin": "https://elsewhere.example"}).status_code == 403
            done = owner.post("/server/decide", json=take, headers={**key, "Origin": base}).json()
            assert done["ok"] and done["took_back"] and done["id"] == ""
            said = owner.get("/server/questions").json()
            assert sorted(q["id"] for q in said["items"]) == sorted([files["id"], arcade["id"]]) and said["answered"] == []
            assert owner.post("/server/decide", json=take, headers=key).status_code == 400  # nothing to take back now
            assert owner.get("/api/session").json()["toDecide"] == 2
            curation.answer(tmp_path, arcade["id"], "2", by="rory")
            edits = owner.get("/api/edits").json()["edits"]
            assert edits[0]["said"] == "answered a file gone (0): moved by hand" and edits[0]["ids"] == ["s:7"]
            data = reader.get("/catalog/data.js").text  # the owner's answers travel to nobody's page
            assert '"s:7"' in data and "moved:0" not in data and '"answers"' not in data
            lib_page = owner.get("/catalog/index.html").text
            assert 'id="decidelink"' in lib_page and "/server#decide" in lib_page and "to decide" in lib_page
    finally:
        srv.shutdown()


def answers_now(root) -> dict:
    """Every title's answers to the catalog's questions, as the edits replay them."""
    from hylly import curation

    return {i: f["catalog"] for i, f in curation.load(root).fixed.items() if f.get("catalog")}


def test_the_answers_without_a_question_are_the_owner_s_and_their_undo_is_exact(tmp_path, log_in):
    """Not one book (copies) and Not this book (refused) are the owner's alone, as the format and
    the answers are: a librarian is refused (403) and nothing of the owner's answers reaches them. The toast's Undo of
    either undoes that one change (its update, key by key), never with auto, which took back the owner's other answers;
    Recent changes' Undo of such an edit on a merged card is as exact, the copy's id too. A title's answers as a dict
    only with undo; ids that aren't text, a 400 naming every field, never a 500."""
    from hylly import curation

    build_library(tmp_path)
    two_moonwakes(tmp_path)
    near_miss(tmp_path, "s:7", 0.9)  # Open Library's details used on Arcade Worlds
    catalog.build(tmp_path, log=lambda s: None)
    (q,) = [q for q in curation.questions(tmp_path) if q["kind"] == "same"]
    curation.answer(tmp_path, q["id"], "1", by="rory")  # the owner: the same book
    catalog.build(tmp_path, log=lambda s: None)
    (card,) = [i for i in Catalog(tmp_path).get() if i["t"] == "Moonwake Saga"]
    before = answers_now(tmp_path)
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with log_in(httpx.Client(base_url=base), name="rory") as owner, log_in(httpx.Client(base_url=base), "librarian") as lin:
            key, theirs = ({"X-Server-Key": who.get("/api/session").json()["key"], "Origin": base} for who in (owner, lin))
            for body in ({"field": "copies", "value": "apart", "ids": [card["id"]]}, {"field": "copies", "value": "auto", "ids": [card["id"]]},
                         {"field": "refused", "value": "openlibrary", "ids": ["s:7"]}, {"field": "refused", "value": "auto", "ids": ["s:7"]}):
                assert lin.post("/api/fix", json=body, headers=theirs).status_code == 403, body  # with their own key
            assert answers_now(tmp_path) == before and "toDecide" not in lin.get("/api/session").json()
            assert "same:" not in json.dumps(lin.get("/api/edits").json())  # nothing of the owner's answers
            apart = owner.post("/api/fix", json={"field": "copies", "value": "apart", "ids": [card["id"]]}, headers=key).json()
            assert apart["ok"] and apart["update"]["keys"] == [f"same:{'|'.join(sorted(curation.ids_of(card)))}"]
            for keys, was in [(apart["update"]["keys"], apart["update"]["was"])]:  # the toast's Undo, as the page sends it
                for value, ids in curation.undo_update(keys, was):
                    assert owner.post("/api/fix", json={"field": "catalog", "value": value, "ids": ids, "undo": True, "update": True},
                                      headers=key).json()["ok"]
            assert answers_now(tmp_path) == before  # the owner's "same book" is as it was: never auto
            owner.post("/api/fix", json={"field": "copies", "value": "apart", "ids": [card["id"]]}, headers=key)
            e = owner.get("/api/edits").json()["edits"][0]  # Recent changes' Undo: key by key, on each id it names
            assert (e["field"], e["op"], sorted(e["ids"])) == ("catalog", "update", sorted(curation.ids_of(card)))
            for value, ids in curation.undo_update(list(e["tag"]), e["was"]):
                owner.post("/api/fix", json={"field": "catalog", "value": value, "ids": ids, "undo": True, "update": True}, headers=key)
            assert answers_now(tmp_path) == before
            refused = owner.post("/api/fix", json={"field": "refused", "value": "openlibrary", "ids": ["s:7"]}, headers=key).json()
            assert refused["named"] == "Open Library"  # the toast names it as people know it
            for value, ids in curation.undo_update(refused["update"]["keys"], refused["update"]["was"]):
                owner.post("/api/fix", json={"field": "catalog", "value": value, "ids": ids, "undo": True, "update": True}, headers=key)
            assert answers_now(tmp_path) == before
            crafted = {"same:s:7|h:novel": {"a": "same"}}  # what no question offered: only an undo's
            for body in ({"field": "catalog", "value": crafted, "ids": ["s:7"]}, {"field": "answers", "value": "keep", "ids": ["s:7"]},
                         {"field": "catalog", "value": crafted, "ids": ["s:7"], "update": True},
                         {"field": "copies", "value": "apart", "ids": [["s:7"]]}, {"field": "copies", "value": "apart", "ids": [{"id": "s:7"}]},
                         {"field": "copies", "value": "apart", "ids": "s:7"}, {"field": "kind", "value": "tech", "ids": [None, 3]}):
                refused = owner.post("/api/fix", json=body, headers=key)
                assert refused.status_code == 400, body
            said = refused.json()["error"]
            assert all(name in said for name in ("kind", "ereader", "title", "authors", "format", "file", "answers", "catalog",
                                                  "copies", "refused"))
            assert owner.post("/api/tags", json={"op": "add", "tag": "Keep", "ids": [["s:7"]]}, headers=key).status_code == 400
            assert owner.post("/api/tags", json={"op": "add", "tag": "Keep", "ids": [{"a": 1}]}, headers=key).status_code == 400
            assert answers_now(tmp_path) == before
            page = owner.get("/catalog/index.html").text
            assert "EDIT.manage && (it.ids" in page and 'EDIT.manage ? ` <button class="linkbtn" data-refuse' in page
    finally:
        srv.shutdown()


def test_the_owner_gets_a_count_and_a_title_s_questions_with_its_details_and_answers_a_near_match_there(tmp_path, log_in):
    """The session gives the owner a count only, the quiet near matches left out, as the Server page's list
    leaves them out; a title's own questions come with its details, by an owner's request for that title (403 for
    anyone else), a near match among them, which the details answer (Use it, /api/decide, the page's key) and take back."""
    from hylly import access

    build_library(tmp_path)
    near_miss(tmp_path, "s:7", 0.7)
    (tmp_path / "shelf").mkdir()
    (tmp_path / "shelf" / "a.xyz").write_bytes(b"x")
    catalog.build(tmp_path, log=lambda s: None)
    access.add_user("bob", "battery staple", role="reader")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with log_in(httpx.Client(base_url=base), name="rory") as owner, log_in(httpx.Client(base_url=base), "librarian") as lin, \
                log_in(httpx.Client(base_url=base), "reader") as bob:
            key = {"X-Server-Key": owner.get("/api/session").json()["key"], "Origin": base}  # each editor's own
            assert owner.get("/api/session").json()["toDecide"] == 1  # the files question; the near match is quiet
            assert [x["kind"] for x in owner.get("/server/questions").json()["items"]] == ["files"]
            (match,) = owner.get("/api/questions?id=s:7").json()["items"]
            assert match["kind"] == "match" and match["options"][0]["source"] == "openlibrary"
            for other in (lin, bob):
                assert other.get("/api/questions?id=s:7").status_code == 403
                own = {"X-Server-Key": other.get("/api/session").json()["key"] or "none", "Origin": base}
                assert other.post("/api/decide", json={"id": match["id"], "choice": 1}, headers=own).status_code == 403
            assert owner.post("/api/decide", json={"id": match["id"], "choice": 1, "offered": match["offered"]}).status_code == 403
            used = owner.post("/api/decide", json={"id": match["id"], "choice": 1, "offered": match["offered"]}, headers=key).json()
            assert used["ok"] and used["did"] == "uses Open Library's details for it from the next hylly catalog"
            assert owner.get("/api/questions?id=s:7").json()["items"] == []
            assert owner.post("/api/decide", json={"id": match["id"], "choice": "auto"}, headers=key).json()["took_back"]
            assert [x["id"] for x in owner.get("/api/questions?id=s:7").json()["items"]] == [match["id"]]
            owner.post("/api/decide", json={"id": match["id"], "choice": 1, "offered": match["offered"]}, headers=key)
            e = owner.get("/api/edits").json()["edits"][0]  # Recent changes' Undo of it: another way than its question
            undo = {id_: ("auto" if had is None else had) for id_, had in e["was"].items()}
            assert owner.post("/api/fix", json={"field": e["field"], "value": undo["s:7"], "ids": ["s:7"], "undo": True},
                              headers=key).json()["ok"]
            # the title's details ask it again at once, before any build
            assert [x["id"] for x in owner.get("/api/questions?id=s:7").json()["items"]] == [match["id"]]
            assert owner.get("/api/questions?id=nosuch").json()["items"] == []
    finally:
        srv.shutdown()


def test_the_server_page_draws_its_lists_anew_even_when_empty():
    """An answered question leaves the open list on the Server page, the last one too: the lists are drawn anew each
    time (draw), as put() leaves alone what it drew last, which a list just emptied matches."""
    page = (Path(serve.__file__).parent / "server_page.html").read_text(encoding="utf-8")
    assert 'draw("questions", shown)' in page and 'draw("answered",' in page
    assert 'put("questions"' not in page and 'put("answered"' not in page
    assert "function draw(id, markup) { $(id).innerHTML = markup; $(id).dataset.was = markup; }" in page


def test_the_server_page_is_told_whether_it_is_looked_at_on_the_server_s_own_computer_and_by_which_device(library, serving):
    """The Server page shows its address on this computer only to a browser on the server's own computer: the status
    says so (here), by the request's sender as the server tells it (behind a trusted proxy, the address it names). The
    devices list marks the device looking (you) and the server's own computer (server), apart."""
    _, client = serving(library)
    tablet = {"X-Forwarded-For": "192.0.2.44"}  # the owner's tablet, through a proxy on the server's computer
    client.get("/opds")  # a use worth counting, as the page's own checks aren't
    on_it = client.get("/server/status").json()
    assert on_it["here"] is True and on_it["pc"].startswith("http://127.0.0.1:")
    assert [(c["ip"], c["you"], c["server"]) for c in on_it["clients"]] == [("127.0.0.1", True, True)]
    client.get("/opds", headers=tablet)
    elsewhere = client.get("/server/status", headers=tablet)
    said = elsewhere.json()
    assert elsewhere.status_code == 200 and said["here"] is False and said["pc"] == on_it["pc"]
    assert {c["ip"]: (c["you"], c["server"]) for c in said["clients"]} == {"192.0.2.44": (True, False), "127.0.0.1": (False, True)}


# ---- books' own fonts: [reader] book_fonts, in the page's policy (once among the PDF worker's tests) -----------------

def policy_font_src(policy):
    return next(part.strip() for part in policy.split(";") if part.strip().startswith("font-src"))


def test_a_books_fonts_are_refused_when_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    settings.reload()
    assert policy_font_src(serve.page_policy("", (), True)) == "font-src 'self' data: blob:"
    assert policy_font_src(serve.page_policy("", (), False)) == "font-src 'self'"
    assert settings.get("reader")["book_fonts"] is True


def test_book_fonts_is_true_or_false(tmp_path, monkeypatch):
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "settings"))
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text('[reader]\nbook_fonts = "no"\n', encoding="utf-8")
    settings.reload()
    with pytest.raises(SystemExit, match="true or false"):
        settings.get("reader")
    paths.settings_file().write_text("[reader]\nbook_fonts = false\n", encoding="utf-8")
    settings.reload()
    assert settings.get("reader")["book_fonts"] is False
    assert policy_font_src(serve.page_headers("")["Content-Security-Policy"]) == "font-src 'self'"
    settings.reload()



def test_the_server_page_compares_a_question_s_files_side_by_side_for_the_owner(tmp_path, log_in, monkeypatch):
    """/server/compare, the owner's: for a same question both copies, each with what the reader shows of it (a comic as
    pages, an EPUB as a book) and that file's name, format, size, date and cover, its pages then drawn by the reader's
    own addresses; for a which-file question the file on the disk, and the shop's listing of the one that isn't; nothing
    for a question of another kind; 404 for one that isn't open, 400 for no number; a reader and a visitor get none."""
    from hylly import access, curation

    build_library(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    two = [{"word": "one book", "answer": "same"}, {"word": "two copies", "answer": "apart"}]
    same = {"id": 7, "kind": "same", "title": "Lantern Wolf Vol. 1", "options": two, "guess": 1,
            "subjects": [{"id": "h:lwolf1", "name": "lw1.cbz"}, {"id": "h:novel", "name": "novel.epub"}]}
    which = {"id": 8, "kind": "which-file", "title": "Lantern Wolf Vol. 2", "catalog_id": "h:lwolf2", "guess": 0,
             "options": [{"file": "lw2.cbz", "format": "cbz", "size": 100}, {"file": "lw2.pdf", "format": "pdf", "size": 300, "label": "PDF"}]}
    other = {"id": 9, "kind": "title", "title": "Moonwake Art Prints", "options": [], "guess": 0}
    monkeypatch.setattr(curation, "questions", lambda root, **k: [same, which, other])
    access.add_user("bob", "battery staple", role="reader")
    srv, _ = serve.make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with log_in(httpx.Client(base_url=base), name="rory") as owner, log_in(httpx.Client(base_url=base), "reader") as reader, \
                httpx.Client(base_url=base) as visitor:
            got = owner.get("/server/compare", params={"q": 7}).json()
            comic, book = got["sides"]
            assert got["ok"] and got["listed"] == [] and (comic["id"], book["id"]) == ("h:lwolf1", "h:novel")
            assert (comic["reads"], comic["format"], book["reads"], book["format"]) == ("page", "cbz", "book", "epub")
            assert comic["name"] == "lw1.cbz" and comic["size"] > 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", comic["date"])
            assert (comic["language"], book["language"], comic["files"], book["files"]) == ("", "English", 1, 1)  # its OPF's
            assert comic["cover"] == "" or (comic["cover"].startswith("/catalog/covers/") and owner.get(comic["cover"]).status_code == 200)
            pages = owner.get("/api/pages", params={"id": comic["id"]}).json()
            assert pages["ok"] and pages["pages"] >= 1
            assert owner.get("/api/page", params={"id": comic["id"], "n": 0, "w": 600, "v": pages["v"]}).status_code == 200
            got = owner.get("/server/compare", params={"q": 8}).json()
            (on_disk,), (listed,) = got["sides"], got["listed"]
            assert on_disk["name"] == "lw2.cbz" and listed == {"file": "lw2.pdf", "format": "pdf", "size": 300, "label": "PDF"}
            assert owner.get("/server/compare", params={"q": 9}).json() == {"ok": True, "sides": [], "listed": [], "unshown": 0}
            for subjects, unshown in (([{"id": "h:lwolf1"}, {"id": "h:lwolf1"}], 1), ([{"id": "h:lwolf1"}, {"id": "gone"}], 1)):
                same["subjects"] = subjects  # merged with the other since, or gone: one side, and the other said
                got = owner.get("/server/compare", params={"q": 7}).json()
                assert [x["id"] for x in got["sides"]] == ["h:lwolf1"] and got["unshown"] == unshown
            assert owner.get("/server/compare", params={"q": 99}).status_code == 404
            for wrong in ({}, {"q": "x"}, {"q": "9" * 40}):
                assert owner.get("/server/compare", params=wrong).status_code in (400, 404)
            assert owner.get("/server/compare").status_code == 400
            assert reader.get("/server/compare", params={"q": 7}).status_code == 403
            assert visitor.get("/server/compare", params={"q": 7}).status_code == 401
    finally:
        srv.shutdown()


def test_a_file_a_question_offers_is_found_on_the_disk_by_the_name_it_was_saved_under():
    """A shop's name made safe for the disk (paths.safe_filename: a : as _), a second file of one name as "(2)" (plan.
    free_name), and of two of one name, the one of its size; one the title hasn't got: None."""
    entries = [["humble/B/T/Lantern_%20Wolf.pdf", "pdf", 50], ["humble/B/T/art.pdf", "pdf", 10], ["humble/B/T/art%20(2).pdf", "pdf", 120]]
    on = serve.Handler._on_disk
    assert on({"file": "Lantern: Wolf.pdf", "size": 999}, entries) is entries[0]  # its listing's size gone stale: by name
    assert on({"file": "art.pdf", "size": 120}, entries) is entries[2] and on({"file": "art.pdf", "size": 10}, entries) is entries[1]
    assert on({"file": "ART.PDF", "size": 1}, entries[:2]) is entries[1]
    assert on({"file": "other.pdf", "size": 10}, entries) is None and on({"file": "art", "size": 1}, entries) is None


# ---- To decide's removals into _Removed, Put back and Empty -----------------------------------------------------------

@pytest.fixture
def builds(monkeypatch):
    """The catalog builds the server wants after a removal or a put back (periodic.Builds), by their library: none is
    run, and each is taken for one started at once."""
    from hylly import periodic

    started = []
    monkeypatch.setattr(periodic.Builds, "want", lambda self, root, log: started.append(Path(root)) or True)
    return started


def moonwakes(root) -> list[dict]:
    return [i for i in Catalog(root).get() if i["t"] == "Moonwake Saga"]


def same_questions(root) -> list[dict]:
    from hylly import curation

    return [q for q in curation.questions(root) if q["kind"] == "same"]


def removals(root) -> list[tuple]:
    from hylly import curation

    with curation.store(root).read() as c:
        return [tuple(row) for row in c.execute("SELECT path, held, shop, state FROM removed ORDER BY seq")]


def test_keep_left_only_moves_the_right_copy_s_files_into_removed_at_their_places_and_put_back_brings_them_back(
        library, serving, log_in, builds):
    """Keep left only (POST /server/remove, the owner's, with the page's key) moves every file of the right copy, as
    /server/compare shows the two, into _Removed at its own place below the library, the left copy's as they are, keeps
    the removal in the database and starts a catalog build, saying so; that build has one card. Refused, nothing moved:
    what is no question's number or keep (400), no open question about two copies of that number (404), one offering
    other options now (409), without the page's key or from another site's page (403), anyone but the owner (403, 401
    without a login). Pressed again, it's settled: no such question is open (404). Removed lists the file, its place, size and when;
    Put back moves it back and starts a build, which asks the question again, and is refused once it's back (409). Keep
    both is the answer two books, as any answer: two cards, not asked again."""
    from hylly import curation

    two_moonwakes(library)
    catalog.build(library, log=lambda s: None)
    (q,) = same_questions(library)
    gone = {"kind": "moved", "item": "71", "file": "0", "title": "Lantern Wolf", "options": list(curation.MOVED), "guess": 0}
    curation.keep_questions(library, "drivethrurpg", [gone], lambda x: True)  # a question of another kind
    other = next(x for x in curation.questions(library) if x["kind"] == "moved")
    srv, owner = serving(library)
    base = f"http://127.0.0.1:{srv.server_port}"
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    left, right = owner.get("/server/compare", params={"q": q["id"]}).json()["sides"]
    cards = {i["id"]: i for i in Catalog(library).get()}
    kept, went = (re.sub("%20", " ", cards[s["id"]]["f"][0][0]) for s in (left, right))
    ask = {"id": q["id"], "keep": "left", "offered": q["offered"]}
    for wrong in ({}, {**ask, "keep": "middle"}, {**ask, "keep": ["left"]}, {**ask, "id": str(q["id"])}, {**ask, "id": True},
                  {**ask, "id": 2**63}, {**ask, "offered": None}, {**ask, "offered": "x" * 65}):
        refused = owner.post("/server/remove", json=wrong, headers=key)
        assert refused.status_code == 400 and refused.json()["error"], wrong
    for number in (99999, other["id"]):
        assert owner.post("/server/remove", json={**ask, "id": number}, headers=key).status_code == 404
    stale = owner.post("/server/remove", json={**ask, "offered": "0123456789ab"}, headers=key)
    assert stale.status_code == 409 and "offers other options now" in stale.json()["error"]
    assert owner.post("/server/remove", json=ask).status_code == 403  # without the page's key
    assert owner.post("/server/remove", json=ask, headers={**key, "Origin": "https://elsewhere.example"}).status_code == 403
    with log_in(httpx.Client(base_url=base), "librarian") as librarian, httpx.Client(base_url=base) as visitor:
        for path, body in (("/server/remove", ask), ("/server/put-back", {"held": went}), ("/server/empty-removed", {"digest": "0" * 64})):
            assert librarian.post(path, json=body, headers=key).status_code == 403
            assert visitor.post(path, json=body, headers=key).status_code == 401
        assert librarian.get("/server/removed").status_code == 403 and visitor.get("/server/removed").status_code == 401
    assert (library / went).is_file() and not (library / "_Removed").exists() and builds == [] and removals(library) == []
    size = (library / went).stat().st_size
    done = owner.post("/server/remove", json=ask, headers={**key, "Origin": base})
    assert done.json() == {"ok": True, "removed": 1, "build": True,
                           "did": f"Moved 1 file ({size_words(size)}) into _Removed, where Put back, under Removed, brings it back. "
                                  "A catalog build has started: the card leaves the library page once it's done."}
    assert (library / "_Removed" / went).is_file() and not (library / went).exists() and (library / kept).is_file()
    assert builds == [library] and removals(library) == [(went, went, "", "removed")]
    again = owner.post("/server/remove", json=ask, headers=key)
    assert again.status_code == 404  # settled: a copy's file has left the library, so the question isn't offered again
    assert ask["id"] not in [q["id"] for q in owner.get("/server/questions").json()["items"]]  # nor listed, the build or not
    listed = owner.get("/server/removed").json()
    (shown,) = listed["files"]
    assert (listed["ok"], listed["folder"], listed["count"], listed["bytes"], listed["more"]) == (True, "_Removed", 1, size, False)
    assert (shown["held"], shown["path"], shown["size"], shown["t"]) == (went, went, size, "Moonwake Saga") and shown["at"]
    catalog.build(library, log=lambda s: None)  # as the build started would
    assert len(moonwakes(library)) == 1 and same_questions(library) == []
    for wrong in ({}, {"held": ""}, {"held": ["x"]}, {"held": "x" * 5000}):
        assert owner.post("/server/put-back", json=wrong, headers=key).status_code == 400
    back = owner.post("/server/put-back", json={"held": went}, headers=key)
    assert back.json() == {"ok": True, "path": went, "build": True, "did": f"Put {went} back in its place. A catalog build has "
                                                                           "started: the card comes back to the library page once it's done."}
    assert (library / went).stat().st_size == size and not (library / "_Removed").exists() and builds == [library] * 2
    assert owner.get("/server/removed").json()["count"] == 0 and removals(library) == [(went, went, "", "back")]
    twice = owner.post("/server/put-back", json={"held": went}, headers=key)
    assert twice.status_code == 409 and "isn't a file in _Removed" in twice.json()["error"]
    catalog.build(library, log=lambda s: None)
    (q,) = same_questions(library)
    assert len(moonwakes(library)) == 2
    both = next(n for n, o in enumerate(q["options"], 1) if o["answer"] == "apart")  # Keep both
    assert owner.post("/server/decide", json={"id": q["id"], "choice": str(both), "offered": q["offered"]}, headers=key).json()["ok"]
    catalog.build(library, log=lambda s: None)
    assert len(moonwakes(library)) == 2 and same_questions(library) == []


def test_a_shop_s_removed_file_is_never_fetched_again_verify_counts_no_removed_file_missing_and_empty_is_for_good(
        library, serving, builds, monkeypatch, capsys):
    """Keep right only of two copies whose left is a shop's file (StoryBundle's) gives its title the answer that it was
    moved, by the owner's name, as hylly decide's "I moved it by hand" does, so no download fetches it again; hylly
    verify counts it removed, not missing. Put back takes that answer back. Remove both moves both copies' files, a
    Humble one too; Empty _Removed is refused for counts other than what's there, then deletes every file there for good,
    and _Removed with them, the removals kept as emptied: verify still counts none of them missing, and the shop's title
    keeps its answer."""
    from hylly import cli, curation

    two = list(curation.SAME)
    q = {"id": 7, "kind": "same", "title": "Arcade Worlds", "options": two, "guess": 1, "offered": curation.fingerprint(two),
         "subjects": [{"id": "s:7"}, {"id": "h:novel"}]}
    monkeypatch.setattr(curation, "questions", lambda root, **k: [] if k else [q])
    srv, owner = serving(library)
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    shop = "storybundle/The Lantern Video Game Bundle/Arcade Worlds/Arcade_Worlds_-_Ann_Writer.epub"
    novel = re.sub("%20", " ", next(i for i in Catalog(library).get() if i["id"] == "h:novel")["f"][0][0])
    ask = {"id": 7, "keep": "right", "offered": q["offered"]}

    def verify() -> str:
        capsys.readouterr()
        cli.main(["verify", "--library", str(library)])
        return capsys.readouterr().out

    assert owner.post("/server/remove", json=ask, headers=key).json()["ok"]
    assert (library / "_Removed" / shop).is_file() and not (library / shop).exists() and (library / novel).is_file()
    assert curation.answers(library)["s:7"] == {"moved:0": {"a": "moved"}} and curation.moved(curation.answers(library)["s:7"], 0) == "moved"
    last = curation.load_lines(library)[-1]
    assert (last["field"], last["op"], last["by"], last["titles"][0]["id"]) == ("answers", "update", "owner", "s:7")
    assert removals(library) == [(shop, shop, "storybundle", "removed")]
    said = verify()
    assert ": 0 problems.\n1 file you removed (in _Removed, emptied from it, or moved by hand as you answered) is no problem." in said
    assert owner.post("/server/put-back", json={"held": shop}, headers=key).json()["ok"]
    assert "s:7" not in curation.answers(library) and (library / shop).is_file()
    assert ": 0 problems.\n" in (said := verify()) and "you removed" not in said
    done = owner.post("/server/remove", json={**ask, "keep": "none"}, headers=key).json()
    assert done["ok"] and done["removed"] >= 2 and not (library / shop).exists() and not (library / novel).exists()
    listed = owner.get("/server/removed").json()
    count, size, digest = listed["count"], listed["bytes"], listed["digest"]
    assert {f["held"] for f in listed["files"]} >= {shop, novel} and count == done["removed"] and listed["by_hand"] == 0
    for wrong in ({}, {"digest": digest.upper()}, {"digest": [digest]}, {"digest": digest[:-1]}, {"files": count, "bytes": size}):
        assert owner.post("/server/empty-removed", json=wrong, headers=key).status_code == 400
    other = owner.post("/server/empty-removed", json={"digest": "0" * 64}, headers=key)
    assert other.status_code == 409 and "holds other files now than the page showed" in other.json()["error"]
    assert (library / "_Removed" / shop).is_file()  # nothing deleted
    emptied = owner.post("/server/empty-removed", json={"digest": digest}, headers=key).json()
    assert (emptied["ok"], emptied["deleted"], emptied["bytes"], emptied["left"]) == (True, count, size, [])
    assert emptied["did"].startswith(f"Deleted {count} files (") and not (library / "_Removed").exists()
    assert owner.get("/server/removed").json()["count"] == 0
    assert [(p, state) for p, _, _, state in removals(library)][:3] == [(shop, "back"), (shop, "emptied"), (novel, "emptied")]
    assert curation.moved(curation.answers(library)["s:7"], 0) == "moved"  # it stays: never fetched again
    said = verify()
    assert ": 0 problems.\n" in said and f"{count} files you removed" in said and builds == [library] * 3


def test_nothing_in_hylly_s_own_folders_or_outside_the_library_is_removed_and_a_place_taken_isn_t_put_back(
        library, serving, builds, monkeypatch):
    """Only a file in a folder of the library may be removed or put back: never one in Hylly's own folders (catalog,
    state, a shop's state among them, curation, superseded), in _Removed or another folder a build passes over, hidden,
    at the top of the library, outside it by its words or by a link. A copy with such a file is refused whole, nothing
    moved (409). Put back is refused while another file is in the place, and for what isn't a file in _Removed or would
    go back to such a place."""
    from hylly import removed

    for rel in ("ebooks/Hedge Lore.epub", "humble/Moonwake/Lantern Wolf.pdf"):
        assert removed.place(library, rel) == library / rel
    for rel in ("state/storybundle/2026-01-01_0000.json", "State/x.json", "catalog/covers/a.webp", "curation/notes.txt",
                "superseded/humble/a.pdf", "_Removed/ebooks/a.epub", "_own/a.epub", ".hidden/a.epub", "ebooks/.hidden/a.epub",
                "top.epub", "../outside.epub", "ebooks/../state/x.json", "/etc/passwd", "", None, 5):
        assert removed.place(library, rel) is None, rel
    if hasattr(os, "symlink") and os.name != "nt":  # a link in the library that leads into state, or out of the library
        (library / "ebooks").mkdir(exist_ok=True)
        os.symlink(library / "state", library / "ebooks" / "linked")
        os.symlink(library.parent, library / "ebooks" / "outside")
        assert removed.place(library, "ebooks/linked/humble/2026-01-01_0000.json") is None
        assert removed.place(library, "ebooks/outside/elsewhere.epub") is None
    snapshot = "state/storybundle/2026-01-01_0000.json"
    two = [{"id": "f:a", "t": "Hedge Lore", "f": [["ebooks/Hedge%20Lore.epub", "epub", 1], [snapshot, "json", 1]]},
           {"id": "f:b", "t": "Hedge Lore", "f": [["comics/Hedge%20Lore.cbz", "cbz", 1]]}]
    for f in ("ebooks/Hedge Lore.epub", "comics/Hedge Lore.cbz"):
        (library / f).parent.mkdir(parents=True, exist_ok=True)
        (library / f).write_bytes(b"made up")
    monkeypatch.setattr(serve.Handler, "_same_sides", lambda self, found: (two, 2))
    from hylly import curation

    q = {"id": 7, "kind": "same", "options": list(curation.SAME), "guess": 1, "offered": "abc", "subjects": []}
    monkeypatch.setattr(curation, "questions", lambda root, **k: [] if k else [q])
    srv, owner = serving(library)
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    refused = owner.post("/server/remove", json={"id": 7, "keep": "right", "offered": "abc"}, headers=key)
    assert refused.status_code == 409 and refused.json()["error"].startswith(f"{snapshot} isn't a file of the library that may be removed")
    assert (library / snapshot).is_file() and (library / "ebooks" / "Hedge Lore.epub").is_file() and builds == []
    assert not (library / "_Removed").exists() and removals(library) == []
    held = library / "_Removed" / "ebooks" / "Hedge Lore.epub"  # put there by hand, while its place is taken
    held.parent.mkdir(parents=True)
    held.write_bytes(b"the other one")
    (library / "_Removed" / "state").mkdir()
    (library / "_Removed" / "state" / "notes.txt").write_bytes(b"x")
    for asked, said in (("ebooks/Hedge Lore.epub", "ebooks/Hedge Lore.epub is taken"), ("state/notes.txt", "isn't a place in the library"),
                        ("../outside.epub", "Which file?"), ("ebooks/nonesuch.epub", "isn't a file in _Removed")):
        answer = owner.post("/server/put-back", json={"held": asked}, headers=key)
        assert answer.status_code == 409 and said in answer.json()["error"], asked
    assert held.read_bytes() == b"the other one" and (library / "ebooks" / "Hedge Lore.epub").read_bytes() == b"made up"
    assert builds == []


def test_with_a_build_running_a_removal_s_build_comes_after_it_and_the_answer_says_so(library, serving, monkeypatch):
    """A removal or a put back while a catalog build runs: no build beside it, and none skipped (periodic.Builds starts
    one once it has ended); the answer says so in words, as the page shows them."""
    from hylly import curation, periodic

    wanted = []
    monkeypatch.setattr(periodic.Builds, "want", lambda self, root, log: wanted.append(Path(root)) and False)
    q = {"id": 7, "kind": "same", "title": "Arcade Worlds", "options": list(curation.SAME), "guess": 1,
         "offered": curation.fingerprint(list(curation.SAME)), "subjects": [{"id": "s:7"}, {"id": "h:novel"}]}
    monkeypatch.setattr(curation, "questions", lambda root, **k: [] if k else [q])
    srv, owner = serving(library)
    key = {"X-Server-Key": srv.RequestHandlerClass.server_key}
    done = owner.post("/server/remove", json={"id": 7, "keep": "left", "offered": q["offered"]}, headers=key).json()
    assert done["ok"] and done["build"] is False and done["did"].endswith(
        "A catalog build is running just now: another starts once it has ended, and the card leaves the library page once "
        "that one is done.")
    (held,) = [f["held"] for f in owner.get("/server/removed").json()["files"]]
    back = owner.post("/server/put-back", json={"held": held}, headers=key).json()
    assert back["build"] is False and "the card comes back to the library page once that one is done." in back["did"]
    assert wanted == [library, library]


def test_the_home_screen_icons_are_named_by_their_own_digest():
    """A changed icon comes at once, though one is kept a week: the manifest and the pages' apple-touch-icon name it with
    its own digest (ICON_VERSION), so an icon redrawn and not named anew in a page fails here; the address with it is
    the icon itself (its path; tests/routes.json pins what it answers)."""
    v = serve.ICON_VERSION
    for page in ("catalog_page.html", "login_page.html"):
        assert f'<link rel="apple-touch-icon" href="/icon-180.png?v={v}">' in Path(serve.__file__).with_name(page).read_text(encoding="utf-8")
    assert [i["src"] for i in serve.Handler.MANIFEST["icons"]] == [f"/icon-192.png?v={v}", f"/icon-512.png?v={v}"]
    assert all(serve.Handler.APP_FILES[f"/icon-{n}.png"] == f"icon-{n}.png" for n in (180, 192, 512))
