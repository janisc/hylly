"""What a script or an assistant goes by: exit codes that say what happened, prompts that don't hang, options that aren't
ignored, a login checked before it's saved, and the commands that tell how things stand."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import httpx
import pytest

from hylly import access, bell, cli, commands, demo, doctor, exits, logins, lookups, paths, runs, serve
from hylly.library import Catalog
from hylly.exits import Exit
from hylly.logins import AuthError, ShopError

from helpers import ORDERS, build_library, damage_a_mobi, exit_of, locked_epub, make_epub, states

REAL_USUAL_FOLDER, REAL_IS_ROOT = paths.usual_settings_folder, paths.is_root
COOKIE = "c" * 80


def job_doing(monkeypatch, does):
    monkeypatch.setattr(cli, "cmd_storybundle", lambda args: does())


# ---- exit codes -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("raises", "code"), [
    (AuthError("The login has expired."), exits.LOGIN),
    (ShopError("The shop answered HTTP 503."), exits.PARTLY),
    (httpx.ConnectError("no network"), exits.PARTLY),
    (OSError(28, "No space left on device"), exits.LOCAL),
    (KeyboardInterrupt(), exits.INTERRUPTED),
    (Exit(exits.NEEDS_YOU, "It needs a person."), exits.NEEDS_YOU),
])
def test_a_job_s_end_has_its_own_code(tmp_path, monkeypatch, capsys, raises, code):
    def does():
        raise raises

    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)
    job_doing(monkeypatch, does)
    assert exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))[0] == code


def test_every_code_is_in_the_table_and_the_help_page():
    for code in (exits.OK, exits.PARTLY, exits.USAGE, exits.LOGIN, exits.BUSY, exits.NOT_SET_UP, exits.LOCAL, exits.NEEDS_UPDATE, exits.NEEDS_YOU,
                 exits.BUG, exits.INTERRUPTED):
        assert exits.TABLE[code]
    page = commands.reference_html()
    assert 'id="exit-codes"' in page and 'id="environment"' in page and "HYLLY_NO_NOTIFY" in page and "HYLLY_HUMBLE_SESSION" in page
    assert ">70<" in page and "bug in Hylly" in page


def test_a_bug_ends_with_70_says_where_its_details_are_and_isn_t_partly_done(tmp_path, monkeypatch, capsys):
    """Not 1, which says run it again: a script would retry a bug for ever. Its job's record and note say so too."""
    said = []
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: said.append(a))

    def does():
        raise ValueError("https://a-shop.example/order?key=secret went wrong")

    job_doing(monkeypatch, does)
    code, err = exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))
    assert code == exits.BUG == 70
    assert "Traceback" in err and "ValueError" in err and "https://a-shop.example/..." in err and "key=secret" not in err
    assert err.rstrip().endswith(exits.BUG_WORDS) and exits.ISSUES in err and err.count(exits.BUG_WORDS) == 1
    last = as_json(capsys, "jobs", "--json")["last"][0]
    assert last["exit"] == exits.BUG and "Stopped by a bug" in last["said"] and "secret" not in json.dumps(last)
    assert said[0][0] == "A StoryBundle download stopped by a bug" and "secret" not in str(said)
    monkeypatch.setattr(cli, "cmd_theme", lambda args: {}["no such key"])  # not a job: the same end
    code, err = exit_of(capsys, "theme", "list")
    assert code == exits.BUG and "KeyError" in err and exits.BUG_WORDS in err


def test_a_run_refused_over_the_database_is_said_alike_everywhere(tmp_path, monkeypatch, capsys):
    """Its words in the exit, the phone's note and no record of a run, as it never started: not 1 and a class's name."""
    from hylly import db

    said = []
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: said.append(a))

    def does():
        raise db.NotHere("The library's own records are kept in the database on another computer: run this there.")

    job_doing(monkeypatch, does)
    code, err = exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))
    assert code == exits.NOT_SET_UP and "run this there" in err
    assert as_json(capsys, "jobs", "--json")["last"] == []
    assert said and "run this there" in said[0][1] and "NotHere" not in str(said)


def test_a_damaged_database_names_the_file_and_the_way_back(tmp_path, monkeypatch, capsys):
    from hylly import db

    db.file().parent.mkdir(parents=True, exist_ok=True)
    db.file().write_bytes(b"this is no database at all " * 200)
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path))
    code, err = exit_of(capsys, "user", "list")
    assert code == exits.LOCAL and "Traceback" not in err
    assert str(db.file()) in err and str(tmp_path / "state" / "db-backup") in err and "README.txt" in err


def test_no_plain_systemexit_with_words_is_left_every_ending_has_its_code():
    """A SystemExit with words ends with 1, whatever the table says: each ending is an Exit with its code. The one left is
    the walled server's way of asking its service to start it again, which is no command's ending."""
    import ast

    left = []
    for path in sorted((Path(__file__).parents[1] / "src" / "hylly").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            called = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(called, ast.Name) and called.id == "SystemExit" and ast.unparse(node.exc) != "SystemExit(instance.RESTART_CODE)":
                left.append(f"{path.name}:{node.lineno}: {ast.unparse(node.exc)[:60]}")
    assert left == []


def test_a_server_that_can_t_start_in_the_background_ends_with_its_own_code(monkeypatch, tmp_path):
    """A port that's taken is 6, as in the foreground, not 1: nothing started, and a retry won't help."""
    from hylly import instance

    class Ended:
        returncode = exits.LOCAL

        def poll(self):
            return self.returncode

    def spawned(arguments, errors_to_log=False):  # it says why in its own log, as it ends
        instance.write_log("Port 8820 is taken.")
        return Ended()

    monkeypatch.setattr(instance, "spawn", spawned)
    with pytest.raises(Exit) as e:
        instance.start_background()
    assert e.value.code == exits.LOCAL and "Port 8820 is taken." in e.value.message
    Ended.returncode = 1  # a crash as older versions ended one (a bug has its own code now, 70), or anything not of the
    # table: trouble here, never partly
    with pytest.raises(Exit) as e:
        instance.start_background()
    assert e.value.code == exits.LOCAL
    Ended.returncode = exits.NOT_SET_UP
    with pytest.raises(Exit) as e:
        instance.start_background()
    assert e.value.code == exits.NOT_SET_UP


def test_a_rules_copy_that_can_t_be_is_a_usage_error(monkeypatch, tmp_path, capsys):
    from hylly import rules

    mine = tmp_path / "rules"
    monkeypatch.setenv("HYLLY_RULES", str(mine))
    cli.main(["rules", "copy", "genres"])
    capsys.readouterr()
    code, err = exit_of(capsys, "rules", "copy", "genres")
    assert code == exits.USAGE and "exists already" in err
    one = tmp_path / "one.toml"
    one.write_text("", encoding="utf-8")
    monkeypatch.setenv("HYLLY_RULES", str(one))
    rules.reset()
    code, err = exit_of(capsys, "rules", "copy", "genres")
    assert code == exits.USAGE and "one file" in err


def test_a_message_is_shown_once(tmp_path, monkeypatch, capsys):
    def does():
        raise Exit(exits.LOCAL, "Not enough space.")

    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)
    job_doing(monkeypatch, does)
    code, said = exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))
    assert code == exits.LOCAL and said.count("Not enough space") == 1


def test_a_second_run_is_busy(tmp_path, monkeypatch, capsys):
    job_doing(monkeypatch, lambda: pytest.fail("it shouldn't run"))
    with runs.only_one("storybundle"):
        code, said = exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))
    assert code == exits.BUSY and "already running" in said


def test_not_set_up_says_what_comes_first(tmp_path, monkeypatch, capsys):
    assert exit_of(capsys, "tag", "list", "--library", str(tmp_path))[0] == exits.NOT_SET_UP  # no catalog
    assert exit_of(capsys, "humble", "seed", str(tmp_path), "--library", str(tmp_path))[0] == exits.NOT_SET_UP  # no snapshot
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    assert exit_of(capsys, "verify", "--library", str(tmp_path / "unmounted"))[0] == exits.NOT_SET_UP  # no library there
    (tmp_path / "settings.toml").write_text("this isn't toml", encoding="utf-8")
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "settings.toml"))
    from hylly import settings

    settings.reload()  # a command of its own reads them afresh; in one test process the ones before had read them
    assert exit_of(capsys, "bell")[0] == exits.NOT_SET_UP


def test_what_comes_first_is_said_in_one_set_of_words_wherever_it_s_missing(tmp_path, capsys):
    """No catalog yet, for hylly reviews, reports and set alike (cli._catalog_items), though set by --id needs none, as
    a shop's title not downloaded yet is named so; no snapshot of the Humble orders, for a seed and for set format and
    file alike (cli._latest_snapshot), the seed's with its own way out after it."""
    for argv in (["reviews"], ["reports"], ["set", "kind", "comic", "--search", "Moonwake"]):
        code, said = exit_of(capsys, *argv, "--library", str(tmp_path))
        assert code == exits.NOT_SET_UP and cli.NO_CATALOG in said, argv
    code, said = exit_of(capsys, "set", "kind", "comic", "--id", "h:nosuch", "--yes", "--library", str(tmp_path))
    assert code == exits.USAGE and cli.NO_CATALOG not in said  # it looked for the title instead
    code, said = exit_of(capsys, "humble", "seed", str(tmp_path), "--library", str(tmp_path))
    assert code == exits.NOT_SET_UP and said.strip().endswith("No snapshot of your Humble orders yet: run hylly humble download "
                                                              "first (with --limit 0 it reads and saves your orders without "
                                                              "downloading), or pass --snapshot.")
    with pytest.raises(Exit) as e:
        cli._latest_snapshot(paths.state_dir(tmp_path))
    assert e.value.code == exits.NOT_SET_UP and e.value.message.endswith("without downloading).")
    from hylly import snapshot

    saved = snapshot.put(paths.state_dir(tmp_path), "humble", "{}")
    assert cli._latest_snapshot(paths.state_dir(tmp_path)) == (saved, {})  # with its orders, as read


def test_a_long_list_shows_its_first_lines_and_how_many_more(capsys):
    """A dry run's files to download: the first 40, then how many there are besides (cli._list_some)."""
    cli._list_some([f"  file {n}" for n in range(1, 43)])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 41 and lines[39] == "  file 40" and lines[-1] == "  ... and 2 more"
    cli._list_some(["  one", "  two"], most=2)
    assert capsys.readouterr().out == "  one\n  two\n"


def test_a_mistyped_command_is_a_usage_error(capsys):
    assert exit_of(capsys, "login")[0] == exits.USAGE
    assert exit_of(capsys, "humble")[0] == exits.USAGE
    assert exit_of(capsys, "user", "remove")[0] == exits.USAGE
    assert exit_of(capsys, "nosuchcommand")[0] == exits.USAGE


class FakeDownloader:
    """A run that ended as these say, for _run_jobs's end."""

    def __init__(self, failed=(), stopped=False, blocked=None, auth_failed=None, disk_full=False):
        self.results = {"ok": [], "failed": list(failed), "stopped": []}
        self.stop, self.blocked, self.auth_failed, self.disk_full = threading.Event(), blocked, auth_failed, disk_full
        if stopped or blocked:
            self.stop.set()

    def run(self, jobs):
        return self.results


def run_ending(monkeypatch, tmp_path, **how) -> None:
    from hylly import download as dl

    monkeypatch.setattr(dl, "Downloader", lambda *a, **k: FakeDownloader(**how))
    monkeypatch.setattr(dl, "write_failures", lambda *a, **k: 0)
    cli._run_jobs(tmp_path, dl.Manifest(tmp_path, "humble"), [], refresh=None, workers=1, failures="f.csv")


def test_a_download_with_failed_files_doesn_t_say_all_is_well(tmp_path, monkeypatch):
    run_ending(monkeypatch, tmp_path)  # all well
    with pytest.raises(Exit) as e:
        run_ending(monkeypatch, tmp_path, failed=[("a job", "an error")])
    assert e.value.code == exits.PARTLY and "1 file failed" in str(e.value)
    with pytest.raises(Exit) as e:
        run_ending(monkeypatch, tmp_path, stopped=True)
    assert e.value.code == exits.INTERRUPTED
    with pytest.raises(AuthError):
        run_ending(monkeypatch, tmp_path, auth_failed="It has expired.")
    with pytest.raises(Exit) as e:
        run_ending(monkeypatch, tmp_path, blocked="The shop has turned us away.")
    assert e.value.code == exits.PARTLY
    with pytest.raises(Exit) as e:
        run_ending(monkeypatch, tmp_path, blocked="The library's disk is full, so the run stopped.", disk_full=True)
    assert e.value.code == exits.LOCAL  # trouble on this computer, as for a library gone away


def test_failed_files_are_a_finish_and_not_a_stop_in_the_note(tmp_path, monkeypatch, capsys):
    said = []
    monkeypatch.setattr(bell, "job_note", lambda title, text, public="": said.append(title))

    def does():
        raise cli.Ended(text="9 files done, 1 failed.", failed=1).carried(Exit(exits.PARTLY, "1 file failed."))

    job_doing(monkeypatch, does)
    assert exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))[0] == exits.PARTLY
    assert said == ["A StoryBundle download finished"]


def test_a_refused_key_is_a_matter_for_its_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", lambda *a, **k: stats_with(stopped="It refused the token.", refused=True))
    assert exit_of(capsys, "lookup", "--library", str(tmp_path))[0] == exits.LOGIN


def test_a_lookup_that_can_t_write_is_trouble_here(tmp_path, monkeypatch, capsys):
    def cannot_write(*a, **k):
        raise lookups.LookupProblem("Can't write the lookup cache.")

    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", cannot_write)
    assert exit_of(capsys, "lookup", "--library", str(tmp_path))[0] == exits.LOCAL


def test_a_seed_with_failed_files_says_so(tmp_path, monkeypatch, capsys):
    from hylly import download as dl
    from hylly import humble, plan

    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)
    monkeypatch.setattr(cli.snapshot, "load", lambda path: [])
    monkeypatch.setattr(plan, "for_library", lambda *a: None)
    monkeypatch.setattr(humble, "titles_of", lambda *a: [])
    monkeypatch.setattr(dl, "seed", lambda *a, **k: {"copied": [], "already": [], "other_version": [], "unknown": [], "failed": [(tmp_path / "a.epub", "an error")]})
    code, said = exit_of(capsys, "humble", "seed", str(tmp_path), "--snapshot", str(tmp_path / "s.json"), "--library", str(tmp_path))
    assert code == exits.PARTLY and "couldn't be copied" in said


def test_a_negative_limit_is_refused(capsys):
    assert exit_of(capsys, "humble", "download", "--limit", "-1")[0] == exits.USAGE
    assert exit_of(capsys, "download", "--limit", "-5")[0] == exits.USAGE
    assert exit_of(capsys, "lookup", "--limit", "x")[0] == exits.USAGE


def test_a_stop_s_dry_run_says_what_it_would_end_and_ends_nothing(capsys):
    with runs.only_one("humble"):
        cli.main(["jobs", "stop", "humble", "--dry-run"])
        assert "It would stop a Humble download or seed" in capsys.readouterr().out and runs.current("humble")


def test_stopping_what_isn_t_running_says_so(capsys):
    cli.main(["jobs", "stop", "itchio"])
    assert capsys.readouterr().out.startswith("No itch.io survey")


def stats_with(**st) -> dict:
    return {"metron": {"found": 0, "stopped": "", "left": 0, "waiting": 0, **st}}


@pytest.mark.parametrize(("st", "code"), [({"stopped": "Three answers in a row couldn't be read.", "changed": True}, exits.NEEDS_UPDATE),
                                          ({"stopped": "It refused."}, exits.PARTLY)])
def test_a_lookup_that_didn_t_finish_says_how(tmp_path, monkeypatch, capsys, st, code):
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", lambda *a, **k: stats_with(**st))
    assert exit_of(capsys, "lookup", "--library", str(tmp_path))[0] == code


def test_a_day_s_budget_used_up_is_no_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", lambda *a, **k: stats_with(stopped="Today's budget is spent.", quota=True))
    cli.main(["lookup", "--library", str(tmp_path)])


def shops_ending(monkeypatch, **ends):
    """Each shop's cmd raising what it's given here; a shop not named has no login."""
    from hylly.logins import SITES

    for shop in ("humble", "storybundle", "drivethrurpg", "itchio"):
        monkeypatch.delenv(SITES[shop][2], raising=False)
    for shop, what in ends.items():
        def does(args, what=what):
            if what:
                raise what
        monkeypatch.setenv(SITES[shop][2], COOKIE)
        monkeypatch.setattr(cli, f"cmd_{shop}", does)
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)


def test_hylly_download_passes_on_what_stopped_every_shop(tmp_path, monkeypatch, capsys):
    shops_ending(monkeypatch, humble=AuthError("Expired."), storybundle=AuthError("Expired."))
    assert exit_of(capsys, "download", "--library", str(tmp_path))[0] == exits.LOGIN
    shops_ending(monkeypatch, humble=AuthError("Expired."), storybundle=None)
    assert exit_of(capsys, "download", "--library", str(tmp_path))[0] == exits.LOGIN
    shops_ending(monkeypatch, humble=AuthError("Expired."), storybundle=ShopError("HTTP 503."))
    assert exit_of(capsys, "download", "--library", str(tmp_path))[0] == exits.PARTLY


def test_hylly_download_with_no_login_at_all_did_nothing(tmp_path, monkeypatch, capsys):
    shops_ending(monkeypatch)
    code, said = exit_of(capsys, "download", "--library", str(tmp_path))
    assert code == exits.LOGIN and "nothing was done" in said


def test_hylly_download_with_no_library_there(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "REQUIRE_MARKER", True)
    shops_ending(monkeypatch, humble=None, storybundle=None)
    assert exit_of(capsys, "download", "--library", str(tmp_path / "unmounted"))[0] == exits.NOT_SET_UP


