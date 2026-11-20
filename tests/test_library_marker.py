"""A folder is the library only if it says so: an unmounted disk or a mistyped path mustn't look like an empty library."""

from __future__ import annotations

import json
import os
import threading

import httpx
import pytest

from hylly import cli, copies, curation, paths, serve
from hylly import download as dl
from hylly.library import Catalog

from helpers import FakeCDN, blob, build_library, make_job


@pytest.fixture(autouse=True)
def the_marker_counts(monkeypatch):
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)


def started(root):
    root.mkdir(parents=True, exist_ok=True)
    cli.main(["init", str(root), "--yes"])
    return root


def test_init_makes_a_library_and_says_what_it_writes_and_how_to_undo_it(tmp_path, capsys):
    empty, used = tmp_path / "new", tmp_path / "used"
    (used / "humble").mkdir(parents=True)
    cli.main(["init", str(empty), "--yes"])
    both = capsys.readouterr()
    said = both.out
    assert f"by writing {paths.marker_file(empty)}, and making the folder" in said and "not there yet" in both.err  # a warning
    assert "now a Hylly library" in said and f"To undo it, delete {paths.marker_file(empty)}" in said and "library = " in said
    cli.main(["init", str(used)])  # a folder with books in it: nothing to ask
    said = capsys.readouterr().out
    assert "unmounted" not in said and f"To undo it, delete {paths.marker_file(used)}" in said
    first = paths.marker_file(used).read_bytes()
    cli.main(["init", str(used)])
    assert "already" in capsys.readouterr().out and paths.marker_file(used).read_bytes() == first  # the same library still
    assert len(json.loads(first)["id"]) == 32


def test_an_empty_library_s_init_ends_with_how_books_come_in(tmp_path, monkeypatch, capsys):
    """An empty or new folder made a library, as with no shop's login yet: the last line says how books come in.
    A folder with books in it already needs none."""
    empty, used = tmp_path / "new", tmp_path / "used"
    (used / "humble").mkdir(parents=True)
    cli.main(["init", str(empty), "--yes"])
    assert capsys.readouterr().out.splitlines()[-1] == (f"Next: hylly login SHOP and hylly download for your books at a shop, or "
                                                        f"put a folder of your own books in {empty}; then hylly catalog.")
    cli.main(["init", str(used)])
    assert "Next:" not in capsys.readouterr().out
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path / "lib"))  # the settings' library: the same last line
    cli.main(["init", "--yes"])
    assert capsys.readouterr().out.splitlines()[-1].startswith("Next: hylly login SHOP and hylly download")


def test_init_goes_by_the_settings_without_a_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path / "lib"))
    cli.main(["init", "--yes"])
    assert paths.marker_file(tmp_path / "lib").is_file() and "library = " not in capsys.readouterr().out