def test_something_unforeseen_in_one_shop_leaves_the_others_their_turn(tmp_path, monkeypatch, capsys):
    """The bug's own end, 70, as what they have in common: the others went fine."""
    ran = []
    shops_ending(monkeypatch, humble=ValueError("https://a-shop.example/?key=secret"), storybundle=None)
    monkeypatch.setattr(cli, "cmd_storybundle", lambda args: ran.append(args.command))
    code, err = exit_of(capsys, "download", "--library", str(tmp_path))
    assert code == exits.BUG and ran == ["storybundle"] and "key=secret" not in err and err.count(exits.BUG_WORDS) == 1


def test_ctrl_c_in_hylly_download_is_said_once(tmp_path, monkeypatch, capsys):
    shops_ending(monkeypatch, humble=Exit(exits.INTERRUPTED, "Stopped."))
    code, said = exit_of(capsys, "download", "--library", str(tmp_path))
    assert code == exits.INTERRUPTED and said.count("Stopped.") == 1


def test_hylly_download_stops_at_ctrl_c(tmp_path, monkeypatch, capsys):
    ran = []

    def interrupted(args):
        ran.append(args.command)
        raise Exit(exits.INTERRUPTED, "Stopped.")

    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", COOKIE)
    monkeypatch.setenv("HYLLY_STORYBUNDLE_SESSION", COOKIE)
    monkeypatch.setattr(cli, "cmd_humble", interrupted)
    monkeypatch.setattr(cli, "cmd_storybundle", interrupted)
    assert exit_of(capsys, "download", "--library", str(tmp_path))[0] == exits.INTERRUPTED and ran == ["humble"]


def test_hylly_download_limit_goes_to_every_shop(tmp_path, monkeypatch):
    limits = {}
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", COOKIE)
    monkeypatch.setenv("HYLLY_STORYBUNDLE_SESSION", COOKIE)
    for name in ("cmd_humble", "cmd_storybundle"):
        monkeypatch.setattr(cli, name, lambda args: limits.update({args.command: args.limit}))
    cli.main(["download", "--limit", "5", "--library", str(tmp_path)])
    assert limits == {"humble": 5, "storybundle": 5}


# ---- nobody at the keyboard ----------------------------------------------------------------------------------

def test_nul_is_no_keyboard():
    done = subprocess.run([sys.executable, "-c", "from hylly import cli; print(cli.at_keyboard())"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60, env={**os.environ})
    assert done.stdout.strip() == "False", done.stderr


@pytest.mark.parametrize("argv", [["user", "add", "someone"], ["login", "humble"], ["login", "metron"]])
def test_what_needs_a_person_doesn_t_hang_without_one(argv, tmp_path):
    """A real process, with no console to read: on Windows getpass would wait for ever."""
    done = subprocess.run([sys.executable, "-m", "hylly.cli", *argv], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=60, env={**os.environ, "HYLLY_LIBRARY": str(tmp_path)})
    assert done.returncode == exits.NEEDS_YOU and "needs a person" in done.stderr


def test_a_yes_needs_someone_to_say_it(monkeypatch):
    with pytest.raises(Exit) as e:
        cli._confirm("Sure?")
    assert e.value.code == exits.NEEDS_YOU and "--yes" in str(e.value)
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)

    def eof(question):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(Exit):
        cli._confirm("Sure?")
    monkeypatch.setattr("builtins.input", lambda question: "y")
    assert cli._confirm("Sure?")


def test_removing_a_user_asks_first(capsys):
    access.add_user("someone", "correct horse")
    assert exit_of(capsys, "user", "remove", "someone")[0] == exits.NEEDS_YOU
    assert "someone" in access.load_accounts()
    assert exit_of(capsys, "user", "remove", "nobody-here", "--yes")[0] == exits.USAGE  # a name that isn't there: no retry helps
    cli.main(["user", "remove", "someone", "--yes"])
    assert "someone" not in access.load_accounts()


def test_no_password_comes_from_a_program():
    with pytest.raises(Exit) as e:
        cli._hidden("Password: ", "hylly user add someone")
    assert e.value.code == exits.NEEDS_YOU and "hylly user add someone" in str(e.value)


# ---- a login checked first ----------------------------------------------------------------------------------

class FakeHumble:
    answer = ["a"]

    def __init__(self, cookie, **options):  # options: a login's check asks once (tries=1)
        self.cookie = cookie

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def check(self):  # whether Humble says you're signed in
        if isinstance(self.answer, Exception):
            raise self.answer

    def order_keys(self):
        self.check()
        return self.answer


def humble_login(monkeypatch, answer, clipboard=COOKIE):
    monkeypatch.delenv("HYLLY_HUMBLE_SESSION", raising=False)
    monkeypatch.setattr("hylly.humble.HumbleClient", type("Fake", (FakeHumble,), {"answer": answer}))
    monkeypatch.setattr(cli, "read_clipboard", lambda: clipboard)


def test_a_login_is_checked_before_it_replaces_the_old_one(monkeypatch, capsys):
    old = "o" * 80
    logins.save_session(old, "humble")
    humble_login(monkeypatch, AuthError("Humble refused it."))
    code, said = exit_of(capsys, "login", "humble", "--clipboard")
    assert code == exits.LOGIN and "Not saved: the login you had stays" in said and logins.load_session("humble") == old
    humble_login(monkeypatch, httpx.ConnectError("no network"))
    code, said = exit_of(capsys, "login", "humble", "--clipboard")
    assert code == exits.PARTLY and "couldn't be checked" in said and logins.load_session("humble") == old


def test_a_site_s_trouble_says_nothing_about_the_login(monkeypatch, capsys):
    humble_login(monkeypatch, ShopError("Humble answered HTTP 503."))
    code, said = exit_of(capsys, "login", "humble", "--clipboard")
    assert code == exits.PARTLY and "couldn't be checked" in said and not logins.session_path("humble").exists()


class FakeSource:
    name, per_hour, per_day, headers = "Metron", 1, 1, {}
    answer = None

    def __init__(self, key):
        self.key = key

    def check(self, session):
        session.budget.note()
        if self.answer:
            raise self.answer
        assert self.key == "k" * 40  # what was given, and not something saved
        return "Metron accepted the token"


def lookup_login(monkeypatch, answer):
    monkeypatch.delenv("HYLLY_METRON_TOKEN", raising=False)
    monkeypatch.setattr(lookups, "sources", lambda: {"metron": type("Fake", (FakeSource,), {"answer": answer})})
    monkeypatch.setattr(lookups, "Session", lambda source, budget, headers=None: type("S", (), {"budget": budget, "close": lambda self: None})())
    monkeypatch.setattr(cli, "read_clipboard", lambda: "k" * 40)


def test_a_key_is_checked_with_no_library_and_no_budget_of_the_library_s(monkeypatch, tmp_path, capsys):
    lookup_login(monkeypatch, None)
    cli.main(["login", "metron", "--clipboard"])  # no library set
    assert logins.load_session("metron") == "k" * 40
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path))
    cli.main(["login", "metron", "--clipboard"])  # a spent budget wouldn't matter, and nothing lands here
    assert list(tmp_path.iterdir()) == []
    assert exit_of(capsys, "login", "metron", "--clipboard", "--library", str(tmp_path))[0] == exits.USAGE  # it takes none


def test_a_login_s_dry_run_checks_it_and_saves_nothing(monkeypatch, capsys):
    lookup_login(monkeypatch, None)
    cli.main(["login", "metron", "--clipboard", "-n"])
    assert "nothing was saved" in capsys.readouterr().out and not logins.session_path("metron").exists()


@pytest.mark.parametrize(("answer", "code"), [(lookups.Refused("It refused the token."), exits.LOGIN), (lookups.Transient("The site is down."), exits.PARTLY),
                                              (lookups.Changed("Its answers look different."), exits.PARTLY)])
def test_only_a_refusal_is_about_the_key(monkeypatch, capsys, answer, code):
    lookup_login(monkeypatch, answer)
    got, said = exit_of(capsys, "login", "metron", "--clipboard")
    assert got == code and "Not saved" in said and not logins.session_path("metron").exists()


def test_ctrl_d_at_a_prompt_is_no_crash(monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", eof)
    with pytest.raises(Exit) as e:
        cli._hidden("Password: ", "hylly user add someone")
    assert e.value.code == exits.NEEDS_YOU


def test_a_good_login_is_saved_and_never_shown(monkeypatch, capsys):
    humble_login(monkeypatch, ["a", "b"])
    cli.main(["login", "humble", "--clipboard"])
    said = capsys.readouterr()
    assert logins.load_session("humble") == COOKIE and COOKIE not in said.out + said.err
    assert "Read 80 characters" in said.err and "you're signed in" in said.out  # a note on the way, and the result


def test_clipboard_text_that_is_no_login_goes_nowhere(monkeypatch, capsys):
    humble_login(monkeypatch, ["a"], clipboard="my shopping list")
    monkeypatch.setattr("hylly.humble.HumbleClient", lambda cookie: pytest.fail("nothing should be sent"))
    assert exit_of(capsys, "login", "humble", "--clipboard")[0] == exits.LOGIN
    assert not logins.session_path("humble").exists()


# ---- logins and users without a keyboard -----------------------------------------------------------------------

class Keyboard(io.StringIO):
    """Standard input at a keyboard, where a secret would show as it's typed."""

    def isatty(self) -> bool:
        return True


def stdin_of(monkeypatch, data: bytes) -> None:
    """Standard input as a pipe gives it, with no file under it."""
    monkeypatch.setattr(cli.sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


JAR = ("# Netscape HTTP Cookie File\n"
       ".example.org\tTRUE\t/\tTRUE\t1893456000\tory_session_stoiccohens510ht9xek\tanother-site-s-made-up-value\n"
       "#HttpOnly_.humblebundle.com\tTRUE\t/\tTRUE\t1700000000\tory_session_stoiccohens510ht9xek\t" + "o" * 80 + "\n"
       "#HttpOnly_www.humblebundle.com\tFALSE\t/\tTRUE\t1893456000\tory_session_stoiccohens510ht9xek\t" + COOKIE + "\n"
       ".humblebundle.com\tTRUE\t/\tFALSE\t1893456000\tcsrf_cookie\ta-made-up-other-cookie\n")


def test_a_login_from_standard_input_is_checked_saved_and_never_shown(monkeypatch, capsys):
    """hylly login SITE --stdin, as docker login --password-stdin takes a password: from a pipe or a file, for a script
    or a container's setup; never from the keyboard, where it would show as it's typed."""
    humble_login(monkeypatch, ["a", "b"])
    stdin_of(monkeypatch, b"\xef\xbb\xbf" + COOKIE.encode("utf-8") + b"\r\n")  # as PowerShell may pipe it
    cli.main(["login", "humble", "--stdin"])
    said = capsys.readouterr()
    assert logins.load_session("humble") == COOKIE and COOKIE not in said.out + said.err
    assert "Read 80 characters from standard input." in said.err and "you're signed in" in said.out
    monkeypatch.setattr(cli.sys, "stdin", Keyboard(COOKIE))
    code, said = exit_of(capsys, "login", "humble", "--stdin")
    assert code == exits.USAGE and "--stdin reads the login from a pipe or a file, not the keyboard" in said
    stdin_of(monkeypatch, b"my shopping list\n")
    monkeypatch.setattr("hylly.humble.HumbleClient", lambda cookie: pytest.fail("nothing should be sent"))
    assert exit_of(capsys, "login", "humble", "--stdin")[0] == exits.LOGIN and logins.load_session("humble") == COOKIE
    assert exit_of(capsys, "login", "humble", "--stdin", "--clipboard")[0] == exits.USAGE  # one way at a time


def test_a_login_from_the_browser_s_cookies_txt(monkeypatch, capsys, tmp_path):
    """--cookies FILE, as yt-dlp takes one: the shop's own login cookie, of those in the file the one that lasts
    longest, never another site's; the file named so its owner can delete it; - reads it from standard input."""
    humble_login(monkeypatch, ["a"])
    jar = tmp_path / "cookies.txt"
    jar.write_text(JAR, encoding="utf-8")
    jar.chmod(0o600)
    cli.main(["login", "humble", "--cookies", str(jar)])
    said = capsys.readouterr()
    assert logins.load_session("humble") == COOKIE and COOKIE not in said.out + said.err and "o" * 80 not in said.err
    assert f"Read the ory_session_stoiccohens510ht9xek cookie, 80 characters, from {jar}. It holds your logins to every site" in said.err
    logins.session_path("humble").unlink()
    stdin_of(monkeypatch, JAR.encode("utf-8"))
    cli.main(["login", "humble", "--cookies", "-"])
    assert logins.load_session("humble") == COOKIE and "from standard input." in capsys.readouterr().err
    logins.session_path("humble").unlink()
    odd = "".join(f".humblebundle.com\tTRUE\t/\tTRUE\t{until}\tory_session_stoiccohens510ht9xek\t{'x' * 80}\n" for until in ("²", "9" * 5000, "-1"))
    jar.write_text(odd + JAR, encoding="utf-8")  # an until that is no time is a line passed over, not a crash
    cli.main(["login", "humble", "--cookies", str(jar)])
    assert logins.load_session("humble") == COOKIE
    capsys.readouterr()
    jar.write_text(JAR.replace("humblebundle.com", "elsewhere.example"), encoding="utf-8")
    code, said = exit_of(capsys, "login", "humble", "--cookies", str(jar))
    assert code == exits.LOGIN and f"No ory_session_stoiccohens510ht9xek cookie of humblebundle.com in {jar}" in said and "made-up" not in said
    code, said = exit_of(capsys, "login", "metron", "--cookies", str(jar))
    assert code == exits.USAGE and "metron takes an API token: give it with --stdin or --clipboard" in said
    assert exit_of(capsys, "login", "humble", "--cookies", str(tmp_path / "gone.txt"))[0] == exits.USAGE
    code, said = exit_of(capsys, "-H", "server-made-up", "login", "humble", "--cookies", str(jar))
    assert code == exits.USAGE and "which can't see" in said and "--cookies -" in said and "Nothing was run" in said


def test_the_login_prompt_and_itch_io_s_refusal_say_where_each_browser_keeps_the_cookie_as_the_help_does(monkeypatch, capsys):
    """Chrome and Edge, Firefox, and Safari on a Mac with its Develop menu: the words of the help's "Not logged in",
    which the prompt and itch.io's refusal once said for Chrome, Edge and Firefox alone."""
    import html

    from hylly import itchio_survey, sites

    page = html.unescape(re.sub(r"<[^>]+>", "", Path(cli.__file__).with_name("help.html").read_text(encoding="utf-8")))
    assert sites.COOKIE_PLACES in page.replace(" → ", " > ") and "Safari" in sites.COOKIE_PLACES
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr(cli, "_hidden", lambda prompt, command: (_ for _ in ()).throw(Exit(exits.NEEDS_YOU, "Nobody pasted it.")))
    code, said = exit_of(capsys, "login", "humble")
    assert code == exits.NEEDS_YOU and f"(Where each browser keeps it, under www.humblebundle.com: {sites.COOKIE_PLACES}.)" in said
    assert f"(under https://itch.io: {sites.COOKIE_PLACES})" in itchio_survey.COOKIE_REFUSED


def test_a_saved_login_is_forgotten_with_how_to_end_it_at_the_site(monkeypatch, capsys):
    """hylly login SITE --forget deletes its file, and says that it works at the site until it's ended there, and how:
    logging out of a cookie's shop, deleting a key where it was made. A variable set for it, which stands in for the
    file, is said too, never its value; --forget-all deletes every one; a dry run deletes nothing. Made-up values, in
    the test's own logins folder."""
    from hylly.sites import ENDED_THERE, LOOKUPS

    cookie, key = "m" * 80, "k" * 40
    logins.save_session(cookie, "humble")
    cli.main(["login", "humble", "--forget", "-n"])
    path = logins.session_path("humble")
    assert capsys.readouterr().out == f"It would delete {path}, your Humble login.\nNothing was deleted.\n" and path.is_file()
    cli.main(["login", "humble", "--forget"])
    said = capsys.readouterr()
    assert said.out == (f"Deleted {path}, your Humble login. It works at the site until it's ended there: log out of "
                        "humblebundle.com in the browser it came from, or change your password there.\n") and said.err == ""
    assert not path.exists()
    with pytest.raises(AuthError, match="No humble session cookie saved"):  # Hylly has it no more
        logins.load_session("humble")
    monkeypatch.setenv("HYLLY_HUMBLE_SESSION", cookie)  # set, it stands in for the file: said, and the run ends well
    cli.main(["login", "humble", "--forget"])
    said = capsys.readouterr()
    assert said.out == f"No humble login is saved here, in {path}: nothing was deleted.\n"
    assert "HYLLY_HUMBLE_SESSION is set here, and Hylly uses it in place of the file: unset it too" in said.err and cookie not in said.err
    monkeypatch.delenv("HYLLY_HUMBLE_SESSION")
    for site in ("metron", "googlebooks", "drivethrurpg"):
        logins.save_session(key, site)
    logins.save_session("i" * 40, "itchio-bundles")
    cli.main(["login", "metron", "--forget"])
    assert "your Metron API token. It works at the site until it's ended there: delete it where you made it, at metron.cloud, " \
           "on your account page, if the site offers that." in capsys.readouterr().out
    cli.main(["login", "--forget-all"])
    said = capsys.readouterr().out
    assert said.count("Deleted ") == 3 and "at console.cloud.google.com" in said and "Library App Keys" in said
    assert "log out of itch.io" in said and key not in said and not list(paths.logins_dir().iterdir())
    cli.main(["login", "--forget-all"])
    assert capsys.readouterr().out == f"No login or key is saved here, in {paths.logins_dir()}: nothing was deleted.\n"
    for words in (["login", "--forget"], ["login", "humble", "--forget-all"], ["login", "humble", "--forget", "--clipboard"]):
        assert exit_of(capsys, *words)[0] == exits.USAGE, words
    assert set(logins.SITES) == set(ENDED_THERE) | {sid for sid, row in LOOKUPS.items() if row.key}  # words for every one
    row = next(c for c in as_json(capsys, "commands", "--json")["commands"] if c["command"] == "hylly login")
    assert "--forget and --forget-all need nobody" in row["person"] and {"--forget", "--forget-all"} <= {o["option"] for o in row["options"]}


def test_a_saved_login_that_is_a_link_is_forgotten_and_the_file_it_leads_to_is_named(monkeypatch, capsys, tmp_path):
    """A login's file may be a link to one kept elsewhere: --forget deletes the link, and says that the file it led to
    stays, with the login in it. Made-up values, in the test's own folders."""
    kept = tmp_path / "elsewhere" / "humble.txt"
    kept.parent.mkdir()
    kept.write_text("m" * 80, encoding="utf-8")
    path = logins.session_path("humble")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(kept)
    except OSError:
        pytest.skip("this computer makes no links here")
    cli.main(["login", "humble", "--forget", "-n"])
    assert f"a link to {os.path.realpath(kept)}, which holds it and stays" in capsys.readouterr().out and path.is_symlink()
    cli.main(["login", "humble", "--forget"])
    said = capsys.readouterr().out
    assert said.startswith(f"Deleted {path}, your Humble login, a link to {os.path.realpath(kept)}, which holds it and stays: "
                           "delete that file too, if it should go. It works at the site")
    assert not path.is_symlink() and kept.read_text(encoding="utf-8") == "m" * 80 and "m" * 80 not in said


@pytest.mark.skipif(sys.platform == "win32", reason="the rights of Linux and macOS")
def test_a_secret_s_file_others_may_read_is_refused(monkeypatch, capsys, tmp_path):
    humble_login(monkeypatch, ["a"])
    jar = tmp_path / "cookies.txt"
    jar.write_text(JAR, encoding="utf-8")
    jar.chmod(0o644)
    code, said = exit_of(capsys, "login", "humble", "--cookies", str(jar))
    assert code == exits.USAGE and "may be read by others on this computer (its rights are 644)" in said and "chmod 600" in said
    assert not logins.session_path("humble").exists()
    password = tmp_path / "password.txt"
    password.write_text("a made-up password\n", encoding="utf-8")
    password.chmod(0o640)
    with open(password, encoding="utf-8") as fh:
        monkeypatch.setattr(cli.sys, "stdin", fh)
        code, said = exit_of(capsys, "user", "add", "ann", "--password-stdin")
    assert code == exits.USAGE and "The file on standard input may be read by others" in said and "ann" not in access.load_accounts()
    password.chmod(0o600)
    with open(password, encoding="utf-8") as fh:
        monkeypatch.setattr(cli.sys, "stdin", fh)
        cli.main(["user", "add", "ann", "--password-stdin"])
    assert access.check_password("a made-up password", access.load_accounts()["ann"][1])


def test_a_user_and_their_password_from_standard_input(monkeypatch, capsys):
    """hylly user add NAME --password-stdin: a container's first user, or a script's; one line, read once, never shown."""
    stdin_of(monkeypatch, b"a made-up password\n")
    cli.main(["user", "add", "ann", "--password-stdin"])
    said = capsys.readouterr()
    accounts = access.load_accounts()
    assert accounts["ann"][0] == "owner" and access.check_password("a made-up password", accounts["ann"][1])
    assert "a made-up password" not in said.out + said.err and "Added ann as owner" in said.out
    for given, words in ((b"short\n", "at least 8 characters"), (b"a made-up password\nand more\n", "takes one line")):
        stdin_of(monkeypatch, given)
        code, said = exit_of(capsys, "user", "add", "bob", "--password-stdin")
        assert code == exits.USAGE and words in said and "bob" not in access.load_accounts() and "made-up" not in said
    monkeypatch.setattr(cli.sys, "stdin", Keyboard("a made-up password\n"))
    code, said = exit_of(capsys, "user", "add", "bob", "--password-stdin")
    assert code == exits.USAGE and "not the keyboard, where it would show as it's typed" in said
    stdin_of(monkeypatch, b"a made-up password\n")
    cli.main(["user", "add", "bob", "--password-stdin", "--dry-run"])
    assert "It would read a password for bob" in capsys.readouterr().out and "bob" not in access.load_accounts()
    stdin_of(monkeypatch, b"another made-up password\r\n")  # an existing user: a new password, as without the option
    cli.main(["user", "add", "ann", "--password-stdin"])
    assert access.check_password("another made-up password", access.load_accounts()["ann"][1])


# ---- options that would do nothing ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["humble", "seed", "x", "--device", "tablet"], ["itchio", "survey", "--yes"], ["itchio", "download", "--yes"],
                                  ["humble", "seed", "x", "--limit", "3"], ["login", "humble", "--json"], ["user", "add", "someone", "--yes"],
                                  ["tag", "add", "a-tag", "--json"], ["tag", "list", "--search", "the"], ["reviews", "export", "--json"],
                                  ["reviews", "list", "--out", "f.md"], ["theme", "list", "my.toml"], ["itchio", "claim", "--again"]])
def test_an_option_that_would_be_ignored_is_refused(argv, capsys):
    """Each action takes only its own options, so argparse refuses another's: exit code 2, and nothing done."""
    code, said = exit_of(capsys, *argv)
    assert code == exits.USAGE and "unrecognized arguments" in said


class Shop:
    """Any shop, with an empty library."""

    def __init__(self, login, **options):  # options: delay, tries, as a login's check gives them
        self.unreadable = []  # DriveThruRPG's products it couldn't read: none

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def bundles(self, progress=None):
        return []

    def login(self):
        pass

    def products(self, progress=None):
        return []

    def check(self):  # Humble's login: you're signed in
        pass

    def order_keys(self):  # Humble's
        return []

    def orders(self, keys, progress=None):
        return {}, []


@pytest.mark.parametrize("shop", ["humble", "storybundle", "drivethrurpg", "itchio"])
def test_a_dry_run_at_a_shop_leaves_no_snapshot(tmp_path, monkeypatch, capsys, shop):
    from hylly import drivethrurpg, humble, itchio, shopping, snapshot, storybundle

    module, name = {"humble": (humble, "HumbleClient"), "storybundle": (storybundle, "StoryBundle"),
                    "drivethrurpg": (drivethrurpg, "DriveThruRPG"), "itchio": (itchio, "Itch")}[shop]
    monkeypatch.setenv({"humble": "HYLLY_HUMBLE_SESSION", "storybundle": "HYLLY_STORYBUNDLE_SESSION",
                        "drivethrurpg": "HYLLY_DRIVETHRURPG_KEY", "itchio": "HYLLY_ITCHIO_KEY"}[shop], COOKIE)
    monkeypatch.setattr(module, name, Shop)
    monkeypatch.setattr(itchio, "library", lambda itch, state, **kw: [])  # keep=False: a dry run adds no answers
    monkeypatch.setattr(shopping, "snapshot", lambda *a: pytest.fail("a dry run leaves no snapshot"))  # the core's,
    monkeypatch.setattr(snapshot, "save", lambda *a: pytest.fail("a dry run leaves no snapshot"))  # and Humble's orders
    cli.main([shop, "download", "--dry-run", "--library", str(tmp_path)])
    assert "snapshot" not in capsys.readouterr().out


@pytest.mark.parametrize("words", [["storybundle", "download"], ["drivethrurpg", "download"], ["itchio", "download"],
                                   ["itchio", "claim"], ["humble", "download"]])
def test_a_dry_run_that_asks_a_shop_waits_its_turn(words, tmp_path, monkeypatch, capsys):
    """Two at once would ask a shop's site twice as fast, a sure way to get blocked: a dry run that reads it is refused
    while a run of that shop goes, here or on another computer working on the library, and takes no lock of its own."""
    from hylly import instance

    monkeypatch.setattr(cli, "load_session", lambda *a: pytest.fail("the shop isn't asked"))
    job = words[0]
    with runs.only_one(job):
        code, said = exit_of(capsys, *words, "-n", "--library", str(tmp_path))
        assert code == exits.BUSY and "already running on this computer" in said
    assert not list(tmp_path.iterdir())  # nothing in the library
    locks = paths.state_dir(tmp_path) / "locks"  # another computer's run of it, on the same library
    locks.mkdir(parents=True)
    (locks / f"{job}.json").write_text(json.dumps({"host": "the-other-pc", "computer": "another", "pid": 1, "started": "2026-01-01 10:00"}))
    with open(locks / f"{job}.lock", "a+b") as fh:
        assert instance._lock(fh)
        code, said = exit_of(capsys, *words, "-n", "--library", str(tmp_path))
        instance._unlock(fh)
    assert code == exits.BUSY and "the-other-pc" in said
    assert sorted(p.name for p in locks.iterdir()) == [f"{job}.json", f"{job}.lock"]  # its note left as it was


def test_counting_on_makes_lines_in_a_log(capsys):
    show = runs.counting("items")
    for n in range(1, 251):
        show(n)
    said = capsys.readouterr()
    out = said.err  # progress, on standard error
    assert said.out == "" and "\r" not in out and out.splitlines() == ["  100 items", "  200 items"]


def test_progress_makes_lines_in_a_log(capsys):
    for n in range(1, 101):
        cli._progress(n, 100)
    said = capsys.readouterr()
    out = said.err  # progress, on standard error
    assert said.out == "" and "\r" not in out and len(out.splitlines()) == 10 and out.splitlines()[-1].strip() == "100/100"


# ---- as JSON -------------------------------------------------------------------------------------------------

def as_json(capsys, *argv) -> dict:
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out)


def test_user_list_as_json_has_no_password_hashes(capsys):
    access.add_user("someone", "correct horse", role="owner")
    data = as_json(capsys, "user", "list", "--json")
    assert data["users"] == [{"name": "someone", "role": "owner"}] and "hash" not in json.dumps(data) and "$" not in json.dumps(data)


def test_bell_as_json_masks_the_topic(monkeypatch, capsys):
    secret = "https://my.example/a-long-secret-topic-" + "x" * 12
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": secret, "ntfy_token": "a-token"})
    data = as_json(capsys, "bell", "--json")
    said = json.dumps(data)
    assert secret not in said and "a-token" not in said and data["ntfy"] == "https://my.example/...xxxx" and data["remote_detail"] == "counts"


def test_a_listing_is_made_once_and_its_json_and_its_lines_say_the_same(library, users, capsys, monkeypatch):
    """hylly tag list, hylly user list and hylly bell show make what they list once, then print it as JSON or as lines,
    so the two can't come to disagree, and what they read (the catalog, the accounts, the alarms' log) is read once."""
    root = ["--library", str(library)]
    cli.main(["tag", "add", "Lantern", "--search", "the", "--yes", *root])
    capsys.readouterr()
    got, read = Catalog.get, []
    monkeypatch.setattr(Catalog, "get", lambda self: read.append("catalog") or got(self))
    tags = as_json(capsys, "tag", "list", "--json", *root)["tags"]
    cli.main(["tag", "list", *root])
    assert [line.split(None, 1) for line in capsys.readouterr().out.splitlines()] == [[str(t["titles"]), t["tag"]] for t in tags]
    titles = as_json(capsys, "tag", "list", "lantern", "--json", *root)["titles"]
    cli.main(["tag", "list", "lantern", *root])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{len(titles)} titles tagged lantern:" and [line.split(" - ")[0].strip() for line in lines[1:]] == [
        t["title"] for t in titles] and len(titles) > 1
    assert read == ["catalog"] * 4
    accounts = access.load_accounts
    monkeypatch.setattr(access, "load_accounts", lambda: read.append("accounts") or accounts())
    names = [u["name"] for u in as_json(capsys, "user", "list", "--json")["users"]]
    cli.main(["user", "list"])
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()[:len(names)]] == names == sorted(users)
    assert read.count("accounts") == 2
    bell.log_file().parent.mkdir(parents=True, exist_ok=True)
    bell.log_file().write_text("2026-11-01 10:00 A test alarm\n", encoding="utf-8")
    tail = access.tail
    monkeypatch.setattr(access, "tail", lambda *a: read.append("log") or tail(*a))
    latest = as_json(capsys, "bell", "show", "--json")["latest"]
    cli.main(["bell", "show"])
    assert capsys.readouterr().out.endswith("Latest:\n  " + "\n  ".join(latest) + "\n") and latest
    assert read.count("log") == 2


def test_the_server_s_status_as_json(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["server", "status", "--json"])
    assert e.value.code == exits.PARTLY and json.loads(capsys.readouterr().out)["running"] is False
    assert exit_of(capsys, "serve", "--json")[0] == exits.USAGE and exit_of(capsys, "server", "stop", "--json")[0] == exits.USAGE


def test_verify_as_json_has_the_problems(tmp_path, capsys, monkeypatch):
    from hylly import copies

    monkeypatch.setattr(copies, "verify", lambda root, **k: (2, [("own/a.epub", "missing")]))
    with pytest.raises(SystemExit) as e:
        cli.main(["verify", "--json", "--library", str(tmp_path)])
    assert e.value.code == exits.PARTLY
    assert json.loads(capsys.readouterr().out) == {"checked": 2, "with_md5": False, "problems": [{"path": "own/a.epub", "problem": "missing"}],
                                                   "removed": 0,  # how many missing you removed: none
                                                   "no_record": None}  # how many of your own files it can't check: no build yet


def test_json_is_all_that_comes_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Catalog, "get", lambda self: [{"id": "a", "t": "A Title"}])
    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], ["Metron is left out: no token saved."]))
    monkeypatch.setattr(lookups, "dry_run", lambda *a, **k: {})
    assert as_json(capsys, "lookup", "--dry-run", "--json", "--library", str(tmp_path))["left_out"] == ["Metron is left out: no token saved."]
    monkeypatch.setattr(bell, "settings", lambda: {**bell.DEFAULTS, "ntfy": "https://ntfy.sh/abc"})
    assert as_json(capsys, "bell", "--json")["weak_topic"] is True


@pytest.fixture
def a_library(tmp_path, monkeypatch):
    """A small made-up library with its catalog built, and a folder of books of one's own beside it to copy."""
    from hylly import catalog

    root = tmp_path / "library"
    build_library(root)
    catalog.build(root, log=lambda s: None)
    books = tmp_path / "books"
    books.mkdir()
    (books / "Ann Writer - Lantern Wolf.epub").write_bytes(b"a made-up book")
    monkeypatch.setenv("HYLLY_LIBRARY", str(root))
    return root, books, tmp_path


@pytest.mark.parametrize("words", [["catalog"], ["catalog", "-n"], ["rules", "show", "genres"], ["rules", "try", "Lantern Wolf 003 (2026).cbz"],
                                   ["add", "ebooks", "--from", "{books}", "-n"], ["add", "ebooks", "--from", "{books}"],
                                   ["copy", "{books}", "{tmp}/copy", "-n"], ["copy", "{books}", "{tmp}/copy", "-y"],
                                   ["backup", "{tmp}/backup", "-n"], ["backup", "{tmp}/backup", "-y"], ["download", "-n"],
                                   ["add", "ebooks", "--from", "{tmp}/not there"], ["copy", "{books}", "{tmp}/a file/below", "-y"],
                                   ["humble", "download", "-n"], ["humble", "seed", "{books}", "-n"],
                                   ["itchio", "survey", "-n"], ["tag", "suggest", "-n"], ["theme", "check", "{theme}"],
                                   ["theme", "check", "{tmp}/no such theme.toml"]])
def test_a_command_run_with_json_prints_only_its_one_object_on_standard_output(words, a_library, monkeypatch, capsys):
    """Its result, with its exit code and what that means, however it ends; what it says on the way goes to standard
    error, so that hylly X --json > file has the object alone."""
    from hylly import themes

    root, books, tmp = a_library
    (tmp / "a file").write_text("a file, not a folder: no copy can go below it", encoding="utf-8")
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)
    argv = [w.format(books=books, tmp=tmp, theme=themes.SHIPPED / "birch.toml") for w in words] + ["--json"]
    code = 0
    try:
        cli.main(argv)
    except SystemExit as e:
        code = e.code
    said = capsys.readouterr()
    found = json.loads(said.out)  # the whole of standard output is one object
    assert found["command"] == "hylly " + (" ".join(words[:2]) if words[0] in ("rules", "humble", "itchio", "tag", "theme") else words[0])
    assert found["exit_code"] == code and found["meaning"] == exits.TABLE[code], (words, found)
    if code:
        assert found["message"] and found["message"] in said.err
    else:
        assert said.err  # it says what it does, on standard error


def test_every_command_that_reports_a_result_takes_json_and_a_bug_is_one_object_too(a_library, monkeypatch, capsys):
    assert set(cli.ONE_OBJECT) <= set(commands.taking("--json"))
    listed = {c["command"]: [o["option"] for o in c["options"]] for c in commands.commands_data()["commands"]}
    assert all("--json" in listed[f"hylly {name}"] for name in cli.ONE_OBJECT)
    monkeypatch.setattr(cli, "cmd_catalog", lambda args: {}["no such key"])
    with pytest.raises(SystemExit) as e:
        cli.main(["catalog", "--json"])
    said = capsys.readouterr()
    found = json.loads(said.out)
    assert e.value.code == exits.BUG and found["exit_code"] == exits.BUG and found["message"] == exits.BUG_WORDS
    assert "KeyError" in said.err and "KeyError" not in said.out


SERVE_IT = "The library page comes from the library's server: run hylly serve and open the address it shows."


def test_hylly_catalog_ends_with_where_the_library_page_shows_it(a_library, capsys, monkeypatch):
    """The page comes from the library server alone: hylly catalog's last line is the address of the one running for
    this library, as other devices reach it when they can, else how to start one; never a file to open."""
    from hylly import instance

    root, books, tmp = a_library
    running = {"pid": 1, "port": 8840, "listen": "127.0.0.1", "library": str(root)}
    monkeypatch.setattr(instance, "running", lambda: running)
    cli.main(["catalog"])
    assert capsys.readouterr().out.splitlines()[-1] == "The library page shows it at http://127.0.0.1:8840/"
    running.update(listen="0.0.0.0", address="https://books.example.org/")
    cli.main(["catalog", "--page"])
    assert capsys.readouterr().out.splitlines()[-2:] == [f"Made again from the database: {root / 'catalog' / 'data.js'}.",
                                                         "The library page shows it at https://books.example.org/"]
    running.update(library=str(tmp / "another library"))  # a server for another library
    cli.main(["catalog", "--page"])
    assert capsys.readouterr().out.splitlines()[-1] == SERVE_IT
    monkeypatch.setattr(instance, "running", lambda: None)
    cli.main(["catalog", "--page"])
    assert capsys.readouterr().out.splitlines()[-1] == SERVE_IT and "index.html" not in SERVE_IT