def test_init_writes_the_library_into_settings_that_aren_t_there_yet_and_never_changes_them(tmp_path, monkeypatch, capsys):
    """hylly init FOLDER, with no settings.toml yet, writes one with the library's line alone, and says where, so that
    every command finds it without --library; with one there, it only says the line to add, as one of yours is never
    changed. Only in the settings folder HYLLY_HOME names, as here, or this user's usual one: a file HYLLY_SETTINGS
    names is someone's own. A dry run says it would."""
    monkeypatch.delenv("HYLLY_SETTINGS")  # the settings folder's own settings.toml, which isn't there yet
    file = paths.settings_file()
    assert file == paths.config_dir() / "settings.toml" and not file.exists()
    used, other = tmp_path / "Ann's books", tmp_path / "other"  # a quote in its name: still read back as it is
    (used / "ebooks").mkdir(parents=True)
    (other / "ebooks").mkdir(parents=True)
    cli.main(["init", str(used), "--dry-run"])
    said = capsys.readouterr().out
    assert f"It would also write {file}" in said and "Nothing changed" in said and not file.exists()
    cli.main(["init", str(used)])
    said = capsys.readouterr().out
    assert f"Wrote {file}, with the library's line" in said and paths.library_root() == used  # found without --library
    written = file.read_text(encoding="utf-8")
    assert written.count("\n") == 1 and written.startswith("library = ")
    if os.name != "nt":
        assert file.stat().st_mode & 0o077 == 0  # it may hold secrets one day: its user's alone
    cli.main(["init", str(other)])  # one there now: never changed, the line to add said
    said = capsys.readouterr().out
    assert file.read_text(encoding="utf-8") == written and f"put this in {file}, at the top, above the first [section]:  library = '{other}'" in said
    cli.main(["init", str(used)])  # a library already, with its line in the settings: as it was
    assert "a Hylly library already" in capsys.readouterr().out and file.read_text(encoding="utf-8") == written
    file.unlink()
    cli.main(["init", str(used)])  # a library already, and settings gone since: written again
    assert "a Hylly library already" in capsys.readouterr().out and paths.library_root() == used
    file.unlink()
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "mine.toml"))  # someone's own file, named: left to them
    cli.main(["init", str(other)])
    said = capsys.readouterr().out
    assert f"put this in {tmp_path / 'mine.toml'}" in said and not (tmp_path / "mine.toml").exists() and not file.exists()


def test_init_writes_no_settings_for_a_folder_whose_name_isn_t_text_and_tells_of_an_app_s_copy(tmp_path, monkeypatch, capsys):
    """A folder's name that isn't UTF-8, as Linux may have, can't go into settings.toml: nothing is written, not even an
    empty file, and it says how else to give the library. And the settings written from inside a Windows packaged app
    are that app's copy: said, as hylly user says it."""
    from hylly import doctor

    monkeypatch.delenv("HYLLY_SETTINGS")
    file = paths.settings_file()
    cli._library_in_settings(paths.Path(str(tmp_path / "books") + "\udcff"), dry_run=False)  # a byte a name may have there
    said = capsys.readouterr().out
    assert not file.exists() and "isn't UTF-8 text" in said and "--library or HYLLY_LIBRARY" in said
    copy = tmp_path / "Packages" / "Some.App_abc" / "LocalCache" / "Local" / "Hylly"
    monkeypatch.setattr(doctor, "app_copies", lambda: [copy])
    (tmp_path / "books" / "ebooks").mkdir(parents=True)
    cli.main(["init", str(tmp_path / "books")])
    said = capsys.readouterr()
    assert f"Wrote {file}" in said.out and f"an app keeps its own copy of this settings folder, {copy}" in said.err


@pytest.mark.parametrize("made", [True, False])
def test_init_asks_first_for_an_empty_or_new_folder_and_with_nobody_there_marks_nothing(tmp_path, made):
    from hylly import exits

    folder = tmp_path / "mnt" / "books"
    if made:
        folder.mkdir(parents=True)  # as an unmounted disk leaves it
    with pytest.raises(SystemExit) as refused:
        cli.main(["init", str(folder)])
    assert refused.value.code == exits.NEEDS_YOU and f"hylly init {folder} --yes" in str(refused.value)
    assert not paths.marker_file(folder).exists() and (list(folder.iterdir()) == [] if made else not folder.exists())


def test_init_at_the_keyboard_marks_an_empty_folder_only_on_a_yes(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "books"
    folder.mkdir()
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda question: next(answers))
    cli.main(["init", str(folder)])
    assert "Nothing changed" in capsys.readouterr().out and not paths.marker_file(folder).exists()
    cli.main(["init", str(folder)])
    assert "now a Hylly library" in capsys.readouterr().out and paths.marker_file(folder).is_file()