def test_a_catalog_of_an_empty_library_ends_with_how_books_come_in(tmp_path, monkeypatch, capsys):
    """A library with no titles, as with no shop's login yet, isn't left at "0 titles": the last line says how books
    come in. One with titles ends as before (above)."""
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setenv("HYLLY_LIBRARY", str(root))
    cli.main(["catalog"])
    assert capsys.readouterr().out.splitlines()[-2:] == [
        SERVE_IT, f"Next: hylly login SHOP and hylly download for your books at a shop, or put a folder of your own books in "
                  f"{root}; then hylly catalog again."]


def test_a_build_kept_whose_data_js_can_t_be_written_says_so_and_how_to_write_it(a_library, capsys, monkeypatch):
    """The database has the new build even when catalog/data.js can't be replaced just then (on Windows, while
    another program has it open): the message says the build is kept, and hylly catalog --page writes the file again."""
    from hylly import catalog

    root, books, tmp = a_library
    data = root / "catalog" / "data.js"
    before, real = data.read_bytes(), catalog.atomic_write

    def held_open(path, text, **more):
        if path.name == "data.js":
            raise PermissionError(13, "Access is denied", str(path))
        return real(path, text, **more)
    monkeypatch.setattr(catalog, "atomic_write", held_open)
    (root / "comics").mkdir(exist_ok=True)
    (root / "comics" / "Lantern Wolf 1.cbz").write_bytes(b"not much of a comic")
    with pytest.raises(SystemExit) as e:
        cli.main(["catalog"])
    said = capsys.readouterr().err
    assert e.value.code == exits.LOCAL and "The catalog was built and kept in the database, but " in said
    assert "Access is denied" in said and "hylly catalog --page writes it again" in said
    with catalog.store(root).read() as c:
        kept = [t["t"] for t in catalog.titles(c)]
    assert "Lantern Wolf 1" in kept and data.read_bytes() == before  # in the database; the server's file as it was
    monkeypatch.setattr(catalog, "atomic_write", real)
    cli.main(["catalog", "--page"])
    assert "Lantern Wolf 1" in data.read_text(encoding="utf-8") and data.read_bytes() != before


def test_progress_and_notes_go_to_standard_error_and_the_result_to_standard_output(a_library, capsys):
    """Piped or in a file, standard output has the command's result only."""
    root, books, tmp = a_library
    cli.main(["catalog"])
    said = capsys.readouterr()
    assert said.out.splitlines()[-1] == SERVE_IT and "Collecting titles..." in said.err
    assert said.out.splitlines()[0].lstrip().startswith("titles: ")  # its counts are its result too
    cli._progress(5, 10)
    said = capsys.readouterr()
    assert said.out == "" and "5/10" in said.err


def test_hylly_settings_shows_every_setting_with_where_it_comes_from(monkeypatch, capsys, tmp_path):
    """The file, the environment or the default, and never the ntfy topic: neither in its file in secrets/, whose place is
    shown, nor under [bell] where an older Hylly kept it. hylly rules alone no longer lists them."""
    from hylly import settings

    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text('[server]\nport = 8820\n\n[bell]\nntfy = "https://ntfy.sh/A-MADE-UP-SECRET-TOPIC"\n',
                                     encoding="utf-8")
    settings.secrets_dir().mkdir(parents=True, exist_ok=True)
    (settings.secrets_dir() / "ntfy-url").write_text("https://ntfy.sh/A-MADE-UP-SECRET-TOPIC\n", encoding="utf-8")
    monkeypatch.setenv("HYLLY_LIBRARY", str(tmp_path))
    cli.main(["settings", "show"])
    out = capsys.readouterr().out
    assert re.search(r"^    port = 8820 +# settings\.toml$", out, re.M) and re.search(r"^    listen = \"127\.0\.0\.1\" +# default$", out, re.M)
    assert re.search(r"^    library = .* +# environment: HYLLY_LIBRARY$", out, re.M) and "A-MADE-UP-SECRET" not in out
    assert re.search(rf"^    ntfy-url +# set: {re.escape(str(settings.secrets_dir() / 'ntfy-url'))}$", out, re.M)
    assert re.search(r"^    ntfy-token +# not set$", out, re.M)
    cli.main(["settings"])  # alone: show
    assert capsys.readouterr().out == out
    rows = json.loads((cli.main(["settings", "show", "--json"]), capsys.readouterr().out)[1])["settings"]
    assert {"section": "server", "key": "port", "value": "8820", "from": "settings.toml"} in rows
    assert "A-MADE-UP-SECRET" not in json.dumps(rows)
    cli.main(["settings", "path"])
    assert capsys.readouterr().out.strip() == str(paths.settings_file())
    cli.main(["rules"])
    out = capsys.readouterr().out
    assert "port = " not in out and "hylly settings show" in out


def test_hylly_settings_check_lists_every_mistake_not_only_the_first(capsys):
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text('[reader]\ncache_gb = "lots"\n\n[server]\nport = 99999\naccess_log = "false"\nsomething = 1\n',
                                     encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(["settings", "check"])
    out = capsys.readouterr().out
    assert e.value.code == exits.NOT_SET_UP and "3 mistakes" in out
    assert "cache_gb" in out and "port = 99999" in out and "true or false, without quotes" in out and "something isn't one of" in out
    with pytest.raises(SystemExit):
        cli.main(["settings", "check", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert len(found["mistakes"]) == 3 and len(found["left_out"]) == 1
    paths.settings_file().write_text("this isn't toml\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(["settings", "check"])
    assert e.value.code == exits.NOT_SET_UP and "not readable as TOML" in capsys.readouterr().out
    paths.settings_file().write_text('[server]\nport = 8820\n', encoding="utf-8")
    cli.main(["settings", "check"])
    assert "it reads fine" in capsys.readouterr().out


def test_hylly_settings_edit_opens_the_editor_asked_for_and_checks_what_was_written(monkeypatch, capsys, tmp_path):
    from hylly import settings

    paths.settings_file().unlink(missing_ok=True)
    named = str(tmp_path / "my-editor") if os.name == "nt" else "my-editor"  # on Windows a bare name is looked for (below)
    monkeypatch.setenv("VISUAL", f"{named} --wait")
    cli.main(["settings", "edit", "-n"])
    assert f"in {named} --wait, after making it, empty" in capsys.readouterr().out and not paths.settings_file().exists()
    with pytest.raises(SystemExit) as e:  # nobody at the keyboard: an editor is for a person
        cli.main(["settings", "edit"])
    assert e.value.code == exits.NEEDS_YOU and not paths.settings_file().exists()
    ran = []

    def editor(command, **kw):
        ran.append(command)
        Path(command[-1]).write_text("[server]\nport = 0\n", encoding="utf-8")  # a mistake, made in the editor
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setattr(subprocess, "run", editor)
    with pytest.raises(SystemExit) as e:
        cli.main(["settings", "edit"])
    assert ran == [[named, "--wait", str(paths.settings_file())]]
    assert e.value.code == exits.NOT_SET_UP and "1 mistake in it now" in capsys.readouterr().out
    settings.reload()


@pytest.mark.parametrize("before", [["--library", "{tmp}"], ["-H", ""], []])
def test_a_run_that_ends_before_it_starts_gives_its_one_object_too(before, a_library, monkeypatch, capsys):
    """A refused --library, an empty -H, settings that aren't TOML, a bug on the way: still the one object, with its code
    as any other end has it."""
    root, books, tmp = a_library
    words = ([w.format(tmp=tmp) for w in before] + ["copy", str(books), str(tmp / "copy"), "--json"] if before[:1] == ["--library"]
             else [*before, "catalog", "--json"])
    if not before:  # settings that can't be read at all: where the command runs can't be told
        paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
        paths.settings_file().write_text("this isn't toml\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(words)
    found = json.loads(capsys.readouterr().out)
    assert found["exit_code"] == e.value.code == (exits.NOT_SET_UP if not before else exits.USAGE) and found["message"]
    paths.settings_file().write_text("", encoding="utf-8")
    from hylly import remote

    monkeypatch.setattr(remote, "wanted", lambda *a: {}["a bug on the way"])
    with pytest.raises(SystemExit) as e:
        cli.main(["catalog", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert found["exit_code"] == e.value.code == exits.BUG and found["message"] == exits.BUG_WORDS


def test_a_lookup_s_and_a_catalog_s_counts_are_their_result(a_library, monkeypatch, capsys):
    """What was found, and how each source's turn ended: on standard output with the rest of the result; the run's own
    lines on the way stay on standard error."""
    root, books, tmp = a_library

    def run(root, queries, chosen, log, **kw):
        log("Metron: asking about 3 titles")
        log("Metron: 3 looked up, 2 found, 1 not found, 0 errors.")
        return {"metron": {"found": 2, "stopped": "", "asked": 3}}

    monkeypatch.setattr(lookups, "available", lambda chosen=None: ([], []))
    monkeypatch.setattr(lookups, "run", run)
    cli.main(["lookup"])
    said = capsys.readouterr()
    assert said.out.splitlines() == ["Metron: 3 looked up, 2 found, 1 not found, 0 errors.",
                                     "Found details for 2 titles. Run hylly catalog to show them in the page and the feeds."]
    assert "asking about 3 titles" in said.err
    cli.main(["catalog", "--rebuild"])
    said = capsys.readouterr()
    assert said.out.splitlines()[0].lstrip().startswith("titles: ") and said.out.splitlines()[-1] == SERVE_IT
    assert "Collecting titles" in said.err


def test_a_bug_in_one_shop_of_hylly_download_is_the_run_s_end_whatever_the_others_did(tmp_path, monkeypatch, capsys):
    shops_ending(monkeypatch, humble=ValueError("a bug"), storybundle=AuthError("Expired."))
    assert exit_of(capsys, "download", "--library", str(tmp_path))[0] == exits.BUG  # not 1, which would hide it


def ended_with_its_reader_gone(*argv, read: int = 0) -> tuple[int, str]:
    """A real hylly whose standard output is a pipe, closed by its reader after this many bytes (0: before anything
    came): its exit code, and what it said on standard error."""
    job = subprocess.Popen([sys.executable, "-m", "hylly", *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, env={**os.environ})
    if read:
        job.stdout.read(read)
    job.stdout.close()
    err = job.stderr.read().decode("utf-8", "replace")
    job.stderr.close()
    return job.wait(120), err


def test_a_reader_that_has_read_enough_gets_no_more_and_the_command_ends_as_it_would():
    """hylly X | head, and a short output whose reader went before it came (| true): no message of the pipe, no
    traceback, and the command's own exit code."""
    for argv, read in ((["commands"], 200), (["user", "list"], 0), (["commands", "--json"], 0)):
        code, err = ended_with_its_reader_gone(*argv, read=read)
        assert code == 0 and "Errno" not in err and "Traceback" not in err and "Exception ignored" not in err, (argv, err)


def test_a_catalog_whose_reader_has_gone_is_built_and_kept_all_the_same(a_library):
    """hylly catalog | true: the build is kept, and ends with its own code, which hylly jobs has. The reader going
    before its lines doesn't stop it half way, nor pass for a success."""
    root, books, tmp = a_library
    paths.marker_file(root).write_text('{"id": "a-made-up-library"}', encoding="utf-8")
    (root / "catalog" / "data.js").unlink()
    make_epub(root / "collections" / "Poetry" / "Ann Writer - The Lantern Keeper.epub")  # new: its line comes before the keeping
    code, err = ended_with_its_reader_gone("catalog", "--library", str(root))
    assert code == 0 and "Collecting titles" in err and "Traceback" not in err and "Exception ignored" not in err, err
    assert (root / "catalog" / "data.js").is_file() and "Lantern Keeper" in (root / "catalog" / "data.js").read_text(encoding="utf-8")
    (last,) = [r for r in runs.last_runs() if r["job"] == "catalog"]
    assert last["exit"] == code


class Failing:
    """Standard output over a real file or pipe, whose every write fails as Windows fails one: EINVAL."""

    def __init__(self, fd: int) -> None:
        self.fd, self.written = fd, []

    def fileno(self) -> int:
        return self.fd

    def write(self, text: str) -> int:
        raise OSError(errno.EINVAL, "Invalid argument")

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kw) -> None:
        pass


def test_only_a_real_pipe_s_reader_can_be_gone_a_file_s_trouble_is_said(tmp_path, monkeypatch, capsys):
    """On Windows EINVAL is what any failed write says: only on a pipe does it mean a reader that has gone. Standard
    output in a file, or a file on a share, keeps its errors: exit code 6, and words."""
    with open(tmp_path / "out.txt", "w") as fh:
        monkeypatch.setattr(sys, "stdout", Failing(fh.fileno()))
        with pytest.raises(SystemExit) as e:
            cli.main(["commands"])
    assert e.value.code == exits.LOCAL and "Invalid argument" in capsys.readouterr().err
    read, write = os.pipe()
    try:
        monkeypatch.setattr(sys, "stdout", Failing(write))
        cli.main(["commands"])  # its reader gone: quietly, with its own code
        assert "Invalid argument" not in capsys.readouterr().err
    finally:
        os.close(read)
        with contextlib.suppress(OSError):
            os.close(write)


def gone_pipe() -> io.TextIOWrapper:
    """A stream on a real pipe whose reader has gone, as head is once it has its lines."""
    read, write = os.pipe()
    os.close(read)
    return io.TextIOWrapper(io.FileIO(write, "wb"), encoding="utf-8")


def test_a_reader_gone_on_standard_error_too_ends_quietly_with_the_command_s_own_code(monkeypatch):
    """hylly X 2>&1 | head, as assistants pipe it: standard error on the pipe too, with progress still coming once its
    reader has gone. Once that ended in "Can't go on", which failed to be said in turn, and Python's own code 120."""
    stream = gone_pipe()
    try:
        monkeypatch.setattr(sys, "stderr", stream)
        monkeypatch.setattr(cli, "cmd_commands", lambda args: [cli._note(f"line {n} of the progress") for n in range(20000)])
        cli.main(["commands"])  # its own end, 0: no exit raised
        assert sys.stderr is stream  # handed back as it was
    finally:
        with contextlib.suppress(OSError):
            stream.close()


@pytest.mark.skipif(sys.platform == "win32", reason="a socket there is a handle, not a file descriptor")
def test_a_socket_s_reader_gone_ends_quietly(monkeypatch, capsys):
    """Standard output may be a socket too, as a service's log can be: its reader gone is a pipe's, quietly, with the
    command's own code."""
    import socket

    ours, theirs = socket.socketpair()
    theirs.close()
    stream = io.TextIOWrapper(io.BufferedWriter(io.FileIO(ours.detach(), "wb")), encoding="utf-8")
    try:
        with monkeypatch.context() as m:
            m.setattr(sys, "stdout", stream)
            m.setattr(cli, "cmd_commands", lambda args: [print("a row of the output " * 4) for _ in range(20000)])
            cli.main(["commands"])  # its own end, 0: no exit raised
        assert "Broken pipe" not in capsys.readouterr().err
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def test_another_program_s_broken_pipe_is_trouble_on_this_computer(monkeypatch, capsys):
    """A broken pipe of anything but standard output or error, a program Hylly writes to, is said with code 6: never
    taken for a reader that has read enough, which would pass for a success."""
    def elsewhere(args):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr(cli, "cmd_commands", elsewhere)
    code, said = exit_of(capsys, "commands")
    assert code == exits.LOCAL and "Can't go on: [Errno 32] Broken pipe" in said


@pytest.mark.parametrize(("raised", "code"), [("integrity", exits.BUG), ("no such table", exits.BUG), ("busy", exits.BUSY)])
def test_only_a_damaged_database_or_its_disk_s_trouble_gets_the_damaged_database_words(raised, code, monkeypatch, capsys):
    """A broken rule or a mistake in Hylly's own SQL is a bug, 70; busy is 4, try again; damage itself is 6 (above)."""
    import sqlite3

    def fails():
        c = sqlite3.connect(":memory:")
        if raised == "integrity":
            c.execute("CREATE TABLE t (a PRIMARY KEY)")
            c.execute("INSERT INTO t VALUES (1)")
            c.execute("INSERT INTO t VALUES (1)")
        elif raised == "no such table":
            c.execute("SELECT * FROM nothing_here")
        e = sqlite3.OperationalError("database is locked")
        e.sqlite_errorname = "SQLITE_BUSY"
        raise e

    monkeypatch.setattr(access, "load_accounts", fails)
    found, err = exit_of(capsys, "user", "list")
    assert found == code and ("nightly copies" in err) is False
    assert ("Traceback" in err) == (code == exits.BUG) and (code != exits.BUSY or "is busy" in err)
    if code == exits.BUSY:  # its own words for what the code means, in a result: not "the same job is already running"
        assert "in use by another program" in cli.LAST["meaning"]


@pytest.mark.parametrize("words", [["user", "role", "nobody", "reader"], ["user", "rename", "nobody", "other"], ["user", "remove", "nobody", "-y"],
                                   ["user", "link", "nobody"], ["user", "role", "nobody", "reader", "-n"], ["people", "export", "nobody"],
                                   ["people", "forget", "nobody", "-y"], ["user", "rename", "ann", "bob", "-n"],
                                   ["user", "restore", "nobody"], ["user", "restore", "ann", "-n"], ["people", "erase", "nobody", "-y"]])
def test_a_name_that_isn_t_there_is_a_mistake_not_partly_done(words, capsys):
    """No retry helps a name that isn't a user's, or one taken already: exit 2, with who there is."""
    access.add_user("ann", "correct horse", role="owner")
    access.add_user("bob", "correct horse", role="reader")
    code, err = exit_of(capsys, *words)
    assert code == exits.USAGE and ("hylly user list" in err or "hylly people list" in err or "give another name" in err), err
    with pytest.raises(Exit) as e:
        access.sign_in_link("nobody")
    assert e.value.code == exits.USAGE


def test_settings_edit_says_plainly_what_s_wrong_with_the_editor_named(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "at_keyboard", lambda: True)
    monkeypatch.setenv("VISUAL", 'my-editor "unclosed')
    code, err = exit_of(capsys, "settings", "edit")  # an unclosed quote is no command: the person's mistake, not a bug
    assert code == exits.USAGE and "can't be read as a command" in err and "Traceback" not in err
    if os.name != "nt":
        return
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: ran.append(command) or subprocess.CompletedProcess(command, 0))
    here, on_path = tmp_path / "here", tmp_path / "bin"
    here.mkdir()
    on_path.mkdir()
    (here / "my-editor.cmd").write_text("@echo off\n", encoding="utf-8")  # in the current folder only: never run from there
    monkeypatch.chdir(here)
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("VISUAL", "my-editor --wait")
    code, err = exit_of(capsys, "settings", "edit")
    assert code == exits.NOT_SET_UP and "isn't on the PATH" in err and ran == []
    (on_path / "my-editor.cmd").write_text("@echo off\n", encoding="utf-8")  # a .cmd on the PATH, as VS Code's code is
    (on_path / "my-editor").write_text("#!/bin/sh\n", encoding="utf-8")  # and beside it, as in VS Code's folder, a script for sh
    cli.main(["settings", "edit"])
    assert [os.path.normcase(w) for w in ran[0]] == [os.path.normcase(w) for w in (str(on_path / "my-editor.cmd"), "--wait", str(paths.settings_file()))]


def test_settings_show_flags_an_empty_hylly_remote(monkeypatch, capsys):
    monkeypatch.setenv("HYLLY_REMOTE", "")
    cli.main(["settings", "show"])
    assert re.search(r"^    ssh = \"\" +# environment: HYLLY_REMOTE, set and empty: a mistake", capsys.readouterr().out, re.M)


def test_json_is_in_plain_letters_for_a_pipe(capsys):
    cli._as_json({"title": "Härbor Wölf"})
    out = capsys.readouterr().out
    assert out.isascii() and json.loads(out) == {"title": "Härbor Wölf"}


def test_a_lookup_as_json_needs_a_dry_run(capsys, tmp_path):
    assert exit_of(capsys, "lookup", "--json", "--library", str(tmp_path))[0] == exits.USAGE


# ---- jobs and logs -------------------------------------------------------------------------------------------

def test_hylly_jobs_tells_what_runs_and_how_the_last_one_ended(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: None)
    assert as_json(capsys, "jobs", "--json") == {"running": [], "last": []}
    with runs.only_one("lookup"):
        monkeypatch.setattr(runs, "SAY_EVERY", 0)
        runs.progress("Metron: 40/100 looked up")
        data = as_json(capsys, "jobs", "--json")
        assert [j["job"] for j in data["running"]] == ["lookup"] and data["running"][0]["progress"] == "Metron: 40/100 looked up"
        assert data["running"][0]["stop"] == "hylly jobs stop lookup"
    assert as_json(capsys, "jobs", "--json")["running"] == []

    def does():
        raise AuthError("The login has expired.")

    job_doing(monkeypatch, does)
    assert exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))[0] == exits.LOGIN
    last = as_json(capsys, "jobs", "--json")["last"]
    assert last[0]["job"] == "storybundle" and last[0]["exit"] == exits.LOGIN
    cli.main(["jobs"])
    assert "exit code 3" in capsys.readouterr().out


def test_a_damaged_note_doesn_t_stop_hylly_jobs(capsys):
    runs.last_file("lookup").parent.mkdir(parents=True, exist_ok=True)
    runs.last_file("lookup").write_text("[1, 2]", encoding="utf-8")
    assert as_json(capsys, "jobs", "--json")["last"] == []


@pytest.mark.parametrize(("code", "noted"), [(exits.NEEDS_YOU, False), (exits.NOT_SET_UP, True), (exits.USAGE, False)])
def test_a_run_that_didn_t_start_is_no_last_run(tmp_path, monkeypatch, capsys, code, noted):
    said = []
    monkeypatch.setattr(bell, "job_note", lambda *a, **k: said.append(a))

    def does():
        raise Exit(code, "It didn't start.")

    job_doing(monkeypatch, does)
    assert exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))[0] == code
    assert as_json(capsys, "jobs", "--json")["last"] == [] and bool(said) == noted


def test_a_refused_run_is_no_last_run(tmp_path, monkeypatch, capsys):
    job_doing(monkeypatch, lambda: pytest.fail("it shouldn't run"))
    with runs.only_one("storybundle"):
        exit_of(capsys, "storybundle", "download", "--library", str(tmp_path))
    assert as_json(capsys, "jobs", "--json")["last"] == []


def test_hylly_logs(tmp_path, capsys):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "lookup-log.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    cli.main(["logs", "--library", str(tmp_path)])
    out = capsys.readouterr().out
    assert "lookup-log.txt" in out and "server.log" in out and "(none yet)" in out
    cli.main(["logs", "lookup", "--tail", "2", "--library", str(tmp_path)])
    assert capsys.readouterr().out.splitlines() == ["two", "three"]
    cli.main(["logs", "lookup", "--tail", "0", "--library", str(tmp_path)])
    assert capsys.readouterr().out.splitlines() == ["three"]
    cli.main(["logs", "bell"])
    assert "Nothing in" in capsys.readouterr().out
    assert exit_of(capsys, "logs", "lookup")[0] == exits.NOT_SET_UP  # no library set


# ---- hylly commands ------------------------------------------------------------------------------------------

def test_every_command_says_what_it_may_do(capsys):
    from hylly.effects import EFFECTS

    data = as_json(capsys, "commands", "--json")
    named = {c["command"] for c in data["commands"]}
    assert named == {f"hylly {name}" for name in EFFECTS if not name.startswith("internal ")}
    claim = next(c for c in data["commands"] if c["command"] == "hylly itchio claim")
    assert claim["account"] and claim["person"] and claim["dry_run"] == "reads the site" and {"--every", "-y, --yes"} <= {o["option"] for o in claim["options"]}
    assert "--again" not in {o["option"] for o in claim["options"]}  # that one is the survey's
    init = next(c for c in data["commands"] if c["command"] == "hylly init")
    assert "--yes" in init["person"] and {"-y, --yes", "--new"} <= {o["option"] for o in init["options"]}
    assert data["exit_codes"]["8"] and data["environment"]["HYLLY_NO_NOTIFY"]
    for c in data["commands"]:  # only what's marked account changes one
        assert c["account"] == (c["command"] == "hylly itchio claim")
    cli.main(["commands"])
    assert "CHANGES YOUR ACCOUNT" in capsys.readouterr().out


def test_what_changes_who_may_do_what_needs_a_person(capsys):
    """A new user, one brought back, one removed and a new role, an owner's above all: each the library's owner's to
    decide, so each needs a person, and hylly commands says so, as it says what asks first."""
    from hylly.effects import EFFECTS

    assert all(EFFECTS[name]["person"] for name in ("user add", "user restore", "user remove", "user role"))
    assert "--yes" in EFFECTS["user role"]["person"] and "user role" in commands.asks_first()
    data = as_json(capsys, "commands", "--json")
    role = next(c for c in data["commands"] if c["command"] == "hylly user role")
    assert "--yes" in role["person"] and "-y, --yes" in {o["option"] for o in role["options"]}
    assert "user role" in commands.epilog()  # among those hylly --help says ask first


def test_hylly_s_help_and_hylly_commands_name_the_file_for_ai_assistants(capsys):
    """AGENTS.md, in Hylly's source, is where an AI assistant starts: hylly --help ends with it, and hylly commands
    names it, as text and as JSON."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "AGENTS.md, in Hylly's source" in " ".join(capsys.readouterr().out.split()[-40:])
    cli.main(["commands"])
    assert capsys.readouterr().out.rstrip().endswith(commands.ASSISTANTS)
    assert "AGENTS.md" in as_json(capsys, "commands", "--json")["assistants"]


# ---- one start at logon --------------------------------------------------------------------------------------

def test_another_settings_folder_can_t_change_the_start_at_logon(monkeypatch, capsys):
    from hylly import instance

    monkeypatch.setattr(paths, "usual_settings_folder", REAL_USUAL_FOLDER)  # HYLLY_HOME and HYLLY_SETTINGS are the tests' own
    monkeypatch.setattr(instance, "_schtasks", lambda *a: pytest.fail("it shouldn't touch the real one"))
    monkeypatch.setattr(instance, "_systemctl", lambda *a: pytest.fail("it shouldn't touch the real one"))
    assert not paths.usual_settings_folder()
    assert instance.autostart_state() is None
    with pytest.raises(Exit) as e:
        instance.autostart_on(dry_run=False)
    assert e.value.code == exits.USAGE and "one for the whole user" in str(e.value)
    with pytest.raises(Exit):
        instance.autostart_off()
    assert exit_of(capsys, "server", "autostart", "off")[0] == exits.USAGE
    assert exit_of(capsys, "server", "autostart", "off", "--dry-run")[0] == exits.USAGE


def test_the_usual_settings_folder_is_known_however_it_is_named(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "usual_settings_folder", REAL_USUAL_FOLDER)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    usual = tmp_path / "AppData" / "Local" / "Hylly" if os.name == "nt" else tmp_path / ".local" / "share" / "Hylly"
    usual.mkdir(parents=True)
    (usual / "hylly.db").write_bytes(b"")  # an install's one folder, with Hylly's files in it: used all in one
    monkeypatch.delenv("HYLLY_SETTINGS")
    monkeypatch.delenv("HYLLY_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(usual.parent))
    if os.name != "nt":
        monkeypatch.delenv("LOCALAPPDATA")
    assert paths.config_dir() == usual and paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_HOME", str(usual / ".." / "Hylly"))  # the usual one, named by Hylly's own word, written otherwise
    assert paths.usual_settings_folder() and paths.usual_settings_folder(settings_file_counts=False)
    try:
        (tmp_path / "a-link").symlink_to(usual, target_is_directory=True)
    except OSError:
        pass  # this computer won't make links: the rest is still tried
    else:
        monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "a-link"))  # and through a link
        assert paths.usual_settings_folder()
    monkeypatch.setenv("HYLLY_SETTINGS", str(tmp_path / "elsewhere.toml"))  # a settings file of its own: not for the start
    assert not paths.usual_settings_folder() and paths.usual_settings_folder(settings_file_counts=False)  # at logon
    monkeypatch.delenv("HYLLY_SETTINGS")
    monkeypatch.setenv("HYLLY_HOME", str(tmp_path / "elsewhere"))
    assert not paths.usual_settings_folder() and not paths.usual_settings_folder(settings_file_counts=False)
    monkeypatch.delenv("HYLLY_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "elsewhere"))  # Windows' own word moves it there, and only there
    assert paths.usual_settings_folder() == (os.name != "nt")


def test_hylly_home_moves_the_database_logins_and_locks_on_every_system_and_wins_over_localappdata(monkeypatch, tmp_path):
    from hylly import db, instance

    home, windows_own = tmp_path / "home", tmp_path / "windows-own"
    monkeypatch.setenv("HYLLY_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(windows_own))
    monkeypatch.delenv("HYLLY_SETTINGS")
    db.close_all()
    assert paths.config_dir() == home  # the folder itself, not one above it
    access.add_user("reader", "correct horse")
    saved = logins.save_session("m" * 32, "drivethrurpg")
    with runs.only_one("catalog"):
        assert runs.lock_file("catalog").is_file()
    instance.Claim().release()
    assert db.file() == home / db.FILE and db.file().is_file()
    assert saved.parent == paths.logins_dir() == home / "logins"
    assert instance.lock_file() == home / "server.lock" and instance.lock_file().is_file()
    assert runs.lock_file("catalog").parent == home and paths.settings_file() == home / "settings.toml"
    assert not windows_own.exists()
    # Without it: an install's one folder, while it holds Hylly's files, LOCALAPPDATA's on Windows, its own word, and
    # ~/.local/share's elsewhere, whatever it says; none there, a folder for each kind instead
    monkeypatch.delenv("HYLLY_HOME")
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path / "a-home")
    db.close_all()
    usual = windows_own / "Hylly" if os.name == "nt" else tmp_path / "a-home" / ".local" / "share" / "Hylly"
    assert paths.place("data") not in (usual, home) and paths.all_in_one() is None
    usual.mkdir(parents=True)
    (usual / "settings.toml").write_text("", encoding="utf-8")
    paths.forget_one_folder()  # looked for once in a process: as the next command finds it
    assert paths.config_dir() == usual and paths.all_in_one() == usual
    access.add_user("reader", "correct horse")
    assert db.file() == usual / db.FILE and db.file().is_file()
    assert os.name == "nt" or not windows_own.exists()


# ---- hylly demo ----------------------------------------------------------------------------------------------

@pytest.fixture
def apart(monkeypatch, demo_made_once):
    """What hylly demo sets for its own process, put back afterwards; and its library, copied from the one drawn once
    (conftest.demo_made_once)."""
    monkeypatch.setenv("HYLLY_LIBRARY", "put back after the test")
    monkeypatch.setenv("HYLLY_HOME", os.environ["HYLLY_HOME"])
    monkeypatch.setenv("HYLLY_SETTINGS", os.environ["HYLLY_SETTINGS"])


def test_a_demo_library_is_real_and_apart(tmp_path, apart):
    settings_before = paths.settings_file()
    root = demo.run(str(tmp_path / "try"), 8779, serve_it=False, open_browser=False, log=lambda s: None)
    assert root == tmp_path / "try" / "library" and paths.config_dir() == tmp_path / "try" / "settings" and paths.settings_file() != settings_before
    assert paths.library_root() == root and not paths.library_missing(root) and demo.is_demo(tmp_path / "try")
    items = Catalog(root).get()
    titles = {i["t"] for i in items}
    assert len(items) == len(demo.BOOKS) + len(demo.COMICS) and "The Salt Ledger" in titles
    book = next(i for i in items if i["t"] == "The Salt Ledger")
    assert book["a"] and book.get("s") == "The Lantern Keepers" and book.get("d") and book.get("c")
    comics = [i for i in items if i.get("k") == "comic" or "Neon Rangers" in i["t"]]
    assert len(comics) >= 3
    assert not (root / "settings").exists()  # the server serves the library's files: its settings are beside it


def test_a_demo_s_server_stays_on_its_port_after_a_restart(tmp_path, apart, monkeypatch, capsys):
    from hylly import instance, settings

    seen = {}
    monkeypatch.setattr(serve, "run", lambda root, **k: seen.update(k, root=root))
    root = demo.run(str(tmp_path / "try"), 8780, open_browser=False, log=lambda s: None)
    assert seen["port"] == 8780 and seen["given"] == {"port": 8780, "library": str(root)}
    # a restart takes the settings, the demo's own: its port, this computer only, its library
    assert serve.options({}) == (8780, "127.0.0.1") and paths.library_root() == root
    monkeypatch.setattr(instance, "running", lambda: None)
    cli.main(["server", "restart", "--dry-run"])
    assert f"on port 8780, listening on 127.0.0.1, for the library in {root}" in capsys.readouterr().out
    paths.settings_file().write_text(paths.settings_file().read_text(encoding="utf-8") + 'name = "Moonwake Shelf"\n', encoding="utf-8")
    demo.run(str(tmp_path / "try"), 8781, serve_it=False, open_browser=False, log=lambda s: None)  # again, on another port
    assert serve.options({}) == (8781, "127.0.0.1") and settings.get("server")["name"] == "Moonwake Shelf"  # the rest stays


def test_a_demo_served_here_says_ctrl_c_in_this_window_stops_it_never_hylly_server_stop(tmp_path, apart, monkeypatch, capsys):
    """hylly server stop, typed in another window without the demo's settings folder set, can't see the demo's server,
    and would stop your own: the Stops line of a demo served here says how it stops, as one in the background says
    hylly demo --stop."""
    from types import SimpleNamespace

    from hylly import periodic

    class Serving(Exception):
        pass

    handler = SimpleNamespace(root=tmp_path, watch=SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(serve, "make_server", lambda *a, **k: (SimpleNamespace(RequestHandlerClass=handler), [0.0]))
    monkeypatch.setattr(periodic, "start", lambda *a, **k: (_ for _ in ()).throw(Serving()))  # just after the start's lines
    with pytest.raises(Serving):
        demo.run(str(tmp_path / "try"), 8780, open_browser=False, log=lambda s: None)
    said = capsys.readouterr().out
    assert "\n  Stops:             Ctrl-C in this window\n" in said and "hylly server stop" not in said
    assert "  Stops:             never by itself; hylly server stop stops it\n" in serve.describe({"pid": 7, "port": 8770})


def test_a_demo_writes_its_server_part_whole_however_the_file_has_it(tmp_path, apart, monkeypatch):
    """The demo made its settings file, so its [server] is the demo's to write: read as the settings read it, so a
    header with a comment or spaces, or a quoted key, leaves no second [server] and no second port; the rest stays."""
    import tomllib

    file = tmp_path / "settings.toml"
    monkeypatch.setenv("HYLLY_SETTINGS", str(file))
    for text in ('library = "x"\n[server]  # the demo\'s own\nport = 8779\nname = "Moonwake Shelf"\n[bell]\ntoast = false\n',
                 '[ server ]\nport = 8779\nname = "Moonwake Shelf"\n',
                 '[server]\n"port" = 8779\n\'listen\' = "0.0.0.0"\n"name" = "Moonwake Shelf"\n'):
        file.write_text(text, encoding="utf-8")
        demo.keep_port(8790)
        written = file.read_text(encoding="utf-8")
        read = tomllib.loads(written)  # a second [server], or a second port, would not even read
        assert read["server"] == {"port": 8790, "listen": "127.0.0.1", "name": "Moonwake Shelf"}, written
        assert len([line for line in written.splitlines() if demo.SERVER_TABLE.match(line)]) == 1
        if "[bell]" in text:
            assert read["bell"] == {"toast": False} and read["library"] == "x"
    file.write_text("server.port = 8779\n", encoding="utf-8")  # [server] some other way: the demo's part alone
    demo.keep_port(8790)
    assert tomllib.loads(file.read_text(encoding="utf-8"))["server"] == {"port": 8790, "listen": "127.0.0.1"}


def test_a_demo_has_an_owner_whom_a_one_time_link_logs_in(tmp_path, apart, monkeypatch):
    """Every request logs in, so the demo makes its owner, demo, with a password nobody is told: its server shows a
    one-time link at a terminal, hylly user link demo makes one at a terminal too (into a pipe or a file only with
    --yes), and hylly user add demo gives demo a password. Made once: a demo run again keeps its users."""
    said = []
    demo.run(str(tmp_path / "try"), 8779, serve_it=False, log=said.append)
    accounts = access.load_accounts()
    assert list(accounts) == ["demo"] and accounts["demo"][0] == "owner"
    assert ("Its owner is demo: with these set, hylly user link demo, at a terminal, makes a one-time link that logs a browser "
            "in, and hylly user add demo") in "\n".join(said)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert serve.owners_link(8779) == (None, serve.LINK_ELSEWHERE)  # into a file: no link
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    link, _ = serve.owners_link(8779)
    assert link.startswith("http://127.0.0.1:8779/login?once=") and access.Sessions().link_user(link.partition("once=")[2]) == "demo"
    demo.run(str(tmp_path / "try"), 8779, serve_it=False, log=lambda s: None)
    assert access.load_accounts() == accounts


def test_a_new_demo_has_its_settings_in_its_settings_folder_and_keeps_them_there(tmp_path, apart):
    for _ in range(2):
        demo.run(str(tmp_path / "try"), 8779, serve_it=False, log=lambda s: None)
        assert paths.config_dir() == tmp_path / "try" / "settings"
    assert not (tmp_path / "try" / "settings" / "Hylly").exists()


def test_a_folder_at_the_top_starting_with_an_underscore_isn_t_served(tmp_path):
    (tmp_path / "_settings").mkdir()
    (tmp_path / "_settings" / "users.txt").write_text("a hash", encoding="utf-8")
    (tmp_path / "ebooks" / "_mine").mkdir(parents=True)
    (tmp_path / "ebooks" / "_mine" / "a.epub").write_text("a book", encoding="utf-8")
    catalog_handler = serve.make_server(tmp_path, "127.0.0.1", 0)[0]
    try:
        servable = catalog_handler.RequestHandlerClass._servable
        assert not servable(tmp_path, tmp_path / "_settings" / "users.txt")
        assert servable(tmp_path, tmp_path / "ebooks" / "_mine" / "a.epub")
    finally:
        catalog_handler.server_close()


def test_a_demo_book_opens(tmp_path, demo_made_once):
    root = demo.make(tmp_path / "try")
    book = next((root / "ebooks").rglob("*.epub"))
    with zipfile.ZipFile(book) as z:
        assert z.namelist()[0] == "mimetype" and z.read("mimetype") == b"application/epub+zip" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.testzip() is None and b"<dc:title>" in z.read("OEBPS/book.opf")
    comic = next((root / "comics").glob("*.cbz"))
    with zipfile.ZipFile(comic) as z:
        assert z.read(z.namelist()[0]).startswith(b"\x89PNG")


def test_a_demo_keeps_to_a_folder_of_its_own(tmp_path, demo_made_once):
    (tmp_path / "my books").mkdir()
    (tmp_path / "my books" / "a.epub").write_text("mine", encoding="utf-8")
    with pytest.raises(Exit) as e:
        demo.make(tmp_path / "my books")
    assert e.value.code == exits.USAGE and sorted(p.name for p in (tmp_path / "my books").iterdir()) == ["a.epub"]


def test_a_second_demo_keeps_what_you_changed(tmp_path, demo_made_once):
    root = demo.make(tmp_path / "try")
    (root / "ebooks" / "yours.epub").write_text("yours", encoding="utf-8")
    book = next((root / "ebooks").rglob("The Salt Ledger.epub"))
    book.unlink()
    demo.make(tmp_path / "try")
    assert (root / "ebooks" / "yours.epub").exists() and not book.exists()


def test_a_demo_s_catalog_needs_no_network(tmp_path, apart, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no network"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no network"))
    root = demo.run(str(tmp_path / "try"), 8779, serve_it=False, log=lambda s: None)
    assert (root / "catalog" / "data.js").is_file()


def test_a_demo_says_every_variable_to_set_and_those_to_leave_unset(tmp_path, apart, capsys):
    """For other commands on the demo: its settings folder and library, each on a line of its own with nothing after
    it, as a path is copied whole; HYLLY_SETTINGS, HYLLY_RULES and HYLLY_REMOTE unset, as they would lead them to the
    owner's own settings, rules or server computer, and the secrets, the logins and the ntfy topic; and
    HYLLY_NO_NOTIFY, so a try that fails rings no bell."""
    place = tmp_path / "a try"
    cli.main(["demo", str(place), "--no-serve"])
    lines = capsys.readouterr().out.splitlines()
    assert f"  HYLLY_HOME={place / 'settings'}" in lines and f"  HYLLY_LIBRARY={place / 'library'}" in lines
    assert "  HYLLY_NO_NOTIFY=1" in lines
    said = "\n".join(lines)
    assert "leave HYLLY_SETTINGS, HYLLY_RULES and HYLLY_REMOTE unset" in said and "with --background, on its own" in said
    assert "hylly --here" in said and "under secret" in said


def test_a_demo_stays_on_this_computer_whatever_the_environment_names_of_the_owner_s(tmp_path, apart, monkeypatch, capsys):
    """HYLLY_REMOTE names the owner's server computer, and wins over the demo's own settings: a command with it set is
    passed on there over ssh, where hylly user add would add a user to the owner's server and hylly server stop stop it.
    hylly demo takes it out of its own run, as it does the owner's settings, rules and secrets (an ntfy address that
    would ring the owner's phone, a shop's login), and so the server it starts in the background has none of them; a
    command after it, in the same process, runs here. Nothing goes over ssh."""
    from hylly import instance, remote

    owners = {"HYLLY_REMOTE": "made-up-server", "HYLLY_NTFY_URL": "https://ntfy.example.org/made-up-topic",
              "HYLLY_NTFY_URL_FILE": str(tmp_path / "made-up-file"), "HYLLY_HUMBLE_SESSION": "made-up-cookie",
              "HYLLY_SETTINGS": str(tmp_path / "owner" / "settings.toml"), "HYLLY_RULES": str(tmp_path / "owner" / "rules")}
    for name, value in owners.items():
        monkeypatch.setenv(name, value)
    assert remote.wanted("user")  # as it is before the demo: passed on to the owner's server
    monkeypatch.setattr(remote, "forward", lambda *a, **k: pytest.fail("passed on over ssh"))
    monkeypatch.setattr(remote, "ask", lambda *a, **k: pytest.fail("asked over ssh"))
    seen = []

    def start_background():
        seen.append({name: os.environ.get(name) for name in owners})
        return {"pid": 4242, "port": 8790, "listen": "127.0.0.1", "started": "2026-11-17T10:00:00"}

    monkeypatch.setattr(instance, "start_background", start_background)
    place = tmp_path / "try"
    cli.main(["demo", str(place), "--background", "--port", "8790"])
    assert seen == [dict.fromkeys(owners)]  # its server has none of the owner's
    assert set(owners) <= set(demo.yours()) and not set(owners) & set(os.environ)
    capsys.readouterr()
    assert not remote.wanted("user") and not remote.wanted("server")
    cli.main(["user", "list"])  # here, on the demo's own users
    assert "demo" in capsys.readouterr().out
    monkeypatch.setattr(instance, "running", lambda: None)
    monkeypatch.setenv("HYLLY_REMOTE", "made-up-server")
    cli.main(["demo", str(place), "--stop"])  # so does its stop, with HYLLY_REMOTE set again
    assert "No demo server is running." in capsys.readouterr().out and "HYLLY_REMOTE" not in os.environ


def test_a_demo_in_the_background_starts_its_own_server_and_ends(tmp_path, apart, monkeypatch, capsys):
    """hylly demo --background, for a script or an assistant that mustn't wait on a server in the foreground or open
    a browser: the demo's own server started on its own, from its own settings folder, and the address, the way to log
    in and the command that stops it said; a second one leaves the one running as it is."""
    from hylly import instance, settings

    started = []

    def start_background():
        started.append((paths.config_dir(), os.environ["HYLLY_LIBRARY"], settings.get("server")["port"]))
        return {"pid": 4242, "port": 8790, "listen": "127.0.0.1", "started": "2026-11-17T10:00:00"}

    monkeypatch.setattr(instance, "start_background", start_background)
    monkeypatch.setattr(serve, "run", lambda *a, **k: pytest.fail("not here, in the foreground"))
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: pytest.fail("no browser"))
    place = tmp_path / "try"
    cli.main(["demo", str(place), "--background", "--port", "8790"])
    said = capsys.readouterr().out
    assert started == [(place / "settings", str(place / "library"), 8790)]  # the demo's, on its own port
    assert "Serving it in the background" in said and "http://127.0.0.1:8790/" in said
    assert "hylly user link demo" in said and f"To stop it, and no other server: hylly demo --stop {paths.typed(place)}" in said
    assert "hylly server stop" not in said  # which, without the demo's settings folder set, would stop the owner's own
    monkeypatch.setattr(instance, "running", lambda: {"pid": 4242, "port": 8790, "listen": "127.0.0.1"})
    cli.main(["demo", str(place), "--background", "--port", "8791"])  # another port: the one running keeps its own
    assert "running already, and is left as it is" in capsys.readouterr().out and len(started) == 1
    assert settings.get("server")["port"] == 8790  # in its settings too, which a restart takes
    cli.main(["demo", str(place), "--background", "-n"])
    assert "serve it in the background at http://127.0.0.1:8779/, and end. Nothing was made." in capsys.readouterr().out
    assert exit_of(capsys, "demo", str(place), "--background", "--no-serve")[0] == exits.USAGE  # one or the other


def test_a_demo_s_stop_stops_its_own_server_and_never_yours(tmp_path, apart, monkeypatch, capsys):
    """A server is known by its settings folder's lock and note, so hylly demo --stop looks only in the demo's own:
    with the owner's server running meanwhile (made up here: this test holds its lock), that one is never stopped, nor
    any other process; nor is anything stopped for a folder that isn't a demo's, or whose settings lead to the usual
    folder."""
    import time

    from hylly import instance

    ended = []
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: pytest.fail(f"process {pid} would be ended"))
    yours_home = os.environ["HYLLY_HOME"]
    yours = instance.Claim()  # the owner's own server, made up
    yours.announce(pid=os.getpid(), port=8770, began=time.time(), started="2026-11-17T09:00:00",
                   library=str(tmp_path / "yours"))
    try:
        place = tmp_path / "try"
        demo.make(place)
        cli.main(["demo", str(place), "--stop"])
        assert capsys.readouterr().out == "No demo server is running.\n"
        cli.main(["demo", str(place), "--stop", "-n"])
        assert capsys.readouterr().out == "No demo server is running.\n"
        monkeypatch.setenv("HYLLY_HOME", str(place / "settings"))  # the demo's own, made up too
        theirs = instance.Claim()
        theirs.announce(pid=os.getpid(), port=8779, began=time.time(), started="2026-11-17T10:00:00",
                        library=str(place / "library"))
        monkeypatch.setattr(instance.os, "kill", lambda pid, sig: (ended.append((pid, paths.config_dir())), theirs.release()))
        monkeypatch.setenv("HYLLY_HOME", yours_home)
        cli.main(["demo", str(place), "--stop", "-n"])
        assert "It would stop the demo's server, and no other" in capsys.readouterr().out and not ended
        cli.main(["demo", str(place), "--stop"])
        assert "Stopped the demo's server." in capsys.readouterr().out
        assert ended == [(os.getpid(), place / "settings")]  # once, and from the demo's folder
        monkeypatch.setenv("HYLLY_HOME", yours_home)
        assert instance.running()["port"] == 8770  # yours runs on
        (tmp_path / "mine").mkdir()
        for words, why in (([str(tmp_path / "mine"), "--stop"], "isn't a demo's folder"), ([str(place), "--stop", "--port", "8779"], "no --port")):
            code, err = exit_of(capsys, "demo", *words)
            assert code == exits.USAGE and why in err and "Nothing was stopped" in err
        monkeypatch.setattr(paths, "usual_settings_folder", lambda settings_file_counts=True: True)  # a link to yours, say
        code, err = exit_of(capsys, "demo", str(place), "--stop")
        assert code == exits.USAGE and "leads to your own" in err and len(ended) == 1
    finally:
        monkeypatch.setenv("HYLLY_HOME", yours_home)
        yours.release()


def test_a_demo_s_stop_ends_no_server_of_another_library_nor_another_process(tmp_path, apart, monkeypatch, capsys):
    """Its settings folder a link to another Hylly's, elsewhere: the server whose lock is there serves another library,
    and is left running. A note in the demo's own folder naming another process, one that doesn't hold the demo's lock:
    that process is never ended. In each, nothing is stopped, and the dry run says so too."""
    import time

    from hylly import instance

    linked, other = tmp_path / "linked", tmp_path / "another Hylly"
    demo.make(linked)
    other.mkdir()
    try:
        (linked / "settings").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("this computer makes no links")
    place = tmp_path / "try"
    demo.make(place)
    other_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # this test's own
    try:
        with monkeypatch.context() as m:
            m.setattr(instance.os, "kill", lambda pid, sig: pytest.fail(f"process {pid} would be ended"))
            m.setenv("HYLLY_HOME", str(other))  # that Hylly's server, made up: this test holds its lock
            theirs = instance.Claim()
            theirs.announce(pid=os.getpid(), port=8790, began=time.time(), library=str(tmp_path / "their library"))
            try:
                for dry in ([], ["-n"]):
                    code, err = exit_of(capsys, "demo", str(linked), "--stop", *dry)
                    assert code == exits.USAGE and "not the demo's" in err and "Nothing was stopped" in err
            finally:
                theirs.release()
            m.setenv("HYLLY_HOME", str(place / "settings"))
            mine = instance.Claim()  # the demo's lock, held here, and a note naming the other process
            mine.announce(pid=other_process.pid, port=8779, began=time.time(), library=str(place / "library"))
            try:
                for dry in ([], ["-n"]):
                    code, err = exit_of(capsys, "demo", str(place), "--stop", *dry)
                    assert code == exits.LOCAL and f"names process {other_process.pid}" in err and "Nothing was stopped" in err
                assert other_process.poll() is None  # still running
            finally:
                mine.release()
    finally:
        other_process.kill()
        other_process.wait()


def test_a_demo_s_stop_where_a_process_s_files_can_t_be_seen_goes_by_its_start_and_its_command(tmp_path, apart, monkeypatch, capsys):
    """Where the files a process has open can't be looked at (not allowed, or Windows, which may not list them all),
    can't tell isn't no: the demo's server is stopped if it began before its note and runs a Hylly server, and else
    nothing is, with the words that it couldn't tell and the way out. A server just starting, its lock taken and its
    note not written yet, is said to be starting."""
    import time

    import psutil

    from hylly import instance

    place = tmp_path / "try"
    demo.make(place)
    monkeypatch.setenv("HYLLY_HOME", str(place / "settings"))
    ended = []
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: pytest.fail(f"process {pid} would be ended"))
    starting = instance.Claim()  # its lock taken, and no note yet
    try:
        code, err = exit_of(capsys, "demo", str(place), "--stop")
        assert code == exits.LOCAL and "is starting" in err and "try again in a moment" in err and "another" not in err
    finally:
        starting.release()

    def not_allowed(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "open_files", not_allowed)
    me = psutil.Process()
    for began, command, told in ((time.time(), me.cmdline(), "isn't a Hylly server"),  # this test's own program
                                 (me.create_time() - 600, [sys.executable, "-P", "-m", "hylly.daemon", "--in-background"],
                                  "didn't begin before the note")):  # a number given to another program meanwhile
        monkeypatch.setattr(psutil.Process, "cmdline", lambda self, command=command: command)
        monkeypatch.setenv("HYLLY_HOME", str(place / "settings"))
        mine = instance.Claim()
        mine.announce(pid=os.getpid(), port=8779, began=began, library=str(place / "library"))
        try:
            code, err = exit_of(capsys, "demo", str(place), "--stop")
            assert code == exits.LOCAL and "can't be told here" in err and "Nothing was stopped" in err, told
            assert "hylly server stop" in err and "HYLLY_HOME" in err, told
        finally:
            mine.release()
    monkeypatch.setenv("HYLLY_HOME", str(place / "settings"))
    mine = instance.Claim()  # began before its note, and a Hylly server: stopped
    mine.announce(pid=os.getpid(), port=8779, began=time.time(), library=str(place / "library"))
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: (ended.append(pid), mine.release()))
    try:
        cli.main(["demo", str(place), "--stop"])
        assert "Stopped the demo's server." in capsys.readouterr().out and ended == [os.getpid()]
    finally:
        mine.release()


# ---- not as root, and how safely it's set up -------------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Linux and macOS")


@pytest.mark.parametrize("words", [["server", "status"], ["serve", "--background"], ["catalog"], ["jobs"], ["user", "list"], ["copy", "a", "b"]])
def test_no_command_runs_as_root(monkeypatch, capsys, words):
    monkeypatch.setattr(paths, "is_root", lambda: True)
    monkeypatch.delenv("HYLLY_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(paths, "keep_private", lambda: pytest.fail("it would make the settings folder root's"))
    code, err = exit_of(capsys, *words)
    assert code == exits.NOT_SET_UP and "doesn't run as root" in err and "without sudo" in err


def test_the_doctor_runs_as_root_to_say_so(monkeypatch, capsys):
    monkeypatch.setattr(paths, "is_root", lambda: True)
    monkeypatch.delenv("HYLLY_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(paths, "keep_private", lambda: pytest.fail("it would make the settings folder root's"))
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    assert "This runs as root" in capsys.readouterr().out
    assert states(doctor.report())["Rights"] == "problem"


def test_root_can_be_let_where_it_can_t_be_otherwise(monkeypatch):
    monkeypatch.setattr(paths, "is_root", lambda: True)
    monkeypatch.setenv("HYLLY_ALLOW_ROOT", "1")
    assert not paths.running_as_root()
    assert states(doctor.report())["Rights"] == "info"


def test_your_own_user_hears_nothing_of_it(monkeypatch):
    monkeypatch.setattr(paths, "is_root", lambda: False)
    monkeypatch.setattr(doctor, "_elevated", lambda: False)
    assert not paths.running_as_root() and "Rights" not in states(doctor.report())


@posix_only
def test_root_is_told_by_who_it_really_is(monkeypatch):
    monkeypatch.setattr(paths, "is_root", REAL_IS_ROOT)
    monkeypatch.delenv("HYLLY_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(paths.os, "geteuid", lambda: 0)
    assert paths.is_root() and paths.running_as_root()
    monkeypatch.setattr(paths.os, "geteuid", lambda: 1000)
    assert not paths.is_root()


def test_an_administrator_s_terminal_is_told_of(monkeypatch):
    monkeypatch.setattr(paths, "is_root", lambda: False)
    monkeypatch.setattr(doctor, "_elevated", lambda: True)
    assert states(doctor.report())["Rights"] == "problem"


@posix_only
def test_what_isn_t_yours_in_the_settings_folder_is_told_of(monkeypatch):
    folder = paths.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "users.txt").write_text("", encoding="utf-8")
    assert doctor._private() == [] or all(c["state"] != "problem" or "get at" in c["says"] for c in doctor._private())
    real = os.getuid()
    monkeypatch.setattr(doctor.os, "getuid", lambda: real + 1)
    found = doctor._private()
    assert found[0]["state"] == "problem" and "isn't yours" in found[0]["says"] and "users.txt" in found[0]["says"] and "chown" in found[0]["next"]


@posix_only
def test_a_settings_folder_named_from_here_is_put_right_by_its_full_path(tmp_path, monkeypatch):
    """HYLLY_HOME may name the folder from the current one: the chmod to run names it in full, wherever it's run, and
    the step in words as it is typed, in quotes where a space would split it ("Application Support", say)."""
    import shlex

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HYLLY_HOME", "a home of its own")
    folder = paths.config_dir()
    folder.mkdir()
    os.chmod(folder, 0o755)
    (folder / "users.txt").write_text("", encoding="utf-8")
    os.chmod(folder / "users.txt", 0o644)
    found = doctor._private()
    assert found[0]["id"] == "settings-rights" and found[0]["next"] == "chmod -R go-rwx 'a home of its own'"  # as typed
    assert found[0]["run"] == shlex.join(["chmod", "-R", "go-rwx", os.path.join(os.getcwd(), "a home of its own")])


@posix_only
def test_what_others_can_get_at_in_the_settings_folder_is_told_of():
    folder = paths.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o755)
    (folder / "users.txt").write_text("", encoding="utf-8")
    os.chmod(folder / "users.txt", 0o644)
    found = doctor._private()
    assert len(found) == 1 and "users.txt" in found[0]["says"] and "go-rwx" in found[0]["next"]
    os.chmod(folder, 0o700)


@posix_only
def test_inside_a_private_folder_nothing_is_out_in_the_open():
    """The reader's cache folder is made with the usual rights: nobody gets to it through the settings folder."""
    folder = paths.config_dir()
    (folder / "cache").mkdir(parents=True, exist_ok=True)
    os.chmod(folder / "cache", 0o775)
    (folder / "users.txt").write_text("", encoding="utf-8")
    os.chmod(folder / "users.txt", 0o644)
    os.chmod(folder, 0o700)
    assert doctor._private() == []


@posix_only
def test_a_library_anyone_can_change_is_told_of(tmp_path):
    os.chmod(tmp_path, 0o777)
    assert doctor._library_rights(tmp_path)[0]["state"] == "info"
    os.chmod(tmp_path, 0o755)
    assert doctor._library_rights(tmp_path) == []


def settings_say(text: str) -> None:
    """The settings say this, read afresh with the rules, as the next command would read them."""
    from hylly import rules, settings

    Path(os.environ["HYLLY_SETTINGS"]).write_text(text, encoding="utf-8")
    settings.reload()
    rules.reset()


def test_on_this_computer_alone_the_address_check_says_nothing(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: None)
    for said in ("", '[server]\nlisten = "::1"\n', '[server]\naddress = "http://127.0.0.1:8770"\n'):
        settings_say(said)
        assert doctor._address() == [], said


def test_an_address_with_no_users_is_told_of_by_the_access_check(monkeypatch):
    """Every request logs in, so no users is a problem wherever the server is reached from: the Access check says it,
    with the address people use, and the address's own check asks nothing of logins."""
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: None)
    settings_say('[server]\naddress = "https://books.example.org/"\n')
    assert doctor._address() == []
    found = doctor._access()[0]
    assert found["state"] == "problem" and "the address people use is https://books.example.org." in found["says"]
    assert "no users yet" in found["says"] and "hylly user add" in found["next"]