def test_init_refuses_the_empty_folder_of_a_library_this_computer_keeps_the_records_of(tmp_path, capsys, monkeypatch):
    """Its disk isn't mounted: marked, the folder under it would take every download, and the next catalog would have
    no titles."""
    from hylly import catalog, exits

    root = tmp_path / "library"
    with monkeypatch.context() as m:  # made as the small made-up libraries are, and marked after: no records are
        m.setattr(paths, "REQUIRE_MARKER", False)  # taken on for a folder that isn't a library yet
        build_library(root)
    started(root)
    catalog.build(root, log=lambda s: None)
    root.rename(tmp_path / "on the disk")  # the disk unmounted: an empty folder left where the library was
    root.mkdir()
    for answer in ([], ["--yes"]):
        with pytest.raises(SystemExit) as refused:
            cli.main(["init", str(root), *answer])
        assert refused.value.code == exits.NOT_SET_UP and "unmounted disk" in str(refused.value)
        assert f"hylly init {root} --new" in str(refused.value) and list(root.iterdir()) == []
    capsys.readouterr()
    cli.main(["init", str(root), "--new", "--yes"])  # that library is gone for good: a new one in its place
    assert paths.marker_file(root).is_file() and "now a Hylly library" in capsys.readouterr().out


@pytest.mark.parametrize("command", [["catalog"], ["verify"], ["humble", "download", "--dry-run"], ["storybundle", "download"], ["itchio", "survey"],
                                     ["lookup", "--dry-run"], ["add", "own"], ["tag", "list"], ["reviews", "list"],
                                     ["reviews", "export"], ["reports"]])
def test_a_folder_without_the_marker_is_refused_and_nothing_appears_in_it(tmp_path, command):
    """The same mistake reads the same in every command that reads the library: one to set up (5), never trouble with
    the disk (6), as hylly reviews and reports said it once."""
    from hylly import exits

    folder = tmp_path / "unmounted"
    folder.mkdir()
    with pytest.raises(SystemExit, match="No Hylly library at .*disk or the share") as refused:
        cli.main([*command, "--library", str(folder)])
    assert refused.value.code == exits.NOT_SET_UP and list(folder.iterdir()) == []  # not even a lock


# Commands with --library that may run where no library is, each for its reason: they make one, say where things
# are and what's wrong, serve or wait for one that may come later, or check it themselves
NO_LIBRARY_NEEDED = {
    "init": "makes the library",
    "serve": "starts all the same, as a share may come up later, and its page says what's missing",
    "server": "jobs runs what is due, as the server would; take-over checks the library itself",
    "logs": "says where the logs are, with or without one",
    "doctor": "says what's wrong, a library that isn't there too",
    "backup": "it holds the disks itself, and checks the library itself, in the same words",
    "download": "each shop's run of it checks the library first",
    "internal": "between Hylly's own processes, each of which checks what it needs",
}


def test_every_command_that_reads_the_library_says_a_folder_that_isn_t_one_as_such():
    """Every command that takes --library is checked before it starts (cli.NEEDS_THE_LIBRARY), or is named above with
    why not: a new command that reads the catalog or the records is held to it here, as reviews and reports weren't."""
    from hylly import commands

    with_library = {name.split()[0] for name, p, _ in commands.leaves(commands.build_parser(), internal=True)
                    if any("--library" in a.option_strings for a in p._actions)}
    assert with_library == cli.NEEDS_THE_LIBRARY | set(NO_LIBRARY_NEEDED) and not cli.NEEDS_THE_LIBRARY & set(NO_LIBRARY_NEEDED)


def test_the_refusal_asks_about_the_disk_first_and_offers_init_only_for_a_new_library(tmp_path):
    with pytest.raises(SystemExit) as refused:
        paths.check_library(tmp_path / "unmounted")
    said = str(refused.value)
    assert said.index("disk or the share there") < said.index("hylly init") and "Only for a new library: hylly init" in said
    assert "from before" not in said


def test_a_mistyped_path_isn_t_created(tmp_path):
    with pytest.raises(SystemExit, match="No Hylly library"):
        cli.main(["catalog", "--library", str(tmp_path / "typo")])
    assert not (tmp_path / "typo").exists()


def test_stopping_a_run_doesn_t_need_the_library(tmp_path, capsys):
    cli.main(["jobs", "stop", "catalog"])
    assert "running" in capsys.readouterr().out