def test_an_https_address_while_listening_everywhere_is_fine(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: None)
    monkeypatch.setattr(access, "peek_accounts", lambda: {"someone": ("reader", "x")})
    for listen in ("0.0.0.0", "::", "192.0.2.10"):
        settings_say(f'[server]\nlisten = "{listen}"\naddress = "https://books.example.org/"\n')
        assert doctor._address() == [], listen


def test_an_address_without_https_is_a_note_only_while_listening_beyond_this_computer(monkeypatch):
    """A note, not a problem: an e-reader's browser on plain http at home is a choice."""
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: None)
    settings_say('[server]\nlisten = "0.0.0.0"\naddress = "http://books.example.org/"\n')
    found = doctor._address()
    assert len(found) == 1 and found[0]["state"] == "info" and "http://books.example.org, isn't https" in found[0]["says"]
    assert doctor.report()["problems"] == sum(c["state"] == "problem" for c in doctor.report()["checks"] if c["what"] != "Address")
    settings_say('[server]\naddress = "http://books.example.org/"\n')  # this computer only: a proxy here gives the address
    assert doctor._address() == []


def test_listening_on_every_address_with_no_address_set_is_a_note(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: None)
    for listen in ("0.0.0.0", "::"):
        settings_say(f'[server]\nlisten = "{listen}"\n')
        found = doctor._address()
        assert len(found) == 1 and found[0]["state"] == "info" and f"It listens on every address ({listen})" in found[0]["says"]
        assert "address under [server]" in found[0]["next"]
    settings_say('[server]\nlisten = "192.0.2.10"\n')  # one address: that one is shown
    assert doctor._address() == []
    monkeypatch.setattr(instance, "running", lambda: {"pid": 8, "listen": "0.0.0.0", "address": "https://books.example.org"})
    settings_say("")
    assert doctor._address() == []  # the running server's own, over the settings