def test_a_download_stops_when_the_library_goes(tmp_path):
    root = started(tmp_path / "lib")
    cdn = FakeCDN({"/a.epub": blob(1000), "/b.epub": blob(1000, 2)})
    d = dl.Downloader(root, dl.Manifest(root, "humble"), refresh=lambda job: job.url,
                      transport=httpx.MockTransport(cdn.handler), log=lambda s: None, backoff=0.0, workers=1)
    assert len(d.run([make_job("a.epub", blob(1000))])["ok"]) == 1
    paths.marker_file(root).unlink()  # as when the disk is unmounted
    result = d.run([make_job("b.epub", blob(1000, 2))])
    assert not result["ok"] and d.blocked and "isn't there any more" in d.blocked and len(cdn.requests) == 1


class GoneShare(FileNotFoundError):
    winerror = 67  # Windows' "the network name cannot be found": a share gone away, which Python calls not found


def share_gone_for(monkeypatch, looks: int) -> None:
    """The library's share gone away for this many looks at its marker (paths.library_missing's), and then back."""
    real, left = paths.marker_file, [looks]

    class Marker:
        def __init__(self, root):
            self.real = real(root)

        def stat(self):
            if left[0] > 0:
                left[0] -= 1
                raise GoneShare(2, "The network name cannot be found")
            return self.real.stat()

    monkeypatch.setattr(paths, "marker_file", Marker)


def test_a_share_gone_for_a_moment_is_waited_for_before_the_library_counts_as_missing(tmp_path, monkeypatch):
    """Windows calls a share gone away "not found" too (storage.share_is_gone): a long run waits for it, looking again
    every SHARE_ASK_EVERY seconds for up to SHARE_WAIT, as a NAS restarting its file sharing is gone for a moment.
    Without a wait (the server's look, the bell's), a share that stays gone, a run stopping meanwhile, and a marker
    that isn't there (an unmounted disk): missing, and at once but for the share that stays gone."""
    root, real = started(tmp_path / "lib"), paths.marker_file
    waited: list[float] = []
    share_gone_for(monkeypatch, 3)
    assert not paths.library_missing(root, waited.append) and waited == [paths.SHARE_ASK_EVERY] * 3
    share_gone_for(monkeypatch, 3)
    assert paths.library_missing(root)  # no wait asked for: at once
    waited.clear()
    share_gone_for(monkeypatch, 1000)
    assert paths.library_missing(root, waited.append) and sum(waited) == paths.SHARE_WAIT == 240  # every 15 s for 4 min
    waited.clear()
    share_gone_for(monkeypatch, 3)
    stopping = lambda s: waited.append(s) or True  # noqa: E731 - a stop event's wait, the run stopping meanwhile
    assert paths.library_missing(root, stopping) and waited == [paths.SHARE_ASK_EVERY]
    monkeypatch.setattr(paths, "marker_file", real)
    paths.marker_file(root).unlink()  # as when the disk is unmounted: no share to wait for
    assert paths.library_missing(root, waited.append) and waited == [paths.SHARE_ASK_EVERY]


def test_a_download_and_a_copy_go_on_after_the_share_was_gone_for_a_moment(tmp_path, monkeypatch):
    """Before each file a download and hylly add look for the library (as hylly humble seed does: copies.DiskWatch);
    its share gone for a moment, as when a NAS restarts its file sharing, is waited for, and the run goes on."""
    monkeypatch.setattr(paths, "SHARE_ASK_EVERY", 0.01)
    root = started(tmp_path / "lib")
    cdn = FakeCDN({"/a.epub": blob(1000), "/b.epub": blob(1000, 2)})
    d = dl.Downloader(root, dl.Manifest(root, "humble"), refresh=lambda job: job.url,
                      transport=httpx.MockTransport(cdn.handler), log=lambda s: None, backoff=0.0, workers=1)
    share_gone_for(monkeypatch, 3)
    result = d.run([make_job("a.epub", blob(1000)), make_job("b.epub", blob(1000, 2))])
    assert len(result["ok"]) == 2 and not d.blocked
    source = tmp_path / "from"
    source.mkdir()
    (source / "a.epub").write_bytes(b"book a")
    real = copies.copy_one

    def then_gone(*a, **k):
        done = real(*a, **k)
        share_gone_for(monkeypatch, 3)  # gone after the first copy, for a moment
        return done

    (source / "b.epub").write_bytes(b"book b")
    monkeypatch.setattr(copies, "copy_one", then_gone)
    copies.add_books(root, "own", source, log=lambda s: None)
    assert (root / "own" / "a.epub").read_bytes() == b"book a" and (root / "own" / "b.epub").read_bytes() == b"book b"