def test_forwarding_headers_from_an_address_not_trusted_are_a_note_from_the_running_server(monkeypatch):
    """The running server notes, once each, the addresses that sent forwarding headers without being one of
    trusted_proxies (serve.Ignored, in its note); the doctor tells of them, as a proxy of yours left out of the list makes
    all its visitors one sender to the lockout."""
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: {"pid": 8, "listen": "127.0.0.1", "address": "",
                                                      "headers_ignored_from": ["172.18.0.3", "192.0.2.40"]})
    found = doctor._address()
    assert len(found) == 1 and found[0]["state"] == "info"
    assert found[0]["says"].startswith("Forwarding headers came from 172.18.0.3, 192.0.2.40, not in trusted_proxies under [server]")
    assert "add it to trusted_proxies and restart the server" in found[0]["next"]
    monkeypatch.setattr(instance, "running", lambda: {"pid": 8, "listen": "127.0.0.1"})
    assert doctor._address() == []


def test_wrong_logins_from_a_trusted_proxys_own_address_are_a_note(monkeypatch):
    """A trusted proxy that hands on no sender makes all its visitors one to the lockout: the running server notes a
    wrong login or a lockout from a trusted proxy's own address (serve.NOTED), and the doctor tells of it."""
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: {"pid": 8, "listen": "127.0.0.1", "unnamed_by_proxy": ["127.0.0.1"]})
    found = doctor._address()
    assert len(found) == 1 and found[0]["state"] == "info" and found[0]["says"].startswith("Wrong logins or a lockout came from 127.0.0.1")
    assert "$proxy_add_x_forwarded_for" in found[0]["next"] and "never pass on the one the visitor sent" in found[0]["next"]


def test_trusted_proxies_beyond_this_computer_are_a_note(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "running", lambda: None)
    settings_say('[server]\ntrusted_proxies = ["127.0.0.1", "::1", "172.18.0.0/16", "192.0.2.0/24"]\n')
    found = doctor._address()
    assert len(found) == 1 and found[0]["says"].startswith("trusted_proxies under [server] holds 172.18.0.0/16, 192.0.2.0/24 beyond")
    settings_say('[server]\ntrusted_proxies = ["127.0.0.0/8", "127.0.0.2", "::1"]\n')
    assert doctor._address() == []


@pytest.mark.parametrize("system", ["windows", "mac"])
def test_on_windows_and_macos_a_server_at_an_address_beside_saved_logins_is_told_of(monkeypatch, system):
    """No walls there, so a server at an address set has the shops' logins saved on that computer within its
    reach: said, not as wrong, with the help's two ways round it. Without logins saved, or without an address, the walls
    are Linux's to speak of."""
    from hylly import instance

    monkeypatch.setattr(instance, "SYSTEM", system)
    monkeypatch.setattr(instance, "system_service", lambda: True)  # a Linux unit's file is nothing here
    settings_say('[server]\naddress = "https://books.example.org/"\n')
    assert doctor._walls() == []  # no logins saved here
    logins.save_session(COOKIE, "humble")
    found = doctor._walls()
    assert len(found) == 1 and found[0]["state"] == "info" and "with the rights of your whole account" in found[0]["says"]
    assert "The logins and keys saved here, of humble, are within its reach too." in found[0]["says"]
    assert "separate standard account" in found[0]["next"] and "logins on another computer" in found[0]["next"]
    assert "hylly.service" not in found[0]["next"]
    settings_say("[server]\n")
    assert doctor._walls() == []  # on this computer alone, nothing to say


def test_a_public_server_without_walls_is_told_of_but_not_as_wrong(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "system_service", lambda: False)
    settings_say('[server]\naddress = "https://books.example.org/"\n')
    assert doctor._walls()[0]["state"] == "info"
    assert "Funnel by the whole internet, with Serve by your own devices" in doctor._walls()[0]["says"]  # it can't tell which
    assert "within its reach" not in doctor._walls()[0]["says"] and "hylly.service" in doctor._walls()[0]["next"]
    logins.save_session(COOKIE, "humble")  # the logins it could read, named
    assert "The logins and keys saved here, of humble, are within its reach too." in doctor._walls()[0]["says"]
    settings_say('[server]\nlisten = "0.0.0.0"\naddress = "http://192.0.2.10:8770"\n')  # a plain address at home: no Funnel
    said = doctor._walls()[0]["says"]
    assert "by every device that reaches that address" in said and "Funnel" not in said
    settings_say("[server]\n")
    assert doctor._walls() == []  # at home, on your own computer, nothing to say


def test_a_server_in_its_walls_says_nothing_of_the_logins_it_can_t_see(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "system_service", lambda: True)
    monkeypatch.setattr(instance, "running", lambda: None)
    settings_say('[server]\naddress = "https://books.example.org/"\n')
    logins.save_session(COOKIE, "humble")
    assert [c["state"] for c in doctor._walls()] == ["ok"]


def test_a_server_in_its_walls_is_said_to_be(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "system_service", lambda: True)
    monkeypatch.setattr(instance, "running", lambda: {"pid": 7})
    monkeypatch.setattr(instance, "system_owns", lambda pid: pid == 7)
    assert doctor._walls()[0]["state"] == "ok"


def test_a_server_outside_the_walls_is_told_of(monkeypatch):
    from hylly import instance

    monkeypatch.setattr(instance, "SYSTEM", "linux")
    monkeypatch.setattr(instance, "system_service", lambda: True)
    monkeypatch.setattr(instance, "running", lambda: {"pid": 8})
    monkeypatch.setattr(instance, "system_owns", lambda pid: False)
    found = doctor._walls()[0]
    assert found["state"] == "problem" and "outside the walls" in found["says"] and "process 8" in found["says"]


def test_the_doctor_says_once_when_the_alarm_command_can_t_be_found_or_last_failed(tmp_path):
    """[bell] command's program not found here, or bell.log's latest word on it a failure: one line, however
    often it failed; nothing once a run has worked since."""
    missing = json.dumps(str(tmp_path / "no-such-program-for-hylly-tests"))  # by its full path, as Windows wants it
    settings_say(f'[bell]\ncommand = [{missing}, "-s", "Library alarm"]\n')
    found = doctor._alarms()
    assert "the command no-such-program-for-hylly-tests" in found[0]["says"] and "Library alarm" not in found[0]["says"]
    assert [c["state"] for c in found] == ["info", "problem"] and "isn't found on this computer" in found[1]["says"]
    settings_say(f'[bell]\ncommand = [{json.dumps(sys.executable)}, "-c", "pass"]\n')
    assert [c["state"] for c in doctor._alarms()] == ["info"]
    w = bell.Watch(log=lambda m: None)
    for _ in range(3):
        w._ran("python ended with code 3: made-up trouble")
    found = doctor._alarms()
    assert [c["state"] for c in found] == ["info", "problem"]
    assert re.fullmatch(r"The alarm command failed at \S+ \S+: python ended with code 3: made-up trouble\.", found[1]["says"])
    assert "hylly bell test" in found[1]["next"]
    w._ran("")
    assert [c["state"] for c in doctor._alarms()] == ["info"]
    script = paths.config_dir() / "alarm.py"  # where the server can write: said before anything else, and not run
    script.write_text("pass\n", encoding="utf-8")
    settings_say(f'[bell]\ncommand = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n')
    found = doctor._alarms()
    assert [c["state"] for c in found] == ["info", "problem"] and "is in one of Hylly's own folders or the library, so it isn't run" in found[1]["says"]
    assert "outside Hylly's own folders and the library" in found[1]["next"]


def test_the_new_looks_name_no_secrets(tmp_path):
    settings_say('[server]\naddress = "https://books.example.org/"\n[bell]\nntfy = "a-secret-topic-of-some-length-here"\n')
    said = json.dumps(doctor.report(str(tmp_path)))
    assert "a-secret-topic" not in said


def test_the_doctor_lists_the_e_books_that_can_t_be_opened_without_calling_it_a_fault(tmp_path):
    from hylly import catalog

    build_library(tmp_path)
    damage_a_mobi(tmp_path)
    before = doctor.report(str(tmp_path))  # no catalog yet: that's the Catalog line's to say
    assert "Books" not in states(before) and states(before)["Catalog"] == "problem"
    locked_epub(tmp_path / "collections" / "Poetry" / "Ann Writer - The Locked Tale.epub", title="The Locked Tale")
    catalog.build(tmp_path, log=lambda s: None)
    result = doctor.report(str(tmp_path))
    told = next(c for c in result["checks"] if c["what"] == "Books")
    assert told["state"] == "info" and "2 e-book files can't be opened" in told["says"]
    assert "collections/Poetry/Ann Writer - The Locked Tale.epub (locked)" in told["says"]
    assert "Herman Melville - Bartleby the Scrivener.mobi (damaged)" in told["says"]
    assert "hylly catalog" in told["next"]


def test_the_doctor_says_only_the_fact_of_a_locked_book_and_asks_another_copy_only_of_a_damaged_one(tmp_path):
    from hylly import catalog

    build_library(tmp_path)
    locked_epub(tmp_path / "collections" / "Poetry" / "Ann Writer - The Locked Tale.epub", title="The Locked Tale")
    catalog.build(tmp_path, log=lambda s: None)
    told = next(c for c in doctor.report(str(tmp_path))["checks"] if c["what"] == "Books")
    assert "1 e-book file can't be opened" in told["says"]
    assert "can only be read in the app of the shop that sold it" in told["next"] and "another copy" not in told["next"].lower()
    damage_a_mobi(tmp_path)
    catalog.build(tmp_path, log=lambda s: None)
    told = next(c for c in doctor.report(str(tmp_path))["checks"] if c["what"] == "Books")
    assert "the shop that sold it" in told["next"] and "To read a damaged one, the library needs another copy" in told["next"]


# ---- the command line's shape, as hylly commands tells it ------------------------------------------------------------

def test_every_action_has_its_own_row_with_its_own_options_and_effects(capsys):
    """An assistant planning from hylly commands learns the verbs, and tells a harmless read from what can't be undone."""
    data = as_json(capsys, "commands", "--json")
    rows = {c["command"]: c for c in data["commands"]}
    assert {"hylly user add", "hylly user list", "hylly people forget", "hylly tag list", "hylly theme check", "hylly bell test",
            "hylly server status", "hylly server autostart on", "hylly jobs stop", "hylly restore", "hylly backup check"} <= set(rows)
    assert rows["hylly user list"]["writes"] == ["nothing"] and rows["hylly tag list"]["writes"] == ["nothing"]
    assert "can't be undone" in rows["hylly people forget"]["person"] and rows["hylly people list"]["person"] is None
    assert {o["option"] for o in rows["hylly user role"]["options"]} == {"name", "ROLE", "-y, --yes", "-n, --dry-run"}
    assert "--search" not in {o["option"] for o in rows["hylly tag list"]["options"]}
    assert "written over" in rows["hylly reviews export"]["writes"][0]  # --out writes the file named