def test_a_copy_stops_when_the_library_goes(tmp_path):
    root, source = started(tmp_path / "lib"), tmp_path / "from"
    source.mkdir()
    (source / "a.epub").write_bytes(b"book a")
    (source / "b.epub").write_bytes(b"book b")
    real = copies.copy_one

    def then_gone(*a, **k):
        done = real(*a, **k)
        paths.marker_file(root).unlink()
        return done

    copies.copy_one = then_gone
    try:
        with pytest.raises(SystemExit, match="1 copied, 0 couldn.t be, and the library .* isn.t there"):
            copies.add_books(root, "own", source, log=lambda s: None)
    finally:
        copies.copy_one = real
    assert not (root / "own" / "b.epub").exists()


def test_seed_stops_when_the_library_goes_and_doesnt_start_without_it(tmp_path, monkeypatch):
    """As hylly add does: on the server, the rest would land on the system disk an unmounted one leaves behind."""
    root, source = started(tmp_path / "lib"), tmp_path / "old"
    source.mkdir()
    jobs = []
    for name, data in (("lw1.cbz", blob(1000)), ("lw2.cbz", blob(1001, 2))):
        (source / name).write_bytes(data)
        jobs.append(make_job(name, data, kind="comic"))
    real = copies.copy_in

    def then_gone(*a, **k):
        real(*a, **k)
        paths.marker_file(root).unlink()

    monkeypatch.setattr(copies, "copy_in", then_gone)
    with pytest.raises(SystemExit, match="1 copied, 0 couldn.t be, and the library .* isn.t there any more"):
        dl.seed([source], jobs, root, dl.Manifest(root, "humble"), log=lambda s: None)
    assert (root / jobs[0].path).is_file() and not (root / jobs[1].path).exists()
    monkeypatch.setattr(copies, "copy_in", real)
    with pytest.raises(SystemExit, match="0 copied, 0 couldn.t be, and the library .* isn.t there any more"):
        dl.seed([source], jobs, root, dl.Manifest(root, "humble"), log=lambda s: None)  # its marker still gone
    assert not (root / jobs[1].path).exists()


def test_a_change_isn_t_kept_where_it_would_be_lost(tmp_path):
    folder = tmp_path / "unmounted"
    folder.mkdir()
    with pytest.raises(OSError, match="isn.t there"):
        curation._append(folder, {"at": "x"})
    assert list(folder.iterdir()) == []


def test_the_server_tells_a_library_that_isn_t_there_from_one_with_no_catalog_yet(tmp_path, log_in):
    folder = tmp_path / "unmounted"
    folder.mkdir()
    with pytest.raises(OSError, match="isn.t there"):
        Catalog(folder).get()
    assert Catalog(started(tmp_path / "new")).get() == []
    srv, _ = serve.make_server(folder, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with log_in(httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", timeout=30)) as client:
            s = client.get("/server/status").json()
            assert s["library_gone"] and s["titles"] == 0
            assert client.get("/opds").status_code == 500  # not an empty list, which a reading app would believe
            paths.marker_file(folder).parent.mkdir()
            paths.marker_file(folder).write_text("{}", encoding="utf-8")  # the disk is back
            assert not client.get("/server/status").json()["library_gone"]
    finally:
        srv.shutdown()
        srv.server_close()