def test_a_backup_and_a_restore_say_all_they_write(capsys):
    """The records made fresh, the server asked over ssh, the note; and a restore's new folder and database."""
    rows = {c["command"]: c for c in as_json(capsys, "commands", "--json")["commands"]}
    backup = " ".join(rows["hylly backup"]["writes"])
    assert "state/db-backup" in backup and "over ssh" in backup and "state/settings-backup" in backup and "backup.json" in backup
    assert rows["hylly backup"]["online"] and rows["hylly backup"]["runs"] == "here"
    restore = " ".join(rows["hylly restore"]["writes"])
    assert "never over what is there" in restore and "this computer's database" in restore and rows["hylly restore"]["job"] == "backup"


def test_where_a_command_runs_and_how_to_keep_it_here_are_said(capsys):
    """The server hand-off, the options before the command, and the variables that steer it."""
    data = as_json(capsys, "commands", "--json")
    assert {"-H, --remote", "--here"} <= {o["option"] for o in data["options"]}
    assert "HYLLY_REMOTE" in data["remote"] and "--here" in data["remote"] and "Running on HOST." in data["remote"]
    runs_at = {c["command"]: c["runs"] for c in data["commands"]}
    assert runs_at["hylly copy"] == runs_at["hylly backup"] == runs_at["hylly restore"] == "here"
    assert "server computer" in runs_at["hylly tag add"]
    assert data["hyllyd"]["options"] and "--in-background" in {o["option"] for o in data["hyllyd"]["options"]}


def test_the_environment_has_every_variable_and_the_logins_are_secret(capsys):
    """The login variables marked secret, the itch.io cookie called one, and the variables a Linux deployment meets."""
    data = as_json(capsys, "commands", "--json")
    env = data["environment"]
    assert {"HYLLY_ALLOW_ROOT", "HYLLY_SYSTEM_SERVICE", "HYLLY_REMOTE"} <= set(env) and "never set by hand" in env["HYLLY_SYSTEM_SERVICE"]
    assert set(data["secret"]) == {variable for _, _, variable in logins.SITES.values()} | {"HYLLY_NTFY_URL", "HYLLY_NTFY_TOKEN"}
    assert all(env[v].startswith("SECRET, set by its owner only") for v in data["secret"])
    # Hylly's own with HYLLY_ in front, every one; systemd's for its credentials is systemd's own name
    assert [v for v in env if not v.startswith("HYLLY_")] == ["CREDENTIALS_DIRECTORY"] and "HYLLY_NTFY_URL_FILE" in env
    assert "login cookie (itchio), for the bundles" in env["HYLLY_ITCHIO_SESSION"] and "itchio-bundles itchio" not in json.dumps(env)
    assert "login cookie (ory_session_stoiccohens510ht9xek)" in env["HYLLY_HUMBLE_SESSION"] and "application key" in env["HYLLY_DRIVETHRURPG_KEY"]


def test_the_calls_between_hylly_s_own_processes_are_out_of_sight(capsys):
    """hylly internal is in neither hylly --help nor hylly commands, but for hylly commands --internal."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "internal" not in capsys.readouterr().out
    assert exit_of(capsys, "nosuch")[1].count("internal") == 0  # nor among the commands a mistyped one is told of
    named = {c["command"] for c in as_json(capsys, "commands", "--json")["commands"]}
    assert not {n for n in named if "internal" in n}
    shown = {c["command"] for c in as_json(capsys, "commands", "--json", "--internal")["commands"]}
    assert {"hylly internal backup-noted", "hylly internal fresh-copy", "hylly internal job"} <= shown
    for words in (["backup", "--help"], ["serve", "--help"]):
        with pytest.raises(SystemExit):
            cli.main(words)
        said = capsys.readouterr().out
        assert "--noted" not in said and "--fresh-copy" not in said and "--in-background" not in said and "--given" not in said


def test_every_command_that_changes_something_has_a_dry_run(capsys):
    """The owner's rule: --dry-run (-n) on every command that writes, sends or starts something, saying what it would do;
    one that only reads needs none."""
    rows = as_json(capsys, "commands", "--json", "--internal")["commands"]
    for c in rows:
        changes = c["writes"] != ["nothing"] or c["account"] or c["command"] == "hylly bell test"
        has = "-n, --dry-run" in {o["option"] for o in c["options"]}
        assert has == changes and bool(c["dry_run"]) == changes, c["command"]


def test_where_to_start_is_said_where_no_library_is_set(capsys):
    code, said = exit_of(capsys, "verify")
    assert code == exits.NOT_SET_UP and "hylly init FOLDER" in said and "hylly demo" in said


def test_the_dry_runs_of_the_users_and_people_change_nothing(capsys, monkeypatch):
    access.add_user("someone", "correct horse", role="owner")
    monkeypatch.setattr(cli, "_hidden", lambda *a: pytest.fail("a dry run asks for no password"))
    for words, said in ((["user", "add", "another", "-n"], "add them as reader"), (["user", "role", "someone", "reader", "-n"], "from owner"),
                        (["user", "rename", "someone", "ann", "-n"], "rename someone to ann"), (["user", "remove", "someone", "-n"], "remove someone"),
                        (["user", "link", "someone", "-n"], "one-time link")):
        cli.main(words)
        assert said in capsys.readouterr().out, words
    assert access.load_accounts()["someone"][0] == "owner" and set(access.load_accounts()) == {"someone"}
    assert exit_of(capsys, "user", "remove", "nobody", "-n")[0] == exits.USAGE  # no retry helps: a mistake


def test_the_dry_runs_of_the_bell_the_rules_and_the_demo_change_nothing(capsys, tmp_path, monkeypatch):
    from hylly import rules

    monkeypatch.setattr(bell.Watch, "push", lambda *a, **k: pytest.fail("a dry run sends nothing"))
    cli.main(["bell", "test", "-n"])
    assert "It would send a test alarm" in capsys.readouterr().out and not bell.log_file().exists()
    cli.main(["rules", "copy", "genres", "-n"])
    assert "It would copy the shipped genres.toml" in capsys.readouterr().out and not (rules.folder() / "genres.toml").exists()
    cli.main(["demo", str(tmp_path / "try"), "-n"])
    assert "It would make a demo library" in capsys.readouterr().out and not (tmp_path / "try").exists()
    cli.main(["init", str(tmp_path / "new"), "-n"])
    assert "Nothing changed" in capsys.readouterr().out and not (tmp_path / "new").exists()


# ---- a dry run changes nothing ---------------------------------------------------------------------------------------

def dry_runs() -> list[str]:
    """Every command and action with a --dry-run, from the parser: a new one is tried below by itself."""
    return [name for name, p, _ in commands.leaves(internal=True) if any("--dry-run" in a.option_strings for a in p._actions)]


DRY_WORDS = {  # what a dry run is given besides -n, and --library where it takes one: {lib} the library, {out} a folder beside it
    "login": ["humble", "--clipboard"], "humble seed": ["{out}/old"], "add": ["ebooks", "--from", "{out}/old"],
    "tag add": ["Knitting", "--search", "a"], "tag remove": ["Knitting", "--search", "a"], "set": ["kind", "comic", "--search", "a"],
    "rules copy": ["genres"], "user add": ["ann"], "user role": ["demo", "reader"], "user rename": ["demo", "ann"],
    "user remove": ["demo"], "user link": ["demo"], "people forget": ["demo"], "reviews export": ["--out", "{lib}/best.md"],
    "user restore": ["rory"], "people erase": ["rory"],  # a user removed, with what was kept of them kept aside
    "copy": ["{out}/old", "{lib}/copied"], "copy check": ["{out}/disk/library"], "backup": ["{out}/disk"],
    "backup check": ["{out}/disk"], "restore": ["{out}/disk", "{out}/restored"], "init": ["{lib}/new"], "jobs stop": ["humble"],
    "demo": ["{out}/try"], "internal backup-noted": ["{out}/disk"], "internal job": ["doctor"],
    "internal server-job": ["nightly copy"], "internal inbox": [],  # its --library alone: no Inbox there, which it says
}


@pytest.fixture(scope="module")
def made_up(tmp_path_factory, made_demo):
    """A demo library with its catalog, its owner with a place kept, a removed user's place kept aside, a backup of it, a
    book to add, Humble's orders saved, an itch.io survey of the bundles, and the shops' logins (all made up): built once,
    copied for each dry run below."""
    from hylly import catalog, db, people, rules, settings, snapshot

    base = tmp_path_factory.mktemp("made-up")
    was = {k: os.environ.get(k) for k in ("HYLLY_HOME", "HYLLY_SETTINGS", "HYLLY_LIBRARY")}
    os.environ.update(HYLLY_HOME=str(base / "settings"), HYLLY_SETTINGS=str(base / "settings.toml"))
    (base / "settings.toml").write_text("", encoding="utf-8")
    settings.reload()
    rules.reset()
    try:
        root = made_demo.make(base / "demo")  # as demo.make would draw it
        catalog.build(root, log=lambda s: None)
        demo.owner()
        people.Person(demo.OWNER).place(Catalog(root).get()[0], 12, 0.25)
        access.add_user("rory", "correct horse")
        people.Person("rory").place(Catalog(root).get()[0], 3, 0.1)
        access.remove_user("rory")
        snapshot.save(ORDERS, paths.state_dir(root), "humble")
        (paths.state_dir(root) / "itchio").mkdir(exist_ok=True)  # itch.io's own folder of the state folder
        (paths.state_dir(root) / "itchio" / "bundle-pages.jsonl").write_text(
            json.dumps({"bundle": "Moonwake Bundle", "page": 1, "items": []}) + "\n", encoding="utf-8")
        (base / "old").mkdir()
        (base / "old" / "Ann Writer - A Made-up Book.epub").write_bytes(b"not really a book")
        with contextlib.suppress(SystemExit):  # the backup is made, whole or partly so: either is a backup to check
            cli.main(["backup", str(base / "disk"), "--library", str(root), "--yes"])
        assert backup_made(base / "disk")
        for site in ("humble", "storybundle", "drivethrurpg", "itchio", "itchio-bundles"):
            logins.save_session(COOKIE if site in ("humble", "storybundle", "itchio-bundles") else "k" * 40, site)
    finally:
        db.close_all()
        for k, v in was.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        settings.reload()
    return base


def backup_made(disk: Path) -> bool:
    from hylly import backup

    return bool(backup.saved_zips(disk)) and (disk / "library" / ".hyllycopy" / "record.jsonl").is_file()


class AShop(Shop):
    """Every shop at once, with nothing new in it: what a dry run asks of a shop's site is answered, so that it gets to its
    own report. Humble's orders are the made-up ones (test_plan.py)."""

    def order_keys(self):
        return ["K1", "K2"]

    def owned(self):  # itch.io's library
        return []

    def orders(self, keys, progress=None):
        return {k: ORDERS[k] for k in keys}, []


def every_file(*folders: Path) -> dict:
    """What a folder holds, its folders too, each file by its contents."""
    import hashlib

    found = {}
    for folder in folders:
        for path in folder.rglob("*") if folder.exists() else []:
            found[str(path)] = "folder" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@pytest.mark.parametrize("library", ["demo", "fresh"])
@pytest.mark.parametrize("name", dry_runs())
def test_a_dry_run_changes_nothing(name, library, made_up, tmp_path, monkeypatch, capsys, request):
    """The owner's rule, no exceptions: every command that has --dry-run (-n) says what it would do and changes nothing:
    no file in the library, the settings folder, the settings file or a folder beside them, no database, no lock, no record
    of a run. On a demo library with its records, and on a new one that no database keeps yet, with no settings folder."""
    import io
    import shutil

    if name.startswith("server autostart"):  # what only the usual settings folder may do: tried with nothing beneath it
        request.getfixturevalue("usual_folder")

    from hylly import db, drivethrurpg, humble, instance, itchio, itchio_survey, storybundle

    def nowhere(self, request):
        raise httpx.ConnectError("no network for a dry run's test")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", nowhere)  # what isn't a shop below asks nothing at all
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    for module, name_of in ((humble, "HumbleClient"), (storybundle, "StoryBundle"), (drivethrurpg, "DriveThruRPG"),
                            (itchio, "Itch"), (itchio_survey, "Web")):
        monkeypatch.setattr(module, name_of, AShop)
    monkeypatch.setattr(itchio, "library", lambda itch, state, **kw: [])
    monkeypatch.setattr(cli, "read_clipboard", lambda: COOKIE)
    for tool in ("_schtasks", "_systemctl", "_systemctl_wide", "_launchctl"):
        monkeypatch.setattr(instance, tool, lambda *a: subprocess.CompletedProcess(a, 1, "", "not here"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"when": "2026-11-09T22:00:00", "target": "D:/disk", "computer": "pc",
                                                            "files": 1, "bytes": 1, "saved": "x.zip", "set_aside": 0})))
    out, settings_file = tmp_path / "out", paths.settings_file()
    if library == "demo":
        shutil.copytree(made_up / "demo" / "library", tmp_path / "library")
        shutil.copytree(made_up / "settings", paths.config_dir())
        for part in ("old", "disk"):
            shutil.copytree(made_up / part, out / part)
    else:
        (tmp_path / "library" / "ebooks").mkdir(parents=True)
        (tmp_path / "library" / "ebooks" / "Ann Writer - A Book.epub").write_bytes(b"not really a book")
        paths.marker_file(tmp_path / "library").parent.mkdir(parents=True)
        paths.marker_file(tmp_path / "library").write_text('{"id": "made-up"}', encoding="utf-8")
        (out / "old").mkdir(parents=True)
        (out / "old" / "A Book.epub").write_bytes(b"not really a book")
    lib = tmp_path / "library"
    parser = next(p for n, p, _ in commands.leaves(internal=True) if n == name)
    words = [w.format(lib=lib, out=out) for w in DRY_WORDS.get(name, [])]
    takes = {o for a in parser._actions for o in a.option_strings}
    argv = [*name.split(), *(words[:1] if name == "internal job" else []), "-n",
            *(words[1:] if name == "internal job" else words), *(["--library", str(lib)] if "--library" in takes else [])]
    if name == "internal job":  # its words are the command it would start: -n is its own, before them
        argv = ["internal", "job", "-n", *words]
    if name in ("server restart", "server autostart on"):  # the server they start takes the settings' library, as any hidden one
        monkeypatch.setenv("HYLLY_LIBRARY", str(lib))
    # the library's own folder and the one beside it (out), the settings folder with the job output beside it, the file
    looked = (tmp_path, paths.config_dir().parent, paths.job_output_dir(), settings_file.parent)
    settings_there = paths.config_dir().exists()
    before = every_file(*looked)
    try:
        cli.main(argv)
        code = 0
    except SystemExit as e:
        code = e.code
    said = capsys.readouterr()
    # it ran, with what it takes: a new one gets its words in DRY_WORDS. On a new library a user named there isn't one,
    # which is refused as a mistake too, in Hylly's own words: argparse's usage line is what says the words are wrong
    assert not (code == exits.USAGE and "usage: hylly" in said.err), f"{argv}: {said.err}"
    assert code != exits.USAGE or library == "fresh", f"{argv}: {said.err}"
    if library == "demo":  # with all it needs: its own dry run's report, to its end
        assert code in (0, None), f"{argv} ended {code}: {said.out}{said.err}"
    assert every_file(*looked) == before, f"{name} changed something: {said.out}{said.err}"
    assert paths.config_dir().exists() == settings_there  # nor made a settings folder
    assert not db.in_dry_run()  # the next command has the real databases


def test_hyllyd_s_dry_run_changes_nothing(tmp_path):
    from hylly import daemon

    before = every_file(paths.config_dir(), tmp_path)
    daemon.main(["-n", "--library", str(tmp_path)])
    assert every_file(paths.config_dir(), tmp_path) == before and not paths.config_dir().exists()


def test_a_dry_run_s_copies_of_the_databases_are_its_own_and_nobody_else_s():
    """Another thread, a server's say, goes on with the real database while a dry run has its copy in memory."""
    from hylly import db

    real = db.database()
    db.start_dry_run()
    try:
        mine, seen = db.database(), []
        other = threading.Thread(target=lambda: seen.append(db.database()))
        other.start()
        other.join()
        assert mine is not real and mine.copy and seen == [real] and not real.copy
    finally:
        db.end_dry_run()
    assert db.database() is real and not db.in_dry_run()


def test_a_thread_a_dry_run_starts_has_its_copies_and_a_copy_never_marks_the_library(tmp_path):
    """Where Hylly starts a thread (db.carried) it goes with the dry run; and a copy in memory, asked from any thread,
    takes the library on in memory only: no marker is written in the library."""
    from hylly import db

    root = tmp_path / "library"
    (root / "state").mkdir(parents=True)
    db.start_dry_run()
    try:
        mine, seen = db.database(), []
        carried = threading.Thread(target=db.carried(lambda: seen.append(db.database())))
        carried.start()
        carried.join()
        assert seen == [mine] and mine.copy
        elsewhere = threading.Thread(target=lambda: db.own(mine, root))  # a thread with none of the dry run's context
        elsewhere.start()
        elsewhere.join()
        assert not db.owner_file(root).exists()
    finally:
        db.end_dry_run()


def test_a_dry_run_reads_the_database_whole_and_leaves_nothing_beside_it(tmp_path, monkeypatch):
    """Copied at one moment: a file that changed while it was read is read again. A database nobody has open gets no
    journal beside it from being read."""
    from hylly import db

    where = tmp_path / "x.db"
    c = db.connect(where)
    c.execute("CREATE TABLE t (n)")
    c.execute("INSERT INTO t VALUES (1)")
    c.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.db"]
    real, asked = db._stamp, []
    changing = iter([(1, 10, False), (2, 10, False)])  # another program wrote it during the first read

    def stamp(path):
        asked.append(path)
        return next(changing, None) or real(path)

    monkeypatch.setattr(db, "_stamp", stamp)
    copy = db.snapshot(where)
    assert copy.execute("SELECT n FROM t").fetchall()[0][0] == 1 and len(asked) == 4  # read twice: before and after each
    copy.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.db"]
